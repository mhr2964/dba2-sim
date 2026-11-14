"""Live I/O proof for `services/gm_command_service.py` -- Stage D''s human
roster-decision commands (docs/design/phase-5-discord-presentation.md §21
JC-4, §23 D'-17, §27, §29 Decision P5-66/P5-67/P5-68).

Every function under test is the service-layer half of Decision P5-38's
dual enforcement -- a non-manager, non-commissioner actor must be rejected
by `permissions.PermissionDenied` BEFORE any write, and a wrong-phase call
must be rejected by `PhaseError`, proven here against real Postgres rather
than trusted from the bot-layer's own (unit-tested-elsewhere) checks.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from dba2.core.errors import NotFoundError, PhaseError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    contract_repo,
    league_repo,
    offer_repo,
    player_repo,
    role_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import Position, RosterStatus
from dba2.domain.role import DefenseMode, ShotDiet, UsageMode
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Timeline,
    TimelineSource,
)
from dba2.domain.transaction import TransactionType
from dba2.phase.permissions import PermissionDenied
from dba2.services import cap_service, gm_command_service

_COMMISSIONER = 1
_MANAGER = 42
_STRANGER = 999_999


async def _make_league(
    pool, *, current_phase: Phase = Phase.REGULAR_SEASON, current_season: int = 2026
) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="GM Command Service Test League",
        start_season_year=2026,
        current_season=current_season,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
        current_phase=current_phase,
    )
    for season in (2026, 2027, 2028):
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
    pool, league_id: int, code: str, *, manager_user_id: int | None = _MANAGER
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
        manager_user_id=manager_user_id,
    )
    return team.id


async def _make_player(
    pool, league_id: int, team_id: int | None, *, roster_status: RosterStatus | None = None
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
        birth_date=date(1997, 6, 12),
        roster_status=status,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _make_active_contract(
    pool, league_id: int, team_id: int, player_id: int, *, guaranteed: bool = True
) -> int:
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
    for season, salary in ((2026, 3_000_000), (2027, 3_200_000), (2028, 3_400_000)):
        await contract_repo.create_contract_year(
            pool,
            contract_id=contract.id,
            season=season,
            salary=salary,
            guaranteed_amount=salary if guaranteed else 0,
        )
    return contract.id


async def _make_lapsed_contract(pool, league_id: int, team_id: int, player_id: int) -> int:
    """A contract whose ONLY covered season is 2026 -- used to reproduce
    the round-3 blocking issue against a league at `current_season=2027`:
    `_occupies_next_season`'s own docstring says such a player "must still
    count" toward the roster (nothing has rolled them over), so the gate
    must count them even though their contract has already lapsed as of
    the current season."""
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
        pool, contract_id=contract.id, season=2026, salary=3_000_000, guaranteed_amount=3_000_000
    )
    return contract.id


# ---------------------------------------------------------------------------
# waive / release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waive_player_by_manager_creates_dead_cap_and_frees_the_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV1")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            charges = await gm_command_service.waive_player(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                actor_user_id=_MANAGER,
            )
            assert len(charges) > 0

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
            assert player.team_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_player_by_stranger_is_denied_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV2")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            with pytest.raises(PermissionDenied):
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_STRANGER,
                )

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
            assert player.team_id == team_id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_player_by_commissioner_on_any_team_succeeds():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV3")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            await gm_command_service.waive_player(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                actor_user_id=_COMMISSIONER,
            )
            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_player_is_illegal_during_draft():
    """Decision P5-40's own builder gap-fill (disclosed, not a doc ruling):
    `ROSTER_TRANSACTION_PHASES` closes waive/release/sign only during
    `DRAFT` -- see `command_gates.py`'s own comment for the reasoning."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_id = await _make_team(pool, league_id, "WV4")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            with pytest.raises(PhaseError):
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_release_player_by_manager_frees_the_player_with_no_dead_cap():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RL1")
            player_id = await _make_player(pool, league_id, team_id)
            # release() rejects any remaining guaranteed money
            # (roster_service.release's own guard) -- a non-guaranteed
            # contract exercises the real, legal release path.
            await _make_active_contract(pool, league_id, team_id, player_id, guaranteed=False)

            released = await gm_command_service.release_player(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=player_id,
                actor_user_id=_MANAGER,
            )
            assert released.status == ContractStatus.VOIDED

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_release_player_is_illegal_during_draft():
    """`release`'s own twin of `test_waive_player_is_illegal_during_draft`
    -- the doc-accuracy nit disclosed alongside the four blocking issues
    (docs/design/phase-5-discord-presentation.md §43 claimed this was
    already covered; only `waive` actually had a test)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_id = await _make_team(pool, league_id, "RL9")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id, guaranteed=False)

            with pytest.raises(PhaseError):
                await gm_command_service.release_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_a_contract_belonging_to_a_different_team_is_rejected():
    """`_active_contract_for_team`'s own real check -- a team code cannot
    be used to waive a PLAYER ON A DIFFERENT TEAM just because the caller
    happens to be that other team's manager or the commissioner supplied
    the wrong `code`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "OW1", manager_user_id=_MANAGER)
            team_b = await _make_team(pool, league_id, "OW2", manager_user_id=77)
            player_id = await _make_player(pool, league_id, team_b)
            await _make_active_contract(pool, league_id, team_b, player_id)

            with pytest.raises(ValidationError):
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_a,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# sign_free_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_free_agent_by_manager_creates_active_contract():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG1")
            player_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

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

            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
            assert player.team_id == team_id

            contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            active = [c for c in contracts if c.status == ContractStatus.ACTIVE]
            assert len(active) == 1
            assert active[0].team_id == team_id
            assert active[0].total_years == 2
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_rejects_a_player_who_is_not_a_free_agent():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG2")
            other_team = await _make_team(pool, league_id, "SG3", manager_user_id=77)
            player_id = await _make_player(pool, league_id, other_team)

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=2_000_000,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_is_illegal_during_draft():
    """`sign_free_agent`'s own twin of `test_waive_player_is_illegal_
    during_draft` -- the doc-accuracy nit disclosed alongside the four
    blocking issues (§43 claimed this was already covered; only `waive`
    actually had a test)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_phase=Phase.DRAFT)
        try:
            team_id = await _make_team(pool, league_id, "SG9")
            player_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(PhaseError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=2_000_000,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_rejects_a_signing_that_breaks_roster_max_and_apron_2():
    """Blocking issue 4 (critic, round 1) -- reproduced with the critic's
    own exact shape: a 15/15 roster (roster_max=15, `_make_league`'s cap
    settings) offered a 16th player at $900M/yr. Before the fix this was
    accepted outright (roster ends at 16/15, cap room deeply negative);
    after the fix it is rejected before any write, and roster/cap state
    are unchanged."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG4")
            for _ in range(15):
                pid = await _make_player(pool, league_id, team_id)
                await _make_active_contract(pool, league_id, team_id, pid)

            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=fa_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=900_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )

            roster = await player_repo.list_players_for_team(pool, team_id)
            assert len(roster) == 15
            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.FREE_AGENT
            assert fa_player.team_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_rejects_a_signing_that_alone_exceeds_apron_2():
    """A near-empty roster (well under `roster_max`) is still rejected on
    the cap rule alone -- proves the fix's two gates (roster_max, apron_2
    headroom) are independent, not "roster-full" catching every case."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG5")
            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=fa_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    # apron_1=190M, apron_2=200M, empty roster -> headroom
                    # is exactly 200M; this is chosen to exceed it outright.
                    annual_salary=250_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_uses_the_same_apron1_room_basis_the_ai_path_does():
    """Round-2 blocking issue: round 1's gate computed `apron1_room` as
    `cap_settings.apron_1 - cap_summary.used` -- a hand-rolled re-derivation
    that silently drops `minimum_filler = max(0, roster_min - roster_count)
    * min_salary` (`capsheet.compute_team_season_cap_sheet`, item 7), the
    exact term `SeasonCapProjection.apron1_room` (`apron2_headroom`'s own
    documented required input, and the AI path's real basis) always
    includes. A single physically-rostered, CONTRACT-LESS player (so
    `capsheet`'s own contract-year-covers-season `roster_count` basis reads
    0, `_make_league`'s `roster_min=14`/`min_salary=$1.2M`) reproduces the
    critic's own numbers exactly: `minimum_filler = 14 * $1,200,000 =
    $16,800,000`; old headroom = `$190,000,000 - $0 + $10,000,000 =
    $200,000,000`; new (correct) headroom = `$200,000,000 - $16,800,000 =
    $183,200,000`.

    Before this fix, `$184,200,000` (inside the old, wrong $200M headroom
    but outside the real $183.2M one) was WRONGLY accepted. After the fix
    it is rejected, and the same salary reduced by exactly the
    `minimum_filler` gap (`$183,200,000`, right at the real boundary) is
    accepted -- proving this isn't just "somewhat stricter" but the exact
    AI-consistent number."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG6")
            # A physically-rostered player with NO contract -- occupies a
            # roster slot (counts toward the roster-max gate) but con
            # tributes nothing to `capsheet`'s contract-year-covers-season
            # `roster_count`/`committed_salary` bases, isolating the
            # `minimum_filler` gap from any other cap-sheet term.
            await _make_player(pool, league_id, team_id)
            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=fa_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=184_200_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )

            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.FREE_AGENT

            # Exactly at the real (AI-consistent) headroom boundary --
            # legal, proving the fix isn't merely stricter but precisely
            # matches `apron2_headroom(cap_settings, projection.apron1_room)`.
            await gm_command_service.sign_free_agent(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=fa_id,
                actor_user_id=_MANAGER,
                years=1,
                annual_salary=183_200_000,
                signing_method=SigningMethod.CAP_SPACE,
            )
            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.ACTIVE
            assert fa_player.team_id == team_id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_roster_max_gate_ignores_retired_players_still_holding_team_id():
    """Round-2 nit: the roster-max gate must use the SAME physical-
    occupancy basis `run_free_agency`'s own gate uses (`gm_service.
    _next_season_roster_count`/`_occupies_next_season`, which starts from
    `roster_status == ACTIVE` per `load_gm_inputs`'s own documented
    interpretive call), not an unfiltered `player_repo.list_players_for_
    team` count. 14 real ACTIVE players plus one RETIRED player who still
    holds `team_id` (a body count of 15, matching `roster_max=15`) must
    NOT block a legal signing -- the AI path would never count the retired
    player at all. Before this fix, the unfiltered count (15) blocked this
    signing outright; after it, only the 14 real ACTIVE bodies count."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG7")
            for _ in range(14):
                pid = await _make_player(pool, league_id, team_id)
                await _make_active_contract(pool, league_id, team_id, pid)
            retired_pid = await _make_player(
                pool, league_id, team_id, roster_status=RosterStatus.RETIRED
            )
            assert retired_pid is not None

            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            await gm_command_service.sign_free_agent(
                pool,
                league_id=league_id,
                team_id=team_id,
                player_id=fa_id,
                actor_user_id=_MANAGER,
                years=1,
                annual_salary=2_000_000,
                signing_method=SigningMethod.CAP_SPACE,
            )
            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.ACTIVE
            assert fa_player.team_id == team_id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_roster_max_gate_counts_players_in_their_contracts_final_year():
    """Round-4 critic's exact repro of round 3's regression -- the bug
    round 3's own fix (`next_season = season + 1`, matched to `gm_service.
    _next_season_roster_count`'s AI-oriented convention) introduced.
    `_occupies_next_season` excludes anyone whose contract's last covered
    season equals `next_season - 1`; once `next_season = season + 1`, that
    boundary collapsed onto `season` itself -- every player in the FINAL
    year of their contract (a common, live, in-season state, unlike round
    2's already-fully-lapsed case) went invisible to the gate.

    Fixture (the critic's own "mixed" shape, not an isolated edge case):
    5 of 15 ACTIVE roster players are in their contract's final year
    (`_make_lapsed_contract`'s single 2026-only year, paired with this
    test's default `current_season=2026` -- their contract's ONLY/LAST
    season IS the current season, not already lapsed before it, unlike
    the round-3-test's `current_season=2027` pairing of the same helper);
    the other 10 are mid-contract (`_make_active_contract`, running
    through 2028). Before this round's fix, the AI-basis-borrowing gate
    read only the 10 mid-contract players and let a 16th, 17th, ... signing
    through indefinitely (reproduced live pre-fix: 30 consecutive
    signings all succeeded on a `roster_max=15` team, ending at 45 ACTIVE
    bodies -- unbounded overfill, not an edge case). After this round's
    fix (direct `RosterStatus.ACTIVE` occupancy count, no contract-year
    reasoning at all), all 15 real bodies count regardless of where each
    one's contract happens to end, and the 16th signing is rejected."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)  # current_season=2026 (default)
        try:
            team_id = await _make_team(pool, league_id, "SG10")
            for _ in range(5):
                pid = await _make_player(pool, league_id, team_id)
                # Contract's ONLY covered season is 2026 == league's
                # current_season -- the final-year boundary round 3 broke.
                await _make_lapsed_contract(pool, league_id, team_id, pid)
            for _ in range(10):
                pid = await _make_player(pool, league_id, team_id)
                await _make_active_contract(pool, league_id, team_id, pid)

            roster = await player_repo.list_players_for_team(pool, team_id)
            active = [p for p in roster if p.roster_status == RosterStatus.ACTIVE]
            assert len(active) == 15  # sanity: fixture really is 15/15

            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=fa_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )

            roster = await player_repo.list_players_for_team(pool, team_id)
            active = [p for p in roster if p.roster_status == RosterStatus.ACTIVE]
            assert len(active) == 15  # unchanged -- signing never wrote
            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.FREE_AGENT
            assert fa_player.team_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_roster_max_gate_counts_players_on_lapsed_contracts():
    """Round-3 blocking issue (critic): the roster-max gate passed the
    league's CURRENT season into `_next_season_roster_count`, a NEXT-
    season-counting function -- `_occupies_next_season`'s own docstring
    says a player whose contract's last covered season is `next_season -
    1` (lapsed as of the current season) "must still count" toward the
    roster, since nothing has rolled them over. Reproduced with the
    critic's own exact numbers: a league at `current_season=2027`, 15
    ACTIVE players all on contracts whose only covered year is 2026 --
    before the fix the gate (fed `season=2027` directly) computed
    `next_season - 1 == 2026 == season`, so EVERY one of them read as
    lapsed and the gate counted an EMPTY roster; a 16th signing was
    accepted outright, leaving 16 ACTIVE bodies against `roster_max=15`.
    After the fix (`season + 1`, matching the AI path's own `ctx.
    next_season` convention at `gm_service.py::run_free_agency`), the
    same 15 players correctly count as occupying the roster and the 16th
    signing is rejected before any write."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool, current_season=2027)
        try:
            team_id = await _make_team(pool, league_id, "SG8")
            for _ in range(15):
                pid = await _make_player(pool, league_id, team_id)
                await _make_lapsed_contract(pool, league_id, team_id, pid)

            fa_id = await _make_player(
                pool, league_id, None, roster_status=RosterStatus.FREE_AGENT
            )

            with pytest.raises(ValidationError):
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=fa_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                )

            roster = await player_repo.list_players_for_team(pool, team_id)
            active = [p for p in roster if p.roster_status == RosterStatus.ACTIVE]
            assert len(active) == 15
            fa_player = await player_repo.get_player(pool, fa_id)
            assert fa_player.roster_status == RosterStatus.FREE_AGENT
            assert fa_player.team_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_sign_free_agent_calls_do_not_overfill_the_roster():
    """Round-5 critic's blocking issue B1: the roster-max/cap-legality
    read and `apply_signing`'s own write were not atomic -- `roster.py`
    passes the POOL (not a single connection) into this whole call chain,
    so two (or more) concurrent `/sign-free-agent` calls on the SAME team
    could each read "room available" before either one's write landed,
    and both writes would succeed. Reproduced live by the critic exactly
    as built here: 14 ACTIVE players on a `roster_max=15` team, 5
    concurrent `sign_free_agent` calls (5 different free agents) fired via
    `asyncio.gather` on the shared pool. Independently re-confirmed here:
    temporarily reverted the `transaction(ex)`/`lock_team_row` (renamed
    from `get_team_for_update`) locking in `sign_free_agent` back to the
    old unlocked shape (the
    legality check and `apply_signing` called separately against the bare
    pool, no row lock) and re-ran this exact test locally -- all 5 calls
    were accepted, final ACTIVE count 19 against `roster_max=15`, a
    genuine pre-fix overfill (not a false pass), before restoring the
    real fix.

    After the fix (one `transaction(ex)` per call, `team_repo.get_team_
    for_update`'s `SELECT ... FOR UPDATE` row lock taken before either
    gate reads anything), the 5 calls' critical sections serialize on that
    lock: each later call blocks until an earlier one's transaction
    commits, then re-reads a roster count that already reflects that
    commit. Exactly 1 of the 5 succeeds; the other 4 correctly reject
    themselves on the now-full roster; the final roster never exceeds
    `roster_max`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SG11")
            for _ in range(14):
                pid = await _make_player(pool, league_id, team_id)
                await _make_active_contract(pool, league_id, team_id, pid)

            fa_ids = [
                await _make_player(pool, league_id, None, roster_status=RosterStatus.FREE_AGENT)
                for _ in range(5)
            ]

            results = await asyncio.gather(
                *(
                    gm_command_service.sign_free_agent(
                        pool,
                        league_id=league_id,
                        team_id=team_id,
                        player_id=fa_id,
                        actor_user_id=_MANAGER,
                        years=1,
                        annual_salary=2_000_000,
                        signing_method=SigningMethod.CAP_SPACE,
                    )
                    for fa_id in fa_ids
                ),
                return_exceptions=True,
            )

            succeeded = [r for r in results if r is None]
            failed = [r for r in results if isinstance(r, Exception)]
            assert len(succeeded) == 1, (
                "expected exactly 1 of 5 concurrent signings onto a 14/15 roster to "
                f"succeed, got {len(succeeded)} (results={results!r})"
            )
            assert len(failed) == 4
            assert all(isinstance(e, ValidationError) for e in failed), failed

            roster = await player_repo.list_players_for_team(pool, team_id)
            active = [p for p in roster if p.roster_status == RosterStatus.ACTIVE]
            assert len(active) == 15  # filled to exactly roster_max, never overfilled
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_sign_free_agent_calls_by_two_different_teams_do_not_both_win():
    """Round-6 critic's blocking issue B1: round 5's `team_repo.get_team_
    for_update` lock serializes two signings racing for the SAME team's
    roster/cap room, but does nothing when two DIFFERENT teams race for
    the SAME free agent -- each call locks a different team row, so
    nothing serializes them. Reproduced live by the critic 3 independent
    ways: two teams, with distinct human managers, concurrently `/sign-
    free-agent` the same free agent -- BOTH succeeded, leaving 2
    `status='active'` contract rows for one player (violating `roster_
    service.sign`'s own one-active-contract-per-player invariant), the
    player landing on whichever write happened to land second, and the
    LOSING team left with a permanent phantom `committed_salary` charge
    feeding into `cap_service.get_cap_summary`.

    Independently re-confirmed here: temporarily reverted `roster_
    service.sign`'s new `player_repo.lock_player_row` (renamed from
    `get_player_for_update`) lock (this round's fix) and re-ran this
    exact test locally -- both concurrent
    signings succeeded, 2 active contracts existed for the one player
    afterward, a genuine pre-fix double-signing (not a false pass),
    before restoring the real fix.

    After the fix (the player row locked inside `roster_service.sign`,
    the "no existing active contract" check re-read after that lock is
    acquired, inside the same transaction that carries the write), the
    two calls' critical sections serialize on the player row: whichever
    call's `apply_signing` reaches `sign()` first wins the lock, creates
    the contract, and commits; the second blocks until then, re-reads
    contract state that already reflects the first's commit, and
    correctly rejects itself with an honest `ValidationError` -- not a
    raw DB error. Exactly 1 of the 2 succeeds; the loser's team shows NO
    phantom `committed_salary` charge afterward."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            manager_a, manager_b = 501, 502
            team_a = await _make_team(pool, league_id, "CT1", manager_user_id=manager_a)
            team_b = await _make_team(pool, league_id, "CT2", manager_user_id=manager_b)
            fa_id = await _make_player(pool, league_id, None, roster_status=RosterStatus.FREE_AGENT)

            results = await asyncio.gather(
                gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_a,
                    player_id=fa_id,
                    actor_user_id=manager_a,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                ),
                gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_b,
                    player_id=fa_id,
                    actor_user_id=manager_b,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                ),
                return_exceptions=True,
            )

            succeeded = [r for r in results if r is None]
            failed = [r for r in results if isinstance(r, Exception)]
            assert len(succeeded) == 1, (
                "expected exactly 1 of 2 concurrent signings of the same free agent "
                f"by different teams to succeed, got {len(succeeded)} (results={results!r})"
            )
            assert len(failed) == 1
            assert isinstance(failed[0], ValidationError), failed

            # Raw-SQL ground truth: exactly one ACTIVE contract row for this player.
            active_contracts = await pool.fetch(
                "SELECT team_id FROM contracts WHERE player_id = $1 AND status = 'active'",
                fa_id,
            )
            assert len(active_contracts) == 1, (
                f"expected exactly 1 active contract row for player {fa_id}, "
                f"got {len(active_contracts)}"
            )
            winning_team_id = active_contracts[0]["team_id"]
            losing_team_id = team_b if winning_team_id == team_a else team_a

            player = await player_repo.get_player(pool, fa_id)
            assert player is not None
            assert player.team_id == winning_team_id
            assert player.roster_status == RosterStatus.ACTIVE

            # The losing team must show NO phantom cap charge from a
            # contract it never actually holds.
            losing_summary = await cap_service.get_cap_summary(
                pool, league_id=league_id, team_id=losing_team_id, season=2026
            )
            assert losing_summary.committed_salary == 0, (
                f"losing team {losing_team_id} shows a phantom committed_salary "
                f"charge of {losing_summary.committed_salary} for a contract it "
                f"doesn't actually hold"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_sign_free_agent_overfill_twin_reproduces_with_locking_disabled(
    monkeypatch,
):
    """RMLP build step 5's required re-verification (design §3.1a,
    "honest correction to revision 1's 'no re-verification needed'
    claim"): the row lock's MODE changed (`FOR UPDATE` -> `FOR NO KEY
    UPDATE`), which is a real change to round 5's shipped fix, even
    though the lock's target/order/call-site are unchanged. This is that
    fix's own lock-disabled mutation twin (§7.1: "a concurrency test
    without its twin is not evidence") -- `team_repo.lock_team_row` is
    monkeypatched to a plain, non-locking read (same shape as `get_team`)
    so the corruption round 5 found must reappear.

    Recorded numbers (this run): with the lock ENABLED (the test directly
    above), exactly 1 of 5 concurrent signings succeeds and the roster
    ends at 15/15. With the lock DISABLED (this twin), more than 1
    succeeds and the roster overfills past `roster_max=15` -- confirming
    the `FOR NO KEY UPDATE` mode change did not silently weaken what the
    test measures."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TW5")
            for _ in range(14):
                pid = await _make_player(pool, league_id, team_id)
                await _make_active_contract(pool, league_id, team_id, pid)

            fa_ids = [
                await _make_player(pool, league_id, None, roster_status=RosterStatus.FREE_AGENT)
                for _ in range(5)
            ]

            async def _unlocked_team_read(ex, team_id):
                return await team_repo.get_team(ex, team_id)

            monkeypatch.setattr(
                gm_command_service.team_repo, "lock_team_row", _unlocked_team_read
            )

            results = await asyncio.gather(
                *(
                    gm_command_service.sign_free_agent(
                        pool,
                        league_id=league_id,
                        team_id=team_id,
                        player_id=fa_id,
                        actor_user_id=_MANAGER,
                        years=1,
                        annual_salary=2_000_000,
                        signing_method=SigningMethod.CAP_SPACE,
                    )
                    for fa_id in fa_ids
                ),
                return_exceptions=True,
            )
            succeeded = [r for r in results if r is None]

            roster = await player_repo.list_players_for_team(pool, team_id)
            active = [p for p in roster if p.roster_status == RosterStatus.ACTIVE]
            # The twin's whole job is to prove the corruption reappears with
            # locking disabled -- assert the FAILURE mode itself, not a
            # passing shape. If this assertion ever stops firing, the
            # lock's justification is weaker than claimed and that must be
            # investigated, not silently deleted (design §7.2 test 10).
            assert len(succeeded) > 1 or len(active) > 15, (
                f"expected the overfill corruption to reappear with locking "
                f"disabled (succeeded={len(succeeded)}, active={len(active)}/15) -- "
                "if this no longer reproduces, FOR NO KEY UPDATE may have "
                "silently gained protection this test doesn't expect; investigate"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_concurrent_sign_free_agent_cross_team_twin_reproduces_with_locking_disabled(
    monkeypatch,
):
    """Round 6's own twin, same re-verification requirement as the round-5
    twin above -- `player_repo.lock_player_row` (the lock `roster_
    service.sign` takes) is monkeypatched to a plain, non-locking read so
    two different teams signing the SAME free agent concurrently can both
    pass the "no active contract" check before either commits.

    Recorded numbers (this run): with the lock ENABLED (the test directly
    above), exactly 1 of 2 succeeds and exactly 1 active contract row
    exists for the player afterward. With the lock DISABLED (this twin),
    both succeed and more than 1 active contract row exists -- reproducing
    round 6's exact double-signing corruption under the new lock mode.

    **RMLP build step 8 update (§7.1's own required twin discipline):**
    migration 017a's `ux_contracts_one_active_per_player` unique index now
    ALSO independently closes this exact race at the DB layer (verified
    live: with the app-level lock disabled but 017a still in place, this
    twin stopped reproducing -- succeeded=1, active_contracts=1, not the
    corruption). §7.1's own text anticipates this exactly: "the DB
    constraints would mask the corruption when the lock is disabled... the
    lock-disabled twins must also drop the relevant index for their
    duration." Done below -- the index is dropped before the race and
    recreated (identical DDL to migration 017a) in a `finally`, so this
    twin isolates the APPLICATION-level lock's own contribution again,
    independent of the (now real, additional) DB backstop."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            manager_a, manager_b = 601, 602
            team_a = await _make_team(pool, league_id, "TW6A", manager_user_id=manager_a)
            team_b = await _make_team(pool, league_id, "TW6B", manager_user_id=manager_b)
            fa_id = await _make_player(pool, league_id, None, roster_status=RosterStatus.FREE_AGENT)

            await pool.execute("DROP INDEX ux_contracts_one_active_per_player")

            async def _unlocked_player_read(ex, player_id):
                # A small forced delay between the read and whatever the
                # caller does next -- widens the race window so this twin
                # reliably reproduces the interleaving instead of depending
                # on asyncio's own scheduling to happen to overlap two
                # real-but-fast Postgres round trips (a plain unlocked read
                # with no delay was observed to sometimes NOT overlap,
                # making the twin flaky rather than a reliable detector).
                row = await player_repo.get_player(ex, player_id)
                await asyncio.sleep(0.05)
                return row

            monkeypatch.setattr(
                gm_command_service.player_repo, "lock_player_row", _unlocked_player_read
            )
            from dba2.services import roster_service as roster_service_module

            monkeypatch.setattr(
                roster_service_module.player_repo, "lock_player_row", _unlocked_player_read
            )

            results = await asyncio.gather(
                gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_a,
                    player_id=fa_id,
                    actor_user_id=manager_a,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                ),
                gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_b,
                    player_id=fa_id,
                    actor_user_id=manager_b,
                    years=1,
                    annual_salary=2_000_000,
                    signing_method=SigningMethod.CAP_SPACE,
                ),
                return_exceptions=True,
            )
            succeeded = [r for r in results if r is None]

            active_contracts = await pool.fetch(
                "SELECT team_id FROM contracts WHERE player_id = $1 AND status = 'active'",
                fa_id,
            )
            assert len(succeeded) > 1 or len(active_contracts) > 1, (
                f"expected round 6's double-signing corruption to reappear with "
                f"locking disabled (succeeded={len(succeeded)}, "
                f"active_contracts={len(active_contracts)}) -- if this no longer "
                "reproduces, investigate rather than delete"
            )
        finally:
            # Delete this test's own (now-duplicated, by design) rows
            # FIRST -- recreating the unique index below would otherwise
            # fail on the very duplicate this twin exists to prove exists.
            # Identical DDL to migration 017a's upgrade(), so the schema is
            # never left in a modified state for any test that runs after
            # this one.
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute(
                "CREATE UNIQUE INDEX ux_contracts_one_active_per_player "
                "ON contracts (player_id) WHERE status = 'active'"
            )
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# set_strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_strategy_by_manager_upserts_the_row():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "ST1")

            strategy = await gm_command_service.set_strategy(
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
            assert strategy.pace_target == 55
            assert strategy.offensive_scheme == OffensiveScheme.PNR_HEAVY
            assert strategy.set_by == _MANAGER

            fetched = await team_repo.get_team_strategy(pool, team_id, 2026)
            assert fetched is not None
            assert fetched.help_aggression == 60
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_strategy_rejects_out_of_range_values():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "ST2")
            with pytest.raises(ValidationError):
                await gm_command_service.set_strategy(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    pace_target=150,
                    offensive_scheme=OffensiveScheme.MOTION,
                    defensive_scheme=DefensiveScheme.DROP,
                    help_aggression=50,
                    three_point_bias=50,
                    crash_oreb_bias=50,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# set_role / set_directive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_role_by_manager_upserts_role_and_touch_share():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RO1")
            player_id = await _make_player(pool, league_id, team_id)

            role = await gm_command_service.set_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                role="primary_option",
                minutes=0.14 * 240.0,
                locked=True,
            )
            assert role.role == "primary_option"
            assert role.locked is True
            assert float(role.touch_share) == pytest.approx(0.14, abs=1e-4)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_role_rejects_a_player_not_on_the_teams_roster():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RO2")
            other_team = await _make_team(pool, league_id, "RO3", manager_user_id=77)
            player_id = await _make_player(pool, league_id, other_team)

            with pytest.raises(ValidationError):
                await gm_command_service.set_role(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    player_id=player_id,
                    role="bench",
                    minutes=0.1 * 240.0,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_directive_by_manager_upserts_partial_fields():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DR1")
            player_id = await _make_player(pool, league_id, team_id)

            directive = await gm_command_service.set_directive(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                shot_diet=ShotDiet.MORE_THREES,
            )
            assert directive.shot_diet == ShotDiet.MORE_THREES
            assert directive.usage_mode is None
            assert directive.set_by == _MANAGER
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_directive_second_call_does_not_clobber_fields_set_by_the_first():
    """Blocking issue 3 (critic, round 1) -- mirrors the critic's own
    3-call repro. Before this fix, every call forwarded all five
    parameters verbatim into an upsert whose `ON CONFLICT` always SETs
    all five columns, so a second call naming only one field silently
    NULLed the four fields the first call set, and a third call naming
    NONE of the five was accepted and wiped all five. `PlayerDirective`
    rows are live sim input (`game_sim_service.py`), so this was real
    gameplay data loss.

    **PD-1/PD-6/PD-7 retype (2026-09-23): three fields now, not five** --
    `role_mode` is gone (column dropped), `clutch_mode` left this
    command's surface (column/enum stay, no hook). Rewritten with real
    closed-enum values in place of the old free-text placeholders
    ("three_heavy"/"high_usage"/"switch_everything" are not members of
    any of the three enums and would now fail the CHECK constraint)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DR9")
            player_id = await _make_player(pool, league_id, team_id)

            # Call 1: sets 2 of 3 fields.
            first = await gm_command_service.set_directive(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                shot_diet=ShotDiet.MORE_THREES,
                usage_mode=UsageMode.INITIATE,
            )
            assert first.shot_diet == ShotDiet.MORE_THREES
            assert first.usage_mode == UsageMode.INITIATE

            # Call 2: sets only 1 field -- must NOT wipe the other 2,
            # including the 2 from call 1 untouched by this call.
            second = await gm_command_service.set_directive(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                defense_mode=DefenseMode.STAY_HOME,
            )
            assert second.shot_diet == ShotDiet.MORE_THREES
            assert second.usage_mode == UsageMode.INITIATE
            assert second.defense_mode == DefenseMode.STAY_HOME

            # Call 3: no fields at all -- rejected, not a silent no-op
            # "success" that wipes everything.
            with pytest.raises(ValidationError):
                await gm_command_service.set_directive(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    player_id=player_id,
                )
            unchanged = await role_repo.get_player_directive(pool, league_id, player_id)
            assert unchanged.shot_diet == ShotDiet.MORE_THREES
            assert unchanged.usage_mode == UsageMode.INITIATE
            assert unchanged.defense_mode == DefenseMode.STAY_HOME
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# set_timeline -- Decision P5-66/P5-68
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_timeline_by_manager_writes_value_source_and_a_transaction_row():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TM1")

            updated = await gm_command_service.set_timeline(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                timeline=Timeline.WIN_NOW,
            )
            assert updated.timeline == Timeline.WIN_NOW
            assert updated.timeline_source == TimelineSource.MANUAL

            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            timeline_txns = [t for t in transactions if t.txn_type == TransactionType.TIMELINE_SET]
            assert len(timeline_txns) == 1
            assert timeline_txns[0].actor_user_id == _MANAGER
            assert timeline_txns[0].details["to"] == "win_now"
            assert timeline_txns[0].details["source"] == "manual"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_timeline_rolls_back_the_team_write_when_the_transaction_log_write_fails():
    """Blocking issue 2 (critic, round 1): before this fix, `team_repo.
    set_team_timeline` and `txn_repo.create_league_transaction` were two
    separate, unwrapped statements against the pool -- a failure in the
    second left the team's `timeline`/`timeline_source` committed with
    zero `league_transactions` rows, a timeline change with no audit
    record. Reproduced here exactly as the critic did: patch `txn_repo.
    create_league_transaction` (the real module `gm_command_service`
    imports and calls) to raise, call `set_timeline`, and confirm NEITHER
    write commits -- a clean rollback, not a partial state."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TM9")
            before = await team_repo.get_team(pool, team_id)

            with patch.object(
                gm_command_service.txn_repo,
                "create_league_transaction",
                side_effect=RuntimeError("txn log write exploded"),
            ):
                with pytest.raises(RuntimeError):
                    await gm_command_service.set_timeline(
                        pool,
                        league_id=league_id,
                        team_id=team_id,
                        actor_user_id=_MANAGER,
                        timeline=Timeline.WIN_NOW,
                    )

            after = await team_repo.get_team(pool, team_id)
            assert after.timeline == before.timeline
            assert after.timeline_source == before.timeline_source

            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            timeline_txns = [t for t in transactions if t.txn_type == TransactionType.TIMELINE_SET]
            assert len(timeline_txns) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_timeline_auto_reverts_source_without_resetting_the_value():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TM2")
            await gm_command_service.set_timeline(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                timeline=Timeline.REBUILD,
            )

            reverted = await gm_command_service.set_timeline(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                timeline=None,
            )
            assert reverted.timeline == Timeline.REBUILD
            assert reverted.timeline_source == TimelineSource.AUTO

            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            timeline_txns = [t for t in transactions if t.txn_type == TransactionType.TIMELINE_SET]
            assert len(timeline_txns) == 2
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_timeline_by_stranger_is_denied():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TM3")
            with pytest.raises(PermissionDenied):
                await gm_command_service.set_timeline(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_STRANGER,
                    timeline=Timeline.WIN_NOW,
                )
            team = await team_repo.get_team(pool, team_id)
            assert team.timeline_source == TimelineSource.AUTO
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_timeline_by_commissioner_on_any_team_succeeds():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TM4")
            updated = await gm_command_service.set_timeline(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_COMMISSIONER,
                timeline=Timeline.REBUILD,
            )
            assert updated.timeline_source == TimelineSource.MANUAL
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_unknown_team_raises_not_found():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            with pytest.raises(NotFoundError):
                await gm_command_service.set_timeline(
                    pool,
                    league_id=league_id,
                    team_id=999_999_999,
                    actor_user_id=_COMMISSIONER,
                    timeline=Timeline.WIN_NOW,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# /gm auto (GM-8, docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_auto_pilot_by_manager_enables_with_a_philosophy_and_logs_a_transaction():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP1")

            updated = await gm_command_service.set_auto_pilot(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                enabled=True,
                philosophy=GmPhilosophy.CHAOS,
            )
            assert updated.auto_pilot is True
            assert updated.gm_philosophy == GmPhilosophy.CHAOS

            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            auto_txns = [t for t in transactions if t.txn_type == TransactionType.AUTO_PILOT_SET]
            assert len(auto_txns) == 1
            assert auto_txns[0].actor_user_id == _MANAGER
            assert auto_txns[0].details["enabled"] is True
            assert auto_txns[0].details["gm_philosophy"] == "chaos"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_auto_pilot_enable_with_no_philosophy_is_rejected_before_any_write():
    """GM-8: Discord cannot express "required only if another option has a
    particular value", so the handler-side rejection lands here, before
    any write -- the rejected alternative (defaulting silently to the
    column's existing value) would let a GM enable a delegate whose
    disposition they never saw."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP2")
            with pytest.raises(ValidationError):
                await gm_command_service.set_auto_pilot(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_MANAGER,
                    enabled=True,
                    philosophy=None,
                )
            team = await team_repo.get_team(pool, team_id)
            assert team.auto_pilot is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_auto_pilot_disable_ignores_philosophy_and_keeps_the_last_value():
    """GM-8: turning auto off does not reset gm_philosophy -- the column
    keeps its last value, which is also the philosophy the AI uses if the
    team is ever left unmanaged."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP3")
            await gm_command_service.set_auto_pilot(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                enabled=True,
                philosophy=GmPhilosophy.VET_OVERRATER,
            )

            updated = await gm_command_service.set_auto_pilot(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                enabled=False,
                philosophy=None,
            )
            assert updated.auto_pilot is False
            assert updated.gm_philosophy == GmPhilosophy.VET_OVERRATER
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_auto_pilot_by_a_non_owning_commissioner_is_refused():
    """Mutation 6 (docs/design/gm-command-surface.md): `/gm auto` deliber-
    ately does NOT take the commissioner-or-manager union every other
    `/gm` command gets -- a commissioner who does not manage the team must
    be refused, proving GM-8's narrower authorization (`require_team_
    manager`, not `require_can_act_for_team`) is real."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP4")
            with pytest.raises(PermissionDenied):
                await gm_command_service.set_auto_pilot(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_COMMISSIONER,
                    enabled=True,
                    philosophy=GmPhilosophy.EGALITARIAN,
                )
            team = await team_repo.get_team(pool, team_id)
            assert team.auto_pilot is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_auto_pilot_by_a_stranger_is_refused():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP5")
            with pytest.raises(PermissionDenied):
                await gm_command_service.set_auto_pilot(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    actor_user_id=_STRANGER,
                    enabled=True,
                    philosophy=GmPhilosophy.EGALITARIAN,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_auto_pilot_rolls_back_the_team_write_when_the_transaction_log_write_fails():
    """Mutation 7: the §43.1 blocking-issue-2 repro, applied to `/gm auto`
    -- patch the transaction-log write to raise and confirm the `teams`
    row does not commit either."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AP6")
            before = await team_repo.get_team(pool, team_id)

            with patch.object(
                gm_command_service.txn_repo,
                "create_league_transaction",
                side_effect=RuntimeError("txn log write exploded"),
            ):
                with pytest.raises(RuntimeError):
                    await gm_command_service.set_auto_pilot(
                        pool,
                        league_id=league_id,
                        team_id=team_id,
                        actor_user_id=_MANAGER,
                        enabled=True,
                        philosophy=GmPhilosophy.EGALITARIAN,
                    )

            after = await team_repo.get_team(pool, team_id)
            assert after.auto_pilot == before.auto_pilot
            assert after.gm_philosophy == before.gm_philosophy

            transactions = await txn_repo.list_transactions_for_league(pool, league_id)
            auto_txns = [t for t in transactions if t.txn_type == TransactionType.AUTO_PILOT_SET]
            assert len(auto_txns) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# /gm status (GM-9, docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GM-3b: the auto-pilot refusal extension (docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


async def _make_auto_piloted_team(pool, league_id: int, code: str) -> int:
    team_id = await _make_team(pool, league_id, code)
    await team_repo.set_auto_pilot(
        pool, team_id, auto_pilot=True, gm_philosophy=GmPhilosophy.EGALITARIAN
    )
    return team_id


@pytest.mark.asyncio
async def test_waive_by_an_auto_piloted_teams_own_manager_is_refused():
    """Mutation 3b: an auto-piloted team's manager calling /waive must be
    refused with GM-3b.2(c)'s manager sentence."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_auto_piloted_team(pool, league_id, "AR1")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            with pytest.raises(PermissionDenied) as excinfo:
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
            assert str(excinfo.value) == (
                "AR1 is on auto-pilot -- the AI is handling its roster moves. "
                "Run `/gm auto enabled:off` to waive yourself."
            )
            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_by_a_non_owning_commissioner_for_an_auto_piloted_team_is_refused():
    """Mutation 3b's fourth assertion: a non-owning commissioner calling
    /waive for an auto-piloted team gets GM-3b.2(c)'s COMMISSIONER
    sentence, not the manager one -- a single unconditional message would
    make this go red while the manager-refusal test above stays green."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_auto_piloted_team(pool, league_id, "AR2")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            with pytest.raises(PermissionDenied) as excinfo:
                await gm_command_service.waive_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_COMMISSIONER,
                )
            assert str(excinfo.value) == (
                "AR2 is on auto-pilot -- the AI is handling its roster moves. "
                "Only AR2's own GM can turn that off (`/gm auto`), or use "
                "`/remove-manager` to hand the team to the AI outright."
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_release_by_an_auto_piloted_teams_own_manager_is_refused():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_auto_piloted_team(pool, league_id, "AR3")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            with pytest.raises(PermissionDenied) as excinfo:
                await gm_command_service.release_player(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                )
            assert "on auto-pilot" in str(excinfo.value)
            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.ACTIVE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sign_free_agent_by_an_auto_piloted_teams_own_manager_is_refused():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_auto_piloted_team(pool, league_id, "AR4")
            player_id = await _make_player(pool, league_id, None)

            with pytest.raises(PermissionDenied) as excinfo:
                await gm_command_service.sign_free_agent(
                    pool,
                    league_id=league_id,
                    team_id=team_id,
                    player_id=player_id,
                    actor_user_id=_MANAGER,
                    years=1,
                    annual_salary=1_500_000,
                )
            assert "on auto-pilot" in str(excinfo.value)
            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_gm_setters_still_work_for_an_auto_piloted_teams_own_manager():
    """Mutation 3c's behavioural half: GM-7 gives all four /gm setters
    ALL_PHASES precisely because a standing setting is not a transaction
    -- delegating your own decisions must not lock you out of configuring
    the delegate. All four must still succeed for the SAME auto-piloted
    team the tests above refuse."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_auto_piloted_team(pool, league_id, "AR5")
            player_id = await _make_player(pool, league_id, team_id)

            strategy = await gm_command_service.set_strategy(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                pace_target=60,
                offensive_scheme=OffensiveScheme.MOTION,
                defensive_scheme=DefensiveScheme.DROP,
                help_aggression=60,
                three_point_bias=60,
                crash_oreb_bias=60,
            )
            assert strategy.pace_target == 60

            role = await gm_command_service.set_role(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                role="primary_option",
                minutes=0.15 * 240.0,
            )
            assert role.role == "primary_option"

            directive = await gm_command_service.set_directive(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                player_id=player_id,
                shot_diet=ShotDiet.MORE_THREES,
            )
            assert directive.shot_diet == ShotDiet.MORE_THREES

            updated_team = await gm_command_service.set_timeline(
                pool,
                league_id=league_id,
                team_id=team_id,
                actor_user_id=_MANAGER,
                timeline=Timeline.WIN_NOW,
            )
            assert updated_team.timeline == Timeline.WIN_NOW
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_gm_status_counts_only_offers_addressed_to_this_team():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "GS1")
            team_b = await _make_team(pool, league_id, "GS2", manager_user_id=_STRANGER)
            await team_repo.upsert_team_strategy(
                pool,
                league_id=league_id,
                team_id=team_a,
                season=2026,
                pace_target=50,
                offensive_scheme=OffensiveScheme.MOTION,
                defensive_scheme=DefensiveScheme.DROP,
                help_aggression=50,
                three_point_bias=50,
                crash_oreb_bias=50,
            )
            await offer_repo.create_trade_offer(
                pool,
                league_id=league_id,
                season=2026,
                phase=Phase.REGULAR_SEASON,
                from_team_id=team_a,
                to_team_id=team_b,
                proposed_by=_MANAGER,
                status=TradeStatus.PROPOSED,
                players_from_to=(),
                players_to_from=(),
                picks_from_to=(),
                picks_to_from=(),
            )
            await offer_repo.create_trade_offer(
                pool,
                league_id=league_id,
                season=2026,
                phase=Phase.REGULAR_SEASON,
                from_team_id=team_b,
                to_team_id=team_a,
                proposed_by=_STRANGER,
                status=TradeStatus.PROPOSED,
                players_from_to=(),
                players_to_from=(),
                picks_from_to=(),
                picks_to_from=(),
            )

            status_a = await gm_command_service.get_gm_status(
                pool, league_id=league_id, team_id=team_a
            )
            assert status_a.strategy is not None
            assert status_a.strategy.pace_target == 50
            # team_a SENT one offer and RECEIVED one -- only the received
            # one is parked in its own inbox (GM-4's rule: offers addressed
            # TO a team are the ones left for its GM to answer).
            assert status_a.parked_offers_addressed_to_team == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_gm_player_view_rejects_a_player_not_on_the_requested_team():
    """Service-level proof of `get_gm_player_view`'s roster-membership
    guard (B2, `bot/cogs/gm.py`'s `/gm player` fix, `0f0eb4e`) -- exercised
    directly here rather than only through the cog, since the cog's own
    regression test only proves the bot-layer authorization check, not
    this independent service-layer guard against a *different* team's
    player id."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "GP1")
            team_b = await _make_team(pool, league_id, "GP2", manager_user_id=77)
            player_on_b = await _make_player(pool, league_id, team_b)

            with pytest.raises(ValidationError):
                await gm_command_service.get_gm_player_view(
                    pool, league_id=league_id, team_id=team_a, player_id=player_on_b
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
