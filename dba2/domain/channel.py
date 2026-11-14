"""league_channels, team_gm_channels -- migration 016, the channel
registry Decision P5-12 asks for ("no table anywhere stores a channel
id, a message id, a role id, or a thread id" -- §1 finding 4).

docs/design/phase-5-discord-presentation.md §6.1 (Decision P5-12), §6.2
(Decision P5-13/P5-14/P5-41). **Narrower than P5-12's own originally
proposed shape, disclosed here rather than silently shipped as if it
were the full thing** -- matching migration 012's and 013's own
precedent for the identical reason (ship exactly what has a real writer
today, widen later):

- P5-12 proposes four league-wide purposes (`wire`, `standings`,
  `transactions`, `games`). **Only `wire` and `standings` shipped in
  migration 016; `games` was added later (Phase 5 Stage D, broadcast
  design §1/§11 row 8) with no migration -- the column is `TEXT`, so
  widening the set was a Python-only change.** `transactions` still has
  no poster.
  Stage C's own scope (§11 step 11) names exactly these two ("the wire
  channel (P5-15) and the auto-updating standings message"); a
  `transactions` or `games` channel has no poster/reconciler built yet
  (§7.2's `/trades` history is served today by a read command,
  `view_service.get_league_transactions_feed`, not by a dedicated
  channel) and a row with no writer is the exact "table with no real
  writer" shape this project has already declined twice (P5-9's beat-log
  table, `trade_offer_assets`' original CHECK pair). Adding the other two
  purposes later costs one migration and zero schema redesign --
  `ChannelPurpose` is a closed, in-code enum specifically because
  `bot/channels.py`'s reconcile branches on it explicitly (unlike
  `txn_type`'s deliberately open vocabulary, there is no value in a
  purpose the reconciler has no branch for).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ChannelPurpose(StrEnum):
    """A closed set, not an open vocabulary like `txn_type`/`story_kind`
    -- `bot/channels.py`'s reconcile has one branch per member and gains
    nothing from a purpose it cannot act on. See module docstring for
    which of P5-12's originally-proposed four purposes ship now."""

    WIRE = "wire"
    STANDINGS = "standings"
    GAMES = "games"


@dataclass(frozen=True)
class LeagueChannel:
    """One row per `(league_id, purpose)` -- a league-wide Discord
    surface. `message_id` is only ever non-`NULL` for `STANDINGS` (P5-12:
    "a message-id column on the standings row specifically ... one
    message that gets edited forever, and its id must survive a bot
    restart"); `WIRE` posts one new message per story (P5-15) and never
    edits one, so its own `message_id` stays `NULL` forever."""

    id: int
    league_id: int
    purpose: ChannelPurpose
    channel_id: int
    message_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class TeamGmChannel:
    """One row per team, but not a unique row-per-team over the table's
    full history -- `archived_at IS NULL` marks the one currently-active
    row a team may have; an archived row is kept (never deleted, P5-41:
    "archive, never delete ... recoverable if the same person comes
    back") so a team can accumulate more than one row across its
    lifetime. `dba2/bot/channels.py`'s reconcile is what enforces "at
    most one active row per team" -- the same application-level
    discipline `team_repo.close_manager_assignment`'s own `WHERE
    removed_at IS NULL` guard uses for `team_manager_history`, chosen
    over a DB partial-unique-index for consistency with that existing
    precedent rather than introducing a second enforcement style for the
    identical shape of fact."""

    id: int
    league_id: int
    team_id: int
    channel_id: int
    archived_at: datetime | None
    created_at: datetime
