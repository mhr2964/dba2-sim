"""Step 2 verify (phase-0-foundation.md §8.2): a pool opens against the
compose Postgres and round-trips SELECT 1.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""
from __future__ import annotations

import pytest

from dba2.data.db import close_pool, get_pool, transaction


@pytest.mark.asyncio
async def test_pool_round_trips_select_1():
    pool = await get_pool()
    try:
        result = await pool.fetchval("SELECT 1")
        assert result == 1
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_transaction_helper_nests_via_savepoint():
    pool = await get_pool()
    try:
        async with transaction(pool) as conn:
            assert await conn.fetchval("SELECT 1") == 1
            async with transaction(conn) as inner_conn:
                assert inner_conn is conn
                assert await inner_conn.fetchval("SELECT 2") == 2
    finally:
        await close_pool()
