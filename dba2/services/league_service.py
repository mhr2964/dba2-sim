"""League creation — seeds the 30 real NBA teams and the current season's
cap-settings row from `seeds/`, per §7.2 and §8 step 8.

phase-0-foundation.md §7.2: "league-scoped seed data (the 30 NBA teams,
divisions, per-season cap constants) ships as JSON under `seeds/` and is
loaded by `league_service.create_league`." Re-tuning the cap curve or
fixing a team's division should not require a schema migration.

`seeds/` is a sibling of the `dba2/` package (§3's project layout), not
inside it, so `importlib.resources` -- which resolves paths *inside* an
installed package -- can't reach it. Deriving the seeds directory from this
file's own location (not a hardcoded absolute path, and not dependent on
the caller's cwd) is the fallback the design doc names for exactly this
case: `dba2/services/league_service.py` -> parents[2] is the repo root.
"""
from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import league_repo, offer_repo, team_repo
from dba2.domain.league import League
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.phase import permissions

_SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"


@lru_cache(maxsize=1)
def _load_teams() -> tuple[dict, ...]:
    """Cached: `seeds/teams.json` is static per-process, and `create_league`
    may run once per commissioner action -- no reason to re-read+re-parse
    disk every call. Returned as a tuple of dicts (immutable) so a caller
    can't accidentally mutate the cached list in place."""
    with open(_SEEDS_DIR / "teams.json", encoding="utf-8") as f:
        return tuple(json.load(f)["teams"])


@lru_cache(maxsize=1)
def _load_cap_settings() -> dict:
    with open(_SEEDS_DIR / "cap_settings.json", encoding="utf-8") as f:
        return json.load(f)["cap_settings"]


async def create_league(
    ex: Executor,
    *,
    discord_guild_id: int,
    name: str,
    start_season_year: int,
    current_season: int,
    commissioner_user_id: int,
    rng_seed: int,
) -> League:
    """Creates a league, seeds all 30 NBA teams, and writes `current_season`'s
    cap-settings row -- one atomic `transaction(ex)` block (§8 step 8; see
    `data/db.py`'s docstring on why atomicity across `leagues` + 30x`teams`
    + `league_cap_settings` matters here). A league left with some but not
    all of its 30 teams, or with no cap-settings row, is not a valid league
    to hand back to a caller -- every write below either all commits or all
    rolls back together.

    `current_phase` is left at `league_repo.create_league`'s own default
    (`Phase.SETUP`), which is correct per §4.1/§6.2 and not re-specified
    here.
    """
    teams = _load_teams()
    cap_settings = _load_cap_settings()

    async with transaction(ex) as conn:
        league = await league_repo.create_league(
            conn,
            discord_guild_id=discord_guild_id,
            name=name,
            start_season_year=start_season_year,
            current_season=current_season,
            commissioner_user_id=commissioner_user_id,
            rng_seed=rng_seed,
            current_phase=Phase.SETUP,
        )

        for team in teams:
            await team_repo.create_team(
                conn,
                league_id=league.id,
                code=team["code"],
                name=team["name"],
                city=team["city"],
                conference=team["conference"],
                division=team["division"],
                home_market_size=team["home_market_size"],
                gm_philosophy=GmPhilosophy(team["gm_philosophy"]),
                timeline=Timeline(team["timeline"]),
            )

        await league_repo.create_cap_settings(
            conn,
            league_id=league.id,
            season=current_season,
            **cap_settings,
        )

    return league


# ---------------------------------------------------------------------------
# Manager assignment (Decision P5-41, docs/design/phase-5-discord-
# presentation.md §19.3). The ONE writer of `teams.manager_user_id` --
# today `team_repo.set_manager`/`record_manager_assignment` exist with no
# service caller anywhere (§1 finding restated at P5-41's own text).
#
# **What this function does NOT do: touch Discord.** Decision P5-14 --
# "Discord is never written to inside a database transaction, and is
# never treated as authoritative for anything." The GM-channel branch of
# the P5-13 reconcile (`dba2.bot.channels.reconcile_gm_channel`) runs
# AFTER this function's transaction commits, called by the `bot/`-side
# command handler, never from in here -- this module stays free of any
# `discord` import (it is not `dba2/bot/`, and Decision P5-1 forbids it
# regardless).
# ---------------------------------------------------------------------------


async def assign_manager(
    ex: Executor, *, league_id: int, team_id: int, user_id: int, actor_user_id: int
) -> Team:
    """Sets `team_id`'s manager to `user_id`, closing any open `team_
    manager_history` row and opening a new one, in one transaction.
    Commissioner-only (`permissions.require_commissioner`) -- a team's
    own manager may not reassign themselves or another team. Permission
    is checked BEFORE `team_id` is resolved (Stage C critic round nit) --
    checking it after would let a non-commissioner enumerate which team
    ids exist in a league via the `NotFoundError`/no-error distinction.

    Idempotent: re-assigning the SAME `user_id` who already manages this
    team is a no-op that returns the team unchanged and writes nothing
    new -- the "double-assign" case C-13's own idempotence obligation
    names, and the shape a retried command (a Discord interaction whose
    first response timed out) would otherwise duplicate.

    **Also expires `team_id`'s outstanding `PROPOSED` trade offers**
    (Blocking Issue 5, Stage C critic round; docs/design/phase-5-discord-
    presentation.md's own P5-41 text: "assignment ... on EITHER side
    expires that team's outstanding offers") -- a newly-assigned human
    manager should not inherit an AI-initiated offer negotiated before
    they existed as this team's decision-maker. `offer_repo.expire_
    offers_for_team` runs in the SAME transaction as the `manager_user_
    id` write (a status transition is a plain DB write, not Discord I/O).
    `offer_service._lock_offer_for_answer`'s authority check depends on
    that: it reads the manager unlocked and trusts this same-transaction
    expiry to close the race, so the expiry must never move out of this
    transaction.
    """
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action="assign_manager"
    )
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")

    if team.manager_user_id == user_id:
        return team

    async with transaction(ex) as conn:
        open_assignment = await team_repo.get_open_manager_assignment(conn, team_id)
        if open_assignment is not None:
            await team_repo.close_manager_assignment(conn, open_assignment.id)
        await team_repo.set_manager(conn, team_id, user_id)
        await team_repo.record_manager_assignment(
            conn, team_id=team_id, user_id=user_id, assigned_by=actor_user_id
        )
        # Same transaction as `set_manager`: `offer_service`'s unlocked
        # manager read depends on it (see this function's docstring).
        await offer_repo.expire_offers_for_team(conn, team_id)

    return dataclasses.replace(team, manager_user_id=user_id)


async def remove_manager(
    ex: Executor, *, league_id: int, team_id: int, actor_user_id: int
) -> Team:
    """The inverse of `assign_manager` -- clears `manager_user_id` and
    closes the open `team_manager_history` row. Idempotent: a team with
    no manager is a no-op that returns it unchanged. Permission is
    checked before `team_id` is resolved, same reasoning as `assign_
    manager`'s own docstring.

    Also expires `team_id`'s outstanding `PROPOSED` trade offers
    (Blocking Issue 5 -- see `assign_manager`'s docstring; "a team that
    stops being human-managed has nobody to answer"). An authority check in
    `offer_service._lock_offer_for_answer` depends on that expiry being in
    the same transaction as `set_manager`; do not move it out.

    **Also clears `auto_pilot` in the same transaction (GM-5, docs/design/
    gm-command-surface.md).** Without this, a team removed while
    auto-piloted keeps `auto_pilot = TRUE` on a row with no manager --
    harmless while unmanaged (the derived self-managed predicate is
    `False` either way), but the NEXT `assign_manager` would silently
    drop a brand-new GM straight onto auto-pilot without anyone choosing
    it. This early-return (a team with no manager) means a row that is
    already `manager_user_id IS NULL AND auto_pilot = TRUE` -- the one
    state GM-1 declares legitimate but instantaneous -- is not reachable
    through THIS path; GM-1/GM-5 both argue it is unreachable by any real
    sequence in the first place (`/gm auto` requires a manager)."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action="remove_manager"
    )
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")

    if team.manager_user_id is None:
        return team

    async with transaction(ex) as conn:
        open_assignment = await team_repo.get_open_manager_assignment(conn, team_id)
        if open_assignment is not None:
            await team_repo.close_manager_assignment(conn, open_assignment.id)
        await team_repo.set_manager(conn, team_id, None)
        # Same transaction as `set_manager`: `offer_service`'s unlocked
        # manager read depends on it (see this function's docstring).
        await offer_repo.expire_offers_for_team(conn, team_id)
        # GM-5: same transaction as the `manager_user_id` write, so no
        # reader ever observes `manager_user_id IS NULL AND auto_pilot =
        # TRUE` past this function's commit.
        await team_repo.clear_auto_pilot(conn, team_id)

    return dataclasses.replace(team, manager_user_id=None, auto_pilot=False)
