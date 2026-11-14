"""The single source of truth for `seeds/gm_phrase_selection_golden.json`'s
input corpus -- Decision P4-47 (as amended by P4-64), docs/design/phase-4-
media-and-player-agency.md §11 Stage D step 15.

Imported by BOTH `tools/generate_gm_phrase_selection_golden.py` (which
writes the committed golden from this corpus) and `tests/unit/tools/
test_gm_phrase_selection_golden.py` (which replays this SAME corpus
against `select_phrase` and diffs element-wise against the committed
file) -- one corpus definition, not two, so the two can never drift apart
the way a hand-copied corpus could (the same discipline `tests/fixtures/
managers.py::generate_archetype_table` already uses for the golden value/
pick/prospect tables, and `tools/gm_phrase_golden_corpus.py`'s own sibling
`test_constant_inventory.py::generate_inventory` uses for its own
before/after comparison).

**Two parts, per Decision P4-64 -- neither substitutes for the other:**

1. `shipped_library_cases()` -- replayed against the REAL, committed
   `seeds/gm_phrases.json` (88 templates, unmodified -- Decision P4-33
   forbids editing it for any reason, including to help this fixture).
   Covers every `(philosophy, situation, window)` combination the live
   library can actually reach -- **the full 7x10x4 = 280-member cross
   product** (`GmPhilosophy`=7, `SituationToken`=10, `CompetitiveWindow`=4),
   which is not an approximation of "reachable" but an exact
   reading of it: `_validate_fallback_coverage` (`phrase_service.py`)
   already guarantees a true, all-wildcard fallback template exists for
   every one of the ten `SituationToken` members, so every `(philosophy,
   situation, window)` triple has at least one survivor (the situation's
   own fallback) regardless of numeric field values -- there is no
   *unreachable* corner of this cross product to exclude. Plus two
   hand-built cases: one with a non-empty `use_counts` on a template that
   is actually among that case's survivors (so the repeat penalty's decay
   engages, not just gets called with an all-zero `.get()` default), and
   one constructed so every specific (triggered) survivor's trigger fails
   and only the situation's designated fallback remains.

2. `synthetic_operator_coverage_cases()` -- replayed against `SYNTHETIC_
   TEMPLATES`, a small fixture library **declared in this module, not in
   `seeds/gm_phrases.json`** (Decision P4-33: a gated file never moves to
   help a test pass). Measured directly against the shipped library
   (`grep -o '"op": "[a-z]*"' seeds/gm_phrases.json`): `gte` 48, `eq` 16,
   `lt` 13, `lte` 3, `gt` 0, `in` 0 -- the shipped library is structurally
   incapable of exercising `gt`/`in` at all, so a golden replayed only
   against it would report full trigger-operator coverage while actually
   reaching four of six branches. `SYNTHETIC_TEMPLATES` carries one
   template per operator (six) plus one true wildcard fallback, and this
   function's cases exercise every operator in BOTH directions (holds /
   does not hold) against a shared, non-overlapping set of trigger
   features chosen so each case's outcome is attributable to exactly one
   operator's own trigger, not an accidental collision with another's.

Every RNG stream below is derived via `dba2.core.rng.rng_for`, keyed on
this module's own fixed master seed plus the case's own label -- the
identical derivation discipline every real selection stream in this
project already uses (Decision P3-20), which is what makes a fresh
`rng_for(...)` call at golden-capture time and a fresh `rng_for(...)` call
at replay time produce byte-identical draws without either call site
sharing or mutating a `random.Random` instance across cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dba2.core.rng import rng_for
from dba2.domain.team import GmPhilosophy
from dba2.managers.negotiation import NegotiationStance
from dba2.managers.tuning import CompetitiveWindow
from dba2.managers.voice.frame import NegotiationFrame, SituationToken
from dba2.managers.voice.select import PhraseLibrary, PhraseTemplate, Trigger

GOLDEN_MASTER_SEED = 424242
"""Fixed, arbitrary, and never reused for anything else -- this corpus's
own RNG namespace, independent of every real `rng_for` salt this project
derives selection/scouting/chaos/progression/media/motivation streams
from (Decision P3-20/P4-23). It is not a production seed and never
touches a real run."""


@dataclass(frozen=True)
class GoldenCase:
    """One `(label, frame, use_counts)` corpus entry. `rng` is not stored
    here -- both the generator and the replay test derive a fresh
    `random.Random` from `label` at the point of use (`case_rng` below),
    so neither ever shares or mutates one `random.Random` instance across
    two cases or across a capture/replay pair."""

    label: str
    frame: NegotiationFrame
    use_counts: dict[str, int]


def case_rng(label: str):
    """The one derivation both the generator and the replay test call --
    same label, same stream, every time, forever (until `GOLDEN_MASTER_
    SEED` itself changes, which would be a deliberate re-capture)."""
    return rng_for(GOLDEN_MASTER_SEED, "gm_phrase_golden", label)


def _base_frame(**overrides: Any) -> NegotiationFrame:
    base: dict[str, Any] = dict(
        gap_abs=0.0, gap_rel=0.0, gap_over_band=0.0, s_in=0.0, s_out=0.0,
        in_players=0, out_players=0, in_picks=0, out_picks=0, top_in_share=0.0,
        cap_relief_share=0.0, illegal_token=None, round_index=0,
        stance=NegotiationStance.COUNTER, situation=SituationToken.OPEN,
        philosophy=GmPhilosophy.STAR_MAXER, window=CompetitiveWindow.CONTEND,
        h2h_shade=0.0, h2h_opened=0.0, h2h_far_reject_streak=0.0,
    )
    base.update(overrides)
    return NegotiationFrame(**base)


# ---------------------------------------------------------------------------
# Part 1 -- shipped-library replay corpus.
# ---------------------------------------------------------------------------

_TOP_IN_SHARE_VALUES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
_GAP_REL_VALUES = (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0)
_GAP_OVER_BAND_VALUES = (-2.0, -1.0, 0.0, 1.0, 2.0)
_ROUND_INDEX_VALUES = (0, 1, 2, 3, 5, 8)
_COUNT_VALUES = (0, 1, 2, 3, 4)
_CAP_RELIEF_VALUES = (-1.0, -0.5, 0.0, 0.5, 1.0)
_H2H_COUNT_VALUES = (0.0, 1.0, 2.0, 3.0, 5.0)
_H2H_SHADE_VALUES = (-1.0, -0.5, 0.0, 0.5, 1.0)
_ILLEGAL_TOKENS = ("roster_max", "roster_min", "apron_2", "apron_1", "salary_matching")
"""The five real `illegal:*` suffixes §4.2's legality gate produces
(`select.py`'s own docstring cites the same five-token gate) -- the exact
values the shipped library's fourteen `illegal_token` triggers compare
against (`grep -n 'illegal_token' seeds/gm_phrases.json`)."""


def _cross_product_case(
    philosophy: GmPhilosophy, situation: SituationToken, window: CompetitiveWindow
) -> GoldenCase:
    """One of the 280 dimension-coverage cases -- numeric fields drawn
    from a `rng_for`-derived stream keyed on this exact combo, so the
    corpus varies meaningfully (different survivor sets, different
    trigger outcomes) across cases without hand-authoring 280 frames."""
    label = f"cross_{philosophy.value}_{situation.value}_{window.value}"
    field_rng = rng_for(GOLDEN_MASTER_SEED, "gm_phrase_golden_fields", label)
    illegal_token = (
        field_rng.choice(_ILLEGAL_TOKENS) if situation is SituationToken.REJECT_ILLEGAL else None
    )
    frame = _base_frame(
        philosophy=philosophy, situation=situation, window=window,
        illegal_token=illegal_token,
        top_in_share=field_rng.choice(_TOP_IN_SHARE_VALUES),
        gap_rel=field_rng.choice(_GAP_REL_VALUES),
        gap_over_band=field_rng.choice(_GAP_OVER_BAND_VALUES),
        round_index=field_rng.choice(_ROUND_INDEX_VALUES),
        in_players=field_rng.choice(_COUNT_VALUES),
        out_players=field_rng.choice(_COUNT_VALUES),
        in_picks=field_rng.choice(_COUNT_VALUES),
        out_picks=field_rng.choice(_COUNT_VALUES),
        cap_relief_share=field_rng.choice(_CAP_RELIEF_VALUES),
        h2h_opened=field_rng.choice(_H2H_COUNT_VALUES),
        h2h_far_reject_streak=field_rng.choice(_H2H_COUNT_VALUES),
        h2h_shade=field_rng.choice(_H2H_SHADE_VALUES),
    )
    return GoldenCase(label=label, frame=frame, use_counts={})


def shipped_library_cases() -> list[GoldenCase]:
    cases = [
        _cross_product_case(philosophy, situation, window)
        for situation in SituationToken
        for philosophy in GmPhilosophy
        for window in CompetitiveWindow
    ]

    # Non-empty use_counts, on a template that is actually a survivor of
    # this exact frame -- confirmed by direct replay against the shipped
    # library (open/star_maxer/contend, top_in_share=0.9, h2h_opened=0.0):
    # survivors are fallback_open, open_star_maxer_headliner, open_star_
    # maxer_new_partner and open_star_maxer_contend_window. use_counts
    # below applies the penalty to one of the triggered survivors, not
    # just the fallback, so the decay is exercised on the branch it is
    # actually meant to suppress.
    cases.append(GoldenCase(
        label="repeat_penalty_engaged",
        frame=_base_frame(
            philosophy=GmPhilosophy.STAR_MAXER, situation=SituationToken.OPEN,
            window=CompetitiveWindow.CONTEND, top_in_share=0.9, h2h_opened=0.0,
            h2h_far_reject_streak=0.0,
        ),
        use_counts={"open_star_maxer_headliner": 5},
    ))

    # Survives-to-fallback-only -- confirmed by direct replay against the
    # shipped library (open/star_maxer/rebuild): the only no-trigger
    # dimension survivor is `fallback_open`, and the three triggered
    # dimension survivors (`open_star_maxer_headliner`, `open_star_maxer_
    # new_partner`, `open_persistent_friction`) all fail their own
    # triggers at these exact values (top_in_share < 0.5, h2h_opened !=
    # 0.0, h2h_far_reject_streak < 2.0), leaving `fallback_open` as the
    # sole survivor.
    cases.append(GoldenCase(
        label="fallback_only_survivor",
        frame=_base_frame(
            philosophy=GmPhilosophy.STAR_MAXER, situation=SituationToken.OPEN,
            window=CompetitiveWindow.REBUILD, top_in_share=0.1, h2h_opened=1.0,
            h2h_far_reject_streak=0.0,
        ),
        use_counts={},
    ))
    return cases


# ---------------------------------------------------------------------------
# Part 2 -- synthetic operator-coverage replay corpus.
# ---------------------------------------------------------------------------

SYNTHETIC_TEMPLATES: PhraseLibrary = (
    PhraseTemplate(
        id="synthetic_fallback", philosophies=(), situations=(), windows=(),
        triggers=(), weight=1.0, text="fallback", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_lt", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="top_in_share", op="lt", value=0.5),),
        weight=1.0, text="lt", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_lte", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="gap_rel", op="lte", value=0.0),),
        weight=1.0, text="lte", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_gte", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="round_index", op="gte", value=3),),
        weight=1.0, text="gte", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_gt", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="in_players", op="gt", value=1),),
        weight=1.0, text="gt", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_eq", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="cap_relief_share", op="eq", value=0.5),),
        weight=1.0, text="eq", slots=(),
    ),
    PhraseTemplate(
        id="synthetic_in", philosophies=(), situations=(), windows=(),
        triggers=(Trigger(feature="out_picks", op="in", value=(2, 3, 4)),),
        weight=1.0, text="in", slots=(),
    ),
)
"""Six operator-specific templates (one per `_trigger_holds` branch,
`engine.py:75-82` as of Phase 4 Stage D step 15's extraction -- was
`select.py:129-136` before it) plus one true wildcard fallback -- all wildcard on
`philosophies`/`situations`/`windows` so step 2 (`_template_matches_
dimensions`) never excludes any of them; the only thing distinguishing a
case is which trigger(s) hold. Each operator's trigger reads a DIFFERENT
`NegotiationFrame` field (`top_in_share`, `gap_rel`, `round_index`, `in_
players`, `cap_relief_share`, `out_picks`) specifically so one case's
frame can flip exactly one operator's outcome without disturbing the
others -- `_SYNTHETIC_BASELINE_OVERRIDES` below is the "every trigger
fails" anchor every single-operator case is built from."""

_SYNTHETIC_BASELINE_OVERRIDES: dict[str, Any] = dict(
    top_in_share=0.9,      # not < 0.5 -- lt fails
    gap_rel=1.0,           # not <= 0.0 -- lte fails
    round_index=0,         # not >= 3 -- gte fails
    in_players=0,          # not > 1 -- gt fails
    cap_relief_share=0.0,  # not == 0.5 -- eq fails
    out_picks=0,           # not in {2, 3, 4} -- in fails
)

_SYNTHETIC_HOLDING_OVERRIDE = {
    "lt": {"top_in_share": 0.1},
    "lte": {"gap_rel": -1.0},
    "gte": {"round_index": 5},
    "gt": {"in_players": 3},
    "eq": {"cap_relief_share": 0.5},
    "in": {"out_picks": 3},
}


def _synthetic_frame(**overrides: Any) -> NegotiationFrame:
    merged = dict(_SYNTHETIC_BASELINE_OVERRIDES)
    merged.update(overrides)
    return _base_frame(**merged)


def synthetic_operator_coverage_cases() -> list[GoldenCase]:
    """One "none hold" baseline case (fallback is the only survivor) plus
    one "holds" case per operator (crushing `use_counts` on the fallback,
    exactly `test_select.py::test_each_closed_operator_evaluated_
    against_the_frame`'s own established convention, so the target
    template's own weight dominates `synthetic_fallback`'s regardless of
    which philosophy/situation/window the frame carries -- irrelevant
    here anyway, since every synthetic template wildcards all three)."""
    cases = [GoldenCase(
        label="synthetic_none_hold", frame=_synthetic_frame(), use_counts={},
    )]
    for op, override in _SYNTHETIC_HOLDING_OVERRIDE.items():
        cases.append(GoldenCase(
            label=f"synthetic_{op}_holds",
            frame=_synthetic_frame(**override),
            use_counts={"synthetic_fallback": 60},
        ))
    # Critic nit (Stage D step 15 review): every case above sits well clear
    # of its own operator's boundary, so a `gt` implemented as `ge` (or any
    # off-by-one at a strict/non-strict comparison) survives the whole
    # corpus undetected -- confirmed directly: mutating `operator.gt` to
    # `operator.ge` in `engine.py` passed both golden parts unchanged before
    # this case existed. `in_players == 1` sits exactly on `gt`'s own
    # threshold (`in_players > 1`): a correct `gt` reads False here (1 is
    # not greater than 1), so the fallback must still win; `ge` would read
    # True and select `synthetic_gt` instead, which this case's own
    # `use_counts` crush makes visible in the recorded selection.
    cases.append(GoldenCase(
        label="synthetic_gt_boundary_value_does_not_hold",
        frame=_synthetic_frame(in_players=1),
        use_counts={"synthetic_fallback": 60},
    ))
    return cases
