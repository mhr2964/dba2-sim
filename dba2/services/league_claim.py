"""The league-level guard every human-initiated write shares: lock the
`leagues` row `FOR SHARE`, refuse while Stage E's advance claim is held,
and confirm the locked `(phase, season)` still equals the one the caller
decided against.

**Why this is its own module rather than a private helper on one surface.**
It was written as `offer_service._check_claim` for the trade-offer review
surface (docs/design/phase-5-trade-offer-review-surface.md §5.3 step 2),
and P5-90 rule 2 makes it a rule about *every* human-initiated write, not
about offers -- `/draft` is the second caller (Decision P5-98.g step 2)
and Stage E's other roster commands are the next. A second copy of a
refusal rule is how two surfaces start disagreeing about when a press
blocks a person, so the rule lives in one place and the messages a person
reads live with it.

The read itself is `league_repo.get_league_state_for_claim_check`, whose
own docstring carries the `FOR SHARE`-versus-`FOR UPDATE` reasoning; this
module is the policy over that read, not a second query.
"""

from __future__ import annotations

from dba2.core.errors import ConcurrencyError, NotFoundError
from dba2.data.repositories import league_repo
from dba2.domain.league import LeagueClaimState
from dba2.services import roster_lock

CLAIM_HELD_MESSAGE = "The commissioner is advancing the league -- try again in a moment."
LEAGUE_ADVANCED_MESSAGE = "The league advanced while that was running -- try again."


async def check_claim(
    conn,
    *,
    gate: roster_lock.LeagueGate | None,
    league_id: int,
    require_phase_agreement: bool = True,
) -> LeagueClaimState:
    """§5.3 step 2 / §4.2 step (e): lock the `leagues` row `FOR SHARE` and
    refuse if Stage E's claim is held. Read under the ROW lock, not the
    advisory gate: a press's `UPDATE leagues SET advance_claimed_at` takes
    the row lock and never the gate, so a claim read under the gate alone
    could be true at read time and false by write time (P5-90 rule 2).

    Also asserts the locked `(current_phase, current_season)` equals the
    gate's -- a mismatch means the league advanced between the gate read and
    this lock. `gate.season`/`gate.phase` stay the only season/phase anything
    downstream reads; this is a guard, not a second source. `veto` passes
    `require_phase_agreement=False`: it gates on `ALL_PHASES` and reads this
    row for the claim only.

    `gate=None` is the claim-only shape for a write that opens no gate -- the
    four settings setters (P5-99.t). There is then no gate-read pair to agree
    with, so it needs `require_phase_agreement=False`; the default (`True`)
    with no gate is a programming error and raises `ValueError` here, before
    any read, rather than an `AttributeError` on `gate.phase` from a refusal
    path a person can reach."""
    if gate is None and require_phase_agreement:
        raise ValueError(
            "check_claim needs a gate to check phase agreement against; pass "
            "require_phase_agreement=False for a write that opens no gate"
        )
    state = await league_repo.get_league_state_for_claim_check(conn, league_id)
    if state is None:
        raise NotFoundError(f"league {league_id} does not exist")
    if state.advance_claimed_at is not None:
        raise ConcurrencyError(CLAIM_HELD_MESSAGE)
    if require_phase_agreement and (state.current_phase, state.current_season) != (
        gate.phase,
        gate.season,
    ):
        raise ConcurrencyError(LEAGUE_ADVANCED_MESSAGE)
    return state
