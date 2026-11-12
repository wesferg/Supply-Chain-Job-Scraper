"""Eightfold career sites (careers.<company>.com/careers), e.g. Lam Research.

The careers page loads jobs from a public JSON endpoint:
  GET https://{host}/api/apply/v2/jobs?domain={domain}&start=N&num=10&query=TERM   (list)
  GET https://{host}/api/apply/v2/jobs/{id}?domain={domain}                       (detail)
Newer sites use /api/pcsx/search with the same parameters, so both are tried.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .. import http
from ..models import Job, BoardUnavailable
from ..text import html_to_text

PAGE = 10
MAX_PAGES_PER_TERM = 5


def _list_url(c: dict) -> str:
    return f"https://{c['host']}/api/apply/v2/jobs"


def _positions(d) -> list[dict] | None:
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("positions"), list):
        return d["positions"]
    data = d.get("data")
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return data["positions"]
    return None


def _page(c: dict, term: str, start: int):
    params = {"domain": c["domain"], "start": start, "num": PAGE, "query": term,
              "sort_by": "relevance"}
    d = http.get_json(_list_url(c), params=params)
    pos = _positions(d)
    if pos is None:  # newer API
        d = http.get_json(f"https://{c['host']}/api/pcsx/search", params=params)
        pos = _positions(d)
    return pos


def validate(c: dict) -> bool | None:
    code, d = http.status_json(_list_url(c) + f"?domain={c['domain']}&start=0&num=1")
    if code == 200:
        return _positions(d) is not None
    return False if code in http.GONE else None


def _locs(p: dict) -> list[str]:
    locs = p.get("locations") or []
    if isinstance(locs, str):
        locs = [locs]
    locs = [l if isinstance(l, str) else (l or {}).get("name", "") for l in locs]
    if not locs and p.get("location"):
        locs = [p["location"]]
    return [l for l in locs if l]


def _posted(p: dict) -> str:
    ts = p.get("t_create") or p.get("postedTs") or p.get("t_update")
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def fetch(name: str, c: dict, search_terms: list[str], title_ok: Callable[[str], bool],
          known: set | None = None, loc_ok=lambda locs: True) -> list[Job]:
    known = known or set()
    seen: dict[str, dict] = {}
    for ti, term in enumerate(search_terms):
        for page in range(MAX_PAGES_PER_TERM):
            pos = _page(c, term, page * PAGE)
            if pos is None:
                if ti == 0 and page == 0:
                    raise BoardUnavailable(f"eightfold:{c['host']}")
                break
            for p in pos:
                if p.get("id") is not None:
                    seen.setdefault(str(p["id"]), p)
            if len(pos) < PAGE:
                break

    out: list[Job] = []
    for jid, p in seen.items():
        title = (p.get("name") or p.get("title") or "").strip()
        locs = _locs(p)
        if not title_ok(title) or not loc_ok(locs):
            continue
        url = (p.get("canonicalPositionUrl") or p.get("positionUrl")
               or f"https://{c['host']}/careers/job/{jid}")
        if url.startswith("/"):
            url = f"https://{c['host']}{url}"
        job = Job(ats="eightfold", company=name, job_id=jid, title=title, url=url,
                  locations=locs, posted=_posted(p))
        if job.key in known:
            job.flags.append("cached")
            out.append(job)
            continue
        desc = p.get("job_description") or ""
        if not desc:
            d = http.get_json(f"https://{c['host']}/api/apply/v2/jobs/{jid}",
                              params={"domain": c["domain"]})
            desc = (d or {}).get("job_description") or ""
        job.description = html_to_text(desc)
        job.remote = "remote" in str(p.get("work_location_option") or "").lower()
        out.append(job)
    return out
