import json

import pytest

from jobhub import config, db, fastscore
from jobhub.config import Profile
from jobhub.llm.base import LLMResult


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DIGESTS_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_CWD", tmp_path)
    monkeypatch.setattr(config, "load_profile", lambda: Profile(term="Winter 2027"))
    with db.session() as c:
        yield c


def add_job(conn, n, title, company="Acme", desc="We build things.", **kw):
    return db.insert_job(conn, dedup_key=f"k{n}", company_name=company, title=title, location="Remote",
                         canonical_url=f"https://x.com/{n}", source="t", description_text=desc, **kw)


def score(conn, title, company="Acme", desc="We build things."):
    jid = add_job(conn, abs(hash((title, company, desc))) % 10**8, title, company, desc)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return fastscore.FastScorer(conn, config.load_profile()).score(row)


def test_target_roles_outscore_generic_and_offtarget(conn):
    gpu = score(conn, "GPU Compiler Intern", desc="CUDA kernels and LLVM codegen.")
    swe = score(conn, "Software Engineer Intern", desc="Write Python and C++ code.")
    generic = score(conn, "Engineering Intern")
    off = score(conn, "Cybersecurity Analyst Intern")
    assert gpu.score > swe.score > generic.score > off.score
    assert gpu.band == fastscore.BAND_YES and off.band == fastscore.BAND_NO


def test_reputation_lifts_score_and_missing_description_is_flagged(conn):
    db.upsert_company(conn, slug="nvidia", name="NVIDIA", source="t", status="approved", tier=1)
    known = score(conn, "Software Engineer Intern", company="NVIDIA")
    unknown = score(conn, "Software Engineer Intern", company="Nobody Inc")
    assert known.score > unknown.score and "known company" in known.why
    jid = db.insert_job(conn, dedup_key="nodesc", company_name="Acme", title="Software Intern", location="",
                        canonical_url="https://x.com/nd", source="t", description_text=None)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert fastscore.FastScorer(conn, config.load_profile()).score(row).unread


def test_run_stores_rows_bucketed_and_never_overwrites_deep(conn):
    phash = config.profile_hash()
    strong = add_job(conn, 1, "GPU Compiler Intern", desc="CUDA and LLVM.")
    weak = add_job(conn, 2, "Marketing Intern")   # prefilter title_exclude
    deep = add_job(conn, 3, "Software Engineer Intern")
    db.insert_evaluation(conn, job_id=deep, rubric_version=config.RUBRIC_VERSION, profile_hash=phash, model="claude-haiku",
                         bucket="reach", likelihood=55, desirability=60, interest=10, sub_scores={}, red_flags=[], summary="deep")
    stats = fastscore.run_fast_scoring(conn, None, use_model=False, log=lambda *_: None)
    rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM evaluations WHERE profile_hash = ?", (phash,))}
    assert rows[strong]["model"] == "local" and rows[strong]["bucket"] in ("likely", "reach")
    assert rows[weak]["bucket"] == "archive"
    assert rows[deep]["model"] == "claude-haiku" and rows[deep]["summary"] == "deep"   # untouched
    assert rows[weak]["model"] == "prefilter"                                          # out-of-scope title: rules, not the scorer
    assert stats["scored_local"] == 1 and stats["hard_rejected"] == 1 and stats["llm_calls"] == 0


class FakeBackend:
    """Triage stand-in: one fixed score for every job, and a token cost per call."""

    def __init__(self, score, tokens):
        self.score, self.tokens, self.calls = score, tokens, 0

    def complete(self, system, prompt, schema, tools=(), model=None, effort=None):
        import re

        self.calls += 1
        ids = [int(i) for i in re.findall(r"job_id: (\d+)", prompt.split("\n\n", 1)[1])]
        return LLMResult(data={"triages": [{"job_id": i, "score": self.score, "reason": "ok"} for i in ids]},
                         usage={"input_tokens": self.tokens, "output_tokens": 0})


def _ambiguous_jobs(conn, n):
    # "Software Engineer Intern" with no keywords lands between lo and hi
    for i in range(n):
        add_job(conn, 100 + i, "Software Engineer Intern", desc="We build things.")


def test_triage_only_sees_ambiguous_band_and_persists_scores(conn):
    _ambiguous_jobs(conn, 3)
    add_job(conn, 1, "Marketing Intern")
    backend = FakeBackend(score=90, tokens=1000)
    stats = fastscore.run_fast_scoring(conn, backend, log=lambda *_: None)
    assert stats["triaged"] == 3 and backend.calls == 1
    rows = conn.execute("SELECT * FROM evaluations WHERE model = 'triage'").fetchall()
    assert len(rows) == 3 and all(r["bucket"] == "likely" for r in rows)
    assert json.loads(rows[0]["sub_scores"])["triage_score"] == 90


def test_token_budget_stops_triage_and_leaves_jobs_visible(conn):
    _ambiguous_jobs(conn, 90)   # three triage batches of 40/40/10 at llm.triage_batch_size
    backend = FakeBackend(score=90, tokens=30_000)
    stats = fastscore.run_fast_scoring(conn, backend, budget=50_000, log=lambda *_: None)
    assert stats["budget_stopped"] and backend.calls < 3
    left = fastscore.ambiguous_rows(conn, config.profile_hash())
    assert left and stats["deferred"] == len(left)
    assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE model = 'local'").fetchone()[0] >= len(left)


def test_weekly_cap_is_off_by_default_and_counts_recorded_runs_when_set(conn, monkeypatch):
    _ambiguous_jobs(conn, 5)
    rid = db.start_run(conn, "run")
    db.finish_run(conn, rid, llm_input_tokens=1_500_000, llm_output_tokens=600_000)
    assert fastscore.token_allowance(conn, config.load_profile()) == 300_000        # weekly cap off: only the run cap
    p = Profile(term="Winter 2027")
    p.llm.weekly_token_budget = 2_000_000                                            # 2.1M already spent this week
    monkeypatch.setattr(config, "load_profile", lambda: p)
    backend = FakeBackend(score=90, tokens=1000)
    stats = fastscore.run_fast_scoring(conn, backend, log=lambda *_: None)
    assert backend.calls == 0 and stats["budget_stopped"] and stats["reason"] == "weekly cap"
    assert fastscore.token_allowance(conn, p) == 0
    last = json.loads(db.get_meta(conn, "last_score"))
    assert last["deferred"] == 5 and last["reason"] == "weekly cap"                # surfaced to the UI, not silent


def test_budget_zero_means_unlimited(conn):
    assert fastscore.token_allowance(conn, config.load_profile(), override=0) is None


def test_unread_posting_is_never_a_clear_no_unless_offtarget(conn):
    def mk(n, title):
        jid = db.insert_job(conn, dedup_key=n, company_name="Acme", title=title, location="", canonical_url=f"https://x/{n}",
                            source="t", description_text=None)
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    sc = fastscore.FastScorer(conn, config.load_profile())
    generic, off = sc.score(mk("g", "Engineering Intern")), sc.score(mk("o", "Cybersecurity Analyst Intern"))
    assert generic.unread and generic.band == fastscore.BAND_AMBIGUOUS
    assert off.band == fastscore.BAND_NO


def test_recompute_reapplies_thresholds_without_a_model(conn, monkeypatch):
    add_job(conn, 1, "Software Engineer Intern", desc="Python and C++ code.")
    fastscore.run_fast_scoring(conn, None, use_model=False, log=lambda *_: None)
    before = conn.execute("SELECT bucket FROM evaluations WHERE model = 'local'").fetchone()[0]
    p = Profile(term="Winter 2027")
    p.fast_scoring.reach_min = 99
    p.fast_scoring.likely_min = 100
    monkeypatch.setattr(config, "load_profile", lambda: p)
    assert fastscore.recompute_fast(conn) == 1
    after = conn.execute("SELECT bucket FROM evaluations WHERE model = 'local'").fetchone()[0]
    assert before != "archive" and after == "archive"
