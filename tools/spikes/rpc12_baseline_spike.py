"""RPC-12 baseline regression spike (docs/design/real-player-rating-calibration.md).

**What this measures.** The design's revision 3 carries five `[SPIKE-MEASURED]` figures
and two `[CRITIC-MEASURED]` ones that RPC-12's baseline step must re-measure before the
Stage 1 swap is trusted, because each one either carries an argument or is quoted as a
before/after anchor:

1. global max |u| over every (row, real field) the builder actually computes a residual
   for, and how many exceed the metric-z clamp 3.0902 (design: 5.019, 688 of 257,386);
2. the `rim_protection` centre-pool max |u| and the pinning threshold it implies,
   `99 - offset_C + u_spread * max|u|` (design: ~3.246 -> 92.318; Jokic 2025 T 92.512);
3. Jokic 2025's rank among the qualified centres on RPC-4-3's ranking key (the blended
   evidence z before `residual_u`) and the value of the centre multiset at that rank,
   with the whole ranked multiset printed (design: rank 42 of 57 -> 87, a plateau at 87);
4. how many T values fail `mu + 1.0 * (T - mu) == T` in IEEE arithmetic (design nit 2,
   `[CRITIC-MEASURED]` ~2.1% of draws; Stage 2's inert-refactor test depends on it).

The drag p95s, the T' excursion count and the T1/T5 anchors are printed by
`python -m tools._gm_real_player_report seeds/real_players --calibration --validation`.

**How.** It builds every raw season in memory with the builder's own code, capturing
`residual_u`'s outputs and the swap's inputs by wrapping those two module functions for
the duration of the build -- no builder code is copied, so it cannot drift from what
ships. `--swap-weight 0` reproduces the pre-swap (level-formula) pipeline exactly, which
is what the baseline figures are measured on. Read-only: writes nothing.

Usage:
    python -m tools.spikes.rpc12_baseline_spike [--swap-weight 0]
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from dba2.domain.player import Position
from dba2.sim.derivation import POSITION_RATING_OFFSETS
from tools import _gm_real_player_build as build
from tools._gm_real_player_report import find_player

_METRIC_Z_CLAMP = build.RECOVERED_Z_LIMIT  # 3.0902, what `_percentile_z` can emit
_TALENT_MEAN = 976 / 14  # RPC-1.3: the tier ladder's weighted mean talent, 69.714


def _capture(seasons: list[int], weight: float) -> tuple[dict, dict]:
    """Every season built through the real builder, with `residual_u`'s return values
    and `swap_cells`' arguments recorded per season."""
    residuals: dict[int, list[dict[str, float]]] = defaultdict(list)
    swaps: dict[int, dict] = {}
    current: list[int] = []
    real_residual_u, real_swap_cells = build.residual_u, build.swap_cells
    real_build_season = build.build_season

    def residual_u(values, impact, position, fit_ids):
        out = real_residual_u(values, impact, position, fit_ids)
        residuals[current[-1]].append(out)
        return out

    def swap_cells(level, evidence, position, qualified, **kwargs):
        swaps[current[-1]] = {
            "level": level,
            "evidence": evidence,
            "position": position,
            "qualified": list(qualified),
        }
        return real_swap_cells(level, evidence, position, qualified, **kwargs)

    def build_season(season, *args, **kwargs):
        current.append(season)
        return real_build_season(season, *args, **kwargs)

    build.residual_u, build.swap_cells, build.build_season = residual_u, swap_cells, build_season
    try:
        seeds, _ = build.build_seasons(seasons, tuning=build.Tuning(swap_evidence_weight=weight))
    finally:
        build.residual_u, build.swap_cells = real_residual_u, real_swap_cells
        build.build_season = real_build_season
    return seeds, {"residuals": residuals, "swaps": swaps}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--swap-weight", type=float, default=0.0)
    args = parser.parse_args()
    seasons = build.available_raw_seasons(build.RAW_DIR)
    seeds, captured = _capture(seasons, args.swap_weight)
    field_order = list(build.FIELD_METRIC_WEIGHTS)

    # 1. global |u|
    total = over = 0
    worst = 0.0
    for per_field in captured["residuals"].values():
        for name, u in zip(field_order, per_field, strict=True):
            if name not in build.REAL_FIELDS:
                continue
            for value in u.values():
                total += 1
                over += abs(value) > _METRIC_Z_CLAMP
                worst = max(worst, abs(value))
    print(
        f"1. global max |u| = {worst:.3f}; {over} of {total} (row, real field) entries "
        f"({over / total:.2%}) past {_METRIC_Z_CLAMP:.4f}"
    )

    # 2. rim_protection centre pool
    rim_index = field_order.index("rim_protection")
    offset_c = POSITION_RATING_OFFSETS[Position.C]["rim_protection"]
    centre_u = []
    for season, per_field in captured["residuals"].items():
        swap = captured["swaps"][season]
        u = per_field[rim_index]
        centre_u += [u[ref] for ref in swap["qualified"] if swap["position"][ref] is Position.C]
    max_u = max(abs(v) for v in centre_u)
    threshold = 99 - offset_c + build.DEFAULT_TUNING.u_spread * max_u
    jokic = find_player(seeds[2025], "Nikola Jokic")
    print(
        f"2. rim_protection qualified-centre max |u| = {max_u:.3f} over {len(centre_u)} "
        f"rows; C offset {offset_c:.3f}; pinning threshold T >= {threshold:.3f}; "
        f"Jokic 2025 T = {jokic['meta']['talent']:.3f} "
        f"(margin {jokic['meta']['talent'] - threshold:+.3f})"
    )

    # 3. Jokic's centre rank on RPC-4-3's key
    swap = captured["swaps"][2025]
    centres = [ref for ref in swap["qualified"] if swap["position"][ref] is Position.C]
    evidence = swap["evidence"]["rim_protection"]
    by_key = sorted(centres, key=lambda ref: (-evidence[ref], ref))
    multiset = sorted((swap["level"][ref]["rim_protection"] for ref in centres), reverse=True)
    rank = by_key.index(jokic["external_ref"]) + 1
    blk36 = _blk36_2025()
    raw_rank = sorted(centres, key=lambda ref: (-blk36[ref], ref)).index(jokic["external_ref"]) + 1
    print(
        f"3. Jokic 2025: rank {rank} of {len(centres)} qualified C on the blended evidence "
        f"key (rank {raw_rank} by raw blk/36) -> multiset value {multiset[rank - 1]}"
    )
    print(
        "   ranked C multiset (rank:value): "
        + ", ".join(f"{i}:{v}" for i, v in enumerate(multiset, start=1))
    )

    # 4. last-ULP share
    talents = [r["meta"]["talent"] for s in seeds.values() for r in s["players"]]
    flips = sum(1 for t in talents if _TALENT_MEAN + 1.0 * (t - _TALENT_MEAN) != t)
    print(
        f"4. mu + 1.0*(T - mu) != T on {flips} of {len(talents)} rows "
        f"({flips / len(talents):.2%}); integer flips after round(): "
        f"{sum(1 for t in talents if round(_TALENT_MEAN + (t - _TALENT_MEAN)) != round(t))}"
    )


def _blk36_2025() -> dict[str, float]:
    import json

    raw = json.loads((build.RAW_DIR / "2025.json").read_text(encoding="utf-8"))
    return {
        str(e["external_ref"]): e["totals"]["blk"] * 36.0 / float(e["minutes_total"])
        if float(e["minutes_total"])
        else 0.0
        for e in raw["players"]
    }


if __name__ == "__main__":
    main()
