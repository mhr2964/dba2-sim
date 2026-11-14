"""Stage D step 22 (docs/design/phase-4-media-and-player-agency.md §37,
Decision P4-113 item 2) -- the three `tools/`-only functions
`tools/generate_media_corpus.py`'s dev-league driver calls, at the DRAFT
cycle, to conduct one real draft per simulated season and roster-place
every selectee: `ensure_draft_class`, `ensure_draft`, `place_selections`.

**Option C (design doc §37.2, ruled).** Draft SELECTION itself is not
here -- that is `dba2.services.gm_service.execute_draft`, extracted
verbatim from `run_phase_hook`'s own `Phase.DRAFT` branch (Decision
P4-113 item 1) and reused directly, never hand-reimplemented. This module
only builds the two real-DB prerequisites that function needs (a
persisted prospect pool, a persisted `Draft`/`DraftPick` order) and the
one real-DB consequence its own docstring says it deliberately skips
(roster placement, §37.1 item 4).

**Quarantine.** This file imports `tools._gm_draft_class_stub`
(§37.1 item 5: its quarantine test, `tests/unit/test_layering.py::
test_nothing_under_dba2_imports_the_gm_draft_class_stub`, forbids only
`dba2/` from importing it -- `tools/` is not `dba2/`, so this caller is
permitted). Nothing under `dba2/` may import THIS module either, for the
identical reason (a corpus-generator driver has no business being
reachable from the pure/service layers) -- not separately enforced by a
new test, since `tools/`-importing-`dba2/` is already the only legal
direction (Decision 4's own rule, `test_layering.py`'s existing
"`dba2/` never imports `tools/`" checks already cover this file too, with
no allowlist widened).

**Idempotence, layered on real DB facts, not an in-memory flag (Decision
P4-114's own "Idempotence and the re-run question" paragraph, corrected
in place by Decision P4-120 after a real gap: a kill mid-`ensure_draft_
class` used to leave a permanently-short class, since the original guard
only checked "any rows exist," not "enough rows exist").**
`ensure_draft_class` is count-aware, not presence-aware: it tops up a
`(league_id, class_year)` row set that has fewer than `class_size`
entries (reusing the same deterministic `generate_draft_class` draw and
skipping already-persisted `mock_rank`s, so a resumed run neither
shrinks nor duplicates the class) and no-ops only once the count is
satisfied. `ensure_draft` no-ops on an existing `drafts` row for
`season`. `place_selections` writes inside one real transaction per
call (Decision P4-120 item 1-2) and is safe to call unconditionally
every DRAFT cycle -- re-placing an already-placed selection is a cheap
idempotent write, not a skip. Neither function reads or writes a
checkpoint file of its own -- `generate_media_corpus.py`'s own
`_CheckpointState`/`_ArmAccumulator` gain no new pickled field for this
step (Decision P4-114's containment list); every fact this module needs
to resume correctly already lives in `draft_classes`/`drafts`/
`draft_picks`/`players` themselves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields as dc_fields
from datetime import UTC, datetime

from dba2.core.rng import rng_for
from dba2.data.db import Executor, transaction
from dba2.data.repositories import draft_repo, player_repo, ratings_repo
from dba2.domain.draft import DraftSelection
from dba2.domain.player import RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.media.pulse import PulseSnapshot
from dba2.services import ratings_service
from tools._gm_draft_class_stub import generate_draft_class

# `PlayerRatings`' own meta fields (§4.3.2) -- everything else on the
# dataclass is one of the 55 rating columns `ratings_repo.upsert_player_
# ratings`'s `ratings: dict[str, int]` wants. Mirrors `tools/seed_dev_
# league.py::_create_synthetic_player`'s own tendencies-dict construction
# (`{f.name: getattr(...) for f in dc_fields(...) if f.name not in
# {...}}`), applied here to ratings instead, rather than reaching into
# `ratings_repo`'s own private `_RATING_COLUMN_NAMES` constant.
_RATINGS_META_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


async def ensure_draft_class(
    pool: Executor, *, league_id: int, season: int, class_size: int, rng_seed: int
) -> None:
    """Decision P4-113 item 2, bullet 1; completeness check hardened by
    Decision P4-120 (round 25). Returns early only once `draft_classes`
    already has `>= class_size` REAL rows for `(league_id, class_year=
    season)` -- a real, durable idempotence ledger, but the original guard
    (`if existing: return`) treated any non-empty class as done, which is
    wrong for the exact case this ledger exists to protect: `ensure_draft_
    class`'s own per-prospect loop below commits one `transaction` per
    prospect (not one for the whole class), so a kill partway through
    leaves a real, non-empty, PERMANENTLY-SHORT class -- the old guard's
    `list_draft_class(...)` call would see it non-empty and never top it
    up, silently running that season's draft short forever (round-25
    critic finding, blocking issue 3's second variant).

    **Fixed by making the ledger count-aware, not just presence-aware, and
    topping up rather than accepting a partial class as done.** `class_
    seed` is derived the same deterministic way every call
    (`rng_for(rng_seed, "dev_league_draft", "class", season)`), so a
    second call regenerates the IDENTICAL prospect sequence (same `seed`,
    same `class_size`, `tools._gm_draft_class_stub.generate_draft_class`'s
    own documented determinism) -- the resume path re-derives the same
    fabricated `mock_rank` for every prospect and skips any `mock_rank`
    already persisted, so a resumed run writes only the missing prospects,
    never a duplicate or a second, differently-seeded class.

    Otherwise draws `class_size` prospects from `tools._gm_draft_class_
    stub.generate_draft_class` and persists each one through the same
    four-call pipeline `seed_dev_league._create_synthetic_player` already
    uses -- `player_repo.create_player`, `ratings_repo.upsert_player_
    ratings`, `ratings_service.recompute_overall`, `ratings_service.
    derive_tendencies_for_player` -- inside one `transaction` per prospect
    (the same B3 atomicity shape that pipeline already carries: a mid-loop
    failure rolls back the whole prospect, never leaves one stuck at the
    `overall_cached=1` placeholder), plus one `draft_repo.create_draft_
    class_entry` in the same transaction so a prospect and its class-entry
    row can never diverge under a partial failure.

    The stub's own fabricated `1..class_size` `player.id` values are used
    ONLY as `mock_rank` (§37.1 item 5) -- every persisted `players` row
    gets a real, DB-assigned serial id, never the stub's fabricated one.
    `derive_tendencies_for_player` runs here, at creation, not deferred to
    `place_selections` below -- a prospect who is never drafted then never
    needs a second pass, and a drafted one is ACTIVE-ready with no extra
    step (Decision P4-113 item 2's own stated reason)."""
    existing = await draft_repo.list_draft_class(pool, league_id, season)
    existing_ranks = {row.mock_rank for row in existing if row.mock_rank is not None}
    if len(existing_ranks) >= class_size:
        return

    class_seed = rng_for(rng_seed, "dev_league_draft", "class", season).getrandbits(63)
    prospects = generate_draft_class(
        season=season, seed=class_seed, league_id=league_id, class_size=class_size
    )

    for prospect in prospects:
        mock_rank = prospect.player.id  # the stub's own fabricated 1..class_size id
        if mock_rank in existing_ranks:
            continue  # already persisted by an earlier, since-killed call -- top up, not redo
        async with transaction(pool) as conn:
            player = await player_repo.create_player(
                conn,
                league_id=league_id,
                first_name=prospect.player.first_name,
                last_name=prospect.player.last_name,
                primary_position=prospect.player.primary_position,
                secondary_position=prospect.player.secondary_position,
                height_in=prospect.player.height_in,
                weight_lb=prospect.player.weight_lb,
                wingspan_in=prospect.player.wingspan_in,
                birth_date=prospect.player.birth_date,
                draft_year=season,
                years_pro=0,
                team_id=None,
                roster_status=RosterStatus.DRAFT_ELIGIBLE,
                # `overall_cached` is NOT NULL, but a player must exist
                # before `player_ratings.player_id` (its FK) can be
                # inserted -- `1` here is a throwaway placeholder,
                # overwritten below by `recompute_overall`, before this
                # transaction commits. Same convention `_create_synthetic_
                # player` already uses.
                overall_cached=1,
                overall_computed_at=datetime.now(UTC),
            )

            ratings_dict = {
                f.name: getattr(prospect.ratings, f.name)
                for f in dc_fields(prospect.ratings)
                if f.name not in _RATINGS_META_FIELDS
            }
            await ratings_repo.upsert_player_ratings(
                conn,
                player_id=player.id,
                league_id=league_id,
                source=RatingSource.GENERATED,
                ratings=ratings_dict,
            )

            await ratings_service.recompute_overall(conn, player.id)

            tendencies_rng = rng_for(
                rng_seed, "dev_league_draft", "tendencies", season, mock_rank
            )
            await ratings_service.derive_tendencies_for_player(
                conn, player.id, rng=tendencies_rng
            )

            await draft_repo.create_draft_class_entry(
                conn,
                league_id=league_id,
                class_year=season,
                player_id=player.id,
                mock_rank=mock_rank,
                is_generated=True,
                source="_gm_draft_class_stub",
            )


async def ensure_draft(
    pool: Executor, *, league_id: int, season: int, snapshot: PulseSnapshot, rounds: int
) -> None:
    """Decision P4-113 item 2, bullet 2. Returns early if a `drafts` row
    already exists for `(league_id, season)` -- the real idempotence
    ledger for this half of the step. Otherwise creates one `Draft`
    (`total_picks = rounds * len(snapshot.teams)`, §37.1 item 6: globally
    unique `1..total_picks` pick numbers, never restarted per round) and
    one `DraftPick` per slot.

    **No lottery (§37.1 item 2, §37.2 Option C).** Pick order within each
    round is reverse standings -- worst team first -- read directly off
    `snapshot` (`MediaPulseCycleResult.snapshot`, the DRAFT cycle's own
    already-returned `PulseSnapshot`, zero extra I/O), sorted descending
    by `TeamPulseState.standings_position` (1-based, best-first, so
    descending is worst-first) with `team_id` ascending as the tiebreak.
    The same order is reused for every round -- nothing in the design doc
    calls for a snake draft, and inventing one would be exactly the kind
    of unspecified mechanic this step is scoped to avoid."""
    existing = await draft_repo.get_draft_for_season(pool, league_id, season)
    if existing is not None:
        return

    team_ids = sorted(
        snapshot.teams.keys(),
        key=lambda team_id: (-snapshot.teams[team_id].standings_position, team_id),
    )
    total_picks = rounds * len(team_ids)

    async with transaction(pool) as conn:
        # `drafts` deliberately has no `status` column (`draft_repo.py`'s
        # own module docstring) and `draft_picks` carries no `draft_id`
        # FK either -- both are keyed by `(league_id, season)` instead, so
        # this row's own id is never needed below.
        await draft_repo.create_draft(
            conn, league_id=league_id, season=season, total_picks=total_picks
        )
        pick_number = 1
        for round_number in range(1, rounds + 1):
            for team_id in team_ids:
                await draft_repo.create_draft_pick(
                    conn,
                    league_id=league_id,
                    season=season,
                    round=round_number,
                    original_team_id=team_id,
                    current_team_id=team_id,
                    pick_number=pick_number,
                )
                pick_number += 1


async def place_selections(
    pool: Executor,
    *,
    league_id: int,
    season: int,
    selections: Sequence[DraftSelection],
) -> None:
    """Decision P4-113 item 2, bullet 3 -- §37.1 item 4's real answer;
    made all-or-nothing AND resume-safe by Decision P4-120 (round 25). For
    each real, PERSISTED `draft_selections` row, moves the drafted
    prospect onto his new team via `player_repo.set_team_and_status(conn,
    player_id, team_id, RosterStatus.ACTIVE)`. No contract, no `roster_
    service.sign`, no cap write -- draft execution places a player on a
    roster; it does not sign him (nothing in the games/stats path this
    step needs reads a contract, and this dev-league path has had none
    since Decision P4-63).

    **Two real, disclosed resume gaps, found by a round-25 critic pass
    reading the real call sites rather than trusting P4-114's own original
    "mid-`execute_draft`" framing (which was WRONG -- `gm_service.execute_
    draft`'s own per-pick loop is already wrapped in one real `transaction
    ()` block, `gm_service.py` around its own `apply_draft` calls, so a
    crash there was never actually unprotected):**

    1. **All placements in one call now run inside ONE `transaction`**,
       matching `execute_draft`'s own atomicity shape. The original
       version looped bare autocommits against `pool` directly (60
       independent commits for a 2-round class), so a kill mid-loop left
       a real, persisted, partially-placed class -- fixed here.
    2. **The caller must not gate this call on `execute_draft`'s own
       return value, and no longer does (see `generate_media_corpus.py`'s
       own DRAFT-cycle call site).** `gm_service._eligible_draft_picks_
       sorted` filters out every pick whose `used_for_player_id` is
       already set (its own B2 idempotence fix), so a resumed `execute_
       draft` call for a season whose draft already fully executed
       correctly returns `None` -- but placement is a SEPARATE, later
       write, and a kill between `execute_draft`'s own commit and this
       function completing would otherwise strand that season's rookies
       as drafted-but-never-placed FOREVER, with an undisclosed knock-on:
       `gm_service._load_draft_prospects` filters only on `roster_status
       == DRAFT_ELIGIBLE` (no `class_year`, no "already drafted" check),
       so a stranded prospect -- still `DRAFT_ELIGIBLE` because placement
       never ran -- would be re-offered to, and re-selectable in, every
       LATER season's draft even though a real `draft_selections` row
       already claims him. The fix: this function now takes the season's
       real, persisted `draft_selections` rows directly (`DraftSelection`,
       read via `draft_repo.list_draft_selections_for_draft`), not
       `execute_draft`'s own return value, and the caller re-derives and
       re-calls this function from the database every DRAFT cycle
       regardless of whether `execute_draft` drafted anything new THIS
       call -- idempotent by construction (below), so re-placing an
       already-placed selection is a cheap no-op, and a stranded one from
       an earlier, killed call gets placed for real on the next attempt.

    Idempotent by construction -- a setter to a fixed target, safe to call
    twice (or on a selection some earlier call already placed), and safe
    to retry the whole batch after a rollback for the same reason.
    `league_id`/`season` are accepted for call-site symmetry with `ensure_
    draft_class`/`ensure_draft` (and for whichever later caller wants to
    scope/log by them) but are not read by this function's own body --
    every fact it needs is already on `selections` itself."""
    del league_id, season  # accepted for call-site symmetry only, see docstring
    async with transaction(pool) as conn:
        for selection in selections:
            await player_repo.set_team_and_status(
                conn, selection.player_id, selection.team_id, RosterStatus.ACTIVE
            )
