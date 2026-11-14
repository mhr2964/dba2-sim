"""Live I/O proof for `services/cap_service.py::get_cap_summary` -- §4.4's
cap-summary formula (`used = committed contract_years salary + dead_cap_charges
+ optional cap_holds`) and Decision 19's `include_holds` toggle.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, team_repo
from dba2.domain.contract import ContractStatus, ContractType, DeadCapReason, SigningMethod
from dba2.domain.player import Position, RosterStatus
from dba2.services import cap_service


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Cap Service Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=2026,
        salary_cap=150_000_000,
        luxury_tax=180_000_000,
        apron_1=190_000_000,
        apron_2=200_000_000,
        max_pct_0_6=25,
        max_pct_7_9=30,
        max_pct_10_plus=35,
        supermax_pct=35,
        min_salary=1_200_000,
        mle_amount=13_000_000,
        bi_annual_amount=5_000_000,
        raise_pct_bird=8,
        raise_pct_non_bird=5,
        roster_min=14,
        roster_max=15,
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
        division="Atlantic",
        home_market_size=3,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int | None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Cap",
        last_name="Summary",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1999, 3, 3),
        roster_status=RosterStatus.ACTIVE if team_id is not None else RosterStatus.FREE_AGENT,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


@pytest.mark.asyncio
async def test_get_cap_summary_raises_not_found_with_no_cap_settings_row():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="No Cap Settings League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            team_id = await _make_team(pool, league.id, "NCS")
            with pytest.raises(NotFoundError):
                await cap_service.get_cap_summary(
                    pool, league_id=league.id, team_id=team_id, season=2026
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_cap_summary_sums_committed_salary_and_dead_cap():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SUM")
            active_player = await _make_player(pool, league_id, team_id)
            waived_player = await _make_player(pool, league_id, team_id)

            active_contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=active_player,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool,
                contract_id=active_contract.id,
                season=2026,
                salary=20_000_000,
                guaranteed_amount=20_000_000,
            )

            waived_contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=waived_player,
                team_id=team_id,
                signed_in_season=2024,
                start_season=2024,
                end_season=2026,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
                terminated_in_season=2025,
            )
            await contract_repo.create_dead_cap_charge(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=waived_player,
                source_contract_id=waived_contract.id,
                season=2026,
                amount=6_500_000,
                reason=DeadCapReason.WAIVE_GUARANTEED,
                created_in_season=2025,
            )

            summary = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2026
            )
            assert summary.team_id == team_id
            assert summary.season == 2026
            assert summary.salary_cap == 150_000_000
            assert summary.committed_salary == 20_000_000
            assert summary.dead_cap == 6_500_000
            assert summary.include_holds is False
            assert summary.used == 26_500_000
            assert summary.room == 150_000_000 - 26_500_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_cap_summary_include_holds_toggle():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "HLD")
            fa_player = await _make_player(pool, league_id, None)

            await contract_repo.create_cap_hold(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=fa_player,
                season=2026,
                amount=9_000_000,
                hold_type="cap_hold_free_agent",
            )

            without_holds = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2026, include_holds=False
            )
            # cap_holds is always reported, but excluded from used/room when
            # include_holds is False.
            assert without_holds.cap_holds == 9_000_000
            assert without_holds.used == 0
            assert without_holds.room == 150_000_000

            with_holds = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2026, include_holds=True
            )
            assert with_holds.cap_holds == 9_000_000
            assert with_holds.used == 9_000_000
            assert with_holds.room == 150_000_000 - 9_000_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
