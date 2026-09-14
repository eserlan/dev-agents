"""Candidate selection for bounded, autonomous god-file decomposition."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from dev_agents.runtime import AgentResult, GitHubError, gh_json, run_agent, run_with_fallback


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


class DegodifyState(TypedDict, total=False):
    repo: Path
    base_branch: str
    provider: str
    providers: list[str]
    log_path: Path | None
    timeout_seconds: float
    dry_run: bool
    supplied_candidate: DegodifyFile | None
    on_event: Callable[[str, dict[str, Any]], None] | None
    candidate: DegodifyFile | None
    result: DegodifyRunResult


IGNORED_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".svelte-kit"}
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".svelte"}


def _emit(state: DegodifyState, event: str, payload: dict[str, Any]) -> None:
    callback = state.get("on_event")
    if callback is not None:
        callback(event, payload)


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
        result = subprocess.run(
            ["git", "branch", "-r"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        items.extend(line.strip().lower() for line in result.stdout.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return items


def plan_degodify(repo: Path, *, top_count: int = 50) -> CandidateSelection:
    """Produce a read-only decomposition plan for a repository."""
    return select_candidate(analyze_repository(repo, top_count=top_count), active_repository_items(repo))


def _run_degodify_impl(
    repo: Path,
    *,
    base_branch: str = "staging",
    provider: str = "codex",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 25 * 60,
    dry_run: bool = True,
    supplied_candidate: DegodifyFile | None = None,
) -> DegodifyRunResult:
    """Run one bounded decomposition, with mutation disabled by default."""
    candidate = supplied_candidate or plan_degodify(repo).candidate
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
        prompt = build_decomposition_prompt(candidate, branch, base_branch, worktree)
        configured_providers = providers or [provider]
        def run_provider(name: str) -> AgentResult:
            if name != configured_providers[0]:
                _git(worktree, "reset", "--hard", f"origin/{base_branch}")
                _git(worktree, "clean", "-fdx")
            return run_agent(name, prompt, cwd=worktree, log_path=log_path or repo / ".dev-agents" / "degodify.log", timeout_seconds=timeout_seconds)
        accepted = run_with_fallback(configured_providers, run_provider)
        result = accepted[1] if accepted else None
        clean = _git(worktree, "status", "--porcelain") == ""
        changed = _git(worktree, "rev-parse", "HEAD") != _git(worktree, "rev-parse", f"origin/{base_branch}")
        if result is None or not clean or not changed:
            return DegodifyRunResult(candidate, branch, None, False, False)
        _git(worktree, "push", "-u", "origin", branch)
        title = f"♻️ Curator: [degodify] extract concern from {PurePosixPath(candidate.relative_path).name}"
        body = f"Automated bounded decomposition of `{candidate.relative_path}`.\n\nAgent: `{provider}`."
        pr_url = _gh(repo, "pr", "create", "--base", base_branch, "--head", branch, "--title", title, "--body", body)
        return DegodifyRunResult(candidate, branch, pr_url, False, True)
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def build_degodify_workflow() -> Any:
    """Build the event-driven degodify graph."""
    graph = StateGraph(DegodifyState)

    def prepare(state: DegodifyState) -> dict[str, object]:
        candidate = state.get("supplied_candidate") or plan_degodify(state["repo"]).candidate
        _emit(
            state,
            "prepare_candidate",
            {"candidate": candidate.relative_path if candidate is not None else None},
        )
        return {"candidate": candidate}

    def execute(state: DegodifyState) -> dict[str, object]:
        result = _run_degodify_impl(
            state["repo"],
            base_branch=state.get("base_branch", "staging"),
            provider=state.get("provider", "codex"),
            providers=state.get("providers"),
            log_path=state.get("log_path"),
            timeout_seconds=state.get("timeout_seconds", 25 * 60),
            dry_run=state.get("dry_run", True),
            supplied_candidate=state.get("candidate"),
        )
        _emit(state, "execute_decomposition", {"succeeded": result.succeeded})
        return {"result": result}

    def publish(state: DegodifyState) -> dict[str, object]:
        _emit(
            state,
            "publish_result",
            {"branch": state["result"].branch, "succeeded": state["result"].succeeded},
        )
        return {"result": state["result"]}

    graph.add_node("prepare_candidate", prepare)
    graph.add_node("execute_decomposition", execute)
    graph.add_node("publish_result", publish)
    graph.add_edge(START, "prepare_candidate")
    graph.add_edge("prepare_candidate", "execute_decomposition")
    graph.add_edge("execute_decomposition", "publish_result")
    graph.add_edge("publish_result", END)
    return graph.compile()


def run_degodify(
    repo: Path,
    *,
    base_branch: str = "staging",
    provider: str = "codex",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 25 * 60,
    dry_run: bool = True,
    supplied_candidate: DegodifyFile | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> DegodifyRunResult:
    """Run degodify through its LangGraph workflow."""
    result = build_degodify_workflow().invoke({
        "repo": repo,
        "base_branch": base_branch,
        "provider": provider,
        "providers": providers,
        "log_path": log_path,
        "timeout_seconds": timeout_seconds,
        "dry_run": dry_run,
        "supplied_candidate": supplied_candidate,
        "on_event": on_event,
    })
    return cast(DegodifyRunResult, result["result"])


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            ("git", *args), cwd=repo, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout:g}s") from error
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def _gh(repo: Path, *args: str, timeout: float = 60) -> str:
    try:
        result = subprocess.run(
            ("gh", *args), cwd=repo, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"gh {' '.join(args)} timed out after {timeout:g}s") from error
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


def build_decomposition_prompt(file: DegodifyFile, branch: str, base_branch: str, worktree: Path) -> str:
    """Build the bounded extraction prompt without performing any mutation."""
    changed_files_output = _git(worktree, "diff", "--name-only", f"origin/{base_branch}...HEAD", check=False)
    changed_files = [path for path in changed_files_output.splitlines() if path and (worktree / path).exists()]
    changed_text = " ".join(changed_files) if changed_files else "<no changed files detected>"
    return f"""You are Curator, an autonomous refactoring specialist.

TARGET FILE: {file.relative_path} (Current size: {file.total_lines} lines, {file.code_lines} code lines, type: {file.file_type})
CURRENT BRANCH: {branch} (branched from {base_branch})

Extract ONE single cohesive responsibility into a dedicated sibling file.
Preserve the existing public API and behavior. Add focused tests for the extraction.

Changed files (use these paths for targeted testing and verification):
{changed_text}

Run focused tests for the changed files first. For linting and formatting, target only the changed files
(for example, `bunx eslint <changed-files>` and `bunx prettier --check <changed-files>`); do not run
the repository-wide `bun run lint` unless a targeted command is unavailable. Run type-check only when
required by repository instructions. Commit and push HEAD to the PR branch.
Do not merge the pull request."""
