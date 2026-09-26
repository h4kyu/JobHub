"""Fast scoring: a free, deterministic 0-100 score for every posting, plus optional budget-capped Haiku triage
for the ambiguous middle band.

This replaces "full rubric on everything" as the default way to fill the Likely / Reach tabs. The idea is
visibility, not precision: most postings are clearly in or out of the target space from the title, the
company and the first few hundred characters, and those never touch a model. Only the ambiguous band is
sent to triage, and only up to `llm.run_token_budget` / `llm.weekly_token_budget` tokens. Anything left over
keeps its local score (and so stays visible) and is picked up by the next run.

Rows are ordinary `evaluations` rows: `model='local'` (never sent to a model) or `model='triage'`. The score is
stored as both `likelihood` and `desirability`, so the existing sorting, digest and UI work unchanged.
A deep (full rubric) evaluation of the same job replaces the fast row: see evaluate.deep_evaluate.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, db, prefilter, roletype
from .config import FastScoring, Profile, TargetDomain
from .llm.base import LLMBackend, LLMRateLimitError
from .models import Bucket
from .normalize import slugify

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


FAST_MODELS = ("local", "triage")
BAND_YES, BAND_NO, BAND_AMBIGUOUS = "yes", "no", "ambiguous"
TIER_REPUTATION = {1: 90, 2: 70, 3: 55}


@dataclass
class FastResult:
    score: int
    band: str
    role: str
    why: list[str] = field(default_factory=list)
    unread: bool = False


def bucket_for(score: int, fs: FastScoring) -> Bucket:
    if score >= fs.likely_min:
        return Bucket.likely
    if score >= fs.reach_min:
        return Bucket.reach
    return Bucket.archive


def _clamp(x: float) -> int:
    return max(0, min(100, int(round(x))))


class FastScorer:
    """Compiles the keyword patterns and company lookup once; `score(row)` is then pure and cheap."""

    def __init__(self, conn: sqlite3.Connection, profile: Profile):
        self.profile = profile
        self.fs = profile.fast_scoring
        self.domains = _domain_patterns(profile.target_domains)
        words = [w.strip() for w in self.fs.negative_title if w.strip()]
        self.negative = re.compile("|".join(_bounded(w) for w in words), re.I) if words else None
        sw = [w.strip() for w in self.fs.software_keywords if w.strip()]
        self.software = re.compile("|".join(_bounded(w) for w in sw), re.I) if sw else None
        self.companies: dict[str, int] = {}
        for c in conn.execute("SELECT slug, tier, reputation_score FROM companies"):
            rep = c["reputation_score"]
            if rep is None and c["tier"] is not None:
                rep = TIER_REPUTATION.get(int(c["tier"]))
            if rep is not None:
                self.companies[c["slug"]] = int(rep)
        self.unknown_rep = profile.scoring.unknown_company_reputation

    def _hits(self, text: str) -> set[str]:
        found: set[str] = set()
        for _, rx in self.domains:
            found.update(m.group(0).lower() for m in rx.finditer(text))
        return found

    def score(self, row: sqlite3.Row) -> FastResult:
        fs = self.fs
        title = row["title"] or ""
        role = roletype.classify(title, company=row["company_name"])
        score = float(fs.role_weights.get(role, fs.role_weights.get("general", 45)))
        why: list[str] = []   # the role type is shown as its own badge, so it is not repeated here

        title_hits = self._hits(title)
        if title_hits:
            score += min(fs.title_domain_cap, fs.title_domain_points * len(title_hits))
            why += sorted(title_hits)[:2]

        if self.software and self.software.search(title):
            score += fs.software_title_bonus
            why.append("software title")

        unread = row["description_text"] is None
        if not unread:
            from .evaluate import trim_description

            head = trim_description(row["description_text"] or "")[: fs.desc_chars]
            desc_hits = self._hits(head) - title_hits
            if desc_hits:
                score += min(fs.desc_domain_cap, fs.desc_domain_points * len(desc_hits))
                why += sorted(desc_hits)[: 2 if not title_hits else 1]
            if self.software:
                sw_hits = {m.group(0).lower() for m in self.software.finditer(head)}
                score += min(fs.software_desc_cap, fs.software_desc_points * len(sw_hits))

        rep = self.companies.get(slugify(row["company_name"] or ""))
        if rep is not None:
            score += (rep - self.unknown_rep) * fs.reputation_weight
            if rep >= 70:
                why.append("known company")

        offtarget = False
        if self.negative:
            m = self.negative.search(title)
            if m:
                offtarget = True
                score -= fs.negative_title_penalty
                why.append(f"off-target: {m.group(0).lower()}")

        score = _clamp(score)
        band = BAND_YES if score >= fs.hi else BAND_NO if score <= fs.lo else BAND_AMBIGUOUS
        # A posting nobody could read (site blocks scraping) can't be judged locally beyond its title: never file it
        # as a clear no on that basis alone. It goes to triage, which at least knows the employer.
        if unread and band == BAND_NO and not offtarget and fs.unread_to_triage:
            band = BAND_AMBIGUOUS
        return FastResult(score=score, band=band, role=role, why=why[:6], unread=unread)


# ---------- storage ----------

def _fast_raw(res: FastResult, *, local: int, triage: int | None = None, reason: str | None = None) -> dict[str, Any]:
    return {"fit_tags": res.why, "description_unavailable": res.unread,
            "fast": {"band": res.band, "local": local, "triage": triage, "reason": reason, "role": res.role}}


def _store(conn: sqlite3.Connection, job_id: int, phash: str, fs: FastScoring, res: FastResult, *, model: str,
           local: int, final: int, triage: int | None = None, reason: str | None = None) -> Bucket:
    bucket = bucket_for(final, fs)
    db.insert_evaluation(
        conn, job_id=job_id, rubric_version=config.RUBRIC_VERSION, profile_hash=phash, model=model,
        hard_reject_reason=None, likelihood=final, desirability=final, interest=None,
        sub_scores={"fast_score": final, "local_score": local, "triage_score": triage},
        bucket=bucket.value, red_flags=[], summary=reason, raw=_fast_raw(res, local=local, triage=triage, reason=reason),
    )
    return bucket


def _combine(fs: FastScoring, local: int, triage: int | None) -> int:
    return local if triage is None else _clamp(fs.triage_weight * triage + (1 - fs.triage_weight) * local)


# ---------- the run ----------

def token_allowance(conn: sqlite3.Connection, profile: Profile, override: int | None = None) -> int | None:
    """Tokens this run may still spend, or None for unlimited. The smaller of the per-run budget and what is left
    of the rolling 7-day budget, so repeated runs can't add up past the weekly ceiling."""
    llm = profile.llm
    caps = []
    if (override if override is not None else llm.run_token_budget) > 0:
        caps.append(override if override is not None else llm.run_token_budget)
    if llm.weekly_token_budget > 0:
        caps.append(max(0, llm.weekly_token_budget - db.tokens_used_since(conn, 7)))
    return min(caps) if caps else None


def ambiguous_rows(conn: sqlite3.Connection, phash: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Jobs whose only evaluation is a local score in the ambiguous band, most promising first."""
    sql = """SELECT j.*, e.likelihood AS local_score FROM evaluations e JOIN jobs j ON j.id = e.job_id
             WHERE e.profile_hash = ? AND e.model = 'local' AND j.active = 1
               AND json_extract(e.raw, '$.fast.band') = 'ambiguous'
             ORDER BY e.likelihood DESC, j.first_seen_at DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (phash,)).fetchall()


def count_ambiguous(conn: sqlite3.Connection, phash: str) -> int:
    return int(conn.execute(
        """SELECT COUNT(*) FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model = 'local' AND j.active = 1
             AND json_extract(e.raw, '$.fast.band') = 'ambiguous'""", (phash,)).fetchone()[0])


def budget_reason(conn: sqlite3.Connection, profile: Profile, override: int | None = None) -> str:
    """Which cap is the binding one, for messages ("run cap" / "weekly cap")."""
    llm = profile.llm
    run_cap = override if override is not None else llm.run_token_budget
    weekly_left = max(0, llm.weekly_token_budget - db.tokens_used_since(conn, 7)) if llm.weekly_token_budget > 0 else None
    if weekly_left is not None and (run_cap <= 0 or weekly_left < run_cap):
        return "weekly cap"
    return "run cap"


def run_fast_scoring(conn: sqlite3.Connection, backend: LLMBackend | None, *, limit: int | None = None,
                     budget: int | None = None, use_model: bool = True,
                     log: Callable[[str], None] = print) -> dict[str, Any]:
    """prefilter -> local score for everything pending -> budget-capped triage of the ambiguous band.

    Also records in `meta.last_score` how many ambiguous jobs were left on their local score and why, so the web
    UI can say so instead of leaving the user to wonder why a job wasn't refined."""
    stats = _run_fast_scoring(conn, backend, limit=limit, budget=budget, use_model=use_model, log=log)
    db.set_meta(conn, "last_score", json.dumps({"at": db.now(), "deferred": stats["deferred"], "reason": stats.get("reason")}))
    conn.commit()
    return stats


def _run_fast_scoring(conn: sqlite3.Connection, backend: LLMBackend | None, *, limit: int | None, budget: int | None,
                      use_model: bool, log: Callable[[str], None]) -> dict[str, Any]:
    from .evaluate import run_triage, store_hard_reject

    profile = config.load_profile()
    fs = profile.fast_scoring
    phash = config.profile_hash()
    stats: dict[str, Any] = {"pending": 0, "hard_rejected": 0, "scored_local": 0, "bands": {BAND_YES: 0, BAND_NO: 0, BAND_AMBIGUOUS: 0},
                             "triaged": 0, "llm_calls": 0, "llm_ms": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0},
                             "errors": [], "buckets": {b.value: 0 for b in Bucket}, "deferred": 0, "budget_stopped": False}
    rows = db.jobs_pending_fast(conn, phash, limit=limit)
    stats["pending"] = len(rows)
    scorer = FastScorer(conn, profile)
    for r in rows:
        reason = prefilter.check(profile, title=r["title"], location=r["location"] or "", terms=db.uj(r["terms"], None),
                                 description=r["description_text"], sponsorship=r["sponsorship"])
        if reason:
            store_hard_reject(conn, r["id"], reason, "prefilter", phash)
            stats["hard_rejected"] += 1
            continue
        res = scorer.score(r)
        stats["bands"][res.band] += 1
        stats["buckets"][_store(conn, r["id"], phash, fs, res, model="local", local=res.score, final=res.score).value] += 1
        stats["scored_local"] += 1
    conn.commit()
    log(f"fast score: {stats['pending']} pending -> {stats['hard_rejected']} rejected by rules, {stats['scored_local']} scored locally "
        f"(yes {stats['bands'][BAND_YES]} / ambiguous {stats['bands'][BAND_AMBIGUOUS]} / no {stats['bands'][BAND_NO]})")

    if not (use_model and backend is not None and profile.llm.triage_enabled and fs.enabled):
        stats["deferred"] = count_ambiguous(conn, phash)
        stats["reason"] = ("model off for this run" if not (use_model and backend is not None) else
                           "llm.triage_enabled is off" if not profile.llm.triage_enabled else "fast_scoring.enabled is off")
        return stats
    queue = ambiguous_rows(conn, phash, limit=limit)
    stats["deferred"] = len(queue)
    if not queue:
        return stats
    allowance = token_allowance(conn, profile, budget)
    if allowance is not None and allowance <= 0:
        stats["budget_stopped"] = True
        stats["reason"] = budget_reason(conn, profile, budget)
        log(f"token budget exhausted ({stats['reason']}): {len(queue)} ambiguous jobs keep their local score until it frees up")
        return stats
    log(f"triaging {len(queue)} ambiguous jobs" + (f" (token allowance {allowance // 1000}K)" if allowance is not None else ""))
    try:
        out = run_triage(conn, backend, queue, profile, dry_run=True, budget_tokens=allowance, log=log)
    except LLMRateLimitError as e:
        stats["errors"].append(f"rate limit: {e}")
        log("usage limit hit; stopping. Ambiguous jobs keep their local score and are retried next run")
        stats["budget_stopped"] = True
        stats["reason"] = "subscription usage limit hit"
        return stats
    ts = out["stats"]
    stats["llm_calls"], stats["llm_ms"] = ts["llm_calls"], ts["llm_ms"]
    for k in stats["tokens"]:
        stats["tokens"][k] += ts["tokens"][k]
    stats["errors"].extend(ts["errors"])
    stats["budget_stopped"] = ts.get("budget_stopped", False)
    if stats["budget_stopped"]:
        stats["reason"] = budget_reason(conn, profile, budget)
    for r in queue:
        got = ts["scores"].get(r["id"])
        if got is None:
            continue
        tscore, reason = got
        local = int(r["local_score"])
        res = scorer.score(r)
        if tscore < profile.llm.triage_min_score:
            store_hard_reject(conn, r["id"], f"triage: {reason} ({tscore})", "triage", phash)
            stats["buckets"][Bucket.archive.value] += 1
        else:
            stats["buckets"][_store(conn, r["id"], phash, fs, res, model="triage", local=local,
                                    final=_combine(fs, local, tscore), triage=tscore, reason=reason).value] += 1
        stats["triaged"] += 1
    conn.commit()
    stats["deferred"] = count_ambiguous(conn, phash)
    log(f"triaged {stats['triaged']}; {stats['deferred']} ambiguous jobs still waiting on a model pass "
        f"({ts['tokens']['input'] // 1000}K in / {ts['tokens']['output'] // 1000}K out)")
    return stats


def reprefilter(conn: sqlite3.Connection, *, deep: bool = False, dry_run: bool = False,
                log: Callable[[str], None] = print) -> dict[str, int]:
    """Re-run the deterministic prefilter over jobs that already have a (non-rejected) evaluation, and archive
    the ones it now rejects. Free, and reversible like any other hard reject.

    This is what applies a tightened *non-model-visible* rule — a new dealbreaker word, a `degrees` ceiling —
    to jobs that were already scored. Without it they keep their old row forever: `jobs_pending_fast` only ever
    looks at jobs with *no* evaluation for the current profile_hash, and these have one.

    `deep=False` (the default, used by `recompute_fast` on every run) touches only `local` / `triage` rows, which
    cost nothing to recreate. `deep=True` also replaces full-rubric rows, discarding that rubric detail — so it
    is behind `jobhub rescore --reprefilter`, never automatic.
    """
    from .evaluate import store_hard_reject

    profile = config.load_profile()
    phash = config.profile_hash()
    sql = """SELECT e.job_id, e.model, j.title, j.location, j.terms, j.sponsorship, j.description_text
             FROM evaluations e JOIN jobs j ON j.id = e.job_id
             WHERE e.profile_hash = ? AND e.hard_reject_reason IS NULL AND j.active = 1"""
    if not deep:
        sql += " AND e.model IN ('local', 'triage')"
    counts: dict[str, int] = {}
    for r in conn.execute(sql, (phash,)).fetchall():
        reason = prefilter.check(profile, title=r["title"], location=r["location"] or "", terms=db.uj(r["terms"], None),
                                 description=r["description_text"], sponsorship=r["sponsorship"])
        if not reason:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        if not dry_run:
            store_hard_reject(conn, r["job_id"], reason, "prefilter", phash)   # same unique key: replaces the row
    if not dry_run:
        conn.commit()
    total = sum(counts.values())
    if total:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        log(f"prefilter {'would reject' if dry_run else 'now rejects'} {total} already-scored jobs ({detail})")
    counts.update(unreject_stale(conn, dry_run=dry_run, log=log))
    return counts


def unreject_stale(conn: sqlite3.Connection, *, dry_run: bool = False,
                   log: Callable[[str], None] = print) -> dict[str, int]:
    """The other half of `reprefilter`: drop prefilter rejections the rules no longer justify, so a *relaxed*
    rule frees its jobs instead of archiving them forever. Deleting the row makes the job pending again
    (`db.jobs_pending_fast` looks for jobs with no evaluation at this profile_hash) and the next `run` gives it
    a free local score. Only ever touches `model='prefilter'` rows, which cost nothing to recreate."""
    profile = config.load_profile()
    phash = config.profile_hash()
    rows = conn.execute(
        """SELECT e.id, e.hard_reject_reason, j.title, j.location, j.terms, j.sponsorship, j.description_text
           FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model = 'prefilter' AND j.active = 1""", (phash,)).fetchall()
    freed: dict[str, int] = {}
    changed = 0
    for r in rows:
        reason = prefilter.check(profile, title=r["title"], location=r["location"] or "", terms=db.uj(r["terms"], None),
                                 description=r["description_text"], sponsorship=r["sponsorship"])
        if reason == r["hard_reject_reason"]:
            continue
        key = f"freed: {r['hard_reject_reason']}" if reason is None else f"{r['hard_reject_reason']} -> {reason}"
        freed[key] = freed.get(key, 0) + 1
        changed += 1
        if dry_run:
            continue
        if reason is None:
            conn.execute("DELETE FROM evaluations WHERE id = ?", (r["id"],))
        else:
            conn.execute("UPDATE evaluations SET hard_reject_reason = ? WHERE id = ?", (reason, r["id"]))
    if not dry_run:
        conn.commit()
    if changed:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(freed.items(), key=lambda kv: -kv[1]))
        log(f"{changed} stale prefilter rejections {'would be' if dry_run else ''} re-judged ({detail})")
    return freed


def recompute_fast(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> int:
    """Re-apply the current fast_scoring knobs to stored fast rows, with no model calls: local rows are re-scored
    from the posting, triage rows keep their stored triage score and are re-combined and re-bucketed. Runs
    `reprefilter` first so a tightened deterministic rule takes effect on rows that already exist."""
    reprefilter(conn, log=log)
    profile = config.load_profile()
    fs = profile.fast_scoring
    phash = config.profile_hash()
    scorer = FastScorer(conn, profile)
    rows = conn.execute(
        """SELECT e.id, e.job_id, e.model, e.raw, e.summary, j.title, j.company_name, j.description_text
           FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model IN ('local', 'triage') AND e.hard_reject_reason IS NULL AND j.active = 1""",
        (phash,)).fetchall()
    n = 0
    for r in rows:
        prev = (db.uj(r["raw"], {}) or {}).get("fast") or {}
        res = scorer.score(r)
        triage = prev.get("triage") if r["model"] == "triage" else None
        final = _combine(fs, res.score, triage)
        bucket = bucket_for(final, fs)
        raw = _fast_raw(res, local=res.score, triage=triage, reason=prev.get("reason"))
        conn.execute(
            "UPDATE evaluations SET likelihood = ?, desirability = ?, bucket = ?, sub_scores = ?, raw = ? WHERE id = ?",
            (final, final, bucket.value, json.dumps({"fast_score": final, "local_score": res.score, "triage_score": triage}),
             json.dumps(raw), r["id"]))
        n += 1
    conn.commit()
    return n


# ---------- calibration ----------

def calibrate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Score every job that already has a deep (Haiku/Fable) evaluation and compare with what that evaluation
    decided. Free. Recall is the number that matters: how many jobs the rubric called Likely/Reach would the
    local scorer have kept away from the archive?"""
    profile = config.load_profile()
    fs = profile.fast_scoring
    phash = config.profile_hash()
    scorer = FastScorer(conn, profile)
    rows = conn.execute(
        """SELECT j.*, e.bucket AS legacy_bucket FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model NOT IN ('prefilter', 'triage', 'local') AND e.hard_reject_reason IS NULL
             AND e.id = (SELECT MAX(e2.id) FROM evaluations e2 WHERE e2.job_id = j.id AND e2.profile_hash = ?)""",
        (phash, phash)).fetchall()
    table: dict[str, dict[str, int]] = {}
    scores_pos: list[int] = []
    for r in rows:
        res = scorer.score(r)
        cell = table.setdefault(r["legacy_bucket"], {BAND_YES: 0, BAND_AMBIGUOUS: 0, BAND_NO: 0})
        cell[res.band] += 1
        if r["legacy_bucket"] in ("likely", "reach"):
            scores_pos.append(res.score)
    drops = conn.execute(
        """SELECT j.* FROM evaluations e JOIN jobs j ON j.id = e.job_id
           WHERE e.profile_hash = ? AND e.model = 'triage' AND e.hard_reject_reason IS NOT NULL""", (phash,)).fetchall()
    if drops:
        cell = table.setdefault("triage-archived", {BAND_YES: 0, BAND_AMBIGUOUS: 0, BAND_NO: 0})
        for r in drops:
            cell[scorer.score(r).band] += 1
    pos = len(scores_pos)
    recall = {t: (sum(1 for s in scores_pos if s > t) / pos if pos else 0.0) for t in (10, 20, 25, 30, 32, 35, 40, 45, 50)}
    pending = db.jobs_pending_fast(conn, phash)
    pend_bands = {BAND_YES: 0, BAND_AMBIGUOUS: 0, BAND_NO: 0}
    for r in pending:
        pend_bands[scorer.score(r).band] += 1
    return {"evaluated": len(rows), "by_legacy_bucket": table, "recall_above": recall, "pending": len(pending),
            "pending_bands": pend_bands, "hi": fs.hi, "lo": fs.lo}
