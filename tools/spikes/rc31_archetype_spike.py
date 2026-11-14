"""RC-31 archetype spike, re-runnable (RC-34.6 item 2).

**What this measures.** Per-archetype scale constants `s_A` for `arch_z`
(`dba2/services/role_service.py`'s `S_A` table): the population standard
deviation of each archetype's raw composite excess `u_A` (own-profile-
relative, position-adjusted, team-standardized -- `role_service`'s own
`_team_relative_z` / `_archetype_composite_excess`), measured over two
populations:

  1. **Synthetic null** -- 10 seeded leagues x 30 teams x 14 players
     (4,200 players), generated with `dba2.sim.derivation`'s pure generator
     using the exact same `rng_for` salt scheme `tools/seed_dev_league.py`
     uses (`"synthetic", "team"/"player", team_index, slot_index`), so a
     given `--leagues N` reproduces the first N rosters
     `tools/seed_dev_league.py --rng-seed 1..N` would persist.
  2. **Real players** -- `seeds/real_players/2025.json` and `2019.json`,
     grouped by each player's own real team.

Per RC-31.5's rule (restated in `role_service`'s module docstring): an
archetype whose real-vs-synthetic `s_A` diverges by more than 25% (relative
to the synthetic value) ships the real (pooled-across-both-seasons) value;
the rest ship the synthetic value. This script prints both measurements,
the divergence, the resolved S_A table, and diffs it against the `S_A`
literally read out of `dba2/services/role_service.py`'s own source, so a
future drift between this script and that module is a loud, visible
mismatch rather than a silent one.

**Why this doesn't just `import dba2.services.role_service`.** At the time
this script was written, that module transitively imports the full
simulation engine (`dba2.sim.game` -> `dba2.sim.possession` ->
`dba2.sim.beats.rotation`), which is mid-refactor and fails to import in
this working tree (`RotationBranchTuning` missing `pop_share_anchor_low`,
an unrelated in-progress feature, uncommitted) -- confirmed against a fully
clean HEAD checkout as well as the in-progress tree; either way this
read-only spike must not depend on a stateful, changeful production import
graph to compute a comparatively small statistic. So the two tiny pure
functions this script actually needs (`_position_adjust`,
`_team_relative_z`, `_archetype_composite_excess`) are reproduced verbatim
below -- **not** hand-copied constants -- and `ARCHETYPES`/`S_A` (the two
literal tables that matter for the reproduction check) are parsed directly
out of `role_service.py`'s own source text via `ast.literal_eval` rather
than retyped, so they can never silently drift from what is actually
shipped. `POOL_FIELDS` is derived the same way `role_service.py` derives
it, straight from `dba2.sim.ratings._OVERALL_WEIGHTING_GROUPS` (a module
that imports cleanly on its own). If `role_service.py`'s import chain is
fixed later, re-running this script still reproduces the same numbers --
nothing here is a fallback that goes stale, it is simply decoupled from a
graph a read-only measurement script has no business needing in the first
place.

**Read-only.** No database connection; no writes to `seeds/`,
`role_service.py`, or any other committed file -- pure in-memory generation
(`dba2.sim.derivation`), one AST parse of `role_service.py`'s source text
(never executed), and a read of the two committed real-player JSON files.
Cites RC-31 / RC-34.6 item 2 (`docs/ledger/reports/rc31-archetype-spike.md`,
`docs/design/auto-gm-role-construction.md`); no runnable copy of the
original spike's harness was ever committed (see that report's own header)
-- this is a from-scratch reproduction of its protocol, not a recovered
copy.

**Since RC-37's build** `role_service.py` no longer carries an `S_A`
literal: it pins `RHO_BAR`, `Z_FLOOR` and `FLOOR_RANK` and derives `S_A`
from `RHO_BAR`. The legacy paths above (the default report, `--reproduce`)
measure the table that shipped before that, pinned here as
`RETIRED_MIXED_S_A`. `--check` is RC-35.4 D7's drift gate for the new
pins: it re-derives all three from the production population and exits
non-zero if any literal in `role_service.py` disagrees, or if the pinned
cell no longer clears criterion 1 (7/7 >= 90%) and criterion 7b (>= 95%).

Usage (run with `-m` from the repo root -- see `tools/seed_dev_league.py`'s
own docstring for why a bare script path breaks the `dba2` import):

    python -m tools.spikes.rc31_archetype_spike
    python -m tools.spikes.rc31_archetype_spike --check
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dba2.core.rng import rng_for
from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, RatingSource
from dba2.sim import derivation as d
from dba2.sim.derivation import POSITION_RATING_OFFSETS
from dba2.sim.ratings import (
    _OVERALL_WEIGHTING_GROUPS,  # noqa: SLF001 -- role_service's own cited pool source
    compute_overall,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REAL_PLAYERS_DIR = _REPO_ROOT / "seeds" / "real_players"
_TEAMS_JSON_PATH = _REPO_ROOT / "seeds" / "teams.json"
_ROLE_SERVICE_PATH = _REPO_ROOT / "dba2" / "services" / "role_service.py"

_TEAMS_PER_SYNTHETIC_LEAGUE = 30
_PLAYERS_PER_TEAM = 14
_DEFAULT_SYNTHETIC_LEAGUE_COUNT = 10
_REAL_SEASONS: tuple[int, ...] = (2025, 2019)
# RC-31.5 / RC-28.5: an archetype's real-vs-synthetic s_A divergence beyond
# this fraction (of the synthetic value) ships the real value instead.
# RC-35.4 D1 repeals the rule this constant drives; kept only for the
# `--reproduce`/default legacy report paths, which measure what shipped.
_DIVERGENCE_THRESHOLD = 0.25
# role_service.py's own pinned standardization floor (a team SD under this
# makes every z on that field exactly 0.0, never a division blow-up).
_STANDARDIZATION_FLOOR = 1e-9

# RC-35.4 D2 -- the production calibration population's own window:
# `real-player-ratings-v2.md`'s status line refuses legacy (builder_version
# != 2) seasons; every seed file 2009-2025 is version 2 (confirmed by this
# spike's own loader, which asserts it).
_PRODUCTION_SEASONS: tuple[int, ...] = tuple(range(2009, 2026))
_PLAYERS_PER_TEAM_PRODUCTION = 14
# tools/seed_dev_league.py's own mapping: seeds/teams.json says BRK for
# Brooklyn; the BDL-derived real-player files say BKN.
_REAL_DATA_TEAM_CODE: Mapping[str, str] = {"BRK": "BKN"}

TeamRoster = list[tuple[object, Position, dict[str, float]]]


# ---------------------------------------------------------------------------
# Sourced directly from role_service.py -- see module docstring's "why not
# just import role_service" section.
# ---------------------------------------------------------------------------


def _pool_fields() -> tuple[str, ...]:
    """Identical derivation to `role_service.POOL_FIELDS`: the 40-field pool
    minus PHYSICAL/MENTAL/DEVELOPMENT, from the same source table."""
    pool_groups = tuple(
        g for g in _OVERALL_WEIGHTING_GROUPS if g not in ("PHYSICAL", "MENTAL", "DEVELOPMENT")
    )
    fields = tuple(f for g in pool_groups for f in _OVERALL_WEIGHTING_GROUPS[g])
    assert len(fields) == 40, f"RC-5.2/RC-28.1's 40-field pool drifted: got {len(fields)}"
    return fields


def _literal_from_role_service(name: str) -> object:
    """The literal value of a top-level `name = ...` (or annotated
    `name: T = ...`) assignment in `role_service.py`, read via `ast` --
    parses the source text only, never imports or executes the module.
    Used for `ARCHETYPES` (a dict literal) and RC-37's three numeric pins
    `RHO_BAR` / `Z_FLOOR` / `FLOOR_RANK`. `S_A` is no longer a literal
    there -- RC-37 derives it from `RHO_BAR` at import -- so it is not
    read this way any more."""
    source = _ROLE_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROLE_SERVICE_PATH))
    for node in tree.body:
        target_name = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            (target,) = node.targets
            if isinstance(target, ast.Name):
                target_name = target.id
        if target_name == name:
            return ast.literal_eval(node.value)
    raise RuntimeError(
        f"{name!r} is no longer a top-level literal assignment in role_service.py -- "
        "resync this spike's extraction logic (or the archetype/scale tables moved)."
    )


POOL_FIELDS: tuple[str, ...] = _pool_fields()
ARCHETYPES: Mapping[str, tuple[str, ...]] = _literal_from_role_service("ARCHETYPES")
ROLE_SERVICE_RHO_BAR = float(_literal_from_role_service("RHO_BAR"))
ROLE_SERVICE_Z_FLOOR = float(_literal_from_role_service("Z_FLOOR"))
ROLE_SERVICE_FLOOR_RANK = int(_literal_from_role_service("FLOOR_RANK"))

# The mixed per-archetype table `role_service.py` shipped from RC-31's build
# until RC-37's (RC-35.1 found its normalizer bug). `--reproduce` (RC-35.6
# P1) and the default legacy report both measure *that* table, so it is
# pinned here verbatim now that `role_service` derives `S_A` from `RHO_BAR`
# instead of carrying a literal.
RETIRED_MIXED_S_A: Mapping[str, float] = {
    "rebounder": 0.4311,
    "rim_protector": 0.4058,
    "perimeter_defender": 0.2109,
    "floor_spacer": 0.3678,
    "shot_creator": 0.3981,
    "playmaker": 0.3160,
    "interior_finisher": 0.3679,
}


def _position_adjust(raw: float, position: Position, field: str) -> float:
    """Mirrors `role_service._position_adjust` verbatim."""
    return raw - POSITION_RATING_OFFSETS[position][field]


def _team_relative_z(
    rows: Sequence[tuple[object, Position, Mapping[str, float]]],
) -> dict[object, dict[str, float]]:
    """Mirrors `role_service._team_relative_z` verbatim (RC-28.1 steps 1-2):
    position-adjust every field, then z-score within this roster (population
    SD -- T1's documented "population SD, not LOO" default). A team SD under
    `_STANDARDIZATION_FLOOR` makes every z on that field exactly 0.0."""
    adjusted: dict[object, dict[str, float]] = {
        pid: {f: _position_adjust(raw[f], pos, f) for f in POOL_FIELDS} for pid, pos, raw in rows
    }
    result: dict[object, dict[str, float]] = {pid: {} for pid, _, _ in rows}
    for f in POOL_FIELDS:
        values = [adjusted[pid][f] for pid, _, _ in rows]
        mean = statistics.mean(values)
        sd = statistics.pstdev(values)
        for pid, _, _ in rows:
            result[pid][f] = 0.0 if sd < _STANDARDIZATION_FLOOR else (adjusted[pid][f] - mean) / sd
    return result


def _archetype_composite_excess(y: Mapping[str, float]) -> dict[str, float]:
    """Mirrors `role_service._archetype_composite_excess` verbatim (RC-28.1
    steps 3-4): own-profile mean, then each archetype's composite excess
    over it."""
    y_bar = statistics.mean(y[f] for f in POOL_FIELDS)
    return {
        name: statistics.mean(y[f] for f in fields) - y_bar for name, fields in ARCHETYPES.items()
    }


# ---------------------------------------------------------------------------
# Synthetic null population (T1's protocol)
# ---------------------------------------------------------------------------


def _synthetic_team_roster(league_seed: int, team_index: int) -> TeamRoster:
    """One team's 14 players, generated exactly as `tools/seed_dev_league.py`
    would persist them for `--rng-seed league_seed` -- same salt scheme, same
    pure generator functions, so this reproduces a real dev-league roster
    rather than an ad hoc distribution."""
    team_rng = rng_for(league_seed, "synthetic", "team", team_index)
    team_offset = team_rng.gauss(0.0, 2.5)
    positions = d.synthetic_positions(team_rng)

    roster: TeamRoster = []
    for slot_index in range(_PLAYERS_PER_TEAM):
        position = positions[slot_index % len(positions)]
        tier = (
            d.TIER_BY_SLOT[slot_index] if slot_index < len(d.TIER_BY_SLOT) else d.RosterTier.BENCH
        )
        player_rng = rng_for(league_seed, "synthetic", "player", team_index, slot_index)
        # `_create_synthetic_player` (tools/seed_dev_league.py) draws
        # physicals from this same stream BEFORE talent/ratings -- the
        # result is discarded here (this spike has no use for it), but the
        # draw itself must still happen, in the same order, or every talent
        # and ratings draw for this slot desyncs from what a real dev
        # league seeded at `league_seed` would actually persist.
        d.synthetic_physicals(position, player_rng)
        talent = d.tier_talent(tier, team_offset, player_rng)
        ratings = d.synthetic_ratings(talent, position, player_rng)
        player_id = (league_seed, team_index, slot_index)
        raw = {f: float(ratings[f]) for f in POOL_FIELDS}
        roster.append((player_id, position, raw))
    return roster


def synthetic_rosters(league_count: int = _DEFAULT_SYNTHETIC_LEAGUE_COUNT) -> list[TeamRoster]:
    """`league_count` synthetic leagues (seeds `1..league_count`) x 30 teams
    -- one roster per team, in generation order."""
    return [
        _synthetic_team_roster(league_seed, team_index)
        for league_seed in range(1, league_count + 1)
        for team_index in range(_TEAMS_PER_SYNTHETIC_LEAGUE)
    ]


# ---------------------------------------------------------------------------
# Real-player population (T5's protocol)
# ---------------------------------------------------------------------------


def _load_real_rosters(season: int) -> list[TeamRoster]:
    """One roster per real team that season, `seeds/real_players/<season>.json`
    grouped by each row's own `team.id` -- never re-teamed or resampled."""
    path = _REAL_PLAYERS_DIR / f"{season}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["players"]

    by_team: dict[int, TeamRoster] = {}
    for row in rows:
        position = Position(row["primary_position"])
        raw = {f: float(row["ratings"][f]) for f in POOL_FIELDS}
        by_team.setdefault(row["team"]["id"], []).append((row["external_ref"], position, raw))
    return list(by_team.values())


def real_rosters(seasons: tuple[int, ...] = _REAL_SEASONS) -> list[TeamRoster]:
    """Real rosters pooled across every season in `seasons` (each team-season
    is its own roster -- the same franchise in two seasons contributes two
    separate rosters, never merged)."""
    rosters: list[TeamRoster] = []
    for season in seasons:
        rosters.extend(_load_real_rosters(season))
    return rosters


# ---------------------------------------------------------------------------
# RC-35.4 D2 / RC-35.7 item 1 -- the production calibration population:
# `tools/seed_dev_league.py::_real_players_on_their_real_teams`'s own
# grouping (team.abbreviation via `_REAL_DATA_TEAM_CODE`, sorted by
# `meta.minutes_total` descending, top `per_team`), NOT this spike's
# `_load_real_rosters`' group-by-`team.id` (RC-35.2 defect 2: that groups
# every row on a team regardless of minutes, averaging ~18-19/roster, not
# production's fixed top-14-by-minutes shape).
# ---------------------------------------------------------------------------


def _team_codes() -> list[str]:
    data = json.loads(_TEAMS_JSON_PATH.read_text(encoding="utf-8"))
    return [t["code"] for t in data["teams"]]


def _real_players_on_their_real_teams(
    pool: list[dict], *, team_codes: list[str], per_team: int
) -> dict[str, list[dict] | None]:
    """Mirrors `tools/seed_dev_league.py::_real_players_on_their_real_teams`
    verbatim (read at HEAD to confirm this, per RC-35.4 D2's own
    instruction). Unlike production -- which hard-errors on a short
    franchise -- returns `None` for that team-season so the caller can
    report the shortfall as a disclosed exclusion (RC-35.10 risk 4) rather
    than abort the whole calibration run; this script is a read-only
    measurement, not the seeding path itself."""
    by_code: dict[str, list[dict]] = {}
    for row in pool:
        by_code.setdefault(row["team"]["abbreviation"], []).append(row)
    placed: dict[str, list[dict] | None] = {}
    for code in team_codes:
        rows = sorted(
            by_code.get(_REAL_DATA_TEAM_CODE.get(code, code), []),
            key=lambda r: r["meta"].get("minutes_total", 0),
            reverse=True,
        )
        placed[code] = rows[:per_team] if len(rows) >= per_team else None
    return placed


def production_rosters(
    seasons: tuple[int, ...] = _PRODUCTION_SEASONS,
    per_team: int = _PLAYERS_PER_TEAM_PRODUCTION,
) -> tuple[list[TeamRoster], dict[str, int]]:
    """RC-35.4 D2's single calibration population: every real franchise-
    season's top `per_team` by `meta.minutes_total`, on the franchise it
    actually played for. Returns `(rosters, shortfall_by_season_code)` --
    `shortfall_by_season_code` names every franchise-season with fewer than
    `per_team` real rows that season (production would hard-error; this is
    a real finding D2's own "17 x 30 x 14" arithmetic did not anticipate --
    see the build report), mapped to how many rows that team-season actually
    had. Never silently dropped."""
    codes = _team_codes()
    rosters: list[TeamRoster] = []
    shortfalls: dict[str, int] = {}
    for season in seasons:
        rows = json.loads((_REAL_PLAYERS_DIR / f"{season}.json").read_text(encoding="utf-8"))[
            "players"
        ]
        versions = {r.get("meta", {}).get("builder_version") for r in rows}
        if versions != {2}:
            raise RuntimeError(
                f"season {season} is not uniformly builder_version 2 (got {versions}) -- "
                "RC-35.4 D2's production window assumes every 2009-2025 season already is"
            )
        placed = _real_players_on_their_real_teams(rows, team_codes=codes, per_team=per_team)
        for code, team_rows in placed.items():
            if team_rows is None:
                actual_code = _REAL_DATA_TEAM_CODE.get(code, code)
                shortfalls[f"{season}:{code}"] = sum(
                    1 for r in rows if r["team"]["abbreviation"] == actual_code
                )
                continue
            roster: TeamRoster = [
                (
                    r["external_ref"],
                    Position(r["primary_position"]),
                    # All 57 rating columns, not just the 40-field pool --
                    # the fixture harness (RC-35.7 item 4) needs the full
                    # profile to recompute `overall_cached` via
                    # `sim.ratings.compute_overall`. Every other reader in
                    # this module only ever indexes `POOL_FIELDS`, so the
                    # extra columns are inert for them.
                    {f: float(v) for f, v in r["ratings"].items()},
                )
                for r in team_rows
            ]
            rosters.append(roster)
    return rosters, shortfalls


def sigma_field(rosters: list[TeamRoster]) -> float:
    """RC-35.7 item 2: the within-team position-adjusted per-field
    population SD, averaged over the 40 pool fields and every roster in
    `rosters` -- RC-31 T3's 9.757 (raw 11.884) on synthetic is the control
    this must reproduce when called on `synthetic_rosters()`."""
    sds: list[float] = []
    for roster in rosters:
        for f in POOL_FIELDS:
            adjusted = [_position_adjust(raw[f], pos, f) for _pid, pos, raw in roster]
            sds.append(statistics.pstdev(adjusted))
    return statistics.mean(sds)


# ---------------------------------------------------------------------------
# RC-35.4 D4 -- the archetype-block permutation null, plus the exchangeable-
# permutation control (RC-35.7 item 3).
# ---------------------------------------------------------------------------


def _derangement(n: int, rng: random.Random) -> list[int]:
    """A permutation of `range(n)` with no fixed points (rejection
    sampling -- expected ~e attempts at any n >= 2)."""
    if n < 2:
        raise ValueError("no derangement exists for n < 2")
    while True:
        perm = list(range(n))
        rng.shuffle(perm)
        if all(perm[i] != i for i in range(n)):
            return perm


def _joint_block_permuted_roster(roster: TeamRoster, rng: random.Random) -> TeamRoster:
    """D4: for each archetype independently, one derangement of this
    roster's players applied to *only* that archetype's field block,
    leaving every other field (including every other archetype's block)
    untouched. Doing all seven derangements on the same roster at once (one
    combined permuted roster, not seven separate copies) is what lets the
    same run also measure the null's cross-archetype correlation
    (`ρ̄`, D4's last paragraph)."""
    n = len(roster)
    raws: dict[int, dict[str, float]] = {i: dict(raw) for i, (_pid, _pos, raw) in enumerate(roster)}
    for _archetype, fields in ARCHETYPES.items():
        perm = _derangement(n, rng)
        source_values = {f: [roster[i][2][f] for i in range(n)] for f in fields}
        for i in range(n):
            for f in fields:
                raws[i][f] = source_values[f][perm[i]]
    return [(roster[i][0], roster[i][1], raws[i]) for i in range(n)]


def _exchangeable_permuted_roster(roster: TeamRoster, rng: random.Random) -> TeamRoster:
    """D4's control: every one of the 40 pool fields independently
    deranged across the roster (not just archetype blocks), destroying
    every within-player cross-field correlation, benign or not."""
    n = len(roster)
    raws: dict[int, dict[str, float]] = {i: dict(raw) for i, (_pid, _pos, raw) in enumerate(roster)}
    for f in POOL_FIELDS:
        perm = _derangement(n, rng)
        original = [roster[i][2][f] for i in range(n)]
        for i in range(n):
            raws[i][f] = original[perm[i]]
    return [(roster[i][0], roster[i][1], raws[i]) for i in range(n)]


def block_permutation_null(
    rosters: list[TeamRoster], *, rng_seed: int = 1
) -> tuple[dict[str, float], dict[str, list[float]]]:
    """D4's estimator: `s_A^null` per archetype, from one joint block
    permutation per roster. Returns `(s_a_null, u_by_archetype)` --
    `u_by_archetype`'s lists are aligned player-for-player across
    archetypes (same traversal order every time), which is what makes the
    permuted `ρ̄` computable from this single pass."""
    rng = random.Random(rng_seed)
    u_by_archetype: dict[str, list[float]] = {name: [] for name in ARCHETYPES}
    for roster in rosters:
        if len(roster) < 2:
            continue
        permuted = _joint_block_permuted_roster(roster, rng)
        y_by_player = _team_relative_z(permuted)
        for pid, _pos, _raw in permuted:
            u = _archetype_composite_excess(y_by_player[pid])
            for name in ARCHETYPES:
                u_by_archetype[name].append(u[name])
    s_a_null = {name: statistics.pstdev(vals) for name, vals in u_by_archetype.items()}
    return s_a_null, u_by_archetype


def exchangeable_permutation_s_a(
    rosters: list[TeamRoster], *, rng_seed: int = 2
) -> dict[str, float]:
    """D4's closed-form control population: `s_A` measured after
    independently deranging every one of the 40 pool fields (not just
    archetype blocks)."""
    rng = random.Random(rng_seed)
    u_by_archetype: dict[str, list[float]] = {name: [] for name in ARCHETYPES}
    for roster in rosters:
        if len(roster) < 2:
            continue
        permuted = _exchangeable_permuted_roster(roster, rng)
        y_by_player = _team_relative_z(permuted)
        for pid, _pos, _raw in permuted:
            u = _archetype_composite_excess(y_by_player[pid])
            for name in ARCHETYPES:
                u_by_archetype[name].append(u[name])
    return {name: statistics.pstdev(vals) for name, vals in u_by_archetype.items()}


def exchangeable_closed_form(m: int) -> float:
    """D4's stated closed form for the exchangeable-permutation null:
    `Var(u_A) = sigma_p^2 * (1/m - 1/40) * 40/39`, with `sigma_p^2 = 1`
    since a team-standardized field's population variance is exactly 1 by
    construction (`_team_relative_z`'s own `pstdev` normalization)."""
    variance = (1.0 / m - 1.0 / 40.0) * (40.0 / 39.0)
    return variance**0.5


def mean_pairwise_correlation(u_by_archetype: dict[str, list[float]]) -> float:
    """`ρ̄`: mean of the C(7,2)=21 pairwise Pearson correlations across the
    seven archetypes' `u` values (RC-31's own measured −0.11 on the
    unpermuted synthetic null; D4's last paragraph asks for this same
    number computed on the permuted population, as a check the
    independence approximation cost nothing)."""
    names = list(ARCHETYPES)
    coeffs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = u_by_archetype[names[i]], u_by_archetype[names[j]]
            coeffs.append(statistics.correlation(a, b))
    return statistics.mean(coeffs)


# ---------------------------------------------------------------------------
# RC-36.11 item 1 -- the P1 blocker. One pass, no RNG: `rho_bar` (mean
# off-diagonal correlation among the 40 pooled fields) and `V_bar =
# Var_p(ybar_p)` (the general-quality share of roster variance), plus P2's
# roster identity `E_p[sigma_p^2] + Var_p(ybar_p) = 1`, checked to 1e-9 on
# every roster that has no degenerate (floored) field.
# ---------------------------------------------------------------------------


def roster_baseline_variance(
    rosters: list[TeamRoster],
) -> tuple[float, float, list[dict[str, object]]]:
    """RC-36.2's identity, measured directly. For every roster: run
    `_team_relative_z`, then for each player compute `ybar_p` (mean of
    their own 40 team-relative z-values) and `sigma_p^2` (population
    variance of the same 40 values). Returns `(rho_bar, v_bar, per_roster)`:

    - `v_bar` = `Var_p(ybar_p)` pooled over every player in every roster
      passed in (RC-36.2's identity holds exactly *per roster*, and every
      roster here contributes cells to the same grand total, so pooling
      preserves it exactly -- see this function's own assertion below).
    - `rho_bar` = `(40 * v_bar - 1) / 39`, RC-36.2's closed form.
    - `per_roster` names, for every roster, its own `v_bar`, its own mean
      `sigma_p^2`, the identity's LHS (`v_bar + mean_sigma_sq`), and any
      field that hit `_STANDARDIZATION_FLOOR` on that roster (P2's escape
      hatch: a degenerate field is a real data finding, not silently
      averaged over).
    """
    ybar_pool: list[float] = []
    sigma_sq_pool: list[float] = []
    per_roster: list[dict[str, object]] = []
    for roster in rosters:
        if len(roster) < 2:
            continue
        degenerate_fields = [
            f
            for f in POOL_FIELDS
            if statistics.pstdev([_position_adjust(raw[f], pos, f) for _pid, pos, raw in roster])
            < _STANDARDIZATION_FLOOR
        ]
        y_by_player = _team_relative_z(roster)
        roster_ybar: list[float] = []
        roster_sigma_sq: list[float] = []
        for pid, _pos, _raw in roster:
            values = [y_by_player[pid][f] for f in POOL_FIELDS]
            ybar_p = statistics.mean(values)
            sigma_p_sq = statistics.pstdev(values) ** 2
            roster_ybar.append(ybar_p)
            roster_sigma_sq.append(sigma_p_sq)
            ybar_pool.append(ybar_p)
            sigma_sq_pool.append(sigma_p_sq)
        roster_v_bar = statistics.pvariance(roster_ybar)
        roster_mean_sigma_sq = statistics.mean(roster_sigma_sq)
        per_roster.append(
            {
                "n_players": len(roster),
                "v_bar": roster_v_bar,
                "mean_sigma_sq": roster_mean_sigma_sq,
                "identity_lhs": roster_v_bar + roster_mean_sigma_sq,
                "degenerate_fields": degenerate_fields,
            }
        )
    v_bar = statistics.pvariance(ybar_pool)
    rho_bar = (40.0 * v_bar - 1.0) / 39.0
    return rho_bar, v_bar, per_roster


def check_identity(per_roster: list[dict[str, object]], *, tolerance: float = 1e-9) -> list[dict]:
    """P2: assert `identity_lhs` == 1 to `tolerance` on every roster with no
    degenerate field. Returns the list of violating rosters (empty if P2
    holds everywhere) -- never raises, since a violation is itself the
    finding RC-36.9 P2 asks this script to report."""
    violations = []
    for row in per_roster:
        if row["degenerate_fields"]:
            continue  # P2's named escape hatch -- reported separately, not a violation
        if abs(row["identity_lhs"] - 1.0) > tolerance:
            violations.append(row)
    return violations


def null_s_a(rho_bar: float) -> dict[str, float]:
    """RC-36.3 Option A / RC-36.4's table: `S_A = sqrt((1/m - 1/40)(1 -
    rho_bar))`, one value per archetype (three distinct values across the
    seven, since `m` only takes 2/3/4)."""
    return {
        name: ((1.0 / len(fields) - 1.0 / 40.0) * (1.0 - rho_bar)) ** 0.5
        for name, fields in ARCHETYPES.items()
    }


def run_rc36_p1(*, seasons: tuple[int, ...] = _PRODUCTION_SEASONS) -> None:
    """RC-36.12 build-plan step 1: P1 alone, first, before anything else.
    Measures `rho_bar`/`V_bar` on the production population, checks P2's
    identity, derives the N1/N2 table, and reports `r_A^obs` against the
    real production `s_A^obs` -- then applies RC-36.9's own decision rule
    verbatim. Prints, never silently decides to keep going past a failed
    blocker."""
    rosters, shortfalls = production_rosters(seasons)
    n_players = sum(len(r) for r in rosters)
    print(
        f"Production population: {len(rosters)} rosters, {n_players} players "
        f"({len(shortfalls)} franchise-seasons excluded for a real-row shortfall)\n"
    )

    rho_bar, v_bar, per_roster = roster_baseline_variance(rosters)
    violations = check_identity(per_roster)
    degenerate = {
        f: sum(1 for row in per_roster if f in row["degenerate_fields"]) for f in POOL_FIELDS
    }
    degenerate = {f: n for f, n in degenerate.items() if n}

    print("=== P2 -- roster identity E_p[sigma_p^2] + Var_p(ybar_p) = 1 ===")
    print(f"  rosters checked: {len(per_roster)}, violations (>1e-9, no degenerate field): "
          f"{len(violations)}")
    if degenerate:
        print(f"  degenerate (floored) fields, roster-count each: {degenerate}")
    else:
        print("  no degenerate fields on any roster")
    if violations:
        for row in violations[:5]:
            print(f"    VIOLATION: identity_lhs={row['identity_lhs']:.12f} "
                  f"(n_players={row['n_players']})")
    print()

    print("=== P1 -- rho_bar on the production population ===")
    print(f"  V_bar (pooled Var_p(ybar_p)): {v_bar:.6f}")
    print(f"  rho_bar = (40*V_bar - 1)/39:  {rho_bar:.6f}")
    print()

    s_a_obs, n_measured = measure_s_a(rosters)
    s_a_null = null_s_a(rho_bar)
    r_a = {name: s_a_obs[name] / s_a_null[name] for name in ARCHETYPES}
    clears = sum(1 for v in r_a.values() if v >= 1.0)

    header = f"{'archetype':>20}  {'m':>2}  {'s_A^obs':>10}  {'S_A (null)':>10}  {'r_A^obs':>8}"
    print(header)
    print("-" * len(header))
    for name, fields in ARCHETYPES.items():
        print(
            f"{name:>20}  {len(fields):>2}  {s_a_obs[name]:>10.4f}  {s_a_null[name]:>10.4f}  "
            f"{r_a[name]:>8.3f}"
        )
    print(f"\n{clears}/7 archetypes clear r_A^obs >= 1.0 (measured over {n_measured} players)\n")

    print("=== RC-36.9 P1 decision rule ===")
    rho_in_range = 0.55 <= rho_bar <= 0.65
    clears_5_of_7 = clears >= 5
    print(f"  rho_bar in [0.55, 0.65]: {rho_in_range} (measured {rho_bar:.6f})")
    print(f"  r_A^obs clears 1.0 on >=5/7: {clears_5_of_7} (measured {clears}/7)")
    if rho_in_range and clears_5_of_7:
        print("  VERDICT: P1 HOLDS. Proceed to P2/P3 and the rest of RC-36.")
    else:
        print(
            "  VERDICT: The pre-registered rho_bar band (0.55-0.65) did NOT hold "
            f"(measured {rho_bar:.4f}). Per RC-36.9's own 'if it misses' clause, the "
            "named stop condition is specifically 'below 5 of 7' on r_A^obs, which "
            f"did NOT occur ({clears}/7 clear). This is a genuine gap in RC-36's own "
            "written decision rule (it predicts a band but only defines failure via "
            "the r_A count) -- reported here rather than silently resolved either way."
        )


# ---------------------------------------------------------------------------
# RC-36.11 items 3-4 -- Option B's enumerated/sampled bundle control (P3) and
# N1 run by actual permutation (the third check that the closed form really
# is the permutation distribution, not just algebra that happens to match).
# ---------------------------------------------------------------------------


def _collect_y_and_ybar(rosters: list[TeamRoster]) -> tuple[list[dict[str, float]], list[float]]:
    y_rows: list[dict[str, float]] = []
    ybar_rows: list[float] = []
    for roster in rosters:
        if len(roster) < 2:
            continue
        y_by_player = _team_relative_z(roster)
        for pid, _pos, _raw in roster:
            y = y_by_player[pid]
            y_rows.append(y)
            ybar_rows.append(statistics.mean(y[f] for f in POOL_FIELDS))
    return y_rows, ybar_rows


def bundle_reference_s_a(
    rosters: list[TeamRoster], *, samples: int = 1000, rng_seed: int = 3
) -> dict[int, float]:
    """RC-36.3 Option B / P3: for each `m` in `{2, 3, 4}` (RC-27's own
    archetype sizes), sample `samples` random `m`-field subsets of the
    40-field pool and compute each subset's population SD of `u_B(p) =
    mean_B(y_p) - ybar_p` over every player in `rosters`, averaging the
    *variance* across subsets before taking the square root (matching
    `E_B[Var(u_B)]`'s own definition, RC-36.2). Returns `{m: s_a_estimate}`,
    the direct enumeration/sampling control on `null_s_a`'s closed form."""
    rng = random.Random(rng_seed)
    y_rows, ybar_rows = _collect_y_and_ybar(rosters)
    n = len(y_rows)
    field_arrays = {f: [y[f] for y in y_rows] for f in POOL_FIELDS}

    results: dict[int, float] = {}
    for m in (2, 3, 4):
        variances = []
        for _ in range(samples):
            subset = rng.sample(POOL_FIELDS, m)
            cols = [field_arrays[f] for f in subset]
            u_values = [sum(col[i] for col in cols) / m - ybar_rows[i] for i in range(n)]
            variances.append(statistics.pvariance(u_values))
        results[m] = statistics.mean(variances) ** 0.5
    return results


def profile_shuffle_null(
    rosters: list[TeamRoster], *, rng_seed: int = 4
) -> dict[str, float]:
    """RC-36.11 item 4 / N1, run for real: for each player independently,
    permute their own 40 team-relative z-values across the 40 field slots,
    then recompute `_archetype_composite_excess` on the shuffled profile.
    Returns `s_A^null` per archetype -- `null_s_a`'s `[EXACT]` closed form
    (same `rho_bar`) predicts these to within the sampling noise of one
    shuffle per player, not by algebra alone."""
    rng = random.Random(rng_seed)
    u_by_archetype: dict[str, list[float]] = {name: [] for name in ARCHETYPES}
    for roster in rosters:
        if len(roster) < 2:
            continue
        y_by_player = _team_relative_z(roster)
        for pid, _pos, _raw in roster:
            values = [y_by_player[pid][f] for f in POOL_FIELDS]
            shuffled = values[:]
            rng.shuffle(shuffled)
            y_shuffled = dict(zip(POOL_FIELDS, shuffled, strict=True))
            u = _archetype_composite_excess(y_shuffled)
            for name in ARCHETYPES:
                u_by_archetype[name].append(u[name])
    return {name: statistics.pstdev(vals) for name, vals in u_by_archetype.items()}


def null_arch_z_distribution(
    rosters: list[TeamRoster], s_a: Mapping[str, float], *, rng_seed: int = 4
) -> list[float]:
    """The N1 null's `arch_z` (max-of-seven-scaled-axes) value for every
    player, from the same per-player profile shuffle `profile_shuffle_null`
    uses -- D13's own definition needs this distribution's p95, not just
    its per-archetype SDs."""
    rng = random.Random(rng_seed)
    out: list[float] = []
    for roster in rosters:
        if len(roster) < 2:
            continue
        y_by_player = _team_relative_z(roster)
        for pid, _pos, _raw in roster:
            values = [y_by_player[pid][f] for f in POOL_FIELDS]
            shuffled = values[:]
            rng.shuffle(shuffled)
            y_shuffled = dict(zip(POOL_FIELDS, shuffled, strict=True))
            u = _archetype_composite_excess(y_shuffled)
            out.append(arch_z_for_u(u, s_a)[0])
    return out


# ---------------------------------------------------------------------------
# RC-35.4 D6 / RC-35.7 item 4 -- the response-defined F1/F2/F3 fixtures and
# the rank-promotion harness. Table-agnostic: takes any `s_a`/cell so it can
# score whichever table a given round actually ships, not just D4's.
# ---------------------------------------------------------------------------

_FIXTURE_FULL_RESPONSE = 5.17  # D6: RC-31's own Delta=+25 median full-cluster response
_FIXTURE_F2_RATIO = 0.6  # D6: preserves RC-31's +15/+25 ratio


def fixture_delta(archetype: str, s_a: Mapping[str, float], sigma_field_p: float) -> float:
    """D6: `Delta_{A,P} = 5.17 * s_A * sigma_field(P) / (1 - m_A/40)`."""
    m = len(ARCHETYPES[archetype])
    return _FIXTURE_FULL_RESPONSE * s_a[archetype] * sigma_field_p / (1 - m / 40)


def _ratings_from_raw(raw: Mapping[str, float]) -> PlayerRatings:
    """Builds a throwaway `PlayerRatings` from a roster row's full 57-field
    profile, just enough to call `sim.ratings.compute_overall` -- mirrors
    `tools/validate_sim.py::_generate_team_roster`'s own construction."""
    int_fields = {"peak_age_start", "peak_age_end"}
    values = {f: (int(round(v)) if f in int_fields else v) for f, v in raw.items()}
    return PlayerRatings(
        player_id=0,
        league_id=0,
        source=RatingSource.REAL_STAT_DERIVED,
        derived_from_season=None,
        confidence=Decimal("1.0"),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        **values,
    )


def _talent(raw: Mapping[str, float], position: Position) -> int:
    return compute_overall(_ratings_from_raw(raw), position)


def _apply_delta(raw: Mapping[str, float], fields: Sequence[str], delta: float) -> dict[str, float]:
    """Raw-rating additive edit, clamped to the `rating` domain's [0, 99]
    (RC-31's own 99-clip handling -- reported, not excluded, by the caller)."""
    out = dict(raw)
    for f in fields:
        out[f] = max(0.0, min(99.0, out[f] + delta))
    return out


def _rank_players(
    roster: TeamRoster, s_a: Mapping[str, float], *, beta: float, z_floor: float, z_cap: float
) -> list[tuple[object, int, float, str | None]]:
    """`role_service.construct_team_roles`'s own ranking rule (talent +
    beta*clamp(z - z_floor, 0, z_cap)), at the `tendency_respecter` identity
    philosophy (`tilted_overall(...) == overall_cached` there -- C10), which
    is what lets `talent` below be plain `compute_overall`. Returns
    `(player_id, rank, bonus, label_if_fired)`, rank 1 = highest score."""
    y_by_player = _team_relative_z(roster)
    scored = []
    for pid, pos, raw in roster:
        talent = _talent(raw, pos)
        u = _archetype_composite_excess(y_by_player[pid])
        z, label = arch_z_for_u(u, s_a)
        clamped = max(0.0, min(z - z_floor, z_cap))
        bonus = beta * clamped
        scored.append((pid, talent + bonus, bonus, label if bonus > 0.0 else None))
    ranked = sorted(scored, key=lambda row: (-row[1], row[0]))
    return [(pid, rank, bonus, label) for rank, (pid, _score, bonus, label) in enumerate(ranked, 1)]


def _rank11_player(roster: TeamRoster) -> object:
    """The rank-11 player by unmodified talent (RC-30.2's own fixture
    target -- ties broken by player_id for determinism)."""
    ranked = sorted(roster, key=lambda row: (-_talent(row[2], row[1]), row[0]))
    return ranked[10][0]


def _within_team_field_sd(roster: TeamRoster, field: str) -> float:
    """D11: the position-adjusted within-team population SD for ONE field
    on ONE roster -- distinct from `sigma_field`'s 40-field-pool average,
    and the correct divisor for F3's per-field delta (RC-36.6 D11: D6's
    exact `Delta_lone = Delta_full/m` identity only holds if F3 is scaled
    by the target field's own spread, not the pooled average)."""
    adjusted = [_position_adjust(raw[field], pos, field) for _pid, pos, raw in roster]
    return statistics.pstdev(adjusted)


def fixture_harness(
    rosters: list[TeamRoster],
    s_a: Mapping[str, float],
    sigma_field_p: float,
    *,
    beta: float,
    z_floor: float,
    z_cap: float,
) -> dict[str, dict[str, float]]:
    """RC-35.7 item 4, with RC-36.11 item 5's four fixes applied (rc35
    correction 4 + RC-36.6 D10/D11):

    (a) clip tracked separately for F1 and F2 (F3/lone is now per-field,
        see (b)); every rank-reach headline reported **clip-included and
        clip-excluded**, with the clip-excluded `n` alongside;
    (b) the lone-field probe (F3) is scored over **every field** in the
        archetype, not just `fields[0]` (D10) -- `kappa_A` takes the worst
        (max) of the per-field lone-conversion rates;
    (c) each field's lone delta is scaled by **that field's own**
        within-team SD (D11's `_within_team_field_sd`), not the pooled
        `sigma_field(P)` average D6 uses for F1/F2;
    (d) `C_lone(A, f)`, `C_full(A)` and `kappa_A` (RC-36.6's 4b' replacement
        for the repealed criterion 4b) are computed **only over the rank-11
        players who do not fire unedited** (RC-36.6's own conditioning).
    """
    results: dict[str, dict[str, float]] = {}
    for archetype, fields in ARCHETYPES.items():
        m = len(fields)
        delta_full = fixture_delta(archetype, s_a, sigma_field_p)
        delta_f2 = delta_full * _FIXTURE_F2_RATIO

        reach9 = reach8 = reach5 = 0
        reach9_incl = reach8_incl = reach5_incl = 0
        f1_bonus_sum = f2_bonus_sum = 0.0
        f1_clipped = f2_clipped = 0
        unedited_fires = 0

        non_firing_n = 0
        full_fires_among_non_firing = 0
        lone_fires_by_field: dict[str, int] = dict.fromkeys(fields, 0)
        lone_clipped_by_field: dict[str, int] = dict.fromkeys(fields, 0)

        n = len(rosters)
        for roster in rosters:
            target_id = _rank11_player(roster)
            idx = next(i for i, row in enumerate(roster) if row[0] == target_id)
            _pid, pos, raw = roster[idx]

            base_ranks = _rank_players(roster, s_a, beta=beta, z_floor=z_floor, z_cap=z_cap)
            base_by_id = {pid: (rank, bonus, label) for pid, rank, bonus, label in base_ranks}
            fires_unedited = base_by_id[target_id][1] > 0.0
            if fires_unedited:
                unedited_fires += 1

            # F1: full cluster
            new_raw_f1 = _apply_delta(raw, fields, delta_full)
            f1_clip = any(raw[f] + delta_full > 99.0 for f in fields)
            if f1_clip:
                f1_clipped += 1
            f1_roster = list(roster)
            f1_roster[idx] = (target_id, pos, new_raw_f1)
            f1_ranks = _rank_players(f1_roster, s_a, beta=beta, z_floor=z_floor, z_cap=z_cap)
            f1_by_id = {pid: (rank, bonus, label) for pid, rank, bonus, label in f1_ranks}
            f1_rank, f1_bonus, _label = f1_by_id[target_id]
            f1_bonus_sum += f1_bonus
            if f1_rank <= 9:
                reach9_incl += 1
            if f1_rank <= 8:
                reach8_incl += 1
            if f1_rank <= 5:
                reach5_incl += 1
            if not f1_clip:
                if f1_rank <= 9:
                    reach9 += 1
                if f1_rank <= 8:
                    reach8 += 1
                if f1_rank <= 5:
                    reach5 += 1

            # F2: 0.6x
            new_raw_f2 = _apply_delta(raw, fields, delta_f2)
            if any(raw[f] + delta_f2 > 99.0 for f in fields):
                f2_clipped += 1
            f2_roster = list(roster)
            f2_roster[idx] = (target_id, pos, new_raw_f2)
            f2_ranks = _rank_players(f2_roster, s_a, beta=beta, z_floor=z_floor, z_cap=z_cap)
            f2_by_id = {pid: (rank, bonus, label) for pid, rank, bonus, label in f2_ranks}
            f2_bonus_sum += f2_by_id[target_id][1]

            # 4b' (RC-36.6): only scored over rank-11 players that do NOT
            # fire unedited -- fires-under-F1 among them is C_full's
            # numerator, fires-under-each-lone-field is C_lone(A, f)'s.
            if fires_unedited:
                continue
            non_firing_n += 1
            if f1_by_id[target_id][1] > 0.0:
                full_fires_among_non_firing += 1
            for f in fields:
                field_delta = (
                    _FIXTURE_FULL_RESPONSE
                    * s_a[archetype]
                    * _within_team_field_sd(roster, f)
                    / (1 - m / 40)
                )
                new_raw_lone = _apply_delta(raw, (f,), field_delta)
                if raw[f] + field_delta > 99.0:
                    lone_clipped_by_field[f] += 1
                lone_roster = list(roster)
                lone_roster[idx] = (target_id, pos, new_raw_lone)
                lone_ranks = _rank_players(
                    lone_roster, s_a, beta=beta, z_floor=z_floor, z_cap=z_cap
                )
                lone_by_id = {pid: (rank, bonus, label) for pid, rank, bonus, label in lone_ranks}
                if lone_by_id[target_id][1] > 0.0:
                    lone_fires_by_field[f] += 1

        c_full = full_fires_among_non_firing / non_firing_n if non_firing_n else float("nan")
        c_lone_by_field = {
            f: (lone_fires_by_field[f] / non_firing_n if non_firing_n else float("nan"))
            for f in fields
        }
        kappa_a = (max(c_lone_by_field.values()) / c_full) if c_full else float("nan")

        results[archetype] = {
            "m": m,
            "reach9_clip_included": reach9_incl / n,
            "reach8_clip_included": reach8_incl / n,
            "reach5_clip_included": reach5_incl / n,
            "reach9_clip_excluded": reach9 / (n - f1_clipped) if n - f1_clipped else float("nan"),
            "reach8_clip_excluded": reach8 / (n - f1_clipped) if n - f1_clipped else float("nan"),
            "reach5_clip_excluded": reach5 / (n - f1_clipped) if n - f1_clipped else float("nan"),
            "f1_clip_rate": f1_clipped / n,
            "f1_excluded_n": n - f1_clipped,
            "f1_mean_bonus": f1_bonus_sum / n,
            "f2_mean_bonus": f2_bonus_sum / n,
            "f2_clip_rate": f2_clipped / n,
            "unedited_fires_rate": unedited_fires / n,
            "non_firing_n": non_firing_n,
            "c_full": c_full,
            "c_lone_by_field": c_lone_by_field,
            "lone_clip_rate_by_field": {
                f: (lone_clipped_by_field[f] / non_firing_n if non_firing_n else float("nan"))
                for f in fields
            },
            "kappa_a": kappa_a,
            "kappa_bar": 1.0 / m,
        }
    return results


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


def measure_s_a(rosters: list[TeamRoster]) -> tuple[dict[str, float], int]:
    """`_team_relative_z` + `_archetype_composite_excess`, run over every
    roster, then the population SD of each archetype's `u_A` across every
    player measured. Returns `(s_a_by_archetype, n_players)`."""
    u_values: dict[str, list[float]] = {name: [] for name in ARCHETYPES}
    n_players = 0
    for roster in rosters:
        y_by_player = _team_relative_z(roster)
        for player_id, _position, _raw in roster:
            u = _archetype_composite_excess(y_by_player[player_id])
            for name, value in u.items():
                u_values[name].append(value)
            n_players += 1
    return {name: statistics.pstdev(values) for name, values in u_values.items()}, n_players


def resolve_s_a(
    synthetic: dict[str, float], real: dict[str, float]
) -> tuple[dict[str, float], dict[str, float]]:
    """RC-31.5's rule: real ships where it diverges from synthetic by more
    than `_DIVERGENCE_THRESHOLD` (relative to the synthetic value), else
    synthetic ships. Returns `(resolved_s_a, relative_divergence_by_archetype)`."""
    divergence = {name: abs(real[name] - synthetic[name]) / synthetic[name] for name in ARCHETYPES}
    resolved = {
        name: real[name] if divergence[name] > _DIVERGENCE_THRESHOLD else synthetic[name]
        for name in ARCHETYPES
    }
    return resolved, divergence


# ---------------------------------------------------------------------------
# RC-35.7 -- the arch_z distribution itself (max over 7 scaled axes), needed
# for RC-35.6's blocking reproductions P1/P2 and everything downstream.
# Mirrors role_service.arch_z verbatim.
# ---------------------------------------------------------------------------


def arch_z_for_u(u: Mapping[str, float], s_a: Mapping[str, float]) -> tuple[float, str]:
    """Mirrors `role_service.arch_z` verbatim: scale each archetype's raw
    composite excess by `s_a`, take the max, ties broken by `ARCHETYPES`'
    declaration order (dict insertion order)."""
    scaled = {name: u[name] / s_a[name] for name in ARCHETYPES}
    label = max(scaled, key=lambda k: scaled[k])
    return scaled[label], label


def collect_u_values(rosters: list[TeamRoster]) -> list[dict[str, float]]:
    """Every player's raw (unscaled) archetype composite-excess vector `u`,
    across every roster, in encounter order."""
    out: list[dict[str, float]] = []
    for roster in rosters:
        y_by_player = _team_relative_z(roster)
        for player_id, _position, _raw in roster:
            out.append(_archetype_composite_excess(y_by_player[player_id]))
    return out


def arch_z_distribution(u_values: list[dict[str, float]], s_a: Mapping[str, float]) -> list[float]:
    """The max-of-seven `arch_z` value for every player in `u_values`, under
    scale table `s_a`."""
    return [arch_z_for_u(u, s_a)[0] for u in u_values]


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (same convention `statistics.quantiles`
    with `n=100` uses at the boundaries), q in [0, 100]."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (q / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def distribution_summary(values: list[float]) -> dict[str, float]:
    """mean/SD/p50/p75/p90/p95/p99 of an `arch_z` (or any) distribution."""
    ordered = sorted(values)
    return {
        "mean": statistics.mean(values),
        "sd": statistics.pstdev(values),
        "p50": _percentile(ordered, 50),
        "p75": _percentile(ordered, 75),
        "p90": _percentile(ordered, 90),
        "p95": _percentile(ordered, 95),
        "p99": _percentile(ordered, 99),
    }


def firing_rate(values: list[float], z_floor: float) -> float:
    """Fraction of players whose max-of-seven `arch_z` clears `z_floor`
    (i.e. `bonus > 0` in `role_service.construct_team_roles` -- `clamped =
    max(0, min(z - z_floor, z_cap))` is > 0 iff `z > z_floor`)."""
    return sum(1 for v in values if v > z_floor) / len(values)


# RC-35.1's table: s_A measured on synthetic for all seven archetypes,
# ignoring RC-31.5's divergence rule entirely (P2's "all-synthetic table").
ALL_SYNTHETIC_S_A: Mapping[str, float] = {
    "rebounder": 0.4311,
    "rim_protector": 0.4058,
    "perimeter_defender": 0.4463,
    "floor_spacer": 0.5875,
    "shot_creator": 0.3981,
    "playmaker": 0.4564,
    "interior_finisher": 0.5998,
}


def run_p1_p2(leagues: int = _DEFAULT_SYNTHETIC_LEAGUE_COUNT) -> None:
    """RC-35.6's blocking reproductions. Both must hold before any
    redesign work proceeds -- see RC-35.6 P1/P2 and RC-35's Step 1."""
    synth_rosters = synthetic_rosters(leagues)
    u_values = collect_u_values(synth_rosters)
    print(f"Synthetic null population: {len(u_values)} players, {len(synth_rosters)} teams "
          f"({leagues} leagues x {_TEAMS_PER_SYNTHETIC_LEAGUE} teams)\n")

    print("=== P1 -- the retired mixed S_A table (shipped until RC-37's build) ===")
    shipped_dist = arch_z_distribution(u_values, RETIRED_MIXED_S_A)
    shipped_summary = distribution_summary(shipped_dist)
    for k, v in shipped_summary.items():
        print(f"  {k:>4}: {v:.4f}")
    for z_floor in (2.20, 3.30):
        fr = firing_rate(shipped_dist, z_floor)
        print(f"  firing rate @ z_floor={z_floor:.2f}: {fr * 100:.2f}%")
    print(
        "  prediction: p90 ~= 3.36, firing ~36% @ 2.20, ~10.8-10.9% @ 3.30\n"
    )

    print("=== P2 -- all-synthetic S_A table (ignores RC-31.5's divergence rule) ===")
    synth_dist = arch_z_distribution(u_values, ALL_SYNTHETIC_S_A)
    synth_summary = distribution_summary(synth_dist)
    for k, v in synth_summary.items():
        print(f"  {k:>4}: {v:.4f}")
    fr220 = firing_rate(synth_dist, 2.20)
    print(f"  firing rate @ z_floor=2.20: {fr220 * 100:.2f}%")
    print("  prediction: p90 ~= 2.168, firing ~= 9.36% @ 2.20\n")


def run_rc36_extended(*, seasons: tuple[int, ...] = _PRODUCTION_SEASONS) -> None:
    """RC-36.12 build-plan step 2-4's measurements, run past P1 regardless of
    its own verdict (RC-36.9's decision rule is genuinely ambiguous on a
    ρ̄-out-of-band-but-r_A-clears outcome -- see `run_rc36_p1`'s own printed
    caveat) so the report has real numbers for every pre-registered
    prediction, not just P1's. Does NOT touch `role_service.py`."""
    rosters, _shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, per_roster = roster_baseline_variance(rosters)
    violations = check_identity(per_roster)
    s_a = null_s_a(rho_bar)

    print(f"rho_bar = {rho_bar:.6f}  (P2 violations: {len(violations)})\n")

    print("=== P3 -- Option B (sampled bundle) control on Option A's closed form ===")
    bundle_s_a = bundle_reference_s_a(rosters, samples=1000)
    for m in (2, 3, 4):
        closed = s_a[next(name for name, f in ARCHETYPES.items() if len(f) == m)]
        sampled = bundle_s_a[m]
        pct_diff = (sampled - closed) / closed * 100
        print(f"  m={m}: closed-form S_A={closed:.4f}  sampled(Option B)={sampled:.4f}  "
              f"diff={pct_diff:+.2f}%")
    print()

    print("=== N1 run by actual permutation (third check on the closed form) ===")
    shuffled_s_a = profile_shuffle_null(rosters)
    for name, fields in ARCHETYPES.items():
        m = len(fields)
        predicted = s_a[name]
        measured = shuffled_s_a[name]
        pct_diff = (measured - predicted) / predicted * 100
        print(f"  {name:>20} (m={m}): closed-form={predicted:.4f}  "
              f"shuffled(N1)={measured:.4f}  diff={pct_diff:+.2f}%")
    print()

    print("=== D13 -- z_floor as the empirical p95 of the N1 null arch_z distribution ===")
    null_dist = null_arch_z_distribution(rosters, s_a)
    null_summary = distribution_summary(null_dist)
    for k, v in null_summary.items():
        print(f"  null {k:>4}: {v:.4f}")
    z_floor = null_summary["p95"]
    gaussian_equiv = None
    try:
        import statistics as _st
        gaussian_equiv = _st.NormalDist().inv_cdf(0.95 ** (1 / 7))
    except Exception:  # noqa: BLE001 -- best-effort reporting only
        pass
    print(f"  D13 z_floor (empirical p95): {z_floor:.4f}")
    if gaussian_equiv is not None:
        print(f"  Gaussian-equivalent floor (P5 comparison): {gaussian_equiv:.4f}  "
              f"gap={gaussian_equiv - z_floor:+.4f}")
    print("  prediction: 2.30-2.45\n")

    print("=== Criterion 5 (by construction) and criterion 7 (operational rate) ===")
    null_fire_5 = firing_rate(null_dist, z_floor)
    print(f"  criterion 5 (null firing rate @ z_floor): {null_fire_5 * 100:.3f}% (expect ~5.000%)")
    real_u_values = collect_u_values(rosters)
    real_dist = arch_z_distribution(real_u_values, s_a)
    op_rate = firing_rate(real_dist, z_floor)
    print(f"  criterion 7 (operational firing rate on real population): {op_rate * 100:.2f}%")
    print("  prediction: 8-14%, strictly above criterion 5's 5.0%\n")

    print("=== Criterion 4b' -- kappa_A, D10/D11-fixed fixture harness ===")
    sigma_field_p = sigma_field(rosters)
    beta, z_cap = 4.0, 1.5
    harness = fixture_harness(rosters, s_a, sigma_field_p, beta=beta, z_floor=z_floor, z_cap=z_cap)
    for name in ARCHETYPES:
        row = harness[name]
        m = row["m"]
        bar = 1.0 / m
        gated = "gated (m>=3)" if m >= 3 else "reported only (m=2)"
        status = "PASS" if row["kappa_a"] <= bar else "FAIL"
        print(
            f"  {name:>20} (m={m}, {gated}): kappa_A={row['kappa_a']:.4f} vs 1/m={bar:.4f} "
            f"-> {status}  (n non-firing={row['non_firing_n']}, C_full={row['c_full']:.4f})"
        )
    print()

    print("=== Criteria 1/2/3, clip-included vs. clip-excluded (rc35 correction 4) ===")
    for name in ARCHETYPES:
        row = harness[name]
        print(
            f"  {name:>20}: reach9 incl={row['reach9_clip_included']*100:.1f}% "
            f"excl={row['reach9_clip_excluded']*100:.1f}%  "
            f"reach5 incl={row['reach5_clip_included']*100:.1f}% "
            f"excl={row['reach5_clip_excluded']*100:.1f}%  "
            f"f1_clip={row['f1_clip_rate']*100:.1f}% (excluded_n={row['f1_excluded_n']})"
        )
    print()

    print("=== Cross-population shape check (RC-36.3 Option A's decisive property) ===")
    synth_rosters = synthetic_rosters(_DEFAULT_SYNTHETIC_LEAGUE_COUNT)
    synth_rho_bar, _synth_v_bar, _synth_per_roster = roster_baseline_variance(synth_rosters)
    print(f"  rho_bar production: {rho_bar:.6f}   rho_bar synthetic: {synth_rho_bar:.6f}")
    for m in (2, 3, 4):
        prod_val = ((1.0 / m - 1.0 / 40.0) * (1.0 - rho_bar)) ** 0.5
        synth_val = ((1.0 / m - 1.0 / 40.0) * (1.0 - synth_rho_bar)) ** 0.5
        print(f"    m={m}: S_A(prod)={prod_val:.4f}  S_A(synth)={synth_val:.4f}  "
              f"ratio={prod_val / synth_val:.4f}")
    print("  (RC-36.4 claims this ratio is identical -- 1.000x -- across all three m,\n"
          "   since only (1 - rho_bar) differs and it cancels in any within-population\n"
          "   ratio; this print shows the cross-population ratio itself, i.e. how far a\n"
          "   table fit on one population would be off on the other.)\n")


# ---------------------------------------------------------------------------
# RC-37 -- the cell-independent fixture precompute, the production rank-gap
# profile, the F1 decomposition, and the (beta, z_floor, z_cap, fixture)
# sweep. `fixture_harness` above recomputes `compute_overall` for all 14
# players on every ranking (7 archetypes x 501 rosters x (3 + m) rankings x
# 14 exact-Fraction overalls), which is why one cell costs ~4 minutes and a
# sweep was never run on the production population (rc35 report: "a full
# sweep ... was not run"). Nothing about `talent` or `arch_z` depends on the
# cell -- only `bonus = beta * clamp(z - z_floor, 0, z_cap)` does -- so every
# state is precomputed once as 14 `(talent, arch_z)` pairs and a cell is a
# sort. A fixture edit changes exactly one player's talent, so each state
# costs one `compute_overall`, not fourteen.
# ---------------------------------------------------------------------------

RosterState = list[tuple[object, int, float, str]]  # (pid, talent, arch_z, argmax label)


@dataclass(frozen=True)
class FixtureState:
    state: RosterState
    clipped: bool  # the additive edit pushed a field past the 99 ceiling


@dataclass(frozen=True)
class RosterFixtures:
    base: RosterState
    target_id: object  # the rank-11 player by unmodified talent
    f1: dict[str, FixtureState]  # archetype -> full-cluster edit
    f2: dict[str, FixtureState]  # archetype -> 0.6x full-cluster edit
    lone: dict[str, dict[str, FixtureState]]  # archetype -> field -> lone-field edit


def _state_for(
    roster: TeamRoster, s_a: Mapping[str, float], talents: Mapping[object, int]
) -> RosterState:
    y_by_player = _team_relative_z(roster)
    out: RosterState = []
    for pid, _pos, _raw in roster:
        z, label = arch_z_for_u(_archetype_composite_excess(y_by_player[pid]), s_a)
        out.append((pid, talents[pid], z, label))
    return out


def _edited_state(
    roster: TeamRoster,
    s_a: Mapping[str, float],
    talents: Mapping[object, int],
    idx: int,
    fields: Sequence[str],
    delta: float,
) -> FixtureState:
    pid, pos, raw = roster[idx]
    new_raw = _apply_delta(raw, fields, delta)
    clipped = any(raw[f] + delta > 99.0 for f in fields)
    edited = list(roster)
    edited[idx] = (pid, pos, new_raw)
    new_talents = dict(talents)
    new_talents[pid] = _talent(new_raw, pos)
    return FixtureState(state=_state_for(edited, s_a, new_talents), clipped=clipped)


def precompute_fixtures(
    rosters: list[TeamRoster],
    s_a: Mapping[str, float],
    sigma_field_p: float,
    *,
    full_response: float = _FIXTURE_FULL_RESPONSE,
    flat_delta: float | None = None,
) -> list[RosterFixtures]:
    """One pass building every cell-independent state `fixture_harness`
    would otherwise rebuild per cell. `flat_delta=None` is D6's response-
    defined fixture at `full_response` (F1/F2 via `fixture_delta`, lone
    fields via D11's per-field SD); a float is RC-31's original flat raw
    delta applied to every edited field (F1: all, F2: 0.6x, lone: one)."""
    out: list[RosterFixtures] = []
    for roster in rosters:
        talents = {pid: _talent(raw, pos) for pid, pos, raw in roster}
        base = _state_for(roster, s_a, talents)
        target_id = sorted(roster, key=lambda row: (-talents[row[0]], row[0]))[10][0]
        idx = next(i for i, row in enumerate(roster) if row[0] == target_id)
        f1: dict[str, FixtureState] = {}
        f2: dict[str, FixtureState] = {}
        lone: dict[str, dict[str, FixtureState]] = {}
        for archetype, fields in ARCHETYPES.items():
            m = len(fields)
            if flat_delta is None:
                delta_full = full_response * s_a[archetype] * sigma_field_p / (1 - m / 40)
            else:
                delta_full = flat_delta
            f1[archetype] = _edited_state(roster, s_a, talents, idx, fields, delta_full)
            f2[archetype] = _edited_state(
                roster, s_a, talents, idx, fields, delta_full * _FIXTURE_F2_RATIO
            )
            lone[archetype] = {}
            for f in fields:
                if flat_delta is None:
                    field_delta = (
                        full_response
                        * s_a[archetype]
                        * _within_team_field_sd(roster, f)
                        / (1 - m / 40)
                    )
                else:
                    field_delta = flat_delta
                lone[archetype][f] = _edited_state(roster, s_a, talents, idx, (f,), field_delta)
        out.append(RosterFixtures(base=base, target_id=target_id, f1=f1, f2=f2, lone=lone))
    return out


def _rank_at_cell(
    state: RosterState,
    *,
    beta: float,
    z_floor: float,
    z_cap: float,
    floor_rank: int | None = None,
) -> dict[object, tuple[int, float, bool]]:
    """`role_service.construct_team_roles`'s ranking rule on a precomputed
    state: `(rank, bonus, fired)` per player, `fired` being `z > z_floor`
    (identical to `bonus > 0` whenever `beta > 0`; kept separate so a
    `beta = 0` cell -- the pure rank-floor mechanism -- still fires).
    `floor_rank` is RC-37's rank-floor variant: after the additive ranking,
    the top `floor_rank - 1` are untouched and everyone at rank
    `floor_rank` or below is reordered firers-first, each block in score
    order. So a firer already at `floor_rank` keeps it and the firers below
    him stack at `floor_rank + 1, ...`; with a non-firer at `floor_rank`,
    the best buried firer takes it. With `beta = 0, floor_rank = 8` this is
    RC-37.3 D15, the mechanism RC-37 specifies."""
    scored = []
    for pid, talent, z, _label in state:
        bonus = beta * max(0.0, min(z - z_floor, z_cap))
        scored.append((pid, talent + bonus, bonus, z > z_floor))
    scored.sort(key=lambda row: (-row[1], row[0]))
    order = [pid for pid, _score, _bonus, _fired in scored]
    by_id = {pid: (bonus, fired) for pid, _score, bonus, fired in scored}
    if floor_rank is not None:
        head, tail = order[: floor_rank - 1], order[floor_rank - 1 :]
        lifted = [pid for pid in tail if by_id[pid][1]]
        rest = [pid for pid in tail if not by_id[pid][1]]
        order = head + lifted + rest
    return {pid: (rank, *by_id[pid]) for rank, pid in enumerate(order, 1)}


def _talent_rank(state: RosterState) -> dict[object, int]:
    ordered = sorted(state, key=lambda row: (-row[1], row[0]))
    return {pid: rank for rank, (pid, _t, _z, _l) in enumerate(ordered, 1)}


def rank_gap_profile(pre: list[RosterFixtures]) -> dict[int, dict[str, float]]:
    """RC-31 T2 (the promotion budget) on whichever population `pre` was
    built from: `talent(rank k) - talent(rank 11)` for k = 5..10, by
    unmodified `compute_overall`. Never measured on production before RC-37
    -- RC-30's bar and RC-19.6's `beta * z_cap <= 6.0` were both set from
    the synthetic profile."""
    gaps: dict[int, list[float]] = {k: [] for k in range(5, 11)}
    for rf in pre:
        ranks = _talent_rank(rf.base)
        by_rank = {ranks[pid]: talent for pid, talent, _z, _l in rf.base}
        for k in gaps:
            gaps[k].append(by_rank[k] - by_rank[11])
    out: dict[int, dict[str, float]] = {}
    for k, values in gaps.items():
        ordered = sorted(values)
        out[k] = {
            "mean": statistics.mean(values),
            "sd": statistics.pstdev(values),
            "p10": _percentile(ordered, 10),
            "p50": _percentile(ordered, 50),
            "p90": _percentile(ordered, 90),
        }
    return out


def f1_decomposition(
    pre: list[RosterFixtures], *, beta: float, z_floor: float, z_cap: float
) -> dict[str, dict[str, float]]:
    """What the F1 target actually receives at a cell, per archetype:
    the talent gain from `overall_cached` recompute, the arch_z reached,
    the saturation rate, and `need` -- the score the target must beat to
    hold rank 9 (the 9th-highest score among the other 13 on the F1 roster,
    at this cell) minus the target's own F1 talent. `unreachable` is the
    share of rosters where `need >= beta * z_cap`, i.e. where no bonus the
    cap allows could promote him -- criterion 1's ceiling under criterion 6."""
    out: dict[str, dict[str, float]] = {}
    for archetype in ARCHETYPES:
        d_talent: list[float] = []
        z_f1: list[float] = []
        needs: list[float] = []
        saturated = 0
        for rf in pre:
            base_talent = next(t for pid, t, _z, _l in rf.base if pid == rf.target_id)
            state = rf.f1[archetype].state
            _pid, talent, z, _label = next(row for row in state if row[0] == rf.target_id)
            d_talent.append(talent - base_talent)
            z_f1.append(z)
            if z >= z_floor + z_cap:
                saturated += 1
            others = [
                t + beta * max(0.0, min(zz - z_floor, z_cap))
                for pid, t, zz, _l in state
                if pid != rf.target_id
            ]
            others.sort(reverse=True)
            needs.append(others[8] - talent)
        n = len(pre)
        ordered = sorted(needs)
        out[archetype] = {
            "mean_delta_talent": statistics.mean(d_talent),
            "mean_z_f1": statistics.mean(z_f1),
            "p10_z_f1": _percentile(sorted(z_f1), 10),
            "saturation_rate": saturated / n,
            "need_p50": _percentile(ordered, 50),
            "need_p90": _percentile(ordered, 90),
            "need_p95": _percentile(ordered, 95),
            "unreachable_at_cap": sum(1 for v in needs if v >= beta * z_cap) / n,
        }
    return out


def score_cell(
    pre: list[RosterFixtures],
    null_z: Sequence[float],
    *,
    beta: float,
    z_floor: float,
    z_cap: float,
    floor_rank: int | None = None,
) -> dict[str, object]:
    """Every criterion RC-35.5/RC-36.10 scores, at one cell, from the
    precomputed states. `null_z` is the N1-null `arch_z` sample the null
    firing rate is read from (a function of `z_floor` alone).

    Operational figures are on the unmodified population: firing rate,
    specialists-per-roster, label concentration, rank-1 occupant change
    rate vs. the talent-only ranking, and the minutes-weighted talent delta
    `sum_p talent_p * (a(rank_with_bonus) - a(rank_talent_only)) / 240` per
    roster (`a` = `sim.game._minutes_curve_raw`), reported as mean and
    max-|.| over rosters. Per-archetype figures are the F1/F2/lone fixtures
    on the rank-11 target: reach 9/8/5 clip-included and clip-excluded, F1
    saturation, mean bonuses, and 4b'-prime's `C_full`/`C_lone`/`kappa`."""
    # Imported here, not at module top: `dba2.sim.game` is the import the
    # module docstring explains this read-only spike deliberately avoids
    # depending on globally; only the tie-break statistics need it.
    from dba2.sim.game import _minutes_curve_raw

    n = len(pre)
    op_fires = 0
    labels: Counter[str] = Counter()
    per_roster_specialists: list[int] = []
    rank1_changed = 0
    mwt_deltas: list[float] = []
    lifted_per_roster: list[int] = []
    firers_top8 = 0
    for rf in pre:
        with_bonus = _rank_at_cell(
            rf.base, beta=beta, z_floor=z_floor, z_cap=z_cap, floor_rank=floor_rank
        )
        talent_only = _talent_rank(rf.base)
        fired = [pid for pid, (_rank, _bonus, hit) in with_bonus.items() if hit]
        op_fires += len(fired)
        per_roster_specialists.append(len(fired))
        label_by_id = {pid: label for pid, _t, _z, label in rf.base}
        labels.update(label_by_id[pid] for pid in fired)
        lifted_per_roster.append(
            sum(1 for pid in fired if talent_only[pid] >= 10 and with_bonus[pid][0] <= 9)
        )
        firers_top8 += sum(1 for pid in fired if talent_only[pid] <= 8)
        top_bonus = min(with_bonus, key=lambda pid: with_bonus[pid][0])
        top_talent = min(talent_only, key=lambda pid: talent_only[pid])
        if top_bonus != top_talent:
            rank1_changed += 1
        mwt_deltas.append(
            sum(
                t * (_minutes_curve_raw(with_bonus[pid][0]) - _minutes_curve_raw(talent_only[pid]))
                for pid, t, _z, _l in rf.base
            )
            / 240.0
        )
    n_players = sum(len(rf.base) for rf in pre)
    total_labels = sum(labels.values())

    per_archetype: dict[str, dict[str, object]] = {}
    for archetype, fields in ARCHETYPES.items():
        m = len(fields)
        reach = {k: 0 for k in (9, 8, 5)}
        reach_excl = {k: 0 for k in (9, 8, 5)}
        f1_clipped = 0
        f1_bonus_sum = f2_bonus_sum = 0.0
        f1_saturated = 0
        unedited_fires = 0
        non_firing_n = 0
        full_fires = 0
        lone_fires: dict[str, int] = dict.fromkeys(fields, 0)
        for rf in pre:
            base_ranks = _rank_at_cell(
                rf.base, beta=beta, z_floor=z_floor, z_cap=z_cap, floor_rank=floor_rank
            )
            fires_unedited = base_ranks[rf.target_id][2]
            unedited_fires += fires_unedited
            fx = rf.f1[archetype]
            f1_ranks = _rank_at_cell(
                fx.state, beta=beta, z_floor=z_floor, z_cap=z_cap, floor_rank=floor_rank
            )
            f1_rank, f1_bonus, f1_fired = f1_ranks[rf.target_id]
            f1_bonus_sum += f1_bonus
            if f1_bonus >= beta * z_cap - 1e-12:
                f1_saturated += 1
            f1_clipped += fx.clipped
            for k in reach:
                if f1_rank <= k:
                    reach[k] += 1
                    if not fx.clipped:
                        reach_excl[k] += 1
            f2_ranks = _rank_at_cell(
                rf.f2[archetype].state,
                beta=beta,
                z_floor=z_floor,
                z_cap=z_cap,
                floor_rank=floor_rank,
            )
            f2_bonus_sum += f2_ranks[rf.target_id][1]
            if fires_unedited:
                continue
            non_firing_n += 1
            full_fires += f1_fired
            for f in fields:
                lone_ranks = _rank_at_cell(
                    rf.lone[archetype][f].state,
                    beta=beta,
                    z_floor=z_floor,
                    z_cap=z_cap,
                    floor_rank=floor_rank,
                )
                lone_fires[f] += lone_ranks[rf.target_id][2]
        c_full = full_fires / non_firing_n if non_firing_n else float("nan")
        c_lone = {
            f: (lone_fires[f] / non_firing_n if non_firing_n else float("nan")) for f in fields
        }
        excl_n = n - f1_clipped
        per_archetype[archetype] = {
            "m": m,
            "reach9": reach[9] / n,
            "reach8": reach[8] / n,
            "reach5": reach[5] / n,
            "reach9_excl": reach_excl[9] / excl_n if excl_n else float("nan"),
            "reach8_excl": reach_excl[8] / excl_n if excl_n else float("nan"),
            "reach5_excl": reach_excl[5] / excl_n if excl_n else float("nan"),
            "f1_clip_rate": f1_clipped / n,
            "f1_saturation": f1_saturated / n,
            "f1_mean_bonus": f1_bonus_sum / n,
            "f2_mean_bonus": f2_bonus_sum / n,
            "unedited_fires": unedited_fires / n,
            "c_full": c_full,
            "c_lone": c_lone,
            "kappa": (max(c_lone.values()) / c_full) if c_full else float("nan"),
            "kappa_bar": 1.0 / m,
        }
    return {
        "beta": beta,
        "z_floor": z_floor,
        "z_cap": z_cap,
        "beta_x_cap": beta * z_cap,
        "floor_rank": floor_rank,
        "null_fire": firing_rate(list(null_z), z_floor),
        "op_fire": op_fires / n_players,
        "rosters_le3_specialists": sum(1 for c in per_roster_specialists if c <= 3) / n,
        "max_label_share": (max(labels.values()) / total_labels) if total_labels else 0.0,
        "label_shares": {k: v / total_labels for k, v in labels.items()} if total_labels else {},
        "rank1_change": rank1_changed / n,
        "lifted_per_roster": statistics.mean(lifted_per_roster),
        "rosters_with_lift": sum(1 for c in lifted_per_roster if c > 0) / n,
        "firers_already_top8": (firers_top8 / op_fires) if op_fires else 0.0,
        "mwt_delta_mean": statistics.mean(mwt_deltas),
        "mwt_delta_max_abs": max(abs(v) for v in mwt_deltas),
        "per_archetype": per_archetype,
    }


def _production_roster_index(seasons: tuple[int, ...]) -> list[tuple[int, str]]:
    """`(season, team_code)` per roster, in `production_rosters`' own order
    (same loop, same exclusions), so a precomputed roster can be named."""
    codes = _team_codes()
    keys: list[tuple[int, str]] = []
    for season in seasons:
        rows = json.loads((_REAL_PLAYERS_DIR / f"{season}.json").read_text(encoding="utf-8"))[
            "players"
        ]
        placed = _real_players_on_their_real_teams(
            rows, team_codes=codes, per_team=_PLAYERS_PER_TEAM_PRODUCTION
        )
        keys.extend((season, code) for code, team_rows in placed.items() if team_rows is not None)
    return keys


def _real_player_names(seasons: tuple[int, ...]) -> dict[tuple[int, object], str]:
    out: dict[tuple[int, object], str] = {}
    for season in seasons:
        rows = json.loads((_REAL_PLAYERS_DIR / f"{season}.json").read_text(encoding="utf-8"))[
            "players"
        ]
        for r in rows:
            out[(season, r["external_ref"])] = f"{r['first_name']} {r['last_name']}"
    return out


def lifted_players(
    pre: list[RosterFixtures],
    keys: Sequence[tuple[int, str]],
    names: Mapping[tuple[int, object], str],
    *,
    z_floor: float,
    floor_rank: int,
) -> list[str]:
    """Every real player the pure rank-floor mechanism (beta = 0, at
    `floor_rank`) moves from talent rank >= 10 to final rank <= 9 -- the
    "deep-bench lift" RC-37 counts, whatever slot he lands in (8 or 9 at
    floor 8) -- one line each, for the report's
    face-validity read -- the equivalent of RC-31 T5's top-10 lists, but
    for the players the mechanism would actually act on."""
    out: list[str] = []
    for rf, (season, code) in zip(pre, keys, strict=True):
        talent_only = _talent_rank(rf.base)
        final = _rank_at_cell(rf.base, beta=0.0, z_floor=z_floor, z_cap=1.5, floor_rank=floor_rank)
        for pid, talent, z, label in rf.base:
            if talent_only[pid] >= 10 and final[pid][0] <= 9:
                out.append(
                    f"{season} {code:>3} {names.get((season, pid), str(pid)):<24} "
                    f"{label:<19} z={z:.2f} ovr={talent} talent-rank={talent_only[pid]}"
                    f" -> {final[pid][0]}"
                )
    return out


_RC37_BETAS: tuple[float, ...] = (3.0, 4.0, 5.0, 6.0, 8.0)
_RC37_Z_FLOORS: tuple[float, ...] = (1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0)
_RC37_Z_CAPS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)


def _print_cell_row(cell: Mapping[str, object]) -> None:
    per = cell["per_archetype"]
    assert isinstance(per, dict)
    r9 = [row["reach9_excl"] for row in per.values()]
    r8 = [row["reach8_excl"] for row in per.values()]
    r5 = [row["reach5_excl"] for row in per.values()]
    kappa_fail = sum(1 for row in per.values() if row["m"] >= 3 and row["kappa"] > row["kappa_bar"])
    floor = cell["floor_rank"]
    print(
        f"  b={cell['beta']:>3.1f} zf={cell['z_floor']:.4f} zc={cell['z_cap']:.2f} "
        f"bxc={cell['beta_x_cap']:>4.1f} floor={floor if floor else '-':>2} | "
        f"null={cell['null_fire'] * 100:5.2f}% op={cell['op_fire'] * 100:5.2f}% "
        f"le3={cell['rosters_le3_specialists'] * 100:5.1f}% "
        f"lbl={cell['max_label_share'] * 100:4.1f}% "
        f"r1chg={cell['rank1_change'] * 100:4.2f}% mwt={cell['mwt_delta_mean']:+.4f} "
        f"lift/r={cell['lifted_per_roster']:.3f} top8={cell['firers_already_top8'] * 100:4.1f}% | "
        f"r9 min={min(r9) * 100:5.1f} mean={statistics.mean(r9) * 100:5.1f} "
        f"n>=90={sum(1 for v in r9 if v >= 0.90)} | r8 min={min(r8) * 100:5.1f} "
        f"r5 max={max(r5) * 100:4.1f} | kfail={kappa_fail}"
    )


def run_rc37(
    *, seasons: tuple[int, ...] = _PRODUCTION_SEASONS, json_path: str | None = None
) -> None:
    """RC-37's measurement pass. Reads `role_service.py` only via
    `ast.literal_eval` (never imports it); writes nothing unless
    `json_path` is given, and then only that one scratch file."""
    rosters, shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, _per_roster = roster_baseline_variance(rosters)
    s_a = null_s_a(rho_bar)
    sigma_field_p = sigma_field(rosters)
    print(
        f"production population: {len(rosters)} rosters, {sum(len(r) for r in rosters)} players "
        f"({len(shortfalls)} franchise-seasons excluded); rho_bar={rho_bar:.6f}; "
        f"S_A m=2/3/4 = {s_a['rebounder']:.4f}/{s_a['rim_protector']:.4f}/"
        f"{s_a['perimeter_defender']:.4f}; sigma_field={sigma_field_p:.4f}\n"
    )
    null_z = null_arch_z_distribution(rosters, s_a)
    null_summary = distribution_summary(null_z)
    print("N1 null arch_z: " + "  ".join(f"{k}={v:.4f}" for k, v in null_summary.items()))
    real_z = arch_z_distribution(collect_u_values(rosters), s_a)
    real_summary = distribution_summary(real_z)
    print("real arch_z:    " + "  ".join(f"{k}={v:.4f}" for k, v in real_summary.items()) + "\n")

    pre_d6 = precompute_fixtures(rosters, s_a, sigma_field_p)
    pre_flat25 = precompute_fixtures(rosters, s_a, sigma_field_p, flat_delta=25.0)

    print("=== RC-37 T1 -- production rank-gap profile (talent = compute_overall) ===")
    gaps = rank_gap_profile(pre_d6)
    for k, row in gaps.items():
        print(
            f"  rank {k:>2} - rank 11: mean={row['mean']:.3f} sd={row['sd']:.3f} "
            f"p10={row['p10']:.1f} p50={row['p50']:.1f} p90={row['p90']:.1f}"
        )
    bound = 1.25 * gaps[8]["mean"]
    print(f"  RC-19.6's rule re-derived: 1.25 x mean(rank8-rank11) = {bound:.2f}\n")

    d13_floor = null_summary["p95"]
    for label, pre in (("D6 (5.17 response)", pre_d6), ("flat +25", pre_flat25)):
        print(f"=== RC-37 T2 -- F1 decomposition at (4.0, {d13_floor:.4f}, 1.5), {label} ===")
        for archetype, row in f1_decomposition(
            pre, beta=4.0, z_floor=d13_floor, z_cap=1.5
        ).items():
            print(
                f"  {archetype:>20}: d_talent={row['mean_delta_talent']:.2f} "
                f"z_f1 mean={row['mean_z_f1']:.2f} p10={row['p10_z_f1']:.2f} "
                f"sat={row['saturation_rate'] * 100:5.1f}% need p50/p90/p95="
                f"{row['need_p50']:.1f}/{row['need_p90']:.1f}/{row['need_p95']:.1f} "
                f"unreachable@6.0={row['unreachable_at_cap'] * 100:4.1f}%"
            )
        print()

    print("=== RC-37 T3 -- reference cells (D6 fixture) ===")
    reference = [
        (4.0, d13_floor, 1.5, None),
        (6.0, 3.30, 1.0, None),
        (4.0, 2.2, 1.5, None),
        (4.0, d13_floor, 1.5, 9),
    ]
    all_cells: list[dict[str, object]] = []
    for beta, zf, zc, floor in reference:
        cell = score_cell(pre_d6, null_z, beta=beta, z_floor=zf, z_cap=zc, floor_rank=floor)
        cell["fixture"] = "d6"
        all_cells.append(cell)
        _print_cell_row(cell)
        per = cell["per_archetype"]
        assert isinstance(per, dict)
        print(
            "      reach9 excl: "
            + "  ".join(f"{a[:6]}={row['reach9_excl'] * 100:.1f}" for a, row in per.items())
        )
        print(
            "      kappa:       "
            + "  ".join(f"{a[:6]}={row['kappa']:.3f}" for a, row in per.items())
        )
    print()

    print("=== RC-37 T4 -- the sweep, D6 fixture, additive bonus ===")
    for zf in _RC37_Z_FLOORS + (d13_floor,):
        for beta in _RC37_BETAS:
            for zc in _RC37_Z_CAPS:
                cell = score_cell(pre_d6, null_z, beta=beta, z_floor=zf, z_cap=zc)
                cell["fixture"] = "d6"
                all_cells.append(cell)
                _print_cell_row(cell)
    print()

    print("=== RC-37 T5 -- the sweep, flat +25 fixture, additive bonus (beta*z_cap = 6 only) ===")
    for zf in _RC37_Z_FLOORS + (d13_floor,):
        for beta, zc in ((4.0, 1.5), (6.0, 1.0), (3.0, 2.0)):
            cell = score_cell(pre_flat25, null_z, beta=beta, z_floor=zf, z_cap=zc)
            cell["fixture"] = "flat25"
            all_cells.append(cell)
            _print_cell_row(cell)
    print()

    print("=== RC-37 T6 -- rank-floor variants, D6 fixture (beta=0 is the pure floor) ===")
    for floor in (9, 8):
        for zf in (2.2, 2.4, d13_floor, 2.8, 3.0):
            for beta in (0.0, 4.0):
                cell = score_cell(
                    pre_d6, null_z, beta=beta, z_floor=zf, z_cap=1.5, floor_rank=floor
                )
                cell["fixture"] = "d6"
                all_cells.append(cell)
                _print_cell_row(cell)
                if beta == 0.0:
                    per = cell["per_archetype"]
                    assert isinstance(per, dict)
                    print(
                        "      reach9 excl: "
                        + "  ".join(
                            f"{a[:6]}={row['reach9_excl'] * 100:.1f}" for a, row in per.items()
                        )
                        + "   c_full: "
                        + "  ".join(f"{row['c_full']:.3f}" for row in per.values())
                    )
    print()

    print("=== RC-37 T6b -- pure floor (beta=0, floor 9) with the flat +25 fixture ===")
    for zf in (2.4, d13_floor, 2.8, 3.0):
        cell = score_cell(pre_flat25, null_z, beta=0.0, z_floor=zf, z_cap=1.5, floor_rank=9)
        cell["fixture"] = "flat25"
        all_cells.append(cell)
        _print_cell_row(cell)
    print()

    print(
        "=== RC-37 T8 -- face validity: real players the pure floor (floor 9) lifts "
        "from talent rank >= 10 to rank <= 9 ==="
    )
    keys = _production_roster_index(seasons)
    names = _real_player_names(seasons)
    for zf in (d13_floor, 3.0):
        rows = lifted_players(pre_d6, keys, names, z_floor=zf, floor_rank=9)
        print(f"  z_floor={zf:.4f}: {len(rows)} players lifted over {len(pre_d6)} rosters")
        for row in rows[:40]:
            print("    " + row)
    print()

    print("=== RC-37 T7 -- synthetic portability (criterion 9's firing half), same table ===")
    synth_z = arch_z_distribution(collect_u_values(synthetic_rosters()), s_a)
    for zf in (2.2, 2.4, d13_floor, 2.8, 3.0):
        print(
            f"  z_floor={zf:.4f}: synthetic firing={firing_rate(synth_z, zf) * 100:.2f}%  "
            f"null={firing_rate(null_z, zf) * 100:.2f}%  real={firing_rate(real_z, zf) * 100:.2f}%"
        )
    print()

    if json_path is not None:
        payload = {
            "rho_bar": rho_bar,
            "s_a": s_a,
            "sigma_field": sigma_field_p,
            "null_summary": null_summary,
            "real_summary": real_summary,
            "rank_gaps": gaps,
            "cells": all_cells,
        }
        Path(json_path).write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        print(f"wrote {json_path}")


def run_rc37_floor(
    *, seasons: tuple[int, ...] = _PRODUCTION_SEASONS, floor_rank: int = 9
) -> None:
    """RC-37's candidate mechanism -- the pure rank floor (`beta = 0`,
    `floor_rank` from `--rc37-floor-rank`, default 9; RC-37 ships 8) --
    scored at exact quantiles of the N1 null, since
    D13's rule (and forbidden fudge 6) says `z_floor` is a published null
    quantile and nothing else. Per quantile: the two criterion-1 halves
    (does the F1 target fire; is a firing target lifted), the F2 firing
    gradient, kappa, criterion 7's three clauses, the lift rate, and the
    synthetic firing rate."""
    rosters, _shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, _per_roster = roster_baseline_variance(rosters)
    s_a = null_s_a(rho_bar)
    sigma_field_p = sigma_field(rosters)
    null_z = sorted(null_arch_z_distribution(rosters, s_a))
    synth_z = arch_z_distribution(collect_u_values(synthetic_rosters()), s_a)
    pre = precompute_fixtures(rosters, s_a, sigma_field_p)
    pre25 = precompute_fixtures(rosters, s_a, sigma_field_p, flat_delta=25.0)
    keys = _production_roster_index(seasons)
    names = _real_player_names(seasons)
    print(f"rho_bar={rho_bar:.6f}  n_null={len(null_z)}  floor_rank={floor_rank}")
    for q in (95.0, 96.0, 97.0, 97.5, 98.0, 99.0):
        zf = _percentile(null_z, q)
        cell = score_cell(pre, null_z, beta=0.0, z_floor=zf, z_cap=1.5, floor_rank=floor_rank)
        cell25 = score_cell(
            pre25, null_z, beta=0.0, z_floor=zf, z_cap=1.5, floor_rank=floor_rank
        )
        per = cell["per_archetype"]
        per25 = cell25["per_archetype"]
        assert isinstance(per, dict) and isinstance(per25, dict)
        print(f"\n--- null p{q:g}: z_floor={zf:.4f} ---")
        print(
            f"  null={cell['null_fire'] * 100:.2f}%  op={cell['op_fire'] * 100:.2f}%  "
            f"synthetic={firing_rate(synth_z, zf) * 100:.2f}%  "
            f"<=3/roster={cell['rosters_le3_specialists'] * 100:.1f}%  "
            f"max label={cell['max_label_share'] * 100:.1f}%  "
            f"lift/roster={cell['lifted_per_roster']:.3f}  "
            f"rosters w/ lift={cell['rosters_with_lift'] * 100:.1f}%  "
            f"firers already top-8={cell['firers_already_top8'] * 100:.1f}%  "
            f"rank1chg={cell['rank1_change'] * 100:.2f}%  mwt={cell['mwt_delta_mean']:+.4f}"
        )
        print(
            f"  {'archetype':>20}  {'D6:reach9':>9} {'F1fires':>8} {'F2fires':>8} "
            f"{'kappa':>6} {'bar':>5} | {'+25:reach9':>10} {'F1fires':>8} {'kappa':>6}"
        )
        for name in ARCHETYPES:
            r, r25 = per[name], per25[name]
            print(
                f"  {name:>20}  {r['reach9_excl'] * 100:8.1f}% {r['c_full'] * 100:7.1f}% "
                f"{_f2_fire(pre, name, zf) * 100:7.1f}% {r['kappa']:6.3f} {r['kappa_bar']:5.3f} | "
                f"{r25['reach9_excl'] * 100:9.1f}% {r25['c_full'] * 100:7.1f}% {r25['kappa']:6.3f}"
            )
        r8 = [row["reach8_excl"] for row in per.values()]
        print(f"  reach8 (D6) min={min(r8) * 100:.1f}% mean={statistics.mean(r8) * 100:.1f}%")
        if q in (95.0, 98.0):
            rows = lifted_players(pre, keys, names, z_floor=zf, floor_rank=floor_rank)
            print(f"  lifted players at this floor: {len(rows)} (first 60)")
            for row in rows[:60]:
                print("    " + row)


def run_rc37_a3(*, seasons: tuple[int, ...] = _PRODUCTION_SEASONS) -> None:
    """RC-36.7 D12's decision inputs for `perimeter_defender`, measured:
    `r_A3^obs` for the shipped 4-field cluster, for A3-i (drop
    `screen_navigation`), and for A3-ii (drop `foul_discipline` too), plus
    the residual correlation matrix among the four fields (each field's
    `y - ybar`, i.e. tilt, across all production players) -- the number
    D12's A3-ii row says must be *measured, never inferred*."""
    rosters, _shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, _per_roster = roster_baseline_variance(rosters)
    y_rows, ybar_rows = _collect_y_and_ybar(rosters)
    a3 = list(ARCHETYPES["perimeter_defender"])
    variants = {
        "A3 as shipped": a3,
        "A3-i (drop screen_navigation)": [f for f in a3 if f != "screen_navigation"],
        "A3-ii (drop screen_navigation + foul_discipline)": [
            f for f in a3 if f not in ("screen_navigation", "foul_discipline")
        ],
    }
    print(f"rho_bar={rho_bar:.6f}  players={len(y_rows)}")
    for label, fields in variants.items():
        m = len(fields)
        u = [sum(y[f] for f in fields) / m - yb for y, yb in zip(y_rows, ybar_rows, strict=True)]
        s_obs = statistics.pstdev(u)
        s_null = ((1.0 / m - 1.0 / 40.0) * (1.0 - rho_bar)) ** 0.5
        print(f"  {label:<48} m={m} s_obs={s_obs:.4f} S_null={s_null:.4f} r={s_obs / s_null:.3f}")
    print("  residual (tilt) correlation matrix among A3's fields:")
    tilt = {f: [y[f] - yb for y, yb in zip(y_rows, ybar_rows, strict=True)] for f in a3}
    print("    " + " ".join(f"{f[:14]:>14}" for f in a3))
    for f in a3:
        row = [statistics.correlation(tilt[f], tilt[g]) for g in a3]
        print(f"    {f[:14]:>14} " + " ".join(f"{v:14.3f}" for v in row))
    for name, fields in ARCHETYPES.items():
        m = len(fields)
        u = [sum(y[f] for f in fields) / m - yb for y, yb in zip(y_rows, ybar_rows, strict=True)]
        s_null = ((1.0 / m - 1.0 / 40.0) * (1.0 - rho_bar)) ** 0.5
        print(f"  control {name:>20}: r_A={statistics.pstdev(u) / s_null:.3f}")


_RC37_NULL_SEEDS: tuple[int, ...] = (4, 5, 6, 7, 8)


def pooled_null_arch_z(
    rosters: list[TeamRoster], s_a: Mapping[str, float], *, seeds: Sequence[int] = _RC37_NULL_SEEDS
) -> list[float]:
    """The N1 null pooled over several independent per-player shuffles --
    the sample RC-37's pinned `z_floor` is a quantile of. One shuffle per
    player (rc36's protocol) leaves the p98 quantile with a seed spread of
    ~0.10 (RC-37 report, seed table); five shuffles is 35,070 draws."""
    out: list[float] = []
    for seed in seeds:
        out.extend(null_arch_z_distribution(rosters, s_a, rng_seed=seed))
    return sorted(out)


def run_rc37_pooled(*, seasons: tuple[int, ...] = _PRODUCTION_SEASONS) -> None:
    """The shipping constant's provenance: per-seed and pooled N1-null
    quantiles, then the rotation-floor mechanism (`beta = 0`, floor 8, and
    floor 9 for comparison) scored at the pooled p95 / p97.5 / p98 with
    both fixtures. Every headline number in RC-37's recommended cell comes
    from this command."""
    rosters, _shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, _per_roster = roster_baseline_variance(rosters)
    s_a = null_s_a(rho_bar)
    print(f"rho_bar={rho_bar:.6f}")
    print("seed   p95     p97.5   p98     p99")
    for seed in _RC37_NULL_SEEDS:
        z = sorted(null_arch_z_distribution(rosters, s_a, rng_seed=seed))
        print(
            f"{seed:>4}  {_percentile(z, 95):.4f}  {_percentile(z, 97.5):.4f}  "
            f"{_percentile(z, 98):.4f}  {_percentile(z, 99):.4f}"
        )
    pooled = pooled_null_arch_z(rosters, s_a)
    quantiles = {q: _percentile(pooled, q) for q in (95.0, 97.0, 97.5, 98.0, 99.0)}
    print(
        f"pooled n={len(pooled)}: "
        + "  ".join(f"p{q:g}={v:.4f}" for q, v in quantiles.items())
    )
    synth_z = arch_z_distribution(collect_u_values(synthetic_rosters()), s_a)
    sig = sigma_field(rosters)
    pre = precompute_fixtures(rosters, s_a, sig)
    pre25 = precompute_fixtures(rosters, s_a, sig, flat_delta=25.0)
    keys = _production_roster_index(seasons)
    names = _real_player_names(seasons)
    for q in (95.0, 97.5, 98.0):
        zf = quantiles[q]
        for floor in (8, 9):
            cell = score_cell(pre, pooled, beta=0.0, z_floor=zf, z_cap=1.5, floor_rank=floor)
            cell25 = score_cell(pre25, pooled, beta=0.0, z_floor=zf, z_cap=1.5, floor_rank=floor)
            per, per25 = cell["per_archetype"], cell25["per_archetype"]
            assert isinstance(per, dict) and isinstance(per25, dict)
            print(
                f"\n=== floor {floor} at pooled p{q:g}: z_floor={zf:.4f} ===\n"
                f"  null={cell['null_fire'] * 100:.2f}%  op={cell['op_fire'] * 100:.2f}%  "
                f"synthetic={firing_rate(synth_z, zf) * 100:.2f}%  "
                f"<=3/roster={cell['rosters_le3_specialists'] * 100:.1f}%  "
                f"max label={cell['max_label_share'] * 100:.1f}%  "
                f"lift/roster={cell['lifted_per_roster']:.3f}  "
                f"rosters w/ lift={cell['rosters_with_lift'] * 100:.1f}%  "
                f"firers already top-8={cell['firers_already_top8'] * 100:.1f}%  "
                f"rank1chg={cell['rank1_change'] * 100:.2f}%  "
                f"mwt={cell['mwt_delta_mean']:+.4f} (max |.| {cell['mwt_delta_max_abs']:.3f})"
            )
            shares = cell["label_shares"]
            assert isinstance(shares, dict)
            op = cell["op_fire"]
            assert isinstance(op, float)
            print(
                "  label share / share of all players: "
                + "  ".join(
                    f"{k[:6]}={v * 100:.1f}%/{v * op * 100:.2f}%"
                    for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                )
            )
            print(
                f"  {'archetype':>20} {'D6:r9':>6} {'r8':>5} {'r5':>4} {'F1fire':>6} {'F2fire':>6} "
                f"{'kappa':>5} {'bar':>5} | {'+25:r9':>6} {'r8':>5} {'F1fire':>6} {'kappa':>5}"
            )
            for name in ARCHETYPES:
                r, r2 = per[name], per25[name]
                print(
                    f"  {name:>20} {r['reach9_excl'] * 100:5.1f}% {r['reach8_excl'] * 100:4.1f}% "
                    f"{r['reach5_excl'] * 100:3.1f}% {r['c_full'] * 100:5.1f}% "
                    f"{_f2_fire(pre, name, zf) * 100:5.1f}% {r['kappa']:.3f} "
                    f"{r['kappa_bar']:.3f} | "
                    f"{r2['reach9_excl'] * 100:5.1f}% {r2['reach8_excl'] * 100:4.1f}% "
                    f"{r2['c_full'] * 100:5.1f}% {r2['kappa']:.3f}"
                )
            if floor == 8 and q == 97.5:
                rows = lifted_players(pre, keys, names, z_floor=zf, floor_rank=8)
                print(f"  lifted players at this cell: {len(rows)}")
                for row in rows:
                    print("    " + row)


_RC37_FLOOR_RANK = 8  # the floor every RC-37 recommended-cell number was scored at
_CHECK_RHO_TOLERANCE = 5e-7  # RHO_BAR is pinned to 6 decimals
_CHECK_Z_FLOOR_TOLERANCE = 5e-5  # Z_FLOOR is pinned to 4 decimals


def run_check(*, seasons: tuple[int, ...] = _PRODUCTION_SEASONS) -> int:
    """RC-35.4 D7 / RC-37.7 step 2: re-derive `role_service.py`'s three
    RC-37 pins from the production population and diff them against the
    module's literals (read via `ast`, never imported). Also re-scores the
    pinned cell, so a new real season that moved the population is caught
    even when the literals still match their own derivation. Returns the
    process exit code: 0 when everything holds, 1 otherwise."""
    failures: list[str] = []
    rosters, _shortfalls = production_rosters(seasons)
    rho_bar, _v_bar, _per_roster = roster_baseline_variance(rosters)
    s_a = null_s_a(rho_bar)
    pinned_s_a = null_s_a(ROLE_SERVICE_RHO_BAR)
    pooled = pooled_null_arch_z(rosters, s_a)
    p975 = _percentile(pooled, 97.5)

    print(f"RHO_BAR     derived={rho_bar:.6f}  role_service={ROLE_SERVICE_RHO_BAR:.6f}")
    if abs(rho_bar - ROLE_SERVICE_RHO_BAR) > _CHECK_RHO_TOLERANCE:
        failures.append("RHO_BAR")
    for name in ARCHETYPES:
        print(
            f"  S_A[{name:>18}] m={len(ARCHETYPES[name])}  derived={s_a[name]:.6f}  "
            f"from pinned RHO_BAR={pinned_s_a[name]:.6f}"
        )
    print(
        f"Z_FLOOR     derived={p975:.4f} (pooled N1 p97.5, n={len(pooled)})  "
        f"role_service={ROLE_SERVICE_Z_FLOOR:.4f}"
    )
    if abs(p975 - ROLE_SERVICE_Z_FLOOR) > _CHECK_Z_FLOOR_TOLERANCE:
        failures.append("Z_FLOOR")
    print(f"FLOOR_RANK  measured={_RC37_FLOOR_RANK}  role_service={ROLE_SERVICE_FLOOR_RANK}")
    if ROLE_SERVICE_FLOOR_RANK != _RC37_FLOOR_RANK:
        failures.append("FLOOR_RANK")

    pre = precompute_fixtures(rosters, pinned_s_a, sigma_field(rosters))
    cell = score_cell(
        pre,
        pooled,
        beta=0.0,
        z_floor=ROLE_SERVICE_Z_FLOOR,
        z_cap=1.5,
        floor_rank=ROLE_SERVICE_FLOOR_RANK,
    )
    per = cell["per_archetype"]
    assert isinstance(per, dict)
    reach9 = {name: row["reach9_excl"] for name, row in per.items()}
    le3 = cell["rosters_le3_specialists"]
    assert isinstance(le3, float)
    print(
        f"criterion 1  reach9 min={min(reach9.values()) * 100:.1f}% "
        f"({min(reach9, key=reach9.get)}), n>=90%: "
        f"{sum(1 for v in reach9.values() if v >= 0.90)}/7"
    )
    print(
        f"criterion 7b <=3 specialists/roster: {le3 * 100:.1f}%  "
        f"op firing={cell['op_fire'] * 100:.2f}%  null firing={cell['null_fire'] * 100:.2f}%"
    )
    if min(reach9.values()) < 0.90:
        failures.append("criterion 1")
    if le3 < 0.95:
        failures.append("criterion 7b")

    if failures:
        print(f"CHECK FAILED: {', '.join(failures)}")
        return 1
    print("CHECK OK: role_service.py's RC-37 pins match their derivation and the cell holds.")
    return 0


def _f2_fire(pre: list[RosterFixtures], archetype: str, z_floor: float) -> float:
    """Share of F2 (0.6x) targets whose post-edit arch_z clears `z_floor` --
    criterion 4a's gradient, in firing terms, for a mechanism with no bonus."""
    hits = 0
    for rf in pre:
        z = next(zz for pid, _t, zz, _l in rf.f2[archetype].state if pid == rf.target_id)
        hits += z > z_floor
    return hits / len(pre)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--leagues",
        type=int,
        default=_DEFAULT_SYNTHETIC_LEAGUE_COUNT,
        help="Number of synthetic leagues to generate (default 10, RC-31's own protocol).",
    )
    parser.add_argument(
        "--reproduce",
        action="store_true",
        help="RC-35.6 P1/P2: reproduce the shipped table's and the all-synthetic "
        "table's arch_z null distributions and firing rates. Blocking check, run "
        "before any redesign work.",
    )
    parser.add_argument(
        "--rc36-p1",
        action="store_true",
        help="RC-36.12 build-plan step 1: rho_bar/V_bar on the production population, "
        "P2's identity check, and the N1/N2 r_A table -- run first and alone, per "
        "RC-36.9's decision rule.",
    )
    parser.add_argument(
        "--rc36-extended",
        action="store_true",
        help="RC-36.12 build-plan steps 2-4: P3's Option B control, N1 run by actual "
        "permutation, D13's z_floor quantile, criteria 5/7, criterion 4b', and the "
        "cross-population shape check. Does not touch role_service.py.",
    )
    parser.add_argument(
        "--rc37",
        action="store_true",
        help="RC-37: production rank-gap profile, F1 decomposition, and the full "
        "(beta, z_floor, z_cap, fixture, rank-floor) sweep on the production "
        "population via the cell-independent precompute. Does not touch role_service.py.",
    )
    parser.add_argument(
        "--rc37-floor",
        action="store_true",
        help="RC-37: score the pure rank-floor mechanism at exact N1-null quantiles.",
    )
    parser.add_argument(
        "--rc37-pooled",
        action="store_true",
        help="RC-37: seed spread and pooled N1-null quantiles, and the rotation-floor "
        "mechanism scored at the pooled p95/p97.5/p98 -- the recommended cell's provenance.",
    )
    parser.add_argument(
        "--rc37-a3",
        action="store_true",
        help="RC-37: RC-36.7 D12's perimeter_defender decision inputs, measured.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="RC-37.7 step 2: re-derive RHO_BAR, the S_A table and the pooled p97.5 "
        "Z_FLOOR, diff them against role_service.py's literals, re-score the pinned "
        "cell; exit non-zero on any mismatch or failed criterion.",
    )
    parser.add_argument(
        "--rc37-floor-rank",
        type=int,
        default=9,
        help="With --rc37-floor: the rank a firing deep-bench player is lifted to (default 9).",
    )
    parser.add_argument(
        "--rc37-json",
        default=None,
        help="With --rc37: also write every scored cell to this JSON path (scratch use).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.check:
        raise SystemExit(run_check())

    if args.rc37:
        run_rc37(json_path=args.rc37_json)
        return

    if args.rc37_floor:
        run_rc37_floor(floor_rank=args.rc37_floor_rank)
        return

    if args.rc37_a3:
        run_rc37_a3()
        return

    if args.rc37_pooled:
        run_rc37_pooled()
        return

    if args.reproduce:
        run_p1_p2(args.leagues)
        return

    if args.rc36_p1:
        run_rc36_p1()
        return

    if args.rc36_extended:
        run_rc36_extended()
        return

    synth_rosters = synthetic_rosters(args.leagues)
    synthetic_s_a, n_synthetic = measure_s_a(synth_rosters)

    real_pooled_rosters = real_rosters(_REAL_SEASONS)
    real_s_a, n_real = measure_s_a(real_pooled_rosters)

    for season in _REAL_SEASONS:
        season_rosters = _load_real_rosters(season)
        s_a, n = measure_s_a(season_rosters)
        print(f"-- season {season}: {n} players, {len(season_rosters)} teams")
        for name in ARCHETYPES:
            print(f"     {name:>20}: s_A = {s_a[name]:.4f}")

    resolved, divergence = resolve_s_a(synthetic_s_a, real_s_a)

    print(
        f"\nSynthetic null: {n_synthetic} players, {len(synth_rosters)} teams "
        f"({args.leagues} leagues x {_TEAMS_PER_SYNTHETIC_LEAGUE} teams)"
    )
    print(
        f"Real pooled ({', '.join(str(s) for s in _REAL_SEASONS)}): "
        f"{n_real} players, {len(real_pooled_rosters)} teams\n"
    )

    header = (
        f"{'archetype':>20}  {'synthetic':>10}  {'real':>10}  {'diverge':>8}  "
        f"{'ships':>10}  {'retired':>10}  match"
    )
    print(header)
    print("-" * len(header))
    all_match = True
    for name in ARCHETYPES:
        ships = "real" if divergence[name] > _DIVERGENCE_THRESHOLD else "synthetic"
        repo_value = RETIRED_MIXED_S_A[name]
        match = abs(resolved[name] - repo_value) < 5e-3
        all_match = all_match and match
        print(
            f"{name:>20}  {synthetic_s_a[name]:>10.4f}  {real_s_a[name]:>10.4f}  "
            f"{divergence[name] * 100:>7.1f}%  {ships:>10}  {repo_value:>10.4f}  "
            f"{'OK' if match else 'MISMATCH'}"
        )

    print()
    if all_match:
        print("All 7 archetypes reproduce the retired mixed S_A table within tolerance.")
    else:
        print(
            "One or more archetypes did NOT reproduce the retired mixed S_A table -- see "
            "MISMATCH rows above. This is a real finding to report, not a bug to silently "
            "paper over."
        )


if __name__ == "__main__":
    main()
