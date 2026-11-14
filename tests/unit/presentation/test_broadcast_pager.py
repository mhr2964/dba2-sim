"""The pager page renderer (docs/design/watch-v3-spacing-and-pager.md W-12, W-15 tests 2-3):
page arithmetic, the clamp, what a page carries and what it must not (a score in the
title), and that every page of every period of the corpus is a legal `Panel` well inside
the chunk budget."""

from __future__ import annotations

import re

import pytest

from dba2.presentation.broadcast import (
    _LINE_SEPARATOR,
    PAGER_PAGE_LINES,
    _utf16_units,
    clamp_page,
    page_count,
    pager_matchup,
    plan_broadcast,
    plan_period_lines,
    render_pager_page,
)
from tests.unit.presentation._broadcast_helpers import make_team, names_for
from tests.unit.presentation.test_live_box import _SEEDS, _game

_HOME, _AWAY = make_team(1, "HOM"), make_team(2, "AWY")


def _lines(count: int) -> list[str]:
    return [f"{index}:00  🏀 line {index}" for index in range(count)]


def test_a_page_holds_eight_plays():
    assert PAGER_PAGE_LINES == 8


@pytest.mark.parametrize(
    ("lines", "pages", "last_page_holds"),
    [(55, 7, 7), (20, 3, 4), (8, 1, 8), (9, 2, 1), (1, 1, 1), (16, 2, 8)],
)
def test_page_arithmetic_puts_the_remainder_on_the_last_page(lines, pages, last_page_holds):
    assert page_count(lines) == pages
    rows = _lines(lines)
    panel = render_pager_page(label="Q2", matchup="AWY @ HOM", lines=rows, page=pages)
    assert len(panel.description.split(_LINE_SEPARATOR)) == last_page_holds
    assert f"Page {pages} of {pages}" in panel.footer


def test_no_plays_is_no_pages_and_says_nothing_was_broadcast_yet():
    assert page_count(0) == 0
    panel = render_pager_page(label="Q3", matchup="AWY @ HOM", lines=[], page=5)
    assert panel.title == "Q3 · no plays yet"
    assert "Nothing" in panel.description and "Page" not in panel.footer


def test_a_negative_line_count_is_refused():
    with pytest.raises(ValueError):
        page_count(-1)


@pytest.mark.parametrize(("page", "expected"), [(-3, 1), (0, 1), (1, 1), (4, 4), (99, 7)])
def test_clamp_forces_a_stale_page_into_range(page, expected):
    assert clamp_page(page, 55) == expected


def test_clamp_with_nothing_shown_is_page_one():
    assert clamp_page(9, 0) == 1


def test_a_stale_page_renders_the_newest_page_and_says_so():
    panel = render_pager_page(label="Q2", matchup="AWY @ HOM", lines=_lines(20), page=50)
    assert panel.footer == "Page 3 of 3 · only you can see this · newest"
    assert panel.title == "Q2 · plays 17-20 of 20"


def test_an_older_page_is_not_marked_newest():
    panel = render_pager_page(label="Q2", matchup="AWY @ HOM", lines=_lines(20), page=2)
    assert panel.footer == "Page 2 of 3 · only you can see this"
    assert panel.title == "Q2 · plays 9-16 of 20"
    assert panel.description == _LINE_SEPARATOR.join(_lines(20)[8:16])


def test_the_matchup_is_the_author_and_a_hostile_code_is_capped():
    assert pager_matchup(_HOME, _AWAY) == "AWY @ HOM"
    hostile = make_team(3, "HOME\nCODE12345")
    text = pager_matchup(hostile, _AWAY)
    assert "\n" not in text


_TITLE = re.compile(r"^(?:Q\d|OT\d*) · (?:plays \d+-\d+ of \d+|no plays yet)$")


def test_a_title_never_carries_a_score_over_the_corpus():
    """A score in the title would state the frame the viewer has not reached. The title is
    the period, the play numbers and the count and nothing else; the running scores that
    are inside the lines belong to the plays shown."""
    for seed in _SEEDS[:10]:
        result, _ = _game(seed)
        plan = plan_broadcast(result, game_id=1, home_code="HOM", away_code="AWY")
        periods = plan_period_lines(plan, home=_HOME, away=_AWAY, names=names_for(result))
        for pl in periods:
            for page in range(1, page_count(len(pl.lines)) + 1):
                panel = render_pager_page(
                    label=pl.label, matchup="AWY @ HOM", lines=pl.lines, page=page
                )
                assert _TITLE.match(panel.title), panel.title
                assert not re.search(r"\d+\s*-\s*\d+ (?:HOM|AWY)|(?:HOM|AWY) \d+\s*-", panel.title)


def test_every_page_of_every_period_of_the_corpus_is_a_legal_panel_and_reads_in_order():
    worst = 0
    for seed in _SEEDS:
        result, _ = _game(seed)
        plan = plan_broadcast(result, game_id=1, home_code="HOM", away_code="AWY")
        periods = plan_period_lines(plan, home=_HOME, away=_AWAY, names=names_for(result))
        for pl in periods:
            rows = list(pl.lines)
            seen: list[str] = []
            for page in range(1, page_count(len(rows)) + 1):
                panel = render_pager_page(
                    label=pl.label, matchup="AWY @ HOM", lines=rows, page=page
                )  # `Panel.__post_init__` is the legal-panel assertion
                worst = max(worst, _utf16_units(panel.description))
                seen.extend(panel.description.split(_LINE_SEPARATOR))
            assert seen == rows, (seed, pl.period)  # every play exactly once, in order
    # the doc's bound: 8 lines at the cap, every character astral, plus 7 separators
    assert 0 < worst <= 1_742, worst


def _stale_page_scenario(module) -> None:
    panel = module.render_pager_page(label="Q2", matchup="AWY @ HOM", lines=_lines(20), page=50)
    assert panel.title == "Q2 · plays 17-20 of 20", panel.title
    assert panel.footer.startswith("Page 3 of 3"), panel.footer


def test_the_clamp_is_caught_when_removed_from_the_renderer():
    from tests.unit.presentation._broadcast_helpers import broadcast_variant

    _stale_page_scenario(broadcast_variant())  # the control
    unclamped = broadcast_variant(("    page = clamp_page(page, total)\n", "    page = page\n"))
    with pytest.raises(AssertionError):
        _stale_page_scenario(unclamped)
