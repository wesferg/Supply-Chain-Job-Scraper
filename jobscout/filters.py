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
from .text import parse_salary, required_years

_US = re.compile(r"\b(US|USA|U\.S\.A?\.?|United States( of America)?|North America|Anywhere)\b", re.I)


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
        self.exclude = _rx(t["exclude"])
        self.engineer_ok = _rx(t.get("engineer_allowed", []))
        self.entry = _rx(t.get("entry_signals", []))
        self.search_terms: list[str] = cfg.get("workday_search_terms", [])

        e = cfg.get("experience", {})
        self.reject_years = e.get("reject_if_min_years_at_least", 5)
        self.stretch_years = e.get("stretch_if_min_years_at_least", 3)

        s = cfg.get("salary", {})
        self.floor = s.get("floor", 0)
        self.good_pay = s.get("good", self.floor)

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
        """'core' | 'adjacent' | None (rejected)."""
        if not title:
            return None
        for rx in self.exclude:
            if rx.search(title):
                return None
        if re.search(r"\bengineer", title, re.I) and not any(r.search(title) for r in self.engineer_ok):
            return None
        if any(r.search(title) for r in self.core):
            return "core"
        if any(r.search(title) for r in self.adjacent):
            return "adjacent"
        return None

    def title_ok(self, title: str) -> bool:
        return self.title_category(title) is not None

    def entry_signal(self, title: str) -> bool:
        return any(r.search(title) for r in self.entry)

    # ---------------- locations ----------------

    def match_location(self, loc: str) -> Optional[str]:
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

    # ---------------- everything ----------------

    def evaluate(self, job: Job) -> Decision:
        cat = self.title_category(job.title)
        if not cat:
            return Decision(False, "title")
        job.flags = [f for f in job.flags if f not in ("core", "adjacent")] + [cat]

        if re.search(r"part[- ]time|contract|temporary|intern", job.time_type or "", re.I):
            return Decision(False, f"type: {job.time_type}")

        d = self.location(job)
        if not d.keep:
            return d
        job.metro = d.reason

        job.min_years = required_years(job.description)
        if job.min_years is not None and job.min_years >= self.reject_years:
            return Decision(False, f"experience: {job.min_years}+ years")
        if job.min_years is not None and job.min_years >= self.stretch_years:
            job.flags.append("stretch")

        if job.salary_max is None:
            job.salary_min, job.salary_max = parse_salary(job.description)
        if job.salary_max is not None and job.salary_max < self.floor:
            return Decision(False, f"salary: tops out at ${job.salary_max:,.0f}")

        if self.entry_signal(job.title):
            job.flags.append("entry")
        return Decision(True, "ok")
