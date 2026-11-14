"""`ensure_defaults_for_season` -- `team_strategy` season defaults, per
docs/design/phase-1-step-7-calibration.md §9 (Decision S7-12, Decision
P1-19 item 3).

The two enum fields (`offensive_scheme`/`defensive_scheme`) are an argmax
over five per-side scheme scores, each a weighted sum of *league-
standardized* team features (§9.3). The four numeric fields
(`pace_target`/`help_aggression`/`three_point_bias`/`crash_oreb_bias`) are
`50 + STRATEGY_NUMERIC_SPREAD * (a second league-standardization of a
raw roster-shape score)` (§9.4, rebuilt in revision 3 after a fixed-50
intercept measured 7-9 points out of band on three of four fields).

Both pipelines standardize across *all* teams in the league, not just the
teams this call is about to write a row for (§9.1 skips teams that already
have one) -- if the standardization set were the write set, a partial
re-run would compute different numbers than a full one and idempotence
would be a lie. §9.3/§9.4 both state this rule explicitly; it is the same
reason both call sites below build squads and standardized features from
every team in `team_repo.list_teams_for_league`, then only write the rows
for the subset missing one.

Constants here (scheme weights/priors, numeric-field weights,
`STRATEGY_NUMERIC_SPREAD`) are the §11.2 exception-in-location: they
describe team-level strategy assignment rather than player derivation and
nothing under `sim/` reads them, so they live here rather than in
`sim/derivation.py`, and they do not bump `engine_version`.
"""
from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import player_repo, ratings_repo, team_repo
from dba2.domain.player import Player
from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.domain.team import DefensiveScheme, OffensiveScheme
from dba2.sim.derivation import RATING_FIELD_ORDER

# §9.1 -- the evaluated squad is each team's top 8 by `overall_cached`; not
# a round number, it is exactly the set `game.py`'s 31/22/3.5 minutes tiers
# give non-trivial minutes to (S7-1b).
_TOP_N = 8

# §9.3/§9.4's shared degenerate-input rule: a feature's or score's
# across-team SD below this floor standardizes to 0.0 for every team
# rather than dividing by (near) zero.
_STANDARDIZATION_FLOOR = 1e-9

# §9.3/§9.4: fewer than 4 teams being evaluated skips standardization
# entirely and lets the priors decide -- deterministic, and it means a
# fixture-scale league gets the highest-prior scheme rather than an
# exception.
_MIN_TEAMS_TO_STANDARDIZE = 4

STRATEGY_NUMERIC_SPREAD = 10.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _z(rating_value: float) -> float:
    """§4's `z(f)` -- duplicated here rather than imported from
    `sim.derivation` (which does not export it), the same
    duplicate-rather-than-reach-into-another-module's-private-helper
    convention `sim.derivation` itself uses for `sim.probability`'s
    `_clamp`/`_weighted_choice` (see that module's own docstring)."""
    return _clamp((rating_value - 70.0) / 12.0, -2.5, 2.5)


def _talent_z(mean_of_55: float) -> float:
    """§4's `talent_z`, evaluated against the plain mean of the 55 rating
    fields (via the public `RATING_FIELD_ORDER`), not against the stored
    `overall_cached` -- same reasoning §4 states for `derive_tendencies`:
    step 12 item 9 will replace `compute_overall`'s body with a
    position-weighted formula, and this section's feature values must not
    silently change when that lands."""
    return _clamp((mean_of_55 - 70.0) / 8.0, -3.0, 3.0)


def _tz(tendency_value: float, base: float) -> float:
    """§4's `tz(t, base)`."""
    return _clamp((tendency_value - base) / 15.0, -2.5, 2.5)


def _mean_of_55(ratings: PlayerRatings) -> float:
    return sum(getattr(ratings, f) for f in RATING_FIELD_ORDER) / len(RATING_FIELD_ORDER)


# ---------------------------------------------------------------------------
# §9.1 -- the evaluated squad
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Evaluee:
    player_id: int
    height_in: int
    mean_of_55: float
    ratings: PlayerRatings
    tendencies: PlayerTendencies


@dataclass(frozen=True)
class _Squad:
    top8: tuple[_Evaluee, ...]
    tall2: tuple[_Evaluee, ...]
    short3: tuple[_Evaluee, ...]
    best: _Evaluee


def _build_squads(
    team_ids: Sequence[int],
    players: Sequence[Player],
    ratings_by_player: Mapping[int, PlayerRatings],
    tendencies_by_player: Mapping[int, PlayerTendencies],
) -> dict[int, _Squad]:
    by_team: dict[int, list[Player]] = {}
    for p in players:
        if p.team_id is not None:
            by_team.setdefault(p.team_id, []).append(p)

    squads: dict[int, _Squad] = {}
    for team_id in team_ids:
        roster = by_team.get(team_id, [])
        if not roster:
            raise NotFoundError(
                f"team {team_id} has no players to derive strategy defaults from"
            )
        roster_sorted = sorted(roster, key=lambda p: (-p.overall_cached, p.id))
        top_n = tuple(roster_sorted[: min(_TOP_N, len(roster_sorted))])

        evaluees: list[_Evaluee] = []
        for p in top_n:
            ratings = ratings_by_player.get(p.id)
            tendencies = tendencies_by_player.get(p.id)
            if ratings is None or tendencies is None:
                raise NotFoundError(
                    f"player {p.id} on team {team_id} is missing a player_ratings or "
                    "player_tendencies row -- ensure_defaults_for_season requires "
                    "recompute_overall_for_league and derive_tendencies_for_league to "
                    "have already run for this league"
                )
            evaluees.append(
                _Evaluee(
                    player_id=p.id,
                    height_in=p.height_in,
                    mean_of_55=_mean_of_55(ratings),
                    ratings=ratings,
                    tendencies=tendencies,
                )
            )

        tall2 = tuple(
            sorted(evaluees, key=lambda e: (-e.height_in, e.player_id))[: min(2, len(evaluees))]
        )
        short3 = tuple(
            sorted(evaluees, key=lambda e: (e.height_in, e.player_id))[: min(3, len(evaluees))]
        )
        squads[team_id] = _Squad(
            top8=tuple(evaluees), tall2=tall2, short3=short3, best=evaluees[0]
        )
    return squads


def _mean(values: object) -> float:
    values = list(values)  # type: ignore[arg-type]
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# §9.3's 17 features + §9.4's 7 new ones -- 24 total, one function each.
# ---------------------------------------------------------------------------

_FEATURES: Mapping[str, Callable[[_Squad], float]] = {
    "screen_big": lambda s: _mean(_z(e.ratings.screen_setting) for e in s.tall2),
    "handle_guards": lambda s: _mean(_z(e.ratings.handle) for e in s.short3),
    "star_talent": lambda s: _talent_z(s.best.mean_of_55),
    "star_handle": lambda s: _z(s.best.ratings.handle),
    "motion": lambda s: _mean(
        _mean(
            (
                _z(e.ratings.off_ball_movement),
                _z(e.ratings.cutting),
                _z(e.ratings.relocation),
            )
        )
        for e in s.top8
    ),
    "post_max": lambda s: max(_z(e.ratings.post_scoring) for e in s.top8),
    "strength_bigs": lambda s: _mean(_z(e.ratings.strength) for e in s.tall2),
    "three_mean": lambda s: _mean(_z(e.ratings.catch_shoot_3) for e in s.top8),
    "three_floor": lambda s: min(_z(e.ratings.catch_shoot_3) for e in s.tall2),
    "rim_anchor": lambda s: max(_z(e.ratings.rim_protection) for e in s.top8),
    "switch_mean": lambda s: _mean(_z(e.ratings.switchability) for e in s.top8),
    "switch_floor": lambda s: min(_z(e.ratings.switchability) for e in s.tall2),
    "discipline": lambda s: _mean(_z(e.ratings.scheme_discipline) for e in s.top8),
    "rotation_speed": lambda s: _mean(_z(e.ratings.rotation_speed) for e in s.top8),
    "lateral": lambda s: _mean(_z(e.ratings.lateral_quickness) for e in s.top8),
    "on_ball_def": lambda s: _mean(_z(e.ratings.on_ball_defense) for e in s.top8),
    "help_iq": lambda s: _mean(_z(e.ratings.help_iq) for e in s.top8),
    # §9.4's 7 new features.
    "speed_mean": lambda s: _mean(_z(e.ratings.speed) for e in s.top8),
    "transition_mean": lambda s: _mean(_tz(e.tendencies.transition_freq, 30.0) for e in s.top8),
    "stamina_mean": lambda s: _mean(_z(e.ratings.stamina) for e in s.top8),
    "help_gamble_mean": lambda s: _mean(
        _tz(e.tendencies.help_gamble_freq, 35.0) for e in s.top8
    ),
    "spot_up_mean": lambda s: _mean(_tz(e.tendencies.spot_up_freq, 45.0) for e in s.top8),
    "crash_mean": lambda s: _mean(_tz(e.tendencies.crash_oreb_freq, 30.0) for e in s.top8),
    "oreb_mean": lambda s: _mean(_z(e.ratings.offensive_rebounding) for e in s.top8),
}
assert len(_FEATURES) == 24

# §9.4's anti-degeneracy assertion (§12 step 5, corrected per critic round
# 15's B15-1) names exactly these 10 -- the features the four numeric raw
# scores actually read, as opposed to `R[k]` itself (already a sum of
# unit-SD standardized inputs, so it can never itself read as degenerate).
NUMERIC_UNDERLYING_FEATURES: tuple[str, ...] = (
    "speed_mean",
    "transition_mean",
    "stamina_mean",
    "help_gamble_mean",
    "help_iq",
    "discipline",
    "three_mean",
    "spot_up_mean",
    "crash_mean",
    "oreb_mean",
)
assert set(NUMERIC_UNDERLYING_FEATURES) <= set(_FEATURES)
assert len(NUMERIC_UNDERLYING_FEATURES) == 10


def _standardize(raw: Mapping[int, float], team_ids: Sequence[int]) -> dict[int, float]:
    """§9.3 step 2 / step 5's shared standardization primitive: `(raw -
    mean) / SD`, population SD across the teams being evaluated. Below
    `_MIN_TEAMS_TO_STANDARDIZE` teams, or an across-team SD under
    `_STANDARDIZATION_FLOOR`, every team standardizes to exactly 0.0."""
    if len(team_ids) < _MIN_TEAMS_TO_STANDARDIZE:
        return dict.fromkeys(team_ids, 0.0)
    values = [raw[t] for t in team_ids]
    mean = statistics.mean(values)
    sd = statistics.pstdev(values)
    if sd < _STANDARDIZATION_FLOOR:
        return dict.fromkeys(team_ids, 0.0)
    return {t: (raw[t] - mean) / sd for t in team_ids}


def raw_features(
    squads: Mapping[int, _Squad], team_ids: Sequence[int]
) -> dict[str, dict[int, float]]:
    """Public for §12 step 5's anti-degeneracy assertion (each of
    `NUMERIC_UNDERLYING_FEATURES`' raw, pre-standardization across-team SD
    must exceed 0.05) -- the test needs the raw values, not just the
    standardized ones `ensure_defaults_for_season` writes."""
    return {name: {tid: fn(squads[tid]) for tid in team_ids} for name, fn in _FEATURES.items()}


def _standardized_features(
    squads: Mapping[int, _Squad], team_ids: Sequence[int]
) -> dict[str, dict[int, float]]:
    raw = raw_features(squads, team_ids)
    return {name: _standardize(values, team_ids) for name, values in raw.items()}


# ---------------------------------------------------------------------------
# §9.3 -- scheme scores
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SchemeSpec:
    weights: Mapping[str, float]
    prior: float


_OFFENSE_SCHEMES: Mapping[OffensiveScheme, _SchemeSpec] = {
    OffensiveScheme.PNR_HEAVY: _SchemeSpec(
        {"screen_big": 0.60, "handle_guards": 0.40}, 0.40
    ),
    OffensiveScheme.ISO_STAR: _SchemeSpec(
        {"star_talent": 0.60, "star_handle": 0.40}, -0.10
    ),
    OffensiveScheme.MOTION: _SchemeSpec({"motion": 1.00}, 0.10),
    OffensiveScheme.POST_UP: _SchemeSpec(
        {"post_max": 0.65, "strength_bigs": 0.35}, -0.45
    ),
    OffensiveScheme.FIVE_OUT: _SchemeSpec(
        {"three_mean": 0.50, "three_floor": 0.50}, 0.05
    ),
}

_DEFENSE_SCHEMES: Mapping[DefensiveScheme, _SchemeSpec] = {
    DefensiveScheme.DROP: _SchemeSpec({"rim_anchor": 1.00}, 0.45),
    DefensiveScheme.SWITCH_ALL: _SchemeSpec(
        {"switch_mean": 0.50, "switch_floor": 0.50}, 0.15
    ),
    DefensiveScheme.HEDGE: _SchemeSpec(
        {"discipline": 0.60, "rotation_speed": 0.40}, 0.00
    ),
    DefensiveScheme.ICE: _SchemeSpec({"lateral": 0.60, "on_ball_def": 0.40}, -0.10),
    DefensiveScheme.ZONE_MIX: _SchemeSpec(
        {"help_iq": -0.60, "lateral": -0.40}, -0.50
    ),
}

for _spec_map in (_OFFENSE_SCHEMES, _DEFENSE_SCHEMES):
    for _scheme, _spec in _spec_map.items():
        _total_abs = sum(abs(w) for w in _spec.weights.values())
        assert abs(_total_abs - 1.0) < 1e-9, (
            f"{_scheme} weights do not sum to 1.0 in absolute value: {_total_abs}"
        )


def _scheme_scores(
    scheme_specs: Mapping[Enum, _SchemeSpec],
    standardized: Mapping[str, Mapping[int, float]],
    team_ids: Sequence[int],
) -> dict[Enum, dict[int, float]]:
    """§9.3 steps 3-6, minus the final argmax (kept as a separate function
    below so a test can assert on scores directly)."""
    raw = {
        scheme: {
            tid: sum(w * standardized[feat][tid] for feat, w in spec.weights.items())
            for tid in team_ids
        }
        for scheme, spec in scheme_specs.items()
    }

    # Step 4: subtract each team's own mean across this side's raw scores.
    centered: dict[Enum, dict[int, float]] = {scheme: {} for scheme in scheme_specs}
    for tid in team_ids:
        team_mean = statistics.mean(raw[scheme][tid] for scheme in scheme_specs)
        for scheme in scheme_specs:
            centered[scheme][tid] = raw[scheme][tid] - team_mean

    # Step 5: standardize each scheme's centered score across teams.
    standardized_scores = {
        scheme: _standardize(centered[scheme], team_ids) for scheme in scheme_specs
    }

    # Step 6: add the prior.
    return {
        scheme: {
            tid: standardized_scores[scheme][tid] + scheme_specs[scheme].prior
            for tid in team_ids
        }
        for scheme in scheme_specs
    }


def _argmax_scheme(
    scores_by_team: Mapping[Enum, Mapping[int, float]],
    enum_cls: type[Enum],
    team_ids: Sequence[int],
) -> dict[int, Enum]:
    """Step 6's argmax, ties broken by enum declaration order."""
    order = list(enum_cls)
    return {
        tid: max(order, key=lambda sch: (scores_by_team[sch][tid], -order.index(sch)))
        for tid in team_ids
    }


# ---------------------------------------------------------------------------
# §9.4 -- the four numeric fields
# ---------------------------------------------------------------------------

_NUMERIC_SPECS: Mapping[str, tuple[Mapping[str, float], tuple[int, int]]] = {
    "pace_target": (
        {"speed_mean": 0.45, "transition_mean": 0.35, "stamina_mean": 0.20},
        (20, 80),
    ),
    "help_aggression": (
        {"help_gamble_mean": 0.45, "help_iq": 0.30, "discipline": -0.25},
        (15, 85),
    ),
    "three_point_bias": (
        {"three_mean": 0.70, "spot_up_mean": 0.30},
        (15, 85),
    ),
    "crash_oreb_bias": (
        {"crash_mean": 0.70, "oreb_mean": 0.30},
        (10, 90),
    ),
}

for _name, (_weights, _band) in _NUMERIC_SPECS.items():
    _total_abs = sum(abs(w) for w in _weights.values())
    assert abs(_total_abs - 1.0) < 1e-9, (
        f"{_name} weights do not sum to 1.0 in absolute value: {_total_abs}"
    )


def _numeric_values(
    standardized: Mapping[str, Mapping[int, float]], team_ids: Sequence[int]
) -> dict[str, dict[int, int]]:
    raw = {
        name: {
            tid: sum(w * standardized[feat][tid] for feat, w in weights.items())
            for tid in team_ids
        }
        for name, (weights, _band) in _NUMERIC_SPECS.items()
    }
    result: dict[str, dict[int, int]] = {}
    for name, (_weights, (lo, hi)) in _NUMERIC_SPECS.items():
        s = _standardize(raw[name], team_ids)
        result[name] = {
            tid: int(_clamp(round(50.0 + STRATEGY_NUMERIC_SPREAD * s[tid]), lo, hi))
            for tid in team_ids
        }
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def ensure_defaults_for_season(ex: Executor, league_id: int, season: int) -> int:
    """§9.1: reads the existing row per team (30 reads at season start, not
    inside any bulk loader -- adding one would be the scope creep §8.2
    warns against) and skips teams that already have one. All writes
    inside one `transaction(ex)`, matching
    `ratings_service.recompute_overall_for_league`'s existing shape.

    Standardizes over *every* team in the league regardless of which need
    writing (§9.3/§9.4's idempotence requirement -- see module docstring),
    so a second call on a fully-populated league writes 0 rows and would
    compute the identical schemes and numeric values for every team if it
    did write. Returns the number of rows written.
    """
    async with transaction(ex) as conn:
        teams = await team_repo.list_teams_for_league(conn, league_id)
        if not teams:
            return 0

        existing_by_team = {
            team.id: await team_repo.get_team_strategy(conn, team.id, season) for team in teams
        }
        write_teams = [t for t in teams if existing_by_team[t.id] is None]
        if not write_teams:
            return 0

        players = await player_repo.list_players_for_league(conn, league_id)
        ratings_by_player = {
            r.player_id: r for r in await ratings_repo.get_for_league(conn, league_id)
        }
        tendencies_by_player = {
            t.player_id: t for t in await ratings_repo.get_tendencies_for_league(conn, league_id)
        }

        team_ids = [t.id for t in teams]
        squads = _build_squads(team_ids, players, ratings_by_player, tendencies_by_player)
        standardized = _standardized_features(squads, team_ids)

        offense_scores = _scheme_scores(_OFFENSE_SCHEMES, standardized, team_ids)
        defense_scores = _scheme_scores(_DEFENSE_SCHEMES, standardized, team_ids)
        offense_choice = _argmax_scheme(offense_scores, OffensiveScheme, team_ids)
        defense_choice = _argmax_scheme(defense_scores, DefensiveScheme, team_ids)
        numeric = _numeric_values(standardized, team_ids)

        for team in write_teams:
            await team_repo.upsert_team_strategy(
                conn,
                league_id=league_id,
                team_id=team.id,
                season=season,
                pace_target=numeric["pace_target"][team.id],
                offensive_scheme=offense_choice[team.id],
                defensive_scheme=defense_choice[team.id],
                help_aggression=numeric["help_aggression"][team.id],
                three_point_bias=numeric["three_point_bias"][team.id],
                crash_oreb_bias=numeric["crash_oreb_bias"][team.id],
                set_by=None,
            )

        return len(write_teams)
