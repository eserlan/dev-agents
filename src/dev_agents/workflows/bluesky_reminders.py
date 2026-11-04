"""Choose and word paced Bluesky reminders for existing article and answer pages.

The daemon owns pacing and publishing; this module only decides which page is next and how its
post reads, so the choice can be tested without a scheduler.
"""

from __future__ import annotations

import hashlib
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

POSTED_STATUSES = ("published", "staged")


def latest_publication_time(records: Iterable[Any]) -> datetime | None:
    """Most recent published or staged time among publication records, in UTC."""
    times: list[datetime] = []
    for record in records:
        if record.status not in POSTED_STATUSES:
            continue
        try:
            posted_at = datetime.fromisoformat(record.published_at)
        except (TypeError, ValueError):
            continue
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)
        times.append(posted_at.astimezone(UTC))
    return max(times, default=None)


def _page_key(url: str) -> str:
    """Compare pages without query strings or fragments (UTM tags, anchors)."""
    parts = urllib.parse.urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def reminder_text(post: Mapping[str, str], hashtags: Sequence[str] = ()) -> str:
    """Post copy for one page: answers lead with their question, blog posts are labelled."""
    title = post.get("title", "").strip()
    path = urllib.parse.urlsplit(post.get("pageUrl", "")).path
    body = f"From the blog: {title}" if path.startswith("/blog/") else title
    tags = " ".join(f"#{tag.strip().lstrip('#')}" for tag in hashtags if tag.strip().lstrip("#"))
    return f"{body} {tags}".strip()


def select_bluesky_reminder(
    content_posts: Sequence[Mapping[str, str]],
    history: Iterable[Any],
    slot: int,
    hashtags: Sequence[str] = (),
) -> dict[str, str] | None:
    """Pick a page never posted to any reminder channel, scattered by slot.

    ``history`` holds the publication records of every channel the reminder posts to, so a page
    already on Bluesky or Instagram is skipped and both channels always receive a fresh page.

    Content arrives in path order, so ``slot % len(pool)`` would post neighbouring pages
    ("How do you keep notes…", "How do you keep track…") on consecutive days. Hashing the slot with
    each page keeps the choice deterministic for a slot while mixing topics and page kinds.
    Pages whose last attempt failed are retried only after every fresh page has been used, so
    one broken image cannot block the queue.
    """
    posted: set[str] = set()
    failed: set[str] = set()
    for record in history:
        key = _page_key(record.page_url or "")
        if record.status in POSTED_STATUSES:
            posted.add(key)
        elif record.status == "failed":
            failed.add(key)
    available = [
        post
        for post in content_posts
        if post.get("title", "").strip() and _page_key(post.get("pageUrl", "")) not in posted
    ]
    fresh = [post for post in available if _page_key(post["pageUrl"]) not in failed]
    pool = fresh or available
    if not pool:
        return None
    selected = min(
        pool, key=lambda post: hashlib.sha256(f"{slot}:{post['pageUrl']}".encode()).hexdigest()
    )
    return {
        "text": reminder_text(selected, hashtags),
        "pageUrl": selected["pageUrl"],
        "image": selected["image"],
    }
