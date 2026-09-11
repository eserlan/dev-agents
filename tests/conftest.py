from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    (repository / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/widgets.git"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return repository
