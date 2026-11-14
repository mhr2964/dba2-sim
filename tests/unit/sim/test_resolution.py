"""Tier 1: monotonicity for beat 5 (zone selection, contest, make/miss,
foul, turnover) plus Decision P1-14 closed-set exhaustiveness over a
seeded sample."""
from __future__ import annotations

import dataclasses
import hashlib
from collections import Counter
from random import Random

import pytest

from dba2.domain.role import ShotDiet
from dba2.sim.beats.resolution import (
    _pop_zone_and_shot_inputs,
    _three_point_bias_scale,
    resolve_resolution_beat,
    select_shot_zone,
)
from dba2.sim.composites import COMMITTED_HELPER_PENALTY, aggression_linear_scale
from dba2.sim.events import TerminalKind, Zone
from dba2.sim.playbook import ActionType
from dba2.sim.probability import ability_score, contest_probability
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_directive, make_sim_player

_RESOLUTION_ELIGIBLE_KINDS = {
    TerminalKind.MADE_FG,
    TerminalKind.MISSED_FG,
    TerminalKind.TURNOVER_LIVE,
    TerminalKind.TURNOVER_DEAD,
    TerminalKind.SHOOTING_FOUL,
    TerminalKind.NON_SHOOTING_FOUL,
    TerminalKind.OFFENSIVE_FOUL,
    # docs/design/turnover-recovery.md TR-5.B.15: reachable only when the
    # caller opts in with a positive recovery probability -- the default
    # call below omits the leaf entirely, which is why this literal needs
    # the opted-in twin test further down to be anything but decoration.
    TerminalKind.LOOSE_BALL_RECOVERED,
}

# The ten leaf names `resolve_resolution_beat` has always offered, in the
# order its dict builds them. TR-5.B.15(b): this file had no leaf census at
# all, so a new leaf could appear (or a zero-weight key be inserted) with
# nothing here noticing.
_DEFAULT_LEAVES = (
    "turnover_live",
    "turnover_dead",
    "offensive_foul",
    "and_one",
    "shooting_foul",
    "non_shooting_foul",
    "blocked_miss",
    "blocked_miss_help",
    "made",
    "missed_unblocked",
)


def _resolve(shooter, defender, seed=0, **kwargs):
    return resolve_resolution_beat(
        shooter,
        defender,
        action_type=ActionType.ISO,
        tuning=DEFAULT_TUNING,
        rng=Random(seed),
        clock_seconds=14.0,
        home_score=20,
        away_score=18,
        **kwargs,
    )


def _candidates() -> tuple:
    """A five-man offense for the recovery leaf's candidates (TR-4.7): the
    shooter's id 1 is among them, as in a real possession, and their
    loose-ball scores differ so a draw is distinguishable from an argmax."""
    return tuple(
        make_sim_player(
            pid,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"motor": r, "hands": r, "basketball_iq": r},
        )
        for pid, r in ((1, 50), (3, 90), (5, 70), (6, 60), (7, 40))
    )


def test_leaf_probabilities_always_sum_to_one_and_stay_in_bounds():
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    result = _resolve(shooter, defender)
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-6
    for p in result.probabilities.values():
        assert -1e-9 <= p <= 1.0 + 1e-9


def test_outcome_is_always_a_resolution_eligible_terminal_kind():
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    for seed in range(200):
        result = _resolve(shooter, defender, seed=seed)
        assert result.outcome.kind in _RESOLUTION_ELIGIBLE_KINDS


def test_elite_shooter_makes_more_often_than_replacement_level():
    elite = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "finishing_rim": 95,
            "catch_shoot_mid": 92,
            "off_dribble_mid": 92,
            "catch_shoot_3": 90,
            "off_dribble_3": 90,
        },
        tendencies_overrides={
            "shot_freq_rim": 40,
            "shot_freq_mid": 30,
            "shot_freq_three": 26,
            "shot_freq_post": 4,
        },
    )
    poor = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "finishing_rim": 10,
            "catch_shoot_mid": 12,
            "off_dribble_mid": 8,
            "catch_shoot_3": 10,
            "off_dribble_3": 8,
        },
        tendencies_overrides={
            "shot_freq_rim": 40,
            "shot_freq_mid": 30,
            "shot_freq_three": 26,
            "shot_freq_post": 4,
        },
    )
    defender = make_sim_player(3, tuning=DEFAULT_TUNING)

    makes_high = sum(
        1
        for seed in range(300)
        if _resolve(elite, defender, seed=seed).outcome.kind is TerminalKind.MADE_FG
    )
    makes_low = sum(
        1
        for seed in range(300)
        if _resolve(poor, defender, seed=seed).outcome.kind is TerminalKind.MADE_FG
    )
    assert makes_high > makes_low


def test_elite_rim_protector_blocks_more_often_than_a_poor_one():
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 100,
            "shot_freq_mid": 0,
            "shot_freq_three": 0,
            "shot_freq_post": 0,
        },
    )
    elite_blocker = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "rim_protection": 95,
            "vertical": 92,
            "lateral_quickness": 88,
        },
    )
    poor_blocker = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "rim_protection": 10,
            "vertical": 12,
            "lateral_quickness": 8,
        },
    )
    blocks_high = sum(
        1
        for seed in range(300)
        if _resolve(shooter, elite_blocker, seed=seed).outcome.blocked
    )
    blocks_low = sum(
        1 for seed in range(300) if _resolve(shooter, poor_blocker, seed=seed).outcome.blocked
    )
    assert blocks_high > blocks_low


def test_high_foul_freq_defender_draws_more_fouls_than_a_disciplined_one():
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    gambler = make_sim_player(
        2, tuning=DEFAULT_TUNING, tendencies_overrides={"foul_freq": 95}
    )
    disciplined = make_sim_player(
        3, tuning=DEFAULT_TUNING, tendencies_overrides={"foul_freq": 5}
    )
    foul_kinds = {
        TerminalKind.SHOOTING_FOUL,
        TerminalKind.NON_SHOOTING_FOUL,
    }

    def _is_foul(result):
        return result.outcome.kind in foul_kinds or (
            result.outcome.kind is TerminalKind.MADE_FG and result.outcome.and_one
        )

    fouls_high = sum(1 for seed in range(300) if _is_foul(_resolve(shooter, gambler, seed=seed)))
    fouls_low = sum(
        1 for seed in range(300) if _is_foul(_resolve(shooter, disciplined, seed=seed))
    )
    assert fouls_high > fouls_low


def test_elite_perimeter_closeout_defender_allows_a_lower_three_point_pct_than_a_poor_one():
    """B25-2 fix (critic-round-25-proven), correct-direction regression:
    the three-point contest used to reuse `resolution_block.defense_weights`
    (rim_protection/vertical/wingspan_in/lateral_quickness), a rim-contest
    skill set that sign-inverted this beat for threes specifically -- a
    drop-coverage rim anchor with zero closeout speed measured as the
    *best* three-point defender in the engine (critic: 18.4% allowed vs.
    47.0% for a genuine perimeter stopper, backwards). This test would
    have failed against that old wiring (the `elite_closeout` archetype
    below scores worse on the old rim-defense vector than `poor_closeout`
    does -- confirmed by hand against `resolution_block.defense_weights`
    before this fix landed) and now asserts the correct real-basketball
    direction: a defender with elite `closeout_speed`/`closeout_control`
    (and weak rim skills, so the old wiring would have called them a poor
    defender) allows a LOWER three-point percentage than one built the
    opposite way."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 70, "off_dribble_3": 70},
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 0,
            "shot_freq_three": 100,
            "shot_freq_post": 0,
        },
    )
    elite_closeout = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        wingspan_in=68,
        ratings_overrides={
            "closeout_speed": 95,
            "closeout_control": 92,
            "rim_protection": 10,
            "vertical": 12,
            "lateral_quickness": 15,
        },
    )
    poor_closeout = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        wingspan_in=93,
        ratings_overrides={
            "closeout_speed": 8,
            "closeout_control": 10,
            "rim_protection": 95,
            "vertical": 92,
            "lateral_quickness": 90,
        },
    )

    def _three_pct(defender) -> float:
        makes = sum(
            1
            for seed in range(600)
            if _resolve(shooter, defender, seed=seed).outcome.kind is TerminalKind.MADE_FG
        )
        return makes / 600

    pct_elite = _three_pct(elite_closeout)
    pct_poor = _three_pct(poor_closeout)
    assert pct_elite < pct_poor, (
        f"elite closeout defender allowed {pct_elite:.3f} 3PT%, poor closeout allowed "
        f"{pct_poor:.3f} -- the three-point contest is still reading the wrong skill set "
        "(or is sign-inverted again)"
    )


def _elite_rim_help_defender(player_id: int, *, level: int = 95):
    """Elite on every field any rim-help vector reads, so the fixture is the
    floor's best helper whichever vector `rim_protection_contribution` uses."""
    return make_sim_player(
        player_id,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "rotation_speed": level,
            "help_iq": level,
            "rim_protection": level,
            "vertical": level,
        },
        wingspan_in=94,
    )


def _weak_helper(player_id: int):
    return make_sim_player(
        player_id,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "rotation_speed": 55,
            "help_iq": 55,
            "rim_protection": 55,
            "vertical": 55,
        },
        wingspan_in=78,
    )


_RIM_ONLY = {"shot_freq_rim": 100, "shot_freq_mid": 0, "shot_freq_three": 0, "shot_freq_post": 0}


def _rim_shooter(player_id: int = 1):
    return make_sim_player(player_id, tuning=DEFAULT_TUNING, tendencies_overrides=_RIM_ONLY)


def _tuning_with_rim_help(**changes):
    return dataclasses.replace(
        DEFAULT_TUNING,
        resolution_rim_help=dataclasses.replace(DEFAULT_TUNING.resolution_rim_help, **changes),
    )


def _help_block_p(result) -> float:
    """`p_help_block` recovered from the leaf dict: `blocked_miss_help` is
    `p_shot * (1 - p_block) * p_help_block` and `made + missed_unblocked` is
    `p_shot * (1 - p_block) * (1 - p_help_block)`."""
    leaves = result.probabilities
    rest = leaves["made"] + leaves["missed_unblocked"]
    return leaves["blocked_miss_help"] / (leaves["blocked_miss_help"] + rest)


def test_weak_side_rim_help_blocks_only_on_rim_zone_shots():
    """BDA-4.4 gate 1: the weak-side rim-help contest only ever fires on a
    rim-zone shot -- forcing the shooter's whole shot diet to mid (or rim)
    via `shot_freq_*` is the same zone-control technique this file's other
    tests already use (`test_elite_rim_protector_blocks_more_often_than_a_
    poor_one` et al.)."""
    mid_only = {
        "shot_freq_rim": 0,
        "shot_freq_mid": 100,
        "shot_freq_three": 0,
        "shot_freq_post": 0,
    }
    shooter_mid = make_sim_player(1, tuning=DEFAULT_TUNING, tendencies_overrides=mid_only)
    shooter_rim = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    pool = (_elite_rim_help_defender(3),)

    mid_help_leaves = sum(
        1
        for seed in range(300)
        if _resolve(shooter_mid, defender, seed=seed, rim_help_pool=pool).record.outcome
        == "blocked_miss_help"
    )
    assert mid_help_leaves == 0

    rim_help_leaves = sum(
        1
        for seed in range(300)
        if _resolve(shooter_rim, defender, seed=seed, rim_help_pool=pool).record.outcome
        == "blocked_miss_help"
    )
    assert rim_help_leaves > 0


def test_help_pool_excludes_the_primary_defender():
    """BDA-4.4 gate 3 under block-attribution-v2's pool (BA-2.6): the shot's
    own primary defender is never a weak-side helper -- his block is the
    `blocked_miss` leaf -- so a pool containing him gives the same leaf dict
    as the pool without him, and a pool of only him is no pool at all."""
    shooter = _rim_shooter()
    defender = _elite_rim_help_defender(2)
    others = (_weak_helper(3), _elite_rim_help_defender(4, level=85))

    for seed in range(20):
        with_him = _resolve(shooter, defender, seed=seed, rim_help_pool=(defender, *others))
        without_him = _resolve(shooter, defender, seed=seed, rim_help_pool=others)
        assert with_him.probabilities == without_him.probabilities
        only_him = _resolve(shooter, defender, seed=seed, rim_help_pool=(defender,))
        no_pool = _resolve(shooter, defender, seed=seed)
        assert only_him.probabilities == no_pool.probabilities
        assert only_him.probabilities["blocked_miss_help"] == 0.0


def test_empty_pool_is_the_nine_leaf_arithmetic():
    """BA-2.8: `rim_help_pool=()` (the default) is the pre-BDA-4 arithmetic
    exactly -- the `blocked_miss_help` key stays in the census at 0.0 and
    nothing else moves, stream position included."""
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    for seed in range(300):
        a_rng, b_rng = Random(seed), Random(seed)
        a = _resolve_with_rng(shooter, defender, a_rng, rim_help_pool=())
        b = _resolve_with_rng(shooter, defender, b_rng)
        assert (a.outcome, a.record, a.probabilities) == (b.outcome, b.record, b.probabilities)
        assert a.probabilities["blocked_miss_help"] == 0.0
        assert a_rng.getstate() == b_rng.getstate()


def test_help_block_is_the_share_weighted_sum_of_each_helpers_own_contest():
    """BA-2.1/BA-2.2 by hand: shares are `max(c - shift, 1) ** exponent`
    normalized over the pool, each helper contests on his own cached scalar,
    and `p_help_block` is the share-weighted sum times the defense's
    help-aggression scale (at 90, so the scale is not 1.0). The second case
    makes helper 4 the committed helper: his scalar is penalized in both his
    share and his contest."""
    cfg = DEFAULT_TUNING.resolution_rim_help
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    pool = (_elite_rim_help_defender(3), _elite_rim_help_defender(4, level=80), _weak_helper(5))
    block_cfg = DEFAULT_TUNING.resolution_block
    shooter_score = ability_score(shooter, block_cfg.offense_weights)
    aggression = aggression_linear_scale(90, cfg.aggression_lo, cfg.aggression_hi)
    assert aggression != 1.0

    def expected(committed_id):
        scalars = [
            p.rim_protection_contribution
            * (COMMITTED_HELPER_PENALTY if p.id == committed_id else 1.0)
            for p in pool
        ]
        raw = [
            max(c - cfg.concentration_shift, 1.0) ** cfg.attribution_concentration_exponent
            for c in scalars
        ]
        return sum(
            r
            / sum(raw)
            * contest_probability(
                c, shooter_score, base_rate=cfg.base_rate, scale=cfg.scale, band=cfg.band
            )
            * aggression
            for r, c in zip(raw, scalars, strict=True)
        )

    for committed_id in (None, 4):
        result = _resolve(
            shooter,
            defender,
            seed=3,
            rim_help_pool=pool,
            rim_help_committed_id=committed_id,
            help_aggression=90,
        )
        assert result.probabilities["blocked_miss_help"] > 0.0
        assert _help_block_p(result) == pytest.approx(expected(committed_id), rel=1e-9)


def test_help_block_share_collapses_to_max_as_gamma_grows():
    """BA-4.2: today's winner-take-all rule is the limit case. At exponent
    200, `p_help_block` equals the single-best-helper value to 1e-9 and the
    drawn blocker is always him."""
    steep = _tuning_with_rim_help(attribution_concentration_exponent=200.0)
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    best = _elite_rim_help_defender(3)
    pool = (_weak_helper(4), _elite_rim_help_defender(5, level=85), best, _weak_helper(6))

    def resolve(seed, helpers):
        return resolve_resolution_beat(
            shooter,
            defender,
            action_type=ActionType.ISO,
            tuning=steep,
            rng=Random(seed),
            clock_seconds=14.0,
            home_score=20,
            away_score=18,
            rim_help_pool=helpers,
        )

    help_blocks = 0
    for seed in range(1500):
        result = resolve(seed, pool)
        alone = resolve(seed, (best,))
        assert _help_block_p(result) == pytest.approx(_help_block_p(alone), abs=1e-9)
        if result.record.outcome == "blocked_miss_help":
            help_blocks += 1
            assert result.outcome.blocking_defender_id == best.id
            assert result.record.actor_ids["blocked_by"] == best.id
    assert help_blocks > 0


def test_help_block_draw_happens_only_on_the_help_leaf():
    """BA-2.1/BA-2.8 (TR-4.7's shape): the blocker draw is the help leaf's
    own and costs exactly one extra `rng.random()`; every other leaf leaves
    the stream where an empty-pool call on the same seed leaves it."""
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    pool = (_elite_rim_help_defender(3), _elite_rim_help_defender(4, level=88), _weak_helper(5))
    help_leaves = others = 0
    for seed in range(500):
        pool_rng, twin_rng = Random(seed), Random(seed)
        result = _resolve_with_rng(shooter, defender, pool_rng, rim_help_pool=pool)
        _resolve_with_rng(shooter, defender, twin_rng)
        if result.record.outcome == "blocked_miss_help":
            help_leaves += 1
            twin_rng.random()
        else:
            others += 1
            assert "blocked_by" not in result.record.actor_ids
        assert pool_rng.getstate() == twin_rng.getstate(), (seed, result.record.outcome)
    assert help_leaves > 0 and others > 0


def test_the_drawn_help_blocker_follows_each_helpers_share_times_contest():
    """BA-2.1: the blocker is the posterior "who blocked, given a help
    block", so over many help leaves each helper's frequency tracks his
    `share * p` -- both helpers appear (not an argmax), the stronger one
    more often, and every help block names its blocker on the outcome and
    the record alike."""
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    strong = _elite_rim_help_defender(3)
    second = _elite_rim_help_defender(4, level=85)
    counts = Counter()
    for seed in range(6000):
        result = _resolve(shooter, defender, seed=seed, rim_help_pool=(strong, second))
        if result.record.outcome == "blocked_miss_help":
            assert result.outcome.blocking_defender_id == result.record.actor_ids["blocked_by"]
            counts[result.outcome.blocking_defender_id] += 1
    assert counts[strong.id] > counts[second.id] > 0
    assert set(counts) == {strong.id, second.id}


def test_committed_helper_is_penalized_in_share_and_contest():
    """BDA-4.4 gate 5 per member (BA-2.6): the possession's committed helper
    contests on `c * COMMITTED_HELPER_PENALTY`, which lowers both his share
    of the floor and his own contest -- alone in the pool (contest only) and
    beside a second helper (share too) `p_help_block` falls, and so does his
    share of drawn blocks."""
    shooter = _rim_shooter()
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    # Good, not elite: at the shipped steepness an elite helper against the
    # default shooter sits on the band's 0.40 ceiling, where a penalty
    # cannot show.
    helper = _elite_rim_help_defender(3, level=75)
    other = _elite_rim_help_defender(4, level=72)

    alone_free = _resolve(shooter, defender, seed=11, rim_help_pool=(helper,))
    alone_committed = _resolve(
        shooter, defender, seed=11, rim_help_pool=(helper,), rim_help_committed_id=helper.id
    )
    assert _help_block_p(alone_committed) < _help_block_p(alone_free)

    free = _resolve(shooter, defender, seed=11, rim_help_pool=(helper, other))
    committed = _resolve(
        shooter, defender, seed=11, rim_help_pool=(helper, other), rim_help_committed_id=helper.id
    )
    assert _help_block_p(committed) < _help_block_p(free)

    def helper_share_of_blocks(committed_id):
        counts = Counter()
        for seed in range(6000):
            result = _resolve(
                shooter,
                defender,
                seed=seed,
                rim_help_pool=(helper, other),
                rim_help_committed_id=committed_id,
            )
            if result.record.outcome == "blocked_miss_help":
                counts[result.outcome.blocking_defender_id] += 1
        return counts[helper.id] / sum(counts.values())

    assert helper_share_of_blocks(helper.id) < helper_share_of_blocks(None)
    # A committed id that is not in the pool changes nothing.
    stranger = _resolve(
        shooter, defender, seed=11, rim_help_pool=(helper, other), rim_help_committed_id=999
    )
    assert stranger.probabilities == free.probabilities


def test_the_new_rim_help_tuning_fields_exist_by_name():
    """BA-3 concurrency: three in-flight builds touch `tuning.py`, and a
    racing commit once dropped a `TuningProfile` field (SC-7 finding 3). Read
    both new fields by name so a dropped one fails loudly here."""
    cfg = DEFAULT_TUNING.resolution_rim_help
    assert cfg.attribution_concentration_exponent == 4.0
    assert cfg.concentration_shift == 50.0


def test_post_up_action_type_reads_post_scoring_and_reports_mid_zone():
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING, ratings_overrides={"post_scoring": 90})
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    result = resolve_resolution_beat(
        shooter,
        defender,
        action_type=ActionType.POST_UP,
        tuning=DEFAULT_TUNING,
        rng=Random(1),
        clock_seconds=14.0,
        home_score=0,
        away_score=0,
    )
    if result.outcome.zone is not None:
        from dba2.sim.events import Zone

        assert result.outcome.zone is Zone.MID


def test_neutral_three_point_bias_scale_is_exactly_one():
    """PD-2 acceptance criterion 3a -- the same numeric fact the module-
    level `_require_neutral_three_point_bias_scale_is_exactly_one` import-
    time pin already enforces, restated here as an ordinary test so a CI
    failure of the import-time raise is not the only signal (mutation 11)."""
    three_scale, mid_scale = _three_point_bias_scale(50, DEFAULT_TUNING)
    assert three_scale == 1.0
    assert mid_scale == 1.0


def test_three_point_bias_moves_the_zone_distribution_both_directions():
    """Mutation 10 -- `three_point_bias=90` must draw THREE more often,
    and `three_point_bias=10` must draw THREE less often, than the neutral
    (50) distribution, over the same seeded RNG sequence. A shooter with
    equal RIM/MID/THREE tendencies isolates the bias's own effect from any
    tendency-driven skew."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 33,
            "shot_freq_mid": 33,
            "shot_freq_three": 34,
            "shot_freq_post": 0,
        },
    )

    def _three_share(three_point_bias: int) -> float:
        rng = Random(7)
        draws = [
            select_shot_zone(shooter, rng, DEFAULT_TUNING, three_point_bias)
            for _ in range(2_000)
        ]
        return sum(1 for z in draws if z is Zone.THREE) / len(draws)

    share_low = _three_share(10)
    share_neutral = _three_share(50)
    share_high = _three_share(90)
    assert share_high > share_neutral > share_low


def test_three_point_bias_never_moves_the_rim_weight():
    """PD-2's own text: the bias is applied to THREE and inversely to MID
    -- RIM gets no factor from this knob at all. A shooter with zero
    MID/THREE tendency (RIM is drawn unconditionally either way) must draw
    RIM at the same rate regardless of three_point_bias."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 50,
            "shot_freq_mid": 0,
            "shot_freq_three": 0,
            "shot_freq_post": 50,
        },
    )
    for bias in (10, 50, 90):
        rng = Random(3)
        zone = select_shot_zone(shooter, rng, DEFAULT_TUNING, bias)
        assert zone is Zone.RIM


def test_shot_diet_more_threes_moves_the_zone_distribution_toward_three():
    """Mutation 9 -- a `more_threes` directive must draw THREE more often
    than `balanced`, on the same seed, holding `three_point_bias` at
    neutral so the two knobs' effects aren't conflated."""
    tendencies = {
        "shot_freq_rim": 33,
        "shot_freq_mid": 33,
        "shot_freq_three": 34,
        "shot_freq_post": 0,
    }
    balanced = make_sim_player(1, tuning=DEFAULT_TUNING, tendencies_overrides=tendencies)
    more_threes = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        tendencies_overrides=tendencies,
        directive=make_directive(2, shot_diet=ShotDiet.MORE_THREES),
    )

    def _three_share(shooter) -> float:
        rng = Random(11)
        draws = [select_shot_zone(shooter, rng, DEFAULT_TUNING, 50) for _ in range(2_000)]
        return sum(1 for z in draws if z is Zone.THREE) / len(draws)

    assert _three_share(more_threes) > _three_share(balanced)


def test_shot_diet_attack_the_rim_moves_the_zone_distribution_toward_rim():
    """The other named direction, so a mutation that only flips one
    ShotDiet value's sign would still be caught."""
    tendencies = {
        "shot_freq_rim": 33,
        "shot_freq_mid": 33,
        "shot_freq_three": 34,
        "shot_freq_post": 0,
    }
    balanced = make_sim_player(1, tuning=DEFAULT_TUNING, tendencies_overrides=tendencies)
    attack_rim = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        tendencies_overrides=tendencies,
        directive=make_directive(2, shot_diet=ShotDiet.ATTACK_THE_RIM),
    )

    def _rim_share(shooter) -> float:
        rng = Random(13)
        draws = [select_shot_zone(shooter, rng, DEFAULT_TUNING, 50) for _ in range(2_000)]
        return sum(1 for z in draws if z is Zone.RIM) / len(draws)

    assert _rim_share(attack_rim) > _rim_share(balanced)


def test_fewer_midrange_directive_never_zeroes_a_shooters_total_weight():
    """PD-2 disclosure 3 / mutation 17: a shooter with zero RIM/THREE
    tendency (all his volume is MID) under `fewer_midrange` -- the
    smallest multiplier in the table -- must still produce a strictly
    positive total weight, so `weighted_choice` never falls through to
    its `rng.choice` fallback branch and desyncs the seeded stream. Zone
    is deterministic here (only MID has any weight at all), so this
    checks the function returns cleanly over many draws rather than
    raising or behaving inconsistently."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 100,
            "shot_freq_three": 0,
            "shot_freq_post": 0,
        },
        directive=make_directive(1, shot_diet=ShotDiet.FEWER_MIDRANGE),
    )
    rng = Random(17)
    for _ in range(500):
        zone = select_shot_zone(shooter, rng, DEFAULT_TUNING, 50)
        assert zone is Zone.MID


def test_balanced_shot_diet_is_a_no_op_matching_no_directive_at_all():
    """PD-1's neutral-member requirement: a player with an explicit
    `balanced` directive must draw identically (same seed) to a player
    with no directive row at all -- `BALANCED`'s multipliers are all 1.0,
    so this also doubles as regression coverage for PD-2's own byte-
    identity acceptance criterion (3b) at the player-directive layer."""
    tendencies = {
        "shot_freq_rim": 40,
        "shot_freq_mid": 30,
        "shot_freq_three": 30,
        "shot_freq_post": 0,
    }
    no_directive = make_sim_player(1, tuning=DEFAULT_TUNING, tendencies_overrides=tendencies)
    balanced = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        tendencies_overrides=tendencies,
        directive=make_directive(2, shot_diet=ShotDiet.BALANCED),
    )
    draws_a = [select_shot_zone(no_directive, Random(9), DEFAULT_TUNING, 50) for _ in range(200)]
    draws_b = [select_shot_zone(balanced, Random(9), DEFAULT_TUNING, 50) for _ in range(200)]
    assert draws_a == draws_b


# --- docs/design/pick-and-pop.md (PP-10) -----------------------------------


def test_pop_shot_never_resolves_at_rim_zone():
    """PP-13 criterion 4(a) / PP-14 mutation 4: a pop is never a rim shot,
    even for a screener whose own tendencies are overwhelmingly rim-heavy
    -- deleting the `is_pop_shot` branch would go red immediately here,
    since a big's `shot_freq_rim` dominates his normal zone draw."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 94,
            "shot_freq_mid": 2,
            "shot_freq_three": 2,
            "shot_freq_post": 2,
        },
    )
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    saw_a_shot = False
    for seed in range(300):
        result = resolve_resolution_beat(
            shooter,
            defender,
            action_type=ActionType.PNR_BALL,
            tuning=DEFAULT_TUNING,
            rng=Random(seed),
            clock_seconds=14.0,
            home_score=0,
            away_score=0,
            assisted=True,
            passer_id=99,
            is_pop_shot=True,
        )
        if result.outcome.zone is not None:
            saw_a_shot = True
            assert result.outcome.zone is not Zone.RIM
    assert saw_a_shot, "no seed in range(300) produced a shot attempt at all"


def test_pop_shot_reads_the_catch_rating_column_not_the_dribble_one():
    """PP-14 mutation 5: forcing the pop's shot field to the `"dribble"`
    column must be catchable by a make-rate comparison, not by inspecting
    an internal string. Two shooters with swapped catch/dribble three-point
    ratings, both forced to `Zone.THREE` (`shot_freq_three=100`, so the
    pop's zero-total guard never fires and the zone draw is deterministic)
    -- if the pop path reads `catch_shoot_3` (correct), the high-catch
    shooter makes more; if it reads `off_dribble_3` (mutated), the
    high-dribble shooter would make more instead, flipping this
    comparison's direction."""
    high_catch = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 92, "off_dribble_3": 15},
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 0,
            "shot_freq_three": 100,
            "shot_freq_post": 0,
        },
    )
    high_dribble = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 15, "off_dribble_3": 92},
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 0,
            "shot_freq_three": 100,
            "shot_freq_post": 0,
        },
    )
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)

    def _pop_makes(shooter) -> int:
        return sum(
            1
            for seed in range(300)
            if resolve_resolution_beat(
                shooter,
                defender,
                action_type=ActionType.PNR_BALL,
                tuning=DEFAULT_TUNING,
                rng=Random(seed),
                clock_seconds=14.0,
                home_score=0,
                away_score=0,
                assisted=True,
                passer_id=99,
                is_pop_shot=True,
            ).outcome.kind
            is TerminalKind.MADE_FG
        )

    assert _pop_makes(high_catch) > _pop_makes(high_dribble)


def test_pop_zone_zero_total_guard_returns_mid_without_consuming_rng():
    """PP-14 mutation 12: both `shot_freq_mid`/`shot_freq_three` at 0 must
    resolve `Zone.MID` (the documented floor, not a `weighted_choice` draw
    that would divert into its `rng.choice` fallback and desync every
    subsequent draw). Checked directly against `_pop_zone_and_shot_inputs`
    rather than sampled: an identically-seeded `Random` that never had this
    function called on it must produce the exact same next value, proving
    the guard consumed zero draws -- a floor-less direct `weighted_choice`
    call would still consume exactly one `rng` call, but a different kind
    (`rng.choice`'s fallback vs. `rng.random()`), which desyncs the stream
    even though both "only cost one draw.\""""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 50,
            "shot_freq_mid": 0,
            "shot_freq_three": 0,
            "shot_freq_post": 50,
        },
    )
    rng_under_test = Random(5)
    zone, field_name, block_zone_key = _pop_zone_and_shot_inputs(
        shooter, DEFAULT_TUNING, rng_under_test, 50
    )
    assert zone is Zone.MID
    assert field_name == "catch_shoot_mid"
    assert block_zone_key == "mid"

    untouched_rng = Random(5)
    assert rng_under_test.random() == untouched_rng.random()


def test_pop_shot_field_is_the_catch_column_for_both_mid_and_three():
    """Direct unit check of `_pop_zone_and_shot_inputs`'s contract (PP-10.2):
    whichever zone is drawn, the field is always the `"catch"` column."""
    mid_only = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 100,
            "shot_freq_three": 0,
            "shot_freq_post": 0,
        },
    )
    three_only = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 0,
            "shot_freq_three": 100,
            "shot_freq_post": 0,
        },
    )
    zone_mid, field_mid, key_mid = _pop_zone_and_shot_inputs(
        mid_only, DEFAULT_TUNING, Random(1), 50
    )
    zone_three, field_three, key_three = _pop_zone_and_shot_inputs(
        three_only, DEFAULT_TUNING, Random(1), 50
    )
    assert (zone_mid, field_mid, key_mid) == (Zone.MID, "catch_shoot_mid", "mid")
    assert (zone_three, field_three, key_three) == (Zone.THREE, "catch_shoot_3", "three")


def test_non_pop_callers_are_unaffected_by_is_pop_shot_defaulting_to_false():
    """PP-12: every existing caller of `resolve_resolution_beat` doesn't
    pass `is_pop_shot` at all -- confirms the default keeps today's
    dribble-vs-catch machinery (a real `pullup_vs_catch` draw can still
    happen) rather than silently routing everyone through the pop path."""
    shooter = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        tendencies_overrides={
            "shot_freq_rim": 94,
            "shot_freq_mid": 2,
            "shot_freq_three": 2,
            "shot_freq_post": 2,
        },
    )
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    saw_rim = False
    for seed in range(100):
        result = _resolve(shooter, defender, seed=seed)
        if result.outcome.zone is Zone.RIM:
            saw_rim = True
            break
    assert saw_rim, "default is_pop_shot=False must still allow RIM zone attempts"


def test_pop_open_converts_strictly_better_than_pop_contested_on_a_fixed_pair():
    """PP-13 criterion 4(d) at the layer that can carry the sample size: the
    same popper and defender, `is_committed_helper_contest=True` (what
    `POP_OPEN` passes) against `False` (`POP_CONTESTED`), over 6,000 seeds
    each. A possession-level version of this comparison only ever sees
    ~20 pops per leaf at the calibrated share, and flipped direction on
    sampling noise alone when the pop-share ceiling was recalibrated -- the
    deterministic flag check lives in `test_possession.py`."""
    popper = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 85, "catch_shoot_mid": 80},
        tendencies_overrides={
            "shot_freq_rim": 0,
            "shot_freq_mid": 0,
            "shot_freq_three": 100,
            "shot_freq_post": 0,
        },
    )
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)

    def _makes(committed: bool) -> int:
        return sum(
            1
            for seed in range(6000)
            if resolve_resolution_beat(
                popper,
                defender,
                action_type=ActionType.PNR_BALL,
                tuning=DEFAULT_TUNING,
                rng=Random(seed),
                clock_seconds=14.0,
                home_score=0,
                away_score=0,
                assisted=True,
                passer_id=99,
                is_committed_helper_contest=committed,
                is_pop_shot=True,
            ).outcome.kind
            is TerminalKind.MADE_FG
        )

    assert _makes(True) > _makes(False)


# --- docs/design/turnover-recovery.md: the loose_ball_recovered leaf ----------


def test_default_call_offers_exactly_the_ten_documented_leaves_in_order():
    """TR-5.B.15(b) / TR-4.8: at the default recovery probability the key
    is OMITTED, not inserted at zero weight -- key presence is the only
    observable difference between those two, and the byte-identity of every
    existing caller depends on it (mutation 2). Order is pinned too: it is
    `weighted_choice`'s cumulative walk."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    result = _resolve(shooter, defender)
    assert tuple(result.probabilities) == _DEFAULT_LEAVES


def test_opting_in_adds_exactly_the_recovery_leaf_carved_from_turnover_live():
    """TR-4.1's arithmetic: the recovery leaf appears right after
    `turnover_live`, the two together hold exactly the live-turnover mass
    the default call gives `turnover_live` alone, and no other leaf moves."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    default = _resolve(shooter, defender).probabilities
    opted = _resolve(
        shooter,
        defender,
        loose_ball_recovery_probability=0.3,
        loose_ball_candidates=_candidates(),
    ).probabilities
    assert tuple(opted) == (
        "turnover_live",
        "loose_ball_recovered",
        *_DEFAULT_LEAVES[1:],
    )
    assert opted["turnover_live"] < default["turnover_live"]
    assert opted["turnover_live"] + opted["loose_ball_recovered"] == pytest.approx(
        default["turnover_live"], rel=1e-12
    )
    assert opted["loose_ball_recovered"] == pytest.approx(0.3 * default["turnover_live"])
    for leaf in _DEFAULT_LEAVES[1:]:
        assert opted[leaf] == default[leaf], leaf


def test_opted_in_outcome_is_always_a_resolution_eligible_kind_and_reaches_the_recovery():
    """TR-5.B.15(a): the opted-in twin of
    `test_outcome_is_always_a_resolution_eligible_terminal_kind`. Non-vacuous:
    `LOOSE_BALL_RECOVERED` must actually be drawn, or the member added to
    `_RESOLUTION_ELIGIBLE_KINDS` above is untested decoration."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    seen = set()
    for seed in range(2000):
        result = _resolve(
            shooter,
            defender,
            seed=seed,
            loose_ball_recovery_probability=0.5,
            loose_ball_candidates=_candidates(),
        )
        assert result.outcome.kind in _RESOLUTION_ELIGIBLE_KINDS
        seen.add(result.outcome.kind)
    assert TerminalKind.LOOSE_BALL_RECOVERED in seen


def test_the_recovery_leaf_builds_the_recovery_payload_and_names_the_recoverer():
    """TR-4.11 / mutation 8 and 18: the leaf makes a `LOOSE_BALL_RECOVERED`
    outcome (never a `TURNOVER_LIVE` one) carrying exactly the two ids --
    the loser in `loose_ball_player_id`, NOT in `turnover_player_id` -- and
    every scoring-adjacent field at its default. The beat record names the
    same drawn recoverer (TR-4.7), who is one of the candidates -- and across
    the sample more than one candidate is named (the draw, not an argmax)."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    candidates = _candidates()
    candidate_ids = {p.id for p in candidates}
    drawn = [
        r
        for seed in range(2000)
        if (
            r := _resolve(
                shooter,
                defender,
                seed=seed,
                loose_ball_recovery_probability=0.5,
                loose_ball_candidates=candidates,
            )
        ).record.outcome
        == "loose_ball_recovered"
    ]
    assert drawn
    for result in drawn:
        outcome = result.outcome
        assert outcome.kind is TerminalKind.LOOSE_BALL_RECOVERED
        assert outcome.loose_ball_player_id == shooter.id
        assert outcome.recovering_player_id in candidate_ids
        assert result.record.actor_ids["recovered_by"] == outcome.recovering_player_id
        defaults = dataclasses.replace(
            outcome, loose_ball_player_id=None, recovering_player_id=None
        )
        assert defaults == type(outcome)(kind=TerminalKind.LOOSE_BALL_RECOVERED), (
            "the recovery payload carries a field beyond its two ids -- every other "
            "field is a box-score channel (TR-4.3), so this is a stat the event never earned"
        )
    assert len({r.outcome.recovering_player_id for r in drawn}) > 1


def test_a_positive_recovery_probability_without_candidates_raises():
    """TR-6 Commit 2.6: with no type checker, this guard is the only thing
    between a threading mistake and a nameless player on a broadcast line."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    with pytest.raises(ValueError, match="no loose_ball_candidates"):
        _resolve(shooter, defender, loose_ball_recovery_probability=0.2)
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        _resolve(
            shooter,
            defender,
            loose_ball_recovery_probability=1.5,
            loose_ball_candidates=_candidates(),
        )


# sha256 over 10,000 default-call draws (leaf, leaf dict in order, outcome
# minus TR-4.11's two new fields, and the beat record), taken from the
# PRE-change engine at 3cdd70e (TUNING_VERSION 20) before
# `loose_ball_recovery_probability` existed. The two new `TerminalOutcome`
# fields are excluded because adding a field is a shape change, not a
# behaviour change (TR-7.1's own note).
_PRE_CHANGE_DEFAULT_CALL_DIGEST = (
    "b8a34f3c1386e90af61583b0413ebfb6a07e9fa95b26e484358c06d4627600c5"
)


def test_default_call_is_byte_identical_to_the_pre_change_engine():
    """TR-7.1 as a real byte-diff (mutations 1 and 2): `==` on a digest, not
    `approx`. Any change to the default path -- a non-zero default
    probability, a zero-weight key, a moved leaf -- changes the digest."""
    new_fields = ("loose_ball_player_id", "recovering_player_id")
    pairs = [
        (make_sim_player(1, tuning=DEFAULT_TUNING), make_sim_player(2, tuning=DEFAULT_TUNING)),
        (
            make_sim_player(
                3,
                tuning=DEFAULT_TUNING,
                ratings_overrides={"ball_security": 30, "pass_under_pressure": 30},
            ),
            make_sim_player(
                4, tuning=DEFAULT_TUNING, ratings_overrides={"hands": 95, "on_ball_defense": 95}
            ),
        ),
    ]
    digest = hashlib.sha256()
    for shooter, defender in pairs:
        for seed in range(5000):
            result = _resolve(shooter, defender, seed=seed)
            outcome = {
                k: v for k, v in dataclasses.asdict(result.outcome).items() if k not in new_fields
            }
            record = result.record
            digest.update(
                repr(
                    (
                        record.outcome,
                        list(result.probabilities.items()),
                        sorted(outcome.items()),
                        record.beat,
                        list(record.actor_ids.items()),
                        list(record.probabilities.items()),
                        record.clock_seconds,
                        record.home_score,
                        record.away_score,
                    )
                ).encode()
            )
    assert digest.hexdigest() == _PRE_CHANGE_DEFAULT_CALL_DIGEST


def test_the_recoverer_is_drawn_once_per_recovery_and_never_otherwise():
    """TR-4.7 (revised 2026-09-24) / TR-4.8: the weighted recoverer draw is
    the recovery leaf's own and costs exactly one extra `rng.random()`;
    every other leaf leaves the stream exactly where the default call
    leaves it. Checked by stream position against a default-call twin on
    the same seed (the leaf dicts differ, but the draw count must not)."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    candidates = _candidates()
    recoveries = others = 0
    for seed in range(3000):
        opted_rng, twin_rng = Random(seed), Random(seed)
        result = _resolve_with_rng(
            shooter,
            defender,
            opted_rng,
            loose_ball_recovery_probability=0.5,
            loose_ball_candidates=candidates,
        )
        _resolve_with_rng(shooter, defender, twin_rng)
        if result.record.outcome == "loose_ball_recovered":
            recoveries += 1
            twin_rng.random()
        else:
            others += 1
        assert opted_rng.getstate() == twin_rng.getstate(), (seed, result.record.outcome)
    assert recoveries > 0 and others > 0


def test_zero_probability_with_candidates_is_the_default_call_exactly():
    """The inert default is exact whatever the caller threads (TR-4.8):
    `possession.py` always passes the offense as candidates, and with the
    budget spent it passes probability 0.0 -- that must be the default
    call byte for byte, stream position included."""
    shooter = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    for seed in range(1000):
        a_rng, b_rng = Random(seed), Random(seed)
        a = _resolve_with_rng(shooter, defender, a_rng, loose_ball_candidates=_candidates())
        b = _resolve_with_rng(shooter, defender, b_rng)
        assert (a.outcome, a.record, a.probabilities) == (b.outcome, b.record, b.probabilities)
        assert a_rng.getstate() == b_rng.getstate()


def _resolve_with_rng(shooter, defender, rng, **kwargs):
    return resolve_resolution_beat(
        shooter,
        defender,
        action_type=ActionType.ISO,
        tuning=DEFAULT_TUNING,
        rng=rng,
        clock_seconds=14.0,
        home_score=20,
        away_score=18,
        **kwargs,
    )
