"""Stage D''s human roster-TRANSACTION commands -- `/waive`, `/release`,
`/sign-free-agent` (docs/design/phase-5-discord-presentation.md §21 JC-4,
§27's "all six, not a subset" confirmation).

**GM-6 (docs/design/gm-command-surface.md) moved the other four of JC-4's
seven roster commands (`/set-strategy`, `/set-role`, `/set-directive`,
`/timeline`) to the `/gm` command group (`bot/cogs/gm.py`)** -- this cog
now holds only the three real asset-moving transactions, matching the
gate-class split `command_gates.py` already draws between `ROSTER_
TRANSACTION_PHASES` (closed during `DRAFT`) and `ALL_PHASES` (the four
standing-settings commands that moved). `_resolve_league_and_team`/
`_safe_team_panel`/`_safe_get_player` moved with them, to `bot/team_
command_base.py`, so both cogs share one copy instead of a second
independent one.

Every command here follows Decision P5-40's discipline exactly: a
`COMMAND_GATES` entry, a bot-layer `ensure_command_legal` check, a
bot-layer `ensure_can_act_for_team` permission check (Decision P5-38's
first half -- UX, an immediate ephemeral rejection naming why) backed by
`gm_command_service`'s own authoritative service-layer re-check of BOTH
the permission and the phase (P5-38's second half), and a `Panel` reply.
No command here moves a player, contract or pick itself -- every write
happens inside `gm_command_service`, which is the one place that decides
which existing Phase 0-4 applier to call (Decision P5-39).

Team resolution mirrors `commissioner.py`'s own `code` parameter
(`/team`'s identical shape); player resolution is by internal `player_id`
(matching `/box`'s own `game_id: int` precedent in `read.py` --
autocomplete lands in a later `/gm`-side commit, GM-10).
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.domain.contract import SigningMethod
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_command_service

from ..context import NoLeagueForGuild
from ..embeds import panel_to_embed
from ..gates import CommandNotLegalNow
from ..interaction_errors import reply_command_failure, reply_ephemeral
from ..team_command_base import (
    PANEL_FOLLOWUP_FAILURE_SUFFIX,
    resolve_league_and_team,
    safe_team_panel,
)

log = get_logger(__name__)

_SIGNING_METHOD_CHOICES = [
    app_commands.Choice(name=method.value.replace("_", " ").title(), value=method.value)
    for method in SigningMethod
    if method not in (SigningMethod.ROOKIE_SCALE, SigningMethod.DRAFT)
]


class RosterCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="waive", description="Waive a player (dead cap applies).")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The player's internal id (see /team)",
        stretch="Stretch the remaining guaranteed money over more seasons",
    )
    async def waive(
        self,
        interaction: discord.Interaction,
        code: str,
        player_id: int,
        stretch: bool = False,
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="waive", action="waive"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            charges = await gm_command_service.waive_player(
                pool,
                league_id=league.id,
                team_id=team.id,
                player_id=player_id,
                actor_user_id=interaction.user.id,
                stretch=stretch,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/waive")
            return

        # The write above already committed -- everything from here down
        # is a separate, never-raising follow-up (blocking issue 1's fix).
        # A failure here must never be reported as "no change was made."
        note = f" ({len(charges)} dead-cap charge(s) created)" if charges else " (no dead cap)"
        panel = await safe_team_panel(pool, league=league, team=team)
        if panel is None:
            content = f"Waived.{note}{PANEL_FOLLOWUP_FAILURE_SUFFIX}"
            await interaction.followup.send(content=content)
        else:
            await interaction.followup.send(content=f"Waived.{note}", embed=panel_to_embed(panel))

    @app_commands.command(name="release", description="Release a player (no dead cap).")
    @app_commands.describe(
        code="Your team's 2-3 letter code", player_id="The player's internal id (see /team)"
    )
    async def release(self, interaction: discord.Interaction, code: str, player_id: int) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="release", action="release"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            await gm_command_service.release_player(
                pool,
                league_id=league.id,
                team_id=team.id,
                player_id=player_id,
                actor_user_id=interaction.user.id,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/release")
            return

        # The write above already committed -- see /waive's identical
        # comment (blocking issue 1's fix).
        panel = await safe_team_panel(pool, league=league, team=team)
        if panel is None:
            await interaction.followup.send(content=f"Released.{PANEL_FOLLOWUP_FAILURE_SUFFIX}")
        else:
            await interaction.followup.send(content="Released.", embed=panel_to_embed(panel))

    @app_commands.command(name="sign-free-agent", description="Sign a free agent.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The player's internal id (see /team)",
        years="Contract length in seasons",
        annual_salary="Flat annual salary, whole dollars",
        signing_method="Which cap mechanism covers this signing",
    )
    @app_commands.choices(signing_method=_SIGNING_METHOD_CHOICES)
    async def sign_free_agent(
        self,
        interaction: discord.Interaction,
        code: str,
        player_id: int,
        years: app_commands.Range[int, 1, 5],
        annual_salary: app_commands.Range[int, 0, None],
        signing_method: app_commands.Choice[str],
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="sign_free_agent", action="sign_free_agent"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            await gm_command_service.sign_free_agent(
                pool,
                league_id=league.id,
                team_id=team.id,
                player_id=player_id,
                actor_user_id=interaction.user.id,
                years=years,
                annual_salary=annual_salary,
                signing_method=SigningMethod(signing_method.value),
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/sign-free-agent")
            return

        # The write above already committed -- see /waive's identical
        # comment (blocking issue 1's fix).
        panel = await safe_team_panel(pool, league=league, team=team)
        if panel is None:
            await interaction.followup.send(content=f"Signed.{PANEL_FOLLOWUP_FAILURE_SUFFIX}")
        else:
            await interaction.followup.send(content="Signed.", embed=panel_to_embed(panel))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCommands(bot))
