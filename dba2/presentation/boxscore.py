"""`/box <game>`'s panel builder (docs/design/phase-5-discord-
presentation.md §11 Stage B step 6, §7.1's `/box` row: "column list from
finding 14" — the column set `tools/box_score_visual.py`'s own
`_PLAYER_COLUMNS` already established for the identical `_BoxScoreColumns`
shape, mirrored here rather than re-derived: MIN, PTS, FG (rim+mid, split
from 3P per that tool's own convention), 3P, FT, REB (`oreb + dreb`), AST,
STL, BLK, TOV, PF.

**Decision P5-5, applied to a box score rather than a standings row.**
Combining `fgm_rim + fgm_mid` into one "FG" column, or `oreb + dreb` into
"REB," is NOT a second implementation of a service-derived fact —
`dba2.domain.stats._BoxScoreColumns`'s own docstring says totals are
"summed across zones at read time" by design, and `tools/box_score_
visual.py` already sums them the identical way for its own HTML box
score. There is no service anywhere in this codebase that derives a
combined FG or REB total for this module to diverge from; the split IS
the stored fact, and the sum is display arithmetic on it, not a rival
derivation of a number a service already owns.

Every other number here (`PlayerGameStats`/`TeamGameStats`/
`GamePeriodScore`/`Game.home_score`/`away_score`) is a stored column, read
verbatim, never recomputed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from dba2.domain.game import Game
from dba2.domain.player import Player
from dba2.domain.stats import GamePeriodScore, PlayerGameStats, TeamGameStats
from dba2.domain.team import Team
from dba2.presentation.panel import Panel, PanelColor, PanelField

_MAX_PLAYER_NAME_CHARS = 18


def _display_player_name(player: Player | None, player_id: int) -> str:
    if player is None:
        full = f"Player {player_id}"
    else:
        full = f"{player.first_name} {player.last_name}"
    if len(full) <= _MAX_PLAYER_NAME_CHARS:
        return full
    return full[: _MAX_PLAYER_NAME_CHARS - 1] + "…"


def _fg(made_rim: int, att_rim: int, made_mid: int, att_mid: int) -> str:
    return f"{made_rim + made_mid}-{att_rim + att_mid}"


def _player_row(line: PlayerGameStats, players: Mapping[int, Player]) -> str:
    name = _display_player_name(players.get(line.player_id), line.player_id)
    reb = line.oreb + line.dreb
    fg = _fg(line.fgm_rim, line.fga_rim, line.fgm_mid, line.fga_mid)
    three = f"{line.fgm_three}-{line.fga_three}"
    ft = f"{line.ftm}-{line.fta}"
    return (
        f"{name:<{_MAX_PLAYER_NAME_CHARS}} {float(line.minutes_played):>4.1f} "
        f"{line.points:>3} {fg:>6} {three:>5} {ft:>5} {reb:>3} {line.ast:>3} "
        f"{line.stl:>2} {line.blk:>2} {line.tov:>2} {line.pf:>2}"
    )


def _team_totals_row(label: str, line: TeamGameStats) -> str:
    reb = line.oreb + line.dreb
    fg = _fg(line.fgm_rim, line.fga_rim, line.fgm_mid, line.fga_mid)
    three = f"{line.fgm_three}-{line.fga_three}"
    ft = f"{line.ftm}-{line.fta}"
    return (
        f"{label:<{_MAX_PLAYER_NAME_CHARS}}      "
        f"{line.points:>3} {fg:>6} {three:>5} {ft:>5} {reb:>3} {line.ast:>3} "
        f"{line.stl:>2} {line.blk:>2} {line.tov:>2} {line.pf:>2}"
    )


def _team_block(
    team: Team,
    player_lines: Sequence[PlayerGameStats],
    team_totals: TeamGameStats | None,
    players: Mapping[int, Player],
) -> list[str]:
    header = f"{team.code} — {team.city} {team.name}"
    column_header = (
        f"{'Player':<{_MAX_PLAYER_NAME_CHARS}}  MIN PTS     FG    3P    FT REB AST ST BL TO PF"
    )
    lines = [header, column_header]
    for line in sorted(player_lines, key=lambda p: p.minutes_played, reverse=True):
        lines.append(_player_row(line, players))
    if team_totals is not None:
        lines.append(_team_totals_row("TEAM", team_totals))
    return lines


def _period_line(period_scores: Sequence[GamePeriodScore], team_id: int, team_code: str) -> str:
    by_period = {p.period: p.points for p in period_scores if p.team_id == team_id}
    if not by_period:
        return f"{team_code:<4} --"
    ordered = " ".join(str(by_period[p]) for p in sorted(by_period))
    return f"{team_code:<4} {ordered}"


def build_boxscore_panel(
    *,
    game: Game,
    home_team: Team,
    away_team: Team,
    home_player_stats: Sequence[PlayerGameStats],
    away_player_stats: Sequence[PlayerGameStats],
    home_team_stats: TeamGameStats | None,
    away_team_stats: TeamGameStats | None,
    period_scores: Sequence[GamePeriodScore],
    players: Mapping[int, Player],
) -> Panel:
    """`players` maps EVERY player id appearing in either stat line to its
    `Player` row (both teams' rosters, pre-resolved by `view_service` —
    this module does no lookup of its own). A missing id falls back to
    `"Player <id>"` rather than raising, matching this project's "absent
    is neutral" convention (a purged/retired player's historical box-score
    line should still render)."""
    lines: list[str] = []
    lines.extend(_team_block(away_team, away_player_stats, away_team_stats, players))
    lines.append("")
    lines.extend(_team_block(home_team, home_player_stats, home_team_stats, players))
    description = "```\n" + "\n".join(lines) + "\n```"

    period_text = "\n".join(
        (
            _period_line(period_scores, away_team.id, away_team.code),
            _period_line(period_scores, home_team.id, home_team.code),
        )
    )
    fields = (
        PanelField(name="Line Score", value=f"```\n{period_text}\n```", inline=False),
    )

    home_score = game.home_score if game.home_score is not None else "-"
    away_score = game.away_score if game.away_score is not None else "-"
    title = f"{away_team.code} {away_score} @ {home_team.code} {home_score}"

    return Panel(title=title, description=description, fields=fields, color=PanelColor.NEUTRAL)
