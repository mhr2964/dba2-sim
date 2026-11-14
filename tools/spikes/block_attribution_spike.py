"""Block-attribution decomposition, re-runnable
(docs/design/block-attribution-v2.md BA-0, BA-4.3, BA-6 Commit 0).

**What this measures.** One full season through `tools.validate_sim`'s own
`simulate_season_detailed` path (so every league-level number is the
harness's own), with each player's box-score blocks split into **primary**
(`blocked_miss`, credited to the contesting `defender`) and **help**
(`blocked_miss_help`, credited to `blocking_defender_id`), plus:

  * each player's help-eligible rim-shot count -- the rim shots on which he
    could have been the weak-side blocker (pre-v2 engine: he was the single
    `rim_help_defender` candidate; v2 engine: he was in `rim_help_pool` and
    was not the shot's primary defender) -- and his help conversion over it;
  * `rim_protection`, the cached `rim_protection_contribution` scalar (`rpc`,
    the candidate/share scalar) and the rim-help contest scalar (`rhc`,
    `ability_score` over `resolution_rim_help.defense_weights`; equal to `rpc`
    once the vectors are unified, BA-2.4);
  * the rotation top-10 blk/36 table and, with `--real`, the real 2025 top-10
    beside it (rotation pool = gp >= 20 and mpg >= 15, BA-0.1), and each named
    anchor's bpg rank.

**How it instruments without editing the engine.** It wraps the module-level
`resolve_resolution_beat` name `dba2.sim.possession` imports (looked up at
call time, so every call site sees the wrap). The wrapper consumes no RNG, so
the simulated games are the engine's own. It reads whichever of the two
contracts the checked-out engine passes (`rim_help_defender` before v2,
`rim_help_pool` after), so the same script measures both sides of the change.

**Pin `PYTHONPATH` to the worktree you mean to measure** (HANDOFF.md TR-12):
unpinned, `dba2` resolves to the venv's editable install, i.e. the shared
tree. The script asserts the pin when `PYTHONPATH` is set.

`--seed` is the `tools.validate_sim --rng-seed` value: the season simmed is
that run's season 1 (the same `rng_for(seed, "validate_sim", "season", 0)`
derivation), so the spike's league numbers equal the harness's first
replicate.

Usage (from the repo root):
    PYTHONPATH=<wt> python -m tools.spikes.block_attribution_spike --seed 1 [--real] [--out f.json]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import dba2
from dba2.core.rng import rng_for
from dba2.sim import possession as possession_module
from dba2.sim.context import build_sim_player
from dba2.sim.probability import ability_score
from dba2.sim.tuning import TuningProfile
from tools import validate_sim
from tools._gm_real_player_report import RAW_DIR as _REAL_RAW_DIR

_NAMED = (
    "Victor Wembanyama",
    "Nikola Jokic",
    "Anthony Davis",
    "Rudy Gobert",
    "Mitchell Robinson",
    "Chet Holmgren",
    "Jonas Valanciunas",
    "Luke Kornet",
    "Walker Kessler",
    "Jay Huff",
)
_REAL_MIN_GAMES = 20
_TOP = 10


class _ShotLog:
    """Per-shot facts captured from each resolution beat, no RNG consumed."""

    def __init__(self) -> None:
        self.primary_blocks: dict[int, int] = defaultdict(int)
        self.help_blocks: dict[int, int] = defaultdict(int)
        self.rim_as_primary: dict[int, int] = defaultdict(int)
        self.rim_help_eligible: dict[int, int] = defaultdict(int)
        self.rim_shots = 0

    def record(self, defender_id: int, kwargs: dict, result) -> None:
        leaf = result.record.outcome
        zone = getattr(result.outcome, "zone", None)
        if zone is not None and zone.value == "rim":
            self.rim_shots += 1
            self.rim_as_primary[defender_id] += 1
            for pid in _help_eligible_ids(defender_id, kwargs):
                self.rim_help_eligible[pid] += 1
        if leaf == "blocked_miss":
            self.primary_blocks[defender_id] += 1
        elif leaf == "blocked_miss_help":
            self.help_blocks[result.outcome.blocking_defender_id] += 1


def _help_eligible_ids(defender_id: int, kwargs: dict) -> list[int]:
    if "rim_help_pool" in kwargs:
        return [p.id for p in kwargs["rim_help_pool"] if p.id != defender_id]
    candidate = kwargs.get("rim_help_defender")
    if candidate is not None and candidate.id != defender_id:
        return [candidate.id]
    return []


def _install(log: _ShotLog) -> None:
    original = possession_module.resolve_resolution_beat

    def wrapped(shooter, defender, **kwargs):
        result = original(shooter, defender, **kwargs)
        log.record(defender.id, kwargs, result)
        return result

    possession_module.resolve_resolution_beat = wrapped


def _real_top(season: int) -> tuple[list[dict], dict[str, dict]]:
    payload = json.loads((_REAL_RAW_DIR / f"{season}.json").read_text(encoding="utf-8"))
    rows: dict[str, dict] = {}
    for entry in payload["players"]:
        gp, minutes = entry["games_played"], float(entry["minutes_total"])
        if gp <= 0 or minutes <= 0:
            continue
        blk = entry["totals"]["blk"]
        rows[str(entry["external_ref"])] = {
            "name": f"{entry['first_name']} {entry['last_name']}",
            "gp": gp,
            "mpg": minutes / gp,
            "bpg": blk / gp,
            "b36": blk * 36.0 / minutes,
        }
    rotation = [
        r
        for r in rows.values()
        if r["gp"] >= _REAL_MIN_GAMES and r["mpg"] >= validate_sim._ROTATION_MPG_THRESHOLD
    ]
    return sorted(rotation, key=lambda r: -r["b36"])[:_TOP], rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--season-year", type=int, default=2026)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    pinned = os.environ.get("PYTHONPATH")
    if pinned:
        root = Path(pinned.split(os.pathsep)[0]).resolve()
        here = Path(dba2.__file__).resolve()
        if root not in here.parents:
            sys.exit(f"dba2 resolved to {here}, not under PYTHONPATH {root}")

    log = _ShotLog()
    _install(log)
    tuning = TuningProfile()
    season_seed = rng_for(args.seed, "validate_sim", "season", 0).getrandbits(63)
    metrics, league, games, acc = validate_sim.simulate_season_detailed(
        league_id=1,
        season=args.season_year,
        rng_seed=season_seed,
        players_per_team=validate_sim._DEFAULT_PLAYERS_PER_TEAM,
        tuning=tuning,
        construct_roles=args.real,
        real_players_season=2025 if args.real else None,
        real_team_rosters=args.real,
    )

    rows: list[dict] = []
    for team in league.teams:
        team_games = acc.games_played.get(team.id, 0)
        for player in league.players_by_team[team.id]:
            minutes = acc.player_minutes.get(player.id, 0.0)
            if team_games == 0 or minutes <= 0:
                continue
            ratings = league.ratings_by_player[player.id]
            sim_player = build_sim_player(
                player, ratings, league.tendencies_by_player[player.id], tuning
            )
            blocks = acc.player_blocks.get(player.id, 0)
            eligible = log.rim_help_eligible[player.id]
            help_blocks = log.help_blocks[player.id]
            rows.append(
                {
                    "pid": player.id,
                    "ref": player.external_ref,
                    "name": f"{player.first_name} {player.last_name}",
                    "pos": player.primary_position.value,
                    "rp": ratings.rim_protection,
                    "rpc": round(sim_player.rim_protection_contribution, 2),
                    "rhc": round(
                        ability_score(sim_player, tuning.resolution_rim_help.defense_weights), 2
                    ),
                    "mpg": round(minutes / team_games, 1),
                    "bpg": round(blocks / team_games, 2),
                    "b36": round(blocks * 36.0 / minutes, 2),
                    "primary": log.primary_blocks[player.id],
                    "rim_as_primary": log.rim_as_primary[player.id],
                    "help": help_blocks,
                    "help_eligible": eligible,
                    "help_conv": round(help_blocks / eligible, 4) if eligible else None,
                }
            )
    rows.sort(key=lambda r: (-r["bpg"], r["pid"]))
    for rank, row in enumerate(rows, start=1):
        row["rank_bpg"] = rank
    rotation = sorted(
        (r for r in rows if r["mpg"] >= validate_sim._ROTATION_MPG_THRESHOLD),
        key=lambda r: (-r["b36"], r["pid"]),
    )
    for rank, row in enumerate(rotation, start=1):
        row["rank_b36"] = rank
    by_pos: dict[str, list[float]] = defaultdict(list)
    for row in rotation:
        by_pos[row["pos"]].append(row["b36"])

    league_keys = (
        "blk_per_game",
        "blk_per36_max",
        "blk_per36_sd_rotation",
        "blk_per36_top10_ratio",
        "blk_per36_p95_rotation",
        "top_shot_blocker_bpg",
        "players_over_2_bpg",
        "guard_blk_per36",
        "fg_pct_rim",
        "share_fga_rim",
        "pf_per_game",
        "team_win_total_sd",
    )
    out: dict = {
        "seed": args.seed,
        "real": args.real,
        "games": games,
        "league": {k: round(metrics[k], 4) for k in league_keys if k in metrics},
        "blocks": {
            "primary": sum(log.primary_blocks.values()),
            "help": sum(log.help_blocks.values()),
            "rim_shots": log.rim_shots,
        },
        "b36_by_pos_rotation": {k: round(statistics.mean(v), 2) for k, v in sorted(by_pos.items())},
        "top10_b36_rotation": rotation[:_TOP],
        "top20_bpg": rows[:20],
    }
    if args.real:
        real_top, real_rows = _real_top(2025)
        out["real_top10_b36"] = [
            {**r, "b36": round(r["b36"], 2), "bpg": round(r["bpg"], 2), "mpg": round(r["mpg"], 1)}
            for r in real_top
        ]
        out["named"] = {
            r["name"]: {**r, "real_b36": round(real_rows[r["ref"]]["b36"], 2)}
            for r in rows
            if r["name"] in _NAMED and r["ref"] in real_rows
        }

    print(f"seed {args.seed} {'real-2025' if args.real else 'synthetic'}: {games} games")
    for k, v in out["league"].items():
        print(f"  {k}: {v}")
    print(f"  blocks primary/help: {out['blocks']['primary']}/{out['blocks']['help']}")
    print(f"  rotation blk/36 by position: {out['b36_by_pos_rotation']}")
    print("  rotation top-10 blk/36 (sim):")
    for i, r in enumerate(rotation[:_TOP], start=1):
        real = out.get("real_top10_b36")
        real_col = f"   | real #{i}: {real[i - 1]['name']} {real[i - 1]['b36']:.2f}" if real else ""
        print(
            f"   #{i} {r['name']} ({r['pos']}) {r['b36']:.2f}/36 {r['bpg']:.2f} bpg "
            f"{r['mpg']} mpg  prim/help {r['primary']}/{r['help']}{real_col}"
        )
    for name, r in out.get("named", {}).items():
        print(
            f"  {name}: {r['bpg']:.2f} bpg (#{r['rank_bpg']}), {r['b36']:.2f}/36 "
            f"(#{r.get('rank_b36', '-')}), real {r['real_b36']:.2f}/36; help {r['help']}/"
            f"{r['help_eligible']} eligible"
        )
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
