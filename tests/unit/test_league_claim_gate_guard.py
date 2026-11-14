"""`check_claim`'s optional gate (phase-5-stage-e-advance.md P5-99.t.1).

The four settings setters open no gate, so they call `check_claim(gate=None,
require_phase_agreement=False)`. The default `require_phase_agreement=True`
with no gate has nothing to agree with; it must be refused as a programming
error at the first line, not surface as an `AttributeError` on `gate.phase`
after a database read, in a refusal path a person can reach.
"""

from __future__ import annotations

import pytest

from dba2.services import league_claim


async def test_no_gate_with_phase_agreement_on_raises_value_error_before_any_read():
    # `conn=None` is the assertion's teeth: a guard that ran after the read
    # would fail here with an AttributeError on the connection instead.
    with pytest.raises(ValueError, match="gate"):
        await league_claim.check_claim(None, gate=None, league_id=1)


async def test_no_gate_with_phase_agreement_explicitly_on_raises_value_error():
    with pytest.raises(ValueError, match="gate"):
        await league_claim.check_claim(
            None, gate=None, league_id=1, require_phase_agreement=True
        )
