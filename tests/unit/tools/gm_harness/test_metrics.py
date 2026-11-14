"""`tools/gm_harness/metrics.py`/`bands.py` -- the 22-metric computation
layer (T7b). Direct unit tests against hand-built `RunRecord` fixtures,
per this slice's own dispatch: no 10+ minute real run behind any of these
(the one disclosed exception, M17, still runs in well under a second at
`drafts=2`) -- see `metrics.py`'s own module docstring for every judgment
call these tests are written against.

Every one of the 22 metrics + A1/A2/A3 gets at least one direct test. The
tricky ones (9, 14, M1/M2's boundary-scoped semantics, 17, 21's
"interpreted" reframing) get an extra, deliberately discriminating test --
one that would fail under a plausible wrong implementation, not just one
that happens to pass under the real one.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import pickle
import statistics
from pathlib import Path

import pytest

from dba2.core.rng import rng_for
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy
from dba2.managers.capsheet import SeasonCapProjection
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.services.gm_service import GM_BEHAVIOR_SCOPE_VERSION
from tools.gm_harness.bands import evaluate, load_bands
from tools.gm_harness.media_corpus import (
    KindOrVoiceSplit,
    MediaCorpusArm,
    MediaCorpusTotals,
    MediaCycleRecord,
    PairDepthBin,
)
from tools.gm_harness.metrics import (
    _FAMILY3_CITATION_VERDICT_KEYS,
    _PATH_SHAPED_DETAIL_FIELDS,
    DEFAULT_GM_DECISION_LOG_DIGEST_PATH,
    DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH,
    EXCLUDED_REPORT_FIELDS,
    Family3CitationStatus,
    MetricValue,
    _family3_citation_reason,
    _family3_citation_status,
    _jackknife_sd_over_seeds,
    _log_or_none,
    _m10_is_room_buyer,
    _m11_raw_value,
    _m12_raw_value,
    _m13_raw_value,
    _m15_raw_value,
    _m16_core,
    _m16_raw_value,
    _m17_precision_detail,
    _salary_cap,
    compare_decision_log_digest,
    compare_exit_gate_reports,
    compute_all,
    compute_m17,
    diff_decision_log_lines,
    load_phase2_exit_gate_baseline,
    season_decision_log_digest,
)
from tools.gm_harness.metrics import _finalize as _finalize_metric
from tools.gm_harness.metrics import compute_a1 as _compute_a1
from tools.gm_harness.metrics import compute_m1 as _compute_m1
from tools.gm_harness.metrics import compute_m2 as _compute_m2
from tools.gm_harness.metrics import compute_m3 as _compute_m3
from tools.gm_harness.metrics import compute_m4 as _compute_m4
from tools.gm_harness.metrics import compute_m5 as _compute_m5
from tools.gm_harness.metrics import compute_m6 as _compute_m6
from tools.gm_harness.metrics import compute_m7 as _compute_m7
from tools.gm_harness.metrics import compute_m8a as _compute_m8a
from tools.gm_harness.metrics import compute_m8b as _compute_m8b
from tools.gm_harness.metrics import compute_m9 as _compute_m9
from tools.gm_harness.metrics import compute_m10 as _compute_m10
from tools.gm_harness.metrics import compute_m11 as _compute_m11
from tools.gm_harness.metrics import compute_m12 as _compute_m12
from tools.gm_harness.metrics import compute_m13 as _compute_m13
from tools.gm_harness.metrics import compute_m14 as _compute_m14
from tools.gm_harness.metrics import compute_m15 as _compute_m15
from tools.gm_harness.metrics import compute_m16 as _compute_m16
from tools.gm_harness.metrics import compute_m18 as _compute_m18
from tools.gm_harness.metrics import compute_m19 as _compute_m19
from tools.gm_harness.metrics import compute_m20 as _compute_m20
from tools.gm_harness.metrics import compute_m21 as _compute_m21
from tools.gm_harness.metrics import compute_m22 as _compute_m22
from tools.gm_harness.metrics import compute_m23 as _compute_m23
from tools.gm_harness.metrics import compute_m23p as _compute_m23p
from tools.gm_harness.metrics import compute_m24 as _compute_m24
from tools.gm_harness.metrics import compute_m25 as _compute_m25
from tools.gm_harness.metrics import compute_m26 as _compute_m26
from tools.gm_harness.metrics import compute_m27 as _compute_m27
from tools.gm_harness.metrics import compute_m28 as _compute_m28
from tools.gm_harness.metrics import compute_m29 as _compute_m29
from tools.gm_harness.metrics import compute_m30 as _compute_m30
from tools.gm_harness.metrics import compute_m31 as _compute_m31
from tools.gm_harness.metrics import compute_m32 as _compute_m32
from tools.gm_harness.metrics import compute_m33 as _compute_m33
from tools.gm_harness.metrics import compute_m34 as _compute_m34
from tools.gm_harness.metrics import compute_m35 as _compute_m35
from tools.gm_harness.metrics import compute_m36 as _compute_m36
from tools.gm_harness.metrics import m34_null_check_readings as _m34_null_check_readings
from tools.gm_harness.season import Acquisition, BoundaryRecord, RunRecord, SeasonRecord


def _strip_family3_precert(bands):
    """Decision P3-47 (Sec.11 step 9's Family-3 measurability fix) added a
    real, VALID `params.gated_value_precertified` citation to the live
    M11-M16 bands entries -- so `_compute_m11(...)` etc. against the bare
    `_BANDS` object now gate on the 45-seed citation, not the fixture's own
    hand-built live value. Every pre-P3-47 test in this file that exercises
    the LIVE computation (`.value == <hand-computed number>`, `detail
    ["se_measured"]` at the top level, etc.) needs a bands object with the
    new key stripped -- the same decoupling-via-local-`dataclasses.replace`
    pattern this suite already uses everywhere for `se_precertified`/
    `precision` staleness, generalized to strip the new key from all six
    Family-3 entries in one pass rather than six separate call sites."""
    metrics = {}
    for metric_id, band in bands.metrics.items():
        if "gated_value_precertified" in band.params:
            params = {k: v for k, v in band.params.items() if k != "gated_value_precertified"}
            band = dataclasses.replace(band, params=params)
        metrics[metric_id] = band
    return dataclasses.replace(bands, metrics=metrics)


def _add_family3_behavior_scope(bands):
    """Decision P4-46/P4-72's scope conjunct, stamped onto a TEST fixture
    only -- never onto `seeds/gm_validation_bands.json` itself. As of this
    commit, `seeds/gm_validation_bands.json`'s real, on-disk citations
    carry no `behavior_scope` key at all (Decision P4-73: `grep -c
    behavior_scope seeds/gm_validation_bands.json` -> `0`), which is
    genuinely, currently true and is covered by its own dedicated test
    below (`test_real_family3_citations_read_stale_for_the_scope_missing_
    reason_today`) -- fabricating a `behavior_scope` value into that file
    without having re-run the corpus is exactly the failure mode Decision
    P4-68 names ("a citation that claims a scope it was never measured
    at, and nothing downstream can tell"). This function does the opposite
    of that: it builds an IN-MEMORY fixture representing "what the corpus
    looks like the moment after Decision P4-42's own end-of-Stage-C
    re-precertification lands," so every test below that predates the
    scope conjunct (written against `_BANDS`'s real citations as `VALID`)
    keeps testing the property it was written for, without this stage's
    own mechanism addition silently flipping thirty-plus pre-existing
    assertions to a STALE reading they were never about."""
    metrics = {}
    for metric_id, band in bands.metrics.items():
        params = dict(band.params)
        if "gated_value_precertified" in params:
            params["gated_value_precertified"] = {
                **params["gated_value_precertified"],
                "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION,
            }
        if "se_precertified" in params:
            params["se_precertified"] = {
                **params["se_precertified"],
                "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION,
            }
        metrics[metric_id] = (
            dataclasses.replace(band, params=params) if params != band.params else band
        )
    return dataclasses.replace(bands, metrics=metrics)


_BANDS = _add_family3_behavior_scope(load_bands())
_BANDS0 = _strip_family3_precert(_BANDS)

# ---------------------------------------------------------------------------
# Fixture builders -- small, hand-built RunRecords, no run_seed anywhere.
# ---------------------------------------------------------------------------

_LABEL_PHASE = {
    "PRESEASON": Phase.PRESEASON,
    "post_trade_window_1": Phase.REGULAR_SEASON,
    "TRADE_DEADLINE": Phase.TRADE_DEADLINE,
    "post_draft": Phase.DRAFT,
    "free_agency_open": Phase.FREE_AGENCY,
    "season_end": Phase.FREE_AGENCY,
    "post_waivers": Phase.WAIVERS,
}


def _cap_row(
    team_id: int,
    *,
    season: int = 2026,
    committed_salary: int = 0,
    dead_cap: int = 0,
    room: int = 10_000_000,
    apron2_room: int = 20_000_000,
    roster_count: int = 15,
) -> SeasonCapProjection:
    used = committed_salary + dead_cap
    return SeasonCapProjection(
        team_id=team_id, season=season, committed_salary=committed_salary, dead_cap=dead_cap,
        cap_holds=0, team_option_salary=0, player_option_salary=0, roster_count=roster_count,
        minimum_filler=0, used=used, room=room, tax_room=room, apron1_room=apron2_room,
        apron2_room=apron2_room, include_holds=False, projected=False,
    )


def _boundary(
    label: str,
    *,
    roster_ids_by_team: dict | None = None,
    overalls_by_team: dict | None = None,
    window_by_team: dict | None = None,
    capsheet_by_team: dict | None = None,
    capsheet_with_holds_by_team: dict | None = None,
) -> BoundaryRecord:
    return BoundaryRecord(
        phase=_LABEL_PHASE[label], label=label,
        roster_ids_by_team=roster_ids_by_team or {}, overalls_by_team=overalls_by_team or {},
        window_by_team=window_by_team or {}, capsheet_by_team=capsheet_by_team or {},
        pick_owner_by_pick={}, contract_count_by_player={},
        capsheet_with_holds_by_team=capsheet_with_holds_by_team,
    )


_ALL_LABELS = (
    "PRESEASON", "post_trade_window_1", "TRADE_DEADLINE", "post_draft", "free_agency_open",
    "season_end", "post_waivers",
)


def _full_boundaries(team_ids: tuple[int, ...], *, n_players: int = 15) -> list[BoundaryRecord]:
    """One clean boundary per label, `n_players` ids per team, no cap
    breach -- the "everything holds" baseline a single-field override test
    starts from."""
    roster = {tid: tuple(range(tid * 100, tid * 100 + n_players)) for tid in team_ids}
    overalls = {
        tid: tuple(sorted((70 + i for i in range(n_players)), reverse=True)) for tid in team_ids
    }
    caps = {tid: _cap_row(tid) for tid in team_ids}
    windows = {tid: CompetitiveWindow.RETOOL for tid in team_ids}
    return [
        _boundary(label, roster_ids_by_team=roster, overalls_by_team=overalls,
                  window_by_team=windows, capsheet_by_team=caps)
        for label in _ALL_LABELS
    ]


def _season_record(season: int, boundaries: list[BoundaryRecord], **overrides) -> SeasonRecord:
    defaults = dict(
        season=season, boundaries=tuple(boundaries), trades_by_window=((), ()),
        packages_evaluated=(0, 0), mutual_accepts=(0, 0), signings=(), draft_picks=(),
        waivers=(), acquisitions=(), minutes_weighted_age_by_team={},
        def_group_mean_by_player={}, future_first_net_by_team={}, win_totals=None,
        jsonl_lines=(), conservation_violations=(), reconstruction_violations=(),
        phase_timings={},
        # Phase 3 §11 step 7 -- Decision P3-12's six new TradeWindowResult
        # counters, plus M25's live invariant count, all defaulting to a
        # counters-off reading (0/0/{}) so every pre-step-7 fixture above
        # keeps constructing a SeasonRecord without having to know these
        # fields exist.
        turns_evaluated=(0, 0), evaluations_performed=(0, 0), counter_opportunities=(0, 0),
        counters_admitted=(0, 0), counter_closed_executions=(0, 0),
        outcome_census=({}, {}), expired_stalled_count=(0, 0),
        negotiation_depth_violations=(0, 0),
    )
    defaults.update(overrides)
    return SeasonRecord(**defaults)


def _run_record(
    rng_seed: int, seasons: list[SeasonRecord], *, philosophy_by_team=None, season1_baseline=None
) -> RunRecord:
    return RunRecord(
        rng_seed=rng_seed, philosophy_by_team=philosophy_by_team or {},
        season1_top1_minus_8th_by_team=season1_baseline or {}, seasons=tuple(seasons),
    )


# ---------------------------------------------------------------------------
# Purity: metrics.py's own hard constraint (module docstring's own claim)
# ---------------------------------------------------------------------------

_FORBIDDEN_METRICS_PREFIXES = ("dba2.data",)


def test_metrics_module_never_imports_dba2_data_or_does_async_io():
    path = Path(__file__).resolve().parents[4] / "tools" / "gm_harness" / "metrics.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    offenders = [m for m in modules if any(m.startswith(p) for p in _FORBIDDEN_METRICS_PREFIXES)]
    assert offenders == [], f"metrics.py must never import dba2.data: {offenders}"
    assert "await " not in source and "async def" not in source


def test_all_runs_taking_compute_functions_accept_exactly_runs_and_bands():
    """A hand-built two-argument call succeeds for every metric except the
    four disclosed exceptions (M17/A1/A2/A3) -- proves the uniform
    `(runs, bands)` signature really holds, not just documented."""
    empty_runs: tuple[RunRecord, ...] = ()
    for func in (
        _compute_m1, _compute_m2, _compute_m3, _compute_m4, _compute_m5, _compute_m6,
        _compute_m7, _compute_m8a, _compute_m8b, _compute_m9, _compute_m10, _compute_m11,
        _compute_m12, _compute_m13, _compute_m14, _compute_m15, _compute_m16, _compute_m18,
        _compute_m19, _compute_m20, _compute_m21, _compute_m22,
        _compute_m23, _compute_m23p, _compute_m24, _compute_m25, _compute_m26, _compute_m27,
        _compute_m28, _compute_m30,
    ):
        result = func(empty_runs, _BANDS)
        assert isinstance(result, MetricValue)


# ---------------------------------------------------------------------------
# bands.py -- comparator evaluation
# ---------------------------------------------------------------------------


def test_evaluate_range_comparator():
    band = _BANDS.metrics["M18"]
    assert evaluate(band, 12) is True
    assert evaluate(band, 55) is True
    assert evaluate(band, 11.9) is False
    assert evaluate(band, 55.1) is False
    assert evaluate(band, None) is None


def test_evaluate_min_max_max_violations_comparators():
    assert evaluate(_BANDS.metrics["M14"], 1.5) is True
    assert evaluate(_BANDS.metrics["M14"], 1.4999) is False
    assert evaluate(_BANDS.metrics["M7"], 0.10) is True
    assert evaluate(_BANDS.metrics["M7"], 0.1001) is False
    assert evaluate(_BANDS.metrics["M3"], 0) is True
    assert evaluate(_BANDS.metrics["M3"], 1) is False


def test_load_bands_covers_all_41_ids():
    """26 Phase 2 entries (M1-M22/A1-A3) + 9 Phase 3 Family-5 entries
    (M23, M23p, M24-M30), landed in one commit at §11 step 7 (Decision
    P3-32's corrected arithmetic -- round 12 added M23' beside the eight
    the phase's own §11 step 7 line names, for nine total) + 2 Phase 4
    Family-6 (agency) entries (M31, M32 -- Decision P4-35, §11 Stage C
    step 14) + 4 Phase 4 Family-7 (media) entries (M33-M36 -- Decision
    P4-94, §11 Stage D step 20), for 41 total. `gm_tuning_version` stays
    3 -- every new entry since Phase 2 is service-scoped (Decision P4-3's
    own posture: zero `GM_TUNING_VERSION` bumps for anything Phase 4
    adds), never a `managers/` constant."""
    assert len(_BANDS.metrics) == 41
    # Phase 3 §11 step 4, Decision P3-30 -- unchanged by step 7 or Phase 4.
    assert _BANDS.gm_tuning_version == 3


# ---------------------------------------------------------------------------
# M1 -- roster size bounds
# ---------------------------------------------------------------------------


def test_m1_clean_run_has_zero_violations():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m1((run,), _BANDS)
    assert result.value == 0.0


def test_m1_ceiling_breach_at_preseason_is_counted():
    boundaries = _full_boundaries((1, 2))
    # Team 1 has 20 players at PRESEASON -- a real ceiling breach.
    boundaries[0] = _boundary(
        "PRESEASON",
        roster_ids_by_team={1: tuple(range(20)), 2: boundaries[0].roster_ids_by_team[2]},
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m1((run,), _BANDS)
    assert result.value == 1.0
    assert result.detail["violations_by_boundary"] == {"PRESEASON": 1}


def test_m1_overfull_post_draft_is_not_a_violation():
    """post_draft is explicitly NOT in the ceiling-checked set -- 20
    bodies there must not be flagged (the exact regression this narrowing
    exists to permit, per the module docstring's judgment call 2)."""
    boundaries = _full_boundaries((1, 2))
    post_draft_idx = _ALL_LABELS.index("post_draft")
    boundaries[post_draft_idx] = _boundary(
        "post_draft", roster_ids_by_team={1: tuple(range(20)), 2: tuple(range(15))}
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m1((run,), _BANDS)
    assert result.value == 0.0


def test_m1_post_waivers_is_floor_only_not_ceiling():
    """20 bodies at post_waivers must NOT be flagged (floor-only), but 10
    bodies (below roster_min=14) at post_waivers MUST be flagged."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("post_waivers")
    boundaries[idx] = _boundary(
        "post_waivers", roster_ids_by_team={1: tuple(range(20)), 2: tuple(range(10))}
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m1((run,), _BANDS)
    assert result.value == 1.0
    assert result.detail["violations_by_boundary"] == {"post_waivers": 1}


# ---------------------------------------------------------------------------
# M2 -- apron_2, with the disclosed known-gap boundaries
# ---------------------------------------------------------------------------


def test_m2_clean_boundary_breach_is_counted():
    boundaries = _full_boundaries((1, 2))
    boundaries[0] = _boundary(
        "PRESEASON", capsheet_by_team={1: _cap_row(1, apron2_room=-1), 2: _cap_row(2)}
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m2((run,), _BANDS)
    assert result.value == 1.0


def test_m2_known_gap_boundary_breach_is_not_counted_in_value():
    """A breach at season_end/post_waivers (the disclosed signing.py gap)
    must not inflate the gating value -- only the 5 clean boundaries do --
    but it must still be visible in `detail`, not hidden."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end", capsheet_by_team={1: _cap_row(1, apron2_room=-5), 2: _cap_row(2)}
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m2((run,), _BANDS)
    assert result.value == 0.0, "known-gap boundary must not gate the main value"
    assert result.detail["known_gap_violations"] == 1


# ---------------------------------------------------------------------------
# M2's hold-inclusive diagnostic (architect round 19 §7 item 4, Slice 19-A)
# -- reported in `detail`, never gated.
# ---------------------------------------------------------------------------


_CLEAN_LABELS = (
    "PRESEASON", "post_trade_window_1", "TRADE_DEADLINE", "post_draft", "free_agency_open",
)


def _full_boundaries_with_holds(team_ids: tuple[int, ...]) -> list[BoundaryRecord]:
    """`_full_boundaries` plus a `capsheet_with_holds_by_team` (identical
    to `capsheet_by_team`, no violations) at every one of M2's 5 clean
    boundaries -- `compute_m2`'s new diagnostic needs the field present at
    ALL 5, not just the one boundary a test overrides, or it reports
    `None` (this metric's own "any record missing -> report `None`, not a
    partial count" contract, exercised on its own below)."""
    boundaries = _full_boundaries(team_ids)
    caps = {tid: _cap_row(tid) for tid in team_ids}
    for i, label in enumerate(_ALL_LABELS):
        if label in _CLEAN_LABELS:
            b = boundaries[i]
            boundaries[i] = dataclasses.replace(b, capsheet_with_holds_by_team=dict(caps))
    return boundaries


def test_m2_hold_inclusive_violations_reported_but_never_gates_value():
    """A clean boundary that is hold-free-clean but hold-inclusive-breached
    must move `detail["hold_inclusive_violations"]`, and must NOT move
    `value`/the real gate at all -- the core contract of this diagnostic
    (round 19 §2.5 item 3/§7 item 4: "reported, never gated")."""
    boundaries = _full_boundaries_with_holds((1, 2))
    idx = _ALL_LABELS.index("PRESEASON")
    boundaries[idx] = _boundary(
        "PRESEASON",
        capsheet_by_team={1: _cap_row(1, apron2_room=5), 2: _cap_row(2)},
        capsheet_with_holds_by_team={1: _cap_row(1, apron2_room=-3), 2: _cap_row(2)},
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m2((run,), _BANDS)
    assert result.value == 0.0, "the real, hold-free gate must be unaffected"
    assert result.detail["hold_inclusive_violations"] == 1
    assert result.detail["hold_inclusive_violations_by_boundary"] == {"PRESEASON": 1}


def test_m2_gate_value_unmoved_by_real_hold_inclusive_violations():
    """The exact fixture the architect memo's own mutation targets: real,
    nonzero hold-inclusive violations coexisting with a fully clean
    hold-free run -- `value` must stay `0.0`, not read the hold-inclusive
    count. Mutation-verified by hand this round: temporarily pointed
    `value=float(clean_violations)` at `float(hold_inclusive_violations)`
    in `compute_m2` -- this test failed (`1.0 == 0.0` -> false); reverted,
    green again (see this round's own build report)."""
    boundaries = _full_boundaries_with_holds((1, 2, 3))
    idx = _ALL_LABELS.index("PRESEASON")
    boundaries[idx] = _boundary(
        "PRESEASON",
        capsheet_by_team={1: _cap_row(1), 2: _cap_row(2), 3: _cap_row(3)},
        capsheet_with_holds_by_team={
            1: _cap_row(1, apron2_room=-1), 2: _cap_row(2, apron2_room=-1), 3: _cap_row(3),
        },
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m2((run,), _BANDS)
    assert result.value == 0.0
    assert result.detail["hold_inclusive_violations"] == 2


def test_m2_hold_inclusive_diagnostics_are_none_not_zero_when_the_field_is_absent():
    """`_full_boundaries` never sets `capsheet_with_holds_by_team` (default
    `None`, the pre-round-19-pickle shape) -- both new diagnostic fields
    must report `None`, not silently default to `0` (round 19 §7 item 4's
    own explicit instruction: a missing record must not be counted as
    zero violations)."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m2((run,), _BANDS)
    assert result.detail["hold_inclusive_violations"] is None
    assert result.detail["hold_inclusive_violations_by_boundary"] is None


# ---------------------------------------------------------------------------
# M3-M5 -- straightforward counts
# ---------------------------------------------------------------------------


def test_m3_sums_conservation_violations():
    boundaries = _full_boundaries((1,))
    s1 = _season_record(2026, boundaries, conservation_violations=("bad thing 1", "bad thing 2"))
    s2 = _season_record(2027, boundaries, conservation_violations=())
    run = _run_record(1, [s1, s2])
    assert _compute_m3((run,), _BANDS).value == 2.0


def test_m4_counts_players_with_more_than_one_active_contract():
    boundaries = _full_boundaries((1,))
    boundaries[0] = _boundary("PRESEASON")
    b = boundaries[0]
    b2 = BoundaryRecord(
        phase=b.phase, label=b.label, roster_ids_by_team=b.roster_ids_by_team,
        overalls_by_team=b.overalls_by_team, window_by_team=b.window_by_team,
        capsheet_by_team=b.capsheet_by_team, pick_owner_by_pick={},
        contract_count_by_player={101: 1, 102: 2},
    )
    boundaries[0] = b2
    run = _run_record(1, [_season_record(2026, boundaries)])
    assert _compute_m4((run,), _BANDS).value == 1.0


def test_m5_sums_reconstruction_violations():
    boundaries = _full_boundaries((1,))
    s1 = _season_record(2026, boundaries, reconstruction_violations=("bad record",))
    run = _run_record(1, [s1])
    assert _compute_m5((run,), _BANDS).value == 1.0


# ---------------------------------------------------------------------------
# M6 -- determinism (requires a duplicate-seed pair in `runs`)
# ---------------------------------------------------------------------------


def test_m6_skips_when_no_duplicate_seed_present():
    boundaries = _full_boundaries((1,))
    run = _run_record(1, [_season_record(2026, boundaries, jsonl_lines=("a", "b"))])
    result = _compute_m6((run,), _BANDS)
    assert result.value is None
    assert result.skip_reason is not None


def test_m6_identical_duplicate_runs_have_zero_mismatches():
    boundaries = _full_boundaries((1,))
    run_a = _run_record(0, [_season_record(2026, boundaries, jsonl_lines=("a", "b"))])
    run_b = _run_record(0, [_season_record(2026, boundaries, jsonl_lines=("a", "b"))])
    result = _compute_m6((run_a, run_b), _BANDS)
    assert result.value == 0.0


def test_m6_catches_a_divergent_duplicate_run():
    boundaries = _full_boundaries((1,))
    run_a = _run_record(0, [_season_record(2026, boundaries, jsonl_lines=("a", "b"))])
    run_b = _run_record(0, [_season_record(2026, boundaries, jsonl_lines=("a", "DIFFERENT"))])
    result = _compute_m6((run_a, run_b), _BANDS)
    assert result.value == 1.0


# ---------------------------------------------------------------------------
# M7 -- capped-out bare roster share
# ---------------------------------------------------------------------------


def test_m7_flags_only_teams_that_are_both_capped_and_bare():
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        overalls_by_team={1: tuple([60] * 15), 2: tuple([90] * 15)},
        capsheet_by_team={1: _cap_row(1, room=-1_000_000), 2: _cap_row(2, room=-1_000_000)},
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m7((run,), _BANDS)
    # Team 1: room<0 AND top8 mean 60<70 -> flagged. Team 2: room<0 but
    # top8 mean 90 -> not flagged.
    assert result.value == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# M8a/M8b -- committed salary share
# ---------------------------------------------------------------------------


def test_m8a_mean_committed_share():
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        capsheet_by_team={
            1: _cap_row(1, committed_salary=80_000_000, room=20_000_000),
            2: _cap_row(2, committed_salary=100_000_000, room=0),
        },
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m8a((run,), _BANDS)
    # team1: 80M / (80M+20M) = 0.8; team2: 100M / (100M+0) = 1.0 -> mean 0.9
    assert result.value == pytest.approx(0.9)


def test_m8b_counts_teams_below_share_threshold():
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        capsheet_by_team={
            1: _cap_row(1, committed_salary=50_000_000, room=50_000_000),  # 0.5 -> below 0.80
            2: _cap_row(2, committed_salary=100_000_000, room=0),  # 1.0 -> not below
        },
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m8b((run,), _BANDS)
    # A single-cell run: mean_across_runs_seasons of one cell equals that
    # cell's own count, so this assertion is unchanged by the P2-35
    # aggregation fix -- it just no longer exercises the cross-cell path.
    assert result.value == 1.0


def _m8b_season_end_boundaries(n_teams: int, n_below: int) -> list[BoundaryRecord]:
    """`n_teams` teams at `season_end`, the first `n_below` below the 0.80
    share threshold (ratio 0.5), the rest comfortably above it (ratio
    0.9) -- gives exact control over one cell's `below_threshold` count."""
    team_ids = tuple(range(1, n_teams + 1))
    caps = {}
    for i, tid in enumerate(team_ids, start=1):
        if i <= n_below:
            caps[tid] = _cap_row(tid, committed_salary=50_000_000, room=50_000_000)  # 0.5
        else:
            caps[tid] = _cap_row(tid, committed_salary=90_000_000, room=10_000_000)  # 0.9
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary("season_end", capsheet_by_team=caps)
    return boundaries


def test_m8b_value_is_the_mean_of_known_per_cell_counts_not_the_max():
    """Decision P2-35 (architect round 17 Sec.2.3): `value` is the plain
    mean of the per-cell counts, not the old `max`. Fixture is chosen so
    mean != max -- a fixture where they coincide is vacuous and this exact
    defect class has been caught three times before in this project's
    history (critic rounds 43/47/48, per the architect memo)."""
    import statistics as _stats

    boundaries_by_season = [
        _m8b_season_end_boundaries(4, 1),  # cell = 1
        _m8b_season_end_boundaries(4, 2),  # cell = 2
        _m8b_season_end_boundaries(4, 4),  # cell = 4
    ]
    seasons = [_season_record(2026 + i, b) for i, b in enumerate(boundaries_by_season)]
    run = _run_record(1, seasons)
    result = _compute_m8b((run,), _BANDS)
    expected_mean = _stats.mean([1, 2, 4])
    assert result.value == expected_mean
    assert result.detail["max_over_cells"] == 4
    assert result.detail["max_over_cells"] != result.value


def test_m8b_superset_monotonicity_value_falls_when_a_below_mean_run_is_appended():
    """The regression guard for the actual bug (round 16 Sec.3 F4): under
    the old `max` aggregator, appending more evidence could only hold
    `value` steady or raise it -- a larger run's cells are a strict
    superset of a smaller one's (`validate_gm.py:233` derives each seed's
    cells from the base seed and index alone), so `max` is monotone
    non-decreasing in cell count by construction. This assertion is
    impossible to satisfy under `max` and must hold under `mean`."""
    run_high = _run_record(1, [_season_record(2026, _m8b_season_end_boundaries(4, 4))])
    value_before = _compute_m8b((run_high,), _BANDS).value
    assert value_before == 4.0

    # Appended run's cells (0) are all below the current mean (4.0).
    run_low = _run_record(2, [_season_record(2026, _m8b_season_end_boundaries(4, 0))])
    value_after = _compute_m8b((run_high, run_low), _BANDS).value
    assert value_after < value_before
    assert value_after == pytest.approx(2.0)


def test_m8b_per_season_matches_detail_per_season_rows_in_order():
    boundaries_by_season = [
        _m8b_season_end_boundaries(4, 1),
        _m8b_season_end_boundaries(4, 2),
        _m8b_season_end_boundaries(4, 4),
    ]
    seasons = [_season_record(2026 + i, b) for i, b in enumerate(boundaries_by_season)]
    run = _run_record(1, seasons)
    result = _compute_m8b((run,), _BANDS)
    assert len(result.per_season) == result.detail["n_cells"] == 3
    assert list(result.per_season) == [1.0, 2.0, 4.0]
    assert [row["below_threshold"] for row in result.detail["per_season"]] == [1, 2, 4]


def test_m8b_histogram_sums_to_n_cells_and_cells_above_band_high_is_strict():
    """`cells_above_band_high` counts strictly-greater-than `band_high`
    (6), not greater-or-equal -- mutation check: two cells sit exactly AT
    6 and one sits at 7, so flipping `>` to `>=` in the implementation
    changes the count from 1 to 3, which this fixture is built to
    exercise (a fixture with no cell exactly at `band_high` could not
    catch that mutation)."""
    boundaries_by_season = [
        _m8b_season_end_boundaries(7, 6),  # cell = 6, at the ceiling, not above it
        _m8b_season_end_boundaries(7, 6),  # cell = 6, at the ceiling, not above it
        _m8b_season_end_boundaries(7, 7),  # cell = 7, strictly above
    ]
    seasons = [_season_record(2026 + i, b) for i, b in enumerate(boundaries_by_season)]
    run = _run_record(1, seasons)
    result = _compute_m8b((run,), _BANDS)
    assert sum(result.detail["histogram"].values()) == result.detail["n_cells"] == 3
    assert result.detail["cells_above_band_high"] == 1
    assert result.detail["cells_above_band_high"] != 3  # what a `>=` mutation would report


def test_m8b_band_file_round_trip_against_the_real_seeds_file():
    """Reads `seeds/gm_validation_bands.json` via `load_bands`, not a
    synthetic fixture, so a future accidental edit to the real file (e.g.
    someone reverting `aggregation` back to `max_over_seed_season_pairs`,
    or moving `band_high`) is caught here rather than silently."""
    band = _BANDS.metrics["M8b"]
    assert band.aggregation == "mean_across_runs_seasons"
    assert band.band_high == 6


# ---------------------------------------------------------------------------
# M9 -- dead cap share (no longer vacuous, T7a2)
# ---------------------------------------------------------------------------


def test_m9_is_a_real_pooled_ratio_not_a_mean_of_per_team_ratios():
    """Discriminating test: team 1 has a small cap (dead cap 1/2 of its
    own total) but team 2 has a huge cap and zero dead cap. A (wrong)
    mean-of-ratios implementation gives (0.5 + 0.0) / 2 = 0.25; the real
    pooled-totals basis gives a much smaller number, since team 2's huge
    cap dominates the denominator."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("post_waivers")
    boundaries[idx] = _boundary(
        "post_waivers",
        capsheet_by_team={
            1: _cap_row(1, dead_cap=1_000_000, room=1_000_000),  # cap = 2M, dead/cap = 0.5
            2: _cap_row(2, dead_cap=0, room=198_000_000),  # cap = 198M, dead/cap = 0.0
        },
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m9((run,), _BANDS)
    pooled = 1_000_000 / (2_000_000 + 198_000_000)
    assert result.value == pytest.approx(pooled)
    # A mean-of-ratios bug would produce 0.25, not the real pooled figure.
    assert result.value < 0.25 - 1e-9


def test_m9_zero_dead_cap_is_a_clean_zero():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m9((run,), _BANDS)
    assert result.value == 0.0


# ---------------------------------------------------------------------------
# M10 -- room-buyers count, gated on a fixed steady-state season index
# (Decision P2-40, architect round 21 §3). All fixtures below are 5+
# seasons -- the exit-criterion floor `resolves_at_season` is pinned to --
# because the old single-season shape now renders SKIP under the new
# statistic (architect round 21 §3.7's own named trap).
# ---------------------------------------------------------------------------


#: Decision P2-46 (architect round 28, R28-M3's V2): the real, unscaled
#: seed anchors `compute_m10` reads off `bands.metrics["M10"].params` --
#: mirrored here as literals (not read dynamically) so a fixture computed
#: against them is legible on its own, the same style M1's roster_min/max
#: docstrings already use. `_M10_CAP` is deliberately set equal to
#: `_M10_CAP_BASE` for every fixture below that isn't specifically testing
#: cap growth, so `mle_this_season == _M10_MLE_BASE` with no scaling factor
#: to reason about.
_M10_MLE_BASE = 14_104_000
_M10_CAP_BASE = 154_647_000
_M10_ROSTER_MAX = 15
_M10_CAP = _M10_CAP_BASE
_M10_N_OPEN = 2  # roster_count = 13 for every "buyer/not" fixture below
_M10_ROSTER_COUNT = _M10_ROSTER_MAX - _M10_N_OPEN
_M10_THRESHOLD = _M10_N_OPEN * _M10_MLE_BASE  # $28,208,000 at no cap growth
_M10_BUYER_ROOM = 30_000_000  # > _M10_THRESHOLD -> buyer
_M10_NOT_BUYER_ROOM = 1_000_000  # < _M10_THRESHOLD -> not a buyer


def _m10_fa_boundary(
    n_buyers: int, *, team_ids: tuple[int, ...] = (1, 2), hold_n_buyers: int | None = None
) -> BoundaryRecord:
    """A `free_agency_open` boundary over `team_ids` where the first
    `n_buyers` teams (by position in `team_ids`) clear the Decision P2-46
    slot-relative threshold (`room > n_open * mle_this_season`) and the
    rest do not -- cap and `roster_count` (hence `n_open`) held fixed for
    every team so the buyer/not split moves only via `room`, mirroring
    this helper's pre-P2-46 shape (cap held fixed, `room` the only mover)
    under the new rule. When `hold_n_buyers` is given, `capsheet_with_
    holds_by_team` is populated the same way (so `holds_basis` stays
    `"computed"` for this boundary); left `None` (the default) reproduces
    the pre-round-19 absent-field shape."""

    def row(team_id: int, buyer: bool) -> SeasonCapProjection:
        room = _M10_BUYER_ROOM if buyer else _M10_NOT_BUYER_ROOM
        return _cap_row(
            team_id, committed_salary=_M10_CAP - room, room=room, roster_count=_M10_ROSTER_COUNT
        )

    caps = {tid: row(tid, i < n_buyers) for i, tid in enumerate(team_ids)}
    holds = None
    if hold_n_buyers is not None:
        holds = {tid: row(tid, i < hold_n_buyers) for i, tid in enumerate(team_ids)}
    return _boundary("free_agency_open", capsheet_by_team=caps, capsheet_with_holds_by_team=holds)


def _m10_season(
    season: int,
    n_buyers: int,
    *,
    team_ids: tuple[int, ...] = (1, 2),
    hold_n_buyers: int | None = None,
) -> SeasonRecord:
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("free_agency_open")
    boundaries[idx] = _m10_fa_boundary(n_buyers, team_ids=team_ids, hold_n_buyers=hold_n_buyers)
    return _season_record(season, boundaries)


def test_m10_counts_teams_with_room_over_slot_relative_threshold():
    """Rebuilt as a 5-season fixture (architect round 21 §3.7): under the
    new gated statistic a single-season fixture renders `SKIP`. Seasons
    1-4 are filler; only season 5's boundary is the one the gate reads.
    Decision P2-46 replaced the flat `room > 0.15 * cap` rule with the
    per-team slot-relative rule -- this fixture demonstrates that
    per-team-ness directly: all three teams share the same cap
    (`_M10_CAP`, no cap growth), so any difference in outcome comes only
    from each team's own `n_open`, not from a fraction of a shared
    denominator. Team 1 (`n_open=2`, threshold $28,208,000) and team 3
    (`n_open=1`, threshold $14,104,000) clear their own thresholds; team 2
    (`n_open=2`, same threshold as team 1) does not. `capsheet_with_holds_
    by_team` is set identical to `capsheet_by_team` throughout (fillers via
    `_m10_hold_neutral_filler_season`, season 5 explicitly) so `holds_
    basis` stays `"computed"` and the Decision P2-41 gate reads the same
    numbers the old hold-free gate did -- this test is about the counting
    rule, not the basis switch (see the dedicated basis tests below)."""
    team_ids = (1, 2, 3)
    filler = [_m10_hold_neutral_filler_season(2026 + i, team_ids) for i in range(4)]
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("free_agency_open")
    caps = {
        # cap = $154,647,000 (== salary_cap_base, no growth) for all three.
        # n_open=2, threshold $28.208M: team 1 buyer, team 2 not.
        # n_open=1, threshold $14.104M: team 3 buyer.
        1: _cap_row(1, committed_salary=124_647_000, room=30_000_000, roster_count=13),
        2: _cap_row(2, committed_salary=153_647_000, room=1_000_000, roster_count=13),
        3: _cap_row(3, committed_salary=134_647_000, room=20_000_000, roster_count=14),
    }
    boundaries[idx] = _boundary(
        "free_agency_open", capsheet_by_team=caps, capsheet_with_holds_by_team=dict(caps),
    )
    season5 = _season_record(2030, boundaries)
    run = _run_record(1, [*filler, season5])
    result = _compute_m10((run,), _BANDS)
    assert result.value == 2.0


def test_m10_gated_value_reads_only_the_resolves_at_season():
    """Mutation: revert to `statistics.mean(per_season)` -- must fail.
    `hold_n_buyers` mirrors `n_buyers` at every season so `holds_basis`
    stays `"computed"` and the Decision P2-41 gate has a real hold-
    inclusive number to read (this test targets the season-index
    aggregation, not the basis -- see the dedicated basis tests below)."""
    seasons = [_m10_season(2026 + i, 0, hold_n_buyers=0) for i in range(4)] + [
        _m10_season(2030, 2, hold_n_buyers=2)
    ]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value == 2.0
    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(0.4)
    assert result.value != result.detail["pooled_mean_across_runs_seasons"]


def test_m10_run_length_invariant_across_5_and_7_season_runs():
    """Two fixtures, identical in seasons 1-5, one padded with 2 more
    seasons whose count is far from season 5's. Mutation: gate on the
    final season (`run.seasons[-1]`) instead of the fixed index -- must
    fail on the 7-season fixture (it would then read the padded tail).
    `hold_n_buyers` mirrors `n_buyers` throughout so `holds_basis` stays
    `"computed"`."""
    padded_team_ids = tuple(range(1, 11))
    seasons_1_to_5 = [_m10_season(2026 + i, 0, hold_n_buyers=0) for i in range(4)] + [
        _m10_season(2030, 2, hold_n_buyers=2)
    ]
    seasons_7 = [
        *seasons_1_to_5,
        _m10_season(2031, 9, team_ids=padded_team_ids, hold_n_buyers=9),
        _m10_season(2032, 9, team_ids=padded_team_ids, hold_n_buyers=9),
    ]
    result_5 = _compute_m10((_run_record(1, seasons_1_to_5),), _BANDS)
    result_7 = _compute_m10((_run_record(1, seasons_7),), _BANDS)
    assert result_5.value == 2.0
    assert result_7.value == result_5.value == 2.0


def test_m10_seed_count_moves_the_mean_not_a_max():
    """Two seeds vs. three, the third seed's season-5 cell extreme.
    Mutation: `max(gated_cells)` instead of the mean -- must fail.
    `hold_n_buyers` mirrors `n_buyers` throughout so `holds_basis` stays
    `"computed"`."""
    common = [_m10_season(2026 + i, 0, hold_n_buyers=0) for i in range(4)] + [
        _m10_season(2030, 2, hold_n_buyers=2)
    ]
    run_a = _run_record(1, common)
    run_b = _run_record(2, common)
    extreme_season5 = _m10_season(2030, 9, team_ids=tuple(range(1, 11)), hold_n_buyers=9)
    run_c = _run_record(
        3,
        [_m10_season(2026 + i, 0, hold_n_buyers=0) for i in range(4)] + [extreme_season5],
    )

    result_2seed = _compute_m10((run_a, run_b), _BANDS)
    result_3seed = _compute_m10((run_a, run_b, run_c), _BANDS)
    assert result_2seed.value == 2.0
    expected_mean = statistics.mean([2.0, 2.0, 9.0])
    assert result_3seed.value == pytest.approx(expected_mean)
    assert result_3seed.value != max(2.0, 2.0, 9.0)


def test_m10_skips_when_fewer_than_5_seasons():
    """Mutation: fall back to the pooled mean when the run is short --
    must fail. This is the test that makes "passes at 4 seasons"
    structurally impossible (Decision P2-40's whole point)."""
    seasons = [_m10_season(2026 + i, 0) for i in range(4)]  # 4 seasons only
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.skip_reason == "no run has >= 5 seasons"

    finalized = _finalize_metric(
        _BANDS.metrics["M10"], result, gm_tuning_version=_BANDS.gm_tuning_version
    )
    assert finalized.status == "SKIP"
    assert finalized.passed is None
    assert finalized.counted_in_go is False


def test_m10_skip_path_still_carries_every_computed_season():
    """Critic round 54's nit 3: the short-run SKIP branch computed every
    cell (the per-cell loop is unconditional, per architect round 21
    §3.3 point 3) but didn't hand `MetricValue.per_season` back, so
    `validate_gm.py`'s trend rendering would silently go blank on a
    SKIP -- contradicting §3.3 point 8's own "still carries every cell,
    unchanged" claim. Mutation: drop the `per_season=` kwarg from the
    SKIP return -- must fail."""
    counts = [0, 1, 2, 1]  # 4 seasons, varying counts (team_ids default (1, 2))
    seasons = [_m10_season(2026 + i, n) for i, n in enumerate(counts)]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.per_season == tuple(float(n) for n in counts)
    assert result.detail["per_season"][0]["count"] == 0
    assert result.detail["holds_basis"] == "unavailable"


def test_m10_asserts_resolves_at_season_matches_exit_criterion_min_seasons():
    """A bands fixture with `resolves_at_season: 4` against the real
    `exit_criterion.min_seasons: 5`. Mutation: delete the assert -- must
    fail (silently reads season 4 instead of raising)."""
    band10 = _BANDS.metrics["M10"]
    assert _BANDS.exit_criterion["min_seasons"] == 5
    mismatched_band = dataclasses.replace(
        band10, params={**band10.params, "resolves_at_season": 4}
    )
    mismatched_bands = dataclasses.replace(
        _BANDS, metrics={**_BANDS.metrics, "M10": mismatched_band}
    )
    seasons = [_m10_season(2026 + i, 0) for i in range(4)] + [_m10_season(2030, 2)]
    run = _run_record(1, seasons)
    with pytest.raises(AssertionError) as excinfo:
        _compute_m10((run,), mismatched_bands)
    message = str(excinfo.value)
    assert "4" in message
    assert "5" in message


def test_m10_pooled_mean_across_runs_seasons_matches_the_pre_change_statistic():
    """Mutation: drop the `pooled_mean_across_runs_seasons` field -- must
    fail. Keeps M10's historical series (19.2 -> 16.8 -> ...) comparable.
    `hold_n_buyers` is left `None` (the default) throughout, so this
    fixture also exercises the Decision P2-41 hard-SKIP path (`holds_
    basis == "unavailable"`) -- `pooled_mean_across_runs_seasons` (a
    hold-free statistic, unaffected by the basis switch) must still be
    reported even when `value` itself is `None`."""
    counts = [0, 1, 2, 1, 2]
    seasons = [_m10_season(2026 + i, c) for i, c in enumerate(counts)]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.detail["holds_basis"] == "unavailable"
    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(
        statistics.mean(counts)
    )


def test_m10_pooled_mean_across_runs_seasons_stays_hold_free_not_hold_inclusive():
    """Decision P2-41 moved the GATE to the hold-inclusive basis but
    `pooled_mean_across_runs_seasons` keeps its OLD hold-free definition --
    the two bases are sized to disagree sharply (hold-free per-season
    counts 0/1/2/1/0, mean 0.8; hold-inclusive count held fixed at 2 every
    season, mean 2.0) so a mutation that points this field at the
    hold-inclusive series instead is caught on the value itself, not just
    on a coincidental match. Mutation: replace `statistics.mean(per_season)`
    with `statistics.mean(row["count"] for row in hold_inclusive_count_
    per_cell)` -- must fail (0.8 != 2.0)."""
    hold_free_counts = [0, 1, 2, 1, 0]
    seasons = [
        _m10_season(2026 + i, c, hold_n_buyers=2) for i, c in enumerate(hold_free_counts)
    ]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.detail["holds_basis"] == "computed"
    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(
        statistics.mean(hold_free_counts)
    )
    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(0.8)
    assert result.detail["pooled_mean_across_runs_seasons"] != pytest.approx(2.0)
    assert result.detail["hold_inclusive_mean"] == pytest.approx(2.0)


def test_m10_hold_inclusive_steady_state_mean_is_season_5_only_not_pooled():
    """`hold_inclusive_mean` stays pooled (unchanged on purpose);
    `hold_inclusive_steady_state_mean` is the season-5 hold-inclusive mean.
    Mutation: compute the steady-state figure from whatever cells happen
    to be present -- must fail."""
    hold_counts = [0, 1, 2, 1, 2]
    seasons = [
        _m10_season(2026 + i, 0, hold_n_buyers=c) for i, c in enumerate(hold_counts)
    ]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.detail["holds_basis"] == "computed"
    assert result.detail["hold_inclusive_mean"] == pytest.approx(statistics.mean(hold_counts))
    assert result.detail["hold_inclusive_steady_state_mean"] == pytest.approx(2.0)
    assert result.detail["hold_inclusive_mean"] != result.detail["hold_inclusive_steady_state_mean"]

    # Partial-presence trap: the field is absent at a FILLER boundary but
    # present (and non-trivial) at season 5 -- `holds_basis` still flips to
    # `"unavailable"` (any missing boundary poisons it), so the steady-state
    # figure must be `None` too, never computed from the one cell that
    # happens to have survived. Under Decision P2-41 the GATE itself now
    # depends on this basis, so `value` must hard-SKIP too, not just the
    # diagnostic fields (season 5 alone, hold-free, would otherwise compute
    # a real number here -- 0.0 -- which `value` must never silently fall
    # back to).
    partial_seasons = [_m10_season(2026 + i, 0, hold_n_buyers=None) for i in range(4)] + [
        _m10_season(2030, 0, hold_n_buyers=2)
    ]
    partial_result = _compute_m10((_run_record(1, partial_seasons),), _BANDS)
    assert partial_result.value is None
    assert partial_result.skip_reason is not None
    assert partial_result.detail["holds_basis"] == "unavailable"
    assert partial_result.detail["hold_inclusive_steady_state_mean"] is None
    assert partial_result.detail["hold_inclusive_gated_cells"] is None
    assert partial_result.detail["gated_cells"] is None


def test_m10_band_low_high_untouched_by_slice_22a_and_tuning_version_unbumped_by_it_alone():
    """A JSON-level assertion that M10's band did not move under Slice
    22-A's instrument correction. Mutation: any band edit -- must fail.
    Decision P2-41 (Slice 22-A) is harness-only: it adds `params.
    holds_basis` so the gate's basis is data, not an implicit code fact,
    but does not itself bump `gm_tuning_version` (no `managers/` constant
    moved by Slice 22-A alone). `gm_tuning_version` has since moved to 2
    under Slice 22-B (`decisions/signing.py`'s `offer_years` constants,
    Decision P2-42) -- a later, independent bump this test does not
    re-litigate; see `test_load_bands_covers_all_26_ids` for the live
    value."""
    band10 = _BANDS.metrics["M10"]
    assert band10.band_low == 3
    assert band10.band_high == 12
    assert band10.comparator == "range"
    assert band10.unit == "count"
    assert band10.params["holds_basis"] == "hold_inclusive"


def test_m10_band_unchanged_by_decision_p2_46_but_params_and_threshold_source_moved():
    """Decision P2-46 (architect round 28, R28-M3's V2) is a harness-only
    instrument correction, matching Decision P2-35/P2-40's own precedent:
    the scale is identical (still a 0-30 team count), so `band_low`/
    `band_high` do not move. Mutation: any band edit -- must fail. The old
    `room_share_threshold` param is removed (nothing reads it any longer --
    `_m10_is_room_buyer` replaced every read site) rather than left as dead
    config; mutation: leave it in the bands JSON -- must fail. The new
    `mle_amount_base`/`salary_cap_base` params are the real, unscaled seed
    anchors `compute_m10` cannot otherwise reach (contractually pure, no
    `seeds/cap_settings.json` import) -- mutation: wrong literal for either
    -- must fail. `roster_max` duplicates M1's own literal rather than
    reaching into M1's bands entry, so M10's params stay self-contained."""
    band10 = _BANDS.metrics["M10"]
    assert band10.band_low == 3
    assert band10.band_high == 12
    assert "room_share_threshold" not in band10.params
    assert band10.params["mle_amount_base"] == 14_104_000
    assert band10.params["salary_cap_base"] == 154_647_000
    assert band10.params["roster_max"] == 15


# ---------------------------------------------------------------------------
# M10's hold-inclusive basis. Originally a reported-only diagnostic
# (architect round 19 §7 item 5, Slice 19-A: gate stays on `capsheet_by_
# team`), the GATE itself switched to `capsheet_with_holds_by_team` under
# Decision P2-41 (architect round 22 §4.1, Slice 22-A) -- the tests below
# reflect the current, post-P2-41 contract.
# ---------------------------------------------------------------------------


def _m10_hold_neutral_filler_season(season: int, team_ids: tuple[int, ...]) -> SeasonRecord:
    """A filler season whose `free_agency_open` boundary carries a real
    (non-`None`) `capsheet_with_holds_by_team` with zero buyers on either
    basis -- needed so 4 padding seasons don't themselves flip `holds_
    basis` to `"unavailable"` in fixtures that need it `"computed"`."""
    return _m10_season(season, 0, team_ids=team_ids, hold_n_buyers=0)


def test_m10_gate_value_reads_the_hold_inclusive_basis():
    """Decision P2-41 (architect round 22 §4.1): the gate now reads
    `capsheet_with_holds_by_team`, not `capsheet_by_team` -- the opposite
    of architect round 19 §7 item 5's original "gate unchanged" ruling,
    superseded by this decision.

    Season 5's boundary keeps critic round 53's N1 fix intact: the two
    bases are sized to disagree on the COUNT itself (1 vs. 2), not just on
    which team clears the threshold, so a gate wrongly reading the
    hold-FREE basis is caught on `result.value` itself, not only on the
    diagnostic fields.

    Mutation-verified by hand this round, against this fixture: reverted
    `compute_m10`'s gated-cell loop to append the hold-free `count`
    instead of `hold_count` -- `result.value == 2.0` failed (got 1.0, the
    hold-free count), confirming the fixture still discriminates; reverted
    back to the real implementation, green again."""
    team_ids = (1, 2)
    filler = [_m10_hold_neutral_filler_season(2026 + i, team_ids) for i in range(4)]
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("free_agency_open")
    boundaries[idx] = _boundary(
        "free_agency_open",
        capsheet_by_team={
            # roster_count=13 (n_open=2) on every row: thresholds
            # ~$7.3M (cap=40M) and ~$2.0M (cap=11M), so the margins below
            # are unambiguous under Decision P2-46's slot-relative rule.
            # cap=40M -> buyer, cap=11M -> not.
            1: _cap_row(1, committed_salary=10_000_000, room=30_000_000, roster_count=13),
            2: _cap_row(2, committed_salary=10_000_000, room=1_000_000, roster_count=13),
        },
        # Hold-inclusive: both teams now clear the threshold -- count=2,
        # not 1, so a gate wrongly reading the hold-free basis is caught on
        # `result.value` itself, not only on the diagnostic fields.
        # cap=40M -> buyer, cap=11M -> buyer.
        capsheet_with_holds_by_team={
            1: _cap_row(1, committed_salary=5_000_000, room=35_000_000, roster_count=13),
            2: _cap_row(2, committed_salary=1_000_000, room=10_000_000, roster_count=13),
        },
    )
    season5 = _season_record(2030, boundaries)
    run = _run_record(1, [*filler, season5])
    result = _compute_m10((run,), _BANDS)
    assert result.value == 2.0
    assert result.detail["holds_basis"] == "computed"
    assert result.detail["hold_inclusive_steady_state_mean"] == 2.0
    # The expected, named-in-spec duplication: the new gated `value` and
    # the reported `hold_inclusive_steady_state_mean` are now numerically
    # identical -- not a bug to engineer around.
    assert result.value == result.detail["hold_inclusive_steady_state_mean"]


def test_m10_holds_basis_stamped_unavailable_when_the_field_is_absent():
    """`_full_boundaries` never sets `capsheet_with_holds_by_team` (the
    pre-round-19-pickle shape) -- every hold-inclusive `detail` field, old
    and new, must be `None`, and `holds_basis` must say so explicitly,
    never silently produce a number as if it were real (round 19 §5/§7's
    own instruction). Rebuilt as a 5-season fixture (architect round 21
    §3.7) since the old single-season shape now renders `SKIP`.
    Mutation-verified by hand against the rebuilt fixture (critic round
    54): temporarily removed the `holds_basis = "unavailable"`
    assignment (kept collecting `hold_inclusive_gated_cells` whenever
    the field happened to be present, silent otherwise) -- this test
    (and its M10-family neighbors reading `hold_inclusive_count_per_cell`)
    failed with a `statistics.StatisticsError` from the now-permanently-
    empty gated list; reverted, green again."""
    team_ids = (1, 2, 3)
    seasons = [_season_record(2026 + i, _full_boundaries(team_ids)) for i in range(5)]
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.skip_reason is not None
    assert result.detail["holds_basis"] == "unavailable"
    assert result.detail["hold_inclusive_mean"] is None
    assert result.detail["hold_inclusive_count_per_cell"] is None
    assert result.detail["hold_inclusive_steady_state_mean"] is None
    assert result.detail["hold_inclusive_gated_cells"] is None
    assert result.detail["gated_cells"] is None


def test_m10_hard_skips_on_unavailable_holds_basis_never_falls_back_to_hold_free():
    """The trap named explicitly by architect round 22 §4.1 (the same
    shape round 19 §5 / round 21 §3.3 point 8 already caught once): force
    `capsheet_with_holds_by_team = None` on exactly ONE boundary (season
    5, the one the gate reads) in a fixture that is otherwise fully
    computable on the hold-free basis -- if `compute_m10` silently fell
    back to `capsheet_by_team` here it would report a real-looking number
    (2.0, the hold-free count), not `None`. Mutation: on `holds_basis ==
    "unavailable"`, fall back to computing `value` from the hold-free
    `capsheet_by_team` cells instead of hard-SKIPping -- must fail (would
    read 2.0 instead of `None`)."""
    team_ids = (1, 2, 3)
    filler = [_m10_hold_neutral_filler_season(2026 + i, team_ids) for i in range(4)]
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("free_agency_open")
    boundaries[idx] = _boundary(
        "free_agency_open",
        capsheet_by_team={
            # roster_count=13 (n_open=2) on every row -- see the fixture
            # note in the hold-inclusive-basis test above for the threshold
            # arithmetic at these caps.
            1: _cap_row(1, committed_salary=10_000_000, room=30_000_000, roster_count=13),  # buyer
            2: _cap_row(2, committed_salary=10_000_000, room=1_000_000, roster_count=13),  # not
            3: _cap_row(3, committed_salary=10_000_000, room=25_000_000, roster_count=13),  # buyer
        },
        capsheet_with_holds_by_team=None,  # the one boundary missing holds
    )
    season5 = _season_record(2030, boundaries)
    run = _run_record(1, [*filler, season5])
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.value != 2.0
    assert result.skip_reason is not None
    assert result.detail["holds_basis"] == "unavailable"


# ---------------------------------------------------------------------------
# M10 -- Fix B1 (critic review of Decision P2-46's own build): historical
# joinability. Decision P2-46's own design-doc text requires this item's
# historical series stay joinable "exactly as [Decisions P2-35 and P2-40]
# required" -- the two named keys that promise attaches to are
# `pooled_mean_across_runs_seasons` and `hold_inclusive_mean` (the design
# doc's own implementation note for Decision P2-40 names these two keys as
# carrying "M10's historical series (19.2 -> 16.8 ..., and 12.2667)"). Both
# moved to the new slot-relative rule under P2-46, breaking that promise;
# `_m10_is_room_buyer_flat_threshold_v0` restores it via two old-rule twins.
# ---------------------------------------------------------------------------


def test_m10_flat_threshold_v0_fields_reproduce_the_pre_p2_46_rule():
    """All 5 seasons share one cap (`_M10_CAP_BASE`, no growth), so
    `0.15 * cap = $23,197,050` exactly. Each season's `room`/`roster_count`
    is chosen so the OLD (flat) and NEW (slot-relative) rules disagree in
    a different direction -- confirming the new fields track the OLD rule
    specifically, not merely happen to equal the new one's own output:

    | season | room | roster_count (n_open) | NEW verdict | OLD verdict |
    |---|---|---|---|---|
    | 1 | $20.0M | 14 (1) | buyer (20.0M > $14.104M) | not (20.0M < $23.197M) |
    | 2 | $25.0M | 13 (2) | not (25.0M < $28.208M) | buyer (25.0M > $23.197M) |
    | 3 |  $5.0M | 15 (0) | buyer (any room > $0) | not |
    | 4 |  $1.0M | 13 (2) | not | not |
    | 5 | $30.0M | 13 (2) | buyer | buyer |

    NEW per-season counts `[1, 0, 1, 0, 1]`, pooled mean 0.6 (the existing,
    already-covered field). OLD per-season counts `[0, 1, 0, 0, 1]`, pooled
    mean **0.4** -- the number this test pins, verified independently of
    `compute_m10` in the `old_rule_counts` line below before it is ever
    compared to the field under test. `capsheet_with_holds_by_team` is set
    identical to `capsheet_by_team` throughout, so the hold-inclusive twin
    reproduces the identical 0.4 via a wholly separate code path (the
    `capsheet_with_holds_by_team` loop, not the `capsheet_by_team` one).
    Mutation: compute either new field from `_m10_is_room_buyer` instead of
    `_m10_is_room_buyer_flat_threshold_v0` -- must fail (0.6 != 0.4)."""
    team_ids = (1,)
    cap = _M10_CAP_BASE
    configs = [
        (20_000_000, 14),
        (25_000_000, 13),
        (5_000_000, 15),
        (1_000_000, 13),
        (30_000_000, 13),
    ]
    seasons = []
    for i, (room, roster_count) in enumerate(configs):
        boundaries = _full_boundaries(team_ids)
        idx = _ALL_LABELS.index("free_agency_open")
        caps = {1: _cap_row(1, committed_salary=cap - room, room=room, roster_count=roster_count)}
        boundaries[idx] = _boundary(
            "free_agency_open", capsheet_by_team=caps, capsheet_with_holds_by_team=dict(caps),
        )
        seasons.append(_season_record(2026 + i, boundaries))
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)

    # Independent hand-check of the fixture itself, before trusting the
    # field under test against it.
    old_rule_counts = [1 if room > 0.15 * cap else 0 for room, _ in configs]
    assert old_rule_counts == [0, 1, 0, 0, 1]
    assert statistics.mean(old_rule_counts) == pytest.approx(0.4)

    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(0.6)
    assert result.detail["pooled_mean_across_runs_seasons_flat_threshold_v0"] == pytest.approx(0.4)
    assert result.detail["hold_inclusive_mean"] == pytest.approx(0.6)
    assert result.detail["hold_inclusive_mean_flat_threshold_v0"] == pytest.approx(0.4)
    # Neither new field feeds the gate -- `value` is untouched (unrelated
    # to this test's fixture, computed on season 5's own hold-inclusive
    # count under the NEW rule only, as every other M10 test verifies).
    assert result.value is not None


def test_m10_flat_threshold_v0_fields_absent_when_holds_basis_unavailable():
    """Mirrors `test_m10_pooled_mean_across_runs_seasons_matches_the_pre_
    change_statistic`'s own shape: `pooled_mean_across_runs_seasons_flat_
    threshold_v0` is hold-free and must survive even when the hold-
    inclusive gate hard-SKIPs; `hold_inclusive_mean_flat_threshold_v0`
    is hold-inclusive and must be `None` under the identical rule its
    non-flat sibling `hold_inclusive_mean` already follows."""
    counts = [0, 1, 2, 1, 2]
    seasons = [_m10_season(2026 + i, c) for i, c in enumerate(counts)]  # hold_n_buyers=None
    run = _run_record(1, seasons)
    result = _compute_m10((run,), _BANDS)
    assert result.value is None
    assert result.detail["holds_basis"] == "unavailable"
    assert result.detail["pooled_mean_across_runs_seasons_flat_threshold_v0"] is not None
    assert result.detail["hold_inclusive_mean_flat_threshold_v0"] is None


# ---------------------------------------------------------------------------
# M10 -- Fix B3 (critic review of Decision P2-46's own build): the critic
# proved untested that BOTH counting bases use the new rule -- reverting
# ONLY the hold-free loop in `compute_m10` (leaving the hold-inclusive loop
# on the new rule) passed all 76 pre-fix tests. This fixture is built so the
# two bases disagree in OPPOSITE directions, so each of the two single-loop
# reversions is caught by a different assertion below.
# ---------------------------------------------------------------------------


def test_m10_both_counting_bases_use_the_new_rule_not_just_one():
    """Season 5's two bases are deliberately sized to disagree with each
    other, not just with the flat rule:

    - `capsheet_by_team` (hold-free): room=$20.0M, roster_count=14
      (n_open=1, threshold $14.104M). NEW: buyer. OLD (`room > 0.15 *
      cap` = $23.197M): not.
    - `capsheet_with_holds_by_team` (hold-inclusive): room=$25.0M,
      roster_count=13 (n_open=2, threshold $28.208M). NEW: not. OLD:
      buyer.

    Under the real (correct) implementation: hold-free season-5 count is
    1, so `pooled_mean_across_runs_seasons` (filler seasons all 0) is
    `1/5 = 0.2`; hold-inclusive season-5 (gated) count is 0, so `value`
    is `0.0`.

    Mutation A -- revert ONLY the `capsheet_by_team` loop to the flat
    rule: season-5 hold-free count flips to 0 (20.0M < 23.197M),
    `pooled_mean_across_runs_seasons` flips to `0.0` -- caught by this
    test's first assertion, `value` unaffected. Mutation-verified by hand
    this round: reverted the `capsheet_by_team` loop's `_m10_is_room_
    buyer(...)` call to `row.room > 0.15 * cap`, this test's `pooled_
    mean_across_runs_seasons` assertion failed (got 0.0), `value`
    assertion still passed; reverted back, green again.

    Mutation B -- revert ONLY the `capsheet_with_holds_by_team` loop to
    the flat rule: season-5 hold-inclusive (gated) count flips to 1
    (25.0M > 23.197M), `value` flips to `1.0` -- caught by this test's
    second assertion, `pooled_mean_across_runs_seasons` unaffected.
    Mutation-verified by hand this round: reverted the `capsheet_with_
    holds_by_team` loop's call the same way, this test's `value`
    assertion failed (got 1.0), the pooled-mean assertion still passed;
    reverted back, green again. Neither mutation alone is caught by any
    test that existed before this fix (both bases were calibrated, in
    every prior fixture, so the flat and slot-relative rules agree)."""
    team_ids = (1,)
    filler = [_m10_hold_neutral_filler_season(2026 + i, team_ids) for i in range(4)]
    boundaries = _full_boundaries(team_ids)
    idx = _ALL_LABELS.index("free_agency_open")
    boundaries[idx] = _boundary(
        "free_agency_open",
        # NEW: buyer (20.0M > $14.104M threshold). OLD: not (20.0M <
        # $23.197M).
        capsheet_by_team={
            1: _cap_row(1, committed_salary=134_647_000, room=20_000_000, roster_count=14)
        },
        # NEW: not (25.0M < $28.208M threshold). OLD: buyer (25.0M >
        # $23.197M).
        capsheet_with_holds_by_team={
            1: _cap_row(1, committed_salary=129_647_000, room=25_000_000, roster_count=13)
        },
    )
    season5 = _season_record(2030, boundaries)
    run = _run_record(1, [*filler, season5])
    result = _compute_m10((run,), _BANDS)
    assert result.detail["pooled_mean_across_runs_seasons"] == pytest.approx(0.2)
    assert result.value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# M10 -- Decision P2-46 (architect round 28 §4/§6, R28-M3's V2): the
# per-team "room buyer" threshold itself, tested directly against
# `_m10_is_room_buyer` so each of the three named failure modes is isolated
# from the season/basis/skip plumbing already covered above. Each mutation
# below was actually injected into `_m10_is_room_buyer`, confirmed to flip
# the named assertion, and reverted.
# ---------------------------------------------------------------------------


def test_m10_is_room_buyer_scales_by_n_open_not_a_flat_share_of_cap():
    """Mutation: revert the body to `return row.room > 0.15 * cap` (the
    pre-P2-46 flat rule) -- must fail. At `cap == salary_cap_base` (no cap
    growth), `n_open=1` gives a slot-relative threshold of exactly
    `mle_amount_base` ($14,104,000); the flat `0.15 * cap` threshold at
    this same cap is $23,197,050. `room=$18,000,000` sits strictly between
    the two, so the two rules disagree on the verdict, not just the
    number: the slot-relative rule says BUYER, the flat rule says NOT.

    Mutation-verified by hand this round: reverted `_m10_is_room_buyer`'s
    body to `return row.room > 0.15 * cap` -- this test's assertion
    (`is True`) failed (`18_000_000 > 0.15 * 154_647_000` is `18M > 23.2M`
    = `False`); reverted back to the real implementation, green again."""
    row = _cap_row(
        1, committed_salary=_M10_CAP_BASE - 18_000_000, room=18_000_000, roster_count=14
    )
    cap = _salary_cap(row)
    assert cap == _M10_CAP_BASE
    assert (
        _m10_is_room_buyer(
            row, cap, mle_amount_base=_M10_MLE_BASE, salary_cap_base=_M10_CAP_BASE, roster_max=15
        )
        is True
    )
    # And the flat rule this replaces really would have said NOT -- the
    # number above the docstring quotes, computed independently of the
    # function under test, so the mutation's failure mode is legible.
    assert not (18_000_000 > 0.15 * _M10_CAP_BASE)


def test_m10_is_room_buyer_mle_scales_with_the_seasons_real_cap():
    """Mutation: drop the cap-growth scaling factor (`mle_this_season =
    mle_amount_base` instead of `mle_amount_base * (cap / salary_cap_
    base)`) -- must fail on a season where the cap has grown. At `n_open=1`
    and a cap 10% above `salary_cap_base` (a season after season 1 in any
    multi-season fixture with `tuning.cap_growth > 1`), the correct
    threshold is `$14,104,000 * 1.1 = $15,514,400`; the unscaled mutant's
    threshold stays at `$14,104,000`. `room=$14,800,000` sits strictly
    between the two.

    Mutation-verified by hand this round: reverted `_m10_is_room_buyer` to
    `mle_this_season = mle_amount_base` (dropping the `cap / salary_cap_
    base` factor) -- this test's assertion (`is False`) failed (the
    unscaled threshold of $14,104,000 is below $14,800,000, so the mutant
    reads BUYER); reverted back to the real implementation, green again."""
    grown_cap = round(_M10_CAP_BASE * 1.1)
    row = _cap_row(
        1, committed_salary=grown_cap - 14_800_000, room=14_800_000, roster_count=14
    )
    cap = _salary_cap(row)
    assert cap == grown_cap
    assert (
        _m10_is_room_buyer(
            row, cap, mle_amount_base=_M10_MLE_BASE, salary_cap_base=_M10_CAP_BASE, roster_max=15
        )
        is False
    )
    correct_threshold = 1 * (_M10_MLE_BASE * (grown_cap / _M10_CAP_BASE))
    unscaled_threshold = 1 * _M10_MLE_BASE
    assert unscaled_threshold < 14_800_000 < correct_threshold


def test_m10_is_room_buyer_clamps_n_open_at_zero_for_an_overfull_roster():
    """Mutation: drop the `max(0, ...)` clamp (`n_open = roster_max -
    row.roster_count`, unclamped) -- must fail for a team already over
    `roster_max` (`roster_count=17 > roster_max=15`). Room is NEGATIVE
    (`-$5,000,000`, an over-the-cap team) so the two rules' verdicts
    literally have opposite signs: clamped, `n_open=0` gives a threshold of
    `$0`, and `-5,000,000 > 0` is `False` (correctly not a buyer -- an
    over-cap team is not "hoarding" room it does not have). Unclamped,
    `n_open=-2` gives a NEGATIVE threshold (`-2 * mle_this_season`), and
    `-5,000,000` is greater than that larger-magnitude negative number, so
    the unclamped rule flips to `True` -- the exact sign-flip failure mode
    the clamp exists to prevent.

    Mutation-verified by hand this round: reverted `_m10_is_room_buyer` to
    `n_open = roster_max - row.roster_count` (no `max(0, ...)`) -- this
    test's assertion (`is False`) failed (the unclamped threshold went
    negative and `-5,000,000` cleared it); reverted back to the real
    implementation, green again."""
    row = _cap_row(1, committed_salary=160_000_000, room=-5_000_000, roster_count=17)
    cap = _salary_cap(row)
    assert cap == 155_000_000  # positive, so the `cap > 0` gate a caller applies still passes
    assert (
        _m10_is_room_buyer(
            row, cap, mle_amount_base=_M10_MLE_BASE, salary_cap_base=_M10_CAP_BASE, roster_max=15
        )
        is False
    )
    # The unclamped rule this guards against really would flip sign.
    unclamped_n_open = 15 - 17
    unclamped_threshold = unclamped_n_open * (_M10_MLE_BASE * (cap / _M10_CAP_BASE))
    assert -5_000_000 > unclamped_threshold


# ---------------------------------------------------------------------------
# M11-M13 -- Family 3, window-cohort statistics
# ---------------------------------------------------------------------------


def test_m11_pooled_ratio_and_undefined_when_denominator_empty():
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
    )
    acq_hi = Acquisition(team_id=1, player_id=1, overall=85, age=27, channel="trade",
                          window_at_acquisition=CompetitiveWindow.CONTEND)
    acq_lo = Acquisition(team_id=2, player_id=2, overall=82, age=24, channel="draft",
                          window_at_acquisition=CompetitiveWindow.REBUILD)
    season = _season_record(2026, boundaries, acquisitions=(acq_hi, acq_lo))
    run = _run_record(1, [season])
    result = _compute_m11((run,), _BANDS0)
    # rate_hi = 1/1 = 1.0, rate_lo = 1/1 = 1.0 -> ratio 1.0
    assert result.value == pytest.approx(1.0)

    # No REBUILD team-seasons at all -> undefined, never PASS(inf).
    boundaries2 = _full_boundaries((1,))
    idx2 = _ALL_LABELS.index("season_end")
    boundaries2[idx2] = _boundary("season_end", window_by_team={1: CompetitiveWindow.CONTEND})
    run2 = _run_record(2, [_season_record(2026, boundaries2, acquisitions=(acq_hi,))])
    result2 = _compute_m11((run2,), _BANDS0)
    assert result2.value is None
    assert result2.undefined is True


def test_m12_net_future_firsts_gap():
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        window_by_team={1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND},
    )
    season = _season_record(2026, boundaries, future_first_net_by_team={1: 3, 2: -1})
    run = _run_record(1, [season])
    result = _compute_m12((run,), _BANDS0)
    assert result.value == pytest.approx(3 - (-1))


def test_m13_skips_when_fewer_than_3_seasons():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m13((run,), _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m13_resolves_at_season_3_only():
    boundaries_s1 = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries_s1[idx] = _boundary(
        "season_end", window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}
    )
    s1 = _season_record(2026, boundaries_s1, minutes_weighted_age_by_team={1: 99.0, 2: 99.0})

    boundaries_s3 = _full_boundaries((1, 2))
    boundaries_s3[idx] = _boundary(
        "season_end", window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}
    )
    s3 = _season_record(2028, boundaries_s3, minutes_weighted_age_by_team={1: 30.0, 2: 25.0})
    run = _run_record(1, [s1, _season_record(2027, boundaries_s1), s3])
    result = _compute_m13((run,), _BANDS0)
    # Must read season 3's ages (30-25=5), not season 1's (99-99=0).
    assert result.value == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# M14 -- the paired talent-concentration delta, SE, and measurability gate
# ---------------------------------------------------------------------------


def test_m14_paired_delta_and_se_match_hand_computation():
    boundaries = _full_boundaries((1, 2, 3, 4))
    idx = _ALL_LABELS.index("post_waivers")
    # Two star_maxer teams (1, 2), two egalitarian teams (3, 4). Overalls
    # descending, 15 players each, so overalls[0]-overalls[7] is well
    # defined for all four.
    overalls_by_team = {
        1: tuple(range(99, 99 - 15, -1)),  # top1=99, 8th(index 7)=92 -> gap 7
        2: tuple(range(90, 90 - 15, -1)),  # gap 7
        3: tuple(range(80, 80 - 15, -1)),  # gap 7
        4: tuple(range(70, 70 - 15, -1)),  # gap 7
    }
    boundaries[idx] = _boundary("post_waivers", overalls_by_team=overalls_by_team)
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.STAR_MAXER,
                             3: GmPhilosophy.EGALITARIAN, 4: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: 2.0, 2: 4.0, 3: 6.0, 4: 8.0},
    )
    result = _compute_m14((run,), _BANDS0)
    # delta = end_gap(7) - baseline: sm=[7-2, 7-4]=[5,3] mean=4;
    # eg=[7-6, 7-8]=[1,-1] mean=0. value = 4 - 0 = 4.
    assert result.value == pytest.approx(4.0)
    import statistics as _stats

    expected_se = ( _stats.variance([5.0, 3.0]) / 2 + _stats.variance([1.0, -1.0]) / 2) ** 0.5
    assert result.detail["se_measured"] == pytest.approx(expected_se)
    assert result.detail["dropped_observations"] == 0


def test_m14_drops_teams_with_fewer_than_8_rostered_players():
    """Discriminating test: a team with only 5 players at the final
    boundary must be dropped (undefined gap), not silently indexed with a
    wrong/negative value."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("post_waivers")
    boundaries[idx] = _boundary(
        "post_waivers",
        overalls_by_team={1: (90, 85, 80, 75, 70), 2: tuple(range(90, 75, -1))},
    )
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: 1.0, 2: 1.0},
    )
    result = _compute_m14((run,), _BANDS0)
    assert result.detail["dropped_observations"] == 1
    # Fewer than 2 observations in the star_maxer cohort -> undefined.
    assert result.value is None
    assert result.extra_pass is False


def _m14_high_se_run() -> RunRecord:
    """High-variance fixture: point estimate clears +1.5, but the LIVE SE
    exceeds 0.75. Shared by every precertification-path test below --
    what differs between them is only which `BandSpec` it is measured
    against."""
    boundaries = _full_boundaries((1, 2, 3, 4))
    idx = _ALL_LABELS.index("post_waivers")
    overalls_by_team = {
        1: tuple(range(99, 84, -1)),  # gap 7
        2: tuple(range(40, 25, -1)),  # gap 7
        3: tuple(range(50, 35, -1)),  # gap 7
        4: tuple(range(50, 35, -1)),  # gap 7
    }
    boundaries[idx] = _boundary("post_waivers", overalls_by_team=overalls_by_team)
    return _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.STAR_MAXER,
                             3: GmPhilosophy.EGALITARIAN, 4: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: -50.0, 2: 50.0, 3: 7.0, 4: 7.0},
    )


def test_m14_high_live_se_still_passes_via_a_valid_precertification():
    """Reframed (was `test_m14_measurability_gate_fails_when_se_exceeds_
    threshold`, architect round 17 Sec.3.2): the premise that a high LIVE
    SE alone must fail the gate no longer universally holds -- when the
    precertification is valid, `extra_pass=True` via the precertification
    rather than the live check. The LIVE check still correctly identifies
    the high SE (`measurability_pass` stays False, still reported in
    `detail`), it just no longer solely carries the gate. The old "live SE
    alone gates" behaviour is still real for a bands file with no valid
    precertification -- see `test_m14_precert_absent_is_byte_identical_
    to_pre_p2_35_behaviour` below.

    **Slice 22-B note**: M14's real, checked-in `se_precertified` citation
    went briefly stale (`gm_tuning_version: 1` against a live `2`, Decision
    P2-42) and has since been re-precertified back to `2` (45-seed
    re-measurement under current code). This test still pins the version
    back to the precert's own version with a local `dataclasses.replace`
    regardless, so the valid-precert code path has a live test independent
    of whatever the real file happens to say at any given moment -- same
    reasoning as `test_m14_stale_precertification_falls_back_to_the_live_
    se_gate`'s own synthetic fixture for the opposite (stale) direction."""
    run = _m14_high_se_run()
    precert = _BANDS0.metrics["M14"].params["se_precertified"]
    matching_bands = dataclasses.replace(_BANDS0, gm_tuning_version=precert["gm_tuning_version"])
    result = _compute_m14((run,), matching_bands)
    assert result.detail["se_measured"] > 0.75
    assert result.detail["measurability_pass"] is False
    assert result.detail["measurability_source"] == "precertified"
    assert result.extra_pass is True

    finalized = _finalize_metric(
        matching_bands.metrics["M14"], result, gm_tuning_version=matching_bands.gm_tuning_version
    )
    assert finalized.passed is True
    assert "SE precertified" in finalized.status


def test_m14_stale_precertification_falls_back_to_the_live_se_gate():
    """The staleness regression guard -- the most important test in this
    half. This must match the real-world drift direction: `tuning.py`'s
    `GM_TUNING_VERSION` (and therefore the bands file's own top-level
    `gm_tuning_version`) moves AHEAD while an `se_precertified` citation
    recorded under an older version is left behind, stale.

    **Slice 22-B made this the REAL, live state for a while** (`_BANDS0.
    gm_tuning_version` moved 1 -> 2 under Decision P2-42 while M14's
    citation stayed at 1), which let this test drop its `dataclasses.
    replace` and assert straight off the live object. That obligation has
    since been discharged (M14 was re-precertified at 45 seeds under
    `gm_tuning_version=2` -- see `seeds/gm_validation_bands.json`'s
    `se_precertified.source`), so the live `_BANDS0` is valid again and no
    longer demonstrates staleness on its own. This test goes back to
    building a local, deliberately-stale fixture -- pinning the citation's
    `gm_tuning_version` one behind the bands' own, matching the real drift
    direction -- so the staleness path stays covered independent of
    whichever way the live file happens to sit at any given moment (same
    reasoning as `test_m14_high_live_se_still_passes_via_a_valid_
    precertification`'s own `dataclasses.replace`, just pinning the other
    direction).
    (An earlier version of this fixture had the mismatch backwards --
    precert ahead of bands -- which let a `==` -> `<=` mutation in
    `compute_m14`'s comparator survive undetected; see docs/ledger/
    phase-2.md for the mutation re-verification. The construction below
    keeps precert strictly behind bands for the same reason.)"""
    band14 = _BANDS0.metrics["M14"]
    live_precert = band14.params["se_precertified"]
    stale_precert = {**live_precert, "gm_tuning_version": _BANDS0.gm_tuning_version - 1}
    stale_band14 = dataclasses.replace(
        band14, params={**band14.params, "se_precertified": stale_precert}
    )
    stale_bands = dataclasses.replace(
        _BANDS0, metrics={**_BANDS0.metrics, "M14": stale_band14}
    )
    stale_precert_version = stale_band14.params["se_precertified"]["gm_tuning_version"]
    assert stale_precert_version == stale_bands.gm_tuning_version - 1

    run = _m14_high_se_run()
    result = _compute_m14((run,), stale_bands)
    assert result.detail["measurability_pass"] is False
    assert result.detail["precertification_stale"] is True
    assert result.detail["measurability_source"] == "this_run"
    assert result.extra_pass is False

    finalized = _finalize_metric(
        stale_band14, result, gm_tuning_version=stale_bands.gm_tuning_version
    )
    assert finalized.passed is False
    assert "PRECERT STALE" in finalized.status


def test_m14_precert_absent_is_byte_identical_to_pre_p2_35_behaviour():
    """Characterisation test: a bands fixture with no `se_precertified`
    key in M14's `params` must behave exactly as the pre-P2-35 function
    did -- `extra_pass` equals the live `measurability_pass` check, with
    none of the new detail keys populated, so the decoupling design
    cannot leak into a bands file that never asked for it."""
    band14 = _BANDS0.metrics["M14"]
    no_precert_params = {k: v for k, v in band14.params.items() if k != "se_precertified"}
    no_precert_band = dataclasses.replace(band14, params=no_precert_params)
    no_precert_bands = dataclasses.replace(
        _BANDS0, metrics={**_BANDS0.metrics, "M14": no_precert_band}
    )

    run = _m14_high_se_run()
    result = _compute_m14((run,), no_precert_bands)
    assert result.detail["measurability_pass"] is False
    assert result.extra_pass is result.detail["measurability_pass"] is False
    assert result.detail["measurability_source"] == "this_run"
    assert result.detail["se_precertified"] is None
    assert result.detail["precertification_stale"] is False
    assert result.detail["se_extrapolated_at_precert_seeds"] is None
    assert result.detail["precertification_consistent"] is None


def test_m14_cohort_too_small_returns_extra_pass_false_even_with_valid_precert():
    """The cohort-too-small early return (`len(deltas_a) < 2 or
    len(deltas_b) < 2`) must return `extra_pass=False` even when a valid
    precertification is present -- mutation check: if the precertification
    check were hoisted above this early return (made to short-circuit it),
    this fixture would flip to `extra_pass=True` and this test would fail.

    **Slice 22-B note**: the real, live `_BANDS0.gm_tuning_version` moved to
    2 (Decision P2-42) and M14's checked-in `se_precertified.gm_tuning_
    version` briefly lagged at 1 before being re-precertified back to 2.
    This test needs a genuinely valid precert to make its point regardless
    of which state the live file is in, so it builds a local bands fixture
    whose top-level version is pinned back to the precert's own version
    rather than relying on the live file happening to match."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("post_waivers")
    boundaries[idx] = _boundary(
        "post_waivers",
        overalls_by_team={1: (90, 85, 80, 75, 70), 2: tuple(range(90, 75, -1))},
    )
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: 1.0, 2: 1.0},
    )
    precert = _BANDS0.metrics["M14"].params.get("se_precertified")
    assert precert is not None
    matching_bands = dataclasses.replace(_BANDS0, gm_tuning_version=precert["gm_tuning_version"])
    assert precert["gm_tuning_version"] == matching_bands.gm_tuning_version
    result = _compute_m14((run,), matching_bands)
    assert result.value is None
    assert result.undefined is True
    assert result.extra_pass is False


def test_m14_se_extrapolated_at_precert_seeds_matches_hand_computation():
    """`se_extrapolated_at_precert_seeds = se * sqrt(n_runs / precert[
    "seeds"])` -- the 1/sqrt(n) law the band's own `se_remedy` already
    assumes, checked against an independently hand-computed SE."""
    import statistics as _stats

    boundaries = _full_boundaries((1, 2, 3, 4))
    idx = _ALL_LABELS.index("post_waivers")
    overalls_by_team = {
        1: tuple(range(99, 84, -1)),  # gap 7
        2: tuple(range(90, 75, -1)),  # gap 7
        3: tuple(range(80, 65, -1)),  # gap 7
        4: tuple(range(70, 55, -1)),  # gap 7
    }
    boundaries[idx] = _boundary("post_waivers", overalls_by_team=overalls_by_team)
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.STAR_MAXER,
                             3: GmPhilosophy.EGALITARIAN, 4: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: 2.0, 2: 4.0, 3: 6.0, 4: 8.0},
    )
    result = _compute_m14((run,), _BANDS0)
    # delta = end_gap(7) - baseline: sm=[7-2, 7-4]=[5,3]; eg=[7-6, 7-8]=[1,-1].
    expected_se = (_stats.variance([5.0, 3.0]) / 2 + _stats.variance([1.0, -1.0]) / 2) ** 0.5
    precert_seeds = _BANDS0.metrics["M14"].params["se_precertified"]["seeds"]
    expected_extrapolated = expected_se * math.sqrt(1 / precert_seeds)
    assert result.detail["se_extrapolated_at_precert_seeds"] == pytest.approx(expected_extrapolated)


def test_m14_required_seeds_uses_len_runs_not_min_seeds():
    """The only fixture shape that can catch the nit-fix (architect round
    17 Sec.3.2 point 6): `len(runs)` (1) must differ from `bands.
    exit_criterion.get("min_seeds", 3)` (3) for a reversion to the old
    `min_seeds`-based formula to be distinguishable at all. Mutation
    check: if the code reverted to using `min_seeds` instead of
    `len(runs)`, `required_seeds_if_over_threshold` would read
    `ceil(3 * (se/0.75)**2)` instead of `ceil(1 * (se/0.75)**2)` -- a
    different number given this fixture's single run."""
    run = _m14_high_se_run()
    assert len(_BANDS0.exit_criterion) > 0
    min_seeds = _BANDS0.exit_criterion.get("min_seeds", 3)
    assert len((run,)) != min_seeds  # 1 != 3 -- the fixture shape that matters

    result = _compute_m14((run,), _BANDS0)
    se = result.detail["se_measured"]
    se_threshold = _BANDS0.metrics["M14"].params["se_threshold"]
    expected_required = math.ceil(len((run,)) * (se / se_threshold) ** 2)
    wrong_required_via_min_seeds = math.ceil(min_seeds * (se / se_threshold) ** 2)
    assert result.detail["required_seeds_if_over_threshold"] == expected_required
    assert expected_required != wrong_required_via_min_seeds


# ---------------------------------------------------------------------------
# M15/M16 -- acquired-age gap, defense-group gap
# ---------------------------------------------------------------------------


def test_m15_acquired_player_age_gap():
    acq_vet = Acquisition(team_id=1, player_id=1, overall=80, age=34, channel="signing",
                           window_at_acquisition=CompetitiveWindow.RETOOL)
    acq_young = Acquisition(team_id=2, player_id=2, overall=80, age=21, channel="draft",
                             window_at_acquisition=CompetitiveWindow.RETOOL)
    boundaries = _full_boundaries((1, 2))
    season = _season_record(2026, boundaries, acquisitions=(acq_vet, acq_young))
    run = _run_record(
        1, [season],
        philosophy_by_team={1: GmPhilosophy.VET_OVERRATER, 2: GmPhilosophy.YOUTH_DEVELOPER},
    )
    result = _compute_m15((run,), _BANDS0)
    assert result.value == pytest.approx(34 - 21)


def test_m16_defense_first_cohort_exceeds_league_mean():
    """Decision P3-47 step 1 pinned M16's gated reading to `params.
    resolves_at_season` (5, matching the real bands entry) -- the def-group
    data has to sit on season 5, not season 1, or the pin would drop this
    single-season-shaped run entirely (`dropped_runs == 1`, `value is
    None`) rather than exercise the cohort/league comparison this test is
    actually about."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end", roster_ids_by_team={1: (101, 102), 2: (201, 202)}
    )
    earlier_seasons = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(4)]
    season5 = _season_record(
        2030, boundaries,
        def_group_mean_by_player={101: 90.0, 102: 90.0, 201: 50.0, 202: 50.0},
    )
    run = _run_record(
        1, [*earlier_seasons, season5],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    )
    result = _compute_m16((run,), _BANDS0)
    # cohort mean (team 1) = 90; league mean over both teams = (90+50)/2 = 70
    assert result.value == pytest.approx(90.0 - 70.0)
    assert result.detail["dropped_runs"] == 0


# ---------------------------------------------------------------------------
# M17 -- draft board divergence (the disclosed "no RunRecord" exception)
# ---------------------------------------------------------------------------


def test_m17_runs_a_small_number_of_real_drafts_and_returns_a_share_diff():
    import dataclasses

    band = _BANDS0.metrics["M17"]
    small_bands = dataclasses.replace(
        _BANDS0, metrics={**_BANDS0.metrics, "M17": dataclasses.replace(
            band, params={**band.params, "drafts": 5}
        )}
    )
    result = compute_m17(small_bands)
    assert result.value is not None
    assert -1.0 <= result.value <= 1.0
    assert result.detail["total_picks_star_maxer"] == 150  # 5 drafts x 30 picks
    assert result.detail["total_picks_egalitarian"] == 150
    assert 0.0 <= result.detail["rate_star_maxer"] <= 1.0
    assert 0.0 <= result.detail["rate_egalitarian"] <= 1.0
    # Discriminating regression guard: an earlier build of this fixture used
    # an empty-roster NeedMatrix, which made need_bonus a per-prospect
    # CONSTANT (see _m17_need_matrix's own docstring) -- collapsing
    # prospect_fit_value_for's ranking to bare prospect_value_for's for
    # every philosophy and forcing both rates to exactly 1.0. A real
    # reference roster with actual positional need variance must not
    # reproduce that.
    rate_sm = result.detail["rate_star_maxer"]
    rate_eg = result.detail["rate_egalitarian"]
    assert not (rate_sm == 1.0 and rate_eg == 1.0)


def test_m17_scores_each_cohort_against_its_own_board_not_a_shared_one(monkeypatch):
    """Discriminating regression test for the specific reversion a critic
    round injected into this slice's own build (Nit 1): `_m17_own_board_
    matches`'s scoring loop hardcoded to ONE fixed philosophy for both
    cohorts instead of each cohort's own bare board -- exactly the mistake
    report Sec.6.2 item 17 warns against ("against a league-default board
    the metric would also pick up the kappa spread"). The whole existing
    suite (including the test above) stayed green under that mutation,
    since it only checks the two rates aren't both exactly 1.0, not which
    philosophy's board produced each rate.

    Patches `metrics.py`'s OWN imported `prospect_value_for` binding, not
    `draft.py`'s module-level one -- `run_draft`/`prospect_fit_value_for`
    call the latter directly (a bare, same-module reference, confirmed by
    reading `prospect_fit_value_for`'s own body), so this spy observes
    ONLY `_m17_own_board_matches`'s own "score this real pick against the
    bare board" calls, not the draft itself."""
    import dataclasses

    band = _BANDS0.metrics["M17"]
    small_bands = dataclasses.replace(
        _BANDS0, metrics={**_BANDS0.metrics, "M17": dataclasses.replace(
            band, params={**band.params, "drafts": 1}
        )}
    )

    import tools.gm_harness.metrics as metrics_module

    seen: list[GmPhilosophy] = []
    real_pvf = metrics_module.prospect_value_for

    def _spy(*args, **kwargs):
        seen.append(kwargs["philosophy"])
        return real_pvf(*args, **kwargs)

    monkeypatch.setattr(metrics_module, "prospect_value_for", _spy)
    metrics_module.compute_m17(small_bands)

    distinct = set(seen)
    assert distinct == {GmPhilosophy.STAR_MAXER, GmPhilosophy.EGALITARIAN}, (
        f"scoring loop must use each cohort's own philosophy, saw {distinct} "
        "(a single value here means both cohorts were scored against one shared board)"
    )
    # compute_m17 processes star_maxer's own-board scoring fully before
    # egalitarian's (one draft, sequential m_a-then-m_b calls) -- a real,
    # non-shared-board implementation must show a clean two-block split,
    # never an interleave or a mismatched block.
    star_maxer_count = sum(1 for p in seen if p == GmPhilosophy.STAR_MAXER)
    assert seen[:star_maxer_count] == [GmPhilosophy.STAR_MAXER] * star_maxer_count
    assert seen[star_maxer_count:] == [GmPhilosophy.EGALITARIAN] * (
        len(seen) - star_maxer_count
    )


# ---------------------------------------------------------------------------
# M18-M20 -- Family 4, market liveness
# ---------------------------------------------------------------------------


class _FakeExecution:
    def __init__(self, team_a_id, team_b_id, players_a_to_b=(), players_b_to_a=()):
        self.team_a_id = team_a_id
        self.team_b_id = team_b_id
        self.players_a_to_b = players_a_to_b
        self.players_b_to_a = players_b_to_a


def test_m18_forces_extra_pass_false_on_any_zero_season():
    """Discriminating test: the RUN-WIDE MEAN sits comfortably in [12, 55],
    but one season has zero trades -- `extra_pass` must be False despite
    the in-band mean (the floor-is-the-important-half rule)."""
    boundaries = _full_boundaries((1,))
    s1 = _season_record(2026, boundaries, trades_by_window=((_FakeExecution(1, 2),) * 30, ()))
    s2 = _season_record(2027, boundaries, trades_by_window=((), ()))
    run = _run_record(1, [s1, s2])
    result = _compute_m18((run,), _BANDS0)
    assert 12 <= result.value <= 55
    assert result.extra_pass is False
    assert result.detail["any_season_zero"] is True


def test_m19_mutual_accept_share():
    boundaries = _full_boundaries((1,))
    season = _season_record(2026, boundaries, mutual_accepts=(3, 2), packages_evaluated=(50, 50))
    run = _run_record(1, [season])
    result = _compute_m19((run,), _BANDS0)
    assert result.value == pytest.approx(5 / 100)


def test_m20_max_team_trade_share():
    boundaries = _full_boundaries((1,))
    execs = (_FakeExecution(1, 2), _FakeExecution(1, 3), _FakeExecution(1, 4), _FakeExecution(2, 3))
    season = _season_record(2026, boundaries, trades_by_window=(execs, ()))
    run = _run_record(1, [season])
    result = _compute_m20((run,), _BANDS0)
    # total trades=4, team 1 participates in 3 of them -> share 0.75
    assert result.value == pytest.approx(0.75)


def test_m21_catches_a_retrade_in_the_immediately_following_window():
    boundaries = _full_boundaries((1,))
    window1 = (_FakeExecution(1, 2, players_a_to_b=(555,)),)
    window2 = (_FakeExecution(2, 1, players_a_to_b=(555,)),)  # traded straight back
    season = _season_record(2026, boundaries, trades_by_window=(window1, window2))
    run = _run_record(1, [season])
    result = _compute_m21((run,), _BANDS0)
    assert result.value == 1.0


def test_m21_no_false_positive_on_an_unrelated_second_trade():
    boundaries = _full_boundaries((1,))
    window1 = (_FakeExecution(1, 2, players_a_to_b=(555,)),)
    window2 = (_FakeExecution(2, 3, players_a_to_b=(999,)),)
    season = _season_record(2026, boundaries, trades_by_window=(window1, window2))
    run = _run_record(1, [season])
    result = _compute_m21((run,), _BANDS0)
    assert result.value == 0.0


def test_m22_skips_under_skip_games():
    boundaries = _full_boundaries((1,))
    run = _run_record(1, [_season_record(2026, boundaries, win_totals=None)])
    result = _compute_m22((run,), _BANDS0)
    assert result.value is None
    assert result.skip_reason == "--skip-games"


def test_m22_computes_mean_of_per_season_sd():
    boundaries = _full_boundaries((1,))
    win_totals = {i: v for i, v in enumerate([10, 20, 30, 40], start=1)}
    run = _run_record(1, [_season_record(2026, boundaries, win_totals=win_totals)])
    result = _compute_m22((run,), _BANDS0)
    import statistics as _stats

    assert result.value == pytest.approx(_stats.pstdev(win_totals.values()))


# ---------------------------------------------------------------------------
# A1 -- one archetype golden-table check (A2/A3 share the identical shape)
# ---------------------------------------------------------------------------


def test_a1_value_archetype_table_matches_the_checked_in_golden_file():
    """Confirms the wiring: metrics.py's own recomputation must currently
    agree with the checked-in golden file (nothing about this slice
    changes valuation.py's own numbers)."""
    result = _compute_a1(_BANDS0)
    assert result.value == 0.0


def test_count_table_diffs_discriminates_a_real_mismatch():
    from tools.gm_harness.metrics import _count_table_diffs

    golden = {"row1": {"col1": 1.0, "col2": 2.0}}
    actual_same = {"row1": {"col1": 1.0, "col2": 2.0}}
    actual_diff = {"row1": {"col1": 1.0, "col2": 999.0}}
    assert _count_table_diffs(actual_same, golden) == 0
    assert _count_table_diffs(actual_diff, golden) == 1


# ---------------------------------------------------------------------------
# compute_all -- end to end over hand-built fixtures
# ---------------------------------------------------------------------------


def test_compute_all_returns_one_result_per_band_entry():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.EGALITARIAN},
    )
    results = compute_all((run,), _BANDS0, include_metric_17=False)
    assert len(results) == 41
    ids = {r.id for r in results}
    assert ids == set(_BANDS0.metrics)
    m17 = next(r for r in results if r.id == "M17")
    assert m17.status == "SKIP"
    m21 = next(r for r in results if r.id == "M21")
    assert m21.counted_in_go is False
    m2 = next(r for r in results if r.id == "M2")
    assert m2.status == "PASS"


def test_compute_all_only_m6_ever_sees_determinism_runs():
    """B1 regression, fixture corrected to match `validate_gm.py`'s REAL
    call shape (T7b critic round-2 Nit 6). `validate_gm.py` derives
    `det_seed` via the exact same `rng_for(..., "seed", 0)` call `runs[0]`'s
    own seed uses -- so `det_run_a`/`det_run_b` always share `runs[0]`'s
    own `rng_seed` in production. The original version of this fixture used
    a distinguishable rng_seed (99) that the real caller never produces,
    which is exactly why this suite was blind to B-NEW-1 (`compute_m6`
    grouping `real_run`/`det_run_a`/`det_run_b` into ONE three-member group
    by shared rng_seed, instead of `determinism_runs`'s own clean two-member
    pair, and silently comparing `real_run` against `det_run_a` -- never
    `det_run_a` against `det_run_b` at all).

    `compute_all`'s `determinism_runs` parameter must still reach ONLY M6's
    own dispatch, exactly paired against itself: `det_run_a` is built to
    coincidentally MATCH `real_run`'s own jsonl (so the B-NEW-1 mispairing
    would report a false 0-mismatch PASS), while `det_run_a`/`det_run_b`
    genuinely diverge from each other (the real signal M6 exists to catch).
    Also confirms the pre-existing no-leak property into M22/M8a/M18 still
    holds now that the determinism pair shares `real_run`'s own rng_seed."""
    boundaries = _full_boundaries((1, 2))
    shared_seed = 777  # real_run's own rng_seed -- det_run_a/b share it too.
    real_season = _season_record(
        2026, boundaries, win_totals={1: 41, 2: 41}, trades_by_window=((), ()),
        jsonl_lines=("a", "b"),
    )
    real_run = _run_record(shared_seed, [real_season])

    det_boundaries = _full_boundaries((1, 2))
    det_season_a = _season_record(2026, det_boundaries, win_totals=None, jsonl_lines=("a", "b"))
    det_season_b = _season_record(
        2026, det_boundaries, win_totals=None, jsonl_lines=("a", "DIFFERENT")
    )
    det_run_a = _run_record(shared_seed, [det_season_a])
    det_run_b = _run_record(shared_seed, [det_season_b])

    results = compute_all(
        (real_run,), _BANDS0, determinism_runs=(det_run_a, det_run_b), include_metric_17=False
    )

    m6 = next(r for r in results if r.id == "M6")
    assert m6.status != "SKIP", "M6 must see the determinism pair and produce a real result"
    assert m6.detail["duplicate_seed_pairs_checked"] == 1
    assert m6.value == 1.0, (
        "M6 must diff det_run_a against det_run_b (which genuinely diverge), never "
        "real_run against det_run_a (which coincidentally match) -- B-NEW-1's exact "
        "bug reports 0.0 (a false PASS) here instead"
    )

    m22 = next(r for r in results if r.id == "M22")
    assert m22.status != "SKIP", (
        "M22 must be computed from the real run's own win_totals -- a leaked "
        "win_totals=None determinism run would force it to SKIP"
    )

    m8a = next(r for r in results if r.id == "M8a")
    assert m8a.detail["n"] == 2, (
        "M8a's pooled sample must be exactly the real run's own 2 team-seasons -- "
        "a leaked determinism pair would triple it to 6"
    )

    m18 = next(r for r in results if r.id == "M18")
    assert len(m18.detail["per_season"]) == 1, (
        "M18 must see exactly the real run's own 1 season, not 3 -- a leaked "
        "determinism pair would triple this list"
    )


def test_compute_all_m2_known_gap_renders_in_status_when_violations_exist():
    """B3 regression: a `known_gap` metric with real, nonzero `known_gap_
    violations` must not render a bare `PASS` -- it must disclose the
    count and boundaries in the status string, per the same mechanism
    already used for `PASS (vacuous)`/`PASS (interpreted)`."""
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end", capsheet_by_team={1: _cap_row(1, apron2_room=-5), 2: _cap_row(2)}
    )
    run = _run_record(1, [_season_record(2026, boundaries)])
    results = compute_all((run,), _BANDS0, include_metric_17=False)
    m2 = next(r for r in results if r.id == "M2")
    assert m2.passed is True
    assert m2.counted_in_go is True
    assert m2.status == "PASS (known gap: 1 violation at season_end, post_waivers)"


# ---------------------------------------------------------------------------
# RS3 (architect round 29 Sec.6, Decision P2-47) -- the reported-not-gated
# `detail["precision"]` block for M11/M12/M13/M15/M16, plus M17's own
# structurally different jackknife-over-classes treatment.
#
# Every fixture below is a small, hand-built multi-run tuple whose per-run
# raw values are DELIBERATELY UNEQUAL (the exact defect class this phase's
# own ledger has caught three times: a fixture where every replicate is
# identical makes any jackknife-SD test vacuous -- see round 29 Sec.6.5
# test 3's own instruction, and HANDOFF.md's T6b/T7a "vacuous test" rows).
# ---------------------------------------------------------------------------


def _m11_multi_run_fixture(hi_counts: list[int], lo_counts: list[int]) -> tuple[RunRecord, ...]:
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
    )
    runs = []
    for i, (hi, lo) in enumerate(zip(hi_counts, lo_counts, strict=True)):
        acqs = [
            Acquisition(team_id=1, player_id=1000 + i * 100 + j, overall=85, age=27,
                        channel="trade", window_at_acquisition=CompetitiveWindow.CONTEND)
            for j in range(hi)
        ] + [
            Acquisition(team_id=2, player_id=2000 + i * 100 + j, overall=82, age=24,
                        channel="draft", window_at_acquisition=CompetitiveWindow.REBUILD)
            for j in range(lo)
        ]
        season = _season_record(2026, boundaries, acquisitions=tuple(acqs))
        runs.append(_run_record(i, [season]))
    return tuple(runs)


def _m12_multi_run_fixture(
    rebuild_values: list[int], contend_values: list[int]
) -> tuple[RunRecord, ...]:
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        window_by_team={1: CompetitiveWindow.REBUILD, 2: CompetitiveWindow.CONTEND},
    )
    runs = []
    for i, (r, c) in enumerate(zip(rebuild_values, contend_values, strict=True)):
        season = _season_record(2026, boundaries, future_first_net_by_team={1: r, 2: c})
        runs.append(_run_record(i, [season]))
    return tuple(runs)


def _m13_multi_run_fixture(
    contend_ages: list[float], rebuild_ages: list[float]
) -> tuple[RunRecord, ...]:
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end",
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
    )
    runs = []
    for i, (ca, ra) in enumerate(zip(contend_ages, rebuild_ages, strict=True)):
        s1 = _season_record(2026, boundaries)
        s2 = _season_record(2027, boundaries)
        s3 = _season_record(2028, boundaries, minutes_weighted_age_by_team={1: ca, 2: ra})
        runs.append(_run_record(i, [s1, s2, s3]))
    return tuple(runs)


def _m15_multi_run_fixture(vet_ages: list[int], young_ages: list[int]) -> tuple[RunRecord, ...]:
    boundaries = _full_boundaries((1, 2))
    runs = []
    for i, (v_age, y_age) in enumerate(zip(vet_ages, young_ages, strict=True)):
        acq_v = Acquisition(team_id=1, player_id=1 + i, overall=80, age=v_age, channel="signing",
                             window_at_acquisition=CompetitiveWindow.RETOOL)
        acq_y = Acquisition(team_id=2, player_id=100 + i, overall=80, age=y_age, channel="draft",
                             window_at_acquisition=CompetitiveWindow.RETOOL)
        season = _season_record(2026, boundaries, acquisitions=(acq_v, acq_y))
        runs.append(_run_record(
            i, [season],
            philosophy_by_team={1: GmPhilosophy.VET_OVERRATER, 2: GmPhilosophy.YOUTH_DEVELOPER},
        ))
    return tuple(runs)


def _m16_multi_run_fixture(
    cohort_means: list[float], league_other_means: list[float]
) -> tuple[RunRecord, ...]:
    """Decision P3-47 step 1 pins M16's gated reading to `params.
    resolves_at_season` (5) -- every run here needs 5 seasons, with the
    def-group data on the 5th (not the 1st, which the pre-P3-47 single-
    season shape of this fixture used), or the pin drops every run
    (`dropped_runs == len(runs)`, `value is None`) rather than exercising
    the cohort/league comparison these fixtures are built for."""
    runs = []
    for i, (cm, om) in enumerate(zip(cohort_means, league_other_means, strict=True)):
        earlier_seasons = [_season_record(2026 + i * 10 + s, _full_boundaries((1, 2)))
                            for s in range(4)]
        boundaries = _full_boundaries((1, 2))
        idx = _ALL_LABELS.index("season_end")
        boundaries[idx] = _boundary(
            "season_end",
            roster_ids_by_team={1: (100 + i * 10 + 1, 100 + i * 10 + 2),
                                 2: (200 + i * 10 + 1, 200 + i * 10 + 2)},
        )
        season5 = _season_record(
            2026 + i * 10 + 4, boundaries,
            def_group_mean_by_player={
                100 + i * 10 + 1: cm, 100 + i * 10 + 2: cm,
                200 + i * 10 + 1: om, 200 + i * 10 + 2: om,
            },
        )
        runs.append(_run_record(
            i, [*earlier_seasons, season5],
            philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
        ))
    return tuple(runs)


_PRECISION_METRIC_FIXTURES = {
    "M11": (_compute_m11, _m11_raw_value,
            lambda: _m11_multi_run_fixture([2, 3, 4], [1, 1, 1])),
    "M12": (_compute_m12, _m12_raw_value,
            lambda: _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])),
    "M13": (_compute_m13, _m13_raw_value,
            lambda: _m13_multi_run_fixture([30, 32, 34], [25, 24, 23])),
    "M15": (_compute_m15, _m15_raw_value,
            lambda: _m15_multi_run_fixture([34, 36, 38], [21, 20, 19])),
    "M16": (_compute_m16, _m16_raw_value,
            lambda: _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])),
}


# --- Sec.6.5 test 1: characterisation / absence guard ---


def test_precision_absent_from_params_leaves_detail_byte_identical():
    """Mutation check: if `_precision_detail` returned `{}` instead of
    `None` when `params.precision` is absent, `_attach_precision` would no
    longer short-circuit and a "precision" key would leak into a bands
    fixture that never asked for it -- this test would fail on the key
    set."""
    for metric_id, (compute_fn, _raw_fn, build_runs) in _PRECISION_METRIC_FIXTURES.items():
        band = _BANDS0.metrics[metric_id]
        no_precision_params = {k: v for k, v in band.params.items() if k != "precision"}
        no_precision_band = dataclasses.replace(band, params=no_precision_params)
        no_precision_bands = dataclasses.replace(
            _BANDS0, metrics={**_BANDS0.metrics, metric_id: no_precision_band}
        )
        runs = build_runs()
        with_precision = compute_fn(runs, _BANDS0)
        without_precision = compute_fn(runs, no_precision_bands)
        assert without_precision.value == with_precision.value, metric_id
        assert "precision" not in without_precision.detail, metric_id
        expected_keys = set(with_precision.detail) - {"precision"}
        assert set(without_precision.detail) == expected_keys, metric_id


# --- Sec.6.5 test 2: the refactor is inert (B2 fix) ---

# Hardcoded, independently pre-refactor-verified raw values for each of
# the five `_PRECISION_METRIC_FIXTURES` fixtures above. The prior version
# of this test asserted `raw_fn(runs, band) == compute_fn(runs, _BANDS0).
# value` -- a comparison to itself by construction (`_mN_raw_value` IS
# `_mN_core(...).value`, and `compute_mN` IS `_attach_precision(_mN_core(
# ...), ...)`, whose `.value` is untouched), so it could never fail
# regardless of what lived inside `_mN_core`. These numbers were verified
# by running the PRE-REFACTOR `compute_mN` (checked out from the commit
# immediately before this build, `git show HEAD:tools/gm_harness/
# metrics.py` at build time) on these exact fixtures and confirming an
# exact match -- see the fix round's own report for the verification
# script and output. A future accidental behavior change inside the
# extracted `_mN_core` logic now has something real to fail against.
_PRECISION_METRIC_PRE_REFACTOR_VALUES = {
    "M11": 3.0,
    "M12": 2.0,
    "M13": 8.0,
    "M15": 16.0,
    "M16": 18.5,
}


def test_mN_raw_value_matches_hardcoded_pre_refactor_verified_value():
    for metric_id, (compute_fn, raw_fn, build_runs) in _PRECISION_METRIC_FIXTURES.items():
        runs = build_runs()
        band = _BANDS0.metrics[metric_id]
        expected = _PRECISION_METRIC_PRE_REFACTOR_VALUES[metric_id]
        assert raw_fn(runs, band) == pytest.approx(expected), metric_id
        assert compute_fn(runs, _BANDS0).value == pytest.approx(expected), metric_id


# --- Sec.6.5 test 3: the jackknife is arithmetically right ---


def test_jackknife_sd_matches_hand_computation_on_a_4_run_fixture_with_unequal_values():
    """A hand-built 4-run M12 fixture whose per-seed values are known by
    construction and deliberately unequal -- a fixture where every seed
    gave the same value would be vacuous (this phase's own already-caught
    defect class, T6b/T7a's HANDOFF rows)."""
    runs = _m12_multi_run_fixture([0, 3, 6, 9], [0, 1, 2, 3])
    band = _BANDS0.metrics["M12"]

    # Hand computation, independent of `_jackknife_sd_over_seeds`'s own
    # loop structure (mirrors, not calls, the production formula).
    n = 4
    replicates = []
    for i in range(n):
        excl_rebuild = [v for j, v in enumerate([0, 3, 6, 9]) if j != i]
        excl_contend = [v for j, v in enumerate([0, 1, 2, 3]) if j != i]
        replicates.append(statistics.mean(excl_rebuild) - statistics.mean(excl_contend))
    theta_bar = sum(replicates) / n
    ss = sum((t - theta_bar) ** 2 for t in replicates)
    expected_sd = math.sqrt((n - 1) / n * ss)

    actual_sd = _jackknife_sd_over_seeds(_m12_raw_value, runs, band)
    assert actual_sd == pytest.approx(expected_sd)
    # Discriminating regression guard: with every replicate equal, the SD
    # would be exactly 0.0 regardless of a real bug in the formula above --
    # confirm this fixture's replicates are NOT all equal.
    assert len(set(round(r, 6) for r in replicates)) > 1


def test_jackknife_sd_over_seeds_returns_none_below_3_runs():
    runs = _m12_multi_run_fixture([0, 3], [0, 1])
    band = _BANDS0.metrics["M12"]
    assert _jackknife_sd_over_seeds(_m12_raw_value, runs, band) is None


# --- Sec.6.5 test 4: `extra_pass` is untouched -- THE most important test ---


def test_precision_never_touches_extra_pass_even_when_measurability_pass_is_false():
    """Sec.6.5 test 4: the mechanical guarantee that a precision block
    reporting a far-over-threshold SE cannot flip a verdict. Mutation
    check: if `extra_pass=measurability_pass` were wired into any one of
    the five `compute_mN` wrappers, this test would fail immediately (the
    forced-tiny threshold below makes `measurability_pass` False for
    every one of the five)."""
    for metric_id, (compute_fn, _raw_fn, build_runs) in _PRECISION_METRIC_FIXTURES.items():
        runs = build_runs()
        band = _BANDS0.metrics[metric_id]
        tiny_precision = {**band.params["precision"], "se_threshold_derived": 1e-9}
        tiny_band = dataclasses.replace(band, params={**band.params, "precision": tiny_precision})
        tiny_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, metric_id: tiny_band})

        result = compute_fn(runs, tiny_bands)
        precision = result.detail.get("precision")
        assert precision is not None, metric_id
        assert precision["measurability_pass"] is False, metric_id
        assert result.extra_pass is True, metric_id

        finalized = _finalize_metric(
            tiny_band, result, gm_tuning_version=tiny_bands.gm_tuning_version
        )
        assert finalized.passed == evaluate(tiny_band, result.value), metric_id


# --- Sec.6.5 test 5: precertified vs. live SD selection ---


def test_precision_precertified_vs_live_sd_selection():
    """Mutation check: change the staleness comparator's `!=` to `==` in
    `_precision_detail` and this test's stale-branch assertions fail.
    Builds the stale fixture with a local `dataclasses.replace`, not
    against the live `_BANDS0` object -- the same reasoning `test_m14_
    stale_precertification_falls_back_to_the_live_se_gate` already
    documents for exactly this reason.

    **The "valid" fixture is also built with a local `dataclasses.replace`
    now, forcing the citation's `gm_tuning_version` to match `_BANDS0`'
    own** -- Phase 3 §11 step 4 (Decision P3-30) bumped the live version
    2 -> 3 without re-measuring these citations (that is step 8's job),
    so the real, committed M12 precision block is now itself stale and
    can no longer stand in for "valid" unmodified. Forcing the match
    locally is what keeps this test decoupled from that real-data
    staleness, exactly as the stale branch below is already decoupled
    from it in the other direction."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])

    valid_precision = {**band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version}
    valid_band = dataclasses.replace(band, params={**band.params, "precision": valid_precision})
    valid_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": valid_band})

    result_valid = _compute_m12(runs, valid_bands)
    precision_valid = result_valid.detail["precision"]
    assert precision_valid["sd_source"] == "precertified"
    assert precision_valid["sd_across_seeds"] == valid_band.params["precision"]["sd_across_seeds"]
    assert precision_valid["sd_across_seeds_this_run"] is not None
    assert precision_valid["precision_stale"] is False

    stale_precision = {
        **band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version - 1
    }
    stale_band = dataclasses.replace(band, params={**band.params, "precision": stale_precision})
    stale_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": stale_band})

    result_stale = _compute_m12(runs, stale_bands)
    precision_stale = result_stale.detail["precision"]
    assert precision_stale["sd_source"] == "this_run"
    assert precision_stale["precision_stale"] is True
    assert precision_stale["sd_across_seeds"] == precision_stale["sd_across_seeds_this_run"]

    finalized = _finalize_metric(
        stale_band, result_stale, gm_tuning_version=stale_bands.gm_tuning_version
    )
    assert "PRECISION SD STALE" in finalized.status


# --- Sec.6.5 test 6: M11's log scale ---


def test_m11_log_scale_margin_differs_from_linear_margin_and_matches_hand_computation():
    runs = _m11_multi_run_fixture([2, 3, 4], [1, 1, 1])
    result = _compute_m11(runs, _BANDS0)
    precision = result.detail["precision"]

    expected_margin_log = (
        math.log(result.value) - math.log(_BANDS0.metrics["M11"].band_low)
    )
    assert precision["margin_over_band_log"] == pytest.approx(expected_margin_log)
    assert precision["margin_in_se_log"] != pytest.approx(precision["margin_in_se"])


def test_log_or_none_guards_non_positive_and_missing_values():
    """Mutation check: drop the `math.log` transform (return the raw value
    unchanged) and this test's positive-value assertion fails."""
    assert _log_or_none(None) is None
    assert _log_or_none(0.0) is None
    assert _log_or_none(-3.0) is None
    assert _log_or_none(math.e) == pytest.approx(1.0)


# --- B3 fix: the double-scaling defect has real regression coverage ---
#
# The critic proved two concrete mutations survive all 92 pre-fix tests:
# (1) injecting `se_at_this_n = sd_live / math.sqrt(n_runs)` into the
# `sd_source == "this_run"` branch of `_precision_detail`, and (2) dropping
# `transform=_log_or_none` from the M11 log-scale jackknife call. Both get
# a real, mutation-verified test below.


def test_this_run_se_at_this_n_is_the_undivided_jackknife_sd_not_divided_by_sqrt_n():
    """B3: on the `sd_source == "this_run"` branch, `se_at_this_n` must
    equal the live delete-one jackknife SD over `runs` UNDIVIDED --
    `_jackknife_sd_over_seeds`'s own docstring states the jackknife SD
    already IS the SE at this run's own `n_runs`. Forces `this_run` via a
    stale precert (matching this file's other stale-branch tests) on a
    3-run M12 fixture with deliberately unequal per-seed values (a
    fixture where every seed matches would make this vacuous -- the same
    defect class this phase's ledger has already caught three times).

    Mutation-verify: divide `se_at_this_n` by `math.sqrt(n_runs)` in the
    `else` branch of `_precision_detail` (the `sd_source == "this_run"`
    case) -- this test fails (it asserts the undivided value to full
    float precision, and `n_runs == 3` makes `/sqrt(3)` a ~1.73x change,
    nowhere near floating-point noise)."""
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    band = _BANDS0.metrics["M12"]
    stale_precision = {
        **band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version - 1
    }
    stale_band = dataclasses.replace(band, params={**band.params, "precision": stale_precision})
    stale_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": stale_band})

    result = _compute_m12(runs, stale_bands)
    precision = result.detail["precision"]
    assert precision["sd_source"] == "this_run"

    expected_sd = _jackknife_sd_over_seeds(_m12_raw_value, runs, band)
    n_runs = len(runs)
    assert n_runs == 3
    # The undivided jackknife SD -- what a correct implementation reports.
    assert precision["se_at_this_n"] == expected_sd
    # The double-scaling bug's own output, for contrast -- must NOT match.
    buggy_value = expected_sd / math.sqrt(n_runs)
    assert precision["se_at_this_n"] != pytest.approx(buggy_value)


def test_m11_log_scale_jackknife_sd_is_computed_on_log_replicates_not_raw_replicates():
    """B3: the critic proved dropping `transform=_log_or_none` from the
    log-scale jackknife call in `_precision_detail` survives every
    pre-fix test. Forces `sd_source == "this_run"` (stale precert, same
    pattern as the test above) on M11's own fixture so `sd_across_seeds_
    log_this_run` really is the live jackknife, and asserts it matches a
    hand-computed delete-one jackknife SD over `log(raw_value)`
    replicates -- and explicitly does NOT match the jackknife SD over the
    raw (untransformed) replicates, which is what dropping the transform
    would produce instead.

    Mutation-verify: remove `transform=_log_or_none` from the
    `_jackknife_sd_over_seeds` call inside `_precision_detail`'s
    `if scale == "log":` branch -- this test's `!=` assertion (comparing
    against the raw-replicate SD) flips to an accidental match and the
    `==` assertion (against the log-replicate SD) fails, since M11's
    fixture here has raw replicates around 2.5-3.5 (SD ~0.577) but their
    logs cluster much more tightly (SD ~0.194) -- a measurable, non-noise
    difference."""
    runs = _m11_multi_run_fixture([2, 3, 4], [1, 1, 1])
    band = _BANDS0.metrics["M11"]
    stale_precision = {
        **band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version - 1
    }
    stale_band = dataclasses.replace(band, params={**band.params, "precision": stale_precision})
    stale_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M11": stale_band})

    result = _compute_m11(runs, stale_bands)
    precision = result.detail["precision"]
    assert precision["sd_source"] == "this_run"

    n = len(runs)
    raw_replicates = []
    for i in range(n):
        subset = runs[:i] + runs[i + 1 :]
        raw_replicates.append(_m11_raw_value(subset, band))
    log_replicates = [math.log(v) for v in raw_replicates]

    def _jack_sd(replicates):
        theta_bar = sum(replicates) / n
        ss = sum((t - theta_bar) ** 2 for t in replicates)
        return math.sqrt((n - 1) / n * ss)

    expected_sd_log = _jack_sd(log_replicates)
    sd_over_raw_replicates = _jack_sd(raw_replicates)
    assert len(set(round(r, 6) for r in raw_replicates)) > 1  # not vacuously all-equal

    assert precision["sd_across_seeds_log_this_run"] == pytest.approx(expected_sd_log)
    assert precision["sd_across_seeds_log_this_run"] != pytest.approx(sd_over_raw_replicates)


# --- B4 fix: M11's log-scale `measurability_pass` wiring has real coverage ---


def test_m11_measurability_pass_uses_log_scale_se_not_linear_se():
    """B4(a): the critic proved that using the linear `se_at_this_n`
    instead of `se_at_this_n_log` when computing M11's `measurability_
    pass` survives all pre-fix tests. This fixture's own live jackknife
    SDs are far enough apart (linear ~0.5774, log ~0.1945) that a
    threshold of 0.3 discriminates cleanly: correct (log-based) code
    passes; a linear-based swap would fail. Forces `sd_source ==
    "this_run"` via a stale precert (same pattern as the two tests above)
    and overrides `se_threshold_derived` to 0.3 to sit the boundary
    between the two real SDs.

    Mutation-verify: change `measurability_se = se_at_this_n_log` to
    `measurability_se = se_at_this_n` at the end of `_precision_detail`'s
    `if scale == "log":` branch -- `measurability_pass` flips from `True`
    to `False` and this test fails."""
    runs = _m11_multi_run_fixture([2, 3, 4], [1, 1, 1])
    band = _BANDS0.metrics["M11"]
    stale_precision = {
        **band.params["precision"],
        "gm_tuning_version": _BANDS0.gm_tuning_version - 1,
        "se_threshold_derived": 0.3,
    }
    stale_band = dataclasses.replace(band, params={**band.params, "precision": stale_precision})
    stale_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M11": stale_band})

    result = _compute_m11(runs, stale_bands)
    precision = result.detail["precision"]
    assert precision["sd_source"] == "this_run"
    assert precision["se_at_this_n"] == pytest.approx(0.5773502691896257)
    assert precision["se_at_this_n_log"] == pytest.approx(0.19448915889801538)
    assert precision["se_at_this_n"] > 0.3  # would FAIL the gate if used (the bug)
    assert precision["se_at_this_n_log"] < 0.3  # correctly PASSes the gate
    assert precision["measurability_pass"] is True


def test_precision_detail_asserts_scale_is_linear_or_log():
    """B4(b): `precision.get("scale", "linear")` had no validation --
    dropping or mistyping M11's `"scale": "log"` would silently fall
    through to the linear branch, computing `measurability_pass` off the
    linear `se_at_this_n` against a threshold that is stored in LOG units
    for M11 -- a fabricated pass/fail off mismatched units, not a loud
    failure. Assert a bad scale value raises rather than silently
    mistagging. Mutation-verify: delete the `assert scale in ("linear",
    "log")` guard in `_precision_detail` -- this test fails (no exception
    raised)."""
    band = _BANDS0.metrics["M11"]
    bad_precision = {**band.params["precision"], "scale": "quadratic"}
    bad_band = dataclasses.replace(band, params={**band.params, "precision": bad_precision})
    bad_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M11": bad_band})
    runs = _m11_multi_run_fixture([2, 3, 4], [1, 1, 1])
    with pytest.raises(AssertionError):
        _compute_m11(runs, bad_bands)


# --- Nit 2: a precertified block missing `sd_seeds` degrades, doesn't raise ---


def test_precision_missing_sd_seeds_degrades_to_precision_unavailable_not_typeerror():
    """Nit 2 (Sec.6.2 item 2's own "guard rather than raise" instruction):
    a precertified block missing `sd_seeds` previously crashed with
    `TypeError` (`None / n_runs` inside `math.sqrt`). Must instead degrade
    `se_at_this_n` to `None` and `precision_available` to `False`.
    Mutation-verify: remove the `sd_seeds is not None` conjunct from
    `_precision_detail`'s precertified branch -- this test raises
    `TypeError` instead of passing.

    `gm_tuning_version` is forced to match `_BANDS0`' own live value (see
    `test_precision_precertified_vs_live_sd_selection`'s docstring for
    why the real, committed citation can no longer be relied on for
    this post-Phase-3-step-4) -- this test is about the missing
    `sd_seeds` key specifically, not about staleness, so the version
    dimension is pinned to "valid" rather than left to whatever the real
    file happens to carry."""
    band = _BANDS0.metrics["M12"]
    broken_precision = {
        k: v for k, v in band.params["precision"].items() if k != "sd_seeds"
    } | {"gm_tuning_version": _BANDS0.gm_tuning_version}
    broken_band = dataclasses.replace(band, params={**band.params, "precision": broken_precision})
    broken_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": broken_band})
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])

    result = _compute_m12(runs, broken_bands)  # must not raise
    precision = result.detail["precision"]
    assert precision["sd_source"] == "precertified"
    assert precision["se_at_this_n"] is None
    assert precision["measurability_pass"] is None
    assert precision["precision_available"] is False
    assert result.extra_pass is True


# --- B1's real half: `margin_in_se_at_sd_seeds`, additive and verdict-inert ---


def test_margin_in_se_at_sd_seeds_populated_only_when_precertified_and_never_gates():
    """B1 (orchestrator fix round, resolving the real communication-risk
    half of the critic's B1 finding): `margin_in_se` answers "is a run at
    THIS seed count a reliable read of this metric"; `margin_in_se_at_
    sd_seeds` (populated only when `sd_source == "precertified"`) answers
    the different question "how far is this run's own value from the
    band, judged against the metric's best-established large-sample
    dispersion" -- `se_at_sd_seeds` is `sd_across_seeds` used
    UNEXTRAPOLATED. Confirms it is `None` on the `this_run` branch (no
    separate large-n figure exists to report) and never touches
    `extra_pass`/`passed`.

    `gm_tuning_version` is forced to match `_BANDS0`' own live value for
    the "precertified" half, per `test_precision_precertified_vs_live_
    sd_selection`'s docstring -- the real, committed M12 citation is
    itself stale post-Phase-3-step-4."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])

    valid_precision = {**band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version}
    valid_band = dataclasses.replace(band, params={**band.params, "precision": valid_precision})
    valid_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": valid_band})

    result = _compute_m12(runs, valid_bands)
    precision = result.detail["precision"]
    assert precision["sd_source"] == "precertified"
    expected_se_at_sd_seeds = valid_band.params["precision"]["sd_across_seeds"]
    assert precision["se_at_sd_seeds"] == expected_se_at_sd_seeds
    expected_margin = precision["margin_over_band"] / expected_se_at_sd_seeds
    assert precision["margin_in_se_at_sd_seeds"] == pytest.approx(expected_margin)
    # A genuinely different number from margin_in_se (which projects to n_runs).
    assert precision["margin_in_se_at_sd_seeds"] != pytest.approx(precision["margin_in_se"])
    assert result.extra_pass is True

    stale_precision = {
        **band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version - 1
    }
    stale_band = dataclasses.replace(band, params={**band.params, "precision": stale_precision})
    stale_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": stale_band})
    result_stale = _compute_m12(runs, stale_bands)
    precision_stale = result_stale.detail["precision"]
    assert precision_stale["sd_source"] == "this_run"
    assert precision_stale["se_at_sd_seeds"] is None
    assert precision_stale["margin_in_se_at_sd_seeds"] is None


# --- Cheap nits (3, 4, 6) folded in here since they're quick and match ---
# --- established patterns already in this file. Nit 5 is covered above ---
# --- by the two `sd_across_seeds_this_run`/`sd_across_seeds_log_this_run` ---
# --- transparency assertions in the B3 tests. ---


def test_margin_over_band_sign_matches_value_vs_band_low():
    """Nit 3: `margin_over_band` must be positive when `value` clears the
    band and negative when it falls short -- a sign-flip regression
    (e.g. `band.band_low - value`) would invert every downstream
    `margin_in_se`/annotation without changing `measurability_pass`'s own
    magnitude-only comparison, so this needs its own explicit check."""
    band = _BANDS0.metrics["M12"]
    runs_above = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    result_above = _compute_m12(runs_above, _BANDS0)
    assert result_above.value > band.band_low
    assert result_above.detail["precision"]["margin_over_band"] > 0

    runs_below = _m12_multi_run_fixture([0, 0, 0], [5, 6, 7])
    result_below = _compute_m12(runs_below, _BANDS0)
    assert result_below.value < band.band_low
    assert result_below.detail["precision"]["margin_over_band"] < 0


def test_required_seeds_if_over_threshold_uses_len_runs_basis():
    """Nit 4: `required_seeds_if_over_threshold` must scale off
    `len(runs)` (this run's own seed count), matching the corrected
    `compute_m14` pattern (`test_m14_required_seeds_uses_len_runs_not_
    min_seeds`) -- not any fixed `min_seeds` constant. Uses a 4-run
    fixture deliberately (`len(runs) == 4 != 3`, the design's own
    `min_seeds` floor) -- the 3-run fixture used elsewhere in this file
    would make a hardcoded-`3` mutation pass by coincidence, the exact
    non-discriminating-test defect class this project's ledger has
    caught before. Forces a tiny threshold so `measurability_pass` is
    `False` (the only branch that populates this field) and checks the
    formula directly.

    Mutation-verify: replace `n_runs` with a hardcoded `3` in the
    `required_seeds` formula -- against the ORIGINAL 3-run fixture this
    passes by coincidence (vacuous); against this 4-run fixture it fails,
    which is exactly why the 4-run fixture is required here."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6, 9], [0, 1, 2, 3])
    tiny_precision = {**band.params["precision"], "se_threshold_derived": 1e-9}
    tiny_band = dataclasses.replace(band, params={**band.params, "precision": tiny_precision})
    tiny_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": tiny_band})

    result = _compute_m12(runs, tiny_bands)
    precision = result.detail["precision"]
    n_runs = len(runs)
    assert precision["n_runs"] == n_runs
    expected = math.ceil(n_runs * (precision["se_at_this_n"] / 1e-9) ** 2)
    assert precision["required_seeds_if_over_threshold"] == expected


def test_precision_available_true_in_the_clean_precertified_case():
    """Nit 6: the positive-case complement to `test_precision_missing_
    sd_seeds_degrades_to_precision_unavailable_not_typeerror` above --
    confirms `precision_available` reads `True` (not just non-crashing)
    on an ordinary, fully-populated precertified block.

    `gm_tuning_version` is forced to match `_BANDS0`' own live value (see
    `test_precision_precertified_vs_live_sd_selection`'s docstring) --
    without it, the real, committed M12 citation reads stale post-
    Phase-3-step-4 and this test silently starts exercising the
    `this_run` branch instead of the "clean precertified" case its own
    name and docstring claim, which happened once already: it kept
    passing on the stale branch (`precision_available` is `True` there
    too) with nobody noticing until a critic re-derived it by hand."""
    band = _BANDS0.metrics["M12"]
    valid_precision = {**band.params["precision"], "gm_tuning_version": _BANDS0.gm_tuning_version}
    valid_band = dataclasses.replace(band, params={**band.params, "precision": valid_precision})
    valid_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": valid_band})

    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    result = _compute_m12(runs, valid_bands)
    assert result.detail["precision"]["sd_source"] == "precertified"
    assert result.detail["precision"]["precision_available"] is True


# --- Sec.6.5 test 7: the annotation branch fires and only fires when it should ---


def test_precision_annotation_fires_below_2_se_and_not_at_or_above_it():
    """Mutation check: flip `< 2.0` to `<= 2.0` (or vice versa) in
    `_finalize` and this exact-boundary pair stops discriminating.

    `gm_tuning_version` is forced to match `_BANDS0`' own live value inside
    `_bands_forcing_margin_in_se` (see `test_precision_precertified_vs_
    live_sd_selection`'s docstring) -- without it, the real, committed
    M12 citation reads stale post-Phase-3-step-4, `_precision_detail`
    falls onto its `this_run` branch, and the live jackknife SD silently
    overrides the `forced_sd` this test is trying to pin, changing what
    `margin_in_se` actually is out from under the assertion below."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    base_result = _compute_m12(runs, _BANDS0)
    margin_over_band = base_result.detail["precision"]["margin_over_band"]
    sd_seeds = band.params["precision"]["sd_seeds"]
    n_runs = len(runs)
    scale_factor = math.sqrt(sd_seeds / n_runs)

    def _bands_forcing_margin_in_se(target_margin: float):
        forced_sd = (margin_over_band / target_margin) / scale_factor
        precision = {
            **band.params["precision"],
            "sd_across_seeds": forced_sd,
            "gm_tuning_version": _BANDS0.gm_tuning_version,
        }
        forced_band = dataclasses.replace(band, params={**band.params, "precision": precision})
        forced_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": forced_band})
        return forced_bands, forced_band

    def _finalize(forced_band, result, forced_bands):
        return _finalize_metric(
            forced_band, result, gm_tuning_version=forced_bands.gm_tuning_version
        )

    bands_1_9, band_1_9 = _bands_forcing_margin_in_se(1.9)
    result_1_9 = _compute_m12(runs, bands_1_9)
    assert result_1_9.detail["precision"]["margin_in_se"] == pytest.approx(1.9, abs=1e-6)
    finalized_1_9 = _finalize(band_1_9, result_1_9, bands_1_9)
    assert "SE from the band" in finalized_1_9.status

    bands_2_1, band_2_1 = _bands_forcing_margin_in_se(2.1)
    result_2_1 = _compute_m12(runs, bands_2_1)
    assert result_2_1.detail["precision"]["margin_in_se"] == pytest.approx(2.1, abs=1e-6)
    finalized_2_1 = _finalize(band_2_1, result_2_1, bands_2_1)
    assert "SE from the band" not in finalized_2_1.status
    assert finalized_2_1.status in ("PASS", "FAIL")

    # The exact boundary itself -- the only fixture shape that can catch a
    # `<` -> `<=` mutation, since 1.9/2.1 both sit strictly on one side of
    # 2.0 regardless of which operator is used. `sd_seeds` is pinned to
    # `n_runs` here (scale_factor == 1.0 exactly) so `margin_in_se` lands
    # on EXACTLY 2.0 in floating point, not 2.0 +/- fp noise from an
    # irrational `sqrt(45/3)` scale factor that could land on either side
    # of the boundary regardless of which operator is live.
    forced_sd_exact_2 = margin_over_band / 2.0
    precision_exact_2 = {
        **band.params["precision"], "sd_across_seeds": forced_sd_exact_2, "sd_seeds": n_runs,
        "gm_tuning_version": _BANDS0.gm_tuning_version,
    }
    band_2_0 = dataclasses.replace(band, params={**band.params, "precision": precision_exact_2})
    bands_2_0 = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M12": band_2_0})
    result_2_0 = _compute_m12(runs, bands_2_0)
    assert result_2_0.detail["precision"]["margin_in_se"] == 2.0
    finalized_2_0 = _finalize(band_2_0, result_2_0, bands_2_0)
    assert "SE from the band" not in finalized_2_0.status
    assert finalized_2_0.status in ("PASS", "FAIL")


def test_precision_annotation_never_shadows_known_gap_or_precert_stale_branches():
    """Mutation check: move the new `elif` branch above `known_gap` in
    `_finalize` and this test fails (the status would show the precision
    annotation instead of the known-gap one)."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    result = _compute_m12(runs, _BANDS0)
    assert "precision" in result.detail  # sanity: the branch would otherwise fire here

    synthetic_band = dataclasses.replace(
        band, known_gap={"boundaries": ["fake_boundary"], "reason": "test-only"}
    )
    synthetic_result = dataclasses.replace(
        result, detail={**result.detail, "known_gap_violations": 1}
    )
    finalized = _finalize_metric(
        synthetic_band, synthetic_result, gm_tuning_version=_BANDS0.gm_tuning_version
    )
    assert "known gap" in finalized.status
    assert "SE from the band" not in finalized.status


# --- Sec.6.5 test 8: M17 ---


def test_m17_precision_is_seed_invariant_and_carries_no_params_precision_block():
    band = _BANDS0.metrics["M17"]
    assert "precision" not in band.params
    small_m17_band = dataclasses.replace(band, params={**band.params, "drafts": 4})
    small_bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M17": small_m17_band})
    result = compute_m17(small_bands)
    precision = result.detail.get("precision")
    assert precision is not None
    assert precision["seed_invariant"] is True
    assert precision["n_classes"] == 4
    assert precision["resampling_unit"] == "draft_class"


def test_m17_precision_jackknife_matches_hand_computation_on_a_4_class_fixture():
    band = _BANDS0.metrics["M17"]
    per_class = [(3, 10, 1, 10), (5, 10, 2, 10), (4, 10, 0, 10), (6, 10, 3, 10)]
    matches_a = sum(p[0] for p in per_class)
    total_a = sum(p[1] for p in per_class)
    matches_b = sum(p[2] for p in per_class)
    total_b = sum(p[3] for p in per_class)
    value = matches_a / total_a - matches_b / total_b

    result = _m17_precision_detail(band, value, per_class, matches_a, total_a, matches_b, total_b)

    replicates = []
    for m_a_i, t_a_i, m_b_i, t_b_i in per_class:
        ra_loo = (matches_a - m_a_i) / (total_a - t_a_i)
        rb_loo = (matches_b - m_b_i) / (total_b - t_b_i)
        replicates.append(ra_loo - rb_loo)
    n = len(replicates)
    theta_bar = sum(replicates) / n
    ss = sum((r - theta_bar) ** 2 for r in replicates)
    expected_sd = math.sqrt((n - 1) / n * ss)

    assert result["sd_jackknife"] == pytest.approx(expected_sd)
    assert len(set(round(r, 9) for r in replicates)) > 1  # not vacuously all-equal


# --- Sec.6.5 test 9: band-file round trip ---


def test_precision_bands_derived_thresholds_and_versions_round_trip():
    """Phase 3 §11 step 4 (Decision P3-30) bumped `GM_TUNING_VERSION`
    2 -> 3 without re-measuring any of these five citations -- that was
    step 8's job (a 45-seed re-precertification under
    artifacts/scratch_measure_m14_se_v3.py), not step 4's, per Decision
    P2-35 item 2's own "by design, not as a regression" rule (`tuning.
    py::GM_TUNING_VERSION`'s own docstring carries the same citation).
    Step 8 has now landed (docs/ledger/phase-3.md), so every citation
    below reads FRESH (`gm_tuning_version == 3`, matching the live
    value) -- this test asserts that match directly, so a future
    GM_TUNING_VERSION bump that re-stales these citations (and does not
    itself re-measure them) is what changes this assertion back, not
    silent drift nobody notices."""
    for metric_id in ("M11", "M12", "M13", "M15", "M16"):
        band = _BANDS0.metrics[metric_id]
        precision = band.params["precision"]
        assert precision["gm_tuning_version"] == 3, (
            f"{metric_id}: expected the step-8 re-precertification citation (3), "
            f"matching the live {_BANDS0.gm_tuning_version} -- if this reads "
            "something else, either the citation regressed or GM_TUNING_VERSION "
            "moved again without a re-precertification"
        )
        assert precision["gm_tuning_version"] == _BANDS0.gm_tuning_version
        null_value = precision["null_value"]
        if metric_id == "M11":
            expected_threshold = (math.log(band.band_low) - math.log(null_value)) / 2
        else:
            expected_threshold = (band.band_low - null_value) / 2
        assert precision["se_threshold_derived"] == pytest.approx(expected_threshold)

    assert "precision" not in _BANDS0.metrics["M14"].params
    assert "precision" not in _BANDS0.metrics["M17"].params


# ---------------------------------------------------------------------------
# Decision P3-47 -- Sec.11 step 9's Family-3 measurability fix. `params.
# gated_value_precertified` (M11-M16) is the new, VALUE-carrying citation
# alongside the existing `precision`/`se_precertified` SE-only citations --
# every test below uses `_BANDS` on purpose (the real M11-M16 citations,
# `behavior_scope`-stamped by `_add_family3_behavior_scope` above so they
# read VALID against Decision P4-46/P4-72's scope conjunct too -- see that
# function's own docstring for why the stamp lives on this in-memory test
# fixture and never on `seeds/gm_validation_bands.json` itself), except
# where a test deliberately builds its own stale/absent fixture.
# ---------------------------------------------------------------------------


def test_family3_valid_citation_gates_on_citation_value_via_real_evaluate():
    """Test 1: a valid citation gates `.value`/`passed`/`status`, not this
    run's own live (fixture) reading -- and `passed` is derived through the
    real, imported `evaluate(band, ...)` function, never a hand-written
    comparison. `detail["value_source"] == "precertified_corpus"`."""
    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    band = _BANDS.metrics["M16"]
    citation = band.params["gated_value_precertified"]
    assert citation["gm_tuning_version"] == _BANDS.gm_tuning_version  # valid, not stale

    result = _compute_m16(runs, _BANDS)
    assert result.value == pytest.approx(citation["value"])
    live_value = _m16_raw_value(runs, band)
    assert result.value != pytest.approx(live_value)  # NOT this run's own fixture value
    assert result.detail["value_source"] == "precertified_corpus"
    assert result.detail["live_reading"]["value"] == pytest.approx(live_value)

    expected_passed = evaluate(band, citation["value"])
    finalized = _finalize_metric(band, result, gm_tuning_version=_BANDS.gm_tuning_version)
    assert finalized.passed is expected_passed
    assert "Family-3 gated at 45 precertified seeds" in finalized.status
    assert finalized.counted_in_go is True


def test_family3_gated_passed_is_invariant_to_live_reading_mutations():
    """Test 2 -- the safety property that matters most: a valid citation's
    own `passed` (and gated `value`) must never move no matter what
    `detail["live_reading"]` looks like -- `live_reading` is a disclosure-
    only annex (`_finalize`'s status string legitimately QUOTES its own
    `value`/`margin_in_se` for transparency, so the status STRING is
    expected to change; `passed`/`value`/`counted_in_go` must not). A real
    mutation test against the real `MetricValue`/`MetricResult`, not just
    an assertion on normal input.

    Mutation-verify: this test is written to catch the wrong
    implementation where `_finalize`'s Family-3 branch reads `passed`
    itself off `mv.detail["live_reading"]["passed"]` instead of `evaluate
    (band, mv.value)` -- confirmed by hand this round (temporarily
    swapping the two inside the citation branch made this test's `passed`
    assertion fail, as expected, on the `{"passed": not ...}` mutation
    specifically; reverted before landing)."""
    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    band = _BANDS.metrics["M16"]
    result = _compute_m16(runs, _BANDS)
    finalized = _finalize_metric(band, result, gm_tuning_version=_BANDS.gm_tuning_version)
    assert result.detail["value_source"] == "precertified_corpus"

    live_reading = result.detail["live_reading"]
    mutations = (
        {**live_reading, "value": -999.0},
        {**live_reading, "passed": not live_reading["passed"]},
        {**live_reading, "indeterminate": True},
        {**live_reading, "precision": None},
        None,
    )
    for mutated_live_reading in mutations:
        mutated = dataclasses.replace(
            result, detail={**result.detail, "live_reading": mutated_live_reading}
        )
        refinalized = _finalize_metric(band, mutated, gm_tuning_version=_BANDS.gm_tuning_version)
        assert refinalized.passed == finalized.passed
        assert refinalized.value == finalized.value
        assert refinalized.counted_in_go == finalized.counted_in_go
        assert "Family-3 gated at 45 precertified seeds" in refinalized.status


def test_family3_stale_citation_falls_back_to_live_reading_never_skip():
    """Test 3: a `gm_tuning_version`-mismatched citation falls back to the
    live reading, loudly -- `value_source == "this_run"`, status carries
    "FAMILY-3 PRECERT STALE", `counted_in_go` stays `True`, `passed` is
    never `None` (never SKIP). Uses a local `dataclasses.replace`-built
    fixture (on `band`/`bands`, the real dataclasses; the citation itself
    is a plain dict loaded from JSON, spread via `{**citation, ...}` --
    this suite's own established pattern for forcing staleness, e.g.
    `test_m14_stale_precertification_falls_back_to_the_live_se_gate`)."""
    band = _BANDS.metrics["M16"]
    live_citation = band.params["gated_value_precertified"]
    stale_citation = {**live_citation, "gm_tuning_version": _BANDS.gm_tuning_version - 1}
    stale_band = dataclasses.replace(
        band, params={**band.params, "gated_value_precertified": stale_citation}
    )
    stale_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M16": stale_band})

    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    result = _compute_m16(runs, stale_bands)
    assert result.detail["value_source"] == "this_run"
    assert result.detail["gated_value_precert_stale"] is True
    live_value = _m16_raw_value(runs, band)
    assert result.value == pytest.approx(live_value)

    finalized = _finalize_metric(
        stale_band, result, gm_tuning_version=stale_bands.gm_tuning_version
    )
    assert "FAMILY-3 PRECERT STALE" in finalized.status
    assert finalized.counted_in_go is True
    assert finalized.passed is not None


def test_family3_absent_citation_leaks_no_family3_specific_detail_keys():
    """Test 4 (renamed, NIT 1 critic round T dispatch): the ORIGINAL name
    ("...is_byte_identical_to_pre_p3_47_behaviour") overclaimed -- M16's
    own detail is NOT byte-identical to its pre-P3-47 shape here even with
    no citation present, because Decision P3-47 step 1's UNRELATED
    season-pin fix unconditionally adds `detail["dropped_runs"]` (0 or
    more) regardless of whether a value citation exists at all. What this
    test actually guarantees, and all it needs to: a bands fixture without
    `params.gated_value_precertified` leaks none of the FAMILY-3-CITATION-
    specific keys (`live_reading`/`value_source`/`gated_value_precertified`/
    `gated_value_precert_stale`) into `detail`, and `.value` is exactly
    this run's own live reading, for every one of M11-M13/M15/M16 AND M14."""
    for metric_id, (compute_fn, raw_fn, build_runs) in _PRECISION_METRIC_FIXTURES.items():
        band = _BANDS0.metrics[metric_id]
        assert "gated_value_precertified" not in band.params
        runs = build_runs()
        result = compute_fn(runs, _BANDS0)
        assert result.value == pytest.approx(raw_fn(runs, band))
        for leaked_key in (
            "live_reading", "value_source", "gated_value_precertified", "gated_value_precert_stale",
        ):
            assert leaked_key not in result.detail, (metric_id, leaked_key)

    band14 = _BANDS0.metrics["M14"]
    assert "gated_value_precertified" not in band14.params
    run = _m14_high_se_run()
    result14 = _compute_m14((run,), _BANDS0)
    for leaked_key in (
        "live_reading", "value_source", "gated_value_precertified", "gated_value_precert_stale",
    ):
        assert leaked_key not in result14.detail


# ---------------------------------------------------------------------------
# Critic round T dispatch (B2/B3/B4/B5/B6): M14's value-citation hookup had
# zero dedicated test coverage (B4) and two composability/mislabeling bugs
# (B2/B3) that survived because of it; the general RS3 precision branch had
# a related-but-distinct mislabeling bug for a valid citation's own gated
# reading (B6), and the fail-side-only INDETERMINATE property itself had
# zero coverage anywhere (B5). Every test below is a real, live-computed
# fixture (no hand-typed expected numbers beyond what `_finalize`/`evaluate`
# themselves produce) -- verified by hand against the exact pre-fix formulas
# to confirm each one fails under the specific mutation it targets (see each
# docstring).
# ---------------------------------------------------------------------------


def test_m14_family3_valid_citation_gates_on_citation_value_via_real_evaluate():
    """B4 test 1 (M14-specific, mirrors `test_family3_valid_citation_gates_
    on_citation_value_via_real_evaluate` for M16): M14's own hookup (`compute_
    m14`'s separate, hand-rolled citation branch, not `_attach_dual_reading`)
    gates `.value`/`passed`/`status` on the citation, not this run's own live
    reading. Uses the real, live `_BANDS` -- M14's own checked-in citation
    AND `se_precertified` are both valid against the real `gm_tuning_
    version` today."""
    band = _BANDS.metrics["M14"]
    citation = band.params["gated_value_precertified"]
    assert citation["gm_tuning_version"] == _BANDS.gm_tuning_version  # valid, not stale
    se_precert = band.params["se_precertified"]
    assert se_precert["gm_tuning_version"] == _BANDS.gm_tuning_version  # valid, not stale

    run = _m14_high_se_run()
    result = _compute_m14((run,), _BANDS)
    assert result.value == pytest.approx(citation["value"])
    assert result.detail["value_source"] == "precertified_corpus"
    assert result.detail["live_reading"]["value"] == pytest.approx(7.0)
    assert result.value != pytest.approx(result.detail["live_reading"]["value"])

    expected_passed = evaluate(band, citation["value"])
    finalized = _finalize_metric(band, result, gm_tuning_version=_BANDS.gm_tuning_version)
    assert finalized.passed is expected_passed
    assert "Family-3 gated at 45 precertified seeds" in finalized.status
    assert finalized.counted_in_go is True


def test_m14_family3_stale_citation_falls_back_to_live_reading_never_skip():
    """B4 test 2 (M14-specific, mirrors `test_family3_stale_citation_falls_
    back_to_live_reading_never_skip`): a `gm_tuning_version`-mismatched
    value citation falls back to M14's own live reading, loudly --
    `value_source == "this_run"`, status carries "FAMILY-3 PRECERT STALE",
    `counted_in_go` stays `True`, `passed` is never `None` (never SKIP).
    `se_precertified` stays VALID here so this isolates the value-citation
    fallback from the separate SE-conjunct fallback (covered below)."""
    band14 = _BANDS.metrics["M14"]
    live_citation = band14.params["gated_value_precertified"]
    stale_citation = {**live_citation, "gm_tuning_version": _BANDS.gm_tuning_version - 1}
    stale_band14 = dataclasses.replace(
        band14, params={**band14.params, "gated_value_precertified": stale_citation}
    )
    stale_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M14": stale_band14})

    run = _m14_high_se_run()
    result = _compute_m14((run,), stale_bands)
    assert result.detail["value_source"] == "this_run"
    assert result.detail["gated_value_precert_stale"] is True
    assert result.value == pytest.approx(result.detail["live_reading"]["value"])

    finalized = _finalize_metric(
        stale_band14, result, gm_tuning_version=stale_bands.gm_tuning_version
    )
    assert "FAMILY-3 PRECERT STALE" in finalized.status
    assert finalized.counted_in_go is True
    assert finalized.passed is not None


def test_m14_family3_gated_passed_is_invariant_to_live_reading_mutations():
    """B4 test 3 (M14-specific, mirrors `test_family3_gated_passed_is_
    invariant_to_live_reading_mutations`): a valid citation's own `passed`
    (and gated `value`) must never move no matter what `detail[
    "live_reading"]` looks like -- `live_reading` is a disclosure-only
    annex. Mutation-verify: this test is written to catch the wrong
    implementation where `_finalize`'s M14 branch reads `passed` itself off
    `mv.detail["live_reading"]["passed"]` instead of `evaluate(band, mv.
    value)`."""
    band = _BANDS.metrics["M14"]
    run = _m14_high_se_run()
    result = _compute_m14((run,), _BANDS)
    finalized = _finalize_metric(band, result, gm_tuning_version=_BANDS.gm_tuning_version)
    assert result.detail["value_source"] == "precertified_corpus"

    live_reading = result.detail["live_reading"]
    mutations = (
        {**live_reading, "value": -999.0},
        {**live_reading, "passed": not live_reading["passed"]},
        {**live_reading, "indeterminate": True},
        {**live_reading, "se_measured": None},
        None,
    )
    for mutated_live_reading in mutations:
        mutated = dataclasses.replace(
            result, detail={**result.detail, "live_reading": mutated_live_reading}
        )
        refinalized = _finalize_metric(band, mutated, gm_tuning_version=_BANDS.gm_tuning_version)
        assert refinalized.passed == finalized.passed
        assert refinalized.value == finalized.value
        assert refinalized.counted_in_go == finalized.counted_in_go
        assert "Family-3 gated at 45 precertified seeds" in refinalized.status


def test_m14_se_precert_stale_warning_still_fires_with_valid_value_citation():
    """B2 mutation-verify: the critic's exact repro -- a valid Family-3
    value citation and a STALE `se_precertified` citation at the same time.
    Before the fix, `_finalize`'s M14 branch was a single `elif` chain
    where the value-citation branch always won, permanently suppressing
    the "...live SE gate re-armed" disclosure the moment a value citation
    existed. Both disclosures must be present and composed: the value
    citation's own PASS (a wide-margin clear GATED reading) AND the
    SE-conjunct's own STALE failure (which is what actually drives the
    overall `passed=False` here, an unrelated reason)."""
    band14 = _BANDS.metrics["M14"]
    live_se_precert = band14.params["se_precertified"]
    stale_se_precert = {**live_se_precert, "gm_tuning_version": _BANDS.gm_tuning_version - 1}
    combo_band14 = dataclasses.replace(
        band14, params={**band14.params, "se_precertified": stale_se_precert}
    )
    combo_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M14": combo_band14})

    run = _m14_high_se_run()
    result = _compute_m14((run,), combo_bands)
    assert result.detail["value_source"] == "precertified_corpus"  # value citation still valid
    assert result.detail["precertification_stale"] is True  # SE citation is the stale one
    assert result.extra_pass is False  # SE conjunct is what fails here, not the value

    finalized = _finalize_metric(
        combo_band14, result, gm_tuning_version=combo_bands.gm_tuning_version
    )
    assert "Family-3 gated at 45 precertified seeds" in finalized.status
    assert "SE PRECERT STALE" in finalized.status
    assert "live SE gate re-armed" in finalized.status
    assert finalized.passed is False  # the SE conjunct's own failure, unrelated to the value


def test_m14_indeterminate_never_applies_to_a_pass_side_live_reading():
    """B3 mutation-verify: the critic's exact repro -- `extra_pass` is
    `False` (both the value citation AND the SE precert are stale here, so
    `.value` falls back to the live reading) but the live value sits
    comfortably on the PASS side of the band (7.0 vs band_low 1.5, margin
    +0.11 SE). Before the fix, `indeterminate` was gated on the combined
    `passed` (`base_passed and extra_pass`), which can be `False` purely
    because of the unrelated SE conjunct -- producing a nonsensical
    "INDETERMINATE" label on a reading that is not a fail at all. The fixed
    rule gates on `base_passed` (the pure band comparison of the reading
    actually in play), so this must read a clean `PASS`, and the
    SE-conjunct's own separate failure must still be visible."""
    band14 = _BANDS.metrics["M14"]
    live_se_precert = band14.params["se_precertified"]
    stale_se_precert = {**live_se_precert, "gm_tuning_version": _BANDS.gm_tuning_version - 1}
    live_citation = band14.params["gated_value_precertified"]
    stale_citation = {**live_citation, "gm_tuning_version": _BANDS.gm_tuning_version - 1}
    combo_band14 = dataclasses.replace(
        band14,
        params={
            **band14.params,
            "se_precertified": stale_se_precert,
            "gated_value_precertified": stale_citation,
        },
    )
    combo_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M14": combo_band14})

    run = _m14_high_se_run()
    result = _compute_m14((run,), combo_bands)
    assert result.detail["value_source"] == "this_run"  # value citation is stale -> live value
    assert result.value == pytest.approx(7.0)
    assert result.extra_pass is False  # SE conjunct fails independently

    finalized = _finalize_metric(
        combo_band14, result, gm_tuning_version=combo_bands.gm_tuning_version
    )
    assert "INDETERMINATE" not in finalized.status
    assert finalized.status.startswith("PASS")
    assert "SE PRECERT STALE" in finalized.status  # the SE conjunct's own failure still shows
    assert finalized.passed is False


def test_m12_comfortable_pass_within_2_se_is_never_indeterminate():
    """B5: the string `INDETERMINATE` had zero test coverage anywhere in
    this suite before this test -- mutating the fail-side-only gate
    (`passed is False and margin_in_se > -2.0`) to a symmetric `abs(
    margin_in_se) < 2.0` survives the entire suite untouched, exactly the
    +/-2-SE symmetric gate the architect explicitly rejected in design
    (docs/ledger/phase-3.md round 38: "a P2-22 amendment disguised as a
    metric fix," would un-green already-passing metrics -- M12's and M16's
    own real 45-seed citation margins, 0.825 SE and 0.342 SE, are both
    comfortably inside a symmetric window). This fixture reproduces the
    same shape without any citation at all (`_BANDS0`, the ordinary
    precision path, sub-case 3): margin_in_se lands at ~1.96 SE, a clean
    PASS. Under the correct fail-side-only rule this stays `PASS`; under
    the rejected symmetric rule it flips to `INDETERMINATE` even though
    `passed` is `True`."""
    band = _BANDS0.metrics["M12"]
    runs = _m12_multi_run_fixture([0, 3, 6], [0, 1, 2])
    result = _compute_m12(runs, _BANDS0)
    assert result.detail["precision"]["margin_in_se"] == pytest.approx(1.958336921462624)
    finalized = _finalize_metric(band, result, gm_tuning_version=_BANDS0.gm_tuning_version)
    assert finalized.passed is True
    assert "INDETERMINATE" not in finalized.status
    assert finalized.status == "PASS (precision: 1.96 SE from the band, n=3)"


def test_m16_indeterminate_never_applies_to_a_valid_citations_own_gated_reading():
    """B6: a valid citation's own gated reading must never be labeled
    INDETERMINATE -- Decision P3-47 round 39's own spec ("fires when a
    citation is stale/absent, never when a valid citation gates"). The
    prior code's comment claimed "measurability is clear by a wide margin
    ... indeterminate should never actually fire here," checking the wrong
    statistic (`measurability_pass`, not `margin_in_se`); M16's real
    45-seed margin is only 0.342 SE, comfortably inside the 2-SE window.
    Hand-substitutes the citation's value to 1.49 (a plausible future
    re-precertification landing just below the 1.5 floor) -- `base_passed`
    is `False` and `margin_in_se` (-0.057) sits well within the symmetric
    window, exactly the shape that flipped to INDETERMINATE before this
    fix. Must read a clean `FAIL`, never `INDETERMINATE`."""
    band = _BANDS.metrics["M16"]
    citation = band.params["gated_value_precertified"]
    mutated_citation = {**citation, "value": 1.49}
    mutated_band = dataclasses.replace(
        band, params={**band.params, "gated_value_precertified": mutated_citation}
    )
    mutated_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M16": mutated_band})

    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    result = _compute_m16(runs, mutated_bands)
    assert result.value == pytest.approx(1.49)
    assert result.detail["value_source"] == "precertified_corpus"
    assert evaluate(mutated_band, result.value) is False  # base_passed -- below band_low 1.5

    finalized = _finalize_metric(
        mutated_band, result, gm_tuning_version=mutated_bands.gm_tuning_version
    )
    assert "INDETERMINATE" not in finalized.status
    assert finalized.status.startswith("FAIL")
    assert finalized.passed is False


def test_family3_citation_verdict_keys_is_exactly_status_passed_counted_in_go():
    """NIT 2 (critic round T dispatch): asserted exactly, not just applied
    -- the same discipline `EXCLUDED_REPORT_FIELDS`'s own test gets -- so a
    later change cannot silently widen or narrow this set without this
    test failing."""
    assert _FAMILY3_CITATION_VERDICT_KEYS == frozenset({"status", "passed", "counted_in_go"})


def test_m16_citation_verdict_keys_are_stripped_before_storage():
    """NIT 2: a citation carrying `status`/`passed`/`counted_in_go` (never
    specified by Decision P3-47 round 39's own citation shape -- "value +
    provenance + gm_tuning_version lock only" -- but nothing previously
    rejected them) must not have those keys silently copied into `detail[
    "gated_value_precertified"]`, where they would land in `report.json`
    looking like they mean something (e.g. a stray `passed: false` sitting
    next to a real `PASS` status computed from the band)."""
    band = _BANDS.metrics["M16"]
    citation = band.params["gated_value_precertified"]
    poisoned_citation = {**citation, "status": "PASS", "passed": False, "counted_in_go": False}
    poisoned_band = dataclasses.replace(
        band, params={**band.params, "gated_value_precertified": poisoned_citation}
    )
    poisoned_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M16": poisoned_band})

    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    result = _compute_m16(runs, poisoned_bands)
    stored_citation = result.detail["gated_value_precertified"]
    for verdict_key in ("status", "passed", "counted_in_go"):
        assert verdict_key not in stored_citation, verdict_key
    assert stored_citation["value"] == pytest.approx(citation["value"])  # everything else intact


def test_m14_citation_verdict_keys_are_stripped_before_storage():
    """NIT 2, same guarantee for M14's own, separate hookup (`compute_
    m14`'s hand-rolled citation branch, not `_attach_dual_reading`)."""
    band = _BANDS.metrics["M14"]
    citation = band.params["gated_value_precertified"]
    poisoned_citation = {**citation, "status": "PASS", "passed": False, "counted_in_go": False}
    poisoned_band = dataclasses.replace(
        band, params={**band.params, "gated_value_precertified": poisoned_citation}
    )
    poisoned_bands = dataclasses.replace(_BANDS, metrics={**_BANDS.metrics, "M14": poisoned_band})

    run = _m14_high_se_run()
    result = _compute_m14((run,), poisoned_bands)
    stored_citation = result.detail["gated_value_precertified"]
    for verdict_key in ("status", "passed", "counted_in_go"):
        assert verdict_key not in stored_citation, verdict_key
    assert stored_citation["value"] == pytest.approx(citation["value"])


# ---------------------------------------------------------------------------
# Decision P4-46/P4-59/P4-72/P4-73/P4-74 -- the Family-3 staleness
# predicate's scope conjunct. The pure predicate test below (4 rows, hand-
# built dicts, no corpus) proves the LOGIC; the two site tests after it
# prove the logic is actually WIRED at each hookup (`_attach_dual_reading`
# via M16, `compute_m14`'s own separate hookup) -- Decision P4-74's own
# "two tiers, a builder must not substitute the cheap one for the
# expensive one." The live citations (`_BANDS`, `behavior_scope`-stamped
# by `_add_family3_behavior_scope` above) cannot exercise this at all --
# see `test_real_family3_citations_read_stale_for_the_scope_missing_
# reason_today` below for the corroborating, zero-evidence-by-construction
# observation against the genuinely unpatched, on-disk corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "citation, expected_status",
    [
        pytest.param(
            {"gm_tuning_version": 3, "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION},
            Family3CitationStatus.VALID,
            id="a-version-and-scope-match",
        ),
        pytest.param(
            {"gm_tuning_version": 3, "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION + 1},
            Family3CitationStatus.SCOPE_MISMATCH,
            id="b-version-matches-scope-differs",
        ),
        pytest.param(
            {"gm_tuning_version": 3},
            Family3CitationStatus.SCOPE_MISSING,
            id="c-version-matches-no-scope-key",
        ),
        pytest.param(
            {"gm_tuning_version": 2, "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION},
            Family3CitationStatus.VERSION_MISMATCH,
            id="d-version-mismatch",
        ),
        pytest.param(None, Family3CitationStatus.ABSENT, id="e-no-citation-at-all"),
    ],
)
def test_family3_citation_status_truth_table(citation, expected_status):
    """Decision P4-74's four-row truth table, plus the `ABSENT` case
    `compute_m14`'s own `:1476` hookup depends on being distinct from the
    four STALE verdicts (Decision P4-72). Case (c)'s `gm_tuning_version`
    is pinned to the version under test (`3`, matching this test's own
    `gm_tuning_version=3` call below) -- per Decision P4-74 part 2, an
    unpinned case (c) would short-circuit through the version branch and
    certify nothing about the scope logic. Cases (b) and (c) both read
    STALE (not `VALID`) -- Decision P4-74's own correction of Decision
    P4-62's false "(b) is the only discriminating cell" claim; a version-
    only predicate would incorrectly read both as valid."""
    status = _family3_citation_status(citation, gm_tuning_version=3)
    assert status is expected_status
    if status is not Family3CitationStatus.VALID and citation is not None:
        # Every STALE case must produce a non-empty, distinguishable
        # reason string (Decision P4-59's three renderings) -- ABSENT
        # never renders one (callers only ask when a citation exists).
        reason = _family3_citation_reason(status, citation, gm_tuning_version=3)
        assert reason, (status, citation)


def test_family3_citation_status_case_c_is_not_discriminating_when_version_unpinned():
    """Decision P4-74 part 2's own warning, reproduced: build case (c)
    with a `gm_tuning_version` that does NOT match the bands version under
    test, and it collapses into row (d) (VERSION_MISMATCH) instead of
    (c) (SCOPE_MISSING) -- discriminating nothing about the scope
    conjunct. This is the trap the pinned case above avoids."""
    unpinned_case_c = {"gm_tuning_version": 1}  # does not match gm_tuning_version=3 below
    status = _family3_citation_status(unpinned_case_c, gm_tuning_version=3)
    assert status is Family3CitationStatus.VERSION_MISMATCH  # NOT scope_missing


def test_family3_citation_status_behavior_scope_version_is_a_real_fixture_driven_parameter():
    """Critic nit 6 fix: `behavior_scope_version` is a real keyword
    parameter (defaulting to the live `GM_BEHAVIOR_SCOPE_VERSION`), not a
    bare module-global read -- symmetric with `gm_tuning_version`, which
    every caller already threads explicitly. Driven here entirely by
    fixture values, with no monkeypatching of the module constant: the
    identical citation reads VALID against the scope it was cited at and
    SCOPE_MISMATCH against a different one, both supplied directly as
    call arguments."""
    citation = {"gm_tuning_version": 3, "behavior_scope": 7}
    assert (
        _family3_citation_status(citation, gm_tuning_version=3, behavior_scope_version=7)
        is Family3CitationStatus.VALID
    )
    assert (
        _family3_citation_status(citation, gm_tuning_version=3, behavior_scope_version=8)
        is Family3CitationStatus.SCOPE_MISMATCH
    )
    # Omitting the keyword still falls back to the live module constant --
    # the default-preservation guard every other optional-parameter
    # convention in this codebase gets (`head_to_head_enabled`, `agency_
    # enabled`, etc.).
    assert (
        _family3_citation_status(
            {"gm_tuning_version": 3, "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION},
            gm_tuning_version=3,
        )
        is Family3CitationStatus.VALID
    )


def test_family3_attach_dual_reading_site_trips_on_scope_mismatch():
    """Site test 1 of 2 (Decision P4-74 part 4): `_attach_dual_reading`
    (`metrics.py:1264`'s hookup, exercised here via M16) actually calls
    the shared predicate, not just that the predicate itself is correct
    in isolation. A version-matched-but-scope-mismatched citation must
    read STALE (`value_source == "this_run"`), which a version-only
    predicate would get wrong (it would read VALID)."""
    band = _BANDS.metrics["M16"]
    live_citation = band.params["gated_value_precertified"]
    assert live_citation["behavior_scope"] == GM_BEHAVIOR_SCOPE_VERSION  # sanity: starts VALID
    scope_mismatched = {**live_citation, "behavior_scope": GM_BEHAVIOR_SCOPE_VERSION + 1}
    mismatched_band = dataclasses.replace(
        band, params={**band.params, "gated_value_precertified": scope_mismatched}
    )
    mismatched_bands = dataclasses.replace(
        _BANDS, metrics={**_BANDS.metrics, "M16": mismatched_band}
    )

    runs = _m16_multi_run_fixture([90, 92, 94], [50, 55, 60])
    result = _compute_m16(runs, mismatched_bands)
    assert result.detail["value_source"] == "this_run"
    assert result.detail["gated_value_precert_stale"] is True

    finalized = _finalize_metric(
        mismatched_band, result, gm_tuning_version=mismatched_bands.gm_tuning_version
    )
    assert f"behavior_scope {GM_BEHAVIOR_SCOPE_VERSION + 1}" in finalized.status
    assert f"live scope {GM_BEHAVIOR_SCOPE_VERSION}" in finalized.status


def test_family3_compute_m14_site_trips_on_scope_missing():
    """Site test 2 of 2 (Decision P4-74 part 4): `compute_m14`'s own
    separate hookup (`:1476`'s `extra_pass`/`:1515`'s value citation) both
    call the shared predicate -- a citation with a matching `gm_tuning_
    version` but no `behavior_scope` key at all must read STALE at both,
    exactly the state every live citation was actually in before Decision
    P4-42's re-precertification (Decision P4-62's own finding). This is
    the site a version-only predicate would get wrong twice over: once
    for `extra_pass` (M14's own top-level GO), once for `.value`."""
    band14 = _BANDS.metrics["M14"]
    live_se_precert = band14.params["se_precertified"]
    live_value_citation = band14.params["gated_value_precertified"]
    assert live_se_precert["behavior_scope"] == GM_BEHAVIOR_SCOPE_VERSION  # sanity
    scope_missing_se_precert = {
        k: v for k, v in live_se_precert.items() if k != "behavior_scope"
    }
    scope_missing_value_citation = {
        k: v for k, v in live_value_citation.items() if k != "behavior_scope"
    }
    scope_missing_band14 = dataclasses.replace(
        band14,
        params={
            **band14.params,
            "se_precertified": scope_missing_se_precert,
            "gated_value_precertified": scope_missing_value_citation,
        },
    )
    scope_missing_bands = dataclasses.replace(
        _BANDS, metrics={**_BANDS.metrics, "M14": scope_missing_band14}
    )

    run = _m14_high_se_run()
    result = _compute_m14((run,), scope_missing_bands)
    assert result.detail["precertification_stale"] is True  # :1476's own extra_pass conjunct
    assert result.extra_pass is False  # falls back to the live measurability check
    assert result.detail["value_source"] == "this_run"  # :1515's own value citation
    assert result.detail["gated_value_precert_stale"] is True

    finalized = _finalize_metric(
        scope_missing_band14, result, gm_tuning_version=scope_missing_bands.gm_tuning_version
    )
    assert "citation carries no behavior_scope -- fail-closed per P4-46" in finalized.status
    assert finalized.status.count("citation carries no behavior_scope") == 2  # both fragments


def test_real_family3_citations_read_valid_after_the_p4_42_reprecertification():
    """Decision P4-42's own re-precertification (2026-09-13, against
    artifacts/validation_gm/m14_seeds_v4_phase4_agency under real
    agency_enabled=True behavior) landed real `behavior_scope=2` values
    in `seeds/gm_validation_bands.json` -- superseding this test's own
    prior form (`test_real_family3_citations_read_stale_for_the_scope_
    missing_reason_today`), which asserted the disclosed, temporary
    SCOPE_MISSING state Decision P4-62 predicted would hold only "today"
    (i.e. before this re-precertification ran). Against the genuinely
    re-precertified `seeds/gm_validation_bands.json` (no in-memory
    stamping -- a fresh `load_bands()`), every live Family-3 citation now
    reads VALID -- the real, information-bearing confirmation Decision
    P4-62 said would only be possible after this round."""
    real_bands = load_bands()  # NOT _BANDS -- the real, re-precertified file
    for metric_id in ("M11", "M12", "M13", "M15", "M16"):
        citation = real_bands.metrics[metric_id].params["gated_value_precertified"]
        assert citation["behavior_scope"] == 2
        status = _family3_citation_status(
            citation, gm_tuning_version=real_bands.gm_tuning_version
        )
        assert status is Family3CitationStatus.VALID

    se_precert = real_bands.metrics["M14"].params["se_precertified"]
    value_citation = real_bands.metrics["M14"].params["gated_value_precertified"]
    assert se_precert["behavior_scope"] == 2
    assert value_citation["behavior_scope"] == 2
    assert _family3_citation_status(
        se_precert, gm_tuning_version=real_bands.gm_tuning_version
    ) is Family3CitationStatus.VALID
    assert _family3_citation_status(
        value_citation, gm_tuning_version=real_bands.gm_tuning_version
    ) is Family3CitationStatus.VALID


def test_compute_m14_against_the_real_reprecertified_bands_file_trusts_the_precert():
    """Nit 7 (critic), updated after Decision P4-42's real re-precertification
    landed (2026-09-13): almost every assertion in this file runs against
    `_BANDS`/`_BANDS0`, the IN-MEMORY fixture stamped with `behavior_
    scope` -- only the predicate-level test above asserts against the
    real, on-disk file. This test asserts the actual METRIC-level
    consequence against `load_bands()` directly (the same file B5 was
    originally found against, now genuinely re-precertified rather than
    in-memory-stamped): `compute_m14`'s `extra_pass` now reads `True`
    unconditionally from the valid `se_precertified` citation (Decision
    P2-35's own contract), the opposite of this test's prior form, which
    demonstrated the fall-back-to-measurability path while every real
    citation was still SCOPE_MISSING (Decision P4-73)."""
    real_bands = load_bands()
    boundaries = _full_boundaries((1, 2, 3, 4))
    idx = _ALL_LABELS.index("post_waivers")
    overalls_by_team = {
        1: tuple(range(99, 99 - 15, -1)),
        2: tuple(range(90, 90 - 15, -1)),
        3: tuple(range(80, 80 - 15, -1)),
        4: tuple(range(70, 70 - 15, -1)),
    }
    boundaries[idx] = _boundary("post_waivers", overalls_by_team=overalls_by_team)
    run = _run_record(
        1, [_season_record(2026, boundaries)],
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.STAR_MAXER,
                             3: GmPhilosophy.EGALITARIAN, 4: GmPhilosophy.EGALITARIAN},
        season1_baseline={1: 2.0, 2: 4.0, 3: 6.0, 4: 8.0},
    )
    result = _compute_m14((run,), real_bands)
    assert result.detail["precertification_stale"] is False
    assert result.detail["measurability_source"] == "precertified"
    # The load-bearing chain B5 names, now exercised the other way: precert
    # VALID -> extra_pass is unconditionally `True` from se_precertified,
    # regardless of what the live reading on this small, adversarial
    # fixture would have said on its own (it fails measurability here,
    # se_measured=1.41 > se_threshold=0.75 -- a valid precert overrides
    # that live reading, exactly as Decision P2-35 specifies).
    assert result.extra_pass is True
    assert result.detail["live_reading"]["measurability_pass"] is False
    assert result.extra_pass != result.detail["live_reading"]["measurability_pass"]


# ---------------------------------------------------------------------------
# Decision P4-35 (Phase 4 Stage C step 14) -- the agency metric family.
# Both `compute_m31`/`compute_m32` `SKIP` (Decision P4-34) on the zero-
# mechanism corpus (`agency_enabled=False`, `SeasonRecord`'s two agency
# fields both defaulting to `()`) -- verified below against hand-built
# fixtures, since the real corpus's own zero-mechanism reading was
# verified separately by executing `run_seed(..., agency_enabled=False)`
# directly (this stage's own build report has the real numbers).
# ---------------------------------------------------------------------------


def test_m31_skips_on_the_zero_mechanism_corpus():
    """Decision P4-34: `agency_enabled=False`'s own `SeasonRecord`s carry
    `agency_request_state_counts == ()` (the field's own default, never
    populated when the checkpoint never runs) -- `compute_m31` must `SKIP`,
    never fabricate a `0.0` rate that would read as "measured and found
    absent" rather than "never measured."""
    seasons = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(2)]
    run = _run_record(1, seasons)
    result = _compute_m31((run,), _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m31_measures_the_pooled_non_content_share():
    """A hand-built two-checkpoint season: 8 `content`, 2 `unsettled` at
    checkpoint 1 (2 non-content of 10); 7 `content`, 2 `unsettled`, 1
    `holdout` at checkpoint 2 (3 non-content of 10). Pooled: 5 non-content
    of 20 total observations = 0.25, `extension_demanded` (absent from
    either checkpoint here) correctly contributing 0."""
    seasons = [
        _season_record(
            2026, _full_boundaries((1, 2)),
            agency_request_state_counts=(
                {"content": 8, "unsettled": 2},
                {"content": 7, "unsettled": 2, "holdout": 1},
            ),
        )
    ]
    run = _run_record(1, seasons)
    result = _compute_m31((run,), _BANDS0)
    assert result.value == pytest.approx(0.25)
    assert result.detail == {"total_observations": 20, "bound_or_above": 5}


def test_m31_pools_across_multiple_runs_and_seasons():
    """Two runs, two seasons each -- confirms the pool is a real sum
    across every `(run, season, checkpoint)` triple, not just the first
    one found."""
    run_a = _run_record(
        1,
        [
            _season_record(
                2026, _full_boundaries((1, 2)),
                agency_request_state_counts=({"content": 10}, {"content": 9, "unsettled": 1}),
            ),
            _season_record(
                2027, _full_boundaries((1, 2)),
                agency_request_state_counts=({"content": 9, "unsettled": 1}, {"content": 10}),
            ),
        ],
    )
    run_b = _run_record(
        2,
        [
            _season_record(
                2026, _full_boundaries((1, 2)),
                agency_request_state_counts=({"content": 10}, {"content": 10}),
            ),
        ],
    )
    result = _compute_m31((run_a, run_b), _BANDS0)
    assert result.detail == {"total_observations": 60, "bound_or_above": 2}
    assert result.value == pytest.approx(2 / 60)


def test_m32_skips_on_the_zero_mechanism_corpus():
    """Decision P4-34: `agency_enabled=False`'s own `SeasonRecord`s carry
    `agency_cumulative_transition_count_by_player == ()` -- `compute_m32`
    must `SKIP`, never fabricate a `0.0` churn reading."""
    seasons = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(2)]
    run = _run_record(1, seasons)
    result = _compute_m32((run,), _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m32_reads_the_max_over_the_last_seasons_own_cumulative_snapshot():
    """`agency_cumulative_transition_count_by_player` is CUMULATIVE for
    the whole run (per that field's own docstring) -- `compute_m32` must
    read only the LAST season's snapshot (season 2's `5`, the real
    whole-run total for player 101), never season 1's own smaller
    snapshot (`2`), and never sum the two together (which would double-
    count and read `7`)."""
    seasons = [
        _season_record(
            2026, _full_boundaries((1, 2)),
            agency_cumulative_transition_count_by_player=((101, 2), (102, 1)),
        ),
        _season_record(
            2027, _full_boundaries((1, 2)),
            agency_cumulative_transition_count_by_player=((101, 5), (102, 1)),
        ),
    ]
    run = _run_record(1, seasons)
    result = _compute_m32((run,), _BANDS0)
    assert result.value == pytest.approx(5.0)
    assert result.detail == {"max_transitions": 5}


def test_m32_takes_the_max_across_multiple_runs_last_seasons():
    """Two runs, each one season -- the max across BOTH runs' own last
    (only) season, not just the first run found."""
    run_a = _run_record(
        1,
        [_season_record(
            2026, _full_boundaries((1, 2)),
            agency_cumulative_transition_count_by_player=((101, 3),),
        )],
    )
    run_b = _run_record(
        2,
        [_season_record(
            2026, _full_boundaries((1, 2)),
            agency_cumulative_transition_count_by_player=((201, 9),),
        )],
    )
    result = _compute_m32((run_a, run_b), _BANDS0)
    assert result.value == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# Decision P4-94 (Stage D step 20) -- the media metric family, M33-M36.
# `compute_m33`-`compute_m36` never take `runs`, only a precomputed `Media
# CorpusTotals` (the "fourth instance" `compute_all`'s own docstring
# discloses) -- so every fixture below builds one by hand rather than
# reusing `_run_record`/`_season_record`.
# ---------------------------------------------------------------------------


def _split(stories: int, fallback_stories: int) -> KindOrVoiceSplit:
    return KindOrVoiceSplit(stories=stories, fallback_stories=fallback_stories)


def _depth_bin(
    selections: int = 0, fallback_selections: int = 0,
    ungated_kind_selections: int = 0, ungated_kind_fallback_selections: int = 0,
    trigger_held_selections: int | None = None, trigger_held_fallback_selections: int | None = None,
) -> PairDepthBin:
    """Decision P4-107 item 6: `trigger_held_selections`/`trigger_held_
    fallback_selections` default to whatever `ungated_kind_selections`/
    `ungated_kind_fallback_selections` read -- every existing caller of
    this fixture builder is describing an ungated-kind-only bin, where
    the trigger holds vacuously for every one of those selections and
    `trigger_held_*` is therefore identical to `ungated_kind_*` by
    construction (`PairDepthBin`'s own docstring: `ungated_kind_
    selections` is always a subset of `trigger_held_selections`, and here
    it is the whole of it). A test that needs the two to genuinely differ
    (a gated kind with some of its own trigger-held selections) passes
    both explicitly."""
    return PairDepthBin(
        selections=selections, fallback_selections=fallback_selections,
        ungated_kind_selections=ungated_kind_selections,
        ungated_kind_fallback_selections=ungated_kind_fallback_selections,
        trigger_held_selections=(
            ungated_kind_selections if trigger_held_selections is None else trigger_held_selections
        ),
        trigger_held_fallback_selections=(
            ungated_kind_fallback_selections
            if trigger_held_fallback_selections is None
            else trigger_held_fallback_selections
        ),
    )


def _media_arm(
    *, stories_total: int = 0, fallback_stories: int = 0,
    by_kind=None, by_voice=None, by_voice_kind_template=None, by_pair_depth=None, cycles=(),
) -> MediaCorpusArm:
    return MediaCorpusArm(
        stories_total=stories_total, fallback_stories=fallback_stories,
        by_kind=by_kind or {}, by_voice=by_voice or {},
        by_voice_kind_template=by_voice_kind_template or {},
        by_pair_depth=by_pair_depth or {}, cycles=tuple(cycles),
    )


def _media_corpus(
    *, live: MediaCorpusArm, control: MediaCorpusArm | None = None,
    ungated_kinds: tuple[str, ...] = (),
    declared_fallback_weight_share: float = 1 / 3,
    top_k_per_cycle: int = 12, league_rng_seed: int = 1,
) -> MediaCorpusTotals:
    return MediaCorpusTotals(
        schema_version=1, library_sha="test-sha", library_path="seeds/media_phrases.json",
        league_rng_seed=league_rng_seed, seed_root="test:1", leagues=1, seasons=1,
        generated_at="2026-01-01T00:00:00Z", top_k_per_cycle=top_k_per_cycle,
        salience_floor=0.3, repeat_penalty=0.3,
        declared_fallback_weight_share=declared_fallback_weight_share,
        persona_exclusivity_floor=0.5, ungated_kinds=ungated_kinds,
        live=live, control=control,
    )


def test_pair_depth_bin_round_trips_the_new_trigger_held_fields():
    """Decision P4-107 item 6 (P4-111) -- `trigger_held_selections`/
    `trigger_held_fallback_selections` survive a real `to_json`/`from_json`
    round trip, additively alongside the four pre-existing fields."""
    bin_ = PairDepthBin(
        selections=10, fallback_selections=4,
        ungated_kind_selections=3, ungated_kind_fallback_selections=1,
        trigger_held_selections=7, trigger_held_fallback_selections=2,
    )
    payload = bin_.to_json()
    assert payload["trigger_held_selections"] == 7
    assert payload["trigger_held_fallback_selections"] == 2
    assert PairDepthBin.from_json(payload) == bin_


def test_pair_depth_bin_from_json_defaults_trigger_held_fields_to_zero_on_old_artifacts():
    """An artifact written before Decision P4-107 item 6 has no `trigger_
    held_*` keys at all -- `from_json` must default both to `0`, a real
    "not measured under this schema version" reading, never a `KeyError`."""
    old_payload = {
        "selections": 5, "fallback_selections": 2,
        "ungated_kind_selections": 5, "ungated_kind_fallback_selections": 2,
    }
    bin_ = PairDepthBin.from_json(old_payload)
    assert bin_.trigger_held_selections == 0
    assert bin_.trigger_held_fallback_selections == 0


def test_m33_skips_on_the_zero_mechanism_corpus():
    """Decision P4-34: `media_corpus is None` -- `compute_m33` must `SKIP`,
    never fabricate a fallback-share reading."""
    result = _compute_m33(None, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m33_skips_when_the_live_arm_has_no_stories():
    corpus = _media_corpus(live=_media_arm(stories_total=0, fallback_stories=0))
    result = _compute_m33(corpus, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m33_measures_b_directly_when_ungated_kinds_have_real_selections():
    """`milestone` is ungated (m=0 by construction) and has real selections
    here, so `b_provenance` must read `"measured"` and `b` must be the
    DIRECT pooled fallback share among `ungated_kinds` (4/10 = 0.4), never
    the depth-reweighted closed form. `m` collapses to a point (no
    bracket) exactly because `b_provenance == "measured"`."""
    corpus = _media_corpus(
        live=_media_arm(
            stories_total=20, fallback_stories=12,
            by_kind={"milestone": _split(10, 4), "streak": _split(10, 8)},
            by_voice_kind_template={
                "analyst": {"milestone": {"analyst_milestone": 6, "fallback_milestone": 4}},
                "historian": {"streak": {"fallback_streak": 8}},
                "beat_writer": {"streak": {"beat_writer_streak_win": 2}},
            },
            by_pair_depth={"0": _depth_bin(20, 12, 10, 4)},
        ),
        ungated_kinds=("milestone",),
    )
    result = _compute_m33(corpus, _BANDS0)
    assert result.value == pytest.approx(0.6)
    assert result.detail["b_provenance"] == "measured"
    assert result.detail["b"] == pytest.approx(0.4)
    assert result.detail["m"] == pytest.approx((0.6 - 0.4) / (1 - 0.4))
    assert result.detail["m_bracket"] is None
    # historian/streak has no `historian_...` key at all (only the
    # fallback) -- zero dedicated selections, so its whole bucket (8)
    # counts toward the numerator; beat_writer/streak and analyst/
    # milestone both drew their own dedicated template at least once, so
    # neither contributes. 8 / 20 = 0.4.
    assert result.detail["m_lower_bound_direct"] == pytest.approx(0.4)


def test_m33_reports_modeled_b_and_a_bracket_when_ungated_kinds_are_unfed():
    """Decision P4-104 -- when every `by_pair_depth` bin restricted to
    `ungated_kinds` reads zero (the committed corpus's own real state),
    `b_provenance` must read `"modeled"`, `b` must be the depth-reweighted
    closed form (never the raw `declared_fallback_weight_share`), and `m`
    must report as a bracket -- `detail["m"]` stays `None`."""
    corpus = _media_corpus(
        live=_media_arm(
            stories_total=10, fallback_stories=5,
            by_kind={"streak": _split(10, 5)},
            by_pair_depth={
                "0": _depth_bin(selections=6, fallback_selections=2),
                "1": _depth_bin(selections=4, fallback_selections=3),
            },
        ),
        ungated_kinds=("milestone",),  # present in the schema, absent from by_kind/by_pair_depth
        declared_fallback_weight_share=1 / 3,
    )
    result = _compute_m33(corpus, _BANDS0)
    assert result.value == pytest.approx(0.5)
    assert result.detail["b_provenance"] == "modeled"
    b_corpus = (6 * (1 / 3) + 4 * (1 / 1.6)) / 10
    assert result.detail["b"] == pytest.approx(b_corpus)
    assert result.detail["m"] is None
    m_declared = (0.5 - 1 / 3) / (1 - 1 / 3)
    m_corpus = (0.5 - b_corpus) / (1 - b_corpus)
    assert result.detail["m_bracket"] == pytest.approx(sorted([m_declared, m_corpus]))


def test_m33_rejects_a_corpus_whose_two_ungated_poolings_disagree():
    """Decision P4-103's own free cross-check: `by_kind` restricted to
    `ungated_kinds` and `by_pair_depth`'s own ungated columns pool the
    SAME selections two independent ways -- a mismatch means the
    artifact's two poolings have drifted apart, and the corpus must be
    REJECTED (a loud exception), never silently read."""
    corpus = _media_corpus(
        live=_media_arm(
            stories_total=10, fallback_stories=4,
            by_kind={"milestone": _split(10, 4)},
            by_pair_depth={"0": _depth_bin(10, 4, ungated_kind_selections=9,
                                            ungated_kind_fallback_selections=4)},
        ),
        ungated_kinds=("milestone",),
    )
    with pytest.raises(ValueError, match="rejected"):
        _compute_m33(corpus, _BANDS0)


def test_m33_matches_decision_p4_123s_predicted_reading_on_the_real_corpus():
    """**Superseded again by the 2026-09-23 critic-round fix's own real corpus
    regeneration** (docs/design/media-v2-newsroom.md, As-built note "2026-09-23:
    step-6 critic-round fixes, round 2") -- every number below is against the
    corpus THIS round produced, fixing the prior round's own real bug: a single
    `subject_last_seen` map (advanced from the live arm's publications only) and a
    single `profile_candidates` tuple, both composed once per cycle and handed
    UNCHANGED to both arms (MV-5 revision 6), not the two independently-tracked
    per-arm values the prior round shipped. A fresh corpus run necessarily creates
    a fresh league and reseeds every RNG stream that salts on database-assigned
    serial ids (the pre-existing "corpus-staleness regression" limitation), so
    these numbers move for that reason alone even before counting this round's own
    real behavior change.

    Real reading: `212/405 = 0.52346`, PASS against the `0.65` structural
    ceiling. `player_unrest` is still by far the largest single driver:
    `80/80 = 1.0` fallback share, the highest of any kind, for the identical
    reason as before (no dedicated ungated template of its own; a real
    motivation event misses its gated dedicated templates' triggers often).
    `insider` is now the single highest-fallback VOICE at `1.0` (`61/61`,
    previously `player_unrest`'s own historical share on this metric was
    highest among KINDS, not voices); `hot_take` is second among voices at
    `0.60345`.

    `b_provenance` is still `"partial"` (not `"measured"`) -- `by_pair_
    depth` still carries real bins with `trigger_held_selections == 0`, so
    `modeled_weight` is non-zero (`b_modeled_weight_share=0.03457`) and `m`
    still reads as a bracket (`m_bracket=[0.25525, 0.28519]`), never a point
    value, with `m_lower_bound_direct` at `0.60247`."""
    path = Path(__file__).resolve().parents[4] / "artifacts" / "media_corpus.json"
    corpus = MediaCorpusTotals.from_json(json.loads(path.read_text(encoding="utf-8")))
    result = _compute_m33(corpus, _BANDS0)
    assert result.value == pytest.approx(212 / 405, abs=1e-9)
    assert result.detail["b_provenance"] == "partial"
    assert result.detail["m"] is None
    assert result.detail["m_bracket"] == pytest.approx(
        [0.2552455148442815, 0.2851851851851852], abs=1e-9
    )
    assert result.detail["m_lower_bound_direct"] == pytest.approx(0.6024691358024692, abs=1e-9)
    assert evaluate(_BANDS0.metrics["M33"], result.value) is True  # PASS, more comfortably


def test_m34_skips_on_the_zero_mechanism_corpus():
    """Decision P4-34: `media_corpus is None` -- `compute_m34` must `SKIP`,
    never fabricate a paired-difference reading."""
    result = _compute_m34(None, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m34_hard_skips_when_the_control_arm_is_none():
    """Decision P4-97: an unpaired `tv_live` would silently resurrect the
    exact contamination the paired form exists to remove -- mirroring
    `compute_m27`'s identical `control_by_philosophy_template is None`
    rule. Never `counted_in_go_override`d; a real hard `SKIP`."""
    corpus = _media_corpus(live=_media_arm(stories_total=10, fallback_stories=5), control=None)
    result = _compute_m34(corpus, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None
    assert result.counted_in_go_override is None


def test_m34_hard_skips_when_a_cohort_reads_zero_restricted_selections():
    corpus = _media_corpus(
        live=_media_arm(stories_total=0, fallback_stories=0),
        control=_media_arm(stories_total=0, fallback_stories=0),
    )
    result = _compute_m34(corpus, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m34_reports_uncounted_real_value_when_gates_fail_never_skip():
    """Decision P4-105: while gate 2 or gate 3 fails, M34 must report its
    REAL computed value, `counted_in_go_override=False`, and name the
    failing gates -- never `SKIP` (a `SKIP` here would be indistinguishable
    from the media-off `SKIP`, destroying real information)."""
    corpus = _media_corpus(
        live=_media_arm(
            stories_total=23, fallback_stories=23,
            by_voice_kind_template={
                "analyst": {"streak": {"fallback_streak": 13}},
                "historian": {"streak": {"fallback_streak": 10}},
            },
        ),
        control=_media_arm(
            stories_total=23, fallback_stories=22,
            by_voice_kind_template={
                "analyst": {"streak": {"beat_writer_streak_win": 1, "fallback_streak": 12}},
                "historian": {"streak": {"fallback_streak": 10}},
            },
        ),
    )
    result = _compute_m34(corpus, _BANDS0)
    assert result.value == pytest.approx(-1 / 13)
    assert result.skip_reason is None
    assert result.counted_in_go_override is False
    blockers = result.detail["counted_in_go_blockers"]
    assert any(b.startswith("gate 2") for b in blockers)
    assert any(b.startswith("gate 3") for b in blockers)


def test_m34_treats_a_k_less_than_2_reading_as_a_gate_failure_never_unhandled():
    """Decision P4-105/P4-106 item 15 (corrected round 20), extended by
    Decision P4-108: a reading whose own restricted, pooled counts carry
    `k < 2` distinct templates -- no split-half variance to measure -- is
    a gate FAILURE regardless of what its (still-computed, still-reported)
    null value happens to be, exactly like a numeric reading above
    threshold. Gates 2/3 are built to PASS here (K_live=9 in the LIVE arm,
    all three kinds covered, >=2 dedicated templates each) specifically so
    this test isolates gate 1 alone -- if gate 1's `k < 2` handling
    regressed to "not a blocker," this test would see `counted_in_go_
    override=True` and the `blockers` list would still be empty.

    **Decision P4-108's own free correctness check** (`n_live == n_
    control` per cohort, since the same persona is drawn in both arms by
    construction) means the CONTROL arm here cannot simply be starved down
    to a single selection the way the pre-P4-108 fixture did -- that would
    make `n_control` disagree with `n_live`, which is itself now a
    detected bug, not a legitimate corpus shape. Instead the control
    arm's totals are matched to the live arm's own (50/44), with every
    selection for a cohort landing on ONE template (`k=1`) rather than
    spread across the live arm's six -- a corpus where a channel that
    varies its templates when live can collapse onto a single template
    once voices are erased, exactly the kind of thing `with_voices_
    disabled` can produce."""
    live = _media_arm(
        stories_total=54, fallback_stories=20,
        by_voice_kind_template={
            "analyst": {
                "streak": {"analyst_streak_slide": 12, "fallback_streak": 8},
                "milestone": {"analyst_milestone": 10, "fallback_milestone": 6},
                "rookie_watch": {"analyst_rookie_watch": 9, "fallback_rookie_watch": 5},
            },
            "historian": {
                "streak": {"historian_streak_notable": 11, "fallback_streak": 7},
                "milestone": {"historian_milestone": 9, "fallback_milestone": 5},
                "rookie_watch": {"historian_rookie_watch": 8, "fallback_rookie_watch": 4},
            },
        },
    )
    control = _media_arm(
        stories_total=94,
        by_voice_kind_template={
            # Same totals as the live arm's own analyst (50) and historian
            # (44), each collapsed onto ONE template -- `k=1` for both,
            # a gate FAILURE regardless of the null value it computes.
            "analyst": {"streak": {"fallback_streak": 50}},
            "historian": {"streak": {"fallback_streak": 44}},
        },
    )
    corpus = _media_corpus(live=live, control=control)
    result = _compute_m34(corpus, _BANDS0)
    control_analyst = result.detail["null_check"]["readings"]["control"]["analyst"]
    control_historian = result.detail["null_check"]["readings"]["control"]["historian"]
    assert control_analyst["n"] == 50
    assert control_analyst["k"] == 1
    assert control_analyst["failed"] is True
    assert control_historian["n"] == 44
    assert control_historian["k"] == 1
    assert control_historian["failed"] is True
    assert result.counted_in_go_override is False
    blockers = result.detail["counted_in_go_blockers"]
    assert any(b.startswith("gate 1") for b in blockers)
    # And gates 2/3 must NOT be why it failed, isolating gate 1 alone.
    assert not any(b.startswith("gate 2") for b in blockers)
    assert not any(b.startswith("gate 3") for b in blockers)


def test_m34_null_gate_treats_a_none_null_reading_as_a_gate_failure_never_unhandled():
    """BLOCKING 2 (round-22 fix dispatch, re-verification of eb519b1): the
    `elif null is None:` branch at `metrics.py`'s own gate-1 dispatch
    (Decision P4-106 item 15 -- "the single most load-bearing correctness
    detail in the whole family," per the design doc) had ZERO test
    coverage after the prior fix round's own `k < 2` test stopped reaching
    it (that test's fixture used a count of exactly 1, which now
    short-circuits on the newer `k < 2` check before `_within_cohort_
    binomial_thin_tv` is ever called). Proven via mutation testing before
    this test was written: changing `elif null is None:` to `elif False:`
    at `metrics.py:3278` left the entire targeted suite green.

    This fixture reaches the `None` path specifically (`k=2`, not `k<2`,
    so gate 1's FIRST branch cannot fire) -- two distinct templates, one
    selection each, restricted-kind pool `{t1: 1, t2: 1}`. At `seed=0`
    (`m34_null_check_readings`'s own `rng_for(seed, "validate_gm",
    "m34_within_cohort_split", <arm>, <cohort>)` salt), both of the LIVE
    arm's readings independently draw a coin flip that puts BOTH
    selections in the same half -- `_within_cohort_binomial_thin_tv`'s own
    empty-half degenerate-split guard, returning `null=None` -- confirmed
    by direct execution against the real function, not a hand-picked
    assertion about what a seed \"should\" do. Both `live` readings must
    read `failed=True` with a `fail_reason` naming the null-is-None case
    specifically (not `k=2 < 2`, which cannot fire here, and not the
    threshold case, which is `control`'s own separate outcome at this same
    seed -- asserted below purely to document the fixture's real full
    output, not because gate 1's `> escalation_threshold` branch is this
    test's target)."""
    restricted_kinds = frozenset({"streak", "milestone", "rookie_watch"})
    by_voice_kind_template = {
        "analyst": {"streak": {"t1": 1, "t2": 1}},
        "historian": {"streak": {"t1": 1, "t2": 1}},
    }
    readings = _m34_null_check_readings(
        by_voice_kind_template, by_voice_kind_template,
        cohort_a="analyst", cohort_b="historian", restrict_to_kinds=restricted_kinds,
        seed=0, escalation_threshold=0.0625,
    )
    for cohort in ("analyst", "historian"):
        live_reading = readings["live"][cohort]
        assert live_reading.k == 2, "fixture must reach k=2, never the separate k<2 branch"
        assert live_reading.null is None, (
            "fixture assumption drifted -- this test's seed/counts no longer reach the real "
            "empty-half degenerate split this branch exists to catch"
        )
        assert live_reading.failed is True
        assert live_reading.fail_reason is not None
        assert "null" in live_reading.fail_reason and "None" in live_reading.fail_reason
        assert "k=" not in live_reading.fail_reason, (
            "must not be misattributed to the k<2 branch, a different, already-tested case"
        )
    # The control arm's own real outcome at this same seed/fixture (null=1.0,
    # above threshold) -- documented so a future reader trusts this fixture's
    # full output, not evidence this test targets the threshold branch.
    for cohort in ("analyst", "historian"):
        control_reading = readings["control"][cohort]
        assert control_reading.null == pytest.approx(1.0)
        assert control_reading.failed is True


def test_m34_null_gate_reads_all_four_arm_by_cohort_readings():
    """Decision P4-108: gate 1 measures FOUR readings (2 cohorts x 2
    arms), each with its OWN `k`/`n`/`null`/`failed` -- never a single
    control-only pair. `detail["null_check"]["readings"]` must carry all
    four, each labeled with its own arm's real `k`, never a `k`/`K`
    borrowed from a different reading (the exact bug Decision P4-108
    fixed at `detail["null_check"]`'s own site)."""
    live = _media_arm(
        stories_total=38, fallback_stories=15,
        by_voice_kind_template={
            "analyst": {"streak": {"analyst_streak_slide": 12, "fallback_streak": 8}},
            "historian": {"streak": {"historian_streak_notable": 11, "fallback_streak": 7}},
        },
    )
    # Decision P4-108's own free correctness check (n_live == n_control per
    # cohort) -- the control arm's totals are matched to the live arm's own
    # (20/18), each collapsed onto ONE template (k=1) rather than spread
    # across live's two, so the K distinction below is real and not merely
    # a total-count mismatch the paired-design assertion would now catch.
    control = _media_arm(
        stories_total=38,
        by_voice_kind_template={
            "analyst": {"streak": {"fallback_streak": 20}},
            "historian": {"streak": {"fallback_streak": 18}},
        },
    )
    corpus = _media_corpus(live=live, control=control)
    result = _compute_m34(corpus, _BANDS0)
    readings = result.detail["null_check"]["readings"]
    assert set(readings) == {"live", "control"}
    assert set(readings["live"]) == {"analyst", "historian"}
    assert set(readings["control"]) == {"analyst", "historian"}
    # Live analyst/historian each have K=2 (one dedicated + one fallback
    # template) -- distinct from the control arm's K=1/K=1.
    assert readings["live"]["analyst"]["k"] == 2
    assert readings["live"]["historian"]["k"] == 2
    assert readings["control"]["analyst"]["k"] == 1
    assert readings["control"]["historian"]["k"] == 1
    # A K<2 reading fails regardless of its (still-reported) null value.
    assert readings["control"]["analyst"]["failed"] is True
    assert readings["control"]["historian"]["failed"] is True
    assert result.counted_in_go_override is False


def test_m34_counts_when_all_three_gates_pass():
    """The mirror image of the previous two tests: real, sufficiently rich
    diversity/coverage/null readings must flip `counted_in_go_override` to
    `True`. The escalation threshold is widened to `0.99` for THIS fixture
    only (via a local `dataclasses.replace`, never touching the real
    `0.0625`) so the test is not a coin-flip on which RNG draw a small
    binomial split happens to land on -- gates 2 and 3 (the structural
    ones this test actually targets) are exercised at their real,
    unmodified thresholds."""
    live = _media_arm(
        stories_total=54, fallback_stories=20,
        by_voice_kind_template={
            "analyst": {
                "streak": {"analyst_streak_slide": 12, "fallback_streak": 8},
                "milestone": {"analyst_milestone": 10, "fallback_milestone": 6},
                "rookie_watch": {"analyst_rookie_watch": 9, "fallback_rookie_watch": 5},
            },
            "historian": {
                "streak": {"historian_streak_notable": 11, "fallback_streak": 7},
                "milestone": {"historian_milestone": 9, "fallback_milestone": 5},
                "rookie_watch": {"historian_rookie_watch": 8, "fallback_rookie_watch": 4},
            },
        },
    )
    control = _media_arm(
        stories_total=54,
        by_voice_kind_template={
            "analyst": {
                "streak": {"analyst_streak_slide": 12, "fallback_streak": 8},
                "milestone": {"analyst_milestone": 10, "fallback_milestone": 6},
                "rookie_watch": {"analyst_rookie_watch": 9, "fallback_rookie_watch": 5},
            },
            "historian": {
                "streak": {"historian_streak_notable": 11, "fallback_streak": 7},
                "milestone": {"historian_milestone": 9, "fallback_milestone": 5},
                "rookie_watch": {"historian_rookie_watch": 8, "fallback_rookie_watch": 4},
            },
        },
    )
    corpus = _media_corpus(live=live, control=control)
    widened_band = dataclasses.replace(
        _BANDS0.metrics["M34"], params={"escalation_threshold": 0.99},
    )
    bands = dataclasses.replace(
        _BANDS0, metrics={**_BANDS0.metrics, "M34": widened_band},
    )
    result = _compute_m34(corpus, bands)
    assert result.skip_reason is None
    assert result.detail["counted_in_go_blockers"] == []
    assert result.counted_in_go_override is True
    assert result.detail["K_live"] == 9
    assert result.detail["kinds_covered"] == ["milestone", "rookie_watch", "streak"]


def test_m34_gate2_requires_distinct_kinds_not_just_distinct_template_ids():
    """BLOCKING 1 (round-23 critic pass on Decision P4-110's own fix): the
    prior gate-2 test coverage never actually discriminated between the
    OLD rule (`len(dedicated_a/b)`, a count of distinct dedicated TEMPLATE
    IDS) and the NEW rule (`len(dedicated_kinds_a/b)`, a count of distinct
    dedicated KINDS) -- the committed corpus rejects gate 2 under EITHER
    rule, for an unrelated reason (`K_live=5 < 6`), so a critic proved the
    entire suite stays green with the fix reverted to the old rule.

    This fixture is built so `K_live=7 >= 6` (gate 2's OTHER sub-check
    independently PASSES, isolating the kind-breadth sub-check as this
    test's real target) AND each cohort supplies exactly 2 dedicated
    template ids -- `{cohort}_streak_slide`/`{cohort}_streak_surge`, a
    sign-split pair, Decision P4-96's own remedy shape -- but both land on
    the SAME single kind (`streak`). Under the OLD rule, `len(dedicated_a)
    == 2 >= _M34_MIN_DEDICATED_KINDS_PER_COHORT` PASSES; under the NEW,
    correct rule, `len(dedicated_kinds_a) == 1 < 2` FAILS, because only one
    of the three restricted kinds (`streak`) has a firing DEDICATED
    template -- `milestone`/`rookie_watch` read fallback-only in this
    fixture, by construction. Confirmed non-vacuous by hand: reverting
    `len(dedicated_kinds_a)`/`len(dedicated_kinds_b)` back to `len(
    dedicated_a)`/`len(dedicated_b)` at `compute_m34`'s own gate-2 check
    makes this test's `gate 2` assertion below fail (blocker list empty),
    restored afterward and confirmed passing again."""
    by_voice_kind_template = {
        "analyst": {
            "streak": {
                "analyst_streak_slide": 5, "analyst_streak_surge": 4,
                "fallback_streak": 3,
            },
            "milestone": {"fallback_milestone": 6},
            "rookie_watch": {"fallback_rookie_watch": 6},
        },
        "historian": {
            "streak": {
                "historian_streak_slide": 5, "historian_streak_surge": 4,
                "fallback_streak": 3,
            },
            "milestone": {"fallback_milestone": 6},
            "rookie_watch": {"fallback_rookie_watch": 6},
        },
    }
    live = _media_arm(
        stories_total=42, fallback_stories=18, by_voice_kind_template=by_voice_kind_template,
    )
    # The control arm mirrors the live arm exactly -- Decision P4-108's own
    # free correctness check (`n_live == n_control` per cohort) is trivially
    # satisfied, and this test's target (gate 2's kind-breadth sub-check
    # reads ONLY the live arm) is unaffected by what the control arm reads.
    control = _media_arm(
        stories_total=42, fallback_stories=18, by_voice_kind_template=by_voice_kind_template,
    )
    corpus = _media_corpus(live=live, control=control)
    # Widened past the real 0.0625 so gate 1 (the null gate, orthogonal to
    # this test's real target) does not add noise to the blocker list.
    widened_band = dataclasses.replace(
        _BANDS0.metrics["M34"], params={"escalation_threshold": 0.99},
    )
    bands = dataclasses.replace(_BANDS0, metrics={**_BANDS0.metrics, "M34": widened_band})
    result = _compute_m34(corpus, bands)
    assert result.detail["K_live"] == 7, "K_live's own sub-check must independently pass"
    assert result.detail["dedicated_templates_live"] == {
        "analyst": ["analyst_streak_slide", "analyst_streak_surge"],
        "historian": ["historian_streak_slide", "historian_streak_surge"],
    }, "the OLD rule's own count (2 dedicated template ids per cohort) must genuinely satisfy >= 2"
    assert result.detail["dedicated_kinds_live"] == {
        "analyst": ["streak"], "historian": ["streak"],
    }, "both dedicated template ids share the SAME kind -- zero real breadth"
    blockers = result.detail["counted_in_go_blockers"]
    assert any(b.startswith("gate 2") for b in blockers), (
        "gate 2 must FAIL under the current, correct kind-counting rule: each cohort reads "
        "only 1 distinct dedicated kind (< 2), even though it reads 2 distinct dedicated "
        "template ids (>= 2, which is what the OLD, hollow rule counted)"
    )
    assert not any(b.startswith("gate 3") for b in blockers), (
        "gate 3 (kind coverage) must independently pass -- all three kinds read a selection "
        "in both cohorts (fallback-only for milestone/rookie_watch) -- isolating gate 2 alone"
    )


def test_m34_reports_both_secondary_pairs_always_computed_never_pooled():
    corpus = _media_corpus(
        live=_media_arm(
            stories_total=23, fallback_stories=23,
            by_voice_kind_template={
                "analyst": {"streak": {"fallback_streak": 13}},
                "historian": {"streak": {"fallback_streak": 10}},
                "hot_take": {"player_unrest": {"fallback_player_unrest": 3}},
                "insider": {"player_unrest": {"fallback_player_unrest": 2}},
            },
        ),
        control=_media_arm(
            stories_total=23, fallback_stories=22,
            by_voice_kind_template={
                "analyst": {"streak": {"beat_writer_streak_win": 1, "fallback_streak": 12}},
                "historian": {"streak": {"fallback_streak": 10}},
                "hot_take": {"player_unrest": {"fallback_player_unrest": 3}},
                "insider": {"player_unrest": {"fallback_player_unrest": 2}},
            },
        ),
    )
    result = _compute_m34(corpus, _BANDS0)
    secondary = result.detail["secondary_pairs"]
    assert secondary["beat_writer_insider"]["paired_difference"] is None
    assert secondary["hot_take_insider"]["paired_difference"] == pytest.approx(0.0)


def test_m34_matches_the_real_corpus_after_the_2026_09_23_critic_round_fix():
    """**Root-caused and fixed, not merely re-measured.** The prior round's own
    `test_m34_raises_on_the_real_corpus_after_the_step_6_critic_round_fixes`
    (this test's predecessor) documented `compute_m34` raising `AssertionError`
    ("M34's paired design is broken") on every real corpus that round's generator
    could produce. Root cause, per a critic's trace: that round shipped TWO
    independently-tracked `subject_last_seen` maps (one per arm, each advanced
    from its own arm's publications) and gave the live arm alone a real
    `profile_candidates` load (control got `()`) -- both readings inverted what
    MV-5 revisions 4/5 actually required (MV-5 revision 6's own correction), and
    together they made the two arms' CANDIDATE SETS diverge on more than
    `with_voices_disabled`, breaking Decision P4-97 ground (ii) for real (a
    critic's reproduction: 0/54 cycles with divergent per-arm cycle records on
    the corpus generated before that bug, 54/54 after).

    This round's fix restores a single `subject_last_seen` map (advanced from the
    live arm's own publications only) and a single `profile_candidates` tuple,
    both computed once per cycle and handed UNCHANGED to both arms. Verified
    directly against this real, regenerated corpus: **0/54 cycles have divergent
    per-arm cycle records** (every one of `candidates_scored`/`candidates_above_
    floor`/`candidates_submitted`/`stories_persisted`/`capped_out`/`band_
    published`/`profiles_published` matches between the two arms, cycle for
    cycle) -- ground (ii) holds again, and `compute_m34` returns a real value
    instead of raising.

    **The null gate (gate 1) still fails, on all 4 arm x cohort readings** --
    `analyst`/`historian` each have too few restricted-kind (`streak`/
    `milestone`/`rookie_watch`) selections in a 6-season corpus for the
    within-cohort split to clear `escalation_threshold=0.0625` (`k` ranges
    6-11, `n` ranges 31-45, `null` ranges `0.29`-`0.43`, all well above
    threshold) -- a real, disclosed measurability limit of a 6-season corpus at
    this restriction, not a regression from this fix. `counted_in_go_override`
    is therefore `False`, and the real computed paired difference is reported
    UNCOUNTED, per Decision P4-105's own rule (never `SKIP` for a gate failure).
    Gates 2/3 (template diversity, kind coverage) both pass on their own:
    `K_live=10`, `analyst` covers `{milestone, streak}`, `historian` covers
    `{milestone, rookie_watch, streak}`, and all three restricted kinds are
    covered by both cohorts."""
    path = Path(__file__).resolve().parents[4] / "artifacts" / "media_corpus.json"
    corpus = MediaCorpusTotals.from_json(json.loads(path.read_text(encoding="utf-8")))

    live_cycles = corpus.live.cycles
    control_cycles = corpus.control.cycles
    assert len(live_cycles) == len(control_cycles) == 54
    diverging = [
        (lc, cc)
        for lc, cc in zip(live_cycles, control_cycles, strict=True)
        if (
            lc.candidates_scored, lc.candidates_above_floor, lc.candidates_submitted,
            lc.stories_persisted, lc.capped_out, lc.band_published, lc.profiles_published,
        ) != (
            cc.candidates_scored, cc.candidates_above_floor, cc.candidates_submitted,
            cc.stories_persisted, cc.capped_out, cc.band_published, cc.profiles_published,
        )
    ]
    assert diverging == []  # ground (ii) restored: 0/54, not 54/54

    result = _compute_m34(corpus, _BANDS0)
    assert result.value == pytest.approx(0.39713261648745524, abs=1e-9)
    assert result.detail["tv_live"] == pytest.approx(0.6222222222222222, abs=1e-9)
    assert result.detail["tv_control"] == pytest.approx(0.22508960573476702, abs=1e-9)
    assert result.detail["analyst_selections_live"] == 31
    assert result.detail["analyst_selections_control"] == 31
    assert result.detail["historian_selections_live"] == 45
    assert result.detail["historian_selections_control"] == 45
    assert result.detail["K_live"] == 10
    assert result.detail["kinds_covered"] == ["milestone", "rookie_watch", "streak"]
    assert result.counted_in_go_override is False
    assert len(result.detail["counted_in_go_blockers"]) == 1  # gate 1 only
    assert "gate 1 (null" in result.detail["counted_in_go_blockers"][0]
    assert evaluate(_BANDS0.metrics["M34"], result.value) is True  # PASS the band, uncounted


def test_m34_null_check_readings_are_invariant_to_within_kind_template_insertion_order():
    """Round-25 critic re-verification found the prior fix round's own
    `sorted()` fix (documented above, in the real-corpus M34 test right
    before this one -- renamed since, from Decision P4-117 to P4-123, as
    the committed artifact moved on) INCOMPLETE: it sorted the OUTER
    `restrict_to_kinds` loop in `_restricted_template_counts` but left the
    INNER `by_kind.get(kind, {}).items()` iteration in raw insertion
    order -- which is first-drawn order from `tools.generate_media_
    corpus._ArmAccumulator.record_stories`'s own `setdefault` calls, not
    template-id order, and nothing about it is sorted before `m34_null_
    check_readings`'s own `_report_null_check` in-memory call path (the
    live corpus-generation run itself, not the serialized artifact) reads
    it. Decision P4-118 fixes the inner loop for real (`sorted(by_kind.
    get(kind, {}).items())`, `metrics.py`).

    This test is the kind that would have caught the ORIGINAL bug, not
    just re-run the green fixture the prior round's own test committed:
    it builds two content-identical `by_voice_kind_template` mappings that
    differ ONLY in within-kind template insertion order (one reversed
    against the other) and asserts every one of `m34_null_check_readings`'s
    four readings (2 cohorts x 2 arms) is bit-identical between them.
    Manually confirmed to FAIL under a temporary revert of the inner
    `sorted(...)` (all four `null` values differ between the forward and
    reversed fixtures at this exact seed -- `live/analyst` reads
    `0.30303.../0.41538...`, `live/historian` `0.26667.../0.35`, `control/
    analyst` `0.26984.../0.30952...`, `control/historian` `0.55556.../
    0.72727...`) and to PASS under the real fix."""
    restricted_kinds = frozenset({"streak"})
    forward = {
        "analyst": {"streak": {"t1": 4, "t2": 7, "t3": 3, "t4": 9}},
        "historian": {"streak": {"h1": 5, "h2": 2, "h3": 6}},
    }
    reversed_insertion = {
        voice: {
            kind: dict(reversed(list(templates.items())))
            for kind, templates in by_kind.items()
        }
        for voice, by_kind in forward.items()
    }
    assert reversed_insertion == forward, "fixture must be content-identical, order aside"
    forward_readings = _m34_null_check_readings(
        forward, forward, cohort_a="analyst", cohort_b="historian",
        restrict_to_kinds=restricted_kinds, seed=424242, escalation_threshold=0.0625,
    )
    reversed_readings = _m34_null_check_readings(
        reversed_insertion, reversed_insertion, cohort_a="analyst", cohort_b="historian",
        restrict_to_kinds=restricted_kinds, seed=424242, escalation_threshold=0.0625,
    )
    for arm in ("live", "control"):
        for cohort in ("analyst", "historian"):
            forward_reading = forward_readings[arm][cohort]
            reversed_reading = reversed_readings[arm][cohort]
            assert forward_reading.null == reversed_reading.null, (
                f"{arm}/{cohort} null reading moved under within-kind insertion-order reversal "
                "-- the inner loop is not genuinely order-invariant"
            )
            assert forward_reading.k == reversed_reading.k
            assert forward_reading.n == reversed_reading.n
            assert forward_reading.failed == reversed_reading.failed


def test_m34_asserts_paired_n_live_equals_n_control_per_cohort():
    """Decision P4-108's free correctness check: P4-97 ground (ii)
    guarantees the same persona is drawn in both arms by construction, so
    `n_live == n_control` per cohort must hold. A synthetic fixture that
    breaks it (a real bug, not a corpus property) must raise loudly rather
    than silently mis-gate."""
    live = _media_arm(
        stories_total=13, fallback_stories=13,
        by_voice_kind_template={
            "analyst": {"streak": {"fallback_streak": 13}},
            "historian": {"streak": {"fallback_streak": 10}},
        },
    )
    control = _media_arm(
        stories_total=20, fallback_stories=20,
        by_voice_kind_template={
            # analyst's control total (20) deliberately does not match
            # the live arm's analyst total (13) -- the broken invariant.
            "analyst": {"streak": {"fallback_streak": 20}},
            "historian": {"streak": {"fallback_streak": 10}},
        },
    )
    corpus = _media_corpus(live=live, control=control)
    with pytest.raises(AssertionError, match="paired design is broken"):
        _compute_m34(corpus, _BANDS0)


def _cycle(
    phase: str, cycle_index: int, candidates_scored: int, candidates_above_floor: int,
    candidates_submitted: int, stories_persisted: int, *, league_id: int = 1, season: int = 2026,
    capped_out: int = 0,
) -> MediaCycleRecord:
    return MediaCycleRecord(
        league_id=league_id, season=season, phase=phase, cycle_index=cycle_index,
        candidates_scored=candidates_scored, candidates_above_floor=candidates_above_floor,
        candidates_submitted=candidates_submitted, stories_persisted=stories_persisted,
        capped_out=capped_out,
    )


def test_m35_skips_on_the_zero_mechanism_corpus():
    result = _compute_m35(None, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m35_skips_when_no_phase_meets_the_eligibility_guard():
    """Every phase persists fewer than `top_k_per_cycle` (12) stories
    corpus-wide -- no phase is eligible, so this must `SKIP`, never take a
    max over a 1-story denominator, matching `compute_m30`'s exact guard
    shape."""
    corpus = _media_corpus(
        live=_media_arm(cycles=[_cycle("FREE_AGENCY", 0, 20, 20, 5, 5)]),
        top_k_per_cycle=12,
    )
    result = _compute_m35(corpus, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m35_takes_the_max_over_eligible_phases_only():
    """FREE_AGENCY persists 12 (eligible, ratio 100/12); DRAFT persists 20
    (eligible, ratio 40/20 = 2.0); WAIVERS persists only 5 (ineligible --
    must be excluded from the max even though its own ratio, 30/5 = 6.0,
    would otherwise win)."""
    corpus = _media_corpus(
        live=_media_arm(
            cycles=[
                _cycle("FREE_AGENCY", 0, 100, 100, 12, 12),
                _cycle("DRAFT", 0, 40, 40, 20, 20),
                _cycle("WAIVERS", 0, 30, 30, 5, 5),
            ],
        ),
        top_k_per_cycle=12,
    )
    result = _compute_m35(corpus, _BANDS0)
    assert result.value == pytest.approx(100 / 12)
    assert result.detail["max_phase"] == "FREE_AGENCY"
    assert result.detail["per_phase"]["WAIVERS"]["eligible"] is False
    assert result.detail["zero_candidate_phase_count"] == 0


def test_m35_matches_decision_p4_123s_predicted_reading_on_the_real_corpus():
    """**Re-measured against the 2026-09-23 critic-round fix's own regenerated
    corpus** -- a fresh league plus this round's real behavior change (one
    shared `subject_last_seen` map and one shared `profile_candidates` tuple,
    MV-5 revision 6) both move every number here from the prior round's own
    `437/33 = 13.2424`, but the qualitative story is unchanged: real novelty
    tracking still lets a repeated/stale subject score lower on later
    appearances, which still keeps every phase's candidates-per-story ratio
    well clear of the `15.0` ceiling.

    Real reading: `max_phase="WAIVERS"`, `445/35 = 12.7143`, BELOW the `15.0`
    band_high (comparator `max`) -- a real PASS. Every phase clears
    comfortably: `WAIVERS=12.71`, `RESIGN_WINDOW=12.03`, `DRAFT_
    LOTTERY=11.29`, `DRAFT=10.60`, `FREE_AGENCY=7.95`, `REGULAR_SEASON=3.52`,
    `TRADE_DEADLINE=3.25`. `PRESEASON` reads `ratio=0.0` (`stories=18 >=
    top_k_per_cycle=12`, eligible) -- real `TEAM_PROFILE` stories persist
    there even though `candidates_scored=0` (profile candidates bypass the
    floor/K entirely and are never counted in `candidates_scored`, MV-3's own
    rule), which is why `0.0` never threatens the max. Decision P4-98/P4-123's
    own band text is unchanged; nothing about `TOP_K_BY_PHASE`'s per-phase K
    or `band_high=15.0` was touched by this fix round, only the real
    per-arm-divergence bug the corpus was regenerated to fix."""
    path = Path(__file__).resolve().parents[4] / "artifacts" / "media_corpus.json"
    corpus = MediaCorpusTotals.from_json(json.loads(path.read_text(encoding="utf-8")))
    result = _compute_m35(corpus, _BANDS0)
    assert result.value == pytest.approx(445 / 35, abs=1e-9)
    assert result.detail["max_phase"] == "WAIVERS"
    assert evaluate(_BANDS0.metrics["M35"], result.value) is True  # real PASS -- see above


def test_m36_skips_on_the_zero_mechanism_corpus():
    result = _compute_m36(None, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m36_skips_never_a_fabricated_zero_when_there_are_no_cycles():
    """Decision P4-99: a violation-count metric on an empty corpus reads
    `0` and PASSES vacuously unless it explicitly SKIPs instead --
    matching `compute_m32`'s own "SKIP, never a fabricated 0" pattern."""
    corpus = _media_corpus(live=_media_arm(cycles=()))
    result = _compute_m36(corpus, _BANDS0)
    assert result.value is None
    assert result.skip_reason is not None


def test_m36_counts_conservation_violations():
    """`stories_persisted != candidates_submitted - capped_out`, not bare
    `stories_persisted != candidates_submitted` -- the fix round's own
    restated identity. Cycle 1's gap (`10 - 10 = 0` expected, `9` real) is
    NOT explained by any recorded `capped_out` (defaults to `0`), so it is
    still a real violation -- an unaccounted-for vanished candidate, the
    exact bug class Decision P4-99 exists to catch."""
    corpus = _media_corpus(
        live=_media_arm(
            cycles=[
                _cycle("PRESEASON", 0, 10, 10, 10, 10),  # clean
                _cycle("PRESEASON", 1, 10, 10, 10, 9),  # persisted != submitted - capped_out
            ],
        ),
        top_k_per_cycle=12,
    )
    result = _compute_m36(corpus, _BANDS0)
    assert result.value == pytest.approx(1.0)
    assert result.detail["conservation_violations"] == 1
    assert result.detail["cycle_key_overflow_cycles"] == 0


def test_m36_does_not_count_a_capped_cycle_as_a_conservation_violation():
    """The real, intentional case the restated identity exists for: a
    never-waived per-persona cap (MV-1c.3) skips 2 of 10 submitted
    candidates, so only 8 are persisted -- `10 - 2 == 8` holds exactly, so
    this is NOT a conservation violation, unlike the unaccounted-for gap in
    `test_m36_counts_conservation_violations` above."""
    corpus = _media_corpus(
        live=_media_arm(
            cycles=[_cycle("TRADE_DEADLINE", 0, 10, 10, 10, 8, capped_out=2)],
        ),
        top_k_per_cycle=12,
    )
    result = _compute_m36(corpus, _BANDS0)
    assert result.value == pytest.approx(0.0)
    assert result.detail["conservation_violations"] == 0
    assert result.detail["cycle_key_overflow_cycles"] == 0


def test_m36_counts_cycle_key_overflow_violations():
    """Two `MediaCycleRecord` rows sharing one `(league_id, season, phase,
    cycle_index)` key, persisting 12 stories each -- 24 total, over
    `top_k_per_cycle=12` -- the exact "two calls to `run_media_selection_
    cycle` for one cycle key" wiring defect Decision P4-99 clause (b)
    names. Both rows in the offending group count as violations."""
    corpus = _media_corpus(
        live=_media_arm(
            cycles=[
                _cycle("FREE_AGENCY", 0, 12, 12, 12, 12),
                _cycle("FREE_AGENCY", 0, 12, 12, 12, 12),
            ],
        ),
        top_k_per_cycle=12,
    )
    result = _compute_m36(corpus, _BANDS0)
    assert result.value == pytest.approx(2.0)
    assert result.detail["conservation_violations"] == 0
    assert result.detail["cycle_key_overflow_groups"] == 1
    assert result.detail["cycle_key_overflow_cycles"] == 2


def test_m36_matches_decision_p4_107s_predicted_reading_on_the_real_corpus():
    """**Re-measured against the 2026-09-23 critic-round fix's own regenerated
    corpus -- still a real FAIL, left open for Decision P4-99's owner, same
    posture as every prior round.** This corpus reads `6.0`
    (`conservation_violations=0`, `cycle_key_overflow_cycles=6`) -- by
    coincidence the identical numbers the prior (buggy) round's own corpus
    read, even though this round's fix changed real content (a fresh league,
    a real behavior fix to `subject_last_seen`/`profile_candidates`); the
    match is not asserted to mean anything beyond "still not zero."

    **Correction (2026-09-23 critic round): clause (a)'s `conservation_
    violations=0` does NOT mean this closes the bug class Decision P4-99 was
    written to catch, and a prior round's docstring here was wrong to say so.**
    See `compute_m36`'s own docstring (`tools/gm_harness/metrics.py`) for the
    full account: over a corpus this tool generates, `stories_persisted`,
    `candidates_submitted`, and `capped_out` are all read off the ONE
    `MediaSelectionCycleResult` object per cycle, whose own producing loop has
    exactly two exits per candidate (`capped_out += 1; continue`, or a real
    draw) -- so `stories_persisted == candidates_submitted - capped_out`
    cannot be false for any corpus this tool could produce, and `0` here is
    internally consistent BY CONSTRUCTION, not a real measurement that could
    have caught a bug. This is a real, honest downgrade from "closed, hard, at
    zero violations" to "cannot currently detect the thing it was designed to
    detect" -- see that docstring for what a real fix would need (an
    independently-derived, arm-marked persisted-story count, which does not
    exist today).

    **Cycle-key overflow (clause b) is a real, independent check** -- it
    compares `stories_persisted` summed across every `MediaCycleRecord`
    sharing one `(league_id, season, phase, cycle_index)` key against
    `top_k_per_cycle`, a value from OUTSIDE that per-cycle result object, so
    it is not subject to the same construction-only critique. `6` is real:
    the comparison is still against one FLAT, corpus-level number, while a
    real cycle's total published count is `main` (per-phase K, up to `12`)
    plus `band` plus `profile`, which can still legitimately exceed a flat
    `12` even with zero capping (e.g. a `TRADE_DEADLINE` cycle at its own
    `K=12` that also publishes a hot-take-band story or a profile). Whether
    the remedy is a per-cycle-key comparison against `TOP_K_BY_PHASE[phase] +
    band_size + quota` or something else remains Decision P4-99's own owner's
    call -- `M36` stays a real, live FAIL for this one reason."""
    path = Path(__file__).resolve().parents[4] / "artifacts" / "media_corpus.json"
    corpus = MediaCorpusTotals.from_json(json.loads(path.read_text(encoding="utf-8")))
    result = _compute_m36(corpus, _BANDS0)
    assert result.value == pytest.approx(6.0)
    assert result.detail["conservation_violations"] == 0
    assert result.detail["cycle_key_overflow_groups"] == 6
    assert result.detail["cycle_key_overflow_cycles"] == 6
    assert evaluate(_BANDS0.metrics["M36"], result.value) is False  # real FAIL -- see above


def test_compute_all_media_family_all_skip_never_pass_when_media_corpus_is_none():
    """Decision P4-34's own hard rule, stated as its own explicit test per
    this dispatch: with `media_corpus=None` (the zero-mechanism case), ALL
    FOUR of M33-M36 must `SKIP` -- never `PASS`, never a fabricated value
    -- matching the existing sibling tests for M26/M27/M30/M32's own
    zero-mechanism `SKIP` behavior."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    results = compute_all((run,), _BANDS0, include_metric_17=False)
    media_results = {r.id: r for r in results if r.id in ("M33", "M34", "M35", "M36")}
    assert set(media_results) == {"M33", "M34", "M35", "M36"}
    for metric_id, result in media_results.items():
        assert result.status == "SKIP", f"{metric_id} must SKIP, read {result.status!r}"
        assert result.passed is None
        assert result.counted_in_go is False


def test_compute_all_one_metrics_crash_never_takes_the_whole_run_down():
    """Fix 4 (2026-09-23 critic round). Before this fix, an uncaught exception
    inside any single `compute_mN` call (the real, historical example: `compute_
    m34`'s own bare `assert` firing on a corpus whose paired-arm design is
    genuinely broken) rose straight through `compute_all`'s dispatch loop and
    crashed the entire exit-gate run -- every OTHER metric's own real,
    independently-computable reading was lost along with it. Reusing the exact
    fixture `test_m34_asserts_paired_n_live_equals_n_control_per_cohort` above
    uses to trigger a real `AssertionError` from `compute_m34`: `compute_all`
    must now catch it, report M34 as a `SKIP` naming the exception (never a
    fabricated `PASS`, never a `FAIL` either -- a crash is not a measurement),
    and every other metric in the same run (M1 and M33 here, standing in for
    "everything else") must still compute and report normally, unaffected."""
    live = _media_arm(
        stories_total=13, fallback_stories=13,
        by_voice_kind_template={
            "analyst": {"streak": {"fallback_streak": 13}},
            "historian": {"streak": {"fallback_streak": 10}},
        },
    )
    control = _media_arm(
        stories_total=20, fallback_stories=20,
        by_voice_kind_template={
            "analyst": {"streak": {"fallback_streak": 20}},
            "historian": {"streak": {"fallback_streak": 10}},
        },
    )
    media_corpus = _media_corpus(live=live, control=control)
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])

    results = compute_all(
        (run,), _BANDS0, include_metric_17=False, media_corpus=media_corpus,
    )
    by_id = {r.id: r for r in results}

    m34_result = by_id["M34"]
    assert m34_result.status == "SKIP"
    assert m34_result.passed is None
    assert m34_result.counted_in_go is False
    assert m34_result.value is None

    # Every other metric in the same run computes normally -- the crash was
    # contained to M34 alone, never propagated to the rest of the report.
    assert by_id["M1"].value is not None
    assert by_id["M1"].status in ("PASS", "FAIL")
    m33_result = by_id["M33"]
    assert m33_result.value == pytest.approx(1.0)  # 13/13, this fixture's own live arm
    assert m33_result.status == "FAIL"  # 1.0 > band_high=0.65 -- computed normally, not skipped


def test_m16_season_pin_matches_old_behaviour_at_exactly_5_seasons():
    """Test 5a: at exactly `resolves_at_season` (5) seasons, `run.seasons
    [resolves_at - 1] is run.seasons[-1]` -- so the pin must read exactly
    what the pre-P3-47 `run.seasons[-1]` read, to full float precision."""
    band = _BANDS0.metrics["M16"]
    assert band.params["resolves_at_season"] == 5
    boundaries = _full_boundaries((1, 2))
    idx = _ALL_LABELS.index("season_end")
    boundaries[idx] = _boundary(
        "season_end", roster_ids_by_team={1: (101, 102), 2: (201, 202)}
    )
    earlier = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(4)]
    season5 = _season_record(
        2030, boundaries,
        def_group_mean_by_player={101: 90.0, 102: 90.0, 201: 50.0, 202: 50.0},
    )
    run = _run_record(
        1, [*earlier, season5],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    )
    assert run.seasons[-1] is season5
    assert run.seasons[band.params["resolves_at_season"] - 1] is season5

    result = _m16_core((run,), band)
    assert result.value == pytest.approx(90.0 - 70.0)
    assert result.detail["dropped_runs"] == 0


def test_m16_season_pin_reads_the_pinned_season_not_the_last_when_run_is_longer():
    """Test 5b: a run with MORE than `resolves_at_season` seasons must
    read season 5, not the true last season -- the exact case a reversion
    to `run.seasons[-1]` fails."""
    band = _BANDS0.metrics["M16"]
    idx = _ALL_LABELS.index("season_end")
    boundaries5 = _full_boundaries((1, 2))
    boundaries5[idx] = _boundary(
        "season_end", roster_ids_by_team={1: (101, 102), 2: (201, 202)}
    )
    season5 = _season_record(
        2030, boundaries5,
        def_group_mean_by_player={101: 90.0, 102: 90.0, 201: 50.0, 202: 50.0},
    )
    boundaries6 = _full_boundaries((1, 2))
    boundaries6[idx] = _boundary(
        "season_end", roster_ids_by_team={1: (301, 302), 2: (401, 402)}
    )
    season6 = _season_record(
        2031, boundaries6,
        def_group_mean_by_player={301: 10.0, 302: 10.0, 401: 10.0, 402: 10.0},
    )
    earlier = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(4)]
    run = _run_record(
        1, [*earlier, season5, season6],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    )

    result = _m16_core((run,), band)
    # Season 6's cohort/league means are all 10.0 (gap 0.0) -- a reversion
    # to `run.seasons[-1]` would read 0.0, not season 5's 90 - 70 = 20.0.
    assert result.value == pytest.approx(90.0 - 70.0)
    assert result.value != pytest.approx(0.0)
    assert result.detail["dropped_runs"] == 0


def test_m16_season_pin_drops_runs_shorter_than_resolves_at_season():
    """Test 5c: a run with FEWER than `resolves_at_season` seasons
    contributes nothing to either mean and is counted in `detail[
    "dropped_runs"]`, never raises `IndexError`."""
    band = _BANDS0.metrics["M16"]
    short_run = _run_record(
        1, [_season_record(2026, _full_boundaries((1, 2)))],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    )
    solo_result = _m16_core((short_run,), band)
    assert solo_result.value is None
    assert solo_result.detail["dropped_runs"] == 1
    assert solo_result.skip_reason is not None  # every run too short -> SKIP, not undefined

    idx = _ALL_LABELS.index("season_end")
    boundaries5 = _full_boundaries((1, 2))
    boundaries5[idx] = _boundary(
        "season_end", roster_ids_by_team={1: (101, 102), 2: (201, 202)}
    )
    earlier = [_season_record(2026 + i, _full_boundaries((1, 2))) for i in range(4)]
    season5 = _season_record(
        2030, boundaries5,
        def_group_mean_by_player={101: 90.0, 102: 90.0, 201: 50.0, 202: 50.0},
    )
    long_run = _run_record(
        2, [*earlier, season5],
        philosophy_by_team={1: GmPhilosophy.DEFENSE_FIRST, 2: GmPhilosophy.EGALITARIAN},
    )

    mixed_result = _m16_core((short_run, long_run), band)
    assert mixed_result.value == pytest.approx(90.0 - 70.0)
    assert mixed_result.detail["dropped_runs"] == 1


# ---------------------------------------------------------------------------
# Test 6 (Decision P3-47 Sec.11 step 9): a `skipif`-guarded corpus test
# reproducing this session's own preflight check 0a -- real with-games
# checkpoints vs the real skip-games `m14_seeds_v3` corpus, same rng
# derivation, index-matched, confirmed exact-match on M11-M13/M15/M16.
# Follows `test_attribution.py`'s own `_real_pickle_dir`-style discovery/
# skip pattern for gitignored corpora (both directories below are under
# `artifacts/`, gitignored, environment-generated -- absent in a fresh
# clone or an isolated worktree that hasn't run a real sim).
# ---------------------------------------------------------------------------


def _family3_preflight_dirs() -> tuple[Path, Path] | None:
    """Locates `artifacts/validation_gm/{m14_seeds_v3,step9_exit_gate_
    checkpoints}` relative to whichever checkout this test file lives in.
    Handles the `.claude/worktrees/<agent>/` layout the same way `test_
    attribution.py::_real_pickle_dir` already does."""
    here = Path(__file__).resolve()
    candidates: list[tuple[Path, Path]] = []
    for parent in here.parents:
        candidates.append((
            parent / "artifacts" / "validation_gm" / "m14_seeds_v3",
            parent / "artifacts" / "validation_gm" / "step9_exit_gate_checkpoints",
        ))
        if parent.parent.name == "worktrees":
            root = parent.parent.parent
            candidates.append((
                root / "artifacts" / "validation_gm" / "m14_seeds_v3",
                root / "artifacts" / "validation_gm" / "step9_exit_gate_checkpoints",
            ))
    for skip_dir, real_dir in candidates:
        if skip_dir.is_dir() and real_dir.is_dir():
            return skip_dir, real_dir
    return None


_FAMILY3_PREFLIGHT_DIRS = _family3_preflight_dirs()


@pytest.mark.skipif(
    _FAMILY3_PREFLIGHT_DIRS is None,
    reason="artifacts/validation_gm/{m14_seeds_v3,step9_exit_gate_checkpoints} not present "
           "(gitignored)",
)
def test_family3_skip_games_invariance_on_the_real_45_seed_corpus():
    """Reproduces this round's own preflight check 0a directly: M11-M13/
    M15/M16 computed on the real 3-seed with-games checkpoints must
    exactly match the same computation on the corresponding (same
    `rng_seed`, index-matched) skip-games `m14_seeds_v3` seeds -- Family 3
    is skip-games-invariant by construction, which is what makes gating it
    on a skip-games 45-seed corpus (Decision P3-47) valid in the first
    place."""
    skip_dir, real_dir = _FAMILY3_PREFLIGHT_DIRS
    real_paths = sorted(real_dir.glob("league*_skipFalse_mnr3.pkl"))
    if not real_paths:
        pytest.skip("no league*_skipFalse_mnr3.pkl files found under step9_exit_gate_checkpoints/")

    def _load(path: Path) -> RunRecord:
        with path.open("rb") as f:
            return pickle.load(f)

    real_runs = tuple(_load(p) for p in real_paths)
    # Index-matched by `rng_seed`, not position -- confirms the "same rng
    # derivation" claim rather than assuming file ordering lines up.
    skip_by_seed = {}
    for p in sorted(skip_dir.glob("seed_*.pkl")):
        run = _load(p)
        skip_by_seed[run.rng_seed] = run
    skip_runs = tuple(skip_by_seed[r.rng_seed] for r in real_runs if r.rng_seed in skip_by_seed)
    if len(skip_runs) != len(real_runs):
        pytest.skip("m14_seeds_v3 does not cover every step9 checkpoint's rng_seed")

    for metric_id, raw_fn in (
        ("M11", _m11_raw_value), ("M12", _m12_raw_value), ("M13", _m13_raw_value),
        ("M15", _m15_raw_value), ("M16", _m16_raw_value),
    ):
        band = _BANDS0.metrics[metric_id]
        real_value = raw_fn(real_runs, band)
        skip_value = raw_fn(skip_runs, band)
        assert real_value == skip_value, metric_id  # bit-for-bit, not approx


# ---------------------------------------------------------------------------
# Decision P3-29 -- the two committed Phase-2 control artifacts and their
# comparators (Phase 3 §11 Stage A step 2). No dependency on any other
# Phase 3 step or switch; nothing here is wired into `compute_all`.
# ---------------------------------------------------------------------------


def _report(**overrides) -> dict:
    """A minimal, hand-built `report.json`-shaped dict -- just enough
    structure for `compare_exit_gate_reports` to exercise every field it
    reads, without a real 24-minute exit-gate run behind it."""
    defaults = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "go": True,
        "go_reason": "all counted metrics pass",
        "seeds": 3,
        "seasons": 5,
        "players_per_team": 15,
        "rng_seed": 1,
        "skip_games": False,
        "determinism_check": True,
        "elapsed_seconds": 1463.2,
        "phase_timings_by_seed": [{"seed_index": 0, "elapsed_seconds": 480.1}],
        "metrics": [
            {
                "id": "M1", "number": 1, "family": "Family-1", "name": "roster_bounds",
                "comparator": "max_violations", "unit": "count", "band_low": None,
                "band_high": 0.0, "value": 0.0, "per_season": None, "status": "PASS",
                "passed": True, "counted_in_go": True, "detail": {},
            },
        ],
    }
    defaults.update(overrides)
    return defaults


def test_excluded_report_fields_is_exactly_the_wall_clock_set():
    """Decision P3-29(a)'s excluded-field list, asserted exactly rather
    than applied ad hoc -- a later step cannot silently widen this set
    without this test failing."""
    assert EXCLUDED_REPORT_FIELDS == frozenset(
        {"generated_at", "elapsed_seconds", "phase_timings_by_seed", "per_season_phase_timings"}
    )


def test_path_shaped_detail_fields_is_exactly_the_golden_path_set():
    """Step 9's own fix, asserted exactly rather than applied ad hoc --
    mirrors `test_excluded_report_fields_is_exactly_the_wall_clock_set`'s
    own discipline so a later step cannot silently widen this set either."""
    assert _PATH_SHAPED_DETAIL_FIELDS == frozenset({"golden_path"})


def test_compare_exit_gate_reports_golden_path_prefix_drift_does_not_mismatch():
    """The confirmed bug this step fixes: two reports whose `golden_path`
    differs only in its absolute prefix (e.g. one generated inside a
    worktree, one generated at the repo root) must compare EQUAL -- a
    naive `!=` on the full absolute string would fail this on every run,
    regardless of correctness."""
    baseline = _report(
        metrics=[
            {
                **_report()["metrics"][0],
                "id": "A1",
                "detail": {
                    "rows": 30,
                    "golden_path": (
                        r"C:\Users\Owner\Desktop\AI\Projects\dba2\.claude\worktrees"
                        r"\agent-abc94cc3363b65407\seeds\gm_value_archetypes.json"
                    ),
                },
            },
        ]
    )
    actual = _report(
        metrics=[
            {
                **_report()["metrics"][0],
                "id": "A1",
                "detail": {
                    "rows": 30,
                    "golden_path": r"C:\Users\Owner\Desktop\AI\Projects\dba2"
                    r"\seeds\gm_value_archetypes.json",
                },
            },
        ]
    )
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is True
    assert result.mismatches == ()


def test_compare_exit_gate_reports_golden_path_different_target_file_still_mismatches():
    """A genuinely different TARGET file (different basename) must still
    register as a mismatch -- the fix compares path tails, it does not
    exclude `golden_path` outright."""
    baseline = _report(
        metrics=[
            {
                **_report()["metrics"][0],
                "id": "A1",
                "detail": {
                    "rows": 30,
                    "golden_path": r"C:\Users\Owner\Desktop\AI\Projects\dba2"
                    r"\seeds\gm_value_archetypes.json",
                },
            },
        ]
    )
    actual = _report(
        metrics=[
            {
                **_report()["metrics"][0],
                "id": "A1",
                "detail": {
                    "rows": 30,
                    "golden_path": r"C:\Users\Owner\Desktop\AI\Projects\dba2"
                    r"\seeds\gm_pick_archetypes.json",
                },
            },
        ]
    )
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("A1.detail" in m for m in result.mismatches)


def test_compare_exit_gate_reports_ignores_every_excluded_wall_clock_field():
    baseline = _report()
    actual = _report(
        generated_at="2099-12-31T23:59:59+00:00",
        elapsed_seconds=1.0,
        phase_timings_by_seed=[{"seed_index": 0, "elapsed_seconds": 9999.9}],
    )
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is True
    assert result.mismatches == ()


def test_compare_exit_gate_reports_catches_a_real_top_level_mismatch():
    baseline = _report(go=True)
    actual = _report(go=False)
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("go" in m for m in result.mismatches)


def test_compare_exit_gate_reports_catches_a_metric_value_mismatch():
    baseline = _report()
    actual_metrics = [dict(baseline["metrics"][0])]
    actual_metrics[0]["value"] = 1.0
    actual_metrics[0]["passed"] = False
    actual = _report(metrics=actual_metrics)
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("M1.value" in m for m in result.mismatches)
    assert any("M1.passed" in m for m in result.mismatches)


def test_compare_exit_gate_reports_catches_a_missing_or_extra_metric():
    baseline = _report()
    actual = _report(metrics=[])
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("only in baseline" in m for m in result.mismatches)


def test_compare_exit_gate_reports_catches_duplicate_metric_ids():
    """Critic round 27 nit 2: `{m["id"]: m for m in ...}` silently
    collapses duplicate ids with no cardinality guard -- a real bug in a
    report writer (two entries sharing an id) must surface as a
    mismatch, not disappear into the dict comprehension."""
    baseline = _report()
    dup_metric = dict(baseline["metrics"][0])
    actual = _report(metrics=[dup_metric, dict(dup_metric)])
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("duplicate ids" in m for m in result.mismatches)


def test_compare_exit_gate_reports_empty_dicts_are_not_silently_equal():
    """Critic round 27 nit 3: both sides using `.get(field, None)` let a
    field that vanished from BOTH sides compare `None == None` and pass
    silently -- `compare_exit_gate_reports({}, {})` must not report
    `equal=True`."""
    result = compare_exit_gate_reports({}, {})
    assert result.equal is False
    assert any(
        "missing from actual" in m or "missing from baseline" in m for m in result.mismatches
    )


def test_compare_exit_gate_reports_detects_a_field_missing_from_one_side_only():
    baseline = _report()
    actual = dict(_report())
    del actual["go_reason"]
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    assert any("go_reason" in m and "missing from actual" in m for m in result.mismatches)


def test_compare_exit_gate_reports_detail_mismatch_reports_only_the_differing_key():
    """Critic round 27 nit 1: a single differing key inside a large
    `detail` block should not dump the whole dict twice."""
    big_blob = list(range(500))
    baseline = _report(
        metrics=[{**_report()["metrics"][0], "detail": {"a": 1, "big_unrelated_blob": big_blob}}]
    )
    actual = _report(
        metrics=[{**_report()["metrics"][0], "detail": {"a": 2, "big_unrelated_blob": big_blob}}]
    )
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is False
    detail_mismatch = next(m for m in result.mismatches if "M1.detail" in m)
    assert "a: 2 != 1" in detail_mismatch
    assert "big_unrelated_blob" not in detail_mismatch


def test_compare_exit_gate_reports_ignores_non_compared_metric_fields():
    """`number`/`family`/`name`/`comparator`/`unit`/`per_season` are
    descriptive, not on Decision P3-29(a)'s field list -- a change to one
    of them alone must not register as a mismatch."""
    baseline = _report()
    actual_metrics = [dict(baseline["metrics"][0])]
    actual_metrics[0]["name"] = "renamed_but_same_metric"
    actual_metrics[0]["per_season"] = [0.0, 0.0, 0.0]
    actual = _report(metrics=actual_metrics)
    result = compare_exit_gate_reports(actual, baseline)
    assert result.equal is True


def test_compare_exit_gate_reports_detail_ignores_a_timing_key_but_catches_a_real_one():
    baseline = _report(
        metrics=[{**_report()["metrics"][0], "detail": {"elapsed_seconds": 1.0, "n": 3}}]
    )
    actual_same_value_different_timing = _report(
        metrics=[{**_report()["metrics"][0], "detail": {"elapsed_seconds": 99.0, "n": 3}}]
    )
    actual_real_change = _report(
        metrics=[{**_report()["metrics"][0], "detail": {"elapsed_seconds": 1.0, "n": 4}}]
    )
    assert compare_exit_gate_reports(actual_same_value_different_timing, baseline).equal is True
    real_change_result = compare_exit_gate_reports(actual_real_change, baseline)
    assert real_change_result.equal is False
    assert any("M1.detail" in m for m in real_change_result.mismatches)


def test_phase2_exit_gate_baseline_is_committed_and_well_formed():
    """The committed `docs/ledger/phase-2-exit-gate-report.json` (Decision
    P3-29(a)) loads and is internally well-formed -- a self-comparison
    (baseline against itself) is a cheap sanity check that `compare_exit_
    gate_reports` reads this file's own real shape correctly. **This is
    NOT the "compares clean today" verify bar** -- a self-comparison is
    tautological and can never fail (critic round 27 blocking finding 4).
    The real verification is NOT a test in this file at all -- it is a
    one-time empirical run recorded as evidence in the committed
    baseline's own `_provenance.reproducibility_verification` field (a
    genuine second, independent with-games regeneration compared against
    this baseline with the real comparator, `equal=True`, 0 mismatches;
    critic round 28 confirmed this by re-running the comparison itself).
    Runs in milliseconds (a JSON load + one comparison) -- no
    `@pytest.mark.slow` needed (critic round 27 nit 4)."""
    baseline = load_phase2_exit_gate_baseline(DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH)
    assert baseline["metrics"], "committed baseline has no metrics -- did generation fail?"
    result = compare_exit_gate_reports(baseline, baseline)
    assert result.equal is True
    assert result.mismatches == ()


# --- Decision P3-29(b): the digest generator/comparator ---


def _seasonal(season: int, lines: tuple[str, ...]) -> SeasonRecord:
    return _season_record(season, _full_boundaries((1,)), jsonl_lines=lines)


def test_season_decision_log_digest_is_deterministic_and_order_sensitive():
    lines = ('{"a": 1}', '{"b": 2}')
    assert season_decision_log_digest(lines) == season_decision_log_digest(lines)
    assert season_decision_log_digest(lines) != season_decision_log_digest(tuple(reversed(lines)))


def test_season_decision_log_digest_does_not_collide_across_a_line_boundary():
    """Hashing the plain concatenation (no separator) would let `("ab",
    "c")` and `("a", "bc")` collide -- the trailing-newline framing this
    module chose must not."""
    assert season_decision_log_digest(("ab", "c")) != season_decision_log_digest(("a", "bc"))


def test_diff_decision_log_lines_returns_none_when_identical():
    lines = ('{"a": 1}', '{"b": 2}')
    assert diff_decision_log_lines(lines, lines) is None


def test_diff_decision_log_lines_localizes_the_first_differing_index():
    actual = ('{"a": 1}', '{"b": 2}', '{"c": 3}')
    expected = ('{"a": 1}', '{"b": 999}', '{"c": 3}')
    diff = diff_decision_log_lines(actual, expected)
    assert diff is not None
    assert diff.index == 1
    assert diff.actual_line == '{"b": 2}'
    assert diff.expected_line == '{"b": 999}'


def test_diff_decision_log_lines_handles_a_length_mismatch():
    actual = ('{"a": 1}',)
    expected = ('{"a": 1}', '{"b": 2}')
    diff = diff_decision_log_lines(actual, expected)
    assert diff is not None
    assert diff.index == 1
    assert diff.actual_line is None
    assert diff.expected_line == '{"b": 2}'


def test_compare_decision_log_digest_clean_when_hashes_match():
    season = _seasonal(2026, ('{"a": 1}',))
    baseline = {
        "season_digests": [
            {"season": 2026, "digest": season_decision_log_digest(season.jsonl_lines)}
        ]
    }
    results = compare_decision_log_digest((season,), baseline)
    assert len(results) == 1
    assert results[0].equal is True
    assert results[0].line_diff is None


def test_compare_decision_log_digest_mismatch_without_reference_reports_no_line_diff():
    season = _seasonal(2026, ('{"a": 1}',))
    baseline = {"season_digests": [{"season": 2026, "digest": "0" * 64}]}
    results = compare_decision_log_digest((season,), baseline)
    assert results[0].equal is False
    assert results[0].line_diff is None


def test_compare_decision_log_digest_mismatch_with_reference_localizes_both_lines():
    """The design's own requirement: on a mismatch, report the first
    differing line index and both lines -- using an in-memory reference
    run, never a committed corpus (none is committed; only the digest is)."""
    season = _seasonal(2026, ('{"a": 1}', '{"b": 2}'))
    reference = _seasonal(2026, ('{"a": 1}', '{"b": 999}'))
    baseline = {"season_digests": [{"season": 2026, "digest": "0" * 64}]}
    results = compare_decision_log_digest((season,), baseline, reference_seasons=(reference,))
    assert results[0].equal is False
    assert results[0].line_diff is not None
    assert results[0].line_diff.index == 1
    assert results[0].line_diff.actual_line == '{"b": 2}'
    assert results[0].line_diff.expected_line == '{"b": 999}'


# ---------------------------------------------------------------------------
# §11 step 16 -- M26 (`phrase_fallback_share`) and M27 (`voice_
# differentiation`) are now real, counted metrics (Decision P3-19/P3-31,
# §9.1). Direct unit tests against hand-built `SeasonRecord` fixtures,
# matching this file's own established fixture-construction style
# (`_season_record`'s `**overrides`) -- exact values, not just bounds.
# ---------------------------------------------------------------------------


def test_compute_m26_skips_when_no_phrase_selections():
    """Decision P3-37's guard -- dialogue off (or no corpus at all) reads
    `phrase_selections_total == (0, 0)` (the dataclass default), never a
    bare `0 / 0`."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [_season_record(2026, boundaries)])
    result = _compute_m26((run,), _BANDS)
    assert result.skip_reason is not None
    assert result.value is None


_HEALTHY_M27_CONTROL: dict[str, dict[str, int]] = {
    GmPhilosophy.STAR_MAXER.value: {"t1": 5}, GmPhilosophy.EGALITARIAN.value: {"t1": 5},
}


def test_compute_m27_skips_when_control_by_philosophy_template_is_none():
    """Decision P3-49, item 5's own precedent (`compute_m28`'s identical
    `control_totals is None` rule) -- `--no-control-check` must SKIP, never
    compute an unpaired `tv_live`, which would silently resurrect the exact
    defect this fix closes."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(5, 5),
            phrase_selections_by_philosophy_template=(
                {
                    GmPhilosophy.STAR_MAXER.value: {"t1": 3, "t2": 1},
                    GmPhilosophy.EGALITARIAN.value: {"t1": 1, "t2": 3},
                },
                {
                    GmPhilosophy.STAR_MAXER.value: {"t1": 1},
                    GmPhilosophy.EGALITARIAN.value: {"t2": 1},
                },
            ),
        )
    ])
    result = _compute_m27((run,), _BANDS, control_by_philosophy_template=None)
    assert result.skip_reason is not None
    assert result.value is None


def test_compute_m27_skips_when_star_maxer_cohort_is_absent_in_the_live_arm():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(5, 0),
            phrase_selections_by_philosophy_template=(
                {GmPhilosophy.EGALITARIAN.value: {"fallback_open": 5}}, {},
            ),
        )
    ])
    result = _compute_m27(
        (run,), _BANDS, control_by_philosophy_template=_HEALTHY_M27_CONTROL,
    )
    assert result.skip_reason is not None
    assert result.value is None
    assert result.detail["star_maxer_selections_live"] == 0
    assert result.detail["egalitarian_selections_live"] == 5


def test_compute_m27_skips_when_egalitarian_cohort_is_absent_in_the_live_arm():
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(5, 0),
            phrase_selections_by_philosophy_template=(
                {GmPhilosophy.STAR_MAXER.value: {"fallback_open": 5}}, {},
            ),
        )
    ])
    result = _compute_m27(
        (run,), _BANDS, control_by_philosophy_template=_HEALTHY_M27_CONTROL,
    )
    assert result.skip_reason is not None
    assert result.value is None
    assert result.detail["star_maxer_selections_live"] == 5
    assert result.detail["egalitarian_selections_live"] == 0


def test_compute_m27_skips_when_a_cohort_is_absent_in_the_control_arm():
    """The SKIP guard is checked in EITHER arm, not just the live one --
    a healthy live corpus paired with a degenerate control arm (e.g. a
    corpus-scale problem on the control re-run) must still SKIP rather
    than compute a bogus paired difference against a one-sided control."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(5, 5),
            phrase_selections_by_philosophy_template=(
                {
                    GmPhilosophy.STAR_MAXER.value: {"t1": 3, "t2": 1},
                    GmPhilosophy.EGALITARIAN.value: {"t1": 1, "t2": 3},
                },
                {},
            ),
        )
    ])
    control = {GmPhilosophy.STAR_MAXER.value: {"t1": 5}}  # egalitarian absent
    result = _compute_m27((run,), _BANDS, control_by_philosophy_template=control)
    assert result.skip_reason is not None
    assert result.value is None
    assert result.detail["egalitarian_selections_control"] == 0


def test_compute_m26_exact_fallback_share_and_per_situation_breakdown():
    """Hand-computed, exact: window 1 has 10 selections (6 `open`, 4
    `reject_too_far`), 3 fallback (1 `open`, 2 `reject_too_far`); window 2
    has 10 more selections, all `open`, 1 fallback. Pooled: 20 total, 4
    fallback -> 0.20 exactly. Per-situation: `open` pools to 16 selections/
    2 fallback (0.125 exactly); `reject_too_far` pools to 4 selections/2
    fallback (0.5 exactly) -- both clean fractions, chosen so a swapped
    numerator/denominator or a mis-pooled window would visibly fail."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(10, 10),
            phrase_fallback_selections=(3, 1),
            phrase_selections_by_situation=(
                {"open": 6, "reject_too_far": 4}, {"open": 10},
            ),
            phrase_fallback_by_situation=(
                {"open": 1, "reject_too_far": 2}, {"open": 1},
            ),
        )
    ])
    result = _compute_m26((run,), _BANDS)
    assert result.skip_reason is None
    assert result.value == pytest.approx(0.20)
    assert result.detail["selections_total"] == 20
    assert result.detail["fallback_selections"] == 4
    per_situation = result.detail["per_situation"]
    assert per_situation["open"]["selections"] == 16
    assert per_situation["open"]["fallback_selections"] == 2
    assert per_situation["open"]["fallback_share"] == pytest.approx(0.125)
    assert per_situation["reject_too_far"]["selections"] == 4
    assert per_situation["reject_too_far"]["fallback_selections"] == 2
    assert per_situation["reject_too_far"]["fallback_share"] == pytest.approx(0.5)


def test_compute_m27_exact_paired_difference_two_templates_pooled_across_windows():
    """Hand-computed, exact. Live arm, pooled across both windows: `star_
    maxer` selects `t1` 4 times and `t2` once (5 total: 0.8/0.2);
    `egalitarian` selects `t1` once and `t2` 4 times (5 total: 0.2/0.8) --
    the mirror-image distribution, `tv_live = 0.5*(|0.8-0.2|+|0.2-0.8|) =
    0.6` exactly. Control arm (hand-built, distinct from the live one):
    `star_maxer` 60/40, `egalitarian` 40/60 -- `tv_control =
    0.5*(|0.6-0.4|+|0.4-0.6|) = 0.2` exactly. `M27' = tv_live - tv_control
    = 0.4` exactly (Decision P3-49)."""
    boundaries = _full_boundaries((1, 2))
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(5, 5),
            phrase_selections_by_philosophy_template=(
                {
                    GmPhilosophy.STAR_MAXER.value: {"t1": 3, "t2": 1},
                    GmPhilosophy.EGALITARIAN.value: {"t1": 1, "t2": 3},
                },
                {
                    GmPhilosophy.STAR_MAXER.value: {"t1": 1},
                    GmPhilosophy.EGALITARIAN.value: {"t2": 1},
                },
            ),
        )
    ])
    control = {
        GmPhilosophy.STAR_MAXER.value: {"t1": 6, "t2": 4},
        GmPhilosophy.EGALITARIAN.value: {"t1": 4, "t2": 6},
    }
    result = _compute_m27((run,), _BANDS, control_by_philosophy_template=control)
    assert result.skip_reason is None
    assert result.value == pytest.approx(0.4)
    assert result.detail["tv_live"] == pytest.approx(0.6)
    assert result.detail["tv_control"] == pytest.approx(0.2)
    assert result.detail["m27_paired_difference"] == pytest.approx(0.4)
    assert result.detail["star_maxer_selections_live"] == 5
    assert result.detail["egalitarian_selections_live"] == 5
    assert result.detail["star_maxer_selections_control"] == 10
    assert result.detail["egalitarian_selections_control"] == 10
    assert result.detail["template_count_live"] == 2
    assert result.detail["template_count_control"] == 2


def test_compute_m27_paired_difference_reads_zero_for_a_zero_exclusivity_library():
    """The architect's own named missing regression test (Decision P3-49) --
    the direct corner the OLD, un-paired M27 could not catch. A library
    with zero `exclusive_mass_share` (every `philosophies` field forced to
    `()`) still lets philosophy-correlated TRIGGER conditions differentiate
    `star_maxer`'s and `egalitarian`'s own template selection (§9.1's dated
    correction) -- so `tv_live` ALONE reads a real, nonzero value here
    (`0.7`, well past the fix's own measured 0.148-0.166 empirical range)
    that would have WRONGLY PASSED the old, un-paired M27 against its
    `>= 0.125` floor. Modeled here as the live and control arms reading
    the IDENTICAL distribution -- the honest reading once `philosophies`
    is already a wildcard in both arms, so disabling it a second time (the
    control transformation) changes nothing further -- `M27' = tv_live -
    tv_control == 0.0` exactly, correctly FAILING the unchanged `0.125`
    floor."""
    boundaries = _full_boundaries((1, 2))
    zero_exclusivity_distribution = {
        GmPhilosophy.STAR_MAXER.value: {"t1": 85, "t2": 15},
        GmPhilosophy.EGALITARIAN.value: {"t1": 15, "t2": 85},
    }
    run = _run_record(1, [
        _season_record(
            2026, boundaries,
            phrase_selections_total=(100, 0),
            phrase_selections_by_philosophy_template=(zero_exclusivity_distribution, {}),
        )
    ])
    result = _compute_m27(
        (run,), _BANDS, control_by_philosophy_template=zero_exclusivity_distribution,
    )
    assert result.skip_reason is None
    assert result.detail["tv_live"] == pytest.approx(0.7)
    assert result.detail["tv_control"] == pytest.approx(0.7)
    assert result.value == pytest.approx(0.0)
    assert evaluate(_BANDS.metrics["M27"], result.value) is False, (
        "M27' == 0.0 must FAIL the unchanged 0.125 floor -- this is the exact corner the old, "
        "bare tv_live reading could not catch"
    )


# ---------------------------------------------------------------------------
# M28 -- item 1: the value formula is `min(ch3, ch2, ch1_up + ch1_down)`,
# never a 4-way min. See `compute_m28`'s own docstring for the full
# derivation this test freezes as a regression (the critic's exact repro).
# ---------------------------------------------------------------------------


def _healthy_control_totals(
    *, ch3_sites: float = 100.0, ch1_up_sites: float = 100.0, ch1_down_sites: float = 100.0,
    ch3_flips: float = 0.0, ch2_flips: float = 0.0, ch1_up_flips: float = 0.0,
    ch1_down_flips: float = 0.0, ch2_sites: float = 0.0,
) -> dict[str, float]:
    """A synthetic, healthy M29-control-pass pooled-totals dict (item 5's
    own audit instruction): positive sites/zero flips for ch3/ch1_up/
    ch1_down (a REAL zero, per §9.2's control-pass table), zero sites for
    ch2 by default (its own documented VACUOUS zero -- `build_counter`
    never runs at `max_negotiation_rounds=0`). Passed to `compute_m28`/
    `compute_all` by every test below whose actual subject is `value` or
    the reachability conjunct, never the control-pass mechanism itself, so
    Item 5's `control_totals is None -> SKIP` change does not silently
    flip these tests' own M28 status out from under them."""
    return {
        "channel3_decision_sites": ch3_sites, "channel3_flips": ch3_flips,
        "channel2_decision_sites": ch2_sites, "channel2_flips": ch2_flips,
        "channel1_up_decision_sites": ch1_up_sites, "channel1_up_flips": ch1_up_flips,
        "channel1_down_decision_sites": ch1_down_sites, "channel1_down_flips": ch1_down_flips,
    }


def test_m28_value_is_min_over_three_channels_not_a_four_way_min():
    """The critic's exact repro, frozen as a regression test (item 1).
    `ch1_down_flips=0` at `ch1_down_reachability=0` is §11 step 17's own
    named "threshold unreachable at this event rate" outcome -- a
    predicted-legitimate reading that MUST pass, not fail. A 4-way `min`
    (`min(ch3_flips=9, ch2_flips=5, ch1_up_flips=300, ch1_down_flips=0)`)
    reads `0.0` and fails the `>= 1` band outright; the correct formula
    (`min(9, 5, 300 + 0)`) reads `5.0` and passes, with `extra_pass=True`
    via the reachability conjunct's own escape clause on `ch1_down`."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(
        2026, boundaries,
        channel3_decision_sites=(9, 0), channel3_reachability=(9, 0), channel3_flips=(9, 0),
        channel2_decision_sites=(5, 0), channel2_reachability=(5, 0), channel2_flips=(5, 0),
        channel1_up_decision_sites=(300, 0), channel1_up_reachability=(300, 0),
        channel1_up_flips=(300, 0),
        channel1_down_decision_sites=(300, 0), channel1_down_reachability=(0, 0),
        channel1_down_flips=(0, 0),
    )
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=_healthy_control_totals())
    assert result.detail["channel1_combined_flips"] == 300
    assert result.value == 5.0, (
        f"expected min(9, 5, 300+0) == 5.0, got {result.value!r} -- a 4-way min would read "
        "0.0 here (min(9, 5, 300, 0)) and wrongly fail a predicted-legitimate outcome"
    )
    assert result.extra_pass is True
    assert result.skip_reason is None


# ---------------------------------------------------------------------------
# M28 -- item 2/3: the triage gate's own two booleans, direct `compute_m28`
# unit tests (`detail["triage_down_violation"]`/`["triage_up_violation"]`).
# `_assert_m28_step17_invariants` (tools/validate_gm.py) is what actually
# RAISES on these -- see that module's own test file for the raise-path
# coverage; these tests only confirm `compute_m28` computes the booleans
# correctly from `far_rejects_observed`/`m18_trades_executed`/the two
# `max_observed` fields.
# ---------------------------------------------------------------------------


def _healthy_ch1_channel_kwargs() -> dict[str, tuple[int, int]]:
    """Positive decision sites for every branch (never `0`, so `compute_
    m28` never SKIPs via Decision P3-37's guard) with an otherwise-healthy
    (ladder-identity-holding, non-triage-tripping) reading, for tests
    whose actual subject is the triage booleans, never the reachability
    conjunct or the site-zero guard."""
    return dict(
        channel3_decision_sites=(10, 0), channel3_reachability=(10, 0), channel3_flips=(1, 0),
        channel2_decision_sites=(10, 0), channel2_reachability=(10, 0), channel2_flips=(1, 0),
        channel1_up_decision_sites=(10, 0), channel1_up_reachability=(10, 0),
        channel1_up_flips=(10, 0),
        channel1_down_decision_sites=(10, 0), channel1_down_reachability=(0, 0),
        channel1_down_flips=(0, 0),
    )


def test_m28_triage_down_violation_when_far_rejects_observed_but_max_observed_is_zero():
    """`far_rejects_observed=100, channel1_down_max_observed=0.0` ->
    `triage_down_violation is True` -- the never-written-field signature
    for `far_reject_streak` (Decision P3-41)."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(
        2026, boundaries, **_healthy_ch1_channel_kwargs(),
        far_rejects_observed=(100, 0), channel1_down_max_observed=(0.0, 0.0),
        channel1_up_max_observed=(3.0, 0.0),
    )
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=_healthy_control_totals())
    assert result.detail["far_rejects_observed"] == 100
    assert result.detail["channel1_down_max_observed"] == 0.0
    assert result.detail["triage_down_violation"] is True
    assert result.detail["triage_up_violation"] is False
    assert result.detail["triage_pass"] is False


def test_m28_triage_up_violation_when_trades_executed_but_max_observed_is_zero():
    """Mirror, up side: real trades executed (via `trades_by_window`, the
    identical M18 expression) but `channel1_up_max_observed=0.0` ->
    `triage_up_violation is True`."""
    boundaries = _full_boundaries((1, 2))
    execution = _FakeExecution(1, 2)
    season = _season_record(
        2026, boundaries, **_healthy_ch1_channel_kwargs(),
        trades_by_window=((execution,), ()),
        channel1_up_max_observed=(0.0, 0.0), channel1_down_max_observed=(0.0, 0.0),
        far_rejects_observed=(0, 0),
    )
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=_healthy_control_totals())
    assert result.detail["m18_trades_executed"] == 1
    assert result.detail["channel1_up_max_observed"] == 0.0
    assert result.detail["triage_up_violation"] is True
    assert result.detail["triage_down_violation"] is False
    assert result.detail["triage_pass"] is False


def test_m28_control_totals_none_skips_rather_than_silently_passing():
    """Item 5's own `--no-control-check` fix, mirroring `compute_m29`'s
    identical rule -- a metric whose band requires the control pass
    cannot silently render a partial PASS on half its own conjunct."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(2026, boundaries, **_healthy_ch1_channel_kwargs())
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=None)
    assert result.skip_reason is not None
    assert result.value is None


def test_m28_control_pass_conjunct_catches_the_contaminated_pre_p3_48_figures():
    """The historically-contaminated pre-Decision-P3-48 control-pass
    reading, verbatim from this metric's own docstring/band rationale --
    `channel1_down_flips=130` over `10,655` real control-pass sites, `head_
    to_head` accumulating real data across the control run despite `max_
    negotiation_rounds=0`. Item 5's own conjunct must now catch this as a
    real, gated failure -- proving a regression back to this contaminated
    shape is catchable, not merely reported."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(2026, boundaries, **_healthy_ch1_channel_kwargs())
    run = _run_record(1, [season])
    contaminated_control_totals = _healthy_control_totals(ch1_down_sites=10_655, ch1_down_flips=130)
    result = _compute_m28((run,), _BANDS, control_totals=contaminated_control_totals)
    assert result.extra_pass is False, (
        "a contaminated control pass (real, nonzero ch1_down flips over real sites) must "
        "fail extra_pass, not pass silently the way the pre-Decision-P3-48 build did"
    )
    assert result.detail["control_branch_pass"]["ch1_down"] is False


def test_m28_control_pass_conjunct_ch2_vacuous_zero_is_not_penalized():
    """§9.2's own carve-out: ch2's control-pass zero is legitimately
    VACUOUS (zero sites, `build_counter` never runs at `max_negotiation_
    rounds=0`) -- this must satisfy the conjunct, never be treated as a
    degenerate zero-site failure the way ch3/ch1_up/ch1_down's own clause
    would treat it."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(2026, boundaries, **_healthy_ch1_channel_kwargs())
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=_healthy_control_totals(ch2_sites=0.0))
    assert result.detail["control_zero_kind"]["ch2"] == "vacuous"
    assert result.detail["control_branch_pass"]["ch2"] is True
    assert result.extra_pass is True


def test_m28_triage_passes_when_max_observed_is_positive():
    """The honest, wired-build reading: real far rejects observed AND a
    positive `max_observed` -- `triage_down_violation` must be `False`,
    never tripped by a healthy, real-signal run."""
    boundaries = _full_boundaries((1, 2))
    season = _season_record(
        2026, boundaries, **_healthy_ch1_channel_kwargs(),
        far_rejects_observed=(100, 0), channel1_down_max_observed=(4.0, 0.0),
        channel1_up_max_observed=(3.0, 0.0),
    )
    run = _run_record(1, [season])
    result = _compute_m28((run,), _BANDS, control_totals=_healthy_control_totals())
    assert result.detail["triage_down_violation"] is False
    assert result.detail["triage_pass"] is True


# ---------------------------------------------------------------------------
# Family 5 (Phase 3 §11 step 7) -- the real-corpus verify clauses this step
# names explicitly: Decision P3-37's guard (M26/M27/M28 SKIP without raising
# on a real counters-on run), M23'/M24/M25/M30 measured for the first time,
# and M29's precomputed-int wrapper shape.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_p3_37_guard_family5_mechanism_absent_metrics_skip_without_raising_on_a_real_run():
    """Decision P3-37's own guard, made mechanical (§11 step 7's verify --
    "M26, M27 and M28 each return a skip_reason and render SKIP, and the
    run does not raise"). `compute_all` (`:2013-2034` per the design doc's
    own citation) calls every registered compute function unconditionally
    regardless of `counted_in_go`, so these three run against REAL
    counters-on data here -- not an empty `runs` tuple -- and must survive
    it by returning an explicit `skip_reason`, never a bare division.

    **§11 step 17 update -- M28 no longer belongs in this loop.** Channel
    1 (Decision P3-40) is now built, and every ordered pair/window is a
    channel-1 decision site regardless of whether dialogue/adaptation is
    otherwise "absent" from this corpus -- so M28's `skip_reason` path is
    now structurally unreachable on any real trade-window run (Decision
    P3-37's condition (A) never fires here), exactly as §11 step 17
    requires. M28 is asserted separately, below, as a real, counted value
    rather than a SKIP.

    **Item 5's own audit instruction, applied here** -- once `control_
    totals is None` is a hard SKIP (item 5), this test's own `compute_all`
    call would SKIP M28 on that (irrelevant to this test's subject) half
    of the conjunct too, since it does not run a real M29 control pass. A
    synthetic, healthy `control_totals` is passed so this test keeps
    exercising the thing it actually names -- the site-zero guard being
    unreachable -- never the control-pass mechanism itself."""
    from tools.gm_harness.season import run_seed

    record = run_seed(
        league_id=1, rng_seed=6, first_season=2026, seasons=1, players_per_team=15,
        skip_games=True, tuning=DEFAULT_TUNING,
    )
    results = compute_all(
        (record,), _BANDS, include_metric_17=False, control_totals=_healthy_control_totals(),
    )
    by_id = {r.id: r for r in results}
    for metric_id in ("M26", "M27"):
        assert by_id[metric_id].status == "SKIP", (
            f"{metric_id} must SKIP on a real, mechanism-absent (no dialogue/adaptation) "
            f"corpus, not render {by_id[metric_id].status!r}"
        )
        assert by_id[metric_id].counted_in_go is False

    m28 = by_id["M28"]
    assert m28.status != "SKIP", (
        "M28's skip_reason path must be unreachable from step 17 forward -- channel 1 is a "
        "decision site for every ordered pair/window regardless of dialogue/adaptation state"
    )
    assert m28.value is not None
    assert m28.detail["channel1_up_decision_sites"] > 0
    assert m28.detail["channel1_down_decision_sites"] > 0


@pytest.mark.slow
def test_m23_prime_m24_m25_m30_measured_on_a_real_counters_on_run():
    """§11 step 7's own verify: M23'/M24/M30 measured for the first time,
    M25 asserted, on a real seed rather than a hand-built fixture. Reads
    against §9.3's frozen band for M23' and the existence/invariant floors
    for M24/M25 -- not a strict pass assertion on M24/M30 (a single-seed
    reading there can legitimately sit anywhere `_pool_negotiation_totals`'s
    own real distribution puts it), but the SHAPE this step's build report
    checks: real, nonzero counter activity occurred, and the hard invariant
    (M25) held.

    **M23' IS asserted against its real, frozen `[0.18, 0.24]` band here,
    not just `status != "SKIP"` and `value >= 0.0`.** That looser pair
    passes on ANY in-`[0, 1]` ratio, including one computed from a swapped
    numerator/denominator or a miscounted (e.g. refused-not-admitted)
    `counters_admitted` -- this run measures `M23p ~= 0.2114` (seed 6,
    `--skip-games`, 1 season), comfortably inside the band, and asserting
    the band itself is what a numerator/denominator regression would
    actually trip. Critic-repro'd: inverting `gm_service.py`'s
    `tally.counters_admitted += 1` to count refused-not-admitted counters
    instead reads `M23p` far outside `[0.18, 0.24]` and this assertion
    catches it; the looser `>= 0.0` check the critic found did not."""
    from tools.gm_harness.season import run_seed

    record = run_seed(
        league_id=1, rng_seed=6, first_season=2026, seasons=1, players_per_team=15,
        skip_games=True, tuning=DEFAULT_TUNING,
    )
    results = compute_all((record,), _BANDS, include_metric_17=False)
    by_id = {r.id: r for r in results}

    m23p = by_id["M23p"]
    assert m23p.status != "SKIP", "a real run must open at least one counter opportunity"
    assert m23p.value is not None and m23p.value >= 0.0
    assert 0.18 <= m23p.value <= 0.24, (
        f"M23' (counter_construction_hit_rate) = {m23p.value!r} is outside the frozen "
        "[0.18, 0.24] band (Sec.9.3, r=0.2040/SE_3=0.0080166) -- a numerator/denominator "
        "regression in counters_admitted/counter_opportunities, not just an out-of-range "
        "single-seed reading (the band already carries that tolerance)."
    )
    assert m23p.status == "PASS"

    m24 = by_id["M24"]
    assert m24.value is not None and m24.value >= 0.0

    m25 = by_id["M25"]
    assert m25.value == 0.0, "M25 is a hard invariant -- any nonzero reading is a real defect"
    assert m25.passed is True

    m30 = by_id["M30"]
    assert m30.value is not None
    assert m30.value >= 2.0, "M30's own baseline floor -- two evaluate_trade calls per package"


def _real_pickle_dir() -> Path | None:
    """Same lookup `test_attribution.py::_real_pickle_dir` uses (duplicated
    here rather than imported -- neither test module exports fixture
    helpers to the other, per this file's own module docstring)."""
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


@pytest.mark.skipif(
    _REAL_PICKLE_DIR is None,
    reason="artifacts/validation_gm/m14_seeds_v2/ not present (gitignored)",
)
def test_family5_metrics_skip_or_read_honestly_on_a_pre_step7_pickle_corpus():
    """The gate-table guarantee this step's own fix protects (§9.4's table,
    `m14_seeds_v2` row): every real, committed pre-step-7 `SeasonRecord`
    pickle lacks all eight of this step's new fields entirely (they were
    added to the dataclass after these files were written), so `compute_
    m23p`/`compute_m24`/`compute_m25`/`compute_m30` must each run to a
    real status on this corpus -- never an `AttributeError` -- via the
    same class-attribute-fallback mechanism `BoundaryRecord.capsheet_with_
    holds_by_team` already relies on (`season.py`'s own `SeasonRecord`
    docstring for the eight new fields).

    **The four metrics do NOT all read `SKIP` here, and asserting that
    they do would assert something false of this design** -- §9.4's own
    table (`docs/design/phase-3-inter-manager-society.md` line ~2280)
    states a DIFFERENT correct reading per metric on this exact corpus:
    only `M23'` SKIPs (its own `counter_opportunities == 0` guard,
    Decision P3-37); `M24` reads a real, honest `FAIL` (`0 <` its `>= 1`
    existence floor -- no counter mechanism existed when this corpus was
    generated, so `0` closed counters is the true reading, not an absent-
    mechanism SKIP); `M25` reads a real, honest `PASS` (`0` violations --
    passes vacuously in substance per §9.4's own table, though its own
    band entry does not set the `vacuous` flag, so `_finalize` renders a
    plain `"PASS"` status string rather than `"PASS (vacuous)"`). Both are
    asserted against `m14_seeds_v2` being `"not a gate"` (§9's own table
    header) -- a FAIL/PASS reading here never contributes to any GO
    verdict.

    **`M30` is a disclosed, out-of-scope accuracy gap, not asserted
    strictly here.** §9.4's table states `M30` should read exactly `2.0`
    on this corpus (both round-0 `evaluate_trade` calls happened on every
    package even before any Phase 3 mechanism existed) -- but this
    corpus's pickles never recorded that count (`evaluations_performed`
    did not exist as a field until this step), so the honest-default fix
    (an empty-tuple fallback, matching every other new field) reads `0.0`
    here instead, a real but historically-inaccurate `FAIL`. Backfilling
    the true historical count would mean inferring un-recorded data from
    `packages_evaluated` alone, a new, undocumented reconstruction this
    fix's own scope (safe defaults + no crash) does not ask for -- flagged
    rather than silently patched. This test asserts only what this fix
    actually guarantees for M30: it computes a real value, never raises."""
    pickle_paths = sorted(_REAL_PICKLE_DIR.glob("seed_*.pkl"))
    if not pickle_paths:
        pytest.skip("no seed_*.pkl files found under m14_seeds_v2/")
    runs = tuple(pickle.load(p.open("rb")) for p in pickle_paths[:3])

    results = compute_all(runs, _BANDS, include_metric_17=False)
    by_id = {r.id: r for r in results}

    assert by_id["M23p"].status == "SKIP"
    assert by_id["M23p"].value is None

    assert by_id["M24"].status == "FAIL"
    assert by_id["M24"].value == 0.0

    assert by_id["M25"].status == "PASS"
    assert by_id["M25"].value == 0.0

    # M30: real value, never raises -- see the docstring's disclosed gap
    # on why this legacy corpus cannot honestly read the theoretical 2.0.
    assert by_id["M30"].value is not None
    assert by_id["M30"].status in ("PASS", "FAIL")


def test_compute_m29_skips_when_control_pass_not_run():
    assert _compute_m29(None, _BANDS).skip_reason is not None


def test_compute_m29_reports_the_precomputed_mismatch_count_verbatim():
    mv = _compute_m29(0, _BANDS)
    assert mv.value == 0.0
    assert mv.skip_reason is None
    mv_nonzero = _compute_m29(3, _BANDS)
    assert mv_nonzero.value == 3.0


@pytest.mark.slow
def test_gm_decision_log_digest_reproduces_byte_identically_on_a_rerun():
    """Decision P3-29(b)'s own verify clause: 1 seed x 2 seasons x
    --skip-games reproduces the exact same per-season digests on an
    independent rerun -- proving this is a real baseline, not a one-off
    snapshot that happened to be captured once."""
    from tools.generate_gm_decision_log_digest import generate_digest

    run_a = generate_digest()
    run_b = generate_digest()
    assert run_a["season_digests"] == run_b["season_digests"]
    assert run_a == run_b


@pytest.mark.slow
def test_gm_decision_log_digest_compares_clean_against_the_committed_baseline_today():
    """The other half of the "compares clean today" verify clause: a fresh
    run at Decision P3-29(b)'s own configuration must match the committed
    `seeds/gm_decision_log_digest.json` exactly, before any Phase 3
    mechanism exists in the codebase.

    **Why this needs BOTH `max_negotiation_rounds=0` AND `head_to_head_
    enabled=False` to hold, not the round cap alone (Decision P3-48).**
    Channels 2/3 are protocol-internal -- their shaded values are only
    ever consulted from inside a negotiation turn, and the round cap
    makes the protocol itself unreachable at round 0, so nullifying the
    protocol nullifies them too. Channel 1 (Decision P3-40) is different:
    it is decided inside `propose_packages`'s own candidate generation,
    UPSTREAM of `respond()`'s own `rounds_remaining <= 0` short-circuit,
    so it is a real read of `state.head_to_head` that happens regardless
    of whether any negotiation turn will ever run. `tools.generate_gm_
    decision_log_digest`'s own `_run_configured_seed` wrapper (which this
    test goes through) now passes `head_to_head_enabled=False` alongside
    `max_negotiation_rounds=0` for exactly this reason -- see that
    module's own docstring, and `compute_m28`'s, for the full finding this
    fix closes (a pre-fix control run measurably produced 130 real
    `ch1-down` and 47 real `ch1-up` non-default target-count sites against
    this exact baseline seed)."""
    from tools.generate_gm_decision_log_digest import _seasons_for_rerun_comparison

    baseline = json.loads(DEFAULT_GM_DECISION_LOG_DIGEST_PATH.read_text(encoding="utf-8"))
    seasons = _seasons_for_rerun_comparison()
    results = compare_decision_log_digest(seasons, baseline)
    assert len(results) == baseline["seasons"]
    for r in results:
        assert r.equal is True, f"season {r.season}: {r.actual_digest} != {r.expected_digest}"


@pytest.mark.slow
def test_negotiation_at_max_rounds_zero_memory_off_reproduces_step_2b_digest_byte_identically():
    """Phase 3 §11 step 7's own dedicated verify clause, named explicitly
    rather than left to `generate_gm_decision_log_digest`'s own tests to
    imply: `run_seed(..., max_negotiation_rounds=0)` -- a PARAMETER, never
    a constant edit (Decision P3-8) -- must reproduce Decision P3-29(b)'s
    committed pre-Phase-3 baseline byte-for-byte, proving round 0 is still
    exactly Phase 2 with the negotiation loop wired in and enabled by
    default everywhere else. The two tests directly above already cover
    this same property through `tools.generate_gm_decision_log_digest`'s
    own wrapper; this test calls `run_seed` itself so the property is
    checked against the real function signature this step actually
    changed, not only against a CLI tool that happens to call it
    correctly.

    **Renamed (Decision P3-48) -- the old name claimed a property `max_
    negotiation_rounds=0` alone no longer holds standalone, once channel 1
    existed.** Both conditions are required, and for different reasons:
    channels 2/3 are protocol-internal, so the round cap reaches them by
    making the negotiation protocol itself unreachable at round 0.
    Channel 1 is supply-side -- its per-partner target count is decided
    inside `propose_packages`, UPSTREAM of `respond()`'s own `rounds_
    remaining <= 0` short-circuit, so it is a real read of `state.head_to_
    head` regardless of whether any negotiation turn will ever run, and
    only memory suppression (`head_to_head_enabled=False`, threaded
    `run_seed -> run_season -> build_ctx`) reaches it. See `compute_m28`'s
    own docstring for the full finding this fix closes, and `tools.
    generate_gm_decision_log_digest`'s own docstring for the parallel fix
    on that module's `run_seed` call."""
    from tools.gm_harness.season import run_seed

    baseline = json.loads(DEFAULT_GM_DECISION_LOG_DIGEST_PATH.read_text(encoding="utf-8"))
    derived_seed = rng_for(baseline["rng_seed"], "validate_gm", "seed", 0).getrandbits(63)
    record = run_seed(
        league_id=baseline["league_id"], rng_seed=derived_seed,
        first_season=baseline["season_year"], seasons=baseline["seasons"],
        players_per_team=baseline["players_per_team"], skip_games=True,
        tuning=DEFAULT_TUNING, max_negotiation_rounds=0, head_to_head_enabled=False,
    )
    results = compare_decision_log_digest(record.seasons, baseline)
    assert len(results) == baseline["seasons"]
    for r in results:
        assert r.equal is True, f"season {r.season}: {r.actual_digest} != {r.expected_digest}"
