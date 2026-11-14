"""RPD-6 (docs/design/real-player-data-integration.md): a real player's own
career-arc deltas, read from `seeds/real_players/*.json`, as a per-field
growth prior -- replacing `tools/_gm_progression_stub.py`'s single-scalar
"nudge toward a static `potential`" mechanic for real-seeded players only,
**restricted to the real-sourced fields** (15 of the 53 nudge-eligible
fields for a legacy seed, `_REAL_SOURCED_FIELDS`; 30 for a version-2 seed,
`real_sourced_fields(2)` -- see that constant's own comment for the empirical
justification and RPD-8-7/8-8 in `docs/design/real-player-ratings-v2.md`).
The stub falls through to its ordinary synthetic scalar-nudge for every
other field on a real player, same as it always has for a non-real one.
Generated players are entirely untouched by this module; nothing here is
imported anywhere except the progression stub.

**Data source, not the DB.** Hydration (`tools/seed_dev_league.py`) never
writes a real player's *career* history into `player_ratings_history` --
only their one hydrated season's ratings land in the live schema. The real
per-season rows this module needs live only in the 30
`seeds/real_players/<season>.json` files the backfill already produced, so
this module reads those directly, the same convention `tools/_gm_real_
player_peak_age.py` established for the identical need.

**Age anchor, restated rather than re-derived differently.** Reuses `tools/
_gm_real_player_peak_age.py`'s own `age = season - draft_year + 21`
convention (drafted players) with the same undrafted fallback (anchor to
the player's own earliest season on file, treated as an assumed age-21
rookie season) -- duplicated here in full rather than imported, since
importing another quarantined tool's private helpers would couple two
independently-quarantined scripts together for ~10 lines of arithmetic;
both must independently agree with `tools/_gm_real_player_etl.py`'s
`birth_date = date(draft_year - 21, 6, 15)` placeholder, which is the
actual shared invariant, not either file's internals.

**Interpolation, deliberately linear -- "crude on purpose," matching this
module's whole lineage.** A real player's own real seasons are rarely
gap-free (injuries, the backfill's own 1996 floor, international/G-League
years with no BDL row) -- rather than requiring exact consecutive-age
data, `expected_delta_at_age` linearly interpolates each field's rating
between the two nearest real ages bracketing the queried age, on both
sides of the one-season step being aged. No attempt at a smoothed curve,
regression, or age-curve model -- Phase 7's real progression system is
where that belongs (design doc's own words, quoted in `_gm_progression_
stub.py`'s RPD-6 section).

**Past the real career: `None`, not a flat extrapolation.** Once the
queried age is at or past the player's own last real season on file (or
before their first), there is no real data left to be a "prior" -- this
module signals that with `None` rather than guessing a trend forward
(a linear extrapolation past real data risks runaway values over many
simulated seasons with nothing to anchor it) or silently returning a
flat zero delta (which would freeze a still-simulated player's ratings
forever once their real career ends, contradicting RPD-3's own "not a
hard cap" decision). The caller (`_gm_progression_stub.py`) falls back to
the existing synthetic potential-based mechanic in this case -- a real
player who outlives their real career keeps aging like anyone else from
that point on, exactly RPD-3's intent.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from tools._real_fields import (
    ADVANCED_REAL_FIELDS,
    BOX_SCORE_REAL_FIELDS,
    REAL_BUILDER_VERSION,
    REAL_FIELD_SET,
)

_SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players"
_DRAFT_AGE_ANCHOR = 21

# RPD-6 tuning pass (2026-09-12): the trajectory is INCLUSION-restricted to
# the real-sourced fields. Originally this was an EXCLUSION list
# (just peak_age_start/peak_age_end/potential/potential_variance), which let
# a season-to-season "delta" through for all of RATING_FIELD_ORDER's other
# 38 fields -- but those 38 have no real BDL source at any tier (RPD-7) and
# are independently re-derived from that season's own percentile/z-score, so
# their "delta" is 100% re-derivation noise, not a growth signal, just like
# potential/potential_variance already were understood to be. Measured
# directly against the full 30-season backfill (657k production
# expected_delta_at_age() calls): the 15 real-sourced fields average
# |delta|=4.2 (median 3, clamp-tuning's own 8.0 cap engages 13.8% of the
# time -- a plausible real-breakout/injury tail); the 38 excluded-here
# fields averaged |delta|=7.5 (median 6, the same 8.0 cap was engaging
# 36.1% of the time -- not a tail, routine clamp-riding on pure noise).
# `_gm_progression_stub.py`'s real-trajectory branch now falls through to
# the ordinary synthetic scalar-nudge for these 38 fields on a real player
# too, exactly as it already does for peak_age_start/peak_age_end/
# potential/potential_variance and for every non-real player.
#
# RPD-8-8: the field list now lives in `tools/_real_fields.py` (shared with the
# builder) instead of being duplicated here, and depends on the seed's builder
# version. A legacy (version 1) seed only ever had RPD-7's 15 real fields --
# its other fields are re-derived noise -- so legacy keeps those 15 and only a
# version-2 seed (30 REAL_FIELDS) trajects the new ones. Sharing the 30 with
# legacy rows would feed re-derivation noise into 15 more fields.
_LEGACY_BUILDER_VERSION = 1

# The legacy (version 1) real-sourced fields: RPD-7's box-score + advanced tiers.
# Kept under this name for existing importers/tests.
_REAL_SOURCED_FIELDS = frozenset((*BOX_SCORE_REAL_FIELDS, *ADVANCED_REAL_FIELDS))


def real_sourced_fields(builder_version: int | None) -> frozenset[str]:
    """The fields the real trajectory covers for a seed of `builder_version`:
    `None`/1 (legacy) -> the original 15; `REAL_BUILDER_VERSION` (2) -> the
    30 `REAL_FIELDS`. Any other version has no defined field list."""
    if builder_version is None or builder_version == _LEGACY_BUILDER_VERSION:
        return _REAL_SOURCED_FIELDS
    if builder_version == REAL_BUILDER_VERSION:
        return REAL_FIELD_SET
    raise ValueError(f"unsupported real-player builder_version: {builder_version!r}")


def _row_builder_version(player: dict) -> int:
    """A seed row's builder version; legacy rows carry no `meta.builder_version`
    and count as version 1."""
    version = (player.get("meta") or {}).get("builder_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return _LEGACY_BUILDER_VERSION


def _season_files() -> list[Path]:
    return sorted(_SEEDS_DIR.glob("[12][0-9][0-9][0-9].json"))


@cache
def _career_curves(
    *, builder_version: int | None = None
) -> dict[str, tuple[tuple[int, dict[str, float]], ...]]:
    """`external_ref -> ((age, {field: rating}), ...)`, sorted ascending by
    age, built once per process (per `builder_version`) from the season files
    on disk. Cached (not reloaded per call) since the 30 files are static for
    the life of a run -- the same one-time-cost tradeoff `tools/_gm_real_
    player_peak_age.py` already accepts for the identical full-backfill scan.

    `builder_version=None` (RPD-8-7) is the original behaviour, unchanged: every
    row of every file feeds the curve, restricted to the legacy 15 fields.
    With a version, only rows whose `meta.builder_version` equals it (a row
    with none is version 1) feed the curve, restricted to that version's
    `real_sourced_fields`, and a career with fewer than two such seasons gets
    no curve -- mixing a legacy `70 + 12*z*w` season with a version-2
    `T + offset + 8u` one would read the scale change as growth. Callers pass
    the version through `expected_delta_at_age`; call this one with the keyword
    (`_career_curves(builder_version=...)`) so each version has one cache entry."""
    wanted_fields = real_sourced_fields(builder_version)
    raw_by_ref: dict[str, list[tuple[int, int | None, dict[str, float]]]] = {}
    for path in _season_files():
        seed = json.loads(path.read_text(encoding="utf-8"))
        for player in seed["players"]:
            if builder_version is not None and _row_builder_version(player) != builder_version:
                continue
            fields = {
                name: float(value)
                for name, value in player["ratings"].items()
                if name in wanted_fields
            }
            raw_by_ref.setdefault(player["external_ref"], []).append(
                (player["season"], player["draft_year"], fields)
            )

    curves: dict[str, tuple[tuple[int, dict[str, float]], ...]] = {}
    for external_ref, entries in raw_by_ref.items():
        if builder_version is not None and len(entries) < 2:
            continue
        draft_year = entries[0][1]
        effective_draft_year = (
            draft_year if draft_year is not None else min(season for season, _, _ in entries)
        )
        aged = sorted(
            (
                (season - effective_draft_year + _DRAFT_AGE_ANCHOR, fields)
                for season, _, fields in entries
            ),
            key=lambda pair: pair[0],
        )
        curves[external_ref] = tuple(aged)
    return curves


def _interp(points: tuple[tuple[int, float], ...], age: float) -> float:
    """Linear interpolation of one field's rating at `age` between its two
    nearest real ages -- `points` must be sorted ascending by age and
    non-empty. `age` is always within `[points[0][0], points[-1][0]]` at
    every real call site (`expected_delta_at_age` guards both endpoints
    before this is ever reached), so no extrapolation branch exists here."""
    for (age_lo, val_lo), (age_hi, val_hi) in zip(points, points[1:], strict=False):
        if age_lo <= age <= age_hi:
            if age_hi == age_lo:
                return val_lo
            t = (age - age_lo) / (age_hi - age_lo)
            return val_lo + t * (val_hi - val_lo)
    # age matches the last point exactly (the loop's pairwise window never
    # tests a single trailing point on its own).
    return points[-1][1]


def expected_delta_at_age(
    external_ref: str, current_age: int, *, builder_version: int | None = None
) -> dict[str, float] | None:
    """The real-trajectory expected per-field delta for aging `external_
    ref` from `current_age` to `current_age + 1`, or `None` if this player
    has no real career data at all, or `current_age`/`current_age + 1`
    falls outside the real age span on file (see module docstring's "past
    the real career" section) -- the caller's signal to fall back to the
    existing synthetic mechanic rather than a real-trajectory delta.

    `builder_version` (RPD-8-7): `None` reads every season on file (today's
    behaviour); a version reads only that version's seasons, and a career with
    fewer than two of them has no trajectory (`None`)."""
    curve = _career_curves(builder_version=builder_version).get(external_ref)
    if not curve:
        return None

    min_age, max_age = curve[0][0], curve[-1][0]
    if current_age < min_age or current_age + 1 > max_age:
        return None

    field_names = curve[0][1].keys()
    delta: dict[str, float] = {}
    for field_name in field_names:
        points = tuple((age, fields[field_name]) for age, fields in curve)
        delta[field_name] = _interp(points, current_age + 1) - _interp(points, current_age)
    return delta
