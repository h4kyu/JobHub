from __future__ import annotations

from ..models import RawJob
from ..normalize import html_to_text
from .base import client


class AshbySource:
    complete_listing = True

    def __init__(self, token: str, company_name: str) -> None:
        self.token = token
        self.company_name = company_name
        self.name = f"ashby:{token}"

    def fetch(self) -> list[RawJob]:
        from ..fetch import _polite

        with client() as c:
            _polite("https://" + {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com"}[self.name.split(":")[0]])
            r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{self.token}", params={"includeCompensation": "true"})
            r.raise_for_status()
        jobs: list[RawJob] = []
        for item in r.json().get("jobs", []):
            if item.get("isListed") is False:
                continue
            loc = item.get("location") or ""
            if item.get("isRemote"):
                loc = f"{loc}; Remote" if loc else "Remote"
            jobs.append(
                RawJob(
                    company_name=self.company_name,
                    title=item.get("title", ""),
                    location=loc,
                    url=item.get("jobUrl") or item.get("applyUrl", ""),
                    source=self.name,
                    external_id=item.get("id"),
                    posted_at=(item.get("publishedAt") or "")[:10] or None,
                    description_text=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")) or None,
                    raw={"department": item.get("department"), "team": item.get("team"), "employmentType": item.get("employmentType"),
                         "compensation": (item.get("compensation") or {}).get("compensationTierSummary")},
                )
            )
        return jobs
