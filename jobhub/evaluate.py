"""Prefilter + model evaluation + composite scoring + bucketing."""
from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from pydantic import ValidationError

from . import config, db, prefilter
from .config import Profile
from .llm.base import LLMBackend, LLMError, LLMRateLimitError
from .models import Bucket, EvaluationBatch, EvaluationOutput
from .normalize import slugify

TRIAGE_BATCH_TOKEN_GUESS = 26_000  # in+out tokens for one 40-job triage call before any has been measured (~26K observed)
MAX_METADATA_BATCH = 36  # hard cap on jobs per call when scoring from metadata alone (see run_evaluation)
MAX_DESC_CHARS = 2500  # requirements sit at the top; measured 2026-09-02, the tail is benefits/EEO boilerplate

# Everything from the first of these markers onward is legal/benefits boilerplate that costs tokens and
# tells the model nothing. Measured on a 120-posting sample: 84 contain a marker, and 20% of all
# description text sits after it.
_BOILERPLATE_RE = re.compile(
    r"(equal opportunity|equal employment|EEO\b|affirmative action|reasonable accommodation|"
    r"without regard to race|all qualified applicants|benefits (?:include|package)|401\(k\)|"
    r"paid time off|medical, dental|E-Verify|we are committed to (?:diversity|creating|building)|"
    r"diversity, equity|background check|drug (?:screen|test)|pay (?:range|transparency)|"
    r"compensation range|base (?:pay|salary) range)", re.I)
_WS_RE = re.compile(r"\n{3,}")


def trim_description(text: str) -> str:
    """Drop boilerplate tails and collapse whitespace before the length cap, so the 2500-char budget is
    spent on requirements rather than EEO statements."""
    m = _BOILERPLATE_RE.search(text)
    if m and m.start() >= 400:  # never let a marker near the top swallow the whole posting
        text = text[:m.start()]
    return _WS_RE.sub("\n\n", text).strip()


def build_system(profile: Profile) -> str:
    constraints = config.model_visible_constraints(profile)
    return (
        config.read_prompt("evaluate_system.md")
        + "\n\n# Candidate hard constraints (JSON)\n" + json.dumps(constraints, indent=2)
        + "\n\n# Candidate profile\n" + config.load_profile_md()
    )


def description_unavailable(row: sqlite3.Row) -> bool:
    return row["description_text"] is None


def job_block(row: sqlite3.Row) -> str:
    if description_unavailable(row):
        desc = ("[DESCRIPTION UNAVAILABLE — the careers site blocks automated access. Evaluate from company, title, "
                "location and what you know about this employer's internships. Do NOT hard-reject for missing information; "
                "set confidence low (<= 40) and note in the summary that the posting must be read manually.]")
    else:
        desc = trim_description(row["description_text"] or "")
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS] + "\n[... truncated]"
    terms = db.uj(row["terms"], [])
    meta = [f"job_id: {row['id']}", f"company: {row['company_name']}", f"title: {row['title']}",
            f"location: {row['location'] or 'unspecified'}"]
    if terms:
        meta.append(f"terms: {', '.join(terms)}")
    if row["sponsorship"]:
        meta.append(f"sponsorship: {row['sponsorship']}")
    meta.append(f"url: {row['canonical_url']}")
    return "\n".join(meta) + "\n--- description ---\n" + desc


# ---------- stage 1: cheap triage ----------
# 85% of everything that reaches the model gets archived. Paying full rubric price to reject is the
# single biggest remaining waste, so an optional first pass scores postings from title + opening lines
# on a small model and archives the obvious misses. Off by default (llm.triage_enabled) until
# `jobhub triage --dry-run` shows the threshold agrees with real evaluations.

def triage_block(row: sqlite3.Row, chars: int) -> str:
    desc = "" if description_unavailable(row) else trim_description(row["description_text"] or "")
    head = desc[:chars].replace("\n\n", "\n")
    return (f"job_id: {row['id']} | {row['company_name']} | {row['title']} | {row['location'] or 'unspecified'}\n"
            f"{head if head else '[no description available]'}")


def build_triage_prompt(rows: list[sqlite3.Row], chars: int) -> str:
    ids = ", ".join(str(r["id"]) for r in rows)
    blocks = "\n\n---\n\n".join(triage_block(r, chars) for r in rows)
    return f"Triage these {len(rows)} postings. Return one entry per job_id: {ids}.\n\n{blocks}"


def build_triage_system(profile: Profile) -> str:
    """Deliberately does NOT include the full profile.md — triage needs the shape of what the candidate
    wants, not their whole history, and the prompt is re-sent on every call."""
    md = config.load_profile_md()
    want = md[md.index("## Desired work"):] if "## Desired work" in md else md
    return config.read_prompt("triage_system.md") + "\n\n# What the candidate wants\n" + want


def run_triage(conn: sqlite3.Connection, backend: LLMBackend, rows: list[sqlite3.Row], profile: Profile,
               *, dry_run: bool = False, budget_tokens: int | None = None,
               log: Callable[[str], None] = print) -> dict[str, Any]:
    """Score rows cheaply; archive those below the threshold. Returns survivors plus stats, including every
    score (`stats["scores"]`) so the fast-score path can keep them instead of discarding survivors' scores.

    With `budget_tokens`, batches run in waves of `llm.concurrency` and stop before the token total would pass
    the budget (`stats["budget_stopped"]`); rows that were never scored survive untouched."""
    from .models import TriageBatch

    llm = profile.llm
    system = build_triage_system(profile)
    schema = config.read_schema("triage.json")
    phash = config.profile_hash()
    scores: dict[int, tuple[int, str]] = {}
    stats: dict[str, Any] = {"scored": 0, "llm_calls": 0, "llm_ms": 0,
                             "tokens": {"input": 0, "output": 0, "cache_read": 0}, "errors": []}
    batches = [rows[i:i + llm.triage_batch_size] for i in range(0, len(rows), llm.triage_batch_size)]

    def score_batch(batch: list[sqlite3.Row]):
        return backend.complete(system, build_triage_prompt(batch, llm.triage_desc_chars), schema,
                                model=llm.triage_model, effort=llm.triage_effort)

    done = 0
    stats["budget_stopped"] = False
    # Without a budget everything is submitted at once (as before); with one, a wave at a time so the total can
    # be checked between waves. Batch cost is estimated from what has been measured so far.
    wave_size = max(1, llm.concurrency) if budget_tokens is not None else max(1, len(batches))
    est_batch, measured = TRIAGE_BATCH_TOKEN_GUESS, 0
    with ThreadPoolExecutor(max_workers=max(1, llm.concurrency)) as pool:
        for w in range(0, len(batches), wave_size):
            spent = stats["tokens"]["input"] + stats["tokens"]["output"]
            wave = batches[w:w + wave_size]
            if budget_tokens is not None:
                fit = int((budget_tokens - spent) // est_batch)
                if fit <= 0:
                    stats["budget_stopped"] = True
                    log(f"  token budget ({budget_tokens // 1000}K) reached after {done} batches; remaining jobs keep their local score")
                    break
                wave = wave[:fit]
            futures = {pool.submit(score_batch, b): b for b in wave}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    out = TriageBatch.model_validate(res.data)
                except LLMRateLimitError:
                    for f in futures:
                        f.cancel()
                    raise
                except (LLMError, ValidationError) as e:
                    stats["errors"].append(str(e)[:300])
                    log(f"triage batch failed (batch passes through to full evaluation): {str(e)[:150]}")
                    continue
                stats["llm_calls"] += 1
                stats["llm_ms"] += res.duration_ms
                for k in stats["tokens"]:
                    stats["tokens"][k] += res.tokens.get(k, 0)
                used = res.tokens.get("input", 0) + res.tokens.get("output", 0)
                measured = max(measured, used)
                est_batch = measured or est_batch
                for t in out.triages:
                    scores[t.job_id] = (t.score, t.reason)
                stats["scored"] += len(out.triages)
                done += 1
                log(f"  triaged {stats['scored']}/{len(rows)} ({done}/{len(batches)} batches, "
                    f"{stats['tokens']['input']//1000}K in / {stats['tokens']['output']//1000}K out)")

    survivors, dropped = [], []
    for r in rows:
        score, reason = scores.get(r["id"], (100, "not triaged"))  # anything unscored survives
        (dropped if score < llm.triage_min_score else survivors).append((r, score, reason))
    if not dry_run:
        for r, score, reason in dropped:
            store_hard_reject(conn, r["id"], f"triage: {reason} ({score})", "triage", phash)
        conn.commit()
    stats["scores"] = scores
    stats["dropped"] = len(dropped)
    stats["survivors"] = len(survivors)
    stats["dropped_rows"] = dropped
    log(f"triage: {len(dropped)} archived, {len(survivors)} to full evaluation "
        f"({stats['tokens']['input']//1000}K in / {stats['tokens']['output']//1000}K out over {stats['llm_calls']} calls)")
    return {"survivors": [r for r, _, _ in survivors], "stats": stats}


def build_prompt(rows: list[sqlite3.Row]) -> str:
    blocks = "\n\n=====\n\n".join(job_block(r) for r in rows)
    ids = ", ".join(str(r["id"]) for r in rows)
    return f"Evaluate the following {len(rows)} posting(s). Return one evaluation for each job_id: {ids}.\n\n{blocks}"


def _w(x: float) -> int:
    return max(0, min(100, int(round(x))))


def composite(out: EvaluationOutput, reputation: int, profile: Profile) -> tuple[int, int, int]:
    lw, dw = profile.scoring.likelihood, profile.scoring.desirability
    likelihood = _w(lw.skills_match * out.skills_match + lw.level_match * out.level_match)
    desirability = _w(dw.work_alignment * out.work_alignment + dw.experience_quality * out.experience_quality
                      + dw.company_reputation * reputation)
    return likelihood, desirability, _w(out.interest)


def assign_bucket(likelihood: int, desirability: int, interest: int, wildcard: bool, profile: Profile) -> Bucket:
    b = profile.buckets
    if likelihood >= b.likely.min_likelihood and desirability >= b.likely.min_desirability:
        return Bucket.likely
    if desirability >= b.reach.min_desirability and likelihood >= b.reach.min_likelihood:
        return Bucket.reach
    if wildcard and interest >= b.wildcard.min_interest:
        return Bucket.wildcard
    return Bucket.archive


def store_hard_reject(conn: sqlite3.Connection, job_id: int, reason: str, source: str, phash: str) -> None:
    db.insert_evaluation(
        conn, job_id=job_id, rubric_version=config.RUBRIC_VERSION, profile_hash=phash, model=source,
        hard_reject_reason=reason, bucket=Bucket.archive.value, sub_scores=None, red_flags=[], summary=None, raw=None,
    )


def store_evaluation(conn: sqlite3.Connection, row: sqlite3.Row, out: EvaluationOutput, profile: Profile, model: str | None, phash: str) -> Bucket:
    if out.hard_reject_reason:
        store_hard_reject(conn, row["id"], f"model: {out.hard_reject_reason}", model or "model", phash)
        return Bucket.archive
    rep = db.company_reputation(conn, row["company_id"], profile.scoring.unknown_company_reputation)
    likelihood, desirability, interest = composite(out, rep, profile)
    bucket = assign_bucket(likelihood, desirability, interest, out.wildcard, profile)
    db.insert_evaluation(
        conn, job_id=row["id"], rubric_version=config.RUBRIC_VERSION, profile_hash=phash, model=model,
        hard_reject_reason=None, likelihood=likelihood, desirability=desirability, interest=interest,
        sub_scores={"skills_match": out.skills_match, "level_match": out.level_match, "work_alignment": out.work_alignment,
                    "experience_quality": out.experience_quality, "company_reputation": rep, "confidence": out.confidence},
        bucket=bucket.value, wildcard_reason=out.wildcard_reason, red_flags=out.red_flags, summary=out.summary,
        raw={**out.model_dump(), "description_unavailable": description_unavailable(row)},
    )
    if out.application_deadline and not row["deadline"]:
        conn.execute("UPDATE jobs SET deadline = ? WHERE id = ?", (out.application_deadline[:10], row["id"]))
    if bucket in (Bucket.likely, Bucket.reach) and row["company_id"] is None:
        propose_company_from_job(conn, row, bucket)
    return bucket


def propose_company_from_job(conn: sqlite3.Connection, row: sqlite3.Row, bucket: Bucket) -> None:
    slug = slugify(row["company_name"])
    if db.get_company_by_slug(conn, slug) is None:
        db.upsert_company(
            conn, slug=slug, name=row["company_name"], source="seen_in_jobs", status="proposed",
            rationale=f"Posted a {bucket.value} job: {row['title']}",
        )


def evaluate_batch(backend: LLMBackend, system: str, rows: list[sqlite3.Row], effort: str | None) -> tuple[dict[int, EvaluationOutput], str | None, int, dict[str, int]]:
    schema = config.read_schema("evaluation.json")
    prompt = build_prompt(rows)
    res = backend.complete(system, prompt, schema, tools=(), effort=effort)
    try:
        batch = EvaluationBatch.model_validate(res.data)
    except ValidationError as e:
        retry_prompt = prompt + f"\n\nYour previous response failed validation: {str(e)[:800]}. Return valid output."
        res = backend.complete(system, retry_prompt, schema, tools=(), effort=effort)
        batch = EvaluationBatch.model_validate(res.data)
    by_id = {ev.job_id: ev for ev in batch.evaluations}
    return by_id, res.model, res.duration_ms, res.tokens


def run_evaluation(
    conn: sqlite3.Connection, backend: LLMBackend, *, limit: int | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Prefilter -> (optional) cheap triage -> full rubric evaluation -> composite scoring."""
    profile = config.load_profile()
    phash = config.profile_hash()
    rows = db.jobs_pending_evaluation(conn, config.RUBRIC_VERSION, phash, limit=limit)
    stats: dict[str, Any] = {"pending": len(rows), "hard_rejected": 0, "evaluated": 0, "llm_calls": 0, "llm_ms": 0,
                             "tokens": {"input": 0, "output": 0, "cache_read": 0},
                             "errors": [], "buckets": {b.value: 0 for b in Bucket}}
    to_model: list[sqlite3.Row] = []
    for r in rows:
        reason = prefilter.check(profile, title=r["title"], location=r["location"] or "", terms=db.uj(r["terms"], None),
                                 description=r["description_text"], sponsorship=r["sponsorship"])
        if reason:
            store_hard_reject(conn, r["id"], reason, "prefilter", phash)
            stats["hard_rejected"] += 1
        else:
            to_model.append(r)
    conn.commit()
    log(f"prefilter: {stats['hard_rejected']} hard-rejected, {len(to_model)} to evaluate")

    if profile.llm.triage_enabled and to_model:
        tri = run_triage(conn, backend, to_model, profile, log=log)
        to_model = tri["survivors"]
        ts = tri["stats"]
        stats["triaged"] = ts["dropped"]
        stats["hard_rejected"] += ts["dropped"]
        stats["llm_calls"] += ts["llm_calls"]
        stats["llm_ms"] += ts["llm_ms"]
        for k in stats["tokens"]:
            stats["tokens"][k] += ts["tokens"][k]
        stats["errors"].extend(ts["errors"])
    if not to_model:
        return stats
    _evaluate_rows(conn, backend, to_model, profile, phash, stats, log)
    return stats


def _evaluate_rows(conn: sqlite3.Connection, backend: LLMBackend, to_model: list[sqlite3.Row], profile: Profile,
                   phash: str, stats: dict[str, Any], log: Callable[[str], None]) -> None:
    """Full-rubric evaluation of `to_model`, accumulating into `stats` (shared by run_evaluation and deep_evaluate)."""
    system = build_system(profile)
    bs = max(1, profile.llm.batch_size)
    with_text = [r for r in to_model if not description_unavailable(r)]
    no_text = [r for r in to_model if description_unavailable(r)]
    batches = [with_text[i:i + bs] for i in range(0, len(with_text), bs)]
    # Metadata-only jobs are short, so they pack more densely — but only up to a point: models start
    # silently omitting job_ids from the response well before the context runs out. Measured 2026-09-02,
    # batches of 72 dropped ~28% of their entries. Anything omitted is simply left unevaluated and is
    # picked up by the next run, but the call is wasted, so keep the cap absolute rather than a multiple
    # of batch_size.
    nbs = min(bs * 3, MAX_METADATA_BATCH)
    batches += [no_text[i:i + nbs] for i in range(0, len(no_text), nbs)]
    if no_text:
        log(f"{len(no_text)} jobs have no readable description and will be scored from metadata only")
    stop = False
    with ThreadPoolExecutor(max_workers=max(1, profile.llm.concurrency)) as pool:
        futures = {pool.submit(evaluate_batch, backend, system, b, profile.llm.effort): b for b in batches}
        for fut in as_completed(futures):
            batch_rows = futures[fut]
            try:
                by_id, model, ms, tokens = fut.result()
            except LLMRateLimitError as e:
                stats["errors"].append(f"rate limit: {e}")
                log("usage/rate limit hit; stopping evaluation (remaining jobs will be picked up next run)")
                stop = True
                for f in futures:
                    f.cancel()
                break
            except (LLMError, ValidationError) as e:
                stats["errors"].append(str(e)[:300])
                log(f"batch failed: {str(e)[:200]}")
                continue
            stats["llm_calls"] += 1
            stats["llm_ms"] += ms
            for k, v in tokens.items():
                stats["tokens"][k] += v
            for r in batch_rows:
                out = by_id.get(r["id"])
                if out is None:
                    stats["errors"].append(f"job {r['id']} missing from model output")
                    continue
                bucket = store_evaluation(conn, r, out, profile, model, phash)
                stats["evaluated"] += 1
                stats["buckets"][bucket.value] += 1
            conn.commit()
            log(f"evaluated batch of {len(batch_rows)} ({ms/1000:.0f}s, {tokens['input']//1000}K in / {tokens['output']} out / {tokens['cache_read']//1000}K cached) — totals {stats['buckets']}")
            if stop:
                break


def deep_evaluate(conn: sqlite3.Connection, backend: LLMBackend, job_ids: list[int],
                  log: Callable[[str], None] = print) -> dict[str, Any]:
    """On-demand full-rubric evaluation of specific jobs (the "Deep eval" button / `jobhub deep`). The result
    replaces any fast-score row for the same job. Prefilter is deliberately skipped: you asked for these."""
    profile = config.load_profile()
    phash = config.profile_hash()
    ids = [int(i) for i in dict.fromkeys(job_ids)]
    rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
    stats: dict[str, Any] = {"requested": len(ids), "found": len(rows), "evaluated": 0, "llm_calls": 0, "llm_ms": 0,
                             "tokens": {"input": 0, "output": 0, "cache_read": 0}, "errors": [],
                             "buckets": {b.value: 0 for b in Bucket}}
    if not rows:
        return stats
    log(f"deep evaluation of {len(rows)} job(s)")
    _evaluate_rows(conn, backend, rows, profile, phash, stats, log)
    return stats


def top_fast_job_ids(conn: sqlite3.Connection, n: int, bucket: str | None = None) -> list[int]:
    """The n best fast-scored jobs still waiting for a deep evaluation (optionally within one bucket)."""
    sql = """SELECT e.job_id FROM evaluations e JOIN jobs j ON j.id = e.job_id
             LEFT JOIN applications a ON a.job_id = j.id
             WHERE e.profile_hash = ? AND e.model IN ('local', 'triage') AND e.hard_reject_reason IS NULL AND j.active = 1
               AND COALESCE(a.status, 'new') = 'new'"""
    params: list[Any] = [config.profile_hash()]
    if bucket:
        sql += " AND e.bucket = ?"
        params.append(bucket)
    sql += " ORDER BY e.likelihood DESC, j.first_seen_at DESC LIMIT ?"
    params.append(int(n))
    return [r[0] for r in conn.execute(sql, params)]


#: A stored reject reason that mentions a term/season/start window is the one thing a widened term list
#: can invalidate, so those rows are re-judged rather than carried over.
_TERM_REASON_RE = re.compile(r"term|season|winter|spring|summer|fall|autumn|start (?:date|window)|20\d\d", re.I)


def carry_over_evaluations(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> dict[str, int]:
    """Re-stamp each active job's newest evaluation onto the current profile_hash, without model calls.

    Only correct when the profile change *widens* what is acceptable — adding `alt_terms`, say. Scores
    the model has already produced are unchanged by a wider term list, and re-running them would burn
    quota for identical output (2,459 jobs when Summer 2027 was added on 2026-09-09). Two kinds of row
    are deliberately left behind so they get judged again: anything hard-rejected for a term-shaped
    reason, and prefilter rows, which cost nothing to recompute on the next run.
    """
    phash, rv = config.profile_hash(), config.RUBRIC_VERSION
    rows = conn.execute(
        """SELECT e.* FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE j.active = 1 AND j.needs_reeval = 0 AND e.rubric_version = ? AND e.profile_hash != ?
             AND e.model IS NOT NULL AND e.model NOT IN ('prefilter', 'local')
             AND NOT EXISTS (SELECT 1 FROM evaluations e2 WHERE e2.job_id = j.id AND e2.profile_hash = ?
                                                            AND e2.rubric_version = ?)
             AND e.id = (SELECT e2.id FROM evaluations e2 WHERE e2.job_id = j.id AND e2.rubric_version = ?
                         ORDER BY e2.created_at DESC LIMIT 1)""",
        (rv, phash, phash, rv, rv),
    ).fetchall()
    stats = {"carried": 0, "requeued": 0}
    for r in rows:
        if r["hard_reject_reason"] and _TERM_REASON_RE.search(r["hard_reject_reason"]):
            stats["requeued"] += 1
            continue
        fields = {k: r[k] for k in r.keys() if k not in ("id", "profile_hash")}
        db.insert_evaluation(conn, **fields, profile_hash=phash)
        stats["carried"] += 1
    conn.commit()
    log(f"carried {stats['carried']} evaluations onto profile {phash}; "
        f"{stats['requeued']} term-related rejects left for re-evaluation")
    return stats


def mark_stale_for_rescore(conn: sqlite3.Connection) -> int:
    """Flag jobs whose latest evaluation isn't for the current rubric/profile."""
    phash = config.profile_hash()
    cur = conn.execute(
        """UPDATE jobs SET needs_reeval = 1 WHERE active = 1 AND description_text IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM evaluations e WHERE e.job_id = jobs.id AND e.rubric_version = ? AND e.profile_hash = ?)""",
        (config.RUBRIC_VERSION, phash),
    )
    return cur.rowcount


def recompute_buckets(conn: sqlite3.Connection) -> int:
    """Recompute composite scores and buckets from stored sub-scores using current weights/thresholds. No model calls."""
    profile = config.load_profile()
    rows = conn.execute(
        "SELECT e.id, e.job_id, e.raw, j.company_id, j.title, j.description_text FROM evaluations e JOIN jobs j ON j.id = e.job_id "
        "WHERE e.rubric_version = ? AND e.hard_reject_reason IS NULL AND e.raw IS NOT NULL "
        "AND COALESCE(e.model, '') NOT IN ('local', 'triage')", (config.RUBRIC_VERSION,)  # fast rows: see fastscore.recompute_fast
    ).fetchall()
    n = 0
    for r in rows:
        try:
            out = EvaluationOutput.model_validate(db.uj(r["raw"], {}))
        except Exception:
            continue
        rep = db.company_reputation(conn, r["company_id"], profile.scoring.unknown_company_reputation)
        likelihood, desirability, interest = composite(out, rep, profile)
        bucket = assign_bucket(likelihood, desirability, interest, out.wildcard, profile)
        if bucket is not Bucket.archive and prefilter.firmware_heavy(r["title"], r["description_text"]):
            bucket = Bucket.archive  # deterministic no-firmware rule; keeps stale evals out of Likely too
        subs = {"skills_match": out.skills_match, "level_match": out.level_match, "work_alignment": out.work_alignment,
                "experience_quality": out.experience_quality, "company_reputation": rep, "confidence": out.confidence}
        conn.execute("UPDATE evaluations SET likelihood = ?, desirability = ?, interest = ?, bucket = ?, sub_scores = ? WHERE id = ?",
                     (likelihood, desirability, interest, bucket.value, json.dumps(subs), r["id"]))
        n += 1
    conn.commit()
    return n
