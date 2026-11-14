"""`dba2/media/write/budget.py` -- MV-4's tier selection and per-tier
rendering budgets."""

from __future__ import annotations

import pytest

from dba2.domain.media import Register, StoryKind
from dba2.domain.media_writer import FormatTier
from dba2.media.write.budget import (
    ARTICLE_MIN_FACTS,
    ARTICLE_SALIENCE_THRESHOLD,
    HEADLINE_STYLE_BY_REGISTER,
    STORY_KIND_HEADLINE_LABEL,
    TEAM_PROFILE_MIN_FACTS,
    apply_headline_style,
    budget_for,
    clamp_text,
    fact_box_field_count,
    headline_label_for,
    select_tier,
)
from dba2.presentation import panel

# ---------------------------------------------------------------------------
# select_tier -- MV-4's deterministic rule
# ---------------------------------------------------------------------------


def test_below_threshold_and_below_fact_count_is_blurb():
    tier = select_tier(0.10, 1, story_kind=StoryKind.STREAK)
    assert tier is FormatTier.BLURB


def test_clearing_both_the_score_and_fact_count_bars_is_article():
    tier = select_tier(ARTICLE_SALIENCE_THRESHOLD, ARTICLE_MIN_FACTS, story_kind=StoryKind.STREAK)
    assert tier is FormatTier.ARTICLE


def test_high_score_but_too_few_facts_is_still_blurb():
    tier = select_tier(0.99, ARTICLE_MIN_FACTS - 1, story_kind=StoryKind.STREAK)
    assert tier is FormatTier.BLURB


def test_enough_facts_but_score_below_threshold_is_still_blurb():
    tier = select_tier(ARTICLE_SALIENCE_THRESHOLD - 0.01, 10, story_kind=StoryKind.STREAK)
    assert tier is FormatTier.BLURB


def test_team_profile_below_six_facts_is_not_published():
    assert select_tier(0.99, TEAM_PROFILE_MIN_FACTS - 1, story_kind=StoryKind.TEAM_PROFILE) is None


def test_team_profile_at_six_facts_is_article_regardless_of_score():
    """MV-4's own amendment: a profile's score never gates its tier -- it bypasses
    the salience floor entirely (MV-3h.1), so thresholding it at 0.62 would apply a
    bar it was deliberately exempted from."""
    tier = select_tier(0.0, TEAM_PROFILE_MIN_FACTS, story_kind=StoryKind.TEAM_PROFILE)
    assert tier is FormatTier.ARTICLE


def test_team_profile_never_becomes_a_blurb():
    for score in (0.0, 0.5, 1.0):
        for facts in range(0, 12):
            tier = select_tier(score, facts, story_kind=StoryKind.TEAM_PROFILE)
            assert tier in (None, FormatTier.ARTICLE)


# ---------------------------------------------------------------------------
# fact_box_field_count / budget_for
# ---------------------------------------------------------------------------


def test_team_profile_gets_five_fields_everyone_else_gets_three():
    assert fact_box_field_count(StoryKind.TEAM_PROFILE) == 5
    assert fact_box_field_count(StoryKind.STREAK) == 3
    assert fact_box_field_count(StoryKind.HOT_STREAK) == 3


def test_blurb_budget_always_has_zero_fields_even_for_team_profile():
    budget = budget_for(FormatTier.BLURB, StoryKind.TEAM_PROFILE)
    assert budget.field_count == 0


def test_article_budget_field_count_varies_only_by_kind():
    default_budget = budget_for(FormatTier.ARTICLE, StoryKind.STREAK)
    profile_budget = budget_for(FormatTier.ARTICLE, StoryKind.TEAM_PROFILE)
    assert default_budget.field_count == 3
    assert profile_budget.field_count == 5
    assert default_budget.title_max == profile_budget.title_max


def test_every_budget_constant_sits_inside_panels_own_real_limits():
    for tier in FormatTier:
        for kind in StoryKind:
            budget = budget_for(tier, kind)
            assert budget.title_max <= panel.MAX_TITLE_CHARS
            assert budget.description_max <= panel.MAX_DESCRIPTION_CHARS
            assert budget.author_max <= panel.MAX_AUTHOR_NAME_CHARS
            assert budget.footer_max <= panel.MAX_FOOTER_TEXT_CHARS
            assert budget.field_count <= panel.MAX_FIELDS
            assert budget.field_name_max <= panel.MAX_FIELD_NAME_CHARS
            assert budget.field_value_max <= panel.MAX_FIELD_VALUE_CHARS
            total = (
                budget.title_max + budget.description_max + budget.author_max
                + budget.footer_max
                + budget.field_count * (budget.field_name_max + budget.field_value_max)
            )
            assert total <= panel.MAX_TOTAL_CHARS


# ---------------------------------------------------------------------------
# clamp_text
# ---------------------------------------------------------------------------


def test_clamp_text_raises_on_empty_or_whitespace_only():
    with pytest.raises(ValueError, match="empty"):
        clamp_text("", 80, field_name="headline")
    with pytest.raises(ValueError, match="empty"):
        clamp_text("   ", 80, field_name="headline")


def test_clamp_text_passes_through_a_short_value_unchanged():
    assert clamp_text("Trail Blazers win big", 80, field_name="headline") == "Trail Blazers win big"


def test_clamp_text_truncates_long_values_at_a_word_boundary_with_an_ellipsis():
    long_text = "word " * 40
    clamped = clamp_text(long_text, 30, field_name="description")
    assert len(clamped) <= 30
    assert clamped.endswith("…")
    assert not clamped[:-1].endswith(" ")


def test_clamp_text_never_exceeds_max_chars_even_for_a_single_giant_word():
    clamped = clamp_text("x" * 200, 10, field_name="headline")
    assert len(clamped) <= 10


# ---------------------------------------------------------------------------
# HEADLINE_STYLE_BY_REGISTER / apply_headline_style
# ---------------------------------------------------------------------------


def test_every_register_has_a_headline_style():
    assert set(HEADLINE_STYLE_BY_REGISTER) == set(Register)


def test_only_inflammatory_allows_exclamation_points():
    for register, style in HEADLINE_STYLE_BY_REGISTER.items():
        if register is Register.INFLAMMATORY:
            assert style.allow_exclamation
        else:
            assert not style.allow_exclamation


def test_apply_headline_style_replaces_exclamation_for_non_inflammatory_registers():
    result = apply_headline_style("This is indefensible!", Register.REPORTORIAL)
    assert "!" not in result
    assert result == "This is indefensible."


def test_apply_headline_style_keeps_exclamation_for_inflammatory():
    result = apply_headline_style("This is indefensible!", Register.INFLAMMATORY)
    assert result == "This is indefensible!"


def test_apply_headline_style_title_cases_narrative_only():
    result = apply_headline_style("a night to remember", Register.NARRATIVE)
    assert result == "A Night To Remember"


# ---------------------------------------------------------------------------
# STORY_KIND_HEADLINE_LABEL / headline_label_for -- bug B4.2 (critic round on
# commits `ea84c5f`..`665526d`): every StoryKind needs a real, hand-written
# label, never the raw enum value.
# ---------------------------------------------------------------------------


def test_every_story_kind_has_a_headline_label():
    assert set(STORY_KIND_HEADLINE_LABEL) == set(StoryKind)


def test_no_headline_label_is_the_raw_enum_value_with_underscores():
    for kind, label in STORY_KIND_HEADLINE_LABEL.items():
        assert "_" not in label, f"{kind.name}'s headline label {label!r} leaks the raw enum value"


def test_headline_label_for_reads_the_registry():
    assert headline_label_for(StoryKind.TRADE_BROKEN_PROMISE) == "broken promise"
    reportorial = apply_headline_style("a night to remember", Register.REPORTORIAL)
    assert reportorial == "a night to remember"


def test_build_fact_box_is_public_skips_identity_facts_and_keeps_package_order():
    """S8-6: promoted out of `template.py` so `AnthropicWriter` builds the
    identical fact box; behavior unchanged."""
    from dba2.media.write.budget import FACT_BOX_EXCLUDED_KEYS, build_fact_box
    from tests.unit._media_ai_fixtures import make_package

    package = make_package()
    box = build_fact_box(package, 3)
    assert box == (
        ("Assets sent", "3"),
        ("Points per game change", "12.5"),
        ("Win percentage", "61.3%"),
    )
    assert not any(k in FACT_BOX_EXCLUDED_KEYS for k in ("assets_sent", "ppg_delta"))
    assert build_fact_box(package, 0) == ()
