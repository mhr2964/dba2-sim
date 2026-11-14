"""Fetches and stores RAW per-player season evidence from BDL, so real-player
ratings can be rebuilt offline as many times as tuning needs (RPD-8-1,
docs/design/real-player-ratings-v2.md).

**Why this exists.** `_gm_real_player_etl.py` downloads every game row for a
season, reduces it to 15 percentile-derived ratings and throws the rest away,
including points, shooting splits and the season-opening team. Adding scoring
evidence to the ratings therefore needed a re-download, and so would every later
tuning pass. This tool downloads once and keeps the per-player totals in
`seeds/real_players_raw/<season>.json`; `tools/_gm_real_player_build.py` (RPD-8-2..9)
turns those into rating seeds with no network and no key.

**What is stored per player** (one entry per player with any minutes that season):
identity and physicals, games and minutes, the counting totals the builder needs
(`pts fgm fga fg3m fg3a ftm fta oreb dreb ast stl blk turnover pf`), one Hollinger
game score per game (`game_scores`, the same formula the ETL uses, for the
consistency rating and GmSc/36), the season-opening team abbreviation (the team on
the player's earliest-dated row, the rule `_gm_real_player_team_enrich.py` uses) and,
for 2012-13+, the advanced endpoint's numeric fields as per-player sums plus
non-null row counts (a mean is `sum / count`). Summing EVERY numeric advanced field
rather than the five the ETL reads is deliberate: whether that endpoint already
returns usage or true-shooting is design question Q1, and this keeps the answer
on disk. Each file also keeps one sample row from each endpoint for inspection.

`--no-advanced` skips the advanced endpoint (`/nba/v2/stats/advanced`), which needs a
higher BDL tier than the box-score endpoint; the raw file then records
`advanced_present: false` and the builder recovers the three advanced ratings
(on_ball_defense, screen_setting, motor) from the existing season seeds instead.

Only regular-season rows (`postseason=false`), the same filter the ratings were
built from. Resumable: a season whose file already exists is skipped unless
`--force`.

Usage (in your own shell; the key is read from the environment, never from a file,
and is never printed):
    $env:BDL_API_KEY = "<your key>"
    python -m tools._gm_real_player_fetch_raw 2009 2025

Quarantined tool, same convention as `_gm_real_player_etl.py`: lives in `tools/`,
not `dba2/`.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any

from tools._gm_real_player_etl import (
    _ADVANCED_MIN_SEASON,
    _fetch_advanced_rows,
    _fetch_season_rows,
    _game_score,
    _parse_height,
    _parse_minutes,
    _parse_weight,
)

RAW_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players_raw"
RAW_FORMAT_VERSION = 1

_COUNTING_FIELDS: tuple[str, ...] = (
    "pts",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "turnover",
    "pf",
)


def _game_date(row: dict) -> str:
    """The row's game date as text, `""` when absent. ISO dates sort correctly as
    strings, which is all the earliest-row rule needs."""
    game = row.get("game") or {}
    return str(game.get("date") or "")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def aggregate_raw_season(
    season: int, rows: list[dict], advanced_rows: list[dict] | None = None
) -> dict[str, Any]:
    """Pure reduction of one season's rows to the raw file's content. No network,
    no clock: the same rows always give the same dict."""
    players: dict[str, dict[str, Any]] = {}
    for row in rows:
        minutes = _parse_minutes(row.get("min"))
        if minutes <= 0:
            continue
        player = row.get("player") or {}
        pid = str(player.get("id"))
        entry = players.get(pid)
        if entry is None:
            entry = {
                "external_ref": pid,
                "first_name": player.get("first_name") or "Unknown",
                "last_name": player.get("last_name") or f"Player{pid}",
                "bdl_position": player.get("position") or "",
                "height_in": _parse_height(player.get("height")),
                "weight_lb": _parse_weight(player.get("weight")),
                "draft_year": player.get("draft_year"),
                "first_team": None,
                "first_game_date": "",
                "games_played": 0,
                "minutes_total": 0.0,
                "totals": dict.fromkeys(_COUNTING_FIELDS, 0.0),
                "game_scores": [],
            }
            players[pid] = entry
        entry["games_played"] += 1
        entry["minutes_total"] += minutes
        for name in _COUNTING_FIELDS:
            entry["totals"][name] += row.get(name) or 0
        score = _game_score(row, minutes)
        if score is not None:
            entry["game_scores"].append(round(score, 2))
        date = _game_date(row)
        team = (row.get("team") or {}).get("abbreviation")
        if team and (entry["first_team"] is None or (date and date < entry["first_game_date"])):
            entry["first_team"] = team
            entry["first_game_date"] = date

    advanced_present = bool(advanced_rows) and season >= _ADVANCED_MIN_SEASON
    if advanced_present:
        for row in advanced_rows or []:
            pid = str((row.get("player") or {}).get("id"))
            entry = players.get(pid)
            if entry is None:
                continue  # no box-score minutes for this player: nothing to normalise against
            block = entry.setdefault("advanced", {"rows": 0, "sums": {}, "counts": {}})
            block["rows"] += 1
            for key, value in row.items():
                if _is_number(value):
                    block["sums"][key] = block["sums"].get(key, 0.0) + value
                    block["counts"][key] = block["counts"].get(key, 0) + 1

    for entry in players.values():
        entry["minutes_total"] = round(entry["minutes_total"], 2)
        entry["totals"] = {name: round(value, 2) for name, value in entry["totals"].items()}
        if "advanced" in entry:
            entry["advanced"]["sums"] = {
                key: round(value, 4) for key, value in entry["advanced"]["sums"].items()
            }
    return {
        "raw_format_version": RAW_FORMAT_VERSION,
        "season": season,
        "advanced_present": advanced_present,
        "sample_stats_row": rows[0] if rows else None,
        "sample_advanced_row": (advanced_rows or [None])[0] if advanced_present else None,
        "players": sorted(players.values(), key=lambda p: p["external_ref"]),
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch_season(
    season: int,
    api_key: str,
    *,
    raw_dir: Path = RAW_DIR,
    force: bool = False,
    advanced: bool = True,
) -> str:
    """Fetch and store one season; returns a one-line status. A season already on
    disk is skipped unless `force`, which is what makes a 17-season run resumable
    after a rate-limit failure or a closed laptop."""
    out_path = raw_dir / f"{season}.json"
    if out_path.exists() and not force:
        return f"season={season}: already on disk, skipped ({out_path.name})"
    # Advanced first: it needs the higher BDL tier, so a key that lacks it fails in
    # seconds instead of after the several-minute box-score download.
    wants_advanced = advanced and season >= _ADVANCED_MIN_SEASON
    advanced_rows = _fetch_advanced_rows(season, api_key) if wants_advanced else []
    rows = _fetch_season_rows(season, api_key)
    payload = aggregate_raw_season(season, rows, advanced_rows)
    _write_atomic(out_path, payload)
    return (
        f"season={season}: {len(payload['players'])} players, "
        f"advanced={'yes' if payload['advanced_present'] else 'no'} -> {out_path.name}"
    )


def main(argv: list[str]) -> int:
    flags = {"--force", "--no-advanced"}
    args = [a for a in argv[1:] if a not in flags]
    force = "--force" in argv[1:]
    advanced = "--no-advanced" not in argv[1:]
    if len(args) != 2:
        print(
            "Usage: python -m tools._gm_real_player_fetch_raw "
            "<first_season> <last_season> [--force] [--no-advanced]",
            file=sys.stderr,
        )
        return 1
    first, last = int(args[0]), int(args[1])
    if first > last:
        print(f"first season {first} is after last season {last}", file=sys.stderr)
        return 1
    api_key = os.environ.get("BDL_API_KEY")
    if not api_key:
        print("Set BDL_API_KEY in your own shell first.", file=sys.stderr)
        return 1
    for season in range(first, last + 1):
        try:
            print(fetch_season(season, api_key, force=force, advanced=advanced), flush=True)
        except urllib.error.HTTPError as exc:
            # The URL carries no key (it travels in a header), so it is safe to show.
            endpoint = str(exc.url).split("?")[0]
            print(
                f"season={season}: BDL answered HTTP {exc.code} for {endpoint}. "
                "401 means this key is not accepted for that endpoint (wrong key or a "
                "tier below the one the endpoint needs).",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
