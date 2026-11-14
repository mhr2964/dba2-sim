"""Live I/O proof for `data/repositories/role_repo.py` — smoke coverage for
player_roles (including its `ON CONFLICT` upsert path and the `locked`
toggle) and player_directives (its own `ON CONFLICT` upsert path).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, player_repo, role_repo, team_repo
from dba2.domain.player import Position, RosterStatus
from dba2.domain.role import DefenseMode, ShotDiet, UsageMode


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Role Repo Test League",
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
        division="Central",
        home_market_size=2,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Role",
        last_name="Player",
        primary_position=Position.SG,
        height_in=77,
        weight_lb=200,
        wingspan_in=80,
        birth_date=date(1998, 5, 5),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=80,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


@pytest.mark.asyncio
async def test_upsert_player_role_get_and_list_for_team():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "ROL")
            player_id = await _make_player(pool, league_id, team_id)

            created = await role_repo.upsert_player_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                season=2026,
                player_id=player_id,
                role="primary_option",
                touch_share=Decimal("0.2500"),
                rationale="Distinct Rationale One",
                assigned_by=11,
                locked=False,
            )
            # league_id/team_id/season/player_id distinct identifiers so a
            # mapper that swapped any pair of them would fail here (Phase 0
            # step-7 critic round, B3 — the previous version of this test
            # never asserted on any of the four).
            assert created.league_id == league_id
            assert created.team_id == team_id
            assert created.season == 2026
            assert created.player_id == player_id
            assert created.role == "primary_option"
            assert created.touch_share == Decimal("0.2500")
            assert created.rationale == "Distinct Rationale One"
            assert created.assigned_by == 11
            assert created.locked is False

            fetched = await role_repo.get_player_role(pool, league_id, team_id, 2026, player_id)
            assert fetched is not None
            assert fetched.id == created.id

            # Re-assigning within the same season overwrites, not duplicates.
            updated = await role_repo.upsert_player_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                season=2026,
                player_id=player_id,
                role="secondary_option",
                touch_share=Decimal("0.1800"),
                rationale="Distinct Rationale Two",
                assigned_by=22,
                locked=True,
            )
            assert updated.id == created.id
            assert updated.role == "secondary_option"
            assert updated.touch_share == Decimal("0.1800")
            assert updated.rationale == "Distinct Rationale Two"
            assert updated.assigned_by == 22
            assert updated.locked is True

            for_team = await role_repo.list_player_roles_for_team(pool, team_id, 2026)
            assert [r.id for r in for_team] == [created.id]

            no_such_season = await role_repo.get_player_role(
                pool, league_id, team_id, 2099, player_id
            )
            assert no_such_season is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_role_locked_toggles_back_and_forth():
    """`locked` is a reversible toggle, not a one-time audit event — must be
    settable to `True` then back to `False` with no guard blocking it."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TGL")
            player_id = await _make_player(pool, league_id, team_id)
            role = await role_repo.upsert_player_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                season=2026,
                player_id=player_id,
                role="bench",
                touch_share=Decimal("0.1000"),
            )
            assert role.locked is False

            await role_repo.set_role_locked(pool, role.id, True)
            locked = await role_repo.get_player_role(pool, league_id, team_id, 2026, player_id)
            assert locked.locked is True

            await role_repo.set_role_locked(pool, role.id, False)
            unlocked = await role_repo.get_player_role(pool, league_id, team_id, 2026, player_id)
            assert unlocked.locked is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_upsert_player_directive_and_get():
    """PD-1/PD-6 retype (2026-09-23): `role_mode`/`clutch_mode` are gone
    from this function's signature (the column dropped, migration 023);
    the three survivors are now closed enums, not free text -- rewritten
    with real members ("high_usage"/"switch_everything"/"starter"/"feature"
    were never real values even before the retype, just untyped strings
    the old free-text column happened to accept)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DIR")
            player_id = await _make_player(pool, league_id, team_id)

            created = await role_repo.upsert_player_directive(
                pool,
                league_id=league_id,
                player_id=player_id,
                shot_diet=ShotDiet.MORE_THREES,
                usage_mode=UsageMode.INITIATE,
                defense_mode=DefenseMode.HELP_EARLY,
                set_by=33,
            )
            # league_id/player_id distinct identifiers so a mapper that
            # swapped the two (both `int`) would fail here (Phase 0 step-7
            # critic round, B3).
            assert created.league_id == league_id
            assert created.player_id == player_id
            assert created.shot_diet == ShotDiet.MORE_THREES
            assert created.usage_mode == UsageMode.INITIATE
            assert created.defense_mode == DefenseMode.HELP_EARLY
            assert created.set_by == 33

            updated = await role_repo.upsert_player_directive(
                pool,
                league_id=league_id,
                player_id=player_id,
                shot_diet=ShotDiet.FEWER_MIDRANGE,
                usage_mode=UsageMode.OFF_BALL,
                defense_mode=DefenseMode.STAY_HOME,
                set_by=44,
            )
            assert updated.id == created.id
            assert updated.shot_diet == ShotDiet.FEWER_MIDRANGE
            assert updated.usage_mode == UsageMode.OFF_BALL
            assert updated.defense_mode == DefenseMode.STAY_HOME
            assert updated.set_by == 44

            fetched = await role_repo.get_player_directive(pool, league_id, player_id)
            assert fetched.shot_diet == ShotDiet.FEWER_MIDRANGE

            other_player = await _make_player(pool, league_id, team_id)
            assert await role_repo.get_player_directive(pool, league_id, other_player) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_player_directives_for_league_bulk_loader():
    """Bulk loader (phase-1-simulation-engine.md §8.2) — a league-scoped
    directives snapshot, not a per-player `get_player_directive` query.
    Scoped to `league_id`: a directive in a second league must not leak in."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "LDA")
            player_a = await _make_player(pool, league_id, team_id)
            player_b = await _make_player(pool, league_id, team_id)
            other_team_id = await _make_team(pool, other_league_id, "LDB")
            player_other = await _make_player(pool, other_league_id, other_team_id)

            await role_repo.upsert_player_directive(
                pool, league_id=league_id, player_id=player_a, shot_diet=ShotDiet.MORE_THREES
            )
            await role_repo.upsert_player_directive(
                pool, league_id=league_id, player_id=player_b, shot_diet=ShotDiet.ATTACK_THE_RIM
            )
            await role_repo.upsert_player_directive(
                pool,
                league_id=other_league_id,
                player_id=player_other,
                shot_diet=ShotDiet.FEWER_MIDRANGE,
            )

            for_league = await role_repo.list_player_directives_for_league(pool, league_id)
            assert {d.player_id for d in for_league} == {player_a, player_b}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()
