"""Discovery of repository-local agent instructions and linked documents."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class InstructionDocument(BaseModel):
    """One local instruction or architecture document."""

    model_config = ConfigDict(frozen=True)

    path: Path
    content: str


class InstructionsContext(BaseModel):
    """Instruction documents found inside the target repository."""

    model_config = ConfigDict(frozen=True)

    documents: list[InstructionDocument]


_CANDIDATES = (Path("AGENTS.md"), Path(".agent/AGENTS.md"), Path(".agent/instructions.md"))
_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)")


def _inside(repository: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(repository)
    except ValueError:
        return False
    return True


def _linked_documents(repository: Path, document: InstructionDocument) -> list[Path]:
    paths: list[Path] = []
    for target in _MARKDOWN_LINK.findall(document.content):
        candidate = (document.path.parent / target).resolve()
        if _inside(repository, candidate) and candidate.is_file():
            paths.append(candidate)
    return paths


def discover_instructions(repository_path: Path) -> InstructionsContext:
    """Discover common local instruction files and direct Markdown-linked documents."""
    repository = repository_path.expanduser().resolve()
    documents: list[InstructionDocument] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        documents.append(InstructionDocument(path=path, content=path.read_text(encoding="utf-8")))

    for candidate in _CANDIDATES:
        path = repository / candidate
        if path.is_file():
            add(path.resolve())

    for document in tuple(documents):
        for linked in _linked_documents(repository, document):
            add(linked)

    return InstructionsContext(documents=documents)
