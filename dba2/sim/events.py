"""`BeatRecord` / `CreditEvent` / `PossessionResult` / `GameResult` record
shapes (phase-1-simulation-engine.md §4.5, §7.1, §8.1, Decision P1-14/P1-23).

`TerminalKind` is Decision P1-14's closed 9-member outcome enum ("every
possession terminates in exactly one member of a closed outcome enum, and
a unit test asserts exhaustiveness") -- see the class docstring for which
members are actually terminal (docs/design/turnover-recovery.md TR-4.13).
`TerminalOutcome` is that member *with its own payload* -- zone, assist
flag, blocked/steal attribution, FT count, as §4.5's "PossessionResult's
actual shape" paragraph describes -- represented as a frozen dataclass
wrapping `kind` plus every payload field a terminal record can carry,
rather than a bare enum, because a plain `StrEnum` member cannot hold
per-instance data (an and-one's scorer id, a block's defender id) the way
this decision requires.

`CreditEvent` is a type alias for the identical dataclass, per §4.5's own
wording: "A `CreditEvent` is the same shape a terminal outcome's payload
already has for the same real-world fact." One dataclass, two names, so
the two can never drift apart from each other by construction.

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TerminalKind(StrEnum):
    """Decision P1-14's closed set of real-world facts a possession record
    can carry. Terminal-ness is a **per-member property**, not a property
    of the set (docs/design/turnover-recovery.md TR-4.13), and three
    members are carve-outs, each for a different reason:

    * `PERIOD_EXPIRED` is declared but no production code constructs it --
      `game.py`'s module docstring flags that as a known, accepted gap (a
      possession running past the buzzer finishes on its own kind).
    * `NON_SHOOTING_FOUL` is the terminal record in the bonus, and outside
      the bonus the same real-world event is only ever a `CreditEvent` --
      see §4.5's "what 'terminates' means" paragraph.
    * `LOOSE_BALL_RECOVERED` is never terminal at all: the offense won its
      own loose ball, so the possession continues. `possession.py` only
      offers the leaf while the continuation budget has room (TR-4.9), so
      every drawn recovery is followed by another beat (TR-4.12). It is a
      narrated fact with no box-score line: not a turnover, not a steal."""

    MADE_FG = "made_fg"
    MISSED_FG = "missed_fg"
    TURNOVER_LIVE = "turnover_live"
    TURNOVER_DEAD = "turnover_dead"
    SHOOTING_FOUL = "shooting_foul"
    NON_SHOOTING_FOUL = "non_shooting_foul"
    OFFENSIVE_FOUL = "offensive_foul"
    LOOSE_BALL_RECOVERED = "loose_ball_recovered"
    PERIOD_EXPIRED = "period_expired"


class Zone(StrEnum):
    RIM = "rim"
    MID = "mid"
    THREE = "three"


@dataclass(frozen=True)
class TerminalOutcome:
    """One real-world possession-ending (or continuation-carrying) fact.
    Every field beyond `kind` is optional/defaulted because which fields
    apply depends entirely on `kind` (a `TURNOVER_DEAD` carries none of
    them; a `MADE_FG` carries `zone`/`assisted`/`and_one`/`scorer_id`/
    `passer_id`). §9's Tier-2 invariants are what actually enforce
    per-`kind` field consistency over a seeded run, not this dataclass's
    typing -- a plain frozen dataclass cannot express "these fields are
    required only when `kind == MADE_FG`" without a much heavier
    discriminated-union machinery this codebase does not otherwise use.
    """

    kind: TerminalKind

    # MADE_FG / MISSED_FG
    zone: Zone | None = None
    and_one: bool = False
    assisted: bool = False
    scorer_id: int | None = None
    passer_id: int | None = None

    # MISSED_FG (block)
    blocked: bool = False
    blocking_defender_id: int | None = None

    # TURNOVER_LIVE (steal)
    steal: bool = False
    stealing_defender_id: int | None = None
    turnover_player_id: int | None = None

    # SHOOTING_FOUL / NON_SHOOTING_FOUL / and-one's attached FT trip.
    # `ftm` is an addition on top of steps 1-3's declared shape (flagged
    # in step 4's build report, not a step 1-3 defect): `ft_count` alone
    # names how many attempts a trip carries, never how many went in, and
    # `boxscore.py` needs the make count to accumulate `ftm` -- purely
    # additive (defaulted), so no step 1-3 caller or test is affected.
    ft_count: int = 0
    ftm: int = 0
    fouler_id: int | None = None
    fouled_player_id: int | None = None
    foul_type: str | None = None

    # OFFENSIVE_FOUL
    offensive_fouler_id: int | None = None

    # LOOSE_BALL_RECOVERED (docs/design/turnover-recovery.md TR-4.11) --
    # neither id is read by boxscore.py or live_box.py; both exist for the
    # play-by-play sentence and the beat log. The recovery payload leaves
    # every scoring-adjacent field above at its default, and that field
    # selection IS the no-stats guarantee (TR-4.3): those defaults are the
    # only channels through which any event reaches the box score.
    loose_ball_player_id: int | None = None  # who lost the handle
    recovering_player_id: int | None = None  # who won it back (may be the same player)

    # rebound continuation (offensive board or a live missed-final-FT board)
    rebounder_id: int | None = None
    rebound_is_offensive: bool | None = None


# §4.5: "A CreditEvent is the same shape a terminal outcome's payload
# already has for the same real-world fact." One dataclass, two names.
type CreditEvent = TerminalOutcome


@dataclass(frozen=True)
class PossessionResult:
    """§4.5's amended shape, restated verbatim: "PossessionResult ...
    carries exactly two things: terminal ... and credit_events ..., an
    ordered list with zero or more entries, one per continuation event
    that occurred before the terminal outcome." `credit_events` stays a
    plain `list` (not a `tuple`) to match the design doc's literal typing;
    frozen only blocks reassigning the field, not appending to the list
    it points at, which is fine since a `PossessionResult` is built once,
    by `possession.py` (step 4), and never handed out before it's
    complete."""

    terminal: TerminalOutcome
    credit_events: list[CreditEvent] = field(default_factory=list)


@dataclass(frozen=True)
class BeatRecord:
    """Decision P1-23: structured play-by-play, not prose. "the beat kind,
    the acting player ids by role, the outcome enum, the resulting
    probabilities, the clock, and the score." `actor_ids`/`probabilities`
    are plain `dict`s (not frozen mappings) for the same reason
    `credit_events` above is a plain list -- a `BeatRecord` is constructed
    once, fully populated, by the beat that produces it.

    `home_score`/`away_score` are the score as of the *start* of this
    possession (unchanged Phase 1 meaning -- every one of the seven beat
    constructors in `dba2/sim/beats/` still passes these in directly).
    `period`/`home_score_after`/`away_score_after` (Decision P5-27,
    Phase 5) are stamped onto an already-built record by
    `possession.py::_finish` -- the one point where both facts are
    final -- and are NOT knowable at any of the seven construction
    sites, which is why they default to impossible sentinel values
    (`period` is 1-based; scores are non-negative) rather than to a
    real-looking `0`/`0` that could hide a record that escaped
    unstamped. See `possession.py::_finish` for the stamp and
    `tests/unit/sim/test_beat_record_stamp.py` for the proof that no
    sentinel survives a real game.

    `game_clock_seconds` (Stage D commit 0, P5-97.b) is the *period* clock
    (seconds remaining in the period, 720.0/300.0 at a period's tip) as of
    the *start* of this possession -- `game.py` decrements
    `game_state.clock_seconds` only after `simulate_possession` returns,
    so `_finish` reads the possession's own opening value. It is stamped
    in the same `replace(...)` call as the three fields above and defaults
    to `-1.0` for the same reason: the clock is non-negative, so a record
    that escapes unstamped fails a test instead of reading as a plausible
    buzzer-beater. The clock at a possession's END is deliberately not
    stamped; it is derived from the next possession's start
    (design: phase-5-stage-d-broadcast.md §2.1). Do not confuse it with
    `clock_seconds`, which is the *shot* clock at that beat.

    `terminal`/`possession_credits` (watch-v2 E-1) carry the possession's
    own `TerminalOutcome` and its ordered credit events, and are set on the
    LAST record of a possession only -- every other record keeps the
    defaults (`None`/`()`). They are stamped in the same `_finish` pass as
    the fields above, and they are read-only data for consumers of the beat
    log: no draw, score or box accumulation reads them. The last record is
    a `resolution` or a `rebound` beat, but it can be ANY beat kind: over
    the seed 0-29 corpus 5 of the 7 kinds (`setup`, `on_ball`, `help`,
    `rotation`, `screen`) occur as a last beat only on a violation close.
    Consumers must read `terminal`, never assume the beat kind. The credit events are named
    `possession_credits`, not `credit_events`, because `PossessionResult`
    already owns that name. Both fields are frozen dataclass values, so
    records may share them safely."""

    beat: str
    actor_ids: dict[str, int]
    outcome: str
    probabilities: dict[str, float]
    clock_seconds: float
    home_score: int
    away_score: int
    period: int = 0
    home_score_after: int = -1
    away_score_after: int = -1
    game_clock_seconds: float = -1.0
    terminal: TerminalOutcome | None = None
    possession_credits: tuple[TerminalOutcome, ...] = ()


@dataclass(frozen=True)
class PlayerBoxLine:
    """A per-player box line in the engine's own output shape -- not the
    `player_game_stats` domain row. Deliberately provisional: the real
    accumulator (`boxscore.py`, step 5) owns which fields exist and how
    they're derived from a run of `PossessionResult`s; this mirrors
    `domain.stats.PlayerGameStats`' counting-stat columns (minus the
    DB-identity columns `game_id`/`league_id`, which `game_sim_service`
    attaches at persistence time, and with `minutes_played` as `float`
    per Decision P1-5's "the engine produces minutes as float" rule) so
    `GameResult` has a concrete, compiling shape today rather than an
    `Any`. Flagged as an interpretive call in this dispatch's build
    report -- step 5's builder should feel free to change this shape."""

    player_id: int
    team_id: int
    minutes_played: float
    points: int = 0
    fga_rim: int = 0
    fgm_rim: int = 0
    fga_mid: int = 0
    fgm_mid: int = 0
    fga_three: int = 0
    fgm_three: int = 0
    fta: int = 0
    ftm: int = 0
    oreb: int = 0
    dreb: int = 0
    ast: int = 0
    stl: int = 0
    blk: int = 0
    tov: int = 0
    pf: int = 0
    plus_minus: int = 0


@dataclass(frozen=True)
class TeamBoxLine:
    """Team-level counterpart to `PlayerBoxLine`, mirroring
    `domain.stats.TeamGameStats` (same provisional-shape caveat)."""

    team_id: int
    possessions: int = 0
    points: int = 0
    fga_rim: int = 0
    fgm_rim: int = 0
    fga_mid: int = 0
    fgm_mid: int = 0
    fga_three: int = 0
    fgm_three: int = 0
    fta: int = 0
    ftm: int = 0
    oreb: int = 0
    dreb: int = 0
    ast: int = 0
    stl: int = 0
    blk: int = 0
    tov: int = 0
    pf: int = 0


@dataclass(frozen=True)
class GameResult:
    """§8.1: "the engine's whole output surface: final score, period
    scores, per-player box lines, per-team box lines, overtime count, and
    the ordered BeatRecord list." Deliberately does NOT carry
    `roster_digest` -- per Decision P1-22, that is computed by
    `build_context` from the *available* roster before the engine ever
    runs, so `game_sim_service.sim_game` already has it in hand and
    attaches it to the persisted `games` row directly, never through this
    type."""

    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    period_scores: tuple[tuple[int, int, int], ...]  # (period, home_points, away_points)
    overtime_periods: int
    player_box_lines: tuple[PlayerBoxLine, ...]
    team_box_lines: tuple[TeamBoxLine, ...]
    beat_log: tuple[BeatRecord, ...]
