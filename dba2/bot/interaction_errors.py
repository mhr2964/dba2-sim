"""`reply_command_failure` -- the one place an interaction's failure is turned
into an ephemeral reply, shared by every slash command that follows the
"defer, call a service, translate its failure" shape and by
`offer_delivery.OfferActionButton.callback`.

**Why a button cannot lean on `CommandTree.error` the way a command does.**
`discord.py` 2.7.1's `ViewStore.schedule_dynamic_item_call` wraps a
`DynamicItem`'s callback in `try/except Exception: _log.exception(...)`
(`discord/ui/view.py:1038-1041`) and returns -- nothing reaches the human,
who sees Discord's own "This interaction failed". The `CommandTree.error`
backstop `client.py` registers is for app commands only. So the button must
catch and reply itself, and both entry points must agree on what a
`PhaseError` or a `ConcurrencyError` sounds like -- hence one function here
instead of a per-cog copy (docs/design/phase-5-trade-offer-review-surface.md
§1's BL-5 and §13.3: the callback may decide how a failure is WORDED, never
whether an action is ALLOWED).

`KNOWN_DOMAIN_ERRORS` and `GENERIC_FAILURE_MESSAGE` were `cogs/roster.py`'s
private `_KNOWN_DOMAIN_ERRORS`/`_GENERIC_FAILURE_MESSAGE`; `roster.py` now
imports them from here so the tuple has one definition. Lives in `bot/`, not
`services/`, for `post_commit.py`'s own reason: this is Discord-reply UX
shaping, not a service contract.
"""
from __future__ import annotations

import discord

from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.core.logging import get_logger
from dba2.phase.permissions import PermissionDenied

log = get_logger(__name__)

KNOWN_DOMAIN_ERRORS = (
    NotFoundError,
    ValidationError,
    PhaseError,
    PermissionDenied,
    ConcurrencyError,
)
"""Every outcome a service raises ON PURPOSE for a human-caused situation.
`str(exc)` of each is written for a person, so it is shown verbatim."""

GENERIC_FAILURE_MESSAGE = (
    "Something went wrong running that command (error logged) -- no change was made. "
    "Try again, and contact the bot's maintainer if it keeps failing."
)


async def reply_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Send `message` only the invoking user can see. `response.is_done()` picks
    `response.send_message` vs `followup.send` because whether a `defer()` or an
    earlier response already ran is not knowable from here."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def reply_command_failure(
    interaction: discord.Interaction,
    exc: BaseException,
    *,
    log_label: str,
    phase_error_suffix: str = "",
) -> None:
    """Reply ephemerally to `exc`.

    A `KNOWN_DOMAIN_ERRORS` member is shown as `str(exc)`; anything else is
    logged with its traceback (a DB-layer failure -- connection reset, pool
    timeout, deadlock -- is neither, and left uncaught means "deferred forever"
    for the user) and answered with `GENERIC_FAILURE_MESSAGE`.

    `phase_error_suffix` is appended to a `PhaseError` and to nothing else.
    Its one user is the trade-offer surface's "trades are closed in this phase
    and any offer from an earlier phase has expired" sentence (design §5.3,
    NIT 10): `offer_respond`/`counter_offer` hit `require_phase` before the
    staleness check, so the generic "not legal in phase X" is the message a
    human sees for an expired offer, and it is about the command, not the
    offer. Pass it only from those two paths.

    A failure of the reply itself (the interaction expired, Discord is down)
    is NOT caught here: it propagates to the command tree's backstop or to
    `ViewStore`'s catch-all, both of which log it -- swallowing it here would
    add a second, contextless log path for the same fault."""
    if isinstance(exc, KNOWN_DOMAIN_ERRORS):
        message = str(exc)
        if phase_error_suffix and isinstance(exc, PhaseError):
            message = f"{message} {phase_error_suffix}"
    else:
        log.error(
            "%s failed for interaction %s", log_label, interaction.id, exc_info=exc
        )
        message = GENERIC_FAILURE_MESSAGE
    await reply_ephemeral(interaction, message)
