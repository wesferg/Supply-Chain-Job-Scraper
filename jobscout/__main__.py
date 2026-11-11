"""Command line:

  python -m jobscout run                 # full daily run, writes results/ and data/
  python -m jobscout run --dry-run       # print matches, change nothing
  python -m jobscout run --company Nike  # one company
  python -m jobscout discover            # show which ATS each company resolves to
  python -m jobscout grow                # find new job boards (Common Crawl, Wayback, web search)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests


def open_issue(title: str, body: str) -> None:
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        print("(no GITHUB_TOKEN/GITHUB_REPOSITORY; skipping issue)")
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
    ap = argparse.ArgumentParser(prog="jobscout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--company")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--issue", action="store_true", help="open a GitHub issue with new matches")
    r.add_argument("--no-universe", action="store_true", help="only your curated companies")
    sub.add_parser("discover", help="re-resolve the careers site of every curated company")
    sub.add_parser("grow", help="find new job boards to add to the discovered universe")
    a = ap.parse_args(argv)

    from . import pipeline, store, discover, universe

    if a.cmd == "grow":
        state = store.State()
        res = universe.grow(pipeline.load_yaml("universe.yaml"), pipeline.curated_board_keys(state))
        print(json.dumps(res))
        return 0

    if a.cmd == "discover":
        state = store.State()
        log: list[str] = []
        for entry in pipeline.load_yaml("companies.yaml")["companies"]:
            state.resolved.pop(entry["name"], None)  # force a fresh look
            hit = pipeline.company_ids(entry, state, log)
            print(f"{entry['name']:<32} {hit[0] + ' ' + json.dumps(hit[1]) if hit else 'NOT FOUND'}")
        state.save()
        return 0

    res = pipeline.run(dry_run=a.dry_run, only=a.company, use_llm=not a.no_llm,
                       sweep_universe=not a.no_universe)
    print(f"{res['companies']} companies, {res['postings_checked']} postings in field, "
          f"{res['new']} new matches, {res['open']} open")
    if a.dry_run:
        for row in res["new_rows"]:
            print(f"  [{row['score']:>3}] {row['title']} - {row['company']} ({row['metro']}) {row['url']}")
        print("\n".join(res["log"]))
        return 0
    if a.issue and res.get("digest_count"):
        from datetime import date
        n = res["digest_count"]
        open_issue(f"{n} new job match{'es' if n != 1 else ''} - {date.today():%b %d}", res["digest"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
