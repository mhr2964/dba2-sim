"""Standalone dev tool: a local desktop-ish app (stdlib HTTP server + a page
that opens in your default browser) for simulating a real best-of-7 series
between two real historical team-seasons, using the actual DBA 2
possession-sim engine live, per request -- the thing a claude.ai Artifact
can't do (its sandbox has no path back to this local Python engine).

Team/roster selection is derived entirely from
`seeds/real_players/<season>.json` (every player entry there now carries a
real `team` field, backfilled from BDL's per-game data -- see that
directory's own `_team_enrich_log.txt`). For a given season, players are
grouped by `team.abbreviation` and each team's ~10-man rotation is picked by
sorting that team-season's real players by `meta.minutes_total` descending
and taking the top 10 -- a real proxy for "who actually played meaningful
minutes," since real rosters often carry 15-20+ players across a season
(10-days, injuries, etc.) who aren't meaningfully "the team." No more
hand-typed rosters (see git history for the retired
`tools/historical_rosters.json` this replaced).

Note: BDL's `team` field reflects each franchise's CURRENT identity (city/
name/abbreviation) even for old seasons -- e.g. 1996 Thunder players are
really the Seattle SuperSonics, 1996 Grizzlies are really the Vancouver
Grizzlies, pre-2012 Nets are really the New Jersey Nets. The underlying
per-player data doesn't carry era-correct franchise names, so labels here
say "Oklahoma City Thunder" etc. even for seasons before that name existed --
a real data-quality gap in the source, not something faked here.

No new dependency: uses only `http.server` from the standard library, the
same pure engine functions `tools/box_score_real_matchup.py` already
wraps (`compute_overall`, `derive_tendencies`, `simulate_game`), and the
same `RealTeamSpec`/`_build_sim_team` helpers that module exports.

Only seasons whose seed rows are RPD-8 builder version 2 (2009-2025) are offered by
default: a legacy season (1996-2008) is refused when the catalog is built and left out
of the picker, with the startup message naming it and the rebuild command.
`--allow-legacy-ratings` offers them anyway at their legacy (star-compressed) ratings.

Usage: python tools/matchup_server.py [--port 8765] [--allow-legacy-ratings]
Then open http://localhost:8765/ (auto-opens by default).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import SystemRandom

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dba2.sim.context import GameContext  # noqa: E402
from dba2.sim.game import simulate_game  # noqa: E402
from dba2.sim.tuning import DEFAULT_TUNING  # noqa: E402
from tools._real_fields import LegacyRatingsError  # noqa: E402
from tools.box_score_real_matchup import (  # noqa: E402
    RealTeamSpec,
    _build_sim_team,
    _load_real_player_pool,
)

_REAL_PLAYERS_DIR = _REPO_ROOT / "seeds" / "real_players"
_TEAMS_SEED_PATH = _REPO_ROOT / "seeds" / "teams.json"

# Every season file currently seeded (see seeds/real_players/, 1996-2025).
_SEASONS = range(1996, 2026)

# A team-season's real "rotation" size, matching the old hand-curated lists'
# own size and _build_sim_team's/the engine's minutes-tier assumptions
# (see dba2/sim/game.py -- roles are ranked off a 10-man-shaped pool).
_ROSTER_SIZE = 10

# BDL's `team.abbreviation` (what seeds/real_players/*.json carries) differs
# from seeds/teams.json's `code` for exactly one franchise.
_ABBR_ALIAS = {"BKN": "BRK"}

# Standard 2-2-1-1-1 best-of-7 format: True = team A hosts that game.
_HOME_SCHEDULE = (True, True, False, False, True, False, True)


def _load_conference_division() -> dict[str, tuple[str, str]]:
    """Current conference/division per team code -- cosmetic only (the sim
    engine doesn't use these fields for anything), so using today's
    conference/division for an old-season team-season is harmless."""
    data = json.loads(_TEAMS_SEED_PATH.read_text(encoding="utf-8"))
    return {t["code"]: (t["conference"], t["division"]) for t in data["teams"]}


def _season_label(season: int) -> str:
    end_yy = (season + 1) % 100
    return f"{season}-{end_yy:02d}"


def _build_catalog(*, allow_legacy_ratings: bool = False) -> tuple[dict[str, dict], list[str]]:
    """Group each season's real players by team.abbreviation and keep the
    top _ROSTER_SIZE by real minutes played -- the whole catalog this
    server's picker offers, derived straight from the seed data, nothing
    hand-typed. Also warms _POOL_CACHE so per-request sims don't re-read
    the season files this startup pass already loaded.

    A season whose rows are not RPD-8 builder version 2 is refused
    (`LegacyRatingsError`) and left out of the catalog unless
    `allow_legacy_ratings`; the second return value is one refusal message per
    such season, so the caller can say what is missing and how to rebuild it."""
    conf_div = _load_conference_division()
    catalog: dict[str, dict] = {}
    refused: list[str] = []
    for season in _SEASONS:
        path = _REAL_PLAYERS_DIR / f"{season}.json"
        if not path.exists():
            continue
        try:
            pool = _load_real_player_pool(season, allow_legacy_ratings=allow_legacy_ratings)
        except LegacyRatingsError as exc:
            refused.append(str(exc))
            continue
        _POOL_CACHE[season] = pool

        by_abbr: dict[str, list[dict]] = {}
        for p in pool:
            team = p.get("team")
            if not team or not team.get("abbreviation"):
                continue  # none observed in practice (backfill log: 0 missing)
            by_abbr.setdefault(team["abbreviation"], []).append(p)

        for abbr, players in by_abbr.items():
            ranked = sorted(
                players,
                key=lambda p: (p["meta"]["minutes_total"], p["meta"]["games_played"]),
                reverse=True,
            )
            top = ranked[:_ROSTER_SIZE]  # naturally <= _ROSTER_SIZE if the team-season has fewer
            team_meta = top[0]["team"]
            conf, div = conf_div.get(_ABBR_ALIAS.get(abbr, abbr), ("", ""))
            key = f"{season}-{abbr}"
            catalog[key] = {
                "key": key,
                "season": season,
                "code": abbr,
                "name": team_meta["name"],
                "city": team_meta["city"],
                "conference": conf,
                "division": div,
                "label": f"{_season_label(season)} {team_meta['city']} {team_meta['name']}",
                "roster": [(p["first_name"], p["last_name"]) for p in top],
            }
    return catalog, refused


_POOL_CACHE: dict[int, list[dict]] = {}
# Filled by `main()`, not at import, so importing this module never reads (or
# refuses) a season file.
_CATALOG: dict[str, dict] = {}
_ALLOW_LEGACY_RATINGS = False


def _pool_for_season(season: int) -> list[dict]:
    if season not in _POOL_CACHE:
        _POOL_CACHE[season] = _load_real_player_pool(
            season, allow_legacy_ratings=_ALLOW_LEGACY_RATINGS
        )
    return _POOL_CACHE[season]


def _spec_for(team_key: str, team_id: int) -> tuple[RealTeamSpec, int]:
    entry = _CATALOG[team_key]
    spec = RealTeamSpec(
        team_id=team_id,
        code=entry["code"],
        name=entry["name"],
        city=entry["city"],
        conference=entry["conference"],
        division=entry["division"],
        roster=[tuple(p) for p in entry["roster"]],
    )
    return spec, entry["season"]


def _player_row(line, names) -> dict:
    name, pos = names[line.player_id]
    return {
        "name": name, "pos": pos,
        "min": round(line.minutes_played, 1), "pts": line.points,
        "fgm": line.fgm_rim + line.fgm_mid, "fga": line.fga_rim + line.fga_mid,
        "tpm": line.fgm_three, "tpa": line.fga_three,
        "ftm": line.ftm, "fta": line.fta,
        "oreb": line.oreb, "dreb": line.dreb, "reb": line.oreb + line.dreb,
        "ast": line.ast, "stl": line.stl, "blk": line.blk, "tov": line.tov, "pf": line.pf,
        "pm": line.plus_minus,
    }


def _team_totals_row(line) -> dict:
    return {
        "name": "Team", "pos": "", "min": None,
        "pts": line.points, "fgm": line.fgm_rim + line.fgm_mid, "fga": line.fga_rim + line.fga_mid,
        "tpm": line.fgm_three, "tpa": line.fga_three, "ftm": line.ftm, "fta": line.fta,
        "oreb": line.oreb, "dreb": line.dreb, "reb": line.oreb + line.dreb,
        "ast": line.ast, "stl": line.stl, "blk": line.blk, "tov": line.tov, "pf": line.pf,
        "pm": None,
    }


def _simulate_one_game(spec_a, season_a, spec_b, season_b, *, a_home: bool, seed: int) -> dict:
    home_spec, home_season = (spec_a, season_a) if a_home else (spec_b, season_b)
    away_spec, away_season = (spec_b, season_b) if a_home else (spec_a, season_a)

    home_team = _build_sim_team(
        home_spec, _pool_for_season(home_season), season=home_season, rng_seed=seed
    )
    away_team = _build_sim_team(
        away_spec, _pool_for_season(away_season), season=away_season, rng_seed=seed + 500000
    )
    context = GameContext(
        game_id=seed, season=home_season, home=home_team, away=away_team, tuning=DEFAULT_TUNING
    )
    result = simulate_game(context, rng_seed=seed)

    names = {}
    for team in (home_team, away_team):
        for sp in team.players:
            p = sp.player
            names[p.id] = (f"{p.first_name} {p.last_name}", p.primary_position.value)

    def block(team_id, label):
        lines = sorted((p for p in result.player_box_lines if p.team_id == team_id),
                        key=lambda p: p.minutes_played, reverse=True)
        team_line = next(t for t in result.team_box_lines if t.team_id == team_id)
        rows = [_player_row(p, names) for p in lines]
        rows.append(_team_totals_row(team_line))
        return {"label": label, "rows": rows}

    home_label = f"{home_team.team.city} {home_team.team.name}"
    away_label = f"{away_team.team.city} {away_team.team.name}"
    return {
        "seed": seed,
        "home_label": home_label, "away_label": away_label,
        "home_score": result.home_score, "away_score": result.away_score,
        "ot": result.overtime_periods,
        "periods": [{"period": p, "home": h, "away": a} for p, h, a in result.period_scores],
        "home": block(home_team.id, home_label),
        "away": block(away_team.id, away_label),
        "a_home": a_home,
        "a_won": (result.home_score > result.away_score) == a_home,
    }


def _simulate_series(team_a_key: str, team_b_key: str, base_seed: int) -> dict:
    spec_a, season_a = _spec_for(team_a_key, 1)
    spec_b, season_b = _spec_for(team_b_key, 2)
    label_a = _CATALOG[team_a_key]["label"]
    label_b = _CATALOG[team_b_key]["label"]

    games = []
    wins_a = wins_b = 0
    for game_index, a_home in enumerate(_HOME_SCHEDULE):
        if wins_a == 4 or wins_b == 4:
            break
        game = _simulate_one_game(
            spec_a, season_a, spec_b, season_b, a_home=a_home, seed=base_seed + game_index
        )
        game["game_number"] = game_index + 1
        if game["a_won"]:
            wins_a += 1
        else:
            wins_b += 1
        game["wins_a_after"] = wins_a
        game["wins_b_after"] = wins_b
        games.append(game)

    return {
        "label_a": label_a, "label_b": label_b,
        "wins_a": wins_a, "wins_b": wins_b,
        "winner": label_a if wins_a == 4 else label_b,
        "games": games,
        "base_seed": base_seed,
    }


_INDEX_HTML = None  # loaded lazily from the sibling .html asset below


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/teams":
            teams = [
                {
                    "key": v["key"], "label": v["label"], "code": v["code"],
                    "conference": v["conference"], "season": v["season"],
                    "city": v["city"], "name": v["name"],
                }
                for v in _CATALOG.values()
            ]
            self._send_json({"teams": teams})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path != "/api/series":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            team_a = payload["team_a"]
            team_b = payload["team_b"]
            if team_a not in _CATALOG or team_b not in _CATALOG:
                raise ValueError("unknown team key")
            seed = int(payload.get("seed") or SystemRandom().randint(1, 2_000_000_000))
            series = _simulate_series(team_a, team_b, seed)
            self._send_json(series)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the page, not swallowed
            self._send_json({"error": str(exc)}, status=400)


def main() -> None:
    global _INDEX_HTML, _CATALOG, _ALLOW_LEGACY_RATINGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--allow-legacy-ratings",
        action="store_true",
        help=(
            "Offer seasons whose seed rows are not RPD-8 builder version 2 (1996-2008) "
            "at their legacy ratings instead of leaving them out of the picker."
        ),
    )
    args = parser.parse_args()

    _ALLOW_LEGACY_RATINGS = args.allow_legacy_ratings
    _CATALOG, refused = _build_catalog(allow_legacy_ratings=_ALLOW_LEGACY_RATINGS)
    if refused:
        print(
            f"{len(refused)} season(s) left out of the picker (legacy ratings). "
            f"First refusal: {refused[0]}",
            file=sys.stderr,
        )

    html_path = Path(__file__).parent / "matchup_server_page.html"
    _INDEX_HTML = html_path.read_text(encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"DBA 2 matchup sim running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
