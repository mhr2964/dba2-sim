"""gm_head_to_head — `head_to_head_repo`'s one table (Decision P2-5's rule:
one dataclass, one table).

phase-3-inter-manager-society.md §7 (Decision P3-22, P3-35, P3-42).

Directed, not symmetric: one row per ordered `(league_id, team_id,
opponent_id)` pair, `team_id != opponent_id` — "I keep rejecting him" and
"he keeps rejecting me" are different facts (§7). 30 teams -> 870 ordered
pairs per league.

Every counter is `float`, not `int`, for two independent reasons stated in
§7: (1) `double precision` is what keeps asyncpg from handing back a
`Decimal` the way `numeric` would (the repeated bug class named in
HANDOFF.md), and (2) the counters decay at the season boundary (`H2H_DECAY`,
Decision P3-25), so a decayed count is genuinely fractional, not merely
represented as one.

`refused_illegal`, and every other counter on this row, is written by
`services/gm_service.py::_record_negotiation_in_h2h` (§11 step 11) — this
module itself stays persistence-only, no step-10-era "nothing writes this
yet" caveat survives that step."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeadToHead:
    """One row: `team_id`'s running memory of negotiations with
    `opponent_id`, within `league_id`. See phase-3-inter-manager-society.md
    §7 for what each counter means and why it exists; the short version:

    - `opened`/`received` are negotiation counts, LEGAL only (P3-35) —
      `opened` is `team_id`-initiated, `received` is opponent-initiated.
    - `accepts_by_opponent`/`rejects_by_opponent` partition `opened`;
      `accepts_by_self`/`rejects_by_self` partition `received`.
    - `counters_given`/`counters_received` count `COUNTER`-*stance* turns,
      not counters actually emitted — `negotiation.respond()` can return
      `COUNTER` and `build_counter` then fail to find a legal repair, or
      the turn can stall, in which case no counter is ever produced.
      Measured on a real seed: ~76% of these increments are from turns
      that reached this stance but emitted nothing (critic re-review,
      round 15) — the design doc's own trigger wording ("a negotiation
      reaches a `COUNTER` stance," §10 Tier 2) matches this code, so the
      gap is in this docstring's prior claim, not the implementation.
      Independent of the accept/reject partitions above.
    - `executions` counts only trades that executed out of a negotiation
      `team_id` OPENED (Decision P3-42) — never the opponent-initiated
      population, which is why it is not derivable from `trades_completed`.
    - `refused_illegal` is reporting-only (Decision P3-35): a negotiation
      the legality gate refused carries zero information about the
      opponent's disposition, so it feeds no other statistic here.
    - `far_reject_streak` stores channel 1's predicate directly as a
      scalar — incremented on an `opened` negotiation ending in an
      opponent merit reject with `gap < -counter_band`, reset to 0 on any
      opponent accept or near-reject, untouched by an illegal termination.
      It reads the gap, never the terminal situation token (§7, round 12).
    - `gap_by_opponent_sum`/`last_gap_by_opponent` are measured in the
      opponent's own value-point scale, over opponent rejections only.
    - `last_season_interacted`/`updated_season` are bookkeeping for the
      season-boundary decay pass (Decision P3-25), not statistics."""

    league_id: int
    team_id: int
    opponent_id: int

    opened: float
    received: float
    accepts_by_opponent: float
    rejects_by_opponent: float
    accepts_by_self: float
    rejects_by_self: float
    counters_given: float
    counters_received: float
    executions: float
    refused_illegal: float
    far_reject_streak: float
    gap_by_opponent_sum: float
    last_gap_by_opponent: float | None
    last_season_interacted: int | None
    updated_season: int
