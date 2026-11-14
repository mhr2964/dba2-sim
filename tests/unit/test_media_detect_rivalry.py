"""`dba2/media/detect/rivalry.py` -- MV-2f: `RIVALRY_MATCHUP`."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.media.detect.rivalry import (
    RivalrySignals,
    rivalry_matchup_package,
    score,
    season_series_is_level,
)

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 3
_TEAM = 1
_OPPONENT = 2


def _signals(**overrides) -> RivalrySignals:
    base = dict(
        same_division=False, close_game_count=0, season_series_level=False,
        recent_trade=False, h2h_signal=False,
    )
    base.update(overrides)
    return RivalrySignals(**base)


def _package_kwargs(**overrides):
    base = dict(
        has_upcoming_scheduled_game=True, team_wins_this_series=2, opponent_wins_this_series=1,
        closest_margin=3, team_standings_position=5, opponent_standings_position=8,
        scheduled_game_date="2026-03-01", season=_SEASON, phase=_PHASE,
        cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# score -- one signal at a time
# ---------------------------------------------------------------------------


def test_same_division_scores_two():
    assert score(_signals(same_division=True)) == 2


def test_close_games_score_one_each_capped_at_three():
    assert score(_signals(close_game_count=2)) == 2
    assert score(_signals(close_game_count=3)) == 3
    assert score(_signals(close_game_count=5)) == 3  # capped


def test_season_series_level_scores_one():
    assert score(_signals(season_series_level=True)) == 1


def test_recent_trade_scores_one():
    assert score(_signals(recent_trade=True)) == 1


def test_h2h_signal_scores_one():
    assert score(_signals(h2h_signal=True)) == 1


def test_every_signal_together():
    assert score(
        _signals(
            same_division=True, close_game_count=3, season_series_level=True,
            recent_trade=True, h2h_signal=True,
        )
    ) == 8


# ---------------------------------------------------------------------------
# season_series_is_level -- well-defined for 2/3/4-game series
# ---------------------------------------------------------------------------


def test_season_series_level_two_game_series():
    assert season_series_is_level(completed=2, wins_a=1, wins_b=1)
    assert not season_series_is_level(completed=2, wins_a=2, wins_b=0)


def test_season_series_level_three_game_series():
    assert season_series_is_level(completed=2, wins_a=1, wins_b=1)
    assert season_series_is_level(completed=3, wins_a=2, wins_b=1)
    assert not season_series_is_level(completed=3, wins_a=3, wins_b=0)


def test_season_series_level_four_game_series():
    assert season_series_is_level(completed=4, wins_a=2, wins_b=2)
    assert not season_series_is_level(completed=4, wins_a=3, wins_b=1)


def test_season_series_not_level_before_two_games_complete():
    assert not season_series_is_level(completed=1, wins_a=1, wins_b=0)
    assert not season_series_is_level(completed=0, wins_a=0, wins_b=0)


# ---------------------------------------------------------------------------
# rivalry_matchup_package -- the threshold boundary and the scheduled-game gate
# ---------------------------------------------------------------------------


def test_score_of_three_does_not_fire():
    signals = _signals(close_game_count=3)  # score == 3
    assert rivalry_matchup_package(_TEAM, _OPPONENT, signals, **_package_kwargs()) is None


def test_score_of_four_fires():
    signals = _signals(close_game_count=3, recent_trade=True)  # score == 4
    package = rivalry_matchup_package(_TEAM, _OPPONENT, signals, **_package_kwargs())
    assert package is not None
    assert package.story_kind is StoryKind.RIVALRY_MATCHUP
    assert package.number("rivalry_score") == 4


def test_no_upcoming_scheduled_game_never_fires_even_at_maximum_score():
    signals = _signals(
        same_division=True, close_game_count=5, season_series_level=True,
        recent_trade=True, h2h_signal=True,
    )
    assert score(signals) == 8
    package = rivalry_matchup_package(
        _TEAM, _OPPONENT, signals, **_package_kwargs(has_upcoming_scheduled_game=False)
    )
    assert package is None


def test_subject_is_a_matchup_of_both_teams():
    from dba2.domain.media_facts import SubjectKind

    signals = _signals(same_division=True, recent_trade=True, h2h_signal=True)  # score == 4
    package = rivalry_matchup_package(_TEAM, _OPPONENT, signals, **_package_kwargs())
    assert package is not None
    assert package.subject.kind is SubjectKind.MATCHUP
    assert package.subject.team_id == _TEAM
    assert package.subject.opponent_team_id == _OPPONENT


# ---------------------------------------------------------------------------
# BLOCKING 5 (critic round on commits b288b8e..99db9f4): MV-2f's named-actor
# fact group (`:1159-1161`) was silently dropped entirely. The top-2-by-
# `overall_cached` half is implemented here; the shared-players/trade-history
# half is disclosed as deferred (this module's own docstring).
# ---------------------------------------------------------------------------

_SCORING_SIGNALS = _signals(same_division=True, recent_trade=True, h2h_signal=True)  # score == 4


def test_named_players_emit_a_fact_per_side_when_supplied():
    package = rivalry_matchup_package(
        _TEAM, _OPPONENT, _SCORING_SIGNALS,
        team_named_players=("Marcus Webb", "Devon Cole"),
        opponent_named_players=("Ray Ortiz",),
        **_package_kwargs(),
    )
    assert package is not None
    assert package.text("team_named_players") == "Marcus Webb, Devon Cole"
    assert package.text("opponent_named_players") == "Ray Ortiz"


def test_named_players_absent_emits_no_fact_never_a_placeholder():
    """Before this fix, `rivalry_matchup_package` had no parameter for this
    at all, so no template could ever declare `team_named_players`/
    `opponent_named_players` without failing load-time validation --
    reproduced by the registry-subset test in `test_media_facts.py`. This
    pins the "absent is neutral" half: calling with the (default) empty
    tuples emits neither key at all, never an empty-string placeholder."""
    package = rivalry_matchup_package(_TEAM, _OPPONENT, _SCORING_SIGNALS, **_package_kwargs())
    assert package is not None
    assert package.text("team_named_players") is None
    assert package.text("opponent_named_players") is None
