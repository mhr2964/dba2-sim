"""`AnthropicWriter` -- the media newsroom's AI writer, run through the
commissioner's own `claude` CLI subscription
(docs/design/media-v2-step8-anthropic-writer.md S8-7, S8-8, S8-9).

**Why a subprocess and not the API.** The commissioner's decision is
subscription auth, and `--bare` (the flag the docs recommend for scripted
use) never reads OAuth credentials, so every call is a normal `claude -p`
run. That makes three things load-bearing, each learned from the S8-0 spike
(six real calls) and not from the docs:

- On Windows the resolved `claude` on PATH is an extensionless POSIX shim
  that `CreateProcess` rejects (`WinError 193`); `claude.cmd` works. So the
  executable is resolved as `shutil.which("claude.cmd")`, never bare
  `claude`.
- Token cost only lands in the assumed ~1-3K range with the FULL argv's
  tool-suppression flags plus a sandbox cwd; `--model` is always passed
  explicitly because a config-less run silently falls back to an expensive
  default model.
- A stray `ANTHROPIC_API_KEY` in the environment makes `-p` bill the API
  instead of the subscription with no visible symptom, so `_child_env`
  scrubs it (and the other provider overrides) from the child.

**Never trust the exit code** (S8-8): a run can fail inside (missing auth,
exhausted usage window) and still exit 0 with the failure printed as the
`result`. `attempt` accepts a result only when ALL of: returncode 0, stdout
is a JSON object, `is_error` falsy with `subtype == "success"`, and
`structured_output` holds non-empty string `headline`/`body`.

**This module never gates, clamps or styles.** Gate policy belongs to the
caller (`media_service`'s rewrite pass, S8-10) and the budget/clamp helpers
live under `dba2.media`, which only `media_service.py` may import (P4-31).
`attempt` returns the model's raw stripped text; the pass clamps, then gates
the clamped text it will actually store. Consequently `WrittenStory.
fact_box` is always `()` here: the fact box is a pure function of the
persisted `fact_package` and is rebuilt at render time, so an AI story still
shows the same three verbatim facts (S8-6).

`attempt` never raises for a runner or CLI failure: it returns a
`WriterOutcome` carrying a category (`spawn_failed`, `timeout`,
`nonzero_exit`, `bad_envelope`, `cli_error`) that drives the circuit
breaker. `write` is the `WriterBackend` protocol form and raises
`AnthropicWriterError` instead, for a future in-cycle caller.

Impure by design (subprocess, temp files, env) -- which is why it is the one
step-8 module under `services/` rather than `media/`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage
from dba2.domain.media_prompt import (
    WRITE_INSTRUCTION,
    build_payload,
    build_persona_card,
    response_schema_json,
    serialize_payload,
    with_retry_note,
)
from dba2.domain.media_writer import FormatTier, PersonaVoice, WrittenStory

logger = logging.getLogger(__name__)

Runner = Callable[
    [list[str], bytes, str, dict[str, str], float], Awaitable[tuple[int, bytes, bytes]]
]
"""`(argv, stdin, cwd, env, timeout_s) -> (returncode, stdout, stderr)`. The
explicit seam tests inject instead of monkeypatching `asyncio.
create_subprocess_exec`, so the writer's real parsing and policy run under
test. A runner signals a spawn failure by raising `OSError` (e.g.
`FileNotFoundError`) and a timeout by raising `TimeoutError`."""

LimitsFor = Callable[[FormatTier, StoryPackage], tuple[int, int]]
"""`(tier, package) -> (title_max, description_max)`. Injected by
`media_service`, which owns `budget_for` (this module may not import
`dba2.media`)."""

CATEGORY_SPAWN_FAILED = "spawn_failed"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_NONZERO_EXIT = "nonzero_exit"
CATEGORY_BAD_ENVELOPE = "bad_envelope"
CATEGORY_CLI_ERROR = "cli_error"
CATEGORY_GATE_FAILED = "gate_failed"
"""S8-8's categories. `gate_failed` is never produced here -- the pass owns
the gates -- but lives beside the others so the breaker has one vocabulary."""

_SCRUBBED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


class AnthropicWriterError(Exception):
    """Raised by `AnthropicWriter.write` (the protocol form) on any failure;
    `attempt` returns the same information as a `WriterOutcome` instead."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class Telemetry:
    """Client-side estimates the CLI reports (the docs say so twice); on a
    subscription they are not a bill, but they are the free per-call proxy
    for S8-16 item 4's measured numbers."""

    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class WriterOutcome:
    """One `attempt`'s result. Exactly one of `written` / `category` is set.
    `cli_subtype` carries the CLI's own `subtype` on a `cli_error`, which is
    where a usage-limit or auth message will show up."""

    written: WrittenStory | None = None
    category: str | None = None
    detail: str = ""
    cli_subtype: str | None = None
    telemetry: Telemetry | None = None

    @property
    def ok(self) -> bool:
        return self.written is not None


def _child_env(environ: Mapping[str, str], *, config_dir: str | None = None) -> dict[str, str]:
    """`environ` minus every provider override, plus the traffic switch (S8-7).
    Pure, so the single most expensive silent failure (an API key silently
    converting a subscription feature into a billed one) is unit-testable
    without spawning anything. `config_dir` optionally points
    `CLAUDE_CONFIG_DIR` at an isolated config directory (S8-0 item 3 found
    this works and saves ~5K tokens/call); unset by default because the
    isolated directory needs a copied OAuth credentials file, whose refresh
    behavior against the commissioner's real login is not yet established."""
    env = {k: v for k, v in environ.items() if k not in _SCRUBBED_ENV_VARS}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def resolve_cli_path(configured: str | None = None) -> str | None:
    """`MEDIA_AI_CLI_PATH` if set, else `claude.cmd` on Windows (S8-0 item 1:
    never bare `claude`, which is a shim `CreateProcess` rejects) and
    `claude` elsewhere. `None` when nothing resolves -- the caller disables
    the pass with one warning; it is never an exception."""
    if configured:
        return configured
    return shutil.which("claude.cmd" if sys.platform == "win32" else "claude")


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and its children. `claude` is a Node process behind a
    shim; `TerminateProcess` on the parent alone leaks the rest, and one
    leaked Node process per timeout over a season is a real failure mode."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            logger.warning("taskkill failed for pid %s; falling back to proc.kill()", proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _spawn_runner(
    argv: list[str], stdin: bytes, cwd: str, env: dict[str, str], timeout_s: float
) -> tuple[int, bytes, bytes]:
    """The real runner. Raises `OSError` if the process cannot start and
    `TimeoutError` (after killing the tree) if it overruns; kills the tree on
    cancellation too so an aborted `/advance` never strands a Node process."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout_s)
    except BaseException:
        await _kill_tree(proc)
        raise
    return proc.returncode if proc.returncode is not None else -1, stdout, stderr


def _as_bytes(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else value


def _nonempty_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _telemetry(envelope: Mapping[str, object]) -> Telemetry:
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def _int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    cost = envelope.get("total_cost_usd")
    return Telemetry(
        total_cost_usd=float(cost) if isinstance(cost, int | float) else None,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        duration_ms=_int(envelope.get("duration_ms")),
    )


def parse_envelope(returncode: int, stdout: bytes) -> tuple[str, str, Telemetry] | WriterOutcome:
    """S8-8's four-part success predicate. Returns `(headline, body,
    telemetry)` on success, otherwise a failed `WriterOutcome`. Pure."""
    text = stdout.decode("utf-8", errors="replace").strip()
    envelope: object = None
    if text:
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = None
    if isinstance(envelope, dict) and envelope.get("is_error"):
        # Checked before the exit code: a usage-limit or auth failure may exit
        # non-zero OR zero, and the CLI's own subtype/result is the useful part.
        result = str(envelope.get("result", ""))[:200]
        return WriterOutcome(
            category=CATEGORY_CLI_ERROR,
            detail=f"exit={returncode} subtype={envelope.get('subtype')} result={result!r}",
            cli_subtype=str(envelope.get("subtype")),
        )
    if returncode != 0:
        return WriterOutcome(
            category=CATEGORY_NONZERO_EXIT,
            detail=f"exit={returncode} stdout[:200]={text[:200]!r}",
        )
    if not isinstance(envelope, dict):
        return WriterOutcome(
            category=CATEGORY_BAD_ENVELOPE, detail=f"stdout is not a JSON object: {text[:200]!r}"
        )
    subtype = envelope.get("subtype")
    if subtype != "success":
        return WriterOutcome(
            category=CATEGORY_CLI_ERROR,
            detail=f"subtype={subtype!r} result={str(envelope.get('result', ''))[:200]!r}",
            cli_subtype=str(subtype),
        )
    structured = envelope.get("structured_output")
    headline = _nonempty_str(structured.get("headline")) if isinstance(structured, dict) else None
    body = _nonempty_str(structured.get("body")) if isinstance(structured, dict) else None
    if headline is None or body is None:
        return WriterOutcome(
            category=CATEGORY_BAD_ENVELOPE,
            detail="structured_output missing or lacks non-empty string headline/body",
            telemetry=_telemetry(envelope),
        )
    return headline, body, _telemetry(envelope)


class AnthropicWriter:
    """One instance per rewrite pass: holds the resolved executable, the
    sandbox cwd (`mkdtemp` -- no `CLAUDE.md`, no `.claude/`, no parent inside
    the AI workspace tree) and the per-persona system-prompt files, so
    `which` and `mkdtemp` run once per pass, not once per story. Call
    `close()` (or use `with`) to remove the sandbox."""

    def __init__(
        self,
        *,
        model: str,
        effort: str = "low",
        timeout_s: float = 90.0,
        limits_for: LimitsFor,
        cli_path: str | None = None,
        runner: Runner | None = None,
        config_dir: str | None = None,
        permission_prompts_flag: bool = True,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.name = f"anthropic:{model}"
        self._model = model
        self._effort = effort
        self._timeout_s = timeout_s
        self._limits_for = limits_for
        self._runner: Runner = runner if runner is not None else _spawn_runner
        self._injected_runner = runner is not None
        self._cli_path = cli_path
        self._config_dir = config_dir
        self._permission_prompts_flag = permission_prompts_flag
        self._environ = environ
        self._sandbox: Path | None = None
        self._prompt_files: dict[tuple[str, FormatTier, bool, int, int], Path] = {}
        self._spawn_retry_used = False

    def __enter__(self) -> AnthropicWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove the sandbox and every prompt file in it. Idempotent."""
        if self._sandbox is not None:
            shutil.rmtree(self._sandbox, ignore_errors=True)
            self._sandbox = None
            self._prompt_files.clear()

    def _sandbox_dir(self) -> Path:
        if self._sandbox is None:
            self._sandbox = Path(tempfile.mkdtemp(prefix="dba2-newsroom-"))
        return self._sandbox

    def _executable(self) -> str | None:
        """Configured path, else resolved once. An injected runner needs no
        real CLI, so a placeholder keeps the fake-runner path independent of
        what is installed."""
        if self._cli_path is None:
            self._cli_path = resolve_cli_path()
        if self._cli_path is None and self._injected_runner:
            return "claude"
        return self._cli_path

    def _prompt_file(
        self, persona: PersonaVoice, tier: FormatTier, has_brief: bool,
        title_max: int, description_max: int,
    ) -> Path:
        key = (persona.key, tier, has_brief, title_max, description_max)
        path = self._prompt_files.get(key)
        if path is None:
            card = build_persona_card(
                persona, tier, title_max=title_max, description_max=description_max,
                has_brief=has_brief,
            )
            path = self._sandbox_dir() / f"persona-{len(self._prompt_files)}.txt"
            path.write_text(card, encoding="utf-8", newline="\n")
            self._prompt_files[key] = path
        return path

    def _argv(self, executable: str, prompt_file: Path) -> list[str]:
        """S8-7's exact argv. `--permission-prompts none` is dropped when the
        installed CLI predates it (v2.1.259)."""
        argv = [
            executable, "-p", WRITE_INSTRUCTION,
            "--output-format", "json",
            "--json-schema", response_schema_json(),
            "--system-prompt-file", str(prompt_file),
            "--model", self._model,
            "--effort", self._effort,
            "--max-turns", "2",
            "--tools", "",
            "--disable-slash-commands",
            "--permission-mode", "dontAsk",
        ]
        if self._permission_prompts_flag:
            argv += ["--permission-prompts", "none"]
        argv.append("--no-session-persistence")
        return argv

    async def _run_once(
        self, argv: list[str], stdin: bytes, env: dict[str, str]
    ) -> tuple[int, bytes, bytes] | WriterOutcome:
        try:
            returncode, stdout, stderr = await self._runner(
                argv, stdin, str(self._sandbox_dir()), env, self._timeout_s
            )
        except TimeoutError:
            return WriterOutcome(
                category=CATEGORY_TIMEOUT, detail=f"no result within {self._timeout_s}s"
            )
        except OSError as exc:
            return WriterOutcome(
                category=CATEGORY_SPAWN_FAILED, detail=f"{type(exc).__name__}: {exc}"
            )
        return returncode, _as_bytes(stdout), _as_bytes(stderr)

    async def attempt(
        self,
        package: StoryPackage,
        *,
        persona: PersonaVoice,
        tier: FormatTier,
        brief: LeagueBrief | None = None,
        retry_note: str | None = None,
    ) -> WriterOutcome:
        """One CLI call for one story. Never raises for a runner or CLI
        failure. At most ONE `spawn_failed` retry per writer instance (S8-9:
        the one category where an immediate second attempt is cheap and
        plausibly different). `retry_note` (the pass's gate retry, G2-18) is
        appended to stdin after the unchanged payload; argv never changes,
        because the note carries model-written text and argv reaches
        `claude.cmd` through cmd.exe."""
        executable = self._executable()
        if executable is None:
            return WriterOutcome(
                category=CATEGORY_SPAWN_FAILED, detail="claude CLI not found on PATH"
            )
        title_max, description_max = self._limits_for(tier, package)
        prompt_file = self._prompt_file(
            persona, tier, brief is not None, title_max, description_max
        )
        argv = self._argv(executable, prompt_file)
        stdin = with_retry_note(
            serialize_payload(
                build_payload(
                    package, brief, tier, title_max=title_max, description_max=description_max
                )
            ),
            retry_note,
        ).encode("utf-8")
        env = _child_env(self._environ if self._environ is not None else os.environ,
                         config_dir=self._config_dir)

        result = await self._run_once(argv, stdin, env)
        if (
            isinstance(result, WriterOutcome)
            and result.category == CATEGORY_SPAWN_FAILED
            and not self._spawn_retry_used
        ):
            self._spawn_retry_used = True
            logger.warning("claude spawn failed (%s); retrying once", result.detail)
            result = await self._run_once(argv, stdin, env)
        if isinstance(result, WriterOutcome):
            return result

        returncode, stdout, stderr = result
        parsed = parse_envelope(returncode, stdout)
        if isinstance(parsed, WriterOutcome):
            if stderr:
                logger.debug("claude stderr: %s", stderr.decode("utf-8", errors="replace")[:500])
            return parsed
        headline, body, telemetry = parsed
        written = WrittenStory(headline=headline, body=body, fact_box=(), backend=self.name)
        return WriterOutcome(written=written, telemetry=telemetry)

    async def write(
        self,
        package: StoryPackage,
        *,
        persona: PersonaVoice,
        tier: FormatTier,
        rng: random.Random,
        brief: LeagueBrief | None = None,
    ) -> WrittenStory:
        """`WriterBackend` protocol form. `rng` is accepted and unused (the
        model, not a seeded draw, chooses the words). Raises
        `AnthropicWriterError` on any failure; the rewrite pass uses
        `attempt` so it never needs a try/except."""
        outcome = await self.attempt(package, persona=persona, tier=tier, brief=brief)
        if outcome.written is None:
            raise AnthropicWriterError(outcome.category or "unknown", outcome.detail)
        return outcome.written
