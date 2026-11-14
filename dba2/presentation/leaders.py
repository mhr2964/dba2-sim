"""`/leaders`'s panel builder -- v2 per docs/design/leaders-v2-per-game-and-
paging.md (revision 2), replacing leaders-live.md's single-mode, unpaged
panel. Still `presentation/`-pure: no `discord` import, no repository/service
call, every input already resolved by `view_service.get_leaders_panel`.

**LD2-5's 47-character line, exact.** Six fields, two spaces between each,
nothing else: `rank` (3, right), `name` (18, left, truncated to 17 chars +
`…` past that), `team` (3, left, sliced), `per_game` (5, right, `.1f`),
`total` (5, right), `games` (3, right) -- `3+18+3+5+5+3 = 37` plus five
2-space separators (`10`) is `47`. Header and every row share the exact same
join, so the header can never drift out of alignment with its own columns.

**The footer, not the description, carries the qualification rule
(LD2-5).** `Panel.footer` is a real, separate field (`presentation/panel.py`)
-- the code block itself stays exactly 47 characters wide on every line,
mobile-safe, and the exclusion rule/team-games range/rank span live outside
it where Discord renders them in the embed's own footer text.

**Two distinct empty branches (LD2-6), both told apart by `max_team_games`
alone, never by parsing a message string:**
1. No final games at all (`max_team_games is None`) -- unchanged from
   leaders-live.md's LD-2 sentence, no footer at all (there is nothing to
   qualify against yet).
2. Games exist but nobody clears the bar (`rows` empty, `max_team_games` is
   not `None`) -- a new state LD2-2's bar introduces; the same footer rule
   and team-games range are shown (no `Rank` span -- there is no page).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.presentation.panel import Panel, PanelColor

_STAT_LABELS: dict[LeaderStat, str] = {
    LeaderStat.POINTS: "Points",
    LeaderStat.REBOUNDS: "Rebounds",
    LeaderStat.ASSISTS: "Assists",
    LeaderStat.STEALS: "Steals",
    LeaderStat.BLOCKS: "Blocks",
    LeaderStat.TURNOVERS: "Turnovers",
}

_NO_GAMES_YET = "No games played yet this season."
_NOBODY_QUALIFIES_YET = "No player has played enough games to qualify yet."

# LD2-5's field widths, named so the 47-character total is derivable from the
# constants themselves rather than a hand-summed magic number.
_RANK_W = 3
_NAME_W = 18
_TEAM_W = 3
_PER_GAME_W = 5
_TOTAL_W = 5
_GAMES_W = 3
_COLUMN_SEP = "  "  # exactly two spaces, LD2-5


@dataclass(frozen=True)
class LeaderRow:
    """One ranked player -- `stat_total`/`games_played` are `StatLeaderRow`'s
    own measured values, copied verbatim (Decision P5-5: this module
    computes the per-game average shown below, which is formatting of an
    already-measured pair, never a re-derivation of the total or the game
    count themselves). `player_name`/`team_code` are read once, by
    `view_service`, and passed in already resolved -- this module never
    looks a player or a team up. `rank` is `StatLeaderRow.rank` verbatim
    (LD2-6) -- the SQL `ROW_NUMBER`, so page 2 reads absolute ranks (11-20),
    never 1-10 again."""

    player_id: int
    player_name: str
    team_code: str
    stat_total: int
    games_played: int
    rank: int


def _name_field(name: str) -> str:
    """LD2-5: longer than 18 -> first 17 characters + `…`, the same
    `…`-slice convention the v1 builder already used."""
    if len(name) > _NAME_W:
        name = name[: _NAME_W - 1] + "…"
    return f"{name:<{_NAME_W}}"


def _team_field(team_code: str) -> str:
    """LD2-5 minor 4: sliced to 3, never padded-without-truncating (the v1
    bug that was real but unreachable at today's 3-character codes)."""
    return f"{team_code[:_TEAM_W]:<{_TEAM_W}}"


def _header_line() -> str:
    return _COLUMN_SEP.join(
        [
            f"{'Rk':>{_RANK_W}}",
            f"{'Player':<{_NAME_W}}",
            f"{'Tm':<{_TEAM_W}}",
            f"{'PG':>{_PER_GAME_W}}",
            f"{'Total':>{_TOTAL_W}}",
            f"{'G':>{_GAMES_W}}",
        ]
    )


def _row_line(row: LeaderRow) -> str:
    per_game = row.stat_total / row.games_played if row.games_played else 0.0
    return _COLUMN_SEP.join(
        [
            f"{row.rank:>{_RANK_W}}",
            _name_field(row.player_name),
            _team_field(row.team_code),
            f"{per_game:>{_PER_GAME_W}.1f}",
            f"{row.stat_total:>{_TOTAL_W}}",
            f"{row.games_played:>{_GAMES_W}}",
        ]
    )


def _qualifier_text(
    *, bar_applied: bool, qualify_fraction: float, min_team_games: int, max_team_games: int
) -> str:
    """LD2-2: "the bar is per player, so there is no single 'min games'
    number to print" -- states the RULE and the league's team-games range
    instead. TOTAL mode applies no bar at all, so its qualifier is simply
    "all players" (LD2-5's own mockup: "Total · all players · Rank 1-10")."""
    if not bar_applied:
        return "all players"
    pct = round(qualify_fraction * 100)
    return (
        f"Qualified: {pct}% of own team's games · "
        f"Teams: {min_team_games}–{max_team_games} played"
    )


def build_leaders_panel(
    *,
    league_name: str,
    season: int,
    stat: LeaderStat,
    mode: LeaderMode,
    rows: Sequence[LeaderRow],
    bar_applied: bool,
    qualify_fraction: float,
    min_team_games: int | None,
    max_team_games: int | None,
) -> Panel:
    label = _STAT_LABELS[stat]
    title = f"{league_name} — {label} Leaders — Season {season}"
    mode_label = "Per Game" if mode is LeaderMode.PER_GAME else "Total"

    if max_team_games is None:
        # LD2-6 case 1: no final games at all -- unchanged from leaders-live.md
        # LD-2. No footer: there is nothing yet to state a qualification rule about.
        return Panel(title=title, description=_NO_GAMES_YET, color=PanelColor.NEUTRAL)

    qualifier = _qualifier_text(
        bar_applied=bar_applied,
        qualify_fraction=qualify_fraction,
        min_team_games=min_team_games,
        max_team_games=max_team_games,
    )

    if not rows:
        # LD2-6 case 2: games exist, but nobody clears the bar. Same qualifier,
        # no `Rank` span -- there is no page to state one for.
        return Panel(
            title=title,
            description=_NOBODY_QUALIFIES_YET,
            color=PanelColor.NEUTRAL,
            footer=f"{mode_label} · {qualifier}",
        )

    lines = [_header_line(), *(_row_line(row) for row in rows)]
    description = "```\n" + "\n".join(lines) + "\n```"
    footer = f"{mode_label} · {qualifier} · Rank {rows[0].rank}–{rows[-1].rank}"
    return Panel(title=title, description=description, color=PanelColor.NEUTRAL, footer=footer)
