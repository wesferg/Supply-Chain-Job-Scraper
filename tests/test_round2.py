"""Fixes after the first live run: inconclusive checks, junk boards, tighter titles,
non-US locations, senior pay bands, rule changes, Eightfold, company display names."""
import json
import os
from datetime import date, timedelta

import pytest
import yaml

from jobscout import discover, http, pipeline, store, universe
from jobscout.ats import eightfold, simple, workday
from jobscout.filters import Filters
from jobscout.models import Job
from tests.test_adapters_pipeline import fake_net, sandbox  # noqa: F401  (fixture reuse)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = (date.today() - timedelta(days=30)).isoformat()


@pytest.fixture(scope="module")
def f():
    return Filters(yaml.safe_load(open(os.path.join(ROOT, "config", "filters.yaml"))))


# ---------------- titles ----------------

@pytest.mark.parametrize("title,cat", [
    ("Operations Research Engineer", "core"),            # "reSEARCH ENGINEer" isn't SEO
    ("Supply Chain Network Optimization Analyst", "core"),
    ("Discrete Event Simulation Engineer", "core"),
    ("Simulation Analyst", "core"),
    ("AI Infrastructure Operations, Demand Planning", "core"),
    ("Planner, Merchandise Planning", "adjacent"),
    ("Manufacturing Operations Analyst", "adjacent"),
    ("Business Operations Analyst", "general"),
    ("Operations Associate", "general"),
    ("Search Engine Optimization (SEO) Specialist", None),
    ("Quantum Compute Simulation Scientist", None),
    ("Researcher, Population Simulation", None),
    ("Simulation Architect, NYC", None),
    ("Portfolio analytics and forecasts actuary", None),
    ("Lab Operations Specialist", None),
    ("Fund Operations Associate", None),
    ("UAS Operations Specialist - Pilot", None),
    ("Robot Operations Specialist", None),
    ("Operations Associate - Join Our Talent Community", None),
    ("Logistics Specialist (SkillBridge)", None),
    ("Store Planner", None),
    ("Operations Associate, Trading and Margin", None),
])
def test_titles_round2(f, title, cat):
    assert f.title_category(title) == cat


def test_general_roles_rank_below_supply_chain_roles(f):
    from jobscout import scoring
    def score(title):
        j = Job(ats="x", company="c", job_id="1", title=title, url="u", locations=["San Francisco, CA"])
        assert f.evaluate(j).keep
        return scoring.rule_score(j, 65000, 85000)
    assert score("Demand Planner") > score("Logistics Analyst") > score("Business Operations Analyst")


# ---------------- locations and pay ----------------

def test_foreign_locations_rejected(f):
    assert f.match_location("San Francisco,CRI") is None          # Costa Rica
    assert f.match_location("San Jose, Costa Rica") is None
    assert f.match_location("Portland, Ontario") is None
    assert f.match_location("San Francisco, CA") == "SF Bay Area"
    assert f.match_location("New York, NYC") == "New York City"   # NYC isn't a country code
    assert f.match_location("Oak Brook, IL") == "Chicago"
    assert f.match_location("Framingham, MA") == "Boston"


def test_senior_pay_band(f):
    j = Job(ats="x", company="c", job_id="1", title="Demand Planning", url="u",
            locations=["San Francisco, CA"], salary_min=320000, salary_max=405000)
    d = f.evaluate(j)
    assert not d.keep and "senior pay band" in d.reason
    j = Job(ats="x", company="c", job_id="2", title="Demand Planner", url="u",
            locations=["San Francisco, CA"], salary_min=160000, salary_max=200000)
    assert f.evaluate(j).keep and "senior_pay" in j.flags
    j = Job(ats="x", company="c", job_id="3", title="Demand Planner", url="u",
            locations=["San Francisco, CA"], salary_min=80000, salary_max=110000)
    assert f.evaluate(j).keep and "senior_pay" not in j.flags


# ---------------- discovery ----------------

def test_slug_candidates_skip_lone_first_word():
    assert "new" not in discover.slug_candidates("New Balance")
    assert "ralph" not in discover.slug_candidates("Ralph Lauren")
    assert discover.slug_candidates("Oura") == ["oura"]


@pytest.mark.parametrize("ats,ids,ok", [
    ("greenhouse", {"token": "1800contacts"}, True),
    ("greenhouse", {"token": "12twenty"}, True),
    ("greenhouse", {"token": "103644278"}, False),
    ("greenhouse", {"token": "2022b-28"}, False),
    ("greenhouse", {"token": "acceptable-use-policy"}, False),
    ("ashby", {"org": "american%20terawatt"}, True),
    ("ashby", {"org": "abridge%5c"}, False),
    ("ashby", {"org": ".sitemap.xml"}, False),
    ("ashby", {"org": "51f67855-3ba7-445a-99bb-97e9f5093e4b"}, False),
    ("workday", {"host": "aep.wd1.myworkdayjobs.com", "tenant": "aep", "site": "favicon"}, False),
    ("workday", {"host": "kla.wd1.myworkdayjobs.com", "tenant": "kla", "site": "Search"}, True),
])
def test_plausible(ats, ids, ok):
    assert discover.plausible(ats, ids) is ok


def test_workday_junk_sites_not_parsed():
    assert workday.parse_url("https://aep.wd1.myworkdayjobs.com/favicon.ico") is None
    assert workday.parse_url("https://aep.wd1.myworkdayjobs.com/cx-jobs-ie/x") is None
    assert workday.parse_url("https://tapestry.wd108.myworkdayjobs.com/en-US/Tapestry_Careers")["site"] \
        == "Tapestry_Careers"


def test_checks_are_three_way(monkeypatch):
    answers = {}
    monkeypatch.setattr(http, "status_json", lambda url, payload=None, **kw: answers[url.rsplit("/", 1)[-1]])
    answers["gone"], answers["busy"], answers["real"] = (404, None), (429, None), (200, {"name": "Real Co"})
    assert simple.greenhouse_info("gone") == (False, "")
    assert simple.greenhouse_info("busy") == (None, "")
    assert simple.greenhouse_info("real") == (True, "Real Co")


def test_rate_limited_lookup_retries_tomorrow(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: None)     # every check: no answer
    monkeypatch.setattr(discover, "_probe_workday", lambda s, unsure: unsure.append(s))
    state, log = store.State(), []
    entry = {"name": "Warby Parker", "ats": "greenhouse", "token": "warbyparker"}
    assert pipeline.company_ids(entry, state, log) is None
    assert state.resolved["Warby Parker"]["unsure"] and "retry tomorrow" in log[0]
    # same day: not retried
    calls = []
    monkeypatch.setattr(discover, "resolve", lambda e: calls.append(1) or ("greenhouse", {"token": "warbyparker"}))
    assert pipeline.company_ids(entry, state, log) is None and calls == []
    # next day: retried
    state.resolved["Warby Parker"]["failed_on"] = (date.today() - timedelta(days=1)).isoformat()
    assert pipeline.company_ids(entry, state, log) == ("greenhouse", {"token": "warbyparker"})


def test_editing_hints_redoes_the_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    state, log = store.State(), []
    monkeypatch.setattr(discover, "resolve", lambda e: ("smartrecruiters", {"company_id": "gapinc"}))
    assert pipeline.company_ids({"name": "Gap Inc"}, state, log)[0] == "smartrecruiters"
    fixed = {"name": "Gap Inc", "url": "https://gapinc.wd1.myworkdayjobs.com/GAPINC"}
    monkeypatch.setattr(discover, "resolve", lambda e: ("workday", workday.parse_url(e["url"])))
    assert pipeline.company_ids(fixed, state, log)[0] == "workday"
    assert pipeline.company_ids(fixed, state, log)[1]["tenant"] == "gapinc"


def test_grow_rechecks_instead_of_burying(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "universe.yaml")))
    monkeypatch.setattr(universe, "_commoncrawl", lambda *a: [])
    monkeypatch.setattr(universe, "_wayback", lambda *a: ["https://jobs.lever.co/busyco/x",
                                                          "https://jobs.lever.co/goneco/y"])
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: None if ids["slug"] == "busyco" else False)
    universe.grow(cfg, set())
    u = universe.load()
    assert u["meta"]["recheck"] == ["lever:busyco"] and "lever:goneco" in u["meta"]["dead"]
    # next pass: the busy board is re-checked even though no source mentions it again
    monkeypatch.setattr(universe, "_wayback", lambda *a: [])
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: True)
    universe.grow(cfg, set())
    assert "lever:busyco" in universe.load()["boards"]


# ---------------- rules change ----------------

def test_rule_change_rechecks_old_matches_but_keeps_first_seen(sandbox):
    pipeline.run()
    m = json.load(open(sandbox / "data" / "matches.json"))
    for v in m.values():
        v["first_seen"] = OLD
    json.dump(m, open(sandbox / "data" / "matches.json", "w"))
    # tighten the rules: logistics analysts are out now
    cfg = sandbox / "config" / "filters.yaml"
    cfg.write_text(cfg.read_text().replace("  exclude:\n", "  exclude:\n    - logistics analyst\n", 1))
    res = pipeline.run()
    m = json.load(open(sandbox / "data" / "matches.json"))
    assert not any(v["title"] == "Logistics Analyst" for v in m.values())
    assert res["new"] == 0          # the other two were re-checked, not announced as new
    # loosen again: it comes back with its original first-seen date
    cfg.write_text(cfg.read_text().replace("    - logistics analyst\n", "", 1))
    pipeline.run()
    m = json.load(open(sandbox / "data" / "matches.json"))
    assert next(v for v in m.values() if v["title"] == "Logistics Analyst")["first_seen"] == OLD


# ---------------- Eightfold ----------------

EF_LIST = {"positions": [
    {"id": 111, "name": "Supply Chain Analyst", "locations": ["Fremont, CA, United States"],
     "t_create": 1762000000, "canonicalPositionUrl": "https://careers.lamresearch.com/careers/job/111"},
    {"id": 222, "name": "Field Service Engineer 3", "locations": ["Fremont, CA"]},
], "count": 2}
EF_DETAIL = {"job_description": "<p>Plan supply. 1-3 years of experience. $85,000 - $110,000</p>"}


def test_eightfold_fetch(monkeypatch):
    def get_json(url, params=None, **kw):
        if url.endswith("/api/apply/v2/jobs"):
            return EF_LIST if params["start"] == 0 else {"positions": []}
        if url.endswith("/api/apply/v2/jobs/111"):
            return EF_DETAIL
        return None
    monkeypatch.setattr(http, "get_json", get_json)
    c = {"host": "careers.lamresearch.com", "domain": "lamresearch.com"}
    jobs = eightfold.fetch("Lam Research", c, ["supply chain"], lambda t: "Engineer" not in t)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Supply Chain Analyst" and j.locations == ["Fremont, CA, United States"]
    assert "Plan supply" in j.description and j.url.endswith("/careers/job/111") and j.posted


def test_eightfold_board_gone(monkeypatch):
    from jobscout.models import BoardUnavailable
    monkeypatch.setattr(http, "get_json", lambda *a, **kw: None)
    with pytest.raises(BoardUnavailable):
        eightfold.fetch("Lam", {"host": "h", "domain": "d"}, ["x"], lambda t: True)


# ---------------- display names ----------------

def test_display_name_used_in_list(sandbox):
    os.makedirs(sandbox / "data", exist_ok=True)
    universe.save({"meta": {"last_grow": date.today().isoformat()}, "boards": {
        "greenhouse:acme": {"ats": "greenhouse", "ids": {"token": "acme"}, "name": "Acme",
                            "added": "x", "last_scanned": "", "last_hit": "", "hits": 1, "fails": 0}}})
    cfg = sandbox / "config"
    (cfg / "universe.yaml").write_text(open(os.path.join(ROOT, "config", "universe.yaml")).read())
    (cfg / "companies.yaml").write_text("companies:\n  - {name: Acme Lever, ats: lever, slug: acme}\n")
    pipeline.run()
    md = (sandbox / "JOBS.md").read_text()
    assert "Acme Brands \\*" in md    # name from the Greenhouse board, not the slug


# ---------------- experience depth and posting age ----------------

@pytest.mark.parametrize("text,req,pref", [
    ("2+ years of experience with SQL. 6+ years of experience in demand planning.", 6, None),
    ("Bachelor's degree and 6+ years of experience, or Master's degree and 4+ years of experience.", 6, None),
    ("Typically requires a minimum of 6 years of related experience with a Bachelor's degree; "
     "or 4 years and a Master's degree; or a PhD with 3 years experience.", 6, None),
    ("Minimum of six (6) years of relevant work experience in planning.", 6, None),
    ("Requirements\n3+ years of experience in planning\nPreferred Qualifications\n"
     "7+ years of experience in semiconductors", 3, 7),
    ("5+ years of experience preferred. 2 years of experience required.", 2, 5),
    ("Our company has over 100 years of history. 0-2 years of experience in logistics.", 0, None),
    ("This is a 2-year rotational program for new grads.", None, None),
    ("Must be 18 years of age or older. 2+ years experience in inventory.", 2, None),
    ("Experience with Excel is a plus. 2+ years in supply chain analytics.", 2, None),
    ("BS plus 5 years of experience, MS plus 3 years of experience, or PhD plus 1 year.", 5, None),
    ("The base pay range is $80,000 - $105,000 per year. 1-3 years of experience in planning.", 1, None),
    ("8+ years of experience in supply chain, with at least 3 years in a leadership role.", 8, None),
])
def test_years_detail(text, req, pref):
    from jobscout.text import years_detail
    assert years_detail(text) == (req, pref)


def test_posted_date_formats():
    from jobscout.text import posted_date
    t = date(2026, 11, 12)
    assert posted_date("2026-11-09", t) == date(2026, 11, 9)
    assert posted_date("2026-10-20T18:01:02-04:00", t) == date(2026, 10, 20)
    assert posted_date("1762905600000", t) == date(2025, 11, 12)          # Lever epoch ms
    assert posted_date("Posted Today", t) == t
    assert posted_date("Posted Yesterday", t) == date(2026, 11, 11)
    assert posted_date("Posted 3 Days Ago", t) == date(2026, 11, 9)
    assert posted_date("Posted 30+ Days Ago", t) == date(2026, 10, 13)
    assert posted_date("", t) is None and posted_date("soon", t) is None


def test_old_postings_rejected_and_aging_ones_drop_off(f, sandbox):
    old = (date.today() - timedelta(days=21)).isoformat()
    j = Job(ats="x", company="c", job_id="1", title="Demand Planner", url="u",
            locations=["San Francisco, CA"], posted=old)
    d = f.evaluate(j)
    assert not d.keep and d.reason == "age: posted 21 days ago"
    j = Job(ats="x", company="c", job_id="2", title="Demand Planner", url="u",
            locations=["San Francisco, CA"], posted="Posted 20 Days Ago")
    assert f.evaluate(j).keep and j.posted == (date.today() - timedelta(days=20)).isoformat()
    j = Job(ats="x", company="c", job_id="3", title="Demand Planner", url="u",
            locations=["San Francisco, CA"])               # no date: kept
    assert f.evaluate(j).keep

    pipeline.run()
    m = json.load(open(sandbox / "data" / "matches.json"))
    k = next(k for k, v in m.items() if v["title"] == "Logistics Analyst")
    m[k]["posted"] = old                                    # it aged past 20 days since
    json.dump(m, open(sandbox / "data" / "matches.json", "w"))
    pipeline.run()
    md = (sandbox / "JOBS.md").read_text()
    assert "Logistics Analyst" not in md and "Planner, Demand Agg" in md
    assert "| Posted |" in md and "3d ago" in md
