"""The read-only command surface (§11 step 9, §7.1's table) --
`/standings`, `/team`, `/box`, `/wire`, `/leaders`, `/phase`. Every
command here is backed by a real read Stage B already built and proved
against Postgres (`view_service.py`); this cog is the first place any of
it becomes reachable from Discord.

**Not built here, disclosed rather than silently missing:** `/player` and
`/schedule` (§7.1's own table lists both as "real data can serve today")
have no `dba2/presentation/*.py` panel builder yet -- Stage B's own
shipped scope (§37.7, `ff544f6`) built exactly four panels (standings,
team, boxscore, wire) plus the `/leaders` repository read, not those
two. Adding either is a small, real, additive follow-up (one presentation
module + one `view_service` function + one command, the identical shape
`/leaders`'s own Stage C completion above followed) -- not attempted in
this session because it is new scope §37.7's own execution record did
not name as already-built, and this cog's job is to wire up what Stage B
actually shipped, not to silently grow Stage B's scope while doing it.

**`/box` is the one surface gated on reveal** (phase-5-stage-e-advance.md P5-99.n): a game
whose `game_broadcasts` row exists and is unrevealed is refused ephemerally, through
`game_feed.box_score_is_public` -- the single definition of "revealed", beside
`_may_reveal`, never restated here. A game with no row (in no block) answers as always.
`/standings`, `/leaders`, `/team`'s record and `/wire` are deliberately NOT gated: a block
stays open until the next press, so gating aggregates would lag the standings one press
behind permanently (the user's scope call, §8 item 1).

Every command: resolves the league for this guild (`bot.context.resolve_
league`), checks `COMMAND_GATES` (`bot.gates.ensure_command_legal` --
Decision P5-17), then translates a `view_service` read into a `Panel`
into a `discord.Embed` (Decision P5-4) and replies. No command here
computes a league fact itself (Decision P5-5) -- every number shown
already came from `view_service`.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import broadcast_repo, game_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.services import view_service

from .. import game_feed
from ..context import NoLeagueForGuild, current_cycle_index, resolve_league
from ..embeds import panel_to_embed
from ..gates import CommandNotLegalNow, ensure_command_legal
from ..leaders_pager import leaders_view_or_missing
from ..wire_pager import WIRE_BEAT_CHOICES, beat_to_persona, wire_view_or_missing

log = get_logger(__name__)

_BOX_NOT_REVEALED = (
    "Game {game_id}'s result hasn't been revealed yet, so its box score is hidden. It is "
    "posted in #games when the game's block closes (the next `/advance` that plays a new "
    "block closes it) and nobody is still broadcasting it. `/watch {game_id}` plays it out "
    "first."
)

_LEADER_STAT_CHOICES = [
    app_commands.Choice(name=stat.value.title(), value=stat.value) for stat in LeaderStat
]


class ReadCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _reply_error(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="standings", description="Show the current league standings.")
    @app_commands.describe(group="Filter to one conference or division")
    async def standings(self, interaction: discord.Interaction, group: str | None = None) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("standings", league.current_phase)
            panel = await view_service.get_standings_panel(
                pool,
                league_id=league.id,
                season=league.current_season,
                cycle_index=current_cycle_index(league),
                group=group,
            )
            await interaction.response.send_message(embed=panel_to_embed(panel))
        except (NoLeagueForGuild, CommandNotLegalNow, ValueError) as exc:
            await self._reply_error(interaction, str(exc))

    @app_commands.command(name="team", description="Show a team's roster, cap sheet, and record.")
    @app_commands.describe(code="The team's 2-3 letter code, e.g. LAL")
    async def team(self, interaction: discord.Interaction, code: str) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("team", league.current_phase)
            team_row = await team_repo.get_team_by_code(pool, league.id, code.upper())
            if team_row is None:
                await self._reply_error(interaction, f"No team with code {code!r} in this league.")
                return
            panel = await view_service.get_team_panel(
                pool,
                league_id=league.id,
                team_id=team_row.id,
                season=league.current_season,
                cycle_index=current_cycle_index(league),
            )
            await interaction.response.send_message(embed=panel_to_embed(panel))
        except (NoLeagueForGuild, CommandNotLegalNow, NotFoundError) as exc:
            await self._reply_error(interaction, str(exc))

    @app_commands.command(name="box", description="Show a game's box score.")
    @app_commands.describe(game_id="The game's internal id")
    async def box(self, interaction: discord.Interaction, game_id: int) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("box", league.current_phase)
            game = await game_repo.get_game(pool, game_id)
            if game is None or game.league_id != league.id:
                await self._reply_error(interaction, f"No game {game_id} in this league.")
                return
            broadcast = await broadcast_repo.get_broadcast(
                pool, league_id=league.id, game_id=game_id
            )
            if not game_feed.box_score_is_public(broadcast):
                await self._reply_error(interaction, _BOX_NOT_REVEALED.format(game_id=game_id))
                return
            panel = await view_service.get_boxscore_panel(pool, game_id=game_id)
            await interaction.response.send_message(embed=panel_to_embed(panel))
        except (NoLeagueForGuild, CommandNotLegalNow, NotFoundError) as exc:
            await self._reply_error(interaction, str(exc))

    @app_commands.command(
        name="wire", description="Browse the league's news stories, newest first."
    )
    @app_commands.describe(beat="Which column to show (default: all five)")
    @app_commands.choices(beat=WIRE_BEAT_CHOICES)
    async def wire(
        self, interaction: discord.Interaction, beat: app_commands.Choice[str] | None = None
    ) -> None:
        """docs/design/media-v2-newsroom.md MV-7, build step 7: replaces the
        old `n`-recent-stories/one-followup-per-story shape (already the
        wrong shape at 20 followups per invocation, worse at a season's own
        ~150-450 stories) with a real pager, one story per page --
        `WireNavButton`'s Prev/Next mirror `/leaders`' own toggle-and-paging
        row (`leaders_pager.py`, LD2-3/LD2-4), reused rather than
        reinvented. `beat` defaults to `all` (every persona, unfiltered) --
        see `wire_pager.py`'s own module docstring for why `no-filter` is
        NOT that default (it is `hot_take`'s own column name)."""
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("wire", league.current_phase)
            beat_token = beat.value if beat is not None else "all"
            persona = beat_to_persona(beat_token)
            view = await view_service.get_wire_page(
                pool, league_id=league.id, season=league.current_season, beat=persona, page=1
            )
            if view.is_empty:
                await self._reply_error(interaction, "No wire stories for this beat yet.")
                return
            nav_view = wire_view_or_missing(
                league_id=league.id, season=league.current_season, beat=beat_token,
                page=view.page, has_more=view.has_more,
            )
            await interaction.response.send_message(embed=panel_to_embed(view.panel), view=nav_view)
        except (NoLeagueForGuild, CommandNotLegalNow) as exc:
            await self._reply_error(interaction, str(exc))

    @app_commands.command(name="leaders", description="Show the league's statistical leaders.")
    @app_commands.describe(stat="Which stat to rank by")
    @app_commands.choices(stat=_LEADER_STAT_CHOICES)
    async def leaders(
        self, interaction: discord.Interaction, stat: app_commands.Choice[str]
    ) -> None:
        """v2 (docs/design/leaders-v2-per-game-and-paging.md): defaults to `PER_GAME`,
        page 1, and sends the toggle-and-paging row alongside the embed -- `LeadersView`
        (LD2-6) tells this apart from the no-games branch (no view at all, LD2-5's own
        case 1: "both modes are empty; there is nothing to toggle to") without ever
        parsing the panel's own description. Gate, `resolve_league` and the error path
        are unchanged from v1."""
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("leaders", league.current_phase)
            stat_enum = LeaderStat(stat.value)
            view = await view_service.get_leaders_panel(
                pool,
                league_id=league.id,
                season=league.current_season,
                game_type=GameType.REGULAR,
                stat=stat_enum,
                mode=LeaderMode.PER_GAME,
                page=1,
            )
            nav_view = leaders_view_or_missing(
                league_id=league.id,
                season=league.current_season,
                game_type=GameType.REGULAR,
                stat=stat_enum,
                mode=LeaderMode.PER_GAME,
                page=view.page,
                has_more=view.has_more,
                no_games=view.no_games,
            )
            await interaction.response.send_message(embed=panel_to_embed(view.panel), view=nav_view)
        except (NoLeagueForGuild, CommandNotLegalNow, NotFoundError) as exc:
            await self._reply_error(interaction, str(exc))

    @app_commands.command(name="phase", description="Show the league's current phase.")
    async def phase(self, interaction: discord.Interaction) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("phase", league.current_phase)
            panel = await view_service.get_phase_panel(pool, league_id=league.id)
            await interaction.response.send_message(embed=panel_to_embed(panel))
        except (NoLeagueForGuild, CommandNotLegalNow, NotFoundError) as exc:
            await self._reply_error(interaction, str(exc))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReadCommands(bot))
