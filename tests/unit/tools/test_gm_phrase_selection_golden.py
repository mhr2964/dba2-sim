"""Decision P4-47 (as amended by P4-64)'s golden replay -- docs/design/
phase-4-media-and-player-agency.md §11 Stage D step 15, §10 test 5.

Both parts replayed through the SAME public entry point,
`select_phrase` -- its signature and behavior do not change across the
`dba2/managers/voice/engine.py` extraction (Decision P4-20), so this one
test file needs no edit at all between the golden-capture commit and the
extraction commit: it is green against the pre-extraction body today and
must still be green, unmodified, against the post-extraction thin wrapper.

Element-wise comparison, never a hash (Decision P4-47's own stated
reason: a hash tells you THAT a selection moved, not WHICH one)."""
from __future__ import annotations

import json
from pathlib import Path

from dba2.managers.voice.select import select_phrase
from dba2.services.phrase_service import DEFAULT_PHRASE_LIBRARY_PATH, load_library
from tools.gm_phrase_golden_corpus import (
    SYNTHETIC_TEMPLATES,
    GoldenCase,
    case_rng,
    shipped_library_cases,
    synthetic_operator_coverage_cases,
)

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[3] / "seeds" / "gm_phrase_selection_golden.json"
)


def _load_golden() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _replay(cases: list[GoldenCase], library, *, repeat_penalty: float) -> list[dict]:
    results = []
    for case in cases:
        selection = select_phrase(
            case.frame, library, use_counts=case.use_counts,
            repeat_penalty=repeat_penalty, rng=case_rng(case.label),
        )
        results.append({
            "label": case.label,
            "template_id": selection.template_id,
            "slot_bindings": dict(selection.slot_bindings),
        })
    return results


def test_golden_file_exists_and_covers_both_parts():
    golden = _load_golden()
    assert "shipped_library_replay" in golden
    assert "synthetic_operator_coverage_replay" in golden


def test_shipped_library_replay_matches_committed_golden_element_wise():
    golden = _load_golden()
    shipped_library = load_library(DEFAULT_PHRASE_LIBRARY_PATH)
    expected = golden["shipped_library_replay"]
    assert shipped_library.repeat_penalty == expected["repeat_penalty"]
    assert len(shipped_library.templates) == expected["template_count"] == 88

    fresh = _replay(
        shipped_library_cases(), shipped_library.templates,
        repeat_penalty=shipped_library.repeat_penalty,
    )
    assert len(fresh) == len(expected["cases"])
    for actual, recorded in zip(fresh, expected["cases"], strict=True):
        assert actual == recorded, (
            f"selection for case {actual['label']!r} moved: "
            f"recorded={recorded}, fresh={actual}"
        )


def test_shipped_library_replay_covers_every_dimension_combination():
    """Decision P4-64's secondary, retained requirement -- corpus breadth
    on the survivor sets entering steps 3-6, not a claim about the
    extracted code itself."""
    from dba2.domain.team import GmPhilosophy
    from dba2.managers.tuning import CompetitiveWindow
    from dba2.managers.voice.frame import SituationToken

    cases = shipped_library_cases()
    labels = {c.label for c in cases}
    expected_combo_count = 0
    for situation in SituationToken:
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                expected_combo_count += 1
                assert f"cross_{philosophy.value}_{situation.value}_{window.value}" in labels
    assert expected_combo_count == 280
    assert "repeat_penalty_engaged" in labels
    assert "fallback_only_survivor" in labels


def test_synthetic_operator_coverage_replay_matches_committed_golden_element_wise():
    golden = _load_golden()
    expected = golden["synthetic_operator_coverage_replay"]
    assert len(SYNTHETIC_TEMPLATES) == expected["template_count"] == 7

    fresh = _replay(
        synthetic_operator_coverage_cases(), SYNTHETIC_TEMPLATES,
        repeat_penalty=expected["repeat_penalty"],
    )
    assert len(fresh) == len(expected["cases"])
    for actual, recorded in zip(fresh, expected["cases"], strict=True):
        assert actual == recorded, (
            f"selection for case {actual['label']!r} moved: "
            f"recorded={recorded}, fresh={actual}"
        )


def test_synthetic_operator_coverage_replay_actually_exercises_gt_and_in():
    """Decision P4-64's whole reason for existing: `seeds/gm_phrases.json`
    has zero `gt`/`in` triggers (measured: `gte` 48, `eq` 16, `lt` 13,
    `lte` 3, `gt` 0, `in` 0), so a golden replayed only against it cannot
    prove either operator branch works. This asserts the synthetic
    fixture's own `gt`/`in` cases actually select their target templates
    -- not merely that a template naming `gt`/`in` exists in the library,
    which would pass even if `_trigger_holds`'s `gt`/`in` branches were
    both deleted and every survivor fell through to the fallback."""
    fresh = {
        c.label: select_phrase(
            c.frame, SYNTHETIC_TEMPLATES, use_counts=c.use_counts,
            repeat_penalty=0.25, rng=case_rng(c.label),
        ).template_id
        for c in synthetic_operator_coverage_cases()
    }
    assert fresh["synthetic_gt_holds"] == "synthetic_gt"
    assert fresh["synthetic_in_holds"] == "synthetic_in"
    assert fresh["synthetic_none_hold"] == "synthetic_fallback"
