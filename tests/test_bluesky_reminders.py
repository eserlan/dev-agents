from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace

from dev_agents.config import ReleaseCommsConfig
from dev_agents.workflows.bluesky_reminders import (
    latest_publication_time,
    reminder_text,
    select_bluesky_reminder,
)

ANSWER = {
    "pageUrl": "https://codexcryptica.com/answers/gods-share-a-domain",
    "image": "https://assets.example.com/og/gods.jpg",
    "title": "Can gods share a domain?",
}
BLOG = {
    "pageUrl": "https://codexcryptica.com/blog/how-import-works",
    "image": "https://assets.example.com/og/import.jpg",
    "title": "How import works",
}
PACK = {
    "pageUrl": "https://codexcryptica.com/for/game-masters",
    "image": "https://assets.example.com/og/gm.jpg",
    "title": "Codex Cryptica for game masters",
}


def record(page_url: str, status: str = "published", published_at: str = "2026-11-01T10:00:00+00:00"):
    return SimpleNamespace(page_url=page_url, status=status, published_at=published_at)


def test_reminder_text_labels_blog_posts_and_appends_hashtags() -> None:
    assert reminder_text(ANSWER) == "Can gods share a domain?"
    assert reminder_text(BLOG, ["TTRPG", "#worldbuilding", " "]) == (
        "From the blog: How import works #TTRPG #worldbuilding"
    )
    assert reminder_text(PACK) == "Codex Cryptica for game masters"


def test_select_skips_pages_already_on_bluesky_including_utm_variants() -> None:
    history = [
        record(ANSWER["pageUrl"] + "?utm_source=bluesky"),
        record(BLOG["pageUrl"] + "/", status="staged"),
    ]

    for slot in range(4):
        selected = select_bluesky_reminder([ANSWER, BLOG, PACK], history, slot)
        assert selected is not None
        assert selected["pageUrl"] == PACK["pageUrl"]
    assert select_bluesky_reminder([ANSWER, BLOG], history, 0) is None


def test_select_scatters_consecutive_slots_and_returns_a_publishable_draft() -> None:
    pages = [
        {**ANSWER, "pageUrl": f"https://codexcryptica.com/answers/q-{index:03}", "title": f"Q{index}?"}
        for index in range(100)
    ]
    picks = [select_bluesky_reminder(pages, [], slot)["pageUrl"] for slot in range(10)]  # type: ignore[index]
    positions = [int(url.rsplit("-", 1)[1]) for url in picks]

    assert picks == [select_bluesky_reminder(pages, [], slot)["pageUrl"] for slot in range(10)]  # type: ignore[index]
    assert len(set(picks)) > 5
    # slot % len(pool) would walk adjacent pages: 0, 1, 2, ...
    assert sum(abs(b - a) == 1 for a, b in pairwise(positions)) < 3
    assert select_bluesky_reminder([BLOG], [], 7, ["TTRPG"]) == {
        "text": "From the blog: How import works #TTRPG",
        "pageUrl": BLOG["pageUrl"],
        "image": BLOG["image"],
    }


def test_page_posted_on_either_mirrored_channel_is_skipped() -> None:
    instagram_only = [SimpleNamespace(page_url=ANSWER["pageUrl"], status="published", published_at="")]

    for slot in range(4):
        assert select_bluesky_reminder([ANSWER, BLOG], instagram_only, slot)["pageUrl"] == BLOG["pageUrl"]  # type: ignore[index]


def test_failed_pages_wait_until_fresh_pages_are_used() -> None:
    failed = [record(ANSWER["pageUrl"], status="failed")]

    assert {
        select_bluesky_reminder([ANSWER, BLOG], failed, slot)["pageUrl"]  # type: ignore[index]
        for slot in range(4)
    } == {BLOG["pageUrl"]}
    retried = select_bluesky_reminder([ANSWER], failed, 0)
    assert retried is not None and retried["pageUrl"] == ANSWER["pageUrl"]


def test_pages_without_a_title_are_never_posted() -> None:
    assert select_bluesky_reminder([{**ANSWER, "title": " "}], [], 0) is None


def test_latest_publication_time_ignores_unposted_and_unparseable_records() -> None:
    records = [
        record("a", published_at="2026-11-01T10:00:00+00:00"),
        record("b", published_at="2026-11-02T10:00:00"),
        record("c", status="failed", published_at="2026-11-03T10:00:00+00:00"),
        record("d", published_at="not a date"),
    ]

    assert latest_publication_time(records) == datetime(2026, 11, 2, 10, tzinfo=UTC)
    assert latest_publication_time([]) is None


def test_bluesky_reminders_are_opt_in_with_daily_default() -> None:
    config = ReleaseCommsConfig.model_validate({"bluesky_reminders": {"enabled": True}})

    assert ReleaseCommsConfig().bluesky_reminders is None
    assert config.bluesky_reminders is not None
    assert config.bluesky_reminders.interval_hours == 24
    assert config.bluesky_reminders.hashtags == []
    assert config.bluesky_reminders.mirror_to_instagram is False
