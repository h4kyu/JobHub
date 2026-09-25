from __future__ import annotations

import re
from typing import Protocol

import httpx

from ..models import RawJob

USER_AGENT = "jobhub/0.1 (+local internship tracker)"


def client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


class Source(Protocol):
    name: str
    complete_listing: bool  # True if fetch() returns every open posting (so absent jobs can be marked inactive)

    def fetch(self) -> list[RawJob]: ...


_INTERN_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op|co-?ops|summer analyst|campus|early[- ]career|"
                        r"student(?:s)?|undergraduate|apprentice(?:ship)?|placement|working student|werkstudent)\b", re.I)


def looks_like_internship(title: str) -> bool:
    """Word-boundary match so 'Internal Audit' doesn't count. Used to trim full company boards."""
    return bool(_INTERN_RE.search(title or ""))
