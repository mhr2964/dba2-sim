"""`game_sim_service._write_beat_log` writes `dataclasses.asdict(record)` per
beat. Since watch-v2 E-1 a `BeatRecord` nests `TerminalOutcome` dataclasses
(with `TerminalKind`/`Zone` `StrEnum`s) on the last record of each
possession, so the file's shape changed even though the writer did not.
This proves the JSON still dumps and round-trips with the payload intact.

`_write_beat_log` writes under a relative `artifacts/` path, so each test
runs from a temp directory.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from dba2.services.game_sim_service import _write_beat_log
from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.beat_log import beat_record_from_json
from tests.fixtures.sim import make_game_context

# Seed 12 goes to overtime (see test_pre_change_golden_seeds_0_49; was
# seed 6 until the assist-compression fix, then seed 48, then 37, then 36,
# then 5 until the RC-4/RC-16 minutes-curve fix (auto-gm-role-construction.md)
# changed who's on the floor late; PD-10 (gm-command-surface.md) and the
# concurrently-landed RC-20/per-36-spread-compression rebound fix then both
# moved every seed's late-game margin again, landing on seed 41; per-36-
# spread-compression Commit 4 (M1, re-anchoring ranks 1-5 a second time,
# 2026-09-23) moved it once more, to seed 13; pick-and-pop C3
# (TUNING_VERSION 18 -> 19) moved it again, to seed 68; its C4
# calibration (19 -> 20) moved it to seed 18; turnover-recovery (TUNING_VERSION
# 20 -> 21) moved it to seed 146; the weighted recoverer draw (21 -> 22)
# moved it to seed 27; block-attribution-v2 (TUNING_VERSION 23 -> 25) moved it to seed 21 (scanned
# 0-99: 21 only); the scale-13 correction (TUNING_VERSION 25 -> 26) moved it
# to seed 12 (scanned 0-99: 12 only)), so the log covers a period-5
# possession as well.
_SEED = 12


def test_written_beat_log_round_trips_with_the_terminal_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = simulate_game(make_game_context(12345, tuning=DEFAULT_TUNING), rng_seed=_SEED)
    assert result.overtime_periods > 0
    game = SimpleNamespace(league_id=7, season=2026, id=12345)

    _write_beat_log(game, result)

    path = tmp_path / "artifacts" / "beatlogs" / "7" / "2026" / "12345.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.beat_log)
    raw = [json.loads(line) for line in lines]

    # The StrEnum fields are written as their plain string values.
    with_terminal = [r for r in raw if r["terminal"] is not None]
    assert with_terminal, "no record carried a terminal -- the round trip would be vacuous"
    assert all(isinstance(r["terminal"]["kind"], str) for r in with_terminal)
    assert any(r["possession_credits"] for r in raw), "no possession credits in this game"
    assert all(
        r["possession_credits"] == [] for r in raw if r["terminal"] is None
    ), "a record with no terminal carried credits"

    assert [beat_record_from_json(r) for r in raw] == list(result.beat_log)
