"""One command does everything:

  python -m jobscout              # find companies, check every board, rank all matches -> JOBS.md

It resolves your curated companies, discovers new ones (first run, then weekly), sweeps
today's slice of discovered boards, filters and scores every posting, and writes one
ranked list. In GitHub Actions it also opens an issue when there are new matches.

Options (rarely needed):
  --dry-run          print the ranked list, save nothing
  --company NAME     check a single company from your list
  --no-llm           rule-based scoring only
"""
from __future__ import annotations

import argparse
import os
import sys

import requests


def open_issue(title: str, body: str) -> None:
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    payload = {"title": title, "body": body[:65000], "labels": ["job-matches"]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 422:  # label trouble; post without it
        payload.pop("labels")
        r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"issue: HTTP {r.status_code}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobscout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", help=argparse.SUPPRESS)  # "run" still accepted
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--company")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.error(f"unknown command '{a.cmd}' - just run `python -m jobscout`")

    from . import pipeline

    res = pipeline.run(dry_run=a.dry_run, only=a.company, use_llm=not a.no_llm)
    print(f"{res['companies']} boards checked, {res['postings_checked']} postings in your field, "
          f"{res['new']} new matches, {res['open']} open matches")
    if a.dry_run:
        for i, row in enumerate(res["ranked"][:50], 1):
            print(f"{i:>3}. [{row['score']:>3}] {row['title']} - {row['company']} "
                  f"({row.get('metro')}) {row['url']}")
        print("\n".join(res["log"]))
        return 0
    if res.get("issue"):
        open_issue(*res["issue"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
