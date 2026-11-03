"""Run an agent in a fresh branch worktree until it leaves a clean committed change.

Shared by the autonomous workflows that open their own PRs (degodify, persona sweeps): each
creates a branch from the base, lets configured providers try in turn, and only accepts a run
that ends with committed work and a clean tree.

`build_commit_loop` is the shared LangGraph subgraph for that:

    run_agent -> check_change -> push_branch
        ^____________|______________|   (repair: the check or the pre-push hook said no)

A workflow supplies its own check (size limits, audits) and opens its own PR afterwards.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from dev_agents.runtime import AgentResult, GitHubError, gh_json, run_agent, run_with_fallback

CONTINUATION_PROMPT = """
The previous attempt did not leave a clean committed change. Continue in this same worktree;
inspect git status and the existing edits before doing anything else. Preserve partial work.
Implement the {task} now, run focused validation, and commit. An inspection summary or
plan is not completion. If blocked, give the exact failing command and actionable blocker.
"""


MAX_REPAIRS = 2

REPAIR_PROMPT = """

The workflow could not publish your change:

{problem}

Fix this in the same worktree and commit the fix, following this run's commit rules (amend
when the run asks for a single commit). Do not bypass git hooks. If it cannot be fixed within
this run's rules, leave the branch unchanged and explain why.
"""


class TransientCommandError(RuntimeError):
    """A git or gh failure from the network or GitHub, worth retrying unchanged."""


# Network and GitHub-side failures. A pre-push hook rejection never matches, so it is
# repaired by the agent instead of retried.
_TRANSIENT = re.compile(
    r"could not resolve host|connection (?:timed out|reset|refused)|operation timed out|early eof"
    r"|remote end hung up|unable to access|tls handshake|i/o timeout|http (?:5\d\d|429)"
    r"|bad gateway|service unavailable|gateway timeout|secondary rate limit",
    re.IGNORECASE,
)

#: Retries a node whose git or gh call failed transiently; other errors are raised at once.
NETWORK_RETRY = RetryPolicy(max_attempts=3, initial_interval=5.0, retry_on=TransientCommandError)


def add_worktree(repo: Path, base_branch: str, branch: str, worktree: Path) -> None:
    """Create `branch` from the freshly fetched base at `worktree`."""
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "fetch", "origin", base_branch)
    git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base_branch}")


def remove_worktree(repo: Path, worktree: Path, branch: str | None = None) -> None:
    """Remove a run's worktree and local branch (read from the worktree if not given), if it got that far."""
    if not worktree.exists():
        return
    branch = branch or git(worktree, "branch", "--show-current", check=False)
    git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    if not branch:
        return
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


def install_dependencies(worktree: Path) -> bool:
    """Install locked dependencies so validation and fallow see the worktree like CI does."""
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
        raise _command_error(result, "git command failed")
    return result.stdout.strip()


def gh(repo: Path, *args: str, timeout: float = 60) -> str:
    try:
        result = subprocess.run(("gh", *args), cwd=repo, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"gh {' '.join(args)} timed out after {timeout:g}s") from error
    if result.returncode:
        raise _command_error(result, "gh command failed")
    return result.stdout.strip()


def _command_error(result: subprocess.CompletedProcess[str], fallback: str) -> RuntimeError:
    message = result.stderr.strip() or result.stdout.strip() or fallback
    return TransientCommandError(message) if _TRANSIENT.search(message) else RuntimeError(message)


@dataclass(frozen=True)
class Problem:
    """Why a finished change cannot be published: `prompt` is for the agent, `reason` for the result."""

    prompt: str
    reason: str


class CommitLoopState(TypedDict, total=False):
    repo: Path
    worktree: Path
    branch: str
    base_branch: str
    prompt: str
    task: str
    providers: list[str] | None
    log_path: Path
    timeout_seconds: float
    model: str | None
    reasoning_effort: str | None
    fallback_model: str | None
    fallback_reasoning_effort: str | None
    after_reset: Callable[[], object] | None
    on_event: Callable[[str, dict[str, Any]], None] | None
    # With a no-op marker, an unchanged tree plus the marker is a finished turn too.
    noop_path: Path | None
    accepted: str
    attempts: int
    problem: Problem | None
    repairs: int
    commit_outcome: Literal["pushed", "no_change", "failed"]
    commit_reason: str | None


CheckChange = Callable[[Any], "tuple[Problem | None, dict[str, Any]]"]


def build_commit_loop(
    check: CheckChange | None = None, *, state_schema: type[Any] = CommitLoopState, max_repairs: int = MAX_REPAIRS,
) -> Any:
    """Build the shared run-agent, check, push subgraph.

    It ends with `commit_outcome`: "pushed", "no_change" (the agent finished without
    committing, e.g. a no-op) or "failed" with `commit_reason`. `check` sees the committed
    change and returns a Problem to hand back to the agent, plus state updates (such as
    measurements for the PR body). A Problem or a rejected push is repaired by the provider
    that made the change, at most `max_repairs` times. `state_schema` must extend
    CommitLoopState so the parent's keys pass through.
    """
    graph = StateGraph(state_schema)

    def emit(state: CommitLoopState, event: str, payload: dict[str, Any]) -> None:
        if (callback := state.get("on_event")) is not None:
            callback(event, payload)

    def failed(reason: str) -> dict[str, object]:
        return {"commit_outcome": "failed", "commit_reason": reason}

    def agent(state: CommitLoopState) -> dict[str, object]:
        worktree, base = state["worktree"], state["base_branch"]
        problem, noop_path = state.get("problem"), state.get("noop_path")
        options: dict[str, Any] = {
            key: state.get(key) for key in (
                "model", "reasoning_effort", "fallback_model", "fallback_reasoning_effort", "after_reset", "on_event")
        }
        options.update(task=state["task"], log_path=state["log_path"], timeout_seconds=state["timeout_seconds"])
        if problem is None:
            def finished() -> bool:
                return committed_change(worktree, base) or (
                    noop_path is not None and noop_path.is_file()
                    and git(worktree, "rev-parse", "HEAD") == git(worktree, "rev-parse", f"origin/{base}")
                )

            accepted, attempts = run_committing_agent(
                worktree, base, state["prompt"], providers=state.get("providers") or ["codex"],
                finished=finished, **options,
            )
            if accepted is None:
                return {"attempts": attempts, **failed(
                    f"no clean committed change after {attempts} agent attempts; see agent_attempt events and log")}
            return {"accepted": accepted, "attempts": attempts, "repairs": 0}

        # A repair stays with the provider that made the change: a fallback would reset the
        # worktree to the base and discard it.
        before = git(worktree, "rev-parse", "HEAD")

        def repaired() -> bool:
            return committed_change(worktree, base) and git(worktree, "rev-parse", "HEAD") != before

        accepted, attempts = run_committing_agent(
            worktree, base, state["prompt"] + REPAIR_PROMPT.format(problem=problem.prompt),
            providers=[state["accepted"]], finished=repaired, **options,
        )
        emit(state, "repair", {"repair": state.get("repairs", 0), "repaired": accepted is not None})
        update: dict[str, object] = {"attempts": state.get("attempts", 0) + attempts}
        if accepted is None:
            return {**update, **failed(f"{problem.reason} (not repaired after {state.get('repairs', 0)} attempt(s))")}
        return {**update, "problem": None}

    def needs_repair(state: CommitLoopState, problem: Problem) -> dict[str, object]:
        repairs = state.get("repairs", 0)
        if repairs >= max_repairs:
            suffix = f" (still failing after {repairs} repair(s))" if repairs else ""
            return failed(problem.reason + suffix)
        return {"problem": problem, "repairs": repairs + 1}

    def check_change(state: CommitLoopState) -> dict[str, object]:
        if not committed_change(state["worktree"], state["base_branch"]):
            return {"commit_outcome": "no_change", "commit_reason": None}
        problem, update = check(state) if check else (None, {})
        emit(state, "check_change", {"problem": problem.reason if problem else None})
        return {**update, **(needs_repair(state, problem) if problem else {"problem": None})}

    def push(state: CommitLoopState) -> dict[str, object]:
        try:
            git(state["worktree"], "push", "-u", "origin", state["branch"], timeout=900)
        except TransientCommandError:
            raise  # NETWORK_RETRY pushes again
        except RuntimeError as error:
            # Usually the pre-push hook rejected the change; its last lines say why.
            output = "\n".join(str(error).splitlines()[-15:])
            emit(state, "push_branch", {"rejected": True})
            return needs_repair(state, Problem(
                f"`git push` ran the repository's pre-push hook, which rejected it:\n\n{output}",
                f"push rejected: {output}"))
        emit(state, "push_branch", {"rejected": False})
        return {"commit_outcome": "pushed", "commit_reason": None}

    def route(next_node: str) -> Callable[[CommitLoopState], str]:
        def choose(state: CommitLoopState) -> str:
            if state.get("commit_outcome") in {"failed", "no_change"}:
                return END
            return "run_agent" if state.get("problem") else next_node
        return choose

    # LangGraph narrows a node's input to its annotated type; the parent's keys must reach `check`.
    graph.add_node("run_agent", agent, input_schema=state_schema)
    graph.add_node("check_change", check_change, input_schema=state_schema)
    graph.add_node("push_branch", push, input_schema=state_schema, retry_policy=NETWORK_RETRY)
    graph.add_edge(START, "run_agent")
    graph.add_conditional_edges("run_agent", route("check_change"), ["check_change", END])
    graph.add_conditional_edges("check_change", route("push_branch"), ["push_branch", "run_agent", END])
    graph.add_conditional_edges("push_branch", route(END), ["run_agent", END])
    return graph.compile()
