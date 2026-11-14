"""`dba2/media/detect/construction.py` -- MV-2e: `ROSTER_CONSTRUCTION`."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.managers.needs import NeedTier
from dba2.media.detect.construction import roster_construction_package

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 1
_TEAM = 1


def _grid(default: float = 0.10) -> dict[Position, dict[NeedTier, float]]:
    return {pos: dict.fromkeys(NeedTier, default) for pos in Position}


def _kwargs(**overrides):
    base = dict(
        need=_grid(), glut=_grid(), supply=_grid(default=1.0),
        starter_tier=NeedTier.STARTER, rotation_tier=NeedTier.ROTATION, star_tier=NeedTier.STAR,
        mean_age_top8=26.0, timeline=Timeline.RETOOL, gm_philosophy=GmPhilosophy.EGALITARIAN,
        measured_three_point_bias=50, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    base.update(overrides)
    return base


def test_a_balanced_roster_fires_nothing():
    assert roster_construction_package(_TEAM, **_kwargs()) is None


def test_stark_need_fires_independently():
    need = _grid()
    need[Position.C][NeedTier.STARTER] = 0.70
    package = roster_construction_package(_TEAM, **_kwargs(need=need))
    assert package is not None
    assert package.story_kind is StoryKind.ROSTER_CONSTRUCTION
    assert package.text("stark_need_position") == "C"
    assert package.number("stark_need_value") == 0.70


def test_stark_glut_fires_independently():
    glut = _grid()
    glut[Position.PG][NeedTier.ROTATION] = 0.65
    package = roster_construction_package(_TEAM, **_kwargs(glut=glut))
    assert package is not None
    assert package.text("stark_glut_position") == "PG"


def test_aging_core_fires_independently():
    package = roster_construction_package(
        _TEAM, **_kwargs(mean_age_top8=30.0, timeline=Timeline.REBUILD)
    )
    assert package is not None
    assert package.number("aging_core_mean_age") == 30.0


def test_aging_core_does_not_fire_without_rebuild_timeline():
    package = roster_construction_package(
        _TEAM, **_kwargs(mean_age_top8=30.0, timeline=Timeline.WIN_NOW)
    )
    assert package is None


def test_starless_win_now_fires_independently():
    need = _grid()
    for pos in (Position.PG, Position.SG, Position.SF):
        need[pos][NeedTier.STAR] = 1.0
    package = roster_construction_package(
        _TEAM, **_kwargs(need=need, timeline=Timeline.WIN_NOW)
    )
    assert package is not None
    assert package.number("starless_win_now_count") == 3


def test_starless_win_now_requires_at_least_three_positions():
    need = _grid()
    need[Position.PG][NeedTier.STAR] = 1.0
    need[Position.SG][NeedTier.STAR] = 1.0
    package = roster_construction_package(
        _TEAM, **_kwargs(need=need, timeline=Timeline.WIN_NOW)
    )
    assert package is None


def test_philosophy_mismatch_defense_first_fires_independently():
    package = roster_construction_package(
        _TEAM,
        **_kwargs(gm_philosophy=GmPhilosophy.DEFENSE_FIRST, measured_three_point_bias=75),
    )
    assert package is not None
    assert package.text("philosophy_mismatch") == "defense_first_high_variance"


def test_philosophy_mismatch_youth_developer_needs_an_aging_core():
    no_fire = roster_construction_package(
        _TEAM, **_kwargs(gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER, mean_age_top8=26.0)
    )
    assert no_fire is None
    fires = roster_construction_package(
        _TEAM, **_kwargs(gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER, mean_age_top8=30.0)
    )
    assert fires is not None
    assert fires.text("philosophy_mismatch") == "youth_developer_aging_core"


def test_a_firing_package_always_carries_the_paired_suggestion_facts():
    need = _grid()
    need[Position.C][NeedTier.STARTER] = 0.70
    package = roster_construction_package(_TEAM, **_kwargs(need=need))
    assert package is not None
    assert package.text("biggest_need_position") is not None
    assert package.text("biggest_glut_position") is not None


def test_multiple_rules_can_fire_on_the_same_package():
    need = _grid()
    need[Position.C][NeedTier.STARTER] = 0.70
    glut = _grid()
    glut[Position.PG][NeedTier.ROTATION] = 0.65
    package = roster_construction_package(_TEAM, **_kwargs(need=need, glut=glut))
    assert package is not None
    assert package.text("stark_need_position") is not None
    assert package.text("stark_glut_position") is not None
