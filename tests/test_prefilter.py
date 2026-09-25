from jobhub.config import Profile
from jobhub.prefilter import TRACK_ALT, TRACK_PRIMARY, TRACK_UNKNOWN, check, in_track, term_mismatch, term_track


def profile(**kw) -> Profile:
    base = {"term": "Summer 2027", "locations": ["Vancouver", "Remote"]}
    base.update(kw)
    return Profile.model_validate(base)


def test_term_mismatch_uses_structured_terms():
    p = profile()
    assert term_mismatch(p, "SWE Intern", ["Fall 2026"]) is True
    assert term_mismatch(p, "SWE Intern", ["Summer 2027"]) is False
    assert term_mismatch(p, "SWE Intern", ["Fall 2026", "Summer 2027"]) is False


def test_term_mismatch_from_title_only_when_explicit():
    p = profile()
    assert term_mismatch(p, "SWE Intern - Fall 2026", None) is True
    assert term_mismatch(p, "SWE Intern - Summer 2027", None) is False
    assert term_mismatch(p, "SWE Intern", None) is False


def two_track(**kw) -> Profile:
    return profile(term="Winter 2027", term_also_accept=["Spring 2027"], alt_terms=["Summer 2027"], **kw)


def test_alt_terms_are_accepted_not_rejected():
    p = two_track()
    assert term_mismatch(p, "SWE Intern", ["Summer 2027"]) is False
    assert term_mismatch(p, "SWE Intern - Summer 2027", None) is False
    assert term_mismatch(p, "SWE Intern", ["Summer 2026"]) is True     # a term we still don't want
    assert term_mismatch(profile(), "SWE Intern", ["Winter 2027"]) is True   # no alt_terms -> unchanged


def test_term_track_splits_the_two_tracks():
    p = two_track()
    assert term_track(p, "SWE Intern", ["Winter 2027"]) == TRACK_PRIMARY
    assert term_track(p, "Co-op - Spring 2027", None) == TRACK_PRIMARY
    assert term_track(p, "SWE Intern", ["Summer 2027"]) == TRACK_ALT
    assert term_track(p, "Summer 2027 SWE Intern", None) == TRACK_ALT
    assert term_track(p, "SWE Intern", None) == TRACK_UNKNOWN            # boards state no term at all
    assert term_track(p, "2027 Internship", None) == TRACK_UNKNOWN       # year but no season: either
    assert term_track(p, "SWE Intern", ["Winter 2027", "Summer 2027"]) == TRACK_UNKNOWN  # both
    assert term_track(profile(), "SWE Intern", ["Summer 2027"]) == TRACK_PRIMARY  # single-term profile


def test_in_track_shows_undated_postings_under_both():
    assert in_track(TRACK_UNKNOWN, "primary") and in_track(TRACK_UNKNOWN, "alt")
    assert in_track(TRACK_ALT, "alt") and not in_track(TRACK_ALT, "primary")
    assert in_track(TRACK_PRIMARY, "primary") and not in_track(TRACK_PRIMARY, "alt")
    assert all(in_track(t, "all") for t in (TRACK_PRIMARY, TRACK_ALT, TRACK_UNKNOWN))


def test_dealbreakers_in_description():
    p = profile()
    assert check(p, title="SWE Intern", location="Remote", terms=None,
                 description="Applicants must be a U.S. citizen. Paid.") == "citizenship"
    assert check(p, title="SWE Intern", location="Remote", terms=None,
                 description="This is an unpaid opportunity.") == "unpaid"
    assert check(p, title="SWE Intern", location="Remote", terms=None, description="Great role.") is None


def test_strict_location():
    p = profile(strict_location=True)
    assert check(p, title="SWE Intern", location="Austin, TX", terms=None, description="") == "location_not_allowed"
    assert check(p, title="SWE Intern", location="Remote - US", terms=None, description="") is None
    assert check(p, title="SWE Intern", location="Vancouver, BC", terms=None, description="") is None


def test_unknown_terms_pass_through():
    p = profile()
    assert term_mismatch(p, "SWE Intern", ["N/A"]) is False
    assert term_mismatch(p, "SWE Intern - Fall 2026", ["N/A"]) is True


def test_sponsorship_field():
    p = profile()
    assert check(p, title="SWE Intern", location="", terms=None, description=None,
                 sponsorship="U.S. Citizenship is Required") == "citizenship"
    assert check(p, title="SWE Intern", location="", terms=None, description=None,
                 sponsorship="Does Not Offer Sponsorship") is None


def test_multi_term_and_long_placements_pass():
    p = profile(term="Winter 2027", term_also_accept=["Spring 2027"])
    assert term_mismatch(p, "Software Co-op", ["Winter 2027", "Summer 2027"]) is False
    assert term_mismatch(p, "Software Co-op", ["Spring 2027"]) is False
    assert term_mismatch(p, "Software Co-op, Winter/Summer 2027 (8 months)", None) is False
    assert term_mismatch(p, "Software Co-op (Jan 2027 - Aug 2027)", None) is False
    assert term_mismatch(p, "Software Co-op 2027", None) is False
    assert term_mismatch(p, "Software Intern - Summer 2027", None) is True
    assert term_mismatch(p, "Software Intern - Fall 2026", ["Fall 2026"]) is True


def test_unpaid_ignores_benefits_boilerplate():
    p = profile()
    benefits = "Benefits include employee assistance program, unpaid time off, 401(k), employee stock purchase plan."
    assert check(p, title="Software Design Student", location="", terms=None, description=benefits) is None
    assert check(p, title="Software Intern", location="", terms=None, description="Salary range: unpaid. Working conditions: ...") == "unpaid"
    assert check(p, title="Software Intern", location="", terms=None, description="This is an unpaid internship for credit.") == "unpaid"


def test_title_exclude():
    p = profile()
    assert check(p, title="Accounting Internship", location="", terms=None, description=None) == "out_of_scope"
    assert check(p, title="Mechanical Design Engineering Intern", location="", terms=None, description=None) == "out_of_scope"
    assert check(p, title="Software Engineer Intern - Robotics", location="", terms=None, description=None) is None
    assert check(p, title="Quantitative Developer Intern", location="", terms=None, description=None) is None


def test_firmware_heavy_title_is_decisive():
    p = profile()
    assert check(p, title="Firmware Engineer Intern", location="Remote", terms=None,
                 description=None) == "firmware_heavy"
    assert check(p, title="Embedded Firmware Co-op", location="", terms=None,
                 description="Great team.") == "firmware_heavy"


def test_firmware_heavy_needs_multiple_signals():
    p = profile()
    robotics = "Develop C++ motion-planning software for autonomous robots. Familiarity with CAN bus a plus."
    assert check(p, title="Robotics Software Intern", location="Remote", terms=None, description=robotics) is None
    fw = "Write drivers for SPI, I2C and UART peripherals on STM32 microcontrollers using FreeRTOS."
    assert check(p, title="Embedded Systems Intern", location="Remote", terms=None, description=fw) == "firmware_heavy"
    # plain "can" as an English word must never count
    assert check(p, title="Software Intern", location="Remote", terms=None,
                 description="You can grow here. You can also learn SPI basics.") is None


def test_firmware_signals_gated_on_embedded_title():
    p = profile()
    # umbrella/systems postings mention protocols in passing; only an embedded/EE title unlocks the signal count
    umbrella = "Teams include kernel, GPU, and firmware. Some roles touch SPI, I2C and UART peripherals on MCUs."
    assert check(p, title="Systems Software Engineering Internships", location="Remote", terms=None,
                 description=umbrella) is None
    assert check(p, title="Electrical Engineer Intern, Implant Embedded Systems", location="", terms=None,
                 description=umbrella) == "firmware_heavy"


def test_frontend_and_fullstack_titles_out_of_scope():
    p = profile()
    assert check(p, title="Full Stack Developer Intern", location="Remote", terms=None, description="") == "out_of_scope"
    assert check(p, title="Frontend Engineer Intern", location="Remote", terms=None, description="") == "out_of_scope"
    assert check(p, title="Software Engineer Intern", location="Remote", terms=None, description="") is None


def test_trim_description_drops_boilerplate_tail():
    from jobhub.evaluate import trim_description

    body = "Requirements: strong C++ and profiling experience. " * 12
    text = body + "\n\nWe are an equal opportunity employer. Benefits include 401(k) and medical, dental."
    out = trim_description(text)
    assert "equal opportunity" not in out and "401(k)" not in out
    assert "strong C++" in out
    assert len(out) < len(text)


def test_trim_description_keeps_short_postings_with_early_markers():
    from jobhub.evaluate import trim_description

    # a marker in the first 400 chars must not swallow the whole posting
    text = "We are an equal opportunity employer. " + "Build a CUDA kernel pipeline. " * 20
    out = trim_description(text)
    assert "CUDA kernel" in out
