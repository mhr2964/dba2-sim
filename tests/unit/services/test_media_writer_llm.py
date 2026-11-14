"""`dba2/services/media_writer_llm.py` -- `AnthropicWriter`
(docs/design/media-v2-step8-anthropic-writer.md S8-15 items 1-2).

Every test injects a fake runner; NO real `claude` process is ever spawned
here except the opt-in contract smoke test at the bottom, skipped unless
`DBA2_TEST_CLAUDE_CLI=1`. The real-spawn tests that exist below run
`sys.executable`, not `claude`."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

import pytest

from dba2.domain.media_writer import FormatTier
from dba2.services import media_writer_llm as m
from dba2.services.media_writer_llm import AnthropicWriter, AnthropicWriterError
from tests.unit._media_ai_fixtures import make_brief, make_package, make_persona


def _limits(tier, package):
    return (80, 350) if tier is FormatTier.BLURB else (120, 1400)


def _ok_envelope(headline="Webb heads to Boston", body="Marcus Webb moves in a 3 asset deal."):
    return {
        "type": "result", "subtype": "success", "is_error": False, "result": "",
        "structured_output": {"headline": headline, "body": body},
        "total_cost_usd": 0.0123, "duration_ms": 4300,
        "usage": {"input_tokens": 2, "output_tokens": 90, "cache_read_input_tokens": 0},
    }


class FakeRunner:
    def __init__(self, *, returncode=0, stdout=None, stderr=b"", exc=None):
        self.returncode = returncode
        self.stdout = json.dumps(_ok_envelope()).encode() if stdout is None else stdout
        self.stderr = stderr
        self.exc = exc
        self.calls: list[dict] = []

    async def __call__(self, argv, stdin, cwd, env, timeout):
        self.calls.append(
            {"argv": argv, "stdin": stdin, "cwd": cwd, "env": env, "timeout": timeout}
        )
        if self.exc is not None:
            raise self.exc
        return self.returncode, self.stdout, self.stderr


def _writer(runner, **kw):
    return AnthropicWriter(
        model="sonnet", limits_for=_limits, runner=runner, cli_path="claude", **kw
    )


async def _attempt(writer, brief=None, tier=FormatTier.BLURB):
    return await writer.attempt(make_package(), persona=make_persona(), tier=tier, brief=brief)


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


# -- the fake-runner matrix ------------------------------------------------


async def test_success_returns_written_story_and_telemetry():
    with _writer(FakeRunner()) as w:
        out = await _attempt(w)
    assert out.ok and out.category is None
    assert out.written.headline == "Webb heads to Boston"
    assert out.written.backend == "anthropic:sonnet"
    assert out.written.fact_box == ()
    t = out.telemetry
    assert (t.total_cost_usd, t.input_tokens, t.output_tokens, t.duration_ms) == (
        0.0123, 2, 90, 4300,
    )


@pytest.mark.parametrize(
    "envelope",
    [
        {**_ok_envelope(), "structured_output": None},
        {**_ok_envelope(), "structured_output": {"headline": "h"}},
        {**_ok_envelope(), "structured_output": {"headline": "  ", "body": "b"}},
        {**_ok_envelope(), "structured_output": {"headline": "h", "body": 7}},
    ],
)
async def test_structured_output_missing_or_malformed_is_bad_envelope(envelope):
    with _writer(FakeRunner(stdout=json.dumps(envelope).encode())) as w:
        out = await _attempt(w)
    assert not out.ok and out.category == m.CATEGORY_BAD_ENVELOPE


async def test_is_error_true_with_usage_limit_subtype_is_cli_error_even_on_exit_zero():
    env = {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "You have hit your session limit",
    }
    with _writer(FakeRunner(returncode=0, stdout=json.dumps(env).encode())) as w:
        out = await _attempt(w)
    assert out.category == m.CATEGORY_CLI_ERROR
    assert out.cli_subtype == "error_during_execution"
    assert "session limit" in out.detail


async def test_is_error_with_nonzero_exit_is_still_cli_error_carrying_the_subtype():
    env = {"subtype": "error_max_turns", "is_error": True, "result": "hit max turns"}
    with _writer(FakeRunner(returncode=1, stdout=json.dumps(env).encode())) as w:
        out = await _attempt(w)
    assert out.category == m.CATEGORY_CLI_ERROR and out.cli_subtype == "error_max_turns"


async def test_nonzero_exit_with_a_plausible_success_stdout_is_nonzero_exit():
    with _writer(FakeRunner(returncode=2)) as w:
        out = await _attempt(w)
    assert not out.ok and out.category == m.CATEGORY_NONZERO_EXIT


async def test_subtype_not_success_is_rejected_even_with_no_is_error():
    env = {**_ok_envelope(), "subtype": "error_max_turns"}
    with _writer(FakeRunner(stdout=json.dumps(env).encode())) as w:
        out = await _attempt(w)
    assert out.category == m.CATEGORY_CLI_ERROR


@pytest.mark.parametrize("stdout", [b"not json at all", b"", b"[1, 2]", b'"str"'])
async def test_unparseable_or_non_object_stdout_is_bad_envelope(stdout):
    with _writer(FakeRunner(stdout=stdout)) as w:
        out = await _attempt(w)
    assert out.category == m.CATEGORY_BAD_ENVELOPE


async def test_runner_timeout_is_categorised_not_raised():
    with _writer(FakeRunner(exc=TimeoutError())) as w:
        out = await _attempt(w)
    assert out.category == m.CATEGORY_TIMEOUT


async def test_runner_file_not_found_is_spawn_failed_and_retried_once_per_writer():
    runner = FakeRunner(exc=FileNotFoundError("claude"))
    with _writer(runner) as w:
        first = await _attempt(w)
        assert first.category == m.CATEGORY_SPAWN_FAILED
        assert len(runner.calls) == 2  # original + the one retry
        second = await _attempt(w)
        assert second.category == m.CATEGORY_SPAWN_FAILED
        assert len(runner.calls) == 3  # retry budget spent: no second retry


async def test_spawn_failure_then_success_on_the_retry_succeeds():
    calls = {"n": 0}

    async def flaky(argv, stdin, cwd, env, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return 0, json.dumps(_ok_envelope()).encode(), b""

    with _writer(flaky) as w:
        out = await _attempt(w)
    assert out.ok and calls["n"] == 2


async def test_timeout_and_cli_error_are_never_retried():
    runner = FakeRunner(exc=TimeoutError())
    with _writer(runner) as w:
        await _attempt(w)
    assert len(runner.calls) == 1
    err = FakeRunner(stdout=json.dumps({"is_error": True, "subtype": "x"}).encode())
    with _writer(err) as w:
        await _attempt(w)
    assert len(err.calls) == 1


async def test_no_cli_on_path_is_spawn_failed_without_calling_any_runner(monkeypatch):
    monkeypatch.setattr(m, "resolve_cli_path", lambda configured=None: None)
    w = AnthropicWriter(model="sonnet", limits_for=_limits)  # real runner, no cli_path
    try:
        out = await _attempt(w)
    finally:
        w.close()
    assert out.category == m.CATEGORY_SPAWN_FAILED and "not found" in out.detail


async def test_write_protocol_form_raises_a_typed_error_on_failure_and_returns_on_success():
    with _writer(FakeRunner()) as w:
        story = await w.write(
            make_package(), persona=make_persona(), tier=FormatTier.BLURB, rng=random.Random(1),
        )
    assert story.backend == "anthropic:sonnet"
    with _writer(FakeRunner(returncode=3)) as w, pytest.raises(AnthropicWriterError) as info:
        await w.write(
            make_package(), persona=make_persona(), tier=FormatTier.BLURB, rng=random.Random(1),
        )
    assert info.value.category == m.CATEGORY_NONZERO_EXIT


# -- argv / env / stdin / sandbox -----------------------------------------


async def test_argv_carries_every_flag_of_the_s8_7_contract():
    runner = FakeRunner()
    with _writer(runner) as w:
        await _attempt(w)
    argv = runner.calls[0]["argv"]
    assert argv[:3] == ["claude", "-p", "Write today's item from the JSON on standard input."]
    assert _flag_value(argv, "--output-format") == "json"
    assert _flag_value(argv, "--model") == "sonnet"  # ALWAYS explicit
    assert _flag_value(argv, "--effort") == "low"
    assert _flag_value(argv, "--max-turns") == "2"
    assert _flag_value(argv, "--tools") == ""
    assert _flag_value(argv, "--permission-mode") == "dontAsk"
    assert _flag_value(argv, "--permission-prompts") == "none"
    assert json.loads(_flag_value(argv, "--json-schema"))["required"] == ["headline", "body"]
    for flag in ("--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert Path(_flag_value(argv, "--system-prompt-file")).name.startswith("persona-")


async def test_permission_prompts_flag_is_dropped_for_an_old_cli():
    runner = FakeRunner()
    with _writer(runner, permission_prompts_flag=False) as w:
        await _attempt(w)
    assert "--permission-prompts" not in runner.calls[0]["argv"]


async def test_payload_goes_on_stdin_not_argv_and_has_an_explicit_null_brief():
    runner = FakeRunner()
    with _writer(runner) as w:
        await _attempt(w)
        await _attempt(w, brief=make_brief())
    no_brief, with_brief = (json.loads(c["stdin"]) for c in runner.calls)
    assert no_brief["league_brief"] is None
    assert with_brief["league_brief"]["standings"]
    assert "Marcus Webb" not in " ".join(runner.calls[0]["argv"])  # league data never in argv


async def test_a_retry_note_goes_after_the_unchanged_payload_on_stdin_never_in_argv():
    """G2-18: the note holds model-written text (a rejected name, a quoted
    span), and argv reaches `claude.cmd` through cmd.exe, so the note must
    never change argv; the payload bytes before it are the first call's."""
    runner = FakeRunner()
    note = 'Your previous draft ... - "a & b | %PATH%": a quotation\n'
    with _writer(runner) as w:
        await _attempt(w, brief=make_brief())
        out = await w.attempt(
            make_package(), persona=make_persona(), tier=FormatTier.BLURB, brief=make_brief(),
            retry_note=note,
        )
    assert out.ok
    first, retry = runner.calls
    assert retry["argv"] == first["argv"]
    assert retry["stdin"] == first["stdin"] + b"\n\n" + note.encode("utf-8")
    assert "%PATH%" not in " ".join(retry["argv"])


async def test_system_prompt_file_is_built_once_per_persona_and_omits_tangent_without_brief():
    runner = FakeRunner()
    with _writer(runner) as w:
        await _attempt(w)
        await _attempt(w)
        files = {_flag_value(c["argv"], "--system-prompt-file") for c in runner.calls}
        assert len(files) == 1
        assert "forced link" not in Path(next(iter(files))).read_text(encoding="utf-8")
        await _attempt(w, brief=make_brief())
        with_brief = _flag_value(runner.calls[2]["argv"], "--system-prompt-file")
        assert with_brief not in files
        assert "forced link" in Path(with_brief).read_text(encoding="utf-8")


async def test_sandbox_cwd_is_a_fresh_temp_dir_outside_the_repo_and_removed_on_close():
    runner = FakeRunner()
    w = _writer(runner)
    await _attempt(w)
    cwd = Path(runner.calls[0]["cwd"])
    assert cwd.is_dir() and cwd.name.startswith("dba2-newsroom-")
    assert not list(cwd.glob("CLAUDE.md")) and not (cwd / ".claude").exists()
    repo = Path(__file__).resolve().parents[3]
    assert repo not in cwd.parents
    w.close()
    assert not cwd.exists()
    w.close()  # idempotent


def test_child_env_scrubs_api_key_and_provider_overrides():
    parent = {
        "ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin", "HOME": "/h",
    }
    child = m._child_env(parent)
    for key in m._SCRUBBED_ENV_VARS:
        assert key not in child
    assert child["PATH"] == "/bin" and child["HOME"] == "/h"
    assert child["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "CLAUDE_CONFIG_DIR" not in child
    assert m._child_env(parent, config_dir="/iso")["CLAUDE_CONFIG_DIR"] == "/iso"
    assert "ANTHROPIC_API_KEY" in parent  # input untouched


async def test_the_env_actually_handed_to_the_runner_has_no_api_key():
    runner = FakeRunner()
    with _writer(runner, environ={"ANTHROPIC_API_KEY": "sk-secret", "PATH": "p"}) as w:
        await _attempt(w)
    assert "ANTHROPIC_API_KEY" not in runner.calls[0]["env"]


async def test_the_configured_timeout_reaches_the_runner():
    runner = FakeRunner()
    with _writer(runner, timeout_s=12.5) as w:
        await _attempt(w)
    assert runner.calls[0]["timeout"] == 12.5


def test_resolve_cli_path_prefers_configured_and_uses_cmd_on_windows(monkeypatch):
    assert m.resolve_cli_path("/x/claude") == "/x/claude"
    seen = []
    monkeypatch.setattr(m.shutil, "which", lambda name: seen.append(name) or f"/found/{name}")
    monkeypatch.setattr(m.sys, "platform", "win32")
    assert m.resolve_cli_path() == "/found/claude.cmd"
    monkeypatch.setattr(m.sys, "platform", "linux")
    assert m.resolve_cli_path() == "/found/claude"
    assert seen == ["claude.cmd", "claude"]


# -- the REAL spawn runner, exercised with python (not claude) --------------


async def test_real_runner_round_trips_stdin_and_stdout(tmp_path):
    code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    rc, out, _ = await m._spawn_runner(
        [sys.executable, "-c", code], b"hello", str(tmp_path), dict(os.environ), 20.0
    )
    assert (rc, out) == (0, b"HELLO")


async def test_real_runner_raises_oserror_for_a_missing_executable(tmp_path):
    with pytest.raises(OSError):
        await m._spawn_runner(
            [str(tmp_path / "definitely-not-a-binary")], b"", str(tmp_path),
            dict(os.environ), 5.0,
        )


async def test_real_runner_times_out_and_kills_the_process(tmp_path):
    marker = tmp_path / "alive.txt"
    code = (
        "import time, pathlib\n"
        f"p = pathlib.Path({str(marker)!r})\n"
        "for i in range(600):\n"
        "    p.write_text(str(i)); time.sleep(0.1)\n"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await m._spawn_runner(
            [sys.executable, "-c", code], b"", str(tmp_path), dict(os.environ), 1.0
        )
    assert time.monotonic() - started < 15
    await asyncio.sleep(0.5)
    before = marker.read_text()
    await asyncio.sleep(0.6)
    assert marker.read_text() == before  # the writer loop is dead: process really killed


# -- opt-in real-process contract smoke test ------------------------------


@pytest.mark.skipif(
    os.environ.get("DBA2_TEST_CLAUDE_CLI") != "1",
    reason="opt-in: spawns the REAL claude CLI (spends subscription usage); "
    "set DBA2_TEST_CLAUDE_CLI=1",
)
async def test_real_cli_envelope_contract_shape_only():
    """Asserts the envelope's SHAPE only, never the text. Its whole job is to
    catch a CLI upgrade changing the output contract, which no fake can."""
    model = os.environ.get("MEDIA_AI_MODEL", "sonnet")
    with AnthropicWriter(model=model, limits_for=_limits) as w:
        out = await _attempt(w)
    assert out.category != m.CATEGORY_SPAWN_FAILED, out.detail
    assert out.ok, (out.category, out.detail)
    t = out.telemetry
    assert isinstance(t.total_cost_usd, float) and isinstance(t.input_tokens, int)
    assert isinstance(t.output_tokens, int) and isinstance(t.duration_ms, int)
    assert isinstance(out.written.headline, str) and isinstance(out.written.body, str)
