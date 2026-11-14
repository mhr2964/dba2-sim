"""In-memory 30x14 league generator + statistics helpers for
`test_derivation_league_distribution.py` (§12 step 4's league-level
distribution test). Not a test module itself (no `test_*` functions,
so pytest's default collection glob never picks this file up) --
shared plumbing both that test file and the calibration-target report
script (`docs/ledger/reports/phase-1-backend-dev-round-9.md`'s 20-seed
run) go through, so the two never compute a statistic two different
ways.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from random import Random
from types import SimpleNamespace

from dba2.domain.player import Position
from dba2.sim import derivation as d


@dataclass(frozen=True)
class GeneratedPlayer:
    team_id: int
    position: Position
    ratings: dict[str, int]
    tendencies: dict[str, int]
    mean_of_55: float


def generate_league(
    seed: int, *, teams: int = 30, players_per_team: int = 14
) -> list[GeneratedPlayer]:
    """One synthetic league, per §5-§8: a team talent offset drawn once
    per team, positions per §5.2, then per slot a tier talent draw and
    the 55 ratings, and finally `derive_tendencies` with the same league
    RNG stream supplying Decision P1-19 item 2's seeded noise (the SD
    bands this harness checks are sized including that noise term --
    §8.4's `TENDENCY_TARGET_SD` bakes in the `Normal(0, 4.0)` variance,
    so calling with `rng=None` here would understate every field's
    realized SD)."""
    rng = Random(seed)
    players: list[GeneratedPlayer] = []
    for team_id in range(teams):
        team_offset = rng.gauss(0.0, 2.5)
        positions = d.synthetic_positions(rng)
        for slot in range(players_per_team):
            tier = d.TIER_BY_SLOT[slot]
            position = positions[slot]
            talent = d.tier_talent(tier, team_offset, rng)
            ratings_dict = d.synthetic_ratings(talent, position, rng)
            mean_of_55 = sum(ratings_dict.values()) / 55
            ratings_ns = SimpleNamespace(**ratings_dict)
            tendencies = d.derive_tendencies(ratings_ns, position, rng=rng)
            players.append(
                GeneratedPlayer(
                    team_id=team_id,
                    position=position,
                    ratings=ratings_dict,
                    tendencies=tendencies,
                    mean_of_55=mean_of_55,
                )
            )
    return players


def group_mean(player: GeneratedPlayer, group: str) -> float:
    fields = d.RATING_GROUPS[group]
    return sum(player.ratings[f] for f in fields) / len(fields)


def league_stats(players: list[GeneratedPlayer]) -> dict[str, float]:
    """§6.6's `overall_cached` league distribution -- `round()`-ed per
    player first, matching how the repair pass pins each player's
    mean-of-55 to `round(target)` (§6.5), which is what `overall_cached`
    itself stores."""
    overalls = sorted(round(p.mean_of_55) for p in players)
    n = len(overalls)
    return {
        "mean": statistics.mean(overalls),
        "sd": statistics.pstdev(overalls),
        "min": overalls[0],
        "max": overalls[-1],
        "p90": overalls[int(round(0.90 * (n - 1)))],
        "count_ge_80": sum(1 for v in overalls if v >= 80),
    }


def per_group_stats(players: list[GeneratedPlayer], group: str) -> tuple[float, float]:
    values = [group_mean(p, group) for p in players]
    return statistics.mean(values), statistics.pstdev(values)


def per_position_mean_overall(players: list[GeneratedPlayer], position: Position) -> float:
    values = [round(p.mean_of_55) for p in players if p.position is position]
    return statistics.mean(values)


def position_conditional_group_mean(
    players: list[GeneratedPlayer], group: str, position: Position
) -> float:
    values = [group_mean(p, group) for p in players if p.position is position]
    return statistics.mean(values)


def tendency_field_stats(players: list[GeneratedPlayer], field_name: str) -> tuple[float, float]:
    values = [p.tendencies[field_name] for p in players]
    return statistics.mean(values), statistics.pstdev(values)


def tendency_position_deviation(
    players: list[GeneratedPlayer], field_name: str, position: Position
) -> float:
    """§8.4's realized positional profile: the field's mean value among
    players at `position`, relative to the field's declared `base` --
    what `DESIRED[position]` is asserted against."""
    base = d._TENDENCY_FORMULAS[field_name].base
    values = [p.tendencies[field_name] for p in players if p.position is position]
    return statistics.mean(values) - base
