"""§7.4 schema-is-truth enforcement tests #3 (mapper/schema drift) and #4
(cascade completeness) -- the two of the four §7.4 tests that need a
repository/domain layer to check against (#1 round-trip and #2 single-head
already live in test_migrations_roundtrip.py, written before `data/` or
`domain/` existed).

    3. Mapper/schema drift: introspect `information_schema.columns` and
       assert every repository dataclass's field set exactly matches the
       columns its mapper reads -- no extra, no missing. This is the test
       that makes `if "col" in keys` unnecessary.
    4. Cascade completeness: assert every FK referencing `leagues` or
       `teams` declares an explicit `ON DELETE` rule. v1 needed migration
       `019_fix_team_cascades` to retrofit this after the fact.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module that touches
real Postgres.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.domain.award import PlayerAward
from dba2.domain.broadcast import GameBroadcast
from dba2.domain.channel import LeagueChannel, TeamGmChannel
from dba2.domain.contract import (
    CapHold,
    Contract,
    ContractYear,
    DeadCapCharge,
    RookieScale,
)
from dba2.domain.draft import Draft, DraftClass, DraftPick, DraftSelection
from dba2.domain.game import Game
from dba2.domain.head_to_head import HeadToHead
from dba2.domain.injury import Injury
from dba2.domain.league import League, LeagueCapSettings, PhaseHistory
from dba2.domain.media import MediaPulseSnapshot, MediaStory, MediaWriter
from dba2.domain.motivation import PlayerMotivation, PlayerMotivationEvent
from dba2.domain.player import Player
from dba2.domain.ratings import PlayerRatings, PlayerRatingsHistory, PlayerTendencies
from dba2.domain.role import PlayerDirective, PlayerRole
from dba2.domain.stats import (
    GamePeriodScore,
    PlayerGameStats,
    PlayerSeasonStats,
    TeamGameStats,
)
from dba2.domain.team import Team, TeamManagerHistory, TeamStrategy
from dba2.domain.trade_offer import TradeOffer
from dba2.domain.transaction import LeagueTransaction, LeagueTransactionAsset

# (table_name, dataclass) registry -- built by reading every repo module's
# `_map_*` function and the table it selects/inserts against (§5's module
# map), cross-checked directly against each `data/repositories/*.py` file
# rather than trusted from the design doc's table list alone. `CapSummary`
# (`domain/league.py`) is deliberately excluded: it is
# `cap_service.get_cap_summary`'s computed return shape, not a table-backed
# dataclass, and has no `_map_*` function or table of its own.
_TABLE_DATACLASSES: list[tuple[str, type]] = [
    ("leagues", League),  # league_repo._map_league
    ("league_cap_settings", LeagueCapSettings),  # league_repo._map_cap_settings
    ("phase_history", PhaseHistory),  # league_repo._map_phase_history
    ("teams", Team),  # team_repo._map_team
    ("team_strategy", TeamStrategy),  # team_repo._map_team_strategy
    ("team_manager_history", TeamManagerHistory),  # team_repo._map_team_manager_history
    ("players", Player),  # player_repo._map_player
    ("player_ratings", PlayerRatings),  # ratings_repo._map_player_ratings
    ("player_ratings_history", PlayerRatingsHistory),  # ratings_repo._map_player_ratings_history
    ("player_tendencies", PlayerTendencies),  # ratings_repo._map_player_tendencies
    ("contracts", Contract),  # contract_repo._map_contract
    ("contract_years", ContractYear),  # contract_repo._map_contract_year
    ("dead_cap_charges", DeadCapCharge),  # contract_repo._map_dead_cap_charge
    ("cap_holds", CapHold),  # contract_repo._map_cap_hold
    ("rookie_scale", RookieScale),  # contract_repo._map_rookie_scale
    ("draft_picks", DraftPick),  # draft_repo._map_draft_pick
    ("drafts", Draft),  # draft_repo._map_draft
    ("draft_selections", DraftSelection),  # draft_repo._map_draft_selection
    ("draft_classes", DraftClass),  # draft_repo._map_draft_class
    ("player_roles", PlayerRole),  # role_repo._map_player_role
    ("player_directives", PlayerDirective),  # role_repo._map_player_directive
    ("player_motivation", PlayerMotivation),  # motivation_repo._map_player_motivation
    # motivation_repo._map_player_motivation_event
    ("player_motivation_events", PlayerMotivationEvent),
    ("player_awards", PlayerAward),  # award_repo._map_player_award
    ("games", Game),  # game_repo._map_game
    ("player_game_stats", PlayerGameStats),  # stats_repo._map_player_game_stats
    ("team_game_stats", TeamGameStats),  # stats_repo._map_team_game_stats
    ("player_season_stats", PlayerSeasonStats),  # stats_repo._map_player_season_stats
    ("injuries", Injury),  # injury_repo._map_injury
    ("game_period_scores", GamePeriodScore),  # stats_repo._map_game_period_score
    ("gm_head_to_head", HeadToHead),  # head_to_head_repo._map_head_to_head
    ("media_stories", MediaStory),  # media_repo._map_media_story
    ("media_pulse_snapshot", MediaPulseSnapshot),  # media_repo._map_pulse
    ("media_writers", MediaWriter),  # media_repo._map_media_writer
    ("trade_offers", TradeOffer),  # offer_repo._map_trade_offer
    ("league_transactions", LeagueTransaction),  # txn_repo._map_transaction
    ("league_transaction_assets", LeagueTransactionAsset),  # txn_repo._map_transaction_asset
    ("league_channels", LeagueChannel),  # channel_repo._map_league_channel
    ("team_gm_channels", TeamGmChannel),  # channel_repo._map_team_gm_channel
    ("game_broadcasts", GameBroadcast),  # broadcast_repo._map_broadcast
]

# records_repo owns no table of its own (§5: "reads stats tables, owns no
# writes to them") -- it reuses stats_repo's own PlayerSeasonStats mapper,
# so it contributes no additional registry entry.

_TABLE_NAMES = [name for name, _ in _TABLE_DATACLASSES]


async def _columns_for_table(pool, table_name: str) -> set[str]:
    rows = await pool.fetch(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table_name,
    )
    return {r["column_name"] for r in rows}


@pytest.mark.asyncio
async def test_registry_covers_every_table_this_project_owns():
    """A guard on the registry above, not on drift itself -- if a future
    migration adds a table with no entry in `_TABLE_DATACLASSES`, the
    per-table drift test below would silently never check it (a
    parametrized test can only fail on cases it was given). Diffs the
    registry's table names against every base table Postgres actually has,
    excluding alembic's own bookkeeping table."""
    pool = await get_pool()
    try:
        rows = await pool.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name != 'alembic_version'
            """
        )
        actual_tables = {r["table_name"] for r in rows}
        registered_tables = set(_TABLE_NAMES)
        assert actual_tables == registered_tables, (
            f"tables missing from _TABLE_DATACLASSES={actual_tables - registered_tables}, "
            f"stale entries in _TABLE_DATACLASSES={registered_tables - actual_tables}"
        )
        assert len(_TABLE_NAMES) == len(set(_TABLE_NAMES)), "duplicate table name in registry"
    finally:
        await close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name,dataclass_type", _TABLE_DATACLASSES, ids=_TABLE_NAMES)
async def test_dataclass_fields_exactly_match_table_columns(table_name, dataclass_type):
    """§7.4 #3, verbatim: "assert every repository dataclass's field set
    exactly matches the columns its mapper reads -- no extra, no
    missing." Field *names* only (not types) -- the field-set match is
    what makes a repo mapper's `r["col"]` indexing safe with no
    `if "col" in keys` fallback (§5's own reasoning for banning that
    pattern)."""
    pool = await get_pool()
    try:
        db_columns = await _columns_for_table(pool, table_name)
        assert db_columns, f"no columns found for table {table_name!r} -- typo in table name?"
        dataclass_fields = {f.name for f in dataclasses.fields(dataclass_type)}
        assert dataclass_fields == db_columns, (
            f"{dataclass_type.__name__} vs. table {table_name!r}: "
            f"missing from dataclass={sorted(db_columns - dataclass_fields)}, "
            f"extra on dataclass (no matching column)={sorted(dataclass_fields - db_columns)}"
        )
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_every_fk_referencing_leagues_or_teams_has_explicit_on_delete():
    """§7.4 #4, verbatim: "assert every FK referencing `leagues` or `teams`
    declares an explicit `ON DELETE` rule." v1 needed migration
    `019_fix_team_cascades` to retrofit this after the fact; dba2 declares
    it at creation (§4's conventions preamble: "every league-scoped table
    carries `league_id` ... `ON DELETE CASCADE`") and this test is the
    assertion that makes that a checked fact instead of a hoped-for
    migration-review habit.

    `pg_constraint.confdeltype` is `'a'` (NO ACTION) for a FK declared with
    no `ON DELETE` clause at all -- Postgres's silent implicit default.
    `'c'`/`'n'`/`'d'`/`'r'` (CASCADE/SET NULL/SET DEFAULT/RESTRICT) are the
    only values this test accepts as "explicit," matching the four rule
    names §7.4 itself lists. `confdeltype`'s Postgres type is `"char"`,
    which asyncpg decodes as a one-byte `bytes` value (`b'c'`), not `str`
    -- decoded to `str` immediately below so every comparison in this
    function works on a normal single-character string.
    """
    pool = await get_pool()
    try:
        rows = await pool.fetch(
            """
            SELECT
                con.conname AS constraint_name,
                cl.relname AS table_name,
                confcl.relname AS referenced_table,
                con.confdeltype AS delete_rule
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_class confcl ON confcl.oid = con.confrelid
            WHERE con.contype = 'f'
              AND confcl.relname IN ('leagues', 'teams')
            """
        )
        assert rows, "expected at least one FK referencing leagues/teams -- query returned none"

        offenders = [
            f"{r['table_name']}.{r['constraint_name']} -> {r['referenced_table']} "
            f"(confdeltype={r['delete_rule'].decode()!r}, implicit NO ACTION)"
            for r in rows
            if r["delete_rule"].decode() not in ("c", "n", "d", "r")
        ]
        assert not offenders, (
            "FK(s) referencing leagues/teams with no explicit ON DELETE rule: "
            f"{offenders}"
        )
    finally:
        await close_pool()
