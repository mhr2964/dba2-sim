"""player_awards — `award_repo`'s table (§5). Decision 20: a Phase 0 table
because it's the supermax-eligibility input, not a Phase 4 concern, even
though Pillar 4's media layer later reads the same table for free.

phase-0-foundation.md §4.4.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class AwardType(StrEnum):
    MVP = "mvp"
    DPOY = "dpoy"
    ROY = "roy"
    MIP = "mip"
    SMOY = "smoy"
    ALL_NBA_1 = "all_nba_1"
    ALL_NBA_2 = "all_nba_2"
    ALL_NBA_3 = "all_nba_3"
    ALL_DEFENSE_1 = "all_defense_1"
    ALL_DEFENSE_2 = "all_defense_2"
    FINALS_MVP = "finals_mvp"
    ALL_STAR = "all_star"


@dataclass
class PlayerAward:
    id: int
    league_id: int
    player_id: int
    season: int
    award_type: AwardType
    place: int | None
    vote_share: Decimal | None
