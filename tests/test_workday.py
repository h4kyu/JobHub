from datetime import date, timedelta

import pytest

from jobhub.sources.workday import WorkdaySource, _posted, parse_token


def test_parse_token_accepts_two_and_three_segments():
    assert parse_token("intel/wd1/External") == ("intel", "wd1", "External")
    assert parse_token("intel/External") == ("intel", "wd1", "External")   # wd defaults
    assert parse_token("/intel/wd5/Careers/") == ("intel", "wd5", "Careers")


def test_parse_token_rejects_garbage():
    with pytest.raises(ValueError):
        parse_token("intel")


def test_source_is_not_a_complete_listing():
    # The endpoint is searched, not enumerated — absent jobs must never be marked inactive.
    assert WorkdaySource("intel/wd1/External", "Intel").complete_listing is False


def test_external_url_drops_the_cxs_prefix():
    s = WorkdaySource("intel/wd1/External", "Intel")
    assert s._external_url("/job/PRC-Beijing/Intern_JR1") == \
        "https://intel.wd1.myworkdayjobs.com/External/job/PRC-Beijing/Intern_JR1"


def test_posted_on_relative_dates():
    assert _posted("Posted Today") == date.today().isoformat()
    assert _posted("Posted Yesterday") == (date.today() - timedelta(days=1)).isoformat()
    assert _posted("Posted 30+ Days Ago") == (date.today() - timedelta(days=30)).isoformat()
    assert _posted("Posted 5 Days Ago") == (date.today() - timedelta(days=5)).isoformat()
    assert _posted(None) is None
    assert _posted("Posted Sometime") is None


def test_board_from_url_recovers_the_workday_triple():
    from jobhub.ingest import board_from_url

    # The site segment ("NVIDIAExternalCareerSite") is exactly what slug-guessing cannot recover,
    # and it is sitting in job URLs we already store.
    assert board_from_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/Intern_JR1") \
        == ("workday", "nvidia/wd5/NVIDIAExternalCareerSite")
    # an optional locale segment sits before the site on some tenants
    assert board_from_url("https://intel.wd1.myworkdayjobs.com/en-US/External/job/Phoenix/AI_JR9") \
        == ("workday", "intel/wd1/External")
    assert board_from_url("https://acme.wd3.myworkdaysite.com/Careers/job/x") == ("workday", "acme/wd3/Careers")


def test_board_from_url_still_handles_the_other_three():
    from jobhub.ingest import board_from_url

    assert board_from_url("https://boards.greenhouse.io/stripe/jobs/123") == ("greenhouse", "stripe")
    assert board_from_url("https://job-boards.greenhouse.io/vercel/jobs/9") == ("greenhouse", "vercel")
    assert board_from_url("https://jobs.lever.co/spotify/abc") == ("lever", "spotify")
    assert board_from_url("https://jobs.ashbyhq.com/openai/xyz/") == ("ashby", "openai")


def test_board_from_url_ignores_custom_portals_and_bad_sites():
    from jobhub.ingest import board_from_url

    assert board_from_url("https://amazon.jobs/en/jobs/123") is None
    assert board_from_url("https://jobs.apple.com/en-us/details/200") is None
    assert board_from_url("") is None
    # a URL with no site segment must not yield "job" as the site
    assert board_from_url("https://acme.wd1.myworkdayjobs.com/job/x") is None
