"""injuries — `injury_repo`'s table (§5).

phase-0-foundation.md §4.7. `injury_type`/`severity` stay plain `str` — the
doc doesn't enumerate their value sets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Injury:
    id: int
    league_id: int
    player_id: int
    game_id: int | None
    injury_type: str
    severity: str
    games_remaining: int | None
    occurred_in_season: int
    resolved_at: datetime | None
