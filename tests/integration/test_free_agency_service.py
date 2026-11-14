"""Live I/O proof for `services/free_agency_service.py` -- §9.3's offer
construction/ranking (Decision P2-21), §12 step 9's own scope (docs/design/
phase-2-manager-minds.md). Zero coverage on this module before this fix
round (critic's finding, confirmed via grep) -- this file closes that gap,
matching the convention every other Phase 2 service step has followed
(e.g. step 7's `capsheet_service.py` got its own integration test file).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, team_repo
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.signing import SigningOffer, construct_offer
from dba2.managers.needs import build_need_matrix
from dba2.managers.snapshot import build_team_snapshot
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import free_agency_service
from tests.fixtures.managers import CAP_SETTINGS, make_roster_player
from tests.fixtures.sim import make_player, make_team

_SEASON = 2026


async def _make_league_and_team(pool, *, gm_philosophy: GmPhilosophy) -> tuple[int, int]:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Free Agency Service Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    team = await team_repo.create_team(
        pool,
        league_id=league.id,
        code="FAS",
        name="FAS Team",
        city="FAS City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        gm_philosophy=gm_philosophy,
    )
    return league.id, team.id


def _empty_matrix():
    snapshot = build_team_snapshot(make_team(1), (), (), CAP_SETTINGS)
    return build_need_matrix(snapshot)


@pytest.mark.asyncio
async def test_construct_offer_for_team_delegates_to_construct_offer():
    """`construct_offer_for_team`'s one real read (`team_repo.get_team`,
    for `gm_philosophy`) is the only thing that differs from calling
    `construct_offer` directly -- everything else should match exactly."""
    pool = await get_pool()
    try:
        league_id, team_id = await _make_league_and_team(
            pool, gm_philosophy=GmPhilosophy.STAR_MAXER
        )
        try:
            roster_player = make_roster_player(5101, age=26, season=_SEASON, overall_cached=85)
            matrix = _empty_matrix()

            offer, record = await free_agency_service.construct_offer_for_team(
                pool,
                team_id=team_id,
                roster_player=roster_player,
                matrix=matrix,
                window=CompetitiveWindow.RETOOL,
                cap_settings=CAP_SETTINGS,
                room=10_000_000,
                season=_SEASON,
            )
            expected_offer, expected_record = construct_offer(
                roster_player, matrix, team_id=team_id, philosophy=GmPhilosophy.STAR_MAXER,
                window=CompetitiveWindow.RETOOL, cap_settings=CAP_SETTINGS, room=10_000_000,
                season=_SEASON,
            )
            assert offer == expected_offer
            assert record.score == pytest.approx(expected_record.score, abs=1e-9)
            assert record.reconstructs()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_construct_offer_for_team_raises_not_found_for_missing_team():
    pool = await get_pool()
    try:
        roster_player = make_roster_player(5102, age=26, season=_SEASON, overall_cached=80)
        matrix = _empty_matrix()
        with pytest.raises(NotFoundError):
            await free_agency_service.construct_offer_for_team(
                pool,
                team_id=999_999,
                roster_player=roster_player,
                matrix=matrix,
                window=CompetitiveWindow.CONTEND,
                cap_settings=CAP_SETTINGS,
                room=5_000_000,
                season=_SEASON,
            )
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# rank_offers -- Decision P2-21's placeholder (no DB needed, but lives with
# this module's other tests per this project's own per-service-file
# convention)
# ---------------------------------------------------------------------------


def test_rank_offers_ranks_by_guaranteed_money_first():
    player = make_player(1)
    low = SigningOffer(team_id=1, player_id=1, years=1, annual_salary=5_000_000,
                        window=CompetitiveWindow.CONTEND)
    high = SigningOffer(team_id=2, player_id=1, years=3, annual_salary=8_000_000,
                         window=CompetitiveWindow.REBUILD)
    ranked = free_agency_service.rank_offers((low, high), player)
    assert ranked[0] is high, "24M guaranteed must outrank 5M regardless of window"
    assert ranked[1] is low


def test_rank_offers_breaks_ties_by_window_rank_contenders_ahead_of_rebuilders():
    player = make_player(1)
    rebuild_offer = SigningOffer(team_id=1, player_id=1, years=2, annual_salary=5_000_000,
                                  window=CompetitiveWindow.REBUILD)
    contend_offer = SigningOffer(team_id=2, player_id=1, years=2, annual_salary=5_000_000,
                                  window=CompetitiveWindow.CONTEND)
    ranked = free_agency_service.rank_offers((rebuild_offer, contend_offer), player)
    assert ranked[0] is contend_offer, "equal guaranteed money must tie-break to CONTEND first"
    assert ranked[1] is rebuild_offer
