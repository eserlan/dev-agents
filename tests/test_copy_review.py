import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dev_agents.config import CopyReviewConfig, PrFixerConfig, ProjectConfig
from dev_agents.copy_review import (
    _answer_page_paths,
    _parse_assessment,
    _prompt,
    is_answer_merge,
    review_answer_draft,
    review_merged_answer,
)
from dev_agents.runtime import StateRepository
from dev_agents.runtime.agent import AgentResult


def assessment(**changes: Any) -> dict[str, Any]:
    return {
        "score": 7.0,
        "verdict": "clean",
        "overall_assessment": "Useful procedure, but the example limits meaningful choice.",
        "what_works": ["Keep the clear separation of access and commitments."],
        "most_important_improvement": "Give participants different fictional ways to affect the deal.",
        "findings": [],
        "checks_performed": [
            "Read the complete example and compared its advice with player choices."
        ],
        "limitations": [],
        "coherence_checks": [
            {"scope": scope, "status": "pass", "evidence": "Reviewed complete article sections"}
            for scope in ("repetition", "examples", "flow")
        ],
        "issue_title": None,
        "issue_body": None,
        "existing_issue_url": None,
        **changes,
    }


def test_selects_new_typescript_answer_over_related_pages() -> None:
    diff = """diff --git a/answers/pages/related.ts b/answers/pages/related.ts
--- a/answers/pages/related.ts
+++ b/answers/pages/related.ts
@@ -1 +1 @@
-link
+updated link
diff --git a/answers/pages/new.ts b/answers/pages/new.ts
new file mode 100644
--- /dev/null
+++ b/answers/pages/new.ts
@@ -0,0 +1 @@
+article
"""
    assert _answer_page_paths(diff, ["answers/pages/*.ts"]) == ["answers/pages/new.ts"]


def test_review_answer_draft_reads_worktree_and_parses_assessment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = tmp_path / "answers" / "pages" / "new.ts"
    page.parent.mkdir(parents=True)
    page.write_text("export const answer = {};\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_run_agent(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        seen.update(provider=provider, prompt=prompt, kwargs=kwargs)
        kwargs["last_message_path"].write_text(json.dumps(assessment()), encoding="utf-8")
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.copy_review.run_agent", fake_run_agent)
    result = review_answer_draft(
        ProjectConfig(repo=tmp_path),
        CopyReviewConfig(),
        worktree=tmp_path,
        page_paths=["answers/pages/new.ts"],
        issue={"number": 7, "title": "Brief", "body": "Write it"},
        log_path=tmp_path / "draft-review.log",
    )

    assert result.verdict == "clean"
    assert seen["provider"] == "codex"
    assert "export const answer" in seen["prompt"]
    assert "unpublished Answer draft" in seen["prompt"]
    assert "8.5: publish-ready" in seen["prompt"]


def test_review_answer_draft_rejects_pages_outside_the_worktree(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside the worktree"):
        review_answer_draft(
            ProjectConfig(repo=tmp_path),
            CopyReviewConfig(),
            worktree=tmp_path,
            page_paths=["../outside.ts"],
            issue={"number": 7},
            log_path=tmp_path / "draft-review.log",
        )


def _blocking_finding(category: str) -> dict[str, Any]:
    return {
        "severity": "blocking", "category": category,
        "location": "article.ts: opening", "evidence": "Quoted problem",
        "reader_impact": "Readers are misled", "proposed_fix": "Correct it",
    }


def test_score_is_capped_by_a_blocking_factual_finding() -> None:
    report = assessment(
        score=9.5, verdict="changes_required",
        findings=[_blocking_finding("Product accuracy")],
        issue_title="Fix the claim", issue_body="Correct the opening advice",
    )
    assert _parse_assessment(json.dumps(report)).score == 7.0


def test_score_is_capped_by_a_blocking_coherence_check() -> None:
    report = assessment(
        score=9.5, verdict="changes_required",
        findings=[_blocking_finding("examples")],
        coherence_checks=[
            {"scope": "repetition", "status": "pass", "evidence": "Checked"},
            {"scope": "examples", "status": "blocking", "evidence": "Contradictory example"},
            {"scope": "flow", "status": "pass", "evidence": "Checked"},
        ],
        issue_title="Fix the example", issue_body="Correct the example",
    )
    assert _parse_assessment(json.dumps(report), require_coherence=True).score == 7.5


def test_score_is_uncapped_without_blocking_findings() -> None:
    assert _parse_assessment(json.dumps(assessment(score=9.5))).score == 9.5


def test_prompt_defines_the_scoring_bands_and_caps() -> None:
    prompt = _prompt(
        1, "https://example.com/pr/1", "article",
        instructions="Use British English.", merge_sha="abc",
        pr_body="", original_issue=None, existing_issues=[],
    )
    assert "8.5: publish-ready" in prompt
    assert "caps the score at 7.0" in prompt


def test_parses_saved_reviewer_finding_names() -> None:
    report = assessment(
        verdict="changes_required",
        findings=[{
            "severity": "blocking", "category": "agency",
            "source_location": "article.ts: opening",
            "quoted_evidence": ["Always force the scene", "Ignore their choice"],
            "reader_impact": "Invalidates decisions",
            "actionable_fix": "Let consequences follow the players' choices",
        }],
        issue_title="Preserve player choices", issue_body="Correct the opening advice",
    )
    finding = _parse_assessment(json.dumps(report)).findings[0]
    assert finding.location == "article.ts: opening"
    assert finding.evidence == "Always force the scene\nIgnore their choice"
    assert finding.proposed_fix == "Let consequences follow the players' choices"


@pytest.mark.parametrize(
    ("title", "branch", "eligible"),
    [
        ("answer: New guide", "writer/topic", True),
        ("docs: Copy correction", "dev-agents/copy-fix-42", True),
        ("fix: unrelated", "dev-agents/issue-42", False),
    ],
)
def test_recognizes_answer_and_bounded_copy_fix_merges(
    title: str, branch: str, eligible: bool
) -> None:
    config = CopyReviewConfig(base_branch="staging")
    assert (
        is_answer_merge(
            {"base": {"ref": "staging"}, "head": {"ref": branch}, "title": title}, config
        )
        is eligible
    )


def test_prompt_requires_reader_outcomes_and_targeted_copy_fix_verification() -> None:
    prompt = _prompt(
        1,
        "https://example.test/pr/1",
        "Complete article",
        instructions="Use British English.",
        merge_sha="abc123",
        pr_body="Fixes #7",
        original_issue={"number": 7},
        existing_issues=[],
        correction_issue={"number": 8, "body": "Acceptance criteria"},
        correction_verification=True,
    )
    assert "targeted verification" in prompt
    assert "Do not start another full-review/correction loop" in prompt
    assert "materially improves the reader's outcome" in prompt
    assert "Copy-fix issue to verify" in prompt


@pytest.mark.parametrize(
    "changes",
    [
        {"score": 11},
        {"issue_title": "Fix the example"},
        {
            "issue_title": "Fix the example",
            "issue_body": "Change it",
            "existing_issue_url": "https://example.test/1",
        },
    ],
)
def test_rejects_invalid_quality_assessments(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _parse_assessment(json.dumps(assessment(**changes)))


def test_rejects_clean_verdict_with_blocking_finding() -> None:
    payload = assessment(
        findings=[
            {
                "severity": "blocking",
                "category": "accuracy",
                "location": "Example",
                "evidence": "The roll total is wrong.",
                "reader_impact": "The reader learns an incorrect rule.",
                "proposed_fix": "Correct the total.",
            }
        ]
    )
    with pytest.raises(ValueError, match="clean review"):
        _parse_assessment(json.dumps(payload))


@pytest.mark.parametrize("scope", ["repetition", "examples", "flow"])
def test_clean_requires_explicit_whole_article_checks(scope: str) -> None:
    payload = assessment()
    payload["coherence_checks"] = [c for c in payload["coherence_checks"] if c["scope"] != scope]
    with pytest.raises(ValueError, match="exactly once"):
        _parse_assessment(json.dumps(payload), require_coherence=True)


def test_missing_coherence_is_legacy_readable_but_not_a_new_live_review() -> None:
    payload = assessment()
    payload.pop("coherence_checks")
    assert _parse_assessment(json.dumps(payload)).verdict == "clean"
    with pytest.raises(ValueError, match="exactly once"):
        _parse_assessment(json.dumps(payload), require_coherence=True)


@pytest.mark.parametrize("scope", ["repetition", "flow"])
def test_whole_article_checks_cannot_be_skipped_as_not_applicable(scope: str) -> None:
    payload = assessment()
    for check in payload["coherence_checks"]:
        if check["scope"] == scope:
            check["status"] = "not_applicable"
    with pytest.raises(ValueError, match="apply to every article"):
        _parse_assessment(json.dumps(payload), require_coherence=True)


def test_optional_polish_and_reference_without_example_can_pass() -> None:
    payload = assessment()
    payload["coherence_checks"][1] = {
        "scope": "examples", "status": "not_applicable", "evidence": "Concise dice notation reference",
    }
    payload["findings"] = [{
        "severity": "suggestion", "category": "flow", "location": "Conclusion",
        "evidence": "A brief recap", "reader_impact": "An optional wording preference",
        "proposed_fix": "Keep as-is or shorten the recap",
    }]
    assert _parse_assessment(json.dumps(payload), require_coherence=True).verdict == "clean"


def test_correction_proposal_is_routed_to_human_without_losing_findings() -> None:
    payload = assessment(
        verdict="changes_required", issue_title="Another rewrite", issue_body="Proposed edits",
        findings=[{
            "severity": "blocking", "category": "examples", "location": "Worked example",
            "evidence": "The novice prepares a caricature of a plan",
            "reader_impact": "The comparison fails to teach a realistic trade-off",
            "proposed_fix": "Show a credible novice plan and its actual failure mode",
        }],
    )
    payload["coherence_checks"][1]["status"] = "blocking"
    report = _parse_assessment(json.dumps(payload), require_coherence=True, correction_verification=True)
    assert report.verdict == "needs_human_review"
    assert len(report.findings) == 1
    assert report.issue_title is report.issue_body is report.existing_issue_url is None


def test_prompt_preserves_project_policy_and_requires_evidence() -> None:
    prompt = _prompt(
        1,
        "https://example.test/pr/1",
        "Complete source and metadata",
        instructions="Consequences should follow causes. Use British English.",
        merge_sha="abc123",
        pr_body="Fixes #7",
        original_issue={"number": 7},
        existing_issues=[{"url": "https://example.test/issues/2", "body": "Existing concern"}],
    )
    assert "Consequences should follow causes." in prompt
    assert "authoritative rules sources" in prompt
    assert "Cosmetic alternatives alone do not warrant an issue" in " ".join(prompt.split())
    assert "abc123" in prompt
    assert "Existing concern" in prompt
    assert "acceptance criteria" in prompt


@pytest.mark.parametrize("outcome", ["new", "none", "existing", "closed"])
def test_quality_review_publication_and_persisted_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    policy = repo / "editorial.md"
    policy.write_text("Review player agency and preserve what works.")
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        pr_fixer=PrFixerConfig(log_dir=tmp_path / "logs"),
    )
    settings = CopyReviewConfig(
        page_globs=["answers/pages/*.ts"], instructions_path=Path("editorial.md")
    )
    state = StateRepository(tmp_path / "state.db", "demo", repo)
    existing_url = "https://github.com/owner/repo/issues/2"
    payload = assessment()
    if outcome == "new":
        payload["verdict"] = "changes_required"
        payload["findings"] = [
            {
                "severity": "blocking",
                "category": "player agency",
                "location": "Negotiation example",
                "evidence": "Participants have no choice.",
                "reader_impact": "The example removes meaningful choice.",
                "proposed_fix": "Let each participant affect the deal.",
            }
        ]
        payload.update(
            issue_title="Preserve player choice in the negotiation example",
            issue_body="Context and strengths.\nSpecific problem: participants have no choice.\nAcceptance criteria: each participant can affect the deal.",
        )
    elif outcome in {"existing", "closed"}:
        payload["verdict"] = "changes_required"
        payload["findings"] = [
            {
                "severity": "blocking",
                "category": "player agency",
                "location": "Negotiation example",
                "evidence": "Participants have no choice.",
                "reader_impact": "The example removes meaningful choice.",
                "proposed_fix": "Let each participant affect the deal.",
            }
        ]
        payload["existing_issue_url"] = existing_url
    calls: list[tuple[str, ...]] = []

    def fake_json(_repo: Path, *args: str, **_kwargs: Any) -> Any:
        if args[0] == "api":
            assert "ref=merged123" in args
            return {
                "content": base64.b64encode(b"Complete article and discovery metadata").decode()
            }
        if args[:2] == ("issue", "view"):
            if args[2] == existing_url:
                return {"state": "CLOSED" if outcome == "closed" else "OPEN", "labels": []}
            return {"number": 7, "body": "Original brief"}
        return []

    def fake_gh(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ("pr", "diff"):
            return (
                "diff --git a/answers/pages/new.ts b/answers/pages/new.ts\nnew file mode 100644\n"
            )
        if args[:2] == ("issue", "create"):
            assert args[args.index("--label") + 1] == "copy-fix"
            body = Path(args[args.index("--body-file") + 1]).read_text()
            assert "Acceptance criteria" in body
            return "https://github.com/owner/repo/issues/3"
        return ""

    def fake_agent(_provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        assert "Original brief" in prompt
        assert "Complete article and discovery metadata" in prompt
        assert "Review player agency and preserve what works." in prompt
        kwargs["last_message_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["last_message_path"].write_text(json.dumps(payload))
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.copy_review.gh_json", fake_json)
    monkeypatch.setattr("dev_agents.copy_review.gh", fake_gh)
    monkeypatch.setattr("dev_agents.copy_review.run_agent", fake_agent)
    pr = {
        "number": 1,
        "merge_commit_sha": "merged123",
        "body": "Fixes #7",
        "html_url": "https://example.test/pr/1",
    }
    if outcome == "closed":
        with pytest.raises(ValueError, match="closed issue"):
            review_merged_answer("demo", project, settings, state, pr)
        assert not any(call[:2] == ("issue", "create") for call in calls)
        return
    assert review_merged_answer("demo", project, settings, state, pr)
    run_id = f"answer-copy-1-{hashlib.sha256(b'merged123').hexdigest()[:16]}"
    record = state.load_run("answer-copy-review", run_id)
    assert record is not None
    assert record.metadata["assessment"]["score"] == 7
    assert (
        record.metadata["editorial_status"]
        == {
            "new": "changes_required",
            "none": "clean",
            "existing": "changes_required",
        }[outcome]
    )
    assert record.metadata["source_revision"] == "merged123"
    assert Path(record.metadata["assessment_path"]).exists()
    assert (
        record.metadata["result"]
        == {"new": "issue-created", "none": "no-findings", "existing": "existing-issue"}[outcome]
    )
    if outcome == "existing":
        assert ("issue", "edit", existing_url, "--add-label", "copy-fix") in calls
    assert not review_merged_answer("demo", project, settings, state, pr)
    assert sum(call[:2] == ("issue", "create") for call in calls) == (outcome == "new")


@pytest.mark.parametrize(
    ("verdict", "scope"),
    [("clean", None), ("needs_human_review", "examples"),
     ("needs_human_review", "repetition"), ("needs_human_review", "flow")],
)
def test_copy_fix_merge_gets_one_targeted_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, scope: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        pr_fixer=PrFixerConfig(log_dir=tmp_path / "logs"),
    )
    settings = CopyReviewConfig(page_globs=["answers/pages/*.ts"])
    state = StateRepository(tmp_path / "state.db", "demo", repo)
    payload = assessment(verdict=verdict)
    if verdict == "needs_human_review":
        for check in payload["coherence_checks"]:
            if check["scope"] == scope:
                check["status"] = "blocking"
                check["evidence"] = "Original fix is resolved but whole-article teaching remains weak"
        payload["limitations"] = ["The corrected outcome remains unclear."]
        payload["findings"] = [
            {
                "severity": "blocking",
                "category": "teaching",
                "location": "Worked example",
                "evidence": "The example never resolves the action.",
                "reader_impact": "A novice cannot use the demonstrated procedure.",
                "proposed_fix": "Show the resolved result or identify the missing evidence.",
            }
        ]

    def fake_json(_repo: Path, *args: str, **_kwargs: Any) -> Any:
        if args[0] == "api" and "contents/answers/pages/scene.ts" in args[1]:
            assert "ref=correction123" in args
            return {"content": base64.b64encode(b"Corrected full article").decode()}
        if args[0] == "api" and args[1].endswith("/issues/2/comments"):
            return []
        if args[:2] == ("issue", "view"):
            if args[2] == "7":
                return {"number": 7, "body": "Original reader goal: choose a useful procedure"}
            return {
                "number": 2,
                "title": "Correct the social example",
                "body": "<!-- dev-agents:answer-copy-review merge=abcdef123456 -->\nAcceptance criteria\nInitial request #7",
                "url": "https://github.com/owner/repo/issues/2",
                "state": "CLOSED",
                "labels": [{"name": "copy-fix"}],
            }
        return []

    calls: list[tuple[str, ...]] = []

    def fake_gh(_repo: Path, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ("pr", "diff"):
            return (
                "diff --git a/answers/pages/scene.ts b/answers/pages/scene.ts\n"
                "--- a/answers/pages/scene.ts\n+++ b/answers/pages/scene.ts\n"
            )
        return ""

    def fake_agent(_provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        assert "targeted verification" in prompt
        assert "Acceptance criteria" in prompt
        assert "Original reader goal: choose a useful procedure" in prompt
        kwargs["last_message_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["last_message_path"].write_text(json.dumps(payload))
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.copy_review.gh_json", fake_json)
    monkeypatch.setattr("dev_agents.copy_review.gh", fake_gh)
    monkeypatch.setattr("dev_agents.copy_review.run_agent", fake_agent)
    pr = {
        "number": 4,
        "title": "docs: Correct social example",
        "head": {"ref": "dev-agents/copy-fix-2"},
        "merge_commit_sha": "correction123",
        "body": "Fixes #2",
        "html_url": "https://github.com/owner/repo/pull/4",
    }

    assert review_merged_answer("demo", project, settings, state, pr)
    run_id = f"answer-copy-4-{hashlib.sha256(b'correction123').hexdigest()[:16]}"
    record = state.load_run("answer-copy-review", run_id)
    assert record is not None
    assert record.metadata["correction_verification"] is True
    assert record.metadata["assessment"]["coherence_checks"] == payload["coherence_checks"]
    assert record.metadata["originating_review_revision"] == "abcdef123456"
    assert record.metadata["result"] == (
        "correction-verified" if verdict == "clean" else "correction-needs-human-review"
    )
    assert any(args[:2] == ("api", "repos/owner/repo/issues/4/comments") for args in calls)
    assert not any(args[:2] == ("issue", "create") for args in calls)


def test_failed_editorial_run_is_persisted_and_reported_on_merged_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        pr_fixer=PrFixerConfig(log_dir=tmp_path / "logs"),
    )
    settings = CopyReviewConfig(page_globs=["answers/pages/*.ts"])
    state = StateRepository(tmp_path / "state.db", "demo", repo)
    comments: list[tuple[str, ...]] = []

    def fake_json(_repo: Path, *args: str, **_kwargs: Any) -> Any:
        if args[:2] == ("issue", "list"):
            return []
        if args[0] == "api" and "contents/answers/pages/scene.ts" in args[1]:
            return {"content": base64.b64encode(b"Full article").decode()}
        if args[0] == "api" and args[1].endswith("/issues/5/comments"):
            return []
        return []

    def fake_gh(_repo: Path, *args: str, **_kwargs: Any) -> str:
        comments.append(args)
        if args[:2] == ("pr", "diff"):
            return "diff --git a/answers/pages/scene.ts b/answers/pages/scene.ts\n"
        return ""

    monkeypatch.setattr("dev_agents.copy_review.gh_json", fake_json)
    monkeypatch.setattr("dev_agents.copy_review.gh", fake_gh)
    monkeypatch.setattr(
        "dev_agents.copy_review.run_agent", lambda *_args, **_kwargs: AgentResult(1, False)
    )
    pr = {
        "number": 5,
        "title": "answer: A new page",
        "merge_commit_sha": "failedmerge",
        "body": "",
        "html_url": "https://github.com/owner/repo/pull/5",
    }

    with pytest.raises(RuntimeError, match="reviewer failed"):
        review_merged_answer("demo", project, settings, state, pr)

    run_id = f"answer-copy-5-{hashlib.sha256(b'failedmerge').hexdigest()[:16]}"
    record = state.load_run("answer-copy-review", run_id)
    assert record is not None
    assert record.status == "failed"
    assert record.metadata["editorial_status"] == "failed"
    assert any(args[:2] == ("api", "repos/owner/repo/issues/5/comments") for args in comments)
