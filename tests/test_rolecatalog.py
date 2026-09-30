"""The catalog, the picks derived from it, and the promise that widening it changed no existing score."""
import pytest

from jobhub import config, rolecatalog, roletype
from jobhub.config import Profile


def test_catalog_is_well_formed():
    keys = [t.key for t in rolecatalog.ALL]
    assert len(keys) == len(set(keys)), "duplicate key in the catalog"
    for t in rolecatalog.CATALOG:
        assert t.keywords, f"{t.key} has no keywords, so nothing can ever classify as it"
        assert t.group in rolecatalog.GROUPS, f"{t.key} is in group {t.group!r}, which the picker won't show"
        for w in t.keywords:
            # roletype._alternation puts one boundary pair around the whole alternation, so a keyword that does
            # not start and end alphanumeric would silently never match.
            assert w[:1].isalnum() and w[-1:].isalnum(), f"{t.key}: {w!r} can never match"
            assert w == w.lower(), f"{t.key}: {w!r} is matched against lowered text"
    assert rolecatalog.ALL[-1].key == rolecatalog.GENERAL, "the fallback must sort last"


def test_every_group_has_members_and_the_picker_covers_the_catalog():
    shown = [t.key for _, types in rolecatalog.by_group() for t in types]
    assert sorted(shown) == sorted(t.key for t in rolecatalog.CATALOG)


def test_the_original_eight_keys_survive_with_their_relative_order():
    """directory/board_directory.csv tags 740 companies with these, and stored raw.fast.role rows name them."""
    original = ["quant", "gpu", "robotics", "perf", "ml", "systems", "hardware", "general"]
    assert all(k in rolecatalog.BY_KEY for k in original)
    order = [k for k in (t.key for t in rolecatalog.ALL) if k in original]
    assert order == original, "re-ordering these changes how already-scored postings classify"


# ---------------- weights ----------------

def _profile(**kw):
    return Profile(term="Winter 2027", **kw)


def test_picked_excluded_and_neutral_are_three_different_things():
    p = _profile(role_types=[{"key": "gpu", "weight": 88}], excluded_role_types=["frontend"])
    assert p.role_weight("gpu") == 88
    assert p.role_weight("frontend") == p.fast_scoring.excluded_weight
    # Not picking something is not rejecting it: an unpicked type scores like any unremarkable posting.
    assert p.role_weight("security") == p.fast_scoring.neutral_weight
    assert p.role_weight("general") == p.fast_scoring.neutral_weight


def test_only_picked_types_lend_their_keywords_to_the_bonus():
    p = _profile(role_types=[{"key": "robotics", "weight": 70}])
    kw = p.picked_keywords()
    assert "slam" in kw and "cuda" not in kw


def test_a_custom_type_classifies_and_wins_ties_over_the_catalog():
    p = _profile(role_types=[{"key": "my-photonics", "label": "Photonics",
                              "weight": 90, "keywords": ["photonic", "waveguide"]}])
    assert roletype.classify("Photonic Integrated Circuits Intern", profile=p) == "my-photonics"
    assert p.role_weight("my-photonics") == 90
    assert "Photonics" in p.target_role_labels()
    # A custom key the catalog doesn't know still gets a label, so the chip row can't render a bare slug.
    assert roletype.labels(p)["my-photonics"] == "Photonics"


def test_unusable_custom_keywords_are_dropped_not_asserted_at_page_load():
    p = _profile(role_types=[{"key": "my-x", "weight": 50, "keywords": ["c++", " ", "rust"]}])
    assert p.role_types[0].keywords == ["rust"]
    roletype.classify("Rust Systems Intern", profile=p)   # must not raise


def test_a_posting_you_did_not_target_still_gets_an_honest_badge():
    p = _profile(role_types=[{"key": "gpu", "weight": 80}])
    assert roletype.classify("Security Engineer Intern", profile=p) == "security"
    assert p.role_weight("security") == p.fast_scoring.neutral_weight


def test_duplicate_keys_collapse_to_one():
    p = _profile(role_types=[{"key": "gpu", "weight": 10}, {"key": "gpu", "weight": 90}])
    assert len(p.role_types) == 1 and p.role_weight("gpu") == 90


# ---------------- migration ----------------

def test_migration_carries_old_weights_onto_the_types_they_were_split_into():
    """Widening the catalog split Compilers out of GPU and Databases out of Systems. A straight read would drop
    those to the neutral weight and archive postings that used to score well, so they inherit the parent."""
    data = {"fast_scoring": {"role_weights": {"gpu": 80, "systems": 66, "general": 36, "hardware": 20}}}
    config._migrate_role_types(data)
    p = Profile.model_validate(data)
    assert p.role_weight("compilers") == 80        # was a GPU keyword
    assert p.role_weight("database") == 66         # was a Systems keyword
    assert p.role_weight("hardware") == p.fast_scoring.excluded_weight   # at/below general = "not this"
    assert p.fast_scoring.neutral_weight == 36     # the old `general` weight
    assert "hardware" in p.excluded_role_types


def test_migration_leaves_an_already_migrated_profile_alone():
    data = {"role_types": [{"key": "ml", "weight": 51}],
            "fast_scoring": {"role_weights": {"gpu": 80}}}
    config._migrate_role_types(data)
    assert data["role_types"] == [{"key": "ml", "weight": 51}]


def test_the_shipped_profile_is_a_usable_pick_list():
    """The repo's own profile.yaml, as the loader sees it after any migration."""
    p = config.load_profile()
    assert p.role_types, "profile.yaml has no picks, so every posting would score the neutral weight"
    for r in p.role_types:
        assert r.key in rolecatalog.BY_KEY or r.keywords, f"{r.key} is neither a catalog key nor a custom type"
        assert r.weight > p.fast_scoring.lo, f"{r.key} is picked but can never clear the archive on its own"
    assert not set(p.excluded_role_types) & set(p.picked()), "a type cannot be both wanted and excluded"
    assert all(k in rolecatalog.BY_KEY for k in p.excluded_role_types)


# ---------------- what the model is told ----------------

def test_target_roles_are_derived_from_the_picks():
    p = _profile(role_types=[{"key": "gpu", "weight": 80}, {"key": "robotics", "weight": 70}])
    assert config.model_visible_constraints(p)["target_roles"] == ["GPU / Accelerators", "Robotics / Autonomy"]


@pytest.mark.parametrize("key", ["gpu", "frontend", "compbio"])
def test_a_catalog_label_is_never_a_bare_key(key):
    assert rolecatalog.label(key) != key and rolecatalog.label(key).strip()


def test_a_trading_firm_overrides_the_title_except_for_explicit_research():
    """Wanting the engineering at a trading firm without the alpha research is a normal preference, so the
    company override yields to a title that says outright it is research or trading."""
    assert roletype.classify("Software Engineer Intern", company="DRW") == "quant"
    assert roletype.classify("Linux Engineer Intern", company="Jane Street") == "quant"
    assert roletype.classify("Quantitative Developer Intern", company="Optiver") == "quantresearch"
    assert roletype.classify("Quantitative Researcher Intern", company="Two Sigma") == "quantresearch"
    # "Quantitative Software Engineer" is engineering, and must not be swept up with the research titles.
    assert roletype.classify("Quantitative Software Engineer Intern", company="Citadel") == "quant"
    assert roletype.classify("Quantitative Trading Intern", company="Nobody Inc") == "quantresearch"
