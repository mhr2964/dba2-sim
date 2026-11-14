"""One-time ETL harvesting real BDL player-season box-score stats into
RPD-4/RPD-7's seed-file shape (docs/design/real-player-data-integration.md).
Quarantined tool, mirrors `tools/_gm_draft_class_stub.py`'s convention:
underscore-prefixed, lives in `tools/`, never imported by `dba2/` itself
(no import-scan test added yet for this file -- follow-up once this slice
is verified, same as that stub's own `test_layering.py` entry).

**What this pass covers, updated from the first version.** RPD-5's
box-score-only tier (`/v1/stats`, any era, 12 fields including
`passing_accuracy` moved here after a second look showed it needs no
advanced data) plus, now, RPD-5's advanced/tracking tier for seasons
2012-13+ (`/nba/v2/stats/advanced`): `on_ball_defense` (real) and the
ramping hustle fields `screen_setting`/`motor` (real where populated,
see RPD-5's per-row caveat). **Not** implemented: the seven shot-type-split
fields RPD-7 originally claimed for this tier turned out to have no
player-level BDL source at all (team-level only) -- moved to
synthetic-backfilled in the design doc, never attempted here. Also not
attempted: blending `defended_at_rim_fg_pct` into `rim_protection` or
PIE into `consistency`/peak-age (both documented as real refinements in
RPD-7, deferred rather than built this pass).

**Interpretation calls this first pass makes, stated so they aren't
mistaken for settled design** (this session's own `>>think` pass in
chat picked option 1 below over two alternatives -- see the design doc's
RPD-5/RPD-7 for the reasoning):

1. **Percentile-within-season -> z -> rating, not a hand-tuned lookup
   table.** Every real box-score field is ranked against every other
   real player in the *same season* (BDL's own player pool for that
   `seasons[]` value), converted to a percentile, then mapped through
   `statistics.NormalDist().inv_cdf` to a z-score, then
   `rating = round(clip(70 + 12*z, 25, 99))` -- exactly the inverse of
   `sim.derivation._z`, so real-derived ratings land on the same scale
   synthetic ones already use. This also makes era normalization free:
   a 1997 stat only ever competes against 1997 peers.
2. **Per-36-minute rate stats, not official NBA advanced-stat formulas.**
   True AST%/USG%/TOV% need team-level totals this endpoint doesn't
   return in the same call; percentile normalization already absorbs
   most of the accuracy gap a team-relative formula would close, and
   pulling team totals is a second, heavier integration this pass defers.
3. **Minutes-floor shrinkage.** A player under `_MIN_MINUTES_FULL_CONFIDENCE`
   total season minutes has their percentile-derived z pulled toward 0
   (rating 70, league-average) proportional to how far under the floor
   they are, so a five-game call-up's one hot outing doesn't produce a 99.
4. **BDL's coarse G/F/C position strings** are mapped to this project's
   5-slot PG/SG/SF/PF/C via a fixed, arbitrary lookup (`_POSITION_MAP`
   below) -- BDL doesn't distinguish PG from SG or SF from PF at all, so
   this is a guess, not a real mapping.
5. **Fields with no real source** (RPD-7's "synthetic-backfilled" bucket,
   39 of the 55 after the shot-type-split correction) are filled by
   `sim.derivation.synthetic_ratings`, seeded with
   `Random(f"{external_ref}:{season}")` and a talent scalar equal to the
   mean of this player-season's own real-derived fields (12, or 15 for
   2012-13+ seasons) -- reproducible per player-season, "shaped like
   them" per RPD-5, but still synthetic.
6. **`peak_age_start`/`peak_age_end` are NOT computed from real career
   data this pass** (that needs every season of a player's career, not
   one season in isolation -- RPD-7 already scopes this as needing the
   player's whole real trajectory) -- placeholder-drawn the same way
   `_gm_draft_class_stub.py` draws them for a synthetic prospect. A real
   known gap, not silently papered over.
7. **`birth_date` is a placeholder** (`draft_year - 21` years, June 15)
   since BDL's basic player object carries no real birthdate -- another
   known gap for the not-yet-built league-generation "hydration" step
   RPD-4 describes, not solved here.

**Real bug caught and fixed during this pass's own smoke test:**
`/v1/stats` blends regular-season and postseason rows under one
`seasons[]` value unless `postseason=false` is passed explicitly --
first run (no filter) gave Nikola Jokic 89 games played in a season with
an 82-game cap (82 regular + his 20 playoff games, capped by pagination
at the sample size checked). Fixed by adding `&postseason=false` to every
request; caught by spot-checking known players' output against real
memory of their season, not by construction -- worth remembering next
time a new BDL endpoint gets integrated.

Reads `BDL_API_KEY` from the environment, same contract as
`tools/_gm_real_player_probe.py` -- never hardcode a real key here.
"""
from __future__ import annotations

import bisect
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

from dba2.domain.player import Position
from dba2.sim.derivation import RATING_FIELD_ORDER, synthetic_ratings

_BASE = "https://api.balldontlie.io/v1/stats"
_ADVANCED_BASE = "https://api.balldontlie.io/nba/v2/stats/advanced"
_ADVANCED_MIN_SEASON = 2012  # live-verified floor, RPD-1
_SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players"
_PER_PAGE = 100
_MAX_RETRIES = 5

_RATING_CENTER = 70.0
_RATING_STEP = 12.0  # matches sim.derivation._z's denominator, inverted here
_MIN_MINUTES_FULL_CONFIDENCE = 1500.0

_POSITION_MAP: dict[str, Position] = {
    "G": Position.PG,
    "G-F": Position.SG,
    "F-G": Position.SG,
    "F": Position.SF,
    "F-C": Position.PF,
    "C-F": Position.PF,
    "C": Position.C,
    "": Position.SF,
}

# RPD-7's "real from 1996-97+ via box score alone" bucket (12 of the 55
# RATING_FIELD_ORDER fields; peak_age_start/peak_age_end are separate
# SMALLINT columns, handled by _placeholder_peak_age below, not here).
_BOX_SCORE_REAL_FIELDS: tuple[str, ...] = (
    "durability",
    "ball_security",
    "draw_foul",
    "free_throw",
    "passing_vision",
    "passing_accuracy",
    "offensive_rebounding",
    "defensive_rebounding",
    "foul_discipline",
    "hands",
    "rim_protection",
    "consistency",
)
assert set(_BOX_SCORE_REAL_FIELDS) <= set(RATING_FIELD_ORDER)

# RPD-7's "real only from 2012-13+" bucket, post-correction (on_ball_defense
# genuinely needs the advanced/tracking endpoint; screen_setting/motor are
# the hustle fields, ramping per RPD-5, real only where BDL populated them).
_ADVANCED_REAL_FIELDS: tuple[str, ...] = ("on_ball_defense", "screen_setting", "motor")
assert set(_ADVANCED_REAL_FIELDS) <= set(RATING_FIELD_ORDER)


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _parse_minutes(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if ":" in text:
        mm, _, ss = text.partition(":")
        return float(mm or 0) + float(ss or 0) / 60.0
    return float(text)


def _parse_height(value: str | None) -> int:
    """BDL height strings look like '6-8' (feet-inches). Falls back to a
    generic 78in (6-6) if missing -- rare but not impossible in older
    records."""
    if not value or "-" not in value:
        return 78
    feet, _, inches = value.partition("-")
    try:
        return int(feet) * 12 + int(inches)
    except ValueError:
        return 78


def _parse_weight(value: str | None) -> int:
    if not value:
        return 210
    try:
        return int(value)
    except ValueError:
        return 210


@dataclass
class _PlayerAccum:
    external_ref: str
    first_name: str
    last_name: str
    bdl_position: str
    height_in: int
    weight_lb: int
    draft_year: int | None
    minutes_total: float = 0.0
    games_played: int = 0
    fgm: float = 0.0
    fga: float = 0.0
    ftm: float = 0.0
    fta: float = 0.0
    oreb: float = 0.0
    dreb: float = 0.0
    ast: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    turnover: float = 0.0
    pf: float = 0.0
    game_scores: list[float] = field(default_factory=list)


def _fetch_season_rows(season: int, api_key: str) -> list[dict]:
    rows: list[dict] = []
    cursor: int | None = None
    while True:
        url = f"{_BASE}?seasons[]={season}&per_page={_PER_PAGE}&postseason=false"
        if cursor is not None:
            url += f"&cursor={cursor}"
        request = urllib.request.Request(url, headers={"Authorization": api_key})
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503) and attempt < _MAX_RETRIES - 1:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
        rows.extend(payload.get("data", []))
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1.1)  # All-Star tier's real limit is 60 req/min (GOAT is 600) -- a full season is 200-300+ pages, so anything faster than ~1s/page reliably trips it even on a healthy key/subscription
    return rows


def _game_score(row: dict, minutes: float) -> float | None:
    if minutes <= 0:
        return None
    fgm, fga = row.get("fgm") or 0, row.get("fga") or 0
    ftm, fta = row.get("ftm") or 0, row.get("fta") or 0
    oreb, dreb = row.get("oreb") or 0, row.get("dreb") or 0
    ast, stl, blk = row.get("ast") or 0, row.get("stl") or 0, row.get("blk") or 0
    pf, tov, pts = row.get("pf") or 0, row.get("turnover") or 0, row.get("pts") or 0
    return (
        pts + 0.4 * fgm - 0.7 * fga - 0.4 * (fta - ftm) + 0.7 * oreb + 0.3 * dreb
        + stl + 0.7 * ast + 0.7 * blk - 0.4 * pf - tov
    )


def _aggregate(rows: list[dict]) -> dict[str, _PlayerAccum]:
    accum: dict[str, _PlayerAccum] = {}
    for row in rows:
        minutes = _parse_minutes(row.get("min"))
        if minutes <= 0:
            continue
        player = row.get("player") or {}
        pid = str(player.get("id"))
        if pid not in accum:
            accum[pid] = _PlayerAccum(
                external_ref=pid,
                first_name=player.get("first_name") or "Unknown",
                last_name=player.get("last_name") or f"Player{pid}",
                bdl_position=player.get("position") or "",
                height_in=_parse_height(player.get("height")),
                weight_lb=_parse_weight(player.get("weight")),
                draft_year=player.get("draft_year"),
            )
        acc = accum[pid]
        acc.minutes_total += minutes
        acc.games_played += 1
        acc.fgm += row.get("fgm") or 0
        acc.fga += row.get("fga") or 0
        acc.ftm += row.get("ftm") or 0
        acc.fta += row.get("fta") or 0
        acc.oreb += row.get("oreb") or 0
        acc.dreb += row.get("dreb") or 0
        acc.ast += row.get("ast") or 0
        acc.stl += row.get("stl") or 0
        acc.blk += row.get("blk") or 0
        acc.turnover += row.get("turnover") or 0
        acc.pf += row.get("pf") or 0
        gs = _game_score(row, minutes)
        if gs is not None:
            acc.game_scores.append(gs)
    return accum


def _per36(total: float, minutes: float) -> float:
    return (total * 36.0 / minutes) if minutes > 0 else 0.0


def _raw_box_score_metrics(acc: _PlayerAccum) -> dict[str, float]:
    """One float per `_BOX_SCORE_REAL_FIELDS` entry, in the direction
    where *higher is better* -- inverted here (not left for the
    percentile step) so every metric's population can be ranked the same
    way. Returns `None` (via `math.nan` sentinel handling upstream) is
    avoided by defaulting to 0.0 for zero-attempt denominators, which
    lands a player with no attempts at the population's low end rather
    than crashing -- appropriate for e.g. a center with 0 threes."""
    fga, fta, turnover = acc.fga, acc.fta, acc.turnover
    tov_denom = fga + 0.44 * fta + turnover
    tov_rate = (turnover / tov_denom) if tov_denom > 0 else 0.0
    ft_pct = (acc.ftm / acc.fta) if acc.fta > 0 else 0.0
    ast_to_tov = acc.ast / turnover if turnover > 0 else acc.ast
    consistency = -statistics.pstdev(acc.game_scores) if len(acc.game_scores) >= 2 else 0.0
    return {
        "durability": float(acc.games_played),
        "ball_security": -tov_rate,
        "draw_foul": _per36(acc.fta, acc.minutes_total),
        "free_throw": ft_pct,
        "passing_vision": _per36(acc.ast, acc.minutes_total),
        "passing_accuracy": ast_to_tov,
        "offensive_rebounding": _per36(acc.oreb, acc.minutes_total),
        "defensive_rebounding": _per36(acc.dreb, acc.minutes_total),
        "foul_discipline": -_per36(acc.pf, acc.minutes_total),
        "hands": _per36(acc.stl, acc.minutes_total),
        "rim_protection": _per36(acc.blk, acc.minutes_total),
        "consistency": consistency,
    }


@dataclass
class _AdvancedAccum:
    matchup_fgm: float = 0.0
    matchup_fga: float = 0.0
    screen_assists: float = 0.0
    loose_balls_recovered_total: float = 0.0
    charges_drawn: float = 0.0


def _fetch_advanced_rows(season: int, api_key: str) -> list[dict]:
    """Same pagination/retry/postseason-filter contract as
    `_fetch_season_rows` -- the postseason-blending bug that first bit
    `/v1/stats` applies here too, unverified but not worth re-discovering
    the hard way a second time."""
    rows: list[dict] = []
    cursor: int | None = None
    while True:
        url = f"{_ADVANCED_BASE}?seasons[]={season}&per_page={_PER_PAGE}&postseason=false"
        if cursor is not None:
            url += f"&cursor={cursor}"
        request = urllib.request.Request(url, headers={"Authorization": api_key})
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503) and attempt < _MAX_RETRIES - 1:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
        rows.extend(payload.get("data", []))
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1.1)  # All-Star tier's real limit is 60 req/min (GOAT is 600) -- a full season is 200-300+ pages, so anything faster than ~1s/page reliably trips it even on a healthy key/subscription
    return rows


def _aggregate_advanced(rows: list[dict]) -> dict[str, _AdvancedAccum]:
    accum: dict[str, _AdvancedAccum] = {}
    for row in rows:
        player = row.get("player") or {}
        pid = str(player.get("id"))
        if pid not in accum:
            accum[pid] = _AdvancedAccum()
        acc = accum[pid]
        acc.matchup_fgm += row.get("matchup_fgm") or 0
        acc.matchup_fga += row.get("matchup_fga") or 0
        acc.screen_assists += row.get("screen_assists") or 0
        acc.loose_balls_recovered_total += row.get("loose_balls_recovered_total") or 0
        acc.charges_drawn += row.get("charges_drawn") or 0
    return accum


def _raw_advanced_metrics(acc: _AdvancedAccum, minutes_total: float) -> dict[str, float]:
    """Same "higher is better" convention as `_raw_box_score_metrics`.
    `on_ball_defense` inverts matchup FG% allowed (lower = better
    defense); `screen_setting`/`motor` are per-36 hustle rates, real only
    where BDL actually populated them for that row (RPD-5's ramp) --
    a row with null hustle fields contributes 0 via the `or 0` sums
    above, which understates a partial-coverage season's true rate
    rather than crashing; a known, accepted simplification, not treated
    as equivalent to a fully-hustle-tracked season's number.

    `minutes_total` is the player's *box-score* season total (from
    `_PlayerAccum`), not derived from this endpoint's own rows -- a real
    bug found via spot-check, not by construction: `/nba/v2/stats/advanced`
    has no `min` field at all (confirmed by inspecting a known player's
    raw row directly), so deriving minutes from it silently produced 0
    for every player, which zeroed the shrink weight and flattened every
    advanced-tier field to exactly 70 regardless of real performance."""
    matchup_fg_pct = (acc.matchup_fgm / acc.matchup_fga) if acc.matchup_fga > 0 else 0.5
    return {
        "on_ball_defense": -matchup_fg_pct,
        "screen_setting": _per36(acc.screen_assists, minutes_total),
        "motor": _per36(acc.loose_balls_recovered_total + acc.charges_drawn, minutes_total),
    }


def _percentile_z(value: float, sorted_population: list[float]) -> float:
    n = len(sorted_population)
    if n <= 1:
        return 0.0
    lo = bisect.bisect_left(sorted_population, value)
    hi = bisect.bisect_right(sorted_population, value)
    mid_rank = (lo + hi) / 2.0
    p = _clamp((mid_rank + 0.5) / n, 0.001, 0.999)
    return statistics.NormalDist().inv_cdf(p)


def _shrink_weight(minutes_total: float) -> float:
    return _clamp(minutes_total / _MIN_MINUTES_FULL_CONFIDENCE, 0.0, 1.0)


def _resolve_position(bdl_position: str) -> Position:
    return _POSITION_MAP.get(bdl_position, Position.SF)


def _placeholder_peak_age(rng: Random) -> tuple[int, int]:
    start = rng.randint(24, 28)
    return start, start + rng.randint(2, 6)


def build_season_seed(
    season: int, rows: list[dict], advanced_rows: list[dict] | None = None
) -> dict:
    accum = _aggregate(rows)
    raw_metrics = {pid: _raw_box_score_metrics(acc) for pid, acc in accum.items()}

    populations: dict[str, list[float]] = {
        field_name: sorted(raw_metrics[pid][field_name] for pid in raw_metrics)
        for field_name in _BOX_SCORE_REAL_FIELDS
    }

    has_advanced = bool(advanced_rows) and season >= _ADVANCED_MIN_SEASON
    adv_accum: dict[str, _AdvancedAccum] = {}
    adv_raw: dict[str, dict[str, float]] = {}
    adv_populations: dict[str, list[float]] = {}
    if has_advanced:
        adv_accum = _aggregate_advanced(advanced_rows)
        # Minutes come from the box-score accumulator (`accum`), not this
        # endpoint's own rows -- see `_raw_advanced_metrics`'s docstring
        # for why. A pid present in `adv_accum` but not `accum` (no box-score
        # minutes to normalize against) is dropped rather than guessed at.
        adv_raw = {
            pid: _raw_advanced_metrics(acc, accum[pid].minutes_total)
            for pid, acc in adv_accum.items()
            if pid in accum
        }
        adv_populations = {
            field_name: sorted(adv_raw[pid][field_name] for pid in adv_raw)
            for field_name in _ADVANCED_REAL_FIELDS
        }

    players_out: list[dict] = []
    for pid, acc in accum.items():
        weight = _shrink_weight(acc.minutes_total)
        real_ratings: dict[str, int] = {}
        for field_name in _BOX_SCORE_REAL_FIELDS:
            z = _percentile_z(raw_metrics[pid][field_name], populations[field_name])
            real_ratings[field_name] = int(
                round(_clamp(_RATING_CENTER + _RATING_STEP * z * weight, 25, 99))
            )

        band = "box_score_only"
        if has_advanced and pid in adv_raw:
            for field_name in _ADVANCED_REAL_FIELDS:
                z = _percentile_z(adv_raw[pid][field_name], adv_populations[field_name])
                real_ratings[field_name] = int(
                    round(_clamp(_RATING_CENTER + _RATING_STEP * z * weight, 25, 99))
                )
            band = "advanced_tracking"

        talent = sum(real_ratings.values()) / len(real_ratings)
        position = _resolve_position(acc.bdl_position)
        rng = Random(f"{pid}:{season}")
        full_ratings = synthetic_ratings(talent, position, rng)
        full_ratings.update(real_ratings)
        peak_start, peak_end = _placeholder_peak_age(rng)

        confidence = round(0.4 + 0.3 * weight, 2) if band == "box_score_only" else round(
            0.55 + 0.3 * weight, 2
        )

        players_out.append(
            {
                "external_ref": pid,
                "first_name": acc.first_name,
                "last_name": acc.last_name,
                "primary_position": position.value,
                "height_in": acc.height_in,
                "weight_lb": acc.weight_lb,
                "draft_year": acc.draft_year,
                "season": season,
                "ratings": {**full_ratings, "peak_age_start": peak_start, "peak_age_end": peak_end},
                "source": "real_stat_derived",
                "confidence": confidence,
                "derived_from_season": season,
                "meta": {
                    "games_played": acc.games_played,
                    "minutes_total": round(acc.minutes_total, 1),
                    "band": band,
                },
            }
        )

    return {
        "_note": (
            f"Real BDL-derived player ratings for the {season} season "
            "(box-score-only tier plus, for 2012-13+, on_ball_defense and the "
            "ramping hustle fields screen_setting/motor -- RPD-5/RPD-7 in "
            "docs/design/real-player-data-integration.md). Built by "
            "tools/_gm_real_player_etl.py -- see that file's docstring for the "
            "percentile-to-rating method and every interpretation call/known gap "
            "(peak_age_start/peak_age_end and birth_date are placeholders, not "
            "real career data, this pass; the seven shot-type-split fields RPD-7 "
            "originally claimed for this tier have no player-level BDL source at "
            "all and are synthetic-backfilled like everything else in that bucket)."
        ),
        "season": season,
        "players": players_out,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python -m tools._gm_real_player_etl <season>", file=sys.stderr)
        return 1
    season = int(argv[1])
    api_key = os.environ.get("BDL_API_KEY")
    if not api_key:
        print("Set BDL_API_KEY in your own shell first.", file=sys.stderr)
        return 1

    rows = _fetch_season_rows(season, api_key)
    advanced_rows = (
        _fetch_advanced_rows(season, api_key) if season >= _ADVANCED_MIN_SEASON else []
    )
    seed = build_season_seed(season, rows, advanced_rows)

    _SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _SEEDS_DIR / f"{season}.json"
    out_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    print(f"season={season}: {len(seed['players'])} players -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
