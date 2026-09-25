"""Extract a stated application deadline from posting text. Free (regex); the model fills gaps when it reads a posting."""
from __future__ import annotations

import re
from datetime import date, timedelta

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_RES = [
    re.compile(rf"\b{_MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(20\d\d)?\b", re.I),          # October 15, 2026 / Oct 15
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\.?,?\s*(20\d\d)?\b", re.I),          # 15 October 2026
    re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b"),                                                     # 2026-10-15
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d|\d{2})\b"),                                          # 10/15/2026 (US)
]
_CUE_RE = re.compile(
    r"(application\s+deadline|deadline\s+(?:to\s+apply|for\s+applications?)?|apply\s+(?:by|before|no\s+later\s+than)|"
    r"applications?\s+(?:close|closes|closing|due|must\s+be\s+(?:received|submitted)|(?:are\s+)?accepted\s+(?:until|through))|"
    r"closing\s+date|closes\s+on|last\s+day\s+to\s+apply|posting\s+(?:closes|expires)|expires?\s+on|open\s+until)"
    r"[:\s]*(?:is|of|on)?[:\s]*",
    re.I,
)
_ROLLING_RE = re.compile(r"\b(rolling\s+basis|rolling\s+admission|applications?\s+(?:reviewed|considered)\s+on\s+a\s+rolling)\b", re.I)


def _parse(text: str, today: date) -> date | None:
    for rx in _DATE_RES:
        m = rx.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if rx is _DATE_RES[0]:
                mon, day, year = _MONTHS[g[0].lower().rstrip(".")[:4 if g[0].lower().startswith("sept") else 3]], int(g[1]), g[2]
            elif rx is _DATE_RES[1]:
                day, mon, year = int(g[0]), _MONTHS[g[1].lower().rstrip(".")[:4 if g[1].lower().startswith("sept") else 3]], g[2]
            elif rx is _DATE_RES[2]:
                return date(int(g[0]), int(g[1]), int(g[2]))
            else:
                mon, day, year = int(g[0]), int(g[1]), g[2]
                year = year if len(year) == 4 else "20" + year
            if year:
                d = date(int(year), mon, day)
            else:
                d = date(today.year, mon, day)
                if d < today - timedelta(days=30):
                    d = date(today.year + 1, mon, day)
            return d
        except (ValueError, KeyError):
            continue
    return None


def extract_deadline(text: str | None, today: date | None = None) -> str | None:
    """Return an ISO date if the text states an application deadline near a deadline cue; None otherwise
    (including 'rolling' postings). Only looks at a short window after each cue to avoid picking up start dates."""
    if not text:
        return None
    today = today or date.today()
    for m in _CUE_RE.finditer(text):
        window = text[m.end(): m.end() + 60]
        d = _parse(window, today)
        if d and d >= today - timedelta(days=60):
            return d.isoformat()
    return None


def is_rolling(text: str | None) -> bool:
    return bool(text and _ROLLING_RE.search(text))
