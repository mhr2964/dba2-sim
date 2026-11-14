"""`services/free_agency_service.py::rank_offers` -- Decision P4-16's
motivation-weighted replacement for the Decision P2-21 placeholder
(`docs/design/phase-4-media-and-player-agency.md` §11 Stage C step 11).

Direct unit tests against hand-built fixtures, no DB -- `rank_offers`
itself is a pure function (`motivation`/`matrix_by_team` are already-
loaded parameters, never a `motivation_repo` read); the DB-backed read
model (`agency_service.load_motivation_for_league`) and the real-DB
`construct_offer_for_team`/`rank_offers` composition already have their
own coverage in `tests/integration/test_free_agency_service.py`.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

from dba2.domain.motivation import MarketPreference, PlayerMotivation, RequestState
from dba2.domain.player import Player, Position, RosterStatus
from dba2.managers.decisions.signing import SigningOffer
from dba2.managers.needs import NeedMatrix, NeedTier
from dba2.managers.tuning import CompetitiveWindow
from dba2.services.free_agency_service import rank_offers


def _player(*, player_id: int = 1, overall_cached: int = 75) -> Player:
    return Player(
        id=player_id,
        league_id=1,
        first_name="Free",
        last_name="Agent",
        external_ref=None,
        primary_position=Position.SG,
        secondary_position=None,
        height_in=78,
        weight_lb=210,
        wingspan_in=80,
        birth_date=date(1996, 1, 1),
        draft_year=2018,
        draft_pick_number=15,
        years_pro=7,
        team_id=None,
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=overall_cached,
        overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC),
        retired_in_season=None,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _motivation(
    *,
    weight_winning: int = 20,
    weight_role_usage: int = 20,
    weight_loyalty: int = 20,
    weight_money: int = 20,
    weight_legacy: int = 20,
    hometown_team_id: int | None = None,
) -> PlayerMotivation:
    return PlayerMotivation(
        player_id=1,
        league_id=1,
        weight_winning=weight_winning,
        weight_role_usage=weight_role_usage,
        weight_loyalty=weight_loyalty,
        weight_money=weight_money,
        weight_legacy=weight_legacy,
        hometown_team_id=hometown_team_id,
        market_preference=MarketPreference.NEUTRAL,
        star_leverage=50,
        satisfaction_winning=0,
        satisfaction_role_usage=0,
        satisfaction_loyalty=0,
        satisfaction_money=0,
        satisfaction_legacy=0,
        morale=0,
        trust_in_management=0,
        request_state=RequestState.CONTENT,
        request_opened_in_season=None,
        last_evaluated_season=2026,
    )


def _uniform_need_matrix(value: float) -> NeedMatrix:
    """A `NeedMatrix` where `role_fit` (`need - glut`) reads exactly
    `value` at every `(position, tier)` cell -- lets a test control the
    role/usage term directly, independent of the fixture player's own
    position/overall."""
    zero = MappingProxyType({p: MappingProxyType({t: 0.0 for t in NeedTier}) for p in Position})
    need = MappingProxyType(
        {p: MappingProxyType({t: value for t in NeedTier}) for p in Position}
    )
    return NeedMatrix(supply=zero, need=need, glut=zero)


def _offer(team_id: int, *, annual_salary: int, window: CompetitiveWindow) -> SigningOffer:
    return SigningOffer(
        team_id=team_id, player_id=1, years=1, annual_salary=annual_salary, window=window
    )


def test_rank_offers_falls_back_to_money_and_window_when_motivation_is_none():
    """Decision P4-16: `motivation is None` (every pre-Phase-4 call site,
    and every call site that never populates `GmContext.motivation_by_
    player`) must reproduce Decision P2-21's original placeholder byte-
    for-byte -- highest `guaranteed_total` wins, ties broken by window."""
    low = _offer(1, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    high = _offer(2, annual_salary=2_000_000, window=CompetitiveWindow.REBUILD)
    ranked = rank_offers((low, high), _player())
    assert ranked == (high, low)


def test_rank_offers_falls_back_when_matrix_by_team_is_none():
    """Same fallback, the other missing half of the pair -- `motivation`
    present but `matrix_by_team` absent must not attempt the weighted
    path (it needs both)."""
    low = _offer(1, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    high = _offer(2, annual_salary=2_000_000, window=CompetitiveWindow.REBUILD)
    ranked = rank_offers((low, high), _player(), motivation=_motivation())
    assert ranked == (high, low)


def test_rank_offers_weighted_by_role_usage_can_flip_the_money_winner():
    """A player who cares overwhelmingly about role/usage fit (weight_
    role_usage dominant, every other weight near-zero) picks the lower-
    money offer from the team that fits him, over a much richer offer
    from a team that does not -- proof the mechanism actually moves the
    ranking, not just that it computes without crashing."""
    player = _player()
    motivation = _motivation(
        weight_winning=1, weight_role_usage=97, weight_money=1, weight_legacy=1
    )
    rich_bad_fit = _offer(1, annual_salary=10_000_000, window=CompetitiveWindow.REBUILD)
    poor_great_fit = _offer(2, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    matrix_by_team = {
        1: _uniform_need_matrix(-1.0),  # team 1: this player is a poor fit everywhere
        2: _uniform_need_matrix(1.0),  # team 2: this player is a great fit everywhere
    }

    money_only = rank_offers((rich_bad_fit, poor_great_fit), player)
    assert money_only[0] is rich_bad_fit  # sanity: money alone picks the rich offer

    weighted = rank_offers(
        (rich_bad_fit, poor_great_fit), player, motivation=motivation, matrix_by_team=matrix_by_team
    )
    assert weighted[0] is poor_great_fit  # role/usage weighting flips the winner


def test_rank_offers_weighted_by_winning_prefers_the_contending_teams_window():
    """A player who cares overwhelmingly about winning picks the lower-
    money offer from the CONTEND team over a richer REBUILD offer."""
    player = _player()
    motivation = _motivation(
        weight_winning=97, weight_role_usage=1, weight_money=1, weight_legacy=1
    )
    rich_rebuild = _offer(1, annual_salary=10_000_000, window=CompetitiveWindow.REBUILD)
    poor_contend = _offer(2, annual_salary=1_000_000, window=CompetitiveWindow.CONTEND)
    matrix_by_team = {1: _uniform_need_matrix(0.0), 2: _uniform_need_matrix(0.0)}

    weighted = rank_offers(
        (rich_rebuild, poor_contend), player, motivation=motivation, matrix_by_team=matrix_by_team
    )
    assert weighted[0] is poor_contend


def test_rank_offers_legacy_term_is_zero_when_hometown_is_none():
    """Decision P4-12's disclosed vacuity (P4-80 item 8), reproduced
    directly: `hometown_team_id is None` for every real corpus today, so
    the legacy term contributes `0.0` to every offer identically -- a
    player weighted entirely on legacy must fall back to the money/window
    tie-break among otherwise-identical-legacy offers, not pick a
    "hometown" team that was never actually recorded as one."""
    player = _player()
    motivation = _motivation(
        weight_winning=1, weight_role_usage=1, weight_money=1, weight_legacy=97,
        hometown_team_id=None,
    )
    low = _offer(1, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    high = _offer(2, annual_salary=2_000_000, window=CompetitiveWindow.REBUILD)
    matrix_by_team = {1: _uniform_need_matrix(0.0), 2: _uniform_need_matrix(0.0)}

    weighted = rank_offers(
        (low, high), player, motivation=motivation, matrix_by_team=matrix_by_team
    )
    assert weighted[0] is high  # money/window tie-break, since legacy is 0.0 for both


def test_rank_offers_legacy_term_prefers_the_hometown_team_when_recorded():
    """The mechanism itself DOES work when `hometown_team_id` is set --
    proof the P4-12 vacuity above is a data-availability gap, not a bug
    in this function."""
    player = _player()
    motivation = _motivation(
        weight_winning=1, weight_role_usage=1, weight_money=1, weight_legacy=97,
        hometown_team_id=2,
    )
    rich_away = _offer(1, annual_salary=10_000_000, window=CompetitiveWindow.REBUILD)
    poor_home = _offer(2, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    matrix_by_team = {1: _uniform_need_matrix(0.0), 2: _uniform_need_matrix(0.0)}

    weighted = rank_offers(
        (rich_away, poor_home), player, motivation=motivation, matrix_by_team=matrix_by_team
    )
    assert weighted[0] is poor_home


def test_rank_offers_falls_back_when_all_relevant_weights_are_zero():
    """A degenerate `PlayerMotivation` with every relevant weight at `0`
    (loyalty is never read here, so it is irrelevant) must not raise
    `ZeroDivisionError` -- falls back to the money/window placeholder,
    same as the `None` cases above."""
    player = _player()
    motivation = _motivation(
        weight_winning=0, weight_role_usage=0, weight_money=0, weight_legacy=0,
        weight_loyalty=97,
    )
    low = _offer(1, annual_salary=1_000_000, window=CompetitiveWindow.REBUILD)
    high = _offer(2, annual_salary=2_000_000, window=CompetitiveWindow.REBUILD)
    matrix_by_team = {1: _uniform_need_matrix(0.0), 2: _uniform_need_matrix(0.0)}

    weighted = rank_offers(
        (low, high), player, motivation=motivation, matrix_by_team=matrix_by_team
    )
    assert weighted == (high, low)
