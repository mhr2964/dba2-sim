"""`presentation/boxscore.py`'s own tests -- Phase 5 Stage B step 6 (docs/
design/phase-5-discord-presentation.md §11, §7.1's `/box` row, §9.1's
golden files, §9.2's limit invariants). No DB, no Discord -- every
`dba2.domain` dataclass here is constructed by hand; `view_service`'s own
integration test proves the real repository reads that feed them."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from dba2.domain.game import Game, GameStatus, GameType
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.stats import GamePeriodScore, PlayerGameStats, TeamGameStats
from dba2.domain.team import GmPhilosophy, Team, Timeline, TimelineSource
from dba2.presentation.boxscore import build_boxscore_panel
from tests.unit.presentation._golden_helpers import load_golden, panel_to_dict


def _team(team_id: int, code: str) -> Team:
    return Team(
        id=team_id, league_id=1, code=code, city=f"City{team_id}", name=f"Team{team_id}",
        conference="East", division="Atlantic", manager_user_id=None,
        gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.WIN_NOW, home_market_size=5,
        timeline_source=TimelineSource.AUTO,
    )


def _player(player_id: int, team_id: int) -> Player:
    return Player(
        id=player_id, league_id=1, first_name=f"F{player_id}", last_name=f"L{player_id}",
        external_ref=None, primary_position=Position.SF, secondary_position=None,
        height_in=78, weight_lb=210, wingspan_in=80, birth_date=date(1998, 1, 1),
        draft_year=None, draft_pick_number=None, years_pro=5, team_id=team_id,
        roster_status=RosterStatus.ACTIVE, overall_cached=80,
        overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC), retired_in_season=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _player_stats(player_id: int, team_id: int, *, points: int, minutes: str) -> PlayerGameStats:
    return PlayerGameStats(
        points=points, fga_rim=6, fgm_rim=4, fga_mid=4, fgm_mid=2, fga_three=5, fgm_three=2,
        fta=4, ftm=3, oreb=1, dreb=5, ast=4, stl=1, blk=1, tov=2, pf=2,
        game_id=1, league_id=1, player_id=player_id, team_id=team_id,
        minutes_played=Decimal(minutes), plus_minus=6,
    )


def _team_stats(team_id: int) -> TeamGameStats:
    return TeamGameStats(
        points=108, fga_rim=36, fgm_rim=20, fga_mid=24, fgm_mid=10, fga_three=25, fgm_three=10,
        fta=20, ftm=16, oreb=8, dreb=32, ast=24, stl=6, blk=4, tov=12, pf=18,
        game_id=1, league_id=1, team_id=team_id, possessions=98,
    )


def _period_scores() -> list[GamePeriodScore]:
    return [
        GamePeriodScore(game_id=1, league_id=1, team_id=1, period=p, points=27)
        for p in (1, 2, 3, 4)
    ] + [
        GamePeriodScore(game_id=1, league_id=1, team_id=2, period=p, points=26)
        for p in (1, 2, 3, 4)
    ]


def _game() -> Game:
    return Game(
        id=1, league_id=1, season=2026, game_index=1, game_date=date(2026, 1, 1),
        home_team_id=1, away_team_id=2, home_score=108, away_score=104,
        status=GameStatus.FINAL, game_type=GameType.REGULAR, rng_seed=1,
        simmed_at=datetime(2026, 1, 1, tzinfo=UTC), engine_version=1, roster_digest=None,
        overtime_periods=0,
    )


def _fixture_panel():
    home = _team(1, "HOM")
    away = _team(2, "AWY")
    home_stats = [_player_stats(1, 1, points=24, minutes="34.5")]
    away_stats = [_player_stats(2, 2, points=20, minutes="30.1")]
    players = {1: _player(1, 1), 2: _player(2, 2)}
    return build_boxscore_panel(
        game=_game(), home_team=home, away_team=away, home_player_stats=home_stats,
        away_player_stats=away_stats, home_team_stats=_team_stats(1),
        away_team_stats=_team_stats(2), period_scores=_period_scores(), players=players,
    )


def test_boxscore_panel_matches_committed_golden_element_wise():
    panel = _fixture_panel()
    assert panel_to_dict(panel) == load_golden("presentation_boxscore.json")


def test_boxscore_panel_title_shows_real_scores_from_game_row():
    panel = _fixture_panel()
    assert "108" in panel.title
    assert "104" in panel.title


def test_boxscore_panel_missing_player_falls_back_to_placeholder_name():
    """"absent is neutral" (this project's standing convention) -- a stat
    line for a player id with no entry in `players` still renders, as
    `Player <id>`, rather than crashing."""
    home = _team(1, "HOM")
    away = _team(2, "AWY")
    home_stats = [_player_stats(99, 1, points=24, minutes="34.5")]
    panel = build_boxscore_panel(
        game=_game(), home_team=home, away_team=away, home_player_stats=home_stats,
        away_player_stats=[], home_team_stats=_team_stats(1), away_team_stats=_team_stats(2),
        period_scores=_period_scores(), players={},
    )
    assert "Player 99" in panel.description


def test_boxscore_panel_thirty_players_max_length_names_and_seven_ot_stays_within_limits():
    """§9.2's adversarial corpus: a 15-man box score for BOTH teams, and a
    7-overtime game -- the two cases §9.2 names by example, combined."""
    home = _team(1, "HOM")
    away = _team(2, "AWY")

    def make_stats(offset: int, team_id: int) -> list[PlayerGameStats]:
        return [_player_stats(offset + i, team_id, points=20, minutes="30.0") for i in range(15)]

    players = {
        pid: Player(
            id=pid, league_id=1, first_name="F" * 40, last_name="L" * 40, external_ref=None,
            primary_position=Position.SF, secondary_position=None, height_in=78,
            weight_lb=210, wingspan_in=80, birth_date=date(1998, 1, 1), draft_year=None,
            draft_pick_number=None, years_pro=5, team_id=1, roster_status=RosterStatus.ACTIVE,
            overall_cached=80, overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            retired_in_season=None, created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for pid in list(range(1, 16)) + list(range(101, 116))
    }
    periods = [
        GamePeriodScore(game_id=1, league_id=1, team_id=tid, period=p, points=10)
        for tid in (1, 2)
        for p in range(1, 12)  # 4 regulation + 7 OT
    ]
    panel = build_boxscore_panel(
        game=_game(), home_team=home, away_team=away,
        home_player_stats=make_stats(1, 1), away_player_stats=make_stats(101, 2),
        home_team_stats=_team_stats(1), away_team_stats=_team_stats(2),
        period_scores=periods, players=players,
    )
    assert len(panel.description) <= 4096
    for field in panel.fields:
        assert len(field.value) <= 1024
