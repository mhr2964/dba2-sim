"""The `/advance` press report as a `Panel` (docs/design/phase-5-stage-e-advance.md
P5-99.a, P5-99.m).

**Takes plain values only.** `dba2/presentation/` is a pure package that may import
`dba2.domain`, `dba2.core` and the sim's event types and nothing else (P5-3), so it
cannot name `season_service.PressReport`. `bot/cogs/advance.py` reads the report --
which is where the service's types live -- and hands this module sentences and phases;
nothing here decides what a press did, only how it is laid out.

**A `Panel` raises over Discord's embed limits, and this is built AFTER the press has
committed.** A raise here would cost the person the whole reply for a press that landed,
so every list is fitted to a budget with a "...and N more" line (the same trade
`presentation/draft.py`'s cycle post makes) and the two field budgets plus the
description budget sum comfortably under the combined 6000-character ceiling.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from dba2.domain.phase import Phase
from dba2.presentation.panel import Panel, PanelColor, PanelField

# 2800 + 2 x 1000 + a title and two field names stays under the 6000-character combined
# ceiling with room to spare; each field is also under its own 1024.
_DESCRIPTION_BUDGET = 2800
_FIELD_BUDGET = 1000
_LINE_BUDGET = 400
_MORE_RESERVE = 40


class PressOutcome(StrEnum):
    """The four press kinds, restated here because this package cannot import the
    service's `PressKind`. The cog maps one to the other 1:1."""

    ENTRY = "entry"
    BLOCK = "block"
    BLOCKED = "blocked"
    DRAFT_RESUME = "draft_resume"


@dataclass(frozen=True)
class PressSummary:
    """What a press did, as sentences. `done` is what ran, one line each. `gate_lines`
    are the exit gate's blockers verbatim -- what stands between the league and leaving
    `from_phase`; empty means nothing does. `notices` are the Discord-side steps that did
    not go out (a slate, a wire post, a standings sync): the press committed either
    way, and the reply says which part of the record is missing."""

    outcome: PressOutcome
    from_phase: Phase
    to_phase: Phase | None = None
    done: tuple[str, ...] = ()
    gate_lines: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bullets(lines: Sequence[str], budget: int) -> str:
    """`- line` per line, in order, fitted to `budget` characters: as many as fit, then a
    single `- …and N more` line in place of the rest. Never empty-handed: the first line
    is always shown (clipped), so a reader always sees at least one reason."""
    shown: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        bullet = f"- {_clip(line, _LINE_BUDGET)}"
        cost = len(bullet) + (1 if shown else 0)
        remaining = len(lines) - index
        reserve = _MORE_RESERVE if remaining > 1 else 0
        if shown and used + cost + reserve > budget:
            shown.append(f"- …and {remaining} more")
            return "\n".join(shown)
        shown.append(bullet)
        used += cost
    return "\n".join(shown)


def _title_and_header(summary: PressSummary) -> tuple[str, str]:
    """`(title, the lead line of the description)`."""
    origin = summary.from_phase.value
    if summary.outcome is PressOutcome.ENTRY:
        target = summary.to_phase.value if summary.to_phase is not None else "?"
        return f"Advanced to {target}", f"**{origin}** -> **{target}**"
    if summary.outcome is PressOutcome.BLOCK:
        return f"Simulated a game day in {origin}", ""
    if summary.outcome is PressOutcome.BLOCKED:
        return f"Blocked in {origin}", "The league did not move."
    return "Draft resumed", ""


def build_press_panel(summary: PressSummary) -> Panel:
    title, header = _title_and_header(summary)
    description = header
    if summary.done:
        room = _DESCRIPTION_BUDGET - len(header) - 1
        bullets = _bullets(summary.done, room)
        description = f"{header}\n{bullets}" if header else bullets

    fields: list[PanelField] = []
    origin = summary.from_phase.value
    if summary.outcome is PressOutcome.BLOCKED:
        # A `Blocked` press with no reason is a service bug; say so rather than let an
        # empty field value make `PanelField` raise after the press committed.
        reasons = _bullets(summary.gate_lines, _FIELD_BUDGET) or "- No reason was recorded."
        fields.append(PanelField("What is in the way", reasons))
    elif summary.outcome in (PressOutcome.BLOCK, PressOutcome.DRAFT_RESUME):
        value = (
            _bullets(summary.gate_lines, _FIELD_BUDGET)
            if summary.gate_lines
            else "Nothing -- press /advance again to move on."
        )
        fields.append(PanelField(f"Before {origin} can be left", value))
    if summary.notices:
        fields.append(PanelField("Did not go out", _bullets(summary.notices, _FIELD_BUDGET)))

    if summary.outcome is PressOutcome.BLOCKED:
        color = PanelColor.NEGATIVE
    elif summary.notices:
        color = PanelColor.WARNING
    elif summary.outcome is PressOutcome.ENTRY:
        color = PanelColor.POSITIVE
    else:
        color = PanelColor.NEUTRAL
    return Panel(title=title, description=description, fields=tuple(fields), color=color)


@dataclass(frozen=True)
class AdvanceToSummary:
    """What `/advance-to` did (P5-99.f): the phases it entered in order, whether it reached
    `requested`, and -- when it did not -- why it stopped (`stop_reason`, a sentence) and
    what the exit gate said (`gate_lines`, when a gate refusal was the reason)."""

    from_phase: Phase
    requested: Phase
    entered: tuple[Phase, ...]
    reached: bool
    stop_reason: str | None = None
    gate_lines: tuple[str, ...] = ()


def _route(from_phase: Phase, entered: Sequence[Phase]) -> str:
    return " -> ".join(f"**{p.value}**" for p in (from_phase, *entered))


def build_advance_to_panel(summary: AdvanceToSummary) -> Panel:
    lines: list[str] = []
    if summary.reached and summary.entered:
        title = f"Advanced to {summary.requested.value}"
        lines.append(_route(summary.from_phase, summary.entered))
    elif summary.reached:
        title = f"Already in {summary.requested.value}"
        lines.append("Nothing to do.")
    else:
        title = f"Stopped short of {summary.requested.value}"
        lines.append(
            _route(summary.from_phase, summary.entered)
            if summary.entered
            else "The league did not move."
        )
        if summary.stop_reason:
            lines.append(_clip(summary.stop_reason, _LINE_BUDGET * 2))
    fields: tuple[PanelField, ...] = ()
    if summary.gate_lines:
        fields = (PanelField("What is in the way", _bullets(summary.gate_lines, _FIELD_BUDGET)),)
    color = PanelColor.POSITIVE if summary.reached and summary.entered else PanelColor.NEUTRAL
    return Panel(title=title, description="\n".join(lines), fields=fields, color=color)


@dataclass(frozen=True)
class ForceSummary:
    """What `/advance-force` did (P5-99.g): a pointer move that names every blocker it
    overrode and every unit of work it walked away from -- the same two lists the audit
    row carries."""

    from_phase: Phase
    to_phase: Phase
    reason: str
    overrode: tuple[str, ...]
    skipped_work: tuple[str, ...]


_FORCE_FOOTER = (
    "No hook, phase work, sim or media ran. Work skipped here is skipped permanently: "
    "no later press revisits a passed entry."
)


def build_force_panel(summary: ForceSummary) -> Panel:
    lines = [
        f"**{summary.from_phase.value}** -> **{summary.to_phase.value}** (forced)",
        f"Reason: {_clip(summary.reason, _LINE_BUDGET)}",
    ]
    fields = (
        PanelField("Blockers it overrode", _bullets(summary.overrode, _FIELD_BUDGET) or "None."),
        PanelField("Work it skipped", _bullets(summary.skipped_work, _FIELD_BUDGET) or "None."),
    )
    return Panel(
        title=f"Forced {summary.from_phase.value} -> {summary.to_phase.value}",
        description="\n".join(lines),
        fields=fields,
        color=PanelColor.WARNING,
        footer=_FORCE_FOOTER,
    )


# --- /advance-run (docs/design/advance-run.md AR-8, AR-9) ---------------------------------------


class RunStopKind(StrEnum):
    """The eleven ways a run ends, restated here because this package cannot import the
    service's `RunStop` (P5-3). The cog maps one to the other 1:1 and pins the map at import."""

    TARGET_REACHED = "target_reached"
    TARGET_PASSED = "target_passed"
    BLOCKED = "blocked"
    NEEDS_A_DRAFT_BOARD = "needs_a_draft_board"
    SEASON_OVER = "season_over"
    NEEDS_A_HUMAN_PICK = "needs_a_human_pick"
    DRAFT_STALLED = "draft_stalled"
    PRESS_FAILED = "press_failed"
    CANCELLED = "cancelled"
    CAP_REACHED = "cap_reached"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True)
class PhaseAiCounts:
    """What the AI did in one phase during the run (R2's audit trail): counts only, the
    transaction log holds the detail."""

    phase: Phase
    trades: int = 0
    signings: int = 0
    waiver_cuts: int = 0
    resign_offers: int = 0


@dataclass(frozen=True)
class RunSummary:
    """What a whole run did, as plain values -- the one input of the panel and the `#games`
    digest, so the two cannot disagree (AR-9).

    **Nothing in it is a score.** The digest is built from this alone, and `reveal_game` stays
    the only publisher of a score to `#games`; counts, dates and phase names are all it carries.

    `blockers` are the exit gate's messages verbatim (rows 2 and 4); `on_the_clock` is the
    sentence naming the pick a human owes (row 5); `stall_reason` is a `DraftCycleReason`'s
    value (row 6); `error_message` is what a failed press said, already worded for a person
    (row 7). `notices` are the Discord-side steps that did not go out."""

    stop: RunStopKind
    from_phase: Phase
    target: Phase
    final_phase: Phase
    presses: int
    elapsed_seconds: float
    phases_entered: tuple[Phase, ...] = ()
    presses_by_kind: tuple[tuple[str, int], ...] = ()
    game_days: int = 0
    games: int = 0
    first_game_date: str | None = None
    last_game_date: str | None = None
    ai_counts: tuple[PhaseAiCounts, ...] = ()
    expired_offers: int = 0
    media_cycles: int = 0
    wire_stories: int = 0
    failed_games: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    on_the_clock: str | None = None
    stall_reason: str | None = None
    error_message: str | None = None
    notices: tuple[str, ...] = ()


_NEEDS_A_DRAFT_BOARD_SENTENCE = (
    "The draft has no board, and nothing the bot does can create one: run "
    "tools/dev_league_draft.py, then /advance-run again."
)

STALL_REASONS = {
    "pool_exhausted": "the class has nobody left for an AI pick",
    "unresolved_picks": "the lottery has not given every pick a number",
    "claim_held": "another command held the league",
    "phase_moved": "the league is no longer in the draft",
    "resume_contended": "the draft lost a wait for a lock",
    "resume_failed": "the draft's AI pass failed (error logged)",
}
"""One phrase per `DraftCycleReason`, keyed by its value (a unit test pins that every member
has one). `SKIPPED` cycles carry the last four; a run never meets them (`run_draft_cycle` never
produces `SKIPPED`), and each is worded anyway so a stop never reads as a shrug."""


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def build_run_progress_line(
    *,
    phase: Phase,
    game_days: int,
    games: int,
    last_game_date: str | None,
    presses: int,
    elapsed_seconds: float,
) -> str:
    """The one line the deferred reply shows while a run goes (AR-8): where the league is, what
    the run has simmed so far, and how long it has taken. A date is shown only once a game
    has been simmed."""
    simmed = f"{_plural(game_days, 'game day')} simmed, {_plural(games, 'game')}"
    if last_game_date is not None:
        simmed += f", {last_game_date}"
    return (
        f"{phase.value} — {simmed}. "
        f"{_plural(presses, 'press', 'presses')}, {_duration(elapsed_seconds)}."
    )


def run_stop_sentence(summary: RunSummary) -> str:
    """The stop's own sentence (AR-5's last column). Every `RunStopKind` has one."""
    stop = summary.stop
    if stop is RunStopKind.TARGET_REACHED:
        if summary.presses == 0:
            return f"The league is already in {summary.target.value}."
        return f"The league is in {summary.target.value}."
    if stop is RunStopKind.BLOCKED:
        where = summary.final_phase.value
        return f"Stopped in {where}: the exit gate says the league cannot move on yet."
    if stop is RunStopKind.NEEDS_A_DRAFT_BOARD:
        return _NEEDS_A_DRAFT_BOARD_SENTENCE
    if stop is RunStopKind.SEASON_OVER:
        return " ".join(summary.blockers) or "The season is over: /advance-run stops at WAIVERS."
    if stop is RunStopKind.NEEDS_A_HUMAN_PICK:
        who = summary.on_the_clock or "a person"
        return (
            f"The draft is waiting on {who}: that pick is made with /draft, then run "
            "/advance-run again."
        )
    if stop is RunStopKind.DRAFT_STALLED:
        why = STALL_REASONS.get(summary.stall_reason or "", summary.stall_reason or "no reason")
        return f"The draft cannot continue: {why}."
    if stop is RunStopKind.PRESS_FAILED:
        return f"A press failed, so the run stopped: {summary.error_message or 'error logged'}"
    if stop is RunStopKind.CANCELLED:
        return "Stopped by /advance-run-stop."
    if stop is RunStopKind.CAP_REACHED:
        return (
            f"Stopped after {_plural(summary.presses, 'press', 'presses')} / "
            f"{_duration(summary.elapsed_seconds)} — run /advance-run {summary.target.value} "
            "again to continue."
        )
    if stop is RunStopKind.TARGET_PASSED:
        return (
            f"The league is now in {summary.final_phase.value}, past {summary.target.value} "
            "(another command moved it while the run was going), so the run stopped rather "
            "than press on."
        )
    return "Two presses in a row changed nothing; stopping so this does not spin."


def _simmed_line(summary: RunSummary) -> str | None:
    if not summary.game_days:
        return None
    span = ""
    if summary.first_game_date and summary.last_game_date:
        span = (
            f", {summary.first_game_date} → {summary.last_game_date}"
            if summary.first_game_date != summary.last_game_date
            else f", {summary.first_game_date}"
        )
    days = _plural(summary.game_days, "game day")
    return f"Simmed {days}{span} ({_plural(summary.games, 'game')})."


def _ai_lines(counts: Sequence[PhaseAiCounts]) -> list[str]:
    lines = []
    for c in counts:
        parts = [
            f"{n} {label}"
            for n, label in (
                (c.trades, "trade(s)"),
                (c.signings, "signing(s)"),
                (c.waiver_cuts, "waiver cut(s)"),
                (c.resign_offers, "re-sign offer(s)"),
            )
            if n
        ]
        lines.append(f"{c.phase.value}: {', '.join(parts)}")
    return lines


def _what_ran_lines(summary: RunSummary) -> list[str]:
    """The run's facts as sentences, in the order the panel and the digest share."""
    lines: list[str] = []
    simmed = _simmed_line(summary)
    if simmed is not None:
        lines.append(simmed)
    if summary.phases_entered:
        lines.append("Entered: " + " → ".join(p.value for p in summary.phases_entered) + ".")
    kinds = ", ".join(f"{n} {name}" for name, n in summary.presses_by_kind if n)
    lines.append(
        f"{_plural(summary.presses, 'press', 'presses')} in {_duration(summary.elapsed_seconds)}"
        + (f" ({kinds})." if kinds else ".")
    )
    extras = []
    if summary.media_cycles:
        extras.append(_plural(summary.media_cycles, "media cycle"))
    if summary.wire_stories:
        extras.append(_plural(summary.wire_stories, "wire story", "wire stories"))
    if summary.expired_offers:
        extras.append(_plural(summary.expired_offers, "stale trade offer") + " expired")
    if extras:
        lines.append(", ".join(extras).capitalize() + ".")
    return lines


# Six fields of 700 plus a 1200-character description, a title, a footer and the field names
# stay under the 6000-character combined ceiling (a unit test builds the worst case).
_RUN_DESCRIPTION_BUDGET = 1200
_RUN_FIELD_BUDGET = 700
_DIGEST_LIMIT = 2000
_DIGEST_TAIL_BUDGET = 450
"""Room for each of the digest's two verbatim sections (blockers, notices): two of them plus
their headers leave the facts above at least ~1000 of the 2000 characters."""
_DIGEST_FOOTER = "Scores are in /standings and /box."
_RUN_FOOTER = (
    "Between two presses another command or a human write can land, exactly as between two "
    "manual /advance presses."
)


def _run_color(summary: RunSummary) -> PanelColor:
    if summary.stop in (RunStopKind.PRESS_FAILED, RunStopKind.NO_PROGRESS):
        return PanelColor.NEGATIVE
    if summary.notices or summary.stop in (
        RunStopKind.TARGET_PASSED,
        RunStopKind.BLOCKED,
        RunStopKind.NEEDS_A_DRAFT_BOARD,
        RunStopKind.DRAFT_STALLED,
        RunStopKind.NEEDS_A_HUMAN_PICK,
        RunStopKind.SEASON_OVER,
    ):
        return PanelColor.WARNING
    if summary.stop is RunStopKind.TARGET_REACHED:
        return PanelColor.POSITIVE
    return PanelColor.NEUTRAL


def build_run_panel(summary: RunSummary) -> Panel:
    """The reply a run ends with (AR-9). Fitted to budgets the way `build_press_panel` is:
    this is built AFTER hundreds of presses have committed, so a `Panel` that raised over an
    embed limit would cost the commissioner the whole record."""
    if summary.stop is RunStopKind.TARGET_REACHED and summary.presses:
        title = f"Ran to {summary.target.value}"
    elif summary.stop is RunStopKind.TARGET_REACHED:
        title = f"Already in {summary.target.value}"
    else:
        title = f"Run to {summary.target.value} stopped in {summary.final_phase.value}"
    lines = (run_stop_sentence(summary), *_what_ran_lines(summary))
    description = _clip(
        "\n".join(_clip(line, _LINE_BUDGET) for line in lines), _RUN_DESCRIPTION_BUDGET
    )

    fields: list[PanelField] = []
    ai = _ai_lines(summary.ai_counts)
    if ai:
        fields.append(PanelField("AI activity", _bullets(ai, _RUN_FIELD_BUDGET)))
    if summary.failed_games:
        failed = _bullets(summary.failed_games, _RUN_FIELD_BUDGET)
        fields.append(PanelField("Games that failed to sim", failed))
    if summary.blockers and summary.stop in (RunStopKind.BLOCKED, RunStopKind.NEEDS_A_DRAFT_BOARD):
        fields.append(
            PanelField("What is in the way", _bullets(summary.blockers, _RUN_FIELD_BUDGET))
        )
    if summary.notices:
        fields.append(PanelField("Did not go out", _bullets(summary.notices, _RUN_FIELD_BUDGET)))
    return Panel(
        title=_clip(title, 250),
        description=description,
        fields=tuple(fields),
        color=_run_color(summary),
        footer=_RUN_FOOTER,
    )


def build_run_digest(summary: RunSummary) -> str:
    """The ONE durable `#games` message of a run (AR-7, AR-9): the summary body, no score.
    Built from `RunSummary` alone -- which carries none -- so this cannot become a second
    publisher of a result; it is sent with `send_line`, never through `reveal_game`."""
    head = f"**/advance-run {summary.target.value}** — {run_stop_sentence(summary)}"
    lines = [head, *_what_ran_lines(summary)]
    ai = _ai_lines(summary.ai_counts)
    if ai:
        lines.append("AI activity: " + "; ".join(ai) + ".")
    if summary.failed_games:
        lines.append(
            f"{_plural(len(summary.failed_games), 'game')} failed to sim and stay scheduled."
        )
    body = "\n".join(_clip(line, _LINE_BUDGET) for line in lines)
    # The digest is the durable copy (R5), so what stopped the run and what did not go out must
    # survive the clip: they get reserved room and the facts above are clipped to what is left.
    # Blocker and notice text is the exit gate's and the cog's own words -- never a score.
    tail: list[str] = []
    if summary.blockers and summary.stop in (
        RunStopKind.BLOCKED,
        RunStopKind.NEEDS_A_DRAFT_BOARD,
    ):
        tail.append("What is in the way:\n" + _bullets(summary.blockers, _DIGEST_TAIL_BUDGET))
    if summary.notices:
        tail.append("Did not go out:\n" + _bullets(summary.notices, _DIGEST_TAIL_BUDGET))
    tail_text = "".join(f"\n{section}" for section in tail)
    room = _DIGEST_LIMIT - len(_DIGEST_FOOTER) - 1 - len(tail_text)
    return f"{_clip(body, room)}{tail_text}\n{_DIGEST_FOOTER}"
