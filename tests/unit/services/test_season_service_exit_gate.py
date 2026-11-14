"""`season_service.evaluate_exit`, the exit gate's pure half
(phase-5-stage-e-advance.md P5-99.e, build-plan commit 7): one row per phase
family, and the multi-blocker case, over hand-built facts.

The loader that produces `GateFacts` is proved against real rows in
`tests/integration/test_season_service_gate_loader.py`; nothing here touches a
database. The expected games targets are written out by hand from P5-99.b.3's
table rather than read from the code under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase
from dba2.services import season_service
from dba2.services.season_service import (
    Blocker,
    BlockerCode,
    GateFacts,
    GateGame,
    evaluate_exit,
)

_SCHEDULE_CONSUMING = {
    Phase.REGULAR_SEASON: 0.45,
    Phase.TRADE_DEADLINE: 0.60,
    Phase.POST_DEADLINE: 0.75,
    Phase.REGULAR_SEASON_COMPLETE: 1.0,
}
_HUMAN_TEAM = 7
_AI_TEAM = 3


def _codes(verdict) -> list[BlockerCode]:
    return [b.code for b in verdict.blockers]


def _pick(
    pick_id: int, *, team: int, pick_number: int | None, used: int | None = None
) -> DraftPick:
    return DraftPick(
        id=pick_id,
        league_id=1,
        season=2026,
        round=1,
        original_team_id=team,
        current_team_id=team,
        pick_number=pick_number,
        used_for_player_id=used,
        protection_type=ProtectionType.NONE,
        protection_through=None,
        swap_with_pick_id=None,
        conveyed=False,
        rolls_over_to_season=None,
    )


# --- the games targets -------------------------------------------------------


def test_the_four_schedule_consuming_phases_carry_the_parents_targets():
    assert season_service.GAMES_TARGET_FRACTION == _SCHEDULE_CONSUMING
    assert season_service.SCHEDULE_CONSUMING_PHASES == frozenset(_SCHEDULE_CONSUMING)


@pytest.mark.parametrize(
    ("fraction", "max_index", "expected"),
    [
        (0.45, 99, 45),  # round(44.55)
        (0.60, 99, 59),  # round(59.4)
        (0.75, 99, 74),  # round(74.25)
        (1.0, 99, 99),
        (1.0, 1229, 1229),
        (0.45, 1229, 553),  # round(553.05)
    ],
)
def test_the_target_index_is_the_corpus_formula(fraction, max_index, expected):
    assert season_service.games_target_index(fraction, max_index) == expected


# --- schedule-consuming phases -----------------------------------------------


@pytest.mark.parametrize("phase", sorted(_SCHEDULE_CONSUMING))
def test_a_scheduled_game_at_or_below_the_target_blocks_and_is_named_by_id(phase):
    # 100 games, indices 0..99; the game AT the target index blocks.
    target = season_service.games_target_index(_SCHEDULE_CONSUMING[phase], 99)
    facts = GateFacts(
        phase=phase,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=4242, game_index=target),),
    )
    verdict = evaluate_exit(facts)

    assert not verdict.allowed
    assert BlockerCode.GAMES_TARGET_NOT_REACHED in _codes(verdict)
    target_blocker = next(
        b for b in verdict.blockers if b.code is BlockerCode.GAMES_TARGET_NOT_REACHED
    )
    assert target_blocker.game_ids == (4242,)
    assert "not played yet" in target_blocker.message, (
        "the gate cannot know a game FAILED (P5-99.e.1); it says only that it is unplayed"
    )


@pytest.mark.parametrize(
    "phase", [Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE, Phase.POST_DEADLINE]
)
def test_the_first_game_past_the_target_does_not_block(phase):
    target = season_service.games_target_index(_SCHEDULE_CONSUMING[phase], 99)
    facts = GateFacts(
        phase=phase,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=1, game_index=target + 1),),
    )
    verdict = evaluate_exit(facts)
    assert verdict.allowed
    assert verdict.blockers == ()


@pytest.mark.parametrize("phase", sorted(_SCHEDULE_CONSUMING))
def test_no_scheduled_games_left_lets_the_phase_go(phase):
    """Postponed games are not `scheduled` (P5-99.u item 5), so a season with a
    postponed game can finish; `max_game_index` still counts them, so the target
    does not shift when one is postponed."""
    facts = GateFacts(phase=phase, max_game_index=99, scheduled_games=())
    assert evaluate_exit(facts).allowed


@pytest.mark.parametrize("phase", sorted(_SCHEDULE_CONSUMING))
def test_a_league_with_no_schedule_at_all_is_blocked_by_name_not_waved_through(phase):
    """A schedule phase with no games would otherwise read "target met" (nothing is
    scheduled), let the phase be left and its closing work -- a media cycle, the stats
    rebuild -- run over zero games (critic m2). The absence is a blocker of its own."""
    verdict = evaluate_exit(GateFacts(phase=phase))
    assert not verdict.allowed
    assert _codes(verdict) == [BlockerCode.SCHEDULE_MISSING]
    (blocker,) = verdict.blockers
    assert "schedule" in blocker.message and phase.value in blocker.message
    assert blocker.game_ids == ()


def test_regular_season_complete_with_one_scheduled_game_is_blocked_twice():
    """Two blockers at once, and both name the game: the arithmetic target
    (1.0 -> the last index) and the absolute statement (P5-99.e's second row)
    are separate facts that happen to agree here."""
    facts = GateFacts(
        phase=Phase.REGULAR_SEASON_COMPLETE,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=77, game_index=99),),
    )
    verdict = evaluate_exit(facts)

    assert not verdict.allowed
    assert _codes(verdict) == [
        BlockerCode.GAMES_TARGET_NOT_REACHED,
        BlockerCode.REGULAR_SEASON_UNFINISHED,
    ]
    assert all(b.game_ids == (77,) for b in verdict.blockers)


def test_the_absolute_statement_is_not_inferred_from_the_arithmetic_target(monkeypatch):
    """`REGULAR_SEASON_COMPLETE` may not be left while ANY regular-season game is
    scheduled, whatever the arithmetic target says: the absolute blocker stands on its
    own, and the loader hands it every scheduled game at that phase, not only the ones
    at or below the target (critic M2). The two only coincide today because the phase's
    fraction is 1.0, so the test moves the fraction: with 0.5 of 100 games the target
    index is 50 and a scheduled game 80 -- exactly the facts the loader now produces,
    unlike a `max_game_index` BELOW the game -- is beyond the target and still blocks."""
    monkeypatch.setitem(season_service.GAMES_TARGET_FRACTION, Phase.REGULAR_SEASON_COMPLETE, 0.5)
    facts = GateFacts(
        phase=Phase.REGULAR_SEASON_COMPLETE,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=9, game_index=80),),
    )
    verdict = evaluate_exit(facts)
    assert _codes(verdict) == [BlockerCode.REGULAR_SEASON_UNFINISHED]
    assert verdict.blockers[0].game_ids == (9,)


@pytest.mark.parametrize(
    "phase", [Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE, Phase.POST_DEADLINE]
)
def test_only_regular_season_complete_carries_the_absolute_blocker(phase):
    facts = GateFacts(
        phase=phase,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=1, game_index=99),),
    )
    assert BlockerCode.REGULAR_SEASON_UNFINISHED not in _codes(evaluate_exit(facts))


def test_every_scheduled_game_at_or_below_the_target_is_named_in_index_order():
    facts = GateFacts(
        phase=Phase.REGULAR_SEASON,
        max_game_index=99,
        scheduled_games=(
            GateGame(game_id=30, game_index=10),
            GateGame(game_id=20, game_index=3),
            GateGame(game_id=40, game_index=90),  # past the target: not named
        ),
    )
    (blocker,) = evaluate_exit(facts).blockers
    assert blocker.game_ids == (20, 30)


# --- DRAFT -------------------------------------------------------------------


def test_a_draft_with_one_unresolved_pick_is_not_finished():
    """The must-fire board: every RESOLVED pick is used, one pick has no
    `pick_number` yet, so `_eligible_draft_picks_sorted` is empty. A gate that
    read "the eligible list is empty" as "finished" would let the league leave
    DRAFT with a pick owed."""
    picks = (
        _pick(1, team=_AI_TEAM, pick_number=1, used=100),
        _pick(2, team=_AI_TEAM, pick_number=None, used=None),
    )
    verdict = evaluate_exit(GateFacts(phase=Phase.DRAFT, draft_picks=picks))

    assert not verdict.allowed
    assert _codes(verdict) == [BlockerCode.DRAFT_NOT_COMPLETE]
    assert "unresolved" in verdict.blockers[0].message


def test_a_draft_waiting_on_a_person_names_the_team_on_the_clock():
    picks = (_pick(1, team=_HUMAN_TEAM, pick_number=1),)
    verdict = evaluate_exit(
        GateFacts(
            phase=Phase.DRAFT,
            draft_picks=picks,
            self_managed_team_ids=frozenset({_HUMAN_TEAM}),
        )
    )
    assert _codes(verdict) == [BlockerCode.DRAFT_NOT_COMPLETE]
    assert str(_HUMAN_TEAM) in verdict.blockers[0].message
    assert "person" in verdict.blockers[0].message


def test_an_empty_human_set_reads_the_same_board_as_an_exhausted_pool():
    """The human set is what tells 'waiting on a person' from 'nobody can pick':
    dropping it turns the first board into the second."""
    picks = (_pick(1, team=_HUMAN_TEAM, pick_number=1),)
    verdict = evaluate_exit(GateFacts(phase=Phase.DRAFT, draft_picks=picks))
    assert _codes(verdict) == [BlockerCode.DRAFT_NOT_COMPLETE]
    assert "exhausted" in verdict.blockers[0].message
    assert "person" not in verdict.blockers[0].message


def test_a_finished_draft_lets_the_phase_go():
    picks = (
        _pick(1, team=_AI_TEAM, pick_number=1, used=100),
        _pick(2, team=_HUMAN_TEAM, pick_number=2, used=101),
    )
    verdict = evaluate_exit(
        GateFacts(
            phase=Phase.DRAFT,
            draft_picks=picks,
            self_managed_team_ids=frozenset({_HUMAN_TEAM}),
        )
    )
    assert verdict.allowed


def test_a_draft_with_no_board_is_vacuously_finished_here():
    """Refusing to ENTER a phase with no board is the pre-entry check's job
    (P5-99.h, build-plan commit 13), not an exit blocker."""
    assert evaluate_exit(GateFacts(phase=Phase.DRAFT)).allowed


# --- WAIVERS and everything else ---------------------------------------------


def test_waivers_always_names_the_phase_7_message():
    verdict = evaluate_exit(GateFacts(phase=Phase.WAIVERS))
    assert not verdict.allowed
    assert _codes(verdict) == [BlockerCode.WAIVERS_EDGE]
    assert verdict.blockers[0].message == season_service.WAIVERS_EDGE_MESSAGE
    assert "Phase 7" in season_service.WAIVERS_EDGE_MESSAGE


_GATED = set(_SCHEDULE_CONSUMING) | {Phase.DRAFT, Phase.WAIVERS}


@pytest.mark.parametrize("phase", sorted(set(Phase) - _GATED))
def test_every_other_phase_has_no_blocker_stated_positively(phase):
    assert len(set(Phase) - _GATED) == 14
    verdict = evaluate_exit(GateFacts(phase=phase))
    assert verdict.allowed
    assert verdict.blockers == ()


def test_facts_that_belong_to_another_phase_are_ignored():
    """A scheduled game and an unresolved pick mean nothing at AWARDS: the
    phase decides which facts are read, so stray facts cannot invent a blocker."""
    facts = GateFacts(
        phase=Phase.AWARDS,
        max_game_index=99,
        scheduled_games=(GateGame(game_id=1, game_index=0),),
        draft_picks=(_pick(1, team=_AI_TEAM, pick_number=None),),
    )
    assert evaluate_exit(facts).allowed


def test_the_verdict_is_allowed_exactly_when_it_names_no_blocker():
    blocked = evaluate_exit(GateFacts(phase=Phase.WAIVERS))
    open_ = evaluate_exit(GateFacts(phase=Phase.AWARDS))
    assert blocked.allowed is (not blocked.blockers)
    assert open_.allowed is (not open_.blockers)


def test_facts_and_verdicts_are_frozen():
    facts = GateFacts(phase=Phase.AWARDS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.phase = Phase.DRAFT  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Blocker(code=BlockerCode.WAIVERS_EDGE, message="x").message = "y"  # type: ignore[misc]
