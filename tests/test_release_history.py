from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows.release_comms import EvaluatorResult, WriterResult, run_release_comms
from dev_agents.workflows.release_history import (
    format_recent_posts,
    page_slug,
    recent_announcements,
    repeated_publication_keys,
)
from dev_agents.workflows.release_publish import PublicationReceipt

OLD_PAGE = "https://codexcryptica.com/tools/holiday-festival-generator"
MOVED_PAGE = "https://codexcryptica.com/generators/holiday-festival-generator"


def _announce(
    state: StateRepository,
    run_id: str,
    page_url: str,
    text: str,
    *,
    channels: tuple[str, ...] = ("bluesky", "discord"),
) -> None:
    state.claim_run("release-comms", run_id)
    for channel in channels:
        state.record_publication(
            "release-comms",
            run_id,
            channel,
            destination="main-community" if channel == "discord" else channel,
            page_url=page_url,
        )
    state.complete_run(
        "release-comms",
        run_id,
        metadata={"drafts": {"bluesky": [{"pageUrl": page_url, "text": text}]}},
    )


def test_page_slug_ignores_path_prefix_query_and_case() -> None:
    assert page_slug(OLD_PAGE) == page_slug(MOVED_PAGE) == "holiday-festival-generator"
    assert page_slug("https://example.com/a/Guide/?utm=1#top") == "guide"
    assert page_slug("https://example.com/") == ""
    assert page_slug("") == ""


def test_recent_announcements_cover_other_runs_within_the_window(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "release.db", "demo", tmp_path)
    _announce(state, "old-release", OLD_PAGE, "My villages all had the same harvest festival.")
    _announce(state, "current", "https://example.com/current", "Being written right now")

    found = recent_announcements(state, exclude_run_id="current", days=14)

    assert [item.run_id for item in found] == ["old-release"]
    assert found[0].channels == ("bluesky", "discord")
    assert found[0].summary == "My villages all had the same harvest festival."
    later = datetime.now(UTC) + timedelta(days=15)
    assert recent_announcements(state, exclude_run_id="current", days=14, now=later) == []


def test_format_recent_posts_lists_page_channels_and_text(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "release.db", "demo", tmp_path)
    assert format_recent_posts([]) == "(none)"
    _announce(state, "old-release", OLD_PAGE, "My villages all had the same harvest festival.")

    block = format_recent_posts(recent_announcements(state, exclude_run_id="new"))

    assert OLD_PAGE in block
    assert "bluesky, discord" in block
    assert '"My villages all had the same harvest festival."' in block


def test_repeat_is_detected_after_a_route_move(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "release.db", "demo", tmp_path)
    _announce(state, "old-release", OLD_PAGE, "Festival generator")
    announcements = recent_announcements(state, exclude_run_id="new")

    keys, runs = repeated_publication_keys(announcements, [MOVED_PAGE, "https://example.com/other"])

    assert runs == {"old-release"}
    assert ("bluesky", MOVED_PAGE) in keys
    assert ("discord", f"main-community\n{MOVED_PAGE}") in keys
    assert not any("other" in key for _, key in keys)


def test_a_new_release_does_not_repost_a_page_announced_by_an_earlier_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    posted: list[str] = []

    def fake_bluesky(**kwargs: object) -> PublicationReceipt:
        page_url = str(kwargs["page_url"])
        posted.append(page_url)
        return PublicationReceipt("bluesky", "bluesky", page_url, f"https://bsky.test/{len(posted)}")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_bluesky", fake_bluesky)
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

    def drafts(*urls: str) -> WriterResult:
        return WriterResult(
            bluesky=[{"pageUrl": url, "text": url, "image": "og/a.jpg"} for url in urls]
        )

    first = run_release_comms(
        project, "demo", "release-1", dry_run=False, publish_approved=True,
        evaluator_result=evaluation, writer_result=drafts(OLD_PAGE),
    )
    assert first.completed is True
    assert posted == [OLD_PAGE]

    events: list[tuple[str, dict[str, object]]] = []
    second = run_release_comms(
        project, "demo", "release-2", dry_run=False, publish_approved=True,
        evaluator_result=evaluation,
        writer_result=drafts(MOVED_PAGE, "https://codexcryptica.com/generators/something-new"),
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert second.completed is True
    assert posted == [OLD_PAGE, "https://codexcryptica.com/generators/something-new"]
    suppressed = dict(events)["repeat_suppressed"]
    assert suppressed["earlier_runs"] == "release-1"
    assert "bluesky" in str(suppressed["channels"])


def test_recent_posts_reach_the_evaluator_prompt(tmp_path: Path) -> None:
    from dev_agents.workflows.release_content import load_skill_prompt, render_template

    prompt = render_template(
        load_skill_prompt("release-evaluate"),
        {
            "new_sha": "n",
            "previous_sha": "p",
            "commits": "c",
            "files_changed": "f",
            "diff_stat": "d",
            "recent_posts": "- 2026-09-23 · https://example.com/tools/x",
        },
    )

    assert "https://example.com/tools/x" in prompt
    assert "{recent_posts}" not in prompt
