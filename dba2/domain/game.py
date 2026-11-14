"""games — `game_repo`'s table (§5: "games, schedule queries").

phase-0-foundation.md §4.7, §6.4. `GameStatus` lives here (not in
`domain/phase.py`) because, unlike `TradeStatus`/`AwardBallotStatus`, an
actual table's `status` column types against it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"


class GameType(StrEnum):
    PRESEASON = "preseason"
    REGULAR = "regular"
    PLAYIN = "playin"
    PLAYOFF = "playoff"


@dataclass
class Game:
    """`rng_seed` per game (Decision 7) is what makes a single game
    replayable without replaying the season.

    `engine_version`/`roster_digest`/`overtime_periods` are migration 008
    additions (phase-1-simulation-engine.md §7.2, Decision P1-22): which
    tuning profile produced this result, a hash of each team's *available*
    roster at `build_context` time (not just who ends up participating),
    and the overtime period count. `roster_digest` is nullable — an unsimmed
    game has no digest yet."""

    id: int
    league_id: int
    season: int
    game_index: int
    game_date: date
    home_team_id: int
    away_team_id: int
    home_score: int | None
    away_score: int | None
    status: GameStatus
    game_type: GameType
    rng_seed: int
    simmed_at: datetime | None
    engine_version: int
    roster_digest: str | None
    overtime_periods: int
