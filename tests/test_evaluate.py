from jobhub.config import Profile
from jobhub.evaluate import assign_bucket, composite
from jobhub.models import Bucket, EvaluationOutput


def out(**kw) -> EvaluationOutput:
    base = dict(job_id=1, skills_match=50, level_match=50, work_alignment=50, experience_quality=50, interest=50)
    base.update(kw)
    return EvaluationOutput(**base)


def test_composite_weights():
    p = Profile()
    lik, des, interest = composite(out(skills_match=80, level_match=60, work_alignment=90, experience_quality=70, interest=40), 80, p)
    assert lik == round(0.65 * 80 + 0.35 * 60)
    assert des == round(0.45 * 90 + 0.30 * 70 + 0.25 * 80)
    assert interest == 40


def test_buckets():
    p = Profile()
    assert assign_bucket(70, 55, 10, False, p) == Bucket.likely
    assert assign_bucket(40, 80, 10, False, p) == Bucket.reach
    assert assign_bucket(10, 80, 10, False, p) == Bucket.archive  # too unlikely to be a reach
    assert assign_bucket(30, 40, 85, True, p) == Bucket.wildcard
    assert assign_bucket(30, 40, 85, False, p) == Bucket.archive
    assert assign_bucket(70, 55, 90, True, p) == Bucket.likely  # good fit wins over wildcard


def test_metadata_only_jobs_are_evaluated_not_dropped(tmp_path, monkeypatch):
    import json
    from jobhub import config, db
    from jobhub.evaluate import job_block
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DIGESTS_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_CWD", tmp_path)
    with db.session() as conn:
        given_up = db.insert_job(conn, dedup_key="a", company_name="Tesla", title="Compiler Intern", location="Palo Alto",
                                 canonical_url="https://tesla.com/1", source="t", raw=json.dumps({"fetch_failures": db.FETCH_GIVE_UP}))
        pending_fetch = db.insert_job(conn, dedup_key="b", company_name="X", title="Intern", location="", canonical_url="https://x.com/1",
                                      source="t", raw=json.dumps({"fetch_failures": 1}))
        rows = db.jobs_pending_evaluation(conn, "1", "h")
        ids = {r["id"] for r in rows}
        assert given_up in ids and pending_fetch not in ids
        block = job_block(next(r for r in rows if r["id"] == given_up))
        assert "DESCRIPTION UNAVAILABLE" in block and "Tesla" in block
