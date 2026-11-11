"""Shared data structures."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


class BoardUnavailable(Exception):
    """The board didn't answer like a real job board (wrong id, removed, blocked)."""


@dataclass
class Job:
    ats: str                      # workday | greenhouse | lever | ashby | smartrecruiters
    company: str
    job_id: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    description: str = ""         # plain text
    posted: str = ""              # whatever the ATS gives us (ISO date or "Posted 3 Days Ago")
    time_type: str = ""           # Full time / Part time / Contract, when known
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    remote: bool = False

    # filled in by the pipeline
    metro: str = ""               # which allowed metro matched (e.g. "SF Bay Area")
    min_years: Optional[int] = None
    flags: list[str] = field(default_factory=list)
    score: Optional[int] = None
    verdict: str = ""
    why: str = ""
    concerns: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.company}:{self.job_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
