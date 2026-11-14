"""RMLP §7.2 -- the remaining per-call-site concurrency tests not already
covered by `test_roster_service.py` (tests 3/4), `test_rmlp_lock_mode.py`
(test 12), `test_roster_lock.py` (tests 14/15), or the existing round-5/6
regression suite in `test_gm_command_service.py` (tests 1/2).

Covers test 9 (apply_trade stale execution / F1, rule 1 -- player
ownership), test 11 (the lock-timeout path, both distinct
`ConcurrencyError` messages, including Addendum A3's decorator-catches-a-
non-helper-statement case), and test 13 (Decision 2's own load-bearing
claim: a human command during a phase hook's decision-computation span is
rejected, not merely during its writes, and the batch itself still
completes) plus test 13's required mutation twin (fix round, 2026-09-18 --
B4).

**B3 (fix round, 2026-09-18) -- `apply_trade`'s remaining revalidation
rules and its roster-status-preservation behavior change, previously
untested despite being real, working, confirmed-correct code (critic
live-verified all four in the shipped code before this round; only rule 1
had a test).** Immediately after test 9 (rule 1) below: rule 2 (contract
ownership), rule 3 (contract existence), rule 4 (status legality), and
roster-status preservation across a trade of an `INJURED` and a `TWO_WAY`
player.

**Disclosed, not built this dispatch:** §7.2 tests 5 (waive vs sign-free-
agent concurrent), 7 (apply_signing concurrent human-vs-human/AI-vs-
human), 8 (apply_trade vs waive of a traded player, concurrent), and 10
(the batch deadlock probe, F2, plus its required twin) -- see the build
report for why.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import random
from datetime import UTC, date, datetime

import pytest

from dba2.core.errors import ConcurrencyError, ValidationError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import contract_repo, league_repo, player_repo, ratings_repo, team_repo
from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import gm_command_service, gm_service, roster_lock, roster_service
from dba2.services.roster_lock import LockMode
from tests.fixtures.rmlp import rmlp_gate
from tests.fixtures.sim import make_ratings

_MANAGER = 42

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int) -> dict[str, int]:
    """Mirrors `test_gm_service.py`'s own helper -- `load_gm_inputs` joins
    `player_ratings`, so a player with no ratings row raises a bare
    `KeyError` deep inside the AI decision layer rather than the clean
    domain error this test actually wants to exercise."""
    base = make_ratings(seed)
    return {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }


async def _make_league(pool, *, current_phase: Phase = Phase.WAIVERS) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="RMLP Concurrency Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=current_phase,
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


async def _make_team(pool, league_id: int, code: str, *, manager_user_id: int | None = None) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        manager_user_id=manager_user_id,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int | None = None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Conc",
        last_name="Urrency",
        primary_position=Position.SG,
        height_in=77,
        weight_lb=200,
        wingspan_in=80,
        birth_date=date(1998, 8, 8),
        roster_status=RosterStatus.ACTIVE if team_id else RosterStatus.FREE_AGENT,
        overall_cached=74,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        ratings=_ratings_columns(player.id),
    )
    return player.id


async def _make_active_contract(pool, league_id, team_id, player_id, *, salary=1_000_000) -> int:
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
    await contract_repo.create_contract_year(
        pool, contract_id=contract.id, season=2026, salary=salary, guaranteed_amount=salary
    )
    return contract.id


def _dummy_record(team_id: int) -> DecisionRecord:
    return DecisionRecord(
        team_id=team_id,
        philosophy=GmPhilosophy.EGALITARIAN,
        window=CompetitiveWindow.RETOOL,
        subject=f"team:{team_id}",
        verdict="test",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


# ---------------------------------------------------------------------------
# Test 9 -- apply_trade with a stale execution (F1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_trade_with_a_stale_execution_raises_and_moves_nothing():
    """RMLP §7.2 test 9. Sequential, not concurrent: a player is moved to a
    THIRD team (simulating some other action that happened between the
    trade's decision and its application), then a `TradeExecution` naming
    their OLD team as the sender is applied -- rule 1 (player ownership)
    must raise, and raw SQL must show nothing moved."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "F1A")
            team_b = await _make_team(pool, league_id, "F1B")
            team_c = await _make_team(pool, league_id, "F1C")
            player_id = await _make_player(pool, league_id, team_a)
            contract_id = await _make_active_contract(pool, league_id, team_a, player_id)

            # Simulate "moved by something else since the trade was
            # decided" -- directly write the player/contract onto team_c.
            await player_repo.set_team_and_status(pool, player_id, team_c, RosterStatus.ACTIVE)
            await contract_repo.set_contract_team(pool, contract_id, team_c)

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(player_id,),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved = await player_repo.get_player(pool, player_id)
            assert unmoved.team_id == team_c
            contract_row = await contract_repo.get_contract(pool, contract_id)
            assert contract_row.team_id == team_c
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# B3 (fix round, 2026-09-18) -- apply_trade's remaining revalidation rules
# (2, 3, 4) and its roster-status-preservation behavior change. All four
# are real, already-working code (critic live-verified them before this
# round) -- these are regression tests, not bug fixes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_trade_rejects_when_the_active_contract_belongs_to_a_third_team():
    """B3(a) -- rule 2, contract ownership. The player's own `team_id`
    still names the sending team (rule 1 passes), but their ACTIVE
    contract has been moved onto a THIRD team since the trade was decided
    (simulating some other action, matching test 9's own technique) --
    rule 2 must raise before any write, naming the contract's real owner."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "R2A")
            team_b = await _make_team(pool, league_id, "R2B")
            team_c = await _make_team(pool, league_id, "R2C")
            player_id = await _make_player(pool, league_id, team_a)
            contract_id = await _make_active_contract(pool, league_id, team_a, player_id)

            # The player row still says team_a; only the CONTRACT moves to
            # team_c -- an inconsistent state, forced directly (as test 9
            # does) to isolate rule 2 from rule 1.
            await contract_repo.set_contract_team(pool, contract_id, team_c)

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(player_id,),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            with pytest.raises(ValidationError, match="active contract belongs to team"):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved = await player_repo.get_player(pool, player_id)
            assert unmoved.team_id == team_a
            contract_row = await contract_repo.get_contract(pool, contract_id)
            assert contract_row.team_id == team_c
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_rejects_when_the_traded_player_has_no_active_contract():
    """B3(b) -- rule 3, contract existence. The player's `team_id` still
    names the sending team, but their contract was terminated since the
    trade was decided (e.g. waived/released in the gap between decision
    and application) -- rule 3 must raise an honest message naming what
    changed, and nothing may move."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "R3A")
            team_b = await _make_team(pool, league_id, "R3B")
            player_id = await _make_player(pool, league_id, team_a)
            contract_id = await _make_active_contract(pool, league_id, team_a, player_id)

            # Terminate the contract directly, leaving the player row's
            # team_id untouched -- isolates rule 3 from rule 1 the same
            # way rule 2's test isolates itself from rule 1 above.
            terminated = await contract_repo.terminate_contract(
                pool, contract_id, status=ContractStatus.WAIVED, terminated_in_season=2026
            )
            assert terminated

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(player_id,),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            with pytest.raises(ValidationError, match="no active contract"):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved = await player_repo.get_player(pool, player_id)
            assert unmoved.team_id == team_a
            contract_row = await contract_repo.get_contract(pool, contract_id)
            assert contract_row.status == ContractStatus.WAIVED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_rejects_an_untradeable_roster_status():
    """B3(c) -- rule 4, status legality. The player's `team_id` and active
    contract both still name the sending team, but their `roster_status`
    has been forced to `FREE_AGENT` (an inconsistent-but-forceable state,
    same technique as rules 2/3's tests) -- rule 4 must raise, naming the
    actual status, before any write."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "R4A")
            team_b = await _make_team(pool, league_id, "R4B")
            player_id = await _make_player(pool, league_id, team_a)
            contract_id = await _make_active_contract(pool, league_id, team_a, player_id)

            await player_repo.set_team_and_status(pool, player_id, team_a, RosterStatus.FREE_AGENT)

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(player_id,),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            with pytest.raises(
                ValidationError, match="roster_status=free_agent.*not tradeable"
            ):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved = await player_repo.get_player(pool, player_id)
            assert unmoved.team_id == team_a
            assert unmoved.roster_status == RosterStatus.FREE_AGENT
            contract_row = await contract_repo.get_contract(pool, contract_id)
            assert contract_row.team_id == team_a
            assert contract_row.status == ContractStatus.ACTIVE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_preserves_injured_and_two_way_roster_status():
    """B3(d) -- roster-status preservation (§7.5's own third pre-
    authorized behavior change, named but never given a test until this
    round). Trades an `INJURED` player and a `TWO_WAY` player from the
    same team in one execution and confirms BOTH retain their real status
    after the trade -- not forced to `ACTIVE`, which would silently heal
    the injured player and silently convert the two-way player into a
    standard roster spot (a real cap/roster-count corruption, per
    `apply_trade`'s own docstring)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "R5A")
            team_b = await _make_team(pool, league_id, "R5B")

            injured_id = await _make_player(pool, league_id, team_a)
            await _make_active_contract(pool, league_id, team_a, injured_id)
            await player_repo.set_team_and_status(pool, injured_id, team_a, RosterStatus.INJURED)

            two_way_id = await _make_player(pool, league_id, team_a)
            await _make_active_contract(pool, league_id, team_a, two_way_id)
            await player_repo.set_team_and_status(pool, two_way_id, team_a, RosterStatus.TWO_WAY)

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(injured_id, two_way_id),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_trade(
                    conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                )

            injured_after = await player_repo.get_player(pool, injured_id)
            assert injured_after.team_id == team_b
            assert injured_after.roster_status == RosterStatus.INJURED

            two_way_after = await player_repo.get_player(pool, two_way_id)
            assert two_way_after.team_id == team_b
            assert two_way_after.roster_status == RosterStatus.TWO_WAY
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Test 11 -- the lock-timeout path, both distinct messages, plus Addendum
# A3's decorator-catches-a-non-helper-statement case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_row_contention_surfaces_the_entity_message():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "T11A")
            holder_locked = asyncio.Event()
            release_holder = asyncio.Event()

            async def _holder():
                async with transaction(pool) as conn:
                    await roster_lock.open_league_gate(
                        conn, league_id=league_id, mode=LockMode.SHARED
                    )
                    await roster_lock.acquire(
                        conn, league_id=league_id, teams=[team_id], players=[]
                    )
                    holder_locked.set()
                    await release_holder.wait()

            async def _contender():
                await holder_locked.wait()
                with pytest.raises(ConcurrencyError) as exc_info:
                    async with transaction(pool) as conn:
                        await roster_lock.open_league_gate(
                            conn, league_id=league_id, mode=LockMode.SHARED
                        )
                        await roster_lock.acquire(
                            conn, league_id=league_id, teams=[team_id], players=[]
                        )
                assert "updated by another action" in str(exc_info.value)
                release_holder.set()

            await asyncio.wait_for(
                asyncio.gather(_holder(), _contender()), timeout=10
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_gate_contention_surfaces_the_league_ai_pass_message():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            holder_locked = asyncio.Event()
            release_holder = asyncio.Event()

            async def _holder():
                async with transaction(pool) as conn:
                    await roster_lock.open_league_gate(
                        conn, league_id=league_id, mode=LockMode.EXCLUSIVE
                    )
                    holder_locked.set()
                    await release_holder.wait()

            async def _contender():
                await holder_locked.wait()
                with pytest.raises(ConcurrencyError) as exc_info:
                    async with transaction(pool) as conn:
                        await roster_lock.open_league_gate(
                            conn, league_id=league_id, mode=LockMode.SHARED
                        )
                assert "AI pass" in str(exc_info.value)
                release_holder.set()

            await asyncio.wait_for(
                asyncio.gather(_holder(), _contender()), timeout=10
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_contract_row_contention_is_caught_by_the_decorator_not_the_helper():
    """Addendum A3's own required extension to test 11 -- this is the test
    that would fail under revision 2's helper-boundary-only spec. A
    concurrent, uncommitted `SELECT ... FOR UPDATE` on the CONTRACT row
    (not a team/player row `acquire()` itself locks) blocks `waive()`'s
    `terminate_contract` CAS -- a raw statement `roster_lock.acquire`
    never sees. Proves `@maps_lock_contention`, not `acquire`'s own
    mapping, is what catches it: the team/player locks acquire fine (no
    conflict there), so if the decorator didn't exist, this would surface
    a raw `asyncpg.exceptions.LockNotAvailableError` instead of a clean
    `ConcurrencyError`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "T11C")
            player_id = await _make_player(pool, league_id, team_id)
            contract_id = await _make_active_contract(pool, league_id, team_id, player_id)

            holder_locked = asyncio.Event()
            release_holder = asyncio.Event()

            async def _holder():
                async with transaction(pool) as conn:
                    await conn.fetchrow(
                        "SELECT id FROM contracts WHERE id = $1 FOR UPDATE", contract_id
                    )
                    holder_locked.set()
                    await release_holder.wait()

            async def _contender():
                await holder_locked.wait()
                with pytest.raises(ConcurrencyError) as exc_info:
                    async with rmlp_gate(pool, league_id) as conn:
                        await roster_service.waive(
                            conn,
                            league_id=league_id,
                            contract_id=contract_id,
                            waived_in_season=2026,
                        )
                assert "updated by another action" in str(exc_info.value)
                release_holder.set()

            await asyncio.wait_for(
                asyncio.gather(_holder(), _contender()), timeout=10
            )

            # Nothing was written by the timed-out attempt.
            row = await contract_repo.get_contract(pool, contract_id)
            assert row.status.value == "active"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Test 13 -- Decision 2's own load-bearing claim.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_command_during_batch_decision_computation_is_rejected(monkeypatch):
    """RMLP §7.2 test 13. A phase-hook branch's DECISION COMPUTATION (not
    just its writes) is artificially slowed; a `/waive` fired during that
    window must fail with `ConcurrencyError` carrying the league-AI-pass
    message, and the batch itself must still complete cleanly -- proving
    Decision 2's exclusive gate covers the whole read-decide-write span,
    not merely the write.

    Simplified from the design's literal text in one disclosed way: rather
    than asserting a SPECIFIC AI waiver execution was applied (which needs
    real domain setup for `run_waivers` to decide to cut someone), this
    proves the load-bearing claim directly -- the hook completes without
    raising, and the identical `/waive` command that failed during the
    hook succeeds cleanly once the hook has released the gate, proving the
    gate was correctly held throughout and correctly released after."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.WAIVERS)
        try:
            team_id = await _make_team(pool, league_id, "T13A", manager_user_id=_MANAGER)
            player_id = await _make_player(pool, league_id, team_id)
            contract_id = await _make_active_contract(pool, league_id, team_id, player_id)

            original_load_gm_inputs = gm_service.load_gm_inputs

            async def _slow_load_gm_inputs(ex, *, league_id, season):
                # Comfortably longer than the SHARED gate's own 3s lock_
                # timeout -- the human command below must genuinely time
                # out and raise, not merely wait and then succeed because
                # the batch happened to finish first.
                await asyncio.sleep(5.0)
                return await original_load_gm_inputs(ex, league_id=league_id, season=season)

            monkeypatch.setattr(gm_service, "load_gm_inputs", _slow_load_gm_inputs)

            hook_task = asyncio.create_task(
                gm_service.run_phase_hook(
                    pool, league_id=league_id, phase=Phase.WAIVERS, actor_user_id=1
                )
            )
            # Give the hook time to open its exclusive gate and enter the
            # (monkeypatched, slow) decision-computation span.
            await asyncio.sleep(0.3)

            with pytest.raises(ConcurrencyError) as exc_info:
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
            assert "AI pass" in str(exc_info.value)

            # The batch itself completes cleanly, not aborted by the
            # human command's attempt.
            result = await asyncio.wait_for(hook_task, timeout=10)
            assert result is not None

            # The contract must be untouched by the rejected human command
            # -- the rejection happened before any write.
            row = await contract_repo.get_contract(pool, contract_id)
            assert row.status.value == "active"

            # And the gate was genuinely released afterward, not leaked --
            # the identical command now succeeds.
            await gm_command_service.waive_player(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                actor_user_id=_MANAGER,
            )
            after = await contract_repo.get_contract(pool, contract_id)
            assert after.status.value == "waived"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# B4 (fix round, 2026-09-18) -- test 13's required mutation twin. The
# design's own text names this the evidence that Decision 2 (holding the
# exclusive gate across the WHOLE read-decide-write span, not just the
# write) is load-bearing rather than merely defensive: simulate the OLDER,
# narrower placement -- gate wraps only the write -- and confirm THAT
# version lets a human command slip in during decision computation and the
# batch itself then abort on a stale revalidation, exactly the failure
# Decision 2 makes unreachable by construction in the test immediately
# above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrow_gate_placement_twin_lets_a_human_command_stale_the_batch(monkeypatch):
    """§7.2 test 13's required twin. Simulates the OLDER, narrower gate
    placement via a test-only monkeypatch that moves the REAL exclusive
    gate acquisition later -- from the top of `run_phase_hook`'s WAIVERS
    branch (Decision 2's actual placement, exercised by the test above) to
    immediately before the write (`apply_waivers`) -- without editing any
    production code:

    - `roster_lock.open_league_gate` is patched mode-aware: a `SHARED`
      call (the human command's own gate) goes through to the REAL
      function unchanged; an `EXCLUSIVE` call (the batch's own, still
      textually the first statement of the branch) is replaced with a
      stub that fetches `phase`/`season` (matching `LeagueGate`'s real
      contract) but does NOT take the actual advisory lock -- so the
      batch's decision-computation span is genuinely unguarded, exactly
      the narrower placement's own shape.
    - `gm_service.apply_waivers` is patched to call the REAL
      `open_league_gate` (EXCLUSIVE) itself, first, before delegating --
      this is the narrow placement's own gate call, now sitting
      immediately before the write instead of at the top of the branch.
    - `gm_service.run_waivers` is canned to always decide to cut the one
      test contract (bypassing the real decision engine's own domain
      preconditions -- the identical, disclosed simplification test 13
      itself already uses for the same reason).
    - `gm_service.load_gm_inputs` is slowed, opening a window.

    During that window, with the batch's own exclusive gate not actually
    held yet, a real `/waive` for the SAME contract is fired and SUCCEEDS
    (unlike the test above, where the identical command is cleanly
    rejected) -- proving the race is genuinely reachable under this
    placement. The batch then reaches its own write with a decision based
    on the now-stale snapshot; `roster_service.waive`'s own revalidation-
    under-lock (unchanged, correct code) catches the staleness and raises
    -- proving the batch ABORTS instead of completing, the exact failure
    Decision 2's actual placement makes unreachable by construction."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.WAIVERS)
        try:
            team_id = await _make_team(pool, league_id, "T13B", manager_user_id=_MANAGER)
            player_id = await _make_player(pool, league_id, team_id)
            contract_id = await _make_active_contract(pool, league_id, team_id, player_id)

            real_open_league_gate = roster_lock.open_league_gate
            real_apply_waivers = gm_service.apply_waivers
            original_load_gm_inputs = gm_service.load_gm_inputs

            async def _narrow_open_league_gate(conn, *, league_id, mode):
                if mode is LockMode.SHARED:
                    # The human command's own gate call -- real and
                    # uncontended, since the batch's own EXCLUSIVE call
                    # below is deferred rather than genuinely held.
                    return await real_open_league_gate(conn, league_id=league_id, mode=mode)
                # The batch's own EXCLUSIVE call, still textually the
                # first statement of the branch -- deferred: reads phase/
                # season (matching LeagueGate's real contract) but does
                # NOT take the advisory lock. The real acquisition happens
                # later, inside the patched apply_waivers below.
                await conn.execute("SET LOCAL lock_timeout = '60s'")
                league = await league_repo.get_league(conn, league_id)
                return roster_lock.LeagueGate(
                    league_id=league_id,
                    phase=league.current_phase,
                    season=league.current_season,
                    mode=mode,
                )

            async def _apply_waivers_with_narrow_gate(conn, execution, *, league_id):
                # The narrow placement's own gate call -- real, sitting
                # immediately before the write instead of at the top of
                # the branch (Decision 2's actual placement).
                await real_open_league_gate(conn, league_id=league_id, mode=LockMode.EXCLUSIVE)
                return await real_apply_waivers(conn, execution, league_id=league_id)

            def _canned_run_waivers(snapshot, ctx):
                record = _dummy_record(team_id)
                execution = gm_service.WaiverExecution(
                    team_id=team_id,
                    player_id=player_id,
                    contract_id=contract_id,
                    waived_in_season=2026,
                    record=record,
                )
                return gm_service.WaiverWindowResult(executions=(execution,), records=(record,))

            async def _slow_load_gm_inputs(ex, *, league_id, season):
                await asyncio.sleep(1.5)
                return await original_load_gm_inputs(ex, league_id=league_id, season=season)

            monkeypatch.setattr(roster_lock, "open_league_gate", _narrow_open_league_gate)
            monkeypatch.setattr(gm_service, "apply_waivers", _apply_waivers_with_narrow_gate)
            monkeypatch.setattr(gm_service, "run_waivers", _canned_run_waivers)
            monkeypatch.setattr(gm_service, "load_gm_inputs", _slow_load_gm_inputs)

            hook_task = asyncio.create_task(
                gm_service.run_phase_hook(
                    pool, league_id=league_id, phase=Phase.WAIVERS, actor_user_id=1
                )
            )
            # Give the hook time to take its (deferred, fake) gate and
            # enter the (monkeypatched, slow) decision-computation span.
            await asyncio.sleep(0.3)

            # Unlike the test above, this identical human command SUCCEEDS
            # -- the narrow placement never actually held the gate during
            # decision computation, so nothing rejects it.
            await gm_command_service.waive_player(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                actor_user_id=_MANAGER,
            )
            mid_batch = await contract_repo.get_contract(pool, contract_id)
            assert mid_batch.status.value == "waived"

            # The batch's own (canned) decision still names the now-
            # already-waived contract -- its own revalidation-under-lock
            # at write time is what catches the staleness, and one stale
            # execution aborts the ENTIRE batch under this narrower
            # placement (Decision 2's own stated argument for why the
            # wider placement, proven in the test above, was necessary).
            with pytest.raises(ValidationError, match="not active"):
                await asyncio.wait_for(hook_task, timeout=10)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
