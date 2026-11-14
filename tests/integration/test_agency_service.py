"""Live I/O proof for `services/agency_service.py` -- Phase 4 §11 Stage B
steps 6/8/9 (Decisions P4-6, P4-7/P4-10/P4-44/P4-45, P4-14). The pure
evaluator/ladder logic already has exhaustive coverage in
`tests/unit/test_agency_motivation.py`/`test_agency_situation.py`; this
file proves only the I/O seam -- real `player_motivation`/`player_
motivation_events` round trips, `motivation_repo` writes composing
correctly with `load_gm_inputs`, and idempotent bootstrapping.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as `test_smoke.py`.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    contract_repo,
    league_repo,
    motivation_repo,
    player_repo,
    ratings_repo,
    team_repo,
)
from dba2.domain.contract import ContractType, SigningMethod
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.managers.philosophy import PROFILES
from dba2.managers.valuation import effective_talent_ovr, market_salary_share, market_value_for
from dba2.services import agency_service
from dba2.services.gm_service import load_gm_inputs
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int, **overrides: int) -> dict[str, int]:
    base = make_ratings(seed)
    values = {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }
    values.update(overrides)
    return values


async def _make_league(pool, *, season: int = _SEASON) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Agency Service Test League",
        start_season_year=season,
        current_season=season,
        commissioner_user_id=1,
        rng_seed=4242,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=season,
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


async def _make_team(
    pool,
    league_id: int,
    code: str,
    *,
    gm_philosophy: GmPhilosophy = GmPhilosophy.EGALITARIAN,
    timeline: Timeline = Timeline.RETOOL,
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
        gm_philosophy=gm_philosophy,
        timeline=timeline,
    )
    return team.id


async def _make_player(
    pool,
    league_id: int,
    team_id: int | None,
    *,
    overall_cached: int = 70,
    age: int = 27,
    season: int = _SEASON,
) -> int:
    status = RosterStatus.ACTIVE if team_id is not None else RosterStatus.FREE_AGENT
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=birth_date_for_age(age, season),
        roster_status=status,
        overall_cached=overall_cached,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    ratings = _ratings_columns(player.id, peak_age_start=27, peak_age_end=31)
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        ratings=ratings,
    )
    return player.id


async def _make_contract(
    pool,
    league_id: int,
    team_id: int,
    player_id: int,
    *,
    start_season: int,
    salaries: tuple[int, ...],
) -> int:
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=start_season,
        start_season=start_season,
        end_season=start_season + len(salaries) - 1,
        total_years=len(salaries),
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    for i, salary in enumerate(salaries):
        await contract_repo.create_contract_year(
            pool,
            contract_id=contract.id,
            season=start_season + i,
            salary=salary,
            guaranteed_amount=salary,
        )
    return contract.id


@pytest.mark.asyncio
async def test_bootstrap_league_motivation_creates_one_row_per_player_and_is_idempotent():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AA")
            p1 = await _make_player(pool, league_id, team_id, overall_cached=90, age=25)
            p2 = await _make_player(pool, league_id, team_id, overall_cached=55, age=20)

            result = await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )
            assert result.created == 2
            assert result.skipped_existing == 0

            m1 = await motivation_repo.get_player_motivation(pool, p1)
            m2 = await motivation_repo.get_player_motivation(pool, p2)
            assert m1 is not None and m2 is not None
            assert m1.request_state is RequestState.CONTENT
            assert m1.satisfaction_winning == 0
            assert m1.morale == 0
            # star_leverage derives from talent -- the 90-overall player
            # must read a higher leverage than the 55-overall one.
            assert m1.star_leverage > m2.star_leverage
            # Decision P4-12 -- no fabricated hometown for anyone.
            assert m1.hometown_team_id is None
            assert m2.hometown_team_id is None

            # Idempotent second pass -- no duplicate rows, nothing
            # overwritten.
            second = await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )
            assert second.created == 0
            assert second.skipped_existing == 2
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_agency_checkpoint_writes_events_and_updates_state_atomically():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(
                pool,
                league_id,
                "BB",
                gm_philosophy=GmPhilosophy.STAR_MAXER,
                timeline=Timeline.WIN_NOW,
            )
            team_b = await _make_team(
                pool,
                league_id,
                "CC",
                gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
                timeline=Timeline.REBUILD,
            )
            p1 = await _make_player(pool, league_id, team_a, overall_cached=85, age=28)
            await _make_contract(
                pool, league_id, team_a, p1, start_season=_SEASON, salaries=(20_000_000, 21_000_000)
            )
            p2 = await _make_player(pool, league_id, team_b, overall_cached=60, age=22)
            await _make_contract(
                pool, league_id, team_b, p2, start_season=_SEASON, salaries=(2_000_000,)
            )

            await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )

            result = await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
            )
            assert result.players_evaluated == 2

            m1_after = await motivation_repo.get_player_motivation(pool, p1)
            m2_after = await motivation_repo.get_player_motivation(pool, p2)
            assert m1_after.last_evaluated_season == _SEASON
            assert m2_after.last_evaluated_season == _SEASON

            events_p1 = await motivation_repo.list_motivation_events_for_player(pool, p1)
            # Every axis on team_a's contending, well-paid veteran should
            # have SOME signal (team_success, pay_vs_market at minimum) --
            # at least one written event row.
            assert len(events_p1) >= 1
            for event in events_p1:
                assert event.league_id == league_id
                assert event.season == _SEASON
                assert event.phase is Phase.TRADE_DEADLINE

            # The reconstruction invariant, checked live against real
            # written rows: state == prior (0) + sum(written deltas) per
            # axis (P4-44).
            from dba2.domain.motivation import MotivationAxis

            by_axis: dict[MotivationAxis, int] = dict.fromkeys(MotivationAxis, 0)
            for event in events_p1:
                if event.event_type != agency_service.LADDER_TRANSITION_EVENT_TYPE:
                    by_axis[event.axis] += event.delta
            assert m1_after.satisfaction_winning == by_axis[MotivationAxis.WINNING]
            assert m1_after.satisfaction_role_usage == by_axis[MotivationAxis.ROLE_USAGE]
            assert m1_after.satisfaction_money == by_axis[MotivationAxis.MONEY]
            assert m1_after.satisfaction_loyalty == by_axis[MotivationAxis.LOYALTY]
            assert m1_after.satisfaction_legacy == by_axis[MotivationAxis.LEGACY]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_agency_checkpoint_skips_players_with_no_motivation_row():
    """A checkpoint is not the trait generator's job -- a player who
    hasn't been bootstrapped yet is skipped, not silently given fabricated
    traits."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DD")
            await _make_player(pool, league_id, team_id, overall_cached=70, age=25)
            # No bootstrap call.
            result = await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
            )
            assert result.players_evaluated == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_agency_checkpoint_skips_a_player_it_already_evaluated_for_the_same_phase():
    """The per-player guard (phase-5-stage-e-advance.md P5-99.r.2): a second pass
    of the SAME `(season, phase)` checkpoint skips the player and writes nothing,
    while the other checkpoint of the same season (a different phase) still
    evaluates them -- the guard is keyed by phase, not just by season."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(
                pool,
                league_id,
                "GG",
                gm_philosophy=GmPhilosophy.STAR_MAXER,
                timeline=Timeline.WIN_NOW,
            )
            player_id = await _make_player(pool, league_id, team_id, overall_cached=85, age=28)
            await _make_contract(
                pool,
                league_id,
                team_id,
                player_id,
                start_season=_SEASON,
                salaries=(20_000_000, 20_000_000),
            )
            await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )

            first = await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
            )
            assert (first.players_evaluated, first.players_skipped) == (1, 0)
            events = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            assert events, "the first pass must write events, or the skip proves nothing"
            after_first = await motivation_repo.get_player_motivation(pool, player_id)
            assert after_first.last_checkpoint_phase is Phase.TRADE_DEADLINE

            again = await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
            )
            assert (again.players_evaluated, again.players_skipped) == (0, 1)
            events_again = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            assert len(events_again) == len(events)
            assert await motivation_repo.get_player_motivation(pool, player_id) == after_first

            other = await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.PROGRESSION
            )
            assert (other.players_evaluated, other.players_skipped) == (1, 0)
            after_other = await motivation_repo.get_player_motivation(pool, player_id)
            assert after_other.last_checkpoint_phase is Phase.PROGRESSION
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_agency_checkpoint_clamps_trust_at_the_db_write_boundary():
    """Round-5 critic finding B4: `trust_in_management` is `SMALLINT NOT
    NULL` with no CHECK constraint (Decision P4-45) -- the `[-100, 100]`
    clamp has to be real at the write boundary, not only in a unit test
    over `fold_trust_deltas` nothing in production called. Seeds an out-
    of-range value through the real `update_motivation_state` write path
    (legal today, since the column accepts it), runs one real checkpoint,
    and asserts the PERSISTED row reads the clamped value, not the raw
    decayed one."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "EE")
            p1 = await _make_player(pool, league_id, team_id, overall_cached=70, age=27)
            await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )
            m = await motivation_repo.get_player_motivation(pool, p1)
            await motivation_repo.update_motivation_state(
                pool,
                p1,
                satisfaction_winning=m.satisfaction_winning,
                satisfaction_role_usage=m.satisfaction_role_usage,
                satisfaction_loyalty=m.satisfaction_loyalty,
                satisfaction_money=m.satisfaction_money,
                satisfaction_legacy=m.satisfaction_legacy,
                morale=m.morale,
                trust_in_management=250,  # out of [-100, 100] -- no CHECK stops this
                request_state=m.request_state,
                request_opened_in_season=m.request_opened_in_season,
                last_evaluated_season=m.last_evaluated_season,
                # A seeding write followed by a real checkpoint for this same
                # season: anything but None would make that checkpoint skip the
                # player it exists to exercise (P5-99.r.2).
                last_checkpoint_phase=None,
            )
            seeded = await motivation_repo.get_player_motivation(pool, p1)
            assert seeded.trust_in_management == 250  # confirms the seed actually landed

            await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
            )

            after = await motivation_repo.get_player_motivation(pool, p1)
            # decay_trust(250) = round(250 * 0.85) = 212, still out of
            # range -- the persisted row must read the CLAMPED value.
            assert after.trust_in_management == 100
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_build_player_situation_pay_vs_market_uses_real_market_salary_share():
    """Round-5 critic finding B2, as corrected by round-5's own re-
    verification. The critic's cited source, `managers/valuation.py::
    market_value_for`, is real and reachable exactly as claimed -- but
    hand-verified this round to return a multi-season DISCOUNTED TRADE-
    EQUITY score (~186 for a 70-overall player on a $150M cap), not a
    dollar figure comparable to one season's salary; dividing a real
    salary by it would saturate `score_money` at `+1.0` for every player,
    which is a worse defect than the proxy it would replace. The actual
    fix reads `market_salary_share(effective_ovr, cap_settings) *
    salary_cap` -- the real §4.4 market-salary curve `managers/decisions/
    signing.py` already uses (`market` at `:402`/`:462`) for a live
    signing decision, genuinely dollar-comparable to `year.salary`.
    Verified by computing the same quantity two ways from the same real
    inputs and asserting they agree exactly, and that the result
    disagrees with both the removed proxy AND a literal `market_value_
    for` read."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(
                pool,
                league_id,
                "FF",
                gm_philosophy=GmPhilosophy.EGALITARIAN,
                timeline=Timeline.RETOOL,
            )
            p1 = await _make_player(pool, league_id, team_id, overall_cached=70, age=27)
            await _make_contract(
                pool, league_id, team_id, p1, start_season=_SEASON, salaries=(5_000_000,)
            )
            await agency_service.bootstrap_league_motivation(
                pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
            )
            motivation = await motivation_repo.get_player_motivation(pool, p1)

            snapshot, ctx = await load_gm_inputs(pool, league_id=league_id, season=_SEASON)
            situation = await agency_service.build_player_situation(
                pool,
                league_id=league_id,
                season=_SEASON,
                snapshot=snapshot,
                ctx=ctx,
                player_id=p1,
                motivation=motivation,
            )
            assert situation.pay_vs_market is not None

            team_snapshot = snapshot.team_by_id(team_id)
            roster_player = team_snapshot.player_by_id(p1)
            philosophy = ctx.philosophy_by_team[team_id]
            effective_ovr = effective_talent_ovr(roster_player, PROFILES[philosophy])
            expected_market_salary = (
                market_salary_share(float(effective_ovr), team_snapshot.cap_settings)
                * team_snapshot.cap_settings.salary_cap
            )
            assert expected_market_salary > 0
            expected_pay_vs_market = 5_000_000 / expected_market_salary
            assert situation.pay_vs_market == pytest.approx(expected_pay_vs_market)

            # A literal `market_value_for` read is the defect this round
            # found, not the fix -- assert the two disagree, using the
            # same real inputs.
            trade_equity_value = market_value_for(
                roster_player,
                league_rng_seed=ctx.league_rng_seed,
                team_id=team_id,
                season=_SEASON,
                philosophy=philosophy,
                window=ctx.window_by_team[team_id],
                cap_settings=team_snapshot.cap_settings,
                tuning=ctx.tuning,
            )
            assert trade_equity_value > 0
            literal_market_value_for_pay_vs_market = 5_000_000 / trade_equity_value
            assert situation.pay_vs_market != pytest.approx(literal_market_value_for_pay_vs_market)

            # The removed proxy read `salary_cap * 0.35 * (overall/100) **
            # 2` -- a $150M cap and 70 overall reads ~$25.7M "expected
            # salary," pricing this $5M contract at pay_vs_market ~ 0.19.
            # The real market-salary read must not land there either.
            old_proxy_expected_salary = 150_000_000 * 0.35 * (70 / 100.0) ** 2
            old_proxy_pay_vs_market = 5_000_000 / old_proxy_expected_salary
            assert situation.pay_vs_market != pytest.approx(old_proxy_pay_vs_market)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
