"""Live I/O proof for `gm_service.run_draft_cycle` -- the one place the AI
draft runs forward, and the four states it reports about the board it
leaves behind (Decision P5-98.g/h, docs/design/phase-5-stage-d-prime-
draft.md §4 commit 5, §5 T9(a)).

**The state tests are about what a caller may act on.** `WAITING_ON_HUMAN`
and `COMPLETE` are the two a Stage E exit gate would read, and the one that
separates them from a plausible wrong implementation is the third: a draft
whose remaining picks the lottery has not resolved presents an EMPTY
eligible list with picks still owed. A cycle that called an empty list
"complete" would report that board as finished and let the league leave
DRAFT with real picks abandoned. That board is the committed case at
`test_gm_service.py:1018`, rebuilt here.

**The contention tests use a real second connection**, held open in its own
task, because a fake that never suspends cannot show the interleaving the
`drafts` row lock exists to force. One arm proves the caller WAITS and then
fails with the draft's own message (not `maps_lock_contention`'s entity
wording, which names the wrong noun for a non-entity row); the other
releases the lock and proves the loser re-reads the board rather than
re-applying against a stale one.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from dba2.core.errors import ConcurrencyError, ValidationError
from dba2.data.db import close_pool, get_pool, transaction
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
from dba2.services import gm_service
from dba2.services.gm_service import DraftCycleReason, DraftCycleState
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026
_COMMISSIONER = 1
_TOR_MANAGER = 101
_BOS_MANAGER = 202

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)

_DRAFT_CONTENTION_MESSAGE = (
    "Another pick is being applied to this draft right now -- try again in a moment."
)


def _ratings_columns(seed: int) -> dict[str, int]:
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
    picks: tuple[tuple[int, int | None, str], ...],
    managers: dict[str, int] | None = None,
    prospects: int = 2,
) -> _Fixture:
    """A league in DRAFT with exactly the board a test needs: `picks` is
    `(round, pick_number, team_code)` in creation order, `pick_number=None`
    being a slot the lottery has not resolved, and `managers` naming which
    of TOR/BOS a person runs (absent means AI-managed)."""
    managers = managers or {}
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Cycle Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=2026_10,
        current_phase=Phase.DRAFT,
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
    for code in ("TOR", "BOS"):
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


async def _cycle(pool, f: _Fixture):
    return await gm_service.run_draft_cycle(pool, league_id=f.league_id, season=_SEASON)


async def _hold_the_drafts_row(pool, f: _Fixture, holding: asyncio.Event, release: asyncio.Event):
    """A second connection, in its own task, holding the season's `drafts`
    row `FOR UPDATE` in an uncommitted transaction. Its own task because
    `transaction(pool)` refuses a second top-level transaction inside one
    that is already open -- and because a holder that never suspends is not
    a holder at all."""
    async with transaction(pool) as conn:
        await draft_repo.get_draft_for_season_for_update(conn, f.league_id, _SEASON)
        holding.set()
        await release.wait()


# ---------------------------------------------------------------------------
# The four states, derived from the post-write board.
# ---------------------------------------------------------------------------


async def test_a_cycle_that_stops_at_a_human_reports_who_is_on_the_clock():
    """`WAITING_ON_HUMAN` carries the pick the person has to make and the
    manager to ping for it -- read from the board AFTER the applier wrote,
    never from the halt index, which is a statement about the list the
    cycle started with."""
    pool = await get_pool()
    try:
        f = await _build(
            pool,
            picks=((1, 30, "TOR"), (2, 31, "BOS")),
            managers={"BOS": _BOS_MANAGER},
        )
        try:
            outcome = await _cycle(pool, f)

            assert outcome.state is DraftCycleState.WAITING_ON_HUMAN
            assert outcome.reason is None
            assert outcome.pick_id == f.picks[1].id
            assert (outcome.round, outcome.pick_number) == (2, 31)
            assert outcome.team_id == f.teams["BOS"]
            assert outcome.manager_user_id == _BOS_MANAGER

            # The picks this cycle made travel with the state, for the
            # caller's decision log and its announcement.
            assert outcome.draft is not None
            assert len(outcome.draft.picks) == 1
            assert len(outcome.records) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_draft_with_every_pick_used_reports_complete():
    """`COMPLETE` is the only state in which leaving DRAFT is legal, so it
    has to mean every pick is used -- not merely that nothing is left to
    draft right now."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, 2, "BOS")), prospects=4)
        try:
            outcome = await _cycle(pool, f)

            assert outcome.state is DraftCycleState.COMPLETE
            assert outcome.reason is None
            assert outcome.pick_id is None
            assert outcome.draft is not None and len(outcome.draft.picks) == 2

            picks = await draft_repo.list_draft_picks_for_season(pool, f.league_id, _SEASON)
            assert all(p.used_for_player_id is not None for p in picks)
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_draft_whose_remaining_pick_has_no_slot_is_stalled_not_complete():
    """The board at `test_gm_service.py:1018`, and the case a
    COMPLETE-on-an-empty-list implementation gets wrong. Picks 1 and 2 are
    drafted; the middle pick's `pick_number` is NULL, so
    `_eligible_draft_picks_sorted` filters it out and the eligible list is
    empty -- while a real pick is still owed. Reporting that as `COMPLETE`
    hands Stage E a finished draft that is not finished; the fix for it is
    upstream, in DRAFT_LOTTERY, which is why the reason names the lottery
    and not the pool."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, None, "BOS"), (1, 2, "TOR")), prospects=3)
        try:
            outcome = await _cycle(pool, f)

            assert outcome.state is DraftCycleState.STALLED
            assert outcome.reason is DraftCycleReason.UNRESOLVED_PICKS
            assert outcome.pick_id is None
            assert outcome.draft is not None and len(outcome.draft.picks) == 2

            unresolved = await draft_repo.get_draft_pick(pool, f.picks[1].id)
            assert unresolved.pick_number is None
            assert unresolved.used_for_player_id is None
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_an_ai_team_left_on_the_clock_with_no_prospects_is_stalled():
    """`STALLED(pool_exhausted)`: an AI team is on the clock and the AI
    drafted nobody, which after the taken-set filter can only mean no
    available prospect remains (Gap F, or a class smaller than the draft).
    Nothing retries -- the cycle reports the reason and stops, and the
    board is exactly as it was."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"),), prospects=0)
        try:
            outcome = await _cycle(pool, f)

            assert outcome.state is DraftCycleState.STALLED
            assert outcome.reason is DraftCycleReason.POOL_EXHAUSTED
            assert outcome.pick_id == f.picks[0].id
            assert (outcome.round, outcome.pick_number) == (1, 1)
            assert outcome.team_id == f.teams["TOR"]
            assert outcome.draft is None
            assert outcome.records == ()

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert selections == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_phase_hook_carries_the_cycles_outcome():
    """Decision P5-98.h's seam: a press can see the stop. `draft` alone
    cannot say why nothing was drafted -- four different situations
    collapse into `None` -- so the hook surfaces the whole outcome, and
    keeps writing the decision log itself from the records the cycle
    returned."""
    pool = await get_pool()
    try:
        f = await _build(
            pool,
            picks=((1, 30, "TOR"), (2, 31, "BOS")),
            managers={"BOS": _BOS_MANAGER},
        )
        try:
            result = await gm_service.run_phase_hook(
                pool, league_id=f.league_id, phase=Phase.DRAFT, actor_user_id=_COMMISSIONER
            )

            assert result.draft_cycle is not None
            assert result.draft_cycle.state is DraftCycleState.WAITING_ON_HUMAN
            assert result.draft_cycle.team_id == f.teams["BOS"]
            assert result.draft is result.draft_cycle.draft
            assert result.draft is not None and len(result.draft.picks) == 1
            assert gm_service.artifact_path(f.league_id, _SEASON).exists()
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# T9(a) -- the `drafts` row is the draft's mutual-exclusion token.
# ---------------------------------------------------------------------------


async def test_a_cycle_waits_for_the_drafts_row_and_then_says_so_in_the_drafts_own_words():
    """T9(a). A second connection holds the row `FOR UPDATE`, so the wait is
    real rather than raced. `run_draft_cycle` opens no league gate, so
    nothing sets `lock_timeout` for it unless it does -- without that it
    would wait forever here. The message is the assertion that fails if the
    mapping is left to `maps_lock_contention`, whose entity wording ("That
    team/player is being updated...") names the wrong noun for a `drafts`
    row."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, 2, "BOS")), prospects=4)
        holding, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(_hold_the_drafts_row(pool, f, holding, release))
        try:
            await holding.wait()

            started = time.perf_counter()
            with pytest.raises(ConcurrencyError) as excinfo:
                await _cycle(pool, f)
            waited = time.perf_counter() - started

            assert str(excinfo.value) == _DRAFT_CONTENTION_MESSAGE
            assert waited >= 2.5, f"returned in {waited:.2f}s -- it did not wait for the row"

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert selections == [], "a refused cycle wrote picks anyway"
        finally:
            release.set()
            await holder
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_second_press_blocked_on_the_row_re_reads_the_board_instead_of_applying():
    """T9(a)'s released-lock twin, and the double-click it exists for. The
    first press holds the `drafts` row across its whole read-decide-write;
    the second blocks on that row, and when it is released it re-reads and
    finds that TOR is no longer on the clock. Exactly one selection and
    exactly one `league_transactions` row -- and the refusal is a sentence,
    not the raw `UniqueViolationError` that `UNIQUE (draft_id,
    pick_number)` would have produced without the row lock."""
    pool = await get_pool()
    try:
        f = await _build(
            pool,
            picks=((1, 30, "TOR"), (2, 31, "BOS")),
            managers={"TOR": _TOR_MANAGER, "BOS": _BOS_MANAGER},
        )
        applied, release = asyncio.Event(), asyncio.Event()

        async def press(hold: bool):
            async with transaction(pool) as conn:
                await draft_repo.get_draft_for_season_for_update(conn, f.league_id, _SEASON)
                pick = await gm_service.apply_human_draft_pick(
                    conn,
                    league_id=f.league_id,
                    season=_SEASON,
                    draft_id=f.draft_id,
                    team_id=f.teams["TOR"],
                    prospect_id=f.prospects[0],
                    actor_user_id=_TOR_MANAGER,
                )
                if hold:
                    applied.set()
                    await release.wait()
                return pick

        first = asyncio.create_task(press(hold=True))
        try:
            await applied.wait()
            second = asyncio.create_task(press(hold=False))
            # Long enough to prove the second press is blocked on the row
            # rather than racing past it: without the lock it would already
            # have read the same pre-write board and applied against it.
            await asyncio.sleep(0.5)
            assert not second.done(), "the second press did not block on the drafts row"

            release.set()
            await first

            with pytest.raises(ValidationError) as excinfo:
                await second
            assert str(excinfo.value) == "BOS is on the clock -- round 2, pick 31."
        finally:
            release.set()
            await first
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            txns = await txn_repo.list_transactions_for_league(pool, f.league_id)
            assert len(selections) == 1
            assert len(txns) == 1
            await _cleanup(pool, f)
    finally:
        await close_pool()
