"""The shared `rmlp_gate` test helper -- Addendum A1.3 to the Roster
Mutation Lock Protocol (RMLP) design.

Every integration test that calls one of the five RMLP mutation functions
(`roster_service.sign`/`waive`/`release`, `gm_service.apply_trade`/
`apply_signing`) *directly* -- rather than through `gm_command_service` or
`run_phase_hook`, both of which open their own gate -- must open a gate on
the SAME `Connection` it hands to the mutator (`roster_lock.acquire`'s
gate-held assertion is scoped to `pg_backend_pid()`, so a fixture opening a
gate on its OWN connection would do nothing for a test passing a different
connection into the function under test -- Addendum A1.2's "trap 1"). A
plain pytest fixture cannot do this job; only a per-call context manager
can, since the gate must be opened on the exact connection the test then
uses.

Lives in `tests/fixtures/` (not `dba2/`) -- this is test-only scaffolding,
not production code the discipline test's `RMLP_ENTRY_POINTS` table needs
to know about.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from dba2.data.db import transaction
from dba2.services import roster_lock
from dba2.services.roster_lock import LockMode


@asynccontextmanager
async def rmlp_gate(
    pool, league_id: int, *, mode: LockMode = LockMode.SHARED
) -> AsyncIterator[asyncpg.Connection]:
    """Opens `transaction(pool)`, calls `roster_lock.open_league_gate` on
    the yielded connection, and yields that SAME connection. Every gate-
    plumbing test edit becomes one line:

        async with rmlp_gate(pool, league_id) as conn:
            await roster_service.waive(conn, league_id=league_id, ...)

    Defaults to `SHARED` so a test author is not asked to reason about
    lock mode for the common case (a single mutation call, not a
    phase-hook-shaped batch)."""
    async with transaction(pool) as conn:
        await roster_lock.open_league_gate(conn, league_id=league_id, mode=mode)
        yield conn
