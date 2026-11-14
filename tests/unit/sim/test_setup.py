"""Tier 1, §12 step 2's verify bullets: every action type's declared roles
are filled by distinct on-court players; role assignment is reproducible
from a seed and differs across seeds; a lineup's tendency mix shifts
action-type selection in the expected direction."""
from __future__ import annotations

import dataclasses
from random import Random

import pytest

from dba2.domain.player import Position
from dba2.domain.role import UsageMode
from dba2.domain.team import OffensiveScheme
from dba2.sim.beats.setup import (
    _action_type_weights,
    _overall_to_touch_share_estimate,
    _require_offensive_scheme_action_multipliers_are_total_and_positive,
    _role_weight,
    assign_offense_roles,
    resolve_setup_beat,
    select_action_type,
)
from dba2.sim.playbook import ROLE_TEMPLATES, ActionType, RoleSlot
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_directive, make_sim_player

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _lineup(base_id: int):
    return tuple(
        make_sim_player(
            base_id + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i],
            height_in=74 + i * 3,
        )
        for i in range(5)
    )


def test_every_action_type_fills_its_declared_roles_with_distinct_players():
    offense = _lineup(1)
    for action_type in ActionType:
        template = ROLE_TEMPLATES[action_type]
        assigned = assign_offense_roles(action_type, offense, DEFAULT_TUNING, Random(0))
        assert set(assigned) == set(template.offense)
        assert len(set(assigned.values())) == len(assigned), (
            f"{action_type}: role assignment reused a player across roles: {assigned}"
        )
        assert set(assigned.values()).issubset({p.id for p in offense})


def test_role_assignment_is_identical_across_two_runs_with_the_same_seed():
    offense = _lineup(1)
    first = assign_offense_roles(ActionType.PNR_BALL, offense, DEFAULT_TUNING, Random(99))
    second = assign_offense_roles(ActionType.PNR_BALL, offense, DEFAULT_TUNING, Random(99))
    assert first == second


def test_role_assignment_differs_across_different_seeds():
    offense = _lineup(1)
    results = {
        tuple(
            assign_offense_roles(
                ActionType.PNR_BALL, offense, DEFAULT_TUNING, Random(seed)
            ).items()
        )
        for seed in range(30)
    }
    assert len(results) > 1, "30 different seeds produced identical role assignments every time"


def test_resolve_setup_beat_end_to_end_fills_every_role_and_returns_a_beat_record():
    offense = _lineup(1)
    defense = _lineup(11)
    result = resolve_setup_beat(
        offense, defense, DEFAULT_TUNING, Random(5), clock_seconds=24.0, home_score=0, away_score=0
    )
    template = ROLE_TEMPLATES[result.action_type]
    assert set(result.role_assignment.offense) == set(template.offense)
    assert set(result.role_assignment.defense) == set(template.defense)
    assert result.record.beat == "setup"
    assert result.record.outcome == result.action_type.value
    assert abs(sum(result.record.probabilities.values()) - 1.0) < 1e-9


def test_select_action_type_is_seed_reproducible_and_seed_sensitive():
    offense = _lineup(1)
    first = [select_action_type(offense, DEFAULT_TUNING, Random(s)) for s in range(20)]
    second = [select_action_type(offense, DEFAULT_TUNING, Random(s)) for s in range(20)]
    assert first == second
    assert len(set(first)) > 1


def test_a_high_pnr_ball_freq_roster_selects_pnr_ball_more_often_than_a_high_iso_freq_roster():
    pnr_heavy = tuple(
        make_sim_player(
            100 + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i],
            tendencies_overrides={"pnr_ball_freq": 95, "iso_freq": 5},
        )
        for i in range(5)
    )
    iso_heavy = tuple(
        make_sim_player(
            200 + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i],
            tendencies_overrides={"pnr_ball_freq": 5, "iso_freq": 95},
        )
        for i in range(5)
    )

    def _pnr_ball_share(lineup, trials: int = 400) -> float:
        rng = Random(12345)
        picks = [select_action_type(lineup, DEFAULT_TUNING, rng) for _ in range(trials)]
        return picks.count(ActionType.PNR_BALL) / trials

    assert _pnr_ball_share(pnr_heavy) > _pnr_ball_share(iso_heavy)


def test_ball_handler_selection_favors_a_real_touch_share_star_over_roleless_bench_players():
    """B3 regression: `player_roles.touch_share` (~0.10-0.35, a real
    fraction) and the no-role-row `overall_cached` fallback used to be
    compared on incompatible scales (`overall_cached / 99.0`, ~0.35-1.0),
    which critic proved let two roleless bench players (overall 48/42)
    out-draw an 85-overall starter with a real 0.28 `touch_share` for
    BALL_HANDLER selection. This mixed lineup (critic noted no existing
    fixture mixed with-role and without-role players in one lineup) pins
    `pnr_ball_freq` identical across every player, isolating the
    touch_share-vs-overall_cached scale term as the only thing that can
    move each player's draw share, then tabulates 20,000 draws.

    B3.1 regression (critic-proven, this dispatch): the assist-compression
    fix's `SetupTuning.ball_handler_passing_ability_anchor_low/_high`
    multiplier reopened this exact invariant from a second axis -- this
    fixture used to pin every player's `passing_vision`/`passing_accuracy`
    to 50 (below that anchor's low end) specifically so the multiplier
    stayed neutral and this test kept isolating only the touch_share axis.
    But `make_sim_player`'s own per-player-distinct default ratings hand
    the no-role `bench_1` (`player_id=2`) a real `passing_vision`/
    `passing_accuracy` of 83/84 -- a good-but-not-elite passing score, not
    a pinned/neutral one -- and at that score the pre-dampening multiplier
    (~1.67x) pulled `bench_1`'s weight from 6.67 up to 11.13, enough on its
    own to flip `star_share > bench_1_share + bench_2_share` to false
    (critic confirmed this directly: removing the pin below made this
    test's own third assertion fail on `main` before the dampening fix).
    The ratings pin is REMOVED below, not restored -- the usage-dampening
    term `_ball_handler_passing_concentration` now applies (see
    `SetupTuning`'s own comment) is what has to keep this invariant true at
    a realistic, non-neutral passing spread, so pinning ratings back to 50
    would hide a regression on exactly this axis if the dampening term
    were ever weakened or removed."""
    star = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        with_role=True,
        touch_share="0.28",
        overall_cached=85,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    bench_1 = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        primary_position=Position.SG,
        with_role=False,
        overall_cached=48,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    bench_2 = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        primary_position=Position.SF,
        with_role=False,
        overall_cached=42,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    role_player_1 = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PF,
        with_role=True,
        touch_share="0.18",
        overall_cached=70,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    role_player_2 = make_sim_player(
        5,
        tuning=DEFAULT_TUNING,
        primary_position=Position.C,
        with_role=True,
        touch_share="0.15",
        overall_cached=65,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    lineup = (star, bench_1, bench_2, role_player_1, role_player_2)

    trials = 20_000
    rng = Random(4242)
    counts = {p.id: 0 for p in lineup}
    for _ in range(trials):
        assigned = assign_offense_roles(ActionType.PNR_BALL, lineup, DEFAULT_TUNING, rng)
        counts[assigned[RoleSlot.BALL_HANDLER]] += 1

    star_share = counts[star.id] / trials
    bench_1_share = counts[bench_1.id] / trials
    bench_2_share = counts[bench_2.id] / trials

    assert star_share > bench_1_share, (star_share, bench_1_share)
    assert star_share > bench_2_share, (star_share, bench_2_share)
    assert star_share > bench_1_share + bench_2_share, (
        "the 85-overall touch_share=0.28 starter should out-draw the two "
        "roleless bench players combined, not the reverse",
        star_share,
        bench_1_share,
        bench_2_share,
    )


def test_a_good_but_not_elite_low_usage_passer_does_not_out_draw_a_real_touch_share_star():
    """B3.1 regression (critic-proven, this dispatch), isolated to exactly
    the one mechanism that broke it: a single roleless bench player with a
    real (not pinned/neutral, not elite-anchor-maxing) 83-composite passing
    score -- `player_id=2`'s actual `make_sim_player` default, the same
    value critic traced the regression to in the fixture above -- must not
    out-draw a real 85-overall, 0.28-`touch_share` starter with an average
    (50-composite) passing score, even though the bench player's better
    passing pulls a real, non-1.0 multiplier. Direct `_role_weight` compare
    (no RNG/trial noise) rather than the Monte Carlo test above, matching
    this file's own established pattern (`test_screener_weight_includes_
    the_screen_setting_ability_term`) for a two-player weight-ordering
    claim that doesn't need a tabulated draw share."""
    star = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        with_role=True,
        touch_share="0.28",
        overall_cached=85,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    good_passer_bench = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        primary_position=Position.SG,
        with_role=False,
        overall_cached=48,
        tendencies_overrides={"pnr_ball_freq": 50},
    )
    bench_passing_score = (
        0.6 * good_passer_bench.ratings.passing_vision
        + 0.4 * good_passer_bench.ratings.passing_accuracy
    )
    assert bench_passing_score > 65.0, (
        "this test only proves what it claims to if the bench player's passing score "
        "actually clears the anchor's low end and pulls a non-1.0 multiplier"
    )
    star_weight = _role_weight(RoleSlot.BALL_HANDLER, star, ActionType.PNR_BALL, DEFAULT_TUNING)
    bench_weight = _role_weight(
        RoleSlot.BALL_HANDLER, good_passer_bench, ActionType.PNR_BALL, DEFAULT_TUNING
    )
    assert star_weight > bench_weight, (star_weight, bench_weight)


def test_elite_passer_out_draws_an_equal_touch_share_average_passer_for_ball_handler():
    """Assist-compression fix regression (season-level analysis, this
    dispatch): before this fix, `_role_weight`'s `BALL_HANDLER` branch read
    only `pnr_ball_freq`/`iso_freq`/`transition_freq` and `touch_share` --
    `passing_vision`/`passing_accuracy` had zero influence on selection
    frequency, which is what let two players with identical touch_share
    and identical ball-handler tendency draw the exact same weight
    regardless of passing skill (directly confirmed: `_role_weight`
    returned byte-identical values for a 99/99-passing and a 50/50-passing
    player before this fix). `touch_share`/`pnr_ball_freq` are pinned
    identical here so passing ability is the only thing left that can move
    either player's draw share."""
    elite_passer = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        with_role=True,
        touch_share="0.30",
        overall_cached=90,
        tendencies_overrides={"pnr_ball_freq": 70},
        ratings_overrides={"passing_vision": 99, "passing_accuracy": 99},
    )
    avg_passer = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        primary_position=Position.SG,
        with_role=True,
        touch_share="0.30",
        overall_cached=90,
        tendencies_overrides={"pnr_ball_freq": 70},
        ratings_overrides={"passing_vision": 50, "passing_accuracy": 50},
    )
    filler = tuple(
        make_sim_player(
            10 + i,
            tuning=DEFAULT_TUNING,
            primary_position=p,
            with_role=True,
            touch_share="0.15",
            overall_cached=65,
            tendencies_overrides={"pnr_ball_freq": 70},
            ratings_overrides={"passing_vision": 50, "passing_accuracy": 50},
        )
        for i, p in enumerate((Position.SF, Position.PF, Position.C))
    )
    lineup = (elite_passer, avg_passer, *filler)

    trials = 20_000
    rng = Random(2468)
    counts = {p.id: 0 for p in lineup}
    for _ in range(trials):
        assigned = assign_offense_roles(ActionType.PNR_BALL, lineup, DEFAULT_TUNING, rng)
        counts[assigned[RoleSlot.BALL_HANDLER]] += 1

    elite_share = counts[elite_passer.id] / trials
    avg_share = counts[avg_passer.id] / trials
    assert elite_share > avg_share, (elite_share, avg_share)


def test_ball_handler_passing_ability_does_not_move_the_no_ball_handler_passer_role():
    """The fix is deliberately scoped to `RoleSlot.BALL_HANDLER` only (see
    `SetupTuning`'s own comment for why) -- `PASSER`/`ENTRY_PASSER` must
    keep reading `pass_freq` * `touch_share` alone, unaffected by
    `passing_vision`/`passing_accuracy`, or a future change that widens
    the ability term's scope silently changes off-ball assist volume too."""
    elite_passer = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"passing_vision": 99, "passing_accuracy": 99},
    )
    avg_passer = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"passing_vision": 50, "passing_accuracy": 50},
        tendencies_overrides={"pass_freq": elite_passer.tendencies.pass_freq},
    )
    elite_weight = _role_weight(RoleSlot.PASSER, elite_passer, ActionType.SPOT_UP, DEFAULT_TUNING)
    avg_weight = _role_weight(RoleSlot.PASSER, avg_passer, ActionType.SPOT_UP, DEFAULT_TUNING)
    assert elite_weight == pytest.approx(avg_weight)


# B2 regression: critic proved this file's tendency-mapping tables
# (`_ROLE_TENDENCY_FIELD`/`_ACTION_TYPE_TENDENCY_FIELD`/
# `_BALL_HANDLER_TENDENCY_FIELD`) and the screener's `screen_setting`
# ability term all survived 4 independent field-swap/term-removal
# mutations against the full suite -- not because the mutations are
# benign, but because `make_tendencies`' old byte-identical fixture made
# every mapped field indistinguishable from every other. Now that fixture
# players carry pairwise-distinct tendencies/ratings (B2 fix,
# `tests/fixtures/sim.py`), these tests target the private mapping
# functions directly with hardcoded expected-field assertions -- not
# derived from the module's own mapping dicts (that would be tautological
# and immune to exactly the mutations this is meant to catch), but from
# Decision P1-8's documented field choices, so a swap in the module's
# table diverges from this file's independent expectation.
def test_role_weight_reads_the_documented_tendency_field_for_each_non_special_role():
    expected_field_by_role = {
        RoleSlot.WEAK_SIDE_SHOOTER: "spot_up_freq",
        RoleSlot.CORNER_SHOOTER: "spot_up_freq",
        RoleSlot.SHOOTER: "spot_up_freq",
        RoleSlot.PASSER: "pass_freq",
        RoleSlot.ENTRY_PASSER: "pass_freq",
        RoleSlot.POST_SCORER: "shot_freq_post",
        RoleSlot.CUTTER: "cut_freq",
        RoleSlot.TRAILER: "transition_freq",
        RoleSlot.WING_RUNNER: "transition_freq",
    }
    player = make_sim_player(1, tuning=DEFAULT_TUNING)
    # RC-20 (docs/design/auto-gm-role-construction.md): `_role_weight`'s
    # `scale` no longer reads a role-carrying player's real `touch_share_f`
    # -- it is unconditionally `_overall_to_touch_share_estimate(overall_
    # cached)`, floored at `min_overall_scale`, for every player.
    scale = max(
        _overall_to_touch_share_estimate(player.player.overall_cached, DEFAULT_TUNING),
        DEFAULT_TUNING.setup.min_overall_scale,
    )
    for role, field_name in expected_field_by_role.items():
        expected = getattr(player.tendencies, field_name) * scale
        actual = _role_weight(role, player, ActionType.SPOT_UP, DEFAULT_TUNING)
        assert actual == pytest.approx(expected), (role, field_name)


def test_role_weight_reads_the_documented_tendency_field_for_the_ball_handler_role():
    expected_field_by_action_type = {
        ActionType.PNR_BALL: "pnr_ball_freq",
        ActionType.DHO: "pnr_ball_freq",
        ActionType.ISO: "iso_freq",
        ActionType.TRANSITION: "transition_freq",
    }
    player = make_sim_player(1, tuning=DEFAULT_TUNING)
    # RC-20: same `scale` correction as the non-special-role test above.
    # Player 1's default passing ratings sit at/below `ball_handler_passing_
    # ability_anchor_low`, so `_ball_handler_passing_concentration` is a
    # 1.0x no-op here and does not need to appear in `expected`.
    scale = max(
        _overall_to_touch_share_estimate(player.player.overall_cached, DEFAULT_TUNING),
        DEFAULT_TUNING.setup.min_overall_scale,
    )
    for action_type, field_name in expected_field_by_action_type.items():
        expected = getattr(player.tendencies, field_name) * scale
        actual = _role_weight(RoleSlot.BALL_HANDLER, player, action_type, DEFAULT_TUNING)
        assert actual == pytest.approx(expected), (action_type, field_name)


def test_action_type_weights_read_the_documented_tendency_field_per_action_type():
    expected_field_by_action_type = {
        ActionType.PNR_BALL: "pnr_ball_freq",
        ActionType.DHO: "pnr_ball_freq",
        ActionType.ISO: "iso_freq",
        ActionType.POST_UP: "shot_freq_post",
        ActionType.OFF_BALL_SCREEN: "cut_freq",
        ActionType.TRANSITION: "transition_freq",
        ActionType.SPOT_UP: "spot_up_freq",
    }
    lineup = _lineup(1)
    weights = _action_type_weights(lineup, DEFAULT_TUNING)
    for action_type, field_name in expected_field_by_action_type.items():
        tendency_mean = sum(getattr(p.tendencies, field_name) for p in lineup) / len(lineup)
        base = DEFAULT_TUNING.setup.action_type_base_weight[action_type.value] * max(
            tendency_mean, 0.0
        )
        expected = (
            base * DEFAULT_TUNING.setup.dho_share_of_pnr if action_type is ActionType.DHO else base
        )
        assert weights[action_type] == pytest.approx(expected), (action_type, field_name)


def test_screener_weight_includes_the_screen_setting_ability_term():
    high_setting = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={"pnr_screen_freq": 50},
        ratings_overrides={"screen_setting": 90},
    )
    low_setting = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={"pnr_screen_freq": 50},
        ratings_overrides={"screen_setting": 10},
    )
    w_high = _role_weight(RoleSlot.SCREENER, high_setting, ActionType.PNR_BALL, DEFAULT_TUNING)
    w_low = _role_weight(RoleSlot.SCREENER, low_setting, ActionType.PNR_BALL, DEFAULT_TUNING)
    assert w_high > w_low, "removing screen_setting from the screener weight would make these equal"
    # RC-20: both fixtures share the same default `overall_cached` (60), so
    # `scale` is identical for both and is no longer `touch_share_f`.
    scale = max(
        _overall_to_touch_share_estimate(high_setting.player.overall_cached, DEFAULT_TUNING),
        DEFAULT_TUNING.setup.min_overall_scale,
    )
    assert w_high == pytest.approx(50 * (90 / 99.0) * scale)
    assert w_low == pytest.approx(50 * (10 / 99.0) * scale)


def test_usage_mode_moves_cross_family_ratios_but_not_within_family_ratios():
    """PD-5, mutations 13/13b -- the actual proof that `usage_mode` and
    `touch_share` are different knobs (revision 1's "different operands of
    one product" proof was vacuous; multiplication commutes). One fixture
    player, `cut_freq != pass_freq` so `CUTTER`/`PASSER` (both `movement`)
    have a non-trivial ratio that would trivially hold at 1.0 otherwise.

    (a) Cross-family: `BALL_HANDLER` (`on_ball`) vs `CUTTER` (`movement`)
    must move by roughly the `initiate` family factor (on_ball 1.3x) when
    switching from `balanced` -- `usage_mode`'s whole reason to exist.

    (b) Within-family: `CUTTER` vs `PASSER` (both `movement`) must stay
    equal to a tight relative tolerance across every `UsageMode` value --
    the family factor cancels out of the ratio exactly (up to float-ulp
    reassociation noise, PD-5's own honesty note on how tightly this can
    be asserted)."""

    def _weight(role: RoleSlot, usage_mode) -> float:
        directed = make_sim_player(
            1,
            tuning=DEFAULT_TUNING,
            tendencies_overrides={"pnr_ball_freq": 60, "cut_freq": 45, "pass_freq": 20},
            directive=make_directive(1, usage_mode=usage_mode),
        )
        return _role_weight(role, directed, ActionType.PNR_BALL, DEFAULT_TUNING)

    balanced_ratio = _weight(RoleSlot.BALL_HANDLER, UsageMode.BALANCED) / _weight(
        RoleSlot.CUTTER, UsageMode.BALANCED
    )
    initiate_ratio = _weight(RoleSlot.BALL_HANDLER, UsageMode.INITIATE) / _weight(
        RoleSlot.CUTTER, UsageMode.INITIATE
    )
    relative_move = abs(initiate_ratio - balanced_ratio) / balanced_ratio
    assert relative_move >= 0.10, (
        "usage_mode=initiate must move the on_ball-vs-movement weight ratio by "
        f"at least 10% -- got {relative_move:.4f}"
    )

    baseline_ratio = _weight(RoleSlot.CUTTER, UsageMode.BALANCED) / _weight(
        RoleSlot.PASSER, UsageMode.BALANCED
    )
    for usage_mode in UsageMode:
        cutter_passer_ratio = _weight(RoleSlot.CUTTER, usage_mode) / _weight(
            RoleSlot.PASSER, usage_mode
        )
        assert cutter_passer_ratio == pytest.approx(baseline_ratio, rel=1e-12), (
            f"within-family (movement) ratio moved under usage_mode={usage_mode!r}"
        )


def test_touch_share_cannot_move_any_role_weight_ratio_including_ball_handler():
    """RC-20 (`docs/design/auto-gm-role-construction.md`), superseding the
    version of this test the PD-5 critic round shipped: that version proved
    `touch_share` genuinely could move the BALL_HANDLER-vs-anything ratio,
    at a high enough passing score, via `_ball_handler_passing_concentration`'s
    `usage_ratio` term reading the same `touch_share`-or-`overall_cached`
    scale `_role_weight` itself multiplied by. RC-20 deletes that reader
    (and `_role_weight`'s own `touch_share_f` reader) outright --
    `player_roles.touch_share` now means exactly one thing, league-wide (a
    raw share of a team's 240 minutes, read only by `game.py::_minutes_
    shares`), and reading it here duplicated a genuine unit collision RC-2
    names. So the invariant is no longer narrowed to "every pair except
    BALL_HANDLER" -- it is total: **no** role-weight ratio, BALL_HANDLER
    included, moves with `touch_share`, even at the passing score (98/98)
    that used to trigger the exception."""

    def _weight(role: RoleSlot, touch_share: str) -> float:
        player = make_sim_player(
            1,
            tuning=DEFAULT_TUNING,
            touch_share=touch_share,
            tendencies_overrides={"pnr_ball_freq": 60, "cut_freq": 45, "pass_freq": 20},
            ratings_overrides={"passing_vision": 98, "passing_accuracy": 98},
        )
        return _role_weight(role, player, ActionType.PNR_BALL, DEFAULT_TUNING)

    # Even BALL_HANDLER -- the one role that used to be the exception --
    # is now untouched by touch_share.
    low_bh_ratio = _weight(RoleSlot.BALL_HANDLER, "0.10") / _weight(RoleSlot.CUTTER, "0.10")
    high_bh_ratio = _weight(RoleSlot.BALL_HANDLER, "0.35") / _weight(RoleSlot.CUTTER, "0.35")
    assert low_bh_ratio == pytest.approx(high_bh_ratio, rel=1e-9)

    # A ratio between two roles that are both outside BALL_HANDLER was
    # already untouched before RC-20 and stays that way.
    low_ratio = _weight(RoleSlot.CUTTER, "0.10") / _weight(RoleSlot.PASSER, "0.10")
    high_ratio = _weight(RoleSlot.CUTTER, "0.35") / _weight(RoleSlot.PASSER, "0.35")
    assert low_ratio == pytest.approx(high_ratio, rel=1e-9)


def test_a_role_carrying_player_and_an_identical_roleless_player_get_the_same_role_weight():
    """RC-20.5's own acceptance test (`docs/design/auto-gm-role-construction.md`):
    fails on HEAD (pre-RC-20), where a role-carrying player's `_role_weight`
    scale reads their real `touch_share_f` and a byte-identical roleless
    teammate's falls back to `_overall_to_touch_share_estimate(overall_
    cached)` instead -- two different numbers for the same `overall_cached`
    unless `touch_share_f` happens to equal the estimate exactly. After
    RC-20 both paths read `_overall_to_touch_share_estimate` unconditionally,
    so the two players -- identical in every field except whether a
    `player_roles` row exists -- must get the exact same weight for every
    role."""
    # Same `player_id` for both (fixture ratings/tendencies are seeded from
    # `player_id`, `tests/fixtures/sim.py`'s own `_distinct_ratings`), so the
    # only difference between the two `SimPlayer`s is whether a
    # `player_roles` row exists at all.
    with_role = make_sim_player(1, tuning=DEFAULT_TUNING, overall_cached=75, with_role=True)
    roleless = make_sim_player(1, tuning=DEFAULT_TUNING, overall_cached=75, with_role=False)

    for template in ROLE_TEMPLATES.values():
        for role in template.offense:
            action_type = next(
                at for at, tpl in ROLE_TEMPLATES.items() if role in tpl.offense
            )
            with_role_weight = _role_weight(role, with_role, action_type, DEFAULT_TUNING)
            roleless_weight = _role_weight(role, roleless, action_type, DEFAULT_TUNING)
            assert with_role_weight == pytest.approx(roleless_weight, rel=1e-12), (
                f"role={role!r} action_type={action_type!r}: role-carrying weight "
                f"{with_role_weight} != roleless weight {roleless_weight}"
            )


def test_role_family_map_is_total_over_every_roleslot_role_weight_is_called_with():
    """Mutation 14's role-family half, restated as an ordinary test (the
    same fact `_require_role_family_map_is_total_over_offensive_roleslots`
    enforces at import) -- every role any `ROLE_TEMPLATES` entry's
    `offense` tuple names must resolve through `_role_weight` without a
    `KeyError`."""
    player = make_sim_player(1, tuning=DEFAULT_TUNING)
    for template in ROLE_TEMPLATES.values():
        for role in template.offense:
            action_type = next(
                at for at, tpl in ROLE_TEMPLATES.items() if role in tpl.offense
            )
            _role_weight(role, player, action_type, DEFAULT_TUNING)  # must not raise


# PD-10 (docs/design/gm-command-surface.md) -- `offensive_scheme`'s
# multiplicative tilt on `_action_type_weights`. Mutations 18-22b.


def _normalized_action_type_weights(lineup, offensive_scheme):
    weights = _action_type_weights(lineup, DEFAULT_TUNING, offensive_scheme=offensive_scheme)
    total = sum(weights.values())
    return {at: w / total for at, w in weights.items()}


def test_offensive_scheme_multiplies_weights_and_lifts_each_scheme_own_signature_action():
    """Mutation 18: a fixed lineup's normalized action-type weight vector
    differs under `iso_star` vs `post_up`, and each scheme's own signature
    action carries a strictly larger normalized weight under that scheme
    than under no scheme at all. Pure-function, no RNG -- the
    distribution-level version is mutation 22's job (test_possession.py/
    test_game.py)."""
    lineup = _lineup(1)
    neutral = _normalized_action_type_weights(lineup, None)
    iso_star = _normalized_action_type_weights(lineup, OffensiveScheme.ISO_STAR)
    post_up = _normalized_action_type_weights(lineup, OffensiveScheme.POST_UP)
    five_out = _normalized_action_type_weights(lineup, OffensiveScheme.FIVE_OUT)

    assert iso_star != post_up
    assert iso_star[ActionType.ISO] > neutral[ActionType.ISO]
    assert post_up[ActionType.POST_UP] > neutral[ActionType.POST_UP]
    assert five_out[ActionType.SPOT_UP] > neutral[ActionType.SPOT_UP]


def test_a_missing_offensive_scheme_argument_is_byte_identical_to_none():
    """Mutation 22b's local half (PD-10.4): `offensive_scheme` defaults to
    `None`, and `None` means no scheme factor is applied at all -- not a
    neutral-row lookup. The full byte-identity proof over a real possession
    lives in test_possession.py/test_block_exposure.py's untouched fixtures
    (acceptance criterion 6a); this pins the same fact at the pure-function
    layer."""
    lineup = _lineup(1)
    assert _action_type_weights(lineup, DEFAULT_TUNING) == _action_type_weights(
        lineup, DEFAULT_TUNING, offensive_scheme=None
    )


def test_resolve_setup_beats_record_matches_the_scheme_weighted_draw_when_a_scheme_is_given():
    """Mutation 19 (PD-10.1's named trap, direction 1): `resolve_setup_beat`
    must forward `offensive_scheme` into its `probabilities` recompute, not
    just the draw -- the emitted `BeatRecord.probabilities` must equal the
    normalized scheme-weighted vector for the same lineup and scheme, to
    `rel=1e-12`."""
    offense = _lineup(1)
    defense = _lineup(11)
    scheme = OffensiveScheme.FIVE_OUT
    expected = {
        at.value: w
        for at, w in _normalized_action_type_weights(offense, scheme).items()
    }

    result = resolve_setup_beat(
        offense,
        defense,
        DEFAULT_TUNING,
        Random(7),
        clock_seconds=24.0,
        home_score=0,
        away_score=0,
        offensive_scheme=scheme,
    )
    for action_value, expected_share in expected.items():
        assert result.record.probabilities[action_value] == pytest.approx(
            expected_share, rel=1e-12
        ), action_value

    # Distinguishes "the record was forwarded the scheme" from "the record
    # happens to look right by coincidence": the neutral (no-scheme)
    # probabilities must be measurably different on this fixture.
    neutral = {at.value: w for at, w in _normalized_action_type_weights(offense, None).items()}
    assert any(
        abs(result.record.probabilities[k] - neutral[k]) > 1e-6 for k in neutral
    ), "scheme-weighted and neutral probabilities coincided -- this fixture proves nothing"


def test_resolve_setup_beats_draw_matches_the_scheme_weighted_distribution_it_records():
    """Mutation 19 (PD-10.1's named trap, direction 2 -- critic round 1's
    correction): forwarding the scheme into the `probabilities` recompute
    while leaving the DRAW un-forwarded produces the opposite lie (an
    honest record, a wrong draw) -- not caught by the direct-equality test
    above, which only inspects the stored record. Tabulates the actual
    action-type draws `resolve_setup_beat` makes over many seeds and
    compares the empirical share against the scheme's own normalized
    weight for its signature action (`post_up`'s 2.20 multiplier is
    discriminating enough on one fixed lineup to make this a real check,
    not a coin flip)."""
    offense = _lineup(1)
    defense = _lineup(11)
    scheme = OffensiveScheme.POST_UP
    expected_share = _normalized_action_type_weights(offense, scheme)[ActionType.POST_UP]

    trials = 3000
    draws = [
        resolve_setup_beat(
            offense,
            defense,
            DEFAULT_TUNING,
            Random(seed),
            clock_seconds=24.0,
            home_score=0,
            away_score=0,
            offensive_scheme=scheme,
        ).action_type
        for seed in range(trials)
    ]
    empirical_share = draws.count(ActionType.POST_UP) / trials
    assert empirical_share == pytest.approx(expected_share, abs=0.03)


def test_every_pair_of_schemes_produces_a_different_normalized_weight_vector():
    """Mutation 20: collapsing the per-scheme row lookup to one shared row
    (the PD-5 mutation-13 shape) would make every scheme produce the same
    normalized vector. All ten pairs checked, not a favourite two, so a
    mutation that merges only two of the five rows still fails."""
    lineup = _lineup(1)
    vectors = {
        scheme: _normalized_action_type_weights(lineup, scheme) for scheme in OffensiveScheme
    }
    schemes = list(OffensiveScheme)
    for i, scheme_a in enumerate(schemes):
        for scheme_b in schemes[i + 1 :]:
            assert vectors[scheme_a] != vectors[scheme_b], (
                f"{scheme_a} and {scheme_b} produced identical normalized action-type weights"
            )


def _tuning_with_offensive_scheme_table(table):
    setup = dataclasses.replace(DEFAULT_TUNING.setup, offensive_scheme_action_multipliers=table)
    return dataclasses.replace(DEFAULT_TUNING, setup=setup)


def test_offensive_scheme_pin_raises_when_a_scheme_member_is_missing():
    """Mutation 21, half 1: constructing the broken profile via
    `dataclasses.replace` and calling the pin directly, per the design
    doc's own instruction for this mutation."""
    table = dict(DEFAULT_TUNING.setup.offensive_scheme_action_multipliers)
    del table[OffensiveScheme.FIVE_OUT]
    tuning = _tuning_with_offensive_scheme_table(table)
    with pytest.raises(RuntimeError, match="five_out"):
        _require_offensive_scheme_action_multipliers_are_total_and_positive(tuning)


def test_offensive_scheme_pin_raises_when_an_action_type_key_is_missing_and_names_it():
    """Mutation 21, half 2 -- the totality-source proof: the required key
    set is derived from `ActionType` itself (`{a.value for a in
    ActionType}`), never a hardcoded list, so a table missing exactly one
    action-type key must name THAT key in the failure message."""
    table = {
        scheme: dict(row)
        for scheme, row in DEFAULT_TUNING.setup.offensive_scheme_action_multipliers.items()
    }
    del table[OffensiveScheme.PNR_HEAVY]["spot_up"]
    tuning = _tuning_with_offensive_scheme_table(table)
    with pytest.raises(RuntimeError, match="spot_up"):
        _require_offensive_scheme_action_multipliers_are_total_and_positive(tuning)


def test_offensive_scheme_pin_raises_when_a_cell_is_zero():
    """Mutation 21, half 3: PD-10.3's strict-positivity rule (the identical
    RNG-desync reason PD-2 disclosure 3 gives)."""
    table = {
        scheme: dict(row)
        for scheme, row in DEFAULT_TUNING.setup.offensive_scheme_action_multipliers.items()
    }
    table[OffensiveScheme.FIVE_OUT]["post_up"] = 0.0
    tuning = _tuning_with_offensive_scheme_table(table)
    with pytest.raises(RuntimeError, match="strictly positive"):
        _require_offensive_scheme_action_multipliers_are_total_and_positive(tuning)
