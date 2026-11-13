import os

import pytest
import yaml

from jobscout.filters import Filters
from jobscout.models import Job
from jobscout.text import parse_salary, required_years, html_to_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def f():
    with open(os.path.join(ROOT, "config", "filters.yaml")) as fh:
        return Filters(yaml.safe_load(fh))


# ---------------- salary ----------------

@pytest.mark.parametrize("text,lo,hi", [
    ("Pay: $71K - $110K", 71000, 110000),
    ("The range is $72,000 - $92,000 per year", 72000, 92000),
    ("USD 65,000 - 95,000", 65000, 95000),
    ("USD 53.9-81.3 / hour + Equity", 53.9 * 2080, 81.3 * 2080),
    ("Hourly Pay Range: $30.00 – $40.00 per hour", 62400, 83200),
    ("Salary: $76,628 Salary, Full-Time", None, None),
    ("$28/hr", 28 * 2080, 28 * 2080),
    ("Bay Area Minimum: $105,000", None, None),
    ("no pay info", None, None),
])
def test_salary(text, lo, hi):
    got = parse_salary(text)
    if lo is None:
        assert got == (None, None)
    else:
        assert got[0] == pytest.approx(lo) and got[1] == pytest.approx(hi)


# ---------------- years of experience ----------------

@pytest.mark.parametrize("text,yrs", [
    ("Four-year college degree and a minimum of 2+ years related experience", 2),
    ("3-5 years of progressive experience in demand planning", 3),
    ("8+ years of hands-on demand planning experience, or 5+ years experience with a Masters", 8),  # bachelor's path
    ("5+ years experience in customer or product supply chain management", 5),
    ("Must be at least 18 years of age.", None),
    ("For over 50 years our company has led the industry.", None),
    ("Prior internship and/or Co-Op experience in Supply Chain", None),
])
def test_years(text, yrs):
    assert required_years(text) == yrs


def test_html_to_text():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;") == "Hello & bye"


# ---------------- titles ----------------

@pytest.mark.parametrize("title,cat", [
    ("Demand Planner - North America", "core"),
    ("Planner, Demand Agg", "core"),
    ("Supply Chain Analyst", "core"),
    ("Supply Chain Business Analyst", "core"),
    ("Supply & Inventory Planning Planner", "core"),
    ("Sales & Operations Planning Analyst", "core"),
    ("S&OP Analyst", "core"),
    ("Operations Research Scientist", "core"),
    ("Supply Chain Engineer", "core"),
    ("Planner, NA", "adjacent"),
    ("Allocator, NA", "adjacent"),
    ("Logistics Analyst", "adjacent"),
    ("Procurement Analyst", "adjacent"),
    ("Supply Chain Analyst II", "core"),
    ("Associate Manager, Supply Planning", "core"),
    ("Data Scientist, Supply Chain Forecasting", "core"),
    # excluded
    ("Senior Demand Planner", None),
    ("Sr. Supply Planner", None),
    ("Lead, Materials Supply Planner", None),
    ("North America Demand Planning Manager", None),
    ("Director, Demand Planning", None),
    ("Supply Chain Analyst III", None),
    ("Inside Sales Engineer - HVAC", None),
    ("Sales Operations Analyst", None),
    ("Mechanical Engineer", None),
    ("Manufacturing Engineer", None),
    ("Supply Chain Intern", None),
    ("Supply Planner (Contract)", None),
    ("Financial Planner", None),
    ("Security Operations Analyst", None),
    ("Software Engineer, Supply Chain Systems", None),
    ("Retail Sales Associate", None),
    ("Marketing Analyst", None),
])
def test_titles(f, title, cat):
    assert f.title_category(title) == cat


def test_contract_manufacturing_is_allowed(f):
    assert f.title_category("Contract Manufacturing Planner") == "adjacent"


# ---------------- locations ----------------

@pytest.mark.parametrize("loc,metro", [
    ("San Francisco, CA", "SF Bay Area"),
    ("Two Folsom, San Francisco, CA, US 94105", "SF Bay Area"),
    ("United States, CA, Morgan Hill (Office)", "SF Bay Area"),
    ("USA-CA-Santa Clara", "SF Bay Area"),
    ("Oakland, California", "SF Bay Area"),
    ("Beaverton, OR", "Portland"),
    ("Portland, Oregon, United States", "Portland"),
    ("Boston, MA", "Boston"),
    ("Hybrid - Boston, MA Headquarters - (NB)", "Boston"),
    ("New York, NY", "New York City"),
    ("Brooklyn", "New York City"),
    ("Chicago, IL", "Chicago"),
    # rejected
    ("Portland, ME", None),
    ("Dublin, Ireland", None),
    ("Newark, NJ", None),
    ("Concord, NH", None),
    ("Austin, TX", None),
    ("Los Angeles, CA", None),
    ("San Diego, CA", None),
    ("Irvine, CA", None),
    ("Mason, OH", None),
    ("Hünenberg, Switzerland", None),
])
def test_metro(f, loc, metro):
    assert f.match_location(loc) == metro


@pytest.mark.parametrize("locs,keep", [
    (["Remote - US"], True),
    (["Remote"], True),
    (["United States (Remote)"], True),
    (["Remote - Texas"], False),
    (["Remote, Canada"], False),
    (["Austin, TX", "San Francisco, CA"], True),   # multi-location: any allowed metro wins
    (["Dallas, TX", "Atlanta, GA"], False),
])
def test_job_locations(f, locs, keep):
    assert f.location(Job("x", "c", "1", "t", "u", locations=locs)).keep is keep


# ---------------- whole-job evaluation ----------------

def _job(**kw):
    base = dict(ats="greenhouse", company="Acme", job_id="1", title="Demand Planner",
                url="https://x", locations=["San Francisco, CA"],
                description="Bachelor's degree and 1-3 years of experience. Pay: $80,000 - $100,000 per year.")
    base.update(kw)
    return Job(**base)


def test_evaluate_pass(f):
    j = _job()
    d = f.evaluate(j)
    assert d.keep and j.metro == "SF Bay Area" and j.salary_max == 100000 and j.min_years == 1


def test_evaluate_too_senior(f):
    d = f.evaluate(_job(description="Requires 6+ years of experience in demand planning."))
    assert not d.keep and "experience" in d.reason


def test_evaluate_stretch(f):
    j = _job(description="3-5 years of experience in supply planning.")
    assert f.evaluate(j).keep and "stretch" in j.flags
    j = _job(description="1+ years of experience required. 6+ years of experience preferred.")
    assert f.evaluate(j).keep and "stretch" in j.flags and (j.min_years, j.pref_years) == (1, 6)
    j = _job(description="2+ years with SQL. 6+ years of experience in demand planning.")
    d = f.evaluate(j)
    assert not d.keep and d.reason == "experience: 6+ years required"


def test_evaluate_low_pay(f):
    d = f.evaluate(_job(description="Pay: $22 - $26 per hour"))
    assert not d.keep and "salary" in d.reason


def test_evaluate_unknown_pay_passes(f):
    assert f.evaluate(_job(description="Great role, 0-2 years of experience.")).keep


def test_evaluate_part_time(f):
    assert not f.evaluate(_job(time_type="Part time")).keep


def test_entry_flag(f):
    j = _job(title="Associate Demand Planner")
    f.evaluate(j)
    assert "entry" in j.flags
