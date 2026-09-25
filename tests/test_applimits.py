from jobhub.applimits import company_key, find_limit


def test_reads_numeric_and_word_limits():
    assert find_limit("Candidates are limited to three (3) applications within a 30-day period.").count == 3
    assert find_limit("Application Limit: Candidates may submit a maximum of 3 applications within a 6-month period.").count == 3
    assert find_limit("Each applicant has the opportunity to apply to up to 4 separate business / location combinations in any given recruiting year.").count == 4
    assert find_limit("Please only apply for one internship position, choosing the role that best matches.").count == 1
    assert find_limit("Barclays accepts one application per season for any of our programme opportunities.").count == 1


def test_ignores_per_role_rules_and_non_application_text():
    assert find_limit("You may submit one application per role each year.") is None
    assert find_limit("Bending - Neck: Occasionally (up to 2 minutes in position).") is None
    assert find_limit("This position will remain open for applications for up to 30 days from the posting date.") is None


def test_company_key_merges_aliases():
    assert company_key("Flyzipline") == company_key("Zipline") == "zipline"
