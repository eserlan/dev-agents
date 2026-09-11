"""Provider execution with bounded logs and observable heartbeats."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentResult:
    """Outcome of one provider invocation."""

    returncode: int | None
    timed_out: bool


def provider_command(provider: str, prompt: str) -> list[str]:
    """Build the command line for a configured provider."""
    if provider == "codex":
        return ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", prompt]
    return [provider, "-p", prompt]


def run_agent(
    provider: str,
    prompt: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    heartbeat_seconds: float = 30.0,
) -> AgentResult:
    """Run a provider, forwarding output to a durable log and emitting heartbeats."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n=== agent started provider={provider} ===\n")
        stream.flush()
        process = subprocess.Popen(
            provider_command(provider, prompt),
            cwd=cwd,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while process.poll() is None:
            remaining = deadline - time.monotonic()
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
        returncode = process.wait()
        stream.write(f"=== agent finished exit={returncode} timed_out={timed_out} ===\n")
    return AgentResult(returncode=returncode, timed_out=timed_out)
