"""Configuration: paths, profile loading, rubric versioning."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

# Bump when prompts/schemas/scoring semantics change so stale evaluations get re-scored.
RUBRIC_VERSION = "2"  # v2: tag-based summaries (fit_tags/gap_tags)

ROOT = Path(os.environ.get("JOBHUB_HOME", Path(__file__).resolve().parent.parent))
PROFILE_DIR = ROOT / "profile"
PROMPTS_DIR = ROOT / "prompts"
SCHEMAS_DIR = ROOT / "schemas"
DATA_DIR = ROOT / "data"
DIGESTS_DIR = ROOT / "digests"
DB_PATH = DATA_DIR / "jobhub.db"
LLM_CWD = DATA_DIR / "llm_cwd"  # empty cwd so headless claude doesn't auto-load CLAUDE.md

PROFILE_YAML = PROFILE_DIR / "profile.yaml"
PROFILE_MD = PROFILE_DIR / "profile.md"
COMPANIES_YAML = PROFILE_DIR / "companies.yaml"
DIRECTORY_CSV = ROOT / "directory" / "board_directory.csv"   # shared company -> job board table, maintained with the code


class Dealbreakers(BaseModel):
    unpaid: list[str] = ["unpaid", "volunteer position", "for academic credit only"]
    clearance: list[str] = ["security clearance", "top secret", "ts/sci", "secret clearance"]
    citizenship: list[str] = ["u.s. citizenship required", "must be a u.s. citizen", "us citizens only",
                              "u.s. citizens only", "must be a us citizen"]
    # Degree level is handled properly by `profile.degrees` + jobhub/degree.py, which reads the *accepted set*
    # and only rejects when its lowest member is above what you hold. The old defaults here were plain
    # substrings, so "currently pursuing a phd" also rejected "currently pursuing a PhD, MS or BS" and every
    # posting that mentions a PhD under "Preferred qualifications". Left empty and kept for anything else
    # level-shaped you want to reject outright.
    level: list[str] = []
    location_exclude: list[str] = []
    other: list[str] = []
    # Matched against an aggregator's structured sponsorship field (e.g. Simplify's "U.S. Citizenship is Required").
    sponsorship_values: list[str] = ["citizenship is required"]
    # Title words that mark a posting as out of scope — rejected before any fetch or model call (reason: out_of_scope).
    # Measured 2026-08-29: 0 false positives across visible buckets. Edit freely in profile.yaml.
    title_exclude: list[str] = [
        "accounting", "accountant", "audit", "tax", "finance analyst", "financial analyst", "marketing", "sales",
        "business development", "customer success", "recruiter", "recruiting", "human resources", "legal", "paralegal",
        "communications", "public relations", "graphic design", "ux", "ui/ux", "product design", "product manag",
        "program manag", "project manag", "supply chain", "procurement", "logistics", "operations analyst",
        "business analyst", "mechanical", "civil engineer", "structural", "chemical engineer", "materials engineer",
        "biomedical", "nurs", "pharmac", "clinical", "content", "social media", "copywrit", "journalis", "editorial",
        "video", "photograph", "event", "hospitality", "retail", "manufacturing engineer", "quality engineer",
        "industrial engineer", "process engineer", "environmental", "sustainability", "data analyst",
        "analytics intern", "business intelligence", "strategy intern", "consult",
        # Added 2026-09-02: frontend / full-stack product roles are out of scope (user request).
        "frontend", "front-end", "front end", "full stack", "full-stack", "fullstack", "web developer", "javascript",
        # Added 2026-09-02: civil/AEC postings that repeatedly reached the model (Olsson et al.).
        "water resources", "wastewater", "stormwater", "geotechnical", "land development", "land survey",
        "surveying", "construction administration", "construction management", "architectural", "transportation planning",
    ]


class LikelihoodWeights(BaseModel):
    skills_match: float = 0.65
    level_match: float = 0.35


class DesirabilityWeights(BaseModel):
    work_alignment: float = 0.45
    experience_quality: float = 0.30
    company_reputation: float = 0.25


class Scoring(BaseModel):
    likelihood: LikelihoodWeights = LikelihoodWeights()
    desirability: DesirabilityWeights = DesirabilityWeights()
    unknown_company_reputation: int = 50


class LikelyBucket(BaseModel):
    min_likelihood: int = 65
    min_desirability: int = 50


class ReachBucket(BaseModel):
    min_desirability: int = 70
    min_likelihood: int = 25


class WildcardBucket(BaseModel):
    min_interest: int = 70
    cap: int = 5


class Buckets(BaseModel):
    likely: LikelyBucket = LikelyBucket()
    reach: ReachBucket = ReachBucket()
    wildcard: WildcardBucket = WildcardBucket()


class LLMConfig(BaseModel):
    # Evaluation runs on a small model: it scores against a fixed rubric and is by far the bulk of spend.
    model: str | None = "haiku"
    effort: str | None = "low"
    batch_size: int = 24
    concurrency: int = 2
    timeout_seconds: int = 900
    # Discovery does web research a few times a week — it must NOT inherit the cheap evaluation model.
    # (Was high / Claude Code default: 13 angles on a frontier model is what exhausted the session limit.)
    discovery_effort: str | None = "medium"
    discovery_model: str | None = "sonnet"     # "__claude_code_default__" -> omit --model, use your Claude Code default
    discovery_max_angles: int = 3              # angles per `jobhub discover` run; the rest rotate in on later runs (0 = all)
    # Two-stage triage: a cheap pass that archives obvious misses before the full rubric runs.
    triage_enabled: bool = True                 # calibrate with `jobhub triage --dry-run` before changing
    triage_model: str | None = "haiku"
    triage_effort: str | None = "low"
    triage_batch_size: int = 40
    triage_min_score: int = 35                  # below this, archive without a full evaluation
    triage_desc_chars: int = 500
    # Token budgets bound only the *refinement* of ambiguous jobs: every job is ingested and gets a free local score
    # regardless, and anything the budget leaves unrefined is listed on the Pipeline page and the Jobs banner.
    run_token_budget: int = 300_000             # input+output tokens one `jobhub score` / `run` may spend on triage; 0 = unlimited
    weekly_token_budget: int = 0                # optional rolling 7-day ceiling across every recorded run; 0 = off (default)


class RoleTypePick(BaseModel):
    """One role type the user is targeting, in `profile.yaml: role_types`.

    `key` is either a shipped catalog key (`rolecatalog.CATALOG`) — in which case the label and keywords come
    from the catalog and only `weight` matters — or a key of the user's own, which must bring its own
    `keywords`. The list is the answer to "what do you want", and everything downstream derives from it: the
    fast score's base weight, the keyword bonus, and the `target_roles` the model is shown.
    """
    key: str
    weight: int = 70
    label: str = ""             # blank -> the catalog's label
    keywords: list[str] = []    # non-empty -> a custom type; catalog entries leave this empty

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = re.sub(r"[^a-z0-9-]+", "-", str(v).strip().lower()).strip("-")
        if not v:
            raise ValueError("a role type needs a key")
        return v

    @field_validator("weight")
    @classmethod
    def _range(cls, v: int) -> int:
        return max(0, min(100, int(v)))

    @field_validator("keywords")
    @classmethod
    def _usable(cls, v: list[str]) -> list[str]:
        """`roletype._alternation` wraps the whole alternation in one boundary pair, so a keyword that does not
        start and end alphanumeric would silently never match. Drop those rather than assert at page-load time."""
        return [w for w in (str(x).strip().lower() for x in v) if w and w[:1].isalnum() and w[-1:].isalnum()]

    def resolved_label(self) -> str:
        from .rolecatalog import BY_KEY

        return self.label or (BY_KEY[self.key].label if self.key in BY_KEY else self.key)

# Plain "this is a programming job" words. The rubric's Likely bucket is mostly generic software internships, and what
# separates those from the Archive (cyber, medical, analyst, test/repair, "engineering intern") is exactly this.
DEFAULT_SOFTWARE_KEYWORDS = [
    "software", "developer", "swe", "sde", "programmer", "programming", "coding", "computer science", "computer engineering",
    "backend", "back-end", "c++", "python", "rust", "java", "golang", "algorithms", "data structures",
]

# Titles that name work outside the target space. Case-insensitive, bounded at word starts.
DEFAULT_NEGATIVE_TITLE = [
    "sales", "recruit", "marketing", "human resources", "hr ", "finance", "accounting", "legal", "paralegal",
    "customer success", "customer support", "business development", "product manager", "program manager",
    "project manager", "designer", "ux", "ui ", "front-end", "frontend", "front end", "full-stack", "full stack",
    "fullstack", "web developer", "wordpress", "content", "social media", "operations", "supply chain",
    "civil", "mechanical", "chemical", "biomedical", "environmental", "cyber", "cybersecurity", "medical", "analyst",
    "repair", "audit", "tax", "actuarial", "underwriting", "claims", "electrical", "manufacturing", "industrial",
    "process engineer", "quality engineer", "gas", "first nations", "apprentice",
]


class FastScoring(BaseModel):
    """Free, deterministic first-pass scoring (jobhub/fastscore.py). None of this is model-visible, so editing
    it never changes `profile_hash`; `jobhub rescore --recompute-only` re-applies it to stored rows for free."""
    enabled: bool = True
    #: Base score for a posting whose role type the user neither picked nor excluded. It is deliberately the old
    #: `general` weight: not targeting something is not the same as rejecting it, so an unpicked type scores like
    #: any unremarkable software posting and stays visible in the chip row with an honest badge.
    neutral_weight: int = 36
    excluded_weight: int = 12              # base score for a type in `excluded_role_types`
    title_domain_points: int = 8           # per distinct keyword of a *picked* role type in the title
    title_domain_cap: int = 16
    desc_domain_points: int = 3            # per distinct picked-role-type keyword in the description head
    desc_domain_cap: int = 12
    desc_chars: int = 600                  # how much of the description the local scorer reads
    reputation_weight: float = 0.3         # (company reputation - 50) * this, added to the score
    software_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_SOFTWARE_KEYWORDS))
    software_title_bonus: int = 14         # any software keyword in the title
    software_desc_points: int = 3          # per distinct software keyword in the description head
    software_desc_cap: int = 9
    negative_title: list[str] = Field(default_factory=lambda: list(DEFAULT_NEGATIVE_TITLE))
    negative_title_penalty: int = 30
    hi: int = 64                           # local score >= hi: clear yes, no model call
    lo: int = 36                           # local score <= lo: clear no, no model call
    triage_weight: float = 0.7             # final = triage_weight * triage + (1 - triage_weight) * local
    likely_min: int = 68                   # final score -> Likely
    reach_min: int = 50                    # final score -> Reach; below is Archive
    unread_to_triage: bool = True          # postings with no readable description are never a clear "no" unless the title is off-target
    digest_cap: int = 80                   # rows per digest section (Likely / Reach), best first; 0 = no cap


class SourcesConfig(BaseModel):
    simplify_repos: list[str] = []          # repos with .github/scripts/listings.json (Simplify format)
    readme_repos: list[str] = []            # repos whose README holds a Markdown table of postings
    enable_generic: bool = False


class Profile(BaseModel):
    #: What you are targeting, best first: entries picked from `rolecatalog.CATALOG` plus any custom ones.
    #: This one list replaces the three places "what I want" used to live (the prose `roles`, the
    #: `fast_scoring.role_weights` numbers and the `target_domains` keyword lists) — see the migration in
    #: `load_profile`. Model-visible via `target_roles`, so editing it re-scores.
    role_types: list[RoleTypePick] = []
    #: Catalog keys you actively do not want. They still classify (the badge stays honest) but score
    #: `fast_scoring.excluded_weight`, which lands them in Archive.
    excluded_role_types: list[str] = []
    #: Degrees held or currently being pursued (`associate` / `bachelor` / `master` / `phd`). The highest one
    #: is a ceiling: `jobhub/degree.py` hard-rejects postings whose *lowest* accepted degree is above it, so a
    #: "PhD, Quantitative Software Engineer" goes but a "BS/MS" one stays. Empty list = no degree filtering.
    #: Not model-visible on purpose — the deterministic filter is complete and a hash change would re-score
    #: everything (the deep rubric never sees these postings, since prefilter rejects them first).
    degrees: list[str] = ["bachelor"]
    term: str = ""
    term_also_accept: list[str] = []          # other term labels that mean the same start window (e.g. US "Spring 2027")
    alt_terms: list[str] = []                 # a second acceptable work term, tracked separately (e.g. "Summer 2027")
    length_weeks: list[int | None] = Field(default_factory=lambda: [12, None])  # [min, max]; None = no upper bound
    locations: list[str] = []
    remote_ok: bool = True
    strict_location: bool = False
    work_authorization: list[str] = []
    dealbreakers: Dealbreakers = Dealbreakers()
    scoring: Scoring = Scoring()
    buckets: Buckets = Buckets()
    llm: LLMConfig = LLMConfig()
    fast_scoring: FastScoring = FastScoring()
    sources: SourcesConfig = SourcesConfig()

    @field_validator("role_types")
    @classmethod
    def _one_entry_per_key(cls, v: list[RoleTypePick]) -> list[RoleTypePick]:
        """Two entries for one key would make the weight depend on dict ordering. Last wins, as in YAML."""
        by_key = {r.key: r for r in v}
        return list(by_key.values())

    # ---- what the rest of the code asks a profile about its role types ----

    def picked(self) -> dict[str, RoleTypePick]:
        return {r.key: r for r in self.role_types}

    def role_weight(self, key: str) -> int:
        """The fast score's starting point for a posting of this type: your weight if you picked it, a floor if
        you excluded it, and otherwise neutral — not picking something is not the same as rejecting it."""
        pick = self.picked().get(key)
        if pick is not None:
            return pick.weight
        if key in self.excluded_role_types:
            return self.fast_scoring.excluded_weight
        return self.fast_scoring.neutral_weight

    def picked_keywords(self) -> list[str]:
        """Every keyword of every picked type — what the title/description bonus is scored against. Only picked
        types contribute, which is what makes a pick worth more than the base weight alone."""
        from .rolecatalog import BY_KEY

        out: list[str] = []
        for r in self.role_types:
            out += r.keywords or list(BY_KEY[r.key].keywords if r.key in BY_KEY else ())
        return sorted(set(out))

    def target_role_labels(self) -> list[str]:
        return [r.resolved_label() for r in self.role_types]

    @field_validator("degrees")
    @classmethod
    def _known_degrees(cls, v: list[str]) -> list[str]:
        """A typo ("bachelors", "BSc") would silently switch the degree filter off, so reject it loudly."""
        from .degree import LEVELS

        out = [str(d).strip().lower() for d in v if str(d).strip()]
        bad = [d for d in out if d not in LEVELS]
        if bad:
            raise ValueError(f"unknown degree(s) {bad}; use any of {sorted(LEVELS, key=LEVELS.get)}")
        return out


def _migrate_role_types(data: dict[str, Any]) -> None:
    """Build `role_types` from a pre-catalog profile, in place.

    Before the catalog, "what I want" was `fast_scoring.role_weights` — eight keys hardcoded to one person's
    taste. Widening the catalog split some of those (Compilers out of GPU, Databases/Networking/Cloud out of
    Systems, and so on), so a straight read would drop a split-out type to the neutral weight and silently
    archive postings that used to score well. Each split-out type therefore inherits its parent's old weight:
    the migration is score-preserving, and the user re-picks deliberately from the UI afterwards.
    """
    from .rolecatalog import GENERAL, SPLIT_FROM

    old = (data.get("fast_scoring") or {}).get("role_weights")
    if data.get("role_types") is not None or not isinstance(old, dict):
        return
    weights = {str(k): int(v) for k, v in old.items()}
    for new_key, parent in SPLIT_FROM.items():
        if parent in weights:
            weights.setdefault(new_key, weights[parent])
    neutral = weights.pop(GENERAL, None)
    if neutral is not None:
        data.setdefault("fast_scoring", {})["neutral_weight"] = neutral
    # Anything at or below the old "general" score was a way of saying "not this", which now has its own list.
    floor = neutral if neutral is not None else 36
    data["excluded_role_types"] = sorted(k for k, w in weights.items() if w <= floor)
    data["role_types"] = [{"key": k, "weight": w}
                          for k, w in sorted(weights.items(), key=lambda kv: -kv[1]) if w > floor]


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    data: dict[str, Any] = {}
    if PROFILE_YAML.exists():
        data = yaml.safe_load(PROFILE_YAML.read_text()) or {}
    _migrate_role_types(data)
    return Profile.model_validate(data)


#: Knobs the web UI may edit in place. None of them is model-visible, so changing one never alters `profile_hash`
#: (nothing gets re-evaluated). Everything else is edited in profile.yaml by hand.
EDITABLE_KNOBS: dict[str, type] = {
    "llm.run_token_budget": int, "llm.weekly_token_budget": int, "llm.triage_enabled": bool, "llm.triage_min_score": int,
    "llm.triage_batch_size": int, "llm.triage_desc_chars": int, "llm.concurrency": int, "llm.discovery_max_angles": int,
    "fast_scoring.enabled": bool, "fast_scoring.hi": int, "fast_scoring.lo": int, "fast_scoring.likely_min": int,
    "fast_scoring.reach_min": int, "fast_scoring.digest_cap": int, "fast_scoring.triage_weight": float,
    "fast_scoring.unread_to_triage": bool,
}


#: Profile constraints the UI may edit. Unlike EDITABLE_KNOBS these ARE model-visible, so saving one
#: changes `profile_hash` and everything gets re-scored — free, but the UI has to say so.
#: value = (kind, is_model_visible)
PROFILE_FIELDS: dict[str, tuple[str, bool]] = {
    "term": ("str", True),
    "term_also_accept": ("list", True),
    "alt_terms": ("list", True),
    "locations": ("list", True),
    "remote_ok": ("bool", True),
    "strict_location": ("bool", True),
    "work_authorization": ("list", True),
    # `roles` is gone: what you want is `role_types`, edited through set_role_types (a list of mappings, which
    # the one-line rewriter below cannot express) and shown to the model as derived `target_roles`.
    # Not model-visible: the deterministic filter in jobhub/degree.py is what acts on it, so editing it
    # re-scores nothing. Apply it to jobs already scored with `jobhub rescore --recompute-only`.
    "degrees": ("list", False),
    "dealbreakers.unpaid": ("list", False),
    "dealbreakers.clearance": ("list", False),
    "dealbreakers.citizenship": ("list", False),
    "dealbreakers.level": ("list", False),
    "dealbreakers.location_exclude": ("list", False),
    "dealbreakers.other": ("list", False),
}


def _yaml_scalar(v: Any) -> str:
    return "true" if v is True else "false" if v is False else json.dumps(str(v))


def set_profile_field(dotted: str, raw: Any) -> Any:
    """Rewrite one profile constraint in profile.yaml, keeping the file's comments.

    Lists are always written as a single flow sequence (`["a", "b"]`) — profile.yaml already mixes
    flow and block style, and collapsing to one line is what makes replacing a block list safe.
    The whole profile is re-validated and rolled back if the edit produces something invalid.
    """
    import re

    if dotted not in PROFILE_FIELDS:
        raise KeyError(f"{dotted} is not editable from the UI")
    kind, _visible = PROFILE_FIELDS[dotted]
    if kind == "bool":
        value: Any = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        text = _yaml_scalar(value)
    elif kind == "list":
        items = raw if isinstance(raw, list) else [p.strip() for p in str(raw).split(",")]
        value = [str(i).strip() for i in items if str(i).strip()]
        text = "[" + ", ".join(json.dumps(i) for i in value) + "]"
    else:
        value = str(raw).strip()
        text = _yaml_scalar(value)

    parts = dotted.split(".")
    lines = PROFILE_YAML.read_text().splitlines(keepends=True)
    original = "".join(lines)

    if len(parts) == 1:
        key, indent, lo, hi = parts[0], "", 0, len(lines)
    else:                                   # one level down, e.g. dealbreakers.other
        parent, key = parts
        lo = next((i for i, l in enumerate(lines) if re.match(rf"^{parent}:\s*(#.*)?$", l)), None)
        if lo is None:
            raise KeyError(f"section {parent}: not found in profile.yaml")
        lo += 1
        hi = next((i for i in range(lo, len(lines))
                   if lines[i].strip() and not lines[i].startswith((" ", "\t"))), len(lines))
        indent = "  "

    at = next((i for i in range(lo, hi) if re.match(rf"^{indent}{re.escape(key)}:", lines[i])), None)
    if at is None:
        raise KeyError(f"{dotted} not found in profile.yaml")

    m = re.match(rf"^({indent}{re.escape(key)}:)([^#\n]*)(#.*)?(\r?\n?)$", lines[at])
    comment = f"   {m.group(3)}" if m and m.group(3) else ""
    end = at + 1                            # swallow a block list so it cannot survive as duplicate entries
    while end < hi and re.match(rf"^{indent}\s+-\s", lines[end]):
        end += 1
    lines[at:end] = [f"{indent}{key}: {text}{comment}\n"]

    PROFILE_YAML.write_text("".join(lines))
    load_profile.cache_clear()
    try:
        load_profile()
    except Exception:
        PROFILE_YAML.write_text(original)
        load_profile.cache_clear()
        raise
    return value


def set_profile_value(dotted: str, raw: Any) -> Any:
    """Rewrite one scalar in profile.yaml in place (keeping comments), validate the whole profile, and roll back on
    failure. Returns the parsed value. Only keys in EDITABLE_KNOBS are accepted."""
    import re

    if dotted not in EDITABLE_KNOBS:
        raise KeyError(f"{dotted} is not editable from the UI")
    kind = EDITABLE_KNOBS[dotted]
    if kind is bool:
        value: Any = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    else:
        value = kind(str(raw).replace(",", "").replace("_", "").strip())
    section, key = dotted.split(".")
    lines = PROFILE_YAML.read_text().splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"^{section}:\s*(#.*)?$", l)), None)
    if start is None:
        raise KeyError(f"section {section}: not found in profile.yaml")
    text = "true" if value is True else "false" if value is False else str(value)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            break   # next top-level key: the setting isn't in this section
        m = re.match(rf"^(\s+){key}:\s*[^#\n]*?(\s*)(#.*)?(\r?\n?)$", lines[i])
        if m:
            comment = f"{m.group(2) or '  '}{m.group(3)}" if m.group(3) else ""
            new = f"{m.group(1)}{key}: {text}{comment}{m.group(4) or chr(10)}"
            break
    else:
        lines.insert(start + 1, f"  {key}: {text}\n")   # not present yet: add it at the top of the section
        new = None
        i = start + 1
    original = PROFILE_YAML.read_text()
    if new is not None:
        lines[i] = new
    PROFILE_YAML.write_text("".join(lines))
    load_profile.cache_clear()
    try:
        load_profile()
    except Exception:
        PROFILE_YAML.write_text(original)
        load_profile.cache_clear()
        raise
    return value


def _replace_block(lines: list[str], key: str, body: list[str]) -> list[str]:
    """Replace a top-level `key:` and the indented block under it with `body`, appending the key if absent.

    `set_profile_field` rewrites a key to a single line, which cannot express a list of mappings. This is the
    multi-line counterpart: same idea (comments elsewhere in the file survive, the key's own trailing comment
    is kept), but the replacement spans lines.
    """
    at = next((i for i, l in enumerate(lines) if re.match(rf"^{re.escape(key)}:", l)), None)
    if at is None:
        return lines + (["\n"] if lines and lines[-1].strip() else []) + body
    m = re.match(rf"^{re.escape(key)}:[^#\n]*(#.*)?$", lines[at].rstrip("\n"))
    if m and m.group(1) and "#" not in body[0]:     # the generated line may carry its own comment already
        body = [body[0].rstrip("\n") + f"   {m.group(1)}\n"] + body[1:]
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        end += 1
    while end - 1 > at and not lines[end - 1].strip():   # leave the blank line that separates the next section
        end -= 1
    return lines[:at] + body + lines[end:]


def set_role_types(picks: list[dict[str, Any]], excluded: list[str]) -> Profile:
    """Rewrite `role_types:` and `excluded_role_types:` in profile.yaml, validate, roll back on failure.

    Written as a block list of flow mappings (`- {key: gpu, weight: 80}`) so one entry is one line: readable
    by hand, and cheap to regenerate wholesale every time the UI saves.
    """
    from .rolecatalog import BY_KEY

    clean: list[RoleTypePick] = []
    for p in picks:
        pick = RoleTypePick.model_validate(p)
        if pick.key not in BY_KEY and not pick.keywords:
            raise ValueError(f"{pick.key!r} is not in the catalog, so it needs keywords of its own")
        if pick.key in BY_KEY and not pick.label:
            pick.label = ""          # keep following the catalog's label rather than freezing today's
        clean.append(pick)
    excluded = sorted({re.sub(r"[^a-z0-9-]+", "-", str(e).strip().lower()).strip("-") for e in excluded} - {""}
                      - {p.key for p in clean})

    body = ["role_types:                 # what you are targeting, best first; picked from jobhub/rolecatalog.py\n"]
    for p in clean:
        bits = [f"key: {p.key}", f"weight: {p.weight}"]
        if p.label:
            bits.append(f"label: {json.dumps(p.label)}")
        if p.keywords:
            bits.append("keywords: [" + ", ".join(json.dumps(k) for k in p.keywords) + "]")
        body.append("  - {" + ", ".join(bits) + "}\n")
    if not clean:
        body = ["role_types: []              # nothing picked yet: every posting scores the neutral weight\n"]
    body.append("excluded_role_types: [" + ", ".join(json.dumps(e) for e in excluded) + "]"
                "   # classified and badged as usual, but scored fast_scoring.excluded_weight\n")

    original = PROFILE_YAML.read_text()
    lines = original.splitlines(keepends=True)
    lines = _replace_block(lines, "role_types", body[:-1])
    lines = _replace_block(lines, "excluded_role_types", body[-1:])
    PROFILE_YAML.write_text("".join(lines))
    load_profile.cache_clear()
    try:
        return load_profile()
    except Exception:
        PROFILE_YAML.write_text(original)
        load_profile.cache_clear()
        raise


def load_profile_md() -> str:
    return PROFILE_MD.read_text() if PROFILE_MD.exists() else ""


def save_profile_md(text: str) -> str:
    """Write profile.md from the UI. It is prose the model reads, not config, so there is nothing to validate —
    but it is also part of `profile_hash`, so saving it re-scores."""
    text = str(text).replace("\r\n", "\n").rstrip() + "\n"
    PROFILE_MD.write_text(text)
    return text


def load_companies_yaml() -> dict[str, Any]:
    if not COMPANIES_YAML.exists():
        return {}
    return yaml.safe_load(COMPANIES_YAML.read_text()) or {}


def model_visible_constraints(profile: Profile) -> dict[str, Any]:
    """The subset of profile.yaml the model is shown. Scoring weights/buckets are NOT included, so
    retuning them never triggers re-evaluation (see `jobhub rescore`, which recomputes locally)."""
    lw = list(profile.length_weeks) + [None, None]
    return {
        # Derived from role_types rather than a separate hand-typed list: one place to say what you want.
        "target_roles": profile.target_role_labels(), "work_term": profile.term,
        # alt_terms are equally acceptable to the model — the tracks are separated downstream, in code.
        "acceptable_term_labels": [profile.term] + profile.term_also_accept + profile.alt_terms,
        "min_length_weeks": lw[0], "max_length_weeks": lw[1],
        "longer_placements_starting_in_term_ok": lw[1] is None,
        "preferred_locations": profile.locations, "remote_ok": profile.remote_ok,
        "locations_are_hard_constraint": profile.strict_location, "work_authorization": profile.work_authorization,
    }


def profile_hash() -> str:
    """Hash of what the model sees (profile.md + model-visible constraints); stale evals re-run when it changes."""
    import json

    h = hashlib.sha1()
    h.update(json.dumps(model_visible_constraints(load_profile()), sort_keys=True).encode())
    h.update(PROFILE_MD.read_bytes() if PROFILE_MD.exists() else b"")
    return h.hexdigest()[:12]


def read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def read_schema(name: str) -> dict[str, Any]:
    import json

    return json.loads((SCHEMAS_DIR / name).read_text())


def ensure_dirs() -> None:
    for d in (DATA_DIR, DIGESTS_DIR, LLM_CWD):
        d.mkdir(parents=True, exist_ok=True)
