# TODO

Working notes for the consumer-product rework (2026-09-25). Not read by the tool.

## Company directory (design agreed)

Replace the mixed `companies` table with two layers plus scoring metadata:

- **Directory** (shared, maintained by us, ships in the repo): `company, aliases, ATS type, token, tags, verified_at, status`.
  Users never edit it or see a board link. `status` includes `no_feed` for known companies whose board we can't find or scrape;
  those rows stay so nobody re-probes them, but no stats about them are shown to users.
- **Watchlist** (per user): references directory entries by ID, so directory fixes reach every user.
  Onboarding pre-fills it from profile tags (GPU, quant, robotics, ...); the user adds/removes.
- Reputation/tier becomes a scoring input (likely folded into directory tags), not a user-facing concept.
- The `approved / proposed / rejected / paused` statuses go away: a company is on the watchlist or it isn't.

### Adding a company to the watchlist (frontend rules)

1. **In the directory:** pick it from a search/dropdown. Guaranteed to have a board we can poll.
2. **Known company, not in the directory:** the user may still add it, but its jobs come from aggregators only.
3. **Unknown company:** don't add anything to the directory (it may be too small to have a board, or a typo).
4. The directory must be large and diverse enough that case 2 is rare.

### Why (data, 2026-09-25)

Per-company board polling finds jobs aggregators miss: 216 of the 261 board-sourced Likely/Reach/Wildcard jobs (83%) had no
aggregator counterpart, across 92 companies (161 of them Workday). Boards supply about a third of the top-bucket jobs.
Caveat: only the first-seen source is stored per job, so overlap was inferred with fuzzy company+title matching.
A `job_sources(job_id, source, first_seen)` table would measure it exactly.

## Todos

- [ ] **Maintainer pipeline to update the directory periodically.** Consider a GitHub Action on a schedule that
  re-verifies every entry (flags dead or moved boards), probes slugs to add new candidates (zero model tokens), and opens a PR
  or commits the refreshed directory.
- [ ] **Prompt users to `git pull` when the directory updates.** Needs a way for the tool to know a newer directory exists
  (e.g. compare a directory version/date against the remote) and surface a nudge in the UI.
- [x] Export the current DB boards and `profile/companies.yaml` into the first directory, with tags
  (`scripts/build_directory.py` -> `directory/board_directory.csv`; 740 companies, 620 boards, 2026-09-25).
- [ ] Directory follow-ups: fix the 9 `dead` boards (e.g. Walmart, IDEXX, CMU Workday sites moved) and review the 8 `empty`;
  tags are coarse (ml and systems are each ~45% of entries, gpu only 4) and 111 companies are untagged, mostly `no_feed`.
  Consider a one-time small-model pass for industry/size tags, and grow the directory well past the ~740 seeds.
- [ ] Self-heal: when a board fails at poll time, re-probe that one company instead of breaking.
- [ ] Remove the "N companies have no job feed" banner and the approved/proposed UI from the main screens.
- [ ] Add `job_sources` table to measure aggregator/board overlap exactly.
- [x] Inventory every knob and page across the app: see `docs/knob-inventory.md` (verdicts are proposals; open questions at the bottom).
