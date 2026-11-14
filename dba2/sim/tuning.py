"""Every tunable constant and weight vector the engine reads, as one frozen
profile object (phase-1-simulation-engine.md §3, §4.4, Decision P1-12).

Decision P1-12: each beat is "two weighted rating aggregates, a difference,
a logistic, a clamp, and a weighted branch draw." The weight vectors below
are the direct data implementation of §4.4's table -- one module, one
tunable surface, so a calibration sweep (§13's largest named risk) is a
parameter edit here, never a code change.

`TUNING_VERSION` is Decision P1-22's `games.engine_version` source: bumped
on any change to a weight vector or constant in this file. It stays a
module-level constant (not a `TuningProfile` field) because it identifies
*which* `TuningProfile` shape/values produced a result, the same way a
schema migration number identifies a schema rather than living inside a
row of the schema it describes.

**Scope read by intent, not by file, per
docs/design/phase-1-step-7-calibration.md §10.3 (Decision S7-1c).** P1-22's
literal wording ties the bump to "a change to `tuning.py` or any beat's
weight vector," but its *purpose* is "can this stored game be replayed?" --
so this constant is also bumped for a change to code that determines sim
output at a fixed seed and fixed roster even when that code lives outside
this file, e.g. `sim/game.py`'s rotation-tier minutes anchors (bumped 4 -> 5
alongside the S7-1b retune, and 9 -> 10 alongside
`docs/design/auto-gm-role-construction.md` RC-4's per-rank curve replacing
the flat tiers). A clarification of P1-22's rule, not an amendment to it;
flagged for the parent design's next amendment pass.

Zero I/O, per Decision P1-1 -- this module imports only `domain` (for the
`DefensiveScheme` enum used as a lookup key) and stdlib.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from dba2.domain.role import DefenseMode, ShotDiet, UsageMode
from dba2.domain.team import DefensiveScheme, OffensiveScheme

TUNING_VERSION: int = 26
"""10 -> 11: docs/design/gm-command-surface.md PD-2 -- `team_strategy.
three_point_bias` becomes load-bearing (`ResolutionShotTuning.three_point_
bias_lo/_hi`, `beats/resolution.py::select_shot_zone`), a real change to
sim output at a fixed seed and fixed roster (a real league's `three_point_
bias` is never uniformly 50, per `strategy_service._NUMERIC_SPECS`).
11 -> 12: PD-3/PD-4 -- `player_directives.shot_diet`/`defense_mode` become
load-bearing (`ResolutionShotTuning.shot_diet_zone_multipliers` at the
same `select_shot_zone` seam; `HelpTuning.defense_mode_gate_offset` in
`beats/help.py::_gate_probability`). Both default to `BALANCED` (neutral,
no-op) for every player with no directive row, so this bump changes
output only for a game with at least one non-`BALANCED` directive set.
12 -> 13: PD-5 -- `player_directives.usage_mode` becomes load-bearing
(`SetupTuning.usage_mode_family_multipliers` in `beats/setup.py::
_role_weight`), same BALANCED-is-neutral/no-directive-row-is-neutral
shape as PD-3/PD-4.
13 -> 14: PD-10 -- `team_strategy.offensive_scheme` becomes load-bearing
(`SetupTuning.offensive_scheme_action_multipliers` in `beats/setup.py::
_action_type_weights`). Wider in scope than PD-2/3/4/5's bumps: there is
no neutral `OffensiveScheme` member and no "no directive row" carve-out --
`TeamStrategy.offensive_scheme` is non-optional, so every team in every
league has always had a real value here, which means this bump changes
sim output at a fixed seed and fixed roster for every real league, not
only ones that opted into a directive.
14 -> 15: docs/design/per-36-spread-compression.md Commit 2 -- the rebound
attribution draw (`beats/rebound.py::resolve_rebound_beat`) raises
`ability_score` to `ReboundModifierTuning.attribution_concentration_
exponent` (2.4) before `_crash_scale`/`out_of_position_defender_penalty`
multiply it, unchanged. A real change to sim output at a fixed seed and
fixed roster -- this fires on every rebound beat, not a directive/scheme
edge case (SC-3.7's fatigue-channel bound: ~7 of a season's 1,230 games
are expected to re-realize from some possession onward). This field and
bump were first added alongside `bccb7c8` but a concurrently-committed,
unrelated `touch_share` rescale (`1b655fe`) was staged from a copy of this
file that predated the addition and silently dropped it on commit; this
bump restores it as a forward fix rather than amending either commit.
15 -> 16: docs/design/per-36-spread-compression.md Commit 3 -- the
kickout/roll-vs-no_pass branch draw (`beats/rotation.py::
resolve_rotation_beat`) tilts `RotationBranchTuning`'s three base weights
by the ball-handler's own passing ability (`RotationBranchTuning.
passer_tilt_anchor_low/_high`, `neutral_passer_score`) before
`branch_total` is computed. A real change to sim output at a fixed seed
and fixed roster on every ball-handler action whose Help beat returns
`HELP_COMMITTED`/`HELP_LATE` (SC-3.5) -- only a passer whose `0.6*
passing_vision + 0.4*passing_accuracy` score is exactly `neutral_passer_
score` (50.0) reproduces today's exact 0.45/0.35/0.20 split; every other
passer's branch weights move.
16 -> 17: docs/design/per-36-spread-compression.md Commit 3 forward-fix,
post-critic-review -- an independent critic found `849a4e7`'s "mean-
preserving by construction" claim false on three grounds (additive, not
odds-space, anchor symmetry; Jensen's inequality; and a real passer_score
distribution centered at 69.9/69.3 mean/median with 48.6% of the league
clamped at the old high anchor, not near the old 50.0 neutral point).
`RotationBranchTuning.neutral_passer_score`/`passer_tilt_anchor_low/_high`
are re-centered on the measured real distribution (70.0 neutral, anchors
at +-15 with exactly-reciprocal multipliers) and `beats/rotation.py::
_passer_tilt_multiplier` now interpolates in log space, not linear, so the
reciprocal-anchor property actually reproduces 1.0 at neutral (see
`RotationBranchTuning`'s own docstring for the full three-part fix and its
measured residual). A real change to sim output at a fixed seed and fixed
roster on the same beat Commit 3 already changed -- every ball-handler
action whose Help beat fires. Committed ahead of the still-unlanded
Commit 4 (M1, the minutes curve), whose own uncommitted working-tree draft
targets this same 16 -> 17 transition for a different change; whoever
finalizes Commit 4 must re-target 17 -> 18 and re-bump this constant to 18
at that time, not reuse 17 -- see HANDOFF.md's own forward-fix entry for
the full account.
17 -> 18: docs/design/per-36-spread-compression.md Commit 4 (M1) --
`game.py::_MINUTES_CURVE` ranks 1-5 re-anchored 33.2/32.0/30.9/29.8/28.7 ->
35.5/32.4/30.5/28.8/27.4 (sum 154.6 unchanged). The curve lives in
`game.py`, not this module, but it feeds every game's minutes allocation
and so moves every possession at a fixed seed and fixed roster -- exactly
what `engine_version` exists to flag. Golden box scores and the OT-seed
pins are regenerated in the same commit.
18 -> 19: docs/design/pick-and-pop.md PP-15 C3 -- `possession.py` wires
`popper_defender`/`is_pop_shot`, so the screener's beat-4 branch splits
into a roll leg and a pop leg by his own catch-and-shoot score
(`RotationBranchTuning.pop_shooting_weights`/`pop_share_anchor_*`,
`TuningProfile.rotation_pop`), and a pop resolves as a MID/THREE catch shot
at the popper's own live matchup defender. Wider than PD-2's and comparable
to PD-10's: no neutral carve-out and no directive gate -- every `pnr_ball`
or `dho` possession that reaches a `HELP_COMMITTED`/`HELP_LATE` read
re-realizes from that beat onward, in every league at every seed (PP-11).
The pop share composes downstream of the passer tilt (17), so the total
screener-branch mass is unchanged and only what the screener does with it
moves. Built against 17 and rebased onto Commit 4 (18) before landing, so it
takes 19; golden box scores and the OT-seed pins are regenerated in the same
commit.
19 -> 20: docs/design/pick-and-pop.md PP-15 C4 -- PP-13.5's first lever:
`RotationBranchTuning.pop_share_anchor_high`'s share 0.75 -> 0.50, because the
un-calibrated C3 wiring measured 35/38 in band at `validate_sim --seasons 3
--rng-seed 101` (baseline 36/38; new failure `blk_per36_max`). Re-realizes
every elite-shooting screener's pop-vs-roll draw; goldens and OT-seed pins are
regenerated in the same commit.
20 -> 21: docs/design/turnover-recovery.md TR-4.1/TR-4.10 -- the resolution
beat's `turnover_live` leaf mass splits into `turnover_live` and a new
`loose_ball_recovered` leaf by an ability contest between the two lineups'
loose-ball scores (`TuningProfile.loose_ball`, `beats/loose_ball.py`). A
recovery charges no turnover and no steal and continues the possession as
the fourth member of Decision P1-11's shared continuation budget. Every
game in every league re-realizes (no neutral or opt-in carve-out); taken
relative to 20, per TR-4.10's "next free value" rule.
21 -> 22: docs/design/turnover-recovery.md TR-4.7 (revised 2026-09-24, user
decision) -- the recovering player is a `weighted_choice` draw over the
offense's five, weighted by `loose_ball_contribution`, instead of the argmax.
One extra `rng` draw per drawn recovery and none otherwise, so every game
that contains a recovery re-realizes from its first one on (goldens and the
overtime-seed pins are regenerated in the same commit). The recovery itself
still credits no stat, and the path at recovery probability 0.0 is
byte-identical.
22 -> 23: docs/design/turnover-recovery.md R12 -- `LooseBallTuning.
recovery_concentration_exponent` (2.4, the user's starting value) raises each
candidate's weight to a power in `beats/loose_ball.py::draw_recoverer`, so a
game's recovery draws land on different players and every game containing a
recovery re-realizes. Draw count is unchanged (one per recovery), and the path
at recovery probability 0.0 is still byte-identical.
23 -> 24: docs/design/block-attribution-v2.md BA-2.1/BA-2.2/BA-2.4 (Commit 1)
-- the weak-side rim-help contest is no longer run against the floor's single
best rim protector: every eligible defender (the lineup minus the shot's
primary and on-ball defenders) contributes `share_i * p_i`, shares from
`ResolutionRimHelpTuning.attribution_concentration_exponent`/`concentration_
shift`, and the blocker is drawn only when `blocked_miss_help` is. The cached
`rim_protection_contribution` now reads `resolution_rim_help.defense_weights`
(one vector for share and contest), which also re-names
`RoleSlot.RIM_PROTECTOR` on some floors. One extra `rng.random()` per drawn
help block; every other leaf consumes the draws it did before.
24 -> 25: docs/design/block-attribution-v2.md BA-2.3/BA-2.4 (Commit 2) --
`resolution_rim_help` calibrated: the shot-blocking vector (`rim_protection
.50 / wingspan_in .25 / vertical .15 / rotation_speed .10`, `help_iq` out)
replaces the help-commit vector, `scale` 45 -> 12, `base_rate` 0.10 ->
0.055. Solved on TODAY's real-player seeds and synthetic generator; a
ratings change that spreads the top of `rim_protection` (the 2K / athleticism
work) must re-solve `scale`/`base_rate` (BA-2.9 rule 3).
25 -> 26: docs/design/block-attribution-v2.md (Correction, 2026-09-24) --
`resolution_rim_help.scale` 12 -> 13 (`base_rate` stays 0.055). At 12 the
gated real-2025 `blk_per36_p95_rotation` (ceiling 2.4) failed on 4 of 5
seeds (101/1/2/7/202); at 13 it passes all five, for one synthetic miss the
design excuses in advance (seed 2's top-10 ratio, a league with no standout
big: BA-7 risk 4)."""

# A beat's ability vector: rating-field-name -> weight. Never sums to
# exactly 1.0 by construction -- `probability.ability_score` normalizes by
# the vector's own weight sum, so callers can add/remove a field without
# re-balancing every other weight by hand.
#
# B4-2 fix (critic-proven): `WeightVector` used to be a plain type alias
# for `Mapping[str, float]` -- indistinguishable at runtime from any other
# `Mapping[str, float]`-annotated field (e.g. `SetupTuning.
# action_type_base_weight` below), which meant Decision P1-16's guard test
# (`tests/unit/sim/test_tuning.py`) could only tell the two apart by the
# *spelling* of the type annotation (`"WeightVector"` vs `"Mapping[str,
# float]"`), and critic proved that's trivially evadable: annotate a new
# tendency-carrying ability vector `Mapping[str, float]` instead of
# `WeightVector` and the guard never sees it. `WeightVector` is now a real
# class (still `Mapping[str, float]`-compatible via the `collections.abc`
# mixin methods, so every existing caller -- `probability.ability_score`'s
# `.values()`/`.items()`, etc. -- keeps working unchanged), constructed
# exclusively through `_wv()` below. The guard test now checks `isinstance
# (value, WeightVector)` against each field's actual runtime value instead
# of its annotation string, so detection survives any annotation spelling
# a future author reaches for -- it only depends on which constructor
# built the value, and `_wv()` is the one constructor every real ability
# vector in this file goes through.
class WeightVector(Mapping[str, float]):
    """Frozen `Mapping[str, float]` with no meaning beyond "constructed by
    `_wv()`" -- the runtime marker Decision P1-16's guard test introspects
    for. Wraps a `MappingProxyType` rather than subclassing it directly
    (CPython's `mappingproxy` cannot be subclassed)."""

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, float]) -> None:
        self._data: MappingProxyType[str, float] = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> float:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"WeightVector({dict(self._data)!r})"


def _wv(values: Mapping[str, float]) -> WeightVector:
    """Freeze a literal weight-vector dict so `TuningProfile` instances
    (and their nested dataclasses) are genuinely immutable, not just
    reassignment-blocked -- a plain `dict` default would let a caller
    mutate a supposedly-frozen tuning profile's weights in place. The one
    and only place a `WeightVector` gets constructed (see the B4-2 comment
    above) -- that's what makes the guard test's runtime-type check
    correct rather than incidental."""
    return WeightVector(values)


# ---------------------------------------------------------------------------
# Decision P1-13 -- physical anchor table (wingspan_in / height_in -> 0-99)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorPoint:
    inches: float
    rating: float


@dataclass(frozen=True)
class PhysicalAnchorTable:
    """Sorted ascending by `inches`. `composites.normalize_physical`
    linearly interpolates between consecutive points and clamps outside
    the table's own range, so this is a genuine percentile-style lookup,
    not a formula that can run away past the endpoints."""

    points: tuple[AnchorPoint, ...]

    def __post_init__(self) -> None:
        inches = [p.inches for p in self.points]
        if inches != sorted(inches):
            raise ValueError("PhysicalAnchorTable.points must be sorted ascending by inches")


@dataclass(frozen=True)
class PhysicalAnchors:
    height: PhysicalAnchorTable
    wingspan: PhysicalAnchorTable


def _default_height_anchors() -> PhysicalAnchorTable:
    # Reference points span replacement-level guard to a true outlier
    # center, in listed inches. Documented placeholders, swept like any
    # other constant here -- there is no canonical published percentile
    # table for NBA height, so these are chosen to span the realistic
    # range rather than derived from a citation.
    return PhysicalAnchorTable(
        points=(
            AnchorPoint(69.0, 1.0),
            AnchorPoint(72.0, 15.0),
            AnchorPoint(78.0, 50.0),
            AnchorPoint(83.0, 90.0),
            AnchorPoint(87.0, 99.0),
        )
    )


def _default_wingspan_anchors() -> PhysicalAnchorTable:
    return PhysicalAnchorTable(
        points=(
            AnchorPoint(68.0, 1.0),
            AnchorPoint(72.0, 15.0),
            AnchorPoint(81.0, 50.0),
            AnchorPoint(88.0, 90.0),
            AnchorPoint(93.0, 99.0),
        )
    )


# ---------------------------------------------------------------------------
# Decision P1-6 / P1-6a -- spacing + scheme-pressure composite weights
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositeTuning:
    """Per-player contribution-term weights (cached at `SimPlayer`
    construction, Decision P1-4/P1-6a) plus the possession-level scaling
    the aggregate applies after summing those cached terms."""

    spacing_weights: WeightVector = field(
        default_factory=lambda: _wv({"catch_shoot_3": 0.6, "relocation": 0.4})
    )
    scheme_weights: WeightVector = field(
        default_factory=lambda: _wv({"scheme_discipline": 0.55, "help_iq": 0.45})
    )
    # team_strategy.help_aggression (tendency-domain, 0-100) linearly maps
    # onto this multiplier range and scales the scheme-pressure aggregate
    # (§4.4's Help/Rotation-roll-tag Modifiers column).
    scheme_pressure_help_aggression_lo: float = 0.7
    scheme_pressure_help_aggression_hi: float = 1.3


# ---------------------------------------------------------------------------
# Decision P1-7/P1-8 -- setup beat: action-type selection + role assignment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupTuning:
    """Action-type selection weights, per Decision P1-8: "the ball-handler
    is chosen by weighted selection ... using pnr_ball_freq / iso_freq /
    transition_freq (matched to the action type) scaled by
    player_roles.touch_share where a row exists and by overall_cached
    where it does not." `action_type_base_weight` is the per-lineup base
    rate each action type starts from before the on-court tendency
    aggregate tilts it; `min_overall_scale` floors the overall_cached
    fallback so a very low-rated player is de-emphasized, never zeroed.
    """

    action_type_base_weight: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(
            {
                "pnr_ball": 0.28,
                "dho": 0.08,
                "iso": 0.12,
                "post_up": 0.08,
                "off_ball_screen": 0.12,
                "transition": 0.14,
                "spot_up": 0.18,
            }
        )
    )
    # Interpretive call (flagged in the build report): `player_tendencies`
    # (domain/ratings.py) has no `dho_freq` column -- DHO is treated as a
    # ball-screen-adjacent action sharing `pnr_ball_freq` as its driving
    # tendency, scaled down by this share.
    dho_share_of_pnr: float = 0.35
    min_overall_scale: float = 0.05
    # B3 fix (critic-proven): `player_roles.touch_share` is a real
    # fraction (~0.10-0.35 for a rotation player, `NUMERIC(5,4)`); the
    # no-role-row fallback used to be `overall_cached / 99.0` (~0.35-1.0),
    # a completely different scale that made roleless bench players
    # systematically out-draw a real starter's genuine `touch_share` for
    # ball-handling/role weight. These two anchor points linearly map
    # `overall_cached` onto `touch_share`'s own numeric range instead --
    # a replacement-level player (~45 overall) lands near the bottom of a
    # real bench player's touch share, a star (~90 overall) near the top
    # of a real high-usage starter's. No published table exists for "what
    # touch share would a player with no role row plausibly carry," so
    # this is a documented interpretive call, not a derived constant --
    # calibrated to be in the right ballpark, not exact.
    overall_fallback_anchor_low: tuple[float, float] = (45.0, 0.12)
    overall_fallback_anchor_high: tuple[float, float] = (90.0, 0.32)

    # Assist-compression fix (season-level analysis, this dispatch):
    # `touch_share`/`overall_cached` (above) scale a ball-handler's *usage*
    # volume, but nothing in this beat's `BALL_HANDLER` weight ever read
    # `passing_vision`/`passing_accuracy` before this fix -- two players
    # with identical `touch_share` and identical `pnr_ball_freq`/
    # `iso_freq`/`transition_freq` drew the exact same BALL_HANDLER weight
    # regardless of whether one of them was a genuinely elite passer
    # (confirmed directly: `_role_weight` returned byte-identical values
    # for a 99/99-passing and a 50/50-passing player pre-fix). Measured
    # effect on a synthetic 30-team/14-deep league: the best passer's
    # season apg over a 200-game sample rose from ~6.1 to ~6.7 apg with
    # this fix at these anchors -- real, in the right direction, but still
    # short of a real 7-13 apg career peak for that passing caliber. The
    # residual gap traced to two mechanisms this fix deliberately does not
    # touch (out of this dispatch's setup/role-assignment scope): (1)
    # `rotation_branch`'s kickout/roll-vs-no_pass split is a fixed rate,
    # not scaled by the passer's own ability, so how often a `HELP_
    # COMMITTED`/`HELP_LATE` read even attempts a pass is unaffected by
    # this fix; (2) the shooter's own finishing on the resulting kickout/
    # roll shot (assist only counts on a make) is likewise untouched.
    # Flagged for a future dispatch, not chased here.
    #
    # `ball_handler_passing_ability_anchor_low/_high` add a second,
    # independent multiplier on top of the existing touch_share/overall
    # scale -- an *ability*-driven concentration effect layered onto the
    # existing *tendency*-driven (`pnr_ball_freq`/`iso_freq`/
    # `transition_freq`) and *usage*-driven (`touch_share`) terms, matching
    # this dataclass's own established pattern of interpolating between two
    # named anchor points rather than a single guessed constant. The score
    # being interpolated is `0.6 * passing_vision + 0.4 * passing_accuracy`
    # (roughly `derivation.py`'s own `pass_freq` formula weights for these
    # two fields, 0.40/0.30, renormalized to sum to 1 -- an interpretive
    # call, not a value named anywhere else, since no other formula in this
    # codebase combines exactly these two fields into one score). The low
    # anchor (65, a solidly-above-average but not elite passer) floors the
    # multiplier at 1.0 -- a merely decent-passing high-usage ball handler
    # is not penalized, only a genuinely elite one (the high anchor, 98,
    # near this rating domain's own ceiling) is boosted, up to 2.2x. Both
    # anchors sit well above `overall_fallback_anchor_high`'s 90.0 because
    # a first pass at (50, 1.0)/(90, 1.5) measured almost no separation
    # between "very good" and "truly elite" passers on a real generated
    # roster (multiple teammates commonly cluster in the high-80s/90s on
    # this score too, since ability fields are not anti-correlated with
    # each other at roster-generation time) -- widening both anchors
    # outward is what actually produced the measured ~6.1 -> ~6.7 apg
    # shift above. Also fires on ISO and TRANSITION (both in `_BALL_
    # HANDLER_TENDENCY_FIELD`), not just the PNR_BALL/DHO pathway the apg
    # finding traced the compression to -- undisclosed at first pass, named
    # here now: an elite passer draws more isolation/transition ball-
    # handler reps too, which is more shot volume/scoring opportunity for
    # that player, not just more assist opportunity. Not chased further
    # this dispatch (the apg finding is specifically about assists), but a
    # real, measurable side effect a future scoring-distribution finding
    # should know this multiplier also touches.
    #
    # Deliberately scoped to `RoleSlot.BALL_HANDLER` only (drive/PNR/iso/
    # transition, the ball-dominant actions `_BALL_HANDLER_ACTIONS` names)
    # -- `PASSER`/`ENTRY_PASSER` (the no-ball-handler actions' passer role)
    # are left unchanged, since those roles already draw on `pass_freq`,
    # which is itself partly generated from `passing_vision`/`passing_
    # accuracy` at roster-generation time (`derivation.py`'s `pass_freq`
    # formula, weights 0.40/0.30); adding this same ability term there too
    # would double-count that correlation for spot-up/post-up/off-ball-
    # screen assists instead of fixing the ball-handler-driven pathway the
    # season-level finding actually traced the compression to. The same
    # concern applies, undisclosed until now, to `pnr_ball_freq` itself
    # (used for PNR_BALL/DHO, half of `_BALL_HANDLER_TENDENCY_FIELD`) --
    # `derivation.py`'s own `pnr_ball_freq` formula also carries a
    # `passing_vision` term (weight 0.20), a smaller share of that
    # tendency's formula than `pass_freq`'s 0.40 `passing_vision` weight,
    # but a real, non-zero one all the same. Left as-is rather than
    # excluded like `PASSER`/`ENTRY_PASSER`: `pnr_ball_freq`'s weight on
    # `passing_vision` (0.20) is roughly a third of `pass_freq`'s (0.40),
    # and PNR_BALL/DHO are only 2 of the 4 action types this multiplier
    # reaches (ISO/TRANSITION's driving tendencies carry no `passing_
    # vision` term at all) -- the double-count this multiplier reintroduces
    # for PNR_BALL/DHO specifically is real but proportionally smaller than
    # the one `PASSER`/`ENTRY_PASSER`'s exclusion was written to avoid, and
    # excluding 2 of this multiplier's 4 action types outright would blunt
    # the apg fix on exactly the action types (PNR_BALL is the single
    # highest-weighted action type in `action_type_base_weight` above) that
    # most need it. Not re-measured separately from the season-level apg
    # finding above, which already reflects whatever share of the ~6.1 ->
    # ~6.7 shift this partial double-count contributes.
    #
    # Usage-dampening fix (critic-proven regression, this dispatch): the
    # raw multiplier above applied identically regardless of how much the
    # ball this player actually sees, which meant a no-role/low-`touch_
    # share` bench player's merely-good (not elite) passing score could
    # pull a bigger multiplier than a real high-usage starter's -- on a
    # real generated 420-player league (seed 1, `--players-per-team 14`),
    # this inverted 537/2630 intra-team BALL_HANDLER weight orderings
    # against `overall_cached`, worst case a 62-overall player outweighing
    # an 82-overall teammate, and on the fixture below (`tests/unit/sim/
    # test_setup.py`'s star-vs-bench test, un-pinned realistic passing
    # ratings), an 85-overall real-touch_share starter's weight (14.0) fell
    # below two roleless bench players' combined weight (17.13) -- both
    # exactly the touch_share/overall-vs-no-role-fallback hierarchy
    # `_overall_to_touch_share_estimate`'s own fix (above) exists to
    # protect, reopened from a new axis. `beats/setup.py`'s `_ball_handler_
    # passing_concentration` now multiplies this raw anchor lookup by a
    # `usage_ratio ** ball_handler_passing_usage_dampening_power` term,
    # where `usage_ratio` is this player's own `overall_cached`-estimated
    # usage (`_overall_to_touch_share_estimate` -- **RC-20**, `docs/design/
    # auto-gm-role-construction.md`: a real `player_roles.touch_share` row
    # is no longer read here either, for the same reason `_role_weight`'s
    # `scale` no longer reads one -- see that function's own comment)
    # against `overall_fallback_anchor_high`'s own
    # share (0.32, reused rather than a new possibly-drifting constant --
    # that anchor already documents 0.32 as "a real high-usage starter's"
    # bar) clamped to 1.0, so a genuine high-usage starter gets this
    # multiplier at full strength while a bench-level usage scale gets a
    # steeply reduced share of it. The cube (power 3, not left at the
    # naive linear power 1) is an empirical choice, not a derived one --
    # power 1 left the fixture's bench-pair weight at 14.525 (still above
    # the 14.0 starter), power 2 at 13.441 (holds, 4% margin), power 3 at
    # 12.989 (holds, 7.8% margin) and also brought the real-league flip
    # count from 537/2630 down to 485/2630 with a worst gap of 16 -- at or
    # below the 502/2630-flips/16-worst-gap this same real league shows
    # with the multiplier removed entirely, i.e. the multiplier's own
    # marginal contribution to the regression is fully absorbed, not just
    # reduced. Re-measured the apg finding after dampening (same
    # methodology, a full synthetic season instead of the original's
    # 200-game sample): the best-passer apg effect is within noise of the
    # undamped version (a real generated league's best passer already
    # carries a usage scale at or near the 0.32 bar, so dampening barely
    # touches the player this fix is actually for -- it targets the no-
    # role/low-usage players the un-dampened version let ride the same
    # multiplier for free). Confirmed via `tools/validate_sim.py` (1
    # season, rng_seed=1): 26/26 metrics stayed in band before and after,
    # including `ast_per_made_fg` -- see the As-built note this dispatch
    # added below for the exact before/after numbers (correcting VERIFIED-
    # BY's earlier "was 4153 passed, 1 failed pre-fix" claim, which
    # `be827c0^` shows was actually 4112 unit tests, not a real pre-fix
    # baseline of 4153/4154).
    ball_handler_passing_ability_anchor_low: tuple[float, float] = (65.0, 1.0)
    ball_handler_passing_ability_anchor_high: tuple[float, float] = (98.0, 2.2)
    ball_handler_passing_usage_dampening_power: float = 3.0
    # PD-5 (docs/design/gm-command-surface.md): `player.directive.
    # usage_mode`'s multiplicative effect on `_role_weight`'s `base` term,
    # keyed on the closed `UsageMode` enum and, within each value, on the
    # plain role-**family** name strings `beats/setup.py::_ROLE_FAMILY`
    # maps every offensive `RoleSlot` onto -- never `playbook.RoleSlot`
    # itself, which this module may not import (module docstring: "domain
    # and stdlib only"). Multiplies `base`, not `scale` -- a legibility
    # choice, not a behavioural one (PD-5's own proof: the two operands
    # are bit-identical up to float-ulp reassociation, since multiplication
    # commutes). This is what makes `usage_mode` observably different
    # from `touch_share`: `scale` (built from `overall_cached`, unconditionally
    # -- **RC-20**, `docs/design/auto-gm-role-construction.md`) is one
    # number per player, identical for every role; this table gives one
    # player **five different numbers**, selected by role family, each
    # independently tunable per `UsageMode` -- `usage_mode` can move which
    # slot a player is drawn into across EVERY role family. **`touch_share`
    # cannot do this for ANY role-family pair, full stop, including
    # BALL_HANDLER** -- corrected in RC-20 from this comment's own prior,
    # narrower claim (which was true before RC-20 and is named here so a
    # future reader does not wonder why it changed):
    # `beats/setup.py::_ball_handler_passing_concentration` used to multiply
    # the `BALL_HANDLER` role's `base` by a second, real-`touch_share_f`-
    # derived dampening term, which genuinely could shift the `BALL_
    # HANDLER`-vs-everything-else balance once a player's passing score
    # cleared `ball_handler_passing_ability_anchor_low` (65.0) (critic round
    # after `4bc4778`: measured a 0% to 102%+ BALL_HANDLER:CUTTER weight-
    # ratio move between touch_share 0.10 and 0.35 at high passing ratings).
    # RC-20 deletes that reader -- the dampening term's `usage_ratio` is now
    # `_overall_to_touch_share_estimate(overall_cached)` unconditionally,
    # the same value for a role-carrying player and a byte-identical
    # roleless one, so `touch_share` no longer reaches role selection at
    # all; `player_roles.touch_share` has exactly one reader left,
    # `game.py::_minutes_shares`. Pinned total over `UsageMode` at
    # import by `beats/setup.py::_require_usage_mode_family_multipliers_
    # is_total`.
    usage_mode_family_multipliers: Mapping[UsageMode, Mapping[str, float]] = field(
        default_factory=lambda: MappingProxyType(
            {
                UsageMode.BALANCED: MappingProxyType(
                    {
                        "on_ball": 1.0,
                        "screening": 1.0,
                        "spacing": 1.0,
                        "post": 1.0,
                        "movement": 1.0,
                    }
                ),
                UsageMode.INITIATE: MappingProxyType(
                    {
                        "on_ball": 1.3,
                        "screening": 1.0,
                        "spacing": 0.75,
                        "post": 1.0,
                        "movement": 1.0,
                    }
                ),
                UsageMode.OFF_BALL: MappingProxyType(
                    {
                        "on_ball": 0.75,
                        "screening": 1.0,
                        "spacing": 1.25,
                        "post": 1.0,
                        "movement": 1.25,
                    }
                ),
                UsageMode.SCREEN_AND_ROLL: MappingProxyType(
                    {
                        "on_ball": 1.0,
                        "screening": 1.3,
                        "spacing": 1.0,
                        "post": 1.0,
                        "movement": 1.0,
                    }
                ),
                UsageMode.POST_UP: MappingProxyType(
                    {
                        "on_ball": 1.0,
                        "screening": 1.0,
                        "spacing": 1.0,
                        "post": 1.3,
                        "movement": 1.0,
                    }
                ),
            }
        )
    )

    # PD-10 (docs/design/gm-command-surface.md): `team_strategy.
    # offensive_scheme`'s multiplicative tilt on `_action_type_weights`
    # (`beats/setup.py`) -- blend, not replace (PD-10.2): this multiplies
    # the existing tendency-driven weight rather than overriding it, the
    # same shape `shot_diet_zone_multipliers`/`usage_mode_family_
    # multipliers` above already use at their own `weighted_choice` seams.
    # Keyed on the closed `OffensiveScheme` enum and, within each value, on
    # the plain action-type value strings `action_type_base_weight` above
    # already uses -- never `playbook.ActionType` itself, which this module
    # may not import (module docstring: domain and stdlib only).
    #
    # Named `..._multipliers`, not `..._shares`: `..._shares` is
    # `ScreenTuning.defense_outcome_shares`'s word and means "a normalized
    # distribution that IS the answer" -- this table is a set of factors on
    # somebody else's weights, not a replacement distribution.
    #
    # Hand-picked and basketball-grounded, not derived (the same standing
    # `defense_outcome_shares`/`shot_diet_zone_multipliers` carry): no
    # published table exists for "how much more often does a PNR-heavy team
    # run a ball screen," so these are calibrated to be plausible and to
    # produce a visible per-scheme identity (PD-10.7's measurement), not
    # swept from data. Per-row reasoning (PD-10.3):
    # - `pnr_heavy`: `pnr_ball`/`dho` up (a handoff is a ball screen with
    #   the pass built in); `post_up` down hardest; `spot_up` NOT boosted
    #   (PNR-generated kick-outs are already modelled inside the PNR chain,
    #   `beats/rotation.py`'s kick-out branch -- boosting `spot_up` too
    #   would double-count the same basketball event); `transition` held at
    #   1.00 (tempo has its own knob, PD-10.5). Deliberately the mildest
    #   signature multiplier in the table: `pnr_heavy` carries the highest
    #   prior in `strategy_service._OFFENSE_SCHEMES` and wins the argmax
    #   for most teams, so its row has the most league-wide leverage over
    #   `validate_sim`'s aggregate bands -- strength here costs the most.
    # - `motion`: `off_ball_screen` is the signature; `iso` gets the
    #   table's lowest non-`post_up` cell (motion is the anti-isolation
    #   scheme); `spot_up`/`dho` up (continuous movement generates
    #   catch-and-shoot looks and handoffs).
    # - `iso_star`: `iso`'s base weight is only 0.12, so a recognisable
    #   identity needs the table's largest-but-one multiplier;
    #   `off_ball_screen` drops hardest (the four non-stars are spacing,
    #   not screening).
    # - `post_up`: `shot_freq_post` is low league-wide and the base weight
    #   is the joint-smallest (0.08), so this needs the table's single
    #   largest cell to be visible at all; `transition 0.85` is the one
    #   place a scheme touches a tempo-adjacent action type (a post-up
    #   offense definitionally walks the pace down), still inside the
    #   0.85-1.10 band PD-10.5 holds every scheme's `transition` cell to.
    # - `five_out`: `post_up 0.35` is the table's floor -- a five-out team
    #   has no post presence by construction -- strictly positive per the
    #   rule below, reusing `fewer_midrange`'s own "down hard" value rather
    #   than re-guessing. `spot_up 1.90` is the signature, chosen from a
    #   direct cell-sensitivity sweep against real 2025 player data (an
    #   earlier `1.45` measured only a 1.26-1.28x relative lift on real
    #   rosters, short of acceptance criterion 6d's 1.5x bar -- `spot_up`
    #   already holds the highest base-weight share of any action type
    #   before any scheme is applied, so a modest multiplier here barely
    #   moves its already-large normalized share). A `five_out` team is
    #   also, by construction, a high-`three_point_bias` team
    #   (`strategy_service._OFFENSE_SCHEMES[FIVE_OUT]`'s argmax score reads
    #   the same roster three-point features `three_point_bias`'s own
    #   derivation does) -- PD-10 and PD-2 compound on exactly those teams,
    #   which is basketball-correct but moves the league-wide three-point
    #   share by more than this table alone suggests.
    #
    # Every cell strictly positive (the identical RNG-desync reason PD-2
    # disclosure 3 gives: `weighted_choice`'s `total <= 0` branch consumes
    # the generator differently from the normal path, so an all-zero
    # weight vector desyncs every subsequent draw in that seeded game --
    # reachable here since a lineup whose five players all have
    # `shot_freq_post == 0` already contributes 0.0 to `post_up` from the
    # tendency term alone). Pinned total (every `OffensiveScheme` member,
    # every `ActionType` value derived from the enum itself, never a
    # hardcoded list) and strictly positive at import by
    # `beats/setup.py::_require_offensive_scheme_action_multipliers_are_
    # total_and_positive` (lives beside the beat that consumes it, not
    # here, for the identical layering reason `shot_diet_zone_multipliers`'
    # own pin does -- this module may import only `domain` and stdlib, and
    # the pin needs `ActionType`).
    offensive_scheme_action_multipliers: Mapping[OffensiveScheme, Mapping[str, float]] = field(
        default_factory=lambda: MappingProxyType(
            {
                OffensiveScheme.PNR_HEAVY: MappingProxyType(
                    {
                        "pnr_ball": 1.35,
                        "dho": 1.35,
                        "iso": 0.85,
                        "post_up": 0.70,
                        "off_ball_screen": 0.85,
                        "transition": 1.00,
                        "spot_up": 0.90,
                    }
                ),
                OffensiveScheme.MOTION: MappingProxyType(
                    {
                        "pnr_ball": 0.85,
                        "dho": 1.25,
                        "iso": 0.55,
                        "post_up": 0.85,
                        "off_ball_screen": 1.70,
                        "transition": 1.10,
                        "spot_up": 1.25,
                    }
                ),
                OffensiveScheme.ISO_STAR: MappingProxyType(
                    {
                        "pnr_ball": 0.85,
                        "dho": 0.70,
                        "iso": 2.10,
                        "post_up": 0.90,
                        "off_ball_screen": 0.60,
                        "transition": 1.00,
                        "spot_up": 0.90,
                    }
                ),
                OffensiveScheme.POST_UP: MappingProxyType(
                    {
                        "pnr_ball": 0.80,
                        "dho": 0.90,
                        "iso": 0.90,
                        "post_up": 2.20,
                        "off_ball_screen": 1.00,
                        "transition": 0.85,
                        "spot_up": 1.00,
                    }
                ),
                OffensiveScheme.FIVE_OUT: MappingProxyType(
                    {
                        "pnr_ball": 1.10,
                        "dho": 1.10,
                        "iso": 0.90,
                        "post_up": 0.35,
                        "off_ball_screen": 1.05,
                        "transition": 1.05,
                        "spot_up": 1.90,
                    }
                ),
            }
        )
    )


# ---------------------------------------------------------------------------
# Decision P1-12 -- the two-outcome ("binary") beat shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BinaryBeatTuning:
    """The plain two-branch instance of Decision P1-12's universal
    contract: offense ability vector vs defense ability vector -> one
    `probability.contest_probability` draw. Beats with more than two
    outcomes (screen, help) get their own richer tuning dataclass below;
    this shape is reused as-is for beats not yet built (rotation,
    resolution, rebound) so §4.4's full table has a home in this file even
    though only steps 1-3's beats read it this dispatch."""

    offense_weights: WeightVector
    defense_weights: WeightVector
    base_rate: float
    scale: float
    band: tuple[float, float]


@dataclass(frozen=True)
class ResolutionRimHelpTuning:
    """BDA-4 (docs/design/block-defender-attribution.md): the weak-side
    rim-help contest folded into resolution beat 5's single leaf draw as a
    new `blocked_miss_help` leaf -- a second, help-side block contest.
    docs/design/block-attribution-v2.md BA-2.1 (superseding BDA-4.1's
    single-candidate `max`): the contest runs over the *pool* of eligible
    helpers -- the defense lineup minus the shot's primary contesting
    defender and the on-ball defender attached to the possession's
    ball-handler (BDA-4.4's gates 3 and 4) -- as `p_help_block = sum_i
    share_i * p_i`, each helper contesting on his own cached `rim_
    protection_contribution` (which reads `defense_weights` below, BA-2.4:
    one vector for who rotates and whether he blocks). The blocker is drawn
    from `{i: share_i * p_i}` only when the leaf is. `composites.best_rim_
    protector` no longer feeds this contest; it only names
    `RoleSlot.RIM_PROTECTOR` for narration.

    Not a plain `BinaryBeatTuning`: no `offense_weights` field, and two
    extra scalars. The offense side of this contest reuses `resolution_
    block.offense_weights` *live* (BDA-4.5's own cited precedent --
    `resolution.py`'s `_select_contest_weights`/`_CONTEST_QUALITY_SOURCE`
    already reuse a sibling tuning entry's vector the same way for the
    rim/mid shot-make contest) rather than declaring a near-duplicate
    field, so there is no `offense_weights` here to keep in sync. `defense_
    weights` below is its own literal field instead -- §4.4's Help row
    vector (`HelpTuning.defense_weights` above happens to already
    materialize the identical 5 fields for beat 3's help-commit gate, but
    this is a genuinely different contest -- the rim-help *block* draw
    inside beat 5, not the help-commit gate -- so it gets its own
    declared vector, the same "duplicate the literal, don't cross-
    reference" precedent `resolution_shot.three_contest_weights` already
    uses for `rotation_kickout.defense_weights`)."""

    defense_weights: WeightVector
    base_rate: float
    scale: float
    band: tuple[float, float]
    # team_strategy.help_aggression (tendency-domain, 0-100) linearly maps
    # onto this multiplier range and scales the realized `p_help_block`,
    # same shape (`composites.aggression_linear_scale`) `CompositeTuning.
    # scheme_pressure_help_aggression_lo/_hi` already uses for the
    # scheme-pressure aggregate.
    aggression_lo: float
    aggression_hi: float
    # docs/design/block-attribution-v2.md BA-2.1/BA-2.2: who rotates to the
    # rim. Each eligible helper's proximity share is `max(c - shift, 1) **
    # exponent` over the pool's sum, `c` his cached `rim_protection_
    # contribution` (x `composites.COMMITTED_HELPER_PENALTY` if he is the
    # possession's committed helper). Centring on the rating midpoint is what
    # lets a moderate exponent say "guards essentially never take weak-side
    # blocks": at 4 / 50 a lone 90 big takes ~95% of the floor's share against
    # three 55 guards and a 70 forward, and two bigs at 90/82 split ~68/32.
    # The rebounding beat's raw `ability ** 2.4` transplanted here measured
    # wings taking 40% of help blocks (BA-2.2's dilution row). As the exponent
    # grows the share collapses onto the single best helper, i.e. the pre-v2
    # winner-take-all rule is the limit case (a unit test, not a mode).
    attribution_concentration_exponent: float
    concentration_shift: float


# ---------------------------------------------------------------------------
# Screen beat (2a) -- 5 outcomes, scheme-dependent defense-side split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenTuning:
    """Offense-favoring outcome is BLOWN alone (the screen defender loses
    the matchup outright); the other four (OVER, UNDER, SWITCH,
    HEDGE_RECOVER) are all defense-favoring "the defense contained this
    the way it intended to" outcomes, split among themselves by
    `defense_outcome_shares`, keyed on the possession's live
    `defensive_scheme` (§4.4's Modifiers column) rather than fixed --
    that's what lets a `switch_all` team produce switches and a `drop`
    team produce unders on the same underlying contest probability."""

    offense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {"screen_setting": 0.4, "screen_timing": 0.35, "strength": 0.25}
        )
    )
    defense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {
                "screen_navigation": 0.4,
                "lateral_quickness": 0.25,
                "switchability": 0.15,
                "scheme_discipline": 0.2,
            }
        )
    )
    base_rate_blown: float = 0.12
    # Step 11 retune: 35 -> 55 -> 77 -> 104 -> 130 (4 passes), see
    # `PossessionTuning`'s team_win_total_sd note -- a chain-wide
    # flattening pass across every skill-sensitive contest (this beat
    # through resolution), not just the shot itself. Each earlier pass
    # moved team_win_total_sd closer but not inside band (18.75 -> 18.23 ->
    # 16.83 -> 15.15 -> 14.19 mean, but with an 8-sample cross-seed spread
    # of ~[11.2, 15.0] -- the sample-to-sample sd of this metric itself is
    # ~1.4, so a mean of ~13.2 still lets an unlucky seed clip the ceiling,
    # as measured).
    #
    # B25-3 fix (critic-round-25-proven): that final pass (130.0) retained
    # only 26.7% of this beat's pre-flatten discrimination at a *realistic*
    # 30-point rating gap (critic's measurement; `tests/unit/sim/test_
    # discrimination_floor.py` reproduces it) -- every surviving unit test
    # used an 85-point gap with a bare `>` assertion, which cannot detect a
    # 73% flattening. This beat is a `CONTAINED`/`DEFENDER_WINS`-branch
    # gate, not a per-possession-guaranteed contest (screen only fires for
    # `pnr_ball`/`dho`, and only once per chain cycle unless the offense
    # loops back), so it is one of the *targeted* pullbacks (see
    # `PossessionTuning`'s B25-3 note for the full targeted-vs-uniform
    # rationale): restored to 70.0, retaining ~50% of pre-flatten
    # discrimination -- re-verified against `team_win_total_sd` after every
    # beat's pullback landed, see the round-25 build report.
    scale: float = 70.0
    band: tuple[float, float] = (0.02, 0.5)
    defense_outcome_shares: Mapping[DefensiveScheme, Mapping[str, float]] = field(
        default_factory=lambda: MappingProxyType(
            {
                DefensiveScheme.DROP: MappingProxyType(
                    {"over": 0.15, "under": 0.6, "switch": 0.05, "hedge_recover": 0.2}
                ),
                DefensiveScheme.SWITCH_ALL: MappingProxyType(
                    {"over": 0.05, "under": 0.05, "switch": 0.85, "hedge_recover": 0.05}
                ),
                DefensiveScheme.HEDGE: MappingProxyType(
                    {"over": 0.1, "under": 0.1, "switch": 0.1, "hedge_recover": 0.7}
                ),
                DefensiveScheme.ICE: MappingProxyType(
                    {"over": 0.3, "under": 0.55, "switch": 0.05, "hedge_recover": 0.1}
                ),
                DefensiveScheme.ZONE_MIX: MappingProxyType(
                    {"over": 0.25, "under": 0.25, "switch": 0.25, "hedge_recover": 0.25}
                ),
            }
        )
    )


# ---------------------------------------------------------------------------
# On-ball beat (2b) -- 3 outcomes, one offense-favoring / two defense-favoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnBallTuning:
    offense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {
                "handle": 0.25,
                "first_step": 0.25,
                "change_of_direction": 0.2,
                "ball_security": 0.15,
                "screen_use": 0.15,
            }
        )
    )
    defense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {
                "on_ball_defense": 0.35,
                "lateral_quickness": 0.25,
                "hands": 0.15,
                "strength": 0.1,
                "foul_discipline": 0.15,
            }
        )
    )
    base_rate_advantage: float = 0.45
    # Step 11 retune: 40 -> 63 -> 88 -> 119 -> 149 (4 passes), same
    # chain-wide flattening -- SUPERSEDED, B25-3 fix below.
    #
    # B25-3 fix (critic-round-25-proven): 149.0 retained only 27.1% of
    # pre-flatten discrimination at a realistic 30-point gap (an elite
    # ball-handler vs. a poor one won this read 47.5% vs 42.5%, critic
    # measured -- basketball-meaningless). This beat is the *sometimes*
    # half of the chain (runs only for the 4 ball-handler action types, at
    # most a few times per possession via the shared continuation budget),
    # not the every-possession-guaranteed resolution contests -- one of the
    # targeted pullbacks (see `PossessionTuning`'s B25-3 note). Restored to
    # 81.0, retaining ~50% of pre-flatten discrimination.
    scale: float = 81.0
    band: tuple[float, float] = (0.1, 0.85)
    # Of the defense-favorable remainder (1 - P(ADVANTAGE)), the share that
    # goes to DEFENDER_WINS outright rather than a merely CONTAINED read.
    defender_wins_share: float = 0.25
    # §4.4's Modifiers column: screen result shifts the diff before the
    # logistic (a BLOWN screen hands the offense a bonus; a well-navigated
    # OVER/UNDER/HEDGE_RECOVER trims it), keyed by `screen.ScreenOutcome`
    # value strings so this file never has to import `beats/screen.py`.
    screen_outcome_diff_bonus: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(
            {
                "blown": 12.0,
                "switch": 0.0,
                "over": -4.0,
                "under": -6.0,
                "hedge_recover": -8.0,
            }
        )
    )
    spacing_diff_weight: float = 6.0
    fatigue_diff_weight: float = 8.0


# ---------------------------------------------------------------------------
# Help beat (3) -- gated: whether to help at all, then commit-quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HelpTuning:
    """§4.4's Help row: the ability vector never contains
    `scheme_discipline`/`help_aggression`/`help_gamble_freq` (those are all
    Modifiers, per the table) -- they only gate whether the helper
    engages at all (`gate_*`) and, for the one allowlisted tendency
    (`help_gamble_freq`), tilt the post-gate commit-quality contest
    (`quality_help_gamble_penalty`) so a gambler who does help is more
    likely to be late, not just more likely to help in the first place."""

    offense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {
                "finishing_rim": 0.35,
                "contact_finishing": 0.25,
                "floater": 0.2,
                "draw_foul": 0.2,
            }
        )
    )
    defense_weights: WeightVector = field(
        default_factory=lambda: _wv(
            {
                "rotation_speed": 0.3,
                "help_iq": 0.25,
                "rim_protection": 0.25,
                "vertical": 0.1,
                "wingspan_in": 0.1,
            }
        )
    )
    gate_base_rate: float = 0.55
    # Step 11 retune: 60 -> 94 -> 132 -> 178 -> 223 /
    # 40 -> 63 -> 88 -> 119 -> 149 (4 passes), same chain-wide flattening
    # pass -- SUPERSEDED, B25-3 fix below.
    #
    # B25-3 fix (critic-round-25-proven): 223.0 retained only 27.0% of
    # pre-flatten discrimination at a realistic 30-point gap. The help gate
    # only fires on the `ADVANTAGE` branch of an already-conditional on-ball
    # beat -- one of the targeted pullbacks (see `PossessionTuning`'s
    # B25-3 note). Restored to 121.0, retaining ~50%.
    gate_scale: float = 121.0
    gate_band: tuple[float, float] = (0.05, 0.95)
    gate_scheme_discipline_weight: float = 0.5
    gate_help_aggression_weight: float = 0.35
    gate_help_gamble_weight: float = 0.3

    quality_base_rate: float = 0.55
    # B25-3 fix (critic-round-25-proven): same rationale as `gate_scale`
    # above -- 149.0 retained 27.1%; restored to 81.0 (~50% retained).
    quality_scale: float = 81.0
    quality_band: tuple[float, float] = (0.05, 0.95)
    # Subtracted from the offense/defense diff per tendency-domain point of
    # `help_gamble_freq` above a neutral midpoint (50) -- a maxed-out
    # gambler (100) at the midpoint's default is a fixed penalty, never
    # scaled by anything else, keeping the mechanism a single documented
    # number instead of a second hidden curve.
    quality_help_gamble_penalty_per_point: float = 0.12
    # B4 fix (critic-proven): §4.6 "foul trouble ... reduces the fouler's
    # help-commit probability" was declared in `state.py`
    # (`PlayerGameState.is_in_foul_trouble`) but never actually read by
    # this beat. Applied the same way `quality_help_gamble_penalty_per_point`
    # already penalizes the helper's side of the quality contest (a fixed,
    # non-scaled subtraction) -- a foul-trouble helper who does engage is
    # modeled as more tentative/out of position, shifting mass from
    # `HELP_COMMITTED` toward `HELP_LATE`, not toward `HELP_STAYED` (the
    # gate itself, governed by `scheme_discipline`/`help_aggression`/
    # `help_gamble_freq`, is untouched -- foul trouble doesn't make a
    # player less willing to help, it makes them worse at it when they do).
    foul_trouble_help_commit_penalty: float = 8.0
    # PD-4 (docs/design/gm-command-surface.md): `helper.directive.
    # defense_mode`'s fixed additive offset on `gate_diff`
    # (`beats/help.py::_gate_probability`) -- a NEW site (2.4/PD-4's own
    # honest framing: the arithmetic SHAPE has a real precedent one line
    # away in `resolve_help_beat`'s quality contest, but applying it to
    # the GATE rather than the quality score is deliberate, not reused).
    # `BALANCED` is the neutral member (zero offset, today's behaviour).
    # Pinned total over `DefenseMode` at import by `beats/help.py::
    # _require_defense_mode_gate_offset_is_total` -- lives beside the beat
    # that consumes it for the same layering reason `shot_diet_zone_
    # multipliers`'s own pin does (this module may import only `domain`
    # and stdlib).
    defense_mode_gate_offset: Mapping[DefenseMode, float] = field(
        default_factory=lambda: MappingProxyType(
            {DefenseMode.BALANCED: 0.0, DefenseMode.STAY_HOME: -10.0, DefenseMode.HELP_EARLY: 10.0}
        )
    )


# ---------------------------------------------------------------------------
# Beats not built this dispatch (steps 4-5) -- data only, per §4.4's table,
# so tuning.py does not need to be revisited when those beats are built.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneShotTuning:
    """`probability.contest_probability`'s 3 scalar inputs, one instance
    per zone (plus one for the post-shape special case) -- §4.4's
    Resolution(shot) row names the ability vectors but, unlike every
    other beat this file already covers, step 1-3 left no base_rate/
    scale/band anywhere for it. Added here in step 4 per this dispatch's
    "only add new scalar constants ... if the doc calls for them and
    they're missing" instruction: the doc's own §4.6 rebound bullet and
    §10's "FG% by zone" validation metric both presuppose a per-zone make
    probability exists to tune, so this is a gap-fill, not a new beat
    shape."""

    base_rate: float
    scale: float
    band: tuple[float, float]


@dataclass(frozen=True)
class ResolutionShotTuning:
    """Zone -> {catch, dribble} rating-field selection (`pullup_vs_catch`
    picks which one a resolution beat reads, per §4.4's Modifiers column),
    plus the per-zone shot tuning `select_shot_zone` draws against.

    **B3 fix (critic-proven): zone selection (rim/mid/three) lives in
    `beats/resolution.py::select_shot_zone`, per §3's module-layout comment
    and §12 step 4's own outline text ("resolution (zone selection,
    contest, make/miss...)").** An earlier version of this dispatch moved
    `select_shot_zone` into `playbook.py` specifically because it reads
    `shot_freq_rim`/`shot_freq_mid`/`shot_freq_three`, which were not on
    Decision P1-16's tendency allowlist at the time, and `playbook.py` sits
    outside `beats/`, so the file-scoped mechanical guard
    (`test_tuning.py::test_resolution_beats_only_read_allowlisted_
    tendency_fields`) never saw the read at all -- critic proved this was
    an evasion of the guard's *reach*, not a legitimate selection-vs-
    resolution distinction (the field is picked once per shot attempt
    inside the same beat that then contests it, not reused across an
    entire possession's role assignment the way `select_action_type`'s
    choice is). The real fix is amending what the guard's *allowlist*
    permits, not where the code that trips it lives: `shot_freq_rim`/
    `shot_freq_mid`/`shot_freq_three` are now 3 more allowlisted names
    (phase-1-simulation-engine.md §4.4/§4.7, Decision P1-16 amendment) --
    the same *kind* of thing `pullup_vs_catch` already permits on this
    exact row (a tendency field steering a branch/selection, never
    entering an ability-score vector) -- and `select_shot_zone` moved back
    into `beats/resolution.py`, where the guard's file-scoped exclusion
    (`setup.py` only) now correctly sees and permits its 3 reads."""

    shot_field_by_zone: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: MappingProxyType(
            {
                "rim": MappingProxyType({"catch": "finishing_rim", "dribble": "finishing_rim"}),
                "mid": MappingProxyType(
                    {"catch": "catch_shoot_mid", "dribble": "off_dribble_mid"}
                ),
                "three": MappingProxyType({"catch": "catch_shoot_3", "dribble": "off_dribble_3"}),
            }
        )
    )
    zone_tuning: Mapping[str, ZoneShotTuning] = field(
        default_factory=lambda: MappingProxyType(
            {
                # Step 11 retune: scale raised 30 -> 36 (rim) / 32 -> 38
                # (mid) alongside the resolution_block/turnover/foul/rebound
                # scale bumps below -- see `PossessionTuning`'s
                # `team_win_total_sd` note for why (flattening every
                # shot/turnover/rebound contest's sensitivity to a skill
                # gap is the tuning-only lever available to compress
                # team-level win-total spread toward the reference target
                # without a structural engine change). Base rates are
                # unchanged here -- rim/mid FG% were already inside band at
                # the old scale, and this flattening moves both slightly
                # toward their own base_rate, not away from it. SUPERSEDED
                # for `scale`, B25-3 fix below.
                #
                # B25-3 fix (critic-round-25-proven): 106.0/113.0 retained
                # only 28.7% of pre-flatten discrimination at a realistic
                # 30-point gap (rim FG%, top-quartile vs. bottom-quartile
                # finishers, measured 65.30% vs 64.93% at season level --
                # 0.4pp, vs. a real-NBA-plausible ~20pp spread). Unlike the
                # conditional beats above, rim/mid shot resolution runs on
                # (effectively) every possession, so it is deliberately
                # NOT pulled all the way back to the ~50% target the
                # conditional beats use (see `PossessionTuning`'s B25-3
                # note on the targeted-vs-uniform tradeoff) -- restored to
                # a more modest 76.0/81.0 (~40% retention), re-verified
                # against `team_win_total_sd` after every beat's pullback
                # landed (see the round-25 build report).
                "rim": ZoneShotTuning(base_rate=0.62, scale=76.0, band=(0.3, 0.85)),
                "mid": ZoneShotTuning(base_rate=0.40, scale=81.0, band=(0.15, 0.65)),
                # B25-2 fix (critic-round-25-proven): step 11's `base_rate=
                # 0.274` was a compensating constant for a real wiring bug,
                # not a basketball-derived value -- `resolve_resolution_beat`
                # used to contest every zone's shot (including three) with
                # `resolution_block.defense_weights` (rim_protection/
                # vertical/wingspan_in/lateral_quickness), a rim-contest
                # skill set that is a *sign-inverted* proxy for perimeter
                # defense: critic measured a drop-coverage rim anchor with
                # zero `closeout_speed` allowing 18.4% from three while a
                # true perimeter stopper allowed 47.0%, a 28.7-point
                # inversion, with 0.274 only working because it happened to
                # cancel a ~13.6-point *average* offense-favoring diff at
                # league-average skill, not because any individual
                # matchup's probability was correct. The real fix (B25-2):
                # the three-point contest now reads `three_contest_weights`
                # below (`closeout_speed`/`closeout_control`, the same
                # perimeter-defense pair `rotation_kickout` already uses for
                # exactly this kind of contest -- see `beats/resolution.py`)
                # instead of the block/rim weight vector. `scale` is solved
                # the same way B25-3's rim/mid pullback was (~40% retention
                # relative to the shared pre-any-flatten baseline of 32,
                # which `three` never actually shipped at -- this is its
                # first real discrimination-aware solve, not a pullback),
                # landing at the same 81.0 `mid` also lands at (same
                # base_rate/band shape). `base_rate` is set to the
                # `fg_pct_three` reference value (0.366) on the reasoning
                # that at a perfectly average shooter vs. average defender,
                # `contest_probability` returns `base_rate` itself (diff ==
                # 0) -- **correction (critic round 26, B26-2): that
                # reasoning was never checked against a real season, and
                # doesn't hold end-to-end.** The realized league-wide
                # `fg_pct_three` actually lands at 0.406-0.414 across seeds
                # (further from 0.366 than round 18's bug-compensating
                # 0.274 base_rate produced, which realized 0.3765) -- the
                # zero-diff base case isn't the same as the population
                # average once real skill-diff distributions and `scale`
                # interact. It still lands inside `tools/validate_sim.py`'s
                # `fg_pct_three` band (confirmed 26/26 across 9 seeds,
                # critic round 26), so this is not a re-tune trigger by
                # itself -- but treat 0.366 as an unverified starting
                # guess, not a solved value, if this constant is revisited.
                "three": ZoneShotTuning(base_rate=0.366, scale=81.0, band=(0.12, 0.60)),
            }
        )
    )
    # B25-2 fix: the perimeter-defense skill set a three-point contest
    # should actually read, per the design doc's Rotation(kick-out) row
    # (`docs/design/phase-1-simulation-engine.md` §4.4) -- the only place
    # §4.4's table names a defender's ability to close out and contest a
    # jump shot from distance. Reused verbatim (same weights, same two
    # fields) rather than declared as a near-duplicate vector, matching
    # this file's own precedent for the rim/mid zones reusing
    # `resolution_block.defense_weights`: contesting a catch-and-shoot
    # three off a kick-out and contesting any other three are the same
    # underlying defensive skill (closing out fast and under control),
    # not two different basketball actions that happen to share a name.
    three_contest_weights: WeightVector = field(
        default_factory=lambda: _wv({"closeout_speed": 0.55, "closeout_control": 0.45})
    )
    # §4.4's Resolution(shot) row: "post_scoring for post shapes" -- a
    # third, zone-independent offense-vector case alongside the
    # catch/dribble split above, so `post_up` actions get their own
    # base_rate/scale/band rather than sharing "mid"'s.
    post_shot: ZoneShotTuning = field(
        default_factory=lambda: ZoneShotTuning(base_rate=0.46, scale=30.0, band=(0.2, 0.7))
    )
    # §4.4's Resolution(block) row Modifiers: "zone (rim >> mid >> three
    # base rate)" -- `resolution_block.base_rate` below is the rim-zone
    # anchor; every other zone's block base rate is that anchor scaled by
    # this multiplier (interpretive call: no published block-rate-by-zone
    # table exists, so this is calibrated to the same "spans the
    # realistic range" standard `tuning.py`'s physical anchor tables use).
    block_zone_multiplier: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(
            {"rim": 1.0, "mid": 0.35, "three": 0.08, "post": 0.55}
        )
    )
    # docs/design/gm-command-surface.md PD-2: `team_strategy.three_point_
    # bias` (0-100, neutral 50) linearly interpolated onto `[lo, hi]` for
    # the THREE-zone shot-selection weight, mirroring `ReboundModifierTuning
    # .crash_oreb_bias_lo/_hi`'s exact shape (`beats/rebound.py::
    # _crash_bias_scale`) -- same modifier pattern, different tuning source.
    # **Copies that field's own exact values on purpose, not a coincidence
    # of independently-chosen numbers**: PD-2's own acceptance criterion 3a
    # requires the neutral (50) scale to be exactly 1.0 in IEEE754, and
    # round 2's critic swept 999 symmetric `lo`/`hi` pairs and found 124
    # that do NOT evaluate to exactly 1.0 at the interpolation's midpoint --
    # `0.7 + 0.5 * (1.3 - 0.7)` is one of the pairs that does, verified, not
    # assumed (`_require_neutral_three_point_bias_scale_is_exactly_one`
    # below is the`raise`-backed pin, not a comment asserting it). The
    # MID-zone scale is this same value's exact reciprocal
    # (`beats/resolution.py::_three_point_bias_scale`) rather than an
    # independently-tuned second pair, specifically so a single verified-
    # exact-1.0 pin covers both directions at once (the reciprocal of an
    # exact 1.0 is exactly 1.0 too, bit for bit).
    three_point_bias_lo: float = 0.7
    three_point_bias_hi: float = 1.3
    # PD-3: `shooter.directive.shot_diet`'s multiplicative effect on the
    # same three zone weights `three_point_bias` scales, keyed on the
    # closed `ShotDiet` enum (`dba2/domain/role.py`) and, within each
    # value, on the plain zone-name strings `zone_tuning`/`shot_field_by_
    # zone` already use above -- never `sim.events.Zone` itself, which
    # this module may not import (module docstring: "domain and stdlib
    # only"). **Every cell strictly positive** (PD-2 disclosure 3): a zero
    # multiplier on a shooter whose OTHER two zone tendencies are also zero
    # would drive `weighted_choice`'s total to exactly zero, diverting it
    # into its `rng.choice` fallback branch (`probability.py:114-116`) and
    # desyncing every subsequent draw in that seeded game -- `fewer_
    # midrange`'s "MID down hard" is the smallest multiplier in the table
    # and is still not zero. Pinned total (every `ShotDiet` member, every
    # zone name) and strictly-positive at import by
    # `beats/resolution.py::_require_shot_diet_zone_multipliers_are_total_
    # and_positive` (lives beside the beat that consumes it, not here,
    # for the identical layering reason `three_point_bias`'s own neutral
    # pin does -- this module may import only `domain` and stdlib, and the
    # pin needs `Zone`/`weighted_choice` semantics from `sim/`).
    shot_diet_zone_multipliers: Mapping[ShotDiet, Mapping[str, float]] = field(
        default_factory=lambda: MappingProxyType(
            {
                ShotDiet.BALANCED: MappingProxyType({"rim": 1.0, "mid": 1.0, "three": 1.0}),
                ShotDiet.MORE_THREES: MappingProxyType({"rim": 1.0, "mid": 0.7, "three": 1.4}),
                ShotDiet.ATTACK_THE_RIM: MappingProxyType({"rim": 1.4, "mid": 1.0, "three": 0.7}),
                ShotDiet.FEWER_MIDRANGE: MappingProxyType(
                    {"rim": 1.15, "mid": 0.35, "three": 1.15}
                ),
            }
        )
    )


# ---------------------------------------------------------------------------
# Step 4 additions -- resolution's turnover/foul flavor splits, rebound's
# non-ability modifiers, and the possession-chain's own scalar constants.
# None of these are named as their own §4.4 table row (the table gives
# each row's *ability vectors*, not the branch shares that decide which
# terminal-eligible flavor a beat resolves to); every default below is
# this dispatch's documented interpretive call, flagged in the build
# report, calibrated to be plausible rather than derived from a citation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreeThrowTuning:
    """§4.6: "each is an independent draw against free_throw with a small
    fatigue modifier." No defender is involved in a free throw, so this
    doesn't need an ability vector at all -- `possession.py` reads
    `free_throw` directly via `probability.rating_value` and compares it
    against a fixed neutral 50.0 "defense" score, reusing
    `contest_probability`'s existing shape rather than inventing a
    defender-less variant of it."""

    base_rate: float = 0.75
    scale: float = 45.0
    band: tuple[float, float] = (0.4, 0.95)


@dataclass(frozen=True)
class RotationBranchTuning:
    """Beat 4 (`beats/rotation.py`) has 3 top-level branches (kickout /
    screener / no-pass) before any of §4.4's three named rotation contests
    (`rotation_kickout`/`rotation_roll_tag`/`rotation_pop`) ever runs -- the table gives
    each contest's ability vectors but names no branch-selection weights
    anywhere, so these are fixed base shares (this dispatch's own
    documented interpretive call), the same "constant, not ability-
    scored" shape `ScreenTuning.defense_outcome_shares` already uses for
    an analogous branch-selection problem.

    **PP-1a fix (docs/design/pick-and-pop.md): `roll_base_weight` only
    applies when the action type has a screener in its role template AND
    offers a roll -- today that is `pnr_ball` only.** This docstring used to
    name `off_ball_screen` as well, which is false: `off_ball_screen` is not
    in `possession.py`'s `_BALL_HANDLER_ACTIONS` and never reaches beat 4 at
    all (it resolves straight from setup to resolution) -- there is no
    beat-4 read to reach for it. `beats/rotation.py` renormalizes over
    whichever branches are actually reachable.

    **`pop_shooting_weights`/`pop_share_anchor_low`/`pop_share_anchor_high`
    (docs/design/pick-and-pop.md PP-5/PP-7) govern the *screener* branch's
    internal roll-vs-pop split, not a fourth top-level branch**:
    `roll_base_weight`'s mass (after the passer tilt below has scaled it --
    the pop split composes strictly downstream of that tilt, PP-17.2) is
    what a pop and a roll compete for, split ability-weighted by the
    screener's own catch-and-shoot score (`beats/rotation.py::_pop_share`).
    The total "screener gets the ball" share never changes, only what he
    does with it (PP-3 Option A, mean-preserving at the branch level).
    Reachable for `pnr_ball` (roll and pop) and `dho` (pop only -- its
    `handoff_big` has no roll leg, PP-6.1) once `possession.py` wires
    `popper_defender`.

    `neutral_passer_score`/`passer_tilt_anchor_low`/`passer_tilt_anchor_high`
    (docs/design/per-36-spread-compression.md SC-3.5/SC-5.3 Option A1/SC-7
    Commit 3): this table's own original docstring (above) and `tuning.py`'s
    former `SetupTuning` comment (still true of `_role_weight`'s ball-
    handler-selection draw) both flagged the same residual: "how often a
    `HELP_COMMITTED`/`HELP_LATE` read even attempts a pass is unaffected by
    [the assist-compression fix], since `rotation_branch`'s kickout/roll-
    vs-no_pass split is a fixed rate, not scaled by the passer's own
    ability." This closes that flag. `beats/rotation.py::
    resolve_rotation_beat` multiplies `kickout_base_weight`/
    `roll_base_weight` by a passer-ability multiplier and divides
    `no_pass_base_weight` by the same multiplier, **before**
    `branch_total` is computed (Nit 6: applying it before renormalization,
    not after, gives it equal strength on `pnr_ball` (roll available, 0.800
    base pass rate) and on `dho`/`iso`/`transition` (no roll, 0.692) --
    applying it after renormalization would not, since the two action
    shapes start from different branch totals).

    The multiplier is a clamped **log-linear** (geometric) interpolation
    between the two anchors over the identical
    `dba2.sim.composites.passer_score` (`0.6*passing_vision + 0.4*
    passing_accuracy` -- SC-7 Commit 3 step 2: imported from
    `composites.py`, not restated, so this tilt and `beats/setup.py`'s
    concentration multiplier can never drift apart on what "passer
    ability" means).

    **Corrected by a forward-fix (docs/design/per-36-spread-compression.md
    SC-7 Commit 3, post-critic-review): the original commit's "mean-
    preserving by construction" claim here was false, on three independent
    grounds.** (1) The mechanism this class feeds is multiplicative
    (`kickout *= tilt`, `roll *= tilt`, `no_pass /= tilt` in
    `beats/rotation.py`), so the correct symmetry condition is an **odds-
    space** one -- `lo_mult * hi_mult == 1.0` -- not the additive symmetry
    (`0.75`/`1.25`, each 0.25 from 1.0) the original anchors actually had:
    `0.75 * 1.25 = 0.9375`, not 1. (2) Even a genuinely odds-symmetric pair
    is not exactly mean-preserving for a *population* of passers, because
    the induced pass-rate is concave in the multiplier (Jensen's
    inequality) -- a symmetric multiplier applied to a symmetric score
    distribution is mean-*reducing*, not neutral. (3) The real distribution
    was never symmetric around the old `neutral_passer_score` (50.0) in the
    first place: measured on `generate_league(league_id=1, season=2026,
    rng_seed=1, players_per_team=14)` (420 players), `passer_score` has
    mean 69.90, median 69.30, and 48.6% of the league clamped at the old
    high anchor (>= 70) -- the mechanism was discriminating among barely
    more than half the league and doing nothing for the rest.

    **The fix, in three parts, each independently verified (see
    `tests/unit/sim/test_tuning.py`/`test_rotation.py` and the per-36 doc's
    As-built note for live-measurement numbers):**

    1. **Re-centered on the real distribution.** `neutral_passer_score` is
       69.9's nearest clean value, **70.0** -- a real measured mean (not
       this rating domain's generic midpoint, which is what 50.0 actually
       was) -- so the interpolation's flat point sits where the league's
       mass actually is, not off in its lower tail.
    2. **Odds-space-symmetric anchors, reduced spread.** `passer_tilt_
       anchor_low`/`_high` are `(55.0, 1/1.15)`/`(85.0, 1.15)`: the
       multipliers are exact reciprocals (`lo_mult * hi_mult == 1.0`,
       enforced by `__post_init__` below), and the score distance is
       reduced from the original's 20 points either side to 15 -- per
       `per-36-spread-compression.md` SC-8.3's own pre-registered rule for
       this exact failure mode ("if the tilt is not truly mean-preserving
       -> reduce the anchor spread"), triggered by the original's measured
       `ast_per_made_fg` overshoot (+1.9-2.1%).
    3. **Log-linear, not linear, interpolation.** `beats/rotation.py::
       _passer_tilt_multiplier` now interpolates `log(multiplier)` linearly
       in score, not the multiplier itself. This is what makes odds-space
       symmetry actually load-bearing: interpolating raw multipliers
       linearly, even with reciprocal anchors, does not generally cross
       1.0 at the exact score midpoint; log-linear interpolation does, by
       construction, for any reciprocal pair -- so a passer scoring exactly
       `neutral_passer_score` still reproduces today's exact 0.45/0.35/0.20
       split to float equality (unchanged guarantee, now provable for a
       non-additively-symmetric anchor pair too).

    **What this does and does not claim.** The three fixes above make the
    mechanism odds-space symmetric by construction and centered on the
    real population, which collapses the old bias from "the whole league
    sits on one side of neutral, using non-reciprocal multipliers" (the
    dominant, first-order effect that produced the +1.9-2.1%
    `ast_per_made_fg` overshoot) down to the much smaller, second-order
    Jensen residual: measured mass-weighted expected pass rate under the
    real distribution above moves the aggregate pass rate by **-0.28%**
    from the untilted baseline (0.7409 -> 0.7388), roughly an order of
    magnitude smaller than the original commit's overshoot and well inside
    the `ast_per_made_fg` band's remaining headroom. This is disclosed as a
    quantified residual, not claimed away as exactly zero -- "mean-
    preserving by construction" was the original commit's error, and this
    docstring does not repeat it. The clamp problem is also reduced, not
    eliminated: 9.3%/10.2% of the same league now clamp at the low/high
    anchor respectively (19.5% total, split both directions), against the
    original's 0%/48.6% (all on one side)."""

    kickout_base_weight: float = 0.45
    roll_base_weight: float = 0.35
    no_pass_base_weight: float = 0.2
    neutral_passer_score: float = 70.0
    passer_tilt_anchor_low: tuple[float, float] = (55.0, 1.0 / 1.15)
    passer_tilt_anchor_high: tuple[float, float] = (85.0, 1.15)
    # PP-5: the selection gate reads exactly the two fields the pop's own
    # resolution will read (`ResolutionShotTuning.shot_field_by_zone`'s
    # "three"/"mid" catch columns are `catch_shoot_3`/`catch_shoot_mid`) --
    # "the read the offense makes is the read the shot then tests," not a
    # coincidence to be tidied away later. `off_dribble_3` is deliberately
    # excluded: a pop is a catch (PP-8.3), and rewarding a rating the
    # resulting shot can never read would repeat the category error B25-2
    # fixed for the three-point contest reading `rim_protection`.
    pop_shooting_weights: WeightVector = field(
        default_factory=lambda: _wv({"catch_shoot_3": 0.70, "catch_shoot_mid": 0.30})
    )
    # PP-7: a two-anchor linear interpolation of `pop_shooting_weights`'
    # score onto a roll-vs-pop share `s in [0, 1]`, the same idiom
    # `beats/setup.py`'s `_overall_to_touch_share_estimate`/
    # `_ball_handler_passing_concentration` already use twice. Hand-picked
    # and basketball-grounded, not derived (no published table exists for
    # "what fraction of a stretch five's short-roll touches are pops"),
    # calibrated to be plausible and visible rather than solved -- (45, 0.05):
    # even a replacement-level shooting big pops occasionally (not 0.0, or
    # the feature becomes a step function at the anchor); (85, 0.50): an
    # elite stretch five pops on 1 of 2 screener touches but still rolls
    # sometimes (not 1.0, or `roll_ability` becomes unreadable for exactly
    # the players -- e.g. a Porzingis-shaped one -- who most need both).
    # **The design's first-draft ceiling was 0.75 (PP-7); PP-15 C4's
    # calibration lowered it to 0.50** -- lever 1 of PP-13.5's fixed order --
    # after the un-calibrated wiring measured 35/38 in band (baseline 36/38)
    # at `validate_sim --seasons 3 --rng-seed 101`. Measured there (in-band /
    # blk_per36_max): 0.75 -> 35 / 2.7186, 0.60 -> 35 / 2.8207, 0.50 -> 36 /
    # 2.8420, 0.45 -> 36 / 2.8741, 0.40 -> 35 / 2.8270 (floor 2.835). THAT
    # SEED-101 SWEEP DOES NOT GENERALIZE: over seeds 101/7/202 the lever has no
    # measurable relationship with blk_per36_max, the feature costs ~0.06 at
    # every ceiling, and 0.50 was chosen by judgement (it passes seed 101 by
    # luck). See pick-and-pop.md "Correction (2026-09-24)". No value change.
    # PP-9.3's pin (`beats/rotation.py`) enforces `_low[0] < _high[0]` and
    # both shares in `[0.0, 1.0]` at import.
    pop_share_anchor_low: tuple[float, float] = (45.0, 0.05)
    pop_share_anchor_high: tuple[float, float] = (85.0, 0.50)

    def __post_init__(self) -> None:
        """Enforce the three properties the class docstring's fix claims,
        so a future edit to any of these five fields cannot silently
        reintroduce the original commit's bug (a false claim with no test
        catching it was exactly the finding that triggered this fix)."""
        lo_score, lo_mult = self.passer_tilt_anchor_low
        hi_score, hi_mult = self.passer_tilt_anchor_high
        if not (lo_score < self.neutral_passer_score < hi_score):
            raise ValueError(
                "RotationBranchTuning: neutral_passer_score "
                f"({self.neutral_passer_score}) must lie strictly between "
                f"passer_tilt_anchor_low's score ({lo_score}) and "
                f"passer_tilt_anchor_high's score ({hi_score})"
            )
        midpoint = (lo_score + hi_score) / 2.0
        if abs(self.neutral_passer_score - midpoint) > 1e-9:
            raise ValueError(
                "RotationBranchTuning: neutral_passer_score "
                f"({self.neutral_passer_score}) must equal the midpoint of "
                f"the two anchor scores ({midpoint}) -- the log-linear "
                "interpolation in beats/rotation.py only returns exactly "
                "1.0 at neutral_passer_score when this holds"
            )
        if lo_mult <= 0.0 or hi_mult <= 0.0:
            raise ValueError(
                "RotationBranchTuning: passer_tilt_anchor_low/_high "
                f"multipliers must be positive (got {lo_mult}, {hi_mult}) "
                "-- beats/rotation.py divides no_pass_base_weight by the "
                "interpolated multiplier, and a non-positive multiplier "
                "at either anchor can produce a zero or negative divisor"
            )
        if abs(lo_mult * hi_mult - 1.0) > 1e-9:
            raise ValueError(
                "RotationBranchTuning: passer_tilt_anchor_low/_high "
                f"multipliers ({lo_mult}, {hi_mult}) must be exact "
                "reciprocals (lo_mult * hi_mult == 1.0) -- the tilt "
                "mechanism is multiplicative (kickout/roll *= tilt, "
                "no_pass /= tilt), so odds-space symmetry, not additive "
                "symmetry around 1.0, is the condition that keeps it "
                "mean-preserving at the anchors themselves"
            )


@dataclass(frozen=True)
class TurnoverSplitTuning:
    """Given `resolution_turnover`'s contest draws "a turnover happens,"
    which flavor it is. `steal_attribution`'s ability vector and the
    defender's `steal_gamble_freq`† both nudge the live-steal share,
    keeping both §4.4 rows load-bearing rather than the WeightVector
    going unused."""

    live_steal_base_share: float = 0.55
    offensive_foul_share: float = 0.08
    steal_ability_share_scale: float = 0.003
    steal_gamble_share_scale: float = 0.003


@dataclass(frozen=True)
class LooseBallTuning:
    """docs/design/turnover-recovery.md TR-4.4: when a resolution beat draws
    a live-ball turnover, the offense can win its own loose ball back. The
    offense's share of those loose balls is a real `contest_probability`
    between the two lineups' mean loose-ball scores -- one symmetric vector
    for both sides, because both teams are doing the identical thing (unlike
    the rebound beat's boxing-out vs crashing split), which also makes
    `base_rate` readable as "the league-average offensive recovery share."

    `weights`: hustle (`motor`), securing a bobbling ball (`hands`) and
    anticipation (`basketball_iq`) -- all three plain `PlayerRatings`
    columns. No tendency is read and none is proposed: `PlayerTendencies`
    has no hustle propensity, and adding one is a migration plus a Decision
    P1-16 allowlist amendment (the same reason §4.4 gives blocks no
    `block_freq`).

    `base_rate` 0.28: below 0.5 on purpose -- a deflected pass or a stripped
    dribble usually travels toward the defender who caused it. Pre-registered
    on basketball grounds and **not** solved against any `tov_per_100_poss`
    target (TR-10.3); it re-opens only if the band fails.

    `scale` 70.0: copied from the rebound contest, which B25-3 set at ~50%
    retention for beats that fire occasionally rather than every possession.
    `band` (0.10, 0.50): the offense never gets a coin-flip-or-better claim
    on a ball it just lost, and never gets locked to zero.

    `scramble_seconds` 2.0: the shot-clock cost of the scramble itself, a
    scalar (not a range) like `continuation_setup_seconds`, so the feature
    consumes no RNG draw anywhere (TR-4.8). The shot clock does not reset
    on a recovery (TR-4.6): no change of possession, no shot hit the rim.

    `recovery_concentration_exponent` 2.4 (R12, user decision 2026-09-24:
    "start with 2.4 and see what it does"): `draw_recoverer` weights each
    offensive player by `max(loose_ball_contribution, 0) ** exponent`.
    1.0 is plain proportional weights (the c3efb8b behavior); larger values
    concentrate recoveries on the hustlers. Mirrors
    `ReboundModifierTuning.attribution_concentration_exponent`, whose 2.4 it
    copies. Must be positive (checked at construction); not solved against
    any target."""

    weights: WeightVector = field(
        default_factory=lambda: _wv({"motor": 0.40, "hands": 0.35, "basketball_iq": 0.25})
    )
    base_rate: float = 0.28
    scale: float = 70.0
    band: tuple[float, float] = (0.10, 0.50)
    scramble_seconds: float = 2.0
    recovery_concentration_exponent: float = 2.4

    def __post_init__(self) -> None:
        if not self.recovery_concentration_exponent > 0.0:
            raise ValueError(
                "LooseBallTuning: recovery_concentration_exponent must be "
                f"positive, got {self.recovery_concentration_exponent}"
            )


@dataclass(frozen=True)
class FoulSplitTuning:
    """Given `resolution_foul`'s contest draws "a foul happens," which
    terminal-eligible flavor it is (shooting / non-shooting / offensive),
    and, on a make, whether it's an and-one. `foul_freq_diff_weight`
    implements §4.4's "foul_freq† on the defender" Modifier by
    shifting the underlying contest's diff before the logistic, the same
    mechanism `on_ball.py`'s `screen_outcome_diff_bonus` uses."""

    shooting_share: float = 0.65
    offensive_share: float = 0.05
    and_one_share_of_shooting_foul_rate: float = 0.12
    foul_freq_diff_weight: float = 0.15


@dataclass(frozen=True)
class ReboundModifierTuning:
    """§4.4's Rebound row Modifiers: `crash_oreb_freq`† and
    `team_strategy.crash_oreb_bias` scale the offensive side;
    "box-out position implied by the possession's matchup pairing ... (a
    committed low-man is out of position) scale the defensive side" --
    `out_of_position_defender_penalty` is that scale, applied to whichever
    defender `possession.py` names as out of position (the beat 3 helper
    on a HELP_COMMITTED/HELP_LATE path), never a rating-vector term.

    `attribution_concentration_exponent`
    (docs/design/per-36-spread-compression.md SC-3.1/SC-5.2/SC-7 Commit 2):
    `beats/rebound.py::resolve_rebound_beat` raises each candidate's
    `ability_score` to this power before the within-side `weighted_choice`
    draw that names one specific rebounder -- the only `weighted_choice`
    call in `dba2/sim/` drawing over a raw `ability_score` (SC-3.1), whose
    implicit proportional-to-zero model dilutes every share toward uniform
    because the rating scale's floor is ~30-40, not 0. Pre-registered at
    2.4 (SC-5.2 finding c: equalizes SD-fit gamma=2.24 and max-fit
    gamma=2.95's relative errors, nudged up for small-sample SD inflation),
    confirmed by a future dispatch's full-season sweep (SC-8.1 measurement
    6), not this commit's blocker. Ordering is load-bearing: the exponent
    applies to `ability_score` ONLY; `_crash_scale`/`out_of_position_
    defender_penalty` multiply the exponentiated value afterward,
    unchanged -- folding the penalty inside would silently turn 0.7x into
    0.7^2.4 ~= 0.41x. No degenerate-case guard needed: every `ability_score`
    here is strictly positive (`derivation.py`'s `[25, 99]` clamp,
    `composites.normalize_physical`'s >=1.0 floor), so `score ** 2.4 > 0`
    always and `weighted_choice`'s `rng.choice` fallback is unreachable."""

    crash_oreb_freq_share_scale: float = 0.003
    crash_oreb_bias_lo: float = 0.7
    crash_oreb_bias_hi: float = 1.3
    out_of_position_defender_penalty: float = 0.7
    attribution_concentration_exponent: float = 2.4


@dataclass(frozen=True)
class FatigueTuning:
    """B4 fix (critic-proven): Decision P1-20 declares `PlayerGameState.
    fatigue` as "a per-player pool depleted by seconds played and by beat
    participation intensity ... recovering on the bench, with `stamina`
    setting both the pool size and the recovery rate," but nothing under
    `sim/` ever wrote to it -- `on_ball.py`'s `offense_fatigue`/
    `defense_fatigue` parameters existed and were tested in isolation, but
    no caller ever passed a non-zero value, making `fatigue_diff_weight`
    dead in practice. `game.py` now depletes/recovers the pool every
    possession (see `game._update_fatigue`) and `possession.py` reads the
    live pool via `game_state.player(id).fatigue` when calling
    `resolve_on_ball_beat`.

    `stamina` (a `PlayerRatings` 0-99 field) scales both directions
    around a neutral midpoint (`~50`) rather than literally resizing the
    pool -- a bounded [0, 1] pool with a stamina-scaled *rate* on both
    depletion and recovery is the documented simplification of "stamina
    sets pool size and recovery rate" this dispatch uses; a genuinely
    variable pool ceiling would need every fatigue read elsewhere to also
    know that player's personal ceiling, which no current caller does.

    `beat_participation_depletion` is read against
    `BeatRecord.actor_ids` -- how many times a player is named as an actor
    across a possession's beat log -- as a documented proxy for "beat
    participation intensity" (a ball-handler who's an actor in setup,
    screen, on-ball, help, rotation, and resolution is named far more often
    than a weak-side shooter who never touches the ball), reusing data
    `simulate_possession` already returns rather than adding a second,
    parallel role-tracking mechanism."""

    # B4-3 fix (critic-round-7-proven): the round-B4 pass above calibrated
    # this comment's target honestly but the constants it actually shipped
    # didn't hit it -- `beat_participation_depletion` alone contributed
    # 1.665 of depletion (166% of the whole [0,1] pool) to the busiest
    # player over one measured game (555 beat-actor mentions x 0.003),
    # dwarfing `seconds_depletion_per_second`'s own full-game contribution
    # of 0.246, and the top 2-3 players per game pinned at the 1.0 ceiling
    # by minute 20-22 played and stayed there for the rest of the game --
    # the exact "always-maxed, no real signal" failure this dataclass's
    # docstring already names.
    #
    # B8 fix (critic-round-8-proven): the B4-3 pass above measured its
    # ~0.24-mentions/second reference rate against the OLD 5/5-role-split
    # fixture (`tests/fixtures/sim.py`, pre-B6) -- the CURRENTLY shipped
    # fixture (B6's 1-role/9-no-role split) produces a materially lower
    # rate for its busiest player, 0.194-0.218 mentions/second measured
    # directly by instrumenting `_update_fatigue` across 3 full seeded
    # games (seeds 5/11/42; 380-389 actor mentions over 1,743-2,003
    # on-court seconds each; mean 0.2087/s), and at that lower rate the
    # B4-3 constants only reached 0.67-0.71, missing the documented 0.7-0.8
    # band -- the same "docstring asserts a calibration the shipped config
    # doesn't hit" defect shape as the original B4, reintroduced by B4-3's
    # own fix. Re-solved from scratch (not nudged) against the CURRENT
    # fixture's measured 0.2087/s mean rate, same target shape as B4-3:
    # the reference scenario is "a neutral-stamina (exactly 50) player who
    # is on court for the full 2,880-second game, never subs out, and is
    # beat-participation-active at ~0.2087 mentions/second" -- solved so
    # that scenario's seconds-term and participation-term contribute
    # roughly equally (0.373 and 0.377 of the [0,1] pool respectively) and
    # together land at ~0.75 (mid-band of the documented 0.7-0.8 target).
    # `seconds_depletion_per_second` happens to round to the same value as
    # B4-3 shipped (0.00013) since it was never the term that drifted;
    # `beat_participation_depletion` moves from 0.00054 to 0.00063 to
    # compensate for the lower real mention rate. Re-ran the same
    # instrumented probe with the new constants across the measured rate's
    # actual spread (0.194-0.218/s, not just the 0.2087 mean): final
    # fatigue lands 0.724-0.768, comfortably inside 0.7-0.8 at every point
    # in that spread, not just at the mean (see the regression test's own
    # mutation-test report for the revert-and-confirm-fails check).
    #
    # B11 fix (critic-round-9-proven): the "re-ran the same instrumented
    # probe with the new constants" claim directly above was false as
    # shipped -- critic round 9 independently re-instrumented
    # `_update_fatigue` across the same seeds 5/11/42 and proved the B8
    # pass above measured against the OLD `beat_participation_depletion`
    # (0.00054), never actually re-run against the shipped 0.00063 it
    # claimed to validate. The true numbers, measured directly against the
    # shipped fixture AND the shipped 0.00063 constant: 384-389 actor
    # mentions over 1,713-2,003 on-court seconds for the busiest player
    # across seeds 5/11/42, mean 0.2108/s (range 0.194-0.224/s, not
    # 0.194-0.218/s -- close to but not identical to B8's stale numbers).
    # At the shipped 0.00063 constant, this true spread lands final fatigue
    # at 0.724-0.779 (mean-rate case ~0.755), still comfortably inside the
    # documented 0.7-0.8 band at every point, including the low end -- B8's
    # claimed calibration holds, just not for the reason or the exact
    # numbers B8 itself gave. Critic also showed the OLD (regressed)
    # 0.00054 constant lands at ~0.700 against this TRUE mean rate -- a
    # ~0.0001 margin above the 0.70 floor, not real discriminating power --
    # which is why the regression test (`test_fatigue.py`) no longer trusts
    # a hardcoded snapshot rate at all: it now measures this reference rate
    # live at test time (so a future fixture/engine change can't leave a
    # stale number behind a third time) and probes the band at the lowest
    # of the 3 seeds' measured rates rather than their mean (the floor side
    # is what a weakened constant threatens, and the lowest rate is where
    # the shipped calibration has the least headroom above it -- see that
    # test's own docstring for the full reasoning). No constant here
    # changed as part of this fix; only the documented numbers and the
    # test's derivation method did.
    # `recovery_per_second` stays pinned equal to `seconds_depletion_per_second`
    # (a deliberate B4-3 choice preserved here, not re-derived) so bench
    # time and on-court time are symmetric absent the participation term.
    # B12 fix (step 11 retune, this dispatch): the step 11 pace retune
    # (`PossessionTuning.setup_seconds`/`secondary_beat_seconds_range`/
    # `resolution_seconds_range`, all scaled ~1.4x to close the pace
    # validation band) lengthens every possession in real seconds without
    # changing how many beats happen inside one -- the direct, measured
    # consequence is fewer possessions (and therefore fewer beat-actor
    # mentions) per real game-second than B11's calibration was solved
    # against. `test_fatigue.py::_measure_worst_case_busiest_player_rate`
    # re-derives this rate live rather than trusting a stale snapshot (per
    # its own B11 fix), and against the post-pace-retune engine it measures
    # ~0.159 mentions/second at the worst-case (lowest) of the 3 reference
    # seeds -- down from B11's ~0.194-0.224 range, as expected. `
    # seconds_depletion_per_second` (below) is untouched -- its contribution
    # depends only on real game-clock seconds played, which the pace retune
    # does not change -- so only this constant needs re-solving:
    # `0.00063 -> 0.00083` restores the same ~0.75 (mid-band of the
    # documented 0.7-0.8 target) final fatigue at the new, lower measured
    # rate that the old constant gave at the old rate (0.159 * 2880 *
    # 0.00083 + 0.00013 * 2880 = 0.380 + 0.374 = 0.754).
    #
    # B25-1 addendum (this round's pace rebalance, see `PossessionTuning`'s
    # own B25-1 comment): re-measured live via the same
    # `_measure_worst_case_busiest_player_rate` after the round-25 possession-
    # length fix -- 0.147 mentions/second at the worst-case seed (down
    # slightly from 0.159, since continuations now consume less real
    # game-clock time per cycle), giving a final fatigue of ~0.726, still
    # comfortably inside the documented 0.7-0.8 band. No constant below
    # changed for this round -- the live re-derivation is what keeps this
    # comment from becoming another instance of the stale-reference defect
    # class (B8/B11/B25-2) rather than a re-solve.
    seconds_depletion_per_second: float = 0.00013
    beat_participation_depletion: float = 0.00083
    recovery_per_second: float = 0.00013
    # Multiplicative scale applied to depletion/recovery, linearly mapped
    # from `stamina` (0-99): a low-stamina player depletes faster and
    # recovers slower than a high-stamina one, both anchored around 1.0 at
    # a neutral stamina (~50).
    stamina_depletion_lo: float = 0.7
    stamina_depletion_hi: float = 1.3
    stamina_recovery_lo: float = 0.7
    stamina_recovery_hi: float = 1.3


@dataclass(frozen=True)
class PossessionTuning:
    """Decision P1-11/P1-17/P1-20 scalar constants with no natural home
    in any single beat's tuning dataclass -- the shot clock, the shared
    continuation-budget cap, and the seconds a possession's beat cycle
    consumes on average. `setup_seconds` mirrors `beats/setup.py`'s own
    existing `seconds_consumed=6.0` default so both live in one place
    rather than two independently-editable literals.

    **B25-3 fix (critic-round-25-proven) -- the targeted-vs-uniform pullback
    rationale every affected beat's own scale comment above/below points
    back to here.** Step 11's chain-wide flattening pass (see this class's
    B25-1 comment) treated every skill-sensitive contest as equally
    responsible for compressing `team_win_total_sd`, and pulled all of them
    down to ~27% of their pre-flatten discrimination uniformly -- critic
    proved that crushed real basketball signal (an elite vs. poor rim
    finisher, 0.4 percentage points apart at season level) for no
    differentiated benefit. The constants this beat and every other
    affected `BinaryBeatTuning`/`ScreenTuning`/`HelpTuning`/
    `ResolutionShotTuning` field carry now split into two groups instead of
    one uniform pullback: **conditional beats** (screen, on-ball, help
    gate/quality, rotation kickout/roll-tag, rebound -- each fires on only
    some possessions, gated by action type or an upstream branch) are
    restored to ~50% of pre-flatten discrimination; **every-possession
    beats** (the shot-make zones, turnover, foul -- resolution runs on
    effectively every possession, per critic's own observation that these
    are what actually drive the team-level compounding) are restored more
    conservatively, to ~40%, since they carry more of the actual
    `team_win_total_sd`-compression load. This is the "targeted flattening
    of only the specific beats that most drive win-total variance" critic
    explicitly recommended as an alternative to blanket uniform scaling.
    `tests/unit/sim/test_discrimination_floor.py` is the regression guard
    (added first, per critic's own recommendation, before this retune
    landed) that a future flattening pass cannot silently repeat this
    defect without a test failure. Re-verified end to end against
    `tools/validate_sim.py` after every beat's pullback landed -- see the
    round-25 build report for the honest before/after `team_win_total_sd`
    snapshot; per the fix brief's own explicit permission, it is acceptable
    for that metric to end up harder to hit or even slightly out of band
    if the alternative is basketball-meaningless discrimination, and the
    report states plainly which outcome this round produced."""

    shot_clock_seconds: float = 24.0
    offensive_rebound_shot_clock_reset: float = 14.0
    continuation_budget_max: int = 3
    chain_depth_max: int = 3
    # Calibrated (not §4.4-table-derived; a step 10/11 tuning-loop target)
    # so a full ball-handler chain (setup + up to 4 secondary beats +
    # resolution) averages well under the 24s shot clock, leaving real
    # room for a secondary read -- an early version of these constants
    # averaged ~29s for a single `pnr_ball` pass with zero secondary
    # reads, which forced roughly a third of all possessions into a
    # shot-clock `TURNOVER_DEAD` regardless of any beat's actual outcome
    # (caught by this dispatch's own smoke test, not a unit test -- see
    # the build report).
    #
    # Step 11 retune (Decision P1-3's exit criterion) -- SUPERSEDED, see the
    # B25-1 comment directly below. The step-11 report's load-bearing claim
    # was that possession length "scales exactly linearly with a uniform
    # multiplier ... independent of how many continuation cycles that path
    # happens to take." Critic round 25 proved that false: `shot_clock_
    # seconds` (above) was never scaled alongside these three constants, so
    # a possession's length was actually min(chain_time * 1.4, 24s) -- a
    # truncation, not a scaling -- and the truncation rate is *not* linear
    # in the scale factor. Measured at the shipped 1.4x scale: 13.85% of
    # possessions were forced to a shot-clock `TURNOVER_DEAD` (vs. 2.96% at
    # the pre-retune constants), a 4.7x jump for a 1.4x scale change, and
    # every one of those was invisible to the box score before the B25-1
    # `possession.py` fix (a `TURNOVER_DEAD` with no attributed player was
    # silently dropped by `boxscore.py`). Real NBA shot-clock violations run
    # well under 1% of possessions (general basketball knowledge -- roughly
    # 0.3-0.5 per team per game against ~99 possessions/team/game); the
    # constants below no longer target that literal figure (the chain
    # architecture's own compounding multi-beat/multi-continuation
    # structure makes single-digit-percent violations the honest floor
    # without a possession-model redesign, out of this round's scope), but
    # they cut the rate roughly 2.5x (13.85% -> ~5%, measured across
    # multiple seeds) while keeping every pace-shape metric this round's
    # predecessor already fixed still in band -- see the round-25 build
    # report for the full before/after measurement and the seeds it was
    # checked against.
    #
    # B25-1 fix (critic-round-25-proven): the step-11 diagnosis (a single
    # scalar multiplier is inadequate) pointed at the truncation, but the
    # deeper structural cause critic didn't fully name is that EVERY
    # continuation cycle -- a `CONTAINED` secondary read, an offensive
    # rebound, or a non-bonus foul, per Decision P1-11's shared budget --
    # loops all the way back to a brand-new `resolve_setup_beat` call and
    # pays the SAME full setup+secondary cost the possession's *first*
    # attempt did, as if the offense were bringing the ball up the floor
    # again. Measured directly (instrumented `possession.py`'s own loop
    # over a real synthetic league, seed 555): 64% of possessions run a
    # single cycle (mean 10.1s, 4.6% violation), but the 36% that continue
    # at least once average 19-25s and 15-62% violation -- almost entirely
    # a single-cycle problem paying multi-cycle prices. Real basketball
    # continuations (a kick-out that goes nowhere, a live offensive
    # rebound, an inbounds after a whistle) resume play far faster than an
    # opening possession's initial framing -- there is no second trip up
    # the floor. `continuation_setup_seconds`/`continuation_secondary_
    # beat_seconds_range` below are new, distinct (cheaper) constants
    # `possession.py`'s chain loop reads on every iteration *after* the
    # first (see its own module docstring interpretive call 12); the
    # original `setup_seconds`/`secondary_beat_seconds_range` now describe
    # the first attempt only, and were raised to compensate for the lower
    # continuation cost so the season-level average still lands near the
    # ~14.55s/possession (~99 poss/48) reference target. All 5 constants
    # were re-solved together against a live measurement, not independently
    # guessed -- see the round-25 build report for the sweep.
    setup_seconds: float = 6.0
    secondary_beat_seconds_range: tuple[float, float] = (1.8, 5.0)
    # B25-1 fix: the cost of resuming play after a continuation event --
    # deliberately much cheaper than the first attempt's own costs above
    # (see this dataclass's B25-1 comment). Read by `possession.py` on
    # every chain-loop iteration after the first.
    continuation_setup_seconds: float = 1.5
    continuation_secondary_beat_seconds_range: tuple[float, float] = (0.8, 1.8)
    resolution_seconds_range: tuple[float, float] = (1.4, 3.5)
    # Real NBA rule (not an interpretive calibration): the team's 5th foul
    # in a period puts them in the bonus (2 FT, no 1-and-1 in the modern
    # rule set) -- Decision P1-20's per-team-per-period foul counter.
    bonus_team_foul_threshold: int = 5
    period_seconds: float = 720.0
    overtime_seconds: float = 300.0
    fouls_to_disqualify: int = 6
    # B4 fix (critic-proven): Decision P1-17 -- "team_strategy.pace_target
    # scales those [per-beat] costs" -- was declared but never read
    # anywhere under `sim/`. `possession.py` maps the offense team's
    # `pace_target` (0-100, neutral at 50) linearly onto this multiplier
    # range and applies it to every seconds-consuming tick in the chain
    # (setup, each secondary beat, resolution); a low-pace team's beats run
    # slower (`pace_scale_hi`, near 50 -> ~1.0), a high-pace team's run
    # faster (`pace_scale_lo`). Deliberately scoped to per-beat second
    # costs only, per this fix round's brief -- the early-clock-shot-
    # probability half of Decision P1-17 is not wired this round.
    pace_scale_lo: float = 0.85
    pace_scale_hi: float = 1.15
    # B4 fix (critic-proven): §4.6 "foul trouble ... feeds the rotation
    # model" -- `PlayerGameState.is_in_foul_trouble` had zero callers.
    # `game._select_lineup`'s ratio-based balancer inflates a foul-trouble
    # player's played/target ratio by this multiplier before sorting, which
    # makes them look further ahead of their minutes target and therefore
    # less likely to be one of the 5 players furthest behind it -- "more
    # likely to sit," not "cannot play" (a team down to 4 healthy-foul
    # players at a position still needs them on the floor).
    foul_trouble_bench_multiplier: float = 1.5


@dataclass(frozen=True)
class TuningProfile:
    """The engine's entire tunable surface. Every field has a default so
    `TuningProfile()` alone is a complete, usable profile -- a calibration
    sweep constructs variants via `dataclasses.replace`, never by editing
    this file's defaults in place mid-sweep."""

    physical_anchors: PhysicalAnchors = field(
        default_factory=lambda: PhysicalAnchors(
            height=_default_height_anchors(), wingspan=_default_wingspan_anchors()
        )
    )
    composites: CompositeTuning = field(default_factory=CompositeTuning)
    setup: SetupTuning = field(default_factory=SetupTuning)

    screen: ScreenTuning = field(default_factory=ScreenTuning)
    on_ball: OnBallTuning = field(default_factory=OnBallTuning)
    help: HelpTuning = field(default_factory=HelpTuning)

    # §4.4's remaining rows -- steps 4-5 scope, held here as data only.
    rotation_kickout: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv(
                {
                    "off_ball_movement": 0.2,
                    "relocation": 0.15,
                    "cutting": 0.1,
                    "basketball_iq": 0.1,
                    "passing_vision": 0.2,
                    "passing_accuracy": 0.15,
                    "pass_under_pressure": 0.1,
                }
            ),
            defense_weights=_wv({"closeout_speed": 0.55, "closeout_control": 0.45}),
            base_rate=0.5,
            # Step 11 retune: 40 -> 63 -> 88 -> 119 -> 149 (4 passes), same
            # chain-wide flattening pass -- SUPERSEDED, B25-3 fix.
            #
            # B25-3 fix (critic-round-25-proven): 149.0 retained 27.1% at a
            # realistic 30-point gap. Kick-out only fires on the
            # `HELP_COMMITTED`/`HELP_LATE` branch of an already-conditional
            # chain -- one of the targeted pullbacks (see `PossessionTuning`'s
            # B25-3 note). Restored to 81.0 (~50% retained).
            scale=81.0,
            band=(0.1, 0.9),
        )
    )
    rotation_roll_tag: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv(
                {
                    "roll_ability": 0.35,
                    "catch_hands": 0.25,
                    "short_roll_passing": 0.2,
                    "vertical": 0.2,
                }
            ),
            defense_weights=_wv(
                {"scheme_discipline": 0.35, "help_iq": 0.35, "rotation_speed": 0.3}
            ),
            base_rate=0.5,
            # Step 11 retune: 40 -> 63 -> 88 -> 119 -> 149 (4 passes), same
            # chain-wide flattening pass -- SUPERSEDED, B25-3 fix (same
            # rationale as `rotation_kickout` above). Restored to 81.0.
            scale=81.0,
            band=(0.1, 0.9),
        )
    )
    # docs/design/pick-and-pop.md PP-8: the pop's openness contest, sitting
    # beside `rotation_kickout`/`rotation_roll_tag` above. PP-8.1: the
    # offense vector is separation, not shooting -- `screen_timing` leads it
    # (popping in rhythm off your own screen is the same skill as setting it
    # in rhythm), `off_ball_movement`/`relocation` are the same pair
    # `_kickout_offense_score`'s vacated-man half already uses for "got to a
    # shootable spot," `catch_hands` because a pop is a catch on the move,
    # `basketball_iq` for reading which pocket to pop into. No shooting
    # field enters this contest (PP-5): shooting gravity cuts both ways (a
    # great shooter is also closed out harder) and the engine has no
    # gravity mechanism to net them, so putting `catch_shoot_3` here too
    # would let one rating compound three ways (more pops, more open ones,
    # more makes) -- declared out of scope, not overlooked. PP-8.2: the
    # defense vector is `rotation_kickout`'s pair verbatim, declared as its
    # own instance (not aliased) so it stays independently tunable --
    # `base_rate=0.55` against kickout's 0.50 is the documented
    # interpretive call that a screen-level closeout starts further from
    # the popper than a corner rotation does; `scale`/`band` are copied
    # from both sibling rotation contests' B25-3-restored values rather
    # than re-solved -- a new conditional beat with no measured
    # discrimination history has no basis for a different retention target.
    rotation_pop: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv(
                {
                    "screen_timing": 0.30,
                    "off_ball_movement": 0.25,
                    "relocation": 0.20,
                    "catch_hands": 0.15,
                    "basketball_iq": 0.10,
                }
            ),
            defense_weights=_wv({"closeout_speed": 0.55, "closeout_control": 0.45}),
            base_rate=0.55,
            scale=81.0,
            band=(0.1, 0.9),
        )
    )
    resolution_shot: ResolutionShotTuning = field(default_factory=ResolutionShotTuning)
    resolution_block: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv(
                {
                    "finishing_rim": 0.3,
                    "contact_finishing": 0.25,
                    "strength": 0.25,
                    "floater": 0.2,
                }
            ),
            defense_weights=_wv(
                {
                    "rim_protection": 0.45,
                    "vertical": 0.2,
                    "wingspan_in": 0.2,
                    "lateral_quickness": 0.15,
                }
            ),
            base_rate=0.06,
            # Step 11 retune: 35 -> 52 -> 73 -> 99 -> 124, then pulled back
            # to 45. The chain-wide flattening pass above (see
            # `PossessionTuning`'s team_win_total_sd note) pushed this
            # beat's scale to 124 along with every other contest, but
            # `test_resolution.py::test_elite_rim_protector_blocks_more_
            # often_than_a_poor_one` caught a real regression that pass
            # introduced: at base_rate=0.06 the block-make probability is
            # already small, and scale=124 flattened an 85-point
            # rim_protection/vertical/lateral_quickness gap (elite vs.
            # replacement-level) down to a probability difference too small
            # to reliably separate from 300-draw sampling noise (measured
            # 8 vs. 9 blocked_miss outcomes -- statistically indistinguishable).
            # Blocks are also a small enough share of resolution's own leaf
            # distribution (a few percent) that this beat individually
            # contributes little to team_win_total_sd's team-level
            # compounding -- unlike the shot-make zones, turnover, and foul
            # contests, which every possession runs -- so pulling this one
            # beat back to a much lighter flattening (35 -> 45, matching
            # the very first pass's value) restores real skill-gap
            # discrimination here without reopening team_win_total_sd (see
            # the build report's re-verify run after this pullback).
            scale=45.0,
            band=(0.0, 0.35),
        )
    )
    # SUPERSEDED in part (docs/design/block-attribution-v2.md, 2026-09-24):
    # the vector, `base_rate` and `scale` below were re-solved by BA-2.3/
    # BA-2.4 under the share model (their inline comments); the BDA-era
    # calibration history that follows is kept as history.
    # BDA-4/BDA-6 (docs/design/block-defender-attribution.md): the weak-
    # side rim-help contest. `base_rate`/`scale` calibrated jointly against
    # `blk_per_game` and `fg_pct_rim` (BDA-6.4/BDA-7 risk 2 -- one knob,
    # two bands, so a bad fit can't hide behind the other) -- see the As-
    # built section at the bottom of the design doc for the measured
    # before/after `tools/validate_sim.py` run this shipped with.
    # `aggression_lo`/`aggression_hi` start at the same 0.7/1.3 spread
    # `CompositeTuning.scheme_pressure_help_aggression_lo/_hi` already
    # uses for the structurally analogous help-aggression scale.
    # Calibrated (this dispatch) jointly against `blk_per_game` and
    # `top_shot_blocker_bpg` via `tools/validate_sim.py` subsets (150-200
    # games, 4 independent league seeds) -- `base_rate=0.16`/`scale=42.0`
    # (this file's first-committed starting guess) measured `blk_per_game`
    # inside band but `top_shot_blocker_bpg` at 5.2-8.3, well ABOVE
    # [1.8, 4.2]: BDA-4's own mechanism is strong enough that the doc's
    # suggested `base_rate≈0.10` (not the higher starting guess) is the
    # one that actually lands both bands, confirmed across seeds 1-4 at
    # `blk_per_game` 4.28-4.33 and `top_shot_blocker_bpg` 2.4-3.3 (both
    # comfortably inside band), with `fg_pct_rim` 0.587-0.592 (also
    # in-band, and BDA-6.1's own note that `base_rate` should be solved
    # against `fg_pct_rim` jointly with `blk_per_game` -- confirmed here,
    # not assumed). See the design doc's As-built section for the full
    # before/after `validate_sim` table.
    resolution_rim_help: ResolutionRimHelpTuning = field(
        default_factory=lambda: ResolutionRimHelpTuning(
            # block-attribution-v2 BA-2.4: a shot-*blocking* vector. Whether a
            # rotator arrives is the Help beat's job; whether he blocks when
            # he gets there is rim protection, length and lift, with
            # `rotation_speed` kept as a small arrival tie-breaker. The old
            # help-commit vector (55% `rotation_speed` + `help_iq`) ranked
            # Jokic over Wembanyama. `vertical` stays at 0.15 on purpose: it
            # is what separates the fifteen real `rim_protection=99` players
            # today; the vector without the synthetic physicals (V3) becomes
            # right only once the ratings side separates the top (BA-2.9).
            defense_weights=_wv(
                {
                    "rim_protection": 0.5,
                    "wingspan_in": 0.25,
                    "vertical": 0.15,
                    "rotation_speed": 0.1,
                }
            ),
            # BA-2.3, one knob pair solved jointly: `base_rate` against
            # `blk_per_game`, `scale` against the top-10 shape. 45 -> 12 -> 13
            # makes the contest skill-sensitive enough that the best blocker
            # separates (Wembanyama #1 by bpg, Jokic well down on real-2025).
            # 13, not 12 (Correction, 2026-09-24): at 12 the gated real-2025
            # `blk_per36_p95_rotation` failed its 2.4 ceiling on 4 of 5 seeds
            # (101/1/2/7/202, 3-season means 2.38-2.46); 13 passes all five
            # (2.19-2.34). The cost is synthetic seed 2's top-10 ratio (1.327
            # vs 1.35), a league with no standout big -- BA-7 risk 4 excuses
            # it. 14 also drops seed 101's max. BA-7 risk 3 (the real SD at
            # 0.74 vs 0.7625) forbids *tightening* `scale` below 12; it says
            # nothing about p95. These are the right constants for today's
            # seeds only: whichever ratings change lands next re-solves them
            # (BA-2.9 rule 3). `team_win_total_sd` is the watched band; the
            # pre-committed lever is this `scale`, never a chain-wide pass.
            base_rate=0.055,
            scale=13.0,
            # BDA-9 note (critic-proven, this dispatch): `resolve_resolution_
            # beat` multiplies `contest_probability`'s output by
            # `aggression_linear_scale(help_aggression, aggression_lo,
            # aggression_hi)` AFTER the clamp to `band` (same post-multiply
            # shape `resolution_block.base_rate`'s own `block_zone_multiplier`
            # already uses) -- so the realized `p_help_block` can reach
            # `band`'s declared ceiling times `aggression_hi` (0.40 * 1.3 =
            # 0.52) at max help aggression, contradicting `contest_
            # probability`'s own docstring guarantee ("bounded to `band`
            # regardless of how extreme the inputs are"). This is the first
            # beat where that gap is provably reachable in practice. Left
            # un-clamped rather than silently re-clipped here: doing so would
            # shift the already-calibrated `blk_per_game`/`top_shot_blocker_
            # bpg`/`fg_pct_rim` bands above (BDA-6/BDA-7 risk 2) at the
            # high-aggression tail, which needs its own `tools/validate_sim`
            # re-verification pass, not a silent one-line change riding
            # alongside an unrelated fatigue-dedup fix. Flagged here for a
            # future pass -- either clamp the post-multiplied value into
            # `band` and re-verify the three bands above, or widen `band`'s
            # declared upper bound to genuinely cover the realized range.
            band=(0.0, 0.40),
            aggression_lo=0.7,
            aggression_hi=1.3,
            # BA-2.2's sweep (half-season synthetic seed 1, rates held at
            # 14 / 0.06): `(c - 50) ** 4` is the first concentration with the
            # real top-10 shape (#1/#10 1.55; `** 3` just at the max floor,
            # `** 5` equivalent and steeper, shift 60 flatter).
            attribution_concentration_exponent=4.0,
            concentration_shift=50.0,
        )
    )
    resolution_turnover: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv({"ball_security": 0.6, "pass_under_pressure": 0.4}),
            defense_weights=_wv({"hands": 0.5, "on_ball_defense": 0.5}),
            base_rate=0.12,
            # Step 11 retune: 35 -> 52 -> 73 -> 99 -> 124 (4 passes), same
            # rationale as the shot/block scale bumps above -- SUPERSEDED,
            # B25-3 fix below.
            #
            # B25-3 fix (critic-round-25-proven): 124.0 retained only 27.9%
            # at a realistic 30-point gap. This contest runs on (effectively)
            # every possession, same as the shot-make zones -- deliberately
            # NOT pulled back to the ~50% conditional-beat target (see
            # `PossessionTuning`'s B25-3 note), restored to a more modest
            # 87.0 (~40% retention), re-verified against `team_win_total_sd`
            # after every beat's pullback landed.
            scale=87.0,
            band=(0.02, 0.4),
        )
    )
    steal_attribution: WeightVector = field(
        default_factory=lambda: _wv(
            {"hands": 0.4, "on_ball_defense": 0.35, "lateral_quickness": 0.25}
        )
    )
    resolution_foul: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv({"draw_foul": 1.0}),
            defense_weights=_wv({"foul_discipline": 1.0}),
            base_rate=0.15,
            # Step 11 retune: 30 -> 47 -> 66 -> 89 -> 111 (4 passes), same
            # chain-wide flattening pass -- SUPERSEDED, B25-3 fix below.
            #
            # B25-3 fix (critic-round-25-proven): 111.0 retained only 26.8%
            # at a realistic 30-point gap. Runs on (effectively) every
            # possession, same rationale as `resolution_turnover` above --
            # restored to a more modest 74.0 (~40% retention).
            scale=74.0,
            band=(0.02, 0.45),
        )
    )
    rebound: BinaryBeatTuning = field(
        default_factory=lambda: BinaryBeatTuning(
            offense_weights=_wv(
                {"offensive_rebounding": 0.5, "motor": 0.25, "height_in": 0.25}
            ),
            defense_weights=_wv(
                {"defensive_rebounding": 0.5, "strength": 0.25, "height_in": 0.25}
            ),
            base_rate=0.28,
            # Step 11 retune: 35 -> 55 -> 77 -> 104 -> 130 (4 passes), same
            # chain-wide flattening pass -- SUPERSEDED, B25-3 fix below.
            #
            # B25-3 fix (critic-round-25-proven): 130.0 retained only 27.1%
            # at a realistic 30-point gap (an elite rebounder pulled 30.4%
            # vs. 25.7% for a poor one, critic measured). Rebound only fires
            # on a miss, not every possession -- one of the targeted
            # pullbacks (see `PossessionTuning`'s B25-3 note). Restored to
            # 70.0 (~50% retained).
            scale=70.0,
            band=(0.05, 0.6),
        )
    )

    # Step 4 additions (see the dataclasses' own docstrings above).
    free_throw: FreeThrowTuning = field(default_factory=FreeThrowTuning)
    rotation_branch: RotationBranchTuning = field(default_factory=RotationBranchTuning)
    turnover_split: TurnoverSplitTuning = field(default_factory=TurnoverSplitTuning)
    loose_ball: LooseBallTuning = field(default_factory=LooseBallTuning)
    foul_split: FoulSplitTuning = field(default_factory=FoulSplitTuning)
    rebound_modifiers: ReboundModifierTuning = field(default_factory=ReboundModifierTuning)
    possession: PossessionTuning = field(default_factory=PossessionTuning)
    fatigue: FatigueTuning = field(default_factory=FatigueTuning)


DEFAULT_TUNING: TuningProfile = TuningProfile()
