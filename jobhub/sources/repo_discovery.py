"""Find new GitHub internship-aggregator repos automatically (weekly), probe their format, and enable the usable ones.
Zero model tokens: GitHub search API (unauthenticated, 10 req/min) + the same parsers used for ingest."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from .. import db
from .base import client
from .github_readme import GithubReadmeSource
from .simplify import SimplifySource

SEARCH_INTERVAL_DAYS = 7
MIN_STARS = 40
MAX_DAYS_SINCE_PUSH = 45
MIN_JOBS_TO_ENABLE = 15


def _queries(year: int) -> list[str]:
    """Aggregator repos are overwhelmingly named for the Summer cycle, so a Winter/Spring (Jan-Apr)
    target term has to be searched for explicitly — measured 2026-09-03, 94% of in-window postings came
    from the `season` field of two Summer-named repos, with the seven README repos contributing 14
    in-window postings between them. Repos name that cycle inconsistently ("Spring2027", "Winter-2027",
    "off-cycle"), hence both spaced and concatenated forms."""
    return [
        f"internships {year} in:name",
        f"internships {year} in:name,description canada",
        f"co-op {year} in:name,description",
        f"internships {year - 1} in:name stars:>300",  # last cycle's repos often carry off-season postings
        "quant internships in:name,description",
        "off-season internships in:name,description",
        "new grad internships tech in:name,description stars:>200",
        # --- off-cycle (Jan-Apr) coverage ---
        f"spring {year} internships in:name,description",
        f"winter {year} internships in:name,description",
        f"spring{year} in:name",
        f"winter{year} in:name",
        "off-cycle internships in:name,description",
    ]


def search_candidates(year: int, log: Callable[[str], None] = print) -> list[dict]:
    seen: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_DAYS_SINCE_PUSH)
    with client(timeout=30) as c:
        for q in _queries(year):
            try:
                r = c.get("https://api.github.com/search/repositories", params={"q": q, "sort": "stars", "per_page": 15},
                          headers={"Accept": "application/vnd.github+json"})
            except httpx.HTTPError as e:
                log(f"  github search failed: {str(e)[:80]}")
                continue
            if r.status_code != 200:
                log(f"  github search {r.status_code}: {r.text[:80]}")
                if r.status_code == 403:
                    break  # rate limited; try again next week
                continue
            for it in r.json().get("items", []):
                if it["stargazers_count"] < MIN_STARS:
                    continue
                pushed = datetime.fromisoformat(it["pushed_at"].replace("Z", "+00:00"))
                if pushed < cutoff:
                    continue
                name = it["full_name"]
                text = f"{name} {it.get('description') or ''}".lower()
                if not re.search(r"intern|co-?op|new.?grad|placement|early.?career", text):
                    continue
                seen[name] = {"repo": name, "stars": it["stargazers_count"], "description": (it.get("description") or "")[:200]}
            time.sleep(7.0)  # stay under 10 req/min unauthenticated (12 queries -> ~9.6/min)
    return list(seen.values())


def probe_repo(repo: str) -> tuple[str | None, int, str | None]:
    """Return (kind, job_count, error). Tries the structured feed first, then the README table."""
    try:
        n = len(SimplifySource(repo).fetch())
        if n:
            return "simplify", n, None
    except (httpx.HTTPError, ValueError):
        pass
    try:
        n = len(GithubReadmeSource(repo).fetch())
        return ("readme" if n >= MIN_JOBS_TO_ENABLE else None), n, None
    except (httpx.HTTPError, ValueError) as e:
        return None, 0, str(e)[:120]


def discover_repos(conn: sqlite3.Connection, year: int, log: Callable[[str], None] = print, force: bool = False) -> int:
    last = db.get_meta(conn, "repo_search_at")
    if last and not force:
        if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(days=SEARCH_INTERVAL_DAYS):
            return 0
    log("searching GitHub for new internship aggregator repos…")
    known = {r["repo"] for r in db.list_repo_sources(conn)}
    added = 0
    for cand in search_candidates(year, log):
        if cand["repo"] in known:
            continue
        kind, n, err = probe_repo(cand["repo"])
        if kind:
            db.upsert_repo_source(conn, cand["repo"], kind=kind, source="github_search", status="active",
                                  stars=cand["stars"], description=cand["description"], last_jobs=n, last_checked=db.now())
            log(f"  + {cand['repo']} ({kind}, {n} postings, ★{cand['stars']})")
            added += 1
        else:
            db.upsert_repo_source(conn, cand["repo"], kind=None, source="github_search", status="broken",
                                  stars=cand["stars"], description=cand["description"], last_jobs=n, last_checked=db.now(),
                                  last_error=err or f"only {n} parsable postings")
    db.set_meta(conn, "repo_search_at", db.now())
    conn.commit()
    return added
