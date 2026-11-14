"""`dba2/media/detect/scheme.py` -- MV-2g: `SCHEME_IDENTITY`/`SCHEME_TENSION`
(one kind, two angles) and `TRADE_MARKET_WATCH`."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.team import DefensiveScheme, OffensiveScheme
from dba2.media.detect.scheme import scheme_identity_package, trade_market_watch_package

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 5
_TEAM = 1
_PLAYER = 1
_LEAGUE_SIZE = 30


def _scheme_kwargs(**overrides):
    base = dict(
        offensive_scheme=OffensiveScheme.MOTION, defensive_scheme=DefensiveScheme.DROP,
        pace_target=50, pace_rank=15, avg_possessions=98.0, avg_possessions_rank=15,
        three_point_bias=50, three_share=0.35, three_share_rank=15, league_size=_LEAGUE_SIZE,
        set_by_a_person=True, games=20, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


def test_no_tension_reads_scheme_identity_angle():
    package = scheme_identity_package(_TEAM, **_scheme_kwargs())
    assert package.story_kind is StoryKind.SCHEME_IDENTITY
    assert package.angle is Angle.SCHEME_IDENTITY
    assert package.text("tension") == "none"


def test_three_point_bias_tension_fires():
    """Declared bias >= 70 but measured share ranks in the bottom third (rank
    > 2*30/3 = 20)."""
    package = scheme_identity_package(
        _TEAM, **_scheme_kwargs(three_point_bias=75, three_share_rank=25)
    )
    assert package.angle is Angle.SCHEME_TENSION
    assert package.text("tension") == "three_point_bias"


def test_three_point_bias_alone_does_not_tension_without_a_bottom_third_rank():
    package = scheme_identity_package(
        _TEAM, **_scheme_kwargs(three_point_bias=75, three_share_rank=15)
    )
    assert package.angle is Angle.SCHEME_IDENTITY


def test_pace_tension_fires():
    package = scheme_identity_package(
        _TEAM, **_scheme_kwargs(pace_target=70, avg_possessions_rank=25)
    )
    assert package.angle is Angle.SCHEME_TENSION
    assert package.text("tension") == "pace"


def test_a_team_with_games_played_produces_a_package_with_no_tension_present():
    package = scheme_identity_package(_TEAM, **_scheme_kwargs())
    assert package is not None
    assert package.number("pace_target") == 50


def test_set_by_a_person_is_a_real_boolean_reading():
    auto = scheme_identity_package(_TEAM, **_scheme_kwargs(set_by_a_person=False))
    assert auto.number("set_by_a_person") == 0
    assert auto.text("set_by_a_person") == "No"  # bug B2: not the literal "0"
    human = scheme_identity_package(_TEAM, **_scheme_kwargs(set_by_a_person=True))
    assert human.number("set_by_a_person") == 1
    assert human.text("set_by_a_person") == "Yes"  # bug B2: not the literal "1"


# ---------------------------------------------------------------------------
# BLOCKING 1 (critic round on commits b288b8e..99db9f4): a team with no
# `team_game_stats` rows yet (PRESEASON, zero games played) must emit
# nothing rather than a measured-looking fact built on no real measurement.
# Before this fix, `games` did not exist as a parameter at all, so this gate
# was unreachable -- reproduced here against the real function signature.
# ---------------------------------------------------------------------------


def test_zero_games_played_emits_no_package():
    assert scheme_identity_package(_TEAM, **_scheme_kwargs(games=0)) is None


def test_negative_games_also_emits_no_package():
    """Defensive: a caller bug producing a negative count must not be read
    as "some games," matching this codebase's `<= 0` convention elsewhere
    (`dba2/media/detect/attribution.py`'s own `window_games <= 0` gate)."""
    assert scheme_identity_package(_TEAM, **_scheme_kwargs(games=-1)) is None


def test_one_game_played_is_sufficient_to_produce_a_package():
    package = scheme_identity_package(_TEAM, **_scheme_kwargs(games=1))
    assert package is not None


def test_league_size_is_carried_as_a_fact():
    package = scheme_identity_package(_TEAM, **_scheme_kwargs())
    assert package is not None
    assert package.number("league_size") == _LEAGUE_SIZE


# ---------------------------------------------------------------------------
# trade_market_watch_package
# ---------------------------------------------------------------------------


def _watch_kwargs(**overrides):
    base = dict(
        team_glut_position="PG", team_glut_value=0.65, league_biggest_need_value=0.70,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


def test_trade_requested_fires():
    package = trade_market_watch_package(
        _PLAYER, _TEAM, request_state=RequestState.TRADE_REQUESTED, **_watch_kwargs()
    )
    assert package is not None
    assert package.story_kind is StoryKind.TRADE_MARKET_WATCH


def test_unsettled_fires():
    package = trade_market_watch_package(
        _PLAYER, _TEAM, request_state=RequestState.UNSETTLED, **_watch_kwargs()
    )
    assert package is not None


def test_content_and_holdout_do_not_fire():
    for state in (RequestState.CONTENT, RequestState.HOLDOUT, RequestState.EXTENSION_DEMANDED):
        assert (
            trade_market_watch_package(_PLAYER, _TEAM, request_state=state, **_watch_kwargs())
            is None
        )


def test_facts_carry_the_teams_glut_and_the_leagues_need():
    package = trade_market_watch_package(
        _PLAYER, _TEAM, request_state=RequestState.UNSETTLED, **_watch_kwargs()
    )
    assert package is not None
    assert package.text("team_glut_position") == "PG"
    assert package.number("team_glut_value") == 0.65
    assert package.number("league_biggest_need_value") == 0.70
