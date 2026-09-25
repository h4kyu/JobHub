"""`jobhub` command-line interface."""
from __future__ import annotations

import json
from typing import Optional

import typer

from . import config, db

app = typer.Typer(help="Local internship search, evaluation and tracking engine.", no_args_is_help=True)
companies_app = typer.Typer(help="Manage the company watchlist.", no_args_is_help=True)
app.add_typer(companies_app, name="companies")


def _backend():
    from .llm.claude_code import backend_from_profile

    return backend_from_profile()


@app.command()
def init() -> None:
    """Create the database and data directories."""
    config.ensure_dirs()
    db.init_db()
    typer.echo(f"initialized {config.DB_PATH}")


@app.command()
def serve(port: int = typer.Option(8765), host: str = typer.Option("127.0.0.1"), open_browser: bool = typer.Option(True)) -> None:
    """Start the local web UI."""
    from .web.app import serve as _serve

    url = f"http://{host}:{port}"
    typer.echo(f"JobHub UI at {url}  (Ctrl-C to stop)")
    if open_browser:
        import threading, webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    _serve(host=host, port=port)


@app.command()
def smoke() -> None:
    """Verify headless Claude Code works with structured output."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}, "word": {"type": "string"}}, "required": ["n", "word"]}
    res = _backend().complete("You answer tersely.", 'Return the number 42 and the word "ok".', schema)
    typer.echo(f"model={res.model} duration_ms={res.duration_ms} data={res.data}")


@app.command()
def ingest(fetch_limit: Optional[int] = typer.Option(None, help="Max job descriptions to fetch this run (default: all pending; 0 = none)"),
           retry_unreadable: bool = typer.Option(False, "--retry-unreadable", help="First give every job whose description failed to fetch another set of attempts")) -> None:
    """Pull all sources, dedupe into the database, fetch missing descriptions."""
    with db.session() as conn:
        if retry_unreadable:
            typer.echo(f"{db.reset_fetch_failures(conn)} jobs re-queued for description fetching")
        run_id = db.start_run(conn, "ingest")
        from .ingest import run_ingest

        stats = run_ingest(conn, fetch_limit=fetch_limit, log=typer.echo)
        db.finish_run(conn, run_id, jobs_seen=stats["jobs_seen"], jobs_new=stats["jobs_new"], errors=stats["errors"])
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "errors"}))


@app.command()
def evaluate(limit: Optional[int] = typer.Option(None, help="Max jobs to evaluate this run")) -> None:
    """Prefilter and score pending jobs with Claude."""
    with db.session() as conn:
        run_id = db.start_run(conn, "evaluate")
        from .evaluate import run_evaluation

        stats = run_evaluation(conn, _backend(), limit=limit, log=typer.echo)
        db.finish_run(conn, run_id, jobs_evaluated=stats["evaluated"], llm_calls=stats["llm_calls"], llm_ms=stats["llm_ms"], errors=stats["errors"],
                      llm_input_tokens=stats["tokens"]["input"], llm_output_tokens=stats["tokens"]["output"], llm_cache_read_tokens=stats["tokens"]["cache_read"])
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "errors"}))
    if stats["errors"]:
        typer.echo(f"{len(stats['errors'])} error(s); first: {stats['errors'][0]}")


@app.command()
def rescore(limit: Optional[int] = typer.Option(None),
            recompute_only: bool = typer.Option(False, help="Only recompute buckets from stored sub-scores (no model calls)"),
            carry_over: bool = typer.Option(False, help="Re-stamp existing scores onto the new profile hash instead of "
                                                        "re-running them — only when the profile change WIDENS what is "
                                                        "acceptable (e.g. adding alt_terms). No model calls.")) -> None:
    """Recompute buckets with current weights, then re-evaluate jobs whose evaluation is stale (profile/rubric changed)."""
    with db.session() as conn:
        from .evaluate import carry_over_evaluations, mark_stale_for_rescore, recompute_buckets

        from .fastscore import recompute_fast

        typer.echo(f"{recompute_buckets(conn)} evaluations re-bucketed with current weights/thresholds")
        typer.echo(f"{recompute_fast(conn)} fast-scored rows re-scored with current fast_scoring settings")
        if carry_over:
            carry_over_evaluations(conn, log=typer.echo)
        n = 0 if (recompute_only or carry_over) else mark_stale_for_rescore(conn)
    if carry_over:  # carrying over is the whole point: don't then re-run what was just carried
        typer.echo("run `jobhub evaluate` (or `jobhub run`) to score what is still pending")
        return
    if recompute_only:
        return
    typer.echo(f"{n} jobs flagged for re-evaluation")
    evaluate(limit=limit)


@app.command()
def digest() -> None:
    """Write today's digest from the current evaluations."""
    with db.session() as conn:
        from .digest import write_digest

        path = write_digest(conn)
    typer.echo(f"wrote {path}")


def _record_run(conn, run_id: int, stats: dict, **extra) -> None:
    """finish_run for any stats dict shaped like fastscore/evaluate/discover output."""
    tok = stats.get("tokens") or {}
    db.finish_run(conn, run_id, llm_calls=stats.get("llm_calls", 0), llm_ms=stats.get("llm_ms", 0), errors=stats.get("errors", []),
                  llm_input_tokens=tok.get("input", 0), llm_output_tokens=tok.get("output", 0),
                  llm_cache_read_tokens=tok.get("cache_read", 0), **extra)


@app.command()
def run(fetch_limit: Optional[int] = typer.Option(None, help="Max descriptions to fetch (default: all pending)"),
        limit: Optional[int] = typer.Option(None, help="Max jobs to score / triage (default: all pending)"),
        budget: Optional[int] = typer.Option(None, help="Token budget for this run's model calls (default: llm.run_token_budget; 0 = unlimited)"),
        model: bool = typer.Option(True, "--model/--no-model", help="--no-model: local scoring only, zero tokens")) -> None:
    """ingest -> fast score (local + budget-capped triage) -> digest. Deep evaluation is separate: `jobhub deep`."""
    from .digest import write_digest
    from .evaluate import recompute_buckets
    from .fastscore import recompute_fast, run_fast_scoring
    from .ingest import run_ingest

    with db.session() as conn:
        run_id = db.start_run(conn, "run")
        n = recompute_buckets(conn)  # cheap: apply current weights/thresholds before anything else
        typer.echo(f"re-bucketed {n} existing evaluations with current weights")
        typer.echo(f"re-scored {recompute_fast(conn)} fast-scored rows with current fast_scoring settings")
        istats = run_ingest(conn, fetch_limit=fetch_limit, log=typer.echo)
        fstats = run_fast_scoring(conn, _backend() if model else None, limit=limit, budget=budget, use_model=model, log=typer.echo)
        _record_run(conn, run_id, {**fstats, "errors": istats["errors"] + fstats["errors"]},
                    jobs_seen=istats["jobs_seen"], jobs_new=istats["jobs_new"], jobs_evaluated=fstats["scored_local"] + fstats["triaged"])
        summary = {"jobs_seen": istats["jobs_seen"], "jobs_new": istats["jobs_new"], "descriptions_fetched": istats["descriptions_fetched"],
                   "hard_rejected": fstats["hard_rejected"], "evaluated": fstats["scored_local"] + fstats["triaged"], "llm_calls": fstats["llm_calls"],
                   "tokens_in": fstats["tokens"]["input"], "tokens_out": fstats["tokens"]["output"], "tokens_cached": fstats["tokens"]["cache_read"],
                   "errors": len(istats["errors"] + fstats["errors"])}
        path = write_digest(conn, summary)
    typer.echo(f"wrote {path}")
    typer.echo(f"{fstats['deferred']} ambiguous jobs are still on their local score; budget stopped: {fstats['budget_stopped']}")


@app.command()
def score(limit: Optional[int] = typer.Option(None, help="Max jobs to score / triage this run"),
          budget: Optional[int] = typer.Option(None, help="Token budget for this run (default: llm.run_token_budget; 0 = unlimited)"),
          model: bool = typer.Option(True, "--model/--no-model", help="--no-model: local scoring only, zero tokens"),
          calibrate: bool = typer.Option(False, "--calibrate", help="Report how the local scorer agrees with existing deep evaluations. Free, writes nothing.")) -> None:
    """Fast-score pending jobs: free local score, then budget-capped Haiku triage of the ambiguous band."""
    from .fastscore import calibrate as _calibrate, run_fast_scoring

    if calibrate:
        with db.session() as conn:
            rep = _calibrate(conn)
        typer.echo(f"{rep['evaluated']} jobs with a deep evaluation, scored locally (hi={rep['hi']}, lo={rep['lo']})\n")
        typer.echo(f"{'legacy bucket':<17}{'yes':>8}{'ambiguous':>11}{'no':>8}")
        for b in ("likely", "reach", "wildcard", "archive", "triage-archived"):
            c = rep["by_legacy_bucket"].get(b)
            if c:
                typer.echo(f"{b:<17}{c['yes']:>8}{c['ambiguous']:>11}{c['no']:>8}")
        typer.echo("\nrecall of Likely+Reach if 'no' = local score <= T:")
        for t, r in rep["recall_above"].items():
            typer.echo(f"  T={t:<3} keeps {r:6.1%} of Likely+Reach")
        pb = rep["pending_bands"]
        typer.echo(f"\n{rep['pending']} pending jobs would land: yes {pb['yes']} / ambiguous {pb['ambiguous']} / no {pb['no']}")
        return
    with db.session() as conn:
        run_id = db.start_run(conn, "score")
        stats = run_fast_scoring(conn, _backend() if model else None, limit=limit, budget=budget, use_model=model, log=typer.echo)
        _record_run(conn, run_id, stats, jobs_evaluated=stats["scored_local"] + stats["triaged"])
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "errors"}))
    if stats["errors"]:
        typer.echo(f"{len(stats['errors'])} error(s); first: {stats['errors'][0]}")


@app.command()
def deep(job_ids: list[int] = typer.Argument(None, help="Job ids to evaluate with the full rubric"),
         top: Optional[int] = typer.Option(None, help="Instead of ids: deep-evaluate the N best fast-scored jobs still to review"),
         bucket: Optional[str] = typer.Option(None, help="With --top: restrict to one bucket (likely, reach)")) -> None:
    """On-demand full-rubric evaluation (~13K tokens for one job, ~2K each after that). Never runs automatically."""
    from .evaluate import deep_evaluate, top_fast_job_ids

    with db.session() as conn:
        ids = list(job_ids or [])
        if top:
            ids += top_fast_job_ids(conn, top, bucket)
        if not ids:
            raise typer.BadParameter("give job ids, or --top N")
        run_id = db.start_run(conn, "deep")
        stats = deep_evaluate(conn, _backend(), ids, log=typer.echo)
        _record_run(conn, run_id, stats, jobs_evaluated=stats["evaluated"])
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "errors"}))
    if stats["errors"]:
        typer.echo(f"{len(stats['errors'])} error(s); first: {stats['errors'][0]}")


@app.command()
def skills(
    limit: int = typer.Option(40, help="Max postings to mine"),
    bucket: Optional[str] = typer.Option(None, help="Restrict to one bucket (reach, wildcard, archive, likely)"),
    no_plan: bool = typer.Option(False, "--no-plan", help="Skip the synthesis call; demand table only"),
    show: bool = typer.Option(False, "--show", help="Print the stored report instead of regenerating"),
) -> None:
    """Mine aspirational postings (GPU, compilers, performance, HFT, systems) for what to go learn."""
    from .skills import run_skills

    path = config.DIGESTS_DIR / "skills.md"
    if show:
        if not path.exists():
            raise typer.BadParameter("no report yet — run `jobhub skills` first")
        typer.echo(path.read_text())
        return
    with db.session() as conn:
        run_id = db.start_run(conn, "skills")
        stats = run_skills(conn, _backend(), limit=limit, buckets=[bucket] if bucket else None,
                           make_plan=not no_plan, log=typer.echo)
        db.finish_run(conn, run_id, llm_calls=stats["llm_calls"], errors=stats["errors"],
                      llm_input_tokens=stats["tokens"]["input"], llm_output_tokens=stats["tokens"]["output"],
                      llm_cache_read_tokens=stats["tokens"]["cache_read"])
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "errors"}))


@app.command()
def triage(
    limit: Optional[int] = typer.Option(None, help="Max pending jobs to triage"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Score without archiving (default)"),
) -> None:
    """Run the cheap stage-1 triage. Use --dry-run to calibrate llm.triage_min_score before enabling it."""
    from .evaluate import run_triage
    from . import prefilter

    profile = config.load_profile()
    with db.session() as conn:
        rows = db.jobs_pending_evaluation(conn, config.RUBRIC_VERSION, config.profile_hash(), limit=limit)
        rows = [r for r in rows if not prefilter.check(
            profile, title=r["title"], location=r["location"] or "", terms=db.uj(r["terms"], None),
            description=r["description_text"], sponsorship=r["sponsorship"])]
        if not rows:
            typer.echo("nothing pending to triage")
            return
        out = run_triage(conn, _backend(), rows, profile, dry_run=dry_run, log=typer.echo)
    st = out["stats"]
    if dry_run:
        typer.echo(f"\nwould archive {st['dropped']} of {len(rows)} at threshold {profile.llm.triage_min_score}:")
        for r, score, reason in sorted(st["dropped_rows"], key=lambda x: -x[1])[:40]:
            typer.echo(f"  {score:3d}  {r['company_name']} — {r['title'][:60]}  ({reason})")
        typer.echo("\nnothing was written; re-run with --apply, or set llm.triage_enabled: true")
    typer.echo(json.dumps({k: v for k, v in st.items() if k not in ("errors", "dropped_rows")}))


@app.command()
def discover(
    angle: list[str] = typer.Option([], help="Research angle(s); defaults derived from profile"),
    like: Optional[str] = typer.Option(None, help="Find companies similar to this one"),
    limit: int = typer.Option(15, help="Companies per angle"),
    max_angles: Optional[int] = typer.Option(None, help="Default angles to research this run (default: llm.discovery_max_angles; they rotate across runs; 0 = all)"),
    resolve: bool = typer.Option(True, help="Try to resolve ATS boards for the newly found companies (free slug probing)"),
    model_resolve: bool = typer.Option(False, "--model-resolve/--no-model-resolve", help="Also spend one WebFetch model call per company the free probe can't resolve"),
) -> None:
    """Research companies matching your desired work; adds them as proposals. Costs frontier-model tokens: manual only."""
    from .discover import discover as _discover, estimate_tokens, new_usage, resolve_ats
    from .llm.base import LLMRateLimitError
    from .normalize import slugify

    profile = config.load_profile()
    n = len(angle) or (1 if like else (max_angles if max_angles is not None else profile.llm.discovery_max_angles))
    lo, hi = estimate_tokens(n or 13)
    typer.echo(f"~{lo // 1000}K-{hi // 1000}K tokens expected ({n or 'all'} angle(s) on {profile.llm.discovery_model})")
    backend = _backend()
    usage = new_usage()
    with db.session() as conn:
        run_id = db.start_run(conn, "discover")
        try:
            found = _discover(conn, backend, angles=angle or None, like=like, limit=limit, max_angles=max_angles, usage=usage, log=typer.echo)
            typer.echo(f"{len(found)} new companies proposed")
            if resolve and found:
                typer.echo("resolving ATS boards" + (" (model fallback on)..." if model_resolve else " (free probe only)..."))
                for c in found:
                    row = db.get_company_by_slug(conn, slugify(c.name))
                    if row is not None and row["ats_token"] is None:
                        resolve_ats(conn, backend if model_resolve else None, row, log=typer.echo, usage=usage)
        except LLMRateLimitError as e:
            usage["errors"].append(f"rate limit: {e}")
            typer.echo("usage limit hit; stopping. Companies found so far are kept.")
        _record_run(conn, run_id, usage)
    typer.echo("review with: jobhub companies list --status proposed")


@companies_app.command("list")
def companies_list(status: Optional[str] = typer.Option(None, help="approved | proposed | rejected | paused")) -> None:
    with db.session() as conn:
        rows = db.list_companies(conn, status=status)
    for c in rows:
        ats = f"{c['ats_type']}:{c['ats_token']}" if c["ats_token"] else (c["careers_url"] or "-")
        typer.echo(f"[{c['status']:8}] {c['slug']:28} {c['name'][:30]:30} tier={c['tier'] or '?'} fit={c['fit_score'] or '?'} rep={c['reputation_score'] or '?'} {ats}")
    typer.echo(f"{len(rows)} companies")


@companies_app.command("show")
def companies_show(slug: str) -> None:
    with db.session() as conn:
        c = db.get_company_by_slug(conn, slug)
    if c is None:
        raise typer.Exit(code=1)
    for k in c.keys():
        typer.echo(f"{k}: {c[k]}")


@companies_app.command("approve")
def companies_approve(slugs: list[str]) -> None:
    with db.session() as conn:
        for s in slugs:
            typer.echo(f"{s}: {'approved' if db.set_company_status(conn, s, 'approved') else 'not found'}")


@companies_app.command("reject")
def companies_reject(slugs: list[str]) -> None:
    with db.session() as conn:
        for s in slugs:
            typer.echo(f"{s}: {'rejected' if db.set_company_status(conn, s, 'rejected') else 'not found'}")


@companies_app.command("add")
def companies_add(name: str, ats: Optional[str] = typer.Option(None, help="e.g. greenhouse:stripe"), tier: Optional[int] = None,
                  careers_url: Optional[str] = None) -> None:
    from .normalize import slugify

    ats_type = ats_token = None
    if ats:
        ats_type, _, ats_token = ats.partition(":")
    with db.session() as conn:
        db.upsert_company(conn, slug=slugify(name), name=name, source="manual", status="approved", tier=tier,
                          ats_type=ats_type or None, ats_token=ats_token or None, careers_url=careers_url)
    typer.echo(f"added {slugify(name)} (approved)")


@companies_app.command("resolve")
def companies_resolve(status: str = typer.Option("approved"), use_model: bool = typer.Option(True)) -> None:
    """Detect ATS boards for companies that don't have one yet."""
    from .discover import resolve_ats

    from .discover import new_usage
    from .llm.base import LLMRateLimitError

    backend = _backend() if use_model else None
    usage = new_usage()
    with db.session() as conn:
        run_id = db.start_run(conn, "resolve")
        try:
            for c in db.list_companies(conn, status=status):
                if c["ats_token"] is None:
                    resolve_ats(conn, backend, c, log=typer.echo, usage=usage)
        except LLMRateLimitError as e:
            usage["errors"].append(f"rate limit: {e}")
            typer.echo("usage limit hit; stopping.")
        _record_run(conn, run_id, usage)


sources_app = typer.Typer(help="Aggregator repos (GitHub) that are polled every ingest.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")


@sources_app.command("list")
def sources_list() -> None:
    with db.session() as conn:
        for r in db.list_repo_sources(conn):
            typer.echo(f"[{r['status']:8}] {r['repo']:60} {r['kind'] or '-':9} ★{r['stars'] or 0:<6} last={r['last_jobs'] or 0:<5} via {r['source']}{(' — ' + r['last_error']) if r['last_error'] else ''}")


@sources_app.command("search")
def sources_search() -> None:
    """Search GitHub for new aggregator repos now (normally runs weekly during ingest)."""
    import re as _re

    from .sources.repo_discovery import discover_repos

    term = config.load_profile().term
    year = int(_re.search(r"20\d\d", term).group(0)) if _re.search(r"20\d\d", term) else 2027
    with db.session() as conn:
        n = discover_repos(conn, year, typer.echo, force=True)
    typer.echo(f"{n} new repos enabled")


@sources_app.command("disable")
def sources_disable(repo: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE repo_sources SET status = 'disabled' WHERE repo = ?", (repo,))
    typer.echo(f"{repo} disabled")


@sources_app.command("enable")
def sources_enable(repo: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE repo_sources SET status = 'active' WHERE repo = ?", (repo,))
    typer.echo(f"{repo} enabled")


@app.command()
def status(job_id: int, new_status: str, note: Optional[str] = typer.Option(None)) -> None:
    """Set application status: new|shortlisted|applied|interview|rejected|offer|skipped|closed (closed also deactivates the posting)."""
    from .models import AppStatus

    if new_status not in AppStatus.__members__:
        typer.echo(f"invalid status; choose from {', '.join(AppStatus.__members__)}")
        raise typer.Exit(code=1)
    with db.session() as conn:
        if db.get_job(conn, job_id) is None:
            typer.echo("no such job")
            raise typer.Exit(code=1)
        db.set_status(conn, job_id, new_status, note)
    typer.echo(f"job {job_id} -> {new_status}")


@app.command()
def show(job_id: Optional[int] = typer.Argument(None), bucket: Optional[str] = typer.Option(None),
         all: bool = typer.Option(False, "--all", help="Include jobs already given a status")) -> None:
    """Show one job with its evaluation, or list jobs in a bucket."""
    with db.session() as conn:
        if job_id is not None:
            j = db.get_job(conn, job_id)
            if j is None:
                typer.echo("no such job")
                raise typer.Exit(code=1)
            for k in ("id", "company_name", "title", "location", "canonical_url", "source", "posted_at", "terms", "sponsorship", "active"):
                typer.echo(f"{k}: {j[k]}")
            e = db.evaluation_for_job(conn, job_id)
            if e:
                typer.echo(f"bucket: {e['bucket']}  likelihood={e['likelihood']} desirability={e['desirability']} interest={e['interest']}")
                typer.echo(f"sub_scores: {e['sub_scores']}")
                typer.echo(f"hard_reject: {e['hard_reject_reason']}")
                typer.echo(f"red_flags: {e['red_flags']}")
                typer.echo(f"wildcard: {e['wildcard_reason']}")
                typer.echo(f"summary: {e['summary']}")
            a = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
            typer.echo(f"status: {a['status'] if a else 'new'}" + (f" — {a['notes']}" if a and a["notes"] else ""))
            typer.echo("--- description ---")
            typer.echo((j["description_text"] or "")[:3000])
            return
        rows = db.latest_evaluations(conn, config.RUBRIC_VERSION, config.profile_hash(), bucket=bucket, include_handled=all)
        for r in rows:
            typer.echo(f"{r['job_id']:6} [{r['bucket']:8}] L{r['likelihood'] or 0:3} D{r['desirability'] or 0:3} I{r['interest'] or 0:3}  {r['company_name'][:24]:24} {r['title'][:50]:50} {r['app_status']}")
        typer.echo(f"{len(rows)} jobs")


if __name__ == "__main__":
    app()
