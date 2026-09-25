"""Aggregator repos that publish a Markdown table in their README (no listings.json).

Handles the common layouts:
  | Company | Role | Location | Apply | Date |            (continuation rows use "↳" for the company)
  | Role | Links |  under a per-company heading            (e.g. northwesternfintech quant repo)
Every http(s) link in a row becomes one RawJob; badge/image links are ignored.
"""
from __future__ import annotations

import re

import httpx

from ..models import RawJob
from .base import client

_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_HTML_LINK_RE = re.compile(r"<a\s[^>]*href=\"(https?://[^\"]+)\"", re.I)
_IMG_HOST_RE = re.compile(r"img\.shields\.io|/images?/|\.(png|svg|gif|jpg)(\?|$)", re.I)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_ROLE_EXPAND = {"qd": "Quantitative Developer Intern", "qr": "Quantitative Researcher Intern", "qt": "Quantitative Trader Intern",
                "swe": "Software Engineer Intern", "hw": "Hardware Engineer Intern", "fpga": "FPGA Engineer Intern", "ds": "Data Scientist Intern"}


def _cells(line: str) -> list[str]:
    parts = line.strip().strip("|").split("|")
    return [p.strip() for p in parts]


_SYMBOLS_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F✓✅🔒]+")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    text = text.replace("**", "").replace("&nbsp;", " ")
    text = _SYMBOLS_RE.sub(" ", text)
    return " ".join(text.split()).strip(" -|")


def _is_site_root(url: str) -> bool:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    return u.path.strip("/") == "" and not u.query


class GithubReadmeSource:
    complete_listing = True  # rows are removed when postings close; absent jobs get deactivated (and re-activated if they return)

    def __init__(self, repo: str, branches: tuple[str, ...] = ("main", "dev", "master"), default_term: str | None = None) -> None:
        self.repo = repo
        self.branches = branches
        self.default_term = default_term
        self.name = f"readme:{repo}"

    def _readme(self) -> str:
        with client() as c:
            for b in self.branches:
                r = c.get(f"https://raw.githubusercontent.com/{self.repo}/{b}/README.md")
                if r.status_code == 200:
                    return r.text
        raise httpx.HTTPError(f"README not found for {self.repo}")

    def fetch(self) -> list[RawJob]:
        md = self._readme()
        jobs: list[RawJob] = []
        heading = ""
        header: list[str] | None = None
        last_company = ""
        for line in md.splitlines():
            h = _HEADING_RE.match(line)
            if h:
                heading = _clean(h.group(1))
                header = None
                continue
            if not line.lstrip().startswith("|"):
                if line.strip() == "":
                    header = None
                continue
            cells = _cells(line)
            if header is None:
                header = [c.lower() for c in cells]
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue  # separator row
            col = {name: i for i, name in enumerate(header)}
            def get(*names: str) -> str:
                for n in names:
                    for k, i in col.items():
                        if n in k and i < len(cells):
                            return cells[i]
                return ""
            company_raw = _clean(get("company", "firm", "employer"))
            if company_raw in ("", "↳", "→", "⮑"):
                company = last_company if "company" in " ".join(header) else heading
            else:
                company = company_raw
            last_company = company or last_company
            role = _clean(get("role", "title", "position", "job"))
            role = _ROLE_EXPAND.get(role.lower(), role)
            # Some lists append "(Company)" to the title; drop it when it just repeats the company.
            m = re.search(r"\s*\(([^()]{2,40})\)\s*$", role)
            if m and company and m.group(1).strip().lower() == company.strip().lower():
                role = role[: m.start()].rstrip()
            location = _clean(get("location", "city"))
            # Badge links look like [![Apply](https://img.shields.io/...)](https://real.url): flatten the inner image first.
            flat = re.sub(r"!\[[^\]]*\]\([^)]*\)", "Apply", line)
            flat = re.sub(r"<img[^>]*>", "Apply", flat)
            links = [(t, u) for t, u in _LINK_RE.findall(flat) if not _IMG_HOST_RE.search(u) and not _is_site_root(u)]
            links += [("", u) for u in _HTML_LINK_RE.findall(flat) if not _IMG_HOST_RE.search(u) and not _is_site_root(u)]
            if not company or not role or not links:
                continue
            for text, url in links:
                text = _clean(text)
                qualifier = f" ({text})" if text and len(text) < 30 and not re.fullmatch(r"[\W\s]*(apply|link|here|✅|🔒)?[\W\s]*", text, re.I) else ""
                jobs.append(RawJob(company_name=company, title=role + qualifier, location=location, url=url, source=self.name,
                                   terms=[self.default_term] if self.default_term else [], raw={"heading": heading}))
        return jobs
