"""Adapters and the full pipeline, with the network replaced by canned API responses
shaped like the real Workday / Greenhouse / Lever / Ashby payloads."""
import json
import os

import pytest

from jobscout import http, discover, pipeline, store
from jobscout.ats import workday, simple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- canned responses ----------------

WD_LIST = {
    "total": 3,
    "jobPostings": [
        {"title": "Planner, Demand Agg", "externalPath": "/job/San-Francisco-CA/Planner--Demand-Agg_R210823",
         "locationsText": "San Francisco, CA", "postedOn": "Posted 2 Days Ago", "bulletFields": ["R210823"]},
        {"title": "Senior Demand Planner", "externalPath": "/job/SF/Senior-Demand-Planner_R1",
         "locationsText": "San Francisco, CA", "postedOn": "Posted Today", "bulletFields": ["R1"]},
        {"title": "Supply Chain Analyst", "externalPath": "/job/Austin-TX/Supply-Chain-Analyst_R2",
         "locationsText": "2 Locations", "postedOn": "Posted Today", "bulletFields": ["R2"]},
    ],
}
WD_DETAIL = {
    "/job/San-Francisco-CA/Planner--Demand-Agg_R210823": {"jobPostingInfo": {
        "title": "Planner, Demand Agg", "location": "Two Folsom, San Francisco, CA, US 94105",
        "jobDescription": "<p>Forecast season inventory. 1-3 years of experience in planning.</p>"
                          "<p>The base pay range is $80,000 - $105,000 per year.</p>",
        "timeType": "Full time", "startDate": "2026-11-09",
        "externalUrl": "https://gap.wd1.myworkdayjobs.com/GAPINC/job/San-Francisco-CA/Planner--Demand-Agg_R210823"}},
    "/job/Austin-TX/Supply-Chain-Analyst_R2": {"jobPostingInfo": {
        "title": "Supply Chain Analyst", "location": "Austin, TX", "additionalLocations": ["Dallas, TX"],
        "jobDescription": "<p>2+ years of experience.</p>", "timeType": "Full time"}},
}
GH = {"jobs": [
    {"id": 7117805, "title": "Demand Planning Analyst", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7117805",
     "location": {"name": "Boston, MA"}, "updated_at": "2026-11-01",
     "content": "&lt;p&gt;0-2 years of experience. $70,000 - $90,000&lt;/p&gt;"},
    {"id": 2, "title": "Account Executive", "absolute_url": "x", "location": {"name": "Boston, MA"}, "content": ""},
]}
LEVER = [
    {"id": "abc", "text": "Logistics Analyst", "hostedUrl": "https://jobs.lever.co/acme/abc",
     "categories": {"location": "Chicago, IL", "allLocations": ["Chicago, IL"], "commitment": "Full-time"},
     "descriptionPlain": "Build routing models.", "lists": [{"text": "Requirements", "content": "<li>1+ years of experience</li>"}],
     "createdAt": 1731000000000, "salaryRange": {"min": 68000, "max": 82000, "interval": "per-year-salary"}},
]
ASHBY = {"jobs": [
    {"id": "u1", "title": "Supply Planner", "location": "Remote", "isRemote": True, "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "descriptionPlain": "Plan supply. 2 years of experience.",
     "publishedAt": "2026-11-05", "employmentType": "FullTime",
     "compensation": {"summaryComponents": [{"compensationType": "Salary", "minValue": 60000, "maxValue": 64000, "interval": "1 YEAR"}]}},
]}


@pytest.fixture
def fake_net(monkeypatch):
    def post_json(url, payload, **kw):
        if url.endswith("/wday/cxs/gap/GAPINC/jobs"):
            return WD_LIST if payload["offset"] == 0 else {"total": 0, "jobPostings": []}
        return None

    def get_json(url, **kw):
        for path, body in WD_DETAIL.items():
            if url.endswith(path):
                return body
        if "boards-api.greenhouse.io/v1/boards/acme/jobs/" in url:      # detail
            jid = url.rsplit("/", 1)[-1]
            return next((j for j in GH["jobs"] if str(j["id"]) == jid), None)
        if url.endswith("boards-api.greenhouse.io/v1/boards/acme/jobs"):  # list (no content)
            return {"jobs": [{k: v for k, v in j.items() if k != "content"} for j in GH["jobs"]]}
        if "api.lever.co/v0/postings/acme" in url:
            return LEVER
        if "api.ashbyhq.com/posting-api/job-board/acme" in url:
            return ASHBY
        if url.endswith("boards-api.greenhouse.io/v1/boards/acme"):       # board info
            return {"name": "Acme Brands", "content": ""}
        return None

    def status_json(url, payload=None, **kw):
        data = post_json(url, payload) if payload is not None else get_json(url)
        return (200, data) if data is not None else (404, None)

    monkeypatch.setattr(http, "post_json", post_json)
    monkeypatch.setattr(http, "get_json", get_json)
    monkeypatch.setattr(http, "status_json", status_json)


# ---------------- unit: adapters ----------------

def test_workday_parse_url():
    assert workday.parse_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite") == {
        "host": "nvidia.wd5.myworkdayjobs.com", "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"}
    assert workday.parse_url("https://nike.wd1.myworkdayjobs.com/en-US/nke/job/Beaverton-OR/Planner_R-94474") == {
        "host": "nike.wd1.myworkdayjobs.com", "tenant": "nike", "site": "nke"}
    assert workday.parse_url("https://wd1.myworkdaysite.com/recruiting/clorox/Clorox/page/e10acc") == {
        "host": "wd1.myworkdaysite.com", "tenant": "clorox", "site": "Clorox"}


def test_workday_fetch(fake_net):
    ids = {"host": "gap.wd1.myworkdayjobs.com", "tenant": "gap", "site": "GAPINC"}
    jobs = workday.fetch("Gap", ids, ["planner"], lambda t: "Senior" not in t)
    assert {j.title for j in jobs} == {"Planner, Demand Agg", "Supply Chain Analyst"}
    j = next(j for j in jobs if j.title.startswith("Planner"))
    assert j.job_id == "Planner--Demand-Agg_R210823"
    assert "Forecast season inventory" in j.description and j.locations[0].startswith("Two Folsom")
    sca = next(j for j in jobs if j.title == "Supply Chain Analyst")
    assert sca.locations == ["Austin, TX", "Dallas, TX"]


def test_workday_skips_detail_for_known(fake_net):
    ids = {"host": "gap.wd1.myworkdayjobs.com", "tenant": "gap", "site": "GAPINC"}
    jobs = workday.fetch("Gap", ids, ["planner"], lambda t: True,
                         known={"workday:Gap:Planner--Demand-Agg_R210823"})
    cached = [j for j in jobs if "cached" in j.flags]
    assert len(cached) == 1 and cached[0].description == ""


def test_simple_adapters(fake_net):
    gh = simple.greenhouse("Acme", {"token": "acme"}, lambda t: "Account" not in t)
    assert len(gh) == 1 and "0-2 years" in gh[0].description and gh[0].locations == ["Boston, MA"]
    lv = simple.lever("Acme", {"slug": "acme"}, lambda t: True)
    assert lv[0].salary_max == 82000 and "1+ years" in lv[0].description
    ab = simple.ashby("Acme", {"org": "acme"}, lambda t: True)
    assert ab[0].remote and ab[0].salary_max == 64000


def test_discover_from_html():
    page = """<a href="https://specialized.wd5.myworkdayjobs.com/en-US/specialized_careers">Jobs</a>
              <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
              <a href="https://jobs.lever.co/whoop">x</a> <a href="https://jobs.ashbyhq.com/oura">y</a>"""
    found = discover.from_html(page)
    assert ("workday", {"host": "specialized.wd5.myworkdayjobs.com", "tenant": "specialized",
                        "site": "specialized_careers"}) in found
    assert ("greenhouse", {"token": "acme"}) in found
    assert ("lever", {"slug": "whoop"}) in found
    assert ("ashby", {"org": "oura"}) in found


# ---------------- end to end ----------------

@pytest.fixture
def sandbox(tmp_path, monkeypatch, fake_net):
    """Real config files, a temp data/results dir, a 4-company list, no Claude key."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    for n in ("filters.yaml", "profile.yaml"):
        (cfg / n).write_text(open(os.path.join(ROOT, "config", n)).read())
    (cfg / "companies.yaml").write_text(
        "companies:\n"
        "  - {name: Gap, url: 'https://gap.wd1.myworkdayjobs.com/GAPINC'}\n"
        "  - {name: Acme GH, ats: greenhouse, token: acme}\n"
        "  - {name: Acme Lever, ats: lever, slug: acme}\n"
        "  - {name: Acme Ashby, ats: ashby, org: acme}\n")
    monkeypatch.setattr(pipeline, "CONFIG", str(cfg))
    monkeypatch.setattr(store, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(store, "RESULTS", str(tmp_path / "results"))
    monkeypatch.setattr(store, "JOBS_MD", str(tmp_path / "JOBS.md"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # explicit hints still get validated; make validation pass for the fakes
    monkeypatch.setattr(discover, "_validate", lambda ats, ids: True)
    monkeypatch.setattr(workday, "validate", lambda ids: True)
    return tmp_path


def test_pipeline_end_to_end(sandbox):
    res = pipeline.run()
    titles = {(r["company"], r["title"]) for r in json.load(open(sandbox / "data" / "matches.json")).values()}
    # kept: SF demand planner, Boston GH analyst, Chicago Lever analyst
    assert titles == {("Gap", "Planner, Demand Agg"), ("Acme GH", "Demand Planning Analyst"),
                      ("Acme Lever", "Logistics Analyst")}
    rejected = json.load(open(sandbox / "data" / "rejected.json"))
    reasons = {v["title"]: v["reason"] for v in rejected.values()}
    assert reasons["Supply Chain Analyst"].startswith("location")      # Austin/Dallas only
    assert reasons["Supply Planner"].startswith("salary")               # Ashby, tops out at $64k
    assert res["new"] == 3 and res["open"] == 3

    csv_text = (sandbox / "results" / "jobs.csv").read_text()
    assert csv_text.splitlines()[0].startswith("score,verdict,title")
    assert "Planner, Demand Agg" in csv_text
    md = (sandbox / "JOBS.md").read_text()
    assert "3 open roles" in md and "3 new today" in md
    # one ranked list, highest score first, numbered across tiers
    order = [l.split("|")[3] for l in md.splitlines() if l.startswith("| ") and l[2].isdigit()]
    assert "Planner, Demand Agg" in order[0] and "Logistics Analyst" in order[-1]
    assert md.index("| 1 |") < md.index("| 2 |") < md.index("| 3 |")
    assert res["issue"] and "3 new matches" in res["issue"][0]

    # second run: nothing new, everything still open, no duplicate scoring
    res2 = pipeline.run()
    assert res2["new"] == 0 and res2["open"] == 3
    assert res2["issue"] is None                        # nothing new -> no issue


def test_resolution_cache(sandbox):
    pipeline.run()
    resolved = json.load(open(sandbox / "data" / "resolved.json"))
    assert resolved["Gap"]["ats"] == "workday" and resolved["Acme GH"]["ids"] == {"token": "acme"}


def test_bad_company_entries_dont_crash_the_run(sandbox):
    """A bare `On` in YAML parses as True; the run should log it and carry on."""
    cfg = sandbox / "config" / "companies.yaml"
    cfg.write_text(cfg.read_text() + "  - name: On\n")
    res = pipeline.run()
    md = (sandbox / "JOBS.md").read_text()
    assert "unusable name (" in md and res["new"] == 3
    assert json.load(open(sandbox / "data" / "resolved.json"))["Gap"]["ats"] == "workday"
