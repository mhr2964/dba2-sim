"""BDA-6.2 (docs/design/block-defender-attribution.md): the exposure
regression guard this whole doc exists to install. Not `slow`-marked -- 40
games at ~0.076s/game is ~3s, cheap enough for the default suite, and this
is precisely the check that must run on every change.

Both tests below were verified to FAIL against pre-BDA-4 behavior before
BDA-4's fix landed, following `test_discrimination_floor.py`'s own stated
convention (its module docstring, `:24-28`): a regression guard that
passes on the broken code is not a guard. The verification method:
`resolution_rim_help.band=(0.0, 0.0)` forces `p_help_block` to exactly
`0.0` on every shot (the same `contest_probability` clamp any in-band
value already goes through, just pinned to its floor). Measured with that
proxy: **1.13 bpg** (below the [1.6, 4.0] floor) and **33.7% rim-contest
share** (below the 40% floor) -- both fail, confirming red before the fix.
(These numbers are higher than BDA-0's own cited ~0.5 bpg/~15% because
that finding used a different, more realistic-mixed-shot-diet fixture;
this file's own fixture forces a 100% rim shot diet for the share test and
an unusually extreme `rim_protection=99` synthetic for the bpg test,
which both push even the pre-fix positional-matchup-only routing higher
than a real roster would produce -- the point of this file is the same
mechanism gap, not reproducing BDA-0's exact numbers on a different
fixture.) See this dispatch's build report for the full verification
transcript.

**BDA-9 correction (critic-proven, docs/design/block-defender-attribution.md):**
this docstring used to claim `band=(0.0, 0.0)` is "mathematically identical
to every call site never passing a `rim_help_defender` at all -- i.e.
identical to the engine before BDA-4's change on this exact fixture, not
merely close to it." That overclaimed. It IS an exact proxy for the
*resolution beat's own leaf arithmetic and RNG draw*: `weighted_choice`
(`probability.py`) accumulates weights in insertion order, and a
zero-weight `blocked_miss_help` entry contributes nothing to any
cumulative boundary, so the same `rng.random()` draw resolves to the same
leaf as the true 9-leaf pre-BDA-4 dict. It is NOT identical to the engine
before BDA-4's change as a whole: `RoleSlot.RIM_PROTECTOR`'s BDA-4.7 Option
C widening onto every action type's defense list (`playbook.py`) is a
separate, still-shipped change `band=(0.0, 0.0)` does not touch -- every
`setup` beat's `actor_ids` now carries a `"defense:rim_protector"` entry
regardless of action type (pre-BDA-4, only `transition` had one), and even
`transition`'s own entry now comes from `composites.best_rim_protector`
instead of the old `matchups[RoleSlot.TRAILER]` lookup. Neither difference
perturbs a score, an RNG draw, or (after this dispatch's `game.py`
`_beat_participation_counts` per-beat dedup fix) fatigue -- so `band=(0.0,
0.0)` remains the right proxy for what these two tests actually measure
(is the block-attribution mechanism responsible for the bpg/share gap),
just not a literal byte-for-byte pre-BDA-4 beat-log replay.

**Re-specified by docs/design/block-attribution-v2.md BA-4.2 (Commit 2).**
The weak-side contest now runs over every eligible helper and draws the
blocker, so "the rim protector is the candidate on a rim shot" no longer
exists. The bpg guard keeps its 1.6 floor, but its ceiling moved 4.0 -> 7.0
(that test's docstring says why). The share guard became "his share of his
team's weak-side blocks while on the floor >= 60%" for the lone-99 fixture.
Both were re-verified to FAIL with `resolution_rim_help.band=(0.0, 0.0)` (no
help block can happen) before Commit 2 landed: see the design doc's As-built
for the measured values.

**What the share guard does not catch (Correction, 2026-09-24).** It is an
exposure guard, not a concentration guard. On this fixture the lone 99 sits
beside four `rim_protection=15` teammates whose own contest barely converts,
so even with the share flattened completely (`attribution_concentration_
exponent=0`, every helper equally likely to arrive) he still takes 70.8% of
the help blocks (85/120) and clears the 60% floor. Concentration is guarded
by `test_possession.py::test_second_big_on_the_floor_gets_a_share_of_help_
blocks` (a lone elite big among 55s must take >= 90%) and, on this fixture,
by `test_flattening_the_help_share_lowers_the_elite_bigs_share` below.
"""
from __future__ import annotations

import dataclasses

from dba2.core.rng import rng_for
from dba2.domain.player import Position
from dba2.domain.team import DefensiveScheme
from dba2.sim.context import GameContext, build_sim_team
from dba2.sim.game import simulate_game
from dba2.sim.possession import simulate_possession
from dba2.sim.state import GameState
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player, make_sim_team, make_team, make_team_strategy

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _elite_rim_protector_team(team_id: int):
    """A 10-player roster whose lone elite rim protector is also its
    highest-`overall_cached`, no-role player -- guarantees starter-tier
    minutes via `game._minutes_shares`'s rank-tiering (Decision P1-19 item
    1), the "normal starter's workload" BDA-6.2 names. Every other player
    is deliberately a poor rim protector (default fixture ratings, capped
    `overall_cached`), so `composites.best_rim_protector` picks the elite
    player unambiguously on every possession."""
    team = make_team(team_id)
    strategy = make_team_strategy(team_id)
    elite = make_sim_player(
        team_id * 1000 + 1,
        tuning=DEFAULT_TUNING,
        primary_position=Position.C,
        height_in=84,
        wingspan_in=94,
        team_id=team_id,
        with_role=False,
        overall_cached=95,
        ratings_overrides={"rim_protection": 99, "vertical": 96, "lateral_quickness": 85},
    )
    rest = tuple(
        make_sim_player(
            team_id * 1000 + 2 + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i % len(_POSITIONS)],
            height_in=74 + i,
            team_id=team_id,
            with_role=False,
            overall_cached=60 - i,
            ratings_overrides={"rim_protection": 15, "vertical": 20, "lateral_quickness": 20},
        )
        for i in range(9)
    )
    players = (elite, *rest)
    return build_sim_team(team, strategy, players), elite


def test_an_elite_rim_protector_at_starter_minutes_reaches_a_realistic_season_bpg():
    """Real elite rim protectors run 2.0-3.6 bpg; a `rim_protection=99`
    synthetic is above any real player, so this band is deliberately wide
    on the top end and gated on the bottom end, which is the side BDA-0
    measured as actually broken (~0.20-0.46 bpg pre-fix).

    block-attribution-v2 (Commit 2) raised the ceiling 4.0 -> 7.0. Under the
    share model this fixture's four `rim_protection=15` teammates -- no real
    floor has them -- leave him essentially every weak-side block, and the
    steeper contest converts them: measured 6.17 bpg at scale 12, 6.20 at
    scale 13 (the 2026-09-24 Correction). The realism
    of the top end is `validate_sim`'s `top_shot_blocker_bpg` [1.8, 4.2] on
    real leagues (Wembanyama 3.6); here the ceiling only catches a runaway
    (e.g. the band clamp lost). The floor is the guard, re-verified red at
    `band=(0.0, 0.0)`: 1.38 bpg (1.375 again at scale 13)."""
    defense_team, elite = _elite_rim_protector_team(1)
    offense_team = make_sim_team(2, tuple(range(2001, 2011)), tuning=DEFAULT_TUNING)
    context = GameContext(
        game_id=1, season=2026, home=defense_team, away=offense_team, tuning=DEFAULT_TUNING
    )

    n_games = 40
    total_blocks = 0
    for seed in range(n_games):
        result = simulate_game(context, rng_seed=seed)
        line = next((p for p in result.player_box_lines if p.player_id == elite.id), None)
        total_blocks += line.blk if line is not None else 0

    bpg = total_blocks / n_games
    assert 1.6 <= bpg <= 7.0, (
        f"elite rim protector averaged {bpg:.2f} bpg over {n_games} games at starter "
        "minutes -- outside the [1.6, 7.0] band this guard exists to check "
        "(BDA-0's measured pre-fix ceiling was ~0.5 bpg)"
    )


def _elite_help_block_counts(tuning) -> tuple[int, int]:
    """(the lone elite big's weak-side blocks, his team's weak-side blocks)
    over 2,000 possessions against a 100%-rim offense (the same zone-control
    technique `test_resolution.py`'s gate tests use), so every shot is a
    help-block opportunity. He is on the floor for every possession."""
    rim_only = {
        "shot_freq_rim": 100,
        "shot_freq_mid": 0,
        "shot_freq_three": 0,
        "shot_freq_post": 0,
    }
    defense_team, elite = _elite_rim_protector_team(1)
    offense = tuple(
        make_sim_player(
            2000 + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i],
            height_in=74 + i * 3,
            team_id=2,
            tendencies_overrides=rim_only,
        )
        for i in range(5)
    )
    defense = defense_team.players[:5]

    game_state = GameState()
    help_blocks = 0
    elite_help_blocks = 0
    for seed in range(2000):
        rng = rng_for(20260922, seed)
        sim_result = simulate_possession(
            offense,
            defense,
            tuning=tuning,
            rng=rng,
            game_state=game_state,
            home_score=0,
            away_score=0,
            offense_is_home=True,
            defense_help_aggression=50,
            defense_defensive_scheme=DefensiveScheme.DROP,
            crash_oreb_bias=50,
        )
        for record in sim_result.beat_log:
            if record.beat == "resolution" and record.outcome == "blocked_miss_help":
                help_blocks += 1
                if record.actor_ids["blocked_by"] == elite.id:
                    elite_help_blocks += 1
    return elite_help_blocks, help_blocks


def test_an_elite_rim_protector_takes_most_of_his_floors_help_blocks():
    """The exposure guard (BDA-6.2), re-specified by docs/design/block-
    attribution-v2.md BA-4.2: "the candidate on a rim shot" no longer
    exists -- every eligible defender is a helper with a share -- so the
    guard is his share of his team's weak-side (`blocked_miss_help`) blocks
    while he is on the floor. A lone `rim_protection=99` big among poor rim
    protectors must take at least 60% of them; an exposure defect -- no help
    contest reaching him -- drops it. A floor with no help blocks at all
    counts as 0%.

    It does NOT catch a flattened share: at `attribution_concentration_
    exponent=0` he still takes 70.8% here, because his teammates' own
    contest barely converts (see the module docstring). The flattening
    guard is the next test."""
    elite_help_blocks, help_blocks = _elite_help_block_counts(DEFAULT_TUNING)
    share = elite_help_blocks / help_blocks if help_blocks else 0.0
    assert share >= 0.60, (
        f"the floor's lone elite rim protector took {share:.1%} of his team's weak-side "
        f"blocks ({elite_help_blocks}/{help_blocks}) -- below the 60% floor this guard "
        "exists to check"
    )


def test_flattening_the_help_share_lowers_the_elite_bigs_share():
    """BA-2.2's concentration is load-bearing on this fixture. Measured at
    the shipped `gamma = 4`: 97.4% (409/420); at `gamma = 0` (every eligible
    helper equally likely to be the one who arrives): 70.8% (85/120). The
    exponent must move his share by at least 15 points. This fails if the
    exponent stops reaching the share (a wiring loss), or if the shipped
    value is flattened to 0."""
    shipped_elite, shipped_total = _elite_help_block_counts(DEFAULT_TUNING)
    flat_tuning = dataclasses.replace(
        DEFAULT_TUNING,
        resolution_rim_help=dataclasses.replace(
            DEFAULT_TUNING.resolution_rim_help, attribution_concentration_exponent=0.0
        ),
    )
    flat_elite, flat_total = _elite_help_block_counts(flat_tuning)
    assert shipped_total and flat_total, (shipped_total, flat_total)
    shipped_share = shipped_elite / shipped_total
    flat_share = flat_elite / flat_total
    assert shipped_share - flat_share >= 0.15, (
        f"shipped share {shipped_share:.1%} ({shipped_elite}/{shipped_total}) vs "
        f"gamma=0 share {flat_share:.1%} ({flat_elite}/{flat_total}): the concentration "
        "exponent no longer concentrates the help blocks on the floor's best rim protector"
    )
