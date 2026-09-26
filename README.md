# JobHub

A local internship search engine. It pulls postings from GitHub aggregator lists and company ATS boards, dedupes
them, drops the ones that break your hard rules, scores everything that survives, and shows you the shortlist —
sorted into **Likely**, **Reach** and **Wildcard**.

Every model call goes through headless Claude Code (`claude -p`) on your subscription. No API key, no per-token bill.

**Most of the work is free.** Ingest, dedupe, the prefilter and a deterministic 0–100 score cost nothing. Only the
unclear middle — roughly one posting in seven — reaches a model, under a token cap you set. The full rubric never
runs on its own; you ask for it per job.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/jobhub init
.venv/bin/jobhub smoke          # checks `claude -p` structured output works
```

Then fill in **`profile/profile.md`** (prose: background, what you have built, what you want — this is the only
thing a model reads) and **`profile/profile.yaml`** (term, locations, dealbreakers). Both are editable from the
web UI's Profile page.

## Daily use

```sh
jobhub serve                    # http://127.0.0.1:8765
jobhub run                      # ingest -> score -> digest, capped
```

The web UI is a left rail — **Home · Jobs · Applications · Profile · Watchlist · Engine** — with **Run** at its foot.

| | |
|---|---|
| **Home** | what arrived since your last visit, what's open, what's in flight, what closes in 14 days |
| **Jobs** | bucket tabs, one search box, filters in a drawer; ⚡ deep-evals selected postings |
| **Applications** | everything you've acted on, grouped by where it stands |
| **Profile** | the constraints that decide what gets scored — editable, with a re-score warning on the ones that matter |
| **Engine** | the pipeline stage by stage, what's holding jobs back, live log |
| **Settings** | 14 themes, scoring thresholds, token budgets |

Themes were built in OKLCH and are checked against WCAG AA plus a contrast floor on card edges; the choice is
stored in the database, so the CLI and browser agree. The setting survives browsers and reinstalls.

To run it every morning: `scripts/install_launchd.sh 7 30` (macOS launchd; logs to `data/logs/`).

## CLI

```sh
jobhub ingest [--fetch-limit N]     # read sources, dedupe, fetch descriptions   (free)
jobhub score  [--budget N]          # local score, then capped Haiku triage of the unclear band
jobhub score  --calibrate           # how the local scorer agrees with your deep evaluations (free)
jobhub deep 123 456                 # full rubric on specific jobs; --top 20 --bucket likely
jobhub digest                       # writes digests/YYYY-MM-DD.md
jobhub show --bucket likely         # or reach / wildcard; `jobhub show <id>` for one
jobhub status <id> applied          # new | shortlisted | applied | interview | rejected | offer | skipped
jobhub rescore --carry-over         # after a change that only *widens* acceptance
jobhub rescore --reprefilter        # apply a tightened deterministic rule to jobs already scored (--dry-run first)
jobhub discover                     # Claude researches companies matching your profile
jobhub companies resolve            # find ATS boards for companies missing one
```

## How companies get watched

1. **Aggregator lists** — the broadest source, read every run. Ingest also searches GitHub weekly for new ones.
2. **Harvest** — any company whose posting URL reveals a Greenhouse/Lever/Ashby board is added automatically and
   polled from then on. This is the main breadth lever, and it costs nothing.
3. **Seeds** in `profile/companies.yaml`, or the Watchlist page.
4. **Discovery** — `jobhub discover` researches companies via web search. Those with a readable board are approved.

Breadth is deliberately cheap: a board with no matching roles is one HTTP call per run, and the filtering happens
downstream. Supported boards are Greenhouse, Lever, Ashby, Workday (`workday:tenant/wd/site`) and SmartRecruiters.
Known gap: Oracle HCM pages (JPMC, Amex) are JS-rendered and can't be fetched, so those postings stay text-less and
are scored on their title alone rather than being dropped.

## Tuning

- **Thresholds and budgets** are in `profile.yaml` (`fast_scoring:`, `llm:`) and on the Settings page. Changing one
  never re-evaluates anything — `jobhub rescore --recompute-only` re-buckets instantly.
- **Model-visible constraints** (term, locations, work authorization, and `profile.md`) change the profile hash, so
  everything is re-scored. That's free. When a change only *widens* acceptance, `jobhub rescore --carry-over`
  keeps the deep evaluations you already paid for.
- **Two work terms.** `term` + `term_also_accept` is the primary track; `alt_terms` is a second one, scored the
  same way but shown under its own chip and digest section. Postings that state no term — most board postings —
  appear under both. Empty `alt_terms` to go back to one.
- **Degree** (`degrees:` in `profile.yaml`, or the Profile page): the degrees you hold or are pursuing. The highest
  is a ceiling — a posting is archived only when the *lowest* degree it accepts is above it, so "Intern, PhD,
  Quantitative SWE" and "MS/PhD" go while "BS/MS" and "BS, MS or PhD" stay. A PhD mentioned under "Preferred
  qualifications", in a pay band, or as "a plus" is not a requirement. **Graduation dates and year of study are
  never a reason to reject.** Free and not model-visible: changing it re-scores nothing, and
  `jobhub rescore --recompute-only` applies it to jobs already scored. Empty the list to switch it off.
- Editing `prompts/` or `schemas/` means bumping `RUBRIC_VERSION` in `jobhub/config.py`, then `jobhub rescore`.

## Repo layout

```
jobhub/            pipeline: ingest, prefilter, fastscore, evaluate, digest, home
jobhub/web/        Flask UI (app.py, themes.py, static/, templates/)
profile/           your profile.md, profile.yaml, companies.yaml
prompts/ schemas/  what the model is asked and what it must return
data/ digests/     SQLite DB, logs and generated digests (gitignored)
```

**Note:** `jobhub/web/templates/` is listed in `.gitignore`. The ten templates already committed stay tracked
(gitignore doesn't apply retroactively), but new ones won't be picked up — and because `pyproject.toml` ships
`web/templates/*.html` as package data, any template that isn't committed will be missing from a fresh clone or an
install from a built distribution. Run `git check-ignore -v <file>` if a template seems to vanish.

## Tests

```sh
.venv/bin/python -m pytest -q
```
