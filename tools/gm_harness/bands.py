"""`seeds/gm_validation_bands.json`'s schema and comparator evaluation
(architect round-7 report §7, round-8 report §9's three corrections).

**Deliberately dumb.** This module owns exactly two things: (1) the frozen
`MetricBand`/`BandSpec` shape a JSON file is parsed into, and (2) `evaluate`,
the one place a `comparator` string (`"range"`/`"min"`/`"max"`/
`"max_violations"`) is turned into a pass/fail boolean. It does not know
what a `RunRecord` is and does not compute a single metric value itself --
that is `metrics.py`'s job (report §6.2: "`compute_mN(runs, bands) ->
MetricValue`"). Splitting it this way means a wrong number in the bands
file is a data bug, reviewable as a JSON diff, and a wrong comparator
interpretation is a one-function bug, not tangled into 22 metric bodies.

Zero I/O beyond `load_bands`'s own single `Path.read_text` call -- no
`dba2.data`, nothing async, matching every other module under `tools/
gm_harness/`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dba2.managers.tuning import GM_TUNING_VERSION

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BANDS_PATH = _REPO_ROOT / "seeds" / "gm_validation_bands.json"

_VALID_COMPARATORS = frozenset({"range", "min", "max", "max_violations"})


@dataclass(frozen=True)
class MetricBand:
    """One row of `seeds/gm_validation_bands.json`'s `"metrics"` array --
    round 7 §7's schema, verbatim field-for-field."""

    id: str
    number: int
    family: int
    name: str
    statistic: str
    comparator: str
    band_low: float | None
    band_high: float | None
    unit: str
    aggregation: str
    vacuous: bool
    interpreted: bool
    """Round 7 §6.2 item 21 -- `True` only for M21 (the unfalsifiable-as-
    literally-written re-trade check, rendered `PASS (interpreted)`)."""
    counted_in_go: bool
    """`False` for M21 only (round 7 §8: vacuous metrics are also excluded
    from the GO count, but M21 is the only entry that is `vacuous=False`
    AND excluded -- see this module's own module docstring and `metrics.
    py`'s M21 docstring for why "unfalsifiable by construction" is not the
    same claim as "structurally never produces data," which is what
    `vacuous` means everywhere else in this file). Phase 3 §9 adds M23 and
    (until step 1d lands) M26/M27/M28 as further permanently/temporarily
    `False` entries -- see each metric's own `rationale`."""
    strength: str
    """Phase 3 Decision P3-37, added by §11 step 7 -- a one-line gate-
    strength label (e.g. `"rate band, pre-registered"`, `"existence"`,
    `"invariant"`) so a "N/N counted metrics pass" report line does not
    read as N equally strong constraints. Additive/backward-compatible:
    every M1-M22/A1-A3 row predates this field and defaults to `""`
    (round 7's own schema never had a strength concept -- inventing one
    for those 25 rows retroactively is a separate, unrequested exercise;
    `""` renders as blank in the report, never as a wrong label)."""
    known_gap: Mapping[str, object] | None
    """Round 8 §9 item 2 (M2 only) -- `{"boundaries": [...], "reason":
    "..."}`. A `known_gap` metric is still real and gating on its *clean*
    boundaries; the gap's own violations are reported, never hidden, but
    never counted against GO/NO-GO -- see `metrics.py::compute_m2`'s own
    docstring for the exact split."""
    params: Mapping[str, object]
    source: str
    rationale: str


@dataclass(frozen=True)
class BandSpec:
    gm_tuning_version: int
    exit_criterion: Mapping[str, object]
    metrics: Mapping[str, MetricBand]
    """Keyed by `id` (`"M1"`.."M22"`, `"A1"`.."A3"`) -- `metrics.py`'s own
    lookup basis, since every `compute_mN`/`compute_aN` needs its own
    band entry's `params` (e.g. M1's `roster_min`/`roster_max`, M14's
    `se_threshold`), not just the numeric bounds."""


def _parse_band(row: Mapping[str, object]) -> MetricBand:
    comparator = row["comparator"]
    if comparator not in _VALID_COMPARATORS:
        raise ValueError(
            f"{row['id']}: unknown comparator {comparator!r} "
            f"(must be one of {sorted(_VALID_COMPARATORS)})"
        )
    return MetricBand(
        id=row["id"],
        number=row["number"],
        family=row["family"],
        name=row["name"],
        statistic=row["statistic"],
        comparator=comparator,
        band_low=row.get("band_low"),
        band_high=row.get("band_high"),
        unit=row["unit"],
        aggregation=row.get("aggregation", ""),
        vacuous=row.get("vacuous", False),
        interpreted=row.get("interpreted", False),
        counted_in_go=row.get("counted_in_go", True),
        known_gap=row.get("known_gap"),
        strength=row.get("strength", ""),
        params=row.get("params", {}),
        source=row["source"],
        rationale=row["rationale"],
    )


def load_bands(path: Path = DEFAULT_BANDS_PATH) -> BandSpec:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metrics = {row["id"]: _parse_band(row) for row in payload["metrics"]}
    if len(metrics) != len(payload["metrics"]):
        raise ValueError("gm_validation_bands.json has a duplicate metric id")
    bands_version = payload["gm_tuning_version"]
    if bands_version != GM_TUNING_VERSION:
        # P2-35's M14 SE-precertification staleness lock only means
        # anything if this check exists: `compute_m14` (tools/gm_harness/
        # metrics.py) compares a precert citation's own `gm_tuning_version`
        # against `bands.gm_tuning_version`, but that field is just a
        # literal copied from this JSON file -- nothing enforced it stayed
        # in sync with `dba2.managers.tuning.GM_TUNING_VERSION`, the real
        # source of truth. Without this load-time cross-check, a future
        # constant sweep can bump `GM_TUNING_VERSION` in tuning.py, forget
        # to also bump this file's top-level `gm_tuning_version`, and every
        # real run keeps citing the now-stale precertification as PASS.
        raise ValueError(
            f"seeds/gm_validation_bands.json's gm_tuning_version "
            f"({bands_version}) does not match dba2.managers.tuning."
            f"GM_TUNING_VERSION ({GM_TUNING_VERSION}) -- the bands file is "
            "out of sync with the live tuning surface; bump the JSON "
            "file's gm_tuning_version (and re-derive any se_precertified "
            "citations, which go stale the moment this fires) before "
            "trusting any metric computed against it."
        )
    return BandSpec(
        gm_tuning_version=bands_version,
        exit_criterion=payload["exit_criterion"],
        metrics=metrics,
    )


def evaluate(band: MetricBand, value: float | None) -> bool | None:
    """`True`/`False` for a real pass/fail, `None` if `value is None`
    (the metric could not be computed this run -- e.g. M22 under
    `--skip-games`, M6 with no duplicate-seed determinism run present --
    a caller renders `None` as `SKIP`, never as a silent pass).

    Does **not** apply `band.vacuous`/`band.interpreted`/`band.known_gap`/
    `band.counted_in_go` -- those are report-rendering and GO-count
    concerns `validate_gm.py`'s own report writer owns (a metric can be
    both "evaluates real, comparator-based pass/fail" and "excluded from
    the GO count," M21 being the one entry where both are true
    simultaneously, so collapsing them into one function would force a
    caller to re-derive the split anyway)."""
    if value is None:
        return None
    if band.comparator == "range":
        return band.band_low <= value <= band.band_high
    if band.comparator == "min":
        return value >= band.band_low
    if band.comparator == "max":
        return value <= band.band_high
    if band.comparator == "max_violations":
        return value <= band.band_high
    raise ValueError(f"unreachable: {band.comparator!r} already validated at load time")
