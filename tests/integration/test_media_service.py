"""Live I/O proof for `services/media_service.py` -- Phase 4 §11 Stage D
steps 17 (Decisions P4-27/P4-28/P4-29/P4-30) and 18 (Decisions P4-22/P4-26/
P4-38/P4-54). The pure diff/scoring/selection/binding logic already has
exhaustive coverage in `tests/unit/test_media_pulse.py`/`test_media_
events.py`/`test_media_salience.py`/`test_media_select.py`; this file
proves only the I/O seams -- real `games`-sourced standings/streak
(Decision P4-30, never `window.py`), real `player_motivation_events`-
sourced unrest intake (Decision P4-27) through `dba2/services/
agency_service.py`'s read-model, and (step 18's own addition) real
`team_repo.get_team`/`player_repo.get_player`-sourced NAME resolution for
slot binding (Decision P4-22).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as `test_smoke.py`.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import (
    draft_repo,
    game_repo,
    league_repo,
    media_repo,
    motivation_repo,
    player_repo,
    stats_repo,
    team_repo,
    txn_repo,
)
from dba2.data.repositories.stats_repo import _BOX_SCORE_COLUMN_NAMES
from dba2.domain.game import GameStatus, GameType
from dba2.domain.media_facts import StoryPackage
from dba2.domain.motivation import MarketPreference, MotivationAxis, RequestState
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.media import salience
from dba2.media.detect import legacy
from dba2.media.frame import InsufficientDataReason, StoryCandidate, StoryKind, derive_frame
from dba2.media.salience import SalienceCandidate, ScoredCandidate
from dba2.services import agency_service, game_sim_service, gm_service, media_service, stats_service
from dba2.services.media_service import PhaseCadence
from tests.integration._press_fixtures import drop, sim_league_at

_SEASON = 2026


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Media Service Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=99,
    )
    return league.id


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
        gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Media",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1997, 1, 1),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=70,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _play_final_game(
    pool, *, league_id, season, game_index, home_team_id, away_team_id, home_won
):
    home_score, away_score = (110, 100) if home_won else (100, 110)
    await game_repo.create_game(
        pool,
        league_id=league_id,
        season=season,
        game_index=game_index,
        game_date=date(season, 11, 1 + game_index),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_type=GameType.REGULAR,
        rng_seed=game_index,
        status=GameStatus.FINAL,
        home_score=home_score,
        away_score=away_score,
    )


@pytest.mark.asyncio
async def test_compute_current_pulse_snapshot_reads_standings_and_streak_from_games():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            winner = await _make_team(pool, league_id, "WW")
            loser = await _make_team(pool, league_id, "LL")
            fresh = await _make_team(pool, league_id, "FF")  # zero games played

            for i in range(5):
                await _play_final_game(
                    pool,
                    league_id=league_id,
                    season=_SEASON,
                    game_index=i,
                    home_team_id=winner,
                    away_team_id=loser,
                    home_won=True,
                )

            snapshot = await media_service.compute_current_pulse_snapshot(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )

            assert snapshot.teams[winner].wins == 5
            assert snapshot.teams[winner].losses == 0
            assert snapshot.teams[winner].streak_length == 5
            assert snapshot.teams[winner].standings_position == 1

            assert snapshot.teams[loser].wins == 0
            assert snapshot.teams[loser].losses == 5
            assert snapshot.teams[loser].streak_length == -5

            # Decision P4-24's "team in game 0" case: present, zero games,
            # never absent.
            assert snapshot.teams[fresh].wins == 0
            assert snapshot.teams[fresh].losses == 0
            assert snapshot.teams[fresh].games_played == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_pulse_cycle_first_cycle_has_no_continuous_candidates():
    """Decision P4-28, proven against real I/O: the very first cycle a
    league ever runs has no prior snapshot, so the continuous side
    contributes nothing -- never a candidate computed against a fabricated
    zero-win baseline."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AA")
            team_b = await _make_team(pool, league_id, "BB")
            for i in range(6):
                await _play_final_game(
                    pool,
                    league_id=league_id,
                    season=_SEASON,
                    game_index=i,
                    home_team_id=team_a,
                    away_team_id=team_b,
                    home_won=True,
                )

            result = await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            snapshot, candidates = result.snapshot, result.candidates
            assert snapshot.teams[team_a].wins == 6
            # docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step
            # 3b: `run_media_pulse_cycle` no longer scores -- `candidates`
            # is the raw, unscored `SalienceCandidate` tuple, empty here
            # for the same reason it was empty pre-split (no previous
            # snapshot, no motivation events).
            assert candidates == ()

            # Stage D step 19: the current snapshot is durably persisted by
            # the time this function returns -- a caller no longer has to
            # store it anywhere itself.
            persisted = await media_repo.get_pulse_snapshot_teams(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert len(persisted) == 2
            by_team = {row.team_id: row for row in persisted}
            assert by_team[team_a].wins == 6
            assert by_team[team_a].losses == 0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_pulse_cycle_second_cycle_produces_real_scored_candidates():
    """Round 10 critic fix (B5), corrected by round 11's re-verification:
    the only prior test of `run_media_pulse_cycle` passed `previous_
    snapshot=None` and asserted an empty result -- real coverage of the
    function's headline job (a real PRIOR snapshot producing non-empty,
    correctly-signed candidates) did not exist.

    **Round 11 fix.** This test's first version used `+4 -> -6`/`-4 -> +6`
    -- both cases where the reversed streak's magnitude also GROWS, which
    the PRE-FIX comparison (`abs(streak) > abs(previous_streak)`) already
    caught on its own (6 > 4), so it never exercised the reversal-at-
    equal-magnitude case B2 actually fixed. The critic proved this by
    reverting B2 in `dba2/media/pulse.py` and re-running this test -- it
    stayed green. This version uses `+5 -> -5`/`-5 -> +5` instead --
    equal magnitude on both sides -- which the pre-fix comparison reads as
    zero growth (`5 > 5` is `False`) and therefore misses entirely; only
    the sign-flip half of B2's fix catches it. Four teams, real games:
    `AA` goes 5-3 (a 3-loss cushion, then a 5-game win streak) before
    reversing into a 5-game loss streak against `DD`, who mirrors it in
    reverse (3-5 cushion, then a 5-game LOSS streak reversing into a
    5-game WIN streak) -- proven through the real DB seam, not just the
    pure `pulse.py` unit tests. The cushion games keep the season-long
    win pct flip (AA 1st -> 4th, DD 4th -> 1st) intact despite the
    streak-only games now being equal-magnitude on both sides. `BB`/`CC`
    play only in the first cycle and hold steady, proving they produce no
    false candidate from a cycle where they play no further games."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AA")
            team_b = await _make_team(pool, league_id, "BB")
            team_c = await _make_team(pool, league_id, "CC")
            team_d = await _make_team(pool, league_id, "DD")

            # Cycle 0: AA-vs-DD (game_index 0-7) -- DD wins games 0-2 (a
            # cushion, not part of the measured streak), AA wins games 3-7
            # -> AA 5-3 (streak +5), DD 3-5 (streak -5).
            for i in range(3):
                await _play_final_game(
                    pool, league_id=league_id, season=_SEASON, game_index=i,
                    home_team_id=team_a, away_team_id=team_d, home_won=False,
                )
            for i in range(3, 8):
                await _play_final_game(
                    pool, league_id=league_id, season=_SEASON, game_index=i,
                    home_team_id=team_a, away_team_id=team_d, home_won=True,
                )
            # BB-vs-CC (game_index 20-24, a disjoint range so it never
            # interleaves with AA/DD's own chronological ordering) -- BB
            # wins 3, CC wins 2. Neither team plays again after cycle 0.
            for i in range(3):
                await _play_final_game(
                    pool, league_id=league_id, season=_SEASON, game_index=20 + i,
                    home_team_id=team_b, away_team_id=team_c, home_won=True,
                )
            for i in range(3, 5):
                await _play_final_game(
                    pool, league_id=league_id, season=_SEASON, game_index=20 + i,
                    home_team_id=team_b, away_team_id=team_c, home_won=False,
                )

            first_cycle_result = await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            previous_snapshot = first_cycle_result.snapshot
            # docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step
            # 3b: `run_media_pulse_cycle` no longer scores -- `candidates`
            # is unscored, empty here for the same reason it always was
            # (Decision P4-28: no prior snapshot yet).
            assert first_cycle_result.candidates == ()
            assert previous_snapshot.teams[team_a].standings_position == 1
            assert previous_snapshot.teams[team_d].standings_position == 4

            # Cycle 1: DD wins the next 5 straight against AA (game_index
            # 8-12) -- AA's +5 win streak reverses into a -5 loss streak
            # at the SAME magnitude (not a growing one -- the exact case
            # B2 fixed and the pre-fix code missed, see this test's own
            # docstring), DD's -5 loss streak reverses into a +5 win
            # streak the same way. The standings still fully flip (AA 1st
            # -> 4th, DD 4th -> 1st): AA ends the season 5-8 (0.3846), DD
            # ends 8-5 (0.6154), which still bracket BB's steady 0.600
            # and CC's steady 0.400 from the opposite sides they started
            # on, exactly as the pre-round-11 4-1/1-4 cushion did.
            for i in range(8, 13):
                await _play_final_game(
                    pool, league_id=league_id, season=_SEASON, game_index=i,
                    home_team_id=team_a, away_team_id=team_d, home_won=False,
                )

            cycle_1_result = await media_service.run_media_pulse_cycle(
                pool,
                league_id=league_id,
                season=_SEASON,
                cycle_index=1,
            )
            current_snapshot, candidates = cycle_1_result.snapshot, cycle_1_result.candidates

            assert current_snapshot.teams[team_a].standings_position == 4
            assert current_snapshot.teams[team_d].standings_position == 1
            assert current_snapshot.teams[team_a].streak_length == -5
            assert current_snapshot.teams[team_d].streak_length == 5
            # BB/CC play no further games this cycle and must not move.
            assert current_snapshot.teams[team_b].standings_position == 2
            assert current_snapshot.teams[team_c].standings_position == 3

            assert len(candidates) == 4  # 2 standings + 2 streak, one pair per team
            by_kind_and_team = {
                (c.package.story_kind, c.package.subject.team_id): c for c in candidates
            }
            collapse = by_kind_and_team[(StoryKind.STANDINGS_COLLAPSE, team_a)]
            assert collapse.package.number("standings_position_delta") == 3
            surge = by_kind_and_team[(StoryKind.STANDINGS_SURGE, team_d)]
            assert surge.package.number("standings_position_delta") == -3
            a_streak = by_kind_and_team[(StoryKind.STREAK, team_a)]
            assert a_streak.package.number("streak_length") == -5
            d_streak = by_kind_and_team[(StoryKind.STREAK, team_d)]
            assert d_streak.package.number("streak_length") == 5

            # docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step
            # 3b: scoring is now `run_media_selection_cycle`'s own act, not
            # this function's -- proven here by scoring `candidates`
            # directly with the same pure `salience.score_candidates` that
            # function calls, rather than reading a `top_candidates` field
            # this result no longer carries.
            scored = salience.score_candidates(candidates)
            assert all(0.0 <= s.score <= 1.0 for s in scored)
            assert len(salience.above_floor(scored)) == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_player_unrest_candidates_reads_real_ladder_transition_events():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CC")
            player_id = await _make_player(pool, league_id, team_id)

            await motivation_repo.create_player_motivation(
                pool,
                player_id=player_id,
                league_id=league_id,
                weight_winning=20,
                weight_role_usage=20,
                weight_loyalty=20,
                weight_money=20,
                weight_legacy=20,
                hometown_team_id=None,
                market_preference=MarketPreference.BIG_MARKET,
                star_leverage=80,
                satisfaction_winning=-50,
                satisfaction_role_usage=-50,
                satisfaction_loyalty=-50,
                satisfaction_money=-50,
                satisfaction_legacy=-50,
                morale=-60,
                trust_in_management=0,
                request_state=RequestState.UNSETTLED,
                request_opened_in_season=_SEASON,
                last_evaluated_season=_SEASON,
            )
            # The event log's own real writer shape (`run_agency_checkpoint`):
            # a `ladder_transition` marker row, delta 0, real `axis` column.
            await motivation_repo.create_motivation_event(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=_SEASON,
                phase=Phase.TRADE_DEADLINE,
                event_type=agency_service.LADDER_TRANSITION_EVENT_TYPE,
                axis=MotivationAxis.WINNING,
                delta=0,
                note="content->unsettled",
            )

            candidates = await media_service.load_player_unrest_candidates(
                pool, league_id=league_id, season=_SEASON
            )
            assert len(candidates) == 1
            candidate = candidates[0]
            assert candidate.kind is StoryKind.PLAYER_UNREST
            assert candidate.player_id == player_id
            assert candidate.motivation_axis is MotivationAxis.WINNING
            assert candidate.request_state is RequestState.UNSETTLED
            assert candidate.motivation_magnitude == 60  # round(-(-60))
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_player_unrest_candidates_empty_when_no_events_this_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            candidates = await media_service.load_player_unrest_candidates(
                pool, league_id=league_id, season=_SEASON
            )
            assert candidates == ()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Stage D step 21 -- Decision P4-107 item 1, `load_rookie_watch_candidates`
# (a field-for-field copy of `load_player_unrest_candidates`'s own shape,
# reading `draft_repo` + `stats_repo` instead of `motivation_repo`).
# ---------------------------------------------------------------------------


async def _make_draft_selection(pool, *, league_id, season, team_id, player_id, round_=1):
    draft = await draft_repo.get_draft_for_season(pool, league_id, season)
    if draft is None:
        draft = await draft_repo.create_draft(
            pool, league_id=league_id, season=season, total_picks=60
        )
    pick = await draft_repo.create_draft_pick(
        pool, league_id=league_id, season=season, round=round_,
        original_team_id=team_id, current_team_id=team_id, pick_number=1,
    )
    return await draft_repo.create_draft_selection(
        pool, draft_id=draft.id, pick_number=1, round=round_,
        team_id=team_id, player_id=player_id, pick_asset_id=pick.id,
    )


def _zero_box() -> dict[str, int]:
    return {name: 0 for name in _BOX_SCORE_COLUMN_NAMES}


@pytest.mark.asyncio
async def test_load_rookie_watch_candidates_reads_prior_season_draft_with_real_games_played():
    """Decision P4-107 item 1's own headline case: a player drafted in
    `_SEASON - 1` who has real, materialized `player_season_stats` for
    `_SEASON` (the rebuilt aggregate `stats_service.rebuild_player_season_
    stats` writes once a season is complete) reads a real, non-`None`
    `player_games_played_this_season` -- and, proven all the way through
    `derive_frame` rather than merely asserted about the candidate's own
    field, is SUFFICIENT (not `NO_GAMES_PLAYED`/`EARLY_SEASON_SAMPLE`)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RW")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_draft_selection(
                pool, league_id=league_id, season=_SEASON - 1,
                team_id=team_id, player_id=player_id,
            )
            await stats_repo.upsert_player_season_stats(
                pool, league_id=league_id, player_id=player_id, season=_SEASON,
                game_type=GameType.REGULAR, games_played=40,
                minutes_played=Decimal("900.0"), box=_zero_box(),
            )

            candidates = await media_service.load_rookie_watch_candidates(
                pool, league_id=league_id, season=_SEASON
            )
            assert len(candidates) == 1
            candidate = candidates[0]
            assert candidate.kind is StoryKind.ROOKIE_WATCH
            assert candidate.player_id == player_id
            assert candidate.team_id == team_id
            assert candidate.player_games_played_this_season == 40

            frame = derive_frame(candidate)
            assert frame.insufficient_data is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_rookie_watch_candidates_none_games_played_when_no_stats_row_yet():
    """No `player_season_stats` row for `_SEASON` yet (the season's own
    stats rebuild has not run) -- a real, disclosed `None`, never a
    fabricated pass, and `derive_frame` correctly reads it as
    `NO_GAMES_PLAYED`-insufficient, dropped before scoring."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RN")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_draft_selection(
                pool, league_id=league_id, season=_SEASON - 1,
                team_id=team_id, player_id=player_id,
            )

            candidates = await media_service.load_rookie_watch_candidates(
                pool, league_id=league_id, season=_SEASON
            )
            assert len(candidates) == 1
            assert candidates[0].player_games_played_this_season is None

            frame = derive_frame(candidates[0])
            assert frame.insufficient_data is InsufficientDataReason.NO_GAMES_PLAYED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_rookie_watch_candidates_empty_when_no_prior_season_draft():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            candidates = await media_service.load_rookie_watch_candidates(
                pool, league_id=league_id, season=_SEASON
            )
            assert candidates == ()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_pulse_cycle_includes_rookie_watch_candidates_in_its_union():
    """The real wiring proof: `run_media_pulse_cycle`'s own candidate union
    (Decision P4-106 register item 14's remedy) now includes rookie_watch,
    not merely `load_rookie_watch_candidates` in isolation."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RC")
            player_id = await _make_player(pool, league_id, team_id)
            await _make_draft_selection(
                pool, league_id=league_id, season=_SEASON - 1,
                team_id=team_id, player_id=player_id,
            )
            await stats_repo.upsert_player_season_stats(
                pool, league_id=league_id, player_id=player_id, season=_SEASON,
                game_type=GameType.REGULAR, games_played=25,
                minutes_played=Decimal("600.0"), box=_zero_box(),
            )

            result = await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            kinds = {c.package.story_kind for c in result.candidates}
            assert StoryKind.ROOKIE_WATCH in kinds
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Stage D step 18 -- select_and_bind_story's own I/O seam: real team/player
# NAME resolution feeding real slot-binding resolution (Decision P4-22).
# ---------------------------------------------------------------------------


async def _sealed_package(
    pool,
    candidate: StoryCandidate,
    *,
    season: int = _SEASON,
    phase: Phase = Phase.REGULAR_SEASON,
    cycle_index: int = 0,
) -> StoryPackage:
    """docs/design/media-v2-newsroom.md, "Build order" step 3a: the real
    production path from a hand-built `StoryCandidate` to a sealed
    `StoryPackage` -- `derive_frame` -> `dba2/media/detect/legacy.py::
    adapt_frame` -> real `team_repo.get_team`/`player_repo.get_player`
    reads (mirroring `run_media_pulse_cycle`'s own bulk name resolution,
    at single-candidate scale) -> `media_service.seal_package`. Names are
    now resolved BEFORE `select_and_bind_story` ever runs (MV-1c.1), so
    every fixture that used to rely on that function's own `team_repo.
    get_team`/`player_repo.get_player` calls for slot values must resolve
    them here instead."""
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=season, phase=phase, cycle_index=cycle_index)
    assert draft is not None, f"candidate for {candidate.kind} was insufficient -- fix the fixture"

    team_names: dict[int, str] = {}
    if candidate.team_id is not None:
        team = await team_repo.get_team(pool, candidate.team_id)
        if team is not None:
            team_names[candidate.team_id] = f"{team.city} {team.name}"
    player_names: dict[int, str] = {}
    if candidate.player_id is not None:
        player = await player_repo.get_player(pool, candidate.player_id)
        if player is not None:
            player_names[candidate.player_id] = f"{player.first_name} {player.last_name}"

    sealed = media_service.seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None, f"candidate for {candidate.kind} failed to seal -- fix the fixture"
    return sealed


async def _scored_streak_candidate(pool, *, team_id: int, streak_length: int) -> ScoredCandidate:
    package = await _sealed_package(
        pool,
        StoryCandidate(
            kind=StoryKind.STREAK, team_id=team_id, team_games_played_this_season=20,
            has_pulse_snapshot=True, streak_length=streak_length,
        ),
    )
    return ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.9)


async def _scored_unrest_candidate(pool, *, player_id: int) -> ScoredCandidate:
    package = await _sealed_package(
        pool,
        StoryCandidate(
            kind=StoryKind.PLAYER_UNREST, player_id=player_id, motivation_magnitude=50,
            request_state=RequestState.TRADE_REQUESTED,
        ),
    )
    return ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.9)


@pytest.mark.asyncio
async def test_select_and_bind_story_resolves_a_real_team_name_into_the_slot_bindings():
    """Decision P4-22's own charge: a real slot value, not an id, sourced
    from a real `team_repo.get_team` read -- proven with a streak long
    enough (9) to reach `beat_writer_streak_win`'s own real trigger
    (`streak_length >= 8`) rather than only ever landing on a fallback."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PT")
            await pool.execute(
                "UPDATE teams SET name = $1, city = $2 WHERE id = $3",
                "Trail Blazers", "Portland", team_id,
            )
            library = media_service.load_media_library()
            scored = await _scored_streak_candidate(pool, team_id=team_id, streak_length=9)

            found_dedicated = False
            for seed in range(50):
                story = await media_service.select_and_bind_story(
                    pool, league_id=league_id, league_rng_seed=seed, season=_SEASON,
                    phase=Phase.REGULAR_SEASON, cycle_index=0, scored=scored, library=library,
                    use_counts={}, candidate_sequence=0,
                )
                assert story.slot_bindings["team_name"] == "Portland Trail Blazers"
                assert story.slot_bindings["team_name"] != str(team_id)
                if story.template_id == "beat_writer_streak_win":
                    found_dedicated = True
                    rendered = media_service.render_story(story, library)
                    assert rendered == "Portland Trail Blazers have won 9 in a row."
                    assert story.rendered_text == rendered
            assert found_dedicated, (
                "never drew the real, triggered dedicated template across 50 seeds"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_select_and_bind_story_resolves_a_real_player_name_into_the_slot_bindings():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PU")
            player_id = await _make_player(pool, league_id, team_id)
            await pool.execute(
                "UPDATE players SET first_name = $1, last_name = $2 WHERE id = $3",
                "Real", "Player", player_id,
            )
            library = media_service.load_media_library()
            scored = await _scored_unrest_candidate(pool, player_id=player_id)

            story = await media_service.select_and_bind_story(
                pool, league_id=league_id, league_rng_seed=1, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, scored=scored, library=library,
                use_counts={}, candidate_sequence=0,
            )
            assert story.slot_bindings["player_name"] == "Real Player"
            rendered = media_service.render_story(story, library)
            assert "Real Player" in rendered
            assert story.rendered_text == rendered
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_select_and_bind_story_degrades_gracefully_when_a_team_was_deleted():
    """Adversarial case, REWRITTEN for the retype (MV-8, revision 5, "Build
    order" step 3a -- names now resolve at SEAL time, before `select_and_
    bind_story` ever runs): the team row exists when the package is
    sealed, then is deleted before `select_and_bind_story` runs its own
    FK-safety re-check. The guarantee this test pins is UNCHANGED (never
    raise on a since-deleted subject); the exact rendered value is not --
    the sealed, REAL name renders (it was resolved while the row still
    existed), and `subject_team_id` is `NULL` (the FK-safety re-check finds
    the row gone). Swept across seeds so at least one draws `beat_writer`
    specifically, whose `writer_id` also goes `NULL` in that case (no
    other persona's writer identity depends on this team at all)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PZ")
            await pool.execute(
                "UPDATE teams SET name = $1, city = $2 WHERE id = $3",
                "Zephyrs", "Portland", team_id,
            )
            library = media_service.load_media_library()
            # Sealed BEFORE the delete -- the real name is resolved here,
            # while the row still exists.
            scored = await _scored_streak_candidate(pool, team_id=team_id, streak_length=6)

            await pool.execute("DELETE FROM teams WHERE id = $1", team_id)

            saw_beat_writer = False
            for seed in range(30):
                story = await media_service.select_and_bind_story(
                    pool, league_id=league_id, league_rng_seed=seed, season=_SEASON,
                    phase=Phase.REGULAR_SEASON, cycle_index=0, scored=scored, library=library,
                    use_counts={}, candidate_sequence=0,
                )
                assert story.slot_bindings["team_name"] == "Portland Zephyrs", (
                    "the SEALED (real, pre-delete) name must still render"
                )
                assert story.subject_team_id is None, (
                    "the FK-safety re-check must find the row gone and persist NULL"
                )
                if story.voice == "beat_writer":
                    saw_beat_writer = True
                    assert story.writer_id is None, (
                        "beat_writer's own team-scoped writer cannot be resolved once the "
                        "team is gone -- persisted UNATTRIBUTED, never a dangling reference"
                    )
                media_service.render_story(story, library)  # must not raise
            assert saw_beat_writer, "never drew beat_writer across 30 seeds -- widen the sweep"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Round 12 critic fix (blocking finding 1) -- Decision P4-88's mandatory
# collision test (P4-54's own "a collision test is required, not optional"
# rule): two candidates sharing one cycle's (kind, subject) must never draw
# from the same RNG stream, including the case where their StoryFrames are
# byte-identical (StoryFrame carries only trade-asset COUNTS, never ids, so
# two different trades for the same team in one trade window frame-collide
# exactly as well as salt-collide).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_and_bind_story_does_not_collide_for_two_same_subject_candidates_in_one_cycle(
    monkeypatch,
):
    """Reproduces, then proves fixed, the exact scenario the critic
    verified: `dba2/media/events.py::trade_execution_candidate` is per
    `(team, trade)`, so a team executing two trades in the same trade
    window yields two `TRADE_REACTION` `StoryCandidate`s with an identical
    `team_id` -- and since `StoryFrame` derives only asset COUNTS
    (`trade_in_players`/`trade_out_players`/...), the two frames below are
    byte-identical to each other (both one-in-one-out), which is exactly
    what made the pre-fix salts (ending at `(kind, subject_kind,
    subject_id)` with no further discriminant) collide byte-identically on
    every seed. `candidate_sequence` (Decision P4-88) is the fix; this test
    would fail (both `recorded_salts` entries duplicated, and every sampled
    seed producing identical `(voice, template_id)` pairs) if `candidate_
    sequence` were dropped from the salt tuple, reverting to the pre-fix
    shape."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TC")
            library = media_service.load_media_library()

            package_a = await _sealed_package(
                pool,
                StoryCandidate(
                    kind=StoryKind.TRADE_REACTION, team_id=team_id,
                    trade_in_player_ids=(101,), trade_out_player_ids=(102,),
                ),
            )
            package_b = await _sealed_package(
                pool,
                StoryCandidate(
                    kind=StoryKind.TRADE_REACTION, team_id=team_id,
                    trade_in_player_ids=(201,), trade_out_player_ids=(202,),
                ),
            )
            assert package_a == package_b, (
                "fixture invariant: both packages must be byte-identical (same team, same "
                "one-in-one-out COUNTS, the only trade facts this kind's registry declares) "
                "-- otherwise this is not the collision case at all"
            )
            scored_a = ScoredCandidate(candidate=SalienceCandidate(package=package_a), score=0.9)
            scored_b = ScoredCandidate(candidate=SalienceCandidate(package=package_b), score=0.9)

            real_rng_for = media_service.rng_for
            recorded_salts: list[tuple[object, ...]] = []

            def _recording_rng_for(seed, *salt):
                recorded_salts.append(salt)
                return real_rng_for(seed, *salt)

            monkeypatch.setattr(media_service, "rng_for", _recording_rng_for)

            distinct_pairs_seen = set()
            for seed in range(20):
                recorded_salts.clear()
                selection_a = await media_service.select_and_bind_story(
                    pool, league_id=league_id, league_rng_seed=seed, season=_SEASON,
                    phase=Phase.REGULAR_SEASON, cycle_index=0, scored=scored_a, library=library,
                    use_counts={}, candidate_sequence=0,
                )
                selection_b = await media_service.select_and_bind_story(
                    pool, league_id=league_id, league_rng_seed=seed, season=_SEASON,
                    phase=Phase.REGULAR_SEASON, cycle_index=0, scored=scored_b, library=library,
                    use_counts={}, candidate_sequence=1,
                )

                # The structural guarantee P4-54/P4-88 actually require:
                # all four salt tuples drawn this "cycle" (persona + template,
                # per candidate) are pairwise distinct -- true by construction
                # once `candidate_sequence` differs, and this is the direct
                # regression check for the pre-fix collision (which produced
                # only two DISTINCT salts across all four draws: `persona`
                # collided with `persona`, `story` collided with `story`).
                # **Step 19 addition**: `select_and_bind_story` now also
                # lazily get-or-creates each candidate's writer, which draws
                # a THIRD-labeled ("writer_name") salt the first time a given
                # (league, team, persona) triple is ever seen -- filtered out
                # here rather than folded into the count, because that draw
                # is deliberately NOT part of P4-88's collision surface (its
                # own salt has no candidate_sequence/subject component at
                # all, Decision P4-54's own "generated once, stable for the
                # league's life" rule) and it is idempotent across this
                # loop's own 20 seeds (a writer created on an early seed is
                # simply re-read, not re-drawn, on every later one).
                media_salts = [s for s in recorded_salts if s[1] in ("persona", "story")]
                assert len(media_salts) == len(set(media_salts)) == 4, (
                    f"seed {seed}: expected 4 distinct persona/story RNG salts, "
                    f"got {media_salts} (full recorded_salts: {recorded_salts})"
                )
                distinct_pairs_seen.add(
                    (
                        (selection_a.voice, selection_a.template_id),
                        (selection_b.voice, selection_b.template_id),
                    )
                )

            # The real, behavioral proof: pre-fix, EVERY seed produced an
            # identical (voice, template_id) pair for both candidates (the
            # streams were the same stream). Post-fix, at least one sampled
            # seed must produce a genuine divergence -- if every one of the
            # 20 sampled seeds still agreed, the fix would not actually be
            # separating the two streams in practice, only on paper.
            diverged = any(pair_a != pair_b for pair_a, pair_b in distinct_pairs_seen)
            assert diverged, (
                "every sampled seed drew an identical (voice, template_id) pair for both "
                "same-subject candidates -- candidate_sequence is not actually separating "
                "the two RNG streams"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Stage D step 19 -- get_or_create_media_writer (Decision P4-38's own
# lazy-identity contract) and run_media_selection_cycle (Decision P4-88's
# single per-cycle candidate_sequence loop).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_media_writer_creates_once_and_reuses_the_same_row():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "GW")

            first = await media_service.get_or_create_media_writer(
                pool, league_rng_seed=1, league_id=league_id, team_id=team_id,
                persona="beat_writer",
            )
            second = await media_service.get_or_create_media_writer(
                pool, league_rng_seed=1, league_id=league_id, team_id=team_id,
                persona="beat_writer",
            )
            assert first.id == second.id
            assert first.name == second.name

            rows = await media_repo.list_media_writers_for_league(pool, league_id)
            assert len(rows) == 1

            # A DIFFERENT league_rng_seed must not regenerate a new name for
            # an EXISTING writer -- Decision P4-38's "generated once, stable
            # for the league's life," proven against the real get-or-create
            # path, not just the pure salt-formula docstring.
            third = await media_service.get_or_create_media_writer(
                pool, league_rng_seed=999, league_id=league_id, team_id=team_id,
                persona="beat_writer",
            )
            assert third.id == first.id
            assert third.name == first.name
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_or_create_media_writer_league_wide_ignores_team_id():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            writer = await media_service.get_or_create_media_writer(
                pool, league_rng_seed=1, league_id=league_id, team_id=None, persona="hot_take"
            )
            assert writer.team_id is None
            assert writer.persona == "hot_take"

            rows = await media_repo.list_media_writers_for_league(pool, league_id)
            assert len(rows) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Round 14 critic fix (blocking finding 2) -- `get_or_create_media_writer`'s
# race-recovery path must actually work when `ex` is a `Connection` already
# inside an open transaction (the multi-repo-in-one-transaction case, e.g. a
# trade's writes across four repos, `db.py`'s own module docstring), not
# only when `ex` is a bare `Pool`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_media_writer_race_survives_shared_transaction(monkeypatch):
    """Forces the exact race the critic reproduced: `get_or_create_media_
    writer` is called with a `Connection` (`conn`) that is already inside an
    open `transaction(pool)` block -- standing in for a caller composing
    this call into a larger atomic unit of work, exactly the shape `db.py`'s
    own docstring exists to support. Between this call's internal read
    (a miss) and its insert, a raw INSERT for the identical `(league_id,
    team_id, persona)` triple is committed on a SEPARATE connection (the
    "other racer," already committed by the time this call's own insert
    runs) -- migration 011's own partial unique index turns this call's
    insert into a REAL `asyncpg.UniqueViolationError`, raised from inside
    `conn`'s own open transaction, not a synthetic/mocked one.

    Pre-fix, that real `UniqueViolationError` aborts the entire surrounding
    transaction (`conn` has no savepoint isolating this call's own insert),
    so the compensating re-read this function does next raises `asyncpg.
    InFailedSQLTransactionError` instead of returning the winner -- exactly
    the critic's own reproduction. Post-fix (the insert now runs inside its
    own `db.py::transaction(ex)` scope, which nests via `SAVEPOINT` when
    `ex` is already a `Connection` inside a transaction), the violation only
    unwinds to the savepoint, `conn`'s outer transaction stays usable, and
    the re-read returns the real winning row."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RW")

            real_create_media_writer = media_repo.create_media_writer

            async def _create_media_writer_after_a_racer_wins(ex, **kwargs):
                # Simulate the other racer's insert landing first, fully
                # committed on its own connection/transaction -- independent
                # of `ex` (this call's own connection), exactly as a real
                # concurrent caller's own successful insert would be.
                await real_create_media_writer(
                    pool,
                    league_id=kwargs["league_id"],
                    team_id=kwargs["team_id"],
                    persona=kwargs["persona"],
                    name="Winning Racer",
                )
                # This call's own insert now genuinely collides with the
                # row the "other racer" just committed -- a real
                # `asyncpg.UniqueViolationError` from Postgres, not a mock.
                return await real_create_media_writer(ex, **kwargs)

            monkeypatch.setattr(
                media_service.media_repo,
                "create_media_writer",
                _create_media_writer_after_a_racer_wins,
            )

            async with transaction(pool) as conn:
                writer = await media_service.get_or_create_media_writer(
                    conn, league_rng_seed=1, league_id=league_id, team_id=team_id,
                    persona="beat_writer",
                )
                assert writer.name == "Winning Racer", (
                    "must return the real winning row, not raise "
                    "InFailedSQLTransactionError from the aborted outer transaction"
                )

                # The outer transaction must still be usable afterward -- the
                # whole point of the fix is that this call's own violation
                # was contained to a savepoint, not the surrounding
                # transaction a real multi-repo caller still needs to use.
                still_usable = await media_repo.get_media_writer(
                    conn, league_id=league_id, team_id=team_id, persona="beat_writer"
                )
                assert still_usable is not None
                assert still_usable.id == writer.id

            rows = await media_repo.list_media_writers_for_league(pool, league_id)
            assert len(rows) == 1, "exactly one row -- the loser's insert must not persist"
            assert rows[0].name == "Winning Racer"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_selection_cycle_persists_one_story_per_candidate_in_order():
    """Decision P4-88's own contract, proven end to end: `candidate_
    sequence` is derived from EXACTLY ONE `enumerate()` over the caller's
    own `candidates` tuple (via `salience.build_publication_plan`'s own
    `main` group), and this is the only place that happens."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "RC")
            library = media_service.load_media_library()

            candidates = tuple(
                c.candidate
                for c in [
                    await _scored_streak_candidate(pool, team_id=team_id, streak_length=length)
                    for length in (9, -9, 8)
                ]
            )

            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=42, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=candidates,
                library=library, use_counts={},
            )
            stories = result.stories

            assert len(stories) == 3
            assert all(story.id is not None for story in stories)
            assert len({story.id for story in stories}) == 3, "three distinct rows, not reuse"

            persisted = await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            assert len(persisted) == 3
            assert {s.id for s in persisted} == {s.id for s in stories}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_selection_cycle_empty_candidates_persists_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            library = media_service.load_media_library()
            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=1, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=(),
                library=library, use_counts={},
            )
            assert result.stories == ()
            assert result.publications == ()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Round 14 critic fix (blocking finding 1) -- `run_media_selection_cycle`'s
# repeat-penalty use_counts must actually advance across candidates within
# one cycle, not stay pinned at the caller's original (typically empty)
# mapping for every candidate. Reproduces the critic's own verification
# method exactly: spy on the `use_counts` mapping `select.select_media_
# template` receives on every call within one cycle (critic: "spied on
# use_counts per call ... every call saw {}").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_media_selection_cycle_advances_use_counts_across_candidates_in_one_cycle(
    monkeypatch,
):
    """Six same-shape `StoryCandidate`s (identical kind, identical trigger-
    eligible `streak_length`, six distinct teams -- the critic's own repro
    shape: "6 STREAK candidates for 6 distinct teams") compete for the same
    small per-(kind, voice) template pool in one cycle. Before round 14's
    fix, `run_media_selection_cycle` passed the identical, un-advancing
    `use_counts` mapping to every candidate's `select_and_bind_story` call
    -- this test proves the wiring is fixed by recording the exact mapping
    `select.select_media_template` receives on each of the six calls and
    comparing it against an INDEPENDENTLY computed expected running count
    (built from the previously-returned stories' own `voice`/`template_id`s,
    not from `run_media_selection_cycle`'s internals) -- would fail pre-
    round-14-fix (every recorded snapshot is `{}`, since the loop never
    advanced anything) and passes post-fix.

    **Round 15 update**: the expectation is now built PER VOICE (Decision
    P4-29's own `(voice, cycle)` scope), not as one flat cross-voice pool
    -- round 14's own version of this test built a flat `dict[str, int]`
    expectation, which happened to still pass under the (wrong) flat
    implementation this round replaces, because it never gave two
    candidates DIFFERENT voices drawing the SAME shared fallback template a
    chance to prove the scopes apart (see `test_run_media_selection_cycle_
    scopes_use_counts_per_voice_not_flat_across_the_cycle` below for the
    test that does). This test's own job stays "does the mapping advance
    at all within the cycle," now measured at the correct (per-voice)
    granularity."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_ids = [await _make_team(pool, league_id, f"S{i}") for i in range(6)]
            library = media_service.load_media_library()
            candidates = tuple(
                c.candidate
                for c in [
                    await _scored_streak_candidate(pool, team_id=team_id, streak_length=12)
                    for team_id in team_ids
                ]
            )

            real_select_media_template = media_service.select.select_media_template
            recorded_use_counts: list[dict[str, int]] = []
            recorded_voices: list[str] = []

            def _spying_select_media_template(package, templates, *, voice, use_counts, **kwargs):
                recorded_use_counts.append(dict(use_counts))
                recorded_voices.append(voice)
                return real_select_media_template(
                    package, templates, voice=voice, use_counts=use_counts, **kwargs
                )

            monkeypatch.setattr(
                media_service.select, "select_media_template", _spying_select_media_template
            )

            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=7, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=candidates,
                library=library, use_counts={},
            )
            stories = result.stories

            assert len(recorded_use_counts) == 6

            # Round 15: the mapping `select_media_template` receives is now
            # scoped per `(voice, cycle)` (Decision P4-29), so the
            # independently-built expectation must be keyed by voice too --
            # `recorded_use_counts[i]` is compared against only the SAME
            # voice's own running count, never the flat cross-voice pool
            # round 14's own version of this test compared against.
            expected_running: dict[str, dict[str, int]] = {}
            for i, story in enumerate(stories):
                assert recorded_voices[i] == story.voice
                voice_expected = expected_running.get(story.voice, {})
                assert recorded_use_counts[i] == voice_expected, (
                    f"candidate {i} (voice={story.voice!r}): select_media_template received "
                    f"{recorded_use_counts[i]!r}, expected {voice_expected!r} (built from the "
                    f"real template_ids of this SAME voice's own candidates persisted earlier "
                    f"in this cycle) -- the repeat penalty's per-voice use_counts is not "
                    f"actually advancing within this cycle"
                )
                voice_bucket = expected_running.setdefault(story.voice, {})
                voice_bucket[story.template_id] = voice_bucket.get(story.template_id, 0) + 1

            # At least one seen use_counts snapshot is non-empty -- i.e. the
            # spy actually observed the map advancing at least once across
            # these six candidates, not merely "always empty, coincidentally
            # matching an also-always-empty expectation."
            assert any(snapshot for snapshot in recorded_use_counts), (
                "every recorded use_counts snapshot was empty -- this run never exercised "
                "the repeat-penalty-advancing behavior this test exists to prove"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Round 15 critic fix -- Decision P4-29's already-specified `(voice, cycle)`
# repeat-penalty scope, restored. The test above proves the mapping ADVANCES
# within a cycle; it never proved a SELECTION changes because of the scope
# choice (it would pass identically whether use_counts is scoped flat or per
# voice, since round 14's own version happened not to give two candidates
# with DIFFERENT voices a shared template to contest). This test forces
# exactly that: two `analyst`-voiced candidates draw `fallback_milestone`
# (MILESTONE's one voice-wildcard template) before a third, `historian`-
# voiced candidate draws from the same kind's survivor set, which also
# contains `fallback_milestone`. It fails under a flat scope (the two
# unrelated `analyst` draws suppress `fallback_milestone`'s weight for the
# unrelated `historian` candidate too) and passes under the correct
# per-`(voice, cycle)` scope (an `analyst` voice's own repeats can never
# suppress a `historian` voice's own draw).
# ---------------------------------------------------------------------------


class _ForcedPersonaRng:
    """Fakes exactly the one call `select_persona` makes (`rng.choices(
    survivors, weights=..., k=1)`) -- returns whichever real `MediaPersona`
    in `survivors` has `.key == forced_key`, ignoring `weights` entirely.
    `select_persona`'s own eligibility/sort logic still runs for real; only
    the weighted draw is forced, so this stays a real production code path
    with a controlled outcome, not a bypass of it."""

    def __init__(self, forced_key: str) -> None:
        self._forced_key = forced_key

    def choices(self, survivors, weights, k=1):  # noqa: ARG002 -- weights ignored by design
        assert k == 1
        return [next(p for p in survivors if p.key == self._forced_key)]


class _ForcedTemplateRng:
    """The template-draw analogue of `_ForcedPersonaRng` -- forces whichever
    real `MediaTemplate` in `survivors` has `.id == forced_id`, ignoring
    `weights`. Used for the two `analyst` candidates below, whose own draw
    is not what this test measures (only that they really do persist
    `fallback_milestone`, so the real `run_media_selection_cycle` loop
    really does record a real prior use of it)."""

    def __init__(self, forced_id: str) -> None:
        self._forced_id = forced_id

    def choices(self, survivors, weights, k=1):  # noqa: ARG002 -- weights ignored by design
        assert k == 1
        return [next(t for t in survivors if t.id == self._forced_id)]


class _ThresholdTemplateRng:
    """The one rng that actually measures the scope question: `engine.
    select_template` computes each survivor's real, penalty-adjusted
    `effective_weight = template.weight * (repeat_penalty **
    use_counts.get(template.id, 0))` (`dba2/managers/voice/engine.py`) and
    passes the real `(survivors, weights)` pair straight through to this
    object's own `choices` -- the only thing faked is which of the two
    already-real weights wins, decided by a plain threshold on
    `fallback_milestone`'s own real, already-computed weight rather than by
    reimplementing the RNG math. `fallback_milestone`'s authored weight is
    `1.0` (`seeds/media_phrases.json`); `repeat_penalty` is `0.3`. Scoped
    per `(voice, cycle)` (the fix this test proves), the `historian` voice
    below has never used `fallback_milestone` in this cycle, so its
    effective weight is the full `1.0` (`0.3 ** 0`), clearing the `0.5`
    threshold and winning. Scoped flat (the bug this test guards against),
    two unrelated `analyst`-voiced draws already used it twice by the time
    this call happens, so its effective weight is `1.0 * (0.3 ** 2) =
    0.09`, well under the threshold, and `historian_milestone` (weight
    `2.0`, untouched by either scope) wins instead -- a real, measured
    divergence in which template gets chosen, not merely in whether a
    count field advances."""

    def choices(self, survivors, weights, k=1):  # noqa: ARG002 -- id, not index, used
        assert k == 1
        pairs = list(zip(survivors, weights, strict=True))
        fallback_weight = next(w for t, w in pairs if t.id == "fallback_milestone")
        target_id = "fallback_milestone" if fallback_weight >= 0.5 else "historian_milestone"
        return [next(t for t, _w in pairs if t.id == target_id)]


async def _scored_milestone_candidate(pool, *, player_id: int) -> ScoredCandidate:
    package = await _sealed_package(
        pool,
        StoryCandidate(
            kind=StoryKind.MILESTONE, player_id=player_id,
            player_games_played_this_season=20,
        ),
    )
    return ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.9)


@pytest.mark.asyncio
async def test_run_media_selection_cycle_scopes_use_counts_per_voice_not_flat_across_the_cycle(
    monkeypatch,
):
    """Real reproduction of the critic's own finding: an `analyst`-voiced
    repeat must never suppress a `historian`-voiced candidate's own draw of
    a template the two voices merely happen to share (MILESTONE's one
    voice-wildcard fallback, `fallback_milestone`) -- Decision P4-29's own
    `(voice, cycle)` scope, restored by round 15.

    Three MILESTONE candidates, three distinct players. `select_persona`'s
    OWN real eligibility/sort logic still runs; only its weighted draw is
    forced (`_ForcedPersonaRng`) to a fixed sequence, `analyst, analyst,
    historian`, via `rng_for`'s own salt -- `candidate_sequence` is embedded
    in the real salt tuple `select_and_bind_story` builds, so this test
    reads it positionally rather than guessing a call order. The first two
    candidates' own template draw is likewise forced
    (`_ForcedTemplateRng("fallback_milestone")`) so both really do persist
    `fallback_milestone` as `analyst` (proving `run_media_selection_cycle`
    really does advance a real per-voice count from a real returned story,
    not a fabricated one). The third candidate's template draw is the one
    actually under test: `_ThresholdTemplateRng` decides its outcome from
    the REAL, already-computed `(survivors, weights)` `engine.select_
    template` hands it -- see that class's own docstring for the exact
    arithmetic.

    Asserts the third story's `template_id == "fallback_milestone"` --
    correct under the real, shipped per-`(voice, cycle)` scope. Confirmed
    by hand (temporarily reverting `run_media_selection_cycle` to round
    14's flat `dict[str, int]` shape) that this same assertion FAILS
    against that flat scope, landing on `"historian_milestone"` instead --
    exactly the cross-voice suppression this test exists to catch."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "MV")
            player_ids = [await _make_player(pool, league_id, team_id) for _ in range(3)]
            library = media_service.load_media_library()
            candidates = tuple(
                c.candidate
                for c in [
                    await _scored_milestone_candidate(pool, player_id=player_id)
                    for player_id in player_ids
                ]
            )

            real_rng_for = media_service.rng_for
            forced_personas = {0: "analyst", 1: "analyst", 2: "historian"}

            def _fake_rng_for(seed, *salt):
                if salt[1] == "persona":
                    candidate_sequence = salt[-1]
                    return _ForcedPersonaRng(forced_personas[candidate_sequence])
                if salt[1] == "story":
                    candidate_sequence = salt[-2]
                    if candidate_sequence in (0, 1):
                        return _ForcedTemplateRng("fallback_milestone")
                    return _ThresholdTemplateRng()
                return real_rng_for(seed, *salt)

            monkeypatch.setattr(media_service, "rng_for", _fake_rng_for)

            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=1, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=candidates,
                library=library, use_counts={},
            )
            stories = result.stories

            assert [story.voice for story in stories] == ["analyst", "analyst", "historian"]
            assert [story.template_id for story in stories] == [
                "fallback_milestone", "fallback_milestone", "fallback_milestone",
            ], (
                "the historian-voiced candidate must still draw fallback_milestone -- two "
                "unrelated analyst-voiced uses of the same template must never suppress a "
                "different voice's own draw of it (Decision P4-29's per-(voice, cycle) scope)"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# load_team_profile_packages -- docs/design/media-v2-newsroom.md MV-2h.2/
# MV-2h.3, "Build order" step 6. Uses `sim_league_at` (a REAL, 30-team,
# fully-seeded dev league, `tests/integration/_press_fixtures.py`'s own
# helper) rather than this file's own minimal `_make_league`/`_make_team`
# -- `load_gm_inputs` (this loader's own shared `LeagueSnapshot` source)
# needs real cap settings/contracts/ratings rows this file's own minimal
# fixtures never create.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_team_profile_packages_publishes_up_to_the_quota():
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            candidates = await media_service.load_team_profile_packages(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                cycle_index=0, quota=3,
            )
            assert 0 < len(candidates) <= 3
            for candidate in candidates:
                package = candidate.package
                assert package.story_kind == StoryKind.TEAM_PROFILE
                assert package.subject.team_name is not None
                assert len(package.facts) >= 6
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_team_profile_packages_excludes_a_team_already_profiled_this_season():
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            teams = await team_repo.list_teams_for_league(pool, league_id)
            already_profiled_team_id = teams[0].id
            await media_repo.create_media_story(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                cycle_index=0, story_kind=StoryKind.TEAM_PROFILE, voice="beat_writer",
                template_id="dedicated_beat_writer_team_profile", slot_bindings={},
                rendered_text="already profiled", salience=0.5,
                subject_player_id=None, subject_team_id=already_profiled_team_id,
                writer_id=None,
            )
            candidates = await media_service.load_team_profile_packages(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                cycle_index=0, quota=30,
            )
            profiled_team_ids = {c.package.subject.team_id for c in candidates}
            assert already_profiled_team_id not in profiled_team_ids
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_team_profile_packages_empty_when_every_team_already_profiled():
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            teams = await team_repo.list_teams_for_league(pool, league_id)
            for team in teams:
                await media_repo.create_media_story(
                    pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                    cycle_index=0, story_kind=StoryKind.TEAM_PROFILE, voice="beat_writer",
                    template_id="dedicated_beat_writer_team_profile", slot_bindings={},
                    rendered_text="already profiled", salience=0.5,
                    subject_player_id=None, subject_team_id=team.id, writer_id=None,
                )
            candidates = await media_service.load_team_profile_packages(
                pool, league_id=league_id, season=_SEASON, phase=Phase.REGULAR_SEASON,
                cycle_index=0, quota=3,
            )
            assert candidates == ()
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Real per-persona caps and the hot-take band -- docs/design/media-v2-
# newsroom.md MV-1c.3/MV-3h, "Build order" step 6.
# ---------------------------------------------------------------------------


def _with_tight_streak_caps(library, *, max_per_cycle: int) -> media_service.LoadedMediaLibrary:
    """Every real persona covering `STREAK` (`beat_writer`/`analyst`/
    `historian`) capped at `max_per_cycle` for `REGULAR_SEASON` -- the real
    shipped library's own caps are generous enough that a small, real
    integration fixture would rarely bind one by chance; overriding just
    the cadence table (not the personas/templates) keeps every other real
    validation/content the shipped library carries."""
    overridden = dict(library.phase_cadence)
    for persona_key in ("beat_writer", "analyst", "historian"):
        per_phase = dict(overridden[persona_key])
        per_phase["REGULAR_SEASON"] = PhaseCadence(
            weight_multiplier=per_phase["REGULAR_SEASON"].weight_multiplier,
            max_per_cycle=max_per_cycle,
        )
        overridden[persona_key] = per_phase
    return dataclasses.replace(library, phase_cadence=overridden)


@pytest.mark.asyncio
async def test_run_media_selection_cycle_caps_out_once_every_eligible_persona_is_at_cap():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_ids = [await _make_team(pool, league_id, f"C{i}") for i in range(4)]
            library = _with_tight_streak_caps(media_service.load_media_library(), max_per_cycle=1)
            candidates = tuple(
                c.candidate
                for c in [
                    await _scored_streak_candidate(pool, team_id=team_id, streak_length=9)
                    for team_id in team_ids
                ]
            )

            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=11, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=candidates,
                library=library, use_counts={},
            )

            # Combined headroom across the three STREAK-covering personas is
            # 3 (1 each); 4 candidates means at least one is capped out no
            # matter which persona real `select_persona` draws for each --
            # a pigeonhole guarantee, not a probabilistic one.
            assert result.capped_out >= 1
            assert len(result.stories) == 4 - result.capped_out
            assert result.plan_size == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_run_media_selection_cycle_publishes_a_real_hot_take_band_candidate():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "BT")
            library = media_service.load_media_library()
            # STANDINGS_COLLAPSE is one of hot_take's declared 12 kinds
            # (MV-3h.0). Zeroed prominence/novelty and a small real delta
            # (`abs(3)/15 = 0.2` magnitude) land the real `score_candidates`
            # formula at `0.4*0.2 + 0.2*0.70 = 0.22`, strictly inside
            # `(HOT_TAKE_FLOOR, SALIENCE_FLOOR] = (0.18, 0.30]`.
            package = await _sealed_package(
                pool,
                StoryCandidate(
                    kind=StoryKind.STANDINGS_COLLAPSE, team_id=team_id,
                    team_games_played_this_season=20, has_pulse_snapshot=True,
                    standings_position=10, standings_position_at_last_pulse=7,
                ),
            )
            candidate = SalienceCandidate(
                package=package, subject_prominence=0.0, cycles_since_last_seen=0
            )

            result = await media_service.run_media_selection_cycle(
                pool, league_id=league_id, league_rng_seed=5, season=_SEASON,
                phase=Phase.REGULAR_SEASON, cycle_index=0, candidates=(candidate,),
                library=library, use_counts={},
            )

            assert result.band_published == 1
            assert len(result.stories) == 1
            assert result.stories[0].voice == "hot_take"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# load_event_candidates -- docs/design/media-v2-newsroom.md MV-11, "Build
# order" step 7.5a. Uses `sim_league_at` (a REAL, 30-team, fully-seeded dev
# league) for the identical reason `load_team_profile_packages`'s own tests
# above do: `load_gm_inputs` needs real cap settings/contracts/ratings rows
# this file's own minimal `_make_league`/`_make_team` never create.
#
# **Why TRADE_EXECUTED, not one of the other seven newly-wired kinds, gets
# the direct DB proof here.** `tools/generate_media_corpus.py`'s own dev-
# league driver never calls any AI trade-window logic (confirmed by reading
# it: it calls `agency_service.run_agency_checkpoint` directly but nothing
# that executes a trade) -- a real, disclosed, PRE-EXISTING harness gap this
# dispatch found, not created: `league_transactions` is empty across the
# WHOLE dev database at the time of this dispatch (`SELECT txn_type,
# count(*) FROM league_transactions GROUP BY txn_type` returns zero rows),
# so a `generate_media_corpus` run can never observe TRADE_EXECUTED/
# TRADE_BROKEN_PROMISE/TRADE_MARKET_WATCH/REVENGE_GAME, or RIVALRY_MATCHUP's
# `recent_trade` signal term, no matter how correct their wiring is. A real
# trade row, inserted directly here via `txn_repo.create_trade_transaction`
# (the same repo call `gm_service.apply_trade` itself makes), is this
# dispatch's own real, DB-backed proof that the trade-dependent branch of
# `load_event_candidates` is wired correctly despite that harness gap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_event_candidates_produces_a_real_trade_executed_package():
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            teams = await team_repo.list_teams_for_league(pool, league_id)
            team_a, team_b = teams[0], teams[1]
            players_a = await player_repo.list_players_for_team(pool, team_a.id)
            players_b = await player_repo.list_players_for_team(pool, team_b.id)
            await txn_repo.create_trade_transaction(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                team_a_id=team_a.id,
                team_b_id=team_b.id,
                players_a_to_b=[players_a[0].id],
                players_b_to_a=[players_b[0].id, players_b[1].id],
                picks_a_to_b=[],
                picks_b_to_a=[],
            )

            league_snapshot, gm_context = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )
            team_names = {
                ts.id: f"{ts.team.city} {ts.team.name}" for ts in league_snapshot.teams
            }
            player_names = {
                rp.player.id: f"{rp.player.first_name} {rp.player.last_name}"
                for ts in league_snapshot.teams
                for rp in ts.roster
            }
            current_snapshot = (
                await media_service.run_media_pulse_cycle(
                    pool, league_id=league_id, season=_SEASON, cycle_index=1
                )
            ).snapshot

            candidates = await media_service.load_event_candidates(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=1,
                league_snapshot=league_snapshot,
                gm_context=gm_context,
                current_snapshot=current_snapshot,
                team_names=team_names,
                player_names=player_names,
            )

            trade_packages = [
                c.package for c in candidates if c.package.story_kind is StoryKind.TRADE_EXECUTED
            ]
            assert len(trade_packages) == 1, (
                f"expected exactly one TRADE_EXECUTED package for the one real trade; "
                f"kinds seen: {sorted({c.package.story_kind.value for c in candidates})}"
            )
            package = trade_packages[0]
            assert {team_a.id, team_b.id} == {
                package.subject.team_id, package.subject.opponent_team_id
            }
            facts_by_key = {f.key: f.value_number for f in package.facts}
            # team_a sent 1 player, received 2 -- whichever side `team_id`
            # landed on (the lower-id-first convention, `trades.py`'s own
            # docstring), the two counts must be {1, 2}, not {0, 0}.
            assert {facts_by_key["players_sent_count"], facts_by_key["players_acquired_count"]} == {
                1.0, 2.0,
            }
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# REVENGE_GAME -- docs/design/media-v2-newsroom.md MV-15, closing 7.5a's own
# named open follow-up ("`RIVALRY_MATCHUP`/`REVENGE_GAME`'s own loader wiring
# has no dedicated integration-test proof yet"). The discriminating setup is
# a pair that has ALREADY MET once before the trade is inserted -- a pair
# that has not yet met would pass even against the pre-MV-15 buggy code (an
# empty final-games list reads as "first meeting" under either the wrong or
# the right comparison), which would make the test a coincidence, not a
# regression test. `_pick_pair_with_a_prior_meeting_and_a_later_game` below
# scans the REAL simulated schedule for such a pair by `game_index`, never
# `teams[0]`/`teams[1]` -- which two teams actually qualify depends on the
# real schedule generator's output, not something safe to assume.
# ---------------------------------------------------------------------------

# Simmed into a real, UNTRIMMED 1,230-game schedule (`keep_games=1230`, i.e.
# `sim_league_at`'s trim is a no-op) so the real repeat matchups a trimmed,
# first-round-only schedule would never contain are still there. 100 games
# is enough for some team's rotation player to reach the `>= 5` games
# REVENGE_GAME's own gate requires (confirmed empirically: this deterministic
# `rng_seed=3` schedule reaches 6-7 games played for a team's top scorer by
# game_index 100), while staying well under the ~1,230-game season so the
# test doesn't sim more than it needs to.
_REVENGE_GAMES_TO_SIM_FIRST = 100


def _pick_pair_with_a_prior_meeting_and_a_later_game(games) -> tuple[int, int, int]:
    """From the real schedule's `game_index` order: the pair whose first two
    meetings straddle `_REVENGE_GAMES_TO_SIM_FIRST` (one at or before it --
    becomes `final` once simmed through that index; one strictly after --
    stays `scheduled`, the pair's "rematch"), preferring the pair whose
    second meeting comes soonest so the negative test's own "sim forward
    until it's final" step sims as few extra games as the real schedule
    allows. Returns `(team_a_id, team_b_id, second_meeting_game_index)`."""
    first_meeting_index: dict[tuple[int, int], int] = {}
    two_meeting_pairs: list[tuple[int, int, tuple[int, int]]] = []
    for game in sorted(games, key=lambda g: g.game_index):
        pair = tuple(sorted((game.home_team_id, game.away_team_id)))
        if pair not in first_meeting_index:
            first_meeting_index[pair] = game.game_index
        else:
            two_meeting_pairs.append((first_meeting_index[pair], game.game_index, pair))
            del first_meeting_index[pair]
    straddling = [
        c
        for c in two_meeting_pairs
        if c[0] <= _REVENGE_GAMES_TO_SIM_FIRST < c[1]
    ]
    assert straddling, (
        "no pair in this real, deterministic schedule met once by game_index "
        f"{_REVENGE_GAMES_TO_SIM_FIRST} with a later meeting still ahead -- "
        "schedule generation changed; this test needs recalibrating, not skipping"
    )
    straddling.sort(key=lambda c: c[1])
    _, second_meeting_index, (team_a_id, team_b_id) = straddling[0]
    return team_a_id, team_b_id, second_meeting_index


async def _load_revenge_test_league(pool) -> tuple[int, int, int, int]:
    """Shared setup for both REVENGE_GAME integration tests below: a real,
    untrimmed, sim-able league; the discriminating pair picked from its real
    schedule; that pair's first meeting simmed to `final`; and a real trade
    of that pair's most-experienced roster player from `team_a` to `team_b`
    (satisfying MV-2i's `>= 5` games-played gate). Returns `(league_id,
    team_a_id, team_b_id, traded_player_id, second_meeting_game_index)`
    -- callers advance the sim from here in whichever direction their own
    case needs."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=1230)
    season_games = await game_repo.list_games_for_league_season(pool, league_id, _SEASON)
    regular_games = [g for g in season_games if g.game_type is GameType.REGULAR]
    team_a_id, team_b_id, second_meeting_index = _pick_pair_with_a_prior_meeting_and_a_later_game(
        regular_games
    )

    await game_sim_service.sim_scheduled_games(
        pool, league_id, _SEASON, through_game_index=_REVENGE_GAMES_TO_SIM_FIRST
    )
    await stats_service.rebuild_player_season_stats(pool, league_id, _SEASON, GameType.REGULAR)

    players_a = await player_repo.list_players_for_team(pool, team_a_id)
    season_stats = await stats_repo.get_player_season_stats_bulk(
        pool, league_id, {p.id for p in players_a}, _SEASON, GameType.REGULAR
    )
    assert season_stats, (
        f"no player on team {team_a_id} has any season_stats row after simming "
        f"{_REVENGE_GAMES_TO_SIM_FIRST} games -- schedule/sim changed; recalibrate "
        "_REVENGE_GAMES_TO_SIM_FIRST, don't skip"
    )
    traded_player_id = max(season_stats, key=lambda pid: season_stats[pid].games_played)
    games_played = season_stats[traded_player_id].games_played
    assert games_played >= 5, (
        f"the most-experienced player on team {team_a_id} only has {games_played} "
        "games by the sim point this test uses -- MV-2i's own >= 5 gate needs "
        "_REVENGE_GAMES_TO_SIM_FIRST raised, not this assertion loosened"
    )

    # Real trade, real roster move -- MV-15 item 5's new guard reads the
    # player's CURRENT team from a fresh `player_repo.list_players_for_
    # league` scan, so the move has to be real, not merely logged.
    await player_repo.set_team_and_status(pool, traded_player_id, team_b_id, RosterStatus.ACTIVE)
    await txn_repo.create_trade_transaction(
        pool,
        league_id=league_id,
        season=_SEASON,
        phase=Phase.REGULAR_SEASON,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        players_a_to_b=[traded_player_id],
        players_b_to_a=[],
        picks_a_to_b=[],
        picks_b_to_a=[],
    )
    return league_id, team_a_id, team_b_id, traded_player_id, second_meeting_index


async def _revenge_candidates_for(pool, league_id: int, *, cycle_index: int):
    league_snapshot, gm_context = await gm_service.load_gm_inputs(
        pool, league_id=league_id, season=_SEASON
    )
    team_names = {ts.id: f"{ts.team.city} {ts.team.name}" for ts in league_snapshot.teams}
    player_names = {
        rp.player.id: f"{rp.player.first_name} {rp.player.last_name}"
        for ts in league_snapshot.teams
        for rp in ts.roster
    }
    current_snapshot = (
        await media_service.run_media_pulse_cycle(
            pool, league_id=league_id, season=_SEASON, cycle_index=cycle_index
        )
    ).snapshot
    return await media_service.load_event_candidates(
        pool,
        league_id=league_id,
        season=_SEASON,
        phase=Phase.REGULAR_SEASON,
        cycle_index=cycle_index,
        league_snapshot=league_snapshot,
        gm_context=gm_context,
        current_snapshot=current_snapshot,
        team_names=team_names,
        player_names=player_names,
    )


@pytest.mark.asyncio
async def test_revenge_game_fires_for_a_pair_that_met_before_the_trade():
    pool = await get_pool()
    try:
        league_id, team_a_id, team_b_id, traded_player_id, _ = await _load_revenge_test_league(
            pool
        )
        try:
            candidates = await _revenge_candidates_for(pool, league_id, cycle_index=0)
            revenge_packages = [
                c.package
                for c in candidates
                if c.package.story_kind is StoryKind.REVENGE_GAME
                and c.package.subject.player_id == traded_player_id
            ]
            assert len(revenge_packages) == 1, (
                "expected exactly one REVENGE_GAME package for the traded player, "
                "who already met his old team before the trade; got "
                f"{len(revenge_packages)}"
            )
            package = revenge_packages[0]
            assert package.subject.team_id == team_b_id
            assert package.subject.opponent_team_id == team_a_id
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_revenge_game_does_not_fire_after_the_pair_meets_post_trade():
    pool = await get_pool()
    try:
        (
            league_id,
            team_a_id,
            team_b_id,
            traded_player_id,
            second_meeting_index,
        ) = await _load_revenge_test_league(pool)
        try:
            # Pins "fires once, not every time": advance the sim until the
            # pair's own rematch (the same game the first test's package
            # previewed) is `final`, then confirm no NEW REVENGE_GAME
            # package for this player -- the fix must not trade "fires
            # correctly the first time" for "keeps firing every cycle after."
            await game_sim_service.sim_scheduled_games(
                pool, league_id, _SEASON, through_game_index=second_meeting_index
            )
            await stats_service.rebuild_player_season_stats(
                pool, league_id, _SEASON, GameType.REGULAR
            )

            candidates = await _revenge_candidates_for(pool, league_id, cycle_index=1)
            revenge_packages = [
                c.package
                for c in candidates
                if c.package.story_kind is StoryKind.REVENGE_GAME
                and c.package.subject.player_id == traded_player_id
            ]
            assert revenge_packages == [], (
                "expected no REVENGE_GAME package once the pair's rematch is final; "
                f"got {len(revenge_packages)}"
            )
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_load_event_candidates_produces_a_real_roster_construction_or_scheme_identity():
    """A trade-independent sanity check that at least one of the non-trade
    detectors (`construction.py`/`scheme.py`, both league-wide scans with no
    special setup needed) fires against a real, freshly-seeded league --
    corroborating the corpus sample this dispatch's As-built note reports
    (`cold_stretch`/`defensive_standout`/`hot_streak`/`roster_construction`/
    `scheme_identity` all real, non-zero kinds in that sample)."""
    pool = await get_pool()
    try:
        league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
        try:
            pulse_result = await media_service.run_media_pulse_cycle(
                pool, league_id=league_id, season=_SEASON, cycle_index=0
            )
            league_snapshot, gm_context = await gm_service.load_gm_inputs(
                pool, league_id=league_id, season=_SEASON
            )
            team_names = {
                ts.id: f"{ts.team.city} {ts.team.name}" for ts in league_snapshot.teams
            }
            player_names = {
                rp.player.id: f"{rp.player.first_name} {rp.player.last_name}"
                for ts in league_snapshot.teams
                for rp in ts.roster
            }
            candidates = await media_service.load_event_candidates(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.REGULAR_SEASON,
                cycle_index=0,
                league_snapshot=league_snapshot,
                gm_context=gm_context,
                current_snapshot=pulse_result.snapshot,
                team_names=team_names,
                player_names=player_names,
            )
            kinds = {c.package.story_kind for c in candidates}
            assert kinds & {StoryKind.ROSTER_CONSTRUCTION, StoryKind.SCHEME_IDENTITY}, (
                f"expected at least one of ROSTER_CONSTRUCTION/SCHEME_IDENTITY over a "
                f"real 30-team league; kinds seen: {sorted(k.value for k in kinds)}"
            )
        finally:
            await drop(pool, league_id)
    finally:
        await close_pool()
