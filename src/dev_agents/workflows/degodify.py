"""Candidate selection and fallow-guided, autonomous god-file decomposition."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
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


class DegodifyState(TypedDict, total=False):
    repo: Path
    base_branch: str
    provider: str
    providers: list[str]
    model: str | None
    reasoning_effort: str
    fallback_model: str | None
    fallback_reasoning_effort: str | None
    log_path: Path | None
    timeout_seconds: float
    dry_run: bool
    supplied_candidate: DegodifyFile | None
    on_event: Callable[[str, dict[str, Any]], None] | None
    candidate: DegodifyFile | None
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


def _install_dependencies(worktree: Path) -> bool:
    """Install locked dependencies so fallow resolves the worktree like CI does."""
    if (worktree / "node_modules").is_dir():
        return True
    if not any((worktree / name).is_file() for name in ("bun.lock", "bun.lockb")):
        return False
    try:
        result = subprocess.run(
            ("bun", "install", "--frozen-lockfile"), cwd=worktree, capture_output=True, text=True, timeout=900, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
    try:
        pulls = gh_json(repo, "pr", "list", "--state", "open", "--json", "headRefName,url", "--limit", "200")
    except (GitHubError, TypeError):
        return None
    for pull in pulls if isinstance(pulls, list) else []:
        if str(pull.get("headRefName", "")).startswith(BRANCH_PREFIX):
            return str(pull.get("url") or pull.get("headRefName"))
    return None


def plan_degodify(repo: Path, *, top_count: int = 50) -> CandidateSelection:
    """Produce a read-only decomposition plan for a repository."""
    files = repository_analysis(repo, top_count=top_count)
    if files is None:
        files = analyze_repository(repo, top_count=top_count)
    return select_candidate(files, active_repository_items(repo))


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
    """Run one bounded decomposition, with mutation disabled by default."""
    candidate = supplied_candidate or plan_degodify(repo).candidate
    if candidate is None:
        return DegodifyRunResult(None, None, None, dry_run, True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    slug = "".join(char if char.isalnum() else "-" for char in PurePosixPath(candidate.relative_path).name).lower()
    branch = f"{BRANCH_PREFIX}{slug}-{stamp}"
    if dry_run:
        return DegodifyRunResult(candidate, branch, None, True, True)
    if open_pull := open_degodify_pull_request(repo):
        # Not a failure: tonight's decomposition waits until the previous one has merged.
        return DegodifyRunResult(candidate, None, None, False, True, f"waiting for open degodify PR {open_pull}")

    root = repo / ".dev-agents-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="degodify-", dir=root))
    try:
        _git(repo, "fetch", "origin", base_branch)
        _git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base_branch}")
        use_fallow = fallow_enabled(worktree) and _install_dependencies(worktree)
        inspected = _fallow_json(worktree, "inspect", "--file", candidate.relative_path) if use_fallow else None
        fallow = summarize_fallow_inspect(inspected, candidate.relative_path) if inspected else None
        prompt = build_decomposition_prompt(candidate, branch, base_branch, worktree, fallow)
        configured_providers = providers or [provider]
        attempts = 0

        def committed_change() -> bool:
            return (
                _git(worktree, "status", "--porcelain") == ""
                and _git(worktree, "rev-parse", "HEAD") != _git(worktree, "rev-parse", f"origin/{base_branch}")
            )

        def run_provider(name: str) -> AgentResult:
            nonlocal attempts
            if name != configured_providers[0]:
                _git(worktree, "reset", "--hard", f"origin/{base_branch}")
                _git(worktree, "clean", "-fdx")
                if use_fallow:
                    _install_dependencies(worktree)
            result = AgentResult(1, False)
            # One continuation per provider preserves partial edits and gives an
            # inspection-only turn a chance to finish. Each provider gets its own
            # deadline so a slow primary cannot starve the fallback.
            deadline = time.monotonic() + timeout_seconds
            for turn in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return AgentResult(1, True)
                attempts += 1
                recovery = "" if turn == 0 else """
The previous attempt did not leave a clean committed change. Continue in this same worktree;
inspect git status and the existing edits before doing anything else. Preserve partial work.
Implement the decomposition now, run focused validation, and commit. An inspection summary or
plan is not completion. If blocked, give the exact failing command and actionable blocker.
"""
                result = run_agent(
                    name, prompt + recovery, cwd=worktree,
                    log_path=log_path or repo / ".dev-agents" / "degodify.log",
                    timeout_seconds=remaining,
                    # `model` and `reasoning_effort` are Codex settings; the fallback ones are Claude's.
                    model=model if name == "codex" else fallback_model if name == "claude" else None,
                    reasoning_effort=(
                        reasoning_effort if name == "codex"
                        else fallback_reasoning_effort if name == "claude" else None
                    ),
                )
                complete = result.returncode == 0 and not result.timed_out and committed_change()
                if on_event:
                    on_event("agent_attempt", {
                        "provider": name, "attempt": attempts, "continuation": bool(turn),
                        "returncode": result.returncode, "timed_out": result.timed_out,
                        "committed_change": complete,
                    })
                if complete or result.timed_out:
                    break
            return result

        accepted = run_with_fallback(
            configured_providers, run_provider,
            lambda _name, result: result.returncode == 0 and not result.timed_out and committed_change(),
        )
        if accepted is None:
            return DegodifyRunResult(candidate, branch, None, False, False,
                                     f"no clean committed change after {attempts} agent attempts; see agent_attempt events and log")
        after_lines = _line_count(worktree / candidate.relative_path)
        # A fallback provider's reset runs `git clean -fdx`, which also removes node_modules.
        audit = (
            _fallow_json(worktree, "audit", "--base", f"origin/{base_branch}")
            if use_fallow and _install_dependencies(worktree)
            else None
        )
        reason = decomposition_gate(candidate.total_lines, after_lines, audit)
        if reason:
            return DegodifyRunResult(candidate, branch, None, False, False, reason)
        _git(worktree, "push", "-u", "origin", branch)
        new_files = [
            path for path in _git(worktree, "diff", "--name-only", "--diff-filter=A", f"origin/{base_branch}...HEAD").splitlines() if path
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
            f"Agent: `{accepted[0] if accepted else provider}`.",
        ])
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
            model=state.get("model"), reasoning_effort=state.get("reasoning_effort", "high"),
            fallback_model=state.get("fallback_model"),
            fallback_reasoning_effort=state.get("fallback_reasoning_effort"),
            dry_run=state.get("dry_run", True),
            supplied_candidate=state.get("candidate"),
            on_event=state.get("on_event"),
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
    model: str | None = None,
    reasoning_effort: str = "high",
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
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
        "model": model, "reasoning_effort": reasoning_effort,
        "fallback_model": fallback_model, "fallback_reasoning_effort": fallback_reasoning_effort,
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

Validation before pushing:
- Add focused tests for each new module; existing tests must keep passing.
- Lint and format only the changed files (for example `bunx eslint <files>` and
  `bunx prettier --check <files>`), not the whole repository.
- Run the type check when the repository instructions require it.
{audit_line}
Commit in logical steps (one commit per extracted module is fine) and push HEAD to {branch}.
Do not open or merge a pull request."""
