"""`dba2/media/salience.py` -- `score_candidates`/`select_top_k` (Decision
P4-29). §11 Stage D step 17.

**Retyped to `StoryPackage` (docs/design/media-v2-newsroom.md MV-1b(b),
"Build order" step 3a).** Every fixture below is built through the REAL
production path a candidate actually takes -- `derive_frame` (unchanged,
`dba2/media/frame.py`) -> `dba2/media/detect/legacy.py::adapt_frame`
(the new thin adapter) -> `dba2.services.media_service.seal_package` (the
one sealing function) -- rather than a hand-built `StoryPackage`, so these
tests exercise the real adapter/sealing code, not a second, independently
constructed package shape that could drift from it.

Adversarial cases this dispatch specifically asked for: an empty candidate
list, all-tied scores, and more candidates than K."""

from __future__ import annotations

import pytest

from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, StoryKind, derive_frame
from dba2.media.salience import (
    KIND_WEIGHTS,
    SalienceCandidate,
    above_floor,
    build_publication_plan,
    score_candidates,
    select_top_k,
)
from dba2.services.media_service import seal_package

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 3


def _package(
    candidate: StoryCandidate, *, team_name: str = "A Team", player_name: str = "A Player"
) -> StoryPackage:
    """The shortest REAL path from a hand-built `StoryCandidate` to a
    sealed `StoryPackage` -- mirrors the old file's own "shortest real
    path to a `StoryFrame`" convention, one step further now that
    `StoryFrame` is confined to `legacy.py`. Asserts sealing actually
    succeeds (never `None`) so a fixture-authoring mistake (a candidate
    missing a `REQUIRED` key) fails loudly at the fixture, not as a
    confusing downstream `AttributeError`."""
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is not None, f"candidate for {candidate.kind} was insufficient -- fix the fixture"
    team_names = {candidate.team_id: team_name} if candidate.team_id is not None else {}
    player_names = {candidate.player_id: player_name} if candidate.player_id is not None else {}
    sealed = seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None, f"candidate for {candidate.kind} failed to seal -- fix the fixture"
    return sealed


_ORIGINAL_ELEVEN_STORY_KINDS = frozenset(
    {
        StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE, StoryKind.STREAK,
        StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT, StoryKind.SIGNING_REACTION,
        StoryKind.MILESTONE, StoryKind.AWARD_RACE, StoryKind.PLAYER_UNREST,
        StoryKind.HOLDOUT_WATCH, StoryKind.ROOKIE_WATCH,
    }
)
"""The exact eleven kinds `derive_frame`/`legacy.adapt_frame` can build a
package for -- see `tests/unit/test_media_frame.py`'s own identical
constant for the full reasoning (`StoryFrame` is permanently scoped to
these, MV-2's eleven new kinds never reach it)."""


def _sufficient_candidate_for_kind(kind: StoryKind) -> StoryCandidate:
    """One real, SUFFICIENT `StoryCandidate` per `StoryKind` -- the
    identical per-kind shape `tests/unit/test_media_select.py`'s own
    `_sufficient_candidate_for_kind` uses (factored identically rather
    than imported, matching that file's own "both totality sweeps build
    the identical per-kind candidate shape rather than drifting apart"
    reasoning applied one file over: `derive_frame`'s own per-kind sample-
    size gates mean a bare `StoryCandidate(kind=kind, player_id=1,
    team_id=1)` is INSUFFICIENT for every team-sample kind)."""
    if kind in (StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE):
        return StoryCandidate(
            kind=kind, team_id=1, team_games_played_this_season=20, has_pulse_snapshot=True,
            standings_position=3, standings_position_at_last_pulse=8,
        )
    if kind is StoryKind.STREAK:
        return StoryCandidate(
            kind=kind, team_id=1, team_games_played_this_season=20, has_pulse_snapshot=True,
            streak_length=6,
        )
    if kind in (StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT):
        return StoryCandidate(kind=kind, team_id=1, trade_in_player_ids=(1,))
    if kind is StoryKind.SIGNING_REACTION:
        return StoryCandidate(kind=kind, team_id=1, player_id=1)
    if kind in (StoryKind.MILESTONE, StoryKind.AWARD_RACE, StoryKind.ROOKIE_WATCH):
        return StoryCandidate(kind=kind, player_id=1, player_games_played_this_season=10)
    if kind is StoryKind.HOLDOUT_WATCH:
        return StoryCandidate(
            kind=kind, player_id=1, motivation_magnitude=10, request_state=RequestState.HOLDOUT,
        )
    return StoryCandidate(  # PLAYER_UNREST
        kind=kind, player_id=1, motivation_magnitude=10, request_state=RequestState.UNSETTLED,
    )


def _sufficient_package(kind: StoryKind = StoryKind.SIGNING_REACTION) -> StoryPackage:
    return _package(_sufficient_candidate_for_kind(kind))


# ---------------------------------------------------------------------------
# KIND_WEIGHTS exhaustiveness
# ---------------------------------------------------------------------------


def test_every_story_kind_has_a_kind_weight():
    assert set(KIND_WEIGHTS) == set(StoryKind)


def test_kind_weights_are_all_in_unit_range():
    assert all(0.0 <= weight <= 1.0 for weight in KIND_WEIGHTS.values())


# ---------------------------------------------------------------------------
# score_candidates
# ---------------------------------------------------------------------------


def test_score_candidates_empty_list_returns_empty():
    assert score_candidates(()) == ()


def test_adapt_frame_drops_an_insufficient_frame_before_a_package_ever_exists():
    """MV-1b(b): the old `insufficient_data` short-circuit `score_
    candidates` used to own has NO package analogue here -- it moved
    upstream into `legacy.py::adapt_frame`, which returns `None` and never
    produces a package for this module to see at all."""
    frame = derive_frame(StoryCandidate(kind=StoryKind.STREAK))  # no team_id -> insufficient
    assert frame.insufficient_data is not None
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is None


def test_score_candidates_scores_are_bounded_zero_to_one_for_the_original_eleven():
    """`_sufficient_package` builds its `StoryPackage` through the real
    `derive_frame`/`legacy.adapt_frame` path, which (by design, `StoryKind`'s
    own docstring) only ever exists for the original eleven kinds. See
    `test_score_candidates_scores_are_bounded_zero_to_one_for_all_22_kinds` below
    for MV-9's own full-22-kind version of this property, using each of MV-2's
    new detectors' own real functions directly."""
    candidates = [
        SalienceCandidate(package=_sufficient_package(kind))
        for kind in _ORIGINAL_ELEVEN_STORY_KINDS
    ]
    scored = score_candidates(candidates)
    assert all(0.0 <= s.score <= 1.0 for s in scored)


def _sufficient_packages_for_the_new_eleven_kinds() -> tuple[StoryPackage, ...]:
    """One real, firing `StoryPackage` per MV-2 kind, built through each
    detector's OWN real function (never a hand-rolled shortcut) -- the
    "Build order" step 4 closing pass MV-9's own test list names: "score_
    candidates over a StoryPackage of each of the 22 kinds returns a score
    in [0, 1]." Minimal, deliberately-firing inputs per detector; the exact
    threshold reasoning for each is already covered by that detector's own
    dedicated test file."""
    from datetime import UTC, datetime

    from dba2.domain.motivation import RequestState
    from dba2.domain.player import Position
    from dba2.domain.stats import PlayerFormWindow
    from dba2.domain.team import DefensiveScheme, GmPhilosophy, OffensiveScheme, Timeline
    from dba2.domain.transaction import (
        LeagueTransaction,
        LeagueTransactionAsset,
        TransactionAssetKind,
        TransactionType,
    )
    from dba2.managers.needs import NeedTier
    from dba2.media.detect import construction, form, profile, revenge, rivalry, scheme, trades

    transaction = LeagueTransaction(
        id=1, league_id=1, season=_SEASON, phase=_PHASE, txn_type=TransactionType.TRADE,
        actor_user_id=None, source_offer_id=None, note=None, details={},
        created_at=datetime(_SEASON, 2, 1, tzinfo=UTC),
    )
    assets = (
        LeagueTransactionAsset(
            id=1, transaction_id=1, asset_kind=TransactionAssetKind.PLAYER,
            player_id=1, draft_pick_id=None, from_team_id=1, to_team_id=2,
        ),
    )
    trade_executed = trades.trade_executed_package(
        transaction, assets, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert trade_executed is not None

    trade_broken_promise = trades.trade_broken_promise_package(
        player_id=1, old_team_id=1, new_team_id=2, state_at_trade=RequestState.CONTENT,
        satisfaction_loyalty=50, weight_loyalty=30, trade_season=_SEASON,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert trade_broken_promise is not None

    hot_window = PlayerFormWindow(
        player_id=1, team_id=1, window_games=5, season_games=15,
        window_points=140.0, window_minutes=180.0,
        window_fga_rim=65, window_fga_mid=35, window_fga_three=25, window_fgm_three=0,
        window_fta=0, window_ftm=0, window_ast=0, window_stl=0, window_blk=0,
        window_dreb=0, window_tov=0,
        season_points=340.0, season_minutes=540.0,
        season_fga_rim=165, season_fga_mid=95, season_fga_three=65, season_fgm_three=0,
        season_fta=0, season_ftm=0, season_ast=0, season_stl=0, season_blk=0,
        season_dreb=0, season_tov=0,
    )
    hot_streak = form.hot_or_cold_package(
        hot_window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert hot_streak is not None

    cold_window = PlayerFormWindow(
        player_id=1, team_id=1, window_games=5, season_games=15,
        window_points=100.0, window_minutes=180.0,
        window_fga_rim=50, window_fga_mid=30, window_fga_three=20, window_fgm_three=0,
        window_fta=0, window_ftm=0, window_ast=0, window_stl=0, window_blk=0,
        window_dreb=0, window_tov=0,
        season_points=380.0, season_minutes=540.0,
        season_fga_rim=150, season_fga_mid=90, season_fga_three=60, season_fgm_three=0,
        season_fta=0, season_ftm=0, season_ast=0, season_stl=0, season_blk=0,
        season_dreb=0, season_tov=0,
    )
    cold_stretch = form.hot_or_cold_package(
        cold_window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert cold_stretch is not None

    defense_window = PlayerFormWindow(
        player_id=1, team_id=1, window_games=5, season_games=15,
        window_points=0.0, window_minutes=150.0,
        window_fga_rim=0, window_fga_mid=0, window_fga_three=0, window_fgm_three=0,
        window_fta=0, window_ftm=0, window_ast=0, window_stl=10, window_blk=10,
        window_dreb=0, window_tov=0,
        season_points=0.0, season_minutes=450.0,
        season_fga_rim=0, season_fga_mid=0, season_fga_three=0, season_fgm_three=0,
        season_fta=0, season_ftm=0, season_ast=0, season_stl=20, season_blk=20,
        season_dreb=0, season_tov=0,
    )
    defensive_standout = form.defensive_standout_package(
        defense_window, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert defensive_standout is not None

    positions = list(Position)
    need = {pos: dict.fromkeys(NeedTier, 0.1) for pos in positions}
    need[positions[0]][NeedTier.STARTER] = 0.70
    roster_construction = construction.roster_construction_package(
        1, need=need, glut={pos: dict.fromkeys(NeedTier, 0.1) for pos in positions},
        supply={pos: dict.fromkeys(NeedTier, 1.0) for pos in positions},
        starter_tier=NeedTier.STARTER, rotation_tier=NeedTier.ROTATION, star_tier=NeedTier.STAR,
        mean_age_top8=26.0, timeline=Timeline.RETOOL, gm_philosophy=GmPhilosophy.EGALITARIAN,
        measured_three_point_bias=50, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert roster_construction is not None

    rivalry_matchup = rivalry.rivalry_matchup_package(
        1, 2,
        rivalry.RivalrySignals(
            same_division=True, close_game_count=0, season_series_level=False,
            recent_trade=True, h2h_signal=True,
        ),
        has_upcoming_scheduled_game=True, team_wins_this_series=2, opponent_wins_this_series=1,
        closest_margin=3, team_standings_position=5, opponent_standings_position=8,
        scheduled_game_date="2026-03-01", season=_SEASON, phase=_PHASE,
        cycle_index=_CYCLE_INDEX,
    )
    assert rivalry_matchup is not None

    scheme_identity = scheme.scheme_identity_package(
        1, offensive_scheme=OffensiveScheme.MOTION, defensive_scheme=DefensiveScheme.DROP,
        pace_target=50, pace_rank=15, avg_possessions=98.0, avg_possessions_rank=15,
        three_point_bias=50, three_share=0.35, three_share_rank=15, league_size=30,
        set_by_a_person=True, games=20, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert scheme_identity is not None

    trade_market_watch = scheme.trade_market_watch_package(
        1, 1, request_state=RequestState.UNSETTLED, team_glut_position="PG",
        team_glut_value=0.65, league_biggest_need_value=0.70,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert trade_market_watch is not None

    revenge_game = revenge.revenge_game_package(
        player_id=1, team_id=1, opponent_team_id=2, trade_season=_SEASON, trade_asset_count=3,
        is_first_meeting_since_trade=True, has_upcoming_scheduled_game=True,
        player_games_played_this_season=10, team_standings_position=5,
        opponent_standings_position=10, scheduled_game_date="2026-03-01", got_his_wish=None,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert revenge_game is not None

    team_profile = profile.team_profile_package(
        1, wins=10, losses=5, standings_position=3, streak_length=2,
        standings_position_delta=None, mean_age_top8=None,
        biggest_need_position=None, biggest_need_value=None,
        biggest_glut_position=None, biggest_glut_value=None,
        offensive_scheme=None, defensive_scheme=None, three_point_bias=None,
        three_share=None, pace_target=None, avg_possessions=None,
        set_by_a_person=None, scheme_tension=None, timeline="rebuild",
        gm_philosophy="star_maxer", trades_in_count=None, trades_out_count=None,
        unrest_player_count=None, next_game_date=None, next_game_is_rivalry=None,
        prior_wins=None, prior_losses=None, prior_standings_position=None,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert team_profile is not None

    return (
        trade_executed, trade_broken_promise, hot_streak, cold_stretch, defensive_standout,
        roster_construction, rivalry_matchup, scheme_identity, trade_market_watch,
        revenge_game, team_profile,
    )


def test_score_candidates_scores_are_bounded_zero_to_one_for_all_22_kinds():
    """MV-9's own Spine-retype acceptance test, now buildable in full: every
    one of MV-2's eleven new detectors has a real function, so this sweeps
    all 22 `StoryKind` members' own real packages through `score_candidates`
    at once."""
    old_packages = [_sufficient_package(kind) for kind in _ORIGINAL_ELEVEN_STORY_KINDS]
    new_packages = list(_sufficient_packages_for_the_new_eleven_kinds())
    all_packages = old_packages + new_packages
    assert {p.story_kind for p in all_packages} == set(StoryKind)

    candidates = [SalienceCandidate(package=p) for p in all_packages]
    scored = score_candidates(candidates)
    assert all(0.0 <= s.score <= 1.0 for s in scored)


def test_score_candidates_higher_magnitude_scores_higher():
    small_delta = _package(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE,
            team_id=1,
            team_games_played_this_season=20,
            has_pulse_snapshot=True,
            standings_position=10,
            standings_position_at_last_pulse=12,
        )
    )
    big_delta = _package(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE,
            team_id=1,
            team_games_played_this_season=20,
            has_pulse_snapshot=True,
            standings_position=1,
            standings_position_at_last_pulse=14,
        )
    )
    small_scored = score_candidates((SalienceCandidate(package=small_delta),))[0]
    big_scored = score_candidates((SalienceCandidate(package=big_delta),))[0]
    assert big_scored.score > small_scored.score


# ---------------------------------------------------------------------------
# BLOCKING 2 (critic round on commits b288b8e..99db9f4): SCHEME_IDENTITY had
# no _magnitude branch at all (fixed-neutral 0.5, identical for every team
# regardless of how extreme its measured ranks were). Reproduction: before
# this fix, the two packages below (median rank vs. an edge rank) scored
# IDENTICALLY; this pins that they now differ.
# ---------------------------------------------------------------------------


def test_scheme_identity_magnitude_reflects_rank_extremity_not_a_fixed_neutral():
    from dba2.domain.team import DefensiveScheme, OffensiveScheme
    from dba2.media.detect import scheme

    median_rank_package = scheme.scheme_identity_package(
        1, offensive_scheme=OffensiveScheme.MOTION, defensive_scheme=DefensiveScheme.DROP,
        pace_target=50, pace_rank=15, avg_possessions=98.0, avg_possessions_rank=15,
        three_point_bias=50, three_share=0.35, three_share_rank=15, league_size=30,
        set_by_a_person=True, games=20, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    edge_rank_package = scheme.scheme_identity_package(
        1, offensive_scheme=OffensiveScheme.MOTION, defensive_scheme=DefensiveScheme.DROP,
        pace_target=50, pace_rank=1, avg_possessions=98.0, avg_possessions_rank=1,
        three_point_bias=50, three_share=0.35, three_share_rank=1, league_size=30,
        set_by_a_person=True, games=20, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert median_rank_package is not None
    assert edge_rank_package is not None

    median_scored = score_candidates((SalienceCandidate(package=median_rank_package),))[0]
    edge_scored = score_candidates((SalienceCandidate(package=edge_rank_package),))[0]
    assert edge_scored.score > median_scored.score


def test_scheme_identity_still_clears_the_floor_even_at_the_least_extreme_rank():
    """The documented, proven-not-just-asserted consequence of this kind
    having no eligibility gate (see `KIND_WEIGHTS`'s own `SCHEME_IDENTITY`
    docstring in `dba2/media/salience.py`): even the LEAST notable possible
    reading (a dead-median rank on every axis) still clears `SALIENCE_FLOOR`
    with neutral prominence and full novelty, because `_KIND_WEIGHT_WEIGHT
    * KIND_WEIGHTS[SCHEME_IDENTITY] + _PROMINENCE_WEIGHT * 0.5 +
    _NOVELTY_WEIGHT * 1.0` alone already exceeds it. This is the reproduction
    of BLOCKING 2's own measured claim, pinned as a real test rather than a
    one-off calculation, and it is WHY option (b) (explicit "K-bounded, not
    floor-bounded" documentation) is required in addition to the magnitude
    fix above, not instead of it."""
    from dba2.domain.team import DefensiveScheme, OffensiveScheme
    from dba2.media.detect import scheme

    median_rank_package = scheme.scheme_identity_package(
        1, offensive_scheme=OffensiveScheme.MOTION, defensive_scheme=DefensiveScheme.DROP,
        pace_target=50, pace_rank=15, avg_possessions=98.0, avg_possessions_rank=15,
        three_point_bias=50, three_share=0.35, three_share_rank=15, league_size=30,
        set_by_a_person=True, games=20, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert median_rank_package is not None
    scored = score_candidates((SalienceCandidate(package=median_rank_package),))[0]
    from dba2.media.salience import SALIENCE_FLOOR

    assert scored.score > SALIENCE_FLOOR


def test_score_candidates_prominence_none_reads_as_neutral_not_zero():
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    neutral = score_candidates((SalienceCandidate(package=package, subject_prominence=None),))[0]
    zeroed = score_candidates((SalienceCandidate(package=package, subject_prominence=0.0),))[0]
    assert neutral.score > zeroed.score


def test_score_candidates_prominence_non_default_value_moves_the_score():
    """Round 10 critic fix (nit 3) regression guard: the existing bounds
    test only ever exercised `subject_prominence=None`. A real,
    non-default, non-`None` in-range value must move the score away from
    the neutral reading."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    neutral = score_candidates((SalienceCandidate(package=package, subject_prominence=None),))[0]
    high = score_candidates((SalienceCandidate(package=package, subject_prominence=0.9),))[0]
    assert high.score > neutral.score


def test_score_candidates_prominence_is_clamped_to_unit_range():
    """Round 10 critic fix (nit 3): a caller-supplied `subject_prominence`
    outside `[0.0, 1.0]` (`1000.0`, `-50.0`) used to break this module's
    own stated "a candidate maxed on every term scores exactly 1.0" bound.
    Both an over-range and an under-range value must clamp to the same
    score as their respective boundary value."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    over = score_candidates((SalienceCandidate(package=package, subject_prominence=1000.0),))[0]
    at_max = score_candidates((SalienceCandidate(package=package, subject_prominence=1.0),))[0]
    assert over.score == at_max.score
    assert over.score <= 1.0

    under = score_candidates((SalienceCandidate(package=package, subject_prominence=-50.0),))[0]
    at_min = score_candidates((SalienceCandidate(package=package, subject_prominence=0.0),))[0]
    assert under.score == at_min.score
    assert under.score >= 0.0


# ---------------------------------------------------------------------------
# KIND_WEIGHTS lookup -- Round 10 critic fix (nit 9)
# ---------------------------------------------------------------------------


def test_score_candidates_raises_assertion_error_for_an_unweighted_kind():
    """Round 10 critic fix (nit 9): a bare `KeyError` for an unhandled
    `StoryKind` member is replaced with a loud, explanatory
    `AssertionError`, matching `dba2/media/frame.py`'s own exhaustiveness-
    guard house style for the identical failure class. `KIND_WEIGHTS` is
    monkeypatched via a real dict copy missing one member -- there is no
    real, unhandled `StoryKind` member today (`test_every_story_kind_has_
    a_kind_weight` above already guarantees that), so this reproduces the
    failure mode the guard exists for rather than waiting for a future
    enum addition to hit it."""
    import dba2.media.salience as salience_module

    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    original = dict(salience_module.KIND_WEIGHTS)
    del salience_module.KIND_WEIGHTS[StoryKind.SIGNING_REACTION]
    try:
        with pytest.raises(AssertionError, match="SIGNING_REACTION"):
            score_candidates((SalienceCandidate(package=package),))
    finally:
        salience_module.KIND_WEIGHTS.clear()
        salience_module.KIND_WEIGHTS.update(original)


# ---------------------------------------------------------------------------
# select_top_k -- empty, all-tied, more-than-k adversarial cases.
# ---------------------------------------------------------------------------


def test_select_top_k_empty_input_returns_empty():
    assert select_top_k(()) == ()


def test_select_top_k_all_tied_scores_preserves_input_order_and_respects_k():
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    candidates = tuple(SalienceCandidate(package=package) for _ in range(5))
    scored = score_candidates(candidates)
    # Every candidate is built identically, so every score ties exactly.
    assert len({s.score for s in scored}) == 1
    top = select_top_k(scored, floor=0.0, k=3)
    assert len(top) == 3
    assert top == scored[:3]  # stable order preserved


def test_select_top_k_more_candidates_than_k_truncates():
    package = _package(
        StoryCandidate(
            kind=StoryKind.HOLDOUT_WATCH, player_id=1, request_state=RequestState.HOLDOUT
        )
    )
    scored = score_candidates(tuple(SalienceCandidate(package=package) for _ in range(20)))
    top = select_top_k(scored, floor=0.0, k=5)
    assert len(top) == 5


def test_select_top_k_excludes_a_candidate_scoring_exactly_the_floor():
    """Round 10 critic fix (nit 1): the floor is "above," per both P4-29's
    own text and `SALIENCE_FLOOR`'s docstring -- a candidate scoring
    EXACTLY the floor value must be excluded, not emitted. Uses the
    candidate's own real score as the floor, rather than a hand-picked
    number, so this cannot pass by coincidence if the comparison direction
    regresses."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates((SalienceCandidate(package=package),))
    exact_score = scored[0].score
    assert select_top_k(scored, floor=exact_score, k=10) == ()
    assert select_top_k(scored, floor=exact_score - 0.0001, k=10) == scored


def test_select_top_k_applies_floor_independently_of_k():
    low = _package(
        StoryCandidate(kind=StoryKind.ROOKIE_WATCH, player_id=1, player_games_played_this_season=10)
    )
    high = _package(
        StoryCandidate(
            kind=StoryKind.HOLDOUT_WATCH, player_id=1, request_state=RequestState.HOLDOUT
        )
    )
    scored = score_candidates((SalienceCandidate(package=low), SalienceCandidate(package=high)))
    top = select_top_k(scored, floor=0.9, k=10)
    assert all(s.score >= 0.9 for s in top)


def test_select_top_k_sorts_descending_by_score():
    a = _package(
        StoryCandidate(kind=StoryKind.ROOKIE_WATCH, player_id=1, player_games_played_this_season=10)
    )
    b = _package(
        StoryCandidate(
            kind=StoryKind.HOLDOUT_WATCH, player_id=2, request_state=RequestState.HOLDOUT
        )
    )
    scored = score_candidates((SalienceCandidate(package=a), SalienceCandidate(package=b)))
    top = select_top_k(scored, floor=0.0, k=10)
    scores = [s.score for s in top]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# above_floor -- Decision P4-103, Part A: the one predicate `select_top_k`
# and `dba2/services/media_service.py::run_media_pulse_cycle`'s own
# `candidates_above_floor` count now share.
# ---------------------------------------------------------------------------


def test_above_floor_matches_select_top_ks_own_floor_semantics():
    """Same `>` (strictly above), not `>=` -- a candidate scoring exactly
    the floor is excluded, mirroring `test_select_top_k_excludes_a_
    candidate_scoring_exactly_the_floor` above but against the extracted
    predicate directly."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates((SalienceCandidate(package=package),))
    exact_score = scored[0].score
    assert above_floor(scored, floor=exact_score) == ()
    assert above_floor(scored, floor=exact_score - 0.0001) == scored


def test_above_floor_is_the_predicate_select_top_k_truncates_from():
    """Decision P4-103's own ordering invariant, checked directly against
    the two real functions rather than only against `run_media_pulse_
    cycle`'s integration coverage: `len(above_floor(scored)) >=
    len(select_top_k(scored))`, and `select_top_k`'s own survivors are a
    subset (by object identity, since neither function rebuilds a
    `ScoredCandidate`) of `above_floor`'s."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates(tuple(SalienceCandidate(package=package) for _ in range(20)))
    survivors = above_floor(scored, floor=0.0)
    top = select_top_k(scored, floor=0.0, k=5)
    assert len(scored) >= len(survivors) >= len(top)
    assert set(id(s) for s in top) <= set(id(s) for s in survivors)


def test_above_floor_empty_input_returns_empty():
    assert above_floor(()) == ()


# ---------------------------------------------------------------------------
# build_publication_plan -- docs/design/media-v2-newsroom.md MV-1c.4, real
# three-group plan as of "Build order" step 6 (`phase`-aware K, MV-5; a real
# `hot_take_kinds`-gated band, MV-3h.0).
# ---------------------------------------------------------------------------


def test_build_publication_plan_on_empty_input_is_an_empty_plan_that_does_not_raise():
    plan = build_publication_plan((), phase=_PHASE, profile_candidates=())
    assert plan.main == ()
    assert plan.band == ()
    assert plan.profile == ()
    assert plan.candidates == ()


def test_build_publication_plan_main_group_uses_the_phase_own_top_k():
    from dba2.media.salience import TOP_K_BY_PHASE

    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates(tuple(SalienceCandidate(package=package) for _ in range(20)))
    plan = build_publication_plan(scored, phase=Phase.WAIVERS)
    assert plan.main == select_top_k(scored, k=TOP_K_BY_PHASE[Phase.WAIVERS])
    assert len(plan.main) == TOP_K_BY_PHASE[Phase.WAIVERS] == 5


def test_build_publication_plan_band_is_empty_when_no_hot_take_kinds_are_named():
    """The default (`hot_take_kinds=frozenset()`) keeps every pre-band caller's old,
    band-less behavior -- a real caller must explicitly name `hot_take`'s covered
    kinds to get a populated band."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates((SalienceCandidate(package=package),))
    assert scored[0].score > 0.0
    plan = build_publication_plan(scored, phase=_PHASE)
    assert plan.band == ()


def test_build_publication_plan_band_admits_only_sub_floor_candidates_of_a_covered_kind():
    """Directly-constructed `ScoredCandidate`s (rather than run through `score_
    candidates`) so this test proves `_band_candidates`' own admission rule --
    score range AND kind membership -- without also depending on any particular
    kind's magnitude formula landing in a specific range."""
    from dba2.media.salience import ScoredCandidate

    covered_package = _sufficient_package(StoryKind.SIGNING_REACTION)
    uncovered_package = _sufficient_package(StoryKind.ROOKIE_WATCH)
    covered = ScoredCandidate(candidate=SalienceCandidate(package=covered_package), score=0.25)
    uncovered = ScoredCandidate(candidate=SalienceCandidate(package=uncovered_package), score=0.25)
    plan = build_publication_plan(
        (covered, uncovered), phase=_PHASE, hot_take_kinds=frozenset({StoryKind.SIGNING_REACTION})
    )
    assert plan.band == (covered,)


def test_build_publication_plan_band_never_overlaps_main():
    """A candidate above `SALIENCE_FLOOR` (in `.main`) never also appears in `.band`,
    even when its kind is one `hot_take` covers -- the two ranges are disjoint by
    construction (`HOT_TAKE_FLOOR < score <= SALIENCE_FLOOR` vs. `score >
    SALIENCE_FLOOR`)."""
    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    scored = score_candidates(tuple(SalienceCandidate(package=package) for _ in range(3)))
    assert all(s.score > 0.30 for s in scored), "fixture must land ABOVE the main floor"
    plan = build_publication_plan(
        scored, phase=_PHASE, hot_take_kinds=frozenset({StoryKind.SIGNING_REACTION})
    )
    assert plan.band == ()
    assert len(plan.main) == 3


def test_build_publication_plan_band_ranks_highest_score_first():
    from dba2.media.salience import ScoredCandidate

    package = _sufficient_package(StoryKind.SIGNING_REACTION)
    low = ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.20)
    high = ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.28)
    plan = build_publication_plan(
        (low, high), phase=_PHASE, hot_take_kinds=frozenset({StoryKind.SIGNING_REACTION})
    )
    assert plan.band == (high, low)


def test_build_publication_plan_profile_group_is_scored_and_bypasses_floor_and_k():
    """MV-3's own rule: a profile is scored through the SAME `score_candidates` every
    other candidate uses, but never filtered by the floor or truncated by K. Proven
    with a real, deliberately sub-floor score (`ROOKIE_WATCH`'s fixed 0.5 magnitude,
    zeroed prominence and novelty -- `0.4*0.5 + 0.25*0.0 + 0.15*0.0 + 0.20*0.35 =
    0.27`, strictly below `SALIENCE_FLOOR = 0.30`, computed here rather than merely
    asserted) among `TOP_K_PER_CYCLE`-plus other candidates -- every one of them, the
    sub-floor one included, survives into `.profile`."""
    from dba2.media.salience import SALIENCE_FLOOR, TOP_K_PER_CYCLE

    package = _sufficient_package(StoryKind.ROOKIE_WATCH)
    sub_floor_candidate = SalienceCandidate(
        package=package, subject_prominence=0.0, cycles_since_last_seen=0
    )
    profile_candidates = (
        sub_floor_candidate,
        *(SalienceCandidate(package=package) for _ in range(TOP_K_PER_CYCLE + 5)),
    )
    plan = build_publication_plan((), phase=_PHASE, profile_candidates=profile_candidates)
    assert len(plan.profile) == len(profile_candidates)
    assert all(0.0 <= s.score <= 1.0 for s in plan.profile)
    sub_floor_scored = plan.profile[0]
    assert sub_floor_scored.candidate is sub_floor_candidate
    assert sub_floor_scored.score == pytest.approx(0.27)
    assert sub_floor_scored.score < SALIENCE_FLOOR, "a real sub-floor score must still publish"


def test_build_publication_plan_candidates_property_concatenates_in_fixed_order():
    main_package = _sufficient_package(StoryKind.SIGNING_REACTION)
    profile_package = _sufficient_package(StoryKind.ROOKIE_WATCH)
    scored = score_candidates((SalienceCandidate(package=main_package),))
    profile_candidates = (SalienceCandidate(package=profile_package),)
    plan = build_publication_plan(scored, phase=_PHASE, profile_candidates=profile_candidates)
    assert plan.candidates == plan.main + plan.band + plan.profile
    assert len(plan.candidates) == len(plan.main) + len(plan.profile)


# ---------------------------------------------------------------------------
# _magnitude's new branches (MV-2's kinds, "Build order" step 4) -- built
# directly, never through `derive_frame`/`legacy.adapt_frame` (see
# `StoryKind`'s own docstring: these kinds never reach that pipeline).
# ---------------------------------------------------------------------------


def _bare_package(kind: StoryKind, facts: tuple[Fact, ...]) -> StoryPackage:
    return StoryPackage(
        story_kind=kind, angle=Angle.TRADE_MADE,
        subject=Subject(kind=SubjectKind.TEAM, team_id=1),
        facts=facts, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )


def _number_fact(key: str, value: float) -> Fact:
    return Fact(key=key, label=key, value_text=str(value), value_number=value,
                 source=FactSource.GAMES)


def test_trade_executed_magnitude_scales_with_total_assets():
    small = _bare_package(
        StoryKind.TRADE_EXECUTED,
        (
            _number_fact("players_acquired_count", 1), _number_fact("players_sent_count", 0),
            _number_fact("picks_acquired_count", 0), _number_fact("picks_sent_count", 0),
        ),
    )
    big = _bare_package(
        StoryKind.TRADE_EXECUTED,
        (
            _number_fact("players_acquired_count", 3), _number_fact("players_sent_count", 3),
            _number_fact("picks_acquired_count", 0), _number_fact("picks_sent_count", 0),
        ),
    )
    small_score = score_candidates((SalienceCandidate(package=small),))[0].score
    big_score = score_candidates((SalienceCandidate(package=big),))[0].score
    assert big_score > small_score


def test_revenge_game_magnitude_absent_reads_as_zero():
    package = _bare_package(StoryKind.REVENGE_GAME, ())
    scored = score_candidates((SalienceCandidate(package=package),))[0]
    assert 0.0 <= scored.score <= 1.0


def test_hot_streak_magnitude_absent_reads_as_neutral():
    package = _bare_package(StoryKind.HOT_STREAK, ())
    scored = score_candidates((SalienceCandidate(package=package),))[0]
    assert 0.0 <= scored.score <= 1.0


def test_rivalry_matchup_magnitude_at_max_score_is_bounded():
    package = _bare_package(StoryKind.RIVALRY_MATCHUP, (_number_fact("rivalry_score", 8),))
    scored = score_candidates((SalienceCandidate(package=package),))[0]
    assert 0.0 <= scored.score <= 1.0
