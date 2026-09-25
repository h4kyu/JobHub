"""SimplifyJobs internship aggregator repos publish structured listings at .github/scripts/listings.json."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from ..models import RawJob
from .base import client


class SimplifySource:
    complete_listing = False  # repo includes inactive entries with active=false; we use that flag instead

    def __init__(self, repo: str, branches: tuple[str, ...] = ("dev", "main")) -> None:
        self.repo = repo
        self.branches = branches
        self.name = f"simplify:{repo}"
        m = re.search(r"(20\d\d)", repo)
        self.year = m.group(1) if m else None

    def _terms(self, item: dict) -> list[str]:
        terms = list(item.get("terms") or [])
        if terms:
            return terms
        season = item.get("season")
        if season and season != "null" and self.year:
            return [f"{s.strip()} {self.year}" for s in str(season).split("/") if s.strip()]
        return []

    def fetch(self) -> list[RawJob]:
        data = None
        with client() as c:
            for branch in self.branches:
                url = f"https://raw.githubusercontent.com/{self.repo}/{branch}/.github/scripts/listings.json"
                r = c.get(url)
                if r.status_code == 200:
                    data = r.json()
                    break
        if data is None:
            raise httpx.HTTPError(f"listings.json not found for {self.repo} on branches {self.branches}")
        jobs: list[RawJob] = []
        for item in data:
            if not item.get("is_visible", True):
                continue
            posted = item.get("date_posted") or item.get("date_updated")
            posted_iso = (
                datetime.fromtimestamp(posted, tz=timezone.utc).date().isoformat() if isinstance(posted, (int, float)) else None
            )
            jobs.append(
                RawJob(
                    company_name=item.get("company_name", "").strip() or "Unknown",
                    title=item.get("title", "").strip(),
                    location="; ".join(item.get("locations") or []),
                    url=item.get("url", ""),
                    source=self.name,
                    external_id=item.get("id"),
                    posted_at=posted_iso,
                    active=bool(item.get("active", True)),
                    terms=self._terms(item),
                    sponsorship=item.get("sponsorship"),
                    description_text=None,
                    raw={"company_url": item.get("company_url"), "season": item.get("season")},
                )
            )
        return jobs
