"""Live I/O proof for the draft's two appliers -- `gm_service.apply_human_
draft_pick` (the human half, Decision P5-98.d/e/f) and the halt plus the
taken-set filter the AI half gained with it (P5-98.b/c). Design:
docs/design/phase-5-stage-d-prime-draft.md §4 commits 3-4, §5 T2/T7/T8/
T10/T11/T12.

**The fixture is the design's own T2 fixture, and its numbers are
deliberate.** Two teams; one draft with `total_picks = 2`; pick A = round 1
`pick_number` 30, pick B = round 2 `pick_number` 31. `draft_selections`
carries `UNIQUE (draft_id, pick_number)`, so pick numbers are unique across
the WHOLE draft -- the pick after round 1 pick 30 is round 2 pick 31, and a
pointer implementation that assumes contiguity inside a round writes
`(1, 31)`. The stored pointer is therefore asserted as a hand-written
literal `(2, 31)`, never re-derived from the pick rows: a test that
recomputes the answer the way the code does agrees with the code's own bug.

Both arms of that differential now live here: the human arm calls
`apply_human_draft_pick` directly, the AI arm runs `run_phase_hook`'s own
DRAFT branch against the identical board with X AI-managed instead. The
arms cannot share a player id (`players` rows are league-scoped), so the
AI arm's pick is resolved to an ORDINAL by last name and the human arm is
handed its own league's prospect at that ordinal.

**Every call here holds the `drafts` row `FOR UPDATE` first**, through
`draft_repo.get_draft_for_season_for_update`, because that is the contract
`apply_human_draft_pick` is written to: it takes no lock itself (it is a
lock inheritor) and every refusal it makes is a decision about a board no
other applier may be changing underneath it. Until `/draft` exists, these
tests are that lock's only holder.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from dba2.core.errors import ValidationError
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
from dba2.domain.transaction import TransactionAssetKind, TransactionType
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026
_COMMISSIONER = 1
_TOR_MANAGER = 101
_BOS_MANAGER = 202
_A_STRANGER = 303

# One ordered list of (last_name, overall, age), so a second league built by
# this same helper has a prospect at every ordinal identical in every field
# the valuation reads. `players` rows are league-scoped, so the AI arm can
# never reuse an id across leagues -- it resolves by ordinal instead.
_PROSPECTS: tuple[tuple[str, int, int], ...] = (
    ("Alpha", 68, 19),
    ("Bravo", 64, 20),
)

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
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
    tor: int
    bos: int
    pick_a: DraftPick
    pick_b: DraftPick
    prospects: tuple[int, ...]
    pick_c: DraftPick | None = None


async def _build(
    pool,
    *,
    tor_manager: int | None,
    bos_manager: int | None,
    third_pick_for: str | None = None,
    tor_auto_pilot: bool = False,
) -> _Fixture:
    """The T2 board: TOR owns round 1 pick 30, BOS owns round 2 pick 31, and
    two draft-eligible prospects exist. `tor_manager=None` makes TOR
    AI-managed, which is the only difference T12's refusal turns on.

    `third_pick_for="TOR"` adds round 2 pick 32 for that team -- the pick
    T8's resume draws on, and the only reason a board here ever has more
    than the two the differential needs.

    The league carries real `league_cap_settings` because the AI arm runs
    `load_gm_inputs`, which raises rather than fabricate one (its own
    "raise rather than fabricate" rule). The human arm never reads them,
    so they change nothing it asserts."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Human Draft Pick Test League",
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
    teams = {}
    for code, manager in (("TOR", tor_manager), ("BOS", bos_manager)):
        team = await team_repo.create_team(
            pool,
            league_id=league.id,
            code=code,
            name=f"{code} Team",
            city=f"{code} City",
            conference="East",
            division="Atlantic",
            home_market_size=3,
            manager_user_id=manager,
            gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
            timeline=Timeline.RETOOL,
        )
        teams[code] = team.id
    if tor_auto_pilot:
        await team_repo.set_auto_pilot(
            pool, teams["TOR"], auto_pilot=True, gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
        )
    draft = await draft_repo.create_draft(
        pool,
        league_id=league.id,
        season=_SEASON,
        total_picks=2 if third_pick_for is None else 3,
    )
    pick_a = await draft_repo.create_draft_pick(
        pool,
        league_id=league.id,
        season=_SEASON,
        round=1,
        original_team_id=teams["TOR"],
        current_team_id=teams["TOR"],
        pick_number=30,
    )
    pick_b = await draft_repo.create_draft_pick(
        pool,
        league_id=league.id,
        season=_SEASON,
        round=2,
        original_team_id=teams["BOS"],
        current_team_id=teams["BOS"],
        pick_number=31,
    )
    pick_c = None
    if third_pick_for is not None:
        pick_c = await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=2,
            original_team_id=teams[third_pick_for],
            current_team_id=teams[third_pick_for],
            pick_number=32,
        )
    prospects = []
    for last_name, overall, age in _PROSPECTS:
        player = await player_repo.create_player(
            pool,
            league_id=league.id,
            first_name="Prospect",
            last_name=last_name,
            primary_position=Position.SF,
            height_in=79,
            weight_lb=215,
            wingspan_in=82,
            birth_date=birth_date_for_age(age, _SEASON),
            roster_status=RosterStatus.DRAFT_ELIGIBLE,
            overall_cached=overall,
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
        prospects.append(player.id)
    return _Fixture(
        league_id=league.id,
        draft_id=draft.id,
        tor=teams["TOR"],
        bos=teams["BOS"],
        pick_a=pick_a,
        pick_b=pick_b,
        pick_c=pick_c,
        prospects=tuple(prospects),
    )


async def _pick_under_lock(pool, f: _Fixture, *, team_id: int, prospect_id: int, actor: int):
    """What `/draft`'s own transaction will do (Decision P5-98.g): take the
    `drafts` row `FOR UPDATE` first, then apply inside that transaction."""
    async with transaction(pool) as conn:
        draft = await draft_repo.get_draft_for_season_for_update(conn, f.league_id, _SEASON)
        assert draft is not None and draft.id == f.draft_id
        return await gm_service.apply_human_draft_pick(
            conn,
            league_id=f.league_id,
            season=_SEASON,
            draft_id=f.draft_id,
            team_id=team_id,
            prospect_id=prospect_id,
            actor_user_id=actor,
        )


async def _board(pool, f: _Fixture) -> tuple[list, list, list]:
    """(selections, used_for_player_ids, transaction rows) -- the three
    places a pick leaves a mark, read together so a refusal can assert all
    three are untouched in one statement."""
    selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
    picks = await draft_repo.list_draft_picks_for_season(pool, f.league_id, _SEASON)
    txns = await txn_repo.list_transactions_for_league(pool, f.league_id)
    return selections, [p.used_for_player_id for p in picks], txns


async def _cleanup(pool, f: _Fixture) -> None:
    # The AI arm runs `run_phase_hook`, which appends to this league/
    # season's JSONL decision log -- left behind, a stale file collides
    # with the next run that happens to reuse the league id.
    gm_service.artifact_path(f.league_id, _SEASON).unlink(missing_ok=True)
    await pool.execute("DELETE FROM leagues WHERE id = $1", f.league_id)


async def _run_ai_draft(pool, f: _Fixture):
    """The AI half, through the production path that owns the halt:
    `run_phase_hook`'s DRAFT branch (`execute_draft`). Nothing in the bot
    can start a draft until Stage E (§7 L8), so the hook is what `/draft`'s
    own resume will call, and it is what these tests drive."""
    return await gm_service.run_phase_hook(
        pool, league_id=f.league_id, phase=Phase.DRAFT, actor_user_id=_COMMISSIONER
    )


async def _drafted_ordinal(pool, f: _Fixture) -> int:
    """Which entry of `_PROSPECTS` the AI took, by NAME. `players` rows are
    league-scoped, so an id read out of one league names a different player
    (or none) in the other -- the ordinal is the only thing the two arms
    can share."""
    selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
    assert len(selections) == 1
    player = await player_repo.get_player(pool, selections[0].player_id)
    return [name for name, _ovr, _age in _PROSPECTS].index(player.last_name)


async def test_a_human_pick_writes_the_selection_and_the_pointer_the_ai_would_have():
    """T2's human arm. The literal `(2, 31)` is the assertion that can fail
    alone: the next pick is in the next ROUND, so a pointer derived from
    "this pick's number + 1" reads `(1, 31)` -- a slot that does not exist.
    """
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            applied = await _pick_under_lock(
                pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_TOR_MANAGER
            )

            assert applied.id == f.pick_a.id
            assert (applied.round, applied.pick_number) == (1, 30)

            draft_row = await draft_repo.get_draft(pool, f.draft_id)
            assert (draft_row.current_round, draft_row.current_pick_number) == (2, 31)

            selections, used, _txns = await _board(pool, f)
            assert len(selections) == 1
            assert (selections[0].player_id, selections[0].team_id) == (f.prospects[0], f.tor)
            assert (selections[0].round, selections[0].pick_number) == (1, 30)
            assert selections[0].pick_asset_id == f.pick_a.id
            assert sorted(u for u in used if u is not None) == [f.prospects[0]]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_human_pick_is_logged_as_a_league_transaction_with_its_two_assets():
    """Decision P5-98.e: the pick's actor lives in `league_transactions`,
    never in a `DecisionRecord` (which has no actor field and would claim a
    person's pick was an AI decision). Two asset rows -- the pick spent and
    the player taken -- both landing ON the picking team."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await _pick_under_lock(
                pool, f, team_id=f.tor, prospect_id=f.prospects[1], actor=_TOR_MANAGER
            )

            txns = await txn_repo.list_transactions_for_league(pool, f.league_id)
            assert len(txns) == 1
            txn = txns[0]
            assert txn.txn_type is TransactionType.DRAFT_PICK
            assert txn.actor_user_id == _TOR_MANAGER
            assert txn.phase is Phase.DRAFT
            assert txn.season == _SEASON

            assets = await txn_repo.list_transaction_assets(pool, txn.id)
            by_kind = {a.asset_kind: a for a in assets}
            assert set(by_kind) == {TransactionAssetKind.PICK, TransactionAssetKind.PLAYER}
            assert by_kind[TransactionAssetKind.PICK].draft_pick_id == f.pick_a.id
            assert by_kind[TransactionAssetKind.PLAYER].player_id == f.prospects[1]
            assert all(a.to_team_id == f.tor for a in assets)
            assert all(a.from_team_id is None for a in assets)

            # The JSONL decision log stays the AI's artifact (P5-98.e/L4).
            assert not gm_service.artifact_path(f.league_id, _SEASON).exists()
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_team_not_on_the_clock_is_refused_and_writes_nothing():
    """T11, the wrong-team must-fire. BOS's manager calls for BOS while TOR
    is on the clock; BOS's pick is real and later in the same draft, so a
    service that trusted a caller-supplied pick would apply it happily and
    skip TOR entirely. Zero rows in all three places is the assertion --
    "it raised" alone would pass against a service that wrote first."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            with pytest.raises(ValidationError) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.bos, prospect_id=f.prospects[0], actor=_BOS_MANAGER
                )
            assert str(excinfo.value) == "TOR is on the clock -- round 1, pick 30."

            selections, used, txns = await _board(pool, f)
            assert selections == []
            assert used == [None, None]
            assert txns == []

            draft_row = await draft_repo.get_draft(pool, f.draft_id)
            assert (draft_row.current_round, draft_row.current_pick_number) == (1, 1)
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_manager_of_another_team_may_not_pick_for_the_team_on_the_clock():
    """The other half of T11: naming TOR (the team that IS on the clock)
    does not help BOS's manager, because the authorization is against the
    on-the-clock team's own manager, read under the lock."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            with pytest.raises(PermissionDenied):
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_BOS_MANAGER
                )
            selections, used, txns = await _board(pool, f)
            assert (selections, used, txns) == ([], [None, None], [])
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_stranger_may_not_pick_at_all():
    """T10's third arm: neither the team's manager nor the commissioner."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            with pytest.raises(PermissionDenied):
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_A_STRANGER
                )
            selections, _used, txns = await _board(pool, f)
            assert (selections, txns) == ([], [])
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_commissioner_may_unblock_a_human_teams_pick():
    """P5-48's override, and it is not a second command: the same call with
    a different `actor_user_id`. The `league_transactions` row is what tells
    the two apart afterwards."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await _pick_under_lock(
                pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_COMMISSIONER
            )
            selections, _used, txns = await _board(pool, f)
            assert len(selections) == 1
            assert [t.actor_user_id for t in txns] == [_COMMISSIONER]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_commissioner_may_not_pick_for_an_ai_managed_team():
    """T12, and it is the bound P5-44/P5-55 put on the override:
    `require_can_act_for_team`'s union alone would let a commissioner hand-
    drive the AI's own draft."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=None, bos_manager=_BOS_MANAGER)
        try:
            with pytest.raises(PermissionDenied) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_COMMISSIONER
                )
            assert str(excinfo.value) == "TOR is AI-managed -- the AI makes its own picks."
            selections, _used, txns = await _board(pool, f)
            assert (selections, txns) == ([], [])
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_an_auto_piloted_teams_own_manager_may_not_pick():
    """GM-3 (docs/design/gm-command-surface.md), mutation 3 -- widening
    2.3's existing conjunction (`manager_user_id is None or auto_pilot`)
    is a no-op for the team's OWN manager, because `is_team_manager`
    returns True for them and the conjunction short-circuits: this test
    is the one the brief's literal instruction would not have caught
    (2.3's own point). Only a SEPARATE branch, checked BEFORE the
    existing AI-managed check, refuses the manager here."""
    pool = await get_pool()
    try:
        f = await _build(
            pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER, tor_auto_pilot=True
        )
        try:
            with pytest.raises(PermissionDenied) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_TOR_MANAGER
                )
            assert str(excinfo.value) == (
                "TOR is on auto-pilot -- the AI is making its picks. "
                "Run `/gm auto enabled:off` to pick yourself."
            )
            selections, _used, txns = await _board(pool, f)
            assert (selections, txns) == ([], [])
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_non_owning_commissioner_may_not_pick_for_an_auto_piloted_team():
    """GM-3's second message: a commissioner hitting the auto-pilot branch
    gets told to use `/gm auto` or `/remove-manager` -- never the "AI-
    managed" sentence `test_the_commissioner_may_not_pick_for_an_ai_
    managed_team` asserts for a genuinely unmanaged team, because that
    sentence would be a lie for a team that still has an owning GM."""
    pool = await get_pool()
    try:
        f = await _build(
            pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER, tor_auto_pilot=True
        )
        try:
            with pytest.raises(PermissionDenied) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_COMMISSIONER
                )
            assert str(excinfo.value) == (
                "TOR is on auto-pilot -- the AI is making its picks. "
                "Only TOR's own GM can turn that off (`/gm auto`), or use `/remove-manager` "
                "to hand the team to the AI outright."
            )
            selections, _used, txns = await _board(pool, f)
            assert (selections, txns) == ([], [])
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_prospect_already_taken_in_this_draft_is_refused():
    """Decision P5-98.c's exclusion, as a validation. Nothing in the schema
    stops a player being drafted twice -- `UNIQUE (draft_id, pick_number)`
    protects the slot, not the player -- and `apply_draft` leaves the
    drafted prospect `DRAFT_ELIGIBLE` with no team, so he is still in every
    later pool. The halt is what arms this; the check is what closes it."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await _pick_under_lock(
                pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_TOR_MANAGER
            )
            # The prospect is still DRAFT_ELIGIBLE and on no team -- the
            # reason the taken-set check has to exist at all.
            still_eligible = await player_repo.get_player(pool, f.prospects[0])
            assert still_eligible.roster_status is RosterStatus.DRAFT_ELIGIBLE
            assert still_eligible.team_id is None

            with pytest.raises(ValidationError) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.bos, prospect_id=f.prospects[0], actor=_BOS_MANAGER
                )
            assert str(excinfo.value) == f"Player {f.prospects[0]} was taken at pick 30."

            selections, _used, txns = await _board(pool, f)
            assert len(selections) == 1, "the refused second pick wrote nothing"
            assert len(txns) == 1
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_player_who_is_not_a_prospect_in_this_league_is_refused():
    """Two shapes, one message: a player on a roster (wrong status) and a
    player id from another league (wrong league). The second is why the
    check reads `league_id` and not only the status -- `players` ids are
    league-scoped, and a caller can name one that exists elsewhere."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        other = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await player_repo.set_team_and_status(pool, f.prospects[0], f.bos, RosterStatus.ACTIVE)
            for bad_id in (f.prospects[0], other.prospects[0]):
                with pytest.raises(ValidationError) as excinfo:
                    await _pick_under_lock(
                        pool, f, team_id=f.tor, prospect_id=bad_id, actor=_TOR_MANAGER
                    )
                assert str(excinfo.value) == (
                    f"Player {bad_id} is not a draft-eligible prospect in this league."
                )
            selections, _used, txns = await _board(pool, f)
            assert (selections, txns) == ([], [])
        finally:
            await _cleanup(pool, other)
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_an_empty_eligible_list_says_which_of_its_two_causes_it_is():
    """`_eligible_draft_picks_sorted` filters out BOTH used picks and picks
    whose `pick_number` the lottery has not resolved, so an empty list is
    two different situations with two different fixes. "Every pick has
    already been made" is a lie on the second, and it sends the person to
    the wrong place (Decision P5-98.h's `unresolved_picks`)."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await pool.execute(
                "UPDATE draft_picks SET pick_number = NULL WHERE league_id = $1", f.league_id
            )
            with pytest.raises(ValidationError) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_TOR_MANAGER
                )
            assert "lottery" in str(excinfo.value)

            await pool.execute(
                "UPDATE draft_picks SET used_for_player_id = $2 WHERE league_id = $1",
                f.league_id,
                f.prospects[1],
            )
            with pytest.raises(ValidationError) as excinfo:
                await _pick_under_lock(
                    pool, f, team_id=f.tor, prospect_id=f.prospects[0], actor=_TOR_MANAGER
                )
            assert str(excinfo.value) == "Every pick in this draft has already been made."
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# The halt and the taken-set filter (commit 4, Decisions P5-98.b/c) --
# T2's AI arm, T7 and T8.
# ---------------------------------------------------------------------------


async def test_the_ai_draft_stops_at_a_human_managed_teams_pick():
    """T7. TOR is AI-managed and on the clock; BOS is human-managed and owns
    the next pick, so the AI makes pick A and stops -- it does not draft
    through BOS.

    The pointer assertion is the exact tuple `(2, 31)`, which is BOS's own
    pick's `(round, pick_number)`, and the round boundary is what makes it
    an assertion at all: the halt truncates only the `draft_order` handed to
    the pure layer, so `_next_pick_pointer` still reads the FULL eligible
    list and answers with BOS's real slot. A halt that truncated the list
    the pointer is read from answers `(1, 31)` -- a slot that does not
    exist, since `draft_selections`' `UNIQUE (draft_id, pick_number)` makes
    pick numbers unique across the whole draft."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=None, bos_manager=_BOS_MANAGER)
        try:
            result = await _run_ai_draft(pool, f)

            assert result.draft is not None
            assert len(result.draft.picks) == 1, "the AI drafted through the human's pick"

            selections, used, _txns = await _board(pool, f)
            assert len(selections) == 1
            assert selections[0].pick_asset_id == f.pick_a.id
            assert selections[0].team_id == f.tor

            picks = await draft_repo.list_draft_picks_for_season(pool, f.league_id, _SEASON)
            by_id = {p.id: p for p in picks}
            assert by_id[f.pick_b.id].used_for_player_id is None, "BOS's pick was taken by the AI"
            assert len([u for u in used if u is not None]) == 1

            draft_row = await draft_repo.get_draft(pool, f.draft_id)
            assert (draft_row.current_round, draft_row.current_pick_number) == (2, 31)
            assert (draft_row.current_round, draft_row.current_pick_number) == (
                f.pick_b.round,
                f.pick_b.pick_number,
            ), "the pointer must already name the human's own pick when they arrive"
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_ai_and_the_human_arms_write_the_same_pointer():
    """T2, closed. Two leagues built by one helper off one ordered prospect
    list, identical in every field the valuation reads; BOS is human-managed
    in both, so both arms stop after pick A for the same reason. The only
    difference is who makes pick A -- the AI's applier loop in league A, a
    person's `apply_human_draft_pick` in league B.

    Two assertions, and the first can fail alone (P5-89): each league's
    stored pointer equals the hand-written literal `(2, 31)`, and the two
    leagues' pointers equal each other. The literal is the detector -- a
    mutation that moves both arms together leaves the equality green."""
    pool = await get_pool()
    try:
        ai = await _build(pool, tor_manager=None, bos_manager=_BOS_MANAGER)
        human = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            await _run_ai_draft(pool, ai)
            ordinal = await _drafted_ordinal(pool, ai)

            await _pick_under_lock(
                pool,
                human,
                team_id=human.tor,
                prospect_id=human.prospects[ordinal],
                actor=_TOR_MANAGER,
            )

            ai_row = await draft_repo.get_draft(pool, ai.draft_id)
            human_row = await draft_repo.get_draft(pool, human.draft_id)
            ai_pointer = (ai_row.current_round, ai_row.current_pick_number)
            human_pointer = (human_row.current_round, human_row.current_pick_number)

            assert ai_pointer == (2, 31)
            assert human_pointer == (2, 31)
            assert ai_pointer == human_pointer

            ai_selections = await draft_repo.list_draft_selections_for_draft(pool, ai.draft_id)
            human_selections = await draft_repo.list_draft_selections_for_draft(
                pool, human.draft_id
            )
            assert [(s.round, s.pick_number) for s in ai_selections] == [(1, 30)]
            assert [(s.round, s.pick_number) for s in human_selections] == [(1, 30)]
        finally:
            await _cleanup(pool, human)
            await _cleanup(pool, ai)
    finally:
        await close_pool()


async def test_a_draft_already_waiting_on_a_human_writes_nothing_at_all():
    """T7's empty truncation. TOR is human-managed and on the clock, so the
    halt index is 0: the pure layer is handed an empty order, returns no
    picks, and the applier's transaction never opens. No selection, no
    `used_for_player_id`, and -- the assertion that matters -- **no pointer
    write**: the `drafts` row still holds `create_draft`'s own `(1, 1)`
    default, because a draft waiting on a person must not advance its
    pointer past the pick they are about to make."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=_TOR_MANAGER, bos_manager=_BOS_MANAGER)
        try:
            before = await draft_repo.get_draft(pool, f.draft_id)
            assert (before.current_round, before.current_pick_number) == (1, 1)

            result = await _run_ai_draft(pool, f)
            assert result.draft is None

            selections, used, txns = await _board(pool, f)
            assert (selections, used, txns) == ([], [None, None], [])

            after = await draft_repo.get_draft(pool, f.draft_id)
            assert (after.current_round, after.current_pick_number) == (1, 1)
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_resumed_draft_never_re_offers_a_player_it_already_took():
    """T8, the re-offer regression (F3). `run_draft`'s "physically absent
    from every later pick's pool" guarantee holds within ONE call, and the
    halt is what makes a draft run in more than one: `apply_draft` leaves
    the drafted prospect `DRAFT_ELIGIBLE` on no roster, so on resume he is
    back in `_load_draft_prospects`' output, and nothing in the schema stops
    him being taken twice.

    The board is TOR (AI) at round 1 pick 30, BOS (human) at round 2 pick
    31, TOR (AI) again at round 2 pick 32, with exactly two prospects: the
    AI takes one, the person takes the other, and the resume must draft
    NOBODY. Without the exclusion the resume re-offers both and pick 32
    lands a player who is already in a `draft_selections` row."""
    pool = await get_pool()
    try:
        f = await _build(pool, tor_manager=None, bos_manager=_BOS_MANAGER, third_pick_for="TOR")
        try:
            await _run_ai_draft(pool, f)
            ordinal = await _drafted_ordinal(pool, f)
            remaining = f.prospects[1 - ordinal]

            await _pick_under_lock(
                pool, f, team_id=f.bos, prospect_id=remaining, actor=_BOS_MANAGER
            )

            resumed = await _run_ai_draft(pool, f)
            assert resumed.draft is None, "the resume drafted from an exhausted pool"

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            player_ids = [s.player_id for s in selections]
            assert len(player_ids) == 2
            assert len(set(player_ids)) == 2, "a player was drafted twice in one draft"
            assert set(player_ids) == set(f.prospects)
            assert {s.pick_asset_id for s in selections} == {f.pick_a.id, f.pick_b.id}

            pick_c_row = await draft_repo.get_draft_pick(pool, f.pick_c.id)
            assert pick_c_row.used_for_player_id is None
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()
