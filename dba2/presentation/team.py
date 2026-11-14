"""`/team <team>`'s panel builder (docs/design/phase-5-discord-
presentation.md §11 Stage B step 6, §7.1's `/team` row: "roster + cap +
record").

**Decision P5-6/P5-5, the same shape `standings.py` documents.** The
team's record/streak comes from a real `TeamPulseState`
(`media_service.compute_current_pulse_snapshot`), passed here as three
plain `int`s rather than the snapshot type itself, because this module
cannot import `dba2.media` (Decision P4-31's one-way valve) any more than
`standings.py` can. Cap numbers come from `cap_service.get_cap_summary`'s
own `CapSummary` (a `dba2.domain.league` dataclass — presentation MAY
import `dba2.domain` freely, so this one rides through unmodified,
field-for-field). A player's overall comes from `Player.overall_cached`
(`ratings_service.recompute_overall`'s one named cache, Decision 12) —
never recomputed from ratings here.
"""
from __future__ import annotations

from collections.abc import Sequence

from dba2.domain.league import CapSummary
from dba2.domain.player import Player
from dba2.domain.team import Team
from dba2.presentation.panel import Panel, PanelColor, PanelField

# Same reasoning as `standings.py`'s own constant: bounds a 15-man roster
# table inside `Panel.description` regardless of real name length (§9.2's
# adversarial "15-field roster of maximum-length names" case).
_MAX_PLAYER_NAME_CHARS = 22


def _display_player_name(player: Player) -> str:
    full = f"{player.first_name} {player.last_name}"
    if len(full) <= _MAX_PLAYER_NAME_CHARS:
        return full
    return full[: _MAX_PLAYER_NAME_CHARS - 1] + "…"


def _streak_text(streak_length: int) -> str:
    if streak_length == 0:
        return "-"
    if streak_length > 0:
        return f"W{streak_length}"
    return f"L{-streak_length}"


def build_team_panel(
    *,
    team: Team,
    wins: int,
    losses: int,
    streak_length: int,
    standings_position: int,
    roster: Sequence[Player],
    cap_summary: CapSummary,
) -> Panel:
    """`roster` is rendered sorted by `overall_cached` descending, then
    `last_name` ascending for a deterministic tie-break — a display
    ordering choice over already-given `Player` rows, not a re-derivation
    of any of their fields (Decision P5-5's own distinction: sorting a
    list is not computing a fact)."""
    ordered_roster = sorted(roster, key=lambda p: (-p.overall_cached, p.last_name, p.id))
    lines = ["Pos  OVR  Player"]
    for player in ordered_roster:
        lines.append(
            f"{player.primary_position.value:<4} {player.overall_cached:>3}  "
            f"{_display_player_name(player)}"
        )
    description = "```\n" + "\n".join(lines) + "\n```"

    fields = (
        PanelField(
            name="Record",
            value=f"{wins}-{losses} ({_streak_text(streak_length)}), #{standings_position}",
            inline=True,
        ),
        PanelField(
            name="Conference / Division",
            value=f"{team.conference} / {team.division}",
            inline=True,
        ),
        PanelField(
            name="Cap Room",
            value=(
                f"Used: {cap_summary.used:,} / Cap: {cap_summary.salary_cap:,}\n"
                f"Room: {cap_summary.room:,}"
            ),
            inline=True,
        ),
        # Decision P5-68's own panel requirement: the source must be shown
        # alongside the value, or an auto classification and a human pin
        # render identically and the override becomes invisible.
        PanelField(
            name="Timeline",
            value=f"{team.timeline.value} ({team.timeline_source.value})",
            inline=True,
        ),
    )

    title = f"{team.city} {team.name}"
    return Panel(title=title, description=description, fields=fields, color=PanelColor.NEUTRAL)
