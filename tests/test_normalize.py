from jobhub.normalize import canonical_url, dedup_key, html_to_text, normalize_title, slugify


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("https://www.boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=Simplify&ref=Simplify")
    b = canonical_url("http://boards.greenhouse.io/acme/jobs/123/")
    assert a == b == "https://boards.greenhouse.io/acme/jobs/123"


def test_canonical_url_keeps_meaningful_query_sorted():
    assert canonical_url("https://x.com/jobs?id=9&lang=en") == "https://x.com/jobs?id=9&lang=en"
    assert canonical_url("https://x.com/jobs?lang=en&id=9") == "https://x.com/jobs?id=9&lang=en"


def test_dedup_key_collapses_variants():
    k1 = dedup_key("Acme Inc.", "Software Engineer Intern (Summer 2027)", "San Francisco, CA")
    k2 = dedup_key("Acme", "Software Engineering Intern - Summer 2027", "San Francisco, CA")
    k3 = dedup_key("Acme", "Software Engineer Intern", "New York, NY")
    assert k1 == k2
    assert k1 != k3


def test_normalize_title_and_slug():
    assert normalize_title("Machine Learning Intern – Summer 2027 (Remote)") == normalize_title("Machine Learning Intern")
    assert slugify("OpenAI, Inc.") == "openai"
    assert slugify("Jane Street") == "jane-street"
    assert slugify("Amazon (Vancouver)") == "amazon"


def test_html_to_text_handles_double_escaped():
    s = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(s) == "Hello & welcome\n\nOne"


def test_dedup_key_is_city_level():
    assert dedup_key("Point72", "Quantitative Developer Intern", "New York") == dedup_key("Point72", "Quantitative Developer Intern (Point72)", "New York, NY")
    assert dedup_key("Point72", "Quantitative Developer Intern", "NYC") == dedup_key("Point72", "Quantitative Developer Intern", "New York, NY, United States")
    assert dedup_key("X", "SWE Intern", "Austin, TX; Seattle, WA") == dedup_key("X", "SWE Intern", "Austin")
    assert dedup_key("X", "SWE Intern", "Austin") != dedup_key("X", "SWE Intern", "Seattle")
