"""`/advance-run` and `/advance-run-stop` in `dba2/bot/cogs/advance.py`, with no database and
no gateway (docs/design/advance-run.md AR-2 to AR-10).

`season_service.press` is scripted and the REAL `season_run.run_to` loops over it, so what is
asserted is the cog's own job around a real loop: the single-run guard, the defer, what is and is
not posted to `#games` during a run (no slate, ever), the kept draft announcement and ping, the
wire, the standings sync on its real cadence (docs/design/standings-v2-conference-split.md SV-1:
every 10th game-carrying press, plus an end-of-run fallback for any partial leftover -- NOT once
at the end, as this file's own docstring used to claim), ONE score-free digest and one reply --
and, for the loop itself, that the caps and the cancel are honoured between presses.

**Nothing here has run against a live Discord gateway.** Every Discord-side call is an
in-process recorder; the service side of a run is proved in `tests/unit/test_season_run.py` and
`tests/integration/test_season_run.py` (real Postgres).
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from dba2.bot import client as bot_client
from dba2.bot import game_feed
from dba2.bot.cogs import advance as cog_mod
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.bot.run_registry import RunRegistry
from dba2.core.errors import ConcurrencyError
from dba2.domain.phase import Phase
from dba2.phase.command_gates import ALL_PHASES, COMMAND_GATES
from dba2.services import season_run, season_service
from dba2.services.gm_service import (
    DraftCycleOutcome,
    DraftCycleReason,
    DraftCycleState,
    PhaseHookResult,
)
from dba2.services.season_service import Blocker, BlockerCode, ExitVerdict, PressKind, PressReport
from tests.unit.bot.test_advance_cog import (
    _COMMISSIONER,
    _OTHER,
    _block_report,
    _entry_report,
    _Interaction,
    _simmed,
    _World,
)

_SEASON = 2026


class _RunInteraction(_Interaction):
    """`_Interaction` plus `created_at`, which the run's deadline is computed from."""

    def __init__(self, log: list[str], user_id: int = _COMMISSIONER) -> None:
        super().__init__(log, user_id)
        self.created_at = datetime.now(UTC)


class _RunWorld:
    """`test_advance_cog._World` (the patched surroundings) plus a scripted press, the
    `#games` digest path, the block close and a team lookup."""

    def __init__(self, monkeypatch, *, phase: Phase) -> None:
        self.w = _World(monkeypatch, phase=phase)
        self.script: list = []
        self.press_calls = 0
        self.digests: list[str] = []
        self.digest_raises: BaseException | None = None
        self.close_block_calls = 0
        self.close_block_raises: BaseException | None = None
        self.close_block_outcomes: dict[int, game_feed.RevealOutcome] = {}
        self.before_press: list = []
        self.runs = RunRegistry()

        async def press(pool, **kwargs):
            self.press_calls += 1
            self.w.order.append("press")
            if self.before_press:
                self.before_press.pop(0)()
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        async def get_league(pool, league_id):
            return self.w.league

        async def close_block(bot, ex, *, league_id):
            self.close_block_calls += 1
            self.w.order.append("close_block")
            if self.close_block_raises is not None:
                raise self.close_block_raises
            return game_feed.CloseBlockReport(
                block_id="2026-10-19" if self.close_block_outcomes else None,
                outcomes=self.close_block_outcomes,
                still_broadcasting=(),
            )

        async def games_channel(bot, ex, league_id):
            return "the-games-channel"

        async def send_line(channel, content):
            self.w.order.append("digest")
            if self.digest_raises is not None:
                raise self.digest_raises
            self.digests.append(content)
            return 1

        async def get_team(ex, team_id):
            for team in self.w.teams:
                if team.id == team_id:
                    return team
            return None

        monkeypatch.setattr(season_service, "press", press)
        monkeypatch.setattr(season_run.league_repo, "get_league", get_league)
        monkeypatch.setattr(game_feed, "close_block", close_block)
        monkeypatch.setattr(game_feed, "games_channel", games_channel)
        monkeypatch.setattr(game_feed, "send_line", send_line)
        monkeypatch.setattr(cog_mod.team_repo, "get_team", get_team)

    @property
    def bot(self):
        return SimpleNamespace(name="the-bot", runs=self.runs)

    def interaction(self, user_id: int = _COMMISSIONER) -> _RunInteraction:
        return _RunInteraction(self.w.order, user_id)


@pytest.fixture
def rw(monkeypatch):
    # The post-press AI pass reads get_config(), which raises without DATABASE_URL.
    # Without a value here these tests pass or fail depending on the runner's shell.
    from dba2.core import config as config_mod

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@localhost/unused")
    monkeypatch.setenv("MEDIA_AI_ENABLED", "false")
    config_mod.reset_config_cache()
    yield _RunWorld(monkeypatch, phase=Phase.PRESEASON)
    config_mod.reset_config_cache()


@pytest.fixture
def cog(rw):
    return cog_mod.AdvanceCommands(bot=rw.bot)


async def _run(cog, interaction, phase: str = "TRADE_DEADLINE") -> None:
    await cog.advance_run.callback(cog, interaction, phase)


async def _stop(cog, interaction) -> None:
    await cog.advance_run_stop.callback(cog, interaction)


def _day(day: int, games: int = 2) -> PressReport:
    d = date(2026, 10, day)
    return _block_report(
        from_phase=Phase.REGULAR_SEASON,
        block=season_service.SimmedBlock(
            block_id=d.isoformat(),
            season=_SEASON,
            games=tuple(_simmed(100 * day + i, 1, 2, d) for i in range(games)),
        ),
    )


def _enter(to_phase: Phase, from_phase: Phase, **overrides) -> PressReport:
    return _entry_report(to_phase=to_phase, from_phase=from_phase, **overrides)


def _season_script() -> list[PressReport]:
    """PRESEASON -> TRADE_DEADLINE: a media cycle on the first entry, two game days between --
    only 2 game-carrying presses, well under SV-1's 10-press sync cadence, so a run of this
    script exercises only the end-of-run fallback (see the cadence tests below for the
    mid-run sync itself)."""
    return [
        _enter(Phase.REGULAR_SEASON, Phase.PRESEASON, media_cycle_index=0, cycle_ordinal=0),
        _day(20),
        _day(21),
        _enter(Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON, cycle_ordinal=2),
    ]


def _long_season_script(game_carrying_presses: int) -> list[PressReport]:
    """`game_carrying_presses` block presses (each a distinct October game day, one media
    cycle on entry, one entry on exit) -- SV-1's own cadence tests need a run long enough to
    cross the 10th/20th game-carrying press, which `_season_script`'s 2-press script never
    does."""
    return [
        _enter(Phase.REGULAR_SEASON, Phase.PRESEASON, media_cycle_index=0, cycle_ordinal=0),
        *(_day(day) for day in range(1, game_carrying_presses + 1)),
        _enter(Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON, cycle_ordinal=game_carrying_presses),
    ]


def _waiting_on(team_id: int = 3, manager: int = 77, picks: int = 0) -> PressReport:
    hook = PhaseHookResult(
        league_id=7,
        season=_SEASON,
        phase=Phase.DRAFT,
        draft=SimpleNamespace(picks=tuple(range(picks))) if picks else None,
        draft_cycle=DraftCycleOutcome(
            state=DraftCycleState.WAITING_ON_HUMAN,
            team_id=team_id,
            round=1,
            pick_number=5,
            manager_user_id=manager,
        ),
    )
    return PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=ExitVerdict(allowed=False, blockers=()),
        hook_result=hook,
    )


# --- registration and the gates ----------------------------------------------------------


async def test_both_commands_are_registered_with_all_phase_gates_and_no_setup_choice():
    bot = bot_client.build_bot()
    await bot.setup_hook()
    by_name = {c.name: c for c in bot.tree.get_commands()}

    assert [p.name for p in by_name["advance-run"].parameters] == ["phase"]
    assert [p.name for p in by_name["advance-run-stop"].parameters] == []
    choices = {c.value for c in by_name["advance-run"].parameters[0].choices}
    assert "SETUP" not in choices and "WAIVERS" in choices
    assert COMMAND_GATES["advance_run"] == ALL_PHASES
    assert COMMAND_GATES["advance_run_stop"] == ALL_PHASES


# --- refusals come before the defer ---------------------------------------------------------


async def test_a_non_commissioner_is_refused_before_the_defer_and_nothing_runs(rw, cog):
    interaction = rw.interaction(_OTHER)

    await _run(cog, interaction)

    assert interaction.response.deferred == []
    assert rw.press_calls == 0 and rw.runs.active() == ()
    assert "commissioner" in interaction.reply["content"]


async def test_a_target_with_no_route_is_refused_before_the_defer(rw, cog):
    rw.w.league.current_phase = Phase.POST_DEADLINE
    interaction = rw.interaction()

    await _run(cog, interaction, "REGULAR_SEASON")

    assert interaction.response.deferred == [] and rw.press_calls == 0
    assert "/advance-run only goes forward" in interaction.reply["content"]
    assert rw.runs.active() == ()


async def test_a_league_already_in_the_target_is_told_so_without_a_defer_or_a_press(rw, cog):
    rw.w.league.current_phase = Phase.TRADE_DEADLINE
    interaction = rw.interaction()

    await _run(cog, interaction, "TRADE_DEADLINE")

    assert interaction.response.deferred == [] and rw.press_calls == 0
    assert interaction.reply["content"] == "The league is already in TRADE_DEADLINE."


async def test_an_unknown_phase_is_refused(rw, cog):
    interaction = rw.interaction()
    await _run(cog, interaction, "NOT_A_PHASE")
    assert interaction.response.deferred == [] and "no phase called" in interaction.reply["content"]


# --- the single-run guard (AR-4) ---------------------------------------------------------------


async def test_a_second_run_for_the_league_is_refused_and_names_the_holder(rw, cog):
    held = rw.runs.begin(
        league_id=7, user_id=_COMMISSIONER, started_at=datetime.now(UTC), phase=Phase.DRAFT
    )
    held.presses = 41
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert interaction.response.deferred == [] and rw.press_calls == 0
    text = interaction.reply["content"]
    assert "already going" in text and "41 press(es)" in text and "/advance-run-stop" in text
    assert rw.runs.current(7) is held  # the refused run did not disturb the holder


async def test_the_slot_is_freed_after_a_run_and_after_a_failed_one(rw, cog):
    rw.script = _season_script()
    await _run(cog, rw.interaction())
    assert rw.runs.active() == ()

    rw.w.league.current_phase = Phase.PRESEASON
    rw.script = [RuntimeError("boom")]
    await _run(cog, rw.interaction())
    assert rw.runs.active() == ()


async def test_the_slot_is_freed_when_the_defer_itself_fails(rw, cog, monkeypatch):
    interaction = rw.interaction()

    async def broken_defer(*, ephemeral=False):
        raise RuntimeError("Unknown interaction")

    monkeypatch.setattr(interaction.response, "defer", broken_defer)

    with pytest.raises(RuntimeError):
        await _run(cog, interaction)

    assert rw.runs.active() == ()


# --- what a run posts: no slate, the kept calls, standings on its real cadence, one digest -------


async def test_a_short_run_posts_no_slate_but_keeps_the_wire_and_syncs_standings_once(rw, cog):
    """`_season_script`'s 2 game-carrying presses never reach SV-1's 10-press cadence, so this
    run's only standings edit is the end-of-run fallback -- see the cadence tests below for the
    mid-run sync itself, which this short a script cannot exercise."""
    rw.script = _season_script()
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert interaction.response.deferred == [True]
    assert rw.w.open_block_calls == []  # mutation 4's assertion: no per-block slate, ever
    assert rw.press_calls == 4
    assert len(rw.w.wire_calls) == 1  # one press finished a media cycle
    assert len(rw.w.standings_calls) == 1  # under cadence: only the end-of-run fallback fires
    assert len(rw.digests) == 1  # exactly one durable #games send
    assert rw.w.order.count("digest") == 1


async def test_the_open_block_is_closed_before_the_first_press_and_the_digest_comes_last(rw, cog):
    rw.script = _season_script()

    await _run(cog, rw.interaction())

    order = rw.w.order
    assert order.index("close_block") < order.index("press")
    assert rw.close_block_calls == 1
    assert order.index("standings") > max(i for i, e in enumerate(order) if e == "press")
    assert order.index("digest") > order.index("standings")


# --- standings-v2's SV-1: the real mid-run cadence, not once at the end -------------------------


async def test_a_long_run_syncs_standings_every_tenth_game_carrying_press(rw, cog):
    """21 game-carrying presses: mid-run syncs fire on the 10th and 20th, each still after
    that same press's wire post; the trailing 21st press leaves 1 press since the last sync,
    covered by the end-of-run fallback -- three standings edits total, not one.

    **M2 fix: the 10th game-carrying press ALSO finishes a media cycle**, so it both carries
    the wire AND crosses SV-1's cadence threshold in the SAME press -- `_long_season_script`
    on its own never produces this coincidence (its only wire-carrying press is the opening
    ENTRY, well before any cadence threshold), so the property below was previously proven
    only by a tautology (`max(w < standings_index for w in wire_indexes) < standings_index`
    is true by construction of the filter that built `wire_indexes`, regardless of whether a
    wire and a sync ever actually share a press)."""
    script = _long_season_script(21)
    script[10] = replace(script[10], media_cycle_index=99)  # the 10th game-carrying press
    rw.script = script
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert rw.press_calls == 23  # entry + 21 block presses + entry
    assert len(rw.w.standings_calls) == 3
    assert len(rw.w.wire_calls) == 2  # the opening entry, plus the 10th day's own media cycle
    order = rw.w.order
    press_indexes = [i for i, e in enumerate(order) if e == "press"]
    # Each press's OWN slice of `order` (up to, not including, the next press) -- a real,
    # per-press-scoped check: a press whose slice holds both events proves the wire and the
    # sync landed in that SAME press, and in the right order within it, not just that some
    # earlier press's wire index happens to be a smaller integer than a later press's
    # standings index.
    same_press_pairs_checked = 0
    for start, end in zip(press_indexes, [*press_indexes[1:], len(order)], strict=True):
        chunk = order[start:end]
        if "wire" in chunk and "standings" in chunk:
            assert chunk.index("wire") < chunk.index("standings"), chunk
            same_press_pairs_checked += 1
    # The 10th game-carrying press is the one crafted above to hit exactly this case; without
    # it, no press in a stock `_long_season_script` ever carries both events at once.
    assert same_press_pairs_checked == 1
    standings_indexes = [i for i, e in enumerate(order) if e == "standings"]
    assert order.index("digest") > max(standings_indexes)


async def test_a_press_that_only_finishes_a_media_cycle_does_not_count_toward_the_cadence(rw, cog):
    """B2 (revision note): a media-cycle-only press moves no team's record, so it must not
    contaminate "every 10th game-carrying press" with a press that carries no games.

    **Must actually diverge from the regression it names.** 20 wire-carrying, GAME-LESS
    block presses (`wants_wire=True`, `block.games == ()`) cross SV-1's 10-press cadence
    TWICE under the exact named bug (`... or wants_wire` back in the counter condition at
    `advance.py:1113`) -- two mid-run syncs fire (at the 10th and 20th such press), each
    leaving `standings_owed` cleared, so the end-of-run fallback has nothing left to do:
    2 total standings calls, not 1. Under the correct, game-carrying-only condition, none
    of these 20 presses ever increments the counter, so the mid-run cadence never fires and
    only the end-of-run fallback (owed by every one of the 20 presses' own `wants_wire`)
    posts: 1 total standings call. A 2-vs-1 call-count assertion, not a script short enough
    that both the correct and the buggy condition land under cadence and look identical."""
    rw.script = [
        _enter(Phase.REGULAR_SEASON, Phase.PRESEASON, media_cycle_index=0, cycle_ordinal=0),
        *(
            replace(_day(day, games=0), media_cycle_index=day)
            for day in range(1, 21)  # 20 game-less presses, each finishing a media cycle
        ),
        _enter(Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON, cycle_ordinal=21),
    ]
    interaction = rw.interaction()

    await _run(cog, interaction)

    # None of the 20 game-less presses (nor either entry) is game-carrying, so the mid-run
    # cadence never fires; the trailing entry's own media cycle still owes the end-of-run
    # fallback -- exactly ONE standings edit for the whole run.
    assert len(rw.w.standings_calls) == 1


async def test_a_failed_mid_run_sync_resets_the_counter_and_still_retries_at_the_end(rw, cog):
    rw.script = _long_season_script(10)
    rw.w.standings_raises = RuntimeError("standings down")
    interaction = rw.interaction()

    await _run(cog, interaction)

    # The 10th press's mid-run attempt failed; the counter still reset (no hammering loop),
    # and `standings_owed` was left `True` (never cleared on failure) -- the end-of-run
    # fallback is the retry, so a second attempt (also failing) still happens.
    assert len(rw.w.standings_calls) == 2
    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "could not be updated mid-run" in notices
    assert "could not be updated (error logged)" in notices


async def test_the_digest_carries_the_summary_and_no_score(rw, cog):
    rw.script = _season_script()

    await _run(cog, rw.interaction())

    (digest,) = rw.digests
    assert "Simmed 2 game days, 2026-10-20 → 2026-10-21 (4 games)." in digest
    assert "Entered: REGULAR_SEASON → TRADE_DEADLINE." in digest
    assert "/advance-run TRADE_DEADLINE" in digest
    assert not re.search(r"(?<![\d-])\d{2,3}\s*[-–:]\s*\d{2,3}(?![\d-])", digest), digest


async def test_the_reply_is_one_panel_that_names_the_target(rw, cog):
    rw.script = _season_script()
    interaction = rw.interaction()

    await _run(cog, interaction)

    reply = interaction.reply
    assert reply["ephemeral"] is True
    assert reply["embed"].title == "Ran to TRADE_DEADLINE"
    assert "The league is in TRADE_DEADLINE." in reply["embed"].description


async def test_a_human_on_the_clock_is_still_pinged_and_the_run_stops_naming_the_team(rw, cog):
    """Mutation 5's assertion: the run stops for a person, so the ping must still go out."""
    rw.w.league.current_phase = Phase.DRAFT
    rw.script = [_waiting_on(team_id=3)]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    (call,) = rw.w.notify_calls
    assert call["human_pick"] is None
    assert call["outcome"].state is DraftCycleState.WAITING_ON_HUMAN
    assert call["outcome"].manager_user_id == 77
    description = interaction.reply["embed"].description
    assert "CCC (round 1, pick 5)" in description and "/draft" in description
    assert rw.w.open_block_calls == []


async def test_an_entry_into_draft_that_lands_waiting_on_a_human_announces_them_once(rw, cog):
    """The entry runs the DRAFT hook and the cog announces its cycle. The run must stop right
    there: pressing on would be a `DRAFT_RESUME` that re-runs the hook and pings the manager a
    second time (and burns a press)."""
    rw.w.league.current_phase = Phase.DRAFT_LOTTERY
    waiting = _waiting_on(team_id=3)
    rw.script = [
        _enter(Phase.DRAFT, Phase.DRAFT_LOTTERY, hook_result=waiting.hook_result),
        waiting,  # what a duplicate press would have returned: it must never be pressed
    ]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    assert rw.press_calls == 1
    (call,) = rw.w.notify_calls
    assert call["outcome"].state is DraftCycleState.WAITING_ON_HUMAN
    assert call["outcome"].manager_user_id == 77
    description = interaction.reply["embed"].description
    assert "CCC (round 1, pick 5)" in description and "/draft" in description


async def test_a_draft_only_run_that_made_a_pick_still_leaves_the_durable_digest(rw, cog):
    """The AI picked, then a human came on the clock: no game and no phase entered, yet the
    picks are real and the ephemeral panel is the only other copy (R5)."""
    rw.w.league.current_phase = Phase.DRAFT
    rw.script = [_waiting_on(team_id=3, picks=2)]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    assert rw.press_calls == 1
    assert len(rw.digests) == 1
    assert "/advance-run POST_DRAFT_TRADES" in rw.digests[0]
    assert "/draft" in rw.digests[0]
    assert interaction.reply["embed"] is not None


async def test_a_draft_only_run_that_changed_nothing_leaves_no_digest(rw, cog):
    rw.w.league.current_phase = Phase.DRAFT
    rw.script = [_waiting_on(team_id=3)]

    await _run(cog, rw.interaction(), "POST_DRAFT_TRADES")

    assert rw.press_calls == 1
    assert rw.digests == []


async def test_a_stalled_draft_stops_the_run_with_the_reason_in_words(rw, cog):
    rw.w.league.current_phase = Phase.DRAFT
    hook = PhaseHookResult(
        league_id=7,
        season=_SEASON,
        phase=Phase.DRAFT,
        draft_cycle=DraftCycleOutcome(
            state=DraftCycleState.STALLED, reason=DraftCycleReason.POOL_EXHAUSTED
        ),
    )
    rw.script = [
        PressReport(
            kind=PressKind.DRAFT_RESUME,
            from_phase=Phase.DRAFT,
            verdict=ExitVerdict(allowed=False, blockers=()),
            hook_result=hook,
        )
    ]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    assert "nobody left for an AI pick" in interaction.reply["embed"].description
    assert rw.press_calls == 1


async def test_a_missing_draft_board_stops_the_run_with_its_own_sentence(rw, cog):
    rw.w.league.current_phase = Phase.DRAFT_LOTTERY
    rw.script = [
        PressReport(
            kind=PressKind.BLOCKED,
            from_phase=Phase.DRAFT_LOTTERY,
            verdict=ExitVerdict(
                allowed=False,
                blockers=(Blocker(BlockerCode.DRAFT_BOARD_NOT_READY, "no draft board exists"),),
            ),
        )
    ]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    embed = interaction.reply["embed"]
    assert "tools/dev_league_draft.py" in embed.description
    assert "no draft board exists" in embed.fields[0].value
    assert rw.digests == []  # nothing was done, so there is no durable record to post


# --- the caps and the cancel ---------------------------------------------------------------------


async def test_a_run_whose_interaction_is_past_the_deadline_makes_no_press(rw, cog):
    """Mutation 1's assertion: the deadline is `interaction.created_at + 12 minutes`."""
    rw.script = _season_script()
    interaction = rw.interaction()
    interaction.created_at = datetime.now(UTC) - timedelta(minutes=12, seconds=1)

    await _run(cog, interaction)

    assert rw.press_calls == 0
    description = interaction.reply["embed"].description
    assert "Stopped after 0 presses" in description
    assert "run /advance-run TRADE_DEADLINE again to continue" in description


async def test_the_deadline_is_twelve_minutes_from_the_interactions_creation(rw, cog, monkeypatch):
    seen: list[season_run.RunCaps] = []
    real = season_run.run_to

    async def spy(*args, caps, **kwargs):
        seen.append(caps)
        return await real(*args, caps=caps, **kwargs)

    monkeypatch.setattr(season_run, "run_to", spy)
    rw.script = _season_script()
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert seen[0].deadline == interaction.created_at + timedelta(minutes=12)
    assert seen[0].max_presses == 400


async def test_a_stop_request_ends_the_run_after_the_press_in_flight_commits(rw, cog):
    rw.script = _season_script()
    rw.before_press = [lambda: None, lambda: rw.runs.request_stop(7)]  # asked during press 2
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert rw.press_calls == 2  # press 2 was in flight: it finished and was recorded
    embed = interaction.reply["embed"]
    assert "Stopped by /advance-run-stop." in embed.description
    assert len(rw.digests) == 1  # what did happen is still on the record


# --- failures -----------------------------------------------------------------------------------


async def test_a_press_that_fails_mid_run_shows_its_message_verbatim_and_keeps_the_record(rw, cog):
    busy = ConcurrencyError("This league is busy with another action right now")
    rw.script = [*_season_script()[:2], busy]
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert "This league is busy with another action right now" in (
        interaction.reply["embed"].description
    )
    assert len(rw.digests) == 1 and rw.w.open_block_calls == []


async def test_an_unexpected_press_failure_shows_the_generic_line_not_the_exception(rw, cog):
    rw.script = [_season_script()[0], RuntimeError("secret internals")]
    interaction = rw.interaction()

    await _run(cog, interaction)

    description = interaction.reply["embed"].description
    assert GENERIC_FAILURE_MESSAGE in description and "secret internals" not in description


async def test_a_failure_before_any_press_replies_like_the_other_commands(rw, cog, monkeypatch):
    async def league_gone(pool, league_id):
        return None

    monkeypatch.setattr(season_run.league_repo, "get_league", league_gone)
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert "does not exist" in interaction.reply["content"]
    assert rw.runs.active() == () and rw.press_calls == 0


async def test_a_failed_digest_is_named_in_the_panel_not_raised(rw, cog):
    rw.script = _season_script()
    rw.digest_raises = RuntimeError("Missing Access")
    interaction = rw.interaction()

    await _run(cog, interaction)

    fields = {f.name: f.value for f in interaction.reply["embed"].fields}
    assert "The #games digest could not be posted" in fields["Did not go out"]


async def test_a_failed_wire_post_and_standings_sync_each_degrade_to_a_notice(rw, cog):
    rw.script = _season_script()
    rw.w.wire_raises = RuntimeError("wire down")
    rw.w.standings_raises = RuntimeError("standings down")
    interaction = rw.interaction()

    await _run(cog, interaction)

    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "A wire post failed" in notices and "standings message could not be updated" in notices
    assert len(rw.digests) == 1  # a lost wire post cost the digest nothing


async def test_a_block_close_whose_reveals_failed_is_a_notice_and_the_run_goes_on(rw, cog):
    """`/advance`'s slate path names a part that did not post; a close that returns normally
    but could not reveal a score must not be silent either."""
    RO = game_feed.RevealOutcome
    rw.script = _season_script()
    rw.close_block_outcomes = {
        1: RO.REVEALED,
        2: RO.POST_FAILED,
        3: RO.MARK_FAILED,
        4: RO.NOT_OWNED,  # someone else's reveal is not this close's failure
    }
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert rw.press_calls == 4
    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "2 score(s)" in notices and "startup sweep" in notices


async def test_a_close_whose_reveals_all_succeeded_adds_no_notice(rw, cog):
    rw.script = _season_script()
    rw.close_block_outcomes = {1: game_feed.RevealOutcome.REVEALED}
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert "Did not go out" not in {f.name for f in interaction.reply["embed"].fields}


async def test_a_bug_in_the_standings_step_cannot_cost_the_digest_or_panel(rw, cog, monkeypatch):
    rw.script = _season_script()
    interaction = rw.interaction()

    async def broken(*args, **kwargs):
        raise RuntimeError("bug in the record step")

    monkeypatch.setattr(cog, "_post_the_run_record", broken)

    await _run(cog, interaction)

    assert len(rw.digests) == 1
    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "standings message could not be updated" in notices


async def test_a_bug_building_the_summary_still_leaves_a_digest_and_a_panel(rw, cog, monkeypatch):
    rw.script = _season_script()
    interaction = rw.interaction()

    async def broken(*args, **kwargs):
        raise RuntimeError("bug in the summary")

    monkeypatch.setattr(cog, "_run_summary", broken)

    await _run(cog, interaction)

    (digest,) = rw.digests
    assert "/advance-run TRADE_DEADLINE" in digest and "summary could not be built" in digest
    assert interaction.reply["embed"].title == "Ran to TRADE_DEADLINE"
    assert rw.runs.active() == ()


async def test_a_bug_sending_the_panel_does_not_escape_the_command(rw, cog, monkeypatch):
    rw.script = _season_script()

    async def broken(*args, **kwargs):
        raise RuntimeError("bug in the panel")

    monkeypatch.setattr(cog, "_send_panel", broken)

    await _run(cog, rw.interaction())  # must not raise

    assert len(rw.digests) == 1 and rw.runs.active() == ()


async def test_a_failed_block_close_is_a_notice_and_the_run_goes_on(rw, cog):
    rw.script = _season_script()
    rw.close_block_raises = RuntimeError("Discord down")
    interaction = rw.interaction()

    await _run(cog, interaction)

    assert rw.press_calls == 4
    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "could not be closed" in notices


async def test_a_failing_draft_announcement_is_a_notice_and_the_run_goes_on(rw, cog):
    rw.w.league.current_phase = Phase.DRAFT
    rw.w.notify_raises = RuntimeError("Missing Access")
    rw.script = [_waiting_on()]
    interaction = rw.interaction()

    await _run(cog, interaction, "POST_DRAFT_TRADES")

    notices = {f.name: f.value for f in interaction.reply["embed"].fields}["Did not go out"]
    assert "draft announcement did not fully post" in notices


# --- progress (AR-8) ----------------------------------------------------------------------------


async def test_the_progress_line_is_forced_on_a_phase_entry_and_throttled_between(
    rw, cog, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    rw.script = _season_script()
    rw.before_press = [lambda: None] * 4
    interaction = rw.interaction()

    await _run(cog, interaction)

    lines = [e["content"] for e in interaction.edits]
    # press 1 (an entry: forced), 2 and 3 (block presses inside 5 s: dropped), 4 (entry: forced)
    assert lines[0].startswith("REGULAR_SEASON — 0 game days simmed, 0 games. 1 press")
    assert lines[1].startswith("TRADE_DEADLINE — 2 game days simmed, 4 games, 2026-10-21. 4 press")
    assert lines[-1] == "Finished -- the result is below."
    assert len(lines) == 3


async def test_the_registry_entry_tracks_the_run_and_the_progress_editor_is_the_slow_one(
    rw, cog, monkeypatch
):
    created: list[float] = []
    real = cog_mod._ProgressEditor

    class Spy(real):
        def __init__(self, interaction, *, interval=2.0):
            created.append(interval)
            super().__init__(interaction, interval=interval)

    monkeypatch.setattr(cog_mod, "_ProgressEditor", Spy)
    seen: list[tuple[int, Phase | None]] = []
    rw.script = _season_script()
    rw.before_press = [
        lambda: seen.append((rw.runs.current(7).presses, rw.runs.current(7).phase))
    ] * 4

    await _run(cog, rw.interaction())

    assert created == [5.0]
    assert seen == [
        (0, Phase.PRESEASON),
        (1, Phase.REGULAR_SEASON),
        (2, Phase.REGULAR_SEASON),
        (3, Phase.REGULAR_SEASON),
    ]


# --- /advance-run-stop --------------------------------------------------------------------------


async def test_stop_with_no_run_says_there_is_nothing_to_stop(rw, cog):
    interaction = rw.interaction()
    await _stop(cog, interaction)
    assert "nothing to stop" in interaction.reply["content"]


async def test_stop_sets_the_runs_cancel_flag_and_says_the_press_finishes(rw, cog):
    run = rw.runs.begin(league_id=7, user_id=_COMMISSIONER, started_at=datetime.now(UTC))
    interaction = rw.interaction()

    await _stop(cog, interaction)

    assert run.cancel.is_set()
    assert "press in flight finishes" in interaction.reply["content"]


async def test_stop_is_commissioner_only(rw, cog):
    run = rw.runs.begin(league_id=7, user_id=_COMMISSIONER, started_at=datetime.now(UTC))
    interaction = rw.interaction(_OTHER)

    await _stop(cog, interaction)

    assert not run.cancel.is_set()
    assert "commissioner" in interaction.reply["content"]


# --- /advance itself is untouched ---------------------------------------------------------------


def _open_block_callers() -> list[str]:
    tree = ast.parse(Path(cog_mod.__file__).read_text(encoding="utf-8"))
    sites: list[str] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else enclosing
            )
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "open_block"
            ):
                sites.append(enclosing)
            visit(child, inner)

    visit(tree, "<module>")
    return sites


def test_open_block_is_called_from_the_slate_helper_alone_never_from_a_run():
    """The static twin of `..._posts_no_slate`: the only `open_block` call in the cog is
    `_open_the_slate`'s, which `/advance` reaches and a run never does."""
    assert _open_block_callers() == ["_open_the_slate"]


def test_no_run_method_reaches_the_slate_helper():
    tree = ast.parse(Path(cog_mod.__file__).read_text(encoding="utf-8"))
    run_methods = {
        "advance_run",
        "advance_run_stop",
        "_run_the_league",
        "_after_run_press",
        "_post_the_run_record",
        "_send_digest",
        "_run_summary",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in run_methods:
            names = {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            } | {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert "_open_the_slate" not in names and "open_block" not in names, node.name


async def test_plain_advance_still_opens_its_slate(rw, cog):
    """`/advance` is unchanged: a block press still opens the #games slate."""
    rw.w.league.current_phase = Phase.REGULAR_SEASON
    rw.script = [_block_report()]
    interaction = rw.w.interaction()

    await cog.advance.callback(cog, interaction)

    assert len(rw.w.open_block_calls) == 1


# --- the progress editor (AR-8) -----------------------------------------------------------------


async def test_the_progress_editor_takes_a_slower_interval_by_keyword(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    interaction = _RunInteraction([])
    editor = cog_mod._ProgressEditor(interaction, interval=5.0)

    await editor("one")
    now[0] += 3.0
    await editor("two")  # inside 5 s: dropped, though past the 2 s default
    now[0] += 2.5
    await editor("three")

    assert [e["content"] for e in interaction.edits] == ["one", "three"]


async def test_a_forced_line_skips_the_throttle_and_restarts_it(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    interaction = _RunInteraction([])
    editor = cog_mod._ProgressEditor(interaction, interval=5.0)

    await editor("one")
    now[0] += 0.1
    await editor("phase entry", force=True)
    now[0] += 0.1
    await editor("throttled")

    assert [e["content"] for e in interaction.edits] == ["one", "phase entry"]


async def test_the_default_interval_and_the_positional_construction_are_unchanged(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cog_mod, "_monotonic", lambda: now[0])
    interaction = _RunInteraction([])
    editor = cog_mod._ProgressEditor(interaction)

    await editor("one")
    now[0] += 1.9
    await editor("two")
    now[0] += 0.2
    await editor("three")

    assert [e["content"] for e in interaction.edits] == ["one", "three"]


def test_every_run_stop_has_its_presentation_twin():
    assert set(cog_mod._STOP_KIND_BY_RUN_STOP) == set(season_run.RunStop)
    assert all(kind.value == stop.value for stop, kind in cog_mod._STOP_KIND_BY_RUN_STOP.items())
