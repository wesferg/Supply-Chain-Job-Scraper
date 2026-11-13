"""State kept in the repo between runs (data/*.json) and the results files."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
JOBS_MD = os.path.join(ROOT, "JOBS.md")


def _load(name: str) -> dict:
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def _save(name: str, obj: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


class State:
    """matches.json  - every job that passed the filters, with its score
    rejected.json - job keys that failed, so we don't re-check them daily
    resolved.json - company -> ATS ids found by discovery"""

    def __init__(self):
        self.matches = _load("matches.json")
        self.rejected = _load("rejected.json")
        self.resolved = _load("resolved.json")
        self.meta = _load("meta.json")

    def known_keys(self) -> set[str]:
        return set(self.matches) | set(self.rejected)

    def prune(self, keep_days: int = 60) -> None:
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        for store in (self.matches, self.rejected):
            for k in [k for k, v in store.items() if v.get("last_seen", "") < cutoff]:
                del store[k]

    def save(self) -> None:
        _save("matches.json", self.matches)
        _save("rejected.json", self.rejected)
        _save("resolved.json", self.resolved)
        _save("meta.json", self.meta)


def company(r: dict) -> str:
    return r.get("display") or r.get("company", "")


CSV_FIELDS = ["score", "verdict", "title", "company", "metro", "locations", "salary",
              "min_years", "pref_years", "posted", "flags", "why", "concerns", "url", "first_seen", "origin", "ats"]


def _salary(r: dict) -> str:
    lo, hi = r.get("salary_min"), r.get("salary_max")
    if not hi:
        return ""
    return f"${lo/1000:,.0f}k-${hi/1000:,.0f}k" if lo and lo != hi else f"${hi/1000:,.0f}k"


def write_csv(rows: list[dict]) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "jobs.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "company": company(r), "locations": "; ".join(r.get("locations") or []),
                        "flags": " ".join(f for f in r.get("flags") or [] if f != "cached"),
                        "salary": _salary(r)})
    return path


def _md_cell(s) -> str:
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


TIERS = [(80, "Apply first"), (65, "Strong fits"), (50, "Worth a look"), (0, "Long shots")]


def rank_key(r: dict):
    """Most important first: highest score, then the newest posting, then name."""
    first = int((r.get("first_seen") or "0").replace("-", "") or 0)
    return (-(r.get("score") or 0), -first, r.get("company", ""), r.get("title", ""))


def _row(i: int, r: dict, today: str) -> str:
    notes = []
    if "entry" in (r.get("flags") or []):
        notes.append("entry-level")
    if r.get("min_years") is not None:
        notes.append(f"requires {r['min_years']}+ yrs")
    if r.get("pref_years") is not None and (r.get("min_years") is None
                                            or r["pref_years"] > r["min_years"]):
        notes.append(f"prefers {r['pref_years']}+ yrs")
    why = _md_cell(r.get("why"))
    if r.get("concerns"):
        why += f" _Watch: {_md_cell(r['concerns'])}_"
    if notes:
        why += f" `{', '.join(notes)}`"
    new = "**NEW** " if r.get("first_seen") == today else ""
    company_cell = _md_cell(company(r)) + (" \\*" if r.get("origin") == "universe" else "")
    return (f"| {i} | {r.get('score')} | {new}[{_md_cell(r['title'])}]({r['url']}) | {company_cell} "
            f"| {_md_cell(r.get('metro'))} | {_salary(r) or '-'} | {_age(r.get('posted'))} | {why} |")


def _age(posted) -> str:
    from .text import posted_date
    d = posted_date(posted)
    if not d:
        return "-"
    n = (date.today() - d).days
    return "today" if n <= 0 else f"{n}d ago"


_HEAD = ["| # | Score | Role | Company | Where | Pay | Posted | Why it fits |",
         "|---:|---:|---|---|---|---|---|---|"]


def ranked_markdown(rows: list[dict], today: str, log: list[str], min_score: int) -> str:
    """One list of every open match, most important first, grouped into tiers."""
    shown = [r for r in rows if (r.get("score") or 0) >= min_score]
    n_new = sum(1 for r in shown if r.get("first_seen") == today)
    stamp = datetime.now().strftime("%a %b %d, %Y")
    lines = [f"# Your job matches", "",
             f"Updated {stamp}: **{len(shown)} open roles**, ranked by fit"
             f"{f', **{n_new} new today**' if n_new else ''}."
             + (f" {len(rows) - len(shown)} lower-scoring roles are in `results/jobs.csv`."
                if len(rows) > len(shown) else ""), ""]
    i = 0
    for floor, label in TIERS:
        ceiling = min((f for f, _ in TIERS if f > floor), default=101)
        tier = [r for r in shown if floor <= (r.get("score") or 0) < ceiling]
        if not tier:
            continue
        lines += [f"## {label} ({floor}+)" if floor else f"## {label}", "", *_HEAD]
        for r in tier:
            i += 1
            lines.append(_row(i, r, today))
        lines.append("")
    if not shown:
        lines += ["No open matches yet.", ""]
    if any(r.get("origin") == "universe" for r in shown):
        lines += ["\\* found by automatic company discovery (not on your list)", ""]
    lines += ["<details><summary>Run log</summary>", "", *[f"- {l}" for l in log], "", "</details>"]
    return "\n".join(lines) + "\n"


def issue_text(rows: list[dict], new_rows: list[dict], today: str, min_score: int,
               top: int = 20, repo: str | None = None) -> tuple[str, str] | None:
    """Title and body for the daily issue: today's top of the ranked list. None if nothing new."""
    new_good = [r for r in new_rows if (r.get("score") or 0) >= min_score]
    if not new_good:
        return None
    shown = [r for r in rows if (r.get("score") or 0) >= min_score][:top]
    best = new_good[0]
    title = (f"{len(new_good)} new match{'es' if len(new_good) != 1 else ''} - "
             f"best new: {best['title']} @ {company(best)} ({best.get('score')})")
    body = "\n".join([
        f"Your top {len(shown)} open roles, most important first. **NEW** = first seen today.", "",
        *_HEAD, *[_row(i + 1, r, today) for i, r in enumerate(shown)], "",
        f"Full ranked list: [JOBS.md](https://github.com/{repo}/blob/main/JOBS.md)" if repo
        else "Full ranked list: JOBS.md in the repo",
    ])
    return title[:250], body
