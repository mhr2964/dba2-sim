"""The running box score of a broadcast in progress (watch-v2 W-2).

A live scoreboard shows each team's top scorers *so far*. The engine's box
lines are final, and a plan keeps no beats, so the tally is rebuilt from the
`_PlayEvent`s each possession carries -- the same facts `boxscore._apply_outcome`
reads, applied by the same rules. Exactness is the point: a tally that drifted
from the real box score would print a leader board the final line then
contradicts. `tests/unit/presentation/test_live_box.py` holds it to
`result.player_box_lines` for every counting stat on the whole seed corpus.

**Events apply in `boxscore`'s own order, `[terminal, *credits]`**
(`boxscore.py:278`), which `_Possession.events` already is. The order is not
cosmetic: `_credit_personal_foul` stops counting at six, so the same fouler
credited twice in one possession reaches the cap on a different event
depending on the order.

**Minutes and plus-minus are not shown live.** They need the lineups, which a
plan does not carry; every other counting stat is exact.

**A tally is recomputed from the plan prefix every tick, never accumulated
across ticks** (`tally` takes the possessions to count), so a skipped frame
cannot skew it.

The module never imports `presentation/broadcast.py` at runtime (that module
imports this one); the possession and event types are only named for the type
checker.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dba2.sim.events import TerminalKind, Zone

if TYPE_CHECKING:
    from dba2.presentation.broadcast import _PlayEvent, _Possession

# Mirrors `boxscore._MAX_PERSONAL_FOULS`: a real rule, not a tunable.
_MAX_PERSONAL_FOULS = 6

COUNTING_STATS = (
    "points",
    "fga_rim",
    "fgm_rim",
    "fga_mid",
    "fgm_mid",
    "fga_three",
    "fgm_three",
    "fta",
    "ftm",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
)
"""The sixteen stats `PlayerBoxLine` carries that this tally reproduces."""


@dataclass
class LiveLine:
    """One player's running counting stats. Mutable only while `tally` builds
    it; treat a returned line as read-only. `is_home` is the side the player was
    credited on (the possession's offense for scoring and ball-handling stats,
    its defense for blocks, steals and fouls), which is what a team's leaders
    are grouped by."""

    player_id: int
    is_home: bool
    points: int = 0
    fga_rim: int = 0
    fgm_rim: int = 0
    fga_mid: int = 0
    fgm_mid: int = 0
    fga_three: int = 0
    fgm_three: int = 0
    fta: int = 0
    ftm: int = 0
    oreb: int = 0
    dreb: int = 0
    ast: int = 0
    stl: int = 0
    blk: int = 0
    tov: int = 0
    pf: int = 0

    @property
    def rebounds(self) -> int:
        return self.oreb + self.dreb


def _zone_fields(zone: Zone | None) -> tuple[str, str] | None:
    if zone is Zone.RIM:
        return "fga_rim", "fgm_rim"
    if zone is Zone.MID:
        return "fga_mid", "fgm_mid"
    if zone is Zone.THREE:
        return "fga_three", "fgm_three"
    return None


def _bump(line: LiveLine, name: str, amount: int = 1) -> None:
    setattr(line, name, getattr(line, name) + amount)


def _apply_event(lines: dict[int, LiveLine], event: _PlayEvent, *, offense_is_home: bool) -> None:
    """One event's contribution, rule for rule `boxscore._apply_outcome`: the
    branches below are in its order and read the same fields. An id the
    box score would credit but the event does not carry is skipped, not
    invented."""
    defense_is_home = not offense_is_home

    def credit(player_id: int | None, is_home: bool) -> LiveLine | None:
        if player_id is None:
            return None
        line = lines.get(player_id)
        if line is None:
            line = LiveLine(player_id=player_id, is_home=is_home)
            lines[player_id] = line
        return line

    def foul(player_id: int | None, is_home: bool) -> None:
        line = credit(player_id, is_home)
        if line is not None and line.pf < _MAX_PERSONAL_FOULS:
            line.pf += 1

    if event.kind is TerminalKind.MADE_FG:
        zone_fields = _zone_fields(event.zone)
        scorer = credit(event.scorer_id, offense_is_home)
        if scorer is not None and zone_fields is not None:
            fga_field, fgm_field = zone_fields
            scorer.points += 3 if event.zone is Zone.THREE else 2
            _bump(scorer, fga_field)
            _bump(scorer, fgm_field)
        if event.assisted and event.passer_id is not None:
            passer = credit(event.passer_id, offense_is_home)
            if passer is not None:
                passer.ast += 1
    elif event.kind is TerminalKind.MISSED_FG:
        zone_fields = _zone_fields(event.zone)
        shooter = credit(event.scorer_id, offense_is_home)
        if shooter is not None and zone_fields is not None:
            _bump(shooter, zone_fields[0])
        if event.blocked and event.blocking_defender_id is not None:
            blocker = credit(event.blocking_defender_id, defense_is_home)
            if blocker is not None:
                blocker.blk += 1

    if event.rebound_is_offensive is not None and event.rebounder_id is not None:
        rebounder = credit(
            event.rebounder_id, offense_is_home if event.rebound_is_offensive else defense_is_home
        )
        if rebounder is not None:
            if event.rebound_is_offensive:
                rebounder.oreb += 1
            else:
                rebounder.dreb += 1

    if event.kind is TerminalKind.TURNOVER_LIVE:
        turner = credit(event.turnover_player_id, offense_is_home)
        if turner is not None:
            turner.tov += 1
        if event.steal and event.stealing_defender_id is not None:
            stealer = credit(event.stealing_defender_id, defense_is_home)
            if stealer is not None:
                stealer.stl += 1
    elif event.kind is TerminalKind.TURNOVER_DEAD:
        if event.turnover_player_id is not None:
            turner = credit(event.turnover_player_id, offense_is_home)
            if turner is not None:
                turner.tov += 1
    elif event.kind is TerminalKind.OFFENSIVE_FOUL:
        fouler = credit(event.offensive_fouler_id, offense_is_home)
        if fouler is not None:
            fouler.tov += 1
            if fouler.pf < _MAX_PERSONAL_FOULS:
                fouler.pf += 1

    if event.kind in (TerminalKind.SHOOTING_FOUL, TerminalKind.NON_SHOOTING_FOUL) or (
        event.kind is TerminalKind.MADE_FG and event.and_one
    ):
        if event.fouler_id is not None:
            foul(event.fouler_id, defense_is_home)

    if event.ft_count > 0 and event.fouled_player_id is not None:
        fouled = credit(event.fouled_player_id, offense_is_home)
        if fouled is not None:
            fouled.fta += event.ft_count
            fouled.ftm += event.ftm
            fouled.points += event.ftm


def tally(possessions: Iterable[_Possession]) -> dict[int, LiveLine]:
    """Every player's running stats over `possessions`, in the order given.
    Pass the plan's prefix up to the frame being shown."""
    lines: dict[int, LiveLine] = {}
    for possession in possessions:
        for event in possession.events:
            _apply_event(lines, event, offense_is_home=possession.offense_is_home)
    return lines


def leaders(lines: Iterable[LiveLine], *, is_home: bool, count: int) -> Sequence[LiveLine]:
    """The top `count` scorers of one side, by `(-points, -rebounds, -assists,
    player_id)`. Only players who have scored: a board of zeros is not a board.

    This tie-break is the live component's own, and the component's stat
    fields (`presentation/broadcast.py::_period_stats_value`) are the final
    board -- there is no separate, "authoritative" Leaders field any more
    (watch-v4 T-2.c deletes `_leaders_value`, which sorted by
    `(-points, -minutes_played, player_id)`). A plan carries no minutes, so a
    points tie can order this component's leaders differently from `/box`'s;
    the points themselves are proven to agree (design W-2)."""
    scorers = (line for line in lines if line.is_home is is_home and line.points > 0)
    ranked = sorted(
        scorers, key=lambda line: (-line.points, -line.rebounds, -line.ast, line.player_id)
    )
    return ranked[:count]
