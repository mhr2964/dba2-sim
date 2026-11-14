"""Live I/O proof for `data/repositories/media_repo.py` -- migration 011,
Stage D step 19's own verify criteria: `float` (never `Decimal`)
`salience` round-tripping, real `JSONB` `slot_bindings` round-tripping,
the pulse-snapshot bulk upsert and prior-cycle read, and the media-writer
get/create pair (including the real `UniqueViolationError` a caller racing
the same `(league_id, team_id, persona)` triple actually hits).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as `test_smoke.py`/`test_head_to_head_repo.py`.
"""
from __future__ import annotations

import dataclasses
import random

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import media_repo, team_repo
from dba2.domain.media import MediaPulseSnapshot, StoryKind
from dba2.domain.phase import Phase
from dba2.services import league_service

_SEASON = 2026


def _guild_id() -> int:
    return random.randint(10_000_000, 2_000_000_000)


async def _make_league(pool) -> int:
    league = await league_service.create_league(
        pool,
        discord_guild_id=_guild_id(),
        name="Media Repo Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


# ---------------------------------------------------------------------------
# media_stories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_story_round_trips_jsonb_slot_bindings_and_float_salience():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            story = await media_repo.create_media_story(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=3,
                story_kind=StoryKind.STREAK,
                voice="beat_writer",
                template_id="fallback_streak",
                slot_bindings={"team_name": "Portland Trail Blazers", "streak_length": "9"},
                rendered_text="Portland Trail Blazers have won 9 in a row.",
                salience=0.732,
                subject_player_id=None,
                subject_team_id=team_id,
                writer_id=None,
            )

            assert story.id is not None
            assert story.league_id == league_id
            assert story.season == _SEASON
            assert story.phase is Phase.REGULAR_SEASON
            assert story.cycle_index == 3
            assert story.story_kind is StoryKind.STREAK
            assert story.voice == "beat_writer"
            assert story.template_id == "fallback_streak"
            assert story.slot_bindings == {
                "team_name": "Portland Trail Blazers",
                "streak_length": "9",
            }
            assert story.rendered_text == "Portland Trail Blazers have won 9 in a row."
            # The float trap this repo has hit before (HANDOFF's recurring
            # bug class): `type(...) is float`, not `==`, because
            # `Decimal("0.732") == 0.732` can be True and would pass even if
            # the column silently came back as `Decimal`.
            assert type(story.salience) is float
            assert story.salience == 0.732
            assert story.subject_player_id is None
            assert story.subject_team_id == team_id
            assert story.writer_id is None
            assert story.created_at is not None

            fetched = await media_repo.get_media_story(pool, story.id)
            assert fetched == story
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_posted_at_starts_null_and_every_read_path_carries_it():
    """Migration 020's `posted_at` ships with its whole read path and no writer yet
    (phase-5-stage-e-advance.md R13): a story is born unposted, and once the column
    holds a value BOTH the by-id read and the list read return it. The value is set
    with raw SQL because the writer is a later commit's."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            story = await media_repo.create_media_story(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=1,
                story_kind=StoryKind.STREAK,
                voice="beat_writer",
                template_id="fallback_streak",
                slot_bindings={},
                rendered_text="A story.",
                salience=0.5,
                subject_player_id=None,
                subject_team_id=None,
                writer_id=None,
            )
            assert story.posted_at is None

            await pool.execute("UPDATE media_stories SET posted_at = NOW() WHERE id = $1", story.id)

            fetched = await media_repo.get_media_story(pool, story.id)
            assert fetched is not None and fetched.posted_at is not None
            (listed,) = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            assert listed.posted_at == fetched.posted_at
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_media_stories_for_league_season_filters_by_cycle_index():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            for cycle_index in (0, 0, 1):
                await media_repo.create_media_story(
                    pool,
                    league_id=league_id,
                    season=_SEASON,
                    phase=Phase.REGULAR_SEASON,
                    cycle_index=cycle_index,
                    story_kind=StoryKind.STREAK,
                    voice="beat_writer",
                    template_id="fallback_streak",
                    slot_bindings={},
                    rendered_text="A streak story.",
                    salience=0.5,
                    subject_player_id=None,
                    subject_team_id=team_id,
                    writer_id=None,
                )

            whole_season = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON
            )
            assert len(whole_season) == 3

            cycle_0_only = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert len(cycle_0_only) == 2
            assert all(s.cycle_index == 0 for s in cycle_0_only)

            cycle_1_only = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            assert len(cycle_1_only) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_media_story_allows_two_subjects_drawing_the_same_template_in_one_cycle():
    """Decision P4-32's own closing note, proven at the repo level too (the
    migration-level proof lives in `tests/integration/test_migrations_
    roundtrip.py`) -- `create_media_story` carries no `ON CONFLICT`, and
    there is no unique constraint for it to conflict against."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0].id, teams[1].id

            for team_id in (team_a, team_b):
                await media_repo.create_media_story(
                    pool,
                    league_id=league_id,
                    season=_SEASON,
                    phase=Phase.REGULAR_SEASON,
                    cycle_index=0,
                    story_kind=StoryKind.STREAK,
                    voice="beat_writer",
                    template_id="fallback_streak",
                    slot_bindings={},
                    rendered_text="A streak story.",
                    salience=0.5,
                    subject_player_id=None,
                    subject_team_id=team_id,
                    writer_id=None,
                )

            rows = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert len(rows) == 2
            assert {r.subject_team_id for r in rows} == {team_a, team_b}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# media_pulse_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_pulse_snapshot_teams_round_trips_and_updates_in_place():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0].id, teams[1].id

            first = [
                MediaPulseSnapshot(
                    league_id=league_id, season=_SEASON, cycle_index=0, team_id=team_a,
                    wins=5, losses=3, standings_position=1, streak_length=5,
                ),
                MediaPulseSnapshot(
                    league_id=league_id, season=_SEASON, cycle_index=0, team_id=team_b,
                    wins=3, losses=5, standings_position=4, streak_length=-3,
                ),
            ]
            written = await media_repo.upsert_pulse_snapshot_teams(pool, first)
            assert len(written) == 2

            loaded = await media_repo.get_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert {row.team_id: row for row in loaded}[team_a].wins == 5
            assert type({row.team_id: row for row in loaded}[team_a].wins) is int

            # Re-upserting the SAME (league, season, cycle, team) key updates
            # in place, not a second row.
            updated = [
                MediaPulseSnapshot(
                    league_id=league_id, season=_SEASON, cycle_index=0, team_id=team_a,
                    wins=6, losses=3, standings_position=1, streak_length=6,
                )
            ]
            await media_repo.upsert_pulse_snapshot_teams(pool, updated)
            loaded_again = await media_repo.get_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert len(loaded_again) == 2, "same PK must update in place, not insert a second row"
            by_team = {row.team_id: row for row in loaded_again}
            assert by_team[team_a].wins == 6
            assert by_team[team_a].streak_length == 6
            assert by_team[team_b].wins == 3, "team_b's row must be untouched"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_upsert_pulse_snapshot_teams_is_a_no_op_on_an_empty_list():
    pool = await get_pool()
    try:
        result = await media_repo.upsert_pulse_snapshot_teams(pool, [])
        assert result == []
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_prior_pulse_snapshot_teams_reads_the_most_recent_cycle_before_a_gap():
    """Decision P4-28's "a missed cycle widens the window, never corrupts
    it" -- `get_prior_pulse_snapshot_teams` reads the most recent PERSISTED
    cycle strictly before the given one, not a literal `cycle_index - 1`,
    so a caller whose cycle 1 was never persisted still gets a real,
    if coarser, prior snapshot at cycle 2 rather than silently reading
    "no snapshot at all"."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            # No rows at all yet -- the league's first pulse cycle ever.
            none_yet = await media_repo.get_prior_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, before_cycle_index=0
            )
            assert none_yet == []

            await media_repo.upsert_pulse_snapshot_teams(
                pool,
                [
                    MediaPulseSnapshot(
                        league_id=league_id, season=_SEASON, cycle_index=0, team_id=team_id,
                        wins=1, losses=0, standings_position=1, streak_length=1,
                    )
                ],
            )
            # Cycle 1 is deliberately never persisted -- the gap.

            prior_for_cycle_2 = await media_repo.get_prior_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, before_cycle_index=2
            )
            assert len(prior_for_cycle_2) == 1
            assert prior_for_cycle_2[0].cycle_index == 0
            assert prior_for_cycle_2[0].wins == 1

            await media_repo.upsert_pulse_snapshot_teams(
                pool,
                [
                    MediaPulseSnapshot(
                        league_id=league_id, season=_SEASON, cycle_index=2, team_id=team_id,
                        wins=3, losses=0, standings_position=1, streak_length=3,
                    )
                ],
            )
            prior_for_cycle_3 = await media_repo.get_prior_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, before_cycle_index=3
            )
            assert len(prior_for_cycle_3) == 1
            assert prior_for_cycle_3[0].cycle_index == 2, (
                "must read the most recent real cycle (2), not stale cycle 0"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# media_writers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_media_writer_distinguishes_team_scoped_and_league_wide_rows():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            assert await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer"
            ) is None
            assert await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=None, persona="hot_take"
            ) is None

            team_scoped = await media_repo.create_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer",
                name="Real Beat Writer",
            )
            league_wide = await media_repo.create_media_writer(
                pool, league_id=league_id, team_id=None, persona="hot_take",
                name="Real Hot Take",
            )

            found_team_scoped = await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer"
            )
            assert found_team_scoped == team_scoped

            found_league_wide = await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=None, persona="hot_take"
            )
            assert found_league_wide == league_wide

            # A team-scoped lookup for a DIFFERENT team must miss, even
            # though a beat_writer row exists for THIS league.
            other_teams = teams[1].id
            assert await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=other_teams, persona="beat_writer"
            ) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_media_writer_raises_unique_violation_on_a_real_race():
    """`get_or_create_media_writer` (`dba2/services/media_service.py`) is
    what actually catches this in production; this test proves the DB-level
    contract that function's own `except asyncpg.UniqueViolationError`
    clause depends on -- a second `create_media_writer` call for the same
    `(league_id, team_id, persona)` triple really does raise, not silently
    insert a duplicate row."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            await media_repo.create_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer",
                name="First Beat Writer",
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await media_repo.create_media_writer(
                    pool, league_id=league_id, team_id=team_id, persona="beat_writer",
                    name="Second Beat Writer",
                )

            rows = await media_repo.list_media_writers_for_league(pool, league_id)
            assert len(rows) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_media_writer_name_changes_the_row_the_wire_already_points_to():
    """docs/design/media-v2-newsroom.md MV-3b.2: the byline rename lands on
    the EXISTING row (a foreign key to it, not to the name), so a story
    already persisted against `writer_id` reflects the new name with no
    write to `media_stories` itself."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            writer = await media_repo.create_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer",
                name="Generated Name",
            )
            story = await media_repo.create_media_story(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=1,
                story_kind=StoryKind.STREAK,
                voice="beat_writer",
                template_id="fallback_streak",
                slot_bindings={"team_name": "A Team", "streak_length": "3"},
                rendered_text="A Team have won 3 in a row.",
                salience=0.5,
                subject_player_id=None,
                subject_team_id=team_id,
                writer_id=writer.id,
            )

            updated = await media_repo.update_media_writer_name(
                pool, writer.id, "Library Display Name"
            )
            assert updated.id == writer.id
            assert updated.name == "Library Display Name"

            re_read = await media_repo.get_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer"
            )
            assert re_read.name == "Library Display Name"

            re_fetched_story = await media_repo.get_media_story(pool, story.id)
            assert re_fetched_story.writer_id == writer.id, (
                "the story's FK is unchanged -- it points at the row, not the name"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_update_media_story_text_changes_exactly_three_columns_and_refuses_posted_rows():
    """docs/design/media-v2-step8-anthropic-writer.md S8-13: the AI rewrite
    pass's one write path touches headline/rendered_text/writer_backend and
    nothing else, and never rewrites a story already on the wire."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id
            writer = await media_repo.create_media_writer(
                pool, league_id=league_id, team_id=team_id, persona="beat_writer", name="W",
            )
            story = await media_repo.create_media_story(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                cycle_index=1, story_kind=StoryKind.STREAK, voice="beat_writer",
                template_id="fallback_streak", slot_bindings={"team_name": "A Team"},
                rendered_text="Template body.", salience=0.5, subject_player_id=None,
                subject_team_id=team_id, writer_id=writer.id,
                headline="Template headline", fact_package={"k": 1},
                league_brief={"standings": []},
            )
            updated = await media_repo.update_media_story_text(
                pool, story.id, headline="AI headline", rendered_text="AI body.",
                writer_backend="anthropic:sonnet",
            )
            assert (updated.headline, updated.rendered_text, updated.writer_backend) == (
                "AI headline", "AI body.", "anthropic:sonnet",
            )
            changed = {
                f.name
                for f in dataclasses.fields(story)
                if getattr(story, f.name) != getattr(updated, f.name)
            }
            assert changed == {"headline", "rendered_text", "writer_backend"}

            await media_repo.mark_media_story_posted(pool, story.id)
            with pytest.raises(LookupError):
                await media_repo.update_media_story_text(
                    pool, story.id, headline="x", rendered_text="y", writer_backend="z",
                )
            with pytest.raises(LookupError):
                await media_repo.update_media_story_text(
                    pool, -1, headline="x", rendered_text="y", writer_backend="z",
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2h -- the team-profile composer's own
# two reads (step 2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_profiled_team_ids_reads_team_profile_stories_only():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            profiled_team, other_team = teams[0].id, teams[1].id

            # A real event story about `other_team` must NOT count as a
            # profile -- only `story_kind = 'team_profile'` does.
            await media_repo.create_media_story(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=1,
                story_kind=StoryKind.STREAK,
                voice="beat_writer",
                template_id="fallback_streak",
                slot_bindings={"team_name": "Other Team", "streak_length": "3"},
                rendered_text="Other Team have won 3 in a row.",
                salience=0.5,
                subject_player_id=None,
                subject_team_id=other_team,
                writer_id=None,
            )
            assert await media_repo.list_profiled_team_ids(
                pool, league_id=league_id, season=_SEASON
            ) == set()

            await pool.execute(
                """
                INSERT INTO media_stories
                    (league_id, season, phase, cycle_index, story_kind, voice, template_id,
                     slot_bindings, rendered_text, salience, subject_team_id)
                VALUES ($1, $2, $3, $4, 'team_profile', 'beat_writer', 'profile_template',
                        '{}', 'A full profile.', 0.7, $5)
                """,
                league_id,
                _SEASON,
                Phase.REGULAR_SEASON.value,
                2,
                profiled_team,
            )

            assert await media_repo.list_profiled_team_ids(
                pool, league_id=league_id, season=_SEASON
            ) == {profiled_team}
            # A different season's profile must not leak in.
            assert await media_repo.list_profiled_team_ids(
                pool, league_id=league_id, season=_SEASON - 1
            ) == set()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_final_pulse_snapshot_for_season_reads_the_highest_cycle_of_that_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_id = teams[0].id

            assert await media_repo.get_final_pulse_snapshot_for_season(
                pool, league_id=league_id, season=_SEASON - 1
            ) == []

            await media_repo.upsert_pulse_snapshot_teams(
                pool,
                [
                    MediaPulseSnapshot(
                        league_id=league_id, season=_SEASON - 1, cycle_index=0, team_id=team_id,
                        wins=10, losses=5, standings_position=3, streak_length=2,
                    )
                ],
            )
            await media_repo.upsert_pulse_snapshot_teams(
                pool,
                [
                    MediaPulseSnapshot(
                        league_id=league_id, season=_SEASON - 1, cycle_index=8, team_id=team_id,
                        wins=54, losses=28, standings_position=4, streak_length=-1,
                    )
                ],
            )
            # A later cycle of a DIFFERENT season must not be read as this one's final.
            await media_repo.upsert_pulse_snapshot_teams(
                pool,
                [
                    MediaPulseSnapshot(
                        league_id=league_id, season=_SEASON, cycle_index=99, team_id=team_id,
                        wins=1, losses=0, standings_position=1, streak_length=1,
                    )
                ],
            )

            final = await media_repo.get_final_pulse_snapshot_for_season(
                pool, league_id=league_id, season=_SEASON - 1
            )
            assert len(final) == 1
            assert final[0].cycle_index == 8
            assert final[0].wins == 54
            assert final[0].losses == 28
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
