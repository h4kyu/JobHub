"""Configuration: paths, profile loading, rubric versioning."""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

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
    level: list[str] = ["phd required", "phd candidates only", "currently pursuing a phd"]
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


class TargetDomain(BaseModel):
    """An aspirational field: postings here are mined by `jobhub skills` for what to go learn,
    whatever bucket they landed in. Not part of model_visible_constraints, so editing these
    never triggers re-evaluation."""
    name: str
    keywords: list[str] = []


DEFAULT_TARGET_DOMAINS = [
    TargetDomain(name="GPU / accelerators", keywords=[
        "cuda", "gpu kernel", "tensorrt", "triton", "rocm", "hip", "accelerator", "cudnn", "cutlass",
        "inference optimization", "quantization", "tensor core"]),
    TargetDomain(name="Compilers / runtimes", keywords=[
        "compiler", "llvm", "mlir", "codegen", "code generation", "jit", "intermediate representation",
        "toolchain", "interpreter", "language runtime", "garbage collect"]),
    TargetDomain(name="Performance engineering", keywords=[
        "low latency", "low-latency", "performance optimization", "profiling", "simd", "avx", "vectoriz",
        "cache-friendly", "cache locality", "throughput", "microarchitecture", "lock-free", "nanosecond",
        "real-time constraints", "hot path"]),
    TargetDomain(name="HFT / quant systems", keywords=[
        "high frequency trading", "high-frequency", "market data", "order book", "matching engine",
        "trading system", "quantitative developer", "exchange connectivity", "kernel bypass", "fpga trading"]),
    TargetDomain(name="Systems / infrastructure", keywords=[
        "distributed system", "consensus", "raft", "paxos", "storage engine", "database internals",
        "query engine", "operating system", "kernel development", "networking stack", "rpc framework",
        "concurrency primitives", "scheduler"]),
]


DEFAULT_ROLE_WEIGHTS: dict[str, int] = {
    "gpu": 80, "quant": 78, "perf": 74, "robotics": 72, "systems": 66, "ml": 62, "general": 36, "hardware": 20,
}

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
    role_weights: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_ROLE_WEIGHTS))  # base score per role type
    title_domain_points: int = 8           # per distinct target_domains keyword in the title
    title_domain_cap: int = 16
    desc_domain_points: int = 3            # per distinct target_domains keyword in the description head
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
    roles: list[str] = []
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
    target_domains: list[TargetDomain] = Field(default_factory=lambda: list(DEFAULT_TARGET_DOMAINS))


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    data: dict[str, Any] = {}
    if PROFILE_YAML.exists():
        data = yaml.safe_load(PROFILE_YAML.read_text()) or {}
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


def load_profile_md() -> str:
    return PROFILE_MD.read_text() if PROFILE_MD.exists() else ""


def load_companies_yaml() -> dict[str, Any]:
    if not COMPANIES_YAML.exists():
        return {}
    return yaml.safe_load(COMPANIES_YAML.read_text()) or {}


def model_visible_constraints(profile: Profile) -> dict[str, Any]:
    """The subset of profile.yaml the model is shown. Scoring weights/buckets are NOT included, so
    retuning them never triggers re-evaluation (see `jobhub rescore`, which recomputes locally)."""
    lw = list(profile.length_weeks) + [None, None]
    return {
        "target_roles": profile.roles, "work_term": profile.term,
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
