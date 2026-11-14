"""`/draft code:<TEAM> player_id:<int>` -- one human draft pick, and the AI
draft resumed behind it (docs/design/phase-5-stage-d-prime-draft.md,
Decisions P5-98.d/f/g/h/k).

**Everything this cog decides is wording.** The pick itself, who may make
it, whether the team is even on the clock, and whether the AI may run on
afterwards are all `gm_command_service.draft_pick`'s -- this file resolves
the league and the team, applies Decision P5-38's bot-layer half (an
immediate ephemeral rejection naming why, never the only gate), defers, and
turns one `DraftPickOutcome` into one sentence.

**Every reply this command makes is ephemeral, and the draft's public
record is the `#games` announcement** (round-1 critic's M1, a DEVIATION
from the design's "a pick that lands is a PUBLIC reply"). A refusal was
always ephemeral, because a refusal is nobody else's business -- but a
followup after a PUBLIC `defer()` is itself public, so the old public
defer left a public "thinking..." placeholder in front of every ephemeral
refusal, which nothing ever replaced. Deferring ephemerally fixes that and
costs nothing the league can see: `draft_feed` already posts every pick to
`#games` exactly once, which is the record a league actually reads.
`cogs/offers.py` and `cogs/commissioner.py` defer the same way.

`SKIPPED` is **not** an error: the pick went through and only the AI's
resume did not, so it is a success reply that says so -- a commissioner's
press claimed the league, the phase moved, a lock was held, or the resume
failed outright, and the next `/draft` (or Stage E's `/advance`) carries
the draft on from there.

**`defer()` comes first, exactly as every write command in `cogs/roster.py`
does** (`:207`): the pick plus the resume is comfortably past Discord's
three-second initial-response budget, and the AI pass runs inline on the
gateway event loop (§7 L7 measures it at ~0.25s of pure work for a 60-pick
draft; the decision about whether that ever needs a worker is Stage E's).

**Nothing after the service call may raise.** The name lookup, the
"who is next" sentence, the announcement and now the reply itself
(`_reply_with_the_pick`) all run after the pick has committed, so each is
wrapped and each degrades honestly instead of telling a person their pick
failed.

**The autocomplete is the codebase's first, and it is not authoritative.**
It offers the available prospects through `view_service.get_draft_board`
and nothing else -- no SQL and no repo read of its own -- and a person who
types a raw player id gets the identical service-layer treatment. Its whole
body is wrapped: Discord gives the callback about three seconds and an
autocomplete that raises produces a silent, unexplained empty dropdown
either way, so `[]` is made the explicit path rather than the accidental
one. It shows name, position and mock rank only, never a rating (§7 L11:
the AI does not see true ratings either).

**Nothing here has run against a live gateway** (§6 risk 5), and after
D'-25 nothing in the bot can START a draft either -- no cog calls
`run_phase_hook`, so `/draft` is fully built and tested but not usable in a
guild until Stage E's `/advance` exists (§7 L8), on top of U1's empty
production draft class.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import player_repo, team_repo
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.draft import build_pick_panel, clip_to_message_limit
from dba2.services import gm_command_service, view_service
from dba2.services.gm_service import DraftCycleOutcome, DraftCycleReason, DraftCycleState

from .. import draft_feed
from ..context import NoLeagueForGuild, resolve_league
from ..embeds import panel_to_embed
from ..gates import CommandNotLegalNow, ensure_can_act_for_team, ensure_command_legal
from ..interaction_errors import reply_command_failure, reply_ephemeral
from ..post_commit import honest_suffix, run_after_commit

log = get_logger(__name__)

_PICK_MADE = "{code} select {player} -- round {round}, pick {pick_number}."
_ON_THE_CLOCK = " {code} are on the clock -- round {round}, pick {pick_number}."
_DRAFT_COMPLETE = " That was the last pick -- every pick in this draft has now been made."
_POOL_EXHAUSTED = (
    " No draft-eligible prospects remain -- the commissioner needs to add a draft class."
)
_UNRESOLVED_PICKS = (
    " Some picks have no slot yet -- the lottery hasn't finished resolving this draft."
)
_CLAIM_HELD = " The commissioner is advancing the league, so the draft will continue from there."
_PHASE_MOVED = " The league has left the draft, so nothing ran on from here."
# The honest sentences for a resume that did not finish (round-1 critic's
# B2). Each says the same three things, because all three matter to the
# person reading them: the pick stands, the AI has NOT continued, and here
# is what makes it continue. That last part names the COMMISSIONER and not
# `/draft` (round-2 critic's minor 3): the next pick after a stopped resume
# usually belongs to an AI team, and running `/draft` again is something
# the person reading this cannot do about it.
_RESUME_CONTENDED = (
    " Your pick stands, but another draft action held the board, so the AI hasn't continued "
    "-- ask the commissioner to continue the draft."
)
_RESUME_FAILED = (
    " Your pick stands, but the AI couldn't continue (the error is logged) "
    "-- ask the commissioner to continue the draft."
)
_RESUME_STOPPED = (
    " Your pick stands, but the AI hasn't continued -- ask the commissioner to continue the draft."
)
"""The generic `SKIPPED` line (round-2 critic's minor 2). The fallback used
to be `_CLAIM_HELD`, which is a specific claim about a specific situation
(a press is running right now) -- so a reason added service-side and not
worded here would have told a person something that was simply untrue."""
_AI_PICKS = " The AI made {n} pick{s} behind you."
_LOOKUP_FAILURE_SUFFIX = honest_suffix("a name couldn't be looked up")
_ANNOUNCE_FAILURE_SUFFIX = honest_suffix(
    "the draft announcement couldn't be posted in full -- check #games and the team's GM channel"
)

_MAX_CHOICE_NAME = 100
"""Discord's own limit on an `app_commands.Choice` name."""


def _choice_label(prospect) -> str:
    rank = f"#{prospect.mock_rank} " if prospect.mock_rank is not None else ""
    label = f"{rank}{prospect.first_name} {prospect.last_name} ({prospect.position.value.upper()})"
    return label[:_MAX_CHOICE_NAME]


class DraftCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="draft", description="Make your team's draft pick.")
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        player_id="The prospect's internal id (start typing a name to pick from the board)",
    )
    async def draft(self, interaction: discord.Interaction, code: str, player_id: int) -> None:
        pool = await get_pool()
        try:
            # Fast, synchronous-shaped rejections BEFORE defer(), matching
            # `cogs/roster.py`'s own convention: the phase gate, the team,
            # and P5-38's bot-layer permission half.
            league = await resolve_league(pool, interaction)
            ensure_command_legal("draft", league.current_phase)
            team = await team_repo.get_team_by_code(pool, league.id, code.upper())
            if team is None:
                raise NotFoundError(f"No team with code {code!r} in this league.")
            ensure_can_act_for_team(interaction.user.id, league, team, action="draft")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        # Ephemeral from the defer, not just at the reply (round-1 critic's
        # M1): a followup after a PUBLIC defer is itself public, so a public
        # defer followed by an ephemeral refusal leaves a public "thinking
        # ..." placeholder that nothing ever replaces. `cogs/offers.py` and
        # `cogs/commissioner.py` both defer ephemerally for the same reason.
        await interaction.response.defer(ephemeral=True)
        try:
            outcome = await gm_command_service.draft_pick(
                pool,
                league_id=league.id,
                team_id=team.id,
                prospect_id=player_id,
                actor_user_id=interaction.user.id,
            )
        except Exception as exc:
            # Wide for its reason (`cogs/roster.py`'s own): a DB-layer
            # failure is no domain error and, uncaught, means "deferred
            # forever" for the person who ran the command.
            await reply_command_failure(interaction, exc, log_label="/draft")
            return

        # The pick is committed from here down. Everything below is a
        # never-raising follow-up (`post_commit.run_after_commit`): a
        # failure degrades a NAME to an id, or loses an announcement, and
        # says so -- it never claims the pick did not happen.
        player_name = await run_after_commit(
            lambda: self._prospect_name(pool, outcome.prospect_id),
            log_context=f"get_player for prospect {outcome.prospect_id}",
        )
        next_up, looked_up = await self._cycle_text(pool, outcome.cycle)
        # The announcement runs BEFORE this reply, because the ping inside
        # it is the only thing that reaches the next person and the reply
        # has to be able to disclose whether it got through.
        announced = await run_after_commit(
            lambda: draft_feed.notify_on_the_clock(
                self.bot,
                pool,
                league_id=league.id,
                # The PICK's season, not `league.current_season` (round-1
                # critic's minor 8): the league object above is an unlocked
                # pre-defer read, while this row was written under the
                # league gate at `gate.season`. When they disagree the board
                # read that supplies round labels finds nothing, and every
                # AI line in the announcement silently loses its round.
                season=outcome.pick.season,
                outcome=outcome.cycle,
                human_pick=draft_feed.AnnouncedPick(
                    team_id=team.id,
                    player_id=outcome.prospect_id,
                    pick_number=outcome.pick.pick_number,
                    round=outcome.pick.round,
                    by_manager=True,
                ),
            ),
            log_context=f"announcing league {league.id}'s draft pick",
        )

        shown_name = player_name or f"player {outcome.prospect_id}"
        content = _PICK_MADE.format(
            code=team.code,
            player=shown_name,
            round=outcome.pick.round,
            pick_number=outcome.pick.pick_number,
        )
        made = 0 if outcome.cycle.draft is None else len(outcome.cycle.draft.picks)
        if made:
            content += _AI_PICKS.format(n=made, s="" if made == 1 else "s")
        content += next_up
        if player_name is None or not looked_up:
            content += _LOOKUP_FAILURE_SUFFIX
        if announced is None or not announced.complete:
            content += _ANNOUNCE_FAILURE_SUFFIX
        content = clip_to_message_limit(content)
        await self._reply_with_the_pick(
            interaction,
            content=content,
            team_code=team.code,
            player_name=shown_name,
            round=outcome.pick.round,
            pick_number=outcome.pick.pick_number,
            next_up=next_up.strip(),
        )

    async def _reply_with_the_pick(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        team_code: str,
        player_name: str,
        round: int,
        pick_number: int,
        next_up: str,
    ) -> None:
        """The confirmation, and the last thing in this command that can
        fail (round-1 critic's blocking issue B3).

        **It is wrapped because it runs after the pick has committed, and
        everything else after that commit already is.** `build_pick_panel`
        validates against Discord's embed limits and RAISES over them, and
        `followup.send` is a network call -- both sat outside every
        never-raising wrapper this cog has, so a 300-character player name
        (reproduced live) cost the person their entire reply for a pick that
        had landed. The panel now clips, and building it here -- never
        inside the send -- is what covers the overflow nobody has thought of
        yet: the embed is simply dropped and the same sentence goes out as
        plain text.

        **Exactly ONE send is attempted, ever** (round-2 critic's minor 1).
        The fallback used to fire on any exception from the first attempt,
        including one raised by `followup.send` itself -- and a `send` that
        reaches Discord and then times out waiting for the response has
        already posted the message, so the person got their confirmation
        twice. A send that raises is therefore logged and left there: the
        message may or may not have arrived, and posting a second copy to
        find out is the worse of the two answers."""
        embed = None
        try:
            panel = build_pick_panel(
                team_code=team_code,
                player_name=player_name,
                round=round,
                pick_number=pick_number,
                next_up=next_up,
            )
            embed = panel_to_embed(panel)
        except Exception:
            log.exception(
                "/draft: the confirmation panel could not be built for interaction %s "
                "-- sending the reply as plain text; the pick itself committed",
                interaction.id,
            )
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
                "/draft: the confirmation could not be sent for interaction %s -- it may "
                "or may not have reached Discord, so it is NOT retried; the pick itself "
                "committed",
                interaction.id,
            )

    async def _cycle_text(self, pool, cycle: DraftCycleOutcome) -> tuple[str, bool]:
        """The sentence for where the draft now stands, and whether every
        name it wanted was resolvable. `WAITING_ON_HUMAN` is the only state
        that needs a second read, and the one whose whole point is telling
        the next person to go."""
        if cycle.state is DraftCycleState.WAITING_ON_HUMAN:
            team = await run_after_commit(
                lambda: team_repo.get_team(pool, cycle.team_id),
                log_context=f"get_team for the team on the clock ({cycle.team_id})",
            )
            return (
                _ON_THE_CLOCK.format(
                    code=team.code if team is not None else f"team {cycle.team_id}",
                    round=cycle.round,
                    pick_number=cycle.pick_number,
                ),
                team is not None,
            )
        if cycle.state is DraftCycleState.COMPLETE:
            return _DRAFT_COMPLETE, True
        if cycle.state is DraftCycleState.STALLED:
            if cycle.reason is DraftCycleReason.UNRESOLVED_PICKS:
                return _UNRESOLVED_PICKS, True
            return _POOL_EXHAUSTED, True
        # SKIPPED, by reason -- one sentence each, and a reason with no
        # sentence of its own gets the GENERIC one (round-2 critic's minor
        # 2). It used to get `_CLAIM_HELD`, which asserts that a press is
        # running right now: true for that reason and false for any new one
        # a future service change forgets to word here.
        if cycle.reason is DraftCycleReason.CLAIM_HELD:
            return _CLAIM_HELD, True
        if cycle.reason is DraftCycleReason.PHASE_MOVED:
            return _PHASE_MOVED, True
        if cycle.reason is DraftCycleReason.RESUME_CONTENDED:
            return _RESUME_CONTENDED, True
        if cycle.reason is DraftCycleReason.RESUME_FAILED:
            return _RESUME_FAILED, True
        return _RESUME_STOPPED, True

    @staticmethod
    async def _prospect_name(pool, player_id: int) -> str | None:
        """A non-`None` return means the lookup ran; `run_after_commit`'s own
        `None` then unambiguously means it failed. A prospect who is genuinely
        missing is impossible here -- the pick that just committed named him --
        so there is no third case to separate out the way `/set-role` has to."""
        player = await player_repo.get_player(pool, player_id)
        return None if player is None else f"{player.first_name} {player.last_name}"

    @draft.autocomplete("player_id")
    async def draft_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """The board, filtered by whatever has been typed so far. Decides
        nothing, and cannot fail loudly: see this module's docstring."""
        try:
            pool = await get_pool()
            league = await resolve_league(pool, interaction)
            prospects = await view_service.get_draft_board(
                pool, league_id=league.id, season=league.current_season, query=current, limit=25
            )
        except NoLeagueForGuild:
            log.debug("/draft autocomplete: no league for guild %s", interaction.guild_id)
            return []
        except Exception:
            log.debug("/draft autocomplete failed", exc_info=True)
            return []
        return [app_commands.Choice(name=_choice_label(p), value=p.player_id) for p in prospects]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DraftCommands(bot))
