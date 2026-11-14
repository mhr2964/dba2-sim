"""draft_picks, drafts, draft_selections, draft_classes — `draft_repo`'s
tables (§5).

phase-0-foundation.md §4.5, §6.3. §6.3: `drafts` deliberately has no
`status` column — draft lifecycle folds into the single league `Phase`
graph (`pending_lottery` = `DRAFT_LOTTERY`, in-progress = `DRAFT`, complete
= `POST_DRAFT_TRADES`); what remains here is a *pointer*
(`current_round`/`current_pick_number`), not a state, so there is no
`Draft`-lifecycle "close" function in this module beyond the lottery
timestamp below.

`complete_lottery` is this module's one "close"-shaped function:
`lottery_completed_at` is a one-time timestamp (a draft's lottery runs
exactly once), so a second call must not silently overwrite it — the
`AND lottery_completed_at IS NULL` guard mirrors
`team_repo.close_manager_assignment`'s fix (Phase 0 step-7 critic round,
B1).
"""
from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from dba2.data.db import Executor
from dba2.domain.draft import (
    AvailableProspect,
    Draft,
    DraftClass,
    DraftPick,
    DraftSelection,
    ProtectionType,
)
from dba2.domain.player import Position

_DRAFT_PICK_COLUMNS = """
    id, league_id, season, round, original_team_id, current_team_id,
    pick_number, used_for_player_id, protection_type, protection_through,
    swap_with_pick_id, conveyed, rolls_over_to_season
"""

_DRAFT_COLUMNS = """
    id, league_id, season, current_round, current_pick_number,
    lottery_completed_at, total_picks
"""

_DRAFT_SELECTION_COLUMNS = """
    id, draft_id, pick_number, round, team_id, player_id, pick_asset_id, selected_at
"""

_DRAFT_CLASS_COLUMNS = "id, league_id, class_year, player_id, mock_rank, is_generated, source"


def _map_draft_pick(r: asyncpg.Record) -> DraftPick:
    return DraftPick(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        round=r["round"],
        original_team_id=r["original_team_id"],
        current_team_id=r["current_team_id"],
        pick_number=r["pick_number"],
        used_for_player_id=r["used_for_player_id"],
        protection_type=ProtectionType(r["protection_type"]),
        protection_through=r["protection_through"],
        swap_with_pick_id=r["swap_with_pick_id"],
        conveyed=r["conveyed"],
        rolls_over_to_season=r["rolls_over_to_season"],
    )


def _map_draft(r: asyncpg.Record) -> Draft:
    return Draft(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        current_round=r["current_round"],
        current_pick_number=r["current_pick_number"],
        lottery_completed_at=r["lottery_completed_at"],
        total_picks=r["total_picks"],
    )


def _map_draft_selection(r: asyncpg.Record) -> DraftSelection:
    return DraftSelection(
        id=r["id"],
        draft_id=r["draft_id"],
        pick_number=r["pick_number"],
        round=r["round"],
        team_id=r["team_id"],
        player_id=r["player_id"],
        pick_asset_id=r["pick_asset_id"],
        selected_at=r["selected_at"],
    )


def _map_draft_class(r: asyncpg.Record) -> DraftClass:
    return DraftClass(
        id=r["id"],
        league_id=r["league_id"],
        class_year=r["class_year"],
        player_id=r["player_id"],
        mock_rank=r["mock_rank"],
        is_generated=r["is_generated"],
        source=r["source"],
    )


async def create_draft_pick(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    round: int,
    original_team_id: int,
    current_team_id: int,
    pick_number: int | None = None,
    used_for_player_id: int | None = None,
    protection_type: ProtectionType = ProtectionType.NONE,
    protection_through: int | None = None,
    swap_with_pick_id: int | None = None,
    conveyed: bool = False,
    rolls_over_to_season: int | None = None,
) -> DraftPick:
    row = await ex.fetchrow(
        f"""
        INSERT INTO draft_picks
            (league_id, season, round, original_team_id, current_team_id,
             pick_number, used_for_player_id, protection_type,
             protection_through, swap_with_pick_id, conveyed, rolls_over_to_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING {_DRAFT_PICK_COLUMNS}
        """,
        league_id,
        season,
        round,
        original_team_id,
        current_team_id,
        pick_number,
        used_for_player_id,
        protection_type.value,
        protection_through,
        swap_with_pick_id,
        conveyed,
        rolls_over_to_season,
    )
    return _map_draft_pick(row)


async def get_draft_pick(ex: Executor, pick_id: int) -> DraftPick | None:
    row = await ex.fetchrow(
        f"SELECT {_DRAFT_PICK_COLUMNS} FROM draft_picks WHERE id = $1", pick_id
    )
    return _map_draft_pick(row) if row is not None else None


async def list_draft_picks_for_team(
    ex: Executor, league_id: int, team_id: int
) -> list[DraftPick]:
    """"A team's current draft assets" — the query a trade evaluator runs
    (migration 007's `ix_draft_picks_league_current_team`, Decision 21)."""
    rows = await ex.fetch(
        f"""
        SELECT {_DRAFT_PICK_COLUMNS} FROM draft_picks
        WHERE league_id = $1 AND current_team_id = $2
        ORDER BY season, round
        """,
        league_id,
        team_id,
    )
    return [_map_draft_pick(r) for r in rows]


async def list_draft_picks_for_league_range(
    ex: Executor, league_id: int, seasons: Sequence[int]
) -> list[DraftPick]:
    """`list_draft_picks_for_season`, batched across every season in
    `seasons` in one query -- phase-2-manager-minds.md §8's fourth bulk
    loader, added alongside `contract_repo`'s three cap-sheet loaders for
    the same "30 teams x a 5-season horizon in one query, not a 150-round-
    trip N+1" reason (§8's own text). `managers/capsheet.py`'s nine-quantity
    projection (Decision P2-15) does not itself read draft picks -- none of
    §8's nine per-`(team, season)` quantities is pick-shaped -- so this
    step's own `capsheet_service.py` does not call this function; it exists
    here as the loader a later step (asset-bundle/trade-valuation across the
    same horizon, `managers/decisions/` step 8-9, not built this dispatch)
    reads instead of re-deriving the same batched query itself. Flagged in
    the step-7 report as a repository addition with no caller yet, rather
    than silently wiring it into `capsheet_service` to manufacture one."""
    rows = await ex.fetch(
        f"""
        SELECT {_DRAFT_PICK_COLUMNS} FROM draft_picks
        WHERE league_id = $1 AND season = ANY($2::int[])
        ORDER BY season, round, pick_number
        """,
        league_id,
        list(seasons),
    )
    return [_map_draft_pick(r) for r in rows]


async def list_draft_picks_for_season(ex: Executor, league_id: int, season: int) -> list[DraftPick]:
    rows = await ex.fetch(
        f"""
        SELECT {_DRAFT_PICK_COLUMNS} FROM draft_picks
        WHERE league_id = $1 AND season = $2
        ORDER BY round, pick_number
        """,
        league_id,
        season,
    )
    return [_map_draft_pick(r) for r in rows]


async def set_pick_number(ex: Executor, pick_id: int, pick_number: int) -> None:
    """The lottery resolving a pick's slot in the order."""
    await ex.execute(
        "UPDATE draft_picks SET pick_number = $2 WHERE id = $1", pick_id, pick_number
    )


async def set_current_team(ex: Executor, pick_id: int, current_team_id: int) -> None:
    """A trade reassigning pick ownership. `original_team_id` never changes
    (it's the pick's identity, §4.5) — only this column moves."""
    await ex.execute(
        "UPDATE draft_picks SET current_team_id = $2 WHERE id = $1",
        pick_id,
        current_team_id,
    )


async def set_used_for_player(ex: Executor, pick_id: int, player_id: int) -> None:
    await ex.execute(
        "UPDATE draft_picks SET used_for_player_id = $2 WHERE id = $1", pick_id, player_id
    )


async def mark_conveyed(ex: Executor, pick_id: int) -> None:
    """Setting an already-`TRUE` `conveyed` flag to `TRUE` again loses no
    information (unlike a timestamp column), so this is a plain idempotent
    setter with no guard — safe to call more than once."""
    await ex.execute(
        "UPDATE draft_picks SET conveyed = TRUE WHERE id = $1", pick_id
    )


async def create_draft(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    total_picks: int,
    current_round: int = 1,
    current_pick_number: int = 1,
) -> Draft:
    row = await ex.fetchrow(
        f"""
        INSERT INTO drafts (league_id, season, current_round, current_pick_number, total_picks)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_DRAFT_COLUMNS}
        """,
        league_id,
        season,
        current_round,
        current_pick_number,
        total_picks,
    )
    return _map_draft(row)


async def get_draft(ex: Executor, draft_id: int) -> Draft | None:
    row = await ex.fetchrow(f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE id = $1", draft_id)
    return _map_draft(row) if row is not None else None


async def get_draft_for_season(ex: Executor, league_id: int, season: int) -> Draft | None:
    row = await ex.fetchrow(
        f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE league_id = $1 AND season = $2",
        league_id,
        season,
    )
    return _map_draft(row) if row is not None else None


async def get_draft_for_season_for_update(
    ex: Executor, league_id: int, season: int
) -> Draft | None:
    """`SELECT ... FOR UPDATE` on the season's `drafts` row -- **the draft's
    mutual-exclusion token** (Decision P5-98.g step 3). A draft pick is a
    read-decide-write over the whole board (who is on the clock comes from
    the eligible pick list, not from this row's advisory pointer), and two
    appliers running that sequence concurrently would both read the same
    team on the clock. This row is the one thing every pick applier must
    pass through, so taking it `FOR UPDATE` serialises them.

    **Taken LAST**, after the league gate (if the caller opens one) and
    after `leagues ... FOR SHARE` (if the caller takes it): gate ->
    `leagues` -> `drafts`, never the reverse, which is the whole content of
    the no-deadlock claim. The wait is bounded only if the caller's
    transaction has set `lock_timeout` -- `open_league_gate` does it for a
    gated caller, `roster_lock.set_lock_timeout` for a gate-less one -- and
    the caller is also responsible for mapping the resulting 55P03 to its
    own message, because the generic entity wording names the wrong noun
    for a non-entity row (OS-13). Must run inside an open transaction on a
    `Connection`; the lock is held to commit."""
    row = await ex.fetchrow(
        f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE league_id = $1 AND season = $2 FOR UPDATE",
        league_id,
        season,
    )
    return _map_draft(row) if row is not None else None


async def advance_draft_pointer(
    ex: Executor, draft_id: int, *, current_round: int, current_pick_number: int
) -> None:
    """The one legal way to move `drafts`'s on-the-clock pointer — keeps
    `current_round` and `current_pick_number` updated together, same
    rationale as `player_repo.set_team_and_status` keeping its pair of
    columns in sync."""
    await ex.execute(
        "UPDATE drafts SET current_round = $2, current_pick_number = $3 WHERE id = $1",
        draft_id,
        current_round,
        current_pick_number,
    )


async def complete_lottery(ex: Executor, draft_id: int) -> None:
    """Sets `lottery_completed_at = NOW()` on a draft whose lottery hasn't
    already run.

    A no-op on a draft that's already completed its lottery — the
    `AND lottery_completed_at IS NULL` guard is load-bearing, not
    decorative: without it, a second call silently overwrites the true
    lottery timestamp with no error (Phase 0 step-7 critic round, B1's bug
    class)."""
    await ex.execute(
        "UPDATE drafts SET lottery_completed_at = NOW() "
        "WHERE id = $1 AND lottery_completed_at IS NULL",
        draft_id,
    )


async def create_draft_selection(
    ex: Executor,
    *,
    draft_id: int,
    pick_number: int,
    round: int,
    team_id: int,
    player_id: int,
    pick_asset_id: int,
) -> DraftSelection:
    row = await ex.fetchrow(
        f"""
        INSERT INTO draft_selections
            (draft_id, pick_number, round, team_id, player_id, pick_asset_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_DRAFT_SELECTION_COLUMNS}
        """,
        draft_id,
        pick_number,
        round,
        team_id,
        player_id,
        pick_asset_id,
    )
    return _map_draft_selection(row)


async def get_draft_selection(ex: Executor, selection_id: int) -> DraftSelection | None:
    row = await ex.fetchrow(
        f"SELECT {_DRAFT_SELECTION_COLUMNS} FROM draft_selections WHERE id = $1",
        selection_id,
    )
    return _map_draft_selection(row) if row is not None else None


async def list_draft_selections_for_draft(ex: Executor, draft_id: int) -> list[DraftSelection]:
    rows = await ex.fetch(
        f"""
        SELECT {_DRAFT_SELECTION_COLUMNS} FROM draft_selections
        WHERE draft_id = $1
        ORDER BY pick_number
        """,
        draft_id,
    )
    return [_map_draft_selection(r) for r in rows]


async def create_draft_class_entry(
    ex: Executor,
    *,
    league_id: int,
    class_year: int,
    player_id: int,
    mock_rank: int | None = None,
    is_generated: bool = True,
    source: str | None = None,
) -> DraftClass:
    row = await ex.fetchrow(
        f"""
        INSERT INTO draft_classes
            (league_id, class_year, player_id, mock_rank, is_generated, source)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_DRAFT_CLASS_COLUMNS}
        """,
        league_id,
        class_year,
        player_id,
        mock_rank,
        is_generated,
        source,
    )
    return _map_draft_class(row)


def _escape_like(query: str) -> str:
    """`%`, `_` and `\\` are `ILIKE`'s own metacharacters -- escaped so a
    person's typing is matched as the characters they typed, paired with the
    `ESCAPE '\\'` clause in the statement below (round-1 critic's minor 9).
    The backslash goes first, or it would escape the escapes."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_available_prospects(
    ex: Executor, *, league_id: int, season: int, query: str = "", limit: int = 25
) -> list[AvailableProspect]:
    """The board `/draft`'s autocomplete offers: this league's
    `DRAFT_ELIGIBLE` players that this season's draft has not already taken
    (Decision P5-98.k).

    **"Already taken" is read from `draft_selections`, not from
    `players.roster_status`,** because `apply_draft` deliberately leaves a
    drafted prospect `DRAFT_ELIGIBLE` on no roster (§7 L1): placement is an
    unbuilt slice. Scoped to THIS draft (`drafts.league_id`/`season`), which
    is the same per-draft scope the applier's own taken-set filter uses --
    and the same reason §7 L9's cross-season twin stays open.

    `query` is matched as a case-insensitive prefix on either name and is
    passed as a bind parameter, never concatenated; the `%` is SQL-side. An
    empty `query` (the dropdown before anyone types) matches everything.

    **`ILIKE`'s own metacharacters are escaped first** (round-1 critic's
    minor 9). Binding the parameter stops injection; it does not stop
    `ILIKE` reading `%`, `_` and `\\` in what a person typed as a pattern --
    `Numb%` matched every prospect and `Numbe_0` matched `Number0`, while a
    name that really contains one of those characters could not be typed at
    all. They are escaped here (backslash, with an explicit `ESCAPE`
    clause), so the dropdown matches the characters somebody typed.
    `ORDER BY mock_rank NULLS LAST, last_name, id` -- `mock_rank` is the
    only ordering a human may legitimately see (§7 L11), and the trailing
    `id` makes the tail of an unranked class deterministic rather than
    Postgres-arbitrary."""
    rows = await ex.fetch(
        """
        SELECT p.id, p.first_name, p.last_name, p.primary_position, dc.mock_rank
        FROM players p
        LEFT JOIN draft_classes dc
            ON dc.player_id = p.id AND dc.league_id = p.league_id AND dc.class_year = $2
        WHERE p.league_id = $1
          AND p.roster_status = 'draft_eligible'
          AND NOT EXISTS (
              SELECT 1 FROM draft_selections ds
              JOIN drafts d ON d.id = ds.draft_id
              WHERE ds.player_id = p.id AND d.league_id = $1 AND d.season = $2
          )
          AND (
              $3 = ''
              OR p.last_name ILIKE $3 || '%' ESCAPE '\\'
              OR p.first_name ILIKE $3 || '%' ESCAPE '\\'
          )
        ORDER BY dc.mock_rank NULLS LAST, p.last_name, p.id
        LIMIT $4
        """,
        league_id,
        season,
        _escape_like(query),
        limit,
    )
    return [
        AvailableProspect(
            player_id=r["id"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            position=Position(r["primary_position"]),
            mock_rank=r["mock_rank"],
        )
        for r in rows
    ]


async def list_draft_class(ex: Executor, league_id: int, class_year: int) -> list[DraftClass]:
    rows = await ex.fetch(
        f"""
        SELECT {_DRAFT_CLASS_COLUMNS} FROM draft_classes
        WHERE league_id = $1 AND class_year = $2
        ORDER BY mock_rank NULLS LAST
        """,
        league_id,
        class_year,
    )
    return [_map_draft_class(r) for r in rows]
