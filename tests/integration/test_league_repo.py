"""Live I/O proof for `data/repositories/league_repo.py` — smoke coverage
for every function, plus the JSONB `phase_data` marshal/unmarshal round
trip (asyncpg has no codec registered for it, so this module hand-rolls
`json.dumps`/`json.loads` at the boundary — worth proving against real
Postgres, not just trusted by inspection).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase


def _guild_id() -> int:
    return random.randint(10_000_000, 2_000_000_000)


@pytest.mark.asyncio
async def test_map_league_round_trips_every_field_distinctly():
    """Every `League` field gets a distinct value so a mapper that swapped
    two same-typed fields (e.g. `start_season_year`/`current_season`, both
    `int`) would fail this test where a fixture reusing 2026 for both could
    not (Phase 0 step-7 critic round, B2 — 7 scripted field-swap mutations
    survived the previous version of this suite)."""
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Distinct Field League",
            start_season_year=2019,
            current_season=2026,
            commissioner_user_id=555,
            rng_seed=999,
            current_phase=Phase.SETUP,
            phase_data={"distinct": "marker"},
        )
        try:
            fetched = await league_repo.get_league(pool, league.id)
            assert fetched.name == "Distinct Field League"
            assert fetched.start_season_year == 2019
            assert fetched.current_season == 2026
            assert fetched.commissioner_user_id == 555
            assert fetched.rng_seed == 999
            assert fetched.phase_data == {"distinct": "marker"}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_get_league_round_trips_including_phase_data():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="League Repo Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=42,
            phase_data={"note": "smoke test"},
        )
        try:
            assert league.current_phase == Phase.SETUP
            assert league.phase_data == {"note": "smoke test"}

            fetched = await league_repo.get_league(pool, league.id)
            assert fetched is not None
            assert fetched.name == "League Repo Test League"
            assert fetched.phase_data == {"note": "smoke test"}

            by_guild = await league_repo.get_league_by_guild_id(pool, league.discord_guild_id)
            assert by_guild is not None
            assert by_guild.id == league.id

            assert await league_repo.get_league(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_archive_league_sets_archived_at():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Archive Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            assert league.archived_at is None
            await league_repo.archive_league(pool, league.id)
            fetched = await league_repo.get_league(pool, league.id)
            assert fetched.archived_at is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_phase_state_and_transition_history():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Phase State Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    state = await league_repo.get_phase_state_for_update(conn, league.id)
                    assert state == (Phase.SETUP, 2026)

            before = await league_repo.get_league(pool, league.id)

            await league_repo.record_phase_transition(
                pool,
                league_id=league.id,
                season=2026,
                from_phase=Phase.SETUP,
                to_phase=Phase.PRESEASON,
                actor_user_id=7,
                forced=False,
                reason=None,
            )
            await league_repo.set_current_phase(pool, league.id, Phase.PRESEASON)

            fetched = await league_repo.get_league(pool, league.id)
            # phase_entered_at must actually advance — a write that dropped
            # this column would still pass every other assertion here
            # (Phase 0 step-7 critic round, B2).
            assert fetched.phase_entered_at > before.phase_entered_at
            assert fetched.current_phase == Phase.PRESEASON

            history = await league_repo.list_phase_history(pool, league.id)
            assert len(history) == 1
            assert history[0].from_phase == Phase.SETUP
            assert history[0].to_phase == Phase.PRESEASON
            assert history[0].forced is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_cap_settings_create_and_get():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Cap Settings Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            created = await league_repo.create_cap_settings(
                pool,
                league_id=league.id,
                season=2026,
                salary_cap=140_000_000,
                luxury_tax=170_000_000,
                apron_1=178_000_000,
                apron_2=189_000_000,
                max_pct_0_6=25,
                max_pct_7_9=30,
                max_pct_10_plus=36,
                supermax_pct=38,
                min_salary=1_100_000,
                mle_amount=12_800_000,
                bi_annual_amount=4_500_000,
                raise_pct_bird=8,
                raise_pct_non_bird=5,
                roster_min=14,
                roster_max=15,
            )
            # Every same-typed field distinct so a mapper field swap would
            # fail here (Phase 0 step-7 critic round, B2).
            assert created.salary_cap == 140_000_000
            assert created.luxury_tax == 170_000_000
            assert created.apron_1 == 178_000_000
            assert created.apron_2 == 189_000_000
            assert created.max_pct_0_6 == 25
            assert created.max_pct_7_9 == 30
            assert created.max_pct_10_plus == 36
            assert created.supermax_pct == 38
            assert created.min_salary == 1_100_000
            assert created.mle_amount == 12_800_000
            assert created.bi_annual_amount == 4_500_000
            assert created.raise_pct_bird == 8
            assert created.raise_pct_non_bird == 5
            assert created.roster_min == 14
            assert created.roster_max == 15

            fetched = await league_repo.get_cap_settings(pool, league.id, 2026)
            assert fetched is not None
            assert fetched.roster_max == 15

            assert await league_repo.get_cap_settings(pool, league.id, 2099) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_league_state_for_claim_check_reads_phase_claim_and_commissioner():
    """The trade-offer review surface's one league-level guard read
    (docs/design/phase-5-trade-offer-review-surface.md §6.2). Every field is
    distinct from its same-typed neighbour so a swapped column would fail."""
    from dba2.data.db import transaction

    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Claim State League",
            start_season_year=2019,
            current_season=2026,
            commissioner_user_id=555,
            rng_seed=1,
            current_phase=Phase.TRADE_DEADLINE,
        )
        try:
            async with transaction(pool) as conn:
                state = await league_repo.get_league_state_for_claim_check(conn, league.id)
            assert state.current_phase is Phase.TRADE_DEADLINE
            assert state.current_season == 2026
            assert state.commissioner_user_id == 555
            assert state.advance_claimed_at is None and state.advance_claimed_by is None

            await pool.execute(
                "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 77 "
                "WHERE id = $1",
                league.id,
            )
            async with transaction(pool) as conn:
                claimed = await league_repo.get_league_state_for_claim_check(conn, league.id)
            assert claimed.advance_claimed_at is not None
            assert claimed.advance_claimed_by == 77

            async with transaction(pool) as conn:
                assert await league_repo.get_league_state_for_claim_check(conn, 987_654_321) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_league_state_for_claim_check_takes_a_share_lock_that_blocks_a_press():
    """`FOR SHARE` must conflict with the writer P5-90 names -- a press's
    `UPDATE leagues SET advance_claimed_at` -- or the claim read is a TOCTOU.
    A second `FOR SHARE` reader must NOT be blocked (that is the whole reason
    it is not `FOR UPDATE`)."""
    import asyncio

    from dba2.data.db import transaction

    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Claim Lock League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
            current_phase=Phase.REGULAR_SEASON,
        )
        try:
            reader_holds, release = asyncio.Event(), asyncio.Event()

            async def reader():
                async with transaction(pool) as conn:
                    await league_repo.get_league_state_for_claim_check(conn, league.id)
                    reader_holds.set()
                    await release.wait()

            async def press():
                await pool.execute(
                    "UPDATE leagues SET advance_claimed_at = NOW() WHERE id = $1", league.id
                )

            held = asyncio.create_task(reader())
            await reader_holds.wait()

            async with transaction(pool) as conn:  # a second share reader is not blocked
                await asyncio.wait_for(
                    league_repo.get_league_state_for_claim_check(conn, league.id), timeout=2
                )

            writer = asyncio.create_task(press())
            done, _ = await asyncio.wait({writer}, timeout=0.5)
            assert not done, "the press's UPDATE was not blocked by the share lock"
            release.set()
            await held
            await asyncio.wait_for(writer, timeout=5)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()
