"""Live I/O proof for `data/repositories/team_repo.py` — smoke coverage for
teams, team_strategy (including its ON CONFLICT upsert path), and
team_manager_history.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo, team_repo
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Timeline,
    TimelineSource,
)


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Team Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


@pytest.mark.asyncio
async def test_create_get_team_and_list_for_league():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="TST",
                name="Testers",
                city="Testville",
                conference="East",
                division="Atlantic",
                home_market_size=3,
            )
            assert team.timeline == Timeline.RETOOL

            fetched = await team_repo.get_team(pool, team.id)
            assert fetched is not None
            assert fetched.code == "TST"

            by_code = await team_repo.get_team_by_code(pool, league_id, "TST")
            assert by_code is not None
            assert by_code.id == team.id

            teams = await team_repo.list_teams_for_league(pool, league_id)
            assert [t.id for t in teams] == [team.id]

            await team_repo.set_manager(pool, team.id, 99)
            await team_repo.set_timeline(pool, team.id, Timeline.WIN_NOW)
            updated = await team_repo.get_team(pool, team.id)
            assert updated.manager_user_id == 99
            assert updated.timeline == Timeline.WIN_NOW
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_lock_team_row_locks_and_returns_the_same_row():
    """Round-5 critic's B1 fix (`gm_command_service.sign_free_agent`'s
    concurrency race) -- `lock_team_row` (renamed from `get_team_for_
    update`; RMLP Decision 1 changed its SQL to `FOR NO KEY UPDATE`, see
    that function's own docstring) is the row-lock precedent `league_repo.
    get_phase_state_for_update` already established, applied to `teams`.
    Smoke coverage only (the real concurrency proof lives in `tests/
    integration/test_gm_command_service.py::
    test_concurrent_sign_free_agent_calls_do_not_overfill_the_roster`,
    which needs the real race, not just this function in isolation): a
    `SELECT ... FOR NO KEY UPDATE` inside an open `transaction()` returns
    the identical row `get_team` would, and a missing team still returns
    `None` rather than raising."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="LCK",
                name="Lockers",
                city="Lock City",
                conference="East",
                division="Atlantic",
                home_market_size=3,
            )
            async with transaction(pool) as conn:
                locked = await team_repo.lock_team_row(conn, team.id)
                assert locked == team

            async with transaction(pool) as conn:
                missing = await team_repo.lock_team_row(conn, team.id + 999_999)
                assert missing is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_team_round_trips_every_field_distinctly():
    """Every `Team` field gets a distinct value so a mapper that swapped two
    same-typed fields (e.g. `conference`/`division`, both `str`) would fail
    this test where an all-equal or partially-equal fixture could not
    (Phase 0 step-7 critic round, B2 — 7 scripted field-swap mutations
    survived the previous version of this suite)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="DST",
                name="Distinct Name",
                city="Distinct City",
                conference="Distinct Conference",
                division="Distinct Division",
                manager_user_id=4242,
                gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
                timeline=Timeline.WIN_NOW,
                home_market_size=7,
            )
            fetched = await team_repo.get_team(pool, team.id)
            assert fetched.league_id == league_id
            assert fetched.code == "DST"
            assert fetched.name == "Distinct Name"
            assert fetched.city == "Distinct City"
            assert fetched.conference == "Distinct Conference"
            assert fetched.division == "Distinct Division"
            assert fetched.manager_user_id == 4242
            assert fetched.gm_philosophy == GmPhilosophy.YOUTH_DEVELOPER
            assert fetched.timeline == Timeline.WIN_NOW
            assert fetched.home_market_size == 7
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_team_strategy_upsert_on_conflict():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="STR",
                name="Strategists",
                city="Stratcity",
                conference="West",
                division="Pacific",
                home_market_size=2,
            )
            strat = await team_repo.upsert_team_strategy(
                pool,
                league_id=league_id,
                team_id=team.id,
                season=2026,
                pace_target=100,
                offensive_scheme=OffensiveScheme.PNR_HEAVY,
                defensive_scheme=DefensiveScheme.DROP,
                help_aggression=50,
                three_point_bias=50,
                crash_oreb_bias=50,
            )
            assert strat.offensive_scheme == OffensiveScheme.PNR_HEAVY

            updated = await team_repo.upsert_team_strategy(
                pool,
                league_id=league_id,
                team_id=team.id,
                season=2026,
                pace_target=95,
                offensive_scheme=OffensiveScheme.FIVE_OUT,
                defensive_scheme=DefensiveScheme.SWITCH_ALL,
                help_aggression=60,
                three_point_bias=70,
                crash_oreb_bias=40,
                set_by=88,
            )
            # Every tendency-typed field distinct so a mapper field swap
            # (e.g. help_aggression/three_point_bias, both `int`) would fail
            # this assertion block (critic round B2).
            assert updated.pace_target == 95
            assert updated.offensive_scheme == OffensiveScheme.FIVE_OUT
            assert updated.defensive_scheme == DefensiveScheme.SWITCH_ALL
            assert updated.help_aggression == 60
            assert updated.three_point_bias == 70
            assert updated.crash_oreb_bias == 40
            assert updated.set_by == 88

            fetched = await team_repo.get_team_strategy(pool, team.id, 2026)
            assert fetched.pace_target == 95
            assert fetched.help_aggression == 60
            assert fetched.three_point_bias == 70
            assert fetched.crash_oreb_bias == 40

            assert await team_repo.get_team_strategy(pool, team.id, 2099) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_manager_history_record_and_close():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="MGR",
                name="Managers",
                city="Managerton",
                conference="East",
                division="Central",
                home_market_size=1,
            )
            history = await team_repo.record_manager_assignment(
                pool, team_id=team.id, user_id=55, assigned_by=1
            )
            assert history.removed_at is None

            await team_repo.close_manager_assignment(pool, history.id)

            all_history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert len(all_history) == 1
            first_removed_at = all_history[0].removed_at
            assert first_removed_at is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_close_manager_assignment_is_a_noop_on_an_already_closed_row():
    """`close_manager_assignment` must not clobber an existing `removed_at`
    on a second call — this is the audit trail for who managed a team when,
    and a silent overwrite would destroy the true removal timestamp with no
    error (Phase 0 step-7 critic round, B1)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code="NOP",
                name="Noops",
                city="Noopville",
                conference="West",
                division="Southwest",
                home_market_size=1,
            )
            history = await team_repo.record_manager_assignment(
                pool, team_id=team.id, user_id=77, assigned_by=1
            )
            await team_repo.close_manager_assignment(pool, history.id)
            [closed] = await team_repo.list_manager_history_for_team(pool, team.id)
            first_removed_at = closed.removed_at
            assert first_removed_at is not None

            await team_repo.close_manager_assignment(pool, history.id)
            [still_closed] = await team_repo.list_manager_history_for_team(pool, team.id)
            assert still_closed.removed_at == first_removed_at
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_manager_history_for_league_joins_through_teams_and_excludes_other_leagues():
    """Phase 5's transactions merge (docs/design/phase-5-discord-
    presentation.md §22.5/B-12) needs a league-wide read; `team_manager_
    history` carries no `league_id` of its own, so this proves the join
    through `teams` actually scopes correctly -- both to the RIGHT league
    and away from a second one."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            team_a = await team_repo.create_team(
                pool, league_id=league_id, code="LGA", name="League A Team",
                city="A City", conference="East", division="Atlantic", home_market_size=1,
            )
            team_b = await team_repo.create_team(
                pool, league_id=league_id, code="LGB", name="League B Team",
                city="B City", conference="West", division="Pacific", home_market_size=1,
            )
            other_team = await team_repo.create_team(
                pool, league_id=other_league_id, code="OTH", name="Other League Team",
                city="Other City", conference="East", division="Central", home_market_size=1,
            )
            await team_repo.record_manager_assignment(
                pool, team_id=team_a.id, user_id=1, assigned_by=1
            )
            await team_repo.record_manager_assignment(
                pool, team_id=team_b.id, user_id=2, assigned_by=1
            )
            await team_repo.record_manager_assignment(
                pool, team_id=other_team.id, user_id=3, assigned_by=1
            )

            history = await team_repo.list_manager_history_for_league(pool, league_id)
            assert {row.team_id for row in history} == {team_a.id, team_b.id}
            assert {row.user_id for row in history} == {1, 2}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# set_team_timeline -- Decision P5-66/P5-68, the `/timeline` command's own
# writer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_team_timeline_writes_value_and_source_together():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool, league_id=league_id, code="TL1", name="Timeline Team",
                city="TL City", conference="East", division="Atlantic", home_market_size=1,
            )
            assert team.timeline_source == TimelineSource.AUTO

            updated = await team_repo.set_team_timeline(
                pool, team.id, timeline=Timeline.WIN_NOW, source=TimelineSource.MANUAL
            )
            assert updated.timeline == Timeline.WIN_NOW
            assert updated.timeline_source == TimelineSource.MANUAL

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched.timeline == Timeline.WIN_NOW
            assert refetched.timeline_source == TimelineSource.MANUAL
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_team_timeline_auto_leaves_the_stored_value_untouched():
    """`/timeline auto`'s own shape (Decision P5-67's "returning to auto is
    a plain write of `timeline_source = 'auto'`") -- `timeline=None` must
    flip ONLY `timeline_source`, leaving `teams.timeline` exactly where the
    prior write left it, so the next classification pass reads it as
    `previous_by_team`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool, league_id=league_id, code="TL2", name="Timeline Team 2",
                city="TL City 2", conference="East", division="Atlantic", home_market_size=1,
            )
            await team_repo.set_team_timeline(
                pool, team.id, timeline=Timeline.REBUILD, source=TimelineSource.MANUAL
            )

            reverted = await team_repo.set_team_timeline(
                pool, team.id, timeline=None, source=TimelineSource.AUTO
            )
            assert reverted.timeline == Timeline.REBUILD, (
                "auto must not reset the stored timeline value itself"
            )
            assert reverted.timeline_source == TimelineSource.AUTO
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_classifier_set_timeline_never_touches_timeline_source():
    """The load-bearing separation P5-67's round-trip proof depends on:
    the CLASSIFIER's own writer (`set_timeline`, unchanged since Decision
    P2-13) must never flip `timeline_source` back to `'auto'` underneath a
    human's pin -- only `set_team_timeline` (the `/timeline` command's own
    writer, above) ever touches that column."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team = await team_repo.create_team(
                pool, league_id=league_id, code="TL3", name="Timeline Team 3",
                city="TL City 3", conference="East", division="Atlantic", home_market_size=1,
            )
            await team_repo.set_team_timeline(
                pool, team.id, timeline=Timeline.WIN_NOW, source=TimelineSource.MANUAL
            )

            # The classifier's own writer, called exactly as `apply_windows`
            # calls it for every team on every pass, pinned or not.
            await team_repo.set_timeline(pool, team.id, Timeline.WIN_NOW)

            after = await team_repo.get_team(pool, team.id)
            assert after.timeline_source == TimelineSource.MANUAL
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
