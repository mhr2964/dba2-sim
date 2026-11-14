"""`ensure_command_legal` -- the real wiring of `command_gates.is_command_
legal` into a live command handler, absent from this codebase until now
(Decision P5-17: "every command ships with a `COMMAND_GATES` entry in the
same commit that adds it"; §1 finding 3: "an unregistered command should
fail loudly," `KeyError`, by design).

`CommandNotLegalNow`/`NotCommissioner` are `bot/`-only exceptions --
UX-shaped rejections a cog's `on_error` (or the command itself) turns
into an ephemeral reply, never something a service layer raises (the
service layer's own authoritative check, per Decision P5-38, raises
`dba2.phase.permissions.PermissionDenied`; this module's checks are the
bot-layer-first half of that same split, "an immediate ephemeral
rejection naming why," never the only gate).
"""
from __future__ import annotations

from dba2.domain.league import League
from dba2.domain.phase import Phase
from dba2.domain.team import Team
from dba2.phase import command_gates, permissions


class CommandNotLegalNow(Exception):
    """The command exists and is registered, but `league.current_phase`
    is not in its `COMMAND_GATES` set."""


def ensure_command_legal(command: str, phase: Phase) -> None:
    """Raises `CommandNotLegalNow` (a bot-layer UX rejection) instead of
    letting `command_gates.is_command_legal`'s own `KeyError` -- an
    unregistered-command bug, not a legal-phase-mismatch -- reach a
    Discord interaction handler and look like a legal rejection. A
    `KeyError` here is a real programming error (a command shipped with
    no gate entry, exactly what Decision P5-17 exists to prevent) and is
    deliberately left to propagate rather than caught and reworded."""
    if not command_gates.is_command_legal(command, phase):
        raise CommandNotLegalNow(
            f"/{command.replace('_', '-')} is not available during {phase.value} "
            f"(legal phases: {sorted(p.value for p in command_gates.COMMAND_GATES[command])})"
        )


def ensure_commissioner(user_id: int, league: League, *, action: str) -> None:
    """The bot-layer half of Decision P5-38's split -- calls the same
    pure predicate `league_service.assign_manager`/`remove_manager`'s
    own service-layer check uses (`dba2.phase.permissions.require_
    commissioner`), so a rejection here and a rejection at the service
    boundary are never two different implementations of "is this the
    commissioner." Re-raises `permissions.PermissionDenied` unchanged --
    this function exists for the single shared call site, not a second
    error type."""
    permissions.require_commissioner(
        user_id, commissioner_user_id=league.commissioner_user_id, action=action
    )


def ensure_can_act_for_team(user_id: int, league: League, team: Team, *, action: str) -> None:
    """The bot-layer half of P5-38's split for Stage D''s roster-decision
    commands -- a team's own manager, or the league's commissioner acting
    on any team's behalf (the P5-48 override precedent). Calls the same
    pure predicate `gm_command_service`'s own service-layer check uses
    (`dba2.phase.permissions.require_can_act_for_team`), so a rejection
    here and a rejection at the service boundary are never two different
    implementations of "who may act for this team" -- the identical
    single-sourcing `ensure_commissioner` already establishes for the
    commissioner-only half of this same split."""
    permissions.require_can_act_for_team(
        user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=team.manager_user_id,
        team_id=team.id,
    )


def ensure_team_manager(user_id: int, league: League, team: Team, *, action: str) -> None:
    """The bot-layer half of P5-38's split for the four trade actions that
    bind a second team's assets (propose, accept, reject, counter) -- the
    team's own manager only, NOT the commissioner-or-manager union
    `ensure_can_act_for_team` allows (docs/design/phase-5-trade-offer-review-
    surface.md §9, B3). Calls the same pure predicate the service layer's
    authoritative check uses (`dba2.phase.permissions.require_team_manager`),
    so a rejection here and one at the service boundary are never two
    implementations of "who may bind this team". Re-raises `PermissionDenied`
    unchanged; a non-owning commissioner's message points at `/veto-offer`."""
    permissions.require_team_manager(
        user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=team.manager_user_id,
        team_id=team.id,
        action=action,
    )
