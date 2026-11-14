"""player_roles, player_directives — `role_repo`'s tables (§5). Pillar 2
scaffolding, ported from v1 unchanged.

phase-0-foundation.md §4.6. `role` stays plain `str` per the enum-vs-str
guidance (still no enumerated value set anywhere in the doc). The
directive mode columns **no longer stay plain `str`** -- corrected here,
not merely noted, per `CLAUDE.md`'s standing rule ("when a name
misrepresents its subject, fix it") and this file's own earlier docstring,
which named the exact condition that would end the `str` era: "a value set
named but not spelled out here is left as `str` rather than guessed at."
docs/design/gm-command-surface.md (PD-1) spells the four surviving
value sets out in full below, so the condition no longer holds. `role_mode`
is not among the four -- PD-6 (confirmed by the user 2026-09-22) drops it
outright rather than typing it: after PD-5, it has nothing left to mean
that `player_roles.role` (magnitude, via `touch_share`) and the new
`usage_mode` (kind) don't already cover between them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


@dataclass
class PlayerRole:
    """Ported from v1's migration 040 unchanged (§4.6)."""

    id: int
    league_id: int
    team_id: int
    season: int
    player_id: int
    role: str
    touch_share: Decimal
    rationale: str | None
    assigned_by: int | None
    locked: bool
    assigned_at: datetime


class ShotDiet(StrEnum):
    """PD-1/PD-3: `player_directives.shot_diet`'s closed value set, wired
    into `beats/resolution.py::select_shot_zone` (PD-2/PD-3) alongside the
    team-level `three_point_bias` knob, at the same seam. `BALANCED` is
    the neutral member PD-1 requires -- selecting it **is** the missing
    "clear" `set_directive`'s merge semantics otherwise have no way to
    express."""

    BALANCED = "balanced"
    MORE_THREES = "more_threes"
    ATTACK_THE_RIM = "attack_the_rim"
    FEWER_MIDRANGE = "fewer_midrange"


class UsageMode(StrEnum):
    """PD-1/PD-5: `player_directives.usage_mode`'s closed value set, wired
    into `beats/setup.py::_role_weight` via a per-role-**family**
    multiplier (PD-5's own invariant: `touch_share` cannot move a role
    *ratio*, only `usage_mode` can). `BALANCED` is the neutral member."""

    BALANCED = "balanced"
    INITIATE = "initiate"
    OFF_BALL = "off_ball"
    SCREEN_AND_ROLL = "screen_and_roll"
    POST_UP = "post_up"


class DefenseMode(StrEnum):
    """PD-1/PD-4: `player_directives.defense_mode`'s closed value set,
    wired into `beats/help.py::_gate_probability` as a fixed additive
    offset on `gate_diff`. `BALANCED` is the neutral member (zero offset,
    today's behaviour)."""

    BALANCED = "balanced"
    STAY_HOME = "stay_home"
    HELP_EARLY = "help_early"


class ClutchMode(StrEnum):
    """PD-1/PD-7: the column and this enum both stay (a future design can
    give clutch behaviour a real hook without another schema change), but
    **no command writes it** -- PD-7 removes `clutch_mode` from `/gm
    directive` outright rather than ship a picker that provably changes
    nothing (§4.7's own "no clutch in this engine" finding: zero matches
    for `clutch` under `dba2/sim/`). `BALANCED` is the only member for now
    -- there is no ruling yet on what a non-neutral clutch directive would
    even mean (PD-7's own "what it would actually take" scope note)."""

    BALANCED = "balanced"


@dataclass
class PlayerDirective:
    """Ported from v1's migration 018, PD-1-retyped (2026-09-23):
    `shot_diet`/`usage_mode`/`defense_mode`/`clutch_mode` were free-text
    `str | None` with no engine reader at all; each is now `<Enum> | None`
    over its own closed value set above, `None` meaning "never set" (a row
    may not exist at all, or an existing row may not have this field set)
    and a real enum member -- including `BALANCED` -- meaning a GM (or
    this dataclass's own construction default) explicitly chose the
    no-op. `role_mode` is gone (PD-6): the column drop is migration 023."""

    id: int
    league_id: int
    player_id: int
    shot_diet: ShotDiet | None
    usage_mode: UsageMode | None
    defense_mode: DefenseMode | None
    clutch_mode: ClutchMode | None
    set_by: int | None
    updated_at: datetime
