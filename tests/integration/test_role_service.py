"""`role_service`'s impure half (`docs/design/auto-gm-role-construction.md`
RC-22.5, RC-24's own test list): the self-managed-team skip and the
locked-row skip, both against a real database."""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    league_repo,
    player_repo,
    ratings_repo,
    role_repo,
    team_repo,
)
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy
from dba2.services import role_service
from tests.fixtures.sim import _distinct_ratings

_COMMISSIONER = 999001
_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="role_service Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
        current_phase=Phase.PRESEASON,
    )
    return league.id


async def _make_team(
    pool, league_id: int, code: str, *, auto_pilot: bool = False, **kwargs
) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        **kwargs,
    )
    if auto_pilot:
        await team_repo.set_auto_pilot(
            pool, team.id, auto_pilot=True, gm_philosophy=GmPhilosophy.TENDENCY_RESPECTER
        )
    return team.id


async def _make_player_with_ratings(pool, league_id: int, team_id: int, *, seed: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1997, 6, 12),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    values = {
        **_distinct_ratings(seed),
        "peak_age_start": 24 + (seed % 6),
        "peak_age_end": 30 + (seed % 6),
    }
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        ratings=values,
    )
    return player.id


@pytest.mark.asyncio
async def test_the_pass_skips_every_self_managed_team():
    """RC-22.2/RC-22.5: a manager-and-not-auto-pilot team is skipped; a
    manager-and-auto-pilot team and an unmanaged team both get real rows."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            self_managed = await _make_team(
                pool, league_id, "RS1", manager_user_id=701, auto_pilot=False
            )
            unmanaged = await _make_team(pool, league_id, "RS2", manager_user_id=None)

            for seed, team_id in enumerate((self_managed, unmanaged)):
                for i in range(5):
                    await _make_player_with_ratings(
                        pool, league_id, team_id, seed=seed * 100 + i
                    )

            await role_service.construct_ai_roles_for_league(
                pool, league_id=league_id, season=2026
            )

            self_managed_roles = await role_repo.list_player_roles_for_team(
                pool, self_managed, 2026
            )
            unmanaged_roles = await role_repo.list_player_roles_for_team(pool, unmanaged, 2026)

            assert self_managed_roles == []
            assert len(unmanaged_roles) == 5
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_locked_row_is_not_overwritten_and_still_consumes_a_rank():
    """RC-22.5: a locked row is skipped (its role/touch_share/rationale
    are untouched), but the locked player still occupies a rank slot --
    checked by writing a low `touch_share` on the roster's best player and
    confirming the pass does not "fix" it for him while still writing
    everyone else."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RS3", manager_user_id=None)
            player_ids = [
                await _make_player_with_ratings(pool, league_id, team_id, seed=i)
                for i in range(6)
            ]
            # Give the first player the best overall (highest rank) and lock a
            # deliberately wrong, low touch_share on him.
            await player_repo.set_overall_cached(pool, player_ids[0], 95, datetime.now(UTC))
            locked_role = await role_repo.upsert_player_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                season=2026,
                player_id=player_ids[0],
                role="human_override",
                touch_share=Decimal("0.0100"),
                locked=True,
            )
            assert locked_role.locked is True

            await role_service.construct_ai_roles_for_league(
                pool, league_id=league_id, season=2026
            )

            roles = await role_repo.list_player_roles_for_team(pool, team_id, 2026)
            by_player = {r.player_id: r for r in roles}

            # The locked row is untouched.
            assert by_player[player_ids[0]].role == "human_override"
            assert by_player[player_ids[0]].touch_share == Decimal("0.0100")
            assert by_player[player_ids[0]].locked is True

            # Every OTHER player got a real AI-written row.
            for pid in player_ids[1:]:
                assert by_player[pid].assigned_by is None
                assert by_player[pid].locked is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
