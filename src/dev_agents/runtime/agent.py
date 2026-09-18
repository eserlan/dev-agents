"""Provider execution with bounded logs and observable heartbeats."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path


@dataclass(frozen=True)
class AgentResult:
    """Outcome of one provider invocation."""

    returncode: int | None
    timed_out: bool


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
            "gpt-5.6-luna",
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
    return AgentResult(returncode=returncode, timed_out=timed_out)
