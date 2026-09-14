"""Safe isolated worktree lifecycle for agent workflows."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


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
) -> Iterator[tuple[Path, list[str]]]:
    """Yield a detached branch worktree and merge-base conflict paths.

    The worktree is always force-removed on exit, including agent failures.
    """
    root.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="pr-", dir=root))
    try:
        _run(repo, "git", "fetch", "origin", branch, base_branch)
        _run(repo, "git", "worktree", "add", "--detach", str(worktree), f"origin/{branch}")
        merge = subprocess.run(
            ["git", "merge", f"origin/{base_branch}", "--no-edit"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        conflicts = _run(worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
        if merge.returncode != 0 and not conflicts:
            raise RuntimeError(f"merge failed for base branch {base_branch}: {merge.stderr.strip()}")
        yield worktree, conflicts
    finally:
        _run(repo, "git", "worktree", "remove", "--force", str(worktree), check=False)
