import base64
from pathlib import Path

import pytest

from dev_agents.config import ContentQueueConfig, ProjectConfig
from dev_agents.workflows.content_queue import (
    DraftedItem,
    draft_backlog_item,
    next_backlog_item,
    next_publishable_drafted,
    parse_queue_file,
    publish_drafted_item,
)
from dev_agents.workflows.release_comms import ReleaseCommsRunResult

QUEUE_FIXTURE = """\
# Bluesky Post Log

## Posted

### 2026-09-06 — Heist Generator (ad hoc)

- **Text:** I needed heists that didn't fall apart on the first bad roll.

  codexcryptica.com/generators/heist

  #TTRPG #Worldbuilding

- **Image:** `https://assets.codexcryptica.com/screenshots/generator-heist.jpg`
- **Alt:** The Codex Cryptica Heist Generator
- **URL:** https://bsky.app/profile/codexcryptica.bsky.social/post/abc

## Backlog (from issue #2086, reordered)

1. **Related Entity Generation** _(orig #2, generation)_ — Need: NPCs, factions and locations that actually relate to the existing setting. Tags: `#TTRPG #Worldbuilding #RPGDesign`. **Blocked on an image** — no screenshot exists yet.
2. **NPC / Character Generator** _(orig #5, generation)_ — Need: usable characters with hooks and relationships, not just names and appearance. Tags: `#TTRPG #Worldbuilding`

## Drafted (not yet posted)

### Release comms auto-draft, 2026-09-10 (`10440ab`)

- **Text:** I needed a quick reference for making a fantasy city feel lived-in.

codexcryptica.com/[relevant page]

#TTRPG #Worldbuilding

- **Image:** _TODO — needs a screenshot before this can be posted._
- **Alt:** _TODO_
- **Note:** Auto-queued; text not yet human-reviewed.

### Random Tables & Interactive Decks (spec #157)

- **Text:** I needed quick random encounter tables and card draws without leaving my campaign notes.

  So I built Random Tables & Decks in Codex Cryptica.

  codexcryptica.com/tables

  #TTRPG #Worldbuilding #RPGDesign

- **Image:** `https://assets.codexcryptica.com/images/blog/oracle-capabilities/oracle-roll-command.png`
- **Alt:** Codex Cryptica showing interactive table and deck rolling
- **Note:** Spec #157 implementation.
"""


def test_parse_queue_file_extracts_drafted_and_backlog() -> None:
    queue = parse_queue_file(QUEUE_FIXTURE)

    assert [item.heading for item in queue.drafted] == [
        "Release comms auto-draft, 2026-09-10 (`10440ab`)",
        "Random Tables & Interactive Decks (spec #157)",
    ]
    assert queue.drafted[1].image == (
        "https://assets.codexcryptica.com/images/blog/oracle-capabilities/oracle-roll-command.png"
    )
    assert queue.drafted[1].link == "codexcryptica.com/tables"
    assert "[" in queue.drafted[0].link

    assert [item.title for item in queue.backlog] == [
        "Related Entity Generation",
        "NPC / Character Generator",
    ]
    assert queue.backlog[0].blocked_note is not None
    assert queue.backlog[1].blocked_note is None
    assert queue.backlog[1].tags == ["#TTRPG", "#Worldbuilding"]
    assert queue.backlog[1].index == 2


def test_next_publishable_drafted_skips_placeholder_fields() -> None:
    queue = parse_queue_file(QUEUE_FIXTURE)

    item = next_publishable_drafted(queue.drafted)

    assert item is not None
    assert item.heading == "Random Tables & Interactive Decks (spec #157)"


def test_next_backlog_item_skips_blocked_items() -> None:
    queue = parse_queue_file(QUEUE_FIXTURE)

    item = next_backlog_item(queue.backlog)

    assert item is not None
    assert item.title == "NPC / Character Generator"


def _fake_contents_gh_json(content: str, sha: str = "sha1"):
    def fake(_repo, *args, **_kwargs):
        assert args[0] == "api"
        return {"content": base64.b64encode(content.encode()).decode(), "sha": sha}

    return fake


def test_publish_drafted_item_records_posted_only_on_real_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    config = ContentQueueConfig(rules_issue=2086)
    queue = parse_queue_file(QUEUE_FIXTURE)
    item = next_publishable_drafted(queue.drafted)
    assert item is not None

    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.repository_slug", lambda _repo: "owner/repo"
    )
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.gh_json", _fake_contents_gh_json(QUEUE_FIXTURE)
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.gh",
        lambda _repo, *args, **_kwargs: calls.append(args) or "",
    )
    canned = ReleaseCommsRunResult(
        promote_run_id="content-queue-2026-09-20",
        new_sha="head",
        previous_sha=None,
        postworthy=True,
        drafts=None,
        published={
            "bluesky": [
                {"channel": "bluesky", "public_url": "https://bsky.app/profile/x/post/1"}
            ]
        },
        completed=True,
    )
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.run_release_comms", lambda *_a, **_kw: canned
    )

    result = publish_drafted_item(
        project=project,
        project_name="codex-cryptica",
        config=config,
        item=item,
        dry_run=False,
        publish_approved=True,
    )

    assert result is canned
    write_calls = [call for call in calls if call[0] == "api"]
    comment_calls = [call for call in calls if call[0] == "issue"]
    assert len(write_calls) == 1
    assert len(comment_calls) == 1
    written_content = base64.b64decode(
        next(a for a in write_calls[0] if a.startswith("content="))[len("content=") :]
    ).decode()
    new_queue = parse_queue_file(written_content)
    assert not any(d.heading == item.heading for d in new_queue.drafted)
    assert any("Random Tables" in d.heading for d in new_queue.drafted) is False


def test_publish_drafted_item_does_not_touch_the_file_on_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    config = ContentQueueConfig(rules_issue=2086)
    queue = parse_queue_file(QUEUE_FIXTURE)
    item = next_publishable_drafted(queue.drafted)
    assert item is not None

    def fail(*_args, **_kwargs):
        raise AssertionError("must not touch the queue file on a dry run")

    monkeypatch.setattr("dev_agents.workflows.content_queue.gh_json", fail)
    monkeypatch.setattr("dev_agents.workflows.content_queue.gh", fail)
    canned = ReleaseCommsRunResult(
        promote_run_id="content-queue-2026-09-20",
        new_sha="head",
        previous_sha=None,
        postworthy=True,
        drafts=None,
        published={"bluesky": [{"channel": "bluesky", "public_url": "dry-run://bluesky/x"}]},
        completed=True,
    )
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.run_release_comms", lambda *_a, **_kw: canned
    )

    result = publish_drafted_item(
        project=project,
        project_name="codex-cryptica",
        config=config,
        item=item,
        dry_run=True,
        publish_approved=False,
    )

    assert result is canned


def test_draft_backlog_item_never_publishes_and_flags_missing_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    config = ContentQueueConfig(rules_issue=2086)
    queue = parse_queue_file(QUEUE_FIXTURE)
    item = next_backlog_item(queue.backlog)
    assert item is not None

    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.repository_slug", lambda _repo: "owner/repo"
    )

    def fake_gh_json(_repo, *args, **_kwargs):
        if "issues" in args[1]:
            return {"body": "rules text"}
        return {"content": base64.b64encode(QUEUE_FIXTURE.encode()).decode(), "sha": "sha1"}

    monkeypatch.setattr("dev_agents.workflows.content_queue.gh_json", fake_gh_json)
    write_payloads: list[str] = []

    def fake_gh(_repo, *args, **_kwargs):
        if args[0] == "api":
            decoded = base64.b64decode(
                next(a for a in args if a.startswith("content="))[len("content=") :]
            ).decode()
            write_payloads.append(decoded)
        return ""

    monkeypatch.setattr("dev_agents.workflows.content_queue.gh", fake_gh)
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue._run_pass",
        lambda **_kwargs: (
            '```json\n{"text": "I needed usable characters. So I built the NPC Generator. '
            'codexcryptica.com/generators/npc #TTRPG #Worldbuilding", '
            '"link": "codexcryptica.com/generators/npc"}\n```'
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.resolve_backlog_image", lambda _title: None
    )
    published_calls: list[object] = []
    monkeypatch.setattr(
        "dev_agents.workflows.content_queue.run_release_comms",
        lambda *a, **kw: published_calls.append((a, kw)),
    )

    drafted = draft_backlog_item(
        project=project,
        item=item,
        config=config,
        providers=["codex"],
        log_dir=tmp_path,
        run_id="test-run",
        timeout_seconds=60,
    )

    assert drafted is not None
    assert isinstance(drafted, DraftedItem)
    assert drafted.image == "_TODO — needs a screenshot before this can be posted._"
    assert not published_calls
    assert write_payloads
    new_queue = parse_queue_file(write_payloads[-1])
    assert not any(b.index == item.index for b in new_queue.backlog)
    assert any("NPC" in d.heading for d in new_queue.drafted)
