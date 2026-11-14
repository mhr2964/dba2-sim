"""Shared golden-file helpers for `tests/unit/presentation/`'s panel
builders -- Decision P5-22 item 1's own instrument ("a committed golden
per panel type turns 'did this render change' into a diff"), replayed
element-wise per Decision P4-47's already-established reason (a hash
tells you THAT a selection moved, not WHICH field) -- the same
`tools/generate_gm_phrase_selection_golden.py`/`test_gm_phrase_selection_
golden.py` pattern this codebase already uses once, applied to `Panel`
instead of a phrase selection."""
from __future__ import annotations

import json
from pathlib import Path

from dba2.presentation.panel import Panel

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden"


def panel_to_dict(panel: Panel) -> dict:
    return {
        "title": panel.title,
        "description": panel.description,
        "fields": [{"name": f.name, "value": f.value, "inline": f.inline} for f in panel.fields],
        "color": int(panel.color),
        "footer": panel.footer,
        "author": panel.author,
    }


def load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))
