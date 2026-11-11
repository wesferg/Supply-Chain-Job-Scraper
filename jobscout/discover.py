"""Figure out which ATS a company uses and the IDs needed to query it.

A company entry in companies.yaml can be:
  - fully specified (ats + ids)        -> used as-is
  - a careers page URL and/or slugs    -> resolved here, cached in data/resolved.json

Resolution order:
  1. Scan the careers page HTML for links to a known ATS.
  2. Probe likely slugs against each ATS's public API.
"""
from __future__ import annotations

import re

from . import http
from .ats import workday, simple

ATS_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
]
_GH_NOT = {"embed", "v1", "jobs"}
WORKDAY_SHARDS = ["wd1", "wd5", "wd3", "wd12", "wd2", "wd10", "wd103", "wd501"]
WORKDAY_SITE_GUESSES = ["External", "Careers", "{t}", "{T}", "{T}Careers", "{t}careers",
                        "External_Careers", "{T}_Careers", "{T}_External", "ExternalCareers",
                        "Search", "jobs", "careers"]


def slug_candidates(name: str, extra: list[str] | None = None) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    cands = list(extra or [])
    cands += ["".join(words), "-".join(words)]
    if words:
        cands.append(words[0])
    return list(dict.fromkeys(c for c in cands if c))


def _validate(ats: str, ids: dict) -> bool:
    if ats == "workday":
        return workday.validate(ids)
    if ats == "greenhouse":
        return simple.greenhouse_validate(ids["token"])
    if ats == "lever":
        return simple.lever_validate(ids["slug"])
    if ats == "ashby":
        return simple.ashby_validate(ids["org"])
    if ats == "smartrecruiters":
        return simple.smartrecruiters_validate(ids["company_id"])
    return False


def from_html(html_text: str) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for url in re.findall(r"https?://[^\s\"'<>]+", html_text):
        wd = workday.parse_url(url)
        if wd:
            found.append(("workday", wd))
    for ats, rx in ATS_LINK_PATTERNS:
        for m in rx.finditer(html_text):
            slug = m.group(1)
            if ats == "greenhouse" and slug in _GH_NOT:
                continue
            key = {"greenhouse": "token", "lever": "slug", "ashby": "org",
                   "smartrecruiters": "company_id"}[ats]
            found.append((ats, {key: slug}))
    # de-dupe, keep order
    uniq, seen = [], set()
    for ats, ids in found:
        k = (ats, tuple(sorted(ids.items())))
        if k not in seen:
            seen.add(k)
            uniq.append((ats, ids))
    return uniq


def _probe_workday(slug: str) -> dict | None:
    for shard in WORKDAY_SHARDS:
        host = f"{slug}.{shard}.myworkdayjobs.com"
        # The bare host usually redirects to the default careers site.
        try:
            r = http.get(f"https://{host}/", allow_redirects=True, timeout=15)
        except Exception:
            continue
        if r.status_code >= 400 and r.status_code != 404:
            continue
        parsed = workday.parse_url(r.url)
        if parsed and workday.validate(parsed):
            return parsed
        if r.status_code == 404 and "myworkdayjobs" not in r.url:
            continue
        for g in WORKDAY_SITE_GUESSES:
            site = g.format(t=slug, T=slug.capitalize())
            ids = {"host": host, "tenant": slug, "site": site}
            if workday.validate(ids):
                return ids
        # tenant exists on this shard or not, other shards won't help if we got a real page
        if r.status_code == 200:
            break
    return None


def resolve(entry: dict) -> tuple[str, dict] | None:
    """Return (ats, ids) for a companies.yaml entry, or None."""
    from .ats import ID_KEYS
    extra_slugs = list(entry.get("slugs") or [])

    # 0) explicit hints, still validated
    if entry.get("url"):
        wd = workday.parse_url(entry["url"])
        if wd:
            if workday.validate(wd):
                return "workday", wd
            extra_slugs.append(wd["tenant"])
    ats = entry.get("ats")
    if ats in ID_KEYS and all(k in entry for k in ID_KEYS[ats]):
        ids = {k: entry[k] for k in ID_KEYS[ats]}
        if _validate(ats, ids):
            return ats, ids

    # 1) careers page scan
    url = entry.get("careers_url")
    if url:
        wd = workday.parse_url(url)
        if wd and workday.validate(wd):
            return "workday", wd
        try:
            r = http.get(url, allow_redirects=True, timeout=20)
            wd = workday.parse_url(r.url)
            if wd and workday.validate(wd):
                return "workday", wd
            if r.status_code == 200:
                for ats, ids in from_html(r.text):
                    if _validate(ats, ids):
                        return ats, ids
        except Exception:
            pass

    # 2) slug probing (cheap APIs first, Workday last)
    slugs = slug_candidates(entry["name"], extra_slugs)
    for s in slugs:
        if simple.greenhouse_validate(s):
            return "greenhouse", {"token": s}
        if simple.ashby_validate(s):
            return "ashby", {"org": s}
        if simple.lever_validate(s):
            return "lever", {"slug": s}
    for s in slugs:
        if simple.smartrecruiters_validate(s):
            return "smartrecruiters", {"company_id": s}
    for s in slugs:
        hit = _probe_workday(s)
        if hit:
            return "workday", hit
    return None
