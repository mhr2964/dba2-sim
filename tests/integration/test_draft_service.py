"""Live I/O proof for `services/draft_service.py` -- §9.4's draft-board
construction (Decision P2-27), §12 step 9's own scope (docs/design/
phase-2-manager-minds.md). Zero coverage on this module before this fix
round (critic's finding, confirmed via grep) -- this file closes that gap,
matching the convention every other Phase 2 service step has followed
(e.g. step 7's `capsheet_service.py` got its own integration test file).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import dataclasses
import random

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, team_repo
from dba2.domain.player import RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.draft import build_draft_board
from dba2.managers.snapshot import build_roster_player
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import draft_service
from tests.fixtures.managers import CAP_SETTINGS, make_roster_player

_SEASON = 2026
_RNG_SEED = 2026_09


async def _make_league_and_team(pool, *, gm_philosophy: GmPhilosophy) -> tuple[int, int]:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Service Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    team = await team_repo.create_team(
        pool,
        league_id=league.id,
        code="DFT",
        name="DFT Team",
        city="DFT City",
        conference="West",
        division="Pacific",
        home_market_size=3,
        gm_philosophy=gm_philosophy,
    )
    return league.id, team.id


def _draft_eligible_prospects(n: int) -> tuple:
    """`n` hand-built `draft_eligible` prospects -- `services/draft_
    service.py`'s own module docstring states it takes `prospects` as an
    already-assembled caller-supplied fact (Gap F: nothing under `dba2/`
    produces a real one yet), so this test builds them the same way
    `tests/unit/managers/decisions/test_draft.py` does rather than
    reaching into the quarantined `tools/_gm_draft_class_stub.py`."""
    prospects = []
    for i in range(1, n + 1):
        rp = make_roster_player(6000 + i, age=20, season=_SEASON, overall_cached=60 + i)
        player = dataclasses.replace(
            rp.player, roster_status=RosterStatus.DRAFT_ELIGIBLE, team_id=None
        )
        prospects.append(build_roster_player(player, rp.ratings, contract=None))
    return tuple(prospects)


@pytest.mark.asyncio
async def test_load_rookie_scale_returns_all_30_rows_keyed_by_pick_number():
    """`rookie_scale` is league-independent seed data (migration 003/009)
    -- no cleanup needed, this test only reads it."""
    pool = await get_pool()
    try:
        table = await draft_service.load_rookie_scale(pool)
        assert len(table) == 30
        assert set(table) == set(range(1, 31))
        assert table[1].pick_number == 1
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_build_draft_board_for_team_delegates_to_build_draft_board():
    """`build_draft_board_for_team`'s two real reads (`team_repo.get_team`
    for `gm_philosophy`, `load_rookie_scale` for the real table) are the
    only things that differ from calling `build_draft_board` directly --
    the returned board must match exactly."""
    pool = await get_pool()
    try:
        league_id, team_id = await _make_league_and_team(
            pool, gm_philosophy=GmPhilosophy.EGALITARIAN
        )
        try:
            prospects = _draft_eligible_prospects(8)
            rookie_scale = await draft_service.load_rookie_scale(pool)

            board = await draft_service.build_draft_board_for_team(
                pool,
                team_id=team_id,
                prospects=prospects,
                league_rng_seed=_RNG_SEED,
                season=_SEASON,
                window=CompetitiveWindow.RETOOL,
                cap_settings=CAP_SETTINGS,
            )
            expected_board = build_draft_board(
                prospects, league_rng_seed=_RNG_SEED, team_id=team_id, season=_SEASON,
                philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.RETOOL,
                cap_settings=CAP_SETTINGS, rookie_scale=rookie_scale,
            )
            assert board == expected_board
            assert len(board) == 8
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_build_draft_board_for_team_raises_not_found_for_missing_team():
    pool = await get_pool()
    try:
        prospects = _draft_eligible_prospects(3)
        with pytest.raises(NotFoundError):
            await draft_service.build_draft_board_for_team(
                pool,
                team_id=999_999,
                prospects=prospects,
                league_rng_seed=_RNG_SEED,
                season=_SEASON,
                window=CompetitiveWindow.CONTEND,
                cap_settings=CAP_SETTINGS,
            )
    finally:
        await close_pool()
