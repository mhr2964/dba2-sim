"""Live I/O proof for `data/repositories/injury_repo.py` — smoke coverage
for create/get/list-for-player plus the `resolve_injury` re-run guard (§4.7).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, injury_repo, league_repo, player_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.player import Position, RosterStatus


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Injury Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Injury",
        last_name="Prone",
        primary_position=Position.C,
        height_in=84,
        weight_lb=250,
        wingspan_in=88,
        birth_date=date(1995, 3, 3),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_create_get_injury_and_list_for_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            # Inserted out of season order to prove the list query sorts.
            later = await injury_repo.create_injury(
                pool,
                league_id=league_id,
                player_id=player_id,
                injury_type="ankle_sprain",
                severity="minor",
                occurred_in_season=2027,
                games_remaining=3,
            )
            earlier = await injury_repo.create_injury(
                pool,
                league_id=league_id,
                player_id=player_id,
                injury_type="hamstring_strain",
                severity="moderate",
                occurred_in_season=2026,
                games_remaining=10,
            )
            assert later.resolved_at is None
            assert earlier.game_id is None

            fetched = await injury_repo.get_injury(pool, later.id)
            assert fetched is not None
            assert fetched.injury_type == "ankle_sprain"

            for_player = await injury_repo.list_injuries_for_player(pool, player_id)
            assert [i.id for i in for_player] == [earlier.id, later.id]

            other_player_id = await _make_player(pool, league_id)
            assert await injury_repo.list_injuries_for_player(pool, other_player_id) == []
            assert await injury_repo.get_injury(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_injury_round_trips_every_field_distinctly():
    """Every `Injury` field gets a distinct value so a mapper that swapped
    two same-typed fields (e.g. `injury_type`/`severity`, both `str`, or
    `games_remaining`/`occurred_in_season`, both `int`) would fail this
    test (Phase 0 step-7 critic round, B2 lesson)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            home_team_id = (await team_repo.create_team(
                pool,
                league_id=league_id,
                code="IJA",
                name="Injury A",
                city="Injury City A",
                conference="East",
                division="Central",
                home_market_size=1,
            )).id
            away_team_id = (await team_repo.create_team(
                pool,
                league_id=league_id,
                code="IJB",
                name="Injury B",
                city="Injury City B",
                conference="East",
                division="Central",
                home_market_size=1,
            )).id
            game = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=1,
                game_date=date(2026, 12, 5),
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                game_type=GameType.REGULAR,
                rng_seed=1,
            )

            created = await injury_repo.create_injury(
                pool,
                league_id=league_id,
                player_id=player_id,
                game_id=game.id,
                injury_type="acl_tear",
                severity="severe",
                occurred_in_season=2026,
                games_remaining=82,
            )
            fetched = await injury_repo.get_injury(pool, created.id)
            assert fetched.league_id == league_id
            assert fetched.player_id == player_id
            assert fetched.game_id == game.id
            assert fetched.injury_type == "acl_tear"
            assert fetched.severity == "severe"
            assert fetched.games_remaining == 82
            assert fetched.occurred_in_season == 2026
            assert fetched.resolved_at is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_resolve_injury_is_a_noop_on_an_already_resolved_row():
    """`resolve_injury` must not clobber an existing `resolved_at` on a
    second call — a silent overwrite would destroy the true resolution
    timestamp with no error (Phase 0 step-7 critic round, B1's bug class,
    same fix as `team_repo.close_manager_assignment` /
    `draft_repo.complete_lottery` / `contract_repo.terminate_contract`)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            injury = await injury_repo.create_injury(
                pool,
                league_id=league_id,
                player_id=player_id,
                injury_type="concussion",
                severity="minor",
                occurred_in_season=2026,
            )
            assert injury.resolved_at is None

            await injury_repo.resolve_injury(pool, injury.id)
            [resolved] = await injury_repo.list_injuries_for_player(pool, player_id)
            first_resolved_at = resolved.resolved_at
            assert first_resolved_at is not None

            await injury_repo.resolve_injury(pool, injury.id)
            [still_resolved] = await injury_repo.list_injuries_for_player(pool, player_id)
            assert still_resolved.resolved_at == first_resolved_at
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
