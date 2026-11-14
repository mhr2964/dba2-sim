"""`dba2/bot/cogs/advance.py` -- `/advance`, with no database and no gateway
(docs/design/phase-5-stage-e-advance.md P5-99.a, P5-99.j, P5-99.m, build-plan commit 14).

`season_service.press` and every Discord-side call the cog makes after it are replaced
by recorders, so what is asserted is exactly the cog's own job: defer ephemerally, hand
the press the right arguments (the cached media library, a progress callback), map each
press kind to one reply, map each service error to one sentence, and -- after the press
has committed -- make the Discord calls in the fixed order the spec gives (the scoreless
slate first, then the draft ping, the wire, the standings), each independently wrapped so
one failing loses none of the others.

**Nothing here has run against a live Discord gateway.** These are in-process fakes that
record what was sent; the service side of every case is proved against real Postgres in
`tests/integration/test_season_service_*.py`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import discord
import pytest

from dba2.bot import client as bot_client
from dba2.bot import draft_feed, game_feed
from dba2.bot.block_registry import BlockGame
from dba2.bot.cogs import advance as cog_mod
from dba2.bot.gates import CommandNotLegalNow
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.core.errors import ConcurrencyError, PhaseError, ValidationError
from dba2.domain.phase import Phase
from dba2.phase.permissions import PermissionDenied
from dba2.services import season_service
from dba2.services.agency_service import CheckpointResult
from dba2.services.game_sim_service import GameFailure, SimmedGame
from dba2.services.gm_service import DraftCycleOutcome, DraftCycleState, PhaseHookResult
from dba2.services.season_service import (
    Blocker,
    BlockerCode,
    ExitVerdict,
    PressKind,
    PressReport,
    SimmedBlock,
)

_COMMISSIONER = 1
_OTHER = 42
_LEAGUE_ID = 7
_SEASON = 2026
_ALLOWED = ExitVerdict(allowed=True, blockers=())


class _Response:
    def __init__(self, log: list[str]) -> None:
        self._done = False
        self._log = log
        self.deferred: list[bool] = []
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        await asyncio.sleep(0)
        self._done = True
        self.deferred.append(ephemeral)
        self._log.append("defer")

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        await asyncio.sleep(0)
        self._done = True
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(
        self, content=None, *, embed=None, ephemeral: bool = False, allowed_mentions=None, **_kw
    ) -> None:
        await asyncio.sleep(0)
        self.sent.append(
            {
                "content": content,
                "embed": embed,
                "ephemeral": ephemeral,
                "allowed_mentions": allowed_mentions,
            }
        )


class _Interaction:
    def __init__(self, log: list[str], user_id: int = _COMMISSIONER) -> None:
        self.id = 1
        self.guild_id = 9
        self.guild = SimpleNamespace(id=9)
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response(log)
        self.followup = _Followup()
        self.edits: list[dict] = []
        self.edit_attempts = 0
        self.edit_raises: BaseException | None = None

    async def edit_original_response(
        self, *, content=None, embed=None, allowed_mentions=None, **_kw
    ) -> None:
        await asyncio.sleep(0)
        self.edit_attempts += 1
        if self.edit_raises is not None:
            raise self.edit_raises
        self.edits.append(
            {"content": content, "embed": embed, "allowed_mentions": allowed_mentions}
        )

    @property
    def reply(self) -> dict:
        """The single reply the person saw, wherever it was sent."""
        seen = self.response.messages + self.followup.sent
        assert len(seen) == 1, seen
        return seen[0]


def _team(team_id: int, code: str) -> SimpleNamespace:
    return SimpleNamespace(id=team_id, code=code, manager_user_id=None)


def _simmed(game_id: int, home: int, away: int, day: date) -> SimmedGame:
    return SimmedGame(
        game_id=game_id,
        game_date=day,
        home_team_id=home,
        away_team_id=away,
        result=SimpleNamespace(marker=f"result-{game_id}"),
    )


def _entry_report(**overrides) -> PressReport:
    base = PressReport(
        kind=PressKind.ENTRY,
        from_phase=Phase.PLAYIN,
        verdict=_ALLOWED,
        to_phase=Phase.PLAYOFFS_R1,
        entry_index=6,
    )
    return replace(base, **overrides)


def _block_report(**overrides) -> PressReport:
    day = date(2026, 10, 25)
    base = PressReport(
        kind=PressKind.BLOCK,
        from_phase=Phase.REGULAR_SEASON,
        verdict=ExitVerdict(
            allowed=False,
            blockers=(
                Blocker(
                    code=BlockerCode.GAMES_TARGET_NOT_REACHED,
                    message="4 game(s) this phase must play are not played yet.",
                    game_ids=(5, 6, 7, 8),
                ),
            ),
        ),
        entry_index=1,
        cycle_ordinal=1,
        block=SimmedBlock(
            block_id=day.isoformat(),
            season=_SEASON,
            games=(_simmed(11, 1, 2, day), _simmed(12, 3, 4, day)),
        ),
    )
    return replace(base, **overrides)


class _World:
    """The patched surroundings. `order` is one shared log across the interaction, the
    press and every Discord-side call, so the ORDER of those calls is assertable."""

    def __init__(self, monkeypatch, *, phase: Phase = Phase.PLAYIN) -> None:
        self.order: list[str] = []
        self.league = SimpleNamespace(
            id=_LEAGUE_ID,
            name="Test League",
            current_phase=phase,
            current_season=_SEASON,
            commissioner_user_id=_COMMISSIONER,
            hook_completed_cycle_index=2,
        )
        self.report: PressReport = _entry_report()
        self.press_raises: BaseException | None = None
        self.press_calls: list[dict] = []
        self.progress_messages_to_send: list[str] = []
        self.open_block_calls: list[dict] = []
        self.open_block_raises: BaseException | None = None
        self.open_block_report: object = SimpleNamespace(failed=(), posted=(), header_posted=True)
        self.notify_calls: list[dict] = []
        self.notify_raises: BaseException | None = None
        self.notify_report: object = SimpleNamespace(complete=True)
        self.wire_calls: list[dict] = []
        self.wire_raises: BaseException | None = None
        self.wire_posted = 2
        self.standings_calls: list[dict] = []
        self.standings_raises: BaseException | None = None
        self.after_press_league: object | None = None
        self.load_library_calls = 0
        self.library = object()
        self.teams = [_team(1, "AAA"), _team(2, "BBB"), _team(3, "CCC"), _team(4, "DDD")]

        async def get_pool():
            return "pool"

        async def resolve_league(pool, interaction):
            return self.league

        async def press(pool, **kwargs):
            self.order.append("press")
            self.press_calls.append({"pool": pool, **kwargs})
            on_progress = kwargs.get("on_progress")
            if on_progress is not None:
                for message in self.progress_messages_to_send:
                    await on_progress(message)
            if self.press_raises is not None:
                raise self.press_raises
            return self.report

        async def open_block(bot, ex, **kwargs):
            self.order.append("open_block")
            self.open_block_calls.append({"bot": bot, "ex": ex, **kwargs})
            if self.open_block_raises is not None:
                raise self.open_block_raises
            return self.open_block_report

        async def notify(bot, ex, **kwargs):
            self.order.append("notify")
            self.notify_calls.append({"bot": bot, "ex": ex, **kwargs})
            if self.notify_raises is not None:
                raise self.notify_raises
            return self.notify_report

        async def post_wire(guild, ex, **kwargs):
            self.order.append("wire")
            self.wire_calls.append({"guild": guild, "ex": ex, **kwargs})
            if self.wire_raises is not None:
                raise self.wire_raises
            return self.wire_posted

        async def sync_standings(guild, ex, **kwargs):
            self.order.append("standings")
            self.standings_calls.append({"guild": guild, "ex": ex, **kwargs})
            if self.standings_raises is not None:
                raise self.standings_raises
            return SimpleNamespace()

        async def list_teams(ex, league_id):
            return self.teams

        async def get_league(ex, league_id):
            return self.after_press_league if self.after_press_league is not None else self.league

        def load_library():
            self.load_library_calls += 1
            return self.library

        monkeypatch.setattr(cog_mod, "_LIBRARY", None)
        monkeypatch.setattr(cog_mod, "get_pool", get_pool)
        monkeypatch.setattr(cog_mod, "resolve_league", resolve_league)
        monkeypatch.setattr(season_service, "press", press)
        monkeypatch.setattr(game_feed, "open_block", open_block)
        monkeypatch.setattr(draft_feed, "notify_on_the_clock", notify)
        monkeypatch.setattr(cog_mod.surfaces, "post_new_wire_stories", post_wire)
        monkeypatch.setattr(cog_mod.surfaces, "sync_standings_message", sync_standings)
        monkeypatch.setattr(cog_mod.team_repo, "list_teams_for_league", list_teams)
        monkeypatch.setattr(
            cog_mod, "league_repo", SimpleNamespace(get_league=get_league), raising=False
        )
        monkeypatch.setattr(cog_mod.media_service, "load_media_library", load_library)

    def interaction(self, user_id: int = _COMMISSIONER) -> _Interaction:
        return _Interaction(self.order, user_id)


@pytest.fixture
def world(monkeypatch):
    return _World(monkeypatch)


@pytest.fixture
def bot():
    return SimpleNamespace(name="the-bot")


@pytest.fixture
def cog(bot):
    return cog_mod.AdvanceCommands(bot=bot)


async def _advance(cog, interaction) -> None:
    await cog.advance.callback(cog, interaction)


# --- registration ----------------------------------------------------------------


def test_the_cog_is_registered_in_the_bot_extensions():
    assert "dba2.bot.cogs.advance" in bot_client._EXTENSIONS


async def test_advance_is_a_registered_command_with_no_options_and_a_gate_entry():
    from dba2.phase.command_gates import COMMAND_GATES

    bot = bot_client.build_bot()
    await bot.setup_hook()
    commands_by_name = {c.name: c for c in bot.tree.get_commands()}
    assert "advance" in commands_by_name
    assert [p.name for p in commands_by_name["advance"].parameters] == []
    assert "advance" in COMMAND_GATES


# --- defer, permission, gate --------------------------------------------------------


async def test_the_defer_is_ephemeral_and_happens_before_the_press(world, cog):
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.response.deferred == [True]
    assert world.order.index("defer") < world.order.index("press")
    assert interaction.reply["ephemeral"] is True


async def test_a_person_who_is_not_the_commissioner_is_refused_before_defer(world, cog):
    interaction = world.interaction(user_id=_OTHER)

    await _advance(cog, interaction)

    assert world.press_calls == []
    assert interaction.response.deferred == []
    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    assert "commissioner" in reply["content"].lower()


async def test_a_command_gate_refusal_is_ephemeral_and_runs_nothing(world, cog, monkeypatch):
    def refuse(command, phase):
        raise CommandNotLegalNow(f"/{command} is not available during {phase.value}")

    monkeypatch.setattr(cog_mod, "ensure_command_legal", refuse)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.press_calls == []
    assert interaction.response.deferred == []
    assert interaction.response.messages == [
        {"content": "/advance is not available during PLAYIN", "ephemeral": True}
    ]


async def test_a_dm_or_unknown_guild_is_refused_with_the_resolver_sentence(world, cog, monkeypatch):
    from dba2.bot.context import NoLeagueForGuild

    async def no_league(pool, interaction):
        raise NoLeagueForGuild("no league is set up for this server (guild 9)")

    monkeypatch.setattr(cog_mod, "resolve_league", no_league)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.press_calls == []
    assert interaction.reply["content"] == "no league is set up for this server (guild 9)"


# --- error mapping -------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ConcurrencyError("An advance is already running for this league."),
        PhaseError("No game of this block could be simulated, so the press stopped."),
        PermissionDenied("Only the commissioner may advance."),
        ValidationError("a reason is required"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_a_known_service_error_is_shown_verbatim_and_ephemerally(world, cog, error):
    world.press_raises = error
    interaction = world.interaction()

    await _advance(cog, interaction)

    reply = interaction.reply
    assert reply["content"] == str(error)
    assert reply["ephemeral"] is True
    # A refused press committed nothing, so nothing is announced.
    assert world.order == ["defer", "press"]


async def test_an_unexpected_error_is_answered_generically_and_never_leaks_its_text(world, cog):
    world.press_raises = RuntimeError("connection reset by peer at 10.0.0.5")
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.reply["content"] == GENERIC_FAILURE_MESSAGE
    assert "10.0.0.5" not in interaction.reply["content"]
    assert world.order == ["defer", "press"]


# --- one reply per press kind ----------------------------------------------------------


async def test_an_entry_press_reply_names_the_phases_and_what_it_did(world, cog):
    world.report = _entry_report(
        expired_offers=2,
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID,
            season=_SEASON,
            phase=Phase.PLAYOFFS_R1,
            trade_window=SimpleNamespace(executions=(1, 2, 3)),
        ),
        checkpoint_result=CheckpointResult(
            players_evaluated=250, ladder_transitions=4, players_skipped=200
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Advanced to PLAYOFFS_R1"
    assert "**PLAYIN** -> **PLAYOFFS_R1**" in embed.description
    assert "Expired 2 stale trade offer(s)." in embed.description
    assert "3 trade(s)" in embed.description
    assert "250 player(s) evaluated" in embed.description
    assert "200" in embed.description, "a resumed checkpoint says how many were already done"
    assert interaction.reply["ephemeral"] is True


async def test_an_entry_press_whose_prefix_finished_an_earlier_hook_says_so(world, cog):
    world.report = _entry_report(
        resumed_hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID,
            season=_SEASON,
            phase=Phase.PLAYIN,
            trade_window=SimpleNamespace(executions=(1,)),
        )
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    description = interaction.reply["embed"].description
    assert "left unfinished" in description
    assert "1 trade(s)" in description


async def test_a_block_press_reply_reports_the_games_the_failures_and_the_gate(world, cog):
    world.league.current_phase = Phase.REGULAR_SEASON
    world.report = _block_report(
        failed_games=(13,),
        failures=(GameFailure(game_id=13, cause="missing team/strategy snapshot data"),),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Simulated a game day in REGULAR_SEASON"
    assert "Simulated 2 game(s) on 2026-10-25." in embed.description
    assert "game 13" in embed.description
    assert "missing team/strategy snapshot data" in embed.description
    assert "/advance-postpone-game" in embed.description
    (field,) = embed.fields
    assert field.name == "Before REGULAR_SEASON can be left"
    assert "4 game(s) this phase must play are not played yet." in field.value


async def test_a_blocked_press_reply_names_every_blocker_and_runs_no_discord_step(world, cog):
    world.report = PressReport(
        kind=PressKind.BLOCKED,
        from_phase=Phase.WAIVERS,
        verdict=ExitVerdict(
            allowed=False,
            blockers=(
                Blocker(code=BlockerCode.WAIVERS_EDGE, message="the Phase 7 message"),
                Blocker(code=BlockerCode.DRAFT_BOARD_NOT_READY, message="a second blocker"),
            ),
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Blocked in WAIVERS"
    (field,) = embed.fields
    assert field.name == "What is in the way"
    assert "- the Phase 7 message" in field.value and "- a second blocker" in field.value
    assert world.order == ["defer", "press"], "a blocked press opens no block and posts nothing"


async def test_a_draft_resume_press_reply_says_where_the_draft_stands(world, cog):
    world.report = PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=ExitVerdict(
            allowed=False,
            blockers=(
                Blocker(
                    code=BlockerCode.DRAFT_NOT_COMPLETE,
                    message="the draft is not finished: team 7 is on the clock.",
                ),
            ),
        ),
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID,
            season=_SEASON,
            phase=Phase.DRAFT,
            draft=SimpleNamespace(picks=(1, 2, 3, 4)),
            draft_cycle=DraftCycleOutcome(state=DraftCycleState.WAITING_ON_HUMAN, team_id=7),
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Draft resumed"
    assert "4 pick(s)" in embed.description
    assert "the draft is not finished: team 7 is on the clock." in embed.fields[0].value


# --- the block: open_block, the seven BlockGame fields ------------------------------------


async def test_open_block_is_called_on_the_pool_with_the_date_id_and_seven_fields(world, cog, bot):
    world.report = _block_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.open_block_calls
    assert call["bot"] is bot
    assert call["ex"] == "pool", "the pool, so no transaction can be open"
    assert call["league_id"] == _LEAGUE_ID
    assert call["season"] == _SEASON
    assert call["block_id"] == "2026-10-25" == date(2026, 10, 25).isoformat()
    day = date(2026, 10, 25)
    assert call["games"] == [
        BlockGame(
            game_id=11,
            home_team_id=1,
            away_team_id=2,
            home_code="AAA",
            away_code="BBB",
            game_date=day,
            result=world.report.block.games[0].result,
        ),
        BlockGame(
            game_id=12,
            home_team_id=3,
            away_team_id=4,
            home_code="CCC",
            away_code="DDD",
            game_date=day,
            result=world.report.block.games[1].result,
        ),
    ]
    assert all(isinstance(g, BlockGame) for g in call["games"])


async def test_a_press_that_simmed_nothing_opens_no_block(world, cog):
    world.report = _block_report(block=None)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.open_block_calls == []


async def test_a_game_that_failed_to_sim_is_not_in_the_slate(world, cog):
    """The slate is what `press` handed over: `SimmedBlock.games` carries one record per
    game that COMMITTED, never a position, so a failed game cannot appear in it."""
    day = date(2026, 10, 25)
    world.report = _block_report(
        block=SimmedBlock(
            block_id=day.isoformat(), season=_SEASON, games=(_simmed(11, 1, 2, day),)
        ),
        failed_games=(12,),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.open_block_calls
    assert [g.game_id for g in call["games"]] == [11]


# --- the draft ping ---------------------------------------------------------------------------


async def test_the_draft_announcement_is_sent_with_no_human_pick(world, cog, bot):
    outcome = DraftCycleOutcome(state=DraftCycleState.WAITING_ON_HUMAN, team_id=7)
    world.report = PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=_ALLOWED,
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID, season=_SEASON, phase=Phase.DRAFT, draft_cycle=outcome
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.notify_calls
    assert call["bot"] is bot
    assert call["ex"] == "pool"
    assert call["league_id"] == _LEAGUE_ID
    assert call["season"] == _SEASON
    assert call["outcome"] is outcome
    assert call["human_pick"] is None, "no person picked in a press"


async def test_an_entry_into_the_draft_announces_its_cycle_too(world, cog):
    outcome = DraftCycleOutcome(state=DraftCycleState.COMPLETE)
    world.report = _entry_report(
        from_phase=Phase.DRAFT_LOTTERY,
        to_phase=Phase.DRAFT,
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID, season=_SEASON, phase=Phase.DRAFT, draft_cycle=outcome
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert [c["outcome"] for c in world.notify_calls] == [outcome]


async def test_a_press_with_no_draft_cycle_announces_nothing(world, cog):
    world.report = _entry_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.notify_calls == []


async def test_an_incomplete_announcement_is_disclosed_in_the_reply(world, cog):
    world.notify_report = SimpleNamespace(complete=False)
    world.report = _entry_report(
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID,
            season=_SEASON,
            phase=Phase.DRAFT,
            draft_cycle=DraftCycleOutcome(state=DraftCycleState.COMPLETE),
        )
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Advanced to PLAYOFFS_R1", "the press committed either way"
    (field,) = embed.fields
    assert field.name == "Did not go out"
    assert "draft announcement" in field.value


# --- the wire and the standings --------------------------------------------------------------


async def test_the_wire_is_posted_from_what_is_unposted_after_a_media_cycle(world, cog):
    world.report = _block_report(media_cycle_index=1)
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.wire_calls
    assert call["guild"] is interaction.guild
    assert call["ex"] == "pool"
    assert call["league_id"] == _LEAGUE_ID
    assert call["season"] == _SEASON
    assert call.get("cycle_index") is None, (
        "the post is driven by what the database says is unposted, never by what the report "
        "says was written (P5-99.m.1)"
    )
    assert "Posted 2 wire stor" in interaction.reply["embed"].description


async def test_no_wire_post_is_attempted_when_no_media_cycle_finished(world, cog):
    world.report = _block_report(media_cycle_index=None)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.wire_calls == []


async def test_the_standings_are_synced_after_a_press_that_simmed_games(world, cog):
    world.report = _block_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.standings_calls
    assert call["guild"] is interaction.guild
    assert call["league_id"] == _LEAGUE_ID
    assert call["season"] == _SEASON
    assert call["cycle_index"] == 1


async def test_the_standings_are_synced_after_a_press_that_finished_a_cycle(world, cog):
    world.report = _entry_report(media_cycle_index=0, cycle_ordinal=0)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert len(world.standings_calls) == 1


async def test_the_standings_are_not_synced_when_nothing_was_simmed_or_closed(world, cog):
    world.report = _entry_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.standings_calls == []


# --- order, and independence --------------------------------------------------------------------


async def test_the_slate_posts_before_the_draft_ping_the_wire_and_the_standings(world, cog):
    world.report = _block_report(
        media_cycle_index=1,
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID,
            season=_SEASON,
            phase=Phase.DRAFT,
            draft_cycle=DraftCycleOutcome(state=DraftCycleState.COMPLETE),
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.order == ["defer", "press", "open_block", "notify", "wire", "standings"]


async def test_the_slate_survives_a_media_failure(world, cog):
    """The wire post raising must not undo, skip or precede the slate, and must not stop
    the standings sync behind it (M6, and `game_feed`'s own "failures degrade" rule)."""
    world.wire_raises = RuntimeError("no wire channel registered for league 7")
    world.report = _block_report(media_cycle_index=1)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.order == ["defer", "press", "open_block", "wire", "standings"]
    embed = interaction.reply["embed"]
    assert "Simulated 2 game(s)" in embed.description, "the press committed either way"
    notices = [f for f in embed.fields if f.name == "Did not go out"]
    assert len(notices) == 1 and "wire" in notices[0].value.lower()


async def test_a_failing_slate_does_not_stop_the_wire_or_the_standings(world, cog):
    world.open_block_raises = RuntimeError("discord is down")
    world.report = _block_report(media_cycle_index=1)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert world.order == ["defer", "press", "open_block", "wire", "standings"]
    (notice,) = [f for f in interaction.reply["embed"].fields if f.name == "Did not go out"]
    assert "#games" in notice.value


async def test_a_partly_failed_slate_is_disclosed(world, cog):
    world.open_block_report = SimpleNamespace(
        failed=(SimpleNamespace(game_id=11, reason="forbidden"),), posted=(12,), header_posted=True
    )
    world.report = _block_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    (notice,) = [f for f in interaction.reply["embed"].fields if f.name == "Did not go out"]
    assert "#games" in notice.value


async def test_a_failing_standings_sync_is_disclosed_and_the_press_still_reports(world, cog):
    world.standings_raises = RuntimeError("no standings channel registered")
    world.report = _block_report()
    interaction = world.interaction()

    await _advance(cog, interaction)

    embed = interaction.reply["embed"]
    assert embed.title == "Simulated a game day in REGULAR_SEASON"
    assert any("standings" in f.value.lower() for f in embed.fields if f.name == "Did not go out")


async def test_without_a_guild_the_wire_and_standings_cannot_run_and_the_reply_says_so(world, cog):
    world.report = _block_report(media_cycle_index=1)
    interaction = world.interaction()
    interaction.guild = None

    await _advance(cog, interaction)

    assert world.wire_calls == [] and world.standings_calls == []
    assert [c["block_id"] for c in world.open_block_calls] == ["2026-10-25"], (
        "the slate needs the bot, not the guild"
    )
    assert any(f.name == "Did not go out" for f in interaction.reply["embed"].fields)


# --- the cached media library -----------------------------------------------------------------


async def test_the_cached_library_is_passed_into_press_and_loaded_once_per_process(world, cog):
    first = world.interaction()
    second = world.interaction()

    await _advance(cog, first)
    await _advance(cog, second)

    assert [c["library"] for c in world.press_calls] == [world.library, world.library]
    assert world.load_library_calls == 1, "loaded once per process, never per press"


async def test_the_press_is_handed_the_pool_the_league_and_the_actor(world, cog):
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.press_calls
    assert call["pool"] == "pool"
    assert call["league_id"] == _LEAGUE_ID
    assert call["actor_user_id"] == _COMMISSIONER


async def test_a_library_that_cannot_be_loaded_is_a_generic_failure_not_a_hang(
    world, cog, monkeypatch
):
    def broken():
        raise FileNotFoundError("seeds/media_phrases.json")

    monkeypatch.setattr(cog_mod.media_service, "load_media_library", broken)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.reply["content"] == GENERIC_FAILURE_MESSAGE
    assert world.press_calls == []


# --- progress ------------------------------------------------------------------------------------


async def test_a_progress_callback_is_passed_and_edits_the_deferred_reply(world, cog):
    world.progress_messages_to_send = ["Claiming the league..."]
    interaction = world.interaction()

    await _advance(cog, interaction)

    (call,) = world.press_calls
    assert callable(call["on_progress"])
    assert interaction.edits[0]["content"] == "Claiming the league..."


async def test_progress_edits_are_throttled_to_one_every_two_seconds(world, cog, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    interaction = world.interaction()
    editor = cog_mod._ProgressEditor(interaction)

    await editor("one")
    now[0] += 0.5
    await editor("two")
    now[0] += 0.5
    await editor("three")
    assert [e["content"] for e in interaction.edits] == ["one"]

    now[0] += 1.5
    await editor("four")
    assert [e["content"] for e in interaction.edits] == ["one", "four"]


async def test_a_progress_edit_that_fails_does_not_raise_into_the_press(world, cog):
    interaction = world.interaction()
    interaction.edit_raises = RuntimeError("Unknown interaction")
    editor = cog_mod._ProgressEditor(interaction)

    await editor("Claiming the league...")  # must not raise

    assert interaction.edits == []


async def test_the_progress_line_is_replaced_when_the_press_finishes(world, cog):
    world.progress_messages_to_send = ["Simming the 2026-10-25 game day..."]
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.edits[0]["content"] == "Simming the 2026-10-25 game day..."
    assert interaction.edits[-1]["content"] != interaction.edits[0]["content"]
    assert "below" in interaction.edits[-1]["content"].lower() or "done" in (
        interaction.edits[-1]["content"].lower()
    )


async def test_no_progress_edit_is_made_when_the_service_reported_nothing(world, cog):
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.edits == []


async def test_the_standings_label_comes_from_the_league_as_it_stands_after_the_press(world, cog):
    """Critic 3, minor 7: the label proxy read `hook_completed_cycle_index` from the row
    `_authorize` read BEFORE the defer -- which the press may have just moved. A press whose
    phase bears no cycle has no ordinal on its report, so the label is the proxy of the league
    RE-READ after the press."""
    world.league.hook_completed_cycle_index = 2
    world.after_press_league = SimpleNamespace(
        **{**vars(world.league), "hook_completed_cycle_index": 5}
    )
    world.report = _entry_report(media_cycle_index=0, cycle_ordinal=None)

    await _advance(cog, world.interaction())

    (call,) = world.standings_calls
    assert call["cycle_index"] == 6, "5 + 1 from the post-press league, not 2 + 1"


async def test_the_reports_own_cycle_ordinal_labels_the_standings_when_it_has_one(world, cog):
    world.after_press_league = SimpleNamespace(
        **{**vars(world.league), "hook_completed_cycle_index": 5}
    )
    world.report = _entry_report(media_cycle_index=3, cycle_ordinal=3)

    await _advance(cog, world.interaction())

    (call,) = world.standings_calls
    assert call["cycle_index"] == 3


async def test_posting_is_announced_before_the_first_discord_call_when_there_is_something(
    world, cog, monkeypatch
):
    """The service names no "posting" milestone (it does not know what is posted); the cog
    announces it, once, just before the Discord calls that follow a press that has some."""
    world.report = _block_report()
    interaction = world.interaction()
    seen: list[list[str]] = []

    async def spy(bot, ex, **kwargs):
        seen.append([e["content"] for e in interaction.edits])
        return world.open_block_report

    monkeypatch.setattr(game_feed, "open_block", spy)

    await _advance(cog, interaction)

    assert seen == [["Posting the results to Discord..."]]


async def test_posting_is_announced_for_a_draft_ping_alone(world, cog):
    outcome = DraftCycleOutcome(state=DraftCycleState.WAITING_ON_HUMAN, team_id=7)
    world.report = PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=_ALLOWED,
        hook_result=PhaseHookResult(
            league_id=_LEAGUE_ID, season=_SEASON, phase=Phase.DRAFT, draft_cycle=outcome
        ),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.edits[0]["content"] == "Posting the results to Discord..."


async def test_posting_is_not_announced_when_the_press_has_nothing_to_post(world, cog):
    world.report = PressReport(
        kind=PressKind.BLOCKED,
        from_phase=Phase.WAIVERS,
        verdict=ExitVerdict(allowed=False, blockers=()),
    )
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.edits == []


async def test_posting_is_not_announced_for_a_wire_only_press_when_there_is_no_guild(world, cog):
    """The wire and the standings need the server; with none, nothing is posted, so the
    "Posting..." milestone would be a promise the reply then contradicts (critic 4, n-5)."""
    world.report = _entry_report(media_cycle_index=0, cycle_ordinal=None)
    interaction = world.interaction()
    interaction.guild = None

    await _advance(cog, interaction)

    assert interaction.edits == []
    assert world.wire_calls == [] and world.standings_calls == []


async def test_posting_is_announced_for_a_wire_only_press_when_there_is_a_guild(world, cog):
    world.report = _entry_report(media_cycle_index=0, cycle_ordinal=None)
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert interaction.edits[0]["content"] == "Posting the results to Discord..."


async def test_a_failing_progress_edit_is_throttled_not_retried_at_every_milestone(
    world, monkeypatch
):
    """Critic 3, minor 3: `_last_edit` was stamped only after a SUCCESSFUL edit, so an expired
    interaction was re-tried (and re-logged) at every milestone of the press."""
    now = [100.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    interaction = world.interaction()
    interaction.edit_raises = RuntimeError("Unknown interaction")
    editor = cog_mod._ProgressEditor(interaction)

    await editor("one")
    now[0] += 0.5
    await editor("two")
    now[0] += 0.5
    await editor("three")

    assert interaction.edit_attempts == 1

    now[0] += 1.5
    await editor("four")

    assert interaction.edit_attempts == 2


async def test_progress_edits_never_ping_anyone(world, cog):
    """An `allowed_mentions` on every edit of the deferred reply: a milestone sentence names
    phases and dates, but the reply is one more place a stray mention could go out."""
    world.progress_messages_to_send = ["Claiming the league..."]
    interaction = world.interaction()

    await _advance(cog, interaction)

    assert len(interaction.edits) >= 2, "a milestone and the closing line"
    for edit in interaction.edits:
        assert edit["allowed_mentions"] is not None
        assert edit["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


def test_every_press_kind_has_a_reply_outcome_and_the_check_survives_python_dash_o():
    """The mapping is checked with a real `raise`, not an `assert` (`python -O` strips those,
    and the KeyError would arrive in `advance`, after the press committed)."""
    outcomes = dict(cog_mod._OUTCOME_BY_KIND)
    cog_mod._require_a_reply_for_every_press_kind(outcomes)  # the module's own: no raise

    del outcomes[PressKind.DRAFT_RESUME]
    with pytest.raises(RuntimeError, match="draft_resume"):
        cog_mod._require_a_reply_for_every_press_kind(outcomes)

    extra = dict(cog_mod._OUTCOME_BY_KIND)
    extra["mystery"] = cog_mod.PressOutcome.ENTRY
    with pytest.raises(RuntimeError, match="mystery"):
        cog_mod._require_a_reply_for_every_press_kind(extra)


# --- structural -------------------------------------------------------------------------


def test_the_cog_module_imports_no_forbidden_layer_and_writes_no_league_column():
    import ast
    import pathlib

    source = pathlib.Path(cog_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(m.startswith("dba2.phase.service") for m in imported)
    assert "dba2.sim" not in imported
    assert "UPDATE leagues" not in source and "phase_service" not in source


def test_gm_service_is_not_reached_from_the_cog():
    """The hook belongs to `season_service.press`; a cog that called `run_phase_hook`
    itself would re-implement the press."""
    import pathlib

    source = pathlib.Path(cog_mod.__file__).read_text(encoding="utf-8")
    assert "run_phase_hook" not in source
