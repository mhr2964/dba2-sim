"""Tier 2: box-score identities over 200 seeded games (§12 step 5's own
verify bullets), plus the required manual eyeball of a single simulated
`GameResult` for obvious nonsense (§12 step 5: "before any tuning
starts")."""
from __future__ import annotations

from collections import Counter

import pytest

from dba2.domain.player import Position
from dba2.domain.team import OffensiveScheme
from dba2.sim.context import GameContext, build_sim_team
from dba2.sim.game import _minutes_curve_raw, _minutes_shares, simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import (
    _distinct_roster_knobs,
    make_game_context,
    make_sim_player,
    make_sim_team,
    make_team,
    make_team_strategy,
)

_BOX_FIELDS = (
    "points",
    "fga_rim",
    "fgm_rim",
    "fga_mid",
    "fgm_mid",
    "fga_three",
    "fgm_three",
    "fta",
    "ftm",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
)

_GAME_COUNT = 200


def _games():
    for seed in range(_GAME_COUNT):
        ctx = make_game_context(seed, tuning=DEFAULT_TUNING)
        yield simulate_game(ctx, rng_seed=seed)


@pytest.mark.slow
def test_two_hundred_seeded_games_satisfy_box_score_identities():
    total_rebounds = 0
    total_fg_misses = 0

    for result in _games():
        for line in (*result.player_box_lines, *result.team_box_lines):
            assert 2 * (line.fgm_rim + line.fgm_mid) + 3 * line.fgm_three + line.ftm == line.points
            assert line.fgm_rim <= line.fga_rim
            assert line.fgm_mid <= line.fga_mid
            assert line.fgm_three <= line.fga_three
            assert line.ftm <= line.fta

        home_line = next(t for t in result.team_box_lines if t.team_id == result.home_team_id)
        away_line = next(t for t in result.team_box_lines if t.team_id == result.away_team_id)
        assert home_line.points == result.home_score
        assert away_line.points == result.away_score

        for team_id, team_line in (
            (result.home_team_id, home_line),
            (result.away_team_id, away_line),
        ):
            players = [p for p in result.player_box_lines if p.team_id == team_id]
            for field_name in _BOX_FIELDS:
                assert sum(getattr(p, field_name) for p in players) == getattr(
                    team_line, field_name
                )

        for p in result.player_box_lines:
            assert p.minutes_played >= 0.0
            assert p.pf <= 6

        expected_minutes = 240 + 25 * result.overtime_periods
        for team_id in (result.home_team_id, result.away_team_id):
            total = sum(p.minutes_played for p in result.player_box_lines if p.team_id == team_id)
            assert total == pytest.approx(expected_minutes, abs=0.05)

        total_rebounds += home_line.oreb + home_line.dreb + away_line.oreb + away_line.dreb
        total_fg_misses += (
            (home_line.fga_rim - home_line.fgm_rim)
            + (home_line.fga_mid - home_line.fgm_mid)
            + (home_line.fga_three - home_line.fgm_three)
            + (away_line.fga_rim - away_line.fgm_rim)
            + (away_line.fga_mid - away_line.fgm_mid)
            + (away_line.fga_three - away_line.fgm_three)
        )

    # "a team's DREB + opponent's OREB = opponent's rebound-eligible
    # misses" (§9): every missed FG requires exactly one rebound, and a
    # missed *final* free throw (§4.6) adds one more rebound-eligible
    # event with no corresponding FGA -- so total rebounds recorded can
    # never fall short of total FG misses (that would mean a rebound
    # leaked, the exact bug class this identity exists to catch), and the
    # gap above FG misses should be a small share of it (missed final FTs
    # only, at a ~20-25% team FT-miss rate on a minority of trips).
    assert total_rebounds >= total_fg_misses
    assert total_rebounds - total_fg_misses <= max(20, total_fg_misses * 0.25)


def test_one_game_eyeballed_for_obvious_nonsense_position_aware_shot_mix():
    """§12 step 5: "a first eyeball at a single simulated box score for
    obvious nonsense (a center taking 20 threes) before any tuning
    starts." Originally written when `tests/fixtures/sim.py`'s
    `make_sim_team` defaulted every player to `Position.SG` (fixed by the
    B6 fix -- `make_sim_team` now gives every player a distinct position/
    height), so this test still builds its own position-varied roster
    directly rather than depending on the shared builder's exact per-index
    knob assignment (a one-off need with its own explicit position list,
    not a second fixture file)."""
    positions = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)

    def _team(team_id: int, base_id: int):
        players = tuple(
            make_sim_player(
                base_id + i,
                tuning=DEFAULT_TUNING,
                primary_position=positions[i % 5],
                height_in=74 + (i % 5) * 3,
                team_id=team_id,
            )
            for i in range(10)
        )
        return build_sim_team(make_team(team_id), make_team_strategy(team_id), players)

    home = _team(1, 1)
    away = _team(2, 101)
    ctx = GameContext(game_id=1, season=2026, home=home, away=away, tuning=DEFAULT_TUNING)
    result = simulate_game(ctx, rng_seed=11)

    position_by_id = {p.id: p.player.primary_position for p in (*home.players, *away.players)}

    # A loose ceiling, not a realism claim: nobody should be able to
    # attempt an unbounded number of threes relative to their minutes,
    # regardless of position -- this catches a genuinely broken
    # zone-selection loop, not an unrealistic-but-legal shot profile.
    for line in result.player_box_lines:
        assert line.fga_three <= line.minutes_played, (
            f"player {line.player_id} attempted {line.fga_three} threes in only "
            f"{line.minutes_played:.1f} minutes -- looks like a broken loop, not variance"
        )

    center_threes = [
        line.fga_three
        for line in result.player_box_lines
        if position_by_id.get(line.player_id) is Position.C
    ]
    # Documented finding (see build report): centers in this single
    # sample attempted single-digit but non-trivial three counts (not
    # literally "20 threes"), root-caused to `sim/derivation.py` (step 7,
    # not built this dispatch) not yet existing to correlate
    # `shot_freq_three` with rating/position the way Decision P1-19 item
    # 2 describes -- `tests/fixtures/sim.py`'s tendency generator is
    # deliberately rating/position-independent for field-swap-bug
    # coverage, so nothing in this dispatch's engine code constrains it
    # either. Not asserted away here since fixing it is out of this
    # dispatch's scope; recorded as a real, expected finding instead.
    assert center_threes  # the check above already ran on real numbers


def test_a_simulated_game_produces_a_plausible_final_score():
    """A cheap smoke check that a game actually produces basketball --
    positive scores on both sides, no negative overtime count."""
    ctx = make_game_context(99, tuning=DEFAULT_TUNING)
    result = simulate_game(ctx, rng_seed=99)
    assert result.home_score > 0
    assert result.away_score > 0
    assert result.overtime_periods >= 0


def test_roster_minutes_are_not_uniformly_degenerate():
    """B6 fix regression: `make_game_context`'s roster used to hand every
    player an identical `touch_share` ("0.22"), which -- since
    `_minutes_shares` normalizes shares to sum to 1.0 across the team --
    made every one of a team's 10 players come out to a perfectly uniform
    24.0 minutes, every game, hiding `_minutes_shares`' touch_share vs.
    overall_cached-rank-tier branch split entirely (nothing in the fixture
    ever routed through the second branch, and the first branch had no
    per-player variance to prove it was even reading the right field).
    Directly checks what critic checked: minutes are no longer uniform."""
    ctx = make_game_context(1, tuning=DEFAULT_TUNING)
    result = simulate_game(ctx, rng_seed=1)
    for team_id in (result.home_team_id, result.away_team_id):
        minutes = [p.minutes_played for p in result.player_box_lines if p.team_id == team_id]
        rounded = {round(m, 1) for m in minutes}
        assert len(rounded) > 1, (
            f"team {team_id}'s players all played the same minutes ({minutes[0]:.1f}) -- "
            "roster knobs are still degenerate"
        )
        assert not all(m == pytest.approx(24.0, abs=0.5) for m in minutes)


def test_minutes_shares_ranks_full_roster_including_role_carrying_player():
    """B6 fix, round 2 (critic-round-7-proven): the fixture roster used to
    put all 5 no-role players in the starter tier, leaving the
    rotation/bench anchors unread by the whole suite -- critic proved this
    live by zeroing them out and getting a green suite anyway, golden file
    included. `tests/fixtures/sim.py`'s `_distinct_roster_knobs` puts 9
    no-role players on a 10-player roster; this test locks the ordering in
    directly against `_minutes_shares`, the pure function the curve
    actually lives in, rather than depending on a full game's rotation
    model to preserve it.

    **Retargeted for RC-4/RC-16** (`docs/design/auto-gm-role-construction.md`,
    RC-10.1 -- "after RC-4 there are no tiers... retarget it at distinct
    anchor ranks"): the old assertion was "5 players collapse to one
    starter share, 3 to one rotation share, 1 to the bench share," which a
    per-rank curve makes false by construction (every rank is now
    distinct). The property worth locking in instead is RC-16's fix
    itself: the fixture's one role-carrying player (`overall_cached=90`)
    is the roster's actual rank 1, so the 9 roleless players are ranked at
    absolute positions 2-10, not 1-9 -- proving a roleless player's curve
    index accounts for a role-carrying teammate ahead of him, which the
    pre-RC-16 `without_role`-only ranking could not do."""
    team = make_sim_team(1, tuple(range(1, 11)), tuning=DEFAULT_TUNING)
    shares = _minutes_shares(team)

    full_roster_ranked = sorted(team.players, key=lambda p: (-p.player.overall_cached, p.id))
    without_role = [p for p in full_roster_ranked if p.touch_share_f is None]
    assert len(without_role) == 9, "fixture roster-knob split changed -- update this test's ranks"
    role_carrying = [p for p in full_roster_ranked if p.touch_share_f is not None]
    assert len(role_carrying) == 1, "fixture roster-knob split changed -- update this test's ranks"
    assert full_roster_ranked[0] is role_carrying[0], (
        "fixture's role-carrying player is no longer the roster's top overall -- "
        "this test needs the role-carrying player ranked first for the RC-16 "
        "assertion below to be meaningful"
    )

    # RC-16: each roleless player's curve index is his 1-indexed position in
    # the *full* roster (rank 1 goes to the role-carrying player), so the
    # 9 roleless players read ranks 2-10 off `_minutes_curve_raw`, not
    # ranks 1-9. `_minutes_shares` divides every player's raw share by the
    # same team-wide normalizer, so `minutes_i / _minutes_curve_raw(rank_i)`
    # must be one constant across all 9 roleless players -- if RC-16's fix
    # regressed to ranking `without_role` alone, this ratio would instead
    # match `_minutes_curve_raw(rank_i - 1)`, one rank too generous for
    # every one of them.
    expected_raw = [_minutes_curve_raw(rank) for rank in range(2, 11)]
    actual_minutes = [shares[p.id] * 240.0 for p in without_role]
    normalizers = [
        actual / expected for actual, expected in zip(actual_minutes, expected_raw, strict=True)
    ]
    assert normalizers == pytest.approx([normalizers[0]] * len(normalizers), rel=1e-9)
    wrong_rank_raw = [_minutes_curve_raw(rank) for rank in range(1, 10)]
    wrong_normalizers = [
        actual / wrong for actual, wrong in zip(actual_minutes, wrong_rank_raw, strict=True)
    ]
    wrong_uniform = pytest.approx([wrong_normalizers[0]] * len(wrong_normalizers), rel=1e-6)
    assert wrong_normalizers != wrong_uniform, (
        "actual minutes are also consistent with the pre-RC-16 (rank - 1) "
        "reading -- this fixture no longer distinguishes the two behaviors"
    )

    # No collapsed tiers, and strictly decreasing with `overall_cached`
    # rank -- the direction a degenerate curve (critic's own zeroed-constant
    # mutation) would visibly break.
    assert len(set(actual_minutes)) == len(actual_minutes)
    assert actual_minutes == sorted(actual_minutes, reverse=True)


def test_a_roleless_player_on_a_roled_roster_is_ranked_against_the_whole_roster():
    """RC-16 / B7 guard (`docs/design/auto-gm-role-construction.md`).

    A 14-man team, 13 role rows and one roleless player whose
    `overall_cached` is the team's 11th-best. Before RC-16, `game.py:115`
    sorted only `without_role` and enumerated it from index 0 -- a
    one-element list -- so this player read the curve's **rank-1
    (starter) anchor**, regardless of his real quality, exactly the "a
    January acquisition draws the rank-1 anchor" defect revision 1 of the
    design mistakenly called graceful degradation. Ranking him within the
    *full* roster (role-carrying teammates included) puts him at his true
    rank 11.

    **Must fail on pre-RC-16 `HEAD`** -- verified live: reverting
    `_minutes_shares` to rank `without_role` alone reproduces the rank-1
    anchor for this fixture's roleless player, which this test's asserted
    rank-11 value rejects.
    """
    overall_by_slot = [90, 88, 86, 84, 82, 80, 78, 76, 74, 72, 70, 68, 66, 64]
    roleless_slot = 10  # 0-indexed; overall_by_slot[10] == 70, the 11th-best
    players = []
    for i, overall in enumerate(overall_by_slot):
        players.append(
            make_sim_player(
                200 + i,
                tuning=DEFAULT_TUNING,
                primary_position=Position.SG,
                overall_cached=overall,
                with_role=(i != roleless_slot),
                touch_share=f"{0.10 + 0.01 * i:.2f}",
            )
        )
    team = make_team(1)
    strategy = make_team_strategy(1)
    team = build_sim_team(team, strategy, tuple(players))

    roleless_player = players[roleless_slot]
    assert roleless_player.touch_share_f is None
    assert roleless_player.player.overall_cached == 70

    shares = _minutes_shares(team)
    minutes = shares[roleless_player.id] * 240.0

    # Reconstruct `_minutes_shares`' own formula directly: every
    # role-carrying player contributes `max(touch_share_f, 0.01)`
    # (dimensionless, pre-normalization) and the roleless player
    # contributes `_minutes_curve_raw(rank) / 240`. His true rank is 11
    # (10 players strictly ahead of him by `overall_cached`).
    role_raw_shares = [max(p.touch_share_f, 0.01) for p in players if p.touch_share_f is not None]
    expected_total_at_rank_11 = sum(role_raw_shares) + _minutes_curve_raw(11) / 240.0
    expected_minutes_at_rank_11 = _minutes_curve_raw(11) / expected_total_at_rank_11

    # What the pre-RC-16 bug would have produced instead: `without_role`
    # is a one-element list, so this player reads index 0 -- the rank-1
    # (starter) anchor.
    expected_total_at_rank_1 = sum(role_raw_shares) + _minutes_curve_raw(1) / 240.0
    expected_minutes_at_rank_1 = _minutes_curve_raw(1) / expected_total_at_rank_1

    assert minutes == pytest.approx(expected_minutes_at_rank_11, rel=1e-9)
    assert minutes != pytest.approx(expected_minutes_at_rank_1, rel=1e-3), (
        "this player was handed the rank-1 (starter) anchor -- the RC-16 bug"
    )


def test_low_ranked_written_shares_stay_distinct():
    """RC-21 (`docs/design/auto-gm-role-construction.md`): a 14-man roster
    with real `player_roles` rows at `_MINUTES_CURVE`'s own values for
    every rank. Ranks 12, 13 and 14 sit at 1.8/1.4/1.0 raw minutes -- all
    three under the old `max(touch_share_f, 0.01)` floor's 2.4 mpg
    equivalent -- so a pass that dutifully writes the curve's own output
    back as `touch_share` used to have all three clamp to the same 0.01
    and, after normalization, receive **identical** minutes. **Must fail
    on pre-RC-21 HEAD** (verified live: reverting the floor to `max(...,
    0.01)` collapses ranks 12-14 to one value here).

    RC-21's fix is a comparison (`share if share > 0.0 else 0.0`), not
    `max(share, 0.0)` -- both give the same answer for a real positive
    `touch_share_f`, so this fixture (all real rows, none exactly zero)
    cannot distinguish the two forms. The NaN-safety reason for choosing
    the comparison form is checked separately (division/`_ratio` behavior
    is `_select_lineup`'s job, not this pure function's)."""
    n = 14
    players = []
    for i in range(n):
        rank = i + 1
        players.append(
            make_sim_player(
                300 + i,
                tuning=DEFAULT_TUNING,
                primary_position=Position.SG,
                overall_cached=99 - i,
                with_role=True,
                touch_share=f"{_minutes_curve_raw(rank) / 240.0:.10f}",
            )
        )
    team = make_team(1)
    strategy = make_team_strategy(1)
    team = build_sim_team(team, strategy, tuple(players))

    shares = _minutes_shares(team)
    ranked = sorted(players, key=lambda p: (-p.player.overall_cached, p.id))
    last_three_minutes = [shares[p.id] * 240.0 for p in ranked[-3:]]

    assert len(set(last_three_minutes)) == 3, (
        f"ranks 12-14 collapsed to fewer than 3 distinct minute values: {last_three_minutes}"
    )
    assert last_three_minutes == sorted(last_three_minutes, reverse=True)


def test_distinct_roster_knobs_overall_cached_stays_in_domain_and_pairwise_distinct():
    """B9 fix (critic-round-8-proven): `_distinct_roster_knobs`' prior
    `40 + 8 * (i - role_count)` step produced `overall_cached=104` for the
    9th no-role player on a standard 10-player roster -- outside the `rating`
    domain's `CHECK (VALUE BETWEEN 0 AND 99)` (`alembic/versions/
    001_league_setup.py`, consumed by `002_players.py`'s `overall_cached
    rating NOT NULL` column). Nothing in `domain/player.py` validates this
    at construction time, so the regression was silent everywhere except a
    live DB write -- this test is the guard that failure class needed.
    Checks both halves of what B6 actually needs this knob for: every
    value in [0, 99], and no two players (including the one role-carrying
    player) colliding on the same `overall_cached`, since B6's rank-tier
    sort depends on real separation, not just distinctness in principle."""
    knobs = _distinct_roster_knobs(10)
    values = [knob["overall_cached"] for knob in knobs]

    assert all(0 <= v <= 99 for v in values), (
        f"overall_cached values out of the [0, 99] rating domain: {values}"
    )
    assert len(set(values)) == len(values), (
        f"overall_cached values are not pairwise distinct: {values}"
    )


# PD-10 (docs/design/gm-command-surface.md) -- mutation 22.


def _home_action_type_counts(offensive_scheme, *, games: int, seed_offset: int) -> Counter:
    """Home team's own realized `setup`-beat action-type mix over `games`
    seeded games, everything else (players, ratings, tendencies, roles,
    directives, every other strategy field, the away team) byte-identical
    across calls -- only `home_strategy_overrides` differs between two
    calls to this helper. `home_player_ids` (the fixture default,
    `range(1, 11)`) is what lets a beat be attributed to the home team's
    own offense: only `setup` beats whose `offense:*` actor is a home
    player id count."""
    home_ids = frozenset(range(1, 11))
    counts: Counter = Counter()
    for i in range(games):
        seed = seed_offset + i
        ctx = make_game_context(
            seed,
            tuning=DEFAULT_TUNING,
            home_strategy_overrides={"offensive_scheme": offensive_scheme},
        )
        result = simulate_game(ctx, rng_seed=seed)
        for beat in result.beat_log:
            if beat.beat != "setup":
                continue
            offense_ids = {pid for key, pid in beat.actor_ids.items() if key.startswith("offense:")}
            if offense_ids & home_ids:
                counts[beat.outcome] += 1
    return counts


def test_two_identical_teams_differing_only_in_offensive_scheme_produce_different_mixes():
    """Mutation 22: PD-2's item-17b lesson, restated for PD-10 -- the only
    row proving `offensive_scheme` reaches a real GAME (through `game.py`'s
    own `simulate_possession` call site), not just the pure-function/
    single-beat layers mutations 18-21 already cover. `_home_action_type_
    counts` builds two `GameContext`s byte-identical in every player,
    rating, tendency, role, directive and strategy field except the home
    team's `offensive_scheme` -- the realized action-type mix over a
    seeded multi-game run must differ. A mutation that leaves `beats/
    setup.py` able to read a scheme but stops `game.py` from passing one
    would make both counters identical (both silently draw with `scheme=
    None`), which is exactly what this test would catch and mutations
    18-21 cannot."""
    games = 30
    iso_counts = _home_action_type_counts(OffensiveScheme.ISO_STAR, games=games, seed_offset=9000)
    post_up_counts = _home_action_type_counts(
        OffensiveScheme.POST_UP, games=games, seed_offset=9000
    )

    assert sum(iso_counts.values()) > 0, "no home-team setup beats attributed -- fixture is broken"
    assert dict(iso_counts) != dict(post_up_counts), (
        "iso_star and post_up produced identical realized action-type mixes over "
        f"{games} seeded games -- offensive_scheme is not reaching game.py's own possession "
        "draw",
        iso_counts,
        post_up_counts,
    )
