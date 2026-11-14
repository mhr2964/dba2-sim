"""`dba2/media/detect/form.py` -- docs/design/media-v2-newsroom.md MV-2c/2d:
`HOT_STREAK`/`COLD_STRETCH` (a live 5-game window vs. a per-36 baseline) and
`DEFENSIVE_STANDOUT` (the identical window, a stocks-per-36 reading).

Pure: fed by `stats_repo.list_recent_form_windows`'s own already-loaded
`PlayerFormWindow` rows (`dba2.domain.stats`, built in an earlier build
step) -- a future loader in `dba2/services/media_service.py` is what
actually calls that repo function; this module never reads a database.

**The comparison is window vs. `baseline`, never window vs. season**
(MV-2c's own revision-4 correction). `baseline = season - window`, column
by column, computed by subtraction here -- exact, since the window is a
strict subset of the season aggregate, and free: no SQL change, no extra
query. `_baseline` is this module's one producer of that quantity; both
detectors below and `dba2/media/detect/attribution.py` read it, never a
second, independently-subtracted copy.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.phase`, `dba2.domain.stats` and stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase
from dba2.domain.stats import PlayerFormWindow

WINDOW_GAMES_REQUIRED = 5
MIN_SEASON_GAMES = 15
MIN_WINDOW_MPG = 20.0
"""MV-2c's own eligibility rule, verbatim -- disclosed, not calibrated:
`window_games == 5`, `season_games >= 15` (so `baseline_games >= 10`, a
real second sample, never a near-empty one), `window_minutes / 5 >= 20.0`
(a bench cameo does not qualify)."""

HOT_PER36_DELTA = 6.0
HOT_TS_DELTA = 0.040
HOT_TS_ABSOLUTE = 0.600
COLD_PER36_DELTA = 6.0
COLD_TS_DELTA = 0.040
"""MV-2c's own thresholds, verbatim. HOT: `per36(window_points) >=
per36(baseline_points) + 6.0` AND (`window_ts >= baseline_ts + 0.040` OR
`window_ts >= 0.600`). COLD: `per36(window_points) <= per36(baseline_
points) - 6.0` AND `window_ts <= baseline_ts - 0.040`."""

DEFENSE_STOCKS_PER36_DELTA = 1.5
DEFENSE_STOCKS_PER36_ABSOLUTE = 4.0
"""MV-2d's own thresholds, verbatim, on the SAME window/eligibility/
baseline-correction as MV-2c: `per36(window_stl + window_blk) >=
per36(baseline_stl + baseline_blk) + 1.5` AND `per36(window_stl +
window_blk) >= 4.0`."""


@dataclass(frozen=True)
class Baseline:
    """`season - window`, column by column -- see module docstring. Only
    the columns MV-2c/2d/`attribution.py` actually read; `PlayerFormWindow`
    itself carries the rest."""

    games: int
    points: float
    minutes: float
    fga_rim: int
    fga_mid: int
    fga_three: int
    fgm_three: int
    fta: int
    ast: int
    tov: int
    stl: int
    blk: int


def compute_baseline(window: PlayerFormWindow) -> Baseline:
    return Baseline(
        games=window.season_games - window.window_games,
        points=window.season_points - window.window_points,
        minutes=window.season_minutes - window.window_minutes,
        fga_rim=window.season_fga_rim - window.window_fga_rim,
        fga_mid=window.season_fga_mid - window.window_fga_mid,
        fga_three=window.season_fga_three - window.window_fga_three,
        fgm_three=window.season_fgm_three - window.window_fgm_three,
        fta=window.season_fta - window.window_fta,
        ast=window.season_ast - window.window_ast,
        tov=window.season_tov - window.window_tov,
        stl=window.season_stl - window.window_stl,
        blk=window.season_blk - window.window_blk,
    )


def is_eligible(window: PlayerFormWindow) -> bool:
    """MV-2c's own eligibility rule -- shared by HOT/COLD/DEFENSIVE_STANDOUT
    (MV-2d: "same eligibility")."""
    if window.window_games != WINDOW_GAMES_REQUIRED:
        return False
    if window.season_games < MIN_SEASON_GAMES:
        return False
    return (window.window_minutes / WINDOW_GAMES_REQUIRED) >= MIN_WINDOW_MPG


def per36(value: float, minutes: float) -> float | None:
    """`None` on a zero-or-negative minutes denominator -- MV-2c's own
    zero-denominator guard: "that reading is absent, and an absent reading
    never fires a kind." Never a `ZeroDivisionError`."""
    if minutes <= 0:
        return None
    return value / minutes * 36.0


def true_shooting(
    points: float, fga_rim: int, fga_mid: int, fga_three: int, fta: int
) -> float | None:
    """`None` on a zero-shot denominator (MV-2c's own "a 0-shot 5-game
    window ... is unlikely and not impossible; 'unlikely' is not a
    guard.\")."""
    denominator = 2 * (fga_rim + fga_mid + fga_three + 0.44 * fta)
    if denominator <= 0:
        return None
    return points / denominator


def _float_fact(key: str, label: str, value: float, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=f"{value:.3f}", value_number=value, source=source)


def hot_or_cold_package(
    window: PlayerFormWindow,
    *,
    season: int,
    phase: Phase,
    cycle_index: int,
    attribution_facts: tuple[Fact, ...] = (),
) -> StoryPackage | None:
    """`HOT_STREAK`, `COLD_STRETCH`, or `None` (ineligible, a zero-
    denominator absent reading, or neither threshold clears) -- never both
    kinds for the same window (the two threshold directions cannot both
    hold at once).

    **`attribution_facts` -- MV-2c's "why," composed here rather than
    inside `dba2/media/detect/attribution.py` itself.** That module's own
    `explain_form` is a standalone pure function over the identical
    `window`/`baseline` this function already computes; a caller
    (`dba2/services/media_service.py`'s future loader, or a test) calls it
    first and passes the result in, so this function stays the single
    producer of the package while `explain_form` stays the single producer
    of the attribution facts -- never two independently-computed copies of
    either. **Only ever appended to a `HOT_STREAK` result** (MV-2c's own
    attribution table is framed entirely as reasons a player is scoring
    MORE; a `COLD_STRETCH` package never carries one, disclosed narrowing).
    Empty by default, matching "if nothing qualifies, the package carries
    zero attribution facts" for a caller that never computed any."""
    if not is_eligible(window):
        return None
    baseline = compute_baseline(window)

    window_p36 = per36(window.window_points, window.window_minutes)
    baseline_p36 = per36(baseline.points, baseline.minutes)
    window_ts = true_shooting(
        window.window_points, window.window_fga_rim, window.window_fga_mid,
        window.window_fga_three, window.window_fta,
    )
    baseline_ts = true_shooting(
        baseline.points, baseline.fga_rim, baseline.fga_mid, baseline.fga_three, baseline.fta,
    )
    if window_p36 is None or baseline_p36 is None or window_ts is None or baseline_ts is None:
        return None

    is_hot = window_p36 >= baseline_p36 + HOT_PER36_DELTA and (
        window_ts >= baseline_ts + HOT_TS_DELTA or window_ts >= HOT_TS_ABSOLUTE
    )
    is_cold = (
        window_p36 <= baseline_p36 - COLD_PER36_DELTA and window_ts <= baseline_ts - COLD_TS_DELTA
    )
    if not is_hot and not is_cold:
        return None

    kind = StoryKind.HOT_STREAK if is_hot else StoryKind.COLD_STRETCH
    angle = Angle.HEATING_UP if is_hot else Angle.GOING_COLD
    facts = (
        _float_fact("window_points_per36", "Points per 36, last 5", window_p36,
                     FactSource.PLAYER_GAME_STATS),
        _float_fact("baseline_points_per36", "Points per 36, rest of season", baseline_p36,
                     FactSource.PLAYER_GAME_STATS),
        _float_fact("window_true_shooting", "True shooting, last 5", window_ts,
                     FactSource.PLAYER_GAME_STATS),
        _float_fact("baseline_true_shooting", "True shooting, rest of season", baseline_ts,
                     FactSource.PLAYER_GAME_STATS),
        *(attribution_facts if is_hot else ()),
    )
    return StoryPackage(
        story_kind=kind,
        angle=angle,
        subject=Subject(kind=SubjectKind.PLAYER, player_id=window.player_id),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )


def defensive_standout_package(
    window: PlayerFormWindow,
    *,
    season: int,
    phase: Phase,
    cycle_index: int,
    attribution_facts: tuple[Fact, ...] = (),
) -> StoryPackage | None:
    """MV-2d: the same window/eligibility/baseline-correction as MV-2c,
    reading `stl + blk` ("stocks") instead of points. `attribution_facts`
    mirrors `hot_or_cold_package`'s own parameter of the same name -- see
    that function's docstring for the composition reasoning
    (`dba2/media/detect/attribution.py::explain_defense` is the real
    producer, called by whoever wires the two together)."""
    if not is_eligible(window):
        return None
    baseline = compute_baseline(window)

    window_stocks_p36 = per36(window.window_stl + window.window_blk, window.window_minutes)
    baseline_stocks_p36 = per36(baseline.stl + baseline.blk, baseline.minutes)
    if window_stocks_p36 is None or baseline_stocks_p36 is None:
        return None

    if not (
        window_stocks_p36 >= baseline_stocks_p36 + DEFENSE_STOCKS_PER36_DELTA
        and window_stocks_p36 >= DEFENSE_STOCKS_PER36_ABSOLUTE
    ):
        return None

    facts = (
        _float_fact("window_stocks_per36", "Steals + blocks per 36, last 5", window_stocks_p36,
                     FactSource.PLAYER_GAME_STATS),
        _float_fact(
            "baseline_stocks_per36", "Steals + blocks per 36, rest of season",
            baseline_stocks_p36, FactSource.PLAYER_GAME_STATS,
        ),
        *attribution_facts,
    )
    return StoryPackage(
        story_kind=StoryKind.DEFENSIVE_STANDOUT,
        angle=Angle.LOCKDOWN,
        subject=Subject(kind=SubjectKind.PLAYER, player_id=window.player_id),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
