"""`postpone_game` and `restore_game` (phase-5-stage-e-advance.md P5-99.u item 5,
P5-99.u.1, P5-99.u.2, P5-99.t.1, build-plan commit 10), over real rows.

A postponement is the one recoverable path out of a game that cannot sim (F11):
it moves the game out of `list_scheduled_games`' filter, so the block cursor moves
on and the gate clears. The restore is its reversal, and it is not free -- three
refusals, one case each: outside the four schedule-consuming phases (P5-99.u.1
refusal 1), at `REGULAR_SEASON_COMPLETE` once the season's stats have been rebuilt
(refusal 2, M-2), and when the league has already played PAST the game's date
(refusal 3, M-3, which would otherwise walk the block cursor backwards onto a
closed block id). A date the block press only HALF-played is not past: it is the
dominant case the restore exists for (one bad game on a multi-game date), so it
restores, and the case that proves it is produced by real presses, not a
hand-built fixture.

Both are human writes, so both read the advance claim (P5-99.t.1: two of the nine
write paths): with a press running, they refuse with `CLAIM_HELD_MESSAGE` and write
nothing.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

from datetime import date

import pytest

from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo, txn_repo
from dba2.domain.game import GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.domain.transaction import TransactionType
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_service, league_claim, season_service, strategy_service
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_game,
    add_two_teams,
    bare_league_at,
    drop,
    press,
    regular_games_by_date,
    sim_league_at,
)

_OTHER_USER = 99
_D1 = date(2026, 11, 1)
_D2 = date(2026, 11, 2)
_D3 = date(2026, 11, 3)


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def _stub_the_hook(monkeypatch):
    """The presses below are about the schedule, not the AI hook (proved elsewhere)."""

    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


async def _status(pool, game_id: int) -> GameStatus:
    return (await game_repo.get_game(pool, game_id)).status


async def _rows(pool, league_id: int, txn_type: TransactionType):
    return [
        t
        for t in await txn_repo.list_transactions_for_league(pool, league_id)
        if t.txn_type is txn_type
    ]


async def _postpone(pool, league_id: int, game_id: int, *, reason: str = "cannot sim"):
    return await season_service.postpone_game(
        pool, league_id=league_id, actor_user_id=COMMISSIONER, game_id=game_id, reason=reason
    )


async def _restore(pool, league_id: int, game_id: int, *, reason: str = "data fixed"):
    return await season_service.restore_game(
        pool, league_id=league_id, actor_user_id=COMMISSIONER, game_id=game_id, reason=reason
    )


async def _postponed_game(pool, phase: Phase = Phase.REGULAR_SEASON):
    """A league at `phase` with one game already postponed. Returns `(league_id, game)`."""
    league_id = await bare_league_at(pool, phase)
    teams = await add_two_teams(pool, league_id)
    game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
    await game_repo.set_status(
        pool, game.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
    )
    return league_id, game


# --- postpone ------------------------------------------------------------------


async def test_postponing_a_scheduled_game_moves_it_and_writes_an_audited_transaction_row(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)

        change = await _postpone(pool, league_id, game.id, reason="  a team has no strategy  ")

        assert change.changed is True
        assert (change.game_id, change.from_status, change.to_status) == (
            game.id,
            GameStatus.SCHEDULED,
            GameStatus.POSTPONED,
        )
        assert await _status(pool, game.id) is GameStatus.POSTPONED
        (row,) = await _rows(pool, league_id, TransactionType.GAME_POSTPONED)
        assert row.actor_user_id == COMMISSIONER
        assert row.note == "a team has no strategy", "the reason is stored trimmed"
        assert row.details["game_id"] == game.id
        assert (row.season, row.phase) == (SEASON, Phase.REGULAR_SEASON)
        assert change.transaction_id == row.id
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)


async def test_a_postponed_game_leaves_the_schedule_the_block_cursor_reads(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        league = await league_repo.get_league(pool, league_id)
        before = season_service.evaluate_exit(await season_service.load_gate_facts(pool, league))
        assert before.allowed is False

        await _postpone(pool, league_id, game.id)

        after = season_service.evaluate_exit(await season_service.load_gate_facts(pool, league))
        assert after.allowed is True, "a postponement clears the schedule blocker at once"
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("which", ["postpone", "restore"])
async def test_a_held_claim_refuses_both_writes_and_writes_nothing(pool, which):
    """P5-99.t.1: `sim_scheduled_games` snapshots its game list before the block
    runs, so a postponement committing mid-block would make that game a sim FAILURE
    (`finalize_game` raises on a `postponed` row). With the claim check the write
    simply waits its turn and refuses."""
    league_id, game = await _postponed_game(pool)
    try:
        if which == "postpone":
            await game_repo.set_status(
                pool, game.id, from_status=GameStatus.POSTPONED, to_status=GameStatus.SCHEDULED
            )
        await season_service._claim_league(pool, league_id, _OTHER_USER)
        expected = GameStatus.SCHEDULED if which == "postpone" else GameStatus.POSTPONED
        write = _postpone if which == "postpone" else _restore

        with pytest.raises(ConcurrencyError) as refused:
            await write(pool, league_id, game.id)

        assert str(refused.value) == league_claim.CLAIM_HELD_MESSAGE
        assert await _status(pool, game.id) is expected
        assert await txn_repo.list_transactions_for_league(pool, league_id) == []
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("which", ["postpone", "restore"])
async def test_only_the_commissioner_may_postpone_or_restore(pool, which):
    league_id, game = await _postponed_game(pool)
    try:
        write = season_service.postpone_game if which == "postpone" else season_service.restore_game
        with pytest.raises(PermissionDenied):
            await write(
                pool,
                league_id=league_id,
                actor_user_id=_OTHER_USER,
                game_id=game.id,
                reason="not my call",
            )
        assert await txn_repo.list_transactions_for_league(pool, league_id) == []
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_at is None
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("reason", ["", "   "])
async def test_a_postponement_without_a_reason_is_refused(pool, reason):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        with pytest.raises(ValidationError, match="reason"):
            await _postpone(pool, league_id, game.id, reason=reason)
        assert await _status(pool, game.id) is GameStatus.SCHEDULED
    finally:
        await drop(pool, league_id)


async def test_a_game_that_is_not_this_leagues_is_not_found(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    other_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        other_teams = await add_two_teams(pool, other_id)
        foreign = await add_game(pool, other_id, other_teams, game_index=0, game_date=_D1)
        with pytest.raises(NotFoundError):
            await _postpone(pool, league_id, foreign.id)
        with pytest.raises(NotFoundError):
            await _postpone(pool, league_id, foreign.id + 10_000_000)
        assert await _status(pool, foreign.id) is GameStatus.SCHEDULED
    finally:
        await drop(pool, league_id)
        await drop(pool, other_id)


@pytest.mark.parametrize("status", ["final", "postponed"])
async def test_the_state_machine_decides_what_can_be_postponed(pool, status):
    """`game_repo.set_status` does not consult `GAME_TRANSITIONS`; the service does
    (P5-99.u.2). `FINAL -> POSTPONED` and `POSTPONED -> POSTPONED` are not edges."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        await pool.execute(
            "UPDATE games SET status = $2, home_score = 1, away_score = 0 WHERE id = $1",
            game.id,
            status,
        )
        with pytest.raises(ValidationError, match=status):
            await _postpone(pool, league_id, game.id)
        assert (await _status(pool, game.id)).value == status
        assert await _rows(pool, league_id, TransactionType.GAME_POSTPONED) == []
    finally:
        await drop(pool, league_id)


async def test_a_zero_row_update_is_reported_not_raised_and_writes_no_audit_row(pool, monkeypatch):
    """`set_status`' `AND status = $2` guard is a silent no-op when somebody else
    already moved the row; the service reports it instead of pretending it acted."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        real = game_repo.set_status

        async def racing(ex, game_id, *, from_status, to_status):
            await pool.execute("UPDATE games SET status = 'postponed' WHERE id = $1", game_id)
            return await real(ex, game_id, from_status=from_status, to_status=to_status)

        monkeypatch.setattr(game_repo, "set_status", racing)

        change = await _postpone(pool, league_id, game.id)

        assert change.changed is False
        assert change.transaction_id is None
        assert await _rows(pool, league_id, TransactionType.GAME_POSTPONED) == []
    finally:
        await drop(pool, league_id)


# --- restore: the happy path ----------------------------------------------------


async def test_restoring_a_postponed_game_puts_it_back_on_the_schedule_with_an_audit_row(pool):
    league_id, game = await _postponed_game(pool)
    try:
        change = await _restore(pool, league_id, game.id, reason="the data is fixed")

        assert change.changed is True
        assert (change.from_status, change.to_status) == (
            GameStatus.POSTPONED,
            GameStatus.SCHEDULED,
        )
        assert await _status(pool, game.id) is GameStatus.SCHEDULED
        (row,) = await _rows(pool, league_id, TransactionType.GAME_RESTORED)
        assert (row.actor_user_id, row.note) == (COMMISSIONER, "the data is fixed")
        assert row.details["game_id"] == game.id
        league = await league_repo.get_league(pool, league_id)
        facts = await season_service.load_gate_facts(pool, league)
        assert [g.game_id for g in facts.scheduled_games] == [game.id]
    finally:
        await drop(pool, league_id)


async def test_a_game_that_is_not_postponed_cannot_be_restored(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        with pytest.raises(ValidationError, match="scheduled"):
            await _restore(pool, league_id, game.id)
        assert await _rows(pool, league_id, TransactionType.GAME_RESTORED) == []
    finally:
        await drop(pool, league_id)


# --- restore: the three refusals (P5-99.u.1) -------------------------------------


async def test_refusal_1_a_restore_outside_the_four_schedule_consuming_phases(pool):
    league_id, game = await _postponed_game(pool, Phase.PLAYIN)
    try:
        with pytest.raises(PhaseError) as refused:
            await _restore(pool, league_id, game.id)

        assert Phase.PLAYIN.value in str(refused.value)
        assert await _status(pool, game.id) is GameStatus.POSTPONED
        assert await _rows(pool, league_id, TransactionType.GAME_RESTORED) == []
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    "phase",
    [
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    ],
)
async def test_a_restore_is_allowed_in_each_of_the_four_schedule_consuming_phases(pool, phase):
    league_id, game = await _postponed_game(pool, phase)
    try:
        await _restore(pool, league_id, game.id)
        assert await _status(pool, game.id) is GameStatus.SCHEDULED
    finally:
        await drop(pool, league_id)


async def test_refusal_2_a_restore_at_regular_season_complete_once_the_stats_are_rebuilt(pool):
    """Otherwise the restored game is simmed AFTER `rebuild_player_season_stats` has
    consumed the season, so its box score never reaches `player_season_stats` and
    E-25's assertion can fail on a table that looks fine. The twin: before the
    rebuild has run, the same restore is fine."""
    league_id, game = await _postponed_game(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        await _restore(pool, league_id, game.id)  # closing work not yet run: allowed
        await game_repo.set_status(
            pool, game.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
        )
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)

        with pytest.raises(PhaseError, match="stats"):
            await _restore(pool, league_id, game.id)

        assert await _status(pool, game.id) is GameStatus.POSTPONED
        assert len(await _rows(pool, league_id, TransactionType.GAME_RESTORED)) == 1, (
            "only the first, allowed restore was written"
        )
    finally:
        await drop(pool, league_id)


async def test_refusal_3_a_restore_onto_a_date_the_league_has_already_played_past(pool):
    """The block cursor must not walk backwards: `block_id` is the game date, a date
    is opened once, and `open_block` closes the previous block on a different id. A
    game whose date the league has played PAST (a later date holds a final game)
    therefore cannot be restored; the message says which game is in the way. The
    twin: a date that is only HALF played (a final sibling on the same date, nothing
    later) is not "past" and restores fine."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        blocked = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        played_later = await add_game(pool, league_id, teams, game_index=1, game_date=_D2)
        half_played_sibling = await add_game(pool, league_id, teams, game_index=2, game_date=_D3)
        half_played_target = await add_game(pool, league_id, teams, game_index=3, game_date=_D3)
        for played in (played_later, half_played_sibling):
            await pool.execute(
                "UPDATE games SET status = 'final', home_score = 1, away_score = 0 WHERE id = $1",
                played.id,
            )
        for g in (blocked, half_played_target):
            await game_repo.set_status(
                pool, g.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
            )

        with pytest.raises(PhaseError) as refused:
            await _restore(pool, league_id, blocked.id)
        assert str(played_later.id) in str(refused.value), "names the game that is in the way"
        assert await _status(pool, blocked.id) is GameStatus.POSTPONED

        await _restore(pool, league_id, half_played_target.id)
        assert await _status(pool, half_played_target.id) is GameStatus.SCHEDULED
    finally:
        await drop(pool, league_id)


async def _press_to_a_half_played_date(pool, league_id: int):
    """Real presses: dates 1 and 2 hold one game each, date 3 holds two, and the
    first of those two cannot sim (its home team's strategy row is gone). Returns the
    days, the doomed game, its surviving sibling and the half-playing press's report."""
    days = await regular_games_by_date(pool, league_id)
    for _ in range(2):
        await press(pool, league_id=league_id)
    _date_3, (doomed, survivor) = days[2]
    await pool.execute(
        "DELETE FROM team_strategy WHERE team_id = $1 AND season = $2",
        doomed.home_team_id,
        SEASON,
    )
    half = await press(pool, league_id=league_id)
    assert half.failed_games == (doomed.id,)
    assert [g.game_id for g in half.block.games] == [survivor.id]
    return days, doomed, survivor, half


async def test_a_date_the_block_press_half_played_is_restorable_and_the_next_press_finishes_it(
    pool,
):
    """The dominant real scenario, produced by presses and not built by hand: one bad
    game on a two-game date. The press finalizes the good one and reports the bad one
    failed; the commissioner postpones the bad one, fixes its data and restores it. The
    restore must succeed -- the date is half-played, not played past -- and the next
    press must sim the restored game under the SAME block id, so `open_block` merges
    it into the day's block."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days, doomed, survivor, half = await _press_to_a_half_played_date(pool, league_id)
        await _postpone(pool, league_id, doomed.id)
        await strategy_service.ensure_defaults_for_season(pool, league_id, SEASON)

        change = await _restore(pool, league_id, doomed.id)

        assert change.changed is True
        assert await _status(pool, doomed.id) is GameStatus.SCHEDULED
        assert await _status(pool, survivor.id) is GameStatus.FINAL
        finished = await press(pool, league_id=league_id)
        assert finished.block.block_id == half.block.block_id == days[2][0].isoformat()
        assert [g.game_id for g in finished.block.games] == [doomed.id]
        assert await _status(pool, doomed.id) is GameStatus.FINAL
    finally:
        await drop(pool, league_id)


async def test_a_restore_after_the_league_played_the_next_date_is_refused(pool):
    """The refusal's real purpose, on the same press-produced state: once the press has
    moved on to the next date, restoring the postponed game would put the block cursor
    back on a date the league has left."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days, doomed, _survivor, _half = await _press_to_a_half_played_date(pool, league_id)
        await _postpone(pool, league_id, doomed.id)
        moved_on = await press(pool, league_id=league_id)
        assert moved_on.block.block_id == days[3][0].isoformat()
        await strategy_service.ensure_defaults_for_season(pool, league_id, SEASON)

        with pytest.raises(PhaseError) as refused:
            await _restore(pool, league_id, doomed.id)

        played_past = moved_on.block.games[0].game_id  # the doomed team may fail again on day 4
        assert str(played_past) in str(refused.value), "names a game played past it"
        assert await _status(pool, doomed.id) is GameStatus.POSTPONED
        assert await _rows(pool, league_id, TransactionType.GAME_RESTORED) == []
    finally:
        await drop(pool, league_id)


# --- m5: only a current-season regular-season game may be postponed or restored ------


@pytest.mark.parametrize("which", ["postpone", "restore"])
@pytest.mark.parametrize("kind", ["other_season", "playin"])
async def test_postpone_and_restore_refuse_a_game_that_is_not_this_seasons_regular_game(
    pool, which, kind
):
    """The regular-season gate, the block cursor and the stats rebuild all read the
    CURRENT season's REGULAR games; a postponement of anything else would be a write
    those readers never see and that Phase 6 or a later season would meet cold."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await game_repo.create_game(
            pool,
            league_id=league_id,
            season=SEASON - 1 if kind == "other_season" else SEASON,
            game_index=0,
            game_date=_D1,
            home_team_id=teams[0],
            away_team_id=teams[1],
            game_type=GameType.PLAYIN if kind == "playin" else GameType.REGULAR,
            rng_seed=1,
        )
        start = GameStatus.SCHEDULED if which == "postpone" else GameStatus.POSTPONED
        if which == "restore":
            await game_repo.set_status(
                pool, game.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
            )
        write = _postpone if which == "postpone" else _restore

        with pytest.raises(PhaseError) as refused:
            await write(pool, league_id, game.id)

        message = str(refused.value)
        assert str(game.id) in message
        assert ("season" if kind == "other_season" else "regular") in message
        assert await _status(pool, game.id) is start
        assert await txn_repo.list_transactions_for_league(pool, league_id) == []
    finally:
        await drop(pool, league_id)
