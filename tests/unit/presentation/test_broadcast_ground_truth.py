"""A rendered play-by-play line may never contradict what the engine did.

`test_broadcast_plan.py` checks the renderer against the beat log's own
shape, and its independent tail helpers read the same beat log the renderer
does -- so a wrong *reading* of the beat log passes both. This file checks
the line against the ENGINE'S OWN RECORD of the possession instead: a
recording wrapper around `dba2.sim.game.simulate_possession` captures every
real `PossessionResult` (its credit events and terminal, with each foul's
`ft_count`/`ftm`) while the game is simulated, and every line is judged
against that.

The defect this pins (critic finding MAJOR-1): the beat log carries no
free-throw count, and the possession's point delta is the SUM over every
credit event. A foul line that printed the delta as "that foul's free
throws" was wrong whenever an earlier event of the possession had scored --
seed 279 possession 141 is `shooting_foul(ft_count=3, ftm=2)` then
`non_shooting_foul(ft_count=0)` closed by the continuation budget
(`possession.py:558`), and it printed "Quintero fouled by Bellweather,
scores +2" for a foul that shot nothing.

**RC-4/RC-16 note (docs/design/auto-gm-role-construction.md, 2026-09-23):**
this was seed 257 possession 51 until the minutes-curve fix changed who is
on the floor for every possession of every seed in this corpus, which moved
this exact rare shape. Re-scanned the same `_SEEDS` corpus for a possession
matching MAJOR-1's shape (`shooting_foul(ft_count=3, ftm=2)` ->
`non_shooting_foul(ft_count=0)` -> budget-forced `TURNOVER_DEAD`,
`resolution_beats == 2`) and found exactly one: seed 388 possession 21,
with the identical `(ft_count, ftm, points) = (3, 2, 2)` numbers the
original had -- a pure re-anchor, no assertion values needed to change.

**PD-10 note (docs/design/gm-command-surface.md, 2026-09-23):** moved
again, same mechanism -- `offensive_scheme` (never neutral, PD-10.4) and
the concurrently-landed RC-20/per-36-spread-compression rebound fix both
shifted every possession's late-game shape across the whole corpus. Seed
388 possession 21 no longer matches MAJOR-1's shape; re-scanned the same
`_SEEDS` corpus and found exactly one match: seed 279 possession 141, with
the identical `(ft_count, ftm, points) = (3, 2, 2)` numbers -- again a pure
re-anchor.

**Turnover-recovery note (docs/design/turnover-recovery.md, TUNING_VERSION
20 -> 21):** the recovered loose ball moves every possession's continuation
budget and shot clock, and the exact MAJOR-1 shape is gone -- rescanned seeds
200-1299 and no possession is `shooting_foul(3, 2)` -> `non_shooting_foul(0)`
-> forced close any more. The anchor is now the nearest shape inside this
same corpus that carries the defect: seed 269 possession 2, an and-one whose
free throw MISSED (`made_fg`, `ft_count=1`, `ftm=0`, 2 points) followed by
two non-bonus fouls that shot nothing and the budget-forced close. Its delta
(+2) is still not either foul's, and a line printing "fouled ..., scores +2"
is still exactly the defect -- same claim, same numbers in the line, a
different earlier scoring event.

**Also (TR-4.15(c)):** `_lines` renders the LEAF grammar only (`events=()`),
so `_contradictions` never sees `_event_parts`' output. The loose-ball
recovery clause is the first events-grammar text with a hard no-claim rule,
so it gets its own clause-level check below `_contradictions`.

The vocabulary of claims a line can make, and the ground truth each is held
to, is `_contradictions`; every claim has a mutation twin below, and the
whole check has a corpus-level twin that re-plants the defect.
"""

from __future__ import annotations

import dataclasses
import re
import string
from dataclasses import dataclass

import pytest

from dba2.presentation import broadcast
from dba2.presentation.broadcast import (
    PlanSettings,
    _Ending,
    render_pbp_line,
    segment_possessions,
)
from dba2.presentation.narration_phrases import LIBRARY, OUTCOME_GROUPS
from dba2.sim import game as sim_game
from dba2.sim import possession as sim_possession
from dba2.sim.context import GameContext
from dba2.sim.events import GameResult, TerminalKind, TerminalOutcome
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context
from tests.unit.presentation._broadcast_helpers import broadcast_variant, make_team, names_for
from tests.unit.presentation.test_live_box import _event

_GAME_ID = 412
_SEEDS = tuple(range(200, 400))  # includes 279 (the critic's counterexample, see module docstring)
_WIDE = PlanSettings(max_pbp_line_chars=200)  # nothing shrinks: the claims are whole sentences

_FOUL_KINDS = (TerminalKind.SHOOTING_FOUL, TerminalKind.NON_SHOOTING_FOUL)
_TURNOVER_KINDS = (TerminalKind.TURNOVER_LIVE, TerminalKind.TURNOVER_DEAD)


@dataclass(frozen=True)
class _ChainCall:
    """One chain-beat (or resolution) call as the engine made it: the beat kind,
    the players it was CALLED with (keyed like the beat log's `actor_ids`), and
    the outcome it RETURNED. Captured by patching the resolver where
    `possession.py` looks it up (`dba2.sim.possession.resolve_*_beat`, bound at
    import), so it is independent of the `BeatRecord` the resolver also builds."""

    beat: str
    actors: tuple[tuple[str, int], ...]
    outcome: str

    def actor(self, key: str) -> int | None:
        return dict(self.actors).get(key)


def _setup_call(args, kwargs, result) -> _ChainCall:
    roles = result.role_assignment
    actors = {f"offense:{role.value}": pid for role, pid in roles.offense.items()}
    actors |= {f"defense:{role.value}": pid for role, pid in roles.defense.items()}
    return _ChainCall("setup", tuple(sorted(actors.items())), result.action_type.value)


def _pair_call(beat: str, first: str, second: str):
    def build(args, kwargs, result) -> _ChainCall:
        return _ChainCall(
            beat,
            tuple(sorted({first: args[0].id, second: args[1].id}.items())),
            result.outcome.value,
        )

    return build


def _rotation_call(args, kwargs, result) -> _ChainCall:
    actors = {key: kwargs[key].id for key in ("vacated_shooter", "passer", "closing_defender")}
    offered = {key: kwargs.get(key) for key in ("tagger", "popper_defender")}
    for key, player in offered.items():
        if player is not None:
            actors[key] = player.id
    if any(player is not None for player in offered.values()):
        actors["screener"] = kwargs["screener"].id
    return _ChainCall("rotation", tuple(sorted(actors.items())), result.outcome.value)


def _resolution_call(args, kwargs, result) -> _ChainCall:
    # The leaf string exists only on the record; the players are the call's own.
    actors = {"shooter": args[0].id, "defender": args[1].id}
    return _ChainCall("resolution", tuple(sorted(actors.items())), result.record.outcome)


_CHAIN_RECORDERS = {
    "resolve_setup_beat": _setup_call,
    "resolve_screen_beat": _pair_call("screen", "screener", "screen_defender"),
    "resolve_on_ball_beat": _pair_call("on_ball", "ball_handler", "on_ball_defender"),
    "resolve_help_beat": _pair_call("help", "driver", "helper"),
    "resolve_rotation_beat": _rotation_call,
    "resolve_resolution_beat": _resolution_call,
}


@dataclass(frozen=True)
class _Truth:
    """One possession as the engine recorded it: `events` are its credit
    events in order, then its terminal; `resolution_beats` is how many
    `resolution` beats its beat log holds. Every credit event has exactly
    one, and the terminal has one unless it was FORCED (a spent-budget
    close, `possession.py:497`/`:562`) or was a shot-clock tick
    (`:646`-`:881`) -- so the event the line's last `resolution` leaf names
    is `events[resolution_beats - 1]`."""

    events: tuple[TerminalOutcome, ...]
    resolution_beats: int
    # docs/design/watch-narration-v2.md WN-12 T2/T3: the engine's own account of the
    # possession's chain beats and of the time it used -- captured at the call, not
    # read back off the beat log, so a pre-line is judged against the engine too.
    chain: tuple[_ChainCall, ...] = ()
    elapsed_seconds: float = 0.0

    @property
    def terminal(self) -> TerminalOutcome:
        return self.events[-1]

    @property
    def last_named(self) -> TerminalOutcome | None:
        return self.events[self.resolution_beats - 1] if self.resolution_beats else None

    @property
    def points(self) -> int:
        return sum(sim_game._points_for(event) for event in self.events)

    @property
    def is_shot_clock_violation(self) -> bool:
        """The terminal is a dead-ball turnover that has no `resolution` beat
        AND names a ball handler: a tick-caused one. A budget-forced close
        passes no player id (`possession.py:414`'s docstring)."""
        terminal = self.terminal
        return (
            self.resolution_beats == len(self.events) - 1
            and terminal.kind is TerminalKind.TURNOVER_DEAD
            and terminal.turnover_player_id is not None
        )


def _is_forced_close(truth: _Truth) -> bool:
    """The terminal is a budget-forced `_turnover_dead()` (`possession.py:497`
    / `:562`): no `resolution` beat of its own and no ball handler named."""
    terminal = truth.terminal
    return (
        len(truth.events) > 1
        and truth.resolution_beats == len(truth.events) - 1
        and terminal.kind is TerminalKind.TURNOVER_DEAD
        and terminal.turnover_player_id is None
    )


@dataclass(frozen=True)
class _RecordedGame:
    seed: int
    result: GameResult
    truths: tuple[_Truth, ...]


def _simulate_recording(seed: int) -> _RecordedGame:
    """Simulate one default-fixture game with `simulate_possession` wrapped
    by a recorder. `game.py` binds the name at import, so the patch is on
    `dba2.sim.game`. The chain resolvers are bound in `possession.py` the
    same way, so their recorders (WN-12 T2) patch `dba2.sim.possession`."""
    real = sim_game.simulate_possession
    truths: list[_Truth] = []
    calls: list[_ChainCall] = []

    def recording(*args, **kwargs):
        calls.clear()
        sim_result = real(*args, **kwargs)
        credit = tuple(sim_result.result.credit_events)
        resolutions = sum(1 for record in sim_result.beat_log if record.beat == "resolution")
        # The pairing rule itself is an engine fact the tests below rely on.
        assert resolutions in (len(credit), len(credit) + 1), (seed, resolutions, len(credit))
        truths.append(
            _Truth(
                (*credit, sim_result.result.terminal),
                resolutions,
                chain=tuple(calls),
                elapsed_seconds=sim_result.elapsed_seconds,
            )
        )
        return sim_result

    def recorder(name: str, build):
        resolver = getattr(sim_possession, name)

        def recorded(*args, **kwargs):
            result = resolver(*args, **kwargs)
            calls.append(build(args, kwargs, result))
            return result

        return recorded

    context: GameContext = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sim_game, "simulate_possession", recording)
        for name, build in _CHAIN_RECORDERS.items():
            patch.setattr(sim_possession, name, recorder(name, build))
        result = sim_game.simulate_game(context, rng_seed=seed)
    return _RecordedGame(seed, result, tuple(truths))


@pytest.fixture(scope="module")
def games() -> list[_RecordedGame]:
    return [_simulate_recording(seed) for seed in _SEEDS]


def _possessions(game: _RecordedGame):
    return segment_possessions(
        game.result.beat_log,
        player_team={line.player_id: line.team_id for line in game.result.player_box_lines},
        home_team_id=game.result.home_team_id,
    )


def _lines(game: _RecordedGame, *, settings: PlanSettings = _WIDE) -> list[str]:
    possessions = _possessions(game)
    names = names_for(game.result)
    return [
        render_pbp_line(
            dataclasses.replace(p, events=()),  # the LEAF grammar: these claims are about it
            names=names,
            home_code="HOM",
            away_code="AWY",
            settings=settings,
        )
        for p in possessions
    ]


_HEAD = re.compile(r"^\d+:\d\d  \S+ (?:\*\*[^*]+\*\*  )?")


def _sentence(line: str) -> str:
    """`11:40  🏀 **HOM 2-0**  <sentence>` -> `<sentence>` (the score is absent on a
    possession that did not score)."""
    return _HEAD.sub("", line, count=1)


def _head(line: str) -> str:
    """The clock, glyph and bold score a line starts with."""
    head = _HEAD.match(line)
    assert head is not None, line
    return head.group(0)


_FOUL_SCORES = re.compile(r"fouled(?: by [^,]+)?, scores \+(\d+)")
_FOUL_NO_POINTS = re.compile(r"fouled(?: by [^,]+)?, no points")
_MADE_SCORES = re.compile(r"^\S+ scores \+(\d+)")
_PLAIN_REBOUND = re.compile(r"(?<!offensive )rebound")


def _contradictions(line: str, truth: _Truth) -> list[str]:
    """Every claim `line` makes that the engine's own record of the
    possession contradicts. Empty means the line claims nothing false; it
    does NOT mean the line claims everything true -- a foul whose free
    throws the beat log cannot attribute is allowed to say only the foul and
    the possession's `(+N)`."""
    sentence = _sentence(line)
    last = truth.last_named
    problems: list[str] = []

    if (claim := _FOUL_SCORES.search(sentence)) is not None:
        n = int(claim.group(1))
        if last is None or last.kind not in _FOUL_KINDS or last.ftm != n:
            problems.append(f"'scores +{n}' on a foul whose own ftm is {last and last.ftm}")
    if _FOUL_NO_POINTS.search(sentence) and (
        last is None or last.kind not in _FOUL_KINDS or last.ftm != 0
    ):
        problems.append("'no points' on a foul whose free throws scored")
    if "no free throws" in sentence and (
        last is None or last.kind is not TerminalKind.NON_SHOOTING_FOUL or last.ft_count != 0
    ):
        problems.append("'no free throws' on a foul that shot free throws")

    if ("shot clock violation" in sentence.lower()) != truth.is_shot_clock_violation:
        problems.append(
            f"'shot clock violation' is {'in' if truth.is_shot_clock_violation else 'absent from'}"
            f" the line but the terminal {'is' if truth.is_shot_clock_violation else 'is not'} one"
        )

    board = None if last is None else last.rebound_is_offensive
    if ("offensive rebound" in sentence) != (board is True):
        problems.append(f"'offensive rebound' vs the engine's rebound_is_offensive={board}")
    if bool(_PLAIN_REBOUND.search(sentence)) != (board is False):
        problems.append(f"a defensive 'rebound' vs the engine's rebound_is_offensive={board}")

    turnover = last is not None and last.kind in _TURNOVER_KINDS
    if ("turnover" in sentence) != turnover:
        problems.append(f"'turnover' vs the named event's kind {last and last.kind}")
    if ("stolen" in sentence) != bool(turnover and last.steal):
        problems.append("'stolen' vs the engine's steal flag")
    if sentence.startswith("Offensive foul") != (
        last is not None and last.kind is TerminalKind.OFFENSIVE_FOUL
    ):
        problems.append("'Offensive foul' vs the named event's kind")

    missed = last is not None and last.kind is TerminalKind.MISSED_FG
    if (" blocked" in sentence) != bool(missed and last.blocked):
        problems.append("'blocked' vs the engine's block flag")
    if (" misses" in sentence) != bool(missed and not last.blocked):
        problems.append("'misses' vs the engine's unblocked miss")

    made = _MADE_SCORES.search(sentence)
    if (made is not None) != (last is not None and last.kind is TerminalKind.MADE_FG):
        problems.append("'scores' as the lead phrase vs the named event's kind")
    if made is not None and int(made.group(1)) != truth.points:
        problems.append(f"'scores +{made.group(1)}' but the possession scored {truth.points}")

    fouled = last is not None and (
        last.kind in _FOUL_KINDS or (last.kind is TerminalKind.MADE_FG and last.and_one)
    )
    if ("fouled" in sentence) != fouled:
        problems.append("'fouled' vs the named event's kind")

    deltas = [int(n) for n in re.findall(r"\+(\d+)", sentence)]
    foul_claim = _FOUL_SCORES.search(sentence)
    for n in deltas:
        if foul_claim is not None and n == int(foul_claim.group(1)) and last is not None:
            continue  # judged above against the foul's own ftm
        if n != truth.points:
            problems.append(f"'+{n}' but the possession scored {truth.points}")
    if truth.points > 0 and not deltas:
        problems.append(f"the possession scored {truth.points} and the line hides it")
    if truth.points == 0 and deltas:
        problems.append("a '+N' on a possession that scored nothing")
    return problems


# --- the loose-ball recovery clause (docs/design/turnover-recovery.md TR-4.15(c)) ---
#
# `_contradictions` above judges the LEAF grammar only (`_lines` strips `events`),
# so it never sees what `_event_parts` prints for a recovery -- which is the text a
# real `/watch` reader actually sees. A recovery makes no statistical claim, so its
# clause must contain none of the claims `_contradictions` checks. Where that
# function uses a compiled pattern it is REUSED here, so the two stay coupled; each
# plain substring names the `_contradictions` check it mirrors.
#
# ONE tuple, shared: /watch narration v2's pre-lines (docs/design/watch-narration-v2.md
# WN-12 T1(d)) are held to the same list, so a claim word added here for either surface
# binds both. Turnover-recovery landed first, so v2 adopted this tuple (WN-13).
_TERMINAL_CLAIM_SUBSTRINGS = (
    "turnover",  # the 'turnover' vs the named event's kind check
    "rebound",  # both rebound checks ('offensive rebound' and _PLAIN_REBOUND)
    "stolen",  # the 'stolen' vs the steal flag check
    "steal",  # the same claim worded as a verb: 'steals it back', 'stealing'
    " blocked",  # the 'blocked' check
    " misses",  # the unblocked-miss check
    "fouled",  # the 'fouled' vs the named event's kind check
    "no free throws",  # the non-bonus close check
    "no points",  # _FOUL_NO_POINTS
    "shot clock violation",  # the violation check
)
_TERMINAL_CLAIM_PATTERNS = (_FOUL_SCORES, _FOUL_NO_POINTS, _MADE_SCORES, _PLAIN_REBOUND)


def _terminal_claims(text: str) -> list[str]:
    """Every statistical claim `text` makes that a recovery clause must not."""
    lowered = text.lower()
    found = [s for s in _TERMINAL_CLAIM_SUBSTRINGS if s in lowered]
    found += [p.pattern for p in _TERMINAL_CLAIM_PATTERNS if p.search(text)]
    if re.search(r"\+\d", text):
        found.append("+N")
    if text.startswith("Offensive foul"):
        found.append("Offensive foul")
    return found


@pytest.mark.parametrize("terse", [False, True], ids=["full", "terse"])
@pytest.mark.parametrize(
    ("loser", "recoverer"),
    [(11, 12), (11, 11), (11, None)],
    ids=["different-players", "same-player", "missing-recoverer"],
)
def test_the_recovery_clause_makes_no_claim_the_box_score_could_contradict(terse, loser, recoverer):
    """TR-4.15(c), mutation 16b: all six combinations of `_event_parts`' recovery
    arm are clean. A missing `recovering_player_id` should never happen (the
    engine raises first) but still renders production's placeholder for a
    `None` id, "Unknown" (`broadcast._display_name`), rather than a claim."""
    event = _event(
        TerminalKind.LOOSE_BALL_RECOVERED,
        loose_ball_player_id=loser,
        recovering_player_id=recoverer,
    )
    names = {11: "Okafor", 12: "Lindqvist"}
    parts = broadcast._event_parts(
        event,
        nm=lambda pid: broadcast._display_name(names, pid, 99),
        terse=terse,
        restart_close=False,
    )
    text = ", ".join(parts)
    assert parts, "the recovery arm printed nothing -- the event is invisible again"
    assert _terminal_claims(text) == [], text
    if recoverer is None:
        assert "Unknown recovers" in text, text
        return
    if terse:
        return
    assert names[recoverer] in text


def test_the_recovery_clause_check_has_teeth():
    """Twin: the checker flags the plausible-sounding rewordings TR-4.15(c)
    warns about."""
    assert _terminal_claims("Okafor loses it, Lindqvist recovers") == []
    for bad in (
        "Okafor turnover, Lindqvist recovers",
        "Lindqvist recovers the rebound",
        "Okafor loses it, stolen back by Lindqvist",
        "Okafor loses it, Lindqvist steals it back",
        "Lindqvist recovers (+2)",
    ):
        assert _terminal_claims(bad), bad


@pytest.mark.slow
def test_the_recovery_clause_is_really_printed_on_the_corpus(games):
    """Non-vacuity for the six unit cases above: rendered WITH its events (not
    the leaf-grammar strip `_lines` uses), at least one real line of the corpus
    carries a recovery clause -- otherwise they test text nothing emits."""
    seen = 0
    for game in games[:20]:
        names = names_for(game.result)
        for p in _possessions(game):
            if not any(e.kind is TerminalKind.LOOSE_BALL_RECOVERED for e in p.events):
                continue
            line = render_pbp_line(p, names=names, home_code="HOM", away_code="AWY", settings=_WIDE)
            if " recovers" in line:
                seen += 1
    assert seen > 0


def _all_contradictions(games: list[_RecordedGame]) -> list[tuple[int, int, str, list[str]]]:
    found = []
    for game in games:
        lines = _lines(game)
        assert len(lines) == len(game.truths), (game.seed, len(lines), len(game.truths))
        for index, (line, truth) in enumerate(zip(lines, game.truths, strict=True)):
            problems = _contradictions(line, truth)
            if problems:
                found.append((game.seed, index, line, problems))
    return found


# --- the corpus -------------------------------------------------------------


def test_the_recording_corpus_holds_the_critics_counterexample(games):
    """Seed 269 possession 2 carries the defect MAJOR-1 was found on (was
    seed 279 possession 141 until turnover-recovery removed the exact shape,
    seed 388 possession 21 until PD-10/RC-20/per-36-spread-compression moved
    it, and seed 257 possession 51 before that -- see this module's own
    docstring); if a tuning change moves it again this test says so instead
    of the corpus quietly no longer covering it."""
    assert len(games) >= 100 and {200, 257, 320, 279, 269} <= {g.seed for g in games}
    truth = next(g for g in games if g.seed == 269).truths[2]
    first, second, third, terminal = truth.events
    assert (first.kind, first.and_one, first.ft_count, first.ftm) == (
        TerminalKind.MADE_FG,
        True,
        1,
        0,
    )
    assert (second.kind, second.ft_count) == (TerminalKind.NON_SHOOTING_FOUL, 0)
    assert (third.kind, third.ft_count) == (TerminalKind.NON_SHOOTING_FOUL, 0)
    assert terminal.kind is TerminalKind.TURNOVER_DEAD and terminal.turnover_player_id is None
    assert truth.resolution_beats == 3 and truth.points == 2


def test_no_rendered_line_contradicts_the_engines_own_record_of_the_possession(games):
    """Every possession of >= 100 seeded games (seeds 200-399, so 257 and the
    critic's 200-320 range are both in): no line claims a foul scored what its
    own free throws did not, that a foul shot nothing when it shot, a shot-clock
    violation the terminal was not, an offensive rebound that was not, and so
    on -- each judged against the recorded `PossessionResult`."""
    found = _all_contradictions(games)
    assert not found, f"{len(found)} contradicted lines; first: {found[:3]}"


def test_the_corpus_exercises_every_foul_shape_the_rule_distinguishes(games):
    """Non-vacuity: the corpus must contain the foul lines the rule allows to
    claim points, the ones it must NOT, and the possessions the defect lived
    in -- otherwise the test above could pass by never meeting them."""
    scored_foul = neutral_foul = no_ft_close = 0
    for game in games:
        for line, truth in zip(_lines(game), game.truths, strict=True):
            sentence = _sentence(line)
            last = truth.last_named
            if last is None or last.kind not in _FOUL_KINDS:
                continue
            if _FOUL_SCORES.search(sentence):
                scored_foul += 1
            elif truth.points > 0:
                neutral_foul += 1  # a foul line that says only the foul and the delta
            if "no free throws" in sentence:
                no_ft_close += 1
    assert scored_foul > 50 and neutral_foul > 0 and no_ft_close > 0, (
        scored_foul,
        neutral_foul,
        no_ft_close,
    )


def _ending_mismatches(games: list[_RecordedGame]) -> list[tuple[int, int, str, str]]:
    """Every possession whose `_Possession.ending` disagrees with what the
    engine recorded, as `(seed, index, ending, why)`. Only what the engine's
    own record settles is asserted in each direction; `UNDETERMINED` is
    allowed exactly where the log cannot settle it."""
    bad: list[tuple[int, int, str, str]] = []
    for game in games:
        for p, truth in zip(_possessions(game), game.truths, strict=True):
            ending = p.ending
            last = truth.last_named
            forced = _is_forced_close(truth)
            forced_foul = (  # `:558`: a NON-BONUS foul (no trip), not a bonus foul's `:493` board
                forced
                and last is not None
                and last.kind is TerminalKind.NON_SHOOTING_FOUL
                and last.ft_count == 0
            )
            forced_oreb = forced and last is not None and last.rebound_is_offensive is True
            violation = truth.is_shot_clock_violation
            claims = {
                _Ending.PRE_RESOLUTION_VIOLATION: violation and len(truth.events) == 1,
                _Ending.RESTART_VIOLATION: violation and len(truth.events) > 1,
                _Ending.OFFENSIVE_REBOUND_CLOSE: forced_oreb,
            }
            for shape, holds in claims.items():
                if (ending is shape) != holds:
                    bad.append((game.seed, p.index, ending.value, f"{shape.value} is {holds}"))
            if ending is _Ending.NON_BONUS_FOUL_CLOSE and not forced_foul:
                bad.append((game.seed, p.index, ending.value, "a non-bonus close that was not"))
            if forced_foul and ending not in (
                _Ending.NON_BONUS_FOUL_CLOSE,
                _Ending.UNDETERMINED_FOUL_CLOSE,
            ):
                bad.append((game.seed, p.index, ending.value, "a forced foul close read as clean"))
            if ending is _Ending.UNDETERMINED_FOUL_CLOSE and not (
                last is not None
                and last.kind is TerminalKind.NON_SHOOTING_FOUL
                and truth.points > 0
            ):
                bad.append((game.seed, p.index, ending.value, "undetermined off its own shape"))
            if ending is _Ending.CLEAN and (forced or violation):
                bad.append((game.seed, p.index, ending.value, "clean but the engine forced it"))
    return bad


def test_the_ending_classifier_agrees_with_the_engines_own_record(games):
    """The classifier's shapes, judged against the recorded terminal rather
    than against a second reading of the same beat log. Includes the budget
    close after earlier points (seed 269 possession 2; seed 279 possession
    141, seed 388 possession 21 and seed 257 possession 51 before that -- see
    this module's own docstring), which the beat log cannot tell from a bonus
    foul whose free throws all went in: that is `UNDETERMINED_FOUL_CLOSE`,
    never `CLEAN`."""
    assert not _ending_mismatches(games), _ending_mismatches(games)[:5]
    endings = {p.ending for game in games for p in _possessions(game)}
    assert endings == set(_Ending), endings  # every shape occurred, so none is vacuous
    game = next(g for g in games if g.seed == 269)
    assert _possessions(game)[2].ending is _Ending.UNDETERMINED_FOUL_CLOSE


def test_the_ending_check_has_teeth_when_the_attribution_defect_is_replanted(games, monkeypatch):
    """Twin: with `_foul_points_attributable` forced True an undetermined close
    reads as `CLEAN`, which the engine's record contradicts."""
    assert not _ending_mismatches(games)
    monkeypatch.setattr(broadcast, "_foul_points_attributable", lambda possession: True)
    bad = _ending_mismatches(games)
    assert any("forced foul close read as clean" in why for *_, why in bad), bad[:3]


# --- mutation twins ---------------------------------------------------------


def _truth_269_2(games) -> tuple[_Truth, str]:
    """Was `_truth_279_141`, `_truth_388_21` and `_truth_257_51` before that --
    see this module's own docstring for why the counterexample's location
    moved."""
    game = next(g for g in games if g.seed == 269)
    return game.truths[2], _lines(game)[2]


def test_the_checker_has_teeth_the_critics_line_is_flagged_and_the_fixed_line_is_not(games):
    truth, fixed = _truth_269_2(games)
    assert _contradictions(fixed, truth) == [], fixed
    head = _head(fixed)
    defective = f"{head}Quintero fouled by Bellweather, scores +2"
    problems = _contradictions(defective, truth)
    assert any("'scores +2' on a foul whose own ftm is 0" in p for p in problems), problems


def _truth_clean_scored_foul(games) -> tuple[_Truth, str]:
    """A possession that is one shooting foul whose free throws scored
    (`ftm == points`, no rebound): the line may honestly say `scores +N`."""
    for game in games:
        for line, truth in zip(_lines(game), game.truths, strict=True):
            last = truth.last_named
            if (
                len(truth.events) == 1
                and last is not None
                and last.kind is TerminalKind.SHOOTING_FOUL
                and last.ftm >= 1
                and last.rebound_is_offensive is None
            ):
                return truth, line
    raise AssertionError("no clean scored foul in the corpus")


@pytest.mark.parametrize(
    ("sentence", "fragment"),
    [
        pytest.param("Q fouled by B, no free throws (+{n})", "no free throws", id="ft"),
        pytest.param("Q fouled by B, no points (+{n})", "'no points'", id="points"),
        pytest.param("Q fouled by B, scores +{m}", "'scores +", id="foul-scores-wrong-n"),
        pytest.param("Q fouled by B (+{n}), then shot clock violation", "shot clock", id="viol"),
        pytest.param("Q fouled by B (+{n}), A offensive rebound", "offensive rebound", id="oreb"),
        pytest.param("Q fouled by B (+{n}), A rebound", "defensive 'rebound'", id="reb"),
        pytest.param("Q turnover (+{n})", "turnover", id="turnover"),
        pytest.param("Q turnover, stolen by B (+{n})", "'stolen'", id="stolen"),
        pytest.param("Q scores +{n}", "lead phrase", id="lead"),
        pytest.param("Q fouled by B (+{m})", "possession scored", id="delta"),
        pytest.param("Q fouled by B", "hides it", id="hidden"),
        pytest.param("Q misses (+{n})", "misses", id="misses"),
        pytest.param("Q blocked by B (+{n})", "blocked", id="blocked"),
        pytest.param("Offensive foul, Q (+{n})", "Offensive foul", id="offfoul"),
    ],
)
def test_the_checker_has_teeth_each_claim_kind_is_caught(games, sentence, fragment):
    """Each mutant swaps a real line's sentence for one that makes a single
    false claim; the checker must name it. The control -- the sentence the
    renderer really printed -- must be clean, so the flag is attributable to
    the mutation."""
    truth, line = _truth_clean_scored_foul(games)
    head = _head(line)
    n = truth.points
    assert _contradictions(line, truth) == [], line
    assert _contradictions(f"{head}Q fouled by B, scores +{n}", truth) == []
    assert _contradictions(f"{head}Q fouled by B (+{n})", truth) == []  # the neutral form
    problems = _contradictions(f"{head}" + sentence.format(n=n, m=n + 1), truth)
    assert any(fragment in p for p in problems), (sentence, problems)


def test_the_shot_clock_and_delta_checks_have_teeth_on_a_real_violation(games):
    """A genuine tick violation: a line that omits it is flagged, and one that
    invents it on a non-violation is flagged (the previous test only covered
    the invented direction)."""
    game = next(g for g in games if any(t.is_shot_clock_violation for t in g.truths))
    index = next(i for i, t in enumerate(game.truths) if t.is_shot_clock_violation)
    truth, line = game.truths[index], _lines(game)[index]
    assert _contradictions(line, truth) == [], line
    head = _head(line)
    assert _contradictions(f"{head}Quintero misses", truth)


def test_the_corpus_check_has_teeth_when_the_attribution_defect_is_replanted(games, monkeypatch):
    """Corpus-level twin: make the renderer believe a foul's points are always
    its own free throws (the exact pre-fix behaviour) and the same corpus
    check must go red."""
    assert not _all_contradictions(games), "the unmodified renderer must be clean"
    monkeypatch.setattr(broadcast, "_foul_points_attributable", lambda possession: True)
    found = _all_contradictions(games)
    assert found, "re-planting the defect went unnoticed"
    assert any("on a foul whose own ftm" in p for _, _, _, ps in found for p in ps), found[:3]


# --- narration v2: the chain facts against the engine (docs/design/watch-narration-v2.md) ---
#
# WN-12's third surface: the pre-lines. Their facts come from the chain beats, so the
# chain beats themselves are first held to what the engine was CALLED with (T2's
# recorder), and the clock derived from them to the time the engine really used (T3).


def _beats_by_possession(game: _RecordedGame):
    return broadcast._group_by_possession(game.result.beat_log)


def _chain_mismatches(games: list[_RecordedGame]) -> list[tuple[int, int, str]]:
    bad = []
    for game in games:
        groups = _beats_by_possession(game)
        assert len(groups) == len(game.truths)
        for index, (beats, truth) in enumerate(zip(groups, game.truths, strict=True)):
            logged = [b for b in beats if b.beat != "rebound"]
            if len(logged) != len(truth.chain):
                bad.append((game.seed, index, f"{len(logged)} beats vs {len(truth.chain)} calls"))
                continue
            for record, call in zip(logged, truth.chain, strict=True):
                actors = dict(call.actors)
                logged_actors = {k: record.actor_ids.get(k) for k in actors}
                if record.beat != call.beat or record.outcome != call.outcome:
                    bad.append((game.seed, index, f"{record.beat}/{record.outcome} vs {call}"))
                elif logged_actors != actors or (
                    record.beat != "resolution" and set(record.actor_ids) != set(actors)
                ):
                    bad.append((game.seed, index, f"{record.actor_ids} vs {actors}"))
    return bad


def test_every_chain_beat_in_the_log_is_the_call_the_engine_made(games):
    """T2's recorder half: the beat log's chain beats are, one for one and in order,
    the resolver calls the engine made -- same players in the same roles, same outcome
    -- so a pre-line built from the log is built from the engine's own account."""
    assert not _chain_mismatches(games), _chain_mismatches(games)[:3]
    assert all(truth.chain for game in games for truth in game.truths)  # non-vacuous


def test_the_chain_recorder_has_teeth(games):
    """Twin: a recorded call with one role swapped is reported."""
    game = games[0]
    truth = game.truths[0]
    swapped = tuple(
        (key, -1) if key == "shooter" else (key, pid) for key, pid in truth.chain[-1].actors
    )
    forged = dataclasses.replace(
        truth, chain=(*truth.chain[:-1], dataclasses.replace(truth.chain[-1], actors=swapped))
    )
    forged_game = dataclasses.replace(game, truths=(forged, *game.truths[1:]))
    assert _chain_mismatches([forged_game])


def _clock_violations(games: list[_RecordedGame], module=broadcast) -> list[tuple[int, int, str]]:
    bad = []
    for game in games:
        for index, (beats, truth) in enumerate(
            zip(_beats_by_possession(game), game.truths, strict=True)
        ):
            elapsed = module._elapsed_before(beats)
            if any(b < a for a, b in zip(elapsed, elapsed[1:], strict=False)):
                bad.append((game.seed, index, f"not monotone: {elapsed}"))
            if elapsed[-1] > truth.elapsed_seconds + 1e-9:
                bad.append((game.seed, index, f"{elapsed[-1]} > {truth.elapsed_seconds}"))
    return bad


def test_the_derived_clock_never_runs_backwards_or_past_the_engines_own_time(games):
    """T3 (WN-6): the elapsed time derived from the shot clocks is monotone inside every
    possession and never exceeds the `elapsed_seconds` the engine returned -- the
    unrecorded remainder is the tick after the last beat."""
    assert not _clock_violations(games), _clock_violations(games)[:3]
    resets = sum(
        1
        for game in games[:10]
        for beats in _beats_by_possession(game)
        for a, b in zip(beats, beats[1:], strict=False)
        if b.clock_seconds > a.clock_seconds
    )
    assert resets > 0, "no offensive-rebound reset in the corpus: the rise rule is untested"


def test_the_derived_clock_check_has_teeth_when_a_reset_is_counted_as_time(games):
    """Twin: counting the offensive-rebound reset's RISE as a (negative) step makes the
    derived clock run backwards, and the corpus check says so."""
    mutated = broadcast_variant(
        (
            "total += max(0.0, previous.clock_seconds - following.clock_seconds)",
            "total += previous.clock_seconds - following.clock_seconds",
        )
    )
    assert _clock_violations(games[:10], module=mutated)


# --- narration v2: every pre-line against the engine (WN-12 T1, T2, T5, T7) -----------
#
# The FULL renderer is run through `_Narrator.possession_lines`, which returns each line
# with the indexes of the beats it was built from. Names are synthetic (`P<id>q`), so a
# name in a line inverts to exactly one player id -- the test instrument WN-15 R8 calls
# for, since real leagues can repeat a surname.

_FULL_WIDE = PlanSettings(max_pbp_line_chars=200, narration=broadcast.NarrationLevel.FULL)
_NAME_TOKEN = re.compile(r"P(\d+)q")


def _id_names(result: GameResult) -> dict[int, str]:
    return {line.player_id: f"P{line.player_id}q" for line in result.player_box_lines}


@dataclass(frozen=True)
class _Narrated:
    """One possession rendered FULL: its lines with their source beat indexes, its
    beats, and the engine's own record of it."""

    beats: tuple
    lines: tuple
    truth: _Truth
    possession: object


def _narrate_game(game: _RecordedGame, module=broadcast, settings=_FULL_WIDE) -> list[_Narrated]:
    plan = module.plan_broadcast(
        game.result, game_id=412, home_code="HOM", away_code="AWY", settings=settings
    )
    home, away = make_team(1, "HOM"), make_team(2, "AWY")
    names = _id_names(game.result)
    narrator = module._Narrator(plan, game.result, home=home, away=away, names=names)
    groups = module._group_by_possession(game.result.beat_log)
    return [
        _Narrated(
            tuple(beats), tuple(narrator.possession_lines(possession, beats)), truth, possession
        )
        for possession, beats, truth in zip(plan.possessions, groups, game.truths, strict=True)
    ]


@pytest.fixture(scope="module")
def narrated(games) -> list[list[_Narrated]]:
    return [_narrate_game(game) for game in games]


def _call_for(item: _Narrated, beat_index: int) -> _ChainCall:
    """The engine's recorded call behind beat `beat_index` (rebounds are not calls)."""
    calls = iter(item.truth.chain)
    for position, beat in enumerate(item.beats):
        if beat.beat == "rebound":
            continue
        call = next(calls)
        if position == beat_index:
            return call
    raise AssertionError(f"no call behind beat {beat_index}")


def _key_patterns(key) -> list[re.Pattern[str]]:
    out = []
    for phrase in LIBRARY[key]:
        parts = []
        for literal, field, _, _ in string.Formatter().parse(phrase.template):
            parts.append(re.escape(literal))
            if field:
                parts.append(r"P\d+q(?: [a-z ]+?)?")
        out.append(re.compile("".join(parts)))
    return out


def _matches(key, text: str) -> bool:
    return any(pattern.search(text) for pattern in _key_patterns(key))


_GROUP_OF = {key: group for group in OUTCOME_GROUPS for key in group}
_RESTART_KEY = {
    ("on_ball", "contained"): "reset",
    ("rebound", "offense"): "offensive_board",
    ("resolution", "non_shooting_foul"): "foul",
    ("resolution", "loose_ball_recovered"): "scramble",  # WN-13's fourth row
}
_ALL_PREFIXES = tuple(
    phrase.template
    for (slot, _), phrases in LIBRARY.items()
    if slot == "R"
    for phrase in phrases
    if "{" not in phrase.template
)


def _expected_outcome_keys(item: _Narrated, sources) -> list[tuple[str, str]]:
    """Which outcome keys a chain line MUST say, from the engine's recorded calls."""
    keys = []
    for index in sources:
        call = _call_for(item, index)
        if call.beat == "screen":
            keys.append(("C", call.outcome))
        elif call.beat == "on_ball":
            keys.append(("O", call.outcome))
        elif call.beat == "help":
            stayed = call.outcome == "help_stayed"
            keys.append(("O+", "help_stayed") if stayed else ("H", call.outcome))
        elif call.beat == "rotation":
            kept = call.outcome == "no_pass"
            keys.append(("O+", "keeps_it") if kept else ("P", call.outcome))
    return keys


def _pre_line_contradictions(item: _Narrated, line, sources) -> list[str]:
    """WN-12 T1 for one CHAIN line, judged against the engine's calls (T2)."""
    problems = []
    text = _sentence(line.text)
    named = {int(pid) for pid in _NAME_TOKEN.findall(text)}
    allowed = {pid for index in sources for _, pid in _call_for(item, index).actors}
    if not named <= allowed:
        problems.append(f"(a) names {sorted(named - allowed)} not in the line's beats")
    for key in _expected_outcome_keys(item, sources):
        if not _matches(key, text):
            problems.append(f"(b) {key} is not said")
        for other in _GROUP_OF.get(key, ()):
            if other != key and _matches(other, text):
                problems.append(f"(b) {other} is said but the beat was {key}")
    if line.kind == "setup":  # by value: a mutated module has its own enum class
        first = sources[0]
        reason = None
        if first > 0:
            previous = item.beats[first - 1]
            reason = _RESTART_KEY.get((previous.beat, previous.outcome))
        said = {prefix for prefix in _ALL_PREFIXES if text.startswith(prefix)}
        wanted = {p.template for p in LIBRARY[("R", reason)]} if reason else set()
        if bool(said) != bool(wanted) or not said <= wanted:
            problems.append(f"(c) prefix {sorted(said)} but the restart reason is {reason}")
    if _terminal_claims(text):
        problems.append(f"(d) terminal claims {_terminal_claims(text)} in a pre-line")
    return problems


_NARRATED_BEATS = frozenset({"setup", "screen", "on_ball", "help", "rotation"})


def _unnarrated_beats(item: _Narrated) -> list[str]:
    """A3/WN-4.2's coverage half of T1(b): every chain beat the engine recorded is said
    by exactly one pre-line -- so dropping a clause (a no-pass, a kick-out) is caught
    even though the lines that remain are all true."""
    said: dict[int, int] = {}
    for line, sources in item.lines:
        if line.kind in _CHAIN_KIND_VALUES:
            for index in sources:
                said[index] = said.get(index, 0) + 1
    return [
        f"(b) beat {index} ({beat.beat}/{beat.outcome}) is said {said.get(index, 0)} times"
        for index, beat in enumerate(item.beats)
        if beat.beat in _NARRATED_BEATS and said.get(index, 0) != 1
    ]


_CHAIN_KIND_VALUES = frozenset({"setup", "on_ball", "pass"})


def _all_pre_line_contradictions(narrated_games) -> list:
    found = []
    for game_items in narrated_games:
        for item in game_items:
            for line, sources in item.lines:
                if line.kind in _CHAIN_KIND_VALUES:
                    problems = _pre_line_contradictions(item, line, sources)
                    if problems:
                        found.append((line.text, problems))
            missing = _unnarrated_beats(item)
            if missing:
                found.append((item.lines[-1][0].text, missing))
    return found


@pytest.mark.slow
def test_no_pre_line_contradicts_the_engines_own_record(narrated):
    """T1 (a)-(d) over every CHAIN line of the 200-seed corpus, against the engine's
    recorded calls (T2): every name is a player of the line's own beats, every outcome
    word is the recorded outcome's and no sibling outcome's, a restart names its reason
    iff the beat before it is that reason, and no pre-line carries a terminal claim."""
    found = _all_pre_line_contradictions(narrated)
    assert not found, f"{len(found)} contradicted pre-lines; first: {found[:3]}"


def test_the_corpus_narrates_every_slot_and_outcome_the_rules_distinguish(narrated):
    """Non-vacuity for T1: every outcome key of every beat group, every restart
    reason, "now has it", and a location suffix all really occur."""
    texts = [
        _sentence(line.text)
        for game_items in narrated[:60]
        for item in game_items
        for line, _ in item.lines
        if line.kind in broadcast.CHAIN_LINE_KINDS
    ]
    for group in OUTCOME_GROUPS[:-1]:  # the T clauses are terminal-line text
        for key in group:
            assert any(_matches(key, t) for t in texts), key
    for reason in ("reset", "offensive_board", "foul", "scramble", "now_has_it"):
        assert any(_matches(("R", reason), t) for t in texts), reason
    assert any(" in the corner" in t or " on the weak side" in t for t in texts)


def test_a_full_possession_is_about_three_lines_and_ends_on_its_terminal(narrated):
    """WN-2.2: ~3.1 lines a possession (band 2.6-3.6), every possession's last line is
    its TERMINAL, and it has exactly one."""
    total_lines = sum(len(item.lines) for g in narrated for item in g)
    possessions = sum(len(g) for g in narrated)
    assert 2.6 <= total_lines / possessions <= 3.6, total_lines / possessions
    for game_items in narrated:
        for item in game_items:
            kinds = [line.kind for line, _ in item.lines]
            assert kinds[-1] is broadcast.LineKind.TERMINAL
            assert kinds.count(broadcast.LineKind.TERMINAL) == 1


def test_the_lines_of_a_possession_sum_to_its_points(narrated):
    """T4's arithmetic half (WN-9.3): the sum of `Line.points` over a possession is
    exactly its score delta -- including a credit that scored (an and-one whose free
    throw missed and was rebounded by the offense) and a promoted credit on a forced
    close."""
    bad = [
        (
            item.possession.index,
            item.possession.points,
            [(ln.kind, ln.points) for ln, _ in item.lines],
        )
        for game_items in narrated
        for item in game_items
        if sum(line.points for line, _ in item.lines) != item.possession.points
    ]
    assert not bad, bad[:3]
    scoring_credits = sum(
        1
        for game_items in narrated
        for item in game_items
        for line, _ in item.lines
        if line.kind is broadcast.LineKind.CREDIT and line.points > 0
    )
    assert scoring_credits > 0, "no scoring credit line: the new arithmetic path is untested"


def test_every_pre_line_clock_is_the_derived_clock_between_start_and_end(narrated):
    """T3's line half: per possession the CHAIN/CREDIT clocks are non-increasing, the
    first equals the possession's start clock, and none reads earlier than its end."""
    for game_items in narrated:
        for item in game_items:
            p = item.possession
            clocks = [line.clock_seconds for line, _ in item.lines[:-1]]
            if not clocks:
                continue
            shown = [broadcast._format_clock(c) for c in clocks]
            assert clocks == sorted(clocks, reverse=True), shown
            assert shown[0] == broadcast._format_clock(p.clock_start_seconds)
            end = broadcast._format_clock(p.clock_end_seconds)
            assert all(broadcast._format_clock(c) >= end or c == 0.0 for c in clocks) or all(
                c >= p.clock_end_seconds - 1e-9 or c == 0.0 for c in clocks
            ), (shown, end)


# --- A3: who has the ball (WN-4) -------------------------------------------------------
#
# The design's corpus test for A3: walk each possession's FULL lines in order, tracking
# the last player a line GAVE the ball to, re-derived from the engine's own calls and
# events (never the renderer's tables). The roles below are the playbook's: who holds the
# ball when an action starts, and who has it once its S line is said.

_START_ROLE = {
    "pnr_ball": "ball_handler",
    "dho": "ball_handler",
    "iso": "ball_handler",
    "transition": "ball_handler",
    "post_up": "entry_passer",
    "off_ball_screen": "entry_passer",
    "spot_up": "passer",
}
_RECEIVER_ROLE = _START_ROLE | {
    "post_up": "post_scorer",
    "off_ball_screen": "cutter",
    "spot_up": "shooter",
}
_PASS_TARGET = {
    "kickout_open": "vacated_shooter",
    "kickout_contested": "vacated_shooter",
    "roll_live": "screener",
    "roll_tagged": "screener",
    "pop_open": "screener",
    "pop_contested": "screener",
}


def _event_actor(event: TerminalOutcome) -> int | None:
    """The player a line about `event` says had the ball when it happened. A shot-clock
    violation is charged by the engine to the segment's handler, not done with the ball
    (seed 217 possession 59 charges the passer of a kick-out), so it names nobody here."""
    kind = event.kind
    if kind in (TerminalKind.MADE_FG, TerminalKind.MISSED_FG):
        return event.scorer_id
    if kind in _FOUL_KINDS:
        return event.fouled_player_id
    if kind is TerminalKind.TURNOVER_LIVE:
        return event.turnover_player_id
    if kind is TerminalKind.OFFENSIVE_FOUL:
        return event.offensive_fouler_id
    return None


def _given_by_credit(event: TerminalOutcome, holder: int | None) -> int | None:
    """Who a credit line hands the ball to: the offensive rebounder, the loose ball's
    recoverer, or the player fouled on a non-bonus foul."""
    if event.rebound_is_offensive and event.rebounder_id is not None:
        return event.rebounder_id
    if event.kind is TerminalKind.LOOSE_BALL_RECOVERED:
        return event.recovering_player_id
    if event.kind is TerminalKind.NON_SHOOTING_FOUL:
        return event.fouled_player_id
    return holder


def _call_index(item: _Narrated, beat_index: int) -> int:
    """The engine call behind beat `beat_index` (rebounds are not calls)."""
    return sum(1 for beat in item.beats[:beat_index] if beat.beat != "rebound")


def _recorded_restart_reason(item: _Narrated, call_index: int) -> str | None:
    """WN-4.3's reason for the restart whose setup is the engine's `call_index`-th call,
    read off the call before it and, after a resolution, that resolution's own event --
    not off the beat log the renderer reads."""
    previous = item.truth.chain[call_index - 1]
    if previous.beat == "on_ball":
        return "reset" if previous.outcome == "contained" else None
    if previous.beat != "resolution":
        return None
    k = sum(1 for call in item.truth.chain[:call_index] if call.beat == "resolution") - 1
    event = item.truth.events[k]
    if event.rebound_is_offensive:
        return "offensive_board"
    if event.kind is TerminalKind.NON_SHOOTING_FOUL:
        return "foul"
    if event.kind is TerminalKind.LOOSE_BALL_RECOVERED:
        return "scramble"
    return None


def _now_has_it(text: str) -> int | None:
    """The player a line says "now has it" (any variant), or `None`."""
    for pattern in _key_patterns(("R", "now_has_it")):
        found = pattern.search(text)
        if found is not None:
            return int(_NAME_TOKEN.search(found.group(0)).group(1))
    return None


_TEAM_MARKER = re.compile(r"^(HOM|AWY) ball, ")


def _without_marker(text: str) -> str:
    return _TEAM_MARKER.sub("", text, count=1)


def _ball_tracking_problems(item: _Narrated) -> list[str]:
    """A3 for one possession. Each problem names the rule it breaks: (holder) an S line
    opens on the player who has the ball, a dribble or a pass is by the player who has
    it, and a credit's or the terminal's shooter (fouled man, turnover man) is the last
    player a line gave the ball to; (now) "X now has it" iff a restart's X is not that
    player and the restart is not a reset (WN-17: after a board, a foul or a scramble the
    reader was just told who had it, so a new holder is news; a reset's S line opening
    on X already says it); (reason) a restart's prefix is the recorded reason's;
    (recoverer) a recovered loose ball's line names the recoverer."""
    problems: list[str] = []
    holder: int | None = None
    resolutions = [i for i, beat in enumerate(item.beats) if beat.beat == "resolution"]
    for line, sources in item.lines:
        text = _without_marker(_sentence(line.text))
        kind = line.kind.value  # by value: a mutated module has its own enum class
        if kind == "setup":
            index = _call_index(item, sources[0])
            call = item.truth.chain[index]
            start = call.actor(f"offense:{_START_ROLE[call.outcome]}")
            said = _now_has_it(text)
            reason = _recorded_restart_reason(item, index) if index > 0 else None
            changed = index > 0 and holder is not None and start != holder
            changed = changed and reason != "reset"
            if changed and said != start:
                problems.append(f"(now) P{holder}q had it, P{start}q starts, unsaid: {text!r}")
            if not changed and said is not None:
                problems.append(f"(now) 'now has it' but the holder did not change: {text!r}")
            opener = text[text.index(f"P{said}q") :] if said is not None else text
            for prefix in _ALL_PREFIXES:
                opener = opener.removeprefix(prefix)
            if not opener.startswith(f"P{start}q"):
                problems.append(f"(holder) the S line does not open on P{start}q: {text!r}")
            said_prefixes = {p for p in _ALL_PREFIXES if text.startswith(p)}
            wanted = {p.template for p in LIBRARY[("R", reason)]} if reason else set()
            if bool(said_prefixes) != bool(wanted) or not said_prefixes <= wanted:
                problems.append(f"(reason) {sorted(said_prefixes)} but the reason is {reason}")
            holder = call.actor(f"offense:{_RECEIVER_ROLE[call.outcome]}")
        elif kind == "on_ball":
            dribbler = item.truth.chain[_call_index(item, sources[0])].actor("ball_handler")
            if dribbler != holder:
                problems.append(f"(holder) P{dribbler}q dribbles but P{holder}q has it: {text!r}")
            holder = dribbler
        elif kind == "pass":
            call = item.truth.chain[_call_index(item, sources[-1])]
            if call.actor("passer") != holder:
                problems.append(f"(holder) P{call.actor('passer')}q passes, P{holder}q has it")
            holder = call.actor(_PASS_TARGET[call.outcome])
        elif kind in ("credit", "terminal"):
            if kind == "terminal" and _is_forced_close(item.truth):
                continue  # the forced close is no act with the ball (possession.py:428)
            k = resolutions.index(sources[0]) if kind == "credit" else len(item.truth.events) - 1
            event = item.truth.events[k]
            actor = _event_actor(event)
            if actor is not None and actor != holder:
                problems.append(f"(holder) P{actor}q acts but P{holder}q has it: {text!r}")
            if event.kind is TerminalKind.LOOSE_BALL_RECOVERED and (
                f"P{event.recovering_player_id}q" not in text
            ):
                problems.append(f"(recoverer) P{event.recovering_player_id}q unnamed: {text!r}")
            holder = _given_by_credit(event, holder)
    return problems


def _all_ball_tracking_problems(narrated_games) -> list[tuple[int, list[str]]]:
    return [
        (item.possession.index, problems)
        for game_items in narrated_games
        for item in game_items
        if (problems := _ball_tracking_problems(item))
    ]


def test_a_possession_opens_on_its_team_when_the_ball_changes_sides(narrated):
    """WN-17's team marker: the first line of a possession names the offense's code when
    the ball changed sides or a new period began, and no other line carries one."""
    for game_items in narrated:
        side = None
        for item in game_items:
            p = item.possession
            code = "HOM" if p.offense_is_home else "AWY"
            wanted = (p.period, p.offense_is_home) != side
            side = (p.period, p.offense_is_home)
            for number, (line, _) in enumerate(item.lines):
                marker = _TEAM_MARKER.match(_sentence(line.text))
                expected = wanted and number == 0 and line.kind is broadcast.LineKind.SETUP
                assert bool(marker) == expected, (p.index, line.text)
                if marker:
                    assert marker.group(1) == code, (p.index, line.text)


def test_the_reader_always_knows_who_has_the_ball(narrated):
    """A3 over every possession of the 200-seed corpus, against the engine's calls and
    events: the S line opens on the player who holds the ball, every dribble, pass and
    shot is by the last player a line gave the ball to, "X now has it" is said exactly
    when a restart's ball holder is someone else, the restart's prefix is the recorded
    reason's, and a recovered loose ball names its recoverer."""
    found = _all_ball_tracking_problems(narrated)
    assert not found, f"{len(found)} possessions lose the ball; first: {found[:3]}"


def test_the_ball_tracking_corpus_exercises_every_handover(narrated):
    """Non-vacuity for A3: "now has it" really follows each credit-caused restart reason,
    a restart that does not say it really occurs, and a recovery really hands the ball
    on."""
    said_after: set[str | None] = set()
    unsaid = scramble_handover = 0
    for game_items in narrated[:80]:
        for item in game_items:
            for line, sources in item.lines:
                if line.kind is not broadcast.LineKind.SETUP or sources[0] == 0:
                    continue
                reason = _recorded_restart_reason(item, _call_index(item, sources[0]))
                if _now_has_it(_sentence(line.text)) is None:
                    unsaid += 1
                    continue
                said_after.add(reason)
                scramble_handover += reason == "scramble"
    assert said_after == {"offensive_board", "foul", "scramble"}, said_after
    assert unsaid > 0 and scramble_handover > 0


_BALL_TRACKING_TWINS = [
    pytest.param(
        (
            "    if credit.kind is TerminalKind.LOOSE_BALL_RECOVERED"
            " and credit.recovering_player_id is not None:\n"
            "        return credit.recovering_player_id\n",
            "",
        ),
        id="M3-recoverer-handover-dropped",
    ),
    pytest.param(
        (
            "holder is not None and start_holder != holder\n",
            "holder is not None\n",
        ),
        id="M4-now-has-it-on-every-restart",
    ),
]


@pytest.mark.parametrize("mutation", _BALL_TRACKING_TWINS)
def test_the_ball_tracking_check_has_teeth(games, mutation):
    """T10 twins for A3, the critic's two hand mutations: M3 forgets that the recoverer
    has the ball (seed 1468 possession 82's "After the scramble -- P10q has it now" loses
    its handover), M4 says "now has it" when the holder did not change (seed 1468
    possession 0's "They reset -- P5q has it now" to the player who already had it; since
    WN-17 a reset never says it, so the twin shows on a board, foul or scramble restart)."""
    few = games[:12]
    mutated = broadcast_variant(mutation)
    found = _all_ball_tracking_problems([_narrate_game(g, mutated) for g in few])
    assert any(p.startswith("(now)") for _, problems in found for p in problems), found[:2]


# --- the head of a FULL line is its OWN event's (WN-17) ---------------------------------
#
# FULL splits a possession's credits onto their own lines, so the possession-wide head
# TERSE prints (its most consequential glyph, its end score) would sit on lines that did
# not earn it: the critic found 288 of 10,402 terminal lines over seeds 0-49 with a glyph
# contradicting their own event, 31 zero-point lines under a basket, and 67 scoring
# credit lines with no score. The expected glyph is re-derived here from the engine's
# own event, not through `_event_glyph_key`.

_BASKETS = frozenset({"🏀", "🎯"})


def _expected_glyph(event: TerminalOutcome) -> str | None:
    kind = event.kind
    if kind is TerminalKind.MADE_FG:
        return "🎯" if event.zone is not None and event.zone.value == "three" else "🏀"
    if kind is TerminalKind.MISSED_FG:
        return "🔒" if event.blocked else "❌"
    if kind in _FOUL_KINDS:
        return "🛑"
    if kind is TerminalKind.TURNOVER_LIVE:
        return "⚡"
    if kind is TerminalKind.OFFENSIVE_FOUL or (
        kind is TerminalKind.TURNOVER_DEAD and event.turnover_player_id is not None
    ):
        return "🔻"
    if kind is TerminalKind.LOOSE_BALL_RECOVERED:
        return "🔄"
    return None


_FULL_HEAD = re.compile(r"^\d+:\d\d  (\S+) (?:\*\*(?:HOM|AWY) (\d+)-(\d+)\*\*  )?")


def _head_problems(item: _Narrated) -> list[str]:
    """Every FULL line's head against the event the line itself narrates: the glyph is
    that event's (a pre-line and a scoreless credit keep the dot), the bold score is there
    iff the line scored and reads the running score after it, and no scoreless line sits
    under a basket."""
    problems: list[str] = []
    truth = item.truth
    p = item.possession
    home_after, away_after = p.score_after
    offense_after, defense_after = (
        (home_after, away_after) if p.offense_is_home else (away_after, home_after)
    )
    running = offense_after - truth.points
    resolutions = [i for i, beat in enumerate(item.beats) if beat.beat == "resolution"]
    forced = _is_forced_close(truth)
    for line, sources in item.lines:
        head = _FULL_HEAD.match(line.text)
        if head is None:
            problems.append(f"no head: {line.text!r}")
            continue
        glyph, scored = head.group(1), head.group(2)
        kind = line.kind.value
        if kind in _CHAIN_KIND_VALUES:
            event = None
        elif kind == "credit":
            event = truth.events[resolutions.index(sources[0])]
        else:
            event = truth.events[-2] if forced and len(truth.events) > 1 else truth.terminal
        points = sim_game._points_for(event) if event is not None else 0
        if points != line.points:
            problems.append(f"line points {line.points} but its event scored {points}")
        running += points
        if event is None or (kind == "credit" and points == 0):
            wanted = broadcast._CHAIN_GLYPH
        else:
            wanted = _expected_glyph(event)
        if wanted is not None and glyph != wanted:
            problems.append(f"glyph {glyph} but the line's own event wants {wanted}: {line.text!r}")
        if points == 0 and (glyph in _BASKETS or scored is not None):
            problems.append(f"a 0-point line with a basket head or a score: {line.text!r}")
        if points > 0:
            if scored is None:
                problems.append(f"a {points}-point line shows no score: {line.text!r}")
            elif (int(head.group(2)), int(head.group(3))) != (running, defense_after):
                problems.append(f"score {head.group(2)}-{head.group(3)} but it is {running}")
    return problems


def _all_head_problems(narrated_games) -> list[tuple[int, list[str]]]:
    return [
        (item.possession.index, problems)
        for game_items in narrated_games
        for item in game_items
        if (problems := _head_problems(item))
    ]


def test_every_full_head_describes_its_own_line(narrated):
    """WN-17 over the 200-seed corpus: a FULL line's glyph is its own event's, a scoreless
    line never carries a basket or a bold score, and a scoring line -- a credit included
    -- shows the running score after it."""
    found = _all_head_problems(narrated)
    assert not found, f"{len(found)} possessions with a wrong head; first: {found[:3]}"


def test_the_head_corpus_holds_the_shapes_that_used_to_lie(narrated):
    """Non-vacuity: a scoring credit, a scoreless terminal after a scoring credit (the
    old basket-on-a-miss), and a terminal whose event is not the possession's most
    consequential one all really occur."""
    scoring_credit = scoreless_after_score = 0
    for game_items in narrated:
        for item in game_items:
            kinds = [(line.kind.value, line.points) for line, _ in item.lines]
            scoring_credit += any(k == "credit" and pts > 0 for k, pts in kinds)
            scoreless_after_score += kinds[-1][1] == 0 and item.possession.points > 0
    assert scoring_credit > 0 and scoreless_after_score > 0


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            (
                "            glyph=_own_glyph(possession, event),\n",
                "            glyph=_line_glyph(possession),\n",
            ),
            id="possession-glyph-on-the-terminal",
        ),
        pytest.param(
            (
                "        if points > 0:\n            head = _own_head(\n",
                "        if False:\n            head = _own_head(\n",
            ),
            id="scoring-credit-keeps-the-dot",
        ),
    ],
)
def test_the_head_check_has_teeth(games, mutation):
    """T10 twins: put the possession's glyph back on the terminal, or drop a scoring
    credit's score head, and the corpus check goes red."""
    few = games[:40]
    found = _all_head_problems([_narrate_game(g, broadcast_variant(mutation)) for g in few])
    assert found


def test_the_full_feed_rarely_repeats_a_sentence_or_restates_who_has_the_ball(games):
    """WN-17's reader measures, with the renderer's own names (`names_for`, as a viewer
    would see them): over the critic's 10 games, 6.4% of sentences were exact repeats of
    an earlier one in the same game and about one line in ten said "now has it". Both
    are held well under that now (measured 3.2% and 2.2% over seeds 0-49)."""
    home, away = make_team(1, "HOM"), make_team(2, "AWY")
    sentences = repeats = now = 0
    for game in games[:20]:
        plan = broadcast.plan_broadcast(
            game.result, game_id=412, home_code="HOM", away_code="AWY", settings=_FULL_WIDE
        )
        narration = broadcast.plan_narration(
            game.result, plan, home=home, away=away, names=names_for(game.result)
        )
        seen: set[str] = set()
        for line in narration.lines:
            if line.kind is broadcast.LineKind.BOUNDARY:
                continue
            text = _sentence(line.text)
            sentences += 1
            repeats += text in seen
            seen.add(text)
            now += any(
                re.search(p.template.replace("{X}", r"\S+").replace(",", r"\,"), text)
                for p in LIBRARY[("R", "now_has_it")]
            )
    assert repeats / sentences < 0.045, repeats / sentences
    assert 0.005 < now / sentences < 0.05, now / sentences


# --- T5: who he scored on, and who stopped him ----------------------------------------

_SPOT_KEYS = ("over", "tough_over", "stopped", "closed_out", "tagged")
_WON = {
    ("on_ball", "defender_wins"): ("on_ball_defender", "stopped"),
    ("rotation", "kickout_contested"): ("closing_defender", "closed_out"),
    ("rotation", "pop_contested"): ("popper_defender", "closed_out"),
    ("rotation", "roll_tagged"): ("tagger", "tagged"),
}


def _core(result: GameResult) -> set[int]:
    teams: dict[int, list] = {}
    for line in result.player_box_lines:
        teams.setdefault(line.team_id, []).append((-line.minutes_played, line.player_id))
    return {pid for rows in teams.values() for _, pid in sorted(rows)[:5]}


def _expected_spotlight(item: _Narrated, core: set[int]) -> tuple[str, int] | None:
    """WN-5's rule, re-derived from the ENGINE'S calls and outcomes (not the renderer's
    helpers): rule A (a defender-won read in the final segment whose winner is the
    shot's recorded defender), then the two-beat tough-make clause."""
    truth = item.truth
    calls = truth.chain
    if truth.resolution_beats != len(truth.events) or not calls or calls[-1].beat != "resolution":
        return None
    last_setup = max(i for i, c in enumerate(calls) if c.beat == "setup")
    segment = calls[last_setup:]
    shot = calls[-1]
    event = truth.terminal
    winner = None
    for call in segment:
        if (call.beat, call.outcome) in _WON:
            key, clause = _WON[(call.beat, call.outcome)]
            if call.actor(key) == shot.actor("defender"):
                winner = (clause, call.actor(key))
            break
    if event.kind is TerminalKind.MISSED_FG and not event.blocked and winner:
        return winner
    if event.kind is TerminalKind.MADE_FG and winner:
        return ("over", winner[1])
    if event.kind is TerminalKind.MADE_FG and {c.beat for c in segment} == {"setup", "resolution"}:
        record = [b for b in item.beats if b.beat == "resolution"][-1]
        made = record.probabilities.get("made")
        missed = record.probabilities.get("missed_unblocked")
        defender = shot.actor("defender")
        if made is not None and missed and made / (made + missed) < 0.40 and defender in core:
            return ("tough_over", defender)
    return None


def _said_spotlight(text: str) -> list[tuple[str, int]]:
    """Every defender clause in `text`, as `(key, defender id)`. A variant can contain
    another's text (" right over P5q" holds " over P5q"), so only the longest match
    over any stretch of the line counts."""
    found = []
    for key in _SPOT_KEYS:
        for phrase in LIBRARY[("T", key)]:
            pattern = re.escape(phrase.template).replace(re.escape("{D}"), r"P(\d+)q")
            for m in re.finditer(pattern, text):
                found.append((m.start(), m.end(), key, int(m.group(1))))
    kept: list[tuple[int, int, str, int]] = []
    for start, end, key, pid in sorted(found, key=lambda f: f[0] - f[1]):
        if all(end <= s0 or start >= e0 for s0, e0, _, _ in kept):
            kept.append((start, end, key, pid))
    return [(key, pid) for _, _, key, pid in sorted(kept)]


def _spotlight_mismatches(narrated_games, games) -> tuple[list, dict[str, int]]:
    bad, counts = [], dict.fromkeys(_SPOT_KEYS, 0)
    for game_items, game in zip(narrated_games, games, strict=True):
        core = _core(game.result)
        for item in game_items:
            terminal = item.lines[-1][0]
            expected = _expected_spotlight(item, core)
            said = _said_spotlight(terminal.text)
            if said != ([expected] if expected else []):
                bad.append((game.seed, item.possession.index, terminal.text, expected))
            for key, _ in said:
                counts[key] += 1
    return bad, counts


def test_the_spotlight_clause_fires_on_recorded_defender_facts_and_names_the_contester(
    narrated, games
):
    """T5: "stopped him"/"closed it out"/"tagged it" iff rule A and naming the shot's
    recorded defender; "over {D}" iff rule A on a make; "a tough one over {D}" iff a
    two-beat make at p_make < .40 over a core-five defender; never on an open read or a
    block. Rates within 50% of WN-5's measured per-game figures."""
    bad, counts = _spotlight_mismatches(narrated, games)
    assert not bad, bad[:3]
    per_game = {key: n / len(games) for key, n in counts.items()}
    stops = per_game["stopped"] + per_game["closed_out"] + per_game["tagged"]
    assert 13.7 * 0.5 <= stops <= 13.7 * 1.5, per_game
    assert 13.0 * 0.5 <= per_game["over"] <= 13.0 * 1.5, per_game
    assert 8.8 * 0.5 <= per_game["tough_over"] <= 8.8 * 1.5, per_game


def test_the_spotlight_check_has_teeth_when_stops_fire_on_every_miss(games):
    """T10 twin for T5: credit the contester on EVERY unblocked miss, the p_make-free
    rule WN-5.4 rejected in its crudest form."""
    mutated = broadcast_variant(
        (
            "        if event.kind is TerminalKind.MISSED_FG and not event.blocked and read"
            " is not None:\n"
            "            key, defender = _STOP_CLAUSES[(read.beat, read.outcome)],"
            " read.defender_id\n",
            "        if event.kind is TerminalKind.MISSED_FG and not event.blocked:\n"
            '            key, defender = "stopped", resolution.actor_ids["defender"]\n',
        )
    )
    few = games[:5]
    bad, _ = _spotlight_mismatches([_narrate_game(g, mutated) for g in few], few)
    assert bad


_PRE_LINE_TWINS = [
    pytest.param(
        "(a)",
        (
            '        defender = on_ball.actor_ids.get("on_ball_defender")\n',
            '        defender = setup.actor_ids.get("defense:on_ball_def")\n',
        ),
        id="setup-defender-on-the-O-line",
    ),
    pytest.param(
        "(b)",
        (
            "            if rotation is not None:  # no_pass: he keeps it\n",
            "            if False:\n",
        ),
        id="drop-keeps-it",
    ),
    pytest.param(
        "(c)",
        (
            "            reason = _restart_reason(previous) if previous is not None else None\n",
            '            reason = "reset" if previous is not None else None\n',
        ),
        id="reset-on-every-restart",
    ),
    pytest.param(
        "(d)",
        (
            "        text = _fit_line(head, sentence_for, limit=self._limit, tiers=_CHAIN_TIERS)\n",
            "        text = _fit_line(head, sentence_for, limit=self._limit, tiers=_CHAIN_TIERS)"
            ' + " (+2)"\n',
        ),
        id="a-delta-on-a-pre-line",
    ),
]


@pytest.mark.parametrize(("claim", "mutation"), _PRE_LINE_TWINS)
def test_the_pre_line_check_has_teeth(games, claim, mutation):
    """T10 twins for T1(a)-(d): each plants one defect in a copy of the module, and the
    corpus check names that very claim."""
    few = games[:12]
    mutated = broadcast_variant(mutation)
    found = _all_pre_line_contradictions([_narrate_game(g, mutated) for g in few])
    assert any(p.startswith(claim) for _, problems in found for p in problems), found[:2]


@pytest.mark.slow
def test_terse_narration_is_todays_play_lines_byte_for_byte(games):
    """T7 (A5): at TERSE the narration IS `_play_lines`, for every corpus game, so fast
    mode, the pager and the `fast_forward` golden cannot move."""
    home, away = make_team(1, "HOM"), make_team(2, "AWY")
    for game in games:
        names = names_for(game.result)
        for settings in (PlanSettings(), _WIDE, PlanSettings(possessions_per_frame=4)):
            plan = broadcast.plan_broadcast(
                game.result, game_id=412, home_code="HOM", away_code="AWY", settings=settings
            )
            narration = broadcast.plan_narration(
                game.result, plan, home=home, away=away, names=names
            )
            played = broadcast._play_lines(plan, home=home, away=away, names=names)
            assert narration.texts == tuple(played)
            assert narration.terminal_at == tuple(range(len(plan.possessions)))


def test_the_same_game_words_every_line_the_same_way_and_another_game_id_does_not(games):
    """A8 over a real game: two renders are identical; a different `game_id` rewords at
    least one line."""
    home, away = make_team(1, "HOM"), make_team(2, "AWY")
    game = games[0]
    names = names_for(game.result)

    def render(game_id):
        plan = broadcast.plan_broadcast(
            game.result, game_id=game_id, home_code="HOM", away_code="AWY", settings=_FULL_WIDE
        )
        return broadcast.plan_narration(game.result, plan, home=home, away=away, names=names)

    assert render(412).texts == render(412).texts
    assert render(412).texts != render(413).texts
