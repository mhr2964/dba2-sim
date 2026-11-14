"""`tools/seed_dev_league.py --replace-league`: the plan, the ordering and the refusals,
with fakes only -- no network, no token, no database.

The ordering is the safety property under test: every Discord delete happens before any
`DELETE FROM leagues`, and a Discord failure leaves the rows untouched."""

from __future__ import annotations

import io
import sys
import urllib.error
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tools import seed_dev_league as sdl

GUILD = 4242


# ---------------------------------------------------------------- the pure plan


def test_plan_orders_channels_by_the_given_rows_and_dedupes() -> None:
    plan = sdl.build_replacement_plan(
        GUILD,
        league_rows=[(7, GUILD), (9, GUILD)],
        channel_rows=[(7, 300), (7, 100), (9, 200), (9, 300)],
    )
    assert plan.guild_id == GUILD
    assert plan.league_ids == (7, 9)
    assert plan.channel_ids == (300, 100, 200)


def test_plan_with_no_leagues_is_empty() -> None:
    plan = sdl.build_replacement_plan(GUILD, league_rows=[], channel_rows=[])
    assert plan.league_ids == ()
    assert plan.channel_ids == ()


def test_plan_refuses_a_league_whose_guild_id_differs() -> None:
    with pytest.raises(sdl.ReplaceLeagueError, match="league 8.*guild id 999.*4242"):
        sdl.build_replacement_plan(
            GUILD, league_rows=[(7, GUILD), (8, 999)], channel_rows=[(7, 1), (8, 2)]
        )


def test_plan_refuses_a_channel_that_belongs_to_no_named_league() -> None:
    with pytest.raises(sdl.ReplaceLeagueError, match="channel 55 belongs to league 3"):
        sdl.build_replacement_plan(GUILD, league_rows=[(7, GUILD)], channel_rows=[(3, 55)])


@pytest.mark.parametrize("bad_guild", [0, -5])
def test_plan_refuses_an_invalid_guild_id(bad_guild: int) -> None:
    with pytest.raises(sdl.ReplaceLeagueError, match="not a valid id"):
        sdl.build_replacement_plan(bad_guild, league_rows=[], channel_rows=[])


# ------------------------------------------------- replace_guild_leagues, fakes


class _FakeConn:
    def __init__(self, events: list, delete_count: int) -> None:
        self._events = events
        self._delete_count = delete_count

    async def execute(self, sql: str, *args):
        self._events.append(("sql", sql.split(" WHERE")[0], args))
        return f"DELETE {self._delete_count}"


@pytest.fixture
def harness(monkeypatch):
    """Patches the plan loader and `transaction`, recording every event in order."""
    events: list = []
    state = {"delete_count": None}

    def install(plan: sdl.ReplacementPlan, delete_count: int | None = None) -> None:
        async def fake_load(ex, guild_id):
            events.append(("load", guild_id))
            return plan

        @asynccontextmanager
        async def fake_transaction(ex):
            events.append(("begin",))
            count = len(plan.league_ids) if delete_count is None else delete_count
            yield _FakeConn(events, count)
            events.append(("commit",))

        monkeypatch.setattr(sdl, "load_replacement_plan", fake_load)
        monkeypatch.setattr(sdl, "transaction", fake_transaction)

    return SimpleNamespace(events=events, install=install, state=state)


def _recording_deleter(events: list, gone: frozenset[int] = frozenset(), fail: int | None = None):
    async def delete(channel_id: int) -> bool:
        events.append(("discord", channel_id))
        if channel_id == fail:
            raise sdl.ReplaceLeagueError(
                f"Discord refused to delete channel {channel_id}: HTTP 500"
            )
        return channel_id not in gone

    return delete


async def test_every_discord_delete_precedes_the_database_delete(harness) -> None:
    plan = sdl.ReplacementPlan(GUILD, (7, 9), (100, 200, 300))
    harness.install(plan)
    lines: list[str] = []

    result = await sdl.replace_guild_leagues(
        object(),
        GUILD,
        delete_channel=_recording_deleter(harness.events, gone=frozenset({200})),
        out=lines.append,
    )

    assert result is plan
    kinds = [e[0] for e in harness.events]
    assert kinds == ["load", "discord", "discord", "discord", "begin", "sql", "commit"]
    assert [e[1] for e in harness.events if e[0] == "discord"] == [100, 200, 300]
    sql_event = next(e for e in harness.events if e[0] == "sql")
    assert sql_event[1] == "DELETE FROM leagues"
    assert sql_event[2] == ([7, 9], GUILD)  # scoped by league ids AND guild id
    assert lines == [
        "channel 100: deleted",
        "channel 200: already gone",
        "channel 300: deleted",
        "league 7: removed",
        "league 9: removed",
    ]


async def test_a_discord_failure_aborts_before_any_database_delete(harness) -> None:
    harness.install(sdl.ReplacementPlan(GUILD, (7,), (100, 200, 300)))
    lines: list[str] = []

    with pytest.raises(sdl.ReplaceLeagueError, match="channel 200"):
        await sdl.replace_guild_leagues(
            object(),
            GUILD,
            delete_channel=_recording_deleter(harness.events, fail=200),
            out=lines.append,
        )

    kinds = [e[0] for e in harness.events]
    assert "sql" not in kinds and "begin" not in kinds
    assert [e[1] for e in harness.events if e[0] == "discord"] == [100, 200]  # 300 never tried
    assert lines == ["channel 100: deleted"]  # nothing claims a league was removed


async def test_a_row_count_mismatch_raises_inside_the_transaction(harness) -> None:
    harness.install(sdl.ReplacementPlan(GUILD, (7, 9), ()), delete_count=1)

    with pytest.raises(sdl.ReplaceLeagueError, match="rolled back"):
        await sdl.replace_guild_leagues(
            object(), GUILD, delete_channel=_recording_deleter(harness.events), out=lambda _: None
        )

    assert ("commit",) not in harness.events  # the raise leaves the transaction block


async def test_no_existing_league_calls_nothing(harness) -> None:
    harness.install(sdl.ReplacementPlan(GUILD, (), ()))
    lines: list[str] = []

    await sdl.replace_guild_leagues(
        object(), GUILD, delete_channel=_recording_deleter(harness.events), out=lines.append
    )

    assert [e[0] for e in harness.events] == ["load"]
    assert lines == [f"No existing league for guild {GUILD}; nothing to replace."]


async def test_a_league_without_channels_is_still_removed(harness) -> None:
    harness.install(sdl.ReplacementPlan(GUILD, (7,), ()))
    await sdl.replace_guild_leagues(
        object(), GUILD, delete_channel=_recording_deleter(harness.events), out=lambda _: None
    )
    assert [e[0] for e in harness.events] == ["load", "begin", "sql", "commit"]


# -------------------------------------------------- seed_dev_league's own gating


async def test_without_the_flag_nothing_is_replaced(monkeypatch) -> None:
    """Default behaviour is unchanged: `replace_guild_leagues` is never reached (the
    ValueError guard fires first here, before any pool is opened)."""
    called = []

    async def spy(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(sdl, "replace_guild_leagues", spy)
    with pytest.raises(ValueError, match="real_players_per_team"):
        await sdl.seed_dev_league(
            discord_guild_id=GUILD,
            name="x",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
            real_players_per_team=1,
        )
    assert called == []


def test_replace_league_requires_an_explicit_guild_id(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["seed_dev_league", "--replace-league"])
    with pytest.raises(SystemExit):
        sdl._parse_args()
    assert "--replace-league requires an explicit --guild-id" in capsys.readouterr().err


def test_guild_id_stays_random_without_the_flag(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["seed_dev_league"])
    args = sdl._parse_args()
    assert 10_000_000 <= args.guild_id <= 2_000_000_000
    assert args.replace_league is False

    monkeypatch.setattr(sys, "argv", ["seed_dev_league", "--guild-id", "77", "--replace-league"])
    args = sdl._parse_args()
    assert args.guild_id == 77
    assert args.replace_league is True


# ---------------------------------------- the real Discord function, no network

_TOKEN = "unit-test-token-must-never-appear"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://discord.test", code, "msg", headers, io.BytesIO(b""))


def _urlopen_sequence(monkeypatch, outcomes: list, seen: list):
    it = iter(outcomes)

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sdl.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sdl.time, "sleep", lambda s: None)


def test_blocking_delete_sends_a_bot_authorized_delete_to_the_channel(monkeypatch) -> None:
    seen: list = []
    _urlopen_sequence(monkeypatch, [_FakeResponse()], seen)

    assert sdl._delete_channel_blocking(_TOKEN, 555) is True

    (request,) = seen
    assert request.get_method() == "DELETE"
    assert request.full_url == "https://discord.com/api/v10/channels/555"
    assert request.get_header("Authorization") == f"Bot {_TOKEN}"
    assert request.get_header("User-agent").startswith("DiscordBot (")


@pytest.mark.parametrize("code", [404, 403])
def test_blocking_delete_treats_404_and_403_as_already_gone(monkeypatch, code) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(code)], [])
    assert sdl._delete_channel_blocking(_TOKEN, 555) is False


def test_blocking_delete_aborts_on_other_errors_without_leaking_the_token(monkeypatch) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(500)], [])
    with pytest.raises(sdl.ReplaceLeagueError) as info:
        sdl._delete_channel_blocking(_TOKEN, 555)
    assert "channel 555" in str(info.value) and "HTTP 500" in str(info.value)
    assert _TOKEN not in str(info.value)


def test_blocking_delete_retries_a_short_rate_limit_then_succeeds(monkeypatch) -> None:
    seen: list = []
    _urlopen_sequence(monkeypatch, [_http_error(429, "0.5"), _FakeResponse()], seen)
    assert sdl._delete_channel_blocking(_TOKEN, 555) is True
    assert len(seen) == 2


def test_blocking_delete_gives_up_on_a_long_rate_limit(monkeypatch) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(429, "3600")], [])
    with pytest.raises(sdl.ReplaceLeagueError, match="HTTP 429"):
        sdl._delete_channel_blocking(_TOKEN, 555)


def test_blocking_delete_reports_a_network_failure_by_type_only(monkeypatch) -> None:
    _urlopen_sequence(monkeypatch, [urllib.error.URLError(ConnectionResetError("boom"))], [])
    with pytest.raises(sdl.ReplaceLeagueError, match="ConnectionResetError") as info:
        sdl._delete_channel_blocking(_TOKEN, 555)
    assert "boom" not in str(info.value)


async def test_delete_discord_channel_reads_the_token_from_config_at_call_time(
    monkeypatch, capsys
) -> None:
    calls: list = []

    def fake_blocking(token: str, channel_id: int) -> bool:
        calls.append((token, channel_id))
        return True

    monkeypatch.setattr(sdl, "get_config", lambda: SimpleNamespace(discord_token=_TOKEN))
    monkeypatch.setattr(sdl, "_delete_channel_blocking", fake_blocking)

    assert await sdl.delete_discord_channel(555) is True
    assert calls == [(_TOKEN, 555)]
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out and _TOKEN not in captured.err


async def test_delete_discord_channel_refuses_without_a_token(monkeypatch) -> None:
    monkeypatch.setattr(sdl, "get_config", lambda: SimpleNamespace(discord_token=None))
    with pytest.raises(sdl.ReplaceLeagueError, match="DISCORD_TOKEN is not configured"):
        await sdl.delete_discord_channel(555)
