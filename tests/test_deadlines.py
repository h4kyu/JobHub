from datetime import date

from jobhub.deadlines import extract_deadline, is_rolling

T = date(2026, 8, 29)


def test_extracts_common_phrasings():
    assert extract_deadline("Application deadline: October 15, 2026.", T) == "2026-10-15"
    assert extract_deadline("Please apply by Oct 15th to be considered.", T) == "2026-10-15"
    assert extract_deadline("Applications close on 1 November 2026", T) == "2026-11-01"
    assert extract_deadline("Applications are accepted until 2026-09-30.", T) == "2026-09-30"
    assert extract_deadline("Closing date: 9/12/2026", T) == "2026-09-12"


def test_ignores_start_dates_and_rolling():
    assert extract_deadline("The internship starts January 4, 2027 and runs 16 weeks.", T) is None
    assert extract_deadline("Applications reviewed on a rolling basis.", T) is None
    assert is_rolling("We review applications on a rolling basis") is True


def test_month_without_year_rolls_forward():
    assert extract_deadline("Apply before Feb 1.", T) == "2027-02-01"
