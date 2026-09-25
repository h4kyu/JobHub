"""Mine aspirational postings for what to go learn.

The evaluation pipeline answers "can I get this job today". This module answers the opposite question:
for the roles that are currently out of reach (GPU, compilers, performance, HFT, systems), what do the
postings actually ask for? Postings are selected by target-domain keywords regardless of bucket — an
archived job the candidate has no chance at is exactly the useful signal here — then a model pass
extracts normalized skills, which are aggregated into a demand table and one ordered learning plan.

Cost: a handful of calls, run monthly rather than per-run. Selection and aggregation are free.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from . import config, db
from .config import Profile, TargetDomain
from .llm.base import LLMBackend, LLMError, LLMRateLimitError

MAX_DESC_CHARS = 3500       # requirements sections are longer here; this pass is rare, accuracy matters more
BATCH_SIZE = 8              # smaller than evaluation: per-posting output is much larger
BUCKET_RANK = {"reach": 0, "wildcard": 1, "archive": 2, "likely": 3}  # aspirational first


# ---------- model output ----------

class SkillItem(BaseModel):
    skill: str
    category: str = "domain knowledge"
    importance: str = "required"
    status: str = "missing"
    evidence: str = ""


class PostingSkills(BaseModel):
    job_id: int
    skills: list[SkillItem] = Field(default_factory=list)


class ExtractionOutput(BaseModel):
    postings: list[PostingSkills] = Field(default_factory=list)


class PlanItem(BaseModel):
    skill: str
    why: str = ""
    starting_point: str = ""
    project: str = ""
    effort_weeks: int = 4


class PlanOutput(BaseModel):
    summary: str = ""
    plan: list[PlanItem] = Field(default_factory=list)


# ---------- selection (no model calls) ----------

def _bounded(kw: str) -> str:
    """Wrap a keyword so it can't match inside a longer word — "hip" must not fire on "internship".
    Lookarounds rather than \\b, because keywords like "c++" end in a non-word character."""
    pat = re.escape(kw)
    if kw[:1].isalnum():
        pat = r"(?<![A-Za-z0-9])" + pat
    if kw[-1:].isalnum():
        pat = pat + r"(?![A-Za-z0-9])"
    return pat


def _domain_patterns(domains: list[TargetDomain]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for d in domains:
        kws = [k for k in d.keywords if k]
        if kws:
            out.append((d.name, re.compile("|".join(_bounded(k) for k in kws), re.I)))
    return out


def domain_hits(patterns: list[tuple[str, re.Pattern[str]]], title: str, description: str | None) -> dict[str, int]:
    """Distinct keyword matches per domain. Title matches count double — a posting titled 'Compiler
    Engineer' is squarely in-domain, while a passing mention of 'compiler' in a benefits blurb is not."""
    body = description or ""
    hits: dict[str, int] = {}
    for name, rx in patterns:
        n = len(set(m.group(0).lower() for m in rx.finditer(body)))
        n += 2 * len(set(m.group(0).lower() for m in rx.finditer(title or "")))
        if n:
            hits[name] = n
    return hits


def select_postings(conn: sqlite3.Connection, profile: Profile, *, limit: int = 40,
                    min_hits: int = 2, buckets: list[str] | None = None) -> list[dict[str, Any]]:
    """Active postings in the target domains, ranked aspirational-first then by keyword density."""
    patterns = _domain_patterns(profile.target_domains)
    if not patterns:
        return []
    phash = config.profile_hash()
    sql = """
        SELECT j.id, j.title, j.company_name, j.location, j.canonical_url, j.description_text,
               COALESCE(e.bucket, 'unscored') AS bucket, e.likelihood, e.desirability
        FROM jobs j
        LEFT JOIN evaluations e ON e.job_id = j.id AND e.profile_hash = ?
        WHERE j.active = 1 AND j.description_text IS NOT NULL AND j.description_text != ''
    """
    rows = conn.execute(sql, (phash,)).fetchall()
    scored: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        hits = domain_hits(patterns, r["title"], r["description_text"])
        total = sum(hits.values())
        if total < min_hits:
            continue
        if buckets and r["bucket"] not in buckets:
            continue
        key = (r["company_name"].lower().strip(), r["title"].lower().strip())
        if key in seen:      # the same posting syndicated across aggregators teaches nothing new
            continue
        seen.add(key)
        scored.append({
            "id": r["id"], "title": r["title"], "company": r["company_name"], "location": r["location"],
            "url": r["canonical_url"], "description": r["description_text"], "bucket": r["bucket"],
            "domains": sorted(hits, key=lambda d: -hits[d]), "hits": total,
        })
    scored.sort(key=lambda p: (BUCKET_RANK.get(p["bucket"], 4), -p["hits"]))
    return scored[:limit]


# ---------- extraction ----------

def _system(prompt_file: str) -> str:
    return config.read_prompt(prompt_file) + "\n\n# Candidate profile\n" + config.load_profile_md()


def _posting_block(p: dict[str, Any]) -> str:
    desc = p["description"].strip()
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS] + "\n[... truncated]"
    return (f"job_id: {p['id']}\ncompany: {p['company']}\ntitle: {p['title']}\n"
            f"target_domains: {', '.join(p['domains'])}\n--- description ---\n{desc}")


def extract_batch(backend: LLMBackend, profile: Profile, batch: list[dict[str, Any]]) -> tuple[ExtractionOutput, dict[str, int]]:
    ids = ", ".join(str(p["id"]) for p in batch)
    prompt = (f"Extract skill requirements from these {len(batch)} postings. "
              f"Return one entry per job_id: {ids}.\n\n"
              + "\n\n=====\n\n".join(_posting_block(p) for p in batch))
    res = backend.complete(_system("skill_extraction_system.md"), prompt,
                           config.read_schema("skill_extraction.json"),
                           model=profile.llm.model, effort="medium")
    return ExtractionOutput.model_validate(res.data), res.tokens


# ---------- aggregation (no model calls) ----------

def clean_text(s: str) -> str:
    """Models sometimes emit JSON-escaped quotes inside string values, which survive decoding as a
    literal backslash and land in the rendered report as \\"like this\\"."""
    return s.replace('\\"', '"').replace("\\'", "'").strip()


def _canon(skill: str) -> str:
    s = re.sub(r"\s+", " ", skill).strip().strip(".,;:")
    return s.lower()


STATUS_RANK = {"missing": 0, "partial": 1, "have": 2}


def aggregate(extractions: list[PostingSkills], postings: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-posting skills into a demand table. A skill's status is the *most pessimistic*
    judgement any posting gave it — if one posting says the candidate only partially has C++20
    concurrency, that gap is real even if another posting waved it through."""
    agg: dict[str, dict[str, Any]] = {}
    for pe in extractions:
        post = postings.get(pe.job_id)
        if not post:
            continue
        for s in pe.skills:
            key = _canon(s.skill)
            if not key:
                continue
            e = agg.setdefault(key, {
                "skill": s.skill.strip(), "category": s.category, "status": s.status,
                "domains": set(), "job_ids": [], "companies": [], "evidence": [],
                "required_count": 0, "preferred_count": 0,
            })
            if STATUS_RANK.get(s.status, 0) < STATUS_RANK.get(e["status"], 0):
                e["status"] = s.status
            if s.importance == "required":
                e["required_count"] += 1
            else:
                e["preferred_count"] += 1
            e["domains"].update(post["domains"])
            e["job_ids"].append(pe.job_id)
            if post["company"] not in e["companies"]:
                e["companies"].append(post["company"])
            if s.evidence and len(e["evidence"]) < 3:
                e["evidence"].append(s.evidence)
    out = []
    for e in agg.values():
        e["domains"] = sorted(e["domains"])
        e["demand_count"] = len(set(e["job_ids"]))
        out.append(e)
    # Rank by how often it is *required*, then total demand — a must-have in 6 postings outranks a
    # nice-to-have in 9.
    out.sort(key=lambda e: (-e["required_count"], -e["demand_count"], e["skill"].lower()))
    return out


def store(conn: sqlite3.Connection, demands: list[dict[str, Any]], generated_at: str) -> None:
    conn.execute("DELETE FROM skill_demands")
    for e in demands:
        conn.execute(
            """INSERT INTO skill_demands (generated_at, skill, category, status, domains, required_count,
                                          preferred_count, demand_count, job_ids, companies, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (generated_at, e["skill"], e["category"], e["status"], db.j(e["domains"]), e["required_count"],
             e["preferred_count"], e["demand_count"], db.j(sorted(set(e["job_ids"]))), db.j(e["companies"]),
             db.j(e["evidence"])))
    conn.commit()


def load_demands(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM skill_demands ORDER BY required_count DESC, demand_count DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("domains", "job_ids", "companies", "evidence"):
            d[k] = db.uj(d.get(k), [])
        out.append(d)
    return out


# ---------- plan ----------

def build_plan(backend: LLMBackend, profile: Profile, demands: list[dict[str, Any]],
               n_postings: int) -> tuple[PlanOutput, dict[str, int]]:
    lines = []
    for e in demands[:40]:
        if e["status"] == "have":
            continue
        lines.append(f"- {e['skill']} [{e['category']}] status={e['status']} "
                     f"required_in={e['required_count']} preferred_in={e['preferred_count']} "
                     f"postings={e['demand_count']} domains={', '.join(e['domains'])} "
                     f"seen_at={', '.join(e['companies'][:4])}")
    prompt = (f"Demand table extracted from {n_postings} real postings in the candidate's target domains.\n"
              f"Counts are numbers of postings.\n\n" + "\n".join(lines)
              + "\n\nProduce the ordered learning plan.")
    res = backend.complete(_system("skill_plan_system.md"), prompt, config.read_schema("skill_plan.json"),
                           model=profile.llm.discovery_model, effort="high")
    return PlanOutput.model_validate(res.data), res.tokens


# ---------- report ----------

def render_report(demands: list[dict[str, Any]], plan: PlanOutput | None, postings: list[dict[str, Any]],
                  generated_at: str) -> str:
    by_domain: dict[str, int] = defaultdict(int)
    for p in postings:
        for d in p["domains"]:
            by_domain[d] += 1
    L = [f"# Skills to build\n", f"_Generated {generated_at[:10]} from {len(postings)} postings in your target domains._\n"]
    L.append("Sources: " + ", ".join(f"{d} ({n})" for d, n in sorted(by_domain.items(), key=lambda kv: -kv[1])) + "\n")

    if plan and plan.plan:
        L.append("## Learning plan\n")
        if plan.summary:
            L.append(f"{clean_text(plan.summary)}\n")
        for i, it in enumerate(plan.plan, 1):
            L.append(f"### {i}. {clean_text(it.skill)}  ·  ~{it.effort_weeks} weeks")
            if it.why:
                L.append(f"**Why:** {clean_text(it.why)}")
            if it.starting_point:
                L.append(f"**Start with:** {clean_text(it.starting_point)}")
            if it.project:
                L.append(f"**Build:** {clean_text(it.project)}")
            L.append("")

    missing = [e for e in demands if e["status"] == "missing"]
    partial = [e for e in demands if e["status"] == "partial"]
    have = [e for e in demands if e["status"] == "have"]

    def table(rows: list[dict[str, Any]], title: str) -> None:
        if not rows:
            return
        L.append(f"## {title}\n")
        L.append("| Skill | Required in | Also preferred | Domains | Seen at |")
        L.append("|---|---:|---:|---|---|")
        for e in rows[:30]:
            L.append(f"| {e['skill']} | {e['required_count']} | {e['preferred_count']} | "
                     f"{', '.join(e['domains'][:2])} | {', '.join(e['companies'][:3])} |")
        L.append("")

    table(missing, "Demanded and missing")
    table(partial, "Demanded, and you're partway there")
    table(have, "Already covered (leverage these)")

    L.append("## Postings mined\n")
    for p in postings:
        L.append(f"- [{p['company']} — {p['title']}]({p['url']}) · {p['bucket']} · {', '.join(p['domains'][:2])}")
    return "\n".join(L) + "\n"


# ---------- orchestration ----------

def run_skills(conn: sqlite3.Connection, backend: LLMBackend, *, limit: int = 40, buckets: list[str] | None = None,
               make_plan: bool = True, log: Callable[[str], None] = print) -> dict[str, Any]:
    profile = config.load_profile()
    postings = select_postings(conn, profile, limit=limit, buckets=buckets)
    stats: dict[str, Any] = {"postings": len(postings), "skills": 0, "llm_calls": 0,
                             "tokens": {"input": 0, "output": 0, "cache_read": 0}, "errors": []}
    if not postings:
        log("no postings matched your target domains — widen profile.yaml target_domains or ingest more")
        return stats
    counts: dict[str, int] = defaultdict(int)
    for p in postings:
        for d in p["domains"]:
            counts[d] += 1
    log(f"mining {len(postings)} postings: " + ", ".join(f"{d} ({n})" for d, n in sorted(counts.items(), key=lambda kv: -kv[1])))

    by_id = {p["id"]: p for p in postings}
    extractions: list[PostingSkills] = []
    for i in range(0, len(postings), BATCH_SIZE):
        batch = postings[i:i + BATCH_SIZE]
        try:
            out, tok = extract_batch(backend, profile, batch)
        except LLMRateLimitError as e:
            stats["errors"].append(f"rate limit: {e}")
            log(f"stopping: {e}")
            break
        except (LLMError, ValidationError) as e:
            stats["errors"].append(str(e)[:300])
            log(f"batch failed: {str(e)[:200]}")
            continue
        stats["llm_calls"] += 1
        for k in stats["tokens"]:
            stats["tokens"][k] += tok.get(k, 0)
        extractions.extend(out.postings)
        log(f"  extracted {len(out.postings)}/{len(batch)} postings "
            f"({stats['tokens']['input']//1000}K in / {stats['tokens']['output']//1000}K out so far)")

    demands = aggregate(extractions, by_id)
    stats["skills"] = len(demands)
    generated_at = db.now()
    store(conn, demands, generated_at)

    plan: PlanOutput | None = None
    if make_plan and demands:
        try:
            plan, tok = build_plan(backend, profile, demands, len(extractions))
            stats["llm_calls"] += 1
            for k in stats["tokens"]:
                stats["tokens"][k] += tok.get(k, 0)
            db.set_meta(conn, "skills_plan", json.dumps(plan.model_dump()))
        except (LLMError, ValidationError, LLMRateLimitError) as e:
            stats["errors"].append(f"plan: {str(e)[:300]}")
            log(f"plan generation failed: {str(e)[:200]}")

    mined = [p for p in postings if p["id"] in {e.job_id for e in extractions}]
    report = render_report(demands, plan, mined, generated_at)
    config.ensure_dirs()
    path = config.DIGESTS_DIR / "skills.md"
    path.write_text(report)
    db.set_meta(conn, "skills_generated_at", generated_at)
    stats["report"] = str(path)
    missing = sum(1 for e in demands if e["status"] == "missing")
    log(f"{len(demands)} distinct skills ({missing} missing) -> {path}")
    return stats


def stored_plan(conn: sqlite3.Connection) -> PlanOutput | None:
    raw = db.get_meta(conn, "skills_plan")
    if not raw:
        return None
    try:
        return PlanOutput.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None
