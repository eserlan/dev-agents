"""Read-only Git repository inspection."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class RepositoryError(ValueError):
    """A directory cannot be inspected as the requested Git repository."""


class RepositoryContext(BaseModel):
    """The Git facts workflows need about a target checkout."""

    model_config = ConfigDict(frozen=True)

    repository_path: Path
    branch: str
    head_sha: str
    remote_url: str | None
    remote_identity: str | None
    status: list[str]
    changed_files: list[str]
    commit_range: str | None = None

    @property
    def is_clean(self) -> bool:
        return not self.status


def _git(repository: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RepositoryError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.strip()


def _remote_identity(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    match = re.search(r"(?:github\.com[/:])([^/]+/[^/#]+?)(?:\.git)?$", remote_url)
    return match.group(1) if match else None


def load_repository_context(repository_path: Path, commit_range: str | None = None) -> RepositoryContext:
    """Inspect a local checkout using only read-only Git commands."""
    repository = repository_path.expanduser().resolve()
    if not repository.is_dir():
        raise RepositoryError(f"Repository path does not exist: {repository}")
    if _git(repository, "rev-parse", "--is-inside-work-tree") != "true":
        raise RepositoryError(f"Not a Git work tree: {repository}")

    branch = _git(repository, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or "HEAD"
    remote_url = _git(repository, "remote", "get-url", "origin", check=False) or None
    status = _git(repository, "status", "--porcelain=v1").splitlines()
    changed_files = (
        _git(repository, "diff", "--name-only", commit_range).splitlines() if commit_range else []
    )

    return RepositoryContext(
        repository_path=repository,
        branch=branch,
        head_sha=_git(repository, "rev-parse", "HEAD"),
        remote_url=remote_url,
        remote_identity=_remote_identity(remote_url),
        status=status,
        changed_files=changed_files,
        commit_range=commit_range,
    )
