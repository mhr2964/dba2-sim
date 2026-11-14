"""The trade-offer review surface's slash commands -- `/propose-trade`,
`/offers`, `/offer-respond`, `/counter-offer`, `/veto-offer`
(docs/design/phase-5-trade-offer-review-surface.md §4, §8, §12 step 8).

**Constraint that shapes every command: the rules live in `offer_service`,
and this file only chooses words.** Who may act (`permissions.require_team_
manager` / `require_commissioner`), which phases are open (`COMMAND_GATES`,
re-checked under the gate) and what a stale offer is are all decided there;
the bot-layer checks here (`ensure_team_manager`, `ensure_commissioner`,
`ensure_command_legal`) call the same predicates purely so a refusal arrives
before `defer()` with a message that names the reason (P5-38's first half).
Nothing in this module opens a league gate, takes a roster lock or writes an
offer -- the discipline suite's check 6 would fail the build if it did.

**Every reply is ephemeral, and each command `defer(ephemeral=True)`s.** An
ephemeral deferral is what makes the later followup ephemeral too (a followup's
own flag is ignored after a public defer). A proposal, an inbox and a veto are
between the people involved; the durable, public artifact of an accepted trade
is the transaction log, not a channel message.

**`/offer-respond` and `/counter-offer` do NOT pre-check the phase.** Their
`require_phase` runs inside the service, before its staleness check, so a
human answering an offer from an earlier phase gets a `PhaseError` about the
command rather than the offer. Only those two commands append
`presentation.offers.EXPIRY_SUFFIX` to that `PhaseError` (§5.3, NIT 10) -- and
a bot-layer `ensure_command_legal` here would raise `CommandNotLegalNow`
first, so the sentence would never be seen. `/propose-trade` keeps the bare
message: it has no offer to have expired.

**`OfferResult.changed=False` is "someone had already answered", never
success**, and `OfferResult.detail` is the human reason a row went `EXPIRED`/
`REJECTED`; both are rendered by `describe_offer_result`.

The delivery drain is called from here in two places, always outside a
transaction and never able to fail the command: after `/propose-trade` and
`/counter-offer` for the one new offer, and at the END of `/offers`, after the
sweep and the reads -- so an offer the sweep just expired is never posted with
live buttons, and "I was away" self-heals.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from dba2.core.errors import NotFoundError
from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import team_repo
from dba2.domain.phase import TradeStatus
from dba2.phase.permissions import PermissionDenied
from dba2.presentation.offers import (
    EXPIRY_SUFFIX,
    build_offer_panel,
    build_offers_panel,
    describe_offer_result,
)
from dba2.presentation.offers import (
    OfferAction as ResultAction,
)
from dba2.services import offer_service
from dba2.services.offer_service import OfferAction, OfferResult

from ..context import NoLeagueForGuild, resolve_league
from ..embeds import panel_to_embed
from ..gates import (
    CommandNotLegalNow,
    ensure_can_act_for_team,
    ensure_command_legal,
    ensure_commissioner,
    ensure_team_manager,
)
from ..interaction_errors import reply_command_failure, reply_ephemeral
from ..offer_delivery import (
    Delivery,
    deliver_offer,
    drain_undelivered_offers,
    load_offer_names,
)
from ..post_commit import honest_suffix, run_after_commit

log = get_logger(__name__)

_PANEL_FAILURE_SUFFIX = honest_suffix("the offer panel couldn't be shown")
_DELIVERY_FAILURE_SUFFIX = honest_suffix(
    "their GM channel couldn't be notified yet -- the offer is safe, shows in their "
    "/offers, and the bot will retry"
)
_BAD_DELIVERY = frozenset(
    {Delivery.NO_GM_CHANNEL, Delivery.CHANNEL_UNREACHABLE, Delivery.FAILED}
)

_ACTION_CHOICES = [
    app_commands.Choice(name="Accept", value=OfferAction.ACCEPT.value),
    app_commands.Choice(name="Reject", value=OfferAction.REJECT.value),
]


def _ids(*values: int | None) -> tuple[int, ...]:
    """The optional slots a command offers (there is no list option type in
    Discord), collapsed to the ids actually given."""
    return tuple(v for v in values if v is not None)


class OfferCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _team_by_code(self, pool, league, code: str, *, noun: str = "team"):
        team = await team_repo.get_team_by_code(pool, league.id, code.upper())
        if team is None:
            raise NotFoundError(f"No {noun} with code {code!r} in this league.")
        return team

    async def _finish_offer_write(
        self,
        interaction: discord.Interaction,
        pool,
        result: OfferResult,
        *,
        action: ResultAction,
        to_label: str | None = None,
    ) -> None:
        """Everything after a `propose`/`counter` that already committed:
        render the new offer, deliver it if it is waiting on a human, and
        answer. Each step is a never-raising follow-up (`run_after_commit`), so
        a failure here is disclosed with `honest_suffix` and can never read as
        "nothing happened" (the write is real)."""
        offer = result.offer

        async def _render():
            names = await load_offer_names(pool, [offer])
            return names, build_offer_panel(offer, names)

        rendered = await run_after_commit(_render, log_context=f"render offer {offer.id}")
        names = rendered[0] if rendered is not None else None
        to_team = names.teams.get(offer.to_team_id) if names is not None else None

        text = describe_offer_result(
            offer,
            changed=result.changed,
            detail=result.detail,
            action=action,
            to_label=to_label or (to_team.code if to_team is not None else None),
        )
        suffix = ""
        if result.changed and offer.status is TradeStatus.PROPOSED:
            outcome = await run_after_commit(
                lambda: deliver_offer(self.bot, pool, offer.id),
                log_context=f"deliver offer {offer.id}",
            )
            if outcome is None or outcome in _BAD_DELIVERY:
                suffix += _DELIVERY_FAILURE_SUFFIX
        if rendered is None:
            await interaction.followup.send(
                content=text + suffix + _PANEL_FAILURE_SUFFIX, ephemeral=True
            )
        else:
            await interaction.followup.send(
                content=text + suffix, embed=panel_to_embed(rendered[1]), ephemeral=True
            )

    @app_commands.command(
        name="propose-trade", description="Propose a trade to another team (by internal ids)."
    )
    @app_commands.describe(
        code="Your team's 2-3 letter code",
        partner="The other team's 2-3 letter code",
        send_player_1="Player id you send",
        send_player_2="Player id you send",
        send_player_3="Player id you send",
        get_player_1="Player id you get",
        get_player_2="Player id you get",
        get_player_3="Player id you get",
        send_pick_1="Draft pick id you send",
        send_pick_2="Draft pick id you send",
        get_pick_1="Draft pick id you get",
        get_pick_2="Draft pick id you get",
    )
    async def propose_trade(
        self,
        interaction: discord.Interaction,
        code: str,
        partner: str,
        send_player_1: int | None = None,
        send_player_2: int | None = None,
        send_player_3: int | None = None,
        get_player_1: int | None = None,
        get_player_2: int | None = None,
        get_player_3: int | None = None,
        send_pick_1: int | None = None,
        send_pick_2: int | None = None,
        get_pick_1: int | None = None,
        get_pick_2: int | None = None,
    ) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("propose_trade", league.current_phase)
            team = await self._team_by_code(pool, league, code)
            other = await self._team_by_code(pool, league, partner, noun="partner team")
            ensure_team_manager(interaction.user.id, league, team, action="propose_trade")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await offer_service.propose(
                pool,
                league_id=league.id,
                from_team_id=team.id,
                to_team_id=other.id,
                players_from_to=_ids(send_player_1, send_player_2, send_player_3),
                players_to_from=_ids(get_player_1, get_player_2, get_player_3),
                picks_from_to=_ids(send_pick_1, send_pick_2),
                picks_to_from=_ids(get_pick_1, get_pick_2),
                actor_user_id=interaction.user.id,
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/propose-trade")
            return
        await self._finish_offer_write(
            interaction, pool, result, action="propose", to_label=other.code
        )

    @app_commands.command(name="offers", description="Your team's pending trade offers.")
    @app_commands.describe(code="Your team's 2-3 letter code")
    async def offers(self, interaction: discord.Interaction, code: str) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("offers", league.current_phase)
            team = await self._team_by_code(pool, league, code)
            ensure_can_act_for_team(interaction.user.id, league, team, action="offers")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied, NotFoundError) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            # 1. the expiry sweep, 2. the reads -- both inside `list_offers`, in
            # separate transactions (§4.3). This is the ONLY caller of the sweep.
            listing = await offer_service.list_offers(
                pool, league_id=league.id, team_id=team.id, actor_user_id=interaction.user.id
            )
            names = await load_offer_names(pool, [*listing.received, *listing.sent])
            panel = build_offers_panel(
                team=team, received=listing.received, sent=listing.sent, names=names
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/offers")
            return
        await interaction.followup.send(embed=panel_to_embed(panel), ephemeral=True)

        # 3. the delivery drain, last and outside any transaction: after the
        # sweep so an offer it just expired is never posted with live buttons.
        # Never allowed to fail a command that has already answered.
        await run_after_commit(
            lambda: drain_undelivered_offers(self.bot, pool, league_id=league.id),
            log_context=f"offer delivery drain from /offers for league {league.id}",
        )

    @app_commands.command(
        name="offer-respond", description="Accept or reject a trade offer made to your team."
    )
    @app_commands.describe(offer_id="The offer's id (see /offers)", action="Accept or reject")
    @app_commands.choices(action=_ACTION_CHOICES)
    async def offer_respond(
        self,
        interaction: discord.Interaction,
        offer_id: int,
        action: app_commands.Choice[str],
    ) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
        except NoLeagueForGuild as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await offer_service.respond(
                pool,
                league_id=league.id,
                offer_id=offer_id,
                action=OfferAction(action.value),
                actor_user_id=interaction.user.id,
            )
        except Exception as exc:
            await reply_command_failure(
                interaction, exc, log_label="/offer-respond", phase_error_suffix=EXPIRY_SUFFIX
            )
            return
        await interaction.followup.send(
            describe_offer_result(
                result.offer, changed=result.changed, detail=result.detail, action=action.value
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="counter-offer",
        description="Counter a trade offer made to your team (by internal ids).",
    )
    @app_commands.describe(
        offer_id="The offer you are countering (see /offers)",
        send_player_1="Player id you send",
        send_player_2="Player id you send",
        send_player_3="Player id you send",
        get_player_1="Player id you get",
        get_player_2="Player id you get",
        get_player_3="Player id you get",
        send_pick_1="Draft pick id you send",
        send_pick_2="Draft pick id you send",
        get_pick_1="Draft pick id you get",
        get_pick_2="Draft pick id you get",
    )
    async def counter_offer(
        self,
        interaction: discord.Interaction,
        offer_id: int,
        send_player_1: int | None = None,
        send_player_2: int | None = None,
        send_player_3: int | None = None,
        get_player_1: int | None = None,
        get_player_2: int | None = None,
        get_player_3: int | None = None,
        send_pick_1: int | None = None,
        send_pick_2: int | None = None,
        get_pick_1: int | None = None,
        get_pick_2: int | None = None,
    ) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
        except NoLeagueForGuild as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            # The countering team is not a parameter: it is the original
            # offer's `to_team_id`, so "counter on someone else's behalf" is not
            # a shape that exists (§4.5). "send" is from that team's side.
            result = await offer_service.counter(
                pool,
                league_id=league.id,
                offer_id=offer_id,
                actor_user_id=interaction.user.id,
                players_from_to=_ids(send_player_1, send_player_2, send_player_3),
                players_to_from=_ids(get_player_1, get_player_2, get_player_3),
                picks_from_to=_ids(send_pick_1, send_pick_2),
                picks_to_from=_ids(get_pick_1, get_pick_2),
            )
        except Exception as exc:
            await reply_command_failure(
                interaction, exc, log_label="/counter-offer", phase_error_suffix=EXPIRY_SUFFIX
            )
            return
        await self._finish_offer_write(interaction, pool, result, action="counter")

    @app_commands.command(
        name="veto-offer", description="Kill a pending trade offer (commissioner only)."
    )
    @app_commands.describe(offer_id="The offer's id (see /offers)")
    async def veto_offer(self, interaction: discord.Interaction, offer_id: int) -> None:
        pool = await get_pool()
        try:
            league = await resolve_league(pool, interaction)
            ensure_command_legal("veto_offer", league.current_phase)
            ensure_commissioner(interaction.user.id, league, action="veto_offer")
        except (NoLeagueForGuild, CommandNotLegalNow, PermissionDenied) as exc:
            await reply_ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await offer_service.veto(
                pool,
                league_id=league.id,
                offer_id=offer_id,
                actor_user_id=interaction.user.id,
            )
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label="/veto-offer")
            return
        await interaction.followup.send(
            describe_offer_result(
                result.offer, changed=result.changed, detail=result.detail, action="veto"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OfferCommands(bot))
