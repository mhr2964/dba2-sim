"""`discord.py` client bootstrap -- §3 module layout's `bot/client.py`,
token from `Config` (§1 finding 5: "`Config` already carries `discord_
token`, lazily").

`on_ready` runs the full channel reconcile (P5-13's first named call
site: "safe to call on every startup") for every league whose `discord_
guild_id` matches a guild this process is connected to -- the per-team
GM-channel branch is included, driven by whichever team currently has a
`manager_user_id` set, so a restart alone (with no new assignment)
brings Discord's channel state back in line with Postgres's, per §13.4's
own named risk ("the reconcile gets written as a startup-only function
and then not called at the moments that matter").

**§42.1's Blocking Issue 3 (an undisclosed regression, critic re-
verification): `_reconcile_guild` used to short-circuit on `except
discord.HTTPException: return` around the league-channel reconcile --
a deliberate bail-out if the bot lacks Manage Channels on a fresh guild
(the exact scenario commit `90443f4`'s own message cites as
motivation), skipping every per-team GM-channel reconcile rather than
attempting 30 more calls that would all fail the same way and burn
Discord's rate-limit budget for nothing. Once `reconcile_league_
channels` itself started swallowing every per-purpose failure
internally (returning a partial/empty dict instead of raising -- a
change made in `dba2/bot/channels.py`, never in this file, and not
mentioned in any commit body that touched it), that `except` clause
here went dead: the `return` stopped ever firing, and the bot silently
started proceeding to 30 individually-failing GM-channel reconciles on
exactly the guild state the original short-circuit protected against.
Fixed by checking `reconcile_league_channels`'s own RESULT instead of
catching an exception it no longer raises -- a short result (fewer
channels than `ChannelPurpose` has members) is now the signal, and the
bail-out fires on it exactly as the original `except` clause intended.**

**Nit N1 (critic round 4): the RESULT-based bail-out above originally
fired on ANY shortfall (`len(league_channels) < len(ChannelPurpose)`),
treating a 1-of-2 partial success identically to total failure -- but a
partial success is evidence the bot CAN create/manage channels in this
guild, which contradicts the "broken permission/connection" story the
bail-out's own comment gave as its rationale. Narrowed to bail only on
total failure (`not league_channels`); a partial result now proceeds to
the per-team loop, which is safe because each iteration already catches
its own `reconcile_gm_channel` failure independently (B1, same round)
instead of depending on this outer bail-out for protection. Both the
total-failure and partial-success paths are pinned by their own test in
`tests/integration/test_client_reconcile_guild.py`.** `reconcile_gm_
channel`'s own per-team guard below was widened from `except discord.
HTTPException` to `except Exception` in the same round (B1) -- see
`dba2/bot/cogs/commissioner.py`'s module docstring for the live repro
that motivated it; the identical DB-write-inside-the-guard gap existed
here too.

**Critic round 6's structural backstop: `DBABot.__init__` now registers
`self._on_app_command_error` as the tree's own `on_error` handler (via
`tree.error(...)`, discord.py's real mechanism for app-command-wide
error handling -- there is no bare `on_error` event for slash commands
the way there is for prefix commands). This does not replace any of the
per-command guards named above or in `commissioner.py`'s own docstring
(a specific, honest failure message is still better UX than a generic
one) -- it is the last-resort net for whatever a FUTURE command's own
guard fails to anticipate, after this exact bug class needed fixing 6
times across 6 functions. See `DBABot.__init__`'s own comment for the
`discord.py` internals that make `CommandTree.error` the correct
mechanism, and `_on_app_command_error`'s own docstring for what it
does.**
"""
from __future__ import annotations

import functools
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.config import get_config
from dba2.core.logging import get_logger, setup_logging
from dba2.data.db import Executor, get_pool
from dba2.data.repositories import league_repo, media_repo, team_repo
from dba2.domain.channel import ChannelPurpose
from dba2.domain.league import League

from . import channels as bot_channels
from . import game_feed, offer_delivery, surfaces
from .block_registry import BlockRegistry
from .broadcast import DEFAULT_BROADCAST_SETTINGS, BroadcastScheduler
from .leaders_pager import LeadersNavButton
from .run_registry import SHUTDOWN_GRACE_SECONDS, RunRegistry
from .watch_pager import PagerRegistry, WatchCycleButton, WatchFullTextButton, WatchPageButton
from .wire_pager import WireNavButton

log = get_logger(__name__)

_EXTENSIONS = (
    "dba2.bot.cogs.read",
    "dba2.bot.cogs.commissioner",
    # Stage D' (docs/design/phase-5-discord-presentation.md §21 JC-4, §29)
    # -- the human roster-TRANSACTION commands (`/waive`, `/release`,
    # `/sign-free-agent`). The other four JC-4 commands moved to `/gm`
    # (GM-6, docs/design/gm-command-surface.md).
    "dba2.bot.cogs.roster",
    # GM-6 -- the `/gm` command group (`/gm strategy`, `/gm role`, `/gm
    # directive`, `/gm timeline`, and later `/gm auto`/`/gm status`/`/gm
    # player`). Without this entry `cogs/gm.py` never loads, `/gm` never
    # enters the tree, and GM-7's strengthened `walk_commands` assertions
    # would fail on an empty group -- the easiest thing to forget in this
    # step and the one that makes `/gm` not exist.
    "dba2.bot.cogs.gm",
    # The trade-offer review surface (docs/design/phase-5-trade-offer-
    # review-surface.md §4, §8) -- five commands over `offer_service`.
    "dba2.bot.cogs.offers",
    # Stage D's broadcast (docs/design/phase-5-stage-d-broadcast.md §7) --
    # `/watch`, a game's own thread.
    "dba2.bot.cogs.broadcast",
    # Stage D'-25 (docs/design/phase-5-stage-d-prime-draft.md) -- `/draft`,
    # one human pick and the AI draft resumed behind it. Registered here
    # although nothing in the bot can start a draft until Stage E's
    # `/advance` exists (§7 L8): the command refuses outside DRAFT with its
    # own sentence, which is a better thing to ship than a cog that is
    # loaded by a later stage and never exercised by this one's tests.
    "dba2.bot.cogs.draft",
    # Stage E (docs/design/phase-5-stage-e-advance.md P5-99.a) -- `/advance`, the
    # commissioner's command that moves a league, and its five companions (`/advance-to`,
    # `/advance-force`, `/advance-unlock`, `/advance-postpone-game`, `/advance-restore-game`).
    "dba2.bot.cogs.advance",
)


def build_intents() -> discord.Intents:
    """`default()` plus `members` -- `/assign-manager`'s `discord.Member`
    parameter and the GM-channel permission overwrite both need real
    member ids resolvable from the guild, not just message-author
    context. Requires the "Server Members Intent" privileged flag be
    enabled for this bot in the Discord developer portal -- a real,
    disclosed prerequisite for `/assign-manager` to work against a real
    guild, not assumed silently."""
    intents = discord.Intents.default()
    intents.members = True
    return intents


class DBABot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=build_intents())
        # Critic round 6's structural backstop: this bug class (a narrow
        # exception guard inside a `defer()`-ed interaction leaving it
        # hanging forever on an unanticipated failure) has now needed
        # fixing 6 times across 6 functions in this project (`/setup`,
        # `reconcile_gm_channel`'s two callers via `_reconcile_gm_channel_
        # after_commit`, `_reconcile_guild`'s `tree.sync` call, and now
        # `/assign-manager`/`/remove-manager`'s own DB-write guard --
        # `dba2/bot/cogs/commissioner.py`'s module docstring and this
        # module's own docstring name each round). Per-command guards are
        # still the FIRST line of defense (they give an honest, specific
        # failure message, which is better UX than a generic fallback) --
        # this is the LAST-resort net for whatever a future command's own
        # guard fails to anticipate, registered once here rather than
        # copy-pasted into every cog.
        #
        # `discord.py` 2.7.1 wraps ANY exception a command callback raises
        # (verified by reading `.venv/Lib/site-packages/discord/app_
        # commands/commands.py::Command._do_call`, `except Exception as e:
        # raise CommandInvokeError(self, e) from e`) into an `AppCommand
        # Error` subclass and routes it to `CommandTree._call`'s own
        # `except AppCommandError as e:` handler, which calls `self.on_
        # error(interaction, e)` -- there is no bare `on_error` EVENT for
        # slash commands the way there is for prefix commands; `tree.on_
        # error`/`tree.error(...)` is the real, documented mechanism
        # (confirmed by reading `tree.py`'s own `on_error`/`error`
        # docstrings). Registered via the `tree.error` decorator method
        # rather than a `CommandTree` subclass -- no other code in this
        # project needs to construct a custom tree, so a subclass would
        # be an extra class for one registration call.
        self.tree.error(self._on_app_command_error)
        # Stage D (docs/design/phase-5-stage-d-broadcast.md §5-§6): the block of
        # games most recently opened per league, and the bounded scheduler that
        # runs `/watch` broadcasts. Per-bot instances, never module singletons,
        # so two bots (or two tests) never share state. `on_ended` is the
        # `#games` reveal rule; it reads `self.blocks` at call time, so the
        # scheduler can be built before the registry is used.
        self.blocks = BlockRegistry()
        # The `/advance-run` that is going, per league (docs/design/advance-run.md AR-4):
        # per-bot, like `blocks`, so two bots (or two tests) never share a run.
        self.runs = RunRegistry()
        # The `/watch` pager's per-game state (docs/design/watch-v3-spacing-and-pager.md
        # W-11): per-bot, like `blocks`, and in memory only (P5-42).
        self.pagers = PagerRegistry()
        self.broadcasts = BroadcastScheduler(
            DEFAULT_BROADCAST_SETTINGS, on_ended=functools.partial(game_feed.on_ended, self)
        )
        # The leagues whose startup wire sweep has already run in this process (P5-99.s.4,
        # B-5's first guard). Per-bot, like `blocks`, so a new bot (a new process, or a test)
        # starts empty and nothing leaks between them; keyed by league id, so two guilds are
        # independent.
        self.wire_swept_leagues: set[int] = set()

    async def close(self) -> None:
        """Ask every `/advance-run` to stop and give it a bounded grace to finish the press it
        is in (docs/design/advance-run.md AR-4), then cancel every running broadcast before
        the connection goes away, so each one's finalizer can record `interrupted` while the
        pool is still open (design P5-97.q item 3). Whatever a cancelled task could not
        record is the next startup's sweep to converge. Never allowed to stop the bot from
        closing.

        The runs are AWAITED, not just flagged: setting the flag and closing at once would
        tear down the press the flag exists to let finish. A press that outlives the grace is
        cancelled mid-press and leaves its claim to `/advance-unlock` -- now the rare case."""
        try:
            if self.runs.cancel_all() and not await self.runs.wait_for_all(SHUTDOWN_GRACE_SECONDS):
                log.warning(
                    "an /advance-run outlived the %.0fs shutdown grace; closing anyway (its "
                    "claim, if it holds one, is /advance-unlock's to clear)",
                    SHUTDOWN_GRACE_SECONDS,
                )
        except Exception:
            log.exception("waiting for /advance-run to stop failed")
        try:
            await self.broadcasts.shutdown()
        except Exception:
            log.exception("broadcast scheduler shutdown failed")
        await super().close()

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """The backstop itself: always logs (so the failure is still
        diagnosable) and always sends exactly one honest-but-generic
        followup, using `interaction.response.is_done()` to pick `response.
        send_message` vs. `followup.send` -- the same choice `Commissioner
        Commands._reply_error` already makes for its own specific errors,
        because whether `defer()`/an earlier response already ran is not
        knowable in advance from here. This only runs for whatever a
        command's own handling did NOT already catch (every command in
        this project that responds to the user and returns normally, or
        that catches its own exceptions and replies, never reaches this
        handler at all -- `CommandTree._call` only routes here on an
        exception that escaped the command callback itself)."""
        command_name = getattr(interaction.command, "qualified_name", None) or "<unknown command>"
        log.exception("unhandled app command error in %r", command_name, exc_info=error)
        message = (
            "Something went wrong running that command (error logged) -- try again, and "
            "contact the bot's maintainer if it keeps failing."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            # The interaction itself may already be invalid/expired (e.g.
            # past Discord's 15-minute followup window) -- logged above
            # already, and there is no further fallback that could reach
            # the user at that point.
            log.exception("failed to notify the user about an unhandled app command error")

    async def setup_hook(self) -> None:
        for extension in _EXTENSIONS:
            await self.load_extension(extension)
        # The trade-offer buttons (docs/design/phase-5-trade-offer-review-
        # surface.md §1, corrected round 2): register the `DynamicItem`
        # CLASS once. NOT `add_view` -- `ViewStore` looks a plain view item
        # up by exact `custom_id`, and an offer's id is inside the
        # `custom_id`, so that would need one registration per offer.
        # `dispatch_dynamic_items` matches by `pattern.fullmatch` instead
        # (`discord/ui/view.py:1043-1052`). Not per guild, not per row.
        self.add_dynamic_items(offer_delivery.OfferActionButton)
        # The `/watch` live component's and pager's buttons (watch-v4, watch-v3 W-13), the
        # same way: the classes are registered once and each click builds a fresh instance
        # from its `custom_id`.
        self.add_dynamic_items(WatchPageButton, WatchFullTextButton, WatchCycleButton)
        # `/leaders` v2's toggle-and-paging row (docs/design/leaders-v2-per-game-and-
        # paging.md LD2-3), the same way: registered once, state lives in the `custom_id`.
        self.add_dynamic_items(LeadersNavButton)
        # `/wire`'s beat-filtered pager (docs/design/media-v2-newsroom.md MV-7,
        # build step 7), the same way: registered once, state lives in the `custom_id`.
        self.add_dynamic_items(WireNavButton)
        # Guild-scoped sync only, per Discord's own documented advice
        # (Stage A's step 2 finding, docs/design/phase-5-discord-
        # presentation.md §11 execution record): "guild commands update
        # instantly; global commands do not." Syncing per-guild in
        # on_ready keeps a freshly `/setup` league's slash commands
        # visible immediately rather than waiting on global propagation.
        #
        # Every cog above adds its commands to the tree GLOBALLY (no
        # `guild=` kwarg anywhere in `cogs/read.py`/`cogs/commissioner.
        # py`) -- `CommandTree.sync(guild=g)` only ever reads `self.
        # _guild_commands[g.id]`, which nothing populates on its own, so
        # a bare `tree.sync(guild=guild)` sends an EMPTY command list and
        # wipes out any prior registration (reproduced offline: `tree.
        # get_commands()` shows all 9 after `setup_hook`, but `tree.
        # _guild_commands.get(guild.id)` is `None` before the copy
        # below). `copy_global_to` is `discord.py`'s own documented
        # mechanism for this exact shape -- "copies all global commands
        # to the specified guild" -- called immediately before the sync
        # in `_reconcile_guild` so the guild-scoped sync actually carries
        # the real command list.

    async def on_ready(self) -> None:
        log.info("logged in as %s (%d guild(s))", self.user, len(self.guilds))
        pool = await get_pool()
        for guild in self.guilds:
            try:
                await self._reconcile_guild(guild, pool)
            except Exception:
                # Same class of bug fixed 4 times over in `_reconcile_
                # guild`'s own body (widened exception guards around
                # Discord I/O) -- this is the sibling gap: nothing wrapped
                # the CALL to `_reconcile_guild` itself, so anything that
                # still escaped its internals (e.g. a future change that
                # adds new I/O without its own guard) would abort this
                # loop for every OTHER guild too, not just the one that
                # failed. `on_ready` re-fires on Discord reconnect, so a
                # transient failure here self-heals on the next READY
                # event -- but one guild's outage should never take down
                # every other guild's reconcile in the meantime.
                log.exception(
                    "guild reconcile failed for guild %s -- continuing to the next guild",
                    guild.id,
                )

    async def _reconcile_guild(self, guild: discord.Guild, pool: Executor) -> None:
        league = await league_repo.get_league_by_guild_id(pool, guild.id)
        if league is None:
            log.info("guild %s has no league set up yet -- skipping reconcile", guild.id)
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            # Critic round 5: `tree.sync` is real Discord network I/O --
            # `discord.py` can raise a bare `ConnectionResetError`/
            # `OSError`/`asyncio.TimeoutError` after exhausting its own
            # retry budget (none of which are `discord.HTTPException`),
            # and `CommandTree.sync`/`copy_global_to` themselves can raise
            # `MissingApplicationID`/`CommandLimitReached`, neither an
            # `HTTPException` either. Widened to match every other Discord
            # I/O guard this project has settled on (see this module's own
            # docstring, B1/B2 in `commissioner.py`) after the narrower
            # `except discord.HTTPException` here let exactly that shape
            # of exception escape and abort the per-guild loop in `on_
            # ready` for every remaining guild -- reproduced live before
            # this fix and pinned by `tests/integration/test_client_
            # reconcile_guild.py::test_on_ready_survives_a_guilds_tree_
            # sync_failure_and_reconciles_every_guild` and that same
            # module's `test_on_ready_one_guilds_reconcile_failure_does_
            # not_abort_the_others` (round 6 nit: the comment used to cite
            # `tests/unit/bot/test_client.py::test_on_ready_one_guilds_
            # sync_failure_does_not_abort_the_others`, a test that does
            # not exist anywhere in this repo -- confirmed via grep).
            log.exception("slash command sync failed for guild %s", guild.id)

        self._report_held_claim(league)

        league_channels = await bot_channels.reconcile_league_channels(
            guild, pool, league_id=league.id
        )
        if not league_channels:
            # `reconcile_league_channels` never raises any more (it
            # catches every per-purpose failure internally -- see that
            # function's own docstring) -- an EMPTY result is now the
            # ONLY signal every purpose failed the same way (e.g. the bot
            # lacks Manage Channels on this guild entirely). Bailing here,
            # before the per-team loop, restores the original safety
            # property this function used to get from its own `except
            # discord.HTTPException: return`: total failure does not turn
            # into 30 more GM-channel reconciles that would all fail
            # identically, burning Discord's rate-limit budget for
            # nothing.
            #
            # A NON-empty but short result (nit N1, critic round 4: the
            # threshold used to be `len(league_channels) < len(
            # ChannelPurpose)`, bailing on ANY shortfall) is deliberately
            # NOT treated the same as total failure -- at least one
            # purpose succeeding is evidence the bot CAN create/manage
            # channels in this guild, which contradicts the "broken
            # permission/connection" story the bail-out exists to guard
            # against. The per-team loop below is safe to run on a
            # partial result because each iteration already catches its
            # own `reconcile_gm_channel` failure independently (B1, critic
            # round 4) rather than needing this outer bail-out to protect
            # it.
            log.warning(
                "league channel reconcile failed entirely for guild %s (0/%d) -- "
                "skipping per-team GM channel reconcile this startup",
                guild.id,
                len(ChannelPurpose),
            )
            # The drain does not depend on the channel creates that just
            # failed (see `_drain_offers_at_startup`), so it still runs.
            await self._drain_offers_at_startup(guild, pool, league.id)
            # Likewise the `#games` sweep: it reads existing `league_channels`
            # rows and never creates a channel.
            await self._sweep_games_at_startup(guild, pool, league.id)
            # And the wire sweep, for the same reason: it reads existing rows.
            await self._sweep_wire_at_startup(guild, pool, league.id)
            return
        if len(league_channels) < len(ChannelPurpose):
            log.warning(
                "league channel reconcile incomplete for guild %s (%d/%d) -- "
                "proceeding to per-team GM channel reconcile anyway",
                guild.id,
                len(league_channels),
                len(ChannelPurpose),
            )

        teams = await team_repo.list_teams_for_league(pool, league.id)
        for team in teams:
            if team.manager_user_id is None:
                continue
            try:
                await bot_channels.reconcile_gm_channel(
                    guild,
                    pool,
                    league_id=league.id,
                    team_id=team.id,
                    team_code=team.code,
                    manager_user_id=team.manager_user_id,
                    commissioner_user_id=league.commissioner_user_id,
                )
            except Exception:
                log.exception("GM channel reconcile failed for team %s", team.id)

        await self._drain_offers_at_startup(guild, pool, league.id)
        await self._sweep_games_at_startup(guild, pool, league.id)
        await self._sweep_wire_at_startup(guild, pool, league.id)

    def _report_held_claim(self, league: League) -> None:
        """A held advance claim at startup means a press was running when the last process
        died, or is running right now in another one. Startup REPORTS it and clears nothing
        (R4, P5-99.c): a claim is not proof its presser is dead, and clearing a live one runs
        two AI passes at once. `/advance-unlock` is the recovery, and it refuses a claim that
        is too young to be dead. Not a decision, so no lock is taken."""
        if league.advance_claimed_at is None:
            return
        age_minutes = max(
            0, int((datetime.now(UTC) - league.advance_claimed_at).total_seconds() // 60)
        )
        log.warning(
            "league %s has an advance claim held by user %s for %d minute(s) -- startup does "
            "not clear it; if that press died, /advance-unlock clears it once it is old enough",
            league.id,
            league.advance_claimed_by,
            age_minutes,
        )

    async def _drain_offers_at_startup(self, guild: discord.Guild, pool, league_id: int) -> None:
        """Deliver offers written while the bot was down (an AI-initiated
        offer from a phase hook, or one whose post failed): the queue is
        `status='proposed' AND message_id IS NULL` in Postgres (§6.5).

        Runs on EVERY `_reconcile_guild` exit past league resolution,
        including the total-channel-failure bail-out: the drain depends on
        the existing `gm_channels` rows, not on this startup's channel
        creates, so a guild where every create fails can still have
        postable GM channels. Never allowed to fail the reconcile it rides
        on."""
        try:
            report = await offer_delivery.drain_undelivered_offers(
                self, pool, league_id=league_id
            )
            if report.outcomes:
                log.info(
                    "offer delivery drain for guild %s: %d delivered of %d queued",
                    guild.id,
                    report.delivered,
                    len(report.outcomes),
                )
        except Exception:
            log.exception("offer delivery drain failed for guild %s", guild.id)

    async def _sweep_games_at_startup(self, guild: discord.Guild, pool, league_id: int) -> None:
        """Converge `#games` after a restart: reveal results whose reveal never
        finished, and settle broadcasts that were mid-flight when the process
        died (`game_feed.sweep_league`, design P5-97.q item 6). The last step of
        `_reconcile_guild`, on both of its exits, like the offer drain -- and
        like it never allowed to fail the reconcile it rides on."""
        try:
            report = await game_feed.sweep_league(self, pool, league_id=league_id)
            if report.entries:
                log.info(
                    "games sweep for guild %s: %d revealed, %d failed of %d unrevealed",
                    guild.id,
                    report.revealed,
                    report.failed,
                    len(report.entries),
                )
        except Exception:
            log.exception("games sweep failed for guild %s", guild.id)

    async def _sweep_wire_at_startup(self, guild: discord.Guild, pool, league_id: int) -> None:
        """Post the wire stories a crash left unposted (P5-99.s.4): a press that died between
        a cycle's selection and its publication would otherwise lose the cycle's wire posts
        until somebody presses `/advance` again. It is the third step of `_reconcile_guild`,
        on both of its exits, and like its two siblings it never fails the reconcile.

        **Two guards, because `on_ready` re-fires on every reconnect** (B-5): an unguarded
        sweep would start mid-press and post the stories the press is posting, interleaved.

        1. **Once per process per league.** The league is recorded BEFORE anything is awaited,
           so two overlapping reconciles cannot both pass, and it is recorded even when this
           attempt skips or fails: the alternative -- retrying on a later reconnect -- is the
           very race this guard exists to prevent, and the next press publishes whatever is
           still unposted anyway (`post_new_wire_stories` reads "unposted", not "this cycle").
        2. **Never while the claim is held.** A press publishes under its own claim; the claim
           is read fresh here, because the league row `_reconcile_guild` loaded is seconds old
           after the channel reconciles.

        Send-then-mark is not atomic, so "at most one duplicate story" holds per poster. These
        two guards give one sweep per process per league that skips a claim it can see; they are
        not a mutex. The claim is read once and never held, so a press that starts right after
        that read can post the same stories alongside the sweep -- a narrow startup window in
        which a story may post twice (spec R12)."""
        if league_id in self.wire_swept_leagues:
            log.info(
                "wire sweep for guild %s already ran in this process -- not repeating", guild.id
            )
            return
        self.wire_swept_leagues.add(league_id)
        try:
            league = await league_repo.get_league(pool, league_id)
            if league is None:
                return
            if league.advance_claimed_at is not None:
                log.warning(
                    "wire sweep for guild %s skipped: league %s's advance claim is held, so a "
                    "press may be publishing; its next press posts anything still unposted",
                    guild.id,
                    league_id,
                )
                return
            unposted = await media_repo.list_unposted_media_stories(
                pool, league_id=league_id, season=league.current_season
            )
            if not unposted:
                return
            posted = await surfaces.post_new_wire_stories(
                guild, pool, league_id=league_id, season=league.current_season, cycle_index=None
            )
            log.info(
                "wire sweep for guild %s: %d of %d unposted stories posted",
                guild.id,
                posted,
                len(unposted),
            )
        except Exception:
            log.exception("wire sweep failed for guild %s", guild.id)


def build_bot() -> DBABot:
    return DBABot()


async def run() -> None:
    """Entry point (`python -m dba2.bot.client`) -- not exercised by any
    test in this repository, because it opens a real gateway connection.
    Blocked in this environment: no `DISCORD_TOKEN` is set (verified --
    `.env` carries only `DATABASE_URL`), so this function has never been
    run against a real guild here; see the Stage C session report's own
    disclosure of that gap."""
    setup_logging()
    config = get_config()
    if config.discord_token is None:
        raise RuntimeError(
            "DISCORD_TOKEN is not set -- copy .env.example to .env and add a real bot token "
            "before running the bot (see dba2/core/config.py)."
        )
    bot = build_bot()
    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
