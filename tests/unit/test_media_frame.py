"""`dba2/media/frame.py` -- `StoryFrame`/`StoryKind`/`InsufficientDataReason`
and `derive_frame` (Decisions P4-24, P4-25). §11 Stage D step 16."""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.motivation import MotivationAxis, RequestState
from dba2.media.frame import (
    _DELTA_KINDS,
    _MIN_PLAYER_GAMES_FOR_SAMPLE,
    _MOTIVATION_KINDS,
    _PLAYER_SAMPLE_KINDS,
    _SIGNING_KINDS,
    _TEAM_SAMPLE_KINDS,
    _TRADE_KINDS,
    InsufficientDataReason,
    StoryCandidate,
    StoryFrame,
    StoryKind,
    derive_frame,
)

# ---------------------------------------------------------------------------
# StoryKind -- listed, not counted; the injury_note exclusion.
# ---------------------------------------------------------------------------


def test_story_kind_is_exactly_twenty_two_members_listed():
    """Decision P4-25 closed the enum at eleven; docs/design/media-v2-
    newsroom.md MV-2 ("Build order" step 4) adds eleven more -- listed
    rather than counted, so the count cannot drift silently, asserted here
    by NAME, not merely by `len(...)`, so a swap (one kind renamed to
    another) would still fail this test. The original eleven are the ones
    `dba2/media/frame.py`'s own `StoryFrame`/`derive_frame` pipeline
    classifies (see the test just below); the eleven new ones never reach
    that pipeline at all (`StoryKind`'s own docstring)."""
    assert {member.value for member in StoryKind} == {
        # The original eleven (Decision P4-25).
        "standings_surge", "standings_collapse", "streak",
        "trade_reaction", "trade_fallout", "signing_reaction",
        "milestone", "award_race", "player_unrest", "holdout_watch",
        "rookie_watch",
        # MV-2's eleven new members.
        "trade_executed", "trade_broken_promise", "revenge_game",
        "hot_streak", "cold_stretch", "defensive_standout",
        "roster_construction", "scheme_identity", "rivalry_matchup",
        "trade_market_watch", "team_profile",
    }


def test_story_kind_excludes_injury_note():
    """Census finding (phase-4-stage-a-census.md step 2): `injuries` has
    zero rows anywhere in the real database and no service ever writes
    one -- `injury_note` is unconditionally zero-candidate today, dropped
    per that doc's own recommendation rather than kept as an unreachable
    member."""
    assert "injury_note" not in {member.value for member in StoryKind}


# The original eleven `StoryKind` members -- the exact, closed set `dba2/
# media/frame.py`'s own `_insufficiency_reason` groups classify. **Not
# `set(StoryKind)`**: MV-2 (docs/design/media-v2-newsroom.md, "Build order"
# step 4) grew the enum to twenty-two, but `frame.py` itself is UNCHANGED by
# that design (absent from its own "where the change lands" file table,
# MV-9) -- the eleven new members are never turned into a `StoryFrame` at
# all, so this module's groups were never meant to cover them, and testing
# them against the live, growing enum would be testing the wrong claim. A
# real `StoryCandidate` of a new kind reaching `derive_frame` is exactly the
# case `_insufficiency_reason`'s own final `AssertionError` branch exists to
# catch -- correct behavior, not a gap this test should paper over.
_ORIGINAL_ELEVEN_STORY_KINDS = frozenset(
    {
        StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE, StoryKind.STREAK,
        StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT, StoryKind.SIGNING_REACTION,
        StoryKind.MILESTONE, StoryKind.AWARD_RACE, StoryKind.PLAYER_UNREST,
        StoryKind.HOLDOUT_WATCH, StoryKind.ROOKIE_WATCH,
    }
)


def test_every_story_kind_is_classified_into_exactly_one_group():
    """`_insufficiency_reason`'s exhaustiveness precondition -- a future
    kind added to the enum without a matching group entry must fail loudly
    (the `AssertionError` branch), not silently read as always-sufficient.
    Checked here directly against the groups, and again indirectly by the
    exhaustiveness test below (every real kind reaches a real branch)."""
    groups = (
        _TEAM_SAMPLE_KINDS, _PLAYER_SAMPLE_KINDS, _MOTIVATION_KINDS,
        _TRADE_KINDS, _SIGNING_KINDS,
    )
    covered: set[StoryKind] = set()
    for group in groups:
        assert not (covered & group), f"a kind appears in two groups: {covered & group}"
        covered |= group
    assert covered == _ORIGINAL_ELEVEN_STORY_KINDS
    assert _DELTA_KINDS <= _TEAM_SAMPLE_KINDS


# ---------------------------------------------------------------------------
# StoryFrame -- totality/shape.
# ---------------------------------------------------------------------------


def test_story_frame_is_frozen():
    frame = derive_frame(StoryCandidate(kind=StoryKind.SIGNING_REACTION))
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.team_id = 5  # type: ignore[misc]


def test_story_frame_field_names_match_the_declared_vocabulary():
    field_names = {f.name for f in dataclasses.fields(StoryFrame)}
    assert field_names == {
        "kind", "team_id", "player_id", "insufficient_data",
        "standings_position", "standings_position_delta", "streak_length",
        "season_points_per_game", "season_points_per_game_delta",
        "season_minutes_per_game",
        "trade_in_players", "trade_out_players", "trade_in_picks", "trade_out_picks",
        "motivation_axis", "motivation_magnitude", "request_state",
        "award_share_estimate",
    }


# ---------------------------------------------------------------------------
# The insufficient_data short-circuit -- five independently constructed
# degenerate cases (§9.5's "five constructed cases minimum, one per
# insufficient_data cause"), each proven to short-circuit rather than
# raise, exactly as managers/voice/frame.py's illegal-record tests do.
# ---------------------------------------------------------------------------


def _assert_short_circuited(frame: StoryFrame, *, reason: InsufficientDataReason) -> None:
    assert frame.insufficient_data == reason
    assert frame.standings_position is None
    assert frame.standings_position_delta is None
    assert frame.streak_length is None
    assert frame.season_points_per_game is None
    assert frame.season_points_per_game_delta is None
    assert frame.season_minutes_per_game is None
    assert frame.trade_in_players == 0
    assert frame.trade_out_players == 0
    assert frame.trade_in_picks == 0
    assert frame.trade_out_picks == 0
    assert frame.motivation_axis is None
    assert frame.motivation_magnitude is None
    assert frame.request_state is None
    assert frame.award_share_estimate is None


def test_no_subject_identity_team_scoped_kind():
    """A standings-surge candidate with no team_id at all."""
    frame = derive_frame(StoryCandidate(kind=StoryKind.STANDINGS_SURGE, team_id=None))
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_SUBJECT_IDENTITY)
    assert frame.kind == StoryKind.STANDINGS_SURGE
    assert frame.team_id is None


def test_no_subject_identity_player_scoped_kind():
    frame = derive_frame(StoryCandidate(kind=StoryKind.MILESTONE, player_id=None))
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_SUBJECT_IDENTITY)


def test_early_season_sample_a_team_in_game_3():
    """Decision P4-24's own example, constructed literally: a team three
    games into its season."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.STREAK,
            team_id=7,
            team_games_played_this_season=3,
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.EARLY_SEASON_SAMPLE)
    assert frame.team_id == 7


def test_no_games_played_a_player_with_zero_games():
    """Decision P4-24's own example: a rookie's first game, or a healthy
    scratch -- season_points_per_game/minutes are undefined, not zero.
    Renamed from `ZERO_MINUTES`/`test_zero_minutes_...` (round 9 critic
    nit 4): this module never reads a minutes field, only games played."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.ROOKIE_WATCH,
            player_id=42,
            player_games_played_this_season=0,
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_GAMES_PLAYED)
    assert frame.player_id == 42


def test_no_games_played_a_negative_games_count_reads_as_insufficient_too():
    """round 9 critic blocking finding 3's negative-input half: a corrupt
    `player_games_played_this_season=-3` used to read SUFFICIENT under the
    old `== 0` check (asymmetric with the team side, which already caught
    negative input via its own threshold comparison) -- `<= 0` now catches
    it the same way `NO_GAMES_PLAYED`'s docstring discloses."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.MILESTONE,
            player_id=7,
            player_games_played_this_season=-3,
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_GAMES_PLAYED)


def test_early_season_sample_now_reachable_for_a_player_scoped_kind():
    """round 9 critic blocking finding 3's positive half: before the real
    `_MIN_PLAYER_GAMES_FOR_SAMPLE` threshold existed, `EARLY_SEASON_SAMPLE`
    was structurally unreachable for every player-scoped kind (a rookie's
    first game read SUFFICIENT, a per-game rate computed from an n=1
    sample). A player with one game played -- nonzero, but below the
    threshold -- now reads EARLY_SEASON_SAMPLE, matching the team side's
    own already-real threshold check."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.AWARD_RACE,
            player_id=8,
            player_games_played_this_season=1,
        )
    )
    assert 1 < _MIN_PLAYER_GAMES_FOR_SAMPLE
    _assert_short_circuited(frame, reason=InsufficientDataReason.EARLY_SEASON_SAMPLE)


def test_player_scoped_kind_is_sufficient_at_the_real_threshold():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.MILESTONE,
            player_id=8,
            player_games_played_this_season=_MIN_PLAYER_GAMES_FOR_SAMPLE,
            season_points_per_game=20.0,
        )
    )
    assert frame.insufficient_data is None
    assert frame.season_points_per_game == 20.0


# ---------------------------------------------------------------------------
# _MOTIVATION_KINDS (player_unrest, holdout_watch) -- round 9 critic
# blocking finding 1: gated on motivation-data presence, never on games
# played, because a real holdout has played zero games by definition.
# ---------------------------------------------------------------------------


def test_holdout_watch_a_real_holdout_with_zero_games_played_is_sufficient():
    """The critic's own exact adversarial case: a real holdout has played
    ZERO games this season (that is what a holdout IS) -- under the old
    `_PLAYER_SAMPLE_KINDS` gating this was structurally unreachable
    (always short-circuited to `NO_GAMES_PLAYED`/`ZERO_MINUTES`, with
    `request_state` nulled out by the short-circuit branch). It must now
    produce a SUFFICIENT frame that actually carries `request_state=
    HOLDOUT`, not a short-circuited `None`."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.HOLDOUT_WATCH,
            player_id=99,
            player_games_played_this_season=0,
            request_state=RequestState.HOLDOUT,
        )
    )
    assert frame.insufficient_data is None
    assert frame.request_state == RequestState.HOLDOUT
    assert frame.player_id == 99


def test_player_unrest_a_zero_game_player_with_a_motivation_axis_is_sufficient():
    """Same adversarial shape as the holdout case, for `player_unrest`,
    driven by `motivation_axis` instead of `request_state` -- proving the
    gate accepts EITHER motivation field, not only `request_state`."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.PLAYER_UNREST,
            player_id=100,
            player_games_played_this_season=0,
            motivation_axis=MotivationAxis.ROLE_USAGE,
            motivation_magnitude=-40,
        )
    )
    assert frame.insufficient_data is None
    assert frame.motivation_axis == MotivationAxis.ROLE_USAGE
    assert frame.motivation_magnitude == -40


def test_no_motivation_data_neither_request_state_nor_axis_present():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.HOLDOUT_WATCH,
            player_id=101,
            player_games_played_this_season=0,
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_MOTIVATION_DATA)


def test_motivation_kind_no_subject_identity():
    frame = derive_frame(
        StoryCandidate(kind=StoryKind.PLAYER_UNREST, player_id=None)
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_SUBJECT_IDENTITY)


def test_no_pulse_snapshot_a_delta_kind_with_no_prior_cycle():
    """A well-established team (plenty of games this season) whose delta
    kind still has nothing to diff against -- Decision P4-28's own named
    failure mode, distinct from EARLY_SEASON_SAMPLE."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.STANDINGS_COLLAPSE,
            team_id=3,
            team_games_played_this_season=60,
            has_pulse_snapshot=False,
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_PULSE_SNAPSHOT)


def test_streak_kind_does_not_require_a_pulse_snapshot():
    """`streak` is team-scoped but NOT in `_DELTA_KINDS` -- a well-sampled
    team with no snapshot still produces a real (non-short-circuited)
    frame, proving the NO_PULSE_SNAPSHOT check is scoped to delta kinds
    only, not every team-scoped kind."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.STREAK,
            team_id=3,
            team_games_played_this_season=60,
            has_pulse_snapshot=False,
            streak_length=4,
        )
    )
    assert frame.insufficient_data is None
    assert frame.streak_length == 4


def test_no_trade_assets_a_trade_story_with_no_legs():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.TRADE_REACTION,
            team_id=11,
            trade_in_player_ids=(),
            trade_out_player_ids=(),
            trade_in_pick_ids=(),
            trade_out_pick_ids=(),
        )
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_TRADE_ASSETS)


def test_signing_reaction_needs_both_identity_tokens():
    frame = derive_frame(
        StoryCandidate(kind=StoryKind.SIGNING_REACTION, player_id=9, team_id=None)
    )
    _assert_short_circuited(frame, reason=InsufficientDataReason.NO_SUBJECT_IDENTITY)


def test_unclassified_kind_raises_rather_than_reading_as_sufficient(monkeypatch):
    """A hypothetical future `StoryKind` member added to the enum without
    a matching group entry in `frame.py` must fail loudly, not silently
    read as always-sufficient -- proven by constructing a real (if
    contrived) case: an object shaped enough like `StoryCandidate` to
    reach `_insufficiency_reason`, but carrying a kind absent from every
    group tuple."""
    from dba2.media import frame as frame_module

    class _RogueKind:
        name = "ROGUE"

    candidate = StoryCandidate(kind=StoryKind.STREAK)
    object.__setattr__(candidate, "kind", _RogueKind())
    with pytest.raises(AssertionError, match="not classified into any"):
        frame_module._insufficiency_reason(candidate)


# ---------------------------------------------------------------------------
# The normal (sufficient-data) path -- real computation, deltas, sign
# convention, and the trade-asset-count/identity-token classification.
# ---------------------------------------------------------------------------


def test_standings_delta_sign_convention_positive_means_fell():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE,
            team_id=1,
            team_games_played_this_season=20,
            has_pulse_snapshot=True,
            standings_position=10,
            standings_position_at_last_pulse=4,
        )
    )
    assert frame.insufficient_data is None
    assert frame.standings_position == 10
    assert frame.standings_position_delta == 6  # rose in position NUMBER -> fell in rank


def test_delta_is_none_when_prior_pulse_value_missing_even_off_short_circuit():
    """A delta kind with a real sample AND a snapshot flag set, but the
    specific prior-value field itself absent (a partial/inconsistent
    caller) -- `_delta` must still return `None`, not `0`, matching the
    module's own "one no-reading value" rule."""
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE,
            team_id=1,
            team_games_played_this_season=20,
            has_pulse_snapshot=True,
            standings_position=10,
            standings_position_at_last_pulse=None,
        )
    )
    assert frame.insufficient_data is None
    assert frame.standings_position_delta is None


def test_season_points_per_game_delta_computed_when_both_sides_present():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.MILESTONE,
            player_id=5,
            player_games_played_this_season=30,
            season_points_per_game=22.5,
            season_points_per_game_at_last_pulse=18.0,
        )
    )
    assert frame.insufficient_data is None
    assert frame.season_points_per_game_delta == pytest.approx(4.5)


def test_trade_asset_counts_from_id_tuples():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.TRADE_FALLOUT,
            team_id=2,
            trade_in_player_ids=(101, 102),
            trade_out_player_ids=(201,),
            trade_in_pick_ids=(),
            trade_out_pick_ids=(301, 302, 303),
        )
    )
    assert frame.insufficient_data is None
    assert frame.trade_in_players == 2
    assert frame.trade_out_players == 1
    assert frame.trade_in_picks == 0
    assert frame.trade_out_picks == 3


def test_motivation_and_request_state_pass_through_when_sufficient():
    frame = derive_frame(
        StoryCandidate(
            kind=StoryKind.PLAYER_UNREST,
            player_id=8,
            player_games_played_this_season=10,
            motivation_axis=MotivationAxis.ROLE_USAGE,
            motivation_magnitude=-15,
            request_state=RequestState.TRADE_REQUESTED,
        )
    )
    assert frame.insufficient_data is None
    assert frame.motivation_axis == MotivationAxis.ROLE_USAGE
    assert frame.motivation_magnitude == -15
    assert frame.request_state == RequestState.TRADE_REQUESTED


def test_not_yet_wired_fields_default_to_none_even_on_a_sufficient_frame():
    """Every field this step has no data source for at all yet stays
    `None` on the NORMAL path too, independent of `insufficient_data` --
    the module docstring's "unconditionally `None` in this step" fields,
    proven here on a frame that is NOT short-circuited."""
    frame = derive_frame(
        StoryCandidate(kind=StoryKind.SIGNING_REACTION, player_id=1, team_id=2)
    )
    assert frame.insufficient_data is None
    assert frame.motivation_axis is None
    assert frame.motivation_magnitude is None
    assert frame.request_state is None
    assert frame.award_share_estimate is None
    assert frame.standings_position is None
    assert frame.season_points_per_game is None
