"""A behaviour-neutrality guard for engine edits that only ADD data to the
beat log (watch-v2 E-1, docs/design/watch-v2-scrollable-broadcast.md).

Why this exists next to `test_determinism.py`'s seed-777 golden: one seed is
one path through the engine. An edit that must not change a single random
draw, score or box accumulation needs a wider net taken BEFORE the edit, so
"same" means "same as the engine that existed before the change", not "same
as whatever the changed engine now produces". The fixture
(`tests/fixtures/golden/game_box_score_seeds_0_49.jsonl`, one seed per line)
was generated from the unmodified engine in its own commit, before the
engine change landed.

Per seed it stores every value the box score is built from -- final score,
period scores, overtime count and every player and team box line -- and
deliberately NOT the beat log (a beat-log addition is exactly what this
guards against needing to re-baseline). Like the seed-777 golden it uses
`make_game_context`, so it proves the ENGINE, not the service layer.

Regenerating it is a deliberate act tied to a tuning change and an
`engine_version` bump, never something to do to make this test pass.

Widened from 30 to 50 seeds (assist-compression fix): the old 0-29 net's
two overtime seeds (6, 8) both fell back to regulation under the new
`SetupTuning.ball_handler_passing_ability_anchor_*` weight (ball-handler
role draws shifted, which shifts late-game possession outcomes even for a
game that never itself hits the ball-handler-passing-ability edge case) --
0-29 alone no longer contained an overtime game to regenerate onto.
Scanning seeds 0-59 under that tuning found exactly one (48); 0-49 is the
smallest contiguous extension of the original net that still contains it,
so the net widened rather than swapping to a non-contiguous seed set.

Regenerated again (BDA-4 fix, docs/design/block-defender-attribution.md):
the weak-side rim-help contest shifts resolution-leaf shares on effectively
every rim-zone shot, which -- same mechanism as the assist-compression note
above -- moves late-game score margins across most seeds, and did so again
mid-calibration as `resolution_rim_help.base_rate`/`scale` were solved
against `blk_per_game`/`top_shot_blocker_bpg` (seed 37 briefly worked, then
stopped once the constants reached their shipped values). Seed 48 fell
back to regulation under the new tuning; seed 36 (also inside 0-49, so the
net does not need widening again) is the fixture's overtime game now,
verified against the shipped calibration, not a mid-flight one.

Regenerated again (RC-4/RC-16 fix, docs/design/auto-gm-role-construction.md,
2026-09-23): `_minutes_shares`' minutes model itself changed -- the flat
5/3/rest tier split was replaced by a 15-entry per-absolute-rank curve, and
roleless players are now ranked within the full roster rather than the
roleless subset alone. Every player's minutes target moves, so this is not
a "same engine, added data" edit this guard exists to check -- it is a real
tuning change, landing in the same commit as the `TUNING_VERSION` bump
(9 -> 10). Seed 36 fell back to regulation under the new curve; seeds 5 and
39 are the fixture's overtime games now (two, not one -- both retained
rather than re-narrowing the net back to a single OT seed).

Regenerated again (PD-10, docs/design/gm-command-surface.md, 2026-09-23):
`beats/setup.py::_action_type_weights` now reads `team_strategy.
offensive_scheme` (default fixture value `OffensiveScheme.MOTION` for both
teams, `tests/fixtures/sim.py::make_team_strategy`), and unlike
`three_point_bias`/`shot_diet`/`usage_mode`, `OffensiveScheme` has no neutral
member (PD-10.4) -- every real `TeamStrategy` row, including this fixture's,
was ALWAYS non-neutral for this knob, so every one of this net's 50 games
moves, not just late-game margins on some of them. Neither of the two
existing overtime seeds (5, 39) reaches overtime under PD-10 alone.

Regenerated a second time in the same dispatch (concurrent, independently
committed work landing on the same branch): `1b655fe` (RC-20, rescaling
`touch_share` out of role selection) and `bccb7c8`
(`docs/design/per-36-spread-compression.md` Commit 2, raising the rebound
attribution draw's `ability_score` to an exponent) both also move every
seed's late-game margin. The combined effect reopens an overtime seed
inside the existing 0-49 net -- seed 41, scanned 0-299 to confirm it is the
only one below 50 -- so the net does **not** need widening this time; only
regenerating onto it.

Regenerated again (per-36-spread-compression Commit 4 / M1, TUNING_VERSION
17 -> 18): `_MINUTES_CURVE` ranks 1-5 re-anchored (33.2 -> 35.5), which moves
every player's minutes target and so every seed. Seed 41 fell back to
regulation; seeds 13 and 48 are the fixture's overtime games now (scanned
0-49 at HEAD + this change; the net does not need widening).

Regenerated again (docs/design/pick-and-pop.md, TUNING_VERSION 18 -> 19 -> 20):
the screener's beat-4 branch now splits into roll and pop legs, which moves
every `pnr_ball`/`dho` possession that reaches a help-committed read. The
first regeneration (the un-calibrated wiring, 19) left **no seed in 0-49
reaching overtime** (scanned 0-149: 68, 71, 99 do); the calibrated ceiling
(20, `pop_share_anchor_high`'s share 0.75 -> 0.50) put one back -- seed 18,
scanned 0-149 to confirm the overtime seeds are 18, 68 and 71 -- so the net
did **not** need widening in the end; only regenerating onto it.

Regenerated again (docs/design/turnover-recovery.md, TUNING_VERSION 20 -> 21):
the resolution beat's live-turnover mass now splits with a recovered-loose-
ball leaf, which moves every possession that reaches the turnover split --
i.e. every seed. **No seed in 0-49 reaches overtime** (scanned 0-199: only
146 and 158 do), so, as pick-and-pop C3 did, the smallest overtime seed
(146) is appended to the 0-49 window rather than widening it. The fixture
therefore holds seeds 0-49 plus 146; the file name keeps its historic
`0_49`.

Regenerated again (docs/design/turnover-recovery.md TR-4.7 as revised
2026-09-24, TUNING_VERSION 21 -> 22): the recovering player is now a weighted
draw, one extra `rng` draw per recovery, which re-realizes every game from its
first recovery on. Seed 27 now reaches overtime (scanned 0-399: 27, 293, 318,
320, 359), so the 146 append is dropped and the fixture is seeds 0-49 again --
the same reversal pick-and-pop C4 made.

Regenerated twice more by docs/design/block-attribution-v2.md: Commit 1
(TUNING_VERSION 23 -> 24, the share model at unchanged constants; no seed in
0-49 reached overtime there) and Commit 2 (24 -> 25, calibration). At 25 seed
21 reaches overtime (scanned 0-99: 21 only), so the fixture stays 0-49.

Regenerated once more by the block-attribution-v2 Correction (2026-09-24,
TUNING_VERSION 25 -> 26, `resolution_rim_help.scale` 12 -> 13): seed 12 now
reaches overtime (scanned 0-99: 12 only; 0-199: 12, 103, 159, 168, 177), so
the fixture stays 0-49."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context

_GOLDEN_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / (
    "game_box_score_seeds_0_49.jsonl"
)
_GAME_ID = 12345
_SEEDS = tuple(range(50))


def _payload(seed: int) -> dict:
    """One seed's checked-in shape. Round-tripped through JSON with sorted
    keys so tuples compare as lists and float repr is exactly what the
    fixture holds."""
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    result = simulate_game(ctx, rng_seed=seed)
    payload = {
        "seed": seed,
        "home_team_id": result.home_team_id,
        "away_team_id": result.away_team_id,
        "home_score": result.home_score,
        "away_score": result.away_score,
        "overtime_periods": result.overtime_periods,
        "period_scores": [list(p) for p in result.period_scores],
        "player_box_lines": [dataclasses.asdict(p) for p in result.player_box_lines],
        "team_box_lines": [dataclasses.asdict(t) for t in result.team_box_lines],
    }
    return json.loads(json.dumps(payload, sort_keys=True))


def _load_golden() -> dict[int, dict]:
    lines = _GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
    golden = {}
    for line in lines:
        row = json.loads(line)
        golden[row["seed"]] = row
    return golden


def test_fixture_holds_exactly_seeds_0_through_49():
    """A truncated or reordered fixture would silently shrink the net."""
    golden = _load_golden()
    assert sorted(golden) == list(_SEEDS)
    assert len(_GOLDEN_PATH.read_text(encoding="utf-8").splitlines()) == len(_SEEDS)


def test_fixture_covers_regulation_and_overtime_games():
    """The net must span an overtime game, or it never exercises the
    period-5 path an engine edit could disturb."""
    overtimes = {row["overtime_periods"] for row in _load_golden().values()}
    assert 0 in overtimes
    assert any(o > 0 for o in overtimes)


@pytest.mark.parametrize("seed", _SEEDS)
def test_seed_matches_the_pre_change_golden(seed: int):
    golden = _load_golden()[seed]
    actual = _payload(seed)
    assert actual == golden, (
        f"seed {seed}: the simulated score / period scores / overtime count / box lines "
        "no longer match the fixture taken from the pre-change engine. An engine edit "
        "that only adds data must not move any of them; if this is an intentional "
        "tuning change, regenerate in the same commit that bumps the engine version."
    )
