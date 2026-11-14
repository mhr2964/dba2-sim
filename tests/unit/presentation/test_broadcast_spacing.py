"""One blank line between plays (docs/design/watch-v3-spacing-and-pager.md W-9,
still true of the live ticker), and the live component's worst-case size over
the corpus (docs/design/watch-v4-live-ticker.md T-1.a, T-8 #1).

v3's packer/renderer coupling this file used to hold (`plan_chunks` packing
UTF-16 units a frozen period message would later render) is GONE with the
packer itself: v4 has no message that is packed once and never re-measured,
so there is no packed-vs-rendered equality left to prove. What remains is the
separator constant (still the one join every ticker and pager page uses) and
the corpus-measured worst-case panel size, now for `render_live_component`
instead of `render_period_chunk`."""

from __future__ import annotations

import pytest

from dba2.presentation.broadcast import (
    _LINE_SEPARATOR,
    _utf16_units,
    last_line_index,
    plan_broadcast,
    plan_narration,
    render_live_component,
    visible_periods,
)
from dba2.presentation.panel import MAX_TOTAL_CHARS
from tests.unit.presentation._broadcast_helpers import make_team, names_for
from tests.unit.presentation.test_broadcast_plan import _PRESETS
from tests.unit.presentation.test_live_box import _SEEDS, _game

_HOME, _AWAY = make_team(1, "HOM"), make_team(2, "AWY")


def test_the_separator_is_one_blank_line_and_two_units():
    assert _LINE_SEPARATOR == "\n\n"
    assert _utf16_units(_LINE_SEPARATOR) == 2


@pytest.mark.slow
def test_the_corpus_component_stays_well_inside_discords_message_total():
    """Measured over the 34 seeds x 3 presets, at Full Game (the widest
    selection: every field FINAL adds, plus the longest ticker LIVE ever
    shows). T-1.a's hand computation puts the worst ticker description at
    ~2,178 characters (FAST_FORWARD) and the whole panel at ~2,100; this test
    is the corpus-measured version of that arithmetic, pinned so a drift is
    visible rather than quietly eating the headroom."""
    worst_description = worst_panel = 0
    for seed in _SEEDS:
        result, _ = _game(seed)
        names = names_for(result)
        for settings in _PRESETS.values():
            plan = plan_broadcast(
                result, game_id=1, home_code="HOM", away_code="AWY", settings=settings
            )
            narration = plan_narration(result, plan, home=_HOME, away=_AWAY, names=names)
            for frame in plan.frames:
                last = last_line_index(frame, plan)
                for selected in (0, *visible_periods(plan, last)):
                    panel = render_live_component(
                        frame,
                        plan,
                        selected_period=selected,
                        home=_HOME,
                        away=_AWAY,
                        names=names,
                        result=result,
                        narration=narration,
                    )
                    worst_description = max(worst_description, _utf16_units(panel.description))
                    total = (
                        len(panel.title)
                        + len(panel.description)
                        + sum(len(f.name) + len(f.value) for f in panel.fields)
                        + len(panel.footer)
                        + len(panel.author)
                    )
                    worst_panel = max(worst_panel, total)
    assert worst_panel < MAX_TOTAL_CHARS, worst_panel
    # Headroom is real but not huge -- a drift toward the ceiling should be visible.
    assert 500 < worst_description < 3_000, worst_description
    assert 500 < worst_panel < 4_000, worst_panel
