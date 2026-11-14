"""/watch v2's presentation half (docs/design/watch-v2-scrollable-broadcast.md
W-5, W-7): the events grammar, the glyph table, the key-play star and the
game's story. The chunk planner this file used to also own (`plan_chunks`,
`PeriodChunk`) is deleted in /watch v4 (docs/design/watch-v4-live-ticker.md
T-6) with no successor -- see the note where its tests used to live, below.
The live component, the hostile-string tests and the goldens are in
`test_broadcast_render.py`; the running tally is `test_live_box.py`.

Two kinds of input. Hand-built possessions with hand-built `_PlayEvent`s pin one
grammar shape at a time, so a failure names the shape. The 54-seed planner
corpus (real engine output, real terminals; `_DEFAULT_SEEDS` (50) +
`_CUSTOM_ID_SEEDS` (4), `test_live_box.py`'s own `_SEEDS`) holds the invariants
that must hold of every possession: the fabricated budget-exhausted close is
never narrated, a restart violation is, a pre-resolution violation keeps
today's wording.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter

import pytest

from dba2.presentation import broadcast
from dba2.presentation.broadcast import (
    _GLYPHS,
    _LEAF_GLYPH_KEYS,
    _LINE_MARKUP_ALLOWANCE,
    _NEUTRAL_GLYPH,
    PlanSettings,
    _Ending,
    _game_story,
    _group_by_possession,
    _key_plays,
    _PlayEvent,
    _utf16_units,
    render_pbp_line,
)
from dba2.sim.events import TerminalKind, Zone
from tests.unit.presentation._broadcast_helpers import names_for
from tests.unit.presentation.test_broadcast import _SETUP, _log, _segment
from tests.unit.presentation.test_live_box import _SEEDS, _event, _game

_WIDE = PlanSettings(max_pbp_line_chars=200)
_NAMES = {11: "Okafor", 12: "Lindqvist", 21: "Tanaka", 22: "Bellweather"}
_MADE = [_SETUP, ("screen", "hedge", 20.0), ("on_ball", "beat_defender", 17.0),
         ("resolution", "made", 13.0)]
_PRE_RESOLUTION = [_SETUP, ("on_ball", "contained", 6.0)]
_OREB_CLOSE = [_SETUP, ("resolution", "missed_unblocked", 15.0), ("rebound", "offense", 14.0)]
_RESTART = [
    _SETUP, ("resolution", "missed_unblocked", 15.0), ("rebound", "offense", 14.0),
    ("setup", "isolation", 14.0), ("on_ball", "contained", 5.0),
]

# --- _PlayEvent -------------------------------------------------------------------


def test_a_play_event_is_the_box_score_fields_plus_exactly_the_two_recovery_ids():
    """Design table row `presentation/broadcast.py`: the live tally's exactness
    depends on this list, so it is pinned -- by name and order, and against the
    engine's own `TerminalOutcome`. It is the eighteen fields
    `boxscore._apply_outcome` reads PLUS `loose_ball_player_id` and
    `recovering_player_id`, which only the loose-ball recovery clause reads
    and nothing in the box score does (docs/design/turnover-recovery.md
    TR-4.16 -- that erosion of the "box-score fields only" rule is deliberate,
    and the two are named here so the list stays a list). The only engine
    field it does not copy is `foul_type`, which nothing here reads."""
    names = [f.name for f in dataclasses.fields(_PlayEvent)]
    assert names == [
        "kind", "zone", "and_one", "assisted", "scorer_id", "passer_id", "blocked",
        "blocking_defender_id", "steal", "stealing_defender_id", "turnover_player_id",
        "ft_count", "ftm", "fouler_id", "fouled_player_id", "offensive_fouler_id",
        "rebounder_id", "rebound_is_offensive", "loose_ball_player_id", "recovering_player_id",
    ]
    from dba2.sim.events import TerminalOutcome

    engine = {f.name for f in dataclasses.fields(TerminalOutcome)}
    assert engine - set(names) == {"foul_type"} and set(names) <= engine
    assert _PlayEvent.__dataclass_params__.frozen


def test_segmentation_reads_the_terminal_and_credits_off_the_last_beat_in_box_score_order():
    """E-1's payload sits on the last beat of a possession; the possession keeps
    it as `[terminal, *credits]` -- the order `boxscore.accumulate_game` applies."""
    checked = 0
    for seed in (0, 6, 33):
        result, possessions = _game(seed)
        groups = _group_by_possession(result.beat_log)
        for possession, beats in zip(possessions, groups, strict=True):
            last = beats[-1]
            expected = (last.terminal, *last.possession_credits)
            assert len(possession.events) == len(expected) >= 1
            for got, want in zip(possession.events, expected, strict=True):
                for field in dataclasses.fields(_PlayEvent):
                    assert getattr(got, field.name) == getattr(want, field.name)
            checked += len(expected)
    assert checked > 500


def test_a_possession_with_credit_events_has_more_events_than_its_terminal():
    counts = Counter(len(p.events) for seed in _SEEDS for p in _game(seed)[1])
    assert set(counts) >= {1, 2, 3} and counts[0] == 0, counts


def test_a_payload_on_a_beat_that_is_not_the_last_is_refused():
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1))
    marked = [
        dataclasses.replace(b, terminal=_engine_outcome()) if i == 1 else b
        for i, b in enumerate(log)
    ]
    with pytest.raises(ValueError, match="not the possession's last"):
        _segment(marked)


def test_credit_events_without_a_terminal_are_refused_and_no_payload_means_no_events():
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1))
    assert _segment(log)[0].events == ()
    lone = [
        dataclasses.replace(b, possession_credits=(_engine_outcome(),)) if i == 3 else b
        for i, b in enumerate(log)
    ]
    with pytest.raises(ValueError, match="credit events without a terminal"):
        _segment(lone)


def _engine_outcome():
    from dba2.sim.events import TerminalOutcome

    return TerminalOutcome(kind=TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=11)


# --- the events grammar, one shape at a time -----------------------------------------


def _line(spec, points, events, *, settings=_WIDE, key_play=False, names=_NAMES) -> str:
    log = _log((spec, points, 720.0, 1), (_MADE, 2, 700.0, 1))
    possession = dataclasses.replace(_segment(log)[0], events=tuple(events))
    return render_pbp_line(
        possession, names=names, home_code="HOM", away_code="AWY", settings=settings,
        key_play=key_play,
    )


def _made(zone, **kw):
    return _event(TerminalKind.MADE_FG, zone=zone, scorer_id=11, **kw)


def _missed(zone, **kw):
    return _event(TerminalKind.MISSED_FG, zone=zone, scorer_id=11, **kw)


@pytest.mark.parametrize(
    ("spec", "points", "events", "expected"),
    [
        pytest.param(
            _MADE, 2, [_made(Zone.RIM, assisted=True, passer_id=12)],
            "11:40  🏀 **HOM 2-0**  Okafor scores at the rim (Lindqvist assist)", id="rim-assisted",
        ),
        pytest.param(
            _MADE, 3, [_made(Zone.THREE, assisted=True, passer_id=12)],
            "11:40  🎯 **HOM 3-0**  Okafor hits a three (Lindqvist assist)", id="three-assisted",
        ),
        pytest.param(
            _MADE, 2, [_made(Zone.MID)],
            "11:40  🏀 **HOM 2-0**  Okafor hits a mid-range shot", id="mid-unassisted",
        ),
        pytest.param(
            _OREB_CLOSE[:2], 0,
            [_missed(Zone.THREE, rebounder_id=22, rebound_is_offensive=False)],
            "11:40  ❌ Okafor misses a three, Bellweather rebounds", id="miss-defensive-board",
        ),
        pytest.param(
            _MADE, 0,
            [_missed(Zone.MID, blocked=True, blocking_defender_id=21, rebounder_id=22,
                     rebound_is_offensive=False)],
            "11:40  🔒 Okafor's mid-range shot blocked by Tanaka, Bellweather rebounds",
            id="blocked",
        ),
        pytest.param(
            _MADE, 1,
            [dataclasses.replace(
                _event(TerminalKind.SHOOTING_FOUL, fouler_id=21, fouled_player_id=11),
                ft_count=2, ftm=1)],
            "11:40  🛑 **HOM 1-0**  Tanaka fouls Okafor, Okafor 1 of 2 at the line",
            id="shooting-foul",
        ),
        pytest.param(
            _MADE, 3,
            [dataclasses.replace(
                _made(Zone.RIM, and_one=True, fouler_id=21, fouled_player_id=11),
                ft_count=1, ftm=1)],
            "11:40  🏀 **HOM 3-0**  Okafor scores at the rim, and-one, Okafor 1 of 1 at the line",
            id="and-one",
        ),
        pytest.param(
            _MADE, 0,
            [_event(TerminalKind.TURNOVER_LIVE, turnover_player_id=11, steal=True,
                    stealing_defender_id=21)],
            "11:40  ⚡ Okafor turnover, stolen by Tanaka", id="steal",
        ),
        pytest.param(
            _MADE, 0, [_event(TerminalKind.TURNOVER_DEAD, turnover_player_id=11)],
            "11:40  🔻 Okafor turnover", id="dead-ball-turnover",
        ),
        pytest.param(
            _MADE, 0, [_event(TerminalKind.OFFENSIVE_FOUL, offensive_fouler_id=11)],
            "11:40  🔻 Offensive foul, Okafor", id="offensive-foul",
        ),
    ],
)
def test_one_event_reads_as_its_shape(spec, points, events, expected):
    assert _line(spec, points, events) == expected


def test_a_possession_that_chains_events_reads_them_in_the_order_they_happened():
    """`possession.events` is in box-score order (terminal first); the line reads
    the other way round -- the credit events first, the terminal last."""
    credit = _missed(Zone.THREE, rebounder_id=12, rebound_is_offensive=True)
    terminal = _event(TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=12)
    line = _line(_MADE, 2, [terminal, credit])
    assert line == (
        "11:40  🏀 **HOM 2-0**  Okafor misses a three, Lindqvist offensive rebound, "
        "Lindqvist scores at the rim"
    )


def test_the_glyph_is_the_most_consequential_events():
    three = _made(Zone.THREE)
    miss = _missed(Zone.THREE, rebounder_id=12, rebound_is_offensive=True)
    foul = _event(TerminalKind.NON_SHOOTING_FOUL, fouler_id=21, fouled_player_id=11)
    glyph = lambda events: _line(_MADE, 2, events)[7]  # noqa: E731 -- the char after "11:40  "
    assert glyph([three, miss]) == _GLYPHS["make3"]
    assert glyph([_made(Zone.RIM), miss]) == _GLYPHS["make2"]
    assert glyph([miss, foul]) == _GLYPHS["foul"]
    assert glyph([miss]) == _GLYPHS["miss"]


def test_a_fabricated_budget_close_is_never_narrated():
    """The engine credits the real event and forces a `TURNOVER_DEAD` terminal that
    "represents no additional real-world fact" (no ball handler). The line shows
    the credit event and no phantom turnover."""
    fabricated = _event(TerminalKind.TURNOVER_DEAD)
    credit = _missed(Zone.THREE, rebounder_id=12, rebound_is_offensive=True)
    line = _line(_OREB_CLOSE, 0, [fabricated, credit])
    assert line == "11:40  ❌ Okafor misses a three, Lindqvist offensive rebound"
    assert "turnover" not in line.lower()
    # the same terminal WITH a ball handler is a real turnover and is narrated
    real = _event(TerminalKind.TURNOVER_DEAD, turnover_player_id=11)
    assert _line(_MADE, 0, [real]).endswith("Okafor turnover")


def test_a_restart_violation_keeps_its_wording_and_narrates_its_real_turnover():
    credit = _missed(Zone.THREE, rebounder_id=12, rebound_is_offensive=True)
    terminal = _event(TerminalKind.TURNOVER_DEAD, turnover_player_id=11)
    possession_log = _log((_RESTART, 0, 720.0, 1), (_MADE, 2, 700.0, 1))
    assert _segment(possession_log)[0].ending is _Ending.RESTART_VIOLATION
    line = _line(_RESTART, 0, [terminal, credit])
    assert line == (
        "11:40  🔻 Okafor misses a three, Lindqvist offensive rebound, "
        "then shot clock violation, Okafor turnover"
    )


def test_a_pre_resolution_violation_keeps_its_wording_whatever_its_terminal_says():
    terminal = _event(TerminalKind.TURNOVER_DEAD, turnover_player_id=11)
    assert _line(_PRE_RESOLUTION, 0, [terminal]) == "11:40  🔻 Shot clock violation, HOM"


def test_a_possession_whose_events_say_nothing_falls_back_to_the_leaf_grammar():
    quiet = _event(TerminalKind.PERIOD_EXPIRED)
    assert _line(_MADE, 2, [quiet]) == "11:40  🏀 **HOM 2-0**  Okafor scores +2"


def test_secondary_players_are_shed_before_the_shooter_or_the_score():
    """At a tight cap the names shrink, then the assist, defender and rebounder
    go; the shooter and the score are still there."""
    credit = _missed(Zone.THREE, rebounder_id=12, rebound_is_offensive=True)
    terminal = _event(
        TerminalKind.MADE_FG, zone=Zone.MID, scorer_id=11, assisted=True, passer_id=12
    )
    names = {11: "Wojciechowski-Nowak", 12: "Papadimitriou-Zorba"}
    line = _line(_MADE, 2, [terminal, credit], settings=PlanSettings(), names=names)
    assert len(line) <= 96 + _LINE_MARKUP_ALLOWANCE
    assert "**HOM 2-0**" in line and "Wo" in line


def test_a_line_ends_in_its_star_only_when_it_is_a_key_play_and_the_star_is_never_cut():
    events = [_made(Zone.MID)]
    assert _line(_MADE, 2, events).endswith("shot")
    assert _line(_MADE, 2, events, key_play=True).endswith("shot  ★")
    tight = PlanSettings(max_pbp_line_chars=20)
    starred = _line(_MADE, 2, events, key_play=True, settings=tight)
    assert starred.endswith("…  ★") and len(starred) == 20 + _LINE_MARKUP_ALLOWANCE


def test_a_cut_never_leaves_half_an_escape_pair_before_the_ellipsis():
    hostile = {11: "\\" * 16}
    line = _line(_MADE, 2, [_made(Zone.MID)], settings=PlanSettings(max_pbp_line_chars=20),
                 names=hostile)
    assert line.endswith("…")
    body = line[: -1]
    assert (len(body) - len(body.rstrip("\\"))) % 2 == 0


# --- the corpus: what must hold of every real possession ---------------------------


def _real_lines():
    """`(seed, possession, line, names)` for every possession of the 54-seed
    corpus, wide enough that nothing shrinks: the assertions are about whole
    sentences. `names` is yielded too (not just used to build `line`) so a
    caller can independently re-derive which `_DETAIL_TIERS` tier a
    possession landed on, per `_forced_terse`'s own docstring."""
    for seed in _SEEDS:
        result, possessions = _game(seed)
        names = names_for(result)
        for possession in possessions:
            line = render_pbp_line(
                possession, names=names, home_code="HOM", away_code="AWY", settings=_WIDE
            )
            yield seed, possession, line, names


def _forced_terse(p, *, names: dict) -> bool:
    """Whether `render_pbp_line` had to fall through every non-terse
    `_DETAIL_TIERS` entry to `_WIDE`'s own budget for this possession.

    This DOES duplicate `render_pbp_line`'s own head/limit computation
    (`broadcast.py`) rather than calling into it directly -- a real second
    copy of that arithmetic, with its own chance to drift, not a thin
    wrapper around the original. One known drift already exists: the real
    `render_pbp_line` computes `limit = settings.max_pbp_line_chars +
    _LINE_MARKUP_ALLOWANCE - len(star)` (a key-play possession reserves
    room for its own trailing star glyph); `limit` below omits the
    `- len(star)` term entirely. Harmless today only because this helper
    always calls `_sentence` with a fixed `offense_code` and never computes
    a `star` of its own, i.e. the term it omits is always 0 on this call
    path -- if a future caller ever needs this helper for a key-play
    possession, this silently under-reserves room and will not match
    `render_pbp_line`'s real terse/non-terse decision for that possession.

    Exists because a possession chaining several offensive rebounds before
    an assisted and-one (multiple named misses, a rebound clause per miss,
    a made shot, an assist clause and a free-throw clause) can be long
    enough on its own, independent of anything that selects who the
    ball-handler or passer is, to overflow every non-terse tier even at
    `_WIDE` -- which is `render_pbp_line`'s own documented degrade-on-
    length behavior (terse drops the assist mention by design, see
    `_event_parts`'s `not terse` guard), not a rendering defect. This
    dispatch's assist-selection fix (`SetupTuning.ball_handler_passing_
    ability_anchor_*`) changed which player wins several BALL_HANDLER/
    PASSER weighted draws across the corpus's seeds -- `weighted_choice`
    itself still consumes exactly one `rng.random()` per draw regardless of
    which player wins, so the RNG stream's own draw count and positions are
    unchanged; it is the *outcome* of those draws, and everything that
    branches on it downstream in the same possession/game, that diverges.
    That divergence is what first produced such a possession within this
    fixed 54-seed corpus; the strict "assisted implies 'assist)' is in the
    line" invariant below was never actually guaranteed by the design for a
    possession this long, just never falsified before."""
    offense_code = "AWY"  # arbitrary and-- like render_pbp_line's own -- irrelevant to length
    home_after, away_after = p.score_after
    score = f"**{offense_code} {home_after}-{away_after}**  " if p.points > 0 else ""
    head = f"{broadcast._format_clock(p.clock_end_seconds)}  {broadcast._line_glyph(p)} {score}"
    # No `- len(star)` term -- see this function's own docstring for why
    # that's currently harmless but fragile.
    limit = _WIDE.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE
    for cap, terse in broadcast._DETAIL_TIERS:
        sentence = broadcast._sentence(
            p, names=names, cap=cap, terse=terse, offense_code=offense_code
        )
        if len(head) + len(sentence) <= limit:
            return terse
    return True  # pragma: no cover -- even the cut-to-fit fallback is effectively terse-length


def test_every_real_possession_reads_by_the_rules_of_its_events_and_its_ending():
    seen = Counter()
    for seed, p, line, names in _real_lines():
        where = f"seed {seed} possession {p.index}: {line!r}"
        terminal = p.events[0]
        lowered = line.lower()
        head = re.match(r"^\d+:\d\d  (\S) ", line)
        assert head is not None and head.group(1) in _GLYPHS.values(), where
        assert _NEUTRAL_GLYPH not in line, where
        assert "+" not in line, where  # the events grammar never prints a `+N`
        if p.points > 0:
            code = "HOM" if p.offense_is_home else "AWY"
            ours, theirs = (
                p.score_after if p.offense_is_home else p.score_after[::-1]
            )
            assert f"**{code} {ours}-{theirs}**" in line, where
        else:
            assert "**" not in line, where
        fabricated = (
            terminal.kind is TerminalKind.TURNOVER_DEAD and terminal.turnover_player_id is None
        )
        if fabricated:
            seen["fabricated"] += 1
            assert "turnover" not in lowered and "violation" not in lowered, where
            assert len(p.events) > 1, where  # the real event is a credit event
        if p.ending is _Ending.RESTART_VIOLATION:
            seen["restart"] += 1
            assert "then shot clock violation, " in line and lowered.endswith("turnover"), where
            assert not fabricated, where
        elif p.ending is _Ending.PRE_RESOLUTION_VIOLATION:
            seen["pre-resolution"] += 1
            assert line.endswith("Shot clock violation, " + ("HOM" if p.offense_is_home else "AWY"))
        else:
            assert "violation" not in lowered, where
        if any(e.kind is TerminalKind.MADE_FG and e.assisted and e.passer_id for e in p.events):
            seen["assist"] += 1
            # `render_pbp_line` drops the assist mention only at its own final
            # (terse) `_DETAIL_TIERS` fallback, reached when even the loosest
            # non-terse tier overflows `_WIDE`'s budget -- see `_forced_terse`'s
            # own docstring for why a long enough rebound-scramble possession
            # can legitimately do that regardless of anything assist-related.
            assert "assist)" in line or _forced_terse(p, names=names), where
        seen["lines"] += 1
    # non-vacuity: each class the design counts really occurs in the corpus.
    # Measured at TUNING_VERSION 22 (weighted loose-ball recoverer, 54 seeds):
    # fabricated 57, restart 96, pre-resolution 19 (21 at 21 over 55 seeds,
    # which included the since-dropped overtime seed 146), so the
    # pre-resolution floor is 15 rather than 20.
    assert seen["fabricated"] >= 30 and seen["restart"] >= 40 and seen["pre-resolution"] >= 15
    assert seen["assist"] > 500 and seen["lines"] > 6000, seen


def test_real_lines_stay_inside_the_cap_plus_markup_at_the_default_settings():
    settings = PlanSettings()
    for seed in _SEEDS[:8]:
        result, possessions = _game(seed)
        names = names_for(result)
        for p in possessions:
            line = render_pbp_line(
                p, names=names, home_code="HOM", away_code="AWY", settings=settings
            )
            assert len(line) <= settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE, line


# --- the glyph table ------------------------------------------------------------------


def test_the_glyph_table_is_the_designs_eight_single_code_points():
    assert set(_GLYPHS) == {
        "make3", "make2", "miss", "block", "foul", "steal", "dead", "loose"
    }
    assert all(len(glyph) == 1 for glyph in (*_GLYPHS.values(), _NEUTRAL_GLYPH))
    assert len(set(_GLYPHS.values())) == 8 and _NEUTRAL_GLYPH not in _GLYPHS.values()
    assert _GLYPHS["make2"] == "🏀" and _GLYPHS["make3"] == "🎯" and _GLYPHS["miss"] == "❌"
    assert _GLYPHS["block"] == "🔒" and _GLYPHS["foul"] == "🛑"
    assert _GLYPHS["steal"] == "⚡" and _GLYPHS["dead"] == "🔻"
    # docs/design/turnover-recovery.md TR-4.15(d): never the steal glyph (a
    # recovery is the opposite of one) and never "dead" (nothing died).
    assert _GLYPHS["loose"] == "🔄" and _LEAF_GLYPH_KEYS["loose_ball_recovered"] == "loose"
    assert set(_LEAF_GLYPH_KEYS.values()) <= set(_GLYPHS)
    assert set(_LEAF_GLYPH_KEYS) == set(broadcast._LEAF_PHRASES)  # every leaf has a glyph


def test_glyph_widths_are_counted_in_real_utf16_code_units_not_assumed():
    """Astral emoji are two units, BMP ones one (the cross mark and the bolt), so a
    chunk budget that counted code points would let a message overflow."""
    units = {key: _utf16_units(glyph) for key, glyph in _GLYPHS.items()}
    assert units == {"make3": 2, "make2": 2, "miss": 1, "block": 2, "foul": 2, "steal": 1,
                     "dead": 2, "loose": 2}
    assert _utf16_units("abc") == 3 and _utf16_units("Dončić") == 6
    assert _utf16_units("\ud800") == 1  # a lone surrogate is measured, not raised on


def test_a_leaf_missing_from_the_glyph_table_renders_a_neutral_bullet():
    log = _log(([_SETUP, ("resolution", "goaltend", 15.0)], 0, 720.0, 1), (_MADE, 2, 700.0, 1))
    line = render_pbp_line(
        _segment(log)[0], names=_NAMES, home_code="HOM", away_code="AWY", settings=_WIDE
    )
    assert line == "11:40  • Okafor — goaltend"


# --- key plays --------------------------------------------------------------------------


def _possessions_from(rows):
    """Hand-built possessions from `(offense_is_home, points, period, clock_end,
    three_or_and_one)` rows, scored cumulatively from 0-0."""
    base = _segment(_log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1)))[0]
    home = away = 0
    built = []
    for index, (offense_home, points, period, clock_end, three) in enumerate(rows):
        before = (home, away)
        if offense_home:
            home += points
        else:
            away += points
        events = (
            (_event(TerminalKind.MADE_FG, zone=Zone.THREE if three else Zone.RIM, scorer_id=11),)
            if points
            else ()
        )
        built.append(
            dataclasses.replace(
                base, index=index, offense_is_home=offense_home, points=points, period=period,
                score_before=before, score_after=(home, away), clock_end_seconds=clock_end,
                events=events,
            )
        )
    return built


def test_a_lead_change_and_a_tying_score_are_key_plays_taking_a_lead_from_a_tie_is_not():
    # home 2-0, away 2 (tie: key), away 3 (away leads 2-5? no: 2-5), home 2 (2... )
    rows = [
        (True, 2, 1, 600.0, False),   # 0: 2-0, home takes the lead from a tie: not a key play
        (False, 2, 1, 590.0, False),  # 1: 2-2, ties it: key
        (False, 3, 1, 580.0, False),  # 2: 2-5, away goes ahead of a TIE: not a lead change
        (True, 2, 1, 570.0, False),   # 3: 4-5, closes the gap: not a key play
        (True, 2, 1, 560.0, False),   # 4: 6-5, flips the lead: key
        (False, 0, 1, 550.0, False),  # 5: no score
    ]
    assert _key_plays(_possessions_from(rows)) == {1, 4}


def test_a_three_that_completes_an_eight_nothing_run_is_a_key_play_a_two_is_not():
    threes = [(True, 3, 1, 600.0 - i, i == 2) for i in range(3)]  # 3, 6, 9: crosses 8 on a three
    twos = [(True, 3, 1, 600.0 - i, False) for i in range(3)]  # same run, but no three
    assert 2 in _key_plays(_possessions_from(threes))
    assert 2 not in _key_plays(_possessions_from(twos))
    # an answering basket resets the run: 3, 3, then the other side scores, then 3 more
    reset = [(True, 3, 1, 600.0, True), (True, 3, 1, 590.0, True), (False, 2, 1, 580.0, False),
             (True, 3, 1, 570.0, True)]
    assert 3 not in _key_plays(_possessions_from(reset))


def test_a_score_in_the_last_minute_of_a_close_late_game_is_a_key_play():
    close = [(True, 2, 4, 90.0, False), (True, 2, 4, 50.0, False), (False, 2, 4, 30.0, False)]
    assert 2 in _key_plays(_possessions_from(close))
    early = [(True, 2, 3, 30.0, False), (False, 2, 3, 10.0, False)]  # third period: no
    assert _key_plays(_possessions_from(early)) == {1}  # only the tying score
    blowout = [(True, 3, 4, 90.0, False)] * 3 + [(True, 2, 4, 30.0, False)]
    assert 3 not in _key_plays(_possessions_from(blowout))  # 11 points up: not one possession


def test_the_key_plays_of_a_real_game_are_a_small_fraction_of_its_possessions():
    result, possessions = _game(6)
    starred = _key_plays(possessions)
    assert 0 < len(starred) < len(possessions) * 0.35


# --- the game's story ---------------------------------------------------------------------


def test_the_story_counts_lead_changes_leads_the_longest_run_and_the_late_score():
    rows = [
        (True, 2, 4, 400.0, False),   # 2-0
        (False, 3, 4, 350.0, False),  # 2-3  lead change 1
        (False, 2, 4, 320.0, False),  # 2-5  away run: 5 unanswered
        (True, 2, 4, 290.0, False),   # 4-5  (the 5:00 mark passed before this one)
        (True, 3, 4, 200.0, False),   # 7-5  lead change 2; home run 5
        (False, 0, 4, 100.0, False),
    ]
    story = _game_story(_possessions_from(rows))
    assert story.lead_changes == 2
    assert (story.largest_home_lead, story.largest_away_lead) == (2, 3)
    assert story.longest_run == (False, 5)  # away's 5 unanswered; home's 5 only ties it
    assert story.late_period == 4 and story.late_score == (2, 5)  # after the last end >= 5:00


def test_the_story_in_an_overtime_period_reads_the_score_at_its_tip():
    rows = [(True, 2, 4, 30.0, False), (False, 2, 5, 280.0, False), (True, 2, 5, 250.0, False)]
    story = _game_story(_possessions_from(rows))
    assert story.late_period == 5 and story.late_score == (2, 0)


# --- the chunk packer (v2's plan_chunks/PeriodChunk) is GONE in watch-v4 -----------
#
# v3's own budget/packing suite lived here (the default 3,800-unit budget holding
# every corpus period, a small budget splitting a period into several chunks,
# monotonicity and prefix-monotonicity of the split, and the UTF-16-vs-code-point
# astral-name case). None of it has a v4 successor: there is no packer, no chunk,
# and no per-period message any more, so there is nothing left to bound a budget
# over. The live component's own size bound is `PlanSettings.__post_init__`'s new
# ticker-fit inequality (T-1.a, `test_broadcast_plan.py`) plus the corpus-measured
# worst-case panel size (`test_broadcast_spacing.py`).
