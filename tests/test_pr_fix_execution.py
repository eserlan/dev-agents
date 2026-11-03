"""Characterisation tests for PrFixerService._fix in fix mode (review_only=False).

They pin the current behaviour of the provider pass, so it can be moved onto graph edges
without changing what reaches GitHub: what gets pushed, when the fallback provider runs, and
when a concurrent push marks the run superseded.
"""

import subprocess
from pathlib import Path

import pytest

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.pr_fixer.service import PrFixerService
from dev_agents.runtime.agent import AgentResult

BRANCH = "feature/x"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


def setup_pr(tmp_path: Path, *, behind_base: bool) -> tuple[Path, Path, str]:
    """An origin with PR branch feature/x (optionally behind staging) and a clone of it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "staging")
    git(origin, "config", "user.email", "test@example.test")
    git(origin, "config", "user.name", "Test")
    (origin / "README.md").write_text("base\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    git(origin, "checkout", "-b", BRANCH)
    (origin / "feature.txt").write_text("feature\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "feature")
    head = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "staging")
    if behind_base:
        # The worktree setup then merges staging in: a clean housekeeping commit.
        (origin / "unrelated.md").write_text("later base change\n")
        git(origin, "add", ".")
        git(origin, "commit", "-m", "unrelated base change")
    git(origin, "config", "receive.denyCurrentBranch", "ignore")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    for checkout in (repo,):
        git(checkout, "config", "user.email", "test@example.test")
        git(checkout, "config", "user.name", "Test")
    return origin, repo, head


def commit_fix(worktree: Path, *, push: bool) -> None:
    git(worktree, "config", "user.email", "test@example.test")
    git(worktree, "config", "user.name", "Test")
    (worktree / "feature.txt").write_text("fixed\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "fix")
    if push:
        git(worktree, "push", "origin", f"HEAD:{BRANCH}")


def external_push(origin: Path) -> str:
    git(origin, "checkout", BRANCH)
    (origin / "external.txt").write_text("someone else\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "external change")
    head = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "staging")
    return head


# scenario: (behind_base, codex behaviour, claude behaviour, expected calls, succeeded)
SCENARIOS = {
    "pushed": (True, "commit_push", None, ["codex"], True),
    "unpushed_commit": (True, "commit", None, ["codex"], True),
    "sync_only": (True, "nothing", None, ["codex"], True),
    "nothing_up_to_date": (False, "nothing", None, ["codex"], True),
    "dirty": (True, "dirty", None, ["codex"], False),
    "codex_fails_untouched": (True, "fail", "commit_push", ["codex", "claude"], True),
    "codex_fails_touched": (True, "fail_dirty", "commit_push", ["codex"], False),
    "codex_timeout": (True, "timeout", "commit_push", ["codex"], False),
    "superseded": (True, "external_then_commit", "commit_push", ["codex"], False),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_fix_mode_provider_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    behind_base, codex, claude, expected_calls, expected_success = SCENARIOS[scenario]
    origin, repo, head = setup_pr(tmp_path, behind_base=behind_base)
    calls: list[str] = []
    external: list[str] = []

    def agent(provider: str, prompt: str, **kwargs) -> AgentResult:
        calls.append(provider)
        assert "Fix actionable GitHub feedback for PR #42" in prompt
        worktree: Path = kwargs["cwd"]
        behaviour = codex if provider == "codex" else claude
        if behaviour == "commit_push":
            commit_fix(worktree, push=True)
        elif behaviour == "commit":
            commit_fix(worktree, push=False)
        elif behaviour == "dirty":
            (worktree / "feature.txt").write_text("uncommitted\n")
        elif behaviour == "fail":
            return AgentResult(1, False)
        elif behaviour == "fail_dirty":
            (worktree / "feature.txt").write_text("partial\n")
            return AgentResult(1, False)
        elif behaviour == "timeout":
            return AgentResult(1, True)
        elif behaviour == "external_then_commit":
            external.append(external_push(origin))
            commit_fix(worktree, push=False)
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.pr_fixer.service.run_agent", agent)
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(
        state_path=tmp_path / "state.db", worktree_dir=tmp_path / "worktrees", log_dir=tmp_path / "logs",
        base_branch="staging", fallback_provider="claude", fallback_model="claude-sonnet-5-5",
    ))

    succeeded, report = service._fix(42, BRANCH, ["review:1"], meta={"headRefOid": head})

    assert calls == expected_calls
    assert succeeded is expected_success
    assert report["agent_provider"] == expected_calls[-1]
    remote = git(origin, "rev-parse", BRANCH)
    if scenario == "superseded":
        # A concurrent push wins: nothing of ours is pushed and the run is not a failure of ours.
        assert report["superseded_by"] == external[0] == remote
        return
    assert "superseded_by" not in report
    if scenario in {"dirty", "codex_fails_touched", "codex_timeout"}:
        assert remote == head
        return
    if scenario == "nothing_up_to_date":
        assert remote == head and report["own_commits"] == []
        return
    # Every successful pass leaves origin at the worktree's head, as a fast-forward of the PR.
    assert remote != head
    git(origin, "merge-base", "--is-ancestor", head, remote)
    assert remote in report["own_commits"]
    if scenario == "sync_only":
        # The base merge made during setup is pushed even though the agent changed nothing.
        assert git(origin, "show", "-s", "--format=%P", remote).count(" ") == 1
        assert git(origin, "show", f"{remote}:feature.txt") == "feature"
    else:
        assert git(origin, "show", f"{remote}:feature.txt") == "fixed"
    # The worktree is gone afterwards.
    assert not service.active_worktrees
