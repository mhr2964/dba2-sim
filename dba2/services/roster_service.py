"""`sign` / `waive` / `release` -- §4.4's roster-money lifecycle, and the
one the design doc calls "the single most valuable test in Phase 0" (§8
step 8, §9 risks table): waiving a contract with remaining guaranteed
seasons must generate the correct `dead_cap_charges` rows.

Every function here wraps its writes in one `transaction(ex)` block (§5:
"a trade's writes -- contracts, dead cap, picks, roster moves across four
repos -- run inside one transaction" is the pattern `data/db.py` exists to
support; a sign/waive/release that touched `contracts`, `contract_years`,
`dead_cap_charges`, and `players` without one atomic boundary could leave a
contract terminated with no dead-cap rows, or vice versa, on a mid-write
failure).

**All three are RMLP mutators (build step 7)** -- each is decorated with
`@roster_lock.maps_lock_contention` and calls `roster_lock.acquire` inside
its own transaction, upfront, before any write. None of the three calls
`roster_lock.open_league_gate` itself -- that is the entry points'
job (`gm_command_service.waive_player`/`release_player`/`sign_free_agent`,
`gm_service.apply_signing`/`apply_trade`, `run_phase_hook`'s branches).
Calling any of these three directly (tests, `tools/`, a future script)
requires a gate already open on the connection passed in -- `acquire`'s own
runtime assertion is what makes that failure loud instead of silent.

`waive`/`release` follow the three-step shape (§3.3): discover (unlocked,
only enough to know what to lock), lock (one `acquire` call), revalidate
every fact under the lock, then write -- `terminate_contract` is the FIRST
write in both (bug (c)/(§4.1), build step 3), its returned bool checked.

**Migration 017's constraint translation (build step 8) lives here, one
place each.** `sign()` translates `asyncpg.UniqueViolationError` on 017a
(`ux_contracts_one_active_per_player`) into the same "player already has
an active contract" `ValidationError` the in-lock guard above it raises --
identical user-facing message whichever layer actually catches the race,
so which one fired is an implementation detail, never user-visible.
`waive()` translates 017b (`ux_dead_cap_one_charge_per_contract_team_
season`) into "that contract was already waived." No raw DB error ever
reaches Discord.
"""
from __future__ import annotations

import asyncpg

from dba2.core.errors import NotFoundError, ValidationError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import contract_repo, player_repo, role_repo
from dba2.domain.contract import (
    Contract,
    ContractStatus,
    ContractType,
    DeadCapCharge,
    DeadCapReason,
    SigningMethod,
)
from dba2.domain.player import RosterStatus
from dba2.services import roster_lock


@roster_lock.maps_lock_contention
async def sign(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    team_id: int,
    signed_in_season: int,
    contract_type: ContractType,
    signing_method: SigningMethod,
    year_salaries: dict[int, int],
    year_guaranteed: dict[int, int] | None = None,
    no_trade_clause: bool = False,
    trade_kicker_pct: int = 0,
) -> Contract:
    """Creates a `contracts` header plus one `contract_years` row per season
    key in `year_salaries`, and moves the player onto the signing team's
    active roster.

    `start_season`/`end_season`/`total_years` are derived from
    `year_salaries`'s keys, not passed separately -- there is exactly one
    correct value for each given the year rows, and accepting them as
    independent parameters would let a caller pass a header that
    contradicts its own year rows.

    `year_guaranteed` defaults to `year_salaries` itself (every dollar
    guaranteed) when omitted -- the common case this Phase 0 slice exists
    to exercise (§8 step 8's 4-year *guaranteed* deal). Pass a smaller
    per-season mapping to model a partial guarantee; migration 003's
    `CHECK (guaranteed_amount <= salary)` enforces the invariant in the DB
    regardless of what this function is given.

    **RMLP §6.1 -- `acquire(conn, league_id=..., teams=[team_id],
    players=[player_id])`, a superset of the pre-RMLP behavior (which
    locked only the player).** When called from `sign_free_agent` (via
    `apply_signing`) the team lock is already held by the caller and this
    re-acquisition is a no-op (idempotent nesting, see `roster_lock`'s
    docstring), so round 5's behavior is unchanged. Locking the player
    row before re-checking "no existing active contract," inside the same
    transaction that carries this call's write (docs/design/phase-5-
    discord-presentation.md §43.6's B1 fix) is what makes that guard
    atomic against a SECOND concurrent `sign()` for the SAME player racing
    in from a different team, not just correct for a single caller.
    """
    if not year_salaries:
        raise ValidationError("sign() requires at least one season in year_salaries")

    guaranteed = year_guaranteed if year_guaranteed is not None else year_salaries
    start_season = min(year_salaries)
    end_season = max(year_salaries)
    total_years = len(year_salaries)

    async with transaction(ex) as conn:
        await roster_lock.acquire(
            conn, league_id=league_id, teams=[team_id], players=[player_id]
        )
        existing = await contract_repo.list_contracts_for_player(conn, player_id)
        if any(c.status == ContractStatus.ACTIVE for c in existing):
            raise ValidationError(
                f"Player {player_id} already has an active contract -- waive() "
                f"or release() it before signing a new one. Without this guard "
                f"a second sign() double-counts the player's cap hit on "
                f"whichever team(s) hold the active contracts (Phase 0 step-8 "
                f"critic round, B2; made atomic against a cross-team race in "
                f"round 6, B1)."
            )

        try:
            contract = await contract_repo.create_contract(
                conn,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=signed_in_season,
                start_season=start_season,
                end_season=end_season,
                total_years=total_years,
                contract_type=contract_type,
                signing_method=signing_method,
                no_trade_clause=no_trade_clause,
                trade_kicker_pct=trade_kicker_pct,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            # Migration 017a's backstop -- should be unreachable under
            # RMLP (the tier-2 player lock already serializes this), but
            # translated anyway: a raw DB error must never reach Discord,
            # and the message matches the in-lock guard above verbatim.
            raise ValidationError(
                f"Player {player_id} already has an active contract -- waive() "
                f"or release() it before signing a new one."
            ) from exc

        for season in sorted(year_salaries):
            salary = year_salaries[season]
            await contract_repo.create_contract_year(
                conn,
                contract_id=contract.id,
                season=season,
                salary=salary,
                guaranteed_amount=guaranteed.get(season, salary),
            )

        # PD-8 (docs/design/gm-command-surface.md): read the player's
        # CURRENT team_id before moving it, so a re-sign by the SAME team
        # (a real path: an expiring contract stays on the roster with
        # `team_id` unchanged until re-signed or waived/released --
        # `gm_service._load_free_agency_pool`'s own docstring) does not
        # wipe that team's own already-correct directive. Only a genuine
        # team CHANGE clears it -- the common real case this guards is a
        # true free agent (`team_id IS NULL`) signing for the first time,
        # where a STALE directive from a team this player was waived off
        # of (PD-8's own "waiver/signing" path, not just a literal trade)
        # would otherwise silently carry over to the new team.
        signing_player = await player_repo.get_player(conn, player_id)
        previous_team_id = signing_player.team_id if signing_player is not None else None
        await player_repo.set_team_and_status(conn, player_id, team_id, RosterStatus.ACTIVE)
        if previous_team_id != team_id:
            await role_repo.clear_player_directive(conn, league_id=league_id, player_id=player_id)

    return contract


@roster_lock.maps_lock_contention
async def waive(
    ex: Executor,
    *,
    league_id: int,
    contract_id: int,
    waived_in_season: int,
    stretch: bool = False,
) -> list[DeadCapCharge]:
    """§4.4's waive path -- the design doc's single most valuable Phase 0
    test target. "Remaining guaranteed seasons" = every `contract_years`
    row whose `season >= waived_in_season` (the season being waived in
    counts as still owed; only strictly-past seasons are excluded).

    Not stretched: one `dead_cap_charges` row per remaining season with
    `guaranteed_amount > 0`, `amount = that year's guaranteed_amount`,
    `reason = waive_guaranteed`.

    Stretched (`stretch=True`): the *total* remaining guarantee is spread
    across `2 * remaining_years + 1` seasons starting at
    `waived_in_season`, `reason = waive_stretched`, each row's
    `stretch_years` set to that span. Money is `BIGINT` whole dollars
    (§4), so an even split can leave a remainder; this implementation
    puts the entire remainder on the final season of the span rather than
    dropping it or spreading fractional cents -- the design doc doesn't
    specify remainder handling, so this is an explicit interpretive call,
    not a literal spec requirement.

    Returns the list of `DeadCapCharge` rows created (empty if the
    contract had no remaining guaranteed money to convert).

    **RMLP §6.2, the full three-step shape (build step 7).**
    - **Discover (unlocked):** `get_contract` -> `(team_id, player_id)`;
      `NotFoundError` if missing, `ValidationError` if `team_id IS NULL`
      (needed as an `acquire` lock target). Nothing else read here is
      trusted for a decision.
    - **`league_id` is this function's own keyword-only parameter**,
      supplied by the caller (`gm_command_service.waive_player` from its
      own `league_id`; `gm_service.apply_waivers` from its own) -- never
      derived from the discovery read (Addendum A2).
    - **Lock:** `acquire(conn, league_id=league_id, teams=[contract.
      team_id], players=[contract.player_id])`.
    - **Revalidate under lock:** the contract's `status == ACTIVE`, its
      `team_id`/`player_id` unchanged since discovery (a concurrent trade
      moves `team_id` without changing `status`, which a status-only
      recheck would miss), and its `league_id == league_id` (Addendum A2,
      belt-and-braces over `acquire`'s own team/player league check).
    - **Write, in this order (build step 3, F4):** `terminate_contract`
      FIRST, its bool checked, `False` raises `ValidationError` -- then
      dead-cap charges, then `set_team_and_status`. This ordering was
      already the shape before `acquire` existed (build step 3); RMLP
      adds the upfront lock as a second, independent layer of protection
      over the same critical section, not a replacement for it.
    """
    async with transaction(ex) as conn:
        discovered = await contract_repo.get_contract(conn, contract_id)
        if discovered is None:
            raise NotFoundError(f"No contract with id {contract_id}")
        if discovered.team_id is None:
            raise ValidationError(
                f"Contract {contract_id} has no team_id to charge dead cap to."
            )

        await roster_lock.acquire(
            conn,
            league_id=league_id,
            teams=[discovered.team_id],
            players=[discovered.player_id],
        )

        contract = await contract_repo.get_contract(conn, contract_id)
        if contract is None or contract.status != ContractStatus.ACTIVE:
            status = contract.status.value if contract is not None else "gone"
            raise ValidationError(
                f"Contract {contract_id} is not active (status={status}) -- "
                "cannot waive a contract twice."
            )
        if contract.team_id != discovered.team_id or contract.player_id != discovered.player_id:
            raise ValidationError(
                f"Contract {contract_id} changed team/player between discovery and "
                "lock acquisition -- retry."
            )
        if contract.league_id != league_id:
            raise ValidationError(
                f"Contract {contract_id} belongs to league {contract.league_id}, "
                f"not {league_id}."
            )

        terminated = await contract_repo.terminate_contract(
            conn,
            contract_id,
            status=ContractStatus.WAIVED,
            terminated_in_season=waived_in_season,
        )
        if not terminated:
            raise ValidationError(
                f"Contract {contract_id} is not active -- it was terminated by "
                "another action."
            )

        years = await contract_repo.list_contract_years_for_contract(conn, contract_id)
        remaining_years = sorted(
            (y for y in years if y.season >= waived_in_season), key=lambda y: y.season
        )

        charges: list[DeadCapCharge] = []

        try:
            if remaining_years:
                if stretch:
                    total_guaranteed = sum(y.guaranteed_amount for y in remaining_years)
                    stretch_span = 2 * len(remaining_years) + 1
                    if total_guaranteed > 0:
                        per_season = total_guaranteed // stretch_span
                        for i in range(stretch_span):
                            amount = (
                                total_guaranteed - per_season * (stretch_span - 1)
                                if i == stretch_span - 1
                                else per_season
                            )
                            if amount == 0:
                                continue
                            charge = await contract_repo.create_dead_cap_charge(
                                conn,
                                league_id=contract.league_id,
                                team_id=contract.team_id,
                                player_id=contract.player_id,
                                source_contract_id=contract.id,
                                season=waived_in_season + i,
                                amount=amount,
                                reason=DeadCapReason.WAIVE_STRETCHED,
                                created_in_season=waived_in_season,
                                stretch_years=stretch_span,
                            )
                            charges.append(charge)
                else:
                    for year in remaining_years:
                        if year.guaranteed_amount == 0:
                            continue
                        charge = await contract_repo.create_dead_cap_charge(
                            conn,
                            league_id=contract.league_id,
                            team_id=contract.team_id,
                            player_id=contract.player_id,
                            source_contract_id=contract.id,
                            season=year.season,
                            amount=year.guaranteed_amount,
                            reason=DeadCapReason.WAIVE_GUARANTEED,
                            created_in_season=waived_in_season,
                        )
                        charges.append(charge)
        except asyncpg.exceptions.UniqueViolationError as exc:
            # Migration 017b's backstop -- should be unreachable under
            # RMLP (terminate_contract's CAS already fired above, and the
            # tier-1/tier-2 locks already serialize this), but translated
            # anyway per the same "no raw DB error reaches Discord" rule.
            raise ValidationError(
                f"Contract {contract_id} was already waived by another action."
            ) from exc

        await player_repo.set_team_and_status(
            conn, contract.player_id, None, RosterStatus.FREE_AGENT
        )

    return charges


@roster_lock.maps_lock_contention
async def release(
    ex: Executor,
    *,
    league_id: int,
    contract_id: int,
    released_in_season: int,
) -> Contract:
    """Administrative release: terminates a contract with *no* dead-cap
    consequence -- e.g. cutting a fully non-guaranteed camp/exhibit deal,
    or voiding a contract for cause.

    Interpretive call: §4.4/§8 name `release` as one of "roster_service
    basics" but don't define it (unlike `waive`, which §4.4 spells out in
    full). `ContractStatus` has no dedicated "released" value, so this
    uses `voided` (§4.4's status list) rather than `waived` -- `waived` is
    reserved for `waive()`'s dead-cap-generating path.

    Enforced, not just documented: a caller who actually owes guaranteed
    money must call `waive()`, not `release()` -- this raises
    `ValidationError` if any remaining season (`season >=
    released_in_season`) still has `guaranteed_amount > 0`. Without this
    check, `release()` erases that guarantee with no dead-cap row and no
    error, silently violating §4.4's "a wrong charge is a visible row with
    a reason, not a missing number" premise (Phase 0 step-8 critic round,
    B3). Guarded the same way as `waive()` against a silent
    already-terminated re-run.

    **RMLP §6.3 (build step 7) -- identical treatment to `waive()`** (see
    that function's docstring for the full three-step shape): discover
    (unlocked) -> `league_id` is this function's own keyword-only param
    (Addendum A2) -> `acquire(conn, league_id=league_id, teams=[contract.
    team_id], players=[contract.player_id])` -> revalidate `status ==
    ACTIVE` + `team_id`/`player_id` unchanged + `league_id` agreement ->
    write.

    **§4.1 -- CAS first, guaranteed-money guard second, DECIDED (not
    "harmless either way").** `terminate_contract` runs BEFORE the
    remaining-guaranteed-money check, applying `waive()`'s same F4
    ordering uniformly to both terminating paths. Raising the
    guaranteed-money guard AFTER the CAS is safe: the raise propagates out
    of this `async with transaction(...)` block, which rolls back (the
    whole transaction at top level, the enclosing savepoint when nested),
    undoing the termination -- so the observable outcome is identical to
    checking the guard first. One deliberate, disclosed difference: if a
    contract is BOTH already-terminated AND carries guaranteed money,
    CAS-first reports "Contract N is not active" rather than "use
    waive() instead" -- the more fundamental fact, since the guard-first
    message would have told the user to `waive()` a contract that no
    longer exists.
    """
    async with transaction(ex) as conn:
        discovered = await contract_repo.get_contract(conn, contract_id)
        if discovered is None:
            raise NotFoundError(f"No contract with id {contract_id}")
        if discovered.team_id is None:
            raise ValidationError(
                f"Contract {contract_id} has no team_id."
            )

        await roster_lock.acquire(
            conn,
            league_id=league_id,
            teams=[discovered.team_id],
            players=[discovered.player_id],
        )

        contract = await contract_repo.get_contract(conn, contract_id)
        if contract is None or contract.status != ContractStatus.ACTIVE:
            status = contract.status.value if contract is not None else "gone"
            raise ValidationError(
                f"Contract {contract_id} is not active (status={status}) -- "
                "cannot release a contract twice."
            )
        if contract.team_id != discovered.team_id or contract.player_id != discovered.player_id:
            raise ValidationError(
                f"Contract {contract_id} changed team/player between discovery and "
                "lock acquisition -- retry."
            )
        if contract.league_id != league_id:
            raise ValidationError(
                f"Contract {contract_id} belongs to league {contract.league_id}, "
                f"not {league_id}."
            )

        terminated = await contract_repo.terminate_contract(
            conn,
            contract_id,
            status=ContractStatus.VOIDED,
            terminated_in_season=released_in_season,
        )
        if not terminated:
            raise ValidationError(
                f"Contract {contract_id} is not active -- it was terminated by "
                "another action."
            )

        years = await contract_repo.list_contract_years_for_contract(conn, contract_id)
        remaining_guaranteed = sum(
            y.guaranteed_amount for y in years if y.season >= released_in_season
        )
        if remaining_guaranteed > 0:
            raise ValidationError(
                f"Contract {contract_id} still has {remaining_guaranteed} in "
                f"guaranteed money for season(s) >= {released_in_season} -- "
                f"use waive() instead of release() so the dead-cap ledger "
                f"records it."
            )

        await player_repo.set_team_and_status(
            conn, contract.player_id, None, RosterStatus.FREE_AGENT
        )
        released = await contract_repo.get_contract(conn, contract_id)

    assert released is not None  # just terminated it inside this same transaction
    return released
