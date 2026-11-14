"""Live I/O proof for `phase/service.py::advance`/`force_advance` — per the
prompt's verification requirement #4: don't just unit-test the graph logic
in isolation, prove the `phase_history`/`leagues.current_phase` write path
against real Postgres once.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py / test_transaction_guard.py.
"""
from __future__ import annotations

import asyncio
import random

import pytest

from dba2.core.errors import PhaseError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.domain.phase import Phase
from dba2.phase.service import advance, force_advance, require_phase


async def _make_league(pool) -> int:
    return await pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             current_phase, commissioner_user_id, rng_seed)
        VALUES ($1, 'Phase Service Test League', 2026, 2026, 'SETUP', 1, 42)
        RETURNING id
        """,
        # discord_guild_id is UNIQUE — randomize per test run to avoid collisions
        # across repeated local runs against the same scratch DB.
        random.randint(10_000_000, 2_000_000_000),
    )


@pytest.mark.asyncio
async def test_advance_writes_phase_history_and_updates_leagues():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await advance(pool, league_id, Phase.PRESEASON, actor_user_id=7)

            league_row = await pool.fetchrow(
                "SELECT current_phase, phase_entered_at FROM leagues WHERE id = $1",
                league_id,
            )
            assert league_row["current_phase"] == "PRESEASON"

            history_row = await pool.fetchrow(
                """
                SELECT from_phase, to_phase, actor_user_id, forced, reason
                FROM phase_history
                WHERE league_id = $1
                """,
                league_id,
            )
            assert history_row["from_phase"] == "SETUP"
            assert history_row["to_phase"] == "PRESEASON"
            assert history_row["actor_user_id"] == 7
            assert history_row["forced"] is False
            assert history_row["reason"] is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_advance_rejects_illegal_transition_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            with pytest.raises(PhaseError):
                # SETUP's legal targets are {PRESEASON, REGULAR_SEASON} only.
                await advance(pool, league_id, Phase.DRAFT, actor_user_id=7)

            league_row = await pool.fetchrow(
                "SELECT current_phase FROM leagues WHERE id = $1", league_id
            )
            assert league_row["current_phase"] == "SETUP"

            history_count = await pool.fetchval(
                "SELECT COUNT(*) FROM phase_history WHERE league_id = $1", league_id
            )
            assert history_count == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_advance_unknown_league_raises_validation_error():
    pool = await get_pool()
    try:
        with pytest.raises(ValidationError):
            await advance(pool, 999_999_999, Phase.PRESEASON, actor_user_id=7)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_force_advance_bypasses_graph_and_flags_forced_row():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            # DRAFT is not a legal next phase from SETUP — force_advance
            # must succeed anyway.
            await force_advance(
                pool,
                league_id,
                Phase.DRAFT,
                actor_user_id=9,
                reason="commissioner manual rollover for testing",
            )

            league_row = await pool.fetchrow(
                "SELECT current_phase FROM leagues WHERE id = $1", league_id
            )
            assert league_row["current_phase"] == "DRAFT"

            history_row = await pool.fetchrow(
                "SELECT forced, reason FROM phase_history WHERE league_id = $1", league_id
            )
            assert history_row["forced"] is True
            assert history_row["reason"] == "commissioner manual rollover for testing"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_force_advance_requires_non_empty_reason():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            with pytest.raises(ValidationError):
                await force_advance(pool, league_id, Phase.DRAFT, actor_user_id=9, reason="  ")

            history_count = await pool.fetchval(
                "SELECT COUNT(*) FROM phase_history WHERE league_id = $1", league_id
            )
            assert history_count == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_advance_concurrent_calls_only_one_succeeds():
    """Regression for the `SELECT ... FOR UPDATE` row lock in `advance()`.

    Without that lock, N concurrent `advance()` calls targeting the same
    phase can all read the league's pre-transition `current_phase`
    before any of them writes, all pass `is_legal_transition` against
    that same stale value, and all write a `phase_history` row — N
    duplicate transitions logged as if each were independently legal,
    instead of 1 success + (N-1) `PhaseError`s. The row lock serializes
    the read-check-write critical section per league row: only the
    first caller to acquire it can check legality against the
    pre-transition phase; every later caller blocks until that
    transaction commits, then re-reads the *already-advanced* phase and
    correctly fails `is_legal_transition` (PRESEASON -> PRESEASON is not
    an edge in `NEXT_PHASES`).
    """
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            # Tie n to the pool's actual configured max_size instead of a
            # bare literal — if max_size is ever reconfigured below 5, a
            # hardcoded n=5 would silently stop being able to open n real
            # connections (pool.acquire() for the nth caller would just
            # queue behind the others instead of establishing a new
            # connection), which quietly defeats the whole point of the
            # pre-warm below without the test ever failing loudly.
            n = min(5, pool.get_max_size())
            assert n >= 2, (
                f"pool max_size ({pool.get_max_size()}) is too small to "
                "exercise this concurrency test meaningfully — need at "
                "least 2 real connections to race."
            )

            # Force n physically-established connections to exist in the
            # pool *before* the race starts. Without this, only the one
            # connection _make_league() already warmed is instant; the
            # other (n - 1) callers spend most of their time in cold TCP
            # connection setup, which staggers their SELECTs apart by far
            # more than the query round-trip itself and hides the race
            # entirely — the first caller finishes its whole transaction
            # before the others even reach their SELECT, so the test
            # would pass identically whether the FOR UPDATE lock is
            # present or not. Acquiring all n up front (before releasing
            # any) forces the pool to open that many real connections;
            # releasing them back afterward makes them available for
            # advance()'s own acquire() calls below without reconnecting.
            warm_conns = await asyncio.gather(*(pool.acquire() for _ in range(n)))
            for conn in warm_conns:
                await pool.release(conn)

            results = await asyncio.gather(
                *(
                    advance(pool, league_id, Phase.PRESEASON, actor_user_id=i)
                    for i in range(n)
                ),
                return_exceptions=True,
            )

            successes = [r for r in results if r is None]
            phase_errors = [r for r in results if isinstance(r, PhaseError)]
            unexpected = [
                r for r in results if r is not None and not isinstance(r, PhaseError)
            ]

            assert unexpected == [], f"unexpected result/exception types: {unexpected}"
            assert len(successes) == 1, f"expected exactly 1 success, got {len(successes)}"
            assert len(phase_errors) == n - 1, (
                f"expected {n - 1} PhaseErrors, got {len(phase_errors)}"
            )

            league_row = await pool.fetchrow(
                "SELECT current_phase FROM leagues WHERE id = $1", league_id
            )
            assert league_row["current_phase"] == "PRESEASON"

            history_count = await pool.fetchval(
                """
                SELECT COUNT(*) FROM phase_history
                WHERE league_id = $1 AND to_phase = $2
                """,
                league_id,
                Phase.PRESEASON.value,
            )
            assert history_count == 1, (
                f"expected exactly 1 phase_history row for this transition, "
                f"got {history_count} (concurrent callers raced past the lock)"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_force_advance_concurrent_calls_serialize_from_phase():
    """Regression for the `SELECT ... FOR UPDATE` row lock in `force_advance()`.

    `force_advance()` has no legality check to race past — every concurrent
    call succeeds regardless of the lock, so this can't reuse the
    1-success/(N-1)-`PhaseError` split
    `test_advance_concurrent_calls_only_one_succeeds` asserts. But the lock
    still enforces a deterministic, order-independent invariant: N
    concurrent `force_advance()` calls targeting the same `to_phase` are
    still serialized one at a time through the read-check-write critical
    section, so only the caller that actually runs first observes the
    league's real pre-race `current_phase` in its `SELECT ... FOR UPDATE`;
    every later caller blocks until the previous one commits, then reads
    the *already-updated* `current_phase` — which, since every caller
    targets the same `to_phase`, is now that same `to_phase` value. So
    exactly 1 of the N resulting `phase_history` rows must have
    `from_phase` equal to the league's real starting phase (whichever
    caller happened to win the race and run first — no control over which
    one is needed), and the other N-1 must have `from_phase` equal to
    `to_phase` itself, proof that they ran only after an earlier racer had
    already written it. Without the lock, nothing blocks the later
    callers: all N reads land on the same still-committed pre-race
    `current_phase`, so all N rows show that stale starting phase and 0
    show `to_phase` instead — this test fails deterministically in that
    case, not flakily.
    """
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            # See test_advance_concurrent_calls_only_one_succeeds for why n
            # is tied to the pool's max_size and why these connections must
            # be forced open before the race starts.
            n = min(5, pool.get_max_size())
            assert n >= 2, (
                f"pool max_size ({pool.get_max_size()}) is too small to "
                "exercise this concurrency test meaningfully — need at "
                "least 2 real connections to race."
            )

            warm_conns = await asyncio.gather(*(pool.acquire() for _ in range(n)))
            for conn in warm_conns:
                await pool.release(conn)

            starting_phase = Phase.SETUP  # _make_league()'s initial current_phase
            to_phase = Phase.DRAFT

            results = await asyncio.gather(
                *(
                    force_advance(
                        pool,
                        league_id,
                        to_phase,
                        actor_user_id=i,
                        reason="concurrent force advance for testing",
                    )
                    for i in range(n)
                ),
                return_exceptions=True,
            )

            unexpected = [r for r in results if r is not None]
            assert unexpected == [], f"unexpected result/exception types: {unexpected}"

            league_row = await pool.fetchrow(
                "SELECT current_phase FROM leagues WHERE id = $1", league_id
            )
            assert league_row["current_phase"] == to_phase.value

            history_rows = await pool.fetch(
                "SELECT from_phase FROM phase_history WHERE league_id = $1", league_id
            )
            from_phases = [r["from_phase"] for r in history_rows]
            assert len(from_phases) == n, (
                f"expected {n} phase_history rows (force_advance never "
                f"rejects), got {len(from_phases)}: {from_phases}"
            )

            starting_count = from_phases.count(starting_phase.value)
            assert starting_count == 1, (
                f"expected exactly 1 row with from_phase == "
                f"{starting_phase.value!r} (the real pre-race phase, "
                f"observed only by whichever caller actually ran first) — "
                f"got {starting_count}: {from_phases}"
            )

            already_advanced_count = from_phases.count(to_phase.value)
            assert already_advanced_count == n - 1, (
                f"expected {n - 1} rows with from_phase == "
                f"{to_phase.value!r} (callers that ran only after an "
                f"earlier racer had already written it) — got "
                f"{already_advanced_count}: {from_phases}"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


def test_require_phase_raises_on_disallowed_phase():
    with pytest.raises(PhaseError):
        require_phase(Phase.DRAFT, Phase.SETUP, Phase.PRESEASON)


def test_require_phase_passes_on_allowed_phase():
    require_phase(Phase.PRESEASON, Phase.SETUP, Phase.PRESEASON)
