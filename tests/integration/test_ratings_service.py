"""Live I/O proof for `services/ratings_service.py` -- Decision 12's
overall-cache consistency requirement, quoted in full from §7.4: "a test
recomputes every player's overall and assert[s] equality with the stored
value." Genuinely recompute-and-compare against a fresh
`sim.ratings.compute_overall` call, not just "the write succeeded."

Also covers `derive_tendencies_for_player`/`derive_tendencies_for_league`
(docs/design/phase-1-step-7-calibration.md §11.3, §12 step 5): every
seeded player gets a `player_tendencies` row that survives the DB CHECK
(`shot_freq_rim + shot_freq_mid + shot_freq_three + shot_freq_post = 100`),
and the persisted values match a fresh `sim.derivation.derive_tendencies`
call, the same "genuinely recompute and compare" discipline the overall
tests above already use.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module that touches
real Postgres.
"""
from __future__ import annotations

import dataclasses
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from dba2.core.errors import NotFoundError
from dba2.core.rng import rng_for
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, player_repo, ratings_repo, team_repo
from dba2.data.repositories.ratings_repo import _RATING_COLUMN_NAMES
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import PlayerRatings, RatingSource
from dba2.services import ratings_service
from dba2.sim.derivation import POSITION_RATING_OFFSETS, RATING_GROUPS, derive_tendencies
from dba2.sim.ratings import (
    _HIDDEN_FROM_OVERALL,
    _NON_RATING_FIELDS,
    _POSITION_GROUP_WEIGHTS_PCT,
    _POSITION_OVERALL_ARTIFACT,
    _PUBLIC_RATING_FIELDS,
    compute_overall,
)


def _distinct_ratings(offset: int = 0) -> dict[str, int]:
    """Every field a different value (mod 90, `rating`'s domain is 0-99) --
    same pattern as `test_ratings_repo.py::_distinct_ratings`, so the
    per-player mean this test hand-computes is actually sensitive to every
    field rather than coincidentally correct under a flat fixture."""
    return {name: (i + offset) % 90 for i, name in enumerate(_RATING_COLUMN_NAMES)}


async def _make_league_team_and_player(pool, *, code: str, offset: int) -> tuple[int, int]:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Ratings Service Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    team = await team_repo.create_team(
        pool,
        league_id=league.id,
        code=code,
        name="Recompute Testers",
        city="Testville",
        conference="East",
        division="Atlantic",
        home_market_size=1,
    )
    player = await player_repo.create_player(
        pool,
        league_id=league.id,
        first_name="Recompute",
        last_name="Target",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=210,
        wingspan_in=82,
        birth_date=date(1999, 6, 15),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=1,
        overall_computed_at=datetime.now(UTC),
        team_id=team.id,
    )
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league.id,
        source=RatingSource.GENERATED,
        ratings=_distinct_ratings(offset=offset),
    )
    return league.id, player.id


@pytest.mark.asyncio
async def test_recompute_overall_raises_not_found_for_player_with_no_ratings_row():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="No Ratings League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="No",
                last_name="Ratings",
                primary_position=Position.C,
                height_in=84,
                weight_lb=250,
                wingspan_in=88,
                birth_date=date(1998, 1, 1),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=1,
                overall_computed_at=datetime.now(UTC),
            )
            with pytest.raises(NotFoundError):
                await ratings_service.recompute_overall(pool, player.id)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_recompute_overall_matches_a_fresh_compute_overall_call():
    """The headline consistency check for a single player: what got
    persisted into `players.overall_cached` equals `compute_overall`
    applied fresh to the same ratings row, not just "some int got
    written."""
    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_team_and_player(pool, code="RS1", offset=0)
        try:
            written = await ratings_service.recompute_overall(pool, player_id)

            fetched_player = await player_repo.get_player(pool, player_id)
            assert fetched_player is not None
            assert fetched_player.overall_cached == written

            ratings = await ratings_repo.get_player_ratings(pool, player_id)
            assert ratings is not None
            assert fetched_player.overall_cached == compute_overall(
                ratings, fetched_player.primary_position
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_recompute_overall_for_league_matches_fresh_compute_for_every_player():
    """Decision 12's actual consistency test, at league scope: seed several
    players with distinct, known `player_ratings` rows and distinct
    positions (Phase 1 step 9: `compute_overall` is position-weighted, so a
    fixture that only ever exercises one position couldn't tell a broken
    per-position weight table from a correct one), run
    `recompute_overall_for_league`, then genuinely recompute
    `compute_overall` fresh from each player's own ratings and position and
    assert equality against what's stored -- not just that the write
    succeeded."""
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="League-Wide Recompute Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league.id,
                code="LWR",
                name="League Wide Recomputers",
                city="Testburg",
                conference="West",
                division="Pacific",
                home_market_size=1,
            )
            positions = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)
            player_ids: list[int] = []
            for i in range(5):
                player = await player_repo.create_player(
                    pool,
                    league_id=league.id,
                    first_name=f"Player{i}",
                    last_name="Distinct",
                    primary_position=positions[i],
                    height_in=70 + i,
                    weight_lb=170 + i * 5,
                    wingspan_in=72 + i,
                    birth_date=date(1995 + i, 3, 10),
                    roster_status=RosterStatus.ACTIVE,
                    overall_cached=1,
                    overall_computed_at=datetime.now(UTC),
                    team_id=team.id,
                )
                await ratings_repo.upsert_player_ratings(
                    pool,
                    player_id=player.id,
                    league_id=league.id,
                    source=RatingSource.GENERATED,
                    ratings=_distinct_ratings(offset=i * 7),
                )
                player_ids.append(player.id)

            await ratings_service.recompute_overall_for_league(pool, league.id)

            for player_id in player_ids:
                fetched_player = await player_repo.get_player(pool, player_id)
                assert fetched_player is not None
                ratings = await ratings_repo.get_player_ratings(pool, player_id)
                assert ratings is not None
                expected = compute_overall(ratings, fetched_player.primary_position)
                assert fetched_player.overall_cached == expected, (
                    f"player {player_id}: stored overall_cached="
                    f"{fetched_player.overall_cached}, freshly recomputed={expected}"
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_derive_tendencies_for_player_raises_not_found_for_player_with_no_ratings_row():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="No Ratings Tendencies League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            player = await player_repo.create_player(
                pool,
                league_id=league.id,
                first_name="No",
                last_name="Ratings",
                primary_position=Position.C,
                height_in=84,
                weight_lb=250,
                wingspan_in=88,
                birth_date=date(1998, 1, 1),
                roster_status=RosterStatus.FREE_AGENT,
                overall_cached=1,
                overall_computed_at=datetime.now(UTC),
            )
            with pytest.raises(NotFoundError):
                await ratings_service.derive_tendencies_for_player(pool, player.id)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_derive_tendencies_for_player_matches_a_fresh_derive_tendencies_call():
    """The same "genuinely recompute and compare" discipline as
    `test_recompute_overall_matches_a_fresh_compute_overall_call`: what got
    persisted equals `sim.derivation.derive_tendencies` applied fresh to
    the same ratings row and position, with `rng=None` (deterministic) on
    both sides, plus a live DB-CHECK proof (the shot-frequency sum)."""
    pool = await get_pool()
    try:
        league_id, player_id = await _make_league_team_and_player(pool, code="RT1", offset=0)
        try:
            written = await ratings_service.derive_tendencies_for_player(pool, player_id)

            ratings = await ratings_repo.get_player_ratings(pool, player_id)
            assert ratings is not None
            player = await player_repo.get_player(pool, player_id)
            assert player is not None

            expected = derive_tendencies(ratings, player.primary_position, rng=None)
            for field_name, expected_value in expected.items():
                assert getattr(written, field_name) == expected_value, field_name

            fetched = await ratings_repo.get_player_tendencies(pool, player_id)
            assert fetched is not None
            assert (
                fetched.shot_freq_rim
                + fetched.shot_freq_mid
                + fetched.shot_freq_three
                + fetched.shot_freq_post
                == 100
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_derive_tendencies_for_league_persists_a_row_for_every_player():
    """Decision 12-shaped league-scope test, matching
    `test_recompute_overall_for_league_matches_fresh_compute_for_every_
    player` above: several players get distinct ratings, `derive_
    tendencies_for_league` runs once, and every resulting row both
    survives the DB CHECK and matches a fresh `derive_tendencies` call
    seeded with the same `rng_for(league.rng_seed, "tendencies",
    player_id)` stream Decision P1-19 item 2 names."""
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="League-Wide Tendencies Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=123,
        )
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league.id,
                code="LWT",
                name="League Wide Tendencies",
                city="Testburg",
                conference="West",
                division="Pacific",
                home_market_size=1,
            )
            positions = (Position.PG, Position.SF, Position.C, Position.SG, Position.PF)
            player_ids: list[int] = []
            for i, position in enumerate(positions):
                player = await player_repo.create_player(
                    pool,
                    league_id=league.id,
                    first_name=f"Player{i}",
                    last_name="Distinct",
                    primary_position=position,
                    height_in=74 + i,
                    weight_lb=190 + i * 5,
                    wingspan_in=76 + i,
                    birth_date=date(1996 + i, 4, 12),
                    roster_status=RosterStatus.ACTIVE,
                    overall_cached=1,
                    overall_computed_at=datetime.now(UTC),
                    team_id=team.id,
                )
                await ratings_repo.upsert_player_ratings(
                    pool,
                    player_id=player.id,
                    league_id=league.id,
                    source=RatingSource.GENERATED,
                    ratings=_distinct_ratings(offset=i * 11),
                )
                player_ids.append(player.id)

            rows_written = await ratings_service.derive_tendencies_for_league(pool, league.id)
            assert rows_written == len(player_ids)

            for player_id, position in zip(player_ids, positions, strict=True):
                ratings = await ratings_repo.get_player_ratings(pool, player_id)
                assert ratings is not None
                fetched = await ratings_repo.get_player_tendencies(pool, player_id)
                assert fetched is not None
                assert (
                    fetched.shot_freq_rim
                    + fetched.shot_freq_mid
                    + fetched.shot_freq_three
                    + fetched.shot_freq_post
                    == 100
                )
                rng = rng_for(league.rng_seed, "tendencies", player_id)
                expected = derive_tendencies(ratings, position, rng=rng)
                for field_name, expected_value in expected.items():
                    assert getattr(fetched, field_name) == expected_value, (
                        f"player {player_id} field {field_name}"
                    )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


def _build_player_ratings(ratings_dict: dict[str, int]) -> PlayerRatings:
    return PlayerRatings(
        player_id=1,
        league_id=1,
        source=RatingSource.MANUAL,
        derived_from_season=None,
        confidence=Decimal("1.00"),
        updated_at=datetime.now(UTC),
        **ratings_dict,
    )


def test_non_rating_fields_exclusion_set_is_exactly_the_eight_named_columns():
    """Phase 0 step-9 critic round, B2, renamed in Phase 1 step 9 rather
    than dropped: this deliberately still pins the *original* 8-name
    `_NON_RATING_FIELDS` set unchanged, not a 10-name superset -- Phase 1
    step 9 added a position-weighted composite that additionally excludes
    `potential`/`potential_variance` from `overall_cached`, but those 2
    fields are still real 0-99 ratings that `_NON_RATING_FIELDS` (the
    *generation* partition `sim.derivation.RATING_FIELD_ORDER`'s 55-field
    shape and the step-7 calibration doc's variance model both depend on)
    must keep including in its 55. The new hidden-from-the-composite
    exclusion is `sim.ratings._HIDDEN_FROM_OVERALL`, pinned separately by
    `test_hidden_from_overall_is_exactly_potential_and_potential_variance`
    below -- if `_NON_RATING_FIELDS` ever gains or loses a name (e.g.
    `motor` wrongly added), this still fails immediately regardless of
    what any particular ratings fixture's mean happens to round to."""
    assert _NON_RATING_FIELDS == frozenset(
        {
            "player_id",
            "league_id",
            "source",
            "derived_from_season",
            "confidence",
            "updated_at",
            "peak_age_start",
            "peak_age_end",
        }
    )


def test_hidden_from_overall_is_exactly_potential_and_potential_variance():
    """Phase 1 step 9's new exclusion, companion to the pin above:
    `compute_overall`'s weighted set additionally drops exactly these 2
    fields (critic round 14: both are 0-99 ratings, but §4.3.2 marks them
    "(hidden)" and the placeholder's plain mean leaked them through the
    sortable, publicly displayed `overall_cached` column)."""
    assert _HIDDEN_FROM_OVERALL == frozenset({"potential", "potential_variance"})


def test_compute_overall_weights_exactly_the_53_public_rating_columns():
    """Renamed from `test_compute_overall_averages_exactly_the_55_named_
    rating_columns`: the weighting map deliberately changed in Phase 1
    step 9 (position-weighted, not a plain mean, and now over 53 fields
    instead of 55 -- `potential`/`potential_variance` dropped per the test
    above), so this is pinning a different, smaller set on purpose, not a
    weakened guard. Confirms the *resulting* weighted set (`PlayerRatings`
    fields minus `_NON_RATING_FIELDS` minus `_HIDDEN_FROM_OVERALL`) is
    exactly `sim.ratings._PUBLIC_RATING_FIELDS`, for every position."""
    all_fields = {f.name for f in dataclasses.fields(PlayerRatings)}
    weighted = all_fields - _NON_RATING_FIELDS - _HIDDEN_FROM_OVERALL
    expected = set(_RATING_COLUMN_NAMES) - {"peak_age_start", "peak_age_end"} - _HIDDEN_FROM_OVERALL
    assert weighted == expected
    assert weighted == set(_PUBLIC_RATING_FIELDS)
    assert len(weighted) == 53


def test_compute_overall_matches_a_hand_computed_weighted_mean_of_distinct_ratings():
    """Formula-level sanity check, no DB needed: `compute_overall`'s output
    matches a weighted, re-centered mean computed independently in this
    test (not by calling `compute_overall` or its internal
    `_OVERALL_WEIGHTS`/`_POSITION_OVERALL_ARTIFACT` tables -- this
    recomputes the group-share-per-field split itself, via its own loop
    over `sim.ratings`'s declared `_POSITION_GROUP_WEIGHTS_PCT` source
    table and `sim.derivation.RATING_GROUPS`, exercising a genuinely
    different code path than `_build_field_weights`) over the 53 public
    fields of a known, all-distinct ratings dict, for every position --
    proves the right fields are being weighted by the right amounts, not
    just that *some* number comes out. Uses exact `Fraction` arithmetic,
    matching `compute_overall`'s own, so this is a code-path check rather
    than also re-litigating float-vs-Fraction rounding.

    Two deviations from `RATING_GROUPS`/plain-weighted-mean, both critic
    round 21 fixes reproduced independently here rather than imported:
    `post_defense` is scored under TEAM_DEF's percentage, not
    ON_BALL_DEF's (nit 1 -- the offset table types it as an interior/big
    skill), and the weighted sum is re-centered by this position's own
    `POSITION_RATING_OFFSETS`-weighted artifact before clamping (B21-1 --
    without this, equal-talent players score differently purely by
    position)."""
    ratings_dict = _distinct_ratings(offset=0)
    ratings = _build_player_ratings(ratings_dict)

    for position in (Position.PG, Position.SG, Position.SF, Position.PF, Position.C):
        group_pcts = _POSITION_GROUP_WEIGHTS_PCT[position]
        weight_by_field: dict[str, Fraction] = {}
        for group, field_names in RATING_GROUPS.items():
            if group == "ON_BALL_DEF":
                field_names = tuple(f for f in field_names if f != "post_defense")
            elif group == "TEAM_DEF":
                field_names = (*field_names, "post_defense")
            public_fields = [f for f in field_names if f not in _HIDDEN_FROM_OVERALL]
            share = Fraction(group_pcts[group], 100 * len(public_fields))
            for field_name in public_fields:
                weight_by_field[field_name] = share

        weighted_sum = sum(
            (weight_by_field[f] * ratings_dict[f] for f in weight_by_field), Fraction(0)
        )
        artifact = float(
            sum(weight_by_field[f] * POSITION_RATING_OFFSETS[position][f] for f in weight_by_field)
        )
        expected = max(0, min(99, round(weighted_sum - artifact)))
        assert compute_overall(ratings, position) == expected, position


def test_compute_overall_clamps_to_the_rating_domains_0_99_range():
    """The lower clamp (`max(0, ...)`) is still exactly hit by every
    position: an all-0 ratings vector re-centered by this position's own
    `_POSITION_OVERALL_ARTIFACT` (currently positive for all 5 positions)
    always lands below 0. The upper clamp is no longer exactly 99 for
    every position after critic round 21's B21-1 re-centering fix --
    subtracting a positive artifact moves an all-99 fixture's weighted sum
    below 99 -- so the all-99 case is asserted against the same
    re-centered computation instead of a hardcoded 99, while still proving
    the result never exceeds the domain."""
    all_99 = dict.fromkeys(_RATING_COLUMN_NAMES, 99)
    all_99["peak_age_start"] = 26
    all_99["peak_age_end"] = 30
    for position in (Position.PG, Position.SG, Position.SF, Position.PF, Position.C):
        expected = max(0, min(99, round(99 - _POSITION_OVERALL_ARTIFACT[position])))
        result = compute_overall(_build_player_ratings(all_99), position)
        assert result == expected, position
        assert result <= 99

    all_0 = dict.fromkeys(_RATING_COLUMN_NAMES, 0)
    all_0["peak_age_start"] = 24
    all_0["peak_age_end"] = 28
    for position in (Position.PG, Position.SG, Position.SF, Position.PF, Position.C):
        assert compute_overall(_build_player_ratings(all_0), position) == 0
