"""Live I/O proof for `data/repositories/contract_repo.py` — smoke coverage
for contracts, contract_years, dead_cap_charges, cap_holds, and the
league-independent `rookie_scale` lookup table, plus the three cap-summary
join functions (`get_committed_salary`, `get_dead_cap_total`,
`get_cap_hold_total`) §5 requires this module expose as single calls.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, team_repo
from dba2.domain.contract import (
    ContractStatus,
    ContractType,
    DeadCapReason,
    OptionDecision,
    OptionType,
    SigningMethod,
)
from dba2.domain.player import Position, RosterStatus

# `seeds/` is a sibling of the `dba2/` package (§3's project layout), same
# derivation `dba2.services.league_service` uses for its own seed loads.
_SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Contract Repo Test League",
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
        first_name="Contract",
        last_name="Player",
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
async def test_create_get_contract_and_list_for_team_and_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CTA")
            player_id = await _make_player(pool, league_id, team_id)

            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2028,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            assert contract.status == ContractStatus.ACTIVE

            fetched = await contract_repo.get_contract(pool, contract.id)
            assert fetched is not None
            assert fetched.player_id == player_id

            for_team = await contract_repo.list_contracts_for_team(pool, team_id)
            assert [c.id for c in for_team] == [contract.id]

            for_team_filtered = await contract_repo.list_contracts_for_team(
                pool, team_id, status=ContractStatus.ACTIVE
            )
            assert [c.id for c in for_team_filtered] == [contract.id]

            for_team_wrong_status = await contract_repo.list_contracts_for_team(
                pool, team_id, status=ContractStatus.WAIVED
            )
            assert for_team_wrong_status == []

            for_player = await contract_repo.list_contracts_for_player(pool, player_id)
            assert [c.id for c in for_player] == [contract.id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_contract_round_trips_every_field_distinctly():
    """Every `Contract` field gets a distinct value so a mapper that swapped
    two same-typed fields (e.g. `start_season`/`end_season`, both `int`)
    would fail this test (Phase 0 step-7 critic round, B2 lesson)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DST")
            player_id = await _make_player(pool, league_id, team_id)

            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2021,
                start_season=2022,
                end_season=2025,
                total_years=4,
                contract_type=ContractType.MAX,
                signing_method=SigningMethod.BIRD,
                no_trade_clause=True,
                trade_kicker_pct=7,
                status=ContractStatus.ACTIVE,
            )
            fetched = await contract_repo.get_contract(pool, contract.id)
            assert fetched.league_id == league_id
            assert fetched.player_id == player_id
            assert fetched.team_id == team_id
            assert fetched.signed_in_season == 2021
            assert fetched.start_season == 2022
            assert fetched.end_season == 2025
            assert fetched.total_years == 4
            assert fetched.contract_type == ContractType.MAX
            assert fetched.signing_method == SigningMethod.BIRD
            assert fetched.no_trade_clause is True
            assert fetched.trade_kicker_pct == 7
            assert fetched.status == ContractStatus.ACTIVE
            assert fetched.terminated_in_season is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_terminate_contract_is_a_noop_on_an_already_terminated_contract():
    """`terminate_contract` must not clobber `terminated_in_season` on a
    second call — mirrors `team_repo.close_manager_assignment`'s guard
    (Phase 0 step-7 critic round, B1's bug class)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TRM")
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
                contract_type=ContractType.VETERAN_MIN,
                signing_method=SigningMethod.MIN_EXCEPTION,
            )
            await contract_repo.terminate_contract(
                pool, contract.id, status=ContractStatus.WAIVED, terminated_in_season=2026
            )
            first = await contract_repo.get_contract(pool, contract.id)
            assert first.status == ContractStatus.WAIVED
            assert first.terminated_in_season == 2026

            # Second call, with a different season, must not overwrite.
            await contract_repo.terminate_contract(
                pool, contract.id, status=ContractStatus.BOUGHT_OUT, terminated_in_season=2027
            )
            second = await contract_repo.get_contract(pool, contract.id)
            assert second.status == ContractStatus.WAIVED
            assert second.terminated_in_season == 2026
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_contract_year_create_get_list_and_set_option_decision():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CYR")
            player_id = await _make_player(pool, league_id, team_id)
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2027,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.NON_BIRD,
            )
            year1 = await contract_repo.create_contract_year(
                pool,
                contract_id=contract.id,
                season=2026,
                salary=5_000_000,
                guaranteed_amount=5_000_000,
            )
            assert year1.option_type == OptionType.NONE
            assert year1.option_decision == OptionDecision.PENDING

            year2 = await contract_repo.create_contract_year(
                pool,
                contract_id=contract.id,
                season=2027,
                salary=6_500_000,
                guaranteed_amount=1_000_000,
                option_type=OptionType.PLAYER,
                option_decision=OptionDecision.PENDING,
            )
            assert year2.salary == 6_500_000
            assert year2.guaranteed_amount == 1_000_000
            assert year2.option_type == OptionType.PLAYER

            fetched_year1 = await contract_repo.get_contract_year(pool, contract.id, 2026)
            assert fetched_year1.salary == 5_000_000

            years = await contract_repo.list_contract_years_for_contract(pool, contract.id)
            assert [y.season for y in years] == [2026, 2027]

            await contract_repo.set_option_decision(
                pool, contract.id, 2027, OptionDecision.EXERCISED
            )
            updated_year2 = await contract_repo.get_contract_year(pool, contract.id, 2027)
            assert updated_year2.option_decision == OptionDecision.EXERCISED

            assert await contract_repo.get_contract_year(pool, contract.id, 2099) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_committed_salary_sums_active_contract_years_for_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CAP")
            player1 = await _make_player(pool, league_id, team_id)
            player2 = await _make_player(pool, league_id, team_id)

            active = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player1,
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
                contract_id=active.id,
                season=2026,
                salary=8_000_000,
                guaranteed_amount=8_000_000,
            )

            waived = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player2,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
            )
            await contract_repo.create_contract_year(
                pool,
                contract_id=waived.id,
                season=2026,
                salary=3_000_000,
                guaranteed_amount=3_000_000,
            )

            # Only the active contract's 2026 salary should count.
            committed = await contract_repo.get_committed_salary(pool, team_id, 2026)
            assert committed == 8_000_000

            # A season with no contract_years rows sums to 0, not NULL.
            assert await contract_repo.get_committed_salary(pool, team_id, 2099) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_dead_cap_charge_create_list_and_total():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DCC")
            player_id = await _make_player(pool, league_id, team_id)
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2025,
                start_season=2025,
                end_season=2027,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
                terminated_in_season=2026,
            )

            # season != created_in_season (2026 vs 2025) so a mapper that
            # swapped the two (both `int`) would fail below (Phase 0
            # step-7 critic round, B3).
            charge1 = await contract_repo.create_dead_cap_charge(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                source_contract_id=contract.id,
                season=2026,
                amount=2_000_000,
                reason=DeadCapReason.WAIVE_GUARANTEED,
                created_in_season=2025,
            )
            assert charge1.league_id == league_id
            assert charge1.team_id == team_id
            assert charge1.player_id == player_id
            assert charge1.source_contract_id == contract.id
            assert charge1.season == 2026
            assert charge1.created_in_season == 2025
            assert charge1.reason == DeadCapReason.WAIVE_GUARANTEED
            assert charge1.stretch_years is None

            # RMLP migration 017b (`ux_dead_cap_one_charge_per_contract_
            # team_season`) forbids a second row for the SAME (source_
            # contract_id, team_id, season) triple -- exactly the bug-(c)
            # shape that constraint exists to kill. A second, DIFFERENT
            # player's contract on the same team/season is the realistic
            # multi-charge scenario (two players waived by the same team
            # in the same season) migration 017b's own docstring names as
            # safe -- this is what actually exercises `list_dead_cap_
            # charges_for_team`/`get_dead_cap_total`'s SUM-across-charges
            # behavior, which was this test's real point.
            player_id_2 = await _make_player(pool, league_id, team_id)
            contract_2 = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id_2,
                team_id=team_id,
                signed_in_season=2025,
                start_season=2025,
                end_season=2027,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
                terminated_in_season=2026,
            )
            charge2 = await contract_repo.create_dead_cap_charge(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id_2,
                source_contract_id=contract_2.id,
                season=2026,
                amount=1_500_000,
                reason=DeadCapReason.WAIVE_STRETCHED,
                created_in_season=2026,
                stretch_years=3,
            )
            assert charge2.stretch_years == 3

            charges = await contract_repo.list_dead_cap_charges_for_team(pool, team_id, 2026)
            assert {c.id for c in charges} == {charge1.id, charge2.id}

            total = await contract_repo.get_dead_cap_total(pool, team_id, 2026)
            assert total == 3_500_000

            assert await contract_repo.get_dead_cap_total(pool, team_id, 2099) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_cap_hold_create_list_and_total():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CHL")
            player_id = await _make_player(pool, league_id, None)

            hold = await contract_repo.create_cap_hold(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                season=2026,
                amount=4_500_000,
                hold_type="cap_hold_free_agent",
            )
            # team_id/player_id distinct identifiers so a mapper that
            # swapped the two (both `int`) would fail here (Phase 0
            # step-7 critic round, B3).
            assert hold.league_id == league_id
            assert hold.team_id == team_id
            assert hold.player_id == player_id
            assert hold.amount == 4_500_000
            assert hold.hold_type == "cap_hold_free_agent"

            holds = await contract_repo.list_cap_holds_for_team(pool, team_id, 2026)
            assert [h.id for h in holds] == [hold.id]

            total = await contract_repo.get_cap_hold_total(pool, team_id, 2026)
            assert total == 4_500_000

            assert await contract_repo.get_cap_hold_total(pool, team_id, 2099) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_committed_salary_for_league_seasons_matches_single_team_calls():
    """Phase 2 step 7's bulk loader must return the exact same totals
    `get_committed_salary` would, per team per season, batched into one
    query -- Decision P2-17 depends on the batched and single-call paths
    never being able to drift apart."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "BLA")
            team_b = await _make_team(pool, league_id, "BLB")
            player_a = await _make_player(pool, league_id, team_a)
            player_b = await _make_player(pool, league_id, team_b)

            contract_a = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_a,
                team_id=team_a,
                signed_in_season=2026,
                start_season=2026,
                end_season=2027,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_a.id, season=2026, salary=9_000_000,
                guaranteed_amount=9_000_000,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_a.id, season=2027, salary=9_500_000,
                guaranteed_amount=9_500_000,
            )

            contract_b = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_b,
                team_id=team_b,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_b.id, season=2026, salary=4_000_000,
                guaranteed_amount=4_000_000,
            )

            batched = await contract_repo.get_committed_salary_for_league_seasons(
                pool, league_id, [2026, 2027, 2028]
            )
            assert type(batched[(team_a, 2026)]) is int
            assert batched[(team_a, 2026)] == 9_000_000
            assert batched[(team_a, 2027)] == 9_500_000
            assert batched[(team_b, 2026)] == 4_000_000
            # 2028 has no rows for either team -- absent from the dict, per
            # the "sum to 0, not NULL" contract read via .get(key, 0).
            assert (team_a, 2028) not in batched
            assert (team_b, 2027) not in batched

            single_a_2026 = await contract_repo.get_committed_salary(pool, team_a, 2026)
            single_a_2027 = await contract_repo.get_committed_salary(pool, team_a, 2027)
            single_b_2026 = await contract_repo.get_committed_salary(pool, team_b, 2026)
            assert batched[(team_a, 2026)] == single_a_2026
            assert batched[(team_a, 2027)] == single_a_2027
            assert batched[(team_b, 2026)] == single_b_2026
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_dead_cap_totals_for_league_seasons_matches_single_team_calls():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "BDC")
            player_id = await _make_player(pool, league_id, team_id)
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=2025,
                start_season=2025,
                end_season=2027,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
                terminated_in_season=2026,
            )
            await contract_repo.create_dead_cap_charge(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                source_contract_id=contract.id,
                season=2026,
                amount=2_500_000,
                reason=DeadCapReason.WAIVE_GUARANTEED,
                created_in_season=2025,
            )

            batched = await contract_repo.get_dead_cap_totals_for_league_seasons(
                pool, league_id, [2026, 2027]
            )
            assert type(batched[(team_id, 2026)]) is int
            assert batched[(team_id, 2026)] == 2_500_000
            assert (team_id, 2027) not in batched

            single = await contract_repo.get_dead_cap_total(pool, team_id, 2026)
            assert batched[(team_id, 2026)] == single
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_active_contract_years_for_league_range_carries_team_and_option_breakdown():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "BAY")
            player_active = await _make_player(pool, league_id, team_id)
            player_waived = await _make_player(pool, league_id, team_id)

            active = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_active,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2027,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=active.id, season=2026, salary=12_000_000,
                guaranteed_amount=12_000_000,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=active.id, season=2027, salary=13_000_000,
                guaranteed_amount=0, option_type=OptionType.TEAM,
                option_decision=OptionDecision.PENDING,
            )

            waived = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_waived,
                team_id=team_id,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=waived.id, season=2026, salary=3_000_000,
                guaranteed_amount=3_000_000,
            )

            rows = await contract_repo.list_active_contract_years_for_league_range(
                pool, league_id, [2026, 2027]
            )
            # Only the active contract's two rows -- the waived contract's
            # row must not appear.
            assert len(rows) == 2
            assert all(r.team_id == team_id for r in rows)

            row_2026 = next(r for r in rows if r.season == 2026)
            assert row_2026.player_id == player_active
            assert row_2026.salary == 12_000_000
            assert row_2026.option_type == OptionType.NONE

            row_2027 = next(r for r in rows if r.season == 2027)
            assert row_2027.player_id == player_active
            assert row_2027.salary == 13_000_000
            assert row_2027.option_type == OptionType.TEAM
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_rookie_scale_get_and_list():
    """`rookie_scale` is league-independent seed data from migration 003 —
    no cleanup needed, this test only reads it."""
    pool = await get_pool()
    try:
        pick_1 = await contract_repo.get_rookie_scale(pool, 1)
        assert pick_1 is not None
        assert pick_1.year_1_salary == 10306508
        assert pick_1.year_2_salary == 10821833
        assert pick_1.year_3_salary == 11337157
        assert pick_1.year_4_salary == 14858652

        assert await contract_repo.get_rookie_scale(pool, 999) is None

        all_picks = await contract_repo.list_rookie_scale(pool)
        assert len(all_picks) == 30
        assert [p.pick_number for p in all_picks] == list(range(1, 31))
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_seeds_rookie_scale_json_is_byte_identical_to_the_live_migrated_table():
    """Phase 2 step 10, T1d: `seeds/rookie_scale.json` and the real,
    migrated `rookie_scale` table must never be able to silently drift
    apart -- all four salary columns, all 30 rows, compared directly
    against `contract_repo.list_rookie_scale`'s live read. Row 30 in
    particular must carry migration 009's Decision P2-33 repair, not
    migration 003's corrupt original."""
    with open(_SEEDS_DIR / "rookie_scale.json", encoding="utf-8") as f:
        seed_rows = json.load(f)["rookie_scale"]

    pool = await get_pool()
    try:
        live_rows = await contract_repo.list_rookie_scale(pool)
        assert len(seed_rows) == 30
        assert len(live_rows) == 30

        live_by_pick = {r.pick_number: r for r in live_rows}
        for seed_row in seed_rows:
            live = live_by_pick[seed_row["pick_number"]]
            assert live.year_1_salary == seed_row["year_1_salary"], seed_row["pick_number"]
            assert live.year_2_salary == seed_row["year_2_salary"], seed_row["pick_number"]
            assert live.year_3_salary == seed_row["year_3_salary"], seed_row["pick_number"]
            assert live.year_4_salary == seed_row["year_4_salary"], seed_row["pick_number"]

        # Row 30 specifically: migration 009's repaired figures, not
        # migration 003's corrupt original (2,082,678 / 2,186,812 /
        # 2,290,946 / 2,000,000).
        row_30 = live_by_pick[30]
        assert row_30.year_1_salary == 3_079_775
        assert row_30.year_2_salary == 3_233_763
        assert row_30.year_3_salary == 3_387_752
        assert row_30.year_4_salary == 3_064_280
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_contract_team_moves_a_contract_between_teams():
    """A real trade-shaped scenario: create a contract on team A, move it
    to team B via `set_contract_team`, confirm `list_contracts_for_team`
    and `get_committed_salary` reflect the move on both teams."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "SCA")
            team_b = await _make_team(pool, league_id, "SCB")
            player_id = await _make_player(pool, league_id, team_a)

            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_a,
                signed_in_season=2026,
                start_season=2026,
                end_season=2027,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract.id, season=2026, salary=7_000_000,
                guaranteed_amount=7_000_000,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract.id, season=2027, salary=7_500_000,
                guaranteed_amount=7_500_000,
            )

            assert await contract_repo.get_committed_salary(pool, team_a, 2026) == 7_000_000
            assert await contract_repo.get_committed_salary(pool, team_b, 2026) == 0

            await contract_repo.set_contract_team(pool, contract.id, team_b)

            moved = await contract_repo.get_contract(pool, contract.id)
            assert moved.team_id == team_b

            for_team_a = await contract_repo.list_contracts_for_team(pool, team_a)
            assert for_team_a == []
            for_team_b = await contract_repo.list_contracts_for_team(pool, team_b)
            assert [c.id for c in for_team_b] == [contract.id]

            assert await contract_repo.get_committed_salary(pool, team_a, 2026) == 0
            assert await contract_repo.get_committed_salary(pool, team_b, 2026) == 7_000_000
            assert await contract_repo.get_committed_salary(pool, team_b, 2027) == 7_500_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_active_contracts_and_contract_years_for_league_match_per_team_calls():
    """`list_active_contracts_for_league`/`list_contract_years_for_league`
    must return everything a per-team cross-check via
    `list_contracts_for_team`/`list_contract_years_for_contract` would --
    no missing/duplicate rows across a small multi-team league, and the
    waived contract must not appear in either bulk loader."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "BLK")
            team_b = await _make_team(pool, league_id, "BLM")
            player_a = await _make_player(pool, league_id, team_a)
            player_b = await _make_player(pool, league_id, team_b)
            player_c = await _make_player(pool, league_id, team_a)

            contract_a = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_a,
                team_id=team_a,
                signed_in_season=2026,
                start_season=2026,
                end_season=2027,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_a.id, season=2026, salary=5_000_000,
                guaranteed_amount=5_000_000,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_a.id, season=2027, salary=5_500_000,
                guaranteed_amount=5_500_000,
            )

            contract_b = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_b,
                team_id=team_b,
                signed_in_season=2026,
                start_season=2026,
                end_season=2026,
                total_years=1,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract_b.id, season=2026, salary=3_000_000,
                guaranteed_amount=3_000_000,
            )

            # A waived contract -- must not appear in either bulk loader.
            waived = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_c,
                team_id=team_a,
                signed_in_season=2025,
                start_season=2025,
                end_season=2026,
                total_years=2,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
                status=ContractStatus.WAIVED,
                terminated_in_season=2026,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=waived.id, season=2026, salary=2_000_000,
                guaranteed_amount=0,
            )

            bulk_contracts = await contract_repo.list_active_contracts_for_league(
                pool, league_id
            )
            bulk_years = await contract_repo.list_contract_years_for_league(pool, league_id)

            # Cross-check against the single-team functions.
            per_team_contracts = [
                *await contract_repo.list_contracts_for_team(
                    pool, team_a, status=ContractStatus.ACTIVE
                ),
                *await contract_repo.list_contracts_for_team(
                    pool, team_b, status=ContractStatus.ACTIVE
                ),
            ]
            assert {c.id for c in bulk_contracts} == {c.id for c in per_team_contracts}
            assert len(bulk_contracts) == len(per_team_contracts) == 2
            assert waived.id not in {c.id for c in bulk_contracts}

            per_contract_years = [
                *await contract_repo.list_contract_years_for_contract(pool, contract_a.id),
                *await contract_repo.list_contract_years_for_contract(pool, contract_b.id),
            ]
            assert {(y.contract_id, y.season) for y in bulk_years} == {
                (y.contract_id, y.season) for y in per_contract_years
            }
            assert len(bulk_years) == len(per_contract_years) == 3
            assert waived.id not in {y.contract_id for y in bulk_years}

            bulk_a_2026 = next(
                y for y in bulk_years if y.contract_id == contract_a.id and y.season == 2026
            )
            assert bulk_a_2026.salary == 5_000_000
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_contracts_for_league_season_events_covers_signed_and_terminated_rows():
    """Phase 5's transactions merge (docs/design/phase-5-discord-
    presentation.md §7.2 item 4/B-12): a contract that STARTED in
    `season` and one that ENDED in `season` both come back, regardless
    of current `status` -- unlike `list_active_contracts_for_league`,
    this must surface a terminated contract's own row, since termination
    IS the event being read. A contract signed/ending in a different
    season is excluded."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "EVT")
            signed_this_season = await _make_player(pool, league_id, team_id)
            terminated_this_season = await _make_player(pool, league_id, team_id)
            unrelated_season = await _make_player(pool, league_id, team_id)

            await contract_repo.create_contract(
                pool, league_id=league_id, player_id=signed_this_season, team_id=team_id,
                signed_in_season=2026, start_season=2026, end_season=2028, total_years=3,
                contract_type=ContractType.STANDARD, signing_method=SigningMethod.CAP_SPACE,
            )
            terminated_contract = await contract_repo.create_contract(
                pool, league_id=league_id, player_id=terminated_this_season, team_id=team_id,
                signed_in_season=2024, start_season=2024, end_season=2027, total_years=4,
                contract_type=ContractType.STANDARD, signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.terminate_contract(
                pool, terminated_contract.id, status=ContractStatus.WAIVED,
                terminated_in_season=2026,
            )
            await contract_repo.create_contract(
                pool, league_id=league_id, player_id=unrelated_season, team_id=team_id,
                signed_in_season=2024, start_season=2024, end_season=2025, total_years=1,
                contract_type=ContractType.STANDARD, signing_method=SigningMethod.CAP_SPACE,
            )

            events = await contract_repo.list_contracts_for_league_season_events(
                pool, league_id, 2026
            )
            assert {c.player_id for c in events} == {signed_this_season, terminated_this_season}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
