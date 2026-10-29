import json
import subprocess
from pathlib import Path

import pytest

from dev_agents.config import CopyReviewConfig, IssueFixerConfig, PrFixerConfig, ProjectConfig
from dev_agents.copy_review import AnswerQualityAssessment, CoherenceCheck, EditorialFinding
from dev_agents.issue_fixer import (
    CONTINUE_PROMPT,
    IssueFixerService,
    _existing_issue_pr,
    _implement,
    _isolated_issue_worktree,
    _issue,
    _issue_result_body,
    _last_agent_output,
    _open_bug_issues,
    _plan_comment_body,
    _pr_number_from_url,
    _pr_result_body,
    _prompt,
    _publish_issue_comment,
    _reported_line,
    _reset_worktree_for_fallback,
)
from dev_agents.runtime import StateRepository
from dev_agents.runtime.agent import AgentResult


def test_issue_reads_latest_reopen_across_event_pages(monkeypatch, tmp_path):
    responses = iter([
        json.dumps({"number": 92, "state": "OPEN"}),
        json.dumps([
            [{"id": 10, "event": "reopened"}],
            [{"id": 30, "event": "closed"}, {"id": 20, "event": "reopened"}],
        ]),
    ])
    monkeypatch.setattr("dev_agents.issue_fixer._run", lambda *_args, **_kwargs: next(responses))
    assert _issue(tmp_path, 92)["reopenEventId"] == "20"


def test_reopened_issue_gets_new_run_and_branch_without_duplicate_runs(monkeypatch, tmp_path):
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    state = StateRepository(tmp_path / "state.db", "lear-bear", tmp_path)
    service = IssueFixerService("lear-bear", project, IssueFixerConfig(), PrFixerConfig(), state)
    issue = {"state": "OPEN", "title": "Same title", "body": "Same body", "labels": [{"name": "bug"}]}
    monkeypatch.setattr("dev_agents.issue_fixer._issue", lambda *_args: issue)
    monkeypatch.setattr("dev_agents.issue_fixer._existing_issue_pr", lambda *_args: None)
    initial = service._collect_issue({"number": 92})
    state.complete_run("issue-fixer", initial["run_id"])
    assert service._collect_issue({"number": 92})["skip"] is True

    for event in ("100", "200"):
        issue["reopenEventId"] = event
        reopened = service._collect_issue({"number": 92})
        assert reopened["claimed"] is True
        assert reopened["run_id"] != initial["run_id"]
        assert reopened["branch"] == f"dev-agents/issue-92-reopen-{event}"
        assert service._collect_issue({"number": 92})["skip"] is True
        state.complete_run("issue-fixer", reopened["run_id"])
        assert service._collect_issue({"number": 92})["skip"] is True

    issue["reopenEventId"] = "300"
    monkeypatch.setattr("dev_agents.issue_fixer._existing_issue_pr", lambda *_args: {"number": 123})
    assert service._collect_issue({"number": 92})["skip"] is True
    assert len(state.list_runs("issue-fixer")) == 3


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


def test_answer_prompt_gives_orchestrator_precedence_over_skill_pr_steps(tmp_path: Path) -> None:
    prompt = _prompt(
        tmp_path,
        {"number": 42, "title": "How do I run a heist?", "url": "https://x/42", "body": "Brief"},
        "staging",
        "dev-agents/answer-42",
        [],
        kind="answer",
    )
    assert "Orchestrator ownership" in prompt
    assert "Do not create branches or pull" in prompt
    # The shared pr-fix skill assumes an existing PR; it must not be inlined here.
    assert "Inspect the PR metadata" not in prompt
    # A word the target skill bans in articles must not be recommended by the prompt.
    assert "cornerstone" not in prompt


def test_issue_fixer_config_kind_defaults_to_fix() -> None:
    assert IssueFixerConfig().kind == "fix"
    assert IssueFixerConfig(label="gui-fix", kind="enhancement").kind == "enhancement"
    # Effort is inherited from the PR fixer unless a queue pins its own.
    assert IssueFixerConfig().reasoning_effort is None
    assert IssueFixerConfig(kind="answer", reasoning_effort="high").reasoning_effort == "high"
    # The pre-PR quality loop defaults to an 8.5 gate with a bounded revision cap.
    assert IssueFixerConfig(kind="answer").pre_pr_score_threshold == 8.5
    assert IssueFixerConfig(kind="answer").pre_pr_max_revisions == 3


def _assessment(
    verdict: str, *, blocking: bool = False, score: float = 8.0, suggestion: bool = False
) -> AnswerQualityAssessment:
    findings = (
        [
            EditorialFinding(
                severity="blocking",
                category="examples",
                location="pages/new.ts: example",
                evidence="The weak approach fails for an unrelated reason.",
                reader_impact="Readers learn the wrong lesson.",
                proposed_fix="Give the weak approach one demonstrated failure mode.",
            )
        ]
        if blocking
        else []
    )
    if suggestion:
        findings = [
            EditorialFinding(
                severity="suggestion",
                category="clarity",
                location="pages/new.ts: opening",
                evidence="The opening repeats the heading.",
                reader_impact="Readers skim past the answer.",
                proposed_fix="Cut the repeated sentence.",
            )
        ]
    return AnswerQualityAssessment(
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        overall_assessment="Assessment",
        what_works=["Clear structure"],
        most_important_improvement="Fix the example" if blocking else "none",
        findings=findings,
        checks_performed=["Read the draft"],
        limitations=[],
        coherence_checks=[
            CoherenceCheck(scope=scope, status="pass", evidence="Checked")  # type: ignore[arg-type]
            for scope in ("repetition", "examples", "flow")
        ],
    )


def _answer_service(tmp_path: Path, **config_kwargs: object) -> IssueFixerService:
    project = ProjectConfig(
        repo=tmp_path, copy_review=CopyReviewConfig(page_globs=["pages/*.ts"])
    )
    state = StateRepository(tmp_path / "state.db", "proj", tmp_path)
    return IssueFixerService(
        "proj",
        project,
        IssueFixerConfig(
            kind="answer", provider="muse", model="muse-wonder-1.0", **config_kwargs  # type: ignore[arg-type]
        ),
        PrFixerConfig(),
        state,
    )


def _review_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "worktree": tmp_path,
        "issue": {"number": 9, "title": "Brief", "body": "Write it"},
        "number": 9,
        "run_id": "issue-fix-9-abc",
        "log_path": tmp_path / "issue-9.log",
        "base_sha": "base",
        "branch": "dev-agents/answer-9",
        "verify_pushed": lambda _result: (True, "ok"),
        "initial_note": "",
    }


def test_pre_pr_review_passes_a_clean_draft_without_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path)
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts"
    )
    monkeypatch.setattr(
        "dev_agents.issue_fixer.review_answer_draft",
        lambda *_a, **_k: _assessment("clean", score=9.0),
    )
    revisions: list[str] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer.run_agent",
        lambda *_a, **_k: revisions.append("ran") or AgentResult(0, False),
    )

    note, still_fixed = service._pre_pr_answer_review(**_review_kwargs(tmp_path))  # type: ignore[arg-type]

    assert still_fixed is True
    assert "passed the score gate in round 1" in note
    assert "score 9.0/10" in note
    assert revisions == []


def test_pre_pr_review_revises_once_then_reports_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path)
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts"
    )
    reviews = iter(
        [_assessment("changes_required", blocking=True), _assessment("clean", score=9.0)]
    )
    monkeypatch.setattr(
        "dev_agents.issue_fixer.review_answer_draft", lambda *_a, **_k: next(reviews)
    )
    revision_prompts: list[str] = []

    def fake_run_agent(provider: str, prompt: str, **kwargs: object) -> AgentResult:
        revision_prompts.append(prompt)
        assert kwargs["json_events"] is True
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.issue_fixer.run_agent", fake_run_agent)

    note, still_fixed = service._pre_pr_answer_review(**_review_kwargs(tmp_path))  # type: ignore[arg-type]

    assert still_fixed is True
    assert "passed the score gate in round 2" in note
    assert len(revision_prompts) == 1
    assert "one demonstrated failure mode" in revision_prompts[0]
    assert "State each corrected fact once" in revision_prompts[0]


def test_pre_pr_loop_keeps_revising_below_threshold_until_the_gate_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path)
    monkeypatch.setattr("dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts")
    reviews = iter(
        [
            _assessment("changes_required", blocking=True, score=7.0),
            _assessment("clean", score=8.0, suggestion=True),
            _assessment("clean", score=9.0),
        ]
    )
    monkeypatch.setattr(
        "dev_agents.issue_fixer.review_answer_draft", lambda *_a, **_k: next(reviews)
    )
    revisions: list[str] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer.run_agent",
        lambda *_a, **_k: revisions.append("ran") or AgentResult(0, False),
    )
    rounds: list[tuple[int, float, str, int, bool]] = []
    kwargs = _review_kwargs(tmp_path)
    kwargs["on_round"] = lambda *args: rounds.append(args)  # type: ignore[assignment]

    note, still_fixed = service._pre_pr_answer_review(**kwargs)  # type: ignore[arg-type]

    assert still_fixed is True
    assert "passed the score gate in round 3" in note
    assert "round 1: score 7.0/10" in note and "round 3: score 9.0/10" in note
    assert len(revisions) == 2
    assert rounds == [
        (1, 7.0, "changes_required", 1, True),
        (2, 8.0, "clean", 0, True),
        (3, 9.0, "clean", 0, False),
    ]


def test_pre_pr_loop_stops_at_the_revision_cap_and_discloses_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path, pre_pr_max_revisions=1)
    monkeypatch.setattr("dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts")
    monkeypatch.setattr(
        "dev_agents.issue_fixer.review_answer_draft",
        lambda *_a, **_k: _assessment("changes_required", blocking=True, score=7.0),
    )
    revisions: list[str] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer.run_agent",
        lambda *_a, **_k: revisions.append("ran") or AgentResult(0, False),
    )

    note, still_fixed = service._pre_pr_answer_review(**_review_kwargs(tmp_path))  # type: ignore[arg-type]

    assert still_fixed is True
    assert "reached the revision cap (1)" in note
    assert "Remaining blocking findings for human review" in note
    assert len(revisions) == 1


def test_pre_pr_review_failure_is_disclosed_not_treated_as_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path)
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts"
    )

    def broken_reviewer(*_args: object, **_kwargs: object) -> AnswerQualityAssessment:
        raise RuntimeError("reviewer unavailable")

    monkeypatch.setattr("dev_agents.issue_fixer.review_answer_draft", broken_reviewer)

    note, still_fixed = service._pre_pr_answer_review(**_review_kwargs(tmp_path))  # type: ignore[arg-type]

    assert still_fixed is True
    assert "unavailable" in note
    assert "post-merge copy review remains" in note


def test_fallback_reset_discards_partial_edits_and_pushed_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "dev_agents.issue_fixer._run",
        lambda _repo, *args, **_kwargs: commands.append(args) or "",
    )

    _reset_worktree_for_fallback(tmp_path, "staging", "dev-agents/answer-9")

    assert commands == [
        ("git", "reset", "--hard", "origin/staging"),
        ("git", "clean", "-fd"),
        ("git", "push", "origin", "--delete", "dev-agents/answer-9"),
    ]


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


def test_pre_pr_gate_requires_a_clean_verdict_even_without_blockers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _answer_service(tmp_path)
    monkeypatch.setattr("dev_agents.issue_fixer._run", lambda *_a, **_k: "pages/new.ts")
    monkeypatch.setattr(
        "dev_agents.issue_fixer.review_answer_draft",
        lambda *_a, **_k: _assessment("needs_human_review", score=9.0),
    )

    note, still_fixed = service._pre_pr_answer_review(**_review_kwargs(tmp_path))  # type: ignore[arg-type]

    assert still_fixed is True
    assert "passed the score gate" not in note
    assert "needs_human_review" in note


def _fake_runs(log_path: Path, outputs: list[str], plan: str = "1. Do it\n2. Test it"):
    """A fake agent runner: each call appends a log segment; the read-only call writes the plan."""
    calls: list[dict[str, object]] = []

    def run(prompt: str, **options: object) -> AgentResult:
        calls.append({"prompt": prompt, **options})
        output = outputs[len(calls) - 1]
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"=== agent started provider=codex ===\nsession id: 0000-000{len(calls)}\n")
            stream.write(f"user\n{prompt}\n{output}\n")
        if options.get("read_only") and plan:
            Path(str(options["last_message_path"])).write_text(plan, encoding="utf-8")
        return AgentResult(124 if output == "TIMEOUT" else 0, output == "TIMEOUT")

    return run, calls


def _prompt_for(plan: str | None, assess: bool) -> str:
    return f"IMPLEMENT plan={plan} assess={assess}" + (
        "\nISSUE_FIX_PLAN_REQUIRED: <why this issue needs a plan, in one line>"
        "\nSUMMARY: <what was implemented, or the concrete blocker>" if assess else ""
    )


def _implement_kwargs(tmp_path: Path, plans: list[tuple[str, str]], done: list[bool], **extra: object):
    return {
        "prompt_for": _prompt_for,
        "log_path": tmp_path / "issue.log",
        "effort": "medium",
        "is_done": lambda: done[0],
        "planning": ("PLAN", "high", tmp_path / "issue.plan.md", lambda plan, why: plans.append((plan, why))),
        **extra,
    }


def test_a_small_issue_is_implemented_without_planning(tmp_path: Path) -> None:
    run, calls = _fake_runs(tmp_path / "issue.log", ["SUMMARY: fixed it"])
    plans: list[tuple[str, str]] = []

    result = _implement(run, **_implement_kwargs(tmp_path, plans, [True], continuations=3))  # type: ignore[arg-type]

    assert result.returncode == 0
    assert [(call["prompt"].split("\n")[0], call["effort"]) for call in calls] == [
        ("IMPLEMENT plan=None assess=True", "medium")
    ]
    assert plans == []


def test_a_comprehensive_issue_is_planned_at_higher_effort_then_implemented(tmp_path: Path) -> None:
    run, calls = _fake_runs(
        tmp_path / "issue.log",
        ["ISSUE_FIX_PLAN_REQUIRED: needs migrations, server and UI", "plan written", "SUMMARY: done"],
        plan="1. Edit /wt/src/a.ts\n",
    )
    plans: list[tuple[str, str]] = []

    _implement(run, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, plans, [True], clean_plan=lambda text: text.replace("/wt/", "").strip(),
    ))

    assert [call["prompt"] for call in calls][1:] == ["PLAN", "IMPLEMENT plan=1. Edit src/a.ts assess=False"]
    assert [call["effort"] for call in calls] == ["medium", "high", "medium"]
    assert calls[1]["read_only"] is True
    assert (tmp_path / "issue.plan.md").read_text(encoding="utf-8") == "1. Edit src/a.ts"
    assert plans == [("1. Edit src/a.ts", "needs migrations, server and UI")]


def test_a_retry_reuses_the_saved_plan_without_planning_or_posting_again(tmp_path: Path) -> None:
    (tmp_path / "issue.plan.md").write_text("1. Saved step", encoding="utf-8")
    run, calls = _fake_runs(tmp_path / "issue.log", ["SUMMARY: done"])
    plans: list[tuple[str, str]] = []

    _implement(run, **_implement_kwargs(tmp_path, plans, [True]))  # type: ignore[arg-type]

    assert [call["prompt"] for call in calls] == ["IMPLEMENT plan=1. Saved step assess=False"]
    assert plans == []


def test_a_failed_or_empty_plan_ends_the_attempt_without_implementing(tmp_path: Path) -> None:
    run, calls = _fake_runs(tmp_path / "issue.log", ["ISSUE_FIX_PLAN_REQUIRED: big", "no plan"], plan="")
    plans: list[tuple[str, str]] = []

    result = _implement(run, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, plans, [False], continuations=2, handoff=("gpt-6-luna", "high", "gpt-6.1-terra"),
    ))

    assert result.returncode == 1
    assert len(calls) == 2
    assert not (tmp_path / "issue.plan.md").exists()


def test_an_unfinished_run_resumes_its_session_until_it_pushes(tmp_path: Path) -> None:
    done = [False]
    run, calls = _fake_runs(tmp_path / "issue.log", ["SUMMARY: Not implemented yet", "working", "SUMMARY: done"])

    def resuming(prompt: str, **options: object) -> AgentResult:
        result = run(prompt, **options)
        done[0] = len(calls) == 3
        return result

    _implement(resuming, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, [], done, planning=None, continuations=5, handoff=("gpt-6-luna", "high", "gpt-6.1-terra"),
    ))

    assert [call["prompt"] for call in calls][1:] == [CONTINUE_PROMPT, CONTINUE_PROMPT]
    assert [call.get("resume_session") for call in calls] == [None, "0000-0001", "0000-0002"]
    assert all("model" not in call for call in calls)


def test_a_first_blocked_report_is_still_resumed_once(tmp_path: Path) -> None:
    """Terra labelled unfinished work on #483 as blocked; the first stop must not skip continuation."""
    run, calls = _fake_runs(tmp_path / "issue.log", [
        "SUMMARY: BLOCKED: needs coordinated changes I did not complete",
        "SUMMARY: BLOCKED: needs a product decision",
        "SUMMARY: done",
    ])

    _implement(run, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, [], [False], planning=None, continuations=3, handoff=("gpt-6-luna", "high", "gpt-6.1-terra"),
    ))

    assert [call.get("resume_session") for call in calls] == [None, "0000-0001", None]
    assert (calls[2]["model"], calls[2]["effort"]) == ("gpt-6-luna", "high")
    assert "Another agent (gpt-6.1-terra) started this issue" in str(calls[2]["prompt"])
    assert str(calls[2]["prompt"]).startswith("IMPLEMENT plan=None assess=False")


def test_handoff_continues_the_plan_after_continuations_run_out(tmp_path: Path) -> None:
    (tmp_path / "issue.plan.md").write_text("1. Step", encoding="utf-8")
    run, calls = _fake_runs(tmp_path / "issue.log", ["SUMMARY: stopped", "SUMMARY: stopped again", "SUMMARY: done"])

    _implement(run, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, [], [False], continuations=1, handoff=("gpt-6-luna", None, "gpt-6.1-terra"),
    ))

    assert [call.get("resume_session") for call in calls] == [None, "0000-0001", None]
    assert str(calls[2]["prompt"]).startswith("IMPLEMENT plan=1. Step assess=False")
    assert (calls[2]["model"], calls[2]["effort"]) == ("gpt-6-luna", "medium")


def test_a_timed_out_run_is_neither_continued_nor_handed_off(tmp_path: Path) -> None:
    run, calls = _fake_runs(tmp_path / "issue.log", ["TIMEOUT"])

    result = _implement(run, **_implement_kwargs(  # type: ignore[arg-type]
        tmp_path, [], [False], planning=None, continuations=3, handoff=("gpt-6-luna", "high", "gpt-6.1-terra"),
    ))

    assert result.timed_out and len(calls) == 1


def test_report_fields_ignore_the_prompt_placeholders_and_earlier_runs(tmp_path: Path) -> None:
    log = tmp_path / "issue.log"
    log.write_text(
        "=== agent started ===\nSUMMARY: first run\n"
        "=== agent started ===\nuser\nSUMMARY: <what was implemented>\nSUMMARY: blocked by X\n",
        encoding="utf-8",
    )

    output = _last_agent_output(log)

    assert _reported_line(output, "SUMMARY") == "blocked by X"
    assert _reported_line("SUMMARY: <placeholder>", "SUMMARY") == ""
    assert _last_agent_output(tmp_path / "missing.log") == ""


def test_prompt_adds_the_scope_assessment_or_the_plan(tmp_path: Path) -> None:
    issue = {"number": 7, "title": "T", "body": "B"}

    plain = _prompt(tmp_path, issue, "main", "b", [], kind="enhancement")
    assessing = _prompt(tmp_path, issue, "main", "b", [], kind="enhancement", assess_scope=True)
    planned = _prompt(tmp_path, issue, "main", "b", [], kind="enhancement", assess_scope=True, plan="1. Step")

    assert "ISSUE_FIX_PLAN_REQUIRED" not in plain
    assert "ISSUE_FIX_PLAN_REQUIRED: <why" in assessing
    assert "Implementation plan:\n1. Step" in planned
    assert "ISSUE_FIX_PLAN_REQUIRED" not in planned


def test_plan_comment_collapses_the_plan() -> None:
    plan = "1. Edit [a.ts](src/a.ts), see [docs](https://x.dev/d) and [below](#tests)"
    body = _plan_comment_body(7, "run-1", "needs migrations", plan, "high", "https://github.com/o/r/blob/main")

    assert "<!-- dev-agents:issue-fix issue=7 run=run-1 phase=plan -->" in body
    assert "comprehensive: needs migrations" in body
    assert "[a.ts](https://github.com/o/r/blob/main/src/a.ts)" in body
    assert "[docs](https://x.dev/d)" in body and "[below](#tests)" in body
    assert "<summary>Implementation plan</summary>" in body


def test_planning_continuation_and_handoff_are_off_by_default() -> None:
    config = IssueFixerConfig()

    assert (config.planning_effort, config.continuations, config.handoff_model) == (None, 0, None)


def test_codex_resume_command_continues_the_session() -> None:
    from dev_agents.runtime.agent import provider_command

    command = provider_command("codex", "x", reasoning_effort="high", model="m", resume_session="abc")

    assert command[:3] == ["codex", "exec", "resume"]
    assert command[-2:] == ["abc", "-"]
