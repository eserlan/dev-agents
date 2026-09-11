"""Small, consistent wrapper around the GitHub CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class GitHubError(RuntimeError):
    """Raised when a GitHub CLI operation fails."""


def gh(repo: Path, *args: str, check: bool = True) -> str:
    """Run ``gh`` in a repository and return trimmed stdout."""
    result = subprocess.run(("gh", *args), cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise GitHubError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    return result.stdout.strip()


def gh_json(repo: Path, *args: str) -> Any:
    """Run ``gh`` and decode its JSON response."""
    try:
        return json.loads(gh(repo, *args))
    except json.JSONDecodeError as error:
        raise GitHubError(f"invalid JSON from gh: {error}") from error


def repository_slug(repo: Path) -> str:
    """Return owner/name for the repository containing ``repo``."""
    return str(gh(repo, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"))
