"""`/wire`'s beat-filtered pager (`dba2/bot/wire_pager.py`, docs/design/
media-v2-newsroom.md MV-7, build step 7), without a database or a gateway.

What has teeth here:

- `beat_to_persona`: the six real choice tokens map to the right persona key
  (or `None` for `all`), and the `no-filter`/`the-beat` trap this module's
  own docstring names is pinned directly (`no-filter` is `hot_take`, NOT
  "no filter at all");
- the ids: the bounded template (a 20-digit `page` must never match at all),
  a matching-but-out-of-range `page` clamps, `from_custom_id` is total;
- the click path (mirroring LD2-3): `defer()` before any read, `resolve_
  league` before the guard, the guild check BEFORE any wire read, a raising
  view builder omits `view=` rather than stripping the panel;
- the view: `timeout=None`, Prev/Next disabled correctly.

No DB, no gateway: every read the click makes is patched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from dba2.bot import context, wire_pager
from dba2.bot.wire_pager import (
    WIRE_BEAT_CHOICES,
    WireNavButton,
    beat_to_persona,
    build_wire_view,
    wire_custom_id,
    wire_view_or_missing,
)
from dba2.domain.phase import Phase
from dba2.presentation.panel import Panel, PanelColor
from dba2.services import view_service
from dba2.services.view_service import WirePageView

LEAGUE = 42
OTHER_LEAGUE = 43
GUILD = 4242
SEASON = 2026


def _panel(text: str = "wire") -> Panel:
    return Panel(title="Streak", description=text, color=PanelColor.NEUTRAL, footer="a footer")


def _view(*, page: int = 1, has_more: bool = False, empty: bool = False) -> WirePageView:
    return WirePageView(panel=None if empty else _panel(), page=page, has_more=has_more)


# --- beat_to_persona: the real trap -------------------------------------------------


def test_all_choices_have_a_mapping_entry():
    for choice in WIRE_BEAT_CHOICES:
        # never raises a KeyError -- every offered choice value round-trips.
        beat_to_persona(choice.value)


def test_all_means_no_filter():
    assert beat_to_persona("all") is None


def test_no_filter_is_hot_takes_column_not_a_second_all():
    """The trap this module's own docstring names: "No Filter" is `hot_
    take`'s column display name (MV-3's table), not a synonym for `all`."""
    assert beat_to_persona("no-filter") == "hot_take"


def test_the_beat_is_beat_writer():
    assert beat_to_persona("the-beat") == "beat_writer"


def test_the_remaining_three_choices_map_to_their_own_persona_key():
    assert beat_to_persona("trade-desk") == "insider"
    assert beat_to_persona("film-room") == "analyst"
    assert beat_to_persona("long-view") == "historian"


def test_an_unrecognized_token_degrades_to_none_rather_than_raising():
    assert beat_to_persona("bogus") is None


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

    monkeypatch.setattr(wire_pager, "get_pool", get_pool)


@pytest.fixture
def page_read(monkeypatch):
    calls: list[dict[str, Any]] = []
    state = SimpleNamespace(view=_view())

    async def get_wire_page(ex, **kwargs):
        calls.append(kwargs)
        return state.view

    monkeypatch.setattr(view_service, "get_wire_page", get_wire_page)
    return SimpleNamespace(calls=calls, state=state)


def _button(**overrides) -> WireNavButton:
    kwargs = dict(league_id=LEAGUE, season=SEASON, beat="all", page=1, action="next")
    kwargs.update(overrides)
    return WireNavButton(**kwargs)


# --- the class -----------------------------------------------------------------------


def test_the_button_overrides_callback_itself():
    assert WireNavButton.callback is not discord.ui.DynamicItem.callback
    assert "callback" in WireNavButton.__dict__


def test_the_wrapped_button_is_inert():
    button = _button()
    assert type(button.item).callback is discord.ui.Item.callback
    assert "callback" not in vars(button.item)


def test_an_unknown_action_cannot_be_constructed():
    with pytest.raises(ValueError):
        _button(action="veto")


# --- ids -------------------------------------------------------------------------------


def test_every_beat_action_round_trips_under_68_chars_and_matches_whole():
    template = WireNavButton.__discord_ui_compiled_template__
    for choice in WIRE_BEAT_CHOICES:
        for action in ("prev", "next"):
            cid = wire_custom_id(654_321, 2026, choice.value, 987, action)
            assert len(cid) <= 68, cid
            match = template.fullmatch(cid)
            assert match is not None, cid


async def test_a_20_digit_page_does_not_match_the_template():
    cid = "dba2:wire:nav:1:2026:all:" + "1" * 20 + ":prev"
    template = WireNavButton.__discord_ui_compiled_template__
    assert template.fullmatch(cid) is None


async def test_a_matching_but_out_of_range_page_clamps():
    cid = wire_custom_id(1, 2026, "no-filter", 999, "next")
    match = WireNavButton.__discord_ui_compiled_template__.fullmatch(cid)
    built = await WireNavButton.from_custom_id(None, None, match)
    assert built.page == wire_pager._MAX_PAGE
    assert built.beat == "no-filter"


async def test_from_custom_id_is_total_never_raises_on_a_matching_string():
    cid = "dba2:wire:nav:0:0:all:0:prev"
    match = WireNavButton.__discord_ui_compiled_template__.fullmatch(cid)
    assert match is not None
    built = await WireNavButton.from_custom_id(None, None, match)
    assert built.page == 1  # clamped up from 0


# --- the view --------------------------------------------------------------------------


def test_view_timeout_is_none():
    assert build_wire_view(LEAGUE, SEASON, "all", 1, True).timeout is None


def _states(page: int, has_more: bool) -> dict[str, bool]:
    view = build_wire_view(LEAGUE, SEASON, "all", page, has_more)
    return {child.action: child.item.disabled for child in view.children}


def test_prev_is_disabled_on_page_1_only():
    assert _states(1, True)["prev"] is True
    assert _states(2, True)["prev"] is False


def test_next_is_disabled_exactly_when_has_more_is_false():
    assert _states(1, False)["next"] is True
    assert _states(1, True)["next"] is False


def test_wire_view_or_missing_omits_the_view_when_the_builder_raises(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("view builder broke")

    monkeypatch.setattr(wire_pager, "build_wire_view", boom)
    result = wire_view_or_missing(
        league_id=LEAGUE, season=SEASON, beat="all", page=1, has_more=True
    )
    assert result is discord.utils.MISSING


# --- the click path ----------------------------------------------------------------------


async def test_the_click_defers_before_any_read(league_lookup, pool, page_read):
    click = FakeInteraction()
    await _button(action="next", page=1).callback(click)
    assert click.order[0] == "defer"


async def test_a_click_naming_another_leagues_league_is_refused_before_any_wire_read(
    league_lookup, pool, page_read
):
    league_lookup.league_id = LEAGUE
    click = FakeInteraction()
    await _button(league_id=OTHER_LEAGUE).callback(click)
    assert click.text() == wire_pager._OTHER_SERVER
    assert page_read.calls == []


async def test_no_league_for_guild_gives_the_same_refusal(league_lookup, pool, page_read):
    league_lookup.no_league = True
    click = FakeInteraction()
    await _button().callback(click)
    assert click.text() == wire_pager._OTHER_SERVER
    assert page_read.calls == []


async def test_a_legal_click_edits_the_panel_and_view_and_queries_the_right_persona(
    league_lookup, pool, page_read
):
    click = FakeInteraction()
    await _button(action="next", page=1, beat="no-filter").callback(click)
    assert click.order == ["defer", "edit_original_response"]
    edit = click.edits[0]
    assert edit["embed"] is not None
    assert edit["view"] is not discord.utils.MISSING
    assert page_read.calls[0]["page"] == 2  # "next" from page 1
    assert page_read.calls[0]["beat"] == "hot_take"  # not None -- the no-filter trap


async def test_prev_shifts_the_page_down(league_lookup, pool, page_read):
    click = FakeInteraction()
    await _button(action="prev", page=3).callback(click)
    assert page_read.calls[0]["page"] == 2


async def test_the_click_sends_an_ephemeral_notice_when_the_beat_is_empty(
    league_lookup, pool, page_read
):
    page_read.state.view = _view(empty=True)
    click = FakeInteraction()
    await _button().callback(click)
    assert click.order == ["defer", "followup.send"]
    assert click.followup.sent[0]["ephemeral"] is True


async def test_a_raising_view_builder_still_edits_the_panel_and_omits_view(
    league_lookup, pool, page_read, monkeypatch
):
    def boom(*_a, **_kw):
        raise RuntimeError("view builder broke")

    monkeypatch.setattr(wire_pager, "build_wire_view", boom)
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

    monkeypatch.setattr(view_service, "get_wire_page", boom)
    click = FakeInteraction()
    import logging

    with caplog.at_level(logging.ERROR):
        await _button().callback(click)
    from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE

    assert click.text() == GENERIC_FAILURE_MESSAGE
    assert any("pool down" in (r.exc_text or "") for r in caplog.records)
