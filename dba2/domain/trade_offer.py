"""trade_offers -- `offer_repo`'s one table (migration 012, a minimal
slice of the design doc's own `trade_offers`/`trade_offer_assets` pair,
Decision P5-35, scoped down per an explicit coordinator ruling to exactly
what A-23's own proof obligation needs: an AI-initiated offer to a
human-managed team must be a real, persisted, restart-safe row, never an
auto-resolved trade).

docs/design/phase-5-discord-presentation.md §19.2 (Decision P5-35), §28.4
(Decision P5-59). **Deliberately smaller than P5-35's own schema, and the
omissions are named rather than silently dropped:**

- No `counters_offer_id`. Nothing in this codebase yet resolves an offer
  -- that is Stage D'-24's `offer_service.respond`, a separate, unbuilt
  commit. A root offer's chain depth is always 0 on this path (P5-35's own
  "an AI-initiated root is round_index = 0" reading), so a column with no
  writer would sit at its default forever until that commit exists to
  give it one.
- No `channel_id`/`message_id`. Discord delivery is Stage C/`dba2/bot/`,
  which does not exist in this codebase yet (verified: no `dba2/bot/`
  directory).
- No `illegal_token`/`resolved_at`. Nothing here expires or resolves a
  row -- `status` is always written `PROPOSED` by this commit's one
  writer (`run_phase_hook`'s trade branch).

**Update (migration 018, docs/design/phase-5-trade-offer-review-surface.md
§2): `counters_offer_id`, `resolved_at`, `resolved_by`, `illegal_token`,
`channel_id` and `message_id` now exist -- each was added by the commit that
built its writer (`services/offer_service.py`, `data/repositories/
offer_repo.py`), exactly as the paragraph below said would happen.** They
are the trailing, defaulted fields on `TradeOffer`, so 012's one writer
(`run_phase_hook`'s trade branch) keeps compiling unchanged.

Every one of those is a real, additive migration for whichever future
commit builds that column's own writer -- this table carries exactly the
columns `run_phase_hook`'s trade branch (P5-59) writes today and nothing
more, the same "ship the inert half first" convention this design doc
uses everywhere else (P5-44's empty `self_managed_team_ids` frozenset,
P5-67's `timeline_source` default), applied at the SCHEMA granularity
here instead of the field granularity.

**Asset references are a single `JSONB` column, not a second
`trade_offer_assets` table -- and `package` stays ONE dataclass field, a
raw `Mapping`, never unpacked into four separate tuple fields.** P5-35's
own text says the assets table would share `league_transaction_assets`'
column shape, but `league_transactions`/`league_transaction_assets` do
not exist in this codebase either (Stage B's transaction-log migration,
P5-31, is separate, unbuilt work) -- there is no existing relational
convention here to mirror. A normalized second table with its own
`asset_kind`/CHECK-correspondence machinery would be schema surface this
commit's own tests cannot meaningfully exercise: nothing here queries an
individual asset row, only ever the whole package as one unit (this
commit's own proof test, and eventually `bot/`'s rehydration). `JSONB`
with no custom pool type codec, marshalled to/from `dict` at the repo
boundary, is the same disclosed convention `media_repo.py`'s `slot_
bindings` and `league_repo.py`'s `phase_data` columns already use --
**and, load-bearingly, `MediaStory.slot_bindings` is that same convention's
own precedent for keeping the JSONB column as ONE dataclass field**:
`tests/integration/test_schema_drift.py`'s `test_dataclass_fields_
exactly_match_table_columns` asserts every repository dataclass's field
NAME set exactly equals its table's column NAME set, no extra, no
missing -- a dataclass that unpacked `package` into four separate tuple
fields would have four field names with no matching column and would
fail that test outright, not merely read oddly.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from dba2.domain.phase import Phase, TradeStatus


@dataclass(frozen=True)
class TradeOffer:
    """One `trade_offers` row -- always expressed from the PROPOSER's own
    perspective (`from_team_id` is always the team that made the offer),
    mirroring `gm_service.CandidatePackage`'s own `team_a_id`-is-always-
    the-proposer convention. `proposed_by IS NULL` means AI-initiated
    (P5-35) -- the only shape this commit's own writer (`run_phase_hook`'s
    trade branch, P5-59) ever produces; a human-initiated row is Stage
    C's `propose_trade` command, not built here."""

    id: int
    league_id: int
    season: int
    phase: Phase
    from_team_id: int
    to_team_id: int
    proposed_by: int | None
    status: TradeStatus
    package: Mapping[str, tuple[int, ...]]
    """The proposer's own package, JSON-shaped: `players_from_to`/
    `players_to_from`/`picks_from_to`/`picks_to_from`, each a tuple of
    ids -- mirroring `gm_service.TradeExecution.players_a_to_b`/`players_
    b_to_a`/`picks_a_to_b`/`picks_b_to_a`'s identical shape one level up.
    A raw mapping, not four dataclass fields (see module docstring for
    why the JSONB column stays one dataclass field)."""
    created_at: datetime
    counters_offer_id: int | None = None
    """The offer this one counters (P5-36: a counter is a NEW row, never a
    mutated one). `None` for a root offer, which is every AI-initiated
    offer and every `propose` offer."""
    resolved_at: datetime | None = None
    """When `status` left `PROPOSED`. Written by every status CAS and by
    both bulk expiry writers; `None` while still `PROPOSED`."""
    resolved_by: int | None = None
    """Discord user id that accepted/rejected/countered/vetoed. `None` on a
    still-`PROPOSED` row AND on a row a rule resolved (an expiry, an AI
    refusal) -- nobody clicked."""
    illegal_token: str | None = None
    """The `"illegal:<rule>"` token an accept-time legality re-check failed
    on, when that is why the row went `EXPIRED`."""
    channel_id: int | None = None
    """The GM-channel message this offer was delivered to. Written by the
    delivery drain only."""
    message_id: int | None = None
    """`status = 'proposed' AND message_id IS NULL` is the undelivered
    queue (P5-60)."""
