"""The `/watch` pager and the live component's cycle row (`dba2/bot/watch_pager.py`,
docs/design/watch-v4-live-ticker.md) without a database or a gateway.

What has teeth here, in the order it can hurt:

- the SPOILER CAP: a pager may show no play after `ComponentState.shown_through`, at
  every tick of a real paced broadcast and after it ends abnormally (a cancel, a run of
  failed edits, a lost final frame) when the scheduler has already forgotten the game;
- the ids: three dynamic-item templates that never both match one id, `from_custom_id`
  that cannot raise on an id that matches, ids under Discord's 100-character limit;
- the views: a view of dynamic items only leaves the registered classes alone when it
  times out (`ViewStore.remove_view`), pinned against the library, with a control; the
  cycle row's own shape (highlight, disabled flags, the OT cap, `timeout=None`);
- the cycle click's full path (T-5.d): the refusal gates, the clamp, the coalesce-or-
  paint cooldown (T-4.a), the bounded edit-lock ordering against the interrupted
  panel (T-5.d "Ordering"), and the view it rebuilds every time;
- every click answers itself: the wrong guild, an unknown period, a stale page, a closed
  block, a database that is down.

The mutation twins load a private copy of the module (or of `bot/broadcast.py`) with one
rule deleted and require the scenario that guards it to go red. Nothing here touches a
database: the degraded path's reads are patched by the `db` fixture, and a test that could
reach `get_pool` uses it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace

import discord
import pytest

from dba2.bot import broadcast, game_feed, watch_pager
from dba2.bot.broadcast import ComponentViewRequest
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.bot.offer_delivery import OfferActionButton
from dba2.bot.watch_pager import (
    PagerEntry,
    PagerRegistry,
    WatchCycleButton,
    WatchFullTextButton,
    WatchPageButton,
    build_component_view,
    build_pager_view,
    cycle_custom_id,
    full_custom_id,
    game_is_open,
    live_line_cap,
    page_custom_id,
)
from dba2.data.repositories import broadcast_repo
from dba2.domain.game import GameStatus
from dba2.presentation.broadcast import _LINE_SEPARATOR
from dba2.services import view_service
from tests.unit.bot._broadcast_fakes import (
    FakeMessage,
    FakeThread,
    fake_component_view_for,
    make_inputs,
    narration_for,
    period_lines_for,
    plan_for,
)
from tests.unit.bot._game_feed_fakes import LEAGUE
from tests.unit.bot._pager_fakes import (
    GAME,
    GUILD,
    OTHER_GUILD,
    PagerInteraction,
    bot_with,
    component_closures,
    entry_showing,
    full_source,
    pager_variant,
    progress_showing,
)
from tests.unit.bot._watch_fakes import BOX_PANEL
from tests.unit.bot.test_broadcast_scheduler import Harness, Repo

LIVE, INTERRUPTED = broadcast.BroadcastStatus.LIVE, broadcast.BroadcastStatus.INTERRUPTED
_SEP = _LINE_SEPARATOR

# --- the classes ------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [WatchCycleButton, WatchFullTextButton, WatchPageButton])
def test_the_button_overrides_callback_itself(cls):
    """A forgotten override still constructs, registers and renders -- and does nothing
    when clicked (the `OfferActionButton` pin, copied)."""
    assert cls.callback is not discord.ui.DynamicItem.callback
    assert "callback" in cls.__dict__


def test_the_wrapped_buttons_are_inert():
    buttons = [
        WatchCycleButton(1, 2, 0),
        WatchFullTextButton(1, 2),
        WatchPageButton(1, 2, 3, 4, "older"),
    ]
    for button in buttons:
        assert type(button.item).callback is discord.ui.Item.callback
        assert "callback" not in vars(button.item)


# --- ids --------------------------------------------------------------------------------------

_CYCLE_IDS = [cycle_custom_id(1, 412, 0), cycle_custom_id(2**63 - 1, 2**63 - 1, 12)]
_FULL_IDS = [full_custom_id(1, 412), full_custom_id(2**63 - 1, 2**63 - 1)]
_PAGE_IDS = [page_custom_id(1, 412, 2, 3, action) for action in watch_pager.ACTIONS] + [
    page_custom_id(2**63 - 1, 2**63 - 1, 99, 9999, "latest")
]
_ALL_IDS = {"cycle": _CYCLE_IDS, "full": _FULL_IDS, "page": _PAGE_IDS}


def _templates(module=watch_pager):
    return {
        "cycle": module.WatchCycleButton.__discord_ui_compiled_template__,
        "full": module.WatchFullTextButton.__discord_ui_compiled_template__,
        "page": module.WatchPageButton.__discord_ui_compiled_template__,
    }


def _exclusivity_violations(module=watch_pager) -> list[str]:
    """Ids two templates both match (`dispatch_dynamic_items` would run both callbacks), or
    that a template fails to match whole."""
    templates = _templates(module)
    found: list[str] = []
    for owner, ids in _ALL_IDS.items():
        for name, template in templates.items():
            for i in ids:
                matched = bool(template.fullmatch(i))
                if name == owner and not matched:
                    found.append(f"{i} matches neither")
                if name != owner and matched:
                    found.append(f"{i} matches the {name} template")
    return found


def test_the_three_templates_are_exclusive_and_each_matches_its_own_ids_whole():
    assert _exclusivity_violations() == []


def test_the_exclusivity_pin_has_teeth_an_overlapping_template_is_caught():
    overlapping = pager_variant(
        (
            r'_CYCLE_TEMPLATE = r"dba2:watch:cycle:'
            r'(?P<league_id>\d+):(?P<game_id>\d+):(?P<period>\d+)"',
            r'_CYCLE_TEMPLATE = r"dba2:watch:\w+:'
            r'(?P<league_id>\d+):(?P<game_id>\d+)(?::(?P<period>\d+))?"',
        )
    )
    assert _exclusivity_violations(pager_variant()) == []  # the control
    assert any("matches the cycle template" in v for v in _exclusivity_violations(overlapping))


@pytest.mark.parametrize(
    "custom_id",
    [
        "dba2:watch:cycle:",
        "dba2:watch:cycle:1:2",
        "dba2:watch:cycle:1:2:3:4",
        "dba2:watch:cycle:1:2:x",
        "dba2:watch:full:1",
        "dba2:watch:full:1:2:3",
        "dba2:watch:page:1:2:3:4:veto",
        "dba2:watch:page:1:2:3:4",
        "dba2:watch:page:1:2:3:4:older:extra",
        "dba2:watch:page:1:2:3:x:older",
        "dba2:offer:accept:12",
    ],
)
def test_a_malformed_id_matches_no_template(custom_id):
    """`dispatch_dynamic_items` uses `fullmatch`; the library's own construction check uses
    `match`, so a template that only PREFIX-matches would route a junk id to a handler."""
    assert not any(t.fullmatch(custom_id) for t in _templates().values())


@pytest.mark.parametrize("custom_id", _CYCLE_IDS + _FULL_IDS + _PAGE_IDS)
async def test_every_id_round_trips_and_stays_under_discords_limit(custom_id):
    assert len(custom_id) <= 100
    built = []
    for cls in (WatchCycleButton, WatchFullTextButton, WatchPageButton):
        match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
        if match is not None:
            built.append(await cls.from_custom_id(None, None, match))
    assert len(built) == 1 and built[0].custom_id == custom_id


@pytest.mark.parametrize(
    "custom_id",
    [
        cycle_custom_id(0, 0, 0),
        cycle_custom_id(10**40, 10**40, 10**10),
        full_custom_id(0, 0),
        page_custom_id(0, 0, 0, 0, "close"),
        page_custom_id(10**40, 10**40, 10**10, 10**10, "latest"),
    ],
)
async def test_from_custom_id_is_total_on_any_id_that_matches(custom_id):
    """A raise in `from_custom_id` is swallowed with no ack (`view.py:1011-1015`), so the
    only defence is that it cannot raise: the callback re-validates the meaning."""
    matched = 0
    for cls in (WatchCycleButton, WatchFullTextButton, WatchPageButton):
        match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
        if match is not None:
            matched += 1
            built = await cls.from_custom_id(None, None, match)
            assert built.custom_id == custom_id
    assert matched == 1, custom_id


def test_an_unknown_pager_action_cannot_be_constructed():
    with pytest.raises(ValueError):
        WatchPageButton(1, 2, 3, 4, "veto")


# --- views ------------------------------------------------------------------------------------


def test_a_view_of_dynamic_items_leaves_the_classes_registered_when_it_ends():
    """An ephemeral followup's view gets a 15-minute timeout, and `ViewStore.remove_view`
    pops a view's dynamic-item patterns when it ends -- unless the view never got a
    dispatch entry, which is the case for a view made only of `DynamicItem`s. This pins
    that library fact (`view.py:964-981`): if an upgrade changed it, a component view
    timing out would unregister every class for the whole bot."""
    store = discord.ui.view.ViewStore(None)
    store.add_dynamic_items(WatchCycleButton, WatchFullTextButton, WatchPageButton)
    cycle_request = ComponentViewRequest(periods=(1, 2), selected=0, disabled=False)
    views = [
        build_component_view(1, GAME, cycle_request),
        build_pager_view(1, GAME, 2, 3, 5),
    ]
    for view in views:
        store.add_view(view, 5)
        store.remove_view(view)  # what a timeout does
        for cls in (WatchCycleButton, WatchFullTextButton, WatchPageButton):
            assert cls.__discord_ui_compiled_template__ in store._dynamic_items


def test_the_pin_has_teeth_a_view_with_an_ordinary_button_would_unregister_them():
    """The control: mix one plain button into the view and the same removal takes the
    dynamic-item pattern with it (`dispatch_info` is then non-empty)."""
    store = discord.ui.view.ViewStore(None)
    store.add_dynamic_items(WatchPageButton)
    mixed = build_pager_view(1, GAME, 2, 3, 5)
    mixed.add_item(discord.ui.Button(label="x", custom_id="plain:1"))
    store.add_view(mixed, 5)
    store.remove_view(mixed)
    assert WatchPageButton.__discord_ui_compiled_template__ not in store._dynamic_items


def test_the_pager_row_has_four_buttons_and_disables_what_cannot_move():
    """Only Older on page 1 and Newer on the newest page are ever disabled. Latest is the
    refresh button of a LIVE quarter (it re-reads the cap), so it is never disabled."""

    def state(page, pages):
        row = build_pager_view(1, GAME, 2, page, pages).children
        return {b.item.label: b.item.disabled for b in row}

    assert state(1, 3) == {"◀ Older": True, "▶ Newer": False, "⟳ Latest": False, "✖ Close": False}
    assert state(3, 3) == {"◀ Older": False, "▶ Newer": True, "⟳ Latest": False, "✖ Close": False}
    assert state(1, 1) == {"◀ Older": True, "▶ Newer": True, "⟳ Latest": False, "✖ Close": False}


def test_component_view_timeout_is_none():
    """T-8 #19: rebuilt every tick and again on every click, so it must never start a
    throwaway timeout task of its own (critic 14)."""
    request = ComponentViewRequest(periods=(1, 2, 3), selected=0, disabled=False)
    assert build_component_view(1, GAME, request).timeout is None


def test_the_cycle_row_highlights_the_selection_and_pins_full_text_to_row_4():
    request = ComponentViewRequest(periods=(1, 2, 3), selected=2, disabled=False)
    view = build_component_view(1, GAME, request)
    *cycle, full_text = view.children
    assert [c.period for c in cycle] == [0, 1, 2, 3]
    assert [c.item.style for c in cycle] == [
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
    ]
    assert all(not c.item.disabled for c in cycle)
    assert full_text.item.row == 4 and not full_text.item.disabled
    assert [c.item.label for c in cycle] == ["Full", "Q1", "Q2", "Q3"]


def test_a_disabled_request_disables_every_cycle_button_but_never_full_text():
    request = ComponentViewRequest(periods=(1,), selected=0, disabled=True)
    view = build_component_view(1, GAME, request)
    *cycle, full_text = view.children
    assert all(c.item.disabled for c in cycle)
    assert not full_text.item.disabled


def test_the_ot_ceiling_keeps_full_q1_through_q4_and_the_15_most_recent_overtimes():
    """T-2.b, T-8 #20: a synthetic 20-period plan (regulation + 16 overtimes) builds a
    view that does not raise, keeps `Full`, `Q1..Q4` and the 15 most recent OTs, and
    puts the full-text button on row 4."""
    periods = tuple(range(1, 21))  # Q1-Q4 plus OT1-OT16
    request = ComponentViewRequest(periods=periods, selected=0, disabled=False)
    view = build_component_view(1, GAME, request)
    *cycle, full_text = view.children
    assert len(cycle) == 20  # Full + 19
    kept = [c.period for c in cycle]
    assert kept[:5] == [0, 1, 2, 3, 4]
    assert kept[5:] == list(range(6, 21))  # OT2..OT16: the 15 most recent, OT1 (period 5) dropped
    assert full_text.item.row == 4


# --- the cap ----------------------------------------------------------------------------------


def test_the_cap_is_shown_through_and_minus_one_when_nothing_shown():
    entry = entry_showing(-1)
    assert live_line_cap(entry.progress) == -1
    entry2 = entry_showing(51)
    assert live_line_cap(entry2.progress) == 51


def test_available_lines_are_the_periods_prefix_up_to_the_cap_and_none_for_an_unknown_period():
    entry = entry_showing(29)  # Q1 has well over 30 real lines; cap 29 shows its first 30
    assert entry.available_lines(1) == entry.source.periods[1].lines[:30]
    assert entry.available_lines(2) == ()  # Q2 has not started (first_index > cap)
    assert entry.available_lines(99) is None


def test_a_global_cap_past_a_period_shows_it_whole_even_with_no_separate_post_of_its_own():
    """The cap is ONE number (`ComponentState.shown_through`), not a per-period flag: if
    it already sits inside Q2, Q1 (entirely before it) reads fully available.

    The cap is computed from Q1's own real line count (BDA-4 fix,
    docs/design/block-defender-attribution.md: previously hardcoded at 45,
    which the weak-side rim-help contest's shift to which fixed seed
    `sim_result()` uses -- see `_broadcast_fakes.py`'s own note -- changed
    to 52; deriving it live means a future reseed can't go stale here
    either)."""
    q1_len = len(entry_showing(-1).source.periods[1].lines)
    cap = q1_len + 3  # 4 lines into Q2: indices q1_len .. q1_len+3 inclusive
    entry = entry_showing(cap)
    assert entry.available_lines(1) == entry.source.periods[1].lines  # all of Q1
    assert len(entry.available_lines(2)) == 4
    assert entry.available_lines(3) == ()


# --- the registry -----------------------------------------------------------------------------


def test_registering_a_game_prunes_games_that_are_neither_in_a_block_nor_broadcasting():
    registry = PagerRegistry()
    entry = entry_showing(-1)
    open_games = {(1, 10)}
    registry.register(1, 10, entry, keep=lambda lg, g: (lg, g) in open_games)
    registry.register(1, 11, entry, keep=lambda lg, g: (lg, g) in open_games)  # 10 is kept
    assert registry.get(1, 10) is entry and registry.get(1, 11) is entry
    open_games.clear()
    registry.register(1, 12, entry, keep=lambda lg, g: (lg, g) in open_games)
    assert registry.get(1, 10) is None and registry.get(1, 11) is None
    assert registry.get(1, 12) is entry and len(registry) == 1


def _prune_scenario(module) -> None:
    """Game 10 is still broadcasting with its block closed (`close_block` left it alone);
    watching game 11 must not drop it."""
    registry = module.PagerRegistry()
    entry = entry_showing(-1)
    live = {(1, 10)}
    registry.register(1, 10, entry, keep=lambda lg, g: (lg, g) in live)
    registry.register(1, 11, entry, keep=lambda lg, g: (lg, g) in live)
    assert registry.get(1, 10) is entry, "a still-broadcasting game lost its pager"


def test_a_still_broadcasting_game_keeps_its_pager_when_another_is_registered():
    _prune_scenario(pager_variant())


def test_dropping_entries_wholesale_is_caught():
    wholesale = pager_variant(("if k != key and not keep(*k)]", "if k != key]"))
    with pytest.raises(AssertionError):
        _prune_scenario(wholesale)


def test_a_game_is_open_while_its_block_holds_it_or_its_broadcast_is_live():
    entry = entry_showing(-1)
    assert game_is_open(bot_with(entry), LEAGUE, GAME)  # block only
    assert game_is_open(bot_with(entry, in_block=False, live=True), LEAGUE, GAME)  # broadcast only
    assert not game_is_open(bot_with(entry, in_block=False), LEAGUE, GAME)
    assert not game_is_open(bot_with(entry), LEAGUE, GAME + 1)  # another game, same league
    assert not game_is_open(bot_with(entry), LEAGUE + 1, GAME)  # same id, another league


# --- the full-text button ----------------------------------------------------------------------


def _ids(interaction: PagerInteraction) -> list[str]:
    return [child.custom_id for child in interaction.last()["view"].children]


async def test_full_text_defers_first_then_sends_the_selected_periods_newest_page():
    entry = entry_showing(29)
    click = PagerInteraction(bot_with(entry))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)  # Full Game (period 0) by default
    assert click.order == ["defer", "followup.send"]
    assert click.response.deferred == [{"ephemeral": True, "thinking": True}]
    sent = click.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].title.startswith("Full Game ·")
    assert sent["embed"].author.name == "AWY @ HOM"


async def test_full_text_reads_the_shared_selected_period_not_a_baked_in_one():
    """T-5.f: the id carries no period; a stale open message still answers the
    component's LIVE selection."""
    entry = entry_showing(29, selected_period=1)
    click = PagerInteraction(bot_with(entry))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.embed().title.startswith("Q1 ·")


async def test_a_period_the_game_never_played_says_so_and_nothing_else():
    entry = entry_showing(29, selected_period=9)
    click = PagerInteraction(bot_with(entry))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == f"That quarter isn't part of game {GAME}."
    assert "embed" not in click.last() and "view" not in click.last()


async def test_a_period_not_started_yet_shows_no_plays_never_its_lines():
    entry = entry_showing(-1, selected_period=0)
    click = PagerInteraction(bot_with(entry))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.embed().title == "Full Game · no plays yet"


# --- pager page navigation (unchanged machinery, watch-v3 W-10..W-14) ---------------------------


@pytest.mark.parametrize(
    ("action", "page", "title"),
    [
        ("older", 4, "Q1 · plays 17-24 of 30"),
        ("older", 1, "Q1 · plays 1-8 of 30"),
        ("newer", 2, "Q1 · plays 17-24 of 30"),
        ("newer", 4, "Q1 · plays 25-30 of 30"),
        ("latest", 1, "Q1 · plays 25-30 of 30"),
    ],
)
async def test_navigation_answers_with_one_edit_and_no_await_before_it(action, page, title):
    click = PagerInteraction(bot_with(entry_showing(29)))
    await WatchPageButton(LEAGUE, GAME, 1, page, action).callback(click)
    assert click.order == ["edit_message"]
    edit = click.response.edits[0]
    assert edit["embed"].title == title and edit["content"] is None
    assert edit["view"] is not None


async def test_a_stale_click_past_the_available_pages_lands_on_the_newest_and_never_errors():
    bot = bot_with(entry_showing(29))
    click = PagerInteraction(bot)
    await WatchPageButton(LEAGUE, GAME, 1, 99, "newer").callback(click)
    edit = click.response.edits[0]
    assert edit["embed"].footer.text == "Page 4 of 4 · only you can see this · newest"
    first = edit["view"].children[0].custom_id
    assert first == page_custom_id(LEAGUE, GAME, 1, 4, "older")
    stale_low = PagerInteraction(bot)
    await WatchPageButton(LEAGUE, GAME, 1, 0, "older").callback(stale_low)
    assert stale_low.response.edits[0]["embed"].title == "Q1 · plays 1-8 of 30"


async def _stale_click_scenario(module) -> None:
    click = PagerInteraction(bot_with(entry_showing(29)))
    await module.WatchPageButton(LEAGUE, GAME, 1, 99, "newer").callback(click)
    ids = [c.custom_id for c in click.response.edits[0]["view"].children]
    assert ids[0] == page_custom_id(LEAGUE, GAME, 1, 4, "older"), ids[0]


async def test_the_clamp_is_caught_when_removed_from_the_click():
    await _stale_click_scenario(pager_variant())  # the control
    unclamped = pager_variant(
        (
            "shown = clamp_page(_target_page(action, page, pages), len(lines))",
            "shown = _target_page(action, page, pages)",
        )
    )
    with pytest.raises(AssertionError):
        await _stale_click_scenario(unclamped)


def _enabled(view) -> dict[str, bool]:
    """Which of a pager row's buttons a viewer can press, by action."""
    return {b.custom_id.rsplit(":", 1)[1]: not b.item.disabled for b in view.children}


async def _plays_arrive_scenario(module) -> None:
    """A viewer opens the pager on a LIVE quarter, then more plays are broadcast. Latest
    must be pressable on the page they are looking at and must reveal the new plays --
    and only those, never one past the cap."""
    entry = entry_showing(29)
    q1 = entry.source.periods[1].lines
    bot = bot_with(entry)
    opened = PagerInteraction(bot)
    await module.WatchFullTextButton(LEAGUE, GAME).callback(opened)
    assert opened.embed().title == "Full Game · plays 25-30 of 30"
    row = _enabled(opened.last()["view"])
    assert row["latest"], "Latest is disabled on the newest page: no refresh"
    assert row == {"older": True, "newer": False, "latest": True, "close": True}

    entry.progress.component.shown_through = 33  # four more plays arrive
    refresh = PagerInteraction(bot)
    await module.WatchPageButton(LEAGUE, GAME, 0, 4, "latest").callback(refresh)
    edit = refresh.response.edits[0]
    assert edit["embed"].title == "Full Game · plays 33-34 of 34"
    assert edit["embed"].description.split(_SEP) == list(q1[32:34])
    assert q1[34] not in edit["embed"].description, "a play past the cap"
    assert _enabled(edit["view"]) == {"older": True, "newer": False, "latest": True, "close": True}

    again = PagerInteraction(bot)  # nothing new: the same page, re-rendered, no error
    await module.WatchPageButton(LEAGUE, GAME, 0, 5, "latest").callback(again)
    assert again.order == ["edit_message"]
    assert again.response.edits[0]["embed"].title == "Full Game · plays 33-34 of 34"


async def test_latest_reveals_plays_that_arrive_after_the_pager_opened_and_never_past_the_cap():
    await _plays_arrive_scenario(watch_pager)


async def test_close_replaces_the_pager_with_a_word_and_no_buttons():
    click = PagerInteraction(bot_with(entry_showing(29)))
    await WatchPageButton(LEAGUE, GAME, 1, 2, "close").callback(click)
    assert click.order == ["edit_message"]
    assert click.response.edits[0] == {"content": "Closed.", "embed": None, "view": None}


# --- authorization -----------------------------------------------------------------------------


async def _cross_guild_scenario(module) -> None:
    bot = bot_with(entry_showing(29))
    for guild in (OTHER_GUILD, None):
        click = PagerInteraction(bot, guild_id=guild)
        await module.WatchPageButton(LEAGUE, GAME, 1, 2, "older").callback(click)
        edit = click.response.edits[0]
        assert edit["content"] == "That button belongs to another server's league.", guild
        assert edit["embed"] is None and edit["view"] is None


async def test_a_click_from_another_guild_or_a_dm_is_refused_and_reads_nothing():
    await _cross_guild_scenario(pager_variant())


async def test_removing_the_guild_check_is_caught():
    """`_answer_from_entry`'s guild check (`WatchPageButton`/`WatchFullTextButton`'s
    shared path) has the identical literal in `_handle_cycle_click` too, so the anchor
    is disambiguated by its own surrounding line."""
    guild_comment = (
        '    # The guild check comes BEFORE `game_is_open`: otherwise a click from another '
        "server\n"
        '    # could tell "this game is being watched" (pages or an error) from "the replay '
        'is gone".\n'
    )
    unguarded = pager_variant(
        (
            guild_comment
            + "    if interaction.guild_id is None or interaction.guild_id != entry.guild_id:\n",
            guild_comment + "    if False:\n",
        )
    )
    with pytest.raises(AssertionError):
        await _cross_guild_scenario(unguarded)


# --- the degraded path -------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """Patches every read the degraded path makes (so nothing reaches a database) and
    records the box-score reads."""
    state = SimpleNamespace(
        league_id=LEAGUE,
        no_league=False,
        game=SimpleNamespace(league_id=LEAGUE, status=GameStatus.FINAL),
        row=SimpleNamespace(is_revealed=False),
        boxscores=[],
        pool_error=None,
    )

    async def get_pool():
        if state.pool_error is not None:
            raise state.pool_error
        return "pool"

    async def league_by_guild(ex, guild_id):
        return None if state.no_league else SimpleNamespace(id=state.league_id)

    async def get_game(ex, game_id):
        return state.game

    async def get_broadcast(ex, *, league_id, game_id):
        return state.row

    async def boxscore(ex, *, game_id):
        state.boxscores.append(game_id)
        return BOX_PANEL

    monkeypatch.setattr(watch_pager, "get_pool", get_pool)
    monkeypatch.setattr("dba2.bot.context.league_repo.get_league_by_guild_id", league_by_guild)
    monkeypatch.setattr(watch_pager.game_repo, "get_game", get_game)
    monkeypatch.setattr(broadcast_repo, "get_broadcast", get_broadcast)
    monkeypatch.setattr(view_service, "get_boxscore_panel", boxscore)
    return state


def _gone_bot():
    return bot_with(None)  # no entry: a restart, or a block that closed


async def test_no_entry_and_no_public_score_says_the_replay_is_gone_and_leaks_nothing(db):
    click = PagerInteraction(_gone_bot())
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.order == ["defer", "followup.send"]
    assert click.text() == watch_pager._GONE
    assert "embed" not in click.last() and db.boxscores == []


async def test_a_public_result_gets_the_box_score_with_the_gone_sentence(db):
    db.row = SimpleNamespace(is_revealed=True)
    click = PagerInteraction(_gone_bot())
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == watch_pager._GONE_REVEALED and click.embed() is not None
    assert db.boxscores == [GAME]


async def test_a_game_with_no_broadcast_row_is_not_watchable_and_reads_no_box_score(db):
    db.row = None
    click = PagerInteraction(_gone_bot())
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == watch_pager._NOT_IN_A_BLOCK and "embed" not in click.last()
    assert game_feed.box_score_is_public(None) and db.boxscores == []
    nav = PagerInteraction(_gone_bot())
    await WatchPageButton(LEAGUE, GAME, 1, 1, "newer").callback(nav)
    assert nav.original_edits[0]["content"] == watch_pager._NOT_IN_A_BLOCK
    assert db.boxscores == []


async def test_a_closed_block_with_nobody_broadcasting_degrades_even_with_an_entry(db):
    click = PagerInteraction(bot_with(entry_showing(29), in_block=False))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == watch_pager._GONE


async def test_a_block_closed_while_the_broadcast_is_still_running_keeps_paging(db):
    bot = bot_with(entry_showing(29), in_block=False, live=True)
    click = PagerInteraction(bot)
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.embed().title == "Full Game · plays 25-30 of 30"
    assert db.boxscores == []


async def test_a_navigation_click_with_no_entry_defers_then_edits_the_message_in_place(db):
    click = PagerInteraction(_gone_bot())
    await WatchPageButton(LEAGUE, GAME, 1, 2, "newer").callback(click)
    assert click.order == ["defer", "edit_original_response"]
    assert click.original_edits[0] == {"content": watch_pager._GONE, "embed": None, "view": None}


async def test_a_click_naming_another_leagues_game_is_refused_before_any_read(db):
    db.league_id = LEAGUE + 1
    click = PagerInteraction(_gone_bot())
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == "That button belongs to another server's league."
    assert db.boxscores == []


async def test_a_failing_read_is_answered_and_logged_never_swallowed(db, caplog):
    db.pool_error = RuntimeError("pool down")
    click = PagerInteraction(_gone_bot())
    with caplog.at_level(logging.ERROR):
        await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == GENERIC_FAILURE_MESSAGE
    assert any("pool down" in (r.exc_text or "") for r in caplog.records)


async def test_a_renderer_that_raises_is_answered_by_the_callback_itself(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(watch_pager, "render_pager_page", boom)
    click = PagerInteraction(bot_with(entry_showing(29)))
    await WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.text() == GENERIC_FAILURE_MESSAGE


async def _failing_send_scenario(module) -> None:
    click = PagerInteraction(bot_with(entry_showing(29)), followup_failures=1)
    with contextlib.suppress(Exception):
        await module.WatchFullTextButton(LEAGUE, GAME).callback(click)
    assert click.followup.sent, "the send failed and the viewer was left with no reply"
    assert click.text() == GENERIC_FAILURE_MESSAGE


async def test_a_followup_send_that_itself_fails_still_ends_in_an_error_reply(caplog):
    with caplog.at_level(logging.ERROR):
        await _failing_send_scenario(watch_pager)
    assert any("followup.send failed" in (r.exc_text or "") for r in caplog.records)


# --- the cycle click: the full path (watch-v4 T-5.d) --------------------------------------------


def _cycle_entry(**kwargs) -> PagerEntry:
    kwargs.setdefault("message_id", 555)
    return entry_showing(300, **kwargs)  # every period visible by default


def _entry_and_plan(shown_through: int, **kwargs):
    """Like `entry_showing`, but also returns the plan: a REAL `_edit_interrupted` call
    (T-8 #27's tests) needs a `BroadcastJob`, which needs the same plan the entry's
    `component`/`visible` closures were built over."""
    source, plan = full_source()
    component, visible = component_closures(plan)
    progress = progress_showing(shown_through=shown_through, **kwargs)
    entry = PagerEntry(
        guild_id=GUILD, source=source, progress=progress, component=component, visible=visible
    )
    return entry, plan


async def _noop_on_ended(*, league_id, game_id, status) -> None:
    pass


def _interrupted_job(plan, *, message: FakeMessage, thread: FakeThread | None = None):
    """A minimal `BroadcastJob` good enough to drive the REAL
    `BroadcastScheduler._edit_interrupted` in a test, with no database and no gateway."""
    return broadcast.BroadcastJob(
        league_id=LEAGUE,
        game_id=GAME,
        user_id=10,
        plan=plan,
        inputs=make_inputs(broadcast),
        message=message,
        thread=thread or FakeThread(),
        db=object(),
        component_view_for=lambda request: build_component_view(LEAGUE, GAME, request),
    )


async def test_the_cycle_click_writes_the_selection_and_the_next_render_sees_it():
    """T-8 #13: the shared object, not a copy -- writing through the click is visible
    to anyone else holding the same `PagerEntry.progress`."""
    entry = _cycle_entry(more_ticks=False)
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 2).callback(click)
    assert entry.progress.component.selected_period == 2
    assert click.response.edits[0]["embed"].fields[1].name.startswith("Q2 ·")


async def test_a_click_clamps_a_period_the_broadcast_has_not_reached_yet():
    entry = entry_showing(0, more_ticks=False)  # only Q1 has started
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 3).callback(click)  # Q3 not visible: clamp to Q1
    assert entry.progress.component.selected_period == 1


async def test_no_entry_refuses_ephemerally_and_edits_nothing():
    click = PagerInteraction(bot_with(None))
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order == ["send_message"]
    assert click.response.messages[0][0] == watch_pager._GONE


async def test_a_superseded_message_is_refused_and_points_at_the_new_one():
    entry = _cycle_entry(more_ticks=False, message_id=999)
    click = PagerInteraction(bot_with(entry), message_id=555)  # stale message id
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order == ["send_message"]
    assert click.response.messages[0][0] == watch_pager._SUPERSEDED


async def test_a_click_with_no_interaction_message_is_refused_not_silently_passed():
    """MINOR 10 (critic round 1): `interaction.message is None` must be handled
    explicitly rather than silently skipping the supersession gate, which would let an
    unidentifiable click fall through as if it matched `state.message_id`."""
    entry = _cycle_entry(more_ticks=False)
    click = PagerInteraction(bot_with(entry), message_id=None)
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order == ["send_message"]
    assert click.response.messages[0][0] == watch_pager._SUPERSEDED


async def test_nothing_broadcast_yet_refuses_ephemerally():
    entry = entry_showing(-1)
    entry.progress.component.frame_index = -1
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.response.messages[0][0] == watch_pager._NOTHING_YET


async def test_a_terminal_broadcast_refuses_every_click_and_edits_nothing():
    """T-8 #15."""
    entry = _cycle_entry(terminal=True, more_ticks=False)
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order == ["send_message"]
    assert click.response.messages[0][0] == watch_pager._ENDED
    assert click.response.edits == []


async def test_cooldown_coalesces_clicks_while_more_ticks_is_true_and_the_last_wins():
    """T-8 #17, `more_ticks=True` regime: N clicks inside one `tick_seconds` window
    produce exactly one paint (none, here -- they all defer) and N-1 bare defers, and
    the last click's period is what a later tick would render."""
    entry = _cycle_entry(more_ticks=True, last_edit_monotonic=time.monotonic())
    bot = bot_with(entry)
    clicks = [PagerInteraction(bot) for _ in range(3)]
    for period, click in zip((1, 2, 3), clicks, strict=True):
        await WatchCycleButton(LEAGUE, GAME, period).callback(click)
    assert all(c.order == ["defer"] for c in clicks)
    assert entry.progress.component.selected_period == 3


async def test_cooldown_does_not_apply_once_more_ticks_is_false():
    """T-8 #17, `more_ticks=False` regime: every click paints immediately, no defers,
    and the panel and the selection agree after every one. Mutation (T-8 #17's twin):
    gating the cooldown on `not state.terminal` instead of `more_ticks` would coalesce
    here, since `state.terminal` is False."""
    entry = _cycle_entry(more_ticks=False, last_edit_monotonic=time.monotonic())
    bot = bot_with(entry)
    for period in (1, 2, 3):
        click = PagerInteraction(bot)
        await WatchCycleButton(LEAGUE, GAME, period).callback(click)
        assert click.order == ["edit_message"]
        assert entry.progress.component.selected_period == period


async def test_the_cooldown_gate_on_terminal_instead_of_more_ticks_is_caught():
    """The exact must-fire mutation T-8 #17 names."""
    entry = entry_showing(
        300, more_ticks=False, terminal=False, last_edit_monotonic=time.monotonic()
    )
    bot = bot_with(entry)
    mutated = pager_variant(
        (
            "if state.more_ticks and (now - state.last_edit_monotonic) < tick_seconds:",
            "if not state.terminal and (now - state.last_edit_monotonic) < tick_seconds:",
        )
    )
    click = PagerInteraction(bot)
    await mutated.WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order == ["defer"], "the FINISHED case coalesced into a tick that will never come"


async def test_the_cooldown_floor_still_coalesces_n_clicks_across_the_faster_tick():
    """T-8 #17 re-asserted at `tick_seconds = 1.5` (watch-v5 WT-2.a): 3 clicks arriving
    2.0s, a beat later and a beat after that past the last edit -- each already past the
    un-floored 1.5s tick, all still inside the floored 4.0s cooldown -- still coalesce,
    exactly as v4 T-8 #17 asserted at the old 4.0s tick. Mutation: read
    `settings.tick_seconds` directly (no floor) -> red, checked directly below rather
    than inferred, since a click landing inside BOTH the floored and the un-floored
    window would pass either way."""
    entry = _cycle_entry(more_ticks=True, last_edit_monotonic=time.monotonic() - 2.0)
    bot = bot_with(entry)
    assert bot.broadcasts.settings.tick_seconds == 1.5  # sanity: the default IS the fast tick
    clicks = [PagerInteraction(bot) for _ in range(3)]
    for period, click in zip((1, 2, 3), clicks, strict=True):
        await WatchCycleButton(LEAGUE, GAME, period).callback(click)
    assert all(c.order == ["defer"] for c in clicks)
    assert entry.progress.component.selected_period == 3


async def test_the_cooldown_floor_removed_is_caught():
    """The exact must-fire mutation T-8 #17/WT-2.a names: without the floor, a click
    2.0s after the last edit is past the real 1.5s tick and paints instead of
    coalescing."""
    entry = _cycle_entry(more_ticks=True, last_edit_monotonic=time.monotonic() - 2.0)
    bot = bot_with(entry)
    mutated = pager_variant(
        (
            "tick_seconds = max(bot.broadcasts.settings.tick_seconds, "
            "_CLICK_COOLDOWN_FLOOR_SECONDS)",
            "tick_seconds = bot.broadcasts.settings.tick_seconds",
        )
    )
    click = PagerInteraction(bot)
    await mutated.WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    assert click.order != ["defer"], "without the floor the faster tick should have painted"


async def test_the_click_rebuilds_both_the_embed_and_the_view():
    """T-8 #25: one edit carries both; the cycle row highlights the clicked period,
    nothing is disabled, and the full-text button is present and enabled."""
    entry = _cycle_entry(more_ticks=False)
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 2).callback(click)
    edit = click.response.edits[0]
    assert edit["embed"] is not None and edit["view"] is not None
    view = edit["view"]
    cycle = [c for c in view.children if isinstance(c, WatchCycleButton)]
    assert not any(c.item.disabled for c in cycle)
    highlighted = [c for c in cycle if c.item.style is discord.ButtonStyle.primary]
    assert [c.period for c in highlighted] == [2]
    full_text = next(c for c in view.children if isinstance(c, WatchFullTextButton))
    assert not full_text.item.disabled


async def test_the_click_still_edits_when_the_view_builder_raises_and_omits_the_view_key():
    """T-8 #26: keep the panel, drop the view. `interaction.response.edit_message`'s own
    convention is `_bounded_edit`'s (T-6): `discord.utils.MISSING` OMITS the key at the
    wire level (the same default `edit_message` itself uses), so the click always passes
    `view=` explicitly, and the failure path sets it to `MISSING` rather than `None`
    (which would STRIP the row)."""
    entry = _cycle_entry(more_ticks=False)
    click = PagerInteraction(bot_with(entry))

    def boom(*_a, **_kw):
        raise RuntimeError("view builder broke")

    import dba2.bot.watch_pager as wp

    original = wp.build_component_view
    wp.build_component_view = boom
    try:
        await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    finally:
        wp.build_component_view = original
    assert click.order == ["edit_message"]
    edit = click.response.edits[0]
    assert edit["view"] is discord.utils.MISSING
    assert edit["embed"] is not None


async def test_w7_advances_shown_through_and_stamps_the_cooldown_only_on_a_painting_click():
    entry = _cycle_entry(more_ticks=False, last_edit_monotonic=0.0)
    click = PagerInteraction(bot_with(entry))
    before = entry.progress.component.shown_through
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    state = entry.progress.component
    assert state.shown_through == max(before, state.line_index)
    assert state.last_edit_monotonic > 0.0


async def _spoiler_snapshot_scenario(module) -> None:
    """BLOCKING 1 (critic round 1): the pacer takes no lock, so its W1 write
    (`state.line_index`) can land WHILE a click's own `edit_message` is still in flight.
    `shown_through` must cap at the line index the click's OWN render used (the step-9
    snapshot), never a fresh read of `state.line_index` taken after the await -- a fresh
    read lets the full-text pager reveal possessions this render never painted. Reproduces
    the critic's repro: painted at window 23, a simulated tick advances to 29 mid-edit."""
    entry = entry_showing(23, line_index=23, more_ticks=True, last_edit_monotonic=0.0)
    state = entry.progress.component
    click = PagerInteraction(bot_with(entry))
    started = asyncio.Event()
    resume = asyncio.Event()

    async def slow_edit_message(**kwargs):
        started.set()
        await resume.wait()

    click.response.edit_message = slow_edit_message  # type: ignore[method-assign]

    click_task = asyncio.create_task(module.WatchCycleButton(LEAGUE, GAME, 1).callback(click))
    await started.wait()  # the render is done; the click is awaiting Discord's answer
    state.line_index = 29  # the pacer's own W1 write, landing mid-flight
    resume.set()
    await click_task

    assert state.shown_through == 23, "the cap advanced past what this click actually painted"


async def test_a_click_never_caps_past_what_its_own_render_painted():
    await _spoiler_snapshot_scenario(pager_variant())  # the control


async def test_reading_line_index_fresh_at_w7_instead_of_the_snapshot_is_caught():
    """The exact mutation the critic found: re-reading `state.line_index` at W7 instead of
    the step-9 snapshot lets the cap jump to whatever the pacer had reached by the time
    Discord answered."""
    stale = pager_variant(
        (
            "state.shown_through = max(state.shown_through, painted_line_index)",
            "state.shown_through = max(state.shown_through, state.line_index)",
        )
    )
    with pytest.raises(AssertionError):
        await _spoiler_snapshot_scenario(stale)


async def test_a_refused_click_writes_nothing_w8_is_conditional():
    entry = _cycle_entry(terminal=True, more_ticks=False, last_edit_monotonic=0.0)
    before = entry.progress.component.selected_period
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 3).callback(click)
    assert entry.progress.component.selected_period == before
    assert entry.progress.component.last_edit_monotonic == 0.0


async def test_a_click_that_gives_up_waiting_for_the_lock_defers_and_writes_nothing():
    """T-5.d step 6: bounded at `_CLICK_LOCK_WAIT_SECONDS`. Real time, deliberately --
    the constant is not injected."""
    entry = _cycle_entry(more_ticks=False)
    state = entry.progress.component
    before = state.selected_period
    async with state.edit_lock:  # held by "someone else" (a concurrent click, or the
        # interrupted edit) for longer than the click is willing to wait
        click = PagerInteraction(bot_with(entry))
        await WatchCycleButton(LEAGUE, GAME, 2).callback(click)
    assert click.order == ["defer"]
    assert state.selected_period == before


async def test_terminal_ordering_a_click_holding_the_lock_finishes_before_the_interrupted_edit():
    """T-8 #27(i): a click that already holds `edit_lock` completes its edit before
    `_edit_interrupted`'s is dispatched, and the interrupted edit lands last. Drives the
    race through the REAL `_edit_interrupted` (critic round 1, MAJOR 7) rather than a
    hand-rolled inline lock acquire, which would pass unchanged if `_edit_interrupted`
    stopped taking the lock at all."""
    entry, plan = _entry_and_plan(300, more_ticks=True, last_edit_monotonic=0.0)
    state = entry.progress.component
    started_click = asyncio.Event()
    finish_click = asyncio.Event()
    click_calls: list[dict] = []

    async def slow_edit_message(**kwargs):
        started_click.set()
        await finish_click.wait()
        click_calls.append(kwargs)

    click = PagerInteraction(bot_with(entry))
    click.response.edit_message = slow_edit_message  # type: ignore[method-assign]

    click_task = asyncio.create_task(WatchCycleButton(LEAGUE, GAME, 1).callback(click))
    await started_click.wait()  # the click holds edit_lock now, mid-edit

    message = FakeMessage(message_id=state.message_id)
    job = _interrupted_job(plan, message=message)
    scheduler = broadcast.BroadcastScheduler(
        broadcast.DEFAULT_BROADCAST_SETTINGS, on_ended=_noop_on_ended
    )
    interrupted_task = asyncio.create_task(scheduler._edit_interrupted(job, entry.progress))
    await asyncio.sleep(0.05)
    assert message.calls == [], "the interrupted path painted while the click holds the lock"

    finish_click.set()
    await click_task
    await interrupted_task

    assert click_calls, "the click's edit_message was eventually called"
    assert len(message.calls) == 1, "the interrupted edit must land exactly once, after the click"
    assert "Broadcast interrupted" in message.calls[0][0]["embed"].description


async def test_step7_recheck_refuses_a_click_whose_wait_for_the_lock_outlasted_w6():
    """BLOCKING 2(i) (critic round 1): the existing terminal-click tests all set
    `state.terminal = True` BEFORE the click starts, so they only ever trip the step-3 gate
    and never reach step 7 while a click is mid-flight holding or waiting on the lock. Here
    the click reaches step 6 and queues on `edit_lock` while `terminal` is STILL `False` --
    exactly the ordering the step-3 gate cannot catch -- and only then does a REAL
    `_edit_interrupted` (W6) run, setting `terminal` and queuing behind it. When the lock
    frees, FIFO wakes the click first; it must refuse under step 7's re-check rather than
    repaint a live-looking panel over the interrupted edit that follows it."""
    entry, plan = _entry_and_plan(23, line_index=23, more_ticks=True, last_edit_monotonic=0.0)
    state = entry.progress.component

    await state.edit_lock.acquire()  # held by the TEST, so both waiters below queue on it

    click = PagerInteraction(bot_with(entry))
    click_task = asyncio.create_task(WatchCycleButton(LEAGUE, GAME, 1).callback(click))
    for _ in range(10):
        await asyncio.sleep(0)  # the click passes step 3 (terminal is still False here) and
        # queues on `edit_lock` at step 6 -- it has not seen `terminal` yet
    assert state.terminal is False, "the click must reach the lock before terminal is set"

    message = FakeMessage(message_id=state.message_id)
    job = _interrupted_job(plan, message=message)
    scheduler = broadcast.BroadcastScheduler(
        broadcast.DEFAULT_BROADCAST_SETTINGS, on_ended=_noop_on_ended
    )
    interrupted_task = asyncio.create_task(scheduler._edit_interrupted(job, entry.progress))
    for _ in range(10):
        await asyncio.sleep(0)  # W6 sets `terminal` synchronously, then queues behind the click
    assert state.terminal is True
    assert state.edit_lock.locked()

    state.edit_lock.release()  # the test's own hold; FIFO wakes the click's waiter first
    await click_task
    await interrupted_task

    assert click.order == ["send_message"], "the click painted instead of refusing under step 7"
    assert click.response.messages[0][0] == watch_pager._ENDED
    assert len(message.calls) == 1, "only the interrupted panel's own edit reached the message"
    assert "Broadcast interrupted" in message.calls[0][0]["embed"].description


async def test_edit_interrupted_gives_up_on_the_lock_after_its_bound_and_paints_anyway():
    """BLOCKING 2(iii) (critic round 1): `_edit_interrupted` must never be delayed
    indefinitely by a viewer holding `edit_lock` across their own click's edit -- it waits
    `_INTERRUPTED_LOCK_WAIT_SECONDS` (1.0s) and paints anyway on timeout. This path had no
    test at all before this one. Real time, deliberately: the constant is not injected."""
    entry, plan = _entry_and_plan(23, line_index=23)
    state = entry.progress.component
    message = FakeMessage(message_id=state.message_id)
    job = _interrupted_job(plan, message=message)
    scheduler = broadcast.BroadcastScheduler(
        broadcast.DEFAULT_BROADCAST_SETTINGS, on_ended=_noop_on_ended
    )

    async with state.edit_lock:  # held by "someone else" (a click's own in-flight edit)
        started = time.monotonic()
        await scheduler._edit_interrupted(job, entry.progress)
        elapsed = time.monotonic() - started

    assert elapsed >= broadcast._INTERRUPTED_LOCK_WAIT_SECONDS
    assert state.terminal is True
    assert len(message.calls) == 1, "the interrupted panel must still be painted on timeout"
    assert "Broadcast interrupted" in message.calls[0][0]["embed"].description


async def test_one_object_seam_a_default_constructed_pace_progress_leaves_every_click_refused(repo):
    """T-8 #29: a broadcast run through `_run_broadcast_guarded` with no explicit
    `PaceProgress` leaves the pager entry's `frame_index` at -1 forever (the doc's own
    pin), so every click is refused instead of silently rendering nothing real."""
    h = Harness(broadcast, repo)
    job = h.job(1)
    scheduler = h.scheduler
    assert scheduler.admit(league_id=1, game_id=1, user_id=10) == broadcast.Admission.ADMITTED
    task = asyncio.ensure_future(scheduler._run_broadcast_guarded(job))  # no progress passed
    # what a pager built against the entry's own `live.progress` never got
    orphan_progress = broadcast.PaceProgress()
    component, visible = component_closures(job.plan)
    entry = PagerEntry(
        guild_id=GUILD, source=full_source()[0], progress=orphan_progress,
        component=component, visible=visible,
    )
    click = PagerInteraction(bot_with(entry))
    await WatchCycleButton(LEAGUE, GAME, 1).callback(click)
    # The orphan `PaceProgress` never receives ANY write (not even W0's message_id), so
    # the click is refused -- exactly which ephemeral sentence depends on which gate it
    # trips first (a message-id mismatch reads before frame_index here, since W0 never
    # ran on this object either), but it must never edit the shared message.
    assert click.order == ["send_message"]
    assert click.response.edits == []
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# --- `_bounded_edit`/`_bounded_send`'s opposite MISSING/None conventions (T-8 #18) --------------


async def test_bounded_edit_and_bounded_send_have_opposite_missing_none_conventions():
    message = FakeMessage()
    from dba2.presentation.panel import Panel

    panel = Panel(title="x")
    await broadcast._bounded_edit(message, panel, 1.0)  # default: MISSING omits
    assert "view" not in message.calls[0][0]
    sentinel = object()
    await broadcast._bounded_edit(message, panel, 1.0, view=sentinel)
    assert message.calls[1][0]["view"] is sentinel
    await broadcast._bounded_edit(message, panel, 1.0, view=None)  # explicit None: strip
    assert message.calls[2][0]["view"] is None

    thread = FakeThread()
    await broadcast._bounded_send(thread, panel, 1.0)  # default: None omits
    assert "view" not in thread.sent[0]
    await broadcast._bounded_send(thread, panel, 1.0, sentinel)
    assert thread.sent[1]["view"] is sentinel


# --- the spoiler cap against a REAL paced broadcast (T-8 #12) -----------------------------------


@pytest.fixture
def repo(monkeypatch):
    fake = Repo()
    monkeypatch.setattr(broadcast_repo, "transition_status", fake.transition_status)
    return fake


async def _start(module, h: Harness, *, script=None, fail_if=None, game_id: int = 1, plan=None):
    """What `/watch` does: render the narration once, spawn with it, hand the pager the
    game's TERSE lines, the broadcast's own `PaceProgress` and the closures (watch-v4
    T-5.d, docs/design/watch-narration-v2.md WN-8)."""
    job = h.job(game_id, script=script, fail_if=fail_if, plan=plan)
    lines = period_lines_for(job.plan)
    source = watch_pager.PagerSource.from_periods("AWY @ HOM", lines)
    component, visible = component_closures(job.plan)
    calls: list = []
    job = job.__class__(
        **{
            **job.__dict__,
            "narration": narration_for(job.plan),
            "component_view_for": fake_component_view_for(calls),
        }
    )
    assert h.scheduler.admit(league_id=1, game_id=game_id, user_id=10) == module.Admission.ADMITTED
    task = await h.scheduler.spawn(job=job)
    live = h.scheduler.live(1, game_id)
    assert live is not None and live.progress is not None
    entry = PagerEntry(
        guild_id=GUILD, source=source, progress=live.progress, component=component, visible=visible
    )
    return task, entry, h.threads[game_id]


def _pager_within_the_cap(entry: PagerEntry) -> None:
    cap = live_line_cap(entry.progress)
    for period, pp in entry.source.periods.items():
        available = entry.available_lines(period) or ()
        assert len(available) <= len(pp.lines)
        if pp.first_index + len(available) - 1 > cap and available:
            raise AssertionError(f"period {period} shows past the cap ({cap})")


@pytest.mark.parametrize("module", [broadcast])
async def test_at_every_tick_the_pager_never_shows_a_play_the_component_has_not(module, repo):
    h = Harness(module, repo)
    problems: list[str] = []
    seen: list[PagerEntry] = []

    async def check(n, kwargs):
        if not seen:
            return
        try:
            _pager_within_the_cap(seen[0])
        except AssertionError as failure:
            problems.append(f"tick {n}: {failure!r}")

    task, entry, _thread = await _start(module, h, script=check)
    seen.append(entry)
    await task
    assert problems == [], problems[:3]
    _pager_within_the_cap(entry)


def _shown_through_within_one_frame(entry: PagerEntry, per_frame: int) -> None:
    state = entry.progress.component
    if state.line_index < 0:
        return
    assert state.shown_through <= state.line_index, "the cap ran ahead of what was rendered"
    gap = state.line_index - state.shown_through
    assert gap <= per_frame, f"cap lagged the render by {gap}, more than one frame ({per_frame})"


@pytest.mark.parametrize("module", [broadcast])
async def test_at_every_tick_the_cap_lags_the_render_by_at_most_one_frame_at_ppf_2(module, repo):
    """T-8 #12 re-run against a `ppf = 2` plan (watch-v5 WT-5): `shown_through` never
    exceeds the last line `line_index` names as rendered, and the gap between them --
    exactly `possessions_per_frame` mid-tick, since W1 writes `line_index` before the
    edit and W2 writes `shown_through` only after it lands -- is at most one frame's
    worth of possessions, down from 6 at the old default."""
    h = Harness(module, repo)
    problems: list[str] = []
    seen: list[PagerEntry] = []

    async def check(n, kwargs):
        if not seen:
            return
        try:
            _shown_through_within_one_frame(seen[0], per_frame=2)
        except AssertionError as failure:
            problems.append(f"tick {n}: {failure!r}")

    task, entry, _thread = await _start(module, h, script=check, plan=plan_for(2))
    seen.append(entry)
    await task
    assert problems == [], problems[:3]
    _shown_through_within_one_frame(entry, per_frame=2)


async def test_the_pagers_progress_is_the_object_the_pacer_mutates(repo):
    h = Harness(broadcast, repo)
    task, entry, _thread = await _start(broadcast, h)
    assert entry.progress is h.scheduler.live(1, 1).progress
    await task
    assert entry.progress.component.shown_through >= 0
    assert h.scheduler.live(1, 1) is None  # forgotten...
    assert entry.available_lines(1)  # ...and the entry still knows what was posted


async def test_removing_the_live_cap_is_caught_by_the_spoiler_test():
    uncapped = pager_variant(
        (
            "return entry.lines[: max(0, min(len(entry.lines), cap - entry.first_index + 1))]",
            "return entry.lines",
        )
    )
    entry = entry_showing(10)
    real_lines = entry.available_lines(1)
    assert len(real_lines) == 11
    capless = uncapped.PagerEntry(
        guild_id=entry.guild_id, source=entry.source, progress=entry.progress,
        component=entry.component, visible=entry.visible,
    )
    assert len(capless.available_lines(1)) == len(entry.source.periods[1].lines)
    assert len(capless.available_lines(1)) > len(real_lines)


# -- abnormal ends: the scheduler has forgotten the game, the thread never got a final score --

_MID_GAME = 7


async def _cancelled_mid_period(module, repo) -> None:
    gate = asyncio.Event()

    async def hold(n, kwargs):
        if n == _MID_GAME:
            await gate.wait()

    h = Harness(module, repo)
    task, entry, thread = await _start(module, h, script=hold)
    for _ in range(200):
        await asyncio.sleep(0)
        if len(h.messages[1].calls) > _MID_GAME:
            break
    cap_before = live_line_cap(entry.progress)
    total_possessions = len(entry.source.periods[0].lines)  # Full Game IS every line
    assert 0 <= cap_before < total_possessions
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert h.scheduler.live(1, 1) is None, "the scheduler forgot the game"
    assert entry.progress.component.terminal
    _pager_within_the_cap(entry)


async def test_a_cancelled_broadcast_stays_capped_at_what_it_showed(repo):
    await _cancelled_mid_period(broadcast, repo)


async def _edit_failures_scenario(module, repo) -> None:
    h = Harness(module, repo)
    task, entry, _thread = await _start(module, h, fail_if=lambda request: request.number >= 30)
    await task
    assert repo.transitions(1)[-1] == (LIVE, INTERRUPTED)
    assert h.scheduler.live(1, 1) is None
    assert entry.progress.component.terminal
    _pager_within_the_cap(entry)


async def test_a_run_of_failed_edits_leaves_the_pager_capped(repo):
    await _edit_failures_scenario(broadcast, repo)


# --- registration ---------------------------------------------------------------------------


async def test_setup_hook_registers_all_three_classes_and_the_bot_holds_a_pager_registry():
    from dba2.bot.client import build_bot

    bot = build_bot()
    await bot.setup_hook()
    registered = bot._connection._view_store._dynamic_items
    assert registered[WatchCycleButton.__discord_ui_compiled_template__] is WatchCycleButton
    assert registered[WatchFullTextButton.__discord_ui_compiled_template__] is WatchFullTextButton
    assert registered[WatchPageButton.__discord_ui_compiled_template__] is WatchPageButton
    assert isinstance(bot.pagers, PagerRegistry) and len(bot.pagers) == 0


async def test_the_real_dispatcher_routes_each_id_to_exactly_one_class(monkeypatch):
    from dba2.bot.client import build_bot

    bot = build_bot()
    await bot.setup_hook()
    store = bot._connection._view_store
    seen: list[tuple[type, str]] = []

    async def spy(component_type, factory, interaction, custom_id, match):
        seen.append((factory, custom_id))

    monkeypatch.setattr(store, "schedule_dynamic_item_call", spy)
    cycle_id = cycle_custom_id(1, GAME, 2)
    full_id = full_custom_id(1, GAME)
    page_id = page_custom_id(1, GAME, 2, 3, "older")
    for custom_id in (cycle_id, full_id, page_id, cycle_id + "x", "dba2:offer:accept:9"):
        store.dispatch_dynamic_items(2, custom_id, SimpleNamespace())
    await asyncio.sleep(0)
    offer_id = "dba2:offer:accept:9"
    assert sorted(seen, key=lambda item: item[1]) == sorted(
        [
            (WatchCycleButton, cycle_id),
            (WatchFullTextButton, full_id),
            (WatchPageButton, page_id),
            (OfferActionButton, offer_id),
        ],
        key=lambda item: item[1],
    )
