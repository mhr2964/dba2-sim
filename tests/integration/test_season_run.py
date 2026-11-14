"""`season_run.run_to` over real rows (docs/design/advance-run.md §3).

Two kinds of league, for two costs (`_press_fixtures`): a REAL sim-able league whose schedule is
trimmed with `keep_games=N`, so a whole stretch of the season is a handful of real presses, and
a bare league whose subject is one stop -- a draft that stalls, a draft that waits on a person, a
game that cannot sim. What is proved here that the scripted-press unit tests cannot: the target
rule against the league's real pointer (the run enters the target and makes no press after it),
a cancel that leaves a league the next run resumes from, the caps between real presses, and each
draft stop derived from the REAL DRAFT hook's `DraftCycleState`.

The full-season walk (`test_full_season_walk.py`) is the acceptance test and runs through
`run_to` too.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

from datetime import date

import pytest

from dba2.core.errors import PhaseError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo
from dba2.domain.game import GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_service, season_run, season_service
from dba2.services.gm_service import DraftCycleReason
from dba2.services.season_run import RunCaps, RunStop
from dba2.services.season_service import BlockerCode, PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    LIBRARY,
    SEASON,
    add_draft_board,
    add_game,
    add_two_teams,
    bare_league_at,
    drop,
    sim_league_at,
)

_HUMAN = 202


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture
def no_closing_work(monkeypatch):
    """A bare league's closing media cycle is `test_season_service_media_cycle.py`'s subject."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.CLOSING, {})


@pytest.fixture
def stub_hook(monkeypatch):
    """The AI hook is proved elsewhere; the cases that only need the loop stub it."""

    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


async def _run(
    pool,
    league_id: int,
    to_phase: Phase,
    *,
    caps: RunCaps | None = None,
    should_continue=lambda: True,
    actor_user_id: int = COMMISSIONER,
    seen: list | None = None,
):
    async def on_step(step) -> None:
        if seen is not None:
            seen.append(step)

    return await season_run.run_to(
        pool,
        league_id=league_id,
        actor_user_id=actor_user_id,
        to_phase=to_phase,
        library=LIBRARY,
        caps=caps or RunCaps(),
        should_continue=should_continue,
        on_step=on_step,
    )


async def _league(pool, league_id: int):
    league = await league_repo.get_league(pool, league_id)
    assert league is not None
    return league


async def _regular_games(pool, league_id: int):
    games = await game_repo.list_games_for_league_season(pool, league_id, SEASON)
    return [g for g in games if g.game_type is GameType.REGULAR]


# --- the target rule, over a real sim league -----------------------------------------------


async def test_a_run_plays_the_regular_season_and_stops_on_entering_playin(pool):
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=12)
    try:
        seen: list = []
        report = await _run(pool, league_id, Phase.PLAYIN, seen=seen)

        assert report.stop is RunStop.TARGET_REACHED
        assert report.phases_entered == (
            Phase.TRADE_DEADLINE,
            Phase.POST_DEADLINE,
            Phase.REGULAR_SEASON_COMPLETE,
            Phase.PLAYIN,
        )
        # The last press is the ENTRY that enters the target; the run made none after it.
        last = report.last_report
        assert last is not None
        assert (last.kind, last.to_phase) == (PressKind.ENTRY, Phase.PLAYIN)
        assert [s.number for s in seen] == list(range(1, len(seen) + 1))
        assert (await _league(pool, league_id)).current_phase is Phase.PLAYIN

        games = await _regular_games(pool, league_id)
        assert len(games) == 12 and all(g.status is GameStatus.FINAL for g in games)
        assert report.games == 12
        assert report.game_days == len({g.game_date for g in games})
        assert report.presses_by_kind[PressKind.BLOCK] >= 3
        assert report.presses_by_kind[PressKind.BLOCKED] == 0
        assert (await _league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)


async def test_a_target_is_a_brake_on_the_phase_after_it_not_on_the_phase_it_enters(pool):
    """`REGULAR_SEASON_COMPLETE` is entered at POST_DEADLINE's 75% target; the last quarter is
    played by block presses INSIDE it (43 of the walk's 147). A run to it stops on the entry, so
    those games are still scheduled: the rule the design states (AR-5), asserted on rows."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=12)
    try:
        report = await _run(pool, league_id, Phase.REGULAR_SEASON_COMPLETE)

        assert report.stop is RunStop.TARGET_REACHED
        assert report.final_phase is Phase.REGULAR_SEASON_COMPLETE
        games = await _regular_games(pool, league_id)
        assert [g.status for g in games].count(GameStatus.FINAL) == report.games < 12
        assert (await _league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)


async def test_a_run_never_overshoots_a_target_two_entries_ahead(pool):
    """Mutation 8's assertion: the run stops ON the entry into the target -- not before it (a
    target test of "the target is ahead in the route" stops on the first entry) and not past it
    -- and the league is where the report says."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=12)
    try:
        report = await _run(pool, league_id, Phase.POST_DEADLINE)

        assert report.stop is RunStop.TARGET_REACHED
        assert report.final_phase is Phase.POST_DEADLINE
        assert report.phases_entered == (Phase.TRADE_DEADLINE, Phase.POST_DEADLINE)
        league = await _league(pool, league_id)
        assert league.current_phase is Phase.POST_DEADLINE
        games = await _regular_games(pool, league_id)
        played = [g for g in games if g.status is GameStatus.FINAL]
        assert 0 < len(played) < len(games), "POST_DEADLINE's target is 75%: not the whole slate"
    finally:
        await drop(pool, league_id)


async def test_a_run_from_inside_the_target_is_already_there_and_makes_no_press(pool):
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=6)
    try:
        report = await _run(pool, league_id, Phase.REGULAR_SEASON)

        assert report.stop is RunStop.TARGET_REACHED and report.steps == ()
        assert (await _league(pool, league_id)).current_phase is Phase.REGULAR_SEASON
        assert all(g.status is GameStatus.SCHEDULED for g in await _regular_games(pool, league_id))
    finally:
        await drop(pool, league_id)


async def test_a_backwards_target_is_refused_before_any_press(pool, stub_hook):
    league_id = await bare_league_at(pool, Phase.POST_DEADLINE)
    try:
        with pytest.raises(PhaseError, match="/advance-run only goes forward"):
            await _run(pool, league_id, Phase.REGULAR_SEASON)
        assert (await _league(pool, league_id)).current_phase is Phase.POST_DEADLINE
    finally:
        await drop(pool, league_id)


async def test_a_non_commissioner_is_refused_before_any_press(pool, stub_hook):
    league_id = await bare_league_at(pool, Phase.POST_DEADLINE)
    try:
        with pytest.raises(PermissionDenied):
            await _run(pool, league_id, Phase.REGULAR_SEASON_COMPLETE, actor_user_id=999)
        assert (await _league(pool, league_id)).current_phase is Phase.POST_DEADLINE
    finally:
        await drop(pool, league_id)


# --- the caps and the cancel, between real presses ----------------------------------------------


async def test_the_press_cap_ends_a_real_run_after_exactly_that_many_presses(pool):
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=12)
    try:
        report = await _run(pool, league_id, Phase.PLAYIN, caps=RunCaps(max_presses=2))

        assert report.stop is RunStop.CAP_REACHED and report.presses == 2
        assert (await _league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)


async def test_a_cancelled_run_leaves_a_league_the_next_run_resumes_from(pool):
    """`/advance-run-stop`, then `/advance-run` again: the press in flight committed, no claim
    is left, and the second run picks up exactly where the first stopped."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=12)
    try:
        steps: list = []
        first = await _run(
            pool,
            league_id,
            Phase.PLAYIN,
            seen=steps,
            should_continue=lambda: len(steps) < 2,
        )
        assert first.stop is RunStop.CANCELLED and first.presses == 2
        assert (await _league(pool, league_id)).advance_claimed_at is None

        second = await _run(pool, league_id, Phase.PLAYIN)

        assert second.stop is RunStop.TARGET_REACHED
        games = await _regular_games(pool, league_id)
        assert all(g.status is GameStatus.FINAL for g in games)
        assert first.games + second.games == 12, "no game simmed twice, none skipped"
    finally:
        await drop(pool, league_id)


# --- the draft stops, from the REAL hook's DraftCycleState --------------------------------------


async def test_a_stalled_draft_board_stops_the_run_as_draft_stalled_with_its_reason(
    pool, no_closing_work
):
    """Mutation 3's case. Deleting row 6 leaves this run pressing a board that makes no pick
    and moves no pointer, so it would end as `NO_PROGRESS` ("changed nothing") instead of
    naming the cause -- the asserted stop and reason go red."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), prospects=0)

        report = await _run(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert report.stop is RunStop.DRAFT_STALLED
        assert report.presses == 1
        assert report.last_report.kind is PressKind.DRAFT_RESUME
        assert report.draft_stop is not None
        assert report.draft_stop.reason is DraftCycleReason.POOL_EXHAUSTED
        assert (await _league(pool, league_id)).current_phase is Phase.DRAFT
    finally:
        await drop(pool, league_id)


async def test_a_board_with_an_unresolved_pick_number_is_the_lotterys_stall(pool, no_closing_work):
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"), (1, None, "BOS")), used=(0,))

        report = await _run(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert report.stop is RunStop.DRAFT_STALLED
        assert report.draft_stop.reason is DraftCycleReason.UNRESOLVED_PICKS
    finally:
        await drop(pool, league_id)


async def test_a_draft_waiting_on_a_person_stops_the_run_naming_the_team_on_the_clock(
    pool, no_closing_work
):
    """Mutation 2's case: with row 5 deleted the run presses a board only `/draft` can move,
    and its stop is the no-progress net (or the cap), not `NEEDS_A_HUMAN_PICK`."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        board = await add_draft_board(
            pool, league_id, picks=((1, 1, "TOR"),), managers={"TOR": _HUMAN}
        )

        report = await _run(pool, league_id, Phase.POST_DRAFT_TRADES, caps=RunCaps(max_presses=6))

        assert report.stop is RunStop.NEEDS_A_HUMAN_PICK
        assert report.presses == 1
        assert report.draft_stop.team_id == board.teams["TOR"]
        assert report.draft_stop.manager_user_id == _HUMAN
    finally:
        await drop(pool, league_id)


async def test_a_completed_draft_lets_the_run_leave_it(pool, no_closing_work, stub_hook):
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), used=(0,))

        report = await _run(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert report.stop is RunStop.TARGET_REACHED
        assert report.phases_entered == (Phase.POST_DRAFT_TRADES,)
    finally:
        await drop(pool, league_id)


async def test_a_missing_draft_board_stops_a_run_at_the_lottery_without_moving_the_pointer(
    pool, no_closing_work, stub_hook
):
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        report = await _run(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert report.stop is RunStop.NEEDS_A_DRAFT_BOARD and report.presses == 1
        blockers = report.last_report.verdict.blockers
        assert [b.code for b in blockers] == [BlockerCode.DRAFT_BOARD_NOT_READY]
        assert (await _league(pool, league_id)).current_phase is Phase.DRAFT_LOTTERY
    finally:
        await drop(pool, league_id)


# --- a press that raises ------------------------------------------------------------------------


async def test_a_game_that_cannot_sim_ends_the_run_as_a_press_failure_with_the_cause(
    pool, stub_hook
):
    """A block press that finalizes nothing raises `GamesFailedToSimError`, which is row 7 --
    not the no-progress net, which is why no league fixture can drive that net (AR-6)."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 11, 1))

        report = await _run(pool, league_id, Phase.TRADE_DEADLINE)

        assert report.stop is RunStop.PRESS_FAILED
        assert isinstance(report.error, season_service.GamesFailedToSimError)
        assert report.error.game_ids == (game.id,)
        assert report.presses == 0
        assert (await _league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)
