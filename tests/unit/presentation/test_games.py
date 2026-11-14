"""`presentation/games.py` -- Stage D commit 4 (docs/design/phase-5-stage-d-
broadcast.md §5, P5-97.m/.n/.v, §9's I10, §10 Tier 1): the `#games` block
header and one line per game.

The property these tests exist for is a negative one: a score must be unable
to reach a slate, matchup or watching line, because `#games` is public and a
game is only supposed to be spoiled to the people who chose to watch it. So
the checks are written to have teeth -- each is a module-level checker a
deliberately leaky renderer is handed and must fail (P4-60/P5-74: a rule that
silently matches nothing is worse than no rule).

I10 also runs over the seeded broadcast corpus (`test_broadcast_plan.py`
`_check_i10`), beside I1-I9."""
from __future__ import annotations

import dataclasses
import inspect
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

import pytest

from dba2.presentation.games import (
    SlateGame,
    build_matchup_line,
    build_result_line,
    build_slate_header,
    build_watching_line,
)
from tests.unit.presentation.test_broadcast_render import _DBA2_DIR, _call_sites

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

_GAME = SlateGame(game_id=412, home_code="LAL", away_code="DAL", game_date=date(2026, 10, 22))


# --- the lines, literally ----------------------------------------------------


def test_the_matchup_line_names_the_game_and_says_it_is_not_yet_played():
    assert build_matchup_line(_GAME) == "Game 412 · DAL @ LAL — not yet played"


def test_the_watching_line_replaces_only_the_status():
    assert build_watching_line(_GAME) == "Game 412 · DAL @ LAL — watching now"


def test_the_result_line_puts_each_score_beside_its_own_team():
    assert build_result_line(_GAME, home_score=118, away_score=112) == (
        "Game 412 · DAL 112 @ LAL 118 — final"
    )


def test_the_header_leads_with_the_block_and_counts_games():
    assert build_slate_header("2026-10-22", 13) == (
        "**2026-10-22** — 13 games played. Use `/watch <game id>` to follow one in its own "
        "thread; results are posted here once the block closes."
    )
    assert "1 game played" in build_slate_header("2026-10-22", 1)


# --- the field set: no score-bearing field (I10, design P5-97.m item 1) -------

_SLATE_FIELDS = {"game_id", "home_code", "away_code", "game_date"}
_SCORE_WORDS = ("score", "points", "pts", "final", "result", "winner")


def _check_field_set(cls: type) -> None:
    """`SlateGame` carries exactly the design's four fields, and none of them
    is score-shaped -- the second clause catches a rename that the exact set
    would also catch, and names WHY the set is pinned."""
    names = {f.name for f in fields(cls)}
    assert names == _SLATE_FIELDS, (
        f"{cls.__name__} fields {sorted(names)} != {sorted(_SLATE_FIELDS)}"
    )
    assert not [n for n in names if any(word in n for word in _SCORE_WORDS)]


def test_slate_game_has_no_score_bearing_field():
    _check_field_set(SlateGame)


def test_the_field_set_check_has_teeth():
    """Mutation twin: a `SlateGame` that grew a score field, or renamed one."""

    @dataclass(frozen=True)
    class WithScore:
        game_id: int
        home_code: str
        away_code: str
        game_date: date
        home_score: int

    @dataclass(frozen=True)
    class Renamed:
        game_id: int
        home_code: str
        away_code: str
        game_date: date
        margin: int

    for leaky in (WithScore, Renamed):
        with pytest.raises(AssertionError):
            _check_field_set(leaky)


def test_no_slate_renderer_can_be_handed_a_score():
    """Structural: only `build_result_line` has a score parameter."""
    def parameters(fn):
        return [p for p in inspect.signature(fn).parameters]

    assert parameters(build_matchup_line) == ["game"]
    assert parameters(build_watching_line) == ["game"]
    assert parameters(build_slate_header) == ["block_id", "game_count"]
    assert parameters(build_result_line) == ["game", "home_score", "away_score"]


# --- I10: rendered against a score whose digits appear nowhere else ----------


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def _slate_lines(
    game: SlateGame, block_id: str, count: int, *, matchup=build_matchup_line,
    watching=build_watching_line, header=build_slate_header,
) -> dict[str, tuple[str, list[str]]]:
    """(line, the only digit runs it may contain) per non-result renderer."""
    return {
        "matchup": (matchup(game), [str(game.game_id)]),
        "watching": (watching(game), [str(game.game_id)]),
        "header": (header(block_id, count), [*_numbers(block_id), str(count)]),
    }


def _check_spoiler_free(
    lines: dict[str, tuple[str, list[str]]], *, home_score: int, away_score: int
) -> None:
    """Each slate line contains exactly the digit runs its own inputs put
    there (a game id; a block id and a count) and no digit that came from the
    score -- checked both ways so a leak is caught whether it adds a run or
    reuses one."""
    for name, (line, allowed) in lines.items():
        assert _numbers(line) == allowed, f"{name} line {line!r} carries a stray number"
        for score in (home_score, away_score):
            assert str(score) not in _numbers(line), f"{name} line {line!r} leaks {score}"


def _generated_cases(count: int):
    """Seeded inputs whose digits are drawn from 0-4 only, and scores from 5-9
    only, so a score digit anywhere in a slate line is unmistakable."""
    rng = random.Random(2026)
    low, high = "01234", "56789"
    days = (1, 2, 3, 4, 10, 11, 12, 13, 14, 20, 21, 22, 23, 24)
    for _ in range(count):
        game_id = int("".join(rng.choice(low) for _ in range(rng.randint(1, 4))).lstrip("0") or "1")
        block = date(2012, rng.choice((1, 2, 3, 4, 10, 11, 12)), rng.choice(days))
        home_score = int("".join(rng.choice(high) for _ in range(rng.randint(2, 3))))
        away_score = int("".join(rng.choice(high) for _ in range(rng.randint(2, 3))))
        codes = ["".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3)) for _ in "ab"]
        game = SlateGame(game_id=game_id, home_code=codes[0], away_code=codes[1], game_date=block)
        yield game, block.isoformat(), rng.choice(days[:9]), home_score, away_score


def test_slate_matchup_and_watching_lines_never_contain_a_score_digit():
    cases = list(_generated_cases(200))
    for game, block_id, count, home_score, away_score in cases:
        lines = _slate_lines(game, block_id, count)
        _check_spoiler_free(lines, home_score=home_score, away_score=away_score)
        for name, (line, _) in lines.items():
            assert not set(line) & set("56789"), f"{name} line {line!r} has a score digit"
        # the positive control: the same fixture through the ONE renderer that
        # takes a score does carry it, so the checker is looking for something
        # real.
        result = build_result_line(game, home_score=home_score, away_score=away_score)
        assert str(home_score) in _numbers(result) and str(away_score) in _numbers(result)
    assert len({(c[0].game_id, c[3], c[4]) for c in cases}) > 100  # the corpus varied


def _leaky(render: Callable[[SlateGame], str]) -> Callable[[SlateGame], str]:
    """A renderer that appends a score it should not have."""
    return lambda game: render(game) + " (118-102)"


def test_the_spoiler_check_has_teeth_a_leaky_matchup_or_watching_line_or_header():
    """Mutation twin: hand the SAME checker renderers that leak a score, by
    appending a run of it, by replacing the game id with it, and through the
    header. Each must fail."""
    game, block_id, count, home_score, away_score = next(_generated_cases(1))
    clean = _slate_lines(game, block_id, count)
    _check_spoiler_free(clean, home_score=home_score, away_score=away_score)

    leaks = {
        "appended run (matchup)": _slate_lines(
            game, block_id, count, matchup=_leaky(build_matchup_line)
        ),
        "appended run (watching)": _slate_lines(
            game, block_id, count, watching=_leaky(build_watching_line)
        ),
        "header": _slate_lines(
            game, block_id, count,
            header=lambda b, c: build_slate_header(b, c) + f" Final: {home_score}",
        ),
        "score in place of the game id": _slate_lines(
            game, block_id, count,
            matchup=lambda g: build_matchup_line(dataclasses.replace(g, game_id=home_score)),
        ),
    }
    for label, lines in leaks.items():
        with pytest.raises(AssertionError):
            _check_spoiler_free(lines, home_score=home_score, away_score=away_score)
        # ... and the leak really is in the text, not a checker artefact
        assert any(
            str(home_score) in _numbers(line) or _numbers(line) != allowed
            for line, allowed in lines.values()
        ), label


# --- build_result_line's single publisher (design P5-97.m item 2) ------------

_GAME_FEED = "bot/game_feed.py"


def _result_line_call_violations(root: Path) -> list[str]:
    """`build_result_line` is called from `bot/game_feed.reveal_game` and
    nowhere else, and at most once. Zero is allowed today (the publisher lands
    in Stage D commit 9); a second caller, or one in any other file, is not."""
    sites = _call_sites("build_result_line", root)
    violations = [
        f"{path}:{scope} is not in {_GAME_FEED}" for path, scope in sites if path != _GAME_FEED
    ]
    if len(sites) > 1:
        violations.append(f"{len(sites)} call sites, at most 1 allowed: {sites}")
    for path, scope in sites:
        if path == _GAME_FEED and scope != "reveal_game":
            violations.append(f"the call is in {scope}, not reveal_game")
    return violations


def test_build_result_line_has_at_most_one_call_site_and_it_is_reveal_game():
    assert _result_line_call_violations(_DBA2_DIR) == []


def test_the_result_line_call_site_pin_has_teeth(tmp_path):
    """Mutation twin (e): a second caller, and a caller in the wrong place,
    planted in a scratch tree, are each flagged by the same function."""
    (tmp_path / "bot").mkdir()
    (tmp_path / "presentation").mkdir()
    feed = tmp_path / "bot" / "game_feed.py"
    call = "    return build_result_line(game, home_score=1, away_score=2)\n"
    feed.write_text("def reveal_game(game):\n" + call, encoding="utf-8")
    assert _result_line_call_violations(tmp_path) == [], "the one legitimate caller must pass"

    second = call.replace("return", "x =")
    feed.write_text("def reveal_game(game):\n" + call + second, encoding="utf-8")
    assert _result_line_call_violations(tmp_path), "a second caller in reveal_game must fail"

    feed.write_text("def close_block(game):\n" + call, encoding="utf-8")
    assert _result_line_call_violations(tmp_path), "the wrong function must fail"

    feed.write_text("def reveal_game(game):\n" + call, encoding="utf-8")
    (tmp_path / "presentation" / "other.py").write_text(
        "def leak(game):\n" + call, encoding="utf-8"
    )
    assert _result_line_call_violations(tmp_path), "a caller in another module must fail"
