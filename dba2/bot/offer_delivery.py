"""The Discord half of a trade offer's life outside slash commands: posting an
offer to its receiving team's GM channel (the delivery drain), and the
Accept / Reject / Counter buttons on that message (`OfferActionButton`).
docs/design/phase-5-trade-offer-review-surface.md §1, §6.5, §7.1, §12 step 9.

**Delivery is a queue read from Postgres, never a callback (§6.5).** An offer
row with `status = 'proposed' AND message_id IS NULL` IS the undelivered set;
the drain posts it and writes `channel_id`/`message_id` back. It is
at-least-once and says so: a crash between the Discord post and the write-back
re-posts that offer on the next drain, and the alternative (a pre-claim write)
would buy a row that claims delivery it did not make. A duplicate notification
cannot double-apply anything -- the action behind every button is CAS-guarded
and the second click loses. All of this runs strictly OUTSIDE any database
transaction (P5-14): the drain holds no lock while it talks to Discord.

**The button carries its offer id in its `custom_id`, and nothing else lives
anywhere.** `discord.py`'s `ViewStore` looks a plain `View` item up by exact
`(type, custom_id)` (`discord/ui/view.py:1063-1077`), which would need one
registration per offer id. `DynamicItem` is matched by `pattern.fullmatch` and
built fresh per click (`view.py:1043-1052`), so the CLASS is registered once in
`DBABot.setup_hook` and nothing about any offer survives in memory -- which is
§7.1 property 1, inherited from the library rather than kept by discipline.

**All button logic is in `OfferActionButton.callback`, and the wrapped
`discord.ui.Button` is inert** (§1, BL-A). `DynamicItem.callback` exists and
delegates to the wrapped item (`discord/ui/dynamic.py:215-216`), and a `Button`
built without a callback falls through to `Item.callback`'s `pass` -- so a
subclass that forgot the override silently does nothing. The tests pin
`"callback" in OfferActionButton.__dict__` and `is not DynamicItem.callback`.
The library also swallows anything the callback raises
(`view.py:1038-1041`), so the callback catches and replies itself, through the
same `reply_command_failure` the slash commands use.

**The callback may decide how a failure is WORDED; it never decides whether an
action is ALLOWED (§13.3).** Authority is re-derived from the database by
`offer_service.respond` on every click -- never from the message the button
sits on, its author, or who can see it -- so a non-manager who somehow clicks
gets `respond`'s `PermissionDenied`, shown ephemerally.
"""
from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass

import discord

from dba2.core.logging import get_logger
from dba2.data.db import Executor, get_pool
from dba2.data.repositories import channel_repo, draft_repo, offer_repo, player_repo, team_repo
from dba2.domain.draft import DraftPick
from dba2.domain.phase import TradeStatus
from dba2.domain.player import Player
from dba2.domain.team import Team
from dba2.domain.trade_offer import TradeOffer
from dba2.presentation.offers import (
    EXPIRY_SUFFIX,
    OfferNames,
    build_counter_pointer_text,
    build_offer_notification_text,
    build_offer_panel,
    describe_offer_result,
)
from dba2.services import offer_service
from dba2.services.offer_service import OfferAction

from .context import NoLeagueForGuild, resolve_league
from .embeds import panel_to_embed
from .interaction_errors import reply_command_failure, reply_ephemeral

log = get_logger(__name__)

_BUTTON_LOOK = {
    "accept": ("Accept", discord.ButtonStyle.success),
    "reject": ("Reject", discord.ButtonStyle.danger),
    "counter": ("Counter", discord.ButtonStyle.secondary),
}
_ACTIONS = tuple(_BUTTON_LOOK)


class OfferActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dba2:offer:(?P<action>accept|reject|counter):(?P<offer_id>\d+)",
):
    """One of an offer's three buttons. Constructed by the drain to post, and
    by `from_custom_id` on every click; the instance is thrown away after."""

    def __init__(self, action: str, offer_id: int) -> None:
        if action not in _BUTTON_LOOK:
            raise ValueError(f"unknown offer action {action!r}")
        label, style = _BUTTON_LOOK[action]
        # Deliberately no callback on the wrapped Button -- see module docstring.
        super().__init__(
            discord.ui.Button(
                label=label, style=style, custom_id=f"dba2:offer:{action}:{offer_id}"
            )
        )
        self.action = action
        self.offer_id = offer_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> OfferActionButton:
        return cls(match["action"], int(match["offer_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "counter":
            # A counter is four id lists; a button cannot carry them, and
            # `/counter-offer` is the one way to make one (§4.5).
            await interaction.response.send_message(
                build_counter_pointer_text(self.offer_id), ephemeral=True
            )
            return

        # First: `from_custom_id` and this callback share Discord's 3 second
        # budget and `respond` holds locks across a `load_gm_inputs`.
        await interaction.response.defer(ephemeral=True)
        label = f"offer button {self.action} #{self.offer_id}"
        try:
            pool = await get_pool()
            league = await resolve_league(pool, interaction)
            result = await offer_service.respond(
                pool,
                league_id=league.id,
                offer_id=self.offer_id,
                action=OfferAction(self.action),
                actor_user_id=interaction.user.id,
            )
        except NoLeagueForGuild as exc:
            await reply_ephemeral(interaction, str(exc))
            return
        except Exception as exc:
            await reply_command_failure(
                interaction, exc, log_label=label, phase_error_suffix=EXPIRY_SUFFIX
            )
            return
        # The write above is committed; only a failure to TELL them can happen
        # from here, and `ViewStore` logs it -- it must not read as "no change".
        await interaction.followup.send(
            describe_offer_result(
                result.offer, changed=result.changed, detail=result.detail, action=self.action
            ),
            ephemeral=True,
        )


def build_offer_view(offer_id: int) -> discord.ui.View:
    """The three buttons for one message. Built to be sent and discarded --
    it is never registered with the bot (§1); only `OfferActionButton`, the
    class, is."""
    view = discord.ui.View(timeout=None)
    for action in _ACTIONS:
        view.add_item(OfferActionButton(action, offer_id))
    return view


async def load_offer_names(ex: Executor, offers: Sequence[TradeOffer]) -> OfferNames:
    """Read every team, player and pick the given offers mention, for
    `presentation.offers`. An id that no longer resolves is simply absent (the
    builders render it as `player #7`)."""
    team_ids: set[int] = set()
    player_ids: set[int] = set()
    pick_ids: set[int] = set()
    for offer in offers:
        team_ids.update((offer.from_team_id, offer.to_team_id))
        player_ids.update(offer.package.get("players_from_to", ()))
        player_ids.update(offer.package.get("players_to_from", ()))
        pick_ids.update(offer.package.get("picks_from_to", ()))
        pick_ids.update(offer.package.get("picks_to_from", ()))
    teams: dict[int, Team] = {}
    for team_id in sorted(team_ids):
        team = await team_repo.get_team(ex, team_id)
        if team is not None:
            teams[team_id] = team
    players: dict[int, Player] = {}
    for player_id in sorted(player_ids):
        player = await player_repo.get_player(ex, player_id)
        if player is not None:
            players[player_id] = player
    picks: dict[int, DraftPick] = {}
    for pick_id in sorted(pick_ids):
        pick = await draft_repo.get_draft_pick(ex, pick_id)
        if pick is not None:
            picks[pick_id] = pick
    return OfferNames(teams=teams, players=players, picks=picks)


class Delivery(enum.Enum):
    DELIVERED = "delivered"
    NOT_PENDING = "not_pending"
    """Resolved, or already delivered, by the time the drain got to it."""
    NO_HUMAN_RECIPIENT = "no_human_recipient"
    NO_GM_CHANNEL = "no_gm_channel"
    CHANNEL_UNREACHABLE = "channel_unreachable"
    FAILED = "failed"


@dataclass(frozen=True)
class DrainReport:
    outcomes: dict[int, Delivery]
    """offer id -> what happened to it."""

    @property
    def delivered(self) -> int:
        return sum(1 for d in self.outcomes.values() if d is Delivery.DELIVERED)


async def deliver_offer(bot: discord.Client, ex: Executor, offer_id: int) -> Delivery:
    """Post one offer to its receiving team's GM channel and record where.

    Never raises for a per-offer problem: every failure is logged and comes
    back as a `Delivery` value, and the offer is left undelivered so the next
    drain retries it. Re-reads the row first so an offer resolved (or
    delivered by a concurrent drain) since the caller listed it is not posted
    with live buttons. That narrows, but does not close, the window for a
    duplicate post -- see the at-least-once note in the module docstring."""
    try:
        offer = await offer_repo.get_trade_offer(ex, offer_id)
        if offer is None or offer.status is not TradeStatus.PROPOSED:
            return Delivery.NOT_PENDING
        if offer.message_id is not None:
            return Delivery.NOT_PENDING

        to_team = await team_repo.get_team(ex, offer.to_team_id)
        if to_team is None or to_team.manager_user_id is None:
            # An AI-managed team never reads an inbox; nothing to notify.
            return Delivery.NO_HUMAN_RECIPIENT
        gm_channel = await channel_repo.get_active_gm_channel(ex, to_team.id)
        if gm_channel is None:
            log.warning(
                "offer %s not delivered: team %s has no active GM channel", offer.id, to_team.id
            )
            return Delivery.NO_GM_CHANNEL

        channel = bot.get_channel(gm_channel.channel_id)
        if channel is None:
            channel = await bot.fetch_channel(gm_channel.channel_id)
        if not callable(getattr(channel, "send", None)):
            # Categories, forum roots and voice-only channels cannot take a
            # message; a GM channel that became one is unreachable, not a bug.
            log.warning("offer %s not delivered: channel %s is not postable", offer.id, channel)
            return Delivery.CHANNEL_UNREACHABLE

        names = await load_offer_names(ex, [offer])
        message = await channel.send(
            content=build_offer_notification_text(
                offer, manager_user_id=to_team.manager_user_id, names=names
            ),
            embed=panel_to_embed(build_offer_panel(offer, names)),
            view=build_offer_view(offer.id),
            # The one mention that should ping is the manager's. Everything
            # else in the message is data (a team or player name), and Discord's
            # default `AllowedMentions` would let an @everyone in one become a
            # real ping.
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                replied_user=False,
                users=[discord.Object(id=to_team.manager_user_id)],
            ),
        )
        await offer_repo.set_offer_message(
            ex, offer.id, channel_id=message.channel.id, message_id=message.id
        )
        return Delivery.DELIVERED
    except (discord.NotFound, discord.Forbidden):
        log.warning("offer %s not delivered: its GM channel is gone or closed to the bot", offer_id)
        return Delivery.CHANNEL_UNREACHABLE
    except Exception:
        log.exception("delivering offer %s failed; it stays queued for the next drain", offer_id)
        return Delivery.FAILED


async def drain_undelivered_offers(
    bot: discord.Client, ex: Executor, *, league_id: int
) -> DrainReport:
    """Deliver every offer in `offer_repo.list_undelivered_offers`, oldest
    first. One offer's failure never stops the rest."""
    outcomes: dict[int, Delivery] = {}
    for offer in await offer_repo.list_undelivered_offers(ex, league_id):
        outcomes[offer.id] = await deliver_offer(bot, ex, offer.id)
    return DrainReport(outcomes=outcomes)
