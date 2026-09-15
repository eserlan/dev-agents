"""Reddit publishing adapter: payload formatting, R2 candidate staging, and export.

Separates asynchronous Reddit staging from direct synchronous platform delivery.
Target repositories supply only release copy; dev-agents stages candidate
manifests to Cloudflare R2 for ingestion by the Devvit companion app.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_agents.workflows.release_publish import (
    ASSET_HOST,
    PublicationReceipt,
    _upload_r2_file,
)


def format_reddit_post(
    *,
    title: str,
    body: str,
    page_url: str,
    source_id: str = "",
    image_url: str | None = None,
) -> dict[str, Any]:
    """Format a Reddit candidate payload for Devvit ingestion."""
    clean_body = body.strip()
    id_tag = f"<!-- id:{source_id} -->" if source_id else ""
    footer = (
        "\n\n---\n"
        "*Posted automatically via release pipeline. Feedback and discussion welcome!*"
    )
    if id_tag:
        footer += f"\n{id_tag}"

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.lower()).strip("-")[:40]
    candidate_id = (
        f"reddit-{source_id}-{slug}"
        if source_id and slug
        else (f"reddit-{source_id}" if source_id else f"reddit-{slug}")
    )
    return {
        "id": candidate_id or "reddit-candidate",
        "title": title,
        "body": f"{clean_body}{footer}",
        "url": page_url,
        "image_url": image_url or "",
        "source_id": source_id,
        "status": "approved",
        "created_at": int(time.time()),
    }


def stage_reddit_candidate(
    *,
    repo: Path,
    candidate: Mapping[str, Any],
    env: Mapping[str, str],
    dry_run: bool,
    destination: str = "reddit",
    key: str = "announcements/reddit-candidates.json",
    timeout: float = 120,
) -> PublicationReceipt:
    """Upload or update the Reddit candidate manifest on Cloudflare R2."""
    page_url = str(candidate.get("url", ""))
    source_id = str(candidate.get("source_id", ""))
    candidate_id = str(candidate.get("id", ""))
    external_id = f"staged:{source_id}" if source_id else f"staged:{candidate_id}"

    if dry_run:
        return PublicationReceipt(
            channel="reddit",
            destination=destination,
            page_url=page_url,
            public_url=f"dry-run://r2/{key}",
            external_id=external_id,
            metadata={"status": "staged_to_r2", "candidate_id": candidate_id, "source_id": source_id},
        )

    # Fetch existing manifest if available to merge
    existing_candidates: list[dict[str, Any]] = []
    try:
        req = urllib.request.Request(
            f"https://{ASSET_HOST}/{key}",
            headers={"User-Agent": "dev-agents/release-comms"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("candidates"), list):
                existing_candidates = [c for c in data["candidates"] if isinstance(c, dict)]
            elif isinstance(data, list):
                existing_candidates = [c for c in data if isinstance(c, dict)]
    except Exception:  # noqa: BLE001
        existing_candidates = []

    merged = [
        c
        for c in existing_candidates
        if c.get("id") != candidate_id and (not source_id or c.get("source_id") != source_id)
    ]
    merged.append(dict(candidate))
    manifest = {
        "updated_at": int(time.time()),
        "candidates": merged,
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as temp:
        temp_path = Path(temp.name)
        json.dump(manifest, temp, indent=2)

    try:
        public_url = _upload_r2_file(
            repo=repo,
            path=temp_path,
            key=key,
            content_type="application/json",
            env=env,
            timeout=timeout,
        )
    finally:
        temp_path.unlink(missing_ok=True)

    return PublicationReceipt(
        channel="reddit",
        destination=destination,
        page_url=page_url,
        public_url=public_url,
        external_id=external_id,
        metadata={"status": "staged_to_r2", "candidate_id": candidate_id, "source_id": source_id},
    )


def export_reddit_markdown(
    discussions: list[dict[str, Any]],
    source_id: str = "",
    image_overrides: Mapping[str, str] | None = None,
) -> str:
    """Format discussions into copy/paste ready sections for manual Reddit posting."""
    from dev_agents.workflows.release_publish import resolve_image

    exported_sections: list[str] = []
    for disc in discussions:
        post = format_reddit_post(
            title=str(disc.get("title", "Release update")),
            body=str(disc.get("body", "")),
            page_url=str(disc.get("pageUrl", "")),
            source_id=source_id,
        )
        try:
            image_url, _ = resolve_image(disc, image_overrides)
        except Exception:  # noqa: BLE001
            image_url = str(disc.get("image", ""))

        section_lines = [
            "## Post Title",
            f"{post['title']}",
            "",
            "## Image (Drag & drop into Reddit)",
            f"{image_url if image_url else '(none)'}",
            "",
            "## Link URL",
            f"{post['url']}",
            "",
            "## Post Body (Markdown text tab)",
            f"{post['body']}",
        ]
        exported_sections.append("\n".join(section_lines))
    return "\n\n---\n\n".join(exported_sections) + "\n"


def clean_subreddit(subreddit: str) -> str:
    """Normalize subreddit name by stripping r/ prefix and slashes."""
    return subreddit.strip().lstrip("/").removeprefix("r/").strip("/")


def fetch_subreddit_posts(
    subreddit: str,
    *,
    limit: int = 25,
    user_agent: str = "dev-agents/release-comms",
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch recent submissions from the public unauthenticated subreddit JSON feed."""
    clean = clean_subreddit(subreddit)
    url = f"https://www.reddit.com/r/{clean}/new.json?limit={limit}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    posts: list[dict[str, Any]] = []
    children = (data.get("data") or {}).get("children") or []
    for child in children:
        item = child.get("data") or {}
        selftext = str(item.get("selftext", ""))
        source_match = re.search(r"<!-- id:([a-zA-Z0-9_-]+) -->", selftext)
        permalink = str(item.get("permalink", ""))
        post_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        posts.append({
            "id": str(item.get("id", "")),
            "name": str(item.get("name", "")),
            "title": str(item.get("title", "")),
            "selftext": selftext,
            "source_id": source_match.group(1) if source_match else "",
            "permalink": permalink,
            "url": post_url or str(item.get("url", "")),
            "created_utc": item.get("created_utc"),
        })
    return posts


def sync_reddit_status(
    *,
    project: Any,
    project_name: str,
    run_id: str | None = None,
    subreddit: str | None = None,
    repository: Any = None,
    fetched_posts: list[dict[str, Any]] | None = None,
    notify_tracking_issue: bool = True,
) -> list[Any]:
    """Reconcile staged Reddit candidates with live submissions from the subreddit."""
    from dev_agents.config import ReleaseCommsConfig
    from dev_agents.runtime import StateRepository, state_database_path

    if repository is None:
        comms_config = project.release_comms or ReleaseCommsConfig()
        db_path = state_database_path(
            comms_config.state_path, project.repo / ".dev-agents/release-comms-state.db"
        )
        repository = StateRepository(db_path, project_name=project_name)

    if not subreddit:
        if project.release_comms and getattr(project.release_comms, "subreddit", None):
            subreddit = project.release_comms.subreddit
        else:
            subreddit = "codexcryptica"

    clean_sub = clean_subreddit(subreddit)

    if run_id:
        pubs = repository.list_run_publications("release-comms", str(run_id))
        staged = [p for p in pubs if p.channel == "reddit" and p.status == "staged"]
    else:
        staged = repository.list_publications(
            "release-comms", channel="reddit", status="staged"
        )

    if not staged:
        return []

    if fetched_posts is None:
        try:
            posts = fetch_subreddit_posts(clean_sub)
        except Exception:  # noqa: BLE001 - network or rate limit failure
            return []
    else:
        posts = fetched_posts

    reconciled: list[Any] = []
    now_iso = datetime.now(UTC).isoformat()

    for pub in staged:
        source_id = str(pub.metadata.get("source_id", ""))
        if not source_id and pub.external_id and pub.external_id.startswith("staged:"):
            source_id = pub.external_id.removeprefix("staged:")

        matched_post: dict[str, Any] | None = None
        for post in posts:
            if source_id and post.get("source_id") == source_id:
                matched_post = post
                break
            if pub.page_url and (
                pub.page_url in post.get("selftext", "") or pub.page_url in post.get("url", "")
            ):
                matched_post = post
                break

        if matched_post is not None:
            live_url = matched_post.get("url", "")
            reddit_id = matched_post.get("id", "")
            new_metadata = dict(pub.metadata)
            new_metadata.update({
                "status": "published",
                "reddit_id": reddit_id,
                "reconciled_at": now_iso,
                "permalink": matched_post.get("permalink", ""),
            })
            updated_pub = repository.record_publication(
                workflow="release-comms",
                run_id=pub.run_id,
                channel=pub.channel,
                destination=pub.destination,
                page_url=pub.page_url,
                public_url=live_url,
                external_id=reddit_id,
                status="published",
                metadata=new_metadata,
            )
            repository.record_event(
                "release-comms",
                pub.run_id,
                "reddit_reconciled",
                "reddit_reconciled",
                metadata={
                    "public_url": live_url,
                    "reddit_id": reddit_id,
                    "source_id": source_id,
                    "title": matched_post.get("title", ""),
                },
            )
            if (
                notify_tracking_issue
                and project.release_comms
                and project.release_comms.tracking_issue
            ):
                try:
                    from dev_agents.runtime import gh

                    gh(
                        project.repo,
                        "issue",
                        "comment",
                        str(project.release_comms.tracking_issue),
                        "--body",
                        f"Reddit publication live on r/{clean_sub}: [{matched_post.get('title', 'Reddit Post')}]({live_url})",
                        check=False,
                    )
                except Exception:  # noqa: BLE001, S110
                    pass

            reconciled.append(updated_pub)

    return reconciled

