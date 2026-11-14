"""`/advance-unlock`'s service side (phase-5-stage-e-advance.md P5-99.c, build-plan
commit 13; the cog is commit 15), over real rows.

A crash mid-press leaves `advance_claimed_at` set and freezes the league until somebody
clears it, and clearing a LIVE claim gives two concurrent AI passes -- the exact thing the
claim prevents. So the unlock refuses under an age (10 minutes), is commissioner-only,
writes an audit row of its own kind, and says what `hook_completed_cycle_index` says so
the person knows whether the dead press's hook still needs finishing.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from dba2.core.errors import ConcurrencyError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.domain.transaction import TransactionType
from dba2.phase.permissions import PermissionDenied
from dba2.services import roster_lock, season_service
from tests.integration._press_fixtures import COMMISSIONER, SEASON, bare_league_at, drop

_OTHER = 99


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


async def _claim_aged(pool, league_id: int, *, minutes: int, by: int = _OTHER):
    """Takes the claim as `by`, then backdates it so it is `minutes` old at the database's
    own clock (the age is measured there, not in Python)."""
    await season_service._claim_league(pool, league_id, by)
    await pool.execute(
        "UPDATE leagues SET advance_claimed_at = NOW() - make_interval(mins => $2) WHERE id = $1",
        league_id,
        minutes,
    )
    return (await league_repo.get_league(pool, league_id)).advance_claimed_at


async def _unlock_rows(pool, league_id: int):
    return await pool.fetch(
        "SELECT id, season, phase, txn_type, actor_user_id, note, details "
        "FROM league_transactions WHERE league_id = $1 AND txn_type = $2",
        league_id,
        TransactionType.ADVANCE_UNLOCK.value,
    )


async def test_a_claim_older_than_the_threshold_is_cleared_and_audited(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)  # hook checkpoint is cycle 1
    try:
        claimed_at = await _claim_aged(pool, league_id, minutes=11)

        result = await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert result.cleared is True
        assert result.claimed_by == _OTHER
        assert result.claimed_at == claimed_at
        assert result.age >= timedelta(minutes=11)
        assert result.hook_completed_cycle_index == 1
        assert result.current_phase is Phase.REGULAR_SEASON
        assert "hook_completed_cycle_index is 1" in result.message
        assert f"user {_OTHER}" in result.message
        league = await league_repo.get_league(pool, league_id)
        assert (league.advance_claimed_at, league.advance_claimed_by) == (None, None)

        (row,) = await _unlock_rows(pool, league_id)
        assert result.transaction_id == row["id"]
        assert row["actor_user_id"] == COMMISSIONER
        assert (row["season"], row["phase"]) == (SEASON, Phase.REGULAR_SEASON.value)
        assert row["note"]
        details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"])
        assert details["claimed_by"] == _OTHER
        assert details["hook_completed_cycle_index"] == 1
        assert details["age_seconds"] >= 11 * 60
    finally:
        await drop(pool, league_id)


async def test_a_claim_younger_than_the_threshold_is_refused_and_left_exactly_as_it_was(pool):
    """Nine minutes old is a press that may still be running. Nothing is written: not the
    claim, not an audit row."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        claimed_at = await _claim_aged(pool, league_id, minutes=9)

        with pytest.raises(ConcurrencyError) as refused:
            await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert f"user {_OTHER}" in str(refused.value)
        assert "10 minutes" in str(refused.value)
        league = await league_repo.get_league(pool, league_id)
        assert (league.advance_claimed_at, league.advance_claimed_by) == (claimed_at, _OTHER)
        assert await _unlock_rows(pool, league_id) == []
    finally:
        await drop(pool, league_id)


async def test_a_fresh_claim_is_refused_too(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await season_service._claim_league(pool, league_id, _OTHER)

        with pytest.raises(ConcurrencyError):
            await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (await league_repo.get_league(pool, league_id)).advance_claimed_by == _OTHER
    finally:
        await drop(pool, league_id)


async def test_unlocking_a_league_with_no_claim_changes_nothing_and_writes_no_row(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        result = await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert result.cleared is False
        assert result.transaction_id is None
        assert result.claimed_by is None
        assert "no advance claim" in result.message.lower()
        assert "hook_completed_cycle_index is 1" in result.message
        assert await _unlock_rows(pool, league_id) == []
    finally:
        await drop(pool, league_id)


async def test_only_the_commissioner_may_unlock_and_a_refusal_touches_nothing(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        claimed_at = await _claim_aged(pool, league_id, minutes=30)

        with pytest.raises(PermissionDenied):
            await season_service.unlock(pool, league_id=league_id, actor_user_id=_OTHER)

        league = await league_repo.get_league(pool, league_id)
        assert (league.advance_claimed_at, league.advance_claimed_by) == (claimed_at, _OTHER)
        assert await _unlock_rows(pool, league_id) == []
    finally:
        await drop(pool, league_id)


async def test_the_press_whose_claim_was_broken_cannot_release_the_next_ones(pool):
    """The unlock and the fenced release belong together: after a claim is broken and
    another press takes a new one, the first press's late release must be a no-op."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        old_claim = await _claim_aged(pool, league_id, minutes=15)
        await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        new_claim = await season_service._claim_league(pool, league_id, COMMISSIONER)

        released = await season_service._release_claim(pool, league_id, _OTHER, old_claim)

        assert released is False
        league = await league_repo.get_league(pool, league_id)
        assert (league.advance_claimed_at, league.advance_claimed_by) == (new_claim, COMMISSIONER)
    finally:
        await drop(pool, league_id)


async def test_a_cleared_claim_warns_that_a_press_still_running_can_run_work_twice(pool):
    """The age threshold is a heuristic, not proof the press is dead (R4, accepted): the
    cleared message says what to do if it was wrong -- wait for that press."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _claim_aged(pool, league_id, minutes=11)

        result = await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert "still running" in result.message
        assert "twice" in result.message
    finally:
        await drop(pool, league_id)


async def test_a_refused_young_claim_says_to_wait_for_a_press_that_may_be_running(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _claim_aged(pool, league_id, minutes=3)

        with pytest.raises(ConcurrencyError) as refused:
            await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert "may still be running" in str(refused.value)
    finally:
        await drop(pool, league_id)


async def test_the_minimum_age_can_be_overridden_by_the_caller_not_by_a_command(pool):
    """`min_age` is a service parameter (an operator's script, a test), never a slash-command
    option: a shorter one clears a claim the default would refuse, a longer one refuses a
    claim the default would clear, and the refusal names the threshold it applied."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _claim_aged(pool, league_id, minutes=11)

        with pytest.raises(ConcurrencyError) as refused:
            await season_service.unlock(
                pool,
                league_id=league_id,
                actor_user_id=COMMISSIONER,
                min_age=timedelta(minutes=30),
            )
        assert "30 minutes" in str(refused.value)
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_by == _OTHER

        result = await season_service.unlock(
            pool, league_id=league_id, actor_user_id=COMMISSIONER, min_age=timedelta(minutes=1)
        )
        assert result.cleared is True
    finally:
        await drop(pool, league_id)


async def test_a_zero_minimum_age_clears_a_fresh_claim(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await season_service._claim_league(pool, league_id, _OTHER)

        result = await season_service.unlock(
            pool, league_id=league_id, actor_user_id=COMMISSIONER, min_age=timedelta(0)
        )

        assert result.cleared is True
    finally:
        await drop(pool, league_id)


async def test_an_unlock_that_cannot_get_the_row_lock_says_to_retry_unlock_not_advance(
    pool, monkeypatch
):
    """The lock wait behind another open transaction ends in a `ConcurrencyError` -- and its
    sentence must name the command the person just ran. It used to be the claim
    contention message, which tells them to try `/advance` again."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _claim_aged(pool, league_id, minutes=30)
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
            with pytest.raises(ConcurrencyError) as refused:
                await season_service.unlock(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        finally:
            release.set()
            await task

        message = str(refused.value)
        assert message == season_service.UNLOCK_CONTENTION_MESSAGE
        assert "/advance-unlock" in message
        assert message != season_service.CLAIM_CONTENTION_MESSAGE
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_by == _OTHER
    finally:
        await drop(pool, league_id)
