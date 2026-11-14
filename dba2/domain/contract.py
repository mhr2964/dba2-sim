"""contracts, contract_years, dead_cap_charges, cap_holds, rookie_scale —
`contract_repo`'s tables (§5: "all cap-sheet queries live here").

phase-0-foundation.md §4.4 — the section with no v1 ancestor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ContractType(StrEnum):
    ROOKIE_SCALE = "rookie_scale"
    STANDARD = "standard"
    MAX = "max"
    SUPERMAX = "supermax"
    VETERAN_MIN = "veteran_min"
    TWO_WAY = "two_way"
    EXTENSION = "extension"


class SigningMethod(StrEnum):
    CAP_SPACE = "cap_space"
    BIRD = "bird"
    EARLY_BIRD = "early_bird"
    NON_BIRD = "non_bird"
    MLE = "mle"
    BI_ANNUAL = "bi_annual"
    MIN_EXCEPTION = "min_exception"
    ROOKIE_SCALE = "rookie_scale"
    DRAFT = "draft"


class ContractStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    WAIVED = "waived"
    BOUGHT_OUT = "bought_out"
    VOIDED = "voided"


class OptionType(StrEnum):
    NONE = "none"
    TEAM = "team"
    PLAYER = "player"
    EARLY_TERMINATION = "early_termination"


class OptionDecision(StrEnum):
    PENDING = "pending"
    EXERCISED = "exercised"
    DECLINED = "declined"
    N_A = "n_a"


class DeadCapReason(StrEnum):
    WAIVE_GUARANTEED = "waive_guaranteed"
    WAIVE_STRETCHED = "waive_stretched"
    BUYOUT = "buyout"
    RETIREMENT = "retirement"
    TRADE_ABSORBED = "trade_absorbed"


@dataclass
class Contract:
    """The header. `team_id` is nullable only for voided rows (§4.4)."""

    id: int
    league_id: int
    player_id: int
    team_id: int | None
    signed_in_season: int
    start_season: int
    end_season: int
    total_years: int
    contract_type: ContractType
    signing_method: SigningMethod
    no_trade_clause: bool
    trade_kicker_pct: int
    status: ContractStatus
    terminated_in_season: int | None
    created_at: datetime
    terminated_at: datetime | None


@dataclass
class ContractYear:
    """One row per contract-season — Decision 18: raises/options/partial
    guarantees are per-year facts a single `contracts.salary` scalar
    cannot express."""

    contract_id: int
    season: int
    salary: int
    guaranteed_amount: int
    option_type: OptionType
    option_decision: OptionDecision


@dataclass
class DeadCapCharge:
    """The ledger. `team_id` is the team that eats the charge (survives the
    player leaving); `player_id`/`source_contract_id` are display-only
    pointers to where it came from."""

    id: int
    league_id: int
    team_id: int
    player_id: int
    source_contract_id: int
    season: int
    amount: int
    reason: DeadCapReason
    created_in_season: int
    stretch_years: int | None


@dataclass
class CapHold:
    """Decision 19: ships schema-only in Phase 0 so
    `cap_service.get_cap_summary(..., include_holds: bool)`'s signature
    doesn't need a rewrite when Phase 2 free agency starts populating it.
    `hold_type` stays a plain `str` — §4.4 doesn't enumerate its values."""

    id: int
    league_id: int
    team_id: int
    player_id: int
    season: int
    amount: int
    hold_type: str


@dataclass
class RookieScale:
    """Ported verbatim from v1 — league-independent lookup data, seeded by
    migration 003 (§7.2)."""

    pick_number: int
    year_1_salary: int
    year_2_salary: int
    year_3_salary: int
    year_4_salary: int
