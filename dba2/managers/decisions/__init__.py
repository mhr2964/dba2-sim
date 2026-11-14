"""GM decision-makers -- one module per decision domain (trade/signing/
draft/waivers), each a pure function of already-loaded `managers.snapshot`
types that emits a `managers.explain.DecisionRecord` (docs/design/phase-2-
manager-minds.md §3: "`decisions/` earned its folder the same way `sim/
beats/` did," nesting appears at exactly one level). `waivers.py` (T7a2,
docs/ledger/reports/phase-2-architect-round-8.md §7) is the fourth module.

Zero I/O, per Decision P2-2's purity extension -- same rules as every
other `managers/` module.
"""
from __future__ import annotations
