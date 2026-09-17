"""Reddit publishing adapter: payload formatting, candidate staging, and export.

Separates asynchronous Reddit staging from direct synchronous platform delivery.
Target repositories supply only release copy; dev-agents stages candidate
manifests to GitHub (or Cloudflare R2) for ingestion by the Devvit companion app.
"""

from __future__ import annotations

import base64
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

    # Convert any markdown image embeds ![alt](url) to clean clickable links
    # because Reddit selftext does not render external image embeds inline.
    clean_body = re.sub(
        r"!\[([^\]]*)\]\((https?://[^)]+)\)",
        lambda m: f"[🖼️ {m.group(1).strip() or 'View Illustration'}]({m.group(2)})",
        clean_body,
    )

    if image_url and image_url not in clean_body:
        clean_body = f"{clean_body}\n\n[🖼️ View Illustration / Reference Guide]({image_url})"

    id_tag = f"<!-- id:{source_id} -->" if source_id else ""
    standard_footer = "*Posted automatically via release pipeline. Feedback and discussion welcome!*"
    if standard_footer not in clean_body:
        footer = f"\n\n---\n{standard_footer}"
        if id_tag and id_tag not in clean_body:
            footer += f"\n{id_tag}"
        formatted_body = f"{clean_body}{footer}"
    else:
        formatted_body = clean_body
        if id_tag and id_tag not in clean_body:
            formatted_body += f"\n{id_tag}"

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.lower()).strip("-")[:40]
    candidate_id = (
        f"reddit-{source_id}-{slug}"
        if source_id and slug
        else (f"reddit-{source_id}" if source_id else f"reddit-{slug}")
    )
    return {
        "id": candidate_id or "reddit-candidate",
        "title": title,
        "body": formatted_body,
        "url": page_url,
        "image_url": image_url or "",
        "source_id": source_id,
        "status": "approved",
        "created_at": int(time.time()),
    }


DEFAULT_MANIFEST_KEY = "announcements/reddit-candidates.json"


def _fetch_manifest_from_url(url: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Fetch existing candidates from a manifest URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "dev-agents/release-comms"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("candidates"), list):
                return [c for c in data["candidates"] if isinstance(c, dict)]
            elif isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
    except Exception:  # noqa: BLE001
        return []
    return []


def _upload_github_manifest(
    *,
    repo: Path,
    github: str,
    branch: str,
    path: str,
    manifest: Mapping[str, Any],
    timeout: float = 60.0,
) -> str:
    """Commit an updated JSON manifest to GitHub using gh CLI."""
    from dev_agents.runtime import gh, gh_json

    content_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    b64_content = base64.b64encode(content_bytes).decode("ascii")

    # Check if branch exists; if not, create it from repository default branch
    try:
        gh(repo, "api", f"/repos/{github}/git/ref/heads/{branch}", timeout=timeout)
    except Exception:  # noqa: BLE001
        repo_info = gh_json(repo, "api", f"/repos/{github}", timeout=timeout)
        default_branch = repo_info.get("default_branch", "main")
        ref_info = gh_json(repo, "api", f"/repos/{github}/git/ref/heads/{default_branch}", timeout=timeout)
        commit_sha = ref_info["object"]["sha"]
        gh(
            repo,
            "api",
            "--method",
            "POST",
            f"/repos/{github}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={commit_sha}",
            timeout=timeout,
        )

    # Get existing file SHA if it exists on branch
    sha: str | None = None
    try:
        existing_file = gh_json(
            repo, "api", f"/repos/{github}/contents/{path}?ref={branch}", timeout=timeout
        )
        if isinstance(existing_file, dict) and existing_file.get("sha"):
            sha = str(existing_file["sha"])
    except Exception:  # noqa: BLE001
        sha = None

    args = [
        "api",
        "--method",
        "PUT",
        f"/repos/{github}/contents/{path}",
        "-f",
        f"message=Update {path}",
        "-f",
        f"content={b64_content}",
        "-f",
        f"branch={branch}",
    ]
    if sha:
        args.extend(["-f", f"sha={sha}"])
    gh(repo, *args, timeout=timeout)

    return f"https://raw.githubusercontent.com/{github}/{branch}/{path}"


def stage_reddit_candidate(
    *,
    repo: Path,
    candidate: Mapping[str, Any],
    env: Mapping[str, str],
    dry_run: bool,
    destination: str = "reddit",
    key: str = DEFAULT_MANIFEST_KEY,
    github: str | None = None,
    branch: str = "release-manifests",
    timeout: float = 120,
) -> PublicationReceipt:
    """Upload or update the Reddit candidate manifest on GitHub (or Cloudflare R2)."""
    page_url = str(candidate.get("url", ""))
    source_id = str(candidate.get("source_id", ""))
    candidate_id = str(candidate.get("id", ""))
    external_id = f"staged:{source_id}" if source_id else f"staged:{candidate_id}"

    if github:
        if dry_run:
            return PublicationReceipt(
                channel="reddit",
                destination=destination,
                page_url=page_url,
                public_url=f"dry-run://github/{github}/{branch}/{key}",
                external_id=external_id,
                metadata={
                    "status": "staged_to_github",
                    "candidate_id": candidate_id,
                    "source_id": source_id,
                    "github": github,
                    "branch": branch,
                },
            )

        manifest_url = f"https://raw.githubusercontent.com/{github}/{branch}/{key}"
        existing_candidates = _fetch_manifest_from_url(manifest_url, timeout=15)
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

        public_url = _upload_github_manifest(
            repo=repo,
            github=github,
            branch=branch,
            path=key,
            manifest=manifest,
            timeout=timeout,
        )

        return PublicationReceipt(
            channel="reddit",
            destination=destination,
            page_url=page_url,
            public_url=public_url,
            external_id=external_id,
            metadata={
                "status": "staged_to_github",
                "candidate_id": candidate_id,
                "source_id": source_id,
                "github": github,
                "branch": branch,
            },
        )

    # Fallback to R2 staging when github is not provided
    if dry_run:
        return PublicationReceipt(
            channel="reddit",
            destination=destination,
            page_url=page_url,
            public_url=f"dry-run://r2/{key}",
            external_id=external_id,
            metadata={"status": "staged_to_r2", "candidate_id": candidate_id, "source_id": source_id},
        )

    existing_candidates = _fetch_manifest_from_url(f"https://{ASSET_HOST}/{key}", timeout=15)
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


def prune_published_reddit_candidates(
    *,
    repo: Path,
    published_source_ids: set[str],
    key: str = DEFAULT_MANIFEST_KEY,
    github: str | None = None,
    branch: str = "release-manifests",
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    dry_run: bool = False,
) -> int:
    """Remove published candidates from the candidate manifest (GitHub or R2).

    Returns the number of candidates removed.
    """
    if not published_source_ids or dry_run:
        return 0

    if github:
        manifest_url = f"https://raw.githubusercontent.com/{github}/{branch}/{key}"
        existing_candidates = _fetch_manifest_from_url(manifest_url, timeout=15)
        remaining_candidates = [
            c
            for c in existing_candidates
            if str(c.get("source_id", "")) not in published_source_ids
            and str(c.get("id", "")) not in published_source_ids
        ]
        pruned_count = len(existing_candidates) - len(remaining_candidates)
        if pruned_count == 0:
            return 0

        manifest = {
            "updated_at": int(time.time()),
            "candidates": remaining_candidates,
        }
        _upload_github_manifest(
            repo=repo,
            github=github,
            branch=branch,
            path=key,
            manifest=manifest,
            timeout=timeout,
        )
        return pruned_count

    # Fallback to R2
    existing_candidates = _fetch_manifest_from_url(f"https://{ASSET_HOST}/{key}", timeout=15)
    remaining_candidates = [
        c
        for c in existing_candidates
        if str(c.get("source_id", "")) not in published_source_ids
        and str(c.get("id", "")) not in published_source_ids
    ]
    pruned_count = len(existing_candidates) - len(remaining_candidates)
    if pruned_count == 0:
        return 0

    manifest = {
        "updated_at": int(time.time()),
        "candidates": remaining_candidates,
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as temp:
        temp_path = Path(temp.name)
        json.dump(manifest, temp, indent=2)

    try:
        _upload_r2_file(
            repo=repo,
            path=temp_path,
            key=key,
            content_type="application/json",
            env=env or {},
            timeout=timeout,
        )
    finally:
        temp_path.unlink(missing_ok=True)

    return pruned_count


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []

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
    prune_manifest: bool = True,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
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

    if reconciled and prune_manifest and not dry_run:
        published_ids = {
            str(pub.metadata.get("source_id", ""))
            for pub in reconciled
            if pub.metadata.get("source_id")
        }
        published_ids.update({
            pub.external_id.removeprefix("staged:")
            for pub in reconciled
            if pub.external_id and pub.external_id.startswith("staged:")
        })
        published_ids.update({
            str(pub.metadata.get("candidate_id", ""))
            for pub in reconciled
            if pub.metadata.get("candidate_id")
        })
        published_ids.discard("")
        if published_ids:
            try:
                prune_published_reddit_candidates(
                    repo=project.repo,
                    published_source_ids=published_ids,
                    env=env,
                    github=getattr(project, "github", None),
                )
            except Exception:  # noqa: BLE001, S110
                pass

    return reconciled

