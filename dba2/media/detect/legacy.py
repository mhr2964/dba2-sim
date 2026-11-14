"""`dba2/media/detect/legacy.py` -- the thin adapter turning each of the
CURRENT eleven `StoryKind` members' already-tuned `StoryFrame` (`dba2/media/
frame.py::derive_frame`) into a `StoryPackage` (docs/design/media-v2-
newsroom.md MV-1b(b), MV-8, "Build order" step 3a).

**Nothing closed and tuned gets discarded (MV-8's own framing).** All five
existing producers (`pulse.diff_pulse_snapshots`, `events.
career_scoring_milestone_candidates`, `events.draft_selection_candidate`,
`events.ladder_transition_candidate`) keep their producers, thresholds, and
tests, completely unchanged. Only the PACKAGING is new: this module reads
an already-derived `StoryFrame`'s fields and re-emits them as `Fact`s
against the exact registry entries `dba2.domain.media_facts.
FACT_KEYS_BY_KIND` declares for that kind (MV-8's own "full shipped
vocabulary" table, swept from `seeds/media_phrases.json` directly).

**The `insufficient_data` short-circuit moves here, one step upstream of
where `salience.score_candidates` used to own it** (MV-1b(b)): `adapt_frame`
returns `None` for a `StoryFrame` whose `insufficient_data` is set, before
computing anything else -- the same "never scored, never persisted" path,
now diagnosed at the earliest point a package could exist at all rather
than the scoring step that used to filter it out afterward.

**This module returns an UNSEALED package.** `Subject.player_name`/
`team_name` are always `None` here -- names are resolved exactly once, by
`dba2.services.media_service.seal_package`, from row maps that cycle's own
loaders already hold (MV-1c.1: "a detector never emits an identity fact").
The `season`/`phase`/`cycle_index` universal facts and the two identity
facts (`team_name`/`player_name`) are `seal_package`'s job to prepend, not
this module's -- an unsealed `StoryPackage` from this module is therefore
never scored or published directly; every real caller passes it through
`seal_package` first.

Zero I/O, the same purity rules `dba2/media/frame.py`/`dba2/media/pulse.py`
already carry (`tests/unit/test_layering.py`'s automatic `dba2/media/`
sweep): no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media_facts`, `dba2.
media.frame` and stdlib.
"""
from __future__ import annotations

from dba2.domain.media_facts import (
    Angle,
    Fact,
    FactSource,
    StoryPackage,
    Subject,
    SubjectKind,
)
from dba2.domain.phase import Phase
from dba2.media.frame import StoryFrame, StoryKind

# ---------------------------------------------------------------------------
# Per-kind classification -- mirrors `dba2/media/frame.py`'s own kind-group
# shape (`_TEAM_SAMPLE_KINDS`/`_PLAYER_SAMPLE_KINDS`/etc.), one dict/set per
# axis this adapter needs to decide, each swept against MV-8's own tables
# rather than assumed.
# ---------------------------------------------------------------------------

_TEAM_SUBJECT_KINDS = frozenset(
    {
        StoryKind.STANDINGS_SURGE,
        StoryKind.STANDINGS_COLLAPSE,
        StoryKind.STREAK,
        StoryKind.TRADE_REACTION,
        StoryKind.TRADE_FALLOUT,
    }
)
_PLAYER_SUBJECT_KINDS = frozenset(
    {
        StoryKind.MILESTONE,
        StoryKind.AWARD_RACE,
        StoryKind.ROOKIE_WATCH,
        StoryKind.PLAYER_UNREST,
        StoryKind.HOLDOUT_WATCH,
    }
)
# `SIGNING_REACTION` is the one kind carrying both ids (MV-1c.1's own
# verified example, `_SIGNING_KINDS` in `frame.py`) -- handled as its own
# case below, subject-kind `PLAYER` by the same player-preferred convention
# `media_service._subject_identity` already uses for the RNG salt.

_ANGLE_BY_KIND: dict[StoryKind, Angle] = {
    StoryKind.STANDINGS_SURGE: Angle.SURGE,
    StoryKind.STANDINGS_COLLAPSE: Angle.COLLAPSE,
    StoryKind.STREAK: Angle.STREAK,
    StoryKind.MILESTONE: Angle.CAREER_CLIMB,
    StoryKind.ROOKIE_WATCH: Angle.ROOKIE_ARRIVAL,
    StoryKind.PLAYER_UNREST: Angle.UNREST,
    StoryKind.HOLDOUT_WATCH: Angle.HOLDOUT,
    # The remaining three old kinds have no real producer today (the
    # Problem section's own verified count: `TRADE_REACTION`/`TRADE_
    # FALLOUT`/`AWARD_RACE` have no production caller) and no shipped
    # template declares an `angles` dimension yet (every current template
    # is wildcard on it) -- so which angle these three carry has zero
    # effect on selection in this build step. Assigned the closest-fitting
    # member of the closed list anyway, so `StoryPackage.angle` is never
    # left unpopulated, and disclosed here rather than left for a reader
    # to wonder whether it was an oversight.
    StoryKind.TRADE_REACTION: Angle.TRADE_MADE,
    StoryKind.TRADE_FALLOUT: Angle.TRADE_MADE,
    StoryKind.AWARD_RACE: Angle.CAREER_CLIMB,
    StoryKind.SIGNING_REACTION: Angle.MARKET_WATCH,
}


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _signed_int_fact_rendered_absolute(
    key: str, label: str, value: int, source: FactSource
) -> Fact:
    """Two of the shipped library's slots render a SIGNED count as its
    absolute value -- `select.py`'s old `_slot_streak_length`/`_slot_
    motivation_magnitude` resolvers ("{team_name} have LOST {streak_length}
    in a row", never "lost -5"). `value_number` stays signed (a trigger
    comparing `streak_length lte -5` needs the real sign); `value_text` is
    pre-rendered to the same string those two resolvers used to produce --
    `Fact.value_text`'s own contract ("render-ready") is exactly what makes
    this the fact-construction step's job rather than a slot-binding-time
    special case (MV-1b(c): "Slot values come from `Fact.value_text`,
    which is render-ready by construction")."""
    return Fact(
        key=key, label=label, value_text=str(abs(value)), value_number=float(value), source=source
    )


def _text_fact(key: str, label: str, value_text: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value_text, value_number=None, source=source)


def _facts_for_frame(frame: StoryFrame) -> tuple[Fact, ...]:
    """The detector's OWN facts -- every key in `FACT_KEYS_BY_KIND[frame.
    kind]` except `team_name`/`player_name` (seal-time identity facts,
    MV-1c.1). `frame.insufficient_data` is already known `None` by the time
    this runs (`adapt_frame`'s own short-circuit); every field read here is
    therefore either a real value or a genuine "not wired yet" `None`
    (`dba2/media/frame.py`'s own module docstring), and a `None` reading
    simply emits no fact for that key -- "absent is neutral," never a
    fabricated zero."""
    kind = frame.kind
    facts: list[Fact] = []

    if kind in (StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE):
        if frame.standings_position_delta is not None:
            facts.append(
                _int_fact(
                    "standings_position_delta", "Standings position change",
                    frame.standings_position_delta, FactSource.PULSE_SNAPSHOT,
                )
            )
        if frame.standings_position is not None:
            facts.append(
                _int_fact(
                    "standings_position", "Standings position",
                    frame.standings_position, FactSource.PULSE_SNAPSHOT,
                )
            )
    elif kind is StoryKind.STREAK:
        if frame.streak_length is not None:
            facts.append(
                _signed_int_fact_rendered_absolute(
                    "streak_length", "Streak length",
                    frame.streak_length, FactSource.PULSE_SNAPSHOT,
                )
            )
    elif kind is StoryKind.TRADE_REACTION:
        # `trade_in_players`/`trade_out_players`/`trade_in_picks` are
        # `int`, never `None`, on a SUFFICIENT frame (`derive_frame`'s own
        # `len(...)` computation) -- always emitted, a real zero being a
        # real "no assets of this kind" reading (`StoryFrame`'s own
        # docstring), not an absence.
        facts.append(
            _int_fact(
                "trade_in_players", "Players acquired",
                frame.trade_in_players, FactSource.LEAGUE_TRANSACTIONS,
            )
        )
        facts.append(
            _int_fact(
                "trade_out_players", "Players moved",
                frame.trade_out_players, FactSource.LEAGUE_TRANSACTIONS,
            )
        )
        facts.append(
            _int_fact(
                "trade_in_picks", "Picks acquired",
                frame.trade_in_picks, FactSource.LEAGUE_TRANSACTIONS,
            )
        )
    elif kind in (StoryKind.PLAYER_UNREST, StoryKind.HOLDOUT_WATCH):
        if frame.motivation_magnitude is not None:
            facts.append(
                _signed_int_fact_rendered_absolute(
                    "motivation_magnitude", "Motivation swing magnitude",
                    frame.motivation_magnitude, FactSource.MOTIVATION_EVENTS,
                )
            )
        if frame.request_state is not None:
            facts.append(
                _text_fact(
                    "request_state", "Current request state",
                    frame.request_state.value, FactSource.MOTIVATION_EVENTS,
                )
            )
    # TRADE_FALLOUT/SIGNING_REACTION/MILESTONE/AWARD_RACE/ROOKIE_WATCH
    # declare no non-identity key in FACT_KEYS_BY_KIND (MV-8's own table) --
    # every one of their shipped templates triggers on nothing and slots
    # only the subject's name(s), so no branch is needed here for them.

    return tuple(facts)


def _draft_subject(frame: StoryFrame) -> Subject:
    """An UNSEALED `Subject` -- ids only, names always `None` (this module
    never resolves a name, MV-1's own "ids only, never names" rule for the
    pure layer)."""
    kind = frame.kind
    if kind is StoryKind.SIGNING_REACTION:
        # The one kind carrying both ids (MV-1c.1) -- player-preferred,
        # mirroring `media_service._subject_identity`'s own rule.
        return Subject(kind=SubjectKind.PLAYER, player_id=frame.player_id, team_id=frame.team_id)
    if kind in _PLAYER_SUBJECT_KINDS:
        return Subject(kind=SubjectKind.PLAYER, player_id=frame.player_id)
    if kind in _TEAM_SUBJECT_KINDS:
        return Subject(kind=SubjectKind.TEAM, team_id=frame.team_id)
    # Unreachable for any real `StoryKind` member (every one of the current
    # eleven is classified above) -- the same loud, explanatory
    # `AssertionError` shape `dba2/media/frame.py::_insufficiency_reason`'s
    # own final branch already uses for the identical failure class.
    raise AssertionError(
        f"StoryKind.{kind.name} is not classified into a subject-kind group in "
        "dba2/media/detect/legacy.py -- every current StoryKind member must be, or "
        "this adapter silently mis-subjects a future kind"
    )


def adapt_frame(
    frame: StoryFrame, *, season: int, phase: Phase, cycle_index: int
) -> StoryPackage | None:
    """`StoryFrame -> StoryPackage`, UNSEALED -- `None` for an insufficient
    frame (the relocated short-circuit, MV-1b(b)). The returned package's
    `Subject` carries no names and its `facts` carry no identity/universal
    keys; `dba2.services.media_service.seal_package` is the only function
    that may complete it. Total over every current `StoryKind` member (each
    is classified in exactly one subject-kind group and has an `Angle`
    entry above); a `StoryFrame` for a kind this module does not recognize
    raises loudly rather than silently mis-subjecting it (`_draft_subject`'s
    own guard)."""
    if frame.insufficient_data is not None:
        return None
    kind = frame.kind
    return StoryPackage(
        story_kind=kind,
        angle=_ANGLE_BY_KIND[kind],
        subject=_draft_subject(frame),
        facts=_facts_for_frame(frame),
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
