"""Turnover-recovery Commit 1 measurement, re-runnable
(docs/design/turnover-recovery.md TR-6 Commit 1).

**What this measures, and why it is measurement only.** TR-6 Commit 1 asks
for five numbers *before* any loose-ball constant ships, so the build report
can say what the feature will do instead of discovering it:

  1. the live share of charged turnovers (`turnover_live` draws / all `tov`);
  2. `turnover_live` draws per game;
  3. the share of `turnover_live` draws made with the continuation budget
     already spent (TR-4.9's dead zone -- the recovery leaf is not offered);
  4. the realized spread of `recovery_probability` across the 30 teams'
     most-used lineups, at TR-4.4's proposed vector (TR-8 R4's honesty check);
  5. the predicted scramble-violation close rate (TR-4.14).

Per TR-10.3 nothing here is an objective: no fit, no sweep over `base_rate`,
and no distance-to-13.6 figure. It reports and stops.

**How it instruments without editing the engine.** It runs on whatever engine
is checked out, wrapping three names in `dba2.sim.possession`'s namespace
(looked up at call time, so the wrap is seen by every call site):
`new_possession_state` (to read the live budget and shot clock at each
resolution beat), `assign_matchups` (called once per possession with both
lineups) and `resolve_resolution_beat` (to see each drawn leaf). No RNG is
consumed by the wrappers, so the simulated games are the engine's own.

The loose-ball scalar is computed here from TR-4.4's literal vector
(`motor .40 / hands .35 / basketball_iq .25`, base 0.28, scale 70, band
[0.10, 0.50]) rather than imported, because on the pre-change engine the
tuning block does not exist yet.

Usage (from the repo root):
    python tools/spikes/tr_loose_ball_measure.py --rng-seed 101 --games 1230
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dba2.core.rng import rng_for  # noqa: E402
from dba2.sim import possession as possession_module  # noqa: E402
from dba2.sim.game import simulate_game  # noqa: E402
from dba2.sim.probability import contest_probability  # noqa: E402
from dba2.sim.tuning import TuningProfile  # noqa: E402
from tools import validate_sim  # noqa: E402

_WEIGHTS = {"motor": 0.40, "hands": 0.35, "basketball_iq": 0.25}
_BASE_RATE = 0.28
_SCALE = 70.0
_BAND = (0.10, 0.50)
_SCRAMBLE_SECONDS = 2.0


def _loose_ball_score(player) -> float:
    total = sum(_WEIGHTS.values())
    return sum(getattr(player.ratings, f) * w for f, w in _WEIGHTS.items()) / total


def _recovery_probability(offense, defense) -> float:
    off = statistics.mean(_loose_ball_score(p) for p in offense)
    dfn = statistics.mean(_loose_ball_score(p) for p in defense)
    return contest_probability(off, dfn, base_rate=_BASE_RATE, scale=_SCALE, band=_BAND)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rng-seed", type=int, default=101)
    parser.add_argument("--games", type=int, default=1230)
    parser.add_argument("--players-per-team", type=int, default=14)
    args = parser.parse_args()

    tuning = TuningProfile()
    lo, hi = tuning.possession.resolution_seconds_range
    budget_max = tuning.possession.continuation_budget_max

    live: dict[str, object] = {}
    lineup_use: dict[int, Counter] = defaultdict(Counter)
    lineup_players: dict[frozenset, tuple] = {}
    tl_draws = 0
    tl_exhausted = 0
    predicted_recoveries = 0.0
    predicted_scramble = 0.0

    real_new_state = possession_module.new_possession_state
    real_assign = possession_module.assign_matchups
    real_resolve = possession_module.resolve_resolution_beat
    real_pace = possession_module._pace_seconds_scale

    def new_state(t):
        state = real_new_state(t)
        live["state"] = state
        return state

    def assign(offense, defense):
        live["offense"], live["defense"] = offense, defense
        key = frozenset(p.id for p in offense)
        lineup_use[offense[0].player.team_id][key] += 1
        lineup_players[key] = offense
        return real_assign(offense, defense)

    def pace(target, t):
        scale = real_pace(target, t)
        live["pace"] = scale
        return scale

    def resolve(*a, **kw):
        nonlocal tl_draws, tl_exhausted, predicted_recoveries, predicted_scramble
        result = real_resolve(*a, **kw)
        if result.record.outcome == "turnover_live":
            tl_draws += 1
            state = live["state"]
            if state.continuation_uses >= budget_max:
                tl_exhausted += 1
            else:
                r = _recovery_probability(live["offense"], live["defense"])
                predicted_recoveries += r
                # Shot clock after the resolution tick U(lo, hi)*pace and the
                # scramble tick 2.0*pace: P(clock - (u + 2) * pace <= 0).
                p_scale = live["pace"]
                need = state.shot_clock_seconds / p_scale - _SCRAMBLE_SECONDS
                p_violate = min(1.0, max(0.0, (hi - need) / (hi - lo))) if need > lo else 1.0
                predicted_scramble += r * p_violate
        return result

    possession_module.new_possession_state = new_state
    possession_module.assign_matchups = assign
    possession_module.resolve_resolution_beat = resolve
    possession_module._pace_seconds_scale = pace

    season_seed = rng_for(args.rng_seed, "validate_sim", "season", 0).getrandbits(63)
    league = validate_sim.generate_league(
        league_id=1,
        season=2026,
        rng_seed=season_seed,
        players_per_team=args.players_per_team,
    )
    schedule = validate_sim.build_schedule(league.teams, league_seed=season_seed, season=2026)
    schedule = schedule[: args.games]

    tov = stl = possessions = 0
    for game in schedule:
        context = validate_sim.build_context(league, game, tuning)
        result = simulate_game(context, rng_seed=game.rng_seed)
        for line in result.team_box_lines:
            tov += line.tov
            stl += line.stl
            possessions += line.possessions

    games = len(schedule)
    print(f"games simmed: {games}  (rng_seed={args.rng_seed}, season index 0)")
    print(f"team possessions: {possessions}  tov: {tov}  stl: {stl}")
    print(f"tov_per_100_poss (this sample): {100.0 * tov / possessions:.3f}")
    print(f"stl_per_game (team): {stl / (2 * games):.3f}  "
          f"tov_per_game (team): {tov / (2 * games):.3f}")
    print()
    print(f"1. live share of charged tov: {tl_draws}/{tov} = {tl_draws / tov:.4f}")
    print(f"2. turnover_live draws per game (both teams): {tl_draws / games:.3f}")
    print(f"3. turnover_live draws with budget exhausted: {tl_exhausted}/{tl_draws} = "
          f"{tl_exhausted / tl_draws:.4f}")

    league_mean_lb = statistics.mean(
        _loose_ball_score(p) for players in lineup_players.values() for p in players
    )
    team_ps = []
    for team_id, counts in sorted(lineup_use.items()):
        key, _ = counts.most_common(1)[0]
        offense = lineup_players[key]
        off = statistics.mean(_loose_ball_score(p) for p in offense)
        team_ps.append(
            (team_id, off, contest_probability(off, league_mean_lb, base_rate=_BASE_RATE,
                                               scale=_SCALE, band=_BAND))
        )
    ps = [p for _, _, p in team_ps]
    offs = [o for _, o, _ in team_ps]
    best = max(offs)
    worst = min(offs)
    print(f"4. recovery_probability, 30 most-used lineups vs a league-mean defense: "
          f"min {min(ps):.4f}  max {max(ps):.4f}  spread {max(ps) - min(ps):.4f}  "
          f"sd {statistics.pstdev(ps):.4f}")
    print(f"   lineup loose-ball means: min {worst:.2f}  max {best:.2f}  "
          f"(mean over observed lineups' players {league_mean_lb:.2f})")
    extreme = contest_probability(best, worst, base_rate=_BASE_RATE, scale=_SCALE, band=_BAND)
    extreme_rev = contest_probability(worst, best, base_rate=_BASE_RATE, scale=_SCALE, band=_BAND)
    print(f"   extreme pairing: best-vs-worst {extreme:.4f}, worst-vs-best {extreme_rev:.4f}")
    print(f"   predicted recoveries per game: {predicted_recoveries / games:.3f}")
    print(f"5. predicted scramble-violation closes: {predicted_scramble:.2f} total, "
          f"{predicted_scramble / games:.4f} per game")


if __name__ == "__main__":
    main()
