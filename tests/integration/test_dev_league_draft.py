"""Live I/O proof for `tools/dev_league_draft.py` -- Decision P4-120
(round 25) hardened `place_selections` (one transaction, not 60 bare
autocommits) and `ensure_draft_class` (a real, count-aware completeness
check, not a bare non-empty guard) against the two real, disclosed resume
gaps a round-25 critic pass found by reading the call sites directly. No
integration test existed for this module at all before this round --
Stage D step 22 (`e25033f`) shipped `ensure_draft_class`/`ensure_draft`/
`place_selections` with real corpus-generation "proof" but no direct,
DB-backed test of any of the three functions -- a real, pre-existing gap
this round closes for the two functions it touches.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from types import SimpleNamespace

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, league_repo, player_repo, team_repo
from dba2.domain.player import Position, RosterStatus
from tools.dev_league_draft import ensure_draft_class, place_selections

_SEASON = 2026


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Dev League Draft Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=2026_25,
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
        home_market_size=2,
    )
    return team.id


async def _make_prospect(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Draft",
        last_name=f"Prospect{random.randint(0, 10**9)}",
        primary_position=Position.PG,
        height_in=75,
        weight_lb=185,
        wingspan_in=78,
        birth_date=date(_SEASON - 19, 6, 6),
        roster_status=RosterStatus.DRAFT_ELIGIBLE,
        overall_cached=55,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


# ---------------------------------------------------------------------------
# place_selections -- Decision P4-120: one transaction, not 60 autocommits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_selections_rolls_back_every_placement_on_a_mid_batch_failure():
    """Blocking issue 3, variant 1 (design doc §37, round-25 critic
    finding; fixed by Decision P4-120): the original `place_selections`
    looped bare `player_repo.set_team_and_status` calls directly against
    the pool -- 60 independent autocommits -- so a kill (or, provably
    here, any real exception) partway through left a REAL, persisted,
    partially-placed class. Two valid selections are placed first, then a
    third selection targets a `team_id` that does not exist (violates
    `players.team_id`'s real FK, `REFERENCES teams(id)`) -- the same shape
    of real failure a crash mid-loop would leave behind, forcing a real
    `asyncpg.ForeignKeyViolationError` partway through the batch.

    Fixed: the whole batch now runs inside one `transaction()`, so this
    failure must roll back ALL THREE placements, including the two valid
    ones that ran before the failing one -- proven directly by re-reading
    every prospect's own `roster_status`/`team_id` afterward and finding
    them exactly as they started (`DRAFT_ELIGIBLE`, `team_id=None`), never
    partially advanced. Manually confirmed this test fails (the two valid
    prospects show `ACTIVE`/their real team) under a temporary revert to
    the original bare-pool loop, and passes under the real fix."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PS1")
            prospect_1 = await _make_prospect(pool, league_id)
            prospect_2 = await _make_prospect(pool, league_id)
            prospect_3 = await _make_prospect(pool, league_id)
            nonexistent_team_id = 999_999_999

            selections = [
                SimpleNamespace(player_id=prospect_1, team_id=team_a),
                SimpleNamespace(player_id=prospect_2, team_id=team_a),
                SimpleNamespace(player_id=prospect_3, team_id=nonexistent_team_id),
            ]

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await place_selections(
                    pool, league_id=league_id, season=_SEASON, selections=selections,
                )

            for prospect_id in (prospect_1, prospect_2, prospect_3):
                row = await player_repo.get_player(pool, prospect_id)
                assert row.roster_status == RosterStatus.DRAFT_ELIGIBLE, (
                    "a mid-batch failure must roll back every placement in the same call, "
                    "including the ones that ran successfully before the failing one"
                )
                assert row.team_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_place_selections_still_places_a_whole_clean_batch():
    """The mirror image of the rollback test above -- a real, no-failure
    batch must still place every selection, proving the transaction wrap
    didn't turn this into a no-op."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PS2")
            team_b = await _make_team(pool, league_id, "PS3")
            prospect_1 = await _make_prospect(pool, league_id)
            prospect_2 = await _make_prospect(pool, league_id)

            selections = [
                SimpleNamespace(player_id=prospect_1, team_id=team_a),
                SimpleNamespace(player_id=prospect_2, team_id=team_b),
            ]
            await place_selections(
                pool, league_id=league_id, season=_SEASON, selections=selections,
            )

            row_1 = await player_repo.get_player(pool, prospect_1)
            row_2 = await player_repo.get_player(pool, prospect_2)
            assert (row_1.roster_status, row_1.team_id) == (RosterStatus.ACTIVE, team_a)
            assert (row_2.roster_status, row_2.team_id) == (RosterStatus.ACTIVE, team_b)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# ensure_draft_class -- Decision P4-120: a real, count-aware completeness
# check, not a bare non-empty guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_draft_class_tops_up_a_partial_class_without_duplicating_existing_prospects():
    """Blocking issue 3, variant 2 (design doc §37, round-25 critic
    finding; fixed by Decision P4-120): the original guard (`if existing:
    return`) treated ANY non-empty `draft_classes` row set as a fully-done
    class -- `ensure_draft_class`'s own per-prospect loop commits one
    `transaction` per prospect, not one for the whole class, so a kill
    partway through left a real, short class that guard would never top
    up, silently running that season's draft short forever.

    Simulated here the same way a real resume would produce a partial
    class: one call at `class_size=2` (a completed, correct 2-prospect
    class -- the stand-in for "however many prospects a prior, since-
    killed call managed to persist before it died"), then a second call
    at the REAL `class_size=5` with the identical `season`/`rng_seed`.
    `class_seed` is derived the same deterministic way every call, so the
    second call re-derives the SAME first two prospects (same `mock_rank`,
    same generated ratings) and must skip them, persisting only the three
    new ones -- never a duplicate `draft_classes` row for `mock_rank` 1 or
    2, and never a second, differently-seeded class chosen instead."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            rng_seed = 4242

            await ensure_draft_class(
                pool, league_id=league_id, season=_SEASON, class_size=2, rng_seed=rng_seed,
            )
            partial = await draft_repo.list_draft_class(pool, league_id, _SEASON)
            assert {c.mock_rank for c in partial} == {1, 2}
            partial_player_ids_by_rank = {c.mock_rank: c.player_id for c in partial}

            await ensure_draft_class(
                pool, league_id=league_id, season=_SEASON, class_size=5, rng_seed=rng_seed,
            )
            topped_up = await draft_repo.list_draft_class(pool, league_id, _SEASON)
            assert {c.mock_rank for c in topped_up} == {1, 2, 3, 4, 5}
            assert len(topped_up) == 5, "no duplicate row for an already-persisted mock_rank"

            topped_up_by_rank = {c.mock_rank: c.player_id for c in topped_up}
            for rank, player_id in partial_player_ids_by_rank.items():
                assert topped_up_by_rank[rank] == player_id, (
                    "the top-up call must reuse the SAME persisted prospect for a rank that "
                    "already exists, never create a second, duplicate player for it"
                )

            # A second, already-complete call must stay a true no-op (the pre-existing
            # idempotence guarantee, unaffected by the count-aware fix).
            await ensure_draft_class(
                pool, league_id=league_id, season=_SEASON, class_size=5, rng_seed=rng_seed,
            )
            still_five = await draft_repo.list_draft_class(pool, league_id, _SEASON)
            assert len(still_five) == 5
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
