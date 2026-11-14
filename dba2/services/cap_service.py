"""`get_cap_summary` -- §4.4's cap-summary formula as one call, so no caller
above this layer has to hand-assemble the three-term sum itself.

phase-0-foundation.md §4.4:

    used(team, season) = Sigma contract_years.salary for active contracts
    covering that season + Sigma dead_cap_charges.amount for that team and
    season (+ Sigma cap_holds.amount when the caller asks for them)

Each term is already a single-call function on `contract_repo` (§5's
"no caller of this module should ever have to hand-write the per-year-row
join itself"); this module's only job is to add them together against the
league's `league_cap_settings` row for that season and produce `room`.
"""
from __future__ import annotations

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor
from dba2.data.repositories import contract_repo, league_repo
from dba2.domain.league import CapSummary


async def get_cap_summary(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    season: int,
    include_holds: bool = False,
) -> CapSummary:
    """Decision 19: `include_holds` is explicit so the signature never needs
    a rewrite once Phase 2 starts populating `cap_holds` (schema-only in
    Phase 0). `cap_holds` total is always computed and returned regardless
    of the toggle -- it's cheap, and it lets a caller see what holds would
    cost without re-querying -- but only added into `used`/`room` when
    `include_holds=True`.

    Raises `NotFoundError` if no `league_cap_settings` row exists for
    `(league_id, season)` -- there's no meaningful cap number to report
    without it, and returning a summary with a fabricated `salary_cap`
    would be worse than raising.
    """
    cap_settings = await league_repo.get_cap_settings(ex, league_id, season)
    if cap_settings is None:
        raise NotFoundError(
            f"No league_cap_settings row for league {league_id}, season {season}"
        )

    committed_salary = await contract_repo.get_committed_salary(ex, team_id, season)
    dead_cap = await contract_repo.get_dead_cap_total(ex, team_id, season)
    cap_holds = await contract_repo.get_cap_hold_total(ex, team_id, season)

    used = committed_salary + dead_cap + (cap_holds if include_holds else 0)

    return CapSummary(
        team_id=team_id,
        season=season,
        salary_cap=cap_settings.salary_cap,
        committed_salary=committed_salary,
        dead_cap=dead_cap,
        cap_holds=cap_holds,
        include_holds=include_holds,
        used=used,
        room=cap_settings.salary_cap - used,
    )
