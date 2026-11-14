"""Draft-board construction -- the I/O half of §9.4
(docs/design/phase-2-manager-minds.md §9.4, §12 step 9), delegating every
formula to the pure `managers.decisions.draft` module.

**Scope boundary, stated because it is a real one.** Nothing in the
committed codebase produces a real `draft_eligible` player yet (Gap F,
§12 step 9's own finding) -- the only source is `tools/_gm_draft_class_
stub.py`, a harness tool this service must not import (`managers`'s
"nothing under `dba2/` may import `tools/`" rule extends to `services/`
too, and there is no reason for a service to reach into a quarantined
harness anyway). `build_draft_board_for_team` below therefore takes an
already-assembled `prospects: tuple[RosterPlayer, ...]` as a caller-
supplied fact, the same discipline `free_agency_service.py`'s own module
docstring states for `window`/`room`. Running a real, multi-team draft
(`managers.decisions.draft.run_draft`) needs a `LeagueSnapshot`-scale
join of every team's philosophy/window/`NeedMatrix` -- `services/gm_
service.py`'s job, not built this dispatch -- so this module wires only
the one real piece of I/O this step's own scope calls for: loading the
real `rookie_scale` table.
"""
from __future__ import annotations

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor
from dba2.data.repositories import contract_repo, team_repo
from dba2.domain.league import LeagueCapSettings
from dba2.managers.decisions.draft import DraftBoardEntry, build_draft_board
from dba2.managers.picks import RookieScaleTable
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning


async def load_rookie_scale(ex: Executor) -> RookieScaleTable:
    """`contract_repo.list_rookie_scale`, keyed by `pick_number` -- the
    shape `managers.picks.RookieScaleTable` itself documents as its
    caller's job to build."""
    rows = await contract_repo.list_rookie_scale(ex)
    return {row.pick_number: row for row in rows}


async def build_draft_board_for_team(
    ex: Executor,
    *,
    team_id: int,
    prospects: tuple[RosterPlayer, ...],
    league_rng_seed: int,
    season: int,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[DraftBoardEntry, ...]:
    """One team's pre-draft board over `prospects` (see module docstring
    for where `prospects` comes from at this phase). Loads `team_repo.
    get_team` (for `gm_philosophy`) and the real `rookie_scale` table,
    then delegates to `managers.decisions.draft.build_draft_board`. Raises
    `NotFoundError` if `team_id` does not exist."""
    team = await team_repo.get_team(ex, team_id)
    if team is None:
        raise NotFoundError(f"team {team_id} does not exist")
    rookie_scale = await load_rookie_scale(ex)
    return build_draft_board(
        prospects,
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        season=season,
        philosophy=team.gm_philosophy,
        window=window,
        cap_settings=cap_settings,
        rookie_scale=rookie_scale,
        tuning=tuning,
    )
