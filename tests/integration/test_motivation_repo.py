"""Live I/O proof for `data/repositories/motivation_repo.py` — smoke
coverage for player_motivation (create, bulk list, and the trait/state split
this module enforces via two separate update functions) and the append-only
player_motivation_events log.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo, motivation_repo, player_repo
from dba2.domain.motivation import MarketPreference, MotivationAxis, RequestState
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Motivation Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Motivated",
        last_name="Player",
        primary_position=Position.C,
        height_in=84,
        weight_lb=250,
        wingspan_in=88,
        birth_date=date(1997, 7, 7),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=65,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


async def _create_distinct_motivation(pool, league_id: int, player_id: int, hometown_team_id=None):
    return await motivation_repo.create_player_motivation(
        pool,
        player_id=player_id,
        league_id=league_id,
        weight_winning=10,
        weight_role_usage=20,
        weight_loyalty=30,
        weight_money=40,
        weight_legacy=50,
        hometown_team_id=hometown_team_id,
        market_preference=MarketPreference.BIG_MARKET,
        star_leverage=60,
        satisfaction_winning=-10,
        satisfaction_role_usage=-20,
        satisfaction_loyalty=-30,
        satisfaction_money=-40,
        satisfaction_legacy=-50,
        morale=15,
        trust_in_management=25,
        request_state=RequestState.UNSETTLED,
        request_opened_in_season=2025,
        last_evaluated_season=2026,
    )


@pytest.mark.asyncio
async def test_create_get_and_list_player_motivation_round_trips_every_field_distinctly():
    """Every field gets a distinct value so a mapper that swapped two
    same-typed fields (e.g. `weight_winning`/`weight_role_usage`, both
    `int`) would fail this test (Phase 0 step-7 critic round, B2 lesson)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            created = await _create_distinct_motivation(pool, league_id, player_id)
            assert created.weight_winning == 10
            assert created.weight_role_usage == 20
            assert created.weight_loyalty == 30
            assert created.weight_money == 40
            assert created.weight_legacy == 50
            assert created.hometown_team_id is None
            assert created.market_preference == MarketPreference.BIG_MARKET
            assert created.star_leverage == 60
            assert created.satisfaction_winning == -10
            assert created.satisfaction_role_usage == -20
            assert created.satisfaction_loyalty == -30
            assert created.satisfaction_money == -40
            assert created.satisfaction_legacy == -50
            assert created.morale == 15
            assert created.trust_in_management == 25
            assert created.request_state == RequestState.UNSETTLED
            assert created.request_opened_in_season == 2025
            assert created.last_evaluated_season == 2026

            fetched = await motivation_repo.get_player_motivation(pool, player_id)
            assert fetched == created

            listed = await motivation_repo.list_player_motivation_for_league(pool, league_id)
            assert [m.player_id for m in listed] == [player_id]

            assert await motivation_repo.get_player_motivation(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_motivation_state_only_touches_state_columns():
    """`update_motivation_state` returns `None` (matching every other
    repo's UPDATE convention) — assertions re-fetch via
    `get_player_motivation` (Phase 0 step-7 critic round, B4)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            created = await _create_distinct_motivation(pool, league_id, player_id)

            result = await motivation_repo.update_motivation_state(
                pool,
                player_id,
                satisfaction_winning=11,
                satisfaction_role_usage=22,
                satisfaction_loyalty=33,
                satisfaction_money=44,
                satisfaction_legacy=55,
                morale=66,
                trust_in_management=77,
                request_state=RequestState.TRADE_REQUESTED,
                request_opened_in_season=2026,
                last_evaluated_season=2027,
                last_checkpoint_phase=Phase.TRADE_DEADLINE,
            )
            assert result is None

            updated = await motivation_repo.get_player_motivation(pool, player_id)
            assert updated.satisfaction_winning == 11
            assert updated.satisfaction_role_usage == 22
            assert updated.satisfaction_loyalty == 33
            assert updated.satisfaction_money == 44
            assert updated.satisfaction_legacy == 55
            assert updated.morale == 66
            assert updated.trust_in_management == 77
            assert updated.request_state == RequestState.TRADE_REQUESTED
            assert updated.request_opened_in_season == 2026
            assert updated.last_evaluated_season == 2027
            assert updated.last_checkpoint_phase is Phase.TRADE_DEADLINE

            # Trait columns from the original create call are untouched.
            assert updated.weight_winning == created.weight_winning
            assert updated.weight_role_usage == created.weight_role_usage
            assert updated.weight_loyalty == created.weight_loyalty
            assert updated.weight_money == created.weight_money
            assert updated.weight_legacy == created.weight_legacy
            assert updated.market_preference == created.market_preference
            assert updated.star_leverage == created.star_leverage
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_motivation_state_requires_explicit_request_opened_in_season():
    """`request_opened_in_season` has no default — a caller cannot omit it
    and silently wipe it. Regression test for Phase 0 step-7 critic round,
    B1: the previous version defaulted this column to `None`, so any caller
    updating satisfaction without also re-passing the open trade-request
    season silently cleared it while `request_state` stayed
    `trade_requested`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            await _create_distinct_motivation(pool, league_id, player_id)

            with pytest.raises(TypeError):
                await motivation_repo.update_motivation_state(
                    pool,
                    player_id,
                    satisfaction_winning=11,
                    satisfaction_role_usage=22,
                    satisfaction_loyalty=33,
                    satisfaction_money=44,
                    satisfaction_legacy=55,
                    morale=66,
                    trust_in_management=77,
                    request_state=RequestState.TRADE_REQUESTED,
                    last_evaluated_season=2027,
                    # A REAL value, deliberately: without it the TypeError would be
                    # about the newer required kwarg and this regression test for
                    # `request_opened_in_season` would pass while proving nothing.
                    last_checkpoint_phase=Phase.TRADE_DEADLINE,
                    # request_opened_in_season deliberately omitted
                )

            # An explicit None is the correct way to clear it, and must
            # actually clear it (not silently preserve the old value).
            await motivation_repo.update_motivation_state(
                pool,
                player_id,
                satisfaction_winning=11,
                satisfaction_role_usage=22,
                satisfaction_loyalty=33,
                satisfaction_money=44,
                satisfaction_legacy=55,
                morale=66,
                trust_in_management=77,
                request_state=RequestState.CONTENT,
                request_opened_in_season=None,
                last_evaluated_season=2027,
                last_checkpoint_phase=None,
            )
            cleared = await motivation_repo.get_player_motivation(pool, player_id)
            assert cleared.request_opened_in_season is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_motivation_state_requires_explicit_last_checkpoint_phase():
    """The twin of the case above for the per-player checkpoint guard
    (phase-5-stage-e-advance.md P5-99.r.2, B-4): `last_checkpoint_phase` has no
    default although its column is nullable, because a defaulted guard column
    silently clears itself and a cleared guard is a double-fold. Every OTHER
    kwarg is passed -- including `request_opened_in_season` -- so the only thing
    that can raise the `TypeError` is the guard column's omission."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            await _create_distinct_motivation(pool, league_id, player_id)

            with pytest.raises(TypeError, match="last_checkpoint_phase"):
                await motivation_repo.update_motivation_state(
                    pool,
                    player_id,
                    satisfaction_winning=11,
                    satisfaction_role_usage=22,
                    satisfaction_loyalty=33,
                    satisfaction_money=44,
                    satisfaction_legacy=55,
                    morale=66,
                    trust_in_management=77,
                    request_state=RequestState.TRADE_REQUESTED,
                    request_opened_in_season=2026,
                    last_evaluated_season=2027,
                    # last_checkpoint_phase deliberately omitted
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_motivation_traits_only_touches_trait_columns():
    """`update_motivation_traits` returns `None` (matching every other
    repo's UPDATE convention) — assertions re-fetch via
    `get_player_motivation` (Phase 0 step-7 critic round, B4)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            created = await _create_distinct_motivation(pool, league_id, player_id)

            result = await motivation_repo.update_motivation_traits(
                pool,
                player_id,
                weight_winning=91,
                weight_role_usage=92,
                weight_loyalty=93,
                weight_money=94,
                weight_legacy=95,
                star_leverage=96,
                market_preference=MarketPreference.WARM_WEATHER,
                hometown_team_id=None,
            )
            assert result is None

            updated = await motivation_repo.get_player_motivation(pool, player_id)
            assert updated.weight_winning == 91
            assert updated.weight_role_usage == 92
            assert updated.weight_loyalty == 93
            assert updated.weight_money == 94
            assert updated.weight_legacy == 95
            assert updated.star_leverage == 96
            assert updated.market_preference == MarketPreference.WARM_WEATHER

            # State columns from the original create call are untouched.
            assert updated.satisfaction_winning == created.satisfaction_winning
            assert updated.satisfaction_role_usage == created.satisfaction_role_usage
            assert updated.satisfaction_loyalty == created.satisfaction_loyalty
            assert updated.satisfaction_money == created.satisfaction_money
            assert updated.satisfaction_legacy == created.satisfaction_legacy
            assert updated.morale == created.morale
            assert updated.trust_in_management == created.trust_in_management
            assert updated.request_state == created.request_state
            assert updated.last_evaluated_season == created.last_evaluated_season
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_player_motivation_event_round_trips_every_field_distinctly():
    """Every field gets a distinct value so a mapper that swapped
    `season`/`delta` (both `int`) would fail this test — the previous
    version of this suite only asserted `event_type`/`axis`/`delta`/`note`,
    never `season`, which is exactly the field a critic-round mutation
    swapped and the suite missed (Phase 0 step-7 critic round, B3)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            event = await motivation_repo.create_motivation_event(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2019,
                phase=Phase.PLAYOFFS_R1,
                event_type="finals_win",
                axis=MotivationAxis.LEGACY,
                delta=42,
                note="Distinct Event Note",
            )
            assert event.season == 2019
            assert event.delta == 42
            assert event.event_type == "finals_win"
            assert event.axis == MotivationAxis.LEGACY
            assert event.note == "Distinct Event Note"

            [fetched] = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            assert fetched.season == 2019
            assert fetched.delta == 42
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


def test_list_motivation_events_sql_orders_by_created_at_then_id():
    """A behavioral test can't reliably prove this: on a small, freshly
    inserted table Postgres tends to return heap/insertion order even
    without an explicit tiebreak, so a same-timestamp-tie test passes
    whether or not `id` is really in the `ORDER BY` (confirmed by mutation
    — stripping the tiebreak still passed 5/5 runs on a 2-row table; the
    critic round that found this bug only proved it with a forced parallel
    scan over 200k rows). Asserting on the literal SQL is the deterministic
    version of the same guarantee (Phase 0 step-7 critic round, B2)."""
    import inspect

    source = inspect.getsource(motivation_repo.list_motivation_events_for_player)
    assert "ORDER BY created_at, id" in source


@pytest.mark.asyncio
async def test_list_motivation_events_orders_by_id_when_timestamps_tie():
    """Same-transaction insertion order preserved when `created_at` ties —
    smoke coverage alongside the deterministic SQL check above, which is
    the one that actually proves the tiebreak is load-bearing."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            async with transaction(pool) as conn:
                first = await motivation_repo.create_motivation_event(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    season=2026,
                    phase=Phase.REGULAR_SEASON,
                    event_type="first",
                    axis=MotivationAxis.WINNING,
                    delta=1,
                )
                second = await motivation_repo.create_motivation_event(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    season=2026,
                    phase=Phase.REGULAR_SEASON,
                    event_type="second",
                    axis=MotivationAxis.WINNING,
                    delta=2,
                )
            # Same transaction => plausibly the same created_at; id must
            # still resolve the order correctly either way.
            assert first.created_at == second.created_at
            assert first.id < second.id

            history = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            assert [e.id for e in history] == [first.id, second.id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_motivation_event_and_list_for_player():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            event1 = await motivation_repo.create_motivation_event(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                phase=Phase.REGULAR_SEASON,
                event_type="losing_streak",
                axis=MotivationAxis.WINNING,
                delta=-15,
                note="Distinct Note One",
            )
            assert event1.event_type == "losing_streak"
            assert event1.axis == MotivationAxis.WINNING
            assert event1.delta == -15
            assert event1.note == "Distinct Note One"
            assert event1.created_at is not None

            event2 = await motivation_repo.create_motivation_event(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                phase=Phase.FREE_AGENCY,
                event_type="trade_denied",
                axis=MotivationAxis.LOYALTY,
                delta=8,
            )
            assert event2.note is None

            history = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            assert [e.id for e in history] == [event1.id, event2.id]

            # docs/design/media-v2-newsroom.md MV-2b: `from_state`/`to_state`
            # default `None` on every existing call shape (event1/event2
            # above pass neither) -- confirmed here, not just at the
            # domain-dataclass default.
            assert event1.from_state is None
            assert event1.to_state is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_motivation_event_round_trips_from_state_and_to_state():
    """migration 021 / MV-2b: the ladder-transition insert's own two typed
    columns round-trip through the mapper, and a pre-migration-shaped call
    (neither passed) still reads `None`/`None` -- "no reading," never a
    fabricated `CONTENT`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            transition = await motivation_repo.create_motivation_event(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                phase=Phase.TRADE_DEADLINE,
                event_type="ladder_transition",
                axis=MotivationAxis.LOYALTY,
                delta=0,
                note="content->trade_requested",
                from_state=RequestState.CONTENT,
                to_state=RequestState.TRADE_REQUESTED,
            )
            assert transition.from_state is RequestState.CONTENT
            assert transition.to_state is RequestState.TRADE_REQUESTED

            history = await motivation_repo.list_motivation_events_for_player(pool, player_id)
            reloaded = next(e for e in history if e.id == transition.id)
            assert reloaded.from_state is RequestState.CONTENT
            assert reloaded.to_state is RequestState.TRADE_REQUESTED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
