"""Waiver-cut decision -- Phase 2 architect round 8, §7.2
(docs/ledger/reports/phase-2-architect-round-8.md), the fourth module in
the `decisions/` subpackage (siblings: `trade.py`, `signing.py`,
`draft.py`). Closes Family-1 invariant 1 (roster-size bounds) and un-
vacuums metric 9 (dead cap) -- the trade-market defect (F1-F7 of that same
report) is explicitly out of scope here, a different module's job
(`gm_service.propose_packages`, deferred to step 11).

**Fallback path taken, disclosed per the architect's own instruction
(§7.2).** The preferred approach -- score each candidate as `fit_value_for
(rp) + cap_delta_bonus(...)` applied to "the room the cut does not free,"
then cut the minimum -- was tried and rejected: `cap_delta_bonus`'s own
formula (`trade.py`) prices an `outgoing` `AssetBundle` by `salary_next`
alone, treating the WHOLE salary as freed. That is exactly the figure a
real waiver does NOT free for a fully-guaranteed contract (§7.4/`roster_
service.waive`'s own semantics: the remaining guaranteed salary converts
to dead cap at par, not to freed room, at all -- the team keeps the
charge and loses the player). Feeding `cap_delta_bonus` an `outgoing=
AssetBundle(players=(candidate,))` bundle the way `trade.py` uses it for
a real trade would silently reward cutting the most expensive fully-
guaranteed contract as though it were the cheapest possible cut -- the
exact wrong-direction risk the architect's own report names in §10's risk
table ("waivers generate more dead cap than the 6% band allows"). No
parameter on `cap_delta_bonus` distinguishes "freed" from "converted to
dead cap," and inventing that distinction inside this module would be
exactly the second, disagreeing dollars->VP conversion the architect's
own spec text forbids (§7.2: "the builder must reuse that bridge, not
invent a second" -- this phase has already hit that defect class once,
§4.6's `rookie_share_y` history).

Built the disclosed fallback instead: rank ascending by `fit_value_for`,
take the lowest three, cut the one with the smallest remaining guarantee
-- `roster_service.waive`'s own definition (`dba2/services/roster_
service.py:129-131`: sum of `guaranteed_amount` for every contract-year
with `season >= waived_in_season`), matched here exactly (`_remaining_
guarantee`, below). Simple, defensible, and it actively steers toward
cheap cuts, which is what keeps metric 9 in band (§7.2's own text).

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()` (only the deterministic `rng_
for` derivation reached transitively through `fit_value_for`), no
`open()`, no `os.environ`.
"""
from __future__ import annotations

from dba2.domain.contract import ContractStatus
from dba2.domain.league import LeagueCapSettings
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.trade import fit_value_for
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import NeedMatrix
from dba2.managers.snapshot import RosterPlayer, TeamSnapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning

_WAIVE_CANDIDATE_POOL = 3
"""§7.2's fallback rule: "take the lowest three" -- the pool `fit_value_
for` ranking narrows to before the remaining-guarantee tiebreak picks the
actual cut."""


def _remaining_guarantee(roster_player: RosterPlayer, waived_in_season: int) -> int:
    """`roster_service.waive`'s own definition (`dba2/services/roster_
    service.py:129-131`), matched exactly: the sum of `guaranteed_amount`
    for every contract-year row with `season >= waived_in_season` (the
    season being waived in still counts as owed; only strictly-past
    seasons are excluded). `0` for a player with no contract --
    `select_waive_candidate`'s own candidate filter excludes those before
    this is ever called (a contract-less player has no `contract_id` for
    `roster_service.waive` to act on), so `0` here is a defensive default,
    not a reachable wrong answer for a real candidate."""
    if roster_player.contract is None:
        return 0
    return sum(
        year.guaranteed_amount
        for year in roster_player.contract.years
        if year.season >= waived_in_season
    )


def select_waive_candidate(
    team_snapshot: TeamSnapshot,
    matrix: NeedMatrix,
    *,
    league_rng_seed: int,
    season: int,
    phase: Phase,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    already_cut: frozenset[int] = frozenset(),
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[RosterPlayer, DecisionRecord]:
    """One team's own §7.2 cut decision: rank `team_snapshot.roster` (minus
    `already_cut` -- this pass's own prior cuts, never selected twice)
    ascending by `fit_value_for`, restrict to the lowest `_WAIVE_
    CANDIDATE_POOL`, and cut whichever of those three has the smallest
    `_remaining_guarantee` as of `season` -- the fallback rule, see module
    docstring. `season` is both the valuation basis `fit_value_for` reads
    ("today's market," `trade.py`'s own convention) and the `waived_in_
    season` basis `_remaining_guarantee` reads -- the same value the
    caller (`services/gm_service.py::run_waivers`) passes to `roster_
    service.waive`/`apply_waiver_inmem` as `waived_in_season`, so a single
    parameter here cannot silently drift out of sync with the season the
    real cut actually executes in.

    Returns the cut `RosterPlayer` plus its `DecisionRecord` (`subject=
    f"player:{id}"`, `verdict="waive"`, terms summing to `score` --
    matching `trade.py`/`signing.py`/`draft.py`'s own emission convention
    exactly, Family-1 invariant 5's own dependency) -- the caller is what
    turns this into a `WaiverExecution`.

    Only ever considers a player with an active contract: a contract-less
    rostered player (the real-DB edge case `gm_service.py::_next_season_
    roster_count`'s own docstring documents, T6a) has no `contract_id` for
    `roster_service.waive` to terminate and no dead-cap consequence to
    price, so it is excluded from the candidate pool entirely -- a real,
    disclosed judgment call, not an oversight. Every remaining candidate's
    contract is asserted `ACTIVE` (never `WAIVED`/`EXPIRED`) rather than
    silently trusted -- the architect's own explicit guard (§7.3).

    **B1 fix (critic round, waivers dead-cap over-spend).** A candidate
    whose contract is already ending exactly THIS season (`max(y.season
    for y in rp.contract.years) == season` -- `gm_service.py::_expiring_
    by_team`'s own exact-equality test, verbatim) is excluded from the
    pool entirely, never scored. That player is leaving the roster for
    free at the season boundary regardless of anything this function
    does; voluntarily waiving them only converts a $0 departure into a
    real dead-cap charge, and (worse) lets them silently "count" toward
    `run_waivers`'s own roster-max trigger even though cutting them
    changes nothing about next-season occupancy (they were never going to
    occupy that slot next season either way) -- `select_waive_candidate`'s
    own ranking (ascending `fit_value_for`, tiebreak on smallest
    remaining guarantee) was previously selecting exactly these players
    disproportionately, since a contract in its final guaranteed year
    trivially has the smallest remaining guarantee of any candidate.
    Excluding them from the pool is the fix, rather than trying to teach
    the ranking to avoid them -- there is no legitimate reason to ever
    choose to waive a player who costs nothing to just let expire.

    Raises `ValueError` if no eligible candidate remains (every roster
    player is either already cut this pass, contract-less, or expiring
    this season) -- `run_waivers`'s own trigger only calls this while the
    team is genuinely over its next-season occupancy target, so an
    eligible, contracted, non-expiring candidate is expected to exist; a
    caller reaching this with none is a real caller-contract violation,
    surfaced loudly rather than silently skipped."""
    candidates = [
        rp
        for rp in team_snapshot.roster
        if rp.id not in already_cut
        and rp.contract is not None
        and max(y.season for y in rp.contract.years) != season
    ]
    for rp in candidates:
        contract_status = rp.contract.contract.status  # non-None: candidates filter above
        assert contract_status is ContractStatus.ACTIVE, (
            f"select_waive_candidate: player {rp.id} on team {team_snapshot.id}'s roster "
            f"carries a non-ACTIVE contract (status={contract_status!r}) -- a WAIVED/EXPIRED "
            f"contract must never reach the cut-candidate pool"
        )
    if not candidates:
        raise ValueError(
            f"team {team_snapshot.id} has no eligible (contracted, not-yet-cut) roster "
            f"player left to waive this pass"
        )

    scored = sorted(
        (
            (
                fit_value_for(
                    rp,
                    matrix,
                    league_rng_seed=league_rng_seed,
                    team_id=team_snapshot.id,
                    season=season,
                    phase=phase,
                    philosophy=philosophy,
                    window=window,
                    cap_settings=cap_settings,
                    tuning=tuning,
                ),
                rp,
            )
            for rp in candidates
        ),
        key=lambda item: (item[0], item[1].id),
    )
    pool = scored[:_WAIVE_CANDIDATE_POOL]
    # Tiebreak, disclosed judgment call: the architect's own fallback text
    # ("cut the one with the smallest remaining guarantee") does not name
    # a secondary tiebreak for two pool members with an EQUAL remaining
    # guarantee. Falling back to `item[0]` (the already-computed
    # `fit_value_for`, ascending) rather than bare `rp.id` keeps ties
    # resolved by the same "cut the weakest asset" signal stage 1 already
    # used to build the pool, instead of an arbitrary id order that would
    # silently ignore stage 1's own ranking whenever the dollar dimension
    # is neutral; `rp.id` is the final, deterministic tertiary tiebreak.
    cut_fv, cut = min(
        pool, key=lambda item: (_remaining_guarantee(item[1], season), item[0], item[1].id)
    )

    remaining = _remaining_guarantee(cut, season)
    terms = (
        ReasonTerm(label="fit_value_lost", input_value=cut_fv, weight=-1.0, contribution=-cut_fv),
        ReasonTerm(
            label="dead_cap_created",
            input_value=float(remaining),
            weight=-1.0,
            contribution=-float(remaining),
        ),
    )
    record = DecisionRecord(
        team_id=team_snapshot.id,
        philosophy=philosophy,
        window=window,
        subject=f"player:{cut.id}",
        verdict="waive",
        score=sum(term.contribution for term in terms),
        threshold=0.0,
        terms=terms,
    )
    return cut, record
