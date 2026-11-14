"""Pillar 5's satisfaction/morale/trust evaluator and the `RequestState`
ladder -- Decisions P4-6, P4-7/P4-44/P4-45, P4-14/P4-56/P4-61/P4-70/P4-71.

docs/design/phase-4-media-and-player-agency.md §4-5.

Everything here is pure: no `dba2.data`, no repo, no connection, no
`Executor` (P4-13 -- "the pure evaluator must therefore never take a
repo... it takes the situation and the current row, and returns
deltas," so the identical code serves both the DB-backed
`services/agency_service.py` and the in-memory `tools/gm_harness/`
caller). Every RNG draw goes through `rng_for(..., "motivation", ...)`
(Decision P4-6) -- never a bare `random.Random(`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dba2.agency.situation import AXIS_SCORERS, PlayerSituation
from dba2.core.rng import rng_for
from dba2.domain.motivation import MarketPreference, MotivationAxis, RequestState

# ---------------------------------------------------------------------------
# Initial state -- a freshly generated player's first row (Stage B step 6's
# bootstrap writer).
# ---------------------------------------------------------------------------

INITIAL_SATISFACTION = 0
"""Neutral -- nothing has happened to this player yet."""

INITIAL_MORALE = 0
INITIAL_TRUST = 0
INITIAL_REQUEST_STATE = RequestState.CONTENT


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Decision P4-6 -- trait generation
# ---------------------------------------------------------------------------

_WEIGHT_MEAN_BASE = 50.0
_WEIGHT_SPREAD = 20.0
_VETERAN_AGE_FLOOR = 22.0
_VETERAN_AGE_CEIL = 37.0
_VETERAN_LOYALTY_BONUS = 15.0
_ROOKIE_LEGACY_BONUS = 10.0
_STAR_LEVERAGE_TALENT_FLOOR = 60
_STAR_LEVERAGE_TALENT_CEIL = 100
"""All five constants above are disclosed, not calibrated -- the same
status every other Phase 4 knob carries (§14's "disclosed levers, not
measurements"). None bump `GM_TUNING_VERSION`: they live under
`dba2/agency/`, not `managers/` (Decision P4-3's corollary)."""


@dataclass(frozen=True)
class GeneratedTraits:
    """One player's trait-column bundle -- everything `create_player_
    motivation` needs beyond the initial state constants above."""

    weight_winning: int
    weight_role_usage: int
    weight_loyalty: int
    weight_money: int
    weight_legacy: int
    star_leverage: int
    market_preference: MarketPreference
    hometown_team_id: int | None


def generate_traits(
    league_rng_seed: int, player_id: int, *, age: float, overall: int
) -> GeneratedTraits:
    """Decision P4-6: seeded, deterministic, independently salted
    (`rng_for(league_rng_seed, "motivation", player_id)` -- a motivation
    draw can never perturb the scouting/chaos/phrase streams). Weights
    are drawn around archetype-conditioned means, not uniformly: `age` is
    the one archetype signal available identically for real and
    generated players (Decision P4-12), so a veteran draws a higher
    loyalty mean and a lower legacy-chase mean than a rookie.
    `star_leverage` is DERIVED from `overall`, never drawn -- a 12th
    man's leverage is not a personality trait (Decision P4-6). Real
    players get this identical generator (Decision P4-12: motivation is
    purely sim-state-driven for everyone)."""
    rng = rng_for(league_rng_seed, "motivation", player_id)

    veteran_bias = _clamp(
        (age - _VETERAN_AGE_FLOOR) / (_VETERAN_AGE_CEIL - _VETERAN_AGE_FLOOR), 0.0, 1.0
    )
    means = {
        MotivationAxis.WINNING: _WEIGHT_MEAN_BASE,
        MotivationAxis.ROLE_USAGE: _WEIGHT_MEAN_BASE,
        MotivationAxis.LOYALTY: _WEIGHT_MEAN_BASE + _VETERAN_LOYALTY_BONUS * veteran_bias,
        MotivationAxis.MONEY: _WEIGHT_MEAN_BASE,
        MotivationAxis.LEGACY: _WEIGHT_MEAN_BASE + _ROOKIE_LEGACY_BONUS * (1.0 - veteran_bias),
    }
    weights = {
        axis: int(round(_clamp(rng.gauss(mean, _WEIGHT_SPREAD), 0.0, 100.0)))
        for axis, mean in means.items()
    }

    talent_norm = _clamp(
        (overall - _STAR_LEVERAGE_TALENT_FLOOR)
        / (_STAR_LEVERAGE_TALENT_CEIL - _STAR_LEVERAGE_TALENT_FLOOR),
        0.0,
        1.0,
    )
    star_leverage = int(round(talent_norm * 100.0))

    market_preference = rng.choice(list(MarketPreference))

    return GeneratedTraits(
        weight_winning=weights[MotivationAxis.WINNING],
        weight_role_usage=weights[MotivationAxis.ROLE_USAGE],
        weight_loyalty=weights[MotivationAxis.LOYALTY],
        weight_money=weights[MotivationAxis.MONEY],
        weight_legacy=weights[MotivationAxis.LEGACY],
        star_leverage=star_leverage,
        market_preference=market_preference,
        # Decision P4-12: no real hometown data exists for any player,
        # real or generated -- inventing one would be fabrication
        # presented as real.
        hometown_team_id=None,
    )


# ---------------------------------------------------------------------------
# Decision P4-13/P4-9 -- the satisfaction evaluator (requested deltas)
# ---------------------------------------------------------------------------

SATISFACTION_SCALE = 10.0
"""Disclosed, not calibrated -- the largest per-axis requested move in
one checkpoint (full weight, full-magnitude `[-1, 1]` signal). Two
checkpoints a season (Decision P4-11) across a ~15-year career means a
±100 axis saturates as a matter of routine at this scale -- the
behaviour P4-44/P4-65's saturating fold exists to handle correctly, not
to avoid."""

_REQUEST_THRESHOLD = 1e-9
"""Below this, a raw scorer signal is treated as "no evaluation happened
this checkpoint for this axis," not "the evaluation happened and
computed exactly zero" -- keeps a checkpoint from emitting five near-
zero-noise rows for a player whose situation has no real signal on most
axes. Never suppresses anything the reconstruction-invariant tests
depend on: those hand-construct their own `RequestedDelta`s directly and
never go through this function."""


@dataclass(frozen=True)
class RequestedDelta:
    """One axis's requested (unsaturated) satisfaction move, as the pure
    evaluator emits it -- P4-10/P4-13: never a new satisfaction vector,
    always a delta with a cause. `note` records anything the evaluator
    itself wants explained (`fold_satisfaction_deltas` appends its own
    saturation note on top, never replacing this one)."""

    axis: MotivationAxis
    requested: float
    event_type: str
    note: str | None = None


def evaluate_satisfaction(
    weights: Mapping[MotivationAxis, int],
    situation: PlayerSituation,
    *,
    event_type: str = "checkpoint_evaluation",
    scale: float = SATISFACTION_SCALE,
) -> tuple[RequestedDelta, ...]:
    """P4-13's pure evaluator: takes the situation and the player's own
    (normalized) weights, returns REQUESTED per-axis deltas -- never a
    new satisfaction vector (P4-10: "computing the new vector and back-
    deriving the deltas would satisfy the arithmetic and destroy the
    point"). Never sees the current satisfaction row or its headroom --
    saturation is the write boundary's job (`fold_satisfaction_deltas`,
    P4-44), not this function's. Iterates `AXIS_SCORERS` in `MotivationAxis`
    declaration order, so the emission order this checkpoint's deltas
    arrive in is deterministic (P4-44: "emission order is part of the
    contract, not an implementation accident")."""
    deltas: list[RequestedDelta] = []
    for axis, scorer in AXIS_SCORERS.items():
        raw = scorer(situation)
        if abs(raw) < _REQUEST_THRESHOLD:
            continue
        requested = raw * (weights[axis] / 100.0) * scale
        if abs(requested) < _REQUEST_THRESHOLD:
            continue
        deltas.append(RequestedDelta(axis=axis, requested=requested, event_type=event_type))
    return tuple(deltas)


# ---------------------------------------------------------------------------
# Decision P4-44/P4-65 -- the saturating fold (the reconstruction invariant)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WrittenDelta:
    """One axis's ACTUALLY-applied, already-saturated, already-`int`
    delta -- what `player_motivation_events.delta` receives. `applied`
    may differ from the `RequestedDelta.requested` it was folded from
    only by saturation (never by anything else)."""

    axis: MotivationAxis
    applied: int
    event_type: str
    note: str | None


def fold_satisfaction_deltas(
    current: Mapping[MotivationAxis, int],
    requested: Sequence[RequestedDelta],
) -> tuple[dict[MotivationAxis, int], tuple[WrittenDelta, ...]]:
    """Decision P4-44 (the reconstruction invariant), as corrected by
    P4-65 (the rounding order, and what the range clause actually
    catches). Folds `requested` IN EMISSION ORDER, per axis, each
    individually truncated against the remaining integer headroom:
    `applied = clamp(prior + round(requested), -100, 100) - prior`.
    Every quantity that touches the invariant is an `int` from the first
    write onward (P4-65 part 2) -- the float `requested` delta is rounded
    to `int` BEFORE saturating against the running integer total, never
    after a float running total is accumulated and rounded once at the
    end (which would make the invariant false intermittently, on data-
    dependent inputs -- P4-65's own named failure mode).

    A truncated-to-zero delta still returns a `WrittenDelta` with
    `applied == 0` (P4-10/P4-44: an absent row and a zero row reconstruct
    identically and explain differently -- explanation is the whole
    point).

    **The invariant this fold guarantees, restated as code rather than
    prose:** for any axis, `running[axis]` after == `current[axis]`
    before + `sum(w.applied for w in written if w.axis == axis)`,
    exactly -- and independently, for every written delta,
    `-100 <= (running[axis] before that delta) + applied <= 100`, which
    is what actually distinguishes this fold from a writer that
    fabricates deltas (P4-65: pure summation alone cannot catch a
    back-deriving writer; this range property is checked by
    construction here, never merely asserted by a caller)."""
    running: dict[MotivationAxis, int] = dict(current)
    written: list[WrittenDelta] = []
    for item in requested:
        prior = running[item.axis]
        requested_int = int(round(item.requested))
        new_value = _clamp_int(prior + requested_int, -100, 100)
        applied = new_value - prior
        running[item.axis] = new_value

        note = item.note
        if applied != requested_int:
            saturation_note = (
                f"requested {requested_int:+d}, applied {applied:+d} (saturated at {new_value})"
            )
            note = saturation_note if note is None else f"{note}; {saturation_note}"

        written.append(
            WrittenDelta(axis=item.axis, applied=applied, event_type=item.event_type, note=note)
        )
    return running, tuple(written)


# ---------------------------------------------------------------------------
# Decision P4-45 -- morale (stateless roll-up) and trust (accumulated, decays)
# ---------------------------------------------------------------------------


def compute_morale(
    weights: Mapping[MotivationAxis, int], satisfactions: Mapping[MotivationAxis, int]
) -> int:
    """Decision P4-45: `morale` is a pure, stateless convex combination of
    the five `(weight, satisfaction)` pairs, recomputed on every write --
    never independently accumulated (unlike `trust_in_management`,
    below). Its own clamp is vacuous by construction (a convex
    combination of values already in `[-100, 100]` cannot leave that
    range) EXCEPT when every weight is `0`, which would otherwise divide
    by zero -- that degenerate case reads `morale = 0`, not
    `ZeroDivisionError` (P4-24's totality rule, arriving on the agency
    side)."""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0
    weighted = sum(weights[axis] * satisfactions[axis] for axis in MotivationAxis)
    return int(round(weighted / total_weight))


TRUST_DECAY = 0.85
"""Disclosed. Matches `dba2/managers/memory.py::decay_head_to_head`'s own
multiplicative-decay SHAPE (Decision P4-7: "reusing that decay shape
rather than inventing one is deliberate") -- not the same numeric
constant as `H2H_DECAY` (a different quantity on a different scale),
just the same mechanism, and not tuned against any measurement."""


def decay_trust(trust: int) -> int:
    """Decays `trust_in_management` toward zero every checkpoint
    (Decision P4-7: "it accumulates from events and decays toward
    zero")."""
    return int(round(trust * TRUST_DECAY))


@dataclass(frozen=True)
class WrittenTrustDelta:
    applied: int
    requested: int


def fold_trust_deltas(
    current: int, requested: Sequence[float]
) -> tuple[int, tuple[WrittenTrustDelta, ...]]:
    """Same saturating-fold shape P4-44/P4-65 specify for satisfaction,
    applied to `trust_in_management` per Decision P4-45 (the column that
    is genuinely unbounded on the schema and needs the write-boundary
    clamp, unlike `morale`). **Stage B has no real trust-moving event
    source yet** -- a broken promise, a near-trade, a role taken away
    (P4-7's own examples) are all later-phase mechanisms nothing in
    Stage B produces -- so every Stage B checkpoint calls this with an
    empty `requested` sequence (decay only), built ready for a future
    caller exactly as `memory.decay_head_to_head_rows` was built ready
    before any production caller existed.

    **`current` is clamped on entry, unconditionally -- not only when a
    delta moves it.** P4-45's write-boundary clamp must hold even for the
    decay-only, zero-`requested` call every Stage B checkpoint makes
    today: with `requested` empty the loop below never runs, so a
    `current` that arrived out of range (a manual edit, a pre-Phase-4
    row, anything upstream of this function) would otherwise pass
    straight through unclamped and this function's own clamp would be
    real only when it happens to have work to do. Round-5 critic finding
    B4: the clamp must be a property of calling this function, not of
    calling it with a non-empty `requested`."""
    running = _clamp_int(current, -100, 100)
    written: list[WrittenTrustDelta] = []
    for raw in requested:
        prior = running
        requested_int = int(round(raw))
        new_value = _clamp_int(prior + requested_int, -100, 100)
        applied = new_value - prior
        running = new_value
        written.append(WrittenTrustDelta(applied=applied, requested=requested_int))
    return running, tuple(written)


# ---------------------------------------------------------------------------
# Decision P4-14/P4-56/P4-61/P4-70/P4-71 -- the RequestState ladder
# ---------------------------------------------------------------------------

LADDER_TRANSITION_EVENT_TYPE = "ladder_transition"
"""Decision P4-56: every ladder transition writes a `player_motivation_
events` row with this `event_type` and `delta = 0` -- a zero-delta
marker row, invisible to the reconstruction invariant (P4-44's fold
sums to zero-contribution by construction), so no exclusion clause is
ever needed there."""


def _rung_index(state: RequestState) -> int:
    if state is RequestState.CONTENT:
        return 0
    if state is RequestState.UNSETTLED:
        return 1
    if state in (RequestState.TRADE_REQUESTED, RequestState.EXTENSION_DEMANDED):
        return 2
    if state is RequestState.HOLDOUT:
        return 3
    raise AssertionError(f"unreachable RequestState {state!r}")


@dataclass(frozen=True)
class LadderBar:
    enter: float
    stay: float

    def __post_init__(self) -> None:
        if not self.enter > self.stay:
            raise ValueError(f"enter bar ({self.enter}) must be > stay bar ({self.stay})")


@dataclass(frozen=True)
class LadderBars:
    """One `(enter, stay)` pair per rung boundary: rung 1 = content ->
    unsettled, rung 2 = unsettled -> {trade_requested|extension_demanded},
    rung 3 = requested -> holdout."""

    rung_1: LadderBar
    rung_2: LadderBar
    rung_3: LadderBar

    def bar(self, rung: int) -> LadderBar:
        return (self.rung_1, self.rung_2, self.rung_3)[rung - 1]


DEFAULT_LADDER_BARS = LadderBars(
    rung_1=LadderBar(enter=20.0, stay=10.0),
    rung_2=LadderBar(enter=50.0, stay=35.0),
    rung_3=LadderBar(enter=75.0, stay=60.0),
)
"""Disclosed, not calibrated -- same status as every other Phase 4 knob
this design leaves to a future tuning pass (`_ROLE_FIT_SCALE`,
`SATISFACTION_SCALE`, etc.). `pressure` is expected on `morale`'s own
`[-100, 100]` scale, negated (see `pressure_from_morale`) -- a player
unhappier than these bars escalates. Increasing across rungs (`rung_1.
enter < rung_2.enter < rung_3.enter`) is this evaluator's own contract,
not re-validated here -- a builder sweeping these is expected to keep
the ordering, the same way P4-9's axis coefficients are expected to be
swept without this module re-deriving its own tests each time."""


def pressure_from_morale(morale: int) -> float:
    """The one scalar the ladder evaluates against. `morale` already
    rolls up all five axes (Decision P4-7) -- lower morale is more
    dissatisfied, so `pressure = -morale` makes "higher pressure
    escalates" read the same direction for every rung boundary."""
    return float(-morale)


TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD: int = 75
"""Decision P4-15/P4-37: only a player whose `star_leverage` clears this
bar produces a trade request any GM-facing system reads -- "a league
where all 450 players can bind a GM's hand is a league where the
mechanic is noise," ratified without change by the user. Disclosed, not
calibrated -- same status as `LADDER_BARS` above and every other Phase 4
knob this design leaves to a future tuning pass. `star_leverage` is
`int(round(talent_norm * 100.0))` (`generate_traits`, above) -- `75`
reads as "clearly above mid-roster talent," not derived from any
measured distribution."""


def trade_request_pressure(
    *, request_state: RequestState, star_leverage: int, morale: int
) -> float | None:
    """Decision P4-17/P4-51's sparse trade-request pressure signal, as a
    pure function of already-loaded state -- both `services/agency_
    service.py` (the DB path) and `tools/gm_harness/state.py` (the
    harness mirror) call this identically, so the P4-15 binding gate and
    the pressure magnitude have exactly one implementation each, never
    two that could silently drift (the same "one shared formula" reason
    `window_success_score`/`resolve_request_opened_in_season` live here
    rather than in each caller).

    `None` (the caller building the sparse `GmContext.trade_request_
    pressure_by_player` mapping drops every `None` result, per Decision
    P4-51's "absence is neutral" contract) unless BOTH:

    - **P4-15's binding gate clears**: `star_leverage >= TRADE_REQUEST_
      BINDS_STAR_LEVERAGE_THRESHOLD`. A demand from a player who does not
      clear this bar is real (it still moved satisfaction, it still
      feeds media) but does not bind a GM's hand -- "a feelings problem,
      not a roster problem" (Decision P4-37).
    - **The ladder is actually at a rung a TRADE reaches**: `TRADE_
      REQUESTED` or `HOLDOUT` only. `EXTENSION_DEMANDED` biases `rank_
      offers` at the re-sign window instead (Decision P4-18), never
      `propose_packages`; `UNSETTLED`/`CONTENT` are both below the rung
      P4-15's gate is even about.

    The magnitude is `pressure_from_morale(morale)`, the SAME reading the
    ladder itself already used to decide this player's own rung
    transitions -- no second, independently-tunable pressure formula."""
    if request_state not in (RequestState.TRADE_REQUESTED, RequestState.HOLDOUT):
        return None
    if star_leverage < TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD:
        return None
    return pressure_from_morale(morale)


def dwell_is_open(*, current_season: int, last_transition_season: int | None) -> bool:
    """Decision P4-56's dwell definition, as amended by P4-71: zero prior
    `ladder_transition` rows (`last_transition_season is None`) means the
    dwell gate does NOT apply -- open, never a deadlock (P4-71: the
    alternative makes the entire ladder permanently inert, since a row is
    only ever written BY a transition). The sentinel is `None`, never
    `0` -- a mirror initialised to `0` would read `current_season - 0
    >= 1` for every real season, accidentally correct and wrong by
    construction (P4-71)."""
    if last_transition_season is None:
        return True
    return (current_season - last_transition_season) >= 1


def step_ladder_rung(
    *, currently_in: bool, value: float, enter_bar: float, stay_bar: float, dwell_open: bool
) -> bool:
    """The single generic hysteresis+dwell step Decisions P4-56/P4-61/
    P4-70/P4-71 specify and test against, in this exact shape (the
    mutation P4-61/P4-70 name -- replacing the stay-bar comparison with
    the enter-bar comparison while `currently_in` -- must flip the
    ladder-hysteresis test's assertion; see
    `tests/unit/test_agency_motivation.py`).

    Minimum dwell (P4-56, ruled on by P4-61 part 2 and P4-71) gates the
    UPWARD transition only -- downward release is governed by `stay_bar`
    alone, unconditionally, regardless of `dwell_open`. `enter_bar >
    stay_bar` is the caller's contract (`LadderBar.__post_init__`
    enforces it), not re-checked here."""
    if currently_in:
        return value >= stay_bar
    return value >= enter_bar and dwell_open


_NEAR_EXPIRY_YEARS_REMAINING = 1
"""Disclosed threshold (Decision P4-14: "a player with real term
remaining asks out; a player near expiry demands an extension") -- one
year of contractual control remaining or less reads as "near expiry."
Not derived from any real negotiation-timing data."""


def _resolve_request_flavor(contract_years_remaining: int | None) -> RequestState:
    if (
        contract_years_remaining is not None
        and contract_years_remaining <= _NEAR_EXPIRY_YEARS_REMAINING
    ):
        return RequestState.EXTENSION_DEMANDED
    return RequestState.TRADE_REQUESTED


def _state_for_rung(
    rung: int, *, current: RequestState, contract_years_remaining: int | None = None
) -> RequestState:
    if rung == 0:
        return RequestState.CONTENT
    if rung == 1:
        return RequestState.UNSETTLED
    if rung == 2:
        # Mutually exclusive by construction (Decision P4-14): keep the
        # existing flavor if the player was already at rung 2, resolve a
        # fresh one only when entering from below.
        if current in (RequestState.TRADE_REQUESTED, RequestState.EXTENSION_DEMANDED):
            return current
        return _resolve_request_flavor(contract_years_remaining)
    if rung == 3:
        return RequestState.HOLDOUT
    raise AssertionError(f"unreachable rung {rung!r}")


def advance_request_state(
    current: RequestState,
    *,
    pressure: float,
    dwell_open: bool,
    bars: LadderBars = DEFAULT_LADDER_BARS,
    contract_years_remaining: int | None = None,
) -> tuple[RequestState, bool]:
    """One checkpoint's full ladder evaluation (Decision P4-14, as
    amended by P4-56/P4-61/P4-70/P4-71).

    **Upward** movement is capped at one rung per checkpoint --
    `apply_hysteresis`'s actual, genuinely-reused shape (P4-56 part 1:
    "P4-14's 'no upward skipping' rung rule IS `apply_hysteresis`'s
    actual shape"), gated by `dwell_open` for that one rung only.
    **Downward** movement is NOT capped (Decision P4-14: "downward
    movement allowed from any rung") -- a large satisfaction recovery can
    clear more than one rung in a single checkpoint, released rung by
    rung from the top down, each release gated by that rung's own
    `stay_bar` alone (dwell never gates a downward move).

    Returns `(new_state, transitioned)`. A transition of any kind (up or
    down) is the caller's cue to write a `LADDER_TRANSITION_EVENT_TYPE`
    marker row and update its own dwell bookkeeping (`last_transition_
    season`) -- this function has no side effects and touches no dwell
    state itself, per P4-13's "the pure evaluator never takes a repo"
    rule extended to the harness mirror as well."""
    current_rung = _rung_index(current)

    rung = current_rung
    while rung > 0 and not step_ladder_rung(
        currently_in=True,
        value=pressure,
        enter_bar=bars.bar(rung).enter,
        stay_bar=bars.bar(rung).stay,
        dwell_open=dwell_open,
    ):
        rung -= 1
    if rung < current_rung:
        # Round-5 critic finding B1: `contract_years_remaining` must be
        # threaded through here too, not only on the upward branch below.
        # A de-escalation that RELEASES from rung 3 (`HOLDOUT`) straight
        # to rung 2 lands on `_state_for_rung`'s flavor-resolution branch
        # (`current` is `HOLDOUT`, not already `TRADE_REQUESTED`/
        # `EXTENSION_DEMANDED`), which must resolve from the player's real
        # contract state (P4-14), not from a `None` default that always
        # reads TRADE_REQUESTED regardless of which direction the player
        # arrived from.
        return (
            _state_for_rung(
                rung, current=current, contract_years_remaining=contract_years_remaining
            ),
            True,
        )

    if current_rung < 3:
        next_rung = current_rung + 1
        entered = step_ladder_rung(
            currently_in=False,
            value=pressure,
            enter_bar=bars.bar(next_rung).enter,
            stay_bar=bars.bar(next_rung).stay,
            dwell_open=dwell_open,
        )
        if entered:
            return (
                _state_for_rung(
                    next_rung, current=current, contract_years_remaining=contract_years_remaining
                ),
                True,
            )

    return current, False


def resolve_request_opened_in_season(
    *,
    previous_state: RequestState,
    new_state: RequestState,
    season: int,
    previous_request_opened_in_season: int | None,
) -> int | None:
    """Round-5 critic finding B1: `request_opened_in_season` "keeps its
    own job -- recording when the request opened" (Decision P4-56) -- a
    dwell mechanism or a lateral/downward ladder move must never also
    write it, because that gives one column two meanings. Both production
    callers (`services/agency_service.py`, `tools/gm_harness/season.py`)
    previously computed this inline, from `new_state.value` string
    comparisons alone, with no notion of which DIRECTION the checkpoint
    moved -- which is exactly how the same bug landed in both places.
    Centralized here, in the pure package, as the one place both callers
    read from, the same reasoning `AXIS_SCORERS` already applies to
    keeping DB and harness normalization identical.

    Ruled by RUNG, not by `RequestState` identity, because "rung 2" is
    two mutually-exclusive states (`TRADE_REQUESTED`/`EXTENSION_DEMANDED`)
    that must be treated identically here:

    - **Newly opened** -- the checkpoint crosses INTO rung 2 or higher
      from below (`content`/`unsettled`) -- returns `season`, the
      checkpoint's own season, recording when the request genuinely
      opened.
    - **Dropped below rung 2** -- lands on `content` OR `unsettled` --
      returns `None`. Reaching `content` alone was the pre-fix rule; a
      request that quiets to `unsettled` has no open request either, and
      leaving the column set there was the second variant of this bug.
    - **Lateral or downward, but still at or above rung 2** (e.g.
      `holdout` releasing to `trade_requested`, or no rung change at
      all) -- returns `previous_request_opened_in_season` UNCHANGED. This
      is the case a downward move must never touch: the request opened
      whenever it first crossed into rung 2, and a later de-escalation
      that stays at or above rung 2 is not a new request."""
    old_rung = _rung_index(previous_state)
    new_rung = _rung_index(new_state)
    if old_rung < 2 <= new_rung:
        return season
    if new_rung < 2:
        return None
    return previous_request_opened_in_season


def largest_magnitude_axis(written: Sequence[WrittenDelta]) -> MotivationAxis:
    """Decision P4-56: the axis that drove a ladder transition -- the
    largest-magnitude contributing axis at that checkpoint's satisfaction
    write, ties broken by `MotivationAxis` DECLARATION order (the enum's
    own iteration order is stable). `written` empty (a transition driven
    by pressure carried over from an earlier checkpoint's saturation,
    with no satisfaction movement of its own this checkpoint) falls back
    to the first axis in declaration order rather than raising."""
    if not written:
        return next(iter(MotivationAxis))
    totals: dict[MotivationAxis, int] = {}
    for w in written:
        totals[w.axis] = totals.get(w.axis, 0) + w.applied
    order = {axis: i for i, axis in enumerate(MotivationAxis)}
    return max(totals, key=lambda axis: (abs(totals[axis]), -order[axis]))
