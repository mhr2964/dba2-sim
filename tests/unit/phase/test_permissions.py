"""docs/design/phase-5-discord-presentation.md §19 (Decision P5-38) --
the actor-role derivation and the two enforcement predicates it feeds,
exercised against the full three-role cross product: every combination
of (actor is the commissioner? actor is the team's manager? the team
has no manager at all?).
"""
from __future__ import annotations

import pytest

from dba2.phase.permissions import (
    ActorRole,
    PermissionDenied,
    actor_role_for_team,
    can_act_for_team,
    is_team_manager,
    require_can_act_for_team,
    require_commissioner,
    require_team_manager,
)

COMMISSIONER_ID = 111
MANAGER_ID = 222
SPECTATOR_ID = 333


def test_commissioner_who_does_not_manage_the_team_is_commissioner_role():
    role = actor_role_for_team(
        COMMISSIONER_ID, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=MANAGER_ID
    )
    assert role is ActorRole.COMMISSIONER


def test_manager_who_is_not_commissioner_is_team_manager_role():
    role = actor_role_for_team(
        MANAGER_ID, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=MANAGER_ID
    )
    assert role is ActorRole.TEAM_MANAGER


def test_commissioner_who_also_manages_the_team_resolves_to_commissioner():
    """Precedence: a commissioner is never demoted to team-manager just
    because they happen to manage the team being acted on."""
    role = actor_role_for_team(
        COMMISSIONER_ID,
        commissioner_user_id=COMMISSIONER_ID,
        team_manager_user_id=COMMISSIONER_ID,
    )
    assert role is ActorRole.COMMISSIONER


def test_neither_commissioner_nor_manager_is_spectator():
    role = actor_role_for_team(
        SPECTATOR_ID, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=MANAGER_ID
    )
    assert role is ActorRole.SPECTATOR


def test_unmanaged_team_and_commissioner_actor_is_still_commissioner():
    role = actor_role_for_team(
        COMMISSIONER_ID, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=None
    )
    assert role is ActorRole.COMMISSIONER


def test_unmanaged_team_and_non_commissioner_actor_is_spectator():
    """A team with `manager_user_id IS NULL` (every AI-controlled team)
    has no `TEAM_MANAGER` at all -- a non-commissioner is a spectator of
    it, never accidentally matched by a `None == None` comparison."""
    role = actor_role_for_team(
        SPECTATOR_ID, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=None
    )
    assert role is ActorRole.SPECTATOR


@pytest.mark.parametrize(
    ("user_id", "commissioner_user_id", "team_manager_user_id", "expected"),
    [
        (COMMISSIONER_ID, COMMISSIONER_ID, MANAGER_ID, True),
        (MANAGER_ID, COMMISSIONER_ID, MANAGER_ID, True),
        (COMMISSIONER_ID, COMMISSIONER_ID, COMMISSIONER_ID, True),
        (SPECTATOR_ID, COMMISSIONER_ID, MANAGER_ID, False),
        (COMMISSIONER_ID, COMMISSIONER_ID, None, True),
        (SPECTATOR_ID, COMMISSIONER_ID, None, False),
        (MANAGER_ID, COMMISSIONER_ID, None, False),
    ],
)
def test_can_act_for_team_cross_product(
    user_id: int, commissioner_user_id: int, team_manager_user_id: int | None, expected: bool
):
    assert (
        can_act_for_team(
            user_id,
            commissioner_user_id=commissioner_user_id,
            team_manager_user_id=team_manager_user_id,
        )
        is expected
    )


def test_require_can_act_for_team_is_silent_for_the_manager():
    require_can_act_for_team(
        MANAGER_ID,
        commissioner_user_id=COMMISSIONER_ID,
        team_manager_user_id=MANAGER_ID,
        team_id=7,
    )


def test_require_can_act_for_team_is_silent_for_the_commissioner_on_any_team():
    require_can_act_for_team(
        COMMISSIONER_ID,
        commissioner_user_id=COMMISSIONER_ID,
        team_manager_user_id=MANAGER_ID,
        team_id=7,
    )


def test_require_can_act_for_team_raises_for_a_spectator():
    """The must-fire case: a check that silently matches nothing (never
    raises) is worse than no check at all."""
    with pytest.raises(PermissionDenied, match=r"user 333 may not act for team 7"):
        require_can_act_for_team(
            SPECTATOR_ID,
            commissioner_user_id=COMMISSIONER_ID,
            team_manager_user_id=MANAGER_ID,
            team_id=7,
        )


def test_require_can_act_for_team_raises_for_the_manager_of_a_different_team():
    """A team's own manager has no authority over a different team --
    `team_manager_user_id` must be the ACTED-ON team's manager, and a
    caller that passes the wrong team's value is exactly the bug this
    test would catch."""
    other_team_manager = 444
    with pytest.raises(PermissionDenied):
        require_can_act_for_team(
            other_team_manager,
            commissioner_user_id=COMMISSIONER_ID,
            team_manager_user_id=MANAGER_ID,
            team_id=7,
        )


def test_require_commissioner_is_silent_for_the_commissioner():
    require_commissioner(COMMISSIONER_ID, commissioner_user_id=COMMISSIONER_ID, action="setup")


def test_require_commissioner_raises_for_a_team_manager():
    """`can_act_for_team`'s COMMISSIONER-or-TEAM_MANAGER union does not
    apply here on purpose -- a team's own manager must not be able to
    reassign themselves or another team (Decision P5-41)."""
    with pytest.raises(PermissionDenied, match=r"user 222 is not the league's commissioner"):
        require_commissioner(
            MANAGER_ID, commissioner_user_id=COMMISSIONER_ID, action="assign_manager"
        )


def test_require_commissioner_raises_for_a_spectator():
    with pytest.raises(PermissionDenied):
        require_commissioner(
            SPECTATOR_ID, commissioner_user_id=COMMISSIONER_ID, action="assign_manager"
        )


# ---------------------------------------------------------------------------
# Trade permissions (docs/design/phase-5-trade-offer-review-surface.md §9, B3,
# §7.4 test 12's unit half): manager-only, and defined off the FACT, never a
# role -- because `actor_role_for_team` resolves a commissioner first.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "team_manager_user_id", "expected"),
    [
        (MANAGER_ID, MANAGER_ID, True),
        (SPECTATOR_ID, MANAGER_ID, False),
        (MANAGER_ID, None, False),
        # `None == None` must never make an unmanaged team "managed by" a
        # caller whose id happens to be falsy or absent.
        (0, None, False),
    ],
)
def test_is_team_manager_is_true_only_for_the_teams_own_manager(
    user_id: int, team_manager_user_id: int | None, expected: bool
):
    assert is_team_manager(user_id, team_manager_user_id=team_manager_user_id) is expected


def test_actor_role_for_team_agrees_with_is_team_manager_on_the_whole_cross_product():
    """`actor_role_for_team` delegates its ownership comparison to
    `is_team_manager` -- one implementation of "who owns this team". Where the
    commissioner does not shadow the answer, the two must agree exactly."""
    for user in (MANAGER_ID, SPECTATOR_ID, 0):
        for manager in (MANAGER_ID, COMMISSIONER_ID, None):
            role = actor_role_for_team(
                user, commissioner_user_id=COMMISSIONER_ID, team_manager_user_id=manager
            )
            assert (role is ActorRole.TEAM_MANAGER) is is_team_manager(
                user, team_manager_user_id=manager
            )


def _require(user_id: int, *, manager: int | None) -> None:
    require_team_manager(
        user_id,
        commissioner_user_id=COMMISSIONER_ID,
        team_manager_user_id=manager,
        team_id=7,
        action="offer_respond",
    )


def test_require_team_manager_allows_the_teams_own_manager():
    _require(MANAGER_ID, manager=MANAGER_ID)


def test_require_team_manager_allows_a_commissioner_who_also_manages_the_team():
    """The B3 case. A `role is TEAM_MANAGER` predicate returns False here
    because `actor_role_for_team` returns COMMISSIONER for this user -- and
    locks the league's most likely trader out of their own team."""
    assert (
        actor_role_for_team(
            COMMISSIONER_ID,
            commissioner_user_id=COMMISSIONER_ID,
            team_manager_user_id=COMMISSIONER_ID,
        )
        is ActorRole.COMMISSIONER
    )
    _require(COMMISSIONER_ID, manager=COMMISSIONER_ID)


def test_require_team_manager_denies_a_non_owning_commissioner_and_points_at_veto():
    with pytest.raises(PermissionDenied, match=r"commissioner.*use /veto-offer"):
        _require(COMMISSIONER_ID, manager=MANAGER_ID)


def test_require_team_manager_denies_a_spectator_and_another_teams_manager():
    for user in (SPECTATOR_ID, 444):
        with pytest.raises(PermissionDenied, match=r"is not the manager of team 7"):
            _require(user, manager=MANAGER_ID)


def test_require_team_manager_denies_everyone_including_the_commissioner_on_an_unmanaged_team():
    """`team_manager_user_id is None` is false for everybody: the AI answers
    for an unmanaged team, so no human decides for it (P5-44/P5-55)."""
    for user in (COMMISSIONER_ID, MANAGER_ID, SPECTATOR_ID):
        with pytest.raises(PermissionDenied):
            _require(user, manager=None)


def test_only_the_commissioner_is_told_about_veto():
    """`commissioner_user_id` is used for exactly one thing: the message. A
    non-commissioner must not be pointed at /veto-offer."""
    with pytest.raises(PermissionDenied) as spectator:
        _require(SPECTATOR_ID, manager=MANAGER_ID)
    assert "veto" not in str(spectator.value)
