"""The trade-offer review surface's pure builders (docs/design/phase-5-trade-
offer-review-surface.md §4.3, §6.5, §12 step 7): one offer as a `Panel`, one
team's `/offers` inbox as a `Panel`, and the plain-text messages around them.

**Constraint that shapes everything here: an offer row holds ids and nothing a
person can read, and this package may not import `dba2.data`.** So every
builder takes a `OfferNames` -- the teams, players and picks the caller (bot
layer) already fetched -- and looks names up in it. A missing entry renders as
`player #7` rather than raising: a deleted or foreign id must never make the
inbox -- the design's recovery path for "I deleted the message" (§4.3) --
unrenderable. No builder computes a league fact (P5-5): the only arithmetic is
counting lines to cap them.

**Discord limits are held by construction, not by hoping the input is small.**
`Panel.__post_init__` raises on overflow, which in production would be an
exception inside the one command that must keep working. So: every free-text
name is clipped, each side of an offer lists at most `_MAX_ASSET_LINES`
assets, and the inbox adds offers as fields only while the running character
total stays under the panel ceiling minus a reserve for the overflow field --
the offers that do not fit are listed by id in that overflow field, because a
human needs the id to answer. That id list is itself capped at one field value
(1024 chars): past that it is clipped with a trailing ellipsis, so an inbox
with enough hidden offers can omit the last ids (the description line still
states how many were hidden).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from dba2.domain.draft import DraftPick
from dba2.domain.phase import TradeStatus
from dba2.domain.player import Player
from dba2.domain.team import Team
from dba2.domain.trade_offer import TradeOffer
from dba2.presentation.panel import (
    MAX_FIELD_VALUE_CHARS,
    MAX_FIELDS,
    MAX_TOTAL_CHARS,
    Panel,
    PanelColor,
    PanelField,
)

OfferAction = Literal["propose", "accept", "reject", "counter", "veto"]

EXPIRY_SUFFIX = (
    "Trades are closed in this phase, and any offer made in an earlier phase has already "
    "expired -- run `/offers` to see what is still live."
)
"""Appended (by `bot/`) to a `PhaseError` from `offer_respond`/`counter_offer`
only (§5.3, NIT 10). `require_phase` fires before the staleness check, so a
human answering an offer from a closed phase is told about the command, not
the offer; this sentence is true in both directions (if trades are closed the
offer cannot be answered; if the phase moved the offer is stale) and names the
recovery command."""

_MAX_NAME_CHARS = 30
_MAX_ASSET_LINES = 10
"""Per side. The service caps a human's package at 3 players + 2 picks
(`offer_service.MAX_*_PER_SIDE`), but an AI-authored row is not bound by a
slash command's slots, so the panel does not assume it."""
_MAX_JOINED_CHARS = 350
_OVERFLOW_RESERVE = 1300
"""Characters held back in the inbox's running total for the overflow field
(name + up to 1024 value chars) and the description sentence that points at
it."""
_MAX_MESSAGE_CHARS = 2000  # Discord's message `content` limit


@dataclass(frozen=True)
class OfferNames:
    """What the caller resolved for a set of offers, keyed by id. Lookups are
    lenient (see module docstring); the maps may be supersets."""

    teams: Mapping[int, Team]
    players: Mapping[int, Player]
    picks: Mapping[int, DraftPick]


def _clip(text: str, limit: int = _MAX_NAME_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _team_code(names: OfferNames, team_id: int) -> str:
    team = names.teams.get(team_id)
    return _clip(team.code, 8) if team is not None else f"team #{team_id}"


def _team_label(names: OfferNames, team_id: int) -> str:
    team = names.teams.get(team_id)
    if team is None:
        return f"team #{team_id}"
    return _clip(f"{team.city} {team.name}", 40)


def _player_line(names: OfferNames, player_id: int) -> str:
    player = names.players.get(player_id)
    if player is None:
        return f"player #{player_id}"
    full = _clip(f"{player.first_name} {player.last_name}")
    return f"{full} ({player.primary_position.value}, {player.overall_cached} OVR)"


def _pick_line(names: OfferNames, pick_id: int) -> str:
    pick = names.picks.get(pick_id)
    if pick is None:
        return f"pick #{pick_id}"
    original = _team_code(names, pick.original_team_id)
    return f"{pick.season} round {pick.round} pick ({original})"


def _package_ids(offer: TradeOffer, key: str) -> tuple[int, ...]:
    return tuple(offer.package.get(key, ()))


def _side_lines(
    names: OfferNames, offer: TradeOffer, *, player_key: str, pick_key: str
) -> list[str]:
    lines = [_player_line(names, i) for i in _package_ids(offer, player_key)]
    lines += [_pick_line(names, i) for i in _package_ids(offer, pick_key)]
    return lines


def _capped(lines: Sequence[str]) -> str:
    if not lines:
        return "(nothing)"
    shown = list(lines[:_MAX_ASSET_LINES])
    if len(lines) > len(shown):
        shown.append(f"... and {len(lines) - len(shown)} more")
    return "\n".join(shown)


def _joined(lines: Sequence[str]) -> str:
    """One line, `, `-separated, clipped to `_MAX_JOINED_CHARS` with a count of
    what did not fit."""
    if not lines:
        return "nothing"
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        cost = len(line) + (2 if out else 0)
        if used + cost > _MAX_JOINED_CHARS:
            out.append(f"+{len(lines) - i} more")
            break
        out.append(line)
        used += cost
    return ", ".join(out)


def _proposer(offer: TradeOffer, names: OfferNames) -> str:
    if offer.proposed_by is None:
        return f"the {_team_code(names, offer.from_team_id)} AI GM"
    return f"<@{offer.proposed_by}>"


_STATUS_COLOR = {
    TradeStatus.PROPOSED: PanelColor.NEUTRAL,
    TradeStatus.ACCEPTED: PanelColor.POSITIVE,
    TradeStatus.REJECTED: PanelColor.NEGATIVE,
    TradeStatus.VETOED: PanelColor.NEGATIVE,
    TradeStatus.EXPIRED: PanelColor.WARNING,
    TradeStatus.COUNTERED: PanelColor.WARNING,
}


def build_offer_panel(offer: TradeOffer, names: OfferNames) -> Panel:
    """One offer, from the PROPOSER's side: `package`'s `*_from_to` keys are
    what `from_team_id` sends. The delivery drain posts this with the buttons;
    the propose/counter replies show it as the confirmation."""
    from_code = _team_code(names, offer.from_team_id)
    to_code = _team_code(names, offer.to_team_id)
    description_lines = [
        f"**{_team_label(names, offer.from_team_id)}** offered this to "
        f"**{_team_label(names, offer.to_team_id)}**.",
        f"Proposed by {_proposer(offer, names)}.",
    ]
    if offer.counters_offer_id is not None:
        description_lines.append(f"Counters offer #{offer.counters_offer_id}.")
    description_lines.append(
        f"Made during {offer.phase.value} (season {offer.season}). "
        "It expires when the league moves to a new phase."
    )
    if offer.status is not TradeStatus.PROPOSED:
        description_lines.append(f"Status: {offer.status.value}.")

    fields = (
        PanelField(
            name=f"{from_code} sends",
            value=_capped(
                _side_lines(
                    names, offer, player_key="players_from_to", pick_key="picks_from_to"
                )
            ),
            inline=True,
        ),
        PanelField(
            name=f"{to_code} sends",
            value=_capped(
                _side_lines(
                    names, offer, player_key="players_to_from", pick_key="picks_to_from"
                )
            ),
            inline=True,
        ),
    )
    footer = (
        f"Answer with the buttons, or /offer-respond offer_id:{offer.id}"
        if offer.status is TradeStatus.PROPOSED
        else ""
    )
    return Panel(
        title=f"Trade offer #{offer.id}: {from_code} to {to_code}",
        description="\n".join(description_lines),
        fields=fields,
        color=_STATUS_COLOR[offer.status],
        footer=footer,
    )


def _inbox_field(offer: TradeOffer, *, team_id: int, names: OfferNames) -> PanelField:
    """One pending offer, phrased for the team reading the inbox."""
    received = offer.to_team_id == team_id
    other_id = offer.from_team_id if received else offer.to_team_id
    other = _team_code(names, other_id)
    incoming = _side_lines(
        names,
        offer,
        player_key="players_from_to" if received else "players_to_from",
        pick_key="picks_from_to" if received else "picks_to_from",
    )
    outgoing = _side_lines(
        names,
        offer,
        player_key="players_to_from" if received else "players_from_to",
        pick_key="picks_to_from" if received else "picks_from_to",
    )
    name = f"Received #{offer.id} from {other}" if received else f"Sent #{offer.id} to {other}"
    value = (
        f"You get: {_joined(incoming)}\n"
        f"You send: {_joined(outgoing)}\n"
        f"Proposed by {_proposer(offer, names)} during {offer.phase.value}"
    )
    return PanelField(name=name, value=value[:MAX_FIELD_VALUE_CHARS])


def build_offers_panel(
    *,
    team: Team,
    received: Sequence[TradeOffer],
    sent: Sequence[TradeOffer],
    names: OfferNames,
) -> Panel:
    """`/offers`: every pending offer `team` received, then every one it sent,
    each in the order given (the service returns oldest first). Offers that do
    not fit the Discord limits are listed by id in a last field instead of
    being dropped -- see module docstring."""
    title = f"{_clip(f'{team.city} {team.name}', 60)} -- pending trade offers"
    ordered = [(o, True) for o in received] + [(o, False) for o in sent]
    header = (
        f"{len(received)} received, {len(sent)} sent. Offers expire when the league moves "
        "to a new phase."
    )
    if not ordered:
        return Panel(title=title, description="No pending trade offers.")
    if received:
        header += (
            "\nAnswer a received offer with `/offer-respond` (accept or reject) or "
            "`/counter-offer`."
        )

    fields: list[PanelField] = []
    total = len(title) + len(header)
    shown = 0
    for offer, _is_received in ordered:
        field = _inbox_field(offer, team_id=team.id, names=names)
        cost = len(field.name) + len(field.value)
        if len(fields) >= MAX_FIELDS - 1 or total + cost > MAX_TOTAL_CHARS - _OVERFLOW_RESERVE:
            break
        fields.append(field)
        total += cost
        shown += 1

    hidden = [o for o, _ in ordered[shown:]]
    description = header
    if hidden:
        ids = ", ".join(f"#{o.id}" for o in hidden)
        if len(ids) > MAX_FIELD_VALUE_CHARS:
            ids = ids[: MAX_FIELD_VALUE_CHARS - 1] + "…"
        description += (
            f"\n{len(hidden)} more did not fit here -- their ids are listed at the bottom."
        )
        fields.append(PanelField(name="Also pending (ids only)", value=ids))
    return Panel(title=title, description=description, fields=tuple(fields))


def build_offer_notification_text(
    offer: TradeOffer, *, manager_user_id: int, names: OfferNames
) -> str:
    """The plain `content` posted above the offer's embed in the receiving
    team's GM channel. The mention is what produces a push (§6.5); the caller
    must restrict `allowed_mentions` to that one user so nothing else in a
    message can ping."""
    text = (
        f"<@{manager_user_id}> {_team_label(names, offer.from_team_id)} sent you a trade "
        f"offer (#{offer.id})."
    )
    return _clip(text, _MAX_MESSAGE_CHARS)


def build_counter_pointer_text(offer_id: int) -> str:
    """The Counter button's whole reply. A counter is four id lists and
    Discord buttons cannot carry them; `/counter-offer` is the one way to make
    one (§4.5), so the button points at it rather than duplicating it.

    **GM-3b.1's sixth entry point (docs/design/gm-command-surface.md,
    round 3 critic MINOR-4).** `/counter-offer` is refused under
    auto-pilot -- this button has no team context to check that itself
    (it carries only an offer id), so the caveat is stated once here
    rather than routing an auto-piloted GM toward a command that will
    refuse them with no warning."""
    return (
        f"To counter offer #{offer_id}, run `/counter-offer offer_id:{offer_id}` and fill in "
        "the players/picks you want to send and get. (If your team is on auto-pilot, "
        "`/counter-offer` will refuse you -- use `/offer-respond` to accept or reject "
        "instead, or `/gm auto enabled:off` first.)"
    )


def describe_offer_result(
    offer: TradeOffer,
    *,
    changed: bool,
    detail: str | None,
    action: OfferAction,
    to_label: str | None = None,
) -> str:
    """One sentence for what an `OfferResult` means to the person who made the
    call. `changed=False` is "someone had already answered" and must never
    read as success. `detail` is the service's human reason for a row that
    went `EXPIRED`/`REJECTED` without a human saying so. For `counter`,
    `offer` is the NEW row when a counter was made (`counters_offer_id` set)
    and the original otherwise -- `counters_offer_id` is what tells them apart."""
    is_counter_row = offer.counters_offer_id is not None and action == "counter"
    label = f"Counter offer #{offer.id}" if is_counter_row else f"Offer #{offer.id}"
    status = offer.status
    if not changed:
        return f"{label} was already {status.value} -- nothing was changed."

    if status is TradeStatus.ACCEPTED:
        return f"{label} accepted -- the trade is done."
    if status is TradeStatus.REJECTED:
        return f"{label} was rejected: {detail}" if detail else f"{label} rejected."
    if status is TradeStatus.EXPIRED:
        text = f"{label} has expired -- nothing was traded."
        if detail:
            text += f" {detail}"
        if offer.illegal_token:
            text += f" ({offer.illegal_token})"
        return text
    if status is TradeStatus.VETOED:
        return f"{label} vetoed."
    if status is TradeStatus.PROPOSED:
        whom = f"{to_label}'s manager" if to_label else "the other team's manager"
        if is_counter_row:
            return (
                f"Countered offer #{offer.counters_offer_id} with offer #{offer.id}; "
                f"it is waiting for {whom}."
            )
        return f"Offer #{offer.id} sent; it is waiting for {whom}."
    return f"{label} is now {status.value}."
