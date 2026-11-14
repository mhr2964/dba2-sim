"""`dba2/media/detect/attribution.py` -- MV-2c's "why," MV-2d's defensive
analogue. Each attribution needs BOTH a real shot-profile shift AND a real
rating at or above the bar; neither alone emits anything."""

from __future__ import annotations

from dba2.domain.stats import PlayerFormWindow
from dba2.media.detect.attribution import explain_defense, explain_form
from dba2.media.detect.form import compute_baseline

_PLAYER = 1
_TEAM = 1
_LOW_RATING = 60
_HIGH_RATING = 80


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
# Three-point volume -- shot-profile shift AND rating gate, "never invent a reason."
# ---------------------------------------------------------------------------


def _three_point_shift_window() -> PlayerFormWindow:
    """window 3PA/game = 4 (20/5), season(=baseline+window) so baseline 3PA/game
    = 2 (10 games -> 20 attempts) -- window is exactly 2x baseline (>= 1.25x).
    3P% window = 8/20 = 0.40 >= baseline 6/20 = 0.30."""
    return _window(
        window_fga_three=20, window_fgm_three=8,
        season_fga_three=40, season_fgm_three=14,
    )


def test_shot_profile_shift_with_a_low_rating_emits_nothing():
    window = _three_point_shift_window()
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=_LOW_RATING, off_dribble_3=_LOW_RATING,
        finishing_rim=_LOW_RATING, first_step=_LOW_RATING, draw_foul=_LOW_RATING,
        passing_vision=_LOW_RATING,
    )
    assert facts == ()


def test_high_rating_with_no_shot_profile_shift_emits_nothing():
    window = _window()  # all zero -- no shift possible
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=_HIGH_RATING, off_dribble_3=_HIGH_RATING,
        finishing_rim=_HIGH_RATING, first_step=_HIGH_RATING, draw_foul=_HIGH_RATING,
        passing_vision=_HIGH_RATING,
    )
    assert facts == ()


def test_both_shift_and_rating_emits_exactly_one_fact_with_both_numbers():
    window = _three_point_shift_window()
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=_HIGH_RATING, off_dribble_3=_LOW_RATING,
        finishing_rim=_LOW_RATING, first_step=_LOW_RATING, draw_foul=_LOW_RATING,
        passing_vision=_LOW_RATING,
    )
    assert len(facts) == 1
    fact = facts[0]
    assert fact.key == "attribution_three_point_volume"
    assert fact.value_number == 4.0  # window 3PA/game


# ---------------------------------------------------------------------------
# The other three MV-2c gates, one qualifying case each
# ---------------------------------------------------------------------------


def test_rim_pressure_gate():
    window = _window(
        window_fga_rim=40, window_fga_mid=10, window_fga_three=0,
        season_fga_rim=60, season_fga_mid=40, season_fga_three=0,
    )
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=0, off_dribble_3=0, finishing_rim=_HIGH_RATING,
        first_step=0, draw_foul=0, passing_vision=0,
    )
    assert any(f.key == "attribution_rim_pressure" for f in facts)


def test_free_throws_gate():
    window = _window(window_fta=20, window_games=5, season_fta=25)
    baseline = compute_baseline(window)  # baseline_fta = 5 / 10 games = 0.5/g
    facts = explain_form(
        window, baseline, catch_shoot_3=0, off_dribble_3=0, finishing_rim=0,
        first_step=0, draw_foul=_HIGH_RATING, passing_vision=0,
    )
    assert any(f.key == "attribution_free_throws" for f in facts)


def test_playmaking_gate():
    window = _window(
        window_ast=20, window_tov=5, window_minutes=180.0,
        season_ast=25, season_tov=15, season_minutes=540.0,
    )
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=0, off_dribble_3=0, finishing_rim=0,
        first_step=0, draw_foul=0, passing_vision=_HIGH_RATING,
    )
    assert any(f.key == "attribution_playmaking" for f in facts)


def test_playmaking_gate_rejects_a_higher_turnover_rate():
    """`window_tov36` not higher than baseline is part of the gate -- more
    assists AND more turnovers is not "playmaking," MV-2c's own condition."""
    window = _window(
        window_ast=20, window_tov=20, window_minutes=180.0,
        season_ast=25, season_tov=22, season_minutes=540.0,
    )
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=0, off_dribble_3=0, finishing_rim=0,
        first_step=0, draw_foul=0, passing_vision=_HIGH_RATING,
    )
    assert not any(f.key == "attribution_playmaking" for f in facts)


def test_multiple_attributions_can_qualify_at_once():
    window = _window(
        window_fga_three=20, window_fgm_three=8, window_fta=20,
        season_fga_three=40, season_fgm_three=14, season_fta=25,
    )
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=_HIGH_RATING, off_dribble_3=0, finishing_rim=0,
        first_step=0, draw_foul=_HIGH_RATING, passing_vision=0,
    )
    keys = {f.key for f in facts}
    assert keys == {"attribution_three_point_volume", "attribution_free_throws"}


def test_zero_games_or_baseline_games_returns_no_facts():
    window = _window(window_games=0, season_games=0)
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=99, off_dribble_3=99, finishing_rim=99,
        first_step=99, draw_foul=99, passing_vision=99,
    )
    assert facts == ()


# ---------------------------------------------------------------------------
# explain_defense (MV-2d) -- rating-gated only, no shot-profile pair
# ---------------------------------------------------------------------------


def test_steals_led_gate():
    window = _window(window_stl=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=_HIGH_RATING, rim_protection=0, defensive_rebounding=0
    )
    assert len(facts) == 1
    assert facts[0].key == "attribution_on_ball_defense"


def test_blocks_led_gate():
    window = _window(window_blk=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=0, rim_protection=_HIGH_RATING, defensive_rebounding=0
    )
    assert len(facts) == 1
    assert facts[0].key == "attribution_rim_protection"


def test_rebounding_led_gate():
    window = _window(window_dreb=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=0, rim_protection=0, defensive_rebounding=_HIGH_RATING
    )
    assert len(facts) == 1
    assert facts[0].key == "attribution_defensive_rebounding"


def test_all_low_ratings_emits_nothing():
    window = _window(window_stl=10, window_blk=10, window_dreb=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=_LOW_RATING, rim_protection=_LOW_RATING,
        defensive_rebounding=_LOW_RATING,
    )
    assert facts == ()


def test_zero_minutes_returns_no_facts():
    window = _window(window_minutes=0.0, window_stl=10)
    facts = explain_defense(
        window, on_ball_defense=_HIGH_RATING, rim_protection=_HIGH_RATING,
        defensive_rebounding=_HIGH_RATING,
    )
    assert facts == ()


# ---------------------------------------------------------------------------
# BLOCKING 3 (critic round on commits b288b8e..99db9f4): `explain_defense`'s
# gate is 78 (MV-2d, `:1077-1079`), not 75 (`explain_form`'s own MV-2c bar).
# `_HIGH_RATING = 80`/`_LOW_RATING = 60` above cannot distinguish the two --
# both clear 75 AND 78, or clear neither. A rating in the 75-77 band is the
# only input that tells the two bars apart, which is exactly what proves
# this fix changed real behavior rather than just renaming a constant.
# ---------------------------------------------------------------------------

_BOUNDARY_BAND_RATING = 76  # >= 75 (explain_form's bar) but < 78 (explain_defense's).


def test_defense_gate_does_not_fire_in_the_75_to_77_band():
    """Before this fix, `_RATING_BAR = 75` was shared by both functions, so
    a `76` would have (incorrectly) cleared `explain_defense`'s gate too.
    This is the reproduction: it must NOT fire at 76, only at >= 78."""
    window = _window(window_stl=10, window_blk=10, window_dreb=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=_BOUNDARY_BAND_RATING, rim_protection=_BOUNDARY_BAND_RATING,
        defensive_rebounding=_BOUNDARY_BAND_RATING,
    )
    assert facts == ()


def test_defense_gate_fires_at_exactly_78():
    window = _window(window_stl=10, window_blk=10, window_dreb=10, window_minutes=150.0)
    facts = explain_defense(
        window, on_ball_defense=78, rim_protection=78, defensive_rebounding=78,
    )
    keys = {f.key for f in facts}
    assert keys == {
        "attribution_on_ball_defense", "attribution_rim_protection",
        "attribution_defensive_rebounding",
    }


def test_form_gate_still_fires_at_75_unaffected_by_the_defense_bar_split():
    """`explain_form`'s own bar is genuinely 75 (MV-2c), not 78 -- this pins
    that splitting the two constants did not accidentally raise MV-2c's own
    bar to match MV-2d's."""
    window = _three_point_shift_window()
    baseline = compute_baseline(window)
    facts = explain_form(
        window, baseline, catch_shoot_3=75, off_dribble_3=0, finishing_rim=0,
        first_step=0, draw_foul=0, passing_vision=0,
    )
    assert any(f.key == "attribution_three_point_volume" for f in facts)
