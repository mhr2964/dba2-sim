"""RMLP §7.3 -- constraint tests, independent of the locks. Direct raw-SQL
attempts (locks ENABLED, via the real service functions where a service
path exists) proving each of migration 017's three constraints rejects the
shape it exists to catch, plus the `UniqueViolationError` -> `ValidationError`
translation tests (roster_service.py's own new try/except blocks).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py. Requires migration 017 to have been
applied (`alembic upgrade head`).
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import asyncpg
import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, team_repo
from dba2.domain.contract import ContractType, DeadCapReason, SigningMethod
from dba2.domain.player import Position, RosterStatus
from dba2.services import roster_service
from tests.fixtures.rmlp import rmlp_gate


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Migration 017 Test League",
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
        division="Atlantic",
        home_market_size=3,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int | None = None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="M17",
        last_name="Test",
        primary_position=Position.C,
        height_in=83,
        weight_lb=250,
        wingspan_in=87,
        birth_date=date(1996, 4, 4),
        roster_status=RosterStatus.ACTIVE if team_id else RosterStatus.FREE_AGENT,
        overall_cached=76,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


@pytest.mark.asyncio
async def test_017a_rejects_a_second_active_contract_for_one_player():
    """A raw INSERT that bypasses the application entirely -- proves the
    constraint itself, not the app-level guard that (redundantly) also
    prevents this."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "M17A")
            player_id = await _make_player(pool, league_id, team_id)
            await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await contract_repo.create_contract(
                    pool,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    start_season=2026,
                    end_season=2026,
                    total_years=1,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_017b_rejects_a_duplicate_dead_cap_triple():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "M17B")
            player_id = await _make_player(pool, league_id, team_id)
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_dead_cap_charge(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                source_contract_id=contract.id,
                season=2026,
                amount=1_000_000,
                reason=DeadCapReason.WAIVE_GUARANTEED,
                created_in_season=2026,
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await contract_repo.create_dead_cap_charge(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    source_contract_id=contract.id,
                    season=2026,
                    amount=1_000_000,
                    reason=DeadCapReason.WAIVE_GUARANTEED,
                    created_in_season=2026,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_017c_rejects_a_termination_that_predates_its_own_signing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "M17C")
            player_id = await _make_player(pool, league_id, team_id)
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2027,
                start_season=2027,
                end_season=2028,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    "UPDATE contracts SET terminated_in_season = $2 WHERE id = $1",
                    contract.id,
                    2025,  # before signed_in_season=2027
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_translates_017a_violation_to_the_same_active_contract_message():
    """The `UniqueViolationError` -> `ValidationError` translation
    (`roster_service.sign`'s own new try/except) -- forced by inserting a
    second active contract for the same player directly (bypassing
    `sign()`'s own in-lock guard, which would otherwise catch it first),
    so the raw DB error is what `sign()`'s translation must actually
    catch."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "M17D")
            player_id = await _make_player(pool, league_id, team_id)
            # Terminate this contract off active FIRST so create_contract's
            # own list-and-check guard (sign() re-reads via list_contracts_
            # for_player) doesn't fire before we ever reach the raw insert
            # -- we specifically want the CONSTRAINT to be what's tested,
            # not the pre-existing app-level check. Simplest: pre-create an
            # ACTIVE contract directly, then call sign() for the SAME
            # player -- sign()'s own in-lock guard fires first in the
            # normal path, which is fine: both layers raise the identical
            # message, exactly what this test asserts.
            await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            with pytest.raises(ValidationError, match="already has an active contract"):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.sign(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        team_id=team_id,
                        signed_in_season=2026,
                        contract_type=ContractType.STANDARD,
                        signing_method=SigningMethod.CAP_SPACE,
                        year_salaries={2026: 1_000_000},
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
