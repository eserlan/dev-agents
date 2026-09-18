import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import DuplicateRunError, StateRepository
from dev_agents.workflows.release_comms import (
    EvaluatorResult,
    WriterResult,
    extract_json_block,
    resolve_promote_shas,
    run_release_comms,
)
from dev_agents.workflows.release_publish import PublicationReceipt


def test_extract_json_block_fenced_and_unfenced() -> None:
    fenced = '```json\n{"postworthy": true, "reason": "good"}\n```'
    assert extract_json_block(fenced) == {"postworthy": True, "reason": "good"}

    unfenced = 'some text {"postworthy": false, "reason": "bad"} trailing'
    assert extract_json_block(unfenced) == {"postworthy": False, "reason": "bad"}

    assert extract_json_block("not json at all") is None


def test_run_release_comms_dry_run_does_not_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_run_release_comms_not_postworthy_skips_drafts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_run_release_comms_claims_promote_run_and_respects_configured_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "custom-state.db"
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(state_path=database),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )

    evaluation = EvaluatorResult(postworthy=True, reason="Launch")
    run_release_comms(
        project, "demo", "123", evaluator_result=evaluation, writer_result=WriterResult()
    )

    with pytest.raises(DuplicateRunError):
        run_release_comms(project, "demo", "123", evaluator_result=evaluation)
    record = StateRepository(database, "demo", repo).get_run("release-comms", "123")
    assert record is not None
    assert record.status == "completed"
    assert record.attempt == 1
    assert database.exists()
    assert not (tmp_path / "custom-state.db.db").exists()


def test_run_release_comms_not_postworthy_is_rejected_and_permanent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A not-postworthy run is 'rejected', not 'completed' -- distinguishable in the
    state DB from a real publish -- but just as permanent: previous_sha is now
    pinned at first resolution, so a rejection is a stable verdict against a fixed
    diff, not something that should be silently re-run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "release.db"
    project = ProjectConfig(
        repo=repo, github="owner/repo", release_comms=ReleaseCommsConfig(state_path=database)
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )

    evaluation = EvaluatorResult(postworthy=False, reason="chore")
    first = run_release_comms(project, "demo", "456", evaluator_result=evaluation)
    assert first.completed is True

    record = StateRepository(database, "demo", repo).get_run("release-comms", "456")
    assert record is not None
    assert record.status == "rejected"

    with pytest.raises(DuplicateRunError):
        run_release_comms(project, "demo", "456", evaluator_result=evaluation)


def test_live_release_comms_schedules_and_resumes_publications(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "release.db"
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(
            state_path=database,
            image_generation=False,
            publication_delay_min_seconds=900,
            publication_delay_max_seconds=1800,
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.comment_tracking_issue", lambda **kwargs: None
    )
    published_pages: list[str] = []

    def fake_publish(**kwargs: object) -> PublicationReceipt:
        page_url = str(kwargs["page_url"])
        published_pages.append(page_url)
        return PublicationReceipt("bluesky", "bluesky", page_url, f"https://bsky.test/{len(published_pages)}")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_bluesky", fake_publish)
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_discord",
        lambda **kwargs: [
            PublicationReceipt(
                "discord", "main-community", str(kwargs["page_url"]), "https://discord.test/1"
            )
        ],
    )
    evaluation = EvaluatorResult(
        postworthy=True, reason="Launch", recommended_channels=["bluesky"]
    )
    drafts = WriterResult(
        bluesky=[
            {"pageUrl": "https://example.com/a", "text": "A", "image": "og/a.jpg"},
            {"pageUrl": "https://example.com/b", "text": "B", "image": "og/b.jpg"},
        ]
    )

    first = run_release_comms(
        project, "demo", "release-1", dry_run=False, publish_approved=True,
        evaluator_result=evaluation, writer_result=drafts,
    )
    assert first.completed is False
    assert first.scheduled is True
    assert published_pages == ["https://example.com/a"]
    state = StateRepository(database, "demo", repo)
    record = state.get_run("release-comms", "release-1")
    assert record is not None
    assert record.status == "scheduled"
    assert record.metadata["_release_comms_resume"]["writer_result"]["bluesky"]
    with pytest.raises(DuplicateRunError, match="scheduled for"):
        run_release_comms(project, "demo", "release-1", dry_run=False, publish_approved=True)

    state.update_run(
        "release-comms",
        "release-1",
        status="scheduled",
        metadata={"next_publication_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    second = run_release_comms(
        project, "demo", "release-1", dry_run=False, publish_approved=True
    )
    assert second.completed is True
    assert second.scheduled is False
    assert published_pages == ["https://example.com/a", "https://example.com/b"]


def test_failed_release_comms_persists_drafts_and_retries_without_regenerating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed run (e.g. an unresolved image) must be retryable like a scheduled one:
    content already drafted should be reused, not regenerated, on the next attempt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "release.db"
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(state_path=database, image_generation=False),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.comment_tracking_issue", lambda **kwargs: None
    )

    evaluator_calls = 0

    def fake_evaluator_pass(**kwargs: object) -> EvaluatorResult:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return EvaluatorResult(
            postworthy=True, reason="Launch", recommended_channels=["bluesky"]
        )

    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_evaluator_pass", fake_evaluator_pass
    )

    evaluation = EvaluatorResult(
        postworthy=True, reason="Launch", recommended_channels=["bluesky"]
    )
    # An unresolved "capture:" image with no override raises a PublicationError,
    # which publish_release_drafts catches as a per-draft error (not an exception).
    drafts = WriterResult(
        bluesky=[{"pageUrl": "https://example.com/a", "text": "A", "image": "capture:shot.png"}]
    )

    first = run_release_comms(
        project, "demo", "release-1", dry_run=False, publish_approved=True,
        evaluator_result=evaluation, writer_result=drafts,
    )
    assert first.completed is False
    assert first.scheduled is False

    state = StateRepository(database, "demo", repo)
    record = state.get_run("release-comms", "release-1")
    assert record is not None
    assert record.status == "failed"
    resume = record.metadata["_release_comms_resume"]
    assert resume["writer_result"]["bluesky"][0]["pageUrl"] == "https://example.com/a"

    # Retrying without re-supplying evaluator/writer results must reuse the persisted
    # drafts (evaluator/writer passes must not run again) rather than regenerate them.
    second = run_release_comms(project, "demo", "release-1", dry_run=False, publish_approved=True)
    assert second.drafts is not None
    assert second.drafts["bluesky"][0]["pageUrl"] == "https://example.com/a"
    assert evaluator_calls == 0


def test_failed_release_comms_pins_shas_and_does_not_reresolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """resolve_promote_shas must run once per run, not once per retry -- otherwise
    later promotions/reverts on main shift what 'previous' means and a real release
    can flip to a false 'net revert' the longer a retry is delayed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "release.db"
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(state_path=database, image_generation=False),
    )
    resolve_calls = 0

    def fake_resolve(r: Path, run_id: str) -> tuple[str, str]:
        nonlocal resolve_calls
        resolve_calls += 1
        return "newsha", "prevsha"

    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas", fake_resolve
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.comment_tracking_issue", lambda **kwargs: None
    )

    evaluation = EvaluatorResult(
        postworthy=True, reason="Launch", recommended_channels=["bluesky"]
    )
    drafts = WriterResult(
        bluesky=[{"pageUrl": "https://example.com/a", "text": "A", "image": "capture:shot.png"}]
    )

    first = run_release_comms(
        project, "demo", "release-1", dry_run=False, publish_approved=True,
        evaluator_result=evaluation, writer_result=drafts,
    )
    assert first.completed is False
    assert resolve_calls == 1

    record = StateRepository(database, "demo", repo).get_run("release-comms", "release-1")
    assert record is not None
    assert record.metadata["_release_comms_resume"]["new_sha"] == "newsha"
    assert record.metadata["_release_comms_resume"]["previous_sha"] == "prevsha"

    second = run_release_comms(project, "demo", "release-1", dry_run=False, publish_approved=True)
    assert second.new_sha == "newsha"
    assert second.previous_sha == "prevsha"
    assert resolve_calls == 1


def _ok(stdout: str) -> Any:
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_resolve_promote_shas_prefers_gh_over_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> Any:
        if args[0] == "git":
            raise AssertionError(f"gh output should suffice, unexpected: {args}")
        if args[2] == "view":
            return _ok("newsha123\n")
        return _ok(json.dumps([{"databaseId": 123, "headSha": "prevsha000"}]))

    monkeypatch.setattr("dev_agents.workflows.release_comms.subprocess.run", fake_run)
    assert resolve_promote_shas(tmp_path, "999") == ("newsha123", "prevsha000")


def test_resolve_promote_shas_timeout_falls_back_to_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> Any:
        if args[0] == "gh":
            raise subprocess.TimeoutExpired(cmd=args, timeout=60)
        if args == ["git", "rev-parse", "HEAD"]:
            return _ok("headsha\n")
        if args == ["git", "rev-parse", "HEAD~1"]:
            return _ok("prevsha\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("dev_agents.workflows.release_comms.subprocess.run", fake_run)
    assert resolve_promote_shas(tmp_path, "123") == ("headsha", "prevsha")


def test_resolve_promote_shas_raises_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args, timeout=60)

    monkeypatch.setattr("dev_agents.workflows.release_comms.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="could not resolve"):
        resolve_promote_shas(tmp_path, "123")


def test_progress_events_trace_phases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    events: list[tuple[str, dict[str, Any]]] = []
    result = run_release_comms(
        project,
        "demo",
        "123",
        dry_run=True,
        evaluator_result=EvaluatorResult(
            postworthy=True, reason="Launch", importance="high", features=[{"name": "F"}]
        ),
        writer_result=WriterResult(
            bluesky=[{"pageUrl": "", "text": "Hi"}],
            discord="Hi",
        ),
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.completed is True
    assert [event for event, _ in events] == [
        "resolved",
        "evaluated",
        "routed",
        "drafted",
        "published",
    ]
    by_name = dict(events)
    assert by_name["resolved"] == {"new_sha": "newsha", "previous_sha": "prevsha"}
    assert by_name["evaluated"]["postworthy"] is True
    assert by_name["evaluated"]["reason"] == "Launch"
    assert by_name["drafted"] == {
        "bluesky": 1,
        "github_discussions": 0,
        "discord": True,
    }
    assert by_name["published"] == {"completed": True, "postworthy": True}


def test_progress_events_skip_drafts_when_not_postworthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    events: list[tuple[str, dict[str, Any]]] = []
    run_release_comms(
        project,
        "demo",
        "123",
        dry_run=True,
        evaluator_result=EvaluatorResult(postworthy=False, reason="Chores"),
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert [event for event, _ in events] == [
        "resolved",
        "evaluated",
        "routed",
        "drafts_skipped",
        "published",
    ]
