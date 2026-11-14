"""Live I/O proof for `gm_command_service.draft_pick` -- `/draft`'s service
half, and the only function in this codebase with two transactions
(Decisions P5-98.g/h, docs/design/phase-5-stage-d-prime-draft.md §4 commit
6, §5 T9(b)/T13).

**What is actually at stake here is the gap between them.** The human's
pick commits in tx1; the AI resume runs in tx2; and in between, a Stage E
press can claim the league or the phase can move on. P5-90 rule 2's race,
arriving through a door P5-90 did not cover. The rule this file pins is
that such a gap SKIPS the resume and never fails the command -- the pick is
already durable, and telling a person their pick failed when it did not is
the one answer that is wrong.

**The claim is taken for real, by a second connection, never by a fixture
that just sets the column.** T9(b) holds an uncommitted `UPDATE leagues SET
advance_claimed_at` so the caller genuinely BLOCKS on the `leagues` row and
only then refuses -- a committed-claim fixture would pass against an
implementation that reads the claim without locking, which is exactly the
TOCTOU `check_claim`'s `FOR SHARE` exists to close. T13 uses the same lock
to make its race deterministic rather than timed: the claiming `UPDATE` is
issued while tx1 still holds `leagues FOR SHARE`, so Postgres' own lock
queue grants it the row the instant tx1 commits and tx2 sees a claim that
landed in the gap.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import asyncpg
import pytest

from dba2.core.errors import ConcurrencyError, PhaseError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    draft_repo,
    league_repo,
    player_repo,
    ratings_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.draft import DraftPick
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_command_service, gm_service, league_claim

_SEASON = 2026
_COMMISSIONER = 1
_TOR_MANAGER = 101
_CHI_MANAGER = 202
_A_STRANGER = 303

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int) -> dict[str, int]:
    from tests.fixtures.sim import make_ratings

    base = make_ratings(seed)
    return {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }


@dataclass(frozen=True)
class _Fixture:
    league_id: int
    draft_id: int
    teams: dict[str, int]
    picks: tuple[DraftPick, ...]
    prospects: tuple[int, ...]


async def _build(
    pool,
    *,
    picks: tuple[tuple[int, int | None, str], ...] = (
        (1, 30, "TOR"),
        (2, 31, "BOS"),
        (2, 32, "CHI"),
    ),
    managers: dict[str, int] | None = None,
    prospects: int = 3,
    phase: Phase = Phase.DRAFT,
) -> _Fixture:
    """A league in DRAFT with three teams: TOR (the person running the
    command), BOS (AI, so the resume has real work to do) and CHI (the next
    person, so the cycle has somewhere to stop). `picks` is `(round,
    pick_number, team_code)`; the default board's round boundary is the T2
    fixture's own, kept because a pointer bug shows up there and nowhere
    else."""
    from tests.fixtures.managers import birth_date_for_age

    managers = managers if managers is not None else {"TOR": _TOR_MANAGER, "CHI": _CHI_MANAGER}
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Command Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=2026_10,
        current_phase=phase,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=_SEASON,
        salary_cap=150_000_000,
        luxury_tax=180_000_000,
        apron_1=190_000_000,
        apron_2=200_000_000,
        max_pct_0_6=25,
        max_pct_7_9=30,
        max_pct_10_plus=35,
        supermax_pct=35,
        min_salary=1_200_000,
        mle_amount=13_000_000,
        bi_annual_amount=5_000_000,
        raise_pct_bird=8,
        raise_pct_non_bird=5,
        roster_min=14,
        roster_max=15,
    )
    teams: dict[str, int] = {}
    for code in ("TOR", "BOS", "CHI"):
        team = await team_repo.create_team(
            pool,
            league_id=league.id,
            code=code,
            name=f"{code} Team",
            city=f"{code} City",
            conference="East",
            division="Atlantic",
            home_market_size=3,
            manager_user_id=managers.get(code),
            gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
            timeline=Timeline.RETOOL,
        )
        teams[code] = team.id
    draft = await draft_repo.create_draft(
        pool, league_id=league.id, season=_SEASON, total_picks=len(picks)
    )
    pick_rows = []
    for round_, pick_number, code in picks:
        pick_rows.append(
            await draft_repo.create_draft_pick(
                pool,
                league_id=league.id,
                season=_SEASON,
                round=round_,
                original_team_id=teams[code],
                current_team_id=teams[code],
                pick_number=pick_number,
            )
        )
    prospect_ids = []
    for i in range(prospects):
        player = await player_repo.create_player(
            pool,
            league_id=league.id,
            first_name="Prospect",
            last_name=f"Number{i}",
            primary_position=Position.SF,
            height_in=79,
            weight_lb=215,
            wingspan_in=82,
            birth_date=birth_date_for_age(19, _SEASON),
            roster_status=RosterStatus.DRAFT_ELIGIBLE,
            overall_cached=70 - i,
            overall_computed_at=datetime.now(UTC),
            team_id=None,
        )
        await ratings_repo.upsert_player_ratings(
            pool,
            player_id=player.id,
            league_id=league.id,
            source=RatingSource.GENERATED,
            ratings=_ratings_columns(player.id),
        )
        prospect_ids.append(player.id)
    return _Fixture(
        league_id=league.id,
        draft_id=draft.id,
        teams=teams,
        picks=tuple(pick_rows),
        prospects=tuple(prospect_ids),
    )


async def _cleanup(pool, f: _Fixture) -> None:
    gm_service.artifact_path(f.league_id, _SEASON).unlink(missing_ok=True)
    await pool.execute("DELETE FROM leagues WHERE id = $1", f.league_id)


async def _pick(
    pool, f: _Fixture, *, team: str = "TOR", prospect: int = 0, actor: int | None = None
):
    return await gm_command_service.draft_pick(
        pool,
        league_id=f.league_id,
        team_id=f.teams[team],
        prospect_id=f.prospects[prospect],
        actor_user_id=_TOR_MANAGER if actor is None else actor,
    )


async def _selections(pool, f: _Fixture):
    return await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)


# ---------------------------------------------------------------------------
# The two transactions, together.
# ---------------------------------------------------------------------------


async def test_a_human_pick_commits_and_the_ai_resumes_to_the_next_person():
    """The whole command in one board: TOR's manager picks at round 1 pick
    30, the AI takes BOS's round 2 pick 31 in the resume, and the cycle
    stops at CHI -- a person -- rather than drafting through them. The
    decision log gets the AI's record and nothing for the human's own pick
    (P5-98.e: a `league_transactions` row is the human's record)."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = await _pick(pool, f)

            assert (outcome.pick.round, outcome.pick.pick_number) == (1, 30)
            assert outcome.pick.current_team_id == f.teams["TOR"]
            assert outcome.prospect_id == f.prospects[0]

            assert outcome.cycle.state is gm_service.DraftCycleState.WAITING_ON_HUMAN
            assert outcome.cycle.team_id == f.teams["CHI"]
            assert outcome.cycle.manager_user_id == _CHI_MANAGER
            assert (outcome.cycle.round, outcome.cycle.pick_number) == (2, 32)
            assert outcome.cycle.draft is not None and len(outcome.cycle.draft.picks) == 1

            selections = await _selections(pool, f)
            assert [s.pick_number for s in selections] == [30, 31]
            assert len({s.player_id for s in selections}) == 2

            txns = await txn_repo.list_transactions_for_league(pool, f.league_id)
            assert len(txns) == 1
            assert txns[0].actor_user_id == _TOR_MANAGER

            # The cycle's records are written AFTER tx2 commits, by the
            # command and not by the cycle -- one line, the AI's pick.
            artifact = gm_service.artifact_path(f.league_id, _SEASON)
            assert artifact.exists()
            assert len(artifact.read_text(encoding="utf-8").splitlines()) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_pick_that_finishes_the_draft_reports_complete():
    """The resume's other ordinary ending, and the one Stage E's exit gate
    reads: no pick is left unused, so the draft really is over."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, 2, "BOS")), prospects=4)
        try:
            outcome = await _pick(pool, f)

            assert outcome.cycle.state is gm_service.DraftCycleState.COMPLETE
            assert outcome.cycle.reason is None
            picks = await draft_repo.list_draft_picks_for_season(pool, f.league_id, _SEASON)
            assert all(p.used_for_player_id is not None for p in picks)
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_pick_whose_resume_finds_no_prospects_left_is_stalled_not_failed():
    """`STALLED(pool_exhausted)` still carries a committed pick: the person
    picked the last prospect, and the AI behind them has nobody. Nothing
    retries, and the command does not raise -- the reason is reported."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, 2, "BOS")), prospects=1)
        try:
            outcome = await _pick(pool, f)

            assert outcome.cycle.state is gm_service.DraftCycleState.STALLED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.POOL_EXHAUSTED
            assert outcome.cycle.team_id == f.teams["BOS"]
            assert len(await _selections(pool, f)) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Who may run it, and when (the service's own half of P5-38's split).
# ---------------------------------------------------------------------------


async def test_the_command_is_refused_outside_the_draft_phase():
    """`COMMAND_GATES["draft"]` is the single-phase entry P5-98.j rules,
    and the service checks it itself -- twice, once before the gate and
    once against `gate.phase` -- rather than trusting the bot layer's."""
    pool = await get_pool()
    try:
        f = await _build(pool, phase=Phase.POST_DRAFT_TRADES)
        try:
            with pytest.raises(PhaseError):
                await _pick(pool, f)
            assert await _selections(pool, f) == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_stranger_is_refused_before_anything_is_locked():
    """P5-38's service-layer half: neither the team's manager nor the
    commissioner, so the command never reaches a transaction."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            with pytest.raises(PermissionDenied):
                await _pick(pool, f, actor=_A_STRANGER)
            assert await _selections(pool, f) == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_commissioner_may_pick_for_a_human_managed_team_on_the_clock():
    """P5-48's override, through the same function with a different
    `actor_user_id` -- and the `league_transactions` row is what tells the
    two apart afterwards (P5-98.f)."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = await _pick(pool, f, actor=_COMMISSIONER)

            assert outcome.pick.current_team_id == f.teams["TOR"]
            txns = await txn_repo.list_transactions_for_league(pool, f.league_id)
            assert [t.actor_user_id for t in txns] == [_COMMISSIONER]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# T9(b) -- a press in flight, held for real.
# ---------------------------------------------------------------------------


async def _hold_claim(pool, league_id: int, *, held: asyncio.Event, release: asyncio.Event):
    """Connection 2: an UNCOMMITTED `UPDATE leagues SET advance_claimed_at`
    -- what a Stage E press does. Holds the `leagues` row lock until
    `release` is set, then commits, exactly D'-26's own shape."""
    from dba2.data.db import transaction

    async with transaction(pool) as conn:
        await conn.execute(
            "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 WHERE id = $1",
            league_id,
        )
        held.set()
        await release.wait()


async def test_a_press_in_flight_blocks_the_pick_and_then_refuses_it():
    """T9(b). The claim is held UNCOMMITTED, so `check_claim`'s `leagues
    ... FOR SHARE` genuinely waits on the row rather than reading a
    committed column -- a fixture that merely sets `advance_claimed_at`
    would pass against an implementation with no row lock at all, which is
    the TOCTOU P5-90 rule 2 is about. The message is asserted: any other
    `ConcurrencyError` (the gate's, the `drafts` row's) would satisfy a
    bare type match and prove nothing."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        held, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(_hold_claim(pool, f.league_id, held=held, release=release))
        try:
            await held.wait()
            task = asyncio.create_task(_pick(pool, f))
            done, _pending = await asyncio.wait({task}, timeout=0.5)
            assert not done, "the pick did not block on the leagues row -- the claim TOCTOU is open"

            release.set()
            await holder

            with pytest.raises(ConcurrencyError) as excinfo:
                await task
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE

            assert await _selections(pool, f) == []
            assert await txn_repo.list_transactions_for_league(pool, f.league_id) == []
        finally:
            release.set()
            await holder
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def _hold_the_drafts_row(pool, f: _Fixture, holding: asyncio.Event, release: asyncio.Event):
    """A second connection holding the season's `drafts` row `FOR UPDATE` in
    an uncommitted transaction, in its own task -- a holder that never
    suspends is not a holder at all."""
    from dba2.data.db import transaction

    async with transaction(pool) as conn:
        await draft_repo.get_draft_for_season_for_update(conn, f.league_id, _SEASON)
        holding.set()
        await release.wait()


async def test_a_pick_waits_for_the_drafts_row_and_then_says_so_in_the_drafts_words():
    """tx1's own copy of T9(a)'s assertion, and the reason the mapping is
    written at the statement in BOTH places: `maps_lock_contention` (which
    this function also carries, as the backstop) would answer a `drafts`-row
    wait with "That team/player is being updated...", the wrong noun for a
    row that is neither. The wait is bounded by the gate's own
    `lock_timeout`, which is the other half of why the gate is opened for a
    command that mutates no RMLP table."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        holding, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(_hold_the_drafts_row(pool, f, holding, release))
        try:
            await holding.wait()

            started = time.perf_counter()
            with pytest.raises(ConcurrencyError) as excinfo:
                await _pick(pool, f)
            waited = time.perf_counter() - started

            assert str(excinfo.value) == gm_service.DRAFT_CONTENTION_MESSAGE
            assert waited >= 2.5, f"returned in {waited:.2f}s -- it did not wait for the row"
            assert await _selections(pool, f) == []
        finally:
            release.set()
            await holder
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# T13 -- the gap between tx1 and tx2.
# ---------------------------------------------------------------------------


async def _pick_with_a_racer_in_the_gap(pool, f: _Fixture, race, *, release=None):
    """Runs `draft_pick` with `race` running between its two transactions --
    by the `leagues` row's own lock queue, not by a sleep in the hope of
    winning one.

    tx1 is paused inside `apply_human_draft_pick`, where it still holds
    `leagues ... FOR SHARE`. `race(tx1_applied, racer_blocked)` is told when
    that has happened and must, in turn, set `racer_blocked` once whatever
    it means to do is genuinely QUEUED on a row tx1 holds -- Postgres then
    grants it that row the instant tx1 commits, and tx2 meets it there. So
    tx2 meets a real racer with no timing assumption beyond the lock queue
    being a queue.

    `release` is set once `draft_pick` has returned, for a racer that HOLDS
    a lock rather than taking and dropping one: such a racer must also bound
    its own hold (a hold that only ends on `release` deadlocks whenever the
    bug under test is that `draft_pick` waits forever)."""
    tx1_applied, racer_blocked = asyncio.Event(), asyncio.Event()
    real = gm_service.apply_human_draft_pick

    async def _pausing_applier(*args, **kwargs):
        applied = await real(*args, **kwargs)
        tx1_applied.set()
        await racer_blocked.wait()
        return applied

    racer = asyncio.create_task(race(tx1_applied, racer_blocked))
    try:
        with patch.object(
            gm_command_service, "apply_human_draft_pick", side_effect=_pausing_applier
        ):
            return await _pick(pool, f)
    finally:
        racer_blocked.set()
        if release is not None:
            release.set()
        await racer


async def _pick_with_something_landing_in_the_gap(pool, f: _Fixture, statement: str):
    """`statement` (a press's claim, or a phase move) committed between
    `draft_pick`'s two transactions -- the harness above with a racer that
    takes the `leagues` row and lets it go again."""

    async def _race_into_the_gap(tx1_applied, racer_blocked):
        await tx1_applied.wait()
        writing = asyncio.create_task(pool.execute(statement, f.league_id))
        try:
            await asyncio.sleep(0.4)
            assert not writing.done(), (
                "the racing write did not block on the leagues row, so it cannot be "
                "guaranteed to land in the gap rather than after tx2 read"
            )
        finally:
            racer_blocked.set()
        await writing

    return await _pick_with_a_racer_in_the_gap(pool, f, _race_into_the_gap)


async def test_a_claim_taken_in_the_gap_skips_the_resume_and_keeps_the_pick():
    """T13. The human's pick is already durable when the press claims the
    league, so the resume is SKIPPED and the command still succeeds: raising
    here would tell that person their pick failed when it did not. Stage E's
    orchestrator continues the draft from its own hook."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = await _pick_with_something_landing_in_the_gap(
                pool,
                f,
                "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 "
                "WHERE id = $1",
            )

            assert outcome.cycle.state is gm_service.DraftCycleState.SKIPPED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.CLAIM_HELD
            assert outcome.cycle.draft is None and outcome.cycle.records == ()

            # The pick stands, and the AI made none.
            selections = await _selections(pool, f)
            assert [s.pick_number for s in selections] == [30]
            assert selections[0].team_id == f.teams["TOR"]
            assert len(await txn_repo.list_transactions_for_league(pool, f.league_id)) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_phase_that_moves_in_the_gap_skips_the_resume_and_keeps_the_pick():
    """T13's twin, and the other half of tx2's re-validation: the league
    left DRAFT between the two transactions, so the resume has no business
    running -- but the pick it follows was legal when it was made and
    stays."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = await _pick_with_something_landing_in_the_gap(
                pool, f, "UPDATE leagues SET current_phase = 'POST_DRAFT_TRADES' WHERE id = $1"
            )

            assert outcome.cycle.state is gm_service.DraftCycleState.SKIPPED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.PHASE_MOVED
            assert [s.pick_number for s in await _selections(pool, f)] == [30]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# The resume is best-effort: nothing it can do may fail a committed pick
# (round-1 critic, blocking issues B1 and B2).
# ---------------------------------------------------------------------------

_A_HOLD_LONGER_THAN_THE_RESUME_MAY_WAIT = 8.0
"""How long the racers below keep a row the resume wants. Longer than the
3-second bound the resume is supposed to wait under, so a resume that waits
for the HOLDER rather than for its own `lock_timeout` is visible as time."""

_THE_RESUMES_OWN_BOUND_PLUS_SLACK = 6.0
"""`SHARED_LOCK_TIMEOUT` (3s) plus tx1's own work and the harness's 0.4s
queue check -- comfortably under the hold above, so this discriminates."""


async def _holding_racer(pool, f: _Fixture, release: asyncio.Event, take):
    """A racer for `_pick_with_a_racer_in_the_gap` that takes a lock tx1
    already holds and KEEPS it -- queued while tx1 is paused (asserted), so
    Postgres grants it the instant tx1 commits and tx2 arrives to find it
    held. Its own hold is bounded, so a `draft_pick` that waits forever on
    it still ends and the test still reports."""

    async def _race(tx1_applied, racer_blocked):
        from dba2.data.db import transaction

        await tx1_applied.wait()
        async with transaction(pool) as conn:
            taking = asyncio.create_task(take(conn))
            try:
                await asyncio.sleep(0.4)
                assert not taking.done(), (
                    "the racer did not block on a row tx1 holds, so it cannot be "
                    "guaranteed to hold that row while tx2 runs"
                )
            finally:
                racer_blocked.set()
            await taking
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(release.wait(), _A_HOLD_LONGER_THAN_THE_RESUME_MAY_WAIT)

    return _race


async def test_a_leagues_row_held_in_the_gap_bounds_the_resumes_wait():
    """B1. tx2 takes `leagues FOR SHARE` (through `check_claim`) in a
    transaction that opens no gate -- so nothing sets `lock_timeout` for it
    unless tx2 does, and the server default is wait-forever. A press that
    holds the `leagues` row for 8 seconds must not hold a pool connection
    and a person's `/draft` reply for 8 seconds with it: the wait is bounded
    at `SHARED_LOCK_TIMEOUT`, and the resume it stops is SKIPPED, not
    raised."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        release = asyncio.Event()
        try:
            race = await _holding_racer(
                pool,
                f,
                release,
                lambda conn: conn.execute(
                    "UPDATE leagues SET name = name WHERE id = $1", f.league_id
                ),
            )
            started = time.perf_counter()
            outcome = await _pick_with_a_racer_in_the_gap(pool, f, race, release=release)
            waited = time.perf_counter() - started

            assert waited < _THE_RESUMES_OWN_BOUND_PLUS_SLACK, (
                f"the command took {waited:.2f}s -- it waited for the holder to let go "
                "instead of for its own lock_timeout"
            )
            assert outcome.cycle.state is gm_service.DraftCycleState.SKIPPED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.RESUME_CONTENDED
            assert [s.pick_number for s in await _selections(pool, f)] == [30]
        finally:
            release.set()
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_drafts_row_held_in_the_gap_skips_the_resume_and_keeps_the_pick():
    """B2's contention arm, and the gap this command's own docstring used to
    disclose rather than close: the resume loses the race for the `drafts`
    row, which is a `ConcurrencyError` AFTER the human's pick has committed.
    Raising it told that person their pick failed when it stands; it is a
    SKIPPED resume with a reason instead."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        release = asyncio.Event()
        try:
            race = await _holding_racer(
                pool,
                f,
                release,
                lambda conn: draft_repo.get_draft_for_season_for_update(conn, f.league_id, _SEASON),
            )
            started = time.perf_counter()
            outcome = await _pick_with_a_racer_in_the_gap(pool, f, race, release=release)
            waited = time.perf_counter() - started

            assert waited < _THE_RESUMES_OWN_BOUND_PLUS_SLACK
            assert outcome.cycle.state is gm_service.DraftCycleState.SKIPPED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.RESUME_CONTENDED
            assert outcome.cycle.draft is None and outcome.cycle.records == ()
            assert [s.pick_number for s in await _selections(pool, f)] == [30]
            assert len(await txn_repo.list_transactions_for_league(pool, f.league_id)) == 1
        finally:
            release.set()
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_resume_that_fails_outright_still_answers_with_the_committed_pick():
    """B2's failure arm -- not contention, not a precondition, just a resume
    that raised (a pool timeout, a bug in the AI pass, a connection reset).
    Every one of those escaped the command before this fix, and the pick was
    committed in all of them, so the person was told their command failed
    while their pick stood and nobody was ever announced or pinged."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            with patch.object(
                gm_command_service,
                "run_draft_cycle",
                side_effect=RuntimeError("the resume exploded"),
            ):
                outcome = await _pick(pool, f)

            assert outcome.cycle.state is gm_service.DraftCycleState.SKIPPED
            assert outcome.cycle.reason is gm_service.DraftCycleReason.RESUME_FAILED
            assert outcome.cycle.draft is None and outcome.cycle.records == ()
            assert outcome.pick.pick_number == 30
            assert [s.pick_number for s in await _selections(pool, f)] == [30]
            assert len(await txn_repo.list_transactions_for_league(pool, f.league_id)) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_resume_that_fails_after_it_commits_still_reports_the_picks_it_made():
    """Round-2 critic's M-A. The broad handler around the resume wraps the
    transaction's own exit -- its COMMIT and then `pool.release` -- so a
    failure raised AFTER the AI's picks were durable reported
    `SKIPPED(resume_failed)`: picks committed that nobody was told about, no
    `DecisionRecord` written for them, the next person never pinged, and a
    public "the draft is paused" line that was simply false. The outcome is
    captured the moment `run_draft_cycle` returns, so a post-commit failure
    is logged and the REAL outcome is still what the command answers with.

    The failure is armed only for the resume's own release, by a wrapper
    that releases the connection for real first (a release that never
    happens leaks a pool connection into the rest of the suite) and then
    raises what asyncpg raises when a connection dies under it."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            armed = {"on": False, "fired": False}
            real_cycle = gm_service.run_draft_cycle
            real_release = asyncpg.pool.Pool.release

            async def _cycle_then_arm(*args, **kwargs):
                outcome = await real_cycle(*args, **kwargs)
                armed["on"] = True
                return outcome

            async def _release_then_explode(self, connection, *, timeout=None):
                released = await real_release(self, connection, timeout=timeout)
                if armed["on"]:
                    armed["on"] = False
                    armed["fired"] = True
                    raise asyncpg.InterfaceError(
                        "the connection was closed while it was being released"
                    )
                return released

            with (
                patch.object(gm_command_service, "run_draft_cycle", side_effect=_cycle_then_arm),
                patch.object(asyncpg.pool.Pool, "release", _release_then_explode),
            ):
                outcome = await _pick(pool, f)

            assert armed["fired"], "the post-commit failure never happened, so this proves nothing"
            assert outcome.cycle.state is gm_service.DraftCycleState.WAITING_ON_HUMAN
            assert outcome.cycle.reason is None
            assert outcome.cycle.draft is not None and len(outcome.cycle.draft.picks) == 1
            assert outcome.cycle.team_id == f.teams["CHI"]

            # The AI's pick really is durable -- which is the whole reason
            # reporting SKIPPED for it was wrong.
            assert [s.pick_number for s in await _selections(pool, f)] == [30, 31]
            artifact = gm_service.artifact_path(f.league_id, _SEASON)
            assert artifact.exists(), "the AI's DecisionRecord was never written"
            assert len(artifact.read_text(encoding="utf-8").splitlines()) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()
