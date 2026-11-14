"""Tier 1, §12 step 3's verify bullets for the help beat: monotonicity at
extreme fixtures, and "a disciplined low-man commits more often than a
gambler.\""""
from __future__ import annotations

from random import Random

import pytest

from dba2.domain.role import DefenseMode
from dba2.sim.beats.help import HelpOutcome, _gate_probability, resolve_help_beat
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_directive, make_sim_player


def _resolve(driver, helper, help_aggression=50):
    return resolve_help_beat(
        driver,
        helper,
        help_aggression=help_aggression,
        tuning=DEFAULT_TUNING,
        rng=Random(0),
        clock_seconds=12.0,
        home_score=40,
        away_score=38,
    )


def test_probabilities_always_sum_to_one_and_stay_in_bounds():
    driver = make_sim_player(1, tuning=DEFAULT_TUNING)
    helper = make_sim_player(2, tuning=DEFAULT_TUNING)
    result = _resolve(driver, helper)
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
    for p in result.probabilities.values():
        assert 0.0 <= p <= 1.0


def test_higher_help_aggression_raises_the_gate_and_lowers_help_stayed():
    driver = make_sim_player(1, tuning=DEFAULT_TUNING)
    helper = make_sim_player(2, tuning=DEFAULT_TUNING)
    low_aggression = _resolve(driver, helper, help_aggression=5)
    high_aggression = _resolve(driver, helper, help_aggression=95)
    assert (
        high_aggression.probabilities[HelpOutcome.HELP_STAYED.value]
        < low_aggression.probabilities[HelpOutcome.HELP_STAYED.value]
    )


def test_finishing_advantage_raises_help_late_relative_to_a_weak_finisher():
    elite_finisher = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "finishing_rim": 95,
            "contact_finishing": 92,
            "floater": 88,
            "draw_foul": 85,
        },
    )
    weak_finisher = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "finishing_rim": 10,
            "contact_finishing": 12,
            "floater": 8,
            "draw_foul": 15,
        },
    )
    helper = make_sim_player(3, tuning=DEFAULT_TUNING)

    high = _resolve(elite_finisher, helper)
    low = _resolve(weak_finisher, helper)
    high_late = high.probabilities[HelpOutcome.HELP_LATE.value]
    low_late = low.probabilities[HelpOutcome.HELP_LATE.value]
    assert high_late > low_late


def test_a_disciplined_low_man_commits_more_often_than_a_gambler():
    """§12 step 3's own wording, verified as an exact probability
    comparison rather than a statistical sample -- `.probabilities` is
    computed deterministically before the RNG draw, so no sampling noise
    is involved."""
    # The defense ability-vector fields (rotation_speed/help_iq/
    # rim_protection/vertical) are pinned to the same value for both
    # fixtures -- only `scheme_discipline` (Modifiers-column gate input)
    # and `help_gamble_freq` (the one allowlisted tendency) vary, so any
    # probability difference is attributable to those two, not to two
    # different players' unrelated default ratings happening to differ
    # (this project's fixture-discipline concern, applied to an isolation
    # test rather than a round-trip test).
    _shared_ability = {
        "rotation_speed": 50,
        "help_iq": 50,
        "rim_protection": 50,
        "vertical": 50,
    }
    driver = make_sim_player(1, tuning=DEFAULT_TUNING)
    disciplined = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={**_shared_ability, "scheme_discipline": 95},
        tendencies_overrides={"help_gamble_freq": 5},
    )
    gambler = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        ratings_overrides={**_shared_ability, "scheme_discipline": 5},
        tendencies_overrides={"help_gamble_freq": 95},
    )

    disciplined_result = _resolve(driver, disciplined)
    gambler_result = _resolve(driver, gambler)
    assert (
        disciplined_result.probabilities[HelpOutcome.HELP_COMMITTED.value]
        > gambler_result.probabilities[HelpOutcome.HELP_COMMITTED.value]
    )


def test_gate_only_scheme_discipline_never_changes_the_post_gate_quality_split():
    """Decision P1-16: `scheme_discipline` only gates *whether* the helper
    attempts to help; it must never enter the post-gate
    HELP_COMMITTED-vs-HELP_LATE quality contest (only the ability vector
    and `help_gamble_freq` may). Verified by holding `help_gamble_freq`
    and the ability vector fixed while sweeping `scheme_discipline` --
    the conditional split `p_committed_given_attempt = P(COMMITTED) /
    P(attempt)` must stay constant."""
    driver = make_sim_player(1, tuning=DEFAULT_TUNING)
    low_discipline = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"scheme_discipline": 10}
    )
    high_discipline = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"scheme_discipline": 90}
    )

    low_result = _resolve(driver, low_discipline)
    high_result = _resolve(driver, high_discipline)

    def _quality_ratio(result):
        p_attempt = 1.0 - result.probabilities[HelpOutcome.HELP_STAYED.value]
        return result.probabilities[HelpOutcome.HELP_COMMITTED.value] / p_attempt

    assert _quality_ratio(low_result) == pytest.approx(_quality_ratio(high_result))


def test_defense_mode_stay_home_and_help_early_move_the_gate_in_opposite_directions():
    """PD-4, mutation 12: `stay_home` must gate below `balanced`, and
    `help_early` must gate above `balanced`, on the same fixed input. A
    direct unit test of the pure `_gate_probability` function, not a
    whole-game assertion. All three fixtures share the same ratings/
    tendencies overrides (this project's distinct-value-per-player-id
    fixture discipline would otherwise confound the comparison -- see
    the `balanced`-vs-no-directive test below for the full reasoning)."""
    shared = {
        "ratings_overrides": {"scheme_discipline": 60},
        "tendencies_overrides": {"help_gamble_freq": 40},
    }
    balanced_helper = make_sim_player(1, tuning=DEFAULT_TUNING, **shared)
    p_balanced = _gate_probability(balanced_helper, help_aggression=50, tuning=DEFAULT_TUNING)

    stay_home_helper = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        directive=make_directive(2, defense_mode=DefenseMode.STAY_HOME),
        **shared,
    )
    p_stay_home = _gate_probability(stay_home_helper, help_aggression=50, tuning=DEFAULT_TUNING)

    help_early_helper = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        directive=make_directive(3, defense_mode=DefenseMode.HELP_EARLY),
        **shared,
    )
    p_help_early = _gate_probability(help_early_helper, help_aggression=50, tuning=DEFAULT_TUNING)

    assert p_stay_home < p_balanced < p_help_early


def test_defense_mode_composes_with_help_aggression_rather_than_overriding_it():
    """The team knob and the player directive compose; neither overrides
    the other -- a `stay_home` player on a `help_aggression=90` team must
    still gate to help MORE than a `stay_home` player on a `help_
    aggression=10` team. Same fixture both times, only `help_aggression`
    (the caller-supplied team knob) differs."""
    stay_home_helper = make_sim_player(
        1, tuning=DEFAULT_TUNING, directive=make_directive(1, defense_mode=DefenseMode.STAY_HOME)
    )
    p_low_team_aggression = _gate_probability(
        stay_home_helper, help_aggression=10, tuning=DEFAULT_TUNING
    )
    p_high_team_aggression = _gate_probability(
        stay_home_helper, help_aggression=90, tuning=DEFAULT_TUNING
    )
    assert p_high_team_aggression > p_low_team_aggression


def test_no_directive_and_an_explicit_balanced_directive_gate_identically():
    """PD-1's neutral-member requirement, at the help-gate layer. Both
    fixtures share the same ratings/tendencies overrides -- this project's
    distinct-value-per-player-id fixture discipline means two different
    ids otherwise differ on `scheme_discipline`/`help_gamble_freq` too,
    which would confound the comparison with something other than the
    directive itself."""
    shared = {
        "ratings_overrides": {"scheme_discipline": 60},
        "tendencies_overrides": {"help_gamble_freq": 40},
    }
    no_directive = make_sim_player(1, tuning=DEFAULT_TUNING, **shared)
    balanced = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        directive=make_directive(2, defense_mode=DefenseMode.BALANCED),
        **shared,
    )
    p_a = _gate_probability(no_directive, help_aggression=50, tuning=DEFAULT_TUNING)
    p_b = _gate_probability(balanced, help_aggression=50, tuning=DEFAULT_TUNING)
    assert p_a == p_b
