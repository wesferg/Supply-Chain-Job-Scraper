"""Workday career sites (*.myworkdayjobs.com and *.myworkdaysite.com).

Every Workday careers page loads its jobs from a public JSON endpoint:
  POST https://{host}/wday/cxs/{tenant}/{site}/jobs   (list, 20 per page)
  GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}   (detail)
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

from .. import http
from ..models import Job, BoardUnavailable
from ..text import html_to_text

PAGE = 20
MAX_PAGES_PER_TERM = 5  # 100 results per search term is plenty after keyword search

_JOBS_URL = re.compile(
    r"https?://(?P<host>(?P<tenant>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_\-]+)",
)
_SITE_URL = re.compile(
    r"https?://(?P<host>wd\d+\.myworkdaysite\.com)"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[a-z0-9_-]+)/(?P<site>[A-Za-z0-9_\-]+)",
)
_NOT_SITES = {"wday", "job", "details", "login", "apply", "userHome"}


def parse_url(url: str) -> dict | None:
    """Turn any Workday careers/job URL into {host, tenant, site}."""
    for rx in (_SITE_URL, _JOBS_URL):
        m = rx.search(url)
        if (m and m.group("site") not in _NOT_SITES
                and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}|[a-z]{2}", m.group("site"))
                and "." not in m.group("site")):
            return {"host": m.group("host"), "tenant": m.group("tenant"), "site": m.group("site")}
    return None


def _base(c: dict) -> str:
    return f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}"


def public_url(c: dict, external_path: str) -> str:
    if "myworkdaysite" in c["host"]:
        return f"https://{c['host']}/en-US/recruiting/{c['tenant']}/{c['site']}{external_path}"
    return f"https://{c['host']}/en-US/{c['site']}{external_path}"


def validate(c: dict) -> bool:
    data = http.post_json(_base(c) + "/jobs",
                          {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    return isinstance(data, dict) and "jobPostings" in data


def _list(c: dict, term: str) -> Iterable[dict]:
    total = None
    for page in range(MAX_PAGES_PER_TERM):
        data = http.post_json(_base(c) + "/jobs", {
            "appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": term,
        })
        if not isinstance(data, dict):
            if page == 0:
                raise BoardUnavailable(f"workday:{c['host']}/{c['site']}")
            return
        posts = data.get("jobPostings") or []
        if total is None:
            total = data.get("total") or 0  # Workday only reports total on the first page
        yield from posts
        if len(posts) < PAGE or (page + 1) * PAGE >= total:
            return


def job_id_for(path: str) -> str:
    """Stable id from the posting path, e.g. 'Planner--Demand-Agg_R210823'."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch(name: str, c: dict, search_terms: list[str],
          title_ok: Callable[[str], bool], known: set[str] | None = None,
          loc_ok: Callable[[list], bool] = lambda _: True) -> list[Job]:
    """known: job keys already decided on a previous run; their (slow) detail
    pages are skipped and they come back flagged 'cached'.
    loc_ok: screens the list page's location text before any detail call."""
    known = known or set()
    seen: dict[str, dict] = {}
    for term in search_terms:
        for p in _list(c, term):
            path = p.get("externalPath")
            if (path and path not in seen and title_ok(p.get("title", ""))
                    and loc_ok([p.get("locationsText", "")])):
                seen[path] = p

    jobs: list[Job] = []
    for path, p in seen.items():
        jid = job_id_for(path)
        if f"workday:{name}:{jid}" in known:
            jobs.append(Job(ats="workday", company=name, job_id=jid, title=p.get("title", ""),
                            url=public_url(c, path), locations=[p.get("locationsText", "")],
                            flags=["cached"]))
            continue
        detail = http.get_json(_base(c) + path) or {}
        info = detail.get("jobPostingInfo") or {}
        locs = [info.get("location")] + list(info.get("additionalLocations") or [])
        locs = [l for l in locs if l] or [p.get("locationsText", "")]
        jobs.append(Job(
            ats="workday",
            company=name,
            job_id=jid,
            title=info.get("title") or p.get("title", ""),
            url=info.get("externalUrl") or public_url(c, path),
            locations=locs,
            description=html_to_text(info.get("jobDescription", "")),
            posted=info.get("startDate") or p.get("postedOn", ""),
            time_type=info.get("timeType", ""),
            remote="remote" in " ".join(locs).lower(),
        ))
    return jobs
