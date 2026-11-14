"""Authorization -- the missing half of `command_gates.py`'s question.

docs/design/phase-5-discord-presentation.md §19 (Decision P5-38): nothing
in this codebase has ever authorized anything, because until Phase 5
nothing has ever had a user. `command_gates.is_command_legal(command,
phase)` answers "is this command legal *now*"; this module answers "is
this actor allowed to run it *for this team*." One `ActorRole` result
(`COMMISSIONER`, `TEAM_MANAGER`, `SPECTATOR`) derived from three values
this schema already stores -- `(user_id, league.commissioner_user_id,
team.manager_user_id)` -- no new column, no I/O, pure.

Enforced twice, per the design's own split: the bot layer checks first
(UX -- an immediate ephemeral rejection naming why); the service layer
checks again, and that check is the authoritative one. Every Phase 5
service function performing a human-initiated write takes an
`actor_user_id` and calls `require_can_act_for_team` before writing.
Both layers call this module's own predicates; there is no second
implementation of "who owns this team," and a caller must never
re-derive the comparison inline (the same "ad hoc comparisons are not
permitted" rule `entity_states.py` states for status strings applies
here to actor identity).

Existing Phase 0-4 services are NOT retrofitted with this check --
`phase/service.advance` keeps its current contract; `/advance`'s
commissioner check lives in the Phase 5 orchestrator that calls it, not
inside a Phase 0 function whose signature four phases depend on. This is
a known, accepted asymmetry (P5-38's own text): after Phase 5, `advance`
is authorized when called through the bot and unauthorized when called
from a script, which is acceptable only because a script already runs
with database credentials.
"""
from __future__ import annotations

from enum import StrEnum


class ActorRole(StrEnum):
    COMMISSIONER = "commissioner"
    TEAM_MANAGER = "team_manager"
    SPECTATOR = "spectator"


class PermissionDenied(Exception):
    """Raised by the service-layer check. The bot layer's own rejection is
    UX only and must never be the sole gate -- a bot-layer-only check is
    one new call site (a `tools/` script written at 2am) away from being
    bypassed."""


def is_team_manager(user_id: int, *, team_manager_user_id: int | None) -> bool:
    """True iff `user_id` IS this team's manager -- decided off the fact
    (`teams.manager_user_id`), never off a resolved role.

    This is the single implementation of "does this user manage this team":
    `actor_role_for_team` calls it, so the ownership comparison still exists
    exactly once (the "no second implementation of who owns this team" rule
    in this module's docstring). It is deliberately NOT expressed as
    `actor_role_for_team(...) is TEAM_MANAGER`: that resolves a commissioner
    first, so a commissioner who also manages the team resolves to
    `COMMISSIONER` and such a predicate would lock them out of their own team
    -- in a league of friends, the single most likely user of a trade
    command. A team with no manager (`None`) is managed by nobody."""
    return team_manager_user_id is not None and user_id == team_manager_user_id


def actor_role_for_team(
    user_id: int,
    *,
    commissioner_user_id: int,
    team_manager_user_id: int | None,
) -> ActorRole:
    """`user_id`'s role with respect to one team in one league.

    Commissioner takes precedence when a user is both: a commissioner
    who also manages a team is still resolved as `COMMISSIONER`, because
    every commissioner-gated action (P5-48's stalled-pick override, a
    veto) must be legal for them regardless of which team they happen to
    manage. A commissioner who is *not* that team's manager is not
    silently treated as if they owned it -- `actor_role_for_team` never
    returns `TEAM_MANAGER` for a non-owning commissioner; it is
    `can_act_for_team` that grants the commissioner an override, as a
    named second case, not this function blurring the two roles
    together.
    """
    if user_id == commissioner_user_id:
        return ActorRole.COMMISSIONER
    if is_team_manager(user_id, team_manager_user_id=team_manager_user_id):
        return ActorRole.TEAM_MANAGER
    return ActorRole.SPECTATOR


def can_act_for_team(
    user_id: int,
    *,
    commissioner_user_id: int,
    team_manager_user_id: int | None,
) -> bool:
    """True iff `user_id` may perform a roster/team action on this team:
    its own manager, or the league's commissioner acting on any team's
    behalf (the P5-48 override precedent -- a commissioner unblocking a
    stalled draft pick through `apply_draft` is commissioner-gated
    authority, not the AI deciding for the team)."""
    role = actor_role_for_team(
        user_id,
        commissioner_user_id=commissioner_user_id,
        team_manager_user_id=team_manager_user_id,
    )
    return role in (ActorRole.COMMISSIONER, ActorRole.TEAM_MANAGER)


def require_commissioner(user_id: int, *, commissioner_user_id: int, action: str) -> None:
    """The check `assign_manager`/`remove_manager` (Decision P5-41) and
    every other commissioner-only, not-team-scoped action needs --
    `can_act_for_team`'s COMMISSIONER-or-TEAM_MANAGER union is too wide
    here: a team's own manager must not be able to reassign themselves
    or another team's manager, only the commissioner may. Named `action`
    (not `team_id`) because a commissioner-only action is not always
    about one team (`/setup`'s channel reconcile is league-wide)."""
    if user_id != commissioner_user_id:
        raise PermissionDenied(
            f"user {user_id} is not the league's commissioner (action: {action})"
        )


def require_can_act_for_team(
    user_id: int,
    *,
    commissioner_user_id: int,
    team_manager_user_id: int | None,
    team_id: int,
) -> None:
    """The service-layer call P5-38 requires before every human-initiated
    write. Raises rather than returns a bool: a service function that
    forgets to check a bool's return value fails open (the write
    happens); one that forgets to call this at all fails loudly at the
    next line that assumed the write had been authorized, which is a
    smaller and more visible blast radius than a silently-permitted
    write.
    """
    if not can_act_for_team(
        user_id,
        commissioner_user_id=commissioner_user_id,
        team_manager_user_id=team_manager_user_id,
    ):
        raise PermissionDenied(
            f"user {user_id} may not act for team {team_id} "
            f"(not its manager, not the league's commissioner)"
        )


def require_team_manager(
    user_id: int,
    *,
    commissioner_user_id: int,
    team_manager_user_id: int | None,
    team_id: int,
    action: str,
) -> None:
    """The service-layer check for the four trade actions that BIND A SECOND
    TEAM'S ASSETS -- propose, accept, reject, counter
    (docs/design/phase-5-trade-offer-review-surface.md §9): the team's own
    manager only. Narrower than `require_can_act_for_team`, and the narrowing
    is scoped to those four actions and nowhere else (`/offers` keeps the
    wider predicate, `/veto-offer` keeps `require_commissioner`).

    **Why trades are narrower than roster commands.** `/waive` is unilateral
    and touches one roster, so a commissioner override for an absent manager
    is right (JC-1). A trade is bilateral: a commissioner accepting for a
    human-managed team hands away someone else's players without their input,
    and accepting for an AI-managed team overrides `evaluate_trade` -- "a
    human decides for the AI", which P5-44/P5-55 both reject by name. Do not
    "fix" this back to the union.

    A commissioner who also manages the team IS the manager and is allowed:
    this asks `is_team_manager`, which never asks for a role.

    `commissioner_user_id` is used for exactly one thing -- the message. A
    non-owning commissioner is told why and pointed at `/veto-offer` instead
    of getting a generic denial; a future reader must not delete it as
    unused. Raises rather than returns a bool for
    `require_can_act_for_team`'s own reason: a forgotten bool check fails
    open."""
    if is_team_manager(user_id, team_manager_user_id=team_manager_user_id):
        return
    if user_id == commissioner_user_id:
        raise PermissionDenied(
            f"You are this league's commissioner, but trades are the team manager's "
            f"decision -- use /veto-offer to kill an offer (user {user_id}, team {team_id}, "
            f"action: {action})"
        )
    if team_manager_user_id is None:
        raise PermissionDenied(
            f"team {team_id} has no manager, so nobody may act for it; the AI answers for "
            f"an unmanaged team (user {user_id}, action: {action})"
        )
    raise PermissionDenied(
        f"user {user_id} is not the manager of team {team_id} (action: {action})"
    )
