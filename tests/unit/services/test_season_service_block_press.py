"""The block press's pure halves (phase-5-stage-e-advance.md P5-99.i, P5-99.u,
build-plan commit 10): the block bound's arithmetic, the failed-game refusal's
message, and the new transaction vocabulary.

The bound is the one place a block can silently over- or under-sim: a block that
ignored the phase's target would sim a whole game-day past it, and one that
ignored the date would sim less than a day. Its two inputs are read from the
database by the press; the arithmetic itself is pure and lives here.
"""

from __future__ import annotations

import pytest

from dba2.core.errors import PhaseError
from dba2.domain.phase import Phase
from dba2.domain.transaction import TransactionType
from dba2.services import season_service
from dba2.services.game_sim_service import GameFailure


@pytest.mark.parametrize(
    ("first_index_on_date", "last_index_on_date", "target_index", "expected"),
    [
        (0, 9, 40, 9),  # the day ends before the target: sim the whole day
        (0, 9, 9, 9),  # the day ends exactly at the target
        (0, 9, 6, 6),  # the target falls inside the day: stop at the target
        (4, 4, 4, 4),  # a one-game day that is also the last due game
        (8, 12, 8, 8),  # the target is the day's first game: one game, not five
    ],
)
def test_the_block_bound_is_the_end_of_the_day_capped_at_the_phases_target(
    first_index_on_date, last_index_on_date, target_index, expected
):
    assert (
        season_service.block_bound(first_index_on_date, last_index_on_date, target_index)
        == expected
    )


def test_a_day_that_starts_past_the_target_is_refused_not_turned_into_an_empty_block():
    """`block_bound` is only called for a phase whose target is unmet, so the
    first unplayed game is at or below it. A bound below the day's first game
    would be a block of nothing -- a press that "succeeds" having simmed no game
    -- so the arithmetic refuses instead of papering over a broken caller."""
    with pytest.raises(ValueError):
        season_service.block_bound(10, 12, 9)


def test_the_failed_game_refusal_names_every_id_and_every_cause_and_is_a_phase_error():
    failures = (
        GameFailure(game_id=41, cause="missing team or strategy snapshot data"),
        GameFailure(game_id=42, cause="roster snapshot incomplete"),
    )
    error = season_service.GamesFailedToSimError(
        game_ids=(41, 42),
        failures=failures,
        gate_lines=("2 game(s) this phase must play before it can be left are not played yet.",),
    )

    assert isinstance(error, PhaseError), "the cog already maps PhaseError to a reply"
    assert error.game_ids == (41, 42)
    assert error.failures == failures
    text = str(error)
    for needle in ("41", "42", "missing team or strategy snapshot data", "roster snapshot"):
        assert needle in text
    assert "not played yet" in text, "the gate's own line rides in the same reply"
    assert "/advance-postpone-game" in text, "the refusal names the one recoverable path"


def test_a_failure_without_a_recorded_cause_still_names_its_game():
    error = season_service.GamesFailedToSimError(game_ids=(7,), failures=(), gate_lines=())
    assert "7" in str(error)


def test_the_two_new_transaction_types_are_written_as_their_own_vocabulary():
    assert TransactionType.GAME_POSTPONED.value == "game_postponed"
    assert TransactionType.GAME_RESTORED.value == "game_restored"


# --- JC-13: the first trade window's threshold (P5-99.k, build-plan commit 12) ---------


@pytest.mark.parametrize(
    ("max_game_index", "expected"),
    [
        (1229, 138),  # the real 1,230-game season: round(1229 * 0.25 * 0.45)
        (14, 2),  # a 15-game test schedule: round(1.575)
        (1, 0),  # a two-game schedule: the first game is the threshold
        (0, 0),  # a one-game schedule
    ],
)
def test_the_first_trade_window_threshold_is_the_target_index_scaled_by_the_fraction(
    max_game_index, expected
):
    assert season_service.first_trade_window_target_index(max_game_index) == expected


def test_the_first_trade_window_threshold_never_exceeds_the_regular_season_target():
    for max_game_index in range(0, 1230, 7):
        threshold = season_service.first_trade_window_target_index(max_game_index)
        target = season_service.games_target_index(
            season_service.GAMES_TARGET_FRACTION[Phase.REGULAR_SEASON], max_game_index
        )
        assert threshold <= target, (
            f"at {max_game_index + 1} games the window ({threshold}) opens after the phase's "
            f"own target ({target}), so it could never run before the phase is left"
        )
