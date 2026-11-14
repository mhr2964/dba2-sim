"""Shared prelude and post-commit follow-up helpers for every cog whose
commands act on one team (GM-6, docs/design/gm-command-surface.md).

**Moved here, not copied.** `_resolve_league_and_team`/`_safe_team_panel`/
`_safe_get_player` were `bot/cogs/roster.py`'s own private helpers --
already the product of two critic rounds (docs/design/phase-5-discord-
presentation.md §43.1 blocking issue 1, §43.2 nit 3, which created
`bot/post_commit.py` precisely because "a future `offers.py` cog would
have been a third independent copy"). Copying them into a second cog
(`bot/cogs/gm.py`) now would re-commit the exact mistake that module was
built to end, so they move to this module instead, and both cogs call it.
`bot/post_commit.py` itself is unchanged -- this module is built on top
of it, the same way `roster.py` always was.
"""
from __future__ import annotations

import discord

from dba2.core.errors import NotFoundError
from dba2.data.db import get_pool
from dba2.data.repositories import player_repo, team_repo
from dba2.domain.player import Player
from dba2.services import view_service

from .context import current_cycle_index, resolve_league
from .gates import ensure_can_act_for_team, ensure_command_legal
from .post_commit import honest_suffix, run_after_commit

PANEL_FOLLOWUP_FAILURE_SUFFIX = honest_suffix("the updated team panel couldn't be shown")
PLAYER_FOLLOWUP_FAILURE_SUFFIX = honest_suffix("the updated player info couldn't be shown")


class PlayerFollowUpNotFound(Exception):
    """Raised by `safe_get_player` when its post-write re-fetch finds no
    such player -- kept deliberately distinct from `safe_get_player`
    returning `None` (a follow-up EXCEPTION, `_PLAYER_FOLLOWUP_FAILURE_
    SUFFIX`'s case). `player_repo.get_player` returns `Player | None`,
    and `None` is itself a LEGITIMATE result (no such player) --
    `post_commit.run_after_commit`'s own docstring names this exact
    hazard for any `Optional`-returning follow-up ("if `coro`'s own
    successful return value can legitimately be `None` too... the
    caller's `coro` should return an explicit non-`None` sentinel on
    success"). Without this, a genuinely-missing player produced the
    same "couldn't be shown -- error logged" message a real fetch
    exception gets, even though nothing actually failed to run -- see
    `safe_get_player`'s own docstring for the fix."""

    def __init__(self, player_id: int) -> None:
        super().__init__(f"No player {player_id} found on the post-write re-fetch.")
        self.player_id = player_id


class _PlayerMissingSentinel:
    """Internal marker -- see `safe_get_player`'s docstring. Never compared
    by value, only by identity (`is`), so a plain instance (not an
    enum/`None`) is sufficient and cannot collide with a real `Player` or
    a genuine `None`."""


_PLAYER_MISSING = _PlayerMissingSentinel()


async def resolve_league_and_team(
    interaction: discord.Interaction, *, code: str, command: str, action: str
):
    """Shared prelude every team-scoped command runs before `defer()`:
    resolve the league, check the phase gate, resolve the team by code,
    and check the bot-layer permission -- all fast/synchronous, matching
    `commissioner.py`'s own "checked before defer()" convention. Raises
    (never replies itself) so each cog's own `except` clause controls the
    reply, matching every call site's existing shape."""
    pool = await get_pool()
    league = await resolve_league(pool, interaction)
    ensure_command_legal(command, league.current_phase)
    team = await team_repo.get_team_by_code(pool, league.id, code.upper())
    if team is None:
        raise NotFoundError(f"No team with code {code!r} in this league.")
    ensure_can_act_for_team(interaction.user.id, league, team, action=action)
    return league, team


async def safe_team_panel(pool, *, league, team):
    """The post-write half of §43.1 blocking issue 1's fix: run strictly
    AFTER a write has already committed, and never raise -- a failure
    here means the DB change is real but the confirmation panel couldn't
    be rendered, which is `_PANEL_FOLLOWUP_FAILURE_SUFFIX`'s job to
    disclose, not a reason to tell the user nothing happened."""
    return await run_after_commit(
        lambda: view_service.get_team_panel(
            pool,
            league_id=league.id,
            team_id=team.id,
            season=league.current_season,
            cycle_index=current_cycle_index(league),
        ),
        log_context=f"get_team_panel for team {team.id}",
    )


async def safe_get_player(pool, player_id: int) -> Player | None:
    """`/gm role`/`/gm directive`'s post-write player re-fetch. `player_
    repo.get_player` returns `Player | None`, so its own legitimate "not
    found" result is indistinguishable from `run_after_commit`'s `None`-
    on-exception return unless `coro` is made to never return `None` on
    success -- exactly the hazard `run_after_commit`'s own docstring names
    and the fix it prescribes: `coro` returns a non-`None` sentinel
    (`_PLAYER_MISSING`) for its own real "not found" result, so a bare
    `None` coming back from `run_after_commit` can only mean the fetch
    itself raised. Raises `PlayerFollowUpNotFound` for the genuinely-
    missing case (a distinct except-branch for callers, not the generic
    failure suffix); returns the `Player` on success, or `None` if the
    follow-up itself failed (logged by `run_after_commit`)."""

    async def _fetch() -> Player | _PlayerMissingSentinel:
        player = await player_repo.get_player(pool, player_id)
        return player if player is not None else _PLAYER_MISSING

    result = await run_after_commit(_fetch, log_context=f"get_player for player {player_id}")
    if result is _PLAYER_MISSING:
        raise PlayerFollowUpNotFound(player_id)
    return result
