"""`/wire`'s beat-filtered pager (docs/design/media-v2-newsroom.md MV-7,
build step 7) -- `WireNavButton`, a `DynamicItem` mirroring `dba2/bot/
leaders_pager.py`'s own shape: state lives entirely in the `custom_id`,
the class is registered once (`client.py::setup_hook`), and a fresh
instance is built per click. **Public, last-click-wins** (`LeadersNavButton`'s
own reasoning, reused verbatim): a wire story has no spoiler concern, so a
click edits the shared message in place -- no followup, no second message,
unlike `/watch`'s own ephemeral, `ComponentState`-backed pager.

**One story per page, Prev/Next only -- no mode toggle** (`/leaders`' own
fourth button has no `/wire` analogue): `beat` is fixed for the life of one
`/wire` invocation's message; changing beat means running the command
again. The two buttons only ever move `page`.

**`WIRE_BEAT_CHOICES`/`beat_to_persona` -- the one real trap in this whole
feature, named so nobody rediscovers it as a bug.** MV-7's own six `beat`
choices are `all|trade-desk|film-room|long-view|no-filter|the-beat`, and
revision 2 renamed the last two "to match MV-3's own column names" --
`hot_take`'s column is literally called "No Filter" (the commissioner's own
placeholder for the Stephen-A-style loud persona) and `beat_writer`'s
column is literally called "The Beat" (MV-3's table). **`no-filter` reads,
in English, like "turn filtering off" -- it is not that.** It is `hot_
take`'s own column name, spelled as a URL-safe slug; the choice that means
"turn filtering off and show all five personas" is `all`, a separate,
sixth value. Get this backwards and `/wire beat:no-filter` would silently
show everything instead of narrowing to Duke Sabatino's own column, and
`/wire beat:all` would incorrectly narrow to one persona.
"""
from __future__ import annotations

import re

import discord
from discord import app_commands

from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.services import view_service

from .context import NoLeagueForGuild, resolve_league
from .embeds import panel_to_embed
from .gates import ensure_command_legal
from .interaction_errors import reply_command_failure

log = get_logger(__name__)

_OTHER_SERVER = "That button belongs to another server's league."

# MV-7's own six `beat` choices, revision 2's column-name spelling for the
# last two. `name` is what a commissioner sees in Discord's own autocomplete
# UI; `value` is what `WireNavButton`'s `custom_id` actually carries forward
# on every Prev/Next click (never re-typed by a user after the initial
# command).
WIRE_BEAT_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Trade Desk", value="trade-desk"),
    app_commands.Choice(name="Film Room", value="film-room"),
    app_commands.Choice(name="Long View", value="long-view"),
    app_commands.Choice(name="No Filter", value="no-filter"),
    app_commands.Choice(name="The Beat", value="the-beat"),
]

# `all` maps to `None` (`view_service.get_wire_page`'s own "no filter"
# sentinel) -- every other choice maps to the real persona `key` MV-3's
# table names, which is what `media_stories.voice` actually stores and
# what the SQL predicate in `media_repo.list_media_stories_page` filters
# on (MV-7's own "the beat value maps to a persona key ... so the label is
# free to change" text).
_BEAT_TO_PERSONA: dict[str, str | None] = {
    "all": None,
    "trade-desk": "insider",
    "film-room": "analyst",
    "long-view": "historian",
    "no-filter": "hot_take",
    "the-beat": "beat_writer",
}


def beat_to_persona(beat_choice: str) -> str | None:
    """An unrecognized choice value degrades to `None` (show everything)
    rather than raising -- total, never-raises, the same discipline
    `leaders_pager.py::_parse_enum` documents for its own token parsing;
    Discord's own `app_commands.Choice` list is the real defense (a client
    cannot send a value it was never offered), this is only the defensive
    second layer for a forged interaction. The `beat` TOKEN itself (e.g.
    `"trade-desk"`) is what a `WireNavButton`'s own `custom_id` carries --
    never reconstructed from a persona key, so there is no reverse mapping
    to keep in sync with this one."""
    return _BEAT_TO_PERSONA.get(beat_choice)


# LD2-3's own bounded-template reasoning, reused verbatim: `\d{1,9}`/
# `\d{1,4}`/`\d{1,3}` (never `\d+`) so a forged 20-digit `page` fails
# `fullmatch` and never dispatches to this class at all, rather than
# parsing fine in Python and raising inside asyncpg on an `int8` OFFSET
# overflow. `beat` is one of the six closed slug tokens above, `[a-z-]{1,20}`
# bounds it the same way without needing a second alternation list here.
_MAX_PAGE = 500
"""Deliberately below the `page` group's own `\\d{1,3}` ceiling (`999`) --
see `view_service._WIRE_MAX_PAGE`'s own docstring for why a real gap
between the two matters."""
_NAV_TEMPLATE = (
    r"dba2:wire:nav:(?P<league_id>\d{1,9}):(?P<season>\d{1,4}):(?P<beat>[a-z-]{1,20})"
    r":(?P<page>\d{1,3}):(?P<action>prev|next)"
)

_ACTION_LABELS: dict[str, str] = {"prev": "◀ Prev", "next": "▶ Next"}


def wire_custom_id(league_id: int, season: int, beat: str, page: int, action: str) -> str:
    return f"dba2:wire:nav:{league_id}:{season}:{beat}:{page}:{action}"


def _clamp_page(page: int) -> int:
    return max(1, min(page, _MAX_PAGE))


class WireNavButton(discord.ui.DynamicItem[discord.ui.Button], template=_NAV_TEMPLATE):
    """One of the row's two buttons -- `beat` is carried forward unchanged
    (page-only navigation, no mode toggle); `page` is what THIS button
    moves to when clicked."""

    def __init__(
        self,
        league_id: int,
        season: int,
        beat: str,
        page: int,
        action: str,
        *,
        disabled: bool = False,
    ) -> None:
        if action not in _ACTION_LABELS:
            raise ValueError(f"unknown wire nav action {action!r}")
        super().__init__(
            discord.ui.Button(
                label=_ACTION_LABELS[action],
                style=discord.ButtonStyle.secondary,
                custom_id=wire_custom_id(league_id, season, beat, page, action),
                disabled=disabled,
            )
        )
        self.league_id = league_id
        self.season = season
        self.beat = beat
        self.page = page
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> WireNavButton:
        page = _clamp_page(int(match["page"]))
        return cls(
            int(match["league_id"]), int(match["season"]), match["beat"], page, match["action"]
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await _handle_wire_click(interaction, self)
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label=_label(self))


def _label(button: WireNavButton) -> str:
    return f"wire nav league {button.league_id} beat {button.beat} action {button.action}"


def _target_page(button: WireNavButton) -> int:
    """Prev/Next shift `page` by one, clamped defensively -- the UI already
    disables Prev on page 1 and Next when `has_more` is false, but a forged
    click can still send either."""
    if button.action == "prev":
        return _clamp_page(button.page - 1)
    return _clamp_page(button.page + 1)


def build_wire_view(
    league_id: int, season: int, beat: str, page: int, has_more: bool
) -> discord.ui.View:
    """The two-button row: Prev disabled on page 1, Next disabled when
    `view_service.get_wire_page` found no next story (its own `page_size +
    1` probe). `timeout=None` -- a view of only `DynamicItem`s gets no
    `dispatch_info` entry (`leaders_pager.py`'s own citation of `discord/ui/
    view.py:964-965`), so its end unregisters nothing, but a timeout task
    per render is waste."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        WireNavButton(league_id, season, beat, page, "prev", disabled=page <= 1)
    )
    view.add_item(
        WireNavButton(league_id, season, beat, page, "next", disabled=not has_more)
    )
    return view


def wire_view_or_missing(
    *, league_id: int, season: int, beat: str, page: int, has_more: bool
) -> discord.ui.View:
    """`_component_view_or_missing`'s convention (`bot/broadcast.py`,
    `leaders_pager.py`'s own reuse of it): a raising builder costs the
    buttons, never the panel -- `discord.utils.MISSING`, never `None`
    (`discord.py`'s own `view=` default IS `MISSING`; an explicit `None`
    would instead strip an existing row on an edit)."""
    try:
        return build_wire_view(league_id, season, beat, page, has_more)
    except Exception:
        log.error("wire nav league %s: could not build the view", league_id, exc_info=True)
        return discord.utils.MISSING


async def _handle_wire_click(interaction: discord.Interaction, button: WireNavButton) -> None:
    """`LeadersNavButton`'s own seven-step path, narrowed to `/wire`'s
    simpler (no-mode-toggle) shape."""
    await interaction.response.defer()
    pool = await get_pool()
    try:
        league = await resolve_league(pool, interaction)
    except NoLeagueForGuild:
        await interaction.followup.send(_OTHER_SERVER, ephemeral=True)
        return
    if league.id != button.league_id:
        await interaction.followup.send(_OTHER_SERVER, ephemeral=True)
        return
    ensure_command_legal("wire", league.current_phase)

    target_page = _target_page(button)
    persona = beat_to_persona(button.beat)
    view = await view_service.get_wire_page(
        pool, league_id=button.league_id, season=button.season, beat=persona, page=target_page
    )
    if view.is_empty:
        await interaction.followup.send("No wire stories for this beat yet.", ephemeral=True)
        return
    nav_view = wire_view_or_missing(
        league_id=button.league_id, season=button.season, beat=button.beat,
        page=view.page, has_more=view.has_more,
    )
    await interaction.edit_original_response(
        embed=panel_to_embed(view.panel),
        view=nav_view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
