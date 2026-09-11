"""Candidate selection for bounded, autonomous god-file decomposition."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from dev_agents.runtime import GitHubError, gh_json, run_agent


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


@dataclass(frozen=True)
class DegodifyRunResult:
    candidate: DegodifyFile | None
    branch: str | None
    pull_request_url: str | None
    dry_run: bool
    succeeded: bool


IGNORED_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".svelte-kit"}
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".svelte"}


def analyze_repository(repo: Path, *, top_count: int = 50) -> list[DegodifyFile]:
    """Build a deterministic, read-only size analysis of source files."""
    results: list[DegodifyFile] = []
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(repo).parts):
            continue
        relative = path.relative_to(repo).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        code_lines = sum(bool(line.strip()) and not line.lstrip().startswith(("//", "/*", "*")) for line in lines)
        is_catalog = len(lines) > 500 and code_lines > 0 and code_lines / len(lines) < 0.2
        status = "CRITICAL" if len(lines) >= 1000 else "WATCH" if len(lines) >= 500 else "STABLE"
        results.append(DegodifyFile(relative, len(lines), code_lines, "Utility / Module", status, is_catalog))
    results.sort(key=lambda item: (item.status == "STABLE", -item.total_lines, item.relative_path))
    return results[:top_count]


def active_repository_items(repo: Path) -> list[str]:
    """Collect open PR titles/branches and remote branches for collision checks."""
    items: list[str] = []
    try:
        for pr in gh_json(repo, "pr", "list", "--state", "open", "--json", "title,headRefName"):
            if pr.get("title"):
                items.append(str(pr["title"]).lower())
            if pr.get("headRefName"):
                items.append(str(pr["headRefName"]).lower())
    except (GitHubError, TypeError):
        pass
    try:
        import subprocess

        result = subprocess.run(
            ["git", "branch", "-r"], cwd=repo, text=True, capture_output=True, check=False
        )
        items.extend(line.strip().lower() for line in result.stdout.splitlines() if line.strip())
    except OSError:
        pass
    return items


def plan_degodify(repo: Path, *, top_count: int = 50) -> CandidateSelection:
    """Produce a read-only decomposition plan for a repository."""
    return select_candidate(analyze_repository(repo, top_count=top_count), active_repository_items(repo))


def run_degodify(
    repo: Path,
    *,
    base_branch: str = "staging",
    provider: str = "codex",
    log_path: Path | None = None,
    timeout_seconds: float = 25 * 60,
    dry_run: bool = True,
) -> DegodifyRunResult:
    """Run one bounded decomposition, with mutation disabled by default."""
    selection = plan_degodify(repo)
    candidate = selection.candidate
    if candidate is None:
        return DegodifyRunResult(None, None, None, dry_run, True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    slug = "".join(char if char.isalnum() else "-" for char in PurePosixPath(candidate.relative_path).name).lower()
    branch = f"curator/degod-{slug}-{stamp}"
    if dry_run:
        return DegodifyRunResult(candidate, branch, None, True, True)

    root = repo / ".dev-agents-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="degodify-", dir=root))
    try:
        _git(repo, "fetch", "origin", base_branch)
        _git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base_branch}")
        prompt = build_decomposition_prompt(candidate, branch, base_branch)
        result = run_agent(
            provider,
            prompt,
            cwd=worktree,
            log_path=log_path or repo / ".dev-agents" / "degodify.log",
            timeout_seconds=timeout_seconds,
        )
        clean = _git(worktree, "status", "--porcelain") == ""
        changed = _git(worktree, "rev-parse", "HEAD") != _git(worktree, "rev-parse", f"origin/{base_branch}")
        if result.returncode != 0 or not clean or not changed:
            return DegodifyRunResult(candidate, branch, None, False, False)
        _git(worktree, "push", "-u", "origin", branch)
        title = f"♻️ Curator: [degodify] extract concern from {PurePosixPath(candidate.relative_path).name}"
        body = f"Automated bounded decomposition of `{candidate.relative_path}`.\n\nAgent: `{provider}`."
        pr_url = _gh(repo, "pr", "create", "--base", base_branch, "--head", branch, "--title", title, "--body", body)
        return DegodifyRunResult(candidate, branch, pr_url, False, True)
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(("git", *args), cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def _gh(repo: Path, *args: str) -> str:
    result = subprocess.run(("gh", *args), cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    return result.stdout.strip()


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
