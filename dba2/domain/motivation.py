"""player_motivation, player_motivation_events — `motivation_repo`'s tables
(§5). Pillar 5's agency vector.

phase-0-foundation.md §4.6.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from dba2.domain.phase import Phase


class MarketPreference(StrEnum):
    BIG_MARKET = "big_market"
    SMALL_MARKET = "small_market"
    NEUTRAL = "neutral"
    WARM_WEATHER = "warm_weather"


class RequestState(StrEnum):
    CONTENT = "content"
    UNSETTLED = "unsettled"
    TRADE_REQUESTED = "trade_requested"
    EXTENSION_DEMANDED = "extension_demanded"
    HOLDOUT = "holdout"


class MotivationAxis(StrEnum):
    """The five axes named in §4.6 (`weight_*`/`satisfaction_*` column
    suffixes) — used by `player_motivation_events.axis` to say *which* axis
    an event moved."""

    WINNING = "winning"
    ROLE_USAGE = "role_usage"
    LOYALTY = "loyalty"
    MONEY = "money"
    LEGACY = "legacy"


@dataclass
class PlayerMotivation:
    """One row per player. Trait columns (near-immutable) then state columns
    (rewritten at trade deadline / offseason checkpoints) — §4.6. Weights
    are normalized at read time, not on write, so a commissioner edit to one
    axis doesn't silently rescale the others."""

    player_id: int
    league_id: int

    weight_winning: int
    weight_role_usage: int
    weight_loyalty: int
    weight_money: int
    weight_legacy: int
    hometown_team_id: int | None
    market_preference: MarketPreference
    star_leverage: int

    satisfaction_winning: int
    satisfaction_role_usage: int
    satisfaction_loyalty: int
    satisfaction_money: int
    satisfaction_legacy: int
    morale: int
    trust_in_management: int
    request_state: RequestState
    request_opened_in_season: int | None
    last_evaluated_season: int
    # The phase of the last checkpoint that evaluated this player (migration 020,
    # phase-5-stage-e-advance.md P5-99.r.2) -- with `last_evaluated_season`, the
    # guard that makes a resumed checkpoint skip a player it already folded.
    # Defaulted so no construction site outside the mapper needs editing.
    last_checkpoint_phase: Phase | None = None


@dataclass
class PlayerMotivationEvent:
    """Append-only audit trail — every satisfaction change points at the
    event that caused it (§4.6, "no black box").

    `from_state`/`to_state` (migration 021, docs/design/media-v2-newsroom.md
    MV-2b) — typed companions to the free-text `note` a ladder-transition
    event already writes as `f"{old.value}->{new.value}"`
    (`agency_service.py`'s own ladder-transition insert). Both nullable,
    both defaulted `None` so no existing construction site needs editing:
    a non-transition event (a plain satisfaction-swing write) never carries
    either, and a row written before this migration reads `NULL`/`NULL` on
    both — "no reading," per this project's standing "absent is neutral,
    never a fabricated value" rule, never a guessed `CONTENT`."""

    id: int
    league_id: int
    player_id: int
    season: int
    phase: Phase
    event_type: str
    axis: MotivationAxis
    delta: int
    note: str | None
    created_at: datetime
    from_state: RequestState | None = None
    to_state: RequestState | None = None
