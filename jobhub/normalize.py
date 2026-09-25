"""URL canonicalization, dedup keys, text cleanup."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "gh_src", "gh_jid", "lever-source", "lever-origin", "ref", "source", "src", "referrer",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "mc_cid", "mc_eid", "trk", "trackingid", "refid", "sid", "share",
}
_TRACKING_HOSTS = {"simplify.jobs": "simplify"}  # aggregator redirectors; handled by sources


def canonical_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    query.sort()
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def slugify(name: str) -> str:
    s = re.sub(r"[\(\[].*?[\)\]]", " ", name.lower()).strip()  # drop "(Vancouver)"-style qualifiers
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


_TITLE_NOISE = re.compile(
    r"\b(20\d\d|summer|fall|spring|winter|internship|intern|co-op|coop|remote|hybrid|onsite|on-site|"
    r"usa|us|canada|new grad|graduate|undergraduate|student|program|full[- ]?time|part[- ]?time)\b",
    re.I,
)


def _stem(word: str) -> str:
    """Tiny suffix stripper so 'engineer'/'engineering', 'systems'/'system' collapse together."""
    for suf in ("ing", "er", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            word = word[: -len(suf)]
    return word


def normalize_title(title: str) -> str:
    t = html.unescape(title).lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)  # drop parenthetical qualifiers
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(_stem(w) for w in t.split())


_LOC_NOISE = re.compile(r"\b(united states|usa|u\.s\.a?\.?|us|canada|remote in|remote|hybrid|on-?site|in-?person|office|hq|metro area|area)\b")


def normalize_location(location: str) -> str:
    """City-level key: 'New York, NY' == 'New York' == 'NYC'; the first listed city wins for multi-location postings."""
    loc = html.unescape(location or "").lower()
    first = re.split(r"[;|/]|\band\b|\bor\b", loc)[0]
    city = first.split(",")[0]
    city = _LOC_NOISE.sub(" ", city)
    city = re.sub(r"[^a-z0-9]+", " ", city).strip()
    aliases = {"nyc": "new york", "new york city": "new york", "sf": "san francisco", "bay area": "san francisco",
               "la": "los angeles", "dc": "washington", "washington dc": "washington", "": "remote"}
    return aliases.get(city, city)


def dedup_key(company_name: str, title: str, location: str) -> str:
    base = "|".join((slugify(company_name), normalize_title(title), normalize_location(location)))
    return hashlib.sha1(base.encode()).hexdigest()


def text_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article)[^>]*>", re.I)


def html_to_text(s: str | None) -> str:
    """Cheap HTML -> text for ATS descriptions (which are simple markup). Handles double-escaped HTML."""
    if not s:
        return ""
    s = html.unescape(s)
    if "&lt;" in s and "<" not in s:
        s = html.unescape(s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    lines = [" ".join(line.split()) for line in s.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
