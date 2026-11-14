"""Live I/O proof for `LeagueBrief` (docs/design/media-v2-newsroom.md
MV-12/MV-13, "Build order" step 7.5b) -- MV-14.4's own acceptance items
3 (bounds), 5 (linkage), and the wiring that produces item 6's dump
(the As-built note carries the actual five-brief text; this file proves
the mechanism that produced it is real, not hand-typed).

Uses `sim_league_at` (a REAL, 30-team, fully-seeded dev league) for the
identical reason `test_media_service.py`'s own MV-11 tests do: this needs
real `games`/`media_pulse_snapshot` rows this file's own minimal fixtures
never create.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as `test_smoke.py`.
"""
from __future__ import annotations

import json

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, media_repo, team_repo
from dba2.domain.media import StoryKind
from dba2.domain.media_context import (
    MAX_SERIALIZED_CHARS,
    OTHER_DESKS_CAP,
    RECENT_WIRE_CAP,
    SEASON_ARC_BOTTOM_CAP,
    SEASON_ARC_TOP_CAP,
    STANDINGS_CAP,
    SUBJECT_HISTORY_CAP,
)
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase
from dba2.media.salience import SalienceCandidate
from dba2.services import media_service
from tests.integration._press_fixtures import drop, sim_league_at

_SEASON = 2026


def _real_streak_candidate(*, team_id: int, team_name: str, cycle_index: int) -> SalienceCandidate:
    """A hand-built, deterministically-high-scoring `STREAK` candidate --
    real `StoryPackage`/`Subject`/`Fact` shapes, never a detector/RNG draw,
    so this test's own linkage/bounds assertions do not depend on which
    detector happens to fire against a freshly-seeded league this cycle
    (`_magnitude`'s own `abs(streak_length) / 15.0` clamps to `1.0` at
    `streak_length=15`, clearing `SALIENCE_FLOOR` with real margin --
    `dba2.media.salience` module docstring's own worked example)."""
    subject = Subject(kind=SubjectKind.TEAM, team_id=team_id, team_name=team_name)
    facts = (
        Fact(
            key="streak_length", label="Streak length", value_text="15", value_number=15.0,
            source=FactSource.TEAM_GAME_STATS,
        ),
        # `FACT_KEYS_BY_KIND[StoryKind.STREAK]` requires `team_name` too --
        # `seal_package` adds this for a real detector-produced package;
        # this hand-built one must carry it itself for `select.bind_slots`'s
        # own slot-presence check to succeed.
        Fact(key="team_name", label="Team", value_text=team_name, value_number=None,
             source=FactSource.TEAMS),
    )
    package = StoryPackage(
        story_kind=StoryKind.STREAK, angle=Angle.STREAK, subject=subject, facts=facts,
        season=_SEASON, phase=Phase.REGULAR_SEASON, cycle_index=cycle_index,
    )
    return SalienceCandidate(package=package)


@pytest.mark.asyncio
async def test_a_real_persisted_brief_stays_in_bounds_and_links_a_real_prior_story():
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a = teams[0]
            team_a_name = f"{team_a.city} {team_a.name}"

            # A real prior story about team_a, cycle 0 -- the same
            # `txn_repo.create_trade_transaction`-style "insert a real row
            # to force an otherwise-rare real condition" technique this
            # file's own sibling (`test_media_service.py`'s TRADE_EXECUTED
            # test) already established, so §3/§5's linkage assertion below
            # is deterministic rather than hoping a fresh league's own real
            # detectors happen to repeat the same subject within 3 cycles.
            await media_repo.create_media_story(
                pool,
                league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON, cycle_index=0,
                story_kind=StoryKind.STANDINGS_SURGE, voice="beat_writer", template_id="t1",
                slot_bindings={}, rendered_text=f"{team_a_name} climbing the standings",
                salience=0.5, subject_player_id=None, subject_team_id=team_a.id, writer_id=None,
                headline=f"{team_a_name}: Standings Surge",
                fact_package={
                    "subject": {
                        "kind": "team", "player_id": None, "player_name": None,
                        "team_id": team_a.id, "team_name": team_a_name,
                        "opponent_team_id": None, "opponent_team_name": None,
                    },
                    "facts": [], "story_kind": "standings_surge", "season": _SEASON,
                    "phase": Phase.REGULAR_SEASON.value, "cycle_index": 0, "package_version": 1,
                },
            )

            pulse_result = await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            recent_stories = await media_repo.list_media_stories_for_cycle_range(
                pool, league_id=league_id, season=_SEASON, start_cycle_index=-2, end_cycle_index=0,
            )
            assert any(s.subject_team_id == team_a.id for s in recent_stories), (
                "the manually-inserted cycle-0 story must be visible to the last-3-"
                "cycles read, or this test cannot prove anything about linkage"
            )
            season_arc_rows = await media_repo.get_final_pulse_snapshot_for_season(
                pool, league_id=league_id, season=_SEASON - 1
            )
            league = await league_repo.get_league(pool, league_id)
            library = media_service.load_media_library()

            candidate = _real_streak_candidate(
                team_id=team_a.id, team_name=team_a_name, cycle_index=1
            )
            result = await media_service.run_media_selection_cycle(
                pool,
                league_id=league_id, league_rng_seed=league.rng_seed, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=1, candidates=(candidate,),
                library=library, use_counts={},
                current_snapshot=pulse_result.snapshot, team_names=pulse_result.team_names,
                recent_stories=recent_stories, season_arc_snapshot_rows=season_arc_rows,
            )
            assert len(result.stories) == 1, (
                f"the hand-built STREAK candidate must clear the salience floor and "
                f"publish -- got {len(result.stories)} stories"
            )
            story = result.stories[0]

            # MV-14.4 item 3 -- bounds, on a REAL persisted row.
            assert story.league_brief, (
                "a real CycleBriefInputs was supplied; league_brief must not be {}"
            )
            serialized = json.dumps(story.league_brief, sort_keys=True)
            assert len(serialized) <= MAX_SERIALIZED_CHARS
            assert len(story.league_brief["standings"]) <= STANDINGS_CAP
            assert len(story.league_brief["other_desks"]) <= OTHER_DESKS_CAP
            assert len(story.league_brief["recent_wire"]) <= RECENT_WIRE_CAP
            assert len(story.league_brief["subject_history"]) <= SUBJECT_HISTORY_CAP
            assert len(story.league_brief["season_arc"]["last_season_top"]) <= SEASON_ARC_TOP_CAP
            assert (
                len(story.league_brief["season_arc"]["last_season_bottom"])
                <= SEASON_ARC_BOTTOM_CAP
            )

            # MV-14.4 item 5 -- linkage, over the real inserted prior story.
            wire_entries = story.league_brief["recent_wire"]
            team_a_entries = [e for e in wire_entries if e["subject_name"] == team_a_name]
            assert team_a_entries, (
                f"expected the real cycle-0 story about {team_a_name!r} in §3 "
                f"(recent_wire); got subjects {[e['subject_name'] for e in wire_entries]}"
            )
            assert team_a_entries[0]["link"] == "same_team"
            # And it must ALSO be sliced into §5 (subject_history), MV-12.1's
            # own "sliced from §3's wider read, no second query" rule.
            assert story.league_brief["subject_history"], (
                "the same real prior story about this exact subject must also "
                "appear in §5"
            )
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()
