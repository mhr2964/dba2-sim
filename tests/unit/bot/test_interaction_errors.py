"""`dba2/bot/interaction_errors.py` -- the shared failure reply for slash
commands and for `OfferActionButton.callback` (design §1, BL-5).

No gateway: a fake `Interaction` records what `response`/`followup` were
asked to send. Each test states which mutation it would catch.
"""
from __future__ import annotations

import logging

import pytest

from dba2.bot import interaction_errors
from dba2.bot.interaction_errors import (
    GENERIC_FAILURE_MESSAGE,
    KNOWN_DOMAIN_ERRORS,
    reply_command_failure,
    reply_ephemeral,
)
from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.phase.permissions import PermissionDenied


class FakeResponse:
    def __init__(self, *, done: bool) -> None:
        self._done = done
        self.sent: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, ephemeral: bool = False, **kw) -> None:
        self._done = True
        self.sent.append({"content": content, "ephemeral": ephemeral})


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral: bool = False, **kw) -> None:
        self.sent.append({"content": content, "ephemeral": ephemeral})


class FakeInteraction:
    def __init__(self, *, deferred: bool) -> None:
        self.id = 4711
        self.response = FakeResponse(done=deferred)
        self.followup = FakeFollowup()


_DOMAIN_ERRORS = [
    NotFoundError("No such offer."),
    ValidationError("Player 7 is not on that roster."),
    PhaseError("Not legal in phase post_deadline"),
    PermissionDenied("Only the team's manager may do that."),
    ConcurrencyError("That team/player is being updated by another action right now."),
]


def test_known_domain_errors_is_exactly_the_five_the_design_names():
    """Adding a sixth here would silently show its `str()` to users."""
    assert set(KNOWN_DOMAIN_ERRORS) == {
        NotFoundError,
        ValidationError,
        PhaseError,
        PermissionDenied,
        ConcurrencyError,
    }


@pytest.mark.parametrize("exc", _DOMAIN_ERRORS, ids=lambda e: type(e).__name__)
async def test_each_domain_error_is_shown_verbatim_and_ephemeral(exc):
    """After a `defer()` the reply must be a followup (a second
    `response.send_message` would raise `InteractionResponded`)."""
    interaction = FakeInteraction(deferred=True)
    await reply_command_failure(interaction, exc, log_label="/x")
    assert interaction.followup.sent == [{"content": str(exc), "ephemeral": True}]
    assert interaction.response.sent == []


async def test_before_defer_the_reply_uses_response_send_message():
    interaction = FakeInteraction(deferred=False)
    await reply_command_failure(interaction, NotFoundError("gone"), log_label="/x")
    assert interaction.response.sent == [{"content": "gone", "ephemeral": True}]
    assert interaction.followup.sent == []


async def test_unknown_exception_gets_the_generic_message_and_is_logged(caplog):
    """The message must NOT carry `str(exc)` -- a raw asyncpg/KeyError text is
    not for users -- and the traceback must be logged with the label."""
    interaction = FakeInteraction(deferred=True)
    boom = RuntimeError("secret internals")
    with caplog.at_level(logging.ERROR, logger=interaction_errors.log.name):
        await reply_command_failure(interaction, boom, log_label="/offer-respond")
    assert interaction.followup.sent == [
        {"content": GENERIC_FAILURE_MESSAGE, "ephemeral": True}
    ]
    assert "secret internals" not in interaction.followup.sent[0]["content"]
    records = [r for r in caplog.records if "/offer-respond" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is boom


async def test_domain_errors_are_not_logged_as_failures(caplog):
    interaction = FakeInteraction(deferred=True)
    with caplog.at_level(logging.ERROR, logger=interaction_errors.log.name):
        await reply_command_failure(interaction, PhaseError("closed"), log_label="/x")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


_SUFFIX = "Trades are closed in this phase."


async def test_phase_error_suffix_is_appended_to_a_phase_error_only():
    interaction = FakeInteraction(deferred=True)
    await reply_command_failure(
        interaction, PhaseError("Not legal now."), log_label="/x", phase_error_suffix=_SUFFIX
    )
    assert interaction.followup.sent[0]["content"] == f"Not legal now. {_SUFFIX}"


@pytest.mark.parametrize(
    "exc",
    [e for e in _DOMAIN_ERRORS if not isinstance(e, PhaseError)]
    + [RuntimeError("unrelated")],
    ids=lambda e: type(e).__name__,
)
async def test_phase_error_suffix_is_never_appended_to_any_other_error(exc):
    """The suffix says "trades are closed / your offer expired" -- true only
    of a phase refusal. On a PermissionDenied it would be a false statement."""
    interaction = FakeInteraction(deferred=True)
    await reply_command_failure(interaction, exc, log_label="/x", phase_error_suffix=_SUFFIX)
    assert _SUFFIX not in interaction.followup.sent[0]["content"]


async def test_no_suffix_by_default():
    interaction = FakeInteraction(deferred=True)
    await reply_command_failure(interaction, PhaseError("Not legal now."), log_label="/x")
    assert interaction.followup.sent[0]["content"] == "Not legal now."


async def test_reply_ephemeral_picks_followup_once_the_interaction_is_done():
    done = FakeInteraction(deferred=True)
    await reply_ephemeral(done, "hi")
    assert done.followup.sent == [{"content": "hi", "ephemeral": True}]
    fresh = FakeInteraction(deferred=False)
    await reply_ephemeral(fresh, "hi")
    assert fresh.response.sent == [{"content": "hi", "ephemeral": True}]
