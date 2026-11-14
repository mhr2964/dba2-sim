"""Live I/O proof for `data/repositories/player_repo.py` — smoke coverage,
plus an actual §9 scope-creep guard: `_RecordingExecutor` below wraps the
real pool and records every SQL statement's text, so
`test_player_repo_queries_touch_no_table_but_players` can assert on the
literal `FROM`/`JOIN`/`INTO`/`UPDATE` targets instead of just trusting that
a player row with no team happens to still work (the previous version of
this docstring claimed that proved scope; a critic round proved it didn't —
rewriting `get_player` to join `teams`/`player_ratings` still passed the
full suite. Phase 0 step-7 critic round, B3).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo, player_repo, team_repo
from dba2.domain.player import Position, RosterStatus

_TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", re.IGNORECASE)


class _RecordingExecutor:
    """Delegates every call to the wrapped pool/connection but records the
    raw SQL text, so a test can assert on which tables a module's queries
    actually reference instead of just observing that the calls succeeded."""

    def __init__(self, delegate):
        self._delegate = delegate
        self.queries: list[str] = []

    async def fetchrow(self, sql, *args, **kwargs):
        self.queries.append(sql)
        return await self._delegate.fetchrow(sql, *args, **kwargs)

    async def fetch(self, sql, *args, **kwargs):
        self.queries.append(sql)
        return await self._delegate.fetch(sql, *args, **kwargs)

    async def fetchval(self, sql, *args, **kwargs):
        self.queries.append(sql)
        return await self._delegate.fetchval(sql, *args, **kwargs)

    async def execute(self, sql, *args, **kwargs):
        self.queries.append(sql)
        return await self._delegate.execute(sql, *args, **kwargs)


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Player Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


@pytest.mark.asyncio
async def test_create_get_player_with_no_team_touches_only_players():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Test",
                last_name="Player",
                primary_position=Position.PG,
                height_in=74,
                weight_lb=185,
                wingspan_in=76,
                birth_date=date(2000, 1, 1),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=70,
                overall_computed_at=datetime.now(UTC),
            )
            assert player.team_id is None
            assert player.roster_status == RosterStatus.FREE_AGENT

            fetched = await player_repo.get_player(pool, player.id)
            assert fetched is not None
            assert fetched.primary_position == Position.PG

            assert await player_repo.get_player(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_players_for_league_and_team_status_filter():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            fa = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Free",
                last_name="Agent",
                primary_position=Position.SG,
                height_in=76,
                weight_lb=200,
                wingspan_in=78,
                birth_date=date(1999, 5, 5),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=65,
                overall_computed_at=datetime.now(UTC),
            )
            active = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Active",
                last_name="Roster",
                primary_position=Position.C,
                height_in=84,
                weight_lb=250,
                wingspan_in=88,
                birth_date=date(1998, 3, 3),
                roster_status=RosterStatus.ACTIVE,
                overall_cached=80,
                overall_computed_at=datetime.now(UTC),
            )

            all_players = await player_repo.list_players_for_league(pool, league_id)
            assert {p.id for p in all_players} == {fa.id, active.id}

            active_only = await player_repo.list_players_for_league(
                pool, league_id, roster_status=RosterStatus.ACTIVE
            )
            assert [p.id for p in active_only] == [active.id]

            team_players = await player_repo.list_players_for_team(pool, -1)
            assert team_players == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_team_and_status_and_set_overall_cached():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Signed",
                last_name="Player",
                primary_position=Position.SF,
                height_in=80,
                weight_lb=220,
                wingspan_in=83,
                birth_date=date(2001, 7, 7),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=60,
                overall_computed_at=datetime.now(UTC),
            )
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="SGN",
                name="Signers",
                city="Signville",
                conference="East",
                division="Southeast",
                home_market_size=2,
            )
            before = await player_repo.get_player(pool, player.id)

            await player_repo.set_team_and_status(
                pool, player.id, team.id, RosterStatus.ACTIVE
            )
            signed = await player_repo.get_player(pool, player.id)
            assert signed.team_id == team.id
            assert signed.roster_status == RosterStatus.ACTIVE
            # updated_at must actually advance — a write that dropped this
            # column would still pass the two assertions above (Phase 0
            # step-7 critic round, B2).
            assert signed.updated_at > before.updated_at

            await player_repo.set_team_and_status(
                pool, player.id, None, RosterStatus.FREE_AGENT
            )
            waived = await player_repo.get_player(pool, player.id)
            assert waived.team_id is None
            assert waived.roster_status == RosterStatus.FREE_AGENT

            now = datetime.now(UTC)
            await player_repo.set_overall_cached(pool, player.id, 88, now)
            updated = await player_repo.get_player(pool, player.id)
            assert updated.overall_cached == 88
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_player_round_trips_every_field_distinctly():
    """Every `Player` field gets a distinct value so a mapper that swapped
    two same-typed fields (e.g. `height_in`/`weight_lb`, both `int`) would
    fail this test where a fixture reusing values across fields could not
    (Phase 0 step-7 critic round, B2 — 7 scripted field-swap mutations
    survived the previous version of this suite)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Distinct",
                last_name="Fields",
                external_ref="ext-distinct-1",
                primary_position=Position.SG,
                secondary_position=Position.SF,
                height_in=79,
                weight_lb=214,
                wingspan_in=82,
                birth_date=date(1995, 11, 3),
                draft_year=2015,
                draft_pick_number=12,
                years_pro=9,
                roster_status=RosterStatus.INJURED,
                overall_cached=91,
                overall_computed_at=datetime(2026, 1, 15, tzinfo=UTC),
                retired_in_season=None,
            )
            fetched = await player_repo.get_player(pool, player.id)
            assert fetched.first_name == "Distinct"
            assert fetched.last_name == "Fields"
            assert fetched.external_ref == "ext-distinct-1"
            assert fetched.primary_position == Position.SG
            assert fetched.secondary_position == Position.SF
            assert fetched.height_in == 79
            assert fetched.weight_lb == 214
            assert fetched.wingspan_in == 82
            assert fetched.birth_date == date(1995, 11, 3)
            assert fetched.draft_year == 2015
            assert fetched.draft_pick_number == 12
            assert fetched.years_pro == 9
            assert fetched.roster_status == RosterStatus.INJURED
            assert fetched.overall_cached == 91
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_lock_player_row_locks_and_returns_the_same_row():
    """Round-6 critic's B1 fix (`roster_service.sign`'s cross-team same-
    player concurrency race, docs/design/phase-5-discord-presentation.md
    §43.6) -- `lock_player_row` (renamed from `get_player_for_update`;
    RMLP Decision 1 changed its SQL to `FOR NO KEY UPDATE`, see that
    function's own docstring) mirrors `team_repo.lock_team_row`'s own
    established row-lock pattern, applied to `players`. Smoke coverage
    only (the real concurrency proof lives in `tests/
    integration/test_gm_command_service.py::
    test_concurrent_sign_free_agent_calls_by_two_different_teams_do_not_
    both_win`, which needs the real race, not just this function in
    isolation): a `SELECT ... FOR NO KEY UPDATE` inside an open
    `transaction()` returns the identical row `get_player` would, and a
    missing player still returns `None` rather than raising."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Locked",
                last_name="Player",
                primary_position=Position.PF,
                height_in=81,
                weight_lb=230,
                wingspan_in=84,
                birth_date=date(1999, 2, 2),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=72,
                overall_computed_at=datetime.now(UTC),
            )
            async with transaction(pool) as conn:
                locked = await player_repo.lock_player_row(conn, player.id)
                assert locked == player

            async with transaction(pool) as conn:
                missing = await player_repo.lock_player_row(conn, player.id + 999_999)
                assert missing is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_player_repo_queries_touch_no_table_but_players():
    """§9's scope-creep guard, made real: records the literal SQL every
    `player_repo` function issues and asserts every `FROM`/`JOIN`/`INTO`/
    `UPDATE` target is `players` — not just that the calls happen to
    succeed with no `teams` row present, which a query that silently joined
    out would also do (Phase 0 step-7 critic round, B3)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            rec = _RecordingExecutor(pool)
            player = await player_repo.create_player(
                rec,
                league_id=league_id,
                first_name="Scope",
                last_name="Guard",
                primary_position=Position.C,
                height_in=85,
                weight_lb=255,
                wingspan_in=89,
                birth_date=date(1994, 6, 6),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=70,
                overall_computed_at=datetime.now(UTC),
            )
            await player_repo.get_player(rec, player.id)
            await player_repo.lock_player_row(rec, player.id)
            await player_repo.list_players_for_team(rec, -1)
            await player_repo.list_players_for_league(rec, league_id)
            await player_repo.list_players_for_league(
                rec, league_id, roster_status=RosterStatus.FREE_AGENT
            )
            await player_repo.set_team_and_status(
                rec, player.id, None, RosterStatus.FREE_AGENT
            )
            await player_repo.set_overall_cached(rec, player.id, 75, datetime.now(UTC))

            assert len(rec.queries) == 8
            tables_touched: set[str] = set()
            for sql in rec.queries:
                tables_touched.update(m.lower() for m in _TABLE_REF.findall(sql))
            assert tables_touched == {"players"}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
