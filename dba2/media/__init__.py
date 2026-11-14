"""`dba2/media/` -- Pillar 4's pure media-vocabulary/selection package
(Phase 4 §11 Stage D).

docs/design/phase-4-media-and-player-agency.md §2.1/§6, Decision P4-31.

Zero I/O, the same denylist `dba2/managers/`/`dba2/agency/` already carry
(`tests/unit/test_layering.py`): no `dba2.data`, no `datetime.now()`, no
bare `random.Random(`, no `open(`, no `os.environ`. May import
`dba2.domain`, `dba2.core`, `dba2.managers` (types) and `dba2.agency`
(types) -- never `dba2.data`.

**Nothing under `dba2/` may import this package except `dba2/services/
media_service.py`** (Decision P4-31's one-way valve) -- media is a
one-way consumer of league state; in particular `dba2.agency` may not
import `dba2.media`, the direction that matters (media pressure feeding
player unhappiness would be the single most tempting feedback loop in
this design).

Real content lands at Stage D (step 15 extracted the frame-agnostic
selection core into `managers/voice/engine.py`; step 16 -- this package's
first real module, `frame.py` -- adds `StoryFrame`/`StoryKind`).
"""
from __future__ import annotations
