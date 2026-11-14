"""gm_head_to_head — `head_to_head_repo`'s one table (§7, Decision P3-22).

phase-3-inter-manager-society.md §7. §11 step 10: pure persistence, no
caller wired yet.

Unlike `player_motivation` (insert once, then two narrow UPDATE splits),
`gm_head_to_head` rows get rewritten repeatedly across a season's trade
windows and at every season-boundary decay pass (Decision P3-25) — a
trade-window's worth of counter increments or a decay pass touches many of
a league's 870 rows at once, so this module exposes one bulk upsert, not a
per-row insert/update split. `upsert_head_to_head_for_league` mirrors
`stats_repo.upsert_game_period_scores`'s `UNNEST` + `ON CONFLICT DO UPDATE`
shape — one round trip for however many rows the caller has in memory,
rather than one query per row (870 round trips for a full-league rewrite
otherwise).

`list_head_to_head_for_league` is the other half of §7's stated contract:
"one `SELECT ... WHERE league_id = $1` is the whole loader, no N+1
concern." Every counter column is `double precision`, which asyncpg maps to
Python `float` — never `Decimal` the way `numeric` would (HANDOFF's
recurring bulk-`SUM`/aggregate bug class); `_map_head_to_head` returns those
columns straight from the driver rather than casting, because there is
nothing to cast — the migration's column types are what make this true, not
this module.
"""
from __future__ import annotations

import asyncpg

from dba2.data.db import Executor
from dba2.domain.head_to_head import HeadToHead

_HEAD_TO_HEAD_COLUMNS = """
    league_id, team_id, opponent_id, opened, received, accepts_by_opponent,
    rejects_by_opponent, accepts_by_self, rejects_by_self, counters_given,
    counters_received, executions, refused_illegal, far_reject_streak,
    gap_by_opponent_sum, last_gap_by_opponent, last_season_interacted,
    updated_season
"""


def _map_head_to_head(r: asyncpg.Record) -> HeadToHead:
    return HeadToHead(
        league_id=r["league_id"],
        team_id=r["team_id"],
        opponent_id=r["opponent_id"],
        opened=r["opened"],
        received=r["received"],
        accepts_by_opponent=r["accepts_by_opponent"],
        rejects_by_opponent=r["rejects_by_opponent"],
        accepts_by_self=r["accepts_by_self"],
        rejects_by_self=r["rejects_by_self"],
        counters_given=r["counters_given"],
        counters_received=r["counters_received"],
        executions=r["executions"],
        refused_illegal=r["refused_illegal"],
        far_reject_streak=r["far_reject_streak"],
        gap_by_opponent_sum=r["gap_by_opponent_sum"],
        last_gap_by_opponent=r["last_gap_by_opponent"],
        last_season_interacted=r["last_season_interacted"],
        updated_season=r["updated_season"],
    )


async def list_head_to_head_for_league(ex: Executor, league_id: int) -> list[HeadToHead]:
    """Bulk loader — §7's stated contract, one query for a league's whole
    870-row block (30 teams x 29 opponents), no N+1."""
    rows = await ex.fetch(
        f"SELECT {_HEAD_TO_HEAD_COLUMNS} FROM gm_head_to_head WHERE league_id = $1",
        league_id,
    )
    return [_map_head_to_head(r) for r in rows]


async def upsert_head_to_head_for_league(
    ex: Executor, rows: list[HeadToHead]
) -> list[HeadToHead]:
    """Bulk upsert via `UNNEST` + `ON CONFLICT DO UPDATE` — a trade-window's
    counter increments or a season-boundary decay pass (Decision P3-25)
    rewrites many of a league's rows at once; this is one round trip for
    all of them, mirroring `stats_repo.upsert_game_period_scores`. `rows`
    need not all share the same `league_id`, though every real caller's
    batch will."""
    if not rows:
        return []
    result = await ex.fetch(
        f"""
        INSERT INTO gm_head_to_head ({_HEAD_TO_HEAD_COLUMNS})
        SELECT * FROM UNNEST(
            $1::int[], $2::int[], $3::int[], $4::float8[], $5::float8[],
            $6::float8[], $7::float8[], $8::float8[], $9::float8[],
            $10::float8[], $11::float8[], $12::float8[], $13::float8[],
            $14::float8[], $15::float8[], $16::float8[], $17::int[],
            $18::int[]
        )
        ON CONFLICT (league_id, team_id, opponent_id) DO UPDATE SET
            opened = EXCLUDED.opened,
            received = EXCLUDED.received,
            accepts_by_opponent = EXCLUDED.accepts_by_opponent,
            rejects_by_opponent = EXCLUDED.rejects_by_opponent,
            accepts_by_self = EXCLUDED.accepts_by_self,
            rejects_by_self = EXCLUDED.rejects_by_self,
            counters_given = EXCLUDED.counters_given,
            counters_received = EXCLUDED.counters_received,
            executions = EXCLUDED.executions,
            refused_illegal = EXCLUDED.refused_illegal,
            far_reject_streak = EXCLUDED.far_reject_streak,
            gap_by_opponent_sum = EXCLUDED.gap_by_opponent_sum,
            last_gap_by_opponent = EXCLUDED.last_gap_by_opponent,
            last_season_interacted = EXCLUDED.last_season_interacted,
            updated_season = EXCLUDED.updated_season
        RETURNING {_HEAD_TO_HEAD_COLUMNS}
        """,
        [r.league_id for r in rows],
        [r.team_id for r in rows],
        [r.opponent_id for r in rows],
        [r.opened for r in rows],
        [r.received for r in rows],
        [r.accepts_by_opponent for r in rows],
        [r.rejects_by_opponent for r in rows],
        [r.accepts_by_self for r in rows],
        [r.rejects_by_self for r in rows],
        [r.counters_given for r in rows],
        [r.counters_received for r in rows],
        [r.executions for r in rows],
        [r.refused_illegal for r in rows],
        [r.far_reject_streak for r in rows],
        [r.gap_by_opponent_sum for r in rows],
        [r.last_gap_by_opponent for r in rows],
        [r.last_season_interacted for r in rows],
        [r.updated_season for r in rows],
    )
    return [_map_head_to_head(r) for r in result]
