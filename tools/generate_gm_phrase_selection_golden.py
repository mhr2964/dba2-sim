"""CLI: generates/refreshes `seeds/gm_phrase_selection_golden.json` --
Decision P4-47 (as amended by P4-64), docs/design/phase-4-media-and-
player-agency.md §11 Stage D step 15.

Mirrors `tools/generate_gm_decision_log_digest.py`'s own shape:
`argparse`, `generate_golden(...)` as the importable entry point,
`main()`, `if __name__ == "__main__": main()`, invoked `python -m
tools.generate_gm_phrase_selection_golden` from the repo root.

**Captured in the commit BEFORE the engine extraction, not alongside
it** (Decision P4-47's own explicit requirement) -- a golden regenerated
in the same commit as the change it gates proves nothing. This script
must be run, and its output committed, while `select.py::select_phrase`
still runs its original, un-extracted six-step body.

The corpus itself (both parts) lives in `tools/gm_phrase_golden_corpus.
py`, imported here and by the replay test (`tests/unit/tools/test_gm_
phrase_selection_golden.py`) -- one definition, so generation and
verification can never drift apart.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dba2.managers.voice.select import select_phrase
from dba2.services.phrase_service import DEFAULT_PHRASE_LIBRARY_PATH, load_library
from tools.gm_phrase_golden_corpus import (
    GOLDEN_MASTER_SEED,
    SYNTHETIC_TEMPLATES,
    GoldenCase,
    case_rng,
    shipped_library_cases,
    synthetic_operator_coverage_cases,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT_PATH = _REPO_ROOT / "seeds" / "gm_phrase_selection_golden.json"


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


def generate_golden() -> dict:
    shipped_library = load_library(DEFAULT_PHRASE_LIBRARY_PATH)
    shipped_cases = shipped_library_cases()
    synthetic_cases = synthetic_operator_coverage_cases()

    return {
        "_description": (
            "Decision P4-47(/P4-64)'s committed template-for-template phrase-"
            "selection golden -- captured BEFORE dba2/managers/voice/engine.py's "
            "extraction, over a fixed input corpus (tools/gm_phrase_golden_"
            "corpus.py). Regenerate with `python -m tools."
            "generate_gm_phrase_selection_golden`. Two parts: 'shipped_library_"
            "replay' (the real, unmodified seeds/gm_phrases.json, dimension-"
            "coverage corpus) and 'synthetic_operator_coverage_replay' (a small "
            "fixture library declared in tools/gm_phrase_golden_corpus.py, NOT "
            "added to seeds/gm_phrases.json per Decision P4-33, covering the two "
            "_trigger_holds operators -- gt, in -- the shipped library cannot "
            "reach). Compare element-wise, never by hash (a hash proves THAT "
            "something moved, not WHICH selection did)."
        ),
        "master_seed": GOLDEN_MASTER_SEED,
        "shipped_library_replay": {
            "library_path": "seeds/gm_phrases.json",
            "template_count": len(shipped_library.templates),
            "repeat_penalty": shipped_library.repeat_penalty,
            "case_count": len(shipped_cases),
            "cases": _replay(
                shipped_cases, shipped_library.templates,
                repeat_penalty=shipped_library.repeat_penalty,
            ),
        },
        "synthetic_operator_coverage_replay": {
            "template_count": len(SYNTHETIC_TEMPLATES),
            "operators_covered": ["lt", "lte", "gte", "gt", "eq", "in"],
            "repeat_penalty": 0.25,
            "case_count": len(synthetic_cases),
            "cases": _replay(
                synthetic_cases, SYNTHETIC_TEMPLATES, repeat_penalty=0.25,
            ),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate/refresh seeds/gm_phrase_selection_golden.json"
    )
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT_PATH)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = generate_golden()
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.out} "
        f"({payload['shipped_library_replay']['case_count']} shipped-library cases, "
        f"{payload['synthetic_operator_coverage_replay']['case_count']} "
        "synthetic-operator cases)"
    )


if __name__ == "__main__":
    main()
