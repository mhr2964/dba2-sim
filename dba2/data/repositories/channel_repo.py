"""league_channels, team_gm_channels -- `channel_repo`'s two tables
(migration 016; see `dba2/domain/channel.py` for the scope note).

Called directly from `dba2/bot/channels.py` rather than through a
services-layer wrapper -- this registry is bot-side bookkeeping ("which
Discord channel did I create for X"), not a business rule, the same
shape `tools/` scripts already call repos directly for. `bot/` has no
import restriction preventing it (only the inward `dba2.services` edge
and the outward `discord` edge are guarded, per `tests/unit/test_
layering.py`), and per Decision P5-14, the actual Discord API calls
(which must never happen inside a DB transaction) live one layer up in
`bot/channels.py`, never here.
"""
from __future__ import annotations

import asyncpg

from dba2.data.db import Executor
from dba2.domain.channel import ChannelPurpose, LeagueChannel, TeamGmChannel

_LEAGUE_CHANNEL_COLUMNS = "id, league_id, purpose, channel_id, message_id, created_at"
_TEAM_GM_CHANNEL_COLUMNS = "id, league_id, team_id, channel_id, archived_at, created_at"


def _map_league_channel(r: asyncpg.Record) -> LeagueChannel:
    return LeagueChannel(
        id=r["id"],
        league_id=r["league_id"],
        purpose=ChannelPurpose(r["purpose"]),
        channel_id=r["channel_id"],
        message_id=r["message_id"],
        created_at=r["created_at"],
    )


def _map_team_gm_channel(r: asyncpg.Record) -> TeamGmChannel:
    return TeamGmChannel(
        id=r["id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        channel_id=r["channel_id"],
        archived_at=r["archived_at"],
        created_at=r["created_at"],
    )


# ---------------------------------------------------------------------------
# league_channels
# ---------------------------------------------------------------------------


async def get_league_channel(
    ex: Executor, *, league_id: int, purpose: ChannelPurpose
) -> LeagueChannel | None:
    row = await ex.fetchrow(
        f"SELECT {_LEAGUE_CHANNEL_COLUMNS} FROM league_channels "
        "WHERE league_id = $1 AND purpose = $2",
        league_id,
        purpose.value,
    )
    return _map_league_channel(row) if row is not None else None


async def create_league_channel(
    ex: Executor, *, league_id: int, purpose: ChannelPurpose, channel_id: int
) -> LeagueChannel:
    row = await ex.fetchrow(
        f"""
        INSERT INTO league_channels (league_id, purpose, channel_id)
        VALUES ($1, $2, $3)
        RETURNING {_LEAGUE_CHANNEL_COLUMNS}
        """,
        league_id,
        purpose.value,
        channel_id,
    )
    return _map_league_channel(row)


async def set_league_channel_id(
    ex: Executor, *, league_id: int, purpose: ChannelPurpose, channel_id: int
) -> None:
    """Re-points an existing row at a freshly (re-)created channel -- the
    "manually-deleted channel" reconcile branch (§13.4's own named risk).
    A no-op if the row does not exist; the caller (`bot/channels.py`) is
    responsible for creating one first via `create_league_channel` when
    that is the case."""
    await ex.execute(
        "UPDATE league_channels SET channel_id = $3 WHERE league_id = $1 AND purpose = $2",
        league_id,
        purpose.value,
        channel_id,
    )


async def set_league_channel_message_id(
    ex: Executor, *, league_id: int, purpose: ChannelPurpose, message_id: int | None
) -> None:
    """The one write P5-12's own "message id on the standings row" clause
    asks for -- P5-37's generalized rule ("any Discord message this bot
    will ever edit again has its id in Postgres") applied here to the
    pinned/auto-updating standings message specifically."""
    await ex.execute(
        "UPDATE league_channels SET message_id = $3 WHERE league_id = $1 AND purpose = $2",
        league_id,
        purpose.value,
        message_id,
    )


# ---------------------------------------------------------------------------
# team_gm_channels
# ---------------------------------------------------------------------------


async def get_active_gm_channel(ex: Executor, team_id: int) -> TeamGmChannel | None:
    """The one row with `archived_at IS NULL`, if any -- see `domain/
    channel.py`'s docstring for why "at most one active row" is an
    application-level invariant, not a DB constraint."""
    row = await ex.fetchrow(
        f"SELECT {_TEAM_GM_CHANNEL_COLUMNS} FROM team_gm_channels "
        "WHERE team_id = $1 AND archived_at IS NULL",
        team_id,
    )
    return _map_team_gm_channel(row) if row is not None else None


async def get_most_recent_gm_channel(ex: Executor, team_id: int) -> TeamGmChannel | None:
    """The most recently created row regardless of archive state --
    reconcile's "reactivate an archived channel rather than create a
    second one" branch (P5-41: "archived ... recoverable if the same
    person comes back") reads this before deciding whether to create."""
    row = await ex.fetchrow(
        f"SELECT {_TEAM_GM_CHANNEL_COLUMNS} FROM team_gm_channels "
        "WHERE team_id = $1 ORDER BY created_at DESC LIMIT 1",
        team_id,
    )
    return _map_team_gm_channel(row) if row is not None else None


async def create_gm_channel(
    ex: Executor, *, league_id: int, team_id: int, channel_id: int
) -> TeamGmChannel:
    row = await ex.fetchrow(
        f"""
        INSERT INTO team_gm_channels (league_id, team_id, channel_id)
        VALUES ($1, $2, $3)
        RETURNING {_TEAM_GM_CHANNEL_COLUMNS}
        """,
        league_id,
        team_id,
        channel_id,
    )
    return _map_team_gm_channel(row)


async def reactivate_gm_channel(ex: Executor, *, channel_row_id: int, channel_id: int) -> None:
    """Clears `archived_at` and re-points at the (possibly re-created)
    channel id in one call -- the reconcile's reassignment-after-removal
    branch."""
    await ex.execute(
        "UPDATE team_gm_channels SET archived_at = NULL, channel_id = $2 WHERE id = $1",
        channel_row_id,
        channel_id,
    )


async def archive_gm_channel(ex: Executor, *, channel_row_id: int) -> None:
    """A no-op on a row that is already archived -- the `AND archived_at
    IS NULL` guard mirrors `team_repo.close_manager_assignment`'s
    identical reasoning: without it, a second call would silently
    overwrite the original archive timestamp."""
    await ex.execute(
        "UPDATE team_gm_channels SET archived_at = NOW() WHERE id = $1 AND archived_at IS NULL",
        channel_row_id,
    )
