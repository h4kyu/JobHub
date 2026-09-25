"""Fallback: one careers-page URL, text extracted with trafilatura. Used for companies without a known ATS.
Produces a single 'page' RawJob only when the page looks like a job posting; listing pages are skipped."""
from __future__ import annotations

from ..models import RawJob
from ..fetch import fetch_text


class GenericPageSource:
    complete_listing = False

    def __init__(self, url: str, company_name: str) -> None:
        self.url = url
        self.company_name = company_name
        self.name = f"page:{url}"

    def fetch(self) -> list[RawJob]:
        text = fetch_text(self.url)
        if not text or "intern" not in text.lower():
            return []
        title = text.splitlines()[0][:120] if text else "Careers page"
        return [RawJob(company_name=self.company_name, title=title, url=self.url, source=self.name, description_text=text)]
