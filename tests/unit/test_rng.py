"""`core.rng.rng_for` determinism (phase-1-simulation-engine.md §12 step 1's
verify bullet: "unit tests for ... rng_for determinism"). `rng_for` itself
shipped in Phase 0; it had no dedicated unit test until Phase 1's engine
made it load-bearing for every possession's reproducibility (§6)."""
from __future__ import annotations

from dba2.core.rng import rng_for


def test_same_seed_and_salt_yields_the_same_stream():
    a = rng_for(12345, "game", 7)
    b = rng_for(12345, "game", 7)
    assert [a.random() for _ in range(20)] == [b.random() for _ in range(20)]


def test_different_salt_yields_an_independent_stream():
    a = rng_for(12345, "game", 7)
    b = rng_for(12345, "game", 8)
    assert [a.random() for _ in range(20)] != [b.random() for _ in range(20)]


def test_different_seed_yields_an_independent_stream():
    a = rng_for(12345, "game", 7)
    b = rng_for(67890, "game", 7)
    assert [a.random() for _ in range(20)] != [b.random() for _ in range(20)]


def test_salt_order_matters():
    """`rng_for` joins salt parts positionally -- (1, 2) and (2, 1) must
    not collide, since a possession's salt tuple is order-sensitive
    (game id, then possession index)."""
    a = rng_for(1, 1, 2)
    b = rng_for(1, 2, 1)
    assert [a.random() for _ in range(10)] != [b.random() for _ in range(10)]
