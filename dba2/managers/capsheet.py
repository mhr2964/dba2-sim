"""Multi-season cap-sheet projection (docs/design/phase-2-manager-minds.md
§8, §12 step 7) -- the first step in Phase 2 with a database-touching
counterpart (`services/capsheet_service.py`), so per Decision P2-2(b) this
module itself stays zero I/O: it takes already-loaded per-`(team, season)`
totals (`CapSheetTeamSeasonInputs`, built by `capsheet_service.py` from its
bulk-loader reads) and a map of the league's *real* `LeagueCapSettings`
rows, and computes Decision P2-15's nine quantities plus Decision P2-16's
`projected` flag -- no `dba2.data` import, no `datetime.now()`, no bare
`random.Random()`, no `open()`, no `os.environ`, mirroring `managers.
snapshot`'s "the actual database reads are `services/`'s job" split
exactly.

**Decision P2-15**'s nine quantities per `(team, season)`:
`committed_salary`, `dead_cap`, `cap_holds` (always computed, only added
into `used` when `include_holds=True`, mirroring `cap_service.
get_cap_summary`'s `include_holds` contract exactly), `team_option_salary`
and `player_option_salary` (reported separately), `roster_count`,
`minimum_filler`, `used`, and `room`/`tax_room`/`apron1_room`/`apron2_room`.

**Judgment call on §8 item 8's "`used = committed + dead + option-weighted
terms + filler (+ holds if asked)`"**, flagged here for critic review since
the design doc's own text does not spell out what "option-weighted" means
numerically and no other section of the design doc defines a cap-dollar
option-exercise weight (the *only* option-exercise probability model in
this codebase, `valuation.py::player_option_q`/`team_option_multiplier`
[§4.4, Decision P2-26], produces a VP-valuation multiplier for `market_
value`'s contract-surplus term -- a different unit, a different formula,
and a different module's job entirely; reusing it here would smuggle a
*valuation* opinion into a *cap-accounting* number, which is exactly what
Decision P2-17's "two independently-written cap calculations" objection
warns against). `committed_salary` here is computed identically to
`contract_repo.get_committed_salary` (every `active` contract-year's salary
for that season, `option_type` included, unfiltered) specifically so a
season-0 row is byte-identical to `cap_service.get_cap_summary`'s own
`committed_salary` -- Decision P2-17's load-bearing test. Reading item 8's
phrase "option-weighted" as "already carried in `committed_salary` at full
(1.0) weight, not filtered out of it" is the only interpretation consistent
with that exact-match requirement, so `used` below adds no further term for
`team_option_salary`/`player_option_salary` -- both are reported purely as
an informational breakdown *of* `committed_salary`, per item 4/5's own
"reported separately... not summed together" text (which this reading
takes literally: not summed a *second* time, since they are already inside
the first sum).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

from dba2.domain.league import LeagueCapSettings
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning


@dataclass(frozen=True)
class CapSheetTeamSeasonInputs:
    """Everything one `(team, season)` row of the projection needs, already
    loaded by `capsheet_service.py` from its bulk-loader reads --
    `compute_team_season_cap_sheet`/`project_team_cap_sheet` read only this,
    never `dba2.data` (Decision P2-2(b))."""

    team_id: int
    season: int
    committed_salary: int
    """Σ `contract_years.salary` for active contracts covering `season` --
    identical to `contract_repo.get_committed_salary`'s own sum, including
    option-type years (see module docstring's judgment call)."""
    dead_cap: int
    cap_holds: int
    team_option_salary: int
    """Subset of `committed_salary` whose `contract_years.option_type` is
    `TEAM` -- informational, not summed into `used` a second time."""
    player_option_salary: int
    """Subset of `committed_salary` whose `contract_years.option_type` is
    `PLAYER` -- informational, not summed into `used` a second time."""
    roster_count: int
    """Distinct players with an active contract-year covering `season`."""


@dataclass(frozen=True)
class SeasonCapProjection:
    """One `(team, season)` row of the projection -- Decision P2-15's nine
    quantities plus Decision P2-16's `projected` flag. `include_holds`
    rides along for the same reason `cap_service.CapSummary` carries it:
    so a caller reading just this record can tell whether `used`/`room`
    include `cap_holds` without also holding onto the flag it was called
    with."""

    team_id: int
    season: int
    committed_salary: int
    dead_cap: int
    cap_holds: int
    team_option_salary: int
    player_option_salary: int
    roster_count: int
    minimum_filler: int
    used: int
    room: int
    tax_room: int
    apron1_room: int
    apron2_room: int
    include_holds: bool
    projected: bool
    """Decision P2-16: `True` when `season` had no real `league_cap_
    settings` row and this row's cap figures were grown from the latest
    real row at `GmTuning.cap_growth` per season instead."""


def project_cap_settings(
    latest_real: LeagueCapSettings, target_season: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> LeagueCapSettings:
    """Decision P2-16: grows every dollar-denominated field of `latest_real`
    by `tuning.cap_growth ** (target_season - latest_real.season)` for a
    `target_season` with no real row of its own. Percentage/count fields
    (`max_pct_*`, `supermax_pct`, `raise_pct_*`, `roster_min`, `roster_max`)
    are rule constants, not dollar figures, and are carried over unchanged
    -- cap growth does not change what fraction of the cap a max contract
    is worth or how many players a legal roster needs.

    `target_season` must be `>= latest_real.season`; this function does not
    itself enforce that (its one caller, `project_team_cap_sheet`, only
    ever calls it forward from the latest real row it found at or before
    the season being projected)."""
    seasons_out = target_season - latest_real.season
    growth = tuning.cap_growth**seasons_out
    return dataclasses.replace(
        latest_real,
        season=target_season,
        salary_cap=round(latest_real.salary_cap * growth),
        luxury_tax=round(latest_real.luxury_tax * growth),
        apron_1=round(latest_real.apron_1 * growth),
        apron_2=round(latest_real.apron_2 * growth),
        min_salary=round(latest_real.min_salary * growth),
        mle_amount=round(latest_real.mle_amount * growth),
        bi_annual_amount=round(latest_real.bi_annual_amount * growth),
    )


def compute_team_season_cap_sheet(
    inputs: CapSheetTeamSeasonInputs,
    cap_settings: LeagueCapSettings,
    *,
    projected: bool,
    include_holds: bool = False,
) -> SeasonCapProjection:
    """The single-season half of §8's formula (item 7's `minimum_filler`
    through item 9's four room figures) -- `project_team_cap_sheet` below
    is the horizon-level wrapper that decides, season by season, which
    `cap_settings` (real or `project_cap_settings`-grown) and `projected`
    value to pass in here.

    `minimum_filler = max(0, roster_min - roster_count) * min_salary`
    (item 7) -- the cost of filling out a legal roster, the reason this
    module exists as its own step per §8's own text."""
    minimum_filler = max(0, cap_settings.roster_min - inputs.roster_count) * cap_settings.min_salary
    used = (
        inputs.committed_salary
        + inputs.dead_cap
        + minimum_filler
        + (inputs.cap_holds if include_holds else 0)
    )
    return SeasonCapProjection(
        team_id=inputs.team_id,
        season=inputs.season,
        committed_salary=inputs.committed_salary,
        dead_cap=inputs.dead_cap,
        cap_holds=inputs.cap_holds,
        team_option_salary=inputs.team_option_salary,
        player_option_salary=inputs.player_option_salary,
        roster_count=inputs.roster_count,
        minimum_filler=minimum_filler,
        used=used,
        room=cap_settings.salary_cap - used,
        tax_room=cap_settings.luxury_tax - used,
        apron1_room=cap_settings.apron_1 - used,
        apron2_room=cap_settings.apron_2 - used,
        include_holds=include_holds,
        projected=projected,
    )


def _latest_real_settings_at_or_before(
    season: int, real_cap_settings_by_season: Mapping[int, LeagueCapSettings]
) -> LeagueCapSettings | None:
    candidates = [s for s in real_cap_settings_by_season if s <= season]
    if not candidates:
        return None
    return real_cap_settings_by_season[max(candidates)]


def project_team_cap_sheet(
    inputs: tuple[CapSheetTeamSeasonInputs, ...],
    real_cap_settings_by_season: Mapping[int, LeagueCapSettings],
    *,
    tuning: GmTuning = DEFAULT_TUNING,
    include_holds: bool = False,
) -> tuple[SeasonCapProjection, ...]:
    """One team's full projection, `inputs` order preserved (ascending
    season is `capsheet_service.py`'s job to hand in, matching Decision
    P2-15's "current + horizon-1" horizon). For each `inputs[i].season`:
    a real `league_cap_settings` row for that exact season is used as-is
    (`projected=False`); otherwise the latest real row *at or before* that
    season is grown forward via `project_cap_settings` (`projected=True`,
    Decision P2-16).

    Raises `ValueError` if `inputs` is empty or if no real row exists at or
    before `inputs[0].season` -- there is no meaningful cap figure to
    project forward *from* without at least one real anchor, the same
    "raise rather than fabricate" call `cap_service.get_cap_summary` makes
    with `NotFoundError` for the single-season case (this module raises a
    plain `ValueError`, not `NotFoundError`, since `managers/` does not
    import `dba2.core` -- mirroring `managers.snapshot`'s own plain-
    exception convention for a similarly "caller handed this function
    nonsense" case)."""
    if not inputs:
        raise ValueError("project_team_cap_sheet requires at least one season of inputs")
    first_season = inputs[0].season
    if _latest_real_settings_at_or_before(first_season, real_cap_settings_by_season) is None:
        raise ValueError(
            f"no real league_cap_settings row at or before season {first_season} to project from"
        )

    projections: list[SeasonCapProjection] = []
    for row in inputs:
        real = real_cap_settings_by_season.get(row.season)
        if real is not None:
            cap_settings = real
            projected = False
        else:
            latest_real = _latest_real_settings_at_or_before(
                row.season, real_cap_settings_by_season
            )
            assert latest_real is not None, (
                "unreachable: seasons ascend from a validated first_season, so every "
                "later season also has a real row at-or-before it"
            )
            cap_settings = project_cap_settings(latest_real, row.season, tuning=tuning)
            projected = True
        projections.append(
            compute_team_season_cap_sheet(
                row, cap_settings, projected=projected, include_holds=include_holds
            )
        )
    return tuple(projections)
