from pathlib import Path

from dev_agents.workflows.inspection import inspect_project


def test_inspection_graph_returns_serializable_summary(git_repository: Path, tmp_path: Path) -> None:
    (git_repository / "AGENTS.md").write_text("Use focused changes.", encoding="utf-8")
    config = tmp_path / "projects.yaml"
    config.write_text(f"projects:\n  demo:\n    repo: {git_repository}\n", encoding="utf-8")

    summary = inspect_project(config, "demo")

    assert summary["project"] == "demo"
    assert summary["repository"]["branch"] == "main"
    assert summary["repository"]["is_clean"] is False
    assert summary["instructions"]["documents"][0]["path"].endswith("AGENTS.md")
