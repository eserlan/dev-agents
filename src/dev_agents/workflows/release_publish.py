"""Direct release-communications delivery adapters.

The daemon owns delivery so a target repository only supplies release copy,
its repository identity, and optional channel configuration.  Every successful
external write is returned as a small receipt; the workflow persists each
receipt immediately, making failed runs safe to retry.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dev_agents.config import ProjectConfig
from dev_agents.runtime.github import gh, gh_json
from dev_agents.workflows.release_content import derive_discord_message

ASSET_HOST = "assets.codexcryptica.com"
ASSET_BUCKET = "codex-cryptica-statics"
SOCIAL_IMAGE_OPTIONS = (
    "width=1080,height=1080,fit=cover,gravity=auto,format=jpeg,quality=75,metadata=none"
)
BLUESKY_LIMIT = 300
BLUESKY_IMAGE_LIMIT = 1_000_000


@dataclass(frozen=True)
class PublicationReceipt:
    channel: str
    destination: str
    page_url: str
    public_url: str | None = None
    external_id: str | None = None
    metadata: dict[str, Any] | None = None


class PublicationError(RuntimeError):
    """A channel could not accept a publication."""


def publication_key(channel: str, destination: str, page_url: str) -> tuple[str, str]:
    """Return the durable deduplication key for one channel publication."""
    if channel == "discord":
        return channel, f"{destination}\n{page_url}"
    return channel, page_url or destination


def _cloudflare_env(repo: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Add only the R2 token from a legacy target `.env` when service env lacks it."""
    command_env = dict(env)
    if command_env.get("CLOUDFLARE_API_TOKEN", "").strip():
        return command_env
    try:
        lines = (repo / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return command_env
    for line in lines:
        if line.startswith("CLOUDFLARE_API_TOKEN="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                command_env["CLOUDFLARE_API_TOKEN"] = value
            break
    return command_env


def upload_release_image(
    *, repo: Path, path: Path, key: str, env: Mapping[str, str], timeout: float = 120
) -> str:
    """Upload generated announcement art to the public R2 asset bucket."""
    if not path.is_file():
        raise PublicationError(f"generated image does not exist: {path}")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", key) or key.startswith("/"):
        raise PublicationError(f"invalid R2 image key: {key}")

    if shutil.which("wrangler"):
        command = ["wrangler"]
    elif shutil.which("bunx"):
        command = ["bunx", "wrangler"]
    else:
        raise PublicationError("wrangler or bunx is required to upload generated images")
    command.extend(
        [
            "r2",
            "object",
            "put",
            f"{ASSET_BUCKET}/{key}",
            "--file",
            str(path),
            "--content-type",
            "image/png",
            "--remote",
        ]
    )
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            env=_cloudflare_env(repo, env),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PublicationError(f"R2 image upload failed for {key}: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "wrangler failed"
        raise PublicationError(f"R2 image upload failed for {key}: {detail}")
    return f"https://{ASSET_HOST}/{key}"


def social_delivery_image_url(image_url: str) -> str:
    """Return the cached square R2 variant used by feed-oriented channels."""
    try:
        url = urllib.parse.urlsplit(image_url)
    except ValueError:
        return image_url
    if (
        url.scheme != "https"
        or url.hostname != ASSET_HOST
        or url.path.startswith("/cdn-cgi/image/")
    ):
        return image_url
    return urllib.parse.urlunsplit(
        (url.scheme, url.netloc, f"/cdn-cgi/image/{SOCIAL_IMAGE_OPTIONS}{url.path}", url.query, "")
    )


def prepare_bluesky_text(text: str, page_url: str) -> str:
    """Resolve a page URL and keep the post within Bluesky's grapheme budget."""
    body = " ".join(text.strip().split())
    url = page_url.strip()
    suffix = f" {url}" if url and url not in body else ""
    budget = BLUESKY_LIMIT - len(suffix)
    if budget <= 0:
        return suffix.strip()
    if len(body) > budget:
        body = (body[: budget - 1].rstrip() + "…") if budget > 1 else ""
    return (body + suffix).strip()


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    body: bytes | None = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
        status = getattr(error, "code", None)
        suffix = f" status {status}" if status else ""
        raise PublicationError(f"HTTP {method} {url} failed{suffix}") from error
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"HTTP {method} {url} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise PublicationError(f"HTTP {method} {url} returned a non-object JSON value")
    return value


def _http_bytes(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 60) -> bytes:
    request_headers = {"User-Agent": "dev-agents/release-comms"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = bytes(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
        raise PublicationError(f"could not download image {url}") from error
    if len(data) > BLUESKY_IMAGE_LIMIT:
        raise PublicationError(f"image is {len(data)} bytes, over the 1 MB Bluesky limit")
    return data


def _image_mime(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _facets(text: str) -> list[dict[str, Any]]:
    """Build UTF-8 link and hashtag facets for AT Protocol rich text."""
    facets: list[dict[str, Any]] = []

    def utf8_len(value: str) -> int:
        return len(value.encode("utf-8"))

    patterns = [
        (re.compile(r"https?://\S+|[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+\S*"), "link"),
        (re.compile(r"#[A-Za-z][\w-]*"), "tag"),
    ]
    for pattern, kind in patterns:
        for match in pattern.finditer(text):
            visible = match.group(0).rstrip(".,;:!?)]}")
            if not visible:
                continue
            start = utf8_len(text[: match.start()])
            end = start + utf8_len(visible)
            if kind == "tag":
                feature = {"$type": "app.bsky.richtext.facet#tag", "tag": visible[1:]}
            else:
                uri = visible if visible.startswith("http") else f"https://{visible}"
                feature = {"$type": "app.bsky.richtext.facet#link", "uri": uri}
            facets.append({"index": {"byteStart": start, "byteEnd": end}, "features": [feature]})
    return sorted(facets, key=lambda item: item["index"]["byteStart"])


def publish_bluesky(
    *,
    text: str,
    page_url: str,
    image_url: str,
    image_alt: str,
    env: Mapping[str, str],
    dry_run: bool,
) -> PublicationReceipt:
    resolved = prepare_bluesky_text(text, page_url)
    delivery_url = social_delivery_image_url(image_url)
    if dry_run:
        return PublicationReceipt("bluesky", "bluesky", page_url, f"dry-run://bluesky/{page_url}")
    identifier = env.get("BLUESKY_IDENTIFIER", "").strip()
    password = env.get("BLUESKY_APP_PASSWORD", "").strip()
    if not identifier or not password:
        raise PublicationError("BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD are required")
    pds = env.get("BLUESKY_PDS_URL", "https://bsky.social").rstrip("/")
    session = _http_json(
        f"{pds}/xrpc/com.atproto.server.createSession",
        method="POST",
        payload={"identifier": identifier, "password": password},
    )
    access_jwt = str(session.get("accessJwt", ""))
    did = str(session.get("did", ""))
    handle = str(session.get("handle", did))
    if not access_jwt or not did:
        raise PublicationError("Bluesky session returned no credentials")
    blob_request = urllib.request.Request(
        f"{pds}/xrpc/com.atproto.repo.uploadBlob",
        data=_http_bytes(delivery_url),
        headers={"Content-Type": _image_mime(delivery_url), "Authorization": f"Bearer {access_jwt}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(blob_request, timeout=60) as response:
            blob = json.loads(response.read().decode()).get("blob")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise PublicationError("Bluesky image upload failed") from error
    if not isinstance(blob, dict):
        raise PublicationError("Bluesky image upload returned no blob")
    record = {
        "$type": "app.bsky.feed.post",
        "text": resolved,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "facets": _facets(resolved),
        "embed": {
            "$type": "app.bsky.embed.images",
            "images": [{"image": blob, "alt": image_alt}],
        },
    }
    created = _http_json(
        f"{pds}/xrpc/com.atproto.repo.createRecord",
        method="POST",
        payload={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        headers={"Authorization": f"Bearer {access_jwt}"},
    )
    uri = str(created.get("uri", ""))
    rkey = uri.rsplit("/", 1)[-1]
    if not rkey:
        raise PublicationError("Bluesky post returned no record URI")
    return PublicationReceipt("bluesky", "bluesky", page_url, f"https://bsky.app/profile/{handle}/post/{rkey}")


def publish_github_discussion(
    *, repo: Path, github: str, title: str, body: str, page_url: str, dry_run: bool
) -> PublicationReceipt:
    if dry_run:
        return PublicationReceipt("github_discussions", "github_discussions", page_url, f"dry-run://github-discussion/{page_url}")
    owner, name = github.split("/", 1)
    query = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){id discussionCategories(first:50){nodes{id name}}}}"""
    data = gh_json(repo, "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}")
    repository = data.get("data", {}).get("repository") or {}
    categories = repository.get("discussionCategories") or {}
    category = next((item for item in categories.get("nodes", []) if str(item.get("name", "")).lower() == "announcements"), None)
    if not category:
        raise PublicationError("GitHub Discussions has no Announcements category")
    mutation = """mutation($repositoryId:ID!,$categoryId:ID!,$title:String!,$body:String!){createDiscussion(input:{repositoryId:$repositoryId,categoryId:$categoryId,title:$title,body:$body}){discussion{url}}}"""
    created = gh_json(
        repo,
        "api",
        "graphql",
        "-f",
        f"query={mutation}",
        "-f",
        f"repositoryId={repository.get('id', '')}",
        "-f",
        f"categoryId={category.get('id', '')}",
        "-f",
        f"title={title}",
        "-f",
        f"body={body}",
    )
    url = created.get("data", {}).get("createDiscussion", {}).get("discussion", {}).get("url")
    if not url:
        raise PublicationError("GitHub Discussion creation returned no URL")
    return PublicationReceipt("github_discussions", "github_discussions", page_url, str(url))


def _discord_config(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".social/discord-destinations.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    discord = data.get("discord") if isinstance(data, dict) else None
    if isinstance(discord, dict) and discord.get("enabled") is False:
        return []
    if not isinstance(discord, dict):
        return [{"id": "main-community", "webhookEnvVar": "DISCORD_WEBHOOK_URL"}]
    destinations = discord.get("destinations")
    if not isinstance(destinations, list):
        return [{"id": "main-community", "webhookEnvVar": "DISCORD_WEBHOOK_URL"}]
    return [item for item in destinations if isinstance(item, dict) and item.get("auto_publish", True)]


def publish_discord(
    *,
    repo: Path,
    message: str,
    env: Mapping[str, str],
    dry_run: bool,
    page_url: str = "",
    initial_delay_min_seconds: float = 0.0,
    initial_delay_max_seconds: float = 0.0,
    between_delay_min_seconds: float = 0.0,
    between_delay_max_seconds: float = 0.0,
    max_publications: int | None = None,
) -> list[PublicationReceipt]:
    receipts: list[PublicationReceipt] = []
    for index, destination in enumerate(_discord_config(repo)):
        destination_id = str(destination.get("id", "main-community"))
        variable = str(destination.get("webhookEnvVar", "DISCORD_WEBHOOK_URL"))
        webhook = env.get(variable) or env.get(f"DISCORD_WEBHOOK_URL_{re.sub(r'[^A-Za-z0-9]', '_', destination_id.upper())}") or env.get("DISCORD_WEBHOOK_URL")
        if dry_run:
            receipts.append(PublicationReceipt("discord", destination_id, page_url, f"dry-run://discord/{destination_id}"))
            continue
        if not webhook:
            raise PublicationError(f"no webhook configured for Discord destination {destination_id}")
        if not dry_run:
            delay_min = (
                initial_delay_min_seconds if index == 0 else between_delay_min_seconds
            )
            delay_max = (
                initial_delay_max_seconds if index == 0 else between_delay_max_seconds
            )
            delay_min = max(0.0, delay_min)
            delay_max = max(delay_min, delay_max)
            delay = random.uniform(delay_min, delay_max)
            if delay > 0:
                time.sleep(delay)
        _http_json(webhook, method="POST", payload={"content": message})
        receipts.append(PublicationReceipt("discord", destination_id, page_url, None))
        if max_publications is not None and len(receipts) >= max_publications:
            break
    return receipts


def publish_instagram(*, caption: str, page_url: str, image_url: str, env: Mapping[str, str], dry_run: bool) -> PublicationReceipt:
    delivery_url = social_delivery_image_url(image_url)
    if dry_run:
        return PublicationReceipt("instagram", "instagram", page_url, f"dry-run://instagram/{page_url}")
    account = env.get("INSTAGRAM_ACCOUNT_ID", "").strip()
    token = env.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    graph = env.get("INSTAGRAM_GRAPH_API_URL", "").strip().rstrip("/")
    if not account or not token or not graph:
        raise PublicationError("Instagram account, access token, and Graph API URL are required")
    form = urllib.parse.urlencode({"image_url": delivery_url, "caption": caption, "access_token": token}).encode()
    create = _form_json(f"{graph}/{account}/media", form)
    container = str(create.get("id", ""))
    if not container:
        raise PublicationError("Instagram media creation returned no container ID")
    for _ in range(30):
        status = _form_json(f"{graph}/{container}?{urllib.parse.urlencode({'fields': 'status_code', 'access_token': token})}", None, method="GET")
        if status.get("status_code") == "FINISHED":
            break
        if status.get("status_code") == "ERROR":
            raise PublicationError("Instagram media processing failed")
        time.sleep(2)
    else:
        raise PublicationError("Instagram media processing timed out")
    published = _form_json(f"{graph}/{account}/media_publish", urllib.parse.urlencode({"creation_id": container, "access_token": token}).encode())
    media_id = str(published.get("id", ""))
    if not media_id:
        raise PublicationError("Instagram publish returned no media ID")
    permalink = _form_json(f"{graph}/{media_id}?{urllib.parse.urlencode({'fields': 'permalink', 'access_token': token})}", None, method="GET").get("permalink")
    return PublicationReceipt("instagram", "instagram", page_url, str(permalink or media_id), media_id)


def _form_json(url: str, body: bytes | None, *, method: str = "POST") -> dict[str, Any]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            value = json.loads(response.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise PublicationError(f"Instagram request failed for {url}") from error
    if not isinstance(value, dict):
        raise PublicationError("Instagram returned invalid JSON")
    return value


def publish_x(*, text: str, page_url: str, env: Mapping[str, str], dry_run: bool) -> PublicationReceipt:
    if dry_run:
        return PublicationReceipt("x", "x", page_url, f"dry-run://x/{page_url}")
    token = env.get("X_ACCESS_TOKEN", "").strip()
    if not token:
        raise PublicationError("X_ACCESS_TOKEN is required")
    result = _http_json(env.get("X_POST_URL", "https://api.x.com/2/tweets"), method="POST", payload={"text": text}, headers={"Authorization": f"Bearer {token}"})
    data = result.get("data") or {}
    post_id = str(data.get("id", ""))
    if not post_id:
        raise PublicationError("X post creation returned no ID")
    return PublicationReceipt("x", "x", page_url, f"https://x.com/i/web/status/{post_id}", post_id)


def resolve_image(
    draft: Mapping[str, str], image_overrides: Mapping[str, str] | None = None
) -> tuple[str, str]:
    page_url = str(draft.get("pageUrl", ""))
    image = str(draft.get("image", "")).strip()
    image_url = (image_overrides or {}).get(page_url, "").strip()
    if not image_url and image.startswith("capture:"):
        raise PublicationError(f"image capture was not uploaded: {image[8:]}")
    if not image_url and image.startswith(("http://", "https://")):
        image_url = image
    elif not image_url:
        key = image.lstrip("/")
        if not key:
            slug = urllib.parse.urlsplit(page_url).path.rstrip("/").split("/")[-1]
            if not slug:
                raise PublicationError("draft has no page URL or image asset")
            key = f"og/{slug}.jpg"
        image_url = f"https://{ASSET_HOST}/{key}"
    return image_url, f"A tabletop roleplaying illustration for {page_url}"


def publish_release_drafts(
    *,
    project: ProjectConfig,
    drafts: Mapping[str, Any],
    recommended_channels: list[str],
    env: Mapping[str, str],
    dry_run: bool,
    already_published: set[tuple[str, str]],
    image_overrides: Mapping[str, str] | None = None,
    publication_delay_min_seconds: float = 900.0,
    publication_delay_max_seconds: float = 1800.0,
    max_publications: int | None = None,
    on_receipt: Callable[[PublicationReceipt], None],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Publish channel variants in message batches, checkpointing each receipt.

    A batch represents one feature/page message. All enabled channel variants
    of that message are attempted without an inter-channel delay. The delay,
    when enabled, is only applied before a subsequent distinct message.
    """
    result: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("bluesky", "github_discussions", "instagram", "x", "discord")
    }
    errors: list[str] = []
    message_batches_published = 0

    def record_receipt(receipt: PublicationReceipt) -> None:
        on_receipt(receipt)
        already_published.add(
            publication_key(receipt.channel, receipt.destination, receipt.page_url)
        )

    def batch_limit_reached() -> bool:
        return (
            max_publications is not None
            and message_batches_published >= max_publications
        )

    def wait_before_message() -> None:
        if not dry_run and message_batches_published:
            delay_min = max(0.0, publication_delay_min_seconds)
            delay_max = max(delay_min, publication_delay_max_seconds)
            delay = random.uniform(delay_min, delay_max)
            if delay > 0:
                time.sleep(delay)

    bluesky_drafts = drafts.get("bluesky") or []
    discussion_drafts = [
        draft for draft in (drafts.get("github_discussions") or []) if isinstance(draft, dict)
    ]
    used_discussions: set[int] = set()
    batches: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []

    for index, draft in enumerate(bluesky_drafts):
        if not isinstance(draft, dict):
            continue
        page_url = str(draft.get("pageUrl", ""))
        discussion_index = next(
            (
                candidate
                for candidate, discussion in enumerate(discussion_drafts)
                if candidate not in used_discussions
                and str(discussion.get("pageUrl", "")) == page_url
                and page_url
            ),
            index if index < len(discussion_drafts) and index not in used_discussions else None,
        )
        discussion = None
        if discussion_index is not None and discussion_index < len(discussion_drafts):
            discussion = discussion_drafts[discussion_index]
            used_discussions.add(discussion_index)
        batches.append((draft, discussion))

    for index, discussion in enumerate(discussion_drafts):
        if index not in used_discussions:
            batches.append((None, discussion))

    # Backwards-compatible fallback for manually injected legacy state that
    # has only a combined Discord draft and no Bluesky drafts.
    if not batches and drafts.get("discord"):
        batches.append((None, {"text": str(drafts["discord"]), "pageUrl": ""}))

    for draft, discussion in batches:
        page_url = str((draft or discussion or {}).get("pageUrl", ""))
        draft_text = str((draft or {}).get("text", ""))
        pending = False
        if draft is not None:
            pending = any(
                channel in recommended_channels
                and publication_key(channel, channel, page_url) not in already_published
                for channel in ("bluesky", "instagram", "x")
            )
        if discussion is not None and "github_discussions" in recommended_channels:
            pending = pending or publication_key(
                "github_discussions", "github_discussions", page_url
            ) not in already_published
        if "discord" in recommended_channels and draft is not None:
            pending = pending or any(
                publication_key("discord", str(destination.get("id", "main-community")), page_url)
                not in already_published
                for destination in _discord_config(project.repo)
            )
        if draft is None and discussion is None:
            pending = "discord" in recommended_channels and bool(drafts.get("discord"))
        if not pending:
            continue

        wait_before_message()
        errors_before_batch = len(errors)
        image_url = image_alt = ""
        if draft is not None:
            try:
                image_url, image_alt = resolve_image(draft, image_overrides)
            except Exception as error:  # noqa: BLE001
                errors.append(f"image {page_url}: {error}")

        if draft is not None and image_url:
            if "bluesky" in recommended_channels and publication_key("bluesky", "bluesky", page_url) not in already_published:
                try:
                    receipt = publish_bluesky(text=draft_text, page_url=page_url, image_url=image_url, image_alt=image_alt, env=env, dry_run=dry_run)
                    record_receipt(receipt)
                    result["bluesky"].append(receipt.__dict__)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"bluesky {page_url}: {error}")
            if "instagram" in recommended_channels and publication_key("instagram", "instagram", page_url) not in already_published:
                try:
                    receipt = publish_instagram(caption=prepare_bluesky_text(draft_text, page_url), page_url=page_url, image_url=image_url, env=env, dry_run=dry_run)
                    record_receipt(receipt)
                    result["instagram"].append(receipt.__dict__)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"instagram {page_url}: {error}")
            if "x" in recommended_channels and publication_key("x", "x", page_url) not in already_published:
                try:
                    receipt = publish_x(text=prepare_bluesky_text(draft_text, page_url), page_url=page_url, env=env, dry_run=dry_run)
                    record_receipt(receipt)
                    result["x"].append(receipt.__dict__)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"x {page_url}: {error}")
            if "discord" in recommended_channels:
                try:
                    for receipt in publish_discord(
                        repo=project.repo,
                        message=derive_discord_message(prepare_bluesky_text(draft_text, page_url)),
                        env=env,
                        dry_run=dry_run,
                        page_url=page_url,
                    ):
                        key = publication_key("discord", receipt.destination, page_url)
                        if key in already_published:
                            continue
                        record_receipt(receipt)
                        result["discord"].append(receipt.__dict__)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"discord {page_url}: {error}")

        if discussion is not None and "github_discussions" in recommended_channels and publication_key("github_discussions", "github_discussions", page_url) not in already_published:
            try:
                discussion_image, discussion_alt = resolve_image(discussion, image_overrides)
                body = f"{discussion.get('body', '').strip()}\n\n![{discussion_alt}]({discussion_image})"
                receipt = publish_github_discussion(repo=project.repo, github=project.github or "", title=str(discussion.get("title", "Release update")), body=body, page_url=page_url, dry_run=dry_run)
                record_receipt(receipt)
                result["github_discussions"].append(receipt.__dict__)
            except Exception as error:  # noqa: BLE001
                errors.append(f"github_discussions {page_url}: {error}")

        if len(errors) > errors_before_batch:
            return result, errors
        message_batches_published += 1
        if batch_limit_reached():
            return result, errors
    return result, errors


def pending_publication_count(
    *,
    repo: Path,
    drafts: Mapping[str, Any],
    recommended_channels: list[str],
    already_published: set[tuple[str, str]],
) -> int:
    """Count external writes still needed for a release run."""
    count = 0
    for draft in drafts.get("bluesky") or []:
        if not isinstance(draft, dict):
            continue
        page_url = str(draft.get("pageUrl", ""))
        for channel in ("bluesky", "instagram", "x"):
            if channel in recommended_channels and publication_key(channel, channel, page_url) not in already_published:
                count += 1
        if "discord" in recommended_channels:
            count += sum(
                1
                for destination in _discord_config(repo)
                if publication_key(
                    "discord", str(destination.get("id", "main-community")), page_url
                )
                not in already_published
            )
    if "github_discussions" in recommended_channels:
        for draft in drafts.get("github_discussions") or []:
            if isinstance(draft, dict):
                page_url = str(draft.get("pageUrl", ""))
                if publication_key("github_discussions", "github_discussions", page_url) not in already_published:
                    count += 1
    if "discord" in recommended_channels and not drafts.get("bluesky") and drafts.get("discord"):
        count += sum(
            1
            for destination in _discord_config(repo)
            if publication_key(
                "discord", str(destination.get("id", "main-community")), ""
            )
            not in already_published
        )
    return count


def comment_tracking_issue(
    *,
    project: ProjectConfig,
    issue_number: int,
    promote_run_id: str,
    evaluator: Mapping[str, Any],
    drafts: Mapping[str, Any] | None,
    publications: Mapping[str, list[dict[str, Any]]],
    errors: list[str],
) -> None:
    """Leave the human-auditable release result on the configured issue."""
    lines = [
        f"## Release communications — promotion `{promote_run_id}`",
        "",
        f"**Postworthy:** {'yes' if evaluator.get('postworthy') else 'no'}  ",
        f"**Importance:** {evaluator.get('importance', 'medium')}  ",
        f"**Reason:** {evaluator.get('reason', '')}",
        "",
        "### Delivery",
    ]
    for channel, items in publications.items():
        if items:
            links = [f"[{item.get('page_url', '')}]({item.get('public_url')})" for item in items if item.get("public_url")]
            lines.append(f"- **{channel}:** " + (", ".join(links) if links else "published"))
    if not publications or not any(publications.values()):
        lines.append("- No external publications recorded.")
    if errors:
        lines.extend(["", "### Delivery errors", *[f"- {error}" for error in errors]])
    if drafts:
        lines.extend(["", "### Draft counts", f"- Bluesky: {len(drafts.get('bluesky') or [])}", f"- GitHub Discussions: {len(drafts.get('github_discussions') or [])}", f"- Discord: {'yes' if drafts.get('discord') else 'no'}"])
    gh(project.repo, "issue", "comment", str(issue_number), "--body", "\n".join(lines), check=False)
