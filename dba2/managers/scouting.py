"""The seeded per-(team, player, season) potential estimate -- Gap C's
answer, the most novel thing in the phase (docs/design/phase-2-manager-
minds.md §4.3.1, §4.7, §12 step 2).

Without this module every team computes value from the same
`overall_cached` through the same formula, so every asset is valued
identically and no trade is ever mutually beneficial -- the trade market
deadlocks at zero volume (§1 Gap C, §14's "Trade deadlock" risk row).
This module is the disagreement mechanism: each team draws its own
seeded, noisy estimate of a player's *future* ceiling, so two teams
genuinely see two different players.

**Two round-1 bugs this module is built to not repeat (Gap E, §4.7):**

1. `growth_room = max(0, potential - overall)` reads the raw `potential`
   field as forward-looking. It is not one -- `sim/derivation.py` draws
   it centred on the player's *current* talent with no age term, so the
   residual is statistically identical for a 21-year-old and a
   34-year-old. `projected_peak` below derives growth room from age and
   `peak_age_start` instead, using `potential` only as a bounded
   per-player *residual* on top of that age shape (Decision P2-25).
2. `potential_variance` is not a rating-point spread -- it is an ordinary
   0-99 rating centred on talent, so reading it directly as a scouting SD
   produces noise of 24-48 rating points (a coin flip, not disagreement,
   and biased by the `[0, 99]` clamp to boot). `scout_sd` below reads it
   only as a normalized *volatility dial* around a small, sane SD anchor.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()` (the seeded draw goes through
`core.rng.rng_for` exclusively), no `open()`, no `os.environ`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dba2.core.rng import rng_for
from dba2.domain.player import RosterStatus
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# §4.3.1 -- the projected-peak model (Decision P2-25)
# ---------------------------------------------------------------------------


def age_ramp(
    age: float, peak_age_start: float, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """`clamp((peak_age_start - age) / max(peak_age_start - RAMP_ORIGIN_AGE,
    RAMP_MIN_SPAN), 0, 1)` (§4.3.1). 1.0 at full ramp (a fresh-19
    prospect against a typical `peak_age_start`), 0.0 at and past
    `peak_age_start` -- a player at or past his own peak has zero growth
    room by construction. The denominator guard (`RAMP_MIN_SPAN` floor)
    exists because `peak_age_start` is an unconstrained column: a
    hand-inserted or future-generated row at or below `RAMP_ORIGIN_AGE`
    would otherwise divide by zero or flip the ramp's sign."""
    span = max(peak_age_start - tuning.ramp_origin_age, tuning.ramp_min_span)
    return _clamp((peak_age_start - age) / span, 0.0, 1.0)


def growth_residual(
    potential: int, ovr: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """The bounded, normalized `potential - ovr` residual, in `[-1, +1]`
    (§4.3.1). Clamped at `RESIDUAL_CAP` (~1.5 sigma of the generator's own
    `potential` noise) before normalizing, so a 3-sigma draw cannot
    dominate `room_raw`."""
    raw = _clamp(potential - ovr, -tuning.residual_cap, tuning.residual_cap)
    return raw / tuning.residual_cap


def room_raw(
    age: float,
    peak_age_start: float,
    potential: int,
    ovr: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`GROWTH_MAX * age_ramp(age, peak_age_start) * (1 + RESIDUAL_GAIN *
    residual)` (§4.3.1). The residual only *modulates* the ramp by
    +/-50% (`RESIDUAL_GAIN = 0.50`) -- it never zeroes or flips it, which
    is what guarantees every player below `peak_age_start` has *some*
    growth room regardless of his residual draw (see `projected_peak`'s
    docstring for why this is the Gap E regression test's actual
    mechanism)."""
    residual = growth_residual(potential, ovr, tuning=tuning)
    ramp = age_ramp(age, peak_age_start, tuning=tuning)
    return tuning.growth_max * ramp * (1.0 + tuning.residual_gain * residual)


def projected_peak(
    ovr: int,
    age: float,
    peak_age_start: float,
    potential: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`ovr + room_raw(age, ...)` (§4.3.1, Decision P2-25) -- Phase 2's
    own belief about a player's future ceiling, derived from age and
    `peak_age_start` rather than read off the raw `potential` field.

    **The Gap E regression test lives on this function's own shape, not
    on a tuned constant:** `age_ramp` is exactly 0 at and above
    `peak_age_start`, so `room_raw` -- and therefore this function's
    output above `ovr` -- is exactly 0 *for every value of `residual`*,
    including the most negative one (`residual` only multiplies `room_raw`,
    it never adds to it independently of the ramp). A 34-year-old with any
    `peak_age_start` in the seeder's 24-28 range is at or past it, so his
    `projected_peak` equals his `ovr` exactly. Symmetrically, for any age
    below `peak_age_start` (`age_ramp > 0`), `1 + RESIDUAL_GAIN * residual`
    is bounded below by `1 - 0.50 = 0.50` (since `residual >= -1`), which
    is strictly positive -- so `room_raw`, and this function's output
    above `ovr`, is strictly positive for *every* residual value including
    the most negative. Round 1's model tied growth room to the raw
    residual directly (`max(0, potential - ovr)`), which is zero for
    roughly half the league regardless of age and identical in
    distribution for a rookie and a 34-year-old -- the exact defect this
    shape is built to not have.
    """
    return ovr + room_raw(age, peak_age_start, potential, ovr, tuning=tuning)


def player_age(birth_date: date, season: int) -> int:
    """Age derived from `players.birth_date` against the league's current
    season, never stored (phase-0-foundation.md §4.3.1, this doc's §4.3).
    Matches `tools/seed_dev_league.py`'s own convention exactly
    (`years_pro = current_season - birth_year - 19`): age is counted in
    whole seasons from birth *year*, not calendar days, since nothing in
    this schema anchors a season to a specific calendar date."""
    return season - birth_date.year


# ---------------------------------------------------------------------------
# §4.7 -- the scouting draw itself (Gap C)
# ---------------------------------------------------------------------------


def volatility(
    potential_variance: int, ovr: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """`clamp((potential_variance - ovr) / RESIDUAL_CAP, -1, +1)` (§4.7)
    -- reads the stored `potential_variance` rating only as a *normalized
    volatility dial*, never as a raw scouting SD (`potential_variance` is
    an ordinary 0-99 rating centred on the player's own talent, per §1
    item 8 and §4.7's round-1 postmortem, not a spread in rating points).
    Shares `RESIDUAL_CAP` with `growth_residual` deliberately (§4.7 states
    it as the same constant reused, not a second one declared)."""
    return _clamp((potential_variance - ovr) / tuning.residual_cap, -1.0, 1.0)


def scout_sd(
    potential_variance: int,
    ovr: int,
    *,
    draft_eligible: bool,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`SCOUT_SD_BASE * (1 + SCOUT_SD_GAIN * vol)` for a rostered player,
    `SCOUT_SD_PROSPECT * (...)` for a `draft_eligible` one (§4.7). Ranges:
    rostered `[1.5, 4.5]`, prospect `[2.5, 7.5]` -- enough that two teams
    meaningfully disagree about a young player's ceiling, small enough
    that neither is scouting a different person (contrast round 1's
    `potential_variance`-as-raw-SD reading, which produced 24-48 points
    of noise and was biased by the `[0, 99]` clamp on top of that)."""
    vol = volatility(potential_variance, ovr, tuning=tuning)
    base = tuning.scout_sd_prospect if draft_eligible else tuning.scout_sd_base
    return base * (1.0 + tuning.scout_sd_gain * vol)


@dataclass(frozen=True)
class ScoutedPeak:
    """One (team, player, season) scouting result -- §4.7's full output.
    `valuation.py` (step 3) reads `effective_peak` for §4.3.1's `room_t`
    term; the other fields are carried for explainability (Decision
    P2-18) and for this step's own verify, which is stated pre-clamp."""

    projected_peak: float
    """The true (unscouted) ceiling, shared by every team (§4.3.1)."""
    scout_sd: float
    """This player's scouting SD for this call -- `scouted_variance` in
    §6's `risk` formula (§4.7)."""
    pre_clamp_draw: float
    """`projected_peak + N(0, scout_sd)`, before the floor-at-`ovr` /
    ceiling-at-99 clamp. Exposed separately because the unbiasedness
    verify is explicitly stated on *this* quantity (§4.7): the floor is a
    deliberate one-sided commitment (`growth_room >= 0`), not a draw
    defect, so measuring bias after the clamp would measure the floor's
    effect instead of the draw's."""
    scouted_peak: float
    """`clamp(pre_clamp_draw, ovr, 99)` -- this team's seeded estimate of
    the player's ceiling (§4.7)."""
    effective_peak: float
    """`scouted_peak + (risk - 1.0) * scout_sd`, applied only below
    `peak_age_start` (§4.7, §6's `risk` definition). Equal to
    `scouted_peak` at and above `peak_age_start`, where `age_ramp == 0`
    and no amount of risk appetite changes a player with zero growth
    room -- the reason veteran disagreement rests entirely on the
    philosophy and window channels (§4.7's second load-bearing note)."""


def scout_player(
    *,
    league_rng_seed: int,
    team_id: int,
    player_id: int,
    season: int,
    ovr: int,
    age: float,
    peak_age_start: float,
    potential: int,
    potential_variance: int,
    draft_eligible: bool,
    risk: float,
    tuning: GmTuning = DEFAULT_TUNING,
) -> ScoutedPeak:
    """The whole §4.7 pipeline for one (team, player, season): compute
    `projected_peak`, this player's `scout_sd`, draw the seeded estimate
    from `rng_for(league_rng_seed, "scout", team_id, player_id, season)`,
    clamp it to `[ovr, 99]`, and apply `risk`'s tail adjustment below
    `peak_age_start`.

    Deterministic in `(league_rng_seed, team_id, player_id, season)`
    alone -- calling this twice with identical arguments always returns
    an identical `ScoutedPeak`, and two different `team_id`s draw
    independent streams (`rng_for`'s own guarantee, §12 step 2's first
    two verifies)."""
    peak = projected_peak(ovr, age, peak_age_start, potential, tuning=tuning)
    sd = scout_sd(potential_variance, ovr, draft_eligible=draft_eligible, tuning=tuning)

    rng = rng_for(league_rng_seed, "scout", team_id, player_id, season)
    pre_clamp_draw = peak + rng.gauss(0.0, sd)
    scouted = _clamp(pre_clamp_draw, float(ovr), 99.0)

    ramp = age_ramp(age, peak_age_start, tuning=tuning)
    effective = scouted + (risk - 1.0) * sd if ramp > 0.0 else scouted

    return ScoutedPeak(
        projected_peak=peak,
        scout_sd=sd,
        pre_clamp_draw=pre_clamp_draw,
        scouted_peak=scouted,
        effective_peak=effective,
    )


def scout_roster_player(
    roster_player: RosterPlayer,
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    risk: float,
    tuning: GmTuning = DEFAULT_TUNING,
) -> ScoutedPeak:
    """Convenience wrapper over `scout_player` for callers holding a
    `RosterPlayer` (step 1's `snapshot.py` join type) rather than the raw
    scalars -- extracts `ovr`/`potential`/`potential_variance`/
    `peak_age_start`/`draft_eligible` from `roster_player`, derives `age`
    from `player.birth_date` via `player_age`, and delegates. Not itself
    part of §4.7's formula; exists so `valuation.py` (step 3) and
    `services/gm_service.py` don't each re-derive this plumbing."""
    player = roster_player.player
    ratings = roster_player.ratings
    return scout_player(
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        player_id=player.id,
        season=season,
        ovr=player.overall_cached,
        age=player_age(player.birth_date, season),
        peak_age_start=ratings.peak_age_start,
        potential=ratings.potential,
        potential_variance=ratings.potential_variance,
        draft_eligible=player.roster_status is RosterStatus.DRAFT_ELIGIBLE,
        risk=risk,
        tuning=tuning,
    )
