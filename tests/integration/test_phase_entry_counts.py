"""`league_repo.count_phase_entries` and the two counters derived from it
(phase-5-stage-e-advance.md P5-99.d.1), proved over real `phase_history` rows.

Driven by `phase_service.advance` directly, not by a press: `season_service` has
no pointer-moving function yet, and the counters are a fact about the table, not
about who wrote it. Each entry is counted BEFORE it is written, the way a press
will count it under the row lock, and the Block-path reading (`count - 1` after
the entry) must land on the same two numbers.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.phase.service import advance, force_advance
from dba2.services import season_service

_SEASON = 2026
_ACTOR = 7

# The whole in-season walk, entry by entry, from SETUP to WAIVERS: nineteen
# entries, nine of them cycle-bearing. Written out rather than derived from the
# graph so the walk is an independent statement of the expected order.
_WALK = [
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.AWARDS,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.POST_DRAFT_TRADES,
    Phase.PROGRESSION,
    Phase.OPTION_DECISIONS,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]


async def _make_league(pool) -> int:
    return await pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             current_phase, commissioner_user_id, rng_seed)
        VALUES ($1, 'Phase Entry Counts Test League', $2, $2, 'SETUP', 1, 42)
        RETURNING id
        """,
        random.randint(10_000_000, 2_000_000_000),
        _SEASON,
    )


async def _counts(pool, league_id: int) -> tuple[int, int]:
    return (
        await league_repo.count_phase_entries(pool, league_id, _SEASON),
        await league_repo.count_phase_entries(
            pool, league_id, _SEASON, season_service.CYCLE_BEARING_PHASES
        ),
    )


async def _walk(
    pool,
    league_id: int,
    *,
    skipped: frozenset[Phase] = frozenset(),
    forced: frozenset[Phase] = frozenset(),
):
    """Enter each phase of the walk in turn, recording `(entered, entry_index,
    cycle_ordinal)` as an Entry press would derive them (counted before the
    write) and asserting the Block-path reading after the write agrees.
    Phases in `skipped` are never entered; those in `forced` are entered with
    `force_advance` instead of `advance`."""
    entries: list[tuple[Phase, int, int | None]] = []
    for target in _WALK:
        if target in skipped:
            continue
        counted_all, counted_cb = await _counts(pool, league_id)
        if target in forced:
            await force_advance(pool, league_id, target, _ACTOR, reason="skip for the test")
        else:
            await advance(pool, league_id, target, _ACTOR)
        entry_index = season_service.derive_entry_index(counted_all, entering=True)
        cycle_ordinal = season_service.derive_cycle_ordinal(target, counted_cb, entering=True)

        after_all, after_cb = await _counts(pool, league_id)
        assert after_all == counted_all + 1
        assert after_cb == counted_cb + (1 if target in season_service.CYCLE_BEARING_PHASES else 0)
        assert season_service.derive_entry_index(after_all, entering=False) == entry_index
        assert season_service.derive_cycle_ordinal(target, after_cb, entering=False) == (
            cycle_ordinal
        )
        entries.append((target, entry_index, cycle_ordinal))
    return entries


@pytest.mark.asyncio
async def test_a_full_walk_of_direct_advances_yields_ordinals_0_to_8_and_entry_indices_0_to_18():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            entries = await _walk(pool, league_id)

            assert [e[0] for e in entries] == _WALK
            assert [e[1] for e in entries] == list(range(19))
            ordinals = [e[2] for e in entries if e[2] is not None]
            assert ordinals == list(range(9))
            # The ordinal exists at exactly the nine cycle-bearing entries (F15).
            assert {e[0] for e in entries if e[2] is not None} == (
                season_service.CYCLE_BEARING_PHASES
            )
            # ...and the two counters really are different numbers mid-season.
            assert dict((e[0], e[1]) for e in entries)[Phase.RESIGN_WINDOW] == 16
            assert dict((e[0], e[2]) for e in entries)[Phase.RESIGN_WINDOW] == 6
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_force_advance_skipping_cycle_bearing_phases_yields_fewer_cycles_monotone_in_both():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            # REGULAR_SEASON -> REGULAR_SEASON_COMPLETE, jumping the deadline and
            # the post-deadline phase: two cycle-bearing entries never happen.
            entries = await _walk(
                pool,
                league_id,
                skipped=frozenset({Phase.TRADE_DEADLINE, Phase.POST_DEADLINE}),
                forced=frozenset({Phase.REGULAR_SEASON_COMPLETE}),
            )

            entered = [e[0] for e in entries]
            assert Phase.TRADE_DEADLINE not in entered
            assert Phase.POST_DEADLINE not in entered
            indices = [e[1] for e in entries]
            ordinals = [e[2] for e in entries if e[2] is not None]
            assert len(entries) == 17
            assert indices == list(range(17))
            assert ordinals == list(range(7))  # fewer than nine, and gap-free
            assert all(a < b for a, b in zip(indices, indices[1:], strict=False))
            assert all(a < b for a, b in zip(ordinals, ordinals[1:], strict=False))
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_count_phase_entries_is_scoped_to_one_league_and_one_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            await advance(pool, league_id, Phase.PRESEASON, _ACTOR)
            await advance(pool, league_id, Phase.REGULAR_SEASON, _ACTOR)
            await advance(pool, other_league_id, Phase.PRESEASON, _ACTOR)
            # A row written under another season is not this season's entry.
            await league_repo.record_phase_transition(
                pool,
                league_id=league_id,
                season=_SEASON + 1,
                from_phase=Phase.WAIVERS,
                to_phase=Phase.PRESEASON,
                actor_user_id=_ACTOR,
                forced=False,
                reason=None,
            )

            assert await league_repo.count_phase_entries(pool, league_id, _SEASON) == 2
            assert await league_repo.count_phase_entries(pool, league_id, _SEASON + 1) == 1
            assert await league_repo.count_phase_entries(pool, other_league_id, _SEASON) == 1
            assert await league_repo.count_phase_entries(pool, league_id, _SEASON + 2) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_count_phase_entries_to_phases_filters_and_an_empty_filter_counts_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            for phase in (Phase.PRESEASON, Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE):
                await advance(pool, league_id, phase, _ACTOR)

            assert await league_repo.count_phase_entries(pool, league_id, _SEASON) == 3
            assert (
                await league_repo.count_phase_entries(
                    pool, league_id, _SEASON, {Phase.REGULAR_SEASON}
                )
                == 1
            )
            assert (
                await league_repo.count_phase_entries(
                    pool, league_id, _SEASON, {Phase.PLAYIN, Phase.FINALS}
                )
                == 0
            )
            # `None` means "every entry"; an empty collection means "none of them" --
            # the two must not be conflated.
            assert await league_repo.count_phase_entries(pool, league_id, _SEASON, set()) == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
