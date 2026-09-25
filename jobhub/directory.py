"""The shared company directory (directory/board_directory.csv): who exists, what they hire for, and where their job board is.

Maintained with the code and never edited by users. A user's *watchlist* (jobhub/watchlist.py) picks entries from here.
Board addresses stay internal: the UI shows a company's name and fields, never its ATS or token.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache

from . import config
from .normalize import slugify

#: A board we can poll. `dead` means it answered 404 at the last check, so it is treated as no feed.
POLLABLE_STATUSES = ("ok", "empty", "unverified")


@dataclass(frozen=True)
class Entry:
    slug: str
    name: str
    aliases: tuple[str, ...] = ()
    ats_type: str = ""
    ats_token: str = ""
    status: str = "no_feed"
    tags: tuple[str, ...] = ()
    tier: int | None = None
    reputation: int | None = None
    domain: str = ""
    careers_url: str = ""
    slugs: frozenset[str] = field(default_factory=frozenset, compare=False)   # slug + alias slugs, for matching DB rows

    @property
    def pollable(self) -> bool:
        return bool(self.ats_type and self.ats_token and self.status in POLLABLE_STATUSES)


def _int(v: str) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def load() -> tuple[Entry, ...]:
    if not config.DIRECTORY_CSV.exists():
        return ()
    out = []
    with config.DIRECTORY_CSV.open(newline="") as f:
        for r in csv.DictReader(f):
            aliases = tuple(a for a in r["aliases"].split("|") if a)
            out.append(Entry(
                slug=r["slug"], name=r["name"], aliases=aliases, ats_type=r["ats_type"], ats_token=r["ats_token"],
                status=r["status"], tags=tuple(t for t in r["tags"].split("|") if t), tier=_int(r["tier"]),
                reputation=_int(r["reputation"]), domain=r["domain"], careers_url=r["careers_url"],
                slugs=frozenset({r["slug"], *(slugify(a) for a in aliases)})))
    return tuple(out)


@lru_cache(maxsize=1)
def _by_slug() -> dict[str, Entry]:
    idx: dict[str, Entry] = {}
    for e in load():
        for s in e.slugs:
            idx.setdefault(s, e)
    return idx


@lru_cache(maxsize=1)
def _by_board() -> dict[tuple[str, str], Entry]:
    return {(e.ats_type, e.ats_token.lower()): e for e in load() if e.ats_token}


def get(slug: str) -> Entry | None:
    return _by_slug().get(slug)


def find(name: str | None = None, *, slug: str | None = None, ats_type: str | None = None,
         ats_token: str | None = None) -> Entry | None:
    """Match a company (a DB row, or free text) to its directory entry by slug/alias, then by board."""
    if slug and (e := _by_slug().get(slug)):
        return e
    if name and (e := _by_slug().get(slugify(name))):
        return e
    if ats_type and ats_token:
        return _by_board().get((ats_type, ats_token.lower()))
    return None


def _rank(e: Entry, q: str) -> int | None:
    """0 exact, 1 prefix, 2 word prefix, 3 substring, None no match. Names and aliases both count."""
    best = None
    for n in (e.name, *e.aliases):
        n = n.lower()
        if n == q:
            r = 0
        elif n.startswith(q):
            r = 1
        elif any(w.startswith(q) for w in n.replace("(", " ").replace(")", " ").split()):
            r = 2
        elif q in n:
            r = 3
        else:
            continue
        best = r if best is None else min(best, r)
    return best


def search(q: str = "", tag: str | None = None) -> list[Entry]:
    """Entries matching the text and/or the field tag, best first (exact name, then prefix, then tier, then name)."""
    q = q.strip().lower()
    hits: list[tuple[int, int, str, Entry]] = []
    for e in load():
        if tag and tag not in e.tags:
            continue
        rank = _rank(e, q) if q else 0
        if rank is None:
            continue
        hits.append((rank, e.tier or 9, e.name.lower(), e))
    hits.sort(key=lambda h: h[:3])
    return [h[3] for h in hits]


def tag_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in load():
        for t in e.tags:
            counts[t] = counts.get(t, 0) + 1
    return counts
