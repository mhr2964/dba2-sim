"""`/phase`'s panel builder -- §7.1's `/phase` row ("league_repo phase
state + phase/graph.legal_next_phases -- read-only").
"""
from __future__ import annotations

from collections.abc import Sequence

from dba2.domain.phase import Phase
from dba2.presentation.panel import Panel, PanelColor


def build_phase_panel(
    *,
    league_name: str,
    season: int,
    current_phase: Phase,
    legal_next: Sequence[Phase],
) -> Panel:
    next_text = ", ".join(p.value for p in legal_next) if legal_next else "(none)"
    description = f"**Current phase:** {current_phase.value}\n**Can advance to:** {next_text}"
    return Panel(
        title=f"{league_name} — Season {season}",
        description=description,
        color=PanelColor.NEUTRAL,
    )
