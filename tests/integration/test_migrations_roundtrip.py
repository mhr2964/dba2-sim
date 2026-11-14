"""§7.4 schema-is-truth enforcement tests #1 and #2 (the ones that apply
purely to migrations, without a repository/domain layer to check against).

#3 (mapper/schema drift) and #4 (cascade completeness) are deferred to
whichever build step adds `data/repositories/` and `domain/` — see
phase-0-foundation.md §8 steps 5-9.

Destructive: these tests upgrade/downgrade the database DATABASE_URL points
at. Point it at the docker-compose scratch Postgres, never anything with
data worth keeping.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_single_head():
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one head, got: {lines}"


def test_upgrade_downgrade_upgrade_round_trip_is_clean():
    up1 = _run_alembic("upgrade", "head")
    assert up1.returncode == 0, up1.stderr

    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up2 = _run_alembic("upgrade", "head")
    assert up2.returncode == 0, up2.stderr


@pytest.mark.asyncio
async def test_migration_009_gm_philosophy_check_rejects_an_unlisted_value():
    """Decision P2-1: `teams.gm_philosophy`'s CHECK must reject any value
    outside the seven `GmPhilosophy` keys -- proves the constraint is
    actually enforced, not just present in the schema."""
    import random

    import asyncpg

    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES ($1, 'CHECK Test League', 2026, 2026, 1, 1)
            RETURNING id
            """,
            random.randint(10_000_000, 2_000_000_000),
        )
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(
                    """
                    INSERT INTO teams
                        (league_id, code, name, city, conference, division,
                         gm_philosophy, home_market_size)
                    VALUES ($1, 'BAD', 'Bad Team', 'Nowhere', 'East', 'Atlantic',
                            'not_a_real_philosophy', 1)
                    """,
                    league_id,
                )
            # The seven real values must all still be legal.
            for value in (
                "star_maxer", "egalitarian", "defense_first", "tendency_respecter",
                "vet_overrater", "youth_developer", "chaos",
            ):
                await pool.execute(
                    """
                    INSERT INTO teams
                        (league_id, code, name, city, conference, division,
                         gm_philosophy, home_market_size)
                    VALUES ($1, $2, $2, 'Nowhere', 'East', 'Atlantic', $3, 1)
                    """,
                    league_id,
                    value[:10],
                    value,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_009_rookie_scale_repair_lands_row_30_and_leaves_1_through_29_untouched():
    """Decision P2-33: row 30's four values must land exactly as repaired,
    and rows 1-29 must stay byte-identical to migration 003's own seed --
    the assertion that stops a data repair from quietly widening into a
    re-seed. Rows 1-29's golden values are `tests.fixtures.managers.
    ROOKIE_SCALE`'s own (that fixture's docstring states it mirrors
    migration 003 verbatim, un-repaired, and is itself the source
    `test_picks.py` already depends on for the same 29 rows)."""
    from dba2.data.db import close_pool, get_pool
    from tests.fixtures.managers import ROOKIE_SCALE

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        rows = await pool.fetch(
            "SELECT pick_number, year_1_salary, year_2_salary, year_3_salary, year_4_salary "
            "FROM rookie_scale ORDER BY pick_number"
        )
        assert len(rows) == 30

        for r in rows[:29]:
            golden = ROOKIE_SCALE[r["pick_number"]]
            assert r["year_1_salary"] == golden.year_1_salary
            assert r["year_2_salary"] == golden.year_2_salary
            assert r["year_3_salary"] == golden.year_3_salary
            assert r["year_4_salary"] == golden.year_4_salary

        row_30 = rows[29]
        assert row_30["pick_number"] == 30
        assert row_30["year_1_salary"] == 3_079_775
        assert row_30["year_2_salary"] == 3_233_763
        assert row_30["year_3_salary"] == 3_387_752
        assert row_30["year_4_salary"] == 3_064_280
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_009_repair_restores_pick_value_monotonicity_at_29_to_30():
    """§12 step 5's own named obligation, owed by step 7 (`test_picks.py`
    lines 262-272 and `picks.py` lines 53-59 both name this step as owner;
    `docs/ledger/phase-2.md` row 37 records the same promise): once
    migration 009 exists, assert `pick_value(30) <= pick_value(29)` at all
    28 (philosophy, window) pairs against the REAL post-migration
    `rookie_scale` table -- not `tests.fixtures.managers.ROOKIE_SCALE`,
    which is deliberately un-repaired and is what step 5's own
    `test_pick_value_is_monotone_non_increasing_...` carves the 29->30
    boundary out of. That fixture-level test only proves the pre-repair
    inversion still exists (by design); this test is the one that proves
    the repair actually fixes it, which is the entire reason migration 009
    exists (Decision P2-33)."""
    from dba2.data.db import close_pool, get_pool
    from dba2.data.repositories import contract_repo
    from dba2.domain.team import GmPhilosophy
    from dba2.managers.picks import RookieScaleTable, pick_value
    from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
    from tests.fixtures.managers import CAP_SETTINGS

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        rows = await contract_repo.list_rookie_scale(pool)
        assert len(rows) == 30
        live_rookie_scale: RookieScaleTable = {row.pick_number: row for row in rows}

        violations = []
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                p29 = pick_value(
                    29, 0, philosophy, window,
                    rookie_scale=live_rookie_scale, cap_settings=CAP_SETTINGS,
                    tuning=DEFAULT_TUNING,
                )
                p30 = pick_value(
                    30, 0, philosophy, window,
                    rookie_scale=live_rookie_scale, cap_settings=CAP_SETTINGS,
                    tuning=DEFAULT_TUNING,
                )
                if p30 > p29:
                    violations.append((philosophy, window, p29, p30))

        assert not violations, (
            f"pick 30 priced above pick 29 at {len(violations)}/28 pairs against "
            f"the live post-migration-009 rookie_scale table: {violations}"
        )
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_round_trip_survives_populated_tables():
    """Downgrade to base must succeed via the declared ON DELETE/DROP TABLE
    ordering even when rows (and their FKs) exist — not just on an empty DB.
    """
    from dba2.data.db import close_pool, get_pool

    up1 = _run_alembic("upgrade", "head")
    assert up1.returncode == 0, up1.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (1, 'Round Trip League', 2026, 2026, 1, 42)
            RETURNING id
            """
        )
        team_id = await pool.fetchval(
            """
            INSERT INTO teams
                (league_id, code, name, city, conference, division, home_market_size)
            VALUES ($1, 'RTL', 'Round Trippers', 'Testville', 'East', 'Atlantic', 1)
            RETURNING id
            """,
            league_id,
        )
        assert league_id is not None
        assert team_id is not None
    finally:
        await close_pool()

    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up2 = _run_alembic("upgrade", "head")
    assert up2.returncode == 0, up2.stderr


@pytest.mark.asyncio
async def test_migration_010_gm_head_to_head_defaults_and_cascades():
    """phase-3-inter-manager-society.md §7 (Decision P3-22, P3-35): pure
    schema checks at the migration level, distinct from
    `tests/integration/test_head_to_head_repo.py`'s float-type/repo-level
    coverage.

    - Counters omitted on INSERT land as `0`, not `NULL` (migration's
      `DEFAULT 0` on every counter column, matching this schema's existing
      convention for per-row counters).
    - `last_gap_by_opponent`/`last_season_interacted` stay `NULL` when
      omitted (no default -- 0.0/season 0 are real, distinguishable
      values, not "no event yet" sentinels).
    - `ON DELETE CASCADE` on `opponent_id` actually removes the row when
      the opponent team is deleted, not just when `team_id`'s own team is
      deleted -- the two FKs to `teams(id)` must both cascade, not only
      the first one written."""
    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (2, 'H2H Migration Test League', 2026, 2026, 1, 7)
            RETURNING id
            """
        )
        try:
            team_a = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'HAA', 'H2H A', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )
            team_b = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'HBB', 'H2H B', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )

            await pool.execute(
                """
                INSERT INTO gm_head_to_head (league_id, team_id, opponent_id, updated_season)
                VALUES ($1, $2, $3, 2026)
                """,
                league_id,
                team_a,
                team_b,
            )

            row = await pool.fetchrow(
                "SELECT * FROM gm_head_to_head WHERE team_id = $1 AND opponent_id = $2",
                team_a,
                team_b,
            )
            assert row["opened"] == 0
            assert row["received"] == 0
            assert row["accepts_by_opponent"] == 0
            assert row["rejects_by_opponent"] == 0
            assert row["accepts_by_self"] == 0
            assert row["rejects_by_self"] == 0
            assert row["counters_given"] == 0
            assert row["counters_received"] == 0
            assert row["executions"] == 0
            assert row["refused_illegal"] == 0
            assert row["far_reject_streak"] == 0
            assert row["gap_by_opponent_sum"] == 0
            assert row["last_gap_by_opponent"] is None
            assert row["last_season_interacted"] is None
            assert row["updated_season"] == 2026

            # Deleting the OPPONENT team (not team_id's own team) must
            # still cascade -- both FKs to teams(id) are independently
            # ON DELETE CASCADE.
            await pool.execute("DELETE FROM teams WHERE id = $1", team_b)
            gone = await pool.fetchrow(
                "SELECT 1 FROM gm_head_to_head WHERE team_id = $1 AND opponent_id = $2",
                team_a,
                team_b,
            )
            assert gone is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_011_media_writers_check_constraint_and_partial_unique_indexes():
    """Decision P4-66's `CHECK ((persona = 'beat_writer') = (team_id IS NOT
    NULL))`, and Decision P4-48's two partial unique indexes -- pure schema
    checks at the migration level, distinct from `tests/integration/
    test_media_repo.py`'s repo-level coverage."""
    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (3, 'Media Migration Test League', 2026, 2026, 1, 11)
            RETURNING id
            """
        )
        try:
            team_id = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'MMA', 'Media A', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )

            # CHECK: beat_writer WITHOUT a team_id is rejected.
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(
                    "INSERT INTO media_writers (league_id, team_id, persona, name) "
                    "VALUES ($1, NULL, 'beat_writer', 'Ghost Writer')",
                    league_id,
                )

            # CHECK: a non-beat_writer archetype WITH a team_id is rejected.
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(
                    "INSERT INTO media_writers (league_id, team_id, persona, name) "
                    "VALUES ($1, $2, 'hot_take', 'Scoped Hot Take')",
                    league_id,
                    team_id,
                )

            # The two legal shapes both succeed.
            await pool.execute(
                "INSERT INTO media_writers (league_id, team_id, persona, name) "
                "VALUES ($1, $2, 'beat_writer', 'Real Beat Writer')",
                league_id,
                team_id,
            )
            await pool.execute(
                "INSERT INTO media_writers (league_id, team_id, persona, name) "
                "VALUES ($1, NULL, 'hot_take', 'Real Hot Take')",
                league_id,
            )

            # ux_media_writers_team: a second beat_writer for the SAME team
            # violates the partial unique index.
            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute(
                    "INSERT INTO media_writers (league_id, team_id, persona, name) "
                    "VALUES ($1, $2, 'beat_writer', 'Duplicate Beat Writer')",
                    league_id,
                    team_id,
                )

            # ux_media_writers_league: a second league-wide hot_take
            # violates the OTHER partial unique index -- proving the two
            # NULL-team_id rows do NOT silently compare unequal the way a
            # naive `UNIQUE (league_id, team_id, persona)` would have let
            # them (Decision P4-48's own reason for two partial indexes
            # instead of one plain constraint).
            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute(
                    "INSERT INTO media_writers (league_id, team_id, persona, name) "
                    "VALUES ($1, NULL, 'hot_take', 'Duplicate Hot Take')",
                    league_id,
                )

            # A DIFFERENT league-wide persona for the same league is fine --
            # the partial index is scoped on (league_id, persona), not just
            # (league_id, team_id IS NULL).
            await pool.execute(
                "INSERT INTO media_writers (league_id, team_id, persona, name) "
                "VALUES ($1, NULL, 'analyst', 'Real Analyst')",
                league_id,
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_011_media_stories_no_uniqueness_on_template_per_cycle():
    """Decision P4-32's own closing note, proven rather than merely
    asserted in a docstring: two different subjects drawing the same
    template in the same cycle is a normal outcome, not a constraint
    violation."""
    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (4, 'Media Uniqueness Test League', 2026, 2026, 1, 12)
            RETURNING id
            """
        )
        try:
            team_a = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'MUA', 'Uniq A', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )
            team_b = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'MUB', 'Uniq B', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )

            for team_id in (team_a, team_b):
                await pool.execute(
                    """
                    INSERT INTO media_stories
                        (league_id, season, phase, cycle_index, story_kind, voice,
                         template_id, slot_bindings, rendered_text, salience,
                         subject_team_id)
                    VALUES ($1, 2026, 'REGULAR_SEASON', 0, 'streak', 'beat_writer',
                            'fallback_streak', '{}'::jsonb, 'A streak story.', 0.5, $2)
                    """,
                    league_id,
                    team_id,
                )

            rows = await pool.fetch(
                "SELECT subject_team_id FROM media_stories WHERE league_id = $1 "
                "AND template_id = 'fallback_streak'",
                league_id,
            )
            assert len(rows) == 2
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_011_media_stories_set_null_on_subject_and_writer_delete():
    """`subject_player_id`/`subject_team_id`/`writer_id` are all `ON DELETE
    SET NULL` -- a story is league history and survives its subject's or
    its writer's deletion, unlike `league_id`'s own `ON DELETE CASCADE`."""
    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (5, 'Media SET NULL Test League', 2026, 2026, 1, 13)
            RETURNING id
            """
        )
        try:
            team_id = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'MSN', 'Set Null', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )
            player_id = await pool.fetchval(
                """
                INSERT INTO players
                    (league_id, first_name, last_name, primary_position, height_in,
                     weight_lb, wingspan_in, birth_date, roster_status, overall_cached,
                     overall_computed_at, team_id)
                VALUES ($1, 'Set', 'Null', 'SF', 79, 215, 82, '1997-01-01', 'active',
                        70, NOW(), $2)
                RETURNING id
                """,
                league_id,
                team_id,
            )
            writer_id = await pool.fetchval(
                "INSERT INTO media_writers (league_id, team_id, persona, name) "
                "VALUES ($1, $2, 'beat_writer', 'Set Null Writer') RETURNING id",
                league_id,
                team_id,
            )
            story_id = await pool.fetchval(
                """
                INSERT INTO media_stories
                    (league_id, season, phase, cycle_index, story_kind, voice,
                     template_id, slot_bindings, rendered_text, salience,
                     subject_player_id, subject_team_id, writer_id)
                VALUES ($1, 2026, 'REGULAR_SEASON', 0, 'milestone', 'beat_writer',
                        'fallback_milestone', '{}'::jsonb, 'A milestone story.', 0.5,
                        $2, $3, $4)
                RETURNING id
                """,
                league_id,
                player_id,
                team_id,
                writer_id,
            )

            await pool.execute("DELETE FROM players WHERE id = $1", player_id)
            await pool.execute("DELETE FROM media_writers WHERE id = $1", writer_id)

            row = await pool.fetchrow(
                "SELECT subject_player_id, subject_team_id, writer_id FROM media_stories "
                "WHERE id = $1",
                story_id,
            )
            assert row["subject_player_id"] is None
            assert row["subject_team_id"] == team_id
            assert row["writer_id"] is None

            await pool.execute("DELETE FROM teams WHERE id = $1", team_id)
            row = await pool.fetchrow(
                "SELECT subject_team_id FROM media_stories WHERE id = $1", story_id
            )
            assert row["subject_team_id"] is None

            still_there = await pool.fetchrow(
                "SELECT 1 FROM media_stories WHERE id = $1", story_id
            )
            assert still_there is not None, "league_id CASCADE must not fire on a team delete"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_migration_011_media_pulse_snapshot_composite_pk_and_cascade():
    """Composite PK `(league_id, season, cycle_index, team_id)`, and `ON
    DELETE CASCADE` on both `league_id` and `team_id` (operational state,
    not persisted history -- see the migration's own docstring)."""
    from dba2.data.db import close_pool, get_pool

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    pool = await get_pool()
    try:
        league_id = await pool.fetchval(
            """
            INSERT INTO leagues
                (discord_guild_id, name, start_season_year, current_season,
                 commissioner_user_id, rng_seed)
            VALUES (6, 'Media Pulse Test League', 2026, 2026, 1, 14)
            RETURNING id
            """
        )
        try:
            team_id = await pool.fetchval(
                """
                INSERT INTO teams
                    (league_id, code, name, city, conference, division, home_market_size)
                VALUES ($1, 'MPS', 'Pulse', 'Testville', 'East', 'Atlantic', 1)
                RETURNING id
                """,
                league_id,
            )
            await pool.execute(
                """
                INSERT INTO media_pulse_snapshot
                    (league_id, season, cycle_index, team_id, wins, losses,
                     standings_position, streak_length)
                VALUES ($1, 2026, 0, $2, 5, 3, 1, 5)
                """,
                league_id,
                team_id,
            )

            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute(
                    """
                    INSERT INTO media_pulse_snapshot
                        (league_id, season, cycle_index, team_id, wins, losses,
                         standings_position, streak_length)
                    VALUES ($1, 2026, 0, $2, 99, 99, 30, -20)
                    """,
                    league_id,
                    team_id,
                )

            await pool.execute("DELETE FROM teams WHERE id = $1", team_id)
            gone = await pool.fetchrow(
                "SELECT 1 FROM media_pulse_snapshot WHERE league_id = $1 AND team_id = $2",
                league_id,
                team_id,
            )
            assert gone is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
