# JobHub

A local internship search engine: pulls postings from aggregators and company ATS boards, dedupes them,
hard-filters dealbreakers, has Claude score the rest against your profile, and writes a daily digest sorted into
**Likely** (good interview odds), **Reach** (best experience/prestige, tougher odds), and **Wildcard** (unexpected but
interesting). Company discovery researches which companies do the work you want and feeds a watchlist.

All model calls go through headless Claude Code (`claude -p`) on your subscription — no API key, no per-token bill.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/jobhub init
.venv/bin/jobhub smoke          # verifies `claude -p` structured output works
```

Then fill in **`profile/profile.md`** (background, experience, desired work — this drives every score) and
**`profile/profile.yaml`** (term, locations, dealbreakers, weights). Optionally seed `profile/companies.yaml`.

## Web UI

```sh
jobhub serve                    # opens http://127.0.0.1:8765
```

Jobs by bucket with filters and one-click status changes, job detail pages, the company watchlist
(approve/reject/add/discover), run buttons with a live log, run history, and rendered digests. It uses the same
database as the CLI, so the two can be mixed freely.

## Daily loop

```sh
jobhub run                      # ingest -> fast score -> digest  (digests/YYYY-MM-DD.md); model spend is capped
jobhub show --bucket likely     # or reach / wildcard; `jobhub show <id>` for full details
jobhub status <id> applied      # new | shortlisted | applied | interview | rejected | offer | skipped
scripts/install_launchd.sh 7 30 # schedule daily at 07:30 (macOS launchd)
```

Individual stages: `jobhub ingest [--fetch-limit N]` (default: fetch every pending description), `jobhub score [--budget N] [--no-model]`
(free local score, then Haiku triage of the ambiguous band under a token cap), `jobhub digest`.

**Fast scoring vs deep evaluation.** Every posting gets a free local score (role type, keywords, company, title red
flags); only the ambiguous middle goes to a cheap Haiku triage, and only up to `llm.run_token_budget` per run and
`llm.weekly_token_budget` per rolling week. The full rubric never runs automatically: use the ⚡ button in the UI,
`jobhub deep <id> ...`, or `jobhub deep --top 20 --bucket likely`. `jobhub score --calibrate` checks the local scorer
against existing deep evaluations. All the knobs are in `profile.yaml` (`fast_scoring:`, `llm:`) and listed at `/settings`.
`jobhub evaluate` is the old full-rubric path and is no longer part of `run`.

## Coverage: how companies get on the watchlist

0. **Aggregator repos (automatic, free).** GitHub internship lists are the broadest source. The configured ones live in
   `profile.yaml`; every week ingest also searches GitHub for new ones, probes their format, and enables those that
   parse (`jobhub sources list|search|disable|enable`).
1. **Harvest (automatic, free).** Every ingest scans the postings it already has: any company whose job URL reveals a
   Greenhouse/Lever/Ashby board is added and its whole board is polled from then on. This is the main breadth lever.
2. **Seeds** in `profile/companies.yaml` (tier + optional board).
3. **Discovery** (`jobhub discover`, or "Find more companies" in the UI) — Claude researches companies matching your
   profile via web search. Companies with a readable board are approved automatically; the rest are kept for reference.

## What to learn next

The evaluation pipeline answers "can I get this today". `jobhub skills` answers the opposite: for the roles
that are currently out of reach — GPU, compilers, performance, HFT, systems — what do the postings actually
require? It mines them whatever bucket they landed in (an archived job you have no chance at is exactly the
useful signal), aggregates a demand table, and writes one ordered learning plan.

```sh
jobhub skills                            # mine ~40 postings -> digests/skills.md, and the Skills tab in the UI
jobhub skills --limit 24 --no-plan       # demand table only, fewer calls
jobhub skills --bucket reach             # restrict to one bucket
jobhub skills --show                     # print the stored report without regenerating
```

Which fields count as "aspirational" is `target_domains` in `profile.yaml` — edit the keyword lists to steer it.
Editing them never triggers re-evaluation. Run it monthly, not per-run.

## Company discovery

```sh
jobhub discover                          # angles derived from profile; proposes companies with fit/reputation scores
jobhub discover --like "Company X"       # companies similar to one you like
jobhub discover --angle "robotics startups in Vancouver with intern programs"
jobhub companies list --status proposed
jobhub companies approve <slug> ...      # (discovered companies with a board are approved automatically)
jobhub companies add "Name" --ats greenhouse:token --tier 1
jobhub companies resolve                 # detect ATS boards for companies missing one
```

## Tuning

- Scoring weights and bucket thresholds live in `profile.yaml` → `jobhub rescore --recompute-only` re-buckets
  instantly with no model calls.
- Changing `profile.md` or the model-visible constraints (term, locations, work authorization, length) changes the
  profile hash → `jobhub rescore` re-evaluates stale jobs. When the change only *widens* what is acceptable
  (adding `alt_terms`, another location), `jobhub rescore --carry-over` re-stamps the scores you already paid for
  onto the new hash instead, and only genuinely new postings are sent to the model.
- **Two work terms.** `term` + `term_also_accept` is the primary track; `alt_terms` (e.g. `["Summer 2027"]`) is a
  second one, scored on the same rubric but shown under its own chip in the UI and its own digest section, so the
  two can be compared before committing. Postings that state no term at all — most company boards do not — are
  listed under both. Empty `alt_terms` to go back to a single-term search.
- Editing `prompts/` or `schemas/` → bump `RUBRIC_VERSION` in `jobhub/config.py`, then `jobhub rescore`.
- `llm.model` / `llm.effort` / `llm.batch_size` / `llm.concurrency` in `profile.yaml` control subscription usage.
  Evaluation defaults to `haiku` at `low` effort with batches of 24: it scores against a fixed rubric, which is a
  small-model job, and leaving `model: null` silently inherits your Claude Code default (a frontier model) for
  every posting. Discovery keeps its own `llm.discovery_model` so web research is unaffected.
- **Two-stage triage** (off by default): `llm.triage_*` adds a cheap first pass that archives obvious misses before
  the full rubric runs — worthwhile because ~85% of everything scored ends up archived. Calibrate it first:
  `jobhub triage --dry-run` scores pending jobs and prints what it *would* cut without writing anything. Enable
  with `llm.triage_enabled: true` once the cut looks right; drops are stored as ordinary archive rows.

## Sources

- SimplifyJobs internship repo (`.github/scripts/listings.json`) — structured: terms, sponsorship, active flag.
- Greenhouse, Lever, Ashby boards for approved companies (JSON APIs, internship titles only).
- Workday boards (Intel, NXP, Cadence, GlobalFoundries, KLA, Salesforce, ...) via the `wday/cxs` JSON endpoint the
  careers site itself uses. The board token is `workday:tenant/wd/site` (e.g. `workday:intel/wd1/External`).
- Description fetching understands Greenhouse, Lever, Ashby, Workday and SmartRecruiters URLs; other pages fall back
  to text extraction. Known gap: Oracle HCM (e.g. JPMC, Amex) is JS-rendered and its REST finder syntax varies
  per tenant, so those descriptions currently fail (jobs stay in the DB without text and are not evaluated).

## Tests

```sh
.venv/bin/python -m pytest -q
```
