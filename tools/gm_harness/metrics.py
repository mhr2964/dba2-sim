"""The 22-metric computation layer (architect round-7 report Sec.6.2, round-8
report Sec.9's three corrections) -- one `compute_mN(runs, bands)` function
per metric, plus `compute_a1`/`a2`/`a3` for the three archetype golden-table
checks, and `compute_all(runs, bands) -> tuple[MetricResult, ...]`.

**Hard constraint, repeated because it is this module's entire reason for
existing as its own file: every `compute_mN` (with the one disclosed
exception, M17, see below) takes `tuple[RunRecord, ...]` and a `BandSpec`
and NOTHING else** -- no `HarnessState`, no `LeagueSnapshot`, no I/O, no
`dba2.data` import. That is what makes all 22 metrics unit-testable
against a hand-built `RunRecord` fixture with no 10+ minute real run behind
it, the same pure/apply split `gm_service.py` (T3/T4) already established.
Verified mechanically at the bottom of `tests/unit/tools/gm_harness/
test_metrics.py` by AST-scanning this file's own import list, mirroring
`tests/unit/test_layering.py`'s own pattern for a similar boundary.

**One disclosed, deliberate exception: M17 (and the three archetype
checks, A1-A3).** Architect round 7 Sec.6.2 item 17 names metric 17 as
"the one that needs no season at all" with its own `--only-metric-17` fast
path -- it is not a statistic *over* a `RunRecord`, it runs its own 100
seeded monoculture drafts from scratch. `compute_m17`/`compute_a1`/
`compute_a2`/`compute_a3` therefore take `(bands: BandSpec)` alone, not
`runs` -- still zero I/O (only calls already-established pure `managers/`
functions, the quarantined `tools/_gm_draft_class_stub.py`, and `tools/
gm_harness/league.py`'s own private cap-settings/rookie-scale loaders,
which read a static seed file once, not a live league). `compute_all`
special-cases these four ids; every other metric's signature is uniform.

**Judgment calls this module makes, every one of them a real design
decision the dispatch asked to be resolved and disclosed rather than
guessed at silently -- collected here so a critic reviewing 22 metric
bodies has one place to check each decision against, not 22 scattered
comments:**

1. **M1's `roster_min`/`roster_max` come from `bands.params`, not a
   hardcoded constant.** Round 7's own Sec.7 schema table left M1's params
   column blank. Since this module cannot read `seeds/cap_settings.json`
   itself (the purity constraint above), the two constants live in the
   bands file instead -- `seeds/gm_validation_bands.json`'s own `_note`
   documents this. They are the real, fixed values that file carries
   (14/15), never re-derived or approximated.
2. **M1/M2's exact measurement-point semantics are transcribed from `tests/
   unit/tools/gm_harness/test_season.py`'s own invariant-1/invariant-2
   tests, not from architect round 8 Sec.9 item 2's text directly.** That
   report section was written before T7a2 existed; T7a2's own build and a
   second critic round on it refined the semantics further (see that
   report's own docstring, and `HANDOFF.md`'s dispatch for this slice,
   both say so explicitly). The real, currently-passing test suite is the
   authoritative source: M1 checks `[roster_min, roster_max]` at
   `PRESEASON`/`post_trade_window_1`/`TRADE_DEADLINE` for every season
   (including 2+, where it is actually load-bearing), and `>= roster_min`
   only (no ceiling) at `post_waivers`. M2 is real and gating at
   `PRESEASON`/`post_trade_window_1`/`TRADE_DEADLINE`/`post_draft`/
   `free_agency_open`; `season_end`/`post_waivers` are a disclosed,
   separate, out-of-scope gap (`signing.py` never checks `apron_2`) --
   reported via `bands.metrics["M2"].known_gap`, never hidden, never
   gating GO/NO-GO.
3. **Cohort-assignment basis for metrics 11-13/16, stated once here rather
   than re-derived per metric.** Metrics 11/15 use each `Acquisition`'s
   own `window_at_acquisition`/acquiring-team-philosophy field directly
   (that is literally why `season.py`'s own module docstring says the
   field was added). Metrics 12/13's "team-season" denominator, and
   metric 16's cohort, use the team's `window_by_team` recorded at the
   `"season_end"` boundary of the season in question (12: every season in
   the run, pooled; 13: season 3 only, per the design doc's own explicit
   "at the end of season 3" text; 16: the final season only, per "at end
   of run"). This is a real interpretive choice -- `season.py` records
   `window_by_team` at every boundary, and `"season_end"` (post-trade-
   deadline reclassification, post-draft/FA roster) is the most stable,
   least-transient point to label a team's whole season by, matching the
   window classifier's own post-`TRADE_DEADLINE` re-classification being
   the last time a team's window changes before the season's own outcome
   is set.
4. **M14's "end of run" reads the FINAL season's `"post_waivers"`
   boundary** (the literal last snapshot `season.py` records each run),
   not `"season_end"` -- the roster composition can differ by whichever
   players `WAIVERS` cut, and "end of run" is read as "the state the run
   actually ends in," not "the state before its last bookkeeping step."
5. **M9/M8a/M8b/M7's exact measurement boundary is a real choice, stated
   per metric below** -- the design doc's own Sec.10 text does not name a
   boundary for M7-M10 the way it does for M12/M13/M16 ("at the end of
   season 3," "at end of run"). M7/M8a/M8b are measured at `"season_end"`
   (the roster/cap state a season concludes with, matching M7's own "share
   of teams *ending* a season" text). M9 is measured at `"post_waivers"`
   (the point `dead_cap_by_team_season` actually reflects this season's
   real cuts). M10 is measured at `"free_agency_open"`, per round 7 Sec.7's
   own explicit `measured_at` param for that one metric.
6. **M20 ("no team accounts for > 25% of a season's trades") counts trade
   *participations*, not trade *count*** -- a team is charged one
   participation per trade it is either side of, and `share = participations
   / total_trades_that_season`; the max across every `(seed, season, team)`
   observed is the reported value.
7. **M17's reference `NeedMatrix` is a small, deliberately position-
   imbalanced 8-man roster, not an empty one -- caught and fixed by this
   slice's own real end-to-end run, not by a hand review.** See `_m17_need_
   matrix`'s own docstring for the full derivation: an empty roster makes
   `need_bonus` collapse to a per-prospect CONSTANT (`Sum_pos coverage(p,
   pos)` is always 1.0), which makes `prospect_fit_value_for`'s ranking
   identical to bare `prospect_value_for`'s for every philosophy --
   structurally forcing M17 to 0.0 regardless of `fit_sens`, confirmed on
   the real smoke run (`rate_star_maxer == rate_egalitarian == 1.0`
   exactly, at 3,000 picks each). Fixed with real positional variation;
   re-verified on the same real run afterward (`0.284`, correctly `PASS`).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _dataclasses_replace
from enum import StrEnum
from itertools import zip_longest
from pathlib import Path

from dba2.core.rng import rng_for
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.managers.decisions.draft import DraftSlot, prospect_value_for, run_draft
from dba2.managers.needs import build_need_matrix
from dba2.managers.snapshot import build_team_snapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning
from dba2.services.gm_service import GM_BEHAVIOR_SCOPE_VERSION
from tests.fixtures.managers import (
    generate_archetype_table,
    generate_pick_archetype_table,
    generate_prospect_archetype_table,
    make_roster_player,
)
from tools._gm_draft_class_stub import generate_draft_class
from tools.gm_harness.bands import BandSpec, MetricBand, evaluate
from tools.gm_harness.league import _load_real_cap_settings, _load_rookie_scale
from tools.gm_harness.media_corpus import MediaCorpusTotals, MediaCycleRecord
from tools.gm_harness.season import RunRecord, SeasonRecord

# ---------------------------------------------------------------------------
# Import-boundary note (see this module's own docstring, "Reuse, don't
# reinvent" instruction): the three `generate_*_archetype_table` functions
# (A1/A2/A3) and their `CAP_SETTINGS`/`ROOKIE_SCALE` fixtures already exist,
# checked in and exercised by three existing golden-file tests
# (test_valuation.py/test_picks.py/test_draft.py), at `tests/fixtures/
# managers.py`. Importing them directly here -- rather than writing a
# fourth transcription of the same row sets -- is a deliberate, disclosed
# choice: this project has repeatedly hit "two implementations of the same
# quantity disagree" as a real, named defect class (docs/ledger/phase-2.md,
# multiple rounds), and a second copy of ARCHETYPES/PICK_ARCHETYPES/
# PROSPECT_ARCHETYPES here would be exactly that risk, for zero benefit --
# these tables exist ONLY to be diffed against the checked-in golden files,
# so there is nothing about "production code depending on tests/" that
# applies in the usual sense (this file never asserts anything about
# `tests/`'s own behavior, it calls three already-pure, already-reviewed
# generator functions). `tests/` carries no `__init__.py` (a PEP 420
# namespace package, confirmed importable the same way pytest itself
# already imports it) and nothing under `dba2/`'s own layering test
# forbids `tools/` from importing `tests/` -- only the reverse direction
# (`dba2/` importing `tools.gm_harness`) is denylisted. The alternative --
# moving the three generator functions and their archetype fixtures to a
# new shared, non-test location -- was considered and rejected here as
# out of this slice's own scope: it would touch three existing test files'
# import lines for a purely organizational win, and this project's own
# standing rule is "don't refactor unrelated code."
# ---------------------------------------------------------------------------

_M17_SEASON = 2026
_M17_LEAGUE_ID = 1
_M17_WINDOW = CompetitiveWindow.RETOOL
"""A neutral, fixed window for M17's own synthetic monoculture drafts --
held identical across both cohorts (star_maxer/egalitarian) so it cannot
itself be a confound; the choice of RETOOL over any other single value is
arbitrary and does not affect which philosophy's picks track its own board
more closely (that is `fit_sens`'s effect alone, per `prospect_fit_value_
for`'s own formula, which the window parameter does not touch)."""


# ---------------------------------------------------------------------------
# Return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricValue:
    """One metric's raw computed value, before band evaluation. `bands.py`
    turns `value` into a pass/fail; this module only ever produces the
    number (or a documented reason it could not be produced)."""

    value: float | None
    detail: Mapping[str, object] = field(default_factory=dict)
    per_season: tuple[float, ...] | None = None
    skip_reason: str | None = None
    """Set (with `value=None`) when the metric genuinely could not be
    computed from the data on hand -- e.g. M22 under `--skip-games`, M6
    with no duplicate-seed run present, M13 with fewer than 3 seasons.
    Renders `SKIP` in the report and is excluded from the GO count
    regardless of `MetricBand.counted_in_go`."""
    undefined: bool = False
    """Set (with `value=None`) when the metric's own statistic is
    genuinely undefined given the data -- e.g. M11's denominator cohort
    has zero acquisitions at the overall threshold. Renders `FAIL
    (undefined)`, never `SKIP` and never a silent `PASS` -- report Sec.6.2
    item 11's own explicit instruction ("never PASS (inf)")."""
    extra_pass: bool = True
    """ANDed into the comparator's own pass/fail. Used by M14 (the `se_
    measured <= se_threshold` measurability gate, which must hold in
    addition to the point estimate) and M18 (the any-single-season-zero
    override, which must fail regardless of the run-wide mean)."""
    counted_in_go_override: bool | None = None
    """Decision P4-105 (M34 only, so far) -- overrides `MetricBand.
    counted_in_go` for THIS reading when set, instead of the band's own
    static JSON default. M34's `counted_in_go` is governed by three
    conjunctive gates measured against the CORPUS actually supplied (null
    gate, template-diversity gate, kind-coverage gate) rather than by a
    fixed per-metric constant the way every other metric's `counted_in_go`
    is -- a `SKIP` would destroy the real "these two voices are
    indistinguishable because the corpus cannot exercise them" reading
    (P4-105's own explicit rule), so the gates flip `counted_in_go`
    per-reading instead. `None` (every metric but M34) means "use `band.
    counted_in_go` unchanged," matching every existing metric's behavior
    exactly -- this is additive, never a behavior change for M1-M32."""


@dataclass(frozen=True)
class MetricResult:
    """One row of the final report -- `band` plus the computed value and
    its resolved status string."""

    id: str
    number: int
    family: int
    name: str
    comparator: str
    unit: str
    band_low: float | None
    band_high: float | None
    value: float | None
    detail: Mapping[str, object]
    per_season: tuple[float, ...] | None
    status: str
    """`"PASS"` / `"FAIL"` / `"SKIP"` / `"FAIL (undefined)"` / `"PASS
    (interpreted)"` / `"FAIL (interpreted)"`."""
    passed: bool | None
    counted_in_go: bool
    rationale: str
    strength: str = ""
    """Decision P3-37 -- `band.strength`, carried straight through so the
    report can render each metric's own gate-strength label (blank for
    every M1-M22/A1-A3 row, which predates the field)."""


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _boundary(season: SeasonRecord, label: str):
    for b in season.boundaries:
        if b.label == label:
            return b
    raise KeyError(f"season {season.season} has no boundary labeled {label!r}")


def _salary_cap(row) -> int:
    """`row.room = salary_cap - row.used` at construction time
    (`capsheet.py::compute_team_season_cap_sheet`) -- `salary_cap` is not
    itself a `SeasonCapProjection` field, so it is derived here rather
    than plumbed as a 23rd `RunRecord`/`BoundaryRecord` field for a value
    that is always recoverable from two fields already present."""
    return row.used + row.room


def _mean(values: Sequence[float]) -> float | None:
    values = list(values)
    return statistics.mean(values) if values else None


# ---------------------------------------------------------------------------
# Family 1 -- invariants 1-6 (report Sec.6.2 items 1-6)
# ---------------------------------------------------------------------------


def compute_m1(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M1"]
    roster_min = band.params["roster_min"]
    roster_max = band.params["roster_max"]
    ceiling_labels = set(band.params["ceiling_and_floor_boundaries"])
    floor_labels = set(band.params["floor_only_boundaries"])

    violations = 0
    by_boundary: dict[str, int] = {}
    for run in runs:
        for season in run.seasons:
            for b in season.boundaries:
                if b.label in ceiling_labels:
                    for _team_id, ids in b.roster_ids_by_team.items():
                        if not (roster_min <= len(ids) <= roster_max):
                            violations += 1
                            by_boundary[b.label] = by_boundary.get(b.label, 0) + 1
                elif b.label in floor_labels:
                    for _team_id, ids in b.roster_ids_by_team.items():
                        if len(ids) < roster_min:
                            violations += 1
                            by_boundary[b.label] = by_boundary.get(b.label, 0) + 1
    return MetricValue(value=float(violations), detail={"violations_by_boundary": by_boundary})


def compute_m2(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """**`value`/the gate read `capsheet_by_team` only, exactly as before
    architect round 19** -- the hold-inclusive counterfactual below is a
    reported-never-gated diagnostic (round 19 §2.5 item 3/§7 item 4),
    added *beside* the real gate computation, never substituted into it.
    `hold_inclusive_violations`/`hold_inclusive_violations_by_boundary`
    are computed off `capsheet_with_holds_by_team`, at the same 5 clean
    boundaries this metric already gates on (not the known-gap boundaries
    -- this diagnostic answers "would M2's own *real* invariant have
    survived a hold-inclusive basis," the same question round 19 §2.2's
    arithmetic bounds, not the disclosed signing.py gap's separate
    question). **Both report `None`, not `0`, if `capsheet_with_holds_by_
    team` is absent on any scanned boundary** (a pre-round-19-shaped
    pickle) -- a missing record silently counted as 0 violations would
    understate a real hazard, exactly the "the comment describes the bug
    as the design" class this project has caught before (round 19 §5's
    own "pickle-compatibility trap")."""
    band = bands.metrics["M2"]
    clean_labels = set(band.params["clean_boundaries"])
    known_gap_labels = set(band.known_gap["boundaries"]) if band.known_gap else set()

    clean_violations = 0
    known_gap_violations = 0
    by_boundary: dict[str, int] = {}
    hold_inclusive_violations = 0
    hold_inclusive_by_boundary: dict[str, int] = {}
    hold_inclusive_available = True
    for run in runs:
        for season in run.seasons:
            for b in season.boundaries:
                if b.label in clean_labels:
                    for _team_id, row in b.capsheet_by_team.items():
                        if row.apron2_room < 0:
                            clean_violations += 1
                            by_boundary[b.label] = by_boundary.get(b.label, 0) + 1
                    if b.capsheet_with_holds_by_team is None:
                        hold_inclusive_available = False
                    else:
                        for _team_id, row in b.capsheet_with_holds_by_team.items():
                            if row.apron2_room < 0:
                                hold_inclusive_violations += 1
                                hold_inclusive_by_boundary[b.label] = (
                                    hold_inclusive_by_boundary.get(b.label, 0) + 1
                                )
                elif b.label in known_gap_labels:
                    for _team_id, row in b.capsheet_by_team.items():
                        if row.apron2_room < 0:
                            known_gap_violations += 1
                            by_boundary[b.label] = by_boundary.get(b.label, 0) + 1
    return MetricValue(
        value=float(clean_violations),
        detail={
            "violations_by_boundary": by_boundary,
            "known_gap_violations": known_gap_violations,
            "known_gap_reason": band.known_gap["reason"] if band.known_gap else None,
            "hold_inclusive_violations": (
                hold_inclusive_violations if hold_inclusive_available else None
            ),
            "hold_inclusive_violations_by_boundary": (
                hold_inclusive_by_boundary if hold_inclusive_available else None
            ),
        },
    )


def compute_m3(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    count = sum(len(season.conservation_violations) for run in runs for season in run.seasons)
    return MetricValue(value=float(count), detail={})


def compute_m4(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    violations = 0
    for run in runs:
        for season in run.seasons:
            for b in season.boundaries:
                violations += sum(1 for c in b.contract_count_by_player.values() if c > 1)
    return MetricValue(value=float(violations), detail={})


def compute_m5(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    count = sum(len(season.reconstruction_violations) for run in runs for season in run.seasons)
    return MetricValue(value=float(count), detail={})


def compute_m6(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Groups `runs` by `rng_seed`; any group with >= 2 entries is a
    determinism re-run pair (`validate_gm.py`'s own `--determinism-check`
    contract: run seed 0 twice, hand both `RunRecord`s in). No such group
    present -> `SKIP` (determinism-check was off, or only one run total),
    never a silent pass."""
    by_seed: dict[int, list[RunRecord]] = {}
    for run in runs:
        by_seed.setdefault(run.rng_seed, []).append(run)

    dup_groups = [group for group in by_seed.values() if len(group) >= 2]
    if not dup_groups:
        return MetricValue(
            value=None, detail={}, skip_reason="no duplicate-seed runs present in `runs`"
        )

    mismatches = 0
    checked_pairs = 0
    for group in dup_groups:
        a, b = group[0], group[1]
        checked_pairs += 1
        if len(a.seasons) != len(b.seasons):
            mismatches += 1
            continue
        for sa, sb in zip(a.seasons, b.seasons, strict=True):
            if sa.jsonl_lines != sb.jsonl_lines:
                pairs = zip(sa.jsonl_lines, sb.jsonl_lines, strict=False)
                mismatches += sum(1 for x, y in pairs if x != y)
                mismatches += abs(len(sa.jsonl_lines) - len(sb.jsonl_lines))
    return MetricValue(
        value=float(mismatches), detail={"duplicate_seed_pairs_checked": checked_pairs}
    )


# ---------------------------------------------------------------------------
# Family 2 -- economic plausibility, items 7-10
# ---------------------------------------------------------------------------


def compute_m7(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M7"]
    top_n = band.params["top_n"]
    overall_threshold = band.params["overall_threshold"]
    measured_at = band.params["measured_at"]

    flagged = 0
    total = 0
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, measured_at)
            for team_id, row in b.capsheet_by_team.items():
                total += 1
                overalls = b.overalls_by_team.get(team_id, ())
                top8_mean = _mean(overalls[:top_n])
                if row.room < 0 and top8_mean is not None and top8_mean < overall_threshold:
                    flagged += 1
    if total == 0:
        return MetricValue(value=None, detail={}, skip_reason="no team-seasons in `runs`")
    return MetricValue(
        value=flagged / total, detail={"flagged_team_seasons": flagged, "total_team_seasons": total}
    )


def compute_m8a(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M8a"]
    measured_at = band.params["measured_at"]
    ratios: list[float] = []
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, measured_at)
            for _team_id, row in b.capsheet_by_team.items():
                cap = _salary_cap(row)
                if cap > 0:
                    ratios.append(row.committed_salary / cap)
    if not ratios:
        return MetricValue(value=None, detail={}, skip_reason="no team-seasons in `runs`")
    return MetricValue(value=statistics.mean(ratios), detail={"n": len(ratios)})


def compute_m8b(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Decision P2-35 (architect rounds 16 Sec.3 F4 / 17 Sec.2): the
    cross-cell aggregator is `mean_across_runs_seasons`, matching
    `compute_m10`'s own aggregator string -- NOT `max_over_seed_season_
    pairs`, which is monotone non-decreasing in the number of cells and
    therefore made the test strictly harder the more seeds were run (round
    16's own finding). The per-cell loop below is untouched from before
    this fix; only the aggregation and the returned `MetricValue` changed.

    `detail` keeps the old `max`-based figure (`max_over_cells`) so every
    historical M8b reading (10.0, 9.0, 7.0) stays comparable, plus a full
    histogram and the count/share of cells at/above `band.band_high` --
    reported diagnostics, never gated (architect round 17 Sec.2.5: a
    tail-side band on `cells_above_band_high_share` is a future, undecided
    user call, not this fix's job). `extra_pass` deliberately stays at its
    default `True` -- no measurability conjunct is added here (RS3,
    explicitly un-ruled by P2-35)."""
    band = bands.metrics["M8b"]
    measured_at = band.params["measured_at"]
    share_threshold = band.params["share_threshold"]

    per_season_counts: list[dict] = []
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, measured_at)
            below = 0
            for _team_id, row in b.capsheet_by_team.items():
                cap = _salary_cap(row)
                if cap > 0 and row.committed_salary / cap < share_threshold:
                    below += 1
            per_season_counts.append(
                {"rng_seed": run.rng_seed, "season": season.season, "below_threshold": below}
            )
    if not per_season_counts:
        return MetricValue(value=None, detail={}, skip_reason="no team-seasons in `runs`")

    counts = [row["below_threshold"] for row in per_season_counts]
    n_cells = len(counts)
    mean_value = statistics.mean(counts)
    max_over_cells = max(counts)

    histogram: dict[int, int] = {}
    for c in counts:
        histogram[c] = histogram.get(c, 0) + 1
    histogram = dict(sorted(histogram.items()))

    cells_above_band_high = sum(1 for c in counts if c > band.band_high)

    se_cell = statistics.stdev(counts) / math.sqrt(n_cells) if n_cells >= 2 else None

    # `se_seed_cluster`: the honest SE -- seasons within one seed are five
    # years of the same league, not independent draws -- computed across
    # one mean per `RunRecord`, not per cell.
    counts_by_seed: dict[int, list[int]] = {}
    for row in per_season_counts:
        counts_by_seed.setdefault(row["rng_seed"], []).append(row["below_threshold"])
    seed_means = [statistics.mean(vals) for vals in counts_by_seed.values()]
    n_runs = len(seed_means)
    se_seed_cluster = statistics.stdev(seed_means) / math.sqrt(n_runs) if n_runs >= 2 else None

    detail = {
        "per_season": per_season_counts,
        "mean": mean_value,
        "max_over_cells": max_over_cells,
        "n_cells": n_cells,
        "histogram": histogram,
        "cells_above_band_high": cells_above_band_high,
        "cells_above_band_high_share": cells_above_band_high / n_cells,
        "se_cell": se_cell,
        "se_seed_cluster": se_seed_cluster,
    }
    return MetricValue(
        value=mean_value, detail=detail, per_season=tuple(float(c) for c in counts)
    )


def compute_m9(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M9"]
    measured_at = band.params["measured_at"]
    total_dead_cap = 0
    total_cap = 0
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, measured_at)
            for _team_id, row in b.capsheet_by_team.items():
                total_dead_cap += row.dead_cap
                total_cap += _salary_cap(row)
    if total_cap == 0:
        return MetricValue(value=None, detail={}, skip_reason="no team-seasons in `runs`")
    return MetricValue(
        value=total_dead_cap / total_cap,
        detail={"total_dead_cap": total_dead_cap, "total_cap_dollars": total_cap},
    )


def _m10_is_room_buyer(
    row, cap: int, *, mle_amount_base: float, salary_cap_base: float, roster_max: int
) -> bool:
    """Decision P2-46 (architect round 28 §4/§6, R28-M3's V2): a team is a
    "room buyer" when its room exceeds what its OWN remaining open roster
    slots could absorb at that season's real MLE amount -- a per-team,
    slot-relative rule, replacing the flat `room > share_threshold * cap`
    threshold every team was held to regardless of how many slots it still
    needed to fill. `n_open` is the number of roster slots still needed to
    fill next season -- `row.roster_count` is `SeasonCapProjection`'s own
    next-season-coverage field, already a field on the same dataclass this
    function already reads for `room`/`committed_salary`/etc. (not itself
    already read by another metric in this module -- `compute_m10` is the
    only reader) -- clamped at 0 so an already-over-full team
    (`roster_count > roster_max`) cannot flip the threshold negative and
    have its negative/low room read as buying.

    **A disclosed, accepted consequence of the per-team form, named here
    rather than left implicit: at `n_open == 0` (a team already at or past
    `roster_max` for next season), ANY positive room clears a $0
    threshold.** On the archived pre-P2-46 data this degenerate case
    affects 38.4% of gated buyer-counts. This is not a bug -- a team with
    zero open slots and any positive room genuinely has "more room than it
    needs," which is exactly what this rule is designed to flag -- but it
    is a real property of the definition and belongs on the record.

    `mle_amount_base`/`salary_cap_base` are the real, unscaled seed anchors
    (bands `params`, not `seeds/cap_settings.json` -- `compute_m10` is
    contractually pure and cannot import it). Every dollar-denominated
    `cap_settings` field grows by the identical `cap_growth` factor from
    the same anchor season (`capsheet.py::project_cap_settings`), so
    `mle_this_season = mle_amount_base * (cap / salary_cap_base)` recovers
    that season's real, cap-growth-scaled MLE without a second recorded
    field -- `cap` is this row's own `_salary_cap(row)`, already computed
    by every caller before this function is invoked."""
    n_open = max(0, roster_max - row.roster_count)
    mle_this_season = mle_amount_base * (cap / salary_cap_base)
    threshold_dollars = n_open * mle_this_season
    return row.room > threshold_dollars


def _m10_is_room_buyer_flat_threshold_v0(row, cap: int) -> bool:
    """The pre-Decision-P2-46 "room buyer" rule (`room > 0.15 * cap`,
    flat, per-team-slot-count-blind) -- kept alive, not merely described,
    so `compute_m10` can report a joinable twin of every P2-46-affected
    field under the identical pattern Decision P2-35 used for M8b's
    `detail["max_over_cells"]` and Decision P2-40 used for `detail
    ["pooled_mean_across_runs_seasons"]`/`detail["hold_inclusive_mean"]`:
    the OLD statistic computed in parallel and exposed under its own,
    unambiguously-named `detail` key, never feeding `value`. `0.15` is a
    literal here rather than a read off `bands.metrics["M10"].params
    ["room_share_threshold"]` -- that param is deliberately removed from
    `seeds/gm_validation_bands.json` (nothing else reads it, and a build
    round removed it precisely so a future reader would not mistake it
    for still load-bearing) -- so the one remaining caller that needs the
    pre-fix constant for historical comparability carries its own literal
    rather than resurrecting dead bands config."""
    return row.room > 0.15 * cap


def compute_m10(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Decision P2-40 (architect round 21 §3): the gated statistic is the
    mean, across seeds, of the buyer count in the run's steady-state season
    only -- a fixed season index (`params.resolves_at_season`), NOT a
    pooled mean across seasons. Every season's cell is still computed here
    and reported in `detail["per_season"]`/`per_season` -- only the
    aggregation that becomes `value` narrows to the resolved season's
    cells; nothing about the per-cell counting rule changes (the same
    "loop untouched, only the aggregation moves" shape Decision P2-35 used
    for M8b).

    A per-cell/max form was rejected: it is a maximum over cells, which
    architect round 16 §3 F4 proved is monotone non-decreasing in the seed
    count -- the exact defect Decision P2-35 corrected for M8b, one metric
    over. "The final season" was rejected in favor of a fixed index: a
    fixed index is invariant in run length (`run.seasons[resolves_at - 1]`
    is byte-identical whether the run requests 5 seasons or 8), while "the
    final season" is not (a longer run reads a still-rising later cell).
    The resolved index is asserted equal to `exit_criterion.min_seasons` so
    it cannot silently drift from Decision P2-22's own floor.

    **Gate is hold-inclusive as of Decision P2-41 (architect round 22
    §4.1, superseding round 19 §7 item 5's original "gate stays hold-free"
    ruling): `value`/`gated_cells`/`n_gated_cells`/`se_across_seeds` read
    `capsheet_with_holds_by_team`, not `capsheet_by_team`.** Decision
    P2-39 made `run_free_agency`'s own affordability basis hold-inclusive,
    so a hold-free gate measured a quantity the model no longer decided
    on -- `params.holds_basis` records this as data, not an implicit code
    fact. Every OTHER historical/reported field keeps its exact old
    hold-free-era definition, so every historical series stays joinable:
    `detail["per_season"]` (unconditional, hold-free, per-cell), `detail
    ["pooled_mean_across_runs_seasons"]` (the old hold-free pooled
    statistic), and `detail["hold_inclusive_mean"]` (the old pooled
    hold-inclusive statistic) are all unchanged. `detail
    ["hold_inclusive_steady_state_mean"]` also keeps its old definition
    (season-5 hold-inclusive mean) -- it is now numerically identical to
    the new gated `value`, which is expected and intentional, not a bug to
    engineer around. `detail["holds_basis"]` is `"computed"` when every
    scanned boundary carried the new field, `"unavailable"` the moment any
    one of them is a pre-Slice-19-A-shaped record
    (`capsheet_with_holds_by_team is None`) -- at that point every
    hold-inclusive detail field (old and new) is `None`, AND the gate
    itself hard-SKIPs (`value=None`, `skip_reason` set): the basis it
    gates on can no longer be computed, so it must never silently fall
    back to the hold-free basis and report a number under the same metric
    name that means something different (the same "never silently produce
    a number as if it were real" trap round 19 §5 and round 21 §3.3 point
    8 already caught).

    **The per-cell "room buyer" counting rule itself changed under Decision
    P2-46 (architect round 28 §4/§6, R28-M3's V2) -- to the slot-relative
    form in `_m10_is_room_buyer`, everywhere a room buyer is counted, not
    only in `gated_cells`.** This is a different axis from Decision P2-41
    above: P2-41 changed WHICH BASIS the gate reads (hold-free vs
    hold-inclusive) while every historical field kept its own basis and its
    own (then-unchanged) counting rule; P2-46 changes the counting rule
    itself, which is not basis-specific -- `_m10_is_room_buyer` runs
    identically whether it is handed a `capsheet_by_team` row or a
    `capsheet_with_holds_by_team` row. Applying the new rule to only
    `gated_cells` and leaving `detail["per_season"]`/`detail[
    "pooled_mean_across_runs_seasons"]`/`detail["hold_inclusive_mean"]` on
    the old flat-percentage rule would silently define "room buyer" two
    different ways under the same field names in the same `MetricValue` --
    so **every per-cell count in this function, hold-free and
    hold-inclusive alike, uses `_m10_is_room_buyer`.** Each basis's own
    field still reads its own `capsheet_*_by_team` dict, unchanged by this
    decision; only the threshold each reads against moved. Historical
    series predating Decision P2-46 are not joinable against this metric's
    counts without accounting for the rule change (the same caveat every
    prior instrument correction in this class -- P2-35, P2-40 -- already
    attaches to its own metric's pre-fix history).

    **Decision P2-46's own text requires this item's historical series stay
    joinable "exactly as [P2-35 and P2-40] required" -- so the two named
    keys those decisions attached that promise to get an old-rule twin,
    computed in parallel by `_m10_is_room_buyer_flat_threshold_v0` and
    never feeding `value`, `gated_cells`, or any other gated quantity:**
    `detail["pooled_mean_across_runs_seasons_flat_threshold_v0"]` (the
    hold-free pooled mean, same cells as `detail[
    "pooled_mean_across_runs_seasons"]`, old flat-threshold rule) and
    `detail["hold_inclusive_mean_flat_threshold_v0"]` (the hold-inclusive
    pooled mean, same cells as `detail["hold_inclusive_mean"]`, old
    flat-threshold rule). These are the identical two fields Decision
    P2-40's own implementation note names as carrying "M10's historical
    series (19.2 -> 16.8 ..., and 12.2667)" -- `detail[
    "hold_inclusive_steady_state_mean"]` is deliberately NOT given an
    old-rule twin here: its own docstring already documents that it is
    *expected* to move in lockstep with the new gated `value` (it always
    has, across every P2-40/P2-41-era reading), so it was never one of the
    two series P2-40's own joinability promise names.

    A run shorter than the gated index renders `SKIP`
    (`skip_reason=f"no run has >= {resolves_at} seasons"`, `compute_m13`'s
    own shape, reused deliberately) rather than falling back to the pooled
    mean -- `validate_gm.py`'s own exit-criterion gate already forces
    NO-GO on any under-5-season run, so this can never produce a silent
    green. This check runs BEFORE the holds-basis check: a too-short run
    reports the original "no run has >= N seasons" reason even if holds
    also happen to be unavailable."""
    band = bands.metrics["M10"]
    measured_at = band.params["measured_at"]
    mle_amount_base = band.params["mle_amount_base"]
    salary_cap_base = band.params["salary_cap_base"]
    roster_max = band.params["roster_max"]
    resolves_at = band.params["resolves_at_season"]
    assert resolves_at == bands.exit_criterion["min_seasons"], (
        f"M10's gated season (params.resolves_at_season={resolves_at}) must equal "
        f"exit_criterion.min_seasons ({bands.exit_criterion['min_seasons']}) -- "
        "Decision P2-40's operating point is pinned to Decision P2-22's own floor "
        "and must not silently drift"
    )

    per_season: list[int] = []
    per_season_flat_threshold_v0: list[int] = []
    detail_rows: list[dict] = []
    hold_inclusive_count_per_cell: list[dict] = []
    hold_inclusive_flat_threshold_v0_counts: list[int] = []
    holds_basis = "computed"
    gated_season_reached = False
    gated_cells: list[int] = []
    gated_season_labels: list[int] = []
    for run in runs:
        for season_index, season in enumerate(run.seasons, start=1):
            b = _boundary(season, measured_at)
            count = 0
            flat_count = 0
            for _team_id, row in b.capsheet_by_team.items():
                cap = _salary_cap(row)
                if cap > 0:
                    if _m10_is_room_buyer(
                        row, cap,
                        mle_amount_base=mle_amount_base,
                        salary_cap_base=salary_cap_base,
                        roster_max=roster_max,
                    ):
                        count += 1
                    # Fix B1 (critic round -- historical joinability):
                    # the OLD flat-threshold rule, computed in parallel and
                    # never gated on, so `pooled_mean_across_runs_seasons_
                    # flat_threshold_v0` stays comparable to every M10
                    # reading recorded before Decision P2-46.
                    if _m10_is_room_buyer_flat_threshold_v0(row, cap):
                        flat_count += 1
            per_season.append(count)
            per_season_flat_threshold_v0.append(flat_count)
            detail_rows.append({"rng_seed": run.rng_seed, "season": season.season, "count": count})
            if season_index == resolves_at:
                gated_season_reached = True

            if b.capsheet_with_holds_by_team is None:
                holds_basis = "unavailable"
            else:
                hold_count = 0
                hold_flat_count = 0
                for _team_id, row in b.capsheet_with_holds_by_team.items():
                    cap = _salary_cap(row)
                    if cap > 0:
                        if _m10_is_room_buyer(
                            row, cap,
                            mle_amount_base=mle_amount_base,
                            salary_cap_base=salary_cap_base,
                            roster_max=roster_max,
                        ):
                            hold_count += 1
                        if _m10_is_room_buyer_flat_threshold_v0(row, cap):
                            hold_flat_count += 1
                hold_inclusive_count_per_cell.append(
                    {"rng_seed": run.rng_seed, "season": season.season, "count": hold_count}
                )
                hold_inclusive_flat_threshold_v0_counts.append(hold_flat_count)
                # Decision P2-41: the GATED cells are the hold-inclusive
                # count, not the hold-free `count` computed above --
                # `gated_cells` now feeds `value`/`n_gated_cells`/
                # `se_across_seeds` directly from this basis.
                if season_index == resolves_at:
                    gated_cells.append(hold_count)
                    gated_season_labels.append(season.season)
    if not per_season:
        return MetricValue(value=None, detail={}, skip_reason="no team-seasons in `runs`")

    pooled_mean_across_runs_seasons = statistics.mean(per_season)
    pooled_mean_across_runs_seasons_flat_threshold_v0 = statistics.mean(
        per_season_flat_threshold_v0
    )

    if not gated_season_reached:
        return MetricValue(
            value=None,
            detail={"per_season": detail_rows, "holds_basis": holds_basis},
            skip_reason=f"no run has >= {resolves_at} seasons",
            per_season=tuple(float(x) for x in per_season),
        )

    if holds_basis == "unavailable":
        # The trap named explicitly by architect round 22 §4.1 (the same
        # shape round 19 §5 / round 21 §3.3 point 8 already caught once):
        # never silently fall back to the hold-free basis just because the
        # gate the run needs (hold-inclusive) cannot be computed for at
        # least one scanned boundary. Hard SKIP instead of a number under
        # the same metric name that would mean something different.
        return MetricValue(
            value=None,
            detail={
                "per_season": detail_rows,
                "holds_basis": holds_basis,
                "hold_inclusive_count_per_cell": None,
                "hold_inclusive_mean": None,
                "hold_inclusive_mean_flat_threshold_v0": None,
                "pooled_mean_across_runs_seasons": pooled_mean_across_runs_seasons,
                "pooled_mean_across_runs_seasons_flat_threshold_v0": (
                    pooled_mean_across_runs_seasons_flat_threshold_v0
                ),
                "gated_season_index": resolves_at,
                "gated_season_labels": None,
                "gated_cells": None,
                "n_gated_cells": None,
                "se_across_seeds": None,
                "hold_inclusive_steady_state_mean": None,
                "hold_inclusive_gated_cells": None,
            },
            skip_reason=(
                "holds_basis is \"unavailable\" -- at least one scanned boundary predates "
                "Slice 19-A and has no capsheet_with_holds_by_team, so the hold-inclusive "
                "gate (Decision P2-41) cannot be computed; never silently falling back to "
                "the hold-free basis"
            ),
            per_season=tuple(float(x) for x in per_season),
        )

    hold_inclusive_mean = statistics.mean(row["count"] for row in hold_inclusive_count_per_cell)
    hold_inclusive_mean_flat_threshold_v0 = statistics.mean(hold_inclusive_flat_threshold_v0_counts)
    hold_inclusive_gated_cells_out = list(gated_cells)
    hold_inclusive_steady_state_mean = statistics.mean(hold_inclusive_gated_cells_out)

    n_gated_cells = len(gated_cells)
    se_across_seeds = (
        statistics.stdev(gated_cells) / math.sqrt(n_gated_cells) if n_gated_cells >= 2 else None
    )

    return MetricValue(
        value=statistics.mean(gated_cells),
        detail={
            "per_season": detail_rows,
            "holds_basis": holds_basis,
            "hold_inclusive_count_per_cell": hold_inclusive_count_per_cell,
            "hold_inclusive_mean": hold_inclusive_mean,
            "hold_inclusive_mean_flat_threshold_v0": hold_inclusive_mean_flat_threshold_v0,
            "pooled_mean_across_runs_seasons": pooled_mean_across_runs_seasons,
            "pooled_mean_across_runs_seasons_flat_threshold_v0": (
                pooled_mean_across_runs_seasons_flat_threshold_v0
            ),
            "gated_season_index": resolves_at,
            "gated_season_labels": gated_season_labels,
            "gated_cells": gated_cells,
            "n_gated_cells": n_gated_cells,
            "se_across_seeds": se_across_seeds,
            "hold_inclusive_steady_state_mean": hold_inclusive_steady_state_mean,
            "hold_inclusive_gated_cells": hold_inclusive_gated_cells_out,
        },
        per_season=tuple(float(x) for x in per_season),
    )


# ---------------------------------------------------------------------------
# Family 3 -- behavioral differentiation, items 11-17
#
# RS3 (architect round 29, docs/ledger/reports/phase-2-architect-round-29.md
# Sec.6; Decision P2-47): a REPORTED-NOT-GATED `detail["precision"]` block
# for M11-M13/M15/M16, built by `_precision_detail` below. It is verdict-
# inert by construction -- it never touches `extra_pass`, `passed`, or
# `counted_in_go` -- and is populated only when a bands fixture opts in via
# `band.params["precision"]` (absent for every other metric, and for M14,
# whose own `se_threshold`/`se_precertified` mechanism answers a different
# question -- see round 29 Sec.2 -- and is left untouched).
# ---------------------------------------------------------------------------


def _jackknife_sd_over_seeds(
    raw_fn, runs: tuple[RunRecord, ...], band: MetricBand, *, transform=None
) -> float | None:
    """Delete-one jackknife SD, computed directly over `runs` at whatever
    `len(runs)` the caller passes in.

    **This value IS the standard error of the pooled statistic at this run's
    own seed count -- it must never be divided by `sqrt(len(runs))` again.**
    Orchestrator round 43 (docs/ledger/phase-2.md) found and fixed exactly
    that bug in its own first-draft census script: dividing an already-
    computed 45-run jackknife SD by `sqrt(45)` a second time understated
    every SE by roughly the same factor and inflated every margin-in-SE
    figure 6-19x. Confirmed correct two ways there: it matches `sd_per_seed
    / sqrt(45)` independently, and on M14 (as a known-answer positive
    control) it reproduces M14's own official `se_measured=0.6415` at 45
    seeds (0.639968 vs 0.6415).

    Returns `None` when `len(runs) < 3` (too few leave-one-out replicates
    to mean anything) or when any leave-one-out subset's own raw value is
    undefined (`raw_fn` returns `None`) -- never raises.
    """
    n = len(runs)
    if n < 3:
        return None
    replicates: list[float] = []
    for i in range(n):
        subset = runs[:i] + runs[i + 1 :]
        v = raw_fn(subset, band)
        if v is None:
            return None
        replicates.append(transform(v) if transform is not None else v)
    if any(r is None for r in replicates):
        return None
    theta_bar = sum(replicates) / n
    ss = sum((t - theta_bar) ** 2 for t in replicates)
    return math.sqrt((n - 1) / n * ss)


def _log_or_none(v: float | None) -> float | None:
    return math.log(v) if v is not None and v > 0 else None


def _precision_detail(
    raw_fn,
    runs: tuple[RunRecord, ...],
    band: MetricBand,
    value: float | None,
    *,
    gm_tuning_version: int,
    n_override: int | None = None,
    sd_live_precomputed: float | None = None,
) -> dict[str, object] | None:
    """The shared "reported, never gated" precision block for M11-M13/M15/
    M16 (architect round 29 Sec.6.2). `raw_fn` is that metric's own
    `_mN_raw_value(runs, band) -> float | None` -- the sole piece of
    metric-specific behaviour this helper needs, everything else is generic
    across the five.

    1. Returns `None` (no `detail["precision"]` key at all) when `band.
       params.get("precision")` is absent -- a bands fixture that never
       asks for this is byte-identical to pre-this-build behaviour.
    2/3/4. `sd_source` is `"precertified"` when the checked-in block's own
       `gm_tuning_version` matches the live `bands.gm_tuning_version`,
       else `"this_run"` (the live jackknife, always computed and always
       reported as `sd_across_seeds_this_run` regardless of which source
       actually feeds `se_at_this_n`).
       - **`this_run`**: `se_at_this_n` IS the live jackknife over `runs`
         at this run's own `len(runs)` -- already the SE at that n, no
         further division (see `_jackknife_sd_over_seeds`'s own docstring).
       - **`precertified`**: the checked-in `sd_across_seeds` is **not a
         per-unit/per-seed SD** -- it IS the standard error already
         measured at the precert's own `sd_seeds` (45) seeds. It is
         *projected* to this run's `n_runs` via
         `se_at_this_n = sd_across_seeds * sqrt(sd_seeds / n_runs)` (scaling
         UP as `n_runs` shrinks below `sd_seeds`, the mirror image of
         `compute_m14`'s own `se_extrapolated_at_precert_seeds`, which
         scales a small-n live SE UP to compare against a larger precert
         count). Guarded against a `sd_seeds`-less precert block (nit 2):
         when `sd_seeds` is missing, `se_at_this_n` (and everything derived
         from it, including `measurability_pass`) degrades to `None` and
         `precision_available` reports `False`, rather than raising
         `TypeError` on `None / n_runs`.
    4b. **`margin_in_se_at_sd_seeds`** (Decision P2-47, orchestrator fix
       round, resolving critic finding B1's real half): only populated
       when `sd_source == "precertified"`. `margin_in_se` (above) answers
       "is a run at THIS seed count a reliable read of this metric" --
       a small-n question. `margin_in_se_at_sd_seeds` answers a different
       one: "how far is THIS RUN'S OWN point estimate from the band,
       judged against the metric's best-established large-sample
       dispersion" -- `se_at_sd_seeds = sd_across_seeds` directly (no
       extrapolation at all, since it is already the SE at the precert's
       own `sd_seeds`), and `margin_in_se_at_sd_seeds = margin_over_band /
       se_at_sd_seeds` using THIS run's own `value` (not a stored 45-seed
       value -- this block has no such figure on hand at report time, and
       does not claim to). It is `None` whenever `sd_source == "this_run"`
       (there is no separate large-n figure to report against a live-only
       SD) or when the precert block omits `sd_across_seeds`. This field
       cannot answer "what does the metric's true 45-seed VALUE look
       like" -- that is a different, already-answered question recorded in
       Decision P2-47 (`docs/design/phase-2-manager-minds.md`); this block
       only has this run's own point estimate to report.
    5. M11 only (`precision["scale"] == "log"`): a parallel log-scale
       computation (`sd_across_seeds_log`/`se_at_this_n_log`/
       `margin_in_se_log`) feeds `measurability_pass`, since `se_threshold_
       derived` is itself stored in log units for a right-skewed ratio
       estimator whose null is 1.0, not 0 (round 29 Sec.3.2). The always-
       reported `margin_in_se` (what `_finalize`'s status-string annotation
       reads) stays on the metric's own natural, linear scale for every
       metric including M11 -- matching both round 29 Sec.0's own
       estimates and orchestrator round 43's real 45-seed reading (both
       linear) and Sec.6.3's pre-registered preview text.
    6. Emits `sd_source`, `sd_across_seeds`, `sd_across_seeds_this_run`,
       `n_runs`, `se_at_this_n`, `margin_over_band`, `margin_in_se`,
       `se_at_sd_seeds`, `margin_in_se_at_sd_seeds`, `se_threshold_derived`,
       `measurability_pass`, `required_seeds_if_over_threshold`,
       `precision_stale`, `sd_precertified`, `precision_available` -- plus,
       for M11 only, `sd_across_seeds_log`, `sd_across_seeds_log_this_run`,
       `se_at_this_n_log`, `margin_over_band_log`, `margin_in_se_log`.
    7. Never touches `extra_pass` -- the whole point (Sec.6.5 test 4).

    **Decision P3-47 (Sec.11 step 9's Family-3 measurability fix) added
    two optional kwargs so this one function can build both this run's own
    live block and, when a `gated_value_precertified` citation is valid, a
    second block for the citation's own `(value, n)` pair without
    recomputing the live jackknife SD twice.** `n_override` replaces
    `len(runs)` as `se_at_this_n`'s projection basis -- the gated call
    passes the citation's own `n_seeds`, at which (`n_seeds == sd_seeds` by
    construction) the projection factor `sqrt(sd_seeds / n_runs)` is
    exactly 1 and the block collapses to the certified SE with no
    extrapolation at all (expected, not a bug -- it is already the SE at
    that seed count). `sd_live_precomputed` lets a caller building both the
    live and gated blocks for the same run pass in the live jackknife SD it
    already computed once, rather than triggering a second `_jackknife_sd_
    over_seeds` pass here. The existing `value` parameter already doubles
    as "a value override" -- callers building the gated block simply pass
    the citation's own value there; no third kwarg was needed for it.
    """
    precision = band.params.get("precision")
    if precision is None:
        return None

    assert band.comparator == "min", (
        f"{band.id}: _precision_detail assumes a min comparator/null band_high "
        f"(true of every Family-3 entry it is wired to) -- got {band.comparator!r}"
    )

    n_runs = n_override if n_override is not None else len(runs)
    scale = precision.get("scale", "linear")
    # B4(b) (critic finding): an unrecognized `scale` would otherwise fall
    # through to the `if scale == "log":` branch's `else`, silently computing
    # `measurability_pass` on the LINEAR `se_at_this_n` against a threshold
    # that, for a log-scale metric (M11), is stored in LOG units -- a
    # fabricated pass/fail off mismatched units, not a loud failure. Assert
    # rather than silently mistag.
    assert scale in ("linear", "log"), (
        f"{band.id}: precision.scale must be 'linear' or 'log', got {scale!r}"
    )

    sd_live = (
        sd_live_precomputed
        if sd_live_precomputed is not None
        else _jackknife_sd_over_seeds(raw_fn, runs, band)
    )
    precision_stale = precision.get("gm_tuning_version") != gm_tuning_version
    sd_source = "this_run" if precision_stale else "precertified"

    sd_seeds = precision.get("sd_seeds")
    if sd_source == "precertified":
        sd = precision.get("sd_across_seeds")
        # Nit 2 (guard rather than raise, Sec.6.2 item 2): a precertified
        # block missing `sd_seeds` must degrade `se_at_this_n` to `None`
        # (and, downstream, `precision_available` to `False`) rather than
        # crash inside `sd_seeds / n_runs` with `sd_seeds is None`.
        se_at_this_n = (
            sd * math.sqrt(sd_seeds / n_runs)
            if sd is not None and sd_seeds is not None and n_runs
            else None
        )
    else:
        sd = sd_live
        se_at_this_n = sd_live

    margin_over_band = value - band.band_low if value is not None else None
    margin_in_se = (
        margin_over_band / se_at_this_n
        if margin_over_band is not None and se_at_this_n
        else None
    )

    # B1 fix (Decision P2-47): `se_at_sd_seeds`/`margin_in_se_at_sd_seeds`
    # are the precert's own SE used UNEXTRAPOLATED, paired with this run's
    # own point estimate -- a companion figure to `margin_in_se`, not a
    # replacement. Only meaningful when a precertified SD exists at all;
    # `None` for `sd_source == "this_run"` (no separate large-n figure to
    # report). See `_precision_detail`'s own docstring point 4b.
    se_at_sd_seeds = sd if sd_source == "precertified" else None
    margin_in_se_at_sd_seeds = (
        margin_over_band / se_at_sd_seeds
        if margin_over_band is not None and se_at_sd_seeds
        else None
    )

    detail: dict[str, object] = {
        "sd_source": sd_source,
        "sd_across_seeds": sd,
        "sd_across_seeds_this_run": sd_live,
        "n_runs": n_runs,
        "se_at_this_n": se_at_this_n,
        "margin_over_band": margin_over_band,
        "margin_in_se": margin_in_se,
        "se_at_sd_seeds": se_at_sd_seeds,
        "margin_in_se_at_sd_seeds": margin_in_se_at_sd_seeds,
        "se_threshold_derived": precision.get("se_threshold_derived"),
        "precision_stale": precision_stale,
        "sd_precertified": precision,
    }

    threshold = precision.get("se_threshold_derived")
    measurability_se = se_at_this_n

    if scale == "log":
        sd_live_log = _jackknife_sd_over_seeds(raw_fn, runs, band, transform=_log_or_none)
        if sd_source == "precertified":
            sd_log = precision.get("sd_across_seeds_log")
            # Same nit-2 guard as the linear branch above: a precertified
            # block missing `sd_seeds` must degrade to `None`, not raise.
            se_at_this_n_log = (
                sd_log * math.sqrt(sd_seeds / n_runs)
                if sd_log is not None and sd_seeds is not None and n_runs
                else None
            )
        else:
            sd_log = sd_live_log
            se_at_this_n_log = sd_live_log

        log_value = _log_or_none(value)
        log_band_low = _log_or_none(band.band_low)
        margin_over_band_log = (
            log_value - log_band_low
            if log_value is not None and log_band_low is not None
            else None
        )
        margin_in_se_log = (
            margin_over_band_log / se_at_this_n_log
            if margin_over_band_log is not None and se_at_this_n_log
            else None
        )
        detail.update(
            {
                "sd_across_seeds_log": sd_log,
                "sd_across_seeds_log_this_run": sd_live_log,
                "se_at_this_n_log": se_at_this_n_log,
                "margin_over_band_log": margin_over_band_log,
                "margin_in_se_log": margin_in_se_log,
            }
        )
        measurability_se = se_at_this_n_log

    measurability_pass = (
        measurability_se <= threshold
        if measurability_se is not None and threshold is not None
        else None
    )
    required_seeds = (
        math.ceil(n_runs * (measurability_se / threshold) ** 2)
        if measurability_pass is False and measurability_se and threshold
        else None
    )
    detail["measurability_pass"] = measurability_pass
    detail["required_seeds_if_over_threshold"] = required_seeds
    detail["precision_available"] = se_at_this_n is not None
    return detail


def compute_m11(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M11"]
    return _attach_dual_reading(_m11_core(runs, band), _m11_raw_value, runs, band, bands)


_FAMILY3_CITATION_VERDICT_KEYS: frozenset[str] = frozenset({"status", "passed", "counted_in_go"})
"""NIT 2 (critic round T dispatch): a `params.gated_value_precertified`
citation is specified to carry value + provenance + `gm_tuning_version`
lock ONLY -- Decision P3-47 round 39's own spec, "never status/passed/
detail, so the band stays the single source of the verdict." Nothing
previously rejected a citation carrying these keys anyway -- they would be
silently copied verbatim into `detail["gated_value_precertified"]` and
could land in `report.json` looking like they mean something (e.g. a
stray `passed: false` sitting next to a real `PASS` status computed from
the band). Stripped at the point of storage (`_strip_family3_citation_
verdict_keys` below) rather than merely documented as inert."""


def _strip_family3_citation_verdict_keys(citation: dict) -> dict:
    """Drops any `_FAMILY3_CITATION_VERDICT_KEYS` key from a Family-3
    `gated_value_precertified` citation before it is stored in `detail` --
    see `_FAMILY3_CITATION_VERDICT_KEYS` for why. Applied by both
    `_attach_dual_reading` (M11-M13/M15/M16) and `compute_m14` (its own
    separate hookup) so every Family-3 metric gets the same guarantee."""
    return {k: v for k, v in citation.items() if k not in _FAMILY3_CITATION_VERDICT_KEYS}


class Family3CitationStatus(StrEnum):
    """Decision P4-72's ONE shared Family-3 staleness verdict, replacing
    three independently-patched copies of the same comparison (Decision
    P4-59 patched two of the three sites in parallel and produced two
    different access idioms -- a `.get()` at `:1264` and a bare subscript
    at `:1515`/`:1476` -- which raises `KeyError` under the live fixture's
    actual state (every citation is missing `behavior_scope` today,
    Decision P4-73) instead of reading STALE, the opposite of what
    Decision P4-46's fail-closed rule requires)."""

    VALID = "valid"
    ABSENT = "absent"
    VERSION_MISMATCH = "version_mismatch"
    SCOPE_MISSING = "scope_missing"
    SCOPE_MISMATCH = "scope_mismatch"


def _family3_citation_status(
    citation: dict | None,
    *,
    gm_tuning_version: int,
    behavior_scope_version: int = GM_BEHAVIOR_SCOPE_VERSION,
) -> Family3CitationStatus:
    """Decision P4-72/P4-73: the one predicate all three sites
    (`_attach_dual_reading`'s `:1264`, `compute_m14`'s `:1476`/`:1515`)
    call, instead of each comparing `gm_tuning_version`/`behavior_scope`
    itself. **Every key read is `.get()`** -- this is the mechanism
    Decision P4-46's fail-closed rule runs on, not a style choice: every
    one of the six live `gated_value_precertified` citations plus the one
    `se_precertified` block carries no `behavior_scope` key at all as of
    this commit (measured, Decision P4-73: `grep -c behavior_scope
    seeds/gm_validation_bands.json` -> `0`), and a bare subscript against
    that raises instead of reading STALE.

    `ABSENT` (citation is `None`) is kept distinct from the four stale
    verdicts below it, per Decision P4-72's own requirement -- `compute_
    m14`'s `:1476` hookup treats "no citation at all" (fall through to
    the live measurability check, `extra_pass = measurability_pass`)
    differently from "a citation exists and is stale" (loudly re-arm AND
    set `precertification_stale = True`); collapsing the two here would
    silently change that reporting.

    **`behavior_scope_version` is a real keyword parameter, not a bare
    module-global read (critic nit 6 fix)** -- symmetric with `gm_tuning_
    version`, which every caller already threads explicitly, rather than
    reading `managers.tuning.GM_TUNING_VERSION` off a global too. Defaults
    to the live `GM_BEHAVIOR_SCOPE_VERSION`, so every existing caller
    (none pass this keyword today) is unaffected; a test can now drive the
    scope conjunct directly off a fixture value instead of monkeypatching
    the module constant to exercise it."""
    if citation is None:
        return Family3CitationStatus.ABSENT
    if citation.get("gm_tuning_version") != gm_tuning_version:
        return Family3CitationStatus.VERSION_MISMATCH
    if "behavior_scope" not in citation:
        return Family3CitationStatus.SCOPE_MISSING
    if citation.get("behavior_scope") != behavior_scope_version:
        return Family3CitationStatus.SCOPE_MISMATCH
    return Family3CitationStatus.VALID


def _family3_citation_reason(
    status: Family3CitationStatus, citation: dict | None, *, gm_tuning_version: int
) -> str:
    """Decision P4-59's three distinguishable STALE renderings (a fourth,
    `ABSENT`, never reaches this function -- callers only render a reason
    string when a citation exists and is stale), so a reader cannot
    mistake "this citation predates the scope conjunct" for a regression.
    Before this function existed, every stale render printed the version-
    mismatch sentence unconditionally -- under a SCOPE-only mismatch with
    `GM_TUNING_VERSION` unbumped (Phase 4's own expected case, since
    Decision P4-46 deliberately never bumps it), that sentence rendered
    as "cited at gm_tuning_version 3, bands now 3," stating its own
    premise false."""
    if status is Family3CitationStatus.VERSION_MISMATCH:
        assert citation is not None
        return (
            f"cited at gm_tuning_version {citation.get('gm_tuning_version')}, "
            f"bands now {gm_tuning_version}"
        )
    if status is Family3CitationStatus.SCOPE_MISMATCH:
        assert citation is not None
        return (
            f"cited at behavior_scope {citation.get('behavior_scope')}, "
            f"live scope {GM_BEHAVIOR_SCOPE_VERSION}"
        )
    if status is Family3CitationStatus.SCOPE_MISSING:
        return "citation carries no behavior_scope -- fail-closed per P4-46"
    # VALID/ABSENT never reach here (callers only ask for a reason when
    # rendering a STALE fragment) -- returning an empty string rather
    # than raising keeps this a total function for a caller that ends up
    # calling it defensively.
    return ""


def _attach_dual_reading(
    mv: MetricValue, raw_fn, runs, band: MetricBand, bands: BandSpec
) -> MetricValue:
    """Shared wrapper for M11-M13/M15/M16 (`_attach_precision`'s
    successor, renamed under Decision P3-47 since it now does more than
    attach a precision block). Computes the original, untouched
    `MetricValue` via `_mN_core` and, only when the metric produced a real
    value AND the bands fixture opted in, merges in `detail["precision"]`
    exactly as before. Nothing here can change `undefined`, `skip_reason`,
    `per_season`, or `extra_pass`.

    **Absent `params.gated_value_precertified`: byte-identical to the
    pre-P3-47 `_attach_precision` behaviour** -- a bands fixture that never
    asked for the new citation is completely unaffected (Sec.11 step 9
    test 4's own backward-compatibility guarantee), and `value` is
    untouched here in that case.

    **Present (Decision P3-47, Sec.11 step 9's Family-3 measurability
    fix):** dual reading, not full replacement. This run's own live value
    is always computed and always reported under `detail["live_reading"]`
    (`{value, passed, precision, indeterminate}` -- `passed` via the same
    imported `evaluate` the real gate path uses, `indeterminate` per the
    fail-side rule this build's `_finalize` also implements: `passed is
    False and margin_in_se > -2.0`, i.e. a FAIL that is not distinguishably
    real at this seed count). When the citation is present AND version-
    matched against `bands.gm_tuning_version`, `.value` becomes the
    citation's own 45-seed value and `detail["value_source"]` reads
    `"precertified_corpus"`; a stale or absent-after-being-present (version
    mismatch) citation falls back to the live value loudly
    (`value_source == "this_run"`, `gated_value_precert_stale == True`) --
    never `SKIP`, per Decision P3-37's own discipline against a
    dynamically-shrinking, silently-greened counted denominator.
    `detail["precision"]` always describes whichever reading is actually
    gating (the gated block when the citation is valid, else the live
    block), so `_finalize`'s own `params.precision`-keyed branch never
    describes a number other than the one that actually gated."""
    if mv.value is None:
        return mv

    live_value = mv.value
    citation = band.params.get("gated_value_precertified")

    if citation is None:
        precision = _precision_detail(
            raw_fn, runs, band, live_value, gm_tuning_version=bands.gm_tuning_version
        )
        if precision is None:
            return mv
        return _dataclasses_replace(mv, detail={**mv.detail, "precision": precision})

    # A valid citation needs the live jackknife SD too (for `live_reading`'s
    # own precision block) -- computed once here and threaded into both
    # `_precision_detail` calls below via `sd_live_precomputed`, rather than
    # recomputed a second time for the gated block.
    sd_live = _jackknife_sd_over_seeds(raw_fn, runs, band)
    live_precision = _precision_detail(
        raw_fn, runs, band, live_value,
        gm_tuning_version=bands.gm_tuning_version, sd_live_precomputed=sd_live,
    )
    live_passed = evaluate(band, live_value)
    live_margin_in_se = live_precision.get("margin_in_se") if live_precision is not None else None
    live_indeterminate = (
        live_passed is False and live_margin_in_se is not None and live_margin_in_se > -2.0
    )

    detail: dict[str, object] = {
        **mv.detail,
        "live_reading": {
            "value": live_value,
            "passed": live_passed,
            "precision": live_precision,
            "indeterminate": live_indeterminate,
        },
        "gated_value_precertified": _strip_family3_citation_verdict_keys(citation),
    }

    citation_status = _family3_citation_status(citation, gm_tuning_version=bands.gm_tuning_version)
    citation_valid = citation_status is Family3CitationStatus.VALID
    if not citation_valid:
        detail["value_source"] = "this_run"
        detail["gated_value_precert_stale"] = True
        if live_precision is not None:
            detail["precision"] = live_precision
        return _dataclasses_replace(mv, detail=detail)

    detail["gated_value_precert_stale"] = False
    gated_value = citation["value"]
    gated_precision = _precision_detail(
        raw_fn, runs, band, gated_value,
        gm_tuning_version=bands.gm_tuning_version,
        n_override=citation["n_seeds"], sd_live_precomputed=sd_live,
    )
    detail["value_source"] = "precertified_corpus"
    if gated_precision is not None:
        detail["precision"] = gated_precision
    return _dataclasses_replace(mv, value=gated_value, detail=detail)


def _m11_raw_value(runs: tuple[RunRecord, ...], band: MetricBand) -> float | None:
    return _m11_core(runs, band).value


def _m11_core(runs: tuple[RunRecord, ...], band: MetricBand) -> MetricValue:
    threshold = band.params["overall_threshold"]
    numerator_cohorts = {CompetitiveWindow(c) for c in band.params["numerator_cohorts"]}
    denominator_cohort = CompetitiveWindow(band.params["denominator_cohort"])

    num_hi = num_lo = 0
    team_seasons_hi = team_seasons_lo = 0
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, "season_end")
            for _team_id, window in b.window_by_team.items():
                if window in numerator_cohorts:
                    team_seasons_hi += 1
                elif window == denominator_cohort:
                    team_seasons_lo += 1
            for acq in season.acquisitions:
                if acq.overall < threshold:
                    continue
                if acq.window_at_acquisition in numerator_cohorts:
                    num_hi += 1
                elif acq.window_at_acquisition == denominator_cohort:
                    num_lo += 1

    detail = {
        "num_hi_acquisitions": num_hi,
        "num_lo_acquisitions": num_lo,
        "team_seasons_hi": team_seasons_hi,
        "team_seasons_lo": team_seasons_lo,
    }
    if team_seasons_hi == 0 or team_seasons_lo == 0:
        return MetricValue(value=None, detail=detail, undefined=True)
    rate_hi = num_hi / team_seasons_hi
    rate_lo = num_lo / team_seasons_lo
    if rate_lo == 0:
        return MetricValue(value=None, detail={**detail, "rate_lo": rate_lo}, undefined=True)
    return MetricValue(
        value=rate_hi / rate_lo, detail={**detail, "rate_hi": rate_hi, "rate_lo": rate_lo}
    )


def compute_m12(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M12"]
    return _attach_dual_reading(_m12_core(runs, band), _m12_raw_value, runs, band, bands)


def _m12_raw_value(runs: tuple[RunRecord, ...], band: MetricBand) -> float | None:
    return _m12_core(runs, band).value


def _m12_core(runs: tuple[RunRecord, ...], band: MetricBand) -> MetricValue:
    cohort_a = CompetitiveWindow(band.params["cohorts"][0])  # rebuild
    cohort_b = CompetitiveWindow(band.params["cohorts"][1])  # contend

    values_a: list[int] = []
    values_b: list[int] = []
    for run in runs:
        for season in run.seasons:
            b = _boundary(season, "season_end")
            for team_id, window in b.window_by_team.items():
                net = season.future_first_net_by_team.get(team_id, 0)
                if window == cohort_a:
                    values_a.append(net)
                elif window == cohort_b:
                    values_b.append(net)
    if not values_a or not values_b:
        return MetricValue(
            value=None,
            detail={"n_rebuild": len(values_a), "n_contend": len(values_b)},
            undefined=True,
        )
    mean_a, mean_b = statistics.mean(values_a), statistics.mean(values_b)
    return MetricValue(
        value=mean_a - mean_b,
        detail={"mean_rebuild": mean_a, "mean_contend": mean_b, "n_rebuild": len(values_a),
                "n_contend": len(values_b)},
    )


def compute_m13(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M13"]
    return _attach_dual_reading(_m13_core(runs, band), _m13_raw_value, runs, band, bands)


def _m13_raw_value(runs: tuple[RunRecord, ...], band: MetricBand) -> float | None:
    return _m13_core(runs, band).value


def _m13_core(runs: tuple[RunRecord, ...], band: MetricBand) -> MetricValue:
    resolves_at = band.params["resolves_at_season"]
    cohort_a = CompetitiveWindow(band.params["cohorts"][0])  # contend
    cohort_b = CompetitiveWindow(band.params["cohorts"][1])  # rebuild

    values_a: list[float] = []
    values_b: list[float] = []
    for run in runs:
        if len(run.seasons) < resolves_at:
            continue
        season = run.seasons[resolves_at - 1]
        b = _boundary(season, "season_end")
        for team_id, window in b.window_by_team.items():
            age = season.minutes_weighted_age_by_team.get(team_id)
            if age is None:
                continue
            if window == cohort_a:
                values_a.append(age)
            elif window == cohort_b:
                values_b.append(age)
    if not values_a or not values_b:
        skip_reason = None
        if all(len(run.seasons) < resolves_at for run in runs):
            skip_reason = f"no run has >= {resolves_at} seasons"
        return MetricValue(
            value=None,
            detail={"n_contend": len(values_a), "n_rebuild": len(values_b)},
            skip_reason=skip_reason,
            undefined=skip_reason is None,
        )
    mean_a, mean_b = statistics.mean(values_a), statistics.mean(values_b)
    return MetricValue(
        value=mean_a - mean_b,
        detail={"mean_contend": mean_a, "mean_rebuild": mean_b, "n_contend": len(values_a),
                "n_rebuild": len(values_b)},
    )


def compute_m14(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Decision P2-35 (architect round 17 Sec.3): the SE measurability
    conjunct can be satisfied by a version-locked `params.se_precertified`
    citation instead of the live `se_measured <= se_threshold` check on
    this run. `precert_valid` requires the citation's own `gm_tuning_
    version` to equal `bands.gm_tuning_version` -- the moment any constant
    under `managers/` moves and bumps that version, the citation
    automatically goes stale and the live conjunct re-arms with no manual
    bookkeeping. `measurability_pass` (the live check) is always computed
    and always reported -- at the top level of `detail` when no `params.
    gated_value_precertified` value citation is present (NIT 4, critic
    round T dispatch: this was stale after Decision P3-47's own hookup),
    else nested under `detail["live_reading"]["measurability_pass"]`
    alongside `se_measured`/`required_seeds_if_over_threshold`, which move
    there too -- even when a valid precertification is what actually
    carries `extra_pass`."""
    band = bands.metrics["M14"]
    cohort_a = GmPhilosophy(band.params["cohort_a"])  # star_maxer
    cohort_b = GmPhilosophy(band.params["cohort_b"])  # egalitarian
    top_rank = band.params["gap_top_rank"] - 1
    bottom_rank = band.params["gap_bottom_rank"] - 1
    se_threshold = band.params["se_threshold"]
    precert = band.params.get("se_precertified")

    deltas_a: list[float] = []
    deltas_b: list[float] = []
    dropped = 0
    for run in runs:
        if not run.seasons:
            continue
        final_boundary = _boundary(run.seasons[-1], "post_waivers")
        for team_id, philosophy in run.philosophy_by_team.items():
            if philosophy not in (cohort_a, cohort_b):
                continue
            baseline = run.season1_top1_minus_8th_by_team.get(team_id)
            overalls = final_boundary.overalls_by_team.get(team_id, ())
            if baseline is None or len(overalls) <= bottom_rank:
                dropped += 1
                continue
            gap_end = float(overalls[top_rank] - overalls[bottom_rank])
            delta = gap_end - baseline
            (deltas_a if philosophy == cohort_a else deltas_b).append(delta)

    detail: dict[str, object] = {"dropped_observations": dropped, "n_star_maxer": len(deltas_a),
                                  "n_egalitarian": len(deltas_b)}
    if len(deltas_a) < 2 or len(deltas_b) < 2:
        detail["se_measured"] = None
        return MetricValue(value=None, detail=detail, undefined=True, extra_pass=False)

    mean_a, mean_b = statistics.mean(deltas_a), statistics.mean(deltas_b)
    var_a, var_b = statistics.variance(deltas_a), statistics.variance(deltas_b)
    se = math.sqrt(var_a / len(deltas_a) + var_b / len(deltas_b))
    measurability_pass = se <= se_threshold
    n_runs = len(runs)
    # Nit-fix (architect round 17 Sec.3.2 point 6): basis was `min_seeds`,
    # the design-floor constant, not the actual seed count behind `se` --
    # HANDOFF:68 disclosed this said "4 more seeds" at a 36-seed
    # measurement when ~39-45 were actually needed. `len(runs)` is the
    # real basis; today's `27` was only ever correct by coincidence
    # (`len(runs) == min_seeds == 3` in every fixture run so far).
    required_seeds = math.ceil(n_runs * (se / se_threshold) ** 2) if se_threshold else None

    precert_status = _family3_citation_status(precert, gm_tuning_version=bands.gm_tuning_version)
    precert_valid = precert_status is Family3CitationStatus.VALID
    extra_pass = True if precert_valid else measurability_pass
    measurability_source = "precertified" if precert_valid else "this_run"
    precertification_stale = precert is not None and not precert_valid
    se_extrapolated = (
        se * math.sqrt(n_runs / precert["seeds"]) if precert is not None else None
    )
    precertification_consistent = (
        se_extrapolated <= se_threshold if se_extrapolated is not None else None
    )

    detail.update(
        {
            "mean_star_maxer": mean_a,
            "mean_egalitarian": mean_b,
            "se_measured": se,
            "se_threshold": se_threshold,
            "measurability_pass": measurability_pass,
            "required_seeds_if_over_threshold": required_seeds if not measurability_pass else None,
            "measurability_source": measurability_source,
            "se_precertified": precert,
            "precertification_stale": precertification_stale,
            "se_extrapolated_at_precert_seeds": se_extrapolated,
            "precertification_consistent": precertification_consistent,
        }
    )

    live_value = mean_a - mean_b
    gated_value = live_value
    # Decision P3-47 (Sec.11 step 9's Family-3 measurability fix), 4c's own
    # hookup: M14 has no `params.precision` block to hang the RS3 dual-
    # reading wrapper (`_attach_dual_reading`) off of, so it gets this
    # small, separate hookup instead -- extra_pass is UNCHANGED by any of
    # this, it still comes from `se_precertified` exactly as above. Absent
    # `params.gated_value_precertified`: none of the keys below are added
    # at all, keeping this byte-identical to pre-P3-47 behaviour for a
    # bands fixture that never asked for the new citation.
    value_citation = band.params.get("gated_value_precertified")
    if value_citation is not None:
        value_citation_status = _family3_citation_status(
            value_citation, gm_tuning_version=bands.gm_tuning_version
        )
        value_citation_valid = value_citation_status is Family3CitationStatus.VALID
        live_passed = evaluate(band, live_value)
        live_margin_in_se = (live_value - band.band_low) / se if se else None
        live_indeterminate = (
            live_passed is False
            and live_margin_in_se is not None
            and live_margin_in_se > -2.0
        )
        detail["live_reading"] = {
            "value": live_value,
            "se_measured": detail.pop("se_measured"),
            "measurability_pass": detail.pop("measurability_pass"),
            "required_seeds_if_over_threshold": detail.pop("required_seeds_if_over_threshold"),
            "passed": live_passed,
            "indeterminate": live_indeterminate,
        }
        detail["gated_value_precertified"] = _strip_family3_citation_verdict_keys(value_citation)
        detail["gated_value_precert_stale"] = not value_citation_valid
        if value_citation_valid:
            detail["value_source"] = "precertified_corpus"
            gated_value = value_citation["value"]
        else:
            detail["value_source"] = "this_run"

    return MetricValue(value=gated_value, detail=detail, extra_pass=extra_pass)


def compute_m15(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M15"]
    return _attach_dual_reading(_m15_core(runs, band), _m15_raw_value, runs, band, bands)


def _m15_raw_value(runs: tuple[RunRecord, ...], band: MetricBand) -> float | None:
    return _m15_core(runs, band).value


def _m15_core(runs: tuple[RunRecord, ...], band: MetricBand) -> MetricValue:
    cohort_a = GmPhilosophy(band.params["cohorts"][0])  # vet_overrater
    cohort_b = GmPhilosophy(band.params["cohorts"][1])  # youth_developer

    ages_a: list[int] = []
    ages_b: list[int] = []
    for run in runs:
        for season in run.seasons:
            for acq in season.acquisitions:
                philosophy = run.philosophy_by_team.get(acq.team_id)
                if philosophy == cohort_a:
                    ages_a.append(acq.age)
                elif philosophy == cohort_b:
                    ages_b.append(acq.age)
    if not ages_a or not ages_b:
        return MetricValue(
            value=None, detail={"n_vet_overrater": len(ages_a), "n_youth_developer": len(ages_b)},
            undefined=True,
        )
    mean_a, mean_b = statistics.mean(ages_a), statistics.mean(ages_b)
    return MetricValue(
        value=mean_a - mean_b,
        detail={"mean_vet_overrater": mean_a, "mean_youth_developer": mean_b,
                "n_vet_overrater": len(ages_a), "n_youth_developer": len(ages_b)},
    )


def compute_m16(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    band = bands.metrics["M16"]
    # NIT 3 (critic round T dispatch): mirror `compute_m10`'s own guard
    # (Decision P2-40) so a future `exit_criterion.min_seasons` change
    # can't silently desynchronize M16's season pin from the citation
    # measured at that season index.
    resolves_at = band.params["resolves_at_season"]
    assert resolves_at == bands.exit_criterion["min_seasons"], (
        f"M16's gated season (params.resolves_at_season={resolves_at}) must equal "
        f"exit_criterion.min_seasons ({bands.exit_criterion['min_seasons']}) -- "
        "Decision P3-47 step 1's operating point is pinned to Decision P2-22's own floor "
        "and must not silently drift"
    )
    return _attach_dual_reading(_m16_core(runs, band), _m16_raw_value, runs, band, bands)


def _m16_raw_value(runs: tuple[RunRecord, ...], band: MetricBand) -> float | None:
    return _m16_core(runs, band).value


def _m16_core(runs: tuple[RunRecord, ...], band: MetricBand) -> MetricValue:
    """Decision P3-47 step 1: mirrors `compute_m10`'s own `Decision P2-40`
    precedent -- the gated reading is pinned to a fixed season index
    (`params.resolves_at_season`), not whichever season happened to be
    last (`run.seasons[-1]`). "The final season" is not invariant in run
    length (a longer run reads a still-changing later cell); a fixed index
    is (`run.seasons[resolves_at - 1]` is byte-identical whether the run
    requests 5 seasons or 8). Verified a no-op at `seasons=5` (every
    corpus this band gates on today) before this change was accepted --
    `run.seasons[4] is run.seasons[-1]` whenever `len(run.seasons) == 5`.

    A run with fewer than `resolves_at` seasons contributes nothing to
    either mean and increments `detail["dropped_runs"]` -- `compute_m13`'s
    own established shape for this same pin pattern, reused deliberately
    rather than raising `IndexError` on `run.seasons[resolves_at - 1]`."""
    cohort = GmPhilosophy(band.params["cohort"])
    resolves_at = band.params["resolves_at_season"]

    cohort_team_means: list[float] = []
    league_team_means: list[float] = []
    dropped_runs = 0
    for run in runs:
        if len(run.seasons) < resolves_at:
            dropped_runs += 1
            continue
        final_season = run.seasons[resolves_at - 1]
        b = _boundary(final_season, "season_end")
        for team_id, roster_ids in b.roster_ids_by_team.items():
            per_player = [
                final_season.def_group_mean_by_player[pid]
                for pid in roster_ids
                if pid in final_season.def_group_mean_by_player
            ]
            if not per_player:
                continue
            team_mean = statistics.mean(per_player)
            league_team_means.append(team_mean)
            if run.philosophy_by_team.get(team_id) == cohort:
                cohort_team_means.append(team_mean)
    if not cohort_team_means or not league_team_means:
        skip_reason = None
        if runs and dropped_runs == len(runs):
            skip_reason = f"no run has >= {resolves_at} seasons"
        return MetricValue(
            value=None,
            detail={
                "n_cohort_teams": len(cohort_team_means),
                "n_league_teams": len(league_team_means),
                "dropped_runs": dropped_runs,
            },
            skip_reason=skip_reason,
            undefined=skip_reason is None,
        )
    cohort_mean = statistics.mean(cohort_team_means)
    league_mean = statistics.mean(league_team_means)
    return MetricValue(
        value=cohort_mean - league_mean,
        detail={"cohort_mean": cohort_mean, "league_mean": league_mean,
                "n_cohort_teams": len(cohort_team_means), "n_league_teams": len(league_team_means),
                "dropped_runs": dropped_runs},
    )


def _m17_need_matrix():
    """A single, fixed `NeedMatrix` shared by every slot/philosophy/seed in
    M17's own synthetic monoculture drafts.

    **Corrected during this slice's own real end-to-end run** (not by a
    critic pass): the first version of this function built the matrix from
    an EMPTY roster, reasoning that `need = 1.0` uniformly at every
    position/tier would be a neutral, symmetric choice. It is not neutral
    -- it is degenerate. `needs.py::need_bonus`'s own formula is `fit_sens
    * (need_gain * Sum_pos coverage(p, pos) * need[pos][tier(p)] - ...)`,
    and `Sum_pos coverage(p, pos)` is exactly 1.0 for every player
    (`position_coverage`'s own normalization, Decision P2-28) -- so with
    `need[pos][tier] == 1.0` for every `(pos, tier)`, `need_bonus` collapses
    to `fit_sens * need_gain` **for every prospect**, regardless of that
    prospect's own position or overall. A per-prospect-constant `need_
    bonus` cannot change `prospect_fit_value_for`'s own *ranking* versus
    bare `prospect_value_for` at all (multiplying every candidate by the
    same `(1 + constant)` factor is order-preserving) -- so the empty-
    roster version made `run_draft`'s own fit-value picks structurally
    IDENTICAL to the bare-board picks for every philosophy, collapsing
    M17 to a hard, structural 0.0 regardless of `fit_sens` (confirmed on
    the real end-to-end smoke run: `rate_star_maxer == rate_egalitarian ==
    1.0` exactly, at 3,000 picks each -- not noise). That is exactly the
    signal this metric exists to detect being destroyed by its own
    fixture, the same defect class this project's own history repeatedly
    names ("the fix's own test doesn't discriminate the thing it claims
    to").

    Fixed here with a small, fixed, deliberately position-imbalanced
    8-man reference roster (heavy at PG/SG, nothing at SF/PF/C) built via
    `tests.fixtures.managers.make_roster_player` -- real, varying `need[
    pos][tier]` values across positions is what lets `need_bonus` actually
    depend on a given prospect's own position, which is what lets `fit_
    sens` (the dial M17 exists to isolate) actually move the ranking."""
    dummy_team = Team(
        id=1, league_id=_M17_LEAGUE_ID, code="M17", name="M17", city="M17",
        conference="East", division="Atlantic", manager_user_id=None,
        gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.RETOOL, home_market_size=3,
    )
    cap_settings = _load_real_cap_settings(_M17_LEAGUE_ID, _M17_SEASON)
    backcourt = (
        (Position.PG, 82), (Position.PG, 74), (Position.PG, 68), (Position.PG, 60),
        (Position.SG, 80), (Position.SG, 72), (Position.SG, 65), (Position.SG, 58),
    )
    roster = tuple(
        make_roster_player(
            9000 + i, age=27, season=_M17_SEASON, overall_cached=overall, team_id=1,
            primary_position=position,
        )
        for i, (position, overall) in enumerate(backcourt)
    )
    snapshot = build_team_snapshot(dummy_team, roster, (), cap_settings)
    return build_need_matrix(snapshot, tuning=DEFAULT_TUNING)


def _m17_own_board_matches(
    prospects,
    philosophy: GmPhilosophy,
    *,
    seed: int,
    cap_settings,
    rookie_scale,
    matrix,
    tuning: GmTuning,
) -> tuple[int, int]:
    """Runs one real 30-slot monoculture draft (every slot the same
    philosophy) via `run_draft`, then independently replays the same
    picking order against that philosophy's own bare `prospect_value_for`
    board (need bonus zeroed -- the literal function, not `prospect_fit_
    value_for`) at each pick's own real `implied_slot`, using the exact
    same tie-break `run_draft` itself uses (`max` on `(value, -id)`) so a
    coincidental tie is scored consistently. Returns `(matches, total)`."""
    draft_order = tuple(
        DraftSlot(team_id=i + 1, philosophy=philosophy, window=_M17_WINDOW, matrix=matrix,
                  pick_number=i + 1)
        for i in range(30)
    )
    results = run_draft(
        prospects, draft_order, league_rng_seed=seed, season=_M17_SEASON,
        cap_settings=cap_settings, rookie_scale=rookie_scale, tuning=tuning,
    )
    remaining = {rp.id: rp for rp in prospects}
    matches = 0
    total = 0
    for result in results:
        scored = [
            (
                rp,
                prospect_value_for(
                    rp, league_rng_seed=seed, team_id=result.team_id, season=_M17_SEASON,
                    philosophy=philosophy, window=_M17_WINDOW, cap_settings=cap_settings,
                    rookie_scale=rookie_scale, implied_slot=result.pick_number, tuning=tuning,
                ),
            )
            for rp in remaining.values()
        ]
        best_rp, _ = max(scored, key=lambda pair: (pair[1], -pair[0].id))
        total += 1
        if best_rp.id == result.prospect_id:
            matches += 1
        remaining.pop(result.prospect_id)
    return matches, total


def compute_m17(bands: BandSpec, *, tuning: GmTuning = DEFAULT_TUNING) -> MetricValue:
    """The one metric that needs no season simulation at all (report Sec.
    6.2 item 17) -- `runs` is not a parameter (see module docstring's
    "disclosed exception"). 100 seeded 60-prospect classes (`tools/
    _gm_draft_class_stub.generate_draft_class`), each run through two
    independent 30-slot monoculture drafts (all `star_maxer`, all
    `egalitarian`), each pick compared against that same philosophy's own
    bare-`prospect_value_for` board."""
    band = bands.metrics["M17"]
    drafts = band.params["drafts"]
    cohort_a = GmPhilosophy(band.params["cohorts"][0])  # star_maxer
    cohort_b = GmPhilosophy(band.params["cohorts"][1])  # egalitarian

    cap_settings = _load_real_cap_settings(_M17_LEAGUE_ID, _M17_SEASON)
    rookie_scale = _load_rookie_scale()
    matrix = _m17_need_matrix()

    matches_a = total_a = matches_b = total_b = 0
    per_class: list[tuple[int, int, int, int]] = []
    for i in range(drafts):
        seed = i
        prospects = generate_draft_class(
            season=_M17_SEASON, seed=seed, league_id=_M17_LEAGUE_ID, tuning=tuning
        )
        m_a, t_a = _m17_own_board_matches(
            prospects, cohort_a, seed=seed, cap_settings=cap_settings, rookie_scale=rookie_scale,
            matrix=matrix, tuning=tuning,
        )
        m_b, t_b = _m17_own_board_matches(
            prospects, cohort_b, seed=seed, cap_settings=cap_settings, rookie_scale=rookie_scale,
            matrix=matrix, tuning=tuning,
        )
        matches_a += m_a
        total_a += t_a
        matches_b += m_b
        total_b += t_b
        per_class.append((m_a, t_a, m_b, t_b))

    if total_a == 0 or total_b == 0:
        return MetricValue(value=None, detail={}, undefined=True)
    rate_a = matches_a / total_a
    rate_b = matches_b / total_b
    value = rate_a - rate_b
    detail: dict[str, object] = {
        "rate_star_maxer": rate_a, "rate_egalitarian": rate_b, "drafts": drafts,
        "total_picks_star_maxer": total_a, "total_picks_egalitarian": total_b,
    }
    precision = _m17_precision_detail(
        band, value, per_class, matches_a, total_a, matches_b, total_b
    )
    if precision is not None:
        detail["precision"] = precision
    return MetricValue(value=value, detail=detail)


def _m17_precision_detail(
    band: MetricBand,
    value: float,
    per_class: list[tuple[int, int, int, int]],
    matches_a: int,
    total_a: int,
    matches_b: int,
    total_b: int,
) -> dict[str, object] | None:
    """M17's different, simpler treatment (round 29 Sec.6.4): no `params.
    precision` block, no threshold, no gate -- `compute_m17` takes no
    `runs` at all and draws its 100 classes at `seed = i`, so its value has
    exactly zero seed-count variance (Sec.3.7). The only real precision
    question left is resampling variance over WHICH 100 classes were
    drawn, answered here for free via a delete-one jackknife over the
    already-retained per-class `(m_a, t_a, m_b, t_b)` counts. Reported for
    completeness only -- `_finalize`'s new annotation branch never fires
    for M17, since M17's own `bands.json` entry carries no `params.
    precision` key (see that branch's own guard)."""
    n = len(per_class)
    if n < 2:
        return None
    replicates: list[float] = []
    for m_a_i, t_a_i, m_b_i, t_b_i in per_class:
        ta_loo = total_a - t_a_i
        tb_loo = total_b - t_b_i
        if ta_loo == 0 or tb_loo == 0:
            return None
        ra_loo = (matches_a - m_a_i) / ta_loo
        rb_loo = (matches_b - m_b_i) / tb_loo
        replicates.append(ra_loo - rb_loo)
    theta_bar = sum(replicates) / n
    ss = sum((t - theta_bar) ** 2 for t in replicates)
    sd_jack = math.sqrt((n - 1) / n * ss)
    margin_over_band = value - band.band_low
    margin_in_se = margin_over_band / sd_jack if sd_jack else None
    return {
        "resampling_unit": "draft_class",
        "n_classes": n,
        "sd_jackknife": sd_jack,
        "se": sd_jack,
        "margin_over_band": margin_over_band,
        "margin_in_se": margin_in_se,
        "seed_invariant": True,
        "note": (
            "This metric takes no `runs` and draws its classes at seed = i "
            "for i in range(100); its value is a pure function of code and "
            "GmTuning and does not vary with the run's seed count at all. "
            "Reported for completeness; RS3's seed-count question does not "
            "apply to this item (architect round 29 Sec.3.7)."
        ),
    }


# ---------------------------------------------------------------------------
# Family 4 -- market liveness, items 18-22
# ---------------------------------------------------------------------------


def compute_m18(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    per_season: list[int] = []
    detail_rows: list[dict] = []
    for run in runs:
        for season in run.seasons:
            count = sum(len(execs) for execs in season.trades_by_window)
            per_season.append(count)
            detail_rows.append({"rng_seed": run.rng_seed, "season": season.season, "trades": count})
    if not per_season:
        return MetricValue(value=None, detail={}, skip_reason="no seasons in `runs`")
    any_zero = any(c == 0 for c in per_season)
    return MetricValue(
        value=statistics.mean(per_season),
        detail={"per_season": detail_rows, "any_season_zero": any_zero},
        per_season=tuple(float(x) for x in per_season),
        extra_pass=not any_zero,
    )


def compute_m19(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    total_mutual = 0
    total_packages = 0
    for run in runs:
        for season in run.seasons:
            total_mutual += sum(season.mutual_accepts)
            total_packages += sum(season.packages_evaluated)
    if total_packages == 0:
        return MetricValue(value=None, detail={}, skip_reason="no packages evaluated in `runs`")
    return MetricValue(
        value=total_mutual / total_packages,
        detail={"total_mutual_accepts": total_mutual, "total_packages_evaluated": total_packages},
    )


def compute_m20(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    worst_share = 0.0
    worst_row: dict | None = None
    for run in runs:
        for season in run.seasons:
            participations: dict[int, int] = {}
            total = 0
            for execs in season.trades_by_window:
                for execution in execs:
                    participations[execution.team_a_id] = (
                        participations.get(execution.team_a_id, 0) + 1
                    )
                    participations[execution.team_b_id] = (
                        participations.get(execution.team_b_id, 0) + 1
                    )
                    total += 1
            if total == 0:
                continue
            for team_id, count in participations.items():
                share = count / total
                if share > worst_share:
                    worst_share = share
                    worst_row = {
                        "rng_seed": run.rng_seed, "season": season.season, "team_id": team_id,
                        "share": share,
                    }
    if worst_row is None:
        return MetricValue(value=None, detail={}, skip_reason="no trades executed in `runs`")
    return MetricValue(value=worst_share, detail={"worst": worst_row})


def compute_m21(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Report Sec.6.2 item 21's reinterpretation: no ordered pair `(A, B)`
    trades the same player back in the *immediately following* trade
    window this same season. `run_trade_window`'s own `consumed_players`
    set already makes an intra-window repeat structurally impossible, so
    this checks only across the two windows within one season -- and per
    that same report section, is expected to always report 0 (the check
    is unfalsifiable as literally written; see `bands.py`'s own
    `MetricBand.interpreted` field and this file's module docstring)."""
    violations = 0
    checked_seasons = 0
    for run in runs:
        for season in run.seasons:
            if len(season.trades_by_window) != 2:
                continue
            checked_seasons += 1
            window1, window2 = season.trades_by_window
            moved_1: dict[tuple[int, int], set[int]] = {}
            for execution in window1:
                moved_1.setdefault((execution.team_a_id, execution.team_b_id), set()).update(
                    execution.players_a_to_b
                )
                moved_1.setdefault((execution.team_b_id, execution.team_a_id), set()).update(
                    execution.players_b_to_a
                )
            for execution in window2:
                window2_moves = (
                    (execution.team_a_id, execution.team_b_id, execution.players_a_to_b),
                    (execution.team_b_id, execution.team_a_id, execution.players_b_to_a),
                )
                for from_team, to_team, moved_players in window2_moves:
                    # A re-trade is the SAME player moving back in the
                    # OPPOSITE direction it moved in window1 -- so look up
                    # window1's own (to_team -> from_team) moves.
                    previously_moved = moved_1.get((to_team, from_team), set())
                    violations += len(previously_moved & set(moved_players))
    return MetricValue(
        value=float(violations),
        detail={"checked_seasons": checked_seasons, "interpretation": "consecutive_trade_windows"},
    )


def compute_m22(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    per_season_sd: list[float] = []
    for run in runs:
        for season in run.seasons:
            if season.win_totals is None:
                return MetricValue(value=None, detail={}, skip_reason="--skip-games")
            wins = list(season.win_totals.values())
            if len(wins) > 1:
                per_season_sd.append(statistics.pstdev(wins))
    if not per_season_sd:
        return MetricValue(
            value=None, detail={}, skip_reason="no seasons with win_totals in `runs`"
        )
    return MetricValue(
        value=statistics.mean(per_season_sd),
        detail={"per_season_sd": per_season_sd},
        per_season=tuple(per_season_sd),
    )


# ---------------------------------------------------------------------------
# Family 5 -- negotiation (Phase 3 §9/§11 step 7). M23-M30 plus M23'
# (`"M23p"` in this file and `seeds/gm_validation_bands.json` -- ASCII, no
# unicode prime, matching this module's own identifier conventions
# elsewhere). Every field these functions pool is `TradeWindowResult`'s own
# new Decision P3-12 counters, carried through `SeasonRecord` unchanged
# (see `season.py`'s own module docstring for the six-field list).
# ---------------------------------------------------------------------------

_OUTCOME_CENSUS_CELLS = (
    "illegal", "both_reject_legal", "A_reject_B_accept", "A_accept_B_reject", "both_accept",
)


def _pool_negotiation_totals(
    runs: tuple[RunRecord, ...],
) -> tuple[dict[str, float], dict[str, int]]:
    """Shared pooling pass for every Family-5 metric below -- one scan of
    `runs` rather than nine. Returns `(totals, outcome_census)`.
    `outcome_census`'s five keys always exist (`0` if a cell never
    occurred), matching step 1's own census shape so a report reader can
    diff the two directly without a `KeyError`. `totals` is `dict[str,
    float]` (not `int`) because B4's five ch3 additions below include
    `channel3_abs_shade_sum`, a float sum -- every other key is still an
    exact integer count, just held in the wider type."""
    totals: dict[str, float] = {
        "packages_evaluated": 0,
        "turns_evaluated": 0,
        "evaluations_performed": 0,
        "counter_opportunities": 0,
        "counters_admitted": 0,
        "counter_closed_executions": 0,
        "expired_stalled_count": 0,
        "negotiation_depth_violations": 0,
        # B4 fix (critic round) -- M28 ch3's four branch counters plus
        # `mean_abs_shade`'s two-part numerator/denominator (§9.2), pooled
        # here exactly like every other Family-5 total, replacing the "no
        # harness plumbing exists at all" gap the critic found (`channel3_
        # *` previously lived only inside `gm_service.py`, consumed by
        # nothing in `season.py`/`metrics.py`).
        "channel3_decision_sites": 0,
        "channel3_reachability": 0,
        "channel3_flips": 0,
        "channel3_abs_shade_sum": 0.0,
        "channel3_abs_shade_row_count": 0,
        # §11 step 13 -- M28 ch2's three branch counters (Decision P3-28's
        # replacement, §8.3), pooled exactly like channel 3's own three.
        "channel2_decision_sites": 0,
        "channel2_reachability": 0,
        "channel2_flips": 0,
        # §11 step 17 -- M28 ch1's SIX branch counters (Decision P3-36/
        # P3-38/P3-40): channel 1 has two branches (`up`/`down`), each
        # carrying its own decision-site/reachability/flip trio, pooled
        # exactly like channels 2/3's own three each.
        "channel1_up_decision_sites": 0,
        "channel1_up_reachability": 0,
        "channel1_up_flips": 0,
        "channel1_down_decision_sites": 0,
        "channel1_down_reachability": 0,
        "channel1_down_flips": 0,
        # §11 step 17 item 2 (Decision P3-41) -- the four `max_observed`
        # diagnostics (pooled with `max`, NOT `+=`, below -- the one place
        # this function's pooling differs from every other counter here)
        # plus the triage's own two counters (`far_rejects_observed`
        # pools with `+=` like every other integer counter; `m18_trades_
        # executed` is computed directly from `trades_by_window`, the
        # identical expression `compute_m18` already uses, deliberately
        # NOT derived from any head-to-head field so a broken h2h writer
        # cannot zero it either).
        "channel3_max_observed": 0.0,
        "channel2_max_observed": 0.0,
        "channel1_up_max_observed": 0.0,
        "channel1_down_max_observed": 0.0,
        "far_rejects_observed": 0,
        "m18_trades_executed": 0,
    }
    census = {cell: 0 for cell in _OUTCOME_CENSUS_CELLS}
    for run in runs:
        for season in run.seasons:
            totals["packages_evaluated"] += sum(season.packages_evaluated)
            totals["turns_evaluated"] += sum(season.turns_evaluated)
            totals["evaluations_performed"] += sum(season.evaluations_performed)
            totals["counter_opportunities"] += sum(season.counter_opportunities)
            totals["counters_admitted"] += sum(season.counters_admitted)
            totals["counter_closed_executions"] += sum(season.counter_closed_executions)
            totals["expired_stalled_count"] += sum(season.expired_stalled_count)
            totals["negotiation_depth_violations"] += sum(season.negotiation_depth_violations)
            totals["channel3_decision_sites"] += sum(season.channel3_decision_sites)
            totals["channel3_reachability"] += sum(season.channel3_reachability)
            totals["channel3_flips"] += sum(season.channel3_flips)
            totals["channel3_abs_shade_sum"] += sum(season.channel3_abs_shade_sum)
            totals["channel3_abs_shade_row_count"] += sum(season.channel3_abs_shade_row_count)
            totals["channel2_decision_sites"] += sum(season.channel2_decision_sites)
            totals["channel2_reachability"] += sum(season.channel2_reachability)
            totals["channel2_flips"] += sum(season.channel2_flips)
            totals["channel1_up_decision_sites"] += sum(season.channel1_up_decision_sites)
            totals["channel1_up_reachability"] += sum(season.channel1_up_reachability)
            totals["channel1_up_flips"] += sum(season.channel1_up_flips)
            totals["channel1_down_decision_sites"] += sum(season.channel1_down_decision_sites)
            totals["channel1_down_reachability"] += sum(season.channel1_down_reachability)
            totals["channel1_down_flips"] += sum(season.channel1_down_flips)
            # `max`, never `+=` -- a running maximum, not a sum. Seeding
            # the `max(...)` call with the running total itself (never a
            # bare `season.*_max_observed`) means an old, pre-this-step
            # pickle's `()` default (Decision P3-41's own pickle-compat
            # convention) contributes nothing rather than raising
            # `ValueError` on `max()` of an empty sequence.
            totals["channel3_max_observed"] = max(
                (totals["channel3_max_observed"], *season.channel3_max_observed)
            )
            totals["channel2_max_observed"] = max(
                (totals["channel2_max_observed"], *season.channel2_max_observed)
            )
            totals["channel1_up_max_observed"] = max(
                (totals["channel1_up_max_observed"], *season.channel1_up_max_observed)
            )
            totals["channel1_down_max_observed"] = max(
                (totals["channel1_down_max_observed"], *season.channel1_down_max_observed)
            )
            totals["far_rejects_observed"] += sum(season.far_rejects_observed)
            totals["m18_trades_executed"] += sum(len(execs) for execs in season.trades_by_window)
            for window_census in season.outcome_census:
                for cell, count in window_census.items():
                    census[cell] = census.get(cell, 0) + count
    return totals, census


_PhraseTotals = tuple[int, int, dict[str, int], dict[str, int], dict[str, dict[str, int]]]


def _pool_phrase_totals(runs: tuple[RunRecord, ...]) -> _PhraseTotals:
    """§11 step 16 -- M26/M27's own pooling pass, kept SEPARATE from
    `_pool_negotiation_totals` above rather than folded into it: that
    function's own return shape is one flat `dict[str, float]` (plus the
    outcome census), and this population mixes two flat scalar totals
    with TWO levels of nested-`Mapping` merging (`phrase_selections_by_
    situation` is one level deep, `phrase_selections_by_philosophy_
    template` is two) -- extending the flat dict to carry nested values
    too would make every OTHER caller's `totals["x"] += ...` read less
    obviously safe. Same merge technique throughout, just applied one
    level deeper for the last field: `dict.get(key, 0) + count`, the
    identical technique `_pool_negotiation_totals`'s own `outcome_census`
    merge (immediately above) already uses.

    Returns `(selections_total, fallback_selections, selections_by_
    situation, fallback_by_situation, selections_by_philosophy_
    template)`. An old, pre-step-16 `SeasonRecord` pickle's `()` defaults
    for all five underlying fields make every one of these no-op to
    `0`/`{}` (`sum(())`/iterating `()` both no-op), giving `compute_m26`/
    `compute_m27` the same `total_selections == 0`-shaped `skip_reason`
    reading Decision P3-37 already requires of them."""
    selections_total = 0
    fallback_selections = 0
    by_situation: dict[str, int] = {}
    fallback_by_situation: dict[str, int] = {}
    by_philosophy_template: dict[str, dict[str, int]] = {}
    for run in runs:
        for season in run.seasons:
            selections_total += sum(season.phrase_selections_total)
            fallback_selections += sum(season.phrase_fallback_selections)
            for window_by_situation in season.phrase_selections_by_situation:
                for situation, count in window_by_situation.items():
                    by_situation[situation] = by_situation.get(situation, 0) + count
            for window_fallback_by_situation in season.phrase_fallback_by_situation:
                for situation, count in window_fallback_by_situation.items():
                    fallback_by_situation[situation] = (
                        fallback_by_situation.get(situation, 0) + count
                    )
            for window_by_philosophy in season.phrase_selections_by_philosophy_template:
                for philosophy, by_template in window_by_philosophy.items():
                    target = by_philosophy_template.setdefault(philosophy, {})
                    for template_id, count in by_template.items():
                        target[template_id] = target.get(template_id, 0) + count
    return selections_total, fallback_selections, by_situation, fallback_by_situation, (
        by_philosophy_template
    )


def _template_distribution_tv(
    counts_a: Mapping[str, int], counts_b: Mapping[str, int],
) -> tuple[float | None, int, int]:
    """The total-variation-distance computation §9.1/Decision P3-31
    define for M27, factored out to its two raw template-id -> count
    distributions (Decision P3-49's own refactor) so `_voice_
    differentiation_tv` (the star_maxer-vs-egalitarian reading) and
    `_within_cohort_binomial_thin_tv` (the SAME cohort's own two binomial-
    thinned halves, below) share exactly one implementation -- never a
    hand-reimplemented twin that could silently drift from the real one.

    `TV = 0.5 * sum(|p_a[t] - p_b[t]| for t in every template id EITHER
    side ever selected)` -- a template selected by only one side
    contributes its full share, the standard TV-distance definition.
    Returns `(tv, total_a, total_b)`; `tv is None` iff either total is `0`
    (the caller's own SKIP guard)."""
    total_a = sum(counts_a.values())
    total_b = sum(counts_b.values())
    if total_a == 0 or total_b == 0:
        return None, total_a, total_b
    templates = set(counts_a) | set(counts_b)
    tv = 0.5 * sum(
        abs(counts_a.get(t, 0) / total_a - counts_b.get(t, 0) / total_b) for t in templates
    )
    return tv, total_a, total_b


def _voice_differentiation_tv(
    by_philosophy_template: Mapping[str, Mapping[str, int]],
) -> tuple[float | None, int, int]:
    """M27's own star_maxer-vs-egalitarian reading, factored out so
    `compute_m27` (the real, counted, banded metric) and `tools.
    validate_gm`'s own control-arm pooling share EXACTLY one
    implementation -- called TWICE as of Decision P3-49 (once on the live
    corpus's pooled totals, once on the philosophies-disabled control
    arm's), never a hand-reimplemented twin that could silently drift from
    the real one. No band/skip-reason machinery here (that lives in
    `compute_m27` and in `tools.validate_gm`'s own escalation check).

    Returns `(tv, star_maxer_total, egalitarian_total)`; `tv is None` iff
    either total is `0` (the caller's own SKIP guard). See `_template_
    distribution_tv`, above, for the exact TV-distance definition this
    delegates to."""
    star = by_philosophy_template.get(GmPhilosophy.STAR_MAXER.value, {})
    egal = by_philosophy_template.get(GmPhilosophy.EGALITARIAN.value, {})
    return _template_distribution_tv(star, egal)


def _within_cohort_binomial_thin_tv(
    by_template: Mapping[str, int], rng: random.Random,
) -> tuple[float | None, int, int]:
    """Decision P3-49's own genuine noise-only null, corrected (§11 Stage D
    step 16's second fix round): a parametric-bootstrap split-half A/A test
    over ONE cohort's own POOLED per-template selection counts (`by_
    template`, the identical `template_id -> count` shape `_pool_phrase_
    totals`'s `by_philosophy_template[philosophy]` already produces, and
    `_voice_differentiation_tv` already pools across for the cross-cohort
    statistic) -- team id is never read here at all.

    **Replaces `_within_cohort_split_half_tv` (withdrawn), which split a
    cohort's own team ids into two groups instead.** Measured on two real
    corpora that null read `0.136-0.207` -- 2-3x over the `0.0625`
    escalation threshold it is supposed to clear. Root cause: at only 5
    teams per philosophy cohort, a team-id split concentrates whichever
    team has an atypical negotiation-outcome mix (cap position,
    competitive window, and roster construction all drive how often a
    team's negotiations reach `reject_too_far`/`reject_illegal` vs.
    terminate at `open`/`accept`) entirely into one half of the split --
    real cross-team heterogeneity in SITUATION MIX, not sampling noise,
    which dominates at `N=5`. §9.1's own stated null assumption ("same
    philosophy, same situation mix, so any TV distance is pure sampling
    noise") is false of a team-level split specifically, because situation
    mix is not held constant across a cohort's five teams.

    The fix: for every template with pooled count `c`, draw `Binomial(c,
    0.5)` (`rng.binomialvariate(c, 0.5)`) independently and assign that
    many of the `c` selections to half A, the remainder to half B. Both
    halves are draws from the SAME underlying cohort-pooled distribution
    by construction, so any TV between them is exactly the sampling noise
    §9.1's own analytic null (`sqrt((K-1)/(pi*n))`) already derives a
    closed form for -- this is literally a parametric bootstrap of "two
    independent samples from one shared distribution," which is precisely
    what that analytic derivation assumes. Team identity plays no role in
    it, matching what the analytic null actually models (it never
    conditions on which team made a selection).

    `rng` must be a `random.Random` seeded distinctly per cohort by the
    caller (`tools.validate_gm`'s own `rng_for(rng_seed, "validate_gm",
    "m27_within_cohort_split", <cohort>)` -- `dba2.core.rng`'s own salted-
    derivation convention, already used elsewhere in that module, reused
    here rather than a bare `random.Random()`) so the reported null is
    reproducible run to run, and so `star_maxer`'s and `egalitarian`'s own
    splits use independent randomness.

    Returns `(tv, first_half_total, second_half_total)`; `tv is None` iff
    either half's own total is `0` (a cohort with zero selections in this
    arm, or -- possible but unlikely at real event counts -- every single
    coin flip landing the same way) -- the caller's own "no reading"
    guard, identical in shape to `_voice_differentiation_tv`'s."""
    first_counts: dict[str, int] = {}
    second_counts: dict[str, int] = {}
    for template_id, count in by_template.items():
        if count <= 0:
            continue
        heads = rng.binomialvariate(count, 0.5)
        if heads:
            first_counts[template_id] = heads
        tails = count - heads
        if tails:
            second_counts[template_id] = tails
    return _template_distribution_tv(first_counts, second_counts)


def compute_m23(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`counter_open_rate` -- `counter_opportunities / packages_evaluated`,
    pooled. **`counted_in_go: false` PERMANENTLY** (Decision P3-43/P3-32 --
    see this metric's own `rationale` in `seeds/gm_validation_bands.json`
    for why no honest band width can make it pass). Reported here beside
    the live five-cell outcome census and the live `EXPIRED:stalled`
    count -- §11 step 7's own verify clause asks for both, and M23's row
    is already the phase's designated "reported diagnostic" home for `c`
    and its denominator, so they ride here rather than opening a metric-
    less report section."""
    totals, census = _pool_negotiation_totals(runs)
    if totals["packages_evaluated"] == 0:
        return MetricValue(value=None, detail={}, skip_reason="no packages evaluated in `runs`")
    return MetricValue(
        value=totals["counter_opportunities"] / totals["packages_evaluated"],
        detail={
            "counter_opportunities": totals["counter_opportunities"],
            "packages_evaluated": totals["packages_evaluated"],
            "outcome_census": census,
            "expired_stalled_count": totals["expired_stalled_count"],
        },
    )


def compute_m23p(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`counter_construction_hit_rate` -- `counters_admitted /
    counter_opportunities`, pooled. Frozen band `[0.18, 0.24]` (§9.3, step
    1d's measured `r = 0.2040`, `SE_3 = 0.0080166`). **SKIPs when zero
    counter opportunities open** (Decision P3-37's guard -- its reading on
    `m14_seeds_v2`-shaped, counters-off input)."""
    totals, _census = _pool_negotiation_totals(runs)
    if totals["counter_opportunities"] == 0:
        return MetricValue(
            value=None, detail={}, skip_reason="no counter opportunities opened in `runs`"
        )
    return MetricValue(
        value=totals["counters_admitted"] / totals["counter_opportunities"],
        detail={
            "counters_admitted": totals["counters_admitted"],
            "counter_opportunities": totals["counter_opportunities"],
        },
    )


def compute_m24(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`counter_closed_executions` -- count of EXECUTED trades whose
    negotiation closed at `round_index >= 1`, pooled. Existence gate
    (`>= 1`, Decision P3-33) -- no `skip_reason`: `0` is a real, gate-
    worthy value here, not an absent-mechanism state (a corpus with real
    trade windows always has an opportunity to close a counter, whether or
    not one closes)."""
    totals, _census = _pool_negotiation_totals(runs)
    return MetricValue(
        value=float(totals["counter_closed_executions"]),
        detail={"counter_closed_executions": totals["counter_closed_executions"]},
    )


def compute_m25(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`negotiation_depth_violations` -- Family-1-grade hard invariant
    (band `0`): negotiations exceeding the round cap or ending non-
    terminal, pooled from `TradeWindowResult`'s own live check (§11 step
    7's own build report: computed at the moment each `Negotiation` is
    built, since no per-turn log is persisted past that call -- see
    `gm_service.py::_run_negotiation`'s own docstring). Passes vacuously
    on a counters-off corpus, which is why M23' exists as this family's
    presence gate instead."""
    totals, _census = _pool_negotiation_totals(runs)
    return MetricValue(
        value=float(totals["negotiation_depth_violations"]),
        detail={"packages_evaluated": totals["packages_evaluated"]},
    )


def compute_m26(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`phrase_fallback_share` -- fallback-template selections / total
    selections, pooled across every real `_select_negotiation_phrase`
    call (§11 step 16, Decision P3-19). **SKIPs when `total_selections ==
    0`** (Decision P3-37's guard) -- dialogue off (`phrase_library=None`)
    or no corpus at all, which is every reading before step 16's own
    dialogue-enabled `validate_gm.py` run (`m14_seeds_v2`, steps 7/9/12/
    13/14 per §9.2.1's own table). `counted_in_go: true` as of step 16.

    `detail["per_situation"]` is M26's own required per-situation
    breakdown (§11 step 16's verify clause) -- every situation token that
    had at least one selection, with its own `selections`/`fallback_
    selections`/`fallback_share`, so a report reader can see e.g.
    `reject_illegal`'s own share directly rather than only the pooled
    figure (§6.2's own "this one situation alone can consume most of the
    budget" concern)."""
    selections_total, fallback_selections, by_situation, fallback_by_situation, _by_phil = (
        _pool_phrase_totals(runs)
    )
    if selections_total == 0:
        return MetricValue(
            value=None, detail={},
            skip_reason="no phrase selections in `runs` (dialogue off or no corpus)",
        )
    per_situation: dict[str, dict[str, object]] = {}
    for situation, total in sorted(by_situation.items()):
        fallback = fallback_by_situation.get(situation, 0)
        per_situation[situation] = {
            "selections": total,
            "fallback_selections": fallback,
            "fallback_share": fallback / total if total > 0 else None,
        }
    return MetricValue(
        value=fallback_selections / selections_total,
        detail={
            "selections_total": selections_total,
            "fallback_selections": fallback_selections,
            "per_situation": per_situation,
        },
    )


def compute_m27(
    runs: tuple[RunRecord, ...], bands: BandSpec, *,
    control_by_philosophy_template: Mapping[str, Mapping[str, int]] | None = None,
) -> MetricValue:
    """`voice_differentiation` -- **Decision P3-49: now a PAIRED
    DIFFERENCE, `tv_live - tv_control`, never the bare `tv_live` this
    function measured before this fix.** `tv_live` is the real total-
    variation distance between `star_maxer`'s and `egalitarian`'s own
    template-id selection distributions on the LIVE library's pooled
    corpus (unchanged statistic, `_voice_differentiation_tv`); `tv_
    control` is the identical statistic on the SAME real corpus's
    negotiation trajectory but the `with_philosophies_disabled` library
    variant (`tools.validate_gm`'s own control arm, byte-identical frames
    per Decision P3-20's independent phrase-RNG-salt guarantee -- see this
    module's own dispatch/§9.1 for the full derivation of why the bare
    `tv_live` reading was wrong: the `philosophies` allow-list is not the
    only philosophy-correlated channel into template selection, trigger
    conditions are a second one, and `tv_control` isolates exactly that
    second channel's own contribution so subtracting it leaves only the
    allow-list's).

    `band_low = 0.125` (unchanged, Decision P3-31's own derivation) now
    applies correctly to this DIFFERENCE -- it was always and only a
    derivation of the allow-list channel's own `exclusive_mass_share`
    structural guarantee, never of `tv_live` alone.

    **The control arm is now NON-OPTIONAL whenever M27 is counted.**
    `control_by_philosophy_template is None` (`--no-control-check`) is a
    hard SKIP -- computing an unpaired `tv_live` would silently resurrect
    the exact defect this fix exists to close -- mirroring `compute_m28`'s
    own `control_totals is None` precedent (Decision P3-48, item 5)
    exactly. Also SKIPs when EITHER cohort's own total selection count is
    `0` in EITHER arm (Decision P3-37's guard, now checked twice).
    `counted_in_go: true`."""
    if control_by_philosophy_template is None:
        return MetricValue(
            value=None, detail={},
            skip_reason="--no-control-check (M27 is a paired difference against its own "
            "philosophies-disabled control arm as of Decision P3-49; it cannot compute an "
            "unpaired value, mirroring compute_m28's identical control_totals is None rule)",
        )
    _total, _fallback, _by_situation, _fallback_by_situation, by_philosophy_template = (
        _pool_phrase_totals(runs)
    )
    tv_live, live_star_total, live_egal_total = _voice_differentiation_tv(by_philosophy_template)
    tv_control, control_star_total, control_egal_total = _voice_differentiation_tv(
        control_by_philosophy_template
    )
    if tv_live is None or tv_control is None:
        return MetricValue(
            value=None,
            detail={
                "star_maxer_selections_live": live_star_total,
                "egalitarian_selections_live": live_egal_total,
                "star_maxer_selections_control": control_star_total,
                "egalitarian_selections_control": control_egal_total,
            },
            skip_reason="star_maxer or egalitarian cohort has zero phrase selections in the "
            "live or the control arm",
        )
    return MetricValue(
        value=tv_live - tv_control,
        detail={
            "tv_live": tv_live,
            "tv_control": tv_control,
            "m27_paired_difference": tv_live - tv_control,
            "star_maxer_selections_live": live_star_total,
            "egalitarian_selections_live": live_egal_total,
            "star_maxer_selections_control": control_star_total,
            "egalitarian_selections_control": control_egal_total,
            "template_count_live": len(
                set(by_philosophy_template.get(GmPhilosophy.STAR_MAXER.value, {}))
                | set(by_philosophy_template.get(GmPhilosophy.EGALITARIAN.value, {}))
            ),
            "template_count_control": len(
                set(control_by_philosophy_template.get(GmPhilosophy.STAR_MAXER.value, {}))
                | set(control_by_philosophy_template.get(GmPhilosophy.EGALITARIAN.value, {}))
            ),
        },
    )


def compute_m28(
    runs: tuple[RunRecord, ...], bands: BandSpec, *, control_totals: dict[str, float] | None = None
) -> MetricValue:
    """`adaptation_decision_flips` -- **§11 step 17: channel 1 (both
    branches) is now built, so this is a real, counted gate** (Decision
    P3-36 + P3-38 + P3-40), not a permanent SKIP. `SKIP` is reachable two
    ways: the structural sense Decision P3-37 always meant -- a corpus
    with zero decision sites on ANY of the four branch counters (channel
    1's two, channel 2's one, channel 3's one), which should not occur on
    any real trade-window run from this step forward, since every ordered
    pair/window is a channel-1 decision site regardless of whether a
    negotiation ever opens -- and, as of item 5, `control_totals is None`
    (`--no-control-check`), mirroring `compute_m29`'s own identical rule.

    **`value` is `min(ch3_flips, ch2_flips, ch1_up_flips + ch1_down_
    flips)` -- `min` over the THREE CHANNELS, channel 1's own count being
    its two branches SUMMED, not a 4-way `min` over four independent
    branch counts.** This corrects a defect a critic found in this
    function's own prior build: §9.2 line ~2126 states the band as "`min`
    over the three channels of that channel's flip count (channel 1 = up
    + down)" and line ~2128 says outright that "`min` alone cannot reach"
    inside channel 1 -- the whole reason Decision P3-38's separate,
    reachability-conditional per-branch conjunct (below) exists as its
    OWN mechanism. `seeds/gm_validation_bands.json`'s own M28 `statistic`
    field (line ~780) says the identical thing verbatim: "channel 1's
    count is its up plus down branch counts, carried separately" --
    "carried separately" describes `ch1_up_flips`/`ch1_down_flips` staying
    two distinct fields in `detail` (so a reader can always see either
    branch's own zero), never a second, per-branch `min` argument. A
    4-way `min` (this function's prior build) double-counts what the
    reachability conjunct already exists to catch: it would let a branch
    that is reachable-and-genuinely-zero (a real wiring bug, caught
    correctly by the conjunct's `flips >= 1 OR reachability == 0` clause)
    ALSO independently zero out `value` even when the OTHER branch is
    firing in the hundreds -- worse, it can zero `value` on the exact
    "threshold unreachable at this event rate" case §11 step 17 names as
    predicted-legitimate (`ch1_down_flips = 0` at `reachability = 0`),
    turning an honest, correctly-passing conjunct outcome into a failed
    `min`. Summing the two branches before the `min` is what "carried
    separately" from the conjunct's own reachability gate actually means:
    `value`'s job is "did channel 1 do SOMETHING" (a magnitude-agnostic
    existence check, matching ch2/ch3's own single-branch entries in the
    same `min`), and the per-branch conjunct's job is "did EACH channel-1
    branch clear its own bar" -- two different questions, and the 4-way
    `min` conflated them by making `value` answer the conjunct's question
    too.

    **Safety property: this is `>=` the old (4-way-`min`) formula at
    every input, so this fix can only turn a FAIL into a PASS, never the
    reverse.** `ch1_up_flips + ch1_down_flips >= min(ch1_up_flips,
    ch1_down_flips)` always holds (summing two non-negative counts cannot
    be smaller than the smaller of the two), so the third argument to
    `min` can only rise, never fall, and `min` of the other two arguments
    is unchanged -- no run that correctly failed under the old formula
    can newly fail under this one; the only possible change is FAIL ->
    PASS.

    **The reachability-conditional conjunct (Decision P3-38), one clause
    per branch: `flips >= 1 OR reachability == 0`.** A branch that was
    never reachable (its own predicate never held, or held only where a
    higher-precedence branch's did too) reads `reachability == 0` and
    passes on the escape clause -- "not exercised, threshold unreachable
    at this event rate," never a gate failure and never repaired by
    moving the threshold (§9.4 item 1). A branch that WAS reachable and
    still flipped nothing fails this conjunct -- the exact B5-3 wiring-bug
    signature this decision exists to catch.

    **The control-pass HALF OF `extra_pass` IS NOW GATED (item 5 of this
    fix round) -- ANDed into `extra_pass` alongside the reachability
    conjunct above, never replacing it.** Prior builds of this function
    reported the control-pass reading in `detail` only and hardcoded
    `control_pass_gates_extra_pass = False` -- §9.2's own band text (line
    ~2126) states the conjunct's first half plainly ("on the control
    pass, every channel that has decision sites there reads exactly 0
    flips"), and that half was never actually wired as a gate until now.
    §9.2's own three-channel control-pass table claims each channel's
    M29-control-pass zero is either "real" (channels 1 and 3) or
    "vacuous" (channel 2), on the premise that the control pass runs at
    `head_to_head = None`. **That premise was false of the control run as
    originally wired at this step's build**, discovered directly against
    the committed Decision P3-29(b) baseline seed: `validate_gm.py`'s own
    `run_seed(..., max_negotiation_rounds=0)` call left `state.head_to_
    head` accumulating real `opened`/`accepts_by_opponent`/`far_reject_
    streak`/`executions` data across that run's own multiple seasons
    regardless of `max_negotiation_rounds` (`_record_negotiation_in_h2h`'s
    writer has been unconditional since step 11), and `build_ctx` handed
    that real, nonzero memory straight to every reader -- so `shade`
    (channels 2/3) and the target-count ladder (channel 1) all saw real
    memory by the run's later seasons, not the `head_to_head = None`
    no-op the table assumed. **Measured, as the contaminated figures this
    gap produced, and kept here as labeled historical evidence for why
    Decision P3-48's fix exists:** that pre-fix control run produced
    `channel1_down_flips = 130`, `channel1_up_flips = 47`, and -- the
    finding that widened the gap beyond channel 1 -- `channel3_flips = 20`
    (not `0`) over `10,655` real sites. Channel 3's nonzero reading did
    NOT mean its counterfactual was wrong or that the control pass's
    actual OUTPUT differed from pre-Phase-3 behavior -- `respond()`'s own
    `rounds_remaining <= 0` branch still short-circuits BEFORE ever
    consulting the substituted (shaded) record, so the turn's real stance
    was unaffected -- it meant the FLIP COUNTER itself (a counterfactual
    `gap` vs. two bands, computed independently of what `respond()`
    actually reads) was not shielded by that short-circuit, and genuinely
    differed once real accumulated shade pushed `gap` across the `band_
    base`/`band_eff` boundary.

    **Decision P3-48 makes the premise true, and item 5 (this fix round)
    is what finally makes this metric's own behavior match it.**
    `validate_gm.py`'s control-check block calls `run_seed(...,
    max_negotiation_rounds=0, head_to_head_enabled=False)` -- a reader-
    side-only suppression flag threaded `run_seed -> run_season ->
    build_ctx`, consumed at context-construction time. `state.head_to_
    head` still accumulates real data across the control run exactly as
    before (the WRITE path is unconditional and untouched); what changed
    is that every `build_ctx` call in a suppressed run now hands back
    `head_to_head=None` regardless, so channels 1 and 3 read the real,
    non-vacuous zero §9.2's table always predicted, and channel 2 reads
    its already-documented vacuous zero (zero decision sites, `build_
    counter` never runs at round 0). **`extra_pass` now ANDs this
    reading in** (`control_conjunct_pass`, computed below), per branch:
    ch3/ch1_up/ch1_down each require a REAL zero (`sites > 0 and flips ==
    0`); ch2's own clause is `sites == 0 or flips == 0` (its vacuous zero
    is the expected, correct reading here, per §9.2's own carve-out, so
    it is not held to the same `sites > 0` bar). This function's own
    reachability-conditional conjunct (Decision P3-38, below) is ANDed
    alongside it, never replaced by it -- both halves of §9.2's own
    `extra_pass` text are now real gates.

    **Every channel's real, non-vacuous `head_to_head = None` zero is
    already established by a DEDICATED unit test, never the M29 corpus
    run -- and this was already true for channels 2/3 before this step:**
    `tests/unit/tools/gm_harness/test_season.py::
    test_channel3_head_to_head_none_is_a_strict_no_op_on_the_flip_counter`
    and its channel-2 sibling near the same file's line ~2065 both
    already replay a real, memory-aged snapshot with `ctx.head_to_head`
    forced to `None` and assert `flips == 0` over a positive site count --
    neither ever relied on the M29 control run for this. This step adds
    the identical construction for channel 1 (`tests/unit/services/
    test_head_to_head_writer.py::
    test_channel1_real_non_vacuous_zero_at_head_to_head_none`). `control_
    totals` (from `validate_gm.py`'s own pooled totals over the SAME
    control run M29 already builds -- the identical `determinism_runs`/
    `control_mismatches` exception this module's own "every `compute_mN`
    takes `runs` and `bands` and nothing else" rule already discloses) is
    threaded through, reported in `detail`, and -- as of item 5 -- ANDed
    into `extra_pass` via `control_conjunct_pass`. `control_totals is
    None` (`--no-control-check`) is a hard `SKIP`, mirroring `compute_
    m29`'s own identical rule on the identical class of input, never a
    silent partial pass on half the conjunct.

    **This same finding is also why `run_seed(..., max_negotiation_
    rounds=0)` alone (no memory suppression) stopped reproducing the
    committed `seeds/gm_decision_log_digest.json` baseline byte-for-byte
    once channel 1 was built** -- a real, disclosed regression, not a bug
    in this step's own code: channel 1's decision site sits UPSTREAM of
    `respond()`'s own round-0 short-circuit (unlike channels 2/3), so its
    real memory-driven candidate-generation change (unlike channels 2/3's
    shielded counterfactual) genuinely perturbs WHICH packages get
    proposed, and therefore the digest. **Decision P3-48 fixes this too**:
    `tools/generate_gm_decision_log_digest.py`'s own `run_seed` call now
    passes `head_to_head_enabled=False` alongside `max_negotiation_
    rounds=0`, and the two previously-failing digest-reproduction tests
    (`tests/unit/tools/gm_harness/test_metrics.py`) are green again. `docs/
    design/phase-3-inter-manager-society.md` §11 step 18's "regenerate the
    control digest iff a `managers/` constant moved" rule did not
    anticipate a SERVICE-scoped wiring change perturbing it -- P3-48's
    resolution is reader-side memory suppression, never regenerating the
    committed baseline itself, which would have silently retired the
    phase's last real Phase-2 regression detector.

    **B4/B3 fixes (critic rounds, steps 12/13) preserved verbatim**:
    `channel3_flip_rate`/`channel2_flip_rate` are `flips / reachability`
    (never raw `sites`), `None` -- never a division -- when `reachability
    == 0`; `channel3_measured_inert`/`channel2_measured_inert`/`mean_abs_
    shade_saturated` are reported diagnostics, never gates. **Channel 1's
    `flip_rate` is not reported as an inertness statistic (§9.2, nit 2,
    round 8)**: under P3-40, `ch1_up.flips == ch1_up.reachability` and
    `ch1_down.flips == ch1_down.reachability` HOLD EXACTLY on a wired
    build (each branch fires at precisely the sites its own reachability
    counts), so channel 1's `flip_rate` is identically `1.0` by
    construction and carries no magnitude information -- reported here as
    an EQUALITY assertion (`channel1_up_ladder_identity_holds`/`channel1_
    down_ladder_identity_holds`) instead, per §11 step 17's own item (v):
    an inequality means the shipped ladder and the documented ladder
    disagree, a sharper and earlier signal than the reachability conjunct
    alone."""
    totals, _census = _pool_negotiation_totals(runs)
    params = bands.metrics["M28"].params
    inertness_flip_rate = params["ch3_inertness_flip_rate"]
    saturation_threshold = params["ch3_saturation_threshold"]
    ch2_inertness_flip_rate = params["ch2_inertness_flip_rate"]

    ch3_sites = totals["channel3_decision_sites"]
    ch3_reachability = totals["channel3_reachability"]
    ch3_flips = totals["channel3_flips"]
    ch3_flip_rate = ch3_flips / ch3_reachability if ch3_reachability > 0 else None
    ch3_row_count = totals["channel3_abs_shade_row_count"]
    mean_abs_shade = (
        totals["channel3_abs_shade_sum"] / ch3_row_count if ch3_row_count > 0 else None
    )

    ch2_sites = totals["channel2_decision_sites"]
    ch2_reachability = totals["channel2_reachability"]
    ch2_flips = totals["channel2_flips"]
    ch2_flip_rate = ch2_flips / ch2_reachability if ch2_reachability > 0 else None

    ch1_up_sites = totals["channel1_up_decision_sites"]
    ch1_up_reachability = totals["channel1_up_reachability"]
    ch1_up_flips = totals["channel1_up_flips"]
    ch1_down_sites = totals["channel1_down_decision_sites"]
    ch1_down_reachability = totals["channel1_down_reachability"]
    ch1_down_flips = totals["channel1_down_flips"]

    detail: dict[str, object] = {
        "channel3_decision_sites": ch3_sites,
        "channel3_reachability": ch3_reachability,
        "channel3_flips": ch3_flips,
        "channel3_flip_rate": ch3_flip_rate,
        "channel3_inertness_flip_rate_threshold": inertness_flip_rate,
        "channel3_measured_inert": (
            ch3_flip_rate is not None and ch3_flip_rate < inertness_flip_rate
        ),
        "channel3_abs_shade_row_count": ch3_row_count,
        "mean_abs_shade": mean_abs_shade,
        "mean_abs_shade_saturation_threshold": saturation_threshold,
        "mean_abs_shade_saturated": (
            mean_abs_shade is not None and mean_abs_shade > saturation_threshold
        ),
        "channel2_decision_sites": ch2_sites,
        "channel2_reachability": ch2_reachability,
        "channel2_flips": ch2_flips,
        "channel2_flip_rate": ch2_flip_rate,
        "channel2_inertness_flip_rate_threshold": ch2_inertness_flip_rate,
        "channel2_measured_inert": (
            ch2_flip_rate is not None and ch2_flip_rate < ch2_inertness_flip_rate
        ),
        "channel1_up_decision_sites": ch1_up_sites,
        "channel1_up_reachability": ch1_up_reachability,
        "channel1_up_flips": ch1_up_flips,
        "channel1_up_ladder_identity_holds": ch1_up_flips == ch1_up_reachability,
        "channel1_down_decision_sites": ch1_down_sites,
        "channel1_down_reachability": ch1_down_reachability,
        "channel1_down_flips": ch1_down_flips,
        "channel1_down_ladder_identity_holds": ch1_down_flips == ch1_down_reachability,
        # Item 1 fix -- the number `value`'s own channel-1 argument to
        # `min` is actually computed from (see this function's own
        # docstring for the full derivation), so a report reader can see
        # the exact figure the band is read against rather than having to
        # add `channel1_up_flips + channel1_down_flips` by hand.
        "channel1_combined_flips": ch1_up_flips + ch1_down_flips,
    }

    # Decision P3-41's `max_observed` diagnostics (§11 step 17 item 2) --
    # reported only, never gated by THIS function. The never-written
    # signature (`max_observed == 0.0` on a run whose census/M18 shows the
    # events that should have written it) is caught by the triage
    # assertion below, and re-asserted as a hard `RuntimeError` at
    # `validate_gm.py::_assert_m28_step17_invariants` (item 3) -- never by
    # `passed`/`extra_pass` here.
    far_rejects_observed = int(totals["far_rejects_observed"])
    m18_trades_executed = int(totals["m18_trades_executed"])
    channel1_up_max_observed = totals["channel1_up_max_observed"]
    channel1_down_max_observed = totals["channel1_down_max_observed"]
    detail["channel3_max_observed"] = totals["channel3_max_observed"]
    detail["channel2_max_observed"] = totals["channel2_max_observed"]
    detail["channel1_up_max_observed"] = channel1_up_max_observed
    detail["channel1_down_max_observed"] = channel1_down_max_observed
    detail["far_rejects_observed"] = far_rejects_observed
    detail["m18_trades_executed"] = m18_trades_executed

    # §11 step 17's own triage gate (Decision P3-41), stated as an
    # assertion rather than an instruction -- see this function's own
    # docstring for why NEITHER of these booleans is ANDed into `extra_
    # pass` here: `compute_m28` only ever REPORTS them. The actual
    # enforcement is a harness-level `RuntimeError`
    # (`validate_gm.py::_assert_m28_step17_invariants`, item 3), evaluated
    # after `write_report` has already put this run's own diagnostics on
    # disk. "The events that must write the field demonstrably occurred
    # in this run, and the field's own maximum is exactly zero" is the
    # never-written signature and nothing else produces it.
    triage_down_violation = far_rejects_observed >= 1 and channel1_down_max_observed == 0.0
    triage_up_violation = m18_trades_executed >= 1 and channel1_up_max_observed == 0.0
    detail["triage_down_violation"] = triage_down_violation
    detail["triage_up_violation"] = triage_up_violation
    detail["triage_pass"] = not (triage_down_violation or triage_up_violation)

    # Decision P3-37's guard, condition (A) -- a channel/branch with ZERO
    # decision sites means the mechanism is absent from this build, not
    # merely inert. Structurally unreachable on a real trade-window run
    # from step 17 forward (channel 1 is a decision site for every
    # ordered pair/window regardless of negotiation activity), so this
    # SKIP now only fires on a genuinely mechanism-absent corpus (e.g.
    # `m14_seeds_v2`, still asserted at step 18's "no Family-5 metric
    # carries a skip_reason at the exit gate").
    if ch3_sites == 0 or ch2_sites == 0 or ch1_up_sites == 0 or ch1_down_sites == 0:
        return MetricValue(
            value=None,
            detail=detail,
            skip_reason="one or more of the four branch counters has zero decision sites "
            "(the mechanism is absent from this corpus, Decision P3-37's condition (A))",
        )

    # Decision P3-38's reachability-conditional conjunct, one clause per
    # branch: `flips >= 1 OR reachability == 0`.
    branch_reachability_conjunct = {
        "ch3": ch3_flips >= 1 or ch3_reachability == 0,
        "ch2": ch2_flips >= 1 or ch2_reachability == 0,
        "ch1_up": ch1_up_flips >= 1 or ch1_up_reachability == 0,
        "ch1_down": ch1_down_flips >= 1 or ch1_down_reachability == 0,
    }
    reachability_conjunct_pass = all(branch_reachability_conjunct.values())
    detail["branch_reachability_conjunct"] = branch_reachability_conjunct
    detail["branch_reachability_conjunct_pass"] = reachability_conjunct_pass

    # Item 5 -- the control-pass HALF of `extra_pass` IS NOW GATED, per
    # §9.2's own two-part `extra_pass` conjunct (line ~2126: "(i) on the
    # control pass, every channel that has decision sites there reads
    # exactly 0 flips"). Prior builds of this function reported this
    # reading in `detail` and hardcoded `control_pass_gates_extra_pass`
    # to `False`, permanently -- a real, disclosed finding at the time
    # (Decision P3-48's own fix was still landing), but the design's own
    # band text was never actually implemented as a gate. It is now.
    #
    # **`control_totals is None` (e.g. `--no-control-check`) is a hard
    # SKIP, mirroring `compute_m29`'s own precedent exactly** -- a metric
    # whose band requires a control pass cannot silently render a partial
    # PASS on half its own conjunct; see `compute_m29`'s docstring for the
    # identical rule on the identical class of input.
    if control_totals is None:
        return MetricValue(
            value=None,
            detail=detail,
            skip_reason="--no-control-check (M28's extra_pass conjunct requires the M29 "
            "control pass; see compute_m29's identical rule)",
        )

    control_flips = {
        "ch3": control_totals["channel3_flips"],
        "ch2": control_totals["channel2_flips"],
        "ch1_up": control_totals["channel1_up_flips"],
        "ch1_down": control_totals["channel1_down_flips"],
    }
    control_sites = {
        "ch3": control_totals["channel3_decision_sites"],
        "ch2": control_totals["channel2_decision_sites"],
        "ch1_up": control_totals["channel1_up_decision_sites"],
        "ch1_down": control_totals["channel1_down_decision_sites"],
    }
    # §9.2's own control-pass table (line ~2136): channel 2's zero
    # reading is legitimately VACUOUS at this control shape (`build_
    # counter` never runs at `max_negotiation_rounds=0`, so `control_
    # sites["ch2"] == 0` is the expected, correct reading, not a defect)
    # -- so ch2's own conjunct clause is `sites == 0 or flips == 0`, no
    # further requirement. Channels 3/ch1-up/ch1-down are expected to
    # read REAL, positive sites there (Decision P3-48's fix, above) -- a
    # control pass that degenerated to zero sites for any of THESE three
    # must not satisfy the conjunct vacuously, so their own clause
    # additionally requires `sites > 0` (equivalently: `sites > 0 and
    # flips == 0`, since `sites == 0` would otherwise pass vacuously).
    control_branch_pass = {
        "ch3": control_sites["ch3"] > 0 and control_flips["ch3"] == 0,
        "ch2": control_sites["ch2"] == 0 or control_flips["ch2"] == 0,
        "ch1_up": control_sites["ch1_up"] > 0 and control_flips["ch1_up"] == 0,
        "ch1_down": control_sites["ch1_down"] > 0 and control_flips["ch1_down"] == 0,
    }
    control_zero_kind = {
        branch: ("real" if control_sites[branch] > 0 else "vacuous")
        for branch in control_sites
    }
    control_conjunct_pass = all(control_branch_pass.values())
    detail["control_flips"] = control_flips
    detail["control_decision_sites"] = control_sites
    detail["control_zero_kind"] = control_zero_kind
    detail["control_branch_pass"] = control_branch_pass
    detail["control_pass_gates_extra_pass"] = True
    detail["control_pass_note"] = (
        "GATED as of this fix (item 5) -- §9.2's own two-part extra_pass conjunct is now "
        "both parts, ANDed together. ch2's control-pass zero is legitimately VACUOUS "
        "(zero sites -- build_counter never runs at max_negotiation_rounds=0), so its own "
        "clause is `sites == 0 or flips == 0` with no further requirement; ch3/ch1_up/"
        "ch1_down are expected to read REAL, positive sites (Decision P3-48), so their own "
        "clause additionally requires sites > 0 -- a degenerate zero-site control pass must "
        "not satisfy those three vacuously. See this metric's own docstring for the "
        "contaminated pre-P3-48 figures this conjunct now catches as a real failure."
    )

    extra_pass = reachability_conjunct_pass and control_conjunct_pass
    detail["extra_pass"] = extra_pass

    value = float(min(ch3_flips, ch2_flips, ch1_up_flips + ch1_down_flips))
    return MetricValue(
        value=value,
        detail=detail,
        extra_pass=extra_pass,
    )


def compute_m29(control_mismatches: int | None, bands: BandSpec) -> MetricValue:
    """`control_equivalence` -- Decision P3-29(b) only (the digest
    comparison), on a control pass at `max_negotiation_rounds = 0`
    (`head_to_head`/dialogue do not exist yet, so this step's own control
    pass is exactly step 2b's own frozen baseline comparison, re-run).
    Decision P3-29(a) (`compare_exit_gate_reports`, built at step 2) is
    deferred to step 9's real 3x5 with-games gate run -- wiring it against
    this cheap 1x2 control pass was tried and produces ~70 permanent,
    regression-unrelated mismatches (config shape drift, plus the 9
    Family-5 metric ids absent from the Phase-2 baseline). See `known_gap`
    on the M29 row in `seeds/gm_validation_bands.json`. **Takes a precomputed mismatch
    count, not `(runs, bands)`** -- the same shape `compute_m6` already
    uses for `determinism_runs` (this module's own "every compute_mN takes
    runs and bands and NOTHING else" rule already discloses M6/M17/A1-A3
    as exceptions; M29 is a further, identically-shaped one). The control
    pass itself (`run_seed(..., max_negotiation_rounds=0)`) plus the
    digest comparison against the committed `seeds/gm_decision_log_
    digest.json` both require file I/O this module's own purity
    constraint forbids, so `validate_gm.py` -- which already reads
    `--bands` from disk -- does both and hands this function only the
    resulting integer. `control_mismatches is None` means the control
    pass was not run this invocation (`--no-control-check`) -> `SKIP`,
    never a silent pass."""
    if control_mismatches is None:
        return MetricValue(value=None, detail={}, skip_reason="--no-control-check")
    return MetricValue(value=float(control_mismatches), detail={})


def compute_m30(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """`evaluations_per_package` -- `evaluations_performed /
    packages_evaluated`, pooled. The cost governor (§9.3/§13.3), band
    `[2.0, 3.2]`, baseline exactly `2.0` when no negotiation ever counters."""
    totals, _census = _pool_negotiation_totals(runs)
    if totals["packages_evaluated"] == 0:
        return MetricValue(value=None, detail={}, skip_reason="no packages evaluated in `runs`")
    return MetricValue(
        value=totals["evaluations_performed"] / totals["packages_evaluated"],
        detail={
            "evaluations_performed": totals["evaluations_performed"],
            "packages_evaluated": totals["packages_evaluated"],
        },
    )


# ---------------------------------------------------------------------------
# Decision P4-35 (Phase 4 Stage C step 14) -- the agency metric family.
# Decision P4-34's own rule: both metrics below `SKIP` (never fabricate a
# reading) when `runs` carries no agency data at all -- `agency_enabled=
# False`'s own zero-mechanism corpus reads `SKIP` for both, recorded and
# disclosed (not silently passed) rather than a band derived to make it
# look like a pass.
# ---------------------------------------------------------------------------

_BOUND_OR_ABOVE_REQUEST_STATES: frozenset[str] = frozenset(
    {"unsettled", "trade_requested", "extension_demanded", "holdout"}
)
"""Every `RequestState` value except `content` -- Decision P4-35's
request-rate metric measures "any real signal above baseline
contentment" at all, not specifically a BOUND (star_leverage-gated)
request -- that narrower, GM-facing-consequence gate is Decision P4-17's
own `dba2.agency.motivation.trade_request_pressure`, a different
measurement answering a different question."""


def compute_m31(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Decision P4-35's agency request-rate metric: the pooled share of
    every (checkpoint, player) observation across `runs` whose `Request
    State` is anything other than `content`. **A presence-and-plausibility
    test, not a differentiation test** -- Decision P4-35's own stated
    null ("what share of players would cross a threshold with the
    mechanism absent is zero by construction") means this band is a
    floor, not a two-sided comparison. `SKIP`s, never fabricates a rate,
    when `runs` carries no agency checkpoint data at all (`agency_
    enabled=False` for every season, or an empty `runs` tuple)."""
    total_observations = 0
    bound_or_above = 0
    for run in runs:
        for season in run.seasons:
            for counts in season.agency_request_state_counts:
                total_observations += sum(counts.values())
                bound_or_above += sum(
                    counts.get(state, 0) for state in _BOUND_OR_ABOVE_REQUEST_STATES
                )
    if total_observations == 0:
        return MetricValue(
            value=None,
            detail={"total_observations": 0, "bound_or_above": 0},
            skip_reason=(
                "no agency checkpoint data in `runs` (agency_enabled=False for every "
                "season, or no season ever ran a checkpoint)"
            ),
        )
    return MetricValue(
        value=bound_or_above / total_observations,
        detail={"total_observations": total_observations, "bound_or_above": bound_or_above},
    )


def compute_m32(runs: tuple[RunRecord, ...], bands: BandSpec) -> MetricValue:
    """Decision P4-35's agency churn-bound metric: the maximum, over every
    player in every run's OWN LAST season snapshot (`SeasonRecord.agency_
    cumulative_transition_count_by_player` is cumulative for the WHOLE
    run, per that field's own docstring -- the last season's snapshot IS
    the whole-run total), of that player's `RequestState`-ladder
    transition count -- "the direct measurement of whether hysteresis
    works" (Decision P4-35). `SKIP`, never a fabricated `0`, when no run
    has any season at all, or no player in any run's last season ever
    transitioned -- `agency_enabled=False` and "a real run where the
    ladder simply never moved" read identically here, which is exactly
    the low-reachability shape Decisions P4-62/P4-81 already disclose for
    this mechanism at small/moderate corpus scale."""
    max_transitions: int | None = None
    for run in runs:
        if not run.seasons:
            continue
        last_season = run.seasons[-1]
        for _player_id, count in last_season.agency_cumulative_transition_count_by_player:
            if max_transitions is None or count > max_transitions:
                max_transitions = count
    if max_transitions is None:
        return MetricValue(
            value=None,
            detail={},
            skip_reason=(
                "no player in any run's last season ever transitioned the RequestState "
                "ladder (agency_enabled=False, or a real run with zero observed ladder "
                "activity)"
            ),
        )
    return MetricValue(value=float(max_transitions), detail={"max_transitions": max_transitions})


# ---------------------------------------------------------------------------
# Decision P4-94 (Stage D step 20) -- the media metric family, M33-M36.
# All four read a single precomputed `MediaCorpusTotals` (Decision P4-102/
# P4-103), the "fourth instance" `compute_all`'s own docstring discloses
# (`control_mismatches`/`control_totals`/`m27_control_by_philosophy_
# template` are the first three) -- `metrics.py` never loads a library and
# never runs `run_media_pulse_cycle`/`run_media_selection_cycle` itself,
# exactly as `compute_m26`/`compute_m27` never load `gm_phrases.json`.
# `media_corpus is None` -> every one of the four `SKIP`s (Decision P4-34's
# hard rule, §30.5's own zero-mechanism table) -- never a fabricated
# reading, and never a PASS.
# ---------------------------------------------------------------------------

_MEDIA_ZERO_MECHANISM_SKIP_REASON = (
    "no media corpus supplied (media_enabled=False, --no-media-corpus, or any run before "
    "Decision P4-95's 20a corpus exists)"
)


def _restricted_template_counts(
    by_voice_kind_template: Mapping[str, Mapping[str, Mapping[str, int]]],
    cohort: str,
    restrict_to_kinds: frozenset[str] | tuple[str, ...],
) -> dict[str, int]:
    """Pools ONE cohort's own `template_id -> count` counts across ONLY
    `restrict_to_kinds`. Factored out so `_media_voice_differentiation_tv`
    (the cross-cohort TV) and `m34_null_check_readings` (the within-cohort
    null, below) share exactly one definition of "a cohort's own
    restricted, pooled counts" -- Decision P4-108's own round-21 finding
    was that two call sites had drifted into two different answers to
    that exact question; this is the fix, not a convenience."""
    counts: dict[str, int] = {}
    by_kind = by_voice_kind_template.get(cohort, {})
    # `sorted(...)` at BOTH levels, not raw iteration order at either
    # (found while verifying Stage D step 22's own real corpus, unrelated
    # to that step's own scope; the outer sort alone -- this function's
    # first fix round -- was ITSELF incomplete, per Decision P4-118 below).
    # `restrict_to_kinds` is a `frozenset[str]` for every real caller, and
    # frozenset iteration order over strings is Python's own per-process
    # hash-randomized order (`PYTHONHASHSEED`), not a stable property of
    # the set's contents -- the outer `sorted(restrict_to_kinds)` fixes
    # that. But `by_kind.get(kind, {})` is itself an ordinary `dict`, whose
    # OWN iteration order is first-INSERTION order, not sorted -- and
    # nothing upstream guarantees insertion order is template-id order:
    # `tools.generate_media_corpus._ArmAccumulator.record_stories` builds
    # it via `setdefault`, in the order templates are FIRST DRAWN across a
    # whole corpus run, and `m34_null_check_readings`'s own in-memory
    # caller (`_report_null_check`) hands that live dict straight through
    # with no serialization step in between to coincidentally re-sort it.
    # `_media_voice_differentiation_tv`'s own use of this dict is order-
    # independent (a plain sum over the template-id union), so that caller
    # was never affected -- but `m34_null_check_readings`'s `_within_
    # cohort_binomial_thin_tv` consumes its shared `rng` stream by
    # iterating `counts` in insertion order (one `rng.binomialvariate(
    # count, 0.5)` draw per template), so a different insertion order at
    # EITHER level silently produces a different split, and therefore a
    # different `null` reading and potentially a different gate-1 PASS/
    # FAIL verdict, from the SAME seed and the SAME corpus -- directly
    # contradicting this module's own documented claim that the null-check
    # reading is "reproducible run to run" / "a reproducible property of
    # THAT corpus, checkable from the committed artifact alone" (`m34_
    # null_check_readings`'s own docstring). The committed `artifacts/
    # media_corpus.json` read this as safe only because `generate_media_
    # corpus.py` happens to write it with `sort_keys=True` -- a
    # serialization coincidence, not a guarantee, and one the in-memory
    # `_report_null_check` call path never benefits from at all. Sorting
    # both levels makes the pooling order -- and therefore the RNG
    # consumption order -- a real, process- and call-path-independent
    # property of the input data alone.
    for kind in sorted(restrict_to_kinds):
        for template_id, count in sorted(by_kind.get(kind, {}).items()):
            counts[template_id] = counts.get(template_id, 0) + count
    return counts


def _media_voice_differentiation_tv(
    by_voice_kind_template: Mapping[str, Mapping[str, Mapping[str, int]]],
    *,
    cohort_a: str,
    cohort_b: str,
    restrict_to_kinds: frozenset[str],
) -> tuple[float | None, dict[str, int], dict[str, int]]:
    """M34's own thin wrapper (Decision P4-97, `by_voice_kind_template`'s
    three-level shape named by P4-102) -- pools each cohort's own template-
    id counts across ONLY `restrict_to_kinds` (`_restricted_template_
    counts`, above), then calls the EXISTING `_template_distribution_tv`
    (never a hand-reimplemented TV twin, that function's own stated reason
    for existing). Returns `(tv, counts_a, counts_b)` -- the two
    restricted, pooled `template_id -> count` dicts are handed back too
    (not just their totals) because the null gate (`m34_null_check_
    readings`) and the template-diversity/kind-coverage gates all need to
    re-inspect them without re-deriving the restriction a second time."""
    counts_a = _restricted_template_counts(by_voice_kind_template, cohort_a, restrict_to_kinds)
    counts_b = _restricted_template_counts(by_voice_kind_template, cohort_b, restrict_to_kinds)
    tv, _total_a, _total_b = _template_distribution_tv(counts_a, counts_b)
    return tv, counts_a, counts_b


@dataclass(frozen=True)
class M34NullCheckReading:
    """One of Decision P4-108's four null-gate readings (2 cohorts x 2
    arms). `k` is the number of distinct templates with non-zero count in
    THIS reading's own restricted, pooled counts -- never `K_live` (gate
    2's cross-arm union), a different quantity measured for a different
    gate. `failed` is `True` for a `k < 2` reading (no split-half variance
    to measure -- M27's `_m27_escalation_gate` guard, ruled by P4-108 to
    be a gate FAILURE here rather than a SKIP), for a `None` null (the
    empty-half degenerate split), or for a null above `escalation_
    threshold` -- the three ways one reading can fail, each named in
    `fail_reason` so a report never has to re-derive why."""

    arm: str
    cohort: str
    k: int
    n: int
    null: float | None
    failed: bool
    fail_reason: str | None


def m34_null_check_readings(
    live_by_voice_kind_template: Mapping[str, Mapping[str, Mapping[str, int]]],
    control_by_voice_kind_template: Mapping[str, Mapping[str, Mapping[str, int]]],
    *,
    cohort_a: str,
    cohort_b: str,
    restrict_to_kinds: frozenset[str],
    seed: int,
    escalation_threshold: float,
) -> dict[str, dict[str, M34NullCheckReading]]:
    """Decision P4-108's ONE implementation of M34's gate 1, called by
    BOTH `compute_m34` (the real gate, on `media_corpus.live`/`.control`)
    AND `tools.generate_media_corpus._report_null_check` (the step-20a
    stdout disclosure, on the same run's own in-memory accumulators) --
    round 21's finding was exactly that those two call sites had each
    grown their own hand-rolled version of "the null for this arm/cohort"
    and silently disagreed about which arm to read. Neither caller
    computes anything itself now; both hand this function their own
    `by_voice_kind_template` mappings and get back the same four readings.

    Measures all four (2 cohorts x 2 arms), each on its OWN `rng_for(seed,
    "validate_gm", "m34_within_cohort_split", <arm>, <cohort>)` stream --
    the arm is part of the salt (Decision P4-108 point 2) precisely so the
    live and control readings for one cohort are independent draws, not
    two reads of the same stream. `seed` must be `media_corpus.league_
    rng_seed` in both callers (Decision P4-108 point 1) -- the media
    corpus is a single committed artifact reused unchanged across every
    gate invocation, and tying this reading to `validate_gm.py`'s own
    per-invocation `--rng-seed` would make it vary run to run for a reason
    that has nothing to do with the corpus being measured (Decision
    P4-105's original reasoning, unchanged here).

    A reading's own `k` (its restricted, pooled counts' distinct-template
    count) below `2` is a FAILURE, not a SKIP and not a silent PASS
    (Decision P4-108, extending `_m27_escalation_gate`'s identical guard
    in `tools/validate_gm.py` to a gate that, as shipped, had none) --
    the null is still computed and reported for disclosure, but the
    verdict does not depend on its value once `k < 2`."""
    readings: dict[str, dict[str, M34NullCheckReading]] = {}
    for arm, by_voice_kind_template in (
        ("live", live_by_voice_kind_template),
        ("control", control_by_voice_kind_template),
    ):
        per_cohort: dict[str, M34NullCheckReading] = {}
        for cohort in (cohort_a, cohort_b):
            counts = _restricted_template_counts(by_voice_kind_template, cohort, restrict_to_kinds)
            n = sum(counts.values())
            k = sum(1 for count in counts.values() if count > 0)
            rng = rng_for(seed, "validate_gm", "m34_within_cohort_split", arm, cohort)
            null, _first_half, _second_half = _within_cohort_binomial_thin_tv(counts, rng)
            if k < 2:
                failed, fail_reason = True, (
                    f"k={k} < 2 (no split-half variance to measure, {arm}/{cohort})"
                )
            elif null is None:
                failed, fail_reason = True, (
                    f"null reading is None (empty-half degenerate split, {arm}/{cohort})"
                )
            elif null > escalation_threshold:
                failed, fail_reason = True, (
                    f"null={null} > escalation_threshold={escalation_threshold} ({arm}/{cohort})"
                )
            else:
                failed, fail_reason = False, None
            per_cohort[cohort] = M34NullCheckReading(
                arm=arm, cohort=cohort, k=k, n=n, null=null,
                failed=failed, fail_reason=fail_reason,
            )
        readings[arm] = per_cohort
    return readings


def compute_m33(media_corpus: MediaCorpusTotals | None, bands: BandSpec) -> MetricValue:
    """`media_fallback_share` -- Decision P4-96 (amended by P4-102/P4-104,
    P4-107 item 6).
    `fallback_stories / stories_total`, pooled from the corpus's `live` arm
    own PRE-COMPUTED split (20a decides fallback-ness once, at generation
    time, against the library it holds -- this function never re-derives
    it, exactly as `compute_m26` reads `_pool_phrase_totals`'s pre-pooled
    `fallback_selections` rather than deciding fallback-ness itself).
    `comparator: max`, `band_high: 0.65`. **SKIPs when `stories_total ==
    0`**, matching `compute_m26`'s exact guard shape (dialogue-off's
    `selections_total == 0` guard, media's own equivalent).

    `detail["b_provenance"]` (Decision P4-104, its per-bin source widened
    by P4-107 item 6) is one of `"measured"` (every `by_pair_depth` bin
    carrying corpus weight has a non-zero `trigger_held_selections`
    denominator -- the GENERAL "this selection's own dedicated template's
    trigger held" reading, superseding P4-104's original `ungated_kind_
    selections`-only reading now that a real trigger-held/trigger-missed
    split exists for every kind, not only the six `ungated_kinds`),
    `"partial"` (some bins measured, some closed-form), or `"modeled"`
    (`b_modeled_weight_share == 1.0` -- the committed corpus's own state at
    P4-104's own writing, not necessarily true once real trigger-held
    volume exists on a gated kind). `detail["m_bracket"]`/`detail["m"]`
    follow that split exactly:
    a bracket (`[m_low, m_high]`, one endpoint per candidate `b`) whenever
    `b_provenance != "measured"`, collapsing to a single point `m` only
    when it is. `detail["m_lower_bound_direct"]` needs no `b` at all -- the
    share of `stories_total` in `(voice, kind)` pairs that recorded ZERO
    selections of their own dedicated template anywhere in the corpus,
    read off `by_voice_kind_template` directly (a pair's dedicated template
    id is always `f"{voice}_..."`, never `fallback_...` -- P4-96/P4-103's
    own one-dedicated-per-eligible-pair authoring convention, verified
    against the real `seeds/media_phrases.json` at dispatch time)."""
    if media_corpus is None:
        return MetricValue(value=None, detail={}, skip_reason=_MEDIA_ZERO_MECHANISM_SKIP_REASON)
    live = media_corpus.live
    if live.stories_total == 0:
        return MetricValue(
            value=None, detail={},
            skip_reason="no stories in the media corpus's live arm (media off or no corpus)",
        )

    def _split_detail(splits: Mapping[str, object]) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for key, split in sorted(splits.items()):
            out[key] = {
                "stories": split.stories,
                "fallback_stories": split.fallback_stories,
                "fallback_share": (
                    split.fallback_stories / split.stories if split.stories > 0 else None
                ),
            }
        return out

    per_kind = _split_detail(live.by_kind)
    per_voice = _split_detail(live.by_voice)
    share = live.fallback_stories / live.stories_total

    # Decision P4-103 Part B / P4-104 -- `b`, measured where the trigger-
    # miss channel is structurally absent (`ungated_kinds`, where m=0 by
    # construction), with a three-state provenance so a blended number is
    # never reported as a measurement of something it is not.
    ungated_kinds = set(media_corpus.ungated_kinds)
    ungated_stories = sum(
        split.stories for kind, split in live.by_kind.items() if kind in ungated_kinds
    )
    ungated_fallback = sum(
        split.fallback_stories for kind, split in live.by_kind.items() if kind in ungated_kinds
    )
    # The free cross-check Decision P4-103 specifies: the SAME selections
    # pooled two independent ways (`by_kind` restricted to `ungated_kinds`
    # vs. `by_pair_depth`'s own ungated columns) must agree exactly, or the
    # artifact's two poolings have drifted apart and the corpus is
    # REJECTED rather than silently read (P4-103's own standing guard
    # against a schema whose fields can drift apart).
    depth_ungated_stories = sum(b.ungated_kind_selections for b in live.by_pair_depth.values())
    depth_ungated_fallback = sum(
        b.ungated_kind_fallback_selections for b in live.by_pair_depth.values()
    )
    if ungated_stories != depth_ungated_stories or ungated_fallback != depth_ungated_fallback:
        raise ValueError(
            "media corpus rejected (Decision P4-103's free cross-check failed): `by_kind` "
            f"restricted to `ungated_kinds` reads {ungated_fallback}/{ungated_stories} "
            "fallback/stories, but `by_pair_depth`'s own ungated columns read "
            f"{depth_ungated_fallback}/{depth_ungated_stories} -- the artifact's two "
            "independent poolings of the same selections disagree."
        )

    # Decision P4-107 item 6 -- `b`'s own per-depth-bin measurement now
    # reads `trigger_held_selections`/`trigger_held_fallback_selections`
    # (the GENERAL form: "this selection's own eligible dedicated
    # template's trigger held," true vacuously for every `ungated_kinds`
    # selection and measured per-selection for a gated one), not the
    # `ungated_kind_*` columns -- a mechanical swap, same shape, same
    # per-bin all-or-nothing structure P4-103/P4-104 already used, now
    # over a strictly LARGER real population (`ungated_kind_selections` is
    # always a subset of `trigger_held_selections` in the same bin, never
    # larger -- `PairDepthBin`'s own docstring). `b_provenance` keeps its
    # existing three states and its existing meaning ("every/some/no bin
    # carrying corpus weight has real, measured data") unchanged; what
    # moved is what counts as real, measured data in a bin. The
    # `ungated_kind_*` cross-check above is intentionally untouched -- it
    # validates the OLD, narrower pooling stays internally consistent with
    # `by_kind`, a property this new field does not change.
    #
    # **Disclosed limitation, carried forward unchanged from P4-103/P4-104,
    # not introduced here:** `by_pair_depth` bins pool every `(voice,
    # kind)` pair sharing one numeric `d`, so a bin already mixed
    # trigger-held and (if any existed) trigger-missed selections before
    # this decision; a bin with `trigger_held_selections > 0` still applies
    # that subset's own `b_d` to the WHOLE bin's weight in `reweighted_sum`
    # below, including any trigger-missed selections riding along in the
    # same bin (which are 100% fallback by construction, not a `b` draw at
    # all). This is the same per-bin, all-or-nothing approximation shape
    # the original `ungated_kind_*` version already had -- a real,
    # disclosed follow-up (splitting `reweighted_sum` per selection rather
    # than per bin) would sharpen it further; it is not required to make
    # this swap a strict improvement, since every selection this version
    # measures directly was previously modeled by the closed form instead.
    total_selections = sum(bin_.selections for bin_ in live.by_pair_depth.values())
    modeled_weight = 0
    reweighted_sum = 0.0
    for d_str, bin_ in live.by_pair_depth.items():
        d = int(d_str)
        closed_form_b_d = 1.0 / (2.0 * (0.3**d) + 1.0)
        if bin_.trigger_held_selections > 0:
            b_d = bin_.trigger_held_fallback_selections / bin_.trigger_held_selections
        else:
            b_d = closed_form_b_d
            modeled_weight += bin_.selections
        reweighted_sum += bin_.selections * b_d
    b_corpus = reweighted_sum / total_selections if total_selections > 0 else None

    trigger_held_stories = sum(b.trigger_held_selections for b in live.by_pair_depth.values())
    trigger_held_fallback = sum(
        b.trigger_held_fallback_selections for b in live.by_pair_depth.values()
    )

    if total_selections == 0:
        b_provenance, b, b_modeled_weight_share = "modeled", None, None
    elif modeled_weight == total_selections:
        b_provenance, b, b_modeled_weight_share = "modeled", b_corpus, 1.0
    elif modeled_weight == 0:
        b_provenance = "measured"
        b = trigger_held_fallback / trigger_held_stories if trigger_held_stories > 0 else None
        b_modeled_weight_share = 0.0
    else:
        b_provenance = "partial"
        b = b_corpus
        b_modeled_weight_share = modeled_weight / total_selections

    m_bracket: list[float] | None = None
    m_point: float | None = None
    if b_provenance == "measured":
        m_point = (share - b) / (1 - b) if b is not None and b < 1 else None
    elif b is not None and b_corpus is not None:
        m_declared = (
            (share - media_corpus.declared_fallback_weight_share)
            / (1 - media_corpus.declared_fallback_weight_share)
        )
        m_corpus = (share - b_corpus) / (1 - b_corpus) if b_corpus < 1 else None
        candidates = [x for x in (m_declared, m_corpus) if x is not None]
        if candidates:
            m_bracket = [min(candidates), max(candidates)]

    # Decision P4-104's directly-measured lower bound on `m` -- needs no
    # `b` at all. The share of `stories_total` landing in `(voice, kind)`
    # pairs that recorded ZERO selections of their own dedicated template
    # across the whole corpus. A pair's dedicated template id always
    # starts with `f"{voice}_"` and a fallback id always starts with
    # `"fallback_"` (verified against the real `seeds/media_phrases.json`
    # at dispatch time -- P4-96/P4-103's own one-dedicated-per-eligible-
    # pair authoring convention), so "recorded zero dedicated selections"
    # is exactly "no key in this (voice, kind) bucket starts with the
    # voice's own prefix."
    zero_dedicated_stories = 0
    for voice, by_kind_templates in live.by_voice_kind_template.items():
        dedicated_prefix = f"{voice}_"
        for by_template in by_kind_templates.values():
            if not any(t.startswith(dedicated_prefix) for t in by_template):
                zero_dedicated_stories += sum(by_template.values())
    m_lower_bound_direct = zero_dedicated_stories / live.stories_total

    return MetricValue(
        value=share,
        detail={
            "stories_total": live.stories_total,
            "fallback_stories": live.fallback_stories,
            "per_kind": per_kind,
            "per_voice": per_voice,
            "declared_fallback_weight_share": media_corpus.declared_fallback_weight_share,
            "b_provenance": b_provenance,
            "b": b,
            "b_modeled_weight_share": b_modeled_weight_share,
            "m": m_point,
            "m_bracket": m_bracket,
            "m_lower_bound_direct": m_lower_bound_direct,
        },
    )


# Decision P4-105's three conjunctive `counted_in_go` gates, stated as
# named constants rather than magic numbers so a critic can check each
# against the decision's own derivation without re-deriving it. `K = 9` is
# the DESIGNED restricted-kind template union (three `analyst_*`, three
# `historian_*`, three `fallback_*`); `K_live >= 6` is "two of the three
# shared kinds fully exercised on both sides" (P4-105 gate 2's own
# derivation). These are properties of the gate's own design, never read
# from the corpus the way `top_k_per_cycle`/`salience_floor` are.
#
# **Decision P4-110 (AMENDS P4-105's gate 2) renamed `_M34_MIN_DEDICATED_
# TEMPLATES_PER_COHORT` to `_M34_MIN_DEDICATED_KINDS_PER_COHORT` -- the
# VALUE (2) is unchanged, but what it counts is not.** Counting distinct
# DEDICATED TEMPLATE IDS was a correct proxy for "two of the three shared
# kinds are fully exercised on both sides" only under §30.2's original
# one-dedicated-template-per-eligible-pair authoring convention. Decision
# P4-96's own sign-split streak remedy (three cohorts, including both
# `analyst` and `historian`, now carry TWO dedicated templates on the
# single kind `streak` alone) breaks that proxy: the real corpus measures
# `analyst: [analyst_streak_slide, analyst_streak_surge]`, both `streak` --
# satisfying the OLD `len(...) >= 2` check with ZERO real breadth across
# `_M34_RESTRICTED_KINDS`. `compute_m34`'s gate 2 now counts DISTINCT KINDS
# a cohort has a firing dedicated template in, read directly off
# `by_voice_kind_template`'s own kind-keyed structure, restoring the gate's
# original breadth intent as an explicit check rather than a count that
# happened to mean breadth before P4-96.
_M34_PRIMARY_COHORTS: tuple[str, str] = ("analyst", "historian")
_M34_RESTRICTED_KINDS: frozenset[str] = frozenset({"streak", "milestone", "rookie_watch"})
_M34_SECONDARY_PAIRS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("beat_writer", "insider", frozenset({"trade_reaction", "trade_fallout", "signing_reaction"})),
    ("hot_take", "insider", frozenset({"trade_reaction", "player_unrest", "holdout_watch"})),
)
_M34_MIN_K_LIVE = 6
_M34_MIN_DEDICATED_KINDS_PER_COHORT = 2
_M34_DEFAULT_ESCALATION_THRESHOLD = 0.0625


def compute_m34(media_corpus: MediaCorpusTotals | None, bands: BandSpec) -> MetricValue:
    """`media_voice_differentiation` -- Decision P4-97, amended by P4-105,
    amended again by P4-108.
    Paired difference `tv_live - tv_control` over the `analyst`/`historian`
    cohort pair, restricted to their three shared kinds (`streak`,
    `milestone`, `rookie_watch`), via `_media_voice_differentiation_tv` ->
    `_template_distribution_tv` (never a hand-reimplemented twin).
    `comparator: min`, `band_low: 0.25`.

    **`counted_in_go` is governed by three CONJUNCTIVE gates (Decision
    P4-105), reported via `MetricValue.counted_in_go_override` rather than
    the band's own static default:**

    1. **Null gate (Decision P4-108, AMENDING P4-105)** --
       `m34_null_check_readings` measures FOUR readings, 2 cohorts x 2
       arms (live AND control, not control alone -- P4-105's original
       control-only version certified only `tv_control`'s own bias term,
       and the live arm's per-cohort support is a strict subset of the
       control arm's at this restriction, so a live-only reading is also
       calibrated against the wrong `K`; see the design doc's §34 for
       both grounds), each seeded `rng_for(media_corpus.league_rng_seed,
       "validate_gm", "m34_within_cohort_split", <arm>, <cohort>)`.
       **Seeded from the CORPUS's own recorded `league_rng_seed`,
       deliberately never from `validate_gm.py`'s own CLI-level
       `--rng-seed`** -- unlike M27 (a real per-invocation negotiation
       re-run, seeded by that invocation's own seed), the media corpus is
       a single committed, precomputed artifact reused unchanged across
       every gate invocation; tying the null gate's own randomness to
       whatever `--rng-seed` a particular run happened to pass would make
       the reading vary run to run for a reason that has nothing to do
       with the corpus being measured. Tying it to the corpus's own seed
       instead makes the reading a reproducible property of THAT corpus,
       checkable from the committed artifact alone. **All four readings**
       must measure null <= `params.escalation_threshold` (default
       `0.0625`) to pass. A reading's own `k < 2` (fewer than 2 distinct
       templates in that reading's restricted, pooled counts -- no
       split-half variance to measure, `_m27_escalation_gate`'s identical
       guard in `tools/validate_gm.py`) is a gate **FAILURE**, not a SKIP
       and not a silent pass. A `None` reading (the split degenerated to
       an empty half, `_template_distribution_tv`'s own guard) also counts
       as a gate FAILURE, exactly like a numeric reading above threshold
       (P4-105/P4-106 item 15, corrected in round 20) -- never an
       unhandled case, never a silent pass.
    2. **Template-diversity gate** -- the LIVE arm's restricted template
       union carries `K_live >= 6` distinct template ids with non-zero
       count, AND each cohort contributes >= 2 distinct DEDICATED templates
       of its own (`f"{cohort}_..."`, never `"fallback_..."'`).
    3. **Kind-coverage gate** -- each of the three restricted kinds records
       >= 1 live selection in EACH cohort.

    While any gate fails, M34 reports its real computed paired difference
    **uncounted** (`counted_in_go_override=False`), never `SKIP` -- a
    `SKIP` here is indistinguishable from the media-off `SKIP` (register
    item 12), which would destroy real information (P4-105's own explicit
    rule). `detail["counted_in_go_blockers"]` names every failing gate.

    **Hard `SKIP`, never computed, in exactly two cases (both unchanged
    from Decision P4-97/§30.5, both distinct from the gates above):**
    `media_corpus.live.control is None` (an unpaired `tv_live` would
    silently resurrect the exact contamination the paired form exists to
    remove -- the identical rule as `compute_m27`'s `control_by_philosophy_
    template is None` branch), or either cohort reads zero restricted-kind
    selections in either arm (`_template_distribution_tv` returns `None`).

    `detail["secondary_pairs"]` reports both other 3-kind cohort pairs
    (`beat_writer`/`insider`, `hot_take`/`insider`) -- real readings, always
    computed, never a substitute for the primary and never pooled with it
    (P4-97/P4-105's own "so a future round can see what the rejected
    candidate actually reads")."""
    if media_corpus is None:
        return MetricValue(value=None, detail={}, skip_reason=_MEDIA_ZERO_MECHANISM_SKIP_REASON)
    live = media_corpus.live
    control = media_corpus.control
    if control is None:
        return MetricValue(
            value=None, detail={},
            skip_reason="--no-media-corpus-control (M34 is a paired difference against its own "
            "with_voices_disabled control arm, Decision P4-97; it cannot compute an unpaired "
            "value, mirroring compute_m27's identical control-arm-is-None rule)",
        )

    cohort_a, cohort_b = _M34_PRIMARY_COHORTS
    tv_live, live_counts_a, live_counts_b = _media_voice_differentiation_tv(
        live.by_voice_kind_template, cohort_a=cohort_a, cohort_b=cohort_b,
        restrict_to_kinds=_M34_RESTRICTED_KINDS,
    )
    tv_control, control_counts_a, control_counts_b = _media_voice_differentiation_tv(
        control.by_voice_kind_template, cohort_a=cohort_a, cohort_b=cohort_b,
        restrict_to_kinds=_M34_RESTRICTED_KINDS,
    )
    if tv_live is None or tv_control is None:
        return MetricValue(
            value=None,
            detail={
                f"{cohort_a}_selections_live": sum(live_counts_a.values()),
                f"{cohort_b}_selections_live": sum(live_counts_b.values()),
                f"{cohort_a}_selections_control": sum(control_counts_a.values()),
                f"{cohort_b}_selections_control": sum(control_counts_b.values()),
            },
            skip_reason=f"{cohort_a} or {cohort_b} has zero restricted-kind selections in the "
            "live or the control arm",
        )
    paired_difference = tv_live - tv_control

    # Gate 1 -- the null gate, on BOTH arms' restricted, per-cohort pooled
    # counts (Decision P4-108, AMENDING P4-105's control-only version --
    # a control-only null certifies only `tv_control`'s own bias term, not
    # `tv_live`'s, and the live arm's per-cohort support is a strict
    # subset of the control arm's at this restriction, so a live-only null
    # would also be calibrated against the wrong K; see the design doc's
    # §34 for both grounds in full). `m34_null_check_readings` is the ONE
    # implementation shared with `tools.generate_media_corpus._report_
    # null_check`'s stdout disclosure -- neither call site computes its
    # own null (round 21's finding was exactly that they used to).
    escalation_threshold = bands.metrics["M34"].params.get(
        "escalation_threshold", _M34_DEFAULT_ESCALATION_THRESHOLD
    )
    rng_seed = media_corpus.league_rng_seed
    null_readings = m34_null_check_readings(
        live.by_voice_kind_template, control.by_voice_kind_template,
        cohort_a=cohort_a, cohort_b=cohort_b,
        restrict_to_kinds=_M34_RESTRICTED_KINDS,
        seed=rng_seed, escalation_threshold=escalation_threshold,
    )
    # Free correctness check (Decision P4-108): P4-97 ground (ii) already
    # guarantees the same persona is drawn in both arms by construction,
    # so each cohort's live `n` and control `n` must match. A mismatch
    # means the paired design itself has broken, not that a gate failed.
    for _cohort in (cohort_a, cohort_b):
        _live_n = null_readings["live"][_cohort].n
        _control_n = null_readings["control"][_cohort].n
        assert _live_n == _control_n, (
            f"M34's paired design is broken: {_cohort} has n_live={_live_n} but "
            f"n_control={_control_n} (Decision P4-97 ground (ii) requires these to match "
            "by construction, since the same persona is drawn in both arms)"
        )
    n_a = sum(control_counts_a.values())
    n_b = sum(control_counts_b.values())
    null_gate_failed = any(
        reading.failed for by_cohort in null_readings.values() for reading in by_cohort.values()
    )

    # Gate 2 -- template-diversity gate, on the LIVE arm's restricted union.
    K_live = len(set(live_counts_a) | set(live_counts_b))
    dedicated_a = sorted(t for t in live_counts_a if t.startswith(f"{cohort_a}_"))
    dedicated_b = sorted(t for t in live_counts_b if t.startswith(f"{cohort_b}_"))

    # Decision P4-110's real breadth check: how many of `_M34_RESTRICTED_
    # KINDS` this cohort has a firing DEDICATED (non-fallback) template in
    # -- never a count of distinct template ids pooled across kinds, which
    # P4-96's sign-split streak templates can satisfy from one kind alone.
    def _dedicated_kinds(cohort: str) -> set[str]:
        by_kind = live.by_voice_kind_template.get(cohort, {})
        return {
            kind
            for kind in _M34_RESTRICTED_KINDS
            if any(
                template_id.startswith(f"{cohort}_") and count > 0
                for template_id, count in by_kind.get(kind, {}).items()
            )
        }

    dedicated_kinds_a = sorted(_dedicated_kinds(cohort_a))
    dedicated_kinds_b = sorted(_dedicated_kinds(cohort_b))
    diversity_gate_failed = (
        K_live < _M34_MIN_K_LIVE
        or len(dedicated_kinds_a) < _M34_MIN_DEDICATED_KINDS_PER_COHORT
        or len(dedicated_kinds_b) < _M34_MIN_DEDICATED_KINDS_PER_COHORT
    )

    # Gate 3 -- kind-coverage gate.
    def _kind_total(cohort: str, kind: str) -> int:
        return sum(live.by_voice_kind_template.get(cohort, {}).get(kind, {}).values())

    kinds_covered = sorted(
        kind for kind in _M34_RESTRICTED_KINDS
        if _kind_total(cohort_a, kind) > 0 and _kind_total(cohort_b, kind) > 0
    )
    coverage_gate_failed = len(kinds_covered) < len(_M34_RESTRICTED_KINDS)

    blockers: list[str] = []
    if diversity_gate_failed:
        blockers.append(
            f"gate 2 (template diversity): K_live={K_live} (need >= {_M34_MIN_K_LIVE}); "
            f"{cohort_a} dedicated kinds={dedicated_kinds_a}, {cohort_b} dedicated "
            f"kinds={dedicated_kinds_b} (need >= {_M34_MIN_DEDICATED_KINDS_PER_COHORT} DISTINCT "
            "kinds each, Decision P4-110 -- distinct template ids alone no longer suffice)"
        )
    if coverage_gate_failed:
        blockers.append(
            f"gate 3 (kind coverage): {kinds_covered} present in both cohorts, "
            f"{sorted(_M34_RESTRICTED_KINDS)} required"
        )
    if null_gate_failed:
        failing_readings = [
            f"{reading.arm}/{reading.cohort} (k={reading.k}, n={reading.n}, "
            f"null={reading.null}): {reading.fail_reason}"
            for by_cohort in null_readings.values()
            for reading in by_cohort.values()
            if reading.failed
        ]
        blockers.append(
            "gate 1 (null, Decision P4-108 -- all 4 arm x cohort readings must clear "
            f"escalation_threshold={escalation_threshold}): {'; '.join(failing_readings)}"
        )
    counted = not (null_gate_failed or diversity_gate_failed or coverage_gate_failed)

    secondary_pairs: dict[str, dict[str, object]] = {}
    for voice_a, voice_b, kinds in _M34_SECONDARY_PAIRS:
        sec_tv_live, sec_live_a, sec_live_b = _media_voice_differentiation_tv(
            live.by_voice_kind_template, cohort_a=voice_a, cohort_b=voice_b,
            restrict_to_kinds=kinds,
        )
        sec_tv_control, _sec_control_a, _sec_control_b = _media_voice_differentiation_tv(
            control.by_voice_kind_template, cohort_a=voice_a, cohort_b=voice_b,
            restrict_to_kinds=kinds,
        )
        if sec_tv_live is None or sec_tv_control is None:
            secondary_pairs[f"{voice_a}_{voice_b}"] = {
                "paired_difference": None,
                f"{voice_a}_selections_live": sum(sec_live_a.values()),
                f"{voice_b}_selections_live": sum(sec_live_b.values()),
                "skip_reason": f"{voice_a} or {voice_b} has zero restricted-kind selections in "
                "the live or the control arm",
            }
        else:
            secondary_pairs[f"{voice_a}_{voice_b}"] = {
                "tv_live": sec_tv_live, "tv_control": sec_tv_control,
                "paired_difference": sec_tv_live - sec_tv_control,
            }

    return MetricValue(
        value=paired_difference,
        detail={
            "tv_live": tv_live,
            "tv_control": tv_control,
            "m34_paired_difference": paired_difference,
            f"{cohort_a}_selections_live": sum(live_counts_a.values()),
            f"{cohort_b}_selections_live": sum(live_counts_b.values()),
            f"{cohort_a}_selections_control": n_a,
            f"{cohort_b}_selections_control": n_b,
            "K_live": K_live,
            "dedicated_templates_live": {cohort_a: dedicated_a, cohort_b: dedicated_b},
            "dedicated_kinds_live": {cohort_a: dedicated_kinds_a, cohort_b: dedicated_kinds_b},
            "kinds_covered": kinds_covered,
            "null_check": {
                "escalation_threshold": escalation_threshold,
                "readings": {
                    arm: {
                        cohort: {
                            "k": reading.k,
                            "n": reading.n,
                            "null": reading.null,
                            "failed": reading.failed,
                            "fail_reason": reading.fail_reason,
                        }
                        for cohort, reading in by_cohort.items()
                    }
                    for arm, by_cohort in null_readings.items()
                },
            },
            "counted_in_go_blockers": blockers,
            "secondary_pairs": secondary_pairs,
        },
        counted_in_go_override=counted,
    )


def compute_m35(media_corpus: MediaCorpusTotals | None, bands: BandSpec) -> MetricValue:
    """`media_candidates_per_story` -- Decision P4-98 (corrected in place
    round 17). The statistic is the MAXIMUM, over `Phase`, of that phase's
    own `candidates_scored / stories_persisted` ratio, each phase pooled
    across every cycle/season in the corpus's `live` arm -- never the
    pooled corpus-wide ratio (which would hide a single-phase regression,
    P4-98's own reason for the correction). A phase enters the max only if
    it persisted at least `media_corpus.top_k_per_cycle` stories corpus-
    wide. `comparator: max`, `band_high: 15.0`. **SKIPs when no phase
    qualifies** (including the `stories_persisted_total == 0` case),
    matching `compute_m30`'s exact `packages_evaluated == 0` guard shape.

    `detail["per_phase"]` carries every phase's own `candidates`/`stories`/
    `ratio`, including the ineligible ones (flagged `eligible: False`);
    `detail["max_phase"]` names the phase that set the max; `detail[
    "pooled_ratio_uncounted"]` is the corpus-wide pooled figure, reported
    for context and never gated (P4-98's own explicit "moves to `detail`,
    uncounted" correction). `detail["zero_candidate_phase_count"]`
    (register item 16) discloses how many phases scored zero candidates
    corpus-wide, so a PASS here is never misread as "vigilance is cheap"
    when it may instead be "vigilance had almost nothing to look at.'"""
    if media_corpus is None:
        return MetricValue(value=None, detail={}, skip_reason=_MEDIA_ZERO_MECHANISM_SKIP_REASON)
    live = media_corpus.live
    top_k = media_corpus.top_k_per_cycle

    candidates_by_phase: dict[str, int] = {}
    stories_by_phase: dict[str, int] = {}
    for cycle in live.cycles:
        candidates_by_phase[cycle.phase] = (
            candidates_by_phase.get(cycle.phase, 0) + cycle.candidates_scored
        )
        stories_by_phase[cycle.phase] = (
            stories_by_phase.get(cycle.phase, 0) + cycle.stories_persisted
        )

    per_phase: dict[str, dict[str, object]] = {}
    eligible_ratios: dict[str, float] = {}
    for phase in sorted(set(candidates_by_phase) | set(stories_by_phase)):
        candidates = candidates_by_phase.get(phase, 0)
        stories = stories_by_phase.get(phase, 0)
        eligible = stories >= top_k
        ratio = candidates / stories if stories > 0 else None
        per_phase[phase] = {
            "candidates": candidates, "stories": stories, "ratio": ratio, "eligible": eligible,
        }
        if eligible and ratio is not None:
            eligible_ratios[phase] = ratio

    stories_persisted_total = sum(stories_by_phase.values())
    candidates_scored_total = sum(candidates_by_phase.values())
    if not eligible_ratios:
        return MetricValue(
            value=None,
            detail={"per_phase": per_phase, "stories_persisted_total": stories_persisted_total},
            skip_reason=(
                "no Phase persisted >= top_k_per_cycle stories corpus-wide "
                f"(stories_persisted_total={stories_persisted_total}, top_k_per_cycle={top_k})"
            ),
        )
    max_phase = max(eligible_ratios, key=lambda p: eligible_ratios[p])
    pooled_ratio = (
        candidates_scored_total / stories_persisted_total if stories_persisted_total > 0 else None
    )
    zero_candidate_phase_count = sum(1 for v in per_phase.values() if v["candidates"] == 0)
    return MetricValue(
        value=eligible_ratios[max_phase],
        detail={
            "per_phase": per_phase,
            "max_phase": max_phase,
            "pooled_ratio_uncounted": pooled_ratio,
            "zero_candidate_phase_count": zero_candidate_phase_count,
        },
    )


def compute_m36(media_corpus: MediaCorpusTotals | None, bands: BandSpec) -> MetricValue:
    """`media_cycle_conservation_violations` -- Decision P4-99. `violations`
    = the count of cycles in the corpus's `live` arm failing either clause:
    (a) conservation or (b) cycle-key overflow (more than `media_corpus.
    top_k_per_cycle` persisted rows sharing one `(league_id, season, phase,
    cycle_index)` key -- a group-by on that exact tuple). `comparator: max`,
    `band_high: 0`. **SKIP, never a fabricated `0`, when the corpus contains
    no cycles at all**, matching `compute_m32`'s exact "SKIP, never a
    fabricated 0" guard shape.

    **Clause (a)'s formula, restated in a later fix round (M36's own
    conservation-invariant restatement, `MediaCycleRecord.capped_out`):
    `stories_persisted != candidates_submitted - capped_out`, not bare
    `stories_persisted != candidates_submitted`.** The old formula predates
    `run_media_selection_cycle`'s own never-waived per-persona cap loop
    (MV-1c.3) -- once real caps exist, a capped-out candidate legitimately
    makes the two raw counts diverge BY DESIGN, every time a persona's own
    per-cycle cap binds, which the old formula could not distinguish from a
    candidate vanishing for an unaccounted reason. Subtracting `capped_out`
    keeps `stories_persisted == candidates_submitted - capped_out` a hard
    identity that no longer flags every cap as a violation.

    **Honest limitation, found by a critic and left disclosed rather than
    oversold (a prior fix round's own docstring here wrongly called this
    identity "closed" and said it "closes the exact bug class Decision
    P4-99 was written to catch" -- corrected 2026-09-23): over a corpus
    produced by `tools/generate_media_corpus.py`, clause (a) is internally
    consistent BY CONSTRUCTION, not by measurement.** `stories_persisted`,
    `candidates_submitted`, and `capped_out` are all read off the exact
    same `MediaSelectionCycleResult` object for that cycle, whose own
    producing loop (`run_media_selection_cycle`'s per-candidate `for`)
    has only two exits per candidate: `capped_out += 1; continue`, or a
    real draw that appends to `publications` (and therefore to `stories`).
    Every candidate the loop ever sees lands in exactly one of those two
    buckets, so `stories_persisted == candidates_submitted - capped_out`
    cannot be false for any corpus this tool could ever produce -- clause
    (a) can never observe a real violation here, because the loop's own
    shape already guarantees the identity holds, regardless of whether
    anything upstream (the loop itself, or something feeding it) is
    correct. **This is not a defect in the formula in the abstract** -- a
    caller with a genuinely INDEPENDENT count of persisted stories (e.g. a
    real `COUNT(*)` against a persisted-story store the accounting was
    never derived from) would make clause (a) a real, checkable
    conservation test again. `media_stories` is a real persisted store, but
    it carries no column distinguishing the `live`/`control` arm that wrote
    a given row (MV-5's own found hazard, `docs/design/media-v2-newsroom.
    md`), so a `COUNT(*)` for one `(league_id, season, phase, cycle_index)`
    key would count BOTH arms' rows together and could not be decomposed
    back into a per-arm figure -- adding a real arm-marker column would
    fix this, but is real schema work this fix round does not do. Until
    that exists, clause (a) should be read as "internally consistent by
    construction," never as "measured and found to hold" -- it cannot
    currently detect the real bug class Decision P4-99 exists to catch.
    Clause (b) (cycle-key overflow) has no such problem: it is a real
    cross-cycle-record check with an independent right-hand side
    (`top_k_per_cycle`), not read off the same per-cycle result object."""
    if media_corpus is None:
        return MetricValue(value=None, detail={}, skip_reason=_MEDIA_ZERO_MECHANISM_SKIP_REASON)
    live = media_corpus.live
    if not live.cycles:
        return MetricValue(
            value=None, detail={}, skip_reason="no cycles in the media corpus's live arm",
        )

    top_k = media_corpus.top_k_per_cycle
    conservation_violations = [
        c for c in live.cycles if c.stories_persisted != c.candidates_submitted - c.capped_out
    ]
    by_key: dict[tuple[int, int, str, int], list[MediaCycleRecord]] = {}
    for cycle in live.cycles:
        key = (cycle.league_id, cycle.season, cycle.phase, cycle.cycle_index)
        by_key.setdefault(key, []).append(cycle)
    overflow_groups = {
        key: cycles for key, cycles in by_key.items()
        if sum(c.stories_persisted for c in cycles) > top_k
    }
    overflow_cycle_count = sum(len(cycles) for cycles in overflow_groups.values())
    violations = len(conservation_violations) + overflow_cycle_count
    return MetricValue(
        value=float(violations),
        detail={
            "conservation_violations": len(conservation_violations),
            "cycle_key_overflow_groups": len(overflow_groups),
            "cycle_key_overflow_cycles": overflow_cycle_count,
            "cycles_total": len(live.cycles),
        },
    )


# ---------------------------------------------------------------------------
# The three archetype golden-table checks (A1/A2/A3) -- need no season.
# ---------------------------------------------------------------------------


def _count_table_diffs(actual: Mapping, golden: Mapping) -> int:
    diffs = 0
    all_labels = set(actual) | set(golden)
    for label in all_labels:
        actual_row = actual.get(label, {})
        golden_row = golden.get(label, {})
        all_cols = set(actual_row) | set(golden_row)
        for col in all_cols:
            if actual_row.get(col) != golden_row.get(col):
                diffs += 1
    return diffs


def _load_golden(path) -> dict:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["archetypes"]


def compute_a1(bands: BandSpec) -> MetricValue:
    from pathlib import Path

    band = bands.metrics["A1"]
    golden_path = Path(__file__).resolve().parent.parent.parent / band.params["file"]
    golden = _load_golden(golden_path)
    actual = generate_archetype_table()
    diffs = _count_table_diffs(actual, golden)
    return MetricValue(
        value=float(diffs), detail={"rows": len(actual), "golden_path": str(golden_path)}
    )


def compute_a2(bands: BandSpec) -> MetricValue:
    from pathlib import Path

    band = bands.metrics["A2"]
    golden_path = Path(__file__).resolve().parent.parent.parent / band.params["file"]
    golden = _load_golden(golden_path)
    actual = generate_pick_archetype_table()
    diffs = _count_table_diffs(actual, golden)
    return MetricValue(
        value=float(diffs), detail={"rows": len(actual), "golden_path": str(golden_path)}
    )


def compute_a3(bands: BandSpec) -> MetricValue:
    from pathlib import Path

    band = bands.metrics["A3"]
    golden_path = Path(__file__).resolve().parent.parent.parent / band.params["file"]
    golden = _load_golden(golden_path)
    actual = generate_prospect_archetype_table()
    diffs = _count_table_diffs(actual, golden)
    return MetricValue(
        value=float(diffs), detail={"rows": len(actual), "golden_path": str(golden_path)}
    )


# ---------------------------------------------------------------------------
# compute_all -- dispatch + band evaluation
# ---------------------------------------------------------------------------

_RUNS_FUNCS: dict[str, object] = {
    "M1": compute_m1, "M2": compute_m2, "M3": compute_m3, "M4": compute_m4,
    "M5": compute_m5, "M6": compute_m6, "M7": compute_m7, "M8a": compute_m8a,
    "M8b": compute_m8b, "M9": compute_m9, "M10": compute_m10, "M11": compute_m11,
    "M12": compute_m12, "M13": compute_m13, "M14": compute_m14, "M15": compute_m15,
    "M16": compute_m16, "M18": compute_m18, "M19": compute_m19, "M20": compute_m20,
    "M21": compute_m21, "M22": compute_m22,
    "M23": compute_m23, "M23p": compute_m23p, "M24": compute_m24, "M25": compute_m25,
    "M26": compute_m26, "M27": compute_m27, "M28": compute_m28, "M30": compute_m30,
    "M31": compute_m31, "M32": compute_m32,
}
_BANDS_ONLY_FUNCS: dict[str, object] = {
    "M17": compute_m17, "A1": compute_a1, "A2": compute_a2, "A3": compute_a3,
}


def _finalize(band: MetricBand, mv: MetricValue, *, gm_tuning_version: int) -> MetricResult:
    if mv.skip_reason is not None:
        status, passed, counted = "SKIP", None, False
    elif mv.value is None and mv.undefined:
        status, passed, counted = "FAIL (undefined)", False, band.counted_in_go
    else:
        base_passed = evaluate(band, mv.value)
        passed = bool(base_passed) and mv.extra_pass
        known_gap_violations = mv.detail.get("known_gap_violations") if band.known_gap else None
        if band.interpreted:
            status = "PASS (interpreted)" if passed else "FAIL (interpreted)"
        elif band.vacuous:
            status = "PASS (vacuous)" if passed else "FAIL (vacuous)"
        elif known_gap_violations:
            # Round-7/T7b critic B3: a metric with a real, disclosed
            # `known_gap` (currently only M2/apron_2) still gates on its
            # own clean boundaries -- passed/counted here are unaffected --
            # but a bare "PASS" reads as "the invariant holds everywhere,"
            # which is false when the gap's own excluded boundaries carry
            # real, nonzero violations. Same mechanism as `interpreted`/
            # `vacuous` above (a status-string annotation only), not a new
            # pass/fail path.
            boundaries = ", ".join(band.known_gap.get("boundaries", ())) if band.known_gap else ""
            base = "PASS" if passed else "FAIL"
            plural = "" if known_gap_violations == 1 else "s"
            status = (
                f"{base} (known gap: {known_gap_violations} violation{plural} at {boundaries})"
            )
        elif band.id == "M14" and (
            mv.detail.get("gated_value_precertified") is not None
            or mv.detail.get("precertification_stale")
            or mv.detail.get("measurability_source") == "precertified"
        ):
            # Decision P3-47 (Sec.11 step 9's Family-3 measurability fix)
            # and Decision P2-35 (architect round 17 Sec.3, M14's SE
            # decoupling) describe two genuinely INDEPENDENT things: the
            # value-citation disclosure is about `.value`'s own provenance
            # and its own PASS/FAIL/INDETERMINATE against the band; the
            # SE-conjunct disclosure is about `extra_pass`'s own, unrelated
            # live-vs-precertified measurability story. Critic round T-B2:
            # an `elif` here let the value-citation branch permanently
            # suppress the SE-conjunct's own loud-failure disclosures
            # (`PRECERT STALE`/`PRECERT SUSPECT`) the moment a value
            # citation was added. Build each fragment separately below,
            # then compose -- never let one fully replace the other, same
            # discipline the `known_gap`/`interpreted`/`vacuous` branches
            # above already follow ("overloading it would make two
            # unrelated conditions indistinguishable in the report").
            se_fragment: str | None = None
            se_suffix = ""
            if mv.detail.get("precertification_stale"):
                precert = mv.detail.get("se_precertified") or {}
                precert_status = _family3_citation_status(
                    precert, gm_tuning_version=gm_tuning_version
                )
                precert_reason = _family3_citation_reason(
                    precert_status, precert, gm_tuning_version=gm_tuning_version
                )
                se_fragment = f"SE PRECERT STALE: {precert_reason} -- live SE gate re-armed"
            elif mv.detail.get("measurability_source") == "precertified":
                precert = mv.detail.get("se_precertified") or {}
                se_fragment = (
                    f"SE precertified: {precert.get('se_measured')} at "
                    f"{precert.get('seeds')} seeds, P2-35"
                )
                if mv.detail.get("precertification_consistent") is False:
                    se_suffix = " -- PRECERT SUSPECT"

            citation = mv.detail.get("gated_value_precertified")
            value_fragment: str | None = None
            if citation is not None:
                live_reading = mv.detail.get("live_reading") or {}
                live_value = live_reading.get("value")
                live_se = live_reading.get("se_measured")
                live_margin_in_se = (
                    (live_value - band.band_low) / live_se
                    if live_value is not None and live_se
                    else None
                )
                live_value_str = f"{live_value:.4f}" if live_value is not None else "n/a"
                margin_str = (
                    f"{live_margin_in_se:+.2f} SE" if live_margin_in_se is not None else "n/a"
                )
                if mv.detail.get("value_source") == "precertified_corpus":
                    # B6 (b): INDETERMINATE never applies to a valid
                    # citation's own gated reading -- Decision P3-47 round
                    # 39's own spec ("fires when a citation is stale/
                    # absent, never when a valid citation gates").
                    # `evaluate(band, gated_value)` (`base_passed`) stands
                    # on its own, no INDETERMINATE overlay.
                    value_label = "PASS" if base_passed else "FAIL"
                    value_fragment = (
                        f"{value_label} (Family-3 gated at 45 precertified seeds, P3-47; "
                        f"this run's own 3-seed read: {live_value_str}, {margin_str})"
                    )
                else:
                    # Stale/absent-after-being-present citation: `.value`
                    # fell back to the live reading, so `base_passed` IS
                    # the live value's own comparison against the band
                    # here. B3's fix: INDETERMINATE must test the SAME
                    # thing its label claims -- the reading actually in
                    # play (`base_passed`), never the combined `passed`
                    # (which conflates this with the SE-conjunct's own,
                    # unrelated failure -- reported via `se_fragment`
                    # above, never folded into this label).
                    indeterminate = (
                        base_passed is False
                        and live_margin_in_se is not None
                        and live_margin_in_se > -2.0
                    )
                    value_label = (
                        "INDETERMINATE" if indeterminate
                        else ("PASS" if base_passed else "FAIL")
                    )
                    # NIT 8: the loud fallback should be at least as
                    # informative as the ordinary precision-bearing path
                    # below (`precision: X SE from the band, n=Y`).
                    n_obs = (
                        (mv.detail.get("n_star_maxer") or 0)
                        + (mv.detail.get("n_egalitarian") or 0)
                    )
                    precision_str = (
                        f"; precision: {live_margin_in_se:+.2f} SE from the band, n={n_obs}"
                        if live_margin_in_se is not None else ""
                    )
                    citation_status = _family3_citation_status(
                        citation, gm_tuning_version=gm_tuning_version
                    )
                    citation_reason = _family3_citation_reason(
                        citation_status, citation, gm_tuning_version=gm_tuning_version
                    )
                    value_fragment = (
                        f"{value_label} -- FAMILY-3 PRECERT STALE ({citation_reason} -- "
                        f"live reading re-armed{precision_str})"
                    )

            if value_fragment is not None and se_fragment is not None:
                status = f"{value_fragment}; {se_fragment}{se_suffix}"
            elif value_fragment is not None:
                status = value_fragment
            else:
                base = "PASS" if passed else "FAIL"
                status = f"{base} ({se_fragment}){se_suffix}"
        elif band.params.get("precision") is not None and mv.detail.get("precision") is not None:
            # RS3 (architect round 29 Sec.6.3/6.5 test 7): the reported-not-
            # gated precision annotation. Structurally identical to the
            # branches above (status-string only; `passed`/`counted` are
            # untouched) and placed after all of them so it can never
            # shadow them -- guarded on `band.params.get("precision")`
            # (present only for M11-M13/M15/M16's bands.json entries) so
            # M17's own, differently-shaped `detail["precision"]` (Sec.6.4,
            # no `params.precision` key at all) can never trigger this
            # branch by accident.
            #
            # Decision P3-47 (Sec.11 step 9's Family-3 fix) folds THREE
            # sub-cases into this one branch rather than adding a new
            # top-level branch: `mv.detail["precision"]` here already
            # describes whichever reading is actually gating (the gated
            # 45-seed block when a valid citation is present, else the
            # live block -- `_attach_dual_reading`'s own contract), so
            # `margin_in_se` below is always the number that matters.
            precision_detail = mv.detail["precision"]
            base = "PASS" if passed else "FAIL"
            margin_in_se = precision_detail.get("margin_in_se")
            # 4e -- fail-side INDETERMINATE: a FAIL within 2 SE of the band
            # is not distinguishably a real fail at this seed count. Never
            # SKIP, `passed`/`counted_in_go` untouched -- this is the
            # ordinary path for ANY precision-bearing metric/fixture,
            # Family-3 citation or not (ordinary case 3 below).
            indeterminate = passed is False and margin_in_se is not None and margin_in_se > -2.0
            label = "INDETERMINATE" if indeterminate else base

            value_source = mv.detail.get("value_source")
            citation = mv.detail.get("gated_value_precertified")
            if value_source == "precertified_corpus":
                # Sub-case 1: a valid citation is gating. B6 fix: a valid
                # citation's own gated reading is NEVER labeled
                # INDETERMINATE -- Decision P3-47 round 39's own spec
                # ("fires when a citation is stale/absent, never when a
                # valid citation gates"). The prior comment here claimed
                # "measurability is clear by a wide margin ... indeterminate
                # should never actually fire here" -- that checks the wrong
                # statistic: `measurability_pass` (whether the SE is small
                # enough to ever detect a difference) being wide-margin-
                # clear says nothing about `margin_in_se` (this specific
                # 45-seed VALUE's own distance from the band), which is what
                # `indeterminate` actually tests. M12's real 45-seed margin
                # is 0.83 SE and M16's is 0.34 SE -- both comfortably inside
                # the 2-SE threshold, so a future re-precertification
                # landing close to a floor WOULD trip the old symmetric
                # check. Resolved per round 39's own literal spec (option
                # (b) of the two honest choices: never apply INDETERMINATE
                # to a valid citation's own gated reading, the smaller and
                # more conservative fix): scope INDETERMINATE out entirely
                # here -- `evaluate(band, gated_value)` (`base`) stands on
                # its own, no overlay.
                live_reading = mv.detail.get("live_reading") or {}
                live_value = live_reading.get("value")
                live_margin = (live_reading.get("precision") or {}).get("margin_in_se")
                live_value_str = f"{live_value:.4f}" if live_value is not None else "n/a"
                live_margin_str = f"{live_margin:+.2f} SE" if live_margin is not None else "n/a"
                status = (
                    f"{base} (Family-3 gated at 45 precertified seeds, P3-47; "
                    f"this run's own 3-seed read: {live_value_str}, {live_margin_str})"
                )
            elif citation is not None:
                # Sub-case 2: a citation is present but version-mismatched
                # against the live bands -- mirrors M14's own "PRECERT
                # STALE" string shape. Live reading re-armed, loudly. NIT 8:
                # carry the same `(precision: X SE from the band, n=Y)`
                # figures sub-case 3 (no citation) prints below -- the loud
                # fallback should never be less informative than the
                # ordinary path.
                precision_str = (
                    f"; precision: {margin_in_se:.2f} SE from the band, "
                    f"n={precision_detail.get('n_runs')}"
                    if margin_in_se is not None else ""
                )
                citation_status = _family3_citation_status(
                    citation, gm_tuning_version=gm_tuning_version
                )
                citation_reason = _family3_citation_reason(
                    citation_status, citation, gm_tuning_version=gm_tuning_version
                )
                status = (
                    f"{label} -- FAMILY-3 PRECERT STALE ({citation_reason} -- "
                    f"live reading re-armed{precision_str})"
                )
            elif margin_in_se is not None and abs(margin_in_se) < 2.0:
                # Sub-case 3: no citation at all -- unchanged from today's
                # existing behaviour, plus the new fail-side INDETERMINATE
                # label (the ordinary path for any metric/fixture without
                # a citation, this build's own default going forward).
                status = (
                    f"{label} (precision: {margin_in_se:.2f} SE from the band, "
                    f"n={precision_detail.get('n_runs')})"
                )
            else:
                status = label
            if precision_detail.get("precision_stale"):
                status += " -- PRECISION SD STALE"
        else:
            status = "PASS" if passed else "FAIL"
        # Decision P4-105 -- M34's own per-reading override (see `MetricValue.
        # counted_in_go_override`'s docstring). `None` for every metric but
        # M34, so this is a no-op everywhere else.
        counted = band.counted_in_go if mv.counted_in_go_override is None else (
            mv.counted_in_go_override
        )

    return MetricResult(
        id=band.id, number=band.number, family=band.family, name=band.name,
        comparator=band.comparator, unit=band.unit, band_low=band.band_low,
        band_high=band.band_high, value=mv.value, detail=mv.detail, per_season=mv.per_season,
        status=status, passed=passed, counted_in_go=counted, rationale=band.rationale,
        strength=band.strength,
    )


def compute_all(
    runs: tuple[RunRecord, ...],
    bands: BandSpec,
    *,
    determinism_runs: tuple[RunRecord, ...] = (),
    include_metric_17: bool = True,
    control_mismatches: int | None = None,
    control_totals: dict[str, float] | None = None,
    m27_control_by_philosophy_template: Mapping[str, Mapping[str, int]] | None = None,
    media_corpus: MediaCorpusTotals | None = None,
) -> tuple[MetricResult, ...]:
    """Every metric except M17 (`include_metric_17=False` for the fast
    `--skip-games`/dev loop where a 100-draft pass is unwanted overhead --
    `validate_gm.py`'s own `--only-metric-17` flag computes M17 alone via
    `compute_m17` directly, bypassing this function and `run_seed`
    entirely, per report Sec.6.2 item 17's own fast-path instruction).

    **`determinism_runs`, T7b critic-round B1's own fix (round 1), refined
    by round-2's B-NEW-1 fix.** `validate_gm.py`'s `--determinism-check`
    builds two extra `RunRecord`s (seed 0, `skip_games=True`, run twice)
    whose ONLY job is M6's own byte-identical JSONL diff (Family-1
    invariant 6). Before B1's own fix, `validate_gm.py` itself concatenated
    those into the main `runs` tuple and handed the combined tuple to this
    function, which fed the determinism duplicates to EVERY metric --
    silently triplicating seed 0's own weight in every pooled Family-2/3/4
    metric, permanently `SKIP`-ing M22 under a real with-games run (the
    duplicates are hardcoded `skip_games=True` regardless of the run's own
    flag), and flipping M14's own `se_measured <= se_threshold` gate from a
    real FAIL to a false PASS by inflating its per-cohort sample size with
    duplicate observations. `determinism_runs` is a separate parameter that
    every metric OTHER than M6 (including M17/A1-A3, which never take
    `runs` at all) never sees at all -- `runs` reaches them exactly as the
    caller passed it, always.

    **B-NEW-1 (round-2 critic, this fix): M6 itself must see
    `determinism_runs` ALONE, never `runs + determinism_runs`.**
    `validate_gm.py` derives `det_seed` with the exact same `rng_for(...,
    "seed", 0)` call `runs[0]`'s own seed uses -- so `runs[0]`,
    `det_run_a`, and `det_run_b` all share one identical `rng_seed`.
    Concatenating `runs` in here made `compute_m6`'s own by-`rng_seed`
    grouping put all three into ONE three-member group; `a, b = group[0],
    group[1]` then silently compared `runs[0]` against `det_run_a` --
    never the intended `det_run_a` vs `det_run_b` pair -- while `det_run_b`
    (a real, costly re-run) was computed and never read by anything.
    `compute_m6`'s own body is untouched by this fix; only the input it is
    handed changes -- `determinism_runs` alone always forms exactly one
    clean two-member group (both entries share `det_seed` with each other,
    and no other run is present to pollute it), and `compute_m6`'s existing
    empty-input handling already covers `--no-determinism-check`
    (`determinism_runs == ()` -> `SKIP`).

    **`control_mismatches`, M29's own precomputed input, the identical
    shape `determinism_runs` already established for M6 -- a further,
    disclosed exception to "every compute_mN takes runs and bands and
    nothing else."** M29 (`control_equivalence`) needs a control pass at
    `max_negotiation_rounds=0` diffed against the committed `seeds/gm_
    decision_log_digest.json` baseline -- both the extra `run_seed` call
    and the baseline file read are I/O this module's own purity
    constraint forbids, so `validate_gm.py` (which already reads `--bands`
    from disk) does both and hands this function only the resulting
    mismatch count. `None` means the control pass was not run this
    invocation (`--no-control-check`) -> `compute_m29` reports `SKIP`.

    **`control_totals`, §11 step 17's own reuse of the identical
    exception -- M28's control-pass `extra_pass` conjunct half.** The
    SAME control run M29 already builds (`run_seed(..., max_negotiation_
    rounds=0)`), pooled through `_pool_negotiation_totals` by `validate_
    gm.py` into the same-shaped totals dict this module already produces
    for the main pooled `runs`. `None` (the default, and `--no-control-
    check`'s own reading) makes `compute_m28` report a hard `SKIP`,
    mirroring `compute_m29`'s own `control_mismatches is None` precedent
    exactly (step 17's second fix round) -- not the vacuously-true
    conjunct this docstring described before that round.

    **`m27_control_by_philosophy_template`, Decision P3-49's own reuse of
    the identical exception.** M27 is now a PAIRED DIFFERENCE against its
    own `with_philosophies_disabled` control arm -- a SEPARATE re-run of
    this invocation's own real corpus (never the M29/M28 control run
    above, which uses `max_negotiation_rounds=0` and a different, fixed
    historical baseline seed). `validate_gm.py` builds that control arm
    and pools it through `_pool_phrase_totals` into the same `philosophy.
    value -> template_id -> count` shape `compute_m27`'s own live pooling
    already produces, and hands this function only the resulting dict.
    `None` (the default, and `--no-control-check`'s own reading) makes
    `compute_m27` report a hard `SKIP`, mirroring `compute_m28`'s/
    `compute_m29`'s identical `None`-means-no-control-pass-ran precedent.

    **`media_corpus`, Decision P4-95's own reuse of the identical
    exception -- the FOURTH instance.** M33-M36 (Decision P4-94) all read
    a single precomputed `MediaCorpusTotals` (Decision P4-102/P4-103) --
    real media entry points are coroutine functions, take an `Executor`, and
    persist to Postgres, all three of which this module's own zero-I/O
    purity constraint forbids, so `tools/generate_media_corpus.py` (20a)
    builds the corpus once, offline, and `validate_gm.py` (20b) loads the
    committed artifact and hands this function only the resulting
    dataclass. `None` (the default, `--no-media-corpus`, or any run before
    20a's artifact exists) makes all four `SKIP` -- Decision P4-34's own
    hard rule, never a fabricated reading and never a PASS.

    **Fix 4 (2026-09-23 critic round): one metric's own bug can no longer
    take the whole gate run down.** Before this fix, an uncaught exception
    inside any `compute_mN` call (e.g. `compute_m34`'s own bare `assert`,
    which a corpus with a genuinely broken paired-arm design can trip) rose
    straight through this loop and crashed `validate_gm.py` entirely --
    every OTHER metric's own real, independently-computable reading was
    lost along with it, for a real exit-gate tool's real production run.
    Each metric's own dispatch now runs inside a `try`; an exception is
    caught, reported as that ONE metric's own `SKIP` (never a fabricated
    `PASS`, and never allowed to look like a real `FAIL` either -- a crash
    is not a measurement), and every other metric still computes and
    reports normally. `skip_reason` names the exception type and message
    so the underlying bug is still visible in the report, not swallowed."""
    results = []
    for metric_id, band in bands.metrics.items():
        if metric_id == "M17" and not include_metric_17:
            results.append(
                MetricResult(
                    id=band.id, number=band.number, family=band.family, name=band.name,
                    comparator=band.comparator, unit=band.unit, band_low=band.band_low,
                    band_high=band.band_high, value=None, detail={}, per_season=None,
                    status="SKIP", passed=None, counted_in_go=False, rationale=band.rationale,
                    strength=band.strength,
                )
            )
            continue
        try:
            if metric_id == "M6":
                mv = compute_m6(determinism_runs, bands)
            elif metric_id == "M29":
                mv = compute_m29(control_mismatches, bands)
            elif metric_id == "M28":
                mv = compute_m28(runs, bands, control_totals=control_totals)
            elif metric_id == "M27":
                mv = compute_m27(
                    runs, bands,
                    control_by_philosophy_template=m27_control_by_philosophy_template,
                )
            elif metric_id == "M33":
                mv = compute_m33(media_corpus, bands)
            elif metric_id == "M34":
                mv = compute_m34(media_corpus, bands)
            elif metric_id == "M35":
                mv = compute_m35(media_corpus, bands)
            elif metric_id == "M36":
                mv = compute_m36(media_corpus, bands)
            elif metric_id in _RUNS_FUNCS:
                mv = _RUNS_FUNCS[metric_id](runs, bands)
            elif metric_id == "M17":
                mv = compute_m17(bands)
            else:
                mv = _BANDS_ONLY_FUNCS[metric_id](bands)
        except Exception as exc:
            # Fix 4's own docstring above: any single metric's own bug must degrade to that
            # metric's SKIP, never crash the whole gate run.
            mv = MetricValue(
                value=None,
                detail={"crash_type": type(exc).__name__, "crash_message": str(exc)},
                skip_reason=f"{metric_id} raised {type(exc).__name__}: {exc}",
            )
        results.append(_finalize(band, mv, gm_tuning_version=bands.gm_tuning_version))
    return tuple(results)


# ---------------------------------------------------------------------------
# Decision P3-29 -- the two committed Phase-2 control artifacts and their
# comparators. Phase 3 §11 Stage A step 2. **No dependency on any other
# Phase 3 step or switch, and nothing below is wired into `compute_all` or
# `seeds/gm_validation_bands.json`** -- this establishes a "before any
# Phase 3 mechanism exists" baseline for a later step (M29, `control_
# equivalence`) to diff against; it does not gate anything itself.
# ---------------------------------------------------------------------------

_REPO_ROOT_FROM_METRICS = Path(__file__).resolve().parent.parent.parent
DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH = (
    _REPO_ROOT_FROM_METRICS / "docs" / "ledger" / "phase-2-exit-gate-report.json"
)
DEFAULT_GM_DECISION_LOG_DIGEST_PATH = (
    _REPO_ROOT_FROM_METRICS / "seeds" / "gm_decision_log_digest.json"
)

EXCLUDED_REPORT_FIELDS: frozenset[str] = frozenset(
    {"generated_at", "elapsed_seconds", "phase_timings_by_seed", "per_season_phase_timings"}
)
"""Decision P3-29(a)'s own excluded-field list, verbatim from the design
doc: every wall-clock field a byte-diff would fail on every run, none of
them reproducible. Doubles as the "any timing key" clause applied to each
metric's `detail` block -- **the design doc's own wording is broader
("minus any timing key") than this hardcoded 4-name set**; that is
believed intentional (no `detail` block on disk today carries any OTHER
timing-shaped key, and `_strip_timing_keys` is deliberately named/scoped
narrowly rather than pattern-matching on e.g. a `*_seconds` suffix, which
could silently swallow a real future metric field). **Asserted exactly**
(not just applied) by `test_excluded_report_fields_is_exactly_the_wall_
clock_set` in `tests/unit/tools/gm_harness/test_metrics.py`, so a later
step cannot quietly widen this set without that test failing."""

_COMPARED_TOP_LEVEL_REPORT_FIELDS: tuple[str, ...] = (
    "go", "go_reason", "seeds", "seasons", "skip_games",
)
"""Decision P3-29(a)'s exact top-level field list. `players_per_team`,
`rng_seed`, `determinism_check`, `summary` are simply not on the design
doc's list -- not "excluded" for being wall-clock, just out of scope for
this comparator."""

_COMPARED_METRIC_FIELDS: tuple[str, ...] = (
    "id", "value", "status", "passed", "counted_in_go", "band_low", "band_high",
)
"""Decision P3-29(a)'s exact per-metric field list. `number`, `family`,
`name`, `comparator`, `unit`, `per_season` are descriptive/cosmetic and not
on the design doc's list. **Known limitation, inherited from the design
doc's own spec rather than introduced here (critic round 27 nit 5):**
`per_season` and the report's top-level `summary` block are excluded
entirely, so per-season drift underneath an identical pooled/aggregate
value is invisible to this comparator."""


_PATH_SHAPED_DETAIL_FIELDS: frozenset[str] = frozenset({"golden_path"})
"""**Step 9's own fix (Phase 3 §11 Stage B), wired in alongside comparison
(a)'s first real use.** Not a timing key -- `EXCLUDED_REPORT_FIELDS` is
reserved for wall-clock fields and has its own asserted-exact test whose
semantics this must not blur -- but still environment-dependent: the
committed baseline's A1/A2/A3 metrics each carry a `detail.golden_path`
field recording an ABSOLUTE filesystem path at generation time (verified,
`grep -n golden_path docs/ledger/phase-2-exit-gate-report.json`: `C:\\Users\\
Owner\\Desktop\\AI\\Projects\\dba2\\.claude\\worktrees\\agent-abc94cc3363b654
07\\seeds\\gm_value_archetypes.json`). A comparison run from any other
checkout/worktree location produces a different absolute string for the
SAME target file -- not a real content difference, and a naive `!=`
comparison would fail every single run regardless of correctness.
**Compared by path tail, not excluded outright** -- see
`_normalize_path_shaped_value` -- so a comparison against a genuinely
different target file (a different basename) still reports a mismatch.
Asserted exactly (not just applied) by `test_path_shaped_detail_fields_is_
exactly_the_golden_path_set`, the same discipline `EXCLUDED_REPORT_FIELDS`
already gets, so a later step cannot silently widen this set either."""


def _normalize_path_shaped_value(path_str: str) -> str:
    """Reduces an absolute, environment-dependent path to its portion from
    `seeds/` onward (`\\`/`/` both split, so a Windows-generated baseline
    compares equal to a POSIX-generated actual pointing at the same file),
    falling back to the bare basename if `seeds` is not a path segment.
    The minimum honest fix per this step's own instructions: two reports
    whose `golden_path` differs only in its absolute prefix compare equal;
    two reports whose target FILE genuinely differs (different basename)
    still compare unequal, because the tail differs too."""
    parts = [p for p in re.split(r"[\\/]+", path_str) if p]
    if "seeds" in parts:
        return "/".join(parts[parts.index("seeds") :])
    return parts[-1] if parts else path_str


def _normalize_path_shaped_fields(value: object) -> object:
    """Recursively rewrites every `_PATH_SHAPED_DETAIL_FIELDS` key's string
    value via `_normalize_path_shaped_value`, mirroring `_strip_timing_
    keys`'s own recursion shape but rewriting rather than dropping."""
    if isinstance(value, dict):
        return {
            k: (
                _normalize_path_shaped_value(v)
                if k in _PATH_SHAPED_DETAIL_FIELDS and isinstance(v, str)
                else _normalize_path_shaped_fields(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_normalize_path_shaped_fields(v) for v in value]
    return value


def _strip_timing_keys(value: object) -> object:
    """Recursively drops any key in `EXCLUDED_REPORT_FIELDS` from a nested
    `dict`/`list` structure -- applied to each metric's own `detail` block
    per Decision P3-29(a)'s "detail minus any timing key" clause. A no-op
    over every `detail` block on disk today (none carries a timing key);
    defensive against a future metric adding one."""
    if isinstance(value, dict):
        return {
            k: _strip_timing_keys(v) for k, v in value.items() if k not in EXCLUDED_REPORT_FIELDS
        }
    if isinstance(value, list):
        return [_strip_timing_keys(v) for v in value]
    return value


def load_phase2_exit_gate_baseline(path: Path = DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH) -> dict:
    """Loads the committed `docs/ledger/phase-2-exit-gate-report.json`.
    The file's own `_provenance` header key (Decision P3-29(a)'s disclosed
    precision-block gap) is not a report field and is simply never read by
    the comparator below -- no stripping needed."""
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ReportComparison:
    equal: bool
    mismatches: tuple[str, ...]
    """Human-readable, one entry per differing field/metric. Empty iff
    `equal` is True."""


_MISSING = object()
"""Sentinel distinguishing "key absent" from "key present with value
`None`" -- critic round 27 nit 3: `.get(field, None)` on both sides makes
a field that vanished from BOTH `actual` and `baseline` compare `None ==
None` and pass silently, which is the wrong answer for a comparator whose
whole job is catching a shape/contract drift, not just a value drift."""


def _detail_diff_summary(a_detail: Mapping, b_detail: Mapping) -> str:
    """Reports only the keys that actually differ (added, removed, or
    changed) between two already timing-stripped `detail` dicts, rather
    than dumping the whole structure on both sides -- critic round 27 nit
    1: a single differing float inside `detail.precision` was printing a
    multi-KB blob twice in the mismatch report. Shallow (top-level keys of
    `detail` only) -- proportionate to this module's actual `detail`
    shapes, none of which nest more than one level deep today."""
    parts = []
    for key in sorted(set(a_detail) | set(b_detail)):
        a_value = a_detail[key] if key in a_detail else _MISSING
        b_value = b_detail[key] if key in b_detail else _MISSING
        if a_value != b_value:
            parts.append(f"{key}: {a_value!r} != {b_value!r}")
    return "; ".join(parts)


def compare_exit_gate_reports(actual: Mapping, baseline: Mapping) -> ReportComparison:
    """Decision P3-29(a)'s value-scoped comparator: `actual` (a freshly
    generated `report.json`'s parsed dict) against `baseline` (the
    committed `docs/ledger/phase-2-exit-gate-report.json`'s parsed dict).
    Compares exactly the field lists above -- **never** a byte/dict
    equality over the whole report, which would fail on every run's own
    wall-clock fields even with zero real drift.

    **`detail.precision` (M11/M12/M13/M15/M16) is NOT a timing key and is
    fully inside this comparison's scope** -- `_strip_timing_keys` is a
    no-op over it. A `GM_TUNING_VERSION` bump (Decision P3-30, scheduled
    once, at step 4) will flip `precision_stale`/`sd_source`/
    `sd_across_seeds` and each affected metric's own `status` string on
    ALL SIX of M11/M12/M13/M15/M16/M14 -- M14 via its own separate
    `precertification_stale` branch, not the `precision_stale` check the
    other five share. For M11/M12/M13/M15/M16 this is a cosmetic
    ` -- PRECISION SD STALE` annotation with `passed` unchanged. **For
    M14 it is not cosmetic**: if `measurability_pass` is False at that
    point (check the committed baseline's own `detail.measurability_pass`
    -- it was False as of this baseline), `extra_pass` collapses and
    M14's `passed` flips True to False, which -- since M14 is
    `counted_in_go: true` -- flips the top-level `go`/`go_reason` from
    GO to NO-GO. This comparator will correctly report a mismatch on all
    six metrics' `status`/`detail` plus, if M14 flips, the top-level `go`
    fields too. The five-metric annotation-only mismatch is an EXPECTED
    non-regression (HANDOFF.md's own "auto-detect staleness and degrade
    loudly, never silently" design working as intended) -- but an M14-
    driven NO-GO is a real gate failure that step 8's scheduled
    re-precertification exists to clear, not something to wave through
    as cosmetic. See `docs/ledger/phase-2-exit-gate-report.json`'s own
    `_provenance` header for the full disclosure."""
    mismatches: list[str] = []

    for field_name in _COMPARED_TOP_LEVEL_REPORT_FIELDS:
        actual_value = actual[field_name] if field_name in actual else _MISSING
        baseline_value = baseline[field_name] if field_name in baseline else _MISSING
        if actual_value is _MISSING or baseline_value is _MISSING:
            missing_side = "actual" if actual_value is _MISSING else "baseline"
            mismatches.append(f"top-level {field_name!r}: missing from {missing_side}")
        elif actual_value != baseline_value:
            mismatches.append(f"top-level {field_name!r}: {actual_value!r} != {baseline_value!r}")

    actual_metrics = actual.get("metrics", ())
    baseline_metrics = baseline.get("metrics", ())
    actual_by_id = {m["id"]: m for m in actual_metrics}
    baseline_by_id = {m["id"]: m for m in baseline_metrics}
    if len(actual_by_id) != len(actual_metrics):
        mismatches.append(
            f"actual['metrics'] has duplicate ids ({len(actual_metrics)} entries, "
            f"{len(actual_by_id)} unique)"
        )
    if len(baseline_by_id) != len(baseline_metrics):
        mismatches.append(
            f"baseline['metrics'] has duplicate ids ({len(baseline_metrics)} entries, "
            f"{len(baseline_by_id)} unique)"
        )

    only_in_actual = sorted(set(actual_by_id) - set(baseline_by_id))
    only_in_baseline = sorted(set(baseline_by_id) - set(actual_by_id))
    if only_in_actual:
        mismatches.append(f"metrics present only in actual: {only_in_actual}")
    if only_in_baseline:
        mismatches.append(f"metrics present only in baseline: {only_in_baseline}")

    for metric_id in sorted(set(actual_by_id) & set(baseline_by_id)):
        a, b = actual_by_id[metric_id], baseline_by_id[metric_id]
        for field_name in _COMPARED_METRIC_FIELDS:
            a_value = a[field_name] if field_name in a else _MISSING
            b_value = b[field_name] if field_name in b else _MISSING
            if a_value is _MISSING or b_value is _MISSING:
                missing_side = "actual" if a_value is _MISSING else "baseline"
                mismatches.append(f"{metric_id}.{field_name}: missing from {missing_side}")
            elif a_value != b_value:
                mismatches.append(f"{metric_id}.{field_name}: {a_value!r} != {b_value!r}")
        a_detail = _normalize_path_shaped_fields(_strip_timing_keys(a.get("detail") or {}))
        b_detail = _normalize_path_shaped_fields(_strip_timing_keys(b.get("detail") or {}))
        if a_detail != b_detail:
            diff_summary = _detail_diff_summary(a_detail, b_detail)
            mismatches.append(f"{metric_id}.detail (minus timing keys): {diff_summary}")

    return ReportComparison(equal=not mismatches, mismatches=tuple(mismatches))


def season_decision_log_digest(jsonl_lines: Sequence[str]) -> str:
    """Decision P3-29(b)'s per-season digest: SHA-256 over `SeasonRecord.
    jsonl_lines`, in emission order. Each line is UTF-8-encoded with a
    trailing `\\n` between entries (JSONL's own on-disk convention) rather
    than hashing the lines' concatenation with no separator, which would
    let `("ab", "c")` and `("a", "bc")` collide -- the design doc does not
    pin the exact byte framing, so this is stated here as the chosen one."""
    hasher = hashlib.sha256()
    for line in jsonl_lines:
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


@dataclass(frozen=True)
class DigestLineDiff:
    index: int
    actual_line: str | None
    """`None` iff `actual` has fewer lines than `expected` and the mismatch
    is a missing trailing line."""
    expected_line: str | None
    """`None` iff `expected` has fewer lines than `actual`."""


def diff_decision_log_lines(
    actual: Sequence[str], expected: Sequence[str]
) -> DigestLineDiff | None:
    """Pure, in-memory line-level diff over two `jsonl_lines` sequences --
    no disk access, no committed corpus. Returns the first index at which
    the two sequences disagree (`None` if they are identical), carrying
    both lines at that index so a digest mismatch is localizable without
    needing the actual seed data on disk (Decision P3-29(b))."""
    for i, (a_line, b_line) in enumerate(zip_longest(actual, expected, fillvalue=None)):
        if a_line != b_line:
            return DigestLineDiff(index=i, actual_line=a_line, expected_line=b_line)
    return None


@dataclass(frozen=True)
class SeasonDigestComparison:
    season: int
    equal: bool
    actual_digest: str
    expected_digest: str
    line_diff: DigestLineDiff | None
    """Populated only when a `reference_seasons` sequence was supplied to
    `compare_decision_log_digest` and this season's digest disagreed --
    the committed digest file stores hashes only, never lines, so a
    mismatch against it alone cannot be localized to a line without a
    live in-memory run to diff against."""


def compare_decision_log_digest(
    seasons: Sequence[SeasonRecord],
    baseline: Mapping,
    *,
    reference_seasons: Sequence[SeasonRecord] | None = None,
) -> tuple[SeasonDigestComparison, ...]:
    """Decision P3-29(b)'s digest comparator. `seasons` is a fresh 1-seed
    run's `RunRecord.seasons` (from `tools.generate_gm_decision_log_digest.
    generate_digest`'s own `run_seed` call); `baseline` is the parsed,
    committed `seeds/gm_decision_log_digest.json`.

    Digest equality is checked unconditionally. Line-level localization
    additionally runs when the caller supplies `reference_seasons` -- a
    second, independently-obtained in-memory run of the same seed/season
    configuration (e.g. a second live call to `generate_digest`, exactly
    how the "reproduces byte-identically on a rerun" verify test uses
    this) -- because the committed baseline itself carries no lines to
    diff against (only the digest is committed; Decision P3-29(b) is
    explicit that nothing about the full corpus gets committed)."""
    results: list[SeasonDigestComparison] = []
    baseline_by_season = {
        row["season"]: row["digest"] for row in baseline.get("season_digests", ())
    }
    reference_by_season = (
        {s.season: s for s in reference_seasons} if reference_seasons is not None else {}
    )

    for season_record in seasons:
        actual_digest = season_decision_log_digest(season_record.jsonl_lines)
        expected_digest = baseline_by_season.get(season_record.season, "")
        equal = actual_digest == expected_digest
        line_diff = None
        if not equal and season_record.season in reference_by_season:
            line_diff = diff_decision_log_lines(
                season_record.jsonl_lines, reference_by_season[season_record.season].jsonl_lines
            )
        results.append(
            SeasonDigestComparison(
                season=season_record.season, equal=equal, actual_digest=actual_digest,
                expected_digest=expected_digest, line_diff=line_diff,
            )
        )
    return tuple(results)
