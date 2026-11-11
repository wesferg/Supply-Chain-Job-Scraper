"""Discover job boards at scale and pick which ones to sweep each day.

Sources (all optional, all fail soft):
  - Common Crawl URL index   (public, no key)
  - Wayback Machine CDX index (public, no key)
  - Brave web search          (BRAVE_API_KEY; finds boards with live niche postings)

Everything lives in data/universe.json:
  {"meta": {...cursors...}, "boards": {board_key: {ats, ids, name, source, added,
                                                  last_scanned, last_hit, hits, fails}}}
"""
from __future__ import annotations

import itertools
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import http, store, discover
from .ats import board_key

FILE = "universe.json"


# ---------------- storage ----------------

def load() -> dict:
    p = os.path.join(store.DATA, FILE)
    if os.path.exists(p):
        with open(p) as f:
            u = json.load(f)
    else:
        u = {}
    u.setdefault("meta", {})
    u.setdefault("boards", {})
    return u


def save(u: dict) -> None:
    os.makedirs(store.DATA, exist_ok=True)
    with open(os.path.join(store.DATA, FILE), "w") as f:
        json.dump(u, f, separators=(",", ":"), sort_keys=True)


# ---------------- URL -> board ----------------

def pretty_name(ats: str, ids: dict) -> str:
    raw = ids.get("tenant") or ids.get("token") or ids.get("slug") or ids.get("org") or ids.get("company_id") or ""
    raw = re.sub(r"[-_.%]+", " ", raw).strip()
    return raw[:1].upper() + raw[1:] if raw.islower() or " " in raw else raw


def boards_from_urls(urls) -> dict[str, tuple[str, dict]]:
    """Map board_key -> (ats, ids) for every recognizable ATS URL."""
    out: dict[str, tuple[str, dict]] = {}
    chunk: list[str] = []

    def flush():
        for ats, ids in discover.from_html(" ".join(chunk)):
            k = board_key(ats, ids)
            out.setdefault(k, (ats, ids))
        chunk.clear()

    for u in urls:
        if u:
            chunk.append(u if u.startswith("http") else "https://" + u)
        if len(chunk) >= 5000:
            flush()
    flush()
    return out


# ---------------- sources ----------------

def _commoncrawl(patterns: list[dict], pages: int, meta: dict) -> list[str]:
    info = http.get_json("https://index.commoncrawl.org/collinfo.json", timeout=60)
    if not info:
        print("  commoncrawl: index list unavailable")
        return []
    api = info[0]["cdx-api"]
    cursors = meta.setdefault("cc_cursor", {})
    if meta.get("cc_index") != api:          # new crawl published: start from page 0
        meta["cc_index"], cursors = api, {}
        meta["cc_cursor"] = cursors
    urls: list[str] = []
    for p in patterns:
        q = f"{api}?url={p['host']}&output=json&fl=url"
        n = http.get_json(q + "&showNumPages=true", timeout=120) or {}
        total = int(n.get("pages") or 0)
        if not total:
            continue
        start = cursors.get(p["host"], 0)
        for i in range(pages):
            page = (start + i) % total
            try:
                r = http.get(f"{q}&page={page}", timeout=180)
            except Exception as e:
                print(f"  commoncrawl {p['host']} p{page}: {e}")
                continue
            if r.status_code != 200:
                continue
            for line in r.text.splitlines():
                try:
                    urls.append(json.loads(line)["url"])
                except (ValueError, KeyError):
                    pass
        cursors[p["host"]] = (start + pages) % total
        print(f"  commoncrawl {p['host']}: pages {start}..{start + pages - 1} of {total}")
    return urls


def _wayback(patterns: list[dict], rows: int, since: int, meta: dict) -> list[str]:
    offsets = meta.setdefault("wb_offset", {})
    urls: list[str] = []
    for p in patterns:
        off = offsets.get(p["wayback"], 0)
        q = (f"https://web.archive.org/cdx/search/cdx?url={p['wayback']}&matchType={p['match']}"
             f"&fl=original&collapse=urlkey&from={since}&limit={rows}&offset={off}")
        try:
            r = http.get(q, timeout=180)
        except Exception as e:
            print(f"  wayback {p['wayback']}: {e}")
            continue
        lines = r.text.split() if r.status_code == 200 else []
        urls += lines
        offsets[p["wayback"]] = 0 if len(lines) < rows else off + rows
        print(f"  wayback {p['wayback']}: {len(lines)} urls from offset {off}")
    return urls


def _brave(cfg: dict, meta: dict) -> list[str]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    combos = list(itertools.product(cfg["roles"], cfg["places"], cfg["sites"]))
    start = meta.get("brave_cursor", 0) % len(combos)
    n = min(cfg.get("queries_per_run", 60), len(combos))
    urls: list[str] = []
    for i in range(n):
        role, place, site = combos[(start + i) % len(combos)]
        q = f'site:{site} "{role}" "{place}"'
        d = http.get_json("https://api.search.brave.com/res/v1/web/search",
                          params={"q": q, "count": 20},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
        for res in ((d or {}).get("web") or {}).get("results") or []:
            urls.append(res.get("url", ""))
    meta["brave_cursor"] = (start + n) % len(combos)
    print(f"  brave: {n} searches, {len(urls)} result urls")
    return urls


# ---------------- grow ----------------

def _validate(item):
    k, (ats, ids) = item
    try:
        return k, discover._validate(ats, ids)
    except Exception:
        return k, False


def grow(cfg: dict, curated_keys: set[str]) -> dict:
    """Harvest board URLs from every enabled source, validate new ones, add them."""
    u = load()
    g = cfg["grow"]
    src = g["sources"]
    urls: list[str] = []
    if src.get("brave", {}).get("enabled"):
        urls += _brave(src["brave"], u["meta"])          # most targeted first
    if src.get("commoncrawl", {}).get("enabled"):
        urls += _commoncrawl(g["patterns"], src["commoncrawl"].get("pages_per_pattern", 2), u["meta"])
    if src.get("wayback", {}).get("enabled"):
        urls += _wayback(g["patterns"], src["wayback"].get("rows_per_pattern", 20000),
                         src["wayback"].get("since_year", 2025), u["meta"])

    found = boards_from_urls(urls)
    excl = re.compile(cfg.get("exclude_board_names") or r"(?!x)x", re.I)
    dead = set(u["meta"].get("dead", []))
    new = [(k, v) for k, v in found.items()
           if k not in u["boards"] and k not in curated_keys and k not in dead
           and not excl.search(pretty_name(*v))]
    new = new[: g.get("max_new_boards", 2000)]
    print(f"grow: {len(urls)} urls -> {len(found)} boards -> {len(new)} new to validate")

    today = date.today().isoformat()
    added = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        for k, ok in pool.map(_validate, new):
            if ok:
                ats, ids = found[k]
                u["boards"][k] = {"ats": ats, "ids": ids, "name": pretty_name(ats, ids),
                                  "added": today, "last_scanned": "", "last_hit": "",
                                  "hits": 0, "fails": 0}
                added += 1
            else:
                dead.add(k)
    u["meta"]["dead"] = sorted(dead)[-50000:]
    u["meta"]["last_grow"] = today
    save(u)
    print(f"grow: added {added}; universe now {len(u['boards'])} boards")
    return {"urls": len(urls), "boards_seen": len(found), "added": added, "total": len(u["boards"])}


# ---------------- daily slice ----------------

def cost(board: dict) -> int:
    return 3 if board["ats"] == "workday" else 1


def todays_slice(u: dict, budget: int, promote_days: int, skip_keys: set[str]) -> list[str]:
    """Promoted boards (recent hits) always; then least-recently-scanned until the budget is spent."""
    cutoff = (date.today() - timedelta(days=promote_days)).isoformat()
    boards = {k: b for k, b in u["boards"].items() if k not in skip_keys}
    hot = [k for k, b in boards.items() if b.get("last_hit", "") >= cutoff and b.get("last_hit")]
    picked, spent = list(hot), sum(cost(boards[k]) for k in hot)
    rest = sorted((k for k in boards if k not in set(hot)),
                  key=lambda k: (boards[k].get("last_scanned") or "", k))
    for k in rest:
        c = cost(boards[k])
        if spent + c > budget:
            break
        picked.append(k)
        spent += c
    return picked
