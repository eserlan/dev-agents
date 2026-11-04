"""Release-comms publishes each channel of a message as its own LangGraph branch."""

import socket
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import RetryPolicy

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows import release_comms
from dev_agents.workflows.release_comms import (
    EvaluatorResult,
    TransientChannelError,
    WriterResult,
    run_release_comms,
)
from dev_agents.workflows.release_publish import PublicationError, is_transient_publication_error


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example.com", code, "status", {}, None)  # type: ignore[arg-type]


def wrapped(cause: BaseException) -> PublicationError:
    try:
        raise PublicationError("HTTP POST failed") from cause
    except PublicationError as error:
        return error


@pytest.mark.parametrize(
    ("cause", "transient"),
    [
        (http_error(429), True),
        (http_error(503), True),
        (http_error(500), False),
        (http_error(400), False),
        (urllib.error.URLError(ConnectionRefusedError()), True),
        (urllib.error.URLError(socket.gaierror()), True),
        (urllib.error.URLError(TimeoutError()), False),
        (TimeoutError(), False),
        (ConnectionResetError(), False),
    ],
)
def test_only_failures_that_left_no_post_are_transient(cause: BaseException, transient: bool) -> None:
    assert is_transient_publication_error(wrapped(cause)) is transient


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        release_comms,
        "CHANNEL_RETRY",
        RetryPolicy(max_attempts=release_comms.CHANNEL_ATTEMPTS, initial_interval=0.0, jitter=False,
                    retry_on=TransientChannelError),
    )
    monkeypatch.setattr(release_comms, "comment_tracking_issue", lambda **kwargs: None)
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish._discord_config", lambda repo: [{"id": "main-community"}]
    )
    return ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(state_path=tmp_path / "release.db", image_generation=False),
    )


def publish(project: ProjectConfig, channels: list[str], run_id: str = "run-1") -> Any:
    return run_release_comms(
        project,
        "demo",
        run_id,
        dry_run=False,
        publish_approved=True,
        evaluator_result=EvaluatorResult(postworthy=True, reason="Launch", recommended_channels=channels),
        writer_result=WriterResult(
            bluesky=[{"pageUrl": "https://example.com/a", "text": "A", "image": "https://example.com/a.png"}]
        ),
        new_sha="sha",
        previous_sha="sha",
        notify_tracking_issue=False,
        exact_channels=True,
    )


def receipt(channel: str, destination: str | None = None) -> Any:
    from dev_agents.workflows.release_publish import PublicationReceipt

    return PublicationReceipt(channel, destination or channel, "https://example.com/a", f"https://{channel}/post")


def install(monkeypatch: pytest.MonkeyPatch, behaviours: dict[str, list[Any]]) -> dict[str, int]:
    """Each call to a channel pops its next behaviour: an exception to raise, or success."""
    calls: dict[str, int] = {}

    def fake(job: Any, *, on_receipt: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls[job.channel] = calls.get(job.channel, 0) + 1
        outcome = behaviours[job.channel].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        sent = receipt(job.channel)
        on_receipt(sent)
        return [sent.__dict__]

    monkeypatch.setattr(release_comms, "publish_channel_job", fake)
    return calls


def published_channels(project: ProjectConfig, run_id: str = "run-1") -> set[str]:
    assert project.release_comms is not None and project.release_comms.state_path is not None
    state = StateRepository(project.release_comms.state_path, "demo", project.repo)
    return {record.channel for record in state.list_run_publications("release-comms", run_id)}


def test_one_failing_channel_does_not_stop_its_siblings(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig
) -> None:
    calls = install(monkeypatch, {
        "bluesky": [None],
        "instagram": [wrapped(http_error(400))],
        "discord": [None],
    })

    result = publish(project, ["bluesky", "instagram", "discord"])

    assert calls == {"bluesky": 1, "instagram": 1, "discord": 1}
    assert result.completed is False
    assert [item["channel"] for item in result.published["bluesky"]] == ["bluesky"]
    assert result.published["instagram"] == []
    assert published_channels(project) == {"bluesky", "discord"}


def test_transient_failure_is_retried_and_recovers(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig
) -> None:
    calls = install(monkeypatch, {"bluesky": [wrapped(http_error(503)), None], "instagram": [None]})

    result = publish(project, ["bluesky", "instagram"])

    assert calls == {"bluesky": 2, "instagram": 1}
    assert result.completed is True
    assert published_channels(project) == {"bluesky", "instagram"}


def test_retries_stop_after_the_last_attempt_without_failing_the_run(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig
) -> None:
    calls = install(monkeypatch, {
        "bluesky": [wrapped(http_error(503)) for _ in range(release_comms.CHANNEL_ATTEMPTS)],
        "instagram": [None],
    })

    result = publish(project, ["bluesky", "instagram"])

    assert calls == {"bluesky": release_comms.CHANNEL_ATTEMPTS, "instagram": 1}
    assert result.completed is False
    assert published_channels(project) == {"instagram"}


@pytest.mark.parametrize(
    ("channel", "error"),
    [
        ("bluesky", wrapped(TimeoutError())),  # may have posted
        ("discord", wrapped(http_error(503))),  # several destinations: a rerun could repeat one
    ],
)
def test_unsafe_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig, channel: str, error: BaseException
) -> None:
    calls = install(monkeypatch, {channel: [error, None]})

    result = publish(project, [channel])

    assert calls == {channel: 1}
    assert result.completed is False


def test_dry_run_plans_nothing_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig
) -> None:
    calls = install(monkeypatch, {"bluesky": [None]})

    result = run_release_comms(
        project, "demo", "dry", dry_run=True,
        evaluator_result=EvaluatorResult(postworthy=True, reason="Launch", recommended_channels=["bluesky"]),
        writer_result=WriterResult(bluesky=[{"pageUrl": "https://example.com/a", "text": "A"}]),
        new_sha="sha", previous_sha="sha", notify_tracking_issue=False,
    )

    assert calls == {}
    assert result.completed is True
