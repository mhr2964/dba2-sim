"""`dba2/bot/embeds.py::panel_to_embed` -- Decision P5-4's "single, small,
boring adapter," proved as a real unit test with no live gateway
connection (`discord.Embed` is a plain, local data structure -- see that
module's own docstring)."""
from __future__ import annotations

from dba2.bot.embeds import panel_to_embed
from dba2.presentation.panel import Panel, PanelColor, PanelField


def test_title_and_description_pass_through():
    panel = Panel(title="Standings", description="a table")
    embed = panel_to_embed(panel)
    assert embed.title == "Standings"
    assert embed.description == "a table"


def test_empty_description_becomes_none_not_empty_string():
    """`discord.Embed(description="")` renders as a visible empty line in
    a real client -- `None` omits the description entirely, which is
    what an empty `Panel.description` (the common case, e.g. `/phase`'s
    title-only-adjacent panels) should produce."""
    panel = Panel(title="Standings")
    embed = panel_to_embed(panel)
    assert embed.description is None


def test_color_round_trips_through_panel_color():
    panel = Panel(title="Standings", color=PanelColor.POSITIVE)
    embed = panel_to_embed(panel)
    assert embed.colour.value == PanelColor.POSITIVE.value


def test_fields_copy_name_value_and_inline_in_order():
    panel = Panel(
        title="Team",
        fields=(
            PanelField(name="Record", value="10-5", inline=True),
            PanelField(name="Cap Room", value="$5M", inline=False),
        ),
    )
    embed = panel_to_embed(panel)
    assert len(embed.fields) == 2
    assert embed.fields[0].name == "Record"
    assert embed.fields[0].value == "10-5"
    assert embed.fields[0].inline is True
    assert embed.fields[1].name == "Cap Room"
    assert embed.fields[1].inline is False


def test_footer_and_author_pass_through_when_present():
    panel = Panel(title="Wire", footer="Season 2026", author="League Wire")
    embed = panel_to_embed(panel)
    assert embed.footer.text == "Season 2026"
    assert embed.author.name == "League Wire"


def test_footer_and_author_omitted_when_empty():
    panel = Panel(title="Wire")
    embed = panel_to_embed(panel)
    assert embed.footer.text is None
    assert embed.author.name is None
