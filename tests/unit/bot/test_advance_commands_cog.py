"""The five companions of `/advance` (`dba2/bot/cogs/advance.py`): `/advance-to`,
`/advance-force`, `/advance-unlock`, `/advance-postpone-game`, `/advance-restore-game`
(docs/design/phase-5-stage-e-advance.md P5-99.a, P5-99.f, P5-99.g, P5-99.u.1, build-plan
commit 15), with no database and no gateway.

Every service function they call is replaced by a recorder, so what is asserted is exactly
the cog's job: commissioner-only (checked BEFORE the defer, and again by the service),
the command gate, an ephemeral defer before the call, the arguments the service is handed,
one reply, and every service refusal shown verbatim -- in particular the three refusals of
`/advance-restore-game`, each with its own message. The end-to-end behaviour against real
Postgres is `tests/integration/test_advance_commands_cog.py` and the service tests.

**Nothing here has run against a live Discord gateway.**
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from dba2.bot import client as bot_client
from dba2.bot.cogs import advance as cog_mod
from dba2.bot.gates import CommandNotLegalNow
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.core.errors import ConcurrencyError, PhaseError, ValidationError
from dba2.domain.game import GameStatus
from dba2.domain.phase import Phase
from dba2.phase.command_gates import ALL_PHASES, COMMAND_GATES
from dba2.phase.permissions import PermissionDenied
from dba2.services import season_service
from dba2.services.season_service import (
    AdvanceToReport,
    Blocker,
    BlockerCode,
    ForceReport,
    GameStatusChange,
    UnlockResult,
)

_COMMISSIONER = 1
_OTHER = 42
_LEAGUE_ID = 7

_COMMANDS = {
    "advance-to": "advance_to",
    "advance-force": "advance_force",
    "advance-unlock": "advance_unlock",
    "advance-postpone-game": "advance_postpone_game",
    "advance-restore-game": "advance_restore_game",
}


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
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, log: list[str], user_id: int = _COMMISSIONER) -> None:
        self.id = 1
        self.guild_id = 9
        self.guild = SimpleNamespace(id=9)
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response(log)
        self.followup = _Followup()

    @property
    def reply(self) -> dict:
        seen = self.response.messages + self.followup.sent
        assert len(seen) == 1, seen
        return seen[0]


def _change(*, changed: bool = True, to: GameStatus = GameStatus.POSTPONED) -> GameStatusChange:
    return GameStatusChange(
        game_id=412,
        from_status=GameStatus.SCHEDULED if to is GameStatus.POSTPONED else GameStatus.POSTPONED,
        to_status=to,
        changed=changed,
        transaction_id=88 if changed else None,
    )


class _World:
    def __init__(self, monkeypatch, *, phase: Phase = Phase.PLAYIN) -> None:
        self.order: list[str] = []
        self.league = SimpleNamespace(
            id=_LEAGUE_ID,
            name="Test League",
            current_phase=phase,
            current_season=2026,
            commissioner_user_id=_COMMISSIONER,
        )
        self.calls: list[tuple[str, dict]] = []
        self.raises: BaseException | None = None
        self.results: dict[str, object] = {
            "advance_to": AdvanceToReport(
                from_phase=Phase.PLAYIN,
                requested=Phase.PLAYOFFS_R1,
                entered=(Phase.PLAYOFFS_R1,),
                reached=True,
            ),
            "force": ForceReport(
                from_phase=Phase.PLAYIN,
                to_phase=Phase.PLAYOFFS_R2,
                overrode=(),
                skipped_work=(),
                recorded_reason="skip a round",
            ),
            "unlock": UnlockResult(
                cleared=True,
                claimed_by=5,
                claimed_at=None,
                age=timedelta(minutes=12),
                hook_completed_cycle_index=3,
                current_phase=Phase.PLAYIN,
                transaction_id=91,
                message="Cleared the advance claim held by user 5 for 12 minute(s).",
            ),
            "postpone_game": _change(),
            "restore_game": _change(to=GameStatus.SCHEDULED),
        }

        async def get_pool():
            return "pool"

        async def resolve_league(pool, interaction):
            return self.league

        def service(name):
            async def call(pool, **kwargs):
                self.order.append(name)
                self.calls.append((name, {"pool": pool, **kwargs}))
                if self.raises is not None:
                    raise self.raises
                return self.results[name]

            return call

        monkeypatch.setattr(cog_mod, "get_pool", get_pool)
        monkeypatch.setattr(cog_mod, "resolve_league", resolve_league)
        for name in ("advance_to", "force", "unlock", "postpone_game", "restore_game"):
            monkeypatch.setattr(season_service, name, service(name))

    def interaction(self, user_id: int = _COMMISSIONER) -> _Interaction:
        return _Interaction(self.order, user_id)


@pytest.fixture
def world(monkeypatch):
    return _World(monkeypatch)


@pytest.fixture
def cog():
    return cog_mod.AdvanceCommands(bot=SimpleNamespace())


async def _run(cog, command: str, interaction, *args) -> None:
    await getattr(cog, _COMMANDS[command]).callback(cog, interaction, *args)


_ARGS = {
    "advance-to": ("PLAYOFFS_R1",),
    "advance-force": ("PLAYOFFS_R2", "skip a round"),
    "advance-unlock": (),
    "advance-postpone-game": (412, "cannot sim"),
    "advance-restore-game": (412, "data fixed"),
}
_SERVICE = {
    "advance-to": "advance_to",
    "advance-force": "force",
    "advance-unlock": "unlock",
    "advance-postpone-game": "postpone_game",
    "advance-restore-game": "restore_game",
}


# --- registration and gates -----


async def test_the_five_commands_exist_with_the_designed_options():
    bot = bot_client.build_bot()
    await bot.setup_hook()
    by_name = {c.name: c for c in bot.tree.get_commands()}

    def options(name):
        return [p.name for p in by_name[name].parameters]

    assert options("advance-to") == ["phase"]
    assert options("advance-force") == ["phase", "reason"]
    assert options("advance-unlock") == []
    assert options("advance-postpone-game") == ["game_id", "reason"]
    assert options("advance-restore-game") == ["game_id", "reason"]
    # Each list omits what its command refuses every time (the service is still the rule).
    advance_to_choices = by_name["advance-to"].parameters[0].choices
    assert {c.value for c in advance_to_choices} == {p.value for p in Phase} - {"SETUP"}
    force_choices = by_name["advance-force"].parameters[0].choices
    assert {c.value for c in force_choices} == {p.value for p in Phase} - {"SETUP", "PRESEASON"}
    for omitted in (Phase.SETUP, Phase.PRESEASON):  # the service refuses them from anywhere
        assert all(season_service.force_target_refusal(src, omitted) for src in Phase), omitted


@pytest.mark.parametrize("command", list(_COMMANDS))
def test_each_command_has_its_own_all_phases_gate_entry(command):
    assert COMMAND_GATES[command.replace("-", "_")] == ALL_PHASES


# --- commissioner-only, gate, defer -----


@pytest.mark.parametrize("command", list(_COMMANDS))
async def test_a_non_commissioner_is_refused_before_defer_and_the_service_is_never_called(
    world, cog, command
):
    interaction = world.interaction(user_id=_OTHER)

    await _run(cog, command, interaction, *_ARGS[command])

    assert world.calls == []
    assert interaction.response.deferred == []
    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True and "commissioner" in reply["content"].lower()


@pytest.mark.parametrize("command", list(_COMMANDS))
async def test_a_command_gate_refusal_is_ephemeral_and_runs_nothing(
    world, cog, command, monkeypatch
):
    def refuse(name, phase):
        raise CommandNotLegalNow(f"/{name} is not available during {phase.value}")

    monkeypatch.setattr(cog_mod, "ensure_command_legal", refuse)
    interaction = world.interaction()

    await _run(cog, command, interaction, *_ARGS[command])

    assert world.calls == []
    assert interaction.response.deferred == []
    assert interaction.response.messages[0]["ephemeral"] is True


@pytest.mark.parametrize("command", list(_COMMANDS))
async def test_the_defer_is_ephemeral_and_happens_before_the_service_call(world, cog, command):
    interaction = world.interaction()

    await _run(cog, command, interaction, *_ARGS[command])

    assert interaction.response.deferred == [True]
    assert world.order == ["defer", _SERVICE[command]]
    assert interaction.reply["ephemeral"] is True


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize(
    "error",
    [
        ConcurrencyError("An advance is already running for this league."),
        PhaseError("game 412 cannot be restored: refused"),
        PermissionDenied("Only the commissioner may do that."),
        ValidationError("a reason is required"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_a_known_service_error_is_shown_verbatim_and_ephemerally(world, cog, command, error):
    world.raises = error
    interaction = world.interaction()

    await _run(cog, command, interaction, *_ARGS[command])

    assert interaction.reply == {"content": str(error), "embed": None, "ephemeral": True}


@pytest.mark.parametrize("command", list(_COMMANDS))
async def test_an_unexpected_error_is_generic_and_never_leaks_its_text(world, cog, command):
    world.raises = RuntimeError("connection reset at 10.0.0.5")
    interaction = world.interaction()

    await _run(cog, command, interaction, *_ARGS[command])

    assert interaction.reply["content"] == GENERIC_FAILURE_MESSAGE


# --- /advance-to -----


async def test_advance_to_passes_the_parsed_phase_and_replies_with_the_route(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-to", interaction, "PLAYOFFS_R1")

    ((name, kwargs),) = world.calls
    assert name == "advance_to"
    assert kwargs == {
        "pool": "pool",
        "league_id": _LEAGUE_ID,
        "actor_user_id": _COMMISSIONER,
        "to_phase": Phase.PLAYOFFS_R1,
    }
    embed = interaction.reply["embed"]
    assert embed.title == "Advanced to PLAYOFFS_R1"
    assert embed.description == "**PLAYIN** -> **PLAYOFFS_R1**"


async def test_advance_to_stopping_before_a_working_phase_says_which(world, cog):
    world.results["advance_to"] = AdvanceToReport(
        from_phase=Phase.PLAYOFFS_R1,
        requested=Phase.DRAFT_LOTTERY,
        entered=(Phase.AWARDS,),
        reached=False,
        stopped_before=Phase.DRAFT_LOTTERY,
        stop_reason="the next phase, DRAFT_LOTTERY, does work; press /advance to enter it.",
    )
    interaction = world.interaction()

    await _run(cog, "advance-to", interaction, "DRAFT_LOTTERY")

    embed = interaction.reply["embed"]
    assert embed.title == "Stopped short of DRAFT_LOTTERY"
    assert "the next phase, DRAFT_LOTTERY, does work" in embed.description


async def test_advance_to_a_gate_stop_lists_the_blockers(world, cog):
    world.results["advance_to"] = AdvanceToReport(
        from_phase=Phase.REGULAR_SEASON_COMPLETE,
        requested=Phase.PLAYOFFS_R1,
        entered=(),
        reached=False,
        stopped_before=Phase.PLAYIN,
        stop_reason="REGULAR_SEASON_COMPLETE cannot be left yet.",
        blockers=(
            Blocker(
                code=BlockerCode.REGULAR_SEASON_UNFINISHED, message="1 game(s) still scheduled"
            ),
        ),
    )
    interaction = world.interaction()

    await _run(cog, "advance-to", interaction, "PLAYOFFS_R1")

    (field,) = interaction.reply["embed"].fields
    assert field.name == "What is in the way" and "1 game(s) still scheduled" in field.value


async def test_advance_to_an_unknown_phase_is_refused_without_calling_the_service(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-to", interaction, "NOT_A_PHASE")

    assert world.calls == []
    assert "NOT_A_PHASE" in interaction.reply["content"]


# --- /advance-force -----


async def test_force_passes_the_phase_and_the_reason_and_names_what_it_overrode(world, cog):
    world.results["force"] = ForceReport(
        from_phase=Phase.DRAFT,
        to_phase=Phase.POST_DRAFT_TRADES,
        overrode=(
            Blocker(code=BlockerCode.DRAFT_NOT_COMPLETE, message="the draft is not finished."),
        ),
        skipped_work=("DRAFT closing work",),
        recorded_reason="league is stuck [overrode: the draft is not finished.]",
    )
    interaction = world.interaction()

    await _run(cog, "advance-force", interaction, "POST_DRAFT_TRADES", "league is stuck")

    ((name, kwargs),) = world.calls
    assert name == "force"
    assert kwargs["to_phase"] is Phase.POST_DRAFT_TRADES
    assert kwargs["reason"] == "league is stuck"
    assert kwargs["league_id"] == _LEAGUE_ID and kwargs["actor_user_id"] == _COMMISSIONER
    embed = interaction.reply["embed"]
    assert embed.title == "Forced DRAFT -> POST_DRAFT_TRADES"
    fields = {f.name: f.value for f in embed.fields}
    assert "the draft is not finished." in fields["Blockers it overrode"]
    assert "DRAFT closing work" in fields["Work it skipped"]


async def test_force_into_preseason_shows_the_services_refusal_verbatim(world, cog):
    world.raises = PhaseError(season_service.FORCE_PRESEASON_MESSAGE)
    interaction = world.interaction()

    await _run(cog, "advance-force", interaction, "PRESEASON", "restart the season")

    assert interaction.reply["content"] == season_service.FORCE_PRESEASON_MESSAGE
    assert interaction.reply["ephemeral"] is True


async def test_force_with_an_unknown_phase_is_refused_without_calling_the_service(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-force", interaction, "NOT_A_PHASE", "why")

    assert world.calls == []


# --- /advance-unlock -----


async def test_unlock_replies_with_the_services_sentence(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-unlock", interaction)

    ((name, kwargs),) = world.calls
    assert name == "unlock"
    assert kwargs == {"pool": "pool", "league_id": _LEAGUE_ID, "actor_user_id": _COMMISSIONER}
    assert (
        interaction.reply["content"] == "Cleared the advance claim held by user 5 for 12 minute(s)."
    )


async def test_unlock_under_the_age_threshold_is_refused_with_the_services_minutes(world, cog):
    world.raises = ConcurrencyError(
        season_service.unlock_too_young_message(claimed_by=5, age=timedelta(minutes=3))
    )
    interaction = world.interaction()

    await _run(cog, "advance-unlock", interaction)

    assert "3 minute(s) old" in interaction.reply["content"]
    assert "7 minute(s)" in interaction.reply["content"]


async def test_unlock_with_no_claim_says_there_was_nothing_to_clear(world, cog):
    world.results["unlock"] = UnlockResult(
        cleared=False,
        claimed_by=None,
        claimed_at=None,
        age=None,
        hook_completed_cycle_index=-1,
        current_phase=Phase.PLAYIN,
        transaction_id=None,
        message="No advance claim is held, so there is nothing to clear.",
    )
    interaction = world.interaction()

    await _run(cog, "advance-unlock", interaction)

    assert interaction.reply["content"] == "No advance claim is held, so there is nothing to clear."


# --- /advance-postpone-game and /advance-restore-game -----


async def test_postpone_passes_the_ids_and_confirms_the_audited_move(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-postpone-game", interaction, 412, "cannot sim")

    ((name, kwargs),) = world.calls
    assert name == "postpone_game"
    assert kwargs == {
        "pool": "pool",
        "league_id": _LEAGUE_ID,
        "actor_user_id": _COMMISSIONER,
        "game_id": 412,
        "reason": "cannot sim",
    }
    text = interaction.reply["content"]
    assert "412" in text and "postponed" in text and "88" in text
    assert "/advance-restore-game" in text, "the way back is named"


async def test_restore_confirms_and_says_the_next_press_plays_it(world, cog):
    interaction = world.interaction()

    await _run(cog, "advance-restore-game", interaction, 412, "data fixed")

    ((name, kwargs),) = world.calls
    assert name == "restore_game"
    assert kwargs["game_id"] == 412 and kwargs["reason"] == "data fixed"
    text = interaction.reply["content"]
    assert "412" in text and "scheduled" in text and "/advance" in text


@pytest.mark.parametrize("command", ["advance-postpone-game", "advance-restore-game"])
async def test_a_game_somebody_else_already_moved_is_reported_not_claimed(world, cog, command):
    key = _SERVICE[command]
    world.results[key] = _change(changed=False)
    interaction = world.interaction()

    await _run(cog, command, interaction, 412, "why")

    text = interaction.reply["content"]
    assert "nothing changed" in text.lower()
    assert "88" not in text


async def test_each_of_the_three_restore_refusals_reaches_the_person_as_its_own_message(world, cog):
    """P5-99.u.1: the service raises three different `PhaseError`s (wrong phase, the
    season's stats already rebuilt, the league has played past the date). The cog adds no
    wording of its own to any of them, so each arrives as itself -- distinct from the
    others and unaltered."""
    refusals = [
        "game 412 cannot be restored in PLAYIN: a restore re-opens the regular-season "
        "schedule, and the league is not in the regular season",
        "game 412 cannot be restored: the season's stats were already rebuilt at the end of "
        "the regular season",
        "game 412 cannot be restored: the league has already played past its date "
        "(2026-11-01) -- game(s) 9 on a later date are already final",
    ]
    seen = []
    for message in refusals:
        world.raises = PhaseError(message)
        interaction = world.interaction()
        await _run(cog, "advance-restore-game", interaction, 412, "data fixed")
        seen.append(interaction.reply["content"])

    assert seen == refusals
    assert len(set(seen)) == 3


# --- structural -----


def test_the_cog_never_moves_the_pointer_itself():
    import pathlib

    source = pathlib.Path(cog_mod.__file__).read_text(encoding="utf-8")
    assert "phase_service" not in source
    assert "force_advance" not in source
    assert "UPDATE leagues" not in source
