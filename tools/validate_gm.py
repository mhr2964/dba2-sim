"""CLI: run N seeded AI-vs-AI GM seasons (in-memory, no Postgres) through
the 22-metric validation harness against `seeds/gm_validation_bands.json`
(docs/design/phase-2-manager-minds.md Sec.10, Decision P2-22's exit
criterion) -- Phase 2 step 10's own last deliverable (architect round-7
report Sec.8, round-8 report Sec.9's three corrections).

Mirrors `tools/validate_sim.py`'s exact shape: `argparse`, `run_validation
(...)` as the importable entry point, `main()`, `if __name__ ==
"__main__": main()`, invoked `python -m tools.validate_gm` from the repo
root. The actual season-running/metric-computing work lives in `tools/
gm_harness/season.py::run_seed` and `tools/gm_harness/metrics.py::
compute_all` -- this module is only the CLI shell, per-seed orchestration,
and report writer.

```
python -m tools.validate_gm --seeds 3 --seasons 5 --out artifacts/validation_gm/<ts>
```

**GO iff every metric with `counted_in_go=True` and a real (non-`SKIP`)
result passes, AND `--skip-games` was not set, AND the run meets the bands
file's own `exit_criterion` (`min_seeds`/`min_seasons`, Decision P2-22).**
`--skip-games` forces NO-GO unconditionally (metric 22 needs real games and
always `SKIP`s without them; a skip-games run is a development loop, never
a gate -- architect round 7 Sec.8's own instruction, enforced here rather
than left to the report reader to notice). **A run with fewer than
`exit_criterion.min_seeds` seeds or fewer than `exit_criterion.min_seasons`
seasons forces NO-GO the same way** (T7b critic round-2 B-NEW-2: the bands
file has always carried this criterion, but nothing enforced it -- a
`--seeds 1 --seasons 1` run could and did report a bare `GO`). Neither
gate blocks the run from executing or computing metrics -- a below-
criterion or `--skip-games` run is still useful for development/debugging,
it just can never report `GO`/exit 0. Vacuous/interpreted/skipped metrics
render their own status label and are excluded from the count regardless
of the flag on their own band entry.

**`--protected-pick-share`, a disclosed limitation.** Architect round 7
Sec.8 names this flag; `tools/gm_harness/league.py::generate_gm_league`
still accepts a `protected_pick_share` parameter (and raises loudly on a
nonzero value -- T6b's own critic-round fix removed silent garbage
generation, it did not remove the parameter). But `tools/gm_harness/
season.py::run_seed` -- built at T7a, before this slice -- never threads a
`protected_pick_share` argument through to `generate_gm_league` at all.
Wiring that through is a `season.py` change, out of this slice's own file
list (`metrics.py`/`bands.py`/`seeds/gm_validation_bands.json`/`tools/
validate_gm.py` only). The flag is therefore accepted here for CLI-shape
parity with round 7's own spec, but only `0.0` (the default, and `run_
seed`'s own only reachable value) is actually usable this slice -- a
nonzero value raises a clear `SystemExit` rather than silently being
ignored. Named, not hidden: round 7 Sec.9's own risk table already flags
"protected picks / swaps never execute in the gating run" as honest,
step-11-scoped behavior.

**`--only-metric-17` is a standalone fast path, not a filtered gating
run.** Metric 17 needs no season simulation at all (report Sec.6.2 item
17) -- this flag calls `metrics.py::compute_m17` directly, skips `run_
seed`/every other metric, prints the one result, and exits without a GO/
NO-GO verdict (there is no verdict to render over one metric).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path

from dba2.core.rng import rng_for
from dba2.domain.team import GmPhilosophy
from dba2.managers.tuning import DEFAULT_TUNING
from dba2.services.gm_service import FUTILITY_STREAK
from dba2.services.media_service import DEFAULT_MEDIA_PHRASE_LIBRARY_PATH
from dba2.services.phrase_service import LoadedPhraseLibrary, load_library
from tools.gm_harness.bands import DEFAULT_BANDS_PATH, BandSpec, load_bands
from tools.gm_harness.media_corpus import MediaCorpusTotals
from tools.gm_harness.metrics import (
    DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH,
    MetricResult,
    _pool_negotiation_totals,
    _pool_phrase_totals,
    _voice_differentiation_tv,
    _within_cohort_binomial_thin_tv,
    compare_decision_log_digest,
    compare_exit_gate_reports,
    compute_all,
    compute_m17,
    load_phase2_exit_gate_baseline,
)
from tools.gm_harness.season import RunRecord, run_seed


def _bands_with_m17_draft_count(bands: BandSpec, drafts: int) -> BandSpec:
    """`--draft-divergence-drafts` overrides M17's own `params["drafts"]`
    (default 100, round 7 Sec.8) without editing the checked-in bands
    file -- used by both the full gating run and `--only-metric-17`."""
    band = bands.metrics["M17"]
    if band.params.get("drafts") == drafts:
        return bands
    new_band = dataclasses.replace(band, params={**band.params, "drafts": drafts})
    return dataclasses.replace(bands, metrics={**bands.metrics, "M17": new_band})


_FAMILY3_METRIC_IDS: tuple[str, ...] = ("M11", "M12", "M13", "M14", "M15", "M16")


def _bands_without_family3_precert(bands: BandSpec) -> BandSpec:
    """`--no-family3-precert`'s own dev-loop escape hatch (Decision P3-47,
    Sec.11 step 9's Family-3 measurability fix, step 5): strips `params.
    gated_value_precertified` from all six Family-3 entries without
    editing the checked-in bands file, forcing every one of them onto its
    own live 3-seed reading regardless of citation validity -- the exact
    class of change (a mechanism change that moves Family-3 behaviour
    without a `GM_TUNING_VERSION` bump) a stale-but-version-matching
    citation could otherwise hide from a dev-loop run."""
    metrics = dict(bands.metrics)
    for metric_id in _FAMILY3_METRIC_IDS:
        band = metrics.get(metric_id)
        if band is None or "gated_value_precertified" not in band.params:
            continue
        params = {k: v for k, v in band.params.items() if k != "gated_value_precertified"}
        metrics[metric_id] = dataclasses.replace(band, params=params)
    return dataclasses.replace(bands, metrics=metrics)


def with_philosophies_disabled(library: LoadedPhraseLibrary) -> LoadedPhraseLibrary:
    """§9.1's control transformation (Decision P3-31, §11 step 16) --
    forces every template's own `philosophies` tuple to `()` (wildcard),
    exactly as if the `philosophies` dimension did not exist in the
    library at all: every philosophy becomes able to select every
    template, on identical footing. The corpus this produces is §9.1's
    "no philosophy-exclusivity structure" corpus.

    **Decision P3-49 -- this corpus is now DUAL-PURPOSE, not a pure
    diagnostic.** `run_validation` measures `_voice_differentiation_tv` on
    the result twice over: once as `tv_control`, folded directly into
    M27's own gated value (`M27' = tv_live - tv_control`, `compute_m27`),
    and once, per cohort, binomial-thinned (never re-run, and never
    regrouped by team id -- **dated correction, §11 Stage D step 16's
    second fix round: this null was originally computed as a team-id
    split of each cohort's own teams, which at only 5 teams/cohort
    concentrated real cross-team situation-mix heterogeneity into one
    half and read 2-3x over the escalation threshold; it is now a
    binomial-thinning split of the cohort's own POOLED template
    distribution instead -- see `_within_cohort_binomial_thin_tv`'s own
    docstring for the full mechanism and why it actually isolates
    sampling noise, matching what §9.1's analytic null models**) as the
    within-cohort split-half A/A escalation check, compared against
    `seeds/gm_validation_bands.json`'s M27 `params.escalation_threshold`
    (`0.0625`). §9.1's own ORIGINAL framing -- this corpus as a pure
    null-control diagnostic that never gated M27's own pass/fail -- is
    partially withdrawn (see that section's own dated correction): the
    allow-list this function disables is not the only philosophy-
    correlated channel into template selection, so measuring it in
    isolation is now part of the real, gated statistic, not just a
    diagnostic check on it.

    **A pure, TEST/MEASUREMENT-ONLY transformation -- never reachable from
    `dba2.services.gm_service`, which never imports this module.** It
    exists solely for `run_validation`'s own control pass (below) and for
    direct unit testing.

    Deliberately built in-memory from an ALREADY-LOADED, already-
    validated real library, never routed back through `phrase_service.py
    ::load_library()`'s own file-loading path -- re-validating it there
    would fail Decision P3-31's own `exclusive_mass_share >= 0.25`
    authoring rule by construction (every exclusive template just became
    a wildcard), which is the whole point of this transformation, not a
    defect in it."""
    return dataclasses.replace(
        library,
        templates=tuple(
            dataclasses.replace(template, philosophies=()) for template in library.templates
        ),
    )


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT_ROOT = _REPO_ROOT / "artifacts" / "validation_gm"
_DEFAULT_DIGEST_PATH = _REPO_ROOT / "seeds" / "gm_decision_log_digest.json"
"""Decision P3-29(b)'s committed control digest -- M29's own baseline
(`--control-check`, on by default). Read here (never inside `metrics.py`,
whose `compute_mN` functions are contractually zero-I/O) and diffed via
`compare_decision_log_digest`, already built at Phase 3 step 2."""

_DEFAULT_MEDIA_CORPUS_PATH = _REPO_ROOT / "artifacts" / "media_corpus.json"
"""Decision P4-95's committed 20a artifact -- M33-M36's own precomputed
input (`--media-corpus`, on by default, mirroring `--control-check`'s own
"read a committed artifact, don't regenerate live" shape). Read here
(never inside `metrics.py`) and handed to `compute_all` as a parsed
`MediaCorpusTotals`, fail-closed on `_load_media_corpus`'s own staleness
check (Decision P4-101)."""


def _load_media_corpus(path: Path) -> MediaCorpusTotals:
    """Decision P4-101's ruled staleness check, FAIL CLOSED: recomputes
    `sha256(seeds/media_phrases.json)` at LOAD time (via the same `dba2.
    services.media_service.DEFAULT_MEDIA_PHRASE_LIBRARY_PATH` the real
    service and `tools/generate_media_corpus.py` both resolve their own
    library path from -- never a second, hand-typed `"seeds/media_phrases.
    json"` literal that could silently drift from the real one) and
    compares it against the committed artifact's own `library_sha`. A
    mismatch means the phrase library has changed since 20a last produced
    this artifact, and M33-M36 would otherwise silently measure a stale
    corpus against the live library's real behavior -- raises loudly
    rather than proceeding, the identical fail-closed shape Decision P4-46
    invented for `behavior_scope`/Family-3 staleness (Decision P4-72
    collapsed that into one shared verdict-plus-reason function; a single
    sha comparison is simple enough not to need one here)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    corpus = MediaCorpusTotals.from_json(payload)
    live_sha = hashlib.sha256(DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.read_bytes()).hexdigest()
    if live_sha != corpus.library_sha:
        raise RuntimeError(
            f"{path} is STALE (Decision P4-101's ruled staleness check): its own "
            f"library_sha ({corpus.library_sha!r}) does not match the live "
            f"{DEFAULT_MEDIA_PHRASE_LIBRARY_PATH}'s own sha256 ({live_sha!r}) -- "
            "seeds/media_phrases.json has changed since tools/generate_media_corpus.py "
            "(20a) last produced this artifact, and M33-M36 would silently measure a "
            "stale corpus against the live library's real behavior. Regenerate the "
            "corpus (tools/generate_media_corpus.py), or pass --no-media-corpus to run "
            "without the media family."
        )
    return corpus


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def _metric_row_json(r: MetricResult) -> dict:
    return {
        "id": r.id,
        "number": r.number,
        "family": r.family,
        "name": r.name,
        "comparator": r.comparator,
        "unit": r.unit,
        "band_low": r.band_low,
        "band_high": r.band_high,
        "value": r.value,
        "per_season": list(r.per_season) if r.per_season is not None else None,
        "status": r.status,
        "passed": r.passed,
        "counted_in_go": r.counted_in_go,
        "strength": r.strength,
        "detail": r.detail,
    }


def _family3_source_row(
    r: MetricResult, original_band, *, family3_precert: bool
) -> dict | None:
    """Decision P3-47 (Sec.11 step 9's Family-3 measurability fix), step 5's
    own informational `family3_source` block -- per Family-3 metric, the
    gated value + its citation's own `source`, this run's own live value +
    margin-in-SE, and whether the gated value sits inside its own 45-seed
    measurement noise (`gated_value - band_low < 2 * sd_across_seeds`,
    computed from data already on the citation/precision blocks).
    **Purely informational -- never reads back into `passed`/`counted_in_
    go`, which `_finalize` alone already decided.**

    `original_band` is the metric's own band from the bands object loaded
    BEFORE `--no-family3-precert` (if set) stripped the citation for
    gating purposes -- the citation's own static fields (`value`/`source`/
    the paired SE) are read from there so this diagnostic still discloses
    "what would have gated" even when the flag forces gating onto the live
    reading. Returns `None` when the metric carries no citation at all (a
    bands file that never asked for Decision P3-47, or M17/other non-
    Family-3 rows this is never called for)."""
    citation = original_band.params.get("gated_value_precertified")
    if citation is None:
        return None

    live_reading = r.detail.get("live_reading")
    if live_reading is not None:
        live_value = live_reading.get("value")
        if r.id == "M14":
            live_se = live_reading.get("se_measured")
            live_margin_in_se = (
                (live_value - r.band_low) / live_se
                if live_value is not None and live_se
                else None
            )
        else:
            live_margin_in_se = (live_reading.get("precision") or {}).get("margin_in_se")
    else:
        # No `live_reading` key means the citation was stripped for gating
        # (`--no-family3-precert`) or this fixture never had one to begin
        # with -- `r.value`/`r.detail` already ARE the live reading in
        # that case (the "absent citation: byte-identical" contract).
        live_value = r.value
        if r.id == "M14":
            live_se = r.detail.get("se_measured")
            live_margin_in_se = (
                (live_value - r.band_low) / live_se
                if live_value is not None and live_se
                else None
            )
        else:
            live_margin_in_se = (r.detail.get("precision") or {}).get("margin_in_se")

    gated_value = citation.get("value")
    if r.id == "M14":
        sd_across_seeds = (original_band.params.get("se_precertified") or {}).get("se_measured")
    else:
        sd_across_seeds = (original_band.params.get("precision") or {}).get("sd_across_seeds")
    inside_own_noise = (
        (gated_value - r.band_low) < 2 * sd_across_seeds
        if gated_value is not None and sd_across_seeds is not None and r.band_low is not None
        else None
    )

    value_source = r.detail.get("value_source", "this_run")
    if not family3_precert:
        value_source = "this_run (forced by --no-family3-precert)"

    return {
        "id": r.id,
        "gated_value": gated_value,
        "gated_source": citation.get("source"),
        "live_value": live_value,
        "live_margin_in_se": live_margin_in_se,
        "value_source": value_source,
        "inside_own_noise": inside_own_noise,
    }


def _family3_source_rows(
    results: tuple[MetricResult, ...], bands_with_family3: BandSpec, *, family3_precert: bool
) -> list[dict]:
    by_id = {r.id: r for r in results}
    rows = []
    for metric_id in _FAMILY3_METRIC_IDS:
        r = by_id.get(metric_id)
        band = bands_with_family3.metrics.get(metric_id)
        if r is None or band is None:
            continue
        row = _family3_source_row(r, band, family3_precert=family3_precert)
        if row is not None:
            rows.append(row)
    return rows


def write_report(
    out_dir: Path,
    *,
    results: tuple[MetricResult, ...],
    go: bool,
    go_reason: str,
    seeds: int,
    seasons: int,
    players_per_team: int,
    rng_seed: int,
    skip_games: bool,
    determinism_check: bool,
    elapsed_seconds: float,
    phase_timings_by_seed: list[dict],
    family3_source: tuple[dict, ...] = (),
    m27_null_result: dict[str, object] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    counted = [r for r in results if r.counted_in_go]
    passed = sum(1 for r in counted if r.passed)
    failed = sum(1 for r in counted if r.passed is False)
    skipped = sum(1 for r in results if r.status == "SKIP")
    excluded = len(results) - len(counted)

    report_json = {
        "generated_at": datetime.now(UTC).isoformat(),
        "go": go,
        "go_reason": go_reason,
        "seeds": seeds,
        "seasons": seasons,
        "players_per_team": players_per_team,
        "rng_seed": rng_seed,
        "skip_games": skip_games,
        "determinism_check": determinism_check,
        "elapsed_seconds": elapsed_seconds,
        "phase_timings_by_seed": phase_timings_by_seed,
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "excluded_from_go_count": excluded,
            "total": len(results),
        },
        "metrics": [_metric_row_json(r) for r in results],
        "family3_source": list(family3_source),
        "m27_null_control": m27_null_result,
    }
    (out_dir / "report.json").write_text(json.dumps(report_json, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# GM validation report -- {seeds} seed(s) x {seasons} season(s), "
        f"{players_per_team} players/team, rng_seed={rng_seed}",
        "",
        f"Generated: {report_json['generated_at']}  |  Elapsed: {elapsed_seconds:.1f}s  |  "
        f"skip_games={skip_games}  |  determinism_check={determinism_check}",
        "",
        f"## Verdict: {'GO' if go else 'NO-GO'}",
        "",
        go_reason,
        "",
        f"**{passed}/{len(counted)} counted metrics pass** ({failed} failed, {skipped} skipped, "
        f"{excluded} excluded from the GO count -- vacuous/interpreted/skipped-by-design)",
        "",
        "| Family | ID | Metric | Value | Band | Status | Counted | Strength |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: (x.family, x.number, x.id)):
        # `statistics.mean` returns a bare `int` for an exact average
        # (e.g. `mean([0, 0]) == 0`), not a `float` -- checking `is not
        # None` rather than `isinstance(..., float)` avoids misrendering
        # a real integer-valued result as "n/a".
        value_str = f"{r.value:.4f}" if r.value is not None else "n/a"
        if r.comparator == "range":
            band_str = f"[{r.band_low}, {r.band_high}]"
        elif r.comparator == "min":
            band_str = f">= {r.band_low}"
        elif r.comparator in ("max", "max_violations"):
            band_str = f"<= {r.band_high}"
        else:
            band_str = "n/a"
        lines.append(
            f"| {r.family} | {r.id} | {r.name} | {value_str} | {band_str} | {r.status} | "
            f"{'yes' if r.counted_in_go else 'no'} | {r.strength} |"
        )

    # §11 step 16 -- M26's per-situation fallback-share breakdown (the
    # step's own verify clause: "M26 <= 0.20 with its per-situation
    # breakdown recorded") and M27's null-control reading (§9.1, Decision
    # P3-31), text-form parity for a `report.md`-only reader -- both
    # numbers already sit verbatim in `report.json` (`_metric_row_json`'s
    # own `detail` dump, plus the top-level `m27_null_control` key).
    # (A fresh, function-scoped `by_id` -- the module's other one, used by
    # the M28 diagnostics sections below, is not yet built at this point
    # in the function.)
    _m26_m27_by_id = {r.id: r for r in results}
    m26_detail = _m26_m27_by_id["M26"].detail if "M26" in _m26_m27_by_id else {}
    if m26_detail.get("per_situation"):
        lines += [
            "",
            "## M26 per-situation fallback share (Decision P3-19/P3-31, §11 step 16)",
            "",
            "| Situation | Selections | Fallback selections | Fallback share |",
            "|---|---|---|---|",
        ]
        for situation, row in m26_detail["per_situation"].items():
            share = row.get("fallback_share")
            share_str = f"{share:.4%}" if share is not None else "n/a"
            lines.append(
                f"| {situation} | {row.get('selections')} | {row.get('fallback_selections')} | "
                f"{share_str} |"
            )

    if m27_null_result is not None:
        def _num(key: str) -> str:
            value = m27_null_result.get(key)
            return f"{value:.6f}" if isinstance(value, float) else "n/a"

        threshold = m27_null_result.get("escalation_threshold")
        below = m27_null_result.get("below_threshold")
        escalation_skip_reason = m27_null_result.get("escalation_skip_reason")
        verdict = (
            "PASS -- below threshold" if below is True
            else "*** ESCALATE -- at or above threshold ***" if below is False
            else f"SKIPPED -- {escalation_skip_reason}" if escalation_skip_reason
            else "no reading (a cohort had zero selections in this arm)"
        )
        lines += [
            "",
            "## M27 paired-difference control pass (Decision P3-49, §9.1/§9.2 correction)",
            "",
            "A control corpus generated with `with_philosophies_disabled` (every template's "
            "`philosophies` tuple forced to `()`), re-running THIS invocation's own real "
            "corpus generation over a bit-identical negotiation trajectory (Decision P3-20). "
            "M27 itself is now the paired difference `tv_live - tv_control`; the escalation "
            "check below is a SEPARATE, genuine noise-only null -- each cohort's own POOLED "
            "template distribution, binomial-thinned in half within the control arm, never "
            "the old (withdrawn) cross-cohort reading (contaminated by the trigger channel's "
            "own philosophy-correlated signal, not noise) **and never a team-id split** "
            "(§11 Stage D step 16's second fix round: a team-id split of each cohort's own "
            "5 teams concentrated real cross-team situation-mix heterogeneity into one half "
            "and read 2-3x over the escalation threshold -- not the sampling-noise-only null "
            "the analytic derivation below models).",
            "",
            f"tv_live = {_num('tv_live')}  |  tv_control = {_num('tv_control')}  |  "
            f"M27' = tv_live - tv_control = {_num('m27_prime')}",
            f"star_maxer_selections (live/control) = "
            f"{m27_null_result.get('star_maxer_selections_live')}/"
            f"{m27_null_result.get('star_maxer_selections_control')}  |  "
            f"egalitarian_selections (live/control) = "
            f"{m27_null_result.get('egalitarian_selections_live')}/"
            f"{m27_null_result.get('egalitarian_selections_control')}",
            "",
            f"within_cohort_null (max of the two cohorts' own binomial-thinned split-half "
            f"TV) = {_num('within_cohort_null')}  |  escalation_threshold = {threshold}",
            f"star_maxer split-half TV = {_num('within_cohort_star_maxer_tv')} "
            f"(first half n={m27_null_result.get('within_cohort_star_maxer_first_half_total')}, "
            f"second half n="
            f"{m27_null_result.get('within_cohort_star_maxer_second_half_total')}) -- "
            f"analytic null = {_num('within_cohort_star_maxer_analytic_null')}",
            f"egalitarian split-half TV = {_num('within_cohort_egalitarian_tv')} "
            f"(first half n="
            f"{m27_null_result.get('within_cohort_egalitarian_first_half_total')}, second half "
            f"n={m27_null_result.get('within_cohort_egalitarian_second_half_total')}) -- "
            f"analytic null = {_num('within_cohort_egalitarian_analytic_null')}",
            f"**{verdict}**",
        ]
        if below is False:
            lines.append(
                "The genuine noise-only null itself is not clean at this event rate -- the "
                "phase escalates rather than sliding the M27 band or adjusting seeds/gm_"
                "phrases.json. This requires a design-level decision (see `_assert_m27_null_"
                "control`, which raises on this condition after this report is written)."
            )

    # Architect round 19 §7 item 6 (Slice 19-A) -- the two new cap-hold
    # diagnostics `compute_m2`/`compute_m10` now populate in `detail`,
    # never gated (neither metric's own `value`/status/band moved above).
    # Rendered as their own small section rather than a new table column,
    # since no other metric's `detail` gets column-level text rendering
    # today (`report.json`'s `_metric_row_json` dumps every metric's whole
    # `detail` verbatim already -- this section is text-form parity for
    # these two specific, newly-added numbers, per round 19's own explicit
    # "both text and JSON forms" instruction).
    by_id = {r.id: r for r in results}
    m2_detail = by_id["M2"].detail if "M2" in by_id else {}
    m10_detail = by_id["M10"].detail if "M10" in by_id else {}
    lines += [
        "",
        "## Cap-hold diagnostics (Slice 19-A, architect round 19 -- reported, not gated)",
        "",
        f"M2 hold_inclusive_violations (5 clean boundaries): "
        f"{m2_detail.get('hold_inclusive_violations')}",
        f"M2 hold_inclusive_violations_by_boundary: "
        f"{m2_detail.get('hold_inclusive_violations_by_boundary')}",
        f"M10 holds_basis: {m10_detail.get('holds_basis')}",
        f"M10 hold_inclusive_mean: {m10_detail.get('hold_inclusive_mean')}",
    ]

    # B4 fix (critic round) -- channel 3's four M28 branch counters and
    # the row-based `mean_abs_shade` diagnostic (B3) now have real harness
    # plumbing (`season.py::SeasonRecord.channel3_*`, `metrics.py::
    # compute_m28`'s pooled `detail`) but M28 itself still SKIPs (channels
    # 1/2 are not built yet), so its own row above renders `SKIP`/`n/a`
    # with nothing else visible. Rendered here as their own text section,
    # same convention as the cap-hold diagnostics immediately above (
    # `report.json`'s `_metric_row_json` already carries the identical
    # numbers verbatim in `detail` -- this is text-form parity for a
    # reader who only opens `report.md`).
    m28_detail = by_id["M28"].detail if "M28" in by_id else {}
    if m28_detail:
        ch3_flip_rate = m28_detail.get("channel3_flip_rate")
        mean_abs_shade = m28_detail.get("mean_abs_shade")
        lines += [
            "",
            "## Channel 3 diagnostics (M28 ch3, Decision P3-36/P3-38 -- since step 17, one of "
            "the three channels M28's own min() gates on; the per-branch conjunct half is "
            "checked here, the value itself is gated in M28's own status)",
            "",
            f"decision sites: {m28_detail.get('channel3_decision_sites')}  |  "
            f"reachability: {m28_detail.get('channel3_reachability')}  |  "
            f"flips: {m28_detail.get('channel3_flips')}",
            f"flip_rate (flips / reachability): "
            f"{f'{ch3_flip_rate:.4%}' if ch3_flip_rate is not None else 'n/a (reachability=0)'}"
            f"  |  measured inert (< {m28_detail.get('channel3_inertness_flip_rate_threshold')}"
            f"): {m28_detail.get('channel3_measured_inert')}",
            f"mean_abs_shade (over {m28_detail.get('channel3_abs_shade_row_count')} head-to-"
            f"head rows): {f'{mean_abs_shade:.6f}' if mean_abs_shade is not None else 'n/a'}"
            f"  |  saturated (> {m28_detail.get('mean_abs_shade_saturation_threshold')}): "
            f"{m28_detail.get('mean_abs_shade_saturated')}",
        ]

        # §11 step 13 -- channel 2's own sibling section, same convention:
        # `report.json`'s `_metric_row_json` already carries these numbers
        # verbatim in `detail`; this is text-form parity for a reader who
        # only opens `report.md`. A small (even zero-rate) flip_rate here
        # is §8.3's own pre-registered, expected reading -- see that
        # section's own text on why a small number is reported as
        # "measured inert," not treated as a build defect.
        ch2_flip_rate = m28_detail.get("channel2_flip_rate")
        lines += [
            "",
            "## Channel 2 diagnostics (M28 ch2, Decision P3-28's replacement/P3-36/P3-38 -- "
            "since step 17, one of the three channels M28's own min() gates on; the per-"
            "branch conjunct half is checked here, the value itself is gated in M28's own "
            "status)",
            "",
            f"decision sites: {m28_detail.get('channel2_decision_sites')}  |  "
            f"reachability: {m28_detail.get('channel2_reachability')}  |  "
            f"flips: {m28_detail.get('channel2_flips')}",
            f"flip_rate (flips / reachability): "
            f"{f'{ch2_flip_rate:.4%}' if ch2_flip_rate is not None else 'n/a (reachability=0)'}"
            f"  |  measured inert (< {m28_detail.get('channel2_inertness_flip_rate_threshold')}"
            f"): {m28_detail.get('channel2_measured_inert')}",
        ]

        # §11 step 17 (Decision P3-38/P3-40/P3-41) -- channel 1's own
        # sibling section, same convention. `ladder_identity_holds` is
        # asserted with no tolerance by `_assert_m28_step17_invariants`
        # (below); rendered here too, text-form parity for a `report.md`-
        # only reader. The escalation line below is EMITTED BY THE
        # HARNESS, gated on `max_observed > 0` (§11 step 17's own explicit
        # instruction: "never written by a human who has read a
        # paragraph") -- when reachability is 0 and `max_observed` is
        # ALSO exactly 0.0, nothing is printed here, because that
        # signature is the never-written-field bug the triage assertion
        # (item 3, below) already raises on, not a threshold question.
        lines += [
            "",
            "## Channel 1 diagnostics (M28 ch1, Decision P3-36/P3-38/P3-40/P3-41 -- reported, "
            "not gated; the triage/ladder assertions below ARE gated)",
            "",
            f"up: decision sites {m28_detail.get('channel1_up_decision_sites')}  |  "
            f"reachability {m28_detail.get('channel1_up_reachability')}  |  "
            f"flips {m28_detail.get('channel1_up_flips')}  |  "
            f"ladder identity holds: {m28_detail.get('channel1_up_ladder_identity_holds')}  |  "
            f"max_observed (executions): {m28_detail.get('channel1_up_max_observed')}",
            f"down: decision sites {m28_detail.get('channel1_down_decision_sites')}  |  "
            f"reachability {m28_detail.get('channel1_down_reachability')}  |  "
            f"flips {m28_detail.get('channel1_down_flips')}  |  "
            f"ladder identity holds: {m28_detail.get('channel1_down_ladder_identity_holds')}  |  "
            f"max_observed (far_reject_streak): {m28_detail.get('channel1_down_max_observed')}",
            f"far_rejects_observed: {m28_detail.get('far_rejects_observed')}  |  "
            f"m18_trades_executed: {m28_detail.get('m18_trades_executed')}  |  "
            f"triage_pass: {m28_detail.get('triage_pass')}",
        ]
        if (
            m28_detail.get("channel1_down_reachability") == 0
            and (m28_detail.get("channel1_down_max_observed") or 0.0) > 0.0
        ):
            lines.append(
                f"FUTILITY_STREAK = {FUTILITY_STREAK} was never reached at this event rate "
                "(ch1_down reachability=0, but max_observed far_reject_streak="
                f"{m28_detail.get('channel1_down_max_observed')} -- the field IS being "
                "written). Report to the user for a threshold decision; never lower "
                "FUTILITY_STREAK to make this gate go green (§9.4 item 1)."
            )

    if family3_source:
        lines += [
            "",
            "## Family 3 -- gated reading vs. this run's own reading (Decision P3-47)",
            "",
            "| ID | Gated value (45 precertified seeds) | This run's live value | "
            "Live margin (SE) | Value source | Inside its own noise? |",
            "|---|---|---|---|---|---|",
        ]
        for row in family3_source:
            gated_str = (
                f"{row['gated_value']:.4f}" if row.get("gated_value") is not None else "n/a"
            )
            live_str = f"{row['live_value']:.4f}" if row.get("live_value") is not None else "n/a"
            margin = row.get("live_margin_in_se")
            margin_str = f"{margin:+.2f}" if margin is not None else "n/a"
            noise = row.get("inside_own_noise")
            noise_str = "yes" if noise is True else ("no" if noise is False else "n/a")
            lines.append(
                f"| {row['id']} | {gated_str} | {live_str} | {margin_str} | "
                f"{row.get('value_source')} | {noise_str} |"
            )
        lines += [
            "",
            "(Informational only -- never affects `passed`/`counted_in_go`, which "
            "`_finalize` alone already decided. \"Inside its own noise\" means "
            "`gated_value - band_low < 2 * sd_across_seeds`, i.e. the band sits inside "
            "the metric's own 45-seed measurement noise, per Decision P3-47's own "
            "root-cause finding for M12/M16 -- docs/ledger/phase-3.md rounds 36-38.)",
        ]

    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _assert_m28_step17_invariants(results: tuple[MetricResult, ...]) -> None:
    """§11 step 17's own two "a builder cannot route around this" checks
    (Decision P3-38/P3-40/P3-41, round 8's items (v)/(vi)) -- a pure
    function of the already-computed `results` tuple, called from `run_
    validation` AFTER `write_report(...)` so this run's own diagnostics
    are on disk before any raise (a build that fails this assertion still
    leaves a readable `report.md`/`report.json` behind, not just a
    traceback).

    **The ladder identity, no tolerance.** `channel1_up_flips ==
    channel1_up_reachability` and `channel1_down_flips == channel1_down_
    reachability` must hold EXACTLY on a wired build -- both counts are
    the same predicate evaluated by two independent implementations (the
    harness's own reachability read off `ctx.head_to_head`'s row, and the
    production ladder's own return value), so an inequality means the
    shipped ladder and the documented ladder disagree (§9.2's nit 2,
    round 8). `compute_m28` already reports `channel1_up_ladder_identity_
    holds`/`channel1_down_ladder_identity_holds` as booleans in `detail`
    -- this assertion is what makes them load-bearing evidence rather
    than unread JSON.

    **The triage gate.** `detail["triage_down_violation"]`/`["triage_up_
    violation"]` (Decision P3-41, computed by `compute_m28` itself) being
    `True` means the never-written-field signature fired: the events that
    must write `far_reject_streak`/`executions` demonstrably occurred
    this run, and the field's own maximum is exactly `0.0`. `compute_m28`
    only ever REPORTS these two booleans (never ANDs them into `extra_
    pass`) -- this function is where they actually gate, by raising.

    Raises `RuntimeError` naming the offending numbers, on any of the
    four conditions. No-ops (M28 absent from `results`, e.g. a run
    computed against a bands file that dropped the metric) rather than
    raising on a missing row -- this assertion has nothing to check in
    that case, and it is not this function's job to require M28 exist.

    **Required-key guard (critic finding B2, third pass).** Once M28 IS
    present, its `detail` dict is required to carry all six keys this
    function reads -- a producer-side rename or dropped field must fail
    LOUD here, not silently agree with itself via two absent `.get()`
    reads both returning `None`. `compute_m28` always populates all six
    on every real (non-SKIP) result; a `SKIP` result (e.g. `--no-
    control-check` or a mechanism-absent corpus) legitimately omits
    them, so a `SKIP` status is the one case this guard steps aside for."""
    m28 = next((r for r in results if r.id == "M28"), None)
    if m28 is None:
        return
    detail = m28.detail
    if m28.status == "SKIP":
        return

    required_keys = (
        "channel1_up_flips", "channel1_up_reachability",
        "channel1_down_flips", "channel1_down_reachability",
        "triage_down_violation", "triage_up_violation",
    )
    missing = [key for key in required_keys if key not in detail]
    if missing:
        raise RuntimeError(
            "§11 step 17's invariant checks cannot run: M28's detail dict is missing "
            f"{missing!r} on a non-SKIP result -- `compute_m28` and this assertion have "
            "drifted apart (a rename or dropped field), which is exactly the drift this "
            "guard exists to catch instead of passing vacuously (critic finding B2)."
        )

    up_flips = detail.get("channel1_up_flips")
    up_reachability = detail.get("channel1_up_reachability")
    if up_flips != up_reachability:
        raise RuntimeError(
            "§11 step 17's ladder identity failed for channel1_up: flips="
            f"{up_flips!r} != reachability={up_reachability!r} -- the shipped target-count "
            "ladder and the documented one (Decision P3-40) disagree."
        )

    down_flips = detail.get("channel1_down_flips")
    down_reachability = detail.get("channel1_down_reachability")
    if down_flips != down_reachability:
        raise RuntimeError(
            "§11 step 17's ladder identity failed for channel1_down: flips="
            f"{down_flips!r} != reachability={down_reachability!r} -- the shipped target-count "
            "ladder and the documented one (Decision P3-40) disagree."
        )

    if detail.get("triage_down_violation") is True:
        raise RuntimeError(
            "§11 step 17's triage gate failed (down): far_rejects_observed="
            f"{detail.get('far_rejects_observed')!r} >= 1 but channel1_down_max_observed="
            f"{detail.get('channel1_down_max_observed')!r} == 0.0 -- far_reject_streak is not "
            "being written despite real far rejects occurring this run (Decision P3-41)."
        )

    if detail.get("triage_up_violation") is True:
        raise RuntimeError(
            "§11 step 17's triage gate failed (up): m18_trades_executed="
            f"{detail.get('m18_trades_executed')!r} >= 1 but channel1_up_max_observed="
            f"{detail.get('channel1_up_max_observed')!r} == 0.0 -- executions is not being "
            "written despite real trades executing this run (Decision P3-41)."
        )


def _m27_escalation_min_n(k: int, escalation_threshold: float) -> int:
    """§11 Stage D step 16's own SKIP-below-a-data-floor guard for the
    within-cohort escalation check (same category as Decision P3-37's
    guards throughout this file's `compute_m23`/`compute_m26`/`compute_
    m28` -- a diagnostic that needs real scale to mean anything, gated on
    a data floor, never on a design-level band change). **This is a
    diagnostic convenience, not a new statistical claim: it does not
    change what M27' measures or the `0.125`/`0.0625` thresholds
    themselves, both already ruled by Decision P3-49.**

    Derivation, so a future reader can reproduce the returned floor from
    the formula alone: §9.1's own analytic null is `E[TV_null] =
    sqrt((K-1)/(pi*n))`. **Recalibrated (§11 Stage D step 16's second fix
    round, correcting the first fix round's own miscalibration)**: an
    8-reading critic sample (4 corpus shapes x 2 cohorts) measured the
    empirical binomial-thinned null against this analytic figure and found
    the ratio ranges `0.822x-1.364x` -- NOT reliably above `1.0x`, which
    refutes the first fix round's `1.1x-1.4x-always-above` claim (that
    claim rested on only 2 data points). There is no real "overdispersion"
    to correct for here, and compounding an overdispersion multiplier with
    a separate safety margin double-counted the same uncertainty. The
    floor instead applies ONE margin, `MARGIN = 1.5`, directly to the
    analytic formula. This is real but modest headroom, not a generous
    cushion -- `1.5` sits only ~1.10x above the observed sample's own
    maximum ratio (`1.364x`), on 8 readings with no confidence bound
    computed -- and it is a deliberate, disclosed choice: wide enough to
    clear the observed range once, narrow enough that the floor still
    saturates below the shipped library's exit-gate `n` (see below),
    which the prior, doubly-inflated floor did not:

        require:  MARGIN * sqrt((k-1) / (pi*n)) <= escalation_threshold
        =>        n >= MARGIN^2 * (k-1) / (pi * escalation_threshold^2)

    Evaluated at the shipped `escalation_threshold = 0.0625`: at the two
    smoke-test shapes this file's own real measured `K` produces (`K=47,
    n=1,982`, from a 1-seed/1-season run; and `K=58, n=6,178`, from a
    3-seed/1-season run), the floor comes out to `8,434` and `10,451`
    respectively -- both still far above the corpus actually pooled,
    correctly SKIPping the check there. At step 18's real exit-gate shape
    (3 seeds x 5 seasons: `K=64, n=33,949` and `K=65, n=31,340`), the
    floor comes out to `11,551` and `11,735` -- BELOW the real `n` by a
    ~2.7x-2.9x margin, so the check now correctly EVALUATES (does not
    SKIP) at exactly the scale it exists to validate, unlike the prior
    `1.5x * 1.75x` compounded floor, which grew faster than `n` and
    SKIPped even at the exit-gate shape -- the disguised-always-skip
    failure mode this guard was built to avoid. `K` is read from the
    caller's own measured cohort (never assumed to be `30`, the original
    derivation's illustrative value only) because it varies run to run
    with how many distinct templates the corpus actually exercises. Since
    the shipped library holds exactly 88 templates (`K <= 88` always), the
    floor is structurally capped at `ceil(MARGIN^2 * 87 / (pi *
    escalation_threshold^2)) = 15,952` regardless of corpus shape --
    permanently below the exit-gate's real `n` of 31,000-34,000, so the
    always-skip failure mode is foreclosed structurally, not merely
    empirically at the shapes measured so far."""
    if k < 2:
        return 0
    margin = 1.5
    return math.ceil(margin ** 2 * (k - 1) / (math.pi * escalation_threshold ** 2))


def _m27_escalation_gate(
    *, within_cohort_null: float | None, escalation_threshold: float,
    star_k: int, star_n: int, egal_k: int, egal_n: int,
) -> tuple[bool | None, str | None]:
    """Owns the within-cohort escalation check's own raise-vs-pass-vs-skip
    decision, factored out of `run_validation`'s body so it is directly
    unit-testable (this file's own "a builder cannot route around this"
    convention -- see `_assert_m28_step17_invariants`'s identical
    reasoning for existing as a standalone function). Returns
    `(below_threshold, skip_reason)`:

    - `skip_reason` is set (and `below_threshold` is `None`) when EITHER
      cohort's own real `K` is degenerate (`< 2` distinct templates --
      `_m27_escalation_min_n(k < 2)` returns `0`, no floor at all, and
      `_within_cohort_binomial_thin_tv` reads `TV=0.0` for a single-
      template cohort, which would otherwise silently read as a below-
      threshold PASS on a check whose whole point is "never silently
      pass" -- so this is checked explicitly, before the floor, rather
      than left to fall through).
    - `skip_reason` is also set when EITHER cohort's own pooled selection
      count is below `_m27_escalation_min_n`'s own floor for that
      cohort's real `K` -- the corpus is too small for this diagnostic to
      mean anything at this scale (§11 Stage D step 16's fix round; not a
      new SKIP category, the same shape as every other Decision P3-37
      data-floor guard in this file).
    - Otherwise `below_threshold` mirrors `within_cohort_null <
      escalation_threshold` (`None` if `within_cohort_null` itself could
      not be computed -- a cohort had zero selections in this arm, or
      every binomial-thinned coin flip landed the same way -- a SEPARATE,
      pre-existing no-reading case this floor does not change)."""
    if star_k < 2 or egal_k < 2:
        return None, (
            "degenerate cohort -- the within-cohort escalation check needs at least 2 "
            f"distinct templates per cohort to mean anything: star_maxer K={star_k}, "
            f"egalitarian K={egal_k} -- a single-template cohort has no split-half variance "
            "to measure and would otherwise silently read as a below-threshold PASS."
        )
    star_floor = _m27_escalation_min_n(star_k, escalation_threshold)
    egal_floor = _m27_escalation_min_n(egal_k, escalation_threshold)
    if star_n < star_floor or egal_n < egal_floor:
        return None, (
            "corpus too small for the within-cohort escalation check to mean anything at "
            f"this scale (_m27_escalation_min_n's own derivation): star_maxer n={star_n} "
            f"(floor {star_floor} at K={star_k}), egalitarian n={egal_n} (floor {egal_floor} "
            f"at K={egal_k}) -- the real validation of this diagnostic happens at step 18's "
            "full-scale exit-gate run, not at this smoke-test scale."
        )
    if within_cohort_null is None:
        return None, None
    return within_cohort_null < escalation_threshold, None


def _assert_m27_null_control(m27_null_result: dict[str, object] | None) -> None:
    """Decision P3-49's own repointed escalation rule (§11 Stage D step 16
    fix round, correcting §9.1's withdrawn cross-cohort control): "the
    within-cohort split-half A/A null must come in below [the escalation_
    threshold] ... or the genuine noise-only null itself is not clean at
    this event rate, and the phase escalates rather than sliding the
    band." Raises `RuntimeError` naming the offending numbers -- never
    silently passes, never adjusts `seeds/gm_validation_bands.json`'s M27
    floor or `seeds/gm_phrases.json`'s library to force a pass. Called
    from `run_validation` AFTER `write_report(...)`, mirroring `_assert_
    m28_step17_invariants`'s own placement immediately above, so this
    run's own diagnostics (including the control-pass section `write_
    report` renders) are on disk before any raise.

    **No longer checks the old cross-cohort control reading at all** --
    that statistic (`tv_control`, now `m27_null_result["tv_control"]`) is
    load-bearing, real, philosophy-correlated signal folded into M27'
    itself (`compute_m27`'s own paired difference), never a noise-only
    null; checking it against `escalation_threshold` was the exact defect
    this fix closes (a zero-`exclusive_mass_share` library's own `tv_
    control` empirically reads `0.148-0.166`, comfortably >= `0.0625`,
    which would have escalated on every real library regardless of
    whether the library itself is healthy). The check now reads `m27_
    null_result["within_cohort_null"]` -- the max of `star_maxer`'s and
    `egalitarian`'s own two binomial-thinned split-half TV readings
    (`_within_cohort_binomial_thin_tv`, over each cohort's own POOLED
    template distribution -- **dated correction, §11 Stage D step 16's
    second fix round: this was originally a team-id split, withdrawn
    because it concentrated real cross-team situation-mix heterogeneity
    into one half at only 5 teams/cohort and read 2-3x over the
    escalation threshold**) within the SAME control arm, a genuine
    sampling-noise-only comparison.

    No-ops when `m27_null_result` is `None` (`--no-control-check`, so no
    control pass ran this invocation) or when `below_threshold` is `None`
    -- two DISTINCT reasons `_m27_escalation_gate` can produce that same
    `None` (both a corpus-scale problem, never raised on here: neither is
    evidence that the noise-only null itself is unclean): (1)
    `within_cohort_null` could not be computed at all -- one or both
    cohorts had zero selections in this arm; or (2) `_m27_escalation_
    gate`'s own minimum-n floor was not met (`m27_null_result.get(
    "escalation_skip_reason")` names the real `n`/floor/`K` in that case
    -- §11 Stage D step 16's fix round, a SKIP-below-a-data-floor guard in
    the same category as every other Decision P3-37 guard in this file,
    not a change to what M27 itself gates)."""
    if m27_null_result is None:
        return
    if m27_null_result.get("below_threshold") is False:
        raise RuntimeError(
            "Decision P3-49's M27 within-cohort split-half A/A escalation fired: null "
            f"{m27_null_result.get('within_cohort_null')!r} >= escalation_threshold "
            f"{m27_null_result.get('escalation_threshold')!r} (star_maxer split-half TV="
            f"{m27_null_result.get('within_cohort_star_maxer_tv')!r}, egalitarian split-half "
            f"TV={m27_null_result.get('within_cohort_egalitarian_tv')!r}) -- the genuine "
            "noise-only null itself is not clean at this event rate. The phase escalates "
            "rather than sliding the band or adjusting the library; this requires a design-"
            "level decision, not a build-level fix."
        )


# The nine Family-5 (negotiation, Phase 3 §9/§11 step 7) metric ids -- `M23-
# M30` plus `M23p` (`metrics.py`'s own ASCII spelling of M23', §11 step 7's
# own comment). Every one of these has its mechanism built by step 18
# (Decision P3-37), so a `SKIP` on any of them at the exit gate is a wiring
# bug, not a benign absence.
_FAMILY5_METRIC_IDS = frozenset(
    {"M23", "M23p", "M24", "M25", "M26", "M27", "M28", "M29", "M30"}
)

# The metrics permanently excluded from Decision P3-32's exit-gate count,
# each for its own documented reason -- M21 predates Phase 3 entirely
# (Family 4, "unfalsifiable as literally written", §13.1), M23 is Decision
# P3-43 (the constructor-repair statistic that cannot fail as written), M29
# is Decision P3-46 (comparison (a) of its own two folded comparisons is
# structurally unable to read 0 mismatches on a live mechanism -- the WHOLE
# metric moves, since both comparisons fold into one value before
# `compute_m29` ever sees them). Each of these three carries `counted_in_go:
# false` IN THE BANDS FILE ITSELF (`seeds/gm_validation_bands.json`) --
# unconditional, for every run, every corpus, every seed. That is what
# makes them "permanent" and distinct from the set below.
_PHASE3_PERMANENTLY_UNCOUNTED_IDS = frozenset({"M21", "M23", "M29"})

# Metric ids whose `counted_in_go` is legitimately DATA-DEPENDENT at
# RUNTIME, via `MetricValue.counted_in_go_override` -- `seeds/gm_
# validation_bands.json` declares `counted_in_go: true` for every one of
# these, and whether a real run actually counts it depends on what it
# measures, not on a fixed design-time exclusion. Today this is M34 alone
# (Decision P4-105, amended by P4-108: three conjunctive gates over the
# committed `artifacts/media_corpus.json`, currently FAILING all three on
# every real gate run -- see `compute_m34`'s own docstring -- but capable
# of passing if that corpus is ever regenerated to clear them). This
# assertion must never treat EITHER state (counted or uncounted) of an id
# in this set as evidence of a wiring bug -- only an id outside both this
# set and `_PHASE3_PERMANENTLY_UNCOUNTED_IDS` turning up uncounted, or an
# id inside `_PHASE3_PERMANENTLY_UNCOUNTED_IDS` turning up counted, is.
_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS = frozenset({"M34"})

# Metric ids whose absence from the counted set is legitimate ONLY when the
# run has no media corpus at all (`--no-media-corpus`, or simply no
# `artifacts/media_corpus.json`-committed corpus present yet because step
# 20a was never run) -- Decision P4-100 item 12's own disclosed, expected
# condition: "a `--no-media-corpus` run (or any run before 20a exists)
# drops the counted population by up to six." M34 is one of those six,
# already covered above by `_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS` for its
# own separate, always-legitimate reason (its `counted_in_go` is data-
# dependent regardless of corpus presence); these three are the other
# three, whose SKIP (per `compute_m33`/`compute_m35`/`compute_m36`'s own
# `media_corpus is None` guard, Decision P4-34's "no mechanism data -> SKIP,
# never fabricate PASS/FAIL" rule) is caused SPECIFICALLY by corpus absence.
# When the corpus IS present, these three must be held to the SAME strict
# rule as every ordinary metric -- an uncounted M33/M35/M36 with a real
# corpus loaded is exactly the silent wiring regression this assertion
# exists to catch, so this set is only ever unioned in below when the
# caller reports the corpus absent, never treated as permanently safe to
# ignore the way `_PHASE3_PERMANENTLY_UNCOUNTED_IDS` is.
_PHASE3_MEDIA_CORPUS_ABSENT_UNCOUNTED_IDS = frozenset({"M33", "M35", "M36"})

# Fifth fix round (this dispatch) -- the three sets below replace what used
# to be a single blanket `if skip_games or not control_check or below_
# gate_scale or not determinism_check: return` early-return at the top of
# `_assert_phase3_exit_gate_counted_total`. That blanket shape disabled
# EVERY check in the function for these four flags/conditions, including
# the arity check, the `restored` check, and the `media_corpus_fabricated`
# check -- none of which have anything to do with run scale or CLI dev-
# loop flags; an independent critic reproduced this live (`--seeds 3
# --seasons 5 --no-determinism-check`, a REAL gate-scale run, silently
# returning GO even with M21 wrongly counted or M30 missing from `results`
# entirely). Those three checks now run UNCONDITIONALLY (below); only the
# `unexpected_uncounted` composition check -- the one genuinely-scale/flag-
# dependent check, since it is the only one asking "which metrics were
# legitimately allowed to be absent from the counted set at all" -- still
# varies with `skip_games`/`control_check`/`determinism_check`, via an
# explicit, named allowed-uncounted id set per flag (the same shape
# `_PHASE3_MEDIA_CORPUS_ABSENT_UNCOUNTED_IDS` above already used), so a
# relaxed run still checks something real (any uncounted id is explainable
# by a documented cause) rather than skipping the composition check
# outright. `below_gate_scale` is NOT one of these three named sets --
# see `_assert_phase3_exit_gate_counted_total`'s own docstring and the
# `if below_gate_scale:` branch inside it for why a live adversarial probe
# this fix round proved that flag needs a full step-aside for this one
# conjunct instead, unlike the other three.

# Legitimate ONLY under `--skip-games` -- `compute_m22`'s own `season.
# win_totals is None` guard (`metrics.py`) SKIPs unconditionally without
# real games, a documented CLI dev-loop escape hatch (this module's own
# docstring), never a wiring defect.
_PHASE3_SKIP_GAMES_UNCOUNTED_IDS = frozenset({"M22"})

# Legitimate ONLY under `--no-control-check` -- `compute_m27`/`compute_
# m28`/`compute_m29` all hard-SKIP without the control pass by design
# (Decision P3-49's own `control_by_philosophy_template is None`/`control_
# totals is None` guards, `metrics.py`), the same documented CLI escape
# hatch `_assert_m28_step17_invariants` and `_assert_no_family5_skip_at_
# exit_gate` already step aside for.
_PHASE3_NO_CONTROL_CHECK_UNCOUNTED_IDS = frozenset({"M27", "M28", "M29"})

# Legitimate ONLY under `--no-determinism-check` -- `compute_m6`'s own "no
# duplicate-seed runs present in `runs`" guard SKIPs whenever no re-run
# pair exists, which `--no-determinism-check` produces even at REAL gate
# scale (`--seeds 3 --seasons 5`, where `below_gate_scale` alone cannot
# catch it -- the exact live crash this dispatch's own required
# adversarial testing reproduced and closed).
_PHASE3_NO_DETERMINISM_CHECK_UNCOUNTED_IDS = frozenset({"M6"})


def _min_gate_scale(bands: BandSpec) -> tuple[int, int]:
    """Single source of truth for Decision P2-22's `exit_criterion.min_seeds`/
    `min_seasons` (default `3`/`5`, matching this module's own docstring
    example, `--seeds 3 --seasons 5`) -- `_decide_go`'s own "a development-
    loop run can never gate" NO-GO rule and `_assert_phase3_exit_gate_
    counted_total`'s own below-gate-scale no-op (below) both need the
    IDENTICAL definition of "this run is not a real exit-gate run."
    Factored out here, round-22 fix dispatch (BLOCKING 1), so the two
    checks read one literal pair rather than each carrying its own copy
    that could silently drift apart."""
    min_seeds = bands.exit_criterion.get("min_seeds", 3)
    min_seasons = bands.exit_criterion.get("min_seasons", 5)
    return min_seeds, min_seasons


def _is_below_gate_scale(*, seeds: int, seasons: int, bands: BandSpec) -> bool:
    """N4 (this fix round) -- `_min_gate_scale` already factored the
    `min_seeds`/`min_seasons` LITERALS into one place so `_decide_go` and
    `_assert_phase3_exit_gate_counted_total`'s own below-gate-scale
    relaxation could not read different numbers, but the `<` COMPARISON
    itself was still written out twice (`_decide_go`'s own `if seeds <
    min_seeds or seasons < min_seasons`, and `run_validation`'s `below_
    gate_scale=(seeds < _min_seeds or seasons < _min_seasons)`), leaving
    the operator (`<` vs `<=`) free to drift between the two call sites
    even with the literals shared. Factored out here too -- one predicate,
    both callers."""
    min_seeds, min_seasons = _min_gate_scale(bands)
    return seeds < min_seeds or seasons < min_seasons


def _assert_phase3_exit_gate_counted_total(
    results: tuple[MetricResult, ...], *, skip_games: bool, control_check: bool,
    total_bands: int, media_corpus_present: bool = True, below_gate_scale: bool = False,
    determinism_check: bool = True,
) -> None:
    """§11 step 18 assertion (i), plus round 12's own conjunct (§9, the
    paragraph beginning "And round 12 adds the conjunct that stops this
    assertion being satisfied the wrong way") -- corrected from `33` to
    `32` by Decision P3-46 (§11 step 9), and corrected AGAIN here (this
    fix round) once Phase 4's own Family 6/7 metrics (M31-M36) pushed the
    real bands file to 41 entries and Decision P4-105 (amended P4-108)
    gave M34 a `counted_in_go` that is legitimately DATA-DEPENDENT rather
    than fixed -- a single hardcoded expected total can no longer be
    right for both a real, healthy media corpus and today's real, always-
    failing one. **What this checks now, restated in full because the
    previous version's own hardcoded `32` had never actually been
    exercised against a real bands file and would have hard-crashed the
    very next real gate run with a message describing the WRONG failure
    (this exact "mirror failure" shape, one level up):**

    1. **`len(results) == total_bands`** -- every band in `seeds/gm_
       validation_bands.json` produced a real `MetricResult`, none
       silently dropped from `compute_all`'s own output entirely (a
       DIFFERENT failure mode than "present but uncounted," and one the
       composition check below cannot see, because a missing id never
       appears in `uncounted_ids` at all).
    2. **Composition of `uncounted_ids`** -- partitioned into exactly two
       kinds, both named by id rather than only counted by size (round
       12's own fix, preserved): `_PHASE3_PERMANENTLY_UNCOUNTED_IDS`
       (`{M21, M23, M29}`, `counted_in_go: false` in the bands file
       itself, unconditionally -- these must ALWAYS be uncounted, never
       "restored") and `_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS` (`{M34}`,
       `counted_in_go: true` in the bands file, flipped at runtime by
       `MetricValue.counted_in_go_override` -- these may be EITHER
       counted or uncounted on a given run, and neither state is a
       defect). **When `media_corpus_present=False`**, `_PHASE3_MEDIA_
       CORPUS_ABSENT_UNCOUNTED_IDS` (`{M33, M35, M36}`) is unioned into
       the allowed-uncounted set for this call only (Decision P4-100 item
       12's own disclosed condition -- a run with no media corpus makes
       `compute_m33`/`compute_m35`/`compute_m36` all hard-SKIP by design,
       never a wiring defect). **When `media_corpus_present=True`** (the
       default, and every pre-Phase-4 call site's own implicit assumption)
       these three ids get NO special treatment -- an uncounted M33/M35/
       M36 with a real corpus loaded is flagged exactly like any other
       unexpected uncounted metric. **Anything else turning up uncounted,
       or either of M21/M23/M29 turning up counted, is the wiring bug this
       assertion exists to catch.**

    `validate_gm.py`'s own GO line reports `all {len(counted)} counted
    metrics pass` against a DYNAMIC denominator (`_decide_go`'s own
    `counted = [r for r in results if r.counted_in_go]`) -- a `SKIP`
    silently removes a metric from the exit criterion while the run still
    prints `GO`. This function is the extra, hard-failing guard on top of
    that existing, correct GO/NO-GO logic, never a rewrite of it.

    **Fifth fix round (this dispatch) -- restructured from a blanket early-
    return to three-unconditional-plus-one-scoped.** Every prior round back
    to round-22 added its own flag/scale relaxation as a blanket `return`
    at the top of this function (`if skip_games or not control_check or
    below_gate_scale or not determinism_check: return`), which disabled
    EVERY check below for that flag -- including the arity check (1, above)
    and the `restored`/`media_corpus_fabricated` halves of the composition
    check (2, above), none of which have anything to do with run scale or
    CLI dev-loop flags at all. An independent critic reproduced the real
    consequence live: `--seeds 3 --seasons 5 --no-determinism-check` (a
    REAL gate-scale run) returned GO even with M21 wrongly counted or M30
    missing from `results` entirely, because the blanket guard swallowed
    the checks that would have caught either. Now:

    - **The arity check and both halves of the composition check's
      "always-uncounted"/"media-fabrication" conjuncts (`restored`,
      `media_corpus_fabricated`) run UNCONDITIONALLY** -- none of
      `skip_games`/`control_check`/`below_gate_scale`/`determinism_check`
      can skip them. A missing metric, a restored M21/M23/M29, or a
      fabricated M33/M35/M36 is a wiring bug at ANY scale or flag
      combination, never a legitimate scale/flag effect.
    - **Only `unexpected_uncounted` -- the composition check's "is every
      OTHER uncounted id explained" half -- stays scale/flag-dependent.**
      `skip_games`/`not control_check`/`not determinism_check` each union
      in a named, per-flag allowed-uncounted id set
      (`_PHASE3_SKIP_GAMES_UNCOUNTED_IDS`, `_PHASE3_NO_CONTROL_CHECK_
      UNCOUNTED_IDS`, `_PHASE3_NO_DETERMINISM_CHECK_UNCOUNTED_IDS`,
      defined above, mirroring `_PHASE3_MEDIA_CORPUS_ABSENT_UNCOUNTED_IDS`'s
      existing shape) -- checking something real (any OTHER uncounted id
      must still be explainable) rather than skipping the composition
      check outright. `below_gate_scale=True` steps this ONE conjunct
      aside entirely instead (see below) -- it is the one condition of the
      four this round found is NOT enumerable this way.
    - `--skip-games` unions in `M22` (`compute_m22`'s own `season.win_
      totals is None` guard, `metrics.py`) -- a development-loop run,
      never a gate (this module's own docstring rule), since M22 needs
      real games and always `SKIP`s without them.
    - `--no-control-check` unions in `M27`/`M28`/`M29` (re-verification's
      own B1 finding -- `compute_m27`/`compute_m28`/`compute_m29` all
      hard-SKIP without the control pass, Decision P3-49's own SKIP
      guards) -- `_assert_m28_step17_invariants` above already steps aside
      on M28's own SKIP for the identical reason.
    - `--no-determinism-check` unions in `M6` (found by an earlier round's
      own required adversarial testing, trying to break the below-gate-
      scale fix with a DIFFERENT flag combination: `--seeds 3 --seasons 5
      --no-determinism-check` is a real gate-scale run, so `below_gate_
      scale` alone would not catch it) -- `compute_m6`'s own docstring
      states the cause directly: "no duplicate-seed runs present in
      `runs`" -> `SKIP`, "determinism-check was off, or only one run
      total."
    - **`below_gate_scale=True` steps `unexpected_uncounted` aside
      entirely** (round-22 fix dispatch, BLOCKING 1 -- critic-reproduced
      live crash: `python -m tools.validate_gm --seeds 1 --seasons 1
      --no-determinism-check --no-media-corpus` raised, citing `M10`/
      `M13`/`M16`/`M32`/`M6` as "unexpected" uncounted, none of which are
      media ids at all). Below `exit_criterion.min_seeds`/`min_seasons`
      (`_min_gate_scale`, `_is_below_gate_scale`, above), M6/M10/M13/M16's
      own cohort-size guards (`n_contend`/`n_rebuild`/`n_cohort_teams`
      etc.) and M31/M32's own agency-data-thinness guards (`compute_m31`'s
      `total_observations == 0`, `compute_m32`'s `max_transitions is
      None`, `metrics.py`) all shrink for reasons that have nothing to do
      with wiring at small scale. **This round tried the same named-set
      shape used for the other three flags first** (a `_PHASE3_BELOW_
      GATE_SCALE_UNCOUNTED_IDS = {M6, M10, M13, M16, M31, M32}` set,
      matching the exact composition round-22's own live repro observed)
      -- but this round's own required adversarial testing broke it: a
      REAL `run_validation` call at `--seeds 1 --seasons 0` (not a
      synthetic fixture) legitimately shrinks a much larger set (`M7`/
      `M8a`/`M8b`/`M9`/`M18`/`M19`/`M20`/`M22`/`M23p`/`M26`/`M30`, in
      addition to the six above), because nearly every metric that pools
      season data can legitimately SKIP or read a degenerate value at
      sufficiently small scale, each for its OWN metric-specific guard,
      not one shared, enumerable cause. This confirms round-22's own
      original finding ("NO composition invariant this strict is true
      below real gate scale, for ANY metric family") rather than
      superseding it, so `unexpected_uncounted` steps aside entirely below
      gate scale, same as it always has -- the fix this round makes is
      that the OTHER three checks (arity, `restored`, `media_corpus_
      fabricated`) no longer step aside with it. **M31/M32 correction
      (re-verification's own N2 finding, replacing this docstring's
      earlier, imprecise "their own SKIP branches" claim):** M31 does not
      actually SKIP at the scale round-22 verified this against, and
      M32's SKIP condition is narrower than "no checkpoint ever fired" --
      it is "no player transitioned the ladder." The real, verified reason
      neither needs its own dedicated flag (the way M22/M27-M29/M6 each
      do) is that no CLI flag can suppress agency checkpoint data at real
      gate scale -- an M31/M32 SKIP, when it happens at all, can only be
      this below-gate-scale phenomenon. `_PHASE3_MEDIA_CORPUS_ABSENT_
      UNCOUNTED_IDS` above is UNCHANGED by this and still does its own,
      separate job: a real-gate-scale run (`--seeds 3 --seasons 5`) with
      `--no-media-corpus` is NOT below gate scale, so this does not fire
      for it, and the media-specific allowance is still exactly what lets
      M33/M35/M36 through in that case.

    Also no-ops (the WHOLE function, still -- this one predates every fix
    round and is unrelated to the four flags above) when `results` does
    not carry all three of M21/M23/M29 by id -- a partial/synthetic
    `results` tuple (this file's own test suite builds these routinely,
    e.g. `(_metric_result("M1"),)`, to exercise `run_validation`'s wiring
    without a real 41-metric computation) is not the full bands file this
    assertion guards and has nothing for it to check, the same shape as
    `_assert_m28_step17_invariants`'s own "M28 absent -> no-op" guard
    above.

    `total_bands` is the caller's own `len(bands.metrics)` (`run_
    validation`'s real, loaded `BandSpec`) -- read from the actual bands
    object in play for this run, never a hardcoded literal, so this
    assertion tracks the bands file's own shape rather than needing a
    manual bump every time a family is added to it."""
    ids_present = {r.id for r in results}
    if not _PHASE3_PERMANENTLY_UNCOUNTED_IDS.issubset(ids_present):
        return

    # --- Unconditional (this fix round): no flag or scale can legitimately
    # cause a metric to be missing from `compute_all`'s own output, a
    # permanently-uncounted id to be restored, or a media-corpus-absent-
    # allowed id to be fabricated -- so none of `skip_games`/`control_
    # check`/`below_gate_scale`/`determinism_check` may skip these checks.
    if len(results) != total_bands:
        raise RuntimeError(
            "§11 step 18 assertion (i) failed: expected exactly "
            f"{total_bands} metric results (one per `seeds/gm_validation_bands.json` "
            f"entry), got {len(results)}: {sorted(ids_present)}. A metric is missing from "
            "`compute_all`'s own output entirely -- a different failure than being present "
            "but uncounted."
        )

    counted = [r for r in results if r.counted_in_go]
    uncounted_ids = {r.id for r in results if not r.counted_in_go}

    # Decision P4-100 item 12's disclosed condition (BLOCKING 1, round-22
    # fix dispatch): with no media corpus, `M33`/`M35`/`M36` legitimately
    # join the allowed-uncounted set for THIS call only -- never
    # permanently, unlike `_PHASE3_PERMANENTLY_UNCOUNTED_IDS`, so a real
    # corpus present on a later run still holds these three to the normal
    # strict rule.
    media_corpus_allowance = (
        _PHASE3_MEDIA_CORPUS_ABSENT_UNCOUNTED_IDS if not media_corpus_present else frozenset()
    )
    restored = sorted(_PHASE3_PERMANENTLY_UNCOUNTED_IDS - uncounted_ids)
    # NIT 2 (round-22 fix dispatch, critic's report): the media-corpus-absent
    # allowance above is otherwise one-directional -- it only excuses
    # M33/M35/M36 turning up UNCOUNTED when the corpus is absent, never
    # checks the other direction. An id in `media_corpus_allowance` that
    # turns up COUNTED anyway, while `media_corpus_present=False`, means a
    # value got produced with no real corpus loaded to produce it from --
    # `compute_m33`/`compute_m35`/`compute_m36`'s own `media_corpus is
    # None` guard makes that impossible today, but this mirrors `restored`
    # above (the identical "permanently-excused id turning up on the wrong
    # side" shape for `_PHASE3_PERMANENTLY_UNCOUNTED_IDS`) so a future
    # regression in that guard is still caught here rather than only by
    # the metric's own test. Empty, and inert, whenever `media_corpus_
    # allowance` itself is empty (i.e. `media_corpus_present=True`).
    media_corpus_fabricated = sorted(media_corpus_allowance - uncounted_ids)

    # --- Scale/flag-dependent (this fix round -- the ONLY conjunct that
    # legitimately varies with these four): which OTHER ids may be
    # uncounted at all.
    #
    # `skip_games`/`not control_check`/`not determinism_check` each map to
    # exactly ONE specific, verified metric/family tied to exactly one CLI
    # flag (`_PHASE3_SKIP_GAMES_UNCOUNTED_IDS`/`_PHASE3_NO_CONTROL_CHECK_
    # UNCOUNTED_IDS`/`_PHASE3_NO_DETERMINISM_CHECK_UNCOUNTED_IDS`), so each
    # is unioned in as an explicit, named allowance -- checking something
    # real (any OTHER uncounted id is still unexpected) rather than
    # skipping the composition check outright.
    #
    # `below_gate_scale`, in contrast, is NOT enumerable this way -- a live
    # adversarial probe this fix round (`--seeds 1 --seasons 0`, a real
    # CLI run through `run_validation`, not a synthetic fixture) found
    # `M7`/`M8a`/`M8b`/`M9`/`M18`/`M19`/`M20`/`M22`/`M23p`/`M26`/`M30`
    # uncounted too -- a much larger set than `_PHASE3_BELOW_GATE_SCALE_
    # UNCOUNTED_IDS` above names, because nearly every metric that pools
    # season data can legitimately SKIP or read a degenerate value at
    # sufficiently small scale, each for its OWN metric-specific guard, not
    # one shared cause. This confirms round-22's own original finding
    # ("NO composition invariant this strict is true below real gate
    # scale, for ANY metric family") rather than superseding it -- below
    # gate scale, `unexpected_uncounted` steps aside entirely, same as it
    # always has. This is still not "don't check anything below gate
    # scale," unlike the pre-this-round blanket return: the three
    # unconditional checks above (arity, `restored`, `media_corpus_
    # fabricated`) still run and still catch a real regression at any
    # scale, including below gate scale -- only this one composition
    # conjunct, whose entire premise (a specific, enumerable, non-scale
    # cause per uncounted id) does not hold below gate scale, steps aside.
    if below_gate_scale:
        unexpected_uncounted: list[str] = []
    else:
        allowed_uncounted = (
            _PHASE3_PERMANENTLY_UNCOUNTED_IDS
            | _PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS
            | media_corpus_allowance
        )
        if skip_games:
            allowed_uncounted |= _PHASE3_SKIP_GAMES_UNCOUNTED_IDS
        if not control_check:
            allowed_uncounted |= _PHASE3_NO_CONTROL_CHECK_UNCOUNTED_IDS
        if not determinism_check:
            allowed_uncounted |= _PHASE3_NO_DETERMINISM_CHECK_UNCOUNTED_IDS
        unexpected_uncounted = sorted(uncounted_ids - allowed_uncounted)

    if unexpected_uncounted or restored or media_corpus_fabricated:
        dynamic_state = {
            id_: (id_ in uncounted_ids) for id_ in sorted(_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS)
        }
        # N6 (this fix round, critic's nit list): this clause is only
        # meaningful when the corpus was actually absent -- with a corpus
        # present, `media_corpus_allowance` (and so `media_corpus_
        # fabricated`) is always empty, and appending the sentence anyway
        # falsely implied a media-corpus-absent condition that never held.
        media_absent_clause = (
            "; media-corpus-absent-allowed metric(s) unexpectedly counted with no corpus "
            f"loaded: {media_corpus_fabricated or 'none'}"
            if not media_corpus_present else ""
        )
        raise RuntimeError(
            "§11 step 18 assertion (i)'s round-12 conjunct failed (Decision P3-32/P3-46, "
            "extended by Decision P4-105/P4-108, and by P4-100 item 12's media-corpus-absent "
            "allowance): expected exactly "
            f"{sorted(_PHASE3_PERMANENTLY_UNCOUNTED_IDS)} to be ALWAYS uncounted, "
            f"{sorted(_PHASE3_DYNAMIC_OVERRIDE_UNCOUNTED_IDS)} to be uncounted-or-counted "
            f"depending on data (currently: {dynamic_state}, uncounted=True), and -- only "
            f"because media_corpus_present={media_corpus_present} -- "
            f"{sorted(media_corpus_allowance) or 'none'} to be allowed uncounted too. "
            f"Flag-based allowances active this call: skip_games={skip_games} "
            f"(-> {sorted(_PHASE3_SKIP_GAMES_UNCOUNTED_IDS) if skip_games else []}), "
            f"no-control-check={not control_check} "
            f"(-> {sorted(_PHASE3_NO_CONTROL_CHECK_UNCOUNTED_IDS) if not control_check else []}), "
            f"no-determinism-check={not determinism_check} (-> "
            f"{sorted(_PHASE3_NO_DETERMINISM_CHECK_UNCOUNTED_IDS) if not determinism_check else []}"
            f"), below_gate_scale={below_gate_scale} (-> "
            f"{'unexpected_uncounted not checked below gate scale' if below_gate_scale else []}). "
            f"Unexpected uncounted metric(s): {unexpected_uncounted or 'none'}; "
            f"permanently-uncounted metric(s) unexpectedly counted: {restored or 'none'}"
            f"{media_absent_clause}. "
            f"This run counted {len(counted)}/{total_bands}: {sorted(r.id for r in counted)}."
        )


def _assert_no_family5_skip_at_exit_gate(
    results: tuple[MetricResult, ...], *, control_check: bool
) -> None:
    """§11 step 18 assertion (ii) -- "No Family-5 metric carries a `skip_
    reason` at this gate." Every one of the nine Family-5 metrics
    (`_FAMILY5_METRIC_IDS`) has its mechanism built by step 18 (Decision
    P3-37), so a `SKIP` here is a wiring bug wearing a green hat, not a
    benign absence -- this is the assertion that gives M28's own benign
    SKIPs at steps 1/7/9/12/13 their teeth (§9.2.1 lists per metric exactly
    which steps it may legitimately SKIP at; step 18 is not among them for
    any of the nine). `MetricResult` has no separate `skip_reason` field --
    `_finalize` folds it into `status == "SKIP"` -- so that is what this
    checks.

    **No-ops under `--no-control-check`** (re-verification's own B1 finding
    -- caught by actually running `python -m tools.validate_gm --skip-games
    --no-control-check`, which tracebacked here before this fix, not just
    by reading the code). `compute_m27`/`compute_m28`/`compute_m29` all
    hard-SKIP without the control pass by design (Decision P3-49), exactly
    the same legitimate-SKIP case `_assert_m28_step17_invariants` already
    steps aside for -- this function checked the OTHER eight Family-5
    metrics but never stepped aside for the three the flag is specifically
    documented to SKIP, and the sibling assertion this one is modeled on
    already had the right shape for this (`if m28.status == "SKIP":
    return`) two functions above.

    Naturally ALSO a no-op (vacuously passes) when none of the nine ids are
    present in `results`, which is what every small/synthetic fixture in
    this file's own test suite looks like -- no separate gate needed for
    that case, unlike `_assert_phase3_exit_gate_counted_total` above,
    because an empty intersection can never produce a false failure here."""
    if not control_check:
        return
    skipped = sorted(
        r.id for r in results if r.id in _FAMILY5_METRIC_IDS and r.status == "SKIP"
    )
    if skipped:
        raise RuntimeError(
            f"§11 step 18 assertion (ii) failed (Decision P3-32/P3-37): Family-5 metric(s) "
            f"{skipped} carry status=SKIP at the exit gate. Every Family-5 mechanism is built "
            "by this point in the build order, so a SKIP here is a wiring bug, not a benign "
            "absence -- per-metric legitimate SKIP steps are enumerated in §9.2.1 and step 18 "
            "is not among them for any of the nine."
        )


def _decide_go(
    results: tuple[MetricResult, ...], *, seeds: int, seasons: int, skip_games: bool,
    bands: BandSpec,
) -> tuple[bool, str]:
    """The GO/NO-GO decision rule, factored out of `run_validation` (step 9,
    Phase 3 §11 Stage B) so the same rule can be applied twice in one
    invocation: once to a provisional `results` tuple (to know what THIS
    run's own `go`/`go_reason` would read, for Decision P3-29(a)'s
    comparison against the Phase-2 baseline -- both are top-level compared
    fields), and once, finally, to the `results` actually written to disk.
    Pure otherwise -- unchanged from the logic this replaces."""
    counted = [r for r in results if r.counted_in_go]
    all_counted_pass = all(r.passed for r in counted)
    min_seeds, min_seasons = _min_gate_scale(bands)
    exit_criterion_source = bands.exit_criterion.get("source", "Decision P2-22")
    if skip_games:
        return False, (
            "NO-GO: --skip-games forces NO-GO unconditionally -- metric 22 (win-total SD) "
            "needs real games and always SKIPs without them; a skip-games run is a "
            "development loop, never a gate (architect round 7 Sec.8)."
        )
    if _is_below_gate_scale(seeds=seeds, seasons=seasons, bands=bands):
        return False, (
            f"NO-GO: exit criterion requires >= {min_seeds} seeds x >= {min_seasons} seasons "
            f"({exit_criterion_source}), this run used {seeds} seed(s) x {seasons} season(s) -- "
            "a development-loop run can never gate."
        )
    if not all_counted_pass:
        failing = [r.id for r in counted if r.passed is False]
        return False, f"NO-GO: {len(failing)} counted metric(s) failed: {', '.join(failing)}."
    return True, (
        f"GO: all {len(counted)} counted metrics pass across {seeds} seed(s) x "
        f"{seasons} season(s)."
    )


# ---------------------------------------------------------------------------
# run_validation -- the importable entry point
# ---------------------------------------------------------------------------


def run_validation(
    *,
    seeds: int,
    seasons: int,
    players_per_team: int,
    rng_seed: int,
    season_year: int,
    skip_games: bool,
    determinism_check: bool,
    control_check: bool = True,
    family3_precert: bool = True,
    protected_pick_share: float,
    draft_divergence_drafts: int,
    bands_path: Path,
    out_dir: Path,
    digest_path: Path = _DEFAULT_DIGEST_PATH,
    media_corpus_path: Path | None = None,
    use_media_corpus: bool = True,
) -> tuple[tuple[MetricResult, ...], bool]:
    if protected_pick_share != 0.0:
        raise SystemExit(
            "--protected-pick-share is accepted for CLI-shape parity with architect round 7 "
            "Sec.8 but tools/gm_harness/season.py::run_seed (built at T7a, before this slice) "
            "never threads it through to generate_gm_league -- only 0.0 is reachable this "
            "slice. Wiring it through is a season.py change, out of this slice's own scope "
            "(metrics.py/bands.py/seeds/gm_validation_bands.json/validate_gm.py only)."
        )

    bands_with_family3 = _bands_with_m17_draft_count(
        load_bands(bands_path), draft_divergence_drafts
    )
    # `--no-family3-precert` (Decision P3-47, Sec.11 step 9's Family-3
    # measurability fix, step 5): the dev-loop escape hatch for catching a
    # mechanism change that moves Family-3 behaviour without a `GM_TUNING_
    # VERSION` bump -- the exact class of thing a stale-but-version-
    # matching citation could hide. `bands_with_family3` (citation intact)
    # is kept around separately so the `family3_source` diagnostic below
    # can still disclose "what would have gated" even when this flag
    # forces the real gate onto the live reading.
    bands = (
        bands_with_family3
        if family3_precert
        else _bands_without_family3_precert(bands_with_family3)
    )
    tuning = DEFAULT_TUNING
    t0 = time.perf_counter()

    # §11 step 16 -- dialogue is now wired live into every real-seed run
    # this invocation makes (Decision P3-19/P3-31), so M26/M27 measure
    # real data instead of permanently SKIPping. Loaded once, here, and
    # threaded verbatim to every main-loop `run_seed` call below --
    # `phrase_library=None` stays the byte-identical default everywhere
    # ELSE this module calls `run_seed` (the determinism re-check and the
    # M29 control pass both intentionally keep dialogue off, matching
    # their own pre-existing, unrelated verify clauses). Decision P3-20's
    # own independent "phrase" RNG salt means turning dialogue on here
    # cannot perturb any OTHER metric's own RNG stream or DecisionRecord
    # (`tests/unit/services/test_gm_service_phrase.py::test_dialogue_on_
    # produces_byte_identical_decision_records_to_dialogue_off`).
    phrase_library = load_library()

    runs: list[RunRecord] = []
    phase_timings_by_seed: list[dict] = []
    for i in range(seeds):
        seed = rng_for(rng_seed, "validate_gm", "seed", i).getrandbits(63)
        t_seed0 = time.perf_counter()
        record = run_seed(
            league_id=i + 1, rng_seed=seed, first_season=season_year, seasons=seasons,
            players_per_team=players_per_team, skip_games=skip_games, tuning=tuning,
            phrase_library=phrase_library,
        )
        runs.append(record)
        elapsed = time.perf_counter() - t_seed0
        phase_timings_by_seed.append(
            {
                "seed_index": i, "rng_seed": seed, "elapsed_seconds": elapsed,
                "per_season_phase_timings": [s.phase_timings for s in record.seasons],
            }
        )
        print(f"seed {i + 1}/{seeds} (rng_seed={seed}): {seasons} season(s) in {elapsed:.1f}s")

    if determinism_check:
        det_seed = rng_for(rng_seed, "validate_gm", "seed", 0).getrandbits(63)
        print(
            "determinism re-check (invariant 6): re-running seed 0 twice under "
            "--skip-games to diff jsonl_lines byte-for-byte..."
        )
        t_det_a0 = time.perf_counter()
        det_run_a = run_seed(
            league_id=1, rng_seed=det_seed, first_season=season_year, seasons=seasons,
            players_per_team=players_per_team, skip_games=True, tuning=tuning,
        )
        elapsed_det_a = time.perf_counter() - t_det_a0
        t_det_b0 = time.perf_counter()
        det_run_b = run_seed(
            league_id=1, rng_seed=det_seed, first_season=season_year, seasons=seasons,
            players_per_team=players_per_team, skip_games=True, tuning=tuning,
        )
        elapsed_det_b = time.perf_counter() - t_det_b0
        # Nit 9 (T7b critic round-2): these two `run_seed` calls are real,
        # costly wall-clock work that the pre-fix `phase_timings_by_seed`
        # never recorded at all -- `elapsed_seconds` (the report's own
        # total) already included their cost via `t0`/`time.perf_counter()`
        # below, so the per-seed breakdown silently didn't sum to the total
        # a reader would reconcile it against. Recorded here the same shape
        # as a real seed's own entry, `seed_index` naming which determinism
        # run it is rather than a numeric index.
        phase_timings_by_seed.append(
            {
                "seed_index": "determinism_a", "rng_seed": det_seed,
                "elapsed_seconds": elapsed_det_a,
                "per_season_phase_timings": [s.phase_timings for s in det_run_a.seasons],
            }
        )
        phase_timings_by_seed.append(
            {
                "seed_index": "determinism_b", "rng_seed": det_seed,
                "elapsed_seconds": elapsed_det_b,
                "per_season_phase_timings": [s.phase_timings for s in det_run_b.seasons],
            }
        )
        determinism_runs: tuple[RunRecord, ...] = (det_run_a, det_run_b)
    else:
        determinism_runs = ()

    # Decision P4-95/P4-101 -- M33-M36's own precomputed input, loaded here
    # (never inside `metrics.py`) exactly like `phrase_library` above and
    # `digest_path`'s baseline below: `--no-media-corpus` forces `None`
    # unconditionally (the media family hard-SKIPs, Decision P4-34) and
    # never raises, regardless of what path was passed. `_load_media_
    # corpus` fails closed on a `library_sha` mismatch rather than
    # silently reading a stale corpus (Decision P4-101's ruled check).
    #
    # **The DEFAULT path (no `--media-corpus` flag at all) is read only if
    # it actually exists** -- a repo that has never run 20a gets a `None`
    # corpus and a SKIP, never an error, because nobody asked for a
    # specific file. **An EXPLICITLY-passed `--media-corpus <path>` is a
    # different case and must NOT degrade the same way**: the caller named
    # a specific file, so a missing/unloadable file there is a hard error,
    # not a silent `None` -- letting it fall through to `None` would make
    # all four M33-M36 metrics SKIP with no visible cause, silently
    # shrinking the counted-metric denominator by 3-4 (the exact risk
    # Decision P4-100 item 12 already disclosed).
    media_corpus: MediaCorpusTotals | None = None
    if use_media_corpus:
        if media_corpus_path is not None:
            if not media_corpus_path.exists():
                raise RuntimeError(
                    f"--media-corpus {media_corpus_path} does not exist. An explicitly-"
                    "passed --media-corpus path must exist and load successfully -- pass "
                    "--no-media-corpus instead if you mean to run without the media family, "
                    "or omit --media-corpus entirely to fall back to the default path "
                    f"({_DEFAULT_MEDIA_CORPUS_PATH}) if present."
                )
            media_corpus = _load_media_corpus(media_corpus_path)
        elif _DEFAULT_MEDIA_CORPUS_PATH.exists():
            media_corpus = _load_media_corpus(_DEFAULT_MEDIA_CORPUS_PATH)

    digest_mismatches: int | None = None
    exit_gate_baseline: dict | None = None
    exit_gate_config_matches = False
    control_totals: dict[str, float] | None = None
    m27_null_result: dict[str, object] | None = None
    m27_control_by_philosophy_template: dict[str, dict[str, int]] | None = None
    if control_check:
        # M29 (control_equivalence) -- a control pass at max_negotiation_
        # rounds=0 AND head_to_head_enabled=False reduces the negotiation
        # loop to exactly Phase 2's own round-0-only accept/reject read
        # AND suppresses the memory channel 1's per-partner target count
        # reads upstream of any negotiation turn (Decision P3-48), so it
        # must reproduce the committed Decision P3-29(b) baseline byte-
        # for-byte. Neither condition alone is sufficient: the round cap
        # nullifies channels 2/3 by making the negotiation protocol itself
        # unreachable, but channel 1 runs in `propose_packages`, upstream
        # of that protocol, so it is only reached by suppressing memory
        # directly. Read the baseline's OWN config (rng_seed/season_year/
        # seasons/players_per_team) rather than this run's -- M29 diffs
        # against a fixed historical baseline, not against whatever config
        # this particular gate run was invoked with, and `compare_
        # decision_log_digest` only checks the seasons present in the
        # baseline anyway (extra seasons in `control_run` beyond the
        # baseline's own `seasons` are simply never compared).
        print("control-equivalence pass (M29): re-running the Decision P3-29(b) baseline "
              "seed at max_negotiation_rounds=0 with head_to_head suppressed...")
        baseline = json.loads(digest_path.read_text(encoding="utf-8"))
        control_seed = rng_for(baseline["rng_seed"], "validate_gm", "seed", 0).getrandbits(63)
        t_control0 = time.perf_counter()
        # `agency_enabled=False`, `media_enabled=False`, explicit, since Phase
        # 4 Sec.11 Stage A step 1 built both flags (Decision P4-2). This is the
        # OTHER call site Decision P4-55 requires pinning -- `tools.generate_
        # gm_decision_log_digest._run_configured_seed` pins the same two flags
        # when it WRITES `seeds/gm_decision_log_digest.json`, but this control
        # run is what READS that baseline and diffs it (`compare_decision_log_
        # digest`, below) every time M29 runs. Leaving this site on `run_seed`'s
        # own `True` defaults is currently a no-op (no Phase 4 mechanism reads
        # either flag yet), but the moment one does, this control run would
        # start exercising it live while the frozen baseline it's diffed
        # against still reflects the flag suppressed -- reporting a digest
        # mismatch that is a wiring artifact of an unpinned control run, not a
        # real regression (exactly the Sec.13.1-named "Phase 3 step-17 shape"
        # failure this project has already hit once, for `head_to_head_
        # enabled`, via Decision P3-48).
        control_run = run_seed(
            league_id=baseline["league_id"], rng_seed=control_seed,
            first_season=baseline["season_year"], seasons=baseline["seasons"],
            players_per_team=baseline["players_per_team"], skip_games=True, tuning=tuning,
            max_negotiation_rounds=0, head_to_head_enabled=False,
            agency_enabled=False, media_enabled=False,
        )
        elapsed_control = time.perf_counter() - t_control0
        phase_timings_by_seed.append(
            {
                "seed_index": "control", "rng_seed": control_seed,
                "elapsed_seconds": elapsed_control,
                "per_season_phase_timings": [s.phase_timings for s in control_run.seasons],
            }
        )
        comparisons = compare_decision_log_digest(control_run.seasons, baseline)
        digest_mismatches = sum(1 for c in comparisons if not c.equal)

        # M28's control-pass `extra_pass` conjunct (§11 step 17) reuses
        # this SAME control run rather than a second one -- pooled through
        # the identical `_pool_negotiation_totals` helper `compute_all`'s
        # own main pass already uses, over a single-run tuple.
        control_totals, _control_census = _pool_negotiation_totals((control_run,))

        # Decision P3-49 (superseding §11 step 16's original null-control
        # framing, §9.1) -- M27's own control arm, now DUAL-PURPOSE: (1)
        # `tv_control`, folded into M27's own gated value as `M27' =
        # tv_live - tv_control` (`compute_m27`, via `m27_control_by_
        # philosophy_template` below), and (2) the source corpus for the
        # within-cohort split-half A/A escalation check (a regrouping of
        # this SAME corpus's own per-team counts, no second run). NOT the
        # M29 control pass above (that one runs at max_negotiation_
        # rounds=0 against a fixed historical baseline seed, to diff a
        # byte-identical digest) -- this is a SEPARATE pass, re-running
        # THIS invocation's own real corpus generation (same per-seed rng
        # derivation, same seasons/players_per_team/skip_games this run
        # was invoked with) against a variant phrase library with `with_
        # philosophies_disabled` (above) applied: every template's own
        # `philosophies` tuple forced to `()`, exactly as if the
        # philosophies dimension did not exist. Decision P3-20's
        # independent phrase-RNG salt guarantees this corpus traverses a
        # BIT-IDENTICAL negotiation trajectory to the live corpus -- the
        # frame-census check immediately below is the mechanical proof of
        # that, not just an assumption.
        print(
            f"M27 null-control pass (§9.1): re-running this invocation's own {seeds} "
            f"seed(s) x {seasons} season(s) with the phrase library's philosophies filter "
            "disabled..."
        )
        disabled_library = with_philosophies_disabled(phrase_library)
        t_null0 = time.perf_counter()
        null_control_runs: list[RunRecord] = []
        for i in range(seeds):
            null_seed = rng_for(rng_seed, "validate_gm", "seed", i).getrandbits(63)
            null_control_runs.append(
                run_seed(
                    league_id=i + 1, rng_seed=null_seed, first_season=season_year,
                    seasons=seasons, players_per_team=players_per_team,
                    skip_games=skip_games, tuning=tuning, phrase_library=disabled_library,
                )
            )
        elapsed_null = time.perf_counter() - t_null0
        phase_timings_by_seed.append(
            {
                "seed_index": "m27_null_control", "rng_seed": rng_seed,
                "elapsed_seconds": elapsed_null,
                "per_season_phase_timings": [
                    s.phase_timings for record in null_control_runs for s in record.seasons
                ],
            }
        )
        (
            null_selections_total, _null_fallback_selections, null_by_situation,
            _null_fallback_by_situation, null_by_philosophy_template,
        ) = _pool_phrase_totals(tuple(null_control_runs))
        m27_control_by_philosophy_template = null_by_philosophy_template

        # Decision P3-49's own load-bearing mechanical guarantee -- the
        # whole paired-difference estimator rests on the live and control
        # arms traversing a BIT-IDENTICAL negotiation trajectory (Decision
        # P3-20's independent phrase-RNG salt: `tests/unit/services/
        # test_gm_service_phrase.py::test_dialogue_on_produces_byte_
        # identical_decision_records_to_dialogue_off`). The cheapest real
        # proxy for that identity, checked here rather than left as a
        # comment: every real negotiation frame that reaches a phrase-
        # selection call does so identically in both arms (only WHICH
        # template gets chosen differs, never WHETHER a call happens or
        # what situation it was for) -- so the two arms' own total
        # selection count, per-situation breakdown, AND per-cohort
        # (star_maxer/egalitarian) selection totals must all match exactly
        # (the cohort totals are computed here, not just reported below,
        # because they are the exact denominators `M27'` divides by -- a
        # mismatch there would silently corrupt the paired-difference's own
        # validity in a way the coarser total/per-situation check alone
        # could miss). Fallback-selection counts are NOT checked here:
        # which templates are eligible legitimately differs between the two
        # arms (that is the whole point of disabling the philosophies
        # filter), so a differing fallback share is expected, not a defect.
        (
            live_selections_total, _live_fallback_selections, live_by_situation,
            _live_fallback_by_situation, live_by_philosophy_template,
        ) = _pool_phrase_totals(tuple(runs))
        tv_live, live_star_total, live_egal_total = _voice_differentiation_tv(
            live_by_philosophy_template
        )
        tv_control, control_star_total, control_egal_total = _voice_differentiation_tv(
            null_by_philosophy_template
        )
        if (
            live_selections_total != null_selections_total
            or dict(live_by_situation) != dict(null_by_situation)
            or live_star_total != control_star_total
            or live_egal_total != control_egal_total
        ):
            raise RuntimeError(
                "Decision P3-49's frame-census check failed: the live and philosophies-"
                f"disabled control arms disagree on real negotiation trajectory (live "
                f"selections={live_selections_total!r}/by_situation={dict(live_by_situation)!r}"
                f"/star_maxer={live_star_total!r}/egalitarian={live_egal_total!r} vs. control "
                f"selections={null_selections_total!r}/by_situation={dict(null_by_situation)!r}"
                f"/star_maxer={control_star_total!r}/egalitarian={control_egal_total!r}) -- the "
                "paired-difference estimator's own bit-identity assumption does not hold the "
                "way Decision P3-20/the architect's finding believed. This is a real finding "
                "requiring a design-level decision, not a build-level workaround."
            )

        m27_prime = (
            tv_live - tv_control if tv_live is not None and tv_control is not None else None
        )

        # Decision P3-49's own repointed escalation check, corrected a
        # second time (§11 Stage D step 16's own fix round): a genuine
        # noise-only null, within EACH cohort's own binomial-thinned split
        # of its POOLED template distribution in the control arm (never
        # the old cross-cohort control reading, contaminated by the
        # trigger channel's own philosophy-correlated signal, not noise --
        # AND never the withdrawn team-id split, which concentrated real
        # cross-team situation-mix heterogeneity into one half at only 5
        # teams/cohort and read 2-3x over the escalation threshold, see
        # `_within_cohort_binomial_thin_tv`'s own docstring). No new corpus
        # run: `null_by_philosophy_template` is the SAME cohort-pooled
        # per-(philosophy, template_id) counts already used to compute
        # `tv_control` above -- team id is never read for this check.
        # Seeded distinctly per cohort (`rng_for`'s own salted-derivation
        # convention, Decision P3-20) so the reported null is reproducible
        # run to run and the two cohorts' splits are independent draws.
        star_rng = rng_for(rng_seed, "validate_gm", "m27_within_cohort_split",
                            GmPhilosophy.STAR_MAXER.value)
        egal_rng = rng_for(rng_seed, "validate_gm", "m27_within_cohort_split",
                            GmPhilosophy.EGALITARIAN.value)
        star_split_tv, star_first_total, star_second_total = _within_cohort_binomial_thin_tv(
            null_by_philosophy_template.get(GmPhilosophy.STAR_MAXER.value, {}), star_rng
        )
        egal_split_tv, egal_first_total, egal_second_total = _within_cohort_binomial_thin_tv(
            null_by_philosophy_template.get(GmPhilosophy.EGALITARIAN.value, {}), egal_rng
        )
        within_cohort_null = (
            max(star_split_tv, egal_split_tv)
            if star_split_tv is not None and egal_split_tv is not None
            else None
        )
        escalation_threshold = bands.metrics["M27"].params.get("escalation_threshold", 0.0625)
        star_k = len(null_by_philosophy_template.get(GmPhilosophy.STAR_MAXER.value, {}))
        egal_k = len(null_by_philosophy_template.get(GmPhilosophy.EGALITARIAN.value, {}))

        # §11 Stage D step 16's minimum-n guard (`_m27_escalation_gate`,
        # `_m27_escalation_min_n`) -- SKIPs the escalation raise/pass
        # decision entirely (never silently passes, never raises) when
        # either cohort's own pooled control-arm selection count is below
        # the data floor its real, measured `K` requires for this check to
        # mean anything at this scale. See `_m27_escalation_min_n`'s own
        # docstring for the full derivation from §9.1's analytic null.
        below_threshold, escalation_skip_reason = _m27_escalation_gate(
            within_cohort_null=within_cohort_null, escalation_threshold=escalation_threshold,
            star_k=star_k, star_n=control_star_total, egal_k=egal_k, egal_n=control_egal_total,
        )

        # §9.1's own closed-form analytic null (`sqrt((K-1)/(pi*n))`),
        # evaluated at each cohort's own real `K` (distinct templates
        # selected in this control arm) and `n` (total selections) --
        # reported alongside the simulated (binomial-thinned) null as a
        # cross-check, never gated: the two should now roughly agree,
        # since the binomial-thinning split is a parametric bootstrap of
        # exactly the same "two independent samples from one shared
        # distribution" question the closed form derives analytically.
        def _analytic_null(k: int, n: int) -> float | None:
            return math.sqrt((k - 1) / (math.pi * n)) if k >= 1 and n > 0 else None

        star_analytic_null = _analytic_null(star_k, control_star_total)
        egal_analytic_null = _analytic_null(egal_k, control_egal_total)

        m27_null_result = {
            "tv_live": tv_live,
            "tv_control": tv_control,
            "m27_prime": m27_prime,
            "star_maxer_selections_live": live_star_total,
            "egalitarian_selections_live": live_egal_total,
            "star_maxer_selections_control": control_star_total,
            "egalitarian_selections_control": control_egal_total,
            "within_cohort_star_maxer_tv": star_split_tv,
            "within_cohort_star_maxer_first_half_total": star_first_total,
            "within_cohort_star_maxer_second_half_total": star_second_total,
            "within_cohort_egalitarian_tv": egal_split_tv,
            "within_cohort_egalitarian_first_half_total": egal_first_total,
            "within_cohort_egalitarian_second_half_total": egal_second_total,
            "within_cohort_null": within_cohort_null,
            "within_cohort_star_maxer_analytic_null": star_analytic_null,
            "within_cohort_egalitarian_analytic_null": egal_analytic_null,
            "escalation_threshold": escalation_threshold,
            "below_threshold": below_threshold,
            "escalation_skip_reason": escalation_skip_reason,
            "elapsed_seconds": elapsed_null,
        }
        if escalation_skip_reason is not None:
            print(f"M27 within-cohort split-half A/A escalation check: SKIPPED -- "
                  f"{escalation_skip_reason}")
        elif within_cohort_null is None:
            print(
                "M27 within-cohort split-half A/A pass: no reading -- one or both cohorts "
                "had zero selections in this arm, or every binomial-thinned coin flip landed "
                f"the same way (star_maxer split={star_split_tv!r}, egalitarian split="
                f"{egal_split_tv!r})."
            )
        elif within_cohort_null >= escalation_threshold:
            print(
                f"\n*** ESCALATION -- M27's within-cohort split-half A/A null "
                f"({within_cohort_null:.6f}) is >= its own escalation_threshold "
                f"({escalation_threshold}) ***\n"
                "The genuine noise-only null itself is not clean at this event rate -- the "
                "phase escalates rather than sliding the band. Do NOT adjust seeds/gm_"
                "validation_bands.json's M27 floor or seeds/gm_phrases.json to force a pass; "
                "this requires a design-level decision.\n"
            )
        else:
            print(
                f"M27 within-cohort split-half A/A pass: null = {within_cohort_null:.6f} "
                f"(star_maxer={star_split_tv:.6f}, egalitarian={egal_split_tv:.6f}), below its "
                f"escalation_threshold ({escalation_threshold}) -- PASS."
            )
        print(
            "M27 within-cohort analytic null (sqrt((K-1)/(pi*n)), §9.1's own closed form, "
            "reported for comparison, never gated): "
            f"star_maxer={star_analytic_null!r} (K={star_k}, n={control_star_total}), "
            f"egalitarian={egal_analytic_null!r} (K={egal_k}, n={control_egal_total})."
        )
        if tv_live is not None and tv_control is not None:
            print(
                f"M27 paired difference: tv_live={tv_live:.6f}, tv_control={tv_control:.6f}, "
                f"M27' = tv_live - tv_control = {m27_prime:.6f}"
            )

        # Decision P3-29(a), wired in for the first time at step 9 (Phase 3
        # §11 Stage B) -- ONLY when this invocation's own config matches
        # the committed Phase-2 baseline's own recorded shape (3 seeds x 5
        # seasons, real games, 15 players/team). A mismatched shape (e.g.
        # the cheap 1x2 skip-games digest-only pass above) is left exactly
        # as before -- digest-only -- per M29's own `known_gap` in
        # `seeds/gm_validation_bands.json`: comparing a mismatched shape
        # produces ~70 permanent, config-drift mismatches unrelated to
        # correctness. Reuses THIS invocation's own just-generated report
        # -- no second run.
        exit_gate_baseline = load_phase2_exit_gate_baseline(DEFAULT_PHASE2_EXIT_GATE_REPORT_PATH)
        exit_gate_config_matches = (
            seeds == exit_gate_baseline.get("seeds")
            and seasons == exit_gate_baseline.get("seasons")
            and skip_games == exit_gate_baseline.get("skip_games")
            and players_per_team == exit_gate_baseline.get("players_per_team")
        )

    # B1 fix: `determinism_runs` is passed as its own parameter, never
    # concatenated into `runs`. `compute_all` hands `determinism_runs`
    # to M6 ALONE (never `runs + determinism_runs` -- that concatenation
    # was B-NEW-1's own bug: it let the real seed-0 run share a group
    # with the determinism pair, so M6 diffed the wrong two runs and
    # never read the second determinism run at all) -- no metric besides
    # M6 (Family-2/3/4 pooled stats, M14's SE gate, M22's skip-games
    # check) ever sees a determinism duplicate. See
    # `metrics.py::compute_all`'s own docstring for the full contamination
    # this replaces.
    #
    # `control_mismatches` is folded in a small loop rather than computed
    # once, because Decision P3-29(a)'s own comparison (a) reads THIS
    # run's own `go`/`go_reason` (both top-level compared fields against
    # the baseline), and `go`/`go_reason` in turn depend on M29's own
    # pass/fail, which depends on the folded count -- a one-bit feedback
    # loop (M29 is entirely absent from the Phase-2 baseline's metric set,
    # so no OTHER field this comparison reads moves between iterations;
    # only `go`/`go_reason` can, and only once each way). Converges in at
    # most 3 iterations by construction; a 4th that still disagrees is a
    # real bug, not a slow fixed point.
    control_mismatches = digest_mismatches
    exit_gate_comparison = None
    _MAX_FOLD_ITERATIONS = 5
    for _fold_iteration in range(_MAX_FOLD_ITERATIONS):
        results = compute_all(
            tuple(runs), bands, determinism_runs=determinism_runs, include_metric_17=True,
            control_mismatches=control_mismatches, control_totals=control_totals,
            m27_control_by_philosophy_template=m27_control_by_philosophy_template,
            media_corpus=media_corpus,
        )
        if not exit_gate_config_matches:
            break
        go_probe, go_reason_probe = _decide_go(
            results, seeds=seeds, seasons=seasons, skip_games=skip_games, bands=bands,
        )
        probe_report = {
            "go": go_probe, "go_reason": go_reason_probe,
            "seeds": seeds, "seasons": seasons, "skip_games": skip_games,
            "metrics": [_metric_row_json(r) for r in results],
        }
        exit_gate_comparison = compare_exit_gate_reports(probe_report, exit_gate_baseline)
        folded = digest_mismatches + len(exit_gate_comparison.mismatches)
        if folded == control_mismatches:
            break
        control_mismatches = folded
    else:
        raise RuntimeError(
            "M29's control-mismatch fold (Decision P3-29(a) wiring, Phase 3 step 9) did not "
            f"converge in {_MAX_FOLD_ITERATIONS} iterations -- this indicates a real "
            "oscillation, not a slow fixed point (the loop this code implements is provably "
            "a bounded feedback: only `go`/`go_reason` can move between iterations, and each "
            "can move at most once)."
        )

    if exit_gate_config_matches and exit_gate_comparison is not None:
        print(
            f"Decision P3-29(a) comparison vs the committed Phase-2 exit-gate baseline: "
            f"{len(exit_gate_comparison.mismatches)} mismatch(es), folded alongside "
            f"{digest_mismatches} digest mismatch(es) into control_mismatches="
            f"{control_mismatches} for M29."
        )
        for _mismatch in exit_gate_comparison.mismatches:
            print(f"  - {_mismatch}")

    elapsed = time.perf_counter() - t0
    go, go_reason = _decide_go(
        results, seeds=seeds, seasons=seasons, skip_games=skip_games, bands=bands,
    )

    family3_source = _family3_source_rows(
        results, bands_with_family3, family3_precert=family3_precert
    )
    write_report(
        out_dir, results=results, go=go, go_reason=go_reason, seeds=seeds, seasons=seasons,
        players_per_team=players_per_team, rng_seed=rng_seed, skip_games=skip_games,
        determinism_check=determinism_check, elapsed_seconds=elapsed,
        phase_timings_by_seed=phase_timings_by_seed, family3_source=family3_source,
        m27_null_result=m27_null_result,
    )
    _assert_m28_step17_invariants(results)
    _assert_m27_null_control(m27_null_result)
    _assert_phase3_exit_gate_counted_total(
        results, skip_games=skip_games, control_check=control_check,
        total_bands=len(bands.metrics), media_corpus_present=media_corpus is not None,
        below_gate_scale=_is_below_gate_scale(seeds=seeds, seasons=seasons, bands=bands),
        determinism_check=determinism_check,
    )
    _assert_no_family5_skip_at_exit_gate(results, control_check=control_check)
    return results, go


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run N seeded AI-vs-AI GM seasons (in-memory, no Postgres) through the 22-metric "
            "validation harness against seeds/gm_validation_bands.json (Sec.10's exit "
            "criterion). See this module's own docstring for the GO/NO-GO rule."
        )
    )
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--seasons", type=int, default=5)
    parser.add_argument("--players-per-team", type=int, default=15)
    parser.add_argument("--rng-seed", type=int, default=1)
    parser.add_argument("--season-year", type=int, default=2026)
    parser.add_argument("--skip-games", action="store_true")
    parser.add_argument("--determinism-check", dest="determinism_check", action="store_true")
    parser.add_argument("--no-determinism-check", dest="determinism_check", action="store_false")
    parser.set_defaults(determinism_check=True)
    parser.add_argument("--control-check", dest="control_check", action="store_true")
    parser.add_argument("--no-control-check", dest="control_check", action="store_false")
    parser.set_defaults(control_check=True)
    parser.add_argument(
        "--media-corpus", type=Path, default=None,
        help="Path to a MediaCorpusTotals JSON artifact (Decision P4-95/P4-102) for M33-M36. "
             "Default: artifacts/media_corpus.json if it exists, else the media family SKIPs "
             "(Decision P4-34). Fails closed (raises) on a library_sha mismatch against the "
             "live seeds/media_phrases.json (Decision P4-101).",
    )
    parser.add_argument("--no-media-corpus", dest="use_media_corpus", action="store_false")
    parser.set_defaults(use_media_corpus=True)
    parser.add_argument(
        "--family3-precert", dest="family3_precert", action="store_true",
        help="Gate Family 3 (M11-M16) on the Decision P3-47 45-seed precertified citation "
             "when present and version-matched (default).",
    )
    parser.add_argument(
        "--no-family3-precert", dest="family3_precert", action="store_false",
        help="Dev-loop escape hatch: force every Family-3 metric onto its own live reading "
             "regardless of citation validity -- catches a mechanism change that moves "
             "Family-3 behaviour without a GM_TUNING_VERSION bump (Decision P3-47 step 5).",
    )
    parser.set_defaults(family3_precert=True)
    parser.add_argument("--draft-divergence-drafts", type=int, default=100)
    parser.add_argument("--only-metric-17", action="store_true")
    parser.add_argument("--bands", type=Path, default=DEFAULT_BANDS_PATH)
    parser.add_argument("--digest", type=Path, default=_DEFAULT_DIGEST_PATH)
    parser.add_argument("--protected-pick-share", type=float, default=0.0)
    parser.add_argument(
        "--out", default=None,
        help="Output directory (default: artifacts/validation_gm/<UTC timestamp>).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.only_metric_17:
        bands = _bands_with_m17_draft_count(load_bands(args.bands), args.draft_divergence_drafts)
        mv = compute_m17(bands)
        print(f"M17 (draft_board_divergence): value={mv.value}, detail={mv.detail}")
        print("(--only-metric-17 is a standalone fast path -- no GO/NO-GO verdict rendered.)")
        return

    out_dir = (
        Path(args.out)
        if args.out is not None
        else _DEFAULT_OUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    results, go = run_validation(
        seeds=args.seeds, seasons=args.seasons, players_per_team=args.players_per_team,
        rng_seed=args.rng_seed, season_year=args.season_year, skip_games=args.skip_games,
        determinism_check=args.determinism_check, control_check=args.control_check,
        family3_precert=args.family3_precert,
        protected_pick_share=args.protected_pick_share,
        draft_divergence_drafts=args.draft_divergence_drafts, bands_path=args.bands,
        out_dir=out_dir, digest_path=args.digest,
        media_corpus_path=args.media_corpus, use_media_corpus=args.use_media_corpus,
    )
    counted = [r for r in results if r.counted_in_go]
    passed = sum(1 for r in counted if r.passed)
    print(f"\n{'GO' if go else 'NO-GO'} -- {passed}/{len(counted)} counted metrics pass.")
    print(f"Report: {out_dir / 'report.md'}")
    failing = [r for r in counted if r.passed is False]
    if failing:
        print("FAILED metrics:")
        for r in failing:
            print(f"  {r.id} ({r.name}): value={r.value} status={r.status}")

    # Nit 6 (round-7/T7b critic): round 7 Sec.8 says "print GO/NO-GO + exit
    # code" -- main() previously always returned None (exit 0) regardless
    # of the verdict, so a caller (CI, a shell script) had no way to detect
    # NO-GO except by parsing stdout. 0 for GO, 1 for NO-GO -- no CLI
    # precedent to match (validate_sim.py's own main() never exits
    # nonzero either), standard Unix "0 == success" convention otherwise.
    raise SystemExit(0 if go else 1)


if __name__ == "__main__":
    main()
