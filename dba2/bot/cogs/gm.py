"""The `/gm` command group (GM-6/GM-7, docs/design/gm-command-surface.md)
-- one discoverable surface for a team's own standing settings, in place
of four unrelated top-level verbs.

**Build step 4 moved the four existing handlers here verbatim, renamed
only at the `@app_commands.command(name=...)` level.** `/set-strategy` ->
`/gm strategy`, `/set-role` -> `/gm role`, `/set-directive` -> `/gm
directive`, `/timeline` -> `/gm timeline`.

**Build step 5 adds `/gm auto` (GM-8) and `/gm status` (GM-9).**

**Build step 6 adds `/gm player` and autocomplete (GM-10).** `player_id`
stays a raw `int` (`Choice.value`), but its `Choice.name` can now show a
real name (`"Jayson Tatum -- SF, 26"`) -- `gm_command_service`'s own
signatures are untouched, and a GM who ignores the suggestions and types
a raw id is still validated the same way today's roster-membership check
already does (autocomplete changes no authority, only what a person sees
while typing).

**`commands.GroupCog`, not a bare `app_commands.Group` (GM-6 Option A).**
A bare `Group` instance would still work at the Discord layer, but it
would silently defeat this project's own `test_command_gates_completeness`
test (GM-7(a)): that test iterates `bot.tree.get_commands()`, which
returns TOP-LEVEL commands only, so moving four commands under a plain
group makes the tree enumerate one `Group` named `"gm"` and a single
`{"gm": ALL_PHASES}` gate entry would pass while checking none of the
seven subcommands at all. `GroupCog` is `discord.py`'s own "a cog that
also doubles as a parent `Group`" shape, and GM-7(a) additionally teaches
both registration tests to `walk_commands()` instead, closing the trap
for any future group too.

**Gate keys are the qualified name** (GM-7(b)): `COMMAND_GATES` now keys
on `gm_strategy`/`gm_role`/`gm_directive`/`gm_timeline`, derived from
`"gm strategy".replace(" ", "_")` etc., not the old `set_strategy`/`set_
role`/`set_directive`/`timeline` names -- an alias table was rejected by
name (GM-7(b)) as a second place to forget a command.

**Shared helpers live in `team_command_base.py`, not copied here**
(GM-6) -- `roster.py`'s `_resolve_league_and_team`/`_safe_team_panel`/
`_safe_get_player` are already the product of two critic rounds
(docs/design/phase-5-discord-presentation.md §43.1 blocking issue 1,
§43.2 nit 3), and copying them into this second cog would re-commit the
exact mistake `bot/post_commit.py` was built to end.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import player_repo, team_repo
from dba2.domain.role import DefenseMode, ShotDiet, UsageMode
from dba2.domain.team import DefensiveScheme, GmPhilosophy, OffensiveScheme, Timeline
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.roster_commands import (
    build_directive_panel,
    build_gm_player_panel,
    build_gm_status_panel,
    build_role_panel,
    build_strategy_panel,
)
from dba2.services import gm_command_service

from ..context import NoLeagueForGuild, resolve_league
from ..embeds import panel_to_embed
from ..gates import (
    CommandNotLegalNow,
    ensure_command_legal,
    ensure_team_manager,
)
from ..interaction_errors import reply_command_failure, reply_ephemeral
from ..team_command_base import (
    PANEL_FOLLOWUP_FAILURE_SUFFIX,
    PLAYER_FOLLOWUP_FAILURE_SUFFIX,
    PlayerFollowUpNotFound,
    resolve_league_and_team,
    safe_get_player,
    safe_team_panel,
)

log = get_logger(__name__)

_OFFENSIVE_SCHEME_CHOICES = [
    app_commands.Choice(name=s.value.replace("_", " ").title(), value=s.value)
    for s in OffensiveScheme
]
_DEFENSIVE_SCHEME_CHOICES = [
    app_commands.Choice(name=s.value.replace("_", " ").title(), value=s.value)
    for s in DefensiveScheme
]
_SHOT_DIET_CHOICES = [
    app_commands.Choice(name=v.value.replace("_", " ").title(), value=v.value) for v in ShotDiet
]
_USAGE_MODE_CHOICES = [
    app_commands.Choice(name=v.value.replace("_", " ").title(), value=v.value) for v in UsageMode
]
_DEFENSE_MODE_CHOICES = [
    app_commands.Choice(name=v.value.replace("_", " ").title(), value=v.value)
    for v in DefenseMode
]
_TIMELINE_CHOICES = [
    app_commands.Choice(name="Rebuild", value="rebuild"),
    app_commands.Choice(name="Retool", value="retool"),
    app_commands.Choice(name="Win now", value="win_now"),
    app_commands.Choice(name="Auto (let the league decide)", value="auto"),
]
# GM-8: built the same way `_OFFENSIVE_SCHEME_CHOICES` already is -- a
# comprehension over the enum, so a future eighth philosophy needs no cog
# edit.
_GM_PHILOSOPHY_CHOICES = [
    app_commands.Choice(name=p.value.replace("_", " ").title(), value=p.value)
    for p in GmPhilosophy
]

_AUTOCOMPLETE_MAX_RESULTS = 25
"""Discord's own documented maximum for an autocomplete response
(`transformers.py`: "a maximum of 25") -- an over-25 result is truncated
by this code, never sent and rejected."""


async def _player_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """GM-10 -- attached to `/gm role`, `/gm directive` and `/gm player`'s
    `player_id` parameter. `player_id` stays a raw `int`; only the
    `Choice.name` shown while typing changes (`app_commands.autocomplete`
    supports `int`-valued options, `commands.py:2314-2315`).

    **Scoped off `interaction.namespace.code`, defensively.** The library
    documents that "In autocomplete interactions, the namespace might not
    be validated or filled in" (`app_commands/namespace.py:128`), and a
    user can type the player name before the team code: when `code` is
    present and resolves to a real team, filter to that roster; when it
    is absent (or does not resolve), fall back to the roster of the team
    the invoking user manages in this league; when neither resolves,
    return an empty list -- Discord simply shows no suggestions, a
    correct, non-crashing answer.

    **Must never raise.** An exception here surfaces as a broken-looking
    picker; every DB read is wrapped and any failure returns `[]`,
    logged with context -- the same never-raise discipline `bot/post_
    commit.py::run_after_commit` established for post-commit follow-ups,
    applied here to a pre-commit read.

    **Changes no authority.** `gm_command_service.set_role`/`set_
    directive` already re-validate roster membership under the lock; a
    GM who ignores the suggestions and types a raw id is refused exactly
    as before this existed."""
    try:
        pool = await get_pool()
        league = await resolve_league(pool, interaction)
        code = getattr(interaction.namespace, "code", None)
        team = None
        if code:
            team = await team_repo.get_team_by_code(pool, league.id, str(code).upper())
        if team is None:
            for candidate in await team_repo.list_teams_for_league(pool, league.id):
                if candidate.manager_user_id == interaction.user.id:
                    team = candidate
                    break
        if team is None:
            return []
        needle = current.lower()
        matches = [
            p
            for p in await player_repo.list_players_for_team(pool, team.id)
            if needle in p.first_name.lower() or needle in p.last_name.lower()
        ]
        return [
            app_commands.Choice(
                name=f"{p.first_name} {p.last_name} -- {p.primary_position.value}", value=p.id
            )
            for p in matches[:_AUTOCOMPLETE_MAX_RESULTS]
        ]
    except Exception:
        log.exception("_player_autocomplete failed for interaction %s", interaction.id)
        return []


class GmCommands(commands.GroupCog, name="gm"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="strategy", description="Set your team's strategy knobs.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        pace_target="0-100",
        offensive_scheme="Offensive scheme",
        defensive_scheme="Defensive scheme",
        help_aggression="0-100",
        three_point_bias="0-100",
        crash_oreb_bias="0-100",
    )
    @app_commands.choices(
        offensive_scheme=_OFFENSIVE_SCHEME_CHOICES, defensive_scheme=_DEFENSIVE_SCHEME_CHOICES
    )
    async def strategy(
        self,
        interaction: discord.Interaction,
        code: str,
        pace_target: app_commands.Range[int, 0, 100],
        offensive_scheme: app_commands.Choice[str],
        defensive_scheme: app_commands.Choice[str],
        help_aggression: app_commands.Range[int, 0, 100],
        three_point_bias: app_commands.Range[int, 0, 100],
        crash_oreb_bias: app_commands.Range[int, 0, 100],
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_strategy", action="set_strategy"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            strategy = await gm_command_service.set_strategy(
                pool,
                league_id=league.id,
                team_id=team.id,
                actor_user_id=interaction.user.id,
                pace_target=pace_target,
                offensive_scheme=OffensiveScheme(offensive_scheme.value),
                defensive_scheme=DefensiveScheme(defensive_scheme.value),
                help_aggression=help_aggression,
                three_point_bias=three_point_bias,
                crash_oreb_bias=crash_oreb_bias,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/gm strategy")
            return

        panel = build_strategy_panel(team=team, strategy=strategy)
        await interaction.followup.send(embed=panel_to_embed(panel))

    @app_commands.command(name="role", description="Set a player's role and target minutes.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The player's internal id (see /team) -- start typing a name to search",
        role="A short role label (e.g. primary_option, glue_guy)",
        minutes=(
            "Target minutes per game (0 = does not play). Shares are normalized "
            "across the roster, so this is a target, not a guarantee."
        ),
        locked="Prevent the AI/roster logic from overriding this role",
        rationale="Optional note explaining why",
    )
    @app_commands.autocomplete(player_id=_player_autocomplete)
    async def role(
        self,
        interaction: discord.Interaction,
        code: str,
        player_id: int,
        role: str,
        minutes: app_commands.Range[float, 0.0, 48.0],
        locked: bool = False,
        rationale: str | None = None,
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_role", action="set_role"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            player_role = await gm_command_service.set_role(
                pool,
                league_id=league.id,
                team_id=team.id,
                actor_user_id=interaction.user.id,
                player_id=player_id,
                role=role,
                minutes=minutes,
                locked=locked,
                rationale=rationale,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/gm role")
            return

        # The write above already committed -- see `/gm strategy`'s
        # identical comment (blocking issue 1's fix). Unlike the
        # panel-only follow-ups above, `build_role_panel` also needs
        # `player`, so a failed re-fetch here means no panel at all --
        # still an honest text reply, never a false "no change was made."
        try:
            player = await safe_get_player(pool, player_id)
        except PlayerFollowUpNotFound:
            # Genuinely missing, not a fetch failure.
            await interaction.followup.send(
                content=(
                    f"Role set for player {player_id}, but that player could not be found "
                    "on the re-fetch afterward -- were they removed by someone else in the "
                    "meantime?"
                )
            )
            return
        if player is None:
            await interaction.followup.send(
                content=f"Role set for player {player_id}.{PLAYER_FOLLOWUP_FAILURE_SUFFIX}"
            )
        else:
            panel = build_role_panel(team=team, player=player, role=player_role)
            await interaction.followup.send(embed=panel_to_embed(panel))

    @app_commands.command(name="directive", description="Set a player's on-court directives.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The player's internal id (see /team) -- start typing a name to search",
        shot_diet="Optional shot-selection directive -- 'balanced' clears it back to neutral",
        usage_mode="Optional usage directive -- 'balanced' clears it back to neutral",
        defense_mode="Optional help-defense directive -- 'balanced' clears it back to neutral",
    )
    @app_commands.choices(
        shot_diet=_SHOT_DIET_CHOICES,
        usage_mode=_USAGE_MODE_CHOICES,
        defense_mode=_DEFENSE_MODE_CHOICES,
    )
    @app_commands.autocomplete(player_id=_player_autocomplete)
    async def directive(
        self,
        interaction: discord.Interaction,
        code: str,
        player_id: int,
        shot_diet: app_commands.Choice[str] | None = None,
        usage_mode: app_commands.Choice[str] | None = None,
        defense_mode: app_commands.Choice[str] | None = None,
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_directive", action="set_directive"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            directive = await gm_command_service.set_directive(
                pool,
                league_id=league.id,
                team_id=team.id,
                actor_user_id=interaction.user.id,
                player_id=player_id,
                shot_diet=ShotDiet(shot_diet.value) if shot_diet is not None else None,
                usage_mode=UsageMode(usage_mode.value) if usage_mode is not None else None,
                defense_mode=DefenseMode(defense_mode.value) if defense_mode is not None else None,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/gm directive")
            return

        # The write above already committed -- see `/gm role`'s identical
        # comment (blocking issue 1's fix).
        try:
            player = await safe_get_player(pool, player_id)
        except PlayerFollowUpNotFound:
            # Genuinely missing, not a fetch failure.
            await interaction.followup.send(
                content=(
                    f"Directive set for player {player_id}, but that player could not be "
                    "found on the re-fetch afterward -- were they removed by someone else in "
                    "the meantime?"
                )
            )
            return
        if player is None:
            await interaction.followup.send(
                content=f"Directive set for player {player_id}.{PLAYER_FOLLOWUP_FAILURE_SUFFIX}"
            )
        else:
            panel = build_directive_panel(player=player, directive=directive)
            await interaction.followup.send(embed=panel_to_embed(panel))

    @app_commands.command(
        name="timeline", description="Set (or auto-classify) your team's timeline."
    )
    @app_commands.describe(
        code="Your team's 2-3 letter code", value="rebuild / retool / win_now / auto"
    )
    @app_commands.choices(value=_TIMELINE_CHOICES)
    async def timeline(
        self, interaction: discord.Interaction, code: str, value: app_commands.Choice[str]
    ) -> None:
        pool = await get_pool()
        try:
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_timeline", action="timeline"
            )
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            await gm_command_service.set_timeline(
                pool,
                league_id=league.id,
                team_id=team.id,
                actor_user_id=interaction.user.id,
                timeline=None if value.value == "auto" else Timeline(value.value),
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/gm timeline")
            return

        # The write above already committed -- see `/gm strategy`'s
        # identical comment (blocking issue 1's fix).
        panel = await safe_team_panel(pool, league=league, team=team)
        if panel is None:
            content = f"Timeline set.{PANEL_FOLLOWUP_FAILURE_SUFFIX}"
            await interaction.followup.send(content=content)
        else:
            await interaction.followup.send(embed=panel_to_embed(panel))


    @app_commands.command(
        name="auto", description="Put your team on (or take it off) auto-pilot."
    )
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        enabled="Turn auto-pilot on or off",
        philosophy="Required to turn auto-pilot ON -- ignored when turning it off",
    )
    @app_commands.choices(philosophy=_GM_PHILOSOPHY_CHOICES)
    async def auto(
        self,
        interaction: discord.Interaction,
        code: str,
        enabled: bool,
        philosophy: app_commands.Choice[str] | None = None,
    ) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("gm_auto", league.current_phase)
            team = await team_repo.get_team_by_code(pool, league.id, code.upper())
            if team is None:
                raise NotFoundError(f"No team with code {code!r} in this league.")
            # GM-8: the team's own CURRENT manager only -- not the
            # commissioner-or-manager union every other `/gm` command
            # takes. A commissioner who wants an AI team has
            # `/remove-manager`, which is what it is for.
            ensure_team_manager(interaction.user.id, league, team, action="gm_auto")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            updated = await gm_command_service.set_auto_pilot(
                pool,
                league_id=league.id,
                team_id=team.id,
                actor_user_id=interaction.user.id,
                enabled=enabled,
                philosophy=GmPhilosophy(philosophy.value) if philosophy is not None else None,
            )
        except Exception as exc:
            # `reply_command_failure` splits a domain error (shown verbatim)
            # from an unexpected one (logged, generic message) -- this guard
            # stays this wide for its reason: a DB-layer failure is neither
            # domain error and, uncaught, means "deferred forever".
            await reply_command_failure(interaction, exc, log_label="/gm auto")
            return

        verb = "enabled" if updated.auto_pilot else "disabled"
        await interaction.followup.send(content=f"Auto-pilot {verb} for {updated.code}.")

    @app_commands.command(name="status", description="Show your team's standing settings.")
    @app_commands.describe(code="Your team's 2-3 letter code")
    async def status(self, interaction: discord.Interaction, code: str) -> None:
        pool = await get_pool()
        try:
            # Same commissioner-or-manager union `/gm strategy`/`/gm role`/
            # `/gm directive`/`/gm timeline`/`/gm player` take (`team_command_
            # base.resolve_league_and_team`'s check). NOT `/gm auto`'s
            # narrower team-manager-only check (GM-8, design ~line 1152-
            # 1158): that one is deliberately the odd one out ("the team's
            # own GM choosing to delegate"), not this one.
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_status", action="gm_status"
            )
        except (
            NoLeagueForGuild,
            CommandNotLegalNow,
            PermissionDenied,
            NotFoundError,
        ) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        try:
            gm_status = await gm_command_service.get_gm_status(
                pool, league_id=league.id, team_id=team.id
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/gm status")
            return

        panel = build_gm_status_panel(
            team=gm_status.team,
            strategy=gm_status.strategy,
            parked_offers_addressed_to_team=gm_status.parked_offers_addressed_to_team,
        )
        await interaction.response.send_message(embed=panel_to_embed(panel))

    @app_commands.command(name="player", description="Show one player's role and directives.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The player's internal id (see /team) -- start typing a name to search",
    )
    @app_commands.autocomplete(player_id=_player_autocomplete)
    async def player(self, interaction: discord.Interaction, code: str, player_id: int) -> None:
        pool = await get_pool()
        try:
            # Same commissioner-or-manager union the other `/gm` subcommands
            # take (see `/gm status`'s identical comment, B1/B2) -- without
            # it, any team code plus any player id in the league returned
            # that player's directive verbatim (B2).
            league, team = await resolve_league_and_team(
                interaction, code=code, command="gm_player", action="gm_player"
            )
        except (
            NoLeagueForGuild,
            CommandNotLegalNow,
            PermissionDenied,
            NotFoundError,
        ) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        try:
            view = await gm_command_service.get_gm_player_view(
                pool, league_id=league.id, team_id=team.id, player_id=player_id
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/gm player")
            return

        panel = build_gm_player_panel(player=view.player, role=view.role, directive=view.directive)
        await interaction.response.send_message(embed=panel_to_embed(panel))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GmCommands(bot))
