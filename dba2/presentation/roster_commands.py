"""Confirmation panels for Stage D''s human roster-decision commands
(docs/design/phase-5-discord-presentation.md §23 D'-17, §27's confirmed
"all six of JC-4's roster decisions," Decision P5-40's own "gate, guard,
permission check AND panel, in the commit that adds the command").

`/waive`/`/release`/`/sign` reuse `view_service.get_team_panel` directly
(the updated roster IS the confirmation, Decision P5-5's own "don't
re-derive a fact a service already derived" -- a roster move's own proof
is the roster) and have no builder here. `team_strategy`/`player_roles`/
`player_directives` have no existing panel to reuse, so this module adds
the three small ones those three commands need -- each a thin, direct
field mapping over an already-written row, computing nothing.
"""
from __future__ import annotations

from dba2.domain.player import Player
from dba2.domain.role import PlayerDirective, PlayerRole
from dba2.domain.team import Team, TeamStrategy
from dba2.presentation.panel import Panel, PanelColor, PanelField


def build_strategy_panel(*, team: Team, strategy: TeamStrategy) -> Panel:
    fields = (
        PanelField(
            name="Pace / Schemes",
            value=(
                f"Pace target: {strategy.pace_target}\n"
                f"Offense: {strategy.offensive_scheme.value}\n"
                f"Defense: {strategy.defensive_scheme.value}"
            ),
            inline=True,
        ),
        PanelField(
            name="Tendencies",
            value=(
                f"Help aggression: {strategy.help_aggression}\n"
                f"3PT bias: {strategy.three_point_bias}\n"
                f"Crash OREB: {strategy.crash_oreb_bias}"
            ),
            inline=True,
        ),
    )
    return Panel(
        title=f"{team.city} {team.name} -- Strategy set (season {strategy.season})",
        fields=fields,
        color=PanelColor.NEUTRAL,
    )


def build_role_panel(*, team: Team, player: Player, role: PlayerRole) -> Panel:
    # RC-20.4 (docs/design/auto-gm-role-construction.md): `/gm role`'s
    # parameter is minutes, not a 0.0-1.0 share, so the confirmation panel
    # renders `touch_share x 240` back into minutes for the same reason.
    minutes = float(role.touch_share) * 240.0
    fields = (
        PanelField(
            name="Role",
            value=f"{role.role} -- target {minutes:.1f} mpg",
            inline=True,
        ),
        PanelField(name="Locked", value="yes" if role.locked else "no", inline=True),
    )
    return Panel(
        title=f"{player.first_name} {player.last_name} -- role set ({team.city} {team.name})",
        fields=fields,
        color=PanelColor.NEUTRAL,
    )


def build_gm_status_panel(
    *, team: Team, strategy: TeamStrategy | None, parked_offers_addressed_to_team: int
) -> Panel:
    """`/gm status` (GM-9) -- team-WIDE settings only (per-player role/
    directive detail is `/gm player`'s job, GM-10: thirty players will
    not fit a `PanelField`'s 1024-character budget). Reads only already-
    fetched values and computes nothing (Decision P5-5) -- the parked-
    offer count arrives as a plain `int` from `gm_command_service.get_gm_
    status`, not derived here."""
    strategy_lines = (
        "No strategy set yet for this season."
        if strategy is None
        else (
            f"Pace target: {strategy.pace_target}\n"
            f"Offense: {strategy.offensive_scheme.value}\n"
            f"Defense: {strategy.defensive_scheme.value}\n"
            f"Help aggression: {strategy.help_aggression}\n"
            f"3PT bias: {strategy.three_point_bias}\n"
            f"Crash OREB: {strategy.crash_oreb_bias}"
        )
    )
    fields = [
        PanelField(name="Strategy", value=strategy_lines, inline=False),
        PanelField(
            name="Timeline",
            value=f"{team.timeline.value} (source: {team.timeline_source.value})",
            inline=True,
        ),
    ]
    if team.auto_pilot:
        fields.append(
            PanelField(
                name="Auto-pilot",
                value=f"ON -- philosophy: {team.gm_philosophy.value}",
                inline=True,
            )
        )
        offer_note = (
            " -- still yours to accept or reject, but /counter-offer is refused while "
            "auto-pilot is on"
            if parked_offers_addressed_to_team
            else ""
        )
    else:
        offer_note = ""
    fields.append(
        PanelField(
            name="Parked trade offers",
            value=f"{parked_offers_addressed_to_team}{offer_note}",
            inline=True,
        )
    )
    return Panel(
        title=f"{team.city} {team.name} -- GM status",
        fields=tuple(fields),
        color=PanelColor.NEUTRAL,
    )


def build_directive_panel(*, player: Player, directive: PlayerDirective) -> Panel:
    def _or_unset(value: str | None) -> str:
        return value if value is not None else "(unset)"

    # PD-6 drops `role_mode` (the column); PD-7 removes `clutch_mode` from
    # the command surface (the column and its enum stay, but nothing ever
    # writes it, so showing it here forever would be "(unset)" for a field
    # a GM cannot act on) -- matches `build_gm_player_panel`'s identical
    # three-field shape below.
    fields = (
        PanelField(
            name="Shot diet / Usage",
            value=f"{_or_unset(directive.shot_diet)} / {_or_unset(directive.usage_mode)}",
            inline=True,
        ),
        PanelField(
            name="Defense",
            value=_or_unset(directive.defense_mode),
            inline=True,
        ),
    )
    return Panel(
        title=f"{player.first_name} {player.last_name} -- directive set",
        fields=fields,
        color=PanelColor.NEUTRAL,
    )


def build_gm_player_panel(
    *, player: Player, role: PlayerRole | None, directive: PlayerDirective | None
) -> Panel:
    """`/gm player` (GM-10) -- the per-player read view `/gm status`
    deliberately excludes (GM-9: thirty players will not fit a
    `PanelField`'s 1024-character budget). Both `role` and `directive` are
    optional: a player nobody has ever set either for still gets a real
    panel naming that, not an error."""

    def _or_unset(value: str | None) -> str:
        return value if value is not None else "(unset)"

    role_value = (
        "No role set for this player."
        if role is None
        else (
            # RC-20.4 (docs/design/auto-gm-role-construction.md): same
            # touch_share-x-240-as-minutes rendering as `build_role_panel`,
            # for the same reason -- a raw 0.0-1.0 share is not the unit
            # `/gm role` accepts anymore.
            f"{role.role} -- target {float(role.touch_share) * 240.0:.1f} mpg "
            f"(locked: {'yes' if role.locked else 'no'})"
        )
    )
    # Only the three fields PD-* keeps (shot_diet/usage_mode/defense_mode)
    # are shown -- role_mode is dropped (PD-6) and clutch_mode leaves the
    # command surface (PD-7) in the same build, so this panel is written
    # against their final shape rather than showing two fields due to
    # disappear immediately after.
    directive_value = (
        "No directives set for this player."
        if directive is None
        else (
            f"Shot diet: {_or_unset(directive.shot_diet)}\n"
            f"Usage: {_or_unset(directive.usage_mode)}\n"
            f"Defense: {_or_unset(directive.defense_mode)}"
        )
    )
    fields = (
        PanelField(name="Role", value=role_value, inline=False),
        PanelField(name="Directives", value=directive_value, inline=False),
    )
    return Panel(
        title=f"{player.first_name} {player.last_name} -- GM view",
        fields=fields,
        color=PanelColor.NEUTRAL,
    )
