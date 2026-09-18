"""Local on-disk state helpers: SQLite-backed PR-fixer state and artifact cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.runtime import (
    SqliteStateStore,
    legacy_json_path,
    remove_older_than,
    state_database_path,
)

from ._shared import _log


def _state_path(config: PrFixerConfig, project: str) -> Path:
    return state_database_path(
        config.state_path,
        Path.home() / ".local/state/dev-agents" / project / "pr-fixer-state.db",
    )


def _load_state(path: Path) -> dict[str, Any]:
    database_path = state_database_path(path, path)
    value = SqliteStateStore(
        database_path,
        "pr-fixer",
        {"version": 1, "pullRequests": {}},
        legacy_json_path(path, database_path),
    ).load()
    if value.get("version") == 1 and isinstance(value.get("pullRequests"), dict):
        return value
    # Migrate the original {"<pr>": ["comment:..."]} shape.
    migrated = {"version": 1, "pullRequests": value}
    SqliteStateStore(database_path, "pr-fixer", {}).save(migrated)
    return migrated


def _save_state(path: Path, state: dict[str, list[str]]) -> None:
    SqliteStateStore(state_database_path(path, path), "pr-fixer", {}).save(state)


def _cleanup_artifacts(project_name: str, project: ProjectConfig, config: PrFixerConfig) -> None:
    """Bound completed logs and stale temporary worktree directories."""
    log_dir = (
        config.log_dir or Path.home() / ".local/state/dev-agents" / project_name / "logs"
    ).expanduser()
    worktree_root = (config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser()
    removed_logs = remove_older_than(log_dir, "pr-*.log", config.log_retention_days * 86400)
    age_seconds = config.worktree_retention_days * 86400
    # "pr-*" is the PR-fixer's own worktree prefix; "issue-*" is the label-driven
    # issue fixer's -- both share worktree_root, but only "pr-*" was ever swept here.
    removed_worktrees = remove_older_than(
        worktree_root, "pr-*", age_seconds, directories=True
    ) + remove_older_than(worktree_root, "issue-*", age_seconds, directories=True)
    # remove_older_than only deletes the directory; it never updates git's own
    # `.git/worktrees/` registration, so removed entries linger there as "prunable"
    # forever unless something calls `git worktree prune`.
    pruned = subprocess.run(
        ["git", "worktree", "prune"], cwd=project.repo, capture_output=True, text=True, check=False
    )
    if removed_logs or removed_worktrees or pruned.returncode != 0:
        detail = f" prune-error={pruned.stderr.strip()}" if pruned.returncode != 0 else ""
        _log(f"cleanup logs={removed_logs} worktrees={removed_worktrees}{detail}")
