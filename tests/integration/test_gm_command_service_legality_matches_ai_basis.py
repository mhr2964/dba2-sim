"""Round-3 critic's own structural recommendation on Stage D' (docs/design/
phase-5-discord-presentation.md §43.2 -> round-3 fix record below it): two
consecutive critic rounds each fixed one divergence between `gm_command_
service`'s human legality gate and the AI's own real basis, and each fix
introduced a DIFFERENT divergence in the same function -- because every
test written so far asserted a specific dollar/count value on a bespoke
fixture, never that the two bases AGREE. A formula can drift away from the
AI's own basis while still producing SOME plausible-looking number; a test
that asserts agreement catches that by construction.

**Round-5 correction, applies to this whole file's framing:** "does this
gate agree with the AI's own basis" turned out to be the RIGHT question
for the cap-legality half of the gate (`apron2_headroom` -- both sides
genuinely ask "what is this team's real cap room, via `capsheet.compute_
team_season_cap_sheet`, at the season the money lands") but the WRONG
question for the roster-max half (`physical_roster_count`): a human
signing is a CURRENT-season, immediate-effect write, while every AI-basis
roster-count formula in this codebase (`_next_season_roster_count`/
`_occupies_next_season`) answers a NEXT-season question built for the AI's
own decision layer. Rounds 2, 3, and 4 each tried to make the human gate's
roster count "agree with" that AI formula and each shipped a real
overfill regression doing it -- round 4's own fix replaced the borrowed
AI formula with a direct current-state count instead, which by
construction has no AI formula left to compare against. This file now
contains two DIFFERENT kinds of test, not one: `test_cap_headroom_basis_
agrees_with_the_ai_real_basis_on_a_shared_fixture` (the original round-3
idea, still correct for cap headroom only) and
`test_physical_roster_count_matches_real_active_count_independent_of_
either_formula` (round 4's own structural fix for the roster-max half --
compares against raw Postgres ground truth, not either side's formula).

This file computes BOTH (a) the human gate's own computed basis
(`gm_command_service._compute_signing_legality_basis` -- see its own
docstring for why it is split out as a return value rather than folded
into the raising function) and (b) the AI's own real basis, loaded through
the exact path `run_phase_hook`/`run_free_agency` use for cap headroom
(`gm_service.load_gm_inputs`, `capsheet_service.project_league_cap_
sheets`) -- or, for roster count, ground-truth Postgres, never an AI
formula -- on the SAME fixture, and asserts they AGREE -- or, where a
real, disclosed reason they should differ exists (the cap-headroom
season-basis asymmetry below), asserts the specific, understood
relationship instead of silently comparing nothing.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import dataclasses
import random
from datetime import UTC, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, ratings_repo, team_repo
from dba2.domain.contract import ContractType, SigningMethod
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.managers.decisions import signing as signing_decisions
from dba2.services import capsheet_service, gm_command_service, gm_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_COMMISSIONER = 1
_MANAGER = 42
_SEASON = 2026

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int) -> dict[str, int]:
    """The 57-column `ratings` dict `ratings_repo.upsert_player_ratings`
    validates against -- read off a hand-built `PlayerRatings` domain
    object (`tests.fixtures.sim.make_ratings`'s own pairwise-distinct-value
    discipline) rather than duplicating that discipline here, the same
    technique `tests/integration/test_gm_service.py::_ratings_columns`
    already established."""
    base = make_ratings(seed)
    return {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Legality Parity Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
        current_phase=Phase.REGULAR_SEASON,
    )
    # Only `_SEASON` itself -- `load_gm_inputs`/`project_league_cap_sheets`
    # both fall back to `managers.capsheet.project_cap_settings` for any
    # horizon season with no real row, exactly as `test_gm_service.py`'s
    # own `_make_league` already relies on for its `load_gm_inputs` tests.
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
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        manager_user_id=_MANAGER,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int, *, age: int = 27) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=birth_date_for_age(age, _SEASON),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    # `load_gm_inputs` reads `ratings_repo.get_for_league` for every
    # player in the league -- a real row is required, matching `test_gm_
    # service.py`'s own fixture (unlike `test_gm_command_service.py`'s
    # fixture, which never exercises the AI's own load path).
    ratings = _ratings_columns(player.id)
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        ratings=ratings,
    )
    return player.id


async def _make_contract(
    pool,
    league_id: int,
    team_id: int,
    player_id: int,
    *,
    start_season: int,
    salaries: tuple[int, ...],
) -> int:
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=start_season,
        start_season=start_season,
        end_season=start_season + len(salaries) - 1,
        total_years=len(salaries),
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    for i, salary in enumerate(salaries):
        await contract_repo.create_contract_year(
            pool,
            contract_id=contract.id,
            season=start_season + i,
            salary=salary,
            guaranteed_amount=salary,
        )
    return contract.id


@pytest.mark.asyncio
async def test_cap_headroom_basis_agrees_with_the_ai_real_basis_on_a_shared_fixture():
    """The round-3 critic's own structural recommendation, still valid for
    the CAP-LEGALITY half of the gate only. **Round-5 correction:** this
    test used to also assert `human_basis.physical_roster_count ==
    ai_roster_count` via `gm_service._next_season_roster_count` -- "the
    identical function" -- and passed only because this fixture happens
    to contain no player in their contract's final year. That assertion
    taught exactly the thesis round 4's own fix disproved: the roster-max
    half of the human gate is now a DIRECT current-state count and is
    correctly NOT expected to agree with any AI next-season-projection
    formula (see `SigningLegalityBasis`'s own corrected docstring). Left
    in place, a future engineer adding a normal final-year-contract
    fixture would have seen this test fail and the "obvious" fix would
    have been to revert the gate back to the broken formula -- precisely
    how round 3's bug got introduced. The roster-count-vs-AI-formula
    assertion is REMOVED, not merely commented out; the correct,
    basis-independent replacement for that half is `test_physical_roster_
    count_matches_real_active_count_independent_of_either_formula` below
    (ground-truth Postgres `COUNT(*)`, no formula on either side).

    What remains here, still true and worth testing: the CAP-legality
    half genuinely does still share the AI's own real `apron1_room` basis
    (`capsheet.compute_team_season_cap_sheet`) -- round 2's fix, unchanged
    by round 4. Fixture: 4 ACTIVE players each on a real 2-year contract
    covering `_SEASON`/`_SEASON + 1` (exercises the cap-legality gate's
    committed-salary/roster_count basis), 1 contract-less ACTIVE player,
    and 1 ACTIVE player on a contract whose ONLY covered season is
    `_SEASON - 1` (already fully lapsed) -- the roster-count shape is kept
    only because it also exercises the cap-legality gate's own separate
    `roster_count` basis (contract-year-covers-season, deliberately
    different from `physical_roster_count`), not because this test still
    asserts anything about physical occupancy."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PT1")
            for _ in range(4):
                pid = await _make_player(pool, league_id, team_id)
                await _make_contract(
                    pool,
                    league_id,
                    team_id,
                    pid,
                    start_season=_SEASON,
                    salaries=(4_000_000, 4_200_000),
                )
            await _make_player(pool, league_id, team_id)  # contract-less
            lapsed_pid = await _make_player(pool, league_id, team_id)
            await _make_contract(
                pool,
                league_id,
                team_id,
                lapsed_pid,
                start_season=_SEASON - 1,
                salaries=(3_000_000,),
            )

            team = await team_repo.get_team(pool, team_id)

            # (a) The human gate's own computed basis -- the exact
            # function `sign_free_agent`'s real command path calls.
            human_basis = await gm_command_service._compute_signing_legality_basis(
                pool, league_id=league_id, team=team, season=_SEASON
            )

            # (b) The AI's own real basis, loaded through the identical
            # path `run_phase_hook` uses -- not a re-derived formula.
            _league_snapshot, ctx = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )
            cap_settings = await league_repo.get_cap_settings(pool, league_id, _SEASON)
            ai_headroom_next_season = signing_decisions.apron2_headroom(
                cap_settings, ctx.apron1_room_by_team[team_id]
            )

            # (c) The AI's own bulk cap-projection primitive
            # (`capsheet_service.project_league_cap_sheets`, the exact
            # function `load_gm_inputs` itself calls once to build `ctx.
            # apron1_room_by_team`), read at the SAME season the human
            # gate actually computes at (`_SEASON`) rather than at `ctx.
            # apron1_room_by_team`'s own fixed `_SEASON + 1` row -- see
            # the season-basis note below for why these are two different,
            # both legitimate, questions.
            projections = await capsheet_service.project_league_cap_sheets(
                pool, league_id=league_id, include_holds=False
            )
            this_season_projection = next(p for p in projections[team_id] if p.season == _SEASON)
            ai_headroom_this_season = signing_decisions.apron2_headroom(
                cap_settings, this_season_projection.apron1_room
            )

            # --- Cap-headroom parity, with the one real, disclosed
            # season-basis asymmetry named rather than silently ignored.
            # `_compute_signing_legality_basis`'s own docstring claims it
            # is "the same pure, single-season primitive `project_league_
            # cap_sheets` ... calls once per (team, season)" -- verified
            # here directly, at the SAME season (`_SEASON`) the human
            # gate's own `sign_free_agent` command actually signs into
            # (`start_season=ctx.season`, a human signing takes effect
            # immediately, never next season).
            # Verified this assertion genuinely fails under round 1's
            # original pre-round-2-fix formula too (temporarily reverted
            # to `cap_settings.apron_1 - cap_summary.used` locally, ran
            # this test: `human_basis.apron2_headroom == $184,000,000`
            # vs `ai_headroom_this_season == $172,000,000` -- an
            # assertion failure, not a false pass) before restoring the
            # real fix.
            assert human_basis.apron2_headroom == ai_headroom_this_season
            # `ctx.apron1_room_by_team` (the AI's own REAL in-flight basis
            # for an actual AI-initiated signing) is deliberately anchored
            # to `_SEASON + 1` instead (`gm_service.py`'s own module
            # docstring, judgment call 2: "a signed deal's first year is
            # `next_season`" -- `run_free_agency`'s own `SigningExecution.
            # start_season=next_season`). This is why the human gate's
            # headroom is NOT compared against `ai_headroom_next_season`
            # directly: they answer genuinely different real-world
            # questions (whose books absorb this salary -- this season's,
            # for a human signing effective now, or next season's, for an
            # AI signing that starts next season), not a formula that
            # merely looks different. Escalating year-2 salaries
            # ($4.0M -> $4.2M across 4 contracts, $800K aggregate) make
            # the two genuinely different numbers here, confirming this
            # is a real, checkable relationship, not a coincidence to
            # silently rely on.
            assert ai_headroom_this_season != ai_headroom_next_season
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_physical_roster_count_matches_real_active_count_independent_of_either_formula():
    """Round-4 critic's structural fix on top of round-3's own structural
    fix: the test above only ever asks "does the human gate agree with
    the AI's OWN formula" -- which is exactly why it PASSED on round 3's
    broken state (`ai_roster_count` was itself computed via the same
    wrong-for-this-question `_next_season_roster_count` call, so a human
    gate that copied the AI's mistake agreed with it perfectly). Rounds 2,
    3, and this round's fix all changed what formula the human gate uses;
    none of that formula-vs-formula comparison would ever catch a case
    where BOTH sides used a formula that answers the wrong question for
    what `/sign-free-agent` (a CURRENT-season, immediate-effect command)
    actually needs.

    This test instead compares the human gate's `physical_roster_count`
    against ground truth read directly from Postgres -- a raw `COUNT(*)`
    over `roster_status = 'active'` rows for the team, with NO reference
    to `_next_season_roster_count`/`_occupies_next_season` or any other
    formula on either side. This is basis-independent by construction: it
    would have caught round 2's bug (contracts already lapsed before
    `current_season` silently excluded), round 3's bug (contracts in
    their final year silently excluded), AND would catch a fifth,
    not-yet-imagined season-basis bug the same way, because it never asks
    a season-basis question at all.

    Fixture mixes every shape the last two rounds each got wrong in a
    different way, on the SAME roster: a contract-less ACTIVE player
    (physically rostered, no contract row at all), a player whose
    contract lapsed entirely before `_SEASON` (round 2's case), a player
    whose contract's LAST covered season IS `_SEASON` (its final year,
    round 3's case), and a player mid-contract with seasons remaining
    past `_SEASON` (the ordinary case neither round broke). All 4 are
    real `RosterStatus.ACTIVE` bodies on the team right now -- the ground-
    truth count must read 4, and the human gate must match it exactly."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PT2")

            await _make_player(pool, league_id, team_id)  # contract-less

            lapsed_pid = await _make_player(pool, league_id, team_id)
            await _make_contract(
                pool,
                league_id,
                team_id,
                lapsed_pid,
                start_season=_SEASON - 1,
                salaries=(3_000_000,),
            )

            final_year_pid = await _make_player(pool, league_id, team_id)
            await _make_contract(
                pool,
                league_id,
                team_id,
                final_year_pid,
                start_season=_SEASON,
                salaries=(3_500_000,),  # single year -- last covered season IS _SEASON
            )

            mid_contract_pid = await _make_player(pool, league_id, team_id)
            await _make_contract(
                pool,
                league_id,
                team_id,
                mid_contract_pid,
                start_season=_SEASON,
                salaries=(4_000_000, 4_200_000, 4_400_000),  # runs through _SEASON + 2
            )

            team = await team_repo.get_team(pool, team_id)
            human_basis = await gm_command_service._compute_signing_legality_basis(
                pool, league_id=league_id, team=team, season=_SEASON
            )

            ground_truth_count = await pool.fetchval(
                "SELECT count(*) FROM players WHERE team_id = $1 AND roster_status = $2",
                team_id,
                RosterStatus.ACTIVE.value,
            )

            assert ground_truth_count == 4
            assert human_basis.physical_roster_count == ground_truth_count == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
