import json
import subprocess
from pathlib import Path

import pytest

from dev_agents.config import IssueFixerConfig, PrFixerConfig, ProjectConfig
from dev_agents.issue_fixer import (
    IssueFixerService,
    _existing_issue_pr,
    _isolated_issue_worktree,
    _issue_result_body,
    _open_bug_issues,
    _pr_number_from_url,
    _pr_result_body,
    _prompt,
    _publish_issue_comment,
)
from dev_agents.runtime import StateRepository


def test_open_bug_issues_keeps_only_open_exact_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = [
        {"number": 1, "state": "OPEN", "labels": [{"name": "bug"}]},
        {"number": 2, "state": "OPEN", "labels": [{"name": "Bug"}]},
        {"number": 3, "state": "CLOSED", "labels": [{"name": "bug"}]},
        {"number": 4, "state": "OPEN", "labels": [{"name": "bugfix"}]},
        {"number": 5, "state": "OPEN", "labels": [{"name": "bug"}, {"name": "paused"}]},
    ]
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run", lambda *_args, **_kwargs: json.dumps(payload)
    )

    issues = _open_bug_issues(tmp_path, IssueFixerConfig())

    assert [issue["number"] for issue in issues] == [1, 2]


def test_existing_issue_pr_uses_hidden_issue_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = iter([json.dumps([
        {"number": 10, "body": "<!-- dev-agents:issue-fix issue=92 -->", "url": "url"},
        {"number": 11, "body": "Fixes #92", "url": "other"},
    ])])
    monkeypatch.setattr("dev_agents.issue_fixer._run", lambda *_args, **_kwargs: next(responses))

    pr = _existing_issue_pr(tmp_path, 92)

    assert pr == {"number": 10, "body": "<!-- dev-agents:issue-fix issue=92 -->", "url": "url"}


def test_issue_result_body_mentions_pr_or_blocker() -> None:
    assert "No PR was created." in _issue_result_body(92, "run-1", None, "blocked")
    assert "https://github.com/example/pr/1" in _issue_result_body(
        92, "run-1", "https://github.com/example/pr/1", "fixed"
    )


def test_pr_number_from_url_extracts_trailing_number() -> None:
    assert _pr_number_from_url("https://github.com/owner/repo/pull/3205") == 3205
    assert _pr_number_from_url("https://github.com/owner/repo/issues/92") is None


def test_pr_result_body_links_the_originating_issue() -> None:
    body = _pr_result_body(92, "run-1", "Implemented and pushed the issue fix.")
    assert "Fixes #92." in body
    assert "Implemented and pushed the issue fix." in body


def test_finalize_posts_result_to_the_pr_when_one_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful run's detailed result must land on the PR it opened, not the issue."""
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService(
        "lear-bear", project, IssueFixerConfig(), PrFixerConfig(), state
    )
    calls: list[tuple[Path, int, str]] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer._publish_issue_comment",
        lambda repo, number, body: calls.append((repo, number, body)) or True,
    )
    state.claim_run("issue-fixer", "issue-fix-92-abc123", metadata={"issue": 92})

    result = service._finalize(
        {
            "number": 92,
            "run_id": "issue-fix-92-abc123",
            "claimed": True,
            "skip": False,
            "fixed": True,
            "pr_url": "https://github.com/owner/repo/pull/3205",
            "summary": "Implemented and pushed the issue fix.",
            "validation": "exit=0",
        }
    )

    assert result["fixed"] is True
    assert len(calls) == 1
    _, posted_number, posted_body = calls[0]
    assert posted_number == 3205
    assert "Fixes #92." in posted_body


def test_finalize_posts_failure_to_the_issue_when_no_pr_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService(
        "lear-bear", project, IssueFixerConfig(), PrFixerConfig(), state
    )
    calls: list[tuple[Path, int, str]] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer._publish_issue_comment",
        lambda repo, number, body: calls.append((repo, number, body)) or True,
    )
    state.claim_run("issue-fixer", "issue-fix-92-abc123", metadata={"issue": 92})

    service._finalize(
        {
            "number": 92,
            "run_id": "issue-fix-92-abc123",
            "claimed": True,
            "skip": False,
            "fixed": False,
            "pr_url": None,
            "summary": "Agent did not produce a clean pushed fix.",
            "validation": "exit=1",
        }
    )

    assert len(calls) == 1
    _, posted_number, _ = calls[0]
    assert posted_number == 92


def test_publish_issue_comment_posts_new_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue updates must not overwrite an existing comment: always POST new."""
    calls: list[tuple[str, ...]] = []

    def fake_run(_repo: Path, *args: str, **_kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr("dev_agents.issue_fixer.repository_slug", lambda _repo: "owner/repo")
    monkeypatch.setattr("dev_agents.issue_fixer._run", fake_run)

    assert _publish_issue_comment(tmp_path, 92, "<!-- marker -->\nhello") is True
    assert calls == [
        ("gh", "api", "repos/owner/repo/issues/92/comments", "-f", "body=<!-- marker -->\nhello")
    ]


def test_prompt_defaults_to_fix_framing(tmp_path: Path) -> None:
    prompt = _prompt(
        tmp_path,
        {"number": 42, "title": "Button misaligned", "url": "https://x/42"},
        "main",
        "dev-agents/issue-42",
        [],
    )
    assert prompt.startswith("Fix GitHub issue #42")
    assert "implement the smallest complete fix" in prompt
    assert "Commit the fix and push" in prompt


def test_prompt_uses_enhancement_framing_for_gui_fix_kind(tmp_path: Path) -> None:
    prompt = _prompt(
        tmp_path,
        {"number": 42, "title": "Add dark mode toggle", "url": "https://x/42"},
        "main",
        "dev-agents/issue-42",
        [],
        kind="enhancement",
    )
    assert prompt.startswith("Implement the GUI enhancement described in GitHub issue #42")
    assert "implement the smallest complete version of the enhancement" in prompt
    assert "Commit the enhancement and push" in prompt
    # Must not carry over "fix"-specific wording that would misdescribe an
    # enhancement as a defect.
    assert "the concrete blocker" in prompt  # still applies: enhancement can still be blocked
    assert "what was fixed" not in prompt


def test_issue_fixer_config_kind_defaults_to_fix() -> None:
    assert IssueFixerConfig().kind == "fix"
    assert IssueFixerConfig(label="gui-fix", kind="enhancement").kind == "enhancement"


def test_collect_issue_claims_labeled_open_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService(
        "lear-bear", project, IssueFixerConfig(), PrFixerConfig(), state
    )
    issue = {
        "number": 92,
        "state": "OPEN",
        "title": "Security fix",
        "body": "Fix it",
        "labels": [{"name": "bug"}],
        "updatedAt": "2026-09-16T12:00:00Z",
    }
    monkeypatch.setattr("dev_agents.issue_fixer._issue", lambda *_args: issue)
    monkeypatch.setattr("dev_agents.issue_fixer._existing_issue_pr", lambda *_args: None)

    result = service._collect_issue({"number": 92})

    assert result["claimed"] is True
    assert result["branch"] == "dev-agents/issue-92"
    assert state.list_runs("issue-fixer")[0].metadata["issue"] == 92


def test_collect_issue_skips_paused_bug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService(
        "lear-bear", project, IssueFixerConfig(), PrFixerConfig(), state
    )
    issue = {
        "number": 92,
        "state": "OPEN",
        "title": "Paused bug",
        "body": "Wait",
        "labels": [{"name": "bug"}, {"name": "paused"}],
    }
    monkeypatch.setattr("dev_agents.issue_fixer._issue", lambda *_args: issue)

    result = service._collect_issue({"number": 92})

    assert result["skip"] is True
    assert state.list_runs("issue-fixer") == []


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone_with_origin(tmp_path: Path) -> Path:
    """A clone of a bare origin whose `main` has one commit."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    for key, value in (("user.email", "t@example.com"), ("user.name", "Test")):
        _git(clone, "config", key, value)
    (clone / "README.md").write_text("base\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "-u", "origin", "main")
    return clone


def test_worktree_replaces_a_stale_empty_branch_instead_of_failing(tmp_path: Path) -> None:
    """An aborted attempt leaves its empty local branch; every retry used to die on
    `fatal: a branch named ... already exists`, which is what looped issues 337/338."""
    clone = _clone_with_origin(tmp_path)
    _git(clone, "branch", "dev-agents/gui-1")

    with _isolated_issue_worktree(clone, tmp_path / "wt", "dev-agents/gui-1", "main") as (
        worktree,
        conflicts,
    ):
        assert conflicts == []
        assert _git(worktree, "branch", "--show-current") == "dev-agents/gui-1"

    # Nothing was pushed and nothing committed, so no leftover branch is kept for the next try.
    assert _git(clone, "branch", "--list", "dev-agents/gui-1") == ""


def test_worktree_continues_from_a_local_branch_that_has_unpushed_work(tmp_path: Path) -> None:
    clone = _clone_with_origin(tmp_path)
    _git(clone, "switch", "-c", "dev-agents/gui-2")
    (clone / "work.txt").write_text("unpushed\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "unpushed work")
    _git(clone, "switch", "main")

    with _isolated_issue_worktree(clone, tmp_path / "wt", "dev-agents/gui-2", "main") as (
        worktree,
        _conflicts,
    ):
        assert (worktree / "work.txt").read_text(encoding="utf-8") == "unpushed\n"

    assert _git(clone, "branch", "--list", "dev-agents/gui-2") != ""


def _service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[IssueFixerService, list[tuple[int, str]], list[tuple[str, ...]]]:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService(
        "lear-bear", project, IssueFixerConfig(), PrFixerConfig(max_consecutive_failures=3), state
    )
    state.claim_run("issue-fixer", "issue-fix-92-abc123", metadata={"issue": 92})
    comments: list[tuple[int, str]] = []
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer._publish_issue_comment",
        lambda repo, number, body: comments.append((number, body)) or True,
    )
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run", lambda repo, *args, **kwargs: commands.append(args) or ""
    )
    return service, comments, commands


def _failed_state(attempt: int) -> dict[str, object]:
    return {
        "number": 92,
        "run_id": "issue-fix-92-abc123",
        "claimed": True,
        "skip": False,
        "fixed": False,
        "pr_url": None,
        "summary": "Issue fixer blocked: boom",
        "validation": "exit=1",
        "attempt": attempt,
    }


def test_repeated_failures_pause_the_issue_after_the_configured_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, comments, commands = _service(monkeypatch, tmp_path)

    service._finalize(_failed_state(1))  # type: ignore[arg-type]
    assert [n for n, _ in comments] == [92]
    assert "auto-paused" not in comments[0][1]
    assert not any("--add-label" in command for command in commands)

    comments.clear()
    service._finalize(_failed_state(2))  # type: ignore[arg-type]
    assert comments == []  # a repeat failure is not re-posted
    assert not any("--add-label" in command for command in commands)

    service._finalize(_failed_state(3))  # type: ignore[arg-type]
    assert ("gh", "issue", "edit", "92", "--add-label", "paused") in commands
    assert len(comments) == 1
    assert "auto-paused" in comments[0][1]
    assert "failed 3 consecutive" in comments[0][1]


def test_pause_creates_the_label_when_the_repository_lacks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, comments, _ = _service(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []
    added = 0

    def fake_run(repo: Path, *args: str, **kwargs: object) -> str:
        nonlocal added
        commands.append(args)
        if args[:3] == ("gh", "issue", "edit"):
            added += 1
            if added == 1:
                raise RuntimeError("could not add label: 'paused' not found")
        return ""

    monkeypatch.setattr("dev_agents.issue_fixer._run", fake_run)

    service._finalize(_failed_state(3))  # type: ignore[arg-type]

    assert ("gh", "label", "create", "paused") == commands[1][:4]
    assert added == 2
    assert "auto-paused" in comments[0][1]


def test_a_retry_does_not_repost_the_started_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, comments, _ = _service(monkeypatch, tmp_path)

    def blocked(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("fatal: a branch named 'x' already exists")

    monkeypatch.setattr("dev_agents.issue_fixer._isolated_issue_worktree", blocked)
    base = {
        "number": 92,
        "run_id": "issue-fix-92-abc123",
        "claimed": True,
        "skip": False,
        "issue": {"title": "T"},
        "branch": "dev-agents/gui-92",
    }

    service._remediate({**base, "attempt": 1})  # type: ignore[arg-type]
    assert len(comments) == 1 and "started" in comments[0][1]

    comments.clear()
    service._remediate({**base, "attempt": 7})  # type: ignore[arg-type]
    assert comments == []
