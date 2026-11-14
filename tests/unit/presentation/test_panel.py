"""§9.2's structural invariants on `Panel`, applied at the type's own
construction boundary (Decision P5-4): field count, per-field length and
total length, checked against Discord's real documented limits (see
`dba2/presentation/panel.py`'s module docstring for the citation) rather
than spot-checked. Includes the adversarial-corpus shapes §9.2 names by
example: a 15-man roster of maximum-length names, and a maximum-length
description/footer/author combination."""
from __future__ import annotations

import dataclasses

import pytest

from dba2.presentation.panel import (
    MAX_AUTHOR_NAME_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_FIELD_NAME_CHARS,
    MAX_FIELD_VALUE_CHARS,
    MAX_FIELDS,
    MAX_FOOTER_TEXT_CHARS,
    MAX_TITLE_CHARS,
    MAX_TOTAL_CHARS,
    Panel,
    PanelColor,
    PanelField,
)


def test_minimal_panel_constructs():
    panel = Panel(title="Standings")
    assert panel.title == "Standings"
    assert panel.description == ""
    assert panel.fields == ()
    assert panel.color is PanelColor.NEUTRAL


def test_panel_field_accepts_max_length_name_and_value():
    field = PanelField(name="x" * MAX_FIELD_NAME_CHARS, value="y" * MAX_FIELD_VALUE_CHARS)
    assert len(field.name) == MAX_FIELD_NAME_CHARS
    assert len(field.value) == MAX_FIELD_VALUE_CHARS


def test_panel_field_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        PanelField(name="", value="something")


def test_panel_field_rejects_name_over_limit():
    with pytest.raises(ValueError, match="name"):
        PanelField(name="x" * (MAX_FIELD_NAME_CHARS + 1), value="y")


def test_panel_field_rejects_empty_value():
    with pytest.raises(ValueError, match="value"):
        PanelField(name="name", value="")


def test_panel_field_rejects_value_over_limit():
    with pytest.raises(ValueError, match="value"):
        PanelField(name="name", value="y" * (MAX_FIELD_VALUE_CHARS + 1))


def test_panel_rejects_empty_title():
    with pytest.raises(ValueError, match="title"):
        Panel(title="")


def test_panel_rejects_title_over_limit():
    with pytest.raises(ValueError, match="title"):
        Panel(title="x" * (MAX_TITLE_CHARS + 1))


def test_panel_accepts_max_length_title():
    panel = Panel(title="x" * MAX_TITLE_CHARS)
    assert len(panel.title) == MAX_TITLE_CHARS


def test_panel_rejects_description_over_limit():
    with pytest.raises(ValueError, match="description"):
        Panel(title="t", description="d" * (MAX_DESCRIPTION_CHARS + 1))


def test_panel_rejects_more_than_25_fields():
    fields = tuple(PanelField(name=f"f{i}", value="v") for i in range(MAX_FIELDS + 1))
    with pytest.raises(ValueError, match="fields"):
        Panel(title="t", fields=fields)


def test_panel_accepts_exactly_25_fields():
    fields = tuple(PanelField(name=f"f{i}", value="v") for i in range(MAX_FIELDS))
    panel = Panel(title="t", fields=fields)
    assert len(panel.fields) == MAX_FIELDS


def test_panel_rejects_footer_over_limit():
    with pytest.raises(ValueError, match="footer"):
        Panel(title="t", footer="f" * (MAX_FOOTER_TEXT_CHARS + 1))


def test_panel_rejects_author_over_limit():
    with pytest.raises(ValueError, match="author"):
        Panel(title="t", author="a" * (MAX_AUTHOR_NAME_CHARS + 1))


def test_panel_rejects_combined_total_over_6000_even_when_every_individual_field_is_legal():
    """The adversarial case §9.2 names: every individual component is
    within its own limit, but the combined total across title +
    description + every field.name/value + footer + author exceeds
    Discord's real 6000-character message-wide ceiling."""
    fields = tuple(
        PanelField(name="n" * MAX_FIELD_NAME_CHARS, value="v" * MAX_FIELD_VALUE_CHARS)
        for _ in range(MAX_FIELDS)
    )
    with pytest.raises(ValueError, match="6000|combined"):
        Panel(
            title="t" * MAX_TITLE_CHARS,
            description="d" * MAX_DESCRIPTION_CHARS,
            fields=fields,
            footer="f" * MAX_FOOTER_TEXT_CHARS,
            author="a" * MAX_AUTHOR_NAME_CHARS,
        )


def test_fifteen_man_roster_of_max_length_names_fits_or_fails_loudly():
    """§9.2's own named adversarial fixture: "a 15-man roster of
    maximum-length names." A 15-field panel (one per roster spot) each at
    the per-field max is well under 25 fields but may or may not clear
    the combined 6000-char ceiling -- this test proves the panel type
    enforces whichever is true rather than silently truncating."""
    fields = tuple(
        PanelField(name=f"Player {i}", value="v" * MAX_FIELD_VALUE_CHARS) for i in range(15)
    )
    total = sum(len(f.name) + len(f.value) for f in fields)
    if total <= MAX_TOTAL_CHARS:
        panel = Panel(title="Roster", fields=fields)
        assert len(panel.fields) == 15
    else:
        with pytest.raises(ValueError, match="6000|combined"):
            Panel(title="Roster", fields=fields)


def test_panel_is_frozen():
    panel = Panel(title="t")
    with pytest.raises(dataclasses.FrozenInstanceError):
        panel.title = "other"  # type: ignore[misc]


def test_panel_field_is_frozen():
    field = PanelField(name="n", value="v")
    with pytest.raises(dataclasses.FrozenInstanceError):
        field.name = "other"  # type: ignore[misc]


def test_panel_color_values_are_valid_24_bit_rgb():
    for color in PanelColor:
        assert 0 <= color.value <= 0xFFFFFF
