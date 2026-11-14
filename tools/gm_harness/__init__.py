"""In-memory GM validation harness (docs/design/phase-2-manager-minds.md
§12 step 10, `docs/ledger/reports/phase-2-architect-round-7.md`) -- a
synthetic-league generator (`league.py`) plus mutable state, snapshot/ctx
rebuilders, and five in-memory appliers (including `apply_waiver_inmem`,
T7a2's own later addition) mirroring `services/gm_service.py`'s real,
Postgres-backed ones (`state.py`), built so §10's 22-metric
exit criterion can run against a few hundred seeded leagues without ever
touching Postgres (`season.py`/`metrics.py`/`bands.py`, T7a/T7b, are not
built this slice).

**Not under the `tools/_gm_draft_class_stub.py`/`tools/_gm_progression_
stub.py` quarantine convention** (Decision P2-23/P2-32) -- this package
generates starting state, the same job `tools/seed_dev_league.py` and
`tools/validate_sim.py::generate_league` already do un-prefixed with no
quarantine test; see the architect round-7 report §4.1 for the full
reasoning (that quarantine exists to stop semantic confusion about which
phase *owns* progression/real-draft-class modeling -- a starting-state
generator was never in that category). Still enforced against `dba2/`
reachability by its own dedicated test
(`tests/unit/test_layering.py::
test_nothing_under_dba2_imports_the_gm_harness`), since this package
imports both quarantined stubs transitively (via `season.py`, T7a) and it
would otherwise be cheap for a future `dba2/` module to reach into it for
a convenient fixture.

Zero I/O beyond loading the static `seeds/*.json` files at generation
time (`league.py`'s own module docstring) -- no Postgres, mirroring
`managers/`'s purity convention even though `tools/` is not formally
bound by it.
"""
from __future__ import annotations
