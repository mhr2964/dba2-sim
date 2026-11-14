"""contracts, contract_years, dead_cap_charges, cap_holds, rookie_scale —
`contract_repo`'s tables per §5's module map: "all cap-sheet queries live
here."

phase-0-foundation.md §4.4 — the section with no v1 ancestor.

The cap-summary formula §4.4 defines is a join across `contracts` and
`contract_years` (plus `dead_cap_charges`, plus `cap_holds` when the caller
opts in): `used(team, season) = Σ contract_years.salary for active contracts
covering that season + Σ dead_cap_charges.amount for that team/season (+ Σ
cap_holds.amount when requested)`. `get_committed_salary`,
`get_dead_cap_total`, and `get_cap_hold_total` below are that formula's three
terms, each a single call — no caller of this module should ever have to
hand-write the per-year-row join itself.

`terminate_contract` is the one "close"-shaped function here: it moves a
contract off `active` and stamps `terminated_in_season`, which is a one-time
fact about a contract's life (a waived/bought-out/voided contract does not
go back to active). The `AND status = 'active'` guard stops a second call
from silently overwriting `terminated_in_season` on a contract that's
already left `active` — the same bug class fixed in
`team_repo.close_manager_assignment` (Phase 0 step-7 critic round, B1).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg

from dba2.data.db import Executor
from dba2.domain.contract import (
    CapHold,
    Contract,
    ContractStatus,
    ContractType,
    ContractYear,
    DeadCapCharge,
    DeadCapReason,
    OptionDecision,
    OptionType,
    RookieScale,
    SigningMethod,
)

_CONTRACT_COLUMNS = """
    id, league_id, player_id, team_id, signed_in_season, start_season,
    end_season, total_years, contract_type, signing_method, no_trade_clause,
    trade_kicker_pct, status, terminated_in_season, created_at, terminated_at
"""

_CONTRACT_YEAR_COLUMNS = """
    contract_id, season, salary, guaranteed_amount, option_type, option_decision
"""

_DEAD_CAP_CHARGE_COLUMNS = """
    id, league_id, team_id, player_id, source_contract_id, season, amount,
    reason, created_in_season, stretch_years
"""

_CAP_HOLD_COLUMNS = "id, league_id, team_id, player_id, season, amount, hold_type"

_ROOKIE_SCALE_COLUMNS = (
    "pick_number, year_1_salary, year_2_salary, year_3_salary, year_4_salary"
)


def _map_contract(r: asyncpg.Record) -> Contract:
    return Contract(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        team_id=r["team_id"],
        signed_in_season=r["signed_in_season"],
        start_season=r["start_season"],
        end_season=r["end_season"],
        total_years=r["total_years"],
        contract_type=ContractType(r["contract_type"]),
        signing_method=SigningMethod(r["signing_method"]),
        no_trade_clause=r["no_trade_clause"],
        trade_kicker_pct=r["trade_kicker_pct"],
        status=ContractStatus(r["status"]),
        terminated_in_season=r["terminated_in_season"],
        created_at=r["created_at"],
        terminated_at=r["terminated_at"],
    )


def _map_contract_year(r: asyncpg.Record) -> ContractYear:
    return ContractYear(
        contract_id=r["contract_id"],
        season=r["season"],
        salary=r["salary"],
        guaranteed_amount=r["guaranteed_amount"],
        option_type=OptionType(r["option_type"]),
        option_decision=OptionDecision(r["option_decision"]),
    )


def _map_dead_cap_charge(r: asyncpg.Record) -> DeadCapCharge:
    return DeadCapCharge(
        id=r["id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        player_id=r["player_id"],
        source_contract_id=r["source_contract_id"],
        season=r["season"],
        amount=r["amount"],
        reason=DeadCapReason(r["reason"]),
        created_in_season=r["created_in_season"],
        stretch_years=r["stretch_years"],
    )


def _map_cap_hold(r: asyncpg.Record) -> CapHold:
    return CapHold(
        id=r["id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        player_id=r["player_id"],
        season=r["season"],
        amount=r["amount"],
        hold_type=r["hold_type"],
    )


@dataclass(frozen=True)
class ActiveContractYearRow:
    """One `contract_years` row joined out to its team/player, for
    `list_active_contract_years_for_league_range` -- a repository-layer join
    projection, not a `domain` type (no `contract_years` column carries
    `team_id`/`player_id` directly; both live on the parent `contracts`
    row). Exists so `managers/capsheet.py` (Decision P2-2(b), zero I/O) can
    group roster composition and option-salary breakdown per `(team_id,
    season)` itself, purely, from data `capsheet_service.py` already
    fetched in one query -- the same "no new `domain` dataclass for a
    join shape" call `managers/snapshot.py::RosterPlayer` already made."""

    team_id: int
    season: int
    player_id: int
    salary: int
    option_type: OptionType


def _map_rookie_scale(r: asyncpg.Record) -> RookieScale:
    return RookieScale(
        pick_number=r["pick_number"],
        year_1_salary=r["year_1_salary"],
        year_2_salary=r["year_2_salary"],
        year_3_salary=r["year_3_salary"],
        year_4_salary=r["year_4_salary"],
    )


async def create_contract(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    team_id: int | None,
    signed_in_season: int,
    start_season: int,
    end_season: int,
    total_years: int,
    contract_type: ContractType,
    signing_method: SigningMethod,
    status: ContractStatus = ContractStatus.ACTIVE,
    no_trade_clause: bool = False,
    trade_kicker_pct: int = 0,
    terminated_in_season: int | None = None,
) -> Contract:
    row = await ex.fetchrow(
        f"""
        INSERT INTO contracts
            (league_id, player_id, team_id, signed_in_season, start_season,
             end_season, total_years, contract_type, signing_method,
             no_trade_clause, trade_kicker_pct, status, terminated_in_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING {_CONTRACT_COLUMNS}
        """,
        league_id,
        player_id,
        team_id,
        signed_in_season,
        start_season,
        end_season,
        total_years,
        contract_type.value,
        signing_method.value,
        no_trade_clause,
        trade_kicker_pct,
        status.value,
        terminated_in_season,
    )
    return _map_contract(row)


async def get_contract(ex: Executor, contract_id: int) -> Contract | None:
    row = await ex.fetchrow(
        f"SELECT {_CONTRACT_COLUMNS} FROM contracts WHERE id = $1", contract_id
    )
    return _map_contract(row) if row is not None else None


async def list_contracts_for_team(
    ex: Executor, team_id: int, *, status: ContractStatus | None = None
) -> list[Contract]:
    if status is None:
        rows = await ex.fetch(
            f"""
            SELECT {_CONTRACT_COLUMNS} FROM contracts
            WHERE team_id = $1
            ORDER BY start_season
            """,
            team_id,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_CONTRACT_COLUMNS} FROM contracts
            WHERE team_id = $1 AND status = $2
            ORDER BY start_season
            """,
            team_id,
            status.value,
        )
    return [_map_contract(r) for r in rows]


async def list_contracts_for_player(ex: Executor, player_id: int) -> list[Contract]:
    rows = await ex.fetch(
        f"""
        SELECT {_CONTRACT_COLUMNS} FROM contracts
        WHERE player_id = $1
        ORDER BY start_season
        """,
        player_id,
    )
    return [_map_contract(r) for r in rows]


async def terminate_contract(
    ex: Executor,
    contract_id: int,
    *,
    status: ContractStatus,
    terminated_in_season: int,
) -> bool:
    """Moves a contract off `active`, stamps the season it happened in,
    and stamps `terminated_at = NOW()` — the real per-event timestamp
    migration 015 added specifically so `view_service`'s transactions
    merge (Decision P5-33/§22.5) has a real termination instant to sort
    on, instead of re-borrowing `created_at` (the SIGNING timestamp,
    which is what the merge wrongly used before migration 015 and is the
    bug that migration's own docstring records).

    A no-op on a contract that's already left `active` status — the
    `AND status = 'active'` guard is load-bearing, not decorative: without
    it, a second call would silently overwrite `terminated_in_season` (and
    `status`, and now `terminated_at`) on a contract that already has a
    true termination on record, with no error (Phase 0 step-7 critic
    round, B1's bug class).

    **RMLP (Roster Mutation Lock Protocol) — returns `bool`, not `None`.**
    `True` means THIS call performed the termination (the `WHERE status =
    'active'` guard matched exactly one row); `False` means the row was
    already off `active` and this call changed nothing. Parsed once, here,
    from asyncpg's own command-tag string (`Connection.execute` returns
    `"UPDATE <n>"`, never exposed to callers as a string) — `n == 0` means
    no row matched. Every caller MUST check this return value: this is the
    one statement in the whole waive/release/apply_signing critical section
    that is safe to run BEFORE any dependent write (its own `WHERE`-guarded
    `UPDATE` takes the row's lock and serializes a second concurrent caller
    against it even with no other locking in place — F4), which is exactly
    why RMLP's callers order it FIRST and treat `False` as a hard
    `ValidationError`, not a "someone else already handled it, carry on"
    no-op. Before this fix the guard was load-bearing in SQL but its result
    was silently discarded at the Python boundary, so a caller with nothing
    else serializing it (the pre-RMLP `waive`/`release`, which computed and
    wrote dead-cap charges BEFORE calling this) could still duplicate those
    dependent writes even though this statement itself never double-fired."""
    tag = await ex.execute(
        """
        UPDATE contracts
        SET status = $2, terminated_in_season = $3, terminated_at = NOW()
        WHERE id = $1 AND status = 'active'
        """,
        contract_id,
        status.value,
        terminated_in_season,
    )
    # asyncpg command tag for UPDATE is "UPDATE <rowcount>" — parse once,
    # here, so no caller ever has to know the tag's shape.
    return tag.rsplit(" ", 1)[-1] != "0"


async def set_contract_team(ex: Executor, contract_id: int, team_id: int) -> None:
    """The one legal way to move a contract from one team to another --
    a trade's contract-side counterpart to `player_repo.
    set_team_and_status`, which does the same for a `Player.team_id`/
    `roster_status`. Unlike `terminate_contract`, this is not gated to
    `status = 'active'` contracts only: a trade moves whatever contract
    the traded player is under, active or otherwise, and re-running it
    with the same `team_id` is idempotent, not a bug to guard against
    (unlike `terminate_contract`'s one-time `terminated_in_season`
    stamp, `team_id` has no "already happened" fact to protect)."""
    await ex.execute(
        "UPDATE contracts SET team_id = $2 WHERE id = $1",
        contract_id,
        team_id,
    )


async def create_contract_year(
    ex: Executor,
    *,
    contract_id: int,
    season: int,
    salary: int,
    guaranteed_amount: int,
    option_type: OptionType = OptionType.NONE,
    option_decision: OptionDecision = OptionDecision.PENDING,
) -> ContractYear:
    row = await ex.fetchrow(
        f"""
        INSERT INTO contract_years
            (contract_id, season, salary, guaranteed_amount, option_type, option_decision)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_CONTRACT_YEAR_COLUMNS}
        """,
        contract_id,
        season,
        salary,
        guaranteed_amount,
        option_type.value,
        option_decision.value,
    )
    return _map_contract_year(row)


async def get_contract_year(
    ex: Executor, contract_id: int, season: int
) -> ContractYear | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_CONTRACT_YEAR_COLUMNS} FROM contract_years
        WHERE contract_id = $1 AND season = $2
        """,
        contract_id,
        season,
    )
    return _map_contract_year(row) if row is not None else None


async def list_contract_years_for_contract(
    ex: Executor, contract_id: int
) -> list[ContractYear]:
    rows = await ex.fetch(
        f"""
        SELECT {_CONTRACT_YEAR_COLUMNS} FROM contract_years
        WHERE contract_id = $1
        ORDER BY season
        """,
        contract_id,
    )
    return [_map_contract_year(r) for r in rows]


async def set_option_decision(
    ex: Executor, contract_id: int, season: int, option_decision: OptionDecision
) -> None:
    await ex.execute(
        """
        UPDATE contract_years SET option_decision = $3
        WHERE contract_id = $1 AND season = $2
        """,
        contract_id,
        season,
        option_decision.value,
    )


async def get_committed_salary(ex: Executor, team_id: int, season: int) -> int:
    """§4.4's cap-summary formula, first term: Σ `contract_years.salary` for
    every `active` contract on this team whose year rows cover `season`.
    The single-call replacement for the per-year-row join §5 says callers
    should never have to hand-write themselves.

    `SUM(bigint)` comes back from asyncpg as `numeric` -> `Decimal`, not
    `int` -- the explicit `int(...)` cast below makes this function's `-> int`
    annotation true rather than aspirational. Left uncast, a `Decimal` like
    `Decimal('2.500E+7')` compares equal to `25_000_000` (so `==`-based tests
    never catch it) but formats/serializes wrong everywhere else (`f"{v:,}"`
    -> `2.500E+7`, `json.dumps` -> `TypeError`) -- Phase 0 step-8 critic
    round, B1."""
    total = await ex.fetchval(
        """
        SELECT COALESCE(SUM(cy.salary), 0)
        FROM contract_years cy
        JOIN contracts c ON c.id = cy.contract_id
        WHERE c.team_id = $1 AND c.status = 'active' AND cy.season = $2
        """,
        team_id,
        season,
    )
    return int(total)


async def create_dead_cap_charge(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    player_id: int,
    source_contract_id: int,
    season: int,
    amount: int,
    reason: DeadCapReason,
    created_in_season: int,
    stretch_years: int | None = None,
) -> DeadCapCharge:
    row = await ex.fetchrow(
        f"""
        INSERT INTO dead_cap_charges
            (league_id, team_id, player_id, source_contract_id, season,
             amount, reason, created_in_season, stretch_years)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING {_DEAD_CAP_CHARGE_COLUMNS}
        """,
        league_id,
        team_id,
        player_id,
        source_contract_id,
        season,
        amount,
        reason.value,
        created_in_season,
        stretch_years,
    )
    return _map_dead_cap_charge(row)


async def list_dead_cap_charges_for_team(
    ex: Executor, team_id: int, season: int
) -> list[DeadCapCharge]:
    rows = await ex.fetch(
        f"""
        SELECT {_DEAD_CAP_CHARGE_COLUMNS} FROM dead_cap_charges
        WHERE team_id = $1 AND season = $2
        """,
        team_id,
        season,
    )
    return [_map_dead_cap_charge(r) for r in rows]


async def get_dead_cap_total(ex: Executor, team_id: int, season: int) -> int:
    """§4.4's cap-summary formula, second term. `int(...)`-cast for the same
    `Decimal`-from-`SUM` reason as `get_committed_salary` (Phase 0 step-8
    critic round, B1)."""
    total = await ex.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0) FROM dead_cap_charges
        WHERE team_id = $1 AND season = $2
        """,
        team_id,
        season,
    )
    return int(total)


async def create_cap_hold(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    player_id: int,
    season: int,
    amount: int,
    hold_type: str,
) -> CapHold:
    row = await ex.fetchrow(
        f"""
        INSERT INTO cap_holds (league_id, team_id, player_id, season, amount, hold_type)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_CAP_HOLD_COLUMNS}
        """,
        league_id,
        team_id,
        player_id,
        season,
        amount,
        hold_type,
    )
    return _map_cap_hold(row)


async def list_cap_holds_for_team(ex: Executor, team_id: int, season: int) -> list[CapHold]:
    rows = await ex.fetch(
        f"""
        SELECT {_CAP_HOLD_COLUMNS} FROM cap_holds
        WHERE team_id = $1 AND season = $2
        """,
        team_id,
        season,
    )
    return [_map_cap_hold(r) for r in rows]


async def get_cap_hold_total(ex: Executor, team_id: int, season: int) -> int:
    """§4.4's cap-summary formula, third (opt-in) term — Decision 19's
    `include_holds` toggle lives in the future `cap_service`, not here; this
    just makes the number that toggle would add a single call. `int(...)`-cast
    for the same `Decimal`-from-`SUM` reason as `get_committed_salary` (Phase 0
    step-8 critic round, B1)."""
    total = await ex.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM cap_holds WHERE team_id = $1 AND season = $2",
        team_id,
        season,
    )
    return int(total)


async def get_rookie_scale(ex: Executor, pick_number: int) -> RookieScale | None:
    row = await ex.fetchrow(
        f"SELECT {_ROOKIE_SCALE_COLUMNS} FROM rookie_scale WHERE pick_number = $1",
        pick_number,
    )
    return _map_rookie_scale(row) if row is not None else None


async def list_rookie_scale(ex: Executor) -> list[RookieScale]:
    rows = await ex.fetch(
        f"SELECT {_ROOKIE_SCALE_COLUMNS} FROM rookie_scale ORDER BY pick_number"
    )
    return [_map_rookie_scale(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2 step 7 (docs/design/phase-2-manager-minds.md §8) -- three bulk
# loaders `capsheet_service.py` uses to build a whole league's cap-sheet
# projection (30 teams x a 5-season horizon, Decision P2-15) in three
# queries total instead of a 150-round-trip per-team-per-season N+1, the
# same reasoning Phase 1's bulk loaders (e.g. `player_repo.list_players_
# for_league`) already established for a game-sim batch.
# ---------------------------------------------------------------------------


async def get_committed_salary_for_league_seasons(
    ex: Executor, league_id: int, seasons: Sequence[int]
) -> dict[tuple[int, int], int]:
    """`get_committed_salary`'s formula (§4.4's first cap-summary term),
    batched across every team in `league_id` and every season in `seasons`
    in one query, keyed `(team_id, season) -> committed_salary`. A `(team_id,
    season)` pair with no active contract_years rows for that season is
    simply absent from the returned dict -- callers read it via `.get(key,
    0)`, matching `get_committed_salary`'s own "sums to 0, not NULL" contract
    for an unrepresented season.

    Deliberately the *same* join `get_committed_salary` runs (`active`
    contracts only, unfiltered by `option_type`) so that a season-0 row built
    from this function's output is byte-identical to `get_committed_salary`'s
    own return for that team/season -- Decision P2-17's cross-check depends
    on the two never being able to drift apart.

    `int(...)`-cast for the same `Decimal`-from-`SUM` reason as
    `get_committed_salary` (Phase 0 step-8 critic round, B1; restated for
    Phase 2 by §8's own text, since a `GROUP BY` sum is exactly as
    `Decimal`-shaped as a scalar one)."""
    rows = await ex.fetch(
        """
        SELECT c.team_id AS team_id, cy.season AS season,
               COALESCE(SUM(cy.salary), 0) AS total
        FROM contract_years cy
        JOIN contracts c ON c.id = cy.contract_id
        WHERE c.league_id = $1 AND c.status = 'active' AND cy.season = ANY($2::int[])
        GROUP BY c.team_id, cy.season
        """,
        league_id,
        list(seasons),
    )
    return {(r["team_id"], r["season"]): int(r["total"]) for r in rows}


async def get_dead_cap_totals_for_league_seasons(
    ex: Executor, league_id: int, seasons: Sequence[int]
) -> dict[tuple[int, int], int]:
    """`get_dead_cap_total`'s formula (§4.4's second cap-summary term),
    batched the same way `get_committed_salary_for_league_seasons` is --
    see that function's docstring for the shared reasoning (byte-identical
    join, `.get(key, 0)` contract, `Decimal`-cast boundary)."""
    rows = await ex.fetch(
        """
        SELECT team_id, season, COALESCE(SUM(amount), 0) AS total
        FROM dead_cap_charges
        WHERE league_id = $1 AND season = ANY($2::int[])
        GROUP BY team_id, season
        """,
        league_id,
        list(seasons),
    )
    return {(r["team_id"], r["season"]): int(r["total"]) for r in rows}


async def list_active_contract_years_for_league_range(
    ex: Executor, league_id: int, seasons: Sequence[int]
) -> list[ActiveContractYearRow]:
    """Every `active`-contract `contract_years` row across `league_id` for
    every season in `seasons`, joined out to `team_id`/`player_id` --
    `capsheet_service.py`'s third bulk loader, the raw material `managers.
    capsheet` groups per `(team_id, season)` to compute `roster_count` (a
    `COUNT DISTINCT player_id`) and the `team_option_salary`/`player_
    option_salary` breakdown (§8 items 4-6), neither of which is a single
    scalar `SUM` and so isn't a fit for the two dict-returning loaders
    above. Raw `BIGINT` column selects, not a `SUM` aggregate -- asyncpg
    returns these as plain `int` already, so no `Decimal` cast applies
    here (unlike the two functions above)."""
    rows = await ex.fetch(
        """
        SELECT c.team_id AS team_id, cy.season AS season, c.player_id AS player_id,
               cy.salary AS salary, cy.option_type AS option_type
        FROM contract_years cy
        JOIN contracts c ON c.id = cy.contract_id
        WHERE c.league_id = $1 AND c.status = 'active' AND cy.season = ANY($2::int[])
        """,
        league_id,
        list(seasons),
    )
    return [
        ActiveContractYearRow(
            team_id=r["team_id"],
            season=r["season"],
            player_id=r["player_id"],
            salary=r["salary"],
            option_type=OptionType(r["option_type"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Phase 2 step 10 (docs/design/phase-2-manager-minds.md §12 step 10, T1) --
# two more bulk loaders, this time returning full `Contract`/`ContractYear`
# domain objects rather than a narrow projection.
# `list_active_contract_years_for_league_range` (step 7, above) is
# insufficient for building a whole league's `managers.snapshot.
# PlayerContract` (which needs a real `Contract` header plus its full
# `ContractYear` rows, not just team/season/player/salary/option_type) --
# these two exist so `services/gm_service.py` (T3+, not built this
# dispatch) can assemble every team's `PlayerContract`s in two queries
# total instead of a per-team N+1, the same reasoning step 7's own three
# loaders already established.
# ---------------------------------------------------------------------------


async def list_active_contracts_for_league(
    ex: Executor, league_id: int
) -> tuple[Contract, ...]:
    """Every `active` `contracts` row in `league_id`, one query -- the
    `Contract` header half of the `PlayerContract`s `services/gm_service.
    py` assembles per team."""
    rows = await ex.fetch(
        f"""
        SELECT {_CONTRACT_COLUMNS} FROM contracts
        WHERE league_id = $1 AND status = 'active'
        ORDER BY team_id, start_season
        """,
        league_id,
    )
    return tuple(_map_contract(r) for r in rows)


async def list_contract_years_for_league(
    ex: Executor, league_id: int
) -> tuple[ContractYear, ...]:
    """Every `contract_years` row belonging to an `active` contract in
    `league_id`, one query -- the per-season half of the same
    `PlayerContract`s `list_active_contracts_for_league` supplies the
    header for. Joined through `contracts` since `contract_years` itself
    carries no `league_id`/`status` column of its own."""
    rows = await ex.fetch(
        """
        SELECT cy.contract_id, cy.season, cy.salary, cy.guaranteed_amount,
               cy.option_type, cy.option_decision
        FROM contract_years cy
        JOIN contracts c ON c.id = cy.contract_id
        WHERE c.league_id = $1 AND c.status = 'active'
        ORDER BY cy.contract_id, cy.season
        """,
        league_id,
    )
    return tuple(_map_contract_year(r) for r in rows)


async def list_contracts_for_league_season_events(
    ex: Executor, league_id: int, season: int
) -> tuple[Contract, ...]:
    """Phase 5's transactions merge (docs/design/phase-5-discord-
    presentation.md §7.2 item 4's "the contracts whose signed_in_season/
    terminated_in_season fall in range," Decision P5-33/B-12): every
    contract that either started OR ended in `season`, regardless of
    `status` today (a terminated contract's own row is exactly the event
    this read exists to surface, so filtering to `status = 'active'` the
    way `list_active_contracts_for_league` does would hide it). Ordered
    by `created_at` (the SIGNING timestamp) purely for a stable result
    order -- the caller (`view_service.get_league_transactions_feed`)
    re-sorts by each row's own real event timestamp (`created_at` for a
    signing, `terminated_at` for a termination, migration 015) before
    presenting anything, so this `ORDER BY` is not itself load-bearing
    for the merge's final order. `created_at`/`terminated_at` are the
    two real per-row timestamps this table carries (§22.5 calls
    contracts "season-grained only," which is true of `signed_in_season`
    /`terminated_in_season` themselves but not of either column)."""
    rows = await ex.fetch(
        f"""
        SELECT {_CONTRACT_COLUMNS} FROM contracts
        WHERE league_id = $1 AND (signed_in_season = $2 OR terminated_in_season = $2)
        ORDER BY created_at
        """,
        league_id,
        season,
    )
    return tuple(_map_contract(r) for r in rows)
