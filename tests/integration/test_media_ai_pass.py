"""`media_service.rewrite_cycle_stories_with_ai` -- the AI rewrite pass
(docs/design/media-v2-step8-anthropic-writer.md S8-15 items 3, 4 and 6).

Every test injects a fake runner; no real `claude` process is ever spawned.
The determinism test is the most important one in step 8: it proves an
external, non-deterministic process cannot corrupt the simulation, because
the pass changes exactly three `media_stories` columns and nothing else.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from dba2.core.config import Config
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, media_repo, team_repo
from dba2.domain.media_facts import StoryPackage
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, StoryKind, derive_frame
from dba2.media.salience import SalienceCandidate, ScoredCandidate
from dba2.services import media_service

_SEASON = 2026
_CYCLE = 3
_REPO = Path(__file__).resolve().parents[2]


def _config(**overrides) -> Config:
    base = {
        "database_url": "postgresql://unused/unused",
        "media_ai_enabled": True,
        "media_ai_concurrency": 1,
        "media_ai_max_stories_per_cycle": 6,
    }
    base.update(overrides)
    return Config(**base)


async def _make_league(pool, *, rng_seed: int = 99) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Media AI Pass Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=rng_seed,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool, league_id=league_id, code=code, name=f"{code} Team", city=f"{code} City",
        conference="East", division="Atlantic", home_market_size=3,
        gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.RETOOL,
    )
    return team.id


async def _scored_streak(pool, team_id: int, *, score: float, length: int = 9) -> ScoredCandidate:
    candidate = StoryCandidate(
        kind=StoryKind.STREAK, team_id=team_id, team_games_played_this_season=20,
        has_pulse_snapshot=True, streak_length=length,
    )
    draft = legacy.adapt_frame(
        derive_frame(candidate), season=_SEASON, phase=Phase.REGULAR_SEASON, cycle_index=_CYCLE
    )
    assert draft is not None
    team = await team_repo.get_team(pool, team_id)
    sealed = media_service.seal_package(
        draft, team_names={team_id: f"{team.city} {team.name}"}, player_names={}
    )
    assert sealed is not None
    return ScoredCandidate(candidate=SalienceCandidate(package=sealed), score=score)


async def _seed_cycle(
    pool, league_id: int, scores, *, persona: str | None = None, team_ids=None
):
    """One template story per score, salience exactly `score`. Returns
    `(library, {team_code: story})` in creation order. Pass `team_ids`
    (a `{code: id}` map) to re-run the identical cycle over existing teams."""
    library = media_service.load_media_library()
    use_counts: dict = {}
    stories = {}
    for i, score in enumerate(scores):
        code = "ABCDEFGH"[i]
        team_id = team_ids[code] if team_ids else await _make_team(pool, league_id, code)
        scored = await _scored_streak(pool, team_id, score=score)
        stories[code] = await media_service.select_and_bind_story(
            pool, league_id=league_id, league_rng_seed=42, season=_SEASON,
            phase=Phase.REGULAR_SEASON, cycle_index=_CYCLE, scored=scored, library=library,
            use_counts=use_counts, candidate_sequence=i,
            eligible_personas=frozenset({persona}) if persona else None,
        )
    return library, stories


def _payload_of(stdin: bytes) -> dict:
    """The JSON payload at the head of stdin; a retry with feedback appends
    the gate's note after it (G2-18), so this never parses the whole stream."""
    payload, _ = json.JSONDecoder().raw_decode(stdin.decode("utf-8"))
    return payload


def _team_of(stdin: bytes) -> str:
    return _payload_of(stdin)["story"]["subject"]["team_name"]


def _envelope(headline: str, body: str) -> bytes:
    return json.dumps(
        {
            "subtype": "success", "is_error": False,
            "structured_output": {"headline": headline, "body": body},
            "total_cost_usd": 0.01, "duration_ms": 1000,
            "usage": {"input_tokens": 1500, "output_tokens": 60},
        }
    ).encode()


class Runner:
    """Fake runner: good prose (only the story's own team name) except for
    teams named in `bad_codes`, which get an invented person."""

    def __init__(self, *, bad_codes=(), exc=None, cli_error=False):
        self.bad_codes = set(bad_codes)
        self.exc = exc
        self.cli_error = cli_error
        self.teams: list[str] = []

    async def __call__(self, argv, stdin, cwd, env, timeout):
        team = _team_of(stdin)
        self.teams.append(team)
        if self.exc is not None:
            raise self.exc
        if self.cli_error:
            body = json.dumps({"subtype": "error_during_execution", "is_error": True,
                               "result": "You have hit your session limit"})
            return 0, body.encode(), b""
        if team.split()[0] in self.bad_codes:
            return 0, _envelope("Big move", f"{team} added Tyrese Holloway today."), b""
        return 0, _envelope(f"{team} keep rolling", f"{team} keep rolling in a fine run."), b""


async def _rows(pool, league_id: int) -> dict[int, dict]:
    rows = await pool.fetch(
        "SELECT * FROM media_stories WHERE league_id = $1 ORDER BY id", league_id
    )
    return {r["id"]: dict(r) for r in rows}


_TEXT_COLUMNS = {"headline", "rendered_text", "writer_backend"}


async def _with_league(body):
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await body(pool, league_id)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_flag_off_reads_and_writes_nothing_and_never_calls_the_runner():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8])
        before = await _rows(pool, league_id)
        runner = Runner()
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_enabled=False), runner=runner,
        )
        assert summary.disabled_reason == "disabled" and summary.attempted == 0
        assert runner.teams == []
        assert await _rows(pool, league_id) == before

    await _with_league(body)


@pytest.mark.asyncio
async def test_only_passing_stories_are_updated_and_failures_stay_byte_identical():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6])
        before = await _rows(pool, league_id)
        runner = Runner(bad_codes={"B", "D"})
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        after = await _rows(pool, league_id)
        ids = {code: s.id for code, s in stories.items()}
        assert summary.attempted == 4 and summary.accepted == 2
        assert summary.failures == {"gate_failed": 2} and not summary.aborted
        # B and D are rejected on every one of their 3 attempts (G2-18).
        assert summary.calls == 8 and summary.retried == 2 and summary.accepted_on_retry == 0
        assert summary.gate_rejections == {"proper_nouns": 6}
        assert summary.updated_story_ids == tuple(sorted((ids["A"], ids["C"])))
        for code in "AC":
            row = after[ids[code]]
            assert row["writer_backend"] == "anthropic:sonnet"
            assert row["rendered_text"].endswith("in a fine run.")
            assert row["headline"] != before[ids[code]]["headline"]
        for code in "BD":
            assert after[ids[code]] == before[ids[code]]  # byte-identical row
            assert after[ids[code]]["writer_backend"] == "template"
        assert summary.mean_input_tokens == 1500 and summary.total_cost_usd == pytest.approx(0.08)

    await _with_league(body)


@pytest.mark.asyncio
async def test_a_second_pass_does_not_rewrite_already_rewritten_stories():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8])
        runner = Runner()
        kwargs = dict(league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
                      config=_config(), runner=runner)
        first = await media_service.rewrite_cycle_stories_with_ai(pool, **kwargs)
        second = await media_service.rewrite_cycle_stories_with_ai(pool, **kwargs)
        assert first.accepted == 2 and second.attempted == 0 and second.accepted == 0

    await _with_league(body)


@pytest.mark.asyncio
async def test_posted_stories_are_never_rewritten():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8])
        await media_repo.mark_media_story_posted(pool, stories["A"].id)
        runner = Runner()
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        assert summary.attempted == 1 and summary.updated_story_ids == (stories["B"].id,)

    await _with_league(body)


@pytest.mark.asyncio
async def test_the_per_cycle_cap_takes_the_highest_salience_stories_first():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.5, 0.9, 0.7, 0.6])
        runner = Runner()
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_max_stories_per_cycle=2), runner=runner,
        )
        assert summary.attempted == 2 and summary.skipped == 2
        assert set(summary.updated_story_ids) == {stories["B"].id, stories["C"].id}
        assert runner.teams == ["B City B Team", "C City C Team"]  # salience order

    await _with_league(body)


@pytest.mark.asyncio
async def test_persona_filter_is_honored():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7])
        voices = {s.voice for s in stories.values()}
        keep = sorted(voices)[0]
        runner = Runner()
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_personas=keep), runner=runner,
        )
        expected = {s.id for s in stories.values() if s.voice == keep}
        assert set(summary.updated_story_ids) == expected
        after = await _rows(pool, league_id)
        for s in stories.values():
            if s.voice != keep:
                assert after[s.id]["writer_backend"] == "template"

    await _with_league(body)


@pytest.mark.asyncio
async def test_circuit_breaker_aborts_after_three_consecutive_failures_and_leaves_the_rest():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6, 0.5])
        before = await _rows(pool, league_id)
        runner = Runner(exc=TimeoutError())
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        assert summary.aborted and summary.attempted == 3 and summary.skipped == 2
        assert summary.failures == {"timeout": 3} and summary.accepted == 0
        assert summary.calls == 3 and summary.retried == 0  # a timeout is never retried
        assert await _rows(pool, league_id) == before

    await _with_league(body)


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_two_consecutive_cli_errors():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6])
        runner = Runner(cli_error=True)
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        assert summary.aborted and summary.attempted == 2
        assert summary.failures == {"cli_error": 2}
        assert summary.calls == 2 and summary.gate_rejections == {}

    await _with_league(body)


@pytest.mark.asyncio
async def test_a_success_resets_the_consecutive_failure_count():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
        runner = Runner(bad_codes={"A", "B", "D", "E"})  # F,C good: fail,fail,ok,fail,fail,ok
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        assert not summary.aborted and summary.attempted == 6 and summary.accepted == 2

    await _with_league(body)


@pytest.mark.asyncio
async def test_results_apply_in_story_id_order_regardless_of_completion_order():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6])
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_concurrency=4), runner=Runner(),
        )
        assert list(summary.updated_story_ids) == sorted(s.id for s in stories.values())

    await _with_league(body)


async def _league_table_checksums(pool, league_id: int) -> dict[str, str]:
    tables = await pool.fetch(
        """
        SELECT c.table_name FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_name = c.table_name AND t.table_schema = c.table_schema
        WHERE c.table_schema = 'public' AND c.column_name = 'league_id'
          AND t.table_type = 'BASE TABLE' AND c.table_name <> 'media_stories'
        ORDER BY c.table_name
        """
    )
    sums = {}
    for r in tables:
        name = r["table_name"]
        sums[name] = await pool.fetchval(
            f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
            f"FROM {name} x WHERE league_id = $1",
            league_id,
        )
    return sums


@pytest.mark.asyncio
async def test_determinism_the_pass_changes_exactly_three_media_stories_columns_and_nothing_else():
    """S8-15 item 4: every `media_stories` column except headline,
    rendered_text and writer_backend is byte-identical after a pass, and every
    other league-scoped table (including media_pulse_snapshot and
    media_writers) is untouched."""

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7])
        rows_before = await _rows(pool, league_id)
        sums_before = await _league_table_checksums(pool, league_id)
        assert len(sums_before) > 5  # the sweep really covered the schema
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=Runner(),
        )
        assert summary.accepted == 3
        rows_after = await _rows(pool, league_id)
        assert rows_after.keys() == rows_before.keys()
        for story_id, before in rows_before.items():
            after = rows_after[story_id]
            assert {k: v for k, v in before.items() if k not in _TEXT_COLUMNS} == {
                k: v for k, v in after.items() if k not in _TEXT_COLUMNS
            }
            assert after["writer_backend"] != before["writer_backend"]
        assert await _league_table_checksums(pool, league_id) == sums_before

    await _with_league(body)


@pytest.mark.asyncio
async def test_determinism_the_same_seeded_cycle_selects_identically_with_the_flag_on_or_off():
    """S8-15 item 4's literal form: run the same seeded cycle twice -- once
    followed by a flag-on pass, once by a flag-off pass -- and every
    `media_stories` column except headline/rendered_text/writer_backend (and
    the surrogate id/created_at/writer_id) matches. Same league and same team
    ids both times, because the RNG salts include DB ids and two different
    leagues could never be compared."""

    async def body(pool, league_id):
        scores = [0.9, 0.8, 0.7]
        library, _ = await _seed_cycle(pool, league_id, scores)
        team_ids = {
            t.code: t.id for t in await team_repo.list_teams_for_league(pool, league_id)
        }
        await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_enabled=True), runner=Runner(),
        )
        skip = _TEXT_COLUMNS | {"id", "created_at", "writer_id"}
        with_ai = [
            {k: v for k, v in r.items() if k not in skip}
            for r in (await _rows(pool, league_id)).values()
        ]
        await pool.execute("DELETE FROM media_stories WHERE league_id = $1", league_id)
        library, _ = await _seed_cycle(pool, league_id, scores, team_ids=team_ids)
        await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(media_ai_enabled=False), runner=Runner(),
        )
        without_ai = [
            {k: v for k, v in r.items() if k not in skip}
            for r in (await _rows(pool, league_id)).values()
        ]
        assert with_ai == without_ai and len(with_ai) == 3

    await _with_league(body)


@pytest.mark.asyncio
async def test_missing_cli_disables_the_pass_without_raising(monkeypatch):
    from dba2.services import media_writer_llm

    monkeypatch.setattr(media_writer_llm, "resolve_cli_path", lambda configured=None: None)

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9])
        before = await _rows(pool, league_id)
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(),  # no injected runner -> the real-runner path
        )
        assert summary.disabled_reason == "claude CLI not found" and summary.attempted == 0
        assert await _rows(pool, league_id) == before

    await _with_league(body)


def test_the_pass_is_post_commit_and_the_corpus_harness_never_enables_it():
    """S8-15 item 6 / S8-1: the in-cycle path (`season_service`) and the
    determinism-dependent corpus harness must never reach the AI writer."""
    forbidden = ("rewrite_cycle_stories_with_ai", "AnthropicWriter", "media_ai_enabled",
                 "media_writer_llm")
    for path in (
        _REPO / "tools" / "generate_media_corpus.py",
        _REPO / "dba2" / "services" / "season_service.py",
    ):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{path.name} must not reference {name}"


def test_story_package_rehydration_round_trips_a_real_sealed_package():
    from tests.unit._media_ai_fixtures import make_package

    payload = make_package().to_prompt_json()
    assert StoryPackage.from_prompt_json(payload).to_prompt_json() == payload
    with pytest.raises(KeyError):
        StoryPackage.from_prompt_json({})  # a pre-migration-024 row's default



class BodyRunner:
    """Fake runner returning a fixed body per story team code."""

    def __init__(self, bodies: dict[str, str]):
        self.bodies = bodies

    async def __call__(self, argv, stdin, cwd, env, timeout):
        code = _team_of(stdin).split()[0]
        return 0, _envelope("Streak watch", self.bodies[code]), b""


@pytest.mark.asyncio
async def test_bound_gate_rejects_a_number_stated_about_another_team_and_aliases_reach_it():
    """Gates v2 (G2-12 item 4). Each story is a 9-game streak about its own
    team. A's body gives the streak to B's nickname: 9 is true of A, not B, so
    the bound gate rejects it and the row keeps its template text (the pooled
    gate would have accepted it: 9 is in the pool). B's body uses its own
    nickname only, so it passes only because the team aliases reached the
    gate from `list_teams_for_league`."""

    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8])
        before = await _rows(pool, league_id)
        runner = BodyRunner({
            "A": "B Team are on a 9-game win streak.",
            "B": "B Team are on a 9-game win streak.",
        })
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
            config=_config(), runner=runner,
        )
        after = await _rows(pool, league_id)
        assert summary.failures == {"gate_failed": 1}
        assert summary.updated_story_ids == (stories["B"].id,)
        assert after[stories["A"].id] == before[stories["A"].id]  # template row untouched
        assert after[stories["B"].id]["rendered_text"] == "B Team are on a 9-game win streak."

    await _with_league(body)


# -- the gate retry (docs/design/media-v2-step8-gates-v2.md G2-18) -------------

_GOOD = "good"
_INVENTED = "invented"  # an invented person: proper_nouns
_WRONG_NUMBER = "wrong_number"  # a 12-game streak; the story's own streak is 9 games
_TIMEOUT = "timeout"
_CLI_ERROR = "cli_error"


class ScriptRunner:
    """Replies per team code from a script, one entry per call, the last entry
    repeating. Records every call's team code, argv and stdin."""

    def __init__(self, script: dict[str, list[str]]):
        self.script = {code: list(steps) for code, steps in script.items()}
        self.calls: list[tuple[str, list[str], bytes]] = []

    def calls_for(self, code: str) -> list[tuple[str, list[str], bytes]]:
        return [c for c in self.calls if c[0] == code]

    async def __call__(self, argv, stdin, cwd, env, timeout):
        team = _team_of(stdin)
        code = team.split()[0]
        self.calls.append((code, list(argv), stdin))
        steps = self.script.get(code, [_GOOD])
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if step == _TIMEOUT:
            raise TimeoutError()
        if step == _CLI_ERROR:
            body = json.dumps({"subtype": "error_during_execution", "is_error": True,
                               "result": "You have hit your session limit"})
            return 0, body.encode(), b""
        if step == _INVENTED:
            return 0, _envelope("Big move", f"{team} added Tyrese Holloway today."), b""
        if step == _WRONG_NUMBER:
            return 0, _envelope("Streak watch", f"{team} are on a 12-game win streak."), b""
        return 0, _envelope(f"{team} keep rolling", f"{team} keep rolling in a fine run."), b""


async def _run(pool, league_id, library, runner, **config):
    return await media_service.rewrite_cycle_stories_with_ai(
        pool, league_id=league_id, season=_SEASON, cycle_index=_CYCLE, library=library,
        config=_config(**config), runner=runner,
    )


@pytest.mark.parametrize(
    "script,calls",
    [([_INVENTED, _GOOD], 2), ([_INVENTED, _WRONG_NUMBER, _GOOD], 3)],
    ids=["attempt-2", "attempt-3"],
)
@pytest.mark.asyncio
async def test_a_rejected_story_is_retried_and_accepted(script, calls):
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9])
        runner = ScriptRunner({"A": script})
        summary = await _run(pool, league_id, library, runner)
        assert summary.updated_story_ids == (stories["A"].id,)
        assert (summary.attempted, summary.accepted, summary.calls) == (1, 1, calls)
        assert summary.retried == 1 and summary.accepted_on_retry == 1
        assert summary.failures == {} and not summary.aborted
        (log,) = summary.stories
        assert log.story_id == stories["A"].id and log.outcome == "accepted"
        assert log.calls == calls and log.attempts[-1].result == "accepted"
        assert [a.result for a in log.attempts[:-1]] == (
            ["proper_nouns", "numbers_bound"][: calls - 1]
        )
        row = (await _rows(pool, league_id))[stories["A"].id]
        assert row["writer_backend"] == "anthropic:sonnet"
        assert row["rendered_text"].endswith("in a fine run.")

    await _with_league(body)


@pytest.mark.asyncio
async def test_every_retry_runs_every_gate_again():
    """Attempt 2 fixes the invented name but states a wrong number; the
    number gate catches it, so the story needs a third call."""

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9])
        runner = ScriptRunner({"A": [_INVENTED, _WRONG_NUMBER, _GOOD]})
        summary = await _run(pool, league_id, library, runner)
        (log,) = summary.stories
        assert [a.result for a in log.attempts] == ["proper_nouns", "numbers_bound", "accepted"]
        assert log.attempts[1].offending == (
            "streak '12-game win streak' -> A City A Team; bound to nothing",
        )
        assert summary.gate_rejections == {"numbers_bound": 1, "proper_nouns": 1}

    await _with_league(body)


@pytest.mark.asyncio
async def test_a_story_rejected_on_every_attempt_keeps_its_template_row_byte_identical():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9])
        before = await _rows(pool, league_id)
        runner = ScriptRunner({"A": [_INVENTED]})
        summary = await _run(pool, league_id, library, runner)
        assert await _rows(pool, league_id) == before
        assert summary.calls == 3 and summary.accepted == 0 and summary.retried == 1
        assert summary.failures == {"gate_failed": 1}
        assert summary.gate_rejections == {"proper_nouns": 3}
        assert summary.stories[0].outcome == "gate_failed"

    await _with_league(body)


@pytest.mark.asyncio
async def test_max_attempts_1_is_the_original_single_call_behavior():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8])
        runner = ScriptRunner({"A": [_INVENTED, _GOOD]})
        summary = await _run(pool, league_id, library, runner, media_ai_max_attempts=1)
        assert summary.calls == 2 and summary.retried == 0 and summary.accepted == 1
        assert summary.failures == {"gate_failed": 1}
        assert len(runner.calls_for("A")) == 1

    await _with_league(body)


@pytest.mark.parametrize("failure", [_TIMEOUT, _CLI_ERROR])
@pytest.mark.asyncio
async def test_a_writer_failure_is_never_retried_by_the_gate_loop(failure):
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8])
        before = await _rows(pool, league_id)
        runner = ScriptRunner({"A": [failure, _GOOD]})
        summary = await _run(pool, league_id, library, runner)
        assert len(runner.calls_for("A")) == 1
        assert summary.failures == {failure: 1} and summary.retried == 0
        assert summary.calls == 2 and summary.updated_story_ids == (stories["B"].id,)
        assert (await _rows(pool, league_id))[stories["A"].id] == before[stories["A"].id]
        assert summary.stories[0].attempts == (media_service.AttemptRecord(failure),)

    await _with_league(body)


@pytest.mark.asyncio
async def test_the_breaker_counts_stories_still_failing_not_raw_rejections():
    """Five rejections in a row, but every story lands on a retry: under the
    old per-rejection count this pass would have aborted after its third."""

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6])
        runner = ScriptRunner({
            "A": [_INVENTED, _INVENTED, _GOOD], "B": [_INVENTED, _GOOD],
            "C": [_INVENTED, _INVENTED, _GOOD], "D": [_GOOD],
        })
        summary = await _run(pool, league_id, library, runner)
        assert not summary.aborted and summary.accepted == 4
        assert summary.calls == 9 and summary.retried == 3 and summary.accepted_on_retry == 3

    await _with_league(body)


@pytest.mark.asyncio
async def test_the_breaker_trips_after_three_stories_exhaust_their_attempts():
    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6, 0.5])
        before = await _rows(pool, league_id)
        runner = ScriptRunner({"A": [_INVENTED], "B": [_INVENTED], "C": [_INVENTED]})
        summary = await _run(pool, league_id, library, runner)
        assert summary.aborted and summary.attempted == 3 and summary.skipped == 2
        assert summary.calls == 9 and summary.failures == {"gate_failed": 3}
        assert runner.calls_for("D") == [] and runner.calls_for("E") == []
        assert await _rows(pool, league_id) == before

    await _with_league(body)


@pytest.mark.asyncio
async def test_a_story_accepted_on_retry_resets_the_breaker():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6, 0.5])
        runner = ScriptRunner({
            "A": [_INVENTED], "B": [_INVENTED], "C": [_INVENTED, _GOOD],
            "D": [_INVENTED], "E": [_INVENTED],
        })
        summary = await _run(pool, league_id, library, runner)
        assert not summary.aborted and summary.attempted == 5 and summary.accepted == 1
        assert summary.failures == {"gate_failed": 4}

    await _with_league(body)


@pytest.mark.asyncio
async def test_the_call_budget_stops_retries_before_it_starves_a_later_story():
    """Budget 4 over three stories (concurrency 1, salience order A, B, C): A
    may retry once (1 call, 2 held for B and C), then no more; B may not
    retry (C still holds one); C gets its one call and lands."""

    async def body(pool, league_id):
        library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7])
        runner = ScriptRunner({"A": [_INVENTED, _INVENTED, _GOOD], "B": [_INVENTED, _GOOD]})
        summary = await _run(pool, league_id, library, runner, media_ai_max_calls_per_cycle=4)
        assert summary.calls == 4 and summary.call_budget == 4 and summary.budget_exhausted
        assert [len(runner.calls_for(c)) for c in "ABC"] == [2, 1, 1]
        assert summary.updated_story_ids == (stories["C"].id,)
        assert summary.failures == {"gate_failed": 2} and not summary.aborted

    await _with_league(body)


@pytest.mark.asyncio
async def test_a_spent_call_budget_skips_the_remaining_stories():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7])
        runner = ScriptRunner({})
        summary = await _run(pool, league_id, library, runner, media_ai_max_calls_per_cycle=2,
                             media_ai_concurrency=2)
        assert summary.calls == 2 and summary.attempted == 2 and summary.skipped == 1
        assert summary.budget_exhausted and summary.accepted == 2
        assert runner.calls_for("C") == []

    await _with_league(body)


@pytest.mark.asyncio
async def test_the_default_budget_is_every_capped_story_times_its_attempts():
    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8])
        runner = ScriptRunner({"A": [_INVENTED], "B": [_INVENTED]})
        summary = await _run(pool, league_id, library, runner,
                             media_ai_max_stories_per_cycle=2, media_ai_max_attempts=3)
        assert summary.call_budget == 6 and summary.calls == 6
        assert not summary.budget_exhausted

    await _with_league(body)


@pytest.mark.parametrize("feedback", [True, False], ids=["feedback", "plain"])
@pytest.mark.asyncio
async def test_a_retry_resends_the_same_payload_and_argv_plus_the_note_on_stdin(
    monkeypatch, feedback
):
    """The retry's argv is byte-identical (the note never reaches argv, where
    `claude.cmd` would hand model-written text to cmd.exe); its stdin is the
    first call's payload, byte for byte, then the note (feedback) or nothing
    (plain)."""
    from dba2.domain.media_prompt import build_retry_note

    monkeypatch.setattr(media_service, "_RETRY_WITH_FEEDBACK", feedback)

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9])
        runner = ScriptRunner({"A": [_INVENTED, _WRONG_NUMBER, _GOOD]})
        summary = await _run(pool, league_id, library, runner)
        (first, second, third) = runner.calls_for("A")
        assert first[1] == second[1] == third[1]
        json.loads(first[2])  # the first call is the bare payload
        if feedback:
            note2 = build_retry_note("proper_nouns", ("Tyrese Holloway",))
            assert second[2] == first[2] + b"\n\n" + note2.encode("utf-8")
            (log,) = summary.stories
            note3 = build_retry_note("numbers_bound", log.attempts[1].offending)
            assert third[2] == first[2] + b"\n\n" + note3.encode("utf-8")
        else:
            assert first[2] == second[2] == third[2]

    await _with_league(body)


@pytest.mark.asyncio
async def test_determinism_holds_when_stories_land_on_a_retry():
    """S8-15 item 4 with the retry on: stories accepted on their second or
    third call still change exactly three columns, and nothing else."""

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7])
        rows_before = await _rows(pool, league_id)
        sums_before = await _league_table_checksums(pool, league_id)
        runner = ScriptRunner({
            "A": [_INVENTED, _GOOD], "B": [_WRONG_NUMBER, _INVENTED, _GOOD], "C": [_INVENTED],
        })
        summary = await _run(pool, league_id, library, runner)
        assert summary.accepted == 2 and summary.accepted_on_retry == 2
        rows_after = await _rows(pool, league_id)
        assert rows_after.keys() == rows_before.keys()
        for story_id, before in rows_before.items():
            after = rows_after[story_id]
            assert {k: v for k, v in before.items() if k not in _TEXT_COLUMNS} == {
                k: v for k, v in after.items() if k not in _TEXT_COLUMNS
            }
        assert await _league_table_checksums(pool, league_id) == sums_before

    await _with_league(body)


class _HeldRunner(ScriptRunner):
    """Holds team A's first call until team D has made its last call."""

    def __init__(self, script):
        super().__init__(script)
        self.release = None

    async def __call__(self, argv, stdin, cwd, env, timeout):
        import asyncio

        if self.release is None:
            self.release = asyncio.Event()
        code = _team_of(stdin).split()[0]
        if code == "A" and not self.calls_for("A"):
            self.calls.append((code, list(argv), stdin))
            await self.release.wait()
            return 0, _envelope("Big move", "A City A Team added Tyrese Holloway today."), b""
        result = await super().__call__(argv, stdin, cwd, env, timeout)
        if code == "D" and len(self.calls_for("D")) == 3:
            self.release.set()
        return result


@pytest.mark.asyncio
async def test_a_breaker_trip_stops_the_retries_of_a_story_already_in_flight():
    """Concurrency 2: A's first call is still out while B, C and D each
    exhaust their attempts and trip the breaker. A's rejected draft then gets
    no retry."""

    async def body(pool, league_id):
        library, _ = await _seed_cycle(pool, league_id, [0.9, 0.8, 0.7, 0.6])
        runner = _HeldRunner({"B": [_INVENTED], "C": [_INVENTED], "D": [_INVENTED]})
        summary = await _run(pool, league_id, library, runner, media_ai_concurrency=2)
        assert summary.aborted
        assert len(runner.calls_for("A")) == 1
        assert summary.calls == 10 and summary.failures == {"gate_failed": 4}

    await _with_league(body)
