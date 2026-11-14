"""`dba2/bot/client.py` -- offline proof for Stage C critic round's
Blocking Issue 1: `on_ready`'s guild-scoped `tree.sync(guild=guild)`
reads only `CommandTree._guild_commands[guild.id]`, which nothing
populates on its own when every cog adds its commands to the tree
globally (no `guild=` kwarg anywhere in `cogs/read.py`/`cogs/
commissioner.py`) -- a bare `tree.sync(guild=guild)` therefore sent an
EMPTY command list, silently un-registering every slash command rather
than syncing the real (global) one.

No live gateway connection is needed to prove this: `setup_hook` alone
populates the tree's global command list (`load_extension` -> `add_cog`
-> `CommandTree.add_command`), and `CommandTree.copy_global_to`/`_get_
all_commands` are pure, in-memory dict operations against that tree --
the exact lookup `CommandTree.sync` itself performs before making any
HTTP call.

Also covers critic round 6's structural backstop (`DBABot._on_app_
command_error`, registered onto `self.tree` via `tree.error(...)` in
`__init__`): proves the backstop actually fires through `discord.py`'s
own REAL dispatch path (`CommandTree._call`), not just as a direct call
to the handler method, by registering a deliberately-broken synthetic
command on the real tree and driving `tree._call` against a minimal fake
`Interaction`. No live gateway connection is needed for this either --
`Command._do_call` wraps ANY exception the callback raises into a
`CommandInvokeError` (a real, in-process `discord.py` behavior, not
network I/O), and `CommandTree._call`/`_dispatch_error` route it to
`self.on_error` the same way for a synthetic command as for a real one.
"""
from __future__ import annotations

import discord

from dba2.bot.client import build_bot

_EXPECTED_COMMAND_NAMES = {
    "standings",
    "team",
    "box",
    "wire",
    "leaders",
    "phase",
    "setup",
    "assign-manager",
    "remove-manager",
    # Stage D' (docs/design/phase-5-discord-presentation.md §21 JC-4, §29)
    "waive",
    "release",
    "sign-free-agent",
    # GM-6 (docs/design/gm-command-surface.md): `/set-strategy`, `/set-role`,
    # `/set-directive` and `/timeline` moved under the `/gm` group -- see
    # `_EXPECTED_GM_SUBCOMMAND_NAMES` below for their new qualified names.
    "gm",
    # The trade-offer review surface (docs/design/phase-5-trade-offer-review-
    # surface.md §4)
    "propose-trade",
    "offers",
    "offer-respond",
    "counter-offer",
    "veto-offer",
    # Stage D's broadcast (docs/design/phase-5-stage-d-broadcast.md §7)
    "watch",
    # Stage D'-25 (docs/design/phase-5-stage-d-prime-draft.md) -- one human
    # draft pick, with the AI draft resumed behind it.
    "draft",
    # Stage E (docs/design/phase-5-stage-e-advance.md P5-99.a) -- the commissioner's
    # `/advance`, and its five companions (build-plan commit 15).
    "advance",
    "advance-to",
    "advance-force",
    "advance-unlock",
    "advance-postpone-game",
    "advance-restore-game",
    # `/advance-run` and its stop (docs/design/advance-run.md).
    "advance-run",
    "advance-run-stop",
}

# GM-6/GM-7 (docs/design/gm-command-surface.md): `/gm`'s own subcommands,
# by QUALIFIED name (`"gm strategy"`, not `"strategy"` -- two different
# groups could otherwise both name a `strategy` subcommand and this set
# would not tell them apart). `_EXPECTED_COMMAND_NAMES` above only ever
# sees the top-level `"gm"` group name once `get_commands()` is used, so
# without this second assertion the three tests below would stop
# asserting anything about the moved commands while still passing
# (GM-7(c)) -- extended here with `gm auto`/`gm status`/`gm player` by
# the later commits that add them.
_EXPECTED_GM_SUBCOMMAND_NAMES = {
    "gm strategy",
    "gm role",
    "gm directive",
    "gm timeline",
    "gm auto",
    "gm status",
    "gm player",
}


class _FakeGuild:
    """A bare `discord.abc.Snowflake` -- `copy_global_to`/`_get_all_
    commands`/dict lookups on `_guild_commands` only ever read `.id`."""

    id = 987_654_321


async def test_setup_hook_registers_every_cog_command_globally():
    """The half of B1 that already worked: `load_extension` adds every
    cog's `@app_commands.command` to the tree as a GLOBAL command -- the
    9 real Stage C commands named in the design doc's own execution
    record (§11 step 9's read surface plus `/setup`/`/assign-manager`/
    `/remove-manager`), plus Stage D''s 7 roster-decision commands (§21
    JC-4's six plus `/timeline`, §23 D'-17/D'-23)."""
    bot = build_bot()
    await bot.setup_hook()
    names = {c.name for c in bot.tree.get_commands()}
    assert names == _EXPECTED_COMMAND_NAMES


async def test_the_gm_group_registers_every_expected_subcommand():
    """GM-7(c): a second assertion over qualified names, so a subcommand
    moved out of (or never added to) `/gm` is caught even though it would
    not change `_EXPECTED_COMMAND_NAMES` at all -- that set only ever sees
    the top-level `"gm"` name."""
    bot = build_bot()
    await bot.setup_hook()
    qualified = {c.qualified_name for c in bot.tree.walk_commands()}
    assert _EXPECTED_GM_SUBCOMMAND_NAMES <= qualified


async def test_guild_scoped_sync_target_is_empty_before_copy_global_to():
    """Reproduces the shipped bug directly: `_guild_commands[guild.id]`
    is unset (nothing has ever been guild-scoped) even after every cog
    has loaded and the tree carries every global command -- a bare
    `tree.sync(guild=guild)` would have read this empty target and sent
    Discord zero commands, wiping out registration rather than syncing
    it."""
    bot = build_bot()
    await bot.setup_hook()
    assert bot.tree._guild_commands.get(_FakeGuild.id) is None
    assert len(bot.tree.get_commands()) == len(_EXPECTED_COMMAND_NAMES)


async def test_copy_global_to_carries_every_command_into_the_guild_scoped_sync_target():
    """The real fix (`_reconcile_guild`'s `tree.copy_global_to(guild=...)`
    immediately before `tree.sync(guild=...)`): after copying, `_get_all_
    commands(guild=guild)` -- the same lookup `CommandTree.sync` itself
    makes internally before building the HTTP payload -- returns the
    full 9 commands, not the empty set B1 shipped with."""
    bot = build_bot()
    await bot.setup_hook()
    guild = _FakeGuild()
    bot.tree.copy_global_to(guild=guild)
    synced_target = bot.tree._get_all_commands(guild=guild)
    assert {c.name for c in synced_target} == _EXPECTED_COMMAND_NAMES


class _BackstopFakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.sent_messages: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        self._done = True
        self.sent_messages.append(message)


class _BackstopFakeFollowup:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, message: str, *, ephemeral: bool = False) -> None:
        self.sent_messages.append(message)


class _BackstopFakeInteraction:
    """The minimal shape `CommandTree._call` actually reads for a plain,
    no-parameter slash command with nothing resolved -- `interaction.
    data` (the raw payload naming the command and its options), `.type`
    (checked against `InteractionType.autocomplete`), and `._state`/
    `.guild_id`/`.guild` (read unconditionally by `Namespace._get_
    resolved_items`, even for an empty `resolved` dict). Reusing `test_
    commissioner_cog.py`'s own `_FakeResponse`/`_FakeFollowup` shape for
    `.response`/`.followup` -- the two attributes the backstop itself
    reads and writes to."""

    def __init__(self, command_name: str) -> None:
        self.data = {"name": command_name, "options": []}
        self.type = discord.InteractionType.application_command
        self._state = None
        self.guild_id = None
        self.guild = None
        self.command_failed = False
        self.command = None
        self.response = _BackstopFakeResponse()
        self.followup = _BackstopFakeFollowup()


async def test_tree_error_backstop_fires_through_the_real_dispatch_path_on_an_uncaught_exception():
    """Critic round 6's structural backstop -- proven through `discord.
    py`'s own real dispatch path (`CommandTree._call`), not a direct call
    to the handler, so this also proves the handler is actually WIRED to
    the tree and not just correct in isolation. A synthetic command whose
    body raises a plain `RuntimeError` it never catches itself stands in
    for "some future command's own error handling has a gap" -- exactly
    the scenario the backstop exists for. `Command._do_call` wraps this
    into a `CommandInvokeError` (a real, documented `discord.py` behavior,
    verified by reading `commands.py`'s own `_do_call`), which `CommandTree
    ._call` routes to `self.on_error` -- `DBABot._on_app_command_error`,
    registered in `__init__` via `tree.error(...)`."""
    bot = build_bot()
    await bot.setup_hook()

    @bot.tree.command(name="deliberately-broken", description="test-only: always raises")
    async def _broken(interaction: discord.Interaction) -> None:
        raise RuntimeError("simulated unanticipated failure the command's own code never catches")

    interaction = _BackstopFakeInteraction("deliberately-broken")

    await bot.tree._call(interaction)  # must not raise -- the backstop must catch this

    assert len(interaction.response.sent_messages) == 1
    assert len(interaction.followup.sent_messages) == 0
    message = interaction.response.sent_messages[0].lower()
    assert "went wrong" in message


async def test_tree_error_backstop_uses_a_followup_when_the_interaction_was_already_deferred():
    """The backstop must pick `followup.send` over `response.send_message`
    once the interaction is already past its initial response (Discord
    rejects a second `response.send_message` call) -- the same `response.
    is_done()` branch `CommissionerCommands._reply_error` already uses for
    its own specific errors."""
    bot = build_bot()
    await bot.setup_hook()

    @bot.tree.command(
        name="deliberately-broken-after-defer", description="test-only: always raises"
    )
    async def _broken(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("placeholder", ephemeral=True)
        raise RuntimeError("simulated failure after the command already responded once")

    interaction = _BackstopFakeInteraction("deliberately-broken-after-defer")

    await bot.tree._call(interaction)

    assert interaction.response.sent_messages == ["placeholder"]
    assert len(interaction.followup.sent_messages) == 1
    message = interaction.followup.sent_messages[0].lower()
    assert "went wrong" in message


# --- Stage D: the block registry, the scheduler and the reveal seam ----------


async def test_the_bot_owns_a_block_registry_and_a_scheduler_wired_to_the_reveal_rule():
    """`bot.blocks` / `bot.broadcasts` are per-bot (two bots never share state),
    and the scheduler's `on_ended` is `game_feed.on_ended` bound to THAT bot, so
    a finished broadcast asks that bot's registry whether its block is closed."""
    import functools

    from dba2.bot import game_feed
    from dba2.bot.block_registry import BlockRegistry
    from dba2.bot.broadcast import BroadcastScheduler

    first, second = build_bot(), build_bot()
    assert isinstance(first.blocks, BlockRegistry)
    assert isinstance(first.broadcasts, BroadcastScheduler)
    assert first.blocks is not second.blocks and first.broadcasts is not second.broadcasts
    on_ended = first.broadcasts._on_ended
    assert isinstance(on_ended, functools.partial)
    assert on_ended.func is game_feed.on_ended and on_ended.args == (first,)


async def test_closing_the_bot_shuts_the_scheduler_down_first_and_survives_its_failure(
    monkeypatch,
):
    from discord.ext import commands

    bot = build_bot()
    order: list[str] = []

    async def shutdown() -> None:
        order.append("shutdown")
        raise RuntimeError("a broadcast refused to unwind")

    async def close(self) -> None:
        order.append("close")

    monkeypatch.setattr(bot.broadcasts, "shutdown", shutdown)
    monkeypatch.setattr(commands.Bot, "close", close)
    await bot.close()  # a failing shutdown must not stop the bot from closing
    assert order == ["shutdown", "close"]
