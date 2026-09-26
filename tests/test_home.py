"""The home dashboard and the theme system.

home_data() is where the front page's five cards come from, so the interesting cases are
the ones that are easy to get wrong: what counts as "new since last visit", what falls
inside the deadline window, and that the themes every page depends on are actually legible.
"""
from datetime import datetime, timedelta, timezone

import pytest

from jobhub import config, db, home
from jobhub.web import themes


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with db.session() as c:
        yield c


def row(job_id, *, bucket="likely", status="new", seen_days=-1, deadline=None, likelihood=80):
    return {
        "job_id": job_id, "company_name": f"Co {job_id}", "title": f"Intern {job_id}",
        "bucket": bucket, "app_status": status, "likelihood": likelihood, "desirability": 70,
        "first_seen_at": (datetime.now(timezone.utc) + timedelta(days=seen_days)).isoformat(),
        "deadline": deadline, "hard_reject_reason": None,
    }


def test_first_visit_counts_everything_unreviewed(conn):
    data = home.home_data(conn, rows=[row(1), row(2), row(3, status="applied")], stats={})
    assert data["since"] is None
    assert data["since_label"] == "your first visit"
    assert data["new_count"] == 2          # the applied one is not waiting on you
    assert data["unreviewed"] == 2


def test_new_since_last_visit_excludes_older_postings(conn):
    home.stamp_visit(conn)
    rows = [row(1, seen_days=-5), row(2, seen_days=-5)]
    assert home.home_data(conn, rows=rows, stats={})["new_count"] == 0

    fresh = row(3)
    fresh["first_seen_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    data = home.home_data(conn, rows=rows + [fresh], stats={})
    assert data["new_count"] == 1
    assert data["new_top"][0]["job_id"] == 3


def test_split_and_top_are_ordered_by_score(conn):
    rows = [row(1, likelihood=60), row(2, bucket="reach", likelihood=95),
            row(3, bucket="wildcard", likelihood=70)]
    data = home.home_data(conn, rows=rows, stats={})
    assert data["new_split"] == {"likely": 1, "reach": 1, "wildcard": 1}
    assert [r["job_id"] for r in data["new_top"]] == [2, 3, 1]


def test_archive_and_hard_rejects_stay_out_of_the_counts(conn):
    rows = [row(1), row(2, bucket="archive")]
    rows.append(dict(row(3), hard_reject_reason="unpaid"))
    data = home.home_data(conn, rows=rows, stats={})
    assert data["new_count"] == 1
    assert data["open_now"] == 1                       # likely + reach only
    assert dict(data["buckets"])["archive"] == 1       # still counted in the breakdown


def test_deadline_window_and_urgency(conn):
    rows = [row(1, deadline=_iso(2)), row(2, deadline=_iso(10)),
            row(3, deadline=_iso(60)), row(4, deadline=_iso(-3)), row(5)]
    data = home.home_data(conn, rows=rows, stats={})
    assert [d["job_id"] for d in data["deadlines"]] == [1, 2]   # sorted, windowed, past dropped
    assert data["deadline_total"] == 2
    assert data["deadlines"][0]["urgent"] is True               # 2 days
    assert data["deadlines"][1]["urgent"] is False              # 10 days


def test_deadline_card_carries_what_the_tile_renders(conn):
    data = home.home_data(conn, rows=[row(1, deadline=_iso(3), status="shortlisted")], stats={})
    d = data["deadlines"][0]
    assert d["mon"] and d["d"] and d["days"] == 3
    assert d["state"] == "shortlisted, not applied"


def test_stages_come_from_the_applications_table(conn):
    for i, status in enumerate(["applied", "applied", "interview", "skipped"], start=1):
        db.insert_job(conn, dedup_key=f"k{i}", company_name="Co", title="Intern", location="",
                      source="t", canonical_url=f"http://x/{i}")
        conn.execute("INSERT INTO applications (job_id, status, updated_at) VALUES (?, ?, ?)",
                     (i, status, "2026-09-25T00:00:00"))
    data = home.home_data(conn, rows=[], stats={})
    assert {s["key"]: s["n"] for s in data["stages"]}["applied"] == 2
    assert data["in_flight"] == 3          # skipped is not in flight


def test_visit_stamp_round_trips(conn):
    assert home.last_visit(conn) is None
    home.stamp_visit(conn)
    assert isinstance(home.last_visit(conn), datetime)


# ---------------------------------------------------------------- themes

def test_every_theme_defines_the_same_token_set():
    expected = set(themes.THEMES[themes.DEFAULT]["tokens"])
    for key, t in themes.THEMES.items():
        assert set(t["tokens"]) == expected, f"{key} token set differs"


def test_default_theme_exists_and_resolve_falls_back():
    assert themes.DEFAULT in themes.THEMES
    assert themes.resolve("nonsense") == themes.DEFAULT
    assert themes.resolve(None) == themes.DEFAULT
    assert themes.resolve("p4a") == "p4a"


def test_css_covers_every_theme_and_roots_the_default():
    css = themes.css()
    assert css.startswith(":root,")
    for key in themes.THEMES:
        assert f'[data-theme="{key}"]' in css
    assert "--accent:" in css


def _lum(hexv: str) -> float:
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@pytest.mark.parametrize("key", list(themes.THEMES))
def test_theme_is_legible(key):
    """AA for text, plus the two house floors that stop a theme losing its edges."""
    t = themes.THEMES[key]["tokens"]
    assert _ratio(t["ink"], t["surface"]) >= 4.5, "body text"
    assert _ratio(t["ink-2"], t["surface"]) >= 4.5, "secondary text"
    assert _ratio(t["accent-ink"], t["accent"]) >= 4.5, "text on the action"
    assert _ratio(t["hero-ink"], t["hero"]) >= 4.5, "text on the review card"
    assert _ratio(t["warn-ink"], t["warn-bg"]) >= 4.5, "alarm text"
    assert _ratio(t["border"], t["surface"]) >= 1.6, "card edge"
    assert _ratio(t["surface"], t["ground"]) >= 1.14, "card against page"
    for cat in ("likely", "reach", "wildcard", "archive"):
        assert _ratio(t[cat], t["surface"]) >= 3.0, f"{cat} swatch"


# ---------------------------------------------------------------- profile editing

@pytest.fixture
def yaml_profile(tmp_path, monkeypatch):
    """A profile.yaml with the two list styles the real file mixes, plus comments to preserve."""
    from jobhub import config as cfg
    f = tmp_path / "profile.yaml"
    f.write_text(
        'roles:                      # target role families\n'
        '  - "Systems"\n'
        '  - "ML"\n'
        'term: "Winter 2027"         # target work term\n'
        'term_also_accept: ["Spring 2027"]   # same window\n'
        'locations:\n'
        '  - "United States"\n'
        '  - "Remote"\n'
        'remote_ok: true\n'
        'strict_location: false\n'
        'work_authorization: ["Canada"]\n'
        'dealbreakers:\n'
        '  unpaid: ["unpaid"]\n'
        '  other: []      # e.g. ["on-site in Austin"]\n'
        'llm:\n'
        '  model: "haiku"\n'
    )
    monkeypatch.setattr(cfg, "PROFILE_YAML", f)
    cfg.load_profile.cache_clear()
    yield f
    cfg.load_profile.cache_clear()


def test_block_list_is_replaced_not_appended(yaml_profile):
    config.set_profile_field("locations", ["Bay Area", "Zurich"])
    text = yaml_profile.read_text()
    assert config.load_profile().locations == ["Bay Area", "Zurich"]
    assert "United States" not in text          # the old block really went away
    assert text.count("locations:") == 1


def test_comments_survive_an_edit(yaml_profile):
    config.set_profile_field("term", "Summer 2028")
    text = yaml_profile.read_text()
    assert config.load_profile().term == "Summer 2028"
    assert "# target work term" in text
    assert "# target role families" in text


def test_nested_dealbreaker_edit_stays_in_its_section(yaml_profile):
    config.set_profile_field("dealbreakers.other", "on-site only, night shift")
    p = config.load_profile()
    assert p.dealbreakers.other == ["on-site only", "night shift"]
    assert p.dealbreakers.unpaid == ["unpaid"]          # sibling untouched
    assert p.llm.model == "haiku"                       # next section untouched


def test_bool_and_csv_coercion(yaml_profile):
    assert config.set_profile_field("remote_ok", False) is False
    assert config.set_profile_field("work_authorization", "Canada, Japan") == ["Canada", "Japan"]
    p = config.load_profile()
    assert p.remote_ok is False and p.work_authorization == ["Canada", "Japan"]


def test_unknown_field_is_refused(yaml_profile):
    with pytest.raises(KeyError):
        config.set_profile_field("llm.model", "sonnet")     # not a profile constraint
    with pytest.raises(KeyError):
        config.set_profile_field("nonsense", "x")


def test_missing_key_leaves_the_file_untouched(yaml_profile):
    """A field the file does not declare must not be silently appended somewhere wrong."""
    yaml_profile.write_text(yaml_profile.read_text().replace('strict_location: false\n', ''))
    before = yaml_profile.read_text()
    with pytest.raises(KeyError):
        config.set_profile_field("strict_location", True)
    assert yaml_profile.read_text() == before


def test_quotes_in_a_value_do_not_corrupt_the_file(yaml_profile):
    config.set_profile_field("dealbreakers.other", ['he said "no"', "it's unpaid"])
    assert config.load_profile().dealbreakers.other == ['he said "no"', "it's unpaid"]


def test_model_visible_flag_drives_the_rescore_warning():
    """The UI only warns about a re-score when the field actually feeds the profile hash."""
    assert config.PROFILE_FIELDS["term"][1] is True
    assert config.PROFILE_FIELDS["dealbreakers.other"][1] is False


# ---------------------------------------------------------------- visit sessions

def test_refreshing_does_not_zero_the_card(conn, monkeypatch):
    """The bug this guards: stamping on every load collapsed the window to 'since you refreshed'."""
    base = datetime.now(timezone.utc)
    monkeypatch.setattr(home, "_now", lambda: base)
    home.stamp_visit(conn)                                   # first ever visit

    arrived = dict(row(1), first_seen_at=(base + timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(home, "_now", lambda: base + timedelta(minutes=10))
    assert home.home_data(conn, rows=[arrived], stats={})["new_count"] == 1

    for extra in (11, 12, 20):                               # refreshing inside the session
        monkeypatch.setattr(home, "_now", lambda e=extra: base + timedelta(minutes=e))
        home.stamp_visit(conn)
        assert home.home_data(conn, rows=[arrived], stats={})["new_count"] == 1


def test_a_gap_starts_a_new_session_and_clears_it(conn, monkeypatch):
    base = datetime.now(timezone.utc)
    monkeypatch.setattr(home, "_now", lambda: base)
    home.stamp_visit(conn)
    arrived = dict(row(1), first_seen_at=(base + timedelta(minutes=5)).isoformat())

    monkeypatch.setattr(home, "_now", lambda: base + timedelta(minutes=10))
    home.stamp_visit(conn)
    assert home.home_data(conn, rows=[arrived], stats={})["new_count"] == 1

    later = base + timedelta(minutes=10 + home.SESSION_GAP_MINUTES + 1)
    monkeypatch.setattr(home, "_now", lambda: later)
    home.stamp_visit(conn)                                   # cutoff rolls to the end of the last session
    assert home.home_data(conn, rows=[arrived], stats={})["new_count"] == 0


def test_postings_arriving_mid_session_still_appear(conn, monkeypatch):
    """A run while you have the page open must not be hidden by your own refresh."""
    base = datetime.now(timezone.utc)
    monkeypatch.setattr(home, "_now", lambda: base)
    home.stamp_visit(conn)
    monkeypatch.setattr(home, "_now", lambda: base + timedelta(minutes=5))
    home.stamp_visit(conn)
    fresh = dict(row(1), first_seen_at=(base + timedelta(minutes=6)).isoformat())
    monkeypatch.setattr(home, "_now", lambda: base + timedelta(minutes=7))
    assert home.home_data(conn, rows=[fresh], stats={})["new_count"] == 1
