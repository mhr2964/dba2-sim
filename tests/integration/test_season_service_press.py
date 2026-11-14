"""The press core for non-schedule phases (phase-5-stage-e-advance.md P5-99.c,
P5-99.b.2, build-plan commit 8): claim -> gate -> pointer -> offer sweep -> hook
-> hook checkpoint -> release, over real `phase_history` rows.

`gm_service.run_phase_hook` is replaced by a recording spy with the real
function's keyword signature: what these cases prove is WHEN the press calls the
hook, with WHAT, and what it writes around it. The real hook's own behaviour is
proved in `test_gm_service.py`, and its first run inside a whole season is the
full-season walk's (build-plan commit 18).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import date

import asyncpg
import pytest

from dba2.core.errors import ConcurrencyError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import draft_repo, game_repo, league_repo, offer_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.phase import Phase
from dba2.phase import service as phase_service
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_service, league_claim, roster_lock, season_service
from dba2.services.season_service import BlockerCode, PressKind
from tests.integration._press_fixtures import add_draft_board, add_played_game, press

_SEASON = 2026
_COMMISSIONER = 1
_OTHER_USER = 99

# The whole in-season walk, written out by hand (as tests/integration/
# test_phase_entry_counts.py does): the press is driven from real history, so
# the ordinals it derives are the ones a real season produces.
_WALK = [
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.AWARDS,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.POST_DRAFT_TRADES,
    Phase.PROGRESSION,
    Phase.OPTION_DECISIONS,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


async def _league_at(pool, phase: Phase) -> int:
    """A league whose real `phase_history` walks `SETUP` -> ... -> `phase` by
    direct `phase_service.advance` calls (no press, no hook)."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Press Core Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
    )
    if phase is not Phase.SETUP:
        for target in _WALK:
            await phase_service.advance(pool, league.id, target, _COMMISSIONER)
            if target is phase:
                break
    return league.id


async def _drop(pool, league_id: int) -> None:
    await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)


async def _state(pool, league_id: int) -> tuple[Phase, int, bool]:
    league = await league_repo.get_league(pool, league_id)
    claim_released = league.advance_claimed_at is None
    return league.current_phase, league.hook_completed_cycle_index, claim_released


async def _history_count(pool, league_id: int) -> int:
    return await league_repo.count_phase_entries(pool, league_id, _SEASON)


@dataclass
class _HookCall:
    ex: object
    in_txn: bool
    phase: Phase
    league_id: int
    actor_user_id: int
    db_phase: Phase
    checkpoint_at_call: int


def _spy_on_the_hook(monkeypatch) -> list[_HookCall]:
    """Replaces `run_phase_hook` with a recorder that has its real keyword
    signature, so a press passing the wrong arguments fails here."""
    calls: list[_HookCall] = []

    async def spy(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        league = await league_repo.get_league(ex, league_id)
        calls.append(
            _HookCall(
                ex=ex,
                in_txn=isinstance(ex, asyncpg.Connection) and ex.is_in_transaction(),
                phase=phase,
                league_id=league_id,
                actor_user_id=actor_user_id,
                db_phase=league.current_phase,
                checkpoint_at_call=league.hook_completed_cycle_index,
            )
        )
        return gm_service.PhaseHookResult(league_id=league_id, season=_SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", spy)
    return calls


@pytest.fixture
def no_phase_work(monkeypatch):
    """Empties the phase-work map for a case about the HOOK, not the work. Since
    build-plan commit 9 an entry into `PRESEASON`, `TRADE_DEADLINE` or `PROGRESSION`
    runs real work that needs a real league (30 populated teams, a cap-settings row),
    which the hook cases have neither reason nor fixtures for; the work itself is
    proved over real rows in `test_season_service_phase_work.py`."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.ENTRY, {})
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.CLOSING, {})


# --- an Entry press into a cycle-bearing phase ---------------------------------


async def test_a_resign_window_entry_press_moves_the_pointer_and_runs_the_hook_once(
    pool, monkeypatch
):
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        history_before = await _history_count(pool, league_id)

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert report.kind is PressKind.ENTRY
        assert (report.from_phase, report.to_phase) == (Phase.OPTION_DECISIONS, Phase.RESIGN_WINDOW)
        assert (report.entry_index, report.cycle_ordinal) == (16, 6)
        assert report.hook_result is not None
        assert await _state(pool, league_id) == (Phase.RESIGN_WINDOW, 6, True)
        assert await _history_count(pool, league_id) == history_before + 1
        assert len(calls) == 1
        (call,) = calls
        assert call.phase is Phase.RESIGN_WINDOW
        assert call.league_id == league_id
        assert call.actor_user_id == _COMMISSIONER
        assert call.db_phase is Phase.RESIGN_WINDOW, "the hook must run AFTER the pointer moved"
        assert call.checkpoint_at_call == -1, "the checkpoint is written after the hook, not before"
    finally:
        await _drop(pool, league_id)


async def test_a_second_press_in_the_same_cycle_does_not_run_that_cycles_hook_again(
    pool, monkeypatch
):
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        second = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (second.from_phase, second.to_phase) == (Phase.RESIGN_WINDOW, Phase.FREE_AGENCY)
        assert [c.phase for c in calls] == [Phase.RESIGN_WINDOW, Phase.FREE_AGENCY], (
            "RESIGN_WINDOW's hook ran once; the second press ran FREE_AGENCY's own"
        )
        assert await _state(pool, league_id) == (Phase.FREE_AGENCY, 7, True)
    finally:
        await _drop(pool, league_id)


async def test_an_entry_whose_cycle_hook_already_completed_is_not_run_again(pool, monkeypatch):
    """The hook checkpoint is READ, not just written: with cycle 6's hook already
    recorded, an Entry press into cycle 6's phase moves the pointer and runs
    nothing. Dropping the read double-runs the hook."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = 6 WHERE id = $1", league_id
        )

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert report.to_phase is Phase.RESIGN_WINDOW
        assert report.cycle_ordinal == 6
        assert report.hook_result is None
        assert calls == []
        assert await _state(pool, league_id) == (Phase.RESIGN_WINDOW, 6, True)
    finally:
        await _drop(pool, league_id)


# --- F15: the ten non-cycle-bearing entries -----------------------------------


async def test_progression_runs_no_hook_and_the_following_resign_window_press_still_does(
    pool, monkeypatch, no_phase_work
):
    """F15's regression test. Entering `PROGRESSION` (and `OPTION_DECISIONS`) runs
    no hook and writes no `hook_completed_cycle_index`; if either wrote the count
    of cycle-bearing entries so far, that number IS the next cycle's ordinal and
    `RESIGN_WINDOW`'s hook would find itself already done."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.POST_DRAFT_TRADES)
    try:
        into_progression = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert into_progression.to_phase is Phase.PROGRESSION
        assert into_progression.cycle_ordinal is None
        assert into_progression.entry_index == 14
        assert into_progression.hook_result is None
        assert calls == []
        assert await _state(pool, league_id) == (Phase.PROGRESSION, -1, True)

        into_options = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert into_options.to_phase is Phase.OPTION_DECISIONS
        assert into_options.cycle_ordinal is None
        assert calls == []
        assert await _state(pool, league_id) == (Phase.OPTION_DECISIONS, -1, True)

        into_resign = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert into_resign.to_phase is Phase.RESIGN_WINDOW
        assert into_resign.cycle_ordinal == 6
        assert [c.phase for c in calls] == [Phase.RESIGN_WINDOW], "run_resign_window's phase ran"
        assert await _state(pool, league_id) == (Phase.RESIGN_WINDOW, 6, True)
    finally:
        await _drop(pool, league_id)


async def test_a_playin_press_runs_nothing_writes_no_checkpoint_and_is_not_stuck(
    pool, monkeypatch
):
    """B-1's empty-list case: a phase with no work has nothing outstanding, so
    entering it and leaving it are ordinary presses -- no hook, no checkpoint,
    no work unit -- and neither one strands on the work prefix."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        await add_played_game(pool, league_id)  # a season with no schedule is not left (m2)
        into_playin = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert into_playin.to_phase is Phase.PLAYIN
        assert (await _state(pool, league_id))[:2] == (Phase.PLAYIN, -1)

        out_of_playin = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert (out_of_playin.from_phase, out_of_playin.to_phase) == (
            Phase.PLAYIN,
            Phase.PLAYOFFS_R1,
        )
        assert (await _state(pool, league_id))[:2] == (Phase.PLAYOFFS_R1, -1)
        assert calls == []
        assert into_playin.hook_result is None
        assert out_of_playin.hook_result is None
    finally:
        await _drop(pool, league_id)


async def test_a_press_walk_runs_a_hook_at_exactly_the_nine_cycle_bearing_entries(
    pool, monkeypatch, no_phase_work
):
    """Nineteen Entry presses from `SETUP` on a league whose one-game schedule is
    already played (a season with NO schedule is a gate blocker at `REGULAR_SEASON`, so
    the walk needs one, and a played one keeps every gate open): the hook runs at
    each of the nine cycle-bearing entries and at no other, once each, in order.
    `REGULAR_SEASON`'s is JC-13's -- held until the schedule has played a fraction of its
    target -- and with nothing left to play at or below that fraction it is due on the
    Entry press (the held-until-played case, with games to play, is proved in
    `test_season_service_first_trade_window.py`). Every one of the nine has its ordinal
    0..8, and the checkpoint follows it; the two collisions F15 names -- `RESIGN_WINDOW`
    and `DRAFT_LOTTERY` -- are both in the hooks that ran."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.SETUP)
    try:
        await add_played_game(pool, league_id)
        reports = []
        for entered in _WALK:
            if entered is Phase.DRAFT:
                # The pre-entry check (P5-99.h): a board with a resolved pick and a
                # prospect, its pick already spent so the press after this one is an Entry.
                await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), used=(0,))
            reports.append(await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER))

        assert [r.to_phase for r in reports] == _WALK
        assert [r.entry_index for r in reports] == list(range(19))
        assert [c.phase for c in calls] == [
            Phase.PRESEASON,
            Phase.REGULAR_SEASON,
            Phase.TRADE_DEADLINE,
            Phase.POST_DEADLINE,
            Phase.DRAFT_LOTTERY,
            Phase.DRAFT,
            Phase.RESIGN_WINDOW,
            Phase.FREE_AGENCY,
            Phase.WAIVERS,
        ]
        assert [r.cycle_ordinal for r in reports if r.cycle_ordinal is not None] == list(range(9))
        assert [c.checkpoint_at_call for c in calls] == [-1, 0, 1, 2, 3, 4, 5, 6, 7]
        assert await _state(pool, league_id) == (Phase.WAIVERS, 8, True)
    finally:
        await _drop(pool, league_id)


# --- Blocked presses ----------------------------------------------------------


async def test_waivers_is_a_blocked_press_naming_the_phase_7_message(pool, monkeypatch):
    """The gate case. Since build-plan commit 10 the work prefix retries the current
    entry's hook, so a league walked here by direct advances (no hook ever ran) would
    have its `WAIVERS` hook run by this press; the checkpoint is pre-set to cycle 8 so
    this stays a case about the refusal. The retry itself, at `WAIVERS`, is proved in
    `test_season_service_hook_retry.py`."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.WAIVERS)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = 8 WHERE id = $1", league_id
        )
        history_before = await _history_count(pool, league_id)

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert report.kind is PressKind.BLOCKED
        assert report.to_phase is None
        assert [b.code for b in report.verdict.blockers] == [BlockerCode.WAIVERS_EDGE]
        assert calls == []
        assert await _history_count(pool, league_id) == history_before
        assert await _state(pool, league_id) == (Phase.WAIVERS, 8, True), (
            "a blocked press leaves the pointer where it was and releases its claim"
        )
    finally:
        await _drop(pool, league_id)


async def test_a_draft_with_an_unresolved_pick_is_a_draft_resume_press_not_a_blocked_one(
    pool, monkeypatch
):
    """Row 2 of P5-99.b.2 (build-plan commit 13), which until then left this case to the
    gate: a `DRAFT` press with the board short of `COMPLETE` is a Draft-resume press,
    ordered AHEAD of the gate -- "not `COMPLETE`" is also the gate's own blocker, so a
    gate-first order would refuse every resume and strand every draft. It re-runs the
    hook (the entry's checkpoint, pre-set to cycle 5 here, does not stop it: the
    exception to P5-73) and moves nothing. The board itself is proved in
    `test_season_service_draft_branch.py`."""
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.DRAFT)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = 5 WHERE id = $1", league_id
        )
        team = await team_repo.create_team(
            pool,
            league_id=league_id,
            code="PRA",
            name="PRA Team",
            city="PRA City",
            conference="West",
            division="Pacific",
            home_market_size=2,
        )
        await draft_repo.create_draft_pick(
            pool,
            league_id=league_id,
            season=_SEASON,
            round=1,
            original_team_id=team.id,
            current_team_id=team.id,
            pick_number=None,
        )

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert report.kind is PressKind.DRAFT_RESUME
        assert report.to_phase is None
        assert [b.code for b in report.verdict.blockers] == [BlockerCode.DRAFT_NOT_COMPLETE]
        assert [c.phase for c in calls] == [Phase.DRAFT], "the resume ran the hook, once"
        assert await _state(pool, league_id) == (Phase.DRAFT, 5, True)
    finally:
        await _drop(pool, league_id)


async def test_an_unfinished_schedule_phase_is_a_block_press_not_a_blocked_one(pool, monkeypatch):
    """Row 3 of P5-99.b.2 (build-plan commit 10): an unmet games target is ordered
    AHEAD of the gate, so a press here is a Block press that tries to sim -- it was
    a `Blocked` press by the gate's schedule blocker while the block press was
    unbuilt. These two hand-made teams have no strategy rows, so the game cannot
    sim: the press refuses with the failed-game error naming it, and moves nothing."""
    _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.REGULAR_SEASON)
    try:
        home = await team_repo.create_team(
            pool,
            league_id=league_id,
            code="PRH",
            name="PRH Team",
            city="PRH City",
            conference="West",
            division="Pacific",
            home_market_size=2,
        )
        away = await team_repo.create_team(
            pool,
            league_id=league_id,
            code="PRV",
            name="PRV Team",
            city="PRV City",
            conference="West",
            division="Pacific",
            home_market_size=2,
        )
        game = await game_repo.create_game(
            pool,
            league_id=league_id,
            season=_SEASON,
            game_index=0,
            game_date=date(2026, 11, 1),
            home_team_id=home.id,
            away_team_id=away.id,
            game_type=GameType.REGULAR,
            rng_seed=1,
        )

        with pytest.raises(season_service.GamesFailedToSimError) as refused:
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert refused.value.game_ids == (game.id,)
        assert (await _state(pool, league_id))[0] is Phase.REGULAR_SEASON
        assert (await _state(pool, league_id))[2] is True, "the claim is released on a refusal"
    finally:
        await _drop(pool, league_id)


# --- the ladder: claim, sweep, release ----------------------------------------


async def test_the_claim_is_released_after_an_entry_press_and_a_blocked_one(pool, monkeypatch):
    _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.AWARDS)
    try:
        await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert (await _state(pool, league_id))[2] is True
    finally:
        await _drop(pool, league_id)


async def test_the_claim_is_released_when_the_hook_raises_and_the_error_is_not_masked(
    pool, monkeypatch
):
    async def exploding_hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        raise RuntimeError("the AI pass exploded")

    monkeypatch.setattr(gm_service, "run_phase_hook", exploding_hook)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        with pytest.raises(RuntimeError, match="the AI pass exploded"):
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        phase, checkpoint, claim_released = await _state(pool, league_id)
        assert phase is Phase.RESIGN_WINDOW, "tx1 committed before the hook ran"
        assert checkpoint == -1, "a hook that did not finish is not checkpointed"
        assert claim_released
    finally:
        await _drop(pool, league_id)


async def test_a_press_on_a_league_whose_claim_is_held_refuses_and_moves_nothing(
    pool, monkeypatch
):
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        held_at = await season_service._claim_league(pool, league_id, _OTHER_USER)

        with pytest.raises(ConcurrencyError) as refused:
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert str(_OTHER_USER) in str(refused.value)
        assert calls == []
        row = await pool.fetchrow(
            "SELECT current_phase, advance_claimed_at, advance_claimed_by FROM leagues "
            "WHERE id = $1",
            league_id,
        )
        assert row["current_phase"] == Phase.OPTION_DECISIONS.value
        assert (row["advance_claimed_at"], row["advance_claimed_by"]) == (held_at, _OTHER_USER), (
            "a refused press must leave the holder's claim exactly as it was"
        )
    finally:
        await _drop(pool, league_id)


async def test_only_the_commissioner_may_press_and_a_refusal_takes_no_claim(pool, monkeypatch):
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        with pytest.raises(PermissionDenied):
            await press(pool, league_id=league_id, actor_user_id=_OTHER_USER)

        assert calls == []
        assert await _state(pool, league_id) == (Phase.OPTION_DECISIONS, -1, True)
    finally:
        await _drop(pool, league_id)


async def test_the_offer_sweep_runs_after_the_pointer_moved_and_its_count_is_reported(
    pool, monkeypatch
):
    """F4: the sweep compares offers against the league's CURRENT phase, so it
    must run after tx1 commits."""
    _spy_on_the_hook(monkeypatch)
    seen: list[Phase] = []
    real_sweep = offer_repo.expire_stale_offers_for_league

    async def watching_sweep(ex, league_id):
        seen.append((await league_repo.get_league(ex, league_id)).current_phase)
        await real_sweep(ex, league_id)
        return 7

    monkeypatch.setattr(offer_repo, "expire_stale_offers_for_league", watching_sweep)
    league_id = await _league_at(pool, Phase.AWARDS)
    try:
        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert seen == [Phase.DRAFT_LOTTERY]
        assert report.expired_offers == 7
    finally:
        await _drop(pool, league_id)


async def test_the_hook_checkpoint_only_ever_moves_forward(pool):
    """A double press or a stale writer must not move it backwards: the write is
    `... WHERE hook_completed_cycle_index < $2`, so a lower value and the value it
    already holds are both zero-row no-ops."""
    league_id = await _league_at(pool, Phase.SETUP)
    try:
        assert await season_service._record_hook_completed(pool, league_id, 5) is True
        assert await season_service._record_hook_completed(pool, league_id, 3) is False
        assert (await _state(pool, league_id))[1] == 5
        assert await season_service._record_hook_completed(pool, league_id, 5) is False
        assert await season_service._record_hook_completed(pool, league_id, 6) is True
        assert (await _state(pool, league_id))[1] == 6
    finally:
        await _drop(pool, league_id)


# --- the hook is never handed an open transaction (T-D1) ----------------------


async def test_the_hook_is_never_handed_a_connection_in_a_transaction(pool, monkeypatch):
    calls = _spy_on_the_hook(monkeypatch)
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert len(calls) == 2
        assert all(isinstance(c.ex, asyncpg.Pool) for c in calls), (
            "the hook owns its own transactions and is handed the pool (P5-99.c)"
        )
        assert not any(c.in_txn for c in calls)
    finally:
        await _drop(pool, league_id)


async def test_the_open_transaction_guard_trips_on_a_connection_inside_one(pool):
    """The twin of the case above: the guard the press runs before the hook has
    teeth -- a connection with a transaction open is refused, a pool and a
    connection outside any transaction are not."""
    async with transaction(pool) as conn:
        with pytest.raises(ValueError, match="run_phase_hook"):
            season_service._require_no_open_transaction(conn, "run_phase_hook")
    season_service._require_no_open_transaction(pool, "run_phase_hook")
    async with pool.acquire() as bare:
        season_service._require_no_open_transaction(bare, "run_phase_hook")


# --- _move_pointer -------------------------------------------------------------


async def _wait_until_a_statement_waits_on_a_row_lock(pool, *, holder_pid: int) -> None:
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        waiting = await pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND pid <> $1 AND query ILIKE '%FOR UPDATE%'",
            holder_pid,
        )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the pointer move never reached its row lock")


async def test_the_counts_are_taken_under_the_row_lock_so_a_concurrent_entry_is_counted(pool):
    """A `tools/`-style writer holds the `leagues` row, jumps the pointer to
    `RESIGN_WINDOW` and back, and commits while the press's tx1 waits for the
    lock. The press must count what that writer left: counted BEFORE the lock
    the ordinal duplicates the one the concurrent entry took."""
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    holder = await pool.acquire()
    try:
        entries_before = await _history_count(pool, league_id)
        cycle_before = await league_repo.count_phase_entries(
            pool, league_id, _SEASON, season_service.CYCLE_BEARING_PHASES
        )
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT id FROM leagues WHERE id = $1 FOR UPDATE", league_id)
        mover = asyncio.create_task(
            season_service._move_pointer(
                pool,
                league_id,
                expected_phase=Phase.OPTION_DECISIONS,
                expected_season=_SEASON,
                to_phase=Phase.RESIGN_WINDOW,
                actor_user_id=_COMMISSIONER,
            )
        )
        await _wait_until_a_statement_waits_on_a_row_lock(
            pool, holder_pid=holder.get_server_pid()
        )
        await phase_service.force_advance(
            holder, league_id, Phase.RESIGN_WINDOW, _COMMISSIONER, reason="concurrent jump"
        )
        await phase_service.force_advance(
            holder, league_id, Phase.OPTION_DECISIONS, _COMMISSIONER, reason="and back"
        )
        await tx.commit()

        moved = await asyncio.wait_for(mover, timeout=5)

        assert moved.entry_index == entries_before + 2
        assert moved.cycle_ordinal == cycle_before + 1
    finally:
        await pool.release(holder)
        await _drop(pool, league_id)


async def test_a_pointer_move_abandons_when_the_phase_changed_since_the_gate_was_read(pool):
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        history_before = await _history_count(pool, league_id)

        with pytest.raises(ConcurrencyError) as refused:
            await season_service._move_pointer(
                pool,
                league_id,
                expected_phase=Phase.PROGRESSION,
                expected_season=_SEASON,
                to_phase=Phase.OPTION_DECISIONS,
                actor_user_id=_COMMISSIONER,
            )

        assert str(refused.value) == league_claim.LEAGUE_ADVANCED_MESSAGE
        assert await _history_count(pool, league_id) == history_before
        assert (await _state(pool, league_id))[0] is Phase.OPTION_DECISIONS
    finally:
        await _drop(pool, league_id)


async def test_a_pointer_move_that_cannot_get_the_row_fails_as_a_league_noun_concurrency_error(
    pool, monkeypatch
):
    monkeypatch.setattr(roster_lock, "SHARED_LOCK_TIMEOUT", "200ms")
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    holder = await pool.acquire()
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT id FROM leagues WHERE id = $1 FOR UPDATE", league_id)
        try:
            with pytest.raises(ConcurrencyError) as refused:
                await season_service._move_pointer(
                    pool,
                    league_id,
                    expected_phase=Phase.OPTION_DECISIONS,
                    expected_season=_SEASON,
                    to_phase=Phase.RESIGN_WINDOW,
                    actor_user_id=_COMMISSIONER,
                )
        finally:
            await tx.rollback()
        assert str(refused.value) == season_service.CLAIM_CONTENTION_MESSAGE
        assert (await _state(pool, league_id))[0] is Phase.OPTION_DECISIONS
    finally:
        await pool.release(holder)
        await _drop(pool, league_id)
