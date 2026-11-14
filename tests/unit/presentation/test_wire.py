"""`presentation/wire.py`'s own tests -- Phase 5 Stage B step 6 (docs/
design/phase-5-discord-presentation.md §11, §6.3, Decision P5-15), extended
for build step 7 (docs/design/media-v2-newsroom.md MV-7): headline, byline,
per-voice color, and the `ARTICLE`-tier fact box read from `fact_package`.
No DB, no Discord -- `MediaStory` is a real `dba2.domain.media` dataclass
constructed by hand."""
from __future__ import annotations

from datetime import UTC, datetime

from dba2.domain.media import COLUMN_BY_VOICE, MediaStory, StoryKind
from dba2.domain.phase import Phase
from dba2.presentation.panel import MAX_DESCRIPTION_CHARS, PanelColor
from dba2.presentation.wire import _VOICE_COLORS, build_wire_panel, build_wire_panels
from tests.unit.presentation._golden_helpers import load_golden, panel_to_dict


def _story(
    story_kind: StoryKind,
    rendered_text: str,
    *,
    story_id: int = 1,
    voice: str = "beat_writer",
    writer_id: int | None = None,
    headline: str | None = None,
    format_tier: str = "blurb",
    fact_package: dict | None = None,
) -> MediaStory:
    return MediaStory(
        id=story_id, league_id=1, season=2026, phase=Phase.REGULAR_SEASON, cycle_index=1,
        story_kind=story_kind, voice=voice, template_id="t1", slot_bindings={},
        rendered_text=rendered_text, salience=0.8, subject_player_id=None, subject_team_id=1,
        writer_id=writer_id, created_at=datetime(2026, 1, 1, tzinfo=UTC),
        headline=headline, format_tier=format_tier, fact_package=fact_package or {},
    )


def test_wire_panel_matches_committed_golden_element_wise():
    story = _story(StoryKind.STREAK, "The Celtics have won six straight games.")
    panel = build_wire_panel(story)
    assert panel_to_dict(panel) == load_golden("presentation_wire.json")


def test_wire_panel_renders_rendered_text_verbatim_never_reformatted():
    """Decision P5-15's own literal guarantee -- whatever `rendered_text`
    says, byte-for-byte, is what `Panel.description` holds, including
    whitespace/punctuation a "helpful" re-render might normalize away."""
    text = "Team A  trails   Team B --- again.\n\nA second paragraph."
    story = _story(StoryKind.STANDINGS_COLLAPSE, text)
    panel = build_wire_panel(story)
    assert panel.description == text


def test_wire_panel_uses_the_headline_when_one_is_stored():
    story = _story(StoryKind.STREAK, "text", headline="Celtics Riding a Six-Game Streak")
    panel = build_wire_panel(story)
    assert panel.title == "Celtics Riding a Six-Game Streak"


def test_wire_panel_falls_back_to_the_kind_title_when_headline_is_none():
    """Every row persisted before migration 024 has `headline=None` --
    `_title_for`'s own raw-kind fallback is what those rows still render as
    (MV-7)."""
    story = _story(StoryKind.STREAK, "text", headline=None)
    panel = build_wire_panel(story)
    assert panel.title == "Streak"


def test_wire_panel_author_names_the_writer_and_the_column():
    story = _story(StoryKind.TRADE_REACTION, "text", voice="insider")
    panel = build_wire_panel(story, "Alex Rivera")
    assert panel.author == "Alex Rivera · The Trade Desk"


def test_wire_panel_author_is_the_column_alone_when_writer_name_is_none():
    """MV-8's disclosed since-deleted-team race: `writer_id=None`, never
    the literal string "None" in the byline."""
    story = _story(StoryKind.STREAK, "text", voice="beat_writer", writer_id=None)
    panel = build_wire_panel(story, None)
    assert panel.author == "The Beat"
    assert "None" not in panel.author


def test_wire_panel_footer_names_the_phase_and_season():
    story = _story(StoryKind.STREAK, "text")
    panel = build_wire_panel(story)
    assert panel.footer == "Regular Season · Season 2026"


def test_every_column_has_a_distinct_voice_color():
    """MV-7: "colors move to a per-voice accent ... so each persona looks
    different" -- every `COLUMN_BY_VOICE` key has an entry, and no two
    personas share a color."""
    assert set(COLUMN_BY_VOICE) <= set(_VOICE_COLORS)
    colors = [_VOICE_COLORS[voice] for voice in COLUMN_BY_VOICE]
    assert len(colors) == len(set(colors)), "two personas share the same accent color"


def test_wire_panel_color_falls_back_to_the_writers_own_voice():
    """A kind with no `_STORY_KIND_COLORS` override (everything but the
    four MV-7 names) shows its WRITER's own accent color, not a fixed
    per-kind color -- the real behavior change build step 7 makes."""
    insider_panel = build_wire_panel(_story(StoryKind.TRADE_REACTION, "text", voice="insider"))
    analyst_panel = build_wire_panel(_story(StoryKind.TRADE_REACTION, "text", voice="analyst"))
    assert insider_panel.color is PanelColor.NEUTRAL
    assert analyst_panel.color is PanelColor.GOLD
    assert insider_panel.color != analyst_panel.color


def test_wire_panel_color_override_wins_for_the_four_named_negative_kinds():
    """MV-7's own narrower override list -- these four ALWAYS read as bad
    news, regardless of which persona drew them."""
    for kind, expected in (
        (StoryKind.STANDINGS_COLLAPSE, PanelColor.NEGATIVE),
        (StoryKind.PLAYER_UNREST, PanelColor.WARNING),
        (StoryKind.HOLDOUT_WATCH, PanelColor.WARNING),
        (StoryKind.COLD_STRETCH, PanelColor.WARNING),
    ):
        for voice in COLUMN_BY_VOICE:
            panel = build_wire_panel(_story(kind, "text", voice=voice))
            assert panel.color is expected, f"{kind} drawn by {voice} did not override"


def test_wire_panel_blurb_tier_has_no_fields():
    story = _story(StoryKind.STREAK, "text", format_tier="blurb", fact_package={"facts": [
        {"key": "streak_length", "label": "Streak", "value_text": "6", "value_number": 6.0},
    ]})
    panel = build_wire_panel(story)
    assert panel.fields == ()


def test_wire_panel_article_tier_reads_up_to_three_fields_from_fact_package():
    fact_package = {
        "facts": [
            {"key": "team_name", "label": "Team", "value_text": "Celtics", "value_number": None},
            {"key": "streak_length", "label": "Streak", "value_text": "6", "value_number": 6.0},
            {"key": "wins", "label": "Wins", "value_text": "30", "value_number": 30.0},
            {"key": "losses", "label": "Losses", "value_text": "10", "value_number": 10.0},
            {"key": "games_played", "label": "Games", "value_text": "40", "value_number": 40.0},
        ]
    }
    story = _story(StoryKind.STREAK, "text", format_tier="article", fact_package=fact_package)
    panel = build_wire_panel(story)
    # `team_name` is excluded (an identity fact); the next three non-identity
    # facts in package order become fields -- the fourth ("games_played") is
    # dropped by MV-7's own literal "up to 3" cap.
    assert [f.name for f in panel.fields] == ["Streak", "Wins", "Losses"]
    assert [f.value for f in panel.fields] == ["6", "30", "10"]


def test_wire_panel_fact_box_is_empty_for_a_pre_migration_row():
    """`fact_package` defaults to `{}` for history that predates migration
    024 -- an `ARTICLE`-tier row with no real package still renders, with
    zero fields, never a KeyError."""
    story = _story(StoryKind.STREAK, "text", format_tier="article", fact_package={})
    panel = build_wire_panel(story)
    assert panel.fields == ()


def test_wire_panels_one_panel_per_story_same_order_given():
    stories = [
        _story(StoryKind.STREAK, "first", story_id=1),
        _story(StoryKind.MILESTONE, "second", story_id=2),
        _story(StoryKind.ROOKIE_WATCH, "third", story_id=3),
    ]
    panels = build_wire_panels(stories)
    assert [p.description for p in panels] == ["first", "second", "third"]


def test_wire_panels_resolves_each_storys_own_writer_name_by_id():
    stories = [
        _story(StoryKind.STREAK, "first", story_id=1, voice="insider", writer_id=11),
        _story(StoryKind.MILESTONE, "second", story_id=2, voice="historian", writer_id=22),
        _story(StoryKind.ROOKIE_WATCH, "third", story_id=3, voice="analyst", writer_id=None),
    ]
    panels = build_wire_panels(stories, {11: "Alex Rivera", 22: "Sam Okafor"})
    assert panels[0].author == "Alex Rivera · The Trade Desk"
    assert panels[1].author == "Sam Okafor · The Long View"
    assert panels[2].author == "Film Room"


def test_wire_panel_max_length_rendered_text_stays_within_the_description_ceiling():
    story = _story(StoryKind.MILESTONE, "x" * MAX_DESCRIPTION_CHARS)
    panel = build_wire_panel(story)
    assert len(panel.description) == MAX_DESCRIPTION_CHARS
