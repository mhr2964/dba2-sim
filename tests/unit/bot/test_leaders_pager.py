"""`/leaders` v2's toggle-and-paging row (`dba2/bot/leaders_pager.py`,
docs/design/leaders-v2-per-game-and-paging.md), without a database or a gateway.

What has teeth here:

- the ids: the bounded template (a 20-digit `page` must never match at all), a
  matching-but-out-of-range `page` clamps, `from_custom_id` is total on every
  string that matches;
- the click path (LD2-3): `defer()` before any read, `resolve_league` before the
  guard, the guild check BEFORE any leaders read, a raising view builder omits
  `view=` rather than stripping the panel;
- the view: `timeout=None`, Prev/Next/the active mode button disabled correctly.

No DB, no gateway: every read the click makes is patched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from dba2.bot import context, leaders_pager
from dba2.bot.leaders_pager import (
    ACTIONS,
    LeadersNavButton,
    build_leaders_view,
    leaders_custom_id,
    leaders_view_or_missing,
)
from dba2.domain.game import GameType
from dba2.domain.phase import Phase
from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.presentation.panel import Panel, PanelColor
from dba2.services import view_service
from dba2.services.view_service import LeadersView

LEAGUE = 42
OTHER_LEAGUE = 43
GUILD = 4242
OTHER_GUILD = 777
SEASON = 2026


def _panel(text: str = "leaders") -> Panel:
    return Panel(title="Leaders", description=text, color=PanelColor.NEUTRAL, footer="a footer")


def _view(*, page: int = 1, has_more: bool = False, row_count: int = 1, no_games: bool = False):
    return LeadersView(
        panel=_panel(),
        page=page,
        has_more=has_more,
        row_count=row_count,
        max_team_games=None if no_games else 72,
    )


# --- fakes -------------------------------------------------------------------------


class _Response:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._done = True
        self._order.append("defer")


class _Followup:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.sent: list[dict[str, Any]] = []

    async def send(self, content=None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})
        self._order.append("followup.send")


class FakeInteraction:
    def __init__(self, *, guild_id: int | None = GUILD) -> None:
        self.order: list[str] = []
        self.id = 1
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=10)
        self.response = _Response(self.order)
        self.followup = _Followup(self.order)
        self.edits: list[dict[str, Any]] = []

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        self.order.append("edit_original_response")

    def last(self) -> dict[str, Any]:
        for group in (self.edits, self.followup.sent):
            if group:
                return group[-1]
        raise AssertionError(f"nothing was sent: order={self.order}")

    def text(self) -> str | None:
        return self.last().get("content")


@pytest.fixture
def league_lookup(monkeypatch):
    """Patches `resolve_league`'s own read -- the one read LD2-3 permits before
    the guard. `state.league_id` is what the click's cross-league check compares
    the button's own `league_id` against."""
    state = SimpleNamespace(league_id=LEAGUE, no_league=False, phase=Phase.REGULAR_SEASON)

    async def get_league_by_guild_id(ex, guild_id):
        if state.no_league:
            return None
        return SimpleNamespace(id=state.league_id, current_phase=state.phase)

    monkeypatch.setattr(context.league_repo, "get_league_by_guild_id", get_league_by_guild_id)
    return state


@pytest.fixture
def pool(monkeypatch):
    async def get_pool():
        return "pool"

    monkeypatch.setattr(leaders_pager, "get_pool", get_pool)


@pytest.fixture
def panel_read(monkeypatch):
    """Patches the one leaders read the click makes; `calls` records every
    invocation so a refused click can be proven to have read nothing."""
    calls: list[dict[str, Any]] = []
    state = SimpleNamespace(view=_view())

    async def get_leaders_panel(ex, **kwargs):
        calls.append(kwargs)
        return state.view

    monkeypatch.setattr(view_service, "get_leaders_panel", get_leaders_panel)
    return SimpleNamespace(calls=calls, state=state)


def _button(**overrides) -> LeadersNavButton:
    kwargs = dict(
        league_id=LEAGUE,
        season=SEASON,
        game_type=GameType.REGULAR,
        stat=LeaderStat.POINTS,
        mode=LeaderMode.PER_GAME,
        page=1,
        action="next",
    )
    kwargs.update(overrides)
    return LeadersNavButton(**kwargs)


# --- the class -----------------------------------------------------------------------


def test_the_button_overrides_callback_itself():
    assert LeadersNavButton.callback is not discord.ui.DynamicItem.callback
    assert "callback" in LeadersNavButton.__dict__


def test_the_wrapped_button_is_inert():
    button = _button()
    assert type(button.item).callback is discord.ui.Item.callback
    assert "callback" not in vars(button.item)


def test_an_unknown_action_cannot_be_constructed():
    with pytest.raises(ValueError):
        _button(action="veto")


# --- ids -------------------------------------------------------------------------------


def test_every_stat_mode_action_round_trips_under_68_chars_and_matches_whole():
    template = LeadersNavButton.__discord_ui_compiled_template__
    for stat in LeaderStat:
        for mode in LeaderMode:
            for action in ACTIONS:
                cid = leaders_custom_id(654_321, 2026, GameType.PRESEASON, stat, mode, 987, action)
                assert len(cid) <= 68, cid
                match = template.fullmatch(cid)
                assert match is not None, cid


async def test_a_20_digit_page_does_not_match_the_template():
    cid = "dba2:leaders:nav:1:2026:regular:points:pergame:" + "1" * 20 + ":prev"
    template = LeadersNavButton.__discord_ui_compiled_template__
    assert template.fullmatch(cid) is None


async def test_a_matching_but_out_of_range_page_clamps():
    cid = leaders_custom_id(
        1, 2026, GameType.REGULAR, LeaderStat.POINTS, LeaderMode.TOTAL, 999, "next"
    )
    match = LeadersNavButton.__discord_ui_compiled_template__.fullmatch(cid)
    built = await LeadersNavButton.from_custom_id(None, None, match)
    assert built.page == leaders_pager._MAX_PAGE


async def test_from_custom_id_is_total_never_raises_on_a_matching_string():
    cid = "dba2:leaders:nav:0:0:zz:zz:pergame:0:prev"
    match = LeadersNavButton.__discord_ui_compiled_template__.fullmatch(cid)
    assert match is not None
    built = await LeadersNavButton.from_custom_id(None, None, match)
    assert built.game_type is GameType.REGULAR  # degraded default
    assert built.stat is LeaderStat.POINTS  # degraded default
    assert built.page == 1  # clamped up from 0


async def test_a_malformed_id_degrades_to_a_fresh_default_view_rather_than_raising():
    cid = "dba2:leaders:nav:5:2026:bogus:bogus:pergame:1:prev"
    match = LeadersNavButton.__discord_ui_compiled_template__.fullmatch(cid)
    built = await LeadersNavButton.from_custom_id(None, None, match)
    assert built.game_type is GameType.REGULAR
    assert built.stat is LeaderStat.POINTS
    assert built.mode is LeaderMode.PER_GAME


# --- the view --------------------------------------------------------------------------


def _view_for(mode: LeaderMode, page: int, has_more: bool) -> discord.ui.View:
    return build_leaders_view(
        LEAGUE, SEASON, GameType.REGULAR, LeaderStat.POINTS, mode, page, has_more
    )


def test_view_timeout_is_none():
    assert _view_for(LeaderMode.PER_GAME, 1, True).timeout is None


def _states(page: int, has_more: bool, mode: LeaderMode) -> dict[str, bool]:
    view = _view_for(mode, page, has_more)
    return {child.action: child.item.disabled for child in view.children}


def test_prev_is_disabled_on_page_1_only():
    assert _states(1, True, LeaderMode.PER_GAME)["prev"] is True
    assert _states(2, True, LeaderMode.PER_GAME)["prev"] is False


def test_next_is_disabled_exactly_when_has_more_is_false():
    assert _states(1, False, LeaderMode.PER_GAME)["next"] is True
    assert _states(1, True, LeaderMode.PER_GAME)["next"] is False


def test_the_active_mode_button_is_disabled_and_primary_the_other_is_not():
    view = _view_for(LeaderMode.PER_GAME, 1, True)
    by_action = {c.action: c for c in view.children}
    assert by_action["pergame"].item.disabled is True
    assert by_action["pergame"].item.style is discord.ButtonStyle.primary
    assert by_action["total"].item.disabled is False
    assert by_action["total"].item.style is discord.ButtonStyle.secondary

    view2 = _view_for(LeaderMode.TOTAL, 1, True)
    by_action2 = {c.action: c for c in view2.children}
    assert by_action2["total"].item.disabled is True
    assert by_action2["pergame"].item.disabled is False


def test_leaders_view_or_missing_omits_the_view_for_the_no_games_branch():
    result = leaders_view_or_missing(
        league_id=LEAGUE, season=SEASON, game_type=GameType.REGULAR, stat=LeaderStat.POINTS,
        mode=LeaderMode.PER_GAME, page=1, has_more=False, no_games=True,
    )
    assert result is discord.utils.MISSING


def test_leaders_view_or_missing_omits_the_view_when_the_builder_raises(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("view builder broke")

    monkeypatch.setattr(leaders_pager, "build_leaders_view", boom)
    result = leaders_view_or_missing(
        league_id=LEAGUE, season=SEASON, game_type=GameType.REGULAR, stat=LeaderStat.POINTS,
        mode=LeaderMode.PER_GAME, page=1, has_more=False, no_games=False,
    )
    assert result is discord.utils.MISSING


# --- the click path (LD2-3) ------------------------------------------------------------


async def test_the_click_defers_before_any_read(league_lookup, pool, panel_read):
    click = FakeInteraction()
    await _button(action="next", page=1).callback(click)
    assert click.order[0] == "defer"


async def test_a_click_naming_another_leagues_league_is_refused_before_any_leaders_read(
    league_lookup, pool, panel_read
):
    league_lookup.league_id = LEAGUE
    click = FakeInteraction()
    await _button(league_id=OTHER_LEAGUE).callback(click)
    assert click.text() == leaders_pager._OTHER_SERVER
    assert panel_read.calls == []


async def test_no_league_for_guild_gives_the_same_refusal(league_lookup, pool, panel_read):
    league_lookup.no_league = True
    click = FakeInteraction()
    await _button().callback(click)
    assert click.text() == leaders_pager._OTHER_SERVER
    assert panel_read.calls == []


async def test_a_dm_click_is_refused_and_reads_nothing(league_lookup, pool, panel_read):
    click = FakeInteraction(guild_id=None)
    await _button().callback(click)
    assert click.text() == leaders_pager._OTHER_SERVER
    assert panel_read.calls == []


async def test_a_legal_click_edits_the_panel_and_view(league_lookup, pool, panel_read):
    click = FakeInteraction()
    await _button(action="next", page=1).callback(click)
    assert click.order == ["defer", "edit_original_response"]
    edit = click.edits[0]
    assert edit["embed"] is not None
    assert edit["view"] is not discord.utils.MISSING
    assert panel_read.calls[0]["page"] == 2  # "next" from page 1


async def test_prev_and_next_preserve_mode_and_shift_the_page(league_lookup, pool, panel_read):
    click = FakeInteraction()
    await _button(action="prev", page=3, mode=LeaderMode.TOTAL).callback(click)
    assert panel_read.calls[0]["page"] == 2
    assert panel_read.calls[0]["mode"] is LeaderMode.TOTAL


async def test_a_mode_toggle_switches_mode_and_keeps_the_page(league_lookup, pool, panel_read):
    click = FakeInteraction()
    await _button(action="total", page=2, mode=LeaderMode.PER_GAME).callback(click)
    assert panel_read.calls[0]["mode"] is LeaderMode.TOTAL
    assert panel_read.calls[0]["page"] == 2


async def test_the_click_omits_view_when_the_panel_has_no_games(league_lookup, pool, panel_read):
    panel_read.state.view = _view(no_games=True)
    click = FakeInteraction()
    await _button().callback(click)
    edit = click.edits[0]
    assert edit["view"] is discord.utils.MISSING


async def test_a_raising_view_builder_still_edits_the_panel_and_omits_view(
    league_lookup, pool, panel_read, monkeypatch
):
    def boom(*_a, **_kw):
        raise RuntimeError("view builder broke")

    monkeypatch.setattr(leaders_pager, "build_leaders_view", boom)
    click = FakeInteraction()
    await _button().callback(click)
    assert click.order == ["defer", "edit_original_response"]
    edit = click.edits[0]
    assert edit["view"] is discord.utils.MISSING
    assert edit["embed"] is not None


async def test_a_failing_read_is_answered_and_logged_never_swallowed(
    league_lookup, pool, monkeypatch, caplog
):
    async def boom(ex, **kwargs):
        raise RuntimeError("pool down")

    monkeypatch.setattr(view_service, "get_leaders_panel", boom)
    click = FakeInteraction()
    import logging

    with caplog.at_level(logging.ERROR):
        await _button().callback(click)
    from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE

    assert click.text() == GENERIC_FAILURE_MESSAGE
    assert any("pool down" in (r.exc_text or "") for r in caplog.records)
