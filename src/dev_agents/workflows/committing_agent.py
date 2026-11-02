"""Run an agent in a fresh branch worktree until it leaves a clean committed change.

Shared by the autonomous workflows that open their own PRs (degodify, persona sweeps): each
creates a branch from the base, lets configured providers try in turn, and only accepts a run
that ends with committed work and a clean tree.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dev_agents.runtime import AgentResult, GitHubError, gh_json, run_agent, run_with_fallback

CONTINUATION_PROMPT = """
The previous attempt did not leave a clean committed change. Continue in this same worktree;
inspect git status and the existing edits before doing anything else. Preserve partial work.
Implement the {task} now, run focused validation, and commit. An inspection summary or
plan is not completion. If blocked, give the exact failing command and actionable blocker.
"""


@contextmanager
def branch_worktree(repo: Path, base_branch: str, branch: str, *, prefix: str) -> Iterator[Path]:
    """Create `branch` from the freshly fetched base in a throwaway worktree, removed on exit."""
    root = repo / ".dev-agents-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    try:
        git(repo, "fetch", "origin", base_branch)
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base_branch}")
        yield worktree
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        # Pushed work lives on the remote; a run that pushed nothing leaves nothing behind.
        git(repo, "branch", "-D", branch, check=False)


def committed_change(worktree: Path, base_branch: str) -> bool:
    """Whether the worktree is clean and HEAD has moved past the base."""
    return git(worktree, "status", "--porcelain") == "" and git(worktree, "rev-parse", "HEAD") != git(
        worktree, "rev-parse", f"origin/{base_branch}"
    )


def run_committing_agent(
    worktree: Path,
    base_branch: str,
    prompt: str,
    *,
    providers: list[str],
    task: str,
    log_path: Path,
    timeout_seconds: float,
    model: str | None = None,
    reasoning_effort: str | None = None,
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
    finished: Callable[[], bool] | None = None,
    after_reset: Callable[[], object] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[str | None, int]:
    """Let providers try in order; return the accepted provider (or None) and the attempt count.

    `finished` decides whether a turn is done; it defaults to a clean committed change. Each
    provider gets one continuation in the same worktree and its own deadline, and every provider
    after the first starts from a clean checkout of the base (`after_reset` then restores anything
    that cleanup removed, such as installed dependencies).
    """
    done = finished or (lambda: committed_change(worktree, base_branch))
    attempts = 0

    def run_provider(name: str) -> AgentResult:
        nonlocal attempts
        if name != providers[0]:
            git(worktree, "reset", "--hard", f"origin/{base_branch}")
            git(worktree, "clean", "-fdx")
            if after_reset is not None:
                after_reset()
        result = AgentResult(1, False)
        # One continuation per provider preserves partial edits and gives an inspection-only
        # turn a chance to finish. Each provider gets its own deadline so a slow primary cannot
        # starve the fallback.
        deadline = time.monotonic() + timeout_seconds
        for turn in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return AgentResult(1, True)
            attempts += 1
            result = run_agent(
                name,
                prompt + ("" if turn == 0 else CONTINUATION_PROMPT.format(task=task)),
                cwd=worktree,
                log_path=log_path,
                timeout_seconds=remaining,
                # `model` and `reasoning_effort` are Codex settings; the fallback ones are Claude's.
                model=model if name == "codex" else fallback_model if name == "claude" else None,
                reasoning_effort=(
                    reasoning_effort if name == "codex" else fallback_reasoning_effort if name == "claude" else None
                ),
            )
            complete = result.returncode == 0 and not result.timed_out and done()
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
        providers, run_provider, lambda _name, result: result.returncode == 0 and not result.timed_out and done()
    )
    return (accepted[0] if accepted else None), attempts


def open_pull_request_with_prefix(repo: Path, prefix: str) -> str | None:
    """Return the URL of an open PR whose head branch starts with `prefix`, if any."""
    try:
        pulls = gh_json(repo, "pr", "list", "--state", "open", "--json", "headRefName,url", "--limit", "200")
    except (GitHubError, TypeError):
        return None
    for pull in pulls if isinstance(pulls, list) else []:
        if str(pull.get("headRefName", "")).startswith(prefix):
            return str(pull.get("url") or pull.get("headRefName"))
    return None


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        result = subprocess.run(("git", *args), cwd=repo, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout:g}s") from error
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def gh(repo: Path, *args: str, timeout: float = 60) -> str:
    try:
        result = subprocess.run(("gh", *args), cwd=repo, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"gh {' '.join(args)} timed out after {timeout:g}s") from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    return result.stdout.strip()
