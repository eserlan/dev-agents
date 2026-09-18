"""Safe isolated worktree lifecycle for agent workflows."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, Semaphore

_repo_locks: dict[str, Lock] = {}
_repo_locks_guard = Lock()
_repo_semaphores: dict[str, Semaphore] = {}
_repo_semaphores_guard = Lock()


def repo_git_lock(repo: Path) -> Lock:
    """Return the process-wide lock serializing git ref-mutating ops for one repo.

    `git fetch`/`worktree add`/`worktree remove` mutate the same on-disk `.git`
    directory (ref updates, worktree metadata) when run against the shared
    checkout rather than a fully independent clone. Two of these running
    concurrently -- e.g. a PR review and an issue fix racing on the same
    project -- can fail with "cannot lock ref ...: is at X but expected Y" as
    one fetch's ref compare-and-swap loses the race against the other's.
    Callers touching the same `repo` must hold this same lock instance, so it
    is keyed by resolved path and shared across every caller in this process
    (isolated_worktree here and issue_fixer's own worktree setup alike).
    """
    key = str(repo.resolve())
    with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = Lock()
            _repo_locks[key] = lock
        return lock


def repo_worktree_semaphore(repo: Path, max_concurrent: int) -> Semaphore:
    """Return the process-wide semaphore capping concurrent worktree runs for one repo.

    Each active worktree typically runs a full validation build (type check,
    lint, tests) alongside the agent itself; several of these running at once
    against the same project can oversubscribe the machine's CPU well past its
    core count, since PR reviews, PR fixes, and issue fixes all draw from the
    same pool. Keyed and shared the same way as `repo_git_lock`: the first
    caller for a given repo fixes the limit for the process's lifetime, so PR-
    fixer and issue-fixer must be configured with the same value to get one
    combined cap rather than two independent ones that together double it.
    """
    key = str(repo.resolve())
    with _repo_semaphores_guard:
        semaphore = _repo_semaphores.get(key)
        if semaphore is None:
            semaphore = Semaphore(max(1, max_concurrent))
            _repo_semaphores[key] = semaphore
        return semaphore


def _run(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            args, cwd=repo, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout:g}s") from error
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


@contextmanager
def isolated_worktree(
    repo: Path,
    root: Path,
    branch: str,
    base_branch: str,
    *,
    max_concurrent: int = 2,
) -> Iterator[tuple[Path, list[str]]]:
    """Yield a detached branch worktree and merge-base conflict paths.

    The worktree is always force-removed on exit, including agent failures.
    Blocks until fewer than `max_concurrent` worktrees are active for this repo
    (see `repo_worktree_semaphore`) -- held for the whole context, since the
    caller's agent run and validation build happen while still inside it.
    """
    root.mkdir(parents=True, exist_ok=True)
    semaphore = repo_worktree_semaphore(repo, max_concurrent)
    semaphore.acquire()
    try:
        worktree = Path(tempfile.mkdtemp(prefix="pr-", dir=root))
        lock = repo_git_lock(repo)
        try:
            with lock:
                # A bare `git fetch origin <branch>` only populates FETCH_HEAD; it
                # does not update refs/remotes/origin/<branch> unless that ref is
                # already covered by the repo's configured remote.origin.fetch
                # refspec. Confirmed live: a single-branch clone (fetch refspec
                # covering only the default branch) left origin/{branch} pointing
                # two pushes behind the actual remote tip after a "successful"
                # fetch. Everything downstream -- this worktree checkout, and
                # later comparisons against origin/{branch} -- depends on that ref
                # being current, so force it with an explicit refspec rather than
                # trusting the configured one to cover this branch.
                _run(
                    repo,
                    "git",
                    "fetch",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                    f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
                )
                _run(repo, "git", "worktree", "add", "--detach", str(worktree), f"origin/{branch}")
            # rerere is for interactive, iterative human conflict resolution; it has
            # no business in a one-shot automated worktree merge. Left enabled
            # (rerere.enabled=true is set globally on this machine), it replays a
            # stale cached resolution from an unrelated earlier conflict, leaving
            # the merge command exiting non-zero with no unmerged paths left to
            # report -- a state this code cannot distinguish from a real failure.
            merge = subprocess.run(
                ["git", "-c", "rerere.enabled=false", "merge", f"origin/{base_branch}", "--no-edit"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            conflicts = _run(
                worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False
            ).splitlines()
            if merge.returncode != 0 and not conflicts:
                raise RuntimeError(f"merge failed for base branch {base_branch}: {merge.stderr.strip()}")
            yield worktree, conflicts
        finally:
            with lock:
                _run(repo, "git", "worktree", "remove", "--force", str(worktree), check=False)
    finally:
        semaphore.release()
