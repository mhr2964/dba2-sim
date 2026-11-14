"""Tier 1: the once-per-possession matchup pairing (Decision P1-9) and the
defensive-role-source table `playbook.py` builds on top of it."""
from __future__ import annotations

from dba2.domain.player import Position
from dba2.sim.composites import best_rim_protector
from dba2.sim.playbook import (
    ROLE_TEMPLATES,
    ActionType,
    RoleSlot,
    assign_matchups,
    defense_roles_from_matchups,
)
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player


def _lineup(ids_positions_heights: list[tuple[int, Position, int]]):
    return tuple(
        make_sim_player(pid, tuning=DEFAULT_TUNING, primary_position=pos, height_in=height)
        for pid, pos, height in ids_positions_heights
    )


def test_assign_matchups_pairs_exact_position_matches_directly():
    offense = _lineup(
        [
            (1, Position.PG, 74),
            (2, Position.SG, 77),
            (3, Position.SF, 79),
            (4, Position.PF, 82),
            (5, Position.C, 85),
        ]
    )
    defense = _lineup(
        [
            (11, Position.PG, 73),
            (12, Position.SG, 76),
            (13, Position.SF, 80),
            (14, Position.PF, 83),
            (15, Position.C, 86),
        ]
    )
    matchups = assign_matchups(offense, defense)
    assert matchups == {1: 11, 2: 12, 3: 13, 4: 14, 5: 15}


def test_assign_matchups_breaks_position_surplus_ties_by_height_then_id():
    """Two offensive centers against one defensive center and one
    defensive PF: the exact-position pass can only pair one of them (the
    other position has a surplus), so the leftover pair falls to the
    height/id tiebreak, per Decision P1-9's literal wording."""
    offense = _lineup(
        [
            (1, Position.PG, 74),
            (2, Position.SG, 77),
            (3, Position.SF, 79),
            (4, Position.C, 81),  # shorter of the two offensive "centers"
            (5, Position.C, 88),  # taller
        ]
    )
    defense = _lineup(
        [
            (11, Position.PG, 73),
            (12, Position.SG, 76),
            (13, Position.SF, 80),
            (14, Position.PF, 84),
            (15, Position.C, 89),
        ]
    )
    matchups = assign_matchups(offense, defense)
    # Exact matches: PG-PG, SG-SG, SF-SF.
    assert matchups[1] == 11
    assert matchups[2] == 12
    assert matchups[3] == 13
    # Leftover: offense {4 (81in), 5 (88in)} vs defense {14 (84in), 15 (89in)}
    # sorted by (height, id) and zipped -- shorter-with-shorter.
    assert matchups[4] == 14
    assert matchups[5] == 15


def test_assign_matchups_is_deterministic():
    offense = _lineup(
        [
            (1, Position.PG, 74),
            (2, Position.C, 84),
            (3, Position.C, 84),
            (4, Position.SF, 79),
            (5, Position.SG, 76),
        ]
    )
    defense = _lineup(
        [
            (11, Position.PG, 73),
            (12, Position.C, 85),
            (13, Position.C, 82),
            (14, Position.SF, 80),
            (15, Position.SG, 77),
        ]
    )
    first = assign_matchups(offense, defense)
    second = assign_matchups(offense, defense)
    assert first == second


def test_assign_matchups_rejects_unequal_lineup_sizes():
    offense = _lineup([(1, Position.PG, 74)])
    defense = _lineup([(11, Position.PG, 73), (12, Position.SG, 76)])
    try:
        assign_matchups(offense, defense)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on unequal lineup sizes")


def test_every_action_type_defense_role_source_resolves_through_its_own_offense_roles():
    """Every defensive role slot named in `_DEFENSE_ROLE_SOURCE` for a
    given action type must source from an offensive role that action
    type's own template actually names -- a table-authoring error
    (pointing a defense role at an offense role from a *different*
    template) would otherwise only surface as a KeyError deep inside
    `defense_roles_from_matchups` at possession-resolution time."""
    offense = _lineup(
        [
            (1, Position.PG, 74),
            (2, Position.SG, 77),
            (3, Position.SF, 79),
            (4, Position.PF, 82),
            (5, Position.C, 85),
        ]
    )
    defense = _lineup(
        [
            (11, Position.PG, 73),
            (12, Position.SG, 76),
            (13, Position.SF, 80),
            (14, Position.PF, 83),
            (15, Position.C, 86),
        ]
    )
    matchups = assign_matchups(offense, defense)
    for action_type in ActionType:
        template = ROLE_TEMPLATES[action_type]
        offense_roles = {role: offense[i].id for i, role in enumerate(template.offense)}
        defense_roles = defense_roles_from_matchups(
            action_type, offense_roles, matchups, defense
        )
        # Every resolved defensive role slot must be one of the template's
        # own declared defense roles.
        assert set(defense_roles).issubset(set(template.defense))
        # Every value is a genuine defender id from this lineup.
        assert set(defense_roles.values()).issubset({p.id for p in defense})


def test_rim_protector_slot_is_the_best_rim_protector_in_every_template():
    """BDA-4.7 (docs/design/block-defender-attribution.md, Option C):
    every action type's `RoleSlot.RIM_PROTECTOR` must resolve to
    `composites.best_rim_protector`, not a matchup-derived id -- including
    `transition`, which used to resolve it as `matchups[TRAILER]` (the
    misnomer BDA-2 fact 3 names)."""
    offense = _lineup(
        [
            (1, Position.PG, 74),
            (2, Position.SG, 77),
            (3, Position.SF, 79),
            (4, Position.PF, 82),
            (5, Position.C, 85),
        ]
    )
    defense = tuple(
        make_sim_player(
            pid,
            tuning=DEFAULT_TUNING,
            primary_position=pos,
            height_in=height,
            # Elite on every field either block vector reads (rim protection,
            # length, lift, and the rotation/help-IQ arrival terms), so he is
            # the floor's best whichever vector `rim_protection_contribution`
            # reads (block-attribution-v2 BA-2.4 moved it).
            ratings_overrides=(
                {"rim_protection": 95, "vertical": 92, "rotation_speed": 90, "help_iq": 90}
                if pid == 15
                else None
            ),
            **({"wingspan_in": 92} if pid == 15 else {}),
        )
        for pid, pos, height in [
            (11, Position.PG, 73),
            (12, Position.SG, 76),
            (13, Position.SF, 80),
            (14, Position.PF, 83),
            (15, Position.C, 86),
        ]
    )
    expected = best_rim_protector(defense)
    # The elite rim protector is the tallest player at a distinct position
    # (center) -- `assign_matchups`'s exact-position pass pairs him with
    # the offense's own center (id 5), never with the trailer (whichever
    # offense role a given template names), so this fixture also proves
    # the slot is NOT silently still matchup-derived for `transition`.
    assert expected.id == 15
    matchups = assign_matchups(offense, defense)

    for action_type in ActionType:
        template = ROLE_TEMPLATES[action_type]
        offense_roles = {role: offense[i].id for i, role in enumerate(template.offense)}
        defense_roles = defense_roles_from_matchups(action_type, offense_roles, matchups, defense)
        assert defense_roles[RoleSlot.RIM_PROTECTOR] == expected.id


def test_role_templates_use_a_distinct_role_slot_within_each_side():
    """Distinctness of the *template itself* -- no action type accidentally
    lists the same `RoleSlot` twice on one side, which would make
    sequential role assignment silently overwrite one role's pick with
    another's."""
    for action_type, template in ROLE_TEMPLATES.items():
        assert len(template.offense) == len(set(template.offense)), action_type
        assert len(template.defense) == len(set(template.defense)), action_type
