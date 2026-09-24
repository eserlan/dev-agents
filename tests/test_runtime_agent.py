import os
import time
from pathlib import Path

import pytest

from dev_agents.runtime.agent import (
    CODEX_MODEL,
    AgentResult,
    _agent_environment,
    provider_command,
    run_agent,
    run_with_fallback,
)


def test_codex_provider_command_uses_non_interactive_exec() -> None:
    assert provider_command("codex", "fix it") == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        CODEX_MODEL,
        "-c",
        'model_reasoning_effort="medium"',
        "fix it",
    ]


def test_codex_provider_command_can_use_luna_high_reasoning() -> None:
    assert 'model_reasoning_effort="high"' in provider_command(
        "codex", "review it", reasoning_effort="high"
    )


def test_claude_provider_command_uses_haiku_medium() -> None:
    assert provider_command("claude", "fix it") == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--model",
        "haiku",
        "--effort",
        "medium",
        "fix it",
    ]


def test_generic_provider_command_uses_prompt_flag() -> None:
    assert provider_command("other-agent", "fix it") == ["other-agent", "-p", "fix it"]


def test_agy_provider_command_uses_bounded_print_mode() -> None:
    assert provider_command("agy", "fix it", 150) == [
        "agy",
        "--print",
        "fix it",
        "--model=gemini-3.8-flash-low",
        "--effort=low",
        "--dangerously-skip-permissions",
        "--print-timeout=3m0s",
    ]


def test_muse_provider_command_uses_spark() -> None:
    assert provider_command("muse", "fix it") == [
        "muse",
        "exec",
        "--model",
        "muse-spark-1.2",
        "--yolo",
        "fix it",
    ]


def test_run_with_fallback_tries_until_accepted() -> None:
    calls: list[str] = []

    def run(provider: str) -> AgentResult:
        calls.append(provider)
        return AgentResult(1 if provider == "codex" else 0, False)

    result = run_with_fallback(["codex", "claude"], run)
    assert result is not None
    assert result[0] == "claude"
    assert calls == ["codex", "claude"]


def test_run_agent_fires_on_progress_at_coarse_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """on_progress must fire well short of a full review's runtime, but not on
    every internal log heartbeat -- a mark every 30s would spam a PR comment,
    silence for 30-40 minutes reads as hung. Uses a real short-lived process
    (not a mock) so the polling loop itself is exercised, just compressed."""
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args, **_kwargs: ["sleep", "0.6"],
    )
    ticks: list[int] = []

    result = run_agent(
        "codex",
        "irrelevant",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        timeout_seconds=5,
        heartbeat_seconds=0.05,
        on_progress=ticks.append,
        progress_interval_seconds=0.2,
    )

    assert result.returncode == 0
    assert not result.timed_out
    # ~0.6s runtime / 0.2s interval: at least 2 ticks, not one per 0.05s heartbeat.
    assert 2 <= len(ticks) <= 4
    assert ticks == sorted(ticks)


def test_run_agent_points_tmpdir_at_the_scratch_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_tmp: Path
) -> None:
    """Agents' tooling caches under $TMPDIR; it must land on disk, not in /tmp."""
    seen = tmp_path / "seen"
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args, **_kwargs: ["sh", "-c", f'printf %s "$TMPDIR" > {seen}'],
    )

    result = run_agent(
        "codex", "irrelevant", cwd=tmp_path, log_path=tmp_path / "agent.log", timeout_seconds=5
    )

    assert result.returncode == 0
    assert seen.read_text() == str(isolated_agent_tmp)
    assert isolated_agent_tmp.is_dir()


def test_run_agent_sweeps_stale_scratch_but_keeps_recent_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_tmp: Path
) -> None:
    stale_dir = isolated_agent_tmp / "fallow-audit-base-cache-old"
    stale_file = isolated_agent_tmp / ".hidden-old"
    fresh_dir = isolated_agent_tmp / "fallow-audit-base-cache-new"
    for directory in (stale_dir, fresh_dir):
        directory.mkdir(parents=True)
        (directory / "cache.bin").write_text("x", encoding="utf-8")
    stale_file.write_text("x", encoding="utf-8")
    two_days_ago = time.time() - 2 * 86400
    for path in (stale_dir, stale_file):
        os.utime(path, (two_days_ago, two_days_ago))
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command", lambda *_args, **_kwargs: ["true"]
    )

    run_agent(
        "codex", "irrelevant", cwd=tmp_path, log_path=tmp_path / "agent.log", timeout_seconds=5
    )

    assert not stale_dir.exists()
    assert not stale_file.exists()
    assert fresh_dir.exists()


def test_agent_environment_survives_an_unwritable_scratch_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr("dev_agents.runtime.agent.agent_tmp_root", lambda: blocker / "tmp")
    monkeypatch.setenv("TMPDIR", "/original")

    assert _agent_environment()["TMPDIR"] == "/original"
