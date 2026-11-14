"""`dba2/bot/cogs/offers.py` command wiring, with no database and no gateway
(design §4, §8, §12 step 8). `offer_service` and the two lookups the cog makes
before it are replaced, so what is asserted is exactly the cog's own job:
which service function each command calls with which arguments, what the
human is told, and that the cog decides no rule of its own.

The end-to-end behaviour against real Postgres is in
`tests/integration/test_offers_cog.py`.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dba2.bot import client as bot_client
from dba2.bot.cogs import offers as cog_mod
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.bot.offer_delivery import Delivery
from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.trade_offer import TradeOffer
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.offers import EXPIRY_SUFFIX, OfferNames
from dba2.services.offer_service import OfferAction, OfferResult, TeamOffers

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

_MANAGER = 42
_COMMISSIONER = 1


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.deferred: list[bool] = []
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.deferred.append(ephemeral)

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        self._done = True
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral: bool = False, **_kw) -> None:
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, user_id: int = _MANAGER) -> None:
        self.id = 1
        self.guild_id = 9
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()

    @property
    def reply(self) -> str:
        """The single thing the human saw, wherever it was sent."""
        seen = self.response.messages + self.followup.sent
        assert len(seen) == 1, seen
        return seen[0]["content"]


def _team(team_id: int, code: str, manager: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=team_id, code=code, city=f"{code} City", name=code, manager_user_id=manager,
        league_id=7,
    )


def _offer(offer_id=12, *, status=TradeStatus.PROPOSED, counters=None, to_team=2) -> TradeOffer:
    return TradeOffer(
        id=offer_id, league_id=7, season=2026, phase=Phase.TRADE_DEADLINE, from_team_id=1,
        to_team_id=to_team, proposed_by=_MANAGER, status=status,
        package={"players_from_to": (1,), "players_to_from": (2,), "picks_from_to": (),
                 "picks_to_from": ()},
        created_at=datetime(2026, 1, 1, tzinfo=UTC), counters_offer_id=counters,
    )


class _World:
    """The patched surroundings. `service` records every call; set
    `.raises` / `.result` per test."""

    def __init__(self, monkeypatch, *, phase=Phase.TRADE_DEADLINE) -> None:
        self.league = SimpleNamespace(
            id=7, current_phase=phase, commissioner_user_id=_COMMISSIONER
        )
        self.teams = {"BOS": _team(1, "BOS", _MANAGER), "NYK": _team(2, "NYK", 77)}
        self.calls: list[tuple[str, dict]] = []
        self.raises: BaseException | None = None
        self.result: object = OfferResult(offer=_offer(), changed=True)
        self.delivered: list[int] = []
        self.delivery_outcome: object = Delivery.DELIVERED
        self.drains: list[int] = []
        self.drain_raises: BaseException | None = None
        self.names_raises: BaseException | None = None

        async def get_pool():
            return "pool"

        async def resolve_league(pool, interaction):
            return self.league

        async def team_by_code(pool, league_id, code):
            return self.teams.get(code)

        def service(name):
            async def call(pool, **kwargs):
                self.calls.append((name, kwargs))
                if self.raises is not None:
                    raise self.raises
                return self.result

            return call

        async def deliver(bot, pool, offer_id):
            self.delivered.append(offer_id)
            if isinstance(self.delivery_outcome, BaseException):
                raise self.delivery_outcome
            return self.delivery_outcome

        async def drain(bot, pool, *, league_id):
            self.drains.append(league_id)
            if self.drain_raises is not None:
                raise self.drain_raises

        async def names(pool, offers):
            if self.names_raises is not None:
                raise self.names_raises
            return OfferNames(
                teams={t.id: t for t in self.teams.values()}, players={}, picks={}
            )

        monkeypatch.setattr(cog_mod, "get_pool", get_pool)
        monkeypatch.setattr(cog_mod, "resolve_league", resolve_league)
        monkeypatch.setattr(cog_mod.team_repo, "get_team_by_code", team_by_code)
        for fn in ("propose", "respond", "counter", "veto", "list_offers"):
            monkeypatch.setattr(cog_mod.offer_service, fn, service(fn))
        monkeypatch.setattr(cog_mod, "deliver_offer", deliver)
        monkeypatch.setattr(cog_mod, "drain_undelivered_offers", drain)
        monkeypatch.setattr(cog_mod, "load_offer_names", names)


@pytest.fixture
def world(monkeypatch):
    return _World(monkeypatch)


@pytest.fixture
def cog():
    return cog_mod.OfferCommands(bot=SimpleNamespace())


async def _propose(cog, interaction) -> None:
    await cog.propose_trade.callback(
        cog, interaction, "BOS", "NYK", send_player_1=1, get_player_1=2
    )


_ACTION = {"accept": SimpleNamespace(value="accept"), "reject": SimpleNamespace(value="reject")}


# --- registration ----------------------------------------------------------


def test_the_cog_is_registered_in_the_bot_extensions():
    assert "dba2.bot.cogs.offers" in bot_client._EXTENSIONS


async def test_the_five_commands_exist_with_the_designed_options():
    bot = bot_client.build_bot()
    await bot.setup_hook()
    commands_by_name = {c.name: c for c in bot.tree.get_commands()}

    def options(name):
        return [p.name for p in commands_by_name[name].parameters]

    propose = options("propose-trade")
    assert propose[:2] == ["code", "partner"]
    assert len(propose) == 12  # design §4.1: twelve options, well inside Discord's 25
    assert set(propose[2:]) == {
        "send_player_1", "send_player_2", "send_player_3", "get_player_1", "get_player_2",
        "get_player_3", "send_pick_1", "send_pick_2", "get_pick_1", "get_pick_2",
    }
    assert "counters" not in propose  # BL-2: /propose-trade creates root offers only
    assert options("offers") == ["code"]
    assert options("offer-respond") == ["offer_id", "action"]
    counter = options("counter-offer")
    assert counter[0] == "offer_id" and "code" not in counter and len(counter) == 11
    assert options("veto-offer") == ["offer_id"]
    respond_choices = commands_by_name["offer-respond"].parameters[1].choices
    assert [c.value for c in respond_choices] == ["accept", "reject"]


# --- /propose-trade --------------------------------------------------------


async def test_propose_maps_send_to_from_to_and_get_to_to_from_and_drops_unset_slots(
    world, cog
):
    interaction = _Interaction()
    await cog.propose_trade.callback(
        cog, interaction, "bos", "nyk",
        send_player_1=10, send_player_3=30, get_player_2=21, send_pick_2=52, get_pick_1=61,
    )
    (name, kwargs), = world.calls
    assert name == "propose"
    assert kwargs == {
        "league_id": 7, "from_team_id": 1, "to_team_id": 2,
        "players_from_to": (10, 30), "players_to_from": (21,),
        "picks_from_to": (52,), "picks_to_from": (61,), "actor_user_id": _MANAGER,
    }
    assert interaction.response.deferred == [True]  # ephemeral defer


async def test_propose_reply_is_ephemeral_with_the_offer_panel(world, cog):
    interaction = _Interaction()
    await _propose(cog, interaction)
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["content"] == "Offer #12 sent; it is waiting for NYK's manager."
    assert sent["embed"] is not None and "Trade offer #12" in sent["embed"].title
    assert world.delivered == [12]


async def test_a_non_manager_is_refused_before_defer_and_the_service_is_never_called(world, cog):
    interaction = _Interaction(user_id=555)
    await _propose(cog, interaction)
    assert world.calls == []
    assert interaction.response.deferred == []
    assert interaction.response.messages[0]["ephemeral"] is True


async def test_the_commissioner_who_does_not_manage_the_team_is_pointed_at_veto(world, cog):
    interaction = _Interaction(user_id=_COMMISSIONER)
    await _propose(cog, interaction)
    assert world.calls == []
    assert "/veto-offer" in interaction.reply


async def test_propose_in_a_closed_phase_is_refused_before_defer(monkeypatch, cog):
    world = _World(monkeypatch, phase=Phase.POST_DEADLINE)
    interaction = _Interaction()
    await _propose(cog, interaction)
    assert world.calls == []
    assert "not available during POST_DEADLINE" in interaction.reply


@pytest.mark.parametrize(("code", "partner", "missing"), [("ZZZ", "NYK", "team"),
                                                         ("BOS", "ZZZ", "partner team")])
async def test_an_unknown_team_code_is_a_not_found_message(world, cog, code, partner, missing):
    interaction = _Interaction()
    await cog.propose_trade.callback(cog, interaction, code, partner, send_player_1=1)
    assert world.calls == []
    assert f"No {missing} with code 'ZZZ'" in interaction.reply


async def test_propose_never_appends_the_expiry_sentence(world, cog):
    """A proposal has no offer that could have expired (§5.3, NIT 10)."""
    world.raises = PhaseError("Not legal in phase post_deadline")
    interaction = _Interaction()
    await _propose(cog, interaction)
    assert interaction.reply == "Not legal in phase post_deadline"


async def test_a_validation_error_from_the_service_is_shown_verbatim_after_defer(world, cog):
    world.raises = ValidationError("Id 10 is listed twice under players you send.")
    interaction = _Interaction()
    await cog.propose_trade.callback(cog, interaction, "BOS", "NYK", send_player_1=10)
    assert interaction.reply == "Id 10 is listed twice under players you send."
    assert interaction.response.deferred == [True]


async def test_an_unexpected_service_failure_gets_the_generic_message(world, cog):
    world.raises = RuntimeError("pool exploded")
    interaction = _Interaction()
    await cog.propose_trade.callback(cog, interaction, "BOS", "NYK", send_player_1=10)
    assert interaction.reply == GENERIC_FAILURE_MESSAGE


async def test_an_ai_answer_is_reported_from_the_status_not_as_a_sent_offer(world, cog):
    world.result = OfferResult(
        offer=_offer(status=TradeStatus.REJECTED), changed=True,
        detail="NYK is not interested in that deal.",
    )
    interaction = _Interaction()
    await _propose(cog, interaction)
    assert interaction.reply == "Offer #12 was rejected: NYK is not interested in that deal."
    assert world.delivered == []  # nothing is waiting on a human


async def test_delivery_failure_after_a_committed_propose_is_disclosed_not_hidden(world, cog):
    """The write is real; a channel that could not be posted to must read as
    "sent, but ..." -- never as a failure of the propose."""
    world.delivery_outcome = Delivery.NO_GM_CHANNEL
    interaction = _Interaction()
    await _propose(cog, interaction)
    sent = interaction.followup.sent[0]["content"]
    assert sent.startswith("Offer #12 sent;")
    assert "The change went through, but their GM channel couldn't be notified yet" in sent


async def test_a_delivery_that_raises_is_still_only_a_disclosure(world, cog):
    world.delivery_outcome = RuntimeError("discord down")
    interaction = _Interaction()
    await _propose(cog, interaction)
    assert "The change went through" in interaction.followup.sent[0]["content"]


async def test_an_unrenderable_panel_is_disclosed_and_the_offer_still_reported(world, cog):
    world.names_raises = RuntimeError("db hiccup")
    interaction = _Interaction()
    await _propose(cog, interaction)
    sent = interaction.followup.sent[0]
    assert sent["embed"] is None
    assert sent["content"].startswith("Offer #12 sent;")
    assert "the offer panel couldn't be shown" in sent["content"]


# --- /offer-respond --------------------------------------------------------


@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_respond_passes_the_action_and_the_actor_and_league_from_the_guild(
    world, cog, action
):
    world.result = OfferResult(offer=_offer(status=TradeStatus.ACCEPTED), changed=True)
    interaction = _Interaction()
    await cog.offer_respond.callback(cog, interaction, 12, _ACTION[action])
    (name, kwargs), = world.calls
    assert name == "respond"
    assert kwargs == {
        "league_id": 7, "offer_id": 12, "action": OfferAction(action), "actor_user_id": _MANAGER,
    }
    assert interaction.response.deferred == [True]


async def test_respond_says_already_answered_and_never_claims_success(world, cog):
    world.result = OfferResult(
        offer=_offer(status=TradeStatus.ACCEPTED), changed=False,
        detail="That offer is already accepted.",
    )
    interaction = _Interaction()
    await cog.offer_respond.callback(cog, interaction, 12, _ACTION["accept"])
    assert interaction.reply == "Offer #12 was already accepted -- nothing was changed."


async def test_respond_shows_the_reason_an_offer_expired(world, cog):
    world.result = OfferResult(
        offer=_offer(status=TradeStatus.EXPIRED), changed=True,
        detail="Marcus Webb is now listed INJURED; this deal can no longer be priced.",
    )
    interaction = _Interaction()
    await cog.offer_respond.callback(cog, interaction, 12, _ACTION["accept"])
    assert "expired -- nothing was traded" in interaction.reply
    assert "Marcus Webb is now listed INJURED" in interaction.reply


@pytest.mark.parametrize("command", ["offer_respond", "counter_offer"])
async def test_only_respond_and_counter_append_the_expiry_sentence_to_a_phase_error(
    world, cog, command
):
    world.raises = PhaseError("Not legal in phase post_deadline; requires one of: [...]")
    interaction = _Interaction()
    args = (12, _ACTION["accept"]) if command == "offer_respond" else (12,)
    await getattr(cog, command).callback(cog, interaction, *args)
    assert interaction.reply.endswith(EXPIRY_SUFFIX)
    assert interaction.reply.startswith("Not legal in phase post_deadline")


@pytest.mark.parametrize(
    "exc",
    [NotFoundError("No such offer."), PermissionDenied("Only the manager may."),
     ConcurrencyError("try again"), ValidationError("nope")],
    ids=lambda e: type(e).__name__,
)
async def test_the_expiry_sentence_is_not_appended_to_any_other_error(world, cog, exc):
    world.raises = exc
    interaction = _Interaction()
    await cog.offer_respond.callback(cog, interaction, 12, _ACTION["accept"])
    assert interaction.reply == str(exc)


async def test_respond_does_not_pre_check_the_phase_so_the_service_can_explain_it(
    monkeypatch, cog
):
    """In a closed phase the SERVICE's PhaseError must be what the human gets:
    a bot-layer `ensure_command_legal` would answer first with a message about
    the command and the expiry sentence would never appear (§5.3, NIT 10)."""
    world = _World(monkeypatch, phase=Phase.POST_DEADLINE)
    world.raises = PhaseError("Not legal in phase post_deadline")
    interaction = _Interaction()
    await cog.offer_respond.callback(cog, interaction, 12, _ACTION["accept"])
    assert [n for n, _ in world.calls] == ["respond"]
    assert interaction.reply.endswith(EXPIRY_SUFFIX)


# --- /counter-offer --------------------------------------------------------


async def test_counter_takes_no_team_and_maps_slots_from_the_countering_teams_side(world, cog):
    world.result = OfferResult(offer=_offer(offer_id=13, counters=12), changed=True)
    interaction = _Interaction()
    await cog.counter_offer.callback(
        cog, interaction, 12, send_player_2=22, get_player_1=11, get_pick_2=62
    )
    (name, kwargs), = world.calls
    assert name == "counter"
    assert kwargs == {
        "league_id": 7, "offer_id": 12, "actor_user_id": _MANAGER,
        "players_from_to": (22,), "players_to_from": (11,),
        "picks_from_to": (), "picks_to_from": (62,),
    }
    assert interaction.reply == (
        "Countered offer #12 with offer #13; it is waiting for NYK's manager."
    )
    assert world.delivered == [13]  # the NEW row is what gets posted


async def test_a_stale_counter_reports_the_expiry_and_delivers_nothing(world, cog):
    world.result = OfferResult(
        offer=_offer(status=TradeStatus.EXPIRED), changed=True,
        detail="That offer was made during trade_deadline; the league has moved on.",
    )
    interaction = _Interaction()
    await cog.counter_offer.callback(cog, interaction, 12, send_player_1=1, get_player_1=2)
    assert interaction.followup.sent[0]["content"].startswith("Offer #12 has expired")
    assert world.delivered == []


async def test_a_counter_that_is_already_answered_says_so(world, cog):
    world.result = OfferResult(offer=_offer(status=TradeStatus.REJECTED), changed=False)
    interaction = _Interaction()
    await cog.counter_offer.callback(cog, interaction, 12, send_player_1=1, get_player_1=2)
    assert interaction.followup.sent[0]["content"] == (
        "Offer #12 was already rejected -- nothing was changed."
    )
    assert world.delivered == []


# --- /veto-offer -----------------------------------------------------------


async def test_veto_by_a_non_commissioner_is_refused_before_the_service(world, cog):
    interaction = _Interaction(user_id=_MANAGER)
    await cog.veto_offer.callback(cog, interaction, 12)
    assert world.calls == []
    assert interaction.response.deferred == []
    assert "commissioner" in interaction.reply.lower()


async def test_veto_calls_the_service_as_the_commissioner_and_reports_the_result(world, cog):
    world.result = OfferResult(offer=_offer(status=TradeStatus.VETOED), changed=True)
    interaction = _Interaction(user_id=_COMMISSIONER)
    await cog.veto_offer.callback(cog, interaction, 12)
    (name, kwargs), = world.calls
    assert (name, kwargs) == (
        "veto", {"league_id": 7, "offer_id": 12, "actor_user_id": _COMMISSIONER}
    )
    assert interaction.reply == "Offer #12 vetoed."


async def test_veto_works_in_every_phase_including_a_closed_one(monkeypatch, cog):
    """`veto_offer` is `ALL_PHASES` (§8): the escape hatch must not be blocked
    by the phase that made the offer bad."""
    world = _World(monkeypatch, phase=Phase.PLAYOFFS_R1)
    world.result = OfferResult(offer=_offer(status=TradeStatus.VETOED), changed=True)
    interaction = _Interaction(user_id=_COMMISSIONER)
    await cog.veto_offer.callback(cog, interaction, 12)
    assert interaction.reply == "Offer #12 vetoed."


async def test_veto_that_lost_the_race_says_already_answered(world, cog):
    world.result = OfferResult(
        offer=_offer(status=TradeStatus.ACCEPTED), changed=False,
        detail="That offer is already accepted.",
    )
    interaction = _Interaction(user_id=_COMMISSIONER)
    await cog.veto_offer.callback(cog, interaction, 12)
    assert interaction.reply == "Offer #12 was already accepted -- nothing was changed."


# --- /offers ---------------------------------------------------------------


async def test_offers_lists_via_the_service_replies_ephemerally_then_drains(world, cog):
    world.result = TeamOffers(team_id=1, received=(_offer(),), sent=())
    interaction = _Interaction()
    await cog.offers.callback(cog, interaction, "BOS")
    (name, kwargs), = world.calls
    assert (name, kwargs) == (
        "list_offers", {"league_id": 7, "team_id": 1, "actor_user_id": _MANAGER}
    )
    assert interaction.response.deferred == [True]
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True and "pending trade offers" in sent["embed"].title
    assert world.drains == [7]


async def test_offers_drain_runs_after_the_reply_so_a_slow_drain_never_delays_the_inbox(
    world, cog
):
    order: list[str] = []
    world.result = TeamOffers(team_id=1, received=(), sent=())
    interaction = _Interaction()
    real_send = interaction.followup.send

    async def send(*a, **kw):
        order.append("reply")
        await real_send(*a, **kw)

    interaction.followup.send = send
    original = cog_mod.drain_undelivered_offers

    async def drain(bot, pool, *, league_id):
        order.append("drain")

    cog_mod.drain_undelivered_offers = drain
    try:
        await cog.offers.callback(cog, interaction, "BOS")
    finally:
        cog_mod.drain_undelivered_offers = original
    assert order == ["reply", "drain"]


async def test_a_failing_drain_never_fails_offers(world, cog):
    world.result = TeamOffers(team_id=1, received=(), sent=())
    world.drain_raises = RuntimeError("discord down")
    interaction = _Interaction()
    await cog.offers.callback(cog, interaction, "BOS")
    assert len(interaction.followup.sent) == 1  # the inbox was still delivered


async def test_offers_by_a_stranger_is_refused_before_defer_and_the_service_not_called(
    world, cog
):
    interaction = _Interaction(user_id=555)
    await cog.offers.callback(cog, interaction, "BOS")
    assert world.calls == []
    assert interaction.response.deferred == []
    assert world.drains == []


async def test_the_commissioner_may_read_any_teams_offers(world, cog):
    """`/offers` keeps the wider predicate (§9): a read is not the binding act."""
    world.result = TeamOffers(team_id=2, received=(), sent=())
    interaction = _Interaction(user_id=_COMMISSIONER)
    await cog.offers.callback(cog, interaction, "NYK")
    assert [n for n, _ in world.calls] == ["list_offers"]


async def test_a_service_error_from_offers_is_shown_and_no_drain_runs(world, cog):
    world.raises = PermissionDenied("nope")
    interaction = _Interaction()
    await cog.offers.callback(cog, interaction, "BOS")
    assert interaction.reply == "nope"
    assert world.drains == []


# --- discipline ------------------------------------------------------------

_BOT_DIR = Path(cog_mod.__file__).resolve().parents[1]
_DBA2_DIR = _BOT_DIR.parent


def test_no_bot_module_opens_a_gate_or_takes_a_roster_lock():
    """Only `offer_service`'s four entry points may open a league gate (check 6
    of the discipline suite); the Discord layer decides wording, not locking.

    Named for the roster lock and the gate by name (`roster_lock`,
    `open_league_gate`) rather than a bare `.acquire(`: `bot/broadcast.py` and
    `bot/watch_pager.py` now hold an ordinary `asyncio.Lock` (`ComponentState.
    edit_lock`) to order a /watch cycle click's edit against the interrupted
    panel's (docs/design/watch-v4-live-ticker.md T-5.d, "Ordering") -- a
    Discord-UI concern with no relationship to the roster lock or any league
    gate, and a bare substring match on `.acquire(` would flag any future
    asyncio synchronization primitive in bot/ the same way, with no way to
    tell the two apart."""
    pattern = re.compile(r"open_league_gate|roster_lock")
    offenders = [
        str(p.relative_to(_DBA2_DIR))
        for p in sorted(_BOT_DIR.rglob("*.py"))
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_the_stale_offer_sweep_is_reached_from_list_offers_and_the_press_and_nowhere_else():
    """§6.4: the sweep is forbidden inside respond/propose/counter (it is a
    league-wide tier-3 write and would invert the lock order), so it has
    exactly two call sites outside the repo that defines it: `list_offers`, and
    Stage E's press, which runs it on the POOL after the pointer-move transaction
    has committed (phase-5-stage-e-advance.md P5-99.c, F4) -- never inside one."""
    callers = [
        str(p.relative_to(_DBA2_DIR))
        for p in sorted(_DBA2_DIR.rglob("*.py"))
        if "expire_stale_offers_for_league(" in p.read_text(encoding="utf-8")
        and p.name != "offer_repo.py"
    ]
    assert callers == [
        str(Path("services") / "offer_service.py"),
        str(Path("services") / "season_service.py"),
    ]
    source = (_DBA2_DIR / "services" / "offer_service.py").read_text(encoding="utf-8")
    body = source.split("async def list_offers", 1)[1]
    assert "expire_stale_offers_for_league(" in body
    assert source.count("expire_stale_offers_for_league(") == 1
    press_source = (_DBA2_DIR / "services" / "season_service.py").read_text(encoding="utf-8")
    assert press_source.count("expire_stale_offers_for_league(") == 1
    assert "expire_stale_offers_for_league(pool, league_id)" in press_source, (
        "the press hands the sweep the pool, not a connection: a connection would "
        "nest it as a savepoint inside the caller's locks"
    )
