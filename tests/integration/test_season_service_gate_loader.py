"""`season_service.load_gate_facts` against real rows, and the two must-fire
boards of build-plan commit 7 run end to end (loader -> `evaluate_exit`):
a `DRAFT` with one unresolved pick, and a `REGULAR_SEASON_COMPLETE` with one
scheduled game (phase-5-stage-e-advance.md P5-99.e).

The pure verdict rows are in `tests/unit/services/test_season_service_exit_gate.py`;
what only a database can say is proved here -- the loader reads the right games
(regular season only, this season only, any status for the target's ceiling,
`scheduled` for the blockers), takes the human set from ONE team query and never
from `load_gm_inputs`, and reads nothing at all for a phase whose gate row
consults nothing.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, game_repo, league_repo, player_repo, team_repo
from dba2.domain.game import GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.services import gm_service, season_service
from dba2.services.season_service import BlockerCode

_SEASON = 2026
_HUMAN_MANAGER = 555


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


async def _make_league(pool, phase: Phase):
    return await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Gate Loader Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=phase,
    )


async def _make_team(pool, league_id: int, code: str, *, manager: int | None = None) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="West",
        division="Pacific",
        home_market_size=2,
        manager_user_id=manager,
    )
    return team.id


async def _make_game(
    pool,
    league_id: int,
    home: int,
    away: int,
    game_index: int,
    status: GameStatus,
    *,
    season: int = _SEASON,
    game_type: GameType = GameType.REGULAR,
) -> int:
    final = status is GameStatus.FINAL
    game = await game_repo.create_game(
        pool,
        league_id=league_id,
        season=season,
        game_index=game_index,
        game_date=date(2026, 11, 1),
        home_team_id=home,
        away_team_id=away,
        game_type=game_type,
        rng_seed=game_index + 1,
        status=status,
        home_score=100 if final else None,
        away_score=90 if final else None,
    )
    return game.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Draft",
        last_name="Prospect",
        primary_position=Position.PG,
        height_in=75,
        weight_lb=185,
        wingspan_in=78,
        birth_date=date(2005, 6, 6),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=60,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


async def test_regular_season_complete_with_one_scheduled_game_is_blocked_naming_it(pool):
    league = await _make_league(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        home = await _make_team(pool, league.id, "GLA")
        away = await _make_team(pool, league.id, "GLB")
        for index in range(3):
            await _make_game(pool, league.id, home, away, index, GameStatus.FINAL)
        last_open = await _make_game(pool, league.id, home, away, 3, GameStatus.SCHEDULED)

        facts = await season_service.load_gate_facts(pool, league)

        assert facts.max_game_index == 3
        assert [g.game_id for g in facts.scheduled_games] == [last_open]
        verdict = season_service.evaluate_exit(facts)
        assert not verdict.allowed
        assert [b.code for b in verdict.blockers] == [
            BlockerCode.GAMES_TARGET_NOT_REACHED,
            BlockerCode.REGULAR_SEASON_UNFINISHED,
        ]
        assert all(b.game_ids == (last_open,) for b in verdict.blockers)

        # Once it is played, the same league leaves the phase.
        await pool.execute(
            "UPDATE games SET status = 'final', home_score = 1, away_score = 0 WHERE id = $1",
            last_open,
        )
        assert season_service.evaluate_exit(
            await season_service.load_gate_facts(pool, league)
        ).allowed
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_the_loader_reads_only_the_scheduled_games_up_to_the_phases_own_target(pool):
    """Build-plan commit 10: the gate is evaluated on every press, so the loader reads
    an aggregate and the scheduled games AT OR BELOW the phase's target index -- what
    the gate consults -- instead of the whole season's rows. Two games (indexes 0 and
    1): `REGULAR_SEASON`'s target index is round(1 * 0.45) = 0, so game 1 is beyond it
    and is not read; nothing due is scheduled, so the gate allows leaving."""
    league = await _make_league(pool, Phase.REGULAR_SEASON)
    try:
        home = await _make_team(pool, league.id, "GLG")
        away = await _make_team(pool, league.id, "GLH")
        await _make_game(pool, league.id, home, away, 0, GameStatus.FINAL)
        await _make_game(pool, league.id, home, away, 1, GameStatus.SCHEDULED)

        facts = await season_service.load_gate_facts(pool, league)

        assert facts.max_game_index == 1, "the ceiling is over every game, read or not"
        assert facts.scheduled_games == ()
        assert season_service.evaluate_exit(facts).allowed
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_the_loader_reads_only_this_seasons_regular_season_games(pool):
    league = await _make_league(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        home = await _make_team(pool, league.id, "GLC")
        away = await _make_team(pool, league.id, "GLD")
        await _make_game(pool, league.id, home, away, 0, GameStatus.FINAL)
        await _make_game(pool, league.id, home, away, 1, GameStatus.SCHEDULED)
        # None of these are this phase's schedule: a playoff game and a
        # preseason game (same season), and a regular-season game of another one.
        await _make_game(
            pool, league.id, home, away, 100, GameStatus.SCHEDULED, game_type=GameType.PLAYOFF
        )
        await _make_game(
            pool, league.id, home, away, 101, GameStatus.SCHEDULED, game_type=GameType.PRESEASON
        )
        await _make_game(pool, league.id, home, away, 102, GameStatus.SCHEDULED, season=_SEASON + 1)

        facts = await season_service.load_gate_facts(pool, league)

        assert facts.max_game_index == 1
        assert [g.game_index for g in facts.scheduled_games] == [1]
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_a_postponed_game_does_not_block_but_still_counts_toward_the_ceiling(pool):
    league = await _make_league(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        home = await _make_team(pool, league.id, "GLE")
        away = await _make_team(pool, league.id, "GLF")
        await _make_game(pool, league.id, home, away, 0, GameStatus.FINAL)
        await _make_game(pool, league.id, home, away, 1, GameStatus.POSTPONED)

        facts = await season_service.load_gate_facts(pool, league)

        assert facts.max_game_index == 1, "a postponement must not move the target"
        assert facts.scheduled_games == ()
        assert season_service.evaluate_exit(facts).allowed
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


@pytest.mark.parametrize(
    "phase",
    [
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    ],
)
async def test_a_schedule_phase_with_no_games_loads_no_ceiling_and_is_blocked_by_name(pool, phase):
    """No schedule is not "nothing to wait for" (critic m2): the gate names it, so the
    phase cannot be left and its closing work (a media cycle over zero games, the stats
    rebuild over none) never runs."""
    league = await _make_league(pool, phase)
    try:
        facts = await season_service.load_gate_facts(pool, league)
        assert facts.max_game_index is None
        assert facts.scheduled_games == ()
        verdict = season_service.evaluate_exit(facts)
        assert not verdict.allowed
        assert [b.code for b in verdict.blockers] == [BlockerCode.SCHEDULE_MISSING]
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_regular_season_complete_reads_every_scheduled_game_not_just_those_up_to_its_target(
    pool, monkeypatch
):
    """Critic M2: the "regular season is unfinished" blocker is an absolute statement,
    so its loader must not narrow to the arithmetic target. It does not matter today
    only because the phase's fraction is 1.0 -- so this moves it. Ten games, fraction
    0.5 (target index 4), every game final except game 8: the loader must still return
    game 8, and the gate must still refuse for it, as the ABSOLUTE blocker alone (game 8
    is beyond the arithmetic target, so the target blocker does not fire)."""
    monkeypatch.setitem(season_service.GAMES_TARGET_FRACTION, Phase.REGULAR_SEASON_COMPLETE, 0.5)
    league = await _make_league(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        home = await _make_team(pool, league.id, "GLI")
        away = await _make_team(pool, league.id, "GLJ")
        for index in range(10):
            status = GameStatus.SCHEDULED if index == 8 else GameStatus.FINAL
            game_id = await _make_game(pool, league.id, home, away, index, status)
            if index == 8:
                open_game = game_id

        facts = await season_service.load_gate_facts(pool, league)

        assert facts.max_game_index == 9
        assert season_service.games_target_index(0.5, 9) == 4, "game 8 is beyond the target"
        assert [g.game_id for g in facts.scheduled_games] == [open_game]
        verdict = season_service.evaluate_exit(facts)
        assert [b.code for b in verdict.blockers] == [BlockerCode.REGULAR_SEASON_UNFINISHED]
        assert verdict.blockers[0].game_ids == (open_game,)
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_a_draft_with_one_unresolved_pick_is_blocked_end_to_end(pool):
    league = await _make_league(pool, Phase.DRAFT)
    try:
        ai_team = await _make_team(pool, league.id, "GLG")
        player_id = await _make_player(pool, league.id)
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=1,
            original_team_id=ai_team,
            current_team_id=ai_team,
            pick_number=1,
            used_for_player_id=player_id,
        )
        owed = await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=2,
            original_team_id=ai_team,
            current_team_id=ai_team,
            pick_number=None,
        )

        facts = await season_service.load_gate_facts(pool, league)

        assert [p.id for p in facts.draft_picks].count(owed.id) == 1
        verdict = season_service.evaluate_exit(facts)
        assert not verdict.allowed
        assert [b.code for b in verdict.blockers] == [BlockerCode.DRAFT_NOT_COMPLETE]
        assert "unresolved" in verdict.blockers[0].message
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_an_auto_piloted_team_on_the_clock_is_described_as_ai_not_a_person(pool):
    """GM-2 (docs/design/gm-command-surface.md), mutation 2 -- rewritten in
    that doc's own revision 2 after the old assertion (that this site
    unblocks the exit gate) was found to never go red: `_draft_blockers`
    returns a blocker for every non-COMPLETE state regardless of which set
    a team is in, so this site changes only which SENTENCE renders, never
    whether the gate blocks (mutation 2b, in a separate test, is the real
    exit-gate assertion). With an auto-piloted team on the clock and no
    eligible pick number to assign it (the pool exhausted), the message
    must read "the AI could not draft anyone" and must NOT read "managed
    by a person" -- the exact lie GM-3's message rules out elsewhere."""
    league = await _make_league(pool, Phase.DRAFT)
    try:
        auto_team = await _make_team(pool, league.id, "GLK", manager=_HUMAN_MANAGER)
        await team_repo.set_auto_pilot(
            pool, auto_team, auto_pilot=True, gm_philosophy=GmPhilosophy.EGALITARIAN
        )
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=1,
            original_team_id=auto_team,
            current_team_id=auto_team,
            pick_number=1,
        )

        facts = await season_service.load_gate_facts(pool, league)
        verdict = season_service.evaluate_exit(facts)

        assert not verdict.allowed
        message = verdict.blockers[0].message
        assert "the AI could not draft anyone" in message
        assert "managed by a person" not in message
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_the_human_set_comes_from_one_team_query_and_never_from_load_gm_inputs(
    pool, monkeypatch
):
    league = await _make_league(pool, Phase.DRAFT)
    try:
        human = await _make_team(pool, league.id, "GLH", manager=_HUMAN_MANAGER)
        ai = await _make_team(pool, league.id, "GLI")
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=1,
            original_team_id=human,
            current_team_id=human,
            pick_number=1,
        )

        team_queries: list[int] = []
        real_list_teams = team_repo.list_teams_for_league

        async def counting_list_teams(ex, league_id):
            team_queries.append(league_id)
            return await real_list_teams(ex, league_id)

        async def forbidden_load_gm_inputs(*args, **kwargs):
            raise AssertionError("the gate loader must not pay load_gm_inputs' round trips")

        monkeypatch.setattr(team_repo, "list_teams_for_league", counting_list_teams)
        monkeypatch.setattr(gm_service, "load_gm_inputs", forbidden_load_gm_inputs)

        facts = await season_service.load_gate_facts(pool, league)

        assert team_queries == [league.id], "exactly one team query"
        assert facts.self_managed_team_ids == frozenset({human})
        assert ai not in facts.self_managed_team_ids
        verdict = season_service.evaluate_exit(facts)
        assert not verdict.allowed
        assert "person" in verdict.blockers[0].message, (
            "the human set reached the verdict: a person's team is on the clock"
        )
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


async def test_a_finished_draft_loads_and_lets_the_league_leave(pool):
    league = await _make_league(pool, Phase.DRAFT)
    try:
        ai_team = await _make_team(pool, league.id, "GLJ")
        player_id = await _make_player(pool, league.id)
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=1,
            original_team_id=ai_team,
            current_team_id=ai_team,
            pick_number=1,
            used_for_player_id=player_id,
        )
        assert season_service.evaluate_exit(
            await season_service.load_gate_facts(pool, league)
        ).allowed
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)


@pytest.mark.parametrize("phase", [Phase.PLAYIN, Phase.AWARDS, Phase.RESIGN_WINDOW, Phase.WAIVERS])
async def test_a_phase_whose_gate_row_consults_nothing_reads_nothing(pool, monkeypatch, phase):
    league = await _make_league(pool, phase)
    try:

        async def forbidden(*args, **kwargs):
            raise AssertionError("the loader read a table for a phase that consults none")

        monkeypatch.setattr(game_repo, "list_games_for_league_season", forbidden)
        monkeypatch.setattr(draft_repo, "list_draft_picks_for_season", forbidden)
        monkeypatch.setattr(team_repo, "list_teams_for_league", forbidden)

        facts = await season_service.load_gate_facts(pool, league)

        assert facts == season_service.GateFacts(phase=phase)
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
