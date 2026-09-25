# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

JobHub is a local internship search engine: deterministic Python pipeline (ingest → dedupe → prefilter → fast score →
digest) where Claude is called only for judgment (triaging the ambiguous postings, on-demand deep evaluation,
discovering companies). **Model spend is capped by design** (see "Fast scoring" below): the goal is to never hit the
usage limit, with visibility of everything in the target space preferred over precise scoring. `plan.md` is the original
brief; `README.md` has the user-facing commands. Not a git repo (yet).

**Hard constraint: no per-token API spend.** Every model call goes through headless Claude Code (`claude -p`) on the
user's subscription via `jobhub/llm/claude_code.py`. Do not add `anthropic` SDK calls unless the user asks; if you do,
put them behind the `LLMBackend` protocol in `jobhub/llm/base.py`.

## Commands

```sh
.venv/bin/pip install -e ".[dev]"        # Python 3.12 venv at .venv (no uv on this machine)
.venv/bin/python -m pytest -q            # unit tests (normalize, prefilter, scoring/buckets)
.venv/bin/python -m pytest -q tests/test_prefilter.py::test_sponsorship_field   # single test
.venv/bin/jobhub smoke                   # verifies `claude -p --json-schema` round-trip (needs `claude` logged in)
.venv/bin/jobhub run                     # ingest -> fast score -> digest (writes digests/YYYY-MM-DD.md); capped tokens
.venv/bin/jobhub run --no-model          # same, 0 tokens: local scoring only
.venv/bin/jobhub score --calibrate       # free: how the local scorer agrees with existing deep evaluations
.venv/bin/jobhub score --budget 100000   # fast score with a one-off token cap
.venv/bin/jobhub deep 123 456            # on-demand full rubric for specific jobs; --top 20 --bucket likely for the best fast-scored
.venv/bin/jobhub ingest --fetch-limit 20 # cheap way to exercise sources; evaluate --limit N for model calls
.venv/bin/jobhub serve --port 8799 --no-open-browser   # local Flask UI (jobhub/web/); curl pages to smoke-test
.venv/bin/jobhub skills --limit 24        # mine aspirational postings for what to learn -> digests/skills.md
.venv/bin/jobhub triage --dry-run         # calibrate the stage-1 cut before enabling llm.triage_enabled
```

The Flask dev server has **no reloader** — restart `jobhub serve` after editing `jobhub/web/`. Page
templates extend `base.html`, whose slot is `{% block body %}` (not `content`), and `base.html` renders
header counters from `stats`, so every page route must pass `stats=_stats(conn)`.

`jobhub evaluate` / `discover` spend the user's subscription quota (~25–30s per batch of 6 jobs). Use `--limit`
when testing. `jobhub smoke` is the cheap sanity check.

## Architecture (read this before touching scoring or storage)

- **Two-layer profile.** `profile/profile.yaml` is read by code (hard filters, weights, thresholds, LLM settings);
  `profile/profile.md` is prose read by the model. `config.model_visible_constraints()` defines the *subset* of the
  YAML the model sees; `config.profile_hash()` hashes only that plus `profile.md`. Consequence: editing weights or
  thresholds never triggers re-evaluation (`jobhub rescore --recompute-only` re-buckets locally); editing constraints
  or the prose does. Changing `prompts/` or `schemas/` requires bumping `RUBRIC_VERSION` in `jobhub/config.py`.
- **Fast scoring (default flow, `jobhub/fastscore.py`).** Every posting gets a free deterministic 0-100 score from
  role type (`roletype.classify`), `target_domains` and software keywords in the title / first 600 chars, company
  reputation and title red flags; all weights are `profile.yaml: fast_scoring`. The score has three bands: `<= lo`
  clear no, `>= hi` clear yes, between is *ambiguous*. Only the ambiguous band goes to Haiku triage
  (`run_triage`, title + ~500 chars), final = `triage_weight*triage + (1-triage_weight)*local`, bucketed by
  `likely_min` / `reach_min`. Rows are ordinary `evaluations` rows with `model='local'` or `'triage'`; the score is stored
  as both `likelihood` and `desirability` (interest NULL) so sorting, digest and UI work unchanged, and `raw.fast` keeps
  `{band, local, triage, role}`. Ambiguous jobs that the budget couldn't reach keep their local row and are triaged on
  the next run (`ambiguous_rows`). **Budgets** only bound the *refinement*, never ingest or scoring (the user's
  priority is showing every job over saving tokens): `llm.run_token_budget` (per run, 0 = none) and the optional
  `llm.weekly_token_budget` (rolling 7 days from `runs`; **off by default**) cap triage; `run_triage(budget_tokens=...)`
  runs batches in waves and stops before the cap. What a cap leaves unrefined is never silent: `meta.last_score` records
  the count and reason, and the Jobs banner and Pipeline page show it with "Refine" / "Refine all, no cap" buttons
  (`jobhub score --budget 0` = uncapped). A posting with no readable description is never a clear "no" locally
  (`fast_scoring.unread_to_triage`) unless its title is off-target.
  Fast scoring only touches jobs with *no* evaluation for the current `profile_hash` (`db.jobs_pending_fast`), so it can
  never overwrite a deep row. `recompute_fast` (in `run`, `rescore`) re-applies changed `fast_scoring` knobs to stored
  rows for free; `jobhub score --calibrate` reports recall against existing deep evaluations. Calibrated 2026-09-25
  (hi 64 / lo 36): ~13-16% of jobs land ambiguous; ~14% of the rubric's Likely/Reach have a low local score (generic
  titles), which is the accepted cost of not sending everything to a model. Do not put fast rows through
  `composite()` — `recompute_buckets` and `carry_over_evaluations` exclude `local`/`triage` models for that reason.
- **Pipeline page** (`/pipeline`, `jobhub/pipeline.py`): the stages with live counts, a funnel bar of where every open job
  sits, a "what is holding jobs back" list (each with the action that lifts it), run forms, and in-place editing of the
  whitelisted non-model-visible knobs (`config.EDITABLE_KNOBS` / `set_profile_value` rewrite `profile.yaml` keeping
  comments; model-visible keys are deliberately not editable there because they change `profile_hash`).
- **Deep evaluation is on demand only** (`jobhub deep`, the ⚡ button, "Deep eval selected"). `evaluate.deep_evaluate`
  runs the full rubric (`_evaluate_rows`, shared with the legacy `run_evaluation`) and its row replaces the fast row
  (same unique key). `jobhub evaluate` still exists as the legacy prefilter → triage → rubric path but is no longer
  part of `run`.
- **Discovery is manual and rotating.** `llm.discovery_max_angles` (3) angles per press via a `meta.discover_cursor`,
  default model `sonnet` at `medium` effort, and ATS resolution uses the free probe unless `--model-resolve`. Discovery
  and `companies resolve` now record their spend in `runs` (kind `discover` / `resolve`) so the weekly budget sees them.
  (Discovery on the default frontier model with 13 angles is what exhausted the session limit on 2026-09-24.)
- **Two-stage evaluation (legacy, optional).** ~85% of what reaches the model gets archived, so `evaluate.run_triage()`
  can score postings first from title + ~500 chars on a small model and archive everything under
  `llm.triage_min_score`, storing the drop as an `evaluations` row with `model='triage'` (auditable and
  reversible like any other hard reject). Calibrate with `jobhub triage --dry-run`, which scores without
  writing and prints what it would have cut; a batch that errors passes through to full evaluation rather
  than being dropped. Measured 2026-09-04 over 1,274 pending jobs: **935 archived (73%)** at 214 in / 299 out
  per job against 818 / 960 for a full pass — a 45% saving, with the highest-scored drop at 34 against a
  threshold of 35 (nothing near the line). Triage runs concurrently (`llm.concurrency`) like `run_evaluation`.
- **Hard rejects are evaluations.** Prefilter rejects (`prefilter.py`, deterministic) and model rejects
  (`hard_reject_reason` in the model output) are both stored as `evaluations` rows with `bucket='archive'` and
  `model='prefilter'` or the model name — so nothing is re-sent and the digest can count reasons. Ingest runs a
  metadata-only prefilter (term, sponsorship, location) *before* fetching descriptions and marks those jobs with
  `description_text = ''` (empty string = "never fetch"; `NULL` = "needs fetch"). After `db.FETCH_GIVE_UP` failed
  fetches a `NULL` job is still evaluated, on metadata alone, and flagged `description_unavailable` ("unread" in the digest) —
  postings from sites that block scraping (Tesla, Oracle HCM) must never be silently dropped.
- **Two work-term tracks.** `profile.yaml: alt_terms` (added 2026-09-09 for Summer 2027 alongside Winter/Spring)
  is a second acceptable term. The model sees one flat `acceptable_term_labels` list — it must not hard-reject the
  alternate term — and the split happens in code: `prefilter.term_track()` labels each posting `primary` / `alt` /
  `unknown` from its structured `terms` or its title, and `prefilter.in_track()` decides visibility. `unknown` means
  the posting names no term (the common case: boards carry no term metadata) *or* names both, and those show under
  **both** tracks rather than being hidden from one — hiding them would empty the tabs. The web UI adds a chip row
  (`?term=primary|alt|all`, orthogonal to the bucket tabs) and the digest gets an alternate-term section.
  Because `acceptable_term_labels` is model-visible, adding an alt term changes `profile_hash` and would re-score
  everything; `evaluate.carry_over_evaluations()` (`jobhub rescore --carry-over`) re-stamps existing rows onto the
  new hash instead, skipping prefilter rows (free to recompute) and term-shaped hard rejects (must be re-judged).
  It saved 2,089 re-evaluations on 2026-09-09. Only ever use it for a change that *widens* acceptance.
- **Scores are composed in code.** The model returns sub-scores only (`schemas/evaluation.json`,
  `models.EvaluationOutput`); `evaluate.composite()` and `evaluate.assign_bucket()` compute likelihood/desirability/
  interest and the Likely/Reach/Wildcard/Archive bucket from `profile.yaml` weights. Company reputation comes from
  the `companies` table (`db.company_reputation`), not from the model's job evaluation. The wildcard cap is applied
  at digest time.
- **Dedup.** `normalize.dedup_key()` = sha1(company slug | stemmed title minus season/intern noise | sorted location);
  `canonical_url()` strips tracking params. Both are checked in `ingest.upsert_raw_job()`. A changed description hash
  sets `needs_reeval`.
- **Sources** implement `sources/base.Source` (`fetch() -> list[RawJob]`, `complete_listing` flag). **Workday**
  (`sources/workday.py`) covers the large-caps the other three miss (Intel, NXP, Cadence, GlobalFoundries, KLA,
  Salesforce): there is no board API, but the careers site is a thin client over
  `POST /wday/cxs/{tenant}/{site}/jobs`. The `site` segment varies per tenant and a wrong one returns 422, so the
  company's `ats_token` is the full triple `tenant/wd/site` (`intel/wd1/External`); `workday.probe_workday()`
  discovers it by trying common names. It sets `complete_listing=False` — the endpoint is *searched*, not
  enumerated, so an absent job is not proof it closed. Some tenants (Broadcom) ignore `searchText` and return the
  whole board, hence `MAX_EMPTY_PAGES` stops paging once results stop containing internships.
  Full ATS boards (Greenhouse/Lever/Ashby) set `complete_listing=True`: they're filtered to internship titles
  (`looks_like_internship`) and jobs absent from a fetch are marked inactive. The Simplify aggregator carries its own
  `active`/`terms`/`sponsorship` fields. `fetch.fetch_description()` prefers ATS JSON endpoints (Greenhouse, Lever,
  Ashby, Workday `wday/cxs`, SmartRecruiters) over page scraping.
- **Aggregator repos** are stored in `repo_sources` (seeded from `profile.yaml`, extended weekly by
  `sources/repo_discovery.py` via the GitHub search API — unauthenticated, throttled to <10 req/min; `meta.repo_search_at`
  gates the weekly run). A repo is enabled only if it yields ≥15 parsable postings; fetch failures mark it `broken`.
- **Harvesting beats probing.** `ingest.board_from_url()` recovers `(ats_type, ats_token)` from a posting's public
  URL, and `harvest_boards()` applies it to every company that lacks a board. For Workday this matters more than
  for the others: the `site` segment (`NVIDIAExternalCareerSite`) is unguessable, but it is sitting verbatim in job
  URLs the aggregators already gave us — one ingest turned 135 unpollable companies into polled boards, 107 of them
  Workday, with zero probing. Prefer extending `board_from_url` over widening `probe_*` slug guesses. The `/blindspots`
  page lists what remains, separating companies with *zero* postings seen (true blind spots, worth checking by hand)
  from those still arriving via aggregators.
- **Company breadth is cheap; be generous.** A board with no matching roles costs one HTTP call per ingest, and
  filtering happens downstream in the prefilter and rubric — so the watchlist is deliberately broad (118 companies,
  98 with boards as of 2026-09-03) rather than curated to the user's niche. New companies are added by probing
  slugs deterministically (`fetch.probe_ats` for GH/Lever/Ashby, `workday.probe_workday` for Workday) — zero model
  tokens. **Board postings carry no term metadata** (447 of 465 had none): unlike the Simplify aggregator's
  `season` field, boards expose only a title, so term filtering falls back to the title and the rubric. Adding
  boards therefore widens coverage but does *not* improve Winter/Spring precision.
- **Company watchlist.** `companies.status` gates polling: only `approved` companies' boards are ingested.
  `ingest.harvest_boards()` runs every ingest and auto-approves any company whose job URL reveals a Greenhouse/Lever/
  Ashby board (`source='harvest'`) — zero-token breadth; `discover.resolve_ats()` auto-approves discovered companies
  once a board is found. "Proposed" therefore only means "known, no readable board".
  `discover.py` proposes companies (web research via `claude -p --tools WebSearch,WebFetch`) and `resolve_ats()`
  probes Greenhouse/Lever/Ashby with slug guesses before asking the model. Jobs from unknown companies that land in
  Likely/Reach also propose the company (`source='seen_in_jobs'`).
- **Token economics** (re-measured 2026-09-02; the fast-score flow above supersedes the per-run picture: run 41 on
  2026-09-24 cost ~2.6M tokens under the old full-rubric flow, and the new flow's model spend is bounded by the budgets). Evaluation is ~all of the spend; ingest/dedupe/prefilter are free and
  discovery is a few calls a week. Per `claude -p` call: ~7K Claude Code scaffolding + ~3.9K system prompt
  (of which profile.md is ~2.7K) + the job blocks; no prompt-cache reuse across calls. The levers, in order of
  measured effect:
  1. **`llm.model`** — this was `null`, inheriting the Claude Code default, so 850 postings/run were scored by a
     frontier model. Now `"haiku"`: scoring against a fixed rubric is a small-model job. `llm.discovery_model` is
     kept separate (sentinel `CLAUDE_DEFAULT` in `llm/base.py` = omit `--model`) so web research never inherits it.
  2. **`MAX_DESC_CHARS`** 5500 -> 2500 plus `evaluate.trim_description()`, which drops the EEO/benefits tail
     (84/120 sampled postings carry one; 20% of all description text sits after it).
  3. **`llm.batch_size`** 12 -> 24, amortizing the ~11K fixed per-call cost over twice as many jobs.
  4. **`llm.effort`** medium -> low (~60% of output tokens were thinking).
  `runs.llm_*_tokens` record per-run totals; compare runs to see whether a change actually paid.
- **Headless Claude details** (`llm/claude_code.py`): `--system-prompt` replaces Claude Code's default prompt (saves
  ~5K tokens/call); structured output arrives in the `structured_output` field; `--bare` must NOT be used (it skips
  keychain auth); the subprocess runs in `data/llm_cwd` so no CLAUDE.md is auto-loaded; `CLAUDECODE` is stripped from
  the env so it works when launched from inside a Claude Code session.

- **Skills mining** (`jobhub/skills.py`, `jobhub skills`): the inverse of evaluation — for postings the candidate
  *cannot* get (GPU, compilers, performance, HFT, systems, per `profile.yaml: target_domains`), extract what they
  require, aggregate a demand table into `skill_demands`, and synthesize one ordered learning plan (stored in
  `meta.skills_plan`, rendered to `digests/skills.md` and `/skills`). Selection is keyword-based and free;
  postings are ranked reach/wildcard first, then by keyword density, and syndicated duplicates are dropped.
  Keywords are matched with alnum lookarounds, not `\b` (keywords like `c++` end in punctuation) — without that,
  `hip` matched inside `internship` and tagged every posting as GPU work. A skill's `status` is the most
  pessimistic judgement any posting gave it. `target_domains` is not in `model_visible_constraints`, so editing it
  never triggers re-evaluation.
- **Role types** (`jobhub/roletype.py`): the Role type chip row (`?role=quant|gpu|robotics|perf|ml|systems|hardware|general`)
  and card badge. The rubric has no category field, so this is a free, deterministic classifier computed per page
  load from stored data: a known trading firm (`QUANT_FIRMS`) is always quant; otherwise the title decides when it
  matches anything (ties go to the earlier, more specific type); only a title with no hits falls back to
  `fit_tags` + summary. Descriptions are skipped (they mention everything, and are slow to scan). Tune keywords
  freely: nothing is re-evaluated.
- **Application limits** (`jobhub/applimits.py`): companies that cap applications ("limited to three (3)
  applications within a 30-day period") get a `max N apps · K used` badge on cards and job pages, warn-coloured
  once your applied/interview/rejected/offer count reaches N; the tooltip is the exact sentence. Regex over
  descriptions, keyed by `slugify(company_name)` (most aggregator jobs have no `company_id`), with `ALIASES` for
  one company under two names (`flyzipline` -> `zipline`). "One application per role" is deliberately not a
  cap. Cached in-process until the jobs table changes (~0.8s cold scan).
- **Web UI** (`jobhub/web/app.py`, Flask + Jinja, no build step): reads/writes the same SQLite DB; long commands are
  launched as `python -m jobhub.cli <cmd>` subprocesses (one at a time) with logs under `data/logs/web-*.log`,
  polled by `/api/tasks/<id>/log`. Templates in `jobhub/web/templates/`, assets in `jobhub/web/static/`.

## Known gaps

- Oracle HCM job pages (JPMC, Amex) can't be fetched; their REST finder syntax rejected every variant tried.
- SimplifyJobs renames its repo each cycle; old names redirect to the same data — list only one repo in
  `profile.yaml`.
