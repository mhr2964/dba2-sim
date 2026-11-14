"""`dba2/media/detect/construction.py` -- docs/design/media-v2-newsroom.md
MV-2e: `ROSTER_CONSTRUCTION`, a pure predicate over `needs.build_need_
matrix`'s own already-computed `NeedMatrix` (`dba2/managers/needs.py`,
reused unchanged) plus a small amount of team-level context.

Pure: fed by already-loaded data. A future loader in `dba2/services/
media_service.py` calls `gm_service.load_gm_inputs`/`needs.build_need_
matrix` (both already exist, called once per checkpoint today) and
`scouting.player_age` (also already exists) and hands this module their
results -- this module never reads a database or imports `dba2.managers.*`
functions itself, only the plain `Position`/`NeedTier`/`Timeline`/
`GmPhilosophy` value types those modules already return.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.phase`, `dba2.domain.player`, `dba2.domain.team`
and stdlib.
"""

from __future__ import annotations

from collections.abc import Mapping

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy, Timeline

STARK_NEED_FLOOR = 0.60
STARK_GLUT_FLOOR = 0.60
AGING_CORE_MEAN_AGE = 29.0
STARLESS_POSITIONS_REQUIRED = 3
PHILOSOPHY_THREE_POINT_BIAS_FLOOR = 70
"""MV-2e's own five thresholds, verbatim -- disclosed, not calibrated, same
status as every other threshold in this design."""


def _text_fact(key: str, label: str, value: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value, value_number=None, source=source)


def _float_fact(key: str, label: str, value: float, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=f"{value:.3f}", value_number=value, source=source)


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _biggest(
    grid: Mapping[Position, Mapping[object, float]], tier: object
) -> tuple[Position, float]:
    """The position with the largest `grid[pos][tier]` value -- ties broken
    by `Position`'s own string value (`StrEnum` comparison), deterministic
    and reproducible, no RNG. Which specific position wins a tie is not
    itself a claim this design makes; only that it is always the same one
    for the same input."""
    return max(((pos, grid[pos][tier]) for pos in Position), key=lambda pair: (pair[1], pair[0]))


def roster_construction_package(
    team_id: int,
    *,
    need: Mapping[Position, Mapping[object, float]],
    glut: Mapping[Position, Mapping[object, float]],
    supply: Mapping[Position, Mapping[object, float]],
    starter_tier: object,
    rotation_tier: object,
    star_tier: object,
    mean_age_top8: float,
    timeline: Timeline,
    gm_philosophy: GmPhilosophy,
    measured_three_point_bias: int,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """MV-2e: notable when ANY of five rules hold. `starter_tier`/
    `rotation_tier`/`star_tier` are the caller's own `dba2.managers.needs.
    NeedTier` members, passed as plain values rather than imported here --
    `dba2/media/` importing `dba2.managers.needs` would be the one real
    cross-package coupling this module can avoid entirely, since every
    real caller already HAS those three enum members in hand from the very
    `NeedMatrix` it is about to pass in.

    Two grounding facts (`biggest_need_position`/`biggest_glut_position`,
    "the biggest single need value on the grid" / "the position with the
    largest glut") are ALWAYS included once any rule fires -- MV-2e's own
    "paired, data-derived suggestion" rule, computed once here rather than
    per-rule, since the suggestion is the same regardless of which rule
    made the team notable."""
    stark_need_pos, stark_need_val = _biggest(need, starter_tier)
    stark_glut_pos, stark_glut_val = _biggest(glut, rotation_tier)
    biggest_need_pos, biggest_need_val = _biggest(need, starter_tier)
    biggest_glut_pos, biggest_glut_val = _biggest(glut, rotation_tier)

    facts: list[Fact] = []

    if stark_need_val >= STARK_NEED_FLOOR:
        facts.append(
            _text_fact("stark_need_position", "Position of stark need", stark_need_pos.value,
                        FactSource.NEED_MATRIX)
        )
        facts.append(
            _float_fact("stark_need_value", "Need value", stark_need_val, FactSource.NEED_MATRIX)
        )
        facts.append(
            _float_fact(
                "stark_need_supply", "Current supply", supply[stark_need_pos][starter_tier],
                FactSource.NEED_MATRIX,
            )
        )

    if stark_glut_val >= STARK_GLUT_FLOOR:
        facts.append(
            _text_fact("stark_glut_position", "Position of stark glut", stark_glut_pos.value,
                        FactSource.NEED_MATRIX)
        )
        facts.append(
            _float_fact("stark_glut_value", "Glut value", stark_glut_val, FactSource.NEED_MATRIX)
        )

    aging_core = mean_age_top8 >= AGING_CORE_MEAN_AGE
    if aging_core and timeline is Timeline.REBUILD:
        facts.append(
            _float_fact("aging_core_mean_age", "Mean age, top 8", mean_age_top8,
                         FactSource.PLAYER_RATINGS)
        )

    starless_positions = sum(1 for pos in Position if need[pos][star_tier] == 1.0)
    if timeline is Timeline.WIN_NOW and starless_positions >= STARLESS_POSITIONS_REQUIRED:
        facts.append(
            _int_fact("starless_win_now_count", "Positions with zero star coverage",
                       starless_positions, FactSource.NEED_MATRIX)
        )

    philosophy_mismatch: str | None = None
    if (
        gm_philosophy is GmPhilosophy.DEFENSE_FIRST
        and measured_three_point_bias >= PHILOSOPHY_THREE_POINT_BIAS_FLOOR
    ):
        philosophy_mismatch = "defense_first_high_variance"
    elif gm_philosophy is GmPhilosophy.YOUTH_DEVELOPER and aging_core:
        philosophy_mismatch = "youth_developer_aging_core"
    if philosophy_mismatch is not None:
        facts.append(
            _text_fact("philosophy_mismatch", "Declared philosophy vs. roster shape",
                        philosophy_mismatch, FactSource.TEAM_STRATEGY)
        )

    if not facts:
        return None

    facts = [
        _text_fact("biggest_need_position", "Biggest single need", biggest_need_pos.value,
                    FactSource.NEED_MATRIX),
        _float_fact("biggest_need_value", "Biggest need value", biggest_need_val,
                     FactSource.NEED_MATRIX),
        _text_fact("biggest_glut_position", "Biggest glut", biggest_glut_pos.value,
                    FactSource.NEED_MATRIX),
        _float_fact("biggest_glut_value", "Biggest glut value", biggest_glut_val,
                     FactSource.NEED_MATRIX),
        *facts,
    ]
    return StoryPackage(
        story_kind=StoryKind.ROSTER_CONSTRUCTION,
        angle=Angle.ROSTER_IMBALANCE,
        subject=Subject(kind=SubjectKind.TEAM, team_id=team_id),
        facts=tuple(facts),
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
