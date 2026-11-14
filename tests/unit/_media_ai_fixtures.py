"""Hand-built fixtures shared by the step-8 AI-writer tests
(docs/design/media-v2-step8-anthropic-writer.md S8-15). Deliberately built
by hand, not through the detector/seal pipeline, so a change to a detector
never silently changes a golden prompt fixture."""
from __future__ import annotations

from dba2.domain.media import Register, StoryKind
from dba2.domain.media_context import (
    BriefLink,
    LeagueBrief,
    OtherDeskEntry,
    SalienceBand,
    SeasonArc,
    SeasonArcRow,
    StandingsRow,
    SubjectHistoryEntry,
    WireEntry,
)
from dba2.domain.media_facts import (
    Angle,
    Fact,
    FactSource,
    StoryPackage,
    Subject,
    SubjectKind,
)
from dba2.domain.media_writer import PersonaVoice
from dba2.domain.phase import Phase


def make_package() -> StoryPackage:
    """A `TRADE_EXECUTED` story -- MV-14.4 item 6's own worked example."""
    return StoryPackage(
        story_kind=StoryKind.TRADE_EXECUTED,
        angle=Angle.TRADE_MADE,
        subject=Subject(
            kind=SubjectKind.MATCHUP, player_id=7, player_name="Marcus Webb",
            team_id=1, team_name="Portland Trail Blazers",
            opponent_team_id=2, opponent_team_name="Boston Celtics",
        ),
        facts=(
            Fact("team_name", "Team", "Portland Trail Blazers", None, FactSource.TEAMS),
            Fact("opponent_team_name", "Opponent", "Boston Celtics", None, FactSource.TEAMS),
            Fact("assets_sent", "Assets sent", "3", 3.0, FactSource.LEAGUE_TRANSACTIONS),
            Fact("ppg_delta", "Points per game change", "12.5", 12.5,
                 FactSource.PLAYER_SEASON_STATS),
            Fact("win_pct", "Win percentage", "61.3%", 0.613, FactSource.PULSE_SNAPSHOT),
            Fact("season", "Season", "2026", 2026.0, FactSource.GAMES),
        ),
        season=2026, phase=Phase.REGULAR_SEASON, cycle_index=4,
    )


def make_persona(*, key: str = "insider") -> PersonaVoice:
    return PersonaVoice(
        key=key, display_name="Jordan Ellis", column="The Trade Desk",
        beat_description="Trades, signings and front-office moves.",
        register=Register.REPORTORIAL,
        voice_rules=("Write in short declarative sentences.",),
        fences=("Never name a source.",),
    )


def make_brief() -> LeagueBrief:
    return LeagueBrief(
        standings=(
            StandingsRow(1, "Portland Trail Blazers", 30, 12, 2, 4),
            StandingsRow(2, "Boston Celtics", 33, 9, 1, -2),
        ),
        other_desks=(
            OtherDeskEntry(
                story_kind="player_unrest", angle="unrest", subject_name="Devon Hale",
                headline_fact="Devon Hale unrest level 8", salience_band=SalienceBand.MAIN,
                link=BriefLink.NONE,
            ),
        ),
        recent_wire=(
            WireEntry(
                cycle_index=3, story_kind="hot_streak", subject_name="Marcus Webb",
                headline="Marcus Webb: hot streak over 5 games", persona_column="The Long View",
                link=BriefLink.SAME_PLAYER,
            ),
        ),
        season_arc=SeasonArc(
            season=2026, phase="regular_season", cycle_index=4, league_games_played=42,
            last_season_top=(SeasonArcRow(2, "Boston Celtics", 60, 22, 1),),
            last_season_bottom=(),
        ),
        subject_history=(
            SubjectHistoryEntry(2, "trade_reaction", "Portland Trail Blazers: trade reaction"),
        ),
    )
