from __future__ import annotations

from ..models import RawJob
from ..normalize import html_to_text
from .base import client


class GreenhouseSource:
    complete_listing = True

    def __init__(self, token: str, company_name: str) -> None:
        self.token = token
        self.company_name = company_name
        self.name = f"greenhouse:{token}"

    def fetch(self) -> list[RawJob]:
        from ..fetch import _polite

        with client() as c:
            _polite("https://" + {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com"}[self.name.split(":")[0]])
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs", params={"content": "true"})
            r.raise_for_status()
        jobs: list[RawJob] = []
        for item in r.json().get("jobs", []):
            jobs.append(
                RawJob(
                    company_name=self.company_name,
                    title=item.get("title", ""),
                    location=(item.get("location") or {}).get("name", ""),
                    url=item.get("absolute_url", ""),
                    source=self.name,
                    external_id=str(item.get("id")),
                    posted_at=(item.get("first_published") or item.get("updated_at") or "")[:10] or None,
                    description_text=html_to_text(item.get("content")) or None,
                    raw={"departments": [d.get("name") for d in item.get("departments") or []]},
                )
            )
        return jobs
