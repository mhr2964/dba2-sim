"""`BroadcastSettings`, the three `/watch` speeds and their presets (Stage D commit 7,
docs/design/phase-5-stage-d-broadcast.md §6 P5-97.h, §10 mutation twin (k);
docs/design/watch-narration-v2.md WN-10.4 for the speeds).

The twin -- deleting the `tick_seconds > 0` check -- is shown red by loading a
mutated private copy of the module (`_broadcast_fakes.variant`) and asserting
the same scenario that passes on the real one fails on it. The control test
loads the copy with NO mutation, so "red" cannot just be a loading failure.
"""

from __future__ import annotations

import dataclasses
import math
import statistics

import pytest

from dba2.bot import broadcast
from dba2.bot.broadcast import (
    DEFAULT_BROADCAST_SETTINGS,
    FAST_FORWARD,
    WATCH_ALONG,
    BroadcastSettings,
    WatchSpeed,
    settings_for_speed,
)
from dba2.presentation.broadcast import (
    FAST_PLAN_SETTINGS,
    FULL_PLAN_SETTINGS,
    GAME_PLAN_SETTINGS,
    PlanSettings,
    RevealUnit,
    reveal_schedule,
    ticker_lines,
)
from tests.unit.bot._broadcast_fakes import narration_for, plan_with, variant

_PRESETS = {"fast": FAST_FORWARD, "game": DEFAULT_BROADCAST_SETTINGS, "full": WATCH_ALONG}
_CLICK_COOLDOWN_SECONDS = 4.0  # `watch_pager._CLICK_COOLDOWN_FLOOR_SECONDS`, mirrored


def test_the_default_is_the_game_speed():
    assert DEFAULT_BROADCAST_SETTINGS == BroadcastSettings()
    s = DEFAULT_BROADCAST_SETTINGS
    assert s.plan == GAME_PLAN_SETTINGS and s.plan.reveal is RevealUnit.LINE
    assert (s.tick_seconds, s.plan.possessions_per_frame) == (1.5, 2)
    assert (s.max_concurrent_broadcasts, s.max_queue_depth) == (4, 12)
    assert s.max_live_broadcasts_per_user == 2
    assert (s.edit_timeout_seconds, s.interrupted_edit_timeout_seconds) == (10.0, 5.0)
    assert s.final_edit_attempts == 3


def test_the_presets_differ_from_the_default_only_in_tick_and_plan():
    for preset, tick, plan in (
        (FAST_FORWARD, 1.5, FAST_PLAN_SETTINGS),
        (WATCH_ALONG, 1.5, FULL_PLAN_SETTINGS),
    ):
        assert (preset.tick_seconds, preset.plan) == (tick, plan)
        assert (
            dataclasses.replace(preset, tick_seconds=1.5, plan=DEFAULT_BROADCAST_SETTINGS.plan)
            == DEFAULT_BROADCAST_SETTINGS
        )
    # WN-17: fast's line cap went from 96 to 240 (the user's call), so names are no longer
    # cut to two letters; its batching (4 a tick) is unchanged.
    assert FAST_FORWARD.plan == PlanSettings(possessions_per_frame=4, max_pbp_line_chars=240)


def test_a_speed_changes_only_the_tick_and_the_plan_of_the_schedulers_settings():
    """WN-10.4: `game` is the scheduler's own settings; `fast` and `full` swap in their
    tick and plan and keep every admission, timeout and FINAL-ladder knob of the base."""
    base = BroadcastSettings(max_concurrent_broadcasts=2, edit_timeout_seconds=3.0)
    assert settings_for_speed(base, WatchSpeed.GAME) is base
    for speed, preset in ((WatchSpeed.FAST, FAST_FORWARD), (WatchSpeed.FULL, WATCH_ALONG)):
        paced = settings_for_speed(base, speed)
        assert (paced.tick_seconds, paced.plan) == (preset.tick_seconds, preset.plan)
        assert dataclasses.replace(paced, tick_seconds=base.tick_seconds, plan=base.plan) == base


@pytest.mark.parametrize("name", sorted(_PRESETS))
def test_every_preset_tick_is_at_least_the_1_5_second_floor(name):
    """watch-v5 WT-1: the 1.5s floor is a PRESET rule, not a validator (`test_a_bad_field_
    is_refused_and_named` still accepts `tick_seconds=0.001` below), so it is pinned here,
    over the presets, instead. A reading mode reveals by line, and its per-line floor is
    `read_min_seconds` (WN-10.4, A9: never below 2.0 s)."""
    preset = _PRESETS[name]
    assert preset.tick_seconds >= 1.5
    if preset.plan.reveal is RevealUnit.LINE:
        assert preset.plan.read_min_seconds >= 2.0


@pytest.mark.parametrize("name", sorted(_PRESETS))
def test_no_preset_replaces_more_than_half_the_ticker_in_one_tick(name):
    """watch-v5 WT-1/WT-3: the complaint this design fixes, pinned as a value so a
    regression is caught rather than re-derived. Mutation: restore `possessions_per_frame
    = 6` at FAST -> red. In a reading mode a reveal is ONE line, so it trivially holds."""
    preset = _PRESETS[name]
    increment = 1 if preset.plan.reveal is RevealUnit.LINE else preset.plan.possessions_per_frame
    assert increment <= 4
    assert ticker_lines(preset.plan) >= 2 * increment


def _schedule(preset: BroadcastSettings):
    plan = plan_with(preset.plan)
    return reveal_schedule(plan, narration_for(plan), tick_seconds=preset.tick_seconds)


# WN-10.4's table for a corpus-mean game (211 possessions): fast 90 s, game 780 s, full
# 2,020 s. The fixture is an overtime game, so the 0.7-1.5x band is generous -- it catches
# a preset off by a factor, not by a line. The builder's measured FULL mean is ~2,630 s
# (seeds 0-19, 43.8 min), inside the band but above the design's 33.7 min: its chain lines
# run ~70 characters against the design's 40-55 estimate (see the As-built).
@pytest.mark.parametrize(
    ("name", "table_seconds"), [("fast", 90.0), ("game", 780.0), ("full", 2020.0)]
)
def test_a_real_game_lasts_about_what_the_design_table_says(name, table_seconds):
    duration = _schedule(_PRESETS[name])[-1].due_seconds
    assert 0.7 * table_seconds <= duration <= 1.5 * table_seconds


@pytest.mark.parametrize("name", ["game", "full"])
def test_a_reading_mode_edits_less_often_than_the_old_default_and_never_under_the_floor(name):
    """A9 and WN-10.4's rate column: one line per edit, never two edits closer than the
    2.0 s floor, and the per-message rate -- pacer plus worst-case clicks at the 4 s
    cooldown -- at or under 3.1 edits per 5 s on the MEAN (the old default ran 3.3 + 1.25 =
    4.6). The reading pace was shortened before the first run (base 0.5 s, 0.035 s per
    character): full's mean rate moved from 2.64 to 3.00; the WORST window is still
    pinned at 5 by the floor below."""
    dues = [reveal.due_seconds for reveal in _schedule(_PRESETS[name])]
    gaps = [b - a for a, b in zip(dues, dues[1:], strict=False)]
    # A difference of two cumulative float sums: an exact 2.0 s hold can read 1.9999999999999.
    assert min(gaps) >= 2.0 - 1e-9
    per_five = 5.0 / statistics.mean(gaps) + 5.0 / _CLICK_COOLDOWN_SECONDS
    assert per_five <= 3.1, per_five


@pytest.mark.parametrize(("name", "worst"), [("fast", 6), ("game", 5), ("full", 5)])
def test_the_worst_five_second_window_is_what_the_docstring_says(name, worst):
    """WN-17 (the critic's nit on WN-10.4's "peak"): the per-message rate above is a MEAN.
    The worst 5-second window of a real schedule holds 4 fast reveals (a 1.5 s tick) or 3
    reading-mode reveals (the 2.0 s floor), plus 2 clicks at the 4 s cooldown: 6 and 5,
    i.e. 120% and 100% of a 5-per-5 s per-message bucket. `BroadcastSettings`' docstring
    states these numbers; discord.py stalls (a late reveal) rather than errors past it."""
    dues = [reveal.due_seconds for reveal in _schedule(_PRESETS[name])]
    most = max(sum(1 for d in dues[i:] if d < start + 5.0) for i, start in enumerate(dues))
    clicks = math.ceil(5.0 / _CLICK_COOLDOWN_SECONDS)
    assert most + clicks == worst, (most, clicks)


def test_the_load_is_a_known_share_of_discords_global_ceiling():
    """watch-v5 WT-2, per speed: four concurrent broadcasts at FAST's 1.5 s tick are
    2.67 edits/second, 5.3% of the 50 requests/second global limit; the reading modes
    edit less often than one per 2.0 s floor, so four of them stay under 4%. The
    literals are hand-updated when a tick or the concurrency changes (round 2's B3)."""
    s = FAST_FORWARD
    edits_per_second = s.max_concurrent_broadcasts / s.tick_seconds
    assert edits_per_second == pytest.approx(4 / 1.5)
    assert edits_per_second / 50 == pytest.approx(0.0533, abs=0.0005)
    for preset in (DEFAULT_BROADCAST_SETTINGS, WATCH_ALONG):
        dues = [reveal.due_seconds for reveal in _schedule(preset)]
        mean_gap = (dues[-1] - dues[0]) / (len(dues) - 1)
        assert preset.max_concurrent_broadcasts / mean_gap / 50 < 0.04


_REJECTED = [
    ("tick_seconds", 0),
    ("tick_seconds", -1.0),
    ("tick_seconds", float("nan")),
    ("tick_seconds", float("inf")),
    ("tick_seconds", True),
    ("tick_seconds", "4"),
    ("edit_timeout_seconds", 0),
    ("edit_timeout_seconds", -0.5),
    ("edit_timeout_seconds", float("inf")),
    ("interrupted_edit_timeout_seconds", 0),
    ("interrupted_edit_timeout_seconds", -3),
    ("final_edit_attempts", 0),
    ("final_edit_attempts", -1),
    ("final_edit_attempts", 2.0),
    ("final_edit_attempts", True),
    ("max_concurrent_broadcasts", 0),
    ("max_concurrent_broadcasts", -2),
    ("max_queue_depth", -1),
    ("max_queue_depth", 1.5),
    ("max_live_broadcasts_per_user", 0),
    ("max_live_broadcasts_per_user", -1),
]


@pytest.mark.parametrize(("field", "value"), _REJECTED)
def test_a_bad_field_is_refused_and_named(field, value):
    with pytest.raises(ValueError, match=field):
        BroadcastSettings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tick_seconds", 0.001),
        ("edit_timeout_seconds", 1),
        ("interrupted_edit_timeout_seconds", 0.1),
        ("final_edit_attempts", 1),
        ("max_concurrent_broadcasts", 1),
        ("max_queue_depth", 0),  # legal: no waiting; slots still admit (see class doc)
        ("max_live_broadcasts_per_user", 1),
    ],
)
def test_the_smallest_legal_value_of_each_field_is_accepted(field, value):
    assert getattr(BroadcastSettings(**{field: value}), field) == value


@pytest.mark.parametrize(
    "name", ["possessions_per_frame", "momentum_window_possessions", "ticker_window_lines"]
)
def test_a_plan_that_got_past_its_own_validation_is_refused_and_named(name):
    plan = PlanSettings()
    object.__setattr__(plan, name, 0)  # what PlanSettings.__post_init__ exists to prevent
    with pytest.raises(ValueError, match=f"plan.{name}"):
        BroadcastSettings(plan=plan)


def test_a_plan_that_is_not_a_plan_is_refused():
    with pytest.raises(ValueError, match="plan"):
        BroadcastSettings(plan=None)  # type: ignore[arg-type]


def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_BROADCAST_SETTINGS.tick_seconds = 0.0  # type: ignore[misc]


# --- mutation twin (k) ------------------------------------------------------

_TICK_CHECK = '_require_finite_positive(owner, "tick_seconds", self.tick_seconds)'


def _a_zero_tick_is_refused(module) -> None:
    with pytest.raises(ValueError, match="tick_seconds"):
        module.BroadcastSettings(tick_seconds=0.0)


def test_twin_control_the_unmutated_copy_refuses_a_zero_tick():
    _a_zero_tick_is_refused(variant())
    assert broadcast.BroadcastSettings is not variant().BroadcastSettings


def test_twin_k_without_the_tick_check_a_zero_tick_is_accepted():
    """§10 (k): a `tick_seconds` of 0 is an infinite busy-loop against Discord;
    the scenario that guards it must go red when the check is deleted."""
    mutated = variant((_TICK_CHECK, "pass"))
    with pytest.raises(pytest.fail.Exception):
        _a_zero_tick_is_refused(mutated)
