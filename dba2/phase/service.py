"""advance / force_advance — the only two ways `leagues.current_phase`
should ever change, plus the `require_phase` guard services call before
mutating state under a phase constraint.

phase-0-foundation.md §6.2, §6.5. This file does I/O (`phase/` is not
`domain/` — it legitimately talks to Postgres, per §3's package layout).

Relocated (was a documented pre-approved shortcut before `league_repo`
existed — see git history / EFFORT_LEDGER.md for the prior version of this
docstring): `_write_transition`'s reads/writes against `leagues` and
`phase_history` now go through `league_repo.get_phase_state_for_update`,
`league_repo.set_current_phase`, and `league_repo.record_phase_transition`
instead of raw SQL, per §5's module map ("league_repo — ... phase state
reads/writes, phase_history").
"""
from __future__ import annotations

from dba2.core.errors import PhaseError, ValidationError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.phase.graph import is_legal_transition
from dba2.phase.graph import legal_next_phases as legal_next_phases  # re-export, §8 step 6


async def advance(ex: Executor, league_id: int, to_phase: Phase, actor_user_id: int) -> None:
    """Advance `league_id` to `to_phase`.

    Validated against `phase/graph.py`'s `NEXT_PHASES` adjacency map;
    raises `PhaseError` if `to_phase` is not a legal next phase from the
    league's current phase, `ValidationError` if `league_id` doesn't exist.
    On success, writes a `phase_history` row and updates
    `leagues.current_phase`/`phase_entered_at`, atomically (both statements
    run inside one `transaction()` block, and the current-phase read that
    the legality check depends on uses `SELECT ... FOR UPDATE` inside that
    same transaction so a concurrent `advance`/`force_advance` on the same
    league can't race past the check).
    """
    async with transaction(ex) as conn:
        state = await league_repo.get_phase_state_for_update(conn, league_id)
        if state is None:
            raise ValidationError(f"No league with id {league_id}")
        from_phase, season = state

        if not is_legal_transition(from_phase, to_phase):
            raise PhaseError(
                f"Illegal phase transition for league {league_id}: "
                f"{from_phase.value} -> {to_phase.value} is not in the phase graph "
                f"(legal next phases: "
                f"{sorted(p.value for p in legal_next_phases(from_phase))})"
            )

        await _write_transition(
            conn,
            league_id=league_id,
            season=season,
            from_phase=from_phase,
            to_phase=to_phase,
            actor_user_id=actor_user_id,
            forced=False,
            reason=None,
        )


async def force_advance(
    ex: Executor,
    league_id: int,
    to_phase: Phase,
    actor_user_id: int,
    reason: str,
) -> None:
    """Bypass the phase graph and move `league_id` straight to `to_phase`.

    §6.2's commissioner escape hatch — the only sanctioned way to skip the
    graph (v1's extra branch points existed only because a commissioner ran
    an ad hoc rollover command; this makes that an explicit, audited action
    instead of an invisible extra edge in the graph). Requires a non-empty
    `reason` — raises `ValidationError` if `reason` is empty/whitespace.
    Writes `phase_history` with `forced = true`.
    """
    if not reason or not reason.strip():
        raise ValidationError("force_advance requires a non-empty reason")

    async with transaction(ex) as conn:
        state = await league_repo.get_phase_state_for_update(conn, league_id)
        if state is None:
            raise ValidationError(f"No league with id {league_id}")
        from_phase, season = state

        await _write_transition(
            conn,
            league_id=league_id,
            season=season,
            from_phase=from_phase,
            to_phase=to_phase,
            actor_user_id=actor_user_id,
            forced=True,
            reason=reason,
        )


def require_phase(current: Phase, *allowed: Phase) -> None:
    """Raise `PhaseError` if `current` is not one of `allowed`.

    The runtime-enforcement half of `command_gates.py`: guard services
    (roster moves, trade proposals, etc.) call this before mutating state.
    """
    if current not in allowed:
        raise PhaseError(
            f"Not legal in phase {current.value}; requires one of: "
            f"{sorted(p.value for p in allowed)}"
        )


async def _write_transition(
    conn: Executor,
    *,
    league_id: int,
    season: int,
    from_phase: Phase,
    to_phase: Phase,
    actor_user_id: int,
    forced: bool,
    reason: str | None,
) -> None:
    """Writes the `phase_history` row and updates `leagues.current_phase`
    via `league_repo`. Must be called with a `Connection` already inside an
    open transaction (both call sites above hold one via `transaction(ex)`),
    not a bare `Pool`.
    """
    await league_repo.record_phase_transition(
        conn,
        league_id=league_id,
        season=season,
        from_phase=from_phase,
        to_phase=to_phase,
        actor_user_id=actor_user_id,
        forced=forced,
        reason=reason,
    )
    await league_repo.set_current_phase(conn, league_id, to_phase)
