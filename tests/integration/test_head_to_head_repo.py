"""Live I/O proof for `data/repositories/head_to_head_repo.py` — §11 step
10's own verify criteria: `float` (never `Decimal`) round-tripping, the
870-row bulk loader, and the bulk upsert actually updating existing rows.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import head_to_head_repo, team_repo
from dba2.domain.head_to_head import HeadToHead
from dba2.services import league_service

_DOUBLE_PRECISION_FIELDS = (
    "opened",
    "received",
    "accepts_by_opponent",
    "rejects_by_opponent",
    "accepts_by_self",
    "rejects_by_self",
    "counters_given",
    "counters_received",
    "executions",
    "refused_illegal",
    "far_reject_streak",
    "gap_by_opponent_sum",
)


def _guild_id() -> int:
    return random.randint(10_000_000, 2_000_000_000)


async def _make_30_team_league(pool) -> int:
    league = await league_service.create_league(
        pool,
        discord_guild_id=_guild_id(),
        name="H2H Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


def _all_pairs(league_id: int, team_ids: list[int]) -> list[HeadToHead]:
    """Every ordered `(team_id, opponent_id)` pair with `team_id !=
    opponent_id` — 30 x 29 = 870 for a full league, matching §7's own count.
    Distinct, non-zero values per field so a mapper that swapped two
    same-typed columns would fail a round-trip assertion (the lesson
    `motivation_repo`'s test suite already applies to this repo's other
    all-float tables)."""
    rows = []
    for team_id in team_ids:
        for opponent_id in team_ids:
            if team_id == opponent_id:
                continue
            rows.append(
                HeadToHead(
                    league_id=league_id,
                    team_id=team_id,
                    opponent_id=opponent_id,
                    opened=1.0,
                    received=2.0,
                    accepts_by_opponent=3.0,
                    rejects_by_opponent=4.0,
                    accepts_by_self=5.0,
                    rejects_by_self=6.0,
                    counters_given=7.0,
                    counters_received=8.0,
                    executions=9.0,
                    refused_illegal=10.0,
                    far_reject_streak=11.0,
                    gap_by_opponent_sum=12.0,
                    last_gap_by_opponent=13.0,
                    last_season_interacted=2025,
                    updated_season=2026,
                )
            )
    return rows


@pytest.mark.asyncio
async def test_bulk_upsert_and_loader_round_trip_870_rows_for_a_30_team_league():
    pool = await get_pool()
    try:
        league_id = await _make_30_team_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            assert len(teams) == 30
            team_ids = [t.id for t in teams]

            rows = _all_pairs(league_id, team_ids)
            assert len(rows) == 870

            written = await head_to_head_repo.upsert_head_to_head_for_league(pool, rows)
            assert len(written) == 870

            loaded = await head_to_head_repo.list_head_to_head_for_league(pool, league_id)
            assert len(loaded) == 870

            pairs = {(r.team_id, r.opponent_id) for r in loaded}
            assert len(pairs) == 870
            assert all(team_id != opponent_id for team_id, opponent_id in pairs)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_loaded_rows_are_real_floats_not_decimal():
    """The float trap this repo has hit before (HANDOFF's recurring bug
    class): `double precision` maps to Python `float` via asyncpg, `numeric`
    maps to `Decimal`. `type(...) is float`, not `==`, because `Decimal(1)
    == 1.0` is `True` and would pass even if the column silently came back
    as `Decimal`."""
    pool = await get_pool()
    try:
        league_id = await _make_30_team_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0].id, teams[1].id

            row = HeadToHead(
                league_id=league_id,
                team_id=team_a,
                opponent_id=team_b,
                opened=1.0,
                received=2.0,
                accepts_by_opponent=3.0,
                rejects_by_opponent=4.0,
                accepts_by_self=5.0,
                rejects_by_self=6.0,
                counters_given=7.0,
                counters_received=8.0,
                executions=9.0,
                refused_illegal=10.0,
                far_reject_streak=11.0,
                gap_by_opponent_sum=12.0,
                last_gap_by_opponent=13.0,
                last_season_interacted=2025,
                updated_season=2026,
            )
            await head_to_head_repo.upsert_head_to_head_for_league(pool, [row])

            [loaded] = await head_to_head_repo.list_head_to_head_for_league(pool, league_id)
            for field in _DOUBLE_PRECISION_FIELDS:
                value = getattr(loaded, field)
                assert type(value) is float, f"{field} came back as {type(value)}, not float"
            assert type(loaded.last_gap_by_opponent) is float
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_bulk_upsert_updates_existing_rows_not_just_inserts():
    pool = await get_pool()
    try:
        league_id = await _make_30_team_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0].id, teams[1].id

            first = HeadToHead(
                league_id=league_id,
                team_id=team_a,
                opponent_id=team_b,
                opened=1.0,
                received=1.0,
                accepts_by_opponent=1.0,
                rejects_by_opponent=1.0,
                accepts_by_self=1.0,
                rejects_by_self=1.0,
                counters_given=1.0,
                counters_received=1.0,
                executions=1.0,
                refused_illegal=1.0,
                far_reject_streak=1.0,
                gap_by_opponent_sum=1.0,
                last_gap_by_opponent=1.0,
                last_season_interacted=2025,
                updated_season=2025,
            )
            await head_to_head_repo.upsert_head_to_head_for_league(pool, [first])

            second = HeadToHead(
                league_id=league_id,
                team_id=team_a,
                opponent_id=team_b,
                opened=99.0,
                received=98.0,
                accepts_by_opponent=97.0,
                rejects_by_opponent=96.0,
                accepts_by_self=95.0,
                rejects_by_self=94.0,
                counters_given=93.0,
                counters_received=92.0,
                executions=91.0,
                refused_illegal=90.0,
                far_reject_streak=89.0,
                gap_by_opponent_sum=88.0,
                last_gap_by_opponent=87.0,
                last_season_interacted=2026,
                updated_season=2026,
            )
            written = await head_to_head_repo.upsert_head_to_head_for_league(pool, [second])
            assert len(written) == 1
            assert written[0].opened == 99.0

            loaded = await head_to_head_repo.list_head_to_head_for_league(pool, league_id)
            assert len(loaded) == 1, "same PK must update in place, not insert a second row"
            [row] = loaded
            assert row.opened == 99.0
            assert row.received == 98.0
            assert row.accepts_by_opponent == 97.0
            assert row.rejects_by_opponent == 96.0
            assert row.accepts_by_self == 95.0
            assert row.rejects_by_self == 94.0
            assert row.counters_given == 93.0
            assert row.counters_received == 92.0
            assert row.executions == 91.0
            assert row.refused_illegal == 90.0
            assert row.far_reject_streak == 89.0
            assert row.gap_by_opponent_sum == 88.0
            assert row.last_gap_by_opponent == 87.0
            assert row.last_season_interacted == 2026
            assert row.updated_season == 2026
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_refused_illegal_round_trips_and_defaults_sensibly_when_omitted():
    """`refused_illegal` is written by nothing this step, but the column,
    field, and loader must still round-trip it correctly, including the
    schema-level default (migration 010's `DEFAULT 0`) a caller that
    doesn't set it explicitly falls back to."""
    pool = await get_pool()
    try:
        league_id = await _make_30_team_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0].id, teams[1].id

            # A caller-supplied non-zero value round-trips exactly.
            row = HeadToHead(
                league_id=league_id,
                team_id=team_a,
                opponent_id=team_b,
                opened=0.0,
                received=0.0,
                accepts_by_opponent=0.0,
                rejects_by_opponent=0.0,
                accepts_by_self=0.0,
                rejects_by_self=0.0,
                counters_given=0.0,
                counters_received=0.0,
                executions=0.0,
                refused_illegal=4.0,
                far_reject_streak=0.0,
                gap_by_opponent_sum=0.0,
                last_gap_by_opponent=None,
                last_season_interacted=None,
                updated_season=2026,
            )
            await head_to_head_repo.upsert_head_to_head_for_league(pool, [row])
            [loaded] = await head_to_head_repo.list_head_to_head_for_league(pool, league_id)
            assert loaded.refused_illegal == 4.0
            assert type(loaded.refused_illegal) is float

            # A row inserted without naming refused_illegal at all (e.g. a
            # future writer that never touches this column) defaults to 0.0
            # at the schema level, not NULL.
            team_c = teams[2].id
            await pool.execute(
                """
                INSERT INTO gm_head_to_head (league_id, team_id, opponent_id, updated_season)
                VALUES ($1, $2, $3, 2026)
                """,
                league_id,
                team_a,
                team_c,
            )
            defaulted = await pool.fetchrow(
                "SELECT refused_illegal FROM gm_head_to_head "
                "WHERE team_id = $1 AND opponent_id = $2",
                team_a,
                team_c,
            )
            assert defaulted["refused_illegal"] == 0.0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
