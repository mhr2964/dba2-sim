"""`dba2/agency/` -- Pillar 5's pure evaluator package (Phase 4 §11 Stage B).

docs/design/phase-4-media-and-player-agency.md §2.2, Decision P4-8.

Zero I/O, the same denylist `dba2/managers/` already carries
(`tests/unit/test_layering.py`): no `dba2.data`, no `datetime.now()`, no
bare `random.Random(`, no `open(`, no `os.environ`, no
`dba2.sim.game`/`possession`/`beats`. May import `dba2.domain`,
`dba2.core`, and `dba2.managers` (reading `NeedMatrix`/`CompetitiveWindow`
as types only, per P4-8) -- never `dba2.data`, and never
`dba2.managers.decisions.signing` (Decision P4-52's directed rule).

`dba2.managers` may NOT import this package (P4-8's directed rule) --
a GM valuation that reads a player's mood would put player agency inside
the byte-identity digest's blast radius through the back door.
"""
from __future__ import annotations
