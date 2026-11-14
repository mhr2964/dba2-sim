"""Live I/O proof for Stage E's claim writer and its fenced release
(phase-5-stage-e-advance.md P5-99.c, build-plan commit 3).

Until this commit nothing under `dba2/` ever wrote `leagues.advance_claimed_at`:
every guard that reads it (`league_claim.check_claim`) was proved only against
hand-written `UPDATE` statements. These drive the real writer, and the one
property the release exists for -- a press must never clear a claim that is no
longer its own.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import pytest

from dba2.core.errors import ConcurrencyError, NotFoundError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.services import roster_lock, season_service

_SEASON = 2026
_PRESSER = 41
_OTHER_PRESSER = 42


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture
async def league_id(pool):
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Claim Writer Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    try:
        yield league.id
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def _claim_columns(pool, league_id: int):
    row = await pool.fetchrow(
        "SELECT advance_claimed_at, advance_claimed_by FROM leagues WHERE id = $1", league_id
    )
    return row["advance_claimed_at"], row["advance_claimed_by"]


async def test_claim_writes_both_columns_and_returns_the_fence_timestamp(pool, league_id):
    claimed_at = await season_service._claim_league(pool, league_id, _PRESSER)
    assert await _claim_columns(pool, league_id) == (claimed_at, _PRESSER)


async def test_claiming_twice_affects_zero_rows_and_names_the_holder(pool, league_id):
    first_at = await season_service._claim_league(pool, league_id, _PRESSER)

    assert await league_repo.claim_advance(pool, league_id, _OTHER_PRESSER) is None
    with pytest.raises(ConcurrencyError) as refused:
        await season_service._claim_league(pool, league_id, _OTHER_PRESSER)

    message = str(refused.value)
    assert str(_PRESSER) in message, "the refusal must name who holds the claim"
    assert "/advance-unlock" in message, "every claim refusal names the way out (R4)"
    assert await _claim_columns(pool, league_id) == (first_at, _PRESSER), (
        "a refused claim must leave the holder's claim exactly as it was"
    )


async def test_claiming_a_league_that_does_not_exist_is_not_found_not_a_held_claim(pool):
    with pytest.raises(NotFoundError):
        await season_service._claim_league(pool, 2_147_000_000, _PRESSER)


async def test_release_clears_the_claim_it_holds(pool, league_id):
    claimed_at = await season_service._claim_league(pool, league_id, _PRESSER)
    assert await season_service._release_claim(pool, league_id, _PRESSER, claimed_at) is True
    assert await _claim_columns(pool, league_id) == (None, None)
    # ...and the league is claimable again.
    await season_service._claim_league(pool, league_id, _OTHER_PRESSER)


async def test_a_stale_timestamp_release_affects_zero_rows_and_keeps_the_new_claim(
    pool, league_id, caplog
):
    """Press A's claim is broken by `/advance-unlock`; press B (the same
    commissioner pressing again) claims; A finishes and releases with ITS
    timestamp. Without the timestamp in the fence, A clears B's live claim and
    a second press can start while B is mid-block."""
    a_at = await season_service._claim_league(pool, league_id, _PRESSER)
    await pool.execute(
        "UPDATE leagues SET advance_claimed_at = NULL, advance_claimed_by = NULL WHERE id = $1",
        league_id,
    )
    b_at = await season_service._claim_league(pool, league_id, _PRESSER)
    assert b_at != a_at

    with caplog.at_level(logging.WARNING, logger=season_service.__name__):
        released = await season_service._release_claim(pool, league_id, _PRESSER, a_at)

    assert released is False
    assert await _claim_columns(pool, league_id) == (b_at, _PRESSER)
    assert any(
        "no longer" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records
    ), "a fenced-out release is logged, never silent"


async def test_a_release_by_another_claimant_affects_zero_rows(pool, league_id):
    claimed_at = await season_service._claim_league(pool, league_id, _PRESSER)
    assert (
        await season_service._release_claim(pool, league_id, _OTHER_PRESSER, claimed_at) is False
    )
    assert await _claim_columns(pool, league_id) == (claimed_at, _PRESSER)


async def test_a_release_that_raises_is_logged_and_never_replaces_the_presses_exception(
    pool, league_id, monkeypatch, caplog
):
    """The release runs in the press's `finally`. If it raised, its exception
    would replace the press's own -- the operator would read a failed release
    instead of whatever actually went wrong."""
    claimed_at = await season_service._claim_league(pool, league_id, _PRESSER)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("release exploded")

    monkeypatch.setattr(league_repo, "release_advance", _boom)

    class PressFailed(Exception):
        pass

    with caplog.at_level(logging.ERROR, logger=season_service.__name__):
        with pytest.raises(PressFailed):
            try:
                raise PressFailed("the press's own failure")
            finally:
                await season_service._release_claim(pool, league_id, _PRESSER, claimed_at)

    assert any("release exploded" in (r.exc_text or "") for r in caplog.records), (
        "the swallowed release failure must still be logged with its traceback"
    )
    # The claim was not released, so it is still there for `/advance-unlock`.
    assert await _claim_columns(pool, league_id) == (claimed_at, _PRESSER)


async def test_a_claim_waits_behind_a_held_row_lock_then_fails_as_a_league_noun_error(
    pool, league_id, monkeypatch
):
    """The statement `phase/service.advance` never had: a bounded wait. A human
    write holds `leagues ... FOR SHARE`; the claim must block on it, then fail at
    the timeout as a designed `ConcurrencyError`, not a raw asyncpg 55P03 -- and
    write nothing."""
    monkeypatch.setattr(roster_lock, "SHARED_LOCK_TIMEOUT", "300ms")
    held, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with transaction(pool) as conn:
            await conn.fetchrow("SELECT id FROM leagues WHERE id = $1 FOR SHARE", league_id)
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    try:
        started = time.perf_counter()
        with pytest.raises(ConcurrencyError) as refused:
            await season_service._claim_league(pool, league_id, _PRESSER)
        waited = time.perf_counter() - started
    finally:
        release.set()
        await task

    assert waited >= 0.25, f"the claim did not wait for the lock (returned in {waited:.3f}s)"
    assert "league" in str(refused.value).lower()
    assert await _claim_columns(pool, league_id) == (None, None)


async def test_a_release_that_times_out_on_a_held_lock_is_logged_and_leaves_the_claim(
    pool, league_id, monkeypatch, caplog
):
    claimed_at = await season_service._claim_league(pool, league_id, _PRESSER)
    monkeypatch.setattr(roster_lock, "SHARED_LOCK_TIMEOUT", "200ms")
    held, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with transaction(pool) as conn:
            await conn.fetchrow("SELECT id FROM leagues WHERE id = $1 FOR SHARE", league_id)
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    try:
        with caplog.at_level(logging.ERROR, logger=season_service.__name__):
            released = await season_service._release_claim(pool, league_id, _PRESSER, claimed_at)
    finally:
        release.set()
        await task

    assert released is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert await _claim_columns(pool, league_id) == (claimed_at, _PRESSER)


# --- the press's release survives cancellation (critic m1) ---------------------------------
#
# `press` releases in a `finally`. `_release_claim` catches `Exception`, and a
# `CancelledError` is not one: a task cancelled while the release was in flight lost the
# release (its transaction rolled back, the claim stayed for /advance-unlock) AND, because
# the exception raised out of a `finally`, replaced the press's own exception with the
# cancellation. The release is therefore shielded, and a cancellation that lands during it
# cannot replace an exception the press is already raising.


class _PressFailed(Exception):
    pass


async def _wait_for_claim_cleared(pool, league_id: int, *, seconds: float = 3.0) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        if await _claim_columns(pool, league_id) == (None, None):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"the claim was never released: {await _claim_columns(pool, league_id)}")


def _slow_release(monkeypatch, in_release: asyncio.Event, delay: float = 0.3) -> None:
    real = league_repo.release_advance

    async def slow(ex, league_id, claimed_by, claimed_at):
        in_release.set()
        await asyncio.sleep(delay)
        return await real(ex, league_id, claimed_by, claimed_at)

    monkeypatch.setattr(league_repo, "release_advance", slow)


async def test_a_cancel_during_the_release_keeps_the_presses_exception_and_the_release(
    pool, league_id, monkeypatch
):
    async def failing_press(*_args, **_kwargs):
        raise _PressFailed("the press's own failure")

    monkeypatch.setattr(season_service, "_press_claimed", failing_press)
    in_release = asyncio.Event()
    _slow_release(monkeypatch, in_release)

    task = asyncio.create_task(
        season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
    )
    await asyncio.wait_for(in_release.wait(), timeout=5)
    task.cancel()

    with pytest.raises(_PressFailed):
        await task
    await _wait_for_claim_cleared(pool, league_id)


async def test_a_cancel_during_the_release_of_a_finished_press_still_cancels_and_releases(
    pool, league_id, monkeypatch
):
    """Nothing to preserve here: the press returned. The cancellation propagates as what
    it is, and the shielded release still completes."""

    async def finished_press(*_args, **_kwargs):
        return "a report"

    monkeypatch.setattr(season_service, "_press_claimed", finished_press)
    in_release = asyncio.Event()
    _slow_release(monkeypatch, in_release)

    task = asyncio.create_task(
        season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
    )
    await asyncio.wait_for(in_release.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_claim_cleared(pool, league_id)


async def test_a_press_cancelled_mid_work_releases_its_claim_and_stays_cancelled(
    pool, league_id, monkeypatch
):
    started = asyncio.Event()

    async def stuck_press(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(season_service, "_press_claimed", stuck_press)

    task = asyncio.create_task(
        season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await _claim_columns(pool, league_id) != (None, None), "the press holds the claim"
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_claim_cleared(pool, league_id)


# --- an absorbed cancellation is un-counted; the release task is strongly held -------------
#
# `press` and `_run_under_claim` absorb a `CancelledError` that lands while the release is being
# awaited, so the body's own exception can propagate. Absorbing a cancellation without
# `Task.uncancel()` leaves the task's cancel count raised, and an `asyncio.timeout()` or a
# `TaskGroup` further up reads that count to decide whether IT was the cancel's cause. A
# shielded release task must also be strongly referenced: the event loop keeps only a weak
# reference to a task, so an unreferenced one can be collected mid-flight.
#
# The limit that remains, stated where it is tested: all of this needs a SURVIVING loop. A
# shutdown-cancel (the loop closing) destroys the pending release with it and the claim
# stays until `/advance-unlock`.


async def _run_body_failing(kind: str, pool, league_id: int, monkeypatch):
    """Runs the claim-taking entry point named by `kind` with a body that raises
    `_PressFailed`, and a slow release."""

    async def failing_body(*_args, **_kwargs):
        raise _PressFailed("the body's own failure")

    if kind == "press":
        monkeypatch.setattr(season_service, "_press_claimed", failing_body)
        return season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
    monkeypatch.setattr(season_service, "_advance_to_claimed", failing_body)
    return season_service.advance_to(
        pool, league_id=league_id, actor_user_id=1, to_phase=Phase.PLAYIN
    )


@pytest.mark.parametrize("kind", ["press", "advance_to"])
async def test_an_absorbed_cancel_is_uncancelled_so_the_bodys_exception_arrives_uncounted(
    pool, league_id, monkeypatch, kind
):
    in_release = asyncio.Event()
    _slow_release(monkeypatch, in_release)
    coroutine = await _run_body_failing(kind, pool, league_id, monkeypatch)

    async def caller() -> int:
        try:
            await coroutine
        except _PressFailed:
            task = asyncio.current_task()
            assert task is not None
            return task.cancelling()
        raise AssertionError("the body's exception did not arrive")

    task = asyncio.create_task(caller())
    await asyncio.wait_for(in_release.wait(), timeout=5)
    task.cancel()

    assert await task == 0, "the absorbed cancel request must be un-counted"
    await _wait_for_claim_cleared(pool, league_id)


async def test_a_body_that_was_itself_cancelled_keeps_its_one_cancel_request_counted(
    pool, league_id, monkeypatch
):
    """The other half: when the ORIGINAL exception is the cancellation, a second cancel that
    lands during the release is absorbed (one uncancel) and the first request stays counted --
    the task ends cancelled, exactly once."""
    started = asyncio.Event()
    in_release = asyncio.Event()
    _slow_release(monkeypatch, in_release)

    async def stuck_press(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(season_service, "_press_claimed", stuck_press)
    seen: list[int] = []

    async def caller() -> None:
        try:
            await season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            assert task is not None
            seen.append(task.cancelling())
            raise

    task = asyncio.create_task(caller())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    await asyncio.wait_for(in_release.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [1]
    await _wait_for_claim_cleared(pool, league_id)


async def test_the_shielded_release_task_is_strongly_held_until_it_finishes(
    pool, league_id, monkeypatch
):
    async def failing_press(*_args, **_kwargs):
        raise _PressFailed("the press's own failure")

    monkeypatch.setattr(season_service, "_press_claimed", failing_press)
    in_release = asyncio.Event()
    _slow_release(monkeypatch, in_release, delay=0.4)

    task = asyncio.create_task(
        season_service.press(pool, league_id=league_id, actor_user_id=1, library=None)
    )
    await asyncio.wait_for(in_release.wait(), timeout=5)
    task.cancel()
    with pytest.raises(_PressFailed):
        await task

    assert len(season_service._PENDING_RELEASES) == 1, "held by a module-level reference"
    await _wait_for_claim_cleared(pool, league_id)
    await asyncio.sleep(0.05)
    assert season_service._PENDING_RELEASES == set(), "discarded by its own done-callback"
