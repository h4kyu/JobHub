"""Deterministic hard filters applied before any model call. Returns a reject reason or None."""
from __future__ import annotations

import re
from typing import Iterable

from . import degree
from .config import Profile

_SEASON_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*(?:of\s*)?(20\d\d)\b", re.I)
_SEASON_WORD_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_REMOTE_RE = re.compile(r"\bremote\b", re.I)


_UNPAID_RE = re.compile(
    r"\b(unpaid|non-?paid|no (?:pay|compensation|salary|stipend))\b(?!\s*(?:time off|leave|pto|vacation|absence|days?|hours?))",
    re.I,
)
_UNPAID_CONTEXT_RE = re.compile(r"\b(salary|compensation|pay|stipend|position|internship|role|opportunity|this is)\b", re.I)


def _contains_any(text: str, needles: Iterable[str]) -> str | None:
    low = text.lower()
    for n in needles:
        if n and n.lower() in low:
            return n
    return None


def _unpaid(text: str) -> bool:
    """'unpaid' only counts when it's about the role itself, not benefits boilerplate ('unpaid time off')."""
    for m in _UNPAID_RE.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 60]
        if _UNPAID_CONTEXT_RE.search(window):
            return True
    return False


def _parse_term(term: str) -> tuple[str | None, str | None]:
    m = _SEASON_RE.search(term or "")
    if m:
        season = m.group(1).lower().replace("autumn", "fall")
        return season, m.group(2)
    y = _YEAR_RE.search(term or "")
    return None, (y.group(1) if y else None)


def _labels(labels: Iterable[str]) -> list[tuple[str | None, str]]:
    return [(s, y) for s, y in (_parse_term(l) for l in labels) if y]


def primary_terms(profile: Profile) -> list[tuple[str | None, str]]:
    return _labels([profile.term] + list(profile.term_also_accept))


def alt_terms(profile: Profile) -> list[tuple[str | None, str]]:
    return _labels(profile.alt_terms)


def _accepted(profile: Profile) -> list[tuple[str | None, str]]:
    return primary_terms(profile) + alt_terms(profile)


def _matches(season: str | None, year: str, accepted: list[tuple[str | None, str]]) -> bool:
    return any(year == ay and (as_ is None or season is None or season == as_) for as_, ay in accepted)


def posting_terms(title: str, terms: list[str] | None) -> list[tuple[str | None, str]] | None:
    """The (season, year) pairs a posting claims, or None when it says nothing about its term —
    which is the common case for company boards (447 of 465 measured carried no term at all)."""
    # Structured terms from aggregators are authoritative when present and parseable ("N/A" is unknown).
    parsed = [(s, y) for s, y in (_parse_term(t) for t in (terms or [])) if y]
    if parsed:
        return parsed
    years = sorted(set(_YEAR_RE.findall(title or "")))
    if not years:
        return None
    seasons = sorted({m.lower().replace("autumn", "fall") for m in _SEASON_WORD_RE.findall(title or "")})
    return [(s, y) for y in years for s in (seasons or [None])]  # "Co-op 2027" -> season unknown, any season matches


def term_mismatch(profile: Profile, title: str, terms: list[str] | None) -> bool:
    """True only when the posting clearly targets a start window we don't want. Multi-term listings
    (e.g. Winter+Summer, 8-month co-ops) pass as long as one of their terms is acceptable."""
    accepted = _accepted(profile)
    claimed = posting_terms(title, terms)
    if not accepted or claimed is None:
        return False
    return not any(_matches(s, y, accepted) for s, y in claimed)


#: Which work-term track a posting belongs to. `unknown` is not a rejection — it is every posting that
#: never names a term, and those are shown in both tracks rather than hidden from one.
TRACK_PRIMARY, TRACK_ALT, TRACK_UNKNOWN = "primary", "alt", "unknown"


def term_track(profile: Profile, title: str, terms: list[str] | None) -> str:
    """primary (the target term), alt (e.g. Summer 2027), or unknown — a posting that names no term at
    all, or one that fits both tracks ("2027 Internship", a Winter+Summer listing). Unknown belongs to
    neither track exclusively, so callers show it under both rather than hiding it from one."""
    if not profile.alt_terms:
        return TRACK_PRIMARY
    claimed = posting_terms(title, terms)
    if claimed is None:
        return TRACK_UNKNOWN
    prim = any(_matches(s, y, primary_terms(profile)) for s, y in claimed)
    alt = any(_matches(s, y, alt_terms(profile)) for s, y in claimed)
    return TRACK_PRIMARY if prim and not alt else TRACK_ALT if alt and not prim else TRACK_UNKNOWN


def in_track(track: str, want: str) -> bool:
    """Track filter for the UI/digest: `unknown` postings appear under both named tracks."""
    return want in ("all", track) or track == TRACK_UNKNOWN


def location_not_allowed(profile: Profile, location: str) -> bool:
    if not profile.strict_location or not location or not profile.locations:
        return False
    if profile.remote_ok and _REMOTE_RE.search(location):
        return False
    low = location.lower()
    return not any(l.lower() in low for l in profile.locations)


# --- firmware-heavy detection --------------------------------------------------------------------
# The user keeps embedded experience on the profile but does not want firmware roles. Bare-metal MCU
# firmware postings (SPI/I2C/UART/CAN, RTOS, bootloaders as the core job) are archived deterministically.
# Embedded-Linux, FPGA, and hardware-test roles are NOT matched here — only bare-metal MCU firmware.
_FW_TITLE_RE = re.compile(r"\bfirmware\b", re.I)
_FW_SIGNAL_RES = [re.compile(p, re.I) for p in (
    r"\bspi\b",
    r"\bi[2²]c\b",
    r"\busart\b|\buart\b",
    r"\bcan[- ]?(?:bus|fd|protocol)\b|\bcanbus\b|\bcan/lin\b|\blin[- ]?bus\b",
    r"\brtos\b|\bfree-?rtos\b|\bzephyr\b|\bvxworks\b|\bthreadx\b",
    r"\bbare[- ]?metal\b",
    r"\bboot-?loaders?\b",
    r"\bmicro-?controllers?\b|\bmcus?\b|\bstm32\w*\b|\bpic1[68]\b|\besp32\b|\bmsp430\b|\bnrf5\d\b|\bcortex[- ]?m\d?\b|\batmega\b",
    r"\bjtag\b|\bswd\b",
    r"\bembedded c\b|\bfirmware\b",
    r"\bgpio\b",
)]
FW_MIN_SIGNALS = 3
# The signal count only condemns a posting whose *title* already reads embedded/EE. Umbrella postings
# ("NVIDIA 2027 Internships: Systems Software") mention SPI/I2C in passing and must reach the model instead.
_FW_TITLE_GATE_RE = re.compile(r"\b(embedded|electrical|hardware|avionics|microcontrollers?|mcu)\b", re.I)


def firmware_heavy(title: str, description: str | None) -> bool:
    """True for roles whose core job is bare-metal MCU firmware. A title saying "firmware" is decisive.
    Otherwise the title must read embedded/EE AND the text must carry several distinct low-level signals —
    so a robotics or systems posting that lists CAN or SPI among many skills still passes to the model
    (which is told to score firmware work very low; this filter only handles the clear-cut cases)."""
    if _FW_TITLE_RE.search(title or ""):
        return True
    if not _FW_TITLE_GATE_RE.search(title or ""):
        return False
    text = f"{title or ''}\n{description or ''}"
    return sum(1 for rx in _FW_SIGNAL_RES if rx.search(text)) >= FW_MIN_SIGNALS


def _title_excluded(profile: Profile, title: str) -> bool:
    words = [w for w in profile.dealbreakers.title_exclude if w]
    if not words:
        return False
    pat = r"\b(?:" + "|".join(re.escape(w) for w in words) + r")"
    return re.search(pat, title or "", re.I) is not None


def check(profile: Profile, *, title: str, location: str, terms: list[str] | None, description: str | None,
          sponsorship: str | None = None) -> str | None:
    """Return a short machine-readable reason (used for grouping) or None if the job passes."""
    if term_mismatch(profile, title, terms):
        return "term_mismatch"
    if sponsorship and _contains_any(sponsorship, profile.dealbreakers.sponsorship_values):
        return "citizenship"
    if _title_excluded(profile, title):
        return "out_of_scope"
    if firmware_heavy(title, description):
        return "firmware_heavy"
    if location_not_allowed(profile, location):
        return "location_not_allowed"
    haystack = "\n".join(filter(None, (title, location, description or "")))
    d = profile.dealbreakers
    if _contains_any(location or "", d.location_exclude) or _contains_any(title or "", d.location_exclude):
        return "location_excluded"
    if _unpaid(haystack) or _contains_any(haystack, [n for n in d.unpaid if n.lower() != "unpaid"]):
        return "unpaid"
    for reason, needles in (
        ("clearance", d.clearance), ("citizenship", d.citizenship), ("level", d.level), ("other", d.other),
    ):
        if _contains_any(haystack, needles):
            return reason
    # Last, so a posting that is *also* clearance-only or unpaid keeps that more fundamental reason.
    # Degree level, not graduation date: rejects only a posting whose lowest accepted degree is above the
    # highest in `profile.degrees` ("PhD, Quantitative SWE" goes, "BS/MS" stays). See jobhub/degree.py.
    return degree.degree_mismatch(profile.degrees, title, description)
