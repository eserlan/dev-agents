import subprocess
from pathlib import Path

import pytest

from dev_agents.context.repository import RepositoryError, load_repository_context


def test_loads_git_context(git_repository: Path) -> None:
    (git_repository / "changed.py").write_text("value = 1\n", encoding="utf-8")

    context = load_repository_context(git_repository)

    assert context.branch == "main"
    assert len(context.head_sha) == 40
    assert context.remote_identity == "example/widgets"
    assert context.status == ["?? changed.py"]
    assert context.changed_files == []


def test_reports_changed_files_for_range(git_repository: Path) -> None:
    (git_repository / "README.md").write_text("# changed\n", encoding="utf-8")
    context = load_repository_context(git_repository, "HEAD..HEAD")

    assert context.changed_files == []


def test_git_timeout_raises_repository_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _expired(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="git", timeout=60)

    monkeypatch.setattr("dev_agents.context.repository.subprocess.run", _expired)
    with pytest.raises(RepositoryError, match="timed out"):
        load_repository_context(tmp_path)
