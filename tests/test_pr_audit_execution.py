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


@pytest.mark.parametrize("mode", ["clean", "findings", "malformed", "mutation", "stale", "incomplete", "fix_noop"])
def test_auditor_and_fixer_are_separate_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    origin, repo, head = setup_repository(tmp_path)
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), PrFixerConfig(
        state_path=tmp_path / "state.db", worktree_dir=tmp_path / "worktrees", log_dir=tmp_path / "logs",
    ))
    run_id = "review-42"
    service.state.claim_run("pr-review", run_id, metadata={"pull_request": 42, "head_sha": head})
    monkeypatch.setattr("dev_agents.pr_fixer._publish_pr_run_comment", lambda *_args: True)
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
            if mode == "incomplete":
                audit["verdict"] = "incomplete"
                audit["checks"][0]["status"] = "unverified"
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
    succeeded, report = service._fix(42, "feature", [], review_only=True, meta={"headRefOid": head}, run_id=run_id)
    assert succeeded == (mode in {"clean", "findings"})
    assert calls == (["auditor", "fixer"] if mode in {"findings", "fix_noop"} else ["auditor"])
    if mode == "findings":
        assert report["review_report"]["verdict"] == "findings"
        assert report["audit_report"]["findings"][0]["evidence"] == "The new branch bypasses validation"
        assert report["review_report"]["findings"][0]["evidence"] == "The new branch bypasses validation"
        assert service.state.get_run("pr-review", run_id).metadata["audit_telemetry"]["findings_count"] == 1
        assert service.state.get_run("pr-review", run_id).metadata["fixer_telemetry"]["returncode"] == 0
        assert [event.node for event in service.state.list_run_events("pr-review", run_id)][-3:] == ["auditor", "fixer", "fixer"]
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
