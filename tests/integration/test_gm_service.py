"""Live I/O proof for `services/gm_service.py`'s T4 apply layer --
`load_gm_inputs`, `run_phase_hook`, and the appliers (five as of T7a2's
`apply_waivers`, docs/design/phase-2-manager-minds.md, Phase 2 step 10,
slice T4). T3's own pure plan
layer already has exhaustive coverage in `tests/unit/services/
test_gm_service_pure.py`; this file proves only the I/O seam T4 adds on
top of it -- real Postgres round trips, transaction atomicity, and the
JSONL artifact writer's determinism (Family-1 invariant 6).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import dataclasses
import json
import random
from datetime import UTC, datetime

import pytest

from dba2.core.errors import NotFoundError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    league_repo,
    offer_repo,
    player_repo,
    ratings_repo,
    role_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.role import ShotDiet
from dba2.domain.team import GmPhilosophy, Timeline, TimelineSource
from dba2.domain.transaction import TransactionAssetKind, TransactionType
from dba2.managers.decisions import draft as draft_decisions
from dba2.managers.explain import DecisionRecord
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import gm_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.rmlp import rmlp_gate
from tests.fixtures.sim import make_ratings

_SEASON = 2026

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int, **overrides: int) -> dict[str, int]:
    """The 57-column `ratings` dict `ratings_repo.upsert_player_ratings`
    validates against, read off a hand-built `PlayerRatings` domain object
    (`tests.fixtures.sim.make_ratings`'s own 55-pairwise-distinct-value
    discipline) rather than duplicating that discipline here."""
    base = make_ratings(seed)
    values = {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }
    values.update(overrides)
    return values


def _dummy_record(
    team_id: int,
    *,
    philosophy: GmPhilosophy = GmPhilosophy.EGALITARIAN,
    window: CompetitiveWindow = CompetitiveWindow.RETOOL,
) -> DecisionRecord:
    """A minimal, valid `DecisionRecord` for tests that exercise an
    applier directly against a hand-built execution value rather than one
    T3 itself produced -- `apply_trade` never reads `TradeExecution.
    record_a`/`record_b` (they exist for the JSONL log only), so any
    reconstructing-or-not record is fine here."""
    return DecisionRecord(
        team_id=team_id,
        philosophy=philosophy,
        window=window,
        subject=f"team:{team_id}",
        verdict="test",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


async def _make_league(
    pool, *, current_phase: Phase = Phase.PRESEASON, season: int = _SEASON
) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="GM Service Test League",
        start_season_year=season,
        current_season=season,
        commissioner_user_id=1,
        rng_seed=2026_10,
        current_phase=current_phase,
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
    roster_status: RosterStatus | None = None,
    season: int = _SEASON,
) -> int:
    status = roster_status if roster_status is not None else (
        RosterStatus.ACTIVE if team_id is not None else RosterStatus.FREE_AGENT
    )
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


# ---------------------------------------------------------------------------
# load_gm_inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_gm_inputs_returns_a_complete_consistent_snapshot_and_context():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(
                pool,
                league_id,
                "LA",
                gm_philosophy=GmPhilosophy.STAR_MAXER,
                timeline=Timeline.WIN_NOW,
            )
            team_b = await _make_team(
                pool,
                league_id,
                "LB",
                gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
                timeline=Timeline.REBUILD,
            )
            p1 = await _make_player(pool, league_id, team_a, overall_cached=85, age=27)
            await _make_contract(
                pool, league_id, team_a, p1, start_season=_SEASON, salaries=(20_000_000, 21_000_000)
            )
            p2 = await _make_player(pool, league_id, team_b, overall_cached=65, age=22)
            await _make_contract(
                pool, league_id, team_b, p2, start_season=_SEASON, salaries=(2_000_000,)
            )
            free_agent = await _make_player(
                pool,
                league_id,
                None,
                overall_cached=60,
                age=29,
                roster_status=RosterStatus.FREE_AGENT,
            )
            prospect = await _make_player(
                pool,
                league_id,
                None,
                overall_cached=55,
                age=19,
                roster_status=RosterStatus.DRAFT_ELIGIBLE,
            )

            snapshot, ctx = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )

            assert snapshot.league_id == league_id
            assert snapshot.season == _SEASON
            assert {t.id for t in snapshot.teams} == {team_a, team_b}
            assert {rp.id for rp in snapshot.team_by_id(team_a).roster} == {p1}
            assert {rp.id for rp in snapshot.team_by_id(team_b).roster} == {p2}

            all_roster_ids = {rp.id for t in snapshot.teams for rp in t.roster}
            assert free_agent not in all_roster_ids
            assert prospect not in all_roster_ids

            assert ctx.league_id == league_id
            assert ctx.season == _SEASON
            assert ctx.phase == Phase.PRESEASON
            assert ctx.cap_settings.season == _SEASON
            assert ctx.next_season_cap_settings.season == _SEASON + 1
            assert set(ctx.room_by_team) == {team_a, team_b}
            assert ctx.philosophy_by_team[team_a] == GmPhilosophy.STAR_MAXER
            assert ctx.philosophy_by_team[team_b] == GmPhilosophy.YOUTH_DEVELOPER
            assert ctx.window_by_team[team_a] == CompetitiveWindow.WIN_NOW
            assert ctx.window_by_team[team_b] == CompetitiveWindow.REBUILD
            assert len(ctx.rookie_scale) == 30
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_gm_inputs_raises_not_found_for_missing_league():
    pool = await get_pool()
    try:
        with pytest.raises(NotFoundError):
            await gm_service.load_gm_inputs(pool, league_id=999_999, season=_SEASON)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_gm_inputs_raises_a_clear_error_for_a_mismatched_season():
    """Nit 8 fix (critic round 19): before this fix, a `season` not equal
    to `league.current_season` would fall through to `room_by_team`'s own
    `next(... if p.season == season + 1)` generator expression and raise a
    bare, unhelpful `StopIteration` once `season + 1` fell outside `project_
    league_cap_sheets`'s horizon (which is always anchored to `league.
    current_season`, never to this function's own `season` parameter).
    Surfaced instead as a clear `ValidationError`, raised immediately
    rather than deep inside a dict comprehension.

    A real `league_cap_settings` row is created for the mismatched season
    too (`season + 10`, far outside the 5-season horizon anchored at
    `league.current_season`) so this scenario would reach the deep, real
    `StopIteration` site rather than being intercepted earlier by the
    unrelated "no cap_settings row for `season`" `NotFoundError` -- this is
    what makes a mutation-test removal of the season-mismatch guard above
    surface as the specific bug this fix targets, not some other error."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, season=_SEASON)
        try:
            # A team is required -- `room_by_team`'s dict comprehension
            # (the `next()` call this fix guards) iterates over `teams`,
            # and does nothing at all (raises nothing) for a team-less
            # league, which would make this test's mutation-removal check
            # falsely pass.
            await _make_team(pool, league_id, "MS1")
            far_season = _SEASON + 10
            await league_repo.create_cap_settings(
                pool,
                league_id=league_id,
                season=far_season,
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
            with pytest.raises(ValidationError):
                await gm_service.load_gm_inputs(pool, league_id=league_id, season=far_season)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# apply_signing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_signing_raises_on_a_still_live_contract_and_terminates_nothing():
    """RMLP §7.2 test 6, landed at build step 4 -- the deterministic,
    non-concurrent repro of bug (d)'s missing half: a `SigningExecution`
    for a player who is genuinely still under contract (`end_season >=
    start_season`) must raise, not silently void the live deal. Raw SQL
    ground truth (not the service's own return value): the contract is
    still `status='active'` with `terminated_in_season IS NULL` after the
    raise."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AS6")
            team_b = await _make_team(pool, league_id, "AS7")
            p = await _make_player(pool, league_id, team_a, overall_cached=80)
            contract_id = await _make_contract(
                pool, league_id, team_a, p, start_season=_SEASON, salaries=(5_000_000, 6_000_000)
            )

            execution = gm_service.SigningExecution(
                team_id=team_b,
                player_id=p,
                years=1,
                annual_salary=3_000_000,
                start_season=_SEASON,
                signing_method=SigningMethod.CAP_SPACE,
                record=_dummy_record(team_b),
            )
            with pytest.raises(ValidationError):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_signing(conn, execution, league_id=league_id)

            row = await pool.fetchrow(
                "SELECT status, terminated_in_season FROM contracts WHERE id = $1",
                contract_id,
            )
            assert row["status"] == "active"
            assert row["terminated_in_season"] is None
            # Nothing moved -- still on the original team.
            unmoved = await player_repo.get_player(pool, p)
            assert unmoved.team_id == team_a
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_signing_terminates_a_genuinely_expired_contract_then_signs():
    """The companion positive case: a contract whose `end_season <
    start_season` (genuinely expired, per `_load_free_agency_pool`'s own
    documented judgment call) is terminated with `EXPIRED` and the new
    signing proceeds."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AS8")
            team_b = await _make_team(pool, league_id, "AS9")
            p = await _make_player(pool, league_id, team_a, overall_cached=80)
            contract_id = await _make_contract(
                pool, league_id, team_a, p, start_season=_SEASON - 1, salaries=(4_000_000,)
            )

            execution = gm_service.SigningExecution(
                team_id=team_b,
                player_id=p,
                years=1,
                annual_salary=3_000_000,
                start_season=_SEASON,
                signing_method=SigningMethod.CAP_SPACE,
                record=_dummy_record(team_b),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_signing(conn, execution, league_id=league_id)

            old = await contract_repo.get_contract(pool, contract_id)
            assert old.status == ContractStatus.EXPIRED
            assert old.terminated_in_season == _SEASON - 1

            moved = await player_repo.get_player(pool, p)
            assert moved.team_id == team_b
            assert moved.roster_status == RosterStatus.ACTIVE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# apply_trade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_trade_moves_players_contracts_and_picks_atomically():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "TA")
            team_b = await _make_team(pool, league_id, "TB")
            p_a = await _make_player(pool, league_id, team_a, overall_cached=80)
            contract_a = await _make_contract(
                pool, league_id, team_a, p_a, start_season=_SEASON, salaries=(5_000_000,)
            )
            p_b = await _make_player(pool, league_id, team_b, overall_cached=75)
            contract_b = await _make_contract(
                pool, league_id, team_b, p_b, start_season=_SEASON, salaries=(4_000_000,)
            )
            pick = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON + 1, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(p_b,),
                picks_a_to_b=(pick.id,),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_trade(
                    conn, execution, _SEASON, Phase.TRADE_DEADLINE, league_id=league_id
                )

            moved_a = await player_repo.get_player(pool, p_a)
            moved_b = await player_repo.get_player(pool, p_b)
            assert moved_a.team_id == team_b
            assert moved_a.roster_status == RosterStatus.ACTIVE
            assert moved_b.team_id == team_a
            assert moved_b.roster_status == RosterStatus.ACTIVE

            contract_a_row = await contract_repo.get_contract(pool, contract_a)
            contract_b_row = await contract_repo.get_contract(pool, contract_b)
            assert contract_a_row.team_id == team_b
            assert contract_b_row.team_id == team_a

            pick_row = await draft_repo.get_draft_pick(pool, pick.id)
            assert pick_row.current_team_id == team_b

            # Decision P5-34/P5-74 -- the trade's own `league_transactions`
            # row, and its asset rows reproducing `TradeExecution`'s own
            # four id tuples exactly: every player and pick, both
            # directions, no extras. This is the real detector P5-74
            # replaces the vacuous digest-byte-identity claim with -- it
            # fails if the writer drops an asset, doubles one, or reverses
            # a direction, none of which the digest can see.
            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            assert len(transactions) == 1
            txn = transactions[0]
            assert txn.txn_type is TransactionType.TRADE
            assert txn.league_id == league_id
            assert txn.season == _SEASON
            assert txn.phase is Phase.TRADE_DEADLINE
            assert txn.actor_user_id is None
            assert txn.source_offer_id is None

            assets = await txn_repo.list_transaction_assets(pool, txn.id)
            player_moves = {
                (a.asset_kind, a.player_id, a.from_team_id, a.to_team_id)
                for a in assets
                if a.asset_kind is TransactionAssetKind.PLAYER
            }
            pick_moves = {
                (a.asset_kind, a.draft_pick_id, a.from_team_id, a.to_team_id)
                for a in assets
                if a.asset_kind is TransactionAssetKind.PICK
            }
            assert player_moves == {
                (TransactionAssetKind.PLAYER, p_a, team_a, team_b),
                (TransactionAssetKind.PLAYER, p_b, team_b, team_a),
            }
            assert pick_moves == {
                (TransactionAssetKind.PICK, pick.id, team_a, team_b),
            }
            assert len(assets) == 3
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_clears_a_traded_players_directive():
    """PD-8, mutation 15 (docs/design/gm-command-surface.md): a directive
    set by the SENDING team's GM must not keep applying to the player
    under the RECEIVING team -- `player_directives` is `UNIQUE (league_id,
    player_id)` with no `team_id` column at all (migration 005), so
    nothing else would ever clear it."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PA")
            team_b = await _make_team(pool, league_id, "PB")
            p_a = await _make_player(pool, league_id, team_a, overall_cached=80)
            p_b = await _make_player(pool, league_id, team_b, overall_cached=75)
            await _make_contract(
                pool, league_id, team_a, p_a, start_season=_SEASON, salaries=(5_000_000,)
            )
            await _make_contract(
                pool, league_id, team_b, p_b, start_season=_SEASON, salaries=(4_000_000,)
            )
            await role_repo.upsert_player_directive(
                pool, league_id=league_id, player_id=p_a, shot_diet=ShotDiet.MORE_THREES
            )

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(p_b,),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_trade(
                    conn, execution, _SEASON, Phase.TRADE_DEADLINE, league_id=league_id
                )

            assert await role_repo.get_player_directive(pool, league_id, p_a) is None
            # The untraded player's own directive (never set here) stays
            # exactly that -- no directive at all, not "cleared" (there
            # was nothing to clear); named so a future reader doesn't
            # misread its absence as this assertion's own target.
            assert await role_repo.get_player_directive(pool, league_id, p_b) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_rolls_back_atomically_on_a_mid_write_failure(monkeypatch):
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "RA")
            team_b = await _make_team(pool, league_id, "RB")
            p_a = await _make_player(pool, league_id, team_a, overall_cached=80)
            contract_a = await _make_contract(
                pool, league_id, team_a, p_a, start_season=_SEASON, salaries=(5_000_000,)
            )
            p_b = await _make_player(pool, league_id, team_b, overall_cached=75)
            contract_b = await _make_contract(
                pool, league_id, team_b, p_b, start_season=_SEASON, salaries=(4_000_000,)
            )
            pick = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON + 1, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(p_b,),
                picks_a_to_b=(pick.id,),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )

            def _boom(*args, **kwargs):
                raise RuntimeError("injected mid-write failure")

            # Fires on the pick-reassignment step, which runs last inside
            # apply_trade -- both players' roster/contract moves (which run
            # first) must be genuinely undone, not merely never reach the
            # pick, proving the whole function is one atomic unit and not
            # four independently-committed writes.
            monkeypatch.setattr(gm_service.draft_repo, "set_current_team", _boom)

            with pytest.raises(RuntimeError):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, _SEASON, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved_a = await player_repo.get_player(pool, p_a)
            unmoved_b = await player_repo.get_player(pool, p_b)
            assert unmoved_a.team_id == team_a
            assert unmoved_b.team_id == team_b

            contract_a_row = await contract_repo.get_contract(pool, contract_a)
            contract_b_row = await contract_repo.get_contract(pool, contract_b)
            assert contract_a_row.team_id == team_a
            assert contract_b_row.team_id == team_b

            pick_row = await draft_repo.get_draft_pick(pool, pick.id)
            assert pick_row.current_team_id == team_a
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_apply_trade_rolls_back_atomically_when_the_log_write_fails(monkeypatch):
    """Decision P5-34/P5-74's own atomicity claim, made specific and
    tested: a trade whose `league_transactions` write raises leaves no
    player moved, no contract moved, no pick reassigned -- the log row
    is not bolted on after the fact, it is inside the same transaction
    as the roster moves it describes."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "LA")
            team_b = await _make_team(pool, league_id, "LB")
            p_a = await _make_player(pool, league_id, team_a, overall_cached=80)
            contract_a = await _make_contract(
                pool, league_id, team_a, p_a, start_season=_SEASON, salaries=(5_000_000,)
            )
            p_b = await _make_player(pool, league_id, team_b, overall_cached=75)
            contract_b = await _make_contract(
                pool, league_id, team_b, p_b, start_season=_SEASON, salaries=(4_000_000,)
            )
            pick = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON + 1, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(p_b,),
                picks_a_to_b=(pick.id,),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )

            def _boom(*args, **kwargs):
                raise RuntimeError("injected log-write failure")

            monkeypatch.setattr(gm_service.txn_repo, "create_trade_transaction", _boom)

            with pytest.raises(RuntimeError):
                async with rmlp_gate(pool, league_id) as conn:
                    await gm_service.apply_trade(
                        conn, execution, _SEASON, Phase.TRADE_DEADLINE, league_id=league_id
                    )

            unmoved_a = await player_repo.get_player(pool, p_a)
            unmoved_b = await player_repo.get_player(pool, p_b)
            assert unmoved_a.team_id == team_a
            assert unmoved_b.team_id == team_b

            contract_a_row = await contract_repo.get_contract(pool, contract_a)
            contract_b_row = await contract_repo.get_contract(pool, contract_b)
            assert contract_a_row.team_id == team_a
            assert contract_b_row.team_id == team_b

            pick_row = await draft_repo.get_draft_pick(pool, pick.id)
            assert pick_row.current_team_id == team_a

            assert await txn_repo.list_transactions_for_league(pool, league_id) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# run_phase_hook -- FREE_AGENCY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_agency_phase_hook_produces_legal_contracts_that_persist():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.FREE_AGENCY)
        try:
            team = await _make_team(pool, league_id, "FA1", gm_philosophy=GmPhilosophy.EGALITARIAN)
            for i in range(3):
                pid = await _make_player(pool, league_id, team, overall_cached=70 + i, age=27)
                await _make_contract(
                    pool, league_id, team, pid, start_season=_SEASON, salaries=(2_000_000,)
                )
            # Below `roster_min` (14) on purpose -- `_enforce_roster_floor`
            # then deterministically signs every remaining pool player
            # regardless of the normal bidding path, which is what makes
            # this test's "at least one signing" assertion a guarantee
            # rather than a hope about how the bidding resolves.
            for i in range(3):
                await _make_player(
                    pool,
                    league_id,
                    None,
                    overall_cached=68 + i,
                    age=26,
                    roster_status=RosterStatus.FREE_AGENT,
                )

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.FREE_AGENCY, actor_user_id=1
            )

            assert result.free_agency is not None
            assert len(result.free_agency.signings) > 0
            for signing in result.free_agency.signings:
                player_row = await player_repo.get_player(pool, signing.player_id)
                assert player_row.team_id == signing.team_id
                assert player_row.roster_status == RosterStatus.ACTIVE

                contracts = await contract_repo.list_contracts_for_player(pool, signing.player_id)
                active = [c for c in contracts if c.status == ContractStatus.ACTIVE]
                assert len(active) == 1, "exactly one active contract per signed player"
                assert active[0].team_id == signing.team_id

                years = await contract_repo.list_contract_years_for_contract(pool, active[0].id)
                assert len(years) == signing.years
                assert all(y.salary == signing.annual_salary for y in years)
                assert {y.season for y in years} == set(
                    range(signing.start_season, signing.start_season + signing.years)
                )
        finally:
            # Nit 8 cleanup (critic round 19) -- the FREE_AGENCY branch
            # writes `free_agency.records` to the JSONL artifact; this was
            # previously left behind (cosmetic only, `artifacts/` is
            # gitignored, but still sloppy).
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_free_agency_phase_hook_expires_the_old_contract_of_a_re_signed_player():
    """The wiring finding `apply_signing`'s own docstring flags: an
    "expiring" player (still on an ACTIVE contract whose last year is the
    current season) that re-signs must have their old contract moved off
    `active` -- otherwise `roster_service.sign`'s own "one active contract
    per player" guard would have raised inside `apply_signing` and this
    whole phase hook would never have completed."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.FREE_AGENCY)
        try:
            team = await _make_team(pool, league_id, "EX1", gm_philosophy=GmPhilosophy.EGALITARIAN)
            expiring_player = await _make_player(pool, league_id, team, overall_cached=75, age=28)
            old_contract = await _make_contract(
                pool, league_id, team, expiring_player, start_season=_SEASON, salaries=(3_000_000,)
            )
            # Keep the team well below roster_min so _enforce_roster_floor
            # is guaranteed to re-sign the expiring player back (the pool
            # has no other candidates for it to prefer).
            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.FREE_AGENCY, actor_user_id=1
            )

            assert result.free_agency is not None
            signed_ids = {s.player_id for s in result.free_agency.signings}
            assert expiring_player in signed_ids

            old_row = await contract_repo.get_contract(pool, old_contract)
            assert old_row.status == ContractStatus.EXPIRED

            all_contracts = await contract_repo.list_contracts_for_player(pool, expiring_player)
            active = [c for c in all_contracts if c.status == ContractStatus.ACTIVE]
            assert len(active) == 1
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)  # nit 8
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# run_phase_hook -- agency_enabled gate (critic B1 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_agency_hook_agency_enabled_false_skips_motivation_loader(monkeypatch):
    """Critic B1: before this fix, `run_phase_hook` had no `agency_enabled`
    parameter at all and called `load_motivation_for_league` unconditionally
    -- production had no way to suppress agency despite two `GmContext`
    docstrings claiming it was "gated on `agency_enabled`." Proven here
    against real Postgres: `load_motivation_for_league` is monkeypatched to
    a spy that would raise if ever called, and `run_free_agency` is spied on
    to capture the exact `ctx` it receives. `agency_enabled=False` must
    both skip the loader entirely and leave `ctx.motivation_by_player` at
    its `None` default."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.FREE_AGENCY)
        try:
            team = await _make_team(pool, league_id, "AG1", gm_philosophy=GmPhilosophy.EGALITARIAN)
            for i in range(3):
                pid = await _make_player(pool, league_id, team, overall_cached=70 + i, age=27)
                await _make_contract(
                    pool, league_id, team, pid, start_season=_SEASON, salaries=(2_000_000,)
                )

            def _boom(*args, **kwargs):
                raise AssertionError(
                    "load_motivation_for_league must not be called when agency_enabled=False"
                )

            monkeypatch.setattr(gm_service, "load_motivation_for_league", _boom)

            captured: dict[str, object] = {}
            real_run_free_agency = gm_service.run_free_agency

            def _spy_run_free_agency(snapshot, ctx, **kwargs):
                captured["motivation_by_player"] = ctx.motivation_by_player
                return real_run_free_agency(snapshot, ctx, **kwargs)

            monkeypatch.setattr(gm_service, "run_free_agency", _spy_run_free_agency)

            await gm_service.run_phase_hook(
                pool,
                league_id=league_id,
                phase=Phase.FREE_AGENCY,
                actor_user_id=1,
                agency_enabled=False,
            )

            assert captured["motivation_by_player"] is None
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_free_agency_hook_agency_enabled_defaults_true_and_calls_loader(monkeypatch):
    """Default-preservation guard, mirroring `head_to_head_enabled`'s own
    `test_run_seed_head_to_head_enabled_defaults_true_and_memory_still_
    accumulates` precedent: a caller that never passes `agency_enabled`
    (every real caller today, since `run_phase_hook` isn't wired to a route
    yet) must still get the loader called, exactly as before this fix."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.FREE_AGENCY)
        try:
            team = await _make_team(pool, league_id, "AG2", gm_philosophy=GmPhilosophy.EGALITARIAN)
            for i in range(3):
                pid = await _make_player(pool, league_id, team, overall_cached=70 + i, age=27)
                await _make_contract(
                    pool, league_id, team, pid, start_season=_SEASON, salaries=(2_000_000,)
                )

            calls: list[int] = []
            real_loader = gm_service.load_motivation_for_league

            async def _spy_loader(ex, *, league_id):
                calls.append(league_id)
                return await real_loader(ex, league_id=league_id)

            monkeypatch.setattr(gm_service, "load_motivation_for_league", _spy_loader)

            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.FREE_AGENCY, actor_user_id=1
            )

            assert calls == [league_id]
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_regular_season_hook_agency_enabled_false_skips_trade_pressure_loader(monkeypatch):
    """Same B1 gate, the trade-window side (`REGULAR_SEASON`/`TRADE_
    DEADLINE`'s `load_trade_request_pressure_for_league` call)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            await _make_team(pool, league_id, "AG3", timeline=Timeline.WIN_NOW)
            await _make_team(pool, league_id, "AG4", timeline=Timeline.WIN_NOW)

            def _boom(*args, **kwargs):
                raise AssertionError(
                    "load_trade_request_pressure_for_league must not be called when "
                    "agency_enabled=False"
                )

            monkeypatch.setattr(gm_service, "load_trade_request_pressure_for_league", _boom)

            captured: dict[str, object] = {}
            real_run_trade_window = gm_service.run_trade_window

            def _spy_run_trade_window(snapshot, ctx):
                captured["trade_request_pressure_by_player"] = ctx.trade_request_pressure_by_player
                return real_run_trade_window(snapshot, ctx)

            monkeypatch.setattr(gm_service, "run_trade_window", _spy_run_trade_window)

            await gm_service.run_phase_hook(
                pool,
                league_id=league_id,
                phase=Phase.REGULAR_SEASON,
                actor_user_id=1,
                agency_enabled=False,
            )

            assert captured["trade_request_pressure_by_player"] is None
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# run_phase_hook -- DRAFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_phase_hook_produces_one_selection_per_pick_with_no_duplicates():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "DR1", gm_philosophy=GmPhilosophy.STAR_MAXER
            )
            team_b = await _make_team(
                pool, league_id, "DR2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
            )

            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=4
            )
            order = [team_a, team_b, team_a, team_b]
            picks = []
            for i, tid in enumerate(order, start=1):
                pick = await draft_repo.create_draft_pick(
                    pool, league_id=league_id, season=_SEASON, round=1,
                    original_team_id=tid, current_team_id=tid, pick_number=i,
                )
                picks.append(pick)

            prospect_ids = [
                await _make_player(
                    pool, league_id, None, overall_cached=50 + i, age=19,
                    roster_status=RosterStatus.DRAFT_ELIGIBLE,
                )
                for i in range(6)
            ]

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )

            assert result.draft is not None
            assert len(result.draft.picks) == 4

            selections = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert len(selections) == 4
            selected_players = [s.player_id for s in selections]
            assert len(selected_players) == len(set(selected_players)), "no player drafted twice"
            assert set(selected_players) <= set(prospect_ids)

            for pick, pick_result in zip(picks, result.draft.picks, strict=True):
                pick_row = await draft_repo.get_draft_pick(pool, pick.id)
                assert pick_row.used_for_player_id == pick_result.prospect_id

            draft_row = await draft_repo.get_draft(pool, draft.id)
            assert (draft_row.current_round, draft_row.current_pick_number) != (1, 1)
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)  # nit 8
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_phase_hook_skips_a_pick_with_unresolved_pick_number():
    """B1 fix (critic round 19): `draft_picks.pick_number` is nullable
    (unresolved until the lottery runs), but `draft_selections.pick_number`
    is NOT NULL. Before the fix, `run_phase_hook`'s DRAFT branch passed a
    `None` `pick_number` straight through to `create_draft_selection`,
    crashing with a raw `NotNullViolationError`. A pick with `pick_number
    is None` must instead be silently skipped -- it isn't ready to be
    drafted -- while every resolved pick still gets drafted normally."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "UN1", gm_philosophy=GmPhilosophy.STAR_MAXER
            )
            team_b = await _make_team(
                pool, league_id, "UN2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
            )

            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=3
            )
            resolved_pick_1 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a, pick_number=1,
            )
            unresolved_pick = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_b, current_team_id=team_b, pick_number=None,
            )
            resolved_pick_2 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a, pick_number=2,
            )

            for i in range(3):
                await _make_player(
                    pool, league_id, None, overall_cached=50 + i, age=19,
                    roster_status=RosterStatus.DRAFT_ELIGIBLE,
                )

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )

            assert result.draft is not None
            # Only the two resolved picks are eligible -- the unresolved
            # one is filtered out before `run_draft_phase` ever sees it.
            assert len(result.draft.picks) == 2

            selections = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert len(selections) == 2
            assert {s.pick_asset_id for s in selections} == {
                resolved_pick_1.id, resolved_pick_2.id,
            }

            unresolved_row = await draft_repo.get_draft_pick(pool, unresolved_pick.id)
            assert unresolved_row.used_for_player_id is None
            assert unresolved_row.pick_number is None
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_phase_hook_is_idempotent_on_a_second_invocation():
    """B2 fix (critic round 19): re-invoking the DRAFT hook after every
    pick has already been used must not re-draft the same prospects into
    the same picks -- before the fix this crashed with a raw
    `UniqueViolationError` on `draft_selections`' own `(draft_id, pick_
    number)` unique constraint. The second call must instead cleanly
    return `draft=None` (a legitimate no-op -- nothing left to draft) and
    leave the first call's own selections untouched."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "ID1", gm_philosophy=GmPhilosophy.STAR_MAXER
            )
            team_b = await _make_team(
                pool, league_id, "ID2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
            )
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=2
            )
            for i, tid in enumerate((team_a, team_b), start=1):
                await draft_repo.create_draft_pick(
                    pool, league_id=league_id, season=_SEASON, round=1,
                    original_team_id=tid, current_team_id=tid, pick_number=i,
                )
            for i in range(4):
                await _make_player(
                    pool, league_id, None, overall_cached=50 + i, age=19,
                    roster_status=RosterStatus.DRAFT_ELIGIBLE,
                )

            first = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )
            assert first.draft is not None
            assert len(first.draft.picks) == 2
            first_selections = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert len(first_selections) == 2

            second = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )
            assert second.draft is None

            second_selections = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert len(second_selections) == 2
            assert {s.id for s in second_selections} == {s.id for s in first_selections}
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_phase_hook_with_no_prospects_loaded_returns_none_and_writes_nothing():
    """Nit 4 (T4 re-verification round): `picks` can be non-empty (real,
    resolved, unused picks exist) while `prospects` is empty -- no
    `draft_eligible` player has been loaded yet (Gap F, `_load_draft_
    prospects`'s own docstring). `run_draft_phase` then returns
    `DraftResult(picks=())` (`draft.run_draft` breaks on its first empty-
    pool check), which must be normalized to `draft=None` on
    `PhaseHookResult` -- the same "nothing was drafted" representation as
    every other no-op branch on this hook, not a bare-empty-tuple result a
    caller would have to special-case against the documented `None`
    contract."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "NP1", gm_philosophy=GmPhilosophy.STAR_MAXER
            )
            team_b = await _make_team(
                pool, league_id, "NP2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
            )
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=2
            )
            for i, tid in enumerate((team_a, team_b), start=1):
                await draft_repo.create_draft_pick(
                    pool, league_id=league_id, season=_SEASON, round=1,
                    original_team_id=tid, current_team_id=tid, pick_number=i,
                )
            # Deliberately no `_make_player(..., roster_status=DRAFT_ELIGIBLE)`
            # calls -- `prospects` must load empty.

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )

            assert result.draft is None

            selections = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert selections == []
            assert not gm_service.artifact_path(league_id, _SEASON).exists()
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def _assert_picks_valued_at_their_real_pick_number(
    pool, league_id: int, draft_result_picks, real_pick_numbers: list[int]
) -> None:
    """Shared verification for both T4 architecture-gap regression tests
    below: independently recomputes `prospect_value_for` at each pick's
    REAL pick number (never re-derived from `draft_result.picks`' own list
    position) and confirms it agrees with the persisted `DecisionRecord`'s
    own `market_value` term -- the thing that was actually priced and the
    thing that got recorded must not disagree. Also recomputes the same
    prospect at the WRONG (1-indexed positional) slot and confirms it
    differs, so a regression back to positional derivation would be caught
    rather than passing by coincidence."""
    _, ctx = await gm_service.load_gm_inputs(pool, league_id=league_id, season=_SEASON)
    prospects = await gm_service._load_draft_prospects(pool, league_id)
    prospect_by_id = {rp.id: rp for rp in prospects}

    assert [p.pick_number for p in draft_result_picks] == real_pick_numbers
    assert [p.record.subject for p in draft_result_picks] == [
        f"pick:{pn}" for pn in real_pick_numbers
    ]

    for pick_result, real_pick_number, positional_index in zip(
        draft_result_picks, real_pick_numbers, range(1, len(draft_result_picks) + 1), strict=True
    ):
        winner = prospect_by_id[pick_result.prospect_id]
        team_id = pick_result.team_id
        expected = draft_decisions.prospect_value_for(
            winner,
            league_rng_seed=ctx.league_rng_seed,
            team_id=team_id,
            season=_SEASON,
            philosophy=ctx.philosophy_by_team[team_id],
            window=ctx.window_by_team[team_id],
            cap_settings=ctx.cap_settings,
            rookie_scale=ctx.rookie_scale,
            implied_slot=real_pick_number,
        )
        market_value_term = next(
            t for t in pick_result.record.terms if t.label == "market_value"
        )
        assert market_value_term.input_value == pytest.approx(expected, abs=1e-6), (
            f"pick {real_pick_number}: DecisionRecord's own market_value term does not "
            f"match prospect_value_for at the real pick_number"
        )

        if positional_index != real_pick_number:
            wrong = draft_decisions.prospect_value_for(
                winner,
                league_rng_seed=ctx.league_rng_seed,
                team_id=team_id,
                season=_SEASON,
                philosophy=ctx.philosophy_by_team[team_id],
                window=ctx.window_by_team[team_id],
                cap_settings=ctx.cap_settings,
                rookie_scale=ctx.rookie_scale,
                implied_slot=positional_index,
            )
            assert wrong != pytest.approx(expected, abs=1e-6), (
                f"pick {real_pick_number}: real slot and its wrong positional slot "
                f"({positional_index}) priced identically -- this test would not catch a "
                f"regression back to positional pick_number derivation"
            )


@pytest.mark.asyncio
async def test_draft_phase_hook_prices_interleaved_unresolved_pick_at_its_real_pick_number():
    """T4 architecture-gap regression (critic round): `draft.py::DraftSlot`
    had no `pick_number` field at step 9, so `run_draft` derived it from
    loop position -- broke the moment `_eligible_draft_picks_sorted`'s own
    B1 filter (this fix round) removed a lottery-unresolved pick from the
    middle of a real draft order. Real picks `[NULL, 2, 3]` filter down to
    `[2, 3]`; before this fix, `run_draft`'s positional `enumerate` priced
    those two remaining picks at `implied_slot` `[1, 2]` instead of their
    real `[2, 3]`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "IL1", gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            team_b = await _make_team(
                pool, league_id, "IL2", gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=3
            )
            await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_b, current_team_id=team_b, pick_number=None,
            )
            pick_2 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a, pick_number=2,
            )
            pick_3 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_b, current_team_id=team_b, pick_number=3,
            )

            for i in range(4):
                await _make_player(
                    pool, league_id, None, overall_cached=50 + i, age=19,
                    roster_status=RosterStatus.DRAFT_ELIGIBLE,
                )

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )

            assert result.draft is not None
            assert len(result.draft.picks) == 2
            await _assert_picks_valued_at_their_real_pick_number(
                pool, league_id, result.draft.picks,
                [pick_2.pick_number, pick_3.pick_number],
            )
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_phase_hook_resumed_draft_prices_remaining_picks_at_their_real_pick_number():
    """T4 architecture-gap regression (critic round), second repro: a
    resumed draft (picks 1-2 already used, e.g. from an earlier hook
    invocation) leaves real remaining picks `[3, 4]` -- before this fix,
    `run_draft`'s positional `enumerate` over the filtered, 2-long
    remaining order priced them at `implied_slot` `[1, 2]` instead of their
    real `[3, 4]`, pricing every remaining prospect as if picking #1
    overall."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_a = await _make_team(
                pool, league_id, "RS1", gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            team_b = await _make_team(
                pool, league_id, "RS2", gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=4
            )
            already_drafted_player = await _make_player(
                pool, league_id, None, overall_cached=60, age=25,
                roster_status=RosterStatus.ACTIVE,
            )
            await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a, pick_number=1,
                used_for_player_id=already_drafted_player,
            )
            await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_b, current_team_id=team_b, pick_number=2,
                used_for_player_id=already_drafted_player,
            )
            pick_3 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a, pick_number=3,
            )
            pick_4 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_b, current_team_id=team_b, pick_number=4,
            )

            for i in range(4):
                await _make_player(
                    pool, league_id, None, overall_cached=50 + i, age=19,
                    roster_status=RosterStatus.DRAFT_ELIGIBLE,
                )

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.DRAFT, actor_user_id=1
            )

            assert result.draft is not None
            assert len(result.draft.picks) == 2
            await _assert_picks_valued_at_their_real_pick_number(
                pool, league_id, result.draft.picks,
                [pick_3.pick_number, pick_4.pick_number],
            )
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


def test_eligible_draft_picks_sorted_filters_and_applies_id_tiebreak():
    """B1 tiebreak proof, exercised directly against `_eligible_draft_
    picks_sorted` (no Postgres round trip) -- a real end-to-end query
    against a freshly-inserted tiny table happens, in practice, to already
    return tied `(round, pick_number)` rows in ascending-`id`/insertion
    order, which would make a broken tiebreak invisible to an
    integration-only test. Feeding a hand-built list in DESCENDING `id`
    order (the opposite of what any real query would incidentally return)
    is what actually proves the explicit `(round, pick_number, id)` sort
    key is doing real work, not merely riding along on Postgres's own
    incidental row order."""
    def _pick(id_: int, *, round_: int = 1, pick_number: int | None = 1,
              used_for_player_id: int | None = None) -> DraftPick:
        return DraftPick(
            id=id_, league_id=1, season=_SEASON, round=round_,
            original_team_id=1, current_team_id=1, pick_number=pick_number,
            used_for_player_id=used_for_player_id, protection_type=ProtectionType.NONE,
            protection_through=None, swap_with_pick_id=None, conveyed=False,
            rolls_over_to_season=None,
        )

    unresolved = _pick(1, pick_number=None)
    already_used = _pick(2, used_for_player_id=999)
    # Two picks sharing (round=1, pick_number=1), fed in DESCENDING id
    # order -- the tiebreak must still resolve them ascending by id.
    tied_high_id = _pick(30, round_=2, pick_number=5)
    tied_low_id = _pick(10, round_=2, pick_number=5)
    resolved_round_1 = _pick(3, pick_number=2)

    ordered = gm_service._eligible_draft_picks_sorted(
        [unresolved, already_used, tied_high_id, tied_low_id, resolved_round_1]
    )

    assert [p.id for p in ordered] == [3, 10, 30]


# ---------------------------------------------------------------------------
# run_phase_hook -- TRADE_DEADLINE CONTEND-reachability investigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_deadline_hook_threads_freshly_classified_contend_into_trade_branch(
    monkeypatch,
):
    """CONTEND investigation, critic round 19. Critic found `ctx.window_by_
    team` seeded from a `load_gm_inputs` reload can never hold `Competitive
    Window.CONTEND`: `apply_windows` persists via `window.to_timeline`,
    which collapses `CONTEND` into the single stored `Timeline.WIN_NOW`
    value (Decision P2-13), and `_TIMELINE_TO_WINDOW`'s reverse map -- by
    construction, `Timeline` has only three values -- can only ever produce
    `WIN_NOW` back out of that column, in every case, not merely "one cycle
    of hysteresis lag" behind as originally assumed.

    Proven here directly against real Postgres: `classify_all_windows` is
    monkeypatched to force a `CONTEND` result for one team (bypassing the
    real ranking/roster-tuning machinery needed to make CONTEND emerge
    naturally from `window_score`, which is orthogonal to this fix -- the
    fix is about whether a same-call CONTEND classification reaches the
    trade branch at all, not about how CONTEND gets classified), `apply_
    windows` still runs for real (so `teams.timeline` genuinely ends up
    `win_now`, proving the persistence really is lossy), and `run_trade_
    window` is monkeypatched to a probe that records the exact `ctx` it
    was called with. Asserts the trade branch's `ctx.window_by_team` still
    holds `CONTEND` for that team despite the DB column holding `win_now`
    -- i.e. `run_phase_hook`'s `dataclasses.replace(ctx, window_by_team=
    window_pass.window_by_team)` fix is what carries it across, not the
    second `load_gm_inputs` reload."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.TRADE_DEADLINE)
        try:
            team_a = await _make_team(pool, league_id, "CT1", timeline=Timeline.WIN_NOW)
            team_b = await _make_team(pool, league_id, "CT2", timeline=Timeline.WIN_NOW)

            real_classify = gm_service.classify_all_windows

            def _fake_classify(snapshot, **kwargs):
                result = real_classify(snapshot, **kwargs)
                forced = dict(result.window_by_team)
                forced[team_a] = CompetitiveWindow.CONTEND
                return dataclasses.replace(result, window_by_team=forced)

            monkeypatch.setattr(gm_service, "classify_all_windows", _fake_classify)

            captured: dict[str, object] = {}
            real_run_trade_window = gm_service.run_trade_window

            def _spy_run_trade_window(snapshot, ctx):
                captured["window_by_team"] = ctx.window_by_team
                return real_run_trade_window(snapshot, ctx)

            monkeypatch.setattr(gm_service, "run_trade_window", _spy_run_trade_window)

            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.TRADE_DEADLINE, actor_user_id=1
            )

            # The persisted column really did collapse CONTEND -> win_now.
            team_a_row = await team_repo.get_team(pool, team_a)
            assert team_a_row.timeline == Timeline.WIN_NOW

            # But the SAME call's trade branch still saw CONTEND, carried
            # in-memory rather than reloaded from that lossy column.
            assert captured["window_by_team"][team_a] == CompetitiveWindow.CONTEND
            # team_b's own (unforced) classification is still present too --
            # the fix overwrites the whole `window_by_team` mapping with
            # `window_pass`'s own, not just the one forced team.
            assert team_b in captured["window_by_team"]
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_regular_season_hook_trade_branch_reads_persisted_window_unaffected(monkeypatch):
    """Control case for the CONTEND fix above: `REGULAR_SEASON` never runs
    window classification (`window_pass` stays `None`), so its trade
    branch's `ctx.window_by_team` must be untouched by the `dataclasses.
    replace` fix and keep reading whatever `teams.timeline` actually
    holds -- proving the fix is scoped to phases that classify AND trade
    in the same call, not a blanket change to every trade branch."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_a = await _make_team(pool, league_id, "RS1", timeline=Timeline.REBUILD)
            team_b = await _make_team(pool, league_id, "RS2", timeline=Timeline.WIN_NOW)

            captured: dict[str, object] = {}
            real_run_trade_window = gm_service.run_trade_window

            def _spy_run_trade_window(snapshot, ctx):
                captured["window_by_team"] = ctx.window_by_team
                return real_run_trade_window(snapshot, ctx)

            monkeypatch.setattr(gm_service, "run_trade_window", _spy_run_trade_window)
            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.REGULAR_SEASON, actor_user_id=1
            )

            assert result.window_pass is None
            assert captured["window_by_team"][team_a] == CompetitiveWindow.REBUILD
            assert captured["window_by_team"][team_b] == CompetitiveWindow.WIN_NOW
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# run_phase_hook -- no-op phase + JSONL determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_phase_hook_at_a_no_op_phase_returns_empty_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.WAIVERS)
        try:
            path = gm_service.artifact_path(league_id, _SEASON)
            path.unlink(missing_ok=True)

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.WAIVERS, actor_user_id=1
            )

            assert result.window_pass is None
            assert result.trade_window is None
            assert result.resign_offers is None
            assert result.free_agency is None
            assert result.draft is None
            assert not path.exists()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_phase_hook_writes_byte_identical_jsonl_across_two_runs():
    """Family-1 invariant 6: two runs of the same phase hook, over
    unchanged DB state and the same league/season/rng seed, must produce
    byte-identical JSONL output. Runs the PRESEASON window-classification
    hook once, captures the file, resets the one thing it wrote
    (`teams.timeline`) back to its pre-run value, deletes the file, and
    runs the identical hook call again -- the two files' bytes must match
    exactly, including line endings (`\\r\\n` would mean the `newline=""`
    contract broke on this platform)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.PRESEASON)
        try:
            team_a = await _make_team(
                pool, league_id, "DT1", gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            team_b = await _make_team(
                pool, league_id, "DT2", gm_philosophy=GmPhilosophy.STAR_MAXER
            )
            for team_id, base_overall in ((team_a, 82), (team_b, 60)):
                for i in range(8):
                    pid = await _make_player(
                        pool, league_id, team_id, overall_cached=base_overall - i, age=24 + (i % 6)
                    )
                    await _make_contract(
                        pool, league_id, team_id, pid, start_season=_SEASON, salaries=(3_000_000,)
                    )

            path = gm_service.artifact_path(league_id, _SEASON)
            path.unlink(missing_ok=True)

            before_timelines = {
                team_id: (await team_repo.get_team(pool, team_id)).timeline
                for team_id in (team_a, team_b)
            }

            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.PRESEASON, actor_user_id=1
            )
            content_1 = path.read_bytes()
            assert content_1, "the window-classification branch must write at least one line"
            assert b"\r\n" not in content_1, "newline=\"\" must prevent CRLF translation on write"
            path.unlink()

            for team_id, timeline in before_timelines.items():
                await team_repo.set_timeline(pool, team_id, timeline)

            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.PRESEASON, actor_user_id=1
            )
            content_2 = path.read_bytes()

            assert content_1 == content_2
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Phase 5 Stage A step A-23 -- the human-managed-team skip (Decisions
# P5-44 through P5-91), proof obligation per Decision P5-50 as amended by
# P5-65: a must-fire synthetic-human integration test (the first test in
# this project's history where `manager_user_id` is not `None`) and the
# `ctx` mapping-completeness check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_gm_inputs_ctx_mappings_stay_complete_with_a_human_managed_team():
    """Decision P5-50 item 5 / P5-45's own named trap -- every by-team
    mapping (`window_by_team`, `philosophy_by_team`, `room_by_team`,
    `apron1_room_by_team`) must still carry all of a league's team ids
    even when one is human-managed, because an AI team reads these
    mappings for BOTH itself and its negotiating partners (`run_trade_
    window:3691-3693` reads `ctx.window_by_team[b_id]`/`room_by_team[b_id]`
    for the PARTNER too). A skip implemented by removing a human team from
    a context mapping, rather than by an actor-loop `continue`, turns it
    into a `KeyError` the first time any AI team looks at it -- this test
    would fail against that implementation shape."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_ai_1 = await _make_team(pool, league_id, "M1", timeline=Timeline.WIN_NOW)
            team_ai_2 = await _make_team(pool, league_id, "M2", timeline=Timeline.RETOOL)
            team_human = await _make_team(pool, league_id, "M3", timeline=Timeline.REBUILD)
            await team_repo.set_manager(pool, team_human, 555_000_555)

            snapshot, ctx = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )

            all_team_ids = {team_ai_1, team_ai_2, team_human}
            assert ctx.self_managed_team_ids == {team_human}
            assert set(ctx.window_by_team) == all_team_ids
            assert set(ctx.philosophy_by_team) == all_team_ids
            assert set(ctx.room_by_team) == all_team_ids
            assert set(ctx.apron1_room_by_team) == all_team_ids
            assert {t.id for t in snapshot.teams} == all_team_ids
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_gm_inputs_excludes_an_auto_piloted_team_from_self_managed_team_ids():
    """GM-2 (docs/design/gm-command-surface.md), mutation 1 -- the exact
    fixture `test_load_gm_inputs_ctx_mappings_stay_complete_with_a_human_
    managed_team` above uses, plus one flag. A team with a manager AND
    `auto_pilot=True` keeps its manager (it is still human-MANAGED -- the
    by-team mappings below must still carry it) but is no longer SELF-
    managed: it must be absent from `ctx.self_managed_team_ids`, which is
    what every `if team_id in ctx.self_managed_team_ids: continue` in the
    AI decision layer (the resign window, the free-agency bidder loop, AI
    waivers, the AI trade window) reads to decide whether to act for a
    team. Dropping `and not t.auto_pilot` from the derivation at
    `gm_service.py`'s `load_gm_inputs` would put this team back in the
    set and turn this assertion red."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_self_managed = await _make_team(pool, league_id, "M1", timeline=Timeline.WIN_NOW)
            await team_repo.set_manager(pool, team_self_managed, 555_000_111)
            team_auto_pilot = await _make_team(pool, league_id, "M2", timeline=Timeline.RETOOL)
            await team_repo.set_manager(pool, team_auto_pilot, 555_000_222)
            await team_repo.set_auto_pilot(
                pool, team_auto_pilot, auto_pilot=True, gm_philosophy=GmPhilosophy.EGALITARIAN
            )
            team_ai = await _make_team(pool, league_id, "M3", timeline=Timeline.REBUILD)

            snapshot, ctx = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )

            all_team_ids = {team_self_managed, team_auto_pilot, team_ai}
            assert ctx.self_managed_team_ids == {team_self_managed}
            assert set(ctx.window_by_team) == all_team_ids
            assert set(ctx.philosophy_by_team) == all_team_ids
            assert {t.id for t in snapshot.teams} == all_team_ids
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_regular_season_hook_ai_offer_to_human_team_persists_and_never_auto_resolves(
    monkeypatch,
):
    """The must-fire synthetic-human integration test P5-50 item 4 (as
    amended by P5-65) requires by name: hand-constructs a league with one
    team's `manager_user_id` set, runs the real `run_trade_window`/`run_
    phase_hook` (only `propose_packages`/`evaluate_trade`/`_check_
    legality` are monkeypatched to force a deterministic, guaranteed-
    legal candidate -- the trade-VALUE formula's own correctness is
    `tests/unit/managers/decisions/test_trade.py`'s job and `test_gm_
    service_pure.py`'s own module docstring states this file's isolated-
    selection-logic convention explicitly; what THIS test proves is the
    real Postgres persistence half nothing at the pure-T3 layer can),
    and asserts:

    - the human team is never a decision-record-bearing actor on the
      PROPOSER side (`result.trade_window.offer_records` carries only the
      AI proposer's own record -- round-7 critic fix: NOT `.records`,
      which stays empty here because this window negotiates nothing);
    - an AI team's proposal TO the human team produces a real, persisted
      `trade_offers` row (`proposed_by IS NULL`, `status = 'proposed'`)
      instead of an auto-resolved trade (`result.trade_window.executions
      == ()`, and the human team's real DB roster is unmoved);
    - zero `trade_offers` rows name the human team as `from_team_id` (the
      AI never proposes AS the human -- P5-55's `a_id` half); and
    - the human team's own participation as a target does not crash or
      silently no-op anywhere in the pipeline (this whole test running to
      completion against real Postgres IS that assertion)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_ai = await _make_team(
                pool, league_id, "H1", gm_philosophy=GmPhilosophy.STAR_MAXER,
                timeline=Timeline.WIN_NOW,
            )
            team_human = await _make_team(
                pool, league_id, "H2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
                timeline=Timeline.REBUILD,
            )
            await team_repo.set_manager(pool, team_human, 555_000_555)

            star = await _make_player(pool, league_id, team_human, overall_cached=88, age=30)
            await _make_contract(
                pool, league_id, team_human, star,
                start_season=_SEASON, salaries=(30_000_000, 32_000_000),
            )
            filler_ai = await _make_player(pool, league_id, team_ai, overall_cached=68, age=26)
            await _make_contract(
                pool, league_id, team_ai, filler_ai, start_season=_SEASON, salaries=(3_000_000,)
            )
            filler_human = await _make_player(
                pool, league_id, team_human, overall_cached=64, age=24
            )
            await _make_contract(
                pool, league_id, team_human, filler_human,
                start_season=_SEASON, salaries=(2_000_000,),
            )

            candidate = gm_service.CandidatePackage(
                team_a_id=team_ai,
                team_b_id=team_human,
                incoming_a=gm_service.AssetBundle(),
                outgoing_a=gm_service.AssetBundle(),
            )

            def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
                if (proposer_id, partner_id) == (team_ai, team_human):
                    star_rp = snap.team_by_id(team_human).player_by_id(star)
                    return (
                        dataclasses.replace(
                            candidate,
                            incoming_a=gm_service.AssetBundle(players=(star_rp,)),
                        ),
                    )
                return ()

            def _fake_evaluate_trade(team_snapshot, incoming, outgoing, philosophy, window, **_kw):
                return DecisionRecord(
                    team_id=team_snapshot.id, philosophy=philosophy, window=window,
                    subject=f"team:{team_snapshot.id}", verdict="accept",
                    score=100.0, threshold=0.0, terms=(),
                )

            def _always_legal(*_args, **_kwargs):
                return None

            monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
            monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _fake_evaluate_trade)
            monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.REGULAR_SEASON, actor_user_id=1
            )

            # No auto-resolved trade -- the human's roster is untouched.
            assert result.trade_window.executions == ()
            star_row = await player_repo.get_player(pool, star)
            assert star_row.team_id == team_human

            # Round-7 critic fix (blocking issue 1) -- a diverted
            # candidate's own record lives in `offer_records`, never
            # `records` (that field's `(record_a, record_b)`-pair-per-
            # negotiation-turn contract can't be satisfied by one, and an
            # earlier version of this assertion is the exact shape the
            # critic proved corrupts `season.py::_count_mutual_accepts`).
            assert result.trade_window.records == ()
            assert result.trade_window.packages_evaluated == 0
            assert result.trade_window.offers_evaluated == 1

            # No DecisionRecord anywhere carries the human team's id.
            assert len(result.trade_window.offer_records) == 1
            assert result.trade_window.offer_records[0].team_id == team_ai
            assert all(r.team_id != team_human for r in result.trade_window.offer_records)

            # The offer is a real, persisted, restart-safe row.
            assert len(result.trade_window.offers) == 1
            offer = result.trade_window.offers[0]
            assert offer.from_team_id == team_ai
            assert offer.to_team_id == team_human

            persisted = await offer_repo.list_trade_offers_for_league(pool, league_id)
            assert len(persisted) == 1
            row = persisted[0]
            assert row.league_id == league_id
            assert row.season == _SEASON
            assert row.phase is Phase.REGULAR_SEASON
            assert row.from_team_id == team_ai
            assert row.to_team_id == team_human
            assert row.proposed_by is None
            assert row.status is TradeStatus.PROPOSED
            assert row.package["players_to_from"] == (star,)

            # The AI never proposes AS the human (P5-55's a_id half).
            assert all(r.from_team_id != team_human for r in persisted)
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_regular_season_hook_ai_offer_to_human_team_persists_to_decision_log_artifact(
    monkeypatch,
):
    """Re-verification critic finding (post-`c15cff5`): the line at
    `gm_service.py:6528` -- `trade_window_result.records + trade_window_
    result.offer_records` -- is the entire mechanism behind Decision P5-56
    ("the proposer's own record IS logged") and design doc §37.5 item 3
    ("`offer_records` is not a dead end -- `run_phase_hook` persists it to
    the decision-log artifact alongside `records`"), and had zero test
    coverage: removing `+ trade_window_result.offer_records` from that line
    left the full suite at 149 passed, 0 failed. The sibling test above
    (`..._persists_and_never_auto_resolves`) only ever inspects in-memory
    `result.trade_window.offer_records` and Postgres rows -- it `unlink()`s
    the artifact file both mid-test and in `finally` and never reads its
    bytes. THIS test reads the real artifact file (same technique as
    `test_run_phase_hook_writes_byte_identical_jsonl_across_two_runs`
    above: `gm_service.artifact_path(...).read_bytes()`, one JSON object
    per `\\n`-terminated line, decoded with `json.loads`) and asserts the
    diverted AI proposer's own `DecisionRecord` genuinely landed in it --
    the actual durable artifact the design doc names, not a stand-in for
    it."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_ai = await _make_team(
                pool, league_id, "J1", gm_philosophy=GmPhilosophy.STAR_MAXER,
                timeline=Timeline.WIN_NOW,
            )
            team_human = await _make_team(
                pool, league_id, "J2", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
                timeline=Timeline.REBUILD,
            )
            await team_repo.set_manager(pool, team_human, 555_000_888)

            star = await _make_player(pool, league_id, team_human, overall_cached=88, age=30)
            await _make_contract(
                pool, league_id, team_human, star,
                start_season=_SEASON, salaries=(30_000_000, 32_000_000),
            )
            filler_ai = await _make_player(pool, league_id, team_ai, overall_cached=68, age=26)
            await _make_contract(
                pool, league_id, team_ai, filler_ai, start_season=_SEASON, salaries=(3_000_000,)
            )
            filler_human = await _make_player(
                pool, league_id, team_human, overall_cached=64, age=24
            )
            await _make_contract(
                pool, league_id, team_human, filler_human,
                start_season=_SEASON, salaries=(2_000_000,),
            )

            candidate = gm_service.CandidatePackage(
                team_a_id=team_ai,
                team_b_id=team_human,
                incoming_a=gm_service.AssetBundle(),
                outgoing_a=gm_service.AssetBundle(),
            )

            def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
                if (proposer_id, partner_id) == (team_ai, team_human):
                    star_rp = snap.team_by_id(team_human).player_by_id(star)
                    return (
                        dataclasses.replace(
                            candidate,
                            incoming_a=gm_service.AssetBundle(players=(star_rp,)),
                        ),
                    )
                return ()

            def _fake_evaluate_trade(team_snapshot, incoming, outgoing, philosophy, window, **_kw):
                return DecisionRecord(
                    team_id=team_snapshot.id, philosophy=philosophy, window=window,
                    subject=f"team:{team_snapshot.id}", verdict="accept",
                    score=100.0, threshold=0.0, terms=(),
                )

            def _always_legal(*_args, **_kwargs):
                return None

            monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
            monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _fake_evaluate_trade)
            monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

            path = gm_service.artifact_path(league_id, _SEASON)
            path.unlink(missing_ok=True)

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.REGULAR_SEASON, actor_user_id=1
            )

            # Sanity: the in-memory shape this whole mechanism depends on.
            assert result.trade_window.records == ()
            assert len(result.trade_window.offer_records) == 1
            assert result.trade_window.offer_records[0].team_id == team_ai

            # The actual assertion: the diverted proposer's DecisionRecord
            # genuinely reached the durable decision-log artifact -- read
            # real bytes off disk, not the in-memory result above.
            assert path.exists(), "run_phase_hook must have written the artifact file"
            lines = path.read_bytes().decode("utf-8").splitlines()
            decoded = [json.loads(line) for line in lines if line]
            matching = [
                obj for obj in decoded
                if obj["team_id"] == team_ai and obj["subject"] == f"team:{team_ai}"
            ]
            assert len(matching) == 1, (
                f"expected exactly one artifact line for the diverted proposer's own "
                f"record, found {len(matching)} in {decoded!r}"
            )
            record = matching[0]
            assert record["verdict"] == "accept"
            assert record["score"] == 100.0
            assert record["league_id"] == league_id
            assert record["season"] == _SEASON
            assert record["phase"] == Phase.REGULAR_SEASON.value

            # No artifact line anywhere names the human team as team_id --
            # mirrors the in-memory-only assertion the sibling test already
            # makes, now proved against the real persisted bytes too.
            assert all(obj["team_id"] != team_human for obj in decoded)
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_regular_season_hook_never_proposes_as_a_human_managed_team(monkeypatch):
    """P5-55's proposer-side half, proved against the real hook: with the
    PROPOSER human-managed instead of the partner, `propose_packages` must
    never be called for that ordered pair at all, so no offer, no
    execution and no `DecisionRecord` are ever produced for it.

    **Round-7 critic fix.** This test's own docstring claim -- `propose_
    packages` "must never be called for that ordered pair at all" -- used
    to be unproven: the fixture teams below have no players/contracts, so
    `propose_packages` returns `()` regardless of whether the P5-45/P5-55
    skip exists at all (an independent critic re-verification proved this
    by mutating the real skip to `if False and ...` and confirming this
    test still passed). `propose_packages` is now spied on directly, the
    same technique `tests/unit/services/test_gm_service_pure.py::test_run_
    trade_window_never_proposes_as_a_human_managed_team` already uses at
    the pure layer, adapted to this integration fixture -- so THIS test
    now genuinely must-fires under that same mutation."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.REGULAR_SEASON)
        try:
            team_human = await _make_team(pool, league_id, "H3", timeline=Timeline.WIN_NOW)
            team_ai = await _make_team(pool, league_id, "H4", timeline=Timeline.REBUILD)
            await team_repo.set_manager(pool, team_human, 555_000_777)

            calls: list[tuple[int, int]] = []
            real_propose_packages = gm_service.propose_packages

            def _spy(proposer_id, partner_id, snap, ctx_, cache):
                calls.append((proposer_id, partner_id))
                return real_propose_packages(proposer_id, partner_id, snap, ctx_, cache)

            monkeypatch.setattr(gm_service, "propose_packages", _spy)

            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)

            result = await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.REGULAR_SEASON, actor_user_id=1
            )

            assert (team_human, team_ai) not in calls
            assert all(proposer_id != team_human for proposer_id, _partner_id in calls)

            assert result.trade_window.executions == ()
            assert result.trade_window.offers == ()
            assert result.trade_window.records == ()

            persisted = await offer_repo.list_trade_offers_for_league(pool, league_id)
            assert persisted == []
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Decision P5-67/P5-68 (docs/design/phase-5-discord-presentation.md §29,
# Stage D' D'-23) -- the `/timeline` override's own classifier-side half.
# "That test is the whole feature" (D'-23's own text): a pinned team's
# stored value must survive a REAL classification pass unchanged while an
# unpinned team's moves, proven end to end through `run_phase_hook` against
# real Postgres, not just at `classify_all_windows`'s own pure layer (which
# `tests/unit/services/test_gm_service_pure.py::
# test_classify_all_windows_carries_a_pinned_team_through_unchanged`
# already covers).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_phase_hook_preseason_classification_pins_a_team_across_two_passes(
    monkeypatch,
):
    """`classify_all_windows` is monkeypatched (the same established
    technique `test_trade_deadline_hook_threads_freshly_classified_contend_
    into_trade_branch` already uses in this file to force a classification
    result that is hard to reach naturally through the real roster-strength
    formula) so the AUTO team's window is forced to a DIFFERENT value on
    each of the two passes -- proving the pinned team's persisted value
    stays fixed for a reason that is NOT "the classifier happened to agree
    with itself twice." The fake still calls the REAL `classify_all_
    windows`, and asserts it was invoked with `pinned_team_ids` containing
    exactly the pinned team -- proving `ctx.manual_timeline_team_ids`
    (derived in `load_gm_inputs` from `teams.timeline_source`) really does
    reach the call, not just that some `pinned_team_ids` was passed.

    `apply_windows` and `team_repo.set_timeline` run FOR REAL (not
    monkeypatched) on every pass -- this is what proves `timeline_source`
    survives the classifier's own unconditional round-trip write (P5-67's
    "provable no-op" argument, §29.2 item 3), not merely that this test's
    own Python-level pinning logic works.
    """
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.PRESEASON)
        try:
            pinned_team = await _make_team(pool, league_id, "PN1", timeline=Timeline.WIN_NOW)
            auto_team = await _make_team(pool, league_id, "PN2", timeline=Timeline.RETOOL)
            await team_repo.set_team_timeline(
                pool, pinned_team, timeline=Timeline.WIN_NOW, source=TimelineSource.MANUAL
            )

            real_classify = gm_service.classify_all_windows
            captured_pinned_ids: list[frozenset[int]] = []
            forced_auto_windows = iter([CompetitiveWindow.REBUILD, CompetitiveWindow.CONTEND])

            def _fake_classify(snapshot, *, pinned_team_ids=frozenset(), **kwargs):
                captured_pinned_ids.append(pinned_team_ids)
                result = real_classify(snapshot, pinned_team_ids=pinned_team_ids, **kwargs)
                forced = dict(result.window_by_team)
                forced[auto_team] = next(forced_auto_windows)
                return dataclasses.replace(result, window_by_team=forced)

            monkeypatch.setattr(gm_service, "classify_all_windows", _fake_classify)

            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.PRESEASON, actor_user_id=1
            )
            after_pass_1 = await team_repo.get_team(pool, pinned_team)
            auto_after_pass_1 = await team_repo.get_team(pool, auto_team)
            assert after_pass_1.timeline == Timeline.WIN_NOW
            assert after_pass_1.timeline_source == TimelineSource.MANUAL
            assert auto_after_pass_1.timeline == Timeline.REBUILD

            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.PRESEASON, actor_user_id=1
            )
            after_pass_2 = await team_repo.get_team(pool, pinned_team)
            auto_after_pass_2 = await team_repo.get_team(pool, auto_team)
            assert after_pass_2.timeline == Timeline.WIN_NOW
            assert after_pass_2.timeline_source == TimelineSource.MANUAL
            assert auto_after_pass_2.timeline == Timeline.WIN_NOW  # to_timeline(CONTEND)

            assert captured_pinned_ids == [frozenset({pinned_team})] * 2
        finally:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
