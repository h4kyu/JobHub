"""Daily digest: Markdown file with Likely / Reach / Wildcard sections."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from . import config, db, prefilter
from .models import Bucket


NEW_WINDOW_HOURS = 36


def _is_new(r: sqlite3.Row) -> bool:
    from datetime import datetime, timedelta, timezone

    try:
        return datetime.fromisoformat(r["created_at"]) >= datetime.now(timezone.utc) - timedelta(hours=NEW_WINDOW_HOURS)
    except (TypeError, ValueError):
        return False


def _row_line(r: sqlite3.Row) -> str:
    subs = db.uj(r["sub_scores"], {}) or {}
    flags = db.uj(r["red_flags"], []) or []
    loc = (r["location"] or "").replace("|", "/")
    title = (r["title"] or "").replace("|", "/")
    unread = " ⚠ **unread — check manually**" if (db.uj(r["raw"], {}) or {}).get("description_unavailable") else ""
    new = "🆕 " if _is_new(r) else ""
    raw = db.uj(r["raw"], {}) or {}
    summary = (r["summary"] or "").replace("\n", " ")
    if raw.get("fit_tags"):
        summary += " · ✓ " + ", ".join(raw["fit_tags"])
    if raw.get("gap_tags"):
        summary += " · ✗ " + ", ".join(raw["gap_tags"])
    if r["deadline"]:
        loc += f" · due {r['deadline']}"
    line = (f"| {r['job_id']} | {new}**{r['company_name']}** | [{title}]({r['canonical_url']}){unread} | {loc} "
            f"| {r['likelihood']} | {r['desirability']} | {'' if r['interest'] is None else r['interest']} | {summary}")
    if r["bucket"] == Bucket.wildcard.value and r["wildcard_reason"]:
        line += f" _Wildcard: {r['wildcard_reason']}_"
    if flags:
        line += f" ⚠ {'; '.join(flags)}"
    return line + " |"


def _table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "_none_\n"
    head = "| id | company | title | location | likely | desire | interest | notes |\n|---|---|---|---|---|---|---|---|\n"
    return head + "\n".join(_row_line(r) for r in rows) + "\n"


def _sorted_bucket(conn: sqlite3.Connection, rv: str, phash: str, bucket: Bucket) -> list[sqlite3.Row]:
    """Rows for one bucket, newly scored first and then by the score that defines the bucket."""
    key = {
        Bucket.likely: lambda r: ((r["likelihood"] or 0) * (r["desirability"] or 0),),
        Bucket.reach: lambda r: (r["desirability"] or 0, r["likelihood"] or 0),
        Bucket.wildcard: lambda r: (r["interest"] or 0,),
    }[bucket]
    rows = db.latest_evaluations(conn, rv, phash, bucket=bucket.value)
    rows.sort(key=lambda r: (_is_new(r), *key(r)), reverse=True)
    return rows


def build_digest(conn: sqlite3.Connection, stats: dict[str, Any] | None = None) -> str:
    profile = config.load_profile()
    phash = config.profile_hash()
    rv = config.RUBRIC_VERSION
    # Two work-term tracks when profile.alt_terms is set: the primary sections stay the target term, the
    # alternate term gets its own section. Postings that state no term at all appear in both (in_track).
    track = lambda r: prefilter.term_track(profile, r["title"], db.uj(r["terms"], []))
    keep = lambda rows, want: [r for r in rows if prefilter.in_track(track(r), want)]
    main = "primary" if profile.alt_terms else "all"
    cap = profile.fast_scoring.digest_cap or None   # fast scoring can fill Likely/Reach with hundreds of rows
    likely_all = keep(_sorted_bucket(conn, rv, phash, Bucket.likely), main)
    reach_all = keep(_sorted_bucket(conn, rv, phash, Bucket.reach), main)
    likely, reach = likely_all[:cap], reach_all[:cap]
    wild = keep(_sorted_bucket(conn, rv, phash, Bucket.wildcard), main)[: profile.buckets.wildcard.cap]
    proposed = db.list_companies(conn, status="proposed")
    rejects = db.hard_reject_counts(conn, rv)

    out = [f"# JobHub digest — {date.today().isoformat()}", ""]
    if stats:
        out.append("Run: " + ", ".join(f"{k}={v}" for k, v in stats.items() if not isinstance(v, (list, dict))))
        out.append("")
    n_new = sum(_is_new(r) for r in likely + reach + wild)
    terms = " / ".join([profile.term] + list(profile.term_also_accept))
    out.append(f"Term: {terms} · rubric v{rv} · profile {phash} · 🆕 = scored in the last {NEW_WINDOW_HOURS}h ({n_new}). "
               "Mark jobs with `jobhub status <id> applied|skipped|shortlisted` or in the web UI.")
    more = lambda shown, total: f"_…and {total - len(shown)} more in the web UI (digest_cap={cap})._\n" if len(shown) < total else ""
    out += ["", f"## Likely ({len(likely_all)}) — good odds of an interview", _table(likely), more(likely, len(likely_all))]
    out += [f"## Reach ({len(reach_all)}) — best experience/prestige, tougher odds", _table(reach), more(reach, len(reach_all))]
    out += [f"## Wildcard ({len(wild)}) — not an obvious match, worth a look", _table(wild)]
    if profile.alt_terms:
        alt_label = " / ".join(profile.alt_terms)
        alt_likely = keep(_sorted_bucket(conn, rv, phash, Bucket.likely), "alt")
        alt_reach = keep(_sorted_bucket(conn, rv, phash, Bucket.reach), "alt")
        out += ["", f"# {alt_label} — alternate term ({len(alt_likely) + len(alt_reach)})",
                f"_Scored on the same rubric. Postings that state no term are listed under both terms._", "",
                f"## Likely — {alt_label} ({len(alt_likely)})", _table(alt_likely),
                f"## Reach — {alt_label} ({len(alt_reach)})", _table(alt_reach)]
    if proposed:
        out.append(f"## Proposed companies ({len(proposed)}) — `jobhub companies approve <slug>`")
        for c in proposed[:30]:
            out.append(f"- `{c['slug']}` **{c['name']}** (tier {c['tier'] or '?'}, fit {c['fit_score'] or '?'}, rep {c['reputation_score'] or '?'}; via {c['source']}) — {(c['rationale'] or '').splitlines()[0][:160]}")
        out.append("")
    if rejects:
        out.append("## Hard rejects (all time, current rubric)")
        out += [f"- {reason}: {n}" for reason, n in rejects]
        out.append("")
    return "\n".join(out)


def write_digest(conn: sqlite3.Connection, stats: dict[str, Any] | None = None) -> Path:
    config.ensure_dirs()
    path = config.DIGESTS_DIR / f"{date.today().isoformat()}.md"
    path.write_text(build_digest(conn, stats))
    return path
