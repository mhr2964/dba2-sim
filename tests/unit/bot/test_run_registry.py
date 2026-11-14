"""`dba2/bot/run_registry.py` -- the one-run-per-league guard and the bounded shutdown wait
(docs/design/advance-run.md AR-4). Synchronous except `wait_for_all`, which only waits on events.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from discord.ext import commands

from dba2.bot import client as bot_client
from dba2.bot.run_registry import SHUTDOWN_GRACE_SECONDS, RunRegistry
from dba2.domain.phase import Phase

_NOW = datetime(2026, 9, 21, tzinfo=UTC)


def _begin(registry: RunRegistry, league_id: int = 7, user_id: int = 1):
    return registry.begin(
        league_id=league_id, user_id=user_id, started_at=_NOW, phase=Phase.PRESEASON
    )


def test_a_second_run_for_the_same_league_is_refused_and_names_the_holder():
    registry = RunRegistry()
    first = _begin(registry)
    assert first is not None

    assert _begin(registry, user_id=2) is None
    assert registry.current(7) is first and first.user_id == 1


def test_two_leagues_run_independently():
    registry = RunRegistry()
    assert _begin(registry, league_id=7) is not None
    assert _begin(registry, league_id=8) is not None


def test_ending_a_run_frees_the_slot_and_marks_it_finished():
    registry = RunRegistry()
    run = _begin(registry)
    registry.end(run)

    assert registry.current(7) is None and run.finished.is_set()
    assert _begin(registry) is not None


def test_a_stale_end_cannot_evict_a_newer_run():
    registry = RunRegistry()
    old = _begin(registry)
    registry.end(old)
    newer = _begin(registry)

    registry.end(old)  # idempotent, and not the slot's holder any more

    assert registry.current(7) is newer and not newer.finished.is_set()


def test_request_stop_sets_the_cancel_flag_of_that_leagues_run_only():
    registry = RunRegistry()
    mine = _begin(registry, league_id=7)
    other = _begin(registry, league_id=8)

    assert registry.request_stop(7) is mine
    assert mine.cancel.is_set() and not other.cancel.is_set()
    assert registry.request_stop(99) is None


def test_cancel_all_flags_every_run_and_counts_them():
    registry = RunRegistry()
    runs = [_begin(registry, league_id=i) for i in (1, 2, 3)]

    assert registry.cancel_all() == 3
    assert all(run.cancel.is_set() for run in runs)
    assert RunRegistry().cancel_all() == 0


async def test_wait_for_all_returns_true_at_once_when_nothing_is_running():
    assert await RunRegistry().wait_for_all(0.01) is True


async def test_wait_for_all_waits_for_a_run_that_ends_inside_the_grace():
    registry = RunRegistry()
    run = _begin(registry)

    async def finish_soon() -> None:
        await asyncio.sleep(0.02)
        registry.end(run)

    finisher = asyncio.create_task(finish_soon())
    assert await registry.wait_for_all(1.0) is True
    await finisher


async def test_wait_for_all_gives_up_after_the_grace_and_does_not_cancel_the_run():
    registry = RunRegistry()
    run = _begin(registry)

    assert await registry.wait_for_all(0.02) is False
    assert not run.finished.is_set() and registry.current(7) is run


def test_the_shutdown_grace_is_about_one_press_worth():
    assert SHUTDOWN_GRACE_SECONDS == 4.0


# --- client.close awaits the runs before it closes the connection -------------------------


async def test_close_flags_every_run_and_awaits_it_before_the_connection_closes(monkeypatch):
    order: list[str] = []

    async def fake_super_close(self) -> None:
        order.append("super().close")

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    bot = bot_client.build_bot()
    run = bot.runs.begin(league_id=7, user_id=1, started_at=_NOW)

    async def the_run() -> None:
        await run.cancel.wait()  # the run reads its flag between presses
        order.append("run saw the cancel")
        await asyncio.sleep(0.02)  # the press in flight finishes
        order.append("run ended")
        bot.runs.end(run)

    task = asyncio.create_task(the_run())
    await bot.close()
    await task

    assert order == ["run saw the cancel", "run ended", "super().close"]


async def test_close_still_closes_when_a_run_outlives_the_grace(monkeypatch, caplog):
    closed: list[bool] = []

    async def fake_super_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    monkeypatch.setattr(bot_client, "SHUTDOWN_GRACE_SECONDS", 0.02)
    bot = bot_client.build_bot()
    bot.runs.begin(league_id=7, user_id=1, started_at=_NOW)  # never ends

    await bot.close()

    assert closed == [True]
    assert any("outlived" in r.getMessage() for r in caplog.records)


async def test_close_with_no_run_going_does_not_wait(monkeypatch):
    async def fake_super_close(self) -> None:
        return None

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    bot = bot_client.build_bot()
    waited: list[float] = []

    async def spy(grace: float = 0.0) -> bool:
        waited.append(grace)
        return True

    monkeypatch.setattr(bot.runs, "wait_for_all", spy)
    await bot.close()

    assert waited == []


def test_the_bot_carries_a_run_registry_beside_the_block_registry():
    bot = bot_client.build_bot()
    assert isinstance(bot.runs, RunRegistry)
