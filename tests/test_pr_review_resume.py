"""A review cut off by a daemon restart resumes from its finished audit (no LangGraph checkpointer).

Interruptions are rare (about 0.1% of runs), so only the slow, read-only audit is resumed:
the retry for the same head reuses it and runs the repair gate, fixer and verify again.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.pr_fixer.audit import AUDIT_LENSES
from dev_agents.pr_fixer.service import PrFixerService, _interrupted_audit
from dev_agents.runtime.agent import AgentResult


class Restart(BaseException):
    """Stands in for the daemon process dying mid-run."""


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


FINDING = {
    "severity": "high", "confidence": "high", "category": "security-privacy",
    "requirement": "base.txt:1 requires input validation", "trigger": "Invalid input enters the new branch",
    "introduced_by": "feature.txt:1 bypasses validation", "location": "feature.txt:1",
    "evidence": "The new branch bypasses validation", "impact": "Invalid input reaches the sensitive sink",
    "remediation": "Validate before use",
}


def audit_report() -> str:
    return json.dumps({
        "summary": "Changes the feature branch.", "files": [{"path": "feature.txt", "description": "Adds the branch"}],
        "verdict": "findings", "findings": [FINDING], "categories_checked": sorted(AUDIT_LENSES),
        "checks": [{"category": c, "status": "finding" if c == "security-privacy" else "pass", "evidence": "Traced"}
                   for c in sorted(AUDIT_LENSES)],
        "limitations": [],
    })


@pytest.mark.parametrize("crash_in", ["fixer", "auditor"])
def test_a_review_cut_off_by_a_restart_resumes_from_its_finished_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_in: str,
) -> None:
    origin, repo, head = setup_repository(tmp_path)
    config = PrFixerConfig(state_path=tmp_path / "state.db", worktree_dir=tmp_path / "worktrees",
                           log_dir=tmp_path / "logs", review_claim_window_seconds=0)
    service = PrFixerService("demo", ProjectConfig(repo=repo, github="owner/repo"), config)
    monkeypatch.setattr("dev_agents.pr_fixer._publish_pr_run_comment", lambda *_args: True)
    calls: list[str] = []
    crashes = [crash_in]

    def agent(_provider: str, prompt: str, **kwargs) -> AgentResult:
        worktree, output = kwargs["cwd"], kwargs["last_message_path"]
        stage = "auditor" if kwargs.get("read_only") else "fixer"
        calls.append(stage)
        if crashes and crashes[0] == stage:
            crashes.pop()
            raise Restart
        if stage == "auditor":
            output.write_text(audit_report())
            return AgentResult(0, False)
        (worktree / "feature.txt").write_text("fixed")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "fix")
        git(worktree, "push", "origin", "HEAD:feature")
        repair = {"verdict": "clean", "findings": [], "categories_checked": [], "validation": ["focused check"],
                  "fixes": [{"location": "feature.txt:1", "summary": "Validated before use"}]}
        output.write_text("DEV_AGENTS_REVIEW_REPORT_BEGIN\nFINDINGS: repaired\nFIXES: repaired\nREPORT_JSON: "
                          + json.dumps(repair) + "\nDEV_AGENTS_REVIEW_REPORT_END\n")
        return AgentResult(0, False)

    monkeypatch.setattr("dev_agents.pr_fixer.service.run_agent", agent)
    meta = {"headRefOid": head, "headRefName": "feature", "state": "OPEN", "baseRefName": "staging",
            "mergeable": "MERGEABLE", "labels": [], "reviews": [], "checks": []}
    monkeypatch.setattr("dev_agents.pr_fixer._feedback", lambda *_args: (meta, []))
    monkeypatch.setattr("dev_agents.pr_fixer.repository_slug", lambda *_args: "owner/repo")
    original_run = __import__("dev_agents.pr_fixer", fromlist=["_run"])._run
    monkeypatch.setattr("dev_agents.pr_fixer._run", lambda repo_path, *args, **kwargs: "[[]]"
                        if args[:2] == ("gh", "api") else original_run(repo_path, *args, **kwargs))

    first = service._collect_feedback({"number": 42})
    assert first["claimed"] and first["review_only"]
    run_id = first["run_id"]
    with pytest.raises(Restart):
        service._fix(42, "feature", [], review_only=True, meta=meta, run_id=run_id)

    # The next daemon start finds the claim's worker gone and fails the run as interrupted.
    restarted = PrFixerService("demo", service.project, config)
    monkeypatch.setattr("dev_agents.runtime.state._worker_is_alive", lambda _metadata: False)
    assert [run.run_id for run in restarted.state.recover_interrupted_runs()] == [run_id]
    monkeypatch.setattr("dev_agents.runtime.state._worker_is_alive", lambda _metadata: True)

    retry = restarted._collect_feedback({"number": 42})
    assert retry["claimed"] and retry["run_id"] == run_id
    succeeded, report = restarted._fix(42, "feature", [], review_only=True, meta=meta, run_id=run_id)

    assert succeeded and report["review_report"]["verdict"] == "findings"
    events = [(event.node, event.event) for event in restarted.state.list_run_events("pr-review", run_id)]
    record = restarted.state.get_run("pr-review", run_id)
    if crash_in == "fixer":
        # The audit is not repeated, and the cut-off fixer still counts against the budget.
        assert calls == ["auditor", "fixer", "fixer"]
        assert ("auditor", "audit_resumed") in events
        assert record.metadata["review_repair_attempts"] == 2
    else:
        # No finished audit to resume from: the retry audits again.
        assert calls == ["auditor", "auditor", "fixer"]
        assert ("auditor", "audit_resumed") not in events
    assert git(origin, "show", "feature:feature.txt") == "fixed"


def test_only_an_interrupted_run_with_an_audit_of_the_same_head_and_round_resumes() -> None:
    telemetry = {"verdict": "findings", "reviewed_head_sha": "abc", "review_round": 0}

    def run(status: str = "failed", **metadata):
        return SimpleNamespace(status=status, metadata={
            "interrupted": True, "audit_report": {"verdict": "findings"}, "audit_telemetry": telemetry, **metadata})

    assert _interrupted_audit(run(), "abc", 0)["resume_interrupted_audit"] is True
    assert _interrupted_audit(None, "abc", 0) == {}
    assert _interrupted_audit(run(interrupted=False), "abc", 0) == {}
    assert _interrupted_audit(run(status="completed"), "abc", 0) == {}
    assert _interrupted_audit(run(), "other-head", 0) == {}
    assert _interrupted_audit(run(), "abc", 1) == {}
    assert _interrupted_audit(run(audit_report=None), "abc", 0) == {}
    assert _interrupted_audit(run(audit_telemetry={**telemetry, "verdict": "failed"}), "abc", 0) == {}
    assert _interrupted_audit(run(review_repair_attempts=1), "abc", 0)["review_repair_attempts"] == 1
