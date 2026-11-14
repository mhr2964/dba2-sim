"""Adds a `team` field to every already-backfilled `seeds/real_players/
<season>.json` player entry -- the season-starting real team, so a real
player can be placed on their real roster at the start of any sim without
hand-picking rosters (the gap `tools/matchup_server.py` currently papers
over with `tools/historical_rosters.json`'s hand-curated lists).

**Why this doesn't need a new API integration.** `_gm_real_player_etl.py`'s
own `_fetch_season_rows` already pulls BDL's full `/v1/stats` response for
a season -- and every row in that response already carries a `team` object
(`{id, abbreviation, city, name, full_name}`) and a `game.date`, confirmed
against the BDL schema mirrored at
`Projects/knowthew/docs/design/bdl-openapi-wnba.yml`'s `WNBAPlayerStat`
definition (NBA/WNBA share the same schema family). The ETL just never
looked at either field -- it only reads `row["player"]` and the box-score
counting stats. This script re-fetches the same season rows (same
`postseason=false` filter the ratings themselves were built from, so
"team" and "ratings" agree on which games count) purely to read the two
fields the ETL discards, and merges the result into the existing seed
files rather than rebuilding them.

**Season-starting team, not "team played the most for."** For each
player, the team on their earliest-dated row that season is their
season-opening team -- exactly what a "place them on their real roster at
the start of a sim" consumer needs. A player traded in-season keeps his
October team here by design; this is not "who they ended the season
with," and nothing about it should be read as such.

Usage: BDL_API_KEY=... python -m tools._gm_real_player_team_enrich <first_season> <last_season>
Requires every season in range to already have a `seeds/real_players/
<season>.json` file (from the ETL/backfill) -- this only adds a field, it
never creates a season file from scratch.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._gm_real_player_etl import _fetch_season_rows, _SEEDS_DIR  # noqa: E402

_LOG_PATH = _SEEDS_DIR / "_team_enrich_log.txt"


def _log(line: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp}] {line}\n")
    print(line, flush=True)


def _earliest_team_by_player(rows: list[dict]) -> dict[str, dict]:
    """One `team` object per player id, taken from the row with the
    earliest `game.date` -- ties (a literal same-day double-header, which
    the real NBA schedule does not produce) resolve to whichever row the
    API returned first, since nothing downstream needs sub-day precision."""
    best_date: dict[str, str] = {}
    best_team: dict[str, dict] = {}
    for row in rows:
        player = row.get("player") or {}
        pid = str(player.get("id"))
        game = row.get("game") or {}
        date = game.get("date")
        team = row.get("team")
        if not pid or pid == "None" or not date or not team:
            continue
        if pid not in best_date or date < best_date[pid]:
            best_date[pid] = date
            best_team[pid] = {
                "id": team.get("id"),
                "abbreviation": team.get("abbreviation"),
                "city": team.get("city"),
                "name": team.get("name"),
                "full_name": team.get("full_name"),
            }
    return best_team


def enrich_season(season: int, api_key: str) -> tuple[int, int]:
    """Returns (players_enriched, players_missing_a_team)."""
    seed_path = _SEEDS_DIR / f"{season}.json"
    if not seed_path.exists():
        raise FileNotFoundError(f"no seed file for season {season} -- run the ETL first")
    seed = json.loads(seed_path.read_text(encoding="utf-8"))

    rows = _fetch_season_rows(season, api_key)
    team_by_player = _earliest_team_by_player(rows)

    enriched = 0
    missing = 0
    for player in seed["players"]:
        team = team_by_player.get(player["external_ref"])
        if team is None:
            missing += 1
            continue
        player["team"] = team
        enriched += 1

    seed_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    return enriched, missing


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "Usage: python -m tools._gm_real_player_team_enrich <first_season> <last_season>",
            file=sys.stderr,
        )
        return 1
    first, last = int(argv[1]), int(argv[2])
    api_key = os.environ.get("BDL_API_KEY")
    if not api_key:
        print("Set BDL_API_KEY in your own shell first.", file=sys.stderr)
        return 1

    _log(f"team enrichment starting: seasons {first}-{last}")
    failures: list[int] = []
    for season in range(first, last + 1):
        try:
            enriched, missing = enrich_season(season, api_key)
            _log(f"season={season}: OK, {enriched} enriched, {missing} missing a team")
        except Exception as exc:  # noqa: BLE001 -- one bad season must not kill the run
            _log(f"season={season}: FAILED -- {type(exc).__name__}: {exc}")
            failures.append(season)
    _log(f"team enrichment done: {last - first + 1 - len(failures)} ok, failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
