"""`StoryKind`'s canonical home (Decision P4-49), plus the three
migration-011 dataclasses -- `MediaStory`, `MediaPulseSnapshot`,
`MediaWriter` (Decision P4-32, as amended by P4-38/P4-48/P4-66).

docs/design/phase-4-media-and-player-agency.md §8 (Decision P4-32), §9.5
(P4-38/P4-39), §15 (P4-49), §11 Stage D step 19.

**Why `StoryKind` moved here rather than staying declared in `dba2/media/
frame.py` (Decision P4-49).** `dba2/data/repositories/media_repo.py` has to
reference `StoryKind` to map a `media_stories.story_kind` column back to a
real enum member. `tests/unit/test_layering.py`'s `_DATA_ALLOWED_PREFIXES =
("dba2.data", "dba2.domain", "dba2.core")` forbids anything under `dba2/
data/` from reaching into `dba2.media` at all, and Decision P4-31
independently forbids anything but `dba2/services/media_service.py` from
importing `dba2.media` in the first place -- so `media_repo.py` cannot
import `StoryKind` from `dba2.media.frame` under either rule.
`dba2/domain/media.py` is the only home that satisfies both constraints
simultaneously (`tests/unit/test_domain_layering.py` independently
restricts every file under `domain/` to `dba2.domain`/`dba2.core`, so this
module can only ever be a leaf other `domain/` files could import, never an
importer of anything media-selection-shaped itself).

**The dependency direction, stated so a future edit does not invert it:**
`dba2/media/frame.py` imports `StoryKind` FROM here and re-exports it (a
plain `from dba2.domain.media import StoryKind` at that module's top) --
never the reverse. Every existing `from dba2.media.frame import StoryKind`
import elsewhere under `dba2/media/` (`pulse.py`, `events.py`, `salience.py`,
`select.py` via `frame.py`) keeps working unchanged, because `StoryKind`
remains a real attribute of `frame.py`'s own namespace; only its
*declaration* site moved. `InsufficientDataReason` stays in `frame.py` --
it is never persisted (`media_stories` only ever holds a SUFFICIENT,
scored, selected candidate; `salience.score_candidates` drops every
`insufficient_data` frame before a story is ever built from it), so
`media_repo.py` has no reason to reference it and no layering pressure
forces it to move.

Zero I/O, the same purity rules every other `domain/` module carries --
this file imports only stdlib.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from dba2.domain.phase import Phase

# ---------------------------------------------------------------------------
# StoryKind -- moved verbatim from dba2/media/frame.py (Decision P4-25's
# closed enum; Decision P4-49 relocates only the declaration site, not the
# member list, the string values, or the docstring's own reasoning about why
# `injury_note` was dropped -- see that module's git history for the full
# original docstring if the "why eleven, not twelve" reasoning is needed;
# repeating it here would create a second copy of the same explanation to
# keep in sync).
# ---------------------------------------------------------------------------


class StoryKind(StrEnum):
    """The original eleven story kinds (Decision P4-25), the design draft's
    own §6.2 order, minus `injury_note` (dropped -- the `injuries` table has
    zero rows in any real league to date; see `dba2/media/frame.py`'s own
    docstring, this enum's prior home, for the full citation), **plus
    eleven more (docs/design/media-v2-newsroom.md MV-2, "Build order" step
    4)**. Declared here rather than in `dba2/media/frame.py` per Decision
    P4-49 -- see this module's own docstring for why.

    **The eleven new members below never reach `dba2/media/frame.py`'s
    `StoryFrame`/`derive_frame` pipeline at all** -- that module is
    unchanged by media-v2 (absent from MV-9's own "where the change lands"
    file table) and stays permanently scoped to the ORIGINAL eleven kinds;
    `dba2/media/detect/legacy.py::adapt_frame` is their one and only path to
    a `StoryPackage`. Each new kind's own pure detector
    (`dba2/media/detect/{trades,form,construction,rivalry,scheme,revenge,
    profile}.py`) builds a `StoryPackage` DRAFT directly, with no
    `StoryCandidate`/`StoryFrame` intermediate -- the whole reason MV-1b
    retyped the selection spine in the first place (`StoryFrame`'s closed
    field list could never have carried these)."""

    STANDINGS_SURGE = "standings_surge"
    STANDINGS_COLLAPSE = "standings_collapse"
    STREAK = "streak"
    TRADE_REACTION = "trade_reaction"
    TRADE_FALLOUT = "trade_fallout"
    SIGNING_REACTION = "signing_reaction"
    MILESTONE = "milestone"
    AWARD_RACE = "award_race"
    PLAYER_UNREST = "player_unrest"
    HOLDOUT_WATCH = "holdout_watch"
    ROOKIE_WATCH = "rookie_watch"

    # -------------------------------------------------------------------
    # MV-2's eleven new members, build step 4. String values match MV-2's
    # own prose names exactly (snake_case of the enum member name).
    # -------------------------------------------------------------------
    TRADE_EXECUTED = "trade_executed"
    TRADE_BROKEN_PROMISE = "trade_broken_promise"
    REVENGE_GAME = "revenge_game"
    HOT_STREAK = "hot_streak"
    COLD_STRETCH = "cold_stretch"
    DEFENSIVE_STANDOUT = "defensive_standout"
    ROSTER_CONSTRUCTION = "roster_construction"
    SCHEME_IDENTITY = "scheme_identity"
    RIVALRY_MATCHUP = "rivalry_matchup"
    TRADE_MARKET_WATCH = "trade_market_watch"
    TEAM_PROFILE = "team_profile"


class Register(StrEnum):
    """A persona's voice register (docs/design/media-v2-newsroom.md MV-3h,
    `PersonaVoice.register`) -- read by BOTH writer backends (MV-6): the
    template backend uses it to pick a headline-style bank and its
    punctuation/casing rules, a future LLM backend renders it into the
    persona card. Declared here (rather than on `dba2/domain/media_
    writer.py`, where `PersonaVoice` itself lives) because it is closed,
    listed vocabulary in the same family as `StoryKind` -- this module's
    own established home for that shape (Decision P4-49's reasoning,
    reused). Added in build step 1 alongside `StoryKind` and `MediaStory`'s
    OTHER pending additions (headline/format_tier/fact_package/
    writer_backend, a later build step) because it has zero interaction
    with either: a closed four-member enum with no reader in this build
    step but `dba2/domain/media_writer.py::PersonaVoice`'s own field type."""

    REPORTORIAL = "reportorial"
    MEASURED = "measured"
    NARRATIVE = "narrative"
    INFLAMMATORY = "inflammatory"


COLUMN_BY_VOICE: Mapping[str, str] = {
    "insider": "The Trade Desk",
    "analyst": "Film Room",
    "historian": "The Long View",
    "hot_take": "No Filter",
    "beat_writer": "The Beat",
}
"""MV-3's own persona table, mirrored here as a static, closed dict for
`presentation/wire.py::column_for` (build step 7, MV-7) -- **a deliberate
duplicate of `seeds/media_phrases.json`'s per-persona `column` field, not a
read of it.** `dba2/presentation/` may import only `domain`/`core`
(Decision P5-3's own layering valve); the loaded library (`PersonaMetadata.
column`) lives behind `dba2/services/media_service.py`, which presentation
may never import. MV-3 itself calls these five strings "the commissioner's
placeholders ... they can be renamed" and says renaming is "editing one
JSON string" -- that is true for every OTHER reader of `column`
(`resolve_persona_voice`'s own `PersonaVoice.column`), but NOT for this one:
a rename here needs a second edit, to this dict, because presentation
cannot read the JSON value at render time. Disclosed, not silently assumed
in sync."""


def column_for(voice: str) -> str:
    """A persona key's display column -- `voice`'s own value, title-cased
    with underscores turned to spaces, for any key this dict does not carry
    (never raises: a future sixth persona, or a corrupt stored value, still
    renders something readable rather than crashing a wire panel)."""
    return COLUMN_BY_VOICE.get(voice, voice.replace("_", " ").title())


# ---------------------------------------------------------------------------
# MediaStory / MediaPulseSnapshot / MediaWriter -- one dataclass per
# migration-011 table (Decision P4-32/P4-38/P4-48/P4-66), matching this
# repo's existing convention of one dataclass per row shape (e.g.
# `dba2.domain.head_to_head.HeadToHead` next to `gm_head_to_head`,
# `dba2.domain.award.PlayerAward` next to `player_awards`).
# ---------------------------------------------------------------------------


@dataclass
class MediaStory:
    """One row: one generated, scored, persisted story. `slot_bindings` and
    `rendered_text` are both stored (deliberate redundancy, Decision P4-32:
    the structured form keeps the story queryable and re-renderable after a
    template-library edit; the rendered snapshot means a template deleted in
    a later library edit does not silently erase last season's news).
    `salience` is `float`, never `Decimal` -- `double precision` at the
    column, matching migration 010's own choice for `gm_head_to_head`'s
    counters and this project's documented `Decimal`-vs-`int`/`float`
    equality trap (HANDOFF.md's Traps section). `subject_player_id`/
    `subject_team_id`/`writer_id` are all nullable with `ON DELETE SET
    NULL` -- a story about a retired-and-purged player, or bylined by a
    since-deleted writer row, is still league history; only the league's own
    deletion (`ON DELETE CASCADE` via `league_id`) removes the row itself.
    No `(league_id, season, cycle_index, template_id)` uniqueness --
    deliberate, per Decision P4-32's own closing note: two different
    subjects can legitimately draw the same template in one cycle."""

    id: int
    league_id: int
    season: int
    phase: Phase
    cycle_index: int
    story_kind: StoryKind
    voice: str
    template_id: str
    slot_bindings: Mapping[str, str]
    rendered_text: str
    salience: float
    subject_player_id: int | None
    subject_team_id: int | None
    writer_id: int | None
    created_at: datetime
    # When this story was posted to `#league-wire` (migration 020,
    # phase-5-stage-e-advance.md P5-99.s.1): publication state is per story, so a
    # crash mid-publication re-posts exactly the unposted ones. Always `None`
    # until the media-cycle commit adds its writer. Defaulted so no construction
    # site outside the mapper needs editing.
    posted_at: datetime | None = None
    # Migration 024 (docs/design/media-v2-newsroom.md MV-7/MV-9, build step
    # 7) -- all four defaulted, so no pre-existing construction site needs
    # editing. `headline` is `None` for every row persisted before this
    # migration (`presentation/wire.py::_title_for` is the fallback); every
    # row `select_and_bind_story` persists from this step forward carries a
    # real one. `format_tier` mirrors `dba2.domain.media_writer.FormatTier`'s
    # value as plain text, the same "closed vocabulary enforced in Python,
    # stored as TEXT" convention `story_kind`/`voice` already use on this
    # same dataclass -- kept a bare `str` here (not `FormatTier` itself)
    # because `FormatTier` lives in `media_writer.py`, which imports THIS
    # module (`Register`), so the reverse import would cycle. `fact_package`
    # is `StoryPackage.to_prompt_json()`, persisted verbatim (MV-6's own
    # prompt-contract payload) so a future `AnthropicWriter` (step 8) prompt
    # and a future re-render both read the SAME facts the story was actually
    # built from. `writer_backend` mirrors `WrittenStory.backend`
    # ("template" today; "anthropic:<model>" once step 8 lands).
    headline: str | None = None
    format_tier: str = "blurb"
    fact_package: Mapping[str, Any] = field(default_factory=dict)
    writer_backend: str = "template"
    # Migration 025 (docs/design/media-v2-newsroom.md MV-12.3 Option A,
    # build step 7.5b): `LeagueBrief.to_prompt_json()`, persisted verbatim
    # for the identical reason `fact_package` already is -- MV-6's own
    # "the LLM rewrites headline/rendered_text only, post-commit" design
    # means a future `AnthropicWriter` (step 8) rewrite call happens AFTER
    # this row already exists, by which point the league itself has moved
    # on; the brief must be recoverable exactly as it was at write time,
    # not re-derived against a since-advanced league state. `{}` for every
    # row persisted before this migration and for any row `select_and_
    # bind_story` writes without a real `CycleBriefInputs` available (a
    # pre-trade-deadline cycle, or a caller/test that predates step 7.5b)
    # -- "absent is neutral," the same rule `fact_package` already follows.
    league_brief: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class MediaPulseSnapshot:
    """One row: one team's pulse read at one `(league, season, cycle)` --
    the persisted mirror of `dba2.media.pulse.TeamPulseState`, one row per
    team per cycle rather than one row per whole-league snapshot, because
    the composite primary key `(league_id, season, cycle_index, team_id)`
    is what lets a caller read/write one team's row without touching every
    other team's, matching `dba2.domain.head_to_head.HeadToHead`'s own
    directed-row-per-pair shape rather than a single JSON blob per cycle.
    No `created_at` -- unlike `MediaStory` (persisted history) or
    `MediaWriter` (a stable, once-generated identity), this table is
    operational, per-cycle, rewritable state, the same status
    `gm_head_to_head`'s own rows have (that table has no `created_at`
    either, for the identical reason)."""

    league_id: int
    season: int
    cycle_index: int
    team_id: int
    wins: int
    losses: int
    standings_position: int
    streak_length: int


@dataclass
class MediaWriter:
    """One row: one persistent, named media-persona instance (Decision
    P4-38) -- one row per `(league_id, team_id, persona)` for `beat_writer`
    (one named writer per team), one row per `(league_id, NULL, persona)`
    for the other four archetypes (one syndicated byline per league).
    `persona` is the archetype key from `seeds/media_phrases.json`'s closed
    five-key set (Decision P4-53) -- a plain `str`, not `dba2.media.select.
    MediaPersona`, because this dataclass lives under `domain/` and cannot
    import anything under `dba2.media` at all (Decision P4-31's one-way
    valve). `name` is generated once, at first use, and stays stable for
    the league's life (Decision P4-38's own "generated once" requirement) --
    nothing in this repository ever updates an existing row's `name`."""

    id: int
    league_id: int
    team_id: int | None
    persona: str
    name: str
    created_at: datetime
