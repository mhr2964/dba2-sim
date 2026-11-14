"""The seven action types and their fixed, ordered role templates
(phase-1-simulation-engine.md §4.2, Decision P1-7), plus the once-per-
possession defensive matchup pairing (Decision P1-9) and the
`RoleAssignment` container both the setup beat and the screen beat's
switch handling read and write.

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from dba2.domain.player import Position
from dba2.sim.composites import best_rim_protector
from dba2.sim.context import SimPlayer


class ActionType(StrEnum):
    PNR_BALL = "pnr_ball"
    DHO = "dho"
    ISO = "iso"
    POST_UP = "post_up"
    OFF_BALL_SCREEN = "off_ball_screen"
    TRANSITION = "transition"
    SPOT_UP = "spot_up"


class RoleSlot(StrEnum):
    # offense
    BALL_HANDLER = "ball_handler"
    SCREENER = "screener"
    WEAK_SIDE_SHOOTER = "weak_side_shooter"
    CORNER_SHOOTER = "corner_shooter"
    HANDOFF_BIG = "handoff_big"
    POST_SCORER = "post_scorer"
    ENTRY_PASSER = "entry_passer"
    CUTTER = "cutter"
    TRAILER = "trailer"
    WING_RUNNER = "wing_runner"
    SHOOTER = "shooter"
    PASSER = "passer"
    # defense
    ON_BALL_DEF = "on_ball_def"
    SCREEN_DEF = "screen_def"
    LOW_MAN = "low_man"
    WEAK_SIDE_DEF = "weak_side_def"
    NEAREST_HELPER = "nearest_helper"
    POST_DEF = "post_def"
    DIGGER = "digger"
    CUTTER_DEF = "cutter_def"
    PASSER_DEF = "passer_def"
    FIRST_BACK = "first_back"
    RIM_PROTECTOR = "rim_protector"
    CLOSER = "closer"


@dataclass(frozen=True)
class RoleTemplate:
    """§4.2's table, one row per action type. Order is the order roles are
    filled in (offense roles are assigned by sequential weighted draws in
    `beats/setup.py`, each removing its pick from the remaining pool, so
    template order is also draw order -- changing it changes which player
    the *first* draw is allowed to consider, hence "fixed, ordered")."""

    offense: tuple[RoleSlot, ...]
    defense: tuple[RoleSlot, ...]


# BDA-4.7 (docs/design/block-defender-attribution.md, Option C):
# `RIM_PROTECTOR` is now listed in every action type's defense list, not
# just `transition`'s -- it names "whoever on this floor is best at rim
# protection," a fact every action type's defense has regardless of which
# offensive action is running, not something only a transition possession
# produces. Unlike every other slot here, it is NOT matchup-derived (see
# `_DEFENSE_ROLE_SOURCE`'s own comment below) -- `defense_roles_from_
# matchups` populates it as a documented special case.
ROLE_TEMPLATES: Mapping[ActionType, RoleTemplate] = {
    ActionType.PNR_BALL: RoleTemplate(
        offense=(
            RoleSlot.BALL_HANDLER,
            RoleSlot.SCREENER,
            RoleSlot.WEAK_SIDE_SHOOTER,
            RoleSlot.CORNER_SHOOTER,
        ),
        defense=(
            RoleSlot.ON_BALL_DEF,
            RoleSlot.SCREEN_DEF,
            RoleSlot.LOW_MAN,
            RoleSlot.WEAK_SIDE_DEF,
            RoleSlot.RIM_PROTECTOR,
        ),
    ),
    ActionType.DHO: RoleTemplate(
        offense=(RoleSlot.BALL_HANDLER, RoleSlot.HANDOFF_BIG, RoleSlot.WEAK_SIDE_SHOOTER),
        defense=(
            RoleSlot.ON_BALL_DEF,
            RoleSlot.SCREEN_DEF,
            RoleSlot.WEAK_SIDE_DEF,
            RoleSlot.RIM_PROTECTOR,
        ),
    ),
    ActionType.ISO: RoleTemplate(
        offense=(RoleSlot.BALL_HANDLER, RoleSlot.WEAK_SIDE_SHOOTER, RoleSlot.CORNER_SHOOTER),
        defense=(
            RoleSlot.ON_BALL_DEF,
            RoleSlot.NEAREST_HELPER,
            RoleSlot.WEAK_SIDE_DEF,
            RoleSlot.RIM_PROTECTOR,
        ),
    ),
    ActionType.POST_UP: RoleTemplate(
        offense=(RoleSlot.POST_SCORER, RoleSlot.ENTRY_PASSER, RoleSlot.WEAK_SIDE_SHOOTER),
        defense=(
            RoleSlot.POST_DEF,
            RoleSlot.DIGGER,
            RoleSlot.WEAK_SIDE_DEF,
            RoleSlot.RIM_PROTECTOR,
        ),
    ),
    ActionType.OFF_BALL_SCREEN: RoleTemplate(
        offense=(RoleSlot.CUTTER, RoleSlot.SCREENER, RoleSlot.ENTRY_PASSER),
        defense=(
            RoleSlot.CUTTER_DEF,
            RoleSlot.SCREEN_DEF,
            RoleSlot.PASSER_DEF,
            RoleSlot.RIM_PROTECTOR,
        ),
    ),
    ActionType.TRANSITION: RoleTemplate(
        offense=(RoleSlot.BALL_HANDLER, RoleSlot.TRAILER, RoleSlot.WING_RUNNER),
        defense=(RoleSlot.FIRST_BACK, RoleSlot.RIM_PROTECTOR),
    ),
    ActionType.SPOT_UP: RoleTemplate(
        offense=(RoleSlot.SHOOTER, RoleSlot.PASSER),
        defense=(RoleSlot.CLOSER, RoleSlot.WEAK_SIDE_DEF, RoleSlot.RIM_PROTECTOR),
    ),
}

# Decision P1-9 fixes the *pairing* algorithm; it does not name which
# specific defensive role slot inherits which specific offensive role's
# matchup defender -- §4.2's table only gives each side's role *counts*.
# This mapping is this dispatch's own deterministic, documented
# interpretive call: each defensive role slot draws from the offensive
# role whose matchup defender is the most basketball-plausible source for
# it (e.g. `weak_side_def` <- `weak_side_shooter`'s own primary
# defender). Slots not listed for a given action type (e.g. `wing_runner`
# in `transition`, which names 3 offense roles against only 2 defense
# roles -- defense is scrambling and deliberately under-specified) simply
# have no defender role assigned; they still have a matchup defender via
# `assign_matchups`, they just aren't named in that beat's role table.
#
# `RoleSlot.RIM_PROTECTOR` is deliberately absent from every entry below
# (BDA-4.7, Option C): it is the one defensive role slot `defense_roles_
# from_matchups` does NOT source from this table -- its real-world meaning
# is "whoever on this floor is best at rim protection," not "whoever is
# guarding that guy," so a matchup-derived source would be a category
# error for it specifically. `transition` used to map it to `matchups[
# TRAILER]` (the misnomer BDA-2 fact 3 names and BDA-3's Option C fixes) --
# removed here, not replaced, since `defense_roles_from_matchups` now
# populates it directly from `composites.best_rim_protector`.
_DEFENSE_ROLE_SOURCE: Mapping[ActionType, Mapping[RoleSlot, RoleSlot]] = {
    ActionType.PNR_BALL: {
        RoleSlot.ON_BALL_DEF: RoleSlot.BALL_HANDLER,
        RoleSlot.SCREEN_DEF: RoleSlot.SCREENER,
        RoleSlot.WEAK_SIDE_DEF: RoleSlot.WEAK_SIDE_SHOOTER,
        RoleSlot.LOW_MAN: RoleSlot.CORNER_SHOOTER,
    },
    ActionType.DHO: {
        RoleSlot.ON_BALL_DEF: RoleSlot.BALL_HANDLER,
        RoleSlot.SCREEN_DEF: RoleSlot.HANDOFF_BIG,
        RoleSlot.WEAK_SIDE_DEF: RoleSlot.WEAK_SIDE_SHOOTER,
    },
    ActionType.ISO: {
        RoleSlot.ON_BALL_DEF: RoleSlot.BALL_HANDLER,
        RoleSlot.NEAREST_HELPER: RoleSlot.WEAK_SIDE_SHOOTER,
        RoleSlot.WEAK_SIDE_DEF: RoleSlot.CORNER_SHOOTER,
    },
    ActionType.POST_UP: {
        RoleSlot.POST_DEF: RoleSlot.POST_SCORER,
        RoleSlot.DIGGER: RoleSlot.ENTRY_PASSER,
        RoleSlot.WEAK_SIDE_DEF: RoleSlot.WEAK_SIDE_SHOOTER,
    },
    ActionType.OFF_BALL_SCREEN: {
        RoleSlot.CUTTER_DEF: RoleSlot.CUTTER,
        RoleSlot.SCREEN_DEF: RoleSlot.SCREENER,
        RoleSlot.PASSER_DEF: RoleSlot.ENTRY_PASSER,
    },
    ActionType.TRANSITION: {
        RoleSlot.FIRST_BACK: RoleSlot.BALL_HANDLER,
    },
    ActionType.SPOT_UP: {
        RoleSlot.CLOSER: RoleSlot.SHOOTER,
        RoleSlot.WEAK_SIDE_DEF: RoleSlot.PASSER,
    },
}

_POSITION_ORDER: tuple[Position, ...] = (
    Position.PG,
    Position.SG,
    Position.SF,
    Position.PF,
    Position.C,
)


@dataclass(frozen=True)
class RoleAssignment:
    action_type: ActionType
    offense: Mapping[RoleSlot, int]
    defense: Mapping[RoleSlot, int]


def assign_matchups(
    offense: tuple[SimPlayer, ...], defense: tuple[SimPlayer, ...]
) -> dict[int, int]:
    """Decision P1-9: "on-court defenders are matched to on-court
    offensive players by a stable, deterministic pairing on primary
    position, breaking ties by height and then by player id." Returns
    offense `player_id` -> defender `player_id`.

    Algorithm: walk the canonical position order (PG..C); wherever exactly
    one offense player and one defense player share that exact primary
    position, pair them immediately. Whatever is left over on each side
    (a position with 0 or 2+ players on one side, e.g. two centers on the
    floor) is paired by sorting both remaining lists by `(height_in,
    player_id)` and zipping index-for-index -- deterministic, and exactly
    the tiebreak order the decision names.
    """
    if len(offense) != len(defense):
        raise ValueError(
            f"assign_matchups requires equal-sized lineups, got "
            f"{len(offense)} offense vs {len(defense)} defense"
        )
    unmatched_offense = list(offense)
    unmatched_defense = list(defense)
    matchups: dict[int, int] = {}

    for position in _POSITION_ORDER:
        off_at_pos = [p for p in unmatched_offense if p.player.primary_position == position]
        def_at_pos = [p for p in unmatched_defense if p.player.primary_position == position]
        if len(off_at_pos) == 1 and len(def_at_pos) == 1:
            o, d = off_at_pos[0], def_at_pos[0]
            matchups[o.id] = d.id
            unmatched_offense.remove(o)
            unmatched_defense.remove(d)

    unmatched_offense.sort(key=lambda p: (p.player.height_in, p.id))
    unmatched_defense.sort(key=lambda p: (p.player.height_in, p.id))
    for o, d in zip(unmatched_offense, unmatched_defense, strict=True):
        matchups[o.id] = d.id

    return matchups


def defense_roles_from_matchups(
    action_type: ActionType,
    offense_roles: Mapping[RoleSlot, int],
    matchups: Mapping[int, int],
    defense_lineup: tuple[SimPlayer, ...],
) -> dict[RoleSlot, int]:
    """Fill the action type's named defensive role slots from
    `_DEFENSE_ROLE_SOURCE`, translating each source offensive role's
    assigned player id through `matchups` to get that player's primary
    defender's id.

    BDA-4.7 (Option C): `RoleSlot.RIM_PROTECTOR` is filled after that loop,
    from `defense_lineup` directly rather than through `matchups` -- the
    one documented non-matchup-derived defensive role slot (see
    `_DEFENSE_ROLE_SOURCE`'s own comment above). Every template now
    declares it (`ROLE_TEMPLATES`), so it is populated unconditionally,
    not gated on `action_type`."""
    source = _DEFENSE_ROLE_SOURCE[action_type]
    defense_roles: dict[RoleSlot, int] = {}
    for def_role, off_role in source.items():
        offense_player_id = offense_roles[off_role]
        defense_roles[def_role] = matchups[offense_player_id]
    defense_roles[RoleSlot.RIM_PROTECTOR] = best_rim_protector(defense_lineup).id
    return defense_roles
