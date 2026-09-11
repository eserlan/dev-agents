"""Candidate selection for bounded, autonomous god-file decomposition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class DegodifyFile:
    relative_path: str
    total_lines: int
    code_lines: int
    file_type: str
    status: str
    is_data_catalog: bool = False


@dataclass(frozen=True)
class SkippedCandidate:
    file: DegodifyFile
    reason: str


@dataclass(frozen=True)
class CandidateSelection:
    candidate: DegodifyFile | None
    skipped: list[SkippedCandidate]


def select_candidate(files: list[DegodifyFile], active_items: list[str]) -> CandidateSelection:
    """Select the highest-priority eligible file, avoiding active branches/PRs."""
    active = [item.lower() for item in active_items]
    skipped: list[SkippedCandidate] = []
    for file in files:
        if file.is_data_catalog or file.status == "STABLE":
            continue
        base = PurePosixPath(file.relative_path).name.lower()
        stem = PurePosixPath(file.relative_path).stem.lower()
        if any(base in item or stem in item for item in active):
            skipped.append(
                SkippedCandidate(file, f"Active branch or open PR already targets {base}")
            )
            continue
        return CandidateSelection(file, skipped)
    return CandidateSelection(None, skipped)


def build_decomposition_prompt(file: DegodifyFile, branch: str, base_branch: str) -> str:
    """Build the bounded extraction prompt without performing any mutation."""
    return f"""You are Curator, an autonomous refactoring specialist.

TARGET FILE: {file.relative_path} (Current size: {file.total_lines} lines, {file.code_lines} code lines, type: {file.file_type})
CURRENT BRANCH: {branch} (branched from {base_branch})

Extract ONE single cohesive responsibility into a dedicated sibling file.
Preserve the existing public API and behavior. Add focused tests for the extraction.
Run the focused test, type-check, and lint gates before committing or pushing.
Do not merge the pull request."""
