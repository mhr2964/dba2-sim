"""The wire channel's panel builder -- `/wire`'s `beat`-filtered pager and
the auto-posted wire channel both (docs/design/phase-5-discord-
presentation.md Section 11 Stage B step 6, Decision P5-15; docs/design/
media-v2-newsroom.md MV-7, build step 7).

**Decision P5-15 -- the wire channel posts `MediaStory.rendered_text`
VERBATIM, one message per story, and never re-renders a story from its
`slot_bindings`.** This module enforces that literally: `build_wire_panel`
reads `story.rendered_text` and places it in `Panel.description`
unmodified -- no truncation, no reformatting, no re-templating, and
nothing here ever touches `story.slot_bindings`. If a real `rendered_
text` were ever long enough to blow `Panel`'s own 4096-char description
ceiling, the correct behavior is `Panel.__post_init__` raising
`ValueError`, not silently truncating a "verbatim" guarantee -- Phase 4's
own media pipeline is what bounds story length in practice (Decision
P4-32's stored, already-rendered snapshot; MV-4's own tighter editorial
budgets narrow it further at write time), and this module does not
second-guess that bound.

**MV-7, build step 7: `build_wire_panel` gains `writer_name`, `build_wire_
panels` gains `writer_names`, and the panel itself gains a headline, a
per-voice byline/color and an `ARTICLE`-tier fact box.** `title = story.
headline or _title_for(story.story_kind)` -- `headline` is `None` for
every story persisted before migration 024 (`_title_for`'s own raw-kind
fallback is what those rows still render as); every story `select_and_
bind_story` persists from build step 7 forward carries a real one.
`author` names the writer AND the column (`f"{writer_name} · {column_
for(story.voice)}"`), or the column alone when `writer_name` is `None`
-- a real, already-shipped state, not a defensive branch: `media_service.
select_and_bind_story` persists `writer_id=None` for a `beat_writer` story
whose team was deleted between sealing and persistence (MV-8), and
`tests/integration/test_media_service.py` already exercises that race.

`MediaStory` is a `dba2.domain.media` dataclass, not a `dba2.media` pure-
package type -- presentation MAY import it freely (Decision P5-3), the
same way `team.py` takes a real `Team`/`Player`/`CapSummary` rather than a
stand-in. `column_for`/`COLUMN_BY_VOICE` live in `dba2.domain.media`
itself, not read from the loaded library -- see that dict's own docstring
for why (`dba2/services/media_service.py` sits behind a layering valve
this module may not cross).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from dba2.domain.media import MediaStory, StoryKind, column_for
from dba2.domain.media_writer import FormatTier
from dba2.presentation.panel import Panel, PanelColor, PanelField

# **Per-voice accent (MV-7): "colors move to a per-voice accent ... so each
# persona looks different."** An editorial, undirected choice among
# `PanelColor`'s own five existing members (disclosed, not calibrated --
# the same status every other unnumbered aesthetic choice in this design
# carries): `insider` (breaking-news blurple), `analyst` (a distinct,
# premium gold), `historian` (a warm, archival amber), `hot_take` (loud
# red, matching its own "inflammatory" register), `beat_writer` (steady
# green, the recurring roamer). Every `COLUMN_BY_VOICE` key has an entry --
# checked by a pure exhaustiveness unit test, the same shape `dba2/media/
# write/budget.py::HEADLINE_STYLE_BY_REGISTER` already uses over `Register`.
_VOICE_COLORS: dict[str, PanelColor] = {
    "insider": PanelColor.NEUTRAL,
    "analyst": PanelColor.GOLD,
    "historian": PanelColor.WARNING,
    "hot_take": PanelColor.NEGATIVE,
    "beat_writer": PanelColor.POSITIVE,
}

# **MV-7's own narrower override list** -- kept for exactly the four
# negative kinds MV-7 names, so a genuinely bad-news story reads as bad
# news regardless of which persona happened to draw it. Revision from the
# pre-step-7 map this module shipped with (which also forced `STANDINGS_
# SURGE`/`STREAK` to `POSITIVE` and `TRADE_FALLOUT` to `WARNING`
# unconditionally) -- those three now show their writer's own per-voice
# color instead, per MV-7's explicit text naming only these four.
# `COLD_STRETCH` (one of MV-2's eleven new kinds, no pre-step-7 entry to
# revise) gets `WARNING`, matching `PLAYER_UNREST`/`HOLDOUT_WATCH`'s own
# "a concerning trend, not yet a catastrophe" treatment rather than
# `STANDINGS_COLLAPSE`'s harsher `NEGATIVE`.
_STORY_KIND_COLORS: dict[StoryKind, PanelColor] = {
    StoryKind.STANDINGS_COLLAPSE: PanelColor.NEGATIVE,
    StoryKind.PLAYER_UNREST: PanelColor.WARNING,
    StoryKind.HOLDOUT_WATCH: PanelColor.WARNING,
    StoryKind.COLD_STRETCH: PanelColor.WARNING,
}

# **MV-7's own literal fact-box budget for presentation: "up to 3 PanelFields
# read from fact_package."** Stated flat, with no `TEAM_PROFILE` exception --
# unlike `dba2/media/write/budget.py::fact_box_field_count` (MV-4's own
# per-kind allowance, `TEAM_PROFILE` at 5), which governs the WRITER's own
# `WrittenStory.fact_box` at write time, a value this module never reads
# (`fact_package` -- the raw `StoryPackage.to_prompt_json()` -- is what is
# persisted and read here, not the writer's already-selected fact box). A
# real, disclosed narrowing for `TEAM_PROFILE` specifically: its own article
# shows 3 of the (up to) 5 facts its writer considered, not all 5. Flagged
# here rather than silently matched to MV-4's number, because MV-7's own
# text is explicit and this module may not import `dba2.media.write.budget`
# to share the real constant (Decision P5-3/P4-31's layering valve --
# `presentation/` imports only `domain`/`core`).
_FACT_BOX_MAX_FIELDS = 3
_FACT_BOX_EXCLUDED_KEYS = frozenset(
    {"season", "phase", "team_name", "player_name", "opponent_team_name"}
)
_FACT_BOX_NAME_MAX = 40
_FACT_BOX_VALUE_MAX = 200


def _title_for(story_kind: StoryKind) -> str:
    return story_kind.value.replace("_", " ").title()


def _clamp(text: str, max_chars: int) -> str:
    """A small, local word-boundary truncation, mirroring `dba2/media/write/
    budget.py::_truncate`'s own shape (word-boundary + ellipsis) without
    importing it -- this module may not reach `dba2.media` at all (the same
    layering valve `column_for`'s own docstring cites), so a short, disclosed
    duplicate is the only option; `budget.py`'s own MV-4 numbers (`name <=
    40`, `value <= 200`) are quoted for this exact case (an `ARTICLE`
    fact box), so the two never need to agree by construction, only by
    citation."""
    stripped = text.strip()
    if not stripped or len(stripped) <= max_chars:
        return stripped
    ellipsis = "…"
    budget = max_chars - len(ellipsis)
    if budget <= 0:
        return ellipsis[:max_chars]
    truncated = stripped[:budget]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip() + ellipsis


def _fact_box_fields(fact_package: Mapping[str, object]) -> tuple[PanelField, ...]:
    """The first `_FACT_BOX_MAX_FIELDS` non-identity, non-universal facts in
    `fact_package["facts"]`'s own order (`StoryPackage.to_prompt_json()`'s
    own emission order, itself a detector's deterministic fact order,
    MV-1b) -- never re-sorted, so the same story renders the same fact box
    on every read. A malformed or missing `"facts"` list (only possible for
    a pre-migration-024 row, whose `fact_package` defaults to `{}`) yields
    no fields at all, never a raised error -- a `BLURB`-shaped degrade for
    history that predates this column, not a broken panel."""
    raw_facts = fact_package.get("facts")
    if not isinstance(raw_facts, list):
        return ()
    fields: list[PanelField] = []
    for raw_fact in raw_facts:
        if len(fields) >= _FACT_BOX_MAX_FIELDS:
            break
        if not isinstance(raw_fact, dict):
            continue
        key = raw_fact.get("key")
        if key in _FACT_BOX_EXCLUDED_KEYS:
            continue
        label = raw_fact.get("label")
        value_text = raw_fact.get("value_text")
        if not isinstance(label, str) or not isinstance(value_text, str):
            continue
        name = _clamp(label, _FACT_BOX_NAME_MAX)
        value = _clamp(value_text, _FACT_BOX_VALUE_MAX)
        if not name or not value:
            continue
        fields.append(PanelField(name=name, value=value))
    return tuple(fields)


def build_wire_panel(story: MediaStory, writer_name: str | None = None) -> Panel:
    """One `MediaStory` -> one `Panel` -> one Discord message, matching
    Decision P5-15's "one message per story" wire-channel shape.

    `writer_name` defaults to `None` (a pre-step-7 caller passing a bare
    `MediaStory` keeps rendering the column alone, never the literal string
    "None") -- every real caller as of build step 7 (`view_service.get_
    wire_panels`/`get_unposted_wire_panels`) resolves and passes a real
    value or an intentional `None`."""
    color = _STORY_KIND_COLORS.get(
        story.story_kind, _VOICE_COLORS.get(story.voice, PanelColor.NEUTRAL)
    )
    column = column_for(story.voice)
    author = f"{writer_name} · {column}" if writer_name is not None else column
    footer = f"{story.phase.value.replace('_', ' ').title()} · Season {story.season}"
    fields = (
        _fact_box_fields(story.fact_package)
        if story.format_tier == FormatTier.ARTICLE.value
        else ()
    )
    return Panel(
        title=story.headline or _title_for(story.story_kind),
        description=story.rendered_text,
        fields=fields,
        color=color,
        footer=footer,
        author=author,
    )


def build_wire_panels(
    stories: Sequence[MediaStory], writer_names: Mapping[int, str] | None = None
) -> tuple[Panel, ...]:
    """`/wire`'s own on-demand form of Section 6.3 -- the same verbatim,
    one-panel-per-story rule, applied to however many stories the caller
    already selected. Which stories, which cycle, and what order `stories`
    arrives in are all `view_service`'s decisions, not this module's --
    this function renders whatever sequence it is given, in that order.

    `writer_names` is a `{writer_id: name}` map (`view_service`'s own one
    extra batch read, MV-7 -- never a per-story lookup); a story whose
    `writer_id` is `None`, or whose id is absent from the map (a writer row
    deleted since, though `media_writers` carries no `ON DELETE` today),
    renders with no writer name, the same "column alone" branch `build_
    wire_panel` documents."""
    names = writer_names or {}
    return tuple(
        build_wire_panel(
            story, names.get(story.writer_id) if story.writer_id is not None else None
        )
        for story in stories
    )


__all__ = ["build_wire_panel", "build_wire_panels"]
