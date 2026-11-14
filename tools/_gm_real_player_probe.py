"""Throwaway coverage probe for docs/design/real-player-data-integration.md's
RPD-1/RPD-7 open item 1 -- NOT part of the build, not imported by anything.
Answers one question empirically before any ETL code gets written against
an assumed boundary: does BallDontLie's advanced-stats endpoint
(`/nba/v2/stats/advanced`, GOAT tier) actually return data for the
1996-97 and 2013-14 seasons, the two boundaries the design doc assumes
from general NBA data-history knowledge but never checked live (the
OpenAPI spec itself states no coverage floor).

Reads the key from the `BDL_API_KEY` environment variable -- never
hardcode a real key into this file or pass one on the command line
(shows up in shell history / process lists). Uses stdlib `urllib` only;
this project has no `requests`/`httpx` dependency and a one-shot probe
script isn't a reason to add one.

Usage (from the user's own shell, key never leaves it):
    export BDL_API_KEY="..."
    python tools/_gm_real_player_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_BASE = "https://api.balldontlie.io/nba/v2/stats/advanced"
_SEASONS_TO_CHECK = {
    1996: "RPD-1's advanced-stats floor (1996-97 season)",
    2013: "RPD-1's tracking-era boundary (2013-14 season)",
    2015: "RPD-7's hustle-stat sub-boundary (2015-16 season)",
}


def _check_season(api_key: str, season: int) -> None:
    url = f"{_BASE}?seasons[]={season}&per_page=5"
    request = urllib.request.Request(url, headers={"Authorization": api_key})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"season={season}: HTTP {exc.code} -- {exc.read()[:200]!r}")
        return
    rows = payload.get("data", [])
    label = _SEASONS_TO_CHECK[season]
    if rows:
        sample = rows[0]
        print(f"season={season} ({label}): {len(rows)} rows returned, e.g. {sample.get('player', sample)!r}")
    else:
        print(f"season={season} ({label}): ZERO rows -- boundary assumption is wrong for this season")


def main() -> int:
    api_key = os.environ.get("BDL_API_KEY")
    if not api_key:
        print("Set BDL_API_KEY in your own shell first (see this file's docstring).", file=sys.stderr)
        return 1
    for season in _SEASONS_TO_CHECK:
        _check_season(api_key, season)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
