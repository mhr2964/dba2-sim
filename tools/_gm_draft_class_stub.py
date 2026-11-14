"""Harness-only draft-class generator (Decision P2-32,
docs/design/phase-2-manager-minds.md §3, §12 step 9) -- the same
quarantine `tools/_gm_progression_stub.py` (Decision P2-23, step 10) now
also uses, copying this file's own quarantine pattern (underscore-
prefixed module in `tools/`, dedicated import-scan test) once that
sibling stub actually existed to copy from; established fresh here since
there was no existing sibling to copy from directly when this file was
first built.

**What this stub does, stated so its limits are not mistaken for
results.** For each of `class_size` (default 60) prospects: draws a
target peak overall from `managers.picks.peak_ovr` at that prospect's
draw-order slot (1..60) plus `N(0, 3.0)` rating points; draws an age
19-22; back-solves an entry talent scalar through `managers.scouting.
age_ramp` at that age against `PEAK_AGE_REF` (the same "no real player
yet to read a `peak_age_start` column from" fallback `picks.py` itself
uses for a generic pick, since this prospect's own `peak_age_start` has
not been drawn yet at this point in the pipeline); calls `sim.derivation.
synthetic_ratings(talent, position, rng)` for the 55 rating fields;
writes `status = RosterStatus.DRAFT_ELIGIBLE`, `team_id = None`, and
returns each prospect with no contract.

**Back-solve, exact formula, corrected (fix round -- see B-list bug this
replaces, below).** An interpretation call -- the design doc names only
"back-solves an entry overall through `age_ramp` at the drawn age," no
closed form -- and round 1's reading (`entry_talent = target_peak -
GROWTH_MAX * age_ramp(age, PEAK_AGE_REF)`) had a real, measured
calibration bug: `GROWTH_MAX * age_ramp(age, PEAK_AGE_REF)` tops out at
exactly `GROWTH_MAX = 12.0` rating points at age 19 and falls to `~6.86`
at age 22 (`age_ramp`'s own denominator, `PEAK_AGE_REF - RAMP_ORIGIN_AGE =
26 - 19 = 7`, shrinks the ramp linearly over the drawn 19-22 age range),
while `picks.py`'s own slot table implies an entry-to-peak gap of **9-14
points across slots 1-30** (`peak_ovr(slot) - entry_ovr(slot)`, §4.6's
anchor table) -- so a prospect drawn at the older end of the range had his
back-solved entry overall pulled to within `~6.86` points of his target
peak even at slot 1 (where the table wants 14), systematically
overpricing him relative to what `pick_value`'s own slot table assumes
for a "typical" occupant of that slot (measured: step 9's "top prospect
within 25% of `pick_value(slot 1)`" check came out 2.5x-4x instead of
~1x, root-caused by critic as this bug, independent of the draft-board
fixed-point defect step 9's other fix round addresses).

**The fix: target the same entry-to-peak gap `picks.py`'s own `entry_ovr`/
`peak_ovr` imply for the slot this prospect's target peak was drawn from,
rather than inverting purely through `age_ramp`.** `entry_talent = target_
peak - (peak_ovr(i) - entry_ovr(i))`, `i` the prospect's own 1..60
draw-order slot -- the gap is now always exactly the slot table's own
9-14-point figure, for every drawn age, closing the systematic-overshoot
defect at its root rather than re-tuning `GROWTH_MAX`/`age_ramp` (which
would also move `valuation.py`'s own §4.3.1 model, out of this harness
tool's scope). **`age`/`age_ramp` no longer participate in the back-solve**
-- `age` is still drawn (it seeds `birth_date` below) and still gives two
prospects targeting the same slot two different real ages, but the
entry-to-peak *gap itself* is now a pure function of draw-order slot, not
age, since the slot table (not `age_ramp`) is what step 9's own consistency
check compares against. This is a deliberate simplification, not an
oversight: nothing in §4.6/§4.3.1 requires the gap to vary by age within
the 19-22 draft window, and the alternative (blending `age_ramp` back in,
scaled so it still lands in `[9, 14]` at every age) was considered and
rejected as needless complexity for a harness-only tool with design
latitude explicitly granted (this dispatch's own task text).

**Two honest consequences, not results:**

1. **The prospect class is generated from the same slot table `pick_
   value` reads** (`peak_ovr`, from `managers.picks`), so any check
   comparing a board built from this class against `pick_value` is a
   *consistency* check between the board and the pick table (they are
   reading correlated data by construction), never a *calibration* check
   of whether the slot table itself is realistic. Nothing in Phase 2 can
   validate that; it needs Phase 7's real classes.
2. **Metric 17** (not built this step -- step 10's harness) is unaffected
   by this circularity, since it compares two philosophies' *orderings* of
   the *same* stub-generated class against each other, never against an
   external ground truth this stub could contaminate.

**Quarantine (Decision P2-32, "same quarantine, same import test" as the
not-yet-built P2-23 progression stub).** Lives in `tools/`, underscore-
prefixed, never imported by anything under `dba2/` -- enforced by
`tests/unit/test_layering.py::
test_nothing_under_dba2_imports_the_gm_draft_class_stub`. `tools/` is not
`managers/` and carries none of Decision P2-2(b)'s zero-I/O purity rules
-- a plain `random.Random(seed)` below is fine, no `rng_for` needed for a
harness tool with no persisted-seed contract to honor.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from random import Random

from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.ratings import PlayerRatings, RatingSource
from dba2.managers.picks import entry_ovr, peak_ovr
from dba2.managers.snapshot import RosterPlayer, build_roster_player
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning
from dba2.sim.derivation import synthetic_physicals, synthetic_ratings
from dba2.sim.ratings import compute_overall

_CLASS_SIZE_DEFAULT = 60
_PEAK_NOISE_SD = 3.0
_AGE_LO = 19
_AGE_HI = 22
_TALENT_CLAMP_LO = 25.0
_TALENT_CLAMP_HI = 99.0
_PEAK_AGE_START_LO = 24
_PEAK_AGE_START_HI = 28
_PEAK_AGE_SPAN_LO = 2
_PEAK_AGE_SPAN_HI = 6

_POSITIONS: tuple[Position, ...] = tuple(Position)

_CONFIDENCE_DEFAULT = Decimal("1.00")
"""`player_ratings.confidence`'s own schema default (migration 002) --
reused as-is rather than inventing a lower "no track record" figure the
design doc never specifies; `scout_sd`'s own widened `SCOUT_SD_PROSPECT`
anchor (§4.7) is what already carries "this is a prospect, not a
rostered veteran" through the pipeline, not `confidence`."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def generate_draft_class(
    *,
    season: int,
    seed: int,
    league_id: int = 1,
    class_size: int = _CLASS_SIZE_DEFAULT,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[RosterPlayer, ...]:
    """`class_size` (default 60) synthetic `draft_eligible` prospects,
    deterministic in `seed` alone. `player.id` is `1..class_size` -- a
    caller assembling a real league's draft class from more than one call
    of this function must re-key ids itself; this stub does not attempt
    global uniqueness across multiple classes.

    `tuning` is unread by the back-solve itself since the fix round above
    (the entry-to-peak gap now comes from `picks.py`'s own slot table, not
    `tuning.growth_max`/`tuning.peak_age_ref`) -- kept in the signature
    anyway for call-site stability (existing callers pass it explicitly)
    rather than breaking the contract for a parameter that may find a use
    again if this stub's calibration is revisited."""
    rng = Random(seed)
    generated_at = datetime(season, 6, 15, tzinfo=UTC)

    prospects: list[RosterPlayer] = []
    for i in range(1, class_size + 1):
        position = rng.choice(_POSITIONS)
        physicals = synthetic_physicals(position, rng)
        age = rng.randint(_AGE_LO, _AGE_HI)

        target_peak = peak_ovr(float(i)) + rng.gauss(0.0, _PEAK_NOISE_SD)
        slot_gap = peak_ovr(float(i)) - entry_ovr(float(i))
        entry_talent = _clamp(target_peak - slot_gap, _TALENT_CLAMP_LO, _TALENT_CLAMP_HI)

        ratings_55 = synthetic_ratings(entry_talent, position, rng)
        peak_age_start = rng.randint(_PEAK_AGE_START_LO, _PEAK_AGE_START_HI)
        peak_age_end = peak_age_start + rng.randint(_PEAK_AGE_SPAN_LO, _PEAK_AGE_SPAN_HI)
        full_ratings = {
            **ratings_55,
            "peak_age_start": peak_age_start,
            "peak_age_end": peak_age_end,
        }

        ratings = PlayerRatings(
            **full_ratings,
            player_id=i,
            league_id=league_id,
            source=RatingSource.GENERATED,
            derived_from_season=None,
            confidence=_CONFIDENCE_DEFAULT,
            updated_at=generated_at,
        )
        overall_cached = compute_overall(ratings, position)

        player = Player(
            id=i,
            league_id=league_id,
            first_name="Prospect",
            last_name=f"{i:03d}",
            external_ref=None,
            primary_position=position,
            secondary_position=None,
            height_in=physicals.height_in,
            weight_lb=physicals.weight_lb,
            wingspan_in=physicals.wingspan_in,
            birth_date=date(season - age, 6, 15),
            draft_year=season,
            draft_pick_number=None,
            years_pro=0,
            team_id=None,
            roster_status=RosterStatus.DRAFT_ELIGIBLE,
            overall_cached=overall_cached,
            overall_computed_at=generated_at,
            retired_in_season=None,
            created_at=generated_at,
            updated_at=generated_at,
        )
        prospects.append(build_roster_player(player, ratings, contract=None))

    return tuple(prospects)
