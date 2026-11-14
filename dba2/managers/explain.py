"""`ReasonTerm` / `DecisionRecord` -- the "no black box" artifact
(docs/design/phase-2-manager-minds.md §9.1, Decision P2-18).

Pillar 2: "No black box -- every AI decision should be explainable by
pointing at the inputs." A promise in a docstring is decoration; a
reconstruction identity is a test -- `DecisionRecord.reconstructs()`
below is what a caller (and `tests/unit/test_explain.py`, not built this
dispatch) asserts against, per §12 step 1's own verify list:
"`DecisionRecord` reconstruction holds on a hand-built record."

Per §13's table and Decision P2-18 itself: `managers/` never emits
English. `ReasonTerm.label` is a stable machine identifier (e.g.
`"talent"`, `"surplus"`, `"need_bonus"`), not a sentence -- Phase 3 and
Phase 4 own turning these into negotiation dialogue and media copy.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Records are written to gitignored `artifacts/gm/` by a
caller outside this module (`services/gm_service.py`, not built this
dispatch); this module only defines the shape.
"""
from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.team import GmPhilosophy
from dba2.managers.tuning import CompetitiveWindow

_RECONSTRUCTION_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReasonTerm:
    """One line item behind a `DecisionRecord`'s score -- Decision P2-18's
    `(label, input_value, weight, contribution)` shape, verbatim."""

    label: str
    """Stable machine identifier for what this term represents (e.g.
    `"talent"`, `"age_mult"`, `"surplus"`, `"need_bonus"`) -- never a
    sentence (§13, mirroring Decision P1-23's structured-not-prose rule
    for `sim/`)."""
    input_value: float
    """The raw input this term was computed from, before `weight` is
    applied -- named separately from `contribution` so a reader can see
    both "what the input was" and "how much it moved the score.\""""
    weight: float
    """The scalar this term's `input_value` was multiplied (or otherwise
    combined) by to produce `contribution`."""
    contribution: float
    """This term's own signed addition to the record's total `score`."""


@dataclass(frozen=True)
class DecisionRecord:
    """One AI GM decision, fully accounted for -- Decision P2-18's
    `(team_id, philosophy, window, subject, verdict, score, threshold,
    terms)` shape, verbatim. `terms` must sum to `score` within
    `reconstructs()`'s tolerance; nothing downstream trusts a record that
    doesn't.
    """

    team_id: int
    philosophy: GmPhilosophy
    window: CompetitiveWindow
    subject: str
    """What this decision was about -- a player name/id, a pick, a
    package description. A stable identifier, not prose (see
    `ReasonTerm.label`'s note)."""
    verdict: str
    """The decision's outcome as a stable token (e.g. `"accept"`,
    `"reject"`, `"offer"`) -- never a sentence."""
    score: float
    threshold: float
    terms: tuple[ReasonTerm, ...]

    def reconstruction_error(self) -> float:
        """`|sum(terms) - score|` -- the quantity Family-1 invariant 5
        (§10) asserts stays within `1e-6` for every emitted record."""
        return abs(sum(term.contribution for term in self.terms) - self.score)

    def reconstructs(self, *, tolerance: float = _RECONSTRUCTION_TOLERANCE) -> bool:
        """True iff `terms` sum to `score` within `tolerance` -- Decision
        P2-18's reconstruction identity as a callable predicate, not just
        a comment promising it holds."""
        return self.reconstruction_error() <= tolerance
