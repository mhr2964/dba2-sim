"""Player identity/bio/status — `player_repo`'s table, and only that table
(§5: "no contracts, no stats, no records").

phase-0-foundation.md §4.3.1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Position(StrEnum):
    """§4.3.1 says `primary_position`/`secondary_position` run "PG..C" —
    read as the five standard positions the phrase implies."""

    PG = "PG"
    SG = "SG"
    SF = "SF"
    PF = "PF"
    C = "C"


class RosterStatus(StrEnum):
    ACTIVE = "active"
    TWO_WAY = "two_way"
    INJURED = "injured"
    FREE_AGENT = "free_agent"
    RETIRED = "retired"
    DRAFT_ELIGIBLE = "draft_eligible"


@dataclass
class Player:
    """Decision 10/11: identity only — ability lives in `player_ratings`,
    propensity in `player_tendencies`. Decision 12: `overall_cached` is a
    named cache with exactly one writer (`ratings_service.recompute_overall`
    — Phase 1), never independently edited."""

    id: int
    league_id: int
    first_name: str
    last_name: str
    external_ref: str | None
    primary_position: Position
    secondary_position: Position | None
    height_in: int
    weight_lb: int
    wingspan_in: int
    birth_date: date
    draft_year: int | None
    draft_pick_number: int | None
    years_pro: int
    team_id: int | None
    roster_status: RosterStatus
    overall_cached: int
    overall_computed_at: datetime
    retired_in_season: int | None
    created_at: datetime
    updated_at: datetime
