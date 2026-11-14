"""`StoryCandidate -> StoryFrame`, the media analogue of `managers/voice/
frame.py::NegotiationFrame` -- Decisions P4-24 (totality + the
`insufficient_data` short-circuit) and P4-25 (`StoryKind`, a listed closed
enum), docs/design/phase-4-media-and-player-agency.md §6.2, §11 Stage D
step 16.

**This step builds vocabulary and derivation logic only -- it wires no
real data source.** `StoryCandidate` below is the raw, caller-supplied
input `derive_frame` turns into a `StoryFrame`; the real pulse snapshot,
event intake and salience layer that will eventually CONSTRUCT a
`StoryCandidate` from `games`/`player_season_stats`/`motivation_repo`/the
decision-log JSONL are step 17's job, not this one (§11 Stage D). Every
`StoryCandidate` in this module's own tests is therefore hand-built, the
same way `tests/unit/managers/voice/test_frame.py` hand-builds
`DecisionRecord`s to drive `NegotiationFrame`'s illegality short-circuit
without a real trade engine in the loop.

**Field list, authored against the real census
(`docs/design/phase-4-stage-a-census.md` step 2), not against the design
draft's own prose list.** The census measured, for each of §6.2's seven
candidate field categories, whether a real value is derivable from
`artifacts/gm/*.jsonl` alone, needs a `games`/`player_season_stats` DB
read no repository function yet exists for, or needs a write path
(`player_motivation`/`player_motivation_events`) that did not exist until
Stage B. Each `StoryFrame` field below is classified in its own docstring
against that census, one of three states:

- **JSONL-derivable today** -- the trade-asset-shape counts and the
  identity tokens. A real value could be computed from
  `artifacts/gm/*.jsonl` right now, with no DB read at all (census step 2,
  "of the seven candidate fields, exactly one ... and one identity-token
  field are meaningfully JSONL-derivable today").
- **Needs a DB read step 17 hasn't built yet** -- standings, streak,
  per-player season aggregates (no bulk `player_season_stats`-for-a-league
  loader exists, census step 3's own finding), and the motivation/request-
  state fields (no event-intake wiring exists yet, even though `dba2/
  agency/`'s own write path is real as of Stage B/C).
- **Neutral-default-for-now** -- `award_share_estimate`. Decision P4-40
  scopes this as a Phase-4-*computed* estimate, not a `player_awards`
  read (`player_awards` itself has zero rows in the real database,
  Phase 7's own single-writer table) -- there is no unbuilt DB read to
  point at here, only an unbuilt computation, one step further away than
  the "needs a DB read" fields above.

**Every field in the last two states is typed `X | None` and defaults to
`None` in this step, unconditionally** -- not because `derive_frame`
short-circuited, but because nothing wires a real value into them yet.
This is `dba2/agency/situation.py::PlayerSituation`'s own "`None` is
neutral, never zero" convention (Decision P4-9), reused here for the
identical reason: a field with no data source behind it yet must not
silently read as a real, computed zero.

**The `insufficient_data` short-circuit (Decision P4-24) is a SEPARATE
mechanism from the above**, exactly as `NegotiationFrame`'s illegality
short-circuit is separate from (and does not excuse) every other field's
own derivation. It fires when the SUBJECT itself -- independent of which
fields happen to be wired yet -- has too little history for this story
kind to mean anything: a rookie with no prior season, a team three games
into a season, a player with no minutes, a story with no snapshot to diff
against, or a subject this module cannot even identify. **A deliberate,
disclosed deviation from a literal reading of P4-24's own template text**:
that text says a short-circuited frame's "numeric fields are all `0.0`",
copying `NegotiationFrame`'s template verbatim -- but `NegotiationFrame`'s
numeric fields (gaps, shares, counts) are all naturally zero-based
arithmetic where `0.0` genuinely means "neutral". `StoryFrame`'s rate/
position/delta fields are not: a `season_points_per_game_delta` of `0.0`
reads as the real, false claim "no change", not "unknown" -- the exact
confusion P4-24's own "not per-field guards" clause exists to prevent one
layer up. This module therefore zeroes only the fields with a genuine
zero (the four trade-asset counts, mirroring `NegotiationFrame.in_players`
etc. exactly), and leaves every rate/position/delta field `None` on the
short-circuit branch, matching what it already reads as `None` whenever
its own data source is unwired -- one "no reading" value throughout,
rather than two different empty values for what is often the same
underlying situation. `kind`/`team_id`/`player_id` are passed through
unchanged on the short-circuit branch, exactly as `philosophy`/`window`
are "as given" on `NegotiationFrame`'s illegal path -- nothing here plays
the role `stance`/`situation`'s `REJECT_ILLEGAL` override plays, because
`insufficient_data` is itself the token a template would trigger on
(directly comparable to `NegotiationFrame.illegal_token`, not to
`stance`/`situation`).

**`StoryFrame`'s fields are the kind of flat, `getattr`-able attributes
`engine.py::_trigger_holds` can already read** (`managers/voice/
engine.py`, Decision P4-20/step 15) -- every field is a plain scalar,
`Enum` member, or `int | None`/`float | None`, never a nested structure a
`Trigger.feature`/`getattr` pair could not address directly. Wiring a real
media selector against this frame (`select_template`'s `dimension_matches`
parameter, a `StoryKind`/persona predicate) is step 18's job, not this
one -- this module never imports `engine.py`.

Zero I/O, the same purity rules `dba2/agency/` already carries (Decision
P4-8, extended to media by Decision P4-31; `tests/unit/test_layering.py`):
no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.motivation`
(`MotivationAxis`, `RequestState`), `dba2.domain.media` (`StoryKind` --
Decision P4-49, see below) and stdlib.

**`StoryKind` moved to `dba2/domain/media.py` (Decision P4-49, Stage D step
19).** This module used to declare `StoryKind` itself; it now imports it
from its canonical home and re-exports it (the `from dba2.domain.media
import StoryKind` line below makes `StoryKind` a real attribute of this
module's own namespace again, so every existing `from dba2.media.frame
import StoryKind` elsewhere under `dba2/media/` keeps working unchanged).
See `dba2/domain/media.py`'s own module docstring for why the move was
forced (`dba2/data/repositories/media_repo.py` needs `StoryKind` to map a
`media_stories.story_kind` column, and neither `dba2.data` nor anything
outside `media_service.py` may import `dba2.media` at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dba2.domain.media import StoryKind
from dba2.domain.motivation import MotivationAxis, RequestState

# ---------------------------------------------------------------------------
# InsufficientDataReason -- Decision P4-24's short-circuit token, closed
# and listed for the identical reason StoryKind is: a reader must be able
# to enumerate every way a frame can be "insufficient" by eye, the same
# property NegotiationFrame's five `illegal:*` tokens already have.
# ---------------------------------------------------------------------------


class InsufficientDataReason(StrEnum):
    """Six reasons, each independently constructible and independently
    tested (§9.5's "five constructed cases minimum, one per
    `insufficient_data` cause" -- Phase 3's equivalent totality claim was
    asserted by a vacuous test for a full review round before a critic
    proved it false by hand; this module's own tests construct each of
    these by name rather than asserting totality generically).

    **round 9 critic fix (blocking finding 1): `NO_MOTIVATION_DATA` was
    added** because `player_unrest`/`holdout_watch` were originally gated
    into `_PLAYER_SAMPLE_KINDS` (a games-played check), which made them
    structurally unreachable for their own canonical subject -- a real
    holdout has played zero games this season, by definition, so the
    games-played short-circuit fired on every real holdout and deleted
    the very `request_state`/`motivation_axis` fields these two kinds
    exist to read. They are now their own group (`_MOTIVATION_KINDS`),
    gated on whether motivation data is actually present, not on games
    played.

    `NO_SUBJECT_IDENTITY` and `NO_TRADE_ASSETS` are two faces of the same
    underlying problem (nothing to name the story about) split into two
    tokens because they are diagnosed from different fields (`team_id`/
    `player_id` vs. all four trade-asset id tuples being empty) and a
    template author may want to react to them differently (the first
    should never fire on a well-formed candidate; the second is a subtly
    different bug -- a trade record with a subject but no legs).

    `EARLY_SEASON_SAMPLE` and `NO_GAMES_PLAYED` both cover the design
    doc's own "a rookie with no prior season" example (§6.2/Decision
    P4-24) -- a rookie's first few games (team OR player, whichever this
    kind is scoped to) trip the former, a subject with literally zero
    games played this season (or a negative/corrupt read) trips the
    latter; there is no separate `NO_PRIOR_SEASON` token because every
    one of this phase's eleven kinds keys off the CURRENT season's own
    sample size, never a season-over-season comparison, so a rookie is
    fully covered by these two without inventing a third, currently-
    untriggerable reason.
    """

    NO_SUBJECT_IDENTITY = "no_subject_identity"
    """Neither `team_id` nor `player_id` (whichever this kind requires) is
    known -- this module cannot even name who or what the story is about.
    Should never fire on a well-formed candidate; exists as the same kind
    of defensive floor `NegotiationFrame`'s illegality check is (a real
    condition, not a hypothetical one, per that class's own five real
    tokens)."""

    EARLY_SEASON_SAMPLE = "early_season_sample"
    """A team-scoped kind (`standings_surge`, `standings_collapse`,
    `streak`) whose subject has played fewer than `_MIN_TEAM_GAMES_FOR_
    SAMPLE` games this season, OR a player-scoped kind (`milestone`,
    `award_race`, `rookie_watch`) whose subject has played at least one
    but fewer than `_MIN_PLAYER_GAMES_FOR_SAMPLE` games this season --
    "a team in game 3" (Decision P4-24's own example), where a standings
    position, a streak, or a rookie's per-game rate reads as noise, not
    signal. Distinct from `NO_GAMES_PLAYED`, which covers zero (or
    corrupt/negative) games, a stronger and separately diagnosed case."""

    NO_GAMES_PLAYED = "no_games_played"
    """A player-scoped kind (`milestone`, `award_race`, `rookie_watch`)
    whose subject has played zero games this season, or the read is
    entirely absent, or the read is negative (corrupt input, treated the
    same as zero rather than as a real, small, sufficient sample) --
    Decision P4-24's own "a player with no minutes" example. Every
    per-game rate is undefined there, division-by-zero included, exactly
    the class of bug §6.2 cites Phase 3 as having shipped once already (a
    `ZeroDivisionError` on 10-21% of all packages, inside a table that
    asserted the field was total on that exact input). **Named for what
    this module actually checks (games played), not renamed `ZERO_
    MINUTES` (round 9 critic nit 4) -- this module never reads a minutes
    field; a player with a genuine minutes-per-game of `0.0` but a real
    games-played count is not this case, it is a real, if unusual,
    `SUFFICIENT` reading.**"""

    NO_MOTIVATION_DATA = "no_motivation_data"
    """A motivation-scoped kind (`player_unrest`, `holdout_watch`) whose
    subject has neither a `request_state` nor a `motivation_axis` reading
    at all -- diagnosed on the DATA these two kinds actually read, not on
    games played (round 9 critic blocking finding 1): a holdout or a
    trade-request situation is defined by the player's ladder/axis state,
    not by how many games he has suited up for, and a real holdout has
    played zero games by definition, which must not itself disqualify the
    subject."""

    NO_PULSE_SNAPSHOT = "no_pulse_snapshot"
    """A kind whose defining feature IS a delta (`standings_surge`,
    `standings_collapse`) has no prior-cycle snapshot to diff against at
    all -- Decision P4-28's own named failure mode ("a diff against a
    snapshot that does not exist yields no candidates at all, never a
    candidate computed against zero"), distinct from `EARLY_SEASON_
    SAMPLE`: this can fire for a ten-year veteran's team on the very first
    cycle a league ever runs, when the subject's OWN sample size is fine
    but the diff MECHANISM has no baseline yet."""

    NO_TRADE_ASSETS = "no_trade_assets"
    """A trade-scoped kind (`trade_reaction`, `trade_fallout`) whose
    candidate names a team but carries zero player/pick ids in every one
    of its four asset-shape tuples -- a trade "story" about a trade that,
    as far as this frame can tell, moved nothing."""


# ---------------------------------------------------------------------------
# Kind groupings -- used only by derive_frame's own generic insufficiency
# check below, not a claim about step 18's eventual real trigger
# conditions (which fields a template actually keys off is that step's
# job). Every StoryKind member appears in exactly one group; _insufficiency_
# reason's final branch RAISES (lazily, per call, not at import/definition
# time -- round 9 critic nit 6 corrects this comment's prior claim that it
# "asserts that exhaustively": there is no import-time exhaustiveness check
# here, only a per-call one) if a future kind is added to the enum without
# a matching group entry, rather than silently reading as "always
# sufficient". The real exhaustiveness GUARANTEE is
# `test_every_story_kind_is_classified_into_exactly_one_group`
# (tests/unit/test_media_frame.py), which iterates every real StoryKind
# member against the groups directly.
# ---------------------------------------------------------------------------

_TEAM_SAMPLE_KINDS = frozenset(
    {StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE, StoryKind.STREAK}
)
_DELTA_KINDS = frozenset({StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE})
"""The subset of `_TEAM_SAMPLE_KINDS` whose defining feature is itself a
delta -- `streak` is team-scoped but is a running counter, not a diff
against a pulse snapshot, so it is deliberately excluded here."""

_PLAYER_SAMPLE_KINDS = frozenset(
    {
        StoryKind.MILESTONE,
        StoryKind.AWARD_RACE,
        StoryKind.ROOKIE_WATCH,
    }
)
_MOTIVATION_KINDS = frozenset({StoryKind.PLAYER_UNREST, StoryKind.HOLDOUT_WATCH})
"""round 9 critic fix (blocking finding 1): `PLAYER_UNREST`/`HOLDOUT_WATCH`
used to live in `_PLAYER_SAMPLE_KINDS`, which gated them on
`player_games_played_this_season` -- structurally unreachable for their
own canonical subject, since a real holdout has played zero games by
definition. Split into their own group, gated on motivation-data
presence (`request_state`/`motivation_axis`) instead of games played --
see `NO_MOTIVATION_DATA`'s own docstring."""
_TRADE_KINDS = frozenset({StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT})
_SIGNING_KINDS = frozenset({StoryKind.SIGNING_REACTION})
"""**Deliberately ungated** (round 9 critic nit 7) -- unlike every other
group above, a signing reaction plausibly needs no history at all: the
story is about the signing itself, not about the player's or team's prior
season. `signing_reaction` accepts a candidate with `0` (or negative)
games played and no pulse snapshot as sufficient, as long as both
identity tokens are present. This is a disclosed asymmetry with the other
three kind-groups, not an oversight."""

_MIN_TEAM_GAMES_FOR_SAMPLE = 5
"""Disclosed, not derived -- same status as `dba2/agency/situation.py::_
ROLE_FIT_SCALE`/`_TENURE_SEASONS_ANCHOR`: a reasonable-looking cutoff with
no real minimum-sample-size study behind it, named here rather than left
implicit so a future tuning pass finds one place to look. "A team in game
3" (Decision P4-24's own example) is below this threshold; a team 5+
games in is treated as having a real-enough sample. Does not bump
`GM_TUNING_VERSION` -- lives under `dba2/media/`, not `managers/`
(Decision P4-3's corollary, same as `_ROLE_FIT_SCALE`'s own note)."""

_MIN_PLAYER_GAMES_FOR_SAMPLE = 5
"""Disclosed, not derived -- the player-side analogue of `_MIN_TEAM_
GAMES_FOR_SAMPLE`, added by the round 9 critic fix round (blocking
finding 3): before this constant existed, a player-scoped kind's own
sample check was `player_games_played_this_season == 0`, which (a) made
`EARLY_SEASON_SAMPLE` structurally unreachable for every player-scoped
kind (a rookie's first game read as SUFFICIENT, a per-game rate computed
from an n=1 sample) and (b) read a corrupt negative games-played value as
SUFFICIENT, unlike the team side's own threshold check, which correctly
treats a negative value as below-threshold. Same disclosed-cutoff status
as the team-side constant; not independently derived. Does not bump
`GM_TUNING_VERSION`, same reasoning as `_MIN_TEAM_GAMES_FOR_SAMPLE`."""


def _delta[Number: (int, float)](
    current: Number | None, prior: Number | None
) -> Number | None:
    """`current - prior`, or `None` if either side is unknown -- never a
    silent `0.0` for "we don't have a baseline" (see the module docstring's
    deviation note: a `0.0` delta is a real, false claim of "no change").
    Generic over `int`/`float` (round 9 critic nit 2, PEP 695 syntax --
    `requires-python = ">=3.12"`, and `ruff`'s own `UP047` rule prefers it
    over a module-level `TypeVar`) rather than fixed at `float` --
    `StoryFrame.standings_position_delta` is `int | None` (a standings
    position is an integer rank) while `StoryFrame.season_points_per_
    game_delta` is `float | None`; a single `float`-only signature made
    the former assignment a real, newly-introduced mypy
    error rather than a pre-existing one."""
    if current is None or prior is None:
        return None
    return current - prior


# ---------------------------------------------------------------------------
# StoryCandidate -- the raw, caller-supplied input derive_frame turns into
# a StoryFrame. Provisional, per this step's own scope: step 17's real
# pulse-snapshot/event-intake/salience layer is what will actually
# construct one of these from `games`/`player_season_stats`/
# `motivation_repo`/the decision-log JSONL; this module only fixes the
# SHAPE that construction has to land in, the same way `DecisionRecord`
# already existed before `NegotiationFrame`'s own derive_frame was written
# against it. Expected to move if step 17 finds a real shape mismatch --
# the design doc's own "deliberately not fixed in this draft" caveat,
# carried forward one step.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryCandidate:
    """One candidate story's raw facts, all optional except `kind` --
    `derive_frame` decides whether they add up to a sufficient subject
    for `kind`, generically, per `InsufficientDataReason`'s own rules
    (not per-kind trigger logic, which is step 18's job)."""

    kind: StoryKind

    # Subject identity -- ids only, never names (Decision P4-22: resolving
    # an id to a displayable name is a `domain` read, `media_service.py`'s
    # job at render time, not this pure module's).
    team_id: int | None = None
    player_id: int | None = None

    # Sample-size facts, feeding EARLY_SEASON_SAMPLE / NO_GAMES_PLAYED.
    team_games_played_this_season: int | None = None
    player_games_played_this_season: int | None = None

    # Whether a prior-cycle pulse snapshot exists to diff against at all
    # (Decision P4-28) -- distinct from the sample-size facts above.
    has_pulse_snapshot: bool = False

    # Standings/streak -- needs a DB read (`games`, Decision P4-30) no
    # bulk loader wires yet; see module docstring.
    standings_position: int | None = None
    standings_position_at_last_pulse: int | None = None
    streak_length: int | None = None

    # Per-player season aggregates -- needs a DB read (`player_season_
    # stats`) census step 3 found no bulk-for-a-league loader exists for
    # yet (`stats_repo.py` has only single-player variants).
    season_points_per_game: float | None = None
    season_points_per_game_at_last_pulse: float | None = None
    season_minutes_per_game: float | None = None

    # Trade asset shape -- JSONL-derivable TODAY (census step 2: the
    # decision-log's `subject`/`terms` give the exact id set moved for
    # every trade-shaped record, with no DB read required).
    trade_in_player_ids: tuple[int, ...] = ()
    trade_out_player_ids: tuple[int, ...] = ()
    trade_in_pick_ids: tuple[int, ...] = ()
    trade_out_pick_ids: tuple[int, ...] = ()

    # Motivation/unrest -- needs a DB read (`motivation_repo`'s event log
    # and `player_motivation.request_state`) the media event-intake layer
    # (step 17) has not wired, even though `dba2/agency/`'s own write path
    # is real as of Stage B/C (Decision P4-14/P4-56's ladder).
    motivation_axis: MotivationAxis | None = None
    motivation_magnitude: int | None = None
    request_state: RequestState | None = None

    # Award estimate -- neutral-default-for-now; Decision P4-40's
    # computation itself is unbuilt (one step further away than an
    # unbuilt DB read -- see module docstring).
    award_share_estimate: float | None = None


def _insufficiency_reason(candidate: StoryCandidate) -> InsufficientDataReason | None:
    """The one function that decides whether `derive_frame` takes the
    short-circuit branch -- a single, by-eye-checkable classification,
    mirroring `derive_frame`'s own `record.verdict.startswith("illegal:")`
    check in `managers/voice/frame.py` for structure, not for content
    (there is no single boolean field to read here; `StoryKind` groups the
    candidate into which check applies)."""
    kind = candidate.kind

    if kind in _TEAM_SAMPLE_KINDS:
        if candidate.team_id is None:
            return InsufficientDataReason.NO_SUBJECT_IDENTITY
        if (
            candidate.team_games_played_this_season is None
            or candidate.team_games_played_this_season < _MIN_TEAM_GAMES_FOR_SAMPLE
        ):
            return InsufficientDataReason.EARLY_SEASON_SAMPLE
        if kind in _DELTA_KINDS and not candidate.has_pulse_snapshot:
            return InsufficientDataReason.NO_PULSE_SNAPSHOT
        return None

    if kind in _PLAYER_SAMPLE_KINDS:
        if candidate.player_id is None:
            return InsufficientDataReason.NO_SUBJECT_IDENTITY
        played = candidate.player_games_played_this_season
        # round 9 critic fix (blocking finding 3): `== 0` used to be the
        # only check here, which (a) made `EARLY_SEASON_SAMPLE` structurally
        # unreachable for every player-scoped kind (a rookie's first game
        # read SUFFICIENT, a per-game rate computed from an n=1 sample) and
        # (b) read a corrupt negative games-played value as SUFFICIENT,
        # unlike the team side's own threshold check. `<= 0` folds "absent"
        # and "corrupt negative" into the same real-zero case as "literally
        # zero," and the real `_MIN_PLAYER_GAMES_FOR_SAMPLE` threshold below
        # makes EARLY_SEASON_SAMPLE reachable here, matching the team side.
        if played is None or played <= 0:
            return InsufficientDataReason.NO_GAMES_PLAYED
        if played < _MIN_PLAYER_GAMES_FOR_SAMPLE:
            return InsufficientDataReason.EARLY_SEASON_SAMPLE
        return None

    if kind in _MOTIVATION_KINDS:
        # round 9 critic fix (blocking finding 1): gated on motivation-data
        # PRESENCE, never on games played -- a real holdout has played zero
        # games by definition, so gating this group into `_PLAYER_SAMPLE_
        # KINDS`'s games-played check made it structurally unreachable for
        # its own canonical subject. `request_state`/`motivation_axis` are
        # passed straight through by `derive_frame`'s normal path (see
        # their own `StoryFrame` docstrings), so a sufficient frame here
        # actually carries the real ladder/axis value, not a short-
        # circuited `None`.
        if candidate.player_id is None:
            return InsufficientDataReason.NO_SUBJECT_IDENTITY
        if candidate.request_state is None and candidate.motivation_axis is None:
            return InsufficientDataReason.NO_MOTIVATION_DATA
        return None

    if kind in _TRADE_KINDS:
        if candidate.team_id is None:
            return InsufficientDataReason.NO_SUBJECT_IDENTITY
        if not (
            candidate.trade_in_player_ids
            or candidate.trade_out_player_ids
            or candidate.trade_in_pick_ids
            or candidate.trade_out_pick_ids
        ):
            return InsufficientDataReason.NO_TRADE_ASSETS
        return None

    if kind in _SIGNING_KINDS:
        if candidate.player_id is None or candidate.team_id is None:
            return InsufficientDataReason.NO_SUBJECT_IDENTITY
        return None

    # round 9 critic fix (nit 3): `kind.name` assumed `kind` is always a
    # real `StoryKind` member. A non-enum `kind` (a plain string, `None`,
    # a stand-in object with no `.name`) used to raise a confusing
    # `AttributeError` here instead of the intended loud, clear
    # `AssertionError` -- `getattr(..., "name", repr(kind))` degrades to
    # `repr` instead of crashing on the crash-reporting path itself.
    kind_label = getattr(kind, "name", repr(kind))
    raise AssertionError(
        f"StoryKind.{kind_label} is not classified into any of derive_frame's "
        "kind groups -- every StoryKind member must appear in exactly one "
        "group above, or this check silently reads a new kind as always "
        "sufficient"
    )


# ---------------------------------------------------------------------------
# StoryFrame -- the FULL frame. Every field always has a defined value
# (Decision P4-24's totality requirement); see the module docstring for
# which fields are `None` unconditionally in this step (not yet wired)
# versus `None` only on the insufficient_data branch (no genuine zero).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryFrame:
    """The media template vocabulary -- `engine.py::_trigger_holds` reads
    every field below via `getattr(frame, trigger.feature)` (a future
    media wrapper's job, step 18). See the module docstring for each
    field's data-source classification and this dataclass's own totality
    argument."""

    kind: StoryKind
    """The story kind this frame was derived for -- never overridden by
    the `insufficient_data` short-circuit (there is no `stance`/`situation`-
    style token swap here; `insufficient_data` itself is the field a
    template triggers on, directly comparable to `NegotiationFrame.
    illegal_token`)."""

    team_id: int | None
    """JSONL/DB-derivable today (census: subject-identity tokens). `None`
    when this kind's subject is not team-scoped, or (on the insufficient-
    data path) whatever was passed in, unchanged."""

    player_id: int | None
    """Same classification and pass-through rule as `team_id`, for
    player-scoped kinds."""

    insufficient_data: InsufficientDataReason | None
    """Decision P4-24's short-circuit token. `None` means every other
    field below was computed normally; any other value means the numeric/
    rate fields below carry no reading and must not be treated as real
    signal by a template."""

    standings_position: int | None
    """Needs a DB read (`games`, Decision P4-30) -- no caller wires a real
    value in here yet (no bulk standings loader exists); `derive_frame`
    passes a caller-supplied value straight through unchanged when one is
    given (round 9 critic nit 1: this is NOT "`None` unconditionally in
    this step," which this module's own tests already disprove -- it is
    "no real production caller supplies one yet"). Separately `None` on
    the `insufficient_data` branch (no genuine zero: position `0` is not a
    real rank)."""

    standings_position_delta: int | None
    """`current - prior`, `None` if either side is unavailable -- see
    `_delta`'s own docstring for why this is `None`, not `0`, when unknown.
    **Sign convention**: standings position `1` is best, so a POSITIVE
    delta means the position NUMBER rose, i.e. the team fell in the
    standings; a NEGATIVE delta means the team rose. Needs a DB read, same
    classification as `standings_position`."""

    streak_length: int | None
    """Signed -- positive is a win streak, negative a losing streak. Needs
    a DB read (`games`, ordered by date, per team) census step 2 confirmed
    is absent from the decision-log JSONL entirely."""

    season_points_per_game: float | None
    """One representative per-player season aggregate (the census's own
    example field, alongside assists/minutes) -- not the full box-score
    line, which is `player_season_stats`' own job to carry once step 17's
    bulk loader exists. Needs a DB read; no bulk `player_season_stats`-
    for-a-league loader exists yet (census step 3's own finding)."""

    season_points_per_game_delta: float | None
    """`current - prior_pulse`, `None` if either side is unavailable. Same
    DB-read classification as `season_points_per_game`."""

    season_minutes_per_game: float | None
    """Needs a DB read, same classification as `season_points_per_game`.
    `None` (never `0.0`) both when unwired and on the `NO_GAMES_PLAYED`
    branch -- a real `0.0` minutes-per-game reading and "we don't have
    this player's minutes at all" must not collapse into the same value."""

    trade_in_players: int
    """`len(candidate.trade_in_player_ids)` -- **JSONL-derivable TODAY**,
    one of the trade-asset count group the census found real and complete
    with no DB read required (the decision-log's `subject`/`terms` give
    the exact player/pick id set moved for every trade-shaped record).
    One of the only fields this dataclass zeroes on the `insufficient_
    data` branch (matching `NegotiationFrame.in_players` exactly) --
    `0` genuinely means "no assets named", not "unknown", the same way
    `NegotiationFrame`'s own zeroed counts do. Each of this field and its
    three siblings below gets its own docstring line (round 9 critic nit
    5), matching `NegotiationFrame`/`negotiation.NegotiationFrame`'s own
    per-field convention rather than one shared trailing docstring."""

    trade_out_players: int
    """`len(candidate.trade_out_player_ids)` -- same classification and
    zeroing rule as `trade_in_players`, matching `NegotiationFrame.
    out_players`."""

    trade_in_picks: int
    """`len(candidate.trade_in_pick_ids)` -- same classification and
    zeroing rule as `trade_in_players`, matching `NegotiationFrame.
    in_picks`."""

    trade_out_picks: int
    """`len(candidate.trade_out_pick_ids)` -- same classification and
    zeroing rule as `trade_in_players`, matching `NegotiationFrame.
    out_picks`."""

    motivation_axis: MotivationAxis | None
    motivation_magnitude: int | None
    """Needs a DB read -- `motivation_repo`'s event log has no media-side
    intake wired yet (step 17), even though `dba2/agency/`'s own write
    path (Stage B/C) is real. No caller wires a real value in here yet in
    production; `derive_frame` passes a caller-supplied value straight
    through unchanged when one is given (round 9 critic nit 1 -- see
    `standings_position`'s own docstring for why "unconditionally `None`"
    was the wrong claim). `_MOTIVATION_KINDS`' own `_insufficiency_reason`
    check reads `motivation_axis` directly, alongside `request_state`."""

    request_state: RequestState | None
    """Needs a DB read (`player_motivation.request_state`, fed by
    Decision P4-14/P4-56's ladder-transition rows) -- drives `player_
    unrest`/`holdout_watch`, the two kinds §6.2's own prose field list
    does not separately name a field for, and (as of the round 9 critic
    fix round) the two fields `_MOTIVATION_KINDS`' own `_insufficiency_
    reason` check reads directly. No caller wires a real value in here
    yet in production; `derive_frame` passes a caller-supplied value
    straight through unchanged when one is given, same correction as
    `standings_position`'s own docstring."""

    award_share_estimate: float | None
    """Neutral-default-for-now -- Decision P4-40 scopes this as a
    Phase-4-*computed* estimate with no real backing table
    (`player_awards` has zero rows anywhere, Phase 7's own single-writer
    loop); there is no DB read to point at yet, only an unbuilt
    computation. No caller wires a real value in here yet; `derive_frame`
    passes a caller-supplied value straight through unchanged when one is
    given, same correction as `standings_position`'s own docstring."""


def derive_frame(candidate: StoryCandidate) -> StoryFrame:
    """`StoryCandidate -> StoryFrame`, Decision P4-24 -- **total over every
    `StoryCandidate` shape, including every degenerate one**: the
    insufficiency check (`_insufficiency_reason`) is this function's
    literal first statement, mirroring `managers/voice/frame.py::
    derive_frame`'s own illegality-first ordering. No division, no
    subtraction, no length computation over an unvalidated collection is
    reached before that check returns, on the branch where it fires.

    See the module docstring for which `StoryFrame` fields are `0` on the
    short-circuit branch (the four trade-asset counts, the only fields
    with a genuine zero) versus `None` (every rate/position/delta field,
    which is also `None` whenever its own data source is unwired,
    independent of `insufficient_data` -- one "no reading" value
    throughout, not two)."""
    reason = _insufficiency_reason(candidate)
    if reason is not None:
        return StoryFrame(
            kind=candidate.kind,
            team_id=candidate.team_id,
            player_id=candidate.player_id,
            insufficient_data=reason,
            standings_position=None,
            standings_position_delta=None,
            streak_length=None,
            season_points_per_game=None,
            season_points_per_game_delta=None,
            season_minutes_per_game=None,
            trade_in_players=0,
            trade_out_players=0,
            trade_in_picks=0,
            trade_out_picks=0,
            motivation_axis=None,
            motivation_magnitude=None,
            request_state=None,
            award_share_estimate=None,
        )

    return StoryFrame(
        kind=candidate.kind,
        team_id=candidate.team_id,
        player_id=candidate.player_id,
        insufficient_data=None,
        standings_position=candidate.standings_position,
        standings_position_delta=_delta(
            candidate.standings_position, candidate.standings_position_at_last_pulse
        ),
        streak_length=candidate.streak_length,
        season_points_per_game=candidate.season_points_per_game,
        season_points_per_game_delta=_delta(
            candidate.season_points_per_game, candidate.season_points_per_game_at_last_pulse
        ),
        season_minutes_per_game=candidate.season_minutes_per_game,
        trade_in_players=len(candidate.trade_in_player_ids),
        trade_out_players=len(candidate.trade_out_player_ids),
        trade_in_picks=len(candidate.trade_in_pick_ids),
        trade_out_picks=len(candidate.trade_out_pick_ids),
        motivation_axis=candidate.motivation_axis,
        motivation_magnitude=candidate.motivation_magnitude,
        request_state=candidate.request_state,
        award_share_estimate=candidate.award_share_estimate,
    )
