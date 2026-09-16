import json
from pathlib import Path

import pytest

from dev_agents.config import IssueFixerConfig, PrFixerConfig, ProjectConfig
from dev_agents.issue_fixer import (
    IssueFixerService,
    _existing_issue_pr,
    _issue_result_body,
    _open_bug_issues,
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
