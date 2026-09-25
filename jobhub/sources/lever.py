from __future__ import annotations

from datetime import datetime, timezone

from ..models import RawJob
from ..normalize import html_to_text
from .base import client


class LeverSource:
    complete_listing = True

    def __init__(self, token: str, company_name: str) -> None:
        self.token = token
        self.company_name = company_name
        self.name = f"lever:{token}"

    def fetch(self) -> list[RawJob]:
        from ..fetch import _polite

        with client() as c:
            _polite("https://" + {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com"}[self.name.split(":")[0]])
            r = c.get(f"https://api.lever.co/v0/postings/{self.token}", params={"mode": "json"})
            r.raise_for_status()
        jobs: list[RawJob] = []
        for item in r.json():
            cats = item.get("categories") or {}
            parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
            for lst in item.get("lists") or []:
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            parts.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
            created = item.get("createdAt")
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat() if created else None
            jobs.append(
                RawJob(
                    company_name=self.company_name,
                    title=item.get("text", ""),
                    location=cats.get("location", "") or "",
                    url=item.get("hostedUrl") or item.get("applyUrl", ""),
                    source=self.name,
                    external_id=item.get("id"),
                    posted_at=posted,
                    description_text="\n\n".join(p for p in parts if p) or None,
                    raw={"team": cats.get("team"), "commitment": cats.get("commitment"), "workplaceType": item.get("workplaceType")},
                )
            )
        return jobs
