"""Thin orchestration wrapper around `tools/_gm_real_player_etl.py`:
runs it once per season across a range instead of one manual invocation
per season. Quarantined tool, same convention as its sibling.

**Why this exists as its own file rather than a loop in the ETL's own
`main()`.** The ETL's `main()` takes one season and is meant to be
runnable/testable in isolation (which is how this session validated it
against 2022-23 before trusting it at scale) -- bolting a range loop
onto that would blur "does one season work" from "did the whole run
finish," which matters when a 30-season run can fail partway through on
a transient API error. This wrapper keeps those concerns separate:
one season's failure is logged and skipped, not fatal to the run.

**Not yet run for the full range this session** -- built as the next
step after the single-season (2022-23) smoke test in
`docs/design/real-player-data-integration.md`'s open-work list, item 6.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._gm_real_player_etl import (  # noqa: E402
    _ADVANCED_MIN_SEASON,
    _fetch_advanced_rows,
    _fetch_season_rows,
    _SEEDS_DIR,
    build_season_seed,
)

_LOG_PATH = _SEEDS_DIR / "_backfill_log.txt"


def _log(line: str) -> None:
    _SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp}] {line}\n")
    print(line, flush=True)


def run_season(season: int, api_key: str) -> None:
    import json

    rows = _fetch_season_rows(season, api_key)
    advanced_rows = (
        _fetch_advanced_rows(season, api_key) if season >= _ADVANCED_MIN_SEASON else []
    )
    seed = build_season_seed(season, rows, advanced_rows)
    out_path = _SEEDS_DIR / f"{season}.json"
    out_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    _log(f"season={season}: OK, {len(seed['players'])} players -> {out_path.name}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "Usage: python -m tools._gm_real_player_backfill_all <first_season> <last_season>",
            file=sys.stderr,
        )
        return 1
    first, last = int(argv[1]), int(argv[2])
    api_key = os.environ.get("BDL_API_KEY")
    if not api_key:
        print("Set BDL_API_KEY in your own shell first.", file=sys.stderr)
        return 1

    _log(f"backfill starting: seasons {first}-{last}")
    failures: list[int] = []
    for season in range(first, last + 1):
        try:
            run_season(season, api_key)
        except Exception as exc:  # noqa: BLE001 -- one bad season must not kill the run
            _log(f"season={season}: FAILED -- {type(exc).__name__}: {exc}")
            failures.append(season)
    _log(f"backfill done: {last - first + 1 - len(failures)} ok, failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
