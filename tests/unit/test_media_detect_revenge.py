"""`dba2/media/detect/revenge.py` -- MV-2i: `REVENGE_GAME`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.media.detect.revenge import first_meeting_since_trade, revenge_game_package

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 6
_PLAYER = 1
_TEAM = 1
_OPPONENT = 2


def _kwargs(**overrides):
    base = dict(
        player_id=_PLAYER, team_id=_TEAM, opponent_team_id=_OPPONENT,
        trade_season=_SEASON, trade_asset_count=3,
        is_first_meeting_since_trade=True, has_upcoming_scheduled_game=True,
        player_games_played_this_season=10,
        team_standings_position=5, opponent_standings_position=10,
        scheduled_game_date="2026-03-01", got_his_wish=None,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


def test_a_trade_three_seasons_back_does_not_fire():
    assert revenge_game_package(**_kwargs(trade_season=_SEASON - 3)) is None


def test_a_trade_last_season_fires():
    package = revenge_game_package(**_kwargs(trade_season=_SEASON - 1))
    assert package is not None
    assert package.story_kind is StoryKind.REVENGE_GAME


def test_no_upcoming_scheduled_game_never_fires_even_at_maximum_asset_count():
    package = revenge_game_package(
        **_kwargs(has_upcoming_scheduled_game=False, trade_asset_count=100)
    )
    assert package is None


def test_the_second_meeting_after_the_trade_does_not_fire_while_the_first_does():
    first = revenge_game_package(**_kwargs(is_first_meeting_since_trade=True))
    assert first is not None
    second = revenge_game_package(**_kwargs(is_first_meeting_since_trade=False))
    assert second is None


def test_games_played_below_five_does_not_fire_five_does():
    assert revenge_game_package(**_kwargs(player_games_played_this_season=4)) is None
    assert revenge_game_package(**_kwargs(player_games_played_this_season=5)) is not None


def test_got_his_wish_none_is_not_carried_as_a_fact():
    package = revenge_game_package(**_kwargs(got_his_wish=None))
    assert package is not None
    assert package.text("got_his_wish") is None


def test_got_his_wish_a_real_reading_is_carried():
    true_package = revenge_game_package(**_kwargs(got_his_wish=True))
    assert true_package is not None
    assert true_package.number("got_his_wish") == 1
    assert true_package.text("got_his_wish") == "Yes"  # bug B2: not the literal "1"

    false_package = revenge_game_package(**_kwargs(got_his_wish=False))
    assert false_package is not None
    assert false_package.number("got_his_wish") == 0
    assert false_package.text("got_his_wish") == "No"  # bug B2: not the literal "0"


def test_subject_carries_both_teams_and_the_player():
    from dba2.domain.media_facts import SubjectKind

    package = revenge_game_package(**_kwargs())
    assert package is not None
    assert package.subject.kind is SubjectKind.PLAYER
    assert package.subject.player_id == _PLAYER
    assert package.subject.team_id == _TEAM
    assert package.subject.opponent_team_id == _OPPONENT


# ---------------------------------------------------------------------------
# `first_meeting_since_trade` -- MV-15: MV-2i condition 3's real derivation.
# `games.simmed_at` vs `league_transactions.created_at` (both real wall
# clock), NOT the original `games.game_date` vs `created_at.date()` (two
# unrelated clocks -- the league's simulated calendar vs real wall clock --
# that this loader's own callers previously compared, which could never be
# true in the intended case). Every case here is deterministic, no DB.
# ---------------------------------------------------------------------------

_TRADE_CREATED_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_BEFORE = _TRADE_CREATED_AT - timedelta(days=1)
_AFTER = _TRADE_CREATED_AT + timedelta(days=1)


def test_no_prior_final_meetings_is_the_first_meeting():
    assert first_meeting_since_trade([], _TRADE_CREATED_AT) is True


def test_a_meeting_simmed_strictly_before_the_trade_is_still_the_first_meeting():
    # This is the bug in miniature: the ORIGINAL `game_date > created_at.
    # date()` comparison read this as "not the first meeting" whenever the
    # league's simulated calendar sat later than the transaction's wall-
    # clock insert date, which is true for essentially every real trade in
    # a season-2026 league today. The fix must return True here.
    assert first_meeting_since_trade([_BEFORE], _TRADE_CREATED_AT) is True


def test_a_meeting_simmed_strictly_after_the_trade_is_not_the_first_meeting():
    assert first_meeting_since_trade([_AFTER], _TRADE_CREATED_AT) is False


def test_meetings_on_both_sides_of_the_trade_is_not_the_first_meeting():
    assert first_meeting_since_trade([_BEFORE, _AFTER], _TRADE_CREATED_AT) is False


def test_a_meeting_simmed_at_exactly_the_trades_timestamp_counts_as_before():
    # Postgres `NOW()` is transaction-start time, so a game finalized in the
    # same transaction as the trade ties -- the tie resolves toward "before"
    # (silence-favoring: it does not fabricate a blocked claim), matching
    # `motivation.py::request_state_at`'s own strict `>` for "after".
    assert first_meeting_since_trade([_TRADE_CREATED_AT], _TRADE_CREATED_AT) is True


def test_a_final_game_with_no_simmed_at_blocks_the_claim():
    # `simmed_at is None` is unreachable through `finalize_game` but not
    # through `set_status`, so this function must be total over it -- an
    # unorderable meeting cannot support "this is the first one."
    assert first_meeting_since_trade([None], _TRADE_CREATED_AT) is False
