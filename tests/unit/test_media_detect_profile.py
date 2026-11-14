"""`dba2/media/detect/profile.py` -- MV-2h: `TEAM_PROFILE`, the composer."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.media.detect.profile import (
    CARRY_OVER_MULTIPLIER,
    HUMAN_MANAGED_MULTIPLIER,
    ProfileCandidate,
    ProfileTerms,
    profile_interest,
    profile_priority,
    select_teams_to_profile,
    standings_movement_term,
    team_profile_package,
)

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 2
_TEAM = 1


def _terms(**overrides) -> ProfileTerms:
    base = dict(
        roster_extremity=0.0, streak_length=0, standings_movement=None,
        trades_this_season=0, players_not_content=0, rivalry_heat=False,
    )
    base.update(overrides)
    return ProfileTerms(**base)


# ---------------------------------------------------------------------------
# profile_interest -- the six-term weighted sum
# ---------------------------------------------------------------------------


def test_weights_sum_to_exactly_one():
    from dba2.media.detect.profile import (
        _MOTIVATION_DRAMA_WEIGHT,
        _RIVALRY_HEAT_WEIGHT,
        _ROSTER_EXTREMITY_WEIGHT,
        _STANDINGS_MOVEMENT_WEIGHT,
        _STREAK_WEIGHT,
        _TRADE_ACTIVITY_WEIGHT,
    )

    total = (
        _ROSTER_EXTREMITY_WEIGHT + _STREAK_WEIGHT + _STANDINGS_MOVEMENT_WEIGHT
        + _TRADE_ACTIVITY_WEIGHT + _MOTIVATION_DRAMA_WEIGHT + _RIVALRY_HEAT_WEIGHT
    )
    assert total == 1.00


def test_all_zero_terms_reads_zero():
    assert profile_interest(_terms()) == 0.0


def test_a_maxed_out_candidate_reads_exactly_one():
    package = _terms(
        roster_extremity=1.0, streak_length=20, standings_movement=1.0,
        trades_this_season=10, players_not_content=10, rivalry_heat=True,
    )
    assert profile_interest(package) == 1.0


def test_standings_movement_none_reads_as_zero_not_a_fabricated_reading():
    with_movement = profile_interest(_terms(standings_movement=1.0))
    without_movement = profile_interest(_terms(standings_movement=None))
    assert without_movement == 0.0
    assert with_movement > without_movement


def test_streak_term_scales_toward_the_anchor():
    small = profile_interest(_terms(streak_length=2))
    big = profile_interest(_terms(streak_length=8))
    assert big > small


# ---------------------------------------------------------------------------
# standings_movement_term
# ---------------------------------------------------------------------------


def test_standings_movement_needs_both_endpoints_to_have_played():
    assert standings_movement_term(
        position_now=5, position_at_last_pulse=10, games_played_now=0,
        games_played_at_last_pulse=5,
    ) is None
    assert standings_movement_term(
        position_now=5, position_at_last_pulse=10, games_played_now=5,
        games_played_at_last_pulse=0,
    ) is None


def test_standings_movement_real_reading():
    value = standings_movement_term(
        position_now=5, position_at_last_pulse=10, games_played_now=5,
        games_played_at_last_pulse=5,
    )
    assert value == 5 / 15


# ---------------------------------------------------------------------------
# profile_priority -- the two multipliers, and the [0, 1.875] range
# ---------------------------------------------------------------------------


def test_profile_priority_no_multipliers_equals_interest():
    assert profile_priority(0.5, not_profiled_last_season=False, human_managed=False) == 0.5


def test_profile_priority_carry_over_multiplier():
    assert profile_priority(
        0.5, not_profiled_last_season=True, human_managed=False
    ) == 0.5 * CARRY_OVER_MULTIPLIER


def test_profile_priority_both_multipliers_reach_the_real_ceiling():
    assert profile_priority(
        1.0, not_profiled_last_season=True, human_managed=True
    ) == CARRY_OVER_MULTIPLIER * HUMAN_MANAGED_MULTIPLIER


# ---------------------------------------------------------------------------
# select_teams_to_profile -- ranking, tiebreak, quota, empty pool
# ---------------------------------------------------------------------------


def test_selects_highest_priority_first():
    candidates = [
        ProfileCandidate(team_id=1, priority=0.3),
        ProfileCandidate(team_id=2, priority=0.9),
    ]
    assert select_teams_to_profile(candidates, quota=1) == (2,)


def test_ties_break_by_lower_team_id_deterministically():
    candidates = [
        ProfileCandidate(team_id=5, priority=0.5),
        ProfileCandidate(team_id=2, priority=0.5),
    ]
    assert select_teams_to_profile(candidates, quota=1) == (2,)
    # Re-run to confirm no RNG involvement -- same input, same output every time.
    assert select_teams_to_profile(candidates, quota=1) == (2,)


def test_fewer_eligible_than_quota_returns_only_what_exists_no_backfill():
    candidates = [
        ProfileCandidate(team_id=1, priority=0.5),
        ProfileCandidate(team_id=2, priority=0.3),
    ]
    assert select_teams_to_profile(candidates, quota=3) == (1, 2)


def test_empty_pool_returns_empty_no_exception():
    assert select_teams_to_profile((), quota=3) == ()


def test_two_season_coverage_property():
    """MV-2h.3's own pinned guarantee: simulate two seasons of quota draws over 30
    teams with the carry-over multiplier, and confirm no team_id goes unprofiled
    both seasons."""
    team_ids = list(range(1, 31))
    quota = 3
    profiled_season_1: set[int] = set()
    remaining = list(team_ids)
    while remaining:
        candidates = [ProfileCandidate(team_id=t, priority=1.0) for t in remaining]
        picked = select_teams_to_profile(candidates, quota=quota)
        profiled_season_1.update(picked)
        remaining = [t for t in remaining if t not in picked]

    unprofiled_after_season_1 = set(team_ids) - profiled_season_1
    # Season 2: everyone gets the SAME base interest, but the carry-over
    # multiplier boosts anyone unprofiled in season 1.
    candidates_season_2 = [
        ProfileCandidate(
            team_id=t,
            priority=profile_priority(
                0.5, not_profiled_last_season=(t in unprofiled_after_season_1),
                human_managed=False,
            ),
        )
        for t in team_ids
    ]
    profiled_season_2: set[int] = set()
    remaining = team_ids
    while remaining:
        pool = [c for c in candidates_season_2 if c.team_id in remaining]
        picked = select_teams_to_profile(pool, quota=quota)
        if not picked:
            break
        profiled_season_2.update(picked)
        remaining = [t for t in remaining if t not in picked]

    never_profiled = (set(team_ids) - profiled_season_1) - profiled_season_2
    assert never_profiled == set()


# ---------------------------------------------------------------------------
# team_profile_package -- the minimum bar, and "absent is neutral"
# ---------------------------------------------------------------------------


def _minimal_kwargs(**overrides):
    base = dict(
        wins=10, losses=5, standings_position=3, streak_length=2,
        standings_position_delta=None, mean_age_top8=None,
        biggest_need_position=None, biggest_need_value=None,
        biggest_glut_position=None, biggest_glut_value=None,
        offensive_scheme=None, defensive_scheme=None, three_point_bias=None,
        three_share=None, pace_target=None, avg_possessions=None,
        set_by_a_person=None, scheme_tension=None, timeline=None,
        gm_philosophy=None, trades_in_count=None, trades_out_count=None,
        unrest_player_count=None, next_game_date=None, next_game_is_rivalry=None,
        prior_wins=None, prior_losses=None, prior_standings_position=None,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


def test_fewer_than_six_facts_returns_none():
    """Only the four unconditional pulse facts (wins/losses/standings/streak) --
    below the six-fact bar."""
    assert team_profile_package(_TEAM, **_minimal_kwargs()) is None


def test_six_facts_clears_the_bar():
    package = team_profile_package(
        _TEAM,
        **_minimal_kwargs(timeline="rebuild", gm_philosophy="star_maxer"),
    )
    assert package is not None
    assert package.story_kind is StoryKind.TEAM_PROFILE


def test_boolean_facts_render_as_yes_no_text_not_literal_digits():
    """Critic round on commits `ea84c5f`..`665526d`, bug B2: `value_text` used
    to be the literal `"1"`/`"0"`, which `_build_fact_box` published verbatim
    with no human-readable meaning at all."""
    package = team_profile_package(
        _TEAM,
        **_minimal_kwargs(
            timeline="rebuild", gm_philosophy="star_maxer",
            set_by_a_person=True, next_game_is_rivalry=False,
        ),
    )
    assert package is not None
    assert package.number("set_by_a_person") == 1
    assert package.text("set_by_a_person") == "Yes"
    assert package.number("next_game_is_rivalry") == 0
    assert package.text("next_game_is_rivalry") == "No"


def test_no_prior_season_emits_no_last_year_facts_not_a_fabricated_zero_zero():
    package = team_profile_package(
        _TEAM,
        **_minimal_kwargs(
            timeline="rebuild", gm_philosophy="star_maxer",
            prior_wins=None, prior_losses=None, prior_standings_position=None,
        ),
    )
    assert package is not None
    assert package.text("prior_wins") is None
    assert package.text("prior_losses") is None


def test_a_real_prior_season_is_carried():
    package = team_profile_package(
        _TEAM,
        **_minimal_kwargs(
            timeline="rebuild", gm_philosophy="star_maxer",
            prior_wins=20, prior_losses=15, prior_standings_position=12,
        ),
    )
    assert package is not None
    assert package.number("prior_wins") == 20
    assert package.number("prior_standings_position") == 12


def test_a_preseason_team_with_zero_games_still_clears_the_bar_with_strategy_and_roster_facts():
    """No record/streak movement, but strategy + roster + timeline facts alone
    can clear six."""
    package = team_profile_package(
        _TEAM,
        **_minimal_kwargs(
            wins=0, losses=0, standings_position=15, streak_length=0,
            offensive_scheme="motion", defensive_scheme="drop",
            timeline="retool", gm_philosophy="egalitarian",
        ),
    )
    assert package is not None
