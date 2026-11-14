"""`resolve_league` -- every Phase 5 command's first real step: turning a
Discord interaction's `guild_id` into the `League` row it belongs to.
`leagues.discord_guild_id` (§1 finding 4) is the join; a bot may sit in
more than one guild, and a guild the bot is in but that has never had a
league created for it is a real, expected state, not a bug.
"""
from __future__ import annotations

import discord

from dba2.data.db import Executor
from dba2.data.repositories import league_repo
from dba2.domain.league import League


class NoLeagueForGuild(Exception):
    """No `leagues` row's `discord_guild_id` matches this interaction's
    guild, or the interaction did not come from a guild at all (a DM)."""


async def resolve_league(ex: Executor, interaction: discord.Interaction) -> League:
    if interaction.guild_id is None:
        raise NoLeagueForGuild("this command must be run in a server, not a DM")
    league = await league_repo.get_league_by_guild_id(ex, interaction.guild_id)
    if league is None:
        raise NoLeagueForGuild(
            f"no league is set up for this server (guild {interaction.guild_id})"
        )
    return league


def current_cycle_index(league: League) -> int:
    """Stage E's orchestrator (unbuilt, §32) is the real owner of "which
    cycle is the league in right now" -- until it exists, `leagues.hook_
    completed_cycle_index` (migration 013, default `-1`, "no cycle's hook
    has completed yet") is the best available proxy: the cycle currently
    in progress is the one AFTER the last one whose hook completed.
    Disclosed as a Stage C compromise, not a claim that this is Stage
    E's real cycle-ordinal derivation (P5-71's own, unbuilt) -- `cycle_
    index` only ever affects a `PulseSnapshot`'s own metadata label
    (`dba2/media/pulse.py::build_pulse_snapshot`'s docstring), never the
    wins/losses/streak arithmetic any command that uses this actually
    shows. One function so every caller (`bot/cogs/read.py`, `bot/cogs/
    commissioner.py`'s `/setup`) shares the identical proxy rather than
    each re-deriving its own.

    Stage E landed (`season_service`, `bot/cogs/advance.py`) and deliberately did
    NOT adopt this: the real `cycle_ordinal` is derived under the `leagues` row
    lock from `phase_history` (P5-99.d.1) and `/advance` labels its standings
    sync with the press report's own; this proxy remains only the display
    fallback for a press that has no ordinal (a non-cycle-bearing phase) and the
    label the read commands use."""
    return max(league.hook_completed_cycle_index + 1, 0)
