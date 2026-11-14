"""Acceptance report for a version-2 real-player build (RPD-8 "Acceptance" and RPD-8-8's
clamp measurement, docs/design/real-player-ratings-v2.md).

Reads the seed files a build wrote to any out-dir (or, for tuning sweeps, an in-memory
`{season: seed}`), and reports:

* **TUNING set** rows (Durant 2014, Kawhi 2017, Curry 2019, Kyrie 2019, Paul George 2019,
  Klay 2021, Lillard 2021): where each lands versus the legacy seed. Weights may be
  adjusted against these. The design gives no numeric bar for them; this report prints
  the numbers and applies one working criterion (inside the league's top 40 and above
  the legacy overall), labelled as a tuning aid, not an acceptance check.
* **VALIDATION set** (Tatum/Brown 2023, Tatum 2025, Jokic/Giannis/SGA/Doncic 2023 and
  2024, Morant 2023, Anthony Davis 2025, Embiid 2024): the checks that can fail. Printed
  only with `--validation`, so tuning runs cannot accidentally read them; never used to
  tune. Ties at a rank boundary are shown both ways (best and worst place among players
  with the same overall) and a check that passes only on the best place is marked
  `PASS (tie-dependent)`.
* **Drag check**: for >= 95% of the qualified pool the mean of the synthetic fields is
  within 8 of the mean of the real fields, and SD(T' - T) <= 4.
* **Solver sanity checks** (true by construction; they only catch a broken build): the
  420-slot overall distribution, per-position mean(overall - T), pegged fields, per-real
  field SD, Spearman(I rank, overall rank).
* **Informational**: how many rows the solver pegs at 25/99 to make the overall fit, and
  the evidence's share of a real field's variance within a talent band.
* **RPD-6 clamp measurement** over the rebuilt seasons: the fraction of per-field,
  per-age deltas the real trajectory would report that exceed the progression stub's
  +/-8 cap, against RPD-8-8's 20% threshold, beside the same measurement on the legacy
  seeds for the same seasons and 15 fields.

* **RPC-12 Tier 1** (`--calibration`, docs/design/real-player-rating-calibration.md):
  instead of the lines above, the seed-level calibration checks T1-T13 against the raw
  season totals in `seeds/real_players_raw` -- the `rim_protection` / real blk/36
  correlation, the control fields, drag p95, T' excursions, the per-position
  distribution, thin samples at the top, pairwise distinctness and the swap's
  permutation summary. `--baseline-dir` names the "before" seeds for the bars that must
  not move; T5 (named players) prints only with `--validation` too.

Usage:
    python -m tools._gm_real_player_report [OUT_DIR] [--legacy-dir DIR] [--validation]
    python -m tools._gm_real_player_report [OUT_DIR] --calibration [--validation]
        [--baseline-dir DIR] [--raw-dir DIR]
"""

from __future__ import annotations

import itertools
import json
import sys
import tempfile
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, pstdev, pvariance
from types import SimpleNamespace

from dba2.domain.player import Position
from dba2.sim.derivation import POSITION_RATING_OFFSETS, RATING_FIELD_ORDER
from dba2.sim.ratings import compute_overall
from tools._real_fields import (
    ADVANCED_REAL_FIELDS,
    BOX_SCORE_REAL_FIELDS,
    REAL_BUILDER_VERSION,
    REAL_FIELDS,
)

LEGACY_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players"
DEFAULT_OUT_DIR = Path(tempfile.gettempdir()) / "dba2_real_players_v2"

# (name, season) -- season is the start year: 2023 is 2023-24.
TUNING_CASES: tuple[tuple[str, int], ...] = (
    ("Kevin Durant", 2014),
    ("Kawhi Leonard", 2017),
    ("Stephen Curry", 2019),
    ("Kyrie Irving", 2019),
    ("Paul George", 2019),
    ("Klay Thompson", 2021),
    ("Damian Lillard", 2021),
)
STARS_TOP15 = ("Nikola Jokic", "Giannis Antetokounmpo", "Shai Gilgeous-Alexander", "Luka Doncic")

LADDER_SLOTS = 420
CLAMP_CAP = 8.0  # tools/_gm_progression_stub.py::_MAX_REAL_FIELD_DELTA
CLAMP_THRESHOLD = 0.20  # RPD-8-8
DRAG_MEAN_LIMIT = 8.0
DRAG_POOL_SHARE = 0.95
DRAG_SD_LIMIT = 4.0
_DRAFT_AGE_ANCHOR = 21
EVIDENCE_BAND = (74.0, 78.0)  # the talent band the evidence-share line reads
EVIDENCE_CHECK_NAME = "evidence share of real-field variance"
EXTREME_PEG_SHARE = 0.5  # a row with this share of synthetic fields at 25/99 is "extreme"


# ------------------------------------------------------------------------------ loading


def load_seasons(directory: Path, only: Sequence[int] | None = None) -> dict[int, dict]:
    return {
        int(path.stem): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("[12][0-9][0-9][0-9].json"))
        if only is None or int(path.stem) in only
    }


def overall_of(row: Mapping) -> int:
    return compute_overall(SimpleNamespace(**row["ratings"]), Position(row["primary_position"]))


def _fold(text: str) -> str:
    """Accent-folded lowercase, so BDL's 'Nikola Jokić' matches 'Nikola Jokic'."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower().strip()


def find_player(seed: Mapping, name: str) -> dict | None:
    """The one row named `name` in a season seed, None if absent; ambiguity is an error
    rather than a silent pick."""
    wanted = _fold(name)
    hits = [r for r in seed["players"] if _fold(f"{r['first_name']} {r['last_name']}") == wanted]
    if len(hits) > 1:
        raise ValueError(f"{name!r} matches {len(hits)} rows in season {seed['season']}")
    return hits[0] if hits else None


# ------------------------------------------------------------------------------ ranking


@dataclass(frozen=True)
class Placement:
    overall: int
    rank_best: int  # 1 + players strictly above
    rank_worst: int  # players at or above (last place among equals)


def placements(overalls: Mapping[str, int]) -> dict[str, Placement]:
    values = sorted(overalls.values(), reverse=True)
    out: dict[str, Placement] = {}
    for ref, value in overalls.items():
        above = sum(1 for v in values if v > value)
        equal = sum(1 for v in values if v == value)
        out[ref] = Placement(value, above + 1, above + equal)
    return out


def status_within(place: Placement, limit: int) -> str:
    if place.rank_worst <= limit:
        return "PASS"
    if place.rank_best <= limit:
        return "PASS (tie-dependent)"
    return "FAIL"


def rank_average_ties(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    rx, ry = rank_average_ties(xs), rank_average_ties(ys)
    mx, my = mean(rx), mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / (vx * vy) ** 0.5 if vx > 0 and vy > 0 else 0.0


# ------------------------------------------------------------------------------ rows


def real_field_names(row: Mapping) -> tuple[str, ...]:
    """The fields that are real for this row: the advanced three only in the advanced
    band."""
    if row["meta"].get("band") == "advanced_tracking":
        return REAL_FIELDS
    return tuple(f for f in REAL_FIELDS if f not in ADVANCED_REAL_FIELDS)


def qualified_rows(seed: Mapping) -> list[dict]:
    return [r for r in seed["players"] if r["meta"].get("qualified")]


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS / FAIL / PASS (tie-dependent) / INFO / SKIPPED
    detail: str

    def line(self) -> str:
        return f"[{self.status}] {self.name}: {self.detail}"


# ------------------------------------------------------------------------------ drag check


def drag_stats(seed: Mapping) -> dict[str, float]:
    """Per qualified player: |mean(synthetic fields) - mean(real fields)| and T' - T."""
    gaps, t_gaps = [], []
    for row in qualified_rows(seed):
        real = set(real_field_names(row))
        real_vals = [row["ratings"][f] for f in real]
        synth_vals = [row["ratings"][f] for f in RATING_FIELD_ORDER if f not in real]
        gaps.append(abs(mean(synth_vals) - mean(real_vals)))
        t_gaps.append(row["meta"]["synthetic_talent"] - row["meta"]["talent"])
    within = sum(1 for g in gaps if g <= DRAG_MEAN_LIMIT)
    return {
        "n": len(gaps),
        "share_within": within / len(gaps) if gaps else 0.0,
        "mean_gap": mean(gaps) if gaps else 0.0,
        "t_prime_minus_t_mean": mean(t_gaps) if t_gaps else 0.0,
        "t_prime_minus_t_sd": pstdev(t_gaps) if t_gaps else 0.0,
    }


def check_drag(seeds: Mapping[int, dict]) -> list[Check]:
    checks = []
    for season in sorted(seeds):
        stats = drag_stats(seeds[season])
        ok = (
            stats["share_within"] >= DRAG_POOL_SHARE
            and stats["t_prime_minus_t_sd"] <= DRAG_SD_LIMIT
        )
        checks.append(
            Check(
                f"drag {season}",
                "PASS" if ok else "FAIL",
                f"{stats['share_within']:.1%} of {stats['n']} qualified within "
                f"{DRAG_MEAN_LIMIT:g} (need {DRAG_POOL_SHARE:.0%}); mean |gap| "
                f"{stats['mean_gap']:.2f}; T'-T mean {stats['t_prime_minus_t_mean']:+.2f} "
                f"SD {stats['t_prime_minus_t_sd']:.2f} (limit {DRAG_SD_LIMIT:g})",
            )
        )
    return [*checks, _pooled_drag(seeds)]


def _pooled_drag(seeds: Mapping[int, dict]) -> Check:
    rows = [r for s in seeds.values() for r in qualified_rows(s)]
    merged = {"players": rows}
    stats = drag_stats(merged)
    ok = stats["share_within"] >= DRAG_POOL_SHARE and stats["t_prime_minus_t_sd"] <= DRAG_SD_LIMIT
    return Check(
        "drag pooled",
        "PASS" if ok else "FAIL",
        f"{stats['share_within']:.1%} of {stats['n']} within {DRAG_MEAN_LIMIT:g}; mean |gap| "
        f"{stats['mean_gap']:.2f}; T'-T SD {stats['t_prime_minus_t_sd']:.2f}",
    )


# ------------------------------------------------------------------------------ solver sanity


def field_shape(rows: Sequence[Mapping]) -> dict[str, dict[str, float]]:
    """Per-field pegging share (max of the share at 25 and at 99), pooled cross-player SD
    of each real field, and its SD within a position (offsets removed)."""
    pegged: dict[str, float] = {}
    for name in RATING_FIELD_ORDER:
        values = [r["ratings"][name] for r in rows]
        pegged[name] = max(sum(v <= 25 for v in values), sum(v >= 99 for v in values)) / len(values)
    pooled: dict[str, float] = {}
    within: dict[str, float] = {}
    for name in REAL_FIELDS:
        holders = [r for r in rows if name in real_field_names(r)]
        if len(holders) < 2:
            continue  # e.g. the advanced three before 2012: synthetic for every row
        pooled[name] = pstdev([r["ratings"][name] for r in holders]) if len(holders) > 1 else 0.0
        by_pos: dict[str, list[int]] = defaultdict(list)
        for r in holders:
            by_pos[r["primary_position"]].append(r["ratings"][name])
        sds = [pstdev(v) for v in by_pos.values() if len(v) > 1]
        within[name] = mean(sds) if sds else 0.0
    return {"pegged": pegged, "pooled_sd": pooled, "within_position_sd": within}


def synthetic_baseline() -> dict[str, dict[str, float]]:
    """`field_shape` of the synthetic league the sim was calibrated on: the tier ladder's
    420 talents (three times over for stability) with uniformly drawn positions, through
    `synthetic_ratings`. The design's pegging and SD thresholds are only meaningful next
    to this."""
    from random import Random

    from dba2.sim.derivation import synthetic_ratings
    from tools._gm_real_player_build import tier_ladder

    rng = Random(1)
    rows = []
    for i, talent in enumerate(tier_ladder() * 3):
        position = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)[
            rng.randrange(5)
        ]
        rows.append(
            {
                "ratings": synthetic_ratings(talent, position, Random(f"baseline:{i}")),
                "primary_position": position.value,
                "meta": {"band": "advanced_tracking"},
            }
        )
    return field_shape(rows)


def check_baseline(seeds: Mapping[int, dict]) -> list[Check]:
    base = synthetic_baseline()
    rows = [r for s in seeds.values() for r in qualified_rows(s)]
    mine = field_shape(rows)

    def summary(shape: dict[str, dict[str, float]]) -> str:
        over = [n for n, v in shape["pegged"].items() if v > 0.03]
        return (
            f"pegged max {max(shape['pegged'].values()):.1%} ({len(over)} fields over 3%: "
            f"{', '.join(over) or 'none'}); pooled real-field SD "
            f"{min(shape['pooled_sd'].values()):.1f}-{max(shape['pooled_sd'].values()):.1f}; "
            f"within-position SD {min(shape['within_position_sd'].values()):.1f}-"
            f"{max(shape['within_position_sd'].values()):.1f}"
        )

    return [
        Check("baseline synthetic league (same field names)", "INFO", summary(base)),
        Check("baseline v2 rebuilt (qualified, all seasons)", "INFO", summary(mine)),
    ]


def check_solver(seeds: Mapping[int, dict]) -> list[Check]:
    checks: list[Check] = []
    for season in sorted(seeds):
        rows = qualified_rows(seeds[season])
        ladder = sorted(rows, key=lambda r: r["meta"]["impact_rank"])[:LADDER_SLOTS]
        overall = {r["external_ref"]: overall_of(r) for r in seeds[season]["players"]}
        top = [overall[r["external_ref"]] for r in ladder]
        mean_top, sd_top = mean(top), pstdev(top)
        top30 = mean(top[:30])
        ok = abs(mean_top - 69.7) <= 2 and 6.0 <= sd_top <= 9.0 and abs(top30 - 84.0) <= 3
        checks.append(
            Check(
                f"solver {season} top-420 overall",
                "PASS" if ok else "FAIL",
                f"mean {mean_top:.2f} (69.7+-2), SD {sd_top:.2f} (6-9), top-30 mean {top30:.2f} "
                "(84+-3); tail beyond the 420 ladder slots excluded by definition",
            )
        )
        by_pos: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            by_pos[r["primary_position"]].append(overall[r["external_ref"]] - r["meta"]["talent"])
        worst = max(abs(mean(v)) for v in by_pos.values())
        checks.append(
            Check(
                f"solver {season} position bias",
                "PASS" if worst <= 0.5 else "FAIL",
                "mean(overall - T) "
                + ", ".join(f"{p} {mean(by_pos[p]):+.2f}" for p in sorted(by_pos))
                + " (each within +-0.5)",
            )
        )
        shape = field_shape(rows)
        pegged = [f"{n} {v:.1%}" for n, v in shape["pegged"].items() if v > 0.03]
        checks.append(
            Check(
                f"solver {season} pegged fields",
                "PASS" if not pegged else "FAIL",
                "none above 3%" if not pegged else "over 3% at 25 or 99: " + ", ".join(pegged),
            )
        )
        sds = shape["pooled_sd"]
        bad = {n: v for n, v in sds.items() if not 5.0 <= v <= 12.0}
        within = shape["within_position_sd"]
        checks.append(
            Check(
                f"solver {season} real-field SD",
                "PASS" if not bad else "FAIL",
                f"pooled range {min(sds.values()):.1f}-{max(sds.values()):.1f} (need 5-12); "
                f"within-position {min(within.values()):.1f}-{max(within.values()):.1f}"
                + ("" if not bad else f"; {len(bad)} pooled outside (see baseline line)"),
            )
        )
        rho = spearman(
            [-r["meta"]["impact_rank"] for r in ladder],
            [overall[r["external_ref"]] for r in ladder],
        )
        checks.append(
            Check(
                f"solver {season} Spearman(I, overall)",
                "PASS" if rho >= 0.9 else "FAIL",
                f"{rho:.3f} (need >= 0.9)",
            )
        )
        misses = sum(
            1
            for r in seeds[season]["players"]
            if abs(overall[r["external_ref"]] - round(r["meta"]["talent"])) > 1
        )
        checks.append(
            Check(
                f"solver {season} fit tolerance",
                "PASS" if misses == 0 else "FAIL",
                f"{misses} of {len(seeds[season]['players'])} players are more than 1 point "
                "from round(T) (any miss fails: the solver is broken)",
            )
        )
    return checks


def check_extreme_pegging(seeds: Mapping[int, dict]) -> Check:
    """Informational (critic minor 4): when the real fields sit far above what the target
    overall allows (Jokic 2025: T' 107), the solver pays by pegging the synthetic remainder
    at 99. Counts rows where at least half of the synthetic fields are pegged at 25 or 99."""
    total = extreme = 0
    worst = 0.0
    by_season: dict[int, int] = {}
    for season, seed in sorted(seeds.items()):
        for row in seed["players"]:
            real = set(real_field_names(row))
            synth = [row["ratings"][f] for f in RATING_FIELD_ORDER if f not in real]
            pegged = sum(1 for v in synth if v <= 25 or v >= 99) / len(synth)
            total += 1
            worst = max(worst, pegged)
            if pegged >= EXTREME_PEG_SHARE:
                extreme += 1
                by_season[season] = by_season.get(season, 0) + 1
    detail = ", ".join(f"{s}: {n}" for s, n in by_season.items()) or "none"
    return Check(
        "extreme T' pegging",
        "INFO",
        f"{extreme} of {total} rows ({extreme / total if total else 0.0:.2%}) have >= "
        f"{EXTREME_PEG_SHARE:.0%} of their synthetic fields at 25/99 (worst row "
        f"{worst:.0%}); by season: {detail}",
    )


def evidence_share(
    seeds: Mapping[int, dict], band: tuple[float, float] | None = EVIDENCE_BAND
) -> dict[str, float]:
    """Per real field, over qualified rows (with talent T inside `band`, or all of them
    when `band` is None): `Var(e) / (Var(e) + Var(T))` where `e = rating - T - offset` is
    the evidence term (`u_spread * u` plus rounding) and T the mapped talent. It answers
    whether a real field is decided by the player's own numbers or by his impact rank:
    within a narrow band T barely varies so evidence dominates by construction; over the
    whole pool it is the honest share (critic M1 measured 22% at u_spread 4)."""
    values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for seed in seeds.values():
        for row in qualified_rows(seed):
            talent = row["meta"]["talent"]
            if band is not None and not band[0] <= talent <= band[1]:
                continue
            offsets = POSITION_RATING_OFFSETS[Position(row["primary_position"])]
            for name in real_field_names(row):
                rating = row["ratings"][name]
                if 25 < rating < 99:  # a clamped rating carries no evidence information
                    values[name].append((talent, rating - offsets[name] - talent))
    out = {}
    for name, pairs in values.items():
        if len(pairs) < 30:
            continue
        evidence, talent_var = pvariance([p[1] for p in pairs]), pvariance([p[0] for p in pairs])
        if evidence + talent_var > 0:
            out[name] = evidence / (evidence + talent_var)
    return out


def check_evidence_share(seeds: Mapping[int, dict]) -> Check:
    in_band = evidence_share(seeds)
    pool = evidence_share(seeds, band=None)
    lo, hi = EVIDENCE_BAND
    if not in_band or not pool:
        return Check(EVIDENCE_CHECK_NAME, "SKIPPED", "no qualified rows to measure")
    low = min(pool, key=pool.get)
    high = max(pool, key=pool.get)
    return Check(
        EVIDENCE_CHECK_NAME,
        "INFO",
        f"evidence / (evidence + talent) variance of a real field, qualified players: whole "
        f"pool mean {mean(pool.values()):.1%} across {len(pool)} fields (lowest {low} "
        f"{pool[low]:.1%}, highest {high} {pool[high]:.1%}); within talent {lo:g}-{hi:g} "
        f"mean {mean(in_band.values()):.1%} (talent barely varies there, so it is high by "
        "construction)",
    )


# ------------------------------------------------------------------------------ clamp (RPD-8-8)


def _is_measured(row: Mapping, field_name: str) -> bool:
    """Whether `field_name` is a real (measured) rating on this row: present, and not one
    of the advanced three on a row the builder marked box-score-only (there they are
    synthetic, and counting them would put synthetic noise in a real-field rate)."""
    if field_name not in row["ratings"]:
        return False
    band = (row.get("meta") or {}).get("band")
    return not (band == "box_score_only" and field_name in ADVANCED_REAL_FIELDS)


def _career_points(
    seeds: Mapping[int, dict], value_of, fields: Sequence[str]
) -> dict[str, list[tuple[int, dict[str, float]]]]:
    raw: dict[str, list[tuple[int, int | None, dict[str, float]]]] = defaultdict(list)
    for season, seed in sorted(seeds.items()):
        for row in seed["players"]:
            values = {f: value_of(row, f) for f in fields if _is_measured(row, f)}
            raw[row["external_ref"]].append((season, row["draft_year"], values))
    out = {}
    for ref, entries in raw.items():
        draft = entries[0][1]
        anchor = draft if draft is not None else min(s for s, _, _ in entries)
        out[ref] = sorted(
            ((s - anchor + _DRAFT_AGE_ANCHOR, v) for s, _, v in entries), key=lambda p: p[0]
        )
    return out


def _interp(points: list[tuple[int, float]], age: int) -> float:
    for (a0, v0), (a1, v1) in zip(points, points[1:], strict=False):
        if a0 <= age <= a1:
            return v0 if a1 == a0 else v0 + (age - a0) / (a1 - a0) * (v1 - v0)
    return points[-1][1]


def clamp_measurement(
    seeds: Mapping[int, dict], fields: Sequence[str], value_of=None
) -> dict[str, float]:
    """The RPD-6 measurement: for every player with career points bracketing an age, the
    per-field expected delta (`expected_delta_at_age`'s interpolation) and how often
    |delta| exceeds the progression stub's cap. `value_of(row, field)` defaults to the
    rating; rows are only compared within `seeds`, so pass version-2 seasons only. A field
    counts only for the rows where it is measured (`_is_measured`)."""
    value_of = value_of or (lambda row, f: float(row["ratings"][f]))
    careers = _career_points(seeds, value_of, fields)
    calls = clamped = 0
    abs_deltas: list[float] = []
    for entries in careers.values():
        if len(entries) < 2:
            continue
        lo, hi = entries[0][0], entries[-1][0]
        for age in range(lo, hi):
            for f in fields:
                points = [(a, v[f]) for a, v in entries if f in v]
                if len(points) < 2 or not points[0][0] <= age < points[-1][0]:
                    continue
                delta = _interp(points, age + 1) - _interp(points, age)
                calls += 1
                abs_deltas.append(abs(delta))
                clamped += abs(delta) > CLAMP_CAP
    return {
        "calls": calls,
        "clamp_rate": clamped / calls if calls else 0.0,
        "mean_abs": mean(abs_deltas) if abs_deltas else 0.0,
        "median_abs": median(abs_deltas) if abs_deltas else 0.0,
    }


def check_clamp(seeds: Mapping[int, dict], legacy: Mapping[int, dict]) -> list[Check]:
    v2 = clamp_measurement(seeds, REAL_FIELDS)
    talent_free = clamp_measurement(
        seeds,
        REAL_FIELDS,
        value_of=lambda row, f: row["ratings"][f] - row["meta"]["talent"],
    )
    fifteen = (*BOX_SCORE_REAL_FIELDS, *ADVANCED_REAL_FIELDS)
    same_seasons = {s: legacy[s] for s in seeds if s in legacy}
    old = clamp_measurement(same_seasons, fifteen) if same_seasons else None
    checks = [
        Check(
            f"RPD-6 clamp rate ({len(REAL_FIELDS)} real fields, v2)",
            "PASS" if v2["clamp_rate"] <= CLAMP_THRESHOLD else "FAIL",
            f"{v2['clamp_rate']:.1%} of {v2['calls']} field-deltas exceed +-{CLAMP_CAP:g} "
            f"(threshold {CLAMP_THRESHOLD:.0%}); mean |delta| {v2['mean_abs']:.2f}, "
            f"median {v2['median_abs']:.1f}",
        ),
        Check(
            "RPD-6 clamp rate (u only: rating - T)",
            "INFO",
            f"{talent_free['clamp_rate']:.1%}; mean |delta| {talent_free['mean_abs']:.2f} -- "
            "the fallback RPD-8-8 names if the full-rating rate exceeds 20%",
        ),
    ]
    if old:
        checks.append(
            Check(
                "RPD-6 clamp rate (15 legacy fields, same seasons)",
                "INFO",
                f"{old['clamp_rate']:.1%} of {old['calls']}; mean |delta| {old['mean_abs']:.2f} "
                "(RPD-6 recorded 13.8% over the full 30-season backfill)",
            )
        )
    return checks


# ------------------------------------------------------------------------------ named cases


def _case_facts(
    seeds: Mapping[int, dict], legacy: Mapping[int, dict], name: str, season: int
) -> dict | None:
    seed = seeds.get(season)
    if seed is None:
        return None
    row = find_player(seed, name)
    if row is None:
        return None
    overall = {r["external_ref"]: overall_of(r) for r in seed["players"]}
    place = placements(overall)[row["external_ref"]]
    facts = {
        "row": row,
        "place": place,
        "team_ranks": None,
        "old_overall": None,
        "old_rank": None,
    }
    team = row.get("team", {}).get("abbreviation")
    facts["team_ranks"] = sorted(
        (overall[r["external_ref"]], r["external_ref"])
        for r in seed["players"]
        if r.get("team", {}).get("abbreviation") == team
    )[::-1]
    old_seed = legacy.get(season)
    if old_seed:
        old_row = next(
            (r for r in old_seed["players"] if r["external_ref"] == row["external_ref"]), None
        )
        if old_row:
            old_overall = {r["external_ref"]: overall_of(r) for r in old_seed["players"]}
            facts["old_overall"] = old_overall[old_row["external_ref"]]
            facts["old_rank"] = placements(old_overall)[old_row["external_ref"]].rank_best
    return facts


def _describe(name: str, season: int, facts: dict) -> str:
    row, place, meta = facts["row"], facts["place"], facts["row"]["meta"]
    old = (
        "n/a"
        if facts["old_overall"] is None
        else f"{facts['old_overall']} (rank {facts['old_rank']})"
    )
    return (
        f"{name} {season}: overall {place.overall} rank {place.rank_best}"
        f"{'' if place.rank_best == place.rank_worst else f'-{place.rank_worst}'} "
        f"[T {meta['talent']:.1f}, I-rank {meta['impact_rank']}, "
        f"{meta['minutes_total']:.0f} min in "
        f"{meta['games_played']} g, +{meta['blend_filled_minutes']:.0f} blended] legacy {old} "
        f"{row['primary_position']} {row.get('team', {}).get('abbreviation', '?')}"
    )


def check_tuning(seeds: Mapping[int, dict], legacy: Mapping[int, dict]) -> list[Check]:
    out = []
    for name, season in TUNING_CASES:
        facts = _case_facts(seeds, legacy, name, season)
        if facts is None:
            out.append(
                Check(f"tuning {name} {season}", "SKIPPED", "season not built or player absent")
            )
            continue
        place = facts["place"]
        moved = facts["old_overall"] is None or place.overall > facts["old_overall"]
        status = "PASS" if place.rank_worst <= 40 and moved else "FAIL"
        out.append(Check(f"tuning {name} {season}", status, _describe(name, season, facts)))
    return out


def check_validation(seeds: Mapping[int, dict], legacy: Mapping[int, dict]) -> list[Check]:
    out: list[Check] = []

    def top_n(name: str, season: int, limit: int, label: str) -> None:
        facts = _case_facts(seeds, legacy, name, season)
        if facts is None:
            out.append(Check(label, "SKIPPED", f"{name} {season} not built"))
            return
        out.append(
            Check(label, status_within(facts["place"], limit), _describe(name, season, facts))
        )

    for name in ("Jayson Tatum", "Jaylen Brown"):
        top_n(name, 2023, 25, f"validation {name} 2023 top 25")
    for name in ("Jayson Tatum", "Jaylen Brown"):
        facts = _case_facts(seeds, legacy, name, 2023)
        if facts is None:
            out.append(Check(f"validation {name} 2023 top two on team", "SKIPPED", "not built"))
            continue
        ranks = facts["team_ranks"]
        mine = overall_of(facts["row"])
        above = sum(1 for o, _ in ranks if o > mine)
        equal = sum(1 for o, _ in ranks if o == mine)
        status = (
            "PASS" if above + equal <= 2 else "PASS (tie-dependent)" if above + 1 <= 2 else "FAIL"
        )
        out.append(
            Check(
                f"validation {name} 2023 top two on team",
                status,
                f"{facts['row'].get('team', {}).get('abbreviation')} team overalls "
                f"{[o for o, _ in ranks[:5]]}, his {mine}",
            )
        )
    top_n("Jayson Tatum", 2025, 40, "validation Jayson Tatum 2025 top 40")
    for name in STARS_TOP15:
        for season in (2023, 2024):
            top_n(name, season, 15, f"validation {name} {season} top 15")
    for name, season in (("Ja Morant", 2023), ("Anthony Davis", 2025), ("Joel Embiid", 2024)):
        facts = _case_facts(seeds, legacy, name, season)
        label = f"validation {name} {season} up vs legacy and top 60"
        if facts is None:
            out.append(Check(label, "SKIPPED", "not built"))
            continue
        moved = facts["old_overall"] is not None and facts["place"].overall > facts["old_overall"]
        status = status_within(facts["place"], 60) if moved else "FAIL"
        out.append(Check(label, status, _describe(name, season, facts)))
    return out


# ------------------------------------------------------------------------------ RPC-12 Tier 1
#
# docs/design/real-player-rating-calibration.md RPC-12, Tier 1: seed-level checks against
# the ground truth in seeds/real_players_raw (never anyone's memory). Opt-in with
# `--calibration`, so the RPD-8 report's own lines and exit code are unchanged; T5 reads
# named players, so it is a validation read and prints only with `--validation` as well.
# `--baseline-dir` supplies the "before" seeds for the bars that compare against them.

RAW_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players_raw"
CALIBRATION_FIELD = "rim_protection"
CALIBRATION_SEASONS_FROM = 2013  # T1's per-season bar covers 2013-2025
T1_POOLED_BAR = 0.75
T1_SEASON_BAR = 0.65
# T2-T4: (label, rating field, truth key, absolute floor or None)
CONTROL_CORRELATIONS: tuple[tuple[str, str, str, float | None], ...] = (
    ("T2", "defensive_rebounding", "dreb36", 0.85),
    ("T3", "passing_vision", "ast36", 0.80),
    ("T4", "ball_security", "tov_security", None),
    ("T4", "free_throw", "ft_pct", None),
)
T_PRIME_RANGE = (40.0, 97.0)  # nit 4: `_TALENT_CLAMP`, which binds T but not T'
T8_REGRESSION = 0.05
T11_TOP = 20
T12_MIN_PAIR_SD = 3.0
T5_SEASON = 2025
T5_ANCHOR_TOP = 5  # Wembanyama inside
T5_ANCHOR_OUTSIDE = 60  # Jokic outside
T5_PASSING_TOP = 3  # Jokic inside, by passing_vision (the anti-nerf control)
T5_LIST = 15
# The expected value of seed_dev_league's per-slot wingspan draw (height + 3.5), used for
# a row that stores no `wingspan_in` of its own (RPC-10).
EXPECTED_WINGSPAN_OFFSET = 3.5


def load_raw_truth(raw_dir: Path, seasons: Sequence[int]) -> dict[int, dict[str, dict]]:
    """Per season, per external_ref: the raw-season per-36 rates and percentages the
    Tier-1 correlations compare against. A season without a raw file is absent."""
    out: dict[int, dict[str, dict]] = {}
    for season in seasons:
        path = raw_dir / f"{season}.json"
        if not path.exists():
            continue
        truth: dict[str, dict] = {}
        for entry in json.loads(path.read_text(encoding="utf-8"))["players"]:
            totals, minutes = entry["totals"], float(entry["minutes_total"])
            per36 = 36.0 / minutes if minutes > 0 else 0.0
            used = totals["fga"] + 0.44 * totals["fta"] + totals["turnover"]
            truth[str(entry["external_ref"])] = {
                "minutes": minutes,
                "blk36": totals["blk"] * per36,
                "dreb36": totals["dreb"] * per36,
                "ast36": totals["ast"] * per36,
                "reb36": (totals["oreb"] + totals["dreb"]) * per36,
                "stl36": totals["stl"] * per36,
                "tov36": totals["turnover"] * per36,
                "pts36": totals["pts"] * per36,
                "tov_security": 1.0 - (totals["turnover"] / used if used > 0 else 0.0),
                "ft_pct": totals["ftm"] / totals["fta"] if totals["fta"] > 0 else 0.0,
            }
        out[season] = truth
    return out


def _joined(seed: Mapping, truth: Mapping[str, dict]) -> list[tuple[dict, dict]]:
    return [
        (r, truth[r["external_ref"]]) for r in qualified_rows(seed) if r["external_ref"] in truth
    ]


def field_truth_spearman(
    seeds: Mapping[int, dict], truth: Mapping[int, dict], rating: str, key: str
) -> tuple[float, dict[int, float]]:
    """Spearman(rating, real stat) over the qualified pool: pooled over every season with
    ground truth, and per season."""
    pooled_x: list[float] = []
    pooled_y: list[float] = []
    by_season: dict[int, float] = {}
    for season in sorted(seeds):
        if season not in truth:
            continue
        pairs = _joined(seeds[season], truth[season])
        xs = [r["ratings"][rating] for r, _ in pairs]
        ys = [t[key] for _, t in pairs]
        by_season[season] = spearman(xs, ys)
        pooled_x += xs
        pooled_y += ys
    return spearman(pooled_x, pooled_y), by_season


def _no_truth(seeds: Mapping[int, dict], truth: Mapping[int, dict], name: str) -> Check | None:
    if not any(s in truth for s in seeds):
        return Check(name, "SKIPPED", "no raw season files for these seeds")
    return None


def check_t1(seeds: Mapping[int, dict], truth: Mapping[int, dict]) -> Check:
    name = f"T1 spearman({CALIBRATION_FIELD}, real blk/36)"
    skipped = _no_truth(seeds, truth, name)
    if skipped:
        return skipped
    pooled, by_season = field_truth_spearman(seeds, truth, CALIBRATION_FIELD, "blk36")
    graded = {s: v for s, v in by_season.items() if s >= CALIBRATION_SEASONS_FROM}
    worst = min(graded, key=graded.get) if graded else None
    ok = pooled >= T1_POOLED_BAR and all(v >= T1_SEASON_BAR for v in graded.values())
    return Check(
        name,
        "PASS" if ok else "FAIL",
        f"pooled {pooled:.3f} (need >= {T1_POOLED_BAR}); worst season "
        + (f"{worst} {graded[worst]:.3f}" if worst else "n/a")
        + f" (need >= {T1_SEASON_BAR} every season {CALIBRATION_SEASONS_FROM}+); per season "
        + ", ".join(f"{s} {v:.3f}" for s, v in by_season.items()),
    )


def check_controls(
    seeds: Mapping[int, dict],
    truth: Mapping[int, dict],
    baseline: Mapping[int, dict] | None,
) -> list[Check]:
    out: list[Check] = []
    for label, rating, key, floor in CONTROL_CORRELATIONS:
        name = f"{label} spearman({rating}, real {key}) control"
        skipped = _no_truth(seeds, truth, name)
        if skipped:
            out.append(skipped)
            continue
        pooled, _ = field_truth_spearman(seeds, truth, rating, key)
        base = field_truth_spearman(baseline, truth, rating, key)[0] if baseline else None
        ok = (floor is None or pooled >= floor) and (base is None or pooled >= base - 1e-12)
        out.append(
            Check(
                name,
                "PASS" if ok else "FAIL",
                f"pooled {pooled:.4f}"
                + (f" (need >= {floor})" if floor is not None else "")
                + (
                    f"; baseline {base:.4f} (must not regress)"
                    if base is not None
                    else "; no --baseline-dir, so 'not below baseline' is unchecked"
                ),
            )
        )
    return out


def _wingspan_in(row: Mapping) -> float:
    stored = row.get("wingspan_in")
    return float(stored) if stored is not None else row["height_in"] + EXPECTED_WINGSPAN_OFFSET


def contest_scalar(row: Mapping, tuning=None) -> float:
    """`composites.rim_protection_contribution` for a seed row: the block contest's
    defensive scalar. Wingspan is the row's own `wingspan_in` when it stores one
    (RPC-10), else the expected value of seed_dev_league's per-slot draw."""
    from dba2.sim.composites import normalize_physical, rim_protection_contribution
    from dba2.sim.tuning import TuningProfile

    tuning = tuning or TuningProfile()
    normalized = normalize_physical(_wingspan_in(row), tuning.physical_anchors.wingspan)
    return rim_protection_contribution(SimpleNamespace(**row["ratings"]), normalized, tuning)


def check_t5(seeds: Mapping[int, dict], truth: Mapping[int, dict]) -> list[Check]:
    """RPC-12 T5 (a validation read): the 2025 qualified pool ranked by the contest
    scalar, ties broken by real blk/36 descending then external_ref."""
    seed = seeds.get(T5_SEASON)
    if seed is None or T5_SEASON not in truth:
        return [Check("T5 named anchors", "SKIPPED", f"season {T5_SEASON} not built or no raw")]
    from dba2.sim.tuning import TuningProfile

    tuning = TuningProfile()
    pairs = _joined(seed, truth[T5_SEASON])
    scored = sorted(
        ((contest_scalar(r, tuning), t["blk36"], r) for r, t in pairs),
        key=lambda x: (-x[0], -x[1], x[2]["external_ref"]),
    )
    contest_rank = {x[2]["external_ref"]: i for i, x in enumerate(scored, start=1)}
    ties_at_99 = sum(1 for r, _ in pairs if r["ratings"][CALIBRATION_FIELD] >= 99)

    def listing(chosen: Sequence[tuple[dict, dict]]) -> str:
        return "; ".join(
            f"{r['first_name']} {r['last_name']} {r['primary_position']} {t['blk36']:.2f} "
            f"-> #{contest_rank[r['external_ref']]}"
            for r, t in chosen
        )

    by_blk = sorted(pairs, key=lambda p: (-p[1]["blk36"], p[0]["external_ref"]))
    out = [
        Check(
            f"T5 {T5_SEASON} tie count at {CALIBRATION_FIELD} 99",
            "INFO",
            f"{ties_at_99} of {len(pairs)} qualified (reported, not graded)",
        ),
        Check(
            f"T5 {T5_SEASON} highest real blk/36 -> contest rank",
            "INFO",
            listing(by_blk[:T5_LIST]),
        ),
        Check(
            f"T5 {T5_SEASON} lowest real blk/36 -> contest rank",
            "INFO",
            listing(by_blk[-T5_LIST:]),
        ),
    ]

    def anchor(name: str, want_inside: bool, limit: int) -> Check:
        row = find_player(seed, name)
        title = f"T5 {name} {'inside' if want_inside else 'outside'} the contest top {limit}"
        if row is None or row["external_ref"] not in contest_rank:
            return Check(title, "SKIPPED", "not in the qualified pool")
        rank = contest_rank[row["external_ref"]]
        ok = rank <= limit if want_inside else rank > limit
        ratings = row["ratings"]
        return Check(
            title,
            "PASS" if ok else "FAIL",
            f"contest rank {rank} of {len(scored)}, scalar {contest_scalar(row, tuning):.3f}; "
            f"{CALIBRATION_FIELD} {ratings[CALIBRATION_FIELD]}, vertical {ratings['vertical']}, "
            f"lateral_quickness {ratings['lateral_quickness']}, wingspan "
            f"{_wingspan_in(row):g}in, {row['primary_position']}",
        )

    out.append(anchor("Victor Wembanyama", True, T5_ANCHOR_TOP))
    out.append(anchor("Nikola Jokic", False, T5_ANCHOR_OUTSIDE))
    jokic = find_player(seed, "Nikola Jokic")
    title = f"T5 Nikola Jokic inside the top {T5_PASSING_TOP} by passing_vision"
    if jokic is None or jokic["external_ref"] not in contest_rank:
        out.append(Check(title, "SKIPPED", "not in the qualified pool"))
    else:
        vision = {r["external_ref"]: r["ratings"]["passing_vision"] for r, _ in pairs}
        place = placements(vision)[jokic["external_ref"]]
        out.append(
            Check(
                title,
                status_within(place, T5_PASSING_TOP),
                f"passing_vision {place.overall}, rank {place.rank_best}-{place.rank_worst}",
            )
        )
    return out


def _pegged_share(rows: Sequence[Mapping], name: str) -> tuple[float, float]:
    values = [r["ratings"][name] for r in rows]
    n = len(values) or 1
    return sum(v <= 25 for v in values) / n, sum(v >= 99 for v in values) / n


def check_t6(seeds: Mapping[int, dict], baseline: Mapping[int, dict] | None) -> Check:
    shares = {s: _pegged_share(qualified_rows(seeds[s]), CALIBRATION_FIELD) for s in sorted(seeds)}
    detail = "at 25/99 " + ", ".join(f"{s} {lo:.1%}/{hi:.1%}" for s, (lo, hi) in shares.items())
    if baseline is None:
        return Check(f"T6 {CALIBRATION_FIELD} pegged share (qualified)", "INFO", detail)
    moved = [
        s
        for s in shares
        if s in baseline
        and shares[s] != _pegged_share(qualified_rows(baseline[s]), CALIBRATION_FIELD)
    ]
    return Check(
        f"T6 {CALIBRATION_FIELD} pegged share unchanged vs baseline",
        "PASS" if not moved else "FAIL",
        ("unchanged in every season; " if not moved else f"moved in {moved}; ") + detail,
    )


def percentile_of(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default method), `q` in [0, 100]."""
    if not sorted_values:
        return 0.0
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def drag_gaps(seed: Mapping) -> list[float]:
    """`drag_stats`'s per-row statistic, |mean(synthetic) - mean(real)|: a difference of
    two group means, not a pooled mean (RPC-8-5)."""
    gaps = []
    for row in qualified_rows(seed):
        real = set(real_field_names(row))
        real_vals = [row["ratings"][f] for f in real]
        synth_vals = [row["ratings"][f] for f in RATING_FIELD_ORDER if f not in real]
        gaps.append(abs(mean(synth_vals) - mean(real_vals)))
    return gaps


def check_t7(seeds: Mapping[int, dict]) -> Check:
    per = {s: percentile_of(sorted(drag_gaps(seeds[s])), 95.0) for s in sorted(seeds)}
    pooled = percentile_of(sorted(g for s in seeds.values() for g in drag_gaps(s)), 95.0)
    stats = {s: drag_stats(seeds[s]) for s in sorted(seeds)}
    worst = max(per, key=per.get)
    worst_share = min(stats, key=lambda s: stats[s]["share_within"])
    ok = all(
        st["share_within"] >= DRAG_POOL_SHARE and st["t_prime_minus_t_sd"] <= DRAG_SD_LIMIT
        for st in stats.values()
    )
    return Check(
        "T7 drag (share within 8, SD(T'-T), p95 of |gap|)",
        "PASS" if ok else "FAIL",
        f"pooled p95 {pooled:.3f}; worst-season p95 {per[worst]:.3f} ({worst}); worst share "
        f"within {DRAG_MEAN_LIMIT:g} {stats[worst_share]['share_within']:.1%} ({worst_share}); "
        f"max SD(T'-T) {max(st['t_prime_minus_t_sd'] for st in stats.values()):.2f}; p95 by "
        "season " + ", ".join(f"{s} {v:.3f}" for s, v in per.items()),
    )


def t_prime_excursions(seeds: Mapping[int, dict]) -> dict[str, float]:
    """Over every row (qualified or not): how many have T' outside the talent clamp,
    the largest |T' - T|, and SD(T' - T)."""
    rows = [r for s in seeds.values() for r in s["players"]]
    lo, hi = T_PRIME_RANGE
    gaps = [r["meta"]["synthetic_talent"] - r["meta"]["talent"] for r in rows]
    return {
        "rows": len(rows),
        "outside": sum(1 for r in rows if not lo <= r["meta"]["synthetic_talent"] <= hi),
        "max_abs_gap": max(abs(g) for g in gaps) if gaps else 0.0,
        "sd": pstdev(gaps) if len(gaps) > 1 else 0.0,
    }


def _jokic_t_prime(seeds: Mapping[int, dict]) -> str:
    row = find_player(seeds[T5_SEASON], "Nikola Jokic") if T5_SEASON in seeds else None
    return f"{row['meta']['synthetic_talent']:.3f}" if row else "n/a"


def check_t8(seeds: Mapping[int, dict], baseline: Mapping[int, dict] | None) -> Check:
    now = t_prime_excursions(seeds)
    detail = (
        f"{now['outside']} of {now['rows']} rows have T' outside [{T_PRIME_RANGE[0]:g}, "
        f"{T_PRIME_RANGE[1]:g}]; max |T'-T| {now['max_abs_gap']:.2f}; SD(T'-T) {now['sd']:.3f}; "
        f"Jokic {T5_SEASON} T' {_jokic_t_prime(seeds)}"
    )
    if baseline is None:
        return Check("T8 T' excursions", "INFO", detail)
    base = t_prime_excursions(baseline)
    ok = now["outside"] <= base["outside"] * (1 + T8_REGRESSION) and now["sd"] <= base["sd"] * (
        1 + T8_REGRESSION
    )
    return Check(
        "T8 T' excursions (pooled count and SD within 5% of baseline)",
        "PASS" if ok else "FAIL",
        f"{detail}; baseline {base['outside']} outside, SD {base['sd']:.3f}, Jokic T' "
        f"{_jokic_t_prime(baseline)}",
    )


def check_t9(seeds: Mapping[int, dict]) -> Check:
    rows = [r for s in seeds.values() for r in s["players"]]
    misses = sum(1 for r in rows if overall_of(r) != round(r["meta"]["talent"]))
    return Check(
        "T9 overall == round(T) on every row",
        "PASS" if misses == 0 else "FAIL",
        f"{misses} of {len(rows)} rows miss",
    )


def position_distribution(seeds: Mapping[int, dict], name: str) -> dict[str, tuple]:
    """Per position over the qualified pool: (mean, SD, min, max, n) of field `name`."""
    by_pos: dict[str, list[int]] = defaultdict(list)
    for seed in seeds.values():
        for r in qualified_rows(seed):
            by_pos[r["primary_position"]].append(r["ratings"][name])
    return {
        p: (round(mean(v), 6), round(pstdev(v), 6), min(v), max(v), len(v))
        for p, v in sorted(by_pos.items())
    }


def check_t10(seeds: Mapping[int, dict], baseline: Mapping[int, dict] | None) -> Check:
    now = position_distribution(seeds, CALIBRATION_FIELD)
    detail = "; ".join(
        f"{p} mean {m:.2f} SD {sd:.2f} [{lo}, {hi}] n={n}" for p, (m, sd, lo, hi, n) in now.items()
    )
    if baseline is None:
        return Check(f"T10 {CALIBRATION_FIELD} per position (qualified)", "INFO", detail)
    same = now == position_distribution(baseline, CALIBRATION_FIELD)
    return Check(
        f"T10 {CALIBRATION_FIELD} per-position distribution identical to baseline",
        "PASS" if same else "FAIL",
        detail,
    )


def check_t11(seeds: Mapping[int, dict]) -> Check:
    """No unqualified player, and no qualified player in the bottom quartile of the
    qualified pool's effective minutes, among a season's top 20 by the field (every row at
    or above the 20th-highest value counts, so a tie is never cut arbitrarily)."""
    offenders: list[str] = []
    for season in sorted(seeds):
        rows = seeds[season]["players"]
        qualified = qualified_rows(seeds[season])
        if len(rows) < T11_TOP or not qualified:
            continue
        cut = sorted((r["ratings"][CALIBRATION_FIELD] for r in rows), reverse=True)[T11_TOP - 1]
        q1 = percentile_of(sorted(r["meta"]["effective_minutes"] for r in qualified), 25.0)
        for r in rows:
            if r["ratings"][CALIBRATION_FIELD] < cut:
                continue
            if not r["meta"].get("qualified") or r["meta"]["effective_minutes"] < q1:
                offenders.append(
                    f"{season} {r['first_name']} {r['last_name']} "
                    f"({CALIBRATION_FIELD} {r['ratings'][CALIBRATION_FIELD]}, "
                    f"{r['meta']['effective_minutes']:.0f} eff min"
                    f"{'' if r['meta'].get('qualified') else ', unqualified'})"
                )
    return Check(
        f"T11 thin samples in the top {T11_TOP} by {CALIBRATION_FIELD}",
        "PASS" if not offenders else "FAIL",
        f"{len(offenders)} offenders" + (": " + "; ".join(offenders[:12]) if offenders else ""),
    )


def pairwise_min_sd(rows: Sequence[Mapping]) -> tuple[int, float, tuple[str, str] | None]:
    """The offset-removed SD of the difference of every pair of real fields over `rows`
    (only where both are real), as the builder's distinctness test computes it:
    (pairs measured, min SD, worst pair)."""
    worst: tuple[float, tuple[str, str] | None] = (float("inf"), None)
    count = 0
    for a, b in itertools.combinations(REAL_FIELDS, 2):
        diffs = []
        for row in rows:
            real = real_field_names(row)
            if a in real and b in real:
                offsets = POSITION_RATING_OFFSETS[Position(row["primary_position"])]
                diffs.append((row["ratings"][a] - offsets[a]) - (row["ratings"][b] - offsets[b]))
        if len(diffs) > 30:
            count += 1
            worst = min(worst, (pstdev(diffs), (a, b)))
    return count, worst[0], worst[1]


def check_t12(seeds: Mapping[int, dict], baseline: Mapping[int, dict] | None = None) -> Check:
    """Without a baseline: every season's worst pair clears 3.0. With one: no season's
    worst pair falls below min(3.0, its baseline value) and the pair count is unchanged
    -- 2012-2014's recovered advanced fields sit far below 3.0 before any calibration
    change (a pre-existing property of the legacy recovery), so "unchanged" is the bar a
    `rim_protection`-only change can be held to."""
    parts, ok = [], True
    for season in sorted(seeds):
        count, sd, pair = pairwise_min_sd(seeds[season]["players"])
        floor = T12_MIN_PAIR_SD
        if baseline is not None and season in baseline:
            base_count, base_sd, _ = pairwise_min_sd(baseline[season]["players"])
            floor = min(T12_MIN_PAIR_SD, base_sd)
            ok &= count == base_count
        ok &= sd >= floor - 1e-9
        worst = f" {pair[0]}/{pair[1]}" if pair and sd < T12_MIN_PAIR_SD else ""
        parts.append(f"{season} {count} pairs min {sd:.2f}{worst}")
    title = f"T12 pairwise distinctness (min offset-free pair SD >= {T12_MIN_PAIR_SD:g}"
    title += ", or not below baseline)" if baseline is not None else ")"
    return Check(title, "PASS" if ok else "FAIL", ", ".join(parts))


def permutation_cells(seeds: Mapping[int, dict]) -> list[dict]:
    """RPC-4-5: per (season, position) cell of the qualified pool, what the swap did.
    Reads `meta.rim_protection_evidence_z` (the ranking key) and
    `meta.rim_protection_level_value` (what the level formula gave before the swap); a
    seed built without the swap carries neither and yields no cells."""
    cells: list[dict] = []
    for season in sorted(seeds):
        by_pos: dict[str, list[dict]] = defaultdict(list)
        for r in qualified_rows(seeds[season]):
            if "rim_protection_level_value" in r["meta"]:
                by_pos[r["primary_position"]].append(r)
        for pos, rows in sorted(by_pos.items()):
            evidence = [r["meta"]["rim_protection_evidence_z"] for r in rows]
            old = [r["meta"]["rim_protection_level_value"] for r in rows]
            new = [r["ratings"][CALIBRATION_FIELD] for r in rows]
            moves = sorted(
                (n - o, f"{r['first_name']} {r['last_name']}")
                for r, o, n in zip(rows, old, new, strict=True)
            )
            cells.append(
                {
                    "season": season,
                    "position": pos,
                    "n": len(rows),
                    "at_99": sum(v >= 99 for v in new),
                    "at_25": sum(v <= 25 for v in new),
                    "rho_old": spearman(old, evidence),
                    "rho_new": spearman(new, evidence),
                    "multiset_kept": sorted(old) == sorted(new),
                    "down": moves[0],
                    "up": moves[-1],
                }
            )
    return cells


def check_t13(seeds: Mapping[int, dict]) -> list[Check]:
    cells = permutation_cells(seeds)
    if not cells:
        return [Check("T13 permutation report", "SKIPPED", "seeds carry no swap provenance")]
    leaked = [f"{c['season']} {c['position']}" for c in cells if not c["multiset_kept"]]
    out = [
        Check(
            "T13 swap keeps each cell's value multiset",
            "PASS" if not leaked else "FAIL",
            f"{len(cells)} cells" + (f"; leaked: {', '.join(leaked)}" if leaked else ""),
        )
    ]
    for c in cells:
        out.append(
            Check(
                f"T13 {c['season']} {c['position']}",
                "INFO",
                f"n {c['n']}, at 99/25 {c['at_99']}/{c['at_25']}, spearman vs evidence "
                f"{c['rho_old']:.3f} -> {c['rho_new']:.3f}; largest up {c['up'][1]} "
                f"{c['up'][0]:+d}, largest down {c['down'][1]} {c['down'][0]:+d}",
            )
        )
    return out


def build_calibration_report(
    seeds: Mapping[int, dict],
    truth: Mapping[int, dict],
    baseline: Mapping[int, dict] | None,
    *,
    validation: bool,
) -> list[Check]:
    checks = [check_t1(seeds, truth), *check_controls(seeds, truth, baseline)]
    if validation:
        checks += check_t5(seeds, truth)
    checks += [
        check_t6(seeds, baseline),
        check_t7(seeds),
        check_t8(seeds, baseline),
        check_t9(seeds),
        check_t10(seeds, baseline),
        check_t11(seeds),
        check_t12(seeds, baseline),
    ]
    checks += check_t13(seeds)
    return checks


# ------------------------------------------------------------------------------ driver


def build_report(
    seeds: Mapping[int, dict], legacy: Mapping[int, dict], *, validation: bool
) -> list[Check]:
    versions = {r["meta"].get("builder_version") for s in seeds.values() for r in s["players"]}
    checks = [
        Check(
            "input",
            "PASS" if versions == {REAL_BUILDER_VERSION} else "FAIL",
            f"seasons {sorted(seeds)}; builder versions {sorted(v for v in versions if v)}",
        )
    ]
    if checks[0].status == "FAIL":
        return checks  # a legacy or mixed-version input cannot be judged by v2's checks
    checks += check_tuning(seeds, legacy)
    if validation:
        checks += check_validation(seeds, legacy)
    checks += check_drag(seeds)
    checks += check_clamp(seeds, legacy)
    checks += check_solver(seeds)
    checks += [check_extreme_pegging(seeds), check_evidence_share(seeds)]
    checks += check_baseline(seeds)
    return checks


def version_two_only(seeds: Mapping[int, dict]) -> dict[int, dict]:
    """The seasons whose every row is builder version 2: `seeds/real_players` also holds
    the legacy 1996-2008 files, which the Tier-1 calibration checks must not read."""
    return {
        s: seed
        for s, seed in seeds.items()
        if seed["players"]
        and all(
            r.get("meta", {}).get("builder_version") == REAL_BUILDER_VERSION
            for r in seed["players"]
        )
    }


def main(argv: Sequence[str]) -> int:
    args = list(argv[1:])
    validation = "--validation" in args
    calibration = "--calibration" in args
    args = [a for a in args if a not in ("--validation", "--calibration")]
    dirs = {"--legacy-dir": LEGACY_DIR, "--raw-dir": RAW_DIR, "--baseline-dir": None}
    for flag in dirs:
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(f"{flag} needs a directory", file=sys.stderr)
                return 1
            dirs[flag] = Path(args[i + 1])
            del args[i : i + 2]
    legacy_dir = dirs["--legacy-dir"]
    out_dir = Path(args[0]) if args else DEFAULT_OUT_DIR
    seeds = load_seasons(out_dir)
    if not seeds:
        print(f"no season files in {out_dir}", file=sys.stderr)
        return 1
    if calibration:
        # RPC-12 Tier 1 alone (docs/design/real-player-rating-calibration.md)
        seeds = version_two_only(seeds)
        baseline_dir = dirs["--baseline-dir"]
        baseline = (
            version_two_only(load_seasons(baseline_dir, only=sorted(seeds)))
            if baseline_dir
            else None
        )
        truth = load_raw_truth(dirs["--raw-dir"], sorted(seeds))
        checks = build_calibration_report(seeds, truth, baseline, validation=validation)
    else:
        legacy = load_seasons(legacy_dir, only=sorted(seeds)) if legacy_dir.exists() else {}
        checks = build_report(seeds, legacy, validation=validation)
    for check in checks:
        print(check.line())
    failed = [c for c in checks if c.status == "FAIL"]
    print(f"\n{len(checks)} lines, {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
