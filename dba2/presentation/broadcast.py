"""The Stage D broadcast's pure half (docs/design/phase-5-stage-d-broadcast.md
§3/§4, Decisions P5-97.b/.c/.t): possessions cut out of a beat log, and
the frames a paced task walks.

**Pure by rule, not by habit.** This module owns no clock and no
concurrency: it never sleeps, never awaits, never touches `asyncio` or
`time`. Pacing belongs to `dba2/bot/broadcast.py`; a pure planner that
also scheduled itself could not be run to completion in a unit test,
which is the property every invariant in design §9 depends on.
`tests/unit/test_layering.py::test_presentation_broadcast_owns_no_clock_
and_no_concurrency` enforces it, with a mutation twin.

**Three halves, one module.** Segmentation (`segment_possessions`) cuts a
beat log into `_Possession`s; planning (`plan_broadcast`) batches them
into the `BroadcastFrame`s a paced task walks; rendering turns a frame into
`Panel`s. /watch v4 (docs/design/watch-v4-live-ticker.md) renders a game as
ONE live-edited component: `render_live_component` draws the score, status,
a live ticker of the last few plays (`ticker_lines`) and a stats block for
whichever scope (Full Game or one period) is selected, gaining Team Totals
and Story at the FINAL frame. `plan_period_lines` renders every play line
once and regroups them by period for the ephemeral full-text pager
(`watch_pager.py`); `visible_periods` says which periods the cycle row may
show. Planning is name-free: a plan holds ids and numbers, and the caller
hands the renderer a `names` mapping (design P5-97.d), so `presentation/`
does no lookup of its own (P5-5).

**Segmentation reads the beat log and nothing else** (design §4.3,
Finding 3). A possession opens on a `setup` beat whose shot clock equals
the log's maximum shot clock: `new_possession_state` opens every
possession at that value, and the one within-possession reset
(`reset_on_offensive_rebound`) is `max(current, 14)` so it can never
return the clock to the maximum. That derivation needs no `sim.tuning`
import, which is what keeps this module inside `_PRESENTATION_ALLOWED_
PREFIXES`.

Every ambiguity raises `ValueError`. A silently mis-segmented log is a
scoreboard that is wrong in a plausible way (design §13.5), so this
module refuses to guess at any of the four places the design names:
where the log opens, whether a possession's stamped facts agree with
themselves, which team is on offense, and whether the score moved the
way one possession can move it.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby

from dba2.domain.player import Player
from dba2.domain.team import Team
from dba2.presentation.live_box import LiveLine, leaders, tally
from dba2.presentation.narration_phrases import (
    KEY_ORDINALS,
    LIBRARY,
    PLACEHOLDERS,
    SETUP_HOLDER,
    Phrase,
)
from dba2.presentation.panel import MAX_DESCRIPTION_CHARS, Panel, PanelColor, PanelField
from dba2.sim.events import BeatRecord, GameResult, TerminalKind, TerminalOutcome, Zone

# `rebound.py:93-95` builds `side_probabilities = {"offense": ..., "defense":
# ...}` and `:114` sets the beat's outcome to that side -- the rebound
# beat's outcome vocabulary is these two words, not a boolean.
_REBOUND_OUTCOME_OFFENSE = "offense"
_REBOUND_OUTCOME_DEFENSE = "defense"


class _Ending(StrEnum):
    """How a possession really ended, read from its beat log alone (design
    P5-97.d's honest rule, widened -- see `_classify_ending`)."""

    CLEAN = "clean"
    PRE_RESOLUTION_VIOLATION = "pre_resolution_violation"
    OFFENSIVE_REBOUND_CLOSE = "offensive_rebound_close"
    NON_BONUS_FOUL_CLOSE = "non_bonus_foul_close"
    UNDETERMINED_FOUL_CLOSE = "undetermined_foul_close"
    RESTART_VIOLATION = "restart_violation"


def _after_last_resolution(beats: Sequence[BeatRecord]) -> Sequence[BeatRecord]:
    for position in range(len(beats) - 1, -1, -1):
        if beats[position].beat == "resolution":
            return beats[position + 1 :]
    return beats


def _tail_rebound(beats: Sequence[BeatRecord]) -> BeatRecord | None:
    """The rebound beat that follows the LAST resolution beat, if any. Not
    `_Possession.rebounder_id`: that is the last rebound of the whole
    possession, and a possession whose last resolution is a foul (which draws
    no rebound of its own) can still carry an earlier one."""
    tail = _after_last_resolution(beats)
    return tail[0] if tail and tail[0].beat == "rebound" else None


# Leaves whose event can never score: a missed field goal carries no free
# throws (`TerminalOutcome.ftm` is only ever set on a foul or an and-one).
# Every other resolution leaf can put points on the board, so an earlier one
# means the possession's delta is not provably the LAST leaf's alone.
# `blocked_miss_help` (BDA-4, docs/design/block-defender-attribution.md) is a
# `MISSED_FG` the same as `blocked_miss` -- the only difference is which
# defender gets `blocking_defender_id`, never whether the shot scored.
# `loose_ball_recovered` (docs/design/turnover-recovery.md TR-5.A.14) is the
# offense winning its own loose ball: its payload carries no `ftm`, no zone,
# nothing that can score (TR-4.11), and it is never the last leaf (TR-4.12),
# so a later foul's points stay provably that foul's own.
_NEVER_SCORING_LEAVES = frozenset(
    {"missed_unblocked", "blocked_miss", "blocked_miss_help", "loose_ball_recovered"}
)


def _foul_points_attributable(beats: Sequence[BeatRecord]) -> bool:
    """Whether the possession's point delta is provably the LAST foul's own
    free throws.

    The beat log carries no free-throw count, and the delta is the SUM over
    every event of the possession (`game.py::_possession_points`), so a foul
    line may say "scores +N" only when no earlier `resolution` beat could have
    scored -- a single resolution beat is the common case, and a miss then an
    offensive rebound then a foul is the other. Otherwise the earlier points
    are not the foul's: seed 257 possession 51 is a 3-shot trip that made two
    followed by a non-bonus foul that shot nothing, and the log alone cannot
    tell that from a bonus foul whose shots all went in.
    `tests/unit/presentation/test_broadcast_ground_truth.py` holds this rule
    to the engine's own record of every possession."""
    resolutions = [b for b in beats if b.beat == "resolution"]
    return all(b.outcome in _NEVER_SCORING_LEAVES for b in resolutions[:-1])


def _classify_ending(
    beats: Sequence[BeatRecord], *, points: int, foul_points_attributable: bool
) -> _Ending:
    """The shapes where the last `resolution` leaf is not how the possession
    really ended (measured ~1.4% of possessions), each detected from the beat
    log's tail alone -- `possession.py:428-434` says the forced terminal
    "represents no *additional* real-world fact", so the engine leaves no beat
    for it:

    * `OFFENSIVE_REBOUND_CLOSE` (`:493`): the log ends on an offensive
      rebound. A restart would have appended another `setup` beat.
    * `NON_BONUS_FOUL_CLOSE` (`:558`): the log ends on a `non_shooting_foul`
      resolution, draws no rebound, and the possession scored NOTHING. A bonus
      foul goes to the line and a missed last free throw draws a rebound beat,
      so a bonus foul with no rebound scored -- which makes a scoreless one
      the non-bonus close-out, whatever came before it.
    * `UNDETERMINED_FOUL_CLOSE`: the same tail on a possession that DID score,
      where an earlier event could have scored it. The log cannot tell a bonus
      foul whose free throws all went in from a non-bonus close-out that
      followed earlier points (seed 257 possession 51), so it says so rather
      than guessing `CLEAN`. When every earlier leaf is a miss the points are
      provably the foul's own and the ending is `CLEAN`.
    * `RESTART_VIOLATION`: beats follow the last resolution (past its own
      rebound), so a continuation restarted after a credit event and the shot
      clock then expired before another resolution -- or, the second shape
      (docs/design/turnover-recovery.md TR-4.14), NO beat follows a last
      resolution whose leaf is `loose_ball_recovered`. A recovery always
      continues the possession (TR-4.12), so being last can only mean the
      scramble itself ran the shot clock out: the scramble-violation close.

    A possession with no `resolution` beat at all is the ordinary
    pre-resolution shot-clock violation."""
    last_resolution = next((b for b in reversed(beats) if b.beat == "resolution"), None)
    if last_resolution is None:
        return _Ending.PRE_RESOLUTION_VIOLATION
    tail = _after_last_resolution(beats)
    rebound = _tail_rebound(beats)
    rest = tail[1:] if rebound is not None else tail
    if rest:
        return _Ending.RESTART_VIOLATION
    if rebound is not None and rebound.outcome == _REBOUND_OUTCOME_OFFENSE:
        return _Ending.OFFENSIVE_REBOUND_CLOSE
    if rebound is None and last_resolution.outcome == "non_shooting_foul":
        if points == 0:
            return _Ending.NON_BONUS_FOUL_CLOSE
        if not foul_points_attributable:
            return _Ending.UNDETERMINED_FOUL_CLOSE
    if rebound is None and last_resolution.outcome == "loose_ball_recovered":
        return _Ending.RESTART_VIOLATION  # TR-4.14's scramble-violation close
    return _Ending.CLEAN


@dataclass(frozen=True, slots=True)
class _PlayEvent:
    """One terminal or credit event of a possession, reduced to the 18 fields
    `boxscore._apply_outcome` reads (watch-v2 W-2: the live tally's exactness
    depends on this list, so it is pinned by a test) **plus exactly two more,
    `loose_ball_player_id` and `recovering_player_id`**, which only the
    loose-ball recovery clause reads and nothing in the box score does
    (docs/design/turnover-recovery.md TR-4.16). Those two are named here
    individually on purpose: the rule is "the box score's fields plus these
    two," a list, not a category a future narration-only field can join
    silently. It is a copy, not the engine's `TerminalOutcome`, so a plan
    never holds an engine object (P5-3) and `foul_type`, which nothing here
    reads, is not carried."""

    kind: TerminalKind
    zone: Zone | None
    and_one: bool
    assisted: bool
    scorer_id: int | None
    passer_id: int | None
    blocked: bool
    blocking_defender_id: int | None
    steal: bool
    stealing_defender_id: int | None
    turnover_player_id: int | None
    ft_count: int
    ftm: int
    fouler_id: int | None
    fouled_player_id: int | None
    offensive_fouler_id: int | None
    rebounder_id: int | None
    rebound_is_offensive: bool | None
    loose_ball_player_id: int | None
    recovering_player_id: int | None


def _play_event(outcome: TerminalOutcome) -> _PlayEvent:
    return _PlayEvent(
        kind=outcome.kind,
        zone=outcome.zone,
        and_one=outcome.and_one,
        assisted=outcome.assisted,
        scorer_id=outcome.scorer_id,
        passer_id=outcome.passer_id,
        blocked=outcome.blocked,
        blocking_defender_id=outcome.blocking_defender_id,
        steal=outcome.steal,
        stealing_defender_id=outcome.stealing_defender_id,
        turnover_player_id=outcome.turnover_player_id,
        ft_count=outcome.ft_count,
        ftm=outcome.ftm,
        fouler_id=outcome.fouler_id,
        fouled_player_id=outcome.fouled_player_id,
        offensive_fouler_id=outcome.offensive_fouler_id,
        rebounder_id=outcome.rebounder_id,
        rebound_is_offensive=outcome.rebound_is_offensive,
        loose_ball_player_id=outcome.loose_ball_player_id,
        recovering_player_id=outcome.recovering_player_id,
    )


@dataclass(frozen=True)
class _Possession:
    """One possession, as the broadcast sees it. Module-private, but the
    tests assert on it (design §4.1).

    `score_before`/`score_after` are `(home, away)`. `points` is the
    offense's delta -- the *possession's* total, never a per-shot value:
    a miss / offensive rebound / put-back possession is one entry with one
    total.

    `events` are the possession's terminal and credit events (watch-v2 E-1),
    read off the LAST beat, in `boxscore`'s own order `[terminal, *credits]`
    -- the order the live tally applies them in. Narration reads them in the
    other, chronological order (credits, then the terminal). Empty when the
    last beat carries no terminal (a hand-built beat log), which is the
    signal to fall back to the leaf grammar.

    `terminal_leaf` is the outcome of the possession's LAST `resolution`
    beat, `None` when it has none (a pre-resolution shot-clock violation:
    `possession.py:642` setup, `:695` screen, `:732` on_ball, `:816`
    help, `:877` rotation). In roughly 1.4% of possessions that is NOT the
    possession's real `TerminalOutcome.kind`: a budget-exhausted
    possession closes through `_turnover_dead()` (`:493`, `:558`) after a
    credit event was already recorded, and the engine's own words say that
    terminal "represents no *additional* real-world fact"
    (`possession.py:428-434`). No branch is added for it, deliberately --
    the last resolution leaf is the more honest thing to display.

    `shooter_id`/`defender_id` come from that same last `resolution` beat.
    `rebounder_id`/`rebound_is_offensive` come from the LAST `rebound`
    beat of the possession (design §4.1 names the fields but not which
    beat when a possession has several): after a miss, an offensive
    rebound and a second miss the last board is the one that ended it.

    `clock_start_seconds` is stamped (design §2); `clock_end_seconds` is
    DERIVED (design §2.1): the next possession's start when it is in the
    same period, else `0.0`.

    **A possession keeps no beats.** A plan holds one of these per possession
    for the life of a broadcast, and the beat records were ~97% of its bytes
    (~287 KB pickled against ~4 KB of frames) though the renderer needs only
    a handful of facts read off them. Those are computed once, in
    segmentation, where the beats are at hand: `ending` (the `_classify_ending`
    verdict), `foul_points_attributable` (whether a foul line may claim the
    delta), and the rebound that follows the last resolution beat -- what
    `_narrate` prints, as opposed to `rebounder_id`, the possession's last
    board of any kind. A new render rule that needs another beat-derived fact
    adds a field here; it does not put the beats back."""

    index: int
    period: int
    offense_is_home: bool
    score_before: tuple[int, int]
    score_after: tuple[int, int]
    points: int
    terminal_leaf: str | None
    shooter_id: int | None
    defender_id: int | None
    rebounder_id: int | None
    rebound_is_offensive: bool | None
    clock_start_seconds: float
    clock_end_seconds: float
    ending: _Ending
    foul_points_attributable: bool
    tail_rebounder_id: int | None
    tail_rebound_is_offensive: bool | None
    events: tuple[_PlayEvent, ...] = ()


def _stamp(record: BeatRecord) -> tuple[int, int, int, int, int, float]:
    """The six facts `possession.py::_finish` writes onto every beat of a
    possession in one pass (`home_score`/`away_score` are the possession's
    entry score, held constant by construction; the other four are the
    stamped fields)."""
    return (
        record.period,
        record.home_score,
        record.away_score,
        record.home_score_after,
        record.away_score_after,
        record.game_clock_seconds,
    )


def _group_by_possession(beat_log: Sequence[BeatRecord]) -> list[list[BeatRecord]]:
    """Design §4.3 steps 1-2. The caller has already handled the empty
    log."""
    shot_clock_start = max(b.clock_seconds for b in beat_log)
    groups: list[list[BeatRecord]] = []
    for position, record in enumerate(beat_log):
        if record.beat == "setup" and record.clock_seconds == shot_clock_start:
            groups.append([])
        elif not groups:
            raise ValueError(
                f"beat log does not open on a possession-opening setup beat: beat {position} "
                f"is {record.beat!r} at shot clock {record.clock_seconds} "
                f"(a possession opens on a setup beat at the log's maximum shot clock, "
                f"{shot_clock_start})"
            )
        groups[-1].append(record)
    return groups


def _offense_is_home_from_actors(
    opener: BeatRecord, *, index: int, player_team: Mapping[int, int], home_team_id: int
) -> bool:
    """Derivation (a) of design §4.3 step 4: the possession's opening
    `setup` beat keys its actors `offense:<role>` / `defense:<role>`
    (`beats/setup.py:226-227`), offense entries first."""
    for key, player_id in opener.actor_ids.items():
        if key.startswith("offense:"):
            if player_id not in player_team:
                raise ValueError(
                    f"possession {index}: offense actor {key}={player_id} is in no team's "
                    f"player_team map"
                )
            return player_team[player_id] == home_team_id
    raise ValueError(f"possession {index}: opening setup beat has no offense:<role> actor")


def _build_possession(
    index: int,
    beats: Sequence[BeatRecord],
    *,
    clock_end_seconds: float,
    player_team: Mapping[int, int],
    home_team_id: int,
) -> _Possession:
    opener = beats[0]

    # Step 3: constancy is asserted, not trusted. A sentinel is a record
    # that skipped `_finish`'s stamp; it would be constant across the
    # possession and so slip past the equality check without this.
    if opener.period < 1 or opener.home_score_after < 0 or opener.away_score_after < 0:
        raise ValueError(f"possession {index}: unstamped period/score_after on its opening beat")
    if opener.game_clock_seconds < 0.0:
        raise ValueError(f"possession {index}: unstamped game_clock_seconds on its opening beat")
    expected = _stamp(opener)
    for record in beats:
        if _stamp(record) != expected:
            raise ValueError(
                f"possession {index}: beats disagree on their stamped "
                f"(period, score, score_after, game_clock) -- {expected} vs {_stamp(record)} "
                f"on a {record.beat!r} beat"
            )

    # Step 4: two independent derivations of who has the ball.
    from_actors = _offense_is_home_from_actors(
        opener, index=index, player_team=player_team, home_team_id=home_team_id
    )
    from_parity = index % 2 == 0  # game.py:269 -- strict alternation, home first
    if from_actors != from_parity:
        raise ValueError(
            f"possession {index}: offense_is_home is {from_actors} from the setup actors but "
            f"{from_parity} from possession parity (game.py:269)"
        )

    # Step 5: one possession moves only the offense's score, and only up.
    score_before = (opener.home_score, opener.away_score)
    score_after = (opener.home_score_after, opener.away_score_after)
    home_delta = score_after[0] - score_before[0]
    away_delta = score_after[1] - score_before[1]
    points, defense_delta = (home_delta, away_delta) if from_actors else (away_delta, home_delta)
    if defense_delta != 0 or points < 0:
        raise ValueError(
            f"possession {index}: score moved {score_before} -> {score_after}, which is not a "
            f"non-negative change to the offense's side alone"
        )

    # Step 6.
    resolution = next((b for b in reversed(beats) if b.beat == "resolution"), None)
    rebound = next((b for b in reversed(beats) if b.beat == "rebound"), None)
    if rebound is not None and rebound.outcome not in (
        _REBOUND_OUTCOME_OFFENSE,
        _REBOUND_OUTCOME_DEFENSE,
    ):
        raise ValueError(f"possession {index}: unrecognized rebound outcome {rebound.outcome!r}")

    tail_rebound = _tail_rebound(beats)
    attributable = _foul_points_attributable(beats)

    # E-1: the payload sits on the LAST beat only, whatever its kind. A payload
    # anywhere else is a log this module does not understand.
    for record in beats[:-1]:
        if record.terminal is not None or record.possession_credits:
            raise ValueError(
                f"possession {index}: a {record.beat!r} beat that is not the possession's last "
                f"carries a terminal payload"
            )
    last = beats[-1]
    if last.terminal is None and last.possession_credits:
        raise ValueError(f"possession {index}: credit events without a terminal")
    events = (
        (_play_event(last.terminal), *(_play_event(credit) for credit in last.possession_credits))
        if last.terminal is not None
        else ()
    )

    # BDA-4 (docs/design/block-defender-attribution.md): for every OTHER
    # leaf, `actor_ids["defender"]` and "who really did this" are the same
    # player. `blocked_miss_help` is the one exception -- `actor_ids
    # ["defender"]` is the shot's PRIMARY contesting defender, but the real
    # blocker is whoever `TerminalOutcome.blocking_defender_id` names (the
    # weak-side helper), which only the enriched terminal on `last` carries.
    # `resolution` is that same beat's own attempt (possibly enriched by a
    # rebound beat's `replace(...)` afterward, which never touches
    # `blocking_defender_id`), so `last.terminal` here is always this
    # `resolution` beat's own outcome, not some other attempt's.
    defender_id = resolution.actor_ids["defender"] if resolution is not None else None
    if (
        resolution is not None
        and resolution.outcome == "blocked_miss_help"
        and last.terminal is not None
    ):
        defender_id = last.terminal.blocking_defender_id

    return _Possession(
        index=index,
        period=opener.period,
        offense_is_home=from_actors,
        score_before=score_before,
        score_after=score_after,
        points=points,
        terminal_leaf=resolution.outcome if resolution is not None else None,
        shooter_id=resolution.actor_ids["shooter"] if resolution is not None else None,
        defender_id=defender_id,
        rebounder_id=rebound.actor_ids["rebounder"] if rebound is not None else None,
        rebound_is_offensive=(
            rebound.outcome == _REBOUND_OUTCOME_OFFENSE if rebound is not None else None
        ),
        clock_start_seconds=opener.game_clock_seconds,
        clock_end_seconds=clock_end_seconds,
        ending=_classify_ending(beats, points=points, foul_points_attributable=attributable),
        foul_points_attributable=attributable,
        tail_rebounder_id=(
            tail_rebound.actor_ids["rebounder"] if tail_rebound is not None else None
        ),
        tail_rebound_is_offensive=(
            tail_rebound.outcome == _REBOUND_OUTCOME_OFFENSE if tail_rebound is not None else None
        ),
        events=events,
    )


def segment_possessions(
    beat_log: Sequence[BeatRecord],
    *,
    player_team: Mapping[int, int],
    home_team_id: int,
) -> tuple[_Possession, ...]:
    """Design §4.3. `player_team` and `home_team_id` stay explicit
    parameters (rather than a `GameResult`) so this is unit-testable
    against a hand-built beat list; `plan_broadcast` derives both from the
    result (§4.2 B4). An empty log is an empty tuple; a log that does not
    open on a possession, or whose possessions disagree with themselves,
    raises `ValueError`.

    The derived end clock (§2.1) reads the *next* possession's stamped
    start: `clock_end(i) = clock_start(i+1)` in the same period, else
    `0.0`. It is exact because `game.py:268`/`:321` drive every period's
    clock to exactly 0.0 (Finding 4) -- an assumption the property test's
    invariant I4 pins rather than this function re-checks."""
    if not beat_log:
        return ()
    groups = _group_by_possession(beat_log)
    possessions: list[_Possession] = []
    for index, beats in enumerate(groups):
        following = groups[index + 1][0] if index + 1 < len(groups) else None
        clock_end = (
            following.game_clock_seconds
            if following is not None and following.period == beats[0].period
            else 0.0
        )
        possessions.append(
            _build_possession(
                index,
                beats,
                clock_end_seconds=clock_end,
                player_team=player_team,
                home_team_id=home_team_id,
            )
        )
    return tuple(possessions)


# --- planning (design §4.1, §4.4) ------------------------------------------


# The narrowest line that still holds the `11:40  🏀 **AWY 2-0**  ` prefix and
# a word or two; below it every line would be an ellipsis-cut prefix.
_MIN_LINE_CHARS = 20
# What a rendered line may carry beyond `max_pbp_line_chars`: the glyph and its
# space, the two `**` around the running score, the extra space after it, and
# the two spaces and star of a key play. The cap governs the sentence; the
# markup is a fixed allowance on top (watch-v2 W-7).
_LINE_MARKUP_ALLOWANCE = 12

_LINE_SEPARATOR = "\n\n"
"""What sits between two play lines: one blank line, so a wrapped line does not run
into the next play on a phone (watch-v3 W-9). ONE constant read by every renderer that
joins play lines into a description -- the live ticker (`_ticker_value`) and the pager
page's join (`render_pager_page`) -- and by `PlanSettings.__post_init__`'s ticker
inequality below, so a description budgeted with a narrower separator than it is
rendered with would overflow (watch-v4 T-1.a)."""

_NEWEST_MARK = "> "
"""The reading modes' newest-line mark (docs/design/watch-narration-v2.md WN-9.2): a
blockquote bar in Discord's embed, added by `_ticker_value` only and never stored on a
line, so every line test and every `_HEAD` regex is untouched."""

TICKER_LINES_FLOOR = 6
"""The floor for `ticker_window_lines` (watch-v5 WT-3): a live ticker window narrower
than this reads as a stutter, however small a preset's `possessions_per_frame` is. The
floor must be defined -- with `ticker_lines` -- before `DEFAULT_PLAN_SETTINGS` is built
below, or that construction raises `NameError`."""


def ticker_lines(settings: PlanSettings) -> int:
    """How many plays the live ticker shows: the larger of the plan's own
    `ticker_window_lines` and its `possessions_per_frame`, so a preset that batches more
    possessions per frame than its stated window never drops a play between two
    consecutive ticks (watch-v4 T-1.a's guarantee, restated for a window that is set
    rather than inferred, watch-v5 WT-3). Every renderer and every test reads this
    helper; no call site hard-codes a number."""
    return max(settings.ticker_window_lines, settings.possessions_per_frame)


_READING_SECONDS = (
    "read_base_seconds",
    "read_per_char_seconds",
    "read_min_seconds",
    "read_max_seconds",
    "score_hold_seconds",
    "key_hold_seconds",
    "period_hold_seconds",
)

_SETTING_FLOORS = {
    "possessions_per_frame": 1,
    "momentum_window_possessions": 1,
    "momentum_margin_threshold": 1,
    "max_pbp_line_chars": _MIN_LINE_CHARS,
    "ticker_window_lines": TICKER_LINES_FLOOR,
}


class RevealUnit(StrEnum):
    """What one paced edit of the live component reveals (WN-10.2). `FRAME` is
    today's batch: a whole frame, `possessions_per_frame` lines at once, on a fixed
    tick (fast mode). `LINE` is one line per edit on the reading-time schedule, with a
    separator line between periods (WN-11) and the newest line marked (WN-9.2) -- the
    two reading modes."""

    FRAME = "frame"
    LINE = "line"


class NarrationLevel(StrEnum):
    """How much of a possession the feed narrates (WN-2.3). `TERSE` is today's one
    line per possession, byte-identical to `_play_lines` (A5); `FULL` adds the
    pre-lines between baskets and splits credit events onto their own lines."""

    TERSE = "terse"
    FULL = "full"


@dataclass(frozen=True)
class PlanSettings:
    """The planner's display constants (design §4.1, P5-97.f). They move no
    metric and enter no gate. `max_pbp_line_chars` caps the sentence of one
    rendered line (the markup adds `_LINE_MARKUP_ALLOWANCE`); neither it nor
    `possessions_per_frame` changes what the planner emits."""

    possessions_per_frame: int = 2
    momentum_window_possessions: int = 10
    momentum_margin_threshold: int = 6
    max_pbp_line_chars: int = 96
    ticker_window_lines: int = 8
    narration: NarrationLevel = NarrationLevel.TERSE
    reveal: RevealUnit = RevealUnit.FRAME
    # WN-10.2's reading-time model, frozen with the plan so a broadcast's pace cannot
    # drift mid-game (`line_read_seconds`). Read only in `RevealUnit.LINE`.
    read_base_seconds: float = 0.5
    read_per_char_seconds: float = 0.035
    read_min_seconds: float = 2.0
    read_max_seconds: float = 5.0
    score_hold_seconds: float = 0.8
    key_hold_seconds: float = 1.2
    period_hold_seconds: float = 6.0

    def __post_init__(self) -> None:
        """Refuse a setting the planner or renderer would misuse rather than
        reject: `possessions_per_frame < 1` crashes `range()` in the planner,
        a zero window or threshold plans a meaningless momentum, and a line
        cap under `_MIN_LINE_CHARS` cannot even hold the clock and score
        prefix. The last check is what makes a live component "bounded by
        construction" (design §4.6, watch-v4 T-1.a) now that `chunk_budget_units`
        is gone: twice the ticker's own worst case (every character astral,
        the same margin the old chunk packer used) plus its separators must
        still fit inside one embed description, so a preset that cannot show
        its own ticker is rejected at configuration time rather than raising
        mid-broadcast out of `Panel.__post_init__`."""
        for name, floor in _SETTING_FLOORS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                raise ValueError(
                    f"PlanSettings.{name} must be an integer >= {floor}, got {value!r}"
                )
        if not isinstance(self.narration, NarrationLevel):
            raise ValueError(
                f"PlanSettings.narration must be a NarrationLevel, got {self.narration!r}"
            )
        if not isinstance(self.reveal, RevealUnit):
            raise ValueError(f"PlanSettings.reveal must be a RevealUnit, got {self.reveal!r}")
        for name in _READING_SECONDS:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"PlanSettings.{name} must be a finite number >= 0, got {value!r}")
        # A reveal with no hold would edit Discord back to back (WN-10.4's 2.0 s floor is
        # what keeps a reading mode under the per-message bucket), and a max under the
        # min makes the clamp meaningless.
        if not self.read_min_seconds > 0 or self.read_max_seconds < self.read_min_seconds:
            raise ValueError(
                f"PlanSettings: read_min_seconds must be > 0 and <= read_max_seconds, got "
                f"{self.read_min_seconds!r} and {self.read_max_seconds!r}"
            )
        lines = ticker_lines(self)
        mark = len(_NEWEST_MARK) if self.reveal is RevealUnit.LINE else 0
        worst_ticker = (
            2 * lines * (self.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE)
            + (lines - 1) * len(_LINE_SEPARATOR)
            + mark
        )
        if worst_ticker > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"PlanSettings: a {lines}-line ticker of {self.max_pbp_line_chars} character "
                f"lines plus markup needs up to {worst_ticker} characters, over the "
                f"{MAX_DESCRIPTION_CHARS} an embed description may hold"
            )


# A module-level singleton for the parameter default: ruff's B008 forbids
# `settings: PlanSettings = PlanSettings()`, and the class is frozen so
# sharing one instance is safe.
DEFAULT_PLAN_SETTINGS = PlanSettings()

# The three `/watch` speeds' plans (WN-10.4). The two reading modes reveal one line per
# edit, so a line is its own row and needs no name cut to share a row: their caps are the
# widest the ticker inequality allows for their window (a 240-character line at 8 lines,
# a 190 one at 10), which took TERSE lines needing an ellipsis from 11.8% at 96
# characters to 0.03% (seeds 0-59) and FULL lines to 0 -- the builder's answer to the
# truncation the turnover-recovery critic measured, not in the design's own table.
# `fast` was FAST_FORWARD's 96 exactly until the user's call after the critic (WN-17):
# at 96 it cut names to two letters in 11.49% of its lines ("Fe... scores at the rim").
# At 240 it cuts 4 of 10,402 (seeds 0-49), inside the same inequality (a 4,046-unit
# worst ticker at 8 lines), and its golden and pager text moved once.
FAST_PLAN_SETTINGS = PlanSettings(possessions_per_frame=4, max_pbp_line_chars=240)
GAME_PLAN_SETTINGS = PlanSettings(reveal=RevealUnit.LINE, max_pbp_line_chars=240)
FULL_PLAN_SETTINGS = PlanSettings(
    reveal=RevealUnit.LINE,
    narration=NarrationLevel.FULL,
    max_pbp_line_chars=190,
    ticker_window_lines=10,
)


@dataclass(frozen=True)
class MomentumReading:
    home_points: int
    away_points: int
    margin: int  # home_points - away_points
    label: str  # "LAL 12-4 run" | "even"


class FrameKind(StrEnum):
    TIPOFF = "tipoff"
    PLAY = "play"
    PERIOD_END = "period_end"
    FINAL = "final"


@dataclass(frozen=True)
class BroadcastFrame:
    """One complete state snapshot -- never a delta -- so a pacer that skips
    a frame can never desynchronise the scoreboard (design P5-97.i).

    `home_score`/`away_score` are ALWAYS a post-possession score: the last
    possession in the frame's `score_after`, never a pre-possession one.
    That single choice makes "a made basket with an unchanged score"
    structurally impossible. `clock_display` is the END clock of the
    frame's last possession (design §2.1), so score and clock describe one
    instant; TIPOFF alone shows the period's full length.

    `span` is the inclusive possession range and is `None` on every
    non-`PLAY` frame (design B5): TIPOFF precedes every possession,
    PERIOD_END and FINAL summarise possessions the PLAY frames already
    covered, and giving them a span would double-count or invent an empty
    range that invariant I1 would then special-case.

    A frame carries no rendered text (design §4.1's `lines` field is
    deliberately absent): a line is a human sentence, `render_pbp_line` is
    the only place one is made (P5-97.g), and it needs player names the
    name-free planner is never given. `_play_lines` renders the lines from the
    plan's possessions instead, so there is one call site and no field that
    could disagree with what is rendered."""

    index: int
    kind: FrameKind
    period: int
    clock_display: str
    home_score: int
    away_score: int
    momentum: MomentumReading
    span: tuple[int, int] | None


@dataclass(frozen=True)
class BroadcastPlan:
    game_id: int
    home_team_id: int
    away_team_id: int
    frames: tuple[BroadcastFrame, ...]
    total_possessions: int
    settings: PlanSettings
    # Kept so the renderers can draw a frame's lines and tally without
    # re-segmenting the beat log on every frame. Ids, numbers and the
    # possession's `_PlayEvent`s only -- never a beat.
    possessions: tuple[_Possession, ...]


def _format_clock(seconds: float) -> str:
    """`M:SS`, rounded UP to the whole second so `0:00` means exactly 0.0
    -- the period is over -- and never "0.4 seconds left"."""
    if seconds < 0.0:
        raise ValueError(f"a period clock cannot be negative: {seconds}")
    whole = math.ceil(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


_EVEN = MomentumReading(home_points=0, away_points=0, margin=0, label="even")


def _momentum(
    possessions: Sequence[_Possession],
    last_index: int,
    *,
    home_code: str,
    away_code: str,
    settings: PlanSettings,
) -> MomentumReading:
    """P5-97.f: points each side scored over the trailing
    `momentum_window_possessions` possessions ending at `last_index`
    (fewer early in the game; the window is game-wide, so it may reach back
    across a period boundary)."""
    first = max(0, last_index + 1 - settings.momentum_window_possessions)
    window = possessions[first : last_index + 1]
    home_points = sum(p.points for p in window if p.offense_is_home)
    away_points = sum(p.points for p in window if not p.offense_is_home)
    margin = home_points - away_points
    if abs(margin) < settings.momentum_margin_threshold:
        label = "even"
    else:
        leader_code = home_code if margin > 0 else away_code
        label = f"{leader_code} {max(home_points, away_points)}-{min(home_points, away_points)} run"
    return MomentumReading(
        home_points=home_points, away_points=away_points, margin=margin, label=label
    )


def plan_broadcast(
    result: GameResult,
    *,
    game_id: int,
    home_code: str,
    away_code: str,
    settings: PlanSettings = DEFAULT_PLAN_SETTINGS,
) -> BroadcastPlan:
    """Design §4.4: a `TIPOFF` frame, then per period its possessions in
    `PLAY` frames of `possessions_per_frame` (a frame never spans a period
    boundary, so a period's last one may be short) closed by a `PERIOD_END`
    frame, then a `FINAL` frame. The segmentation inputs come out of the
    result itself (§4.2 B4): `player_team` from the box lines,
    `home_team_id` from the result.

    `home_code`/`away_code` are extra to the design's signature, which says
    the result alone is enough (B4). It is not: `MomentumReading.label`
    names a team by code, and a `GameResult` carries team ids only.

    An empty beat log plans no frames. `FINAL` is the one frame whose truth
    comes from the result rather than the log; invariant I2 proves the two
    agree."""
    possessions = segment_possessions(
        result.beat_log,
        player_team={line.player_id: line.team_id for line in result.player_box_lines},
        home_team_id=result.home_team_id,
    )
    home_code, away_code = _code_text(home_code), _code_text(away_code)

    def momentum_at(last_index: int) -> MomentumReading:
        return _momentum(
            possessions, last_index, home_code=home_code, away_code=away_code, settings=settings
        )

    frames: list[BroadcastFrame] = []
    if possessions:
        frames.append(
            BroadcastFrame(
                index=0,
                kind=FrameKind.TIPOFF,
                period=1,  # no period 0 anywhere in the engine (game.py:253)
                clock_display=_format_clock(possessions[0].clock_start_seconds),
                home_score=0,
                away_score=0,
                momentum=_EVEN,
                span=None,
            )
        )
        for period, run in groupby(possessions, key=lambda p: p.period):
            in_period = tuple(run)
            for start in range(0, len(in_period), settings.possessions_per_frame):
                batch = in_period[start : start + settings.possessions_per_frame]
                last = batch[-1]
                frames.append(
                    BroadcastFrame(
                        index=len(frames),
                        kind=FrameKind.PLAY,
                        period=period,
                        clock_display=_format_clock(last.clock_end_seconds),
                        home_score=last.score_after[0],
                        away_score=last.score_after[1],
                        momentum=momentum_at(last.index),
                        span=(batch[0].index, last.index),
                    )
                )
            closing = in_period[-1]
            frames.append(
                BroadcastFrame(
                    index=len(frames),
                    kind=FrameKind.PERIOD_END,
                    period=period,
                    clock_display=_format_clock(closing.clock_end_seconds),
                    home_score=closing.score_after[0],
                    away_score=closing.score_after[1],
                    momentum=momentum_at(closing.index),
                    span=None,
                )
            )
        final = possessions[-1]
        frames.append(
            BroadcastFrame(
                index=len(frames),
                kind=FrameKind.FINAL,
                period=final.period,
                clock_display=_format_clock(final.clock_end_seconds),
                home_score=result.home_score,
                away_score=result.away_score,
                momentum=momentum_at(final.index),
                span=None,
            )
        )

    return BroadcastPlan(
        game_id=game_id,
        home_team_id=result.home_team_id,
        away_team_id=result.away_team_id,
        frames=tuple(frames),
        total_possessions=len(possessions),
        settings=settings,
        possessions=possessions,
    )


# --- rendering (design §4.5, §4.6, P5-97.d/.g/.h) --------------------------

_REGULATION_PERIODS = 4  # game.py:258 -- periods above this are overtime


def _period_label(period: int) -> str:
    """`Q1`..`Q4`, then `OT1`, `OT2`... (design §4.4)."""
    if period <= _REGULATION_PERIODS:
        return f"Q{period}"
    return f"OT{period - _REGULATION_PERIODS}"


def surnames(players: Mapping[int, Player]) -> dict[int, str]:
    """The `names` mapping the renderers take: surname only (design
    P5-97.d -- no jersey numbers exist, and a full name would crowd the
    96-character line). Built by the caller from the `Player` rows it already
    resolved, so this package does no lookup of its own (P5-5)."""
    return {player_id: player.last_name for player_id, player in players.items()}


# Characters that end or reshape a line of monospace text: control characters
# (a newline is one), format characters (a right-to-left override reorders the
# whole line on screen; a zero-width joiner hides), and the Unicode line and
# paragraph separators. `str.split()` also treats most of these as whitespace.
_LINE_SHAPING_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _fence_safe(text: str) -> str:
    """Make a database string safe to print inside the code fence a frame's
    description and fields sit in. Player names and team codes are database
    strings, and a fence is a line-oriented place: a backtick would close it
    (swapped for an apostrophe); a newline, carriage return or U+2028 would add
    a line -- and a line that starts `Q4 0:31  ` is a forged play, past the
    row-count bound -- and a bidi override would reorder the line
    it is in. Those characters become spaces, and runs of whitespace collapse
    to one, so a name is always exactly one printable, single-line token."""
    swapped = "".join(
        " " if unicodedata.category(ch) in _LINE_SHAPING_CATEGORIES else ("'" if ch == "`" else ch)
        for ch in text
    )
    return " ".join(swapped.split())


def _display_name(names: Mapping[int, str], player_id: int | None, cap: int) -> str:
    """`Player <id>` for an id nobody resolved -- "absent is neutral", the
    convention `boxscore.py` keeps."""
    if player_id is None:
        return "Unknown"
    raw = _fence_safe(names.get(player_id) or "") or f"Player {player_id}"
    return raw if len(raw) <= cap else raw[: cap - 1] + "…"


# Everything Discord's Markdown reads inside an embed description or field value
# (watch-v2 W-6): the backslash itself, emphasis, spoilers, quotes, code, masked
# links, headings, emoji shortcodes, mentions and autolinks. A backslash before
# any non-alphanumeric is consumed by Discord, so escaping a `:` or `<` is clean.
_MARKDOWN_SPECIALS = frozenset("\\*_~|>`[]()#:@<")


def _markdown_safe(text: str) -> str:
    """Escape a database string for an embed DESCRIPTION or FIELD VALUE, where
    Markdown is live. Not for embed titles, field names or the author line:
    those do not render Markdown, and escaping there would print a literal
    backslash. Fenced blocks use `_fence_safe` instead (Markdown is off inside
    one)."""
    return "".join("\\" + ch if ch in _MARKDOWN_SPECIALS else ch for ch in text)


def _md_name(names: Mapping[int, str], player_id: int | None, cap: int) -> str:
    return _markdown_safe(_display_name(names, player_id, cap))


# `teams.code` is unbounded TEXT, and a code is the one database string that sits
# INSIDE the bold running score (`**CODE a-b**`) and in fixed-width rows. A real
# code is three letters; the cap keeps a long or hostile one from widening a row,
# overflowing a title, or pushing the line-length ladder's cut into the bold pair.
_CODE_CHARS = 6


def _code_text(code: str) -> str:
    """A team code made single-line (`_fence_safe`) and capped at `_CODE_CHARS`.
    Every place a team code is printed goes through this, so the cap is one rule."""
    return _fence_safe(code)[:_CODE_CHARS].rstrip()


def _md_code(code: str) -> str:
    return _markdown_safe(_code_text(code))


# The leaf -> phrase map, total over the eleven censused `resolution` leaves
# (design P5-97.d; BDA-4 added the tenth, `blocked_miss_help`;
# turnover-recovery.md TR-4.15(b) the eleventh, `loose_ball_recovered`); `{S}` is the
# shooter, `{by}` is " by <defender>" (empty in the terse form), `{N}` the
# possession's point delta. This is the FALLBACK grammar (watch-v2): a
# possession whose last beat carries its terminal payload (E-1) is narrated
# from its events, which know the zone, the assist and the free-throw count.
# The leaf grammar reads only the beat log's `resolution` outcomes, so it says
# none of those and runs only for a hand-built beat log with no payload (and
# for a possession whose events yield no sentence).
_LEAF_PHRASES: dict[str, str] = {
    "made": "{S} scores +{N}",
    "and_one": "{S} scores +{N}, fouled{by}",
    "missed_unblocked": "{S} misses",
    "blocked_miss": "{S} blocked{by}",
    # BDA-4 (docs/design/block-defender-attribution.md): a weak-side
    # rim-help block -- same phrase text as `blocked_miss` (both are "shot
    # attempt, blocked, by someone"). `_build_possession` special-cases
    # this leaf's `defender_id` to the real blocker
    # (`TerminalOutcome.blocking_defender_id`), not the beat's own
    # `actor_ids["defender"]` (the shot's primary contester, a different
    # player on this leaf specifically) -- see that function's own comment.
    "blocked_miss_help": "{S} blocked{by}",
    "shooting_foul": "{S} fouled{by}",
    "non_shooting_foul": "{S} fouled{by}",
    "turnover_live": "{S} turnover, stolen{by}",
    "turnover_dead": "{S} turnover",
    "offensive_foul": "Offensive foul, {S}",
    # TR-4.15(b): the leaf grammar cannot name the recoverer, so it does not
    # pretend to. The wording is load-bearing: it must not contain
    # "turnover", "rebound", "stolen", a "+N" or any other claim
    # `test_broadcast_ground_truth._contradictions` holds against the
    # engine's record -- on the scramble close (TR-4.14) the recovery is the
    # possession's last NAMED event, so even there "turnover" is forbidden.
    "loose_ball_recovered": "{S} loses it, offense recovers",
}
# Leaves whose sentence already carries the possession's +N. A foul leaf
# carries it through `_foul_clause` only when the delta is provably the foul's
# own; every other line gets a trailing `(+N)` when the possession scored, so
# a line NEVER hides its own delta and never pins it on a foul that did not
# earn it.
_LEAVES_SHOWING_DELTA = frozenset({"made", "and_one"})
_FOUL_LEAVES = frozenset({"shooting_foul", "non_shooting_foul"})


def _foul_clause(possession: _Possession, *, drew_rebound: bool) -> str | None:
    """What a foul line says about its own free throws, or `None` when the
    beat log cannot say. A scoreless possession settles every case: nothing
    scored, so this foul's free throws scored nothing, and a non-shooting foul
    with no rebound after it drew no trip at all (a bonus trip that missed its
    last shot draws one). A scoring possession is attributed to the foul only
    when `_foul_points_attributable` proves it; otherwise the line names the
    foul and lets the trailing `(+N)` carry the possession's delta."""
    if possession.points == 0:
        if possession.terminal_leaf == "non_shooting_foul" and not drew_rebound:
            return "no free throws"  # the non-bonus close-out: nobody shot
        return "no points"
    if possession.foul_points_attributable:
        return f"scores +{possession.points}"
    return None


def _narrate_leaf(
    possession: _Possession,
    *,
    names: Mapping[int, str],
    cap: int,
    terse: bool,
    offense_code: str,
) -> str:
    """The leaf grammar. `terse` drops the defender and the rebounder by name --
    the last resort before a line is cut, because the players a sentence can
    lose are the secondary ones, never the shooter, the delta or the real
    ending."""
    ending = possession.ending
    if ending is _Ending.PRE_RESOLUTION_VIOLATION:
        return f"Shot clock violation, {offense_code}"
    leaf = possession.terminal_leaf
    if leaf is None:
        # `_classify_ending` reports every leafless possession as
        # PRE_RESOLUTION_VIOLATION, handled above; a value that disagrees is a
        # malformed `_Possession` (an `assert` here would vanish under `python -O`).
        raise ValueError(f"possession {possession.index}: ending {ending.value!r} but no leaf")
    shooter = _md_name(names, possession.shooter_id, cap)
    by = "" if terse else f" by {_md_name(names, possession.defender_id, cap)}"
    unknown = "{S} — " + _markdown_safe(leaf).replace("{", "{{").replace("}", "}}")
    template = _LEAF_PHRASES.get(leaf, unknown)
    parts = [template.format(S=shooter, by=by, N=possession.points)]
    drew_rebound = possession.tail_rebound_is_offensive is not None
    delta_shown = leaf in _LEAVES_SHOWING_DELTA
    if leaf in _FOUL_LEAVES:
        clause = _foul_clause(possession, drew_rebound=drew_rebound)
        if clause is not None:
            parts.append(clause)
            delta_shown = possession.points > 0
    if drew_rebound:
        boarder = "" if terse else _md_name(names, possession.tail_rebounder_id, cap) + " "
        parts.append(
            f"{boarder}offensive rebound"
            if possession.tail_rebound_is_offensive
            else f"{boarder}rebound"
        )
    if ending is _Ending.RESTART_VIOLATION:
        parts.append("then shot clock violation")
    text = ", ".join(parts)
    if possession.points > 0 and not delta_shown:
        text += f" (+{possession.points})"
    return text


# --- play lines (watch-v2 W-5) -----------------------------------------------

# One table, one code point each (no keycap sequences), so a glyph is the same
# width everywhere and a test can count it. A line takes the glyph of its most
# consequential event (`_GLYPH_PRIORITY`).
_GLYPHS = {
    "make3": "🎯",
    "make2": "🏀",
    "miss": "❌",
    "block": "🔒",
    "foul": "🛑",
    "steal": "⚡",  # live-ball turnover
    "dead": "🔻",  # dead-ball turnover, offensive foul, shot-clock violation
    "loose": "🔄",  # the offense won its own loose ball (TR-4.15(d))
}
_NEUTRAL_GLYPH = "•"  # a leaf the table has no entry for: never a failure
# `loose` is last: a recovery is never a possession's most consequential
# event (a later terminal is always on the same line), so it shows only when
# nothing else on the line has a glyph -- in practice never, since even the
# scramble close ends on an attributed `dead` turnover. It is listed so
# `_event_glyph_key` never returns a key this loop silently ignores.
_GLYPH_PRIORITY = ("make3", "make2", "steal", "foul", "dead", "block", "miss", "loose")
# The fallback grammar's leaves. A `made` leaf cannot say its zone, so it reads
# as a two-point make.
_LEAF_GLYPH_KEYS = {
    "made": "make2",
    "and_one": "make2",
    "missed_unblocked": "miss",
    "blocked_miss": "block",
    "blocked_miss_help": "block",
    "shooting_foul": "foul",
    "non_shooting_foul": "foul",
    "turnover_live": "steal",
    "turnover_dead": "dead",
    "offensive_foul": "dead",
    "loose_ball_recovered": "loose",  # never "steal" (the opposite) or "dead"
}

_SHOT_MADE = {
    Zone.RIM: "scores at the rim",
    Zone.MID: "hits a mid-range shot",
    Zone.THREE: "hits a three",
}
_SHOT_MISSED = {
    Zone.RIM: "misses at the rim",
    Zone.MID: "misses a mid-range shot",
    Zone.THREE: "misses a three",
}
_SHOT_BLOCKED = {Zone.RIM: "shot at the rim", Zone.MID: "mid-range shot", Zone.THREE: "three"}
_FOUL_KINDS = (TerminalKind.SHOOTING_FOUL, TerminalKind.NON_SHOOTING_FOUL)


def _event_glyph_key(event: _PlayEvent) -> str | None:
    if event.kind is TerminalKind.MADE_FG:
        return "make3" if event.zone is Zone.THREE else "make2"
    if event.kind is TerminalKind.MISSED_FG:
        return "block" if event.blocked else "miss"
    if event.kind in _FOUL_KINDS:
        return "foul"
    if event.kind is TerminalKind.TURNOVER_LIVE:
        return "steal"
    if event.kind is TerminalKind.OFFENSIVE_FOUL or (
        event.kind is TerminalKind.TURNOVER_DEAD and event.turnover_player_id is not None
    ):
        return "dead"
    if event.kind is TerminalKind.LOOSE_BALL_RECOVERED:
        return "loose"
    return None


def _line_glyph(possession: _Possession) -> str:
    if possession.ending is _Ending.PRE_RESOLUTION_VIOLATION:
        return _GLYPHS["dead"]
    keys = {key for event in possession.events if (key := _event_glyph_key(event)) is not None}
    for key in _GLYPH_PRIORITY:
        if key in keys:
            return _GLYPHS[key]
    return _GLYPHS.get(_LEAF_GLYPH_KEYS.get(possession.terminal_leaf or "", ""), _NEUTRAL_GLYPH)


def _free_throws(event: _PlayEvent, *, nm: Callable[[int | None], str], terse: bool) -> str | None:
    if event.ft_count <= 0:
        return None
    who = (
        ""
        if terse
        else nm(event.fouled_player_id if event.fouled_player_id is not None else event.scorer_id)
        + " "
    )
    return f"{who}{event.ftm} of {event.ft_count} at the line"


def _rebound_clause(
    event: _PlayEvent, *, nm: Callable[[int | None], str], terse: bool
) -> str | None:
    if event.rebound_is_offensive is None:
        return None
    named = event.rebounder_id is not None and not terse
    who = nm(event.rebounder_id) + " " if named else ""
    if event.rebound_is_offensive:
        return f"{who}offensive rebound"
    return f"{who}rebounds" if named else "rebound"


def _event_parts(
    event: _PlayEvent, *, nm: Callable[[int | None], str], terse: bool, restart_close: bool
) -> list[str]:
    """The clauses one event adds to a line, in the order they happened. An
    event that owes the reader nothing returns `[]`: a `TURNOVER_DEAD` with no
    ball handler is the engine's fabricated budget-exhausted close, which
    "represents no additional real-world fact" (`possession.py:428-434`) and is
    NEVER narrated. `restart_close` marks a terminal that is a shot-clock
    violation after a continuation restarted: a real, attributed turnover."""
    kind = event.kind
    parts: list[str] = []
    if kind is TerminalKind.MADE_FG:
        text = f"{nm(event.scorer_id)} {_SHOT_MADE.get(event.zone, 'scores')}"
        if event.assisted and event.passer_id is not None and not terse:
            text += f" ({nm(event.passer_id)} assist)"
        if event.and_one:
            text += ", and-one"
        parts.append(text)
    elif kind is TerminalKind.MISSED_FG:
        shooter = nm(event.scorer_id)
        if event.blocked:
            text = f"{shooter}'s {_SHOT_BLOCKED.get(event.zone, 'shot')} blocked"
            if not terse and event.blocking_defender_id is not None:
                text += f" by {nm(event.blocking_defender_id)}"
        else:
            text = f"{shooter} {_SHOT_MISSED.get(event.zone, 'misses')}"
        parts.append(text)
    elif kind in _FOUL_KINDS:
        if terse or event.fouler_id is None:
            parts.append(f"{nm(event.fouled_player_id)} fouled")
        else:
            parts.append(f"{nm(event.fouler_id)} fouls {nm(event.fouled_player_id)}")
        if kind is TerminalKind.NON_SHOOTING_FOUL and event.ft_count <= 0:
            parts.append("no free throws")
    elif kind is TerminalKind.TURNOVER_LIVE:
        text = f"{nm(event.turnover_player_id)} turnover"
        if event.steal:
            text += ", stolen"
            if not terse and event.stealing_defender_id is not None:
                text += f" by {nm(event.stealing_defender_id)}"
        parts.append(text)
    elif kind is TerminalKind.TURNOVER_DEAD:
        if event.turnover_player_id is not None:
            text = f"{nm(event.turnover_player_id)} turnover"
            parts.append(f"then shot clock violation, {text}" if restart_close else text)
    elif kind is TerminalKind.OFFENSIVE_FOUL:
        parts.append(f"Offensive foul, {nm(event.offensive_fouler_id)}")
    elif kind is TerminalKind.LOOSE_BALL_RECOVERED:
        # docs/design/turnover-recovery.md TR-4.15(a): narrated, but it is
        # not a turnover and produces no box-score line, so the clause must
        # make no statistical claim -- TR-4.15(c)'s forbidden list, enforced
        # by test_broadcast_ground_truth's clause-level test. `terse` sheds
        # the loser (the secondary player), never the recoverer; the loser
        # may be the recoverer himself (TR-4.7).
        recoverer = nm(event.recovering_player_id)
        if terse:
            parts.append(f"{recoverer} recovers the loose ball")
        elif event.recovering_player_id == event.loose_ball_player_id:
            parts.append(f"{recoverer} bobbles it, recovers")
        else:
            parts.append(f"{nm(event.loose_ball_player_id)} loses it, {recoverer} recovers")
    if kind in (TerminalKind.MADE_FG, *_FOUL_KINDS):
        free_throws = _free_throws(event, nm=nm, terse=terse)
        if free_throws is not None:
            parts.append(free_throws)
    board = _rebound_clause(event, nm=nm, terse=terse)
    if board is not None:
        parts.append(board)
    return parts


def _narrate_events(
    possession: _Possession,
    *,
    names: Mapping[int, str],
    cap: int,
    terse: bool,
    offense_code: str,
) -> str:
    """The possession as its events tell it, chronologically: credit events
    first, then the terminal (`possession.events` is in box-score order, terminal
    first, so this reads it the other way round). `_Possession.ending` still
    gates the line exactly as the leaf grammar's does: a pre-resolution shot-clock
    violation says so and nothing else, and a restart violation says "then shot
    clock violation". An empty string means the events owed the reader no
    sentence, and the caller falls back to the leaf grammar."""
    ending = possession.ending
    if ending is _Ending.PRE_RESOLUTION_VIOLATION:
        return f"Shot clock violation, {offense_code}"

    def nm(player_id: int | None) -> str:
        return _md_name(names, player_id, cap)

    chronological = (*possession.events[1:], possession.events[0])
    parts: list[str] = []
    violation_said = False
    for position, event in enumerate(chronological):
        restart_close = ending is _Ending.RESTART_VIOLATION and position == len(chronological) - 1
        clauses = _event_parts(event, nm=nm, terse=terse, restart_close=restart_close)
        violation_said = violation_said or any("shot clock violation" in c for c in clauses)
        parts.extend(clauses)
    if ending is _Ending.RESTART_VIOLATION and not violation_said:
        parts.append("then shot clock violation")
    return ", ".join(parts)


def _sentence(
    possession: _Possession,
    *,
    names: Mapping[int, str],
    cap: int,
    terse: bool,
    offense_code: str,
) -> str:
    if possession.events:
        text = _narrate_events(
            possession, names=names, cap=cap, terse=terse, offense_code=offense_code
        )
        if text:
            return text
    return _narrate_leaf(possession, names=names, cap=cap, terse=terse, offense_code=offense_code)


# Detail is shed before a line is cut: a cut lands on the END of the line,
# which is exactly where the possession's real ending lives. Names shrink first,
# then the secondary players are dropped by name.
_NAME_CAPS = (16, 14, 12, 10, 8, 6, 5, 3)
_DETAIL_TIERS = tuple((cap, False) for cap in _NAME_CAPS) + ((3, True),)
_KEY_PLAY_MARK = "  ★"


def _cut(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters ending in an ellipsis, without leaving
    the half of an escape pair behind (a dangling backslash would escape the
    ellipsis)."""
    body = text[: limit - 1]
    trailing = len(body) - len(body.rstrip("\\"))
    if trailing % 2:
        body = body[:-1]
    return body + "…"


def render_pbp_line(
    possession: _Possession,
    *,
    names: Mapping[int, str],
    home_code: str,
    away_code: str,
    settings: PlanSettings,
    key_play: bool = False,
) -> str:
    """The P5-11 prose seam (design P5-97.g): the ONLY function that turns a
    possession into a line, taking a value object and returning a `str`, with
    exactly one call site (`_play_lines`) that a unit test pins.

    A line reads `clock  glyph [**CODE a-b**  ]sentence[  ★]`: the possession's
    END clock (design §2.1), a glyph for its most consequential event, the
    running score from the scoring team's side when the possession scored, and
    a sentence built from its events (`_narrate_events`), or from the leaf
    grammar when the last beat carried no payload. The period is not repeated on
    the line: the message it sits in is that period's.

    Every database string is Markdown-escaped (W-6). The sentence is at most
    `settings.max_pbp_line_chars` and the markup adds at most
    `_LINE_MARKUP_ALLOWANCE`: names shrink first, then the secondary players
    are dropped by name, and only if the line still does not fit is it cut with
    an ellipsis (the score and the key-play star are never cut: a team code is
    capped at `_CODE_CHARS`, and the cut lands in the sentence, never the head)."""
    home_code, away_code = _md_code(home_code), _md_code(away_code)
    offense_code = home_code if possession.offense_is_home else away_code
    head = _terminal_head(possession, home_code=home_code, away_code=away_code)
    star = _KEY_PLAY_MARK if key_play else ""
    limit = settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE - len(star)
    # `_fit_line` cuts only the sentence: the head carries the bold score, and a cut
    # through it would leave an unbalanced `**` live in the description. `_code_text`
    # keeps the head well inside `limit` at every settings floor a real code can meet.
    return _fit_line(
        head,
        lambda cap, terse: _sentence(
            possession, names=names, cap=cap, terse=terse, offense_code=offense_code
        ),
        limit=limit,
        star=star,
    )


def _terminal_head(possession: _Possession, *, home_code: str, away_code: str) -> str:
    """`clock  glyph [**CODE a-b**  ]`: the head of a possession's line (codes already
    Markdown-escaped). The possession's END clock (design §2.1), the glyph of its most
    consequential event, and the running score from the scoring team's side when the
    possession scored."""
    offense_code = home_code if possession.offense_is_home else away_code
    home_after, away_after = possession.score_after
    offense_after, defense_after = (
        (home_after, away_after) if possession.offense_is_home else (away_after, home_after)
    )
    score = f"**{offense_code} {offense_after}-{defense_after}**  " if possession.points > 0 else ""
    return f"{_format_clock(possession.clock_end_seconds)}  {_line_glyph(possession)} {score}"


def _own_glyph(possession: _Possession, event: _PlayEvent | None) -> str:
    """The glyph of the ONE event a FULL line narrates (narration v2's own-event head,
    WN-17): its `_event_glyph_key`, or -- for a line the leaf grammar tells, which has no
    event with a glyph -- the terminal leaf's, exactly as `_line_glyph` falls back. A
    pre-resolution shot-clock violation is always `dead`."""
    if possession.ending is _Ending.PRE_RESOLUTION_VIOLATION:
        return _GLYPHS["dead"]
    key = _event_glyph_key(event) if event is not None else None
    if key is not None:
        return _GLYPHS[key]
    return _GLYPHS.get(_LEAF_GLYPH_KEYS.get(possession.terminal_leaf or "", ""), _NEUTRAL_GLYPH)


def _own_head(
    possession: _Possession,
    *,
    clock: float,
    glyph: str,
    points: int,
    offense_after: int,
    home_code: str,
    away_code: str,
) -> str:
    """`clock  glyph [**CODE a-b**  ]` for one FULL line: the bold running score only
    when THIS line put points on the board, from the scoring team's side, as it stands
    after this line (`offense_after`). TERSE keeps `_terminal_head`, the whole
    possession's head, byte for byte (A5)."""
    offense_code = home_code if possession.offense_is_home else away_code
    home_after, away_after = possession.score_after
    defense_after = away_after if possession.offense_is_home else home_after
    score = f"**{offense_code} {offense_after}-{defense_after}**  " if points > 0 else ""
    return f"{_format_clock(clock)}  {glyph} {score}"


# --- key plays and the game's story, both computed from the plan ---------------


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _is_three_or_and_one(possession: _Possession) -> bool:
    return any(
        event.kind is TerminalKind.MADE_FG and (event.zone is Zone.THREE or event.and_one)
        for event in possession.events
    )


_RUN_STAR_POINTS = 8
_CLUTCH_SECONDS = 60.0
_ONE_POSSESSION_MARGIN = 3


def _key_plays(possessions: Sequence[_Possession]) -> frozenset[int]:
    """Indexes of the possessions a line marks with a star: a lead change, a
    tying score, a three or and-one that completes a run of 8-0 or more, or a
    score in the last minute of a one-possession game (the fourth period or
    later)."""
    starred: set[int] = set()
    run_owner: bool | None = None
    run_points = 0
    for possession in possessions:
        if possession.points <= 0:
            continue
        before = possession.score_before[0] - possession.score_before[1]
        after = possession.score_after[0] - possession.score_after[1]
        before_run = run_points if run_owner is possession.offense_is_home else 0
        run_owner = possession.offense_is_home
        run_points = before_run + possession.points
        crossed = before_run < _RUN_STAR_POINTS <= run_points
        if (
            (_sign(before) * _sign(after) == -1)
            or (after == 0 and before != 0)
            or (crossed and _is_three_or_and_one(possession))
            or (
                possession.period >= _REGULATION_PERIODS
                and possession.clock_end_seconds <= _CLUTCH_SECONDS
                and min(abs(before), abs(after)) <= _ONE_POSSESSION_MARGIN
            )
        ):
            starred.add(possession.index)
    return frozenset(starred)


@dataclass(frozen=True)
class _Story:
    lead_changes: int
    largest_home_lead: int
    largest_away_lead: int
    longest_run: tuple[bool, int] | None  # (owner is home, unanswered points)
    late_period: int
    late_score: tuple[int, int]  # (home, away) with five minutes left in the last period


def _game_story(possessions: Sequence[_Possession]) -> _Story:
    lead_changes = 0
    largest_home = largest_away = 0
    longest: tuple[bool, int] | None = None
    run_owner: bool | None = None
    run_points = 0
    for possession in possessions:
        before = possession.score_before[0] - possession.score_before[1]
        after = possession.score_after[0] - possession.score_after[1]
        if _sign(before) * _sign(after) == -1:
            lead_changes += 1
        largest_home = max(largest_home, after)
        largest_away = max(largest_away, -after)
        if possession.points > 0:
            continued = run_owner is possession.offense_is_home
            run_points = run_points + possession.points if continued else possession.points
            run_owner = possession.offense_is_home
            if longest is None or run_points > longest[1]:
                longest = (run_owner, run_points)
    last_period = possessions[-1].period
    in_last = [p for p in possessions if p.period == last_period]
    before_mark = [p for p in in_last if p.clock_end_seconds >= 300.0]
    late = before_mark[-1].score_after if before_mark else in_last[0].score_before
    return _Story(lead_changes, largest_home, largest_away, longest, last_period, late)


# --- lines, periods and the live component (watch-v4 T-1..T-3) -----------------


def _utf16_units(text: str) -> int:
    """Discord counts a message in UTF-16 code units: an astral character (the
    basketball, the lock) is two, a BMP one is one. Counted for real, never
    assumed. `surrogatepass` so a lone surrogate in a database string is
    measured rather than raised on. Kept for `render_pager_page`'s budget test
    even though the chunk packer that used to be its only production caller is
    gone (watch-v4 T-6)."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _play_lines(
    plan: BroadcastPlan, *, home: Team, away: Team, names: Mapping[int, str]
) -> list[str]:
    """Every possession's line, rendered once, in possession-index order.
    `render_pbp_line`'s only call site."""
    starred = _key_plays(plan.possessions)
    return [
        render_pbp_line(
            possession,
            names=names,
            home_code=home.code,
            away_code=away.code,
            settings=plan.settings,
            key_play=possession.index in starred,
        )
        for possession in plan.possessions
    ]


@dataclass(frozen=True)
class PeriodLines:
    """One period's rendered lines, in order, and the possession index the first
    one sits at. What `plan_period_lines` regroups `_play_lines` into for the
    ephemeral full-text pager (watch-v4 T-6) -- the direct successor of v2's
    `PeriodChunk`, minus the budget packing nothing needs any more."""

    period: int
    first_index: int
    lines: tuple[str, ...]

    @property
    def label(self) -> str:
        """`period` is always a real period here (`plan_period_lines` groups by
        `_Possession.period`, never 0), so `_period_label` is never called with
        0 (watch-v4 T-3)."""
        return _period_label(self.period)


def plan_period_lines(
    plan: BroadcastPlan, *, home: Team, away: Team, names: Mapping[int, str]
) -> tuple[PeriodLines, ...]:
    """Every possession's line, rendered once and regrouped by period -- the
    same `groupby` `plan_chunks` used to run, minus the packing. Concatenating
    every period's `lines` in order reproduces `_play_lines`' own output,
    because possessions are contiguous and increasing within a period and
    periods never interleave."""
    lines = _play_lines(plan, home=home, away=away, names=names)
    result: list[PeriodLines] = []
    for period, group in groupby(plan.possessions, key=lambda p: p.period):
        indexes = [possession.index for possession in group]
        result.append(
            PeriodLines(
                period=period, first_index=indexes[0], lines=tuple(lines[i] for i in indexes)
            )
        )
    return tuple(result)


# --- narration v2: the chain facts (docs/design/watch-narration-v2.md WN-1, WN-5, WN-6) ---
#
# Everything below reads the beat log of ONE possession -- the chain beats
# (`setup`/`screen`/`on_ball`/`help`/`rotation`) that today's one-line-per-possession
# feed never shows. It is pure, private and name-free: the phrase layer turns these
# facts into sentences. Nothing here is stored on a `_Possession` or a plan (WN-8: the
# plan pin is nearly spent), so these helpers run once per `/watch`, over the beat log
# the caller still holds, and the beats are dropped afterwards.

# The chain beats: what a pre-line may be built from (WN-1.3). `resolution` and
# `rebound` are narrated by the credit and terminal lines, from the events payload.
_CHAIN_BEATS = frozenset({"setup", "screen", "on_ball", "help", "rotation"})

# WN-4.3: why a continuation restarted, read off the beat immediately before its
# `setup`. The table is total over the shapes that can precede a restart (measured,
# Appendix B): a `contained` read, an offensive rebound, a non-bonus foul -- and, since
# turnover-recovery (TR-4.6; WN-13's fourth row), the offense winning back its own
# loose ball, which restarts at Setup like every other continuation.
_RESTART_REASONS: dict[tuple[str, str], str] = {
    ("on_ball", "contained"): "reset",
    ("rebound", _REBOUND_OUTCOME_OFFENSE): "offensive_board",
    ("resolution", "non_shooting_foul"): "foul",
    ("resolution", "loose_ball_recovered"): "scramble",
}

# WN-5.1's "defender fact": a read the defender WON, and which actor key of that
# beat names him. The resolution beat's `defender` is the same player by
# construction (`possession.py` passes `on_ball_defender` after `defender_wins`,
# `helper` after the contested kick-out and the tagged roll, `popper_defender`
# after the contested pop); `_defender_read` checks that rather than trusting it.
_DEFENDER_WON_READS: dict[tuple[str, str], str] = {
    ("on_ball", "defender_wins"): "on_ball_defender",
    ("rotation", "kickout_contested"): "closing_defender",
    ("rotation", "roll_tagged"): "tagger",
    ("rotation", "pop_contested"): "popper_defender",
}

_CORE_FIVE = 5


def _elapsed_before(beats: Sequence[BeatRecord]) -> tuple[float, ...]:
    """WN-6: the game seconds a possession had used before each of its beats.

    Every beat's `clock_seconds` is the SHOT clock read before that beat's own
    tick, and the only non-tick change to the shot clock is the offensive-rebound
    reset (`max(current, 14)`), which consumes no time. So the time between two
    consecutive beats is the shot clock's fall between them, and a RISE is that
    reset and contributes nothing. Monotone by construction; the tick after the
    last beat is not recorded, which is why a TERMINAL line keeps the possession's
    derived end clock instead."""
    elapsed = [0.0]
    total = 0.0
    for previous, following in zip(beats, beats[1:], strict=False):
        total += max(0.0, previous.clock_seconds - following.clock_seconds)
        elapsed.append(total)
    return tuple(elapsed)


def _game_clock_at(possession: _Possession, elapsed: float) -> float:
    """The period clock at a beat `elapsed` seconds into `possession`, clamped at
    0.0: a possession that starts in the last seconds of a period runs past the
    horn (`game.py` credits `min(elapsed, remaining)`), and its later beats read
    `0:00`, which is what the real clock showed."""
    return max(0.0, possession.clock_start_seconds - elapsed)


def _p_make(resolution: BeatRecord) -> float | None:
    """WN-1.1: the make probability the engine drew this shot against, exactly.
    `made` and `missed_unblocked` share the factor `p_shot*(1-p_block)*(1-p_help_block)`
    (`resolution.py`), so their ratio is `p_make` itself. `None` when a hand-built
    record carries neither leaf."""
    made = resolution.probabilities.get("made")
    missed = resolution.probabilities.get("missed_unblocked")
    if made is None or missed is None or made + missed <= 0.0:
        return None
    return made / (made + missed)


@dataclass(frozen=True, slots=True)
class _ChainSegment:
    """One pass through the possession chain: a `setup` beat and everything up to
    (not including) the next `setup`. `first` and `last` index the possession's
    beat list; a possession restarts once per extra segment (WN-4.3)."""

    first: int
    last: int

    def beats(self, beats: Sequence[BeatRecord]) -> Sequence[BeatRecord]:
        return beats[self.first : self.last + 1]


def _chain_segments(beats: Sequence[BeatRecord]) -> tuple[_ChainSegment, ...]:
    """Split a possession's beats at every `setup`. A possession always opens on
    one (`_group_by_possession` refuses a log that does not), so the first segment
    starts at 0."""
    starts = [position for position, beat in enumerate(beats) if beat.beat == "setup"]
    if not starts or starts[0] != 0:
        raise ValueError("a possession's beats must open on a setup beat")
    ends = [*(start - 1 for start in starts[1:]), len(beats) - 1]
    return tuple(_ChainSegment(first, last) for first, last in zip(starts, ends, strict=True))


def _restart_reason(previous: BeatRecord) -> str | None:
    """WN-4.3's prefix key for a restart whose `setup` follows `previous`, or
    `None` for a shape the table does not name (never guessed: an unnamed restart
    prints no reason rather than a wrong one, and the corpus test says so)."""
    return _RESTART_REASONS.get((previous.beat, previous.outcome))


@dataclass(frozen=True, slots=True)
class _DefenderRead:
    """A read the defense won inside a segment (WN-5.1): which one, and who won it."""

    beat: str
    outcome: str
    defender_id: int


def _defender_read(segment: Sequence[BeatRecord]) -> _DefenderRead | None:
    """The defender-won read of a segment whose resolution names that same
    defender as the contest (WN-5.2 rule A), or `None`. A segment holds at most one
    such read: `defender_wins` goes straight to the shot, and the contested
    rotations are the chain's last read. The equality check is the honesty guard:
    "stopped by Y" may only name the player the engine recorded as contesting the
    shot, so a read whose winner is not the resolution's `defender` (never the case
    in the engine today) credits nobody."""
    resolution = next((b for b in segment if b.beat == "resolution"), None)
    if resolution is None:
        return None
    for beat in segment:
        key = _DEFENDER_WON_READS.get((beat.beat, beat.outcome))
        if key is None:
            continue
        winner = beat.actor_ids.get(key)
        if winner is not None and winner == resolution.actor_ids.get("defender"):
            return _DefenderRead(beat=beat.beat, outcome=beat.outcome, defender_id=winner)
        return None
    return None


def _core_five(result: GameResult) -> frozenset[int]:
    """The five players per team with the most minutes (WN-1.2's "a known
    defender"). NOT the opening lineup (`game.py::_select_lineup` opens with the
    five furthest behind target), so a line never calls anyone a "starter". Used
    only to choose WHICH true sentence to print (WN-5.3), never to decide whether
    something happened. Ties break on the lower id so the set is replay-stable."""
    by_team: dict[int, list[tuple[float, int]]] = {}
    for line in result.player_box_lines:
        by_team.setdefault(line.team_id, []).append((-line.minutes_played, line.player_id))
    return frozenset(
        player_id for rows in by_team.values() for _minutes, player_id in sorted(rows)[:_CORE_FIVE]
    )


# --- narration v2: a replay-stable phrase selector (WN-7.2) ---------------------------

_MIX_SEED = 0x9E3779B97F4A7C15
_MIX_MULTIPLIER = 0xBF58476D1CE4E5B9
_MASK_64 = (1 << 64) - 1


def _variant(seed_parts: Sequence[int], count: int) -> int:
    """A splitmix-style integer mix of `seed_parts`, reduced to `range(count)`.
    Pure integer arithmetic: no `random`, no `hash()` (Python salts `str` hashes per
    process), no clock -- so the same game renders the same text on every `/watch`,
    in every process (A8)."""
    if count < 1:
        raise ValueError(f"cannot choose among {count} variants")
    mixed = _MIX_SEED
    for part in seed_parts:
        mixed ^= part & _MASK_64
        mixed = (mixed * _MIX_MULTIPLIER) & _MASK_64
        mixed ^= mixed >> 31
    return mixed % count


class _PhrasePicker:
    """One game's walk through the phrase library. Inputs are `(game_id, possession
    index, slot ordinal within the possession, the key's library ordinal)`; the
    anti-repeat rule (never the same variant of a key twice in a row) is state, and
    deterministic only because a game is always rendered in one pass, in possession
    order -- so a picker is built per render and never shared."""

    def __init__(self, game_id: int) -> None:
        self._game_id = game_id
        self._last: dict[tuple[str, str], int] = {}
        self._used: dict[tuple[tuple[str, str], tuple[int, ...]], set[int]] = {}

    def pick(
        self, key: tuple[str, str], *, possession: int, slot: int, actors: tuple[int, ...] = ()
    ) -> Phrase:
        """The drawn variant, stepped forward past the one this key used last and, when
        `actors` names the players the phrase will carry, past every variant already said
        about those same players this game (until all have been): the same matchup comes
        up dozens of times a game, and without this about one sentence in sixteen was an
        exact repeat of an earlier one (WN-17)."""
        phrases = LIBRARY[key]
        count = len(phrases)
        drawn = _variant((self._game_id, possession, slot, KEY_ORDINALS[key]), count)
        used = self._used.setdefault((key, actors), set()) if actors else set()
        if len(used) >= count:
            used.clear()
        last = self._last.get(key) if count > 1 else None
        candidates = [(drawn + step) % count for step in range(count)]
        index = next(
            (c for c in candidates if c not in used and c != last),
            next((c for c in candidates if c != last), drawn),
        )
        if actors:
            used.add(index)
        self._last[key] = index
        return phrases[index]


# --- narration v2: a possession as lines (WN-2, WN-4, WN-5) ---------------------------


class LineKind(StrEnum):
    """What a rendered line is. The first three are the CHAIN lines (WN-2.2's S, O
    and P slots), built from chain beats only and so never able to leak the
    possession's ending (WN-1.3)."""

    SETUP = "setup"
    ON_BALL = "on_ball"
    PASS = "pass"
    CREDIT = "credit"
    TERMINAL = "terminal"
    BOUNDARY = "boundary"  # WN-11: a period separator, not a play; no clock/glyph head


CHAIN_LINE_KINDS = frozenset({LineKind.SETUP, LineKind.ON_BALL, LineKind.PASS})

_CHAIN_GLYPH = "·"  # WN-2.1: a pre-line (or a scoreless credit line) is not a play result
_CLAUSE_JOIN = " — "
_TOUGH_MAKE_P = 0.40  # WN-5.3: the engine's own number calls a make "tough"

# WN-3.3: the only locations there are, and they are role names.
_ROLE_LOCATIONS = {
    "corner_shooter": " in the corner",
    "weak_side_shooter": " on the weak side",
    "trailer": " trailing",
    "wing_runner": " running the wing",
}

# The actor each S key's placeholders come from: (side, role) per placeholder.
_SETUP_ROLES: dict[str, dict[str, tuple[str, str]]] = {
    "pnr_ball": {
        "BH": ("offense", "ball_handler"),
        "SCR": ("offense", "screener"),
        "OBD": ("defense", "on_ball_def"),
    },
    "dho": {
        "BH": ("offense", "ball_handler"),
        "HB": ("offense", "handoff_big"),
        "OBD": ("defense", "on_ball_def"),
    },
    "iso": {"BH": ("offense", "ball_handler"), "OBD": ("defense", "on_ball_def")},
    "transition": {"BH": ("offense", "ball_handler"), "FB": ("defense", "first_back")},
    "attack": {"BH": ("offense", "ball_handler"), "FB": ("defense", "first_back")},
    "has_it": {"BH": ("offense", "ball_handler"), "OBD": ("defense", "on_ball_def")},
    "post_up": {
        "EP": ("offense", "entry_passer"),
        "PS": ("offense", "post_scorer"),
        "POSTD": ("defense", "post_def"),
    },
    "off_ball_screen": {
        "EP": ("offense", "entry_passer"),
        "CUT": ("offense", "cutter"),
        "SCR": ("offense", "screener"),
        "CD": ("defense", "cutter_def"),
    },
    "spot_up": {
        "PASSER": ("offense", "passer"),
        "SH": ("offense", "shooter"),
        "CL": ("defense", "closer"),
    },
}
# Who holds the ball once the S line is said: the handler, or the pass's target.
_SETUP_RECEIVER = {
    "pnr_ball": "BH",
    "dho": "BH",
    "iso": "BH",
    "transition": "BH",
    "attack": "BH",
    "has_it": "BH",
    "post_up": "PS",
    "off_ball_screen": "CUT",
    "spot_up": "SH",
}
_SCREEN_ACTIONS = frozenset({"pnr_ball", "dho"})

# The P line's placeholders, by rotation actor key.
_PASS_ACTORS = {
    "PASSER": "passer",
    "VS": "vacated_shooter",
    "CD": "closing_defender",
    "SCR": "screener",
    "TAG": "tagger",
    "PD": "popper_defender",
}
_PASS_RECEIVER = {
    "kickout_open": "vacated_shooter",
    "kickout_contested": "vacated_shooter",
    "roll_live": "screener",
    "roll_tagged": "screener",
    "pop_open": "screener",
    "pop_contested": "screener",
}
# WN-5.2's miss clause, by the read the defender won.
_STOP_CLAUSES = {
    ("on_ball", "defender_wins"): "stopped",
    ("rotation", "kickout_contested"): "closed_out",
    ("rotation", "pop_contested"): "closed_out",
    ("rotation", "roll_tagged"): "tagged",
}


@dataclass(frozen=True, slots=True)
class Line:
    """One rendered line of the feed (WN-2.1). `points` is what the line itself put on
    the board -- 0 for a CHAIN line, a credit's own points, the terminal event's own
    points -- so a possession's lines sum to its `points` and the running header
    score can move exactly when a scoring line is revealed (WN-9.3). `clock_seconds`
    is the period clock at the line's beat (WN-6); a TERMINAL line uses the
    possession's derived end clock. `sentence_chars` is the length of the text
    without its clock/glyph/score head or key-play star: what a reader reads
    (WN-10.2)."""

    kind: LineKind
    possession: int
    clock_seconds: float
    text: str
    points: int
    sentence_chars: int


@dataclass(frozen=True)
class Narration:
    """A whole game rendered once as lines (WN-8's option (d)): held beside the plan,
    never in it, so the plan's byte pin is untouched. `terminal_at[p]` is the flat
    index of possession `p`'s TERMINAL line -- the line after which the possession is
    over."""

    level: NarrationLevel
    lines: tuple[Line, ...]
    terminal_at: tuple[int, ...]

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines)


def _event_points(event: _PlayEvent) -> int:
    """`game._points_for`, over the fields `_PlayEvent` carries (WN-9.3)."""
    points = event.ftm
    if event.kind is TerminalKind.MADE_FG:
        points += 3 if event.zone is Zone.THREE else 2
    return points


def _with_clause(text: str, clause: str) -> str:
    """Put a WN-5 defender clause right after a shot's own phrase: before an assist
    `(... assist)` or an `, and-one`, else at the end. A name's own parentheses are
    Markdown-escaped (`\\(`), so the only ` (` in the text is the assist's."""
    for marker in (" (", ", and-one"):
        at = text.find(marker)
        if at != -1:
            return text[:at] + clause + text[at:]
    return text + clause


# A pre-line's ladder: full wording at full names, then every picked phrase swapped
# for its key's SHORTEST variant (same facts, fewer words) while the names shrink.
# Only then is the sentence cut. Keeps a busy restart line ("Off the offensive board
# -- X now has it, ...") from reaching three-letter names first.
_CHAIN_TIERS = ((_NAME_CAPS[0], False), *((cap, True) for cap in _NAME_CAPS))


def _fit_line(
    head: str,
    sentence_for: Callable[[int, bool], str],
    *,
    limit: int,
    star: str = "",
    tiers: Sequence[tuple[int, bool]] = _DETAIL_TIERS,
) -> str:
    """The line-length ladder every rendered line shares (watch-v2 W-7): names shrink
    first, then the secondary players are dropped by name (or, for a pre-line, the
    shorter wording is used), and only then is the sentence -- never the head -- cut
    with an ellipsis."""
    sentence = ""
    for cap, terse in tiers:
        sentence = sentence_for(cap, terse)
        if len(head) + len(sentence) <= limit:
            return head + sentence + star
    room = limit - len(head)
    if room < 1:
        return head.rstrip() + star
    return head + _cut(sentence, room) + star


@dataclass(frozen=True, slots=True)
class _Clause:
    """A picked template and the players (with any role-location suffix) its
    placeholders stand for. Rendered at a name cap, so the ladder can shrink names
    without re-picking a variant."""

    template: str
    values: tuple[tuple[str, int, str], ...] = ()
    short: str | None = None  # the key's shortest variant, same placeholders

    def render(self, names: Mapping[int, str], cap: int, *, short: bool = False) -> str:
        template = self.short if short and self.short is not None else self.template
        return template.format(
            **{key: _md_name(names, pid, cap) + suffix for key, pid, suffix in self.values}
        )


class _Narrator:
    """One game's FULL rendering (WN-2.2): walks each possession's beats once, in
    order, picking phrases through one `_PhrasePicker` (whose anti-repeat state is
    why a narrator is built per render and never shared)."""

    def __init__(
        self,
        plan: BroadcastPlan,
        result: GameResult,
        *,
        home: Team,
        away: Team,
        names: Mapping[int, str],
    ) -> None:
        self._picker = _PhrasePicker(plan.game_id)
        self._core_five = _core_five(result)
        self._names = names
        self._home_code, self._away_code = _md_code(home.code), _md_code(away.code)
        self._limit = plan.settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE
        self._starred = _key_plays(plan.possessions)
        self._slot = 0
        self._side: tuple[int, bool] | None = None  # (period, offense_is_home) last narrated

    # -- one possession -----------------------------------------------------------

    def possession_lines(
        self, possession: _Possession, beats: Sequence[BeatRecord]
    ) -> list[tuple[Line, tuple[int, ...]]]:
        """The possession's lines, each with the indexes (into `beats`) of the beats
        it was built from -- the tests' handle on WN-12 T1. The last line is always
        the TERMINAL."""
        self._slot = 0
        elapsed = _elapsed_before(beats)
        events = possession.events
        credits = events[1:]
        terminal_parts_empty = bool(events) and not _event_parts(
            events[0],
            nm=lambda pid: "",
            terse=False,
            restart_close=possession.ending is _Ending.RESTART_VIOLATION,
        )
        promoted = credits[-1] if terminal_parts_empty and credits else None
        resolutions = [i for i, b in enumerate(beats) if b.beat == "resolution"]
        lines: list[tuple[Line, tuple[int, ...]]] = []
        holder: int | None = None
        home_after, away_after = possession.score_after
        offense_after = home_after if possession.offense_is_home else away_after
        offense_score = offense_after - possession.points  # before the possession
        segments = _chain_segments(beats)
        # WN-17: a light team marker opens the first line when the ball changes sides (or
        # a new period starts), so a reader joining mid-feed knows whose possession it is.
        side = (possession.period, possession.offense_is_home)
        marker = ""
        if side != self._side:
            code = self._home_code if possession.offense_is_home else self._away_code
            marker = f"{code} ball, "
        self._side = side
        for number, segment in enumerate(segments):
            previous = beats[segment.first - 1] if number else None
            for built, after in self._segment_lines(
                possession,
                beats,
                segment,
                previous=previous,
                holder=holder,
                elapsed=elapsed,
                marker=marker if number == 0 else "",
            ):
                lines.append(built)
                holder = after
            for position in range(segment.first, segment.last + 1):
                if position not in resolutions:
                    continue
                k = resolutions.index(position)
                if k >= len(credits) or credits[k] is promoted:
                    continue
                credit = credits[k]
                sources = (position,) + (
                    (position + 1,)
                    if position + 1 < len(beats) and beats[position + 1].beat == "rebound"
                    else ()
                )
                clock = _game_clock_at(possession, elapsed[position])
                line = self._credit_line(possession, credit, clock, offense_score=offense_score)
                offense_score += line.points
                lines.append((line, sources))
                holder = _holder_after_credit(credit, holder)
        terminal = self._terminal_line(
            possession, beats, segments[-1], resolutions=resolutions, promoted=promoted
        )
        lines.append((terminal, tuple(range(len(beats)))))
        return lines

    # -- chain lines ----------------------------------------------------------------

    def _pick(
        self, key: tuple[str, str], possession: _Possession, actors: tuple[int, ...] = ()
    ) -> tuple[str, str]:
        """The picked variant's template and the key's shortest one (the ladder's
        fallback wording, `_CHAIN_TIERS`). `actors` are the players the phrase will name,
        so the picker can avoid repeating itself about them."""
        self._slot += 1
        phrase = self._picker.pick(key, possession=possession.index, slot=self._slot, actors=actors)
        shortest = min(LIBRARY[key], key=lambda candidate: len(candidate.template))
        return phrase.template, shortest.template

    def _chain_line(
        self,
        kind: LineKind,
        possession: _Possession,
        clauses: Sequence[_Clause | str],
        clock: float,
    ) -> Line:
        head = f"{_format_clock(clock)}  {_CHAIN_GLYPH} "

        def sentence_for(cap: int, short: bool) -> str:
            return "".join(
                part if isinstance(part, str) else part.render(self._names, cap, short=short)
                for part in clauses
            )

        text = _fit_line(head, sentence_for, limit=self._limit, tiers=_CHAIN_TIERS)
        return Line(kind, possession.index, clock, text, 0, max(0, len(text) - len(head)))

    def _segment_lines(
        self,
        possession: _Possession,
        beats: Sequence[BeatRecord],
        segment: _ChainSegment,
        *,
        previous: BeatRecord | None,
        holder: int | None,
        elapsed: Sequence[float],
        marker: str = "",
    ):
        """Yield `((line, sources), holder_after)` for the S, O and P lines of one
        segment. A slot whose actors a beat does not name (a hand-built log) yields
        nothing rather than a line with a hole in it."""
        first = segment.first
        setup = beats[first]
        offense = {
            k.split(":", 1)[1]: v for k, v in setup.actor_ids.items() if k.startswith("offense:")
        }
        defense = {
            k.split(":", 1)[1]: v for k, v in setup.actor_ids.items() if k.startswith("defense:")
        }
        position = first + 1
        screen = (
            beats[position]
            if position <= segment.last and beats[position].beat == "screen"
            else None
        )
        if screen is not None:
            position += 1

        action = setup.outcome
        key = action
        if action == "transition" and previous is not None:
            key = "attack"
        elif action in _SCREEN_ACTIONS and screen is None:
            key = "has_it"
        roles = _SETUP_ROLES.get(key)
        values: dict[str, int] | None = None
        if roles is not None:
            values = {}
            for placeholder, (side, role) in roles.items():
                pid = (offense if side == "offense" else defense).get(role)
                if pid is None:
                    values = None
                    break
                values[placeholder] = pid
        if values is not None:
            clauses: list[_Clause | str] = [marker] if marker else []
            reason = _restart_reason(previous) if previous is not None else None
            if reason is not None:
                prefix, prefix_short = self._pick(("R", reason), possession)
                clauses.append(_Clause(prefix, (), prefix_short))
            template, short = self._pick(("S", key), possession, tuple(values.values()))
            start_holder = values[SETUP_HOLDER[key]]
            # WN-4.3 as amended by WN-17: "X now has it" only when it tells the reader
            # something the S line's own subject does not. After a credit line (a board, a
            # foul, a scramble) the reader was just told who had the ball, so a different
            # player starting the action is news; after a reset the offense is moving the
            # ball around by definition, and the S line opening on X already says it.
            changed = previous is not None and holder is not None and start_holder != holder
            if changed and reason != "reset":
                now, now_short = self._pick(("R", "now_has_it"), possession, (start_holder,))
                clauses.append(_Clause(now, (("X", start_holder, ""),), now_short))
                subject = "{" + SETUP_HOLDER[key] + "} "
                template, short = template.removeprefix(subject), short.removeprefix(subject)
            clauses.append(_Clause(template, tuple((k, v, "") for k, v in values.items()), short))
            sources: tuple[int, ...] = (first,)
            if screen is not None and key in _SCREEN_ACTIONS:
                coverage = self._coverage(possession, screen, handler=values["BH"])
                if coverage is not None:
                    clauses.extend((_CLAUSE_JOIN, coverage))
                    sources = (first, first + 1)
            clock = _game_clock_at(possession, elapsed[first])
            line = self._chain_line(LineKind.SETUP, possession, clauses, clock)
            holder = values[_SETUP_RECEIVER[key]]
            yield (line, sources), holder

        on_ball = (
            beats[position]
            if position <= segment.last and beats[position].beat == "on_ball"
            else None
        )
        if on_ball is None:
            return
        handler = on_ball.actor_ids.get("ball_handler")
        defender = on_ball.actor_ids.get("on_ball_defender")
        if handler is None or defender is None or ("O", on_ball.outcome) not in LIBRARY:
            return
        o_position = position
        position += 1
        help_beat = (
            beats[position] if position <= segment.last and beats[position].beat == "help" else None
        )
        rotation = None
        if help_beat is not None:
            position += 1
            if position <= segment.last and beats[position].beat == "rotation":
                rotation = beats[position]
        read, read_short = self._pick(("O", on_ball.outcome), possession, (handler, defender))
        o_clauses: list[_Clause | str] = [
            _Clause(read, (("BH", handler, ""), ("OBD", defender, "")), read_short)
        ]
        o_sources = [o_position]
        help_clause = None
        helper = help_beat.actor_ids.get("helper") if help_beat is not None else None
        if help_beat is not None and helper is not None:
            if help_beat.outcome == "help_stayed":
                stayed, stayed_short = self._pick(("O+", "help_stayed"), possession, (helper,))
                o_clauses.append(_Clause(stayed, (("HELPER", helper, ""),), stayed_short))
                o_sources.append(o_position + 1)
            elif ("H", help_beat.outcome) in LIBRARY:
                came, came_short = self._pick(("H", help_beat.outcome), possession, (helper,))
                help_clause = _Clause(came, (("HELPER", helper, ""),), came_short)
        passed = rotation is not None and rotation.outcome != "no_pass"
        if help_clause is not None and not passed:
            o_clauses.extend(("; ", help_clause))
            o_sources.append(o_position + 1)
            if rotation is not None:  # no_pass: he keeps it
                kept, kept_short = self._pick(("O+", "keeps_it"), possession, (handler,))
                o_clauses.append(_Clause(kept, (("BH", handler, ""),), kept_short))
                o_sources.append(o_position + 2)
        clock = _game_clock_at(possession, elapsed[o_position])
        line = self._chain_line(LineKind.ON_BALL, possession, o_clauses, clock)
        holder = handler
        yield (line, tuple(o_sources)), holder

        if not passed or rotation is None:
            return
        pass_key = ("P", rotation.outcome)
        if pass_key not in LIBRARY:
            return
        needed = PLACEHOLDERS[pass_key]
        p_values: list[tuple[str, int, str]] = []
        for placeholder in sorted(needed):
            pid = rotation.actor_ids.get(_PASS_ACTORS[placeholder])
            if pid is None:
                return
            suffix = _role_location(offense, pid) if placeholder == "VS" else ""
            p_values.append((placeholder, pid, suffix))
        template, short = self._pick(pass_key, possession, tuple(pid for _, pid, _ in p_values))
        p_clauses: list[_Clause | str] = []
        p_sources = [position]
        if help_clause is not None:
            p_clauses.extend((help_clause, "; "))
            p_sources.insert(0, position - 1)
        p_clauses.append(_Clause(template, tuple(p_values), short))
        clock = _game_clock_at(possession, elapsed[p_sources[0]])
        line = self._chain_line(LineKind.PASS, possession, p_clauses, clock)
        holder = rotation.actor_ids.get(_PASS_RECEIVER[rotation.outcome], holder)
        yield (line, tuple(p_sources)), holder

    def _coverage(
        self, possession: _Possession, screen: BeatRecord, *, handler: int
    ) -> _Clause | None:
        key = ("C", screen.outcome)
        if key not in LIBRARY:
            return None
        actors = {
            "SD": screen.actor_ids.get("screen_defender"),
            "SCR": screen.actor_ids.get("screener"),
            "BH": handler,
        }
        values = []
        for placeholder in sorted(PLACEHOLDERS[key]):
            pid = actors.get(placeholder)
            if pid is None:
                return None
            values.append((placeholder, pid, ""))
        template, short = self._pick(key, possession, tuple(pid for _, pid, _ in values))
        return _Clause(template, tuple(values), short)

    # -- credit and terminal lines ------------------------------------------------------

    def _credit_line(
        self, possession: _Possession, credit: _PlayEvent, clock: float, *, offense_score: int
    ) -> Line:
        """A credit event's own line. One that put points on the board (an and-one whose
        free throw was rebounded, a scoring foul trip) carries its own glyph and the
        running score after it, like any scoring line; the rest keep the pre-line dot."""
        points = _event_points(credit)
        head = f"{_format_clock(clock)}  {_CHAIN_GLYPH} "
        if points > 0:
            head = _own_head(
                possession,
                clock=clock,
                glyph=_own_glyph(possession, credit),
                points=points,
                offense_after=offense_score + points,
                home_code=self._home_code,
                away_code=self._away_code,
            )

        def sentence_for(cap: int, terse: bool) -> str:
            return ", ".join(
                _event_parts(
                    credit,
                    nm=lambda pid: _md_name(self._names, pid, cap),
                    terse=terse,
                    restart_close=False,
                )
            )

        text = _fit_line(head, sentence_for, limit=self._limit)
        return Line(
            LineKind.CREDIT, possession.index, clock, text, points, max(0, len(text) - len(head))
        )

    def _spotlight(
        self,
        possession: _Possession,
        event: _PlayEvent,
        beats: Sequence[BeatRecord],
        segment: _ChainSegment,
        resolutions: Sequence[int],
    ) -> _Clause | None:
        """WN-5: "over {D}" / "a tough one over {D}" on a make, "{D} stopped him" and
        its siblings on an unblocked miss -- only on the terminal's OWN resolution,
        in the final segment, and only from recorded defender facts."""
        if len(resolutions) != len(possession.events) or not resolutions:
            return None  # the terminal has no resolution of its own (a forced close)
        last = resolutions[-1]
        if last < segment.first:
            return None
        resolution = beats[last]
        seg_beats = segment.beats(beats)
        read = _defender_read(seg_beats)
        key: str | None = None
        defender: int | None = None
        if event.kind is TerminalKind.MISSED_FG and not event.blocked and read is not None:
            key, defender = _STOP_CLAUSES[(read.beat, read.outcome)], read.defender_id
        elif event.kind is TerminalKind.MADE_FG and read is not None:
            key, defender = "over", read.defender_id
        elif event.kind is TerminalKind.MADE_FG and all(
            b.beat in ("setup", "resolution", "rebound") for b in seg_beats
        ):
            p_make = _p_make(resolution)
            contester = resolution.actor_ids.get("defender")
            if p_make is not None and p_make < _TOUGH_MAKE_P and contester in self._core_five:
                key, defender = "tough_over", contester
        if key is None or defender is None:
            return None
        template, _short = self._pick(("T", key), possession, (defender,))
        return _Clause(template, (("D", defender, ""),))

    def _terminal_line(
        self,
        possession: _Possession,
        beats: Sequence[BeatRecord],
        segment: _ChainSegment,
        *,
        resolutions: Sequence[int],
        promoted: _PlayEvent | None,
    ) -> Line:
        home_code, away_code = self._home_code, self._away_code
        offense_code = home_code if possession.offense_is_home else away_code
        star = _KEY_PLAY_MARK if possession.index in self._starred else ""
        ending = possession.ending
        event = promoted or (possession.events[0] if possession.events else None)
        if ending is _Ending.PRE_RESOLUTION_VIOLATION or event is None:
            points = possession.points if event is None else 0
        else:
            points = _event_points(event)
        home_after, away_after = possession.score_after
        # The head is the terminal event's OWN (WN-17): the possession's credits have
        # their own lines, so a miss after an and-one is a miss, not a basket.
        head = _own_head(
            possession,
            clock=possession.clock_end_seconds,
            glyph=_own_glyph(possession, event),
            points=points,
            offense_after=home_after if possession.offense_is_home else away_after,
            home_code=home_code,
            away_code=away_code,
        )
        clause = (
            self._spotlight(possession, event, beats, segment, resolutions)
            if event is not None
            and promoted is None
            and ending is not _Ending.PRE_RESOLUTION_VIOLATION
            else None
        )
        names = self._names

        def sentence_for(cap: int, terse: bool) -> str:
            if ending is _Ending.PRE_RESOLUTION_VIOLATION:
                return f"Shot clock violation, {offense_code}"
            if event is not None:
                parts = _event_parts(
                    event,
                    nm=lambda pid: _md_name(names, pid, cap),
                    terse=terse,
                    restart_close=ending is _Ending.RESTART_VIOLATION and promoted is None,
                )
                if parts:
                    # Its credits are lines of their own, so "then ..." has nothing to
                    # follow on this line: it opens the sentence instead (WN-17).
                    parts[0] = _capitalize_violation(parts[0])
                    if ending is _Ending.RESTART_VIOLATION and not any(
                        "shot clock violation" in part for part in parts
                    ):
                        parts.append("then shot clock violation")
                    if clause is not None and not terse:
                        parts[0] = _with_clause(parts[0], clause.render(names, cap))
                    return ", ".join(parts)
            return _narrate_leaf(
                possession, names=names, cap=cap, terse=terse, offense_code=offense_code
            )

        limit = self._limit - len(star)
        text = _fit_line(head, sentence_for, limit=limit, star=star)
        chars = max(0, len(text) - len(head) - len(star))
        return Line(
            LineKind.TERMINAL, possession.index, possession.clock_end_seconds, text, points, chars
        )


_THEN_VIOLATION = "then shot clock violation"


def _capitalize_violation(clause: str) -> str:
    """A FULL terminal's opening clause, made to stand alone: TERSE's "then shot clock
    violation, X turnover" follows the credits on the same line, which FULL has already
    printed on their own lines."""
    if clause.startswith(_THEN_VIOLATION):
        return "Shot clock violation" + clause.removeprefix(_THEN_VIOLATION)
    return clause


def _holder_after_credit(credit: _PlayEvent, holder: int | None) -> int | None:
    """Who the reader was last told has the ball after a credit event: the offensive
    rebounder, the loose ball's recoverer, or the player fouled with it."""
    if credit.rebound_is_offensive and credit.rebounder_id is not None:
        return credit.rebounder_id
    if credit.kind is TerminalKind.LOOSE_BALL_RECOVERED and credit.recovering_player_id is not None:
        return credit.recovering_player_id
    if credit.kind is TerminalKind.NON_SHOOTING_FOUL and credit.fouled_player_id is not None:
        return credit.fouled_player_id
    return holder


def _role_location(offense: Mapping[str, int], player_id: int) -> str:
    for role, suffix in _ROLE_LOCATIONS.items():
        if offense.get(role) == player_id:
            return suffix
    return ""


def plan_narration(
    result: GameResult,
    plan: BroadcastPlan,
    *,
    home: Team,
    away: Team,
    names: Mapping[int, str],
) -> Narration:
    """Render the whole game once, at `plan.settings.narration` (WN-8 decision (d)):
    the caller holds the result in memory at `/watch`, so the beat log is re-walked
    here and dropped -- the plan still keeps no beats (N7) and its byte pin does not
    move. `TERSE` is `_play_lines` itself, one TERMINAL line per possession (A5).
    In the reading modes (`RevealUnit.LINE`) one BOUNDARY line sits between every two
    periods (WN-11); `fast` gets none, which is what keeps its golden still.

    `result` must be the game `plan` was planned from: the beat log is re-segmented
    and must yield exactly the plan's possessions."""
    level = plan.settings.narration
    per_possession: list[list[Line]]
    if level is NarrationLevel.TERSE:
        home_code, away_code = _md_code(home.code), _md_code(away.code)
        starred = _key_plays(plan.possessions)
        per_possession = []
        for possession, text in zip(
            plan.possessions, _play_lines(plan, home=home, away=away, names=names), strict=True
        ):
            head = _terminal_head(possession, home_code=home_code, away_code=away_code)
            star = _KEY_PLAY_MARK if possession.index in starred else ""
            per_possession.append(
                [
                    Line(
                        LineKind.TERMINAL,
                        possession.index,
                        possession.clock_end_seconds,
                        text,
                        possession.points,
                        max(0, len(text) - len(head) - len(star)),
                    )
                ]
            )
    else:
        groups = _group_by_possession(result.beat_log) if result.beat_log else []
        if len(groups) != len(plan.possessions):
            raise ValueError(
                f"game {plan.game_id}: the beat log has {len(groups)} possessions but the "
                f"plan has {len(plan.possessions)} -- not the result this plan was made from"
            )
        narrator = _Narrator(plan, result, home=home, away=away, names=names)
        per_possession = [
            [line for line, _sources in narrator.possession_lines(possession, beats)]
            for possession, beats in zip(plan.possessions, groups, strict=True)
        ]

    separators = (
        _period_separators(plan, home=home, away=away)
        if plan.settings.reveal is RevealUnit.LINE
        else {}
    )
    lines: list[Line] = []
    terminal_at: list[int] = []
    for possession, own in zip(plan.possessions, per_possession, strict=True):
        lines.extend(own)
        terminal_at.append(len(lines) - 1)
        separator = separators.get(possession.index)
        if separator is not None:
            lines.append(separator)
    return Narration(level, tuple(lines), tuple(terminal_at))


# --- period separators (WN-11) -------------------------------------------------------

_RULE = "━" * 10  # U+2501: one BMP code point, a continuous bar on every Discord client


def _separator_text(
    period: int, *, home_score: int, away_score: int, home_code: str, away_code: str
) -> str:
    """WN-11.4's table: the period that ended and the score at that moment, away first
    as `_score_title` prints it, so the separator agrees with the embed title."""
    score = f"{away_code} {away_score} - {home_score} {home_code}"
    if period == 2:
        head = f"Halftime · {score}"
    elif period == _REGULATION_PERIODS:
        head = f"End of regulation · {score} · Overtime"
    elif period > _REGULATION_PERIODS:
        head = f"End of {_period_label(period)} · {score} · {_period_label(period + 1)}"
    else:
        head = f"End of {_period_label(period)} · {score}"
    return f"{_RULE}  {head}  {_RULE}"


def _period_separators(plan: BroadcastPlan, *, home: Team, away: Team) -> dict[int, Line]:
    """One BOUNDARY line per boundary between two periods that both have possessions,
    keyed by the ended period's last possession (the line goes right after its
    TERMINAL). The score is the PERIOD_END frame's, the period's closing score by
    construction (invariant I3). Never before the first period and never after the
    last: the FINAL status says the game is over."""
    home_code, away_code = _md_code(home.code), _md_code(away.code)
    last_period = plan.possessions[-1].period if plan.possessions else 0
    ends = _period_ends(plan)
    separators: dict[int, Line] = {}
    for frame in plan.frames:
        if frame.kind is not FrameKind.PERIOD_END or frame.period >= last_period:
            continue
        text = _separator_text(
            frame.period,
            home_score=frame.home_score,
            away_score=frame.away_score,
            home_code=home_code,
            away_code=away_code,
        )
        closing = ends[frame.period]
        separators[closing] = Line(LineKind.BOUNDARY, closing, 0.0, text, 0, len(text))
    return separators


def frame_cursor(frame: BroadcastFrame, plan: BroadcastPlan, narration: Narration) -> int:
    """The flat line index a frame reveals through, `-1` for TIPOFF: the TERMINAL line
    of its newest possession, or -- for a PERIOD_END frame in a reading mode -- the
    period's BOUNDARY line right after it (WN-11.4: the separator is revealed by the
    PERIOD_END frame's own edit)."""
    if frame.kind is FrameKind.TIPOFF:
        return -1
    cursor = narration.terminal_at[last_line_index(frame, plan)]
    following = cursor + 1
    if (
        frame.kind is FrameKind.PERIOD_END
        and following < len(narration.lines)
        and narration.lines[following].kind is LineKind.BOUNDARY
    ):
        return following
    return cursor


def completed_through(narration: Narration, cursor: int) -> int:
    """The newest possession whose TERMINAL line is at or before `cursor` (-1 if
    none): what the stats slice, the line score, the cycle row and the pager's spoiler
    cap read, so none of them counts a possession whose ending is not on screen."""
    if cursor < 0:
        return -1
    line = narration.lines[cursor]
    if line.kind in (LineKind.TERMINAL, LineKind.BOUNDARY):
        return line.possession
    return line.possession - 1


def header_score(plan: BroadcastPlan, narration: Narration, cursor: int) -> tuple[int, int]:
    """WN-9.3: `(home, away)` as of the newest revealed line -- the possession's entry
    score plus the points of its lines revealed so far. A scoring line and its score
    change therefore land in the same edit, and the header never shows a value outside
    `[score_before, score_after]` of the possession on screen."""
    if cursor < 0:
        return (0, 0)
    owner = narration.lines[cursor].possession
    revealed = 0
    position = cursor
    while position >= 0 and narration.lines[position].possession == owner:
        revealed += narration.lines[position].points
        position -= 1
    possession = plan.possessions[owner]
    home_score, away_score = possession.score_before
    if possession.offense_is_home:
        return (home_score + revealed, away_score)
    return (home_score, away_score + revealed)


# --- pacing for a human reader (WN-10) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reveal:
    """One paced edit of the live component: the frame whose status it shows, the flat
    narration line it reveals through, and when it is due, in seconds after the
    broadcast starts. Pure data -- `bot/broadcast.py` owns the clock that honours it."""

    frame_index: int
    cursor: int
    due_seconds: float


def line_read_seconds(line: Line, settings: PlanSettings, *, key_play: bool = False) -> float:
    """WN-10.2's reading-time model: how long a line stays the newest before the next
    one arrives. `read_base + read_per_char * len(sentence)` clamped to
    `[read_min, read_max]`, plus `score_hold` when the line put points on the board and
    `key_hold` on a starred key play; a period separator holds `period_hold` (WN-11.4).
    The sentence is the text without its clock/glyph/score head -- what is read."""
    if line.kind is LineKind.BOUNDARY:
        return settings.period_hold_seconds
    seconds = settings.read_base_seconds + settings.read_per_char_seconds * line.sentence_chars
    seconds = min(settings.read_max_seconds, max(settings.read_min_seconds, seconds))
    if line.points > 0:
        seconds += settings.score_hold_seconds
    if key_play:
        seconds += settings.key_hold_seconds
    return seconds


def reveal_schedule(
    plan: BroadcastPlan, narration: Narration, *, tick_seconds: float
) -> tuple[Reveal, ...]:
    """Every reveal of a broadcast, in order, with its cumulative deadline (WN-10.2).

    `RevealUnit.FRAME` (`fast`): one reveal per frame at `index * tick_seconds`, the
    frame's own cursor -- today's pacing exactly. `RevealUnit.LINE` (the reading
    modes): TIPOFF (held `read_min`), then one reveal per line of each PLAY frame's
    possessions, each due once the line before it has had its reading time; a
    PERIOD_END frame reveals its separator (held `period_hold`) or, after the last
    period, re-shows the final line for `read_min`; FINAL is the last reveal. The plan
    is untouched: frames still exist, the pacer sub-steps lines inside them."""
    if plan.settings.reveal is RevealUnit.FRAME:
        return tuple(
            Reveal(frame.index, frame_cursor(frame, plan, narration), frame.index * tick_seconds)
            for frame in plan.frames
        )
    settings = plan.settings
    starred = _key_plays(plan.possessions)
    reveals: list[Reveal] = []
    due = 0.0
    shown = -1
    for frame in plan.frames:
        if frame.kind is FrameKind.TIPOFF:
            reveals.append(Reveal(frame.index, -1, due))
            due += settings.read_min_seconds
        elif frame.kind is FrameKind.PLAY:
            through = frame_cursor(frame, plan, narration)
            for cursor in range(shown + 1, through + 1):
                line = narration.lines[cursor]
                reveals.append(Reveal(frame.index, cursor, due))
                key_play = line.kind is LineKind.TERMINAL and line.possession in starred
                due += line_read_seconds(line, settings, key_play=key_play)
            shown = max(shown, through)
        elif frame.kind is FrameKind.PERIOD_END:
            cursor = frame_cursor(frame, plan, narration)
            reveals.append(Reveal(frame.index, cursor, due))
            if cursor > shown:
                due += line_read_seconds(narration.lines[cursor], settings)
                shown = cursor
            else:
                due += settings.read_min_seconds
        else:  # FINAL
            reveals.append(Reveal(frame.index, frame_cursor(frame, plan, narration), due))
    return tuple(reveals)


def _period_ends(plan: BroadcastPlan) -> dict[int, int]:
    return {possession.period: possession.index for possession in plan.possessions}


def _completed_periods(plan: BroadcastPlan, last_line: int) -> int:
    """The highest period whose last possession is at or before `last_line`
    (0 if none). Keyed on PLAN POSITION, never on frame kind, so a pacer that
    jumped over a PERIOD_END frame still shows exactly the periods that are
    over, and never a period's row before its final possession (W-3)."""
    done = 0
    for period, end in sorted(_period_ends(plan).items()):
        if last_line < end:
            break
        done = period
    return done


def last_line_index(frame: BroadcastFrame, plan: BroadcastPlan) -> int:
    """Index of the newest possession a frame's line window may show; -1 when
    it shows none. A PERIOD_END frame summarises possessions the PLAY frames
    already covered, so its window ends at its period's last possession.
    Public (watch-v4 T-5.c): the pacer computes this ONCE per tick and hands it
    to the ticker, the stats slice, the line score and `visible_periods` --
    the click path never computes one of its own, it reads `state.line_index`."""
    if frame.kind is FrameKind.TIPOFF:
        return -1
    if frame.kind is FrameKind.PLAY:
        if frame.span is None:  # design B5: a PLAY frame always carries a span
            raise ValueError(f"PLAY frame {frame.index} has no span")
        return frame.span[1]
    if frame.kind is FrameKind.FINAL:
        return plan.total_possessions - 1
    return max(p.index for p in plan.possessions if p.period == frame.period)


def visible_periods(plan: BroadcastPlan, last_line: int) -> tuple[int, ...]:
    """The sorted, distinct periods with at least one possession at or before
    `last_line` (watch-v4 T-2.a) -- what the cycle row may show. Keyed on PLAN
    POSITION like `_completed_periods`, so a pacer that jumped a PERIOD_END
    frame still shows exactly the periods that have begun, and empty at the
    TIPOFF frame (`last_line == -1`): nothing is revealed the status line does
    not already say. Non-decreasing over the pacer's loop, so a button already
    on screen never disappears."""
    return tuple(sorted({p.period for p in plan.possessions if p.index <= last_line}))


def _line_score_value(
    period_scores: Sequence[tuple[int, int, int]], *, home: Team, away: Team, through_period: int
) -> str | None:
    """`boxscore.py::_period_line`'s exact row shape (`{code:<4} p1 p2 ...`),
    away above home like the box score, for the periods played so far."""
    played = sorted(row for row in period_scores if row[0] <= through_period)
    if not played:
        return None
    away_row = f"{_code_text(away.code):<4} " + " ".join(str(row[2]) for row in played)
    home_row = f"{_code_text(home.code):<4} " + " ".join(str(row[1]) for row in played)
    return f"```\n{away_row}\n{home_row}\n```"


_MAX_LEADER_NAME_CHARS = 18
_LEADERS_PER_SIDE = 3


# --- the live component (watch-v4 T-1, T-2, T-3) --------------------------------


def _score_title(frame: BroadcastFrame, home: Team, away: Team) -> str:
    """Embed titles do not render Markdown, so the codes are only made
    single-line and capped (`_code_text`), never escaped."""
    return _score_title_of(frame.home_score, frame.away_score, home=home, away=away)


def _score_title_of(home_score: int, away_score: int, *, home: Team, away: Team) -> str:
    return f"{_code_text(away.code)} {away_score} - {home_score} {_code_text(home.code)}"


def _momentum_text(reading: MomentumReading, *, home_code: str, away_code: str) -> str:
    """`MomentumReading.label` with its team code Markdown-escaped, for a
    description where Markdown is live."""
    if reading.label == "even":
        return "even"
    code = home_code if reading.margin > 0 else away_code
    high = max(reading.home_points, reading.away_points)
    low = min(reading.home_points, reading.away_points)
    return f"{code} {high}-{low} run"


def cycle_label(period: int) -> str:
    """The cycle row's own button label: `"Full"` for period 0, `_period_label`
    otherwise (watch-v4 T-3). Period 0 is a key, never a label -- `_period_label`
    is never called with it. Public (no leading underscore, critic round 1, MINOR
    11): `bot/watch_pager.py` is its only caller, exactly like the other
    cross-module names this design exports (`last_line_index`, `visible_periods`)."""
    return "Full" if period == 0 else _period_label(period)


def _stats_scope_label(period: int) -> str:
    """The stats fields' scope name: `"Full Game"` for period 0 (hardcoded, like
    `PagerSource.from_periods`, never through `_period_label`), `_period_label`
    otherwise. Distinct from `cycle_label`'s button text (Mock A2 vs. the cycle
    row)."""
    return "Full Game" if period == 0 else _period_label(period)


def _period_possession_slice(
    plan: BroadcastPlan, *, period: int, last_line: int
) -> tuple[_Possession, ...]:
    """Possessions of `period` at or before `last_line`; `period == 0` (Full
    Game) drops the period filter entirely -- one function, one cap, no second
    path (watch-v4 T-3)."""
    return tuple(
        p for p in plan.possessions if p.index <= last_line and (period == 0 or p.period == period)
    )


def _period_stats_value(
    lines: Mapping[int, LiveLine], *, is_home: bool, names: Mapping[int, str]
) -> str:
    """One side's stats field (watch-v4 T-2.c): the top three scorers by
    `pts / reb / ast`, then one team row of `FG · 3P · FT · REB · AST · TOV`.
    Personal fouls are deliberately absent (watch-v4 T-3: `pf` does not sum
    across a period slice, so it cannot be shown as a period stat)."""
    team = [line for line in lines.values() if line.is_home is is_home]
    rows = [
        f"{_display_name(names, line.player_id, _MAX_LEADER_NAME_CHARS)}  "
        f"{line.points} pts  {line.rebounds} reb  {line.ast} ast"
        for line in leaders(lines.values(), is_home=is_home, count=_LEADERS_PER_SIDE)
    ]
    fgm = sum(line.fgm_rim + line.fgm_mid + line.fgm_three for line in team)
    fga = sum(line.fga_rim + line.fga_mid + line.fga_three for line in team)
    fgm3 = sum(line.fgm_three for line in team)
    fga3 = sum(line.fga_three for line in team)
    ftm = sum(line.ftm for line in team)
    fta = sum(line.fta for line in team)
    reb = sum(line.rebounds for line in team)
    ast = sum(line.ast for line in team)
    tov = sum(line.tov for line in team)
    rows.append(
        f"TEAM  FG {fgm}/{fga}  3P {fgm3}/{fga3}  FT {ftm}/{fta}  REB {reb}  AST {ast}  TOV {tov}"
    )
    return "```\n" + "\n".join(rows) + "\n```"


def _ticker_slice(
    plan: BroadcastPlan,
    narration: Narration,
    *,
    cursor: int,
    is_final: bool,
    selected_period: int,
) -> tuple[str, ...]:
    """The tail the ticker shows (watch-v4 T-1), by LINE (WN-9.2): while the game is
    LIVE, always the whole game's last `ticker_lines` lines up to `cursor`, whatever is
    selected; at FINAL, the selected period's own last `ticker_lines` play lines (a
    separator belongs to no period; Full Game, period 0, is every line -- the same
    tail as LIVE)."""
    if cursor < 0:
        return ()
    lines = narration.lines
    if is_final and selected_period != 0:
        indexes = [
            n
            for n in range(cursor + 1)
            if lines[n].kind is not LineKind.BOUNDARY
            and plan.possessions[lines[n].possession].period == selected_period
        ]
    else:
        indexes = list(range(cursor + 1))
    n = ticker_lines(plan.settings)
    return tuple(lines[i].text for i in indexes[-n:])


def _ticker_value(
    plan: BroadcastPlan,
    narration: Narration,
    *,
    cursor: int,
    is_final: bool,
    selected_period: int,
) -> tuple[str, str] | None:
    """`(header, body)` for the ticker section, or `None` when there is nothing
    to show yet (the TIPOFF frame, `cursor == -1`). The header always names
    the ticker's own scope (watch-v4 T-1, T-1.b): `**Latest**` for the live tail
    or for Full Game at any state, `**Q2 · last plays** · running game score`
    for a selected period at FINAL -- the line carries the cumulative GAME
    score, never a within-period one, so the header says so rather than lie.
    While LIVE in a reading mode the newest line is blockquoted (WN-9.2), here and
    only here, so no stored line ever carries the mark."""
    tail = _ticker_slice(
        plan, narration, cursor=cursor, is_final=is_final, selected_period=selected_period
    )
    if not tail:
        return None
    if not is_final or selected_period == 0:
        header = "**Latest**"
    else:
        header = f"**{_period_label(selected_period)} · last plays** · running game score"
    if not is_final and plan.settings.reveal is RevealUnit.LINE:
        tail = (*tail[:-1], _NEWEST_MARK + tail[-1])
    return header, _LINE_SEPARATOR.join(tail)


def render_live_component(
    frame: BroadcastFrame,
    plan: BroadcastPlan,
    *,
    selected_period: int,
    home: Team,
    away: Team,
    names: Mapping[int, str],
    result: GameResult,
    narration: Narration,
    cursor: int | None = None,
) -> Panel:
    """The ONE live-edited component (watch-v4 T-2): score, status, momentum, a
    live ticker (T-1) and the stats block for `selected_period` (T-2.c, T-3) --
    the SAME shape at every frame and every selection, FINAL included, which
    also gains Team Totals and Story (kept from the old recap). `narration` is the
    game rendered once as lines (`plan_narration`), computed once by the caller and
    shared across every reveal and every click so the ticker, the stats slice and the
    line score all read one truth.

    `cursor` is the flat line index revealed through (WN-9.1); `None` means "all the
    frame reveals" (`frame_cursor`), which is what a frame-at-a-time (`fast`) pacer
    and the goldens render. On a PLAY frame the title's score, the status clock and
    the momentum are read AT the cursor (WN-9.3): a scoring line and its score change
    land in one edit, the clock is the newest line's own, and momentum counts only
    possessions whose ending is on screen. At a frame's own cursor these are exactly
    the frame's values, so a FRAME-reveal render is today's."""
    if cursor is None:
        cursor = frame_cursor(frame, plan, narration)
    home_code, away_code = _md_code(home.code), _md_code(away.code)
    label = _period_label(frame.period)
    is_final = frame.kind is FrameKind.FINAL
    last = completed_through(narration, cursor)
    title = _score_title(frame, home, away)
    if is_final:
        status = "**FINAL**" if frame.period <= _REGULATION_PERIODS else f"**FINAL/{label}**"
    else:
        reading = frame.momentum
        if frame.kind is FrameKind.PLAY:
            home_score, away_score = header_score(plan, narration, cursor)
            title = _score_title_of(home_score, away_score, home=home, away=away)
            reading = (
                _momentum(
                    plan.possessions,
                    last,
                    home_code=_code_text(home.code),
                    away_code=_code_text(away.code),
                    settings=plan.settings,
                )
                if last >= 0
                else _EVEN
            )
        momentum = _momentum_text(reading, home_code=home_code, away_code=away_code)
        if frame.kind is FrameKind.TIPOFF:
            where = f"{label} {frame.clock_display} · tip-off"
        elif frame.kind is FrameKind.PERIOD_END:
            where = f"End of {label} · {momentum}"
        else:
            clock = _format_clock(narration.lines[cursor].clock_seconds)
            where = f"{label} {clock} · {momentum}"
        status = f"**LIVE** · {where}"

    ticker = _ticker_value(
        plan, narration, cursor=cursor, is_final=is_final, selected_period=selected_period
    )
    description_lines = [status]
    if ticker is not None:
        header, body = ticker
        description_lines.extend(("", header, body))
    description = "\n".join(description_lines)

    fields: list[PanelField] = []
    line_score = _line_score_value(
        result.period_scores, home=home, away=away, through_period=_completed_periods(plan, last)
    )
    if line_score is not None:
        fields.append(PanelField(name="Line Score", value=line_score, inline=False))

    period_tally = tally(_period_possession_slice(plan, period=selected_period, last_line=last))
    scope = _stats_scope_label(selected_period)
    plain_home, plain_away = _code_text(home.code), _code_text(away.code)
    fields.append(
        PanelField(
            name=f"{scope} · {plain_home}",
            value=_period_stats_value(period_tally, is_home=True, names=names),
            inline=False,
        )
    )
    fields.append(
        PanelField(
            name=f"{scope} · {plain_away}",
            value=_period_stats_value(period_tally, is_home=False, names=names),
            inline=False,
        )
    )

    if is_final:
        totals = _totals_value(result, home=home, away=away)
        if totals is not None:
            fields.append(PanelField(name="Team Totals", value=totals, inline=False))
        story = _story_value(plan, home=home, away=away)
        if story is not None:
            fields.append(PanelField(name="Story", value=story, inline=False))

    return Panel(
        title=title,
        description=description,
        fields=tuple(fields),
        color=PanelColor.POSITIVE if is_final else PanelColor.NEUTRAL,
    )


def _totals_value(result: GameResult, *, home: Team, away: Team) -> str | None:
    by_team = {line.team_id: line for line in result.team_box_lines}
    rows: list[str] = []
    for team in (away, home):
        line = by_team.get(team.id)
        if line is None:
            continue
        fgm = line.fgm_rim + line.fgm_mid + line.fgm_three
        fga = line.fga_rim + line.fga_mid + line.fga_three
        rows.append(
            f"{_code_text(team.code):<4} FG {fgm}/{fga}  3P {line.fgm_three}/{line.fga_three}  "
            f"FT {line.ftm}/{line.fta}  REB {line.oreb + line.dreb}  AST {line.ast}  "
            f"TOV {line.tov}"
        )
    return "```\n" + "\n".join(rows) + "\n```" if rows else None


def _story_value(plan: BroadcastPlan, *, home: Team, away: Team) -> str | None:
    if not plan.possessions:
        return None
    story = _game_story(plan.possessions)
    home_code, away_code = _md_code(home.code), _md_code(away.code)
    rows = [f"Lead changes: {story.lead_changes}"]
    rows.append(
        f"Largest lead: {home_code} {story.largest_home_lead}, "
        f"{away_code} {story.largest_away_lead}"
    )
    if story.longest_run is not None:
        owner_is_home, points = story.longest_run
        rows.append(f"Longest run: {home_code if owner_is_home else away_code} {points}-0")
    late_home, late_away = story.late_score
    rows.append(
        f"Five minutes left in {_period_label(story.late_period)}: "
        f"{away_code} {late_away}, {home_code} {late_home}"
    )
    return "\n".join(rows)


def render_interrupted(
    frame: BroadcastFrame | None,
    *,
    home: Team,
    away: Team,
    home_score: int,
    away_score: int,
    period_scores: Sequence[tuple[int, int, int]] = (),
) -> Panel:
    """The panel a broadcast that died mid-game ends on (design P5-97.q items
    1 and 6): the game is over, so the result is shown, and `frame` -- the last
    one delivered, `None` if none was -- says where the replay stopped.

    Takes the final score and `(period, home, away)` rows as plain values
    rather than the design's `GameResult`: the startup sweep rebuilds this
    from persisted data alone, and no `GameResult` survives a restart
    (P5-42)."""
    fields: list[PanelField] = []
    if frame is not None:
        fields.append(
            PanelField(
                name="Stopped at",
                value=f"{_period_label(frame.period)} {frame.clock_display}",
                inline=False,
            )
        )
    if period_scores:
        line_score = _line_score_value(
            period_scores,
            home=home,
            away=away,
            through_period=max(row[0] for row in period_scores),
        )
        if line_score is not None:
            fields.append(PanelField(name="Line Score", value=line_score, inline=False))
    return Panel(
        title=f"{_code_text(away.code)} {away_score} - {home_score} {_code_text(home.code)}",
        description="**Broadcast interrupted** — the game is final; the result is below.",
        fields=tuple(fields),
        color=PanelColor.WARNING,
    )


# --- the ephemeral pager (watch-v3 W-10..W-12) ------------------------------------

PAGER_PAGE_LINES = 8
"""Plays per pager page: about one phone screen of embed at the blank-line spacing.
A page is bounded by construction (8 lines of at most `max_pbp_line_chars +
_LINE_MARKUP_ALLOWANCE` characters, every one astral, plus 7 separators is 1,742
units), so no page budget setting exists."""


def page_count(line_count: int) -> int:
    """Pages a period of `line_count` plays occupies (0 when there are none): the last
    page holds the remainder, so 55 plays are 7 pages and 20 are 3."""
    if line_count < 0:
        raise ValueError(f"a period cannot have {line_count} lines")
    return -(-line_count // PAGER_PAGE_LINES)


def clamp_page(page: int, line_count: int) -> int:
    """`page` (1-based) forced into `1..page_count`, or 1 when nothing is shown: a stale
    button that names a page the period no longer has (it shrank under the spoiler cap
    or was never that long) lands on the newest page instead of failing."""
    return max(1, min(page, page_count(line_count)))


def pager_matchup(home: Team, away: Team) -> str:
    """The pager's author line. Made single-line and capped like every code (`_code_text`)."""
    return f"{_code_text(away.code)} @ {_code_text(home.code)}"


def render_pager_page(*, label: str, matchup: str, lines: Sequence[str], page: int) -> Panel:
    """One page of an ephemeral pager: the period's `lines` (already limited to what the
    thread has shown, by the caller), `PAGER_PAGE_LINES` to a page, `page` clamped.

    The title carries the period and the play numbers and NEVER a score: a score in the
    title would state the frame the viewer has not reached, while the running scores inside
    the lines belong to the plays shown (watch-v3 W-12). "of N" counts the plays available
    to this viewer, not the period's real length, for the same reason."""
    total = len(lines)
    if total == 0:
        return Panel(
            title=f"{label} · no plays yet",
            description="Nothing from this period has been broadcast yet.",
            footer="Only you can see this",
            author=matchup,
        )
    pages = page_count(total)
    page = clamp_page(page, total)
    first = (page - 1) * PAGER_PAGE_LINES
    shown = lines[first : first + PAGER_PAGE_LINES]
    newest = " · newest" if page == pages else ""
    return Panel(
        title=f"{label} · plays {first + 1}-{first + len(shown)} of {total}",
        description=_LINE_SEPARATOR.join(shown),
        footer=f"Page {page} of {pages} · only you can see this{newest}",
        author=matchup,
    )
