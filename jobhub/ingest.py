"""Ingestion: sync watchlist, pull sources, dedupe/upsert jobs, fetch missing descriptions."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

import httpx

from . import config, db, fetch, prefilter
from .models import RawJob
from .deadlines import extract_deadline
from .normalize import canonical_url, dedup_key, slugify, text_hash
from .sources.ashby import AshbySource
from .sources.base import Source, looks_like_internship
from .sources.greenhouse import GreenhouseSource
from .sources.lever import LeverSource
from .sources.simplify import SimplifySource
from .sources.workday import WorkdaySource
from .sources.github_readme import GithubReadmeSource

ATS_SOURCES = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource,
               "workday": WorkdaySource}


def sync_companies_yaml(conn: sqlite3.Connection) -> int:
    """Manual seeds from profile/companies.yaml become approved companies."""
    data = config.load_companies_yaml()
    n = 0
    for c in data.get("companies") or []:
        name = c.get("name")
        if not name:
            continue
        ats_type = ats_token = None
        if c.get("ats"):
            ats_type, _, ats_token = str(c["ats"]).partition(":")
        db.upsert_company(
            conn, slug=c.get("slug") or slugify(name), name=name, source="manual", status="approved",
            tier=c.get("tier"), rationale=c.get("notes"), ats_type=ats_type or None, ats_token=ats_token or None,
            domain=c.get("domain"), careers_url=c.get("careers_url"),
        )
        n += 1
    conn.commit()
    return n


# Workday's public job URL carries the whole tenant/wd/site triple the API needs
# (https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/...), and the site segment is the one
# thing slug-guessing cannot recover. An optional locale segment ("/en-US/") sits before the site on some
# tenants. This is why harvesting beats probing for Workday: the answer is already in postings we hold.
_WORKDAY_RE = re.compile(
    r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkday(?:jobs|site)\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")

_BOARD_RES = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)/"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/"),
}

_HARVEST_LIKE = ("%greenhouse.io/%", "%lever.co/%", "%ashbyhq.com/%",
                 "%myworkdayjobs.com/%", "%myworkdaysite.com/%")


def board_from_url(url: str) -> tuple[str, str] | None:
    """(ats_type, ats_token) implied by a public job URL, or None."""
    m = _WORKDAY_RE.search(url or "")
    if m:
        tenant, wd, site = m.groups()
        if site.lower() not in ("job", "jobs", "details"):   # guard against a missing site segment
            return "workday", f"{tenant}/{wd}/{site}"
    for ats, rx in _BOARD_RES.items():
        m = rx.search(url or "")
        if m:
            return ats, m.group(1)
    return None


def harvest_boards(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> int:
    """Broad, token-free coverage: any company that has an eligible posting whose URL reveals a Greenhouse/Lever/Ashby
    board gets that board added to the watchlist (approved, source='harvest'), so its whole board is polled from now on."""
    rows = conn.execute(
        """SELECT DISTINCT j.company_name, j.canonical_url FROM jobs j
           LEFT JOIN companies c ON c.id = j.company_id
           WHERE j.active = 1 AND (j.description_text IS NULL OR j.description_text != '')
             AND (c.id IS NULL OR c.ats_token IS NULL)
             AND (""" + " OR ".join("j.canonical_url LIKE ?" for _ in _HARVEST_LIKE) + ")", _HARVEST_LIKE
    ).fetchall()
    added = 0
    seen: set[str] = set()
    for r in rows:
        slug = slugify(r["company_name"])
        if slug in seen:
            continue
        hit = board_from_url(r["canonical_url"])
        if hit:
            existing = db.get_company_by_slug(conn, slug)
            if existing is not None and existing["status"] in ("rejected", "paused"):
                continue
            db.upsert_company(conn, slug=slug, name=r["company_name"], source=existing["source"] if existing else "harvest",
                              status=existing["status"] if existing else "approved", ats_type=hit[0], ats_token=hit[1])
            seen.add(slug)
            added += 1
    conn.commit()
    if added:
        log(f"harvested {added} company boards from job URLs (now polled in full)")
    return added


def sync_repo_sources(conn: sqlite3.Connection) -> None:
    """profile.yaml repos are the seed; discovered repos live only in the DB."""
    profile = config.load_profile()
    for repo in profile.sources.simplify_repos:
        db.upsert_repo_source(conn, repo, kind="simplify", source="config")
    for repo in profile.sources.readme_repos:
        db.upsert_repo_source(conn, repo, kind="readme", source="config")
    conn.commit()


def build_sources(conn: sqlite3.Connection) -> list[Source]:
    profile = config.load_profile()
    sync_repo_sources(conn)
    sources: list[Source] = []
    for r in db.list_repo_sources(conn, status="active"):
        sources.append(SimplifySource(r["repo"]) if r["kind"] == "simplify" else GithubReadmeSource(r["repo"]))
    for c in db.list_companies(conn, status="approved"):
        cls = ATS_SOURCES.get(c["ats_type"] or "")
        if cls and c["ats_token"]:
            sources.append(cls(c["ats_token"], c["name"]))
        elif profile.sources.enable_generic and c["careers_url"]:
            from .sources.generic import GenericPageSource

            sources.append(GenericPageSource(c["careers_url"], c["name"]))
    return sources


def upsert_raw_job(conn: sqlite3.Connection, job: RawJob, company_ids: dict[str, int]) -> tuple[int, bool]:
    """Insert or update a job. Returns (job_id, is_new)."""
    curl = canonical_url(job.url)
    key = dedup_key(job.company_name, job.title, job.location)
    existing = db.get_job_by_dedup(conn, key) or (db.get_job_by_url(conn, curl) if curl else None)
    company_id = company_ids.get(slugify(job.company_name))
    desc = job.description_text
    dhash = text_hash(desc)
    if existing is None:
        jid = db.insert_job(
            conn, dedup_key=key, company_id=company_id, company_name=job.company_name, title=job.title,
            location=job.location, canonical_url=curl, source=job.source, external_id=job.external_id,
            posted_at=job.posted_at, active=int(job.active), terms=job.terms or None, sponsorship=job.sponsorship,
            description_text=desc, description_hash=dhash, deadline=extract_deadline(desc), raw=job.raw or None,
        )
        db.add_job_url(conn, jid, job.url)
        if curl != job.url:
            db.add_job_url(conn, jid, curl)
        return jid, True
    updates: dict[str, Any] = {"last_seen_at": db.now(), "active": int(job.active)}
    if company_id and not existing["company_id"]:
        updates["company_id"] = company_id
    if job.terms and not existing["terms"]:
        updates["terms"] = job.terms
    if job.sponsorship and not existing["sponsorship"]:
        updates["sponsorship"] = job.sponsorship
    if desc and dhash != existing["description_hash"]:
        # Prefer board-API text over page-scraped text, but only treat it as a *change* (worth re-scoring)
        # when the same source reports different text — different extractors of one posting differ trivially.
        if existing["description_hash"] is None or existing["source"] == job.source:
            updates.update(description_text=desc, description_hash=dhash)
            if not existing["deadline"]:
                updates["deadline"] = extract_deadline(desc)
            if existing["description_hash"] is not None:
                updates["needs_reeval"] = 1
    db.update_job(conn, existing["id"], **updates)
    db.add_job_url(conn, existing["id"], job.url)
    return int(existing["id"]), False


def run_ingest(conn: sqlite3.Connection, *, fetch_limit: int | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """fetch_limit: None = fetch every pending description (daily default); 0 = skip fetching; N = at most N."""
    stats: dict[str, Any] = {"companies_synced": 0, "sources": 0, "jobs_seen": 0, "jobs_new": 0, "descriptions_fetched": 0,
                             "fetch_failures": 0, "deactivated": 0, "errors": []}
    stats["companies_synced"] = sync_companies_yaml(conn)
    stats["boards_harvested"] = harvest_boards(conn, log)
    from .sources.repo_discovery import discover_repos

    year = int(re.search(r"20\d\d", config.load_profile().term).group(0)) if re.search(r"20\d\d", config.load_profile().term) else 2027
    try:
        stats["repos_discovered"] = discover_repos(conn, year, log)
    except Exception as e:  # never let repo discovery break ingest
        stats["errors"].append(f"repo discovery: {str(e)[:120]}")
    company_ids = {c["slug"]: int(c["id"]) for c in db.list_companies(conn)}
    for src in build_sources(conn):
        try:
            jobs = src.fetch()
        except (httpx.HTTPError, ValueError) as e:
            stats["errors"].append(f"{src.name}: {str(e)[:200]}")
            log(f"source failed: {src.name}: {str(e)[:120]}")
            if src.name.startswith(("simplify:", "readme:")):
                db.upsert_repo_source(conn, src.name.split(":", 1)[1], kind=None, source="config", status="broken",
                                      last_checked=db.now(), last_error=str(e)[:200])
            continue
        if src.name.startswith(("simplify:", "readme:")):
            db.upsert_repo_source(conn, src.name.split(":", 1)[1], kind=None, source="config", status="active",
                                  last_checked=db.now(), last_jobs=len(jobs), last_error=None)
        stats["sources"] += 1
        seen_ids: list[int] = []
        new = 0
        for job in jobs:
            if not job.url or not job.title:
                continue
            if src.complete_listing and not looks_like_internship(job.title):
                continue  # full company boards include every role; keep internships only
            jid, is_new = upsert_raw_job(conn, job, company_ids)
            seen_ids.append(jid)
            new += int(is_new)
        if src.complete_listing and len(seen_ids) >= 5:  # a near-empty result is a parse/outage problem, not mass closure
            placeholders = ",".join("?" * len(seen_ids))
            cur = conn.execute(
                f"UPDATE jobs SET active = 0 WHERE source = ? AND active = 1 AND id NOT IN ({placeholders})",
                [src.name] + seen_ids,
            )
            stats["deactivated"] += cur.rowcount
        conn.commit()
        stats["jobs_seen"] += len(seen_ids)
        stats["jobs_new"] += new
        log(f"{src.name}: {len(seen_ids)} jobs, {new} new")

    # Metadata-only prefilter (term, location) before spending fetches on postings we'd reject anyway.
    profile = config.load_profile()
    phash = config.profile_hash()
    from .evaluate import store_hard_reject

    # Jobs prefiltered under an older profile/rubric (e.g. the target term changed) get re-queued for re-checking.
    requeued = conn.execute(
        """UPDATE jobs SET description_text = NULL WHERE active = 1 AND description_text = '' AND NOT EXISTS (
               SELECT 1 FROM evaluations e WHERE e.job_id = jobs.id AND e.rubric_version = ? AND e.profile_hash = ?)""",
        (config.RUBRIC_VERSION, phash),
    ).rowcount
    if requeued:
        stats["requeued_after_profile_change"] = requeued

    for row in db.jobs_needing_description(conn):
        reason = prefilter.check(profile, title=row["title"], location=row["location"] or "",
                                 terms=db.uj(row["terms"], None), description=None, sponsorship=row["sponsorship"])
        if reason:
            store_hard_reject(conn, row["id"], reason, "prefilter", phash)
            db.update_job(conn, row["id"], description_text="", description_hash=None)  # never fetch
            stats["prefiltered"] = stats.get("prefiltered", 0) + 1
    conn.commit()

    # Fetch descriptions for jobs that arrived without one (aggregators). Newest first, bounded per run.
    todo = [] if fetch_limit == 0 else db.jobs_needing_description(conn, limit=fetch_limit)
    if todo:
        log(f"fetching descriptions for {len(todo)} jobs (rate-limited per host; this can take several minutes)")
    for i, row in enumerate(todo, 1):
        if i % 25 == 0:
            log(f"  fetched {i}/{len(todo)} ({stats['fetch_failures']} failed so far)")
        text = fetch.fetch_description(row["canonical_url"])
        if text:
            already_scored = conn.execute("SELECT 1 FROM evaluations WHERE job_id = ? LIMIT 1", (row["id"],)).fetchone() is not None
            db.update_job(conn, row["id"], description_text=text, description_hash=text_hash(text), needs_reeval=int(already_scored),
                          deadline=row["deadline"] or extract_deadline(text))
            stats["descriptions_fetched"] += 1
        else:
            stats["fetch_failures"] += 1
            # Store an empty-marker so we don't retry forever; the evaluator skips jobs with no text.
            db.update_job(conn, row["id"], raw=_mark_fetch_failed(row))
        conn.commit()
    log(f"descriptions: {stats['descriptions_fetched']} fetched, {stats['fetch_failures']} failed")
    return stats


def _mark_fetch_failed(row: sqlite3.Row) -> dict[str, Any]:
    raw = db.uj(row["raw"], {}) or {}
    raw["fetch_failures"] = int(raw.get("fetch_failures", 0)) + 1
    return raw
