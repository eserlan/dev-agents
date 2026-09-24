"""Provider execution with bounded logs and observable heartbeats."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path

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
    reasoning_effort: str = "medium",
) -> list[str]:
    """Build the command line for a configured provider."""
    if provider == "codex":
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            CODEX_MODEL,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            prompt,
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
            "--model",
            "muse-spark-1.2",
            "--yolo",
            prompt,
        ]
    return [provider, "-p", prompt]


def run_agent(
    provider: str,
    prompt: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    heartbeat_seconds: float = 30.0,
    reasoning_effort: str = "medium",
    on_progress: Callable[[int], None] | None = None,
    progress_interval_seconds: float = 600.0,
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
        process = subprocess.Popen(
            provider_command(provider, prompt, timeout_seconds, reasoning_effort),
            cwd=cwd,
            env=_agent_environment(),
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        start = time.monotonic()
        deadline = start + timeout_seconds
        last_progress_at = start
        timed_out = False
        while process.poll() is None:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                process.kill()
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
        returncode = process.wait()
        stream.write(f"=== agent finished exit={returncode} timed_out={timed_out} ===\n")
    sweep_agent_tmp()
    return AgentResult(returncode=returncode, timed_out=timed_out)
