"""Draft-board pricing and the BPA-vs-need draft decision -- Decision
P2-27's second half, Decision P2-34 (docs/design/phase-2-manager-minds.md
§9.4, §12 step 9).

**Interpretation call -- `effective_ovr_t` here is deliberately NOT
`valuation.py::effective_ovr_t`, despite §9.4's formula using that exact
name.** This is the load-bearing reading of this whole module, so it is
proven rather than merely asserted:

`valuation.py::room_t` is `max(0, effective_peak - ovr) * age_ramp(age + t,
peak_age_start) / age_ramp(age, peak_age_start)` -- and `age_ramp` is
*non-increasing* in age (§4.3.1's own stated property), so `room_t` is
non-increasing in `t` whenever there is any room at all (`effective_peak >
ovr`), and identically zero for every `t` otherwise. Either way,
`valuation.py::effective_ovr_t = ovr + upside_weight_eff * room_t` is
**non-increasing in `t`** as a matter of the formula's own shape.

`picks.py::e_ovr_y(slot, y) = entry(slot) + (peak(slot) - entry(slot)) *
y / (PEAK_AGE_REF - 20)` is **strictly increasing in `y`** whenever
`peak(slot) > entry(slot)`, which is true at every real slot (§4.6's own
anchor table).

A non-increasing sequence and a strictly-increasing sequence cannot agree
at more than one point unless both are constant -- so §9.4's own required
equality ("a prospect at implied slot 31 ... equals `pick_value(31)` when
his scouted projection equals the slot table's own `E_ovr_y`", asserted
term-by-term across a 2-or-4-season horizon at every philosophy) is
**algebraically impossible** if `effective_ovr_t` here reuses `valuation.
py`'s own function. So it does not: `_prospect_talent_at` below is
`picks.py::e_ovr_y`'s own linear entry-to-peak *shape*, applied to the
named prospect's own scouted `(ovr, effective_peak)` pair instead of the
slot table's population-average `(entry(slot), peak(slot))` pair -- which
is exactly §9.4's own text, read literally: "they are the same function
with the expected player swapped for a named one." Flagged here for
critic scrutiny, same discipline step 8's flagged interpretation calls
used, because it overrides what the design doc's prose would suggest at a
skim (reusing the identically-named `valuation.py` function) in favor of
what the design doc's own required *test* proves it must mean.

**`ovr`/`effective_peak` are `float` here, not `int`** (departing from
`market_value`'s `ovr: int` convention) specifically so the equality
above can hold *exactly* at a non-anchor slot (e.g. 31, whose `entry_ovr`
interpolates to a non-integer, 57.8) and not just at the eight anchor
slots. `prospect_value_for` (the production wrapper) still sources `ovr`
from `effective_talent_ovr` (an `int`, a real player's `overall_cached`)
and widens it to `float` at the call boundary -- production behavior is
unaffected; only the raw function's signature is wider than `market_
value`'s.

**Stated simplification (§9.4, sign corrected in the design doc's own
round 3):** `season_contribution_t = V_t` for every season, never
`softplus(V_t)` for a would-be team-option year 3/4. Real rookie deals
are guaranteed years 1-2 with team options in 3-4; the honest treatment
of years 3-4 is §4.4's `season_contribution`/`softplus`, not a flat
`V_t`. Phase 2 treats all four seasons as guaranteed, which **underprices**
a first-round prospect (a team option prices at or above a guaranteed
year under Decision P2-26's rebuilt option model) -- most in relative
terms at the back of the round, where `V_t` is small enough for the
option premium to be a meaningful share of it. Slots 31-60 are unaffected
(their two seasons carry no option in this model). This is exactly the
reason `season_contribution` (the option-branching function) is never
imported here -- using it would silently fix the simplification the
design doc explicitly asks to keep and name, not fix quietly.

**`build_draft_board` -- withdrawn fixed point, corrected rule (design doc
§9.4, corrected in commit `448e3b9` after step 9's critic round).** The
original text read `implied_slot` as a self-consistent fixed point: rank
on talent, assign slots, price every prospect at its assigned slot,
re-rank by the resulting price, done in one documented pass. **That claim
is false and is withdrawn.** `prospect_value(p, slot)` is *additively
separable*: `value(p, slot) = A(p) + C(slot)`, where `A` is the
prospect's own talent-and-market side (his scouted `(ovr, effective_
peak)` never changes with the slot he is hypothetically assigned) and
`C(slot) = -pick_pref * surplus_weight * VP_PER_CAP_SEASON * Sum_t
gamma^t * rookie_share_y(slot, t)` is identical for every prospect at a
given slot. `C` is *increasing* in slot (a later slot is a cheaper rookie
deal, so it raises anyone's surplus, by roughly 7.3 VP per adjacent slot
near the top of the round) where a prospect's own talent term is
typically *decreasing* in slot (worse-ranked prospects are worse talents).
So "re-rank each prospect by the price he got at his own rank's slot" is
the map "rank by talent, plus a bonus that grows with your current rank"
-- order-destabilizing wherever an adjacent talent gap is smaller than
that bonus, which is routine in a real class. Measured against a real
60-prospect class, the one-pass repricing moves ~50 of 60 prospects and
then oscillates with period 2 rather than converging, at every one of the
28 (philosophy, window) pairs -- confirmed by cycle detection, not
assumed.

**The corrected rule, which needs no fixed point because it never
re-ranks.** A board's *order* and a board's *prices* are two different
questions, computed at two different slots:

- **Order** is `prospect_value` evaluated at a single, common
  `tuning.board_reference_slot` for every prospect on the board (`1` by
  default). Because `C` is prospect-independent, pricing every prospect at
  the same slot makes `C` a constant offset across the whole board, so the
  order is exactly the order of `A` alone -- the same board at every
  reference slot sharing a horizon. That invariance (identical order at
  reference slots 1, 15, and 30, `tests/unit/managers/decisions/
  test_draft.py`'s own mutation-tested verify) is the property that
  replaces the fixed point. It is not vacuous: it fails the moment
  anything couples a prospect's talent path to the slot he is
  hypothetically priced at -- which is exactly the coupling `_prospect_
  talent_at`'s proof above rules out (see next paragraph). Slot 1 is the
  default reference because it makes the top of the board directly
  comparable to `pick_value(1)`, §12 step 9's own 25%-consistency check.
- **Price** is `prospect_value` at the prospect's own `implied_slot` --
  his board rank, assigned by the order above -- reported per entry
  (`DraftBoardEntry.implied_slot_value`) and never fed back into the
  order. It answers "what is this prospect worth on the rookie deal he
  would sign where I rank him," and it is comparable to `pick_value
  (implied_slot)` **and to nothing else** -- two entries priced at two
  different slots are two different player-plus-contract bundles. A
  caller asking "what is prospect *p* worth to me at *my* pick" calls
  `prospect_value(p, my_slot)` directly rather than reading another
  entry's price column.

`DraftBoardEntry.rank_value` and `DraftBoardEntry.implied_slot_value` are
two distinct fields, deliberately not one `value` field with two possible
readings -- an ambiguous name was the whole hazard the withdrawn fixed
point created (a caller comparing one entry's price against another's,
which is meaningless once slots differ), so the rename removes the
temptation structurally rather than documenting around it.

**Why `_prospect_talent_at`'s own proof (above) is load-bearing here, not
just for the P2-34 equality.** That proof establishes `_prospect_talent_
at`'s formula reads a prospect's own scouted `(ovr, effective_peak)` pair
and never the slot he is hypothetically priced at -- exactly the
"`A(p)` does not depend on `slot`" half of the separability argument this
section's invariance property rests on. Had `_prospect_talent_at` instead
been `valuation.py::effective_ovr_t` (the rejected reading), a `t`/`age`
term would still be slot-independent, but a reader should not assume that
without the proof above -- which is why the proof is unchanged and its
conclusion is now cited by name in both places.

**BPA vs. need needs no new machinery (§9.4's own text).** It is exactly
`prospect_value_for` (BPA -- no team-need term, the `pick_value`-
equivalent axis) vs. `prospect_fit_value_for` (`prospect_value_for(...) *
(1 + clamp_need_bonus(need_bonus(...)))`, mirroring `trade.py::fit_value`'s
own wrap around `market_value` exactly), with `fit_sens` the one dial:
`star_maxer` (`fit_sens = 0.60`) drafts near-best-available; `egalitarian`
(`fit_sens = 1.30`) drafts for need. `build_draft_board` deliberately uses
`prospect_value_for` (BPA), never `prospect_fit_value_for`, for both order
and price -- the board-vs-pick-table guarantee is a `pick_value`
equivalence, and `pick_value` itself has no need term at all, so folding
fit into the board's own pricing would break the very equality this
module exists to preserve. `run_draft`'s actual on-the-clock selection is
where `fit_sens` gets to matter.

**`DecisionRecord`/`ReasonTerm` output (Decision P2-18, added this fix
round -- decisions/__init__.py's own docstring states every module in
`decisions/` emits one, unscoped to trade evaluation alone).**
`run_draft` emits one `DecisionRecord` per pick, attached to `DraftPick
Result.record`: `market_value` (the winning prospect's BPA `prospect_
value_for` at the real `pick_number`) and `need_bonus` (the same clamped
bonus `prospect_fit_value_for` folds in) as its two terms, summing to the
fit value that won the pick; `threshold` is the runner-up candidate's own
fit value (or the winner's own score if it was the only candidate left),
so a reader can see the pick's margin, not just its winner.
`build_draft_board`/`prospect_value_for` themselves stay pure valuation
functions with no record output -- a pre-draft board is not itself a
"decision" in Decision P2-18's sense (nothing is chosen yet), the same
reasoning `picks.py::pick_value` (a pure pricing function, not a
decision) is never wrapped in a record either.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()` (only `rng_for`'s
deterministic derivation, reached transitively via `scout_player`), no
`open()`, no `os.environ`.
"""
from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.league import LeagueCapSettings
from dba2.domain.player import RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import NeedMatrix, clamp_need_bonus, need_bonus
from dba2.managers.philosophy import PROFILES, PhilosophyProfile
from dba2.managers.picks import RookieScaleTable, pick_horizon, rookie_share_y
from dba2.managers.scouting import ScoutedPeak, player_age, scout_player
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import (
    DEFAULT_TUNING,
    WINDOW_PARAMS,
    CompetitiveWindow,
    GmTuning,
    WindowParams,
)
from dba2.managers.valuation import (
    age_mult_eff,
    base_value,
    effective_talent_ovr,
    market_salary_share,
)

# ---------------------------------------------------------------------------
# §9.4 -- the prospect's own scouted entry-to-peak progression
# ---------------------------------------------------------------------------


def _prospect_talent_at(
    ovr: float, effective_peak: float, t: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """`ovr + (effective_peak - ovr) * t / (PEAK_AGE_REF - 20)` -- `picks.
    py::e_ovr_y`'s own linear entry-to-peak shape, applied to a named
    prospect's own scouted `(ovr, effective_peak)` pair rather than a
    slot's population-average `(entry(slot), peak(slot))` pair. See
    module docstring for why this is required to be this shape, not
    `valuation.py::effective_ovr_t`, despite §9.4's own formula naming
    the latter."""
    return ovr + (effective_peak - ovr) * t / (tuning.peak_age_ref - 20.0)


# ---------------------------------------------------------------------------
# §9.4 -- prospect_value, the implied-rookie-contract price
# ---------------------------------------------------------------------------


def prospect_value(
    roster_player: RosterPlayer,
    ovr: float,
    scouted_peak: ScoutedPeak,
    profile: PhilosophyProfile,
    window: WindowParams,
    cap_settings: LeagueCapSettings,
    rookie_scale: RookieScaleTable,
    season: int,
    implied_slot: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§9.4's full formula: `pick_pref * Sum_{t=0..T-1} gamma^t * V_t`,
    `T = pick_horizon(implied_slot)` (Decision P2-27: `ROOKIE_HORIZON = 4`
    for `implied_slot <= 30`, `SECOND_ROUND_HORIZON = 2` for `implied_slot
    >= 31`, reused from `picks.py` rather than re-derived). `cap_hit_t` is
    `rookie_share_y(implied_slot, t, ...)` -- the exact same `picks.py`
    lookup `pick_value` itself reads, at the same `implied_slot`, which is
    the whole point of Decision P2-27's second half: a prospect and a pick
    at the same slot read the identical rookie-scale row. `season_
    contribution_t = V_t` for every season (the stated simplification --
    see module docstring), never `season_contribution`'s option-branching.

    `ovr`/`scouted_peak` are explicit parameters for the same reason `market_
    value`'s own docstring gives: whichever talent scalar the caller's
    `scouted_peak` was scouted against must be the same one selecting the
    talent curve here, and this function cannot verify that consistency on
    its own -- `prospect_value_for` below is the convenience wrapper that
    keeps the two in lockstep."""
    horizon = pick_horizon(implied_slot, tuning=tuning)
    birth_date = roster_player.player.birth_date
    age = float(player_age(birth_date, season))

    total = 0.0
    for t in range(horizon):
        talent_t = _prospect_talent_at(ovr, scouted_peak.effective_peak, t, tuning=tuning)
        a_mult = age_mult_eff(age + t, vet_bias=profile.vet_bias, age_exponent=window.age_exponent)
        talent = base_value(talent_t, profile.kappa) * a_mult

        cap_hit_share = rookie_share_y(implied_slot, t, rookie_scale, cap_settings, tuning=tuning)
        surplus_t = (
            market_salary_share(talent_t, cap_settings) - cap_hit_share
        ) * tuning.vp_per_cap_season
        v_t = talent + profile.surplus_weight * surplus_t
        total += (window.gamma**t) * v_t

    return profile.pick_pref * total


def prospect_value_for(
    roster_player: RosterPlayer,
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    rookie_scale: RookieScaleTable,
    implied_slot: int,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """Convenience wrapper over `prospect_value`, `effective_talent_ovr`,
    and `managers.scouting.scout_player`, mirroring `market_value_for`'s
    exact role over `market_value`: keeps `ovr` and `scouted_peak`
    consistent for a caller holding a `RosterPlayer`, a `(philosophy,
    window)` pair, and a scouting identity, rather than the three
    primitives `prospect_value` itself needs."""
    profile = PROFILES[philosophy]
    params = WINDOW_PARAMS[window]
    ovr = effective_talent_ovr(roster_player, profile)
    player = roster_player.player
    ratings = roster_player.ratings
    age = float(player_age(player.birth_date, season))

    scouted = scout_player(
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        player_id=player.id,
        season=season,
        ovr=ovr,
        age=age,
        peak_age_start=ratings.peak_age_start,
        potential=ratings.potential,
        potential_variance=ratings.potential_variance,
        draft_eligible=player.roster_status is RosterStatus.DRAFT_ELIGIBLE,
        risk=profile.risk,
        tuning=tuning,
    )
    return prospect_value(
        roster_player,
        float(ovr),
        scouted,
        profile,
        params,
        cap_settings,
        rookie_scale,
        season,
        implied_slot,
        tuning=tuning,
    )


def prospect_fit_value_for(
    roster_player: RosterPlayer,
    matrix: NeedMatrix,
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    rookie_scale: RookieScaleTable,
    implied_slot: int,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`prospect_value_for(...) * (1 + clamp_need_bonus(need_bonus(...)))`
    -- the BPA-vs-need axis, mirroring `trade.py::fit_value_for`'s own
    wrap around `market_value_for` exactly (§9.4: "it is exactly `market_
    value` vs. `fit_value`, and `fit_sens` is the dial"). No `chaos`
    jitter here -- `trade.py`'s own module docstring ties that jitter to
    a `RosterPlayer`'s `fit_value` specifically ("never to a pick's
    `pick_value`"), and a prospect is priced off an implied rookie
    contract exactly like a pick (Decision P2-34), so there is nothing of
    that kind for the jitter to multiply here either."""
    profile = PROFILES[philosophy]
    pv = prospect_value_for(
        roster_player,
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        season=season,
        philosophy=philosophy,
        window=window,
        cap_settings=cap_settings,
        rookie_scale=rookie_scale,
        implied_slot=implied_slot,
        tuning=tuning,
    )
    ovr = effective_talent_ovr(roster_player, profile)
    bonus = clamp_need_bonus(need_bonus(roster_player, ovr, matrix, profile, tuning=tuning))
    return pv * (1.0 + bonus)


# ---------------------------------------------------------------------------
# §9.4 -- a pre-draft board: order at one reference slot, price at each
# entry's own implied slot (see module docstring for the withdrawn fixed
# point this replaces)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftBoardEntry:
    """One prospect's place on one team's pre-draft board. See module
    docstring for the full reasoning; the comparability rule for each
    field, one sentence apiece:

    `prospect_id` -- the prospect being ranked.
    `implied_slot` -- this board's own rank (1 = best), not necessarily
    the real pick number a caller will draft at.
    `rank_value` -- `prospect_value` at `tuning.board_reference_slot`, the
    same slot for every entry on this board -- the sort key, and the only
    field here comparable across two different prospects.
    `implied_slot_value` -- `prospect_value` at this entry's own `implied_
    slot` -- comparable to `pick_value(implied_slot)` and to nothing else;
    never re-used as a sort key, and never comparable to another entry's
    `implied_slot_value` if the two entries' `implied_slot`s differ.
    """

    prospect_id: int
    implied_slot: int
    rank_value: float
    implied_slot_value: float


def build_draft_board(
    prospects: tuple[RosterPlayer, ...],
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    rookie_scale: RookieScaleTable,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[DraftBoardEntry, ...]:
    """One deterministic pass, no loop, no re-ranking (see module
    docstring for why the previously-claimed fixed point is withdrawn):
    scout every prospect once (`scout_player`'s `rng_for` salt carries no
    slot term, so one draw is bit-identical for both prices below --
    calling the raw `prospect_value` twice off that one draw, rather than
    `prospect_value_for` twice, avoids a second, redundant scouting call);
    price every prospect at the common `tuning.board_reference_slot` and
    sort descending on that price (`rank_value`), ties broken ascending by
    `rp.id`; assign `implied_slot = rank` (1-indexed) in that order; price
    each entry a second time at its own assigned `implied_slot`
    (`implied_slot_value`); return in `rank_value` order, unchanged by the
    second pricing pass. Raises `ValueError` if `prospects` contains a
    duplicate `id` -- ids double as this function's own dict keys and
    would silently collapse two prospects onto one slot otherwise."""
    if len({rp.id for rp in prospects}) != len(prospects):
        raise ValueError("build_draft_board requires prospects with distinct ids")

    profile = PROFILES[philosophy]
    params = WINDOW_PARAMS[window]

    ovr_by_id: dict[int, float] = {}
    scouted_by_id: dict[int, ScoutedPeak] = {}
    for rp in prospects:
        ovr = effective_talent_ovr(rp, profile)
        player = rp.player
        ratings = rp.ratings
        age = float(player_age(player.birth_date, season))
        scouted = scout_player(
            league_rng_seed=league_rng_seed,
            team_id=team_id,
            player_id=player.id,
            season=season,
            ovr=ovr,
            age=age,
            peak_age_start=ratings.peak_age_start,
            potential=ratings.potential,
            potential_variance=ratings.potential_variance,
            draft_eligible=player.roster_status is RosterStatus.DRAFT_ELIGIBLE,
            risk=profile.risk,
            tuning=tuning,
        )
        ovr_by_id[rp.id] = float(ovr)
        scouted_by_id[rp.id] = scouted

    rank_priced = [
        (
            rp,
            prospect_value(
                rp,
                ovr_by_id[rp.id],
                scouted_by_id[rp.id],
                profile,
                params,
                cap_settings,
                rookie_scale,
                season,
                tuning.board_reference_slot,
                tuning=tuning,
            ),
        )
        for rp in prospects
    ]
    rank_order = sorted(rank_priced, key=lambda pair: (-pair[1], pair[0].id))

    entries = []
    for i, (rp, rank_value) in enumerate(rank_order):
        implied_slot = i + 1
        implied_slot_value = prospect_value(
            rp,
            ovr_by_id[rp.id],
            scouted_by_id[rp.id],
            profile,
            params,
            cap_settings,
            rookie_scale,
            season,
            implied_slot,
            tuning=tuning,
        )
        entries.append(
            DraftBoardEntry(
                prospect_id=rp.id,
                implied_slot=implied_slot,
                rank_value=rank_value,
                implied_slot_value=implied_slot_value,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# §9.4 -- running an actual, multi-team draft
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftSlot:
    """One pick in a real draft order -- the team on the clock and how it
    values assets. `matrix` is that team's own `NeedMatrix` (built from
    its own roster, `needs.py::build_need_matrix`), caller-supplied since
    this module never reads a `TeamSnapshot` roster on its own.

    `pick_number` is that pick's own real, resolved DB pick number --
    supplied by the caller, never re-derived from this slot's position in
    `draft_order`. A caller's `draft_order` is expected to already carry
    pick identity: a filtered (lottery-unresolved picks removed) or
    resumed (already-used picks removed) draft is a normal, expected input
    shape, and in either case list position and real pick number routinely
    diverge (Decision P2-27/gm_service.py's `_eligible_draft_picks_sorted`
    is exactly such a filter)."""

    team_id: int
    philosophy: GmPhilosophy
    window: CompetitiveWindow
    matrix: NeedMatrix
    pick_number: int


@dataclass(frozen=True)
class DraftPickResult:
    pick_number: int
    team_id: int
    prospect_id: int
    value: float
    record: DecisionRecord
    """Decision P2-18's explainability record for this pick (module
    docstring) -- `market_value`/`need_bonus` terms, summing to (within
    floating-point noise of) `value`, `threshold` the runner-up
    candidate's own fit value."""


def run_draft(
    prospects: tuple[RosterPlayer, ...],
    draft_order: tuple[DraftSlot, ...],
    *,
    league_rng_seed: int,
    season: int,
    cap_settings: LeagueCapSettings,
    rookie_scale: RookieScaleTable,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[DraftPickResult, ...]:
    """Simulates `draft_order` one pick at a time: the team on the clock
    scores every *remaining* prospect via `prospect_fit_value_for` at
    `implied_slot = slot.pick_number` (the real, resolved slot -- not the
    board's own pre-draft rank), takes the max, removes it from the pool.
    `remaining` is a plain dict keyed by prospect id, popped on each pick
    -- "no team drafts an already-selected player" holds structurally,
    not just by convention, since a drafted prospect is physically absent
    from every later pick's candidate pool. Stops early (fewer results
    than `len(draft_order)`) if the prospect pool runs out first.

    `draft_order`'s own list position is used only for iteration, never
    for pick identity -- each `DraftSlot` carries its own real
    `pick_number`, supplied by the caller (see `DraftSlot`'s docstring for
    why this is a deliberate, permanent correction rather than an
    incidental deviation: a filtered or resumed `draft_order` is a normal
    input shape, and list position does not track real pick number for
    either).

    Selection logic (the `max` below) is unchanged from before this fix
    round -- only the `DecisionRecord` built afterward, from the already-
    decided winner, is new (Decision P2-18, module docstring)."""
    remaining: dict[int, RosterPlayer] = {rp.id: rp for rp in prospects}
    results: list[DraftPickResult] = []

    for slot in draft_order:
        if not remaining:
            break
        scored = [
            (
                rp,
                prospect_fit_value_for(
                    rp,
                    slot.matrix,
                    league_rng_seed=league_rng_seed,
                    team_id=slot.team_id,
                    season=season,
                    philosophy=slot.philosophy,
                    window=slot.window,
                    cap_settings=cap_settings,
                    rookie_scale=rookie_scale,
                    implied_slot=slot.pick_number,
                    tuning=tuning,
                ),
            )
            for rp in remaining.values()
        ]
        best_rp, best_value = max(scored, key=lambda pair: (pair[1], -pair[0].id))

        profile = PROFILES[slot.philosophy]
        winner_ovr = effective_talent_ovr(best_rp, profile)
        market_value = prospect_value_for(
            best_rp,
            league_rng_seed=league_rng_seed,
            team_id=slot.team_id,
            season=season,
            philosophy=slot.philosophy,
            window=slot.window,
            cap_settings=cap_settings,
            rookie_scale=rookie_scale,
            implied_slot=slot.pick_number,
            tuning=tuning,
        )
        bonus = clamp_need_bonus(
            need_bonus(best_rp, winner_ovr, slot.matrix, profile, tuning=tuning)
        )
        terms = (
            ReasonTerm(
                label="market_value", input_value=market_value, weight=1.0,
                contribution=market_value,
            ),
            ReasonTerm(
                label="need_bonus", input_value=bonus, weight=market_value,
                contribution=market_value * bonus,
            ),
        )
        score = sum(term.contribution for term in terms)
        other_values = [v for rp2, v in scored if rp2.id != best_rp.id]
        threshold = max(other_values) if other_values else best_value
        record = DecisionRecord(
            team_id=slot.team_id, philosophy=slot.philosophy, window=slot.window,
            subject=f"pick:{slot.pick_number}", verdict=f"select:prospect:{best_rp.id}",
            score=score, threshold=threshold, terms=terms,
        )

        results.append(
            DraftPickResult(
                pick_number=slot.pick_number,
                team_id=slot.team_id,
                prospect_id=best_rp.id,
                value=best_value,
                record=record,
            )
        )
        del remaining[best_rp.id]

    return tuple(results)
