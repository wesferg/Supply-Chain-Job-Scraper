"""Discovery at scale: URL harvesting, the daily rotation, and sweeping discovered boards."""
import json
import os
from datetime import date, timedelta

import pytest

from jobscout import universe, discover, pipeline, store, http
from jobscout.ats import workday, board_key
from tests.test_adapters_pipeline import fake_net, GH, LEVER  # noqa: F401  (fixture reuse)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CRAWLED = [
    "https://acme.wd1.myworkdayjobs.com/en-US/External/job/San-Francisco-CA/Demand-Planner_R123",
    "https://acme.wd1.myworkdayjobs.com/External/job/Boston/Analyst_R9",      # same board
    "https://acme.wd1.myworkdayjobs.com/en-US",                                # junk, no site
    "https://boards.greenhouse.io/tinybrand/jobs/555",
    "https://job-boards.greenhouse.io/tinybrand/jobs/556",                     # same board
    "https://jobs.lever.co/coldchainco/0b7c-uuid",
    "https://jobs.ashbyhq.com/nichefoods/abc-uuid",
    "https://jobs.smartrecruiters.com/BoxMakers/7438",
    "https://wd5.myworkdaysite.com/recruiting/bigco/BigCoCareers/job/x",
    "https://example.com/careers",                                             # not an ATS
]


def test_boards_from_urls():
    found = universe.boards_from_urls(CRAWLED)
    assert set(found) == {
        "workday:acme.wd1.myworkdayjobs.com/external",
        "greenhouse:tinybrand", "lever:coldchainco", "ashby:nichefoods",
        "smartrecruiters:boxmakers", "workday:wd5.myworkdaysite.com/bigcocareers",
    }


def test_pretty_name():
    assert universe.pretty_name("lever", {"slug": "cold-chain-co"}) == "Cold chain co"
    assert universe.pretty_name("smartrecruiters", {"company_id": "BoxMakers"}) == "BoxMakers"


def _board(ats="greenhouse", scanned="", hit=""):
    return {"ats": ats, "ids": {}, "name": "x", "last_scanned": scanned, "last_hit": hit}


def test_todays_slice_rotation_and_promotion():
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    u = {"boards": {
        "a": _board(scanned=d(1)),                      # scanned yesterday
        "b": _board(scanned=""),                        # never scanned -> first
        "c": _board(scanned=d(5)),                      # oldest scan -> next
        "w": _board(ats="workday", scanned=d(3)),       # costs 3
        "hot": _board(scanned=d(0), hit=d(10)),         # recent hit -> always included
        "cur": _board(scanned=""),                      # duplicate of a curated board -> skipped
    }}
    picked = universe.todays_slice(u, budget=3, promote_days=45, skip_keys={"cur"})
    assert picked == ["hot", "b", "c"]
    picked = universe.todays_slice(u, budget=10, promote_days=45, skip_keys={"cur"})
    assert picked == ["hot", "b", "c", "w", "a"]


@pytest.fixture
def sandbox(tmp_path, monkeypatch, fake_net):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for n in ("filters.yaml", "profile.yaml", "universe.yaml"):
        (cfg / n).write_text(open(os.path.join(ROOT, "config", n)).read())
    (cfg / "companies.yaml").write_text("companies:\n  - {name: Acme GH, ats: greenhouse, token: acme}\n")
    monkeypatch.setattr(pipeline, "CONFIG", str(cfg))
    monkeypatch.setattr(store, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(store, "RESULTS", str(tmp_path / "results"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: ats != "smartrecruiters")
    monkeypatch.setattr(workday, "validate", lambda ids: True)
    return tmp_path


def test_grow(sandbox, monkeypatch):
    monkeypatch.setattr(universe, "_commoncrawl", lambda *a: CRAWLED[:5])
    monkeypatch.setattr(universe, "_wayback", lambda *a: CRAWLED[5:])
    res = universe.grow(pipeline.load_yaml("universe.yaml"), curated_keys={"greenhouse:tinybrand"})
    u = universe.load()
    # tinybrand is already curated, smartrecruiters failed validation
    assert set(u["boards"]) == {"workday:acme.wd1.myworkdayjobs.com/external", "lever:coldchainco",
                                "ashby:nichefoods", "workday:wd5.myworkdaysite.com/bigcocareers"}
    assert "smartrecruiters:boxmakers" in u["meta"]["dead"]
    assert res["added"] == 4
    # second grow adds nothing new
    assert universe.grow(pipeline.load_yaml("universe.yaml"), {"greenhouse:tinybrand"})["added"] == 0


def test_sweep_finds_niche_role_and_drops_dead_board(sandbox):
    os.makedirs(sandbox / "data", exist_ok=True)
    universe.save({"meta": {}, "boards": {
        "lever:acme": {"ats": "lever", "ids": {"slug": "acme"}, "name": "Cold Chain Co",
                       "added": "2026-11-01", "last_scanned": "", "last_hit": "", "hits": 0, "fails": 0},
        "lever:gone": {"ats": "lever", "ids": {"slug": "gone"}, "name": "Gone Inc",
                       "added": "2026-11-01", "last_scanned": "", "last_hit": "", "hits": 0, "fails": 2},
        "lever:talentco": {"ats": "lever", "ids": {"slug": "acme"}, "name": "Apex Talent Staffing",
                           "added": "2026-11-01", "last_scanned": "", "last_hit": "", "hits": 0, "fails": 0},
    }})
    res = pipeline.run()
    matches = json.load(open(sandbox / "data" / "matches.json"))
    found = [m for m in matches.values() if m["company"] == "Cold Chain Co"]
    assert len(found) == 1 and found[0]["origin"] == "universe" and found[0]["metro"] == "Chicago"
    assert not any(m["company"] == "Apex Talent Staffing" for m in matches.values())  # staffing excluded

    u = universe.load()
    assert u["boards"]["lever:acme"]["last_hit"] == date.today().isoformat()
    assert "lever:gone" not in u["boards"]          # 3rd straight failure -> dropped
    md = (sandbox / "results" / "latest.md").read_text()
    assert "Cold Chain Co *" in md and "automatic discovery" in md
    assert "[discovered boards] swept" in md
    assert res["new"] == 2   # curated Acme GH analyst + discovered Cold Chain Co analyst


def test_no_universe_flag(sandbox):
    os.makedirs(sandbox / "data", exist_ok=True)
    universe.save({"meta": {}, "boards": {
        "lever:acme": {"ats": "lever", "ids": {"slug": "acme"}, "name": "Cold Chain Co",
                       "added": "x", "last_scanned": "", "last_hit": "", "hits": 0, "fails": 0}}})
    res = pipeline.run(sweep_universe=False)
    assert res["new"] == 1


def test_loc_maybe():
    import yaml
    from jobscout.filters import Filters
    f = Filters(yaml.safe_load(open(os.path.join(ROOT, "config", "filters.yaml"))))
    assert f.loc_maybe(["San Jose, CA"]) and f.loc_maybe(["3 Locations"]) and f.loc_maybe([])
    assert f.loc_maybe(["Remote"]) and f.loc_maybe(["United States"])
    assert not f.loc_maybe(["Bengaluru, India"]) and not f.loc_maybe(["Houston, TX"])
