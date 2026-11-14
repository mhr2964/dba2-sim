"""Live I/O proof for `data/repositories/ratings_repo.py` — smoke coverage
for the upsert/get paths on all three tables, plus a dedicated check on
`get_for_team`'s dynamically-built `pr.<col>`-prefixed SELECT (the one query
in this module not written as a static string — worth proving it actually
compiles and round-trips against real Postgres, not just trusted by
inspection).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, player_repo, ratings_repo, team_repo
from dba2.data.repositories.ratings_repo import _RATING_COLUMN_NAMES
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingHistoryReason, RatingSource


def _flat_ratings(value: int) -> dict[str, int]:
    return {name: value for name in _RATING_COLUMN_NAMES}


def _distinct_ratings(offset: int = 0) -> dict[str, int]:
    """Every field gets a different value (mod 90, `rating`'s domain is
    0-99) so a mapper that swapped two fields — invisible under
    `_flat_ratings`, where every field holds the same value — would produce
    a detectable mismatch. `offset` shifts the whole set so two calls (e.g.
    insert vs. overwrite) don't coincidentally reuse the same per-field
    values (Phase 0 step-7 critic round, B2 — 7 scripted field-swap
    mutations survived the previous, flat-valued version of this suite)."""
    return {name: (i + offset) % 90 for i, name in enumerate(_RATING_COLUMN_NAMES)}


async def _make_league_and_player(pool, *, team_id: int | None = None) -> tuple[int, int]:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Ratings Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    player = await player_repo.create_player(
        pool,
        league_id=league.id,
        first_name="Rated",
        last_name="Player",
        primary_position=Position.PG,
        height_in=74,
        weight_lb=185,
        wingspan_in=76,
        birth_date=date(2000, 1, 1),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return league.id, player.id


@pytest.mark.asyncio
async def test_validate_ratings_dict_rejects_missing_and_extra_keys():
    from dba2.core.errors import ValidationError

    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_and_player(pool)
        try:
            incomplete = _flat_ratings(50)
            del incomplete["speed"]
            with pytest.raises(ValidationError):
                await ratings_repo.upsert_player_ratings(
                    pool,
                    player_id=player_id,
                    league_id=league_id,
                    source=RatingSource.GENERATED,
                    ratings=incomplete,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_upsert_player_ratings_round_trips_and_overwrites():
    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_and_player(pool)
        try:
            initial_ratings = _distinct_ratings(offset=0)
            created = await ratings_repo.upsert_player_ratings(
                pool,
                player_id=player_id,
                league_id=league_id,
                source=RatingSource.GENERATED,
                ratings=initial_ratings,
                confidence=Decimal("0.75"),
            )
            assert created.source == RatingSource.GENERATED
            for name, value in initial_ratings.items():
                assert getattr(created, name) == value, name

            fetched = await ratings_repo.get_player_ratings(pool, player_id)
            assert fetched is not None
            assert fetched.confidence == Decimal("0.75")
            for name, value in initial_ratings.items():
                assert getattr(fetched, name) == value, name

            overwrite_ratings = _distinct_ratings(offset=30)
            updated = await ratings_repo.upsert_player_ratings(
                pool,
                player_id=player_id,
                league_id=league_id,
                source=RatingSource.PROGRESSION,
                ratings=overwrite_ratings,
                confidence=Decimal("1.00"),
            )
            assert updated.source == RatingSource.PROGRESSION
            for name, value in overwrite_ratings.items():
                assert getattr(updated, name) == value, name

            assert await ratings_repo.get_player_ratings(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_for_team_and_get_for_league_bulk_loaders():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Bulk Loader Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        team = await team_repo.create_team(
            pool,
            league_id=league.id,
            code="BLK",
            name="Bulkers",
            city="Bulktown",
            conference="West",
            division="Northwest",
            home_market_size=1,
        )
        try:
            on_team = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="On",
                last_name="Team",
                primary_position=Position.PF,
                height_in=81,
                weight_lb=230,
                wingspan_in=84,
                birth_date=date(1997, 2, 2),
                roster_status=RosterStatus.ACTIVE,
                overall_cached=77,
                overall_computed_at=datetime.now(UTC),
                team_id=team.id,
            )
            off_team = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="Off",
                last_name="Team",
                primary_position=Position.C,
                height_in=83,
                weight_lb=245,
                wingspan_in=87,
                birth_date=date(1996, 4, 4),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=72,
                overall_computed_at=datetime.now(UTC),
            )
            await ratings_repo.upsert_player_ratings(
                pool,
                player_id=on_team.id,
                league_id=league.id,
                source=RatingSource.GENERATED,
                ratings=_flat_ratings(60),
            )
            await ratings_repo.upsert_player_ratings(
                pool,
                player_id=off_team.id,
                league_id=league.id,
                source=RatingSource.GENERATED,
                ratings=_flat_ratings(40),
            )

            team_ratings = await ratings_repo.get_for_team(pool, team.id)
            assert [r.player_id for r in team_ratings] == [on_team.id]
            assert team_ratings[0].speed == 60

            league_ratings = await ratings_repo.get_for_league(pool, league.id)
            assert {r.player_id for r in league_ratings} == {on_team.id, off_team.id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_tendencies_for_league_bulk_loader():
    """Mirrors `get_for_league`'s bulk-loader test (phase-1-simulation-engine.md
    §8.2) — a league-scoped tendencies snapshot, not a per-player query."""
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Tendencies Bulk Loader League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            player_a = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="Tend",
                last_name="A",
                primary_position=Position.SF,
                height_in=79,
                weight_lb=215,
                wingspan_in=82,
                birth_date=date(1998, 3, 3),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=68,
                overall_computed_at=datetime.now(UTC),
            )
            player_b = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="Tend",
                last_name="B",
                primary_position=Position.C,
                height_in=84,
                weight_lb=250,
                wingspan_in=88,
                birth_date=date(1995, 6, 6),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=71,
                overall_computed_at=datetime.now(UTC),
            )
            other_league = await league_repo.create_league(
                pool,
                discord_guild_id=random.randint(10_000_000, 2_000_000_000),
                name="Other Tendencies League",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=2,
            )
            player_other = await player_repo.create_player(
                pool,
                league_id=other_league.id,
                first_name="Tend",
                last_name="Other",
                primary_position=Position.PG,
                height_in=73,
                weight_lb=180,
                wingspan_in=75,
                birth_date=date(2001, 7, 7),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=65,
                overall_computed_at=datetime.now(UTC),
            )
            try:
                freqs = dict(
                    shot_freq_rim=25, shot_freq_mid=25, shot_freq_three=25, shot_freq_post=25,
                    drive_freq=1, pullup_vs_catch=2, iso_freq=3, pnr_ball_freq=4,
                    pnr_screen_freq=5, transition_freq=6, pass_freq=7, spot_up_freq=8,
                    cut_freq=9, crash_oreb_freq=10, help_gamble_freq=11, steal_gamble_freq=12,
                    foul_freq=13,
                )
                await ratings_repo.upsert_player_tendencies(
                    pool, player_id=player_a.id, league_id=league.id, **freqs
                )
                await ratings_repo.upsert_player_tendencies(
                    pool, player_id=player_b.id, league_id=league.id, **freqs
                )
                await ratings_repo.upsert_player_tendencies(
                    pool, player_id=player_other.id, league_id=other_league.id, **freqs
                )

                league_tendencies = await ratings_repo.get_tendencies_for_league(pool, league.id)
                assert {t.player_id for t in league_tendencies} == {player_a.id, player_b.id}
            finally:
                await pool.execute("DELETE FROM leagues WHERE id = $1", other_league.id)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_ratings_history_snapshot_and_list():
    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_and_player(pool)
        try:
            snap = await ratings_repo.create_ratings_history_snapshot(
                pool,
                player_id=player_id,
                season=2026,
                league_id=league_id,
                reason=RatingHistoryReason.MANUAL,
                ratings=_flat_ratings(55),
            )
            assert snap.reason == RatingHistoryReason.MANUAL
            assert snap.speed == 55

            history = await ratings_repo.get_ratings_history_for_player(pool, player_id)
            assert len(history) == 1
            assert history[0].season == 2026
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_player_tendencies_upsert_on_conflict():
    """Every field distinct (within the `shot_freq_*` group's sum-to-100
    constraint) so a mapper that swapped two fields — e.g. `cut_freq`/
    `crash_oreb_freq`, both `int` and both 10 in the previous version of
    this fixture — would fail (Phase 0 step-7 critic round, B2)."""
    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_and_player(pool)
        try:
            freqs = dict(
                shot_freq_rim=40, shot_freq_mid=30, shot_freq_three=20, shot_freq_post=10,
                drive_freq=11, pullup_vs_catch=12, iso_freq=13, pnr_ball_freq=14,
                pnr_screen_freq=15, transition_freq=16, pass_freq=17, spot_up_freq=18,
                cut_freq=19, crash_oreb_freq=21, help_gamble_freq=22, steal_gamble_freq=23,
                foul_freq=24,
            )
            created = await ratings_repo.upsert_player_tendencies(
                pool, player_id=player_id, league_id=league_id, **freqs
            )
            for name, value in freqs.items():
                assert getattr(created, name) == value, name

            freqs2 = dict(
                shot_freq_rim=10, shot_freq_mid=20, shot_freq_three=30, shot_freq_post=40,
                drive_freq=31, pullup_vs_catch=32, iso_freq=33, pnr_ball_freq=34,
                pnr_screen_freq=35, transition_freq=36, pass_freq=37, spot_up_freq=38,
                cut_freq=39, crash_oreb_freq=41, help_gamble_freq=42, steal_gamble_freq=43,
                foul_freq=44,
            )
            updated = await ratings_repo.upsert_player_tendencies(
                pool, player_id=player_id, league_id=league_id, **freqs2
            )
            for name, value in freqs2.items():
                assert getattr(updated, name) == value, name

            fetched = await ratings_repo.get_player_tendencies(pool, player_id)
            for name, value in freqs2.items():
                assert getattr(fetched, name) == value, name

            assert await ratings_repo.get_player_tendencies(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
