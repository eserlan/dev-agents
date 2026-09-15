from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.pr_fixer import (
    PrFixerService,
    _agent_report,
    _fix_summary_body,
    _normalise_review_report,
    _publish_fix_summary,
    _pull_request_checks,
    _review_is_due,
    _review_prompt,
    _review_skill,
    _review_started_body,
    _target_validation_instructions,
)
from dev_agents.runtime import StateRepository


def test_no_checks_reported_is_treated_as_empty(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        assert args == (
            "gh",
            "pr",
            "checks",
            "42",
            "--json",
            "name,state,bucket,workflow,link",
        )
        raise RuntimeError("no checks reported on the 'feature/no-checks' branch")

    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)

    assert _pull_request_checks(repo, 42) == []


def test_check_query_errors_are_not_suppressed(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "dev_agents.pr_fixer._run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("authentication failed")),
    )

    with pytest.raises(RuntimeError, match="authentication failed"):
        _pull_request_checks(repo, 42)


def test_agent_report_extracts_findings_and_fixes(tmp_path: Path) -> None:
    log = tmp_path / "review.log"
    log.write_text(
        "before\n"
        "DEV_AGENTS_REVIEW_REPORT_BEGIN\n"
        "FINDINGS: Remix loading could apply a stale response.\n"
        "FIXES: Added a version guard and focused tests.\n"
        'REPORT_JSON: {"verdict":"findings","findings":[{"severity":"MEDIUM","category":"async","location":"src/routes.ts:42","impact":"A stale response can overwrite newer state.","remediation":"Add a request version guard."}],"categories_checked":["async","tests"],"validation":["bun run test:changed"],"fixes":[{"location":"src/routes.ts:42","summary":"Added a request version guard."}]}\n'
        "DEV_AGENTS_REVIEW_REPORT_END\n",
        encoding="utf-8",
    )

    report = _agent_report(log)
    assert report["findings"] == "Remix loading could apply a stale response."
    assert report["fixes"] == "Added a version guard and focused tests."
    assert report["report_valid"] is True
    assert report["review_report"]["verdict"] == "findings"
    assert report["review_report"]["findings"][0]["severity"] == "MEDIUM"


def test_agent_report_marks_missing_or_invalid_structured_result(tmp_path: Path) -> None:
    log = tmp_path / "review.log"
    log.write_text(
        "DEV_AGENTS_REVIEW_REPORT_BEGIN\n"
        "FINDINGS: none\n"
        "FIXES: none\n"
        "REPORT_JSON: not-json\n"
        "DEV_AGENTS_REVIEW_REPORT_END\n",
        encoding="utf-8",
    )

    report = _agent_report(log)

    assert report["report_valid"] is False
    assert report["report_error"] == "REPORT_JSON was not valid JSON"


def test_normalise_review_report_rejects_incomplete_findings() -> None:
    assert _normalise_review_report(
        {
            "verdict": "findings",
            "findings": [{"severity": "HIGH"}],
            "categories_checked": [],
            "validation": [],
            "fixes": [],
        }
    ) is None


def test_review_skill_prefers_canonical_path(tmp_path: Path) -> None:
    canonical = tmp_path / ".agent/skills/codex-review/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical review skill", encoding="utf-8")
    fallback = tmp_path / ".codex/skills/codex-review/SKILL.md"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("stale adapter", encoding="utf-8")

    assert _review_skill(tmp_path) == (
        ".agent/skills/codex-review/SKILL.md",
        "canonical review skill",
    )


def test_review_prompt_requires_lifecycle_comments(tmp_path: Path) -> None:
    prompt = _review_prompt(
        tmp_path,
        42,
        {"headRefOid": "abc123", "headRefName": "feature/review-me"},
        "staging",
        [],
        run_id="pr-review-42-abc123",
    )

    assert "pr-review-findings run=pr-review-42-abc123" in prompt
    assert "pr-review-fixes-started run=pr-review-42-abc123" in prompt
    assert "DEV_AGENTS_REVIEW_REPORT_BEGIN" in prompt


def test_target_validation_uses_codex_cryptica_affected_scripts(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("affected-workspaces.mjs", "lint-changed.mjs", "test-changed.mjs"):
        (scripts / name).write_text("", encoding="utf-8")

    instructions = _target_validation_instructions(tmp_path, "staging")

    assert "scripts/affected-workspaces.mjs" in instructions
    assert "scripts/lint-changed.mjs --base \"$BASE_SHA\" --head HEAD" in instructions
    assert "scripts/test-changed.mjs --base \"$BASE_SHA\" --head HEAD" in instructions
    assert "in parallel" in instructions
    assert "repository-wide" in instructions


def test_target_validation_falls_back_without_repository_scripts(tmp_path: Path) -> None:
    instructions = _target_validation_instructions(tmp_path, "staging")

    assert "changed files" in instructions
    assert "repository-wide lint or test" in instructions


def test_pr_fixer_claims_same_feedback_once_across_services(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    metadata = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "abc123",
    }
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, ["check:1"]))

    first = PrFixerService("demo", project, config)
    second = PrFixerService("demo", project, config)

    first_state = first._collect_feedback({"number": 42})
    second_state = second._collect_feedback({"number": 42})

    assert first_state["claimed"] is True
    assert first_state["unseen"] == ["check:1"]
    assert second_state["claimed"] is False
    assert second_state["skip"] is True
    record = StateRepository(database, "demo", repo).load_run("pr-fixer", first_state["run_id"])
    assert record is not None
    assert record.status == "running"


def test_pr_fixer_starts_internal_review_for_green_pr_without_copilot(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefName": "feature/review-me",
        "headRefOid": "abc123",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "labels": [],
        "reviews": [],
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    state = PrFixerService("demo", project, config)._collect_feedback({"number": 42})

    assert state["review_only"] is True
    assert state["workflow"] == "pr-review"
    assert state["run_id"] == "pr-review-42-abc123"
    assert "GENERAL DEFECT REVIEW" in _review_prompt(repo, 42, metadata, "staging", [])
    assert ".agent/skills/codex-review/SKILL.md" in _review_prompt(
        repo, 42, metadata, "staging", []
    )
    assert "REPORT_JSON" in _review_prompt(repo, 42, metadata, "staging", [])
    record = StateRepository(database, "demo", repo).load_run("pr-review", state["run_id"])
    assert record is not None
    assert record.status == "running"


def test_external_agent_commit_pauses_pr_automation(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=tmp_path / "state.db")
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefOid": "jules-sha",
        "state": "OPEN",
        "isDraft": False,
        "labels": [],
        "commits": [{"authors": [{"login": "google-labs-jules[bot]"}]}],
    }
    published: list[tuple[Any, ...]] = []
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))
    monkeypatch.setattr(
        "dev_agents.pr_fixer._publish_pr_run_comment",
        lambda *args: published.append(args) or True,
    )

    state = PrFixerService("demo", project, config)._collect_feedback({"number": 42})

    assert state["skip"] is True
    assert state["skip_reason"] == "external-agent-commit"
    assert state["external_agent"] == "google-labs-jules[bot]"
    assert published[0][2:4] == ("pr-review-pause-42-jules-sha", "external-agent-paused")


def test_external_agent_pause_can_be_explicitly_resumed(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=tmp_path / "state.db")
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefName": "feature/review-me",
        "headRefOid": "jules-sha",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "labels": [{"name": "dev-agents-resume"}],
        "commits": [{"authors": [{"login": "google-labs-jules[bot]"}]}],
        "reviews": [],
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    state = PrFixerService("demo", project, config)._collect_feedback({"number": 42})

    assert state["skip"] is False
    assert state["review_only"] is True


def test_internal_review_uses_one_targeted_follow_up_after_luna_fix(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    old_meta: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefName": "feature/review-me",
        "headRefOid": "old-sha",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "labels": [],
        "reviews": [],
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    initial_run = "pr-review-42-old-sha"
    state = StateRepository(database, "demo", repo)
    state.claim_run(
        "pr-review",
        initial_run,
        metadata={
            "pull_request": 42,
            "head_sha": "old-sha",
            "review_round": 0,
            "review_chain_id": initial_run,
        },
    )
    state.complete_run(
        "pr-review",
        initial_run,
        metadata={
            "reviewed_head_sha": "old-sha",
            "review_fix_pushed": True,
            "review_follow_up_pending": True,
            "review_follow_up_head_sha": "new-sha",
            "review_report": {
                "verdict": "findings",
                "findings": [],
                "categories_checked": ["async"],
                "validation": ["bun test"],
                "fixes": [],
            },
        },
    )
    new_meta = {**old_meta, "headRefOid": "new-sha"}
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (new_meta, []))

    service = PrFixerService("demo", project, config)
    follow_up = service._collect_feedback({"number": 42})

    assert follow_up["review_only"] is True
    assert follow_up["review_round"] == 1
    assert follow_up["review_scope"] == "targeted-post-fix"
    assert follow_up["review_parent_run_id"] == initial_run
    assert follow_up["review_parent_sha"] == "old-sha"
    prompt = _review_prompt(
        repo,
        42,
        new_meta,
        "staging",
        [],
        review_round=1,
        parent_sha="old-sha",
        prior_report=follow_up["review_prior_report"],
    )
    assert "targeted post-fix verification" in prompt
    assert "GENERAL DEFECT REVIEW" not in prompt
    assert "old-sha" in prompt


def test_internal_review_stops_after_targeted_follow_up_fix(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    follow_up_run = "pr-review-42-follow-up-sha"
    state = StateRepository(database, "demo", repo)
    state.claim_run(
        "pr-review",
        follow_up_run,
        metadata={
            "pull_request": 42,
            "head_sha": "follow-up-sha",
            "review_round": 1,
            "review_scope": "targeted-post-fix",
            "review_chain_id": "pr-review-42-old-sha",
        },
    )
    state.complete_run(
        "pr-review",
        follow_up_run,
        metadata={
            "reviewed_head_sha": "follow-up-sha",
            "review_fix_pushed": True,
            "review_exhausted_head_sha": "final-sha",
        },
    )
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefName": "feature/review-me",
        "headRefOid": "final-sha",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "labels": [],
        "reviews": [],
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    service = PrFixerService("demo", project, config)
    capped = service._collect_feedback({"number": 42})

    assert capped["review_only"] is False
    assert capped["workflow"] == "pr-fixer"

    metadata["headRefOid"] = "later-user-commit"
    next_review = service._collect_feedback({"number": 42})
    assert next_review["review_only"] is True
    assert next_review["review_round"] == 0


def test_internal_review_stops_after_clean_targeted_follow_up(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    run_id = "pr-review-42-follow-up-sha"
    state = StateRepository(database, "demo", repo)
    state.claim_run(
        "pr-review",
        run_id,
        metadata={
            "pull_request": 42,
            "head_sha": "follow-up-sha",
            "review_round": 1,
            "review_scope": "targeted-post-fix",
            "review_chain_id": "pr-review-42-old-sha",
        },
    )
    state.complete_run(
        "pr-review",
        run_id,
        metadata={
            "reviewed_head_sha": "follow-up-sha",
            "review_fix_pushed": False,
            "review_report_valid": True,
            "review_report": {"verdict": "clean"},
        },
    )

    assert PrFixerService("demo", project, config)._review_plan(42, "follow-up-sha") is None


def test_internal_review_is_due_before_any_checks_exist(tmp_path: Path) -> None:
    config = PrFixerConfig(base_branch="staging")
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [],
    }

    assert _review_is_due(metadata, config) is True


def test_copilot_review_suppresses_internal_review(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=tmp_path / "state.db")
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "headRefOid": "abc123",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "labels": [],
        "reviews": [{"author": {"login": "copilot-pull-request-reviewer[bot]"}, "state": "APPROVED"}],
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    state = PrFixerService("demo", project, config)._collect_feedback({"number": 42})

    assert state["review_only"] is False
    assert state["workflow"] == "pr-fixer"


def test_auto_merge_waits_for_checks_and_uses_separate_claim(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database, auto_merge=True)
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "abc123",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
        "autoMergeRequest": None,
    }
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("gh", "pr", "view"):
            return '{"state":"OPEN","autoMergeRequest":{"enabledAt":"now"}}'
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)
    service = PrFixerService("demo", project, config)
    review_state = StateRepository(database, "demo", repo)
    review_state.claim_run("pr-review", "pr-review-42-abc123")
    review_state.complete_run("pr-review", "pr-review-42-abc123")

    service._ensure_auto_merge(42)

    assert ("gh", "pr", "merge", "42", "--auto", "--squash") in commands
    record = StateRepository(database, "demo", repo).load_run("pr-auto-merge", "pr-42-abc123")
    assert record is not None
    assert record.status == "completed"


def test_auto_merge_does_not_run_without_checks(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=tmp_path / "state.db", auto_merge=True)
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "abc123",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [],
        "autoMergeRequest": None,
    }
    merge_called = False

    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        nonlocal merge_called
        merge_called = args[:3] == ("gh", "pr", "merge")
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)
    PrFixerService("demo", project, config)._ensure_auto_merge(42)

    assert merge_called is False


def test_auto_merge_accepts_clean_review_chain_final_fix_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database, auto_merge=True)
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "final-sha",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
        "autoMergeRequest": None,
    }
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("gh", "pr", "view"):
            return '{"state":"OPEN","autoMergeRequest":{"enabledAt":"now"}}'
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)
    state = StateRepository(database, "demo", repo)
    state.claim_run(
        "pr-review",
        "pr-review-42-pre-fix-sha",
        metadata={
            "pull_request": 42,
            "head_sha": "pre-fix-sha",
            "review_round": 1,
        },
    )
    state.complete_run(
        "pr-review",
        "pr-review-42-pre-fix-sha",
        metadata={
            "reviewed_head_sha": "pre-fix-sha",
            "review_exhausted_head_sha": "final-sha",
            "review_report_valid": True,
            "review_report": {"verdict": "clean"},
        },
    )

    PrFixerService("demo", project, config)._ensure_auto_merge(42)

    assert ("gh", "pr", "merge", "42", "--auto", "--squash") in commands


def test_reconcile_persists_a_run_for_the_daemon_index(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    dispatched: list[tuple[Any, ...]] = []

    monkeypatch.setattr(
        "dev_agents.pr_fixer._run",
        lambda _repo, *args, **_kwargs: '[{"number":42,"labels":[]}]'
        if args[:3] == ("gh", "pr", "list")
        else "",
    )
    monkeypatch.setattr(
        "dev_agents.pr_fixer.Thread",
        lambda *args, **kwargs: (
            dispatched.append((args, kwargs))
            or SimpleNamespace(start=lambda: None)
        ),
    )

    service = PrFixerService("demo", project, config)
    service.reconcile()

    state = StateRepository(database, "demo", repo)
    runs = state.list_runs("pr-reconcile")
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert any(
        event.event == "jobs_dispatched"
        for event in state.list_run_events("pr-reconcile", runs[0].run_id)
    )
    assert len(dispatched) == 1


def test_fix_summary_mentions_trigger_commit_and_changed_files() -> None:
    body = _fix_summary_body(
        42,
        "pr-42-new-sha-checks",
        {"headRefOid": "old-sha", "url": "https://github.com/owner/repo/pull/42"},
        {"headRefOid": "new-sha-1234567890", "url": "https://github.com/owner/repo/pull/42"},
        ["comment:1", "check:sha:ci:tests:FAILURE"],
        ["src/fix.ts", "tests/fix.test.ts"],
        details="Added validation and corrected the async race.",
    )

    assert "<!-- dev-agents:pr-fixer-summary run=pr-42-new-sha-checks -->" in body
    assert "1 review thread" in body
    assert "1 failing check" in body
    assert "new-sha-1234" in body
    assert "src/fix.ts" in body
    assert "tests/fix.test.ts" in body
    assert "Added validation and corrected the async race." in body


def test_review_started_comment_links_to_report_run() -> None:
    body = _review_started_body(
        3056,
        "pr-review-3056-head",
        {"headRefOid": "abc123"},
        "https://dev-agents-reports.vercel.app?workflow=pr-review&run=pr-review-3056-head",
    )

    assert "[Open this run in dev-agents report](https://dev-agents-reports.vercel.app?workflow=pr-review&run=pr-review-3056-head)" in body


def test_publish_fix_summary_updates_existing_run_comment(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    body = "<!-- dev-agents:pr-fixer-summary run=run-1 -->\nsummary"

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        if args[:3] == ("gh", "api", "repos/owner/repo/issues/42/comments"):
            return '[[{"id": 77, "body": "<!-- dev-agents:pr-fixer-summary run=run-1 -->"}]]'
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer.repository_slug", lambda _repo: "owner/repo")
    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)

    assert _publish_fix_summary(tmp_path, 42, "run-1", body) is True
    assert any(
        call[:5]
        == (
            "gh",
            "api",
            "repos/owner/repo/issues/comments/77",
            "--method",
            "PATCH",
        )
        for call in calls
    )
    assert not any(
        len(call) > 3
        and call[0:3] == ("gh", "api", "repos/owner/repo/issues/42/comments")
        and call[3] == "-f"
        for call in calls
    )


def test_publish_fix_summary_creates_comment_when_run_comment_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []
    body = "<!-- dev-agents:pr-fixer-summary run=run-2 -->\nsummary"

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        if (
            args[:3] == ("gh", "api", "repos/owner/repo/issues/42/comments")
            and "--paginate" in args
        ):
            return "[]"
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer.repository_slug", lambda _repo: "owner/repo")
    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)

    assert _publish_fix_summary(tmp_path, 42, "run-2", body) is True
    assert any(
        call[:4] == ("gh", "api", "repos/owner/repo/issues/42/comments", "-f")
        for call in calls
    )
