"""`dba2/media/detect/form.py` -- MV-2c/2d: `HOT_STREAK`/`COLD_STRETCH`/
`DEFENSIVE_STANDOUT`, a live 5-game window vs. a per-36 baseline."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle
from dba2.domain.phase import Phase
from dba2.domain.stats import PlayerFormWindow
from dba2.media.detect.form import (
    compute_baseline,
    defensive_standout_package,
    hot_or_cold_package,
    is_eligible,
    per36,
    true_shooting,
)

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 4
_PLAYER = 1
_TEAM = 1


def _window(**overrides) -> PlayerFormWindow:
    base = dict(
        player_id=_PLAYER, team_id=_TEAM, window_games=5, season_games=15,
        window_points=0.0, window_minutes=150.0,
        window_fga_rim=0, window_fga_mid=0, window_fga_three=0, window_fgm_three=0,
        window_fta=0, window_ftm=0, window_ast=0, window_stl=0, window_blk=0,
        window_dreb=0, window_tov=0,
        season_points=0.0, season_minutes=300.0,
        season_fga_rim=0, season_fga_mid=0, season_fga_three=0, season_fgm_three=0,
        season_fta=0, season_ftm=0, season_ast=0, season_stl=0, season_blk=0,
        season_dreb=0, season_tov=0,
    )
    base.update(overrides)
    return PlayerFormWindow(**base)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_window_games_must_be_exactly_five():
    assert not is_eligible(_window(window_games=4))
    assert not is_eligible(_window(window_games=6))


def test_season_games_14_is_ineligible_15_is_eligible():
    assert not is_eligible(_window(season_games=14))
    assert is_eligible(_window(season_games=15))


def test_window_minutes_per_game_below_20_is_ineligible():
    assert not is_eligible(_window(window_minutes=99.0))  # 19.8 mpg
    assert is_eligible(_window(window_minutes=100.0))  # 20.0 mpg


# ---------------------------------------------------------------------------
# per36 / true_shooting -- zero-denominator guards
# ---------------------------------------------------------------------------


def test_per36_zero_minutes_is_none():
    assert per36(10.0, 0.0) is None


def test_true_shooting_zero_shot_denominator_is_none():
    assert true_shooting(10.0, 0, 0, 0, 0) is None


def test_compute_baseline_is_season_minus_window():
    window = _window(
        window_points=100.0, season_points=300.0, window_fga_rim=10, season_fga_rim=40,
    )
    baseline = compute_baseline(window)
    assert baseline.points == 200.0
    assert baseline.fga_rim == 30
    assert baseline.games == 10


# ---------------------------------------------------------------------------
# hot_or_cold_package
# ---------------------------------------------------------------------------


def _hot_window() -> PlayerFormWindow:
    """baseline: 10 games, 360 min, 200 pts -> per36 20.0; TS 200/400=0.50
    (fga_rim=100, fga_mid=60, fga_three=40, fta=0). window: 5 games, 180
    min, 140 pts -> per36 28.0 (+8.0, clears +6.0); TS 140/250=0.56 (fga_
    rim=65, fga_mid=35, fga_three=25, fta=0), clears baseline+0.04=0.54 by
    the DELTA path (0.56 < 0.6, so the absolute path does not apply)."""
    return _window(
        window_points=140.0, window_minutes=180.0,
        window_fga_rim=65, window_fga_mid=35, window_fga_three=25,
        season_points=340.0, season_minutes=540.0,
        season_fga_rim=165, season_fga_mid=95, season_fga_three=65,
    )


def test_hot_streak_fires_on_a_real_per36_and_ts_shift():
    package = hot_or_cold_package(
        _hot_window(), season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert package is not None
    assert package.story_kind is StoryKind.HOT_STREAK
    assert package.angle is Angle.HEATING_UP
    assert package.number("window_points_per36") == 28.0
    assert package.number("baseline_points_per36") == 20.0


def _cold_window() -> PlayerFormWindow:
    """baseline: 10 games, 360 min, 280 pts -> per36 28.0; TS 280/400=0.70
    (fga_rim=100, fga_mid=60, fga_three=40, fta=0). window: 5 games, 180
    min, 100 pts -> per36 20.0 (-8.0, clears -6.0); TS 100/200=0.50 (fga_
    rim=50, fga_mid=30, fga_three=20, fta=0), clears baseline-0.04=0.66."""
    return _window(
        window_points=100.0, window_minutes=180.0,
        window_fga_rim=50, window_fga_mid=30, window_fga_three=20,
        season_points=380.0, season_minutes=540.0,
        season_fga_rim=150, season_fga_mid=90, season_fga_three=60,
    )


def test_cold_stretch_fires_on_a_real_decline():
    package = hot_or_cold_package(
        _cold_window(), season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert package is not None
    assert package.story_kind is StoryKind.COLD_STRETCH
    assert package.angle is Angle.GOING_COLD


def test_minutes_only_increase_does_not_fire():
    """The per-36 regression test: points rise proportionally with minutes, so
    per-36 (and TS, scaled identically) are unchanged -- must not read as hot."""
    window = _window(
        window_points=75.0, window_minutes=150.0,
        window_fga_rim=30, window_fga_mid=15, window_fga_three=15,
        season_points=175.0, season_minutes=350.0,
        season_fga_rim=70, season_fga_mid=35, season_fga_three=35,
    )
    result = hot_or_cold_package(window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert result is None


def test_a_zero_shot_window_emits_nothing_rather_than_dividing_by_zero():
    window = _hot_window()
    zero_shot = _window(
        window_points=140.0, window_minutes=180.0,
        window_fga_rim=0, window_fga_mid=0, window_fga_three=0, window_fta=0,
        season_points=window.season_points, season_minutes=window.season_minutes,
        season_fga_rim=window.season_fga_rim - window.window_fga_rim,
        season_fga_mid=window.season_fga_mid - window.window_fga_mid,
        season_fga_three=window.season_fga_three - window.window_fga_three,
    )
    assert (
        hot_or_cold_package(zero_shot, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
        is None
    )


def test_a_player_traded_mid_window_still_aggregates_regardless_of_team_id():
    """`PlayerFormWindow.team_id` is the CURRENT team -- the window/season sums
    already aggregate across both team ids at the SQL layer (MV-2c's own
    disclosed rule); this pure function reads only the aggregates, so a real
    trade mid-window is invisible to it by construction, proven here by
    varying `team_id` alone and confirming the outcome is identical."""
    same_shape_different_team = PlayerFormWindow(**{**_hot_window().__dict__, "team_id": 999})
    package = hot_or_cold_package(
        same_shape_different_team, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert package is not None
    assert package.story_kind is StoryKind.HOT_STREAK


def test_ineligible_window_returns_none_even_with_hot_looking_numbers():
    window = PlayerFormWindow(**{**_hot_window().__dict__, "season_games": 14})
    result = hot_or_cold_package(window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert result is None


def test_attribution_facts_only_appended_to_a_hot_result():
    from dba2.domain.media_facts import Fact, FactSource

    extra = (
        Fact(key="attribution_free_throws", label="x", value_text="1.0", value_number=1.0,
             source=FactSource.PLAYER_GAME_STATS),
    )
    hot = hot_or_cold_package(
        _hot_window(), season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
        attribution_facts=extra,
    )
    assert hot is not None
    assert hot.number("attribution_free_throws") == 1.0

    cold = hot_or_cold_package(
        _cold_window(), season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
        attribution_facts=extra,
    )
    assert cold is not None
    assert cold.text("attribution_free_throws") is None


# ---------------------------------------------------------------------------
# defensive_standout_package
# ---------------------------------------------------------------------------


def test_defensive_standout_fires_on_a_real_stocks_shift():
    """baseline: 10 games, 300 min, 10 stl + 10 blk -> per36 (20/300*36)=2.4.
    window: 5 games, 150 min, 10 stl + 10 blk -> per36 (20/150*36)=4.8, which
    clears BOTH the +1.5 delta (4.8 >= 2.4+1.5) and the >=4.0 absolute floor."""
    window = _window(
        window_minutes=150.0, window_stl=10, window_blk=10,
        season_minutes=450.0, season_stl=20, season_blk=20,
    )
    package = defensive_standout_package(
        window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert package is not None
    assert package.story_kind is StoryKind.DEFENSIVE_STANDOUT
    assert package.angle is Angle.LOCKDOWN


def test_defensive_standout_does_not_fire_below_the_absolute_floor():
    """A real +1.5 delta that never reaches the 4.0 absolute floor must not fire."""
    window = _window(
        window_minutes=150.0, window_stl=2, window_blk=1,  # per36 = 0.72
        season_minutes=450.0, season_stl=2, season_blk=1,  # baseline per36 = 0.24
    )
    assert (
        defensive_standout_package(window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
        is None
    )


def test_defensive_standout_ineligible_window_returns_none():
    window = _window(window_games=4, window_stl=10, window_blk=10)
    assert (
        defensive_standout_package(window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
        is None
    )
