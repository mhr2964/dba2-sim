"""§10's own green/red gate: sims one full season and asserts every
metric in `seeds/nba_reference_distributions.json` is inside its band.

"The roadmap says 'tune beat weights until the macro numbers hold up --
that's the check.' A check that is a green/red gate is a check; a check
that is a table someone eyeballs is a habit." (phase-1-simulation-engine.md
§10) -- this is that gate, not `tools/validate_sim.py`'s own printed
table.

**Marked `slow` and excluded from the default run** (§10's own instruction;
see `pyproject.toml`'s `addopts = "-m 'not slow'"` and its `slow` marker
registration) -- one full 1,230-game season, entirely in memory, takes
roughly 60-120s (see `tools/validate_sim.py`'s module docstring for the
measured figure), which is disqualifying for a suite meant to run on every
change but is exactly the shape of check §10 asks for as an explicit,
occasionally-run gate. Run it with `pytest -m slow` or
`pytest tests/integration/test_sim_validation.py -m slow`.

**This is expected to fail against the current (untuned) engine**, per this
dispatch's own instructions: steps 1-9 built the possession engine but
never tuned it, and step 11 (the tuning loop) is a separate, later dispatch
-- this test is the gate step 11 is written to close, not a claim that it
is already closed. Loosening these bands to make it pass now would defeat
the point of building it: see `docs/ledger/reports/
phase-1-backend-dev-round-16.md` for the current pass/fail snapshot this
test reproduces.

Needs no live Postgres, per §10's own text ("sims N full seasons headless
(default: in-memory, no persistence)") -- `tools.validate_sim` never
imports anything that opens a connection, only its pure in-memory
generation/simulation/aggregation path.
"""
from __future__ import annotations

import pytest

from dba2.sim.tuning import TuningProfile
from tools import validate_sim

# A fixed CLI rng-seed and player count, matching `tools/validate_sim.py`'s
# own CLI defaults. FIXED (critic round 23, nit 1): this test used to pass
# `_RNG_SEED` straight into `simulate_season`, while the CLI (`run_validation`)
# derives a per-replicate season seed via
# `rng_for(rng_seed, "validate_sim", "season", i).getrandbits(63)` --
# different leagues under the same nominal seed. Deriving the season seed the
# same way here makes this test genuinely reproduce the CLI's season 1 (not
# merely a coincidental agreement of band verdicts across two different
# leagues), so its numbers really are comparable to a manual
# `python -m tools.validate_sim --rng-seed 1` run.
_RNG_SEED = 1
_PLAYERS_PER_TEAM = 14
_SEASON_YEAR = 2026
_SEASON_SEED = validate_sim.rng_for(_RNG_SEED, "validate_sim", "season", 0).getrandbits(63)

_REFERENCE = validate_sim.load_reference()
_METRIC_NAMES = sorted(_REFERENCE)


@pytest.fixture(scope="module")
def season_evaluation() -> list[validate_sim.MetricResult]:
    """One full season, simmed once and shared across every parametrized
    metric assertion below -- resimming per metric would be 27x the cost
    for identical results."""
    metrics, _league, games_simmed = validate_sim.simulate_season(
        league_id=1,
        season=_SEASON_YEAR,
        rng_seed=_SEASON_SEED,
        players_per_team=_PLAYERS_PER_TEAM,
        tuning=TuningProfile(),
    )
    assert games_simmed == 1230, f"expected a full 82-game/30-team season, got {games_simmed} games"
    return validate_sim.evaluate(metrics, _REFERENCE)


@pytest.mark.slow
@pytest.mark.parametrize("metric_name", _METRIC_NAMES)
def test_metric_in_band(
    season_evaluation: list[validate_sim.MetricResult], metric_name: str
) -> None:
    result = next(r for r in season_evaluation if r.name == metric_name)
    assert result.passed is True, (
        f"{metric_name}: value={result.value} not in band "
        f"[{result.band_low}, {result.band_high}] (reference {result.reference_value})"
    )


@pytest.mark.slow
def test_every_reference_metric_was_computed(
    season_evaluation: list[validate_sim.MetricResult],
) -> None:
    """A metric with `passed is None` means `compute_metrics` never
    produced a value for it (e.g. no rotation players, an empty roster) --
    a harness bug or a degenerate run, not a calibration finding, and
    should never silently show up as neither pass nor fail."""
    missing = [r.name for r in season_evaluation if r.passed is None]
    assert not missing, f"metrics with no computed value: {missing}"
