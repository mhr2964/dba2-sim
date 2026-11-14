"""Team, team_strategy, team_manager_history — `team_repo`'s tables (§5).

phase-0-foundation.md §4.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Timeline(StrEnum):
    """Pillar 2's competitive-window model, §4.2."""

    REBUILD = "rebuild"
    RETOOL = "retool"
    WIN_NOW = "win_now"


class TimelineSource(StrEnum):
    """docs/design/phase-5-discord-presentation.md §29 (Decision P5-66)
    -- an open vocabulary enforced in Python (matching `player_
    motivation_events.event_type`/`media_stories.story_kind`), not a DB
    CHECK, so a future `'commissioner'` source costs one enum value and
    no migration. `teams.timeline` stays the ONE column holding the
    value; this is a SOURCE tag on that column, never a second value."""

    AUTO = "auto"
    MANUAL = "manual"


class GmPhilosophy(StrEnum):
    """Phase 2's Decision P2-1 (docs/design/phase-2-manager-minds.md §3):
    Pillar 2's seven GM philosophy keys, verbatim. `teams.gm_philosophy`
    stays an unconstrained `TEXT` column until migration 009 adds the
    CHECK constraint (Decision P2-1, landed at Phase 2 step 7 alongside
    Decision P2-33's `rookie_scale` repair) -- this enum's job is only to
    give `Team.gm_philosophy` and `managers/philosophy.py::PROFILES` a
    shared, closed key type in the meantime."""

    STAR_MAXER = "star_maxer"
    EGALITARIAN = "egalitarian"
    DEFENSE_FIRST = "defense_first"
    TENDENCY_RESPECTER = "tendency_respecter"
    VET_OVERRATER = "vet_overrater"
    YOUTH_DEVELOPER = "youth_developer"
    CHAOS = "chaos"


class OffensiveScheme(StrEnum):
    PNR_HEAVY = "pnr_heavy"
    MOTION = "motion"
    ISO_STAR = "iso_star"
    POST_UP = "post_up"
    FIVE_OUT = "five_out"


class DefensiveScheme(StrEnum):
    DROP = "drop"
    SWITCH_ALL = "switch_all"
    HEDGE = "hedge"
    ICE = "ice"
    ZONE_MIX = "zone_mix"


@dataclass
class Team:
    """Decision 9: no cached `team_offense_rating` / `team_defense_rating` /
    `pace` — strength is computed from the roster on read; pace moved to
    `team_strategy` as a strategy choice, not a rating.

    `gm_philosophy` is `GmPhilosophy`, not `str` (Decision P2-1,
    phase-2-manager-minds.md §3): Phase 0 left it a plain `str` under the
    enum-vs-str rule ("a value set named but not spelled out here is left
    as `str` rather than guessed at") because §4.2 referenced "the seven
    v1 philosophy keys" without enumerating them; Pillar 2 spells them
    out, so the condition that justified `str` no longer holds. Mirrors
    `timeline`'s existing `Timeline` mapping shape exactly.
    """

    id: int
    league_id: int
    code: str
    name: str
    city: str
    conference: str
    division: str
    manager_user_id: int | None
    gm_philosophy: GmPhilosophy
    timeline: Timeline
    home_market_size: int
    timeline_source: TimelineSource = TimelineSource.AUTO
    """Migration 013, §24.1's three hand-construction sites
    (`tools/validate_sim.py`, `tools/box_score_real_matchup.py`,
    `tools/gm_harness/metrics.py`) all take this default and keep
    compiling and behaving identically -- appended last per Decision
    P5-66's own instruction."""
    auto_pilot: bool = False
    """Migration 022 (docs/design/gm-command-surface.md, GM-1) --
    `teams.auto_pilot`, fully independent of `manager_user_id`. A human
    GM delegating their decisions to the AI, not resigning: the manager
    keeps the team, its channel and every permission. `NOT NULL DEFAULT
    FALSE`, so every existing row and every hand-constructed `Team` in
    `tools/` (`validate_sim.py`, `box_score_real_matchup.py`,
    `gm_harness/metrics.py`) is correct without edit -- the identical
    inertness-by-default argument `timeline_source` above already made.
    Appended LAST, after `timeline_source`, for the same reason."""


@dataclass
class TeamStrategy:
    """One row per (league, team, season) — the possession engine's
    setup-beat inputs (§4.2). Phase 0 creates the table; Phase 1 gives the
    values meaning."""

    league_id: int
    team_id: int
    season: int
    pace_target: int
    offensive_scheme: OffensiveScheme
    defensive_scheme: DefensiveScheme
    help_aggression: int
    three_point_bias: int
    crash_oreb_bias: int
    set_by: int | None


@dataclass
class TeamManagerHistory:
    """Ported from v1 unchanged (§4.2)."""

    id: int
    team_id: int
    user_id: int | None
    assigned_at: datetime
    removed_at: datetime | None
    assigned_by: int
