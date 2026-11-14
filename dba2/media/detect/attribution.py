"""`dba2/media/detect/attribution.py` -- docs/design/media-v2-newsroom.md
MV-2c's "why": `explain_form(window, baseline, ...) -> tuple[Fact, ...]`,
pure, plus MV-2d's `explain_defense` analogue.

Each MV-2c attribution requires BOTH a real shot-profile shift over the
SAME `window`/`baseline` `dba2/media/detect/form.py` already computes AND a
real rating at or above a stated bar -- neither alone emits anything. **If
nothing qualifies, this function returns `()`, and the composing caller
(`form.py::hot_or_cold_package`'s own `attribution_facts` parameter) then
carries zero attribution facts** -- MV-2c's own text: "the scanner never
invents a cause." MV-2d's `explain_defense` needs only the rating gate
(no shot-profile-shift pair is named for it).

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media_facts`, `dba2.
domain.stats`, `dba2.media.detect.form` and stdlib.
"""

from __future__ import annotations

from dba2.domain.media_facts import Fact, FactSource
from dba2.domain.stats import PlayerFormWindow
from dba2.media.detect.form import Baseline, per36

_FORM_RATING_BAR = 75
"""MV-2c's own gate (`explain_form`, `:1063-1066`) -- disclosed, not
calibrated, the same status as every other threshold in this design."""

_DEFENSE_RATING_BAR = 78
"""MV-2d's own gate (`explain_defense`, `:1077-1079`) -- **78, not 75.**
`on_ball_defense`/`rim_protection`/`defensive_rebounding` are each named at
`>= 78` in the design doc's own MV-2d table, three times, unambiguously; 75
is `explain_form`'s bar (`_FORM_RATING_BAR`), not this one. A prior revision
of this module used one shared `_RATING_BAR = 75` for both functions and
mislabeled it as MV-2d's own number -- corrected here rather than left, per
this project's naming rule (a name/value that misrepresents its subject
gets fixed, not noted)."""

_RIM_SHARE_DELTA = 0.08
_FTA_PER_GAME_DELTA = 1.5
_AST_PER36_DELTA = 2.0
_THREE_VOLUME_RATIO = 1.25


def _shot_fact(key: str, label: str, value: float) -> Fact:
    return Fact(
        key=key, label=label, value_text=f"{value:.3f}", value_number=value,
        source=FactSource.PLAYER_GAME_STATS,
    )


def explain_form(
    window: PlayerFormWindow,
    baseline: Baseline,
    *,
    catch_shoot_3: int,
    off_dribble_3: int,
    finishing_rim: int,
    first_step: int,
    draw_foul: int,
    passing_vision: int,
) -> tuple[Fact, ...]:
    """MV-2c's four-row table, each row independent (a player can qualify
    for more than one). Denominators of `0` (an ineligible or degenerate
    window -- `form.py`'s own eligibility gate makes this practically
    unreachable, but this function does not assume its caller checked)
    read as "no shift," never a `ZeroDivisionError`."""
    facts: list[Fact] = []
    if window.window_games <= 0 or baseline.games <= 0:
        return ()

    window_three_pg = window.window_fga_three / window.window_games
    baseline_three_pg = baseline.fga_three / baseline.games
    window_three_pct = (
        window.window_fgm_three / window.window_fga_three if window.window_fga_three > 0 else None
    )
    baseline_three_pct = baseline.fgm_three / baseline.fga_three if baseline.fga_three > 0 else None
    if (
        window_three_pct is not None
        and baseline_three_pct is not None
        and window_three_pg >= _THREE_VOLUME_RATIO * baseline_three_pg
        and window_three_pct >= baseline_three_pct
        and (catch_shoot_3 >= _FORM_RATING_BAR or off_dribble_3 >= _FORM_RATING_BAR)
    ):
        facts.append(
            _shot_fact(
                "attribution_three_point_volume", "Three-point volume shift", window_three_pg
            )
        )

    window_fga_total = window.window_fga_rim + window.window_fga_mid + window.window_fga_three
    baseline_fga_total = baseline.fga_rim + baseline.fga_mid + baseline.fga_three
    if window_fga_total > 0 and baseline_fga_total > 0:
        window_rim_share = window.window_fga_rim / window_fga_total
        baseline_rim_share = baseline.fga_rim / baseline_fga_total
        if (
            window_rim_share >= baseline_rim_share + _RIM_SHARE_DELTA
            and (finishing_rim >= _FORM_RATING_BAR or first_step >= _FORM_RATING_BAR)
        ):
            facts.append(
                _shot_fact("attribution_rim_pressure", "Rim-pressure shift", window_rim_share)
            )

    window_fta_pg = window.window_fta / window.window_games
    baseline_fta_pg = baseline.fta / baseline.games
    if window_fta_pg >= baseline_fta_pg + _FTA_PER_GAME_DELTA and draw_foul >= _FORM_RATING_BAR:
        facts.append(_shot_fact("attribution_free_throws", "Free-throw-rate shift", window_fta_pg))

    window_ast36 = per36(window.window_ast, window.window_minutes)
    baseline_ast36 = per36(baseline.ast, baseline.minutes)
    window_tov36 = per36(window.window_tov, window.window_minutes)
    baseline_tov36 = per36(baseline.tov, baseline.minutes)
    if (
        window_ast36 is not None
        and baseline_ast36 is not None
        and window_tov36 is not None
        and baseline_tov36 is not None
        and window_ast36 >= baseline_ast36 + _AST_PER36_DELTA
        and window_tov36 <= baseline_tov36
        and passing_vision >= _FORM_RATING_BAR
    ):
        facts.append(_shot_fact("attribution_playmaking", "Playmaking shift", window_ast36))

    return tuple(facts)


def explain_defense(
    window: PlayerFormWindow,
    *,
    on_ball_defense: int,
    rim_protection: int,
    defensive_rebounding: int,
) -> tuple[Fact, ...]:
    """MV-2d's own three attribution gates: steals-led (`on_ball_defense`),
    blocks-led (`rim_protection`), dreb-led (`defensive_rebounding`) -- each
    independent, each gated on its own rating at `>= 78` (`_DEFENSE_RATING_
    BAR`, not `explain_form`'s `75`), no shot-profile shift required (MV-2d
    names only the rating bar, unlike MV-2c's own shot-profile-AND-rating
    pair). Reads the WINDOW rate only -- the rating gate is what makes it
    "led," not a second baseline comparison
    `form.py::defensive_standout_package` already made once."""
    facts: list[Fact] = []
    if window.window_minutes <= 0:
        return ()

    window_stl36 = per36(window.window_stl, window.window_minutes)
    window_blk36 = per36(window.window_blk, window.window_minutes)
    window_dreb36 = per36(window.window_dreb, window.window_minutes)
    if window_stl36 is not None and on_ball_defense >= _DEFENSE_RATING_BAR:
        facts.append(_shot_fact("attribution_on_ball_defense", "Steals-led", window_stl36))
    if window_blk36 is not None and rim_protection >= _DEFENSE_RATING_BAR:
        facts.append(_shot_fact("attribution_rim_protection", "Blocks-led", window_blk36))
    if window_dreb36 is not None and defensive_rebounding >= _DEFENSE_RATING_BAR:
        facts.append(
            _shot_fact("attribution_defensive_rebounding", "Rebounding-led", window_dreb36)
        )
    return tuple(facts)
