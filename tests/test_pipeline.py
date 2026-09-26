import json

import pytest

from jobhub import config, db, fastscore, pipeline
from jobhub.config import Profile


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DIGESTS_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_CWD", tmp_path)
    monkeypatch.setattr(config, "load_profile", lambda: Profile(term="Winter 2027"))
    with db.session() as c:
        yield c


def add(conn, n, title, desc="We build things.", raw=None):
    return db.insert_job(conn, dedup_key=f"k{n}", company_name="Acme", title=title, location="", canonical_url=f"https://x/{n}",
                         source="t", description_text=desc, raw=json.dumps(raw) if raw else None)


def test_status_reports_backlogs_and_the_reason_a_cap_left_jobs_unrefined(conn):
    for i in range(3):
        add(conn, i, "Software Engineer Intern")                      # ambiguous after local scoring
    add(conn, 10, "Engineering Intern", desc=None, raw={"fetch_failures": db.FETCH_GIVE_UP})   # unreadable
    add(conn, 11, "Compiler Intern", desc=None, raw={"fetch_failures": 0})                      # still to fetch
    st = pipeline.pipeline_status(conn)
    assert st["score"]["pending"] == 4 and st["funnel"]["unscored"] == 5
    fastscore.run_fast_scoring(conn, None, use_model=False, log=lambda *_: None)
    st = pipeline.pipeline_status(conn)
    assert st["score"]["pending"] == 0 and st["triage"]["awaiting"] >= 3
    refine = [l for l in st["limits"] if l["kind"] == "refine"][0]
    assert "model off for this run" in refine["text"]                # the reason is stated, not implied
    assert sum(st["funnel"].values()) == 5 and st["funnel"]["unscored"] == 1   # the un-fetched job is still waiting


def test_set_profile_value_edits_in_place_and_keeps_comments(tmp_path, monkeypatch):
    y = tmp_path / "profile.yaml"
    y.write_text("term: 'Winter 2027'\nllm:\n  model: haiku\n  run_token_budget: 300000      # per run\n  triage_enabled: true\n"
                 "fast_scoring:\n  hi: 64   # clear yes\n")
    monkeypatch.setattr(config, "PROFILE_YAML", y)
    config.load_profile.cache_clear()
    assert config.set_profile_value("llm.run_token_budget", "1,000,000") == 1_000_000
    assert config.set_profile_value("llm.triage_enabled", "false") is False
    assert config.set_profile_value("fast_scoring.hi", 70) == 70
    text = y.read_text()
    assert "run_token_budget: 1000000      # per run" in text and "triage_enabled: false" in text and "hi: 70   # clear yes" in text
    assert config.load_profile().llm.run_token_budget == 1_000_000
    with pytest.raises(KeyError):
        config.set_profile_value("term", "x")                          # model-visible: not editable here
    with pytest.raises(ValueError):
        config.set_profile_value("llm.run_token_budget", "lots")
    assert "run_token_budget: 1000000" in y.read_text()               # a rejected edit leaves the file alone
    config.load_profile.cache_clear()


def test_warning_only_when_the_ai_pass_was_cut_short():
    w = pipeline.refine_warning({"reason": "subscription usage limit hit"}, 12)
    assert "usage limit" in w and "12 unclear postings are" in w and "resets" in w
    assert "per-run token cap" in pipeline.refine_warning({"reason": "run cap"}, 1)
    assert "weekly token cap" in pipeline.refine_warning({"reason": "weekly cap"}, 3)
    assert pipeline.refine_warning({"reason": "subscription usage limit hit"}, 0) is None   # nothing left unrefined
    assert pipeline.refine_warning({"reason": "model off for this run"}, 9) is None          # switched off, not interrupted
    assert pipeline.refine_warning({}, 9) is None


def test_status_carries_the_interruption_warning_into_the_limits(conn):
    for i in range(3):
        add(conn, i, "Software Engineer Intern")
    fastscore.run_fast_scoring(conn, None, use_model=False, log=lambda *_: None)
    db.set_meta(conn, "last_score", json.dumps({"at": db.now(), "deferred": 3, "reason": "subscription usage limit hit"}))
    st = pipeline.pipeline_status(conn)
    assert "usage limit" in st["triage"]["warning"]
    refine = [l for l in st["limits"] if l["kind"] == "refine"]
    assert len(refine) == 1 and refine[0]["level"] == "warn" and refine[0]["text"] == st["triage"]["warning"]
