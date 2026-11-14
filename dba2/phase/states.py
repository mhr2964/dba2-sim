"""The `Phase` enum — canonically declared in `dba2.domain.phase`, not here.

Decision 4 (phase-0-foundation.md §3) requires `domain/` to import nothing
from `dba2` except `core`, so the enum a status column needs has to live
somewhere I/O-free that `phase/` can depend on. `phase/` legitimately does
I/O (`service.py`); `domain/` never does. Declaring `Phase` in `domain/` and
importing it upward into `phase/` is the only direction that satisfies both
constraints without a circular import (the reverse — `domain/` importing
`phase/` — would violate Decision 4 directly). Re-exported here so callers
following §3's file layout (`from dba2.phase.states import Phase`) get the
same type without needing to know where it's actually declared.
"""
from __future__ import annotations

from dba2.domain.phase import Phase

__all__ = ["Phase"]
