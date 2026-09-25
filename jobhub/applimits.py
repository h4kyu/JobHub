"""Per-company application limits ("Candidates are limited to three (3) applications within a 30-day period").

Read from posting text with a regex — free, no model calls — and shown on job cards next to how many of that
company's postings you have marked applied, so going over a cap is visible before you submit. Companies state
the rule in boilerplate on every posting, so any one posting carrying it tags the whole company. Some are hard
caps (Zipline, Coinbase, Goldman), some are requests (Salesforce, EY); the tooltip shows the exact sentence.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from .normalize import slugify

# Statuses that used up one of your applications.
SUBMITTED = ("applied", "interview", "rejected", "offer")

# The same company under two slugs (a board and an aggregator spelling). Maps alias slug -> canonical slug.
ALIASES = {"flyzipline": "zipline"}

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_N = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|[1-9]|10)(?![0-9])"
_THING = (r"(?:applications?|roles?|positions?|job postings?|postings?|jobs?|internships?|opportunit(?:y|ies)"
          r"|programs?|programmes?|combinations?)")
_LIMIT_RX = re.compile(
    # "a maximum of 3 applications", "limited to three (3) applications", "up to 4 separate business / location combinations"
    rf"(?:up to|no more than|a maximum of|maximum of|max(?:imum)? of|limited to|at most)\s+"
    rf"(?P<n>{_N})\s*(?:\((?:{_N})\))?\s+(?:[\w&/-]+\s+){{0,4}}{_THING}"
    # "only submit one application", "only apply for one internship position", "only allow you to submit one application"
    rf"|\bonly\s+(?:[\w-]+\s+){{1,4}}(?P<m>{_N})\s+(?:[\w&/-]+\s+){{0,4}}{_THING}"
    # "one application per season" -- but not "one application per role", which caps duplicates, not the company
    rf"|\bone (?:[\w-]+\s+){{0,2}}application per (?:candidate|applicant|person|season|recruitment|cycle|year)",
    re.I)
# Only sentences about applying count: "up to 2 minutes in position" (a physical-demands table) must not.
_ABOUT_APPLYING = re.compile(r"\bappl(?:y|ying|ied|ication|ications)\b|\bsubmit", re.I)
_PER_ROLE = re.compile(r"\bper (?:role|position|posting|job|requisition|opening)\b", re.I)
# The regex only runs in a window around one of these (found with str.find on lowered text). Running it over
# whole descriptions took 5.5s for ~8K postings; anchored, 0.7s with identical results.
_ANCHORS = ("maximum of", "no more than", "limited to", "at most", "up to", "only ", "application per")


@dataclass(frozen=True)
class Limit:
    count: int          # 1 for phrasings with no number ("one application per season")
    text: str           # the sentence, for the tooltip


def _sentence(text: str, start: int, end: int) -> str:
    s = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start)) + 1
    e = min((i for i in (text.find(". ", end), text.find("\n", end)) if i != -1), default=len(text))
    return re.sub(r"\s+", " ", text[s:e + 1]).strip()


def find_limit(text: str | None) -> Limit | None:
    low = (text or "").lower()
    if "appl" not in low:
        return None
    for anchor in _ANCHORS:
        i = low.find(anchor)
        while i != -1:
            for m in _LIMIT_RX.finditer(text, max(0, i - 40), i + 200):
                sentence = _sentence(text, m.start(), m.end())
                if not _ABOUT_APPLYING.search(sentence) or len(sentence) > 400 or _PER_ROLE.search(m.group(0)):
                    continue
                n = m.group("n") or m.group("m")
                count = (int(n) if n.isdigit() else _NUM[n.lower()]) if n else 1
                return Limit(count, sentence)
            i = low.find(anchor, i + 1)
    return None


def company_key(company_name: str | None) -> str:
    """Keyed by name, not companies.id: most aggregator postings (all 297 TikTok ones) have no company row."""
    slug = slugify(company_name or "")
    return ALIASES.get(slug, slug)


def company_limits(conn: sqlite3.Connection) -> dict[str, Limit]:
    """company_key -> its limit, taking the sentence most of its postings carry."""
    found: dict[str, Counter[Limit]] = {}
    for name, text in conn.execute("SELECT company_name, description_text FROM jobs WHERE description_text != ''"):
        lim = find_limit(text)
        if lim:
            found.setdefault(company_key(name), Counter())[lim] += 1
    return {key: c.most_common(1)[0][0] for key, c in found.items()}


def submitted_counts(conn: sqlite3.Connection) -> dict[str, int]:
    marks = ",".join("?" * len(SUBMITTED))
    out: Counter[str] = Counter()
    for name, n in conn.execute(f"""SELECT j.company_name, COUNT(*) FROM applications a JOIN jobs j ON j.id = a.job_id
                                    WHERE a.status IN ({marks}) GROUP BY j.company_name""", SUBMITTED):
        out[company_key(name)] += n
    return dict(out)


_cache: dict[str, object] = {}


def cached_company_limits(conn: sqlite3.Connection) -> dict[str, Limit]:
    """company_limits() rescans posting text; redo it only when the jobs table has changed."""
    key = conn.execute("SELECT COUNT(*), MAX(last_seen_at), MAX(id) FROM jobs").fetchone()
    key = tuple(key)
    if _cache.get("key") != key:
        _cache["limits"], _cache["key"] = company_limits(conn), key
    return _cache["limits"]  # type: ignore[return-value]
