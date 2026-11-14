"""`OfferActionButton` without a database or a gateway (design §1 BL-5/BL-A,
§7.4 test 14).

The two failure modes this class exists to defeat are both silent, which is
why each has a test that would go red for exactly that reason:

- a subclass that forgot to override `callback` inherits
  `DynamicItem.callback`, which delegates to the wrapped `Button`'s no-op
  (`discord/ui/dynamic.py:215-216`) -- a button that acknowledges nothing;
- a callback with no error handling has its exception swallowed by
  `ViewStore` (`discord/ui/view.py:1038-1041`) -- "This interaction failed"
  for every one of `respond`'s five designed outcomes.

Every other test here calls `callback` directly, so NONE of them would notice
the first; the guard tests are the only thing that does.
"""
from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace

import discord
import pytest

from dba2.bot import offer_delivery
from dba2.bot.client import build_bot
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE, KNOWN_DOMAIN_ERRORS
from dba2.bot.offer_delivery import OfferActionButton
from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.trade_offer import TradeOffer
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.offers import EXPIRY_SUFFIX
from dba2.services.offer_service import OfferAction, OfferResult

# --- the two guards --------------------------------------------------------


def test_the_button_overrides_callback_itself():
    """A forgotten override still constructs, still registers, still renders --
    and does nothing when clicked."""
    assert OfferActionButton.callback is not discord.ui.DynamicItem.callback
    assert "callback" in OfferActionButton.__dict__


def test_the_wrapped_button_is_inert():
    """All logic lives on the DynamicItem override (§1); a callback on the
    wrapped Button would split one behaviour across two objects."""
    for action in ("accept", "reject", "counter"):
        inner = OfferActionButton(action, 5).item
        assert type(inner).callback is discord.ui.Item.callback
        assert "callback" not in vars(inner)


def test_a_dynamic_item_without_the_override_would_be_caught_by_the_guard():
    """Mutation twin: the broken shape the guard describes really does pass
    `is not Item.callback` (the assertion round 3 wrote and BL-A corrected) and
    really does fail the corrected one."""

    class Forgot(discord.ui.DynamicItem[discord.ui.Button], template=r"x:(?P<n>\d+)"):
        def __init__(self, n: int) -> None:
            super().__init__(discord.ui.Button(custom_id=f"x:{n}"))

    assert Forgot.callback is not discord.ui.Item.callback  # the wrong guard is fooled
    assert Forgot.callback is discord.ui.DynamicItem.callback  # the right one is not
    assert "callback" not in Forgot.__dict__


# --- custom_id and registration --------------------------------------------


@pytest.mark.parametrize("action", ["accept", "reject", "counter"])
def test_custom_id_round_trips_through_the_template(action):
    button = OfferActionButton(action, 4711)
    assert button.custom_id == f"dba2:offer:{action}:4711"
    match = OfferActionButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
    assert match is not None
    rebuilt = asyncio.run(OfferActionButton.from_custom_id(None, button.item, match))
    assert (rebuilt.action, rebuilt.offer_id) == (action, 4711)
    assert rebuilt.custom_id == button.custom_id


@pytest.mark.parametrize(
    "custom_id",
    [
        "dba2:offer:accept:",
        "dba2:offer:accept:12x",
        "dba2:offer:veto:12",
        "dba2:offer:accept:12:extra",
        "xdba2:offer:accept:12",
        "dba2:offer:accept:-1",
    ],
)
def test_the_template_rejects_anything_that_is_not_exactly_an_offer_button(custom_id):
    """`dispatch_dynamic_items` uses `fullmatch`; an anchor-less template that
    merely `match`es would route `...:12x` to the offer-12 handler."""
    assert OfferActionButton.__discord_ui_compiled_template__.fullmatch(custom_id) is None


def test_the_template_is_the_designed_pattern():
    assert OfferActionButton.__discord_ui_compiled_template__.pattern == (
        r"dba2:offer:(?P<action>accept|reject|counter):(?P<offer_id>\d+)"
    )


def test_an_unknown_action_cannot_be_constructed():
    with pytest.raises(ValueError):
        OfferActionButton("veto", 1)


def test_the_view_holds_three_persistent_buttons_for_one_offer():
    view = offer_delivery.build_offer_view(77)
    assert [c.custom_id for c in view.children] == [
        "dba2:offer:accept:77",
        "dba2:offer:reject:77",
        "dba2:offer:counter:77",
    ]
    assert view.timeout is None and view.is_persistent()


async def test_setup_hook_registers_the_class_with_the_view_store():
    """One registration of the CLASS, not `add_view` per offer (§1). Reads the
    store's private dict -- the only place the registration is observable
    without a gateway."""
    bot = build_bot()
    await bot.setup_hook()
    registered = bot._connection._view_store._dynamic_items
    assert registered[OfferActionButton.__discord_ui_compiled_template__] is OfferActionButton


async def test_the_view_store_routes_an_offer_custom_id_to_the_class_and_ignores_others(
    monkeypatch,
):
    """The real dispatcher, real registration: a matching `custom_id` reaches
    `schedule_dynamic_item_call` with this class; a look-alike does not."""
    bot = build_bot()
    await bot.setup_hook()
    store = bot._connection._view_store
    seen: list[tuple[type, str]] = []

    async def spy(component_type, factory, interaction, custom_id, match):
        seen.append((factory, custom_id))

    monkeypatch.setattr(store, "schedule_dynamic_item_call", spy)
    store.dispatch_dynamic_items(2, "dba2:offer:accept:9", SimpleNamespace())
    store.dispatch_dynamic_items(2, "dba2:offer:accept:9x", SimpleNamespace())
    store.dispatch_dynamic_items(2, "someone-elses-button", SimpleNamespace())
    await asyncio.sleep(0)
    assert seen == [(OfferActionButton, "dba2:offer:accept:9")]


# --- callback behaviour, with a fake interaction ---------------------------


class _Response:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self._order.append(f"defer(ephemeral={ephemeral})")

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        self._done = True
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        self.sent.append({"content": content, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order: list[str] = order if order is not None else []
        self.id = 99
        self.guild_id = 5
        self.user = SimpleNamespace(id=42)
        self.response = _Response(self.order)
        self.followup = _Followup()


@pytest.fixture
def wired(monkeypatch):
    """Patches the two I/O lookups the callback makes before `respond`, and
    returns a recorder for `respond`'s calls."""
    calls: list[dict] = []

    async def fake_pool():
        return "pool"

    async def fake_league(pool, interaction):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(offer_delivery, "get_pool", fake_pool)
    monkeypatch.setattr(offer_delivery, "resolve_league", fake_league)
    box = SimpleNamespace(calls=calls, behaviour=None)

    async def fake_respond(pool, **kwargs):
        interaction_order.append("respond")
        calls.append(kwargs)
        if isinstance(box.behaviour, BaseException):
            raise box.behaviour
        return box.behaviour

    interaction_order: list[str] = []
    box.order = interaction_order
    monkeypatch.setattr(offer_delivery.offer_service, "respond", fake_respond)
    return box


def _offer(status: TradeStatus = TradeStatus.ACCEPTED) -> TradeOffer:
    from datetime import UTC, datetime

    return TradeOffer(
        id=12, league_id=7, season=2026, phase=Phase.TRADE_DEADLINE, from_team_id=1,
        to_team_id=2, proposed_by=None, status=status, package={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _run(button: OfferActionButton, interaction: _Interaction, box) -> None:
    await button.callback(interaction)


_ERRORS = [
    NotFoundError("No such offer."),
    ValidationError("Marcus Webb is not on that roster."),
    PermissionDenied("Only the receiving team's manager may answer that offer."),
    ConcurrencyError("That offer changed while it was being answered."),
]


@pytest.mark.parametrize("exc", _ERRORS, ids=lambda e: type(e).__name__)
async def test_each_known_error_becomes_one_ephemeral_followup_after_defer_and_respond(
    wired, exc
):
    """§7.4 test 14 (a)-(c): defer first, exactly one ephemeral followup,
    containing `str(exc)`. Delete the `try/except` from `callback` and every
    case goes to "no followup at all"."""
    wired.behaviour = exc
    interaction = _Interaction(wired.order)
    await _run(OfferActionButton("accept", 12), interaction, wired)

    assert wired.order == ["defer(ephemeral=True)", "respond"]
    assert len(interaction.followup.sent) == 1
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert str(exc) in interaction.followup.sent[0]["content"]


async def test_a_phase_error_gets_the_expiry_sentence_on_the_button_path_too(wired):
    """The button is the most common way to answer a stale offer, so the
    sentence that makes a phase refusal about the OFFER belongs here as much as
    on `/offer-respond`."""
    wired.behaviour = PhaseError("Not legal in phase post_deadline")
    interaction = _Interaction(wired.order)
    await _run(OfferActionButton("reject", 12), interaction, wired)
    assert interaction.followup.sent[0]["content"] == (
        f"Not legal in phase post_deadline {EXPIRY_SUFFIX}"
    )


async def test_the_five_designed_errors_are_all_covered_by_these_tests():
    tested = {type(e) for e in _ERRORS} | {PhaseError}
    assert tested == set(KNOWN_DOMAIN_ERRORS)


async def test_an_unexpected_error_gets_the_generic_message_and_is_logged(wired, caplog):
    wired.behaviour = RuntimeError("asyncpg connection reset")
    interaction = _Interaction(wired.order)
    with caplog.at_level(logging.ERROR):
        await _run(OfferActionButton("accept", 12), interaction, wired)
    assert interaction.followup.sent == [{"content": GENERIC_FAILURE_MESSAGE, "ephemeral": True}]
    logged = [r for r in caplog.records if "offer button accept #12" in r.getMessage()]
    assert logged and logged[0].exc_info[1] is wired.behaviour


async def test_a_reply_exists_even_though_the_library_swallows_what_escapes(wired):
    """Mimics `ViewStore.schedule_dynamic_item_call`'s catch-all around the
    callback: whatever escapes is swallowed, so the human's reply has to be sent
    before that can happen."""
    wired.behaviour = RuntimeError("boom")
    interaction = _Interaction(wired.order)
    try:
        await _run(OfferActionButton("accept", 12), interaction, wired)
    except Exception:  # noqa: S110 -- what ViewStore does
        pass
    assert len(interaction.followup.sent) == 1


async def test_a_callback_without_the_handler_would_leave_the_human_with_nothing(wired):
    """Mutation twin for the two tests above: the same call with the `try`
    removed raises out of the callback and sends no followup -- which is
    exactly what the swallowing library turns into "This interaction failed"."""

    class NoHandler(OfferActionButton, template=r"dba2:offer:(?P<action>accept):(?P<offer_id>\d+)"):
        async def callback(self, interaction):
            await interaction.response.defer(ephemeral=True)
            await offer_delivery.offer_service.respond(
                "pool", league_id=7, offer_id=self.offer_id,
                action=OfferAction(self.action), actor_user_id=interaction.user.id,
            )

    wired.behaviour = ValidationError("nope")
    interaction = _Interaction(wired.order)
    with pytest.raises(ValidationError):
        await _run(NoHandler("accept", 12), interaction, wired)
    assert interaction.followup.sent == []


@pytest.mark.parametrize(
    ("action", "status", "detail", "changed", "expected"),
    [
        ("accept", TradeStatus.ACCEPTED, None, True, "Offer #12 accepted -- the trade is done."),
        ("reject", TradeStatus.REJECTED, None, True, "Offer #12 rejected."),
        (
            "accept", TradeStatus.EXPIRED, "Marcus Webb is now listed INJURED.", True,
            "Offer #12 has expired -- nothing was traded. Marcus Webb is now listed INJURED.",
        ),
        (
            "accept", TradeStatus.ACCEPTED, None, False,
            "Offer #12 was already accepted -- nothing was changed.",
        ),
    ],
)
async def test_the_success_reply_says_what_happened_and_never_claims_success_for_no_change(
    wired, action, status, detail, changed, expected
):
    wired.behaviour = OfferResult(offer=_offer(status), changed=changed, detail=detail)
    interaction = _Interaction(wired.order)
    await _run(OfferActionButton(action, 12), interaction, wired)
    assert interaction.followup.sent == [{"content": expected, "ephemeral": True}]
    assert wired.calls == [
        {"league_id": 7, "offer_id": 12, "action": OfferAction(action), "actor_user_id": 42}
    ]


async def test_the_counter_button_points_at_the_command_and_calls_no_service(wired):
    interaction = _Interaction(wired.order)
    await _run(OfferActionButton("counter", 12), interaction, wired)
    assert wired.calls == []
    assert len(interaction.response.messages) == 1
    message = interaction.response.messages[0]
    assert message["ephemeral"] is True
    assert re.search(r"/counter-offer offer_id:12", message["content"])
    # `/counter-offer` has no team-code option (the team is the offer's recipient).
    assert "team code" not in message["content"].lower()
    assert interaction.followup.sent == []
