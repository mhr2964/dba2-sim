"""Live I/O proof for `dba2/services/roster_lock.py` -- RMLP build step 6.
§7.2 tests 14 (nested timeout non-overwrite) and 15 (league-mismatch
assertion), plus the runtime assertions `open_league_gate`/`acquire`
document as their own contract (Connection-required, re-entry, gate-held,
lock_timeout-set).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.core.errors import NotFoundError, TransactionError, ValidationError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo, player_repo, team_repo
from dba2.domain.player import Position, RosterStatus
from dba2.services import roster_lock
from dba2.services.roster_lock import LockMode


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Roster Lock Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
    )
    return team.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Lock",
        last_name="Test",
        primary_position=Position.PG,
        height_in=74,
        weight_lb=185,
        wingspan_in=76,
        birth_date=date(2000, 1, 1),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_open_league_gate_requires_a_connection_not_a_pool():
    pool = await get_pool()
    try:
        with pytest.raises(TransactionError):
            await roster_lock.open_league_gate(pool, league_id=1, mode=LockMode.SHARED)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_requires_a_connection_not_a_pool():
    pool = await get_pool()
    try:
        with pytest.raises(TransactionError):
            await roster_lock.acquire(pool, league_id=1, teams=[], players=[])
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_raises_when_no_gate_is_held():
    """The documentary precondition made runtime, per Decision 2/3 --
    calling `acquire` with no `open_league_gate` call anywhere on this
    backend/league must fail loudly, not silently run unprotected."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                with pytest.raises(TransactionError):
                    await roster_lock.acquire(conn, league_id=league_id, teams=[], players=[])
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_open_league_gate_raises_on_re_entry():
    """F9's anti-pattern, caught at runtime: calling `open_league_gate`
    twice on the same backend/league (whether a literal second call, or a
    nested-transaction re-open) must raise `TransactionError` naming the
    rule, not silently re-acquire (which the underlying advisory lock call
    itself would do without complaint -- Postgres advisory locks are
    re-entrant by design; this assertion is RMLP's own extra guard)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                await roster_lock.open_league_gate(conn, league_id=league_id, mode=LockMode.SHARED)
                with pytest.raises(TransactionError):
                    await roster_lock.open_league_gate(
                        conn, league_id=league_id, mode=LockMode.SHARED
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_raises_when_lock_timeout_is_unset():
    """The loud version of F7's pool-starvation cascade: a transaction
    that holds the RMLP gate (bypassing `open_league_gate`, taken directly
    here to isolate this one precondition) but never had `lock_timeout`
    set must be rejected by `acquire`, not allowed to wait forever."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                # Take the gate directly (bypassing open_league_gate, which
                # would also set lock_timeout) to isolate this precondition.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock_shared($1, $2)",
                    roster_lock.RMLP_LOCK_NAMESPACE,
                    league_id,
                )
                with pytest.raises(TransactionError):
                    await roster_lock.acquire(conn, league_id=league_id, teams=[], players=[])
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_locks_teams_and_players_and_returns_them():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "LK1")
            player_id = await _make_player(pool, league_id)
            async with transaction(pool) as conn:
                await roster_lock.open_league_gate(conn, league_id=league_id, mode=LockMode.SHARED)
                lock_set = await roster_lock.acquire(
                    conn, league_id=league_id, teams=[team_id], players=[player_id]
                )
                assert lock_set.teams_by_id[team_id].id == team_id
                assert lock_set.players_by_id[player_id].id == player_id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_raises_not_found_for_a_missing_team_or_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                await roster_lock.open_league_gate(conn, league_id=league_id, mode=LockMode.SHARED)
                with pytest.raises(NotFoundError):
                    await roster_lock.acquire(
                        conn, league_id=league_id, teams=[999_999_999], players=[]
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_acquire_raises_before_any_write_on_a_league_mismatch():
    """RMLP §7.2 test 15 (Decision 4c). A `league_id` that disagrees with a
    locked team's ACTUAL `league_id` must raise immediately -- the guard
    against `apply_trade`/`apply_signing` taking `league_id` as a keyword
    separate from the ids their execution dataclasses carry, with nothing
    else cross-checking they agree."""
    pool = await get_pool()
    try:
        league_a = await _make_league(pool)
        league_b = await _make_league(pool)
        try:
            team_in_a = await _make_team(pool, league_a, "MA1")
            async with transaction(pool) as conn:
                await roster_lock.open_league_gate(conn, league_id=league_b, mode=LockMode.SHARED)
                with pytest.raises(ValidationError):
                    # Claiming league_b while the team actually belongs to
                    # league_a.
                    await roster_lock.acquire(
                        conn, league_id=league_b, teams=[team_in_a], players=[]
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_a)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_b)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_nested_acquire_calls_never_overwrite_the_outer_lock_timeout():
    """RMLP §7.2 test 14 (Decision 3). `open_league_gate` sets `lock_
    timeout` exactly once, at the outermost level; several nested `acquire`
    calls (each running inside its own savepoint, via `transaction(conn)`)
    must never touch it -- `current_setting('lock_timeout')` must still
    read the ORIGINAL value after every nested call returns."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "NT1")
            player_id = await _make_player(pool, league_id)
            async with transaction(pool) as conn:
                gate = await roster_lock.open_league_gate(
                    conn, league_id=league_id, mode=LockMode.EXCLUSIVE
                )
                assert gate.mode is LockMode.EXCLUSIVE
                before = await conn.fetchval("SELECT current_setting('lock_timeout')")
                assert before == "1min"

                for _ in range(3):
                    async with transaction(conn) as nested_conn:
                        await roster_lock.acquire(
                            nested_conn,
                            league_id=league_id,
                            teams=[team_id],
                            players=[player_id],
                        )
                        mid = await nested_conn.fetchval(
                            "SELECT current_setting('lock_timeout')"
                        )
                        assert mid == "1min"

                after = await conn.fetchval("SELECT current_setting('lock_timeout')")
                assert after == "1min"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_open_league_gate_shared_and_exclusive_use_different_timeouts():
    pool = await get_pool()
    try:
        league_id_a = await _make_league(pool)
        league_id_b = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                gate = await roster_lock.open_league_gate(
                    conn, league_id=league_id_a, mode=LockMode.SHARED
                )
                assert gate.mode is LockMode.SHARED
                value = await conn.fetchval("SELECT current_setting('lock_timeout')")
                assert value == "3s"

            async with transaction(pool) as conn:
                gate = await roster_lock.open_league_gate(
                    conn, league_id=league_id_b, mode=LockMode.EXCLUSIVE
                )
                assert gate.mode is LockMode.EXCLUSIVE
                value = await conn.fetchval("SELECT current_setting('lock_timeout')")
                assert value == "1min"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id_a)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id_b)
    finally:
        await close_pool()
