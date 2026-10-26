"""Provider execution with bounded logs and observable heartbeats."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from threading import Event
from uuid import uuid4

from dev_agents.runtime.retention import remove_older_than

# Shared by PR review/fix and issue-fix queues, including GUI enhancements.
CODEX_MODEL = "gpt-6-luna"

# Agents run validation tooling (node, bunx, fallow, ...) that caches under $TMPDIR. On a
# typical Linux desktop /tmp is RAM-backed and often quota-limited, and those caches are
# never cleaned up, so they are pointed at a directory on disk that is swept after each run.
AGENT_TMP_RETENTION_SECONDS = 24 * 3600


@dataclass(frozen=True)
class AgentResult:
    """Outcome of one provider invocation."""

    returncode: int | None
    timed_out: bool


def agent_tmp_root() -> Path:
    """Return the scratch directory used as $TMPDIR for agent processes."""
    return Path.home() / ".cache/dev-agents/tmp"


def _agent_environment() -> dict[str, str]:
    """Return the environment for an agent process, with $TMPDIR moved onto disk."""
    environment = dict(os.environ)
    root = agent_tmp_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Scratch redirection is an optimisation; never stop an agent from running over it.
        return environment
    environment["TMPDIR"] = str(root)
    return environment


def sweep_agent_tmp() -> int:
    """Remove scratch entries untouched for a day; returns how many were removed.

    Anything younger is left alone so concurrent agents keep their working caches.
    """
    root = agent_tmp_root()
    removed = 0
    try:
        for pattern in ("*", ".[!.]*"):
            for directories in (True, False):
                removed += remove_older_than(
                    root, pattern, AGENT_TMP_RETENTION_SECONDS, directories=directories
                )
    except OSError:
        pass
    return removed


def run_with_fallback(
    providers: Iterable[str],
    run_one: Callable[[str], AgentResult],
    accept: Callable[[str, AgentResult], bool] | None = None,
) -> tuple[str, AgentResult] | None:
    """Run providers in order until a result satisfies the acceptance policy."""
    for provider in providers:
        result = run_one(provider)
        if accept is None:
            accepted = result.returncode == 0 and not result.timed_out
        else:
            accepted = accept(provider, result)
        if accepted:
            return provider, result
    return None


def provider_command(
    provider: str,
    prompt: str,
    timeout_seconds: float = 1200,
    reasoning_effort: str | None = None,
    model: str | None = None,
    read_only: bool = False,
    json_events: bool = False,
) -> list[str]:
    """Build the command line for a configured provider.

    ``reasoning_effort=None`` means the caller did not choose an effort:
    Codex falls back to medium, while Muse is left on its model's own
    default rather than silently inheriting the Codex-oriented default.
    """
    if provider == "codex":
        permissions = (
            ["--sandbox", "read-only", "--ignore-user-config", "-c", 'approval_policy="never"']
            if read_only else ["--dangerously-bypass-approvals-and-sandbox"]
        )
        return [
            "codex",
            "exec",
            *permissions,
            "-m",
            model or CODEX_MODEL,
            "-c",
            f'model_reasoning_effort="{reasoning_effort or "medium"}"',
            "-",
        ]
    if provider == "claude":
        return [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--model",
            "haiku",
            "--effort",
            "medium",
            prompt,
        ]
    if provider == "agy":
        timeout_minutes = max(1, ceil(timeout_seconds / 60))
        return [
            "agy",
            "--print",
            prompt,
            "--model=gemini-3.8-flash-low",
            "--effort=low",
            "--dangerously-skip-permissions",
            f"--print-timeout={timeout_minutes}m0s",
        ]
    if provider == "muse":
        return [
            "muse",
            "exec",
            *(["--json"] if json_events else []),
            "--model",
            model or "muse-spark-1.2",
            *(["--reasoning-effort", reasoning_effort] if reasoning_effort else []),
            "--yolo",
            prompt,
        ]
    return [provider, "-p", prompt]


class AgentCleanupError(RuntimeError):
    """The run may still contain live processes; retain its worktree."""


def _user_systemd_available() -> bool:
    """Probe without exposing the manager's environment in logs."""
    try:
        return subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _group_has_live_processes(pgid: int) -> bool:
    """Ignore dead children awaiting reaping by their new parent on Linux."""
    if Path("/proc/self/stat").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_process_group(process: subprocess.Popen[str], grace_seconds: float = 5) -> None:
    """Wait for the group, not just its leader, before escalating survivors."""
    for stop_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            process.poll()
            if not _group_has_live_processes(process.pid):
                process.wait()
                return
            time.sleep(0.05)
    if _group_has_live_processes(process.pid):
        raise AgentCleanupError(f"Agent process group {process.pid} did not stop")
    process.wait()


def _stop_scope(unit: str) -> None:
    # stop waits for the cgroup, including descendants in separate sessions.
    result = subprocess.run(
        ["systemctl", "--user", "stop", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15, check=False,
    )
    if result.returncode:
        state = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if state.stdout.strip() != "not-found":
            raise AgentCleanupError(f"Could not stop agent scope {unit}: {result.stderr.strip()}")



def run_agent(
    provider: str,
    prompt: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    heartbeat_seconds: float = 30.0,
    reasoning_effort: str | None = None,
    model: str | None = None,
    json_events: bool = False,
    last_message_path: Path | None = None,
    on_progress: Callable[[int], None] | None = None,
    progress_interval_seconds: float = 600.0,
    cancel_event: Event | None = None,
    read_only: bool = False,
    output_schema_path: Path | None = None,
) -> AgentResult:
    """Run a provider, forwarding output to a durable log and emitting heartbeats.

    `on_progress`, if given, fires roughly every `progress_interval_seconds`
    (elapsed seconds since start) -- much coarser than the log's own
    `heartbeat_seconds` liveness marker. It exists for callers that want to
    surface long-running work somewhere a human is actually watching (e.g. a
    PR comment), where a mark every 30s would be spam but silence for a
    30-40 minute review reads as hung.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n=== agent started provider={provider} ===\n")
        stream.flush()
        scope = f"dev-agents-run-{uuid4().hex}.scope" if _user_systemd_available() else None
        prompt_stream = None
        if provider == "codex":
            prompt_stream = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")  # noqa: SIM115 - closed immediately after spawn in finally
            prompt_stream.write(prompt)
            prompt_stream.seek(0)
        try:
            if json_events and provider != "muse":
                raise ValueError("json_events requires the Muse provider")
            command = provider_command(
                provider, prompt, timeout_seconds, reasoning_effort, model,
                **({"json_events": True} if json_events else {}),
            )
            if read_only:
                if provider != "codex":
                    raise ValueError("read_only auditing requires the Codex provider")
                command = provider_command(provider, prompt, timeout_seconds, reasoning_effort, model, read_only=True)
            if last_message_path is not None:
                if provider != "codex":
                    raise ValueError("last_message_path requires the Codex provider")
                last_message_path.parent.mkdir(parents=True, exist_ok=True)
                command.extend(["--output-last-message", str(last_message_path)])
            if output_schema_path is not None:
                if provider != "codex":
                    raise ValueError("output_schema_path requires the Codex provider")
                command.extend(["--output-schema", str(output_schema_path)])
            if scope is not None:
                command = [
                    "systemd-run", "--user", "--scope", "--quiet", "--collect",
                    f"--unit={scope}", "--property=TimeoutStopSec=5s", "--", *command,
                ]
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=_agent_environment(),
                stdin=prompt_stream,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        finally:
            if prompt_stream is not None:
                prompt_stream.close()
        start = time.monotonic()
        deadline = start + timeout_seconds
        last_progress_at = start
        timed_out = False
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    stream.write("=== agent cancelled: PR head advanced ===\n")
                    stream.flush()
                    break
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    timed_out = True
                    stream.write("=== agent timeout ===\n")
                    stream.flush()
                    break
                time.sleep(min(heartbeat_seconds, remaining))
                if process.poll() is None:
                    stream.write("=== agent heartbeat ===\n")
                    stream.flush()
                    now = time.monotonic()
                    if on_progress is not None and now - last_progress_at >= progress_interval_seconds:
                        last_progress_at = now
                        on_progress(int(now - start))
        finally:
            try:
                if scope is not None:
                    _stop_scope(scope)
                _stop_process_group(process)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AgentCleanupError("Could not confirm agent shutdown") from error
        returncode = process.wait()
        stream.write(f"=== agent finished exit={returncode} timed_out={timed_out} ===\n")
    sweep_agent_tmp()
    return AgentResult(returncode=returncode, timed_out=timed_out)
