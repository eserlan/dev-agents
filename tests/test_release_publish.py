from pathlib import Path
from typing import Self

import pytest

from dev_agents.config import ProjectConfig
from dev_agents.workflows.release_publish import (
    PublicationError,
    PublicationReceipt,
    publish_discord,
    publish_pinterest,
    publish_release_drafts,
    resolve_image,
    social_delivery_image_url,
    upload_release_image,
)


def test_social_delivery_image_url_is_square_and_idempotent() -> None:
    source = "https://assets.codexcryptica.com/og/example.jpg?version=2"
    transformed = social_delivery_image_url(source)
    assert transformed == (
        "https://assets.codexcryptica.com/cdn-cgi/image/"
        "width=1080,height=1080,fit=cover,gravity=auto,format=jpeg,quality=75,metadata=none/"
        "og/example.jpg?version=2"
    )
    assert social_delivery_image_url(transformed) == transformed
    assert social_delivery_image_url("https://other.example/image.jpg") == "https://other.example/image.jpg"


def test_http_image_download_sends_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from dev_agents.workflows.release_publish import _http_bytes

    seen: dict[str, str] = {}

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"image"

    def fake_urlopen(request: object, timeout: float = 0) -> Response:
        seen.update(getattr(request, "headers", {}))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _http_bytes("https://assets.codexcryptica.com/image.png") == b"image"
    assert seen["User-agent"] == "dev-agents/release-comms"


def test_discord_execution_uses_canonical_host_and_wait_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_http(url: str, **kwargs: object) -> dict[str, object]:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return {"id": "message"}

    monkeypatch.setattr("dev_agents.workflows.release_publish._http_json", fake_http)
    receipts = publish_discord(
        repo=tmp_path,
        message="Hello",
        page_url="https://example.com/a",
        env={"DISCORD_WEBHOOK_URL": "https://discordapp.com/api/webhooks/123/token"},
        dry_run=False,
    )

    assert len(receipts) == 1
    assert str(seen["url"]).startswith("https://discord.com/api/webhooks/123/token?")
    assert "wait=true" in str(seen["url"])
    assert seen["headers"] == {"User-Agent": "dev-agents/release-comms"}


def test_publish_pinterest_creates_a_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_http(url: str, **kwargs: object) -> dict[str, object]:
        seen["url"] = url
        seen["payload"] = kwargs.get("payload")
        seen["headers"] = kwargs.get("headers")
        return {"id": "pin123"}

    monkeypatch.setattr("dev_agents.workflows.release_publish._http_json", fake_http)
    receipt = publish_pinterest(
        title="Demo",
        description="Hello https://codexcryptica.com/answers/demo",
        page_url="https://codexcryptica.com/answers/demo",
        image_url="https://other.example/og/demo.jpg",
        env={"PINTEREST_ACCESS_TOKEN": "token", "PINTEREST_BOARD_ID": "board1"},
        dry_run=False,
    )

    assert receipt == PublicationReceipt(
        "pinterest",
        "pinterest",
        "https://codexcryptica.com/answers/demo",
        "https://www.pinterest.com/pin/pin123/",
        "pin123",
    )
    assert seen["url"] == "https://api.pinterest.com/v5/pins"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["board_id"] == "board1"
    assert payload["link"] == "https://codexcryptica.com/answers/demo"
    assert payload["media_source"] == {
        "source_type": "image_url",
        "url": "https://other.example/og/demo.jpg",
    }
    assert seen["headers"] == {"Authorization": "Bearer token"}


def test_publish_pinterest_requires_credentials() -> None:
    with pytest.raises(PublicationError, match="PINTEREST_ACCESS_TOKEN"):
        publish_pinterest(
            title="Demo",
            description="Hello",
            page_url="https://example.com/a",
            image_url="https://assets.codexcryptica.com/og/a.jpg",
            env={},
            dry_run=False,
        )


def test_resolve_image_uses_draft_key_or_page_slug() -> None:
    assert resolve_image({"pageUrl": "https://codexcryptica.com/answers/demo", "image": "og/demo.jpg"})[0] == "https://assets.codexcryptica.com/og/demo.jpg"
    assert resolve_image({"pageUrl": "https://codexcryptica.com/answers/demo", "image": ""})[0] == "https://assets.codexcryptica.com/og/demo.jpg"


def test_resolve_image_rejects_unuploaded_capture() -> None:
    with pytest.raises(PublicationError, match="capture was not uploaded"):
        resolve_image(
            {"pageUrl": "https://codexcryptica.com/answers/demo", "image": "capture:https://codexcryptica.com/answers/demo"}
        )


def test_upload_release_image_uses_remote_r2_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(b"png")
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr("dev_agents.workflows.release_publish.shutil.which", lambda name: "/usr/bin/wrangler" if name == "wrangler" else None)

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs["env"]))  # type: ignore[arg-type]
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("dev_agents.workflows.release_publish.subprocess.run", fake_run)
    url = upload_release_image(
        repo=tmp_path,
        path=image,
        key="announcements/release-123-0.png",
        env={"CLOUDFLARE_API_TOKEN": "token"},
    )

    assert url == "https://assets.codexcryptica.com/announcements/release-123-0.png"
    assert calls[0][0] == [
        "wrangler",
        "r2",
        "object",
        "put",
        "codex-cryptica-statics/announcements/release-123-0.png",
        "--file",
        str(image),
        "--content-type",
        "image/png",
        "--remote",
    ]
    assert calls[0][1] == {"CLOUDFLARE_API_TOKEN": "token"}


def test_upload_release_image_reads_only_legacy_cloudflare_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(b"png")
    (tmp_path / ".env").write_text(
        "BLUESKY_APP_PASSWORD=do-not-pass\nCLOUDFLARE_API_TOKEN=legacy-token\n",
        encoding="utf-8",
    )
    captured: list[dict[str, str]] = []
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.shutil.which",
        lambda name: "wrangler" if name == "wrangler" else None,
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.subprocess.run",
        lambda command, **kwargs: captured.append(kwargs["env"])
        or type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    upload_release_image(repo=tmp_path, path=image, key="announcements/a.png", env={})

    assert captured == [{"CLOUDFLARE_API_TOKEN": "legacy-token"}]


def test_publish_release_drafts_dry_run_emits_receipts_without_network(tmp_path: Path) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    seen = []
    publications, errors = publish_release_drafts(
        project=project,
        drafts={
            "bluesky": [{"pageUrl": "https://codexcryptica.com/answers/demo", "text": "Hello", "image": "og/demo.jpg"}],
            "github_discussions": [],
            "discord": "Hello",
        },
        recommended_channels=["bluesky", "instagram", "x", "pinterest", "discord"],
        env={},
        dry_run=True,
        already_published=set(),
        on_receipt=seen.append,
    )
    assert errors == []
    assert {receipt.channel for receipt in seen} == {
        "bluesky", "instagram", "x", "pinterest", "discord",
    }
    assert len(publications["discord"]) == 1


def test_discussion_uses_public_image_override_in_dry_run(tmp_path: Path) -> None:
    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    seen = []
    publications, errors = publish_release_drafts(
        project=project,
        drafts={
            "bluesky": [],
            "github_discussions": [
                {
                    "pageUrl": "https://codexcryptica.com/answers/demo",
                    "title": "Demo",
                    "body": "Details",
                    "image": "capture:https://codexcryptica.com/answers/demo",
                }
            ],
            "discord": "",
        },
        recommended_channels=["github_discussions"],
        env={},
        dry_run=True,
        already_published=set(),
        image_overrides={
            "https://codexcryptica.com/answers/demo":
                "https://assets.codexcryptica.com/announcements/release-1-0.png"
        },
        on_receipt=seen.append,
    )
    assert errors == []
    assert len(publications["github_discussions"]) == 1
    assert seen[0].public_url == "dry-run://github-discussion/https://codexcryptica.com/answers/demo"


def test_live_publications_are_spaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipts = []
    sleeps: list[float] = []

    def fake_publish(**kwargs: object) -> PublicationReceipt:
        return PublicationReceipt("bluesky", "bluesky", str(kwargs["page_url"]), "https://bsky.example/post")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_bluesky", fake_publish)
    monkeypatch.setattr("dev_agents.workflows.release_publish.random.uniform", lambda low, high: 1234)
    monkeypatch.setattr("dev_agents.workflows.release_publish.time.sleep", sleeps.append)
    publications, errors = publish_release_drafts(
        project=ProjectConfig(repo=tmp_path, github="owner/repo"),
        drafts={
            "bluesky": [
                {"pageUrl": "https://example.com/a", "text": "A", "image": "og/a.jpg"},
                {"pageUrl": "https://example.com/b", "text": "B", "image": "og/b.jpg"},
            ],
            "github_discussions": [],
            "discord": "",
        },
        recommended_channels=["bluesky"],
        env={},
        dry_run=False,
        already_published=set(),
        publication_delay_min_seconds=900,
        publication_delay_max_seconds=1800,
        on_receipt=receipts.append,
    )

    assert errors == []
    assert len(publications["bluesky"]) == 2
    assert len(receipts) == 2
    assert sleeps == [1234]


def test_channel_variants_share_a_batch_and_discord_uses_bluesky_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, str]] = []
    sleeps: list[float] = []

    def receipt(channel: str, page_url: str) -> PublicationReceipt:
        return PublicationReceipt(channel, channel, page_url, f"https://example.test/{channel}")

    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_bluesky",
        lambda **kwargs: (
            calls.append(("bluesky", str(kwargs["page_url"]), str(kwargs["text"])))
            or receipt("bluesky", str(kwargs["page_url"]))
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_instagram",
        lambda **kwargs: (
            calls.append(("instagram", str(kwargs["page_url"]), str(kwargs["caption"])))
            or receipt("instagram", str(kwargs["page_url"]))
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_x",
        lambda **kwargs: (
            calls.append(("x", str(kwargs["page_url"]), str(kwargs["text"])))
            or receipt("x", str(kwargs["page_url"]))
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_discord",
        lambda **kwargs: (
            calls.append(("discord", str(kwargs["page_url"]), str(kwargs["message"])))
            or [receipt("discord", str(kwargs["page_url"]))]
        ),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.random.uniform", lambda low, high: 1234
    )
    monkeypatch.setattr("dev_agents.workflows.release_publish.time.sleep", sleeps.append)

    publications, errors = publish_release_drafts(
        project=ProjectConfig(repo=tmp_path, github="owner/repo"),
        drafts={
            "bluesky": [
                {"pageUrl": "https://example.com/a", "text": "A #one", "image": "og/a.jpg"},
                {"pageUrl": "https://example.com/b", "text": "B #two", "image": "og/b.jpg"},
            ],
            "github_discussions": [],
        },
        recommended_channels=["bluesky", "instagram", "x", "discord"],
        env={},
        dry_run=False,
        already_published=set(),
        publication_delay_min_seconds=900,
        publication_delay_max_seconds=1800,
        on_receipt=lambda _: None,
    )

    assert errors == []
    assert [item[0] for item in calls] == [
        "bluesky", "instagram", "x", "discord",
        "bluesky", "instagram", "x", "discord",
    ]
    assert calls[3][2] == "A https://example.com/a"
    assert calls[7][2] == "B https://example.com/b"
    assert sleeps == [1234]
    assert len(publications["discord"]) == 2
