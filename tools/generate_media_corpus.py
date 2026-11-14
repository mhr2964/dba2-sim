"""CLI: Stage D step 20a's media corpus generator (Decision P4-95, amended
by Decisions P4-102/P4-103) -- a real, dev-league DB driver that runs the
real per-cycle media pipeline (`media_service.run_media_pulse_cycle` ->
`media_service.run_media_selection_cycle`) twice per season, once against
the shipped library (`live`) and once against `media_service.with_voices_
disabled(library)` (`control`), and pools the result into a committed
`MediaCorpusTotals` artifact.

docs/design/phase-4-media-and-player-agency.md §30 (Decisions P4-94
through P4-101), §32 (Decisions P4-102/P4-103).

Usage (run with `-m` from the repo root, matching every other `tools/
*_dev_league*.py` script's own convention):

    python -m tools.generate_media_corpus --seasons 6 --rng-seed 424242

**Why this is a new dev-league driver rather than a caller of `tools/
gm_harness/season.py`** -- Decision P4-95's own three grounds, restated
briefly rather than re-derived: that harness has no database connection at
all (`season.py:102-105`'s own asserted zero-I/O import list), every media
entry point is `async def`/`Executor`-shaped, and this document has
already solved the identical "unreachable in the DB-free harness" problem
once for Pillar 5 (Decision P4-50/P4-67 split Stage C step 13 the same
way). This script instead composes the same real, DB-backed, transaction-
scoped services `tools/seed_dev_league.py`/`tools/sim_dev_league_
season.py`/`tools/advance_dev_league_season.py` already compose --
`schedule_service.generate_regular_season`, `game_sim_service.sim_
scheduled_games`, `stats_service.rebuild_player_season_stats`,
`strategy_service.ensure_defaults_for_season` -- but stages the game
simulation across the season's own cycles rather than calling `tools.
sim_dev_league_season`'s own one-shot wrapper, for a real reason found
while building this script and explained in full below.

**One real, disclosed judgment call this script makes that Decision
P4-95's own text does not spell out: it also calls `agency_service.run_
agency_checkpoint` directly, twice a season** (once at the season's own
trade-deadline point, phase=`Phase.TRADE_DEADLINE`; once more for free,
inside `tools.advance_dev_league_season`'s own season-boundary call, phase
=`Phase.PROGRESSION`) -- Decision P4-11's own "exactly two checkpoints a
season" cadence, called directly rather than invented. **Without this, no
`player_motivation_events` ladder-transition row is EVER written**, and
`media_service.load_player_unrest_candidates` (the only candidate producer
for `PLAYER_UNREST`/`HOLDOUT_WATCH`) would read empty every cycle of every
season -- `agency_service.bootstrap_league_motivation` (already called by
`seed_dev_league`) only ever creates a player's FIRST `player_motivation`
row; it writes no event and moves no `request_state`. This is not "wiring
media into a harness loop" (the thing Decision P4-95 forbids) -- it is
this script's own dev-league driver reusing an already-built, already-
tested, real service function directly, the identical shape `tools.
advance_dev_league_season` already uses for its own `Phase.PROGRESSION`
call.

**A second, load-bearing, and considerably more consequential disclosure,
found while building this script rather than assumed from the design
doc's own text: five of `StoryKind`'s eleven members have NO real
candidate producer wired into `run_media_pulse_cycle` at all today.**
`dba2/media/events.py`'s own module docstring says so directly --
`trade_execution_candidate` "has no real CALL PATH at all, because there
is nothing correct to call it FROM yet," and no equivalent function exists
for `signing_reaction`/`milestone`/`award_race`/`rookie_watch` either.
`run_media_pulse_cycle` only ever derives frames from `pulse.diff_pulse_
snapshots` (`STANDINGS_SURGE`/`STANDINGS_COLLAPSE`/`STREAK`) and `load_
player_unrest_candidates` (`PLAYER_UNREST`/`HOLDOUT_WATCH`, via `events.
story_kind_for_request_state`). **`TRADE_REACTION`, `TRADE_FALLOUT`,
`SIGNING_REACTION`, `MILESTONE`, `AWARD_RACE`, and `ROOKIE_WATCH` are
therefore structurally UNREACHABLE by this script, and will read zero
selections in the corpus this script produces, no matter how many seasons
it runs.** Wiring a real emission call site for any of them is real,
cross-cutting work inside the trade/free-agency/draft/award pipelines
themselves -- explicitly out of scope for this dispatch (which scopes
step 20a to "the corpus generator," not new candidate producers) and
explicitly named as future, disclosed work by `dba2/media/events.py`'s own
module docstring and Decision P4-100 item 13. **This means five of the six
`ungated_kinds` (`trade_fallout`, `signing_reaction`, `milestone`, `award_
race`, `rookie_watch`) will read zero real selections in this corpus, and
Decision P4-97's own cohort pair (`analyst`/`historian`, chosen partly for
`milestone`/`rookie_watch` volume) will see real volume on `streak` only.**
This is reported plainly by this script's own summary output, not
papered over -- see the module-level `_KNOWN_UNREACHABLE_KINDS` comment
below and this script's own final report section.

**A third structural finding, found by RUNNING this script against a
real dev league, not by reading the design doc: an early build that
called `tools.sim_dev_league_season`'s own one-shot "sim the whole
season, then run every cycle" composition produced a real corpus with
ZERO `STANDINGS_SURGE`/`STANDINGS_COLLAPSE`/`STREAK` candidates across
every cycle of every season.** Two compounding reasons, both real: (1)
`media_repo.get_prior_pulse_snapshot_teams` is deliberately season-scoped
(Decision P4-28), so the FIRST cycle of every season -- not just the
corpus's first cycle ever -- reads no prior snapshot and therefore no
diff, by design; (2) `compute_current_pulse_snapshot` reads ALL of a
season's FINAL games regardless of which cycle is asking, so once the
whole season is simmed before any cycle runs, every cycle within that
season computes the IDENTICAL `current_snapshot` and diffs against it
with zero delta. **Fixed by staging the season's own game simulation
across the cycles that run within it** (`_SEASON_PROGRESS_BY_CYCLE`,
below), using `game_sim_service.sim_scheduled_games`'s own already-
shipped `through_game_index` parameter -- never a new sim capability,
just calling an existing one in increasing-sized chunks instead of once.
This makes later cycles within a season see genuinely more games played
than earlier ones, which is what gives the continuous-kind candidate
producers something real to diff. **One remaining, disclosed wrinkle
this fix does not remove:** the season's own first (`PRESEASON`) cycle
still simulates zero games, so every team's `current_snapshot` at that
point is a `0-0` tie -- the season's SECOND cycle then diffs real
standings/streaks against that degenerate all-tied baseline, which can
read as more movement than the same comparison would against a real,
partially-played prior state. Named here rather than engineered around
further, in the interest of shipping a real, measured corpus rather than
chasing a perfectly-faithful one -- see this script's own final report
for what the real numbers looked like.

**One disclosed limitation of season-level checkpoint granularity, found
by a real crash during this dispatch's own build/run, not by inspection:**
`_run_one_season`'s own body is not one atomic unit -- it makes many
independent real DB calls (staged game sims, up to two agency checkpoints,
nine cycles' worth of pulse/selection calls per arm) and this script only
checkpoints its own in-memory accumulators AFTER the whole season
finishes. A kill partway through one season's cycle loop leaves that
season's schedule/games/stats already real and persisted, and on resume
this script's own cycle loop replays that season's cycles from `cycle_
index=0` again -- which is safe and RNG-deterministic for the pulse/
selection calls themselves (same prior snapshot, same games, same salts,
same candidates and story content every time), but can call `agency_
service.run_agency_checkpoint` a second time for that one season's own
trade-deadline point if the crash landed after it first ran. This does
not corrupt the corpus's own schema or double-count anything THIS script
pools (a re-run cycle's stories are pooled exactly once, from that one
attempt's own return value) -- it can only, in the rare case where a kill
lands inside exactly one season's cycle loop, apply that ONE season's
motivation-ladder movement slightly more than Decision P4-11's nominal
"exactly two checkpoints a season" cadence. Disclosed rather than
engineered around, per this dispatch's own "don't over-engineer a
checkpoint system for a script whose real per-season cost is a few
minutes" guidance -- the failure mode is narrow, bounded to at most one
season, and does not touch the artifact's own internal consistency.

**Checkpointed by season** (HANDOFF's own standing rule for a run that
might cross this environment's ~10-minute external kill boundary): after
every season's cycles are recorded, the accumulated per-arm totals and the
number of seasons completed so far are pickled to `--checkpoint-path`
(default `artifacts/media_corpus_checkpoints/<rng_seed>_<seasons>.pkl`,
gitignored like every other `artifacts/` scratch file). Re-running the
identical command resumes from the last completed season rather than
restarting the league or re-simulating any season already recorded. A
season boundary, not a cycle boundary, is the checkpoint granularity --
matching HANDOFF's own account of why the exit-gate checkpoint wrapper
had to move from seed-level to season-level granularity (coarser than
season-level starved the environment's own kill window; finer than
season-level is not needed here since one season's real cost is well
under the ~10-minute mark, see this script's own timing output)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pickle
import time
from collections.abc import Mapping
from dataclasses import asdict as dc_asdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, game_repo, league_repo
from dba2.domain.game import GameType
from dba2.domain.media import StoryKind
from dba2.domain.media_facts import FACT_KEYS_BY_KIND, UNIVERSAL_FACT_KEYS, FactType
from dba2.domain.media_writer import FormatTier
from dba2.domain.phase import Phase
from dba2.managers.voice import engine as voice_engine
from dba2.media import select
from dba2.services import (
    agency_service,
    game_sim_service,
    gm_service,
    media_service,
    schedule_service,
    stats_service,
    strategy_service,
)
from tools.advance_dev_league_season import _cap_settings_for_season, advance_dev_league_season
from tools.dev_league_draft import ensure_draft, ensure_draft_class, place_selections
from tools.gm_harness.media_corpus import (
    KindOrVoiceSplit,
    MediaCorpusArm,
    MediaCorpusTotals,
    MediaCycleRecord,
    PairDepthBin,
)
from tools.gm_harness.metrics import _M34_DEFAULT_ESCALATION_THRESHOLD, m34_null_check_readings
from tools.seed_dev_league import seed_dev_league

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ARTIFACT_PATH = _REPO_ROOT / "artifacts" / "media_corpus.json"
_DEFAULT_CHECKPOINT_DIR = _REPO_ROOT / "artifacts" / "media_corpus_checkpoints"

# Decision P4-95's own nine-cycle cadence (Stage A census's own "one cycle"
# judgment call, cited verbatim in the design doc): TRADE_DEADLINE appears
# twice, matching the two real trade windows a season actually has.
_CYCLE_PHASES: tuple[Phase, ...] = (
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.TRADE_DEADLINE,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
)

# The fraction of the season's real schedule simmed BEFORE each cycle in
# `_CYCLE_PHASES` runs, index-for-index -- disclosed, not measured (no
# citation fixes when a real season's trade deadline falls at 60% games
# played; these are plausible round numbers, not census-derived). `0.0` at
# `PRESEASON` means no games are simmed before it (a real preseason has
# played none yet); `1.0` from `DRAFT_LOTTERY` on means the regular season
# is fully complete before any offseason cycle runs, matching real season
# structure. See `_run_one_season`'s own docstring/comment for WHY staging
# matters at all -- an atomic whole-season sim starves every continuous
# candidate producer, not just the offseason ones.
_SEASON_PROGRESS_BY_CYCLE: tuple[float, ...] = (0.0, 0.45, 0.60, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0)

# See this module's own docstring -- these six kinds have no real candidate
# producer wired into `run_media_pulse_cycle` today (`dba2/media/events.py`'s
# own module docstring, and `dba2/media/pulse.py::diff_pulse_snapshots`'s
# own three-kind scope). Reported, not hidden, in this script's own summary.
_KNOWN_UNREACHABLE_KINDS = frozenset(
    {
        StoryKind.TRADE_REACTION.value,
        StoryKind.TRADE_FALLOUT.value,
        StoryKind.SIGNING_REACTION.value,
        StoryKind.MILESTONE.value,
        StoryKind.AWARD_RACE.value,
        StoryKind.ROOKIE_WATCH.value,
    }
)


# ---------------------------------------------------------------------------
# Library-derived, generation-time-only lookups (Decision P4-102's "the
# library-derived scalars, computed ONCE by 20a from the loaded library").
# ---------------------------------------------------------------------------


def _matches_kind(template_kinds: tuple[str, ...], kind: str) -> bool:
    return not template_kinds or kind in template_kinds


# ---------------------------------------------------------------------------
# BLOCKING 2's own real, checkable pairwise-disjointness check -- a fix-
# round addition. `_ArmAccumulator.record_stories`'s corrected `k_dedicated`
# resolution (BLOCKING 1) assumes at most one dedicated template's triggers
# can ever hold for one real candidate on a given (persona, kind) pair; this
# is what makes that assumption a checked, load-time-verified fact rather
# than a trust-the-comment claim. Deliberately narrow (interval reasoning on
# ONE shared feature) rather than a general constraint solver -- this is the
# real shape of every dedicated-template-pair split this project's own
# authoring remedy (P4-96) has ever produced (the sign-split `streak_length`
# pairs), and it fails LOUDLY (never silently assumes disjointness) on any
# trigger shape it cannot reason about, per this fix round's own
# instruction.
#
# **Round-23 critic fix: feature-type-aware interval widening.** The
# original version applied INTEGER interval-widening (`gt -> value+1`,
# `lt -> value-1`) to ANY numeric feature, with no check that the feature is
# actually integer-typed -- unsound on a genuinely `FLOAT`-declared feature:
# `gt 0.5` widened to a closed lower bound of `1.5`, `lt 0.9` widened to a
# closed upper bound of `-0.1` -- both nonsensical for a continuous feature,
# and capable of certifying two genuinely OVERLAPPING triggers (both hold at
# `0.7`) as disjoint.
#
# **Re-derived from `FACT_KEYS_BY_KIND` (docs/design/media-v2-newsroom.md
# MV-1c.5, "Build order" step 3a) -- `StoryFrame`'s own field-type
# annotations were the only typed source in this codebase before the
# retype; the registry is the replacement, and it is strictly better: it is
# the SAME constant `media_service.py`'s load-time validators read, rather
# than a second, independently-derived one.** `INT`-declared keys keep the
# integer `+-1` widening path (sound: MV-1c.1 requires an `INT` fact's
# `value_number` to be integer-valued); `FLOAT`-declared keys take the
# open/closed interval path; a `TEXT`-declared key reaching `_feature_
# interval` still raises its existing loud `ValueError`, unchanged (a
# `TEXT` key's value is never numeric). **Empty on the current eleven
# `StoryKind` members' own registry** -- none of them declares a `FLOAT`
# key (MV-8's own "full shipped vocabulary" table lists only `INT`/`TEXT`),
# which reproduces this constant's existing PRACTICAL effect exactly:
# `StoryFrame`'s own four `float`-typed fields (`season_points_per_game`
# and friends) were never a real shipped trigger feature either (swept
# against `seeds/media_phrases.json` directly), so this set has always
# been empty in terms of what it actually changed at runtime.
# ---------------------------------------------------------------------------

_INTERVAL_OPS = frozenset({"gte", "gt", "lte", "lt", "eq"})

_FLOAT_FEATURES = frozenset(
    key
    for kind_map in FACT_KEYS_BY_KIND.values()
    for key, fact_type in kind_map.items()
    if fact_type is FactType.FLOAT
) | frozenset(key for key, fact_type in UNIVERSAL_FACT_KEYS.items() if fact_type is FactType.FLOAT)
"""Every fact key declared `FactType.FLOAT`, across `FACT_KEYS_BY_KIND` and
`UNIVERSAL_FACT_KEYS` -- see the module comment above for why this replaces
the pre-retype `StoryFrame`-field-annotation derivation. A hand-built,
two-key registry test (`tests/unit/tools/test_generate_media_corpus.py`)
asserts this constant contains every `FLOAT`-declared key and no
`INT`-declared one, so it cannot pass vacuously on the current library's
real, empty intersection alone."""


@dataclass(frozen=True)
class _Interval:
    """A `[lo, hi]` range with its own open/closed boundary flags -- the
    float path's `gt`/`lt` triggers are genuinely OPEN bounds (`actual >
    0.5`, never `actual >= 0.5`), which a bare `(lo, hi)` closed-interval
    tuple cannot represent without either widening (unsound for a
    continuous feature) or silently rounding the boundary case wrong."""

    lo: float
    lo_inclusive: bool
    hi: float
    hi_inclusive: bool


def _tighten_lower(
    lo: float, lo_inclusive: bool, value: float, inclusive: bool
) -> tuple[float, bool]:
    if value > lo:
        return value, inclusive
    if value == lo:
        return value, lo_inclusive and inclusive
    return lo, lo_inclusive


def _tighten_upper(
    hi: float, hi_inclusive: bool, value: float, inclusive: bool
) -> tuple[float, bool]:
    if value < hi:
        return value, inclusive
    if value == hi:
        return value, hi_inclusive and inclusive
    return hi, hi_inclusive


def _feature_interval(template: Any, feature: str) -> _Interval | None:
    """The `[lo, hi]` range `template`'s own triggers on `feature` imply,
    deliberately IGNORING any of its other triggers (on other features) --
    under-constraining like this can only WIDEN the computed interval, so
    two templates whose single-feature intervals are already provably
    disjoint stay disjoint once the ignored constraints are added back in;
    this function is therefore only ever used to prove disjointness, never
    to prove overlap. Returns `None` if `template` has no trigger on
    `feature` at all (unconstrained on this feature).

    **Feature-type-aware widening (round-23 fix).** `feature in _FLOAT_
    FEATURES` routes to plain open/closed interval logic (`gt`/`lt` produce
    a genuinely OPEN bound, no `+-1` shift) -- the only sound reading for a
    continuous value. Every other (integer-typed) feature keeps the
    original `+-1` widening (`gt -> value+1`, `lt -> value-1`, always
    closed/inclusive after widening), sound because `actual` is guaranteed
    integer-valued on an int-typed `StoryFrame` field.

    Raises `ValueError` -- never an uncaught `TypeError` -- if a trigger on
    `feature` uses an operator this narrow interval check cannot reason
    about (`in`), or if its value is not itself numeric (e.g. `request_
    state eq "trade_requested"`, a real trigger shape this library ships):
    fails loudly rather than silently assuming disjointness holds for a
    shape it never checked."""
    feature_triggers = [t for t in template.triggers if t.feature == feature]
    if not feature_triggers:
        return None
    is_float_feature = feature in _FLOAT_FEATURES
    lo, lo_inclusive = float("-inf"), True
    hi, hi_inclusive = float("inf"), True
    for trigger in feature_triggers:
        if trigger.op not in _INTERVAL_OPS:
            raise ValueError(
                f"media_phrases.json: template {template.id!r}'s trigger on "
                f"{feature!r} uses op {trigger.op!r} -- the load-time "
                "disjointness check only reasons about gte/gt/lte/lt/eq; "
                "extend _feature_interval before shipping a second dedicated "
                "template on a pair that needs this operator to prove "
                "disjointness"
            )
        value = trigger.value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"media_phrases.json: template {template.id!r}'s trigger on "
                f"{feature!r} has a non-numeric value {value!r} -- the "
                "load-time disjointness check only reasons about numeric "
                "gte/gt/lte/lt/eq triggers; extend _feature_interval before "
                "shipping a second dedicated template on a pair whose "
                "disjointness depends on a non-numeric feature"
            )
        if is_float_feature:
            if trigger.op == "gte":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value, True)
            elif trigger.op == "gt":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value, False)
            elif trigger.op == "lte":
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value, True)
            elif trigger.op == "lt":
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value, False)
            elif trigger.op == "eq":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value, True)
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value, True)
        else:
            if trigger.op == "gte":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value, True)
            elif trigger.op == "gt":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value + 1, True)
            elif trigger.op == "lte":
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value, True)
            elif trigger.op == "lt":
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value - 1, True)
            elif trigger.op == "eq":
                lo, lo_inclusive = _tighten_lower(lo, lo_inclusive, value, True)
                hi, hi_inclusive = _tighten_upper(hi, hi_inclusive, value, True)
    return _Interval(lo=lo, lo_inclusive=lo_inclusive, hi=hi, hi_inclusive=hi_inclusive)


def _intervals_disjoint(interval1: _Interval, interval2: _Interval) -> bool:
    """`interval1`/`interval2` are disjoint iff one ends strictly before the
    other starts -- "ends strictly before" means either a real gap
    (`hi < lo`) or a touching boundary where at least one side is OPEN
    (`hi == lo` but not both inclusive, e.g. `(..., 5)` open and `[5, ...)`
    closed share no actual value)."""
    if interval1.hi < interval2.lo:
        return True
    if interval1.hi == interval2.lo and not (interval1.hi_inclusive and interval2.lo_inclusive):
        return True
    if interval2.hi < interval1.lo:
        return True
    if interval2.hi == interval1.lo and not (interval2.hi_inclusive and interval1.lo_inclusive):
        return True
    return False


def _assert_pairwise_disjoint(persona_key: str, kind_value: str, templates: list[Any]) -> None:
    """For every pair of dedicated templates sharing one (persona, kind),
    tries EVERY feature both templates place an interval-reasoning trigger
    on and confirms AT LEAST ONE proves the pair's ranges do not overlap --
    "prove disjointness" only ever needs one feature to succeed, never
    agreement across all of them, so one shared feature that cannot prove
    disjointness must not short-circuit a different shared feature that can
    (round-23 critic fix: the prior version raised on the first,
    alphabetically-sorted shared feature it could not reason about, even
    when a different shared feature would have proven disjointness
    cleanly). Raises loudly -- never silently passes -- when NO shared
    feature can prove disjointness, whether because every one of them
    genuinely overlaps or because every one of them uses a trigger shape
    `_feature_interval` cannot reason about (or some mix of both): a future
    template edit that adds an overlapping trigger fails HERE, at load
    time, instead of silently corrupting `k_dedicated` downstream in
    `_ArmAccumulator.record_stories` the way the original, unverified
    "trigger disjointness" assumption did."""
    for i, t1 in enumerate(templates):
        for t2 in templates[i + 1 :]:
            # **MV-1c.6: an exact angle/tier set-disjointness branch runs
            # FIRST, before the interval reasoning below.** Two templates
            # are disjoint if their `angles` are both non-wildcard with an
            # empty intersection, or their `tiers` are -- set intersection
            # is exact, so this branch can never certify an overlapping
            # pair. No shipped template declares either dimension yet (a
            # later build step), so this is inert against the real library
            # today and exists so a future angle-split pair (the natural
            # way to author two templates for one `(persona, kind)` under
            # this design, MV-1c.6's own framing) is provably disjoint
            # without needing a shared trigger feature at all.
            if t1.angles and t2.angles and not (set(t1.angles) & set(t2.angles)):
                continue
            if t1.tiers and t2.tiers and not (set(t1.tiers) & set(t2.tiers)):
                continue

            shared_features = {t.feature for t in t1.triggers} & {t.feature for t in t2.triggers}
            disjoint = False
            unprovable_reasons: list[str] = []
            for feature in sorted(shared_features):
                try:
                    # Both templates are guaranteed to carry >= 1 trigger on
                    # `feature` here (that is exactly what membership in
                    # `shared_features`, the intersection of both templates'
                    # own trigger features, means) -- `_feature_interval`
                    # only ever returns `None` for a feature a template has
                    # NO trigger on, which cannot happen for either side of
                    # this loop, so neither call below can read `None`.
                    interval1 = _feature_interval(t1, feature)
                    interval2 = _feature_interval(t2, feature)
                except ValueError as exc:
                    unprovable_reasons.append(str(exc))
                    continue
                assert interval1 is not None and interval2 is not None
                if _intervals_disjoint(interval1, interval2):
                    disjoint = True
                    break
            if not disjoint:
                reason_detail = (
                    f" ({len(unprovable_reasons)} shared feature(s) could not be reasoned "
                    f"about at all: {'; '.join(unprovable_reasons)})"
                    if unprovable_reasons
                    else ""
                )
                raise ValueError(
                    "media_phrases.json: dedicated templates "
                    f"{t1.id!r} and {t2.id!r} (persona={persona_key!r}, "
                    f"kind={kind_value!r}) could not be PROVEN disjoint -- "
                    "by_pair_depth's own k_dedicated reconstruction "
                    "(_ArmAccumulator.record_stories) assumes exactly one "
                    "dedicated template's triggers can ever hold for one real "
                    "candidate on this pair; a second dedicated template here "
                    "requires a real, checkable disjoint trigger range, not "
                    f"just an assumption{reason_detail}"
                )


@dataclass(frozen=True)
class _LibraryLookups:
    """Every generation-time-only fact this script derives from a
    `LoadedMediaLibrary`, computed once and shared by both arms (the arms
    differ only in which templates a persona may draw, never in the
    kind/persona/fallback STRUCTURE itself -- `with_voices_disabled`
    touches `voices` and nothing else)."""

    is_fallback_template: Mapping[str, bool]
    fallback_template_id_by_kind: Mapping[str, str]
    dedicated_template_ids_by_pair: Mapping[tuple[str, str], tuple[str, ...]]
    template_by_id: Mapping[str, Any]
    ungated_kinds: tuple[str, ...]
    declared_fallback_weight_share: float


def _build_library_lookups(library: media_service.LoadedMediaLibrary) -> _LibraryLookups:
    """Decision P4-96's fallback predicate (`not template.voices`), Decision
    P4-103 Part B's `ungated_kinds`, and the two load-time assertions both
    decisions require -- raising loudly rather than emitting a bad
    artifact, per this dispatch's own instruction."""
    templates = library.templates

    # Decision P4-96: fallback-ness is `not template.voices`, asserted to
    # coincide with `not template.triggers and not template.voices` (the
    # STRUCTURAL fallback-coverage predicate `_validate_media_fallback_
    # coverage` already enforces at load time).
    is_fallback_template = {t.id: (not t.voices) for t in templates}
    mismatched = [
        t.id
        for t in templates
        if is_fallback_template[t.id] != (not t.triggers and not t.voices)
    ]
    if mismatched:
        raise ValueError(
            "media_phrases.json: Decision P4-96 requires `not template.voices` to "
            "coincide with `not template.triggers and not template.voices` for every "
            f"template -- mismatched: {sorted(mismatched)}"
        )

    fallback_template_id_by_kind: dict[str, str] = {}
    for kind in StoryKind:
        matches = [
            t.id
            for t in templates
            if is_fallback_template[t.id] and _matches_kind(t.kinds, kind.value)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"media_phrases.json: expected exactly one fallback template for "
                f"{kind.value!r}, found {sorted(matches)} -- load_media_library's own "
                "fallback-coverage check should have already made this unreachable"
            )
        fallback_template_id_by_kind[kind.value] = matches[0]

    # Decision P4-103 Part B: a kind is "ungated" iff every DEDICATED
    # template matching it ships empty triggers -- computed per kind,
    # DIRECTLY from `templates` (never from `dedicated_template_ids_by_pair`,
    # which used to have to come AFTER this in the original implementation
    # -- a real ordering artifact this fix round removes as a side effect:
    # this kind-level property never actually depended on which PERSONA
    # happens to carry a positive `story_kind_weights` entry for the kind,
    # only on the templates' own `voices`/`triggers` shape). Computed BEFORE
    # the per-pair guard below, which needs it to apply BLOCKING 2's
    # stricter, exactly-one rule to `ungated_kinds` specifically. The
    # "clean partition" assertion (no kind mixes gated and ungated dedicated
    # templates) is checked directly rather than assumed.
    ungated_kind_set: set[str] = set()
    for kind in StoryKind:
        dedicated_for_kind = [
            t
            for t in templates
            if not is_fallback_template[t.id] and _matches_kind(t.kinds, kind.value)
        ]
        gated = [t.id for t in dedicated_for_kind if t.triggers]
        ungated = [t.id for t in dedicated_for_kind if not t.triggers]
        if gated and ungated:
            raise ValueError(
                f"media_phrases.json: {kind.value!r} mixes gated ({sorted(gated)}) and "
                f"ungated ({sorted(ungated)}) dedicated templates -- Decision P4-103's "
                "ungated_kinds partition requires a clean split at the kind level"
            )
        if dedicated_for_kind and not gated:
            ungated_kind_set.add(kind.value)

    # At least one dedicated template per eligible (persona, kind) pair --
    # §30.2's own verified structural fact of the ORIGINAL shipped library
    # ("every one of the 21 has exactly one dedicated template, and no pair
    # has two"). **Decision P4-96's own remedy (aligning trigger thresholds
    # and adding sign-complementary streak templates) breaks the "exactly
    # one" half of that fact on purpose** -- `streak` now has two mutually
    # sign-exclusive dedicated templates for `beat_writer`/`analyst`/
    # `historian` (a winning-streak candidate can only ever match the
    # `gte` template, a losing-streak candidate only the `lte` one; no real
    # candidate is ever eligible for both).
    #
    # **BLOCKING 2 fix (this fix round; comment corrected in a later fix
    # round -- this no longer describes a stricter `ungated_kinds`-only
    # rule, see below).** The load-time guard below applies the SAME
    # relaxed `>= 1` rule to EVERY kind, `ungated_kinds` included, not a
    # stricter hard `== 1` for the six `ungated_kinds` -- `b`'s own
    # identifiability (Decision P4-103 Part B) only ever needed "exactly
    # one dedicated template can HOLD for one real candidate," never
    # "exactly one dedicated template EXISTS for the pair" (see the fuller
    # explanation a few lines below, at the `len(matches) < 1`/`> 1` guard
    # itself). Any pair with more than one dedicated template -- ungated or
    # not -- must pass a real, checkable pairwise-disjointness check
    # (`_assert_pairwise_disjoint`) rather than relying on an unverified
    # "the triggers are disjoint" comment -- see that function's own
    # docstring.
    #
    # **BLOCKING 1 fix (this fix round, `_ArmAccumulator.record_stories`):
    # `by_pair_depth`'s own `d = k_dedicated - k_fallback` state variable no
    # longer SUMS the running per-template use-count across every dedicated
    # id sharing the pair.** That summing was mathematically wrong: it is
    # only exact when a bucket's TRUE per-draw `k_dedicated` (the running
    # count of the ONE dedicated template actually eligible for THIS
    # candidate's own sign) happens to equal the sum, which fails the
    # moment one voice draws candidates of BOTH signs within one cycle
    # (e.g. a winning-streak story then a losing-streak story) -- the
    # second draw's true `d` uses `k_dedicated=0` (nothing has fired
    # against ITS OWN template yet), not the first draw's own count folded
    # in. The corrected reconstruction resolves the SPECIFIC dedicated
    # template whose triggers hold for each candidate's own frame (reusing
    # `dba2.managers.voice.engine._trigger_holds`, the same predicate the
    # real selection algorithm's own step 3 evaluates -- Decision P4-103's
    # own "no second seam" argument, now actually honored) and reads THAT
    # one template's own running count, never a sum.
    dedicated_template_ids_by_pair: dict[tuple[str, str], tuple[str, ...]] = {}
    for persona in library.personas:
        for kind_value, weight in persona.story_kind_weights.items():
            if weight <= 0.0:
                continue
            matches = [
                t.id
                for t in templates
                if t.voices == (persona.key,) and _matches_kind(t.kinds, kind_value)
            ]
            # **MV-1c.6 residual risk, closed here ("Build order" step 6).**
            # Revision 5's own text left this open: "whether `by_pair_
            # depth`'s own `d = k_dedicated - k_fallback` state variable is
            # still the right sufficient statistic once a (persona, kind)
            # pair can carry several angle-split templates is a question
            # about the estimator, not about the code." It is, and the
            # answer is yes: `record_stories` below already resolves the
            # SPECIFIC dedicated template a real candidate's own eligibility
            # holds for via `select.template_eligible` (which reads
            # `package.angle`, not just triggers) and reads THAT template's
            # own running count -- `b`'s own identifiability (the reason
            # `ungated_kind_set` pairs used to require EXACTLY one dedicated
            # template) only ever needed "exactly one dedicated template can
            # HOLD for one real candidate," never "exactly one dedicated
            # template EXISTS for this pair." `_assert_pairwise_disjoint`
            # already proves the former (by trigger interval OR by
            # angle/tier set-disjointness, MV-1c.6's own branch) for every
            # OTHER kind's `len(matches) > 1` case below -- calling it here
            # too, unconditionally (ungated or not), means an angle-split
            # ungated pair (e.g. `hot_take`/`trade_broken_promise`,
            # `PROMISE_BROKEN` vs `TRADE_MADE`) is exactly as safe as the
            # sign-split `streak` pairs the general path already allows,
            # with no second, ungated-specific disjointness rule to drift
            # out of sync with it.
            if len(matches) < 1:
                raise ValueError(
                    "media_phrases.json: expected at least one dedicated template for "
                    f"eligible pair (persona={persona.key!r}, kind={kind_value!r}), found "
                    "none -- by_pair_depth's own state variable is undefined without this "
                    "holding"
                )
            if len(matches) > 1:
                _assert_pairwise_disjoint(
                    persona.key, kind_value, [t for t in templates if t.id in matches]
                )
            dedicated_template_ids_by_pair[(persona.key, kind_value)] = tuple(sorted(matches))

    ungated_kinds = sorted(ungated_kind_set)
    template_by_id = {t.id: t for t in templates}

    # Decision P4-96/P4-102: `declared_fallback_weight_share`, computed
    # (never hardcoded) from the library's own per-template weights --
    # §30.2's verified structural fact ("every dedicated template weighs
    # 2.0; every fallback weighs 1.0") is asserted here rather than
    # assumed, so a future library edit that varies weights fails loudly
    # instead of silently reporting a stale constant.
    all_dedicated_ids = {
        template_id
        for ids in dedicated_template_ids_by_pair.values()
        for template_id in ids
    }
    dedicated_weights = {t.weight for t in templates if t.id in all_dedicated_ids}
    fallback_weights = {t.weight for t in templates if is_fallback_template[t.id]}
    if len(dedicated_weights) != 1 or len(fallback_weights) != 1:
        raise ValueError(
            "media_phrases.json: declared_fallback_weight_share assumes every dedicated "
            f"template shares one weight and every fallback shares another -- found "
            f"dedicated weights {sorted(dedicated_weights)}, fallback weights "
            f"{sorted(fallback_weights)}"
        )
    dedicated_weight = next(iter(dedicated_weights))
    fallback_weight = next(iter(fallback_weights))
    declared_fallback_weight_share = fallback_weight / (dedicated_weight + fallback_weight)

    return _LibraryLookups(
        is_fallback_template=is_fallback_template,
        fallback_template_id_by_kind=fallback_template_id_by_kind,
        dedicated_template_ids_by_pair=dedicated_template_ids_by_pair,
        template_by_id=template_by_id,
        ungated_kinds=tuple(ungated_kinds),
        declared_fallback_weight_share=declared_fallback_weight_share,
    )


# ---------------------------------------------------------------------------
# Mutable per-arm accumulator -- pickled between seasons for checkpointing.
# ---------------------------------------------------------------------------


@dataclass
class _ArmAccumulator:
    stories_total: int = 0
    fallback_stories: int = 0
    stories_by_kind: dict[str, int] = field(default_factory=dict)
    stories_by_kind_tier: dict[str, dict[str, int]] = field(default_factory=dict)
    """MV-14.4 item 0 (`docs/design/media-v2-newsroom.md`): the corpus recorded NO
    tier at all before this -- every "today's `ARTICLE` count" claim earlier in that
    doc's own revision history was inference, not measurement. `kind -> format_tier
    -> count`, incremented beside `stories_by_kind` from the same `publication.story.
    format_tier` read, never a second scoring pass."""
    fallback_by_kind: dict[str, int] = field(default_factory=dict)
    stories_by_voice: dict[str, int] = field(default_factory=dict)
    fallback_by_voice: dict[str, int] = field(default_factory=dict)
    by_voice_kind_template: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    by_pair_depth: dict[int, dict[str, int]] = field(default_factory=dict)
    cycles: list[MediaCycleRecord] = field(default_factory=list)

    def _depth_bin(self, d: int) -> dict[str, int]:
        return self.by_pair_depth.setdefault(
            d,
            {
                "selections": 0,
                "fallback_selections": 0,
                "ungated_kind_selections": 0,
                "ungated_kind_fallback_selections": 0,
                # Decision P4-107 item 6 -- additive, alongside the four
                # columns above, never replacing them (see `PairDepthBin`'s
                # own docstring for the general-vs-ungated-only relationship
                # between the two column pairs).
                "trigger_held_selections": 0,
                "trigger_held_fallback_selections": 0,
            },
        )

    def record_cycle(self, record: MediaCycleRecord) -> None:
        self.cycles.append(record)

    def record_stories(
        self,
        publications: tuple,
        *,
        lookups: _LibraryLookups,
    ) -> None:
        """Pools `publications` (one arm's real `Publication` tuple for one
        cycle, `media_service.MediaSelectionCycleResult.publications`) into
        every count this accumulator carries, including the `by_pair_depth`
        reconstruction (Decision P4-103, Part B) -- replaying the SAME
        `voice -> template_id` increment rule `media_service.run_media_
        selection_cycle` itself applies, starting from a fresh `{}` for
        this cycle (Decision P4-95/P4-29's own per-cycle reset), needing no
        new instrumentation.

        **docs/design/media-v2-newsroom.md MV-1c.5, "Build order" step 3b:
        the old `zip(stories, candidates, strict=True)` (BLOCKING 1's own
        fix, this file's prior revision) is DELETED, not repaired** -- a
        `Publication` already carries its own `story`/`scored` pair, so
        this function never re-derives which candidate produced which
        story. This is the replacement MV-1c.5's own text names: `.stories`
        can now be longer than the pulse cycle's own candidate count (a
        future band/profile group, step 6) or shorter (a future capped-out
        skip, also step 6) -- neither is possible in THIS build step
        (`band` is always empty and no caller supplies a non-empty
        `profile_candidates` yet, `MediaSelectionCycleResult`'s own
        docstring), but the carried-pairing shape is correct regardless,
        which is the whole point of building it now rather than waiting
        for step 6 to need it. The prior implementation needed no candidate
        data at all (it summed running counts across every dedicated
        template sharing a pair); the corrected reconstruction needs each
        candidate's own `StoryPackage` to resolve which SPECIFIC dedicated
        template's triggers actually hold for THAT candidate, the true
        sufficient statistic Decision P4-103's own derivation assumed."""
        running_use_counts: dict[str, dict[str, int]] = {}
        for publication in publications:
            story, scored = publication.story, publication.scored
            kind = story.story_kind.value
            voice = story.voice
            is_fallback = lookups.is_fallback_template[story.template_id]

            self.stories_total += 1
            if is_fallback:
                self.fallback_stories += 1
            self.stories_by_kind[kind] = self.stories_by_kind.get(kind, 0) + 1
            tier_counts = self.stories_by_kind_tier.setdefault(kind, {})
            tier_counts[story.format_tier] = tier_counts.get(story.format_tier, 0) + 1
            self.stories_by_voice[voice] = self.stories_by_voice.get(voice, 0) + 1
            if is_fallback:
                self.fallback_by_kind[kind] = self.fallback_by_kind.get(kind, 0) + 1
                self.fallback_by_voice[voice] = self.fallback_by_voice.get(voice, 0) + 1

            by_kind = self.by_voice_kind_template.setdefault(voice, {})
            by_template = by_kind.setdefault(kind, {})
            by_template[story.template_id] = by_template.get(story.template_id, 0) + 1

            dedicated_ids = lookups.dedicated_template_ids_by_pair.get((voice, kind), ())
            fallback_id = lookups.fallback_template_id_by_kind[kind]
            voice_counts = running_use_counts.get(voice, {})

            # **MV-1c.6, "Build order" step 3a: `held_ids` is now resolved
            # with `select.template_eligible` AND `engine.triggers_hold`
            # combined -- the same two-predicate survivor test `select_
            # media_template` itself computes, never `triggers_hold` alone.**
            # `template_eligible` is the "dimension_matches" half (kind/
            # voice/angle/tier/slot-presence); triggers remain a SEPARATE
            # evaluation (`engine.select_template`'s own step 3, unchanged,
            # MV-1c.1's own "engine.py is genuinely untouched" claim). The
            # moment a library declares `angles` (a later build step; the
            # current library declares none), two same-`(persona, kind)`
            # templates genuinely disjoint BY ANGLE but sharing no trigger
            # feature (both vacuously "hold" trigger-wise) can only be told
            # apart by the dimension check -- `triggers_hold` alone would
            # read both as "held," exactly the `AssertionError` two lines
            # below exists to catch, on a library that is perfectly
            # correct. Reading the SAME predicate combination the real
            # selection algorithm evaluates (rather than a partial,
            # independently-reimplemented one) is the identical "no second
            # seam" move Decision P4-103 Part A and P4-107 item 6 each
            # already made once. `tier` is a disclosed stand-in constant
            # (`FormatTier.BLURB`): the `MediaStory.format_tier` column
            # exists and is populated, and every story in this build step
            # is drawn at that one tier in effect (no shipped template
            # declares a non-wildcard `tiers`).
            package = scored.candidate.package
            trigger_view = select.PackageTriggerView(package)
            held_ids = [
                tid
                for tid in dedicated_ids
                if select.template_eligible(
                    lookups.template_by_id[tid], package, voice=voice, tier=FormatTier.BLURB
                )
                and voice_engine.triggers_hold(lookups.template_by_id[tid], trigger_view)
            ]
            if len(held_ids) > 1:
                raise AssertionError(
                    "more than one dedicated template's triggers held for one real "
                    f"candidate (voice={voice!r}, kind={kind!r}, held={sorted(held_ids)}) -- "
                    "the load-time pairwise-disjointness check (_assert_pairwise_disjoint) "
                    "should have made this unreachable"
                )
            k_dedicated = voice_counts.get(held_ids[0], 0) if held_ids else 0
            k_fallback = voice_counts.get(fallback_id, 0)
            d = k_dedicated - k_fallback

            bin_ = self._depth_bin(d)
            bin_["selections"] += 1
            if is_fallback:
                bin_["fallback_selections"] += 1
            if kind in lookups.ungated_kinds:
                bin_["ungated_kind_selections"] += 1
                if is_fallback:
                    bin_["ungated_kind_fallback_selections"] += 1

            # Decision P4-107 item 6 -- the general form of the two columns
            # just above: "trigger held" iff this SELECTION's own eligible
            # dedicated template's trigger held (`held_ids` non-empty),
            # true vacuously for every `ungated_kinds` selection (empty
            # `triggers` always holds, `triggers_hold`'s own docstring) and
            # measured per-selection for a gated kind. Additive, alongside
            # `ungated_kind_selections`/`ungated_kind_fallback_selections`,
            # never replacing them.
            if held_ids:
                bin_["trigger_held_selections"] += 1
                if is_fallback:
                    bin_["trigger_held_fallback_selections"] += 1

            # Advance the SAME per-(voice, cycle) running counts the real
            # service call already advanced internally, mirroring `run_
            # media_selection_cycle`'s own two-line increment exactly
            # (Decision P4-103, Part B's own "no second seam" argument).
            voice_counts = running_use_counts.setdefault(voice, {})
            voice_counts[story.template_id] = voice_counts.get(story.template_id, 0) + 1

    def to_arm(self) -> MediaCorpusArm:
        by_kind = {
            kind: KindOrVoiceSplit(
                stories=count, fallback_stories=self.fallback_by_kind.get(kind, 0)
            )
            for kind, count in self.stories_by_kind.items()
        }
        by_voice = {
            voice: KindOrVoiceSplit(
                stories=count, fallback_stories=self.fallback_by_voice.get(voice, 0)
            )
            for voice, count in self.stories_by_voice.items()
        }
        by_pair_depth = {
            str(d): PairDepthBin(
                selections=bin_["selections"],
                fallback_selections=bin_["fallback_selections"],
                ungated_kind_selections=bin_["ungated_kind_selections"],
                ungated_kind_fallback_selections=bin_["ungated_kind_fallback_selections"],
                trigger_held_selections=bin_["trigger_held_selections"],
                trigger_held_fallback_selections=bin_["trigger_held_fallback_selections"],
            )
            for d, bin_ in self.by_pair_depth.items()
        }
        # Decision P4-107 item 6's own docstring claim, checked rather than
        # merely asserted in prose: `ungated_kind_selections` is always a
        # SUBSET of `trigger_held_selections` in the same bin (an ungated
        # kind's trigger always holds vacuously), never larger and never
        # disjoint from it.
        for d, bin_ in by_pair_depth.items():
            if bin_.ungated_kind_selections > bin_.trigger_held_selections:
                raise AssertionError(
                    f"by_pair_depth[{d!r}]: ungated_kind_selections "
                    f"({bin_.ungated_kind_selections}) exceeds trigger_held_selections "
                    f"({bin_.trigger_held_selections}) -- every ungated-kind selection's "
                    "trigger holds vacuously, so this must always be a subset relationship"
                )
        return MediaCorpusArm(
            stories_total=self.stories_total,
            fallback_stories=self.fallback_stories,
            by_kind=by_kind,
            by_kind_tier={
                kind: dict(tier_counts) for kind, tier_counts in self.stories_by_kind_tier.items()
            },
            by_voice=by_voice,
            by_voice_kind_template=self.by_voice_kind_template,
            by_pair_depth=by_pair_depth,
            cycles=tuple(self.cycles),
        )


@dataclass
class _CheckpointState:
    league_id: int
    seasons_completed: int
    live: _ArmAccumulator
    control: _ArmAccumulator
    created_by_this_tool: bool
    # The `season` value used for the most recently completed season index,
    # or `None` before any season has completed. Needed on resume because
    # `advance_dev_league_season` runs in its own atomic transaction
    # AFTER a season's cycles are checkpointed -- a kill between the
    # checkpoint write and that transaction's commit leaves the league's
    # own `current_season` unadvanced, and re-deriving "the next season to
    # sim" from the DB's live value alone cannot tell that case apart from
    # "already advanced." Comparing against this field can.
    last_completed_season: int | None = None


_CHECKPOINT_SCHEMA_VERSION = 2
"""Bumped by MV-14.4 item 0 (`docs/design/media-v2-newsroom.md`): `_ArmAccumulator`
gained `stories_by_kind_tier`, a plain dataclass field with no `__setstate__` of its
own -- pickle restores an OLD checkpoint's `__dict__` verbatim, so a pre-bump pickle
would come back missing the new attribute entirely (an `AttributeError` on first
`.setdefault` call, not a default silently filling in), never a `NotFoundError`-shaped
failure a caller could catch. Folding this version into the checkpoint's own filename
means an old checkpoint is simply never FOUND by the new code -- `_load_checkpoint`
returns `None`, and the season restarts from scratch -- rather than being loaded and
crashing mid-season. Bump this again the next time `_ArmAccumulator`'s own shape
changes."""


def _checkpoint_path(checkpoint_dir: Path, *, rng_seed: int, seasons: int) -> Path:
    return checkpoint_dir / f"state_v{_CHECKPOINT_SCHEMA_VERSION}_{rng_seed}_{seasons}.pkl"


def _load_checkpoint(path: Path) -> _CheckpointState | None:
    if not path.exists():
        return None
    with path.open("rb") as f:
        return pickle.load(f)  # noqa: S301 -- this process's own scratch checkpoint, never untrusted input


def _save_checkpoint(path: Path, state: _CheckpointState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pkl.tmp")
    with tmp.open("wb") as f:
        pickle.dump(state, f)
    tmp.replace(path)


async def _ensure_cap_settings(pool, *, league_id: int, season: int) -> None:
    """A real, disclosed gap this script's own "call `run_agency_
    checkpoint` directly" judgment call ran into and needed to close, not
    a hypothetical worry: `agency_service.run_agency_checkpoint`'s own
    `load_gm_inputs` call requires a REAL, PERSISTED `league_cap_settings`
    row for `season` itself (`tools.advance_dev_league_season`'s own
    module docstring -- "the season being left should read as a real,
    historical fact from here on, not a moving projection"). `seed_dev_
    league` only ever writes one, for the league's own creation season;
    `advance_dev_league_season` persists one for the season it LEAVES,
    never the season it advances TO.

    **Called once, at the START of `_run_one_season`, before any cycle
    runs (fix round: moved here from the `Phase.TRADE_DEADLINE` branch)**
    -- `load_team_profile_packages`'s own `load_gm_inputs` call needs this
    same row from the season's very FIRST cycle (`Phase.PRESEASON`, index
    0) now that the live arm's `profile_candidates` are wired for real, not
    merely from the trade-deadline checkpoint at index 2 where this
    function's only caller used to live. Idempotent either way (checks
    first, no-ops if a row already exists), so calling it earlier changes
    nothing about what it does, only how soon the row exists. Reuses
    `_cap_settings_for_season` (real row if one exists, else the league's
    creation-season row projected forward) rather than reimplementing it,
    then persists the result (not merely projects it in memory) so every
    later real read of this season's cap settings sees a real row too."""
    if await league_repo.get_cap_settings(pool, league_id, season) is not None:
        return
    projected = await _cap_settings_for_season(pool, league_id, season)
    projected_kwargs = {
        k: v for k, v in dc_asdict(projected).items() if k not in ("league_id", "season")
    }
    await league_repo.create_cap_settings(
        pool, league_id=league_id, season=season, **projected_kwargs
    )


# ---------------------------------------------------------------------------
# One season's worth of real cycles.
# ---------------------------------------------------------------------------


async def _run_one_season(
    pool,
    *,
    league_id: int,
    season: int,
    league_rng_seed: int,
    live_library: media_service.LoadedMediaLibrary,
    control_library: media_service.LoadedMediaLibrary,
    lookups: _LibraryLookups,
    live_acc: _ArmAccumulator,
    control_acc: _ArmAccumulator,
    draft_rounds: int,
    draft_class_size: int,
) -> None:
    # Resume-safety (see also this module's own docstring section on the
    # real crash that found this): `generate_regular_season` raises rather
    # than regenerating an existing schedule, so scheduling only happens
    # once, guarded by the cheapest real signal available -- does this
    # `(league_id, season)` already have any games at all.
    existing_games = await game_repo.list_games_for_league_season(pool, league_id, season)
    if not existing_games:
        # Mirrors `tools.sim_dev_league_season`'s own composition
        # (`ensure_defaults_for_season` -> `generate_regular_season`), but
        # this script does NOT then call that tool's own `sim_scheduled_
        # games`/`rebuild_player_season_stats` in one shot -- see the
        # staged sim below and this module's own docstring for why a
        # whole-season-atomic sim silently starves the continuous-kind
        # candidate producers this corpus needs.
        await strategy_service.ensure_defaults_for_season(pool, league_id, season)
        await schedule_service.generate_regular_season(pool, league_id, season)
        existing_games = await game_repo.list_games_for_league_season(pool, league_id, season)
    max_game_index = max((g.game_index for g in existing_games), default=-1)

    # **Fix round (this dispatch): moved from the `Phase.TRADE_DEADLINE`
    # branch below to HERE, before any cycle runs.** `load_team_profile_
    # packages` (wired into the live arm's own cycle loop as of this fix
    # round, below) calls `gm_service.load_gm_inputs`, which requires a
    # REAL, persisted `league_cap_settings` row for THIS season -- the
    # identical requirement `_agency_checkpoint`'s own trade-deadline call
    # already had, but that call doesn't run until partway through the
    # season (cycle index 2). A fresh season's PRESEASON/REGULAR_SEASON
    # cycles (indices 0-1) would call `load_gm_inputs` before the old
    # call site ever ran, raising `NotFoundError` (caught by actually
    # running this script over a 2nd+ season, not by inspection: season 1
    # never surfaces this, since `seed_dev_league` already writes its own
    # creation-season row). `_ensure_cap_settings` is idempotent (checks
    # first, no-ops if a row already exists), so calling it once here
    # costs nothing extra for the trade-deadline checkpoint that still
    # needs it later this same season.
    await _ensure_cap_settings(pool, league_id=league_id, season=season)

    stats_rebuilt = False
    trade_deadline_checkpointed = False
    cycle_index = 0
    # **Fix round (2026-09-23 critic round): ONE map, not two.** MV-5
    # revision 5 says `subject_last_seen` is arm-scoped "by construction,"
    # meaning by the parameter boundary, never that this tool must maintain
    # two independently-tracked maps -- the prior fix round's own per-arm
    # maps inverted that intent: with each arm's novelty advanced from ITS
    # OWN publications, the two arms' scoring inputs diverge on more than
    # the `with_voices_disabled` channel the whole paired design (Decision
    # P4-97 ground (ii)) requires to be the ONLY difference, and a critic
    # reproduction confirmed it (0/54 cycles diverging on the corpus before
    # this bug, 54/54 after). The fix restores a single map, advanced ONLY
    # from the LIVE arm's own publications each cycle -- matching what
    # `season_service._run_media_cycle`'s real production caller actually
    # does (one arm, one real history) -- and handed to BOTH arms' `run_
    # media_selection_cycle` calls for that cycle, unchanged. `with_voices_
    # disabled` (the control arm's own perturbation) is the only channel
    # left to differ between the two arms, which is what MV-1c.4's "arm-
    # scoped by construction, no new column, no arm marker" language was
    # always describing -- the scoping is "this tool decides what to feed
    # each call," not "this tool must derive a separate value per arm."
    # Reset once per season, matching `media_service.load_subject_last_
    # seen`'s own real production query, which is season-scoped (`WHERE
    # ... season = $2` in `media_repo.get_subject_last_seen_cycles`).
    subject_last_seen: dict[tuple[str, int], int] = {}
    for phase, progress in zip(_CYCLE_PHASES, _SEASON_PROGRESS_BY_CYCLE, strict=True):
        # **The fix for this dispatch's own third major disclosed finding**
        # (see module docstring): simming the whole season BEFORE any
        # cycle runs made every cycle within one season read the identical
        # `current_snapshot` (games don't change between cycles), and the
        # very first cycle of every season reads no PRIOR snapshot either
        # (`media_repo.get_prior_pulse_snapshot_teams` is season-scoped by
        # design) -- so `STANDINGS_SURGE`/`STANDINGS_COLLAPSE`/`STREAK`
        # were structurally unreachable regardless of season count.
        # Simming in real, incrementally-larger chunks BEFORE each cycle
        # (`game_sim_service.sim_scheduled_games`'s own `through_game_
        # index`, already-shipped and reused, never reimplemented) gives
        # successive cycles WITHIN a season genuinely different `current_
        # snapshot`s to diff against, matching what a real per-season
        # cycle loop would actually see.
        if progress > 0.0 and max_game_index >= 0:
            through = min(max_game_index, round(max_game_index * progress))
            await game_sim_service.sim_scheduled_games(
                pool, league_id, season, through_game_index=through
            )
            if progress >= 1.0 and not stats_rebuilt:
                await stats_service.rebuild_player_season_stats(
                    pool, league_id, season, GameType.REGULAR
                )
                stats_rebuilt = True

        if phase is Phase.TRADE_DEADLINE and not trade_deadline_checkpointed:
            # Decision P4-11's own "exactly two checkpoints a season"
            # cadence -- see this module's own docstring for why this
            # script calls it directly rather than inventing a third
            # cadence of its own. Cap settings are already ensured above,
            # before the cycle loop -- this call site no longer needs its
            # own, since the season-scoped row this checkpoint requires is
            # guaranteed to exist by the time any cycle in this season runs.
            await agency_service.run_agency_checkpoint(
                pool, league_id=league_id, season=season, phase=Phase.TRADE_DEADLINE
            )
            trade_deadline_checkpointed = True

        result = await media_service.run_media_pulse_cycle(
            pool, league_id=league_id, season=season, cycle_index=cycle_index
        )
        # Ground (i) of Decision P4-97: the candidate set comes from
        # pulse/events/salience, none of which ever loads the library --
        # so ONE pulse-cycle call correctly feeds BOTH arms' selection
        # calls below, rather than calling it twice for an identical
        # result. **docs/design/media-v2-newsroom.md MV-1c.4, "Build order"
        # step 3b: `result.candidates` is now UNSCORED** -- each arm below
        # scores it independently (`run_media_selection_cycle`'s own new
        # first act), which is what makes the two arms' `candidates_scored`
        # counts genuinely per-arm rather than a shared read of one pulse
        # result, and is real today even though both libraries currently
        # produce the identical score (no arm-dependent scoring signal
        # exists yet).
        #
        # **Fix round (2026-09-23 critic round): ONE profile-package tuple,
        # composed ONCE, handed to BOTH arms -- not `()` on control and a
        # live-only load on live.** The prior fix round read MV-5 revision
        # 4 backwards: revision 4's own concern is "the profile packages
        # are composed by the CALLER and handed in" -- i.e. `load_team_
        # profile_packages` must be called exactly ONCE per cycle (never
        # once per arm), because `media_stories`' no-repeat rule derives
        # the profiled-team pool from that table and nothing on the row
        # distinguishes arms, so calling the LOADER twice in one cycle
        # would let the second call consume teams the first call already
        # claimed. It was never about the two arms publishing DIFFERENT
        # profile content -- revision 5's own stated goal is the opposite,
        # "the candidate set itself" must not become arm-dependent, and
        # feeding live a real load while control got `()` did exactly that
        # (on top of the `subject_last_seen` divergence below). One call,
        # one tuple, passed unchanged to both `run_media_selection_cycle`
        # calls. **Honest disclosure this requires:** the control arm now
        # also "publishes" the same `TEAM_PROFILE` stories as the live arm
        # in its own accounting -- correct and expected, not a bug: the
        # control arm is a single-channel-perturbation measurement device
        # (Decision P4-97), not a self-consistent alternate league, and it
        # already shares the RNG seed and the underlying league/pulse
        # result with the live arm for the identical reason.
        # docs/design/media-v2-newsroom.md MV-11 / "Build order" step 7.5a:
        # the SAME `LeagueSnapshot`/`GmContext` `load_team_profile_packages`
        # needs is loaded ONCE here and handed to it AND to `load_event_
        # candidates` -- mirroring `season_service._run_media_cycle`'s own
        # real production wiring exactly, so this corpus generator actually
        # exercises the eight newly-wired detectors rather than silently
        # staying on the pre-MV-11 eight-kind pool. `_ensure_cap_settings`
        # above already guarantees this call never raises here (unlike the
        # production caller, which tolerates a missing pre-trade-deadline
        # row).
        league_snapshot, gm_context = await gm_service.load_gm_inputs(
            pool, league_id=league_id, season=season
        )
        profile_candidates = await media_service.load_team_profile_packages(
            pool,
            league_id=league_id,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            quota=live_library.team_profile_quota,
            league_snapshot=league_snapshot,
            gm_context=gm_context,
        )
        team_names = {
            team_snapshot.id: f"{team_snapshot.team.city} {team_snapshot.team.name}"
            for team_snapshot in league_snapshot.teams
        }
        player_names = {
            roster_player.player.id: (
                f"{roster_player.player.first_name} {roster_player.player.last_name}"
            )
            for team_snapshot in league_snapshot.teams
            for roster_player in team_snapshot.roster
        }
        event_candidates = await media_service.load_event_candidates(
            pool,
            league_id=league_id,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            league_snapshot=league_snapshot,
            gm_context=gm_context,
            current_snapshot=result.snapshot,
            team_names=team_names,
            player_names=player_names,
        )
        all_candidates = (*result.candidates, *event_candidates)
        live_result = await media_service.run_media_selection_cycle(
            pool,
            league_id=league_id,
            league_rng_seed=league_rng_seed,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            candidates=all_candidates,
            library=live_library,
            use_counts={},
            profile_candidates=profile_candidates,
            subject_last_seen=subject_last_seen,
        )
        control_result = await media_service.run_media_selection_cycle(
            pool,
            league_id=league_id,
            league_rng_seed=league_rng_seed,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            candidates=all_candidates,
            library=control_library,
            use_counts={},
            profile_candidates=profile_candidates,
            subject_last_seen=subject_last_seen,
        )

        # **MV-1c.4's own callers table, `:1055-1066`:** `candidates_scored`/
        # `candidates_above_floor`/`candidates_submitted` now read EACH
        # ARM'S OWN selection result, never the shared pulse result -- both
        # are per-arm quantities as of this build step, since each arm
        # scores independently.
        for acc, arm_result in ((live_acc, live_result), (control_acc, control_result)):
            acc.record_cycle(
                MediaCycleRecord(
                    league_id=league_id,
                    season=season,
                    phase=phase.value,
                    cycle_index=cycle_index,
                    candidates_scored=arm_result.candidates_scored,
                    candidates_above_floor=arm_result.candidates_above_floor,
                    candidates_submitted=arm_result.plan_size,
                    stories_persisted=len(arm_result.stories),
                    capped_out=arm_result.capped_out,
                    band_published=arm_result.band_published,
                    profiles_published=arm_result.profiles_published,
                )
            )
            acc.record_stories(arm_result.publications, lookups=lookups)

        # **Fix round (2026-09-23 critic round): advance the ONE `subject_
        # last_seen` map from the LIVE arm's own publications ONLY, after
        # BOTH arms' selection calls already read its pre-cycle state
        # above.** The prior fix round advanced two separate maps, one per
        # arm, from each arm's own publications -- exactly the bug this
        # round closes (see the map's own declaration comment). Advancing
        # from the live arm alone matches what a real production caller
        # (`season_service._run_media_cycle`, which has only one arm) would
        # ever observe, and is what keeps the control arm's own scoring
        # inputs identical to live's except for `with_voices_disabled`.
        # `subject_last_seen_map[identity] = cycle_index` is a plain
        # assignment, not a `max()` -- **Fix (2026-09-23): the old `max(
        # subject_last_seen_map.get(identity, cycle_index), cycle_index)`
        # was dead code**, not a defensive one: its own `.get` default
        # equals the value it is maxed against, so the expression always
        # evaluated to `cycle_index` regardless of any prior stored value.
        # Cycles run in strictly increasing `cycle_index` order within a
        # season (the `for phase, progress in zip(_CYCLE_PHASES, ...)`
        # loop above never revisits an earlier index), so "last seen" for
        # any identity touched this cycle really is just "this cycle" --
        # a plain assignment says that directly instead of writing a
        # comparison that can never do anything else.
        for publication in live_result.publications:
            subject = publication.scored.candidate.package.subject
            identity = media_service._subject_identity(subject)
            subject_last_seen[identity] = cycle_index

        if phase is Phase.DRAFT:
            # Stage D step 22 (design doc §37, Decision P4-113 item 3).
            # AFTER this cycle's own pulse/selection calls above, never
            # before -- the draft needs THIS cycle's own already-returned
            # `result.snapshot` for its pick order (Option C, zero extra
            # I/O, no lottery -- `dev_league_draft.ensure_draft`'s own
            # docstring), and placing rookies any earlier would let the
            # current season's own draft class leak into the CURRENT
            # season's own candidate union, which `load_rookie_watch_
            # candidates` is deliberately built not to do (Decision
            # P4-111's `season - 1` ruling). Placement here cannot perturb
            # season `season`'s own games either -- they were fully simmed
            # at cycle index 4, one cycle before this one runs.
            await ensure_draft_class(
                pool,
                league_id=league_id,
                season=season,
                class_size=draft_class_size,
                rng_seed=league_rng_seed,
            )
            await ensure_draft(
                pool,
                league_id=league_id,
                season=season,
                snapshot=result.snapshot,
                rounds=draft_rounds,
            )
            # `execute_draft` (Decision P4-113 item 1, Option C) is
            # `run_phase_hook`'s own `Phase.DRAFT` branch, extracted --
            # this driver reuses it directly rather than hand-
            # reimplementing `_eligible_draft_picks_sorted`'s B1/B2/
            # tiebreak fixes or the pointer arithmetic. A fresh `load_gm_
            # inputs` call here (not `result`/`ctx` from any earlier
            # branch -- this cycle loop never loads one otherwise) matches
            # `run_phase_hook`'s own DRAFT branch, which always loads its
            # own snapshot/ctx rather than reusing a caller's.
            draft_snapshot, draft_ctx = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=season
            )
            await gm_service.execute_draft(
                pool,
                league_id=league_id,
                season=season,
                snapshot=draft_snapshot,
                ctx=draft_ctx,
            )
            # Decision P4-120 (round 25): placement is re-derived from the
            # season's real, PERSISTED `draft_selections` rows, never
            # gated on `execute_draft`'s own return value. A resumed
            # `execute_draft` call for a season whose draft already fully
            # executed correctly returns `None` (nothing left to draft),
            # but that must not mean placement is skipped -- a kill
            # between `execute_draft`'s own commit and `place_selections`
            # completing on an earlier attempt would otherwise strand
            # that season's rookies as drafted-but-never-placed forever
            # (see `place_selections`'s own docstring for the undisclosed
            # knock-on this closes: a stranded, still-`DRAFT_ELIGIBLE`
            # prospect being re-offered to every later season's draft).
            # `place_selections` is idempotent, so re-placing an
            # already-placed selection every DRAFT cycle is a cheap no-op.
            draft_row = await draft_repo.get_draft_for_season(
                pool, league_id, season
            )
            if draft_row is not None:
                selections = await draft_repo.list_draft_selections_for_draft(
                    pool, draft_row.id
                )
                if selections:
                    await place_selections(
                        pool,
                        league_id=league_id,
                        season=season,
                        selections=selections,
                    )

        cycle_index += 1


# ---------------------------------------------------------------------------
# The null-measurement step Decision P4-97 requires (reused, not
# reimplemented -- `tools.gm_harness.metrics._within_cohort_binomial_thin_
# tv` already ships this exact procedure).
# ---------------------------------------------------------------------------


def _report_null_check(
    live_acc: _ArmAccumulator, control_acc: _ArmAccumulator, *, rng_seed: int
) -> dict:
    """M34's own cohort pair (`analyst`/`historian`), restricted to their
    three shared kinds (`streak`/`milestone`/`rookie_watch`, Decision
    P4-97) -- the SAME restriction the gated statistic itself would apply,
    so the reported `n` matches what a real M34 reading would see. This
    does not gate anything in 20a (that is 20b's job); it only surfaces
    the numbers Decision P4-105/P4-108 say 20a's own output must show.

    **Decision P4-108: calls `tools.gm_harness.metrics.m34_null_check_
    readings` -- the SAME shared implementation `compute_m34`'s real gate
    1 calls, on BOTH arms, with the SAME `rng_seed` this run records into
    the artifact as `league_rng_seed` (the caller's own contract; see
    `generate_media_corpus`, below).** Round 21's finding was that this
    function used to compute the live arm's own null with a hand-rolled
    loop that never read the control arm at all, while `compute_m34`'s
    real gate read a differently-seeded control-only version -- two
    independent, silently-disagreeing implementations of one conceptual
    check. There is now exactly one implementation; this function is a
    disclosure-only caller of it, never a second one."""
    shared_kinds = frozenset(
        {StoryKind.STREAK.value, StoryKind.MILESTONE.value, StoryKind.ROOKIE_WATCH.value}
    )
    readings = m34_null_check_readings(
        live_acc.by_voice_kind_template, control_acc.by_voice_kind_template,
        cohort_a="analyst", cohort_b="historian",
        restrict_to_kinds=shared_kinds,
        seed=rng_seed, escalation_threshold=_M34_DEFAULT_ESCALATION_THRESHOLD,
    )
    result: dict[str, dict] = {}
    for arm, by_cohort in readings.items():
        for cohort, reading in by_cohort.items():
            result[f"{arm}_{cohort}"] = {
                "arm": arm,
                "cohort": cohort,
                "k": reading.k,
                "n": reading.n,
                "within_cohort_null_tv": reading.null,
                "failed": reading.failed,
                "fail_reason": reading.fail_reason,
            }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def generate_media_corpus(
    *,
    seasons: int,
    rng_seed: int,
    players_per_team: int,
    checkpoint_dir: Path,
    out_path: Path,
    keep_league: bool,
    league_id: int | None,
    draft_rounds: int,
    draft_class_size: int,
) -> MediaCorpusTotals:
    if seasons < 1:
        raise ValueError("seasons must be >= 1")
    if draft_rounds < 1:
        raise ValueError("draft_rounds must be >= 1")
    if draft_class_size < 1:
        raise ValueError("draft_class_size must be >= 1")

    pool = await get_pool()

    library = media_service.load_media_library()
    lookups = _build_library_lookups(library)
    control_library = media_service.with_voices_disabled(library)
    library_bytes = media_service.DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.read_bytes()
    library_sha = hashlib.sha256(library_bytes).hexdigest()

    checkpoint_path = _checkpoint_path(checkpoint_dir, rng_seed=rng_seed, seasons=seasons)
    state = _load_checkpoint(checkpoint_path)
    if state is None:
        created_by_this_tool = league_id is None
        if league_id is None:
            league_id = await seed_dev_league(
                discord_guild_id=rng_seed,
                name=f"media-corpus-{rng_seed}",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=rng_seed,
                players_per_team=players_per_team,
            )
        state = _CheckpointState(
            league_id=league_id,
            seasons_completed=0,
            live=_ArmAccumulator(),
            control=_ArmAccumulator(),
            created_by_this_tool=created_by_this_tool,
        )
    else:
        print(
            f"Resuming from checkpoint {checkpoint_path}: "
            f"{state.seasons_completed}/{seasons} seasons already recorded "
            f"(league_id={state.league_id})."
        )

    league = await league_repo.get_league(pool, state.league_id)
    if league is None:
        raise ValueError(f"checkpoint names league_id={state.league_id}, which no longer exists")
    current_season = league.current_season

    # Resume-safety reconciliation: `advance_dev_league_season` runs in its
    # own atomic transaction AFTER a season's cycles are checkpointed, so a
    # kill between the two can leave the league's own `current_season`
    # un-advanced even though this tool's own state says that season is
    # done. Detected by comparing the DB's live value against the season
    # this state last recorded cycles for -- if they match, the advance
    # never (successfully) ran, and this reconciles it before the loop
    # below tries to sim a season that already has a schedule.
    if (
        0 < state.seasons_completed < seasons
        and state.last_completed_season is not None
        and current_season == state.last_completed_season
    ):
        print(
            f"Reconciling: season {current_season} was already recorded but the league was "
            "never advanced past it (a kill between the two) -- advancing now."
        )
        current_season, _aged, _retired = await advance_dev_league_season(
            league_id=state.league_id, rng_seed=rng_seed
        )

    for season_offset in range(state.seasons_completed, seasons):
        t0 = time.perf_counter()
        await _run_one_season(
            pool,
            league_id=state.league_id,
            season=current_season,
            league_rng_seed=rng_seed,
            live_library=library,
            control_library=control_library,
            lookups=lookups,
            live_acc=state.live,
            control_acc=state.control,
            draft_rounds=draft_rounds,
            draft_class_size=draft_class_size,
        )
        elapsed = time.perf_counter() - t0
        state.seasons_completed = season_offset + 1
        state.last_completed_season = current_season
        _save_checkpoint(checkpoint_path, state)
        print(
            f"season {current_season} (index {season_offset + 1}/{seasons}) done in "
            f"{elapsed:.1f}s -- checkpointed."
        )
        if season_offset < seasons - 1:
            current_season, _aged, _retired = await advance_dev_league_season(
                league_id=state.league_id, rng_seed=rng_seed
            )

    null_check = _report_null_check(state.live, state.control, rng_seed=rng_seed)
    print(
        "M34 within-cohort binomial-thin null (analyst/historian, 3 shared kinds, "
        "Decision P4-108 -- all 4 arm x cohort readings, live AND control):"
    )
    for key, reading in null_check.items():
        verdict = "FAIL" if reading["failed"] else "pass"
        print(
            f"  {key}: k={reading['k']} n={reading['n']} "
            f"null_tv={reading['within_cohort_null_tv']} -> {verdict}"
            + (f" ({reading['fail_reason']})" if reading["failed"] else "")
        )

    totals = MediaCorpusTotals(
        schema_version=1,
        library_sha=library_sha,
        library_path=str(media_service.DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.relative_to(_REPO_ROOT)),
        league_rng_seed=rng_seed,
        seed_root=f"generate_media_corpus:{rng_seed}",
        leagues=1,
        seasons=seasons,
        generated_at=datetime.now(UTC).isoformat(),
        # **Fix round (this dispatch): `TOP_K_PER_CYCLE` is no longer the
        # real budget for any cycle-bearing phase** -- MV-5's own
        # `TOP_K_BY_PHASE` covers all nine cycle-bearing phases, so
        # `TOP_K_PER_CYCLE` is only ever a fallback for a phase not in that
        # table (unreachable for a real corpus run). Recording it here used
        # to pin a budget that governed zero real cycles. `MediaCorpusTotals.
        # top_k_per_cycle` is a single, corpus-wide int (Decision P4-102's
        # schema has no per-cycle/per-phase field for it), so the real,
        # least-invasive fix is the real per-phase table's own MAXIMUM --
        # the tightest single number that is still a true upper bound on
        # every cycle-bearing phase's own real per-phase K.
        top_k_per_cycle=max(media_service.salience.TOP_K_BY_PHASE.values()),
        salience_floor=media_service.salience.SALIENCE_FLOOR,
        repeat_penalty=library.repeat_penalty,
        declared_fallback_weight_share=lookups.declared_fallback_weight_share,
        persona_exclusivity_floor=library.persona_exclusivity_floor,
        ungated_kinds=lookups.ungated_kinds,
        live=state.live.to_arm(),
        control=state.control.to_arm(),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(totals.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")

    if not keep_league and state.created_by_this_tool:
        await pool.execute("DELETE FROM leagues WHERE id = $1", state.league_id)
        print(f"Deleted league {state.league_id} (pass --keep-league to retain it).")
    elif not keep_league:
        print(
            f"league_id={state.league_id} was supplied by the caller, not created here -- "
            "not deleting it."
        )

    if checkpoint_path.exists():
        checkpoint_path.unlink()

    return totals


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Stage D step 20a's committed media corpus artifact "
            "(Decision P4-95/P4-102/P4-103) from a real, DB-backed dev league."
        )
    )
    parser.add_argument("--seasons", type=int, default=6)
    parser.add_argument("--rng-seed", type=int, default=424242)
    parser.add_argument("--players-per-team", type=int, default=15)
    parser.add_argument("--league-id", type=int, default=None, help="Reuse an existing dev league.")
    parser.add_argument("--keep-league", action="store_true")
    parser.add_argument("--out", type=Path, default=_DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--checkpoint-dir", type=Path, default=_DEFAULT_CHECKPOINT_DIR)
    # Stage D step 22 (design doc §37, Decision P4-117 JC-2, RULED BY THE
    # USER): two rounds (60 picks) by default, against the architect's own
    # one-round recommendation -- real NBA-draft realism over M35's
    # instrument-stability margin. `--draft-class-size` defaults to
    # `rounds * 30` (P4-113 item 4's own rule: class size must equal pick
    # count by construction, §37.1 item 7 -- a leftover prospect is frozen
    # forever and would be re-offered, un-aged, to every later draft) --
    # computed from the PARSED `--draft-rounds` value below, not a second
    # independent default, so passing only `--draft-rounds` still keeps
    # the two in sync.
    parser.add_argument("--draft-rounds", type=int, default=2)
    parser.add_argument(
        "--draft-class-size",
        type=int,
        default=None,
        help="Defaults to --draft-rounds * 30 (one class slot per pick).",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    draft_class_size = (
        args.draft_class_size if args.draft_class_size is not None else args.draft_rounds * 30
    )
    try:
        await generate_media_corpus(
            seasons=args.seasons,
            rng_seed=args.rng_seed,
            players_per_team=args.players_per_team,
            checkpoint_dir=args.checkpoint_dir,
            out_path=args.out,
            keep_league=args.keep_league,
            league_id=args.league_id,
            draft_rounds=args.draft_rounds,
            draft_class_size=draft_class_size,
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
