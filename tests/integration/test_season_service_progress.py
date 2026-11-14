"""`press(on_progress=...)` (phase-5-stage-e-advance.md P5-99.j "Progress", build-plan
commit 14).

`on_progress` is a Discord write reached from inside a service, so it may only be
awaited with NO transaction open (P5-14, M5). The detector used here is the pool itself:
a transaction holds a checked-out connection for its whole life, so "nothing is
checked out of the pool" is exactly "no transaction of ours is open". Its twin awaits the
same check inside `transaction(pool)` and must see a connection checked out -- without
the twin, a detector that could never fire would prove nothing.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

from datetime import date

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.services import gm_service, season_service
from dba2.services.season_service import PressKind, WorkKind
from tests.integration._press_fixtures import (
    SEASON,
    add_game,
    add_played_game,
    add_two_teams,
    bare_league_at,
    drop,
    press,
    sim_league_at,
)


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def stub_hook(monkeypatch):
    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


def _nothing_is_checked_out(pool) -> bool:
    return pool.get_size() == pool.get_idle_size()


class _Recorder:
    """An `on_progress` that records each message and whether a transaction of ours was
    open (a connection checked out of the pool) when it was awaited."""

    def __init__(self, pool) -> None:
        self.pool = pool
        self.messages: list[str] = []
        self.checked_out: list[bool] = []

    async def __call__(self, message: str) -> None:
        self.messages.append(message)
        self.checked_out.append(not _nothing_is_checked_out(self.pool))


async def test_the_detector_sees_a_transaction_that_is_open(pool):
    """The twin: awaited inside a transaction, the same check must report a checked-out
    connection. If this ever fails, the assertions below prove nothing."""
    recorder = _Recorder(pool)
    await recorder("outside")
    async with transaction(pool):
        await recorder("inside")
    assert recorder.checked_out == [False, True]


async def test_an_entry_press_reports_its_milestones_with_no_transaction_open(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.kind is PressKind.ENTRY
        assert len(recorder.messages) >= 2, recorder.messages
        assert "claim" in recorder.messages[0].lower()
        assert any("playin" in m.lower() and "playoffs_r1" in m.lower() for m in recorder.messages)
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)


async def test_a_block_press_reports_the_game_day_it_is_simming(pool):
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.kind is PressKind.BLOCK
        assert report.block is not None
        assert any(report.block.block_id in m for m in recorder.messages), recorder.messages
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)


async def test_a_progress_callback_that_raises_does_not_abort_the_press(pool):
    """Progress is decoration. A Discord edit that fails (an expired interaction, a rate
    limit) must not strand a press whose claim is held and whose work is half done."""
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:

        async def failing(message: str) -> None:
            raise RuntimeError("the edit failed")

        report = await press(pool, league_id=league_id, on_progress=failing)

        assert report.kind is PressKind.ENTRY
        assert report.to_phase is Phase.PLAYOFFS_R1
    finally:
        await drop(pool, league_id)


async def test_a_progress_report_handed_a_connection_in_a_transaction_raises(pool):
    """`_report_progress` is the one place the service awaits the callback, and it carries
    the `_require_no_open_transaction` assertion the spec asks for. The twin of the cases
    above: awaited with a transaction open, it must trip instead of posting."""
    seen: list[str] = []

    async def callback(message: str) -> None:
        seen.append(message)

    async with pool.acquire() as conn:
        async with transaction(conn):
            assert isinstance(conn, asyncpg.Connection) and conn.is_in_transaction()
            with pytest.raises(ValueError, match="on_progress"):
                await season_service._report_progress(conn, callback, "must not be sent")
    assert seen == []


# --- the milestones name work that really runs (critic 3, M2 and minor 2) --------------------


async def _noop_unit(ctx) -> None:
    return None


async def test_the_work_prefix_reports_the_ai_pass_it_finishes_for_the_current_phase(
    pool, monkeypatch
):
    """A press that died before its hook finished leaves the hook owed; the NEXT press's prefix
    runs it before anything else -- a few seconds of AI work the commissioner used to watch in
    silence, because `_press_claimed` did not hand `on_progress` to the prefix."""
    monkeypatch.setitem(season_service._WORK_UNITS, WorkKind.CLOSING, {})
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = -1 WHERE id = $1", league_id
        )
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.resumed_hook_result is not None, "the prefix ran the owed hook"
        assert "Running DRAFT_LOTTERY's AI pass..." in recorder.messages
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)


async def test_the_work_prefix_reports_the_phase_work_it_finishes_for_the_current_phase(
    pool, monkeypatch
):
    monkeypatch.setitem(
        season_service._WORK_UNITS,
        WorkKind.ENTRY,
        {Phase.TRADE_DEADLINE: (_noop_unit,)},
    )
    monkeypatch.setitem(season_service._WORK_UNITS, WorkKind.CLOSING, {})
    league_id = await bare_league_at(pool, Phase.TRADE_DEADLINE)
    try:
        await pool.execute(
            "UPDATE leagues SET entry_work_completed_index = -1 WHERE id = $1", league_id
        )
        recorder = _Recorder(pool)

        await press(pool, league_id=league_id, on_progress=recorder)

        assert "Running TRADE_DEADLINE's phase work..." in recorder.messages
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)


async def test_entering_a_phase_with_no_work_to_run_this_press_announces_none(pool, monkeypatch):
    """`REGULAR_SEASON` reads a pointer (`_needs_pointer`) but owns no entry work, holds its
    hook until a block press (JC-13) and has an unmet target, so nothing runs on the Entry
    press: the old milestone ("phase work and AI pass") was gated on the pointer, not on work,
    and was false here."""
    league_id = await bare_league_at(pool, Phase.PRESEASON)
    try:
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)
        teams = await add_two_teams(pool, league_id)
        await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 10, 20))
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.kind is PressKind.ENTRY and report.to_phase is Phase.REGULAR_SEASON
        assert report.hook_result is None and report.media_cycle_index is None
        assert not any(
            "phase work" in m or "AI pass" in m or "closing work" in m for m in recorder.messages
        ), recorder.messages
        assert any("Advancing" in m for m in recorder.messages)
    finally:
        await drop(pool, league_id)


async def test_entering_a_phase_that_has_work_and_a_hook_announces_both(pool, monkeypatch):
    monkeypatch.setitem(
        season_service._WORK_UNITS,
        WorkKind.ENTRY,
        {Phase.TRADE_DEADLINE: (_noop_unit,)},
    )
    monkeypatch.setitem(season_service._WORK_UNITS, WorkKind.CLOSING, {})
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await add_played_game(pool, league_id)  # the games target is met, so the gate allows
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.kind is PressKind.ENTRY and report.to_phase is Phase.TRADE_DEADLINE
        assert "Running TRADE_DEADLINE's phase work..." in recorder.messages
        assert "Running TRADE_DEADLINE's AI pass..." in recorder.messages
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)


async def test_a_block_press_reports_its_ai_pass_and_its_closing_work(pool):
    """The block press's own hook (JC-13's held trade window) and its closing work (the media
    cycle a block that meets the target runs) are announced like the entry press's."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=2)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = -1 WHERE id = $1", league_id
        )
        recorder = _Recorder(pool)

        report = await press(pool, league_id=league_id, on_progress=recorder)

        assert report.kind is PressKind.BLOCK and report.hook_result is not None
        assert report.media_cycle_index is not None, "the block met the target"
        assert "Running REGULAR_SEASON's AI pass..." in recorder.messages
        assert "Running REGULAR_SEASON's closing work..." in recorder.messages
        assert recorder.checked_out == [False] * len(recorder.messages), recorder.messages
    finally:
        await drop(pool, league_id)
