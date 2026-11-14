"""Live I/O proof for `services/league_service.py::create_league` -- §8 step
8's first verification target: "seeds 30 teams from `seeds/`, writes cap
settings, sets phase `SETUP`."

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, offer_repo, team_repo
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.team import GmPhilosophy
from dba2.phase.permissions import PermissionDenied
from dba2.services import league_service


def _guild_id() -> int:
    return random.randint(10_000_000, 2_000_000_000)


@pytest.mark.asyncio
async def test_create_league_seeds_exactly_30_teams_with_correct_structure():
    pool = await get_pool()
    try:
        league = await league_service.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="League Service Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=42,
        )
        try:
            assert league.current_phase == Phase.SETUP

            teams = await team_repo.list_teams_for_league(pool, league.id)
            assert len(teams) == 30

            codes = [t.code for t in teams]
            assert len(set(codes)) == 30, "every team code must be unique within the league"

            conferences = Counter(t.conference for t in teams)
            assert conferences == {"East": 15, "West": 15}

            divisions = Counter(t.division for t in teams)
            assert divisions == {
                "Atlantic": 5,
                "Central": 5,
                "Southeast": 5,
                "Northwest": 5,
                "Pacific": 5,
                "Southwest": 5,
            }

            # Spot-check a handful of real teams landed in the right
            # conference/division, not just that the counts add up.
            lal = await team_repo.get_team_by_code(pool, league.id, "LAL")
            assert lal is not None
            assert lal.name == "Lakers"
            assert lal.city == "Los Angeles"
            assert lal.conference == "West"
            assert lal.division == "Pacific"

            bos = await team_repo.get_team_by_code(pool, league.id, "BOS")
            assert bos is not None
            assert bos.conference == "East"
            assert bos.division == "Atlantic"

            okc = await team_repo.get_team_by_code(pool, league.id, "OKC")
            assert okc is not None
            assert okc.conference == "West"
            assert okc.division == "Northwest"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_league_writes_cap_settings_for_current_season():
    pool = await get_pool()
    try:
        league = await league_service.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Cap Settings Seed Test League",
            start_season_year=2025,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            cap_settings = await league_repo.get_cap_settings(pool, league.id, 2026)
            assert cap_settings is not None
            assert cap_settings.salary_cap == 154_647_000
            assert cap_settings.luxury_tax == 187_895_000
            assert cap_settings.apron_1 == 195_945_000
            assert cap_settings.apron_2 == 207_824_000
            assert cap_settings.max_pct_0_6 == 25
            assert cap_settings.max_pct_7_9 == 30
            assert cap_settings.max_pct_10_plus == 35
            assert cap_settings.supermax_pct == 35
            assert cap_settings.min_salary == 1_272_870
            assert cap_settings.mle_amount == 14_104_000
            assert cap_settings.bi_annual_amount == 5_130_000
            assert cap_settings.raise_pct_bird == 8
            assert cap_settings.raise_pct_non_bird == 5
            assert cap_settings.roster_min == 14
            assert cap_settings.roster_max == 15

            # No cap-settings row exists for start_season_year (2025) --
            # create_league only seeds current_season, never the whole
            # league history.
            assert await league_repo.get_cap_settings(pool, league.id, 2025) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_league_is_atomic_and_uses_teams_default_gm_philosophy():
    """Every seeded team round-trips the Phase 0 placeholder GM
    philosophy/timeline values (not the DB column defaults by coincidence --
    league_service explicitly passes them from seeds/teams.json)."""
    pool = await get_pool()
    try:
        league = await league_service.create_league(
            pool,
            discord_guild_id=_guild_id(),
            name="Atomicity Spot Check League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            teams = await team_repo.list_teams_for_league(pool, league.id)
            assert all(t.gm_philosophy == "tendency_respecter" for t in teams)
            assert all(t.timeline.value == "retool" for t in teams)
            assert all(1 <= t.home_market_size <= 5 for t in teams)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# assign_manager / remove_manager (Decision P5-41)
# ---------------------------------------------------------------------------

_COMMISSIONER_ID = 100
_MANAGER_ID = 200
_OTHER_MANAGER_ID = 201
_SPECTATOR_ID = 300


async def _make_manager_test_league(pool):
    league = await league_service.create_league(
        pool,
        discord_guild_id=_guild_id(),
        name="Manager Assignment Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER_ID,
        rng_seed=1,
    )
    teams = await team_repo.list_teams_for_league(pool, league.id)
    return league, teams[0]


@pytest.mark.asyncio
async def test_assign_manager_sets_manager_and_opens_a_history_row():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            updated = await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            assert updated.manager_user_id == _MANAGER_ID

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID

            history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert len(history) == 1
            assert history[0].user_id == _MANAGER_ID
            assert history[0].removed_at is None
            assert history[0].assigned_by == _COMMISSIONER_ID
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_reassignment_closes_the_prior_open_history_row():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_OTHER_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _OTHER_MANAGER_ID

            history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert len(history) == 2
            first, second = history
            assert first.user_id == _MANAGER_ID
            assert first.removed_at is not None
            assert second.user_id == _OTHER_MANAGER_ID
            assert second.removed_at is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_is_idempotent_for_the_same_user():
    """C-13's own "double-assign" idempotence obligation, at the service
    layer: re-assigning the SAME manager writes no second history row."""
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert len(history) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_rejects_a_non_commissioner_actor():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            with pytest.raises(PermissionDenied):
                await league_service.assign_manager(
                    pool,
                    league_id=league.id,
                    team_id=team.id,
                    user_id=_MANAGER_ID,
                    actor_user_id=_SPECTATOR_ID,
                )
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_raises_not_found_for_a_team_in_a_different_league():
    pool = await get_pool()
    try:
        league_a, _ = await _make_manager_test_league(pool)
        league_b, team_b = await _make_manager_test_league(pool)
        try:
            with pytest.raises(NotFoundError):
                await league_service.assign_manager(
                    pool,
                    league_id=league_a.id,
                    team_id=team_b.id,
                    user_id=_MANAGER_ID,
                    actor_user_id=_COMMISSIONER_ID,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_a.id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_b.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_clears_manager_and_closes_the_history_row():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            updated = await league_service.remove_manager(
                pool, league_id=league.id, team_id=team.id, actor_user_id=_COMMISSIONER_ID
            )
            assert updated.manager_user_id is None

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None

            history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert len(history) == 1
            assert history[0].removed_at is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_clears_auto_pilot_so_a_reassigned_manager_does_not_inherit_it():
    """GM-5 (docs/design/gm-command-surface.md), mutation 4 -- without the
    clear, a team removed while auto-piloted keeps `auto_pilot = TRUE` on a
    row with no manager, and the NEXT `/assign-manager` would silently drop
    a brand-new GM straight onto auto-pilot without anyone choosing it."""
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            await team_repo.set_auto_pilot(
                pool, team.id, auto_pilot=True, gm_philosophy=GmPhilosophy.EGALITARIAN
            )

            removed = await league_service.remove_manager(
                pool, league_id=league.id, team_id=team.id, actor_user_id=_COMMISSIONER_ID
            )
            assert removed.auto_pilot is False

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched.auto_pilot is False

            reassigned = await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID + 1,
                actor_user_id=_COMMISSIONER_ID,
            )
            assert reassigned.auto_pilot is False
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_is_idempotent_for_an_already_unmanaged_team():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            updated = await league_service.remove_manager(
                pool, league_id=league.id, team_id=team.id, actor_user_id=_COMMISSIONER_ID
            )
            assert updated.manager_user_id is None
            history = await team_repo.list_manager_history_for_team(pool, team.id)
            assert history == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_rejects_a_non_commissioner_actor():
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            with pytest.raises(PermissionDenied):
                await league_service.remove_manager(
                    pool, league_id=league.id, team_id=team.id, actor_user_id=_MANAGER_ID
                )
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Stage C critic round: authorization-before-lookup ordering nit, and
# Blocking Issue 5's outstanding-offer expiry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_manager_checks_commissioner_before_team_lookup():
    """A non-commissioner must get `PermissionDenied` for a NONEXISTENT
    team id, never `NotFoundError` -- the ordering fix that stops a
    non-commissioner from enumerating which team ids exist in a league
    by reading which exception comes back."""
    pool = await get_pool()
    try:
        league, _ = await _make_manager_test_league(pool)
        try:
            with pytest.raises(PermissionDenied):
                await league_service.assign_manager(
                    pool,
                    league_id=league.id,
                    team_id=999_999_999,
                    user_id=_MANAGER_ID,
                    actor_user_id=_SPECTATOR_ID,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_checks_commissioner_before_team_lookup():
    pool = await get_pool()
    try:
        league, _ = await _make_manager_test_league(pool)
        try:
            with pytest.raises(PermissionDenied):
                await league_service.remove_manager(
                    pool,
                    league_id=league.id,
                    team_id=999_999_999,
                    actor_user_id=_SPECTATOR_ID,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


async def _propose_ai_offer(pool, *, league, from_team_id: int, to_team_id: int):
    return await offer_repo.create_trade_offer(
        pool,
        league_id=league.id,
        season=league.current_season,
        phase=Phase.REGULAR_SEASON,
        from_team_id=from_team_id,
        to_team_id=to_team_id,
        proposed_by=None,
        status=TradeStatus.PROPOSED,
        players_from_to=(),
        players_to_from=(),
        picks_from_to=(),
        picks_to_from=(),
    )


@pytest.mark.asyncio
async def test_assign_manager_expires_the_teams_outstanding_proposed_offers():
    """Blocking Issue 5: docs/design/phase-5-discord-presentation.md's
    own P5-41 text -- "assignment ... on EITHER side expires that
    team's outstanding offers" -- a newly-assigned human manager should
    not inherit an AI-initiated offer negotiated before they existed as
    this team's decision-maker."""
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league.id)
            other_team = next(t for t in teams if t.id != team.id)
            offer = await _propose_ai_offer(
                pool, league=league, from_team_id=other_team.id, to_team_id=team.id
            )

            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )

            offers = await offer_repo.list_trade_offers_for_team(
                pool, league_id=league.id, team_id=team.id
            )
            refetched_offer = next(o for o in offers if o.id == offer.id)
            assert refetched_offer.status == TradeStatus.EXPIRED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_does_not_expire_already_resolved_offers():
    """§6.4's transition table: `PROPOSED` can move to any of the other
    five, but none of THOSE have an outgoing edge -- an already-`ACCEPTED`
    row must never be flipped to `EXPIRED` by a later manager change."""
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            teams = await team_repo.list_teams_for_league(pool, league.id)
            other_team = next(t for t in teams if t.id != team.id)
            offer = await _propose_ai_offer(
                pool, league=league, from_team_id=other_team.id, to_team_id=team.id
            )
            await pool.execute(
                "UPDATE trade_offers SET status = $2 WHERE id = $1",
                offer.id,
                TradeStatus.ACCEPTED.value,
            )

            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )

            offers = await offer_repo.list_trade_offers_for_team(
                pool, league_id=league.id, team_id=team.id
            )
            refetched_offer = next(o for o in offers if o.id == offer.id)
            assert refetched_offer.status == TradeStatus.ACCEPTED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_expires_the_teams_outstanding_proposed_offers():
    """Blocking Issue 5's other named case: "a team that stops being
    human-managed has nobody to answer.\""""
    pool = await get_pool()
    try:
        league, team = await _make_manager_test_league(pool)
        try:
            await league_service.assign_manager(
                pool,
                league_id=league.id,
                team_id=team.id,
                user_id=_MANAGER_ID,
                actor_user_id=_COMMISSIONER_ID,
            )
            teams = await team_repo.list_teams_for_league(pool, league.id)
            other_team = next(t for t in teams if t.id != team.id)
            offer = await _propose_ai_offer(
                pool, league=league, from_team_id=other_team.id, to_team_id=team.id
            )

            await league_service.remove_manager(
                pool, league_id=league.id, team_id=team.id, actor_user_id=_COMMISSIONER_ID
            )

            offers = await offer_repo.list_trade_offers_for_team(
                pool, league_id=league.id, team_id=team.id
            )
            refetched_offer = next(o for o in offers if o.id == offer.id)
            assert refetched_offer.status == TradeStatus.EXPIRED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()
