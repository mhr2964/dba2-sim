"""Shared fixtures for the broadcast tests (`test_broadcast_plan.py`'s corpus
and `test_broadcast_render.py`'s goldens): real `Team` rows and a
deterministic player-id -> surname table, so a rendered line is readable when
a golden diff is reviewed by eye. No DB, no Discord."""
from __future__ import annotations

import sys
import types
from pathlib import Path

from dba2.domain.team import GmPhilosophy, Team, Timeline, TimelineSource
from dba2.presentation import broadcast
from dba2.sim.events import GameResult

# Invented surnames, one non-ASCII pair so an encoding slip in a golden shows.
_SURNAMES = (
    "Okafor", "Lindqvist", "Marchetti", "Adeyemi", "Kowalczyk", "Tanaka",
    "Bellweather", "Nkemelu", "Ferreira", "Öztürk", "Halvorsen", "Delacroix",
    "Vasquez", "Mbeki", "Ashworth", "Rasmussen", "Quintero", "Dončić-Ray",
    "Petrov", "Whitfield", "Abernathy", "Sørensen", "Castellano", "Uchenna",
)


def make_team(team_id: int, code: str) -> Team:
    return Team(
        id=team_id, league_id=1, code=code, city=f"City{team_id}", name=f"Team{team_id}",
        conference="East", division="Atlantic", manager_user_id=None,
        gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.WIN_NOW, home_market_size=5,
        timeline_source=TimelineSource.AUTO,
    )


def names_for(result: GameResult) -> dict[int, str]:
    """Deterministic surnames for every player who appeared, in box-line
    order (home roster first, as the engine emits it)."""
    return {
        line.player_id: _SURNAMES[position % len(_SURNAMES)]
        for position, line in enumerate(result.player_box_lines)
    }


def broadcast_variant(*mutations: tuple[str, str]) -> types.ModuleType:
    """A private copy of `presentation/broadcast.py`'s source with each
    `(old, new)` mutation applied, loaded under a scratch module name so the
    real module is untouched. A mutation anchor must occur exactly once: a
    twin that silently mutates nothing proves nothing."""
    source = Path(broadcast.__file__).read_text(encoding="utf-8")
    for old, new in mutations:
        assert source.count(old) == 1, f"mutation anchor {old!r} is not unique in the source"
        source = source.replace(old, new)
    module = types.ModuleType("_broadcast_variant")
    sys.modules[module.__name__] = module  # `@dataclass` resolves annotations through it
    try:
        exec(compile(source, "<broadcast variant>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


def narration_for(plan, result, *, home, away, names, module=broadcast):
    """The game rendered once as lines at the plan's own narration level --
    `render_live_component`'s `narration` argument (docs/design/watch-narration-v2.md
    WN-8), built the way `/watch` and `bot/broadcast.py::pace_broadcast` build it."""
    return module.plan_narration(result, plan, home=home, away=away, names=names)
