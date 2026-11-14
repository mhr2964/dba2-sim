"""Commissioner utilities -- `/setup` (P5-13's own named third call site
for the channel reconcile: "safe to call on every startup, after every
manager assignment, and from a `/setup` command") and `/assign-manager`/
`/remove-manager` (Decision P5-41, the ONE writer of `teams.manager_
user_id` gaining its first caller).

Every command here does the DB write (via `league_service`, inside its
own transaction) FIRST, then the Discord-side reconcile AFTER it commits
(Decision P5-14: "Discord is never written to inside a database
transaction") -- `league_service.assign_manager`/`remove_manager`
themselves import no `discord` (Decision P5-1 forbids it outside `bot/`
regardless), so this cog is the one place the two steps are composed.

**Blocking Issue 6 (Stage C critic round): `/assign-manager`/`/remove-
manager` now `defer()` before any slow work, exactly where `/setup`
already does** -- right after the fast, synchronous `resolve_league`/
`ensure_command_legal` checks, before the DB write and the post-commit
`reconcile_gm_channel` round trip. The Discord reconcile call is wrapped
in `except discord.HTTPException`, matching `client.py::_reconcile_
guild`'s own identical call -- if the DB write commits but the channel
reconcile fails (a near-certainty before the B2 fix; still a real,
possible failure after it -- a permissions error, a rate limit, a
missing "Server Members Intent"), the user gets an honest followup
naming the partial failure, never silence and never "the application
did not respond."

**§42.1's Blocking Issue 2 (a later critic re-verification): `/setup`'s
own guard around `reconcile_league_channels` only caught `discord.
HTTPException`, missing the asyncpg registry write inside that same
function and any non-`HTTPException` Discord I/O failure (a bare
`ConnectionResetError`/`OSError`/`asyncio.TimeoutError`) -- the literal
same "deferred forever" failure mode Blocking Issue A fixed, for a
narrower slice of exceptions than the real failure surface. The guard
now lives inside `reconcile_league_channels` itself (per-purpose, widened
to `except Exception`), which makes that function never raise -- so the
`try/except` that used to wrap its call HERE is gone (it was dead code
after the widening, not merely redundant).** `/assign-manager`/
`/remove-manager`'s own `reconcile_gm_channel` guard below was UNCHANGED
by that round and stayed scoped to `discord.HTTPException` -- disclosed
then rather than silently left inconsistent.

**Blocking Issue B1 (a third critic re-verification, round 4): the gap
disclosed immediately above was real, not just a disclosed inconsistency
-- `reconcile_gm_channel` (unlike `reconcile_league_channels`) still
writes to Postgres and calls `guild.fetch_member` inside the exact scope
`_reconcile_gm_channel_after_commit` only guarded with `except discord.
HTTPException`, so a DB-layer exception or a bare `ConnectionResetError`/
`OSError`/`asyncio.TimeoutError` from `fetch_member` escaped uncaught.
Reproduced live: patching `channel_repo.create_gm_channel` to raise
`RuntimeError` and driving `/assign-manager`'s callback left the
interaction deferred with zero followups and the exception propagating
out of the command entirely -- the exact "deferred forever" failure mode
Blocking Issue A and §42.1's Blocking Issue 2 already fixed elsewhere,
recurring here for the third consecutive round. Fixed by widening this
guard to `except Exception`, matching the precedent those two rounds
already established.**
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import team_repo
from dba2.domain.channel import ChannelPurpose
from dba2.domain.league import League
from dba2.domain.team import Team
from dba2.phase.permissions import PermissionDenied
from dba2.services import league_service

from .. import channels as bot_channels
from ..context import NoLeagueForGuild, current_cycle_index, resolve_league
from ..gates import CommandNotLegalNow, ensure_command_legal, ensure_commissioner
from ..post_commit import honest_suffix, run_after_commit
from ..surfaces import sync_standings_message

log = get_logger(__name__)


class CommissionerCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _reply_error(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="setup",
        description="Reconcile this server's league channels (wire, standings, games).",
    )
    async def setup(self, interaction: discord.Interaction) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("setup", league.current_phase)
            ensure_commissioner(interaction.user.id, league, action="setup")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied) as exc:
            await self._reply_error(interaction, str(exc))
            return

        guild = interaction.guild
        if guild is None:
            await self._reply_error(interaction, "This command must be run in a server.")
            return

        await interaction.response.defer(ephemeral=True)

        # Blocking Issue A (Stage C critic round): this call was
        # completely unguarded -- a `discord.Forbidden` (very plausible
        # on a fresh guild where the bot doesn't yet have Manage
        # Channels, meaning a user's very first command) left the
        # interaction deferred forever with no response.
        #
        # §42.1's Blocking Issue 2 (critic re-verification): the guard
        # used to live HERE, catching only `discord.HTTPException` --
        # narrower than the real failure surface (an asyncpg error from
        # the DB write `_reconcile_one_league_channel` also does, or a
        # bare `ConnectionResetError`/`OSError`/`asyncio.TimeoutError`
        # `discord.py` raises after exhausting its own retries, is not an
        # `HTTPException` and escaped uncaught). The guard now lives
        # inside `reconcile_league_channels` itself (per-purpose, so one
        # purpose's failure never loses another's result) and widened to
        # a bare `except Exception` there -- see that function's own
        # docstring. `reconcile_league_channels` therefore never raises
        # any more, which makes a `try/except` wrapper HERE dead code
        # (unreachable, and its own message could never be shown to a
        # user) -- removed rather than left as a misleading branch.
        created = await bot_channels.reconcile_league_channels(guild, pool, league_id=league.id)

        total_purposes = len(ChannelPurpose)
        if not created:
            await self._reply_error(
                interaction,
                "Channel setup failed -- none of this league's channels could be created "
                "or reconciled (error logged; check the bot's Manage Channels permission "
                "in this server). Run /setup again once the issue is fixed.",
            )
            return

        # §42.1's Blocking Issue 2: only attempt the standings sync when
        # the standings channel itself actually reconciled this run.
        # Before this guard, a failed STANDINGS purpose (already reported
        # below via "Could not set up: standings") fell straight into
        # `sync_standings_message`, which reads the very channel that
        # just failed to reconcile and raises its own `RuntimeError` --
        # two overlapping failure messages for the one root cause.
        #
        # Round-3 critic nit: this was still a THIRD independent, hand-
        # rolled copy of the "write committed, follow-up didn't" pattern
        # (`try`/`except Exception`/a local bool flag) -- `roster.py`'s
        # own copy was already migrated to `run_after_commit`/`honest_
        # suffix` (round 2 nit), but this one, and its own §43.2 claim of
        # "zero duplicate copies remain," were missed. Migrated here to
        # match `_reconcile_gm_channel_after_commit`'s own identical
        # shape immediately below.
        standings_synced = True
        if ChannelPurpose.STANDINGS in created:

            async def _sync_standings() -> bool:
                await sync_standings_message(
                    guild,
                    pool,
                    league_id=league.id,
                    season=league.current_season,
                    cycle_index=current_cycle_index(league),
                )
                return True

            result = await run_after_commit(
                _sync_standings, log_context=f"sync_standings_message for league {league.id}"
            )
            standings_synced = result is True
        message = (
            f"Reconciled {len(created)}/{total_purposes} league channel(s): "
            f"{', '.join(c.mention for c in created.values())}."
        )
        if len(created) < total_purposes:
            missing = [p.value for p in ChannelPurpose if p not in created]
            message += (
                f" (Could not set up: {', '.join(missing)} -- see the bot's logs; "
                "will retry next /setup.)"
            )
        if not standings_synced:
            message += self._standings_sync_failure_suffix()
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="assign-manager", description="Assign a human GM to a team.")
    @app_commands.describe(code="The team's 2-3 letter code, e.g. LAL", user="The Discord user")
    async def assign_manager(
        self, interaction: discord.Interaction, code: str, user: discord.Member
    ) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("assign_manager", league.current_phase)
            # Blocking Issue C (Stage C critic round): the interaction-
            # layer half of P5-38's "enforced at both the interaction
            # layer and the service layer" split was missing here -- the
            # service layer's own `require_commissioner` check ran AFTER
            # this cog had already resolved the team by code, so a non-
            # commissioner got a different error for a real team code
            # (`PermissionDenied`, once the service check ran) than for a
            # bogus one (`NotFoundError` from `get_team_by_code`, raised
            # below before the service call is ever reached) -- a live
            # team-id enumeration oracle. `ensure_commissioner` matches
            # `/setup`'s own existing pattern: checked before ANY team
            # resolution, so both cases now fail identically.
            ensure_commissioner(interaction.user.id, league, action="assign_manager")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied) as exc:
            await self._reply_error(interaction, str(exc))
            return

        # Blocking Issue 6: defer before the DB write and the post-commit
        # Discord reconcile -- both together can exceed Discord's 3-second
        # initial-response window, exactly the reason `/setup` already
        # defers at this same point in its own flow.
        await interaction.response.defer(ephemeral=True)

        try:
            team_row = await team_repo.get_team_by_code(pool, league.id, code.upper())
            if team_row is None:
                await self._reply_error(interaction, f"No team with code {code!r} in this league.")
                return
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team_row.id,
                user_id=user.id,
                actor_user_id=interaction.user.id,
            )
        except (NotFoundError, PermissionDenied) as exc:
            await self._reply_error(interaction, str(exc))
            return
        except Exception:
            # Critic round 6: this `try` wraps real DB I/O (`get_team_by_
            # code`'s read and `assign_manager`'s own transaction), but
            # used to catch only the two domain exception types above.
            # An asyncpg connection reset, pool-acquisition timeout,
            # deadlock, or constraint violation is neither, and escaped
            # uncaught -- the exact "deferred forever" failure mode
            # Blocking Issue A, §42.1's Blocking Issue 2, and B1 already
            # fixed at five other call sites, recurring here for the DB-
            # write half of this specific command (the Discord-reconcile
            # half below already had its own `except Exception` since
            # round 4). Reproduced live before this fix: patching `league_
            # service.assign_manager` to raise a non-domain exception and
            # driving this callback produced `deferred=True`, zero
            # `response`/`followup` calls, exception escaping uncaught.
            log.exception("assign_manager DB write failed for team code %r", code)
            await self._reply_error(
                interaction,
                "Something went wrong assigning that manager (error logged) -- no change "
                "was made. Try again, and contact the bot's maintainer if it keeps failing.",
            )
            return

        reconcile_ok = await self._reconcile_gm_channel_after_commit(
            interaction, league=league, team_row=team_row, manager_user_id=user.id
        )
        message = f"{user.mention} now manages {team_row.city} {team_row.name}."
        if not reconcile_ok:
            message += self._reconcile_failure_suffix()
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="remove-manager", description="Remove a team's human GM.")
    @app_commands.describe(code="The team's 2-3 letter code, e.g. LAL")
    async def remove_manager(self, interaction: discord.Interaction, code: str) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("remove_manager", league.current_phase)
            # Blocking Issue C: same reasoning as `assign_manager`'s own
            # comment above -- the interaction-layer commissioner check
            # must run before ANY team resolution.
            ensure_commissioner(interaction.user.id, league, action="remove_manager")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied) as exc:
            await self._reply_error(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)

        try:
            team_row = await team_repo.get_team_by_code(pool, league.id, code.upper())
            if team_row is None:
                await self._reply_error(interaction, f"No team with code {code!r} in this league.")
                return
            await league_service.remove_manager(
                pool, league_id=league.id, team_id=team_row.id, actor_user_id=interaction.user.id
            )
        except (NotFoundError, PermissionDenied) as exc:
            await self._reply_error(interaction, str(exc))
            return
        except Exception:
            # Critic round 6: `remove_manager`'s twin of `assign_manager`'s
            # own identical gap above -- see that guard's comment.
            log.exception("remove_manager DB write failed for team code %r", code)
            await self._reply_error(
                interaction,
                "Something went wrong removing that manager (error logged) -- no change "
                "was made. Try again, and contact the bot's maintainer if it keeps failing.",
            )
            return

        reconcile_ok = await self._reconcile_gm_channel_after_commit(
            interaction, league=league, team_row=team_row, manager_user_id=None
        )
        message = f"{team_row.city} {team_row.name} no longer has a manager."
        if not reconcile_ok:
            message += self._reconcile_failure_suffix()
        await interaction.followup.send(message, ephemeral=True)

    async def _reconcile_gm_channel_after_commit(
        self,
        interaction: discord.Interaction,
        *,
        league: League,
        team_row: Team,
        manager_user_id: int | None,
    ) -> bool:
        """The Discord-side half of `/assign-manager`/`/remove-manager`,
        run strictly AFTER `league_service.assign_manager`/`remove_
        manager`'s own transaction has committed (Decision P5-14). Returns
        `False` (never raises) on any Discord-side OR pool-acquisition
        failure, INCLUDING "no guild object resolved" -- the caller
        always tells the user the truth about which half succeeded.

        `run_after_commit` (round 2 nit -- shared with `roster.py`'s own
        post-write follow-ups) wraps the reconcile call, matching `client.
        py::_reconcile_guild`'s own guard for the same call (widened from
        `except discord.HTTPException` in critic round 5 -- see that
        module's docstring). `reconcile_gm_channel` itself returns `None`
        on success (a bare side effect), so the wrapped closure returns
        the explicit sentinel `True` instead -- `run_after_commit` also
        returns `None` on failure, and without a non-`None` success value
        the two would be indistinguishable."""
        guild = interaction.guild
        if guild is None:
            log.warning(
                "no guild object resolved for interaction -- GM channel reconcile skipped "
                "for team %s",
                team_row.id,
            )
            return False

        async def _reconcile() -> bool:
            pool = await get_pool()
            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league.id,
                team_id=team_row.id,
                team_code=team_row.code,
                manager_user_id=manager_user_id,
                commissioner_user_id=league.commissioner_user_id,
            )
            return True

        result = await run_after_commit(
            _reconcile, log_context=f"reconcile_gm_channel for team {team_row.id}"
        )
        return result is True

    @staticmethod
    def _reconcile_failure_suffix() -> str:
        return honest_suffix(
            "the GM channel could not be set up (it will be fixed automatically on the next "
            "bot startup or the next manager change)"
        )

    @staticmethod
    def _standings_sync_failure_suffix() -> str:
        """Round-3 critic nit: `/setup`'s own copy of `_reconcile_failure_
        suffix` immediately above -- the wording changes from the pre-
        migration text ("Standings message sync failed -- see the bot's
        logs; will retry next /setup.") to `honest_suffix`'s shared
        template, a real, disclosed message-text change (not asserted
        identical), same as the wording change `8d22751`'s own migration
        of `roster.py`/`_reconcile_gm_channel_after_commit` already made
        and (per that commit's own `[CORRECTED ...]` note, docs/design/
        phase-5-discord-presentation.md) never actually tested word-for-
        word either time."""
        return honest_suffix(
            "the standings message could not be synced (it will be retried on the next /setup)"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommissionerCommands(bot))
