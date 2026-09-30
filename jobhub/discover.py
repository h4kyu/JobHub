"""Company discovery (web research via headless Claude Code) and ATS resolution."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable

from pydantic import ValidationError

from . import config, db, fetch
from .config import Profile
from .llm.base import LLMBackend, LLMError, LLMRateLimitError
from .models import ATSResolution, DiscoveredCompany, DiscoveryOutput
from .normalize import slugify

DISCOVERY_TOOLS = ("WebSearch", "WebFetch")


def default_angles(profile: Profile) -> list[str]:
    """One press should be thorough: cover every role, both work directions, and every wildcard domain from profile.md.
    Each angle is one web-research call (~30-80K tokens), so this is a monthly action, not a daily one."""
    locs = ", ".join(profile.locations) or "anywhere"
    angles = [f"Companies with well-regarded {r} internship/co-op programs for {profile.term} in {locs}, including ones that hire international students"
              for r in profile.target_role_labels()]
    md = config.load_profile_md()

    def section(title: str) -> str:
        m = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", md, re.S | re.M)
        return m.group(1).strip() if m else ""

    desired = section("Desired work")
    for block in re.split(r"\n\*\*\d+\.\s*", "\n" + desired)[1:]:
        heading = block.split("**")[0].strip(" .:")
        angles.append(f"Companies of any size whose core engineering work is: {heading}. Prioritize strong engineering reputation and intern/co-op hiring.")
    for line in section("Things I'm open to being surprised by").splitlines():
        m = re.match(r"-\s*\*\*(.+?)\*\*", line)
        if m:
            angles.append(f"Companies and labs working on {m.group(1).strip(' :')} that hire software/embedded interns or co-ops (skip ones requiring citizenship the candidate lacks)")
    angles.append("Quant trading and HFT firms with software/C++ internships open to Canadian students, including London and Amsterdam offices")
    angles.append("Robotics, autonomy and drone companies with embedded/planning/controls internships, including Canadian offices")
    return angles


def _system(profile: Profile) -> str:
    constraints = {"target_roles": profile.target_role_labels(), "work_term": profile.term, "locations": profile.locations,
                   "remote_ok": profile.remote_ok, "work_authorization": profile.work_authorization}
    return (config.read_prompt("discover_system.md")
            + "\n\n# Candidate constraints (JSON)\n" + json.dumps(constraints, indent=2)
            + "\n\n# Candidate profile\n" + config.load_profile_md())


def new_usage() -> dict[str, Any]:
    """Accumulator passed through discover()/resolve_ats() so the CLI can record the run's model spend in `runs`."""
    return {"llm_calls": 0, "llm_ms": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0}, "errors": []}


def _add_usage(usage: dict[str, Any] | None, res) -> None:
    if usage is None:
        return
    usage["llm_calls"] += 1
    usage["llm_ms"] += res.duration_ms
    for k in usage["tokens"]:
        usage["tokens"][k] += res.tokens.get(k, 0)


def pick_angles(conn: sqlite3.Connection, profile: Profile, max_angles: int | None = None) -> list[str]:
    """The next `discovery_max_angles` default angles, rotating through the full list across runs (a cursor in
    `meta`) so every angle is eventually covered without one press paying for all of them."""
    every = default_angles(profile)
    n = profile.llm.discovery_max_angles if max_angles is None else max_angles
    if n <= 0 or n >= len(every):
        return every
    cursor = int(db.get_meta(conn, "discover_cursor") or 0) % len(every)
    db.set_meta(conn, "discover_cursor", str((cursor + n) % len(every)))
    return [every[(cursor + i) % len(every)] for i in range(n)]


def estimate_tokens(n_angles: int) -> tuple[int, int]:
    """Rough (low, high) token cost of `n_angles` web-research calls, from the ~30-80K per angle in default_angles."""
    return 30_000 * n_angles, 80_000 * n_angles


def discover(
    conn: sqlite3.Connection, backend: LLMBackend, *, angles: list[str] | None = None, like: str | None = None,
    limit: int = 15, max_angles: int | None = None, usage: dict[str, Any] | None = None,
    log: Callable[[str], None] = print,
) -> list[DiscoveredCompany]:
    profile = config.load_profile()
    system = _system(profile)
    schema = config.read_schema("discovery.json")
    if like:
        angles = [f"Companies similar to {like} in the kind of work they do and their reputation, that hire interns"]
    angles = angles or pick_angles(conn, profile, max_angles)
    known = {r["slug"]: r for r in db.list_companies(conn)}
    found: dict[str, DiscoveredCompany] = {}
    for angle in angles:
        log(f"discovering: {angle}")
        prompt = (f"Research angle: {angle}\n\nReturn up to {limit} companies. Already on the watchlist (skip these): "
                  + ", ".join(sorted(r['name'] for r in known.values())[:100]))
        try:
            res = backend.complete(system, prompt, schema, tools=DISCOVERY_TOOLS,
                                   model=profile.llm.discovery_model, effort=profile.llm.discovery_effort)
            _add_usage(usage, res)
            out = DiscoveryOutput.model_validate(res.data)
        except (LLMError, ValidationError) as e:
            log(f"  angle failed: {str(e)[:200]}")
            if usage is not None:
                usage["errors"].append(str(e)[:200])
            if isinstance(e, LLMRateLimitError):
                break   # every further angle would fail the same way
            continue
        for c in out.companies:
            slug = slugify(c.name)
            if slug in known or slug in found:
                continue
            found[slug] = c
        log(f"  -> {len(out.companies)} results, {len(found)} new so far")
    for slug, c in found.items():
        db.upsert_company(
            conn, slug=slug, name=c.name, source="discover", status="proposed", domain=c.domain, tier=c.tier_guess,
            reputation_score=c.reputation_score, fit_score=c.fit_score, work_areas=c.work_areas,
            rationale=f"{c.why_fit}\nReputation: {c.reputation_notes}\nEvidence: {', '.join(c.evidence_urls[:3])}",
            careers_url=c.careers_url,
        )
    conn.commit()
    return list(found.values())


def resolve_ats(conn: sqlite3.Connection, backend: LLMBackend | None, row: sqlite3.Row, log: Callable[[str], None] = print,
                usage: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """Deterministic probe first; then one WebFetch-enabled model call if a careers URL is known."""
    cands = fetch.slug_candidates(row["name"], row["domain"])
    hit = fetch.probe_ats(cands)
    if hit is None and backend is not None and row["careers_url"]:
        try:
            res = backend.complete(config.read_prompt("resolve_ats_system.md"),
                                   f"Company: {row['name']}\nCareers URL: {row['careers_url']}",
                                   config.read_schema("ats_resolution.json"), tools=("WebFetch",), effort="low")
            _add_usage(usage, res)
            r = ATSResolution.model_validate(res.data)
            if r.ats_type in ("greenhouse", "lever", "ashby") and r.ats_token:
                hit = fetch.probe_ats([r.ats_token]) or None
            if hit is None and r.careers_url:
                db.upsert_company(conn, slug=row["slug"], name=row["name"], source=row["source"], status=row["status"],
                                  careers_url=r.careers_url, ats_type=r.ats_type)
        except LLMRateLimitError:
            raise   # the caller stops resolving: every further company would fail the same way
        except (LLMError, ValidationError) as e:
            log(f"  ats resolution via model failed for {row['name']}: {str(e)[:150]}")
    if hit:
        # A company with a readable board costs nothing to poll: approve it automatically.
        status = "approved" if row["status"] == "proposed" else row["status"]
        db.upsert_company(conn, slug=row["slug"], name=row["name"], source=row["source"], status=status,
                          ats_type=hit[0], ats_token=hit[1])
        conn.commit()
        log(f"  {row['name']}: {hit[0]}:{hit[1]}")
    else:
        log(f"  {row['name']}: no ATS found")
    return hit
