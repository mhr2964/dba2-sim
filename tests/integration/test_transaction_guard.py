"""Regression coverage for `dba2.data.db.transaction()`'s re-entrancy guard.

This file exists because the guard went through 6 rounds of fix/re-verify
(EFFORT_LEDGER.md, Phase 0) with critic re-finding a live defect on 4 of
those rounds — three times (B4, B5, B6) a regression introduced by the
*previous* round's own fix, all the same root cause: the old
`dict[Task, Connection]` guard used save-on-entry/restore-on-exit
semantics, and restoring a remembered prior value is only correct if that
value is still valid by the time you restore it — which an abandoned,
GC-finalized async generator's late-running `finally` cannot guarantee.

Round 6 replaced that data structure with `dict[Task, set[object]]`: each
`transaction()` call adds a private token to its task's set on entry and
*discards* (never restores) exactly that token on exit, so cleanup never
reads or depends on any other call's state and cannot corrupt it no matter
what order `finally` blocks run in. Every case below is a behavior some
round of that loop established and then had to re-verify by hand against
the scratch Postgres — this file makes that verification automatic instead
of re-derived from scratch (and re-missed) each round. B6 in particular was
a test gap, not just a code gap: `test_b5_*` used to stop asserting inside
the sibling block and never checked state after it exited, which is
exactly where B6 lived — that test now asserts post-exit cleanliness too.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import gc

import asyncpg
import pytest

from dba2.core.config import get_config
from dba2.core.errors import TransactionError
from dba2.data import db
from dba2.data.db import close_pool, get_pool, transaction


@pytest.mark.asyncio
async def test_nested_pool_in_pool_same_task_raises():
    """A `transaction(pool)` call while the current task already has one
    open must raise `TransactionError` instead of silently opening a
    second, unrelated connection (Phase 0 db-engineer round 2, B2).
    """
    pool = await get_pool()
    try:
        async with transaction(pool):
            with pytest.raises(TransactionError):
                async with transaction(pool):
                    pass
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_connection_in_connection_savepoint_isolates():
    """`transaction(conn)` nested inside `transaction(conn)` must use the
    same backend connection (LIFO savepoint nesting, not a second
    top-level transaction) and the inner block's rollback must not affect
    rows the outer block already wrote (Phase 0 db-engineer round 1).
    """
    pool = await get_pool()
    try:
        async with transaction(pool) as conn:
            outer_pid = await conn.fetchval("SELECT pg_backend_pid()")
            await conn.execute("CREATE TEMP TABLE t_savepoint (v int)")
            await conn.execute("INSERT INTO t_savepoint VALUES (1)")

            with pytest.raises(ValueError):
                async with transaction(conn) as inner_conn:
                    inner_pid = await inner_conn.fetchval("SELECT pg_backend_pid()")
                    await inner_conn.execute("INSERT INTO t_savepoint VALUES (2)")
                    raise ValueError("force inner rollback")

            assert inner_pid == outer_pid
            rows = await conn.fetch("SELECT v FROM t_savepoint ORDER BY v")
            assert [r["v"] for r in rows] == [1]
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_task_child_gets_independent_transaction():
    """A task spawned via `asyncio.create_task()` from inside an open
    `transaction()` block is a distinct `Task` and is not covered by the
    parent's re-entrancy guard — its own `transaction(pool)` call must
    succeed on its own connection while the parent's is still open
    (Phase 0 db-engineer round 2, B2 — the guard's original contextvars
    implementation leaked into child tasks and blocked them; fixed by
    keying on `asyncio.current_task()`).
    """
    pool = await get_pool()
    result = {}
    try:
        async def child():
            async with transaction(pool) as conn:
                result["ok"] = await conn.fetchval("SELECT 1") == 1

        async with transaction(pool):
            await asyncio.create_task(child())

        assert result.get("ok") is True
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_fire_and_forget_child_outlives_parent_block():
    """A child task spawned inside a `transaction()` block that is still
    running after the parent block has already exited must still be able
    to open (and complete) its own `transaction(pool)` call.
    """
    pool = await get_pool()
    result = {}
    try:
        async def child():
            async with transaction(pool) as conn:
                result["ok"] = await conn.fetchval("SELECT 1") == 1

        async with transaction(pool):
            t = asyncio.create_task(child())
        # Parent block has closed; child may still be running.
        await t

        assert result.get("ok") is True
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_pool_starvation_raises_transaction_error():
    """A genuine `pool.acquire()` timeout — a different task blocked
    behind a small pool's `max_size` while another task holds the only
    connection — must raise `TransactionError` with a starvation-
    indicating message, not hang forever (Phase 0 db-engineer round 2,
    B2). Uses a dedicated `max_size=1` pool and a shortened acquire
    timeout so the test is fast and deterministic, not the full 10s
    production default.

    Wrapped in `asyncio.wait_for` (Phase 0 critic round 5 nit): if a
    future regression reintroduces the old "hang forever instead of
    raising" behavior, this test fails fast with a `TimeoutError` instead
    of hanging CI indefinitely. The wrapper timeout is generous relative
    to the `db._POOL_ACQUIRE_TIMEOUT_S` used here, so it never fires on
    a passing run.
    """
    cfg = get_config()
    small_pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=1)
    orig_timeout = db._POOL_ACQUIRE_TIMEOUT_S
    db._POOL_ACQUIRE_TIMEOUT_S = 0.5
    outcome: dict[str, object] = {}
    try:
        async def contender():
            # A different task -> the task-scoped re-entrancy guard does
            # not apply here; this must starve on pool.acquire() itself
            # since max_size=1 and the outer task holds the only
            # connection.
            try:
                async with transaction(small_pool):
                    outcome["raised"] = False
            except TransactionError as exc:
                outcome["raised"] = True
                outcome["msg"] = str(exc)

        async def run():
            async with transaction(small_pool):
                await asyncio.create_task(contender())

        await asyncio.wait_for(run(), timeout=5.0)

        assert outcome.get("raised") is True
        assert "starvation" in outcome["msg"] or "Timed out" in outcome["msg"]
    finally:
        db._POOL_ACQUIRE_TIMEOUT_S = orig_timeout
        await small_pool.close()


@pytest.mark.asyncio
async def test_inbody_timeout_error_propagates_unrelabeled():
    """A `TimeoutError`/`asyncio.wait_for` timeout raised from *inside* a
    transaction body must propagate as-is, not get misreported as pool-
    acquire starvation (Phase 0 critic round 2, B1 — `except TimeoutError`
    originally wrapped the whole body including the yield, since
    `asyncio.TimeoutError is TimeoutError` on 3.11+; fixed in round 3 by
    scoping the try/except to only the manual `pool.acquire()` call).
    """
    pool = await get_pool()
    try:
        with pytest.raises(TimeoutError) as exc_info:
            async with transaction(pool):
                await asyncio.wait_for(asyncio.sleep(1), timeout=0.05)
        assert not isinstance(exc_info.value, TransactionError)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_b4_late_gc_generator_does_not_poison_task():
    """B4 (Phase 0 critic round 3): an async generator holding a nested
    `transaction(conn)`, abandoned (never fully consumed) and finalized by
    GC only after its enclosing `transaction(pool)` block has already
    exited, must not leave a stale `_active_conn` entry behind — a
    subsequent `transaction()` call on the same task must succeed, not
    falsely raise `TransactionError` forever.

    Under the round-6 token-set design, "not poisoned" means the guard
    clears once the abandoned call's own `finally` actually runs (via GC)
    and stops blocking future `transaction()` calls on the task — not that
    the entry is already gone the instant the *enclosing* block exits.
    That immediate-clear behavior in the pre-round-6 test was an artifact
    of the old design tracking connection *identity*: `transaction(conn)`
    nested on the very same `Connection` object the outer block already
    tracked wrote the same value back, so the outer block's cleanup
    happened to match and clear it — coincidence, not a guarantee, and it
    let a fresh `transaction(pool)` call proceed while the abandoned
    generator's savepoint was still logically open on a connection already
    returned to the pool (the actual hazard the round-4 ledger entry noted
    as "expected fallout," but which could interleave with an unrelated
    caller's independent transaction on that same physical connection).
    The token-set design tracks each call's own membership independently
    of connection identity, so it correctly reports the guard as still
    armed until the abandoned generator's own token is actually discarded
    — a truthful reflection of the still-open scope, not a permanent
    poison. What must still hold, and is asserted below, is that it is
    *not permanent*: once GC finalizes the generator, the guard clears and
    a subsequent `transaction()` call on the task succeeds.
    """
    pool = await get_pool()
    try:
        task = asyncio.current_task()

        async def open_and_abandon():
            async with transaction(pool) as outer_conn:
                async def agen():
                    async with transaction(outer_conn) as inner_conn:
                        yield inner_conn

                g = agen()
                await g.__anext__()
                # abandoned: never re-entered, never explicitly aclose()d
            return g

        g = await open_and_abandon()
        # The outer block's own token has been discarded, but the
        # abandoned generator's nested transaction(outer_conn) call is
        # still suspended at its yield — its token has not been discarded
        # yet, so the task's entry legitimately still shows one open
        # membership at this point (not None) until GC runs the
        # generator's finally.
        tokens = db._active_conn.get(task)
        assert tokens is not None and len(tokens) == 1

        del g
        gc.collect()
        for _ in range(20):
            await asyncio.sleep(0.01)

        # Now the generator's own late token-discard has run — task not
        # permanently poisoned.
        assert db._active_conn.get(task) is None

        async with transaction(pool) as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_b5_abandoned_pool_generator_does_not_disarm_sibling_block():
    """B5 (Phase 0 critic round 4) / B6 (Phase 0 critic round 5): an async
    generator holding a nested `transaction(pool)`, abandoned and finalized
    by GC *while* an unrelated, legitimately-open `transaction(conn)` block
    is still open on the same task, must not remove that unrelated block's
    live membership. Before the B5 fix, the Pool branch's `finally` did an
    unconditional `del _active_conn[task]`, so the abandoned generator's
    late cleanup silently disarmed the guard for the still-open sibling.
    The B5 patch (conditional delete) introduced B6: it validated its own
    entry but never re-validated what it was about to restore, so a
    *different* stale-restore path could still leak/corrupt state — this
    only showed up by asserting state *after* the sibling block exits,
    which the original version of this test never did (the exact test gap
    critic flagged). This version keeps the original in-block assertions
    and adds the post-exit assertion that would have caught B6.
    """
    pool = await get_pool()
    try:
        task = asyncio.current_task()

        async def abandoned_gen():
            async with transaction(pool) as conn:
                yield conn

        agen = abandoned_gen()
        await agen.__anext__()

        raw_conn = await pool.acquire()
        try:
            async with transaction(raw_conn) as outer_conn:
                del agen
                gc.collect()
                for _ in range(20):
                    await asyncio.sleep(0.01)

                # The unrelated, still-open block's membership must
                # survive: exactly one token (its own) left in the set.
                tokens = db._active_conn.get(task)
                assert tokens is not None
                assert len(tokens) == 1

                # And the guard must still be armed for it.
                with pytest.raises(TransactionError):
                    async with transaction(pool):
                        pass

                assert outer_conn is not None  # sanity: block still usable
        finally:
            await pool.release(raw_conn)

        # B6: after the sibling block itself exits, its own token must
        # also have been discarded — no residual/leaked entry for this
        # task at all, empty or absent, not some stale leftover value.
        assert db._active_conn.get(task) is None
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_b6_repeated_abandonment_does_not_leak_dict_entries():
    """Growth check (Phase 0 critic round 5 nit, folded into the B6 fix's
    verification scope): running the B5/B6 abandonment pattern repeatedly
    must never leave `_active_conn` growing across iterations — each
    iteration's task-scoped entries must be fully cleaned up (key absent)
    before the next iteration starts, and the dict must end empty.
    """
    pool = await get_pool()
    try:
        task = asyncio.current_task()

        for _ in range(5):
            async def abandoned_gen():
                async with transaction(pool) as conn:
                    yield conn

            agen = abandoned_gen()
            await agen.__anext__()

            raw_conn = await pool.acquire()
            try:
                async with transaction(raw_conn):
                    del agen
                    gc.collect()
                    for _ in range(20):
                        await asyncio.sleep(0.01)
            finally:
                await pool.release(raw_conn)

            # Fully clean after each iteration — no accumulation.
            assert db._active_conn.get(task) is None

        assert task not in db._active_conn
    finally:
        await close_pool()
