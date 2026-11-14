"""player_motivation, player_motivation_events — `motivation_repo`'s tables
(§5). Pillar 5's agency vector.

phase-0-foundation.md §4.6.

`player_motivation` splits into trait columns (near-immutable, set at
creation, drift only slowly) and state columns (rewritten at trade deadline
/ offseason checkpoints, §4.6). That split isn't spelled out as a repo
function boundary anywhere in §4.6/§5 — it's this module's own read of the
domain docstring's "trait columns ... then state columns" framing — so
`update_motivation_state` only ever touches the state group and
`update_motivation_traits` only ever touches the trait group; neither can
accidentally clobber the other's columns. A reviewer should double-check
this split matches how `phase/service.py`'s future checkpoint logic
actually wants to call it.

Both update functions require every state/trait column as a keyword with
no default (`request_opened_in_season` included, `int | None` but
mandatory) and return `None`, matching every other repo's UPDATE
convention (`team_repo.set_manager`, `league_repo.set_current_phase`,
etc.) — a caller must explicitly pass `None` to clear
`request_opened_in_season` rather than it silently dropping out via a
default, and must re-`get_player_motivation` if it wants the post-update
row (Phase 0 step-7 critic round, B1/B4: the prior version defaulted this
column to `None`, so any caller updating satisfaction without also
re-passing the open season silently wiped it, and separately used
`RETURNING` with a `-> PlayerMotivation` annotation that raised a raw
`TypeError` on a missing `player_id` instead of returning cleanly).

`player_motivation_events` is append-only (§4.6: "no black box" — every
satisfaction change points at the event that caused it), so this module
only ever inserts and lists it, never updates or deletes a row.
`list_motivation_events_for_player` orders by `(created_at, id)`, not
`created_at` alone — `created_at` is transaction time, so a batch of events
written inside one `transaction()` (the normal production shape for a
checkpoint) can share one timestamp; `id`'s insertion-order guarantee is
the only reliable tiebreaker (critic round, B2, reproduced live: 200k
same-timestamp events came back out of insertion order under a forced
parallel scan without it).
"""
from __future__ import annotations

import asyncpg

from dba2.data.db import Executor
from dba2.domain.motivation import (
    MarketPreference,
    MotivationAxis,
    PlayerMotivation,
    PlayerMotivationEvent,
    RequestState,
)
from dba2.domain.phase import Phase

_PLAYER_MOTIVATION_COLUMNS = """
    player_id, league_id, weight_winning, weight_role_usage, weight_loyalty,
    weight_money, weight_legacy, hometown_team_id, market_preference,
    star_leverage, satisfaction_winning, satisfaction_role_usage,
    satisfaction_loyalty, satisfaction_money, satisfaction_legacy, morale,
    trust_in_management, request_state, request_opened_in_season,
    last_evaluated_season, last_checkpoint_phase
"""

_PLAYER_MOTIVATION_EVENT_COLUMNS = """
    id, league_id, player_id, season, phase, event_type, axis, delta, note, created_at,
    from_state, to_state
"""


def _map_player_motivation(r: asyncpg.Record) -> PlayerMotivation:
    return PlayerMotivation(
        player_id=r["player_id"],
        league_id=r["league_id"],
        weight_winning=r["weight_winning"],
        weight_role_usage=r["weight_role_usage"],
        weight_loyalty=r["weight_loyalty"],
        weight_money=r["weight_money"],
        weight_legacy=r["weight_legacy"],
        hometown_team_id=r["hometown_team_id"],
        market_preference=MarketPreference(r["market_preference"]),
        star_leverage=r["star_leverage"],
        satisfaction_winning=r["satisfaction_winning"],
        satisfaction_role_usage=r["satisfaction_role_usage"],
        satisfaction_loyalty=r["satisfaction_loyalty"],
        satisfaction_money=r["satisfaction_money"],
        satisfaction_legacy=r["satisfaction_legacy"],
        morale=r["morale"],
        trust_in_management=r["trust_in_management"],
        request_state=RequestState(r["request_state"]),
        request_opened_in_season=r["request_opened_in_season"],
        last_evaluated_season=r["last_evaluated_season"],
        last_checkpoint_phase=(
            Phase(r["last_checkpoint_phase"]) if r["last_checkpoint_phase"] is not None else None
        ),
    )


def _map_player_motivation_event(r: asyncpg.Record) -> PlayerMotivationEvent:
    return PlayerMotivationEvent(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        season=r["season"],
        phase=Phase(r["phase"]),
        event_type=r["event_type"],
        axis=MotivationAxis(r["axis"]),
        delta=r["delta"],
        note=r["note"],
        created_at=r["created_at"],
        from_state=RequestState(r["from_state"]) if r["from_state"] is not None else None,
        to_state=RequestState(r["to_state"]) if r["to_state"] is not None else None,
    )


async def create_player_motivation(
    ex: Executor,
    *,
    player_id: int,
    league_id: int,
    weight_winning: int,
    weight_role_usage: int,
    weight_loyalty: int,
    weight_money: int,
    weight_legacy: int,
    star_leverage: int,
    satisfaction_winning: int,
    satisfaction_role_usage: int,
    satisfaction_loyalty: int,
    satisfaction_money: int,
    satisfaction_legacy: int,
    morale: int,
    trust_in_management: int,
    last_evaluated_season: int,
    hometown_team_id: int | None = None,
    market_preference: MarketPreference = MarketPreference.NEUTRAL,
    request_state: RequestState = RequestState.CONTENT,
    request_opened_in_season: int | None = None,
) -> PlayerMotivation:
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_motivation
            (player_id, league_id, weight_winning, weight_role_usage,
             weight_loyalty, weight_money, weight_legacy, hometown_team_id,
             market_preference, star_leverage, satisfaction_winning,
             satisfaction_role_usage, satisfaction_loyalty, satisfaction_money,
             satisfaction_legacy, morale, trust_in_management, request_state,
             request_opened_in_season, last_evaluated_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20)
        RETURNING {_PLAYER_MOTIVATION_COLUMNS}
        """,
        player_id,
        league_id,
        weight_winning,
        weight_role_usage,
        weight_loyalty,
        weight_money,
        weight_legacy,
        hometown_team_id,
        market_preference.value,
        star_leverage,
        satisfaction_winning,
        satisfaction_role_usage,
        satisfaction_loyalty,
        satisfaction_money,
        satisfaction_legacy,
        morale,
        trust_in_management,
        request_state.value,
        request_opened_in_season,
        last_evaluated_season,
    )
    return _map_player_motivation(row)


async def get_player_motivation(ex: Executor, player_id: int) -> PlayerMotivation | None:
    row = await ex.fetchrow(
        f"SELECT {_PLAYER_MOTIVATION_COLUMNS} FROM player_motivation WHERE player_id = $1",
        player_id,
    )
    return _map_player_motivation(row) if row is not None else None


async def list_player_motivation_for_league(ex: Executor, league_id: int) -> list[PlayerMotivation]:
    rows = await ex.fetch(
        f"SELECT {_PLAYER_MOTIVATION_COLUMNS} FROM player_motivation WHERE league_id = $1",
        league_id,
    )
    return [_map_player_motivation(r) for r in rows]


async def update_motivation_state(
    ex: Executor,
    player_id: int,
    *,
    satisfaction_winning: int,
    satisfaction_role_usage: int,
    satisfaction_loyalty: int,
    satisfaction_money: int,
    satisfaction_legacy: int,
    morale: int,
    trust_in_management: int,
    request_state: RequestState,
    request_opened_in_season: int | None,
    last_evaluated_season: int,
    last_checkpoint_phase: Phase | None,
) -> None:
    """Rewrites only the state columns (§4.6) — trade-deadline / offseason
    checkpoint territory. Never touches the trait columns; see
    `update_motivation_traits` for those.

    `request_opened_in_season` has no default: a caller must explicitly
    pass `None` to clear it or the current value to keep it, rather than it
    silently dropping out (critic round, B1).

    `last_checkpoint_phase` follows the same rule for the same reason
    (phase-5-stage-e-advance.md P5-99.r.2): the per-player guard that stops a
    resumed checkpoint folding a player twice. A defaulted guard column would
    silently clear itself on any write that forgot it, and a cleared guard is a
    double-fold. A seeding write that must not be skipped passes `None`."""
    await ex.execute(
        """
        UPDATE player_motivation
        SET satisfaction_winning = $2, satisfaction_role_usage = $3,
            satisfaction_loyalty = $4, satisfaction_money = $5,
            satisfaction_legacy = $6, morale = $7, trust_in_management = $8,
            request_state = $9, request_opened_in_season = $10,
            last_evaluated_season = $11, last_checkpoint_phase = $12
        WHERE player_id = $1
        """,
        player_id,
        satisfaction_winning,
        satisfaction_role_usage,
        satisfaction_loyalty,
        satisfaction_money,
        satisfaction_legacy,
        morale,
        trust_in_management,
        request_state.value,
        request_opened_in_season,
        last_evaluated_season,
        None if last_checkpoint_phase is None else last_checkpoint_phase.value,
    )


async def update_motivation_traits(
    ex: Executor,
    player_id: int,
    *,
    weight_winning: int,
    weight_role_usage: int,
    weight_loyalty: int,
    weight_money: int,
    weight_legacy: int,
    star_leverage: int,
    market_preference: MarketPreference,
    hometown_team_id: int | None,
) -> None:
    """Rewrites only the trait columns (§4.6) — rare, slow drift, distinct
    from the per-checkpoint state rewrite in `update_motivation_state`."""
    await ex.execute(
        """
        UPDATE player_motivation
        SET weight_winning = $2, weight_role_usage = $3, weight_loyalty = $4,
            weight_money = $5, weight_legacy = $6, star_leverage = $7,
            market_preference = $8, hometown_team_id = $9
        WHERE player_id = $1
        """,
        player_id,
        weight_winning,
        weight_role_usage,
        weight_loyalty,
        weight_money,
        weight_legacy,
        star_leverage,
        market_preference.value,
        hometown_team_id,
    )


async def create_motivation_event(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    season: int,
    phase: Phase,
    event_type: str,
    axis: MotivationAxis,
    delta: int,
    note: str | None = None,
    from_state: RequestState | None = None,
    to_state: RequestState | None = None,
) -> PlayerMotivationEvent:
    """`from_state`/`to_state` (migration 021, docs/design/media-v2-
    newsroom.md MV-2b) are two DEFAULTED keyword params, not a
    signature-breaking pair -- every existing call site (the per-axis
    satisfaction-swing writes in `agency_service.py`'s own checkpoint loop)
    keeps working unchanged, passing neither. Only the ONE ladder-
    transition insert in that same loop passes both, real values, matching
    the transition `note` already records as free text."""
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_motivation_events
            (league_id, player_id, season, phase, event_type, axis, delta, note,
             from_state, to_state)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_PLAYER_MOTIVATION_EVENT_COLUMNS}
        """,
        league_id,
        player_id,
        season,
        phase.value,
        event_type,
        axis.value,
        delta,
        note,
        from_state.value if from_state is not None else None,
        to_state.value if to_state is not None else None,
    )
    return _map_player_motivation_event(row)


async def list_motivation_events_for_player(
    ex: Executor, player_id: int
) -> list[PlayerMotivationEvent]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_MOTIVATION_EVENT_COLUMNS} FROM player_motivation_events
        WHERE player_id = $1
        ORDER BY created_at, id
        """,
        player_id,
    )
    return [_map_player_motivation_event(r) for r in rows]


async def list_motivation_events_for_league(
    ex: Executor, *, league_id: int, season: int, event_type: str
) -> list[PlayerMotivationEvent]:
    """Phase 4 Stage D step 17's bulk read (docs/design/phase-4-media-and-
    player-agency.md §7, Decision P4-27): media's own event-sourced intake
    needs "real events already available" at O(events), never a per-player
    N+1 loop over the whole roster to find who transitioned this season.
    One league-scoped, season-scoped, event-type-scoped query -- the same
    shape `list_player_motivation_for_league` already establishes for the
    state table, extended to the event log. **Only `dba2/services/agency_
    service.py` may call this** (`tests/unit/test_layering.py::test_only_
    agency_service_imports_motivation_repo`'s existing whole-file rule
    covers this function too, unchanged, since it names the whole module,
    not individual functions) -- `dba2/services/media_service.py` reads it
    through `agency_service.py`'s own read-model wrapper, matching Decision
    P4-16's "one reader of one table" pattern."""
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_MOTIVATION_EVENT_COLUMNS} FROM player_motivation_events
        WHERE league_id = $1 AND season = $2 AND event_type = $3
        ORDER BY created_at, id
        """,
        league_id,
        season,
        event_type,
    )
    return [_map_player_motivation_event(r) for r in rows]


async def get_last_ladder_transition_season(
    ex: Executor, player_id: int, *, event_type: str
) -> int | None:
    """Round-5 critic nit 4: `services/agency_service.py`'s dwell check
    (Decision P4-56/P4-71) needs only `max(season)` over this player's
    `ladder_transition` rows, but used to load that player's ENTIRE event
    history every checkpoint (~10 rows/season/player, unbounded, forever)
    just to fold it client-side -- an N+1-shaped read sitting next to a
    module docstring that specifically claims to avoid that shape
    elsewhere. `MAX(season)` at the DB is the same query, bounded."""
    value = await ex.fetchval(
        """
        SELECT MAX(season) FROM player_motivation_events
        WHERE player_id = $1 AND event_type = $2
        """,
        player_id,
        event_type,
    )
    return value
