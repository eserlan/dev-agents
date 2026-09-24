import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows.release_comms import EvaluatorResult, WriterResult, run_release_comms
from dev_agents.workflows.release_history import (
    format_recent_posts,
    narrow_previous_sha,
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


def _commit_chain(repo: Path, count: int) -> list[str]:
    """Append ``count`` commits to a repository and return their SHAs, oldest first."""
    shas: list[str] = []
    for index in range(count):
        (repo / f"file-{len(shas)}-{index}.txt").write_text(str(index), encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"change {index}"], cwd=repo, check=True, capture_output=True
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        )
        shas.append(head.stdout.strip())
    return shas


def test_narrow_previous_sha_starts_after_the_newest_handled_release(
    git_repository: Path,
) -> None:
    c1, c2, c3, c4 = _commit_chain(git_repository, 4)

    # Two releases in flight both derived c1 as "previous"; c2 was already handled.
    assert narrow_previous_sha(git_repository, c1, c4, [c2]) == c2
    assert narrow_previous_sha(git_repository, c1, c4, [c2, c3]) == c3
    assert narrow_previous_sha(git_repository, c1, c4, [c3, c2]) == c3
    assert narrow_previous_sha(git_repository, None, c4, [c2]) == c2


def test_narrow_previous_sha_keeps_the_promote_value_when_nothing_applies(
    git_repository: Path,
) -> None:
    c1, c2, c3, c4 = _commit_chain(git_repository, 4)

    assert narrow_previous_sha(git_repository, c1, c4, []) == c1
    # Handled commit older than the promote-derived start: keep the later start.
    assert narrow_previous_sha(git_repository, c3, c4, [c2]) == c3
    # The release's own commit, or one git does not know, must not move the start.
    assert narrow_previous_sha(git_repository, c1, c4, [c4]) == c1
    assert narrow_previous_sha(git_repository, c1, c4, ["0" * 40]) == c1


def test_narrow_previous_sha_is_a_no_op_outside_a_git_repository(tmp_path: Path) -> None:
    assert narrow_previous_sha(tmp_path, "prev", "new", ["handled"]) == "prev"


def test_overlapping_release_does_not_reevaluate_an_earlier_releases_commits(
    monkeypatch: pytest.MonkeyPatch, git_repository: Path, tmp_path: Path
) -> None:
    c1, c2, c3, c4 = _commit_chain(git_repository, 4)
    database = tmp_path / "release.db"
    project = ProjectConfig(
        repo=git_repository,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(state_path=database, image_generation=False),
    )
    state = StateRepository(database, "demo", git_repository)
    state.claim_run("release-comms", "earlier")
    state.record_event(
        "release-comms", "earlier", "resolved", "resolved", metadata={"new_sha": c2, "previous_sha": c1}
    )
    state.complete_run("release-comms", "earlier")
    state.claim_run("release-comms", "failed-earlier")
    state.record_event(
        "release-comms", "failed-earlier", "resolved", "resolved", metadata={"new_sha": c3}
    )
    state.complete_run("release-comms", "failed-earlier", status="failed", error="boom")
    # A failed run may never announce c3, so it must not move the start; c2 (handled) does.
    # The promote lookup still says the range starts at c1, as it did for the earlier release.
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas", lambda r, run_id: (c4, c1)
    )
    events: list[tuple[str, dict[str, object]]] = []

    result = run_release_comms(
        project, "demo", "later",
        evaluator_result=EvaluatorResult(postworthy=False, reason="chore"),
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.previous_sha == c2
    assert dict(events)["resolved"] == {
        "new_sha": c4, "previous_sha": c2, "promote_previous_sha": c1
    }
