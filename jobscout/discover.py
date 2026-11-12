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
from .ats import workday, simple, eightfold


class LookupUnsure(Exception):
    """Nothing found, but some checks couldn't get an answer (rate limit, timeout)."""

ATS_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
]
_GH_NOT = {"embed", "v1", "jobs"}
WORKDAY_SHARDS = ["wd1", "wd5", "wd3", "wd12", "wd2", "wd10", "wd103", "wd108", "wd501", "wd503"]

# Path segments that show up in crawled ATS URLs but aren't company boards.
_NOT_BOARDS = {"embed", "v1", "jobs", "api", "app", "apply", "assets", "static", "careers",
               "job", "search", "login", "privacy", "terms", "sitemap", "robots", "favicon",
               "acceptable-use-policy", "_incapsula_resource", "example"}


def plausible(ats: str, ids: dict) -> bool:
    """Cheap sanity check before spending a request on a crawled board name."""
    if ats == "workday":
        return ids.get("site", "").lower() not in workday._NOT_SITES_LOWER
    slug = str(next(iter(ids.values()), ""))
    s = slug.lower().replace("%20", " ")  # Ashby boards can have encoded spaces
    return (1 < len(s) <= 40 and s not in _NOT_BOARDS and not s.isdigit()
            and not re.search(r"[%\\\\\"'…“”]|\.\.|^[.\-_ ]|\.(xml|html?|js|css|png|svg|ico)$", s)
            and not re.fullmatch(r"[0-9a-f-]{20,}|[0-9a-z_-]{60,}", s)   # uuids / hashes / tokens
            and not re.fullmatch(r"\d{4}[a-z]{1,2}-\d+", s))           # "2022b-28" style ids
WORKDAY_SITE_GUESSES = ["External", "Careers", "{t}", "{T}", "{T}Careers", "{t}careers",
                        "External_Careers", "{T}_Careers", "{T}_External", "ExternalCareers",
                        "Search", "jobs", "careers"]


def slug_candidates(name: str, extra: list[str] | None = None) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    cands = list(extra or [])
    cands += ["".join(words), "-".join(words)]
    if len(words) == 1:
        cands.append(words[0])
    # A lone first word ("new" for New Balance, "ralph" for Ralph Lauren) matches the
    # wrong company too often, so multi-word names only try the joined forms.
    return list(dict.fromkeys(c for c in cands if c))


def _validate(ats: str, ids: dict) -> bool | None:
    """True = real board, False = definitely not, None = couldn't tell right now."""
    if ats == "eightfold":
        return eightfold.validate(ids)
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
    found = [(a, i) for a, i in found if plausible(a, i)]
    # de-dupe, keep order
    uniq, seen = [], set()
    for ats, ids in found:
        k = (ats, tuple(sorted(ids.items())))
        if k not in seen:
            seen.add(k)
            uniq.append((ats, ids))
    return uniq


def _probe_workday(slug: str, unsure: list) -> dict | None:
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
            ok = workday.validate(ids)
            if ok:
                return ids
            if ok is None:
                unsure.append(host)
        # tenant exists on this shard or not, other shards won't help if we got a real page
        if r.status_code == 200:
            break
    return None


def resolve(entry: dict) -> tuple[str, dict] | None:
    """Return (ats, ids) for a companies.yaml entry, or None if it's definitely not found.
    Raises LookupUnsure when nothing was found but some checks got no answer."""
    from .ats import ID_KEYS
    extra_slugs = list(entry.get("slugs") or [])
    unsure: list = []

    def ok(ats: str, ids: dict) -> bool:
        v = _validate(ats, ids)
        if v is None:
            unsure.append((ats, ids))
        return bool(v)

    # 0) explicit hints, still validated
    if entry.get("url"):
        wd = workday.parse_url(entry["url"])
        if wd:
            if ok("workday", wd):
                return "workday", wd
            extra_slugs.append(wd["tenant"])
    ats = entry.get("ats")
    if ats in ID_KEYS and all(k in entry for k in ID_KEYS[ats]):
        ids = {k: entry[k] for k in ID_KEYS[ats]}
        if ok(ats, ids):
            return ats, ids

    # 1) careers page scan
    url = entry.get("careers_url")
    if url:
        wd = workday.parse_url(url)
        if wd and ok("workday", wd):
            return "workday", wd
        try:
            r = http.get(url, allow_redirects=True, timeout=20)
            wd = workday.parse_url(r.url)
            if wd and ok("workday", wd):
                return "workday", wd
            if r.status_code == 200:
                for a, ids in from_html(r.text):
                    if ok(a, ids):
                        return a, ids
            elif r.status_code not in http.GONE:
                unsure.append(url)
        except Exception:
            unsure.append(url)

    # 2) slug probing (cheap APIs first, Workday last)
    slugs = slug_candidates(entry["name"], extra_slugs)
    for s in slugs:
        for a, ids in (("greenhouse", {"token": s}), ("ashby", {"org": s}), ("lever", {"slug": s})):
            if ok(a, ids):
                return a, ids
    for s in slugs:
        if ok("smartrecruiters", {"company_id": s}):
            return "smartrecruiters", {"company_id": s}
    for s in slugs:
        hit = _probe_workday(s, unsure)
        if hit:
            return "workday", hit
    if unsure:
        raise LookupUnsure(f"{len(unsure)} checks got no answer")
    return None
