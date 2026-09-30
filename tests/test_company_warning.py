import pytest

from jobhub import config, db
from jobhub.models import Bucket
from jobhub.web import app as web


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DIGESTS_DIR", tmp_path)
    with db.session() as c:
        yield c


def add_scored_job(conn, number, company_id=None):
    jid = db.insert_job(
        conn,
        dedup_key=f"warning-{number}",
        company_id=company_id,
        company_name="Acme Defense",
        title=f"Software Intern {number}",
        location="Remote",
        canonical_url=f"https://example.com/jobs/{number}",
        source="test",
        description_text="Build software.",
    )
    db.insert_evaluation(
        conn,
        job_id=jid,
        rubric_version=config.RUBRIC_VERSION,
        profile_hash=config.profile_hash(),
        model="local",
        bucket=Bucket.likely.value,
        likelihood=80,
        desirability=80,
        sub_scores={"fast_score": 80, "local_score": 80},
        red_flags=[],
        raw={"fast": {"local": 80, "triage": None, "role": "systems", "band": "yes"}},
    )
    return jid


def test_company_warning_propagates_to_existing_and_future_jobs(conn):
    first = add_scored_job(conn, 1)
    second = add_scored_job(conn, 2)

    assert db.set_company_us_work_auth_warning(conn, first, True)
    company = db.get_company_by_slug(conn, "acme-defense")
    assert company["us_work_auth_warning"] == 1
    assert {r["company_id"] for r in conn.execute("SELECT company_id FROM jobs")} == {company["id"]}

    future = add_scored_job(conn, 3, company["id"])
    rows = {r["job_id"]: r for r in db.latest_evaluations(
        conn, config.RUBRIC_VERSION, config.profile_hash(), include_handled=True
    )}
    assert rows[first]["us_work_auth_warning"] == 1
    assert rows[second]["us_work_auth_warning"] == 1
    assert rows[future]["us_work_auth_warning"] == 1
    assert all(r["hard_reject_reason"] is None for r in rows.values())


def test_warning_api_can_skip_one_job_without_hiding_company_jobs(conn):
    first = add_scored_job(conn, 1)
    second = add_scored_job(conn, 2)
    conn.commit()
    client = web.app.test_client()

    response = client.post("/api/company-us-work-auth-warning", json={
        "job_id": first, "enabled": True, "skip": True,
    })
    assert response.get_json() == {"ok": True, "enabled": True, "skipped": True}
    assert conn.execute("SELECT status FROM applications WHERE job_id = ?", (first,)).fetchone()[0] == "skipped"
    assert conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (second,)).fetchone() is None

    page = client.get("/jobs?bucket=likely&status=all").get_data(as_text=True)
    assert page.count("us-warning") >= 2
    assert "U.S. work authorization / export-control risk" in page
    assert f'data-href="/job/{first}"' in page


def test_job_page_has_quick_status_and_autosave_controls(conn):
    jid = add_scored_job(conn, 1)
    conn.commit()
    page = web.app.test_client().get(f"/job/{jid}").get_data(as_text=True)
    assert 'class="status" id="jobstatus"' in page
    assert 'data-status="shortlisted"' in page
    assert 'data-status="applied"' in page
    assert 'data-status="skipped"' in page
    assert 'data-status="closed"' in page
    assert 'id="savestatus"' not in page

