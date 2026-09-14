from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dev_agents.runtime.github import GitHubError, gh


def _timeout(*args: object, **kwargs: object) -> object:
    raise subprocess.TimeoutExpired(cmd="gh", timeout=60)


def test_gh_timeout_raises_github_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dev_agents.runtime.github.subprocess.run", _timeout)
    with pytest.raises(GitHubError, match="timed out"):
        gh(tmp_path, "pr", "view", "1")


def test_gh_passes_default_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class Proc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(cmd: object, **kwargs: object) -> Proc:
        seen.update(kwargs)
        return Proc()

    monkeypatch.setattr("dev_agents.runtime.github.subprocess.run", fake_run)
    assert gh(tmp_path, "repo", "view") == "ok"
    assert seen.get("timeout") == 60
