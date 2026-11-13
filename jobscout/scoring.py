"""Fit scoring: a deterministic rule score, upgraded by Claude when an API key is set."""
from __future__ import annotations

import json
import os
import re

import requests
import yaml

from .models import Job

DESC_KEYWORDS = [
    "forecast", "demand plan", "supply plan", "inventory", "s&op", "sql", "python", "excel",
    "tableau", "power bi", "sap", "erp", "kinaxis", "o9", "blue yonder", "anaplan",
    "optimization", "simulation", "linear programming", "statistics", "industrial engineering",
    "operations research", "new grad", "recent graduate", "0-2 years", "1-3 years",
]


def rule_score(job: Job, floor: float, good_pay: float) -> int:
    s = 0
    general = "core" not in job.flags and "adjacent" not in job.flags
    s += 40 if "core" in job.flags else 28 if "adjacent" in job.flags else 8
    s += {"SF Bay Area": 25, "Remote (US)": 12}.get(job.metro, 18)
    if "entry" in job.flags and not general:  # "Associate" alone doesn't make bizops supply chain
        s += 10
    if job.min_years is None or job.min_years <= 2:
        s += 8
    if "stretch" in job.flags:
        s -= 6
    if "senior_pay" in job.flags:
        s -= 12
    if job.salary_max is not None:
        mid = ((job.salary_min or job.salary_max) + job.salary_max) / 2
        s += 7 if mid >= good_pay else 3
    desc = job.description.lower()
    s += min(10, sum(2 for k in DESC_KEYWORDS if k in desc))
    return max(0, min(100, s))


_PROMPT = """You are screening job postings for one specific candidate. Score how good a fit
this posting is for them, from 0 to 100, being honest and calibrated:
  85-100 strong fit: right field, right level, preferred location, they'd be competitive
  65-84  good fit: worth applying
  45-64  stretch or partial fit
  0-44   poor fit (wrong field, too senior, location/pay they ruled out)

CANDIDATE
{profile}

POSTING
Company: {company}
Title: {title}
Location(s): {locations}
Pay: {pay}
Description (may be truncated):
{description}

Reply with ONLY a JSON object, no prose:
{{"score": <int>, "verdict": "strong|good|stretch|poor",
  "why": "<one sentence on why it fits, specific to his background>",
  "concerns": "<one short phrase, or empty string>"}}"""


def _profile_text(profile: dict) -> str:
    keep = {k: profile[k] for k in ("summary", "experience", "skills", "interests", "preferences")
            if k in profile}
    return yaml.safe_dump(keep, sort_keys=False, width=100)


def llm_score(job: Job, profile: dict) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    pay = (f"${job.salary_min:,.0f} - ${job.salary_max:,.0f}"
           if job.salary_max else "not listed")
    prompt = _PROMPT.format(
        profile=_profile_text(profile), company=job.company, title=job.title,
        locations="; ".join(job.locations) or "not listed", pay=pay,
        description=job.description[:7000],
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": profile.get("model", "claude-haiku-5-5"), "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else None
        if not data or "score" not in data:
            return None
        data["score"] = max(0, min(100, int(data["score"])))
        return data
    except Exception as e:  # never let scoring break the run
        print(f"  ! Claude scoring failed for {job.title} @ {job.company}: {e}")
        return None


def rule_why(job: Job) -> str:
    parts = ["core planning/supply chain role" if "core" in job.flags else
             "adjacent supply chain role" if "adjacent" in job.flags else "general operations role"]
    if "senior_pay" in job.flags:
        parts.append("pay band suggests a more senior hire")
    if "entry" in job.flags:
        parts.append("entry-level title")
    if job.min_years is not None:
        parts.append(f"requires {job.min_years}+ yrs")
    if job.pref_years is not None and (job.min_years is None or job.pref_years > job.min_years):
        parts.append(f"prefers {job.pref_years}+ yrs")
    if job.salary_max is None:
        parts.append("pay not listed")
    return "; ".join(parts).capitalize()


def score(job: Job, profile: dict, floor: float, good_pay: float, allow_llm: bool = True) -> None:
    job.score = rule_score(job, floor, good_pay)
    job.verdict = ""
    job.why = rule_why(job)
    res = llm_score(job, profile) if allow_llm else None
    if res:
        job.score = res["score"]
        job.verdict = str(res.get("verdict", ""))
        job.why = str(res.get("why", ""))
        job.concerns = str(res.get("concerns", ""))
