"""`dba2/media/salience.py` -- Decision P4-29: relevance is a salience
score, pure, and the cycle emits top-K above a floor, not everything above
a floor. docs/design/phase-4-media-and-player-agency.md §7, §11 Stage D
step 17.

Four terms, in the decision's own order: magnitude (how big was the
move), subject prominence (a star's milestone outranks a bench player's),
recency/novelty (the same subject repeatedly is worth less), and kind
weight. **Every weight, threshold, floor and K value below is disclosed,
not calibrated** -- the same posture `dba2/agency/motivation.py`'s
`TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD`/`DEFAULT_LADDER_BARS` and
`dba2/media/frame.py`'s `_MIN_TEAM_GAMES_FOR_SAMPLE` already carry, for
the reason this whole phase states repeatedly: no real corpus of
generated media stories exists yet to calibrate against, and presenting a
first-draft number as derived would be worse than disclosing that it is
not.

**Retyped to `StoryPackage` (docs/design/media-v2-newsroom.md MV-1b(b),
"Build order" step 3a).** `SalienceCandidate.frame: StoryFrame` becomes
`.package: StoryPackage`; `_magnitude` reads a NAMED fact key through
`package.number(...)` per branch, each key a real member of that kind's
`FACT_KEYS_BY_KIND` entry, rather than a `StoryFrame` attribute. The old
`insufficient_data` short-circuit has NO package analogue and is deleted
here, not translated -- it moved one layer upstream, into `dba2/media/
detect/legacy.py::adapt_frame`, which returns `None` for an insufficient
frame and therefore never produces a package for `score_candidates` to see
at all (the identical "never scored, never persisted" path, now diagnosed
earlier). `above_floor`/`select_top_k` are UNCHANGED by this retype --
verified line by line: both read only `ScoredCandidate.score`, never a
frame, a package, or a kind.

Zero I/O, the same purity rules `dba2/media/frame.py`/`dba2/media/pulse.py`
already carry: no `dba2.data`, no `datetime.now()`, no bare
`random.Random(`, no `open(`, no `os.environ`. Imports `dba2.domain.
media_facts` (`StoryPackage`), `dba2.domain.media` (`StoryKind`) and
stdlib.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import StoryPackage
from dba2.domain.phase import Phase

# ---------------------------------------------------------------------------
# Kind weight -- Decision P4-29's fourth term. Every `StoryKind` member has
# an entry; `tests/unit/test_media_salience.py::test_every_story_kind_has_
# a_kind_weight` iterates the real enum to guarantee it, the same
# exhaustiveness shape `dba2/media/frame.py`'s own kind-groups already
# have, for the identical reason: a future kind added to the enum with no
# matching entry here must fail loudly, never silently score as if the
# kind did not matter.
# ---------------------------------------------------------------------------

KIND_WEIGHTS: dict[StoryKind, float] = {
    StoryKind.HOLDOUT_WATCH: 1.00,
    StoryKind.TRADE_REACTION: 0.90,
    StoryKind.TRADE_EXECUTED: 0.90,
    StoryKind.TRADE_FALLOUT: 0.85,
    StoryKind.TRADE_BROKEN_PROMISE: 0.85,
    StoryKind.REVENGE_GAME: 0.85,
    StoryKind.PLAYER_UNREST: 0.80,
    StoryKind.RIVALRY_MATCHUP: 0.70,
    StoryKind.STANDINGS_SURGE: 0.70,
    StoryKind.STANDINGS_COLLAPSE: 0.70,
    StoryKind.AWARD_RACE: 0.65,
    StoryKind.HOT_STREAK: 0.65,
    StoryKind.COLD_STRETCH: 0.65,
    StoryKind.STREAK: 0.60,
    StoryKind.DEFENSIVE_STANDOUT: 0.60,
    StoryKind.SIGNING_REACTION: 0.55,
    StoryKind.TRADE_MARKET_WATCH: 0.55,
    StoryKind.ROSTER_CONSTRUCTION: 0.50,
    StoryKind.TEAM_PROFILE: 0.50,
    StoryKind.MILESTONE: 0.45,
    StoryKind.SCHEME_IDENTITY: 0.45,
    StoryKind.ROOKIE_WATCH: 0.35,
}
"""Disclosed ranking, not measured: a holdout is about the most newsworthy
thing that can happen to a player short of a trade, and a rookie's routine
per-game milestone is the least. `TRADE_REACTION` outranks `TRADE_FALLOUT`
only because it is the one with a real, disclosed path toward production
wiring (`dba2/media/events.py`'s own gap note) -- an arbitrary tiebreak
between two kinds neither of which has a calibration corpus behind it, not
a claim that fallout stories matter less in principle.

**MV-2's eleven new members (docs/design/media-v2-newsroom.md, "Build
order" step 4), same disclosed-not-measured status, placed by analogy to
the closest-fitting existing member rather than derived:** `TRADE_EXECUTED`
mirrors `TRADE_REACTION` (the same underlying event, now with a real
production path, MV-2a); `TRADE_BROKEN_PROMISE`/`REVENGE_GAME` sit with
`TRADE_FALLOUT` (the irony/drama angle on a trade is not a lesser story than
the trade itself; `REVENGE_GAME`'s `0.85` is the value revisions 1-3 already
carried for it, per MV-2i's own text, kept rather than re-guessed now that
a real detector exists); `RIVALRY_MATCHUP` sits with the standings-tier
kinds (a scheduled-game preview is a real, dated hook, MV-2f); `HOT_STREAK`/
`COLD_STRETCH` sit just above `STREAK` (real per-36-vs-baseline shooting
data backs them, MV-2c, not just a running win/loss counter);
`DEFENSIVE_STANDOUT` sits at `STREAK`'s own tier (the identical data source
and eligibility gate, MV-2d); `TRADE_MARKET_WATCH` sits with
`SIGNING_REACTION` (speculative-but-grounded market content, MV-2g);
`ROSTER_CONSTRUCTION`/`SCHEME_IDENTITY` are the most analytical, least
urgent kinds (structural observations about a roster or a scheme, not an
event, MV-2e/2g) and sit at the bottom alongside `MILESTONE`.
**`TEAM_PROFILE`'s `0.50` is close to irrelevant by construction**, not a
considered editorial placement: `salience.build_publication_plan`'s
`profile` group bypasses both `SALIENCE_FLOOR` and `TOP_K_PER_CYCLE`
entirely (MV-3's "profiles bypass the floor and K" rule) -- every profile
candidate publishes regardless of its score -- so this entry exists only
because `score_candidates`/`_kind_weight`'s own exhaustiveness guard
requires every `StoryKind` to have one, and `media_stories.salience` is a
real, non-null column a profile row still has to carry a bounded number
into.

**`SCHEME_IDENTITY` is a real, disclosed decision, not an oversight**
(BLOCKING 2, critic round on commits `b288b8e`..`99db9f4`): **this kind is
"always eligible, K-bounded, not floor-bounded," and no magnitude branch
this module could add changes that, proven below rather than asserted.**
`scheme_identity_package` (`dba2/media/detect/scheme.py`) fires for every
team with at least one game played, every cycle -- unlike every other new
kind, it has no eligibility gate narrowing which teams produce a candidate
at all. Combined with `score_candidates`' own formula, that is a real
volume problem the floor cannot solve, and the reason is arithmetic, not
this kind's magnitude specifically: with `subject_prominence` still
`None`-neutral for every `TEAM`-subject package (no wiring exists for one,
MV-5's own "subject prominence finally gets a real signal" section only
wires `PLAYER`/motivation subjects) and `cycles_since_last_seen` still
`None` for every candidate (no production caller supplies one before step
6), those two terms alone contribute a FIXED `_PROMINENCE_WEIGHT *
_NEUTRAL_PROMINENCE + _NOVELTY_WEIGHT * 1.0 = 0.25 * 0.5 + 0.15 * 1.0 =
0.275` to every candidate's score regardless of magnitude. Add
`_KIND_WEIGHT_WEIGHT * KIND_WEIGHTS[kind]` (`0.20 * 0.45 = 0.09` for this
kind) and the floor (`0.30`) is already cleared at `0.365` **before
`_magnitude` contributes anything at all** -- `_magnitude` is clamped to
`[0, 1]` and weighted `0.40`, so it can only ever ADD to that `0.365`,
never subtract. **This holds for every `StoryKind` whose `KIND_WEIGHTS`
entry is `>= 0.125`** (the point at which `0.275 + 0.20 * weight` alone
reaches `0.30`) -- which is every entry in this table -- so it is not a
`SCHEME_IDENTITY`-specific defect in this module's formula; it is what
"no real novelty/prominence signal wired yet" (a disclosed MV-10 risk,
"the novelty term ... is still wired to its `None` default today") means
for any kind with no eligibility gate of its own. A real `_magnitude`
branch was still added (`_rank_extremity`, below) because it is a genuine
quality improvement -- an unremarkable, dead-average scheme now reads as
less notable than a genuinely extreme one, which the prior fixed `0.5`
never could -- but it is disclosed here as NOT the fix for the volume
problem, because it provably cannot be. **The real fix is step 6's own
job** (MV-1c.4's "real cadence/cap wiring" scope): either a real per-team
novelty derivation for `SCHEME_IDENTITY` specifically (mirroring MV-2h.1's
own "no-repeat" derivation for `TEAM_PROFILE` -- e.g., a team is not
`SCHEME_IDENTITY`-eligible again for N cycles after its last one), or an
explicit persona-level/per-cycle cap on this one kind analogous to
`TEAM_PROFILE`'s own reserved-and-bounded quota rather than a floor/K
admission it was never going to lose. Until step 6 ships one of those,
`TOP_K_BY_PHASE` is the only real ceiling on this kind's volume, and that
is what "K-bounded, not floor-bounded" means."""


def _rank_extremity(rank: float, league_size: float) -> float:
    """`0.0` at the median rank, `1.0` at either edge (`rank == 1` or
    `rank == league_size`) -- a real, deterministic "how extreme is this
    measurement" reading built ONLY from facts every `SCHEME_IDENTITY`
    package already carries (`pace_rank`/`avg_possessions_rank`/
    `three_share_rank`/`league_size`), needing no new detector read. Chosen
    over the design doc's own MV-5 anchor (`declared_vs_measured_gap / 40`)
    because that anchor was never a real, unambiguous formula -- MV-5's own
    parenthetical ("both inputs are 0-100 scale knobs") does not hold for
    `avg_possessions`/`three_share`, which are not 0-100 knobs, and no doc
    revision ever specifies the scaling that would make the subtraction
    well-defined. Rank extremity is well-defined for every input this
    detector already emits and needs no new interpretation to implement."""
    return abs(rank - (league_size + 1) / 2) / ((league_size - 1) / 2)


def _magnitude(package: StoryPackage) -> float:
    """Per-kind magnitude, normalized to `[0, 1]` -- **unconditionally
    clamped at this function's one exit** (MV-1b(b)/MV-5: the old version
    left a branch that could return outside `[0, 1]` uncaught; this is the
    fix `profile_interest`/`profile_priority`'s own split (MV-2h.2) argued
    for generally). Every branch reads a NAMED fact key through `package.
    number(...)`, each key a real member of that kind's `FACT_KEYS_BY_KIND`
    entry -- **`TRADE_REACTION`'s asset sum is now three terms
    (`trade_in_players`/`trade_out_players`/`trade_in_picks`), not four**:
    `trade_out_picks` is not in that kind's registry entry (MV-8's own
    "full shipped vocabulary" table, swept from the shipped library
    directly, never declares it), and MV-1b(b)'s own rule is that every key
    this function reads must be a real registry member. `TRADE_FALLOUT` has
    NO registered numeric key at all (MV-8's table: no trigger features)
    and therefore falls to the same fixed-neutral branch `MILESTONE`/
    `AWARD_RACE`/`ROOKIE_WATCH`/`SIGNING_REACTION` already use -- a
    disclosed narrowing from the old frame-attribute read, which had no
    registry to answer to and read `StoryFrame`'s trade-asset counts for
    `TRADE_FALLOUT` even though no shipped template ever triggers on them;
    `TRADE_FALLOUT` has no real production caller today (the Problem
    section's own verified count) and no test asserts its magnitude value
    (only that every kind's score is `[0, 1]`-bounded), so this changes
    nothing observable."""
    kind = package.story_kind
    if kind in (StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE):
        delta = package.number("standings_position_delta")
        # A 15-spot swing is a full-league-width move -- disclosed anchor.
        value = 0.0 if delta is None else abs(delta) / 15.0
    elif kind is StoryKind.STREAK:
        streak = package.number("streak_length")
        value = 0.0 if streak is None else abs(streak) / 15.0
    elif kind is StoryKind.TRADE_REACTION:
        trade_in_players = package.number("trade_in_players") or 0.0
        trade_out_players = package.number("trade_out_players") or 0.0
        trade_in_picks = package.number("trade_in_picks") or 0.0
        assets = trade_in_players + trade_out_players + trade_in_picks
        # A 3-for-3 (6 total assets on one side) reads as a mega-trade.
        value = assets / 6.0
    elif kind in (StoryKind.PLAYER_UNREST, StoryKind.HOLDOUT_WATCH):
        magnitude = package.number("motivation_magnitude")
        # Morale's own [-100, 100] scale (Decision P4-7).
        value = 0.5 if magnitude is None else abs(magnitude) / 100.0
    elif kind is StoryKind.TRADE_EXECUTED:
        # MV-2's own new, real trade kind -- the identical "assets/6" anchor
        # TRADE_REACTION already uses, over its own four real count keys.
        players_acquired = package.number("players_acquired_count") or 0.0
        players_sent = package.number("players_sent_count") or 0.0
        picks_acquired = package.number("picks_acquired_count") or 0.0
        picks_sent = package.number("picks_sent_count") or 0.0
        value = (players_acquired + players_sent + picks_acquired + picks_sent) / 6.0
    elif kind is StoryKind.REVENGE_GAME:
        # MV-2i's own instruction: "the existing trade anchor -- total
        # assets in that transaction / 6 -- not a fourth normalization for
        # the same underlying quantity."
        assets = package.number("trade_asset_count")
        value = 0.0 if assets is None else assets / 6.0
    elif kind in (StoryKind.HOT_STREAK, StoryKind.COLD_STRETCH):
        window = package.number("window_points_per36")
        baseline = package.number("baseline_points_per36")
        # The same +/-6.0 firing delta MV-2c's own threshold uses as its
        # own anchor -- a streak right at the firing line reads as a
        # baseline (barely) newsworthy move; twice that reads as maxed.
        value = 0.5 if window is None or baseline is None else abs(window - baseline) / 12.0
    elif kind is StoryKind.DEFENSIVE_STANDOUT:
        window = package.number("window_stocks_per36")
        baseline = package.number("baseline_stocks_per36")
        value = 0.5 if window is None or baseline is None else abs(window - baseline) / 3.0
    elif kind is StoryKind.RIVALRY_MATCHUP:
        rivalry_score = package.number("rivalry_score")
        # MV-2f's own scoring table maxes at 8 (2 division + 3 close games
        # + 1 season series + 1 trade + 1 h2h) -- the real ceiling, not a
        # guessed one.
        value = 0.0 if rivalry_score is None else rivalry_score / 8.0
    elif kind is StoryKind.SCHEME_IDENTITY:
        # BLOCKING 2 (critic round on commits b288b8e..99db9f4): this kind
        # had NO branch at all before this fix (fell to the fixed-neutral
        # 0.5 below) -- see this branch's own docstring note in the module
        # docstring for why a real branch here does not, by itself, make
        # this kind genuinely floor-sensitive; it is still a real quality
        # fix (an unremarkable, dead-average scheme now reads as less
        # notable than a genuinely extreme one, which the old fixed 0.5
        # never could).
        league_size = package.number("league_size")
        ranks = (
            package.number("pace_rank"),
            package.number("avg_possessions_rank"),
            package.number("three_share_rank"),
        )
        if league_size is None or league_size <= 1 or any(r is None for r in ranks):
            value = 0.5
        else:
            value = max(_rank_extremity(r, league_size) for r in ranks)  # type: ignore[arg-type]
    else:
        value = 0.5
    return max(0.0, min(value, 1.0))


_NEUTRAL_PROMINENCE = 0.5
"""`subject_prominence` is `None`-neutral (Decision P4-9's convention,
reused here): a caller that has not wired a prominence signal for this
subject yet (no talent/`star_leverage` read plumbed through -- no
production caller does this step) reads as an average player, never as a
bench player OR a superstar. The same "absence is not the worst case"
rule every other Phase 4 pure layer already follows."""

_NOVELTY_FULL_CYCLES = 4
"""Disclosed. A subject not seen in the last 4 cycles reads as fully
novel again; a subject seen last cycle reads at its least novel. No
measured repeat-rate corpus exists for media yet -- Decision P4-29's own
text: "both scopes are marked provisional pending the Stage A volume
measurement," and the volume measurement Stage A actually ran covers
discrete-event COUNTS, not repeat CADENCE, since no selection engine
exists yet to measure cadence against (that is step 18's job)."""


def _novelty(cycles_since_last_seen: int | None) -> float:
    if cycles_since_last_seen is None:
        return 1.0
    return min(max(cycles_since_last_seen, 0) / _NOVELTY_FULL_CYCLES, 1.0)


_MAGNITUDE_WEIGHT = 0.40
_PROMINENCE_WEIGHT = 0.25
_NOVELTY_WEIGHT = 0.15
_KIND_WEIGHT_WEIGHT = 0.20
"""The four term weights, summing to `1.0` so a candidate maxed on every
term scores exactly `1.0`. Magnitude weighted highest, because it is the
term this step's real producers most often supply a genuine (non-
placeholder) value for; kind weight next, because it is the one term that
is ALWAYS real (every kind has an entry). Disclosed, not derived -- same
status as every constant above."""


@dataclass(frozen=True)
class SalienceCandidate:
    """One scoring input -- the package plus the two signals neither
    `StoryFrame` nor `StoryPackage` itself carries. Deliberately NOT added
    as `StoryPackage` fields (MV-1b(a)'s own reasoning, carried over
    unchanged from `StoryFrame`'s closed field list): prominence/novelty
    are this module's own scoring concern, not the subject's vocabulary."""

    package: StoryPackage
    subject_prominence: float | None = None
    cycles_since_last_seen: int | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: SalienceCandidate
    score: float


def _kind_weight(kind: StoryKind) -> float:
    """Round 10 critic fix (nit 9): `KIND_WEIGHTS[kind]` used to raise a
    bare `KeyError` for an unhandled enum member -- a new `StoryKind`
    added with no matching entry would crash confusingly deep inside a
    scoring formula, rather than the loud, explanatory `AssertionError`
    shape this project's own house style uses for the identical failure
    class (`dba2/media/frame.py`'s own exhaustiveness guard, `derive_
    frame`'s kind-group check)."""
    weight = KIND_WEIGHTS.get(kind)
    if weight is None:
        raise AssertionError(
            f"StoryKind.{kind.name} has no entry in KIND_WEIGHTS -- every "
            "StoryKind member must be scored explicitly, or this check "
            "silently reads a new kind as worth nothing"
        )
    return weight


def score_candidates(candidates: Sequence[SalienceCandidate]) -> tuple[ScoredCandidate, ...]:
    """Scores every candidate handed to it. **The old `insufficient_data`
    branch has no package analogue and is deleted here, not translated**
    (MV-1b(b)) -- the short-circuit moved one layer upstream, into `dba2/
    media/detect/legacy.py::adapt_frame`, which returns `None` for an
    insufficient frame and therefore never produces a `StoryPackage` for
    this function to see at all. A `SalienceCandidate` reaching this
    function is therefore always a real, sealed package by construction of
    its only real caller (`dba2.services.media_service.seal_package`);
    there is nothing left for this function itself to filter.

    **Round 10 critic fix (nit 3).** `subject_prominence` is clamped to
    `[0.0, 1.0]` here, at the one point it enters the formula -- a
    caller-supplied value outside that range (`1000.0`, `-50.0`) used to
    break this module's own stated "a candidate maxed on every term scores
    exactly 1.0" bound. `None` still reads as `_NEUTRAL_PROMINENCE` before
    the clamp is applied (an explicit, real `0.0`/`1.0` supplied by a
    caller is a genuine boundary value, not a signal of absence, so it is
    clamped rather than treated as `None`-neutral)."""
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        prominence = (
            _NEUTRAL_PROMINENCE
            if candidate.subject_prominence is None
            else max(0.0, min(1.0, candidate.subject_prominence))
        )
        score = (
            _MAGNITUDE_WEIGHT * _magnitude(candidate.package)
            + _PROMINENCE_WEIGHT * prominence
            + _NOVELTY_WEIGHT * _novelty(candidate.cycles_since_last_seen)
            + _KIND_WEIGHT_WEIGHT * _kind_weight(candidate.package.story_kind)
        )
        scored.append(ScoredCandidate(candidate=candidate, score=score))
    return tuple(scored)


SALIENCE_FLOOR = 0.30
"""Disclosed. Keeps trivia out (Decision P4-29) -- a candidate scoring
below this has a below-neutral reading on roughly every term it carries
(low magnitude, average-or-below prominence, not especially novel, a
lower-tier kind), which is the profile of "technically a change, not
actually news." Not derived from any real story corpus -- none exists yet
to derive it from."""

TOP_K_PER_CYCLE = 8
"""docs/design/media-v2-newsroom.md MV-5, "Build order" step 6: the
default K for any phase not in `TOP_K_BY_PHASE` below -- unreachable for a
real caller today (media only ever runs on the nine cycle-bearing phases,
all of which are keyed in that table), kept as a safe, disclosed fallback
rather than a `KeyError`. Was `12` and flat across every cycle prior to
this step -- Stage A's own census (`docs/design/phase-4-stage-a-census.md`,
"Total and the K-sizing warning") measured the real per-cycle candidate
distribution as sharply peaked (FREE_AGENCY ~70-140 candidates, DRAFT
~30-60, the two trade-window cycles ~24 each, most other cycles near
zero) and explicitly recommended a per-phase K instead, which is what
`TOP_K_BY_PHASE` now is -- this constant is no longer the real cap for any
production cycle, only the table's own fallback value."""

TOP_K_BY_PHASE: dict[Phase, int] = {
    Phase.PRESEASON: 6,
    Phase.REGULAR_SEASON: 8,
    Phase.TRADE_DEADLINE: 12,
    Phase.POST_DEADLINE: 8,
    Phase.DRAFT_LOTTERY: 5,
    Phase.DRAFT: 8,
    Phase.RESIGN_WINDOW: 6,
    Phase.FREE_AGENCY: 12,
    Phase.WAIVERS: 5,
}
"""MV-5's own per-phase table, verbatim -- the nine cycle-bearing phases
(`season_service.CYCLE_BEARING_PHASES`, duplicated by value in `dba2/
services/media_service.py::_CYCLE_BEARING_PHASE_VALUES` for the same
layering reason that constant is duplicated rather than imported), summing
to 70 events a season (MV-5's own "season total: 70 event-driven stories
across 9 cycles" claim). Disclosed, not calibrated -- same status as every
other number in this module."""

HOT_TAKE_FLOOR = 0.18
"""MV-3h/MV-5's own sub-floor for the hot-take band: a candidate scoring
in `(HOT_TAKE_FLOOR, SALIENCE_FLOOR]` whose `story_kind` is one `hot_take`
covers is eligible for the band (MV-3h.0) -- eligible only for `hot_take`,
filled only up to that persona's own remaining per-cycle cap headroom
(enforced by `dba2/services/media_service.py::run_media_selection_cycle`'s
own never-waived cap loop, MV-1c.3 -- this module does not know about caps
at all, it only produces the candidate pool). Disclosed, not calibrated,
same status as `SALIENCE_FLOOR`."""


def above_floor(
    scored: Iterable[ScoredCandidate], *, floor: float = SALIENCE_FLOOR
) -> tuple[ScoredCandidate, ...]:
    """Decision P4-103 Part A: the one definition of "above the floor" --
    `score > floor`, strictly (Round 10 critic fix's own semantics, see
    `select_top_k`'s docstring below for the full history of that `>` vs
    `>=` fix). Extracted here so `select_top_k` and `dba2/services/media_
    service.py::run_media_pulse_cycle`'s own `candidates_above_floor`
    count share exactly one predicate rather than two hand-rolled copies
    that could silently drift apart -- Round 10's own nit 1 already moved
    this exact comparison once (`>=` to `>`), which is what makes a second,
    independently-written copy a demonstrated drift risk rather than a
    defensive worry. Calling this twice per cycle (once inside `select_
    top_k`, once more from `run_media_pulse_cycle` for its own count) is a
    deliberate, disclosed extra linear pass, not an oversight -- the cost
    of the second pass is the price of there being one definition of
    "above the floor.\""""
    return tuple(s for s in scored if s.score > floor)


@dataclass(frozen=True)
class PublicationPlan:
    """docs/design/media-v2-newsroom.md MV-1c.4/MV-1c.5 -- the three-group
    plan, real as of "Build order" step 6: `main` (`select_top_k` at the
    phase's own `TOP_K_BY_PHASE` value), `band` (the hot-take sub-floor
    path, MV-3h), `profile` (the reserved `TEAM_PROFILE` quota, MV-2h).

    `profile` is a plain pass-through scored via the SAME `score_candidates`
    every other candidate uses (MV-3's "one publication plan" rule -- a
    profile bypasses the floor and K, which is what "reserved quota"
    means, but it is still scored, because `media_stories.salience` is
    `DOUBLE PRECISION NOT NULL`).

    **`band` is real as of "Build order" step 6.** It is every candidate
    scoring in `(HOT_TAKE_FLOOR, SALIENCE_FLOOR]` whose `story_kind` is one
    `hot_take` covers (MV-3h.0's declared beat, passed in as `hot_take_
    kinds` data so this module still imports no service layer), ranked
    the same way `main` is (highest score first, stable tie-break).

    **This module does not enforce `hot_take`'s per-cycle cap.** MV-1c.3's
    own "cap is never waived, capped-out is skipped, not filtered" rule
    needs the persona's own draw outcome (a candidate is only ever capped
    out AFTER `select_persona` would have drawn it), so caps are `dba2/
    services/media_service.py::run_media_selection_cycle`'s own job,
    applied to this plan's own concatenated `.candidates`, never this
    module's. `band` here is therefore the FULL admissible pool, not
    pre-truncated to "hot_take's remaining headroom" -- MV-3h's own text
    names that headroom as the real limit, and the never-waived cap loop
    downstream produces exactly that limit by construction: once
    `hot_take` is at cap, every remaining band candidate's eligible-
    persona set is empty and it is skipped (counted `capped_out`), the
    identical outcome a pre-truncation would have produced, arrived at by
    the one general mechanism rather than a second, band-specific copy."""

    main: tuple[ScoredCandidate, ...]
    band: tuple[ScoredCandidate, ...]
    profile: tuple[ScoredCandidate, ...]

    @property
    def candidates(self) -> tuple[ScoredCandidate, ...]:
        """The one concatenated sequence `run_media_selection_cycle`'s own
        single `enumerate()` walks -- main, then band, then profile, always
        in that order, so a caller's own group-boundary bookkeeping (`plan.
        main`/`len(plan.band)`/...) stays valid against the same indices
        the loop assigns as `candidate_sequence`."""
        return self.main + self.band + self.profile


def _band_candidates(
    scored: Sequence[ScoredCandidate], *, hot_take_kinds: frozenset[StoryKind]
) -> tuple[ScoredCandidate, ...]:
    """MV-3h.0's own admission rule for the hot-take band: `HOT_TAKE_FLOOR
    < score <= SALIENCE_FLOOR` (strictly above the sub-floor, at-or-below
    the main floor, so a candidate is in exactly one of `main`/`band`,
    never both and never neither in the gap) AND `story_kind` is one
    `hot_take` actually covers (a candidate of an uncovered kind was never
    going to draw `hot_take` anyway -- admitting it would only ever
    produce an empty eligible-persona set downstream; MV-3h.0's own "this
    also makes the band's rule safe by construction" point). Ranked
    highest-score-first, stable on ties -- the identical shape `select_
    top_k` already uses, so when the cap binds the candidates skipped are
    the least salient ones, not an arbitrary subset."""
    admissible = [
        s
        for s in scored
        if HOT_TAKE_FLOOR < s.score <= SALIENCE_FLOOR
        and s.candidate.package.story_kind in hot_take_kinds
    ]
    return tuple(sorted(admissible, key=lambda s: -s.score))


def build_publication_plan(
    scored: Sequence[ScoredCandidate],
    *,
    phase: Phase,
    hot_take_kinds: frozenset[StoryKind] = frozenset(),
    profile_candidates: Sequence[SalienceCandidate] = (),
) -> PublicationPlan:
    """docs/design/media-v2-newsroom.md MV-1c.4 -- the pure grouping step
    `run_media_selection_cycle` delegates to, so "which candidates get
    published" has exactly one producer. `main` is `select_top_k(scored,
    k=TOP_K_BY_PHASE.get(phase, TOP_K_PER_CYCLE))` (MV-5's per-phase K);
    `band` is `_band_candidates` above; `profile` is `score_candidates(
    profile_candidates)`, unconditionally. `hot_take_kinds` defaults to
    `frozenset()` (an empty band) rather than being required, so every
    existing caller/test that predates the band keeps its old, band-less
    behavior unless it explicitly names `hot_take`'s covered kinds."""
    k = TOP_K_BY_PHASE.get(phase, TOP_K_PER_CYCLE)
    return PublicationPlan(
        main=select_top_k(scored, k=k),
        band=_band_candidates(scored, hot_take_kinds=hot_take_kinds),
        profile=score_candidates(tuple(profile_candidates)),
    )


def select_top_k(
    scored: Sequence[ScoredCandidate], *, floor: float = SALIENCE_FLOOR, k: int = TOP_K_PER_CYCLE
) -> tuple[ScoredCandidate, ...]:
    """Decision P4-29: top-K ABOVE a floor, not everything above a floor
    and not simply the top K regardless of floor -- both bounds apply
    together, and neither alone reproduces this function.

    **Round 10 critic fix (nit 1).** The floor comparison is `score >
    floor` (strictly above), not `score >= floor` -- P4-29's own text and
    `SALIENCE_FLOOR`'s docstring both say "above a floor," and a candidate
    scoring exactly the floor value used to be emitted anyway under the
    old `>=`. Fixed to match the stated semantics rather than fixing the
    text to match the code, since "above" is the reading `SALIENCE_FLOOR`'s
    own docstring ("keeps trivia out") argues for -- a candidate reading
    exactly at the trivia/news boundary is the trivia case, not the news
    one.

    **Round 18 critic fix (Decision P4-103, Part A).** The floor filter is
    now `above_floor(scored, floor=floor)` (above), not a hand-rolled `[s
    for s in scored if s.score > floor]` -- see that function's own
    docstring for why a second copy of this exact predicate is a
    demonstrated drift risk, not a hypothetical one.

    **Round 10 critic fix (nit 2), a correction to this function's own
    prior claim.** Ties (including the all-tied-scores adversarial case)
    are broken by ORIGINAL INPUT ORDER ONLY: Python's `sorted` is stable
    and the sort key only ever compares `-score`, so two equal-scoring
    candidates keep their relative input order. **This is NOT the same
    determinism convention `managers/voice/engine.py`'s own `template_id`
    sort establishes** -- that convention is a real, order-INDEPENDENT
    content key (ties broken by a stable field's own value, so two equal-
    scoring candidates tie-break identically no matter what order a
    caller happens to hand them in); input-order stability is weaker and
    order-DEPENDENT (a caller that reorders its own input before calling
    this function gets a different tie-break outcome for the identical
    candidate set). The prior version of this docstring analogized to the
    stronger guarantee; this version states the real one."""
    survivors = above_floor(scored, floor=floor)
    ranked = sorted(survivors, key=lambda s: -s.score)
    return tuple(ranked[:k])
