"""`PlayerSituation` -- the caller-supplied per-checkpoint context Pillar
5's satisfaction evaluator scores against, plus the five axis scorers
that turn it into a signed `[-1.0, 1.0]` contribution per `MotivationAxis`.

docs/design/phase-4-media-and-player-agency.md §4, Decision P4-9.

Every field is `float | None`, and `None` is neutral, never zero --
RPD-6's degradation rule (`tools/_gm_progression_stub.py`), adopted
verbatim per P4-9's own text: a league with no signal yet (no games
simmed, no contract on file) should read as "no divergence from the
prior," not as the worst possible reading. A scorer therefore returns a
neutral `0.0` contribution for any axis whose feeding field(s) are all
`None` -- never a punitive value -- and, for a multi-field axis
(`role_usage`, `legacy`), averages over whichever component fields ARE
present rather than diluting a real signal to zero because its sibling
field is absent.

Built in `dba2/services/agency_service.py` (the DB-backed caller) and in
`tools/gm_harness/` (the in-memory caller) -- never inside this module,
which stays a pure dataclass plus pure scorers per P4-13's "the pure
evaluator never takes a repo" rule.

Zero I/O, same denylist `dba2/managers/` carries (see package
`__init__.py`).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from dba2.domain.motivation import MotivationAxis
from dba2.managers.tuning import CompetitiveWindow

# ---------------------------------------------------------------------------
# Disclosed, not derived -- same status as RPD-6's own `_FIT_SCALE`/
# `_FULL_MINUTES_ANCHOR` (`tools/advance_dev_league_season.py`): a
# reasonable scale with no real-world calibration behind it, named here
# rather than left implicit so a future tuning pass finds one place to
# look. None of these bump `GM_TUNING_VERSION` -- they live under
# `dba2/agency/`, not `managers/` (Decision P4-3's corollary).
# ---------------------------------------------------------------------------

_ROLE_FIT_SCALE = 2.0
"""Matches `tools/advance_dev_league_season.py::_FIT_SCALE` for the same
raw `need - glut` difference -- a reader who has seen one disclosed
scale for this exact quantity has seen the other."""

_TENURE_SEASONS_ANCHOR = 8.0
"""An eight-year tenure reads "fully settled" (loyalty score saturates at
`1.0`); a fresh acquisition reads `0.0`. Not derived from any real
loyalty-vs-tenure curve."""


_WINDOW_ORDER: tuple[CompetitiveWindow, ...] = (
    CompetitiveWindow.REBUILD,
    CompetitiveWindow.RETOOL,
    CompetitiveWindow.WIN_NOW,
    CompetitiveWindow.CONTEND,
)
"""`managers/window.py::_BAND_ORDER`, restated here rather than imported
(private name) -- the identical "worst-to-best, used only to normalize a
`CompetitiveWindow` into a `0..1` success score" convention `tools/
advance_dev_league_season.py::_WINDOW_ORDER` already uses for the same
signal (P4-9: "same as `_build_real_player_context`'s own success
signal"). Kept here, in the pure package, so BOTH callers (`services/
agency_service.py`'s DB path and `tools/gm_harness/`'s in-memory path)
normalize a window the identical way rather than maintaining two copies
that could silently drift."""

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def window_success_score(window: CompetitiveWindow | None) -> float | None:
    """`team_success`'s normalization: a `CompetitiveWindow` band index
    on `[0.0, 1.0]`. `None` in, `None` out -- the caller has not
    classified a window for this team yet (P4-9's "absent is neutral,
    never zero")."""
    if window is None:
        return None
    return _WINDOW_ORDER.index(window) / (len(_WINDOW_ORDER) - 1)


@dataclass(frozen=True)
class PlayerSituation:
    """One player's per-checkpoint context (Decision P4-9's table).
    `None` is the "no signal" state for every field -- see module
    docstring.

    | field             | which axis it feeds     |
    |-------------------|--------------------------|
    | `team_success`    | winning                  |
    | `role_fit`        | role_usage               |
    | `minutes_share`   | role_usage               |
    | `pay_vs_market`   | money                    |
    | `tenure_seasons`  | loyalty                  |
    | `market_match`    | legacy                   |
    | `team_accolades`  | legacy                   |
    """

    team_success: float | None = None
    role_fit: float | None = None
    minutes_share: float | None = None
    pay_vs_market: float | None = None
    tenure_seasons: float | None = None
    market_match: float | None = None
    team_accolades: float | None = None


def score_winning(situation: PlayerSituation) -> float:
    """`team_success` alone. Expected on `[0.0, 1.0]` (a normalized
    `CompetitiveWindow` band index -- `_WINDOW_ORDER.index(window) /
    (len(_WINDOW_ORDER) - 1)`, `tools/advance_dev_league_season.py`'s own
    convention for this exact signal, per P4-9's "same as
    `_build_real_player_context`'s own success signal"), re-centered to a
    signed `[-1, 1]` contribution around its own midpoint -- `0.5`
    (the `RETOOL`/`WIN_NOW` boundary) is neither good nor bad news for a
    player who was already there."""
    if situation.team_success is None:
        return 0.0
    return _clamp(2.0 * situation.team_success - 1.0, -1.0, 1.0)


def score_role_usage(situation: PlayerSituation) -> float:
    """`role_fit` and `minutes_share`, averaged over whichever are
    present (`None`-neutral per COMPONENT, not just per axis)."""
    parts: list[float] = []
    if situation.role_fit is not None:
        # role_fit is the raw `need - glut` grid value (P4-9: "raw grid
        # not need_bonus"), typically small (needs.py's own target table
        # tops out at 2.6) -- _ROLE_FIT_SCALE keeps a typical reading
        # inside [-1, 1] without claiming real calibration.
        parts.append(_clamp(situation.role_fit / _ROLE_FIT_SCALE, -1.0, 1.0))
    if situation.minutes_share is not None:
        # minutes_share is itself already ~[0, 1.2] (RPD-6's own anchor
        # allows modest overshoot); 0.5 is the "everyday rotation player"
        # midpoint, not a real distributional median.
        parts.append(_clamp(2.0 * situation.minutes_share - 1.0, -1.0, 1.0))
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


def score_money(situation: PlayerSituation) -> float:
    """`pay_vs_market` alone. Expected as a ratio (`salary / market_
    value`) centered on `1.0` -- paid exactly market value is neutral,
    underpaid (`< 1.0`) a dissatisfaction pressure, overpaid (`> 1.0`) a
    satisfaction one."""
    if situation.pay_vs_market is None:
        return 0.0
    return _clamp(situation.pay_vs_market - 1.0, -1.0, 1.0)


def score_loyalty(situation: PlayerSituation) -> float:
    """`tenure_seasons` alone. Monotonically increasing and saturating,
    `[0, 1]` -- a rookie season and a ten-year tenure are not symmetric
    around some imagined "neutral" tenure the way pay/winning are around
    market value/.500, so this is a plain non-negative reading, `0.0` at
    a fresh acquisition, not a signed contribution around a midpoint."""
    if situation.tenure_seasons is None:
        return 0.0
    return _clamp(situation.tenure_seasons / _TENURE_SEASONS_ANCHOR, 0.0, 1.0)


def score_legacy(situation: PlayerSituation) -> float:
    """`market_match` and `team_accolades`, averaged over whichever are
    present, same `None`-per-component rule as `score_role_usage`. Both
    are expected pre-scored on `[0, 1]` by the caller and re-centered
    around `0.5` here, the same way `score_winning` re-centers its own
    `[0, 1]` input -- "no better or worse than expected" (`0.5`) is a
    real reading, not just an absent one."""
    parts: list[float] = []
    if situation.market_match is not None:
        parts.append(_clamp(2.0 * situation.market_match - 1.0, -1.0, 1.0))
    if situation.team_accolades is not None:
        parts.append(_clamp(2.0 * situation.team_accolades - 1.0, -1.0, 1.0))
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


AxisScorer = Callable[[PlayerSituation], float]

AXIS_SCORERS: Mapping[MotivationAxis, AxisScorer] = MappingProxyType(
    {
        MotivationAxis.WINNING: score_winning,
        MotivationAxis.ROLE_USAGE: score_role_usage,
        MotivationAxis.LOYALTY: score_loyalty,
        MotivationAxis.MONEY: score_money,
        MotivationAxis.LEGACY: score_legacy,
    }
)
"""Every `MotivationAxis` mapped to its scorer -- `motivation.py`'s
evaluator iterates this rather than hardcoding the five calls, so a
sixth axis (should one ever be added) fails loudly at the `MotivationAxis`
enum boundary rather than silently going unscored.

**Iteration order matches `MotivationAxis` DECLARATION order exactly**
(`WINNING, ROLE_USAGE, LOYALTY, MONEY, LEGACY`) -- round-5 critic finding
B3: this dict previously listed `MONEY` before `LOYALTY`, contradicting
`evaluate_satisfaction`'s own docstring claim ("iterates `AXIS_SCORERS`
in `MotivationAxis` declaration order") and `largest_magnitude_axis`'s
tie-break, which reads the real enum order. Emission order is part of
the contract, not an implementation accident (P4-44) -- a future axis
addition or reordering must keep this dict's key order identical to
`MotivationAxis`'s own, and `tests/unit/test_agency_situation.py` asserts
that order-sensitively, not by set equality."""
