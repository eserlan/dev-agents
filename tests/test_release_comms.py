from pathlib import Path

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.workflows.release_comms import (
    EvaluatorResult,
    WriterResult,
    extract_json_block,
    run_release_comms,
)


def test_extract_json_block_fenced_and_unfenced() -> None:
    fenced = "```json\n{\"postworthy\": true, \"reason\": \"good\"}\n```"
    assert extract_json_block(fenced) == {"postworthy": True, "reason": "good"}

    unfenced = "some text {\"postworthy\": false, \"reason\": \"bad\"} trailing"
    assert extract_json_block(unfenced) == {"postworthy": False, "reason": "bad"}

    assert extract_json_block("not json at all") is None


def test_run_release_comms_dry_run_does_not_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(tracking_issue=100),
    )

    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha123", "prevsha000"),
    )

    eval_result = EvaluatorResult(
        postworthy=True,
        reason="Real changes",
        importance="high",
        features=[{"name": "Feature A", "why_users_care": "Speed", "bluesky_worthy": True}],
        recommended_channels=["bluesky", "discord"],
    )
    writer_result = WriterResult(
        bluesky=[{"pageUrl": "https://example.com/a", "text": "Draft A"}],
        discord="Discord draft",
    )

    result = run_release_comms(
        project,
        "demo",
        "12345",
        dry_run=True,
        publish_approved=False,
        evaluator_result=eval_result,
        writer_result=writer_result,
    )

    assert result.promote_run_id == "12345"
    assert result.new_sha == "newsha123"
    assert result.previous_sha == "prevsha000"
    assert result.postworthy is True
    assert result.completed is True
    assert result.drafts is not None
    assert len(result.drafts["bluesky"]) == 1
    # Dry run should not record external publication URLs
    assert result.published["bluesky"] == []


def test_run_release_comms_not_postworthy_skips_drafts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")

    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha123", "prevsha000"),
    )

    eval_result = EvaluatorResult(
        postworthy=False,
        reason="Only chore updates",
        importance="low",
    )

    result = run_release_comms(
        project,
        "demo",
        "999",
        dry_run=True,
        evaluator_result=eval_result,
    )

    assert result.postworthy is False
    assert result.drafts is None
    assert result.completed is True
