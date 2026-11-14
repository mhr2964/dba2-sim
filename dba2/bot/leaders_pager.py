"""`/leaders` v2's toggle-and-paging row (docs/design/leaders-v2-per-game-and-
paging.md LD2-3/LD2-4) -- ONE public, shared button class, `LeadersNavButton`, a
`DynamicItem` mirroring `dba2/bot/watch_pager.py`'s own shape: state lives entirely
in the `custom_id`, the class is registered once (`client.py::setup_hook`), and a
fresh instance is built per click.

**Public, last-click-wins (LD2-3), unlike `/watch`'s ephemeral pager.** A
leaderboard has no spoiler concern, so the click edits the shared message in
place -- no followup, no second message, no per-viewer `ComponentState`.

**The template's numeric groups are BOUNDED, not `\\d+` (LD2-3 [r2/minor 3]).**
`league_id` at `\\d{1,9}`, `season` at `\\d{1,4}`, `page` at `\\d{1,3}` -- a forged
20-digit `page` fails `fullmatch` and never dispatches to this class at all, rather
than parsing fine in Python and raising inside asyncpg on an `int8` OFFSET
overflow. For an id that DOES match, `from_custom_id` additionally clamps `page`
into `[1, _MAX_PAGE]` (never raises) so `OFFSET` stays bounded by inspection.

**The click path, in LD2-3's own implementable order:** parse (total, never
raises) -> `defer()` -> `get_pool()` -> `resolve_league` (the one read permitted
before the guard) -> `NoLeagueForGuild` refusal -> cross-league refusal BEFORE any
leaders read -> `ensure_command_legal` (symmetry with the command; `ALL_PHASES`
today, so this can never fire) -> the panel query -> `edit_original_response`.
`resolve_league` cannot run before `defer()` here the way `watch_pager`'s
registry-backed gate does (`_answer_from_entry`'s guild check runs against an
in-memory `entry.guild_id` it already holds) -- leaders has no registry entry by
design (LD2-3's whole point), so there is nothing to compare against until the
guild's league is resolved, and that resolution is itself a read.

**MISSING, never `None`, on a raising or absent view** -- `bot/broadcast.py`'s
`_component_view_or_missing`/`_bounded_edit` convention, reused rather than
reinvented: an absent view (LD2-6's no-games empty branch, case 1 -- "both modes
are empty; there is nothing to toggle to") and a raising builder both cost the
buttons, never the panel. `timeout=None` for `build_pager_view`'s own reason: a
view of only `DynamicItem`s gets no `dispatch_info` entry (`view.py:964-965`), so
its end unregisters nothing, but a timeout task per render is waste.
"""
from __future__ import annotations

import re
from typing import Any

import discord

from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.domain.game import GameType
from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.services import view_service

from .context import NoLeagueForGuild, resolve_league
from .embeds import panel_to_embed
from .gates import ensure_command_legal
from .interaction_errors import reply_command_failure

log = get_logger(__name__)

_OTHER_SERVER = "That button belongs to another server's league."

# LD2-4: today's default page size and the bound on a forged `page` -- `100` is
# ample (1,000 rows against a ~420-player league), used only to clamp, never to
# size a real query.
_PAGE_SIZE = 10
_MAX_PAGE = 100

# LD2-3's bounded template -- see the module docstring for why `\d{1,9}`/`\d{1,4}`/
# `\d{1,3}` matter and `\d+` does not. `mode`/`action` share the literal
# `pergame`/`total` tokens with `LeaderMode`'s own `StrEnum` values, so no second
# token table is needed for either.
_NAV_TEMPLATE = (
    r"dba2:leaders:nav:(?P<league_id>\d{1,9}):(?P<season>\d{1,4}):(?P<game_type>[a-z]{1,12})"
    r":(?P<stat>[a-z]{1,12}):(?P<mode>pergame|total):(?P<page>\d{1,3})"
    r":(?P<action>pergame|total|prev|next)"
)

ACTIONS = ("prev", "next", "pergame", "total")
_ACTION_LABELS: dict[str, str] = {
    "prev": "◀ Prev",
    "next": "▶ Next",
    "pergame": "Per Game",
    "total": "Total",
}
_MODE_ACTIONS = frozenset({"pergame", "total"})


def leaders_custom_id(
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    mode: LeaderMode,
    page: int,
    action: str,
) -> str:
    return (
        f"dba2:leaders:nav:{league_id}:{season}:{game_type.value}:{stat.value}"
        f":{mode.value}:{page}:{action}"
    )


def _clamp_page(page: int) -> int:
    return max(1, min(page, _MAX_PAGE))


def _parse_enum(enum_cls: type, token: str, default: Any) -> Any:
    """Total, never raises (LD2-3): an unrecognized token degrades to `default`
    rather than propagating -- a raise in `from_custom_id` is swallowed with no
    ack (`discord/ui/view.py:1011-1015`), so the only defence is that it cannot
    raise at all."""
    try:
        return enum_cls(token)
    except ValueError:
        return default


class LeadersNavButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_NAV_TEMPLATE,
):
    """One of the row's four buttons -- Prev/Next preserve `mode`, a toggle
    (`pergame`/`total`) switches it (LD2-3, mirroring `WatchPageButton`'s own
    `page`+`action` shape). `mode` is what the message shows NOW; `action` is
    what THIS button does when clicked."""

    def __init__(
        self,
        league_id: int,
        season: int,
        game_type: GameType,
        stat: LeaderStat,
        mode: LeaderMode,
        page: int,
        action: str,
        *,
        disabled: bool = False,
    ) -> None:
        if action not in _ACTION_LABELS:
            raise ValueError(f"unknown leaders nav action {action!r}")
        style = discord.ButtonStyle.secondary
        if action in _MODE_ACTIONS and action == mode.value:
            style = discord.ButtonStyle.primary
        super().__init__(
            discord.ui.Button(
                label=_ACTION_LABELS[action],
                style=style,
                custom_id=leaders_custom_id(league_id, season, game_type, stat, mode, page, action),
                disabled=disabled,
            )
        )
        self.league_id = league_id
        self.season = season
        self.game_type = game_type
        self.stat = stat
        self.mode = mode
        self.page = page
        self.action = action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> LeadersNavButton:
        game_type = _parse_enum(GameType, match["game_type"], GameType.REGULAR)
        stat = _parse_enum(LeaderStat, match["stat"], LeaderStat.POINTS)
        mode = _parse_enum(LeaderMode, match["mode"], LeaderMode.PER_GAME)
        page = _clamp_page(int(match["page"]))
        return cls(
            int(match["league_id"]),
            int(match["season"]),
            game_type,
            stat,
            mode,
            page,
            match["action"],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await _handle_leaders_click(interaction, self)
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label=_label(self))


def _label(button: LeadersNavButton) -> str:
    return f"leaders nav league {button.league_id} action {button.action}"


def _target_mode_and_page(button: LeadersNavButton) -> tuple[LeaderMode, int]:
    """Prev/Next preserve `mode` and shift `page` by one, clamped defensively
    (the UI already disables Prev on page 1 and Next when `has_more` is false,
    but a forged click can still send either). A mode toggle keeps `page` as-is
    -- `view_service.get_leaders_panel`'s own LD2-4 clamp renders page 1 instead
    if that page comes back empty under the new mode."""
    if button.action == "prev":
        return button.mode, _clamp_page(button.page - 1)
    if button.action == "next":
        return button.mode, _clamp_page(button.page + 1)
    return LeaderMode(button.action), button.page


def build_leaders_view(
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    mode: LeaderMode,
    page: int,
    has_more: bool,
) -> discord.ui.View:
    """LD2-4's four-button row: Prev disabled on page 1, Next disabled when the
    repo's own `page_size + 1` probe found no more rows, the active mode button
    primary and disabled (mirroring `build_component_view`'s highlight,
    `watch_pager.py`) -- clicking the mode you are already viewing is a no-op.
    `timeout=None` -- see the module docstring."""
    view = discord.ui.View(timeout=None)
    for action in ACTIONS:
        if action == "prev":
            disabled = page <= 1
        elif action == "next":
            disabled = not has_more
        else:
            disabled = action == mode.value
        view.add_item(
            LeadersNavButton(
                league_id, season, game_type, stat, mode, page, action, disabled=disabled
            )
        )
    return view


def leaders_view_or_missing(
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    mode: LeaderMode,
    page: int,
    has_more: bool,
    no_games: bool,
) -> discord.ui.View | Any:
    """`_component_view_or_missing`'s convention (`bot/broadcast.py`), reused:
    the LD2-6 no-games branch (case 1 -- nothing to toggle to) and a raising
    builder both cost the buttons, never the panel -- `discord.utils.MISSING`,
    never `None`, since `discord.py`'s own `view=` default on `send_message`/
    `edit_message`/`edit_original_response` IS `MISSING` (an explicit `None`
    would instead strip an existing row on an edit). Shared by `cogs/read.py`'s
    `leaders()` (a fresh send) and this module's own click handler (an edit) --
    both accept the same sentinel for "omit"."""
    if no_games:
        return discord.utils.MISSING
    try:
        return build_leaders_view(league_id, season, game_type, stat, mode, page, has_more)
    except Exception:
        log.error(
            "leaders nav league %s: could not build the view", league_id, exc_info=True
        )
        return discord.utils.MISSING


async def _handle_leaders_click(interaction: discord.Interaction, button: LeadersNavButton) -> None:
    """LD2-3's seven-step path."""
    # Step 2: ack inside the 3-second window, commit to editing the message later.
    await interaction.response.defer()
    # Step 3: the only read permitted before the guard.
    pool = await get_pool()
    try:
        league = await resolve_league(pool, interaction)
    except NoLeagueForGuild:
        # Step 4.
        await interaction.followup.send(_OTHER_SERVER, ephemeral=True)
        return
    # Step 5: before any leaders read.
    if league.id != button.league_id:
        await interaction.followup.send(_OTHER_SERVER, ephemeral=True)
        return
    # Step 6: symmetry with the command; `COMMAND_GATES["leaders"]` is `ALL_PHASES`,
    # so this can never fire today.
    ensure_command_legal("leaders", league.current_phase)

    mode, target_page = _target_mode_and_page(button)
    # Step 7: only now the panel query.
    view = await view_service.get_leaders_panel(
        pool,
        league_id=button.league_id,
        season=button.season,
        game_type=button.game_type,
        stat=button.stat,
        mode=mode,
        page=target_page,
        page_size=_PAGE_SIZE,
    )
    nav_view = leaders_view_or_missing(
        league_id=button.league_id,
        season=button.season,
        game_type=button.game_type,
        stat=button.stat,
        mode=mode,
        page=view.page,
        has_more=view.has_more,
        no_games=view.no_games,
    )
    await interaction.edit_original_response(
        embed=panel_to_embed(view.panel),
        view=nav_view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
