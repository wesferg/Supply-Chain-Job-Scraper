"""ATSs with documented public job-board APIs: Greenhouse, Lever, Ashby, SmartRecruiters.

Every fetcher takes:
  title_ok(title) -> bool          cheap title screen
  loc_ok(list_of_locations) -> bool  cheap location screen (lenient when unsure)
  known: set of job keys already decided on earlier runs (skip their detail calls)
and raises BoardUnavailable if the board itself doesn't respond like a job board.
"""
from __future__ import annotations

from typing import Callable

from .. import http
from ..models import Job, BoardUnavailable
from ..text import html_to_text

TitleOk = Callable[[str], bool]
LocOk = Callable[[list], bool]
_yes = lambda *_: True


def _cached(ats, name, jid, title, url, locs) -> Job:
    return Job(ats=ats, company=name, job_id=jid, title=title, url=url, locations=locs, flags=["cached"])


# ---------------- Greenhouse ----------------
# List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
# Detail: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}


def greenhouse_validate(token: str) -> bool:
    d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return isinstance(d, dict) and "jobs" in d


def greenhouse(name: str, c: dict, title_ok: TitleOk, loc_ok: LocOk = _yes,
               known: set | None = None) -> list[Job]:
    known = known or set()
    token = c["token"]
    base = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    d = http.get_json(base)
    if not isinstance(d, dict) or "jobs" not in d:
        raise BoardUnavailable(f"greenhouse:{token}")
    out = []
    for j in d["jobs"]:
        title = j.get("title", "")
        locs = [(j.get("location") or {}).get("name", "")]
        locs = [l for l in locs if l]
        if not title_ok(title) or not loc_ok(locs):
            continue
        jid = str(j["id"])
        if f"greenhouse:{name}:{jid}" in known:
            out.append(_cached("greenhouse", name, jid, title, j.get("absolute_url", ""), locs))
            continue
        det = http.get_json(f"{base}/{jid}") or j
        offices = [o.get("location") or o.get("name", "") for o in det.get("offices") or []]
        locs = [l for l in dict.fromkeys(locs + offices) if l]
        out.append(Job(
            ats="greenhouse", company=name, job_id=jid, title=title,
            url=j.get("absolute_url", ""), locations=locs,
            description=html_to_text(det.get("content", "")),
            posted=j.get("first_published") or j.get("updated_at", ""),
            remote="remote" in " ".join(locs).lower(),
        ))
    return out


# ---------------- Lever ----------------
# GET https://api.lever.co/v0/postings/{company}?mode=json   (descriptions included)


def lever_validate(slug: str) -> bool:
    d = http.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
    return isinstance(d, list)


def lever(name: str, c: dict, title_ok: TitleOk, loc_ok: LocOk = _yes,
          known: set | None = None) -> list[Job]:
    d = http.get_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    if not isinstance(d, list):
        raise BoardUnavailable(f"lever:{c['slug']}")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        locs = [l for l in (list(cat.get("allLocations") or []) or [cat.get("location", "")]) if l]
        if not title_ok(j.get("text", "")) or not loc_ok(locs):
            continue
        sal = j.get("salaryRange") or {}
        lo, hi = sal.get("min"), sal.get("max")
        if lo and hi and (sal.get("interval") or "").startswith("per-hour"):
            lo, hi = lo * 2080, hi * 2080
        desc = j.get("descriptionPlain", "") + "\n" + "\n".join(
            f"{l.get('text','')}\n{html_to_text(l.get('content',''))}" for l in j.get("lists") or []
        ) + "\n" + (j.get("additionalPlain") or "")
        out.append(Job(
            ats="lever", company=name, job_id=j["id"], title=j.get("text", ""),
            url=j.get("hostedUrl", ""), locations=locs,
            description=desc.strip(), posted=str(j.get("createdAt", "")),
            time_type=cat.get("commitment", ""),
            salary_min=lo, salary_max=hi,
            remote=(j.get("workplaceType") == "remote"),
        ))
    return out


# ---------------- Ashby ----------------
# GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true


def ashby_validate(org: str) -> bool:
    d = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    return isinstance(d, dict) and "jobs" in d


def ashby(name: str, c: dict, title_ok: TitleOk, loc_ok: LocOk = _yes,
          known: set | None = None) -> list[Job]:
    d = http.get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{c['org']}?includeCompensation=true")
    if not isinstance(d, dict) or "jobs" not in d:
        raise BoardUnavailable(f"ashby:{c['org']}")
    out = []
    for j in d["jobs"]:
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        locs = [l for l in locs if l]
        if j.get("isListed") is False or not title_ok(j.get("title", "")):
            continue
        if not (j.get("isRemote") or loc_ok(locs)):
            continue
        comp = j.get("compensation") or {}
        lo = hi = None
        for t in comp.get("summaryComponents") or []:
            if t.get("compensationType") == "Salary":
                lo, hi = t.get("minValue"), t.get("maxValue")
                if (t.get("interval") or "").upper().endswith("HOUR") and lo and hi:
                    lo, hi = lo * 2080, hi * 2080
        out.append(Job(
            ats="ashby", company=name, job_id=j.get("id") or j.get("jobUrl", ""),
            title=j.get("title", ""), url=j.get("jobUrl", ""), locations=locs,
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted=j.get("publishedAt", ""), time_type=j.get("employmentType", ""),
            salary_min=lo, salary_max=hi, remote=bool(j.get("isRemote")),
        ))
    return out


# ---------------- SmartRecruiters ----------------
# GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N


def smartrecruiters_validate(cid: str) -> bool:
    d = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=1")
    return isinstance(d, dict) and (d.get("totalFound") or 0) > 0


def smartrecruiters(name: str, c: dict, title_ok: TitleOk, loc_ok: LocOk = _yes,
                    known: set | None = None) -> list[Job]:
    known = known or set()
    cid = c["company_id"]
    out, offset = [], 0
    while offset < 2000:
        d = http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100&offset={offset}")
        if not isinstance(d, dict):
            if offset == 0:
                raise BoardUnavailable(f"smartrecruiters:{cid}")
            break
        items = d.get("content") or []
        for p in items:
            loc = p.get("location") or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            locs = [loc_s + (" (Remote)" if loc.get("remote") else "")] if loc_s else []
            if not title_ok(p.get("name", "")) or not loc_ok(locs):
                continue
            jid = str(p.get("id"))
            url = f"https://jobs.smartrecruiters.com/{cid}/{jid}"
            if f"smartrecruiters:{name}:{jid}" in known:
                out.append(_cached("smartrecruiters", name, jid, p.get("name", ""), url, locs))
                continue
            det = http.get_json(p.get("ref", "")) or {}
            secs = ((det.get("jobAd") or {}).get("sections") or {})
            desc = "\n".join(html_to_text((secs.get(k) or {}).get("text", ""))
                             for k in ("jobDescription", "qualifications", "additionalInformation"))
            out.append(Job(
                ats="smartrecruiters", company=name, job_id=jid, title=p.get("name", ""),
                url=url, locations=locs, description=desc, posted=p.get("releasedDate", ""),
                time_type=((p.get("typeOfEmployment") or {}).get("label") or ""),
                remote=bool(loc.get("remote")),
            ))
        offset += 100
        if len(items) < 100:
            break
    return out
