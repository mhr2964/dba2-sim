"""Live I/O proof for `services/capsheet_service.py::project_league_cap_sheets`
-- §8's cap-sheet projection (Decision P2-15), §12 step 7's own verify list:

- season-0 equals `cap_service.get_cap_summary` exactly (Decision P2-17)
- a future season with no `league_cap_settings` row is flagged `projected`
  (Decision P2-16)
- `minimum_filler` is nonzero for a 12-man roster, through the full bulk-
  loader path, not just the unit-level formula
  (`tests/unit/managers/test_capsheet.py` already covers the formula in
  isolation; this is the same claim proven against real committed-salary/
  dead-cap/contract-year rows read back out of Postgres)

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
from dba2.managers.tuning import DEFAULT_TUNING
from dba2.services import cap_service, capsheet_service


async def _make_league(pool, *, current_season: int = 2026) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Cap Sheet Service Test League",
        start_season_year=current_season,
        current_season=current_season,
        commissioner_user_id=1,
        rng_seed=1,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=current_season,
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
        last_name="Sheet",
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


async def _make_contract_year(
    pool, league_id: int, team_id: int, player_id: int, season: int, salary: int
) -> None:
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=season,
        start_season=season,
        end_season=season,
        total_years=1,
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    await contract_repo.create_contract_year(
        pool, contract_id=contract.id, season=season, salary=salary, guaranteed_amount=salary
    )


@pytest.mark.asyncio
async def test_project_league_cap_sheets_raises_not_found_with_no_cap_settings_row():
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
            with pytest.raises(NotFoundError):
                await capsheet_service.project_league_cap_sheets(pool, league_id=league.id)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_project_league_cap_sheets_season_0_matches_get_cap_summary_exactly():
    """Decision P2-17: the season-0 row must equal `cap_service.
    get_cap_summary` for the same team/season, to the dollar.

    Item 8's `used` formula folds `minimum_filler` in for *every* season,
    including season 0 -- `get_cap_summary` has no such term (Phase 0 never
    modeled roster-fill cost), so the two can only agree exactly when
    season 0's own roster already meets `roster_min` (14, this fixture's
    `CAP_SETTINGS`-equivalent `create_cap_settings` call above), making
    `minimum_filler` zero on both sides. This fixture signs a full 14-man
    roster for exactly that reason -- not a simplification, the condition
    the cross-check is actually valid under."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "P17")
            active_player = await _make_player(pool, league_id, team_id)
            waived_player = await _make_player(pool, league_id, team_id)

            await _make_contract_year(pool, league_id, team_id, active_player, 2026, 22_500_000)
            # 13 more min-ish-salary players to bring roster_count to 14
            # (== roster_min), so minimum_filler is 0 on both sides.
            for i in range(13):
                filler_player = await _make_player(pool, league_id, team_id)
                await _make_contract_year(
                    pool, league_id, team_id, filler_player, 2026, 1_200_000 + i * 1_000
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
                amount=4_100_000,
                reason=DeadCapReason.WAIVE_GUARANTEED,
                created_in_season=2025,
            )

            projections = await capsheet_service.project_league_cap_sheets(
                pool, league_id=league_id
            )
            season_0 = next(p for p in projections[team_id] if p.season == 2026)

            summary = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2026
            )

            assert season_0.team_id == summary.team_id
            assert season_0.season == summary.season
            assert season_0.committed_salary == summary.committed_salary
            assert season_0.dead_cap == summary.dead_cap
            assert season_0.cap_holds == summary.cap_holds
            assert season_0.used == summary.used
            assert season_0.room == summary.salary_cap - summary.used
            assert season_0.room == summary.room
            assert season_0.projected is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_project_league_cap_sheets_flags_future_seasons_with_no_real_row_projected():
    """Decision P2-16: seasons 1-4 of the horizon, which have no real
    `league_cap_settings` row in this fixture, must be `projected=True` and
    grown at `CAP_GROWTH` per season from the one real (season-0) row.

    This fixture signs no contracts at all, so `roster_count` is 0 in every
    season and `minimum_filler` (item 7) is `roster_min * min_salary` --
    `min_salary` itself grows with `CAP_GROWTH` too (a projected season's
    minimum salary is a dollar figure, `project_cap_settings` grows it same
    as `salary_cap`), so the expected `room` below nets both effects out
    rather than only checking `salary_cap`'s own growth in isolation."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "P16")
            projections = await capsheet_service.project_league_cap_sheets(
                pool, league_id=league_id
            )
            rows = sorted(projections[team_id], key=lambda r: r.season)
            assert len(rows) == DEFAULT_TUNING.horizon == 5
            assert rows[0].season == 2026
            assert rows[0].projected is False
            # roster_min is 14 (see _make_league's create_cap_settings call).
            assert rows[0].minimum_filler == 14 * 1_200_000
            assert rows[0].room == 150_000_000 - rows[0].minimum_filler

            for i, row in enumerate(rows[1:], start=1):
                assert row.season == 2026 + i
                assert row.projected is True
                growth = DEFAULT_TUNING.cap_growth**i
                expected_cap = round(150_000_000 * growth)
                expected_min_salary = round(1_200_000 * growth)
                expected_filler = 14 * expected_min_salary
                assert row.minimum_filler == expected_filler
                assert row.room == expected_cap - expected_filler
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_project_league_cap_sheets_minimum_filler_nonzero_for_a_12_man_roster():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "P07")
            for i in range(12):
                player_id = await _make_player(pool, league_id, team_id)
                await _make_contract_year(
                    pool, league_id, team_id, player_id, 2026, 3_000_000 + i * 10_000
                )

            projections = await capsheet_service.project_league_cap_sheets(
                pool, league_id=league_id
            )
            season_0 = next(p for p in projections[team_id] if p.season == 2026)
            assert season_0.roster_count == 12
            assert season_0.minimum_filler == 2 * 1_200_000
            assert season_0.minimum_filler > 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_project_league_cap_sheets_covers_every_team_in_the_league():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "TAA")
            team_b = await _make_team(pool, league_id, "TAB")
            projections = await capsheet_service.project_league_cap_sheets(
                pool, league_id=league_id
            )
            assert set(projections) == {team_a, team_b}
            assert len(projections[team_a]) == DEFAULT_TUNING.horizon
            assert len(projections[team_b]) == DEFAULT_TUNING.horizon
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
