"""Pool construction, the `Executor` type alias, and the `transaction()` helper.

§5's repository convention: every repo/service function takes an `Executor`
(a `Pool` or a `Connection`) as its first parameter, not a module-global
pool. That is what lets a trade's writes — contracts, dead cap, picks,
roster moves across four repos — run inside one transaction, and what lets
tests wrap every case in a transaction that gets rolled back instead of
truncating tables between tests.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from dba2.core.config import get_config
from dba2.core.errors import TransactionError
from dba2.core.logging import get_logger

log = get_logger(__name__)

Executor = asyncpg.Pool | asyncpg.Connection

_pool: asyncpg.Pool | None = None

# How long a top-level `transaction(pool)` call will wait for `pool.acquire()`
# before giving up. Without a timeout, a caller nested past `max_size` open
# connections deep blocks inside `pool.acquire()` forever with no exception
# and no log line — connection starvation that looks exactly like a hang.
# This turns that hang into an immediate, diagnosable `TransactionError`.
_POOL_ACQUIRE_TIMEOUT_S = 10.0

# Tracks which tasks currently have an open transaction() block, keyed by
# the `asyncio.Task` that opened it — NOT a contextvar. A contextvar looks
# right at first (asyncpg itself uses one for connection-local state) but is
# wrong here: `asyncio.create_task()` gives the child task a *copy* of the
# parent's context at creation time, and resetting the var in the parent
# afterward does not retroactively affect that copy. A fire-and-forget task
# spawned from inside a `transaction()` block would inherit the parent's
# "transaction open" marker and could see it well after the parent block
# (and its connection) had already been torn down. Keying by
# `asyncio.current_task()` instead scopes the guard to exactly what it's
# meant to catch — re-entrant/nested calls within the *same task's* call
# stack — while a task created via `create_task()` is a distinct `Task`
# object with no entry of its own, so it's correctly treated as independent
# no matter when it happens to run relative to the parent block.
#
# Each task maps to a *set of per-call tokens*, not a single remembered
# Connection. This is the third design for this guard, and it exists
# because the previous one (`dict[Task, Connection]` with save-on-entry /
# restore-on-exit semantics) produced three separate blocking bugs (B4, B5,
# B6 — see EFFORT_LEDGER.md, Phase 0) that were all the same root cause
# wearing a different hat: restoring a *remembered prior value* on exit is
# only correct if that remembered value is still valid by the time you
# restore it, and a `transaction()` call living inside an abandonable scope
# (most commonly an async generator that's never fully consumed, whose
# `finally` only runs whenever GC happens to finalize it) has no way to
# know whether the thing it remembered is still live, already replaced, or
# itself gone stale. Patching each instance individually kept finding a new
# one, because "restore what I remembered" is inherently order-dependent —
# it assumes cleanup runs in the same relative order state was captured in,
# which an abandoned/GC-finalized scope cannot guarantee.
#
# The token-set design removes that assumption entirely. On entry, a call
# creates a private `object()` token — unique by identity, cannot collide
# with any other call's token — and adds it to its task's set. On exit
# (`finally`), it *discards* (not `remove`) that exact token from that set,
# nothing else: never reads, never restores, never touches any other
# call's membership. Discard is idempotent (a no-op if the token is already
# gone) and doesn't care what order it runs in relative to any other call
# on the same task, because it never depends on any other call's state to
# do its job correctly. A late-finalizing abandoned generator's cleanup can
# therefore only ever remove its own token — it structurally cannot corrupt
# a sibling or ancestor call's marker, no matter when its `finally` runs.
# "Guard armed for this task" = "this task's token set is non-empty"; a set
# is deleted from the dict entirely once it empties, so finished tasks
# don't accumulate residual entries (verified — see
# tests/integration/test_transaction_guard.py's B6 case).
#
# Guard scope and its limits: this dict is task-scoped, not call-scoped.
# The Connection branch (nested savepoints, i.e. `transaction(conn)`) does
# not run the re-entrancy check below at all — passing an explicit
# `Connection` down is exactly how callers are meant to compose nested
# transactional work, so it only adds/discards its own token. The Pool (or
# no-argument) branch is the one that checks "is this task's set already
# non-empty" and raises if so. A `transaction()` block must still not
# outlive a scope that can be abandoned without running to completion (an
# async generator that's never fully consumed or explicitly `aclose()`d,
# most commonly) — the token-set design makes that safe to abandon (no
# corruption of other calls' state), not free of side effects entirely
# (the abandoned generator's own connection/savepoint teardown still runs
# whenever GC gets to it). Also note the guard cannot see fan-out:
# `asyncio.gather()` or `create_task()` calls made *from inside* a
# transaction() block spawn their own Task with no entry of its own (see
# above), so a nested `transaction(pool)` call from that sibling task is
# invisible to this check and will silently open an independent, unrelated
# transaction — atomicity across that kind of fan-out is the caller's
# responsibility, not this guard's.
_active_conn: dict[asyncio.Task, set[object]] = {}


def _guard_add(task: asyncio.Task, token: object) -> None:
    """Add this call's token to the task's open-transaction set."""
    _active_conn.setdefault(task, set()).add(token)


def _guard_discard(task: asyncio.Task, token: object) -> None:
    """Discard this call's token, and only this call's token.

    Idempotent and order-independent: safe to call from a `finally` that
    runs at any time relative to any other call on the same task, since it
    never reads or depends on anything other calls have done. If the set
    becomes empty, the task's entry is removed entirely so finished tasks
    don't leave residual dict entries behind.
    """
    tokens = _active_conn.get(task)
    if tokens is None:
        return
    tokens.discard(token)
    if not tokens:
        del _active_conn[task]


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        cfg = get_config()
        _pool = await asyncpg.create_pool(
            cfg.database_url,
            min_size=1,
            max_size=10,
            max_inactive_connection_lifetime=60,
            command_timeout=60,
        )
        log.info("Database pool established")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("Database pool closed")


@asynccontextmanager
async def transaction(ex: Executor | None = None) -> AsyncIterator[asyncpg.Connection]:
    """Yield a `Connection` with an open transaction.

    - Given an existing `Connection` (i.e. the caller is already inside a
      transaction), nests via asyncpg's savepoint support instead of opening
      a second top-level transaction — a service that calls another
      service, each wrapping its own writes in `transaction()`, composes
      correctly instead of double-committing or silently flattening the
      inner scope.
    - Given a `Pool` (or nothing, in which case the module pool is used),
      acquires a connection and owns commit/rollback for the block — but
      only if the current task doesn't already have a `transaction()` open.
      If one is open, the caller meant to pass that `Connection` down and
      forgot; raising a `TransactionError` here is the contract, not
      acquiring an unrelated second connection and silently running two
      independent transactions for what the caller believed was one atomic
      unit. This guard is task-scoped, not call-stack- or coroutine-scoped:
      work spawned from inside the block via `asyncio.gather()` or
      `asyncio.create_task()` runs on its own `Task` and is invisible to
      this check — a nested `transaction(pool)` call made from that sibling
      task will NOT raise here and will open its own, unrelated
      transaction. Atomicity across that kind of fan-out is the caller's
      responsibility, not this guard's.

    Callers composing multiple repos/services into one atomic unit (e.g. a
    trade's writes across four repos, §5) must open exactly one outer
    `transaction(pool)` and pass the yielded `Connection` — not the
    `Pool` — into every nested call.

    Tests should pass an already-open `Connection` whose outer transaction
    they intend to roll back after the test, so every repo/service call in
    the test nests via savepoint and nothing touches real committed rows.
    """
    task = asyncio.current_task()
    token = object()

    if isinstance(ex, asyncpg.Connection):
        # Connection-in-Connection savepoint nesting (same task, same
        # connection passed down explicitly) does not consult the
        # re-entrancy check below at all — this is the legitimate nesting
        # path. Add this call's own token; discard exactly that token on
        # exit, regardless of what else is in the set or when this finally
        # happens to run relative to any other call on this task.
        _guard_add(task, token)
        try:
            async with ex.transaction():
                yield ex
        finally:
            _guard_discard(task, token)
        return

    if _active_conn.get(task):
        raise TransactionError(
            "transaction() was called with a Pool (or no argument) while "
            "the current task is already inside an open transaction() "
            "block. Pass the already-open Connection into the nested call "
            "instead — forwarding the Pool here would silently acquire a "
            "second, unrelated connection and run an independent "
            "transaction, breaking the atomicity the outer transaction() "
            "call was meant to guarantee. (Work spawned via "
            "asyncio.gather()/create_task() runs on its own Task and is "
            "not covered by this guard — see the transaction() docstring.)"
        )

    pool = ex if isinstance(ex, asyncpg.Pool) else await get_pool()

    # Only `pool.acquire()` itself is wrapped in the `except TimeoutError`
    # below — the transaction body (the `yield`) is deliberately outside this
    # `try`. `asyncio.TimeoutError is TimeoutError` since Python 3.11, so if
    # the body were inside this `try`, any `asyncio.wait_for` timeout raised
    # by caller code, or asyncpg's own `command_timeout` (set on the pool in
    # `get_pool()`) firing on a slow query, would get misreported as pool
    # starvation. Acquiring manually (instead of `async with pool.acquire()`)
    # is what makes it possible to scope the `try` this tightly; see
    # https://magicstack.github.io/asyncpg — `Pool.acquire()` can be awaited
    # directly and released via `pool.release()`, not only used as an async
    # context manager.
    try:
        conn = await pool.acquire(timeout=_POOL_ACQUIRE_TIMEOUT_S)
    except TimeoutError as exc:
        raise TransactionError(
            f"Timed out after {_POOL_ACQUIRE_TIMEOUT_S}s waiting for "
            "pool.acquire() — likely connection-pool starvation (e.g. "
            "transaction() calls nested via Pool past the pool's max_size, "
            "or a connection leaked by code that never released it). "
            "Failing fast instead of hanging."
        ) from exc

    try:
        _guard_add(task, token)
        try:
            async with conn.transaction():
                yield conn
        finally:
            # Discard only this call's own token — see _guard_discard and
            # the module comment above _active_conn. This call's finally
            # can run *late* relative to the task's logical state (e.g.
            # this call lives inside an async generator that gets
            # abandoned and is only finalized by GC at some arbitrary
            # later point, possibly while an unrelated, legitimately-open
            # transaction() on this same task is mid-block), but that no
            # longer matters: discarding a token never touches any other
            # call's membership, so a late/out-of-order discard here
            # cannot disarm a sibling block's guard.
            _guard_discard(task, token)
    finally:
        await pool.release(conn)
