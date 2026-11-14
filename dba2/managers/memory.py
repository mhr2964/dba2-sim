"""`shade()` -- Decision P3-26's self-calibrating head-to-head statistic --
plus the other TWO of Decision P3-30's six `managers/`-scoped Phase 3
constants, `H2H_SHADE_PRIOR` and `H2H_DECAY` (docs/design/phase-3-inter-
manager-society.md §7, §8.1, §11 step 4). §11 step 11 adds `decay_head_to_
head`/`decay_head_to_head_rows`, Decision P3-25's season-boundary applier.

**`shade()` itself still reads only the structural `HeadToHeadLike`
protocol below, never the real `HeadToHead` dataclass** -- that was true
before `domain/head_to_head.py` existed (step 4, six steps before step
10 built it) and stays true now that it does: `shade()`'s contract has not
changed, and a hand-built Tier 1 test double still satisfies it. **The
decay applier is different**: Decision P3-25 says "every counter,"
including several this module's own `HeadToHeadLike` protocol does not
name, so it must read/return the real, full-width `HeadToHead` row --
there is no protocol narrow enough to express "every counter on the real
schema" without just being the schema. Importing `dba2.domain.head_to_
head` here is legal under `test_layering.py`'s rule (`managers` may not
import `dba2.data`; `dba2.domain` is unrestricted) and costs nothing at
runtime -- `domain/` is itself zero-I/O (`test_domain_layering.py`).

Zero I/O, per Decision P2-2(b)'s purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Imports only `dba2.domain.head_to_head` and stdlib.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Protocol

from dba2.domain.head_to_head import HeadToHead

# ---------------------------------------------------------------------------
# Decision P3-30's table -- the two managers/-scoped Phase 3 constants
# that belong next to shade(), the one function that reads them.
# ---------------------------------------------------------------------------

H2H_SHADE_PRIOR: float = 4.0
"""`shade()`'s shrinkage prior -- `n / (n + H2H_SHADE_PRIOR)` keeps
`|shade| <= 1` exactly and gives a smooth ramp rather than a kink as a
pair's decayed mass `n` grows from 0 (§8.1, Decision P3-26). At the
season-5 decayed mass `Σw = 14.50` (legal-only, HEAD-window measurement,
§7), the shrinkage factor is `14.50 / 18.50 = 0.784`."""

H2H_DECAY: float = 0.6
"""Exponential decay applied to every `gm_head_to_head` counter --
including `far_reject_streak` -- at each season boundary (Decision
P3-25). A single event's weight after 4 seasons is `0.6**4 = 0.1296`; the
half-life is `ln(0.5) / ln(0.6) ≈ 1.357` seasons. Applied by `decay_head_
to_head`/`decay_head_to_head_rows`, below (§11 step 11) -- this constant
was declared at step 4, three steps before either the schema (`domain/
head_to_head.py`, step 10) or a caller for it (this step) existed."""


class HeadToHeadLike(Protocol):
    """The two `gm_head_to_head` columns `shade()` reads (Decision
    P3-22's schema, §7) -- a structural protocol rather than an import of
    the real, not-yet-built `HeadToHead` dataclass. Any object exposing
    these two `float` attributes satisfies this protocol, including the
    eventual `domain.head_to_head.HeadToHead` row and any hand-built Tier
    1 test double."""

    opened: float
    """Decayed count of LEGAL negotiations `team_id` initiated against
    `opponent_id` (Decision P3-35 -- an illegal termination never
    increments this). `shade()`'s `n`."""

    accepts_by_opponent: float
    """Of `opened`, the decayed count the opponent's final-turn verdict
    accepted."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def shade(head_to_head: HeadToHeadLike | None, p_bar: float | None) -> float:
    """Decision P3-26's shade formula, verbatim:

    ```
    n     = head_to_head.opened                      (decayed)
    p_hat = head_to_head.accepts_by_opponent / n      (n > 0)
    z     = (p_hat - p_bar) / max(p_bar, 1 - p_bar)
    shade = clamp(z, -1, +1) * n / (n + H2H_SHADE_PRIOR)
    ```

    Reading: `shade > 0` means this opponent says yes to me more than the
    live market average -- I have been leaving value on the table.
    `shade < 0` means this opponent refuses me more than average -- I
    should sweeten or stop.

    **Zero is located twice over, both handled as the same `0.0` return
    (§6.1(b), §8.1):**

    - `head_to_head is None` -- no memory subsystem at all, or this pair
      has never negotiated (every pair's first-ever encounter, and every
      pair absent from a loaded mapping).
    - `p_bar is None` -- Decision P3-26's own live-population mean is
      undefined this window (no memory loaded, or the population's total
      `opened` across all rows is `0`). A `None` `p_bar` makes every
      shade-reading channel a strict no-op, by construction, rather than
      falling back to some pinned constant (Decision P3-26 is precisely
      the decision that there is no such constant -- `H2H_BASE_ACCEPT_R`
      was cut).
    - `head_to_head.opened <= 0` -- `n = 0`, this specific pair has no
      decayed mass yet. `p_hat` would be a `0/0` division; short-circuits
      to `0.0` before reaching it, matching `n / (n + H2H_SHADE_PRIOR)`'s
      own value at `n = 0` exactly (so "unknown opponent" and "there is
      no memory subsystem at all" are the same state by construction, per
      §6.1(b), not by coincidence).

    **The reachable range is asymmetric, and the asymmetry is real
    information, not an artifact needing correction (§8.1).** For any
    `p_bar` in `(0, 1)`: `z` reaches exactly `-1` at `p_hat = 0` when
    `p_bar >= 0.5` (and a smaller magnitude when `p_bar < 0.5`), and
    reaches its positive ceiling `(1 - p_bar) / max(p_bar, 1 - p_bar)` at
    `p_hat = 1`. At the measured real `p_bar ≈ 0.7709`, that ceiling is
    `≈ 0.2972` -- an opponent who accepts nothing is about 3.4x more
    distinguishable than one who accepts everything. The `clamp(z, -1,
    +1)` call is a defensive bound (algebraically, `|z| <= 1` already
    holds for any `p_hat, p_bar` both in `[0, 1]`, since the denominator
    IS `max(p_bar, 1 - p_bar)` -- the largest magnitude the numerator
    `p_hat - p_bar` can reach), kept because the design states it
    explicitly and because a `p_hat` computed from float division should
    never be trusted to land inside `[0, 1]` to the last bit."""
    if head_to_head is None or p_bar is None:
        return 0.0
    n = head_to_head.opened
    if n <= 0.0:
        return 0.0
    p_hat = head_to_head.accepts_by_opponent / n
    denom = max(p_bar, 1.0 - p_bar)
    z = (p_hat - p_bar) / denom
    z_clamped = _clamp(z, -1.0, 1.0)
    return z_clamped * n / (n + H2H_SHADE_PRIOR)


# ---------------------------------------------------------------------------
# Decision P3-25 -- the season-boundary decay applier, §11 step 11.
# ---------------------------------------------------------------------------

def decay_head_to_head(row: HeadToHead, *, season: int) -> HeadToHead:
    """Multiplies every COUNTER on `row` by `H2H_DECAY` -- Decision P3-25,
    verbatim: "every counter (including `far_reject_streak`) is multiplied
    by `H2H_DECAY` when the season rolls over." A single event's weight is
    `0.6` after one application, `0.6**4 = 0.1296` after four.

    **What is, and is not, a counter here -- derived mechanically, not
    hardcoded (round 13 correction, NIT 6).** Every field on `HeadToHead`
    NOT named in the local `non_decaying` set below is scaled, via a loop
    over `dataclasses.fields(HeadToHead)` rather than a hand-written list
    of 13 keyword assignments -- an earlier build hardcoded that list,
    which would silently skip any future column addition (no test would
    catch it, since the loop-free version simply never reads the new
    field). `non_decaying` is deliberately a FUNCTION-LOCAL name, not a
    module-level constant: `dba2/managers/` constants are tracked by
    `test_constant_inventory.py`'s mechanical Decision P3-30 scanner
    (anything committing a change bumps `GM_TUNING_VERSION`), and this
    name is pure internal bookkeeping for one function's own loop, never a
    tunable surface a `GmTuning` profile or a design-doc decision names --
    giving it module scope would force a version bump this round's fix has
    no real claim to. Identity (`league_id`/`team_id`/`opponent_id`, never
    touched) and season-bookkeeping ints (`last_season_interacted`, left
    as the caller's own record of the last real interaction; `updated_
    season`, restamped to the `season` argument below -- NIT 8's fix, see
    that parameter's own docstring) are the only fields excluded.
    `last_gap_by_opponent` decays too (it is measured on the same VP scale
    as `gap_by_opponent_sum` and the design names no exemption for it) but
    is `None` on a row that has never recorded an opponent merit-rejection
    -- handled generically below (a `None` value is never multiplied,
    whichever field it belongs to), not as a `last_gap_by_opponent`-
    specific special case.

    `season` -- **new parameter, round 13 correction, NIT 8.** The
    decay applier previously left `updated_season` untouched, so a row
    with no fresh write for 3 seasons kept a 3-seasons-stale `updated_
    season` even though its counters had just decayed 3 times -- the
    field's own name ("updated") became a lie the moment a row went a
    season without a write. `season` is the caller's own current season
    index AFTER rolling over (`tools/gm_harness/season.py`'s call site
    passes `state.season` right after `state.season = state.season + 1`,
    never the pre-rollover value), restamped onto every row this call
    decays, touched or not -- matching the design doc's own coverage-table
    requirement that the decay arm's test check `updated_season` "matches
    the harness's own season index," not merely "stayed constant."
    `last_season_interacted` is a different fact (the last season a real
    write happened) and is deliberately NOT touched by this restamp.

    **Idempotent under a replayed phase hook (round 13 correction, B2;
    §10 Tier 4's own requirement).** If `row.updated_season == season`
    already, this row has already been decayed to this exact season
    boundary -- returned UNCHANGED (no second multiplication) rather than
    decayed again. Without this guard, a crash-and-retry of the
    season-boundary step (the same `season` value, called twice against
    the same underlying rows) would compound: `H2H_DECAY` applied twice
    reads `0.36`, not `0.6`, silently double-decaying every counter. A
    season boundary is the right event to key this guard on -- unlike
    "was this window's negotiation write already applied" (genuinely
    ambiguous from `run_trade_window`'s own arguments alone, per that
    function's own docstring), a season boundary is unambiguous to detect
    *given the actual call ordering this guard relies on*: the harness
    (`tools/gm_harness/season.py`) always advances `state.season` (the
    `+1` rollover) before calling this decay pass, so by the time it runs,
    no negotiation write this season can have stamped `updated_season` to
    the new value yet. **This is not true in general** — `_record_
    negotiation_in_h2h` stamps `updated_season = ctx.season` on every
    write, so a caller that decayed into season N *after* a season-N
    write (rather than strictly before it, as the harness does) would see
    `row.updated_season == season` already and silently skip the decay.
    The real precondition this guard needs is "`season` (the decay call's
    argument) must exceed every `updated_season` the write path could
    have stamped so far" — true for the harness's own call site today,
    and something a future production `PRESEASON`-hook caller must
    preserve rather than assume (critic re-review, round 15).

    Pure and total: never mutates `row`, always returns a new `HeadToHead`
    (`dataclasses.replace`) or `row` itself unchanged on the idempotency
    short-circuit above, matching `shade()`'s own zero-I/O contract."""
    if row.updated_season == season:
        return row
    non_decaying = {
        "league_id", "team_id", "opponent_id", "last_season_interacted", "updated_season",
    }
    decayed: dict[str, object] = {"updated_season": season}
    for f in dataclasses.fields(HeadToHead):
        if f.name in non_decaying:
            continue
        value = getattr(row, f.name)
        decayed[f.name] = value * H2H_DECAY if value is not None else None
    return dataclasses.replace(row, **decayed)


def target_count_for_pair(head_to_head: HeadToHead | None, futility_streak: float) -> int:
    """Decision P3-40's ladder -- channel 1's one real decision site
    (§8.4/§9.2, §11 step 17): the per-ordered-pair count of targets
    `gm_service.propose_packages` ranks and takes from `ranked_b`. Ordered
    and total, first match wins:

    1. **`down`** -- `head_to_head is not None and head_to_head.far_
       reject_streak >= futility_streak` -> **`1`** target.
    2. **`up`** (only reached if `down` did not fire) -- `head_to_head is
       not None and head_to_head.executions >= 1.0` -> **`3`** targets.
    3. **default** -- `None` memory, or neither predicate held ->
       **`2`** targets (`gm_service.TRADE_TARGETS_PER_PARTNER`'s own
       pre-step-17 value, the ladder's fallback rung, never imported
       here).

    **`down` is checked first and wins the overlap.** §8.4's worked
    sequence constructs a real site where `executions >= 1` and `far_
    reject_streak >= futility_streak` hold simultaneously (`far_reject_
    streak` resets only on an opponent accept/near-reject and decays only
    at the season boundary; `executions` decays at the same boundary and
    never resets -- nothing in the schema separates them), so the two
    predicates are NOT disjoint and the order is a real behavioural
    choice, not a formality. This is why this function has five, not
    four, Tier-1 test cases (`tests/unit/managers/test_memory.py`) --
    round 6's four cases each set exactly one predicate, so two builders
    could ship opposite precedence and both pass; the fifth (both
    predicates true) is what makes the ladder a test rather than a
    paragraph.

    **`futility_streak` is a PLAIN parameter, never imported.** The real
    threshold (`gm_service.FUTILITY_STREAK = 4.0`) is a SERVICE-scoped
    constant (Decision P3-40 places it beside `TRADE_TARGETS_PER_
    PARTNER`, the other constant it gates, not under `managers/`) --
    this function stays agnostic to where its own threshold argument
    comes from, matching this module's own zero-I/O, no-cross-layer-
    import purity contract. Reads `HeadToHead` structurally (duck-typed
    at the call site; a hand-built double satisfying the two attributes
    below works identically in a Tier-1 test), imported here only for the
    type hint -- exactly `decay_head_to_head`'s own precedent above, not
    `shade()`'s narrower `HeadToHeadLike` protocol (adding a new `managers/`
    -scoped `Protocol`/dataclass here would itself be a new tracked
    declaration under Decision P3-30's mechanical inventory guard, which
    this step's own instruction is to leave untouched)."""
    if head_to_head is not None and head_to_head.far_reject_streak >= futility_streak:
        return 1
    if head_to_head is not None and head_to_head.executions >= 1.0:
        return 3
    return 2


def decay_head_to_head_rows[K](rows: Mapping[K, HeadToHead], *, season: int) -> dict[K, HeadToHead]:
    """`decay_head_to_head`, applied over a whole `{key: HeadToHead}`
    mapping -- the shape both call sites §7 names actually hold (`tools/
    gm_harness/season.py`'s own `HarnessState.head_to_head`, keyed
    `(team_id, opponent_id)`; a production `PRESEASON`-hook applier would
    read/write the same shape after `head_to_head_repo.list_head_to_head_
    for_league`). **No production caller exists yet** -- there is no
    production wiring of `GmContext.head_to_head` outside the harness as
    of this step, so this function has exactly one real caller today
    (`tools/gm_harness/season.py`'s season-boundary call) and is exposed
    here, ready, rather than left unbuilt until a production caller shows
    up (§11 step 11's own instruction: "build the pure decay function so
    it's ready, but don't invent a fake production call site"). `season`
    is forwarded to every row's own `decay_head_to_head` call unchanged --
    one season boundary restamps every row to the same new season index."""
    return {key: decay_head_to_head(row, season=season) for key, row in rows.items()}
