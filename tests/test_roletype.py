from jobhub.roletype import GENERAL, classify


def test_quant_firm_wins_whatever_the_title():
    assert classify("Software Developer Intern", company="DRW") == "quant"
    assert classify("Linux Engineer Intern", company="Jane Street") == "quant"
    assert classify("Software Engineer Intern", company="Signify") != "quant"   # "sig" is bounded


def test_title_decides_and_ties_go_to_the_specific_type():
    # "compiler" + "ai inference" (gpu) ties "ai" + "inference" (ml); gpu is listed first
    assert classify("Software Compiler Engineer Intern - AI Inference", summary="- AI AI machine learning llm") == "gpu"
    assert classify("Perception Intern (Summer 2027)", summary="- deep learning training models") == "robotics"
    assert classify("Robotics Planning & Controls Engineer Intern") == "robotics"
    assert classify("Software Engineer Intern - Recommendation Infra - Performance Efficiency") == "perf"


def test_fallback_to_tags_and_summary_needs_real_signal():
    assert classify("Software Engineer Intern", fit_tags=["robotics", "ros 2"]) == "robotics"
    assert classify("Software Engineer Intern", summary="- some performance work") == GENERAL
    assert classify("Software Engineer Intern") == GENERAL


def test_keywords_do_not_match_inside_words():
    assert classify("Brainstorm Intern") == GENERAL          # "ai" inside a word
    assert classify("Rosetta Platformer Intern") == GENERAL   # "ros", "platform"
