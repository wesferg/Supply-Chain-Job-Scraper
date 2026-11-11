"""The daily run: resolve companies -> fetch -> filter -> score -> write results."""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import yaml

from . import discover, store, scoring, universe
from .ats import FETCHERS, board_key
from .filters import Filters
from .models import Job, BoardUnavailable

CONFIG = os.path.join(store.ROOT, "config")
RETRY_FAILED_AFTER_DAYS = 7


def load_yaml(name: str) -> dict:
    with open(os.path.join(CONFIG, name)) as f:
        return yaml.safe_load(f)


def company_ids(entry: dict, state: store.State, log: list[str]) -> tuple[str, dict] | None:
    """Cached result if we have one, else resolve (validating any hints) and cache it."""
    name = entry["name"]
    cached = state.resolved.get(name)
    today = date.today()
    if cached and cached.get("ats"):
        return cached["ats"], cached["ids"]
    if cached and cached.get("failed_on", "") > (today - timedelta(days=RETRY_FAILED_AFTER_DAYS)).isoformat():
        return None

    hit = discover.resolve(entry)
    if hit:
        state.resolved[name] = {"ats": hit[0], "ids": hit[1], "found_on": today.isoformat()}
        log.append(f"{name}: discovered {hit[0]} {hit[1]}")
        return hit
    state.resolved[name] = {"failed_on": today.isoformat()}
    log.append(f"{name}: could not find a supported careers site (add it by hand in companies.yaml)")
    return None


def curated_board_keys(state: store.State) -> set[str]:
    return {board_key(v["ats"], v["ids"]) for v in state.resolved.values() if v.get("ats")}


def run(dry_run: bool = False, only: str | None = None, use_llm: bool = True,
        workers: int = 10, sweep_universe: bool = True) -> dict:
    filters_cfg = load_yaml("filters.yaml")
    profile = load_yaml("profile.yaml")
    companies = load_yaml("companies.yaml")["companies"]
    ucfg = load_yaml("universe.yaml") if os.path.exists(os.path.join(CONFIG, "universe.yaml")) else {}
    if only:
        companies = [c for c in companies if c["name"].lower() == only.lower()]
        sweep_universe = False
    if not use_llm:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    f = Filters(filters_cfg)
    state = store.State()
    known = state.known_keys()
    today = date.today().isoformat()
    log: list[str] = []
    started = time.monotonic()

    # ---- what to check today: curated companies + a slice of the discovered universe
    targets: list[dict] = []
    for entry in companies:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            log.append(f"companies.yaml: skipped an entry with an unusable name ({entry!r}); "
                       "put quotes around names like \"On\" or \"Yes\"")
            continue
        try:
            ids = company_ids(entry, state, log)
        except Exception as e:
            log.append(f"{name}: lookup ERROR {type(e).__name__}: {e}")
            continue
        if ids:
            targets.append({"name": entry["name"], "ats": ids[0], "ids": ids[1],
                            "origin": "curated", "terms": f.search_terms})

    if not dry_run:
        state.save()  # keep company lookups even if something later in the run fails

    u = None
    sweep = ucfg.get("sweep", {})
    if sweep_universe and ucfg.get("enabled"):
        # Find new companies automatically: on the first run, then every `every_days`.
        every = ucfg.get("grow", {}).get("every_days", 7)
        last = universe.load()["meta"].get("last_grow", "")
        due = not last or last <= (date.today() - timedelta(days=every)).isoformat()
        if due and not dry_run:
            try:
                g = universe.grow(ucfg, curated_board_keys(state))
                log.append(f"[company discovery] found {g['added']} new job boards "
                           f"({g['total']} in the discovered set)")
            except Exception as e:
                log.append(f"[company discovery] ERROR {type(e).__name__}: {e}")
        u = universe.load()
        excl = re.compile(ucfg.get("exclude_board_names") or r"(?!x)x", re.I)
        for k in universe.todays_slice(u, sweep.get("daily_budget_units", 1500),
                                       sweep.get("promote_days", 45), curated_board_keys(state)):
            b = u["boards"][k]
            if excl.search(b["name"]):
                continue
            targets.append({"name": b["name"], "ats": b["ats"], "ids": b["ids"], "origin": "universe",
                            "bkey": k, "terms": sweep.get("workday_search_terms", f.search_terms)})
    deadline = time.monotonic() + 60 * sweep.get("max_minutes", 40)

    def fetch_one(t: dict):
        if t["origin"] == "universe" and time.monotonic() > deadline:
            return t, None, "skipped"
        try:
            jobs = FETCHERS[t["ats"]](t["name"], t["ids"], f.title_ok, f.loc_maybe, t["terms"], known)
            return t, jobs, None
        except BoardUnavailable as e:
            return t, [], f"unavailable ({e})"
        except Exception as e:
            return t, [], f"{type(e).__name__}: {e}"

    fetched: list[Job] = []
    board_of: dict[str, dict] = {}  # job key -> target
    uni_stats = {"scanned": 0, "failed": 0, "skipped": 0, "postings": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(fetch_one, t) for t in targets]):
            t, jobs, err = fut.result()
            if t["origin"] == "curated":
                log.append(f"{t['name']}: ERROR {err}" if err else
                           f"{t['name']}: {len(jobs)} postings in your field")
            elif u is not None:
                b = u["boards"][t["bkey"]]
                if err == "skipped":
                    uni_stats["skipped"] += 1
                    continue
                b["last_scanned"] = today
                if err:
                    b["fails"] = b.get("fails", 0) + 1
                    uni_stats["failed"] += 1
                else:
                    b["fails"] = 0
                    uni_stats["scanned"] += 1
                    uni_stats["postings"] += len(jobs)
            for j in jobs or []:
                board_of[j.key] = t
            fetched.extend(jobs or [])

    llm_left = sweep.get("max_llm_scores_per_run", 150)
    new_rows = []
    done: set[str] = set()
    for job in fetched:
        k = job.key
        if k in done:
            continue
        done.add(k)
        if k in state.matches:
            state.matches[k]["last_seen"] = today
            continue
        if k in state.rejected:
            state.rejected[k]["last_seen"] = today
            continue
        if "cached" in job.flags:  # known elsewhere but pruned; skip until it's re-fetched
            continue
        d = f.evaluate(job)
        if not d.keep:
            state.rejected[k] = {"reason": d.reason, "title": job.title, "last_seen": today}
            continue
        t = board_of.get(k, {})
        scoring.score(job, profile, f.floor, f.good_pay, allow_llm=llm_left > 0)
        llm_left -= 1
        rec = {**job.to_dict(), "first_seen": today, "last_seen": today,
               "origin": t.get("origin", "curated")}
        rec.pop("description", None)
        if not rec.get("verdict") and os.environ.get("ANTHROPIC_API_KEY"):
            # Not Claude-scored yet (no key, or today's cap hit): keep enough text to score later.
            rec["pending_desc"] = job.description[:5000]
        state.matches[k] = rec
        new_rows.append(rec)
        if t.get("origin") == "universe" and u is not None:
            b = u["boards"][t["bkey"]]
            b["last_hit"] = today
            b["hits"] = b.get("hits", 0) + 1

    if u is not None:
        drop_after = sweep.get("drop_after_failures", 3)
        dead = [k for k, b in u["boards"].items() if b.get("fails", 0) >= drop_after]
        for k in dead:
            del u["boards"][k]
        hit_boards = sorted({board_of[r["key"]]["name"] for r in new_rows
                             if board_of.get(r["key"], {}).get("origin") == "universe"})
        msg = (f"[discovered boards] swept {uni_stats['scanned']} of {len(u['boards'])}, "
               f"{uni_stats['postings']} postings in your field, {uni_stats['failed']} failed")
        if dead:
            msg += f", {len(dead)} dropped"
        if uni_stats["skipped"]:
            msg += f", {uni_stats['skipped']} left for tomorrow (time limit)"
        if hit_boards:
            msg += "; new matches from: " + ", ".join(hit_boards)
        log.append(msg)

    # "Open" = seen in the last 3 days, so one failed fetch doesn't drop a company's jobs.
    recent = (date.today() - timedelta(days=2)).isoformat()
    open_rows = [r for r in state.matches.values() if r.get("last_seen", "") >= recent]

    # Spare Claude budget upgrades open matches that only have a rule score, best first,
    # so the whole list ends up ranked on the same scale.
    if os.environ.get("ANTHROPIC_API_KEY") and llm_left > 0 and not dry_run:
        pending = sorted((r for r in open_rows if not r.get("verdict") and r.get("pending_desc")),
                         key=lambda r: -(r.get("score") or 0))
        for r in pending[:llm_left]:
            job = Job(**{k: r[k] for k in Job.__dataclass_fields__ if k in r and k != "description"},
                      description=r["pending_desc"])
            scoring.score(job, profile, f.floor, f.good_pay)
            if job.verdict:
                r.update(score=job.score, verdict=job.verdict, why=job.why, concerns=job.concerns)
                r.pop("pending_desc", None)

    open_rows.sort(key=store.rank_key)
    new_rows.sort(key=store.rank_key)
    log.sort()

    summary = {"companies": len(targets), "postings_checked": len(fetched),
               "new": len(new_rows), "open": len(open_rows)}
    if dry_run:
        return {**summary, "new_rows": new_rows, "ranked": open_rows, "log": log}

    state.prune()
    state.save()
    if u is not None:
        universe.save(u)
    store.write_csv(open_rows)
    min_score = profile.get("list_min_score", 45)
    md = store.ranked_markdown(open_rows, today, log, min_score)
    with open(store.JOBS_MD, "w") as fh:
        fh.write(md)
    return {**summary, "issue": store.issue_text(open_rows, new_rows, today, min_score,
                                                  repo=os.environ.get("GITHUB_REPOSITORY"))}
