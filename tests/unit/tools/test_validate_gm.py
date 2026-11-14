"""`tools/validate_gm.py` -- T7b critic-round B2: zero test coverage on the
CLI shell/report writer/GO-NO-GO decision rule itself before this file
existed (every prior test in this slice targeted `metrics.py`/`bands.py`
only). Covers `run_validation`'s own wiring (B1's determinism-isolation
fix), the GO/NO-GO decision rule, `--skip-games`'s forced NO-GO, and
`write_report`'s own output shape -- per this slice's own dispatch.

**Deliberately avoids a real `run_seed` call anywhere** (a real seed/season
run costs 10+ minutes with games, several minutes even skip-games) --
`run_seed` and `compute_all` are monkeypatched to `tools.validate_gm`'s own
imported names, so every test here runs in well under a second while still
exercising `run_validation`'s real body (the loop, the determinism-check
branch, the GO/NO-GO rule, `write_report`'s call), not a reimplementation
of it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import statistics

import pytest

import tools.validate_gm as validate_gm
from dba2.managers.voice.select import PhraseTemplate
from dba2.services.media_service import DEFAULT_MEDIA_PHRASE_LIBRARY_PATH
from dba2.services.phrase_service import LoadedPhraseLibrary, load_library
from tools.gm_harness.bands import DEFAULT_BANDS_PATH, load_bands
from tools.gm_harness.media_corpus import MediaCorpusArm, MediaCorpusTotals
from tools.gm_harness.metrics import MetricResult
from tools.gm_harness.season import RunRecord

_BANDS = load_bands(DEFAULT_BANDS_PATH)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _fake_run_seed(
    *, league_id, rng_seed, first_season, seasons, players_per_team, skip_games, tuning,
    max_negotiation_rounds=None, head_to_head_enabled=True, phrase_library=None,
    agency_enabled=True, media_enabled=True,
) -> RunRecord:
    """Instant stand-in for the real (10+ minute) `run_seed` -- `run_
    validation`'s own loop only needs a `RunRecord` with the right
    `rng_seed`/`seasons` shape to exercise its own wiring; the metric
    VALUES are `compute_all`'s job, separately mocked below.
    `max_negotiation_rounds` (Phase 3 §11 step 7) and `head_to_head_
    enabled` (Decision P3-48) accepted and ignored -- the M29 control pass
    calls `run_seed` with both explicitly, and this fake's own signature
    must not reject a keyword the real function accepts. `phrase_library`
    (§11 step 16) is the identical accept-and-ignore case: `run_validation`
    now passes a real, loaded library to every main-loop call and its own
    null-control pass -- this fake needs no dialogue behavior, just to not
    reject the keyword. `agency_enabled`/`media_enabled` (Decision P4-2,
    pinned at the M29 control-run site per Decision P4-55) are the same
    accept-and-ignore case again -- this fake's signature must not reject
    them either."""
    return RunRecord(
        rng_seed=rng_seed, philosophy_by_team={}, season1_top1_minus_8th_by_team={}, seasons=(),
    )


def _metric_result(
    id_: str, *, family: int = 1, number: int = 1, passed: bool | None = True,
    counted_in_go: bool = True, status: str | None = None, value: float | None = 1.0,
) -> MetricResult:
    if status is None:
        status = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
    return MetricResult(
        id=id_, number=number, family=family, name=id_, comparator="min", unit="unit",
        band_low=0.0, band_high=None, value=value, detail={}, per_season=None, status=status,
        passed=passed, counted_in_go=counted_in_go, rationale="test fixture",
    )


class _ComputeAllSpy:
    """Records every call `run_validation` makes to `compute_all` (args and
    kwargs) and returns a pre-set `results` tuple -- lets a test assert on
    exactly what `run_validation` passed through (B1's own wiring: `runs`
    must never include `determinism_runs`) without needing a real 22-metric
    computation."""

    def __init__(self, results: tuple[MetricResult, ...]):
        self.results = results
        self.calls: list[dict] = []

    def __call__(
        self, runs, bands, *, determinism_runs=(), include_metric_17=True,
        control_mismatches=None, control_totals=None,
        m27_control_by_philosophy_template=None,
        media_corpus=None,
    ):
        self.calls.append(
            {
                "runs": runs, "bands": bands, "determinism_runs": determinism_runs,
                "include_metric_17": include_metric_17, "control_mismatches": control_mismatches,
                "control_totals": control_totals,
                "m27_control_by_philosophy_template": m27_control_by_philosophy_template,
                "media_corpus": media_corpus,
            }
        )
        return self.results


def _run(monkeypatch, tmp_path, results, *, seeds=1, seasons=0, skip_games=False,
          determinism_check=False,
          control_check=False,
          family3_precert=True,
          media_corpus_path=None,
          use_media_corpus=True) -> tuple[tuple[MetricResult, ...], bool, _ComputeAllSpy]:
    spy = _ComputeAllSpy(results)
    monkeypatch.setattr(validate_gm, "run_seed", _fake_run_seed)
    monkeypatch.setattr(validate_gm, "compute_all", spy)
    out_results, go = validate_gm.run_validation(
        seeds=seeds, seasons=seasons, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=skip_games, determinism_check=determinism_check, control_check=control_check,
        family3_precert=family3_precert,
        protected_pick_share=0.0,
        draft_divergence_drafts=1, bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
        media_corpus_path=media_corpus_path, use_media_corpus=use_media_corpus,
    )
    return out_results, go, spy


# ---------------------------------------------------------------------------
# B1's own wiring: determinism_runs must never merge into runs
# ---------------------------------------------------------------------------


def test_run_validation_never_concatenates_determinism_runs_into_runs(monkeypatch, tmp_path):
    """The literal B1 bug this slice fixed: `run_validation` used to build
    `tuple(runs) + determinism_runs` and hand the COMBINED tuple to
    `compute_all` as its one `runs` argument. Asserts the real call
    `compute_all` receives has `runs` sized exactly `seeds` (no duplicate
    seeds folded in) and `determinism_runs` passed as its own, separate
    keyword argument."""
    results = (_metric_result("M6", family=1, number=6),)
    _, _, spy = _run(monkeypatch, tmp_path, results, seeds=2, determinism_check=True)
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert len(call["runs"]) == 2, "runs must carry exactly the 2 requested seeds, no more"
    assert len(call["determinism_runs"]) == 2, "the det-check pair must still reach compute_all"
    # The determinism pair is its own 2-tuple (both entries re-running
    # seed-index-0's own rng_seed, by design) -- it must never be folded
    # into `runs` itself, which is exactly the B1 bug this asserts against.
    det_seeds = {r.rng_seed for r in call["determinism_runs"]}
    assert len(det_seeds) == 1, "both determinism runs must share seed-index-0's own rng_seed"


def test_run_validation_determinism_runs_empty_when_check_disabled(monkeypatch, tmp_path):
    results = (_metric_result("M1"),)
    _, _, spy = _run(monkeypatch, tmp_path, results, seeds=1, determinism_check=False)
    assert spy.calls[0]["determinism_runs"] == ()


def test_determinism_runs_own_wall_clock_cost_is_recorded_in_the_report(monkeypatch, tmp_path):
    """Nit 9 (T7b critic round-2): the two `run_seed` calls `--determinism-
    check` makes are real, costly wall-clock work that `phase_timings_by_
    seed` never recorded at all before this fix -- `elapsed_seconds` (the
    report's own total) already included their cost, so the per-seed
    breakdown silently didn't sum to the total a reader would reconcile it
    against. Confirms both determinism runs now get their own entry."""
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results, seeds=1, determinism_check=True)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    seed_indices = {row["seed_index"] for row in report["phase_timings_by_seed"]}
    assert "determinism_a" in seed_indices
    assert "determinism_b" in seed_indices
    # 1 real seed + 2 determinism runs = 3 timing rows total.
    assert len(report["phase_timings_by_seed"]) == 3
    for row in report["phase_timings_by_seed"]:
        assert isinstance(row["elapsed_seconds"], float)


# ---------------------------------------------------------------------------
# GO/NO-GO decision rule
# ---------------------------------------------------------------------------


def test_run_validation_go_when_every_counted_metric_passes(monkeypatch, tmp_path):
    """`seeds=3, seasons=5` deliberately meets the bands file's own real
    `exit_criterion` (Decision P2-22) -- below-criterion would force NO-GO
    regardless of metric results (see the exit-criterion tests below), so a
    GO test must clear both gates to actually exercise the "all metrics
    pass" path."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M2", passed=True, counted_in_go=True),
        # A non-counted metric that would fail the rule if wrongly included.
        _metric_result("M21", passed=False, counted_in_go=False, status="FAIL (interpreted)"),
        # A SKIP (passed=None, not counted) must not block GO either.
        _metric_result("M22", passed=None, counted_in_go=False, status="SKIP", value=None),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=3, seasons=5)
    assert go is True
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is True
    assert "GO:" in report["go_reason"]
    assert report["summary"]["passed"] == 2
    assert report["summary"]["failed"] == 0


def test_run_validation_no_go_when_a_counted_metric_fails(monkeypatch, tmp_path):
    """`seeds=3, seasons=5` meets the exit criterion so the failing-metric
    path (not the exit-criterion gate) is what actually produces NO-GO
    here -- see the exit-criterion tests below for that gate on its own."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M18", passed=False, counted_in_go=True),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=3, seasons=5)
    assert go is False
    failing = next(r for r in out_results if r.id == "M18")
    assert failing.passed is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "M18" in report["go_reason"]
    assert report["summary"]["failed"] == 1


# ---------------------------------------------------------------------------
# Exit criterion (B-NEW-2): min_seeds/min_seasons from the bands file's own
# `exit_criterion` (Decision P2-22) must force NO-GO below threshold, the
# same hard-gate mechanism `--skip-games` already uses.
# ---------------------------------------------------------------------------


def test_exit_criterion_forces_no_go_when_seeds_below_minimum(monkeypatch, tmp_path):
    """B-NEW-2's own critic repro, reproduced directly: every counted metric
    passes and `seasons` alone already meets the criterion, but `seeds=1`
    is below the real bands file's `min_seeds=3` -- must still be NO-GO."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M2", passed=True, counted_in_go=True),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=1, seasons=5)
    assert go is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "exit criterion" in report["go_reason"].lower()
    assert "1 seed" in report["go_reason"] or "1 seed(s)" in report["go_reason"]


def test_exit_criterion_forces_no_go_when_seasons_below_minimum(monkeypatch, tmp_path):
    """Same gate, the `seasons` half: `seeds=3` alone meets the criterion,
    `seasons=1` does not (real bands file's `min_seasons=5`)."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M2", passed=True, counted_in_go=True),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=3, seasons=1)
    assert go is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "exit criterion" in report["go_reason"].lower()


def test_exit_criterion_does_not_block_go_at_seeds_1_seasons_1_repro(monkeypatch, tmp_path):
    """B-NEW-2's own critic repro title-for-title: `--seeds 1 --seasons 1`
    with every counted metric passing used to report GO/exit 0 -- this is
    the exact CLI repro at the `run_validation` level, confirming it now
    reports NO-GO with a clear, specific reason."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M2", passed=True, counted_in_go=True),
        _metric_result("M13", passed=None, counted_in_go=False, status="SKIP", value=None),
        _metric_result("M20", passed=None, counted_in_go=False, status="SKIP", value=None),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=1, seasons=1)
    assert go is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "exit criterion" in report["go_reason"].lower()


def test_exit_criterion_does_not_block_go_when_criterion_is_met(monkeypatch, tmp_path):
    """The gate must only fire BELOW threshold, not always -- a run that
    genuinely meets `min_seeds`/`min_seasons` with every counted metric
    passing must still report GO."""
    results = (_metric_result("M1", passed=True, counted_in_go=True),)
    out_results, go, _ = _run(monkeypatch, tmp_path, results, seeds=3, seasons=5)
    assert go is True
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is True


def test_skip_games_forces_no_go_even_when_every_counted_metric_passes(monkeypatch, tmp_path):
    """Round 7 Sec.8's own rule, tested directly rather than relying on M22
    accidentally SKIPping under `--skip-games` (that accidental enforcement
    was itself B1's bug, not this rule) -- every mocked metric here passes,
    but `skip_games=True` must still force NO-GO."""
    results = (
        _metric_result("M1", passed=True, counted_in_go=True),
        _metric_result("M2", passed=True, counted_in_go=True),
        _metric_result("M22", passed=True, counted_in_go=True),
    )
    out_results, go, _ = _run(monkeypatch, tmp_path, results, skip_games=True)
    assert go is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "skip-games" in report["go_reason"].lower()
    assert report["skip_games"] is True


# ---------------------------------------------------------------------------
# write_report -- output shape and the int-vs-float statistics.mean render
# ---------------------------------------------------------------------------


def test_write_report_smoke_shape(tmp_path):
    results = (
        _metric_result("M1", family=1, number=1, passed=True, counted_in_go=True),
        _metric_result("M21", family=3, number=21, passed=True, counted_in_go=False,
                        status="PASS (interpreted)"),
    )
    validate_gm.write_report(
        tmp_path, results=results, go=True, go_reason="GO: test", seeds=1, seasons=1,
        players_per_team=15, rng_seed=1, skip_games=False, determinism_check=True,
        elapsed_seconds=1.23, phase_timings_by_seed=[],
    )
    report_json_path = tmp_path / "report.json"
    report_md_path = tmp_path / "report.md"
    assert report_json_path.exists()
    assert report_md_path.exists()

    report = json.loads(report_json_path.read_text(encoding="utf-8"))
    assert set(report) >= {
        "generated_at", "go", "go_reason", "seeds", "seasons", "players_per_team", "rng_seed",
        "skip_games", "determinism_check", "elapsed_seconds", "summary", "metrics",
    }
    assert report["summary"]["total"] == 2
    assert len(report["metrics"]) == 2

    md = report_md_path.read_text(encoding="utf-8")
    assert "## Verdict: GO" in md
    assert "| Family | ID | Metric | Value | Band | Status | Counted |" in md


def test_write_report_renders_the_cap_hold_diagnostics_in_both_json_and_text_forms(tmp_path):
    """Architect round 19 §7 item 6 (Slice 19-A) -- `detail["hold_
    inclusive_violations"]`/`detail["hold_inclusive_violations_by_
    boundary"]` (M2) and `detail["holds_basis"]`/`detail["hold_inclusive_
    mean"]` (M10) must show up in `report.json` (already true for any
    `detail` key via `_metric_row_json`'s verbatim dump -- checked here
    directly rather than assumed) and in `report.md`'s own new dedicated
    section (this file's own existing convention renders no other metric's
    `detail` in text form at all, so this is new, not "matching an
    existing per-field text renderer")."""
    m2 = dataclasses.replace(
        _metric_result("M2", family=1, number=2, passed=True, counted_in_go=True),
        detail={
            "hold_inclusive_violations": 3,
            "hold_inclusive_violations_by_boundary": {"PRESEASON": 3},
        },
    )
    m10 = dataclasses.replace(
        _metric_result("M10", family=2, number=10, passed=True, counted_in_go=True),
        detail={"holds_basis": "computed", "hold_inclusive_mean": 7.5},
    )
    validate_gm.write_report(
        tmp_path, results=(m2, m10), go=True, go_reason="GO: test", seeds=1, seasons=1,
        players_per_team=15, rng_seed=1, skip_games=False, determinism_check=False,
        elapsed_seconds=0.1, phase_timings_by_seed=[],
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in report["metrics"]}
    assert by_id["M2"]["detail"]["hold_inclusive_violations"] == 3
    assert by_id["M2"]["detail"]["hold_inclusive_violations_by_boundary"] == {"PRESEASON": 3}
    assert by_id["M10"]["detail"]["holds_basis"] == "computed"
    assert by_id["M10"]["detail"]["hold_inclusive_mean"] == 7.5

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Cap-hold diagnostics" in md
    assert "M2 hold_inclusive_violations (5 clean boundaries): 3" in md
    assert "M10 holds_basis: computed" in md
    assert "M10 hold_inclusive_mean: 7.5" in md


def test_write_report_renders_an_exact_zero_mean_value_not_n_a(tmp_path):
    """Regression for the `statistics.mean` int-vs-float bug: `mean([0, 0])
    == 0` is a bare Python `int`, not a `float` -- a `isinstance(value,
    float)` check (instead of `value is not None`) would misrender this
    real, exact-zero result as `n/a`. Confirmed still fixed after this
    slice's own B1/B3 changes to `_finalize`/`compute_all`."""
    zero_mean = statistics.mean([0, 0])
    assert isinstance(zero_mean, int)
    results = (_metric_result("M9", value=zero_mean, passed=True, counted_in_go=True),)
    validate_gm.write_report(
        tmp_path, results=results, go=True, go_reason="GO: test", seeds=1, seasons=1,
        players_per_team=15, rng_seed=1, skip_games=False, determinism_check=False,
        elapsed_seconds=0.1, phase_timings_by_seed=[],
    )
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "0.0000" in md
    assert "n/a" not in [line for line in md.splitlines() if "M9" in line][0]


# ---------------------------------------------------------------------------
# B7 (critic round T dispatch): `_family3_source_row`/`_family3_source_rows`,
# `report.md`'s new Family-3 table, and `--no-family3-precert` were entirely
# untested -- the critic manually confirmed both paths worked today, but
# nothing would catch a future regression. Uses the real, live `_BANDS`
# (M12's own checked-in citation: gated value 1.554395, band_low 1.5,
# `precision.sd_across_seeds` 0.065923; M14's own: gated value
# 6.546666666666667, `se_precertified.se_measured` 0.6235) so the numbers
# below are real, not hand-typed guesses.
# ---------------------------------------------------------------------------


def test_family3_source_row_reports_gated_vs_live_reading_for_a_valid_citation():
    """A non-M14 Family-3 metric (M12) with a valid, gating citation --
    `live_margin_in_se` comes from `detail["live_reading"]["precision"][
    "margin_in_se"]`, `inside_own_noise` is computed from the citation's
    own `value` against `band_low` and `params.precision.sd_across_seeds`
    (1.554395 - 1.5 = 0.054395 < 2 * 0.065923 = 0.131846 -> True)."""
    band = _BANDS.metrics["M12"]
    citation = band.params["gated_value_precertified"]
    r = dataclasses.replace(
        _metric_result("M12", family=3, number=12, passed=True, counted_in_go=True),
        value=citation["value"],
        detail={
            "value_source": "precertified_corpus",
            "live_reading": {"value": 2.0, "precision": {"margin_in_se": 1.95}},
        },
        band_low=band.band_low,
    )
    row = validate_gm._family3_source_row(r, band, family3_precert=True)
    assert row == {
        "id": "M12",
        "gated_value": citation["value"],
        "gated_source": citation["source"],
        "live_value": 2.0,
        "live_margin_in_se": 1.95,
        "value_source": "precertified_corpus",
        "inside_own_noise": True,
    }


def test_family3_source_row_returns_none_when_band_has_no_citation():
    """A bands fixture without `params.gated_value_precertified` at all
    (a bands file that never asked for Decision P3-47, or M17/other
    non-Family-3 rows this is never called for) must not fabricate a row."""
    band = _BANDS.metrics["M12"]
    no_citation_params = {k: v for k, v in band.params.items() if k != "gated_value_precertified"}
    no_citation_band = dataclasses.replace(band, params=no_citation_params)
    r = _metric_result("M12", family=3, number=12, passed=True, counted_in_go=True)
    assert validate_gm._family3_source_row(r, no_citation_band, family3_precert=True) is None


def test_family3_source_row_labels_forced_live_when_family3_precert_false():
    """`--no-family3-precert` forces every Family-3 metric onto its own
    live reading regardless of citation validity -- the diagnostic row
    must say so plainly rather than silently reporting whatever `detail[
    "value_source"]` happens to already read (`"this_run"`, indistinguishable
    from an ordinary stale/absent citation)."""
    band = _BANDS.metrics["M12"]
    r = dataclasses.replace(
        _metric_result("M12", family=3, number=12, passed=True, counted_in_go=True),
        value=2.0,
        detail={"value_source": "this_run", "live_reading": None},
        band_low=band.band_low,
    )
    row = validate_gm._family3_source_row(r, band, family3_precert=False)
    assert row["value_source"] == "this_run (forced by --no-family3-precert)"
    # No `live_reading` key present in `detail` (absent-citation shape) --
    # `r.value`/`r.detail` themselves ARE the live reading.
    r_no_live_reading = dataclasses.replace(
        _metric_result("M12", family=3, number=12, passed=True, counted_in_go=True),
        value=2.0, detail={}, band_low=band.band_low,
    )
    row2 = validate_gm._family3_source_row(r_no_live_reading, band, family3_precert=False)
    assert row2["live_value"] == 2.0
    assert row2["value_source"] == "this_run (forced by --no-family3-precert)"


def test_family3_source_row_handles_m14_shape():
    """M14's own hookup shapes `live_reading`/`detail` differently from the
    `_attach_dual_reading` metrics (M11-M13/M15/M16) -- `se_measured` at
    the top level of `live_reading`/`detail`, not a nested `precision`
    block -- and `sd_across_seeds` for the noise check comes from `params.
    se_precertified.se_measured`, not `params.precision.sd_across_seeds`
    (M14 has no `precision` block at all). Real numbers: gated value
    6.546666666666667, band_low 1.5, se_measured 0.6235 -> (6.546666... -
    1.5) = 5.046666... is NOT < 2 * 0.6235 = 1.247 -> `inside_own_noise`
    is `False`, the opposite of M12's case above -- a real discriminating
    check, not a coincidental match."""
    band = _BANDS.metrics["M14"]
    citation = band.params["gated_value_precertified"]
    r = dataclasses.replace(
        _metric_result("M14", family=3, number=14, passed=True, counted_in_go=True),
        value=citation["value"],
        detail={
            "value_source": "precertified_corpus",
            "live_reading": {"value": 7.0, "se_measured": 50.0},
        },
        band_low=band.band_low,
    )
    row = validate_gm._family3_source_row(r, band, family3_precert=True)
    assert row["gated_value"] == pytest.approx(citation["value"])
    assert row["live_value"] == pytest.approx(7.0)
    assert row["live_margin_in_se"] == pytest.approx((7.0 - band.band_low) / 50.0)
    assert row["inside_own_noise"] is False


def test_family3_source_rows_only_includes_family3_metrics_with_citations():
    """`_family3_source_rows` iterates the fixed `_FAMILY3_METRIC_IDS`
    tuple, skipping any metric absent from `results` or whose band carries
    no citation, and never includes a non-Family-3 metric (M1) even if
    present in `results`."""
    no_citation_m13 = dataclasses.replace(
        _BANDS.metrics["M13"],
        params={k: v for k, v in _BANDS.metrics["M13"].params.items()
                if k != "gated_value_precertified"},
    )
    bands_mixed = dataclasses.replace(
        _BANDS, metrics={**_BANDS.metrics, "M13": no_citation_m13}
    )
    m12_citation = _BANDS.metrics["M12"].params["gated_value_precertified"]
    results = (
        _metric_result("M1", family=1, number=1, passed=True, counted_in_go=True),
        dataclasses.replace(
            _metric_result("M12", family=3, number=12, passed=True, counted_in_go=True),
            value=m12_citation["value"],
            detail={
                "value_source": "precertified_corpus",
                "live_reading": {"value": 2.0, "precision": {"margin_in_se": 1.95}},
            },
            band_low=_BANDS.metrics["M12"].band_low,
        ),
        dataclasses.replace(
            _metric_result("M13", family=3, number=13, passed=True, counted_in_go=True),
            value=2.0, detail={}, band_low=_BANDS.metrics["M13"].band_low,
        ),
    )
    rows = validate_gm._family3_source_rows(results, bands_mixed, family3_precert=True)
    ids = [row["id"] for row in rows]
    assert ids == ["M12"]  # M13 has no citation (skipped), M1 is never Family-3, M11/14-16 absent


def test_write_report_renders_family3_source_table(tmp_path):
    """`report.json["family3_source"]` carries the rows verbatim and
    `report.md` renders the dedicated "Family 3" markdown table with the
    same figures -- the informational-only disclosure (never affects
    `passed`/`counted_in_go`)."""
    results = (_metric_result("M12", family=3, number=12, passed=True, counted_in_go=True),)
    family3_source = (
        {
            "id": "M12", "gated_value": 1.554395, "gated_source": "test source",
            "live_value": 2.0, "live_margin_in_se": 1.95,
            "value_source": "precertified_corpus", "inside_own_noise": True,
        },
    )
    validate_gm.write_report(
        tmp_path, results=results, go=True, go_reason="GO: test", seeds=1, seasons=1,
        players_per_team=15, rng_seed=1, skip_games=False, determinism_check=False,
        elapsed_seconds=0.1, phase_timings_by_seed=[], family3_source=family3_source,
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["family3_source"] == list(family3_source)

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Family 3 -- gated reading vs. this run's own reading (Decision P3-47)" in md
    assert "| M12 | 1.5544 | 2.0000 | +1.95 | precertified_corpus | yes |" in md
    assert "Informational only -- never affects `passed`/`counted_in_go`" in md


def test_write_report_omits_family3_source_table_when_empty(tmp_path):
    """The default (`family3_source=()`, a bands fixture with no Family-3
    citations at all) must not render an empty/broken table section."""
    results = (_metric_result("M1", family=1, number=1, passed=True, counted_in_go=True),)
    validate_gm.write_report(
        tmp_path, results=results, go=True, go_reason="GO: test", seeds=1, seasons=1,
        players_per_team=15, rng_seed=1, skip_games=False, determinism_check=False,
        elapsed_seconds=0.1, phase_timings_by_seed=[],
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["family3_source"] == []
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Family 3 -- gated reading" not in md


def test_bands_without_family3_precert_strips_citation_from_every_family3_metric_only():
    """`_bands_without_family3_precert` (the `--no-family3-precert` escape
    hatch) strips `params.gated_value_precertified` from all six Family-3
    entries and touches nothing else -- other params on those same bands
    (e.g. M12's own `precision`/`se_threshold` block) and every non-
    Family-3 metric's params must be byte-identical to the input."""
    stripped = validate_gm._bands_without_family3_precert(_BANDS)
    for metric_id in validate_gm._FAMILY3_METRIC_IDS:
        assert "gated_value_precertified" not in stripped.metrics[metric_id].params, metric_id
        original_other_params = {
            k: v for k, v in _BANDS.metrics[metric_id].params.items()
            if k != "gated_value_precertified"
        }
        assert stripped.metrics[metric_id].params == original_other_params, metric_id
    for metric_id, band in _BANDS.metrics.items():
        if metric_id in validate_gm._FAMILY3_METRIC_IDS:
            continue
        assert stripped.metrics[metric_id].params == band.params, metric_id


def test_run_validation_no_family3_precert_forces_live_reading_regardless_of_citation_validity(
    monkeypatch, tmp_path,
):
    """`--no-family3-precert` must actually change what `compute_all` gates
    on -- not just report differently. `run_validation` threads a bands
    object with every Family-3 citation stripped into `compute_all` when
    `family3_precert=False`, forcing the live reading regardless of
    citation validity; with the default `family3_precert=True`, the real
    checked-in citations pass through untouched."""
    results = (_metric_result("M1", family=1, number=1, passed=True, counted_in_go=True),)

    _, _, spy_forced = _run(monkeypatch, tmp_path, results, family3_precert=False)
    bands_forced = spy_forced.calls[0]["bands"]
    for metric_id in validate_gm._FAMILY3_METRIC_IDS:
        assert "gated_value_precertified" not in bands_forced.metrics[metric_id].params, metric_id

    _, _, spy_default = _run(monkeypatch, tmp_path, results, family3_precert=True)
    bands_default = spy_default.calls[0]["bands"]
    for metric_id in validate_gm._FAMILY3_METRIC_IDS:
        assert "gated_value_precertified" in bands_default.metrics[metric_id].params, metric_id


# ---------------------------------------------------------------------------
# main() -- exit code (Nit 6)
# ---------------------------------------------------------------------------


def _fake_args(**overrides):
    import argparse

    defaults = dict(
        seeds=1, seasons=1, players_per_team=15, rng_seed=1, season_year=2026,
        skip_games=False, determinism_check=False, control_check=False, family3_precert=True,
        draft_divergence_drafts=1, only_metric_17=False, bands=DEFAULT_BANDS_PATH,
        digest=validate_gm._DEFAULT_DIGEST_PATH, protected_pick_share=0.0, out=None,
        media_corpus=None, use_media_corpus=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_main_exits_zero_on_go(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_gm, "_parse_args", lambda: _fake_args(out=str(tmp_path)))
    monkeypatch.setattr(
        validate_gm, "run_validation",
        lambda **kwargs: ((_metric_result("M1", passed=True, counted_in_go=True),), True),
    )
    with pytest.raises(SystemExit) as exc_info:
        validate_gm.main()
    assert exc_info.value.code == 0


def test_main_exits_nonzero_on_no_go(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_gm, "_parse_args", lambda: _fake_args(out=str(tmp_path)))
    monkeypatch.setattr(
        validate_gm, "run_validation",
        lambda **kwargs: ((_metric_result("M18", passed=False, counted_in_go=True),), False),
    )
    with pytest.raises(SystemExit) as exc_info:
        validate_gm.main()
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Decision P3-29(a) -- folded into `--control-check` at Phase 3 step 9.
# Fast, fully mocked (no real run_seed/compute_all) -- proves the FOLD
# LOGIC itself (convergence, and which config triggers it), not the real
# comparator's own correctness (that's `test_metrics.py`'s job).
# ---------------------------------------------------------------------------


class _FakeComparison:
    def __init__(self, mismatches):
        self.mismatches = mismatches


def test_control_check_folds_exit_gate_comparison_when_config_matches(monkeypatch, tmp_path):
    """When this invocation's own config matches the committed Phase-2
    baseline's recorded shape, comparison (a)'s mismatch count is folded
    into the same `control_mismatches` integer M29 reads, alongside the
    digest comparison's own count -- and `compute_all` is called more than
    once, because folding a nonzero comparison-(a) count can flip M29's
    own pass/fail, which flips `go`/`go_reason` (both compared fields),
    which comparison (a) has to be re-run against to converge."""
    baseline_report = {
        "go": True, "go_reason": "baseline", "seeds": 1, "seasons": 0,
        "skip_games": False, "players_per_team": 1, "metrics": [],
    }
    monkeypatch.setattr(
        validate_gm, "load_phase2_exit_gate_baseline", lambda path: baseline_report
    )
    monkeypatch.setattr(validate_gm, "run_seed", _fake_run_seed)
    monkeypatch.setattr(
        validate_gm, "compare_decision_log_digest",
        lambda seasons, baseline, **kw: (type("FakeDigestComparison", (), {"equal": False})(),),
    )
    # 3 fixed exit-gate mismatches, independent of control_mismatches --
    # mirrors the real comparator, where M29 itself is absent from the
    # baseline's own metric set, so its value/status never feeds back into
    # the mismatch count except via `go`/`go_reason`, which this fake
    # ignores for simplicity (it always returns the same 3).
    monkeypatch.setattr(
        validate_gm, "compare_exit_gate_reports",
        lambda actual, baseline: _FakeComparison(("m1", "m2", "m3")),
    )

    calls: list[int | None] = []

    def _fake_compute_all(runs, bands, *, determinism_runs=(), include_metric_17=True,
                           control_mismatches=None, control_totals=None,
                           m27_control_by_philosophy_template=None, media_corpus=None):
        calls.append(control_mismatches)
        return (
            _metric_result(
                "M29", passed=(control_mismatches == 0), counted_in_go=True,
                value=float(control_mismatches or 0),
            ),
        )

    monkeypatch.setattr(validate_gm, "compute_all", _fake_compute_all)

    results, go = validate_gm.run_validation(
        seeds=1, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=False, determinism_check=False, control_check=True,
        protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )

    # 1 digest mismatch + 3 exit-gate mismatches = 4, converged onto every
    # `compute_all` call from the point the fold first computes it onward.
    assert calls[-1] == 4, f"final control_mismatches must be 1 + 3 = 4, calls were {calls}"
    assert len(calls) > 1, "the fold must call compute_all more than once to converge"
    by_id = {r.id: r for r in results}
    assert by_id["M29"].value == 4.0


def test_control_check_does_not_fold_exit_gate_comparison_when_config_does_not_match(
    monkeypatch, tmp_path,
):
    """A config that does NOT match the committed baseline's shape (e.g.
    the cheap dev-loop pass) must leave M29 exactly as before -- digest-
    only, `compute_all` called exactly once, `compare_exit_gate_reports`
    never even invoked."""
    baseline_report = {
        "go": True, "go_reason": "baseline", "seeds": 999, "seasons": 999,
        "skip_games": True, "players_per_team": 999, "metrics": [],
    }
    monkeypatch.setattr(
        validate_gm, "load_phase2_exit_gate_baseline", lambda path: baseline_report
    )
    monkeypatch.setattr(validate_gm, "run_seed", _fake_run_seed)
    monkeypatch.setattr(
        validate_gm, "compare_decision_log_digest", lambda seasons, baseline, **kw: (),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("compare_exit_gate_reports must not be called on a shape mismatch")

    monkeypatch.setattr(validate_gm, "compare_exit_gate_reports", _boom)

    calls: list[int | None] = []

    def _fake_compute_all(runs, bands, *, determinism_runs=(), include_metric_17=True,
                           control_mismatches=None, control_totals=None,
                           m27_control_by_philosophy_template=None, media_corpus=None):
        calls.append(control_mismatches)
        return (_metric_result("M29", passed=True, counted_in_go=True, value=0.0),)

    monkeypatch.setattr(validate_gm, "compute_all", _fake_compute_all)

    validate_gm.run_validation(
        seeds=1, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=False, determinism_check=False, control_check=True,
        protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    assert calls == [0], "compute_all must run exactly once, with digest-only mismatches"


# ---------------------------------------------------------------------------
# `--control-check` -- Fix 3's own coverage gap (critic-noted: this path was
# never exercised end to end anywhere in this file; every test above mocks
# `run_seed` and never sets `control_check=True`). Real `run_seed`/`compute_
# all` calls, deliberately -- a mocked control run cannot prove the real
# comparator reads `0` mismatches against the real committed baseline, only
# that the wiring compiles. `@pytest.mark.slow`: the control pass alone is a
# real 1 seed x 2 season `--skip-games` run (Decision P3-29(b)'s own ~34s
# estimate), on top of this test's own (small) main run.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_control_check_reports_zero_mismatches_against_the_real_committed_digest(tmp_path):
    """Only Decision P3-29(b)'s digest comparison is wired into `--control-
    check` this step (see this fix's own HANDOFF disclosure for why
    comparison (a), the `compare_exit_gate_reports` value-scoped check
    against `docs/ledger/phase-2-exit-gate-report.json`, is NOT wired here)
    -- this test proves that wired half runs for real, end to end, against
    the real committed `seeds/gm_decision_log_digest.json`, and reads a
    real `0` mismatches on M29, not just that the code path compiles."""
    results, go = validate_gm.run_validation(
        seeds=1, seasons=1, players_per_team=15, rng_seed=1, season_year=2026,
        skip_games=True, determinism_check=False, control_check=True,
        protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    by_id = {r.id: r for r in results}
    m29 = by_id["M29"]
    assert m29.status != "SKIP", "control_check=True must produce a real control_mismatches int"
    assert m29.value == 0.0, "a real control pass must reproduce the committed digest baseline"


@pytest.mark.slow
def test_control_check_reports_nonzero_mismatches_against_a_corrupted_digest_baseline(tmp_path):
    """Same real control pass, against a deliberately corrupted copy of the
    committed digest baseline -- proves the comparator is actually being
    consulted (not a wiring that always reads `0` regardless of input)."""
    real_digest = json.loads(validate_gm._DEFAULT_DIGEST_PATH.read_text(encoding="utf-8"))
    corrupted = dict(real_digest)
    corrupted["season_digests"] = [
        {"season": row["season"], "digest": "0" * 64} for row in real_digest["season_digests"]
    ]
    corrupted_path = tmp_path / "corrupted_digest.json"
    corrupted_path.write_text(json.dumps(corrupted), encoding="utf-8")

    results, go = validate_gm.run_validation(
        seeds=1, seasons=1, players_per_team=15, rng_seed=1, season_year=2026,
        skip_games=True, determinism_check=False, control_check=True,
        protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path, digest_path=corrupted_path,
    )
    by_id = {r.id: r for r in results}
    m29 = by_id["M29"]
    assert m29.status != "SKIP"
    assert m29.value is not None and m29.value > 0.0, (
        "a corrupted baseline must read nonzero mismatches, proving the comparator is real"
    )


# ---------------------------------------------------------------------------
# `_assert_m28_step17_invariants` -- item 3's own "a builder cannot route
# around this" gate: the ladder identity (no tolerance) plus the triage
# assertion (Decision P3-41), both re-checked against `compute_m28`'s own
# `detail` dict. Direct, pure-function tests against a hand-built `M28`
# `MetricResult` -- no real seed/season run needed, mirroring this file's
# own "mock run_seed/compute_all" convention above.
# ---------------------------------------------------------------------------

_HEALTHY_M28_DETAIL: dict[str, object] = {
    "channel1_up_flips": 300,
    "channel1_up_reachability": 300,
    "channel1_down_flips": 0,
    "channel1_down_reachability": 0,
    "triage_down_violation": False,
    "triage_up_violation": False,
    "far_rejects_observed": 0,
    "channel1_down_max_observed": 0.0,
    "m18_trades_executed": 42,
    "channel1_up_max_observed": 3.0,
}


def _m28_result(detail: dict[str, object]) -> MetricResult:
    return dataclasses.replace(_metric_result("M28", number=28, family=5), detail=detail)


def test_assert_m28_step17_invariants_is_a_noop_on_a_healthy_run():
    results = (_m28_result(dict(_HEALTHY_M28_DETAIL)),)
    validate_gm._assert_m28_step17_invariants(results)  # must not raise


def test_assert_m28_step17_invariants_noops_when_m28_is_absent():
    results = (_metric_result("M1"),)
    validate_gm._assert_m28_step17_invariants(results)  # must not raise


def test_assert_m28_step17_invariants_raises_on_up_ladder_identity_mismatch():
    """Reintroduces a ladder-identity disagreement -- the production
    target-count ladder and the harness's own reachability read disagree
    at one site (`flips=299` against `reachability=300`), exactly the
    shape a mis-ordered/off-by-one `FUTILITY_STREAK` branch would produce.
    Without this test, this assertion could go unexercised the same way
    it did before this fix round."""
    detail = dict(_HEALTHY_M28_DETAIL, channel1_up_flips=299, channel1_up_reachability=300)
    with pytest.raises(RuntimeError, match="channel1_up"):
        validate_gm._assert_m28_step17_invariants((_m28_result(detail),))


def test_assert_m28_step17_invariants_raises_on_down_ladder_identity_mismatch():
    detail = dict(_HEALTHY_M28_DETAIL, channel1_down_flips=1, channel1_down_reachability=0)
    with pytest.raises(RuntimeError, match="channel1_down"):
        validate_gm._assert_m28_step17_invariants((_m28_result(detail),))


def test_assert_m28_step17_invariants_raises_on_triage_down_violation():
    detail = dict(_HEALTHY_M28_DETAIL, triage_down_violation=True)
    with pytest.raises(RuntimeError, match="triage gate failed \\(down\\)"):
        validate_gm._assert_m28_step17_invariants((_m28_result(detail),))


def test_assert_m28_step17_invariants_raises_on_triage_up_violation():
    detail = dict(_HEALTHY_M28_DETAIL, triage_up_violation=True)
    with pytest.raises(RuntimeError, match="triage gate failed \\(up\\)"):
        validate_gm._assert_m28_step17_invariants((_m28_result(detail),))


def test_assert_m28_step17_invariants_raises_on_a_missing_required_key():
    """Critic finding B2 -- the pre-fix `.get()`-on-both-sides comparison
    made a missing/renamed `detail` key pass vacuously (`None == None`).
    Dropping one required key from an otherwise-healthy, non-SKIP detail
    dict must now raise LOUD instead of silently agreeing with itself."""
    detail = dict(_HEALTHY_M28_DETAIL)
    del detail["channel1_down_reachability"]
    with pytest.raises(RuntimeError, match="channel1_down_reachability"):
        validate_gm._assert_m28_step17_invariants((_m28_result(detail),))


def test_assert_m28_step17_invariants_noops_on_a_skip_status_with_empty_detail():
    """A genuine SKIP result (e.g. `--no-control-check`, or a mechanism-
    absent corpus) legitimately carries an empty/partial `detail` -- the
    B2 required-key guard must step aside for `status == "SKIP"` rather
    than raising on a case that was never a real regression."""
    base = _metric_result("M28", number=28, family=5)
    result = dataclasses.replace(base, detail={}, status="SKIP")
    validate_gm._assert_m28_step17_invariants((result,))  # must not raise


@pytest.mark.slow
def test_assert_m28_step17_invariants_against_a_real_compute_m28_result(tmp_path):
    """Critic finding B2's other half -- every other test on this function
    feeds it a hand-built dict; none had ever driven a REAL `compute_m28`
    output through it. A real (skip-games) 1 seed x 2 season run with
    `control_check=True` produces a genuine, non-SKIP M28 `MetricResult`
    with all six required keys populated by the real code, not this test
    -- proving the assertion is actually wired to real production shape,
    not just to the shapes this file's own fixtures happen to construct."""
    results, go = validate_gm.run_validation(
        seeds=1, seasons=2, players_per_team=15, rng_seed=1, season_year=2026,
        skip_games=True, determinism_check=False, control_check=True,
        protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    by_id = {r.id: r for r in results}
    m28 = by_id["M28"]
    assert m28.status != "SKIP", "a real trade-window run must produce a real, gated M28"
    validate_gm._assert_m28_step17_invariants((m28,))  # must not raise on real output


def test_run_validation_calls_the_m28_step17_assertion_after_write_report(monkeypatch, tmp_path):
    """Wiring check -- `_assert_m28_step17_invariants` must be reachable
    from `run_validation`'s own body, called AFTER `write_report` so this
    run's own diagnostics are already on disk before any raise."""
    call_order: list[str] = []
    real_write_report = validate_gm.write_report

    def _spy_write_report(*args, **kwargs):
        call_order.append("write_report")
        return real_write_report(*args, **kwargs)

    def _spy_assert(results):
        call_order.append("assert_m28")

    monkeypatch.setattr(validate_gm, "write_report", _spy_write_report)
    monkeypatch.setattr(validate_gm, "_assert_m28_step17_invariants", _spy_assert)
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results)
    assert call_order == ["write_report", "assert_m28"]


# ---------------------------------------------------------------------------
# §11 step 16 -- `with_philosophies_disabled` (§9.1's null-control
# transformation) and its wiring into `run_validation`'s own phrase-library
# threading (M26/M27 are now real, dialogue-on metrics).
# ---------------------------------------------------------------------------


def _template(id_: str, philosophies: tuple[str, ...] = ()) -> PhraseTemplate:
    return PhraseTemplate(
        id=id_, philosophies=philosophies, situations=(), windows=(), triggers=(),
        weight=1.0, text="text", slots=(),
    )


def test_with_philosophies_disabled_forces_every_template_to_wildcard():
    library = LoadedPhraseLibrary(
        templates=(
            _template("t1", philosophies=("star_maxer",)),
            _template("t2", philosophies=()),
            _template("t3", philosophies=("egalitarian", "chaos")),
        ),
        repeat_penalty=0.5, exclusive_mass_share=0.25,
    )
    disabled = validate_gm.with_philosophies_disabled(library)

    assert [t.id for t in disabled.templates] == ["t1", "t2", "t3"]
    assert all(t.philosophies == () for t in disabled.templates)
    assert disabled.repeat_penalty == library.repeat_penalty
    assert disabled.exclusive_mass_share == library.exclusive_mass_share
    for original, transformed in zip(library.templates, disabled.templates, strict=True):
        assert transformed.situations == original.situations
        assert transformed.windows == original.windows
        assert transformed.triggers == original.triggers
        assert transformed.weight == original.weight
        assert transformed.text == original.text
        assert transformed.slots == original.slots
    # The real, already-loaded library object is never mutated in place --
    # `dataclasses.replace` on a frozen dataclass always returns a new one.
    assert library.templates[0].philosophies == ("star_maxer",)


def test_with_philosophies_disabled_on_the_real_library_produces_only_wildcards():
    real = load_library()
    assert any(t.philosophies != () for t in real.templates), (
        "fixture error -- the real library must have at least one non-wildcard (philosophy-"
        "exclusive) template, or this test proves nothing about the transformation"
    )
    disabled = validate_gm.with_philosophies_disabled(real)
    assert len(disabled.templates) == len(real.templates)
    assert all(t.philosophies == () for t in disabled.templates)


def test_run_validation_threads_a_real_phrase_library_into_every_main_loop_run_seed_call(
    monkeypatch, tmp_path,
):
    """§11 step 16 -- every main-loop `run_seed` call must receive a real,
    loaded `phrase_library` (dialogue on), never the `None` default, so
    M26/M27 measure real data instead of permanently SKIPping."""
    calls: list[dict] = []

    def _spy_run_seed(**kwargs):
        calls.append(kwargs)
        return _fake_run_seed(**kwargs)

    monkeypatch.setattr(validate_gm, "run_seed", _spy_run_seed)
    monkeypatch.setattr(validate_gm, "compute_all", _ComputeAllSpy((_metric_result("M1"),)))
    validate_gm.run_validation(
        seeds=2, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=False, determinism_check=False, control_check=False,
        family3_precert=True, protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    assert len(calls) == 2
    for call in calls:
        assert call.get("phrase_library") is not None


def test_run_validation_null_control_pass_uses_a_philosophies_disabled_library(
    monkeypatch, tmp_path,
):
    """§11 step 16's own null-control pass (§9.1) must re-run `run_seed`
    with a DIFFERENT library object than the main loop -- one whose every
    template has `philosophies == ()` -- never the real library (which
    would make the "null" measurement identical to the real corpus and
    defeat the whole point of the control)."""
    calls: list[dict] = []

    def _spy_run_seed(**kwargs):
        calls.append(kwargs)
        return _fake_run_seed(**kwargs)

    monkeypatch.setattr(validate_gm, "run_seed", _spy_run_seed)
    monkeypatch.setattr(validate_gm, "compute_all", _ComputeAllSpy((_metric_result("M1"),)))
    validate_gm.run_validation(
        seeds=1, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=True, determinism_check=False, control_check=True,
        family3_precert=True, protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    # Call order: main loop (seed 1), then the M29 control pass, then this
    # step's own null-control pass -- exactly the order `run_validation`'s
    # own body issues them in.
    assert len(calls) == 3
    main_call, _m29_call, null_call = calls
    assert main_call.get("phrase_library") is not None
    assert null_call.get("phrase_library") is not None
    assert main_call["phrase_library"] is not null_call["phrase_library"]
    assert all(t.philosophies == () for t in null_call["phrase_library"].templates)
    assert any(t.philosophies != () for t in main_call["phrase_library"].templates)


def test_control_check_run_pins_agency_and_media_disabled(monkeypatch, tmp_path):
    """Decision P4-55's OTHER pin site (the critic-round B2 gap this test
    closes): the M29 control run must call `run_seed` with `agency_
    enabled=False, media_enabled=False`, explicit, the same way it already
    pins `max_negotiation_rounds=0, head_to_head_enabled=False`. Neither
    flag does anything today (no Phase 4 mechanism reads them yet), so a
    silent regression here would stay invisible until Stage B/C wires a
    real mechanism -- at which point an unpinned control run would diff a
    live, mechanism-enabled run against the frozen `seeds/gm_decision_log_
    digest.json` baseline and report a wiring-artifact mismatch, not a real
    one. This test would fail the moment that pin is dropped, long before
    Stage B/C exists to expose it at runtime."""
    calls: list[dict] = []

    def _spy_run_seed(**kwargs):
        calls.append(kwargs)
        return _fake_run_seed(**kwargs)

    monkeypatch.setattr(validate_gm, "run_seed", _spy_run_seed)
    monkeypatch.setattr(validate_gm, "compute_all", _ComputeAllSpy((_metric_result("M1"),)))
    validate_gm.run_validation(
        seeds=1, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=True, determinism_check=False, control_check=True,
        family3_precert=True, protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
    )
    # Call order: main loop (seed 1), then the M29 control pass, then the
    # null-control pass -- same order asserted in the test above.
    assert len(calls) == 3
    _main_call, m29_call, _null_call = calls
    assert m29_call.get("agency_enabled") is False, (
        "M29 control run must pin agency_enabled=False -- see Decision P4-55"
    )
    assert m29_call.get("media_enabled") is False, (
        "M29 control run must pin media_enabled=False -- see Decision P4-55"
    )


# ---------------------------------------------------------------------------
# `_assert_m27_null_control` -- Decision P3-49's own repointed escalation
# rule (within-cohort split-half A/A, superseding §9.1's withdrawn cross-
# cohort control reading), mirroring `_assert_m28_step17_invariants`'s own
# direct, hand-built-dict test style immediately above.
# ---------------------------------------------------------------------------


def test_assert_m27_null_control_is_a_noop_when_result_is_none():
    """`--no-control-check` -- no control pass ran this invocation."""
    validate_gm._assert_m27_null_control(None)  # must not raise


def test_assert_m27_null_control_is_a_noop_when_below_threshold_is_none():
    """One or both cohorts had too few distinct team ids or zero selections
    in one half -- a corpus-scale problem, never raised on here (never
    evidence that the noise-only null itself is unclean)."""
    validate_gm._assert_m27_null_control({
        "within_cohort_null": None, "within_cohort_star_maxer_tv": None,
        "within_cohort_egalitarian_tv": 0.02, "escalation_threshold": 0.0625,
        "below_threshold": None,
    })  # must not raise


def test_assert_m27_null_control_is_a_noop_when_below_threshold():
    validate_gm._assert_m27_null_control({
        "within_cohort_null": 0.025, "within_cohort_star_maxer_tv": 0.02,
        "within_cohort_egalitarian_tv": 0.025, "escalation_threshold": 0.0625,
        "below_threshold": True,
    })  # must not raise


def test_assert_m27_null_control_raises_when_at_or_above_threshold():
    """Decision P3-49's own words, made mechanical: "the genuine noise-only
    null itself is not clean at this event rate ... the phase escalates
    rather than sliding the band" -- never a silent pass, never an
    automatic band/library adjustment."""
    with pytest.raises(RuntimeError, match="split-half A/A escalation fired"):
        validate_gm._assert_m27_null_control({
            "within_cohort_null": 0.07, "within_cohort_star_maxer_tv": 0.07,
            "within_cohort_egalitarian_tv": 0.03, "escalation_threshold": 0.0625,
            "below_threshold": False,
        })


# ---------------------------------------------------------------------------
# `_m27_escalation_min_n` / `_m27_escalation_gate` -- §11 Stage D step 16's
# own minimum-n guard on the within-cohort escalation check (same SKIP-
# below-a-data-floor category as every other Decision P3-37 guard in
# `metrics.py`), added once the corrected binomial-thinning null (real,
# ~1.1-1.4x over the analytic figure) was found to still read 0.117/0.064
# at 1-season/2-season smoke-test corpus scale -- above 0.0625 not because
# the mechanism is wrong (already fixed and NOT touched again here) but
# because the 0.0625 derivation itself assumed exit-gate-scale data.
#
# **Recalibrated (§11 Stage D step 16's second fix round)**: the original
# `1.5x overdispersion * 1.75x margin` (2.625x combined) floor grew faster
# than `n` grows with corpus scale and SKIPped even at the real exit-gate
# shape (`K=64-65`, `n=31,000-34,000`) -- the exact "disguised always-skip"
# failure mode this guard exists to avoid. A single `MARGIN = 1.5` floor,
# applied directly to the analytic formula, replaces it; see `_m27_
# escalation_min_n`'s own docstring for the full derivation.
# ---------------------------------------------------------------------------


def test_m27_escalation_min_n_matches_its_own_stated_derivation():
    """Reproduces `_m27_escalation_min_n`'s own docstring formula
    (`n >= margin^2 * (k-1) / (pi * threshold^2)`, `margin=1.5`)
    independently, so a future reader who changes the constant without
    updating the formula (or vice versa) gets a failing test, not a
    silent drift."""
    threshold = 0.0625
    margin = 1.5
    for k in (2, 10, 47, 55, 88):
        expected = math.ceil(margin ** 2 * (k - 1) / (math.pi * threshold ** 2))
        assert validate_gm._m27_escalation_min_n(k, threshold) == expected


def test_m27_escalation_min_n_at_the_real_measured_cohort_sizes():
    """The two real `K` values §11 Stage D step 16's own dispatch measured
    on a real 1-season/15-players-per-team smoke corpus (star_maxer
    `K=47`, egalitarian `K=55`) -- pinned so a future change to the margin
    constant is visible as a concrete number moving, not just an abstract
    formula change."""
    assert validate_gm._m27_escalation_min_n(47, 0.0625) == 8434
    assert validate_gm._m27_escalation_min_n(55, 0.0625) == 9901


def test_m27_escalation_min_n_is_zero_below_k_two():
    """`K < 2` (0 or 1 distinct templates selected) makes the analytic
    null itself undefined (`sqrt((k-1)/...)` is 0 or imaginary) -- no
    floor to derive, so no gate to apply; the (separate, pre-existing)
    zero-selections/no-reading path in `_within_cohort_binomial_thin_tv`
    already handles this shape."""
    assert validate_gm._m27_escalation_min_n(0, 0.0625) == 0
    assert validate_gm._m27_escalation_min_n(1, 0.0625) == 0


def test_m27_escalation_gate_skips_on_degenerate_k_never_a_bare_pass():
    """`_m27_escalation_min_n(k < 2)` returns `0` (no floor), which alone
    would let a degenerate 1-template cohort sail past the floor check
    with ANY `n >= 0` -- and `_within_cohort_binomial_thin_tv` reads
    `TV=0.0` for a single-template cohort, which would then read as a
    silent below-threshold PASS on a check whose whole point is "never
    silently pass" (§11 Stage D step 16's fix round nit). `_m27_
    escalation_gate` must catch this itself, before the floor check, and
    SKIP naming the degenerate `K` -- never return `below_threshold=True`
    here."""
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=0.0, escalation_threshold=0.0625,
        star_k=1, star_n=500, egal_k=55, egal_n=100_000,
    )
    assert below_threshold is None
    assert skip_reason is not None
    assert "degenerate" in skip_reason
    assert "star_maxer K=1" in skip_reason


def test_m27_escalation_gate_skips_below_the_min_n_floor():
    """The exact real-world shape this guard exists for: a 1-season smoke
    corpus's real measured (K, n) pair, well under its own floor, with a
    genuinely bad (above-threshold) within_cohort_null -- proves the SKIP
    fires on n, not on the null value."""
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=0.117253, escalation_threshold=0.0625,
        star_k=47, star_n=1982, egal_k=55, egal_n=1977,
    )
    assert below_threshold is None
    assert skip_reason is not None
    assert "1982" in skip_reason and "1977" in skip_reason
    assert "8434" in skip_reason and "9901" in skip_reason


def test_m27_escalation_gate_skips_when_only_one_cohort_is_below_floor():
    """Either cohort alone below its own floor is enough to SKIP -- the
    check is only meaningful if BOTH cohorts' own readings are trustworthy
    (the gated value is `max` of the two)."""
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=0.02, escalation_threshold=0.0625,
        star_k=2, star_n=100_000, egal_k=55, egal_n=1977,
    )
    assert below_threshold is None
    assert skip_reason is not None


def test_m27_escalation_gate_preserves_the_pre_existing_no_reading_case():
    """`within_cohort_null is None` (a cohort had zero selections in this
    arm, or every binomial-thinned coin flip landed the same way) is a
    SEPARATE, pre-existing no-reading case this guard must not relabel as
    a skip-for-scale even when both cohorts otherwise clear their own
    floor -- `skip_reason` stays `None` so `write_report`'s own verdict
    text keeps falling through to its original "no reading" message."""
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=None, escalation_threshold=0.0625,
        star_k=2, star_n=100_000, egal_k=2, egal_n=100_000,
    )
    assert below_threshold is None
    assert skip_reason is None


def test_m27_escalation_gate_still_fires_above_the_floor_with_a_genuinely_bad_null():
    """The guard-doesn't-defang-the-check proof: a FIXED, independently-
    chosen (K, n) pair -- the real exit-gate shape this dispatch's own
    recalibration was measured against (3 seeds x 5 seasons: `K=64-65`,
    `n=31,000-34,000`; clean round numbers picked from that range) --
    deliberately NOT derived from `_m27_escalation_min_n` itself (the
    prior version of this test computed `n` FROM the floor formula, so it
    was green for any floor value however wrong and caught nothing; see
    §11 Stage D step 16's second fix round). Paired with a hand-crafted
    `within_cohort_null` above `escalation_threshold` -- if a future
    change to the floor formula miscalibrates it (in either direction)
    such that this fixed, realistic shape stops evaluating, this test
    fails."""
    star_k, star_n = 64, 33_000
    egal_k, egal_n = 65, 31_000
    threshold = 0.0625
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=0.15, escalation_threshold=threshold,
        star_k=star_k, star_n=star_n, egal_k=egal_k, egal_n=egal_n,
    )
    assert skip_reason is None, "n is well above the floor -- this must NOT be a scale-skip"
    assert below_threshold is False

    # And the raise itself still fires when this gate's own output is fed
    # through to `_assert_m27_null_control`, exactly as `run_validation`
    # wires it -- the full path from "n clears the floor and the null is
    # bad" to "escalation raises" stays intact end to end.
    with pytest.raises(RuntimeError, match="split-half A/A escalation fired"):
        validate_gm._assert_m27_null_control({
            "within_cohort_null": 0.15, "within_cohort_star_maxer_tv": 0.15,
            "within_cohort_egalitarian_tv": 0.12, "escalation_threshold": threshold,
            "below_threshold": below_threshold, "escalation_skip_reason": skip_reason,
        })


def test_m27_escalation_gate_still_evaluates_and_passes_with_a_genuinely_good_null():
    """Mirror of the above with the SAME fixed, independently-chosen
    (K, n) pair (real exit-gate shape) but a hand-crafted GOOD null below
    `escalation_threshold` -- confirms the gate does not just fire on any
    reading once it clears the floor, it correctly reads the null both
    ways. Both this test and its bad-null mirror above use `n`/`K` chosen
    independently of the floor formula, so together they would catch a
    future miscalibration in either direction (floor too high -> false
    SKIP masking a real escalation; floor too low -> evaluating on a
    corpus too small to mean anything)."""
    star_k, star_n = 64, 33_000
    egal_k, egal_n = 65, 31_000
    threshold = 0.0625
    below_threshold, skip_reason = validate_gm._m27_escalation_gate(
        within_cohort_null=0.03, escalation_threshold=threshold,
        star_k=star_k, star_n=star_n, egal_k=egal_k, egal_n=egal_n,
    )
    assert skip_reason is None, "n is well above the floor -- this must NOT be a scale-skip"
    assert below_threshold is True

    # `_assert_m27_null_control` must NOT raise on a genuinely good null.
    validate_gm._assert_m27_null_control({
        "within_cohort_null": 0.03, "within_cohort_star_maxer_tv": 0.03,
        "within_cohort_egalitarian_tv": 0.025, "escalation_threshold": threshold,
        "below_threshold": below_threshold, "escalation_skip_reason": skip_reason,
    })


# ---------------------------------------------------------------------------
# `_assert_phase3_exit_gate_counted_total` / `_assert_no_family5_skip_at_
# exit_gate` -- §11 step 18 assertions (i) and (ii) (Decision P3-32/P3-37,
# corrected from 33/35 to 32/35 by Decision P3-46, and corrected AGAIN by
# this fix round -- Phase 4's Family 6/7 metrics push the real bands file
# to 41 entries, and Decision P4-105/P4-108 gave M34 a `counted_in_go`
# that is legitimately DATA-DEPENDENT, so a single hardcoded expected
# total is no longer right for every healthy run). Neither assertion
# existed as a real, hard-failing check before Decision P3-32/P3-37's own
# fix round -- the design's own step 18 spec called for them, and `_decide_
# go` only ever computed the denominator dynamically with no cardinality
# or composition check at all. Direct, hand-built-results tests, mirroring
# `_assert_m28_step17_invariants`'s own style above -- no real seed/season
# run needed to exercise the counting/composition logic itself (a real
# full-bands run needs real games, which this file's own tests never pay
# for -- see the `--skip-games` tests above).
# ---------------------------------------------------------------------------

# 30 of the file's 41 real ids that carry no Family-5 meaning -- Phase 2's
# 26-entry set (M1-M7, M8a, M8b, M9-M22, A1-A3) minus M21 (handled
# separately below because it is uncounted, Decision P3-32's own pre-
# Phase-3 exclusion, unrelated to Decision P3-43/P3-46), plus Phase 4's
# five Family 6/7 ids that are NOT data-dependent (M31, M32, M33, M35,
# M36 -- M34 is handled separately below, like M21/M23/M29, because its
# `counted_in_go` is the one legitimately dynamic case, Decision P4-105/
# P4-108).
_PHASE3_NON_FAMILY5_COUNTED_IDS = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8a", "M8b",
    "M9", "M10", "M11", "M12", "M13", "M14", "M15", "M16",
    "M17", "M18", "M19", "M20", "M22", "A1", "A2", "A3",
    "M31", "M32", "M33", "M35", "M36",
)


def _phase3_exit_gate_healthy_results(*, m34_counted: bool = False) -> tuple[MetricResult, ...]:
    """A hand-built 41-entry composition matching the real, corrected
    shape: `M21`/`M23`/`M29` are ALWAYS uncounted (Decision P3-43/P3-46,
    unconditional -- **re-verification's own N2 finding**: M23 and M29
    read `status="FAIL", passed=False` in the REAL exit-gate report, since
    each is a statistic that cannot honestly read a pass by its own
    definition, not a passing check nobody wants counted; M21 predates
    Phase 3 (Family 4) and is uncounted the same way). `M34` defaults to
    uncounted too, matching TODAY's real corpus state (Decision P4-105/
    P4-108's three conjunctive gates all FAIL against the committed
    `artifacts/media_corpus.json`), but `m34_counted=True` flips it to the
    OTHER legitimate state -- this assertion must accept both, and the
    two variants below (`test_phase3_exit_gate_healthy_fixture_*`) prove
    it does. No Family-5 metric carries `status == "SKIP"`, matching a
    real exit-gate run where every Family-5 mechanism is built."""
    results = [
        _metric_result(id_, counted_in_go=True, passed=True, status="PASS")
        for id_ in _PHASE3_NON_FAMILY5_COUNTED_IDS
    ]
    results.append(
        _metric_result("M21", counted_in_go=False, passed=False, status="FAIL (interpreted)")
    )
    for id_ in ("M23", "M29"):
        results.append(_metric_result(id_, counted_in_go=False, passed=False, status="FAIL"))
    for id_ in ("M23p", "M24", "M25", "M26", "M27", "M28", "M30"):
        results.append(_metric_result(id_, counted_in_go=True, passed=True, status="PASS"))
    results.append(
        _metric_result(
            "M34", counted_in_go=m34_counted, passed=m34_counted,
            status=("PASS" if m34_counted else "FAIL"),
        )
    )
    return tuple(results)


_PHASE3_EXIT_GATE_TOTAL_BANDS = len(_phase3_exit_gate_healthy_results())


def test_phase3_exit_gate_healthy_fixture_is_shaped_41_entries_37_counted():
    """Fixture self-check -- if this drifts, every test below it is
    meaningless. Default `m34_counted=False`, matching today's real
    corpus state."""
    results = _phase3_exit_gate_healthy_results()
    assert len(results) == 41
    assert len([r for r in results if r.counted_in_go]) == 37
    assert {r.id for r in results if not r.counted_in_go} == {"M21", "M23", "M29", "M34"}


def test_phase3_exit_gate_healthy_fixture_m34_counted_variant_is_38_counted():
    """The OTHER legitimate state (Decision P4-105/P4-108): a healthy
    media corpus would clear all three of M34's gates and count it."""
    results = _phase3_exit_gate_healthy_results(m34_counted=True)
    assert len(results) == 41
    assert len([r for r in results if r.counted_in_go]) == 38
    assert {r.id for r in results if not r.counted_in_go} == {"M21", "M23", "M29"}


def test_assert_phase3_exit_gate_counted_total_is_a_noop_on_a_healthy_composition():
    results = _phase3_exit_gate_healthy_results()
    # must not raise
    validate_gm._assert_phase3_exit_gate_counted_total(
        results, skip_games=False, control_check=True, total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
    )


def test_assert_phase3_exit_gate_counted_total_is_a_noop_when_m34_is_counted_instead():
    """The dynamic-override id (M34) may legitimately be counted OR
    uncounted -- neither state alone is a defect."""
    results = _phase3_exit_gate_healthy_results(m34_counted=True)
    # must not raise
    validate_gm._assert_phase3_exit_gate_counted_total(
        results, skip_games=False, control_check=True, total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
    )


def test_assert_phase3_exit_gate_counted_total_noops_when_the_three_uncounted_ids_are_absent():
    """A partial/synthetic `results` tuple -- this file's own small
    fixtures elsewhere in this module -- has nothing for this assertion to
    check, the same shape as `_assert_m28_step17_invariants`'s own "M28
    absent -> no-op" guard."""
    results = (_metric_result("M1"),)
    # must not raise
    validate_gm._assert_phase3_exit_gate_counted_total(
        results, skip_games=False, control_check=True, total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
    )


def test_assert_phase3_exit_gate_counted_total_relaxes_composition_check_under_skip_games():
    """`--skip-games` always `SKIP`s M22 (needs real games), which would
    otherwise shrink the counted set for a reason unrelated to Family-5
    wiring -- this assertion is only meaningful at a real-games run, so it
    must no-op here even though the composition below is otherwise "wrong"
    by this assertion's own rule."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise, purely because skip_games=True
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=True, control_check=True,
        total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
    )


def test_assert_phase3_exit_gate_counted_total_relaxes_composition_check_under_no_control_check():
    """**Re-verification's own B1 finding**: `--no-control-check` legitimately
    `SKIP`s M27/M28/M29 (Decision P3-49's own SKIP guards in `metrics.py`,
    since none of the three can compute without the control pass) -- a real
    live `python -m tools.validate_gm --skip-games --no-control-check`
    invocation tracebacked here before this fix, because the first version
    of this assertion only stepped aside on `skip_games`, not on the flag
    that actually causes the legitimate SKIPs. This is the regression test
    for that live crash, built the same shape as the skip_games test above
    (with-games, so the skip_games gate does NOT fire, isolating the
    control_check gate)."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M27", "M28", "M29"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise, purely because control_check=False -- note skip_games=False
    # (with-games) so the OTHER gate cannot be masking this one
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=False, control_check=False,
        total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
    )


def test_assert_phase3_exit_gate_counted_total_raises_when_a_metric_is_missing_entirely():
    """The failure mode the composition check alone cannot see: a metric
    silently DROPPED from `compute_all`'s own output (never present in
    `results` at all, as opposed to present-but-uncounted). `len(results)`
    no longer equals `total_bands`, which is the independent, bands-file-
    derived ground truth this checks against."""
    results = [r for r in _phase3_exit_gate_healthy_results() if r.id != "M22"]
    with pytest.raises(RuntimeError, match="expected exactly 41 metric results"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_raises_when_an_unexpected_metric_is_uncounted():
    """Corrupt input: M22 silently `SKIP`s (e.g. a wiring regression) while
    every permanently- and dynamically-uncounted id stays in its normal
    place -- M22 is neither, so it must be flagged by name."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M22'\]"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_the_round_12_mirror_failure():
    """The composition failure a plain count check cannot see: M22
    silently `SKIP`s (uncounted) WHILE M23 is silently restored to
    `counted_in_go=True` -- the total counted is unchanged, but the wrong
    metric is missing from the counted set. This is the exact case round
    12 added its own conjunct to catch (§9: "a permanently uncounted metric
    introduces the mirror failure, where the denominator is right and its
    composition is not")."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
        elif r.id == "M23":
            results[i] = dataclasses.replace(r, counted_in_go=True)
    with pytest.raises(RuntimeError, match="round-12 conjunct failed"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_against_the_real_bands_file():
    """Blocking finding 4 (round-21 fix dispatch): before this test, the
    ONLY coverage of this assertion was the hand-built fixture above,
    which never once touched the real `seeds/gm_validation_bands.json` --
    so a stale hardcoded total (the literal bug this dispatch fixed) could
    never have been caught by the existing suite. This test builds its
    `results` ids from the REAL, loaded bands file (`_BANDS`, module-level
    `load_bands(DEFAULT_BANDS_PATH)`) rather than a hand-maintained id
    list, so it fails loudly the day the bands file's shape moves in a way
    `_PHASE3_PERMANENTLY_UNCOUNTED_IDS`/`_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_
    IDS` no longer account for."""
    real_ids = sorted(_BANDS.metrics)
    assert len(real_ids) == 41, (
        "seeds/gm_validation_bands.json's own shape has moved -- re-verify "
        "validate_gm._PHASE3_PERMANENTLY_UNCOUNTED_IDS/_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS "
        "still name every statically- or dynamically-uncounted id before trusting this test."
    )
    results = []
    for id_ in real_ids:
        if id_ in (
            validate_gm._PHASE3_PERMANENTLY_UNCOUNTED_IDS
            | validate_gm._PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS
        ):
            # Today's real state for M34 too (Decision P4-108: the
            # committed media corpus fails all three of its gates) -- the
            # m34-counted variant is exercised separately, above, against
            # the hand-built fixture.
            results.append(_metric_result(id_, counted_in_go=False, passed=False, status="FAIL"))
        else:
            results.append(_metric_result(id_, counted_in_go=True, passed=True, status="PASS"))
    # must not raise
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=False, control_check=True, total_bands=len(_BANDS.metrics),
    )


# ---------------------------------------------------------------------------
# BLOCKING 1 (round-22 fix dispatch, re-verification of eb519b1): the prior
# fix round's own assertion above treated ANY uncounted id outside
# `{M21, M23, M29, M34}` as a wiring bug, which hard-crashes a real
# `--no-media-corpus` gate run -- Decision P4-100 item 12's own disclosed
# condition -- since M33/M35/M36 legitimately SKIP (uncounted) with no
# media corpus present (`compute_m33`/`compute_m35`/`compute_m36`'s own
# `media_corpus is None` guard, Decision P4-34's SKIP-never-fabricate
# rule). `media_corpus_present` is the caller's own signal for this case,
# threaded from `run_validation`'s own `media_corpus is not None`.
# ---------------------------------------------------------------------------


def test_assert_phase3_exit_gate_counted_total_noops_when_media_corpus_absent_and_media_ids_skip():
    """The legitimate case this dispatch fixes: with no media corpus
    loaded, M33/M35/M36 all SKIP (uncounted) by design, and must NOT be
    flagged as a Family-5-style wiring bug the way an uncounted M22
    would be."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M33", "M35", "M36"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise -- media_corpus_present=False is the caller's own
    # signal that this SKIP composition is the legitimate, disclosed one
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=False, control_check=True,
        total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, media_corpus_present=False,
    )


def test_assert_phase3_exit_gate_counted_total_raises_when_media_present_but_ids_skip_anyway():
    """The critic's own explicitly-flagged risk: the media-corpus-absent
    allowance above must not become a PERMANENT whitelist that would hide
    a real future regression in M33/M35/M36. With `media_corpus_present=
    True` (a real corpus WAS loaded this run), the exact same uncounted-
    SKIP composition as the test above is a genuine wiring bug and must
    still raise -- proving the fix is data-dependent, not a blanket
    exemption."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M33", "M35", "M36"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(
        RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M33', 'M35', 'M36'\]"
    ):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, media_corpus_present=True,
        )


def test_assert_phase3_exit_gate_counted_total_default_media_corpus_present_is_strict():
    """`media_corpus_present` defaults to `True` -- every one of this
    file's OTHER tests above, which never pass it, must keep today's
    strict pre-fix behavior unchanged (a single uncounted M33 with no
    stated corpus-absence must still raise)."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M33":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M33'\]"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_run_validation_threads_media_corpus_presence_into_the_counted_total_assertion(
    monkeypatch, tmp_path
):
    """Wiring check for BLOCKING 1: `run_validation` must tell the
    assertion whether the media corpus was actually loaded this run
    (`media_corpus is not None`), never just rely on the assertion's own
    default -- `--no-media-corpus` forces `media_corpus=None`
    unconditionally (the load block's own comment, above), so this must
    observe `False` end-to-end through `run_validation`."""
    captured: dict = {}

    def _spy_counted_total(results, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        validate_gm, "_assert_phase3_exit_gate_counted_total", _spy_counted_total
    )
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results, use_media_corpus=False)
    assert captured["media_corpus_present"] is False


def test_assert_phase3_exit_gate_counted_total_raises_when_media_absent_but_allowed_id_counted():
    """NIT 2 (round-22 fix dispatch, critic's report): the media-corpus-
    absent allowance must catch the OTHER direction too -- an id in
    `_PHASE3_MEDIA_CORPUS_ABSENT_UNCOUNTED_IDS` turning up COUNTED while
    `media_corpus_present=False` means a value got produced with no real
    corpus loaded to produce it from, the mirror of `restored` catching a
    permanently-uncounted id turning up counted."""
    # M33 is left as-is (counted=True, its real fixture state) despite
    # `media_corpus_present=False` below -- the fabrication this check
    # exists to catch: a value produced with no real corpus loaded to
    # produce it from. M35/M36 are flipped to their legitimate SKIP so
    # they don't ALSO trip the ordinary `unexpected_uncounted` check.
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M35", "M36"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(
        RuntimeError,
        match=r"media-corpus-absent-allowed metric\(s\) unexpectedly counted.*\['M33'\]",
    ):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, media_corpus_present=False,
        )


# ---------------------------------------------------------------------------
# BLOCKING 1, second round (round-22 fix dispatch, re-verification of
# 640962a): the media-corpus-absent allowance above only covers 3 of the 6
# ids Decision P4-100 item 12 itself discloses as legitimately droppable
# below real gate scale (M31/M32 drop for their own, unrelated agency-data-
# thinness reasons; M6/M10/M13/M16 -- and any other metric -- can drop for
# their own cohort-size-thinness reasons too, none of it media-specific).
# Critic reproduced the still-broken case directly: `python -m tools.
# validate_gm --seeds 1 --seasons 1 --no-determinism-check --no-media-
# corpus` still raised, citing `M10`/`M13`/`M16`/`M32`/`M6` as unexpected.
# The fix is a real scale guard (`below_gate_scale`, mirroring `_decide_
# go`'s own `exit_criterion.min_seeds`/`min_seasons` NO-GO rule), not a
# longer hardcoded id list.
# ---------------------------------------------------------------------------


def test_min_gate_scale_reads_exit_criterion_with_documented_defaults():
    """`_min_gate_scale` is the single source of truth `_decide_go` and
    `_assert_phase3_exit_gate_counted_total` both now read from -- the
    real bands file's own `exit_criterion` (or the `3`/`5` defaults this
    module's own docstring names, `--seeds 3 --seasons 5`)."""
    assert validate_gm._min_gate_scale(_BANDS) == (
        _BANDS.exit_criterion.get("min_seeds", 3), _BANDS.exit_criterion.get("min_seasons", 5),
    )


def test_assert_phase3_exit_gate_counted_total_relaxes_composition_check_when_below_gate_scale():
    """The critic's exact repro shape: a small-scale run (1 seed x 1
    season) legitimately thins out a whole family of UNRELATED metrics
    (M6/M10/M13/M16's own cohort-size guards, M32's own "no ladder
    activity observed" guard) that have nothing to do with Family-5 wiring
    or media at all -- `below_gate_scale=True` fully relaxes the
    composition (`unexpected_uncounted`) check, but NOT the whole
    function: the arity/restored/media-fabrication checks stay
    unconditional even here (see the round-24 fix and its own
    combinatorial regression tests below/elsewhere in this file) -- this
    test only proves the composition check's own relaxation, on a
    fixture with no other defect present."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M6", "M10", "M13", "M16", "M32"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise -- below_gate_scale=True is the caller's own signal
    # that this run cannot gate and its composition is not held to the
    # strict invariant
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=False, control_check=True,
        total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=True,
    )


def test_assert_phase3_exit_gate_counted_total_still_raises_at_gate_scale_for_same_composition():
    """The regression proof the guard above needs: the IDENTICAL
    composition as the previous test, but `below_gate_scale=False`
    (today's real default, and what a real `--seeds 3 --seasons 5` run
    passes) -- must still raise, proving the new guard is data-dependent,
    never a blanket exemption that would hide a real gate-scale
    regression."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M6", "M10", "M13", "M16", "M32"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(
        RuntimeError,
        match=r"Unexpected uncounted metric\(s\): \['M10', 'M13', 'M16', 'M32', 'M6'\]",
    ):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=False,
        )


def test_assert_phase3_exit_gate_counted_total_default_below_gate_scale_is_strict():
    """`below_gate_scale` defaults to `False` -- every one of this file's
    OTHER tests above, which never pass it, must keep their existing
    strict behavior unchanged."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M6":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M6'\]"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


# ---------------------------------------------------------------------------
# `determinism_check` no-op -- found by this dispatch's own required
# adversarial testing (trying `--seeds 3 --seasons 5 --no-determinism-check`,
# a real gate-scale run `below_gate_scale` alone does not catch): `compute_
# m6` SKIPs whenever no duplicate-seed pair is present, which is exactly
# what `--no-determinism-check` produces even at real gate scale. Mirrors
# `skip_games`/`control_check` above -- a documented CLI dev-loop escape
# hatch, not a named-id allowance.
# ---------------------------------------------------------------------------


def test_assert_phase3_exit_gate_counted_total_relaxes_composition_under_no_determinism_check():
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M6":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise -- determinism_check=False is the caller's own signal
    # that M6's SKIP is `--no-determinism-check`'s own documented effect
    validate_gm._assert_phase3_exit_gate_counted_total(
        tuple(results), skip_games=False, control_check=True,
        total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, determinism_check=False,
    )


def test_assert_phase3_exit_gate_counted_total_still_raises_when_determinism_check_true():
    """The regression proof: the IDENTICAL M6-uncounted composition, but
    `determinism_check=True` (today's real default and CLI default) --
    must still raise, proving the guard is flag-dependent, not a blanket
    exemption for M6."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M6":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M6'\]"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, determinism_check=True,
        )


# ---------------------------------------------------------------------------
# Fifth fix round (this dispatch): the blanket early-return that used to sit
# at the top of `_assert_phase3_exit_gate_counted_total` (`if skip_games or
# not control_check or below_gate_scale or not determinism_check: return`)
# disabled the arity check, the `restored` check, and the `media_corpus_
# fabricated` check for ALL FOUR of those conditions -- none of which have
# anything to do with run scale or CLI dev-loop flags. The tests below prove
# each of those three checks now fires regardless of which of the four
# conditions is active, closing the exact swallowing shape an independent
# critic reproduced live (`--seeds 3 --seasons 5 --no-determinism-check`
# silently returning GO with M21 wrongly counted or a metric missing
# entirely).
# ---------------------------------------------------------------------------


def test_assert_phase3_exit_gate_counted_total_raises_on_restored_id_under_skip_games():
    """The `restored` check (M21/M23/M29 turning up counted) must fire even
    under `skip_games=True` -- a permanently-uncounted id turning up
    COUNTED can never be a legitimate `--skip-games` effect, only a wiring
    regression."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M21":
            results[i] = dataclasses.replace(r, counted_in_go=True)
        elif r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match="round-12 conjunct failed"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=True, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_restored_id_under_no_control_check():
    """Same shape, isolating `control_check=False` instead."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M23":
            results[i] = dataclasses.replace(r, counted_in_go=True)
        elif r.id in ("M27", "M28", "M29"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match="round-12 conjunct failed"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=False,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_restored_id_under_below_gate_scale():
    """Same shape, isolating `below_gate_scale=True` -- the critic's exact
    scenario: a real gate-scale run cannot exist below gate scale, but this
    proves the fix even for the maximally-relaxed condition, where
    `unexpected_uncounted` itself steps aside entirely (see the function's
    own docstring) -- `restored` must still catch it regardless."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M29":
            results[i] = dataclasses.replace(r, counted_in_go=True, passed=True, status="PASS")
        elif r.id in ("M6", "M10", "M13", "M16", "M32"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match="round-12 conjunct failed"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=True,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_restored_id_under_no_determinism_check():
    """The critic's exact repro shape: a REAL gate-scale run
    (`below_gate_scale=False`) with `--no-determinism-check` and M21 wrongly
    counted must still raise -- before this fix round, the blanket
    `not determinism_check` arm swallowed this entirely."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M21":
            results[i] = dataclasses.replace(r, counted_in_go=True)
        elif r.id == "M6":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match="round-12 conjunct failed"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=False,
            determinism_check=False,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_missing_metric_under_skip_games():
    """The arity check (a metric dropped from `compute_all`'s own output
    entirely) must fire even under `skip_games=True`."""
    results = [r for r in _phase3_exit_gate_healthy_results() if r.id != "M30"]
    with pytest.raises(RuntimeError, match="expected exactly 41 metric results"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=True, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_missing_metric_under_below_gate_scale():
    """Same shape, isolating `below_gate_scale=True` -- the maximally-
    relaxed condition for `unexpected_uncounted`. A metric missing from
    `results` entirely must still raise, since the arity check is
    unconditional."""
    results = [r for r in _phase3_exit_gate_healthy_results() if r.id != "M30"]
    with pytest.raises(RuntimeError, match="expected exactly 41 metric results"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=True,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_missing_metric_no_determinism_check():
    """The critic's exact repro shape: a real gate-scale run with
    `--no-determinism-check` and M30 missing from `results` entirely must
    still raise."""
    results = [r for r in _phase3_exit_gate_healthy_results() if r.id != "M30"]
    with pytest.raises(RuntimeError, match="expected exactly 41 metric results"):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=False,
            determinism_check=False,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_media_fabrication_under_skip_games():
    """The `media_corpus_fabricated` check must fire even under
    `skip_games=True`, proving it is now reachable at ANY scale/flag
    combination -- not just the ~2-hour real-games `--seeds 3 --seasons 5`
    corpus-absent configuration the blanket guard used to require to even
    reach this check at all."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M35", "M36"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
        elif r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(
        RuntimeError,
        match=r"media-corpus-absent-allowed metric\(s\) unexpectedly counted.*\['M33'\]",
    ):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=True, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, media_corpus_present=False,
        )


def test_assert_phase3_exit_gate_counted_total_raises_on_media_fabrication_under_below_gate_scale():
    """Same shape, isolating `below_gate_scale=True` -- proves media-
    fabrication detection survives even the maximally-relaxed
    `unexpected_uncounted` condition."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M35", "M36"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(
        RuntimeError,
        match=r"media-corpus-absent-allowed metric\(s\) unexpectedly counted.*\['M33'\]",
    ):
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, below_gate_scale=True,
            media_corpus_present=False,
        )


def test_assert_phase3_exit_gate_counted_total_no_media_absent_clause_when_corpus_present():
    """N6 (this fix round, critic's nit list): the media-corpus-absent
    clause must NOT appear in the raised message when `media_corpus_
    present=True` -- the concept is meaningless with a corpus actually
    loaded, and the message used to append it unconditionally."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M22":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError) as exc_info:
        validate_gm._assert_phase3_exit_gate_counted_total(
            tuple(results), skip_games=False, control_check=True,
            total_bands=_PHASE3_EXIT_GATE_TOTAL_BANDS, media_corpus_present=True,
        )
    assert "media-corpus-absent-allowed" not in str(exc_info.value)


def test_is_below_gate_scale_matches_min_gate_scale_for_both_callers():
    """N4 (this fix round, critic's nit list): `_min_seeds`/`_min_seasons`
    comparison operator (`<`) is now factored into one predicate,
    `_is_below_gate_scale`, so `_decide_go` and `run_validation` can never
    drift on the operator the way they could when each wrote out its own
    `seeds < min_seeds or seasons < min_seasons` comparison."""
    min_seeds, min_seasons = validate_gm._min_gate_scale(_BANDS)
    assert validate_gm._is_below_gate_scale(
        seeds=min_seeds, seasons=min_seasons, bands=_BANDS
    ) is False
    assert validate_gm._is_below_gate_scale(
        seeds=min_seeds - 1, seasons=min_seasons, bands=_BANDS
    ) is True
    assert validate_gm._is_below_gate_scale(
        seeds=min_seeds, seasons=min_seasons - 1, bands=_BANDS
    ) is True


def test_run_validation_does_not_crash_at_gate_scale_with_no_determinism_check(
    monkeypatch, tmp_path,
):
    """The real, previously-live-crashing shape this guard fixes, driven
    through `run_validation`'s own real wiring (not just the assertion
    function in isolation): `--seeds 3 --seasons 5 --no-determinism-check`
    is a real gate-scale run (`below_gate_scale=False`), so ONLY the new
    `determinism_check` guard -- not the scale guard -- can be what keeps
    this from crashing on M6's legitimate SKIP."""
    monkeypatch.setattr(validate_gm, "_assert_m28_step17_invariants", lambda results: None)
    monkeypatch.setattr(validate_gm, "_assert_m27_null_control", lambda m27_null_result: None)
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M6":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise
    _run(
        monkeypatch, tmp_path, tuple(results), seeds=3, seasons=5, control_check=True,
        determinism_check=False,
    )


def test_run_validation_threads_determinism_check_into_the_counted_total_assertion(
    monkeypatch, tmp_path
):
    """Wiring check: `run_validation` must tell the assertion the run's own
    actual `determinism_check` flag, never just rely on the assertion's
    own default."""
    captured: dict = {}

    def _spy_counted_total(results, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        validate_gm, "_assert_phase3_exit_gate_counted_total", _spy_counted_total
    )
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results, determinism_check=False)
    assert captured["determinism_check"] is False


def test_run_validation_threads_below_gate_scale_into_the_counted_total_assertion(
    monkeypatch, tmp_path
):
    """Wiring check: `run_validation` must compute `below_gate_scale` from
    the run's OWN actual `seeds`/`seasons` against the loaded bands file's
    own `exit_criterion` (via `_min_gate_scale`), never just rely on the
    assertion's own default -- a 1-seed/0-season run (this file's own `_run`
    default) is below the real `3`/`5` gate scale, so this must observe
    `True` end-to-end through `run_validation`."""
    captured: dict = {}

    def _spy_counted_total(results, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        validate_gm, "_assert_phase3_exit_gate_counted_total", _spy_counted_total
    )
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results)
    assert captured["below_gate_scale"] is True


def test_run_validation_threads_below_gate_scale_false_at_real_gate_scale(monkeypatch, tmp_path):
    """The mirror of the above -- a run that actually meets `exit_criterion`
    (`--seeds 3 --seasons 5`, the real bands file's own default) must
    thread `below_gate_scale=False`, so the assertion still holds a real
    gate-scale run to its normal strict rule."""
    captured: dict = {}

    def _spy_counted_total(results, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        validate_gm, "_assert_phase3_exit_gate_counted_total", _spy_counted_total
    )
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results, seeds=3, seasons=5)
    assert captured["below_gate_scale"] is False


# ---------------------------------------------------------------------------
# BLOCKING 2 (round-22 fix dispatch): critic proved the real call site's own
# `media_corpus_present=media_corpus is not None` wiring (`run_validation`'s
# own call to `_assert_phase3_exit_gate_counted_total`) had ZERO coverage --
# hardcoding `media_corpus_present=False` there on an isolated copy left
# every one of this file's 81 tests passing, because every existing strict-
# direction test above calls the assertion FUNCTION directly, never through
# `run_validation`'s own real wiring line. This drives a REAL, committed-
# shaped media corpus through `run_validation`'s own loader, at real gate
# scale (so `below_gate_scale` cannot mask it either), proving the strict
# direction still works end to end, not just in isolation.
# ---------------------------------------------------------------------------


def test_run_validation_raises_through_real_wiring_when_media_present_but_m33_skips_anyway(
    monkeypatch, tmp_path,
):
    """Isolates the ONE thing under test (`_assert_phase3_exit_gate_counted_
    total`'s own real call-site wiring) by no-opping the two OTHER §11
    step 17/18 assertions `run_validation` also calls -- this fixture's
    hand-built `M28`/`M27` entries carry no real `detail` (`_metric_
    result`'s own `detail={}` default), which `_assert_m28_step17_
    invariants`/`_assert_m27_null_control` would otherwise flag first, for
    a reason unrelated to what this test checks."""
    monkeypatch.setattr(validate_gm, "_assert_m28_step17_invariants", lambda results: None)
    monkeypatch.setattr(validate_gm, "_assert_m27_null_control", lambda m27_null_result: None)
    path = tmp_path / "media_corpus.json"
    _write_media_corpus(path, library_sha=_real_media_library_sha())
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M33":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"Unexpected uncounted metric\(s\): \['M33'\]"):
        _run(
            monkeypatch, tmp_path, tuple(results), seeds=3, seasons=5, control_check=True,
            determinism_check=True, media_corpus_path=path,
        )


def test_assert_no_family5_skip_at_exit_gate_is_a_noop_on_a_healthy_composition():
    # must not raise
    validate_gm._assert_no_family5_skip_at_exit_gate(
        _phase3_exit_gate_healthy_results(), control_check=True
    )


def test_assert_no_family5_skip_at_exit_gate_noops_when_no_family5_metric_is_present():
    results = (_metric_result("M1"),)
    # must not raise
    validate_gm._assert_no_family5_skip_at_exit_gate(results, control_check=True)


def test_assert_no_family5_skip_at_exit_gate_noops_under_no_control_check():
    """**Re-verification's own B1 finding**, mirroring the counted-total
    regression test above -- `--no-control-check` legitimately `SKIP`s
    M27/M28/M29, and this assertion must step aside for that flag exactly
    as `_assert_m28_step17_invariants` already does for M28's own SKIP."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M27", "M28", "M29"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    # must not raise, purely because control_check=False
    validate_gm._assert_no_family5_skip_at_exit_gate(tuple(results), control_check=False)


def test_assert_no_family5_skip_at_exit_gate_raises_on_a_family5_skip():
    """A Family-5 metric (M28) carrying `status == "SKIP"` at the exit gate
    is a wiring bug wearing a green hat per Decision P3-37 -- every
    Family-5 mechanism is built by step 18, so a SKIP here is never
    benign."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id == "M28":
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"\['M28'\]"):
        validate_gm._assert_no_family5_skip_at_exit_gate(tuple(results), control_check=True)


def test_assert_no_family5_skip_at_exit_gate_names_every_offender():
    """More than one simultaneous Family-5 SKIP must be named together in
    one raise, not just the first found."""
    results = list(_phase3_exit_gate_healthy_results())
    for i, r in enumerate(results):
        if r.id in ("M26", "M28"):
            results[i] = dataclasses.replace(
                r, counted_in_go=False, passed=None, status="SKIP", value=None
            )
    with pytest.raises(RuntimeError, match=r"\['M26', 'M28'\]"):
        validate_gm._assert_no_family5_skip_at_exit_gate(tuple(results), control_check=True)


def test_run_validation_calls_the_phase3_exit_gate_assertions_after_write_report(
    monkeypatch, tmp_path
):
    """Wiring check -- both new §11 step 18 assertions must be reachable
    from `run_validation`'s own body, called AFTER `write_report`,
    mirroring `_assert_m28_step17_invariants`'s own wiring test above."""
    call_order: list[str] = []
    real_write_report = validate_gm.write_report

    def _spy_write_report(*args, **kwargs):
        call_order.append("write_report")
        return real_write_report(*args, **kwargs)

    def _spy_counted_total(
        results, *, skip_games, control_check, total_bands, media_corpus_present,
        below_gate_scale, determinism_check,
    ):
        call_order.append("assert_counted_total")

    def _spy_no_family5_skip(results, *, control_check):
        call_order.append("assert_no_family5_skip")

    monkeypatch.setattr(validate_gm, "write_report", _spy_write_report)
    monkeypatch.setattr(
        validate_gm, "_assert_phase3_exit_gate_counted_total", _spy_counted_total
    )
    monkeypatch.setattr(
        validate_gm, "_assert_no_family5_skip_at_exit_gate", _spy_no_family5_skip
    )
    results = (_metric_result("M1"),)
    _run(monkeypatch, tmp_path, results)
    assert call_order == ["write_report", "assert_counted_total", "assert_no_family5_skip"]


# ---------------------------------------------------------------------------
# `_load_media_corpus` and `run_validation`'s own `--media-corpus`/
# `--no-media-corpus` loader wiring -- Blocking findings 2 and 3 of the
# round-21 fix dispatch. Decision P4-101's ruled fail-closed staleness
# check had ZERO coverage anywhere in the suite before these tests (it is
# the ONE mitigation that makes a committed, never-live-regenerated
# corpus artifact an acceptable design choice at all), and the explicit-
# vs-default path split (Blocking 2) had a real bug: an explicit
# `--media-corpus <typo>` used to fail OPEN to `media_corpus=None`,
# silently shrinking the counted-metric denominator by 3-4 with no
# visible error.
# ---------------------------------------------------------------------------


def _real_media_library_sha() -> str:
    return hashlib.sha256(DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.read_bytes()).hexdigest()


def _write_media_corpus(path, *, library_sha: str) -> None:
    empty_arm = MediaCorpusArm(
        stories_total=0, fallback_stories=0, by_kind={}, by_voice={},
        by_voice_kind_template={}, by_pair_depth={}, cycles=(),
    )
    corpus = MediaCorpusTotals(
        schema_version=1, library_sha=library_sha, library_path="seeds/media_phrases.json",
        league_rng_seed=1, seed_root="test:1", leagues=1, seasons=1,
        generated_at="2026-01-01T00:00:00Z", top_k_per_cycle=12, salience_floor=0.3,
        repeat_penalty=0.3, declared_fallback_weight_share=1 / 3,
        persona_exclusivity_floor=0.5, ungated_kinds=(), live=empty_arm, control=None,
    )
    path.write_text(json.dumps(corpus.to_json()), encoding="utf-8")


def test_load_media_corpus_succeeds_when_the_sha_matches(tmp_path):
    path = tmp_path / "media_corpus.json"
    real_sha = _real_media_library_sha()
    _write_media_corpus(path, library_sha=real_sha)
    corpus = validate_gm._load_media_corpus(path)
    assert corpus.library_sha == real_sha


def test_load_media_corpus_fails_closed_on_a_sha_mismatch(tmp_path):
    """Decision P4-101's ruled staleness check. Before this test, `grep -rn
    "_load_media_corpus\\|library_sha" tests/` found only a fixture string
    -- this check had never actually been exercised."""
    path = tmp_path / "media_corpus.json"
    _write_media_corpus(path, library_sha="0" * 64)
    with pytest.raises(RuntimeError, match="STALE"):
        validate_gm._load_media_corpus(path)


def test_run_validation_no_media_corpus_forces_none_unconditionally(monkeypatch, tmp_path):
    """`--no-media-corpus` forces `media_corpus=None` regardless of
    whatever `--media-corpus` path was ALSO passed, and never raises even
    when that path does not exist -- the third case Blocking finding 2's
    fix must preserve unchanged."""
    results = (_metric_result("M1"),)
    _, _, spy = _run(
        monkeypatch, tmp_path, results,
        use_media_corpus=False, media_corpus_path=tmp_path / "does-not-exist.json",
    )
    assert spy.calls[0]["media_corpus"] is None


def test_run_validation_default_media_corpus_path_missing_falls_back_to_none(
    monkeypatch, tmp_path
):
    """No `--media-corpus` flag at all, and no file at the (monkeypatched,
    so this test does not depend on whether the real committed corpus
    happens to exist) default path -- must silently fall back to `None`
    (the media family SKIPs, Decision P4-34), never raise. This is the
    ONLY case allowed to fail this way; see the explicit-path test below
    for the case that must NOT."""
    monkeypatch.setattr(validate_gm, "_DEFAULT_MEDIA_CORPUS_PATH", tmp_path / "nope.json")
    results = (_metric_result("M1"),)
    _, _, spy = _run(monkeypatch, tmp_path, results)
    assert spy.calls[0]["media_corpus"] is None


def test_run_validation_explicit_media_corpus_path_missing_raises(monkeypatch, tmp_path):
    """Blocking finding 2's core fix: an explicitly-named `--media-corpus`
    path that does not exist must raise loudly. Before this fix it fell
    through to `media_corpus=None`, silently SKIPping all four of M33-M36
    with no visible error -- exactly the risk Decision P4-100 item 12
    already disclosed."""
    missing = tmp_path / "typo.json"
    results = (_metric_result("M1"),)
    with pytest.raises(RuntimeError, match=r"--media-corpus .* does not exist"):
        _run(monkeypatch, tmp_path, results, media_corpus_path=missing)


def test_run_validation_explicit_media_corpus_path_loads_when_present(monkeypatch, tmp_path):
    """The mirror of the above: an explicit path that DOES exist and has a
    matching `library_sha` loads successfully and is threaded through to
    `compute_all` as a real `MediaCorpusTotals`, not `None`."""
    path = tmp_path / "media_corpus.json"
    real_sha = _real_media_library_sha()
    _write_media_corpus(path, library_sha=real_sha)
    results = (_metric_result("M1"),)
    _, _, spy = _run(monkeypatch, tmp_path, results, media_corpus_path=path)
    assert spy.calls[0]["media_corpus"] is not None
    assert spy.calls[0]["media_corpus"].library_sha == real_sha


def test_compute_all_media_family_all_skip_through_the_real_no_media_corpus_cli_path(
    monkeypatch, tmp_path
):
    """Blocking finding 3's fourth requirement: the existing sibling test
    in `test_metrics.py` (`test_compute_all_media_family_all_skip_never_
    pass_when_media_corpus_is_none`) calls `compute_all(media_corpus=None)`
    directly, which never touches the loader/CLI code this dispatch fixed.
    This test drives `run_validation` with `--no-media-corpus`'s own real
    flag (`use_media_corpus=False`), using the REAL `compute_all` (never
    monkeypatched) against `_fake_run_seed`'s minimal `RunRecord` -- so the
    actual loader-to-`compute_all` wire is what is under test, and the
    real M33-M36 SKIP behavior confirmed at the end of it."""
    monkeypatch.setattr(validate_gm, "run_seed", _fake_run_seed)
    results, _go = validate_gm.run_validation(
        seeds=1, seasons=0, players_per_team=1, rng_seed=1, season_year=2026,
        skip_games=False, determinism_check=False, control_check=False,
        family3_precert=True, protected_pick_share=0.0, draft_divergence_drafts=1,
        bands_path=DEFAULT_BANDS_PATH, out_dir=tmp_path,
        use_media_corpus=False,
    )
    media_results = {r.id: r for r in results if r.id in ("M33", "M34", "M35", "M36")}
    assert set(media_results) == {"M33", "M34", "M35", "M36"}
    for metric_id, result in media_results.items():
        assert result.status == "SKIP", f"{metric_id} must SKIP, read {result.status!r}"
        assert result.counted_in_go is False
