import sqlite3
import subprocess
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
    _prompt,
    _publish_fix_summary,
    _publish_pr_run_comment,
    _pull_request_checks,
    _review_is_due,
    _review_progress_body,
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


def test_normalise_review_report_coerces_near_miss_field_names() -> None:
    """A finding with the right content under the wrong key name is recoverable."""
    report = _normalise_review_report(
        {
            "verdict": "findings",
            "findings": [
                {
                    "severity": "HIGH",
                    "category": "security",
                    "file": "src/auth.ts",
                    "line": 12,
                    "message": "Token is logged in plaintext.",
                    "remediation": "Redact the token before logging.",
                }
            ],
            "categories_checked": [],
            "validation": [],
            "fixes": [],
        }
    )

    assert report is not None
    finding = report["findings"][0]
    assert finding["location"] == "src/auth.ts:12"
    assert finding["impact"] == "Token is logged in plaintext."


def test_normalise_review_report_still_rejects_genuinely_missing_fields() -> None:
    """Aliasing must never fabricate a category or remediation that was never given."""
    assert _normalise_review_report(
        {
            "verdict": "findings",
            "findings": [
                {
                    "severity": "HIGH",
                    "file": "src/auth.ts",
                    "line": 12,
                    "message": "Token is logged in plaintext.",
                }
            ],
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

    assert "pr-review run=pr-review-42-abc123" in prompt
    assert "Never edit, PATCH, or" in prompt
    assert "as new PR comments" in prompt
    assert "DEV_AGENTS_REVIEW_REPORT_BEGIN" in prompt


def test_review_prompt_shows_a_concrete_findings_example(tmp_path: Path) -> None:
    """An empty findings:[] example gives no guidance on the finding OBJECT
    shape, so the agent guesses -- confirmed live (PR #205 on LearBear): it
    produced {pass, file, line, message} instead of the five required fields,
    three times in a row within the same run. Both prompt variants must show a
    concrete non-empty example using the exact required field names."""
    review_prompt = _review_prompt(
        tmp_path,
        42,
        {"headRefOid": "abc123", "headRefName": "feature/review-me"},
        "staging",
        [],
        run_id="pr-review-42-abc123",
    )
    fix_prompt = _prompt(tmp_path, 42, ["comment:1"], "staging", [])
    for prompt in (review_prompt, fix_prompt):
        assert '"verdict":"findings"' in prompt
        for field in ("severity", "category", "location", "impact", "remediation"):
            assert f'"{field}"' in prompt


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


def test_failed_review_with_pushed_fix_uses_targeted_follow_up(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    run_id = "pr-review-42-old-sha"
    state = StateRepository(database, "demo", repo)
    state.claim_run(
        "pr-review",
        run_id,
        metadata={
            "pull_request": 42,
            "head_sha": "old-sha",
            "review_round": 0,
            "review_chain_id": run_id,
        },
    )
    state.complete_run(
        "pr-review",
        run_id,
        status="failed",
        error="review agent did not complete successfully",
        metadata={
            "reviewed_head_sha": "old-sha",
            "review_fix_pushed": True,
            "review_follow_up_pending": True,
            "review_follow_up_head_sha": "new-sha",
        },
    )

    plan = PrFixerService("demo", project, config)._review_plan(42, "new-sha")

    assert plan is not None
    assert plan["round"] == 1
    assert plan["scope"] == "targeted-post-fix"
    assert plan["parent_run_id"] == run_id


def test_failed_review_auto_pauses_after_max_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run_id that keeps failing (e.g. the provider is out of quota) must stop
    retrying and spamming comments once it hits the configured failure cap."""
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database, max_consecutive_failures=3)
    service = PrFixerService("demo", project, config)
    run_id = "pr-review-42-sha"
    service.state.claim_run(
        "pr-review", run_id, metadata={"pull_request": 42, "head_sha": "sha", "review_round": 0}
    )

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "dev_agents.pr_fixer.service._pkg._run",
        lambda _repo, *args, **_kwargs: calls.append(args) or "",
    )
    monkeypatch.setattr(
        "dev_agents.pr_fixer.service._pkg._feedback",
        lambda *_args: ({"headRefOid": "sha"}, []),
    )

    service._finalize(
        {
            "number": 42,
            "workflow": "pr-review",
            "run_id": run_id,
            "claimed": True,
            "review_only": True,
            "review_round": 0,
            "attempt": 3,
            "fixed": False,
            "meta": {"headRefOid": "sha"},
            "report": {},
        }
    )

    label_calls = [call for call in calls if call[:3] == ("gh", "pr", "edit")]
    assert label_calls == [("gh", "pr", "edit", "42", "--add-label", "paused")]


def test_failed_review_does_not_auto_pause_below_the_failure_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database, max_consecutive_failures=3)
    service = PrFixerService("demo", project, config)
    run_id = "pr-review-42-sha"
    service.state.claim_run(
        "pr-review", run_id, metadata={"pull_request": 42, "head_sha": "sha", "review_round": 0}
    )

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "dev_agents.pr_fixer.service._pkg._run",
        lambda _repo, *args, **_kwargs: calls.append(args) or "",
    )
    monkeypatch.setattr(
        "dev_agents.pr_fixer.service._pkg._feedback",
        lambda *_args: ({"headRefOid": "sha"}, []),
    )

    service._finalize(
        {
            "number": 42,
            "workflow": "pr-review",
            "run_id": run_id,
            "claimed": True,
            "review_only": True,
            "review_round": 0,
            "attempt": 2,
            "fixed": False,
            "meta": {"headRefOid": "sha"},
            "report": {},
        }
    )

    assert [call for call in calls if call[:3] == ("gh", "pr", "edit")] == []


def test_superseded_review_does_not_exhaust_the_chain(tmp_path: Path) -> None:
    """A run whose accept_provider check detected a concurrent push (Jules, a
    human, or an overlapping run landing a new commit mid-review) must not be
    recorded as a genuine failure: it must not set review_exhausted_head_sha
    (which would permanently block ever reviewing the new head) and must leave
    _review_plan free to offer a fresh full review for it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(state_path=database)
    service = PrFixerService("demo", project, config)
    run_id = "pr-review-42-old-sha"
    service.state.claim_run(
        "pr-review",
        run_id,
        metadata={"pull_request": 42, "head_sha": "old-sha", "review_round": 1},
    )

    result = service._finalize(
        {
            "number": 42,
            "workflow": "pr-review",
            "run_id": run_id,
            "claimed": True,
            "review_only": True,
            "review_round": 1,
            "fixed": False,
            "meta": {"headRefOid": "old-sha"},
            "report": {"superseded_by": "jules-pushed-sha"},
        }
    )

    assert result == {"started": False}
    record = service.state.get_run("pr-review", run_id)
    assert record is not None
    assert record.status == "failed"
    assert record.error == "superseded by a concurrent push to the same branch"
    assert record.metadata.get("review_exhausted_head_sha") is None
    assert record.metadata.get("review_fix_pushed") is None

    # The new head must be freely reviewable, not permanently blocked.
    plan = service._review_plan(42, "jules-pushed-sha")
    assert plan is not None
    assert plan["round"] == 0
    assert plan["scope"] == "full"


def test_clean_review_auto_pushes_unpushed_base_merge(monkeypatch, tmp_path: Path) -> None:
    """isolated_worktree merges origin/{base} into the branch to surface conflicts
    before the agent runs. When base has moved and that merge is clean, local HEAD
    ends up one commit ahead of origin/{branch} even though the agent found nothing
    to fix -- a housekeeping commit, not agent output. The agent's own "clean status"
    self-check doesn't catch this (a committed-but-unpushed commit leaves git status
    empty), so the daemon must push it itself as a provable fast-forward rather than
    reporting a real review as failed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    for command in (
        ["git", "init", "-b", "staging"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=origin, check=True, capture_output=True)
    (origin / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "feature/x"], cwd=origin, check=True, capture_output=True
    )
    (origin / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature"], cwd=origin, check=True, capture_output=True
    )
    pr_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "staging"], cwd=origin, check=True, capture_output=True)
    # Advance base AFTER the PR branch diverged, so isolated_worktree's setup
    # merge produces a real (clean, no-conflict) merge commit.
    (origin / "unrelated.md").write_text("later base change\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.md"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "unrelated base change"], cwd=origin, check=True, capture_output=True
    )

    checkout = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)], check=True, capture_output=True
    )

    def fake_run_agent(_provider: str, _prompt: str, *, log_path: Path, **_kwargs: Any) -> Any:
        log_path.write_text(
            "DEV_AGENTS_REVIEW_REPORT_BEGIN\n"
            "FINDINGS: none\n"
            "FIXES: none\n"
            'REPORT_JSON: {"verdict":"clean","findings":[],'
            '"categories_checked":["general"],"validation":["tests passed"],"fixes":[]}\n'
            "DEV_AGENTS_REVIEW_REPORT_END\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, timed_out=False)

    monkeypatch.setattr("dev_agents.pr_fixer.service.run_agent", fake_run_agent)

    project = ProjectConfig(repo=checkout, github="owner/repo")
    config = PrFixerConfig(
        state_path=tmp_path / "state.db",
        worktree_dir=tmp_path / "worktrees",
        base_branch="staging",
    )
    service = PrFixerService("demo", project, config)

    succeeded, report = service._fix(
        42,
        "feature/x",
        [],
        review_only=True,
        meta={"headRefOid": pr_head},
        run_id="pr-review-42-test",
    )

    assert succeeded is True
    assert report.get("review_report", {}).get("verdict") == "clean"
    assert "superseded_by" not in report
    # The housekeeping merge commit must actually be on origin now.
    origin_feature_head = subprocess.run(
        ["git", "rev-parse", "feature/x"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert origin_feature_head != pr_head


def _service_with_reviews(tmp_path: Path) -> tuple[PrFixerService, StateRepository]:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    state = StateRepository(database, "demo", repo)
    service = PrFixerService(
        "demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(state_path=database)
    )
    return service, state


def _record_review(
    state: StateRepository,
    run_id: str,
    *,
    review_round: int,
    reviewed_head: str,
    fix_pushed: bool,
    verdict: str | None,
    exhausted_head: str | None = None,
) -> None:
    state.claim_run(
        "pr-review",
        run_id,
        metadata={"pull_request": 42, "head_sha": reviewed_head, "review_round": review_round},
    )
    state.complete_run(
        "pr-review",
        run_id,
        metadata={
            "reviewed_head_sha": reviewed_head,
            "review_round": review_round,
            "review_fix_pushed": fix_pushed,
            "review_exhausted_head_sha": exhausted_head,
            "review_report_valid": verdict is not None,
            "review_report": {"verdict": verdict} if verdict else None,
        },
    )


def test_targeted_round_that_pushed_a_non_clean_fix_earns_one_more_round(tmp_path: Path) -> None:
    """PR #339 sat forever: its only targeted round found and fixed a defect (verdict
    "findings"), which the chain treated as exhausted, so the fixed head was never verified
    and auto-merge could never accept it."""
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-a", review_round=1, reviewed_head="pre-fix", fix_pushed=True,
        verdict="findings", exhausted_head="final-sha",
    )

    plan = service._review_plan(42, "final-sha")

    assert plan is not None
    assert plan["round"] == 2
    assert plan["scope"] == "targeted-post-fix"
    assert plan["parent_run_id"] == "pr-review-42-a"
    assert plan["prior_report"] == {"verdict": "findings"}


def test_a_targeted_round_with_no_valid_report_that_pushed_a_fix_is_also_verified(
    tmp_path: Path,
) -> None:
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-a", review_round=1, reviewed_head="pre-fix", fix_pushed=True,
        verdict=None, exhausted_head="final-sha",
    )

    plan = service._review_plan(42, "final-sha")

    assert plan is not None and plan["round"] == 2


def test_the_last_allowed_round_always_ends_the_chain(tmp_path: Path) -> None:
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-a", review_round=2, reviewed_head="round-two-head", fix_pushed=True,
        verdict="findings", exhausted_head="last-sha",
    )

    assert service._review_plan(42, "last-sha") is None


@pytest.mark.parametrize(
    ("fix_pushed", "verdict"),
    [(False, "findings"), (False, "clean"), (True, "clean"), (False, None)],
)
def test_a_targeted_round_that_pushed_nothing_or_came_back_clean_ends_the_chain(
    tmp_path: Path, fix_pushed: bool, verdict: str | None
) -> None:
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-a", review_round=1, reviewed_head="reviewed", fix_pushed=fix_pushed,
        verdict=verdict, exhausted_head="final-sha",
    )

    assert service._review_plan(42, "final-sha") is None


def test_review_chain_terminates_even_if_every_round_pushes_a_non_clean_fix(
    tmp_path: Path,
) -> None:
    """Worst case: every round finds something and pushes a fix. The chain must still stop
    after MAX_INTERNAL_REVIEW_ROUNDS reviews, not loop the way the issue fixer did."""
    from dev_agents.pr_fixer import MAX_INTERNAL_REVIEW_ROUNDS

    service, state = _service_with_reviews(tmp_path)
    head = "head-0"
    rounds: list[int] = []
    for index in range(20):  # far more than could ever be legitimate
        plan = service._review_plan(42, head)
        if plan is None:
            break
        rounds.append(plan["round"])
        new_head = f"head-{index + 1}"
        ended = plan["round"] >= MAX_INTERNAL_REVIEW_ROUNDS - 1
        _record_review(
            state, f"pr-review-42-{index}", review_round=plan["round"], reviewed_head=head,
            fix_pushed=True, verdict="findings", exhausted_head=new_head if ended else None,
        )
        head = new_head
        # Start times have one-second resolution; space the runs out so "latest" is unambiguous.
        with sqlite3.connect(tmp_path / "state.db") as connection:
            connection.execute(
                "UPDATE runs SET started_at = ? WHERE run_id = ?",
                (f"2026-09-24T12:{index:02d}:00+00:00", f"pr-review-42-{index}"),
            )

    assert rounds == [0, 1, 2]
    assert service._review_plan(42, head) is None


def test_internal_review_stops_after_the_final_targeted_round_and_a_user_push_restarts(
    monkeypatch, tmp_path: Path
) -> None:
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-last", review_round=2, reviewed_head="round-two-head",
        fix_pushed=True, verdict="findings", exhausted_head="final-sha",
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

    capped = service._collect_feedback({"number": 42})
    assert capped["review_only"] is False
    assert capped["workflow"] == "pr-fixer"

    metadata["headRefOid"] = "later-user-commit"
    next_review = service._collect_feedback({"number": 42})
    assert next_review["review_only"] is True
    assert next_review["review_round"] == 0


def test_the_second_targeted_round_is_scheduled_for_a_fixed_unverified_head(
    monkeypatch, tmp_path: Path
) -> None:
    service, state = _service_with_reviews(tmp_path)
    _record_review(
        state, "pr-review-42-a", review_round=1, reviewed_head="pre-fix", fix_pushed=True,
        verdict="findings", exhausted_head="final-sha",
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

    scheduled = service._collect_feedback({"number": 42})

    assert scheduled["review_only"] is True
    assert scheduled["review_round"] == 2
    assert scheduled["review_scope"] == "targeted-post-fix"


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


def test_auto_merge_can_be_scoped_to_issue_fixer_prs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(
        state_path=tmp_path / "state.db",
        auto_merge=True,
        auto_merge_issue_fixes_only=True,
        review_without_copilot=False,
    )
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "issue-fix-sha",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
        "autoMergeRequest": None,
        "body": "<!-- dev-agents:issue-fix issue=92 run=issue-fix-92-demo -->",
    }
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda _repo, _number: (metadata, []))

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("gh", "pr", "view"):
            return '{"state":"OPEN","autoMergeRequest":{"enabledAt":"now"}}'
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)
    PrFixerService("demo", project, config)._ensure_auto_merge(42)

    assert ("gh", "pr", "merge", "42", "--auto", "--squash") in commands


def test_scoped_auto_merge_leaves_regular_prs_manual(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    config = PrFixerConfig(
        state_path=tmp_path / "state.db",
        auto_merge=True,
        auto_merge_issue_fixes_only=True,
        review_without_copilot=False,
    )
    metadata: dict[str, Any] = {
        "baseRefName": "staging",
        "isDraft": False,
        "labels": [],
        "headRefOid": "regular-sha",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
        "autoMergeRequest": None,
        "body": "A regular pull request.",
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


def test_review_progress_body_reuses_the_started_marker() -> None:
    """Must share _review_started_body's exact marker so progress notes for one
    run stay correlatable; publishing posts each as a new comment."""
    body = _review_progress_body(
        3056, "pr-review-3056-head", {"headRefOid": "abc123"}, None, 0, elapsed_seconds=754
    )

    assert "<!-- dev-agents:pr-review run=pr-review-3056-head -->" in body
    assert "12m elapsed" in body
    assert "abc123" in body


def test_review_lifecycle_posts_new_comment(monkeypatch, tmp_path: Path) -> None:
    """A matching lifecycle comment must not be overwritten: always POST new."""
    calls: list[tuple[str, ...]] = []

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer.repository_slug", lambda _repo: "owner/repo")
    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)

    assert _publish_pr_run_comment(
        tmp_path, 42, "run-1", "review-final", "<!-- dev-agents:pr-review run=run-1 -->\\nfinal"
    )
    assert any(
        call[:4] == ("gh", "api", "repos/owner/repo/issues/42/comments", "-f")
        for call in calls
    )
    assert not any("PATCH" in call for call in calls)
    assert not any("--paginate" in call for call in calls)


def test_publish_fix_summary_posts_new_comment(monkeypatch, tmp_path: Path) -> None:
    """A matching summary comment must not be overwritten: always POST new."""
    calls: list[tuple[str, ...]] = []
    body = "<!-- dev-agents:pr-fixer-summary run=run-1 -->\nsummary"

    def fake_run(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr("dev_agents.pr_fixer.repository_slug", lambda _repo: "owner/repo")
    monkeypatch.setattr("dev_agents.pr_fixer._run", fake_run)

    assert _publish_fix_summary(tmp_path, 42, "run-1", body) is True
    assert any(
        call[:4] == ("gh", "api", "repos/owner/repo/issues/42/comments", "-f")
        for call in calls
    )
    assert not any("PATCH" in call for call in calls)


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
