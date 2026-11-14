"""`presentation/phase.py` -- `/phase`'s panel builder (§7.1)."""
from __future__ import annotations

from dba2.domain.phase import Phase
from dba2.presentation.phase import build_phase_panel


def test_phase_panel_shows_current_phase_and_legal_next():
    panel = build_phase_panel(
        league_name="Test League",
        season=2026,
        current_phase=Phase.REGULAR_SEASON,
        legal_next=[Phase.REGULAR_SEASON_COMPLETE, Phase.TRADE_DEADLINE],
    )
    assert "REGULAR_SEASON" in panel.description
    assert "TRADE_DEADLINE" in panel.description
    assert "REGULAR_SEASON_COMPLETE" in panel.description


def test_phase_panel_handles_no_legal_next_phases():
    panel = build_phase_panel(
        league_name="Test League", season=2026, current_phase=Phase.WAIVERS, legal_next=[]
    )
    assert "(none)" in panel.description
