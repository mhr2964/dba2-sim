"""`panel_to_embed` -- the ONLY function in the codebase that turns a
`dba2.presentation.panel.Panel` into a `discord.Embed` (Decision P5-4:
"`presentation/` returns `Panel`; `bot/embeds.py` is the single, small,
boring adapter").

docs/design/phase-5-discord-presentation.md §2.2 Option B, §3 module
layout. Deliberately thin: every rendering judgment already happened
inside whichever `dba2/presentation/*.py` builder constructed the
`Panel` (Decision P5-5 -- a rendering layer must never re-derive a fact
a service already derived, and "re-format it a second way" is the same
failure shape wearing a display hat). This module's only job is a 1:1
field mapping, plus the one thing `Panel` cannot express on its own
(Discord's own `Colour` wrapper around a plain `int`).

No network call happens anywhere in this module -- `discord.Embed` is a
plain, local data structure (`discord.py` builds the actual HTTP request
body from it later, inside `TextChannel.send`/`Message.edit`), which is
what makes `test_panel_to_embed_*` a real unit test with no live
gateway connection and no test guild required.
"""
from __future__ import annotations

import discord

from dba2.presentation.panel import Panel


def panel_to_embed(panel: Panel) -> discord.Embed:
    """`Panel`'s own `__post_init__` already validated every length limit
    this function's caller relies on (Decision P5-4) -- this function
    does not re-validate anything, it only copies fields across."""
    embed = discord.Embed(
        title=panel.title,
        description=panel.description or None,
        colour=discord.Colour(int(panel.color)),
    )
    for pf in panel.fields:
        embed.add_field(name=pf.name, value=pf.value, inline=pf.inline)
    if panel.footer:
        embed.set_footer(text=panel.footer)
    if panel.author:
        embed.set_author(name=panel.author)
    return embed
