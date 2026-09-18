import os
import shutil
import subprocess
import time
from pathlib import Path

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.pr_fixer import _cleanup_artifacts


def _make_repo(path: Path) -> None:
    path.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _age(path: Path, seconds: float) -> None:
    stale = time.time() - seconds
    os.utime(path, (stale, stale))


def test_cleanup_artifacts_removes_stale_issue_and_pr_worktrees(tmp_path: Path) -> None:
    """The retention glob only ever matched 'pr-*'; the issue fixer's own 'issue-*'
    worktrees shared the same directory but were never swept, so they accumulated
    forever regardless of worktree_retention_days."""
    repo_path = tmp_path / "repo"
    _make_repo(repo_path)
    worktree_dir = tmp_path / "worktrees"
    worktree_dir.mkdir()
    stale_pr = worktree_dir / "pr-stale"
    stale_issue = worktree_dir / "issue-stale"
    fresh_issue = worktree_dir / "issue-fresh"
    for directory in (stale_pr, stale_issue, fresh_issue):
        directory.mkdir()
    _age(stale_pr, 10 * 86400)
    _age(stale_issue, 10 * 86400)

    project = ProjectConfig(repo=repo_path, github="owner/repo")
    config = PrFixerConfig(worktree_dir=worktree_dir, worktree_retention_days=2)
    _cleanup_artifacts("demo", project, config)

    assert not stale_pr.exists()
    assert not stale_issue.exists()
    assert fresh_issue.exists()


def test_cleanup_artifacts_prunes_stale_git_worktree_registrations(tmp_path: Path) -> None:
    """remove_older_than deletes the directory but never tells git, so a worktree
    registration for a directory that no longer exists lingers as 'prunable'
    forever unless `git worktree prune` runs."""
    repo_path = tmp_path / "repo"
    _make_repo(repo_path)
    worktree_dir = tmp_path / "worktrees"
    worktree_dir.mkdir()
    worktree_path = worktree_dir / "pr-orphaned"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_path)],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    # Simulate what remove_older_than already did in production: delete the
    # directory without deregistering it from git.
    shutil.rmtree(worktree_path)
    before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo_path, capture_output=True, text=True, check=True
    ).stdout
    assert str(worktree_path) in before

    project = ProjectConfig(repo=repo_path, github="owner/repo")
    config = PrFixerConfig(worktree_dir=worktree_dir)
    _cleanup_artifacts("demo", project, config)

    after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo_path, capture_output=True, text=True, check=True
    ).stdout
    assert str(worktree_path) not in after
