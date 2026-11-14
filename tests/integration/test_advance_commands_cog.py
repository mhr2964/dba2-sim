"""The five `/advance-*` companions driven through the real cog callbacks against real
Postgres, with an in-process fake interaction (build-plan commit 15;
docs/design/phase-5-stage-e-advance.md P5-99.f, P5-99.g, P5-99.u.1, P5-99.c).

The service behaviour behind each is proved in `test_season_service_*.py`; what these
cases add is that the COG puts the real service's answer in front of the person -- above
all, that `/advance-restore-game` surfaces each of P5-99.u.1's three refusals as its own
message, that `/advance-unlock` refuses under the age threshold and writes its row when it
clears, that `/advance-force` refuses `PRESEASON` and names what it overrode, and that
`/advance-to` stops before the first working phase and says which.

**Nothing here has run against a live Discord gateway**: the interaction is a recorder.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from dba2.bot.cogs import advance as cog_mod
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo
from dba2.domain.game import GameStatus
from dba2.domain.phase import Phase
from dba2.services import gm_service, season_service
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_draft_board,
    add_game,
    add_two_teams,
    bare_league_at,
    drop,
)

_D1 = date(2026, 11, 1)
_D2 = date(2026, 11, 2)


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def _stub_the_hook(monkeypatch):
    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.deferred: list[bool] = []
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        await asyncio.sleep(0)
        self._done = True
        self.deferred.append(ephemeral)

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        await asyncio.sleep(0)
        self._done = True
        self.messages.append({"content": content, "embed": None, "ephemeral": ephemeral})


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(
        self, content=None, *, embed=None, ephemeral: bool = False, allowed_mentions=None, **_kw
    ) -> None:
        await asyncio.sleep(0)
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, guild_id: int, user_id: int = COMMISSIONER) -> None:
        self.id = 1
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id)
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()

    @property
    def reply(self) -> dict:
        seen = self.response.messages + self.followup.sent
        assert len(seen) == 1, seen
        assert seen[0]["ephemeral"] is True
        return seen[0]


async def _interaction_for(pool, league_id: int) -> _Interaction:
    league = await league_repo.get_league(pool, league_id)
    return _Interaction(league.discord_guild_id)


@pytest.fixture
def cog():
    return cog_mod.AdvanceCommands(bot=SimpleNamespace())


async def _postponed_game(pool, phase: Phase):
    league_id = await bare_league_at(pool, phase)
    teams = await add_two_teams(pool, league_id)
    game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
    await game_repo.set_status(
        pool, game.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
    )
    return league_id, game


async def _restore(cog, interaction, game_id: int) -> None:
    await cog.advance_restore_game.callback(cog, interaction, game_id, "data fixed")


# --- /advance-restore-game: three refusals, three messages -----


async def test_each_restore_refusal_reaches_the_commissioner_as_its_own_message(pool, cog):
    refusals: dict[str, str] = {}

    # 1: outside the four schedule-consuming phases.
    league_id, game = await _postponed_game(pool, Phase.PLAYIN)
    try:
        interaction = await _interaction_for(pool, league_id)
        await _restore(cog, interaction, game.id)
        refusals["phase"] = interaction.reply["content"]
        assert (await game_repo.get_game(pool, game.id)).status is GameStatus.POSTPONED
    finally:
        await drop(pool, league_id)

    # 2: at REGULAR_SEASON_COMPLETE once the season's stats are rebuilt.
    league_id, game = await _postponed_game(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)
        interaction = await _interaction_for(pool, league_id)
        await _restore(cog, interaction, game.id)
        refusals["stats"] = interaction.reply["content"]
    finally:
        await drop(pool, league_id)

    # 3: the league has already played past the game's date.
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        blocked = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        later = await add_game(pool, league_id, teams, game_index=1, game_date=_D2)
        await pool.execute(
            "UPDATE games SET status = 'final', home_score = 1, away_score = 0 WHERE id = $1",
            later.id,
        )
        await game_repo.set_status(
            pool, blocked.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.POSTPONED
        )
        interaction = await _interaction_for(pool, league_id)
        await _restore(cog, interaction, blocked.id)
        refusals["past"] = interaction.reply["content"]
        assert str(later.id) in refusals["past"]
    finally:
        await drop(pool, league_id)

    assert "PLAYIN" in refusals["phase"] and "regular season" in refusals["phase"]
    assert "stats" in refusals["stats"] and "rebuilt" in refusals["stats"]
    assert "played past" in refusals["past"]
    assert len(set(refusals.values())) == 3, "three refusals, three different messages"


async def test_a_postpone_then_restore_round_trips_through_the_cog(pool, cog):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=_D1)
        interaction = await _interaction_for(pool, league_id)

        await cog.advance_postpone_game.callback(cog, interaction, game.id, "cannot sim")

        assert "postponed" in interaction.reply["content"]
        assert (await game_repo.get_game(pool, game.id)).status is GameStatus.POSTPONED

        interaction = await _interaction_for(pool, league_id)
        await _restore(cog, interaction, game.id)

        assert "scheduled" in interaction.reply["content"]
        assert (await game_repo.get_game(pool, game.id)).status is GameStatus.SCHEDULED
    finally:
        await drop(pool, league_id)


# --- /advance-unlock -----


async def test_unlock_refuses_a_young_claim_and_clears_an_old_one_with_its_audit_row(pool, cog):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        await season_service._claim_league(pool, league_id, 5)
        interaction = await _interaction_for(pool, league_id)

        await cog.advance_unlock.callback(cog, interaction)

        assert "minute(s) old" in interaction.reply["content"]
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_by == 5

        await pool.execute(
            "UPDATE leagues SET advance_claimed_at = NOW() - INTERVAL '20 minutes' WHERE id = $1",
            league_id,
        )
        interaction = await _interaction_for(pool, league_id)
        await cog.advance_unlock.callback(cog, interaction)

        assert "Cleared the advance claim" in interaction.reply["content"]
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_at is None
        rows = await pool.fetch(
            "SELECT txn_type FROM league_transactions WHERE league_id = $1", league_id
        )
        assert [r["txn_type"] for r in rows] == ["advance_unlock"]
    finally:
        await drop(pool, league_id)


# --- /advance-force -----


async def test_force_into_preseason_is_refused_through_the_cog(pool, cog):
    league_id = await bare_league_at(pool, Phase.WAIVERS)
    try:
        interaction = await _interaction_for(pool, league_id)

        await cog.advance_force.callback(cog, interaction, "PRESEASON", "restart the season")

        assert interaction.reply["content"] == season_service.FORCE_PRESEASON_MESSAGE
        assert (await league_repo.get_league(pool, league_id)).current_phase is Phase.WAIVERS
    finally:
        await drop(pool, league_id)


async def test_force_names_the_blockers_it_overrode_in_the_reply(pool, cog):
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), managers={"TOR": 101})
        interaction = await _interaction_for(pool, league_id)

        await cog.advance_force.callback(
            cog, interaction, "POST_DRAFT_TRADES", "the manager is gone"
        )

        embed = interaction.reply["embed"]
        assert embed.title == "Forced DRAFT -> POST_DRAFT_TRADES"
        fields = {f.name: f.value for f in embed.fields}
        assert "the draft is not finished" in fields["Blockers it overrode"]
        assert "closing work" in fields["Work it skipped"]
        assert (
            await league_repo.get_league(pool, league_id)
        ).current_phase is Phase.POST_DRAFT_TRADES
    finally:
        await drop(pool, league_id)


# --- /advance-to -----


async def test_advance_to_stops_before_the_first_working_phase_and_says_which(pool, cog):
    league_id = await bare_league_at(pool, Phase.PLAYOFFS_R1)
    try:
        interaction = await _interaction_for(pool, league_id)

        await cog.advance_to.callback(cog, interaction, "DRAFT_LOTTERY")

        embed = interaction.reply["embed"]
        assert embed.title == "Stopped short of DRAFT_LOTTERY"
        assert "DRAFT_LOTTERY" in embed.description and "/advance" in embed.description
        assert "AWARDS" in embed.description
        assert (await league_repo.get_league(pool, league_id)).current_phase is Phase.AWARDS
    finally:
        await drop(pool, league_id)
