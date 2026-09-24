"""Cross-run memory of recent release announcements.

A promote run only knows about its own publications, so consecutive releases that touch the
same feature used to be announced again with different wording. This module reads recent
publication receipts from earlier runs so the agents can be told what has already gone out, and
so publishing can refuse a repeat even when the agents write about it anyway.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dev_agents.runtime import PublicationRecord, StateRepository
from dev_agents.workflows.release_publish import publication_key

DEFAULT_LOOKBACK_DAYS = 14
MAX_PROMPT_ENTRIES = 20
SUMMARY_CHARS = 200
NO_RECENT_POSTS = "(none)"
_HISTORY_LIMIT = 1000
_EARLIER_RUN_LIMIT = 30
_GIT_TIMEOUT_SECONDS = 60.0
# Runs whose commits were (or are being) announced, or were judged and deliberately skipped.
# A failed run is excluded: it may never announce its range, so a later run should still cover it.
_HANDLED_STATUSES = ("completed", "scheduled", "running", "rejected")


@dataclass(frozen=True)
class Announcement:
    """One page announced by an earlier run, across every channel that carried it."""

    run_id: str
    published_at: datetime
    page_url: str
    summary: str
    records: tuple[PublicationRecord, ...]

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted({record.channel for record in self.records}))


def page_slug(url: str) -> str:
    """Return the last path segment of a page URL, lowercased; "" when there is none.

    Only the final segment is compared so a route move such as ``/tools/foo`` to
    ``/generators/foo`` still counts as the same page.
    """
    segments = [segment for segment in urlparse(url.strip()).path.split("/") if segment]
    return segments[-1].lower() if segments else ""


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= SUMMARY_CHARS:
        return collapsed
    return collapsed[: SUMMARY_CHARS - 1].rstrip() + "…"


def _draft_summary(drafts: Any, page_url: str) -> str:
    """Return the text of the draft that announced ``page_url`` in an earlier run."""
    if not isinstance(drafts, dict):
        return ""
    for key, field in (("bluesky", "text"), ("github_discussions", "title")):
        items = drafts.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("pageUrl", "")).strip() == page_url:
                text = str(item.get(field) or item.get("body") or "")
                if text.strip():
                    return _one_line(text)
    return ""


def recent_announcements(
    repository: StateRepository,
    *,
    exclude_run_id: str,
    days: int = DEFAULT_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> list[Announcement]:
    """Return pages announced by other release-comms runs in the last ``days`` days."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    grouped: dict[tuple[str, str], list[PublicationRecord]] = {}
    for record in repository.list_publications("release-comms", limit=_HISTORY_LIMIT):
        if record.run_id == exclude_run_id or record.status not in ("published", "staged"):
            continue
        published_at = _parse_time(record.published_at)
        if published_at is None or published_at < cutoff:
            continue
        grouped.setdefault((record.run_id, record.page_url), []).append(record)

    drafts_by_run: dict[str, Any] = {}
    announcements: list[Announcement] = []
    for (run_id, page_url), records in grouped.items():
        if run_id not in drafts_by_run:
            run = repository.get_run("release-comms", run_id)
            drafts_by_run[run_id] = run.metadata.get("drafts") if run is not None else None
        published_at = max(
            time for record in records if (time := _parse_time(record.published_at)) is not None
        )
        announcements.append(
            Announcement(
                run_id=run_id,
                published_at=published_at,
                page_url=page_url,
                summary=_draft_summary(drafts_by_run[run_id], page_url),
                records=tuple(records),
            )
        )
    announcements.sort(key=lambda item: item.published_at, reverse=True)
    return announcements


def format_recent_posts(announcements: Iterable[Announcement]) -> str:
    """Render announcements as a prompt block, newest first."""
    lines = [
        " · ".join(
            part
            for part in (
                announcement.published_at.strftime("%Y-%m-%d"),
                announcement.page_url or "(no page link)",
                ", ".join(announcement.channels),
                f'"{announcement.summary}"' if announcement.summary else "",
            )
            if part
        )
        for announcement in list(announcements)[:MAX_PROMPT_ENTRIES]
    ]
    return "\n".join(f"- {line}" for line in lines) or NO_RECENT_POSTS


def repeated_publication_keys(
    announcements: Iterable[Announcement], draft_urls: Iterable[str]
) -> tuple[set[tuple[str, str]], set[str]]:
    """Return publication keys that would repeat an earlier announcement, and their run IDs.

    A draft repeats an announcement when their page URLs share a slug. The returned keys are
    exactly what ``publish_release_drafts`` checks, so adding them to ``already_published``
    suppresses those channels for the draft.
    """
    by_slug: dict[str, list[Announcement]] = {}
    for announcement in announcements:
        slug = page_slug(announcement.page_url)
        if slug:
            by_slug.setdefault(slug, []).append(announcement)

    keys: set[tuple[str, str]] = set()
    matched_runs: set[str] = set()
    for url in draft_urls:
        for announcement in by_slug.get(page_slug(url), []):
            matched_runs.add(announcement.run_id)
            for record in announcement.records:
                keys.add(publication_key(record.channel, record.destination, url))
    return keys, matched_runs


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def earlier_release_shas(repository: StateRepository, *, exclude_run_id: str) -> list[str]:
    """Return the commits earlier release-comms runs evaluated up to (their ``new_sha``)."""
    shas: list[str] = []
    for run in repository.list_runs("release-comms", limit=_EARLIER_RUN_LIMIT):
        if run.run_id == exclude_run_id or run.status not in _HANDLED_STATUSES:
            continue
        for event in repository.list_run_events("release-comms", run.run_id):
            new_sha = event.metadata.get("new_sha") if event.event == "resolved" else None
            if new_sha:
                shas.append(str(new_sha))
                break
    return shas


def narrow_previous_sha(
    repo: Path, previous_sha: str | None, new_sha: str, handled_shas: Iterable[str]
) -> str | None:
    """Start a release's diff after the newest commit an earlier release already covered.

    Promote runs derive ``previous_sha`` from the last *successful* promote, so two releases
    in flight together both start from the same commit and the second re-evaluates (and
    re-announces) everything in the first. Any earlier handled commit that is an ancestor of
    ``new_sha`` and newer than ``previous_sha`` moves the start forward; otherwise the promote
    derived value is kept. Without git history to compare, nothing changes.
    """
    best: str | None = None
    for sha in handled_shas:
        if not sha or sha == new_sha or not _is_ancestor(repo, sha, new_sha):
            continue
        if best is None or _is_ancestor(repo, best, sha):
            best = sha
    if best is None:
        return previous_sha
    if previous_sha and not _is_ancestor(repo, previous_sha, best):
        return previous_sha
    return best
