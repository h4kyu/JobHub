"""Role-type classification for the web UI's filter chips and the fast score's base weight.

Deterministic and free — no model calls, no re-evaluation. The rubric has no category field, so this reads
what is already stored: the title (strongest signal), the model's `fit_tags`, and its summary. Descriptions
are deliberately left out: they mention everything (a robotics posting's "nice to have: CUDA"), and scanning
~12K of them on every page load is slow.

The types themselves live in `rolecatalog.py` (shipped, broad) plus whatever custom types the user typed
into `profile.yaml: role_types`. Classification runs against **the whole catalog**, not just the user's
picks, so a posting the user did not target still gets an honest badge ("Security / AppSec") instead of
being dumped into General — what the picks change is the *weight* (`config.role_weight`), not the label.

Each posting gets exactly one type. A known trading firm is always HFT / Quant. Otherwise the title decides
whenever it matches anything, ties going to the earlier (more specific) entry — custom types first, then
catalog order. Only a title that matches nothing falls back to fit_tags and summary.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .rolecatalog import ALL, GENERAL, RoleType, label  # noqa: F401  (re-exported for callers)

TAG_WEIGHT, SUMMARY_WEIGHT = 2, 1
MIN_FALLBACK_SCORE = 2   # with no title match, one summary mention alone is not enough to leave "general"

# Firms where nearly every engineering role sits next to trading, whatever the title says
# ("Software Developer Intern" at DRW). Matched as a bounded substring of the company name.
QUANT_FIRMS = (
    "jane street", "drw", "hudson river trading", "hrt", "imc", "optiver", "citadel", "jump trading", "virtu",
    "tower research", "point72", "de shaw", "d. e. shaw", "d.e. shaw", "five rings", "pdt", "pdtpartners",
    "two sigma", "susquehanna", "sig", "akuna", "old mission", "radix", "xtx", "headlands", "aquatic capital",
    "hyannis port", "hpr", "belvedere", "chicago trading", "flow traders", "maven securities", "millennium",
    "balyasny", "squarepoint", "wolverine", "peak6", "arrowstreet", "renaissance technologies", "cubist", "qube",
    "voleon", "gts", "tradeweb", "sparta", "geneva trading", "vatic", "hehmeyer", "optiver", "transmarket",
    "da vinci", "mako", "quantlab", "g-research", "man group", "garda", "valkyrie", "bridgewater",
)


# Longest keyword first, so "ai inference" matches as one phrase instead of "ai" then "inference".
# Case-sensitive over lowered text, with one boundary pair around the whole alternation instead of one per
# keyword (every keyword starts and ends alphanumeric): both are several times faster in CPython, and this runs
# over every evaluated posting on each page load.
def _alternation(words) -> re.Pattern[str]:
    assert all(w[:1].isalnum() and w[-1:].isalnum() for w in words), "keywords must start and end alphanumeric"
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                      + r")(?![a-z0-9])")


_FIRMS = _alternation(QUANT_FIRMS)


def _distinct(rx: re.Pattern[str], text: str | None) -> int:
    return len(set(rx.findall(text))) if text else 0


def is_quant_firm(company: str | None) -> bool:
    return bool(company and _FIRMS.search(company.lower()))


#: (key, label, compiled keywords) in tie-break order, cached per distinct set of custom types. The cache key
#: is the custom types alone — the shipped catalog cannot change at runtime — so the common case (no custom
#: types) compiles ~30 alternations once for the process.
@lru_cache(maxsize=8)
def _compiled(custom: tuple[tuple[str, str, tuple[str, ...]], ...]) -> tuple[tuple, ...]:
    types: list[RoleType] = [RoleType(k, lbl, "Yours", kw) for k, lbl, kw in custom if kw]
    types += [t for t in ALL if t.keywords and t.key not in {c[0] for c in custom}]
    return tuple((t.key, t.label, _alternation(t.keywords), t.decisive) for t in types)


def custom_signature(profile) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The hashable part of a profile that changes how classification works: the custom types only."""
    return tuple((r.key, r.label or r.key, tuple(sorted(r.keywords)))
                 for r in getattr(profile, "role_types", []) if r.keywords)


def types_for(profile=None) -> tuple[tuple, ...]:
    return _compiled(custom_signature(profile) if profile is not None else ())


def labels(profile=None) -> dict[str, str]:
    """Every key that can come out of `classify`, mapped to its display label."""
    out = {t[0]: t[1] for t in types_for(profile)}
    out[GENERAL] = label(GENERAL)
    return out


def keys(profile=None) -> tuple[str, ...]:
    return tuple(t[0] for t in types_for(profile)) + (GENERAL,)


def classify(title: str, fit_tags: list[str] | None = None, summary: str | None = None,
             company: str | None = None, profile=None) -> str:
    patterns = types_for(profile)
    order = {key: i for i, (key, _, _, _) in enumerate(patterns)}
    title = (title or "").lower()
    title_hits = {key: _distinct(rx, title) for key, _, rx, _ in patterns}
    # A decisive type settles it before anything else, including the company override: a posting that says
    # "Quantitative Researcher" is that job whether or not the employer is a known trading firm.
    for key, _, _, decisive in patterns:
        if decisive and title_hits[key]:
            return key
    # At a trading firm nearly every engineering role sits next to trading, whatever the title says
    # ("Linux Engineer Intern" at Jane Street is a quant job).
    if is_quant_firm(company):
        return "quant"
    if any(title_hits.values()):
        return max(order, key=lambda k: (title_hits[k], -order[k]))
    tags, summary = " | ".join(fit_tags or []).lower(), (summary or "").lower()
    fallback = {key: TAG_WEIGHT * _distinct(rx, tags) + SUMMARY_WEIGHT * _distinct(rx, summary)
                for key, _, rx, _ in patterns}
    best = max(order, key=lambda k: (fallback[k], -order[k]))
    return best if fallback[best] >= MIN_FALLBACK_SCORE else GENERAL
