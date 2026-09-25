#!/usr/bin/env python
"""Bootstrap the shared company directory (directory/board_directory.csv) from the local DB and profile/companies.yaml.

Maintainer tool. Zero model tokens: merges duplicate companies, derives tags from the internships already seen at each
company, and pings every board once to record whether it still answers.

    .venv/bin/python scripts/build_directory.py               # build + verify (about a minute)
    .venv/bin/python scripts/build_directory.py --no-verify   # offline: keep verified_at/status from the previous file

Columns: slug, name, aliases (|), ats_type, ats_token, status, jobs_at_check, tags (|), tier, reputation, domain,
careers_url, verified_at, notes.  status: ok | empty | dead | unverified | no_feed.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobhub import config, roletype  # noqa: E402
from jobhub.normalize import slugify  # noqa: E402
from jobhub.sources.workday import _endpoint, parse_token  # noqa: E402

OUT = ROOT / "directory" / "board_directory.csv"
COLUMNS = ["slug", "name", "aliases", "ats_type", "ats_token", "status", "jobs_at_check", "tags", "tier", "reputation",
           "domain", "careers_url", "verified_at", "notes"]
BOARD_TYPES = ("greenhouse", "lever", "ashby", "workday")
TAG_MIN_JOBS, TAG_MIN_SHARE, MAX_TAGS = 2, 0.15, 3
UA = {"User-Agent": "jobhub/0.1 (+company directory verifier)"}


# ---------- build ----------

def _norm_name(name: str) -> str:
    return slugify(name)


def load_entries(conn: sqlite3.Connection) -> list[dict]:
    """One dict per DB company / yaml entry, before merging."""
    rows = []
    for r in conn.execute("SELECT * FROM companies"):
        if not re.search(r"[A-Za-z]{2}", r["name"]):   # scraped junk like "1000"
            continue
        board = r["ats_type"] in BOARD_TYPES and r["ats_token"]
        rows.append({"name": r["name"].strip(" \\/"), "ats_type": r["ats_type"] if board else "", "ats_token": r["ats_token"] if board else "",
                     "tier": r["tier"], "reputation": r["reputation_score"], "domain": r["domain"] or "",
                     "careers_url": r["careers_url"] or "", "source": r["source"], "notes": ""})
    for c in config.load_companies_yaml().get("companies", []):
        ats = c.get("ats") or ""
        t, _, tok = ats.partition(":")
        board = t in BOARD_TYPES and tok
        rows.append({"name": c["name"], "ats_type": t if board else "", "ats_token": tok if board else "",
                     "tier": c.get("tier"), "reputation": None, "domain": "", "careers_url": c.get("careers_url", ""),
                     "source": "yaml", "notes": c.get("notes", "") or ""})
    return rows


def merge(entries: list[dict]) -> list[dict]:
    """Same board => same company (NXP / NXP Semiconductors). Then same normalized name. Prefer the yaml/curated spelling."""
    groups: dict[str, list[dict]] = {}
    by_board: dict[tuple[str, str], str] = {}
    by_name: dict[str, str] = {}
    for e in entries:
        board = (e["ats_type"], e["ats_token"].lower()) if e["ats_token"] else None
        key = (by_board.get(board) if board else None) or by_name.get(_norm_name(e["name"])) or f"g{len(groups)}"
        groups.setdefault(key, []).append(e)
        if board:
            by_board.setdefault(board, key)
        by_name.setdefault(_norm_name(e["name"]), key)
    out = []
    for members in groups.values():
        members.sort(key=lambda m: (m["source"] != "yaml", m["source"] != "manual", bool(not m["ats_token"]), len(m["name"])))
        primary = members[0]
        boards = [m for m in members if m["ats_token"]]
        b = boards[0] if boards else None
        names = [m["name"] for m in members]
        tiers = [m["tier"] for m in members if m["tier"] is not None]
        reps = [m["reputation"] for m in members if m["reputation"] is not None]
        out.append({
            "name": primary["name"],
            "aliases": sorted({n for n in names if n != primary["name"]}),
            "ats_type": b["ats_type"] if b else "", "ats_token": b["ats_token"] if b else "",
            "tier": min(tiers) if tiers else "", "reputation": max(reps) if reps else "",
            "domain": next((m["domain"] for m in members if m["domain"]), ""),
            "careers_url": next((m["careers_url"] for m in members if m["careers_url"]), ""),
            "notes": next((m["notes"] for m in members if m["notes"]), ""),
        })
    return out


def _tags_by_company(conn: sqlite3.Connection) -> tuple[dict[str, Counter], dict[str, Counter]]:
    """Role-type counts of postings already seen, keyed by board source and by normalized company name."""
    by_source: dict[str, Counter] = defaultdict(Counter)
    by_name: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute("SELECT company_name, title, source FROM jobs"):
        kind = roletype.classify(r["title"], company=r["company_name"])
        by_source[r["source"]][kind] += 1
        by_name[_norm_name(r["company_name"])][kind] += 1
    return by_source, by_name


def derive_tags(entry: dict, by_source: dict, by_name: dict, board_titles: list[str] | None = None) -> list[str]:
    counts: Counter = Counter()
    for t in board_titles or []:
        counts[roletype.classify(t, company=entry["name"])] += 1
    if entry["ats_token"]:
        token = entry["ats_token"].split("/")[0] if entry["ats_type"] == "workday" else entry["ats_token"]
        counts.update(by_source.get(f"{entry['ats_type']}:{token}", {}))
    for n in [entry["name"], *entry["aliases"]]:
        counts.update(by_name.get(_norm_name(n), {}))
    # Share is taken over titles that matched a specific role type: most postings at a big company (sales, finance,
    # facilities) classify as "general" and would otherwise dilute every real signal below the threshold.
    specific = Counter({k: c for k, c in counts.items() if k != roletype.GENERAL})
    total = sum(specific.values())
    tags = [k for k, c in specific.most_common(MAX_TAGS) if c >= TAG_MIN_JOBS and c / total >= TAG_MIN_SHARE]
    if not tags and total:   # a lone signal at a small company still beats no tag
        tags = [specific.most_common(1)[0][0]]
    if roletype.is_quant_firm(entry["name"]) and "quant" not in tags:
        tags.insert(0, "quant")
    return tags


# ---------- verify ----------

_gap_lock = threading.Lock()
_last: dict[str, float] = defaultdict(float)


def _polite(url: str, gap: float = 0.3) -> None:
    host = urlsplit(url).netloc
    with _gap_lock:
        wait = _last[host] + gap - time.monotonic()
        _last[host] = max(time.monotonic(), _last[host] + gap)
    if wait > 0:
        time.sleep(wait)


def verify(ats: str, token: str) -> tuple[str, int | None, list[str]]:
    """(status, job count, open job titles). dead = the board itself is gone; unverified = we couldn't tell (network,
    rate limit). Titles feed the tags: what a company hires for says what it works on. Workday is searched, not listed,
    so it reports up to 20 titles matching "engineer" and its count is the number of matches."""
    try:
        with httpx.Client(timeout=25.0, headers=UA, follow_redirects=True) as c:
            if ats == "workday":
                tenant, wd, site = parse_token(token)
                url = _endpoint(tenant, wd, site)
                _polite(url, 0.0)
                r = c.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "engineer"},
                           headers={"Accept": "application/json", "Content-Type": "application/json"})
            else:
                url = {"greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                       "lever": f"https://api.lever.co/v0/postings/{token}?mode=json",
                       "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}"}[ats]
                _polite(url)
                r = c.get(url)
            if r.status_code in (404, 422):
                return "dead", None, []
            if r.status_code != 200:
                return "unverified", None, []
            body = r.json()
    except (httpx.HTTPError, ValueError, KeyError):
        return "unverified", None, []
    if ats == "workday":
        if not (isinstance(body, dict) and "jobPostings" in body):
            return "dead", None, []
        n = body.get("total") or 0
        titles = [p.get("title") or "" for p in body["jobPostings"]]
    else:
        jobs = body.get("jobs") if isinstance(body, dict) else body
        if not isinstance(jobs, list):
            return "dead", None, []
        n = len(jobs)
        titles = [j.get("title") or j.get("text") or "" for j in jobs]
    return ("ok" if n else "empty"), int(n), titles


# ---------- main ----------

def read_previous() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    with OUT.open(newline="") as f:
        return {r["slug"]: r for r in csv.DictReader(f)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true", help="skip pinging boards; keep prior status/verified_at")
    args = ap.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    merged = merge(load_entries(conn))
    by_source, by_name = _tags_by_company(conn)
    prev = read_previous()
    today = date.today().isoformat()

    used: set[str] = set()
    for e in merged:
        slug = slugify(e["name"])
        base, i = slug, 2
        while slug in used:
            slug, i = f"{base}-{i}", i + 1
        used.add(slug)
        e["slug"] = slug
        old = prev.get(slug, {})
        e["prev_tags"] = [t for t in old.get("tags", "").split("|") if t]
        e.update(status=old.get("status") or ("unverified" if e["ats_token"] else "no_feed"),
                 jobs_at_check=old.get("jobs_at_check", ""), verified_at=old.get("verified_at", ""))
        if not e["ats_token"]:
            e["status"] = "no_feed"

    titles: dict[str, list[str]] = {}
    if not args.no_verify:
        boards = [e for e in merged if e["ats_token"]]
        print(f"verifying {len(boards)} boards...")
        with ThreadPoolExecutor(max_workers=16) as pool:
            for e, (status, n, ts) in zip(boards, pool.map(lambda e: verify(e["ats_type"], e["ats_token"]), boards)):
                e.update(status=status, jobs_at_check="" if n is None else n, verified_at=today)
                titles[e["slug"]] = ts
    for e in merged:
        derived = derive_tags(e, by_source, by_name, titles.get(e["slug"]))
        e["tags"] = sorted({*derived, *e["prev_tags"]})

    merged.sort(key=lambda e: e["slug"])
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, COLUMNS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for e in merged:
            w.writerow({**e, "aliases": "|".join(e["aliases"]), "tags": "|".join(e["tags"])})

    st = Counter(e["status"] for e in merged)
    print(f"wrote {len(merged)} companies -> {OUT.relative_to(ROOT)}")
    print("status:", dict(st))
    print("boards by type:", dict(Counter(e["ats_type"] for e in merged if e["ats_token"])))
    tagged = sum(1 for e in merged if e["tags"])
    print(f"tagged: {tagged}/{len(merged)}; tags:", dict(Counter(t for e in merged for t in e["tags"]).most_common()))
    print(f"with aliases: {sum(1 for e in merged if e['aliases'])}; with reputation/tier: "
          f"{sum(1 for e in merged if e['reputation'] != '' or e['tier'] != '')}")


if __name__ == "__main__":
    main()
