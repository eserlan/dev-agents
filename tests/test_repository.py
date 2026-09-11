from pathlib import Path

from dev_agents.context.repository import load_repository_context


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
