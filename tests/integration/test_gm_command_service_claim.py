"""P5-99.t: every human-initiated roster/settings write refuses while Stage E's
advance claim is held, and writes nothing.

One case per function, all seven `gm_command_service` write paths named
(phase-5-stage-e-advance.md P5-99.t.1). They are separate cases rather than one
parametrised body on purpose: each function has a different write to prove
absent, and each is the target of its own must-fire mutation (remove that
function's claim check and only its case may go red).

The claim is set by a plain committed `UPDATE leagues` because nothing else is
needed to hold it: the check reads `advance_claimed_at`, not who set it.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import pytest

from dba2.core.errors import ConcurrencyError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    contract_repo,
    player_repo,
    role_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import ContractStatus, SigningMethod
from dba2.domain.player import RosterStatus
from dba2.domain.role import ShotDiet
from dba2.domain.team import DefensiveScheme, OffensiveScheme, Timeline
from dba2.domain.transaction import TransactionType
from dba2.services import gm_command_service, league_claim
from tests.integration.test_gm_command_service import (
    _MANAGER,
    _make_active_contract,
    _make_league,
    _make_player,
    _make_team,
)


async def _hold_the_claim(pool, league_id: int) -> None:
    await pool.execute(
        "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 WHERE id = $1",
        league_id,
    )


async def test_waive_player_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CW1")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
            assert player.team_id == team_id
            contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            assert [c.status for c in contracts] == [ContractStatus.ACTIVE]
            dead_cap = await pool.fetchval(
                "SELECT COUNT(*) FROM dead_cap_charges WHERE league_id = $1", league_id
            )
            assert dead_cap == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_release_player_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CR1")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.release_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
            assert player.team_id == team_id
            contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            assert [c.status for c in contracts] == [ContractStatus.ACTIVE]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_sign_free_agent_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CS1")
            player_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                    years=2,
                    annual_salary=4_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
            assert player.team_id is None
            assert await contract_repo.list_contracts_for_player(pool, player_id) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_set_strategy_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CT1")
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.set_strategy(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    pace_target=55,
                    offensive_scheme=OffensiveScheme.PNR_HEAVY,
                    defensive_scheme=DefensiveScheme.SWITCH_ALL,
                    help_aggression=60,
                    three_point_bias=70,
                    crash_oreb_bias=40,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            assert await team_repo.get_team_strategy(pool, team_id, 2026) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_set_role_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CO1")
            player_id = await _make_player(pool, league_id, team_id)
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.set_role(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    player_id=player_id,
                    role="primary_option",
                    minutes=0.14 * 240.0,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            assert await role_repo.list_player_roles_for_team(pool, team_id, 2026) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_set_directive_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CD1")
            player_id = await _make_player(pool, league_id, team_id)
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.set_directive(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    player_id=player_id,
                    shot_diet=ShotDiet.MORE_THREES,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            assert await role_repo.get_player_directive(pool, league_id, player_id) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_set_timeline_refuses_while_the_claim_is_held_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CL1")
            before = await team_repo.get_team(pool, team_id)
            await _hold_the_claim(pool, league_id)

            with pytest.raises(ConcurrencyError) as excinfo:
                await gm_command_service.set_timeline(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    timeline=Timeline.WIN_NOW,
                )
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            after = await team_repo.get_team(pool, team_id)
            assert (after.timeline, after.timeline_source) == (
                before.timeline,
                before.timeline_source,
            )
            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            assert [t for t in transactions if t.txn_type == TransactionType.TIMELINE_SET] == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
