"""Aggregator feeds: Simplify, Zapply, Adzuna, JSearch, enrichment, cross-source dedupe."""
import json
import os
import time
from datetime import date, timedelta

import pytest
import yaml

from jobscout import feeds, http, pipeline, store, universe
from jobscout.filters import Filters
from jobscout.models import Job
from tests.test_adapters_pipeline import fake_net, sandbox, days_ago  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def f():
    return Filters(yaml.safe_load(open(os.path.join(ROOT, "config", "filters.yaml"))))


def ts(days):
    return int(time.time() - days * 86400)


SIMPLIFY = [
    {"company_name": "Analog Devices", "id": "s1", "title": "Associate Supply Chain Analyst",
     "active": True, "is_visible": True, "date_posted": ts(2), "locations": ["San Jose, CA"],
     "url": "https://analogdevices.wd1.myworkdayjobs.com/External/job/San-Jose/Associate-Supply-Chain-Analyst_R1"},
    {"company_name": "TikTok", "id": "s2", "title": "Logistics Product Strategy & Operations Graduate",
     "active": True, "date_posted": ts(1), "locations": ["Seattle, WA"], "url": "https://lifeattiktok.com/x"},
    {"company_name": "Old Co", "id": "s3", "title": "Demand Planner", "active": True,
     "date_posted": ts(60), "locations": ["Boston, MA"], "url": "https://x.com/3"},
    {"company_name": "Gone Co", "id": "s4", "title": "Demand Planner", "active": False,
     "date_posted": ts(1), "locations": ["Boston, MA"], "url": "https://x.com/4"},
    {"company_name": "Applied Materials", "id": "s6", "title": "Logistics Automation New Grad",
     "active": True, "date_posted": ts(60), "locations": ["Santa Clara, CA"], "url": "https://x.com/6"},
    {"company_name": "Acme", "id": "s5", "title": "Software Engineer", "active": True,
     "date_posted": ts(1), "locations": ["San Francisco, CA"], "url": "https://jobs.lever.co/acme/x"},
]

ZAPPLY_MD = """# Zapply
| Company | Role | Location | Posted | Visa | **Apply** |
|---------|------|----------|--------|------|----------|
| **Northrop Grumman** | 2027 Associate Supply Chain Planning Specialist | Sunnyvale, CA | 11h | ✅ Sponsor | [<img src="images/apply.png" width="80" alt="Apply">](https://zapply.jobs/l/d/workday-ngc-R1?s=gh) |
| **Ecolab** | Logistics Associate | McDonough, Georgia | 13h |  | [<img src="images/apply.png">](https://zapply.jobs/l/d/workday-ecolab-R2?s=gh) |
| **Boeing** | Software Engineer | Seattle, WA | 1h |  | [<img src="images/apply.png">](https://zapply.jobs/l/d/workday-boeing-R3?s=gh) |
| **Stale Inc** | Demand Planner | Boston, MA | 5w |  | [<img src="images/apply.png">](https://zapply.jobs/l/d/x-R4?s=gh) |
"""


def test_simplify_feed(monkeypatch, f):
    monkeypatch.setattr(feeds, "simplify_listings", lambda: SIMPLIFY)
    jobs = feeds.simplify({}, f.title_ok, f.loc_maybe, f.too_old)
    # not Seattle, stale, inactive or SWE; the 60-day-old new-grad program is exempt
    assert [j.company for j in jobs] == ["Analog Devices", "Applied Materials"]
    assert jobs[0].posted == (date.today() - timedelta(days=2)).isoformat()


def test_zapply_feed(monkeypatch, f):
    class R:
        status_code, text = 200, ZAPPLY_MD
    monkeypatch.setattr(http, "get", lambda *a, **kw: R())
    jobs = feeds.zapply({}, f.title_ok, f.loc_maybe, 30)
    assert [(j.company, j.locations) for j in jobs] == [("Northrop Grumman", ["Sunnyvale, CA"])]
    assert jobs[0].url.startswith("https://zapply.jobs/l/d/workday-ngc-R1") and jobs[0].posted == date.today().isoformat()


def test_adzuna_needs_key_and_parses(monkeypatch, f):
    assert feeds.adzuna({"what": ["x"], "where": [{"where": "Boston"}]}, f.title_ok, f.loc_maybe, 30) == []
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    calls = []

    def get_json(url, params=None, **kw):
        calls.append(params)
        return {"results": [
            {"id": 9, "title": "<strong>Demand</strong> Planner", "company": {"display_name": "Bose"},
             "location": {"display_name": "Framingham, Massachusetts",
                          "area": ["US", "Massachusetts", "Middlesex County", "Framingham"]},
             "description": "2+ years of experience in planning...", "created": days_ago(1) + "T10:00:00Z",
             "redirect_url": "https://www.adzuna.com/details/9", "salary_min": 70000, "salary_max": 90000,
             "salary_is_predicted": "0", "contract_time": "full_time"},
            {"id": 10, "title": "Demand Planner", "company": {"display_name": "X"},
             "location": {"display_name": "Austin, Texas", "area": ["US", "Texas", "Travis", "Austin"]},
             "redirect_url": "u", "salary_is_predicted": "1", "salary_min": 1, "salary_max": 2}]}
    monkeypatch.setattr(http, "get_json", get_json)
    cfg = {"max_calls": 3, "what": ["demand planner", "supply planner"],
           "where": [{"where": "Boston", "km": 40}, {"where": "Chicago"}]}
    jobs = feeds.adzuna(cfg, f.title_ok, f.loc_maybe, 30)
    assert len(calls) == 3 and calls[0]["what"] == "demand planner" and calls[0]["max_days_old"] == 30
    j = jobs[0]
    assert j.title == "Demand Planner" and "Framingham, MA" in j.locations and j.salary_max == 90000
    assert all(x.company == "Bose" for x in jobs)                # Austin dropped


def test_jsearch_rotates_and_labels(monkeypatch, f):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    seen = []

    def get_json(url, params=None, headers=None, **kw):
        seen.append(params["query"])
        assert headers["X-RapidAPI-Host"] == "jsearch.p.rapidapi.com"
        return {"data": [{"job_id": "j1", "job_title": "Supply Chain Analyst", "employer_name": "Clorox",
                          "job_city": "Oakland", "job_state": "CA", "job_apply_link": "https://a/1",
                          "job_description": "0-2 years of experience", "job_publisher": "LinkedIn",
                          "job_posted_at_datetime_utc": days_ago(1) + "T00:00:00.000Z",
                          "job_min_salary": 35, "job_max_salary": 45, "job_salary_period": "HOUR",
                          "job_employment_type": "Full-time"}]}
    monkeypatch.setattr(http, "get_json", get_json)
    cfg = {"max_calls": 2, "queries": ["a", "b", "c", "d", "e"]}
    jobs = feeds.jsearch(cfg, f.title_ok, f.loc_maybe, 30)
    assert len(seen) == 2 and len(set(seen)) == 2
    j = jobs[0]
    assert j.salary_min == 35 * 2080 and feeds.label(j) == "LinkedIn via JSearch"


def test_enrich_from_company_posting(monkeypatch):
    def get_json(url, **kw):
        if url.endswith("/wday/cxs/analogdevices/External/job/San-Jose/Associate-Supply-Chain-Analyst_R1"):
            return {"jobPostingInfo": {"jobDescription": "<p>6+ years of experience in planning.</p>",
                                       "location": "San Jose, CA", "startDate": days_ago(3), "timeType": "Full time"}}
        if url.endswith("/v1/boards/acme/jobs/123"):
            return {"content": "&lt;p&gt;1+ years of experience&lt;/p&gt;", "location": {"name": "Boston, MA"},
                    "first_published": days_ago(4)}
        return None
    monkeypatch.setattr(http, "get_json", get_json)
    j = Job(ats="simplify", company="Analog Devices", job_id="s1", title="Associate Supply Chain Analyst",
            url=SIMPLIFY[0]["url"], locations=["San Jose, CA"])
    assert feeds.enrich(j) and "6+ years" in j.description and j.posted == days_ago(3)
    g = Job(ats="zapply", company="Acme", job_id="z", title="Analyst",
            url="https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x", locations=[])
    assert feeds.enrich(g) and g.locations == ["Boston, MA"] and "1+ years" in g.description
    assert not feeds.enrich(Job(ats="zapply", company="x", job_id="1", title="t", url="https://zapply.jobs/l/d/x"))


def test_fingerprints_match_across_sources():
    a = {"url": "https://analogdevices.wd1.myworkdayjobs.com/en-US/External/job/X_R1?src=li",
         "company": "Analogdevices", "title": "Analyst, Supply Chain", "metro": "SF Bay Area"}
    b = {"url": "https://analogdevices.wd1.myworkdayjobs.com/External/job/X_R1",
         "company": "Analog Devices, Inc.", "title": "Analyst - Supply Chain", "metro": "SF Bay Area"}
    assert store.fingerprints(a) & store.fingerprints(b)


def test_feeds_merge_into_one_list_without_duplicates(sandbox, monkeypatch):
    (sandbox / "config" / "feeds.yaml").write_text("simplify: {enabled: true}\nzapply: {enabled: false}\n")
    monkeypatch.setattr(feeds, "simplify_listings", lambda: [
        # the same Gap posting the board sweep finds -> duplicate, dropped
        {"company_name": "Gap Inc", "id": "dup", "title": "Planner, Demand Agg", "active": True,
         "date_posted": ts(1), "locations": ["San Francisco, CA"],
         "url": "https://gap.wd1.myworkdayjobs.com/GAPINC/job/San-Francisco-CA/Planner--Demand-Agg_R210823"},
        {"company_name": "Clorox", "id": "new1", "title": "Demand Planning Analyst", "active": True,
         "date_posted": ts(1), "locations": ["Oakland, CA"], "url": "https://clorox.example/jobs/1"},
    ])
    res = pipeline.run()
    md = (sandbox / "JOBS.md").read_text()
    assert md.count("Planner, Demand Agg") == 1
    assert "Clorox <sub>via Simplify</sub>" in md and res["new"] == 4
    assert "[Simplify] 2 listings in your field and cities" in md
    rej = json.load(open(sandbox / "data" / "rejected.json"))
    assert any(v["reason"] == "duplicate" for v in rej.values())


def test_daily_simplify_topup_keeps_weekly_schedule(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "universe.yaml")))
    monkeypatch.setattr(universe, "_commoncrawl", lambda *a: pytest.fail("weekly source ran"))
    monkeypatch.setattr(universe, "_wayback", lambda *a: pytest.fail("weekly source ran"))
    monkeypatch.setattr(feeds, "simplify_listings", lambda: [{"url": "https://jobs.lever.co/newgradco/abc"}])
    from jobscout import discover
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: True)
    u = universe.load()
    u["meta"]["last_grow"] = "2026-11-10"
    universe.save(u)
    g = universe.grow(cfg, set(), only_sources=["simplify"])
    u = universe.load()
    assert g["added"] == 1 and "lever:newgradco" in u["boards"] and u["meta"]["last_grow"] == "2026-11-10"


def test_throttled_workday_board_gets_no_strike(sandbox, monkeypatch):
    from jobscout import universe as uni
    os.makedirs(sandbox / "data", exist_ok=True)
    key = "workday:busy.wd1.myworkdayjobs.com/careers"
    uni.save({"meta": {"last_grow": date.today().isoformat()}, "boards": {key: {
        "ats": "workday", "ids": {"host": "busy.wd1.myworkdayjobs.com", "tenant": "busy", "site": "careers"},
        "name": "Busy", "added": "x", "last_scanned": "", "last_hit": "", "hits": 0, "fails": 2}}})
    (sandbox / "config" / "universe.yaml").write_text(open(os.path.join(ROOT, "config", "universe.yaml")).read())
    real = http.status_json

    def status_json(url, payload=None, **kw):
        if "busy.wd1" in url:
            return 429, None
        return real(url, payload, **kw)
    monkeypatch.setattr(http, "status_json", status_json)
    pipeline.run()
    b = uni.load()["boards"][key]
    assert b["fails"] == 2                                  # unchanged, not dropped
    assert "1 busy (retried next run)" in (sandbox / "JOBS.md").read_text()
