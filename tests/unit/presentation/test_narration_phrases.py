"""The narration phrase library and its selector (docs/design/watch-narration-v2.md
WN-3, WN-7; WN-12 T6): what a template may reference, what its flavor may assert, and
that the choice among variants is replay-stable."""

from __future__ import annotations

import re
import string

import pytest

from dba2.presentation import broadcast
from dba2.presentation.broadcast import _PhrasePicker, _variant
from dba2.presentation.narration_phrases import (
    KEY_ORDINALS,
    LIBRARY,
    OUTCOME_GROUPS,
    PLACEHOLDERS,
    SETUP_HOLDER,
)
from tests.unit.presentation.test_broadcast_ground_truth import _terminal_claims

_SAMPLE = {name: f"N{n}x" for n, name in enumerate(sorted(set().union(*PLACEHOLDERS.values())))}


def _fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def _pattern(template: str) -> re.Pattern[str]:
    """A template as a regex: its literal text, any name where a placeholder is."""
    parts = []
    for literal, field, _, _ in string.Formatter().parse(template):
        parts.append(re.escape(literal))
        if field:
            parts.append(r".+")
    return re.compile("".join(parts))


def test_the_library_is_sized_as_the_design_says():
    """WN-7.1: ~115 phrases, 3-5 variants a key (2 for the rare restart prefixes). WN-17
    widened the three on-ball reads to 8 each: the same matchup is read dozens of times
    a game, and at 5 they made most of the feed's exact repeats."""
    total = sum(len(v) for v in LIBRARY.values())
    assert 100 <= total <= 150, total
    assert all(2 <= len(v) <= 5 for key, v in LIBRARY.items() if key[0] != "O")
    assert all(
        len(LIBRARY[("O", outcome)]) == 8 for outcome in ("advantage", "contained", "defender_wins")
    )
    assert set(PLACEHOLDERS) == set(LIBRARY) == set(KEY_ORDINALS)


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_every_variant_of_a_key_uses_exactly_its_placeholders(key):
    """T6: a variant cannot silently drop the defender (or add a player the beat did
    not name)."""
    for phrase in LIBRARY[key]:
        assert _fields(phrase.template) == PLACEHOLDERS[key], phrase.template


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_every_flavor_string_is_really_in_its_template_and_every_variant_is_tagged(key):
    for phrase in LIBRARY[key]:
        assert phrase.tags.startswith(("[F]", "[M]")), phrase
        for word in phrase.flavor:
            assert word in phrase.template, (word, phrase.template)


@pytest.mark.parametrize("group", OUTCOME_GROUPS, ids=lambda g: g[0][0] + ":" + g[0][1])
def test_flavor_is_disjoint_across_the_outcomes_of_one_beat(group):
    """WN-3.1's checkable form: a `[V]` string attached to one outcome is attached to
    no other outcome of the same beat."""
    seen: dict[str, tuple[str, str]] = {}
    for key in group:
        for phrase in LIBRARY[key]:
            for word in phrase.flavor:
                owner = seen.setdefault(word, key)
                assert owner == key, f"{word!r} is flavor of both {owner} and {key}"


@pytest.mark.parametrize("group", OUTCOME_GROUPS, ids=lambda g: g[0][0] + ":" + g[0][1])
def test_no_variant_reads_like_a_variant_of_another_outcome(group):
    """The stronger form the corpus test relies on (WN-12 T1(b)): a rendered variant
    of one outcome matches no template of any other outcome of the same beat, so a
    line's outcome can be read back off its text."""
    for key in group:
        for phrase in LIBRARY[key]:
            text = phrase.template.format(**_SAMPLE)
            for other in group:
                if other == key:
                    continue
                for candidate in LIBRARY[other]:
                    assert not _pattern(candidate.template).search(text), (
                        text,
                        other,
                        candidate.template,
                    )


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_no_phrase_makes_a_claim_the_terminal_grammar_owns(key):
    """T6/T1(d): no template -- flavor or fact -- carries a word the terminal line's
    claim checker reads (the one shared tuple, TR-4.15(c))."""
    for phrase in LIBRARY[key]:
        assert _terminal_claims(phrase.template.format(**_SAMPLE)) == [], phrase.template


_FORBIDDEN_INVENTIONS = (
    # named moves (WN-3.2)
    "crossover", "spin", "hesitation", "step-back", "stepback", "behind the back", "euro",
    # speed and effort adjectives
    "quick", "fast", "slow", "hard", "explosive", "lazy", "burst", "blows by",
    # court locations other than the role names of WN-3.3
    "wing", "top of the key", "elbow", "baseline", "paint", "block", "lane", "arc",
    "in the corner", "on the weak side",
)  # fmt: skip


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_no_phrase_names_a_move_an_effort_or_an_unrecorded_place(key):
    """WN-3.2/WN-3.3: the library itself never says "corner" or "weak side" -- those
    come only from the role-derived location suffix the renderer adds -- and "in the
    post" appears only where the post scorer is named."""
    for phrase in LIBRARY[key]:
        text = phrase.template.lower()
        for word in _FORBIDDEN_INVENTIONS:
            assert not re.search(rf"\b{re.escape(word)}\b", text), (word, phrase.template)
        if "in the post" in text:
            assert key == ("S", "post_up"), phrase.template


@pytest.mark.parametrize("action", sorted(SETUP_HOLDER))
def test_every_setup_variant_opens_on_the_ball_holder(action):
    """WN-4.3: "{X} now has it, <rest>" drops the holder placeholder, so every S
    variant must open on it."""
    holder = SETUP_HOLDER[action]
    for phrase in LIBRARY[("S", action)]:
        assert phrase.template.startswith("{" + holder + "} "), phrase.template
        assert holder in PLACEHOLDERS[("S", action)]


# --- the selector (WN-7.2) ---------------------------------------------------------


def test_the_variant_mixer_is_pure_integer_arithmetic_and_pinned():
    """Same inputs, same output, in any process: the values are pinned so a change to
    the mixer (which would re-word every replay) is a deliberate test edit."""
    assert [_variant((412, p, 0, 3), 5) for p in range(8)] == [
        _variant((412, p, 0, 3), 5) for p in range(8)
    ]
    assert _variant((0,), 1) == 0
    assert [_variant((412, p, 1, 7), 5) for p in range(10)] == _PINNED
    assert _variant((-5, 2), 7) == _variant(((-5) & ((1 << 64) - 1), 2), 7)


_PINNED = [3, 0, 3, 3, 3, 4, 3, 3, 3, 1]  # re-pin only on a deliberate mixer change


def test_the_mixer_refuses_an_empty_choice():
    with pytest.raises(ValueError):
        _variant((1,), 0)


def test_a_picker_never_repeats_a_key_back_to_back_and_is_replay_stable():
    key = ("O", "advantage")
    first = [_PhrasePicker(412).pick(key, possession=p, slot=1) for p in range(1)]
    picker, again = _PhrasePicker(412), _PhrasePicker(412)
    run = [picker.pick(key, possession=p, slot=1) for p in range(200)]
    rerun = [again.pick(key, possession=p, slot=1) for p in range(200)]
    assert run == rerun and run[0] == first[0]
    assert all(a != b for a, b in zip(run, run[1:], strict=False))
    assert len(set(run)) == len(LIBRARY[key])  # every variant gets used


def test_a_picker_says_every_variant_about_the_same_players_before_repeating_one():
    """WN-17: the same matchup is read dozens of times a game, so a key picked for the
    same players steps past every variant already said about them until all have been.
    Without players the draw is the old one (only the back-to-back rule applies)."""
    key = ("O", "contained")
    count = len(LIBRARY[key])
    picker = _PhrasePicker(412)
    said = [picker.pick(key, possession=p, slot=1, actors=(11, 21)) for p in range(count)]
    assert len(set(said)) == count
    plain, old = _PhrasePicker(412), []
    last = None
    for p in range(50):
        index = _variant((412, p, 1, KEY_ORDINALS[key]), count)
        if index == last:
            index = (index + 1) % count
        last = index
        old.append(LIBRARY[key][index])
    assert [plain.pick(key, possession=p, slot=1) for p in range(50)] == old


def test_key_ordinals_are_distinct_and_independent_of_library_order():
    """Adding a key must not reword any other key's lines: each key's selector input is
    a function of its own name alone, and no two keys share one."""
    import zlib

    assert len(set(KEY_ORDINALS.values())) == len(KEY_ORDINALS)
    for (slot, name), ordinal in KEY_ORDINALS.items():
        assert ordinal == zlib.crc32(f"{slot}:{name}".encode())


def test_a_different_game_id_words_some_line_differently():
    key = ("S", "spot_up")
    a = [_PhrasePicker(412).pick(key, possession=p, slot=0) for p in range(20)]
    b = [_PhrasePicker(413).pick(key, possession=p, slot=0) for p in range(20)]
    assert a != b


def test_the_selector_reads_no_randomness_hash_or_clock():
    """A8: the selector's code (docstrings aside) names none of the process-varying
    inputs -- no `random`, no `hash`, no `time`/clock."""
    import ast
    import inspect
    import textwrap

    names: set[str] = set()
    for obj in (broadcast._variant, broadcast._PhrasePicker):
        tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    assert names.isdisjoint({"random", "hash", "time", "monotonic", "clock", "uuid4"}), names
