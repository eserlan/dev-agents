import subprocess
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


def _fake_bun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, install: str = "exit 0") -> Path:
    """Put a `bun` shim on PATH that logs argv, cwd and the checked-out answer, then runs a script.

    `install` runs for `bun install`; `script` runs for every other invocation.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "bun-calls.log"
    shim = bin_dir / "bun"
    shim.write_text(
        f'#!/bin/sh\necho "$PWD $* [$(cat answer.txt 2>/dev/null)]" >> "{log}"\n'
        f'if [ "$1" = install ]; then\n{install}\nfi\n{script}\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return log


def _release_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose released commit differs from what its working tree has since moved to."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    (repo / "answer.txt").write_text("released", encoding="utf-8")
    git("add", "answer.txt")
    git("commit", "-q", "-m", "release")
    sha = git("rev-parse", "HEAD")
    (repo / "answer.txt").write_text("later", encoding="utf-8")
    git("commit", "-q", "-am", "after release")
    return repo, sha


def _worktrees(repo: Path) -> list[str]:
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo, check=True, capture_output=True, text=True)
    return listing.stdout.strip().splitlines()


ANSWER_URL = "https://codexcryptica.com/answers/does-an-rpg-party-need-balanced-roles"
PODCAST_RESULT = (
    '{"guid": "codex-cryptica:answer:does-an-rpg-party-need-balanced-roles", '
    '"audioUrl": "https://assets.codexcryptica.com/podcast/answers/audio/a-0123456789ab.mp3", '
    '"status": "created"}'
)


def test_podcast_answer_slug_accepts_only_canonical_answer_pages() -> None:
    from dev_agents.workflows.release_publish import podcast_answer_slug

    assert podcast_answer_slug(ANSWER_URL) == "does-an-rpg-party-need-balanced-roles"
    assert podcast_answer_slug("https://codexcryptica.com/blog/a-post") is None
    assert podcast_answer_slug("https://codexcryptica.com/answers/a/b") is None
    assert podcast_answer_slug("https://example.com/answers/a") is None
    assert podcast_answer_slug("https://codexcryptica.com/answers/$(rm)") is None


def test_publish_podcast_runs_target_command_in_the_released_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dev_agents.workflows.release_publish import publish_podcast

    repo, sha = _release_repo(tmp_path)
    root = tmp_path / "worktrees"
    log = _fake_bun(tmp_path, monkeypatch, f"echo 'encoding audio'\necho '{PODCAST_RESULT}'")

    receipt = publish_podcast(
        repo=repo, page_url=ANSWER_URL, release_sha=sha, env={"PATH": "/usr/bin:/bin"}, dry_run=False, worktree_root=root
    )

    install, run = log.read_text().strip().splitlines()
    tree = install.split(" ", 1)[0]
    # Locked dependencies, then the command, both in a worktree of the released commit,
    # never in the shared checkout that has since moved on.
    assert Path(tree).parent == root
    assert install == f"{tree} install --frozen-lockfile [released]"
    assert run == f"{tree} run podcast:publish does-an-rpg-party-need-balanced-roles [released]"
    assert (repo / "answer.txt").read_text() == "later"
    assert not Path(tree).exists() and len(_worktrees(repo)) == 1
    assert receipt.channel == "podcast"
    assert receipt.page_url == ANSWER_URL
    assert receipt.external_id == "codex-cryptica:answer:does-an-rpg-party-need-balanced-roles"
    assert receipt.public_url.endswith("a-0123456789ab.mp3")
    # The receipt is a feed receipt: Patreon's own publication is never confirmed.
    assert receipt.metadata == {"episode": "created", "patreon": "unconfirmed"}
    assert (repo / ".dev-agents/podcast-publish.lock").exists()


@pytest.mark.parametrize(
    "script, install, message",
    [
        ("echo 'boom' >&2\nexit 3", "exit 0", "run podcast:publish failed for .*boom"),
        ("exit 0", "echo 'lockfile drift' >&2\nexit 1", "install --frozen-lockfile failed for .*lockfile drift"),
        ("echo 'not json'", "exit 0", "did not print a JSON result"),
        ("""echo '{"guid": "g", "audioUrl": "https://evil.example/a.mp3", "status": "created"}'""",
         "exit 0", "invalid result"),
        ("""echo '{"guid": "g", "audioUrl": "https://assets.codexcryptica.com/a.mp3", "status": "queued"}'""",
         "exit 0", "invalid result"),
    ],
)
def test_publish_podcast_rejects_failed_or_malformed_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str, install: str, message: str
) -> None:
    from dev_agents.workflows.release_publish import publish_podcast

    repo, sha = _release_repo(tmp_path)
    root = tmp_path / "worktrees"
    _fake_bun(tmp_path, monkeypatch, script, install)
    with pytest.raises(PublicationError, match=message):
        publish_podcast(repo=repo, page_url=ANSWER_URL, release_sha=sha, env={}, dry_run=False, worktree_root=root)
    # A failed episode never leaves a worktree behind.
    assert list(root.iterdir()) == [] and len(_worktrees(repo)) == 1


@pytest.mark.parametrize("release_sha", ["", "HEAD", "--orphan", "0" * 40])
def test_publish_podcast_needs_a_checkout_of_the_released_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release_sha: str
) -> None:
    from dev_agents.workflows.release_publish import publish_podcast

    repo, _ = _release_repo(tmp_path)
    root = tmp_path / "worktrees"
    log = _fake_bun(tmp_path, monkeypatch, f"echo '{PODCAST_RESULT}'")
    # No release commit, a ref or option instead of a SHA, or a commit that cannot be
    # fetched: never fall back to the shared checkout.
    with pytest.raises(PublicationError, match="released commit|could not check out release"):
        publish_podcast(
            repo=repo, page_url=ANSWER_URL, release_sha=release_sha, env={}, dry_run=False, worktree_root=root
        )
    assert not log.exists()
    assert not root.exists() or list(root.iterdir()) == []


def test_publish_podcast_dry_run_and_non_answer_pages_never_run_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dev_agents.workflows.release_publish import publish_podcast

    log = _fake_bun(tmp_path, monkeypatch, "exit 1")
    receipt = publish_podcast(repo=tmp_path, page_url=ANSWER_URL, release_sha="", env={}, dry_run=True)
    assert receipt.public_url == "dry-run://podcast/does-an-rpg-party-need-balanced-roles"
    with pytest.raises(PublicationError, match="only published for answer pages"):
        publish_podcast(
            repo=tmp_path, page_url="https://codexcryptica.com/blog/x", release_sha="abc1234", env={}, dry_run=False
        )
    assert not log.exists()


def test_podcast_channel_publishes_answer_content_posts_only_and_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_podcast(**kwargs: object) -> PublicationReceipt:
        calls.append(str(kwargs["page_url"]))
        assert kwargs["release_sha"] == "abc1234def"

        return PublicationReceipt("podcast", "podcast", str(kwargs["page_url"]), "https://assets.codexcryptica.com/x.mp3")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_podcast", fake_podcast)
    blog_url = "https://codexcryptica.com/blog/a-post"
    drafts = {
        "instagram": [
            {"pageUrl": ANSWER_URL, "text": "Answer", "image": "https://assets.codexcryptica.com/og/a.jpg"},
            {"pageUrl": blog_url, "text": "Blog", "image": "https://assets.codexcryptica.com/og/b.jpg"},
        ],
    }
    receipts: list[PublicationReceipt] = []
    published: set[tuple[str, str]] = set()

    def run() -> tuple[dict[str, list[dict[str, object]]], list[str]]:
        return publish_release_drafts(
            project=ProjectConfig(repo=tmp_path, github="owner/repo"),
            drafts=drafts,
            recommended_channels=["podcast"],
            env={},
            dry_run=False,
            already_published=published,
            publication_delay_min_seconds=0,
            publication_delay_max_seconds=0,
            on_receipt=receipts.append,
            release_sha="abc1234def",
        )

    publications, errors = run()
    assert errors == []
    assert calls == [ANSWER_URL]
    assert [item["page_url"] for item in publications["podcast"]] == [ANSWER_URL]
    # A retry within the run sees the checkpointed key and does not republish.
    publications, errors = run()
    assert errors == [] and calls == [ANSWER_URL] and publications["podcast"] == []
    assert len(receipts) == 1


def test_podcast_failure_is_reported_without_a_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing(**kwargs: object) -> PublicationReceipt:
        raise PublicationError("tts failed")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_podcast", failing)
    receipts: list[PublicationReceipt] = []
    publications, errors = publish_release_drafts(
        project=ProjectConfig(repo=tmp_path, github="owner/repo"),
        drafts={"instagram": [{"pageUrl": ANSWER_URL, "text": "Answer", "image": "og/a.jpg"}]},
        recommended_channels=["podcast"],
        env={},
        dry_run=False,
        already_published=set(),
        on_receipt=receipts.append,
    )

    assert errors == [f"podcast {ANSWER_URL}: tts failed"]
    assert receipts == [] and publications["podcast"] == []
