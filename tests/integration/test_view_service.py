"""Live I/O proof for `services/view_service.py` -- Phase 5 Stage B step 7
(docs/design/phase-5-discord-presentation.md §11, §10 test tier 2: "does
the assembled data match what the DB holds, never does it look right").
The pure rendering logic already has exhaustive coverage in `tests/unit/
presentation/`; this file proves only the I/O seams -- real standings
from `media_service.compute_current_pulse_snapshot` (never recomputed
here, Decision P5-5/P5-6), a real roster + cap summary, a real box score,
real media stories read verbatim, and B-12's five-source transactions
merge.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from dba2.core.errors import NotFoundError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    game_repo,
    league_repo,
    media_repo,
    player_repo,
    stats_repo,
    team_repo,
    txn_repo,
)
from dba2.data.repositories.stats_repo import _BOX_SCORE_COLUMN_NAMES
from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.game import GameStatus, GameType
from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.services import view_service

_SEASON = 2026


def _flat_box(value: int) -> dict[str, int]:
    return {name: value for name in _BOX_SCORE_COLUMN_NAMES}


def _box_with(**overrides: int) -> dict[str, int]:
    """A zero box with only the named columns non-zero -- used by the
    `/leaders` live-read tests below (docs/design/leaders-live.md) so a
    `REBOUNDS`-style two-column stat (`oreb`/`dreb`) can be set
    independently without every other column also carrying a value."""
    box = _flat_box(0)
    box.update(overrides)
    return box


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="View Service Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=_SEASON,
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
    return league.id


async def _make_team(
    pool, league_id: int, code: str, *, conference: str = "East", division: str = "Atlantic"
) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference=conference,
        division=division,
        home_market_size=3,
        gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL,
    )
    return team.id


def _standings_row(field_text: str, code: str) -> tuple[str, str, str, str, str]:
    """Parses one row's `Pos  Pct   GB    W -  L   Team` line out of a
    rendered standings field for `code` -- against the FULL formatted line
    (`presentation/standings.py::_table_lines`' own layout), not a loose
    substring check, so a mutant that gets one column right by coincidence
    (e.g. a leader's `-` GB showing up as a stray character elsewhere)
    cannot slip past it. Returns `(pos, pct, gb, wins, losses)`, all still
    strings -- callers compare against the exact rendered text."""
    line = next(ln for ln in field_text.splitlines() if f"{code} City" in ln)
    match = re.match(r"\s*(\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s*-\s*(\d+)\s+", line)
    assert match, line
    return match.groups()


async def _make_player(pool, league_id: int, team_id: int, *, last_name: str | None = None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=last_name or f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1998, 1, 1),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _make_final_game(
    pool, league_id: int, *, game_index: int, home_id: int, away_id: int,
    home_score: int, away_score: int,
) -> int:
    game = await game_repo.create_game(
        pool,
        league_id=league_id,
        season=_SEASON,
        game_index=game_index,
        game_date=date(2026, 11, game_index),
        home_team_id=home_id,
        away_team_id=away_id,
        game_type=GameType.REGULAR,
        rng_seed=game_index,
        status=GameStatus.FINAL,
        home_score=home_score,
        away_score=away_score,
    )
    return game.id


async def _seed_final_game_stats(
    pool, *, game_id: int, league_id: int, player_id: int, team_id: int, box: dict[str, int],
) -> None:
    """One player's box line for a real final game -- the `/leaders`
    live read's own source table (docs/design/leaders-live.md LD-1:
    `stats_repo.list_live_stat_leaders` reads `player_game_stats` straight,
    never `player_season_stats`)."""
    await stats_repo.create_player_game_stats(
        pool, game_id=game_id, league_id=league_id, player_id=player_id, team_id=team_id,
        minutes_played=Decimal("30.0"), plus_minus=0, box=box,
    )


async def _seed_final_games(
    pool,
    league_id: int,
    *,
    home_id: int,
    away_id: int,
    count: int,
    index_start: int,
    home_player: int | None = None,
    away_player: int | None = None,
    home_points: int = 10,
    away_points: int = 10,
) -> list[int]:
    """`count` final regular-season games between the same two teams -- docs/
    design/leaders-v2-per-game-and-paging.md LD2-2's own regression scenario
    needs series of 12-40 games, past `_make_final_game`'s own `date(2026, 11,
    game_index)` shortcut (November has 30 days), so this walks the date
    forward with `timedelta` instead. Returns the created game ids."""
    game_ids: list[int] = []
    for i in range(count):
        idx = index_start + i
        game = await game_repo.create_game(
            pool,
            league_id=league_id,
            season=_SEASON,
            game_index=idx,
            game_date=date(2026, 10, 1) + timedelta(days=idx),
            home_team_id=home_id,
            away_team_id=away_id,
            game_type=GameType.REGULAR,
            rng_seed=idx,
            status=GameStatus.FINAL,
            home_score=100,
            away_score=90,
        )
        game_ids.append(game.id)
        if home_player is not None:
            await _seed_final_game_stats(
                pool, game_id=game.id, league_id=league_id, player_id=home_player,
                team_id=home_id, box=_box_with(points=home_points),
            )
        if away_player is not None:
            await _seed_final_game_stats(
                pool, game_id=game.id, league_id=league_id, player_id=away_player,
                team_id=away_id, box=_box_with(points=away_points),
            )
    return game_ids


@pytest.mark.asyncio
async def test_get_standings_panel_reflects_real_games_never_recomputed_independently():
    """docs/design/standings-v2-conference-split.md: all content lives in
    `panel.fields` now (`description` is left empty), one field per
    conference -- `_make_team` seeds every team into "East", so both teams
    land in the same, single field here."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AAA")
            team_b = await _make_team(pool, league_id, "BBB")
            # AAA wins both games it plays -- 2-0; BBB is 0-2.
            await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=110, away_score=100,
            )
            await _make_final_game(
                pool, league_id, game_index=2, home_id=team_b, away_id=team_a,
                home_score=90, away_score=95,
            )

            panel = await view_service.get_standings_panel(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            assert panel.description == ""
            field_text = "\n".join(f.value for f in panel.fields)
            assert "2 - 0" in field_text
            assert "0 - 2" in field_text
            assert "AAA City" in field_text
            assert "BBB City" in field_text
            # AAA (2-0) leads the conference: no games behind, `.000`-free win pct.
            assert "1.000" in field_text
            assert "-.--" not in field_text
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_standings_panel_zero_games_league_renders_dashes_not_a_crash():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await _make_team(pool, league_id, "AAA")
            await _make_team(pool, league_id, "BBB")

            panel = await view_service.get_standings_panel(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            field_text = "\n".join(f.value for f in panel.fields)
            assert "-.--" in field_text
            assert " 0 - 0" in field_text
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_standings_panel_ranks_and_games_behind_are_per_conference():
    """docs/design/standings-v2-conference-split.md SV-3/SV-4, against REAL seeded games
    (B1): both conferences' `conference_rank` and `games_behind` are exercised end to end
    through `view_service.get_standings_panel`, not asserted against a hand-built
    `StandingsRow` one layer down. Two teams per conference, each conference's own leader
    at a different number of games played than its chaser -- a real, uneven-schedule GB
    number (`1.5`), not a round one a `99.0`-style constant mutant could coincide with."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "GBA", conference="East")
            team_b = await _make_team(pool, league_id, "GBB", conference="East")
            team_c = await _make_team(pool, league_id, "GBC", conference="West")
            team_d = await _make_team(pool, league_id, "GBD", conference="West")
            # East: A sweeps B twice (A: 2-0); B then beats D once for its only win (B: 1-2).
            await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=110, away_score=90,
            )
            await _make_final_game(
                pool, league_id, game_index=2, home_id=team_b, away_id=team_a,
                home_score=90, away_score=95,
            )
            await _make_final_game(
                pool, league_id, game_index=3, home_id=team_b, away_id=team_d,
                home_score=100, away_score=90,
            )
            # West: C beats D once (C: 1-0; D's loss here plus the one above makes D 0-2).
            await _make_final_game(
                pool, league_id, game_index=4, home_id=team_c, away_id=team_d,
                home_score=100, away_score=90,
            )

            panel = await view_service.get_standings_panel(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            assert len(panel.fields) == 2
            by_name = {f.name: f.value for f in panel.fields}
            assert set(by_name) == {"East", "West"}
            east, west = by_name["East"], by_name["West"]

            # East leader (A, 2-0): rank 1, `.000`-free `1.000` win pct, GB `-`.
            pos, pct, gb, wins, losses = _standings_row(east, "GBA")
            assert (pos, pct, gb, wins, losses) == ("1", "1.000", "-", "2", "0")
            # East chaser (B, 1-2 across 3 games vs A's 2): a REAL, uneven-schedule GB of
            # 1.5 -- ((2-1)+(2-0))/2 -- never reachable from a constant-99.0 mutant.
            pos, pct, gb, wins, losses = _standings_row(east, "GBB")
            assert (pos, pct, gb, wins, losses) == ("2", ".333", "1.5", "1", "2")

            # West leader (C, 1-0): rank 1 too -- proves the rank RESETS per conference,
            # never carrying East's numbering forward.
            pos, pct, gb, wins, losses = _standings_row(west, "GBC")
            assert (pos, pct, gb, wins, losses) == ("1", "1.000", "-", "1", "0")
            # West chaser (D, 0-2): winless but games played, so `.000`, not `-.--`; its GB
            # is measured against ITS OWN conference's leader (C, 1-0), not East's (A, 2-0)
            # -- both happen to be 1.5 here, which is why the rank/pos assertions above (not
            # just the GB number) are load-bearing: a `conference_rank=1`-for-everyone
            # mutant would still leave this exact GB number in the text.
            pos, pct, gb, wins, losses = _standings_row(west, "GBD")
            assert (pos, pct, gb, wins, losses) == ("2", ".000", "1.5", "0", "2")
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_standings_panel_floors_games_behind_at_zero_from_real_games():
    """SV-3's disclosed floor case, reached from real seeded games, not a hand-built
    `StandingsRow`: a percentage leader with FEWER wins than a chaser (an uneven number of
    games played) makes the naive GB formula go negative; `view_service` must floor it at
    `0.0`, never render a negative number."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            leader = await _make_team(pool, league_id, "GBL")
            chaser = await _make_team(pool, league_id, "GBX")
            fodder = await _make_team(pool, league_id, "GBZ")
            # Leader: 1-0 (a single, real win) -- the fewest games played, and the highest
            # win pct (1.000), of the three.
            await _make_final_game(
                pool, league_id, game_index=1, home_id=leader, away_id=fodder,
                home_score=110, away_score=90,
            )
            # Chaser: 5-1 (.833) -- more RAW wins than the leader, but a lower win pct, the
            # exact shape SV-3 says can compute negative under the naive formula.
            await _seed_final_games(
                pool, league_id, home_id=chaser, away_id=fodder, count=5, index_start=100,
            )
            await _make_final_game(
                pool, league_id, game_index=2, home_id=fodder, away_id=chaser,
                home_score=100, away_score=90,
            )

            panel = await view_service.get_standings_panel(
                pool, league_id=league_id, season=_SEASON, cycle_index=1
            )
            field_text = "\n".join(f.value for f in panel.fields)
            pos, pct, gb, wins, losses = _standings_row(field_text, "GBL")
            assert (pos, pct, gb, wins, losses) == ("1", "1.000", "-", "1", "0")
            # The naive formula gives ((1-5)+(1-0))/2 = -1.5 here; SV-3's floor renders it
            # as `0.0`, never negative and never dropped to `-` (the chaser HAS games played
            # and is not the leader, so `-` would be the wrong state entirely).
            pos, pct, gb, wins, losses = _standings_row(field_text, "GBX")
            assert (pos, pct, gb, wins, losses) == ("2", ".833", "0.0", "5", "1")
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_standings_panel_raises_not_found_for_missing_league():
    pool = await get_pool()
    try:
        with pytest.raises(NotFoundError):
            await view_service.get_standings_panel(
                pool, league_id=999_999_999, season=_SEASON, cycle_index=1
            )
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_team_panel_assembles_real_roster_and_cap_summary():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CCC")
            player_id = await _make_player(pool, league_id, team_id, last_name="Roster")
            contract = await contract_repo.create_contract(
                pool,
                league_id=league_id,
                player_id=player_id,
                team_id=team_id,
                signed_in_season=_SEASON,
                start_season=_SEASON,
                end_season=_SEASON + 2,
                total_years=3,
                contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )
            await contract_repo.create_contract_year(
                pool, contract_id=contract.id, season=_SEASON, salary=10_000_000,
                guaranteed_amount=10_000_000,
            )

            panel = await view_service.get_team_panel(
                pool, league_id=league_id, team_id=team_id, season=_SEASON, cycle_index=1
            )
            assert "Test Roster" in panel.description
            cap_field = next(f for f in panel.fields if f.name == "Cap Room")
            assert "10,000,000" in cap_field.value
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_team_panel_raises_not_found_for_team_in_another_league():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            other_team_id = await _make_team(pool, other_league_id, "OTH")
            with pytest.raises(NotFoundError):
                await view_service.get_team_panel(
                    pool, league_id=league_id, team_id=other_team_id, season=_SEASON,
                    cycle_index=1,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_boxscore_panel_assembles_real_stat_lines_and_resolves_names():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_id = await _make_team(pool, league_id, "HOM")
            away_id = await _make_team(pool, league_id, "AWY")
            home_player = await _make_player(pool, league_id, home_id, last_name="Home")
            away_player = await _make_player(pool, league_id, away_id, last_name="Away")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=home_id, away_id=away_id,
                home_score=108, away_score=101,
            )
            await stats_repo.create_player_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=home_player,
                team_id=home_id, minutes_played=Decimal("34.5"), plus_minus=7,
                box=_flat_box(3),
            )
            await stats_repo.create_player_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=away_player,
                team_id=away_id, minutes_played=Decimal("30.0"), plus_minus=-7,
                box=_flat_box(2),
            )
            await stats_repo.create_team_game_stats(
                pool, game_id=game_id, league_id=league_id, team_id=home_id,
                possessions=98, box=_flat_box(30),
            )
            await stats_repo.create_team_game_stats(
                pool, game_id=game_id, league_id=league_id, team_id=away_id,
                possessions=97, box=_flat_box(28),
            )
            await stats_repo.upsert_game_period_scores(
                pool,
                [
                    {"game_id": game_id, "league_id": league_id, "team_id": home_id,
                     "period": p, "points": 27}
                    for p in (1, 2, 3, 4)
                ]
                + [
                    {"game_id": game_id, "league_id": league_id, "team_id": away_id,
                     "period": p, "points": 25}
                    for p in (1, 2, 3, 4)
                ],
            )

            panel = await view_service.get_boxscore_panel(pool, game_id=game_id)
            assert "108" in panel.title
            assert "101" in panel.title
            assert "Test Home" in panel.description
            assert "Test Away" in panel.description
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_boxscore_panel_raises_not_found_for_missing_game():
    pool = await get_pool()
    try:
        with pytest.raises(NotFoundError):
            await view_service.get_boxscore_panel(pool, game_id=999_999_999)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_wire_panels_reads_rendered_text_verbatim_from_real_rows():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WIR")
            story = await media_repo.create_media_story(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=1,
                story_kind=StoryKind.STREAK,
                voice="beat_writer",
                template_id="t1",
                slot_bindings={"team": "WIR"},
                rendered_text="The WIR City squad has won four in a row.",
                salience=0.7,
                subject_player_id=None,
                subject_team_id=team_id,
                writer_id=None,
            )

            panels = await view_service.get_wire_panels(pool, league_id=league_id, season=_SEASON)
            assert len(panels) == 1
            assert panels[0].description == story.rendered_text

            limited = await view_service.get_wire_panels(
                pool, league_id=league_id, season=_SEASON, limit=1
            )
            assert len(limited) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_wire_panels_clamps_a_non_positive_limit_instead_of_returning_everything():
    """Blocking Issue 3, Stage C critic round: `stories[-limit:]` with
    `limit=0` evaluates as `stories[0:]` (the WHOLE list, not zero
    stories) and a negative `limit` slices from the FRONT instead of the
    back -- `/wire n:0` (or any `n <= 0`) posted every media story of
    the season, one Discord message each, reachable by any league
    member. Five real rows, `limit=0` and `limit=-3` must both behave
    like `limit=1` (the floor), never like "no limit at all"."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "WI2")
            for i in range(5):
                await media_repo.create_media_story(
                    pool,
                    league_id=league_id,
                    season=_SEASON,
                    phase=Phase.REGULAR_SEASON,
                    cycle_index=i,
                    story_kind=StoryKind.STREAK,
                    voice="beat_writer",
                    template_id="t1",
                    slot_bindings={"team": "WI2"},
                    rendered_text=f"Story number {i}.",
                    salience=0.7,
                    subject_player_id=None,
                    subject_team_id=team_id,
                    writer_id=None,
                )

            all_stories = await view_service.get_wire_panels(
                pool, league_id=league_id, season=_SEASON
            )
            assert len(all_stories) == 5

            zero_limit = await view_service.get_wire_panels(
                pool, league_id=league_id, season=_SEASON, limit=0
            )
            assert len(zero_limit) == 1, (
                "limit=0 returned the whole season's stories instead of being clamped to 1"
            )
            assert zero_limit[0].description == all_stories[-1].description

            negative_limit = await view_service.get_wire_panels(
                pool, league_id=league_id, season=_SEASON, limit=-3
            )
            assert len(negative_limit) == 1, (
                "a negative limit sliced from the front instead of being clamped to 1"
            )
            assert negative_limit[0].description == all_stories[-1].description

            oversized_limit = await view_service.get_wire_panels(
                pool, league_id=league_id, season=_SEASON, limit=10_000
            )
            assert len(oversized_limit) == 5
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_stat_leaders_orders_by_real_totals_descending():
    """docs/design/leaders-live.md LD-1: seeded straight through `games` +
    `player_game_stats`, never `stats_repo.upsert_player_season_stats` --
    this is the live read's own source table, and the league is left in
    `Phase.SETUP` (`_make_league`'s default), proving the read needs no
    `REGULAR_SEASON_COMPLETE` closing-work step to see real totals."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "LDA")
            team_b = await _make_team(pool, league_id, "LDB")
            low_scorer = await _make_player(pool, league_id, team_a, last_name="Low")
            high_scorer = await _make_player(pool, league_id, team_b, last_name="High")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=100, away_score=110,
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=low_scorer,
                team_id=team_a, box=_box_with(points=5),
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=high_scorer,
                team_id=team_b, box=_box_with(points=20),
            )

            leaders = await view_service.get_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, limit=5,
            )
            assert [row.player_id for row in leaders[:2]] == [high_scorer, low_scorer]
            assert leaders[0].stat_total == 20
            assert leaders[0].games_played == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_leaders_panel_resolves_real_player_names_and_team_codes():
    """Stage C's own half of `/leaders` -- `get_stat_leaders` above proves
    the ordering; this proves the name/team-code resolution `StatLeaderRow`
    itself carries neither of (that repo row's own "measured values only"
    split). Seeded via `games` + `player_game_stats`, same as above."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "LDR")
            team_b = await _make_team(pool, league_id, "OPP")
            scorer = await _make_player(pool, league_id, team_a, last_name="Scorer")
            opponent = await _make_player(pool, league_id, team_b, last_name="Opponent")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=105, away_score=95,
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=scorer,
                team_id=team_a, box=_box_with(points=5),
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=opponent,
                team_id=team_b, box=_box_with(points=2),
            )
            panel = (
                await view_service.get_leaders_panel(
                    pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                    stat=LeaderStat.POINTS,
                )
            ).panel
            assert "Test Scorer" in panel.description
            assert "LDR" in panel.description
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_leaders_panel_reflects_live_totals_mid_regular_season_no_completion_step():
    """The design doc's core claim (Problem/LD-1): a league still in
    `REGULAR_SEASON` -- never advanced through `REGULAR_SEASON_COMPLETE`,
    so `player_season_stats` has zero rows for it -- still gets real ranked
    `/leaders` totals, summed correctly across more than one final game."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        await league_repo.set_current_phase(pool, league_id, Phase.REGULAR_SEASON)
        try:
            team_a = await _make_team(pool, league_id, "MSA")
            team_b = await _make_team(pool, league_id, "MSB")
            leader = await _make_player(pool, league_id, team_a, last_name="Leader")
            trailer = await _make_player(pool, league_id, team_b, last_name="Trailer")

            game_1 = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=110, away_score=100,
            )
            await _seed_final_game_stats(
                pool, game_id=game_1, league_id=league_id, player_id=leader,
                team_id=team_a, box=_box_with(points=18),
            )
            await _seed_final_game_stats(
                pool, game_id=game_1, league_id=league_id, player_id=trailer,
                team_id=team_b, box=_box_with(points=10),
            )

            game_2 = await _make_final_game(
                pool, league_id, game_index=2, home_id=team_b, away_id=team_a,
                home_score=90, away_score=95,
            )
            await _seed_final_game_stats(
                pool, game_id=game_2, league_id=league_id, player_id=leader,
                team_id=team_a, box=_box_with(points=14),
            )
            await _seed_final_game_stats(
                pool, game_id=game_2, league_id=league_id, player_id=trailer,
                team_id=team_b, box=_box_with(points=12),
            )

            league = await league_repo.get_league(pool, league_id)
            assert league is not None
            assert league.current_phase == Phase.REGULAR_SEASON

            panel = (
                await view_service.get_leaders_panel(
                    pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                    stat=LeaderStat.POINTS,
                )
            ).panel
            leaders = await view_service.get_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, limit=5,
            )
            assert [row.player_id for row in leaders] == [leader, trailer]
            assert leaders[0].stat_total == 32  # 18 + 14
            assert leaders[0].games_played == 2
            assert leaders[1].stat_total == 22  # 10 + 12
            assert "Test Leader" in panel.description
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_leaders_panel_shows_empty_message_for_a_zero_games_league():
    """LD-2: a league with no final games (e.g. `/leaders` pressed before
    the season's first game sims) gets the empty-season message, never a
    bare header row over no rows."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await _make_team(pool, league_id, "ZRO")
            view = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS,
            )
            assert view.panel.description == "No games played yet this season."
            assert view.is_empty and view.no_games

            leaders = await view_service.get_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, limit=10,
            )
            assert leaders == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


_LEADER_STAT_LIVE_CASES: tuple[tuple, ...] = (
    (LeaderStat.POINTS, {"points": 24}, {"points": 9}, 24, 9),
    (LeaderStat.REBOUNDS, {"oreb": 5, "dreb": 10}, {"oreb": 1, "dreb": 1}, 15, 2),
    (LeaderStat.ASSISTS, {"ast": 12}, {"ast": 3}, 12, 3),
    (LeaderStat.STEALS, {"stl": 4}, {"stl": 1}, 4, 1),
    (LeaderStat.BLOCKS, {"blk": 3}, {"blk": 0}, 3, 0),
    (LeaderStat.TURNOVERS, {"tov": 6}, {"tov": 1}, 6, 1),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stat, high_overrides, low_overrides, high_total, low_total", _LEADER_STAT_LIVE_CASES
)
async def test_get_stat_leaders_reads_every_leader_stat_live_from_box_scores(
    stat, high_overrides, low_overrides, high_total, low_total
):
    """One case per `LeaderStat`, including `REBOUNDS`'s `(oreb + dreb)`
    expression (the syntax error critic round 2 caught against a blind
    `pgs.` prefix, docs/design/leaders-live.md LD-1) -- against real rows
    in `player_game_stats`, never `player_season_stats`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "SHI")
            team_b = await _make_team(pool, league_id, "SLO")
            high_player = await _make_player(pool, league_id, team_a, last_name="High")
            low_player = await _make_player(pool, league_id, team_b, last_name="Low")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=100, away_score=95,
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=high_player,
                team_id=team_a, box=_box_with(**high_overrides),
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=low_player,
                team_id=team_b, box=_box_with(**low_overrides),
            )

            leaders = await view_service.get_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=stat, limit=10,
            )
            assert [row.player_id for row in leaders] == [high_player, low_player]
            assert leaders[0].stat_total == high_total
            assert leaders[1].stat_total == low_total
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_leaders_live_read_never_writes_to_or_reopens_player_season_stats():
    """Regression proof that B1/B2 (revision 1's rejected rebuild-on-read
    idea, docs/design/leaders-live.md's Revision note) stay closed: a
    `/leaders`-equivalent call, run against a league with real final games
    and zero `player_season_stats` rows, leaves that table's row count for
    the league at zero -- and media's own `player_season_stats` reader
    (`get_career_scoring_to_date_for_league`, the exact function the design
    doc names as B1's risk) still sees nothing, exactly as it does today."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "REG")
            team_b = await _make_team(pool, league_id, "OPP")
            player_id = await _make_player(pool, league_id, team_a, last_name="Regression")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=100, away_score=90,
            )
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=player_id,
                team_id=team_a, box=_box_with(points=15),
            )

            async def _season_stats_row_count() -> int:
                return await pool.fetchval(
                    "SELECT COUNT(*) FROM player_season_stats WHERE league_id = $1", league_id
                )

            before = await _season_stats_row_count()
            assert before == 0

            for _ in range(3):  # a few repeated /leaders-equivalent reads
                await view_service.get_leaders_panel(
                    pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                    stat=LeaderStat.POINTS,
                )

            after = await _season_stats_row_count()
            assert after == before == 0

            media_view = await stats_repo.get_career_scoring_to_date_for_league(
                pool, league_id, season=_SEASON, game_type=GameType.REGULAR
            )
            assert media_view == {}, (
                "media's player_season_stats reader saw a row a /leaders read created"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_leaders_panel_raises_not_found_for_missing_league():
    pool = await get_pool()
    try:
        with pytest.raises(NotFoundError):
            await view_service.get_leaders_panel(
                pool, league_id=999_999_999, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS,
            )
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# /leaders v2 (docs/design/leaders-v2-per-game-and-paging.md) -- the per-team
# qualification bar (LD2-2), per-game-before-limit ordering (LD2-1), paging
# (LD2-4) and `LeadersView`'s two empty branches (LD2-6). `get_leaders_panel`'s
# existing call sites above (mode defaults to `PER_GAME`) already prove the
# v1-shaped happy path still works; these prove v2's own new behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bar_never_wipes_a_team_even_at_very_different_games_played():
    """LD2-2's own regression test: revision 1's league-wide `MAX` bar would
    delete every player on the team that is behind (measured live against
    league 543: 21 of 30 teams wiped at one snapshot). The per-team bar
    (LD2-2's replacement) cannot, because a full-season player's own
    `games_played` always equals his own team's `own_team_games` -- true no
    matter how far ahead or behind his team's schedule is."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_ahead = await _make_team(pool, league_id, "AHD")
            team_behind = await _make_team(pool, league_id, "BHD")
            opponent = await _make_team(pool, league_id, "OPX")
            ahead_player = await _make_player(pool, league_id, team_ahead, last_name="Ahead")
            behind_player = await _make_player(pool, league_id, team_behind, last_name="Behind")

            # team_ahead: 40 games. team_behind: 12 games. `opponent` (the away side of
            # both series) racks up 52 -- the "busiest team" a league-wide MAX would
            # measure everyone against, and the exact shape of the bug LD2-2 replaces.
            await _seed_final_games(
                pool, league_id, home_id=team_ahead, away_id=opponent, count=40,
                index_start=1, home_player=ahead_player, home_points=10,
            )
            await _seed_final_games(
                pool, league_id, home_id=team_behind, away_id=opponent, count=12,
                index_start=100, home_player=behind_player, home_points=8,
            )

            page = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=True,
                page=1, page_size=10,
            )
            qualified_ids = {row.player_id for row in page.rows}
            assert ahead_player in qualified_ids, "the ahead team lost a full-season player"
            assert behind_player in qualified_ids, "the behind team lost a full-season player"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_traded_player_qualifies_using_the_min_of_his_teams_games():
    """LD2-2: `own_team_games` is the MIN of `team_games` over every team a
    player has appeared for, so a trade can never push him under the bar by
    measuring him against a schedule his own games don't support."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_old = await _make_team(pool, league_id, "OLD")
            team_new = await _make_team(pool, league_id, "NEW")
            opponent = await _make_team(pool, league_id, "OPX")
            traded = await _make_player(pool, league_id, team_new, last_name="Traded")

            # team_old plays 10 games; the player appears in 6 of them (before the trade).
            old_games = await _seed_final_games(
                pool, league_id, home_id=team_old, away_id=opponent, count=10, index_start=1,
            )
            for game_id in old_games[:6]:
                await _seed_final_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=traded,
                    team_id=team_old, box=_box_with(points=10),
                )
            # team_new plays 8 games; the player appears in 2 of them (after the trade).
            # own_team_games = MIN(10, 8) = 8; games_played = 6 + 2 = 8.
            new_games = await _seed_final_games(
                pool, league_id, home_id=team_new, away_id=opponent, count=8, index_start=100,
            )
            for game_id in new_games[:2]:
                await _seed_final_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=traded,
                    team_id=team_new, box=_box_with(points=12),
                )

            page = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=True,
                page=1, page_size=10,
            )
            row = next(r for r in page.rows if r.player_id == traded)
            assert row.games_played == 8
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_per_game_ordering_is_computed_before_the_limit_not_after():
    """LD2-1's own correctness point: a low-games, high-average player is
    nowhere near the top by TOTAL (every filler here outscores him 200-50), so
    limiting a totals-ordered query before dividing would silently omit him.
    The real query's `ROW_NUMBER` orders by the per-game ratio INSIDE the
    window function, before `OFFSET`/`LIMIT`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            filler_team = await _make_team(pool, league_id, "FIL")
            hot_team = await _make_team(pool, league_id, "HOT")
            opponent = await _make_team(pool, league_id, "OPX")
            filler_players = [
                await _make_player(pool, league_id, filler_team, last_name=f"Filler{i}")
                for i in range(10)
            ]
            hot_player = await _make_player(pool, league_id, hot_team, last_name="HotStreak")

            for g in range(5):
                game_id = await _make_final_game(
                    pool, league_id, game_index=g + 1, home_id=filler_team, away_id=opponent,
                    home_score=100, away_score=90,
                )
                for player_id in filler_players:
                    await _seed_final_game_stats(
                        pool, game_id=game_id, league_id=league_id, player_id=player_id,
                        team_id=filler_team, box=_box_with(points=40),  # 5 games -> total 200
                    )
            hot_game = await _make_final_game(
                pool, league_id, game_index=6, home_id=hot_team, away_id=opponent,
                home_score=100, away_score=95,
            )
            await _seed_final_game_stats(
                pool, game_id=hot_game, league_id=league_id, player_id=hot_player,
                team_id=hot_team, box=_box_with(points=50),  # 1 game -> total 50, but 50.0 ppg
            )

            page = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=False,
                page=1, page_size=10,
            )
            assert page.rows[0].player_id == hot_player
            assert page.rows[0].rank == 1
            assert all(row.stat_total >= 200 for row in page.rows[1:])
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_total_mode_applies_no_bar_even_for_a_one_game_player():
    """LD2-2: TOTAL mode passes `apply_bar=False` -- a total already encodes
    playing time, so a one-game late signee is excluded from PER_GAME but
    still shown under TOTAL."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "TNB")
            team_b = await _make_team(pool, league_id, "TNC")
            long_games = await _seed_final_games(
                pool, league_id, home_id=team_a, away_id=team_b, count=10, index_start=1,
            )
            veteran = await _make_player(pool, league_id, team_a, last_name="Veteran")
            for game_id in long_games:
                await _seed_final_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=veteran,
                    team_id=team_a, box=_box_with(points=10),
                )
            late_signee = await _make_player(pool, league_id, team_a, last_name="LateSignee")
            await _seed_final_game_stats(
                pool, game_id=long_games[-1], league_id=league_id, player_id=late_signee,
                team_id=team_a, box=_box_with(points=99),
            )

            per_game = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=True,
                page=1, page_size=10,
            )
            assert late_signee not in {row.player_id for row in per_game.rows}

            totals = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )
            assert late_signee in {row.player_id for row in totals.rows}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_zero_row_result_still_carries_bar_metadata():
    """LD2-1 [r2/B2]: `bar b LEFT JOIN page p ON TRUE` returns exactly one row
    always -- a season with no final games at all gets `(NULL, NULL, 0)`; a
    season with final games but no qualifying rows still carries the real
    team-games range."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "ZBA")
            team_b = await _make_team(pool, league_id, "ZBB")
            await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=100, away_score=90,
            )

            no_final_games_at_all = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=9999, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=True,
                page=1, page_size=10,
            )
            assert no_final_games_at_all.rows == ()
            assert no_final_games_at_all.min_team_games is None
            assert no_final_games_at_all.max_team_games is None
            assert no_final_games_at_all.teams_counted == 0

            games_exist_nobody_qualifies = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME, apply_bar=True,
                page=1, page_size=10,
            )
            assert games_exist_nobody_qualifies.rows == ()
            assert games_exist_nobody_qualifies.min_team_games == 1
            assert games_exist_nobody_qualifies.max_team_games == 1
            assert games_exist_nobody_qualifies.teams_counted == 2
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qualified_count, expected_has_more", [(10, False), (11, True), (20, True)]
)
async def test_has_more_at_the_page_size_plus_one_boundary(qualified_count, expected_has_more):
    """LD2-4: fetch `page_size + 1`, `has_more = len(fetched) > page_size` --
    checked at the boundary itself (exactly 10 qualified rows, `page_size=10`,
    is NOT `has_more`) and just past it (11, 20)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PSA")
            team_b = await _make_team(pool, league_id, "PSB")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=200, away_score=190,
            )
            for i in range(qualified_count):
                player_id = await _make_player(pool, league_id, team_a, last_name=f"P{i:02d}")
                await _seed_final_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=player_id,
                    team_id=team_a, box=_box_with(points=qualified_count - i),
                )
            page = await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )
            assert page.has_more is expected_has_more
            assert len(page.rows) == min(qualified_count, 10)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_unknown_leader_mode_raises_validation_error():
    pool = await get_pool()
    try:
        with pytest.raises(ValidationError, match="Unknown LeaderMode"):
            await stats_repo.list_live_stat_leaders(
                pool, league_id=1, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode="not_a_real_mode", apply_bar=False,
                page=1, page_size=10,
            )
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_page_1_then_page_2_have_no_overlap_and_no_gap():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "PGA")
            team_b = await _make_team(pool, league_id, "PGB")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=200, away_score=190,
            )
            names = [f"Q{i:02d}" for i in range(15)]
            for i, name in enumerate(names):
                player_id = await _make_player(pool, league_id, team_a, last_name=name)
                await _seed_final_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=player_id,
                    team_id=team_a, box=_box_with(points=100 - i),  # strictly descending
                )

            page1 = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, page=1,
            )
            page2 = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, page=2,
            )
            assert page1.page == 1 and page2.page == 2
            assert page1.has_more is True and page2.has_more is False
            assert page1.row_count == 10 and page2.row_count == 5
            for name in names[:10]:
                assert name in page1.panel.description and name not in page2.panel.description
            for name in names[10:]:
                assert name in page2.panel.description and name not in page1.panel.description
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_stale_page_past_the_end_clamps_to_page_1():
    """LD2-4's one-shot clamp: a page that comes back empty (the qualified
    list got shorter than it used to be) re-renders page 1 instead of an
    empty panel on a non-empty list."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "STA")
            team_b = await _make_team(pool, league_id, "STB")
            game_id = await _make_final_game(
                pool, league_id, game_index=1, home_id=team_a, away_id=team_b,
                home_score=110, away_score=100,
            )
            player_id = await _make_player(pool, league_id, team_a, last_name="Solo")
            await _seed_final_game_stats(
                pool, game_id=game_id, league_id=league_id, player_id=player_id,
                team_id=team_a, box=_box_with(points=20),
            )
            view = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, page=5,
            )
            assert view.page == 1
            assert view.row_count == 1
            assert not view.is_empty
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_leaders_view_is_empty_and_no_games_are_correct_in_each_state():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "NGA")
            team_b = await _make_team(pool, league_id, "NGB")

            # Case 1: no final games at all.
            no_games_view = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS,
            )
            assert no_games_view.no_games and no_games_view.is_empty

            # Case 2: final games exist, but a one-game late signee can't clear the
            # per-game bar on a team that has already played many.
            veteran_games = await _seed_final_games(
                pool, league_id, home_id=team_a, away_id=team_b, count=10, index_start=1,
            )
            late_signee = await _make_player(pool, league_id, team_a, last_name="LateOnly")
            await _seed_final_game_stats(
                pool, game_id=veteran_games[-1], league_id=league_id, player_id=late_signee,
                team_id=team_a, box=_box_with(points=5),
            )
            nobody_qualifies_view = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.PER_GAME,
            )
            assert nobody_qualifies_view.is_empty and not nobody_qualifies_view.no_games

            # TOTAL applies no bar, so the same late signee IS visible there.
            totals_view = await view_service.get_leaders_panel(
                pool, league_id=league_id, season=_SEASON, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL,
            )
            assert not totals_view.is_empty and not totals_view.no_games
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_phase_panel_shows_current_phase_and_real_legal_next():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            panel = await view_service.get_phase_panel(pool, league_id=league_id)
            assert "SETUP" in panel.description
            assert "REGULAR_SEASON" in panel.description  # a real legal next phase
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_phase_panel_raises_not_found_for_missing_league():
    pool = await get_pool()
    try:
        with pytest.raises(NotFoundError):
            await view_service.get_phase_panel(pool, league_id=999_999_999)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_league_transactions_feed_merges_all_seven_sources_with_real_derived_phases():
    """Covers all SEVEN real `TransactionFeedItem.kind` values the merge
    emits -- the original version of this test covered only five
    (`contract_terminated` and `manager_removed` had zero coverage, the
    exact gap an independent critic re-verification of `ff544f6` flagged
    as blocking issue 2). Crosses a real phase boundary (`REGULAR_SEASON`
    -> `PLAYOFFS_R1`) between the manager assignment/contract signing and
    the manager removal/contract termination, so each item's expected
    `(season, phase)` is a hand-verified fact about which side of that
    boundary its own real timestamp falls on -- not merely "the final
    list happens to be sorted," which is all the original assertion
    (`feed[i].phase_ordinal <= feed[i + 1].phase_ordinal`, checked
    against a list this module's own `.sort()` on that exact key had
    just produced one line earlier) could ever prove."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "MRG")
            team_b = await _make_team(pool, league_id, "MRG2")
            player_a = await _make_player(pool, league_id, team_a, last_name="TradedAway")
            player_b = await _make_player(pool, league_id, team_b, last_name="TradedIn")
            drafted_player = await _make_player(
                pool, league_id, team_a, last_name="Drafted"
            )
            signed_player = await _make_player(pool, league_id, team_a, last_name="Signed")
            removed_player = await _make_player(pool, league_id, team_a, last_name="Kept")

            # 1. phase_history: SETUP -> REGULAR_SEASON -> PLAYOFFS_R1. Two
            #    real transitions, not one, so a "before the boundary" and
            #    an "after the boundary" event both have a real, distinct
            #    expected phase to be checked against.
            await league_repo.record_phase_transition(
                pool, league_id=league_id, season=_SEASON, from_phase=Phase.SETUP,
                to_phase=Phase.REGULAR_SEASON, actor_user_id=1, forced=False, reason=None,
            )

            # 2. league_transactions: a trade, during REGULAR_SEASON.
            await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                team_a_id=team_a, team_b_id=team_b, players_a_to_b=(player_a,),
                players_b_to_a=(player_b,), picks_a_to_b=(), picks_b_to_a=(),
                actor_user_id=None, source_offer_id=None,
            )

            # 3. team_manager_history: an assignment during REGULAR_SEASON,
            #    still open (no removal) -- `manager_assigned` coverage.
            await team_repo.record_manager_assignment(
                pool, team_id=team_a, user_id=555, assigned_by=1,
            )

            # 3b. A SECOND assignment, closed after the boundary -- real
            #    `manager_removed` coverage, distinct from `manager_assigned`.
            removed_history = await team_repo.record_manager_assignment(
                pool, team_id=team_b, user_id=777, assigned_by=1,
            )

            # 4. draft_selections: this season's draft, during REGULAR_SEASON.
            draft = await draft_repo.create_draft(
                pool, league_id=league_id, season=_SEASON, total_picks=1,
            )
            pick = await draft_repo.create_draft_pick(
                pool, league_id=league_id, season=_SEASON, round=1,
                original_team_id=team_a, current_team_id=team_a,
            )
            await draft_repo.create_draft_selection(
                pool, draft_id=draft.id, pick_number=1, round=1, team_id=team_a,
                player_id=drafted_player, pick_asset_id=pick.id,
            )

            # 5. contracts: signed during REGULAR_SEASON.
            await contract_repo.create_contract(
                pool, league_id=league_id, player_id=signed_player, team_id=team_a,
                signed_in_season=_SEASON, start_season=_SEASON, end_season=_SEASON + 1,
                total_years=2, contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )

            # 6. A second contract, signed during REGULAR_SEASON but
            #    TERMINATED after the boundary, during PLAYOFFS_R1 -- the
            #    exact reproduction of blocking issue 1 (a contract's
            #    `contract_terminated` item was stamped with its SIGNING
            #    timestamp/phase, sorting it into the wrong place). This is
            #    the regression test that blocking issue 1's real fix
            #    (`contracts.terminated_at`, migration 015) closes it.
            terminated_contract = await contract_repo.create_contract(
                pool, league_id=league_id, player_id=removed_player, team_id=team_a,
                signed_in_season=_SEASON, start_season=_SEASON, end_season=_SEASON + 1,
                total_years=2, contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE,
            )

            # Cross the boundary: REGULAR_SEASON -> PLAYOFFS_R1.
            await league_repo.record_phase_transition(
                pool, league_id=league_id, season=_SEASON, from_phase=Phase.REGULAR_SEASON,
                to_phase=Phase.PLAYOFFS_R1, actor_user_id=1, forced=False, reason=None,
            )

            # 3b (cont'd). Close the second manager assignment now, AFTER
            #    the boundary -- its `manager_removed` item must be stamped
            #    PLAYOFFS_R1, not REGULAR_SEASON.
            await team_repo.close_manager_assignment(pool, removed_history.id)

            # 6 (cont'd). Terminate the contract now, AFTER the boundary --
            #    its `contract_terminated` item must be stamped PLAYOFFS_R1
            #    (and its own real `terminated_at`), not REGULAR_SEASON/
            #    `created_at` (the bug blocking issue 1 found and this test
            #    would catch if it recurred).
            await contract_repo.terminate_contract(
                pool, terminated_contract.id, status=ContractStatus.WAIVED,
                terminated_in_season=_SEASON,
            )
            terminated_contract = await contract_repo.get_contract(
                pool, terminated_contract.id
            )
            assert terminated_contract is not None
            assert terminated_contract.terminated_at is not None

            feed = await view_service.get_league_transactions_feed(
                pool, league_id=league_id, season=_SEASON
            )
            by_kind = {item.kind: item for item in feed}
            assert set(by_kind) == {
                "phase_advance", "trade", "manager_assigned", "manager_removed",
                "draft_selection", "contract_signed", "contract_terminated",
            }

            _regular = view_service._PHASE_ORDINAL[Phase.REGULAR_SEASON]
            _playoffs = view_service._PHASE_ORDINAL[Phase.PLAYOFFS_R1]

            # Real, hand-verified expected (season, phase) per item --
            # never re-derived from "the list is sorted," which proves
            # nothing about whether the DERIVATION (the actual risk §22.5
            # names) is correct.
            assert by_kind["trade"].season == _SEASON
            assert by_kind["trade"].phase_ordinal == _regular
            assert by_kind["manager_assigned"].season == _SEASON
            assert by_kind["manager_assigned"].phase_ordinal == _regular
            assert by_kind["draft_selection"].season == _SEASON
            assert (
                by_kind["draft_selection"].phase_ordinal
                == view_service._PHASE_ORDINAL[Phase.DRAFT]
            )
            assert by_kind["contract_signed"].season == _SEASON
            assert by_kind["contract_signed"].phase_ordinal == _regular

            # The two boundary-crossing items -- the ones blocking issue 1
            # and issue 2 both named -- must land AFTER the boundary, at
            # PLAYOFFS_R1, strictly after every REGULAR_SEASON item's own
            # `phase_ordinal`.
            assert by_kind["manager_removed"].season == _SEASON
            assert by_kind["manager_removed"].phase_ordinal == _playoffs
            assert by_kind["manager_removed"].phase_ordinal > _regular
            assert by_kind["contract_terminated"].season == _SEASON
            assert by_kind["contract_terminated"].phase_ordinal == _playoffs
            assert by_kind["contract_terminated"].phase_ordinal > _regular
            # And its `occurred_at` is the real `terminated_at`, never the
            # contract's `created_at` (the signing timestamp) -- the exact
            # value blocking issue 1 found stamped in its place.
            assert by_kind["contract_terminated"].occurred_at == terminated_contract.terminated_at
            assert by_kind["contract_terminated"].occurred_at != terminated_contract.created_at

            # Final list is still ordered ascending by phase_ordinal -- a
            # real property of the merge's output, checked IN ADDITION to
            # (never instead of) the per-item derivation checks above.
            assert all(
                feed[i].phase_ordinal <= feed[i + 1].phase_ordinal
                for i in range(len(feed) - 1)
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_league_transactions_feed_skips_a_terminated_contract_with_no_terminated_at():
    """A contract terminated before migration 015 added `terminated_at`
    has no real termination instant to sort a `contract_terminated` item
    on -- `get_league_transactions_feed`'s own disclosed fix for blocking
    issue 1 is to SKIP such a row, not to fall back to `created_at` (the
    signing timestamp), which is the exact bug the fix exists to close.
    Reproduced directly by writing `terminated_in_season` without ever
    calling `terminate_contract` (the only real write path that stamps
    `terminated_at`), the same shape a contract terminated on a pre-
    migration-015 database would actually have."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "OLD")
            player_id = await _make_player(pool, league_id, team_id)
            await league_repo.record_phase_transition(
                pool, league_id=league_id, season=_SEASON, from_phase=Phase.SETUP,
                to_phase=Phase.REGULAR_SEASON, actor_user_id=1, forced=False, reason=None,
            )
            # Directly writes terminated_in_season without terminated_at --
            # simulates a row that predates migration 015.
            await contract_repo.create_contract(
                pool, league_id=league_id, player_id=player_id, team_id=team_id,
                signed_in_season=_SEASON - 1, start_season=_SEASON - 1, end_season=_SEASON,
                total_years=2, contract_type=ContractType.STANDARD,
                signing_method=SigningMethod.CAP_SPACE, terminated_in_season=_SEASON,
            )

            feed = await view_service.get_league_transactions_feed(
                pool, league_id=league_id, season=_SEASON
            )
            kinds = {item.kind for item in feed}
            assert "contract_terminated" not in kinds
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_league_transactions_feed_excludes_other_seasons():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await _make_team(pool, league_id, "OLD")
            await league_repo.record_phase_transition(
                pool, league_id=league_id, season=_SEASON - 1, from_phase=Phase.SETUP,
                to_phase=Phase.REGULAR_SEASON, actor_user_id=1, forced=False, reason=None,
            )
            feed = await view_service.get_league_transactions_feed(
                pool, league_id=league_id, season=_SEASON
            )
            assert feed == ()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
