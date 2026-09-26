"""Degree-level filtering: reject postings that require a degree above the one the candidate holds.

The motivating case: Waymo posts ``2027 Summer Intern, PhD, Quantitative Software Engineer`` next to
``2027 Summer Intern, BS/MS, Software Engineering``. Both are on-target autonomy software and the fast
scorer loved the first one (100), but a 2B undergrad cannot apply to it.

The whole risk here is over-rejecting: most postings that *mention* a PhD accept a bachelor's
("BS, MS or PhD in Computer Science", "PhD a plus"). So the rule is deliberately asymmetric —

* a posting is rejected only when it names an **accepted set** of degrees whose *lowest* member is
  above the candidate's highest degree (``{phd}`` and ``{master, phd}`` reject a bachelor's student;
  ``{bachelor, master, phd}`` does not);
* a title that names degrees is authoritative and the description is not read at all — Waymo, Google,
  NVIDIA and Apple all encode the level in the title, and titles carry no boilerplate;
* otherwise only *enrollment requirement* clauses in the description count ("currently enrolled in a
  PhD program"), never a bare mention — and a clause hedged with "preferred" / "a plus" is ignored.

**Graduation dates and years of study are never a reason to reject.** This filter is about degree
*level* only. Postings routinely bound the graduation window ("anticipated graduation date December
2027 – May 2029", "must graduate within 12 months of the internship", "rising senior") and a Waterloo
BASc running Sept 2024 – May 2029 sits outside a lot of those windows while still being perfectly
eligible — Waterloo co-op students are junior-year-equivalent for years. So year-of-study words are read
in one direction only: they *add* ``bachelor`` to the accepted set (``ceiling`` can then only rise, never
fall), and no date is parsed anywhere in this module. ``tests/test_degree.py`` pins that invariant.

Nothing here is model-visible (see ``config.model_visible_constraints``), so tuning the patterns or the
``degrees`` list never re-evaluates anything: ``jobhub rescore --recompute-only`` re-applies it for free.
"""
from __future__ import annotations

import re
from typing import Iterable

#: Degree levels, low to high. `associate` exists so a community-college posting isn't read as a ceiling.
LEVELS: dict[str, int] = {"associate": 1, "bachelor": 2, "master": 3, "phd": 4}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}

#: How a posting names each level. Abbreviations are matched case-*sensitively* ("BS/MS", not "ms"),
#: because lowercase "ms"/"ba" are common words and units; spelled-out forms are case-insensitive.
#: Every abbreviation needs an explicit leading boundary — `\b` is no help where a pattern may start with
#: a letter that is mid-word: without one, "ph\.?\s?d" happily matches the "ph d" in "graph database".
_PATTERNS: list[tuple[str, str, int]] = [
    # (level, pattern, re-flags)
    ("phd", r"(?<![A-Za-z])ph\.?[\s-]?d\.?s?(?![A-Za-z])|doctoral|doctorate|(?<![A-Za-z])d\.?phil", re.I),
    ("master", r"master'?s?(?:\s+degree)?|graduate degree|(?<![A-Za-z])m\.?eng\b|(?<![A-Za-z])m\.?a\.?sc\b", re.I),
    ("master", r"\bM\.?S\.?c?\.?(?![A-Za-z])|\bM\.?A\.?(?![A-Za-z])|\bMASc\b|\bMEng\b", 0),
    ("bachelor", r"bachelor'?s?(?:\s+degree)?|baccalaureate|undergraduate|undergrad\b"
                 r"|(?<![A-Za-z])b\.?eng\b|(?<![A-Za-z])b\.?a\.?sc\b", re.I),
    ("bachelor", r"\bB\.?S\.?c?\.?(?![A-Za-z])|\bB\.?A\.?(?![A-Za-z])|\bBASc\b|\bBEng\b", 0),
    ("associate", r"associate'?s?\s+degree|\bA\.?A\.?S?\.?(?![A-Za-z])", re.I),
]
_COMPILED = [(lvl, re.compile(pat, flags)) for lvl, pat, flags in _PATTERNS]

#: "Graduate student" means master's-or-above, but "graduate" alone is the trap: "recent graduate",
#: "graduating in 2027", "new graduate program" all say nothing about a degree level.
_GRAD_STUDENT_RE = re.compile(r"\bgraduate\s+(?:student|program\b(?!\s*(?:for|open))|degree|level\s+(?:student|coursework))", re.I)
_GRAD_FALSE_FRIEND_RE = re.compile(r"\b(?:recent|new|upcoming|graduating|will\s+graduate|expected\s+to\s+graduate)\b", re.I)

#: Undergraduate *year* words name a bachelor's without naming the degree — RBC's "Must be a Sophomore,
#: Junior, or in a Masters program" and "the final year of a four-year college program or a relevant
#: master's program" are both open to undergrads and must not read as master's-only.
_UNDERGRAD_YEAR_RE = re.compile(
    r"\b(?:fresh(?:man|men)|sophomore|junior|rising\s+(?:junior|senior)|penultimate\s+year"
    r"|four-?\s?year\s+(?:college|university|degree|program|institution)"
    r"|(?:1st|2nd|3rd|4th|first|second|third|fourth)[\s-]year)\b", re.I)


#: Dotted abbreviations must lose their dots before the description is split into clauses, or "pursuing a
#: Ph.D. or B.S." splits into "pursuing a Ph" / "D" / "or B" / "S" and the B.S. falls out of the clause —
#: turning a posting open to a bachelor's into a phd-only reject.
_DOTTED_DEGREE_RE = re.compile(
    r"\b(Ph\.\s?D|B\.\s?A\.\s?Sc|M\.\s?A\.\s?Sc|B\.\s?Sc|M\.\s?Sc|B\.\s?S|M\.\s?S|B\.\s?A|M\.\s?A"
    r"|B\.\s?Eng|M\.\s?Eng|D\.\s?Phil|A\.\s?A\.?\s?S)\.?", re.I)


def _undot(text: str) -> str:
    return _DOTTED_DEGREE_RE.sub(lambda m: m.group(1).replace(".", "").replace(" ", ""), text or "")


def levels_in(text: str) -> set[str]:
    """Every degree level named anywhere in `text`. No context, no judgement — callers supply both."""
    text = text or ""
    found = {lvl for lvl, rx in _COMPILED if rx.search(text)}
    if _GRAD_STUDENT_RE.search(text) and not _GRAD_FALSE_FRIEND_RE.search(text):
        found.add("master")
    if _UNDERGRAD_YEAR_RE.search(text):
        found.add("bachelor")
    return found


# --- description clauses ---------------------------------------------------------------------------
# Only a clause that states an enrollment/degree *requirement* may set a ceiling. Bare mentions
# ("Hourly PhD Pay", a sidebar listing the company's other openings, "our team has PhDs") are ignored.
# The description is split into clauses first and each is matched whole: the patterns then need no
# leading wildcard, which keeps this linear over descriptions that run to several thousand characters.
# Bullet lists flattened out of HTML often carry no sentence punctuation at all ("Minimum Qualifications -
# Currently pursuing a PhD degree in CS - Knowledge of database kernels - ... Preferred Qualifications - ..."),
# so a spaced dash counts as a clause break too, or the whole run reads as one clause and the "Preferred"
# heading further along hedges away a requirement it has nothing to do with.
_CLAUSE_SPLIT_RE = re.compile(r"(?:[.;:!?\n\r•|]|\s[-–—*]\s)+")
_REQUIRE_RE = re.compile(
    r"(?:currently\s+)?(?:enrolled|enrolment|enrollment|pursuing|working\s+to(?:wards?|ward)|matriculated"
    r"|must\s+(?:be|have|hold)|required|require[sd]?\s|requirements?\b|eligib|open\s+(?:only\s+)?to"
    r"|candidat\w+\s+(?:should|must)|you\s+(?:are|will\s+be)\s+(?:currently\s+)?(?:enrolled|pursuing))", re.I)
#: A clause that merely *prefers* or *adds* a degree sets no ceiling: "PhD preferred" leaves the job open
#: to a BS, and Hexagon's "completed your 2nd or 3rd year; Masters and PhD students are also eligible"
#: names the grad degrees precisely because they are an *addition* to the undergrad baseline.
_OPTIONAL_RE = re.compile(
    r"\b(?:preferred|preferable|preferably|a\s+plus|nice\s+to\s+have|bonus|ideally|desirable|desired|"
    # "or equivalent experience" waives the degree; "or an equivalent technical discipline" is about the
    # major, not the degree, so only the experience form counts as a hedge.
    r"advantageous|an\s+asset|not\s+required|or\s+equivalent\s+(?:work\s+)?experience|"
    r"also\s+(?:eligible|welcome|considered|encouraged)|are\s+welcome|as\s+well)\b", re.I)
#: Pay bands list every degree the company hires ("Hourly Masters Pay / Hourly PhD Pay", "actual pay depends
#: on degree-seeking academic program (PhD, Master's, Bachelor's, etc)") and must never set a ceiling.
#: This is the one exclusion; requiring a study word instead ("program", "degree") missed the commonest
#: PhD-only phrasing there is — ByteDance's bare "Currently pursuing a PhD in Computer Science".
_PAY_RE = re.compile(r"\b(?:pay|salary|hourly|compensation|wage|stipend|remuneration|rate\s+range|base\s+rate)\b", re.I)

#: Section headings. A degree named under "Preferred qualifications" is not a requirement — Microsoft's
#: generic SWE intern posting lists a Bachelor's as basic and a Doctorate as preferred, and reading the
#: second as a ceiling would archive the most ordinary internship on the board.
_HEADING_MAX = 60
_PREFERRED_HEAD_RE = re.compile(
    r"\b(?:preferred|desired|nice[\s-]to[\s-]have|bonus|additional|other|pluses)\b", re.I)
_REQUIRED_HEAD_RE = re.compile(
    r"\b(?:basic|minimum|required|requirements?|mandatory|must[\s-]have|essential|eligibility|"
    r"qualifications?|education)\b", re.I)


def _requirement_clauses(description: str) -> list[str]:
    """Clauses that state a degree requirement, each judged on its own: the hedge and pay guards apply to
    the clause that names the degree, never to its neighbours. Reading a clause together with the next one
    was what made ByteDance's "Minimum Qualifications - Currently pursuing a PhD degree in CS" pass — the
    word "Preferred" from the *following* section's heading landed in the same string and hedged it away.

    Clauses under a "Preferred qualifications" heading are skipped until the next required-style heading.
    """
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(description)]
    out: list[str] = []
    in_preferred = False
    for p in parts:
        if not p:
            continue
        if len(p) <= _HEADING_MAX and not levels_in(p):     # a short line with no degree in it: a heading
            if _PREFERRED_HEAD_RE.search(p):
                in_preferred = True
                continue
            if _REQUIRED_HEAD_RE.search(p):
                in_preferred = False
        if in_preferred or len(p) > 400 or not _REQUIRE_RE.search(p):
            continue
        if not _OPTIONAL_RE.search(p) and not _PAY_RE.search(p):
            out.append(p)
    return out


def accepted_levels(title: str, description: str | None) -> tuple[set[str], str] | None:
    """The degree levels a posting accepts, plus where that was read from ("title" / "description"),
    or None when the posting says nothing usable. A title that names any degree wins outright."""
    from_title = levels_in(_undot(title or ""))
    if from_title:
        return from_title, "title"
    if not description:
        return None
    found: set[str] = set()
    for clause in _requirement_clauses(_undot(description)):
        found |= levels_in(clause)
    return (found, "description") if found else None


# --- the filter -----------------------------------------------------------------------------------

def ceiling(degrees: Iterable[str]) -> int:
    """The highest level in the candidate's `degrees` list (0 when the list is empty = filter off)."""
    return max((LEVELS[d] for d in (str(x).strip().lower() for x in degrees) if d in LEVELS), default=0)


def degree_mismatch(degrees: Iterable[str], title: str, description: str | None) -> str | None:
    """"phd-only" / "master-only" when the posting's *lowest* accepted degree is above the candidate's
    highest, else None. An empty `degrees` list disables the check entirely."""
    top = ceiling(degrees)
    if not top:
        return None
    got = accepted_levels(title, description)
    if not got:
        return None
    levels, _where = got
    lowest = min(LEVELS[l] for l in levels)
    if lowest <= top:
        return None
    return f"{LEVEL_NAMES[lowest]}-only"
