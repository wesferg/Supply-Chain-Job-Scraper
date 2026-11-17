"""Job listings from aggregators, merged into the same filter -> score -> rank pipeline.

  simplify  SimplifyJobs/New-Grad-Positions on GitHub (structured JSON, no key)
  zapply    zapplyjobs/New-Grad-Jobs-2027 on GitHub (README table, no key)
  adzuna    Adzuna job search API (free key: ADZUNA_APP_ID + ADZUNA_APP_KEY)
  jsearch   JSearch on RapidAPI = Google for Jobs, which carries LinkedIn, Indeed,
            Glassdoor and ZipRecruiter postings (RAPIDAPI_KEY; free tier 200 calls/month)

LinkedIn, Indeed and Handshake aren't scraped directly: their terms forbid it, they
block automated traffic, and Handshake sits behind your student login. JSearch is the
sanctioned way to reach the LinkedIn/Indeed postings.

When a listing links to a Workday / Greenhouse / Lever / Ashby posting, the full
description is fetched from the company's own careers site, so experience, pay and
location are read from the real posting rather than an aggregator snippet.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from . import http
from .ats import workday
from .models import Job
from .text import html_to_text, parse_salary, posted_date

SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
                ".github/scripts/listings.json")
ZAPPLY_URL = "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md"

LABELS = {"simplify": "Simplify", "zapply": "Zapply", "adzuna": "Adzuna", "jsearch": "JSearch"}

_simplify_cache: list | None = None


def simplify_listings() -> list:
    """The whole Simplify listings file (also used to discover company boards). Cached per run."""
    global _simplify_cache
    if _simplify_cache is None:
        d = http.get_json(SIMPLIFY_URL, timeout=(10, 120))
        _simplify_cache = d if isinstance(d, list) else []
    return _simplify_cache


def _iso(d) -> str:
    return d.isoformat() if d else ""


def _age_check(max_age):
    """max_age may be a number of days or a callable too_old(posted, title)."""
    if callable(max_age):
        return lambda posted, title="": not max_age(posted, title)

    def fresh(posted, title=""):
        d = posted_date(posted)
        return not (max_age and d and (date.today() - d).days > max_age)
    return fresh


# ---------------- GitHub new-grad lists ----------------

def simplify(cfg: dict, title_ok, loc_ok, max_age: int | None) -> list[Job]:
    fresh, out = _age_check(max_age), []
    for x in simplify_listings():
        if not x.get("active") or x.get("is_visible") is False:
            continue
        title, locs = x.get("title", ""), list(x.get("locations") or [])
        posted = ""
        if x.get("date_posted"):
            posted = datetime.fromtimestamp(int(x["date_posted"]), tz=timezone.utc).date().isoformat()
        if not title_ok(title) or not loc_ok(locs) or not fresh(posted, title):
            continue
        out.append(Job(ats="simplify", company=x.get("company_name", ""), job_id=str(x.get("id")),
                       title=title, url=x.get("url", ""), locations=locs, posted=posted,
                       remote=any("remote" in l.lower() for l in locs)))
    return out


_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


def zapply(cfg: dict, title_ok, loc_ok, max_age: int | None) -> list[Job]:
    text = ""
    try:
        r = http.get(ZAPPLY_URL, timeout=(10, 60))
        text = r.text if r.status_code == 200 else ""
    except Exception:
        pass
    fresh, out = _age_check(max_age), []
    for line in text.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        company, title, loc, age = cells[0].strip("* "), cells[1], cells[2], cells[3]
        m = _LINK.search(line)
        if not m:
            continue
        url = m.group(1)
        posted = _iso(posted_date(_age_words(age)))
        if not title_ok(title) or not loc_ok([loc]) or not fresh(posted, title):
            continue
        jid = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or url
        out.append(Job(ats="zapply", company=company, job_id=jid, title=title, url=url,
                       locations=[loc], posted=posted, remote="remote" in loc.lower()))
    return out


def _age_words(age: str) -> str:
    """Zapply's '11h' / '3d' / '2w' -> words posted_date understands."""
    m = re.fullmatch(r"\s*(\d+)\s*([hdwm])\s*", age or "")
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    return "today" if unit == "h" else f"{n} {'days' if unit == 'd' else 'weeks' if unit == 'w' else 'months'}"


# ---------------- Adzuna ----------------

_STATES = {"California": "CA", "New York": "NY", "Massachusetts": "MA", "Oregon": "OR",
           "Illinois": "IL", "Washington": "WA", "New Jersey": "NJ"}


def adzuna(cfg: dict, title_ok, loc_ok, max_age: int | None) -> list[Job]:
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        return []
    out, calls = [], 0
    for where in cfg.get("where", []):
        for what in cfg.get("what", []):
            if calls >= cfg.get("max_calls", 50):
                return out
            calls += 1
            d = http.get_json("https://api.adzuna.com/v1/api/jobs/us/search/1", params={
                "app_id": app_id, "app_key": key, "what": what, "where": where["where"],
                "distance": where.get("km", 40),
                "max_days_old": (max_age if isinstance(max_age, int) else cfg.get("max_days_old", 30)),
                "results_per_page": 50, "sort_by": "date", "content-type": "application/json"})
            for x in (d or {}).get("results") or []:
                title = html_to_text(x.get("title", ""))
                area = (x.get("location") or {}).get("area") or []
                locs = [(x.get("location") or {}).get("display_name", "")]
                if len(area) >= 3:
                    locs.append(f"{area[-1]}, {_STATES.get(area[1], area[1])}")
                locs = [l for l in locs if l]
                if not title_ok(title) or not loc_ok(locs):
                    continue
                lo = hi = None
                if str(x.get("salary_is_predicted", "1")) == "0":
                    lo, hi = x.get("salary_min"), x.get("salary_max")
                tt = "Part time" if x.get("contract_time") == "part_time" else (
                    "Contract" if x.get("contract_type") == "contract" else "")
                out.append(Job(ats="adzuna", company=(x.get("company") or {}).get("display_name", ""),
                               job_id=str(x.get("id")), title=title, url=x.get("redirect_url", ""),
                               locations=locs, description=html_to_text(x.get("description", "")),
                               posted=x.get("created", ""), time_type=tt,
                               salary_min=lo, salary_max=hi))
    return out


# ---------------- JSearch (Google for Jobs: LinkedIn, Indeed, Glassdoor, ...) ----------------

def jsearch(cfg: dict, title_ok, loc_ok, max_age: int | None) -> list[Job]:
    key = os.environ.get("RAPIDAPI_KEY")
    queries = cfg.get("queries", [])
    if not key or not queries:
        return []
    n = max(1, cfg.get("max_calls", 6))
    start = (date.today().toordinal() * n) % len(queries)       # rotate through queries daily
    todays = [queries[(start + i) % len(queries)] for i in range(min(n, len(queries)))]
    out = []
    for q in todays:
        d = http.get_json("https://jsearch.p.rapidapi.com/search", params={
            "query": q, "page": 1, "num_pages": 1, "country": "us",
            "date_posted": cfg.get("date_posted", "3days"), "employment_types": "FULLTIME"},
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
        for x in (d or {}).get("data") or []:
            title = x.get("job_title", "")
            loc = ", ".join(p for p in (x.get("job_city"), x.get("job_state")) if p)
            locs = [loc or x.get("job_location") or ""]
            remote = bool(x.get("job_is_remote"))
            if remote:
                locs.append("Remote, US")
            locs = [l for l in locs if l]
            if not title_ok(title) or not (loc_ok(locs) or remote):
                continue
            lo, hi = x.get("job_min_salary"), x.get("job_max_salary")
            period = (x.get("job_salary_period") or "").upper()
            if lo and hi and period == "HOUR":
                lo, hi = lo * 2080, hi * 2080
            elif lo and hi and period == "MONTH":
                lo, hi = lo * 12, hi * 12
            elif period and period != "YEAR":
                lo = hi = None
            job = Job(ats="jsearch", company=x.get("employer_name", ""), job_id=str(x.get("job_id")),
                      title=title, url=x.get("job_apply_link") or x.get("job_google_link", ""),
                      locations=locs, description=x.get("job_description", ""),
                      posted=x.get("job_posted_at_datetime_utc", ""), remote=remote,
                      time_type=x.get("job_employment_type", ""), salary_min=lo, salary_max=hi)
            if x.get("job_publisher"):
                job.flags.append(f"via:{x['job_publisher']}")
            out.append(job)
    return out


# ---------------- full descriptions from the company's own posting ----------------

_ashby_cache: dict[str, list] = {}


def enrich(job: Job) -> bool:
    """Fill description/locations/date/pay from the real ATS posting when the link
    points at one. Returns True if the posting was found."""
    url = job.url or ""
    host = urlparse(url).netloc.lower()
    try:
        if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
            c = workday.parse_url(url)
            m = re.search(r"(/job/[^?#]+)", url)
            if not c or not m:
                return False
            info = (http.get_json(workday._base(c) + m.group(1)) or {}).get("jobPostingInfo") or {}
            if not info:
                return False
            job.description = html_to_text(info.get("jobDescription", ""))
            locs = [info.get("location")] + list(info.get("additionalLocations") or [])
            job.locations = [l for l in locs if l] or job.locations
            job.posted = info.get("startDate") or job.posted
            job.time_type = info.get("timeType", "") or job.time_type
            return True
        m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url)
        if m:
            d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
            if not d:
                return False
            job.description = html_to_text(d.get("content", ""))
            loc = (d.get("location") or {}).get("name")
            job.locations = [loc] if loc else job.locations
            job.posted = d.get("first_published") or d.get("updated_at") or job.posted
            return True
        m = re.search(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url)
        if m:
            d = http.get_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
            if not d:
                return False
            job.description = (d.get("descriptionPlain", "") + "\n" + "\n".join(
                f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in d.get("lists") or []))
            cat = d.get("categories") or {}
            locs = [l for l in (list(cat.get("allLocations") or []) or [cat.get("location", "")]) if l]
            job.locations = locs or job.locations
            job.posted = str(d.get("createdAt", "")) or job.posted
            job.time_type = cat.get("commitment", "") or job.time_type
            return True
        m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
        if m:
            org, jid = m.group(1), m.group(2)
            if org not in _ashby_cache:
                d = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
                _ashby_cache[org] = (d or {}).get("jobs") or []
            x = next((j for j in _ashby_cache[org] if j.get("id") == jid), None)
            if not x:
                return False
            job.description = x.get("descriptionPlain") or html_to_text(x.get("descriptionHtml", ""))
            job.posted = x.get("publishedAt") or job.posted
            return True
    except Exception:
        return False
    return False


# ---------------- all feeds ----------------

FEEDS = {"simplify": simplify, "zapply": zapply, "adzuna": adzuna, "jsearch": jsearch}


def collect(cfg: dict, title_ok, loc_ok, max_age: int | None, known: set, log: list[str]) -> list[Job]:
    """Every enabled feed's listings that pass the cheap title/location screens.
    New ones get their full posting fetched (up to `enrich_max` per run)."""
    jobs: list[Job] = []
    for name, fn in FEEDS.items():
        fcfg = cfg.get(name) or {}
        if not fcfg.get("enabled", True):
            continue
        try:
            got = fn(fcfg, title_ok, loc_ok, max_age)
        except Exception as e:
            log.append(f"[{LABELS[name]}] ERROR {type(e).__name__}: {e}")
            continue
        if got or name in ("simplify", "zapply") or _has_key(name):
            log.append(f"[{LABELS[name]}] {len(got)} listings in your field and cities")
        jobs.extend(got)
    budget = cfg.get("enrich_max", 150)
    for j in jobs:
        if j.key in known:
            j.flags.append("cached")
            continue
        if budget > 0 and enrich(j):
            budget -= 1
            if j.salary_max is None:
                j.salary_min, j.salary_max = parse_salary(j.description)
    return jobs


def _has_key(name: str) -> bool:
    return bool(os.environ.get({"adzuna": "ADZUNA_APP_KEY", "jsearch": "RAPIDAPI_KEY"}.get(name, "")))


def label(job_or_rec) -> str:
    """'Simplify', 'LinkedIn via JSearch', ..."""
    ats = job_or_rec.ats if isinstance(job_or_rec, Job) else job_or_rec.get("ats", "")
    flags = job_or_rec.flags if isinstance(job_or_rec, Job) else (job_or_rec.get("flags") or [])
    via = next((f[4:] for f in flags if f.startswith("via:")), "")
    base = LABELS.get(ats, ats)
    return f"{via} via {base}" if via and via.lower() != base.lower() else base
