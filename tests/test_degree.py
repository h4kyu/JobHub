"""Degree-level filtering. The cases here are real postings from the database, because the whole risk in
this filter is over-rejecting: most postings that mention a PhD accept a bachelor's."""
import pytest

from jobhub.config import Profile
from jobhub.degree import accepted_levels, ceiling, degree_mismatch, levels_in
from jobhub.prefilter import check

BS = ["bachelor"]


def profile(**kw) -> Profile:
    base = {"term": "Summer 2027", "locations": ["Vancouver", "Remote"]}
    base.update(kw)
    return Profile.model_validate(base)


# --- the title is authoritative ---------------------------------------------------------------------
# Waymo, AMD, NVIDIA, BlackRock and Netflix all encode the level in the title, and titles carry no
# boilerplate — so a title that names degrees is decided on without reading the description at all.

@pytest.mark.parametrize("title, expected", [
    ("2027 Summer Intern, PhD, Quantitative Software Engineer", "phd-only"),
    ("2027 Summer Intern, MS/PhD, Software Engineer, Multiverse", "master-only"),
    ("2027 Summer Intern, MS, Software Engineering, Behavior Test", "master-only"),
    ("Ph.D. Intern - AI/ML & Design Automation", "phd-only"),
    ("Machine Learning Intern - PhD", "phd-only"),
    ("Quantitative Master's Intern - Investments", "master-only"),
    ("Current PhD, AI Engineering Internship Program - Summer 2027", "phd-only"),
    # ...and the ones that stay: a bachelor's anywhere in the accepted set is enough.
    ("2027 Summer Intern, BS/MS, Software Engineering, Maneuvering Tech", None),
    ("2027 Summer Intern, BS, SysEng Software Engineer", None),
    ("Undergraduate Research Intern", None),
])
def test_title_decides(title, expected):
    assert degree_mismatch(BS, title, None) == expected
    # Every title above names a degree, so the description is not consulted at all.
    assert degree_mismatch(BS, title, "Currently enrolled in a PhD program.") == expected, "title wins"


def test_title_without_a_degree_falls_through_to_the_description():
    assert degree_mismatch(BS, "Software Engineering Intern", None) is None
    assert degree_mismatch(BS, "Software Engineering Intern", "We build fast systems.") is None
    assert degree_mismatch(BS, "Software Engineering Intern", "Currently enrolled in a PhD program.") == "phd-only"


# --- graduation dates and year of study are NEVER a reason to reject --------------------------------
# A Waterloo BASc runs Sept 2024 - May 2029 and falls outside many stated graduation windows while
# remaining perfectly eligible, so this module parses no dates and reads year-of-study words in one
# direction only: they add `bachelor` to the accepted set, which can never cause a rejection.

@pytest.mark.parametrize("description", [
    "Must be graduating between December 2027 and May 2028.",
    "Anticipated graduation date between December 2027 - June 2028 is required.",
    "Candidates must graduate within 12 months of completing the internship.",
    "You must be a rising senior returning to school after the internship.",
    "Eligibility: must be enrolled and returning to your studies in Fall 2027.",
    "Requirements: expected to graduate in 2026.",
    "Must be in your penultimate year of study.",
    "Currently enrolled in a degree program graduating no later than June 2027.",
])
def test_graduation_window_never_rejects(description):
    assert degree_mismatch(BS, "Software Engineer Intern", description) is None
    assert check(profile(), title="Software Engineer Intern", location="Remote", terms=None,
                 description=description) != "phd-only"


def test_year_words_only_ever_widen():
    """RBC: "Must be a Sophomore, Junior, or in a Masters program" is open to undergrads. The master's
    mention must not make it master-only, and the year words must not reject on their own either."""
    rbc = ("Must be a Sophomore, Junior, or in a Masters program (graduation date December 2027 - May 2029) "
           "with a major in Software Engineering or Computer Science.")
    assert degree_mismatch(BS, "Software Development Intern", rbc) is None
    rbc2 = ("Entering the final year of a four-year college or university program or enrolled in a relevant "
            "master's program (anticipating graduation Winter 2027 or Spring 2028).")
    assert degree_mismatch(BS, "Quantitative Technology Summer Analyst", rbc2) is None


# --- description: only requirement clauses, only unhedged ones --------------------------------------

def test_enrollment_requirement_sets_the_ceiling():
    optiver = ("- Currently enrolled in a PhD program in Statistics, Computer Science, Machine Learning, "
               "Mathematics, or a related STEM field with outstanding academic performance "
               "- Expected graduation between December 2027 - June 2029")
    assert degree_mismatch(BS, "Quantitative Research Intern", optiver) == "phd-only"
    # ByteDance's phrasing names no "program" or "degree" at all — the commonest PhD-only wording there is.
    bd = ("Minimum Qualifications - Must be able to commit to a 12-week full-time work. "
          "- Currently pursuing a PhD in Software Development, Computer Science, or a related technical discipline.")
    assert degree_mismatch(BS, "Machine Learning Engineer Intern - Basic Ranking", bd) == "phd-only"


def test_bachelor_in_the_accepted_set_passes():
    assert degree_mismatch(BS, "SWE Intern", "Currently pursuing a BS, MS or PhD in Computer Science.") is None
    assert degree_mismatch(BS, "SWE Intern", "Enrolled in a baccalaureate or graduate program in computer science.") is None
    # Dots must not split the list apart: "Ph.D. or B.S." accepts a bachelor's.
    assert degree_mismatch(BS, "SWE Intern", "Currently pursuing a Ph.D. or B.S. in Computer Science.") is None


def test_preferred_qualifications_set_no_ceiling():
    """Microsoft's generic SWE intern posting lists a Bachelor's as basic and a Doctorate as preferred.
    Reading the second as a requirement would archive the most ordinary internship on the board."""
    ms = ("Required qualifications - Currently pursuing a Bachelor's Degree in Computer Science. "
          "Preferred qualifications - Currently pursuing a Doctorate in Computer Science, Software Engineering, "
          "Artificial Intelligence, or a related technical field.")
    assert degree_mismatch(BS, "Software Engineering INTERN", ms) is None
    # ...even with no basic-qualification degree line at all.
    assert degree_mismatch(BS, "ML Research Intern",
                           "Preferred Qualifications: 1. Currently pursuing a PhD in computer science.") is None


@pytest.mark.parametrize("hedge", [
    "PhD preferred", "a PhD is a plus", "PhD nice to have", "PhD or equivalent experience",
    "Master's degree desirable", "PhD candidates are also welcome", "Masters and PhD students are also eligible",
])
def test_hedged_degrees_set_no_ceiling(hedge):
    assert degree_mismatch(BS, "Software Engineer Intern", f"Requirements: enrolled in a program. {hedge}.") is None


def test_pay_bands_are_ignored():
    """Waymo lists "Hourly Masters Pay / Hourly PhD Pay" and Intuitive lists every degree it hires."""
    pay = ("Requirements: currently enrolled in a degree program. Hourly Masters Pay $70-$70 USD. Hourly PhD Pay $85 USD. "
           "Actual pay will be determined based on degree-seeking academic program (PhD, Master's, Bachelor's, etc).")
    assert degree_mismatch(BS, "Software Engineer Intern", pay) is None


def test_bare_mentions_are_ignored():
    """A posting that merely talks about PhDs, or an aggregator page carrying a sidebar of the company's
    other openings (Waymo's fetched pages do), must not be read as a requirement."""
    assert degree_mismatch(BS, "Simulation Intern", "You will work alongside engineers and PhDs on hard problems.") is None
    sidebar = ("Requirements: enrolled in a degree program. | 2027 Summer Intern, PhD, Data Science INTERN | "
               "Mountain View, CA | $177k | View | 2027 Summer Intern, MS/PhD, Road Understanding | $146k | View")
    assert degree_mismatch(BS, "Simulation Intern", sidebar) is None


# --- the ceiling itself ----------------------------------------------------------------------------

def test_ceiling_is_the_highest_degree_held():
    assert degree_mismatch(["bachelor", "master"], "Intern, MS/PhD, SWE", None) is None
    assert degree_mismatch(["bachelor", "master"], "Intern, PhD, SWE", None) == "phd-only"
    assert degree_mismatch(["phd"], "Intern, PhD, SWE", None) is None
    assert ceiling(["bachelor", "PhD "]) == 4
    assert ceiling([]) == 0


def test_empty_degrees_disables_the_filter():
    assert degree_mismatch([], "2027 Summer Intern, PhD, Quantitative Software Engineer", None) is None
    p = profile(degrees=[])
    assert check(p, title="2027 Summer Intern, PhD, Quantitative SWE", location="Remote", terms=None,
                 description=None) is None


def test_unknown_degree_name_is_rejected_loudly():
    """A typo would silently switch the filter off, so validation must fail instead."""
    with pytest.raises(Exception):
        profile(degrees=["bachelors"])
    with pytest.raises(Exception):
        profile(degrees=["BSc"])


def test_prefilter_reports_the_reason():
    p = profile(degrees=["bachelor"])
    assert check(p, title="2027 Summer Intern, PhD, Quantitative SWE", location="Remote", terms=None,
                 description=None) == "phd-only"
    assert check(p, title="2027 Summer Intern, BS/MS, Software Engineering", location="Remote", terms=None,
                 description=None) is None


def test_graduate_is_only_a_degree_when_it_names_one():
    assert "master" in levels_in("pursuing a graduate degree in Engineering")
    assert "master" in levels_in("open to graduate students")
    for false_friend in ("recent graduate", "graduating in 2027", "expected to graduate next year"):
        assert levels_in(false_friend) == set(), false_friend


@pytest.mark.parametrize("text", [
    "graph databases", "graph design", "morph detection", "programming", "alpha delta",
])
def test_degree_patterns_do_not_match_mid_word(text):
    """"ph\\.?\\s?d" matches the "ph d" in "graph database" unless the pattern carries a leading boundary,
    and a posting for an AI-native *graph database* team would then read as PhD-only."""
    assert levels_in(text) == set(), text


@pytest.mark.parametrize("text, level", [
    ("Ph.D", "phd"), ("Ph D", "phd"), ("PhD", "phd"), ("Ph-D", "phd"), ("PhDs", "phd"),
    ("doctoral studies", "phd"), ("D.Phil", "phd"), ("MEng", "master"), ("MASc", "master"),
    ("B.A.Sc", "bachelor"), ("BEng", "bachelor"), ("baccalaureate", "bachelor"),
])
def test_degree_spellings(text, level):
    assert level in levels_in(text), text


def test_accepted_levels_reports_its_source():
    assert accepted_levels("Intern, MS/PhD, SWE", "anything") == ({"master", "phd"}, "title")
    levels, src = accepted_levels("SWE Intern", "Requirements: currently enrolled in a PhD program.")
    assert (levels, src) == ({"phd"}, "description")
    assert accepted_levels("SWE Intern", "We build fast systems.") is None
