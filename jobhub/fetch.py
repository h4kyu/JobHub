"""Fetching job descriptions and probing ATS endpoints, with per-host politeness."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from .sources.base import client

_last_hit: dict[str, float] = defaultdict(float)
MIN_GAP_S = 1.0
_API_HOSTS = {"boards-api.greenhouse.io": 0.35, "api.lever.co": 0.35, "api.ashbyhq.com": 0.35}


def _polite(url: str) -> None:
    host = urlsplit(url).netloc
    gap = time.monotonic() - _last_hit[host]
    min_gap = _API_HOSTS.get(host, MIN_GAP_S)
    if gap < min_gap:
        time.sleep(min_gap - gap)
    _last_hit[host] = time.monotonic()


def fetch_text(url: str, min_chars: int = 600) -> str | None:
    """Fetch a page and extract main text. Returns None on failure or if too little text was found
    (JS-rendered career portals return a shell with only boilerplate, which must not reach the model)."""
    import trafilatura

    _polite(url)
    try:
        with client(timeout=30.0) as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
            final_url = str(r.url)
    except httpx.HTTPError:
        return None
    text = trafilatura.extract(html, url=final_url, include_comments=False, include_tables=True, favor_recall=True)
    if not text or len(text) < min_chars:
        return None
    return text


# ---------- ATS-aware description fetching ----------

_GH_RE = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)")
_GH_EMBED_RE = re.compile(r"greenhouse\.io/embed/job_app\?.*?(?:for=([^&]+)).*?token=(\d+)")
_LEVER_RE = re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{20,})")
_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{20,})")
_WORKDAY_RE = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/(.+?)(?:[?#]|$)")
_SMARTRECRUITERS_RE = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)")
_ORACLE_RE = re.compile(r"https?://([^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/(\d+)")


def fetch_description(url: str) -> str | None:
    """Prefer structured ATS endpoints when the URL reveals one; fall back to page extraction."""
    from .normalize import html_to_text

    m = _GH_RE.search(url) or _GH_EMBED_RE.search(url)
    if m:
        board, jid = m.group(1), m.group(2)
        _polite(url)
        try:
            with client() as c:
                r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
                if r.status_code == 200:
                    return html_to_text(r.json().get("content")) or None
        except httpx.HTTPError:
            pass
    m = _LEVER_RE.search(url)
    if m:
        site, pid = m.group(1), m.group(2)
        _polite(url)
        try:
            with client() as c:
                r = c.get(f"https://api.lever.co/v0/postings/{site}/{pid}")
                if r.status_code == 200:
                    item = r.json()
                    parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
                    for lst in item.get("lists") or []:
                        parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
                    parts.append(item.get("additionalPlain") or "")
                    return "\n\n".join(p for p in parts if p) or None
        except httpx.HTTPError:
            pass
    m = _ASHBY_RE.search(url)
    if m:
        org, pid = m.group(1), m.group(2)
        _polite(url)
        try:
            with client() as c:
                r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
                if r.status_code == 200:
                    for item in r.json().get("jobs", []):
                        if item.get("id") == pid:
                            return item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")) or None
        except httpx.HTTPError:
            pass
    m = _WORKDAY_RE.search(url)
    if m:
        tenant, wd, site, path = m.groups()
        api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}"
        _polite(url)
        try:
            with client() as c:
                r = c.get(api, headers={"Accept": "application/json"})
                if r.status_code == 200:
                    info = (r.json() or {}).get("jobPostingInfo") or {}
                    text = html_to_text(info.get("jobDescription"))
                    if text:
                        extra = [f"Location: {info.get('location')}" if info.get("location") else "",
                                 f"Time type: {info.get('timeType')}" if info.get("timeType") else ""]
                        return "\n".join(x for x in extra if x) + "\n\n" + text
        except (httpx.HTTPError, ValueError):
            pass
    m = _SMARTRECRUITERS_RE.search(url)
    if m:
        company, pid = m.group(1), m.group(2)
        _polite(url)
        try:
            with client() as c:
                r = c.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{pid}")
                if r.status_code == 200:
                    sections = ((r.json() or {}).get("jobAd") or {}).get("sections") or {}
                    parts = [f"{v.get('title', '')}\n{html_to_text(v.get('text'))}" for v in sections.values() if isinstance(v, dict)]
                    text = "\n\n".join(p for p in parts if p.strip())
                    if text:
                        return text
        except (httpx.HTTPError, ValueError):
            pass
    m = _ORACLE_RE.search(url)
    if m:
        host, site, rid = m.groups()
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
               f"?expand=all&onlyData=true&finder=ByRequisitionId;Id=%22{rid}%22,siteNumber=%22{site}%22")
        _polite(url)
        try:
            with client() as c:
                r = c.get(api, headers={"Accept": "application/json"})
                if r.status_code == 200:
                    items = (r.json() or {}).get("items") or []
                    if items:
                        it = items[0]
                        parts = [html_to_text(it.get("ExternalDescriptionStr")), html_to_text(it.get("ExternalQualificationsStr")),
                                 html_to_text(it.get("ExternalResponsibilitiesStr"))]
                        loc = it.get("PrimaryLocation")
                        text = "\n\n".join(p for p in parts if p)
                        if text:
                            return (f"Location: {loc}\n\n" if loc else "") + text
        except (httpx.HTTPError, ValueError):
            pass
    return fetch_text(url)


# ---------- ATS probing for discovered companies ----------

def probe_ats(slugs: list[str]) -> tuple[str, str] | None:
    """Try candidate board tokens against Greenhouse, Lever and Ashby. Returns (ats_type, token) or None.
    A board that exists but is empty is remembered as a fallback; a non-empty one wins."""
    empty: tuple[str, str] | None = None
    with client(timeout=15.0) as c:
        for slug in slugs:
            for ats, url in (
                ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
                ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
                ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
            ):
                _polite(url)
                try:
                    r = c.get(url)
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    body = r.json()
                except ValueError:
                    continue
                jobs = body.get("jobs") if isinstance(body, dict) else (body if isinstance(body, list) else None)
                if jobs is None:
                    continue
                if jobs:
                    return ats, slug
                empty = empty or (ats, slug)
    return empty


def slug_candidates(name: str, domain: str | None) -> list[str]:
    from .normalize import slugify

    cands: list[str] = []
    if domain:
        base = domain.lower().split("/")[0]
        base = base[4:] if base.startswith("www.") else base
        cands.append(base.split(".")[0])
    s = slugify(name)
    cands += [s, s.replace("-", ""), s.split("-")[0]]
    seen: set[str] = set()
    return [c for c in cands if c and not (c in seen or seen.add(c))]
