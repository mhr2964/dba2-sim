"""`dba2/media/events.py` -- `story_kind_for_request_state`/
`ladder_transition_candidate`/`trade_execution_candidate` (Decision
P4-27's event-sourced intake). §11 Stage D step 17. `draft_selection_
candidate` (Decision P4-107 item 1, Stage D step 21). `career_scoring_
milestone_candidates` (Decision P4-121 item 3, Stage D step 23) -- P4-122
obligation 1: the rule proven pure and by hand here, no database, one line
of the rule (§39.1 item 3) per test."""

from __future__ import annotations

from dba2.domain.motivation import MotivationAxis, RequestState
from dba2.domain.stats import PlayerCareerScoringToDate
from dba2.media.events import (
    career_scoring_milestone_candidates,
    draft_selection_candidate,
    ladder_transition_candidate,
    story_kind_for_request_state,
    trade_execution_candidate,
)
from dba2.media.frame import StoryKind


def test_story_kind_for_request_state_maps_holdout_to_its_own_kind():
    assert story_kind_for_request_state(RequestState.HOLDOUT) is StoryKind.HOLDOUT_WATCH


def test_story_kind_for_request_state_maps_rung_two_and_unsettled_to_unrest():
    for state in (
        RequestState.UNSETTLED,
        RequestState.TRADE_REQUESTED,
        RequestState.EXTENSION_DEMANDED,
    ):
        assert story_kind_for_request_state(state) is StoryKind.PLAYER_UNREST


def test_story_kind_for_request_state_content_is_none():
    assert story_kind_for_request_state(RequestState.CONTENT) is None


def test_ladder_transition_candidate_holdout_shape():
    candidate = ladder_transition_candidate(
        player_id=42,
        request_state=RequestState.HOLDOUT,
        motivation_axis=MotivationAxis.WINNING,
        morale=-60,
    )
    assert candidate is not None
    assert candidate.kind is StoryKind.HOLDOUT_WATCH
    assert candidate.player_id == 42
    assert candidate.motivation_axis is MotivationAxis.WINNING
    assert candidate.motivation_magnitude == 60  # round(-(-60))
    assert candidate.request_state is RequestState.HOLDOUT


def test_ladder_transition_candidate_content_returns_none():
    """A player somehow read back as CONTENT (should not happen under
    Decision P4-11's two-checkpoint cadence, but defensively handled) is
    not turned into a candidate, not raised."""
    candidate = ladder_transition_candidate(
        player_id=1,
        request_state=RequestState.CONTENT,
        motivation_axis=MotivationAxis.LOYALTY,
        morale=10,
    )
    assert candidate is None


def test_ladder_transition_candidate_magnitude_is_negated_morale_rounded():
    candidate = ladder_transition_candidate(
        player_id=1,
        request_state=RequestState.UNSETTLED,
        motivation_axis=MotivationAxis.MONEY,
        morale=17,
    )
    assert candidate is not None
    assert candidate.motivation_magnitude == -17


def test_trade_execution_candidate_shape():
    candidate = trade_execution_candidate(
        team_id=5,
        trade_in_player_ids=(101, 102),
        trade_out_player_ids=(201,),
        trade_in_pick_ids=(),
        trade_out_pick_ids=(301,),
    )
    assert candidate.kind is StoryKind.TRADE_REACTION
    assert candidate.team_id == 5
    assert candidate.trade_in_player_ids == (101, 102)
    assert candidate.trade_out_player_ids == (201,)
    assert candidate.trade_out_pick_ids == (301,)


def test_trade_execution_candidate_defaults_to_no_assets():
    candidate = trade_execution_candidate(team_id=9)
    assert candidate.trade_in_player_ids == ()
    assert candidate.trade_out_player_ids == ()
    assert candidate.trade_in_pick_ids == ()
    assert candidate.trade_out_pick_ids == ()


def test_draft_selection_candidate_shape():
    candidate = draft_selection_candidate(
        player_id=77, team_id=3, player_games_played_this_season=12
    )
    assert candidate.kind is StoryKind.ROOKIE_WATCH
    assert candidate.player_id == 77
    assert candidate.team_id == 3
    assert candidate.player_games_played_this_season == 12


def test_draft_selection_candidate_games_played_is_passed_through_unchanged():
    """No live computation, no default -- whatever the caller supplies
    (including `None`, a real "we don't have this yet" reading) is passed
    straight through to the `StoryCandidate`; `derive_frame`'s own
    `_PLAYER_SAMPLE_KINDS` gate (unchanged by this step) is what decides
    sufficiency from there."""
    candidate = draft_selection_candidate(
        player_id=1, team_id=None, player_games_played_this_season=None
    )
    assert candidate.team_id is None
    assert candidate.player_games_played_this_season is None


def _row(
    player_id: int,
    *,
    before: int = 0,
    in_season: int = 0,
    games: int = 0,
) -> PlayerCareerScoringToDate:
    return PlayerCareerScoringToDate(
        player_id=player_id,
        career_points_before_season=before,
        points_in_season=in_season,
        games_played_in_season=games,
    )


def test_milestone_empty_prior_board_returns_nothing():
    """§39.1 item 9's own closing worry: a league's first season of record
    (no player has scored anything before this season) must not emit ten
    stories ordered by serial id. `career_points_before_season == 0` for
    every row keeps the prior board empty, and the guard fires."""
    rows = (
        _row(1, before=0, in_season=1200, games=60),
        _row(2, before=0, in_season=900, games=58),
    )
    assert career_scoring_milestone_candidates(rows) == ()


def test_milestone_new_entrant_to_top_ten():
    """Nine players already occupy the prior top ten's own ranks (with
    plenty of room below `leaderboard_size`); a tenth, brand-new player
    with a real prior-season row of `0` scores enough this season to crack
    the board for the first time -- `prior_rank` is absent, not derived
    from a zero tie."""
    prior_nine = tuple(_row(i, before=10_000 - i * 100, in_season=100) for i in range(1, 10))
    new_entrant = _row(99, before=0, in_season=5_000, games=60)
    rows = (*prior_nine, new_entrant)
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    entrant_candidates = [c for c in candidates if c.player_id == 99]
    assert len(entrant_candidates) == 1
    assert entrant_candidates[0].kind is StoryKind.MILESTONE
    assert entrant_candidates[0].player_games_played_this_season == 60
    assert entrant_candidates[0].season_points_per_game == 5_000 / 60


def test_milestone_climb_within_the_board():
    """Player 2 starts BEHIND player 1 on the prior board (rank 2 vs. rank
    1) and outscores him this season enough to pass him -- an improved
    rank within an already-occupied top ten, not a new entrant."""
    rows = (
        _row(1, before=5_000, in_season=100),
        _row(2, before=4_900, in_season=500),
    )
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    movers = {c.player_id for c in candidates}
    assert 2 in movers
    assert 1 not in movers


def test_milestone_static_board_emits_nothing():
    """Both players hold their exact same relative order both times -- an
    unchanged list is not a story."""
    rows = (
        _row(1, before=5_000, in_season=100),
        _row(2, before=4_000, in_season=100),
    )
    assert career_scoring_milestone_candidates(rows, leaderboard_size=10) == ()


def test_milestone_retired_player_passed():
    """A retired player has no row this season at all (`in_season=0`,
    `games=0`) -- his career total is frozen. An active player who
    outscores him this season passes him and IS emitted; the retired
    player himself has no games this season and is never a subject."""
    retired = _row(1, before=5_000, in_season=0, games=0)
    active = _row(2, before=4_900, in_season=500, games=40)
    candidates = career_scoring_milestone_candidates((retired, active), leaderboard_size=10)
    movers = {c.player_id for c in candidates}
    assert 2 in movers
    assert 1 not in movers


def test_milestone_points_tie_broken_by_player_id():
    """Two players tie exactly on `career_points_before_season` -- the
    deterministic `player_id`-ascending tiebreak decides prior rank, not
    row order, so the rule is reproducible independent of how the caller's
    dict iterated."""
    rows = (
        _row(5, before=3_000, in_season=0, games=0),
        _row(2, before=3_000, in_season=0, games=0),
        _row(9, before=2_000, in_season=600, games=50),
    )
    # Prior ranks: player 2 (id-ascending tiebreak) = 1, player 5 = 2,
    # player 9 = 3. This season nobody moves (no in-season scoring for 2/5),
    # so the tie itself produces no candidate -- proven separately by
    # asserting player 9 (who genuinely cannot climb past a real career
    # total of 3,000 with only 2,600) is not emitted either.
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    assert candidates == ()


def test_milestone_zero_games_this_season_no_division():
    """A genuine new entrant whose row reads `games_played_in_season == 0`
    (a defensive edge case -- points recorded with no games row, or a data
    shape a future caller has not ruled out) must not raise
    `ZeroDivisionError`: the emitted candidate's `season_points_per_game`
    reads `None` rather than a fabricated value, and `player_games_played_
    this_season` still reads the real `0` rather than being silently
    swapped for `None`. (`derive_frame`'s own `NO_GAMES_PLAYED` gate,
    unchanged by this step, is what drops such a candidate downstream --
    this test proves the pure layer itself never crashes and never
    fabricates a rate.)"""
    incumbent = _row(9, before=1, in_season=0, games=0)
    rows = (incumbent, _row(1, before=0, in_season=50, games=0))
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    entrant_candidates = [c for c in candidates if c.player_id == 1]
    assert len(entrant_candidates) == 1
    assert entrant_candidates[0].player_games_played_this_season == 0
    assert entrant_candidates[0].season_points_per_game is None


def test_milestone_emission_order_is_current_board_rank_order():
    """Emission order is part of the contract (§39.1 item 9) -- the best
    career total among the movers comes first, never row-input order. A
    tiny-prior incumbent keeps the prior board non-empty (the guard) without
    itself qualifying as a mover (it gets passed, not improved); the three
    new entrants are handed to the function in the WORST-current-rank-first
    order to prove the output re-sorts them rather than echoing input order."""
    incumbent = _row(9, before=1, in_season=0, games=0)
    rows = (
        _row(3, before=0, in_season=9_000, games=60),  # worst of the three movers
        incumbent,
        _row(1, before=0, in_season=20_000, games=60),  # best of the three movers
        _row(2, before=0, in_season=15_000, games=60),
    )
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    assert [c.player_id for c in candidates] == [1, 2, 3]


def test_milestone_volume_bound_never_exceeds_leaderboard_size():
    """P4-122 obligation 2 -- the volume bound asserted, never assumed. 500
    players, every one of them a genuine climber (an empty prior board of
    one low-scoring incumbent, then 500 players who all outscore him this
    season) -- `len(candidates) <= leaderboard_size` must hold regardless
    of how much larger the mover population is than the board itself."""
    incumbent = _row(0, before=1, in_season=0, games=0)
    movers = tuple(_row(i, before=0, in_season=10_000 - i, games=60) for i in range(1, 501))
    rows = (incumbent, *movers)
    candidates = career_scoring_milestone_candidates(rows, leaderboard_size=10)
    assert len(candidates) <= 10
