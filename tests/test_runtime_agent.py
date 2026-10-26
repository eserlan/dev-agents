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
        "-",
    ]


def test_codex_provider_command_can_use_luna_high_reasoning() -> None:
    assert 'model_reasoning_effort="high"' in provider_command(
        "codex", "review it", reasoning_effort="high"
    )


def test_auditor_uses_enforced_read_only_without_user_mcp_or_hooks() -> None:
    command = provider_command("codex", "audit", read_only=True)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert 'approval_policy="never"' in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


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


def test_codex_final_message_is_separate_from_echoed_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sys

    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args: [
            sys.executable,
            "-c",
            (
                'import pathlib,sys; print("Echoed prompt and intermediate text"); '
                'pathlib.Path(sys.argv[-1]).write_text("Final assessment")'
            ),
        ],
    )
    output = tmp_path / "final.txt"
    result = run_agent(
        "codex",
        "Review",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        timeout_seconds=5,
        heartbeat_seconds=0.01,
        last_message_path=output,
    )
    assert result.returncode == 0
    assert output.read_text() == "Final assessment"
    assert "Echoed prompt" in (tmp_path / "agent.log").read_text()


def test_muse_provider_command_accepts_queue_model() -> None:
    command = provider_command("muse", "write an answer", model="muse-wonder-1.0")
    assert command[command.index("--model") + 1] == "muse-wonder-1.0"


def test_muse_provider_command_forwards_reasoning_effort_when_set() -> None:
    command = provider_command(
        "muse", "write an answer", model="muse-wonder-1.0", reasoning_effort="high"
    )
    assert command[command.index("--reasoning-effort") + 1] == "high"
    # No effort requested: leave the model's own default in charge.
    assert "--reasoning-effort" not in provider_command("muse", "write an answer")


def test_muse_provider_command_can_emit_json_events() -> None:
    command = provider_command("muse", "write an answer", json_events=True)
    assert "--json" in command
    assert "--json" not in provider_command("muse", "write an answer")


@pytest.fixture(autouse=True)
def disable_systemd_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dev_agents.runtime.agent._user_systemd_available", lambda: False)


@pytest.mark.parametrize("use_scope", [False, True])
@pytest.mark.parametrize("cancel", [True, False])
def test_cancellation_and_timeout_kill_surviving_descendant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel: bool, use_scope: bool,
) -> None:
    import signal
    import sys
    from threading import Event, Thread

    if use_scope:
        import subprocess

        probe = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if probe.returncode:
            pytest.skip("user systemd manager unavailable")
        monkeypatch.setattr("dev_agents.runtime.agent._user_systemd_available", lambda: True)

    ready = tmp_path / "child.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session={use_scope!r}); time.sleep(60)"
    )
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args, **_kwargs: [sys.executable, "-c", parent],
    )
    event = Event()

    def trigger() -> None:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        if cancel:
            event.set()

    watcher = Thread(target=trigger)
    watcher.start()
    try:
        result = run_agent(
            "codex", "test", cwd=tmp_path, log_path=tmp_path / "agent.log",
            timeout_seconds=10 if cancel else 1, heartbeat_seconds=.01,
            cancel_event=event,
        )
        assert result.timed_out is (not cancel)
        assert result.returncode != 0
        child_pid = int(ready.read_text())
        # A reparented child may remain a zombie until PID 1 reaps it.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(.01)
        else:
            pytest.fail("test descendant survived run_agent cleanup")
    finally:
        watcher.join()
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_scope_stop_accepts_a_collected_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from dev_agents.runtime.agent import _stop_scope

    results = iter([
        subprocess.CompletedProcess([], 5, stderr="Unit not loaded"),
        subprocess.CompletedProcess([], 1, stdout="not-found\n"),
    ])
    monkeypatch.setattr("dev_agents.runtime.agent.subprocess.run", lambda *a, **kw: next(results))
    _stop_scope("dev-agents-test.scope")


def test_scope_stop_does_not_hide_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from dev_agents.runtime.agent import _stop_scope

    results = iter([
        subprocess.CompletedProcess([], 1, stderr="Access denied"),
        subprocess.CompletedProcess([], 0, stdout="loaded\n"),
    ])
    monkeypatch.setattr("dev_agents.runtime.agent.subprocess.run", lambda *a, **kw: next(results))
    with pytest.raises(RuntimeError, match="Access denied"):
        _stop_scope("dev-agents-test.scope")
