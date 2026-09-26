"""Where every job currently sits in the pipeline, and what (if anything) is holding jobs back.

Backs the web UI's Pipeline page. Pure reads: each stage reports counts, the knobs that govern it, and a list of
`limits` — concrete reasons some jobs are not ingested / scored / refined right now, each with the setting or action
that lifts it. Nothing here is hidden in a log: if a cap is leaving jobs on a coarser score, it shows up here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config, db, fastscore

POLLABLE = ("greenhouse", "lever", "ashby", "workday")


#: `meta.last_score.reason` values that mean the AI pass was cut short (as opposed to switched off), and what to tell the user.
_INTERRUPTIONS = {
    "subscription usage limit hit": ("Scoring was interrupted because your Claude usage limit was reached.",
                                     "Run Score again once your limit resets."),
    "run cap": ("Scoring stopped at your per-run token cap.", "Run Score again, or raise the cap in Settings."),
    "weekly cap": ("Scoring stopped at your weekly token cap.", "Run Score again next week, or raise the cap in Settings."),
}


def refine_warning(last_score: dict[str, Any], awaiting: int) -> str | None:
    """A message when the last score run was cut short and postings are still on their free score only, else None."""
    cut = _INTERRUPTIONS.get(last_score.get("reason") or "")
    if not cut or not awaiting:
        return None
    return f"{cut[0]} {awaiting:,} unclear posting{'s are' if awaiting != 1 else ' is'} showing the free score only. {cut[1]}"


def _q(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def funnel(conn: sqlite3.Connection, phash: str) -> dict[str, int]:
    """Every active job counted once, by the latest evaluation it has under the current profile."""
    rows = conn.execute(
        """SELECT CASE WHEN e.id IS NULL THEN 'unscored'
                       WHEN e.model = 'prefilter' THEN 'rules'
                       WHEN e.model = 'triage' THEN 'triage'
                       WHEN e.model = 'local' THEN 'local_' || COALESCE(json_extract(e.raw, '$.fast.band'), 'no')
                       ELSE 'deep' END k, COUNT(*) n
           FROM jobs j LEFT JOIN evaluations e ON e.job_id = j.id AND e.profile_hash = ?
                AND e.id = (SELECT MAX(e2.id) FROM evaluations e2 WHERE e2.job_id = j.id AND e2.profile_hash = ?)
           WHERE j.active = 1 GROUP BY 1""", (phash, phash)).fetchall()
    out = {"unscored": 0, "rules": 0, "local_no": 0, "local_ambiguous": 0, "local_yes": 0, "triage": 0, "deep": 0}
    for r in rows:
        out[r["k"]] = out.get(r["k"], 0) + r["n"]
    return out


def pipeline_status(conn: sqlite3.Connection) -> dict[str, Any]:
    profile = config.load_profile()
    llm, fs = profile.llm, profile.fast_scoring
    phash = config.profile_hash()

    # ---- stage 0/1: sources and ingest
    repos = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM repo_sources GROUP BY status")}
    polled = _q(conn, "SELECT COUNT(*) FROM companies WHERE status = 'approved' AND ats_token IS NOT NULL AND ats_type IN (%s)"
                % ",".join("?" * len(POLLABLE)), *POLLABLE)
    open_by_source = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM jobs WHERE active = 1 GROUP BY source")}
    lists = [{"repo": r["repo"], "kind": r["kind"], "status": r["status"], "origin": r["source"], "checked": r["last_checked"],
              "error": r["last_error"], "postings": open_by_source.get(f"{r['kind']}:{r['repo']}", 0)}
             for r in conn.execute("SELECT * FROM repo_sources WHERE status != 'disabled' ORDER BY status = 'broken', last_jobs DESC")]
    boards = []
    for r in conn.execute("SELECT * FROM companies WHERE status = 'approved' AND ats_token IS NOT NULL AND ats_type IN (%s) ORDER BY name COLLATE NOCASE"
                          % ",".join("?" * len(POLLABLE)), POLLABLE):
        key = f"{r['ats_type']}:{r['ats_token'].split('/')[0] if r['ats_type'] == 'workday' else r['ats_token']}"
        boards.append({"slug": r["slug"], "name": r["name"], "kind": r["ats_type"], "token": r["ats_token"], "postings": open_by_source.get(key, 0)})
    last_ingest = conn.execute("SELECT * FROM runs WHERE kind IN ('ingest', 'run') ORDER BY id DESC LIMIT 1").fetchone()
    active = _q(conn, "SELECT COUNT(*) FROM jobs WHERE active = 1")
    desc = {
        "text": _q(conn, "SELECT COUNT(*) FROM jobs WHERE active = 1 AND description_text IS NOT NULL AND description_text != ''"),
    }

    # ---- stage 2: prefilter
    reasons = conn.execute(
        """SELECT hard_reject_reason r, COUNT(*) n FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model = 'prefilter' AND j.active = 1 GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", (phash,)).fetchall()

    # ---- stage 3/4: local score + refinement
    fun = funnel(conn, phash)
    awaiting = fastscore.count_ambiguous(conn, phash)
    last_score = db.uj(db.get_meta(conn, "last_score"), {}) or {}
    week = db.tokens_used_since(conn, 7)
    unread_rows = _q(conn, """SELECT COUNT(*) FROM evaluations e JOIN jobs j ON j.id = e.job_id WHERE e.profile_hash = ?
                              AND e.model IN ('local', 'triage') AND j.active = 1 AND json_extract(e.raw, '$.description_unavailable') = 1""", phash)
    deep = _q(conn, "SELECT COUNT(DISTINCT job_id) FROM evaluations WHERE profile_hash = ? AND hard_reject_reason IS NULL "
                    "AND model NOT IN ('prefilter', 'local', 'triage')", phash)

    # ---- stage 6: what the user sees
    buckets = {r["bucket"]: r["n"] for r in conn.execute(
        """SELECT e.bucket, COUNT(*) n FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND j.active = 1
             AND e.id = (SELECT MAX(e2.id) FROM evaluations e2 WHERE e2.job_id = j.id AND e2.profile_hash = ?) GROUP BY 1""", (phash, phash))}

    pending_score = db.count_pending_fast(conn, phash)
    limits: list[dict[str, Any]] = []

    def limit(level: str, stage: str, text: str, action: str, **kw: Any) -> None:
        limits.append({"level": level, "stage": stage, "text": text, "action": action, **kw})

    if pending_score:
        limit("warn", "score", f"{pending_score} readable postings have no score yet.", "Run Score.", kind="score")
    warning = refine_warning(last_score, awaiting)
    if warning:
        limit("warn", "score", warning, "", kind="refine")
    elif awaiting:
        reason = last_score.get("reason") or "not yet refined"
        limit("info", "score", f"{awaiting} ambiguous postings are showing their local score only ({reason}).",
              "The next Score run will refine them.", kind="refine")
    if not warning and llm.weekly_token_budget and week >= llm.weekly_token_budget:
        limit("warn", "triage", f"The weekly token cap ({llm.weekly_token_budget:,}) is used up, so no ambiguous posting is refined.",
              "Raise or clear llm.weekly_token_budget (0 = off).")
    if not llm.triage_enabled:
        limit("info", "triage", "llm.triage_enabled is off: ambiguous postings are never refined by a model.", "Set llm.triage_enabled: true.")

    return {
        "phash": phash,
        "sources": {"repos": repos, "polled": polled, "lists": lists, "boards": boards,
                    "last_ingest": dict(last_ingest) if last_ingest else None},
        "ingest": {"active": active, "desc": desc},
        "prefilter": {"count": fun["rules"], "reasons": [(r["r"], r["n"]) for r in reasons]},
        "score": {"pending": pending_score, "no": fun["local_no"], "ambiguous": fun["local_ambiguous"], "yes": fun["local_yes"],
                  "unread": unread_rows, "hi": fs.hi, "lo": fs.lo},
        "triage": {"awaiting": awaiting, "done": fun["triage"], "enabled": llm.triage_enabled, "run_cap": llm.run_token_budget,
                   "weekly_cap": llm.weekly_token_budget, "week_used": week, "last": last_score, "warning": warning,
                   "batch": llm.triage_batch_size, "min_score": llm.triage_min_score, "model": llm.triage_model},
        "deep": {"count": deep},
        "output": {"buckets": buckets},
        "funnel": fun,
        "limits": limits,
    }
