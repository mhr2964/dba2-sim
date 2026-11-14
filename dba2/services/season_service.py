"""Stage E's season orchestrator (docs/design/phase-5-stage-e-advance.md).

The claim writer and its fenced release, the two counters' pure derivations,
the exit gate, the press core, the phase work with its two completion checkpoints,
the block press with its failed-game policy, the commissioner's postpone/restore and
the media cycle, JC-13's delayed first trade window, and the draft branch (its pre-entry
board check and its resume press) with `/advance-unlock`'s service side exist so far
(build-plan commits 3, 5, 7, 8, 9, 10, 11, 12 and 13), and `press`'s optional `on_progress`
milestones (commit 14; `_report_progress` is the one place the callback is awaited).
`advance_to` and `force` (commit 15) are the two claim-taking pointer moves that are not a press.

**The claim is the whole press's mutual exclusion against human writes**
(P5-99.c, P5-99.t): while `leagues.advance_claimed_at` is set, every
human-initiated write that reads it (`league_claim.check_claim`) refuses. The
claim therefore has to be taken and released in transactions of its own, never
inside the press's other work -- a claim that committed only with a later
statement would be invisible to the writers it exists to stop.

**Every transaction this module opens begins with `roster_lock.set_lock_timeout`**
and takes no league gate: each one takes the `leagues` row lock, and the server
default is wait-forever. `tests/unit/test_roster_mutation_lock_discipline.py`
pins this per function (check 10, for the functions listed there) and for the
whole module (its module-scoped variant, which is what covers every later
private writer without a list to keep in sync).

Imports nothing from `dba2.bot` (services allowlist, P5-99.m): the cog makes
every Discord call.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import asyncpg

from dba2.core.errors import ConcurrencyError, NotFoundError, PhaseError, ValidationError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import (
    draft_repo,
    game_repo,
    league_repo,
    media_repo,
    offer_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.draft import DraftPick
from dba2.domain.game import Game, GameStatus, GameType
from dba2.domain.league import League, LeagueClaimState
from dba2.domain.phase import Phase
from dba2.domain.transaction import TransactionType
from dba2.phase import entity_states, permissions
from dba2.phase import service as phase_service
from dba2.phase.graph import _DEFAULT_SUCCESSOR, _NO_OP_PHASES, NEXT_PHASES, route_to
from dba2.services import (
    agency_service,
    game_sim_service,
    gm_service,
    league_claim,
    media_service,
    role_service,
    roster_lock,
    schedule_service,
    stats_service,
    strategy_service,
)
from dba2.services.agency_service import CheckpointResult
from dba2.services.game_sim_service import GameFailure, SimmedGame
from dba2.services.gm_service import (
    DraftCycleReason,
    DraftCycleState,
    PhaseHookResult,
    draft_completion_state,
)
from dba2.services.media_service import LoadedMediaLibrary

log = logging.getLogger(__name__)

# The nine phases that get an ordinal, a hook call and a media cycle -- the
# parent's cycle-bearing table (P5-99.b.3). The other ten never do (F15).
# `tests/unit/services/test_season_service_derivations.py` holds an independent,
# hand-written copy and compares.
CYCLE_BEARING_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.PRESEASON,
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.DRAFT_LOTTERY,
        Phase.DRAFT,
        Phase.RESIGN_WINDOW,
        Phase.FREE_AGENCY,
        Phase.WAIVERS,
    }
)

# The four phases that consume the regular-season schedule, each with the
# fraction of it that must be played before the phase may be left (P5-99.b.3's
# "games target" column; the media cycle for the three cycle-bearing ones and the
# stats rebuild for `REGULAR_SEASON_COMPLETE` hang off the same targets).
# `tests/unit/services/test_season_service_exit_gate.py` holds an independent,
# hand-written copy and compares. The other sixteen phases have no target that can be unmet
# (`SETUP`/`PRESEASON` come before the schedule is played, the rest after it), so they are not
# keys: nothing reads a target for them.
GAMES_TARGET_FRACTION: dict[Phase, float] = {
    Phase.REGULAR_SEASON: 0.45,
    Phase.TRADE_DEADLINE: 0.60,
    Phase.POST_DEADLINE: 0.75,
    Phase.REGULAR_SEASON_COMPLETE: 1.0,
}
SCHEDULE_CONSUMING_PHASES: frozenset[Phase] = frozenset(GAMES_TARGET_FRACTION)

# JC-13 (P5-99.k): the AI's first trade window is held at the first block press whose
# sim has played this fraction OF `REGULAR_SEASON`'s OWN games target -- 0.25 of 0.45,
# about 11% of the schedule -- rather than on the Entry press, which is before a single
# game has been simmed. A fraction of the target, not of the season, so the threshold
# can never lie past the phase's own target. Pure taste (spec section 8, call 4): a
# module constant beside the targets it scales, because `core.config.Config` is the
# environment's (database URL, token, log level) and carries no tunable of this kind.
FIRST_TRADE_WINDOW_GAMES_FRACTION = 0.25

WAIVERS_EDGE_MESSAGE = (
    "WAIVERS -> PRESEASON crosses the season boundary, which is Phase 7's rollover "
    "and is not built: /advance walks one season and stops here (P5-70, P5-84)."
)

CLAIM_CONTENTION_MESSAGE = (
    "This league is busy with another action right now -- try /advance again in a moment."
)

# `/advance-unlock`'s own: the person just ran it, so the sentence names it (the claim
# contention message above says to try `/advance`, which is the wrong command here).
UNLOCK_CONTENTION_MESSAGE = (
    "This league is busy with another action right now, so the advance claim could not be "
    "read -- try /advance-unlock again in a moment."
)


def cap_settings_missing_message(phase: Phase, season: int) -> str:
    return (
        f"{phase.value}'s agency checkpoint needs a league_cap_settings row for season {season}, "
        "and this league has none: there is no cap-settings row to evaluate players against. "
        "Add one for that season and press /advance again -- the checkpoint runs then, before "
        "anything else."
    )


# The two counters of P5-99.d.1, derived from two `count_phase_entries` counts
# taken under the same row lock. They are different numbers for most of a
# season and sit in adjacent columns, so each has its own derivation and the
# names `entry_index` and `cycle_ordinal` belong to their VALUES only: never
# assign one to the other's name, and never name a function the same as the
# value it returns (a local `cycle_ordinal = cycle_ordinal(...)` would shadow it).


NO_ENTRY_TO_READ_MESSAGE = (
    "This league has no phase_history row for the phase it is in this season, so /advance "
    "cannot tell which entry of the season it is inside. That happens to a league placed at a "
    "mid-season phase by hand, or carried into a new season before Phase 7's rollover exists "
    "(it is not built): walk the league through the phase graph so its history is real."
)


def derive_entry_index(entries_counted: int, *, entering: bool) -> int:
    """The number of `phase_history` rows for this season before the entry
    under discussion (P5-99.d.1). `entering=True`: the count was taken before
    the entry was written, so it IS the index. `entering=False` (a Block press,
    which writes no row): the entry already exists and is counted, so the index
    is one less. Every phase entry has one, cycle-bearing or not.

    A count of zero with `entering=False` is a league whose history does not
    contain the entry it stands in -- reachable without any bug of ours (a league
    placed mid-season by hand; a new season before Phase 7's rollover), so it is a
    `PhaseError` a person can read, not a `ValueError` the cog cannot map."""
    if entering:
        return entries_counted
    if entries_counted < 1:
        raise PhaseError(NO_ENTRY_TO_READ_MESSAGE)
    return entries_counted - 1


def derive_cycle_ordinal(phase: Phase, cycle_bearing_counted: int, *, entering: bool) -> int | None:
    """The ordinal of the cycle `phase` belongs to, or `None` when `phase` is not
    cycle-bearing (P5-99.d.1, F15). `cycle_bearing_counted` is the
    `count_phase_entries` count filtered to `CYCLE_BEARING_PHASES`; `entering`
    means the same as in `derive_entry_index`.

    `None`, not "the count so far": at a non-cycle-bearing entry that count is
    the NEXT cycle's ordinal, and a hook checkpoint written under it would make
    that cycle's hook look already run."""
    if phase not in CYCLE_BEARING_PHASES:
        return None
    if entering:
        return cycle_bearing_counted
    if cycle_bearing_counted < 1:
        raise PhaseError(NO_ENTRY_TO_READ_MESSAGE)
    return cycle_bearing_counted - 1


async def _claim_league(pool: asyncpg.Pool, league_id: int, claimed_by: int) -> datetime:
    """Takes the advance claim (P5-99.c tx0) and returns the timestamp it was
    written with, which `_release_claim` needs as its fence.

    Its own transaction, `set_lock_timeout` first -- the statement
    `phase/service.advance` never had, the unbounded wait P5-90 named and OS-9
    assigned here. A 55P03 from the `UPDATE` is mapped at the statement to a
    league-noun `ConcurrencyError` (OS-13's residue, closed for the one path
    OS-13 predicted); the generic entity message names a team or a player.

    Zero rows means the claim is held: refuse, naming the holder and how long
    they have held it. That sentence comes from an unlocked read taken AFTER the
    claim transaction ended -- it is prose for a person, not a decision, so it
    must not take a lock the holder's own release would then wait behind.
    """
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            claimed_at = await league_repo.claim_advance(conn, league_id, claimed_by)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc
    if claimed_at is not None:
        return claimed_at
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    holder, since = league.advance_claimed_by, league.advance_claimed_at
    raise ConcurrencyError(_claim_held_message(holder, since))


def _claim_held_message(claimed_by: int | None, claimed_at: datetime | None) -> str:
    if claimed_by is None or claimed_at is None:
        # Released between our failed UPDATE and this read: nothing to name.
        return "An advance was in progress a moment ago -- try /advance again."
    age_minutes = max(0, int((datetime.now(UTC) - claimed_at).total_seconds() // 60))
    return (
        f"An advance is already running for this league (claimed by user {claimed_by}, "
        f"{age_minutes} minute(s) ago). If that press died, /advance-unlock clears the claim "
        "once it is old enough."
    )


async def _release_claim(
    pool: asyncpg.Pool, league_id: int, claimed_by: int, claimed_at: datetime
) -> bool:
    """Releases the claim `(claimed_by, claimed_at)` and reports whether it did.

    **Fenced:** the update matches the claimant AND the exact timestamp, so a
    press whose claim `/advance-unlock` broke -- and which another press has
    since re-taken -- affects zero rows instead of clearing the new holder's
    live claim. Zero rows is logged, never raised.

    **Exception-safe by construction, because it runs in the press's
    `finally`:** a release that raised would replace the press's real exception
    with its own. Everything, including the timeout `set_lock_timeout` bounds
    the update with, is caught and logged with its traceback; a release that
    could not run leaves a claim `/advance-unlock` clears (R4).
    """
    try:
        async with transaction(pool) as conn:
            await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
            released = await league_repo.release_advance(conn, league_id, claimed_by, claimed_at)
    except Exception:
        log.exception(
            "season_service._release_claim: could not release league_id=%s claim held by "
            "user %s since %s; it stays until /advance-unlock clears it",
            league_id,
            claimed_by,
            claimed_at,
        )
        return False
    if not released:
        log.warning(
            "season_service._release_claim: league_id=%s claim by user %s (%s) is no longer "
            "ours -- broken by /advance-unlock or replaced; left untouched",
            league_id,
            claimed_by,
            claimed_at,
        )
    return released


# --- the exit gate (P5-99.e) ---------------------------------------------------
#
# `load_gate_facts` (impure, one read per fact) and `evaluate_exit` (pure) are two
# functions so the verdict can be driven over hand-built facts, one row at a time,
# and so a blocker cannot depend on anything the loader did not name. The verdict
# names EVERY blocker, not the first.


def games_target_index(fraction: float, max_game_index: int) -> int:
    """The highest `game_index` a phase's games target covers: the season's last
    index scaled by `fraction`, capped at the last index. The formula
    `tools/generate_media_corpus.py` sims with (`through = min(max_game_index,
    round(max_game_index * progress))`), so a press and the measured corpus agree
    on where a cycle ends. Only the four schedule-consuming phases have a target
    here; a target of 0.0 (`PRESEASON`) is met by definition and never reaches
    this arithmetic."""
    return min(max_game_index, round(max_game_index * fraction))


def first_trade_window_target_index(max_game_index: int) -> int:
    """The highest `game_index` that must be played before `REGULAR_SEASON`'s hook is
    due (JC-13): `round(0.25 * 0.45 * max_game_index)` -- `FIRST_TRADE_WINDOW_GAMES_FRACTION`
    of the phase's own target, expressed as a fraction of the SEASON's last index (0.1125 of
    it) and rounded exactly as the target itself is (`games_target_index`), so it is never
    above the target index."""
    return games_target_index(
        FIRST_TRADE_WINDOW_GAMES_FRACTION * GAMES_TARGET_FRACTION[Phase.REGULAR_SEASON],
        max_game_index,
    )


class BlockerCode(StrEnum):
    # A schedule-consuming phase in a season with no regular-season game at all: there is
    # nothing to reach a target WITH, so the target is not met -- never "nothing to wait for".
    SCHEDULE_MISSING = "schedule_missing"
    GAMES_TARGET_NOT_REACHED = "games_target_not_reached"
    REGULAR_SEASON_UNFINISHED = "regular_season_unfinished"
    DRAFT_NOT_COMPLETE = "draft_not_complete"
    WAIVERS_EDGE = "waivers_edge"
    # Not an exit blocker: the destination check of the `DRAFT_LOTTERY -> DRAFT` edge
    # (P5-99.h), reported through the same `Blocker` shape so a `Blocked` press names it
    # the way it names every other reason.
    DRAFT_BOARD_NOT_READY = "draft_board_not_ready"


@dataclass(frozen=True)
class Blocker:
    """One reason a phase may not be left. `game_ids` carries the blocking games
    in full for the schedule blockers (a report may truncate its sentence; the
    data is not truncated)."""

    code: BlockerCode
    message: str
    game_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ExitVerdict:
    allowed: bool
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class GateGame:
    """A regular-season game still `scheduled`: all the gate needs of a game."""

    game_id: int
    game_index: int


@dataclass(frozen=True)
class GateFacts:
    """Everything `evaluate_exit` may read, loaded for ONE phase: the loader
    fills only the fields that phase's row of P5-99.e consults and leaves the
    rest at their defaults, and `evaluate_exit` reads only the fields its phase's
    row names -- so a stray fact can never invent a blocker.

    `max_game_index` is the highest `game_index` of ANY regular-season game
    whatever its status (`None`: no schedule -- which a schedule-consuming phase
    reports as `SCHEDULE_MISSING`), so a postponement does not move a target.
    `scheduled_games` is the ones still `scheduled` that the phase's row reads: at or
    below the phase's own target index for the target blocker, and at
    `REGULAR_SEASON_COMPLETE` **every** scheduled game whatever the target, because
    its "the regular season is not finished" blocker is an absolute statement that must
    not depend on the arithmetic (the loader reads the rest for no other phase).
    `draft_picks` and `self_managed_team_ids` are what `draft_completion_state`
    takes."""

    phase: Phase
    max_game_index: int | None = None
    scheduled_games: tuple[GateGame, ...] = ()
    draft_picks: tuple[DraftPick, ...] = ()
    self_managed_team_ids: frozenset[int] = frozenset()
    """Every team id whose `manager_user_id is not None` and `not
    auto_pilot` (GM-2, docs/design/gm-command-surface.md) -- the same
    two-part predicate `gm_service.load_gm_inputs` derives, kept as this
    module's own separately-loaded copy per `GateFacts`'s own "loaded for
    ONE phase" contract above."""


# A sentence lists at most this many ids; `Blocker.game_ids` keeps them all.
_IDS_SHOWN = 10


def _game_ids_sentence(game_ids: tuple[int, ...]) -> str:
    shown = ", ".join(str(i) for i in game_ids[:_IDS_SHOWN])
    if len(game_ids) > _IDS_SHOWN:
        shown += f" and {len(game_ids) - _IDS_SHOWN} more"
    return shown


def _schedule_blockers(facts: GateFacts) -> list[Blocker]:
    if facts.max_game_index is None:
        return [
            Blocker(
                code=BlockerCode.SCHEDULE_MISSING,
                message=(
                    f"{facts.phase.value} plays the regular-season schedule and this season has "
                    "none: no game exists to reach its games target (PRESEASON's entry work "
                    "generates the schedule)."
                ),
            )
        ]
    blockers: list[Blocker] = []
    target = games_target_index(GAMES_TARGET_FRACTION[facts.phase], facts.max_game_index)
    due = tuple(
        g.game_id
        for g in sorted(facts.scheduled_games, key=lambda g: (g.game_index, g.game_id))
        if g.game_index <= target
    )
    if due:
        blockers.append(
            Blocker(
                code=BlockerCode.GAMES_TARGET_NOT_REACHED,
                message=(
                    f"{len(due)} game(s) this phase must play before it can be left are "
                    f"not played yet: game id(s) {_game_ids_sentence(due)}."
                ),
                game_ids=due,
            )
        )
    if facts.phase is Phase.REGULAR_SEASON_COMPLETE and facts.scheduled_games:
        # Stated absolutely, not inferred from the arithmetic target above: the
        # regular season is over only when no regular-season game is scheduled, and the
        # loader gives this phase every scheduled game, not the ones under the target.
        remaining = tuple(
            g.game_id
            for g in sorted(facts.scheduled_games, key=lambda g: (g.game_index, g.game_id))
        )
        blockers.append(
            Blocker(
                code=BlockerCode.REGULAR_SEASON_UNFINISHED,
                message=(
                    f"the regular season is not finished: {len(remaining)} game(s) are still "
                    f"scheduled: game id(s) {_game_ids_sentence(remaining)}."
                ),
                game_ids=remaining,
            )
        )
    return blockers


def _draft_blockers(facts: GateFacts) -> list[Blocker]:
    # GM-2 (docs/design/gm-command-surface.md, 2.2): `self_managed_team_ids`
    # now excludes an auto-piloted team, so this changes which of the two
    # sentences below renders for it -- never whether the gate blocks.
    # `draft_completion_state` reaches COMPLETE on a path that never
    # consults this set at all; what actually unblocks the gate for an
    # auto-piloted team is `execute_draft`'s halt reading the SAME
    # extended predicate (`gm_service.py`), a separate site.
    completion = draft_completion_state(facts.draft_picks, facts.self_managed_team_ids)
    if completion.state is DraftCycleState.COMPLETE:
        return []
    pick = completion.pick
    if completion.state is DraftCycleState.WAITING_ON_HUMAN and pick is not None:
        why = (
            f"team {pick.current_team_id} (managed by a person) is on the clock at pick "
            f"{pick.pick_number}, round {pick.round}"
        )
    elif completion.reason is DraftCycleReason.POOL_EXHAUSTED and pick is not None:
        why = (
            f"team {pick.current_team_id} is on the clock at pick {pick.pick_number} and the AI "
            "could not draft anyone: the prospect pool is exhausted"
        )
    elif completion.reason is DraftCycleReason.UNRESOLVED_PICKS:
        why = "picks are still owed but the lottery has not resolved their order (unresolved picks)"
    else:
        why = f"the board is {completion.state.value}"
    return [
        Blocker(code=BlockerCode.DRAFT_NOT_COMPLETE, message=f"the draft is not finished: {why}.")
    ]


def evaluate_exit(facts: GateFacts) -> ExitVerdict:
    """The exit gate as a pure function of loaded facts (P5-99.e). Which rows
    apply is decided by `facts.phase` alone; every other phase has no blocker,
    stated positively."""
    blockers: list[Blocker] = []
    if facts.phase in SCHEDULE_CONSUMING_PHASES:
        blockers.extend(_schedule_blockers(facts))
    elif facts.phase is Phase.DRAFT:
        blockers.extend(_draft_blockers(facts))
    elif facts.phase is Phase.WAIVERS:
        blockers.append(Blocker(code=BlockerCode.WAIVERS_EDGE, message=WAIVERS_EDGE_MESSAGE))
    return ExitVerdict(allowed=not blockers, blockers=tuple(blockers))


async def load_gate_facts(ex: Executor, league: League) -> GateFacts:
    """Reads the facts `evaluate_exit` needs for `league`'s CURRENT phase and
    nothing else -- one read per fact, and no read at all for a phase whose row
    consults none.

    The schedule phases read an aggregate (the season's highest regular-season
    `game_index`) and a projection of the scheduled games up to the phase's own
    target -- not the whole season's `Game` rows -- except `REGULAR_SEASON_COMPLETE`,
    which reads every scheduled game: its "the regular season is not finished" blocker
    is absolute, and a loader that narrowed to the target would make it depend on the
    arithmetic (it would silently weaken the day the phase's fraction is tuned below
    1.0). The gate is evaluated on EVERY
    press and a block press is one of ~144 a season, so what it costs per press is
    what a season of game-days costs (regular-season only: the play-in and playoff
    games are Phase 6's). `DRAFT` reads the board and takes the self-managed set
    from `team_repo.list_teams_for_league` -- one query for the league's rows,
    each carrying `manager_user_id` and `auto_pilot`, the same two-part
    derivation `load_gm_inputs` makes (`teams` -> `manager_user_id is not None
    and not auto_pilot`, GM-2, docs/design/gm-command-surface.md) without its
    eleven round trips. This site changes only which SENTENCE `_draft_blockers`
    renders for an auto-piloted team on the clock, never whether the exit gate
    blocks -- see that function's own comment."""
    phase = league.current_phase
    if phase in SCHEDULE_CONSUMING_PHASES:
        season = league.current_season
        max_game_index = await game_repo.get_max_game_index(ex, league.id, season, GameType.REGULAR)
        if max_game_index is None:
            return GateFacts(phase=phase)
        through_index = (
            max_game_index
            if phase is Phase.REGULAR_SEASON_COMPLETE
            else games_target_index(GAMES_TARGET_FRACTION[phase], max_game_index)
        )
        keys = await game_repo.list_scheduled_game_keys(
            ex, league.id, season, GameType.REGULAR, through_game_index=through_index
        )
        return GateFacts(
            phase=phase,
            max_game_index=max_game_index,
            scheduled_games=tuple(GateGame(game_id=i, game_index=n) for i, n in keys),
        )
    if phase is Phase.DRAFT:
        picks = await draft_repo.list_draft_picks_for_season(ex, league.id, league.current_season)
        teams = await team_repo.list_teams_for_league(ex, league.id)
        return GateFacts(
            phase=phase,
            draft_picks=tuple(picks),
            self_managed_team_ids=frozenset(
                t.id for t in teams if t.manager_user_id is not None and not t.auto_pilot
            ),
        )
    return GateFacts(phase=phase)


# --- the pre-entry board check (P5-99.h) -----------------------------------------
#
# The `DRAFT_LOTTERY -> DRAFT` edge guards its DESTINATION, so it is not an exit blocker
# and is not `evaluate_exit`'s: `evaluate_exit` is about leaving the phase the league is
# in, and nothing under `dba2/` can create a board (F1), so a league that entered `DRAFT`
# without one could never leave it. Same split as the gate: a pure verdict over loaded
# facts, and a loader that reads one fact each.


@dataclass(frozen=True)
class DraftBoardFacts:
    """The three preconditions of entering `DRAFT`, each independently: a `drafts` row
    for the season, at least one `draft_picks` row with a resolved `pick_number`, and at
    least one available prospect (`draft_repo.list_available_prospects`)."""

    has_draft_row: bool
    resolved_picks: int
    has_available_prospect: bool


DRAFT_BOARD_HINT = (
    "tools/dev_league_draft.py is today's only way to create a draft board and class. "
    "Where a real league's class comes from (U1) and whether a drafted player lands on a "
    "roster (U2) are open product questions, so /advance does not build one."
)


def draft_board_blockers(facts: DraftBoardFacts) -> list[Blocker]:
    """One blocker naming EVERY missing precondition (never just the first: a person told
    "no draft" when the prospects are also absent fixes one and meets the other on the
    next press), or none when the board can be entered."""
    missing: list[str] = []
    if not facts.has_draft_row:
        missing.append("this season has no drafts row")
    if facts.resolved_picks < 1:
        missing.append("no draft pick has a resolved pick number")
    if not facts.has_available_prospect:
        missing.append("there is no available prospect to draft")
    if not missing:
        return []
    return [
        Blocker(
            code=BlockerCode.DRAFT_BOARD_NOT_READY,
            message=f"the draft cannot be entered yet: {'; '.join(missing)}. {DRAFT_BOARD_HINT}",
        )
    ]


async def load_draft_board_facts(ex: Executor, league: League) -> DraftBoardFacts:
    """Reads the three facts for `league`'s CURRENT season: the `drafts` row, the season's
    picks, and whether one prospect is available (`limit=1` -- existence, not the list)."""
    season = league.current_season
    draft = await draft_repo.get_draft_for_season(ex, league.id, season)
    picks = await draft_repo.list_draft_picks_for_season(ex, league.id, season)
    prospects = await draft_repo.list_available_prospects(
        ex, league_id=league.id, season=season, limit=1
    )
    return DraftBoardFacts(
        has_draft_row=draft is not None,
        resolved_picks=sum(1 for p in picks if p.pick_number is not None),
        has_available_prospect=bool(prospects),
    )


# --- the press (P5-99.c, P5-99.b.2) ----------------------------------------------
#
# Built up commit by commit: claim -> work prefix (entry work, hook, closing work of
# the CURRENT entry) -> then one of three kinds, derived first-match (P5-99.b.2):
#
#   row 3, BLOCK    a schedule-consuming phase whose own games target is not met:
#                   sim one game-day, and if that meets the target run the phase's
#                   closing work. Ordered AHEAD of the gate on purpose -- an unmet
#                   target IS the gate's schedule blocker, so checking the gate first
#                   would refuse every ordinary game-day press.
#   row 2, DRAFT_RESUME  a press in `DRAFT` with the board not `COMPLETE`: re-run the DRAFT
#                   hook and nothing else. Ordered AHEAD of the gate for the same reason
#                   as row 3 -- "not `COMPLETE`" is also the gate's own draft blocker, so a
#                   gate-first order would refuse every resume and strand every draft.
#   row 4/1, BLOCKED  the gate refuses (WAIVERS' Phase 7 edge).
#   row 5, BLOCKED    the gate allows `DRAFT_LOTTERY -> DRAFT` but the destination has no
#                   board (`draft_board_blockers`): refused, pointer unmoved.
#   rows 6/7, ENTRY   the gate allows: claim -> pointer -> offer sweep -> the entered
#                   phase's entry work -> hook -> hook checkpoint -> closing work.
#
# **The work prefix retries the CURRENT entry's hook** (P5-99.l's "after tx1, before
# the hook" row) as well as its entry and closing work: a press that died between
# moving the pointer and finishing the hook leaves `hook_completed_cycle_index <
# cycle_ordinal`, and the next press of ANY kind finishes it before doing anything
# else. It is the same routine an Entry press then runs against the phase it enters.
# **JC-13 changes the hook's due-ness at `REGULAR_SEASON` only** -- that one hook is
# held until the schedule has played `first_trade_window_target_index` -- and it is
# decided in `_run_hook_once` (`_hook_is_due`), the one place both the prefix and the
# Entry press ask. That is also what keeps the crash retry correct there: a press that
# died between the threshold block's sim and its hook finds the threshold played and
# the checkpoint behind, and the next press's prefix finishes the hook. The block press
# itself asks the same question after its sim (`_block_press`), because at
# `REGULAR_SEASON` the entry's own hook is what a block press is for.


class PressKind(StrEnum):
    BLOCKED = "blocked"
    ENTRY = "entry"
    BLOCK = "block"
    DRAFT_RESUME = "draft_resume"


def derive_press_kind(phase: Phase, verdict: ExitVerdict) -> PressKind:
    """P5-99.b.2's first-match derivation, as a pure function of the phase and the exit
    verdict the press just evaluated -- rows 1 to 4, then the Entry the gate allows. Row
    5 (the `DRAFT_LOTTERY -> DRAFT` destination check) needs the board, so it stays in
    `_press_claimed` after this returns `ENTRY`: it turns that Entry into a `BLOCKED`
    press before any pointer moves.

    **The order is part of the decision** (rows 2 and 3 precede row 4): "the draft is not
    `COMPLETE`" and "the games target is not met" are also exit blockers, so a gate-first
    order would refuse every game-day press and strand every draft.

    **Work state is not an input.** The work prefix runs before this is called and changes
    what the gate then reads, never the derivation; the signature -- a phase and a verdict --
    is the guarantee."""
    if phase is Phase.WAIVERS:
        return PressKind.BLOCKED  # row 1: the Phase 7 edge, whatever else is true
    if phase is Phase.DRAFT and _draft_unfinished(verdict.blockers):
        return PressKind.DRAFT_RESUME  # row 2
    if phase in SCHEDULE_CONSUMING_PHASES and _target_unmet(verdict.blockers):
        return PressKind.BLOCK  # row 3
    if not verdict.allowed:
        return PressKind.BLOCKED  # row 4
    return PressKind.ENTRY  # rows 6 and 7 (row 5 is the caller's, on this Entry)


class WorkKind(StrEnum):
    """The two kinds of phase work a press may owe (P5-99.b.1): work that belongs
    to entering a phase, and work that belongs to its games target being met.
    The hook is a third thing with its own checkpoint and is not a work kind."""

    ENTRY = "entry"
    CLOSING = "closing"


# One unit of phase work. Each unit owns its own transactions and is idempotent or
# guarded (P5-99.r.3), because a checkpoint is per ENTRY, not per unit: a crash after
# one unit committed re-enters the whole list. The one unit with a result worth
# reporting is the agency checkpoint's.
#
# A unit takes ONE `_WorkContext` rather than a growing positional list: the media
# cycle needs the entry's cycle ordinal and the live library, the rest need neither,
# and a signature that changes each time a unit needs something new is a signature
# every test double must change with it.


@dataclass(frozen=True)
class _WorkContext:
    """What a work unit is handed. `cycle_ordinal` is the entry's ordinal, `None` at a
    non-cycle-bearing phase (F15) -- only the media cycle reads it, and it exists only
    where a media cycle does. `library` is the process's cached media library
    (P5-99.s.5): loaded once by the caller, never per press, and read only by the media
    cycle."""

    pool: Executor
    league_id: int
    season: int
    phase: Phase
    cycle_ordinal: int | None
    library: LoadedMediaLibrary


WorkUnit = Callable[[_WorkContext], Awaitable[CheckpointResult | None]]


async def _has_scheduled_games(ex: Executor, league_id: int, season: int) -> bool:
    return bool(await game_repo.list_games_for_league_season(ex, league_id, season))


async def _ensure_team_strategies(ctx: _WorkContext) -> CheckpointResult | None:
    """`PRESEASON` entry work, unit 1. Idempotent on its own (a second call writes 0
    rows), but games-guarded with its partner: once a schedule exists the entry work
    has run, and re-running strategy defaults over a season already in play would
    standardize against rosters that have since moved (P5-99.r)."""
    if not await _has_scheduled_games(ctx.pool, ctx.league_id, ctx.season):
        await strategy_service.ensure_defaults_for_season(ctx.pool, ctx.league_id, ctx.season)
    return None


async def _construct_ai_roles(ctx: _WorkContext) -> CheckpointResult | None:
    """`PRESEASON` entry work, unit 2 (RC-22.1, `docs/design/auto-gm-role-
    construction.md`, C11): the auto-GM role-construction pass. **Position
    in the tuple is load-bearing, not cosmetic** -- placed AFTER `_ensure_
    team_strategies` and BEFORE `_generate_schedule`. `_run_work_units`
    re-enters the WHOLE list on a crash, and both neighbours are
    idempotent under that re-entry only because their own `_has_scheduled_
    games` guard is still false; placing this unit AFTER `_generate_
    schedule` would let a crash between the two leave a schedule on disk,
    which makes the guard true, which would skip role construction
    permanently for that league-season -- silently, with a green
    checkpoint. Same guard as its two neighbours for the identical reason
    (P5-99.r: re-running over a season in play would rank against rosters
    that have since moved)."""
    if not await _has_scheduled_games(ctx.pool, ctx.league_id, ctx.season):
        await role_service.construct_ai_roles_for_league(
            ctx.pool, league_id=ctx.league_id, season=ctx.season
        )
    return None


async def _generate_schedule(ctx: _WorkContext) -> CheckpointResult | None:
    """`PRESEASON` entry work, unit 3. **The guard is load-bearing, not defensive:**
    `generate_regular_season` raises `ValidationError` on an existing schedule, and
    a crash between this unit and its checkpoint re-enters it on the next press."""
    if not await _has_scheduled_games(ctx.pool, ctx.league_id, ctx.season):
        await schedule_service.generate_regular_season(ctx.pool, ctx.league_id, ctx.season)
    return None


async def _require_cap_settings(ctx: _WorkContext) -> CheckpointResult | None:
    """The cap-settings assertion before each agency checkpoint: `load_gm_inputs`
    requires a persisted `league_cap_settings` row for `season`, and Stage E does not
    port the tools projection that fabricates one (a cross-season concern, Phase 7's).
    Production writes one at league creation for the creation season, the only season
    Phase 5 walks -- so its absence is refused by name, not by a `NotFoundError` from
    the middle of a checkpoint."""
    if await league_repo.get_cap_settings(ctx.pool, ctx.league_id, ctx.season) is None:
        raise PhaseError(cap_settings_missing_message(ctx.phase, ctx.season))
    return None


async def _agency_checkpoint(ctx: _WorkContext) -> CheckpointResult | None:
    """P4-11's checkpoint #1 (`TRADE_DEADLINE`) or #2 (`PROGRESSION`), under the
    entered phase's own name -- the per-player guard is keyed by it."""
    return await agency_service.run_agency_checkpoint(
        ctx.pool, league_id=ctx.league_id, season=ctx.season, phase=ctx.phase
    )


async def _rebuild_regular_season_stats(ctx: _WorkContext) -> CheckpointResult | None:
    """`REGULAR_SEASON_COMPLETE`'s closing work. Idempotent (upsert plus prune), so
    re-running it after a crash costs a recompute and nothing else."""
    await stats_service.rebuild_player_season_stats(
        ctx.pool, ctx.league_id, ctx.season, GameType.REGULAR
    )
    return None


async def _run_media_cycle(ctx: _WorkContext) -> CheckpointResult | None:
    """The closing work of the nine cycle-bearing phases (P5-99.s, P5-99.s.2): the
    pulse cycle, then the selection cycle over its own `candidates` (docs/design/
    media-v2-newsroom.md MV-1c.4 -- the selection cycle now scores them itself),
    keyed by the entry's cycle ordinal (`media_stories.cycle_index`, `media_pulse_
    snapshot`).

    **Two different crash states, never conflated into one "did it run?"** -- and the
    pulse snapshot is NOT the marker, because it lands BEFORE any story (F10): using it
    would permanently skip a cycle whose stories were never written. The marker is the
    entry's closing-work checkpoint, which the caller writes after this returns; what
    THIS function checks is the state a crash mid-selection leaves.

    - **stories exist for this ordinal** (and the checkpoint is unset, or we would not
      be here): a crash mid-selection. `create_media_story` is a bare insert, so a
      re-run would DUPLICATE them, and media is report-only -- losing the remainder of
      one cycle is strictly cheaper than duplicates in `/wire`. Log, do nothing, and let
      the caller record the cycle finished;
    - **no story exists**: run both from the start. The snapshot upsert is keyed per
      team and the prior-snapshot read is "most recent persisted strictly before"
      (`media_repo.py`), so a re-run over an already-written snapshot is identical.

    Opens no transaction of its own (neither media call does), and publishes nothing:
    the stories are left `posted_at IS NULL` for the cog (P5-99.m.1)."""
    cycle_index = ctx.cycle_ordinal
    if cycle_index is None:
        raise RuntimeError(f"a media cycle was asked for at {ctx.phase.value}, which has no cycle")
    existing = await media_repo.list_media_stories_for_league_season(
        ctx.pool, league_id=ctx.league_id, season=ctx.season, cycle_index=cycle_index
    )
    if existing:
        log.warning(
            "season_service: league_id=%s cycle %s already has %d media story(ies) but its "
            "closing-work checkpoint was unset -- a crash mid-selection. Skipping the "
            "remainder and recording the cycle finished (a re-run would duplicate them)",
            ctx.league_id,
            cycle_index,
            len(existing),
        )
        return None
    league = await league_repo.get_league(ctx.pool, ctx.league_id)
    if league is None:
        raise NotFoundError(f"league {ctx.league_id} does not exist")
    pulse_result = await media_service.run_media_pulse_cycle(
        ctx.pool, league_id=ctx.league_id, season=ctx.season, cycle_index=cycle_index
    )
    # docs/design/media-v2-newsroom.md MV-11 / "Build order" step 7.5a: the
    # cycle's own `LeagueSnapshot`/`GmContext` is loaded ONCE here and
    # handed to BOTH `load_team_profile_packages` and `load_event_
    # candidates` (MV-11.2's own "hoist the shared load ... into the
    # cycle" instruction) -- never a second `load_gm_inputs` build within
    # the same cycle (MV-10's own rule, restated by MV-11.2).
    #
    # **Critic-round fix: the comment this replaced claimed `load_gm_
    # inputs`'s `NotFoundError` (no `league_cap_settings` row) was normal,
    # expected TRADE_DEADLINE/PROGRESSION entry work that "every
    # REGULAR_SEASON cycle before the trade deadline" lacks. That is false.**
    # `_require_cap_settings`'s own docstring (above, this file) and
    # `league_service.py`'s `create_league` (the `create_cap_settings` call
    # at league creation) both confirm every REAL league gets a
    # `league_cap_settings` row for its own `current_season` the moment it
    # is created -- there is no normal point in a real league's season
    # where this row is legitimately still missing. The only leagues this
    # codebase ever builds without one are TEST FIXTURES that call
    # `league_repo.create_league` directly, bypassing `league_service`
    # (e.g. `tests/integration/_press_fixtures.py::bare_league_at`). A real
    # missing cap-settings row in production is therefore a genuine
    # data-integrity problem, not a harmless, expected gap -- `media_
    # service.load_gm_inputs_for_cycle` (the shared guard both this call
    # and `load_team_profile_packages`'s own fallback call go through, see
    # its own docstring) logs it at `warning`, not `info`, for exactly that
    # reason. Still tolerated here (log-and-skip, not a hard crash of the
    # WHOLE media cycle including the four legacy producers this dispatch
    # never touched) rather than raised, because a media cycle that skips
    # its cap-settings-dependent work is strictly better than one that
    # crashes entirely over what is, in production, a bug elsewhere.
    league_snapshot, gm_context = await media_service.load_gm_inputs_for_cycle(
        ctx.pool, league_id=ctx.league_id, season=ctx.season, cycle_index=cycle_index
    )

    # docs/design/media-v2-newsroom.md "Build order" step 6: both the
    # reserved `TEAM_PROFILE` quota and the real per-subject novelty signal
    # are wired in for real, per MV-1c.4's own callers table.
    profile_candidates = await media_service.load_team_profile_packages(
        ctx.pool, league_id=ctx.league_id, season=ctx.season, phase=ctx.phase,
        cycle_index=cycle_index, quota=ctx.library.team_profile_quota,
        league_snapshot=league_snapshot, gm_context=gm_context,
    )
    # MV-11: the eight previously-orphaned `dba2/media/detect/` modules
    # (`trades`, `form`+`attribution`, `construction`, `rivalry`, `scheme`,
    # `revenge`), wired into the same main scored pool `run_media_pulse_
    # cycle`'s own four producers already feed -- these compete for
    # publication on score, exactly like `STREAK`/`PLAYER_UNREST`/etc.,
    # never the reserved `TEAM_PROFILE` quota `profile_candidates` uses.
    event_candidates: tuple = ()
    if league_snapshot is not None and gm_context is not None:
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
            ctx.pool,
            league_id=ctx.league_id,
            season=ctx.season,
            phase=ctx.phase,
            cycle_index=cycle_index,
            league_snapshot=league_snapshot,
            gm_context=gm_context,
            current_snapshot=pulse_result.snapshot,
            team_names=team_names,
            player_names=player_names,
        )
    subject_last_seen = await media_service.load_subject_last_seen(
        ctx.pool, league_id=ctx.league_id, season=ctx.season
    )
    # docs/design/media-v2-newsroom.md MV-12.1, "Build order" step 7.5b:
    # the two new reads a `LeagueBrief` needs, on top of everything already
    # loaded above -- MV-12.1's own "total new I/O: 2 reads per cycle"
    # (never per story; `run_media_selection_cycle` caches these once and
    # every candidate this cycle shares them). `pulse_result.snapshot`/
    # `.team_names` are real every cycle unconditionally (`run_media_pulse_
    # cycle` always runs, unlike `league_snapshot`/`gm_context` above,
    # which MV-11's own disclosed `NotFoundError` fallback can leave `None`
    # pre-trade-deadline) -- this is why the brief is built off THOSE, not
    # off `team_names`/`player_names` from the `league_snapshot` branch.
    recent_stories = await media_repo.list_media_stories_for_cycle_range(
        ctx.pool,
        league_id=ctx.league_id,
        season=ctx.season,
        start_cycle_index=cycle_index - 3,
        end_cycle_index=cycle_index - 1,
    )
    season_arc_snapshot_rows = await media_repo.get_final_pulse_snapshot_for_season(
        ctx.pool, league_id=ctx.league_id, season=ctx.season - 1
    )
    await media_service.run_media_selection_cycle(
        ctx.pool,
        league_id=ctx.league_id,
        league_rng_seed=league.rng_seed,
        season=ctx.season,
        phase=ctx.phase,
        cycle_index=cycle_index,
        candidates=(*pulse_result.candidates, *event_candidates),
        library=ctx.library,
        use_counts={},
        profile_candidates=profile_candidates,
        subject_last_seen=subject_last_seen,
        current_snapshot=pulse_result.snapshot,
        team_names=pulse_result.team_names,
        recent_stories=recent_stories,
        season_arc_snapshot_rows=season_arc_snapshot_rows,
    )
    return None


# P5-99.b.3's entry-work and closing-work columns, read as data: entry work at three
# phases, closing work at ten -- the nine cycle-bearing phases' media cycle plus
# `REGULAR_SEASON_COMPLETE`'s stats rebuild (P5-99.q item 1).
_WORK_UNITS: dict[WorkKind, dict[Phase, tuple[WorkUnit, ...]]] = {
    WorkKind.ENTRY: {
        Phase.PRESEASON: (_ensure_team_strategies, _construct_ai_roles, _generate_schedule),
        Phase.TRADE_DEADLINE: (_require_cap_settings, _agency_checkpoint),
        Phase.PROGRESSION: (_require_cap_settings, _agency_checkpoint),
    },
    WorkKind.CLOSING: {
        **{phase: (_run_media_cycle,) for phase in CYCLE_BEARING_PHASES},
        Phase.REGULAR_SEASON_COMPLETE: (_rebuild_regular_season_stats,),
    },
}


def phase_has_work(kind: WorkKind, phase: Phase) -> bool:
    """Whether `phase` owns any work of `kind`. A phase with an empty list has
    nothing outstanding, ever -- the first half of B-1's
    `outstanding(kind) = phase_has_work(kind, phase) AND checkpoint(kind) < index`."""
    return bool(_WORK_UNITS[kind].get(phase))


def work_outstanding(kind: WorkKind, phase: Phase, *, checkpoint: int, entry_index: int) -> bool:
    """B-1's predicate, stated once (P5-99.b.1 item 4): work of `kind` is outstanding
    at `phase` when the phase OWNS work of that kind AND its completion checkpoint is
    behind the entry. **Always both conditions:** a bare `checkpoint < index` reads
    "outstanding" forever at `PLAYIN`, which has nothing to do and whose checkpoint
    therefore never advances."""
    return phase_has_work(kind, phase) and checkpoint < entry_index


async def _run_work_units(kind: WorkKind, ctx: _WorkContext) -> CheckpointResult | None:
    """Runs `ctx.phase`'s work list of `kind` in order and returns the agency
    checkpoint's result if the list had one."""
    checkpoint_result: CheckpointResult | None = None
    for unit in _WORK_UNITS[kind].get(ctx.phase, ()):
        result = await unit(ctx)
        if result is not None:
            checkpoint_result = result
    return checkpoint_result


@dataclass(frozen=True)
class SimmedBlock:
    """One game-day's simulated games, in plain types for the cog to map to
    `game_feed.open_block` (P5-99.i, P5-99.m). `block_id` is the game date's
    isoformat -- the contract `block_registry.py` documents, unique per season, and
    equal for the two halves of a day a games target splits, so `open_block` merges
    them into one `#games` block. Every game carries its own ids (`SimmedGame`),
    never a position."""

    block_id: str
    season: int
    games: tuple[SimmedGame, ...]


class GamesFailedToSimError(PhaseError):
    """A block press that finalized no game (P5-99.u item 3): refused with the ids,
    the causes and the gate's own "not played yet" line in one reply, rather than
    returning "success, nothing happened" and inviting a loop. A `PhaseError`
    because the cog already maps that to an ephemeral reply.

    The causes are the in-memory `BatchResult.failures` of the call that just
    tried (P5-99.e.1) -- not persisted, re-derived by the next press."""

    def __init__(
        self,
        *,
        game_ids: tuple[int, ...],
        failures: tuple[GameFailure, ...],
        gate_lines: tuple[str, ...],
    ) -> None:
        self.game_ids = game_ids
        self.failures = failures
        self.gate_lines = gate_lines
        causes = {f.game_id: f.cause for f in failures}
        if game_ids:
            named = "; ".join(f"game {i}: {causes.get(i, 'cause not recorded')}" for i in game_ids)
            head = f"No game of this block could be simulated, so the press stopped ({named})."
        else:
            head = "No game of this block was simulated, so the press stopped."
        tail = (
            " /advance-postpone-game <game_id> <reason> takes a game out of the schedule so "
            "the league can move on; /advance-restore-game puts it back."
        )
        gate = (" The exit gate says: " + " ".join(gate_lines)) if gate_lines else ""
        super().__init__(head + gate + tail)


def block_bound(first_index_on_date: int, last_index_on_date: int, target_index: int) -> int:
    """The highest `game_index` a block press sims through (P5-99.i): the end of the
    next unplayed game date, capped at the phase's own target index. Two things a
    block must never do -- sim past the target (the phase's closing work would then
    run against games it does not own), or sim less than the whole day it is allowed
    (the day would be split across presses for no reason).

    Refuses a target below the day's first game: this is only called for a phase
    whose target is unmet, so the first unplayed game is at or below it, and a bound
    under the day would be a press that "succeeds" having simmed nothing."""
    if target_index < first_index_on_date:
        raise ValueError(
            f"the phase's target index {target_index} is below the next unplayed game-day's "
            f"first game index {first_index_on_date}: there is no block to sim"
        )
    return min(last_index_on_date, target_index)


@dataclass(frozen=True)
class PressReport:
    """What a press did, in plain types (P5-99.m): the cog maps it to Discord.
    `to_phase` is `None` for a `BLOCKED` or `BLOCK` press. `entry_index` and
    `cycle_ordinal` are the two counters of P5-99.d.1 for the entry the press worked
    on -- the phase ENTERED by an `ENTRY` press, the phase the league is inside for a
    `BLOCK` press, `None` for a `BLOCKED` one (`cycle_ordinal` is also `None` at a
    non-cycle-bearing entry, F15). `hook_result` is
    `None` when no hook ran -- the phase is not cycle-bearing, or its cycle's hook was
    already recorded as complete. **A `DRAFT_RESUME` press's `hook_result` is the DRAFT
    hook that resumed the board** -- carrying the `draft_cycle` outcome the on-the-clock
    ping is built from -- whether the work prefix or the resume itself ran it (one call
    per press, reported once, so `resumed_hook_result` is `None` there).
    `resumed_hook_result` is the hook the WORK PREFIX finished for the phase the league
    was standing in (P5-99.l: a press that died
    before its hook finished) -- the ENTERED phase's hook is `hook_result`, and an
    Entry press can carry both. `checkpoint_result` is the agency checkpoint this
    press ran, if any: for an `ENTRY` press the ENTERED phase's, for any other the
    one its work prefix finished for the current phase (`players_skipped` says how
    many an earlier, interrupted pass had already done).

    `media_cycle_index` is the cycle ordinal whose media SELECTION this press finished
    (the entered phase's, else the one its prefix finished) -- a hint that stories are
    ready to publish, never what the cog publishes from: the cog posts whatever the
    database says is unposted (P5-99.m.1), so a report that arrives after a partial
    earlier attempt still posts exactly the remainder. `None` when no media cycle
    finished here, including at `REGULAR_SEASON_COMPLETE`, whose closing work is the
    stats rebuild and bears no cycle.

    A `BLOCK` press carries `block` (the day it simmed; `None` for any other kind)
    and its `failed_games` / `failures`: a game that failed even after the one retry
    but did not stop the day's other games. `verdict` is the exit gate evaluated
    AFTER the sim on a block press -- what would stop the league leaving this phase
    now."""

    kind: PressKind
    from_phase: Phase
    verdict: ExitVerdict
    to_phase: Phase | None = None
    entry_index: int | None = None
    cycle_ordinal: int | None = None
    expired_offers: int = 0
    hook_result: PhaseHookResult | None = None
    checkpoint_result: CheckpointResult | None = None
    resumed_hook_result: PhaseHookResult | None = None
    media_cycle_index: int | None = None
    block: SimmedBlock | None = None
    failed_games: tuple[int, ...] = ()
    failures: tuple[GameFailure, ...] = ()


@dataclass(frozen=True)
class _PointerMove:
    season: int
    entry_index: int
    cycle_ordinal: int | None


def _successor(phase: Phase) -> Phase:
    """The one phase an Entry press enters from `phase`: the branch rule at the two
    branch points (F12), the graph's only edge everywhere else. `WAIVERS` has one
    graph edge and no successor here -- it crosses the season boundary."""
    if phase is Phase.WAIVERS:
        raise PhaseError(WAIVERS_EDGE_MESSAGE)
    if phase in _DEFAULT_SUCCESSOR:
        return _DEFAULT_SUCCESSOR[phase]
    (only,) = NEXT_PHASES[phase]
    return only


def _require_no_open_transaction(ex: Executor, what: str) -> None:
    """P5-99.c's assertion before the hook: `run_phase_hook` opens its own
    transactions and gates (RMLP tier 0), and handed a connection that is already
    in one it would nest them -- the shape `game_feed._require_no_open_transaction`
    guards for Discord. A pool, or a connection outside any transaction, passes."""
    if isinstance(ex, asyncpg.Connection) and ex.is_in_transaction():
        raise ValueError(
            f"{what} must not be handed a connection with a transaction open: it owns its "
            "own transactions and league gate (P5-99.c)"
        )


# A milestone sentence for whoever is watching the press (P5-99.j "Progress"): awaited
# only BETWEEN this module's transactions, by `_report_progress` below.
ProgressCallback = Callable[[str], Awaitable[None]]


async def _report_progress(
    ex: Executor, on_progress: ProgressCallback | None, message: str
) -> None:
    """Awaits `on_progress(message)` -- a Discord write reached from inside a service --
    with the same open-transaction assertion `game_feed`/`draft_feed` carry (P5-14, M5):
    `on_progress` may only ever run with no transaction of ours open, so it is called from
    the press's own frame, between its transactions, and this asserts it immediately
    before each call. `press` hands `ex` a pool, which cannot be inside a transaction;
    the assertion is what a future caller that handed a connection, or a call placed
    inside an `async with transaction(...)`, would trip.

    Progress is decoration. A callback that raises (an expired interaction, a rate
    limit) is logged with its traceback and the press goes on: aborting it here would
    strand a held claim and half-finished work for a sentence nobody needed. A
    cancellation is not an `Exception` and propagates as the cancellation it is."""
    if on_progress is None:
        return
    _require_no_open_transaction(ex, "on_progress")
    try:
        await on_progress(message)
    except Exception:
        log.warning(
            "season_service: the progress callback failed for %r; the press continues",
            message,
            exc_info=True,
        )


async def _move_pointer(
    pool: asyncpg.Pool,
    league_id: int,
    *,
    expected_phase: Phase,
    expected_season: int,
    to_phase: Phase,
    actor_user_id: int,
) -> _PointerMove:
    """tx1 (P5-99.c): moves the pointer to `to_phase` and derives the entered
    entry's two counters. `set_lock_timeout` first, then the `leagues` row lock we
    take ourselves; **both counts are taken after the lock**, because an unlocked
    count can be stale against a concurrent `tools/` advance (P5-99.d) and the lock
    is needed a line later regardless.

    Abandons -- writing nothing -- if the locked `(phase, season)` is not the pair
    the gate was evaluated against (`league_claim`'s own agreement rule): the
    verdict was about a state that no longer holds. `phase_service.advance`'s own
    `FOR UPDATE` is then a free re-acquisition. A 55P03 from the lock is mapped at
    the statement to a league-noun `ConcurrencyError`.
    """
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        locked_season, entries_counted, cycle_bearing_counted = await _lock_and_count_entries(
            conn, league_id, expected_phase=expected_phase, expected_season=expected_season
        )
        await phase_service.advance(conn, league_id, to_phase, actor_user_id)
    return _PointerMove(
        season=locked_season,
        entry_index=derive_entry_index(entries_counted, entering=True),
        cycle_ordinal=derive_cycle_ordinal(to_phase, cycle_bearing_counted, entering=True),
    )


async def _lock_and_count_entries(
    conn: asyncpg.Connection, league_id: int, *, expected_phase: Phase, expected_season: int
) -> tuple[int, int, int]:
    """The shared body of the two locked pointer reads, run inside a transaction
    that has already set `lock_timeout`: take the `leagues` row lock, refuse if the
    locked `(phase, season)` is not the pair the caller evaluated, then take both
    counts -- AFTER the lock, so a concurrent `tools/` advance cannot make them stale.
    Returns `(season, entries_counted, cycle_bearing_counted)`."""
    try:
        state = await league_repo.get_phase_state_for_update(conn, league_id)
    except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
        raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc
    if state is None:
        raise NotFoundError(f"league {league_id} does not exist")
    locked_phase, locked_season = state
    if (locked_phase, locked_season) != (expected_phase, expected_season):
        raise ConcurrencyError(league_claim.LEAGUE_ADVANCED_MESSAGE)
    entries_counted = await league_repo.count_phase_entries(conn, league_id, locked_season)
    cycle_bearing_counted = await league_repo.count_phase_entries(
        conn, league_id, locked_season, CYCLE_BEARING_PHASES
    )
    return locked_season, entries_counted, cycle_bearing_counted


async def _read_pointer_locked(
    pool: asyncpg.Pool, league_id: int, *, expected_phase: Phase, expected_season: int
) -> _PointerMove:
    """The pointer read for a press that does NOT move it (P5-99.c.1): the current
    entry's `entry_index` and `cycle_ordinal`, counted under the `leagues` row lock
    and derived with `entering=False` (the entry already exists, so it is counted and
    the index is one less). **Takes the row lock and releases it before any long
    work** -- it never spans phase work or a sim. Same lock-timeout-first shape, same
    phase-agreement abandon, same league-noun mapping of a 55P03 as `_move_pointer`."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        locked_season, entries_counted, cycle_bearing_counted = await _lock_and_count_entries(
            conn, league_id, expected_phase=expected_phase, expected_season=expected_season
        )
    return _PointerMove(
        season=locked_season,
        entry_index=derive_entry_index(entries_counted, entering=False),
        cycle_ordinal=derive_cycle_ordinal(expected_phase, cycle_bearing_counted, entering=False),
    )


async def _record_hook_completed(pool: asyncpg.Pool, league_id: int, cycle_ordinal: int) -> bool:
    """The hook checkpoint (P5-99.c.1): its own transaction, `set_lock_timeout`
    first, then the monotone update. Keyed by `cycle_ordinal` and written only for
    a cycle-bearing phase -- the caller never has an ordinal to pass otherwise."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            return await league_repo.set_hook_completed_cycle_index(conn, league_id, cycle_ordinal)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc


async def _record_entry_work_completed(
    pool: asyncpg.Pool, league_id: int, entry_index: int
) -> bool:
    """The entry-work checkpoint (P5-99.r.1): its own transaction,
    `set_lock_timeout` first, then the monotone update. Keyed by `entry_index` --
    never by the cycle ordinal `_record_hook_completed` takes."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            return await league_repo.set_entry_work_completed_index(conn, league_id, entry_index)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc


async def _record_closing_work_completed(
    pool: asyncpg.Pool, league_id: int, entry_index: int
) -> bool:
    """The closing-work checkpoint (P5-99.r.1): same shape as
    `_record_entry_work_completed`, its own column, keyed by `entry_index`."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            return await league_repo.set_closing_work_completed_index(conn, league_id, entry_index)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc


async def _games_target_met(
    ex: Executor, league: League, *, facts: GateFacts | None = None
) -> bool:
    """Whether `league`'s current phase's OWN games target is met -- what makes its
    closing work due. Only the four schedule-consuming phases have a target that can
    be unmet; every other phase's target is met by definition (`PRESEASON`'s is 0.0,
    and the rest come after the regular season). Reuses the gate's own schedule
    blockers, so the two can never disagree about what "target met" means.

    **No schedule at all is NOT met** (critic m2): with nothing scheduled the arithmetic
    finds nothing due, which would read as "reached" and run a media cycle or the stats
    rebuild over zero games -- and the closing checkpoint is monotone, so the press that
    finally played them would skip it.

    `facts` are the phase's gate facts when the caller has just read them (the block press,
    after its sim); they are read here otherwise. They must be for the phase `league` is in."""
    if league.current_phase not in SCHEDULE_CONSUMING_PHASES:
        return True
    if facts is None:
        facts = await load_gate_facts(ex, league)
    elif facts.phase is not league.current_phase:
        raise RuntimeError(
            f"gate facts for {facts.phase.value} were handed to a check at "
            f"{league.current_phase.value}"
        )
    blockers = _schedule_blockers(facts)
    return not _target_unmet(blockers) and not _schedule_missing(blockers)


def _schedule_missing(blockers: Iterable[Blocker]) -> bool:
    """Whether `blockers` include `SCHEDULE_MISSING`: the season has no regular-season
    game at all. Named because `_games_target_met` needs it beside `_target_unmet` -- the two
    together are the whole of "this phase's target is not met"."""
    return any(b.code is BlockerCode.SCHEDULE_MISSING for b in blockers)


async def _season_has_no_schedule(ex: Executor, league: League) -> bool:
    """Whether `league`'s current season has no regular-season game at all, whatever its
    status: the same fact `load_gate_facts` reads (`max_game_index is None`) for the schedule
    phases, readable from any phase -- `/advance-force` asks it at `PRESEASON` and `SETUP`,
    which have no gate row for it."""
    return (
        await game_repo.get_max_game_index(ex, league.id, league.current_season, GameType.REGULAR)
        is None
    )


def _target_unmet(blockers: Iterable[Blocker]) -> bool:
    """Whether `blockers` include the games-target one: the single definition of
    "this phase's own target is not met", shared by `_games_target_met` (closing work's
    due-ness) and the press's Block-versus-gate decision, so the two can never
    disagree about it. A season with NO schedule is a different blocker
    (`SCHEDULE_MISSING`): a block press has nothing to sim, so the press refuses it
    through the gate rather than entering `_block_press`."""
    return any(b.code is BlockerCode.GAMES_TARGET_NOT_REACHED for b in blockers)


@dataclass(frozen=True)
class _OwedWork:
    """What `_finish_owed_work` did: whether it ran (and checkpointed) the list, and
    the agency checkpoint's result if the list had one. `ran=False` means nothing was
    outstanding, or the closing work's target is not yet met."""

    ran: bool = False
    checkpoint_result: CheckpointResult | None = None


async def _finish_owed_work(
    pool: asyncpg.Pool,
    league_id: int,
    kind: WorkKind,
    *,
    pointer: _PointerMove,
    library: LoadedMediaLibrary,
    facts: GateFacts | None = None,
    on_progress: ProgressCallback | None = None,
) -> _OwedWork:
    """Completes the current entry's work of `kind` if it is outstanding, then
    records the checkpoint (P5-99.b.1). The checkpoint and phase are read from the
    row as it stands NOW, like `_run_hook_once`.

    Closing work is additionally due only once the phase's own games target is met:
    the checkpoint is monotone, so a rebuild or a media cycle recorded early would make
    the press that finally plays the last game skip it. `facts` (optional) are the gate facts
    the caller has just read for the current phase, handed to `_games_target_met` so it does
    not read them again. `on_progress` is told the work is starting, and only when it really
    will (after the outstanding and target-met checks), so the milestone is never a false one."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    phase = league.current_phase
    checkpoint = (
        league.entry_work_completed_index
        if kind is WorkKind.ENTRY
        else league.closing_work_completed_index
    )
    if not work_outstanding(kind, phase, checkpoint=checkpoint, entry_index=pointer.entry_index):
        return _OwedWork()
    if kind is WorkKind.CLOSING and not await _games_target_met(pool, league, facts=facts):
        return _OwedWork()
    await _report_progress(
        pool,
        on_progress,
        f"Running {phase.value}'s {'phase work' if kind is WorkKind.ENTRY else 'closing work'}...",
    )
    result = await _run_work_units(
        kind,
        _WorkContext(
            pool=pool,
            league_id=league_id,
            season=pointer.season,
            phase=phase,
            cycle_ordinal=pointer.cycle_ordinal,
            library=library,
        ),
    )
    if kind is WorkKind.ENTRY:
        await _record_entry_work_completed(pool, league_id, pointer.entry_index)
    else:
        await _record_closing_work_completed(pool, league_id, pointer.entry_index)
    return _OwedWork(ran=True, checkpoint_result=result)


def _media_cycle_finished(pointer: _PointerMove, closing: _OwedWork) -> int | None:
    """The cycle ordinal whose media selection this press finished, or `None`:
    closing work ran AND the entry bears a cycle (the stats rebuild at
    `REGULAR_SEASON_COMPLETE` is closing work at a phase that bears none). A hint for
    the cog -- what it publishes is what the database says is unposted (P5-99.m.1)."""
    return pointer.cycle_ordinal if closing.ran else None


# The one cycle-bearing phase whose hook is not run by its Entry press (JC-13).
_HOOK_DEFERRED_TO_BLOCK: frozenset[Phase] = frozenset({Phase.REGULAR_SEASON})


def _needs_pointer(phase: Phase) -> bool:
    """Whether a press standing in `phase` must read its entry's counters under the
    row lock before it does anything: the phase owns work, bears a hook (both are
    owed-work the prefix may finish), or consumes the schedule (a block press names
    its entry to record closing work). A press at any other phase -- `PLAYIN`, the
    playoffs, `AWARDS` -- costs nothing here."""
    return (
        any(phase_has_work(kind, phase) for kind in WorkKind)
        or phase in CYCLE_BEARING_PHASES
        or phase in SCHEDULE_CONSUMING_PHASES
    )


@dataclass(frozen=True)
class _EntryOutcome:
    """What finishing one entry's owed work produced: the agency checkpoint's result
    (the entry work's, if it ran), the hook's (if it ran) and the cycle ordinal whose
    media selection finished (if it did)."""

    checkpoint_result: CheckpointResult | None = None
    hook_result: PhaseHookResult | None = None
    media_cycle_index: int | None = None


async def _finish_entry(
    pool: asyncpg.Pool,
    league_id: int,
    *,
    pointer: _PointerMove,
    actor_user_id: int,
    agency_enabled: bool,
    library: LoadedMediaLibrary,
    on_progress: ProgressCallback | None = None,
) -> _EntryOutcome:
    """Completes ONE phase entry's owed work in its fixed order (P5-99.b.1, P5-99.r):
    entry work, then the hook, then closing work -- each a no-op when its own
    checkpoint says it is already done. The one routine both the work prefix (the
    entry the league is standing in) and an Entry press (the entry it just created)
    call, so what "finishing an entry" means is decided once.

    The hook exists only for a cycle-bearing phase and is keyed by `cycle_ordinal`
    (`None` everywhere else, F15): a non-cycle-bearing entry has no hook to retry and
    writes no hook checkpoint. Closing work is additionally due only once the phase's
    own games target is met (`_finish_owed_work`).

    **The phase is not a parameter:** every step reads the phase from the `leagues` row as
    it stands NOW (`_finish_owed_work`, `_run_hook_once`), so there is one source and no
    way for a caller's idea of the phase to disagree with the row the work is keyed by.

    `on_progress` is handed to each step, which reports only work that is really about to run:
    the prefix's resumed hook or agency checkpoint reports exactly as an Entry press's does."""
    entry = await _finish_owed_work(
        pool, league_id, WorkKind.ENTRY, pointer=pointer, library=library, on_progress=on_progress
    )
    hook_result = None
    if pointer.cycle_ordinal is not None:
        hook_result = await _run_hook_once(
            pool,
            league_id,
            cycle_ordinal=pointer.cycle_ordinal,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
            on_progress=on_progress,
        )
    closing = await _finish_owed_work(
        pool, league_id, WorkKind.CLOSING, pointer=pointer, library=library, on_progress=on_progress
    )
    return _EntryOutcome(
        checkpoint_result=entry.checkpoint_result or closing.checkpoint_result,
        hook_result=hook_result,
        media_cycle_index=_media_cycle_finished(pointer, closing),
    )


async def _complete_owed_work(
    pool: asyncpg.Pool,
    league: League,
    pointer: _PointerMove | None,
    *,
    actor_user_id: int,
    agency_enabled: bool,
    library: LoadedMediaLibrary,
    on_progress: ProgressCallback | None = None,
) -> _EntryOutcome:
    """The work prefix (P5-99.b.1): finish the CURRENT entry's owed work -- entry
    work, hook, closing work -- before anything else a press does, on every press of
    every kind. `pointer` is the entry's counters read under the row lock, or `None`
    for a phase that needs none (`_needs_pointer`), which owes nothing."""
    if pointer is None:
        return _EntryOutcome()
    return await _finish_entry(
        pool,
        league.id,
        pointer=pointer,
        actor_user_id=actor_user_id,
        agency_enabled=agency_enabled,
        library=library,
        on_progress=on_progress,
    )


async def _hook_is_due(ex: Executor, league: League, phase: Phase) -> bool:
    """Whether `phase`'s hook may run now, given that its checkpoint is behind. True
    everywhere except `REGULAR_SEASON` (JC-13, P5-99.k): its hook -- the first AI
    trade window -- is due once every scheduled game up to
    `first_trade_window_target_index` has been played. No schedule at all is "not
    due": there is nothing played to be past a fraction of, so a league with no games
    never holds that window -- and cannot leave `REGULAR_SEASON` either, because the exit gate
    reports `SCHEDULE_MISSING` for it. Reads the same
    `list_scheduled_game_keys` the exit gate does, so a postponed game counts as
    played here exactly as it does for the target."""
    if phase is not Phase.REGULAR_SEASON:
        return True
    season = league.current_season
    max_game_index = await game_repo.get_max_game_index(ex, league.id, season, GameType.REGULAR)
    if max_game_index is None:
        return False
    still_scheduled = await game_repo.list_scheduled_game_keys(
        ex,
        league.id,
        season,
        GameType.REGULAR,
        through_game_index=first_trade_window_target_index(max_game_index),
    )
    return not still_scheduled


async def _run_hook_once(
    pool: asyncpg.Pool,
    league_id: int,
    *,
    cycle_ordinal: int,
    actor_user_id: int,
    agency_enabled: bool,
    on_progress: ProgressCallback | None = None,
) -> PhaseHookResult | None:
    """Runs the CURRENT phase's hook unless its cycle's checkpoint says it already
    completed, then records the checkpoint. The phase and the checkpoint are both read
    from the row as it stands NOW, not carried from the start of the press (one source,
    like `_finish_owed_work`). `None`: skipped."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    phase = league.current_phase
    if league.hook_completed_cycle_index >= cycle_ordinal:
        return None
    if not await _hook_is_due(pool, league, phase):
        return None
    _require_no_open_transaction(pool, "run_phase_hook")
    await _report_progress(pool, on_progress, f"Running {phase.value}'s AI pass...")
    result = await gm_service.run_phase_hook(
        pool,
        league_id=league_id,
        phase=phase,
        actor_user_id=actor_user_id,
        agency_enabled=agency_enabled,
    )
    await _record_hook_completed(pool, league_id, cycle_ordinal)
    return result


async def press(
    pool: asyncpg.Pool,
    *,
    league_id: int,
    actor_user_id: int,
    library: LoadedMediaLibrary,
    agency_enabled: bool = True,
    on_progress: ProgressCallback | None = None,
) -> PressReport:
    """One `/advance` press. Commissioner-only (checked here as well as in the
    cog, P5-99.a), and the whole press runs inside the advance claim, taken and
    released in transactions of their own so it excludes every human write
    (P5-99.t) for its whole duration. The release never raises and is shielded from
    cancellation, so it can replace neither the press's own exception nor -- while the event
    loop survives -- leak the claim (`_run_under_claim`, `_release_claim_shielded`); a
    shutdown-cancel that closes the loop can still leave it for `/advance-unlock`.

    `library` is REQUIRED (P5-99.s.5): `run_media_selection_cycle` needs the live media
    library and `LoadedMediaLibrary` is a `media_service` type, so it crosses this
    boundary as an argument the cog loads once per process and caches -- never per
    press, and never a hidden default that reads a file inside a service. A press that
    owes no media cycle ignores it.

    `on_progress` is optional and called at named milestones only -- claiming, advancing,
    running a phase's work, its AI pass or its closing work (each only when that work really
    runs this press, including the ones the work prefix finishes for the phase the league is
    standing in), simming a game day, resuming the draft -- each time from this call's own
    frame between transactions (`_report_progress`). "Posting" is the cog's own milestone: the
    service does not know what is posted. It reaches Discord, so it is never awaited inside a
    transaction, and a callback that fails cannot fail the press."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action="advance"
    )
    await _report_progress(pool, on_progress, "Claiming the league...")
    return await _run_under_claim(
        pool,
        league_id,
        actor_user_id,
        lambda: _press_claimed(
            pool,
            league_id,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
            library=library,
            on_progress=on_progress,
        ),
    )


# Strong references to the in-flight shielded release tasks. The event loop keeps only a weak
# reference to a task, so a release nobody else refers to can be collected mid-flight -- and
# the shield's whole purpose is that this task outlives the press that started it.
_PENDING_RELEASES: set[asyncio.Task[bool]] = set()


async def _release_claim_shielded(
    pool: asyncpg.Pool, league_id: int, claimed_by: int, claimed_at: datetime
) -> bool:
    """`_release_claim` run as a task of its own, awaited through `asyncio.shield`.

    `_release_claim` catches `Exception`, which a `CancelledError` is not: cancelled
    mid-release, its transaction rolled back and the claim stayed set until
    `/advance-unlock` (critic m1). Shielded, a cancellation of the awaiting press
    interrupts only the wait -- the release task is not cancelled and commits. The task is
    held in `_PENDING_RELEASES` until it finishes, so it cannot be garbage-collected while
    nothing awaits it.

    **What this does not cover:** all of it needs a loop that survives the cancellation. A
    shutdown-cancel -- the loop closing -- destroys the pending release with it, and the claim
    then stays until `/advance-unlock` clears it (R4). This wrapper opens no transaction
    (`_release_claim` is the one that does, and it is the function the lock-discipline check
    pins)."""
    release = asyncio.create_task(_release_claim(pool, league_id, claimed_by, claimed_at))
    _PENDING_RELEASES.add(release)
    release.add_done_callback(_PENDING_RELEASES.discard)
    return await asyncio.shield(release)


async def _run_under_claim[T](
    pool: asyncpg.Pool, league_id: int, actor_user_id: int, body: Callable[[], Awaitable[T]]
) -> T:
    """Runs `body` inside the advance claim -- the one place `press`, `advance_to` and `force`
    take and release it. The release never raises, is shielded from cancellation
    (`_release_claim_shielded`), and cannot replace the body's own exception or cancellation
    with its own.

    **A cancellation absorbed here is un-counted.** The body's exception is what the caller
    must see; a cancel that lands while the release is being awaited would replace it if it
    propagated, so it is caught and logged and the original re-raised. Swallowing a cancel
    request without `Task.uncancel()` leaves the task's cancel count raised, which an
    `asyncio.timeout()` or a `TaskGroup` up the stack reads to decide whether it was the cause
    of the cancellation; so the absorbed request is un-counted, once. (When the body's own
    exception WAS a cancellation, its request stays counted: the task still ends cancelled.)
    After a body that RETURNED, a cancel during the release simply propagates as the
    cancellation it is; the release still completes."""
    claimed_at = await _claim_league(pool, league_id, actor_user_id)
    try:
        result = await body()
    except BaseException:
        try:
            await _release_claim_shielded(pool, league_id, actor_user_id, claimed_at)
        except asyncio.CancelledError:
            log.warning(
                "season_service: cancelled again while releasing league_id=%s's claim; the "
                "release continues in the background and the original exception propagates",
                league_id,
            )
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
        raise
    await _release_claim_shielded(pool, league_id, actor_user_id, claimed_at)
    return result


async def _press_claimed(
    pool: asyncpg.Pool,
    league_id: int,
    *,
    actor_user_id: int,
    agency_enabled: bool,
    library: LoadedMediaLibrary,
    on_progress: ProgressCallback | None = None,
) -> PressReport:
    league = await league_repo.get_league(pool, league_id)  # read again, now that we hold the claim
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    from_phase = league.current_phase

    # The entry's counters, counted under the row lock and released at once: the
    # work prefix, a block press and (later) a Draft-resume press all name the entry
    # they are inside by them (P5-99.c.1, M1).
    pointer = (
        await _read_pointer_locked(
            pool, league.id, expected_phase=from_phase, expected_season=league.current_season
        )
        if _needs_pointer(from_phase)
        else None
    )

    # The work prefix (P5-99.b.1): finish the CURRENT entry's owed work -- entry work,
    # hook, closing work -- before anything else, on every press of every kind. A
    # `Blocked` one included, which is what lets a press that crashed after the
    # pointer moved converge.
    prefix = await _complete_owed_work(
        pool,
        league,
        pointer,
        actor_user_id=actor_user_id,
        agency_enabled=agency_enabled,
        library=library,
        on_progress=on_progress,
    )

    facts = await load_gate_facts(pool, league)
    verdict = evaluate_exit(facts)
    kind = derive_press_kind(from_phase, verdict)

    # Row 3 of P5-99.b.2, AHEAD of the gate on purpose: an unmet games target is the
    # gate's own schedule blocker, and a block press is what resolves it.
    if kind is PressKind.BLOCK:
        if pointer is None:  # unreachable: a schedule phase always reads its pointer
            raise RuntimeError(f"a block press at {from_phase.value} has no entry counters")
        return await _block_press(
            pool,
            league,
            pointer=pointer,
            facts=facts,
            prefix=prefix,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
            library=library,
            on_progress=on_progress,
        )

    # Row 2 of P5-99.b.2, ahead of the gate on purpose (see the header above).
    if kind is PressKind.DRAFT_RESUME:
        if pointer is None:  # unreachable: `DRAFT` is cycle-bearing, so it reads its pointer
            raise RuntimeError("a draft-resume press has no entry counters")
        return await _draft_resume_press(
            pool,
            league,
            pointer=pointer,
            prefix=prefix,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
            on_progress=on_progress,
        )

    if kind is PressKind.BLOCKED:
        return PressReport(
            kind=PressKind.BLOCKED,
            from_phase=from_phase,
            verdict=verdict,
            checkpoint_result=prefix.checkpoint_result,
            resumed_hook_result=prefix.hook_result,
            media_cycle_index=prefix.media_cycle_index,
        )

    to_phase = _successor(from_phase)
    if to_phase is Phase.DRAFT:
        # Row 5: the destination check. Before `_move_pointer`, so a refusal writes no
        # `phase_history` row and leaves the league where it was.
        board_blockers = draft_board_blockers(await load_draft_board_facts(pool, league))
        if board_blockers:
            return PressReport(
                kind=PressKind.BLOCKED,
                from_phase=from_phase,
                verdict=ExitVerdict(allowed=False, blockers=tuple(board_blockers)),
                checkpoint_result=prefix.checkpoint_result,
                resumed_hook_result=prefix.hook_result,
                media_cycle_index=prefix.media_cycle_index,
            )
    await _report_progress(
        pool, on_progress, f"Advancing {from_phase.value} -> {to_phase.value}..."
    )
    moved = await _move_pointer(
        pool,
        league_id,
        expected_phase=from_phase,
        expected_season=league.current_season,
        to_phase=to_phase,
        actor_user_id=actor_user_id,
    )
    # After tx1 commits, so the sweep reads the NEW phase (F4).
    expired_offers = await offer_repo.expire_stale_offers_for_league(pool, league_id)
    entered = await _finish_entry(
        pool,
        league_id,
        pointer=moved,
        actor_user_id=actor_user_id,
        agency_enabled=agency_enabled,
        library=library,
        on_progress=on_progress,
    )
    return PressReport(
        kind=PressKind.ENTRY,
        from_phase=from_phase,
        verdict=verdict,
        to_phase=to_phase,
        entry_index=moved.entry_index,
        cycle_ordinal=moved.cycle_ordinal,
        expired_offers=expired_offers,
        hook_result=entered.hook_result,
        checkpoint_result=entered.checkpoint_result or prefix.checkpoint_result,
        resumed_hook_result=prefix.hook_result,
        media_cycle_index=(
            entered.media_cycle_index
            if entered.media_cycle_index is not None
            else prefix.media_cycle_index
        ),
    )


def _draft_unfinished(blockers: Iterable[Blocker]) -> bool:
    """Whether `blockers` include the draft's own: the single definition of "the draft is
    not `COMPLETE`" the resume press shares with the gate. `_draft_blockers` is empty
    exactly when `draft_completion_state` is `COMPLETE`, so this is the spec's guard --
    re-run iff the draft is not `COMPLETE` -- without a second copy of the predicate."""
    return any(b.code is BlockerCode.DRAFT_NOT_COMPLETE for b in blockers)


async def _draft_resume_press(
    pool: asyncpg.Pool,
    league: League,
    *,
    pointer: _PointerMove,
    prefix: _EntryOutcome,
    actor_user_id: int,
    agency_enabled: bool,
    on_progress: ProgressCallback | None = None,
) -> PressReport:
    """Row 2 of P5-99.b.2 (P5-99.h): re-runs the DRAFT hook and nothing else, moving no
    pointer. **A narrow, stated exception to P5-73:** it runs whatever
    `hook_completed_cycle_index` says, because that checkpoint is written after the
    ENTRY's hook and a draft that stopped at a person (or was topped up) is not finished
    by it -- while the DRAFT branch merges nothing, so a re-run cannot double-count what
    P5-73 exists to protect. The eligible list is re-derived every call, which makes the
    re-run idempotent. The checkpoint is not written here: it is already at (or past) this
    cycle, and the write is monotone.

    If the work prefix ran the hook this very press (the entry's own was still owed), THAT
    run was the resume -- a second call would re-derive the same board -- and its result is
    the report's `hook_result`. The verdict is the gate re-evaluated AFTER the resume, so a
    resume that completes the draft reports the gate open; the pointer stays until the
    next press, which is then an Entry."""
    if prefix.hook_result is not None:
        hook_result = prefix.hook_result
    else:
        await _report_progress(pool, on_progress, "Resuming the draft...")
        _require_no_open_transaction(pool, "run_phase_hook")
        hook_result = await gm_service.run_phase_hook(
            pool,
            league_id=league.id,
            phase=Phase.DRAFT,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
        )
    after = evaluate_exit(await load_gate_facts(pool, league))
    return PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=after,
        entry_index=pointer.entry_index,
        cycle_ordinal=pointer.cycle_ordinal,
        hook_result=hook_result,
        checkpoint_result=prefix.checkpoint_result,
        media_cycle_index=prefix.media_cycle_index,
    )


async def _block_press(
    pool: asyncpg.Pool,
    league: League,
    *,
    pointer: _PointerMove,
    facts: GateFacts,
    prefix: _EntryOutcome,
    actor_user_id: int,
    agency_enabled: bool,
    library: LoadedMediaLibrary,
    on_progress: ProgressCallback | None = None,
) -> PressReport:
    """Row 3 of P5-99.b.2 (P5-99.i, P5-99.u): sims the next unplayed game date -- the
    bound is that date's last `game_index`, capped at the phase's own target -- and
    runs the phase's closing work if that meets the target.

    **Failed games** stay `scheduled` (F11), so this policy is what stops a league
    freezing: one bounded retry of the same bound (a lock timeout or a blip is the
    ordinary failure, and only the still-scheduled games are re-simmed); then, if the
    press finalized NO game, it refuses naming the ids, the causes and the gate's own
    line -- never "success, nothing happened". A day with some games finalized and
    some failed is a press that progressed: its report lists the failures. The only
    recoverable path is `postpone_game`.

    `game_sim_service.sim_scheduled_games` is reached through the module, and on the
    pool: it opens one transaction per game itself. The `Block` is plain types for the
    cog to hand to `game_feed.open_block` once every transaction here has closed."""
    phase, season = league.current_phase, league.current_season
    if facts.max_game_index is None:
        raise RuntimeError(f"a block press at {phase.value} found no schedule")
    target_index = games_target_index(GAMES_TARGET_FRACTION[phase], facts.max_game_index)
    day = await game_repo.get_next_scheduled_game_day(pool, league.id, season, GameType.REGULAR)
    if day is None:
        raise RuntimeError(
            f"a block press at {phase.value} found the games target unmet and no scheduled game"
        )
    game_date, first_index, last_index = day
    bound = block_bound(first_index, last_index, target_index)

    await _report_progress(
        pool,
        on_progress,
        f"Simming the {game_date.isoformat()} game day ({bound - first_index + 1} game(s))...",
    )
    batch = await game_sim_service.sim_scheduled_games(
        pool, league.id, season, through_game_index=bound, return_results=True
    )
    simmed = list(batch.results)
    failed_ids, failures = batch.failed_game_ids, batch.failures
    if failed_ids:
        retry = await game_sim_service.sim_scheduled_games(
            pool, league.id, season, through_game_index=bound, return_results=True
        )
        simmed.extend(retry.results)
        failed_ids, failures = retry.failed_game_ids, retry.failures

    after_facts = await load_gate_facts(pool, league)
    after = evaluate_exit(after_facts)
    if not simmed:
        raise GamesFailedToSimError(
            game_ids=failed_ids,
            failures=failures,
            gate_lines=tuple(b.message for b in after.blockers),
        )

    # JC-13: at `REGULAR_SEASON` the entry's hook is held for a block press, and this is
    # it -- run after the sim, so the trade window sees the games it exists to follow.
    # The ordinal is `pointer.cycle_ordinal`, counted on THIS press's own Block path
    # under the row lock (M1), never one carried from the entry press. Every later
    # block press reads the checkpoint and skips; no other phase's hook is deferred, so
    # asking here for them would be a read that can only say "already done".
    hook_result = None
    if phase in _HOOK_DEFERRED_TO_BLOCK and pointer.cycle_ordinal is not None:
        hook_result = await _run_hook_once(
            pool,
            league.id,
            cycle_ordinal=pointer.cycle_ordinal,
            actor_user_id=actor_user_id,
            agency_enabled=agency_enabled,
            on_progress=on_progress,
        )

    # Closing work, when this block met the phase's own target (`_games_target_met`
    # decides): the media cycle of a cycle-bearing phase, the stats rebuild at
    # `REGULAR_SEASON_COMPLETE`.
    closing = await _finish_owed_work(
        pool,
        league.id,
        WorkKind.CLOSING,
        pointer=pointer,
        library=library,
        facts=after_facts,
        on_progress=on_progress,
    )
    return PressReport(
        kind=PressKind.BLOCK,
        from_phase=phase,
        verdict=after,
        entry_index=pointer.entry_index,
        cycle_ordinal=pointer.cycle_ordinal,
        hook_result=hook_result,
        checkpoint_result=prefix.checkpoint_result,
        resumed_hook_result=prefix.hook_result,
        media_cycle_index=(
            _media_cycle_finished(pointer, closing) if closing.ran else prefix.media_cycle_index
        ),
        block=SimmedBlock(block_id=game_date.isoformat(), season=season, games=tuple(simmed)),
        failed_games=tuple(failed_ids),
        failures=tuple(failures),
    )


# --- /advance-to and /advance-force (P5-99.f, P5-99.g) ------------------------------------
#
# Both take the advance claim and neither is a press: they run no hook, no phase work, no
# sim and no media, and write no completion checkpoint. `advance_to` steps through the
# phases whose Entry press would do nothing; `force` is the audited override.

FORCE_PRESEASON_MESSAGE = (
    "PRESEASON cannot be forced into, even by the commissioner: nothing writes "
    "leagues.current_season yet, so entering it again would replay the same season -- a second "
    "PRESEASON history row, a second AI hook and a second media cycle -- rather than start a new "
    "one. Starting the next season is Phase 7's rollover, which is not built (P5-70, P5-84)."
)

FORCE_SETUP_MESSAGE = (
    "SETUP cannot be forced into: it is only the state a league is created in, never a "
    "destination. Leaving it again would take PRESEASON's entry, replaying the season's opening "
    "(a second PRESEASON history row, hook and media cycle) for a season already under way."
)


def force_same_phase_message(phase: Phase) -> str:
    return (
        f"the league is already in {phase.value}: a forced move to the phase it is in "
        "would write a second history row for the same entry and break the entry count"
    )


def force_not_forward_message(from_phase: Phase, to_phase: Phase) -> str:
    return (
        f"{to_phase.value} is not ahead of {from_phase.value} in this season: /advance-force "
        "only moves a league forward along the season's route, never back to an earlier phase "
        "and never across the season boundary (Phase 7's rollover is not built). Going back "
        "would replay phase work and history rows that already exist."
    )


def force_target_refusal(from_phase: Phase, to_phase: Phase) -> str | None:
    """The reason `/advance-force` refuses moving from `from_phase` to `to_phase` whatever the
    exit gate says, or `None` when the move is a legal forced one. Force overrides blockers, not
    geometry: the target must be strictly FORWARD along the season route, by `graph.route_to`'s
    own rule (a route exists within one season, never into `SETUP`, never across `WAIVERS ->
    PRESEASON`). Checked in the order a person would want it explained: the two phases that are
    never a target (`PRESEASON`, `SETUP`), the phase the league is in, then everything not
    ahead. Each has its own sentence; a raw payload can name any phase, so this is the service's
    rule and not the command's choice list."""
    if to_phase is Phase.PRESEASON:
        return FORCE_PRESEASON_MESSAGE
    if to_phase is Phase.SETUP:
        return FORCE_SETUP_MESSAGE
    if to_phase is from_phase:
        return force_same_phase_message(from_phase)
    if route_to(from_phase, to_phase) is None:
        return force_not_forward_message(from_phase, to_phase)
    return None


def force_schedule_missing_message(from_phase: Phase, to_phase: Phase, season: int) -> str:
    """The refusal for a forced move with no regular-season schedule to play, in the four
    places the league can stand (out of `PRESEASON`, out of a schedule phase, into a schedule
    phase, or out of `SETUP` to any other phase). **The reason is that the season would be
    stranded:** once the pointer leaves `PRESEASON`, `SETUP` or a schedule phase with no schedule,
    nothing in the bot can ever create or play those games -- there is no route back to
    `PRESEASON`, a block press refuses a season with none, and `SCHEDULE_MISSING` closes the
    gate -- so the league has no in-bot exit."""
    stranded = (
        "once the league moved on, nothing in the bot could ever create or play those games "
        "(there is no route back), so the season would be stranded with no in-bot exit"
    )
    if from_phase is Phase.PRESEASON:
        return (
            f"season {season} has no regular-season schedule yet -- PRESEASON's entry work "
            f"builds it -- so /advance-force will not move the league out of PRESEASON: "
            f"{stranded}. Run /advance instead: it finishes PRESEASON's entry work, which builds "
            "the schedule, and then enters REGULAR_SEASON."
        )
    if from_phase in SCHEDULE_CONSUMING_PHASES:
        return (
            f"{from_phase.value} plays the regular-season schedule and season {season} has none, "
            f"so /advance-force will not move the league out of it: {stranded}. Nothing "
            "generates the schedule after PRESEASON's entry work, so this league needs its games "
            "created (tools/) before it can move."
        )
    if to_phase in SCHEDULE_CONSUMING_PHASES:
        return (
            f"{to_phase.value} plays the regular-season schedule and season {season} has none, "
            f"so /advance-force will not move the league into it from {from_phase.value}: "
            f"{stranded}. Run /advance to reach PRESEASON, whose entry work builds the schedule."
        )
    return (
        f"season {season} has no regular-season schedule yet -- PRESEASON's entry work builds "
        f"it -- so /advance-force will not move the league out of {from_phase.value} to "
        f"{to_phase.value}: {stranded}. "
        "Run /advance to reach PRESEASON, whose entry work builds the schedule."
    )


def force_needs_a_schedule(from_phase: Phase, to_phase: Phase) -> bool:
    """Whether a forced `from_phase -> to_phase` move is one the missing-schedule refusal
    applies to (`force_schedule_missing_message`): out of `PRESEASON` or `SETUP` (the phases
    before the schedule exists -- the move skips the only entry work that builds it, whatever
    the target), out of or into a schedule phase. A league already past its schedule phases
    moving between later phases is not force's to police."""
    return (
        from_phase is Phase.PRESEASON
        or from_phase is Phase.SETUP
        or from_phase in SCHEDULE_CONSUMING_PHASES
        or to_phase in SCHEDULE_CONSUMING_PHASES
    )


def no_route_message(from_phase: Phase, to_phase: Phase) -> str:
    return (
        f"There is no route from {from_phase.value} to {to_phase.value} within this season: "
        "/advance-to only goes forward, never crosses the season boundary, and never targets "
        "SETUP."
    )


@dataclass(frozen=True)
class AdvanceToReport:
    """What `/advance-to` did. `entered` is the phases it moved into, in order, and
    `reached` is whether the last of them is `requested` (also true, with nothing entered,
    when the league was already there). When it stopped short, `stopped_before` is the
    phase it would not enter next and `stop_reason` says why: that phase does work (which
    `/advance-to` never runs), or the exit gate refused leaving the phase it stood in
    (`blockers` then names every reason). It sweeps no trade offers: a stale offer is
    already refused by phase, and the press's own sweep (F4) is the one caller of
    `expire_stale_offers_for_league` this stage adds."""

    from_phase: Phase
    requested: Phase
    entered: tuple[Phase, ...]
    reached: bool
    stopped_before: Phase | None = None
    stop_reason: str | None = None
    blockers: tuple[Blocker, ...] = ()


@dataclass(frozen=True)
class ForceReport:
    """What `/advance-force` did. `overrode` is every blocker the exit gate (and, entering
    `DRAFT`, the board check) would have raised; `skipped_work` names each unit of the
    league's CURRENT entry work that was still owed and is now skipped permanently -- the
    prefix only ever looks at the current entry. `recorded_reason` is exactly what
    `phase_history.reason` holds: the commissioner's reason plus both lists."""

    from_phase: Phase
    to_phase: Phase
    overrode: tuple[Blocker, ...]
    skipped_work: tuple[str, ...]
    recorded_reason: str


def _work_description(phase: Phase) -> str:
    """What an Entry press into `phase` would do, from the same tables the press reads."""
    parts: list[str] = []
    if phase_has_work(WorkKind.ENTRY, phase):
        parts.append("entry work")
    if phase in CYCLE_BEARING_PHASES:
        parts.append("an AI hook")
    if phase_has_work(WorkKind.CLOSING, phase):
        parts.append("closing work (a media cycle or the stats rebuild)")
    return ", ".join(parts) or "work"


_OWED_ENTRY_WORK = "entry work"
_OWED_HOOK = "AI hook"
_OWED_CLOSING_WORK = "closing work"


def _owed_work(league: League, pointer: _PointerMove | None) -> tuple[str, ...]:
    """The units of the league's CURRENT entry that are still owed, in the order a press does
    them: entry work, the AI hook, closing work. **The one definition** `/advance-to`'s refusal
    to start and `/advance-force`'s "walked away from" list both read, so the two commands can
    never disagree about what is owed. Entry and closing work are B-1's predicate
    (`work_outstanding`: the phase OWNS work of that kind AND its checkpoint is behind the
    entry -- read as a bare `checkpoint < index` it would fire at every no-op phase), and the
    hook is owed when its cycle's checkpoint is behind the entry's ordinal. `()` for a phase
    that reads no pointer (`_needs_pointer`), which owes nothing."""
    if pointer is None:
        return ()
    phase = league.current_phase
    owed: list[str] = []
    if work_outstanding(
        WorkKind.ENTRY,
        phase,
        checkpoint=league.entry_work_completed_index,
        entry_index=pointer.entry_index,
    ):
        owed.append(_OWED_ENTRY_WORK)
    if (
        pointer.cycle_ordinal is not None
        and league.hook_completed_cycle_index < pointer.cycle_ordinal
    ):
        owed.append(_OWED_HOOK)
    if work_outstanding(
        WorkKind.CLOSING,
        phase,
        checkpoint=league.closing_work_completed_index,
        entry_index=pointer.entry_index,
    ):
        owed.append(_OWED_CLOSING_WORK)
    return tuple(owed)


def _refuse_a_start_with_owed_work(league: League, pointer: _PointerMove | None) -> None:
    """P5-99.f: `/advance-to` REFUSES TO START while the current entry has owed work
    (`_owed_work`). A command whose whole promise is "this does no work" must neither quietly
    finish a crashed agency checkpoint or hook nor walk past one (F16): a step out of the entry
    leaves it permanently unfinished, because the work prefix only ever looks at the current
    entry. **The one exception is the hook JC-13 defers at `REGULAR_SEASON`**: it is owed by
    design until a block press has played enough of the schedule, not because anything crashed,
    and pressing `/advance` would not run it now either -- so it does not refuse a start.
    (`/advance-force` still names it: leaving `REGULAR_SEASON` early really does skip it.)"""
    phase = league.current_phase
    owed = [
        unit
        for unit in _owed_work(league, pointer)
        if not (unit == _OWED_HOOK and phase in _HOOK_DEFERRED_TO_BLOCK)
    ]
    if owed:
        raise PhaseError(
            f"{phase.value} still owes its {' and '.join(owed)}, and /advance-to does no work: "
            "press /advance once -- it finishes that first -- then run /advance-to again."
        )


async def advance_to(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, to_phase: Phase
) -> AdvanceToReport:
    """`/advance-to <phase>` (P5-99.f): walks the phases whose Entry press does nothing --
    the parent's eight (`graph._NO_OP_PHASES`) -- and never executes a working phase's press.
    It steps while the NEXT press would be a no-op Entry press and stops, possibly short of
    `to_phase`, the moment the next one would do work (or the exit gate refuses leaving the
    phase it stands in), saying which phase stopped it and why. Runs no hook, no phase work,
    no sim, posts nothing. Commissioner-only (checked here as well as in the cog), inside the
    advance claim, and bounded by the route's length."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        action="advance to a phase",
    )
    return await _run_under_claim(
        pool,
        league_id,
        actor_user_id,
        lambda: _advance_to_claimed(
            pool, league_id, actor_user_id=actor_user_id, to_phase=to_phase
        ),
    )


async def _advance_to_claimed(
    pool: asyncpg.Pool, league_id: int, *, actor_user_id: int, to_phase: Phase
) -> AdvanceToReport:
    league = await league_repo.get_league(pool, league_id)  # read again, now that we hold the claim
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    start, season = league.current_phase, league.current_season
    if start is Phase.WAIVERS:
        raise PhaseError(WAIVERS_EDGE_MESSAGE)
    route = route_to(start, to_phase)
    if route is None:
        raise PhaseError(no_route_message(start, to_phase))
    if not route:
        return AdvanceToReport(from_phase=start, requested=to_phase, entered=(), reached=True)
    if _needs_pointer(start):
        pointer = await _read_pointer_locked(
            pool, league_id, expected_phase=start, expected_season=season
        )
        _refuse_a_start_with_owed_work(league, pointer)

    entered: list[Phase] = []
    current = start
    stopped_before: Phase | None = None
    stop_reason: str | None = None
    blockers: tuple[Blocker, ...] = ()
    for step in route:
        # The gate FIRST: a league that cannot leave its phase is told why, not told to press
        # `/advance` to enter a working phase that `/advance` would then refuse (critic 3, m4).
        verdict = evaluate_exit(await load_gate_facts(pool, league))
        if not verdict.allowed:
            stopped_before = step
            blockers = verdict.blockers
            stop_reason = f"{current.value} cannot be left yet."
            break
        if step not in _NO_OP_PHASES:
            stopped_before = step
            stop_reason = (
                f"the next phase, {step.value}, does work ({_work_description(step)}), and "
                "/advance-to never runs work: press /advance to enter it."
            )
            break
        await _move_pointer(
            pool,
            league_id,
            expected_phase=current,
            expected_season=season,
            to_phase=step,
            actor_user_id=actor_user_id,
        )
        entered.append(step)
        current = step
        refreshed = await league_repo.get_league(pool, league_id)
        if refreshed is None:
            raise NotFoundError(f"league {league_id} does not exist")
        league = refreshed
    return AdvanceToReport(
        from_phase=start,
        requested=to_phase,
        entered=tuple(entered),
        reached=stopped_before is None and bool(entered) and entered[-1] is to_phase,
        stopped_before=stopped_before,
        stop_reason=stop_reason,
        blockers=blockers,
    )


def _outstanding_work_names(league: League, pointer: _PointerMove | None) -> tuple[str, ...]:
    """Each unit of the league's CURRENT entry work still owed -- what a forced move walks
    away from, named with its phase. Read from `_owed_work`, the definition `/advance-to`'s
    refusal to start reads too."""
    return tuple(f"{league.current_phase.value} {unit}" for unit in _owed_work(league, pointer))


def _forced_reason(reason: str, overrode: Iterable[Blocker], skipped: Iterable[str]) -> str:
    """The audited reason: the commissioner's own, then -- only when there is something to
    say -- every blocker overridden and every unit of work skipped, so `phase_history` is
    where a forced move's consequences are written down (M-1)."""
    text = reason
    overrode_messages = [b.message for b in overrode]
    if overrode_messages:
        text += f" [overrode: {'; '.join(overrode_messages)}]"
    skipped_names = list(skipped)
    if skipped_names:
        text += f" [skipped: {'; '.join(skipped_names)}]"
    return text


async def force(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, to_phase: Phase, reason: str
) -> ForceReport:
    """`/advance-force <phase> <reason>` (P5-99.g): a pointer move, not a press -- inside the
    advance claim, through `phase_service.force_advance` (which writes `forced = true` and the
    reason), running no hook, no phase work, no sim, no media, and writing no checkpoint. It
    does not run the work prefix and does not refuse on outstanding work -- it is the
    override -- but the report and the audit row name every blocker it overrode and every
    unit of owed work it walks away from.

    Refuses even under force (each with its own sentence): a target that is not strictly
    forward along the season route (`force_target_refusal`: `PRESEASON` (F8), `SETUP`, the
    phase it is in, any earlier phase), and a league with no regular-season schedule that is in
    `PRESEASON`, `SETUP` or a schedule phase, or would enter one (`force_needs_a_schedule`,
    `force_schedule_missing_message`: once it moves on nothing in the bot can create or play
    those games, so the season is stranded).
    An unmet games target is NOT among them: it is overridden and named. The target rule is
    checked here from the row read above so a refusal takes no claim, and again under the claim,
    which is the authoritative read. Commissioner-only (checked here as well as in the cog)."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        action="force a phase change",
    )
    trimmed = reason.strip()
    if not trimmed:
        raise ValidationError(
            "a reason is required to force a phase change: it is written to the audit log"
        )
    refusal = force_target_refusal(league.current_phase, to_phase)
    if refusal is not None:
        raise PhaseError(refusal)
    return await _run_under_claim(
        pool,
        league_id,
        actor_user_id,
        lambda: _force_claimed(
            pool, league_id, actor_user_id=actor_user_id, to_phase=to_phase, reason=trimmed
        ),
    )


async def _force_claimed(
    pool: asyncpg.Pool, league_id: int, *, actor_user_id: int, to_phase: Phase, reason: str
) -> ForceReport:
    league = await league_repo.get_league(pool, league_id)  # read again, now that we hold the claim
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    from_phase, season = league.current_phase, league.current_season
    refusal = force_target_refusal(from_phase, to_phase)
    if refusal is not None:
        raise PhaseError(refusal)
    if force_needs_a_schedule(from_phase, to_phase) and await _season_has_no_schedule(pool, league):
        raise PhaseError(force_schedule_missing_message(from_phase, to_phase, season))
    verdict = evaluate_exit(await load_gate_facts(pool, league))
    overrode = list(verdict.blockers)
    if to_phase is Phase.DRAFT:
        overrode.extend(draft_board_blockers(await load_draft_board_facts(pool, league)))
    pointer = (
        await _read_pointer_locked(
            pool, league_id, expected_phase=from_phase, expected_season=season
        )
        if _needs_pointer(from_phase)
        else None
    )
    skipped = _outstanding_work_names(league, pointer)
    recorded = _forced_reason(reason, overrode, skipped)
    await _force_pointer(
        pool,
        league_id,
        expected_phase=from_phase,
        expected_season=season,
        to_phase=to_phase,
        actor_user_id=actor_user_id,
        reason=recorded,
    )
    return ForceReport(
        from_phase=from_phase,
        to_phase=to_phase,
        overrode=tuple(overrode),
        skipped_work=skipped,
        recorded_reason=recorded,
    )


async def _force_pointer(
    pool: asyncpg.Pool,
    league_id: int,
    *,
    expected_phase: Phase,
    expected_season: int,
    to_phase: Phase,
    actor_user_id: int,
    reason: str,
) -> None:
    """`_move_pointer`'s forced twin: `set_lock_timeout` first, the `leagues` row lock we
    take ourselves, the phase-agreement abandon (the verdict the report names was about a
    state that no longer holds otherwise), then `phase_service.force_advance` -- whose own
    `FOR UPDATE` is a free re-acquisition and which writes `forced = true` and `reason`."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _lock_and_count_entries(
            conn, league_id, expected_phase=expected_phase, expected_season=expected_season
        )
        await phase_service.force_advance(conn, league_id, to_phase, actor_user_id, reason)


# --- postpone / restore (P5-99.u item 5, P5-99.u.1, P5-99.u.2, P5-99.t.1) -----------
#
# The one recoverable path out of a game that cannot sim (F11). Both are human writes,
# so both read the advance claim: `sim_scheduled_games` snapshots its game list before
# a block runs, and a postponement committing mid-block would turn that game into a
# sim FAILURE (`finalize_game` raises on a `postponed` row) instead of a clean skip.
# With the claim check the write simply waits and refuses with `CLAIM_HELD_MESSAGE`.
# Each is two functions: the public one checks the commissioner and the reason, the
# private one is the transaction (`set_lock_timeout` first, then the claim check --
# check 10 pins both by qualname).


@dataclass(frozen=True)
class GameStatusChange:
    """What a postpone/restore did. `changed=False` means somebody else already moved
    the game (`game_repo.set_status`'s `AND status = $2` guard matched no row): that
    is reported, never raised, and writes no audit row."""

    game_id: int
    from_status: GameStatus
    to_status: GameStatus
    changed: bool
    transaction_id: int | None


async def _authorize_game_status_change(
    pool: asyncpg.Pool, league_id: int, actor_user_id: int, reason: str, *, action: str
) -> str:
    """The checks that need no lock: the league exists, the actor is its commissioner,
    the reason is not blank. Returns the reason trimmed."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action=action
    )
    trimmed = reason.strip()
    if not trimmed:
        raise ValidationError(f"a reason is required to {action}: it is written to the audit log")
    return trimmed


async def _game_of_league(conn: asyncpg.Connection, league_id: int, game_id: int) -> Game:
    game = await game_repo.get_game(conn, game_id)
    if game is None or game.league_id != league_id:
        raise NotFoundError(f"game {game_id} does not exist in this league")
    return game


def _require_current_regular_season_game(game: Game, state: LeagueClaimState, *, verb: str) -> None:
    """A postponement and its reversal are about the schedule the exit gate, the block
    cursor and the stats rebuild read: the CURRENT season's REGULAR games, and nothing
    else. Any other game (an earlier season's, a play-in or playoff game) has none of
    those readers, so moving it would be a write nobody sees -- and a restored playoff
    game would be met cold by Phase 6."""
    if game.season != state.current_season:
        raise PhaseError(
            f"game {game.id} belongs to season {game.season}, not the league's current season "
            f"{state.current_season}, so it cannot be {verb}: only a game of the current "
            "season's regular-season schedule can be"
        )
    if game.game_type is not GameType.REGULAR:
        raise PhaseError(
            f"game {game.id} is a {game.game_type.value} game, so it cannot be {verb}: only a "
            "regular-season game can be (the play-in and playoffs are not this schedule)"
        )


def _require_legal_game_move(game: Game, to_status: GameStatus, *, verb: str, only: str) -> None:
    """`game_repo.set_status` does not consult `GAME_TRANSITIONS` (its docstring says
    the caller does), so this caller does (P5-99.u.2). `finalize_game` is the
    precedent for what a caller that skips it gets."""
    if not entity_states.is_legal_transition(
        entity_states.GAME_TRANSITIONS, game.status, to_status
    ):
        raise ValidationError(
            f"game {game.id} is {game.status.value}, so it cannot be {verb}: only {only} can be"
        )


async def _write_game_status_change(
    conn: asyncpg.Connection,
    *,
    state: LeagueClaimState,
    game: Game,
    to_status: GameStatus,
    txn_type: TransactionType,
    actor_user_id: int,
    reason: str,
) -> GameStatusChange:
    """The CAS write and its audit row, in the caller's transaction. A zero-row update
    means somebody else already moved the game: reported, nothing logged."""
    moved = await game_repo.set_status(conn, game.id, from_status=game.status, to_status=to_status)
    if not moved:
        log.warning(
            "season_service: game %s was no longer %s when the %s landed; nothing changed",
            game.id,
            game.status.value,
            txn_type.value,
        )
        return GameStatusChange(
            game_id=game.id,
            from_status=game.status,
            to_status=to_status,
            changed=False,
            transaction_id=None,
        )
    row = await txn_repo.create_league_transaction(
        conn,
        league_id=game.league_id,
        season=game.season,
        phase=state.current_phase,
        txn_type=txn_type,
        actor_user_id=actor_user_id,
        note=reason,
        details={
            "game_id": game.id,
            "game_index": game.game_index,
            "game_date": game.game_date.isoformat(),
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "from_status": game.status.value,
            "to_status": to_status.value,
        },
    )
    return GameStatusChange(
        game_id=game.id,
        from_status=game.status,
        to_status=to_status,
        changed=True,
        transaction_id=row.id,
    )


async def postpone_game(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, game_id: int, reason: str
) -> GameStatusChange:
    """`/advance-postpone-game`: moves a `scheduled` game to `postponed`, so
    `list_scheduled_games` stops returning it, the block cursor moves on and the gate's
    schedule blocker clears (P5-99.u item 5). Commissioner-only, audited with a
    `GAME_POSTPONED` transaction row carrying the reason, claim-checked. Nothing is
    invented -- no forfeit score: a season can then finish with a game unplayed, which
    is what a postponement means."""
    trimmed = await _authorize_game_status_change(
        pool, league_id, actor_user_id, reason, action="postpone a game"
    )
    return await _postpone_game(
        pool, league_id=league_id, actor_user_id=actor_user_id, game_id=game_id, reason=trimmed
    )


async def _postpone_game(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, game_id: int, reason: str
) -> GameStatusChange:
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            state = await league_claim.check_claim(
                conn, gate=None, league_id=league_id, require_phase_agreement=False
            )
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc
        game = await _game_of_league(conn, league_id, game_id)
        _require_current_regular_season_game(game, state, verb="postponed")
        _require_legal_game_move(
            game, GameStatus.POSTPONED, verb="postponed", only="a scheduled game"
        )
        return await _write_game_status_change(
            conn,
            state=state,
            game=game,
            to_status=GameStatus.POSTPONED,
            txn_type=TransactionType.GAME_POSTPONED,
            actor_user_id=actor_user_id,
            reason=reason,
        )


async def restore_game(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, game_id: int, reason: str
) -> GameStatusChange:
    """`/advance-restore-game`: the reversal of a postponement -- `postponed` back to
    `scheduled` -- audited with `GAME_RESTORED`. Not free: it re-opens a schedule the
    league may have moved past, so it carries three refusals (P5-99.u.1), each a
    `PhaseError` naming what is in the way -- outside the four schedule-consuming
    phases; at `REGULAR_SEASON_COMPLETE` once its stats are rebuilt (M-2); onto a game
    date the league has already played past (M-3: a LATER date holds a final game -- a
    date the block press only half-played is where the cursor still stands, and is
    restorable). Like a postponement it refuses any game that is not a current-season
    regular-season game."""
    trimmed = await _authorize_game_status_change(
        pool, league_id, actor_user_id, reason, action="restore a game"
    )
    return await _restore_game(
        pool, league_id=league_id, actor_user_id=actor_user_id, game_id=game_id, reason=trimmed
    )


async def _restore_game(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, game_id: int, reason: str
) -> GameStatusChange:
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            state = await league_claim.check_claim(
                conn, gate=None, league_id=league_id, require_phase_agreement=False
            )
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(CLAIM_CONTENTION_MESSAGE) from exc
        game = await _game_of_league(conn, league_id, game_id)
        _require_current_regular_season_game(game, state, verb="restored")
        _require_legal_game_move(
            game, GameStatus.SCHEDULED, verb="restored", only="a postponed game"
        )
        await _refuse_restore_that_reopens_the_season(conn, state=state, game=game)
        return await _write_game_status_change(
            conn,
            state=state,
            game=game,
            to_status=GameStatus.SCHEDULED,
            txn_type=TransactionType.GAME_RESTORED,
            actor_user_id=actor_user_id,
            reason=reason,
        )


async def _refuse_restore_that_reopens_the_season(
    conn: asyncpg.Connection, *, state: LeagueClaimState, game: Game
) -> None:
    """P5-99.u.1's three refusals, in order, each a read over facts the transaction
    already holds under the `leagues` row's `FOR SHARE` lock (so the phase cannot move
    while they are checked)."""
    phase = state.current_phase
    if phase not in SCHEDULE_CONSUMING_PHASES:
        raise PhaseError(
            f"game {game.id} cannot be restored in {phase.value}: a restore re-opens the "
            "regular-season schedule, and the league is not in the regular season -- the exit "
            "gate has already been satisfied about it (or the schedule has not started)"
        )
    if phase is Phase.REGULAR_SEASON_COMPLETE:
        league = await league_repo.get_league(conn, game.league_id)
        if league is None:
            raise NotFoundError(f"league {game.league_id} does not exist")
        entries = await league_repo.count_phase_entries(conn, game.league_id, state.current_season)
        entry_index = derive_entry_index(entries, entering=False)
        if not work_outstanding(
            WorkKind.CLOSING,
            phase,
            checkpoint=league.closing_work_completed_index,
            entry_index=entry_index,
        ):
            raise PhaseError(
                f"game {game.id} cannot be restored: the season's stats were already rebuilt at "
                "the end of the regular season, and a game simmed now would never reach "
                "player_season_stats. Restore it before the last regular-season game is played "
                "(the rebuild is idempotent, so restoring one phase earlier reaches the same "
                "outcome)"
            )
    # Refusal 3's purpose is the block cursor never walking BACKWARDS onto a date the
    # league has left, so the evidence is a final game on a LATER date. A final game on
    # this very date is not: the block press finalizes what it can and leaves a failed
    # game scheduled, so a half-played date is exactly the state a postponed game is
    # restored into, and the next block is that same date -- the same block id, which
    # `open_block` merges (a games target that splits a day already relies on it).
    played_past = tuple(
        await game_repo.list_final_game_ids_after_date(
            conn, game.league_id, game.season, game.game_type, game.game_date
        )
    )
    if played_past:
        raise PhaseError(
            f"game {game.id} cannot be restored: the league has already played past its date "
            f"({game.game_date.isoformat()}) -- game(s) {_game_ids_sentence(played_past)} on a "
            "later date are already final. A restore now would walk the block cursor backwards "
            "onto a date whose block is closed"
        )


# --- /advance-unlock (P5-99.c) ----------------------------------------------------------
#
# A crash mid-press leaves `advance_claimed_at` set (R4) and nothing clears it but this:
# `on_ready` deliberately reports and repairs nothing, because a claim is not proof its
# presser is dead and clearing a live one gives two concurrent AI passes. The age
# threshold is what makes the clear SAFER, not safe: ten minutes is a heuristic, not proof the
# press is dead, and a live press whose claim is cleared runs beside the next one -- a phase's
# work or a cycle's hook can then run twice (`_run_hook_once`'s read, run, write is not
# atomic; the checkpoint write is monotone, not a mutex). That exposure is ACCEPTED (spec R4):
# a true fix (a claim heartbeat, or an advisory lock around the hook) is a design of its own.
# What the code does about it is say so, in both the refusal and the reply, and keep the
# threshold in one place (`min_age` overrides it for a caller that knows better -- an operator
# script, a test -- and is deliberately not a command option). The audit row is what makes a
# clear accountable.

# "Default 10 minutes" (P5-99.c). A module constant beside the other tunables of this
# module, not an environment-backed `Config` field: `core.config.Config` carries none.
ADVANCE_UNLOCK_MIN_AGE = timedelta(minutes=10)


@dataclass(frozen=True)
class UnlockResult:
    """What `/advance-unlock` did. `cleared=False` means there was no claim to clear
    (nothing written, no audit row). `message` is the sentence for the reply and states
    what `hook_completed_cycle_index` says -- the one fact a person needs to judge whether
    the dead press's hook still has to be finished, which the next `/advance` does before
    anything else (the work prefix)."""

    cleared: bool
    claimed_by: int | None
    claimed_at: datetime | None
    age: timedelta | None
    hook_completed_cycle_index: int
    current_phase: Phase
    transaction_id: int | None
    message: str


def unlock_too_young_message(
    *, claimed_by: int, age: timedelta, min_age: timedelta = ADVANCE_UNLOCK_MIN_AGE
) -> str:
    """The refusal for a claim under `min_age`: who holds it, how old it is, the threshold,
    and how long is left -- rounded UP, so a person told "1 minute" is never refused again for
    being 59 seconds short of the last one. It says to wait for a press that may still be
    running: unlocking a live one can run a phase's work twice."""
    threshold_minutes = int(min_age.total_seconds() // 60)
    remaining_minutes = max(1, math.ceil((min_age - age).total_seconds() / 60))
    return (
        f"The advance claim held by user {claimed_by} is only {int(age.total_seconds() // 60)} "
        f"minute(s) old, so its press may still be running -- wait for it: unlocking a live "
        f"press can run a phase's work twice. /advance-unlock clears a claim "
        f"once it is {threshold_minutes} minutes old -- try again in {remaining_minutes} "
        "minute(s)."
    )


async def unlock(
    pool: asyncpg.Pool,
    *,
    league_id: int,
    actor_user_id: int,
    min_age: timedelta = ADVANCE_UNLOCK_MIN_AGE,
) -> UnlockResult:
    """`/advance-unlock`: clears a stuck advance claim. Commissioner-only (checked here as
    well as in the cog, P5-99.a), refuses a claim younger than `min_age` (default
    `ADVANCE_UNLOCK_MIN_AGE`; the cog never passes one -- it is a service parameter, not a
    command option), and writes a `league_transactions` row of type `ADVANCE_UNLOCK` -- in the
    same transaction as the clear, so a cleared claim always has its record. It takes no
    press claim of its own: it is the one write that must work while one is held."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        action="unlock the advance claim",
    )
    return await _unlock_claim(
        pool, league_id=league_id, actor_user_id=actor_user_id, min_age=min_age
    )


def _hook_checkpoint_sentence(index: int) -> str:
    if index < 0:
        return "hook_completed_cycle_index is -1: no cycle's AI hook has completed yet"
    return (
        f"hook_completed_cycle_index is {index}: the AI hook of cycle {index} is the last that "
        "completed"
    )


async def _unlock_claim(
    pool: asyncpg.Pool, *, league_id: int, actor_user_id: int, min_age: timedelta
) -> UnlockResult:
    """The transaction: `set_lock_timeout` first, then the `leagues` row `FOR UPDATE`
    (the age is measured under the lock, so it is the age of the claim then cleared), the
    refusal if it is too young, the FENCED clear (`release_advance` with the exact claimant
    and timestamp just read -- under the row lock it cannot miss, and a miss would mean
    something else is wrong), and the audit row. A refusal raises out of the transaction,
    which rolls back: nothing is written."""
    async with transaction(pool) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            snapshot = await league_repo.get_advance_claim_for_update(conn, league_id)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(UNLOCK_CONTENTION_MESSAGE) from exc
        if snapshot is None:
            raise NotFoundError(f"league {league_id} does not exist")
        hook_sentence = _hook_checkpoint_sentence(snapshot.hook_completed_cycle_index)
        claimed_by, claimed_at, age = (
            snapshot.advance_claimed_by,
            snapshot.advance_claimed_at,
            snapshot.claim_age,
        )
        if claimed_by is None or claimed_at is None or age is None:
            return UnlockResult(
                cleared=False,
                claimed_by=None,
                claimed_at=None,
                age=None,
                hook_completed_cycle_index=snapshot.hook_completed_cycle_index,
                current_phase=snapshot.current_phase,
                transaction_id=None,
                message=(
                    f"No advance claim is held, so there is nothing to clear ({hook_sentence})."
                ),
            )
        if age < min_age:
            raise ConcurrencyError(
                unlock_too_young_message(claimed_by=claimed_by, age=age, min_age=min_age)
            )
        released = await league_repo.release_advance(conn, league_id, claimed_by, claimed_at)
        if not released:  # unreachable: we hold the row lock and read this exact claim
            raise RuntimeError(
                f"league {league_id}'s claim by user {claimed_by} could not be cleared under "
                "the row lock that read it"
            )
        age_minutes = int(age.total_seconds() // 60)
        row = await txn_repo.create_league_transaction(
            conn,
            league_id=league_id,
            season=snapshot.current_season,
            phase=snapshot.current_phase,
            txn_type=TransactionType.ADVANCE_UNLOCK,
            actor_user_id=actor_user_id,
            note=f"cleared the advance claim held by user {claimed_by} for {age_minutes} minute(s)",
            details={
                "claimed_by": claimed_by,
                "claimed_at": claimed_at.isoformat(),
                "age_seconds": int(age.total_seconds()),
                "hook_completed_cycle_index": snapshot.hook_completed_cycle_index,
            },
        )
        return UnlockResult(
            cleared=True,
            claimed_by=claimed_by,
            claimed_at=claimed_at,
            age=age,
            hook_completed_cycle_index=snapshot.hook_completed_cycle_index,
            current_phase=snapshot.current_phase,
            transaction_id=row.id,
            message=(
                f"Cleared the advance claim held by user {claimed_by} for {age_minutes} "
                f"minute(s); {hook_sentence}. If that press died mid-hook, the next /advance "
                "finishes it before anything else. If that press is in fact still running, do "
                "not press /advance until it finishes: unlocking a live press can run a phase's "
                "work twice."
            ),
        )
