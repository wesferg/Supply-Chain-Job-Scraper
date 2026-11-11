"""State kept in the repo between runs (data/*.json) and the results files."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")


def _load(name: str) -> dict:
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def _save(name: str, obj: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


class State:
    """matches.json  - every job that passed the filters, with its score
    rejected.json - job keys that failed, so we don't re-check them daily
    resolved.json - company -> ATS ids found by discovery"""

    def __init__(self):
        self.matches = _load("matches.json")
        self.rejected = _load("rejected.json")
        self.resolved = _load("resolved.json")

    def known_keys(self) -> set[str]:
        return set(self.matches) | set(self.rejected)

    def prune(self, keep_days: int = 60) -> None:
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        for store in (self.matches, self.rejected):
            for k in [k for k, v in store.items() if v.get("last_seen", "") < cutoff]:
                del store[k]

    def save(self) -> None:
        _save("matches.json", self.matches)
        _save("rejected.json", self.rejected)
        _save("resolved.json", self.resolved)


CSV_FIELDS = ["score", "verdict", "title", "company", "metro", "locations", "salary",
              "min_years", "flags", "why", "concerns", "url", "first_seen", "origin", "ats"]


def _salary(r: dict) -> str:
    lo, hi = r.get("salary_min"), r.get("salary_max")
    if not hi:
        return ""
    return f"${lo/1000:,.0f}k-${hi/1000:,.0f}k" if lo and lo != hi else f"${hi/1000:,.0f}k"


def write_csv(rows: list[dict]) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "jobs.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "locations": "; ".join(r.get("locations") or []),
                        "flags": " ".join(f for f in r.get("flags") or [] if f != "cached"),
                        "salary": _salary(r)})
    return path


def _md_cell(s) -> str:
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def digest_markdown(new_rows: list[dict], all_rows: list[dict], log: list[str],
                    min_score: int) -> str:
    today = datetime.now().strftime("%a %b %d, %Y")
    shown = [r for r in new_rows if (r.get("score") or 0) >= min_score]
    hidden = len(new_rows) - len(shown)
    lines = [f"# Job matches - {today}", ""]
    if shown:
        lines += [f"**{len(shown)} new match{'es' if len(shown) != 1 else ''}** "
                  f"(score {min_score}+){f', plus {hidden} lower-scoring in jobs.csv' if hidden else ''}.",
                  "", "| Score | Role | Company | Where | Pay | Why |",
                  "|---:|---|---|---|---|---|"]
        for r in shown:
            flags = []
            if "entry" in (r.get("flags") or []):
                flags.append("entry-level")
            if "stretch" in (r.get("flags") or []):
                flags.append(f"stretch: {r.get('min_years')}+ yrs")
            why = r.get("why") or ""
            if r.get("concerns"):
                why += f" _Watch: {r['concerns']}_"
            if flags:
                why += f" `{', '.join(flags)}`"
            company = _md_cell(r["company"]) + (" *" if r.get("origin") == "universe" else "")
            lines.append(
                f"| {r.get('score')} | [{_md_cell(r['title'])}]({r['url']}) | {company} "
                f"| {_md_cell(r.get('metro'))} | {_salary(r) or '-'} | {_md_cell(why)} |")
        if any(r.get("origin") == "universe" for r in shown):
            lines += ["", "\\* a company found by automatic discovery, not on your list"]
    else:
        lines.append(f"No new matches scoring {min_score}+ today"
                     f"{f' ({hidden} lower-scoring added to jobs.csv)' if hidden else ''}.")
    lines += ["", f"{len(all_rows)} open matches in total - see `results/jobs.csv`.", "",
              "<details><summary>Run log</summary>", "", *[f"- {l}" for l in log], "", "</details>"]
    return "\n".join(lines) + "\n"
