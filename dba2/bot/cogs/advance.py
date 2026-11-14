"""`/advance` -- the one command that moves a league -- and its five commissioner companions,
`/advance-to`, `/advance-force`, `/advance-unlock`, `/advance-postpone-game` and
`/advance-restore-game` (docs/design/phase-5-stage-e-advance.md P5-99.a, P5-99.f, P5-99.g,
P5-99.j, P5-99.m, P5-99.u.1; build-plan commits 14 and 15).

**Everything this cog decides is wording and Discord.** What a press does -- claim the
league, derive which of the four kinds it is, move the pointer, run the phase work, the
AI hook and the sim, write every checkpoint -- is `season_service.press`'s, and it hands
back a `PressReport` of plain types. This file checks the commissioner (the bot-layer
half of P5-38; the service checks again), defers, passes the press its cached media
library and a progress callback (whose milestones the service names, plus the cog's own
"Posting..." just before the Discord calls below), turns each service error into one
sentence, and then -- strictly AFTER every transaction has closed (P5-14) -- makes the
Discord calls the report calls for.

**The order of those calls is fixed (M6), and each is wrapped on its own.** The scoreless
`#games` slate posts first, because it is the record of what was simulated and nothing
derived from the results may reach the league before it. Then the draft's on-the-clock
announcement, the wire, the standings. A failure in any one is logged and named in the
reply's "Did not go out" field, never raised past the others: the press has already
committed, so "nothing happened" would be a lie and a lost slate must not cost the wire
its stories. This is `game_feed`/`draft_feed`'s own "failures degrade, they do not abort"
rule (`game_feed.py:32-40`), applied to the press.

**The wire post is driven by what the database says is unposted, never by what the
report says was written** (P5-99.m.1). `post_new_wire_stories` reads the stories with no
`posted_at` and marks each one as its own send returns; this cog therefore passes no
cycle, so a report that arrives after a partial earlier attempt still posts exactly the
remainder. It is attempted only after a press that finished a media selection -- the
startup sweep (build-plan commit 17) is what covers a restart.

**`dba2/bot/` writes no `leagues` column** (P5-99.m.1, pinned by
`tests/unit/test_bot_writes_no_leagues_column.py`): the claim, the three completion
checkpoints and the pointer are all `season_service`'s. **Every reply is ephemeral,
including the defer** (`cogs/draft.py`'s D2 precedent: a followup after a public defer is
itself public and leaves a "thinking..." placeholder nothing replaces). `#games`,
`#league-wire` and the pinned standings message are the public record of a press.

**The media library is parsed once per process and cached at module level**
(P5-99.s.4/s.5): `load_media_library` is a synchronous file read that parses and validates
`seeds/media_phrases.json`, and `press` takes the loaded object as an argument -- it never
loads one itself.

**The five companions are thinner still.** Each is commissioner-only (bot-layer check
before the defer; the service checks again), has its own `COMMAND_GATES` entry, defers
ephemerally, makes ONE service call and shows its answer -- and every refusal the service
raises (`/advance-restore-game`'s three, the age threshold on `/advance-unlock`, `PRESEASON`
under `/advance-force`) is shown verbatim, so the wording lives where the rule does. None of
them runs Discord calls after the fact: they move a pointer or a game's status, and post
nothing to `#games`, the wire or the standings.

**`/advance-run <phase>` and `/advance-run-stop` (docs/design/advance-run.md)** press this same
`season_service.press` in a loop (`services/season_run.py`) until the league enters the phase or
a stop condition says otherwise. The cog's job is Discord: one run per league (`bot.runs`), the
deferred reply edited every 5 s, and a post-press record that differs from `/advance`'s in
exactly the two ways AR-11 amends P5-99.a and P5-99.m's M6 for -- **no per-block `#games` slate**
(`open_block` is never called; ~2,750 Discord calls would stretch a ~90 s season to the better
part of an hour), and **one** standings sync and **one** score-free digest at the end. Kept as
`/advance` does them: the draft announcement with its on-the-clock ping (row 5 stops a run
*because* a human is on the clock) and the wire stories. `/advance`, `/advance-to` and
`/advance-force` are unchanged.

**Nothing here has run against a live Discord gateway.** Interaction edits, `#games`
posts, the wire and the pinned standings message are exercised only through in-process
fakes (`tests/unit/bot/test_advance_cog.py`, `tests/unit/bot/test_advance_run_cog.py`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.config import get_config
from dba2.core.errors import PhaseError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import league_repo, team_repo
from dba2.domain.league import League
from dba2.domain.phase import Phase
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.advance import (
    AdvanceToSummary,
    ForceSummary,
    PhaseAiCounts,
    PressOutcome,
    PressSummary,
    RunStopKind,
    RunSummary,
    build_advance_to_panel,
    build_force_panel,
    build_press_panel,
    build_run_digest,
    build_run_panel,
    build_run_progress_line,
)
from dba2.presentation.draft import clip_to_message_limit
from dba2.presentation.panel import Panel
from dba2.services import media_service, season_run, season_service
from dba2.services.gm_service import DraftCycleOutcome, DraftCycleState, PhaseHookResult
from dba2.services.media_service import LoadedMediaLibrary
from dba2.services.season_service import PressKind, PressReport

from .. import draft_feed, game_feed, surfaces
from ..block_registry import BlockGame
from ..context import NoLeagueForGuild, current_cycle_index, resolve_league
from ..embeds import panel_to_embed
from ..gates import CommandNotLegalNow, ensure_command_legal, ensure_commissioner
from ..interaction_errors import (
    GENERIC_FAILURE_MESSAGE,
    KNOWN_DOMAIN_ERRORS,
    reply_command_failure,
    reply_ephemeral,
)
from ..post_commit import run_after_commit
from ..run_registry import LeagueRun

log = get_logger(__name__)

_PROGRESS_INTERVAL_SECONDS = 2.0
"""P5-99.j: the progress editor makes at most one edit per this many seconds. Discord
rate-limits interaction edits, and a press that reports ten milestones in a second gains
nothing from ten edits."""

_PROGRESS_DONE = "Finished -- the result is below."

_OUTCOME_BY_KIND: dict[PressKind, PressOutcome] = {
    PressKind.ENTRY: PressOutcome.ENTRY,
    PressKind.BLOCK: PressOutcome.BLOCK,
    PressKind.BLOCKED: PressOutcome.BLOCKED,
    PressKind.DRAFT_RESUME: PressOutcome.DRAFT_RESUME,
}


def _require_a_reply_for_every_press_kind(mapping: dict[PressKind, PressOutcome]) -> None:
    """Every `PressKind` maps to a reply outcome, checked at import with a real `raise` -- an
    `assert` is stripped by `python -O`, and the KeyError it would have prevented arrives in
    `advance`, after the press has committed."""
    missing = sorted(kind.value for kind in set(PressKind) - set(mapping))
    unknown = sorted(str(kind) for kind in set(mapping) - set(PressKind))
    if missing or unknown:
        raise RuntimeError(
            f"the press kinds and their reply outcomes disagree: no reply for {missing}, "
            f"replies for unknown kinds {unknown}"
        )


_require_a_reply_for_every_press_kind(_OUTCOME_BY_KIND)

_LIBRARY: LoadedMediaLibrary | None = None

_ADVANCE_TO_CHOICES = [
    app_commands.Choice(name=p.value, value=p.value) for p in Phase if p is not Phase.SETUP
]
"""Every phase but `SETUP`, which no route ever enters (`graph.route_to`), so `/advance-to` would
refuse it every time. Discord allows 25 choices; the `Phase` enum has 20. A raw call can still
pass anything, so `_parse_phase` re-checks and the service refuses."""

_FORCE_CHOICES = [c for c in _ADVANCE_TO_CHOICES if c.value != Phase.PRESEASON.value]
"""`/advance-force`'s list: also without `PRESEASON`, the other phase force always refuses
(`season_service.force_target_refusal`). The list is a courtesy -- the service is the rule."""


_RUN_DEADLINE = timedelta(minutes=12)
"""AR-2: a run stops between presses once `interaction.created_at + 12 minutes` has passed --
measured from the interaction's creation, because the 15-minute token is, and leaving 3 minutes
of margin for the final posts and the panel. The cap is soft by exactly one press."""

_RUN_PROGRESS_INTERVAL_SECONDS = 5.0
"""AR-8: one edit of the deferred reply per this many seconds, plus one forced line per phase
entry. Slower than a single press's 2 s: a run is ~120 edits over ten minutes, not a burst."""

_STANDINGS_SYNC_EVERY = 10
"""docs/design/standings-v2-conference-split.md SV-1/AR-7 (amended): the pinned standings
message syncs every 10th GAME-CARRYING press during a run, not once at the very end. A full
147-block-press season gets roughly 15 syncs total (14 mid-run plus the end-of-run fallback
for any partial leftover), evenly spread whether the run covers the whole season or the
short, single-phase runs `/advance-run <phase>` is actually used for."""

_STOP_KIND_BY_RUN_STOP: dict[season_run.RunStop, RunStopKind] = {
    stop: RunStopKind(stop.value) for stop in season_run.RunStop
}
"""Every `RunStop` maps to the presentation's `RunStopKind` twin. Built by value, so a stop
added to the service with no twin raises `ValueError` HERE, at import -- the same "real raise,
not an assert" pin `_OUTCOME_BY_KIND` carries -- instead of a `KeyError` after a run has
committed hundreds of presses."""


_FAILED_REVEALS = frozenset(
    {
        game_feed.RevealOutcome.POST_FAILED,
        game_feed.RevealOutcome.MARK_FAILED,
        game_feed.RevealOutcome.ERRORED,
    }
)
"""The `RevealOutcome`s that mean a score did not reach `#games` and the row is left for the
sweep. `NOT_OWNED` (someone else owns the reveal) and `NOT_REVEALABLE` (nothing to publish)
are not failures of this close."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _RunLedger:
    """What a run's post-press steps have accumulated: the numbers the progress line shows,
    what reached the wire, whether the end-of-run standings fallback is owed, how many
    game-carrying presses have landed since the last mid-run standings sync (SV-1), and every
    "did not go out" notice (each said once, however many presses hit it)."""

    started: float
    game_days: set[str] = field(default_factory=set)
    games: int = 0
    last_game_date: str | None = None
    wire_stories: int = 0
    standings_owed: bool = False
    presses_since_standings_sync: int = 0
    notices: list[str] = field(default_factory=list)

    def add_notice(self, notice: str) -> None:
        if notice not in self.notices:
            self.notices.append(notice)


def _run_held_message(run: LeagueRun | None) -> str:
    if run is None:  # unreachable: `begin` refused only because a run holds the slot
        return "A run is already going for this league."
    ran = _utcnow() - run.started_at
    minutes, seconds = divmod(int(ran.total_seconds()), 60)
    where = f" It is in {run.phase.value}." if run.phase is not None else ""
    return (
        f"An /advance-run is already going for this league (started by <@{run.user_id}> "
        f"{minutes}m{seconds:02d}s ago, {run.presses} press(es) so far).{where} "
        "/advance-run-stop ends it after its current press."
    )


async def _on_the_clock_label(pool: asyncpg.Pool, cycle: DraftCycleOutcome) -> str:
    """`TOR (round 1, pick 5)` for the run's draft stop: decoration on a stop, so a failed
    lookup degrades to `team <id>` and never fails the summary."""
    if cycle.team_id is None:
        return "the team on the clock"
    label = f"team {cycle.team_id}"
    try:
        team = await team_repo.get_team(pool, cycle.team_id)
        if team is not None:
            label = team.code
    except Exception:
        log.warning("/advance-run: could not look up team %s's code", cycle.team_id, exc_info=True)
    if cycle.round is not None and cycle.pick_number is not None:
        label += f" (round {cycle.round}, pick {cycle.pick_number})"
    return label


def _run_plain_text(summary: RunSummary) -> str:
    """The panel's plain-text fallback: the digest, which carries what did not go out."""
    return clip_to_message_limit(build_run_digest(summary))


def _bare_run_summary(report: season_run.RunReport, ledger: _RunLedger) -> RunSummary:
    """What the summary falls back to when the full one could not be built: only what the
    report itself holds, no database lookup and no derived view that could raise again."""
    return RunSummary(
        stop=_STOP_KIND_BY_RUN_STOP[report.stop],
        from_phase=report.from_phase,
        target=report.target,
        final_phase=report.final_phase,
        presses=report.presses,
        elapsed_seconds=report.elapsed_seconds,
        notices=tuple(ledger.notices),
    )


def _monotonic() -> float:
    return time.monotonic()


async def _media_library() -> LoadedMediaLibrary:
    """The process's media library, loaded on first use and cached (P5-99.s.4). Two presses
    of DIFFERENT leagues can race here -- the claim is per league, not per process -- and both
    would load: the loser's object replaces the winner's with an equal one, read from the same
    file, so the race is harmless rather than excluded."""
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = await asyncio.to_thread(media_service.load_media_library)
    return _LIBRARY


def _ai_pass_notice(summary: media_service.RewriteSummary) -> str | None:
    """The one honest line the commissioner gets about the newsroom AI (S8-11).
    A full pass says nothing (the wire line already reports what posted); a
    partial or aborted one says so, because the failure mode of a silent AI
    writer is a commissioner who thinks the feature is on while their own
    coding session has exhausted the subscription window. Being capped by
    `MEDIA_AI_MAX_STORIES_PER_CYCLE` is configuration, not a failure, so it
    is not reported. A partial pass also says how many stories needed a gate
    retry and how many calls it spent (G2-18), and whether the per-pass call
    budget cut it short; a full pass stays silent even if it retried."""
    if summary.disabled_reason not in (None, "disabled"):
        return (
            f"The newsroom AI is switched on but could not run ({summary.disabled_reason}); "
            "the stories posted in the house style."
        )
    if summary.attempted and (
        summary.accepted < summary.attempted or summary.aborted or summary.budget_exhausted
    ):
        notes = []
        if summary.aborted:
            notes.append("it stopped early after repeated failures")
        if summary.budget_exhausted:
            notes.append(f"it reached its budget of {summary.call_budget} calls")
        calls = "call" if summary.calls == 1 else "calls"
        retried = "story" if summary.retried == 1 else "stories"
        notes.append(
            f"{summary.retried} {retried} needed a retry, {summary.calls} {calls} in all"
            if summary.retried
            else f"{summary.calls} {calls}"
        )
        return (
            f"The newsroom AI wrote {summary.accepted} of {summary.attempted} stories "
            f"({'; '.join(notes)}); the rest posted in the house style (logged)."
        )
    return None


async def _rewrite_wire_with_ai(
    pool: asyncpg.Pool, league: League, cycle_index: int
) -> str | None:
    """S8-1's post-commit rewrite pass, run just before the wire post so the
    stories that pass the gates post as AI prose and every other story posts
    exactly as the template wrote it. Returns a commissioner notice or `None`.
    Inert with `MEDIA_AI_ENABLED` false (the default): one config read, no
    library load, no database or process work. Wrapped in `run_after_commit`
    like every other follow-up, so an unhandled failure here can never fail
    the press or stop the wire."""

    async def _pass() -> media_service.RewriteSummary:
        config = get_config()
        if not config.media_ai_enabled:
            return media_service.RewriteSummary(disabled_reason="disabled")
        return await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league.id, season=league.current_season, cycle_index=cycle_index,
            library=await _media_library(), config=config,
        )

    summary = await run_after_commit(
        _pass, log_context=f"rewriting league {league.id}'s wire stories with the newsroom AI"
    )
    if summary is None:
        return "The newsroom AI pass failed (error logged); the stories posted in the house style."
    return _ai_pass_notice(summary)


class _ProgressEditor:
    """`press`'s `on_progress`: edits the deferred ephemeral reply with the latest
    milestone, at most once every `interval` seconds (`_PROGRESS_INTERVAL_SECONDS` unless a
    caller passes its own: `/advance-run` uses a slower one). `interval` is keyword-only with a
    default so the three positional constructions in `test_advance_cog.py` stay valid.

    It never raises: progress is decoration, and a failed edit (an expired interaction, a
    rate limit) must not disturb a press that holds the league's claim. A milestone that
    arrives inside the interval is dropped, not queued -- the next one supersedes it."""

    def __init__(
        self, interaction: discord.Interaction, *, interval: float = _PROGRESS_INTERVAL_SECONDS
    ) -> None:
        self._interaction = interaction
        self._interval = interval
        self._last_edit: float | None = None
        self.edited = False

    async def __call__(self, message: str, *, force: bool = False) -> None:
        """`force` skips the throttle for the one line a person always wants (`/advance-run`'s
        phase entries); the attempt is still stamped, so a failing edit is still throttled."""
        now = _monotonic()
        if (
            not force
            and self._last_edit is not None
            and now - self._last_edit < self._interval
        ):
            return
        # Stamped BEFORE the attempt: a failing edit (an expired interaction, a rate limit) is
        # throttled like a successful one instead of being retried at every milestone.
        self._last_edit = now
        try:
            await self._interaction.edit_original_response(
                content=message, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            log.warning("/advance: could not edit the progress line %r", message, exc_info=True)
            return
        self.edited = True

    async def finish(self) -> None:
        """Replaces the last milestone with a pointer to the real reply, so a stale
        "Simming..." never sits above the result. A no-op when nothing was ever shown."""
        if not self.edited:
            return
        try:
            await self._interaction.edit_original_response(
                content=_PROGRESS_DONE, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            log.warning("/advance: could not clear the progress line", exc_info=True)


def _draft_line(outcome: DraftCycleOutcome, drafted: int) -> str:
    head = f"Draft: the AI made {drafted} pick(s)"
    if outcome.state is DraftCycleState.COMPLETE:
        return f"{head}; every pick is now made."
    if outcome.state is DraftCycleState.WAITING_ON_HUMAN:
        where = ""
        if outcome.round is not None and outcome.pick_number is not None:
            where = f" (round {outcome.round}, pick {outcome.pick_number})"
        return f"{head}; team {outcome.team_id} is on the clock{where}."
    reason = outcome.reason.value if outcome.reason is not None else "no reason recorded"
    return f"{head}; the draft is {outcome.state.value} ({reason})."


def _hook_lines(result: PhaseHookResult) -> list[str]:
    """One line per AI decision layer the hook touched (`PhaseHookResult` is a bag with
    every untouched field `None`)."""
    lines: list[str] = []
    if result.window_pass is not None:
        lines.append(
            f"Classified {len(result.window_pass.window_by_team)} team(s)' competitive windows."
        )
    if result.trade_window is not None:
        lines.append(f"AI trade window: {len(result.trade_window.executions)} trade(s) executed.")
    if result.resign_offers is not None:
        lines.append(f"Re-sign window: {len(result.resign_offers)} offer(s) made.")
    if result.free_agency is not None:
        lines.append(f"Free agency: {len(result.free_agency.signings)} signing(s).")
    if result.draft_cycle is not None:
        drafted = 0 if result.draft is None else len(result.draft.picks)
        lines.append(_draft_line(result.draft_cycle, drafted))
    if result.waivers is not None:
        lines.append(f"Waivers: {len(result.waivers.executions)} cut(s).")
    return lines


def _service_lines(report: PressReport) -> list[str]:
    """What the press itself did, in the order it did it, as sentences."""
    lines: list[str] = []
    if report.resumed_hook_result is not None:
        lines.append(
            f"Finished the {report.resumed_hook_result.phase.value} AI pass an earlier press "
            "left unfinished."
        )
        lines.extend(_hook_lines(report.resumed_hook_result))
    if report.expired_offers:
        lines.append(f"Expired {report.expired_offers} stale trade offer(s).")
    if report.checkpoint_result is not None:
        cp = report.checkpoint_result
        line = (
            f"Agency checkpoint: {cp.players_evaluated} player(s) evaluated, "
            f"{cp.ladder_transitions} ladder transition(s)"
        )
        if cp.players_skipped:
            line += f"; {cp.players_skipped} already done by an earlier, interrupted pass"
        lines.append(line + ".")
    if report.hook_result is not None:
        lines.extend(_hook_lines(report.hook_result))
    if report.block is not None:
        lines.append(f"Simulated {len(report.block.games)} game(s) on {report.block.block_id}.")
    if report.failed_games:
        causes = {f.game_id: f.cause for f in report.failures}
        named = "; ".join(
            f"game {i} ({causes.get(i, 'cause not recorded')})" for i in report.failed_games
        )
        lines.append(
            f"{len(report.failed_games)} game(s) failed to sim and stay scheduled: {named}. "
            "/advance retries them; /advance-postpone-game <game_id> <reason> takes one out."
        )
    if report.media_cycle_index is not None:
        lines.append(f"Media cycle {report.media_cycle_index} selected its stories.")
    return lines


def _parse_phase(value: str) -> Phase | None:
    try:
        return Phase(value)
    except ValueError:
        return None


class AdvanceCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    async def _authorize(
        interaction: discord.Interaction, command: str, action: str
    ) -> tuple[object, League] | None:
        """The fast, synchronous-shaped rejections that come BEFORE `defer()`
        (`cogs/commissioner.py`'s own convention): the league, the command gate, and P5-38's
        bot-layer half. Replies ephemerally and returns `None` on a refusal."""
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal(command, league.current_phase)
            ensure_commissioner(interaction.user.id, league, action=action)
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied) as exc:
            await reply_ephemeral(interaction, str(exc))
            return None
        return pool, league

    @app_commands.command(
        name="advance",
        description="Commissioner: run the league's next press (a game day, or the next phase).",
    )
    async def advance(self, interaction: discord.Interaction) -> None:
        authorized = await self._authorize(interaction, "advance", "advance")
        if authorized is None:
            return
        pool, league = authorized

        await interaction.response.defer(ephemeral=True)
        progress = _ProgressEditor(interaction)
        try:
            report = await season_service.press(
                pool,
                league_id=league.id,
                actor_user_id=interaction.user.id,
                library=await _media_library(),
                on_progress=progress,
            )
        except Exception as exc:
            # Wide for its reason (`cogs/roster.py`'s own): a DB-layer failure is no
            # domain error and, uncaught, means "deferred forever" for the commissioner.
            await progress.finish()
            await reply_command_failure(interaction, exc, log_label="/advance")
            return

        # The press has committed from here down. Everything below is a never-raising
        # follow-up: a failure loses a piece of the public record and says so; it never
        # claims the press did not happen.
        done, notices = await self._post_the_record(interaction, pool, league, report, progress)
        summary = PressSummary(
            outcome=_OUTCOME_BY_KIND[report.kind],
            from_phase=report.from_phase,
            to_phase=report.to_phase,
            done=tuple(_service_lines(report) + done),
            gate_lines=tuple(b.message for b in report.verdict.blockers),
            notices=tuple(notices),
        )
        await progress.finish()
        await self._reply_with_the_press(interaction, summary)

    async def _post_the_record(
        self,
        interaction: discord.Interaction,
        pool: asyncpg.Pool,
        league: League,
        report: PressReport,
        progress: _ProgressEditor,
    ) -> tuple[list[str], list[str]]:
        """Every Discord-side call the report calls for, in the fixed order of the module
        docstring, each independently wrapped. Returns `(done, notices)`: what reached
        Discord, and what did not. The "Posting..." milestone is this cog's own -- the service
        has no idea what gets posted -- and is announced only when something will be."""
        done: list[str] = []
        notices: list[str] = []

        slate_games = report.block.games if report.block is not None else ()
        # `resumed_hook_result` can carry a DRAFT cycle too (a prefix that finished a
        # crashed DRAFT hook), so both are looked at.
        draft_hooks = [
            hook
            for hook in (report.hook_result, report.resumed_hook_result)
            if hook is not None and hook.draft_cycle is not None
        ]
        wants_wire = report.media_cycle_index is not None
        wants_standings = bool(slate_games) or wants_wire
        # The slate and the draft announcement need only the bot; the wire and the standings
        # need this server, so a press with only those to post says nothing when there is none.
        if draft_hooks or slate_games or (wants_wire and interaction.guild is not None):
            await progress("Posting the results to Discord...")

        if report.block is not None and slate_games:
            block = report.block
            slate = await run_after_commit(
                lambda: self._open_the_slate(pool, league.id, block),
                log_context=f"opening league {league.id}'s #games block {block.block_id}",
            )
            if slate is None:
                notices.append(
                    "The #games slate could not be posted (error logged): the games are "
                    "simulated and final, but their scoreless slate is missing."
                )
            else:
                done.append(f"Opened the #games slate ({len(slate_games)} game(s)).")
                if slate.failed:
                    notices.append(
                        f"{len(slate.failed)} part(s) of the #games slate did not post (error "
                        "logged); /watch and the startup sweep repair a missing game post."
                    )

        # `human_pick=None` because no person picked in a press
        # (`draft_feed.notify_on_the_clock`'s docstring names this seam).
        for hook in draft_hooks:
            announced = await run_after_commit(
                lambda hook=hook: draft_feed.notify_on_the_clock(
                    self.bot,
                    pool,
                    league_id=league.id,
                    season=hook.season,
                    outcome=hook.draft_cycle,
                    human_pick=None,
                ),
                log_context=f"announcing league {league.id}'s draft cycle",
            )
            if announced is None or not announced.complete:
                notices.append(
                    "The draft announcement did not fully post -- check #games and the team's "
                    "GM channel."
                )

        guild = interaction.guild
        if guild is None:
            if wants_wire or wants_standings:
                notices.append(
                    "The wire and the standings could not be updated: this server is not "
                    "available to the bot."
                )
            return done, notices

        if wants_wire:
            assert report.media_cycle_index is not None
            ai_notice = await _rewrite_wire_with_ai(pool, league, report.media_cycle_index)
            if ai_notice is not None:
                notices.append(ai_notice)
            posted = await run_after_commit(
                lambda: surfaces.post_new_wire_stories(
                    guild, pool, league_id=league.id, season=league.current_season
                ),
                log_context=f"posting league {league.id}'s wire stories",
            )
            if posted is None:
                notices.append(
                    "The wire post failed (error logged): the stories are saved and post on the "
                    "next press or at startup."
                )
            elif posted:
                done.append(f"Posted {posted} wire {'story' if posted == 1 else 'stories'}.")

        if wants_standings:
            synced = await run_after_commit(
                lambda: self._sync_standings(guild, pool, league.id, report.cycle_ordinal),
                log_context=f"syncing league {league.id}'s standings message",
            )
            if synced is None:
                notices.append("The standings message could not be updated (error logged).")
            else:
                done.append("Updated the standings message.")
        return done, notices

    async def _open_the_slate(
        self, pool: asyncpg.Pool, league_id: int, block: season_service.SimmedBlock
    ) -> game_feed.OpenBlockReport:
        """Maps each `SimmedGame` to a `BlockGame` -- all seven fields, the two codes from
        the league's own team rows -- and opens the block on the POOL, so
        `game_feed._require_no_open_transaction` is satisfied trivially (P5-99.i)."""
        teams = await team_repo.list_teams_for_league(pool, league_id)
        codes = {t.id: t.code for t in teams}
        games = [
            BlockGame(
                game_id=g.game_id,
                home_team_id=g.home_team_id,
                away_team_id=g.away_team_id,
                home_code=codes[g.home_team_id],
                away_code=codes[g.away_team_id],
                game_date=g.game_date,
                result=g.result,
            )
            for g in block.games
        ]
        return await game_feed.open_block(
            self.bot,
            pool,
            league_id=league_id,
            season=block.season,
            block_id=block.block_id,
            games=games,
        )

    @staticmethod
    async def _sync_standings(
        guild: discord.Guild, pool: asyncpg.Pool, league_id: int, report_cycle: int | None
    ) -> bool:
        """Syncs the pinned standings message, labelled with the press report's own cycle
        ordinal or, where the press had none (a phase that bears no cycle), the display proxy
        read from the league AS IT STANDS NOW -- not the row `_authorize` read before the
        press, whose `hook_completed_cycle_index` the press may have just moved."""
        league = await league_repo.get_league(pool, league_id)
        if league is None:
            raise RuntimeError(f"league {league_id} no longer exists to sync its standings for")
        await surfaces.sync_standings_message(
            guild,
            pool,
            league_id=league_id,
            season=league.current_season,
            cycle_index=report_cycle if report_cycle is not None else current_cycle_index(league),
        )
        return True

    async def _reply_with_the_press(
        self, interaction: discord.Interaction, summary: PressSummary
    ) -> None:
        await self._send_panel(
            interaction,
            lambda: build_press_panel(summary),
            _plain_text(summary),
            label="/advance",
        )

    @staticmethod
    async def _send_panel(
        interaction: discord.Interaction, build: Callable[[], Panel], plain: str, *, label: str
    ) -> None:
        """The last thing a command does and the last thing in it that can fail
        (`cogs/draft.py::_reply_with_the_pick`'s own reasoning): the change has committed,
        so building the panel and sending it are wrapped, the panel degrades to plain text,
        and exactly ONE send is ever attempted -- a send that raises may already have
        reached Discord, and posting a second copy to find out is the worse answer."""
        embed = None
        content = None
        try:
            embed = panel_to_embed(build())
        except Exception:
            log.exception(
                "%s: the reply panel could not be built for interaction %s -- sending plain "
                "text; the change itself committed",
                label,
                interaction.id,
            )
            content = clip_to_message_limit(plain)
        kwargs = {} if embed is None else {"embed": embed}
        try:
            await interaction.followup.send(
                content=content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                **kwargs,
            )
        except Exception:
            log.exception(
                "%s: the reply could not be sent for interaction %s -- it may or may not have "
                "reached Discord, so it is NOT retried; the change itself committed",
                label,
                interaction.id,
            )

    # --- the five companions (build-plan commit 15) ------------------------------------------

    @app_commands.command(
        name="advance-to",
        description="Commissioner: step through the no-op phases toward a phase (runs no work).",
    )
    @app_commands.describe(phase="The phase to walk toward")
    @app_commands.choices(phase=_ADVANCE_TO_CHOICES)
    async def advance_to(self, interaction: discord.Interaction, phase: str) -> None:
        authorized = await self._authorize(interaction, "advance_to", "advance to a phase")
        if authorized is None:
            return
        pool, league = authorized
        to_phase = _parse_phase(phase)
        if to_phase is None:
            await reply_ephemeral(interaction, f"There is no phase called {phase!r}.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            report = await season_service.advance_to(
                pool, league_id=league.id, actor_user_id=interaction.user.id, to_phase=to_phase
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/advance-to")
            return
        summary = AdvanceToSummary(
            from_phase=report.from_phase,
            requested=report.requested,
            entered=report.entered,
            reached=report.reached,
            stop_reason=report.stop_reason,
            gate_lines=tuple(b.message for b in report.blockers),
        )
        await self._send_panel(
            interaction,
            lambda: build_advance_to_panel(summary),
            f"/advance-to {report.requested.value}: entered "
            f"{', '.join(p.value for p in report.entered) or 'nothing'}. "
            f"{report.stop_reason or ''}",
            label="/advance-to",
        )

    @app_commands.command(
        name="advance-force",
        description="Commissioner: force the league to a phase, past its blockers (audited).",
    )
    @app_commands.describe(phase="The phase to force the league into", reason="Why (audited)")
    @app_commands.choices(phase=_FORCE_CHOICES)
    async def advance_force(
        self, interaction: discord.Interaction, phase: str, reason: str
    ) -> None:
        authorized = await self._authorize(interaction, "advance_force", "force a phase change")
        if authorized is None:
            return
        pool, league = authorized
        to_phase = _parse_phase(phase)
        if to_phase is None:
            await reply_ephemeral(interaction, f"There is no phase called {phase!r}.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            report = await season_service.force(
                pool,
                league_id=league.id,
                actor_user_id=interaction.user.id,
                to_phase=to_phase,
                reason=reason,
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/advance-force")
            return
        summary = ForceSummary(
            from_phase=report.from_phase,
            to_phase=report.to_phase,
            reason=reason.strip(),
            overrode=tuple(b.message for b in report.overrode),
            skipped_work=report.skipped_work,
        )
        await self._send_panel(
            interaction,
            lambda: build_force_panel(summary),
            f"Forced {report.from_phase.value} -> {report.to_phase.value}: "
            f"{report.recorded_reason}",
            label="/advance-force",
        )

    @app_commands.command(
        name="advance-unlock",
        description="Commissioner: clear a stuck advance claim (only once it is old enough).",
    )
    async def advance_unlock(self, interaction: discord.Interaction) -> None:
        authorized = await self._authorize(
            interaction, "advance_unlock", "unlock the advance claim"
        )
        if authorized is None:
            return
        pool, league = authorized

        await interaction.response.defer(ephemeral=True)
        try:
            result = await season_service.unlock(
                pool, league_id=league.id, actor_user_id=interaction.user.id
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/advance-unlock")
            return
        await reply_ephemeral(interaction, result.message)

    @app_commands.command(
        name="advance-postpone-game",
        description="Commissioner: take a game that cannot sim out of the schedule (audited).",
    )
    @app_commands.describe(game_id="The game's id", reason="Why (audited)")
    async def advance_postpone_game(
        self, interaction: discord.Interaction, game_id: int, reason: str
    ) -> None:
        authorized = await self._authorize(interaction, "advance_postpone_game", "postpone a game")
        if authorized is None:
            return
        pool, league = authorized

        await interaction.response.defer(ephemeral=True)
        try:
            change = await season_service.postpone_game(
                pool,
                league_id=league.id,
                actor_user_id=interaction.user.id,
                game_id=game_id,
                reason=reason,
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/advance-postpone-game")
            return
        if not change.changed:
            await reply_ephemeral(
                interaction,
                f"Game {change.game_id} is no longer {change.from_status.value}: somebody else "
                "already moved it, so nothing changed.",
            )
            return
        await reply_ephemeral(
            interaction,
            f"Game {change.game_id} is now postponed (it was {change.from_status.value}): it is "
            "out of the schedule until /advance-restore-game puts it back. Recorded as "
            f"transaction {change.transaction_id}.",
        )

    @app_commands.command(
        name="advance-restore-game",
        description="Commissioner: put a postponed game back on the schedule (audited).",
    )
    @app_commands.describe(game_id="The game's id", reason="Why (audited)")
    async def advance_restore_game(
        self, interaction: discord.Interaction, game_id: int, reason: str
    ) -> None:
        authorized = await self._authorize(interaction, "advance_restore_game", "restore a game")
        if authorized is None:
            return
        pool, league = authorized

        await interaction.response.defer(ephemeral=True)
        try:
            change = await season_service.restore_game(
                pool,
                league_id=league.id,
                actor_user_id=interaction.user.id,
                game_id=game_id,
                reason=reason,
            )
        except Exception as exc:
            # The service's three refusals (P5-99.u.1: wrong phase, the season's stats
            # already rebuilt, the league played past the date) are each a `PhaseError`
            # with its own sentence, shown verbatim -- the wording lives where the rule does.
            await reply_command_failure(interaction, exc, log_label="/advance-restore-game")
            return
        if not change.changed:
            await reply_ephemeral(
                interaction,
                f"Game {change.game_id} is no longer {change.from_status.value}: somebody else "
                "already moved it, so nothing changed.",
            )
            return
        await reply_ephemeral(
            interaction,
            f"Game {change.game_id} is scheduled again (it was {change.from_status.value}): the "
            "next /advance that reaches its date plays it. Recorded as transaction "
            f"{change.transaction_id}.",
        )

    # --- /advance-run and /advance-run-stop (docs/design/advance-run.md) ---------------------

    @app_commands.command(
        name="advance-run",
        description="Commissioner: keep pressing /advance until the league reaches a phase.",
    )
    @app_commands.describe(phase="The phase to run to (the run stops on entering it)")
    @app_commands.choices(phase=_ADVANCE_TO_CHOICES)
    async def advance_run(self, interaction: discord.Interaction, phase: str) -> None:
        authorized = await self._authorize(interaction, "advance_run", "run the season")
        if authorized is None:
            return
        pool, league = authorized
        to_phase = _parse_phase(phase)
        if to_phase is None:
            await reply_ephemeral(interaction, f"There is no phase called {phase!r}.")
            return
        try:
            route = season_run.plan_route(league.current_phase, to_phase)
        except PhaseError as exc:
            await reply_ephemeral(interaction, str(exc))
            return
        if not route:
            await reply_ephemeral(interaction, f"The league is already in {to_phase.value}.")
            return

        # `begin` is check-and-insert with no `await` between them: the whole single-run guard
        # (AR-4). It sits BEFORE the defer so a refused run costs nothing, and the `finally`
        # below frees the slot whatever the run does, including a failed defer.
        run = self.bot.runs.begin(
            league_id=league.id,
            user_id=interaction.user.id,
            started_at=_utcnow(),
            phase=league.current_phase,
        )
        if run is None:
            await reply_ephemeral(interaction, _run_held_message(self.bot.runs.current(league.id)))
            return
        try:
            await interaction.response.defer(ephemeral=True)
            await self._run_the_league(interaction, pool, league, to_phase, run)
        finally:
            self.bot.runs.end(run)

    @app_commands.command(
        name="advance-run-stop",
        description="Commissioner: stop the running /advance-run once its current press ends.",
    )
    async def advance_run_stop(self, interaction: discord.Interaction) -> None:
        authorized = await self._authorize(interaction, "advance_run_stop", "stop the run")
        if authorized is None:
            return
        _pool, league = authorized
        run = self.bot.runs.request_stop(league.id)
        if run is None:
            await reply_ephemeral(
                interaction,
                "No /advance-run is going for this league (in this bot process), so there is "
                "nothing to stop.",
            )
            return
        await reply_ephemeral(
            interaction,
            "Stop requested: the press in flight finishes and commits, then the run ends with "
            "its summary. A press is never interrupted.",
        )

    async def _run_the_league(
        self,
        interaction: discord.Interaction,
        pool: asyncpg.Pool,
        league: League,
        to_phase: Phase,
        run: LeagueRun,
    ) -> None:
        """One run, after the defer: close what an earlier `/advance` left open, loop the
        presses, post the record once, and reply with the summary.

        **`open_block` is the one call this never makes** (AR-7): a per-block slate is ~40x
        slower than the sim. What a run does post is the draft announcement and on-the-clock
        ping after each press that has one (row 5 stops the run *because* a human is on the
        clock, and the ping is how that human finds out), the wire stories, ONE standings sync
        and ONE score-free digest carrying the summary."""
        ledger = _RunLedger(started=_monotonic())
        progress = _ProgressEditor(interaction, interval=_RUN_PROGRESS_INTERVAL_SECONDS)

        # A block an earlier `/advance` left open would otherwise hang unrevealed behind the
        # run; games still broadcasting are left to their own `on_ended` (`close_block`).
        closed = await run_after_commit(
            lambda: game_feed.close_block(self.bot, pool, league_id=league.id),
            log_context=f"closing league {league.id}'s open #games block before a run",
        )
        if closed is None:
            ledger.notices.append(
                "The #games block an earlier /advance left open could not be closed (error "
                "logged): its scores stay hidden until the next block opens or the bot restarts."
            )
        else:
            # `/advance`'s slate path checks `slate.failed` the same way: a reveal Discord
            # refused leaves a claimed-not-posted row that the startup sweep completes.
            unrevealed = sum(1 for o in closed.outcomes.values() if o in _FAILED_REVEALS)
            if unrevealed:
                ledger.add_notice(
                    f"{unrevealed} score(s) of the #games block an earlier /advance left open "
                    "did not post (error logged): the startup sweep completes them."
                )

        async def on_step(step: season_run.RunStep) -> None:
            # The press has committed, so a bug in a post-press step must cost a notice, never
            # the run: every Discord call inside is already wrapped, this is the backstop.
            try:
                await self._after_run_press(interaction, pool, league, step, ledger, run, progress)
            except Exception:
                log.exception("/advance-run: the post-press steps of press %d failed", step.number)
                ledger.add_notice("A post-press step failed (error logged); the press committed.")

        try:
            report = await season_run.run_to(
                pool,
                league_id=league.id,
                actor_user_id=interaction.user.id,
                to_phase=to_phase,
                library=await _media_library(),
                caps=season_run.RunCaps(deadline=interaction.created_at + _RUN_DEADLINE),
                should_continue=lambda: not run.cancel.is_set(),
                on_step=on_step,
            )
        except Exception as exc:
            # Before any press (no league, not the commissioner, no route): nothing to record.
            await progress.finish()
            await reply_command_failure(interaction, exc, log_label="/advance-run")
            return

        # Every step below is wrapped on its own: the presses have committed, so a bug in one
        # record step must cost that step, never the others' record of the run.
        try:
            await self._post_the_run_record(interaction, pool, league, report, ledger)
        except Exception:
            log.exception("/advance-run: league %s's end-of-run standings sync failed", league.id)
            ledger.add_notice("The standings message could not be updated (error logged).")
        try:
            summary = await self._run_summary(pool, report, ledger)
        except Exception:
            log.exception("/advance-run: league %s's run summary could not be built", league.id)
            ledger.add_notice(
                "The run's detailed summary could not be built (error logged); the presses "
                "committed and /standings shows the result."
            )
            summary = _bare_run_summary(report, ledger)
        # The digest is the durable copy (R5): owed whenever any press changed something the
        # league keeps -- a pointer move, a game, a draft pick, a media cycle -- so a run that
        # only made AI draft picks still leaves a record. A run whose presses changed nothing
        # has only the panel.
        digest_owed = any(season_run.made_progress(s.report) for s in report.steps)
        if digest_owed:
            sent = await run_after_commit(
                lambda: self._send_digest(pool, league.id, build_run_digest(summary)),
                log_context=f"posting league {league.id}'s run digest to #games",
            )
            if sent is None:
                ledger.notices.append(
                    "The #games digest could not be posted (error logged): the games are "
                    "simulated and final, and /standings and /box show them."
                )
                summary = replace(summary, notices=tuple(ledger.notices))
        try:
            await progress.finish()
        except Exception:
            log.exception("/advance-run: league %s's progress line could not be closed", league.id)
        try:
            await self._send_panel(
                interaction,
                lambda: build_run_panel(summary),
                _run_plain_text(summary),
                label="/advance-run",
            )
        except Exception:
            log.exception("/advance-run: league %s's summary panel could not be sent", league.id)

    async def _after_run_press(
        self,
        interaction: discord.Interaction,
        pool: asyncpg.Pool,
        league: League,
        step: season_run.RunStep,
        ledger: _RunLedger,
        run: LeagueRun,
        progress: _ProgressEditor,
    ) -> None:
        """What a run does after each committed press: the kept `draft_feed` and wire calls
        (each wrapped on its own, never raising -- the press has committed), the mid-run
        standings sync every `_STANDINGS_SYNC_EVERY`th game-carrying press (SV-1, below), and
        the progress line. No slate: that stays out of a run entirely."""
        report = step.report
        entered = report.kind is PressKind.ENTRY and report.to_phase is not None
        run.presses = step.number
        run.phase = report.to_phase if entered and report.to_phase else report.from_phase
        if report.block is not None:
            ledger.game_days.add(report.block.block_id)
            ledger.games += len(report.block.games)
            ledger.last_game_date = max(ledger.last_game_date or "", report.block.block_id)

        # `human_pick=None`: no person picked in a press. Kept exactly as `/advance` posts it.
        for hook in (report.hook_result, report.resumed_hook_result):
            if hook is None or hook.draft_cycle is None:
                continue
            announced = await run_after_commit(
                lambda hook=hook: draft_feed.notify_on_the_clock(
                    self.bot,
                    pool,
                    league_id=league.id,
                    season=hook.season,
                    outcome=hook.draft_cycle,
                    human_pick=None,
                ),
                log_context=f"announcing league {league.id}'s draft cycle",
            )
            if announced is None or not announced.complete:
                ledger.add_notice(
                    "A draft announcement did not fully post -- check #games and the team's "
                    "GM channel."
                )

        wants_wire = report.media_cycle_index is not None
        if (report.block is not None and report.block.games) or wants_wire:
            ledger.standings_owed = True
        # SV-1: a SEPARATE, independent counter, alongside the untouched flag above -- never
        # in place of it. Deliberately excludes `wants_wire`: a media-cycle-only press moves
        # no team's record, so it must not count toward a cadence that exists to catch
        # board-moving presses.
        if report.block is not None and report.block.games:
            ledger.presses_since_standings_sync += 1
        guild = interaction.guild
        if wants_wire:
            if guild is None:
                ledger.add_notice(
                    "The wire and the standings could not be updated: this server is not "
                    "available to the bot."
                )
            else:
                assert report.media_cycle_index is not None
                ai_notice = await _rewrite_wire_with_ai(pool, league, report.media_cycle_index)
                if ai_notice is not None:
                    ledger.add_notice(ai_notice)
                posted = await run_after_commit(
                    lambda: surfaces.post_new_wire_stories(
                        guild, pool, league_id=league.id, season=league.current_season
                    ),
                    log_context=f"posting league {league.id}'s wire stories",
                )
                if posted is None:
                    ledger.add_notice(
                        "A wire post failed (error logged): the stories are saved and post on "
                        "the next press that finishes a media cycle, or at startup."
                    )
                else:
                    ledger.wire_stories += posted

        # SV-1: the mid-run sync, always after the wire post above, within this same press.
        if ledger.presses_since_standings_sync >= _STANDINGS_SYNC_EVERY:
            # Resets whether or not the sync below succeeds -- a failing sync does not retry
            # on the very next press (that would turn one bad edit into a hammering loop for
            # the rest of the run); it gets one more chance at the run's end instead.
            ledger.presses_since_standings_sync = 0
            if guild is not None:
                synced = await run_after_commit(
                    lambda: self._sync_standings(guild, pool, league.id, report.cycle_ordinal),
                    log_context=f"mid-run standings sync for league {league.id}",
                )
                if synced is None:
                    ledger.add_notice(
                        "The standings message could not be updated mid-run (error "
                        "logged); it will be retried at the end of the run."
                    )
                else:
                    # Only on SUCCESS: the board is current as of this press, so the
                    # end-of-run fallback should not spend one more Discord edit on content
                    # it already posted. On failure, `standings_owed` is left exactly as the
                    # untouched line above set it -- the end-of-run fallback is the retry.
                    ledger.standings_owed = False
            # `guild is None` skips the attempt but still resets the counter above: the guild
            # does not become available mid-run in any known path, so checking every
            # subsequent press would be dead code; the end-of-run fallback has its own
            # `guild is None` early return and stays a correct no-op if this ever changes.

        await progress(
            build_run_progress_line(
                phase=run.phase,
                game_days=len(ledger.game_days),
                games=ledger.games,
                last_game_date=ledger.last_game_date,
                presses=step.number,
                elapsed_seconds=_monotonic() - ledger.started,
            ),
            force=entered,
        )

    async def _post_the_run_record(
        self,
        interaction: discord.Interaction,
        pool: asyncpg.Pool,
        league: League,
        report: season_run.RunReport,
        ledger: _RunLedger,
    ) -> None:
        """The run's end-of-run standings FALLBACK (AR-7, amended by SV-1): most of a run's
        standings edits now happen mid-run, every `_STANDINGS_SYNC_EVERY`th game-carrying
        press (`_after_run_press`, above); this is the one that covers whatever partial
        stretch is left after the last mid-run sync, or the whole run if it never reached
        one. Never raises."""
        guild = interaction.guild
        if not ledger.standings_owed or guild is None:
            return
        last = report.last_report
        synced = await run_after_commit(
            lambda: self._sync_standings(
                guild, pool, league.id, last.cycle_ordinal if last is not None else None
            ),
            log_context=f"syncing league {league.id}'s standings message after a run",
        )
        if synced is None:
            ledger.add_notice("The standings message could not be updated (error logged).")

    async def _send_digest(self, pool: asyncpg.Pool, league_id: int, text: str) -> bool:
        """The run's one durable `#games` message. `send_line`, never `reveal_game`: the digest
        carries no score, and `reveal_game` stays the only publisher of one."""
        channel = await game_feed.games_channel(self.bot, pool, league_id)
        await game_feed.send_line(channel, text)
        return True

    async def _run_summary(
        self, pool: asyncpg.Pool, report: season_run.RunReport, ledger: _RunLedger
    ) -> RunSummary:
        """`RunReport` -> plain values for the presentation layer. What needs the database
        (a team's code) is looked up here, with a fallback: it is decoration on a stop."""
        stop = _STOP_KIND_BY_RUN_STOP[report.stop]
        last = report.last_report
        blockers: tuple[str, ...] = ()
        if last is not None and report.stop in (
            season_run.RunStop.BLOCKED,
            season_run.RunStop.NEEDS_A_DRAFT_BOARD,
            season_run.RunStop.SEASON_OVER,
        ):
            blockers = tuple(b.message for b in last.verdict.blockers)
        on_the_clock = None
        stall_reason = None
        cycle = report.draft_stop
        if cycle is not None and report.stop is season_run.RunStop.NEEDS_A_HUMAN_PICK:
            on_the_clock = await _on_the_clock_label(pool, cycle)
        if cycle is not None and report.stop is season_run.RunStop.DRAFT_STALLED:
            stall_reason = cycle.reason.value if cycle.reason is not None else None
        error_message = None
        if report.error is not None:
            error_message = (
                str(report.error)
                if isinstance(report.error, KNOWN_DOMAIN_ERRORS)
                else GENERIC_FAILURE_MESSAGE
            )
        return RunSummary(
            stop=stop,
            from_phase=report.from_phase,
            target=report.target,
            final_phase=report.final_phase,
            presses=report.presses,
            elapsed_seconds=report.elapsed_seconds,
            phases_entered=report.phases_entered,
            presses_by_kind=tuple((kind.value, n) for kind, n in report.presses_by_kind.items()),
            game_days=report.game_days,
            games=report.games,
            first_game_date=report.first_game_date,
            last_game_date=report.last_game_date,
            ai_counts=tuple(
                PhaseAiCounts(
                    phase=phase,
                    trades=c.trades,
                    signings=c.signings,
                    waiver_cuts=c.waiver_cuts,
                    resign_offers=c.resign_offers,
                )
                for phase, c in report.ai_counts_by_phase().items()
            ),
            expired_offers=report.expired_offers,
            media_cycles=len(report.media_cycles),
            wire_stories=ledger.wire_stories,
            failed_games=tuple(f"game {i}: {cause}" for i, cause in report.failed_games),
            blockers=blockers,
            on_the_clock=on_the_clock,
            stall_reason=stall_reason,
            error_message=error_message,
            notices=tuple(ledger.notices),
        )



def _plain_text(summary: PressSummary) -> str:
    parts: list[str] = [f"/advance from {summary.from_phase.value}: {summary.outcome.value}."]
    parts.extend(summary.done)
    parts.extend(summary.gate_lines)
    parts.extend(summary.notices)
    return " ".join(parts)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdvanceCommands(bot))
