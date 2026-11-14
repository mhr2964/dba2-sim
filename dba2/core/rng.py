"""Seeded RNG helper.

Decision 7 (phase-0-foundation.md): reproducibility has to be rooted in
persisted state (`leagues.rng_seed`, `games.rng_seed`), not a process-local
seed — otherwise replaying a possession after a restart produces different
basketball. Nothing in `sim/` should ever call `random.Random()` bare;
everything derives from a stored seed plus a salt describing *which* stream
(a specific game, a specific possession index) is being requested.
"""
from __future__ import annotations

import hashlib
import random


def rng_for(seed: int, *salt: int | str) -> random.Random:
    """Return a `random.Random` deterministically derived from `seed` + `salt`.

    Same seed + same salt tuple always yields the same stream. Different
    salt (e.g. a game id, or a game id plus a possession index) yields an
    independent stream without needing a separate persisted seed per
    context — the derivation itself is the isolation.
    """
    parts = "|".join((str(seed), *(str(s) for s in salt)))
    digest = hashlib.sha256(parts.encode("utf-8")).digest()
    derived_seed = int.from_bytes(digest[:8], "big")
    return random.Random(derived_seed)
