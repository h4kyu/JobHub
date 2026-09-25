"""SQLite persistence (stdlib sqlite3). Schema is created idempotently by init_db()."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    domain TEXT,
    tier INTEGER,
    reputation_score INTEGER,
    fit_score INTEGER,
    work_areas TEXT,            -- JSON list
    rationale TEXT,
    ats_type TEXT,              -- greenhouse | lever | ashby | workday | other
    ats_token TEXT,
    careers_url TEXT,
    source TEXT NOT NULL,       -- manual | discover | seen_in_jobs
    status TEXT NOT NULL,       -- approved | proposed | rejected | paused
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    company_id INTEGER REFERENCES companies(id),
    company_name TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    canonical_url TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    terms TEXT,                 -- JSON list
    sponsorship TEXT,
    description_text TEXT,
    description_hash TEXT,
    needs_reeval INTEGER NOT NULL DEFAULT 0,
    deadline TEXT,              -- ISO date if the posting states an application deadline
    raw TEXT                    -- JSON
);
CREATE INDEX IF NOT EXISTS idx_jobs_canonical_url ON jobs(canonical_url);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);

CREATE TABLE IF NOT EXISTS job_urls (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    url TEXT NOT NULL,
    PRIMARY KEY (job_id, url)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    rubric_version TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    hard_reject_reason TEXT,
    likelihood INTEGER,
    desirability INTEGER,
    interest INTEGER,
    sub_scores TEXT,            -- JSON
    bucket TEXT NOT NULL,       -- likely | reach | wildcard | archive
    wildcard_reason TEXT,
    red_flags TEXT,             -- JSON list
    summary TEXT,
    raw TEXT,                   -- JSON
    UNIQUE (job_id, rubric_version, profile_hash)
);
CREATE INDEX IF NOT EXISTS idx_eval_job ON evaluations(job_id);

CREATE TABLE IF NOT EXISTS applications (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL,
    notes TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_sources (
    repo TEXT PRIMARY KEY,      -- owner/name
    kind TEXT,                  -- simplify (listings.json) | readme (markdown table)
    stars INTEGER,
    description TEXT,
    status TEXT NOT NULL,       -- active | disabled | broken
    source TEXT NOT NULL,       -- config | github_search
    added_at TEXT NOT NULL,
    last_checked TEXT,
    last_jobs INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS skill_demands (
    id INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL,
    skill TEXT NOT NULL,
    category TEXT,
    status TEXT,                -- have | partial | missing (vs the candidate profile)
    domains TEXT,               -- JSON list of target-domain names it was demanded in
    required_count INTEGER NOT NULL DEFAULT 0,
    preferred_count INTEGER NOT NULL DEFAULT 0,
    demand_count INTEGER NOT NULL DEFAULT 0,
    job_ids TEXT,               -- JSON list
    companies TEXT,             -- JSON list of example company names
    evidence TEXT               -- JSON list of short quotes
);
CREATE INDEX IF NOT EXISTS idx_skill_demands_gen ON skill_demands(generated_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    jobs_seen INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_evaluated INTEGER DEFAULT 0,
    llm_calls INTEGER DEFAULT 0,
    llm_ms INTEGER DEFAULT 0,
    llm_input_tokens INTEGER DEFAULT 0,
    llm_output_tokens INTEGER DEFAULT 0,
    llm_cache_read_tokens INTEGER DEFAULT 0,
    errors TEXT                 -- JSON list
);
"""

_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN deadline TEXT",
    "ALTER TABLE runs ADD COLUMN llm_input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN llm_output_tokens INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN llm_cache_read_tokens INTEGER DEFAULT 0",
]


FETCH_GIVE_UP = 2  # description fetch attempts before a job is evaluated on metadata alone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    if own:
        conn.close()


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def j(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


def uj(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


# ---------- runs ----------

def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, now()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, **stats: Any) -> None:
    errors = stats.pop("errors", None)
    sets = ", ".join(f"{k} = ?" for k in stats)
    params: list[Any] = list(stats.values())
    sql = "UPDATE runs SET finished_at = ?, errors = ?" + (", " + sets if sets else "") + " WHERE id = ?"
    conn.execute(sql, [now(), j(errors)] + params + [run_id])


# ---------- companies ----------

def get_company_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE slug = ?", (slug,)).fetchone()


def upsert_company(conn: sqlite3.Connection, *, slug: str, name: str, source: str, status: str, **fields: Any) -> int:
    existing = get_company_by_slug(conn, slug)
    ts = now()
    if "work_areas" in fields:
        fields["work_areas"] = j(fields["work_areas"])
    if existing is None:
        cols = ["slug", "name", "source", "status", "created_at", "updated_at"] + list(fields)
        vals = [slug, name, source, status, ts, ts] + list(fields.values())
        cur = conn.execute(
            f"INSERT INTO companies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )
        return int(cur.lastrowid)
    # Update only provided non-null fields; never demote an approved company from a proposal.
    updates = {k: v for k, v in fields.items() if v is not None}
    if status == "approved" or existing["status"] not in ("approved", "rejected"):
        updates["status"] = status
    updates["updated_at"] = ts
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", list(updates.values()) + [existing["id"]])
    return int(existing["id"])


def list_companies(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM companies WHERE status = ? ORDER BY tier, name", (status,)).fetchall()
    return conn.execute("SELECT * FROM companies ORDER BY status, tier, name").fetchall()


def set_company_status(conn: sqlite3.Connection, slug: str, status: str) -> bool:
    cur = conn.execute("UPDATE companies SET status = ?, updated_at = ? WHERE slug = ?", (status, now(), slug))
    return cur.rowcount > 0


def company_reputation(conn: sqlite3.Connection, company_id: int | None, default: int) -> int:
    if company_id is None:
        return default
    row = conn.execute("SELECT tier, reputation_score FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        return default
    if row["reputation_score"] is not None:
        return int(row["reputation_score"])
    if row["tier"] is not None:
        return {1: 90, 2: 70, 3: 55}.get(int(row["tier"]), default)
    return default


# ---------- jobs ----------

def get_job_by_dedup(conn: sqlite3.Connection, dedup_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()


def get_job_by_url(conn: sqlite3.Connection, canonical_url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE canonical_url = ?", (canonical_url,)).fetchone()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def insert_job(conn: sqlite3.Connection, **fields: Any) -> int:
    ts = now()
    fields.setdefault("first_seen_at", ts)
    fields.setdefault("last_seen_at", ts)
    for k in ("terms", "raw"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = j(fields[k])
    cols = list(fields)
    cur = conn.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(fields.values())
    )
    return int(cur.lastrowid)


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    for k in ("terms", "raw"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = j(fields[k])
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", list(fields.values()) + [job_id])


def add_job_url(conn: sqlite3.Connection, job_id: int, url: str) -> None:
    conn.execute("INSERT OR IGNORE INTO job_urls (job_id, url) VALUES (?, ?)", (job_id, url))


def jobs_pending_evaluation(conn: sqlite3.Connection, rubric_version: str, profile_hash: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Active jobs with no evaluation for the current rubric/profile, or flagged for re-eval.
    Includes jobs whose description could not be fetched after FETCH_GIVE_UP attempts: they are evaluated
    on metadata alone rather than silently dropped (see evaluate.job_block).
    Token rule: a rubric (prompt/format) change only re-scores jobs that are currently visible — anything already
    archived or rejected under the *same profile* by an older rubric stays put. A profile change re-scores everything."""
    sql = f"""
        SELECT j.* FROM jobs j
        WHERE j.active = 1
          AND ((j.description_text IS NOT NULL AND j.description_text != '')
               OR (j.description_text IS NULL AND COALESCE(json_extract(j.raw, '$.fetch_failures'), 0) >= {FETCH_GIVE_UP}))
          AND NOT EXISTS (
                SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.profile_hash = ? AND e.rubric_version != ?
                  AND (e.bucket = 'archive' OR e.hard_reject_reason IS NOT NULL) AND j.needs_reeval = 0)
          AND (j.needs_reeval = 1 OR NOT EXISTS (
                SELECT 1 FROM evaluations e
                WHERE e.job_id = j.id AND e.rubric_version = ? AND e.profile_hash = ?))
        ORDER BY j.first_seen_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (profile_hash, rubric_version, rubric_version, profile_hash)).fetchall()


def jobs_pending_fast(conn: sqlite3.Connection, profile_hash: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Active, readable jobs with no evaluation of any rubric version under the current profile: the queue for the
    free fast scorer. Unlike jobs_pending_evaluation, a job that already has a (deep or fast) row is never
    returned, so fast scoring can't overwrite a deep evaluation. Jobs that could not be fetched after
    FETCH_GIVE_UP attempts are included and scored from metadata alone, like the deep path does."""
    sql = f"""
        SELECT j.* FROM jobs j
        WHERE j.active = 1
          AND ((j.description_text IS NOT NULL AND j.description_text != '')
               OR (j.description_text IS NULL AND COALESCE(json_extract(j.raw, '$.fetch_failures'), 0) >= {FETCH_GIVE_UP}))
          AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.profile_hash = ?)
        ORDER BY j.first_seen_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (profile_hash,)).fetchall()


def count_pending_fast(conn: sqlite3.Connection, profile_hash: str) -> int:
    """Same predicate as jobs_pending_fast, without loading rows: the header counter runs on every page view."""
    return int(conn.execute(f"""
        SELECT COUNT(*) FROM jobs j WHERE j.active = 1
          AND ((j.description_text IS NOT NULL AND j.description_text != '')
               OR (j.description_text IS NULL AND COALESCE(json_extract(j.raw, '$.fetch_failures'), 0) >= {FETCH_GIVE_UP}))
          AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.profile_hash = ?)""", (profile_hash,)).fetchone()[0])


def tokens_used_since(conn: sqlite3.Connection, days: float, kind: str | None = None) -> int:
    """Input + output tokens recorded in `runs` over the trailing window (cache reads excluded)."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = "SELECT COALESCE(SUM(COALESCE(llm_input_tokens, 0) + COALESCE(llm_output_tokens, 0)), 0) FROM runs WHERE started_at >= ?"
    params: list[Any] = [cutoff]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return int(conn.execute(sql, params).fetchone()[0])


def tokens_by_kind(conn: sqlite3.Connection, days: float) -> list[sqlite3.Row]:
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return conn.execute(
        """SELECT kind, COUNT(*) runs, COALESCE(SUM(llm_calls), 0) calls,
                  COALESCE(SUM(llm_input_tokens), 0) inp, COALESCE(SUM(llm_output_tokens), 0) outp
           FROM runs WHERE started_at >= ? GROUP BY kind ORDER BY inp + outp DESC""", (cutoff,)).fetchall()


def reset_fetch_failures(conn: sqlite3.Connection) -> int:
    """Give every job whose description could not be fetched another set of attempts (see FETCH_GIVE_UP)."""
    cur = conn.execute(
        """UPDATE jobs SET raw = json_set(COALESCE(raw, '{}'), '$.fetch_failures', 0)
           WHERE active = 1 AND description_text IS NULL AND COALESCE(json_extract(raw, '$.fetch_failures'), 0) > 0""")
    return cur.rowcount


def jobs_needing_description(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = f"""SELECT * FROM jobs WHERE active = 1 AND description_text IS NULL
             AND COALESCE(json_extract(raw, '$.fetch_failures'), 0) < {FETCH_GIVE_UP}
             ORDER BY (company_id IS NULL), first_seen_at DESC"""  # watchlist companies first, then newest
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


# ---------- evaluations ----------

def insert_evaluation(conn: sqlite3.Connection, **fields: Any) -> int:
    fields.setdefault("created_at", now())
    for k in ("sub_scores", "red_flags", "raw"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = j(fields[k])
    cols = list(fields)
    cur = conn.execute(
        f"INSERT OR REPLACE INTO evaluations ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(fields.values()),
    )
    conn.execute("UPDATE jobs SET needs_reeval = 0 WHERE id = ?", (fields["job_id"],))
    return int(cur.lastrowid)


def latest_evaluations(conn: sqlite3.Connection, rubric_version: str, profile_hash: str, bucket: str | None = None, include_handled: bool = False) -> list[sqlite3.Row]:
    """Latest evaluation per job for the current rubric/profile, joined with job + application status."""
    sql = """
        SELECT e.*, j.company_name, j.title, j.location, j.canonical_url, j.company_id, j.posted_at, j.deadline,
               j.first_seen_at, j.active, j.terms, COALESCE(a.status, 'new') AS app_status, a.notes AS app_notes, c.tier AS tier
        FROM evaluations e
        JOIN jobs j ON j.id = e.job_id
        LEFT JOIN applications a ON a.job_id = j.id
        LEFT JOIN companies c ON c.id = j.company_id
        WHERE e.profile_hash = ? AND j.active = 1
          AND e.id = (SELECT e2.id FROM evaluations e2 WHERE e2.job_id = j.id AND e2.profile_hash = ?
                      ORDER BY (e2.rubric_version = ?) DESC, e2.created_at DESC LIMIT 1)
    """
    params: list[Any] = [profile_hash, profile_hash, rubric_version]
    if bucket:
        sql += " AND e.bucket = ?"
        params.append(bucket)
    if not include_handled:
        sql += " AND COALESCE(a.status, 'new') = 'new'"
    sql += " ORDER BY e.desirability DESC, e.likelihood DESC"
    return conn.execute(sql, params).fetchall()


def evaluation_for_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM evaluations WHERE job_id = ? ORDER BY created_at DESC LIMIT 1", (job_id,)
    ).fetchone()


def hard_reject_counts(conn: sqlite3.Connection, rubric_version: str, since: str | None = None) -> list[tuple[str, int]]:
    """Counts by reason. Model reasons are free text, so group them under 'model: ...' -> 'model'."""
    sql = """
        SELECT CASE WHEN hard_reject_reason LIKE 'model:%' THEN 'model (see jobhub show <id>)' ELSE hard_reject_reason END AS r,
               COUNT(*) FROM evaluations WHERE rubric_version = ? AND hard_reject_reason IS NOT NULL
    """
    params: list[Any] = [rubric_version]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " GROUP BY r ORDER BY 2 DESC"
    return [(r[0], r[1]) for r in conn.execute(sql, params).fetchall()]


# ---------- applications ----------

def set_status(conn: sqlite3.Connection, job_id: int, status: str, notes: str | None = None) -> None:
    conn.execute(
        """INSERT INTO applications (job_id, status, notes, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET status = excluded.status,
               notes = COALESCE(excluded.notes, applications.notes), updated_at = excluded.updated_at""",
        (job_id, status, notes, now()),
    )
    if status == "closed":
        conn.execute("UPDATE jobs SET active = 0 WHERE id = ?", (job_id,))


# ---------- repo sources / meta ----------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def upsert_repo_source(conn: sqlite3.Connection, repo: str, *, kind: str | None, source: str, status: str = "active", **fields: Any) -> None:
    existing = conn.execute("SELECT * FROM repo_sources WHERE repo = ?", (repo,)).fetchone()
    if existing is None:
        cols = ["repo", "kind", "source", "status", "added_at"] + list(fields)
        vals = [repo, kind, source, status, now()] + list(fields.values())
        conn.execute(f"INSERT INTO repo_sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return
    updates = {k: v for k, v in fields.items() if v is not None}
    if kind:
        updates["kind"] = kind
    if existing["status"] != "disabled":
        updates["status"] = status
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE repo_sources SET {sets} WHERE repo = ?", list(updates.values()) + [repo])


def list_repo_sources(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM repo_sources WHERE status = ? ORDER BY stars DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM repo_sources ORDER BY status, stars DESC").fetchall()
