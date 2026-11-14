"""`dba2/media/detect/trades.py` -- MV-2a: `TRADE_EXECUTED`/`TRADE_BROKEN_PROMISE`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, SubjectKind
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.transaction import (
    LeagueTransaction,
    LeagueTransactionAsset,
    TransactionAssetKind,
    TransactionType,
)
from dba2.media.detect.trades import trade_broken_promise_package, trade_executed_package

_SEASON = 2026
_PHASE = Phase.TRADE_DEADLINE
_CYCLE_INDEX = 2
_TEAM_A = 1
_TEAM_B = 2


def _transaction(*, source_offer_id: int | None = None, actor_user_id: int | None = None):
    return LeagueTransaction(
        id=1, league_id=1, season=_SEASON, phase=_PHASE, txn_type=TransactionType.TRADE,
        actor_user_id=actor_user_id, source_offer_id=source_offer_id, note=None, details={},
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _asset(
    *, asset_kind: TransactionAssetKind, from_team_id: int, to_team_id: int,
    player_id: int | None = None, draft_pick_id: int | None = None,
) -> LeagueTransactionAsset:
    return LeagueTransactionAsset(
        id=1, transaction_id=1, asset_kind=asset_kind, player_id=player_id,
        draft_pick_id=draft_pick_id, from_team_id=from_team_id, to_team_id=to_team_id,
    )


def _kwargs(**overrides):
    base = dict(season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# trade_executed_package
# ---------------------------------------------------------------------------


def test_players_both_ways():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=1),
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=2),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.story_kind is StoryKind.TRADE_EXECUTED
    assert package.subject.kind is SubjectKind.MATCHUP
    assert package.subject.team_id == _TEAM_A
    assert package.subject.opponent_team_id == _TEAM_B
    assert package.number("players_acquired_count") == 1  # A received player 2
    assert package.number("players_sent_count") == 1  # A sent player 1
    assert package.number("picks_acquired_count") == 0
    assert package.number("picks_sent_count") == 0
    assert package.number("asset_count_edge") == 0  # a numerically even 1-for-1 swap


def test_asset_count_edge_is_positive_when_team_id_side_counts_more_assets_in():
    """`team_id` (the lower id, `_TEAM_A`) receives 2 players and sends none --
    `asset_count_edge` is `+2`, the real signal `hot_take_trade_executed_fleeced`
    now gates on (critic round on commits `ea84c5f`..`665526d`, bug B3)."""
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=1),
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=2),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("asset_count_edge") == 2


def test_asset_count_edge_is_negative_when_team_id_side_counts_more_assets_out():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PICK, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               draft_pick_id=10),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("asset_count_edge") == -1


def test_picks_only():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PICK, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               draft_pick_id=10),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("picks_sent_count") == 1
    assert package.number("players_acquired_count") == 0
    assert package.number("players_sent_count") == 0


def test_one_sided():
    """Team A gets everything -- team B sends two players and gets nothing back."""
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=1),
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=2),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("players_acquired_count") == 2
    assert package.number("players_sent_count") == 0


def test_a_purged_players_asset_row_still_counts_and_does_not_raise():
    """The documented post-purge state: `player_id IS NULL` on a real `PLAYER` asset
    row. `asset_kind` alone is enough to count it -- must not raise, must still count."""
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=None),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("players_sent_count") == 1


def test_a_two_team_four_asset_trade_produces_exactly_one_package():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=1),
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               player_id=2),
        _asset(asset_kind=TransactionAssetKind.PICK, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               draft_pick_id=10),
        _asset(asset_kind=TransactionAssetKind.PICK, from_team_id=_TEAM_B, to_team_id=_TEAM_A,
               draft_pick_id=11),
    )
    package = trade_executed_package(_transaction(), assets, **_kwargs())
    assert package is not None
    assert package.number("players_acquired_count") == 1
    assert package.number("players_sent_count") == 1
    assert package.number("picks_acquired_count") == 1
    assert package.number("picks_sent_count") == 1


def test_more_than_two_distinct_teams_returns_none():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=1),
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=3, to_team_id=_TEAM_A,
               player_id=2),
    )
    assert trade_executed_package(_transaction(), assets, **_kwargs()) is None


def test_human_negotiated_and_ai_executed_flags():
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=1),
    )
    negotiated = trade_executed_package(
        _transaction(source_offer_id=5, actor_user_id=1), assets, **_kwargs()
    )
    assert negotiated is not None
    assert negotiated.number("human_negotiated") == 1
    assert negotiated.number("ai_executed") == 0

    ai_done = trade_executed_package(_transaction(), assets, **_kwargs())
    assert ai_done is not None
    assert ai_done.number("human_negotiated") == 0
    assert ai_done.number("ai_executed") == 1


def test_boolean_facts_render_as_yes_no_text_not_literal_digits():
    """Critic round on commits `ea84c5f`..`665526d`, bug B2: `value_text` used
    to be the literal `"1"`/`"0"`, which `_build_fact_box` published verbatim
    with no human-readable meaning at all."""
    assets = (
        _asset(asset_kind=TransactionAssetKind.PLAYER, from_team_id=_TEAM_A, to_team_id=_TEAM_B,
               player_id=1),
    )
    negotiated = trade_executed_package(
        _transaction(source_offer_id=5, actor_user_id=1), assets, **_kwargs()
    )
    assert negotiated is not None
    assert negotiated.text("human_negotiated") == "Yes"
    assert negotiated.text("ai_executed") == "No"

    ai_done = trade_executed_package(_transaction(), assets, **_kwargs())
    assert ai_done is not None
    assert ai_done.text("human_negotiated") == "No"
    assert ai_done.text("ai_executed") == "Yes"


# ---------------------------------------------------------------------------
# trade_broken_promise_package
# ---------------------------------------------------------------------------


def _promise_kwargs(**overrides):
    base = dict(
        player_id=1, old_team_id=_TEAM_A, new_team_id=_TEAM_B, trade_season=_SEASON,
        satisfaction_loyalty=50, weight_loyalty=30,
    )
    base.update(overrides)
    return base


def test_content_with_high_loyalty_fires_promise_broken():
    package = trade_broken_promise_package(
        state_at_trade=RequestState.CONTENT, **_promise_kwargs(), **_kwargs()
    )
    assert package is not None
    assert package.angle is Angle.PROMISE_BROKEN
    assert package.number("got_his_wish") == 0
    assert package.text("got_his_wish") == "No"  # bug B2: not the literal "0"
    assert package.subject.kind is SubjectKind.PLAYER
    assert package.subject.team_id == _TEAM_B
    assert package.subject.opponent_team_id == _TEAM_A


def test_content_below_the_loyalty_bars_does_not_fire():
    package = trade_broken_promise_package(
        state_at_trade=RequestState.CONTENT,
        **_promise_kwargs(satisfaction_loyalty=39, weight_loyalty=30),
        **_kwargs(),
    )
    assert package is None
    package = trade_broken_promise_package(
        state_at_trade=RequestState.CONTENT,
        **_promise_kwargs(satisfaction_loyalty=50, weight_loyalty=24),
        **_kwargs(),
    )
    assert package is None


def test_trade_requested_fires_with_the_other_angle_and_no_loyalty_gate():
    package = trade_broken_promise_package(
        state_at_trade=RequestState.TRADE_REQUESTED,
        **_promise_kwargs(satisfaction_loyalty=0, weight_loyalty=0),
        **_kwargs(),
    )
    assert package is not None
    assert package.angle is Angle.TRADE_MADE
    assert package.number("got_his_wish") == 1


@pytest.mark.parametrize(
    "state", [None, RequestState.UNSETTLED, RequestState.EXTENSION_DEMANDED, RequestState.HOLDOUT]
)
def test_every_other_state_does_not_fire(state):
    """`None` (no reading -- a pre-migration row, or the reconstruction landed on
    one) never fires and specifically never reads as CONTENT; the other real
    states are not one of MV-2a's two named ironies."""
    package = trade_broken_promise_package(state_at_trade=state, **_promise_kwargs(), **_kwargs())
    assert package is None
