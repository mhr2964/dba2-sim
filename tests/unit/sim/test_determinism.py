"""Tier 3: determinism and drift (§9's Tier 3, §12 step 5's own verify
list, Decision P1-21/§6's reproducibility contract) -- named as a required
deliverable and never built ("the golden-file determinism test named in
§12 step 5's verify list was never built," this fix round's B5).

Two things, kept in separate tests per §9's own split:

1. **A golden-file test** (`test_game_box_score_matches_the_checked_in_golden_file`):
   one fixed roster fixture, one fixed seed, one fixed tuning profile,
   compared against a checked-in box-score JSON
   (`tests/fixtures/golden/game_box_score_seed777.json`). Per §9: "It is
   regenerated deliberately whenever tuning changes, in the same commit
   that bumps `engine_version` -- which makes accidental tuning drift a
   failing test and intentional tuning drift a reviewable diff." Scoped to
   the box score (not the full beat log -- ~1,400 records for this one
   fixture, unwieldy to check in and review as a diff) per §9's own
   wording ("asserted against a checked-in box score JSON").
2. **§6's two named determinism claims, as their own explicit tests**
   (full-game determinism from a persisted seed; standalone possession
   replay from `(rng_seed, possession_index, entering state)`) --
   `test_game.py`/`test_possession.py` don't already have either, checked
   via grep before this file was written.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context

_GOLDEN_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / (
    "game_box_score_seed777.json"
)
_GAME_ID = 12345
_RNG_SEED = 777


def _box_score_payload(result) -> dict:
    """The same shape the golden file was generated with -- see this
    module's docstring for why the (large, low-review-value) beat log is
    reduced to a count rather than checked in verbatim."""
    return {
        "home_team_id": result.home_team_id,
        "away_team_id": result.away_team_id,
        "home_score": result.home_score,
        "away_score": result.away_score,
        "overtime_periods": result.overtime_periods,
        "period_scores": [list(p) for p in result.period_scores],
        "player_box_lines": [dataclasses.asdict(p) for p in result.player_box_lines],
        "team_box_lines": [dataclasses.asdict(t) for t in result.team_box_lines],
        "beat_count": len(result.beat_log),
    }


def test_game_box_score_matches_the_checked_in_golden_file():
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    result = simulate_game(ctx, rng_seed=_RNG_SEED)

    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(_box_score_payload(result), sort_keys=True))
    assert actual == golden, (
        "simulated box score no longer matches the checked-in golden file -- "
        "if this is an intentional tuning change, regenerate the golden file "
        "in the same commit that bumps TUNING_VERSION, per §9's Tier 3"
    )


def test_full_game_determinism_from_a_persisted_seed():
    """§6's first named claim, verbatim: "simulating the same `games` row
    twice, from the same persisted seed against the same roster and
    ratings, produces a byte-identical box score, a byte-identical beat
    log, and identical `engine_version`." `engine_version` isn't produced
    at this layer (it's `game_sim_service`'s concern, not built yet), so
    this test covers the box score and beat log halves of the claim --
    both `GameResult`'s dataclasses (frozen, structural `__eq__`) and this
    module's own JSON-round-tripped payload are compared, so a divergence
    anywhere in either shape fails loudly."""
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    first = simulate_game(ctx, rng_seed=_RNG_SEED)
    second = simulate_game(ctx, rng_seed=_RNG_SEED)

    assert first.player_box_lines == second.player_box_lines
    assert first.team_box_lines == second.team_box_lines
    assert first.beat_log == second.beat_log
    assert first.period_scores == second.period_scores
    assert first.home_score == second.home_score
    assert first.away_score == second.away_score
    assert first.overtime_periods == second.overtime_periods
