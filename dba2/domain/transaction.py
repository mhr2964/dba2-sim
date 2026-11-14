"""league_transactions, league_transaction_assets -- `txn_repo`'s tables
(migration 013).

docs/design/phase-5-discord-presentation.md §18 (Decision P5-31): a
general, extensible transaction/audit log, added because the trade is
the one fact this schema has never had a durable record of (§18.1's own
table: every other plausible "thing that needs tracking" already has a
table -- a phase advance is `phase_history`, a manager assignment is
`team_manager_history`, a motivation swing is `player_motivation_
events`). `TransactionType` is deliberately NOT the closed set a real
league will eventually need -- Decision P5-32 is explicit that a new
`txn_type` costs one vocabulary value and one writer, never a migration,
so this enum grows as real writers are built, not speculatively ahead of
them. Today it holds three members with real writers: `TRADE` (`gm_
service.apply_trade`, Decision P5-34/P5-74), `TIMELINE_SET` (`gm_
command_service.set_timeline`, Decision P5-68 -- the log's second
legitimate member, admissible under P5-32's own test because §18.1's
table has no existing history table for a team's timeline changes;
`teams.timeline`/`timeline_source` are current state with no history
anywhere else), and `DRAFT_PICK` (`gm_service.apply_human_draft_pick`,
Decision P5-98.e -- the third, and it passes the same P5-32 test:
`draft_selections` records WHAT was picked but has no actor column
anywhere, so "who made this pick" has no other home, and P5-48's own text
already asks for "a `league_transactions` row whose `actor_user_id` is the
commissioner". An AI pick writes no such row, by the same rule that keeps
`DecisionRecord` the AI's artifact: `apply_draft` is the AI's applier and
logs to the JSONL decision log, which has no actor field at all).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from dba2.domain.phase import Phase


class TransactionType(StrEnum):
    """Open vocabulary enforced in Python, not by a DB CHECK -- matching
    `player_motivation_events.event_type`/`media_stories.story_kind`'s
    identical shape, for the identical reason (P5-31's own text: a CHECK
    on the value set would make adding a transaction type a migration,
    which defeats the extensibility Decision P5-26 (Q6b) asked for)."""

    TRADE = "trade"
    TIMELINE_SET = "timeline_set"
    DRAFT_PICK = "draft_pick"
    # Stage E's commissioner game-status writes (docs/design/phase-5-stage-e-
    # advance.md P5-99.u, P5-99.u.1): a postponement is the one recoverable path out
    # of a game that cannot sim, and its restore is the reversal. Both pass P5-32's
    # test for the same reason `DRAFT_PICK` does -- `games.status` is current state
    # with no actor and no history anywhere else, so "who moved this game and why"
    # has no other home. `note` carries the commissioner's reason.
    GAME_POSTPONED = "game_postponed"
    GAME_RESTORED = "game_restored"
    # `/advance-unlock` (docs/design/phase-5-stage-e-advance.md P5-99.c): clearing a
    # press's claim by hand is the one act that can put two AI passes on a league at
    # once, so it leaves a durable "who broke whose claim, at what age" record. Passes
    # P5-32's test for the same reason -- `leagues.advance_claimed_*` is current state
    # with no history anywhere else.
    ADVANCE_UNLOCK = "advance_unlock"
    # `/gm auto` (GM-8, docs/design/gm-command-surface.md): `teams.auto_pilot`/
    # `teams.gm_philosophy` are current state with no history table anywhere
    # (the identical P5-32 reasoning `TIMELINE_SET` above already applies) --
    # Python-only, no migration, the `TIMELINE_SET` precedent §43's own
    # verification record names explicitly.
    AUTO_PILOT_SET = "auto_pilot_set"


class TransactionAssetKind(StrEnum):
    PLAYER = "player"
    PICK = "pick"


@dataclass(frozen=True)
class LeagueTransactionAsset:
    """One asset movement inside one transaction. `asset_kind` is fixed
    at insert and never changes; what each id column is *allowed* to
    hold and what it *must* hold at insert time are two separate
    guarantees, enforced two different ways (migration 013, amended by
    014, mirrors docs/design/phase-5-discord-presentation.md's own
    Decision P5-93):

    - **Wrong-column exclusion is DB-enforced.** `asset_kind = PLAYER`
      row's `draft_pick_id` is always `None`, and `asset_kind = PICK`
      row's `player_id` is always `None` -- migration 014's `CHECK`
      rejects the other shape outright, at every write, forever.
    - **Presence-correspondence at insert is Python-enforced**, in
      `txn_repo.add_transaction_asset`'s own `ValueError` guard, not by
      a DB CHECK -- a CHECK for "the matching id is non-`None`" would
      re-validate on every row state, including the one `ON DELETE SET
      NULL` produces when the referenced player or pick is later purged,
      and reject a state that is supposed to be legal.

    **After a purge, the matching id can be `None` even though it was
    supplied at insert.** `player_id`/`draft_pick_id` are both `ON
    DELETE SET NULL`, so a since-purged player leaves its own asset row
    with `asset_kind == PLAYER` and `player_id is None` -- both ids
    `None` on a `PLAYER` (or `PICK`) row is a legitimate post-purge
    state, not an invariant violation, proven directly by
    `tests/integration/test_txn_repo.py::
    test_asset_row_survives_its_players_own_purge_with_player_id_nulled`
    (and its pick-side twin). A reader that assumes a non-`None` id
    because `asset_kind` says so will be wrong the first time it reads a
    trade involving a purged player or pick."""

    id: int
    transaction_id: int
    asset_kind: TransactionAssetKind
    player_id: int | None
    draft_pick_id: int | None
    from_team_id: int | None
    to_team_id: int | None


@dataclass(frozen=True)
class LeagueTransaction:
    """One header row -- "what happened," never "what was asked" (that
    is `trade_offers`, a separate table by design, §19.2's own reasoning
    for keeping the two apart: a single table with a `status` column
    would put "this trade was proposed and rejected" next to "this trade
    happened," one forgotten `WHERE` away from announcing a trade that
    never occurred).

    `actor_user_id IS NULL` means the AI did it, unattended (mirrors
    `phase_history.actor_user_id`'s role, except nullable here because a
    trade executed by `run_trade_window` inside a phase hook has no
    human actor at all). `details` is a `Mapping`, never filtered on by
    any query -- type-specific payload only (P5-31's own fence)."""

    id: int
    league_id: int
    season: int
    phase: Phase
    txn_type: TransactionType
    actor_user_id: int | None
    source_offer_id: int | None
    note: str | None
    details: Mapping[str, object]
    created_at: datetime
