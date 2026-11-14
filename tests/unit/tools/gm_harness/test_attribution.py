"""`tools/gm_harness/attribution.py` -- the Family-3 fixed-membership
counterfactual differ (Phase 3 design doc Sec.11 Stage A step 3; full spec
architect round-17 report Sec.4.6 item 2). Direct unit tests against
hand-built `RunRecord` fixtures, no real run behind any of these, matching
`test_metrics.py`'s own fixture-builder conventions (this file duplicates
the small builders locally rather than importing them from that file --
neither module exports them, and a shared conftest would be a bigger
change than this slice's own scope for two files' worth of ~10-line
helpers).

Per the dispatch's own verify bar:
- **Zero movement / zero repartitioning on two runs of the same seed** --
  `test_identical_runs_report_zero_repartitioning_and_zero_movement`.
- **Separation on a deliberately perturbed run** -- two complementary
  cases, since "separates the two" means the tool can tell a pure
  membership reshuffle apart from a pure behavioural change, not just
  that it reports *some* nonzero number:
  `test_pure_membership_reshuffle_is_fully_absorbed_by_the_counterfactual`
  and
  `test_pure_behavioural_change_survives_the_counterfactual_unabsorbed`.
"""
from __future__ import annotations

import dataclasses
import pickle
from pathlib import Path

import pytest

from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy
from dba2.managers.tuning import CompetitiveWindow
from tools.gm_harness.attribution import (
    FixedMembershipDiffResult,
    _pair_by_seed,
    fixed_membership_diff,
)
from tools.gm_harness.bands import load_bands
from tools.gm_harness.season import Acquisition, BoundaryRecord, RunRecord, SeasonRecord

_BANDS = load_bands()

_LABEL_PHASE = {
    "PRESEASON": Phase.PRESEASON,
    "post_trade_window_1": Phase.REGULAR_SEASON,
    "TRADE_DEADLINE": Phase.TRADE_DEADLINE,
    "post_draft": Phase.DRAFT,
    "free_agency_open": Phase.FREE_AGENCY,
    "season_end": Phase.FREE_AGENCY,
    "post_waivers": Phase.WAIVERS,
}


def _boundary(
    label: str, *, window_by_team: dict | None = None, roster_ids_by_team: dict | None = None
) -> BoundaryRecord:
    return BoundaryRecord(
        phase=_LABEL_PHASE[label], label=label, roster_ids_by_team=roster_ids_by_team or {},
        overalls_by_team={}, window_by_team=window_by_team or {}, capsheet_by_team={},
        pick_owner_by_pick={}, contract_count_by_player={},
    )


def _season_record(season: int, window_by_team: dict, **overrides) -> SeasonRecord:
    defaults = dict(
        season=season, boundaries=(_boundary("season_end", window_by_team=window_by_team),),
        trades_by_window=((), ()), packages_evaluated=(0, 0), mutual_accepts=(0, 0),
        signings=(), draft_picks=(), waivers=(), acquisitions=(),
        minutes_weighted_age_by_team={}, def_group_mean_by_player={},
        future_first_net_by_team={}, win_totals=None, jsonl_lines=(),
        conservation_violations=(), reconstruction_violations=(), phase_timings={},
        # Phase 3 §11 step 7 -- Decision P3-12's six new TradeWindowResult
        # counters, plus M25's live invariant count, defaulting to a
        # counters-off reading (this test file predates the negotiation loop).
        turns_evaluated=(0, 0), evaluations_performed=(0, 0), counter_opportunities=(0, 0),
        counters_admitted=(0, 0), counter_closed_executions=(0, 0),
        outcome_census=({}, {}), expired_stalled_count=(0, 0),
        negotiation_depth_violations=(0, 0),
    )
    defaults.update(overrides)
    return SeasonRecord(**defaults)


def _run_record(
    rng_seed: int, seasons: list[SeasonRecord], *, philosophy_by_team=None
) -> RunRecord:
    return RunRecord(
        rng_seed=rng_seed, philosophy_by_team=philosophy_by_team or {},
        season1_top1_minus_8th_by_team={}, seasons=tuple(seasons),
    )


# ---------------------------------------------------------------------------
# M12 fixtures -- window_by_team basis, the metric Sec.4.6 was written for.
# ---------------------------------------------------------------------------


def _m12_run(rng_seed: int, window_by_team: dict, future_first_net_by_team: dict) -> RunRecord:
    season = _season_record(2026, window_by_team, future_first_net_by_team=future_first_net_by_team)
    return _run_record(rng_seed, [season])


def test_identical_runs_report_zero_repartitioning_and_zero_movement():
    """Two structurally-identical (but distinct-object) runs of the same
    seed: the differ must report no membership change and no metric
    delta at all -- this is the sanity check that the tool does not
    manufacture false signal on identical inputs."""
    window = {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}
    net = {1: 3, 2: -1}
    before = (_m12_run(7, dict(window), dict(net)),)
    after = (_m12_run(7, dict(window), dict(net)),)  # distinct dict/RunRecord objects, same values

    result = fixed_membership_diff("M12", before, after, _BANDS)

    assert result.membership_changed is False
    assert all(o.unchanged for o in result.overlaps)
    assert all(o.jaccard == pytest.approx(1.0) for o in result.overlaps if o.before or o.after)
    assert result.value_before == pytest.approx(result.value_after_real)
    assert result.value_before == pytest.approx(result.value_after_fixed_membership)
    assert result.real_delta == pytest.approx(0.0)
    assert result.fixed_membership_delta == pytest.approx(0.0)
    assert result.membership_reshuffle_delta == pytest.approx(0.0)


def test_pure_membership_reshuffle_is_fully_absorbed_by_the_counterfactual():
    """Deliberate perturbation 1: team 2 is reclassified CONTEND -> REBUILD
    between "before" and "after", and the underlying pick-holding data
    (`future_first_net_by_team`) is untouched. Channel A only. The
    counterfactual (after's data, before's membership) must reproduce the
    BEFORE value almost exactly -- `fixed_membership_delta ~= 0` even
    though `real_delta` is large and nonzero. This is "separation": the
    tool must not just report a nonzero number, it must show the
    perturbation is *membership*, not behaviour."""
    net = {1: 3, 2: -1, 3: 5, 4: -2}
    before_window = {
        1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND,
        3: CompetitiveWindow.REBUILD, 4: CompetitiveWindow.CONTEND,
    }
    after_window = {
        1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.REBUILD,  # 2 moved cohorts
        3: CompetitiveWindow.REBUILD, 4: CompetitiveWindow.CONTEND,
    }
    before = (_m12_run(7, before_window, net),)
    after = (_m12_run(7, after_window, net),)  # same data, different membership

    result = fixed_membership_diff("M12", before, after, _BANDS)

    assert result.membership_changed is True
    contend_overlap = next(o for o in result.overlaps if o.category == "contend")
    assert contend_overlap.before == frozenset({2, 4})
    assert contend_overlap.after == frozenset({4})
    assert contend_overlap.unchanged is False

    # Real movement is nonzero (team 2's data left the CONTEND pool).
    assert result.real_delta != pytest.approx(0.0)
    # But holding membership fixed at "before" reproduces "before" exactly,
    # since the counterfactual literally recomputes on before's own
    # partition of after's (identical) data.
    assert result.value_after_fixed_membership == pytest.approx(result.value_before)
    assert result.fixed_membership_delta == pytest.approx(0.0)
    # Channel A explains the whole of the real movement.
    assert result.membership_reshuffle_delta == pytest.approx(result.real_delta)


def test_pure_behavioural_change_survives_the_counterfactual_unabsorbed():
    """Deliberate perturbation 2, the complementary case: cohort membership
    is IDENTICAL before/after, but the measured quantity itself
    (future_first_net_by_team) changes -- e.g. team 2 stops trading away
    its own picks. Channel B only. Since membership never moved, the
    counterfactual is a no-op and `fixed_membership_delta` must equal
    `real_delta` exactly, with `membership_reshuffle_delta == 0` -- the
    complementary proof that the tool is not vacuously reporting "no
    difference" or attributing every movement to membership."""
    window = {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}
    before = (_m12_run(7, dict(window), {1: 3, 2: -5}),)
    after = (_m12_run(7, dict(window), {1: 3, 2: 1}),)  # team 2 stopped shedding picks

    result = fixed_membership_diff("M12", before, after, _BANDS)

    assert result.membership_changed is False
    assert result.real_delta != pytest.approx(0.0)
    assert result.fixed_membership_delta == pytest.approx(result.real_delta)
    assert result.membership_reshuffle_delta == pytest.approx(0.0)


def test_multi_seed_runs_are_paired_by_rng_seed_not_list_position():
    """A discriminating pairing test: `real_delta`/`value_before`/`value_
    after_real` are pooled over every run regardless of order, so they
    cannot tell correct seed-pairing apart from positional mis-pairing --
    only the COUNTERFACTUAL can, since it substitutes one specific run's
    own "before" membership into one specific run's own "after" data. Seed
    1 and seed 2 are given deliberately SWAPPED before-membership
    (REBUILD/CONTEND flipped) so that pairing seed 1's after-data with
    seed 2's before-membership (the wrong pairing a positional zip would
    produce once the lists are given out of order) yields the opposite
    sign from the correct, seed-matched counterfactual."""
    seed1_before = _m12_run(
        1, {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}, {1: 0, 2: 0}
    )
    seed1_after = _m12_run(
        1, {1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}, {1: 100, 2: -100}
    )
    seed2_before = _m12_run(
        2, {1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}, {1: 0, 2: 0}
    )
    seed2_after = _m12_run(
        2, {1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}, {1: 5, 2: -5}
    )

    # Lists given out of rng_seed order on both sides.
    before = (seed2_before, seed1_before)
    after = (seed1_after, seed2_after)

    result = fixed_membership_diff("M12", before, after, _BANDS)

    # Correct pairing: seed 1's after-data (100/-100) classified by seed
    # 1's OWN before-membership (1=rebuild,2=contend) -> rebuild=[100],
    # contend=[-100]; seed 2's after-data (5/-5) classified by seed 2's
    # own before-membership (1=contend,2=rebuild) -> contend=[5],
    # rebuild=[-5]. Pooled: rebuild=[100,-5] mean=47.5, contend=[-100,5]
    # mean=-47.5, value = 95.0. A positional (list-order) mis-pairing would
    # cross the two seeds' memberships and data and produce -95.0 instead.
    assert result.value_after_fixed_membership == pytest.approx(95.0)


def test_pair_by_seed_raises_on_mismatched_seed_sets():
    before = (_m12_run(1, {1: CompetitiveWindow.REBUILD}, {1: 1}),)
    after = (_m12_run(2, {1: CompetitiveWindow.REBUILD}, {1: 1}),)
    with pytest.raises(ValueError, match="not the same seed set"):
        _pair_by_seed(before, after)


def test_pair_by_seed_raises_on_duplicate_seed_within_one_side():
    before = (
        _m12_run(1, {1: CompetitiveWindow.REBUILD}, {1: 1}),
        _m12_run(1, {1: CompetitiveWindow.CONTEND}, {1: 2}),
    )
    after = (_m12_run(1, {1: CompetitiveWindow.REBUILD}, {1: 1}),)
    with pytest.raises(ValueError, match="sharing an rng_seed"):
        _pair_by_seed(before, after)


def test_unsupported_metric_id_raises():
    run = (_m12_run(1, {1: CompetitiveWindow.REBUILD}, {1: 1}),)
    with pytest.raises(ValueError, match="M17"):
        fixed_membership_diff("M17", run, run, _BANDS)
    with pytest.raises(ValueError, match="M14"):
        fixed_membership_diff("M14", run, run, _BANDS)


# ---------------------------------------------------------------------------
# B1 (critic-round fix) -- season-number mismatch must raise, never silently
# no-op. `--season-year`/`--seasons` are user-settable, so two runs meant to
# be compared can carry partially or fully disjoint (or reordered) season
# numbers.
# ---------------------------------------------------------------------------


def _m12_two_season_run(rng_seed: int, season_numbers: tuple[int, int], window: dict) -> RunRecord:
    seasons = [
        _season_record(n, window, future_first_net_by_team={1: 1, 2: -1}) for n in season_numbers
    ]
    return _run_record(rng_seed, seasons)


def test_fully_disjoint_season_numbers_raise_instead_of_no_opping():
    """The critic's own repro: before=season 2026, after=season 2030 --
    fully disjoint. Before the fix this was a total no-op that reported
    `value_after_fixed_membership == value_after_real` and
    `membership_reshuffle_delta == 0.0` with no caveat -- the module's own
    highest-confidence "channel B, not membership" verdict, emitted from a
    counterfactual that was never computed. It must now raise."""
    before = (
        _m12_run(1, {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}, {1: 1, 2: -1}),
    )
    after_season = _season_record(
        2030, {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND},
        future_first_net_by_team={1: 1, 2: -1},
    )
    after = (_run_record(1, [after_season]),)
    with pytest.raises(ValueError, match="season-number sequence"):
        fixed_membership_diff("M12", before, after, _BANDS)


def test_partial_season_overlap_raises_rather_than_silently_blending():
    """Before has seasons (2026, 2027); after has (2026, 2028) -- one
    season lines up, one doesn't. Before the fix this would silently
    substitute season 2026 and pass season 2028 through un-substituted
    while `n_paired_cells` still reported 2, as if both cells were really
    held fixed."""
    before = _m12_two_season_run(
        1, (2026, 2027), {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}
    )
    after = _m12_two_season_run(
        1, (2026, 2028), {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}
    )
    with pytest.raises(ValueError, match="season-number sequence"):
        fixed_membership_diff("M12", (before,), (after,), _BANDS)


def test_reordered_same_season_set_raises_even_though_every_number_is_present():
    """Before=(2026, 2027, 2028), after=(2027, 2026, 2028) -- the identical
    SET of season numbers, but in a different order. A set-only check
    would pass this silently; `_m13_core` reads its season *positionally*
    (`run.seasons[resolves_at - 1]`), so a reordered sequence would still
    substitute the wrong season's membership for that positional read even
    though the substitution's own key lookup "succeeds." Must raise."""
    window = {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}
    net = {1: 1, 2: -1}
    before = _run_record(
        1, [_season_record(n, window, future_first_net_by_team=net) for n in (2026, 2027, 2028)]
    )
    after = _run_record(
        1, [_season_record(n, window, future_first_net_by_team=net) for n in (2027, 2026, 2028)]
    )
    with pytest.raises(ValueError, match="season-number sequence"):
        fixed_membership_diff("M12", (before,), (after,), _BANDS)


def test_m13_positional_read_is_correctly_fixed_when_season_sequences_match():
    """`_m13_core` reads `run.seasons[resolves_at_season - 1]` positionally
    (band default `resolves_at_season = 3`), not by looking up `season.
    season == 3` -- this test proves the by-key substitution still lands
    on the exact season M13 actually reads once `_assert_same_season_
    sequence` has confirmed before/after share one sequence, including
    for a run whose season *labels* don't start at 1 (2026/2027/2028, not
    1/2/3), matching this codebase's own real season-numbering scheme."""
    cohort_a = CompetitiveWindow.CONTEND  # M13 band cohorts[0]
    cohort_b = CompetitiveWindow.REBUILD  # M13 band cohorts[1]
    # Deliberately the OPPOSITE assignment from season 2028's own map below,
    # so a substitution that lands on the wrong season (2026 or 2027 instead
    # of 2028) produces a DIFFERENT answer rather than the same one by
    # coincidence -- a filler map identical to the read season's map would
    # let a wrong-season substitution pass this test undetected.
    filler_window = {1: cohort_b, 2: cohort_a}  # irrelevant seasons 1-2; only season 3 is read

    before_s3 = _season_record(
        2028, {1: cohort_a, 2: cohort_b}, minutes_weighted_age_by_team={1: 30.0, 2: 25.0}
    )
    before = _run_record(
        1,
        [
            _season_record(2026, filler_window),
            _season_record(2027, filler_window),
            before_s3,
        ],
    )
    # Same underlying ages, cohorts swapped at the one season M13 reads.
    after_s3 = _season_record(
        2028, {1: cohort_b, 2: cohort_a}, minutes_weighted_age_by_team={1: 30.0, 2: 25.0}
    )
    after = _run_record(
        1,
        [
            _season_record(2026, filler_window),
            _season_record(2027, filler_window),
            after_s3,
        ],
    )

    result = fixed_membership_diff("M13", (before,), (after,), _BANDS)

    assert result.value_before == pytest.approx(30.0 - 25.0)
    # Real movement: cohorts swapped -> value flips sign.
    assert result.value_after_real == pytest.approx(25.0 - 30.0)
    # Fixed-membership counterfactual: after's (unchanged) ages reclassified
    # by before's season-2028 membership -> reproduces value_before exactly,
    # proving the substitution landed on season 2028 (the one M13 actually
    # reads). Because filler_window is now the OPPOSITE of season 2028's
    # map, a substitution that wrongly used season 2026 or 2027's map
    # instead would flip the sign here rather than passing by coincidence.
    assert result.value_after_fixed_membership == pytest.approx(result.value_before)
    assert result.membership_reshuffle_delta == pytest.approx(result.real_delta)


# ---------------------------------------------------------------------------
# M11 -- the disclosed partial-basis caveat (denominator only).
# ---------------------------------------------------------------------------


def test_m11_only_substitutes_the_team_season_denominator_not_the_acquisition_numerator():
    """M11's numerator reads each Acquisition's own `window_at_acquisition`
    (captured live, not from `window_by_team`) -- this instrument does not
    and cannot substitute it (module docstring). A discriminating check
    that this limitation is real and disclosed, not accidentally already
    covered: build a case where ONLY the acquisition-level window differs
    between before/after (team-season `window_by_team` is identical), and
    confirm the counterfactual does NOT reproduce the before value --
    proving the numerator genuinely passed through un-substituted."""
    window = {1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}
    acq_before = Acquisition(team_id=1, player_id=1, overall=85, age=27, channel="trade",
                              window_at_acquisition=CompetitiveWindow.CONTEND)
    acq_after = Acquisition(team_id=1, player_id=1, overall=85, age=27, channel="trade",
                             window_at_acquisition=CompetitiveWindow.REBUILD)
    acq_lo = Acquisition(team_id=2, player_id=2, overall=82, age=24, channel="draft",
                          window_at_acquisition=CompetitiveWindow.REBUILD)

    before = (_run_record(1, [_season_record(2026, window, acquisitions=(acq_before, acq_lo))]),)
    after = (_run_record(1, [_season_record(2026, window, acquisitions=(acq_after, acq_lo))]),)

    result = fixed_membership_diff("M11", before, after, _BANDS)

    assert result.caveats  # disclosed, not silent
    assert result.membership_changed is False  # window_by_team itself never moved
    # But the metric's own value DID move (numerator's window_at_acquisition
    # changed which cohort the acquisition counts against), and the
    # counterfactual -- which only fixes window_by_team -- cannot undo that.
    assert result.value_before != pytest.approx(result.value_after_real)
    assert result.value_after_fixed_membership == pytest.approx(result.value_after_real)


# ---------------------------------------------------------------------------
# M15/M16 -- the philosophy (run-level) basis.
# ---------------------------------------------------------------------------


def test_m16_philosophy_reshuffle_is_absorbed_by_the_counterfactual():
    """Decision P3-47 step 1 pinned `_m16_core` to `params.resolves_at_
    season` (5 in the real, live `_BANDS` this test uses) -- the def-group
    data has to sit on the 5th season, not the 1st, or the pin drops this
    single-season-shaped fixture entirely (`dropped_runs == 1`, `value is
    None`) before the counterfactual mechanism this test is about ever
    runs."""
    boundary = _boundary("season_end", roster_ids_by_team={1: (101, 102), 2: (201, 202)})
    season5 = _season_record(
        2030, {}, boundaries=(boundary,),
        def_group_mean_by_player={101: 90.0, 102: 90.0, 201: 50.0, 202: 50.0},
    )
    earlier_seasons = [_season_record(2026 + i, {}) for i in range(4)]
    before = (_run_record(
        1, [*earlier_seasons, season5],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    ),)
    # Same data, but team 1 (the strong-defense roster) is no longer the
    # defense_first team in the "after" league generation -- a pure
    # cohort-assignment reshuffle with the underlying rosters/stats fixed.
    after = (_run_record(
        1, [*earlier_seasons, season5],
        philosophy_by_team={1: GmPhilosophy.EGALITARIAN, 2: GmPhilosophy.DEFENSE_FIRST},
    ),)

    result = fixed_membership_diff("M16", before, after, _BANDS)

    assert result.membership_changed is True
    assert result.real_delta != pytest.approx(0.0)
    assert result.value_after_fixed_membership == pytest.approx(result.value_before)
    assert result.fixed_membership_delta == pytest.approx(0.0)
    assert result.membership_reshuffle_delta == pytest.approx(result.real_delta)


def test_m15_identical_philosophy_assignment_across_runs_has_no_caveats_and_zero_movement():
    acq_vet = Acquisition(team_id=1, player_id=1, overall=80, age=34, channel="signing",
                           window_at_acquisition=CompetitiveWindow.RETOOL)
    acq_young = Acquisition(team_id=2, player_id=2, overall=80, age=21, channel="draft",
                             window_at_acquisition=CompetitiveWindow.RETOOL)
    season = _season_record(2026, {}, acquisitions=(acq_vet, acq_young))
    philosophy = {1: GmPhilosophy.VET_OVERRATER, 2: GmPhilosophy.YOUTH_DEVELOPER}
    before = (_run_record(1, [season], philosophy_by_team=dict(philosophy)),)
    after = (_run_record(1, [season], philosophy_by_team=dict(philosophy)),)

    result = fixed_membership_diff("M15", before, after, _BANDS)

    assert result.caveats == ()
    assert result.membership_changed is False
    assert result.real_delta == pytest.approx(0.0)
    assert result.fixed_membership_delta == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_result_is_the_documented_dataclass_with_metric_id_stamped():
    run = (
        _m12_run(1, {1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND}, {1: 1, 2: -1}),
    )
    result = fixed_membership_diff("M12", run, run, _BANDS)
    assert isinstance(result, FixedMembershipDiffResult)
    assert result.metric_id == "M12"
    assert result.n_paired_cells == 1


# ---------------------------------------------------------------------------
# Integration: real RunRecord pickles, not just hand-built fixtures.
# `artifacts/validation_gm/m14_seeds_v2/` is gitignored (per HANDOFF.md) --
# absent in a fresh clone or an isolated worktree that hasn't run a real
# sim, so this section skips rather than fails when it isn't present. Where
# it IS present, this is the same class of check the critic ran by hand
# against these same pickles (identity case: all deltas exactly 0.0; a real
# CONTEND<->REBUILD reshuffle: fully absorbed on M12).
# ---------------------------------------------------------------------------


def _real_pickle_dir() -> Path | None:
    """Locates `artifacts/validation_gm/m14_seeds_v2/` relative to whichever
    checkout this test file lives in. Handles the `.claude/worktrees/
    <agent>/` layout (this file's own environment) by also checking the
    shared main checkout that a worktree's `.claude/worktrees/<agent>`
    directory sits three levels under."""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    for parent in here.parents:
        candidates.append(parent / "artifacts" / "validation_gm" / "m14_seeds_v2")
        if parent.parent.name == "worktrees":
            candidates.append(
                parent.parent.parent / "artifacts" / "validation_gm" / "m14_seeds_v2"
            )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


_REAL_PICKLE_DIR = _real_pickle_dir()


def _load_real_run(path: Path) -> RunRecord:
    with path.open("rb") as f:
        return pickle.load(f)


@pytest.mark.skipif(
    _REAL_PICKLE_DIR is None,
    reason="artifacts/validation_gm/m14_seeds_v2/ not present (gitignored)",
)
def test_real_pickles_identity_is_zero_and_a_real_reshuffle_is_fully_absorbed():
    pickle_paths = sorted(_REAL_PICKLE_DIR.glob("seed_*.pkl"))
    if not pickle_paths:
        pytest.skip("no seed_*.pkl files found under m14_seeds_v2/")
    runs = tuple(_load_real_run(p) for p in pickle_paths[:3])

    # Identity: real runs diffed against themselves report zero everything.
    identity = fixed_membership_diff("M12", runs, runs, _BANDS)
    assert identity.membership_changed is False
    assert identity.real_delta == pytest.approx(0.0)
    assert identity.fixed_membership_delta == pytest.approx(0.0)
    assert identity.membership_reshuffle_delta == pytest.approx(0.0)

    # A real, deliberate reshuffle built ON TOP of real data: swap two
    # teams' season_end window classification in the final season of a
    # real run, leaving every real quantity (pick holdings, ages, rosters,
    # every other season) untouched, and confirm it is fully absorbed by
    # the counterfactual -- the same "separation" property proven on
    # hand-built fixtures above, now on the real M12 aggregation over a
    # real 30-team league.
    base_run = runs[0]
    last_season = base_run.seasons[-1]
    boundary = next(b for b in last_season.boundaries if b.label == "season_end")
    window = dict(boundary.window_by_team)
    contend_team = next(t for t, w in window.items() if w == CompetitiveWindow.CONTEND)
    rebuild_team = next(t for t, w in window.items() if w == CompetitiveWindow.REBUILD)
    window[contend_team], window[rebuild_team] = window[rebuild_team], window[contend_team]

    new_boundary = dataclasses.replace(boundary, window_by_team=window)
    new_boundaries = tuple(
        new_boundary if b.label == "season_end" else b for b in last_season.boundaries
    )
    reshuffled_season = dataclasses.replace(last_season, boundaries=new_boundaries)
    reshuffled_seasons = tuple(
        reshuffled_season if s is last_season else s for s in base_run.seasons
    )
    reshuffled_run = dataclasses.replace(base_run, seasons=reshuffled_seasons)

    reshuffled = fixed_membership_diff("M12", (base_run,), (reshuffled_run,), _BANDS)
    assert reshuffled.membership_changed is True
    assert reshuffled.real_delta != pytest.approx(0.0)
    assert reshuffled.value_after_fixed_membership == pytest.approx(reshuffled.value_before)
    assert reshuffled.fixed_membership_delta == pytest.approx(0.0)
    assert reshuffled.membership_reshuffle_delta == pytest.approx(reshuffled.real_delta)
