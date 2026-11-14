"""`MediaCorpusTotals` -- Decision P4-102 (amended by Decision P4-103), the
one authoritative schema for Stage D step 20a's committed corpus artifact
(`artifacts/media_corpus.json`) and the only thing step 20b's four
`compute_m33`-`compute_m36` functions will ever be allowed to read.

docs/design/phase-4-media-and-player-agency.md §30.3 (Decisions P4-96
through P4-99), §32 (Decisions P4-102/P4-103).

**Pure -- no `dba2.data`, no `Executor`, no `await`.** This module is
imported by TWO callers on opposite sides of Decision P4-31's/`tools/
gm_harness/season.py`'s own zero-I/O posture: `tools/generate_media_
corpus.py` (20a, real Postgres, writes the artifact) and (in a later
dispatch) `tools/gm_harness/metrics.py` (20b, zero I/O, reads it). Neither
caller may be forced to acquire the other's dependencies just to share one
dataclass, which is why this type lives here rather than beside either
caller -- `tests/unit/test_layering.py::test_nothing_under_dba2_imports_
the_gm_harness` already forbids the reverse edge (nothing under `dba2/`
may import anything under `tools/gm_harness/`); this module adds nothing
new to that rule, it just needs to not need `dba2.data`/`asyncpg`/`await`
itself for `metrics.py`'s own zero-I/O import list to stay true once it
starts importing this module.

**Counts and keys only -- never shares, ratios, or per-story booleans**
(Decision P4-102's rule (iii)). Every derivation happens either at
generation time (`tools/generate_media_corpus.py`, where the library
actually is) or at metric time (`compute_m33`-`compute_m36`, where the
bands are) -- never in both places, and never inside this module, which
only defines the shape and its JSON round-trip.

**`ungated_kinds` is arm-invariant and lives at the top level, not per
arm** (Decision P4-103, Part B): `with_voices_disabled` touches `voices`
and nothing else, so the set of kinds whose dedicated templates are all
trigger-free is identical in both arms and is recorded once.

**`control` may be `null`, and only `null` -- never a half-populated
arm.** A `MediaCorpusArm` with any real data always carries every field
this schema defines; the shape does not vary by which fields happen to be
zero."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KindOrVoiceSplit:
    """One `by_kind`/`by_voice` entry (Decision P4-96/P4-102, M33's own
    per-kind/per-voice breakdown) -- a real story/fallback-story count
    split, never a pre-computed share (rule (iii): `compute_m33` divides,
    this module never does)."""

    stories: int
    fallback_stories: int

    def to_json(self) -> dict:
        return {"stories": self.stories, "fallback_stories": self.fallback_stories}

    @staticmethod
    def from_json(payload: Mapping[str, object]) -> KindOrVoiceSplit:
        return KindOrVoiceSplit(
            stories=int(payload["stories"]), fallback_stories=int(payload["fallback_stories"])
        )


@dataclass(frozen=True)
class PairDepthBin:
    """One `by_pair_depth[d]` entry (Decision P4-103, Part B) -- `d =
    k_dedicated - k_fallback`, the repeat penalty's own real state
    variable and a sufficient statistic for `P(fallback | d)` (§32's own
    derivation: the common `0.3^k_f` factor cancels). Four counts, never a
    ratio: `selections`/`fallback_selections` are this bin's totals across
    every eligible-pair selection (gated and ungated kinds alike);
    `ungated_kind_selections`/`ungated_kind_fallback_selections` are the
    SAME bin restricted to `ungated_kinds`, where the trigger-miss channel
    is structurally absent (`m = 0` by construction) and the observed
    fallback share therefore IS `b`, with nothing to invert.

    **Decision P4-107 item 6 -- `trigger_held_selections`/`trigger_held_
    fallback_selections`, added alongside the four fields above, not
    replacing them.** The general form of the `ungated_kind_*` columns:
    `ungated_kind_selections` is exactly "selections whose survivor set was
    `{dedicated, fallback}` because the KIND has no gated dedicated
    template at all" (true by construction for all six `ungated_kinds`);
    `trigger_held_selections` is "selections whose survivor set was
    `{dedicated, fallback}` because THIS selection's own dedicated
    template's trigger held" -- evaluated per selection (`dba2.managers.
    voice.engine.triggers_hold`, never a second, hand-reimplemented trigger
    check) against every kind, gated or not. An `ungated_kinds` selection's
    trigger always holds vacuously (`triggers_hold` on an empty-`triggers`
    template is `True` by definition), so `ungated_kind_selections` is
    always a SUBSET of `trigger_held_selections` in the same bin, never
    disjoint from it and never larger. `d = k_dedicated - k_fallback` is
    the SAME sufficient statistic either way (§32's own derivation: the
    common `0.3^k_f` factor cancels regardless of why the survivor set was
    `{dedicated, fallback}`), so `trigger_held_fallback_selections /
    trigger_held_selections` at a given `d` measures the identical `b_d`
    the `ungated_kind_*` columns measure, over a strictly larger, real
    population."""

    selections: int
    fallback_selections: int
    ungated_kind_selections: int
    ungated_kind_fallback_selections: int
    trigger_held_selections: int = 0
    trigger_held_fallback_selections: int = 0

    def to_json(self) -> dict:
        return {
            "selections": self.selections,
            "fallback_selections": self.fallback_selections,
            "ungated_kind_selections": self.ungated_kind_selections,
            "ungated_kind_fallback_selections": self.ungated_kind_fallback_selections,
            "trigger_held_selections": self.trigger_held_selections,
            "trigger_held_fallback_selections": self.trigger_held_fallback_selections,
        }

    @staticmethod
    def from_json(payload: Mapping[str, object]) -> PairDepthBin:
        return PairDepthBin(
            selections=int(payload["selections"]),
            fallback_selections=int(payload["fallback_selections"]),
            ungated_kind_selections=int(payload["ungated_kind_selections"]),
            ungated_kind_fallback_selections=int(payload["ungated_kind_fallback_selections"]),
            trigger_held_selections=int(payload.get("trigger_held_selections", 0)),
            trigger_held_fallback_selections=int(
                payload.get("trigger_held_fallback_selections", 0)
            ),
        )


@dataclass(frozen=True)
class MediaCycleRecord:
    """One real cycle -- Decision P4-102's rule (i): per-cycle, never
    pre-aggregated to `Phase` (M35's corrected statistic aggregates to
    `Phase` at METRIC time; pre-aggregating here would force a corpus
    regeneration the day §30.6's per-phase-`K` question needs answering).
    `(league_id, season, phase, cycle_index)` is the full cycle key --
    M36 clause (b)'s duplicate-key check is a group-by on this tuple.
    `candidates_scored`/`candidates_above_floor` come from Decision
    P4-103's `MediaPulseCycleResult`, never re-derived; `candidates_
    submitted`/`stories_persisted` are `len(top_candidates)`/`len(stories)`
    as `tools/generate_media_corpus.py` actually observes them.

    **`capped_out` added in a later fix round (M36's own conservation-
    invariant restatement).** Recorded per cycle for **this record's own
    arm** (`live` or `control` -- whichever arm's `MediaSelectionCycleResult
    .capped_out` this record mirrors, byte for byte, never re-derived; a
    stale comment here used to say "this cycle's live-arm call" -- false as
    of the fix round that wired real per-arm caps into both arms, corrected
    2026-09-23). Decision P4-99's own "0 violations" clause (a)
    (`stories_persisted != candidates_submitted`) was written before real
    never-waived per-persona caps existed (`run_media_selection_cycle`'s
    own MV-1c.3 cap loop); a capped candidate legitimately makes the two
    counts diverge by design, which the OLD formula could not tell apart
    from a real bug (a candidate vanishing for an unaccounted reason).
    `stories_persisted == candidates_submitted - capped_out` is a hard
    identity, checkable per cycle -- but see `compute_m36`'s own docstring
    (tools/gm_harness/metrics.py) for why, over a corpus produced by `tools/
    generate_media_corpus.py`, this identity holds BY CONSTRUCTION (all
    three values are read off the one `MediaSelectionCycleResult` object
    whose own producing loop has only two exits) rather than by measuring
    anything -- it cannot, on its own, detect the real bug class Decision
    P4-99 was written to catch. Defaults to `0` so every pre-existing
    corpus/fixture that never recorded it (the byte-for-byte identity above
    already held for them, since `capped_out` did not exist until the
    never-waived cap loop did) round-trips unchanged.

    **`band_published`/`profiles_published` added in a later fix round
    (2026-09-23 critic round, Fix 3).** Mirror `MediaSelectionCycleResult
    .band_published`/`.profiles_published` for this record's own arm, byte
    for byte -- both already existed on that result type as of "Build
    order" step 6 but were never carried into this schema. Added so a
    future band-occupancy or profile-volume question is answerable
    directly from the committed corpus, the same reason `capped_out` was
    added: without them, answering "how many of this cycle's published
    stories came from the hot-take band vs. the profile quota vs. the main
    pool" requires re-deriving it from `candidates_submitted`/other fields
    (error-prone, and not always possible) rather than reading a field.
    Both default to `0` for the identical pre-existing-corpus round-trip
    reason `capped_out` defaults to `0`."""

    league_id: int
    season: int
    phase: str
    cycle_index: int
    candidates_scored: int
    candidates_above_floor: int
    candidates_submitted: int
    stories_persisted: int
    capped_out: int = 0
    band_published: int = 0
    profiles_published: int = 0

    def to_json(self) -> dict:
        return {
            "league_id": self.league_id,
            "season": self.season,
            "phase": self.phase,
            "cycle_index": self.cycle_index,
            "candidates_scored": self.candidates_scored,
            "candidates_above_floor": self.candidates_above_floor,
            "candidates_submitted": self.candidates_submitted,
            "stories_persisted": self.stories_persisted,
            "capped_out": self.capped_out,
            "band_published": self.band_published,
            "profiles_published": self.profiles_published,
        }

    @staticmethod
    def from_json(payload: Mapping[str, object]) -> MediaCycleRecord:
        return MediaCycleRecord(
            league_id=int(payload["league_id"]),
            season=int(payload["season"]),
            phase=str(payload["phase"]),
            cycle_index=int(payload["cycle_index"]),
            candidates_scored=int(payload["candidates_scored"]),
            candidates_above_floor=int(payload["candidates_above_floor"]),
            candidates_submitted=int(payload["candidates_submitted"]),
            stories_persisted=int(payload["stories_persisted"]),
            capped_out=int(payload.get("capped_out", 0)),
            band_published=int(payload.get("band_published", 0)),
            profiles_published=int(payload.get("profiles_published", 0)),
        )


@dataclass(frozen=True)
class MediaCorpusArm:
    """One of the two identically-shaped arms (`live`/`control`, Decision
    P4-95/P4-102) -- M33 and M35/M36 read `live` only; M34 reads both and
    hard-`SKIP`s if `control` is `None` at the `MediaCorpusTotals` level.

    `by_voice_kind_template` is `voice -> story_kind -> template_id ->
    count` (Decision P4-97's three-level shape, one level deeper than
    `tools/gm_harness/metrics.py::_pool_phrase_totals`'s own `by_
    philosophy_template`) -- keeping `story_kind` as its own level is what
    lets M34's `restrict_to_kinds` be applied at metric time rather than
    baked into the corpus."""

    stories_total: int
    fallback_stories: int
    by_kind: Mapping[str, KindOrVoiceSplit]
    by_voice: Mapping[str, KindOrVoiceSplit]
    by_voice_kind_template: Mapping[str, Mapping[str, Mapping[str, int]]]
    by_pair_depth: Mapping[str, PairDepthBin]
    cycles: tuple[MediaCycleRecord, ...] = field(default_factory=tuple)
    by_kind_tier: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    """MV-14.4 item 0 (`docs/design/media-v2-newsroom.md`) -- `story_kind -> format_
    tier -> count`, additive and defaulted to `{}` for the identical backward-
    compatibility reason `persona_metadata`/`phase_cadence` default to `{}` elsewhere
    in this design: the committed `artifacts/media_corpus.json` predates this field."""

    def to_json(self) -> dict:
        return {
            "stories_total": self.stories_total,
            "fallback_stories": self.fallback_stories,
            "by_kind": {k: v.to_json() for k, v in sorted(self.by_kind.items())},
            "by_kind_tier": {
                kind: dict(sorted(tier_counts.items()))
                for kind, tier_counts in sorted(self.by_kind_tier.items())
            },
            "by_voice": {k: v.to_json() for k, v in sorted(self.by_voice.items())},
            "by_voice_kind_template": {
                voice: {
                    kind: dict(sorted(by_template.items()))
                    for kind, by_template in sorted(by_kind.items())
                }
                for voice, by_kind in sorted(self.by_voice_kind_template.items())
            },
            "by_pair_depth": {
                d: bin_.to_json()
                for d, bin_ in sorted(self.by_pair_depth.items(), key=lambda kv: int(kv[0]))
            },
            "cycles": [c.to_json() for c in self.cycles],
        }

    @staticmethod
    def from_json(payload: Mapping[str, object]) -> MediaCorpusArm:
        return MediaCorpusArm(
            stories_total=int(payload["stories_total"]),
            fallback_stories=int(payload["fallback_stories"]),
            by_kind={
                k: KindOrVoiceSplit.from_json(v) for k, v in payload["by_kind"].items()
            },
            # MV-14.4 item 0: `.get(..., {})`, never `payload["by_kind_tier"]` -- the
            # committed corpus predates this key entirely, and a strict read would
            # break `from_json` on every hard-pinned M33-M36 corpus test the moment
            # this field shipped, before any regeneration ever ran.
            by_kind_tier={
                kind: dict(tier_counts)
                for kind, tier_counts in payload.get("by_kind_tier", {}).items()
            },
            by_voice={
                k: KindOrVoiceSplit.from_json(v) for k, v in payload["by_voice"].items()
            },
            by_voice_kind_template={
                voice: {
                    kind: dict(by_template)
                    for kind, by_template in by_kind.items()
                }
                for voice, by_kind in payload["by_voice_kind_template"].items()
            },
            by_pair_depth={
                d: PairDepthBin.from_json(v) for d, v in payload["by_pair_depth"].items()
            },
            cycles=tuple(
                MediaCycleRecord.from_json(c) for c in payload.get("cycles", ())
            ),
        )


@dataclass(frozen=True)
class MediaCorpusTotals:
    """The one authoritative schema (Decision P4-102, amended by P4-103).
    `to_json`/`from_json` are this module's own committed-artifact
    round-trip -- `tools/generate_media_corpus.py` writes via `to_json`,
    and 20b's loader (a later dispatch) reads via `from_json` and fails
    closed on a `library_sha` mismatch (Decision P4-101's ruled staleness
    check) before handing the result to `compute_all`."""

    schema_version: int
    library_sha: str
    library_path: str
    league_rng_seed: int
    seed_root: str
    leagues: int
    seasons: int
    generated_at: str
    top_k_per_cycle: int
    salience_floor: float
    repeat_penalty: float
    declared_fallback_weight_share: float
    persona_exclusivity_floor: float
    ungated_kinds: tuple[str, ...]
    live: MediaCorpusArm
    control: MediaCorpusArm | None

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "library_sha": self.library_sha,
            "library_path": self.library_path,
            "league_rng_seed": self.league_rng_seed,
            "seed_root": self.seed_root,
            "leagues": self.leagues,
            "seasons": self.seasons,
            "generated_at": self.generated_at,
            "top_k_per_cycle": self.top_k_per_cycle,
            "salience_floor": self.salience_floor,
            "repeat_penalty": self.repeat_penalty,
            "declared_fallback_weight_share": self.declared_fallback_weight_share,
            "persona_exclusivity_floor": self.persona_exclusivity_floor,
            "ungated_kinds": sorted(self.ungated_kinds),
            "live": self.live.to_json(),
            "control": self.control.to_json() if self.control is not None else None,
        }

    @staticmethod
    def from_json(payload: Mapping[str, object]) -> MediaCorpusTotals:
        control_payload = payload.get("control")
        return MediaCorpusTotals(
            schema_version=int(payload["schema_version"]),
            library_sha=str(payload["library_sha"]),
            library_path=str(payload["library_path"]),
            league_rng_seed=int(payload["league_rng_seed"]),
            seed_root=str(payload["seed_root"]),
            leagues=int(payload["leagues"]),
            seasons=int(payload["seasons"]),
            generated_at=str(payload["generated_at"]),
            top_k_per_cycle=int(payload["top_k_per_cycle"]),
            salience_floor=float(payload["salience_floor"]),
            repeat_penalty=float(payload["repeat_penalty"]),
            declared_fallback_weight_share=float(payload["declared_fallback_weight_share"]),
            persona_exclusivity_floor=float(payload["persona_exclusivity_floor"]),
            ungated_kinds=tuple(payload["ungated_kinds"]),
            live=MediaCorpusArm.from_json(payload["live"]),
            control=MediaCorpusArm.from_json(control_payload) if control_payload else None,
        )
