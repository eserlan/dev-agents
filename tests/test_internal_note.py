from pathlib import Path
from typing import Any

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows.release_comms import EvaluatorResult, WriterResult, run_release_comms
from dev_agents.workflows.release_publish import (
    PublicationError,
    PublicationReceipt,
    publish_internal_note,
)

NOTE = "Cloud Backup now uploads only changed entities, so big vaults sync much faster."


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.database = tmp_path / "release.db"
        self.project = ProjectConfig(
            repo=self.repo,
            github="owner/repo",
            release_comms=ReleaseCommsConfig(state_path=self.database, image_generation=False),
        )
        self.bluesky: list[str] = []
        self.discord: list[dict[str, Any]] = []
        self.fail_note = False
        monkeypatch.setattr(
            "dev_agents.workflows.release_comms.resolve_promote_shas",
            lambda repo, run_id: ("newsha", "prevsha"),
        )
        monkeypatch.setattr(
            "dev_agents.workflows.release_comms.comment_tracking_issue", lambda **kwargs: None
        )
        monkeypatch.setattr("dev_agents.workflows.release_publish.publish_bluesky", self._bluesky)
        monkeypatch.setattr("dev_agents.workflows.release_publish.publish_discord", self._discord)

    def _bluesky(self, **kwargs: Any) -> PublicationReceipt:
        self.bluesky.append(str(kwargs["page_url"]))
        return PublicationReceipt("bluesky", "bluesky", str(kwargs["page_url"]), "https://bsky.test/1")

    def _discord(self, **kwargs: Any) -> list[PublicationReceipt]:
        self.discord.append(kwargs)
        page_url = str(kwargs.get("page_url", ""))
        if self.fail_note and page_url.startswith("internal-note:"):
            raise PublicationError("Discord webhook execution failed")
        return [PublicationReceipt("discord", "main-community", page_url, "https://discord.test/1")]

    def notes(self) -> list[dict[str, Any]]:
        return [call for call in self.discord if str(call.get("page_url", "")).startswith("internal-note:")]

    def run(self, run_id: str, evaluation: EvaluatorResult, writer: WriterResult | None = None, **kw: Any) -> Any:
        events: list[tuple[str, dict[str, Any]]] = []
        result = run_release_comms(
            self.project, "demo", run_id,
            evaluator_result=evaluation, writer_result=writer,
            on_event=lambda event, payload: events.append((event, payload)),
            **kw,
        )
        self.events = dict(events)
        return result


def test_a_technical_only_release_goes_to_discord_and_nowhere_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, tmp_path)
    evaluation = EvaluatorResult(postworthy=False, reason="technical only", internal_note=NOTE)

    result = harness.run("run-1", evaluation, dry_run=False, publish_approved=True)

    assert result.completed is True and result.postworthy is False
    assert [call["message"] for call in harness.notes()] == [f"**Dev note:** {NOTE}"]
    assert harness.bluesky == []
    receipts = StateRepository(harness.database, "demo", harness.repo).list_run_publications(
        "release-comms", "run-1"
    )
    assert [(r.channel, r.page_url, r.status) for r in receipts] == [
        ("discord", "internal-note:run-1", "published")
    ]
    assert harness.events["internal_note"] == {"text": NOTE, "sent": True}


def test_a_note_alongside_public_drafts_is_sent_once_and_public_posts_still_go_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, tmp_path)
    evaluation = EvaluatorResult(
        postworthy=True, reason="hub", recommended_channels=["bluesky"], internal_note=NOTE
    )
    drafts = WriterResult(
        bluesky=[{"pageUrl": "https://example.com/a", "text": "A", "image": "og/a.jpg"}]
    )

    result = harness.run("run-2", evaluation, drafts, dry_run=False, publish_approved=True)

    assert result.completed is True
    assert harness.bluesky == ["https://example.com/a"]
    assert len(harness.notes()) == 1


def test_a_failing_discord_note_does_not_block_public_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, tmp_path)
    harness.fail_note = True
    evaluation = EvaluatorResult(
        postworthy=True, reason="hub", recommended_channels=["bluesky"], internal_note=NOTE
    )
    drafts = WriterResult(
        bluesky=[{"pageUrl": "https://example.com/a", "text": "A", "image": "og/a.jpg"}]
    )

    result = harness.run("run-3", evaluation, drafts, dry_run=False, publish_approved=True)

    assert result.completed is True
    assert harness.bluesky == ["https://example.com/a"]
    assert "internal_note_failed" in harness.events


def test_dry_run_reports_the_note_without_sending_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, tmp_path)
    evaluation = EvaluatorResult(postworthy=False, reason="technical only", internal_note=NOTE)

    harness.run("run-4", evaluation)

    assert harness.discord == []
    assert harness.events["internal_note"] == {"text": NOTE, "sent": False}


def test_an_already_posted_note_is_not_sent_again(tmp_path: Path) -> None:
    posted: set[tuple[str, str]] = {("discord", "main-community\ninternal-note:run-5")}

    assert (
        publish_internal_note(
            repo=tmp_path, note=NOTE, source_id="run-5", env={}, dry_run=True, already_published=posted
        )
        == []
    )
    assert publish_internal_note(
        repo=tmp_path, note="  ", source_id="run-6", env={}, dry_run=True, already_published=set()
    ) == []


def test_no_note_means_no_discord_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = Harness(monkeypatch, tmp_path)

    harness.run(
        "run-7", EvaluatorResult(postworthy=False, reason="chore"), dry_run=False, publish_approved=True
    )

    assert harness.discord == []
    assert "internal_note" not in harness.events
