"""Tier 1: `GmTuning`/`CompetitiveWindow`/`WINDOW_PARAMS` -- §12 step 1's
own verify list (docs/design/phase-2-manager-minds.md)."""
from __future__ import annotations

from dba2.managers.tuning import (
    DEFAULT_TUNING,
    GM_TUNING_VERSION,
    WINDOW_PARAMS,
    CompetitiveWindow,
    GmTuning,
)


def test_gm_tuning_version_is_3_after_phase_3_step_4():
    """Bumped 1 -> 2 by Slice 22-B (architect round 23 §4.3, Decision
    P2-42) -- `decisions/signing.py`'s `_YEARS_BASE_LO`/`_YEARS_BASE_HI`
    moved. Bumped 2 -> 3 by Phase 3 §11 step 4 (Decision P3-30) --
    `managers/negotiation.py`/`managers/memory.py` declare Phase 3's
    entire six-constant `managers/`-scoped set in one commit, the phase's
    one and only bump. Must move in lockstep with `seeds/gm_validation_
    bands.json`'s own top-level `gm_tuning_version` -- see `tests/unit/
    tools/gm_harness/test_metrics.py::test_load_bands_covers_all_26_ids`
    for the cross-file assertion, and `tools/gm_harness/bands.py::
    load_bands` for the runtime cross-check that raises `ValueError` if
    the two ever drift apart."""
    assert GM_TUNING_VERSION == 3


def test_default_tuning_constructs_with_no_arguments():
    assert isinstance(DEFAULT_TUNING, GmTuning)
    assert GmTuning() == DEFAULT_TUNING


def test_window_params_covers_exactly_the_four_competitive_windows():
    assert set(WINDOW_PARAMS) == set(CompetitiveWindow)
    assert len(WINDOW_PARAMS) == 4


def test_window_params_match_section_7s_table():
    """§7's table, spot-checked at both ends -- REBUILD compounds the
    future the least steeply (highest gamma) and rewards youth the most
    (highest age_exponent and window_upside); CONTEND does the reverse
    and wants zero cap room held back."""
    rebuild = WINDOW_PARAMS[CompetitiveWindow.REBUILD]
    contend = WINDOW_PARAMS[CompetitiveWindow.CONTEND]
    assert rebuild.gamma == 0.93
    assert rebuild.window_upside == 1.40
    assert rebuild.age_exponent == 1.50
    assert rebuild.desired_room == 0.20
    assert rebuild.window_urgency == 0.00
    assert contend.gamma == 0.74
    assert contend.window_upside == 0.60
    assert contend.age_exponent == 0.70
    assert contend.desired_room == 0.00
    assert contend.window_urgency == 0.18


def test_unresolved_discount_for_clamps_at_the_4_plus_entry():
    tuning = GmTuning()
    assert tuning.unresolved_discount_for(0) == 1.00
    assert tuning.unresolved_discount_for(4) == 0.50
    assert tuning.unresolved_discount_for(9) == 0.50, (
        "years_out beyond the table's last index must clamp, not IndexError"
    )
