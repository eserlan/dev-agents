from pathlib import Path

from dev_agents.context.instructions import discover_instructions


def test_discovers_agents_and_linked_document(git_repository: Path) -> None:
    docs = git_repository / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text("Architecture details", encoding="utf-8")
    (git_repository / "AGENTS.md").write_text(
        "Read the [architecture](docs/architecture.md).", encoding="utf-8"
    )

    context = discover_instructions(git_repository)

    assert [document.path.name for document in context.documents] == ["AGENTS.md", "architecture.md"]
