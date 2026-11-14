"""Live I/O proof for `data/repositories/draft_repo.py` — smoke coverage
for draft_picks, drafts (including the `complete_lottery` guard), and
draft_selections/draft_classes.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, league_repo, player_repo, team_repo
from dba2.domain.draft import ProtectionType
from dba2.domain.player import Position, RosterStatus


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Repo Test League",
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
        conference="West",
        division="Pacific",
        home_market_size=2,
    )
    return team.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Draft",
        last_name="Prospect",
        primary_position=Position.PG,
        height_in=75,
        weight_lb=185,
        wingspan_in=78,
        birth_date=date(2005, 6, 6),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=60,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_create_get_draft_pick_and_list_for_team_and_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PKA")
            team_b = await _make_team(pool, league_id, "PKB")

            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2027,
                round=1,
                original_team_id=team_a,
                current_team_id=team_b,
            )
            assert pick.protection_type == ProtectionType.NONE
            assert pick.conveyed is False
            assert pick.pick_number is None

            fetched = await draft_repo.get_draft_pick(pool, pick.id)
            assert fetched is not None
            assert fetched.current_team_id == team_b

            for_team = await draft_repo.list_draft_picks_for_team(pool, league_id, team_b)
            assert [p.id for p in for_team] == [pick.id]
            assert await draft_repo.list_draft_picks_for_team(pool, league_id, team_a) == []

            for_season = await draft_repo.list_draft_picks_for_season(pool, league_id, 2027)
            assert [p.id for p in for_season] == [pick.id]
            assert await draft_repo.list_draft_picks_for_season(pool, league_id, 2099) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_draft_picks_for_league_range_batches_across_seasons():
    """Phase 2 step 7's fourth bulk loader (§8) -- one query across every
    season in `seasons`, equivalent to running `list_draft_picks_for_season`
    once per season and concatenating the results."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "RGA")
            team_b = await _make_team(pool, league_id, "RGB")

            pick_2026 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=2026, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )
            pick_2027 = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=2027, round=2,
                original_team_id=team_b, current_team_id=team_b,
            )
            # Outside the requested range -- must not appear.
            await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=2030, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )

            ranged = await draft_repo.list_draft_picks_for_league_range(
                pool, league_id, [2026, 2027, 2028]
            )
            assert {p.id for p in ranged} == {pick_2026.id, pick_2027.id}

            for_2026 = await draft_repo.list_draft_picks_for_season(pool, league_id, 2026)
            for_2027 = await draft_repo.list_draft_picks_for_season(pool, league_id, 2027)
            assert {p.id for p in ranged} == {p.id for p in for_2026} | {p.id for p in for_2027}

            assert await draft_repo.list_draft_picks_for_league_range(pool, league_id, []) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_draft_pick_round_trips_every_field_distinctly():
    """Every `DraftPick` field gets a distinct value so a mapper that
    swapped two same-typed fields (e.g. `original_team_id`/`current_team_id`,
    both `int`) would fail this test (Phase 0 step-7 critic round, B2
    lesson)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "DPA")
            team_b = await _make_team(pool, league_id, "DPB")
            other_pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2027,
                round=2,
                original_team_id=team_a,
                current_team_id=team_a,
            )
            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2028,
                round=1,
                original_team_id=team_a,
                current_team_id=team_b,
                pick_number=5,
                protection_type=ProtectionType.TOP_N,
                protection_through=8,
                swap_with_pick_id=other_pick.id,
                conveyed=True,
                rolls_over_to_season=2029,
            )
            fetched = await draft_repo.get_draft_pick(pool, pick.id)
            assert fetched.league_id == league_id
            assert fetched.season == 2028
            assert fetched.round == 1
            assert fetched.original_team_id == team_a
            assert fetched.current_team_id == team_b
            assert fetched.pick_number == 5
            assert fetched.protection_type == ProtectionType.TOP_N
            assert fetched.protection_through == 8
            assert fetched.swap_with_pick_id == other_pick.id
            assert fetched.conveyed is True
            assert fetched.rolls_over_to_season == 2029
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_pick_number_current_team_and_used_for_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "SPA")
            team_b = await _make_team(pool, league_id, "SPB")
            player_id = await _make_player(pool, league_id)

            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2027,
                round=1,
                original_team_id=team_a,
                current_team_id=team_a,
            )
            await draft_repo.set_pick_number(pool, pick.id, 14)
            await draft_repo.set_current_team(pool, pick.id, team_b)
            await draft_repo.set_used_for_player(pool, pick.id, player_id)

            fetched = await draft_repo.get_draft_pick(pool, pick.id)
            assert fetched.pick_number == 14
            assert fetched.current_team_id == team_b
            assert fetched.original_team_id == team_a
            assert fetched.used_for_player_id == player_id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_mark_conveyed_is_idempotent():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "CVA")
            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2027,
                round=1,
                original_team_id=team_a,
                current_team_id=team_a,
            )
            assert (await draft_repo.get_draft_pick(pool, pick.id)).conveyed is False

            await draft_repo.mark_conveyed(pool, pick.id)
            assert (await draft_repo.get_draft_pick(pool, pick.id)).conveyed is True

            await draft_repo.mark_conveyed(pool, pick.id)
            assert (await draft_repo.get_draft_pick(pool, pick.id)).conveyed is True
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_get_draft_and_get_for_season_and_advance_pointer():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=2027, total_picks=60
            )
            assert draft.current_round == 1
            assert draft.current_pick_number == 1
            assert draft.lottery_completed_at is None

            fetched = await draft_repo.get_draft(pool, draft.id)
            assert fetched is not None
            assert fetched.total_picks == 60

            by_season = await draft_repo.get_draft_for_season(pool, league_id, 2027)
            assert by_season is not None
            assert by_season.id == draft.id
            assert await draft_repo.get_draft_for_season(pool, league_id, 2099) is None

            await draft_repo.advance_draft_pointer(
                pool, draft.id, current_round=2, current_pick_number=17
            )
            advanced = await draft_repo.get_draft(pool, draft.id)
            assert advanced.current_round == 2
            assert advanced.current_pick_number == 17
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_complete_lottery_is_a_noop_on_an_already_completed_draft():
    """`complete_lottery` must not clobber `lottery_completed_at` on a
    second call — mirrors `team_repo.close_manager_assignment`'s guard
    (Phase 0 step-7 critic round, B1's bug class)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=2027, total_picks=60
            )
            await draft_repo.complete_lottery(pool, draft.id)
            first = await draft_repo.get_draft(pool, draft.id)
            assert first.lottery_completed_at is not None

            await draft_repo.complete_lottery(pool, draft.id)
            second = await draft_repo.get_draft(pool, draft.id)
            assert second.lottery_completed_at == first.lottery_completed_at
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_selection_create_get_and_list():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "SLA")
            player_id = await _make_player(pool, league_id)
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=2027, total_picks=60
            )
            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=2027,
                round=3,
                original_team_id=team_a,
                current_team_id=team_a,
                pick_number=17,
            )
            # pick_number != round (17 vs 3) so a mapper that swapped the
            # two (both `int`) would fail below (Phase 0 step-7 critic
            # round, B3 — the previous version of this fixture used 1 for
            # both, which made the swap structurally invisible).
            selection = await draft_repo.create_draft_selection(
                pool,
                draft_id=draft.id,
                pick_number=17,
                round=3,
                team_id=team_a,
                player_id=player_id,
                pick_asset_id=pick.id,
            )
            assert selection.pick_number == 17
            assert selection.round == 3
            assert selection.draft_id == draft.id
            assert selection.team_id == team_a
            assert selection.player_id == player_id
            assert selection.pick_asset_id == pick.id
            assert selection.selected_at is not None

            fetched = await draft_repo.get_draft_selection(pool, selection.id)
            assert fetched is not None
            assert fetched.id == selection.id

            for_draft = await draft_repo.list_draft_selections_for_draft(pool, draft.id)
            assert [s.id for s in for_draft] == [selection.id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_draft_class_create_and_list_ordered_by_mock_rank():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_1 = await _make_player(pool, league_id)
            player_2 = await _make_player(pool, league_id)
            player_unranked = await _make_player(pool, league_id)

            entry_2 = await draft_repo.create_draft_class_entry(
                pool,
                league_id=league_id,
                class_year=2027,
                player_id=player_2,
                mock_rank=2,
                source="scout_board",
            )
            entry_1 = await draft_repo.create_draft_class_entry(
                pool,
                league_id=league_id,
                class_year=2027,
                player_id=player_1,
                mock_rank=1,
                is_generated=False,
                source="consensus",
            )
            entry_unranked = await draft_repo.create_draft_class_entry(
                pool, league_id=league_id, class_year=2027, player_id=player_unranked
            )
            assert entry_1.is_generated is False
            assert entry_2.source == "scout_board"
            assert entry_unranked.mock_rank is None

            listed = await draft_repo.list_draft_class(pool, league_id, 2027)
            assert [e.player_id for e in listed] == [player_1, player_2, player_unranked]
            assert await draft_repo.list_draft_class(pool, league_id, 2099) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
