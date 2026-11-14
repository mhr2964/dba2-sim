"""`game_feed.box_score_is_public` -- the one predicate `/box` asks (P5-99.n), over every
row state a `game_broadcasts` row can be in. The cog-level proof (a real command against
real rows) is `tests/integration/test_box_reveal_gate.py`."""

from __future__ import annotations

import pytest

from dba2.bot import game_feed
from dba2.domain.broadcast import BroadcastStatus
from tests.unit.bot._game_feed_fakes import Store


def _row(**kwargs):
    return Store().add_row(1, **kwargs)


def test_a_game_in_no_block_is_public():
    assert game_feed.box_score_is_public(None) is True


@pytest.mark.parametrize("status", list(BroadcastStatus))
def test_an_unrevealed_row_is_hidden_whatever_its_status(status):
    assert game_feed.box_score_is_public(_row(status=status)) is False


def test_a_claimed_but_unposted_reveal_is_still_hidden():
    assert game_feed.box_score_is_public(_row(claimed=True)) is False


def test_a_posted_reveal_is_public():
    assert game_feed.box_score_is_public(_row(posted=True)) is True
