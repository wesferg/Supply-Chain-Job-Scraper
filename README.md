# job-scout

A daily scraper that checks company career sites directly (Workday, Greenhouse, Lever,
Ashby, SmartRecruiters), keeps the supply chain / demand planning / OR roles that fit
Wes's rules, scores each one, and posts the new ones as a GitHub issue.

It skips the big job boards. It reads the same public JSON that each company's own
careers page loads, so it sees postings as soon as the company lists them.

It watches two sets of companies:

- **Your list** (`config/companies.yaml`, about 70 companies). These are checked every day.
- **The discovered universe.** Thousands of company job boards are found automatically
  (see below) and swept in rotation. This is how it finds niche roles at companies you've
  never heard of.

## Finding companies beyond your list

Each week, `python -m jobscout grow` collects career-site URLs from public sources and
pulls out every company job board it recognizes on the five supported systems:

| Source | What it gives | Needs |
|---|---|---|
| Common Crawl URL index | Millions of crawled career-site URLs, read a few pages at a time each week | Nothing |
| Wayback Machine index | More career-site URLs from the Internet Archive | Nothing |
| Brave web search | Boards with live postings like "demand planner" + "Oakland". The most targeted source | A `BRAVE_API_KEY` secret (optional) |

Each new board is checked once to confirm it's real. Staffing agencies are skipped by name.
New boards are added to `data/universe.json`.

The daily run then sweeps part of the universe, set by `daily_budget_units` in
`config/universe.yaml`:

- Boards never checked, or checked longest ago, go first, so the whole universe is covered
  every few days.
- A board that produced a match in the last 45 days is checked **every day**.
- A board that fails 3 sweeps in a row is dropped.
- In the daily digest, discovered companies are marked with `*` and the run log names
  them.

Your filters apply to these boards exactly as they do to your list. Postings outside your
cities are screened out from the list page, before any detail is fetched, which keeps the
sweep cheap.

## What it filters for

All rules live in `config/filters.yaml`.

| Rule | Setting |
|---|---|
| Field | Supply chain, demand/supply/inventory planning, forecasting, S&OP, OR, simulation, logistics/procurement analytics |
| Never | Sales, sales engineering, mechanical/other engineering, HVAC, internships, part-time/contract |
| Level | No senior/lead/manager/director titles; rejects postings that require 5+ years; flags 3–4 years as a "stretch" |
| Where | SF Bay Area (preferred), NYC, Boston, Portland OR, Chicago, or US-remote. Nothing in Texas, the South, Florida, the Midwest, or SoCal |
| Pay | Rejects ranges that top out under $65k; unlisted pay passes |

The run has three stages:

1. **Title screen.** List pages are cheap, so titles get screened there first. Full job
   details are fetched only for titles that pass.
2. **Full check.** Each remaining job is checked for location, years of experience, pay
   and job type.
3. **Scoring.** Each survivor gets a 0–100 score. With an `ANTHROPIC_API_KEY` secret set,
   Claude scores it against `config/profile.yaml` and writes a one-line "why". Without
   the key, a rule-based score is used.

## Setup (once)

1. Create a **private** repo, since the profile is personal. Push this folder to it.
2. Optional but recommended: go to **Settings → Secrets and variables → Actions → New
   repository secret** and add `ANTHROPIC_API_KEY`. Scoring uses Claude Haiku and costs
   pennies per day, because only new matches are scored.
3. Optional: add a `BRAVE_API_KEY` secret too, for web-search discovery. Brave's Search
   API is paid by usage, with monthly credits; check their current pricing. Everything
   else works without it.
4. Go to **Actions → Job scout → Run workflow**, choose `discover`, and run it. The log
   shows which careers site was found for each of your companies. Fix any `NOT FOUND` rows
   as described below.
5. Run it again with `grow`. This builds the universe for the first time and then does a
   normal run. After that it runs every morning on its own, and grows the universe every
   Monday.
6. To get phone notifications, install the GitHub mobile app and watch the repo (or just
   its issues). Each day with new matches opens an issue.

**Actions minutes:** private repos get a monthly allowance of free Actions minutes. The
sweep stops starting new boards after `max_minutes` (40 by default), so a daily run stays
well under an hour. Check **Settings → Billing** after the first week. If you're running
low, lower `daily_budget_units` or `max_minutes` in `config/universe.yaml`.

## Where results go

| File | What it holds |
|---|---|
| `results/latest.md` | Today's digest: new matches, scores, why, and a run log |
| `results/jobs.csv` | Every currently open match, best first. GitHub renders it as a sortable table |
| `data/matches.json`, `data/rejected.json` | Memory, so a job is only reported and scored once |
| `data/resolved.json` | Which ATS each of your companies uses. Delete an entry to force a re-check |
| `data/universe.json` | Every discovered board, with when it was last checked and last matched |

## Adding or fixing companies

Edit `config/companies.yaml`. Only a `name` is required. Discovery tries three things in
order:

1. Any hint you give.
2. Scanning the `careers_url` page for ATS links.
3. Guessing board names on each ATS.

The fastest fix for a `NOT FOUND` company is to open its careers site, click any job, and
copy the URL into the entry:

- A `...myworkdayjobs.com/...` link goes in `url:`.
- A `boards.greenhouse.io/<name>` link becomes `ats: greenhouse` plus `token: <name>`.
- A `jobs.lever.co/<name>` link becomes `ats: lever` plus `slug: <name>`.
- A `jobs.ashbyhq.com/<name>` link becomes `ats: ashby` plus `org: <name>`.

Apple, Google, Tesla, Amazon and Meta run their own custom sites and aren't supported.

## Tuning

- Too many off-target titles: add a pattern to `titles.exclude`.
- Missing a title type: add it to `titles.core` or `titles.adjacent`.
- To add a city, add it under `locations.metros`.
- To see lower-scoring roles in the digest, lower `digest_min_score` in `profile.yaml`.
  Lower-scoring roles always stay in `jobs.csv`.
- To test a change without saving anything, run the workflow in `dry-run` mode, or run
  locally:

```bash
pip install -r requirements.txt
python -m pytest -q                       # 101 offline tests
python -m jobscout grow                   # find new job boards
python -m jobscout run --dry-run          # live, prints matches, saves nothing
python -m jobscout run --dry-run --company Nike
python -m jobscout run --no-universe      # only your list
python -m jobscout discover
```

- If a discovered company keeps showing up with roles you'll never want, add its name to
  `exclude_board_names` in `config/universe.yaml`.
- If a discovered company turns out to be great, copy it into `companies.yaml` so it's
  checked daily with the fuller search.

## Being a polite scraper

Requests to each site are spaced out and retried with backoff: 0.6s per company site,
0.25s on the public job-board APIs. Workday is queried with search terms instead of
downloading every posting. A job's detail page is only fetched if its title and location
pass, and only the first time that job is seen. One run a day is plenty, so keep it that
way.
