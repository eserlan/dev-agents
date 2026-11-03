"""Candidate selection and fallow-guided, autonomous god-file decomposition."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from dev_agents.runtime import GitHubError, gh_json
from dev_agents.workflows.committing_agent import (
    MAX_REPAIRS,
    NETWORK_RETRY,
    CommitLoopState,
    Problem,
    add_worktree,
    build_commit_loop,
    install_dependencies,
    open_pull_request_with_prefix,
    remove_worktree,
)
from dev_agents.workflows.committing_agent import gh as _gh
from dev_agents.workflows.committing_agent import git as _git

# Kept under the old name; tests and callers patch it here.
_install_dependencies = install_dependencies


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
    reason: str | None = None


@dataclass(frozen=True)
class FallowFileReport:
    """The parts of `fallow inspect --file` that tell an agent where and how to split."""

    exports: list[tuple[str, list[str]]]
    complex_functions: list[dict[str, Any]]
    clones: list[str]
    dead_code: list[str]

    def prompt_section(self) -> str:
        lines = [f"Exports and the files that use them ({len(self.exports)}). Exports used by the same files usually belong together:"]
        for name, consumers in self.exports[:MAX_PROMPT_ITEMS]:
            shown = ", ".join(PurePosixPath(path).name for path in consumers[:4]) or "no direct importers"
            more = f" (+{len(consumers) - 4} more)" if len(consumers) > 4 else ""
            lines.append(f"- {name}: {shown}{more}")
        if self.complex_functions:
            lines.append("Complex functions to break up (cyclomatic / cognitive):")
            lines.extend(
                f"- {item['name']} at line {item['line']}: {item['cyclomatic']} / {item['cognitive']} ({item['severity']})"
                for item in self.complex_functions[:MAX_PROMPT_ITEMS]
            )
        if self.clones:
            lines.append("Duplicated blocks to consolidate:")
            lines.extend(f"- {clone}" for clone in self.clones[:MAX_PROMPT_ITEMS])
        if self.dead_code:
            lines.append("Dead code to delete:")
            lines.extend(f"- {item}" for item in self.dead_code[:MAX_PROMPT_ITEMS])
        return "\n".join(lines)


class DegodifyState(CommitLoopState, total=False):
    """Degodify inputs and result; the commit-loop keys (worktree, prompt, repairs, ...) are inherited."""

    provider: str
    dry_run: bool
    stamp: str
    supplied_candidate: DegodifyFile | None
    candidate: DegodifyFile | None
    use_fallow: bool
    fallow: FallowFileReport | None
    # Measured by the commit loop's check, for the PR body.
    after_lines: int
    audit: dict[str, Any] | None
    result: DegodifyRunResult


IGNORED_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".svelte-kit"}
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".svelte"}
# A decomposition aims for the WATCH threshold, and must at least cut the file by a quarter to be
# worth a PR; anything less is the single surgical extraction this workflow used to make.
TARGET_LINES = 500
MIN_REDUCTION = 0.25
# Only one decomposition is open at a time, so nightly runs don't stack refactors on each other.
BRANCH_PREFIX = "curator/degod-"
MAX_PROMPT_ITEMS = 25


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


def fallow_enabled(worktree: Path) -> bool:
    """Whether the target repository uses fallow (its own config or dependency)."""
    package = worktree / "package.json"
    return (worktree / ".fallowrc.json").is_file() or (
        package.is_file() and '"fallow"' in package.read_text(encoding="utf-8", errors="replace")
    )


def _fallow_json(worktree: Path, *args: str, timeout: float = 600) -> dict[str, Any] | None:
    """Run one read-only fallow query; None when it is unavailable or its output is unusable."""
    try:
        result = subprocess.run(
            ("bunx", "fallow", *args, "--format", "json", "--quiet"),
            cwd=worktree, capture_output=True, text=True, timeout=timeout, check=False,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def summarize_fallow_inspect(data: dict[str, Any], target: str) -> FallowFileReport:
    """Reduce `fallow inspect --file` JSON to the evidence a decomposition needs."""
    evidence = data.get("evidence") or {}

    def section(name: str) -> dict[str, Any]:
        value = evidence.get(name) or {}
        inner = value.get("data") if isinstance(value, dict) and "data" in value else value
        return inner if isinstance(inner, dict) else {}

    exports = [
        (str(item.get("name")), [str(ref.get("from_file")) for ref in item.get("referenced_by") or [] if ref.get("from_file")])
        for item in section("trace_file").get("exports") or []
        if isinstance(item, dict) and item.get("name")
    ]
    complex_functions = sorted(
        (
            {key: item.get(key) for key in ("name", "line", "cyclomatic", "cognitive", "severity")}
            for item in section("complexity").get("findings") or []
            if isinstance(item, dict) and item.get("path") == target
        ),
        key=lambda item: -(item.get("cognitive") or 0),
    )
    clones = []
    for group in section("duplication").get("clone_groups") or []:
        instances = [item for item in group.get("instances") or [] if isinstance(item, dict)]
        clones.append(", ".join(f"{PurePosixPath(str(item.get('file'))).name}:{item.get('start_line')}-{item.get('end_line')}" for item in instances))
    dead = section("dead_code")
    dead_code = [
        str(item.get("export_name") or item.get("name") or item)
        for key in ("unused_exports", "unused_types", "unused_class_members", "unused_enum_members")
        for item in dead.get(key) or []
        if isinstance(item, dict)
    ]
    return FallowFileReport(exports, complex_functions, clones, dead_code)


def decomposition_gate(before_lines: int, after_lines: int, audit: dict[str, Any] | None) -> str | None:
    """Return why a finished decomposition should not become a PR, or None when it qualifies."""
    if before_lines and (before_lines - after_lines) / before_lines < MIN_REDUCTION:
        return f"target only shrank from {before_lines} to {after_lines} lines (needs at least {MIN_REDUCTION:.0%})"
    if audit is not None and audit.get("verdict") == "fail":
        return "fallow audit failed for the decomposition"
    return None


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines()) if path.is_file() else 0


# The target repository's own analyzer owns which files may stay large (Codex-Cryptica keeps its
# data-catalogue exemptions there); the built-in scan is only a fallback for repositories without it.
REPOSITORY_ANALYZER = "scripts/god-file-analysis.ts"


def repository_analysis(repo: Path, *, top_count: int = 50) -> list[DegodifyFile] | None:
    """Rank files with the repository's own god-file analyzer, or None when it has none."""
    if not (repo / REPOSITORY_ANALYZER).is_file():
        return None
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / "god-files.json"
        try:
            subprocess.run(
                ("bun", REPOSITORY_ANALYZER, f"--json-file={report_path}", f"--summary-file={Path(directory) / 'summary.md'}", "--top", str(top_count)),
                cwd=repo, capture_output=True, text=True, timeout=300, check=False,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None
    return [
        DegodifyFile(
            str(item.get("relativePath") or item["path"]),
            int(item["totalLines"]),
            int(item["codeLines"]),
            str(item.get("type") or "Utility / Module"),
            str(item.get("status") or "WATCH"),
            bool(item.get("isDataCatalog", False)),
        )
        for item in report.get("topFiles") or []
        if isinstance(item, dict) and "totalLines" in item and "codeLines" in item
    ]


def open_degodify_pull_request(repo: Path) -> str | None:
    """Return the URL of an already open degodify PR, if there is one."""
    return open_pull_request_with_prefix(repo, BRANCH_PREFIX)


def plan_degodify(repo: Path, *, top_count: int = 50) -> CandidateSelection:
    """Produce a read-only decomposition plan for a repository."""
    files = repository_analysis(repo, top_count=top_count)
    if files is None:
        files = analyze_repository(repo, top_count=top_count)
    return select_candidate(files, active_repository_items(repo))


def build_degodify_workflow() -> Any:
    """Build the event-driven degodify graph.

    prepare_candidate -> prepare_worktree -> commit_loop -> open_pull_request -> publish_result,
    where commit_loop is the shared run_agent / check_change / push_branch subgraph. Its check
    is the decomposition gate, so a split that is too small or fails the fallow audit goes back
    to the agent. Runs that end early go straight to publish_result.
    """
    graph = StateGraph(DegodifyState)

    def prepare_candidate(state: DegodifyState) -> dict[str, object]:
        candidate = state.get("supplied_candidate") or plan_degodify(state["repo"]).candidate
        _emit(state, "prepare_candidate", {"candidate": candidate.relative_path if candidate is not None else None})
        if candidate is None:
            return {"candidate": None, "result": DegodifyRunResult(None, None, None, state.get("dry_run", True), True)}
        return {"candidate": candidate}

    def prepare_worktree(state: DegodifyState) -> dict[str, object]:
        repo, candidate, base = state["repo"], state["candidate"], state["base_branch"]
        assert candidate is not None
        slug = "".join(char if char.isalnum() else "-" for char in PurePosixPath(candidate.relative_path).name).lower()
        branch = f"{BRANCH_PREFIX}{slug}-{state['stamp']}"
        if state.get("dry_run", True):
            return {"branch": branch, "result": DegodifyRunResult(candidate, branch, None, True, True)}
        if open_pull := open_degodify_pull_request(repo):
            # Not a failure: tonight's decomposition waits until the previous one has merged.
            return {"result": DegodifyRunResult(candidate, None, None, False, True, f"waiting for open degodify PR {open_pull}")}
        worktree = state["worktree"]
        add_worktree(repo, base, branch, worktree)
        use_fallow = fallow_enabled(worktree) and _install_dependencies(worktree)
        inspected = _fallow_json(worktree, "inspect", "--file", candidate.relative_path) if use_fallow else None
        fallow = summarize_fallow_inspect(inspected, candidate.relative_path) if inspected else None
        return {
            "branch": branch, "use_fallow": use_fallow, "fallow": fallow, "task": "decomposition",
            "prompt": build_decomposition_prompt(candidate, branch, base, worktree, fallow),
            "log_path": state.get("log_path") or repo / ".dev-agents" / "degodify.log",
            "providers": state.get("providers") or [state.get("provider", "codex")],
            "after_reset": (lambda: _install_dependencies(worktree)) if use_fallow else None,
        }

    def gate(state: DegodifyState) -> tuple[Problem | None, dict[str, Any]]:
        candidate, worktree = state["candidate"], state["worktree"]
        assert candidate is not None
        after_lines = _line_count(worktree / candidate.relative_path)
        # A fallback provider's reset runs `git clean -fdx`, which also removes node_modules.
        audit = (
            _fallow_json(worktree, "audit", "--base", f"origin/{state['base_branch']}")
            if state.get("use_fallow") and _install_dependencies(worktree)
            else None
        )
        reason = decomposition_gate(candidate.total_lines, after_lines, audit)
        measured = {"after_lines": after_lines, "audit": audit}
        if reason is None:
            return None, measured
        return Problem(f"The decomposition does not meet the bar yet: {reason}. Keep going until it does.", reason), measured

    def open_pr(state: DegodifyState) -> dict[str, object]:
        repo, worktree, branch, candidate = state["repo"], state["worktree"], state["branch"], state["candidate"]
        assert candidate is not None
        after_lines, audit, fallow = state["after_lines"], state.get("audit"), state.get("fallow")
        # Idempotent, so a retry after a timed-out `gh pr create` does not open a second PR.
        if pr_url := open_pull_request_with_prefix(repo, branch):
            return {"result": DegodifyRunResult(candidate, branch, pr_url, False, True)}
        new_files = [
            path for path in _git(worktree, "diff", "--name-only", "--diff-filter=A", f"origin/{state['base_branch']}...HEAD").splitlines() if path
        ]
        name = PurePosixPath(candidate.relative_path).name
        title = f"♻️ Curator: [degodify] decompose {name} ({candidate.total_lines} → {after_lines} lines)"
        body = "\n".join([
            f"Automated decomposition of `{candidate.relative_path}`, guided by fallow.",
            "",
            "| | Before | After |",
            "| --- | --- | --- |",
            f"| Lines in `{name}` | {candidate.total_lines} | {after_lines} |",
            f"| Complex functions flagged | {len(fallow.complex_functions) if fallow else 'n/a'} | see fallow audit |",
            f"| fallow audit | | {audit.get('verdict') if audit else 'not run'} |",
            "",
            "New modules:",
            *[f"- `{path}` ({_line_count(worktree / path)} lines)" for path in new_files],
            "",
            f"Agent: `{state['accepted']}`.",
        ])
        pr_url = _gh(repo, "pr", "create", "--base", state["base_branch"], "--head", branch, "--title", title, "--body", body)
        return {"result": DegodifyRunResult(candidate, branch, pr_url, False, True)}

    def publish(state: DegodifyState) -> dict[str, object]:
        result = state.get("result") or DegodifyRunResult(
            state["candidate"], state.get("branch"), None, False, False,
            # The gate requires a committed change, so "no_change" only follows an agent failure.
            state.get("commit_reason") or "no clean committed change; see agent_attempt events and log",
        )
        _emit(state, "publish_result", {"branch": result.branch, "succeeded": result.succeeded, "reason": result.reason})
        return {"result": result}

    graph.add_node("prepare_candidate", prepare_candidate)
    graph.add_node("prepare_worktree", prepare_worktree)
    graph.add_node("commit_loop", build_commit_loop(gate, state_schema=DegodifyState))
    graph.add_node("open_pull_request", open_pr, retry_policy=NETWORK_RETRY)
    graph.add_node("publish_result", publish)
    graph.add_edge(START, "prepare_candidate")
    graph.add_conditional_edges(
        "prepare_candidate", lambda state: "publish_result" if "result" in state else "prepare_worktree",
        ["prepare_worktree", "publish_result"])
    graph.add_conditional_edges(
        "prepare_worktree", lambda state: "publish_result" if "result" in state else "commit_loop",
        ["commit_loop", "publish_result"])
    graph.add_conditional_edges(
        "commit_loop", lambda state: "open_pull_request" if state.get("commit_outcome") == "pushed" else "publish_result",
        ["open_pull_request", "publish_result"])
    graph.add_edge("open_pull_request", "publish_result")
    graph.add_edge("publish_result", END)
    return graph.compile()


def _run_degodify_impl(
    repo: Path,
    *,
    base_branch: str = "staging",
    provider: str = "codex",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 25 * 60,
    model: str | None = None,
    reasoning_effort: str = "high",
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
    dry_run: bool = True,
    supplied_candidate: DegodifyFile | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> DegodifyRunResult:
    """Invoke the degodify graph, then remove its worktree and unpushed branch whatever happened."""
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    worktree = repo / ".dev-agents-worktrees" / f"degodify-{stamp}"
    try:
        # Each repair is three more steps (run_agent, check_change, push_branch).
        final = build_degodify_workflow().invoke({
            "repo": repo, "base_branch": base_branch, "provider": provider, "providers": providers,
            "log_path": log_path, "timeout_seconds": timeout_seconds,
            "model": model, "reasoning_effort": reasoning_effort,
            "fallback_model": fallback_model, "fallback_reasoning_effort": fallback_reasoning_effort,
            "dry_run": dry_run, "supplied_candidate": supplied_candidate, "on_event": on_event,
            "stamp": stamp, "worktree": worktree,
        }, {"recursion_limit": 25 + 3 * MAX_REPAIRS})
    finally:
        remove_worktree(repo, worktree)
    return cast(DegodifyRunResult, final["result"])


def run_degodify(
    repo: Path,
    *,
    base_branch: str = "staging",
    provider: str = "codex",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 25 * 60,
    model: str | None = None,
    reasoning_effort: str = "high",
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
    dry_run: bool = True,
    supplied_candidate: DegodifyFile | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> DegodifyRunResult:
    """Run degodify through its LangGraph workflow, with mutation disabled by default."""
    return _run_degodify_impl(
        repo, base_branch=base_branch, provider=provider, providers=providers, log_path=log_path,
        timeout_seconds=timeout_seconds, model=model, reasoning_effort=reasoning_effort,
        fallback_model=fallback_model, fallback_reasoning_effort=fallback_reasoning_effort,
        dry_run=dry_run, supplied_candidate=supplied_candidate, on_event=on_event,
    )


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


def build_decomposition_prompt(
    file: DegodifyFile,
    branch: str,
    base_branch: str,
    worktree: Path,
    fallow: FallowFileReport | None = None,
) -> str:
    """Build the decomposition prompt without performing any mutation."""
    target = min(TARGET_LINES, file.total_lines // 2)
    evidence = (
        fallow.prompt_section()
        if fallow
        else "fallow evidence is unavailable; find the seams by reading the file and its importers."
    )
    audit_line = (
        f"- `bunx fallow audit --base origin/{base_branch}` must not fail.\n" if fallow else ""
    )
    return f"""You are Curator, an autonomous refactoring specialist.

TARGET FILE: {file.relative_path} ({file.total_lines} lines, {file.code_lines} code lines, type: {file.file_type})
CURRENT BRANCH: {branch} (branched from {base_branch})

This is an execution task. Start implementing after enough inspection to identify the seams;
do not finish with a plan or an explanation that the task needs multiple extractions. Work in
small validated steps in this worktree until the requested decomposition is complete. If the
evidence lists no dead code, skip that step. Report an exact blocker only if execution fails.

Decompose this file thoroughly. One surgical extraction is not enough: move every separable
responsibility into focused sibling modules until the file is at most {target} lines. Keep the
original path as a thin module that composes or re-exports the new modules, so every existing
import and the public API keep working unchanged. Do not change behaviour.

Work through it in this order:
1. Delete the dead code listed below.
2. Split by responsibility. Use the export and importer map below to find the seams; exports
   used by the same files usually belong in the same module.
3. Break each complex function below into smaller named functions.
4. Replace duplicated blocks with shared helpers.

Fallow evidence for this file:
{evidence}

Validation before committing:
- Add focused tests for each new module; existing tests must keep passing.
- Lint and format only the changed files (for example `bunx eslint <files>` and
  `bunx prettier --check <files>`), not the whole repository.
- Run the type check when the repository instructions require it.
{audit_line}
Commit in logical steps (one commit per extracted module is fine). Never bypass git hooks
(`--no-verify`, `HUSKY=0`). Do not push, open, or merge a pull request: the workflow checks the
result and pushes it through the repository's pre-push hook, which lints every changed file."""
