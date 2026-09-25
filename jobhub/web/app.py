"""Local web UI: `jobhub serve` -> http://127.0.0.1:8765. Read/write access to the same SQLite DB the CLI uses,
plus buttons that launch CLI commands as background subprocesses with a live log."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .. import applimits, config, db, directory, fastscore, pipeline, prefilter, roletype, watchlist
from ..models import AppStatus, Bucket
from ..normalize import slugify

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False

@app.template_filter("k")
def _k(n) -> str:
    """Compact token counts: 1234 -> 1.2K, 2500000 -> 2.5M."""
    n = int(n or 0)
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}K" if n >= 10_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


_TASKS: dict[str, dict] = {}
_TASK_LOCK = threading.Lock()
ALLOWED_TASKS = {"ingest", "evaluate", "digest", "run", "rescore", "skills", "score", "deep"}


# ---------------- helpers ----------------

def _budget(conn) -> dict:
    """Token spend recorded in `runs` against the configured ceilings (see llm.run_token_budget / weekly_token_budget)."""
    llm = config.load_profile().llm
    week, day = db.tokens_used_since(conn, 7), db.tokens_used_since(conn, 1)
    cap = llm.weekly_token_budget
    return {"week": week, "day": day, "week_cap": cap, "run_cap": llm.run_token_budget,
            "pct": min(100, round(100 * week / cap)) if cap else 0,
            "level": "over" if cap and week >= cap else "high" if cap and week >= 0.75 * cap else "ok"}


def _stats(conn) -> dict:
    rv, ph = config.RUBRIC_VERSION, config.profile_hash()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    pending = db.count_pending_fast(conn, ph)
    last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "active": q("SELECT COUNT(*) FROM jobs WHERE active = 1"),
        "with_text": q("SELECT COUNT(*) FROM jobs WHERE active = 1 AND description_text IS NOT NULL AND description_text != ''"),
        "pending": pending,
        "awaiting_model": fastscore.count_ambiguous(conn, ph),   # local score only; a triage pass would refine these
        "evaluated": q("SELECT COUNT(DISTINCT job_id) FROM evaluations WHERE profile_hash = ? AND hard_reject_reason IS NULL", ph),
        "deep": q("SELECT COUNT(DISTINCT job_id) FROM evaluations WHERE profile_hash = ? AND hard_reject_reason IS NULL "
                  "AND model NOT IN ('prefilter', 'local', 'triage')", ph),
        "last_run": dict(last) if last else None,
        "budget": _budget(conn),
        "last_score": db.uj(db.get_meta(conn, "last_score"), {}) or {},
        "profile_hash": ph, "rubric": rv,
    }


def _bullets(summary: str | None) -> list[str]:
    """New-format summaries are '- ' bullet lines; older ones are prose (returned as no bullets)."""
    lines = [l.strip() for l in (summary or "").splitlines() if l.strip()]
    if lines and all(l.startswith(("- ", "• ", "* ")) for l in lines):
        return [l[2:].strip() for l in lines]
    return []


def _row(r) -> dict:
    d = dict(r)
    d["bullets"] = _bullets(d.get("summary"))
    d["terms"] = db.uj(d.get("terms"), []) or []
    d.update(_term_badge(config.load_profile(), d.get("title") or "", d["terms"]))
    raw0 = db.uj(d.get("raw"), {}) or {}
    m = d.get("model") or ""
    d["src"] = "rules" if m == "prefilter" else "triage" if m == "triage" else "fast" if m == "local" else "deep"
    d["fast"] = raw0.get("fast") or {}
    d["fit_tags"] = raw0.get("fit_tags") or []
    d["gap_tags"] = raw0.get("gap_tags") or []
    d["role"] = d["fast"].get("role") or roletype.classify(d.get("title") or "", d["fit_tags"], d.get("summary"), d.get("company_name"))
    d["role_label"] = roletype.LABELS[d["role"]]
    d.setdefault("tier", None)
    d["sub_scores"] = db.uj(d.get("sub_scores"), {}) or {}
    d["red_flags"] = db.uj(d.get("red_flags"), []) or []
    raw = db.uj(d.get("raw"), {}) or {}
    d["unread"] = bool(raw.get("description_unavailable"))
    d["confidence"] = d["sub_scores"].get("confidence")
    from ..digest import _is_new

    d["is_new"] = _is_new(r)
    d["due_days"] = None
    if d.get("deadline"):
        try:
            d["due_days"] = (datetime.fromisoformat(d["deadline"]).date() - datetime.now().date()).days
        except ValueError:
            pass
    return d


def _app_limit(limits: dict, used: dict, company_name: str | None) -> dict | None:
    """The company's application cap and how many of its postings you've marked applied (or later)."""
    key = applimits.company_key(company_name)
    lim = limits.get(key)
    if lim is None:
        return None
    n = used.get(key, 0)
    return {"count": lim.count, "used": n, "full": n >= lim.count, "text": lim.text}


def _term_badge(profile, title: str, terms: list[str]) -> dict[str, str]:
    """Track plus how to label it on a card: 'unknown' covers both a posting that names no term (most
    company boards) and one that fits both tracks, which are worth telling apart on screen."""
    track = prefilter.term_track(profile, title, terms)
    claimed = prefilter.posting_terms(title, terms)
    label = _term_labels(profile)[track] if track != prefilter.TRACK_UNKNOWN else (
        "either term" if claimed else "term unstated")
    hint = ("posting says: " + ", ".join(terms) if claimed and terms else
            "term read from the title" if claimed else "the posting does not state a work term")
    return {"track": track, "term_label": label, "term_hint": hint}


def _term_labels(profile) -> dict[str, str]:
    """Chip labels for the work-term tracks; `alt` is empty when the profile has no second term."""
    primary = " / ".join([profile.term] + list(profile.term_also_accept)) or "Target term"
    return {"all": "All terms", "primary": primary, "alt": " / ".join(profile.alt_terms)}


def _running_tasks() -> list[dict]:
    with _TASK_LOCK:
        return sorted((dict(t, log=None) for t in _TASKS.values()), key=lambda t: t["started"], reverse=True)


def _spawn(kind: str, args: list[str]) -> str:
    tid = uuid.uuid4().hex[:8]
    config.ensure_dirs()
    logdir = config.DATA_DIR / "logs"
    logdir.mkdir(exist_ok=True)
    logfile = logdir / f"web-{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, "-m", "jobhub.cli", kind] + args
    task = {"id": tid, "kind": kind, "args": args, "started": time.time(), "status": "running", "rc": None, "logfile": str(logfile)}
    with _TASK_LOCK:
        _TASKS[tid] = task

    def runner() -> None:
        with open(logfile, "w") as fh:
            fh.write(f"$ jobhub {kind} {' '.join(args)}\n")
            fh.flush()
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=config.ROOT)
            rc = proc.wait()
        with _TASK_LOCK:
            task["status"] = "done" if rc == 0 else "failed"
            task["rc"] = rc
            task["finished"] = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return tid


# ---------------- pages ----------------

@app.route("/")
def index():
    bucket = request.args.get("bucket", "likely")
    status = request.args.get("status", "new")
    term = request.args.get("term", "all")          # all | primary | alt (see prefilter.term_track)
    role = request.args.get("role", "all")          # all | a roletype key
    q = (request.args.get("q") or "").strip().lower()
    unread = request.args.get("unread") == "1"
    sort = request.args.get("sort", "match")
    src = request.args.get("src", "all")            # all | fast (local + triage scored) | deep (full rubric)
    watch = request.args.get("watch") == "1"        # only companies on the watchlist
    conn = db.connect()
    try:
        db.init_db(conn)
        profile = config.load_profile()
        rv, ph = config.RUBRIC_VERSION, config.profile_hash()
        # One pass over every bucket: the tab counters need the whole set anyway, and the term chips
        # count within the selected bucket, so both are cheaper to do here than in a second query.
        every = [_row(r) for r in db.latest_evaluations(conn, rv, ph, include_handled=True)]
        limits, used = applimits.cached_company_limits(conn), applimits.submitted_counts(conn)
        for r in every:
            r["app_limit"] = _app_limit(limits, used, r["company_name"])
        if status != "all":
            every = [r for r in every if r["app_status"] == status]
        src_counts = {"all": len(every), "fast": sum(1 for r in every if r["src"] in ("fast", "triage")),
                      "deep": sum(1 for r in every if r["src"] == "deep")}
        if src == "fast":
            every = [r for r in every if r["src"] in ("fast", "triage")]
        elif src == "deep":
            every = [r for r in every if r["src"] == "deep"]
        counts = {b.value: 0 for b in Bucket}
        for r in every:
            if prefilter.in_track(r["track"], term):
                counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
        in_bucket = every if bucket == "all" else [r for r in every if r["bucket"] == bucket]
        term_counts = {t: sum(1 for r in in_bucket if prefilter.in_track(r["track"], t)) for t in ("all", "primary", "alt")}
        rows = [r for r in in_bucket if prefilter.in_track(r["track"], term)]
        # Role chips count within bucket + term, and the other filters narrow further, like the term chips.
        role_counts = {"all": len(rows), **{k: sum(1 for r in rows if r["role"] == k) for k in roletype.KEYS}}
        if role != "all":
            rows = [r for r in rows if r["role"] == role]
        if unread:
            rows = [r for r in rows if r["unread"]]
        if watch:
            watched = watchlist.watch_slugs(conn)
            rows = [r for r in rows if slugify(r["company_name"] or "") in watched]
        if q:
            rows = [r for r in rows if q in (r["company_name"] or "").lower() or q in (r["title"] or "").lower() or q in (r["location"] or "").lower()]
        key = {"likely": lambda r: (r["likelihood"] or 0) * (r["desirability"] or 0), "reach": lambda r: (r["desirability"] or 0, r["likelihood"] or 0),
               "wildcard": lambda r: r["interest"] or 0}.get(bucket, lambda r: (r["desirability"] or 0, r["likelihood"] or 0))
        if sort == "newest":
            rows.sort(key=lambda r: (r["posted_at"] or r["first_seen_at"][:10] or ""), reverse=True)
        elif sort == "role":
            order = {k: i for i, k in enumerate(roletype.KEYS)}
            rows.sort(key=lambda r: (order.get(r["role"], 99), -(r["likelihood"] or 0)))   # role groups, best score first within each
        elif sort == "deadline":
            rows.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or "", -(r["desirability"] or 0)))  # soonest first, unknown last
        else:
            rows.sort(key=lambda r: (r["is_new"], key(r)), reverse=True)  # newly scored first, like the digest
        stats = _stats(conn)
    finally:
        conn.close()
    return render_template("index.html", rows=rows, bucket=bucket, status=status, term=term, role=role, q=q, unread=unread, sort=sort, watch=watch,
                           src=src, src_counts=src_counts,
                           counts=counts, term_counts=term_counts, term_labels=_term_labels(profile),
                           role_counts=role_counts, role_labels={"all": "All types", **roletype.LABELS},
                           stats=stats, statuses=[s.value for s in AppStatus], tasks=_running_tasks())


@app.route("/job/<int:job_id>")
def job(job_id: int):
    conn = db.connect()
    try:
        j = db.get_job(conn, job_id)
        if j is None:
            abort(404)
        e = db.evaluation_for_job(conn, job_id)
        a = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        urls = [r["url"] for r in conn.execute("SELECT url FROM job_urls WHERE job_id = ?", (job_id,))]
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (j["company_id"],)).fetchone() if j["company_id"] else None
        app_limit = _app_limit(applimits.cached_company_limits(conn), applimits.submitted_counts(conn), j["company_name"])
        stats = _stats(conn)
    finally:
        conn.close()
    jd = dict(j)
    jd["terms"] = db.uj(jd.get("terms"), []) or []
    profile = config.load_profile()
    jd.update(_term_badge(profile, jd["title"], jd["terms"]))
    ev = _row(e) if e else None
    return render_template("job.html", job=jd, ev=ev, app_limit=app_limit, term_labels=_term_labels(profile), app_status=(a["status"] if a else "new"), notes=(a["notes"] if a else ""),
                           urls=urls, company=dict(company) if company else None, statuses=[s.value for s in AppStatus], stats=stats, tasks=_running_tasks())


@app.route("/skills")
def skills():
    """What the aspirational postings ask for, and the plan to get there (see jobhub/skills.py)."""
    from .. import skills as skills_mod

    status_filter = request.args.get("status", "missing")
    conn = db.connect()
    try:
        db.init_db(conn)
        demands = skills_mod.load_demands(conn)
        plan = skills_mod.stored_plan(conn)
        generated_at = db.get_meta(conn, "skills_generated_at")
        counts = {s: sum(1 for d in demands if d["status"] == s) for s in ("missing", "partial", "have")}
        if status_filter != "all":
            demands = [d for d in demands if d["status"] == status_filter]
        job_ids = sorted({i for d in demands for i in d["job_ids"]})
        titles = {}
        if job_ids:
            qs = ",".join("?" * len(job_ids))
            for r in conn.execute(f"SELECT id, company_name, title FROM jobs WHERE id IN ({qs})", job_ids):
                titles[r["id"]] = f"{r['company_name']} — {r['title']}"
        stats = _stats(conn)          # base.html renders the header counters on every page
    finally:
        conn.close()
    return render_template("skills.html", demands=demands, plan=plan, generated_at=generated_at,
                           counts=counts, status_filter=status_filter, titles=titles, stats=stats,
                           tasks=_running_tasks())


@app.route("/companies")
def companies():
    """The watchlist: companies whose careers pages are read directly. Adding goes through the directory search."""
    conn = db.connect()
    try:
        db.init_db(conn)
        watched = watchlist.list_watchlist(conn)
        stats = _stats(conn)
    finally:
        conn.close()
    tags = [(k, roletype.LABELS.get(k, k), n) for k, n in sorted(directory.tag_counts().items(), key=lambda kv: -kv[1])]
    return render_template("companies.html", watched=watched, tags=tags, stats=stats, tasks=_running_tasks())


@app.route("/runs")
def runs():
    conn = db.connect()
    try:
        db.init_db(conn)
        history = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30")]
        for h in history:
            h["errors"] = db.uj(h.get("errors"), []) or []
        stats = _stats(conn)
        by_kind = {d: [dict(r) for r in db.tokens_by_kind(conn, d)] for d in (7, 30)}   # what actually spent the tokens
    finally:
        conn.close()
    digests = sorted((p.name for p in config.DIGESTS_DIR.glob("*.md")), reverse=True) if config.DIGESTS_DIR.exists() else []
    return render_template("runs.html", history=history, stats=stats, tasks=_running_tasks(), digests=digests,
                           by_kind=by_kind, llm=config.load_profile().llm)


def _flatten(prefix: str, obj) -> list[tuple[str, str]]:
    if isinstance(obj, dict):
        out: list[tuple[str, str]] = []
        for k, v in obj.items():
            out += _flatten(f"{prefix}.{k}" if prefix else str(k), v)
        return out
    return [(prefix, json.dumps(obj) if isinstance(obj, (list, tuple)) else str(obj))]


@app.route("/pipeline")
def pipeline_page():
    """Every stage, where the jobs are, what is holding some back, and the knobs and run buttons for each."""
    profile = config.load_profile()
    conn = db.connect()
    try:
        db.init_db(conn)
        st = pipeline.pipeline_status(conn)
        stats = _stats(conn)
    finally:
        conn.close()
    editable = {k: v for k, v in _flatten("", profile.model_dump()) if k in config.EDITABLE_KNOBS}
    return render_template("pipeline.html", st=st, stats=stats, llm=profile.llm, fs=profile.fast_scoring, editable=editable,
                           kinds={k: t.__name__ for k, t in config.EDITABLE_KNOBS.items()}, give_up=db.FETCH_GIVE_UP,
                           tasks=_running_tasks())


@app.post("/api/settings")
def api_settings():
    """Edit one whitelisted, non-model-visible knob in profile.yaml (comments preserved)."""
    data = request.get_json(force=True)
    try:
        value = config.set_profile_value(str(data.get("key", "")), data.get("value"))
    except KeyError as e:
        return jsonify({"error": str(e)}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"bad value: {e}"}), 400
    except Exception as e:   # validation of the rewritten profile failed; the file was restored
        return jsonify({"error": f"profile.yaml rejected the value: {str(e)[:200]}"}), 400
    return jsonify({"ok": True, "key": data["key"], "value": value})


@app.route("/settings")
def settings():
    """Every knob the code reads from profile.yaml, with its effective value. Read-only: edit profile/profile.yaml
    (comments there explain each one). Knobs the model sees are flagged, because changing those re-evaluates."""
    profile = config.load_profile()
    visible = {"roles", "term", "term_also_accept", "alt_terms", "length_weeks", "locations", "remote_ok",
               "strict_location", "work_authorization"}
    dump = profile.model_dump()
    sections = []
    top = [(k, v) for k, v in dump.items() if k in visible]
    sections.append({"name": "Profile constraints", "note": "Shown to the model. Editing these (or profile.md) changes the profile hash "
                     "and re-scores everything that is not carried over.", "rows": [r for k, v in top for r in _flatten(k, v)]})
    for name, note in (("dealbreakers", "Deterministic prefilter, applied before any scoring. Free to change."),
                       ("fast_scoring", "The free local score and its bands. Not model-visible; `jobhub score --calibrate` checks a change."),
                       ("scoring", "Deep-evaluation composite weights. Not model-visible; `jobhub rescore --recompute-only` re-buckets."),
                       ("buckets", "Deep-evaluation bucket gates."),
                       ("llm", "Models, batching, triage and token budgets."),
                       ("sources", "Aggregator repos polled every ingest."),
                       ("target_domains", "Fields mined by `jobhub skills` and rewarded by the fast score.")):
        sections.append({"name": name, "note": note, "rows": _flatten(name, dump[name])})
    conn = db.connect()
    try:
        db.init_db(conn)
        stats = _stats(conn)
    finally:
        conn.close()
    return render_template("settings.html", sections=sections, stats=stats, tasks=_running_tasks(),
                           yaml_path=str(config.PROFILE_YAML), md_path=str(config.PROFILE_MD))


@app.route("/digests/<name>")
def digest_view(name: str):
    if not re.fullmatch(r"[\w.-]+\.md", name):
        abort(404)
    path = config.DIGESTS_DIR / name
    if not path.exists():
        abort(404)
    conn = db.connect()
    try:
        stats = _stats(conn)
    finally:
        conn.close()
    return render_template("digest.html", name=name, content=path.read_text(), stats=stats, tasks=_running_tasks())


# ---------------- API ----------------

@app.post("/api/status")
def api_status():
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in AppStatus.__members__:
        return jsonify({"error": "bad status"}), 400
    conn = db.connect()
    try:
        if db.get_job(conn, int(data["job_id"])) is None:
            return jsonify({"error": "no such job"}), 404
        db.set_status(conn, int(data["job_id"]), status, data.get("notes"))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/directory/search")
def api_directory_search():
    conn = db.connect()
    try:
        db.init_db(conn)
        return jsonify(watchlist.suggest(conn, request.args.get("q", ""), request.args.get("tag") or None))
    finally:
        conn.close()


@app.post("/api/watchlist")
def api_watchlist_add():
    data = request.get_json(force=True)
    conn = db.connect()
    try:
        db.init_db(conn)
        try:
            if data.get("directory") is False:
                slug = watchlist.add_known(conn, (data.get("key") or "").strip())
            else:
                slug = watchlist.add_directory(conn, data.get("key") or "")
        except watchlist.NotFound:
            return jsonify({"error": "We don't recognize that company. Check the spelling."}), 404
    finally:
        conn.close()
    return jsonify({"ok": True, "slug": slug})


@app.post("/api/watchlist/<slug>/remove")
def api_watchlist_remove(slug: str):
    conn = db.connect()
    try:
        ok = watchlist.remove(conn, slug)
    finally:
        conn.close()
    return jsonify({"ok": ok})


@app.post("/api/tasks/<kind>")
def api_task(kind: str):
    if kind not in ALLOWED_TASKS:
        return jsonify({"error": "unknown task"}), 400
    with _TASK_LOCK:
        if any(t["status"] == "running" for t in _TASKS.values()):
            return jsonify({"error": "a task is already running"}), 409
    data = request.get_json(silent=True) or {}
    args: list[str] = []
    num = lambda k: int(data[k]) if str(data.get(k, "")).strip() not in ("", "None") else None
    if kind in ("ingest", "run") and num("fetch_limit") is not None:
        args += ["--fetch-limit", str(num("fetch_limit"))]
    if kind == "ingest" and data.get("retry_unreadable"):
        args += ["--retry-unreadable"]
    if kind in ("evaluate", "rescore", "run", "score") and data.get("limit"):
        args += ["--limit", str(int(data["limit"]))]
    if kind in ("run", "score"):
        if num("budget") is not None:
            args += ["--budget", str(num("budget"))]
        if data.get("no_model"):
            args += ["--no-model"]
    if kind == "rescore":
        if data.get("recompute_only"):
            args += ["--recompute-only"]
        if data.get("carry_over"):
            args += ["--carry-over"]
    if kind == "deep":
        args += [str(int(i)) for i in (data.get("job_ids") or [])]
        if data.get("top"):
            args += ["--top", str(int(data["top"]))]
            if data.get("bucket") in ("likely", "reach", "wildcard", "archive"):
                args += ["--bucket", data["bucket"]]
        if not args:
            return jsonify({"error": "pick at least one job, or set 'top N'"}), 400
    if kind == "skills":
        if data.get("limit"):
            args += ["--limit", str(int(data["limit"]))]
        if data.get("no_plan"):
            args += ["--no-plan"]
    return jsonify({"id": _spawn(kind, args)})


@app.get("/api/tasks")
def api_tasks():
    return jsonify(_running_tasks())


@app.get("/api/tasks/<tid>/log")
def api_task_log(tid: str):
    with _TASK_LOCK:
        t = _TASKS.get(tid)
    if not t:
        return jsonify({"error": "no such task"}), 404
    try:
        text = Path(t["logfile"]).read_text()
    except OSError:
        text = ""
    return jsonify({"status": t["status"], "rc": t["rc"], "log": text[-20000:]})


def serve(host: str = "127.0.0.1", port: int = 8765, debug: bool = False) -> None:
    config.ensure_dirs()
    db.init_db()
    app.run(host=host, port=port, debug=debug, threaded=True)
