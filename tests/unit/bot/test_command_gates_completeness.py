"""Stage C critic round nit: nothing asserted that every registered slash
command has a `COMMAND_GATES` entry (Decision P5-17/P5-40's "same commit
that adds the command" rule) -- it was enforced only by `command_gates.
is_command_legal`'s own runtime `KeyError` and builder discipline, which
means a command shipped with no gate would only be discovered the first
time someone actually ran it against a live guild. Built offline, same
technique as `test_client.py`'s B1 proof: `setup_hook` alone populates
the tree with every cog's real global commands, no gateway connection
needed.

**GM-7(a) (docs/design/gm-command-surface.md): `walk_commands()`, not
`get_commands()`.** Verified against the installed library:
`get_commands()` returns TOP-LEVEL commands only, so a `Group` (e.g.
`/gm`) enumerates as a single item named `"gm"` and a lone `{"gm":
ALL_PHASES}` gate entry would satisfy this test while checking NONE of
the group's real subcommands -- precisely the trap Decision P5-17 exists
to prevent, quietly unhooked by a refactor that looks purely cosmetic.
`walk_commands()` recurses into every group; a `Group` instance itself is
skipped (it is a container, never dispatched on its own -- Discord never
calls `/gm` alone), and each leaf's gate key is its `qualified_name`
(`"gm strategy"`) with spaces and hyphens folded to underscores,
matching `command_gates.py`'s own `gm_strategy` etc. This STRENGTHENS the
test: it now also catches a missing gate on any future subcommand of any
future group, which the `get_commands()` version could not.
"""
from __future__ import annotations

from discord import app_commands

from dba2.bot.client import build_bot
from dba2.phase.command_gates import COMMAND_GATES


def _gate_key(qualified_name: str) -> str:
    return qualified_name.replace(" ", "_").replace("-", "_")


async def test_every_registered_slash_command_has_a_command_gates_entry():
    bot = build_bot()
    await bot.setup_hook()
    missing = sorted(
        _gate_key(cmd.qualified_name)
        for cmd in bot.tree.walk_commands()
        if not isinstance(cmd, app_commands.Group)
        and _gate_key(cmd.qualified_name) not in COMMAND_GATES
    )
    assert not missing, (
        f"the following registered slash command(s) have no COMMAND_GATES entry, "
        f"violating Decision P5-17/P5-40: {missing}"
    )
