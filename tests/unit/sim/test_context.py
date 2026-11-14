"""Tier 1: `SimPlayer`/`SimTeam` construction -- the `Decimal` -> `float`
boundary (Decision P1-5) and the construction-time caches (Decision
P1-4/P1-6/P1-13)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from dba2.domain.player import Position
from dba2.sim.composites import (
    normalize_physical,
    rim_protection_contribution,
    scheme_contribution,
    spacing_contribution,
)
from dba2.sim.context import build_sim_player, build_sim_team
from dba2.sim.probability import ability_score
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import (
    make_player,
    make_ratings,
    make_role,
    make_team,
    make_team_strategy,
    make_tendencies,
)


def test_build_sim_player_converts_decimal_touch_share_and_confidence_to_float():
    player = make_player(1)
    ratings = make_ratings(1)
    tendencies = make_tendencies(1)
    role = make_role(1, touch_share="0.31")
    sim_player = build_sim_player(player, ratings, tendencies, DEFAULT_TUNING, role=role)

    assert isinstance(sim_player.touch_share_f, float)
    assert sim_player.touch_share_f == pytest.approx(0.31)
    assert isinstance(sim_player.confidence_f, float)
    assert sim_player.confidence_f == pytest.approx(float(ratings.confidence))
    # The raw Decimal-typed domain field is still reachable by reference
    # (Decision P1-5 doesn't hide the domain objects, only mandates a
    # mirrored float for engine math) -- confirms this is a mirror, not a
    # destructive replace.
    assert isinstance(ratings.confidence, Decimal)
    assert isinstance(role.touch_share, Decimal)


def test_build_sim_player_leaves_touch_share_none_when_no_role_row_exists():
    player = make_player(2)
    ratings = make_ratings(2)
    tendencies = make_tendencies(2)
    sim_player = build_sim_player(player, ratings, tendencies, DEFAULT_TUNING, role=None)
    assert sim_player.touch_share_f is None


def test_build_sim_player_caches_match_the_pure_composite_functions():
    player = make_player(3, wingspan_in=86, height_in=79)
    ratings = make_ratings(3)
    tendencies = make_tendencies(3)
    sim_player = build_sim_player(player, ratings, tendencies, DEFAULT_TUNING)

    assert sim_player.wingspan_normalized == normalize_physical(
        86, DEFAULT_TUNING.physical_anchors.wingspan
    )
    assert sim_player.height_normalized == normalize_physical(
        79, DEFAULT_TUNING.physical_anchors.height
    )
    assert sim_player.spacing_contribution == spacing_contribution(ratings, DEFAULT_TUNING)
    assert sim_player.scheme_contribution == scheme_contribution(ratings, DEFAULT_TUNING)


def test_rim_protection_contribution_reads_the_rim_help_vector():
    """docs/design/block-attribution-v2.md BA-2.4: the cached scalar the
    rim-help pool reads for each helper's share *and* contest must be the
    hand-computed weighted mean of `resolution_rim_help.defense_weights`
    (until v2 it read `resolution_block`'s, so the player the floor sent
    contested on a different vector) -- including the `wingspan_in` honorary
    exception (Decision P1-13), which `_weighted_mean_of_ratings` alone
    can't resolve since `wingspan_in` isn't a `PlayerRatings` field. The
    fixture's ratings differ across the two vectors' fields, so reading the
    block vector fails here."""
    player = make_player(6, wingspan_in=90)
    ratings = make_ratings(6)
    tendencies = make_tendencies(6)
    sim_player = build_sim_player(player, ratings, tendencies, DEFAULT_TUNING)

    weights = DEFAULT_TUNING.resolution_rim_help.defense_weights
    wingspan_normalized = normalize_physical(90, DEFAULT_TUNING.physical_anchors.wingspan)
    expected = sum(
        (wingspan_normalized if field == "wingspan_in" else getattr(ratings, field)) * w
        for field, w in weights.items()
    ) / sum(weights.values())

    assert sim_player.rim_protection_contribution == pytest.approx(expected)
    assert sim_player.rim_protection_contribution == pytest.approx(
        rim_protection_contribution(ratings, wingspan_normalized, DEFAULT_TUNING)
    )
    # The same number the live `ability_score` path would compute: one vector.
    assert sim_player.rim_protection_contribution == pytest.approx(
        ability_score(sim_player, weights)
    )
    block_weights = DEFAULT_TUNING.resolution_block.defense_weights
    assert sim_player.rim_protection_contribution != pytest.approx(
        ability_score(sim_player, block_weights)
    )


def test_build_sim_player_distinct_players_get_distinct_caches():
    """Fixture-discipline check: two players built with genuinely
    different ratings/physicals must not collapse onto the same cached
    scalars -- the same class of bug ("same-value fixture hides a mapper
    bug") this project has hit four times in the repo layer, here aimed
    at the sim layer's own construction path instead of a SQL mapper."""
    a = make_player(4, wingspan_in=74, height_in=73, primary_position=Position.PG)
    b = make_player(5, wingspan_in=92, height_in=85, primary_position=Position.C)
    sim_a = build_sim_player(a, make_ratings(4), make_tendencies(4), DEFAULT_TUNING)
    sim_b = build_sim_player(b, make_ratings(5), make_tendencies(5), DEFAULT_TUNING)

    assert sim_a.wingspan_normalized != sim_b.wingspan_normalized
    assert sim_a.height_normalized != sim_b.height_normalized
    assert sim_a.spacing_contribution != sim_b.spacing_contribution
    assert sim_a.scheme_contribution != sim_b.scheme_contribution
    assert sim_a.rim_protection_contribution != sim_b.rim_protection_contribution


def test_build_sim_team_composes_team_strategy_and_players():
    team = make_team(10)
    strategy = make_team_strategy(10)
    players = (
        build_sim_player(
            make_player(1, team_id=10), make_ratings(1), make_tendencies(1), DEFAULT_TUNING
        ),
        build_sim_player(
            make_player(2, team_id=10), make_ratings(2), make_tendencies(2), DEFAULT_TUNING
        ),
    )
    sim_team = build_sim_team(team, strategy, players)
    assert sim_team.id == 10
    assert sim_team.player_by_id(1) is players[0]
    with pytest.raises(KeyError):
        sim_team.player_by_id(999)
