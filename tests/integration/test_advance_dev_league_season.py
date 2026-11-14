"""Live I/O proof for `tools/advance_dev_league_season.py`'s Phase 4
Stage C step 13a wiring (`docs/design/phase-4-media-and-player-agency.md`
Decision P4-67, §11 Stage C step 13a) and the real, previously-
undiscovered bug this stage's own build found and fixed while wiring it.

**The regression this file exists to guard, found by actually running
this tool twice in a row against a real dev league before landing, not
assumed.** `run_agency_checkpoint`'s own `load_gm_inputs` call requires a
REAL `league_cap_settings` row for `season` itself -- `seed_dev_league.py`
only ever writes one, for the league's creation season. A SECOND
`advance_dev_league_season` call (the first ever exercised in this
codebase's history against a real dev league, per this stage's own build
report) raised `NotFoundError` before the fix in `advance_dev_league_
season`'s own body that creates a real row for the season being left, the
moment one doesn't already exist.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""
from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import motivation_repo, player_repo, ratings_repo
from dba2.domain.phase import Phase
from dba2.managers.snapshot import build_roster_player
from dba2.services import agency_service
from tools import _gm_progression_stub as stub_module
from tools._gm_progression_stub import progress_one_season
from tools.advance_dev_league_season import (
    _build_real_player_context,
    _ratings_dict,
    advance_dev_league_season,
)
from tools.seed_dev_league import seed_dev_league


async def _seed_small_league(pool) -> int:
    return await seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Advance Dev League Test",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
        players_per_team=8,
    )


@pytest.mark.asyncio
async def test_seed_dev_league_bootstraps_a_real_motivation_row_per_player():
    """Decision P4-67 (Stage C step 13a): `seed_dev_league` now generates
    `player_motivation` for real -- this docstring's own prior claim
    ("Deliberately does NOT generate ... player_motivation") is stale on
    purpose, per that decision's own required same-commit docstring
    update."""
    pool = await get_pool()
    try:
        league_id = await _seed_small_league(pool)
        players = await player_repo.list_players_for_league(pool, league_id)
        assert players
        rows = await motivation_repo.list_player_motivation_for_league(pool, league_id)
        assert len(rows) == len(players)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_advance_dev_league_season_twice_in_a_row_does_not_raise():
    """The real regression: a second consecutive advance on the same
    league must succeed. **Before this stage's fix, this call raised
    `dba2.core.errors.NotFoundError: No league_cap_settings row for
    league <id>, season 2027`** -- critic nit 9 correction: 2027 is the
    season being LEFT on this second advance (the league starts at 2026,
    the first advance moves it to 2027), not `season + 1` as this
    docstring previously, incorrectly, said. `run_agency_checkpoint`'s
    own `load_gm_inputs` call requires a real `league_cap_settings` row
    for the season it is CALLED WITH -- the season being left -- and
    `seed_dev_league.py` only ever writes one, for the league's creation
    season (2026), so a second advance (which calls it with 2027) is the
    first call this row is ever missing for. `season + 1`'s own cap-
    settings row is handled separately, by `_cap_settings_for_season`'s
    projection fallback, and was never the missing row this regression
    was about -- matching `advance_dev_league_season`'s own source
    comment, which already said "the season being LEFT," not "the
    season after it," correctly."""
    pool = await get_pool()
    try:
        league_id = await _seed_small_league(pool)

        first_season, _aged1, _retired1 = await advance_dev_league_season(
            league_id=league_id, rng_seed=1
        )
        assert first_season == 2027

        second_season, _aged2, _retired2 = await advance_dev_league_season(
            league_id=league_id, rng_seed=1
        )
        assert second_season == 2028

        third_season, _aged3, _retired3 = await advance_dev_league_season(
            league_id=league_id, rng_seed=1
        )
        assert third_season == 2029
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_advance_dev_league_season_runs_a_real_agency_checkpoint():
    """Decision P4-67 (Stage C step 13a): each advance runs a real
    `agency_service.run_agency_checkpoint` before `_build_real_player_
    context` -- proven here by a real, non-trivial side effect
    (`player_motivation_events` rows actually written), not merely "the
    call did not raise" (the test above's own job)."""
    pool = await get_pool()
    try:
        league_id = await _seed_small_league(pool)
        players = await player_repo.list_players_for_league(pool, league_id)
        before = await motivation_repo.list_player_motivation_for_league(pool, league_id)

        await advance_dev_league_season(league_id=league_id, rng_seed=1)

        after = await motivation_repo.list_player_motivation_for_league(pool, league_id)
        assert len(after) == len(before)
        # The strongest, most direct proof a real checkpoint ran (not
        # "the season number matched," which the first-ever advance
        # cannot discriminate from the bootstrap's own initial value --
        # both read `current_season`): a real `player_motivation_events`
        # row exists for at least one player, written only by `run_
        # agency_checkpoint`'s own transaction.
        events = await motivation_repo.list_motivation_events_for_player(pool, players[0].id)
        assert len(events) > 0, "the checkpoint must write at least one real event row"
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_morale_trim_moves_only_real_player_deltas_on_the_real_dev_league_path():
    """Decision P4-50's own required targeted integration check (critic
    B2), run for real end-to-end -- same league, same seed, a real DB-
    backed `_build_real_player_context`, the real `run_agency_checkpoint`
    P4-67 requires to run first -- once with `MORALE_TRIM_SPAN`
    monkeypatched to `0.0` and once at its real value, asserting real-
    player rating deltas differ only for players whose morale reads non-
    neutral, and zero synthetic-player deltas differ at all.

    **This replaces, not merely supplements, the shipped unit-level
    control-run test's claim to BE this check.** `tests/unit/tools/
    test_gm_progression_stub.py::test_morale_trim_span_zero_is_a_real_
    control_run` proves the arithmetic off-switch on a single hand-built
    `RealPlayerContext` -- it never exercises `seed_dev_league`'s own
    `real_players_per_team` path at all. Critic finding: every test in
    this file before this one used `_seed_small_league`'s default
    `real_players_per_team=0`, so `_build_real_player_context` always
    returned `{}` and the morale trim never executed against a real
    dev-league player anywhere in this codebase's shipped tests.

    **Every real player's morale is forced to a known non-neutral value
    (alternating happy/miserable) via the real `motivation_repo.update_
    motivation_state` write (the same function `run_agency_checkpoint`
    itself calls), after the real checkpoint has already run.** Measured
    directly against this shared dev DB: `player_motivation`'s trait
    generator salts by the player's own auto-increment `player.id`
    (`agency_service.bootstrap_league_motivation` -> `generate_traits(
    league_rng_seed, player.id, ...)`), so which of a freshly-bootstrapped
    league's real players land non-neutral after one checkpoint pass
    depends on this shared DB's ambient id offset (how many players every
    other test run before this one has already inserted) -- confirmed by
    running this exact league/seed/checkpoint sequence standalone and
    getting 0 of 90 real players non-neutral on one run, verifying it is
    not this mechanism being broken. Forcing every real player away from
    neutral (rather than trusting the natural post-checkpoint
    distribution, or hand-picking one or two players who might not
    happen to have real trajectory data bracketing their current age --
    RPD-3's "not a hard cap" fallback) makes the "at least one real
    player actually moves" assertion below hold deterministically on
    every run, while every other step in the pipeline -- `_build_real_
    player_context`'s real DB reads, `progress_one_season`'s real math --
    stays exactly as real as before."""
    pool = await get_pool()
    try:
        league_id = await seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Morale Trim Integration Test",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
            players_per_team=15,
            real_players_season=2016,
            real_players_per_team=3,
        )

        # Decision P4-67's own required order: the checkpoint runs BEFORE
        # anything reads `player_motivation.morale` -- otherwise every
        # real player's morale reads the neutral bootstrap default and
        # this test could not discriminate anything (P4-67's own finding,
        # `advance_dev_league_season`'s own body mirrors this order).
        await agency_service.run_agency_checkpoint(
            pool, league_id=league_id, season=2026, phase=Phase.PROGRESSION
        )

        players = await player_repo.list_players_for_league(pool, league_id)
        real_player_ids = sorted(p.id for p in players if p.external_ref is not None)
        synthetic_player_ids = {p.id for p in players if p.external_ref is None}
        assert real_player_ids, "seeding must have produced at least one real player"
        assert synthetic_player_ids, "seeding must have produced at least one synthetic player"

        # Force EVERY real player away from neutral, alternating
        # happy/miserable, via the real production write path --
        # deterministic regardless of this shared DB's ambient id offset
        # or of any individual player's real-trajectory bracket coverage
        # (see docstring above).
        for i, player_id in enumerate(real_player_ids):
            forced_morale = 100 if i % 2 == 0 else -100
            current = await motivation_repo.get_player_motivation(pool, player_id)
            await motivation_repo.update_motivation_state(
                pool,
                player_id,
                satisfaction_winning=current.satisfaction_winning,
                satisfaction_role_usage=current.satisfaction_role_usage,
                satisfaction_loyalty=current.satisfaction_loyalty,
                satisfaction_money=current.satisfaction_money,
                satisfaction_legacy=current.satisfaction_legacy,
                morale=forced_morale,
                trust_in_management=current.trust_in_management,
                request_state=current.request_state,
                request_opened_in_season=current.request_opened_in_season,
                last_evaluated_season=current.last_evaluated_season,
                # Seeds morale ahead of a real PROGRESSION checkpoint: None, so
                # the checkpoint's per-player guard does not skip these players.
                last_checkpoint_phase=None,
            )

        all_ratings = await ratings_repo.get_for_league(pool, league_id)
        ratings_by_player = {r.player_id: r for r in all_ratings}
        roster_players = tuple(
            build_roster_player(p, ratings_by_player[p.id], contract=None) for p in players
        )
        # Rebuilt AFTER the forced writes above -- a real DB read of the
        # now-updated `player_motivation` rows, same as production.
        real_player_context = await _build_real_player_context(
            pool, league_id, 2026, players, ratings_by_player
        )
        assert all(
            real_player_context[pid].morale in (1.0, 0.0) for pid in real_player_ids
        ), "every real player's morale should now read strictly happy or miserable"

        # `progress_one_season` reads `stub_module.MORALE_TRIM_SPAN` off
        # the module each call (not a bound default), exactly like `tests/
        # unit/tools/test_gm_progression_stub.py`'s own monkeypatch proves
        # -- mutated and restored by hand here (rather than `monkeypatch`)
        # so the ordering between the two calls below is explicit and this
        # test has no fixture dependency.
        original_span = stub_module.MORALE_TRIM_SPAN
        try:
            stub_module.MORALE_TRIM_SPAN = 0.0
            control = progress_one_season(
                roster_players, season=2027, seed=1, real_player_context=real_player_context
            )
        finally:
            stub_module.MORALE_TRIM_SPAN = original_span
        assert stub_module.MORALE_TRIM_SPAN == original_span

        real = progress_one_season(
            roster_players, season=2027, seed=1, real_player_context=real_player_context
        )

        control_by_id = {rp.player.id: rp for rp in control}
        real_by_id = {rp.player.id: rp for rp in real}

        def _ratings_differ(pid: int) -> bool:
            return _ratings_dict(control_by_id[pid].ratings) != _ratings_dict(
                real_by_id[pid].ratings
            )

        differing_real = {pid for pid in real_player_ids if _ratings_differ(pid)}
        differing_synthetic = {pid for pid in synthetic_player_ids if _ratings_differ(pid)}

        assert not differing_synthetic, (
            "MORALE_TRIM_SPAN must not move a single synthetic player's rating -- "
            f"it moved {differing_synthetic}"
        )
        non_neutral_real = {
            pid
            for pid in real_player_ids
            if real_player_context[pid].morale is not None
            and real_player_context[pid].morale != 0.5
        }
        assert differing_real <= non_neutral_real, (
            "a real player moved despite reading neutral morale -- "
            f"unexpected movers: {differing_real - non_neutral_real}"
        )
        assert non_neutral_real == set(real_player_ids), (
            "every real player was forced away from neutral morale above -- "
            f"{set(real_player_ids) - non_neutral_real} unexpectedly read neutral"
        )
        assert differing_real, (
            "every real player has non-neutral morale by construction, so at least one "
            "must actually move if MORALE_TRIM_SPAN's real-trajectory branch works at all -- "
            "zero movers means the mechanism is not firing"
        )
    finally:
        await close_pool()
