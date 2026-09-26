# Knob and page inventory

Audit of everything a user can currently see or set, with a proposed verdict for the consumer-product rework.
Written 2026-09-25 from the code as it stands. Nothing here is implemented yet; mark it up.

**Verdicts**

| Verdict | Meaning |
|---|---|
| **Expose** | Part of onboarding and/or a normal Settings screen, in plain language |
| **Advanced** | Available, but behind an "Advanced" section; sensible default |
| **Derive** | Computed from something the user already chose; no control |
| **Internal** | Stays a code constant or config default; not shown |
| **Remove** | Redundant, obsolete, or replaced by another design |

## The three big findings

1. **"What I want" is stored three separate ways.** `roles` (prose for the model), `target_domains` (keyword lists for the
   fast score and skills mining) and `fast_scoring.role_weights` (classifier weights in `roletype.py`). A user should pick
   their interests once, from one catalog, and all three should be derived from that pick.
2. **"How picky should it be" is spread over ~15 knobs and two bucket systems.** `fast_scoring.{hi,lo,likely_min,reach_min,triage_weight}`,
   `llm.triage_min_score`, and the deep-eval gates `buckets.*` all answer one question. It should be one control.
3. **AI spend is spread over ~12 knobs.** Budgets, batch sizes, concurrency, models and effort levels. It should be one
   choice ("AI help: off / light / standard / unlimited") plus a usage meter.

## Onboarding: the minimum a new user must answer

1. **Connect Claude.** Detect `claude` and login (today `jobhub smoke`). If absent, continue in local-only mode.
2. **What roles/fields?** Multi-select from one catalog (systems, ML/AI, robotics, GPU, quant, hardware, general software...) plus free text.
3. **When?** Work terms (one marked primary, others accepted) and minimum length.
4. **Where?** Locations, remote yes/no, "only show these locations" yes/no.
5. **Eligibility.** Work authorization and "do you need visa sponsorship?" (drives the citizenship dealbreakers); toggles for
   unpaid / clearance / PhD-only.
6. **About you.** Paste or upload a resume; this produces `profile.md` (the prose the model reads).
7. **Companies.** A pre-filled watchlist from the directory based on step 2; add/remove.
8. **AI help level** and, optionally, "update daily".

Everything else has a default and lives in Advanced or is internal.

## Profile: `profile/profile.yaml`

### Constraints the model sees (changing these re-scores everything)

| Knob | Verdict | Notes |
|---|---|---|
| `roles` | Expose | Multi-select from the shared catalog; also feeds `target_domains` and `role_weights` (see finding 1) |
| `term`, `term_also_accept`, `alt_terms` | Expose, merge | Three fields for one idea. Make it one list of accepted terms with one marked primary. The primary/alt track split is derived from that |
| `length_weeks` | Expose | Only the minimum matters to most people; max is Advanced |
| `locations`, `remote_ok`, `strict_location` | Expose | Locations list, remote toggle, "only these places" toggle |
| `work_authorization` | Expose, reframe | Ask "do you need sponsorship?" and derive the citizenship dealbreaker phrases from it. Free-text list becomes Advanced |

### Dealbreakers (free to change; deterministic prefilter)

| Knob | Verdict | Notes |
|---|---|---|
| `unpaid`, `clearance`, `citizenship`, `level` | Expose as toggles | "Hide unpaid", "Hide clearance required", "Hide citizen-only", "Hide PhD-only". The phrase lists stay Internal |
| `location_exclude`, `other` | Advanced | One free-text "hide postings containing..." box |
| `sponsorship_values` | Internal | Matches an aggregator field |
| `title_exclude` | Derive | Current defaults bake in this user's scope (no frontend, no consulting). Derive from selected roles; extras go to Advanced |
| `fast_scoring.negative_title` | Merge with `title_exclude` | Two overlapping lists for the same idea (reject vs penalize) |

### Scoring (deep evaluation only)

| Knob | Verdict | Notes |
|---|---|---|
| `scoring.likelihood.*`, `scoring.desirability.*`, `scoring.unknown_company_reputation` | Internal | Composite weights for on-demand deep eval. No user has an opinion about `skills_match: 0.65`. Optional: one "prefer well-known companies" slider |
| `buckets.likely/reach/wildcard.*` | Remove as user controls | Second bucket system, applied to deep evals only. Unify with the fast-score bands so there is one definition of Likely/Reach |

### AI usage (`llm.*`)

| Knob | Verdict | Notes |
|---|---|---|
| `model`, `effort`, `triage_model`, `triage_effort` | Internal | Cheap-model defaults are a product decision, not a user choice |
| `batch_size`, `triage_batch_size`, `triage_desc_chars`, `concurrency`, `timeout_seconds` | Internal | Performance tuning |
| `triage_enabled`, `run_token_budget`, `weekly_token_budget` | Expose as one control | "AI help": Off (local only) / Light / Standard / Unlimited, mapped to these three. Show a usage meter |
| `triage_min_score` | Internal | Floor for triage; duplicates the fast-score `lo` idea |
| `discovery_model`, `discovery_effort`, `discovery_max_angles` | Remove | Discovery is replaced by the shipped company directory |

### Fast scoring (`fast_scoring.*`)

| Knob | Verdict | Notes |
|---|---|---|
| `enabled` | Remove | Always on; "local only" is covered by the AI help level |
| `hi`, `lo`, `likely_min`, `reach_min`, `triage_weight` | Expose as one control | A single "How picky?" choice (Only strong matches / Balanced / Show me everything) that sets all five |
| `role_weights` | Derive | From the roles the user selected (optionally order them by preference) |
| `title_domain_points/cap`, `desc_domain_points/cap`, `desc_chars`, `software_title_bonus`, `software_desc_points/cap`, `negative_title_penalty`, `reputation_weight` | Internal | Calibrated constants (`jobhub score --calibrate` is a maintainer tool) |
| `software_keywords` | Internal | |
| `unread_to_triage` | Internal | Correct default; never surface |
| `digest_cap` | Remove | The UI shows everything; cap the digest in code |
| `target_domains` | Derive / Advanced | Keyword lists per field. Derive from selected roles; "fields I want to grow into" (skills mining) is an optional Advanced list |

### Sources (`sources.*`)

| Knob | Verdict | Notes |
|---|---|---|
| `simplify_repos`, `readme_repos` | Internal, maintained by us | Shipped list, auto-extended by weekly repo discovery. Possibly tie region-specific repos (e.g. Canadian) to the user's locations |
| `enable_generic` | Remove | Off by default, noisy, slow |

### Other files

| Item | Verdict | Notes |
|---|---|---|
| `profile/profile.md` | Expose (generated) | Onboarding produces it from a resume; editable as "About me" |
| `profile/companies.yaml` | Remove | Replaced by the shipped directory plus a per-user watchlist |
| `profile.yaml` / `profile.md` tracked in git | **Decision needed** | These hold one person's data. For a product, ship `profile.example.*` and gitignore the real files |
| Profile hash, rubric version (shown in the header of Settings) | Remove from UI | Implementation detail. Replace with "Changing this will re-score your jobs. Apply?" when a model-visible field changes |

## Web UI pages

| Page | Verdict | Notes |
|---|---|---|
| **Jobs** (`/`) | Keep, simplify | The core screen. See the table below |
| **Job detail** (`/job/<id>`) | Keep | |
| **Pipeline** (`/pipeline`) | Move to Advanced/diagnostics | Seven stages with ~16 inline knobs. Users need one line ("Updated 2h ago, 14 new") and a status strip, not the funnel. Keep the funnel for debugging |
| **Companies** (`/companies`) | Rebuild | Directory search + watchlist. No approved/proposed/stopped groups, no tiers, no "why" column, no Discover button |
| **Aggregator repos** table (on Companies) | Remove | Maintainer concern |
| **Blind spots** (`/blindspots`) | Remove | Directory `no_feed` rows are hidden by decision |
| **Skills** (`/skills`) | Removed 2026-09-25 | Was a personal what-to-learn report; code, prompts, schemas and tests deleted |
| **Runs** (`/runs`) | Removed 2026-09-25 | Pipeline page covers it: run/score/deep buttons, digests list and the live log moved there; the token meter in the header links to Settings |
| **Settings** (`/settings`) | Rebuild | Today it is a read-only dump of every YAML key. Becomes real, grouped, editable settings: Search, Eligibility, Companies, AI help, Notifications |
| **Digest view** | Keep, optional | Output, off by default |
| **Header stats** (open / scored / to score / unrefined / boards / token meter) | Remove | Implementation counters. Keep at most "N new" |
| **Task bar and live log** | Keep | Useful for the one "Update now" action |

### Jobs page detail

| Element | Verdict | Notes |
|---|---|---|
| Bucket tabs Likely/Reach/Wildcard/Archive/All | Keep, rename | Plain names ("Best matches", "Worth a look", "Wildcards", "Filtered out") |
| Status select (To review, shortlisted, applied...) | Keep | Application tracking is core |
| "needs manual check" | Keep, rename | "Couldn't read description" |
| Sort (best match / newest / role / deadline) | Keep | |
| Search | Keep | |
| Work-term chips | Keep | Only shown when a second term exists (already the behavior) |
| **"Scored by" chips** (Everything / Fast / Deep) | Remove | Implementation detail |
| Role-type chips | Keep | Real user value |
| Banner ("N unscored", Score now, Refine, Refine all no cap) | Remove buttons | Scoring runs automatically after ingest; leftover refinement is background. Show at most a quiet note |
| Deep eval selected, per-job ⚡ | Keep, rename | "Analyze this job". Hide token counts unless AI help is metered |
| Card: source badge (fast/triage/deep/rules) | Remove | |
| Card: `tier N` badge | Remove | Tier is a maintainer concept |
| Card: three scores (Likely / Desire / Interest, or Score / Triage / Local) | Simplify | One match score plus the reason tags |
| Card: `max N apps` badge | Keep | Niche but useful, non-obvious |
| Card: term badge, new badge, open link | Keep | |
| Footnote line explaining ⚡ and icons | Replace | Tooltips or first-run tour |

## CLI (`jobhub ...`)

| Command | Verdict | Notes |
|---|---|---|
| `init`, `serve`, `run`, `status`, `show`, `digest` | Keep | `init` and `serve` become the onboarding entry point |
| `smoke` | Fold into onboarding | "Connect Claude" step |
| `ingest`, `score`, `deep`, `skills` | Keep, Advanced | Building blocks the UI calls |
| `rescore` | Advanced | `--recompute-only` is automatic on weight edits; `--carry-over` is an expert tool |
| `evaluate` | Remove | Legacy path, already outside `run` |
| `triage --dry-run`, `score --calibrate` | Maintainer only | Calibration tools |
| `discover`, `companies approve/reject/add/resolve/list/show` | Remove / replace | Directory + watchlist commands instead (`companies add <name>`, `companies list`) |
| `sources list/search/enable/disable` | Maintainer only | |
| `scripts/install_launchd.sh` | Expose as "Update daily" | macOS-only today; needs a cross-platform story |

## Open questions

- Is the product single-user local (as now), or eventually multi-user hosted? This decides whether `profile.*` and the DB stay per-checkout files.
- Should the one "How picky?" control have three fixed presets or a slider? (Recommend three presets; calibration exists only at a few points.)
- Is "AI help: Off" a fully supported mode? Fast scoring already works without a model, so it should be.
- Do we keep the Wildcard bucket? Its cap and `min_interest` only apply to deep evals; with fast scoring it is rarely populated (15 active today).
