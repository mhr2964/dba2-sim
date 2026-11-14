"""Pure accumulator: consumes every possession's `terminal` outcome AND
every entry in its `credit_events` (§4.5/§9 -- "a check that sums only
terminals is the exact bug class round 3 of the design re-verify already
caught once") into `PlayerBoxLine`/`TeamBoxLine`, with structural assists
per Decision P1-15 and plus-minus by diffing score against the on-court
lineup (phase-1-simulation-engine.md §3, §9, §12 step 5).

Zero I/O, per Decision P1-1. Takes a flat, already-built possession log
(`PossessionRecord`) rather than reaching into `state.py`/`game.py` for
anything live -- `game.py` is the only caller, and it owns turning its
own possession-by-possession loop into this log.

**Interpretive call, flagged in this dispatch's build report: minutes
played and plus-minus are both derived here, from the possession log
itself** (crediting every possession's `elapsed_seconds` and point swing
to the 10 players named in that possession's `offense_lineup`/
`defense_lineup`), **rather than read from `state.GameState`'s live
per-player counters.** `state.py`'s counters exist for `game.py`'s own
in-game decisions (rotation/fatigue/foul-trouble, all of which need a
*running* answer mid-game); the box line is a *final* answer computed
once, and deriving it from the same possession log this module already
owns for stat accumulation avoids two independently-maintained tallies
that could silently drift apart.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from dba2.sim.events import (
    PlayerBoxLine,
    PossessionResult,
    TeamBoxLine,
    TerminalKind,
    TerminalOutcome,
    Zone,
)


@dataclass(frozen=True)
class PossessionRecord:
    """One `simulate_possession` call's worth of bookkeeping, in the
    shape `boxscore.py` needs to accumulate stats, minutes, and
    plus-minus without reaching back into `state.py`/`context.py`."""

    offense_team_id: int
    defense_team_id: int
    offense_lineup: tuple[int, ...]
    defense_lineup: tuple[int, ...]
    elapsed_seconds: float
    result: PossessionResult


@dataclass
class _MutablePlayerLine:
    player_id: int
    team_id: int
    seconds_played: float = 0.0
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
    plus_minus: int = 0


@dataclass
class _MutableTeamLine:
    team_id: int
    possessions: int = 0
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


# Real NBA rule, not a tunable: a player is physically removed before a
# 7th personal foul is possible, so a box score's `pf` column never
# exceeds 6. Mirrors `tuning.possession.fouls_to_disqualify`'s value by
# definition -- duplicated as a fixed constant rather than importing
# `tuning` here, since this module's foul tally is built directly from
# the possession log (module docstring) and can otherwise legitimately
# count a 7th foul call on an already-disqualified defender: `game.py`'s
# rotation model only benches a disqualified player at the *next*
# possession boundary (real dead-ball substitution timing), so the same
# player can draw another foul within the very possession that
# disqualified them.
_MAX_PERSONAL_FOULS = 6


def _credit_personal_foul(line: _MutablePlayerLine, team_line: _MutableTeamLine) -> None:
    """Credits a personal foul to `line` (and, symmetrically, to
    `team_line`) only if `line` hasn't already reached
    `_MAX_PERSONAL_FOULS` -- capping *both* sides of the same increment
    together, rather than clamping the player total after the fact, is
    what keeps "team totals = sum of player lines" (§9's own Tier-2
    identity) true for `pf` specifically once this cap is in play."""
    if line.pf < _MAX_PERSONAL_FOULS:
        line.pf += 1
        team_line.pf += 1


def _points_for(outcome: TerminalOutcome) -> int:
    points = 0
    if outcome.kind is TerminalKind.MADE_FG:
        points += 3 if outcome.zone is Zone.THREE else 2
    points += outcome.ftm
    return points


def _zone_fields(zone: Zone | None) -> tuple[str, str] | None:
    if zone is Zone.RIM:
        return "fga_rim", "fgm_rim"
    if zone is Zone.MID:
        return "fga_mid", "fgm_mid"
    if zone is Zone.THREE:
        return "fga_three", "fgm_three"
    return None  # pragma: no cover -- every FG/MISS carries a zone


@dataclass
class _Accumulator:
    player_lines: dict[int, _MutablePlayerLine] = field(default_factory=dict)
    team_lines: dict[int, _MutableTeamLine] = field(default_factory=dict)

    def player(self, player_id: int, team_id: int) -> _MutablePlayerLine:
        line = self.player_lines.get(player_id)
        if line is None:
            line = _MutablePlayerLine(player_id=player_id, team_id=team_id)
            self.player_lines[player_id] = line
        return line

    def team(self, team_id: int) -> _MutableTeamLine:
        line = self.team_lines.get(team_id)
        if line is None:
            line = _MutableTeamLine(team_id=team_id)
            self.team_lines[team_id] = line
        return line


def _apply_outcome(
    acc: _Accumulator,
    outcome: TerminalOutcome,
    *,
    offense_team_id: int,
    defense_team_id: int,
    player_team: Mapping[int, int],
) -> None:
    """One terminal/credit event's stat contribution -- the same shape
    regardless of which flavor it is (§4.5: "the *only* thing that
    distinguishes 'terminal' from 'continuation' is which one is allowed
    to end the possession," never which stats it produces)."""
    team_off = acc.team(offense_team_id)
    team_def = acc.team(defense_team_id)

    if outcome.kind is TerminalKind.MADE_FG:
        zone_fields = _zone_fields(outcome.zone)
        fga_field, fgm_field = zone_fields
        scorer = acc.player(outcome.scorer_id, offense_team_id)
        pts = 3 if outcome.zone is Zone.THREE else 2
        scorer.points += pts
        setattr(scorer, fga_field, getattr(scorer, fga_field) + 1)
        setattr(scorer, fgm_field, getattr(scorer, fgm_field) + 1)
        team_off.points += pts
        setattr(team_off, fga_field, getattr(team_off, fga_field) + 1)
        setattr(team_off, fgm_field, getattr(team_off, fgm_field) + 1)
        if outcome.assisted and outcome.passer_id is not None:
            passer = acc.player(outcome.passer_id, offense_team_id)
            passer.ast += 1
            team_off.ast += 1

    elif outcome.kind is TerminalKind.MISSED_FG:
        zone_fields = _zone_fields(outcome.zone)
        fga_field, _ = zone_fields
        setattr(team_off, fga_field, getattr(team_off, fga_field) + 1)
        if outcome.scorer_id is not None:
            shooter = acc.player(outcome.scorer_id, offense_team_id)
            setattr(shooter, fga_field, getattr(shooter, fga_field) + 1)
        if outcome.blocked and outcome.blocking_defender_id is not None:
            blocker = acc.player(outcome.blocking_defender_id, defense_team_id)
            blocker.blk += 1
            team_def.blk += 1

    if outcome.rebound_is_offensive is not None and outcome.rebounder_id is not None:
        rebounder_team = player_team.get(outcome.rebounder_id)
        rebounder = acc.player(outcome.rebounder_id, rebounder_team or offense_team_id)
        if outcome.rebound_is_offensive:
            rebounder.oreb += 1
            team_off.oreb += 1
        else:
            rebounder.dreb += 1
            team_def.dreb += 1

    if outcome.kind is TerminalKind.TURNOVER_LIVE:
        turner = acc.player(outcome.turnover_player_id, offense_team_id)
        turner.tov += 1
        team_off.tov += 1
        if outcome.steal and outcome.stealing_defender_id is not None:
            stealer = acc.player(outcome.stealing_defender_id, defense_team_id)
            stealer.stl += 1
            team_def.stl += 1

    elif outcome.kind is TerminalKind.TURNOVER_DEAD:
        if outcome.turnover_player_id is not None:
            turner = acc.player(outcome.turnover_player_id, offense_team_id)
            turner.tov += 1
            team_off.tov += 1

    elif outcome.kind is TerminalKind.OFFENSIVE_FOUL:
        fouler = acc.player(outcome.offensive_fouler_id, offense_team_id)
        fouler.tov += 1
        team_off.tov += 1
        _credit_personal_foul(fouler, team_off)

    if outcome.kind in (TerminalKind.SHOOTING_FOUL, TerminalKind.NON_SHOOTING_FOUL) or (
        outcome.kind is TerminalKind.MADE_FG and outcome.and_one
    ):
        if outcome.fouler_id is not None:
            fouler = acc.player(outcome.fouler_id, defense_team_id)
            _credit_personal_foul(fouler, team_def)

    if outcome.ft_count > 0 and outcome.fouled_player_id is not None:
        fouled = acc.player(outcome.fouled_player_id, offense_team_id)
        fouled.fta += outcome.ft_count
        fouled.ftm += outcome.ftm
        fouled.points += outcome.ftm
        team_off.fta += outcome.ft_count
        team_off.ftm += outcome.ftm
        team_off.points += outcome.ftm


def accumulate_game(
    possessions: Sequence[PossessionRecord],
    *,
    player_team: Mapping[int, int],
    home_team_id: int,
    away_team_id: int,
) -> tuple[tuple[PlayerBoxLine, ...], tuple[TeamBoxLine, ...]]:
    """§12 step 5's Tier-2 identities all reduce to this function being
    exhaustive over `terminal` + every `credit_events` entry, per
    possession, with `possessions`/minutes/plus-minus credited exactly
    once per possession record."""
    acc = _Accumulator()
    acc.team(home_team_id)
    acc.team(away_team_id)

    for record in possessions:
        acc.team(record.offense_team_id).possessions += 1

        for player_id in (*record.offense_lineup, *record.defense_lineup):
            acc.player(player_id, player_team[player_id]).seconds_played += (
                record.elapsed_seconds
            )

        events: list[TerminalOutcome] = [record.result.terminal, *record.result.credit_events]
        possession_points = 0
        for outcome in events:
            _apply_outcome(
                acc,
                outcome,
                offense_team_id=record.offense_team_id,
                defense_team_id=record.defense_team_id,
                player_team=player_team,
            )
            possession_points += _points_for(outcome)

        if possession_points != 0:
            for player_id in record.offense_lineup:
                acc.player(player_id, player_team[player_id]).plus_minus += possession_points
            for player_id in record.defense_lineup:
                acc.player(player_id, player_team[player_id]).plus_minus -= possession_points

    player_lines = tuple(
        PlayerBoxLine(
            player_id=line.player_id,
            team_id=line.team_id,
            minutes_played=line.seconds_played / 60.0,
            points=line.points,
            fga_rim=line.fga_rim,
            fgm_rim=line.fgm_rim,
            fga_mid=line.fga_mid,
            fgm_mid=line.fgm_mid,
            fga_three=line.fga_three,
            fgm_three=line.fgm_three,
            fta=line.fta,
            ftm=line.ftm,
            oreb=line.oreb,
            dreb=line.dreb,
            ast=line.ast,
            stl=line.stl,
            blk=line.blk,
            tov=line.tov,
            pf=min(line.pf, _MAX_PERSONAL_FOULS),
            plus_minus=line.plus_minus,
        )
        for line in sorted(acc.player_lines.values(), key=lambda x: x.player_id)
    )
    team_lines = tuple(
        TeamBoxLine(
            team_id=line.team_id,
            possessions=line.possessions,
            points=line.points,
            fga_rim=line.fga_rim,
            fgm_rim=line.fgm_rim,
            fga_mid=line.fga_mid,
            fgm_mid=line.fgm_mid,
            fga_three=line.fga_three,
            fgm_three=line.fgm_three,
            fta=line.fta,
            ftm=line.ftm,
            oreb=line.oreb,
            dreb=line.dreb,
            ast=line.ast,
            stl=line.stl,
            blk=line.blk,
            tov=line.tov,
            pf=line.pf,
        )
        for line in sorted(acc.team_lines.values(), key=lambda x: x.team_id)
    )
    return player_lines, team_lines
