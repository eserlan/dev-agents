import json
import subprocess
from pathlib import Path

import pytest

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.pr_fixer.audit import AUDIT_LENSES
from dev_agents.pr_fixer.service import PrFixerService
from dev_agents.runtime.agent import AgentResult


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


def setup_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "staging")
    git(origin, "config", "user.email", "test@example.test")
    git(origin, "config", "user.name", "Test")
    (origin / "base.txt").write_text("base")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    git(origin, "checkout", "-b", "feature")
    (origin / "feature.txt").write_text("bug")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "feature")
    head = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "staging")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    return origin, repo, head


@pytest.mark.parametrize("mode", ["clean", "findings", "malformed", "mutation", "stale", "incomplete", "incomplete_findings", "fix_noop", "handoff"])
def test_auditor_and_fixer_are_separate_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    origin, repo, head = setup_repository(tmp_path)
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(
        state_path=tmp_path / "state.db", worktree_dir=tmp_path / "worktrees", log_dir=tmp_path / "logs",
    ))
    run_id = "review-42"
    service.state.claim_run("pr-review", run_id, metadata={"pull_request": 42, "head_sha": head})
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "dev_agents.pr_fixer._publish_pr_run_comment",
        lambda _repo, _number, _run_id, phase, body: comments.append((phase, body)) or True,
    )
    calls: list[str] = []

    def agent(_provider: str, prompt: str, **kwargs) -> AgentResult:
        worktree = kwargs["cwd"]
        output = kwargs["last_message_path"]
        if kwargs.get("read_only"):
            calls.append("auditor")
            assert "separate fixer owns implementation" in prompt
            assert kwargs["output_schema_path"].is_file()
            if mode == "malformed":
                output.write_text("not JSON")
                return AgentResult(0, False)
            findings = [] if mode in {"clean", "mutation", "incomplete"} else [{
                "severity": "high", "confidence": "high", "category": "security-privacy",
                "location": "feature.txt:1", "evidence": "The new branch bypasses validation",
                "impact": "Invalid input reaches the sensitive sink", "remediation": "Validate before use",
            }]
            audit = {
                "verdict": "findings" if findings else "clean", "findings": findings,
                "categories_checked": sorted(AUDIT_LENSES),
                "checks": [{"category": c, "status": "finding" if c == "security-privacy" and findings else "pass", "evidence": "Traced changed path"} for c in sorted(AUDIT_LENSES)],
                "limitations": [],
            }
            if mode == "mutation":
                (worktree / "feature.txt").write_text("unauthorised audit edit")
            if mode in {"incomplete", "incomplete_findings"}:
                audit["verdict"] = "incomplete"
                accessibility = next(check for check in audit["checks"] if check["category"] == "accessibility")
                accessibility.update(status="unverified", evidence="Contrast tokens were not resolvable")
            if mode == "stale":
                git(origin, "checkout", "feature")
                (origin / "external.txt").write_text("concurrent push")
                git(origin, "add", ".")
                git(origin, "commit", "-m", "external change")
                git(origin, "checkout", "staging")
            output.write_text(json.dumps(audit))
        else:
            calls.append("fixer")
            assert "Independent auditor report" in prompt and "feature.txt:1" in prompt
            fixes = []
            if mode != "fix_noop":
                (worktree / "feature.txt").write_text("fixed")
                git(worktree, "add", ".")
                git(worktree, "commit", "-m", "fix")
                git(worktree, "push", "origin", "HEAD:feature")
                fixes = [{"location": "feature.txt:1", "summary": "Validated before use"}]
            repair = {"verdict": "clean", "findings": [], "fixes": fixes, "categories_checked": [], "validation": ["focused check"]}
            output.write_text("DEV_AGENTS_REVIEW_REPORT_BEGIN\nFINDINGS: repaired\nFIXES: repaired\nREPORT_JSON: " + json.dumps(repair) + "\nDEV_AGENTS_REVIEW_REPORT_END\n")
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.pr_fixer.service.run_agent", agent)
    meta = {"headRefOid": head, **({"headRefName": "claude/feature"} if mode == "handoff" else {})}
    succeeded, report = service._fix(42, "feature", [], review_only=True, meta=meta, run_id=run_id)
    assert succeeded == (mode in {"clean", "findings", "incomplete_findings", "handoff"})
    assert calls == (["auditor", "fixer"] if mode in {"findings", "incomplete_findings", "fix_noop"} else ["auditor"])
    if mode == "findings":
        assert report["review_report"]["verdict"] == "findings"
        assert report["audit_report"]["findings"][0]["evidence"] == "The new branch bypasses validation"
        assert report["review_report"]["findings"][0]["evidence"] == "The new branch bypasses validation"
        assert service.state.get_run("pr-review", run_id).metadata["audit_telemetry"]["findings_count"] == 1
        assert service.state.get_run("pr-review", run_id).metadata["fixer_telemetry"]["returncode"] == 0
        assert [event.node for event in service.state.list_run_events("pr-review", run_id)][-3:] == ["auditor", "fixer", "fixer"]
        phases = [phase for phase, _body in comments]
        assert phases.index("review-findings") < phases.index("review-fixes-started")
        findings_body = dict(comments)["review-findings"]
        assert "**HIGH** · security-privacy — `feature.txt:1`" in findings_body
        assert "Suggested fix: Validate before use" in findings_body
        assert "reported above" in dict(comments)["review-fixes-started"]
    if mode == "handoff":
        assert report["handoff_agent"] == "claude"
        assert [phase for phase, _body in comments] == ["review-findings"]
    if mode == "incomplete_findings":
        assert "Not verified, needs a human check" in dict(comments)["review-findings"]
        assert "accessibility: Contrast tokens were not resolvable" in dict(comments)["review-findings"]
    if mode == "clean":
        assert "review-findings" not in [phase for phase, _body in comments]
    if mode == "stale":
        assert report["superseded_by"] != head


def test_later_reference_feedback_is_persisted_once_for_the_audited_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(state_path=tmp_path / "state.db"))
    service.state.claim_run("pr-review", "run-1", metadata={
        "pull_request": 42, "head_sha": "audited",
        "audit_report": {"verdict": "clean", "findings": []},
        "audit_telemetry": {"stage": "auditor"},
    })
    comments = [{"id": 5, "original_commit_id": "audited", "path": "view.ts", "line": 42, "user": {"login": "Copilot", "type": "Bot"}}]
    monkeypatch.setattr("dev_agents.pr_fixer._run", lambda *_args, **_kwargs: json.dumps([comments]))
    meta = {"author": {"login": "writer"}, "reviews": [{"author": {"login": "Copilot"}, "state": "COMMENTED"}]}
    service._track_audit_comparison(42, meta)
    comparison = service.state.get_run("pr-review", "run-1").metadata["audit_telemetry"]["copilot_overlap"]
    assert comparison["potential_false_negative_rate"] == 1
    before = len(service.state.list_run_events("pr-review", "run-1"))
    service._track_audit_comparison(42, meta)
    assert len(service.state.list_run_events("pr-review", "run-1")) == before


def test_own_fix_push_does_not_cancel_the_active_run(tmp_path: Path) -> None:
    from threading import Event

    origin, repo, _head = setup_repository(tmp_path)
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(
        state_path=tmp_path / "state.db", worktree_dir=tmp_path / "worktrees", log_dir=tmp_path / "logs",
    ))
    git(repo, "checkout", "feature")
    (repo / "feature.txt").write_text("fixed")
    git(repo, "commit", "-am", "fix")
    own_head = git(repo, "rev-parse", "HEAD")
    service.active.add(42)
    service.cancel_events[42] = Event()
    service.active_worktrees[42] = repo

    assert service.handle(42, new_head_sha=own_head) is False
    assert not service.cancel_events[42].is_set()
    assert 42 in service.rerun_requested

    git(origin, "checkout", "feature")
    (origin / "external.txt").write_text("concurrent push")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "external change")
    foreign_head = git(origin, "rev-parse", "HEAD")
    assert service.handle(42, new_head_sha=foreign_head) is False
    assert service.cancel_events[42].is_set()


def test_final_comments_explain_fix_pushes_and_supersession() -> None:
    from dev_agents.pr_fixer.comments import _review_completed_body

    report = {"report_valid": True, "review_report": {
        "verdict": "findings",
        "findings": [{"severity": "medium", "location": "a.ts:1", "impact": "Wrong price"}],
        "fixes": [{"location": "a.ts:1", "summary": "Parse the price with the shop precision"}],
    }}
    pushed = _review_completed_body(7, "run-7", report, old_sha="a" * 40, new_sha="b" * 40, fix_pushed=True, follow_up=True)
    assert "### 🤖 Luna fixes pushed" in pushed
    assert "- `a.ts:1`: Parse the price with the shop precision" in pushed
    assert "round 2/3" in pushed and "That review is expected" in pushed
    assert "Pushed fix commit `bbbbbbbbbbbb`" in pushed

    superseded = _review_completed_body(7, "run-7", report, superseded_by="c" * 40)
    assert "Someone else pushed `cccccccccccc`" in superseded
    assert "Nothing was pushed by this run" in superseded
    assert "Findings:" not in superseded and "What changed" not in superseded


def test_review_handoff_agent_matches_branch_prefix_or_commit_login() -> None:
    from dev_agents.pr_fixer.github_ops import _review_handoff_agent

    config = PrFixerConfig()
    assert _review_handoff_agent({"headRefName": "claude/merchant-stock"}, config) == "claude"
    by_login = {"headRefName": "feature", "commits": [
        {"authors": [{"login": "eserlan"}]}, {"authors": [{"login": "claude"}]},
    ]}
    assert _review_handoff_agent(by_login, config) == "claude"
    assert _review_handoff_agent({"headRefName": "feature", "commits": [{"authors": [{"login": "eserlan"}]}]}, config) is None
    assert _review_handoff_agent({"headRefName": "claude/x"}, PrFixerConfig(review_handoff_branch_prefixes=[], review_handoff_logins=[])) is None


def test_final_comment_explains_handed_off_findings() -> None:
    from dev_agents.pr_fixer.comments import _review_completed_body

    report = {"handoff_agent": "claude", "review_report": {"verdict": "findings", "findings": [], "fixes": []}}
    body = _review_completed_body(9, "run-9", report, old_sha="a" * 40, new_sha="a" * 40)
    assert "### 🤖 Luna review completed — findings left for claude" in body
    assert "Its next push gets a fresh review" in body
