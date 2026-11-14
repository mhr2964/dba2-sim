"""Live I/O proof for `dba2/bot/cogs/gm.py` (GM-6, docs/design/gm-
command-surface.md) -- the four commands moved here verbatim from
`roster.py` still behave identically, including §43.1 blocking issue 1's
honest-partial-success fix, now served through the shared `bot/team_
command_base.py` helpers instead of a second private copy.

`test_roster_cog.py`'s own `/set-role` follow-up tests moved here with
the command (the `/waive`/`/release` cases that test the SAME fix stay
in `test_roster_cog.py`, next to the command that still lives there).
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from dba2.bot.cogs import gm as gm_cog
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, player_repo, team_repo
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

    async def send_message(
        self, message: str | None = None, *, embed=None, ephemeral: bool = False
    ) -> None:
        self._done = True
        self.sent_messages.append(message)
        self.sent_embeds = getattr(self, "sent_embeds", [])
        self.sent_embeds.append(embed)


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content: str | None = None, *, embed=None, ephemeral: bool = False):
        self.sent.append({"content": content, "embed": embed})


class _FakeNamespace:
    def __init__(self, *, code: str | None = None) -> None:
        self.code = code


class _FakeInteraction:
    def __init__(
        self, *, guild_id: int, user_id: int, namespace: _FakeNamespace | None = None
    ) -> None:
        self.guild_id = guild_id
        self.guild = None
        self.user = _FakeUser(user_id)
        self.namespace = namespace if namespace is not None else _FakeNamespace()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.command = "test"
        self.id = random.randint(1, 10**9)


async def _make_league(pool, *, current_phase: Phase = Phase.REGULAR_SEASON) -> tuple[int, int]:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="GM Cog Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
        current_phase=current_phase,
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


@pytest.mark.asyncio
async def test_role_reports_an_honest_partial_success_when_the_player_refetch_fails():
    """Proves the §43.1 blocking-issue-1 fix survived the move: `/gm role`'s
    own follow-up is `player_repo.get_player`, not the team panel, and it
    gets the identical honest-partial-success treatment through `team_
    command_base.safe_get_player`."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RO1")
            player_id = await _make_player(pool, league_id, team_id)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            cog = gm_cog.GmCommands(bot=None)

            # `gm_command_service.set_role` itself calls `player_repo.get_
            # player` once, BEFORE the write, to validate the player is on
            # this team's roster -- patching the shared module attribute
            # naively would fail that read too and never reach the write
            # at all. This wrapper lets exactly the first call (the real
            # validation read) through and fails only the SECOND call
            # (`team_command_base.safe_get_player`'s own post-write
            # follow-up refetch), isolating the follow-up-only failure the
            # fix is about. Patched on the shared `player_repo` module
            # object directly -- every importer of it (gm_command_service,
            # team_command_base) shares the same attribute.
            original_get_player = player_repo.get_player
            call_count = {"n": 0}

            async def _flaky_get_player(ex, pid):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return await original_get_player(ex, pid)
                raise RuntimeError("player refetch exploded")

            with patch.object(player_repo, "get_player", side_effect=_flaky_get_player):
                await cog.role.callback(
                    cog, interaction, "RO1", player_id, "primary_option", 0.3, False, None
                )

            # The role really was written.
            from dba2.data.repositories import role_repo

            role = await role_repo.get_player_role(pool, league_id, team_id, 2026, player_id)
            assert role is not None
            assert role.role == "primary_option"

            assert len(interaction.followup.sent) == 1
            message = interaction.followup.sent[0]["content"] or ""
            assert "Role set" in message
            assert "no change was made" not in message.lower()
            assert "couldn't be shown" in message
            assert interaction.followup.sent[0]["embed"] is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_role_legitimately_missing_player_does_not_claim_a_followup_failure():
    """Round-3 critic nit, preserved from the pre-move test: `safe_get_
    player` wraps `player_repo.get_player`, which returns `Player | None`
    -- a LEGITIMATE `None` (the player genuinely isn't there) must not
    produce the same "couldn't be shown -- error logged" message an
    actual fetch exception gets (the test above)."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RO2")
            player_id = await _make_player(pool, league_id, team_id)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            cog = gm_cog.GmCommands(bot=None)

            original_get_player = player_repo.get_player
            call_count = {"n": 0}

            async def _vanishing_get_player(ex, pid):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return await original_get_player(ex, pid)
                return None

            with patch.object(player_repo, "get_player", side_effect=_vanishing_get_player):
                await cog.role.callback(
                    cog, interaction, "RO2", player_id, "primary_option", 0.3, False, None
                )

            from dba2.data.repositories import role_repo

            role = await role_repo.get_player_role(pool, league_id, team_id, 2026, player_id)
            assert role is not None

            assert len(interaction.followup.sent) == 1
            message = interaction.followup.sent[0]["content"] or ""
            assert "Role set" in message
            assert "no change was made" not in message.lower()
            assert "couldn't be shown" not in message
            assert "error logged" not in message
            assert "could not be found" in message
            assert interaction.followup.sent[0]["embed"] is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_service_failure_reaches_the_user_through_the_shared_reply_helper():
    """`/gm strategy`'s own failure translation, mirroring `test_roster_
    cog.py`'s identical `/waive` proof: a domain error is shown verbatim,
    an unexpected one gets the generic message -- both through `bot/
    interaction_errors.py`'s one shared helper, not a per-cog copy."""
    from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
    from dba2.core.errors import ValidationError
    from dba2.domain.team import DefensiveScheme, OffensiveScheme

    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            await _make_team(pool, league_id, "GM1")
            cog = gm_cog.GmCommands(bot=None)
            offense = gm_cog.app_commands.Choice(
                name="Motion", value=OffensiveScheme.MOTION.value
            )
            defense = gm_cog.app_commands.Choice(name="Drop", value=DefensiveScheme.DROP.value)

            domain = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            with patch.object(
                gm_cog.gm_command_service,
                "set_strategy",
                side_effect=ValidationError("pace_target must be between 0 and 100, got 500"),
            ):
                await cog.strategy.callback(
                    cog, domain, "GM1", 50, offense, defense, 50, 50, 50
                )
            assert domain.followup.sent == [
                {
                    "content": "pace_target must be between 0 and 100, got 500",
                    "embed": None,
                }
            ]

            unexpected = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            with patch.object(
                gm_cog.gm_command_service, "set_strategy", side_effect=RuntimeError("pool exploded")
            ):
                await cog.strategy.callback(
                    cog, unexpected, "GM1", 50, offense, defense, 50, 50, 50
                )
            assert unexpected.followup.sent == [
                {"content": GENERIC_FAILURE_MESSAGE, "embed": None}
            ]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# GM-10: player autocomplete (docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


async def _make_named_player(pool, league_id: int, team_id: int, *, first: str, last: str) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name=first,
        last_name=last,
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


async def test_player_autocomplete_filters_by_substring_and_caps_at_25():
    """Mutation 8's first half: a substring query must return a strict
    subset -- and, separately, an over-25 result is truncated rather than
    sent whole (Discord's own documented maximum)."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AC1")
            for i in range(27):
                await _make_named_player(pool, league_id, team_id, first="Bench", last=f"Guy{i}")
            target_id = await _make_named_player(
                pool, league_id, team_id, first="Jayson", last="Tatum"
            )

            interaction = _FakeInteraction(
                guild_id=guild_id, user_id=_MANAGER, namespace=_FakeNamespace(code="AC1")
            )
            all_results = await gm_cog._player_autocomplete(interaction, "")
            assert len(all_results) == 25, "over-25 results must be truncated, never sent whole"

            filtered = await gm_cog._player_autocomplete(interaction, "tatum")
            assert [c.value for c in filtered] == [target_id]
            assert len(filtered) < len(all_results)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_player_autocomplete_falls_back_to_the_managed_team_with_no_namespace_code():
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AC2")
            player_id = await _make_named_player(
                pool, league_id, team_id, first="Jaylen", last="Brown"
            )

            interaction = _FakeInteraction(
                guild_id=guild_id, user_id=_MANAGER, namespace=_FakeNamespace(code=None)
            )
            results = await gm_cog._player_autocomplete(interaction, "brown")
            assert [c.value for c in results] == [player_id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_player_autocomplete_returns_empty_with_no_resolvable_team():
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            interaction = _FakeInteraction(
                guild_id=guild_id, user_id=999_999_999, namespace=_FakeNamespace(code=None)
            )
            results = await gm_cog._player_autocomplete(interaction, "anything")
            assert results == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_player_autocomplete_never_raises_when_the_db_read_fails():
    """A callback whose DB read raises must return [], not propagate --
    an exception here would surface as a broken-looking picker."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AC3")
            await _make_named_player(pool, league_id, team_id, first="Test", last="Player")
            interaction = _FakeInteraction(
                guild_id=guild_id, user_id=_MANAGER, namespace=_FakeNamespace(code="AC3")
            )
            with patch.object(
                gm_cog.player_repo,
                "list_players_for_team",
                side_effect=RuntimeError("db exploded"),
            ):
                results = await gm_cog._player_autocomplete(interaction, "test")
            assert results == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_gm_player_shows_no_role_or_directive_set_for_a_fresh_player():
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PV1")
            player_id = await _make_player(pool, league_id, team_id)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            cog = gm_cog.GmCommands(bot=None)
            await cog.player.callback(cog, interaction, "PV1", player_id)

            assert interaction.response.sent_messages == [None]
            (embed,) = interaction.response.sent_embeds
            assert embed is not None
            fields = {f.name: f.value for f in embed.fields}
            assert fields["Role"] == "No role set for this player."
            assert fields["Directives"] == "No directives set for this player."
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# B1/B2 regression: `/gm status` and `/gm player` authorization
# (critic review of docs/design/gm-command-surface.md's GM-8/GM-9 build)
# ---------------------------------------------------------------------------

_STRANGER = 999
"""Neither `_COMMISSIONER` nor `_MANAGER` in any league these tests build --
the "any Discord user" case B1/B2 found completely unchecked."""


async def test_gm_status_refuses_a_non_manager_non_commissioner_user():
    """B1 -- `/gm status` used to call no authorization check at all and
    hand back a team's full panel (strategy knobs, timeline, auto-pilot
    state, GM philosophy, parked-offer count) to any Discord user who
    could type its team code. Must now refuse the same commissioner-or-
    manager union `/gm strategy`/`/gm role`/`/gm directive`/`/gm timeline`
    already take."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            await _make_team(pool, league_id, "ST1")
            cog = gm_cog.GmCommands(bot=None)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_STRANGER)
            await cog.status.callback(cog, interaction, "ST1")

            assert interaction.response.sent_embeds == [None]
            (message,) = interaction.response.sent_messages
            assert "may not act for team" in message
            assert interaction.response.is_done()

            # The team's own manager and the league's commissioner must
            # both still be let through -- this is not a lockout.
            for user_id in (_MANAGER, _COMMISSIONER):
                allowed = _FakeInteraction(guild_id=guild_id, user_id=user_id)
                await cog.status.callback(cog, allowed, "ST1")
                assert allowed.response.sent_embeds and allowed.response.sent_embeds[0] is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_gm_player_refuses_a_non_manager_non_commissioner_user():
    """B1/B2 -- `/gm player` had the identical missing bot-layer check as
    `/gm status`."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PL1")
            player_id = await _make_player(pool, league_id, team_id)
            cog = gm_cog.GmCommands(bot=None)

            interaction = _FakeInteraction(guild_id=guild_id, user_id=_STRANGER)
            await cog.player.callback(cog, interaction, "PL1", player_id)

            assert interaction.response.sent_embeds == [None]
            (message,) = interaction.response.sent_messages
            assert "may not act for team" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_gm_player_refuses_reading_a_rival_teams_player():
    """B2 -- `gm_command_service.get_gm_player_view` only checked league
    membership, so a team's own manager (correctly authorized to call
    `/gm player` for their OWN team code) could pass a rival's player id
    and read that player's private directive verbatim. Must be refused
    the same way `set_directive` already refuses it
    (`gm_command_service.py:1215-1216`)."""
    pool = await get_pool()
    try:
        league_id, guild_id = await _make_league(pool)
        try:
            own_team_id = await _make_team(pool, league_id, "OWN")
            rival_team_id = await _make_team(pool, league_id, "RIV")
            rival_player_id = await _make_player(pool, league_id, rival_team_id)
            cog = gm_cog.GmCommands(bot=None)

            # `_MANAGER` manages every team `_make_team` creates in this
            # test file's fixture, so authorization alone would pass here
            # -- this isolates the roster-membership check specifically.
            interaction = _FakeInteraction(guild_id=guild_id, user_id=_MANAGER)
            await cog.player.callback(cog, interaction, "OWN", rival_player_id)

            assert interaction.response.sent_embeds == [None]
            (message,) = interaction.response.sent_messages
            assert f"not on team {own_team_id}'s roster" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
