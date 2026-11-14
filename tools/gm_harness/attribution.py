"""The Family-3 attribution instrument (Phase 3 design doc Sec.11 Stage A
step 3; the full spec is architect round-17 report Sec.4.6 item 2 -- the
one-line gloss in the Phase 3 doc is a pointer, not the spec).

**What problem this solves, restated from the memo that specified it.**
Architect round 17 Sec.4 found M12 (a Family-3, `fit_value`-dependent
cohort statistic) moved 1.7908 -> 1.4194 across a build change, and could
not tell whether that movement was a real behavioural effect or an
artifact of teams getting reclassified into different `CompetitiveWindow`
cohorts between the two runs being compared -- cohort membership is not
stable run-to-run (Sec.4.3: cohort sizes moved 118->120 and 87->90 on the
same 15-cell sample). Sec.4.6 item 2's mechanism, bullet (A): **recompute
the "after" run's metric using the "before" run's own cohort memberships,
letting everything else -- the actual quantities being measured -- vary
between the two runs as they really did.** If the counterfactual
reproduces the "before" value, the movement is pure membership reshuffle.
If it reproduces the "after" value, membership is irrelevant and whatever
changed did so within stable cohorts.

**Generalized here, not hardcoded to M12** (Phase 3 design doc Sec.11 step
3's own text: "no dependency on any Phase 3 switch," i.e. usable on any
Family-3 metric in this codebase, not a one-off M12 script). Per `metrics.
py`'s own module docstring (Judgment call 3), this codebase's Family-3
metrics classify teams into cohorts on exactly two bases:

- **`window_by_team`** (a `CompetitiveWindow`, recorded per `BoundaryRecord`
  -- i.e. genuinely re-derived, and re-shufflable, every season) for M11
  (team-season denominator only -- see the M11 caveat below), M12, M13.
- **`philosophy_by_team`** (a `GmPhilosophy`, a `RunRecord`-level field
  assigned once at league generation, not re-derived per season) for M15,
  M16.

M14 is Family 3 in `seeds/gm_validation_bands.json` but is deliberately
excluded here: unlike M11-M13/M15/M16, `compute_m14` has no `_m14_core`/
`_mN_raw_value` seam to call into -- its cohort/SE/gate logic all lives
directly in `compute_m14` itself (`metrics.py`). Hooking it in would mean
either editing `metrics.py` (out of scope -- see this module's own "Reuse,
don't reinvent" note and the surgical/additive posture this slice was
built under) or duplicating `compute_m14`'s body a second time (exactly
the risk that note exists to avoid). M17 is also excluded: it takes no
`runs` at all (`compute_m17`'s own docstring -- 100 freshly-drawn
synthetic draft classes per call), so there are no two runs' cohort
memberships to compare.

**The substitution mechanism.** Rather than re-implementing each metric's
own cohort-conditioned aggregation a second time (a duplicate-implementation
risk this project has been burned by before -- see `metrics.py`'s own
"Reuse, don't reinvent" note), this module builds a synthetic `RunRecord`
that is the "after" run with its `window_by_team` (or `philosophy_by_team`)
fields replaced by the "before" run's own values, then calls the real,
unmodified `_mN_core` function on it. Every other field on the synthetic
run -- rosters, acquisitions, pick holdings, ages, cap sheets -- is the
"after" run's own real data, untouched. This is bullet (A) applied
literally: membership held fixed, everything else left to vary as it
really did.

**Disclosed scope limitation for M11.** M11's *numerator* reads each
`Acquisition`'s own `window_at_acquisition` field, captured live at the
moment of that transaction (`season.py`'s own doc note), not `window_by_
team` at `season_end` -- there is no clean per-season mapping to swap in
for it (a given acquisition either happened in the "after" run's own
history or it did not; there is no "before" run counterpart to pair it
against). Only M11's *denominator* (`team_seasons_hi`/`team_seasons_lo`,
which reads `window_by_team` at `season_end` exactly like M12/M13) is held
fixed by this instrument. This is disclosed via `FixedMembershipDiffResult.
caveats`, not silently partial.

**Run pairing.** `before`/`after` are matched by `RunRecord.rng_seed`, not
list position -- `validate_gm.py` derives seed `i` from a base seed and
index alone (architect round 17 Sec.4.3's own citation), so two runs meant
to be compared "the same seed, before and after a build change" share
literal `rng_seed` values. A seed present on only one side raises rather
than silently pairing by position (this project's standing "never silently
degrade" convention, e.g. `metrics.py::_boundary`'s own `KeyError`).

**Season pairing, for the window basis (M11/M12/M13).** `season.season`
values come from `--season-year` (default 2026) plus an index, both
user-settable -- two runs compared across a build change that also
changed `--season-year` or `--seasons` have partially or fully disjoint
season numbers. Substituting membership by key would then silently no-op
(or partially no-op) rather than raise, and the result would report the
strongest possible false verdict: a `membership_reshuffle_delta` of 0.0
with no caveat, when the counterfactual was never actually computed. This
instrument therefore requires `before_run.seasons` and `after_run.seasons`
to carry the exact same season-number SEQUENCE (not just the same set --
`_m13_core` reads its season *positionally*, `run.seasons[resolves_at -
1]`, so a same-set-different-order pairing would substitute the wrong
season's membership even though every number is present on both sides)
and raises otherwise, exactly as `_pair_by_seed` already does for the
seed axis.

**No standard error on the counterfactual (deferred, not forgotten).**
`fixed_membership_diff` calls each metric's raw `_mN_core` directly,
bypassing `metrics.py::_attach_precision` -- `value_before`/`value_after_
real`/`value_after_fixed_membership` carry no SE, so a nonzero `fixed_
membership_delta` on real data has no yardstick against noise (the exact
problem Sec.4.6 item 3's per-seed SE was written to solve for the plain
metric value, not for this counterfactual). Wiring precision into the
counterfactual itself -- e.g. a jackknife over the paired seeds' own
fixed-membership recomputations -- is a real extension and is explicitly
left to a future round rather than attempted here; a caller judging a
real `fixed_membership_delta` should cross-reference the metric's own
`detail["precision"]["sd_across_seeds"]` as an approximate scale, not
treat any nonzero delta as automatically material."""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dba2.domain.team import GmPhilosophy
from dba2.managers.tuning import CompetitiveWindow
from tools.gm_harness.bands import BandSpec
from tools.gm_harness.metrics import (
    _boundary,
    _m11_core,
    _m12_core,
    _m13_core,
    _m15_core,
    _m16_core,
)
from tools.gm_harness.season import RunRecord

_WINDOW_BOUNDARY_LABEL = "season_end"
"""Matches `metrics.py`'s own module docstring Judgment call 3: M11/M12/
M13's team-season cohort basis is `window_by_team` at the `"season_end"`
boundary."""

_CORE_BY_METRIC = {
    "M11": _m11_core,
    "M12": _m12_core,
    "M13": _m13_core,
    "M15": _m15_core,
    "M16": _m16_core,
}

_BASIS_BY_METRIC = {
    "M11": "window",
    "M12": "window",
    "M13": "window",
    "M15": "philosophy",
    "M16": "philosophy",
}

_CAVEATS_BY_METRIC: dict[str, tuple[str, ...]] = {
    "M11": (
        "M11's numerator reads each Acquisition's own window_at_acquisition "
        "field, which this instrument does NOT substitute (see module "
        "docstring); only the team-season denominator's window_by_team is "
        "held fixed. A nonzero membership_reshuffle_delta for M11 reflects "
        "the denominator cohort alone.",
    ),
}

_SUPPORTED_METRICS = tuple(_CORE_BY_METRIC)


# ---------------------------------------------------------------------------
# Membership extraction / substitution, per basis
# ---------------------------------------------------------------------------


def _window_membership(
    run: RunRecord, boundary_label: str = _WINDOW_BOUNDARY_LABEL
) -> dict[int, dict[int, CompetitiveWindow]]:
    """`{season_number: {team_id: CompetitiveWindow}}` at `boundary_label`,
    one entry per season actually present on `run`."""
    return {
        season.season: dict(_boundary(season, boundary_label).window_by_team)
        for season in run.seasons
    }


def _assert_same_season_sequence(before_run: RunRecord, after_run: RunRecord) -> None:
    """Requires `before_run`/`after_run` to carry the exact same season-
    number SEQUENCE, not merely the same set. `season.season` values come
    from `--season-year` (default 2026) plus an index, both user-settable
    (see module docstring's "Season pairing" note) -- two runs that used a
    different `--season-year` or `--seasons` can have partially or fully
    disjoint season numbers, and `_m13_core` additionally reads its season
    *positionally* (`run.seasons[resolves_at - 1]`, `metrics.py:1252`), so
    even a same-SET-different-ORDER pairing would substitute the wrong
    season's membership. Raises loudly rather than letting `_substitute_
    window_membership` silently (or partially) no-op on a mismatch -- this
    project's standing "never silently degrade" convention, the same
    treatment `_pair_by_seed` already applies on the seed axis."""
    before_seasons = tuple(s.season for s in before_run.seasons)
    after_seasons = tuple(s.season for s in after_run.seasons)
    if before_seasons != after_seasons:
        raise ValueError(
            f"rng_seed {before_run.rng_seed}: before/after runs do not carry the "
            f"same season-number sequence -- cannot hold window membership fixed "
            f"by season key (before: {before_seasons}, after: {after_seasons}). "
            "This typically means the two runs used a different --season-year or "
            "--seasons flag; the fixed-membership differ requires both runs to "
            "come from otherwise-identical harness invocations."
        )


def _substitute_window_membership(
    run: RunRecord,
    membership: Mapping[int, Mapping[int, CompetitiveWindow]],
    boundary_label: str = _WINDOW_BOUNDARY_LABEL,
) -> RunRecord:
    """A copy of `run` whose `boundary_label` boundary's `window_by_team` is
    replaced, per season (matched by `season.season`), by `membership`.
    Every other field -- rosters, cap sheets, acquisitions, pick holdings,
    ages -- is `run`'s own real data. Raises if any of `run`'s own seasons
    has no matching key in `membership`, rather than silently passing that
    season through unsubstituted (callers should have already called
    `_assert_same_season_sequence` so this branch is unreachable in
    practice; it stays as defense-in-depth for any future direct caller
    of this function)."""
    new_seasons = []
    for season in run.seasons:
        if season.season not in membership:
            raise ValueError(
                f"rng_seed {run.rng_seed}: season {season.season} has no matching "
                f"entry in the substituted membership (available seasons: "
                f"{sorted(membership)}) -- refusing to silently leave this season "
                "un-substituted."
            )
        replacement = membership[season.season]
        new_boundaries = tuple(
            dataclasses.replace(b, window_by_team=dict(replacement))
            if b.label == boundary_label
            else b
            for b in season.boundaries
        )
        new_seasons.append(dataclasses.replace(season, boundaries=new_boundaries))
    return dataclasses.replace(run, seasons=tuple(new_seasons))


def _philosophy_membership(run: RunRecord) -> dict[int, GmPhilosophy]:
    return dict(run.philosophy_by_team)


def _substitute_philosophy_membership(
    run: RunRecord, membership: Mapping[int, GmPhilosophy]
) -> RunRecord:
    """`philosophy_by_team` is a `RunRecord`-level field (assigned once at
    league generation, not re-derived per season) -- the substitution is a
    single whole-run replacement, not a per-season one."""
    return dataclasses.replace(run, philosophy_by_team=dict(membership))


# ---------------------------------------------------------------------------
# Run pairing
# ---------------------------------------------------------------------------


def _pair_by_seed(
    before: Sequence[RunRecord], after: Sequence[RunRecord]
) -> list[tuple[RunRecord, RunRecord]]:
    before_by_seed = {r.rng_seed: r for r in before}
    after_by_seed = {r.rng_seed: r for r in after}
    if len(before_by_seed) != len(before):
        raise ValueError("`before` has two or more RunRecords sharing an rng_seed")
    if len(after_by_seed) != len(after):
        raise ValueError("`after` has two or more RunRecords sharing an rng_seed")
    only_before = before_by_seed.keys() - after_by_seed.keys()
    only_after = after_by_seed.keys() - before_by_seed.keys()
    if only_before or only_after:
        raise ValueError(
            "before/after are not the same seed set -- cannot pair cells "
            f"unambiguously (only in before: {sorted(only_before)}, "
            f"only in after: {sorted(only_after)})"
        )
    return [(before_by_seed[seed], after_by_seed[seed]) for seed in sorted(before_by_seed)]


# ---------------------------------------------------------------------------
# Membership overlap reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MembershipOverlap:
    """One cohort category's before/after team-id sets for one comparison
    cell. `season` is the season number for the `"window"` basis, and
    `None` for the `"philosophy"` basis (a whole-run-level cohort)."""

    rng_seed: int
    season: int | None
    category: str
    before: frozenset[int]
    after: frozenset[int]

    @property
    def jaccard(self) -> float | None:
        union = self.before | self.after
        if not union:
            return None
        return len(self.before & self.after) / len(union)

    @property
    def unchanged(self) -> bool:
        return self.before == self.after


def _window_overlaps(
    paired: Sequence[tuple[RunRecord, RunRecord]], boundary_label: str
) -> tuple[MembershipOverlap, ...]:
    overlaps: list[MembershipOverlap] = []
    for before_run, after_run in paired:
        before_mem = _window_membership(before_run, boundary_label)
        after_mem = _window_membership(after_run, boundary_label)
        for season in sorted(set(before_mem) | set(after_mem)):
            b_by_team = before_mem.get(season, {})
            a_by_team = after_mem.get(season, {})
            categories = sorted(
                {w.value for w in b_by_team.values()} | {w.value for w in a_by_team.values()}
            )
            for category in categories:
                overlaps.append(
                    MembershipOverlap(
                        rng_seed=before_run.rng_seed,
                        season=season,
                        category=category,
                        before=frozenset(t for t, w in b_by_team.items() if w.value == category),
                        after=frozenset(t for t, w in a_by_team.items() if w.value == category),
                    )
                )
    return tuple(overlaps)


def _philosophy_overlaps(
    paired: Sequence[tuple[RunRecord, RunRecord]]
) -> tuple[MembershipOverlap, ...]:
    overlaps: list[MembershipOverlap] = []
    for before_run, after_run in paired:
        b_map = _philosophy_membership(before_run)
        a_map = _philosophy_membership(after_run)
        categories = sorted({p.value for p in b_map.values()} | {p.value for p in a_map.values()})
        for category in categories:
            overlaps.append(
                MembershipOverlap(
                    rng_seed=before_run.rng_seed,
                    season=None,
                    category=category,
                    before=frozenset(t for t, p in b_map.items() if p.value == category),
                    after=frozenset(t for t, p in a_map.items() if p.value == category),
                )
            )
    return tuple(overlaps)


# ---------------------------------------------------------------------------
# The differ itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixedMembershipDiffResult:
    """Architect round-17 report Sec.4.6 item 2's own decision rule, made
    concrete: read `fixed_membership_delta` -- if it is ~0, the metric's
    movement (`real_delta`) is pure cohort reshuffle (channel A); if it is
    ~`real_delta`, membership is irrelevant and whatever moved did so
    within stable cohorts (channel B or later). `membership_reshuffle_
    delta` (`real_delta - fixed_membership_delta`) isolates channel A's own
    contribution directly."""

    metric_id: str
    value_before: float | None
    value_after_real: float | None
    value_after_fixed_membership: float | None
    real_delta: float | None
    """`value_after_real - value_before` -- the movement actually observed."""
    fixed_membership_delta: float | None
    """`value_after_fixed_membership - value_before` -- the movement left
    over once cohort membership is held at the "before" run's own
    assignment. Near zero on two runs of the same underlying league state
    (see this module's own tests)."""
    membership_reshuffle_delta: float | None
    """`real_delta - fixed_membership_delta` -- channel A's own
    contribution: how much of the real movement is explained by cohort
    membership changing, as opposed to the measured quantities themselves
    changing within stable cohorts."""
    overlaps: tuple[MembershipOverlap, ...]
    membership_changed: bool
    """`True` iff any reported cohort's before/after team-id set differs on
    any cell."""
    n_paired_cells: int
    caveats: tuple[str, ...] = ()


def fixed_membership_diff(
    metric_id: str,
    before: Sequence[RunRecord],
    after: Sequence[RunRecord],
    bands: BandSpec,
    *,
    boundary_label: str = _WINDOW_BOUNDARY_LABEL,
) -> FixedMembershipDiffResult:
    """Architect round-17 report Sec.4.6 item 2, generalized to any of
    M11/M12/M13/M15/M16 (see module docstring for the excluded M14/M17 and
    the M11 partial-basis caveat). `before`/`after` are two seed-matched
    sets of `RunRecord`s -- typically the same base-seed run pickled before
    and after a build change. Empty `before`/`after` is not a special case
    here: `_pair_by_seed` returns an empty pairing, and the underlying
    `_mN_core` on zero runs reports its own already-documented `undefined`/
    `None` value (e.g. `_m12_core`'s empty-cohort guard) -- this function
    does not add a second, redundant empty-input check on top of that."""
    if metric_id not in _CORE_BY_METRIC:
        raise ValueError(
            f"{metric_id!r} has no registered cohort basis for the fixed-membership "
            f"differ -- supported: {sorted(_SUPPORTED_METRICS)}"
        )
    core = _CORE_BY_METRIC[metric_id]
    basis = _BASIS_BY_METRIC[metric_id]
    band = bands.metrics[metric_id]

    paired = _pair_by_seed(list(before), list(after))
    before_runs = tuple(b for b, _ in paired)
    after_runs = tuple(a for _, a in paired)

    if basis == "window":
        for before_run, after_run in paired:
            _assert_same_season_sequence(before_run, after_run)
        overlaps = _window_overlaps(paired, boundary_label)
        counterfactual_after = tuple(
            _substitute_window_membership(
                after_run, _window_membership(before_run, boundary_label), boundary_label
            )
            for before_run, after_run in paired
        )
        n_paired_cells = len({(o.rng_seed, o.season) for o in overlaps})
    else:
        overlaps = _philosophy_overlaps(paired)
        counterfactual_after = tuple(
            _substitute_philosophy_membership(after_run, _philosophy_membership(before_run))
            for before_run, after_run in paired
        )
        n_paired_cells = len(paired)

    value_before = core(before_runs, band).value
    value_after_real = core(after_runs, band).value
    value_after_fixed = core(counterfactual_after, band).value

    def _delta(a: float | None, b: float | None) -> float | None:
        return (a - b) if a is not None and b is not None else None

    real_delta = _delta(value_after_real, value_before)
    fixed_delta = _delta(value_after_fixed, value_before)
    reshuffle_delta = (
        _delta(real_delta, fixed_delta)
        if real_delta is not None and fixed_delta is not None
        else None
    )

    return FixedMembershipDiffResult(
        metric_id=metric_id,
        value_before=value_before,
        value_after_real=value_after_real,
        value_after_fixed_membership=value_after_fixed,
        real_delta=real_delta,
        fixed_membership_delta=fixed_delta,
        membership_reshuffle_delta=reshuffle_delta,
        overlaps=overlaps,
        membership_changed=any(not o.unchanged for o in overlaps),
        n_paired_cells=n_paired_cells,
        caveats=_CAVEATS_BY_METRIC.get(metric_id, ()),
    )
