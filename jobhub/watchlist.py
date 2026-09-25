"""A user's watchlist: the companies whose own careers pages we read directly, on top of the aggregator lists.

It is the set of `companies` rows with status 'approved' (paused = removed). Picking a directory entry approves its row
(creating one from the directory if needed) and hands the polling code its board; a company the directory doesn't have
but the job lists do carry can still be followed, and then arrives through the lists only. Anything else is refused:
it may be too small to have a board, or a typo.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db, directory
from .normalize import slugify

STATUS_ON, STATUS_OFF = "approved", "paused"


class NotFound(LookupError):
    """Not in the directory and not seen in any job list."""


def _approved(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM companies WHERE status = ?", (STATUS_ON,)).fetchall()


def _row_for_entry(conn: sqlite3.Connection, e: directory.Entry) -> sqlite3.Row | None:
    """The companies row already standing for this directory entry (under its slug, an alias, or the same board)."""
    for s in sorted(e.slugs, key=lambda s: s != e.slug):
        if row := db.get_company_by_slug(conn, s):
            return row
    if e.ats_token:
        return conn.execute("SELECT * FROM companies WHERE ats_type = ? AND lower(ats_token) = lower(?)",
                            (e.ats_type, e.ats_token)).fetchone()
    return None


def _entry_for_row(row: sqlite3.Row) -> directory.Entry | None:
    return directory.find(row["name"], slug=row["slug"], ats_type=row["ats_type"], ats_token=row["ats_token"])


def list_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in _approved(conn):
        e = _entry_for_row(r)
        out.append({"slug": r["slug"], "name": e.name if e else r["name"], "tags": list(e.tags) if e else []})
    out.sort(key=lambda w: w["name"].lower())
    return out


def watch_slugs(conn: sqlite3.Connection) -> set[str]:
    """Every slug a watchlisted company can appear under in a job's company name (its own plus directory aliases)."""
    slugs: set[str] = set()
    for r in _approved(conn):
        slugs.add(r["slug"])
        if e := _entry_for_row(r):
            slugs |= e.slugs
    return slugs


def add_directory(conn: sqlite3.Connection, slug: str) -> str:
    e = directory.get(slug)
    if e is None:
        raise NotFound(slug)
    board: dict[str, Any] = {"ats_type": e.ats_type, "ats_token": e.ats_token} if e.pollable else {}
    row = _row_for_entry(conn, e)
    extras = {"tier": e.tier, "reputation_score": e.reputation, "domain": e.domain or None, "careers_url": e.careers_url or None}
    if row is not None:
        db.upsert_company(conn, slug=row["slug"], name=row["name"], source=row["source"], status=STATUS_ON, **board, **extras)
        key = row["slug"]
    else:
        db.upsert_company(conn, slug=e.slug, name=e.name, source="directory", status=STATUS_ON, **board, **extras)
        key = e.slug
    conn.commit()
    return key


def add_known(conn: sqlite3.Connection, name: str) -> str:
    """Follow a company the directory lacks but the job lists carry. Refuses names no posting mentions."""
    if e := directory.find(name):
        return add_directory(conn, e.slug)
    spelled = conn.execute("SELECT company_name FROM jobs WHERE company_name = ? LIMIT 1", (name,)).fetchone()
    if spelled is None:
        raise NotFound(name)
    slug = slugify(name)
    db.upsert_company(conn, slug=slug, name=name, source="watchlist", status=STATUS_ON)
    conn.commit()
    return slug


def remove(conn: sqlite3.Connection, slug: str) -> bool:
    ok = db.set_company_status(conn, slug, STATUS_OFF)
    conn.commit()
    return ok


def _known_in_jobs(conn: sqlite3.Connection, q: str, limit: int) -> list[str]:
    """Company names seen in job lists that the directory doesn't have: the most common spelling per slug."""
    like = "%" + q.replace("%", "").replace("_", "") + "%"
    best: dict[str, tuple[int, str]] = {}
    for r in conn.execute("SELECT company_name, COUNT(*) n FROM jobs WHERE company_name LIKE ? GROUP BY company_name", (like,)):
        s = slugify(r["company_name"])
        if directory.get(s) or s == "unknown":
            continue
        if s not in best or r["n"] > best[s][0]:
            best[s] = (r["n"], r["company_name"])
    return [n for _, n in sorted(best.values(), key=lambda v: (-v[0], v[1].lower()))[:limit]]


def suggest(conn: sqlite3.Connection, q: str = "", tag: str | None = None, limit: int = 30) -> dict[str, Any]:
    """Directory matches for the search box (browse by field when only a tag is given), then job-list-only names."""
    watched = watch_slugs(conn)
    hits = directory.search(q, tag)
    results = [{"key": e.slug, "name": e.name, "tags": list(e.tags), "on": bool(e.slugs & watched), "in_directory": True}
              for e in hits[:limit]]
    more = max(0, len(hits) - limit)
    if q.strip() and len(q.strip()) >= 2 and not tag:
        for name in _known_in_jobs(conn, q.strip(), 8):
            results.append({"key": name, "name": name, "tags": [], "on": slugify(name) in watched, "in_directory": False})
    return {"results": results, "more": more}
