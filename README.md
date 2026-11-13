# Supply Chain Job Scraper

**[→ Your ranked job list: JOBS.md](JOBS.md)**

One daily run finds supply chain, demand planning, and operations research roles that fit
Wes's rules. It ranks every open match by fit and puts the result in a single list,
`JOBS.md`, with the most important roles first.

It skips the big job boards. Instead it reads the same public JSON that each company's own
careers site loads, on Workday, Greenhouse, Lever, Ashby, SmartRecruiters and Eightfold. That way it
sees postings as soon as the company lists them.

## What one run does

Everything happens in a single command, `python -m jobscout`, which GitHub Actions runs
every morning:

1. **Finds companies.** It checks your list in `config/companies.yaml` every day. On the
   first run, and then weekly, it also discovers thousands more company job boards from
   public web indexes (see below).
2. **Checks the boards.** That means all of your companies, plus a rotating slice of the
   discovered ones, so all of them get covered every few days. Any company that has
   produced a match is checked daily.
3. **Filters** every posting against your rules (table below).
4. **Scores and ranks** every match from 0 to 100 against `config/profile.yaml`. If an
   `ANTHROPIC_API_KEY` is set, Claude does the scoring; otherwise a rule-based score is
   used.
5. **Writes one ranked list** to `JOBS.md`, grouped into four tiers: *Apply first (80+)*,
   *Strong fits (65+)*, *Worth a look (50+)*, and *Long shots*.
   - **NEW** marks roles first seen today.
   - `*` marks companies it discovered on its own.
6. **Opens a GitHub issue** whenever there are new matches. The issue shows the top 20 of
   the same ranked list, and it reaches your phone if you use the GitHub app.

## What it filters for

All rules live in `config/filters.yaml`.

| Rule | Setting |
|---|---|
| Field | Supply chain, demand/supply/inventory/merchandise planning, forecasting, S&OP, operations research, operations simulation and optimization, logistics/procurement analytics |
| Ranked lower | General operations titles (Business Operations, Strategy & Operations, Operations Associate) are kept but sit below real supply chain roles |
| Never | Sales, sales engineering, mechanical/other engineering, HVAC, SEO, actuarial, lab/fund/drone/robot/facilities operations, internships, part-time/contract, talent-community posts |
| Level | No senior/lead/manager/director titles. Reads every experience mention in the full posting and uses the highest *required* number on a bachelor's path ("2+ yrs SQL … 6+ yrs planning" counts as 6). Rejects 5+ years required; ranks 3–4 years required, or 5+ only "preferred", lower; rejects pay bands that *start* at $190k+ (a senior hire) and ranks $150k+ starts lower |
| Where | SF Bay Area (preferred), NYC, Boston, Portland OR, Chicago (and near suburbs), or US-remote. Nothing in Texas, the South, Florida, the Midwest, SoCal, or outside the US |
| Pay | Rejects ranges that top out under $65k; unlisted pay passes |
| Fresh | Only postings from the last 20 days (`max_posting_age_days`); roles drop off the list as they age past that. Undated postings are kept |

## How companies are discovered

The scraper collects career-site URLs from three public sources and keeps every company
job board it recognizes:

| Source | Needs |
|---|---|
| Common Crawl URL index (millions of crawled career-site URLs) | Nothing |
| Wayback Machine index | Nothing |
| Brave web search, e.g. "demand planner" + "Oakland" on career sites | Optional `BRAVE_API_KEY` secret (paid by usage; check Brave's pricing) |

- Each new board is checked once to confirm it's real. If a site is busy or rate-limits the
  check, the board is re-checked next time instead of being written off.
- Junk links (asset files, numeric IDs, tracking pages) are ignored before any request.
- Greenhouse boards show the company's real name, not its board slug.
- Staffing agencies are skipped.
- Boards that stop responding are dropped.

## Setup

1. Make the repo **private**: Settings → General → Danger Zone → Change visibility. The
   profile and results are personal.
2. Recommended: add `ANTHROPIC_API_KEY` under Settings → Secrets and variables → Actions.
   Claude Haiku scores each match once, which costs pennies a day.
3. Go to **Actions → Job scout → Run workflow**. That's the only button. After the first
   run, it runs every morning by itself.

The first run takes longer, because it discovers companies and checks every board for the
first time. Private repos have a monthly allowance of Actions minutes. The daily sweep
stops starting new boards after 40 minutes, and anything left rolls to the next day. You
can tune this with `max_minutes` and `daily_budget_units` in `config/universe.yaml`.

## Files

| File | What it holds |
|---|---|
| `JOBS.md` | **The ranked list.** Every open match, most important first, with a run log at the bottom |
| `results/jobs.csv` | The same ranking with every column, including lower-scoring roles |
| `data/*.json` | Memory: seen jobs, company boards, discovery progress |

## Tuning

- **Off-target titles:** add a pattern to `titles.exclude` in `config/filters.yaml`.
- **Missing a kind of title:** add it to `titles.core` or `titles.adjacent`.
- **Cities:** edit `locations.metros`.
- **Show more or fewer roles in JOBS.md:** change `list_min_score` in `profile.yaml`.
- **Changing the rules** in `filters.yaml` re-checks every saved job on the next run, so
  roles that no longer fit drop off and older ones keep their original "first seen" date.
- **A company on your list says "could not find a supported careers site" in the run
  log:** open any of its job postings and copy the link into its entry in
  `companies.yaml`. Editing an entry re-runs its lookup on the next run.
  - A `...myworkdayjobs.com/...` link goes in `url:`.
  - A `boards.greenhouse.io/<name>` link becomes `ats: greenhouse` plus `token: <name>`.
  - A Lever link becomes `ats: lever` plus `slug: <name>`.
  - An Ashby link becomes `ats: ashby` plus `org: <name>`.
- **A discovered company you never want:** add its name to `exclude_board_names` in
  `config/universe.yaml`.
- **A discovered company you love:** copy it into `companies.yaml` so it's checked daily.

Apple, Google, Tesla, Amazon, Meta and Williams-Sonoma run their own custom career sites
and aren't covered.

To run it locally:

```bash
pip install -r requirements.txt
python -m pytest -q                 # offline tests
python -m jobscout                  # the whole thing
python -m jobscout --dry-run        # print the ranked list, save nothing
```

## Being a polite scraper

- Requests are spaced out: 0.6s per company site, 0.25s on the public job-board APIs.
- Requests are retried with backoff.
- Workday sites are searched by keyword instead of downloading every posting.
- A job's detail page is fetched only once, and only if its title and location pass.
