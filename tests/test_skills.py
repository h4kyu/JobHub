import pytest

from jobhub import skills
from jobhub.config import Profile, TargetDomain
from jobhub.skills import PostingSkills, SkillItem


def domains():
    return [
        TargetDomain(name="GPU", keywords=["cuda", "gpu kernel", "hip", "tensor core"]),
        TargetDomain(name="Compilers", keywords=["llvm", "mlir", "compiler"]),
    ]


def hits(title, desc):
    return skills.domain_hits(skills._domain_patterns(domains()), title, desc)


def test_keywords_do_not_match_inside_words():
    # "hip" must not fire on "internship" — this silently tagged every posting as GPU work.
    assert hits("Civil Drafting Internship", "a summer internship in drafting") == {}
    assert hits("Championship Software Intern", "worship of clean code") == {}


def test_keyword_matches_on_word_boundaries():
    assert hits("", "we use HIP and ROCm") == {"GPU": 1}
    assert hits("", "CUDA kernels, and an LLVM backend") == {"GPU": 1, "Compilers": 1}


def test_title_matches_count_double():
    body = hits("Software Intern", "some work on a compiler")
    title = hits("Compiler Intern", "some work on a compiler")
    assert body == {"Compilers": 1}
    assert title == {"Compilers": 3}  # 1 body + 2x title


def test_aggregate_counts_and_pessimistic_status():
    postings = {
        1: {"company": "Acme", "domains": ["GPU"]},
        2: {"company": "Globex", "domains": ["Compilers"]},
    }
    ex = [
        PostingSkills(job_id=1, skills=[
            SkillItem(skill="CUDA", category="library/tool", importance="required", status="missing", evidence="a"),
            SkillItem(skill="C++20", category="language", importance="preferred", status="have", evidence="b"),
        ]),
        PostingSkills(job_id=2, skills=[
            SkillItem(skill="cuda", category="library/tool", importance="required", status="partial", evidence="c"),
            SkillItem(skill="C++20", category="language", importance="required", status="partial", evidence="d"),
        ]),
    ]
    agg = {e["skill"].lower(): e for e in skills.aggregate(ex, postings)}
    cuda = agg["cuda"]
    assert cuda["required_count"] == 2 and cuda["demand_count"] == 2
    assert cuda["status"] == "missing"          # most pessimistic judgement wins
    assert set(cuda["domains"]) == {"GPU", "Compilers"}
    assert cuda["companies"] == ["Acme", "Globex"]
    cpp = agg["c++20"]
    assert cpp["required_count"] == 1 and cpp["preferred_count"] == 1
    assert cpp["status"] == "partial"


def test_aggregate_ranks_required_over_volume():
    postings = {i: {"company": f"C{i}", "domains": ["GPU"]} for i in range(1, 5)}
    ex = [PostingSkills(job_id=1, skills=[SkillItem(skill="MustHave", importance="required", status="missing")])]
    ex += [PostingSkills(job_id=i, skills=[SkillItem(skill="NiceToHave", importance="preferred", status="missing")])
           for i in (2, 3, 4)]
    ranked = skills.aggregate(ex, postings)
    assert ranked[0]["skill"] == "MustHave"     # 1 required beats 3 preferred


def test_aggregate_ignores_unknown_job_ids():
    assert skills.aggregate([PostingSkills(job_id=99, skills=[SkillItem(skill="X")])], {}) == []


def test_clean_text_unescapes_model_quote_artifacts():
    assert skills.clean_text(r'turning \"fast C++\" into proof') == 'turning "fast C++" into proof'
    assert skills.clean_text("  plain text  ") == "plain text"
