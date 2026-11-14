"""Live I/O proof for `dba2/bot/cogs/roster.py`'s blocking issue 1 fix
(critic round 1 on Stage D'): every write command's DB write and its
post-write panel/player fetch used to share one `try` block, so a follow-
up-only failure (the write committed, the confirmation render didn't) was
reported with the SAME "no change was made" message a real write failure
gets -- a lie the moment the write actually committed.

Reproduces the critic's exact repro: patch `view_service.get_team_panel`
(via `team_command_base.view_service`, the name `safe_team_panel` --
GM-6's shared helper, moved out of this cog -- imports and calls) to
raise, drive a real `/waive` callback against real Postgres, and confirm
(a) the player really is waived in the DB and (b) the user's followup is
an honest "waived, but couldn't show you the panel" message, never the
generic no-change-was-made text. `/gm role`'s identical proof (its own
follow-up is `player_repo.get_player` rather than the team panel) lives
in `test_gm_cog.py`, next to the command since GM-6 moved it.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from dba2.bot import team_command_base
from dba2.bot.cogs import roster as roster_cog
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, team_repo
from dba2.domain.contract import ContractType, SigningMethod
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus

_COMMISSIONER = 1
_MANAGER = 42


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self._done = False
        self.sent_messages: list[str] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True
        self._done = True

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        self._done = True
        self.sent_messages.append(message)


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content: str | None = None, *, embed=None, ephemeral: bool = False):
        self.sent.append({"content": content, "embed": embed})


class _FakeInteraction:
    def __init__(self, *, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.guild = None
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.command = "test"
        self.id = random.randint(1, 10**9)


async def _make_league(pool, *, current_phase: Phase = Phase.REGULAR_SEASON) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Roster Cog Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
        current_phase=current_phase,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=2026,
        salary_cap=150_000_000,
        luxury_tax=180_000_000,
        apron_1=190_000_000,
        apron_2=200_000_000,
        max_pct_0_6=25,
        max_pct_7_9=30,
        max_pct_10_plus=35,
        supermax_pct=35,
        min_salary=1_200_000,
        mle_amount=13_000_000,
        bi_annual_amount=5_000_000,
        raise_pct_bird=8,
        raise_pct_non_bird=5,
        roster_min=14,
        roster_max=15,
    )
    return league.id, league.discord_guild_id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        manager_user_id=_MANAGER,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1997, 6, 12),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _make_active_contract(pool, league_id: int, team_id: int, player_id: int) -> None:
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=2026,
        start_season=2026,
        end_season=2028,
        total_years=3,
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    for season, salary in ((2026, 3_000_000), (2027, 3_200_000), (2028, 3_400_000)):
        await contract_repo.create_contract_year(
            pool,
            contract_id=contract.id,
            season=season,
            salary=salary,
            guaranteed_amount=salary,
        )


@pytest.mark.asyncio
async def test_waive_reports_an_honest_partial_success_when_the_panel_fetch_fails():
    """The critic's exact repro: patch `view_service.get_team_panel` to
    raise, drive `/waive`, confirm the DB write committed (player really
    waived, dead cap really written) but the user is told the truth, not
    "no change was made"."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV1")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            cog = roster_cog.RosterCommands(bot=None)

            with patch.object(
                team_command_base.view_service,
                "get_team_panel",
                side_effect=RuntimeError("panel render exploded"),
            ):
                await cog.waive.callback(cog, interaction, "WV1", player_id, False)

            # The write committed for real -- this is the whole point.
            player = await player_repo.get_player(pool, player_id)
            assert player.roster_status == RosterStatus.FREE_AGENT
            assert player.team_id is None
            contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            assert any(c.status.value != "active" for c in contracts)

            assert interaction.response.deferred is True
            assert len(interaction.followup.sent) == 1
            message = interaction.followup.sent[0]["content"] or ""
            assert "Waived" in message
            assert "no change was made" not in message.lower()
            assert "couldn't be shown" in message
            # No panel embed -- the render is exactly what failed.
            assert interaction.followup.sent[0]["embed"] is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_waive_with_a_healthy_panel_fetch_still_shows_the_panel():
    """Control case for the test above -- confirms the fix didn't just
    stop sending panels altogether."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV2")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_active_contract(pool, league_id, team_id, player_id)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            cog = roster_cog.RosterCommands(bot=None)

            await cog.waive.callback(cog, interaction, "WV2", player_id, False)

            assert len(interaction.followup.sent) == 1
            sent = interaction.followup.sent[0]
            assert "Waived" in (sent["content"] or "")
            assert "couldn't be shown" not in (sent["content"] or "")
            assert sent["embed"] is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_service_failure_reaches_the_user_through_the_shared_reply_helper():
    """The failure translation moved to `bot/interaction_errors.py`; this pins
    that `/waive` still (a) shows a domain error verbatim and (b) answers an
    unexpected exception with the generic message, both as ephemeral followups
    after `defer()` -- the behaviour the cog's own private helpers had."""
    from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
    from dba2.core.errors import ValidationError

    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WV3")
            player_id = await _make_player(pool, league_id, team_id)
            cog = roster_cog.RosterCommands(bot=None)

            domain = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            with patch.object(
                roster_cog.gm_command_service,
                "waive_player",
                side_effect=ValidationError("that player is not on your roster"),
            ):
                await cog.waive.callback(cog, domain, "WV3", player_id, False)
            assert domain.followup.sent == [
                {"content": "that player is not on your roster", "embed": None}
            ]

            unexpected = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            with patch.object(
                roster_cog.gm_command_service,
                "waive_player",
                side_effect=RuntimeError("pool exploded"),
            ):
                await cog.waive.callback(cog, unexpected, "WV3", player_id, False)
            assert unexpected.followup.sent == [
                {"content": GENERIC_FAILURE_MESSAGE, "embed": None}
            ]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
