"""Hard filters: title, location, seniority/experience, salary.

Two stages:
  title_ok(title)  - cheap; runs on list pages before fetching job details.
  evaluate(job)    - full check once the description and locations are in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import Job
from datetime import date

from .text import parse_salary, posted_date, years_detail

_US = re.compile(r"\b(US|USA|U\.S\.A?\.?|United States( of America)?|North America|Anywhere)\b", re.I)


# Non-US countries, as names or the ISO codes Workday appends ("San Francisco,CRI").
_FOREIGN = re.compile(
    r"(?<![A-Za-z])(CRI|CAN|MEX|IND|CHN|GBR|IRL|DEU|FRA|NLD|ESP|ITA|POL|ISR|JPN|KOR|TWN|SGP|MYS|PHL"
    r"|BRA|ARG|COL|CHL|PER|AUS|NZL|CHE|SWE|DNK|NOR|FIN|BEL|AUT|CZE|HUN|ROU|PRT|VNM|THA|IDN|ARE"
    r"|SAU|ZAF|EGY|TUR|HKG|PRI)(?![A-Za-z])"
    r"|(?i:\b(costa rica|canada|mexico|india|china|united kingdom|england|ireland|germany|france"
    r"|netherlands|spain|italy|poland|israel|japan|korea|taiwan|singapore|malaysia|philippines"
    r"|brazil|argentina|colombia|chile|australia|new zealand|switzerland|sweden|denmark|norway"
    r"|finland|belgium|austria|czech|hungary|romania|portugal|vietnam|thailand|indonesia"
    r"|emirates|saudi|south africa|egypt|turkey|hong kong|puerto rico|ontario|british columbia"
    r"|quebec|alberta)\b)")


def _rx(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


def _state_rx(tokens: list[str]) -> re.Pattern:
    codes = [t for t in tokens if len(t) == 2 and t.isupper()]
    names = [t for t in tokens if t not in codes]
    parts = []
    if codes:
        parts.append(r"(?<![A-Za-z])(?:" + "|".join(codes) + r")(?![A-Za-z])")
    if names:
        parts.append(r"(?i:\b(?:" + "|".join(re.escape(n) for n in names) + r")\b)")
    return re.compile("|".join(parts))


@dataclass
class Decision:
    keep: bool
    reason: str = ""


class Filters:
    def __init__(self, cfg: dict):
        t = cfg["titles"]
        self.core = _rx(t["core"])
        self.adjacent = _rx(t["adjacent"])
        self.general = _rx(t.get("general", []))
        self.exclude = _rx(t["exclude"])
        self.exclude_unless_core = _rx(t.get("exclude_unless_core", []))
        self.engineer_ok = _rx(t.get("engineer_allowed", []))
        self.entry = _rx(t.get("entry_signals", []))
        self.search_terms: list[str] = cfg.get("workday_search_terms", [])

        e = cfg.get("experience", {})
        self.reject_years = e.get("reject_if_min_years_at_least", 5)
        self.stretch_years = e.get("stretch_if_min_years_at_least", 3)
        self.max_age_days = cfg.get("max_posting_age_days")

        s = cfg.get("salary", {})
        self.floor = s.get("floor", 0)
        self.good_pay = s.get("good", self.floor)
        self.senior_reject = s.get("senior_reject_min")
        self.senior_flag = s.get("senior_flag_min")

        loc = cfg["locations"]
        self.allow_remote = loc.get("allow_remote_us", True)
        unamb = {c.lower() for c in loc.get("unambiguous", [])}
        self.metros: list[tuple[str, re.Pattern, list[tuple[re.Pattern, bool]]]] = []
        for metro, spec in loc["metros"].items():
            state = _state_rx(spec["state"])
            cities = [(re.compile(r"\b" + re.escape(c) + r"\b", re.I), c.lower() in unamb)
                      for c in spec["cities"]]
            self.metros.append((metro, state, cities))
        self.blocked = _state_rx(loc.get("blocked_states", []))

    # ---------------- titles ----------------

    def title_category(self, title: str) -> Optional[str]:
        """'core' | 'adjacent' | 'general' | None (rejected)."""
        if not title:
            return None
        for rx in self.exclude:
            if rx.search(title):
                return None
        if re.search(r"\bengineer", title, re.I) and not any(r.search(title) for r in self.engineer_ok):
            return None
        if any(r.search(title) for r in self.core):
            return "core"
        if any(r.search(title) for r in self.exclude_unless_core):
            return None
        if any(r.search(title) for r in self.adjacent):
            return "adjacent"
        if any(r.search(title) for r in self.general):
            return "general"
        return None

    def title_ok(self, title: str) -> bool:
        return self.title_category(title) is not None

    def entry_signal(self, title: str) -> bool:
        return any(r.search(title) for r in self.entry)

    # ---------------- locations ----------------

    def match_location(self, loc: str) -> Optional[str]:
        if _FOREIGN.search(loc):  # "San Francisco,CRI" is in Costa Rica
            return None
        for metro, state, cities in self.metros:
            for city_rx, unambiguous in cities:
                if city_rx.search(loc) and (unambiguous or state.search(loc)):
                    return metro
        return None

    _VAGUE = re.compile(r"\d+\s+locations|multiple|various|remote|anywhere|hybrid|^\s*(us|usa|"
                        r"united states( of america)?|north america)\s*$", re.I)

    def loc_maybe(self, locs: list[str]) -> bool:
        """Cheap pre-screen on list-page location text. Says yes when unsure,
        so it only drops postings that are clearly somewhere you won't go."""
        locs = [l for l in locs if l and l.strip()]
        if not locs:
            return True
        return any(self.match_location(l) or self._VAGUE.search(l) for l in locs)

    def location(self, job: Job) -> Decision:
        locs = [l for l in job.locations if l and l.strip()]
        for l in locs:
            m = self.match_location(l)
            if m:
                return Decision(True, m)
        if self.allow_remote:
            for l in locs or [""]:
                is_remote = job.remote or "remote" in l.lower()
                if not is_remote:
                    continue
                if self.blocked.search(l):
                    continue
                stripped = re.sub(r"(?i)remote|hybrid|[-,()/|:]", " ", l).strip()
                if not stripped or _US.search(l):
                    return Decision(True, "Remote (US)")
        shown = "; ".join(locs)[:80] or "no location listed"
        return Decision(False, f"location: {shown}")

    # ---------------- posting age ----------------

    def age_days(self, posted) -> int | None:
        d = posted_date(posted)
        return (date.today() - d).days if d else None

    def too_old(self, posted) -> bool:
        a = self.age_days(posted)
        return bool(self.max_age_days) and a is not None and a > self.max_age_days

    def age(self, job: Job) -> Decision:
        d = posted_date(job.posted)
        if d:
            job.posted = d.isoformat()
        if self.too_old(job.posted):
            return Decision(False, f"age: posted {self.age_days(job.posted)} days ago")
        return Decision(True, "")

    # ---------------- everything ----------------

    def evaluate(self, job: Job) -> Decision:
        cat = self.title_category(job.title)
        if not cat:
            return Decision(False, "title")
        job.flags = [f for f in job.flags if f not in ("core", "adjacent", "general")] + [cat]

        if re.search(r"part[- ]time|contract|temporary|intern", job.time_type or "", re.I):
            return Decision(False, f"type: {job.time_type}")

        d = self.location(job)
        if not d.keep:
            return d
        job.metro = d.reason

        d = self.age(job)
        if not d.keep:
            return d

        job.min_years, job.pref_years = years_detail(job.description)
        if job.min_years is not None and job.min_years >= self.reject_years:
            return Decision(False, f"experience: {job.min_years}+ years required")
        if job.min_years is not None and job.min_years >= self.stretch_years:
            job.flags.append("stretch")
        elif job.pref_years is not None and job.pref_years >= self.reject_years:
            job.flags.append("stretch")  # "6+ years preferred": not ruled out, ranked lower

        if job.salary_max is None:
            job.salary_min, job.salary_max = parse_salary(job.description)
        if job.salary_max is not None and job.salary_max < self.floor:
            return Decision(False, f"salary: tops out at ${job.salary_max:,.0f}")
        low = job.salary_min or job.salary_max
        if low and self.senior_reject and low >= self.senior_reject:
            return Decision(False, f"salary: starts at ${low:,.0f} (senior pay band)")
        if low and self.senior_flag and low >= self.senior_flag:
            job.flags.append("senior_pay")

        if self.entry_signal(job.title):
            job.flags.append("entry")
        return Decision(True, "ok")
