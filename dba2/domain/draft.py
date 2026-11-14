"""draft_picks, drafts, draft_selections, draft_classes — `draft_repo`'s
tables (§5).

phase-0-foundation.md §4.5, §6.3. §6.3: `drafts` deliberately has no
`status` column — draft lifecycle folds into the single league `Phase`
graph (`pending_lottery` = `DRAFT_LOTTERY`, in-progress = `DRAFT`, complete
= `POST_DRAFT_TRADES`); what remains here is a *pointer*
(`current_round`/`current_pick_number`), not a state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from dba2.domain.player import Position


class ProtectionType(StrEnum):
    """Decision 21: protection columns exist in Phase 0 schema; conveyance
    logic is Phase 2/3."""

    NONE = "none"
    TOP_N = "top_n"
    LOTTERY = "lottery"
    UNPROTECTED = "unprotected"


@dataclass
class DraftPick:
    id: int
    league_id: int
    season: int
    round: int
    original_team_id: int
    current_team_id: int
    pick_number: int | None
    used_for_player_id: int | None
    protection_type: ProtectionType
    protection_through: int | None
    swap_with_pick_id: int | None
    conveyed: bool
    rolls_over_to_season: int | None


@dataclass
class Draft:
    id: int
    league_id: int
    season: int
    current_round: int
    current_pick_number: int
    lottery_completed_at: datetime | None
    total_picks: int


@dataclass
class DraftSelection:
    id: int
    draft_id: int
    pick_number: int
    round: int
    team_id: int
    player_id: int
    pick_asset_id: int
    selected_at: datetime


@dataclass
class DraftClass:
    id: int
    league_id: int
    class_year: int
    player_id: int
    mock_rank: int | None
    is_generated: bool
    source: str | None


@dataclass(frozen=True)
class AvailableProspect:
    """One row of the board a person choosing a pick sees (Decision P5-98.k,
    docs/design/phase-5-stage-d-prime-draft.md) -- a join of `players` and
    `draft_classes`, not a table.

    **Named for what it is rather than `DraftBoardEntry`**, which already
    exists in `managers/decisions/draft.py` as the AI's own scouted view of
    a prospect. Two types with one name across the human and AI halves of
    the same phase is how a future reader imports the wrong one.

    **It carries no rating, and that is a ruling, not an omission** (§7
    L11). The AI does not see true ratings either -- it scouts through
    `effective_talent_ovr` with per-team noise -- so a human picking off a
    list sorted by `overall_cached` would know something no GM in the league
    knows. `mock_rank` is the one public artefact both sides can legitimately
    see, and it is `None` for a prospect no draft class ranked."""

    player_id: int
    first_name: str
    last_name: str
    position: Position
    mock_rank: int | None
