import pytest

from jobhub import config, db, directory, ingest, watchlist


CSV = """slug,name,aliases,ats_type,ats_token,status,jobs_at_check,tags,tier,reputation,domain,careers_url,verified_at,notes
nxp,NXP,NXP Semiconductors,workday,nxp/wd3/Careers,ok,10,hardware|ml,2,,,,2026-09-25,
acme,Acme Robotics,,greenhouse,acme,ok,3,robotics,,,,,2026-09-25,
gone,Gone Co,,greenhouse,gone,dead,,ml,,,,,2026-09-25,
hrt,Hudson River Trading,,,,no_feed,,quant,1,,,,,
"""


@pytest.fixture
def conn(tmp_path, monkeypatch):
    csv = tmp_path / "dir.csv"
    csv.write_text(CSV)
    monkeypatch.setattr(config, "DIRECTORY_CSV", csv)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for fn in (directory.load, directory._by_slug, directory._by_board):
        fn.cache_clear()
    with db.session() as c:
        yield c
    for fn in (directory.load, directory._by_slug, directory._by_board):
        fn.cache_clear()


def add_job(conn, company):
    db.insert_job(conn, dedup_key=company, company_name=company, title="Intern", location="",
                  canonical_url=f"https://x.com/{company}", source="simplify:x", description_text="d")


def test_search_ranks_exact_then_prefix_and_matches_aliases(conn):
    assert [e.name for e in directory.search("nxp semi")] == ["NXP"]
    assert [e.name for e in directory.search("", "robotics")] == ["Acme Robotics"]
    assert directory.search("zzz") == []


def test_only_live_boards_are_pollable(conn):
    by = {e.slug: e for e in directory.load()}
    assert by["nxp"].pollable and not by["gone"].pollable and not by["hrt"].pollable


def test_add_directory_entry_polls_its_board_and_remove_stops_it(conn):
    slug = watchlist.add_directory(conn, "acme")
    row = db.get_company_by_slug(conn, slug)
    assert (row["status"], row["ats_type"], row["ats_token"]) == ("approved", "greenhouse", "acme")
    assert [c["slug"] for c in db.list_companies(conn, status="approved")] == ["acme"]
    assert watchlist.remove(conn, slug)
    assert db.list_companies(conn, status="approved") == []


def test_adding_reuses_the_existing_row_for_an_alias(conn):
    db.upsert_company(conn, slug="nxp-semiconductors", name="NXP Semiconductors", source="harvest", status="proposed",
                      ats_type="workday", ats_token="nxp/wd3/Careers")
    assert watchlist.add_directory(conn, "nxp") == "nxp-semiconductors"
    assert len(db.list_companies(conn)) == 1


def test_dead_or_missing_board_follows_via_lists_only(conn):
    watchlist.add_directory(conn, "gone")
    watchlist.add_directory(conn, "hrt")
    rows = {r["slug"]: r for r in db.list_companies(conn, status="approved")}
    assert rows["gone"]["ats_token"] is None and rows["hrt"]["ats_token"] is None
    assert ingest.build_sources(conn) is not None  # nothing to poll for either, and nothing breaks


def test_unknown_company_is_refused_but_one_seen_in_job_lists_is_allowed(conn):
    with pytest.raises(watchlist.NotFound):
        watchlist.add_known(conn, "Typo Corp")
    add_job(conn, "Small Startup")
    slug = watchlist.add_known(conn, "Small Startup")
    assert db.get_company_by_slug(conn, slug)["status"] == "approved"
    assert {n["name"] for n in watchlist.suggest(conn, "small")["results"]} == {"Small Startup"}


def test_suggest_marks_watched_and_hides_directory_names_from_job_list_matches(conn):
    add_job(conn, "NXP Semiconductors")
    watchlist.add_directory(conn, "nxp")
    res = watchlist.suggest(conn, "nxp")["results"]
    assert [(r["name"], r["on"], r["in_directory"]) for r in res] == [("NXP", True, True)]


def test_seed_and_harvest_add_new_companies_but_never_bring_back_a_removed_one(conn, monkeypatch):
    monkeypatch.setattr(config, "load_companies_yaml", lambda: {"companies": [{"name": "Seedy", "ats": "greenhouse:seedy"}]})
    ingest.sync_companies_yaml(conn)
    assert db.get_company_by_slug(conn, "seedy")["status"] == "approved"
    watchlist.remove(conn, "seedy")
    ingest.sync_companies_yaml(conn)
    assert db.get_company_by_slug(conn, "seedy")["status"] == "paused"
    db.insert_job(conn, dedup_key="h", company_name="Harvested", title="Intern", location="",
                  canonical_url="https://boards.greenhouse.io/harvested/jobs/1", source="simplify:x", description_text=None)
    ingest.harvest_boards(conn, log=lambda *_: None)
    assert db.get_company_by_slug(conn, "harvested")["status"] == "approved"
    watchlist.remove(conn, "harvested")
    ingest.harvest_boards(conn, log=lambda *_: None)
    assert db.get_company_by_slug(conn, "harvested")["status"] == "paused"


# ---- aggregator list health: one network blip must not switch a list off for good ----

import httpx


def _status(conn, repo):
    return conn.execute("SELECT status FROM repo_sources WHERE repo = ?", (repo,)).fetchone()["status"]


def _http_error(code):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def test_network_errors_do_not_break_a_list_but_a_404_does(conn):
    db.upsert_repo_source(conn, "a/list", kind="readme", source="github_search", last_jobs=500)
    ingest.record_repo_failure(conn, "a/list", httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    assert _status(conn, "a/list") == "active"
    ingest.record_repo_failure(conn, "a/list", _http_error(503))
    assert _status(conn, "a/list") == "active"
    ingest.record_repo_failure(conn, "a/list", _http_error(404))
    assert _status(conn, "a/list") == "broken"


def test_broken_lists_are_retried_and_recover_but_disabled_ones_stay_off(conn):
    db.upsert_repo_source(conn, "gone/list", kind="readme", source="github_search", status="broken")
    db.upsert_repo_source(conn, "off/list", kind="readme", source="github_search", status="disabled")
    db.upsert_repo_source(conn, "ok/list", kind="readme", source="github_search")
    names = {s.name for s in ingest.build_sources(conn)}
    assert "readme:gone/list" in names and "readme:ok/list" in names and "readme:off/list" not in names
    ingest.record_repo_success(conn, "gone/list", 400)
    assert _status(conn, "gone/list") == "active"
    ingest.record_repo_success(conn, "off/list", 400)
    assert _status(conn, "off/list") == "disabled"


def test_a_broken_list_that_still_parses_to_almost_nothing_stays_broken(conn):
    db.upsert_repo_source(conn, "thin/list", kind="readme", source="github_search", status="broken")
    ingest.record_repo_success(conn, "thin/list", 3)
    assert _status(conn, "thin/list") == "broken"


# ---- Pipeline > Sources: turn lists on/off, add one, list boards ----

def test_source_endpoints_toggle_validate_and_the_status_lists_boards(conn):
    from jobhub import pipeline
    from jobhub.web import app as web

    db.upsert_repo_source(conn, "a/list", kind="readme", source="github_search", last_jobs=40)
    watchlist.add_directory(conn, "acme")
    conn.commit()
    client = web.app.test_client()
    assert client.post("/api/sources/a/list/disable").get_json() == {"ok": True}
    assert _status(conn, "a/list") == "disabled"
    assert all(l["repo"] != "a/list" for l in pipeline.pipeline_status(conn)["sources"]["lists"])   # removed lists leave the panel
    assert client.post("/api/sources/a/list/enable").get_json() == {"ok": True}
    assert client.post("/api/sources/a/list/sideways").status_code == 400
    assert client.post("/api/sources", json={"repo": "not a repo"}).status_code == 400
    st = pipeline.pipeline_status(conn)["sources"]
    assert [b["name"] for b in st["boards"]] == ["Acme Robotics"] and st["boards"][0]["token"] == "acme"
    assert any(l["repo"] == "a/list" for l in st["lists"])
