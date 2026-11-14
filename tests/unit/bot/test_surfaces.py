"""`dba2/bot/surfaces.py::post_wire_panels` -- Stage C critic round nit:
this function (and its own only caller, `post_new_wire_stories`) had zero
callers and zero tests anywhere in the codebase. `post_new_wire_stories`/
`sync_standings_message` stay untested here (both need a real Postgres
pool via `channel_repo`/`view_service`, out of this pure unit test's
scope) -- `post_wire_panels` alone needs no I/O beyond a fake channel's
`.send`, so it gets a real proof rather than staying entirely unreached.
"""

from __future__ import annotations

import pytest

from dba2.bot.surfaces import post_wire_panels
from dba2.presentation.panel import Panel


class _FakeChannel:
    def __init__(self) -> None:
        self.sent_embeds: list = []

    async def send(self, *, embed) -> None:
        self.sent_embeds.append(embed)


async def test_post_wire_panels_sends_one_message_per_panel_in_order():
    """Decision P5-15: "one message per story," never a batched message
    -- and in the order given, oldest-to-most-recent per `get_wire_
    panels`'s own tail-slice convention."""
    channel = _FakeChannel()
    panels = (
        Panel(title="Story 1", description="first"),
        Panel(title="Story 2", description="second"),
        Panel(title="Story 3", description="third"),
    )

    count = await post_wire_panels(channel, panels)

    assert count == 3
    assert len(channel.sent_embeds) == 3
    assert [e.title for e in channel.sent_embeds] == ["Story 1", "Story 2", "Story 3"]


async def test_post_wire_panels_sends_nothing_for_an_empty_tuple():
    channel = _FakeChannel()
    count = await post_wire_panels(channel, ())
    assert count == 0
    assert channel.sent_embeds == []


async def test_post_wire_panels_returns_the_count_of_sends_that_completed():
    """The return is the number of `channel.send` calls that returned, counted as they
    complete -- not `len(panels)` restated: a send that raises propagates (the caller's
    "send raises => nothing after it is posted or marked" rule) after the earlier ones went
    out, and never yields a count that includes it."""

    class _FailsOnThird(_FakeChannel):
        async def send(self, *, embed) -> None:
            if len(self.sent_embeds) == 2:
                raise RuntimeError("discord is down")
            await super().send(embed=embed)

    channel = _FailsOnThird()
    panels = tuple(Panel(title=f"Story {i}", description=str(i)) for i in range(4))

    with pytest.raises(RuntimeError, match="discord is down"):
        await post_wire_panels(channel, panels)

    assert [e.title for e in channel.sent_embeds] == ["Story 0", "Story 1"]
