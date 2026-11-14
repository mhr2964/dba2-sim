"""`dba2/media/context.py` -- the pure assembly/ranking/linkage layer
(docs/design/media-v2-newsroom.md MV-12/MV-13). Zero I/O: every fixture
here hand-builds real domain objects (`StoryPackage`/`Subject`/`Fact`/
`pulse.PulseSnapshot`/`MediaStory`) rather than touching the database."""
from __future__ import annotations

from dba2.domain.media import MediaPulseSnapshot, MediaStory, StoryKind
from dba2.domain.media_context import BriefLink, SalienceBand
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase
from dba2.media import context, pulse, salience

_SEASON = 2
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 6


def _team_state(team_id, *, wins=10, losses=5, position=1, streak=2):
    return pulse.TeamPulseState(
        team_id=team_id, wins=wins, losses=losses, standings_position=position,
        streak_length=streak, win_pct=wins / (wins + losses) if (wins + losses) else 0.0,
    )


def _snapshot(team_states: dict[int, pulse.TeamPulseState]) -> pulse.PulseSnapshot:
    return pulse.PulseSnapshot(cycle_index=_CYCLE_INDEX, teams=team_states)


def _package(
    *, story_kind=StoryKind.STREAK, angle=Angle.STREAK, subject: Subject, facts=()
) -> StoryPackage:
    return StoryPackage(
        story_kind=story_kind, angle=angle, subject=subject, facts=facts,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )


def _team_subject(team_id: int, team_name: str) -> Subject:
    return Subject(kind=SubjectKind.TEAM, team_id=team_id, team_name=team_name)


def _player_subject(player_id: int, player_name: str, *, team_id=None, team_name=None) -> Subject:
    return Subject(
        kind=SubjectKind.PLAYER, player_id=player_id, player_name=player_name,
        team_id=team_id, team_name=team_name,
    )


def _fact(key: str, label: str, value_text: str, value_number: float | None = None) -> Fact:
    return Fact(key=key, label=label, value_text=value_text, value_number=value_number,
                source=FactSource.TEAMS)


def _scored(package: StoryPackage, score: float = 0.5) -> salience.ScoredCandidate:
    return salience.ScoredCandidate(candidate=salience.SalienceCandidate(package=package),
                                     score=score)


def _story(
    *, story_id: int, cycle_index: int, story_kind: StoryKind, voice: str,
    subject_player_id: int | None = None, subject_team_id: int | None = None,
    headline: str = "A real headline", subject_name: str = "Someone",
) -> MediaStory:
    return MediaStory(
        id=story_id, league_id=1, season=_SEASON, phase=_PHASE, cycle_index=cycle_index,
        story_kind=story_kind, voice=voice, template_id="t1", slot_bindings={},
        rendered_text=headline, salience=0.5, subject_player_id=subject_player_id,
        subject_team_id=subject_team_id, writer_id=None,
        created_at=__import__("datetime").datetime(2026, 1, 1),
        headline=headline,
        fact_package={
            "subject": {
                "player_name": subject_name if subject_player_id is not None else None,
                "team_name": subject_name if subject_team_id is not None else None,
            }
        },
    )


# ---------------------------------------------------------------------------
# league_games_played / build_standings_base
# ---------------------------------------------------------------------------


def test_league_games_played_reads_the_furthest_along_team():
    snapshot = _snapshot(
        {1: _team_state(1, wins=10, losses=5), 2: _team_state(2, wins=3, losses=2)}
    )
    assert context.league_games_played(snapshot) == 15


def test_league_games_played_is_zero_for_an_empty_snapshot():
    assert context.league_games_played(_snapshot({})) == 0


def test_build_standings_base_takes_top_5_bottom_3_by_position():
    teams = {
        i: _team_state(i, wins=30 - i, losses=i, position=i + 1) for i in range(12)
    }
    snapshot = _snapshot(teams)
    names = {i: f"Team {i}" for i in range(12)}
    rows = context.build_standings_base(snapshot, names)
    assert [r.standings_position for r in rows] == [1, 2, 3, 4, 5, 10, 11, 12]


def test_build_standings_base_never_double_counts_a_small_league():
    teams = {i: _team_state(i, position=i + 1) for i in range(6)}
    snapshot = _snapshot(teams)
    names = {i: f"Team {i}" for i in range(6)}
    rows = context.build_standings_base(snapshot, names)
    assert len(rows) == 6
    assert len({r.team_id for r in rows}) == 6


# ---------------------------------------------------------------------------
# build_season_arc_rows
# ---------------------------------------------------------------------------


def _pulse_row(team_id, *, wins, losses, position):
    return MediaPulseSnapshot(
        league_id=1, season=_SEASON - 1, cycle_index=8, team_id=team_id,
        wins=wins, losses=losses, standings_position=position, streak_length=0,
    )


def test_build_season_arc_rows_is_top_3_and_bottom_1():
    rows = [_pulse_row(i, wins=60 - i, losses=22 + i, position=i + 1) for i in range(10)]
    names = {i: f"Team {i}" for i in range(10)}
    top, bottom = context.build_season_arc_rows(rows, names)
    assert [r.standings_position for r in top] == [1, 2, 3]
    assert [r.standings_position for r in bottom] == [10]


def test_build_season_arc_rows_is_empty_for_no_prior_season():
    top, bottom = context.build_season_arc_rows([], {})
    assert top == ()
    assert bottom == ()


# ---------------------------------------------------------------------------
# build_league_brief
# ---------------------------------------------------------------------------


def _inputs(
    *, current_snapshot=None, team_names=None, other_desks_pool=(), recent_wire_pool=(),
    season_arc_top=(), season_arc_bottom=(),
):
    return context.CycleBriefInputs(
        season=_SEASON, phase=_PHASE.value, cycle_index=_CYCLE_INDEX,
        league_games_played=30,
        current_snapshot=current_snapshot or _snapshot({1: _team_state(1)}),
        team_names=team_names or {1: "Boston Celtics"},
        standings_base=context.build_standings_base(
            current_snapshot or _snapshot({1: _team_state(1)}), team_names or {1: "Boston Celtics"}
        ),
        other_desks_pool=other_desks_pool,
        recent_wire_pool=recent_wire_pool,
        season_arc_top=season_arc_top,
        season_arc_bottom=season_arc_bottom,
    )


def test_build_league_brief_excludes_the_writing_candidates_own_package():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb", team_id=10))
    other_package = _package(subject=_team_subject(20, "Sacramento Kings"))
    inputs = _inputs(
        other_desks_pool=(
            (_scored(writing_package), SalienceBand.MAIN),
            (_scored(other_package), SalienceBand.MAIN),
        ),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    assert len(brief.other_desks) == 1
    assert brief.other_desks[0].subject_name == "Sacramento Kings"


def test_build_league_brief_computes_same_player_link():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb", team_id=10))
    linked_package = _package(
        story_kind=StoryKind.PLAYER_UNREST, angle=Angle.UNREST,
        subject=_player_subject(1, "Marcus Webb", team_id=10),
    )
    unlinked_package = _package(subject=_player_subject(2, "Someone Else", team_id=20))
    inputs = _inputs(
        other_desks_pool=(
            (_scored(linked_package), SalienceBand.MAIN),
            (_scored(unlinked_package), SalienceBand.MAIN),
        ),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    by_name = {e.subject_name: e for e in brief.other_desks}
    assert by_name["Marcus Webb"].link is BriefLink.SAME_PLAYER
    assert by_name["Someone Else"].link is BriefLink.NONE


def test_build_league_brief_computes_same_team_and_opponent_links():
    writing_package = _package(
        story_kind=StoryKind.TRADE_EXECUTED, angle=Angle.TRADE_MADE,
        subject=Subject(
            kind=SubjectKind.MATCHUP, team_id=10, team_name="Boston Celtics",
            opponent_team_id=20, opponent_team_name="Sacramento Kings",
        ),
    )
    same_team_package = _package(subject=_team_subject(10, "Boston Celtics"))
    opponent_package = _package(subject=_team_subject(20, "Sacramento Kings"))
    unrelated_package = _package(subject=_team_subject(30, "Miami Heat"))
    inputs = _inputs(
        other_desks_pool=(
            (_scored(same_team_package), SalienceBand.MAIN),
            (_scored(opponent_package), SalienceBand.MAIN),
            (_scored(unrelated_package), SalienceBand.MAIN),
        ),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    by_name = {e.subject_name: e for e in brief.other_desks}
    assert by_name["Boston Celtics"].link is BriefLink.SAME_TEAM
    assert by_name["Sacramento Kings"].link is BriefLink.OPPONENT
    assert by_name["Miami Heat"].link is BriefLink.NONE


def test_build_league_brief_ranks_linked_ahead_of_unlinked_and_caps():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb", team_id=10))
    linked = _package(
        story_kind=StoryKind.PLAYER_UNREST,
        subject=_player_subject(1, "Marcus Webb", team_id=10),
    )
    unlinked_pool = tuple(
        (_scored(_package(subject=_team_subject(100 + i, f"Team {i}"))), SalienceBand.MAIN)
        for i in range(10)
    )
    inputs = _inputs(other_desks_pool=((_scored(linked), SalienceBand.MAIN), *unlinked_pool))
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    from dba2.domain.media_context import OTHER_DESKS_CAP

    assert len(brief.other_desks) == OTHER_DESKS_CAP
    assert brief.other_desks[0].link is BriefLink.SAME_PLAYER


def test_build_league_brief_ranks_on_beat_ahead_of_off_beat_within_a_link_class():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb", team_id=10))
    off_beat = _package(
        story_kind=StoryKind.ROOKIE_WATCH, subject=_team_subject(20, "Off Beat Team")
    )
    on_beat = _package(
        story_kind=StoryKind.TRADE_EXECUTED, subject=_team_subject(30, "On Beat Team")
    )
    inputs = _inputs(
        other_desks_pool=(
            (_scored(off_beat), SalienceBand.MAIN),
            (_scored(on_beat), SalienceBand.MAIN),
        ),
    )
    weights = {StoryKind.TRADE_EXECUTED.value: 1.0, StoryKind.ROOKIE_WATCH.value: 0.0}
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights=weights,
    )
    assert brief.other_desks[0].subject_name == "On Beat Team"
    assert brief.other_desks[1].subject_name == "Off Beat Team"


def test_build_league_brief_headline_fact_skips_identity_keys():
    package_with_only_identity = _package(
        subject=_team_subject(1, "Boston Celtics"),
        facts=(_fact("team_name", "Team", "Boston Celtics"),),
    )
    package_with_real_fact = _package(
        subject=_team_subject(2, "Sacramento Kings"),
        facts=(
            _fact("team_name", "Team", "Sacramento Kings"),
            _fact("streak_length", "Streak length", "5", 5.0),
        ),
    )
    writing_package = _package(subject=_player_subject(1, "Marcus Webb"))
    inputs = _inputs(
        other_desks_pool=(
            (_scored(package_with_only_identity), SalienceBand.MAIN),
            (_scored(package_with_real_fact), SalienceBand.MAIN),
        ),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    by_name = {e.subject_name: e for e in brief.other_desks}
    assert by_name["Boston Celtics"].headline_fact is None
    assert by_name["Sacramento Kings"].headline_fact == "Streak length: 5"


def test_build_league_brief_recent_wire_and_subject_history_share_one_read():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb", team_id=10))
    own_story = _story(
        story_id=1, cycle_index=_CYCLE_INDEX - 1, story_kind=StoryKind.PLAYER_UNREST,
        voice="insider", subject_player_id=1, subject_name="Marcus Webb",
        headline="Marcus Webb requests a trade",
    )
    other_story = _story(
        story_id=2, cycle_index=_CYCLE_INDEX - 2, story_kind=StoryKind.STREAK,
        voice="beat_writer", subject_team_id=99, subject_name="Miami Heat",
        headline="Miami Heat riding a hot streak",
    )
    inputs = _inputs(
        recent_wire_pool=((own_story, "The Trade Desk"), (other_story, "The Beat")),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    assert len(brief.recent_wire) == 2
    own_entry = next(e for e in brief.recent_wire if e.subject_name == "Marcus Webb")
    assert own_entry.link is BriefLink.SAME_PLAYER
    assert len(brief.subject_history) == 1
    assert brief.subject_history[0].headline == "Marcus Webb requests a trade"


def test_build_league_brief_subject_history_is_capped_and_newest_first():
    writing_package = _package(subject=_player_subject(1, "Marcus Webb"))
    stories = tuple(
        (
            _story(story_id=i, cycle_index=i, story_kind=StoryKind.PLAYER_UNREST,
                   voice="insider", subject_player_id=1, subject_name="Marcus Webb",
                   headline=f"Headline {i}"),
            "The Trade Desk",
        )
        for i in range(8)
    )
    inputs = _inputs(recent_wire_pool=stories)
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    from dba2.domain.media_context import SUBJECT_HISTORY_CAP

    assert len(brief.subject_history) == SUBJECT_HISTORY_CAP
    assert [e.cycle_index for e in brief.subject_history] == sorted(
        (e.cycle_index for e in brief.subject_history), reverse=True
    )


def test_build_league_brief_injects_the_subjects_own_team_into_standings():
    snapshot = _snapshot(
        {i: _team_state(i, position=i + 1) for i in range(12)}
    )
    names = {i: f"Team {i}" for i in range(12)}
    writing_package = _package(subject=_team_subject(7, "Team 7"))
    inputs = context.CycleBriefInputs(
        season=_SEASON, phase=_PHASE.value, cycle_index=_CYCLE_INDEX,
        league_games_played=30, current_snapshot=snapshot, team_names=names,
        standings_base=context.build_standings_base(snapshot, names),
        other_desks_pool=(), recent_wire_pool=(),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    assert 7 in {r.team_id for r in brief.standings}


def test_build_league_brief_does_not_duplicate_a_team_already_in_standings():
    snapshot = _snapshot({i: _team_state(i, position=i + 1) for i in range(12)})
    names = {i: f"Team {i}" for i in range(12)}
    writing_package = _package(subject=_team_subject(0, "Team 0"))  # already top-5
    inputs = context.CycleBriefInputs(
        season=_SEASON, phase=_PHASE.value, cycle_index=_CYCLE_INDEX,
        league_games_played=30, current_snapshot=snapshot, team_names=names,
        standings_base=context.build_standings_base(snapshot, names),
        other_desks_pool=(), recent_wire_pool=(),
    )
    brief = context.build_league_brief(
        inputs, package=writing_package, persona_story_kind_weights={},
    )
    assert [r.team_id for r in brief.standings].count(0) == 1
