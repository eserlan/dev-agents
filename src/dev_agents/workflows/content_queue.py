"""Cadence-driven social posts drafted from a target repo's own marketing
backlog file (e.g. ``.social/bluesky-posts.md``), as a parallel entry point to
release-comms's deploy-triggered drafting.

release-comms drafts from a git diff: "what shipped, is it postworthy." A
marketing backlog is the opposite -- already-shipped features, announced on a
fixed cadence independent of any current commit. There is no diff to
evaluate, so this module builds the same ``EvaluatorResult``/``WriterResult``
inputs ``run_release_comms`` already accepts directly, from the queue file
instead.

Two tiers, kept deliberately asymmetric in trust:

- A ``## Drafted`` entry already has human-reviewed text, image, and link --
  nothing left but to actually publish it. Safe to automate, gated by the
  project's existing ``auto_publish`` flag (same trust model release-comms
  already uses for deploy-triggered posts).
- A ``## Backlog`` entry is bare (a need + tags). Turning it into a draft
  needs an LLM writing pass matching the queue's voice rules, plus image
  sourcing. This never auto-publishes -- it only ever appends a new
  ``## Drafted`` entry for a human to review, mirroring the existing
  `bsky-note` skill's own confirm-before-publish rule.

The queue file itself is read and written via the GitHub Contents API rather
than the shared local checkout other workflows (`pr_fixer`, `issue_fixer`)
operate on -- this avoids that checkout's `repo_git_lock`/worktree
contention entirely, and the API's blob-`sha` precondition gives the same
"don't clobber a concurrent edit" safety `repo_git_lock` gives those
workflows, without touching the working tree.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_agents.config import ContentQueueConfig, ProjectConfig
from dev_agents.runtime.github import GitHubError, gh, gh_json, repository_slug
from dev_agents.workflows.release_comms import ReleaseCommsRunResult, run_release_comms
from dev_agents.workflows.release_content import (
    EvaluatorResult,
    WriterResult,
    _run_pass,
    extract_json_block,
    load_skill_prompt,
    render_template,
)
from dev_agents.workflows.release_publish import ASSET_HOST

DRAFTED_HEADING = "## Drafted (not yet posted)"
BACKLOG_HEADING = "## Backlog"
POSTED_HEADING = "## Posted"


_BACKLOG_LINE = re.compile(
    r"^(?P<num>\d+)\.\s+\*\*(?P<title>.+?)\*\*\s+_\((?P<origin>.+?)\)_\s+—\s+"
    r"Need:\s+(?P<need>.+?)\.\s+Tags:\s+`(?P<tags>[^`]+)`(?P<rest>.*)$"
)


@dataclass(frozen=True)
class DraftedItem:
    heading: str
    text: str
    image: str
    alt: str
    note: str
    link: str
    block: str  # exact raw "### heading ... " text as it appears in the file


@dataclass(frozen=True)
class BacklogItem:
    index: int
    title: str
    origin_note: str
    need: str
    tags: list[str]
    blocked_note: str | None
    line: str  # exact raw line as it appears in the file


@dataclass(frozen=True)
class QueueFile:
    drafted: list[DraftedItem]
    backlog: list[BacklogItem]


def _section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Return the (start, end) span of one ``## Heading`` section's body."""
    match = re.search(rf"^{re.escape(heading)}.*$", text, re.MULTILINE)
    if match is None:
        return None
    start = match.end()
    following = re.search(r"^## ", text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    return start, end


def _parse_drafted(text: str) -> list[DraftedItem]:
    span = _section_span(text, DRAFTED_HEADING)
    if span is None:
        return []
    section_start, section_end = span
    section = text[section_start:section_end]
    heading_positions = [m.start() for m in re.finditer(r"^### ", section, re.MULTILINE)]
    items: list[DraftedItem] = []
    for index, rel_start in enumerate(heading_positions):
        rel_end = (
            heading_positions[index + 1] if index + 1 < len(heading_positions) else len(section)
        )
        block = section[rel_start:rel_end]
        heading = block.splitlines()[0][len("### ") :].strip()
        text_match = re.search(r"-\s+\*\*Text:\*\*\s*(.+?)(?=\n-\s+\*\*|\Z)", block, re.DOTALL)
        image_match = re.search(r"-\s+\*\*Image:\*\*\s*(.+)", block)
        alt_match = re.search(r"-\s+\*\*Alt:\*\*\s*(.+)", block)
        note_match = re.search(r"-\s+\*\*Note:\*\*\s*(.+)", block)
        item_text = text_match.group(1).strip() if text_match else ""
        image = image_match.group(1).strip() if image_match else ""
        alt = alt_match.group(1).strip() if alt_match else ""
        note = note_match.group(1).strip() if note_match else ""
        link_match = re.search(r"codexcryptica\.com\S*", item_text)
        link = link_match.group(0) if link_match else ""
        items.append(
            DraftedItem(
                heading=heading,
                text=item_text,
                image=image.strip("`"),
                alt=alt,
                note=note,
                link=link,
                block=block,
            )
        )
    return items


def _parse_backlog(text: str) -> list[BacklogItem]:
    span = _section_span(text, BACKLOG_HEADING)
    if span is None:
        return []
    body = text[span[0] : span[1]]
    items: list[BacklogItem] = []
    for line in body.splitlines():
        match = _BACKLOG_LINE.match(line.strip())
        if match is None:
            continue
        rest = match.group("rest").strip(" .")
        blocked = rest if "blocked" in rest.lower() else None
        items.append(
            BacklogItem(
                index=int(match.group("num")),
                title=match.group("title").strip(),
                origin_note=match.group("origin").strip(),
                need=match.group("need").strip(),
                tags=match.group("tags").split(),
                blocked_note=blocked,
                line=line,
            )
        )
    return items


def parse_queue_file(text: str) -> QueueFile:
    return QueueFile(drafted=_parse_drafted(text), backlog=_parse_backlog(text))


def next_publishable_drafted(items: list[DraftedItem]) -> DraftedItem | None:
    """The top Drafted item with everything a real post needs -- never guess a
    missing field, just skip to the next one."""
    for item in items:
        if not item.image or not item.alt or not item.link:
            continue
        if "_TODO" in item.image or "_TODO" in item.alt:
            continue
        # A literal "[" marks an unresolved bracket placeholder like
        # "[relevant page]" left by a human as a deliberate TODO.
        if "[" in item.link:
            continue
        return item
    return None


def next_backlog_item(items: list[BacklogItem]) -> BacklogItem | None:
    for item in items:
        if item.blocked_note:
            continue
        return item
    return None


def _probe_image(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return bool(200 <= response.status < 300)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def resolve_backlog_image(title: str) -> str | None:
    """Try the established screenshot/OG-image R2 conventions before giving up
    -- never fabricate a path that doesn't resolve."""
    slug = _slugify(title)
    for key in (f"screenshots/generator-{slug}.jpg", f"og/{slug}.jpg"):
        url = f"https://{ASSET_HOST}/{key}"
        if _probe_image(url):
            return url
    return None


def read_queue_file(project: ProjectConfig, log_path: str) -> tuple[str, str]:
    """Fetch the queue file's current committed content and blob sha."""
    slug = repository_slug(project.repo)
    data = gh_json(project.repo, "api", f"repos/{slug}/contents/{log_path}")
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, str(data["sha"])


def write_queue_file(project: ProjectConfig, log_path: str, content: str, sha: str, message: str) -> None:
    slug = repository_slug(project.repo)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    gh(
        project.repo,
        "api",
        "-X",
        "PUT",
        f"repos/{slug}/contents/{log_path}",
        "-f",
        f"message={message}",
        "-f",
        f"content={encoded}",
        "-f",
        f"sha={sha}",
    )


def _apply_with_retry(
    project: ProjectConfig,
    log_path: str,
    apply_edit: Callable[[str], str | None],
    message: str,
    max_attempts: int = 2,
) -> str | None:
    """Read-modify-write against the live file, retrying once against a fresh
    read if a concurrent edit changed the blob sha out from under us."""
    last_error: GitHubError | None = None
    for _ in range(max_attempts):
        content, sha = read_queue_file(project, log_path)
        new_content = apply_edit(content)
        if new_content is None:
            return None
        try:
            write_queue_file(project, log_path, new_content, sha, message)
            return new_content
        except GitHubError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    return None


def _remove_backlog_line(text: str, line: str) -> str:
    if f"{line}\n" in text:
        return text.replace(f"{line}\n", "", 1)
    if line in text:
        return text.replace(line, "", 1)
    return text


def _insert_after_heading(text: str, heading: str, new_block: str) -> str:
    match = re.search(rf"^{re.escape(heading)}.*$", text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"queue file has no {heading!r} heading")
    insert_at = match.end()
    return text[:insert_at] + "\n\n" + new_block.rstrip("\n") + "\n" + text[insert_at:]


def _append_to_section(text: str, heading: str, new_block: str) -> str:
    span = _section_span(text, heading)
    if span is None:
        raise RuntimeError(f"queue file has no {heading!r} heading")
    start, end = span
    insertion = text[start:end].rstrip("\n") + "\n\n" + new_block.rstrip("\n") + "\n\n"
    return text[:start] + insertion + text[end:]


def draft_backlog_item(
    *,
    project: ProjectConfig,
    item: BacklogItem,
    config: ContentQueueConfig,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> DraftedItem | None:
    """Draft one backlog item into a new ``## Drafted`` entry. Never publishes."""
    rules_text = "(no rules issue configured; use the general need/built/outcome/link/tags shape)"
    if config.rules_issue is not None:
        issue = gh_json(
            project.repo,
            "api",
            f"repos/{repository_slug(project.repo)}/issues/{config.rules_issue}",
        )
        rules_text = str(issue.get("body") or rules_text)
    prompt = render_template(
        load_skill_prompt("release-queue-post"),
        {
            "title": item.title,
            "need": item.need,
            "tags": " ".join(f"#{tag.lstrip('#')}" for tag in item.tags),
            "rules_text": rules_text,
        },
    )
    output = _run_pass(
        repo=project.repo,
        prompt=prompt,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        kind="content-queue-draft",
        timeout_seconds=timeout_seconds,
    )
    parsed = extract_json_block(output)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str) or not isinstance(
        parsed.get("link"), str
    ):
        return None
    resolved_image = str(parsed.get("image", "")).strip() or resolve_backlog_image(item.title) or ""
    image = resolved_image or "_TODO — needs a screenshot before this can be posted._"
    alt = str(parsed.get("alt", "")).strip() or "_TODO_"
    drafted = DraftedItem(
        heading=f"{item.title} (queue #{item.index})",
        text=str(parsed["text"]).strip(),
        image=image,
        alt=alt,
        note=f"Auto-drafted from backlog item #{item.index} by the content-queue agent; not yet human-reviewed.",
        link=str(parsed["link"]).strip(),
        block="",
    )
    new_block = (
        f"### {drafted.heading}\n\n"
        f"- **Text:** {drafted.text}\n\n"
        f"- **Image:** {drafted.image}\n"
        f"- **Alt:** {drafted.alt}\n"
        f"- **Note:** {drafted.note}\n"
    )

    def apply(text: str) -> str | None:
        if item.line not in text:
            return None
        text = _remove_backlog_line(text, item.line)
        return _append_to_section(text, DRAFTED_HEADING, new_block)

    _apply_with_retry(
        project,
        config.log_path,
        apply,
        f"content-queue: draft backlog item #{item.index} ({item.title})",
    )
    return drafted


def _record_posted(
    project: ProjectConfig, config: ContentQueueConfig, item: DraftedItem, url: str, date: str
) -> None:
    new_block = (
        f"### {date} — {item.heading}\n\n"
        f"- **Text:** {item.text}\n\n"
        f"- **Image:** {item.image}\n"
        f"- **Alt:** {item.alt}\n"
        f"- **URL:** {url}\n"
        + (f"- **Note:** {item.note}\n" if item.note else "")
    )

    def apply(text: str) -> str | None:
        if item.block and item.block not in text:
            return None
        remaining = text.replace(item.block, "", 1) if item.block else text
        return _insert_after_heading(remaining, POSTED_HEADING, new_block)

    _apply_with_retry(project, config.log_path, apply, f"content-queue: posted {item.heading}")
    if config.rules_issue is not None:
        gh(
            project.repo,
            "issue",
            "comment",
            str(config.rules_issue),
            "--body",
            f'Posted "{item.heading}": {url}',
            check=False,
        )


def publish_drafted_item(
    *,
    project: ProjectConfig,
    project_name: str,
    config: ContentQueueConfig,
    item: DraftedItem,
    dry_run: bool,
    publish_approved: bool,
) -> ReleaseCommsRunResult:
    """Publish one already-reviewed Drafted item through release-comms's own
    publish/dedup/pacing pipeline, then record the outcome in the queue file."""
    today = datetime.now(UTC).date().isoformat()
    evaluator_result = EvaluatorResult(
        postworthy=True,
        reason="content-queue cadence post",
        importance="medium",
        features=[{"name": item.heading, "bluesky_worthy": True}],
        recommended_channels=["bluesky", "discord"],
    )
    writer_result = WriterResult(
        bluesky=[{"text": item.text, "pageUrl": item.link, "image": item.image}],
        github_discussions=[],
    )
    result = run_release_comms(
        project,
        project_name,
        f"content-queue-{today}",
        dry_run=dry_run,
        publish_approved=publish_approved,
        evaluator_result=evaluator_result,
        writer_result=writer_result,
    )
    if not dry_run and publish_approved:
        receipts: list[dict[str, Any]] = result.published.get("bluesky") or []
        published_url = next(
            (
                str(receipt["public_url"])
                for receipt in receipts
                if isinstance(receipt, dict)
                and receipt.get("public_url")
                and not str(receipt["public_url"]).startswith("dry-run://")
            ),
            None,
        )
        if published_url:
            _record_posted(project, config, item, published_url, today)
    return result
