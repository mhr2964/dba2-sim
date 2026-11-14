"""Rebuild a `BeatRecord` from one line of the opt-in beat-log JSONL
(`game_sim_service._write_beat_log`), so a test can prove the file is a
faithful round trip of the in-memory record -- the direction that matters,
since `dataclasses.asdict` -> `json.dumps` is the writer and nothing in
`dba2/` reads the file back.
"""
from __future__ import annotations

from typing import Any

from dba2.sim.events import BeatRecord, TerminalKind, TerminalOutcome, Zone


def terminal_from_json(raw: dict[str, Any] | None) -> TerminalOutcome | None:
    if raw is None:
        return None
    fields = dict(raw)
    fields["kind"] = TerminalKind(fields["kind"])
    if fields["zone"] is not None:
        fields["zone"] = Zone(fields["zone"])
    return TerminalOutcome(**fields)


def beat_record_from_json(raw: dict[str, Any]) -> BeatRecord:
    fields = dict(raw)
    fields["terminal"] = terminal_from_json(fields["terminal"])
    fields["possession_credits"] = tuple(
        terminal_from_json(credit) for credit in fields["possession_credits"]
    )
    return BeatRecord(**fields)
