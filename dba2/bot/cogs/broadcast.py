"""`/watch <game_id>` -- a game's own broadcast thread
(docs/design/phase-5-stage-d-broadcast.md §7, Decisions P5-97.l/.p/.q/.r/.s/.w).

**The constraint that shapes every branch: `/watch` may never leak a score the
league has not posted.** `#games` is public and a result appears there only
once the game's block is closed and nobody is still broadcasting it
(P5-97.m). So the only reply that carries a score is the revealed-game box
score, and what makes a game "revealed" is `game_broadcasts.reveal_posted_at`
-- never the game's status, never the mere absence of an in-memory result.
Every other reply is a plain sentence, and every reply is ephemeral.

**The state machine is keyed on the persisted `game_broadcasts` row, never on
memory** (P5-97.w), so a restart cannot produce a second thread for one game:

* no row -> the game is in no block; nothing to watch, nothing to create;
* `pending`, no thread -> start one (below);
* any row with a thread (`queued`/`live`/`finished`/`interrupted`) -> link it,
  start nothing;
* a `pending` row whose reveal was claimed or posted, or whose block is gone
  from memory (a restart, an evicted block) or holds no result (cap-degraded),
  is NOT started: a broadcast of a game whose result is already public would
  spoil nothing but mean nothing either, and one with no result cannot be
  planned. It gets the revealed box score or the "not watchable" sentence.

**RULING (design gap, commit body): admit first, then create the thread.** The
design creates the thread before scheduler admission so a queued viewer still
gets a link. But `admit` reserves a place, and the alternatives both strand a
row: admitting after the row is `queued` can be refused (full, per-user cap) and
`queued` -> anything-but-`live` is terminal (`BROADCAST_TRANSITIONS`), so a
refused `/watch` would leave a `queued` row with a thread and no task that a
later `/watch` could only link, never start. Admitting FIRST means a refusal
creates nothing (the row stays `pending`, a later `/watch` retries), and an
admitted `/watch` always ends in a thread and a spawned task, so the design's
purpose -- a link even when the broadcast is queued behind others -- still
holds. The cost is that the reservation is held across the awaits that create
the thread, so every failure after `admit` MUST give it back
(`BroadcastScheduler.release_unspawned`) and `spawn` follows `record_thread`
with no `await` between the two. It also gives the placeholder its "N games
ahead" number, which is known at admission and not before.

**Failure ladder** (P5-97.q item 4): `/watch` never awaits the broadcast task.
Everything before `spawn` that is not a named Discord permission failure goes
through `reply_command_failure(..., log_label="watch")`. A `discord.Forbidden`
is answered with the missing permission BY NAME and says which half happened --
the "tell the truth about which half succeeded" contract `channels.py` keeps.
After a failed attempt the reservation is returned, the empty thread (if one
was made) is deleted best-effort, and `game_feed.on_ended` puts the `#games`
line back to "not yet played" -- or reveals the game if its block closed while
this `/watch` held the reservation (`close_block` skips a game it sees as
still broadcasting, so nothing else would).

**A root message that already has a thread is ADOPTED, not re-created.** Discord
allows one thread per message. A thread that was made but never recorded --
`create_thread` timed out client-side after Discord made it, the request was
cancelled between `create_thread` and `record_thread`, `_delete_thread` failed
-- would otherwise make every later `/watch` of that game fail. So before
creating, `_existing_thread` asks the message (`PartialMessage.thread`, the
cache, then `fetch_thread`, which also finds archived threads) and uses what it
finds. An adopted thread is never deleted by this cog -- it was there before
this call, and if another `/watch` won the `record_thread` race it is THAT
broadcast's thread too. Discord's real refusal for a second thread (believed to
be HTTP 400 / code 160004) and how it treats a send into an archived thread are
NOT verified against a live gateway.

The placeholder's "N games ahead" is an approximation, computed once at
admission: the broadcasts admitted beyond the free slots. It does not know which
of them will finish first, and it is not updated as they do.

Every send passes `AllowedMentions.none()` (P5-97.s). `create_thread` has
exactly one call site in the codebase (guarded by `thread_id IS NULL` and
`record_thread`'s CAS) and a unit test pins that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.logging import get_logger
from dba2.data.db import Executor, get_pool
from dba2.data.repositories import broadcast_repo, game_repo
from dba2.domain.broadcast import BroadcastStatus, GameBroadcast
from dba2.domain.game import GameStatus
from dba2.presentation.broadcast import (
    pager_matchup,
    plan_broadcast,
    plan_narration,
    plan_period_lines,
    render_live_component,
    visible_periods,
)
from dba2.presentation.games import SlateGame, build_matchup_line, build_watching_line
from dba2.services import view_service

from .. import game_feed
from ..broadcast import (
    Admission,
    BroadcastInputs,
    BroadcastJob,
    ComponentViewRequest,
    WatchSpeed,
    settings_for_speed,
)
from ..context import NoLeagueForGuild, resolve_league
from ..embeds import panel_to_embed
from ..gates import CommandNotLegalNow, ensure_command_legal
from ..interaction_errors import reply_command_failure
from ..watch_pager import PagerEntry, PagerSource, build_component_view, game_is_open

log = get_logger(__name__)

_THREAD_AUTO_ARCHIVE_MINUTES = 1440
"""A legal `ThreadArchiveDuration` (60/1440/4320/10080). One day: the thread is
left to Discord's own auto-archive (design §7 step 9) rather than archived the
moment the broadcast ends, so a game that just finished is easy to scroll to."""

# --- the ephemeral replies (design P5-97.p's table, plus this file's own) ----------

_NOT_PLAYED = (
    "Game {game_id} hasn't been played yet. It'll be watchable after the commissioner "
    "runs `/advance`."
)
_REPLAY_GONE_REVEALED = (
    "That game's replay is gone — it only lives in memory while its block is open. "
    "Here's the final box score."
)
_NOT_WATCHABLE = (
    "That game isn't watchable — its replay is gone and its result hasn't been posted yet. "
    "It'll appear in #games when the block closes."
)
_RESULT_BEING_POSTED = (
    "That game isn't watchable — its replay is gone, and its result is still being posted "
    "to #games. It will show up there once that finishes; if it does not, it is retried "
    "when the bot next reconnects or restarts."
)
_SERVER_ONLY = "`/watch` only works inside a server, where the game's thread lives."
_NOT_IN_A_BLOCK = (
    "That game isn't watchable — it isn't part of a block of games with a replay, and a "
    "replay only lives in memory while its block is open."
)
_LINK_UNDER_WAY = "Game {game_id} is being broadcast — the thread is here: {url}"
_LINK_FINISHED = "That broadcast already finished; the thread is here: {url}"
_THREAD_LOST = (
    "Game {game_id}'s broadcast has no thread on record, so there is nothing to link. "
    "Ask the commissioner to look at it (error logged)."
)
_BROADCASTS_FULL = (
    "Too many broadcasts are running right now, so game {game_id} can't start yet. "
    "Try `/watch {game_id}` again in a few minutes — nothing was created."
)
_USER_CAP = (
    "You're already following {cap} broadcasts, the most one person can run at once. "
    "Once one finishes, `/watch {game_id}` again — nothing was created."
)
_STARTING_ELSEWHERE = (
    "Someone is starting game {game_id}'s broadcast right now — run `/watch {game_id}` "
    "again in a few seconds and you'll get its thread."
)
_STARTED = "Broadcasting game {game_id} in its own thread: {url}"
_STARTS_NOW = " It starts right away."
_STARTS_LATER = " {ahead} {games} ahead of it — it starts when a slot frees up."
_GAMES_CHANNEL_UNAVAILABLE = (
    "This league's #games channel isn't set up or can't be reached, so the broadcast "
    "thread can't be created. Nothing was started."
)
_PLACEHOLDER_NOW = "Starting the broadcast…"
_PLACEHOLDER_WAITING = "Waiting for a broadcast slot — {ahead} {games} ahead."

_STEP_GAMES, _STEP_THREAD, _STEP_POST = "games", "thread", "post"


def _games_word(count: int) -> str:
    return "game" if count == 1 else "games"


def _forbidden_reply(step: str, *, thread_deleted: bool | None) -> str:
    """The missing permission BY NAME, and which half happened (design §7)."""
    if step == _STEP_THREAD:
        return (
            "The bot can't create the thread: it is missing the `create_public_threads` "
            "permission in #games. Nothing was started."
        )
    if step == _STEP_POST:
        left = (
            "The empty thread was removed."
            if thread_deleted
            else "The empty thread is still in #games — a moderator can delete it."
        )
        return (
            "The thread was created but the bot can't post in it: it is missing "
            f"`send_messages_in_threads` (and `embed_links` for the scoreboard). No broadcast "
            f"started. {left}"
        )
    return (
        "The bot can't post in #games: it is missing the `send_messages` permission there "
        "(and `embed_links`). Nothing was started."
    )


@dataclass(frozen=True)
class _Reply:
    """The one ephemeral answer a `/watch` gives. `embed` is only ever the
    revealed game's box score."""

    text: str
    embed: discord.Embed | None = None


def _thread_url(guild_id: int, thread_id: int) -> str:
    """A thread's link from its id alone -- the same string as
    `Thread.jump_url` (`discord/threads.py:256`), built without resolving the
    thread so an archived or uncached one still links."""
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


# docs/design/watch-narration-v2.md WN-10.4: the viewer picks the speed; the names are
# the viewer's, and each label says what it costs in minutes (measured, the As-built).
_SPEED_CHOICES = [
    app_commands.Choice(
        name="fast -- a scoreboard in about 1.5 minutes", value=WatchSpeed.FAST.value
    ),
    app_commands.Choice(
        name="game -- one line a play, about 13 minutes", value=WatchSpeed.GAME.value
    ),
    app_commands.Choice(
        name="full -- every screen, drive and pass, about 44 minutes", value=WatchSpeed.FULL.value
    ),
]


class BroadcastCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="watch", description="Watch a game's broadcast in its own thread.")
    @app_commands.describe(
        game_id="The game's internal id (shown on its line in #games)",
        speed="How fast the broadcast runs (default: game)",
    )
    @app_commands.choices(speed=_SPEED_CHOICES)
    async def watch(
        self,
        interaction: discord.Interaction,
        game_id: int,
        speed: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pool = await get_pool()
            chosen = WatchSpeed(speed.value) if speed is not None else WatchSpeed.GAME
            reply = await self._watch(interaction, pool, game_id, chosen)
        except (NoLeagueForGuild, CommandNotLegalNow) as exc:
            await self._send(interaction, _Reply(str(exc)))
            return
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="watch")
            return
        # Outside the try: from here the broadcast may already be running, and
        # "something went wrong, no change was made" would be a lie.
        await self._send(interaction, reply)

    @staticmethod
    async def _send(interaction: discord.Interaction, reply: _Reply) -> None:
        if reply.embed is not None:
            await interaction.followup.send(
                content=reply.text,
                embed=reply.embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                content=reply.text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    # -- the state machine ----------------------------------------------------

    async def _watch(
        self,
        interaction: discord.Interaction,
        pool: Executor,
        game_id: int,
        speed: WatchSpeed = WatchSpeed.GAME,
    ) -> _Reply:
        league = await resolve_league(pool, interaction)
        ensure_command_legal("watch", league.current_phase)
        game = await game_repo.get_game(pool, game_id)
        if game is None or game.league_id != league.id:
            return _Reply(f"No game {game_id} in this league.")
        if game.status is not GameStatus.FINAL:
            return _Reply(_NOT_PLAYED.format(game_id=game_id))

        row = await broadcast_repo.get_broadcast(pool, league_id=league.id, game_id=game_id)
        if row is None:
            return _Reply(_NOT_IN_A_BLOCK)
        if row.thread_id is not None:
            return self._link_reply(interaction, row)
        if row.status is not BroadcastStatus.PENDING:
            # queued/live/finished/interrupted are only ever written together
            # with a thread (`record_thread`), so this row is corrupt.
            log.error("game %s: broadcast row is %s with no thread", game_id, row.status)
            return _Reply(_THREAD_LOST.format(game_id=game_id))

        # `pending`, no thread. A reveal that was claimed or posted means the
        # block is closed and the result is public or about to be: not a
        # broadcast, whatever memory still holds.
        if row.reveal_claimed_at is not None:
            return await self._revealed_or_unwatchable(pool, row)
        block_game = self.bot.blocks.get(league.id, game_id)
        if block_game is None or block_game.result is None:
            return await self._revealed_or_unwatchable(pool, row)
        return await self._start(interaction, pool, league.id, row, block_game, speed)

    async def _revealed_or_unwatchable(self, pool: Executor, row: GameBroadcast) -> _Reply:
        """The only place a score can reach a reply, and only through the box
        score of a game whose reveal is POSTED (P5-97.p): the fallback is gated
        on `reveal_posted_at`, not on the game being final."""
        if game_feed.box_score_is_public(row):
            panel = await view_service.get_boxscore_panel(pool, game_id=row.game_id)
            return _Reply(_REPLAY_GONE_REVEALED, embed=panel_to_embed(panel))
        if row.reveal_claimed_at is not None:
            # The claim only exists once the block has closed: "when the block closes"
            # would be false. The post is in flight, or failed and awaits the sweep.
            return _Reply(_RESULT_BEING_POSTED)
        return _Reply(_NOT_WATCHABLE)

    def _link_reply(self, interaction: discord.Interaction, row: GameBroadcast) -> _Reply:
        if row.thread_id is None:  # every caller checks first; loud, not a bare assert
            log.error("game %s: asked to link a broadcast row with no thread", row.game_id)
            return _Reply(_THREAD_LOST.format(game_id=row.game_id))
        if interaction.guild_id is None:
            return _Reply(_SERVER_ONLY)
        url = _thread_url(interaction.guild_id, row.thread_id)
        if row.status in (BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED):
            return _Reply(_LINK_FINISHED.format(url=url))
        return _Reply(_LINK_UNDER_WAY.format(game_id=row.game_id, url=url))

    async def _link_current(
        self, interaction: discord.Interaction, pool: Executor, league_id: int, game_id: int
    ) -> _Reply:
        """Re-read the row and link whoever else got there first."""
        row = await broadcast_repo.get_broadcast(pool, league_id=league_id, game_id=game_id)
        if row is not None and row.thread_id is not None:
            return self._link_reply(interaction, row)
        return _Reply(_STARTING_ELSEWHERE.format(game_id=game_id))

    # -- starting a broadcast -------------------------------------------------

    async def _start(
        self,
        interaction: discord.Interaction,
        pool: Executor,
        league_id: int,
        row: GameBroadcast,
        block_game: Any,
        speed: WatchSpeed = WatchSpeed.GAME,
    ) -> _Reply:
        game_id = row.game_id
        result = block_game.result
        scheduler = self.bot.broadcasts
        settings = scheduler.settings
        # The viewer's speed changes only the tick and the plan (WN-10.4); admission and
        # the reply wording read the scheduler's own settings above.
        pacing = settings_for_speed(settings, speed)
        # Read and admit with no await between them: `ahead` is "how many
        # broadcasts must finish before a slot is free for this one", computed
        # once, at admission, and worded in the placeholder and the reply.
        depth_before = scheduler.depth
        admission = scheduler.admit(
            league_id=league_id, game_id=game_id, user_id=interaction.user.id
        )
        if admission is Admission.ALREADY_LIVE:
            return await self._link_current(interaction, pool, league_id, game_id)
        if admission is Admission.REJECTED_FULL:
            return _Reply(_BROADCASTS_FULL.format(game_id=game_id))
        if admission is Admission.REJECTED_USER_CAP:
            return _Reply(
                _USER_CAP.format(cap=settings.max_live_broadcasts_per_user, game_id=game_id)
            )
        ahead = max(0, depth_before - settings.max_concurrent_broadcasts + 1)
        placeholder = (
            _PLACEHOLDER_WAITING.format(ahead=ahead, games=_games_word(ahead))
            if ahead
            else _PLACEHOLDER_NOW
        )

        reserved = True
        persisted = False  # the row says `queued`, so the thread is no longer ours to delete
        thread: Any = None
        created = False  # this call made `thread`; an adopted one is never ours to delete
        step = _STEP_GAMES
        try:
            try:
                data = await view_service.get_broadcast_inputs(pool, game_id=game_id, result=result)
                plan = plan_broadcast(
                    result,
                    game_id=game_id,
                    home_code=data.home.code,
                    away_code=data.away.code,
                    settings=pacing.plan,
                )
                # Rendered ONCE: the pacer's every tick and every cycle click read the very
                # same narration (watch-v4 T-5.a, docs/design/watch-narration-v2.md WN-8),
                # and the pager pages the same game's TERSE lines (WN-9.4).
                narration = plan_narration(
                    result, plan, home=data.home, away=data.away, names=data.names
                )
                lines = plan_period_lines(plan, home=data.home, away=data.away, names=data.names)
                pager_source = PagerSource.from_periods(
                    pager_matchup(data.home, data.away), lines
                )

                def component(frame_index: int, cursor: int, selected_period: int) -> Any:
                    return render_live_component(
                        plan.frames[frame_index],
                        plan,
                        selected_period=selected_period,
                        home=data.home,
                        away=data.away,
                        names=data.names,
                        result=result,
                        narration=narration,
                        cursor=cursor,
                    )

                def visible(last_line: int) -> tuple[int, ...]:
                    return visible_periods(plan, last_line)

                def component_view_for(request: ComponentViewRequest) -> Any:
                    return build_component_view(league_id, game_id, request)

                slate = SlateGame(
                    game_id=game_id,
                    home_code=block_game.home_code,
                    away_code=block_game.away_code,
                    game_date=block_game.game_date,
                )
                root = await self._watching_root(pool, league_id, row, slate)
                step = _STEP_THREAD
                thread = await self._existing_thread(root, game_id)
                if thread is None:
                    thread = await game_feed.bounded(
                        root.create_thread(
                            name=f"{slate.away_code} @ {slate.home_code} — game {game_id}",
                            auto_archive_duration=_THREAD_AUTO_ARCHIVE_MINUTES,
                        )
                    )
                    created = True
                step = _STEP_POST
                message = await game_feed.bounded(
                    thread.send(
                        content=placeholder, allowed_mentions=discord.AllowedMentions.none()
                    )
                )
                recorded = await broadcast_repo.record_thread(
                    pool,
                    league_id=league_id,
                    game_id=game_id,
                    thread_id=thread.id,
                    message_id=message.id,
                )
                if recorded:
                    persisted = True
                    # `spawn` must follow `record_thread` with nothing awaited in
                    # between: from here the row says `queued`, and only a task
                    # moves it on.
                    await scheduler.spawn(
                        job=BroadcastJob(
                            league_id=league_id,
                            game_id=game_id,
                            user_id=interaction.user.id,
                            plan=plan,
                            inputs=BroadcastInputs(
                                home=data.home, away=data.away, names=data.names, result=result
                            ),
                            message=message,
                            thread=thread,
                            db=pool,
                            narration=narration,
                            component_view_for=component_view_for,
                            settings=pacing,
                        )
                    )
                    reserved = False
                    self._register_pager(
                        league_id, game_id, interaction, pager_source, component, visible
                    )
                else:
                    # Another `/watch` (another process, or the sweep) recorded a
                    # thread first. Ours is the loser: give the place back, drop
                    # the thread nothing points at, link the winner's. The winner
                    # owns the game's lifecycle, so no `on_ended` from here.
                    scheduler.release_unspawned(league_id, game_id)
                    reserved = False
                    if created:
                        await self._delete_thread(thread)
                    return await self._link_current(interaction, pool, league_id, game_id)
            except game_feed.GamesChannelUnavailable:
                log.warning("game %s: /watch found no usable #games channel", game_id)
                scheduler.release_unspawned(league_id, game_id)
                reserved = False
                await self._restore(pool, league_id, game_id)
                return _Reply(_GAMES_CHANNEL_UNAVAILABLE)
            except discord.Forbidden:
                log.warning("game %s: /watch was refused a permission at step %s", game_id, step)
                scheduler.release_unspawned(league_id, game_id)
                reserved = False
                deleted: bool | None = None
                if thread is not None:
                    deleted = await self._delete_thread(thread) if created else False
                await self._restore(pool, league_id, game_id)
                return _Reply(_forbidden_reply(step, thread_deleted=deleted))
            except Exception:
                scheduler.release_unspawned(league_id, game_id)
                reserved = False
                if persisted:
                    # Only `spawn` can fail from here (a programming error): a
                    # `queued` row with no task would read "under way" forever, so
                    # end it; its thread stays, the row points at it.
                    await self._interrupt_stranded(pool, league_id, game_id)
                elif thread is not None and created:
                    await self._delete_thread(thread)
                await self._restore(pool, league_id, game_id)
                raise
        finally:
            # The net for a `CancelledError` (not an `Exception`): the reservation
            # is returned synchronously, and only if this call still holds it --
            # a second `/watch` may have been admitted since a branch above
            # released it.
            if reserved:
                scheduler.release_unspawned(league_id, game_id)

        text = _STARTED.format(game_id=game_id, url=thread.jump_url)
        text += (
            _STARTS_LATER.format(ahead=ahead, games=_games_word(ahead)) if ahead else _STARTS_NOW
        )
        return _Reply(text)

    def _register_pager(
        self,
        league_id: int,
        game_id: int,
        interaction: discord.Interaction,
        source: PagerSource,
        component: Callable[[int, int], Any],
        visible: Callable[[int], tuple[int, ...]],
    ) -> None:
        """Hand the pager the broadcast's OWN `PaceProgress` (the object the pacer
        mutates), right after `spawn` and with no await between them, plus the two
        closures the cycle click renders through (watch-v4 T-5.d). The broadcast is
        already running: a pager that cannot be registered costs the buttons' answers,
        never the broadcast, so a failure is logged and swallowed here."""
        try:
            live = self.bot.broadcasts.live(league_id, game_id)
            if live is None or interaction.guild_id is None:
                log.error("game %s: no live broadcast to page; the buttons will degrade", game_id)
                return
            self.bot.pagers.register(
                league_id,
                game_id,
                PagerEntry(
                    guild_id=interaction.guild_id,
                    source=source,
                    progress=live.progress,
                    component=component,
                    visible=visible,
                ),
                keep=lambda other_league, other_game: game_is_open(
                    self.bot, other_league, other_game
                ),
            )
        except Exception:
            log.exception("game %s: could not register the pager", game_id)

    async def _watching_root(
        self, pool: Executor, league_id: int, row: GameBroadcast, slate: SlateGame
    ) -> Any:
        """The game's `#games` line as a message, showing "watching now". A
        missing line (a failed slate post, P5-97.v) or a deleted one is posted
        fresh first -- as the matchup line, then persisted, then edited -- so the
        repair and the normal path end in the same state."""
        channel = await game_feed.games_channel(self.bot, pool, league_id)
        root_id = row.root_message_id
        if root_id is not None:
            try:
                await game_feed.edit_line(channel, root_id, build_watching_line(slate))
                return channel.get_partial_message(root_id)
            except discord.NotFound:
                log.warning(
                    "game %s: root message %s is gone; posting a fresh line", row.game_id, root_id
                )
        root_id = await game_feed.send_line(channel, build_matchup_line(slate))
        await broadcast_repo.set_root_message(
            pool, league_id=league_id, game_id=row.game_id, root_message_id=root_id
        )
        await game_feed.edit_line(channel, root_id, build_watching_line(slate))
        return channel.get_partial_message(root_id)

    async def _existing_thread(self, root: Any, game_id: int) -> Any | None:
        """The thread already attached to the game's root message, or `None`.
        The cache first (free, active threads only), then one API call. A lookup
        that fails for any reason but "there is none" is logged and treated as
        none: `create_thread` is then attempted and Discord has the last word."""
        cached = root.thread
        if cached is not None:
            return cached
        try:
            return await game_feed.bounded(root.fetch_thread())
        except discord.NotFound:
            return None
        except Exception as exc:
            log.warning(
                "game %s: could not look for an existing thread: %s",
                game_id,
                game_feed.describe_discord_failure(exc),
            )
            return None

    async def _delete_thread(self, thread: Any) -> bool:
        """Best-effort removal of a thread nothing points at. `False` means it
        is still there (and is logged); never raises for an `Exception`."""
        try:
            await game_feed.bounded(thread.delete())
        except Exception:
            log.warning("could not delete the unused thread %s", getattr(thread, "id", "?"))
            return False
        return True

    async def _interrupt_stranded(self, pool: Executor, league_id: int, game_id: int) -> None:
        try:
            await broadcast_repo.transition_status(
                pool,
                league_id=league_id,
                game_id=game_id,
                expected=BroadcastStatus.QUEUED,
                new=BroadcastStatus.INTERRUPTED,
            )
        except Exception:
            log.exception("game %s: could not end a queued row whose task never started", game_id)

    async def _restore(self, pool: Executor, league_id: int, game_id: int) -> None:
        """After a failed start, put `#games` back the way the rest of the
        system expects: the matchup line while the block is open, or the result
        if the block closed while this `/watch` held the reservation
        (`close_block` skipped the game as still broadcasting). That is exactly
        `on_ended`; it never raises."""
        await game_feed.on_ended(
            self.bot,
            league_id=league_id,
            game_id=game_id,
            status=BroadcastStatus.INTERRUPTED,
            executor=pool,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BroadcastCommands(bot))
