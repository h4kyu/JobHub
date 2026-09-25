"""Workday board source.

Most large-cap employers (Intel, Broadcom, Marvell, ...) run Workday, which has no public board API of
the Greenhouse/Lever/Ashby kind — but the careers site itself is a thin client over a JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

`site` varies per tenant ("External", "Careers", "MarvellCareers", ...) and a wrong one returns 422,
so the token stored on the company is the full triple `tenant/wd/site` (e.g. `intel/wd1/External`).
`probe_workday()` discovers it by trying the common names.

Unlike the other ATS sources this is NOT a complete listing: the endpoint is queried with a search term
rather than enumerated, so jobs absent from a fetch must not be marked inactive.
"""
from __future__ import annotations

import re

import httpx

from ..models import RawJob
from .base import client, looks_like_internship

PAGE = 20
MAX_PAGES = 25                      # 500 postings per search term is plenty for an internship sweep
# Some tenants (Broadcom) ignore searchText and return the whole board, so paging must also stop when
# the results stop containing internships rather than relying on the search to have filtered anything.
MAX_EMPTY_PAGES = 5
SEARCH_TERMS = ("intern", "co-op", "student")
COMMON_SITES = (
    "External", "Careers", "External_Career", "External_Careers", "ExternalCareers",
    "careers", "External_Career_Site", "Search", "Professional", "Global",
)


def _endpoint(tenant: str, wd: str, site: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def parse_token(token: str) -> tuple[str, str, str]:
    """`tenant/wd/site`; the wd segment may be omitted and defaults to wd1."""
    parts = [p for p in token.split("/") if p]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], "wd1", parts[1]
    raise ValueError(f"workday token must be tenant/wd/site, got {token!r}")


def probe_workday(tenant: str, wds: tuple[str, ...] = ("wd1", "wd5", "wd3", "wd12")) -> str | None:
    """Find a working tenant/wd/site triple, or None. Deterministic, no model tokens."""
    with client(timeout=20.0) as c:
        for wd in wds:
            for site in COMMON_SITES:
                try:
                    r = c.post(_endpoint(tenant, wd, site),
                               json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "intern"},
                               headers={"Accept": "application/json", "Content-Type": "application/json"})
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    body = r.json()
                except ValueError:
                    continue
                if isinstance(body, dict) and "jobPostings" in body:
                    return f"{tenant}/{wd}/{site}"
    return None


class WorkdaySource:
    # The endpoint is searched, not enumerated, so a job missing from one fetch is not proof it closed.
    complete_listing = False

    def __init__(self, token: str, company_name: str) -> None:
        self.token = token
        self.company_name = company_name
        self.name = f"workday:{token.split('/')[0]}"
        self.tenant, self.wd, self.site = parse_token(token)

    def _external_url(self, path: str) -> str:
        # externalPath looks like "/job/Santa-Clara/Intern_JR123"; the public page drops the /wday/cxs prefix.
        return f"https://{self.tenant}.{self.wd}.myworkdayjobs.com/{self.site}{path}"

    def fetch(self) -> list[RawJob]:
        from ..fetch import _polite

        url = _endpoint(self.tenant, self.wd, self.site)
        seen: dict[str, RawJob] = {}
        with client(timeout=30.0) as c:
            for term in SEARCH_TERMS:
                empty = 0
                for page in range(MAX_PAGES):
                    _polite(url)
                    try:
                        r = c.post(url, json={"appliedFacets": {}, "limit": PAGE,
                                              "offset": page * PAGE, "searchText": term},
                                   headers={"Accept": "application/json", "Content-Type": "application/json"})
                        r.raise_for_status()
                        body = r.json()
                    except (httpx.HTTPError, ValueError):
                        break
                    postings = body.get("jobPostings") or []
                    if not postings:
                        break
                    before = len(seen)
                    for it in postings:
                        title = (it.get("title") or "").strip()
                        path = it.get("externalPath") or ""
                        if not title or not path or not looks_like_internship(title):
                            continue
                        seen[path] = RawJob(
                            company_name=self.company_name,
                            title=title,
                            location=(it.get("locationsText") or "").strip(),
                            url=self._external_url(path),
                            source=self.name,
                            external_id=(it.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1],
                            posted_at=_posted(it.get("postedOn")),
                            raw={"postedOn": it.get("postedOn")},
                        )
                    empty = empty + 1 if len(seen) == before else 0
                    if empty >= MAX_EMPTY_PAGES or len(postings) < PAGE:
                        break
        return list(seen.values())


_DAYS_RE = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def _posted(posted_on: str | None) -> str | None:
    """Workday reports "Posted 30+ Days Ago" / "Posted Today" rather than a date."""
    if not posted_on:
        return None
    from datetime import date, timedelta

    if re.search(r"\btoday\b", posted_on, re.I):
        return date.today().isoformat()
    if re.search(r"\byesterday\b", posted_on, re.I):
        return (date.today() - timedelta(days=1)).isoformat()
    m = _DAYS_RE.search(posted_on)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    return None
