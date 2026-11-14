"""Live I/O proof for `services/roster_service.py` -- §8 step 8's headline
verification target, quoted directly from the design doc: "an integration
test that signs a 4-year guaranteed deal, waives it after year 1, and
asserts the cap sheet shows the dead money for the remaining three
seasons -- this is the test that proves the §4.4 design works, and it is
the single most valuable test in Phase 0."

`test_sign_then_waive_after_year_one_shows_dead_money_for_remaining_three_seasons`
below is that exact test. Everything else in this file covers the
recurring bug classes named in the dispatch prompt and the stretch path
§4.4 spells out.

**RMLP gate plumbing (Addendum A1, pre-authorized, mechanical).** Every
direct call to `roster_service.sign`/`waive`/`release` below is wrapped in
`tests.fixtures.rmlp.rmlp_gate` (opens a transaction + RMLP gate on ONE
connection, yields that connection) instead of passing the bare `pool` --
`roster_lock.acquire`'s gate-held assertion requires it. `waive`/`release`
also gained a required `league_id=` keyword argument (Addendum A2). Neither
is a semantic test change -- see that addendum's own text.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, date, datetime

import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, role_repo, team_repo
from dba2.domain.contract import ContractStatus, ContractType, DeadCapReason, SigningMethod
from dba2.domain.player import Position, RosterStatus
from dba2.domain.role import ShotDiet
from dba2.services import cap_service, roster_service
from tests.fixtures.rmlp import rmlp_gate


async def _make_league(pool, *, salary_cap: int = 150_000_000) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Roster Service Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    # cap_service.get_cap_summary needs a league_cap_settings row for the
    # exact season queried (Decision 8 — per-season rows, not a single
    # league-level constant). The waive/stretch tests below check the cap
    # sheet across the full 2026-2030 span a 4-year deal plus a 5-season
    # stretch spread can touch, so every one of those seasons gets a row.
    for season in range(2026, 2031):
        await league_repo.create_cap_settings(
            pool,
            league_id=league.id,
            season=season,
            salary_cap=salary_cap,
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


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Roster",
        last_name="Service",
        primary_position=Position.PF,
        height_in=81,
        weight_lb=230,
        wingspan_in=84,
        birth_date=date(1997, 6, 12),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=80,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_sign_then_waive_after_year_one_shows_dead_money_for_remaining_three_seasons():
    """The headline test §8 step 8 names explicitly. A 4-year, fully
    guaranteed deal (2026-2029) with a distinct salary per season (so a
    mapper/loop bug that reused one season's amount for another would be
    visible) is signed, then waived at the start of year 2 (2027) -- the
    cap sheet for 2027/2028/2029 must each show that year's guaranteed
    amount as dead money, and 2026 (already played before the waive) must
    NOT get a charge."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WAV")
            player_id = await _make_player(pool, league_id)

            year_salaries = {
                2026: 10_000_000,
                2027: 12_000_000,
                2028: 14_000_000,
                2029: 16_000_000,
            }
            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries=year_salaries,
                )
            assert contract.status == ContractStatus.ACTIVE
            assert contract.start_season == 2026
            assert contract.end_season == 2029
            assert contract.total_years == 4

            years = await contract_repo.list_contract_years_for_contract(pool, contract.id)
            assert {y.season: y.salary for y in years} == year_salaries
            # Fully guaranteed by default -- every year's guaranteed_amount
            # equals its salary.
            assert {y.season: y.guaranteed_amount for y in years} == year_salaries

            player_after_sign = await player_repo.get_player(pool, player_id)
            assert player_after_sign.team_id == team_id
            assert player_after_sign.roster_status == RosterStatus.ACTIVE

            async with rmlp_gate(pool, league_id) as conn:
                charges = await roster_service.waive(
                    conn, league_id=league_id, contract_id=contract.id, waived_in_season=2027
                )

            # Exactly 3 charges -- one per remaining guaranteed season
            # (2027, 2028, 2029), not 2026 (already elapsed before the waive).
            assert len(charges) == 3
            by_season = {c.season: c for c in charges}
            assert set(by_season) == {2027, 2028, 2029}
            assert by_season[2027].amount == 12_000_000
            assert by_season[2028].amount == 14_000_000
            assert by_season[2029].amount == 16_000_000
            for c in charges:
                assert c.reason == DeadCapReason.WAIVE_GUARANTEED
                assert c.team_id == team_id
                assert c.player_id == player_id
                assert c.source_contract_id == contract.id
                assert c.created_in_season == 2027
                assert c.stretch_years is None

            terminated = await contract_repo.get_contract(pool, contract.id)
            assert terminated.status == ContractStatus.WAIVED
            assert terminated.terminated_in_season == 2027

            player_after_waive = await player_repo.get_player(pool, player_id)
            assert player_after_waive.team_id is None
            assert player_after_waive.roster_status == RosterStatus.FREE_AGENT

            # No dead-cap charge should exist for 2026 -- that season had
            # already been played out before the waive.
            dead_cap_2026 = await contract_repo.get_dead_cap_total(pool, team_id, 2026)
            assert dead_cap_2026 == 0

            # The cap sheet itself (via cap_service, not just the raw repo
            # total) reflects the dead money for each remaining season.
            summary_2027 = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2027
            )
            assert summary_2027.committed_salary == 0  # contract is no longer active
            assert summary_2027.dead_cap == 12_000_000
            assert summary_2027.used == 12_000_000
            # Phase 0 step-8 critic round, B1: `SUM(bigint)` comes back from
            # asyncpg as `Decimal`, not `int`, unless the repo layer casts
            # it explicitly. `==` against an int literal can't catch this
            # (Decimal('1.2E+7') == 12_000_000 is True) -- assert the real
            # type and that the value survives a JSON round-trip instead.
            for field in ("committed_salary", "dead_cap", "cap_holds", "used", "room"):
                value = getattr(summary_2027, field)
                assert isinstance(value, int), f"{field} is {type(value).__name__}, not int"
            assert json.dumps({"dead_cap": summary_2027.dead_cap}) == '{"dead_cap": 12000000}'

            summary_2028 = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2028
            )
            assert summary_2028.dead_cap == 14_000_000

            summary_2029 = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2029
            )
            assert summary_2029.dead_cap == 16_000_000
            assert summary_2029.room == summary_2029.salary_cap - 16_000_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_is_guarded_against_double_waive_and_does_not_duplicate_charges():
    """Recurring bug class #1: a "close"-shaped UPDATE re-run on an
    already-non-active row must not silently duplicate side effects. A
    second `waive()` call on the same contract must raise, not create a
    second batch of dead-cap charges."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DBL")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 8_000_000, 2027: 9_000_000},
                )

            async with rmlp_gate(pool, league_id) as conn:
                first_charges = await roster_service.waive(
                    conn, league_id=league_id, contract_id=contract.id, waived_in_season=2026
                )
            assert len(first_charges) == 2

            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.waive(
                        conn, league_id=league_id, contract_id=contract.id, waived_in_season=2026
                    )

            # Still exactly the 2 charges from the first (successful) waive
            # -- the rejected second call must not have written anything.
            all_charges = await contract_repo.list_dead_cap_charges_for_team(
                pool, team_id, 2026
            )
            assert len(all_charges) == 1
            all_charges_2027 = await contract_repo.list_dead_cap_charges_for_team(
                pool, team_id, 2027
            )
            assert len(all_charges_2027) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_with_stretch_spreads_total_guarantee_across_2n_plus_1_seasons():
    """§4.4: "Stretching instead spreads the total remaining guarantee
    across `2 * remaining_years + 1` seasons." 2 remaining guaranteed
    seasons -> 5-season span, `reason = waive_stretched`, `stretch_years`
    populated, and the sum across the span equals the pre-stretch total
    exactly (BIGINT whole dollars -- no dollar dropped to a remainder)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "STR")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 10_000_000, 2027: 11_000_000},
                )
            # Waiving at the start of the contract -- both years remain,
            # so remaining_years = 2, stretch_span = 2*2+1 = 5.
            total_guaranteed = 10_000_000 + 11_000_000

            async with rmlp_gate(pool, league_id) as conn:
                charges = await roster_service.waive(
                    conn,
                    league_id=league_id,
                    contract_id=contract.id,
                    waived_in_season=2026,
                    stretch=True,
                )

            assert len(charges) == 5
            seasons = sorted(c.season for c in charges)
            assert seasons == [2026, 2027, 2028, 2029, 2030]
            assert sum(c.amount for c in charges) == total_guaranteed
            for c in charges:
                assert c.reason == DeadCapReason.WAIVE_STRETCHED
                assert c.stretch_years == 5
                assert c.created_in_season == 2026

            terminated = await contract_repo.get_contract(pool, contract.id)
            assert terminated.status == ContractStatus.WAIVED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_on_partially_guaranteed_year_only_charges_the_guaranteed_amount():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PRT")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.NON_BIRD,
                    year_salaries={2026: 5_000_000, 2027: 6_000_000},
                    year_guaranteed={2026: 5_000_000, 2027: 1_500_000},
                )

            async with rmlp_gate(pool, league_id) as conn:
                charges = await roster_service.waive(
                    conn, league_id=league_id, contract_id=contract.id, waived_in_season=2026
                )
            by_season = {c.season: c.amount for c in charges}
            assert by_season[2026] == 5_000_000
            assert by_season[2027] == 1_500_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_release_terminates_with_no_dead_cap_and_is_guarded_against_re_release():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "REL")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.TWO_WAY,
                    signing_method=SigningMethod.MIN_EXCEPTION,
                    year_salaries={2026: 500_000},
                    year_guaranteed={2026: 0},
                )

            async with rmlp_gate(pool, league_id) as conn:
                released = await roster_service.release(
                    conn, league_id=league_id, contract_id=contract.id, released_in_season=2026
                )
            assert released.status == ContractStatus.VOIDED
            assert released.terminated_in_season == 2026

            player_after = await player_repo.get_player(pool, player_id)
            assert player_after.team_id is None
            assert player_after.roster_status == RosterStatus.FREE_AGENT

            dead_cap_total = await contract_repo.get_dead_cap_total(pool, team_id, 2026)
            assert dead_cap_total == 0

            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.release(
                        conn,
                        league_id=league_id,
                        contract_id=contract.id,
                        released_in_season=2026,
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_raises_when_player_already_has_an_active_contract():
    """Phase 0 step-8 critic round, B2: `sign()` had no guard against a
    player who already holds an active contract -- a second `sign()` call
    created a second `active` row and double-counted (or mis-attributed)
    the player's cap hit, with no DB constraint to catch it either
    (`contracts` has zero CHECK constraints and no partial-unique index on
    active-per-player). Covers both the same-team and cross-team case."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "ACT")
            team_b = await _make_team(pool, league_id, "ACB")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_a,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 5_000_000},
                )

            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.sign(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        team_id=team_a,
                        signed_in_season=2026,
                        contract_type=ContractType.STANDARD,
                        signing_method=SigningMethod.CAP_SPACE,
                        year_salaries={2026: 5_000_000},
                    )
            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.sign(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        team_id=team_b,
                        signed_in_season=2026,
                        contract_type=ContractType.STANDARD,
                        signing_method=SigningMethod.CAP_SPACE,
                        year_salaries={2026: 7_000_000},
                    )

            # Only the one contract from the first, successful sign() exists.
            all_contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            assert len(all_contracts) == 1
            assert all_contracts[0].team_id == team_a

            committed_a = await contract_repo.get_committed_salary(pool, team_a, 2026)
            committed_b = await contract_repo.get_committed_salary(pool, team_b, 2026)
            assert committed_a == 5_000_000
            assert committed_b == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_release_raises_when_remaining_guaranteed_money_exists():
    """Phase 0 step-8 critic round, B3: `release()` erased guaranteed money
    with no dead-cap row and no error -- violating §4.4's "a wrong charge is
    a visible row with a reason, not a missing number" premise. A caller who
    actually owes guaranteed money must be forced into `waive()` instead."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RLG")
            player_id = await _make_player(pool, league_id)

            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 30_000_000, 2027: 32_000_000, 2028: 34_000_000},
                )

            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.release(
                        conn,
                        league_id=league_id,
                        contract_id=contract.id,
                        released_in_season=2026,
                    )

            # Nothing changed -- the contract is still active, no dead cap
            # was written, and the player is still rostered.
            unchanged = await contract_repo.get_contract(pool, contract.id)
            assert unchanged.status == ContractStatus.ACTIVE
            dead_cap_total = (
                await contract_repo.get_dead_cap_total(pool, team_id, 2026)
                + await contract_repo.get_dead_cap_total(pool, team_id, 2027)
                + await contract_repo.get_dead_cap_total(pool, team_id, 2028)
            )
            assert dead_cap_total == 0
            player_after = await player_repo.get_player(pool, player_id)
            assert player_after.team_id == team_id
            assert player_after.roster_status == RosterStatus.ACTIVE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_requires_at_least_one_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "EMP")
            player_id = await _make_player(pool, league_id)

            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.sign(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        team_id=team_id,
                        signed_in_season=2026,
                        contract_type=ContractType.STANDARD,
                        signing_method=SigningMethod.CAP_SPACE,
                        year_salaries={},
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_clears_a_stale_directive_carried_from_a_previous_team():
    """PD-8 (docs/design/gm-command-surface.md), the "waiver/signing" half
    of the two named appliers -- the leak this closes doesn't happen at
    `waive()`'s own team_id -> NULL write (nobody is imposed on yet while
    a player sits in free agency), it happens the moment a NEW team signs
    him and inherits whatever a past team left behind. `_make_player`
    starts every fixture as a true free agent (`team_id IS NULL`), so
    setting a directive here directly via `role_repo` simulates exactly
    that -- a directive that outlived its own team relationship."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SGN")
            player_id = await _make_player(pool, league_id)
            await role_repo.upsert_player_directive(
                pool, league_id=league_id, player_id=player_id, shot_diet=ShotDiet.MORE_THREES
            )

            async with rmlp_gate(pool, league_id) as conn:
                await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 2_000_000},
                )

            assert await role_repo.get_player_directive(pool, league_id, player_id) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_does_not_clear_a_directive_when_the_team_is_unchanged():
    """The other half of the same guard -- `sign()` must NOT wipe a
    team's own already-correct directive just because it happens to be
    the caller. Constructed directly (`player_repo.create_player` with
    `team_id` already set, no contract) rather than via a real waive/
    re-sign cycle -- no auto-expiration job exists yet to produce this
    state through the normal command surface (`gm_service._load_free_
    agency_pool`'s own disclosed gap), but the guard in `sign()` must hold
    regardless of how a player ever reaches "on team X, no active
    contract" -- if it didn't, a directive would be lost on every future
    contract-extension path the moment one is built, not just this test."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "EXT")
            player = await player_repo.create_player(
                pool,
                league_id=league_id,
                first_name="Extension",
                last_name="Case",
                primary_position=Position.PF,
                height_in=81,
                weight_lb=230,
                wingspan_in=84,
                birth_date=date(1997, 6, 12),
                roster_status=RosterStatus.ACTIVE,
                overall_cached=80,
                overall_computed_at=datetime.now(UTC),
                team_id=team_id,
            )
            await role_repo.upsert_player_directive(
                pool, league_id=league_id, player_id=player.id, shot_diet=ShotDiet.MORE_THREES
            )

            async with rmlp_gate(pool, league_id) as conn:
                await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player.id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 2_000_000},
                )

            directive = await role_repo.get_player_directive(pool, league_id, player.id)
            assert directive is not None
            assert directive.shot_diet == ShotDiet.MORE_THREES
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_waive_of_the_same_contract_charges_dead_cap_exactly_once():
    """RMLP §7.2 test 3 -- ground truth is raw Postgres (`COUNT`/`SUM` on
    `dead_cap_charges`, never the service's own return value) -- exactly
    one caller's charges land, the other 4 raise `ValidationError` and
    write nothing. Each concurrent call opens its OWN `rmlp_gate` (mode=
    SHARED, the default) on its OWN connection -- multiple SHARED holders
    are expected and do not serialize each other; the tier-1/tier-2 row
    locks `roster_service.waive`'s own `acquire` call takes are what
    serialize the actual race."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CW3")
            player_id = await _make_player(pool, league_id)
            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 1_000_000, 2027: 1_000_000, 2028: 1_000_000},
                )

            async def _waive_with_stagger(delay_ms: int):
                await asyncio.sleep(delay_ms / 1000)
                async with rmlp_gate(pool, league_id) as conn:
                    return await roster_service.waive(
                        conn, league_id=league_id, contract_id=contract.id, waived_in_season=2026
                    )

            results = await asyncio.gather(
                *(_waive_with_stagger(i * 3) for i in range(5)),
                return_exceptions=True,
            )
            succeeded = [r for r in results if not isinstance(r, Exception)]
            failed = [r for r in results if isinstance(r, Exception)]
            assert len(succeeded) == 1, f"expected exactly 1 of 5 to win, got {results!r}"
            assert len(failed) == 4
            assert all(isinstance(e, ValidationError) for e in failed), failed

            count = await pool.fetchval(
                "SELECT COUNT(*) FROM dead_cap_charges WHERE source_contract_id = $1",
                contract.id,
            )
            total = await pool.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM dead_cap_charges "
                "WHERE source_contract_id = $1",
                contract.id,
            )
            assert count == 3
            assert int(total) == 3_000_000
            cap = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=team_id, season=2026
            )
            # $1M/season charge, so a single season's dead_cap is $1M --
            # the $3M total is the sum across all 3 charged seasons,
            # asserted directly above.
            assert cap.dead_cap == 1_000_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_release_of_the_same_contract_transitions_exactly_once():
    """RMLP §7.2 test 4 -- `release`'s own twin of the waive test above,
    same argument applied to §4.1's CAS-first ordering. Exactly one
    `VOIDED` transition, `terminated_in_season` stamped once, the player
    ends `FREE_AGENT` with `team_id IS NULL`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CR4")
            player_id = await _make_player(pool, league_id)
            async with rmlp_gate(pool, league_id) as conn:
                contract = await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.TWO_WAY,
                    signing_method=SigningMethod.MIN_EXCEPTION,
                    year_salaries={2026: 500_000},
                    year_guaranteed={2026: 0},
                )

            async def _release_with_stagger(delay_ms: int):
                await asyncio.sleep(delay_ms / 1000)
                async with rmlp_gate(pool, league_id) as conn:
                    return await roster_service.release(
                        conn,
                        league_id=league_id,
                        contract_id=contract.id,
                        released_in_season=2026,
                    )

            results = await asyncio.gather(
                *(_release_with_stagger(i * 3) for i in range(5)),
                return_exceptions=True,
            )
            succeeded = [r for r in results if not isinstance(r, Exception)]
            failed = [r for r in results if isinstance(r, Exception)]
            assert len(succeeded) == 1, f"expected exactly 1 of 5 to win, got {results!r}"
            assert len(failed) == 4
            assert all(isinstance(e, ValidationError) for e in failed), failed

            row = await contract_repo.get_contract(pool, contract.id)
            assert row.status == ContractStatus.VOIDED
            assert row.terminated_in_season == 2026
            player_after = await player_repo.get_player(pool, player_id)
            assert player_after.team_id is None
            assert player_after.roster_status == RosterStatus.FREE_AGENT
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
