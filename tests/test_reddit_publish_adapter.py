import json
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from dev_agents.config import ProjectConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows.reddit import (
    clean_subreddit,
    export_reddit_markdown,
    fetch_subreddit_posts,
    format_reddit_post,
    prune_published_reddit_candidates,
    stage_reddit_candidate,
    sync_reddit_status,
)
from dev_agents.workflows.release_content import WriterResult, recommend_channels
from dev_agents.workflows.release_publish import (
    PublicationReceipt,
    pending_publication_count,
    publish_release_drafts,
)


def test_format_reddit_post_creates_expected_payload() -> None:
    post = format_reddit_post(
        title="Superheroes & Villains: Dynamic Alignment System",
        body="I built an alignment tracker because classical D&D alignments felt too rigid for modern supers.",
        page_url="https://codexcryptica.com/answers/superhero-alignments",
        source_id="pr-3093",
        image_url="https://assets.codexcryptica.com/og/superheroes.jpg",
    )

    assert post["id"].startswith("reddit-pr-3093-superheroes-villains-dynamic-alignment")
    assert post["title"] == "Superheroes & Villains: Dynamic Alignment System"
    assert "I built an alignment tracker" in post["body"]
    assert "*Posted automatically via release pipeline. Feedback and discussion welcome!*" in post["body"]
    assert "<!-- id:pr-3093 -->" in post["body"]
    assert post["url"] == "https://codexcryptica.com/answers/superhero-alignments"
    assert post["image_url"] == "https://assets.codexcryptica.com/og/superheroes.jpg"
    assert post["source_id"] == "pr-3093"
    assert post["status"] == "approved"
    assert isinstance(post["created_at"], int)


def test_stage_reddit_candidate_dry_run(tmp_path: Path) -> None:
    candidate = {
        "id": "reddit-pr-100-demo",
        "title": "Demo Post",
        "body": "Body text",
        "url": "https://codexcryptica.com/answers/demo",
        "source_id": "pr-100",
    }
    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate=candidate,
        env={},
        dry_run=True,
    )
    assert receipt.channel == "reddit"
    assert receipt.destination == "reddit"
    assert receipt.page_url == "https://codexcryptica.com/answers/demo"
    assert receipt.public_url == "dry-run://r2/announcements/reddit-candidates.json"
    assert receipt.external_id == "staged:pr-100"
    assert receipt.metadata is not None
    assert receipt.metadata["status"] == "staged_to_r2"


def test_stage_reddit_candidate_live_invokes_wrangler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = {
        "id": "reddit-pr-100-live",
        "title": "Live Post",
        "body": "Body text",
        "url": "https://codexcryptica.com/answers/live",
        "source_id": "pr-100",
    }

    uploaded_files: list[dict[str, Any]] = []

    def fake_upload_r2_file(*, repo: Path, path: Path, key: str, content_type: str, env: Any, timeout: float) -> str:
        uploaded_files.append({
            "key": key,
            "content_type": content_type,
            "data": json.loads(path.read_text(encoding="utf-8")),
        })
        return f"https://assets.codexcryptica.com/{key}"

    monkeypatch.setattr("dev_agents.workflows.reddit._upload_r2_file", fake_upload_r2_file)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=15.0: (_ for _ in ()).throw(FileNotFoundError("isolated test")))

    def no_existing_manifest(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("no existing manifest in test")

    monkeypatch.setattr(urllib.request, "urlopen", no_existing_manifest)

    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate=candidate,
        env={},
        dry_run=False,
    )

    assert receipt.channel == "reddit"
    assert receipt.public_url == "https://assets.codexcryptica.com/announcements/reddit-candidates.json"
    assert receipt.external_id == "staged:pr-100"
    assert len(uploaded_files) == 1
    manifest = uploaded_files[0]["data"]
    assert len(manifest["candidates"]) == 1
    assert manifest["candidates"][0]["id"] == "reddit-pr-100-live"


def test_stage_reddit_candidate_merges_with_existing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = {
        "id": "reddit-pr-99-old",
        "title": "Old Post",
        "body": "Old body",
        "url": "https://codexcryptica.com/answers/old",
        "source_id": "pr-99",
    }
    candidate = {
        "id": "reddit-pr-100-live",
        "title": "Live Post",
        "body": "Body text",
        "url": "https://codexcryptica.com/answers/live",
        "source_id": "pr-100",
    }

    uploaded_files: list[dict[str, Any]] = []

    def fake_upload_r2_file(*, repo: Path, path: Path, key: str, content_type: str, env: Any, timeout: float) -> str:
        uploaded_files.append({
            "key": key,
            "content_type": content_type,
            "data": json.loads(path.read_text(encoding="utf-8")),
        })
        return f"https://assets.codexcryptica.com/{key}"

    class _FakeManifestResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps({"candidates": [existing]}).encode("utf-8")

    monkeypatch.setattr("dev_agents.workflows.reddit._upload_r2_file", fake_upload_r2_file)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=15.0: _FakeManifestResponse()
    )

    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate=candidate,
        env={},
        dry_run=False,
    )

    assert receipt.external_id == "staged:pr-100"
    manifest = uploaded_files[0]["data"]
    assert [item["source_id"] for item in manifest["candidates"]] == ["pr-99", "pr-100"]

    # Re-staging the same source replaces instead of duplicating.
    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate={**candidate, "title": "Live Post (updated)"},
        env={},
        dry_run=False,
    )
    assert receipt.external_id == "staged:pr-100"
    manifest = uploaded_files[1]["data"]
    assert [item["source_id"] for item in manifest["candidates"]] == ["pr-99", "pr-100"]
    assert manifest["candidates"][1]["title"] == "Live Post (updated)"


def test_recommend_channels_includes_reddit_when_enabled() -> None:
    drafts = WriterResult(
        github_discussions=[{"pageUrl": "https://codexcryptica.com/answers/a", "title": "A", "body": "B"}],
    )
    # Default enabled does not include reddit
    assert "reddit" not in recommend_channels(drafts)
    # Explicit enabled includes reddit when discussions exist
    assert recommend_channels(drafts, enabled=("reddit",)) == ["reddit"]


def test_publish_release_drafts_stages_reddit_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/repo")

    drafts = {
        "bluesky": [],
        "github_discussions": [
            {
                "pageUrl": "https://codexcryptica.com/answers/supers",
                "title": "Superheroes System",
                "body": "Detailed discussion body",
                "image": "og/supers.jpg",
            }
        ],
    }

    receipts_recorded: list[PublicationReceipt] = []

    result, errors = publish_release_drafts(
        project=project,
        drafts=drafts,
        recommended_channels=["reddit"],
        env={},
        dry_run=True,
        already_published=set(),
        source_id="pr-999",
        on_receipt=receipts_recorded.append,
    )

    assert errors == []
    assert len(result["reddit"]) == 1
    assert len(receipts_recorded) == 1
    receipt = receipts_recorded[0]
    assert receipt.channel == "reddit"
    assert receipt.external_id == "staged:pr-999"
    assert receipt.page_url == "https://codexcryptica.com/answers/supers"


def test_pending_publication_count_includes_reddit(tmp_path: Path) -> None:
    drafts = {
        "github_discussions": [
            {"pageUrl": "https://codexcryptica.com/answers/supers", "title": "A", "body": "B"}
        ]
    }
    count = pending_publication_count(
        repo=tmp_path,
        drafts=drafts,
        recommended_channels=["reddit"],
        already_published=set(),
    )
    assert count == 1

    count_after = pending_publication_count(
        repo=tmp_path,
        drafts=drafts,
        recommended_channels=["reddit"],
        already_published={("reddit", "https://codexcryptica.com/answers/supers")},
    )
    assert count_after == 0


def test_cli_export_reddit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dev_agents.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    state_db = tmp_path / "state.db"

    config_path = tmp_path / "projects.yaml"
    config_path.write_text(
        f"projects:\n"
        f"  demo:\n"
        f"    repo: {repo}\n"
        f"    github: owner/demo\n"
        f"    release_comms:\n"
        f"      state_path: {state_db}\n"
    )

    state_repo = StateRepository(state_db, project_name="demo")
    state_repo.claim_run("release-comms", "run-42")
    state_repo.complete_run(
        "release-comms",
        "run-42",
        status="completed",
        metadata={
            "postworthy": True,
            "drafts": {
                "github_discussions": [
                    {
                        "pageUrl": "https://codexcryptica.com/answers/magic",
                        "title": "Magic Rules",
                        "body": "Here are the new magic rules.",
                    }
                ]
            },
        },
    )

    output_file = tmp_path / "exported.md"
    exit_code = main(["release-comms", "export-reddit", "demo", "run-42", "--config", str(config_path), "--output", str(output_file)])

    assert exit_code == 0
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert "Magic Rules" in content
    assert "## Image (Drag & drop into Reddit)" in content
    assert "https://codexcryptica.com/answers/magic" in content
    assert "Here are the new magic rules." in content
    assert "<!-- id:run-42 -->" in content


def test_export_reddit_markdown_formats_multiple_discussions() -> None:
    discussions = [
        {"title": "Post 1", "body": "Body 1", "pageUrl": "https://example.com/1"},
        {"title": "Post 2", "body": "Body 2", "pageUrl": "https://example.com/2"},
    ]
    markdown = export_reddit_markdown(discussions, source_id="pr-123")
    assert "Post 1" in markdown
    assert "Post 2" in markdown
    assert "## Image (Drag & drop into Reddit)" in markdown
    assert "<!-- id:pr-123 -->" in markdown
    assert "---" in markdown


def test_clean_subreddit() -> None:
    assert clean_subreddit("r/codexcryptica") == "codexcryptica"
    assert clean_subreddit("/r/codexcryptica/") == "codexcryptica"
    assert clean_subreddit("r/another_sub/") == "another_sub"
    assert clean_subreddit("codexcryptica") == "codexcryptica"


def test_fetch_subreddit_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            payload = {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "post123",
                                "name": "t3_post123",
                                "title": "Dynamic Superhero Alignments",
                                "selftext": "Check out this system!\n\n<!-- id:pr-3093 -->",
                                "permalink": "/r/codexcryptica/comments/post123/dynamic_superhero_alignments/",
                                "url": "https://www.reddit.com/r/codexcryptica/comments/post123/dynamic_superhero_alignments/",
                                "created_utc": 1710000000.0,
                            }
                        }
                    ]
                }
            }
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=15.0: DummyResponse())

    posts = fetch_subreddit_posts("r/codexcryptica")
    assert len(posts) == 1
    p = posts[0]
    assert p["id"] == "post123"
    assert p["title"] == "Dynamic Superhero Alignments"
    assert p["source_id"] == "pr-3093"
    assert p["url"] == "https://www.reddit.com/r/codexcryptica/comments/post123/dynamic_superhero_alignments/"
    # A text post's own link is its permalink.
    assert p["link_url"] == p["url"]


def test_sync_reddit_status_reconciles_staged_publication(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_db = tmp_path / "state.db"

    project = ProjectConfig(
        repo=repo,
        github="owner/demo",
    )
    repository = StateRepository(state_db, project_name="demo")
    repository.claim_run("release-comms", "run-500")

    repository.record_publication(
        workflow="release-comms",
        run_id="run-500",
        channel="reddit",
        destination="reddit",
        page_url="https://codexcryptica.com/answers/dragons",
        public_url="dry-run://r2/announcements/reddit-candidates.json",
        external_id="staged:pr-500",
        status="staged",
        metadata={"status": "staged_to_r2", "source_id": "pr-500"},
    )

    initial_staged = repository.list_publications(
        workflow="release-comms", channel="reddit", status="staged"
    )
    assert len(initial_staged) == 1

    fetched_posts = [
        {
            "id": "t3_dragon_post",
            "title": "Dragon Ecology Guide",
            "selftext": "Full details at https://codexcryptica.com/answers/dragons\n\n<!-- id:pr-500 -->",
            "source_id": "pr-500",
            "permalink": "/r/codexcryptica/comments/dragon_post/dragon_ecology_guide/",
            "url": "https://www.reddit.com/r/codexcryptica/comments/dragon_post/dragon_ecology_guide/",
        }
    ]

    reconciled = sync_reddit_status(
        project=project,
        project_name="demo",
        run_id="run-500",
        repository=repository,
        fetched_posts=fetched_posts,
        notify_tracking_issue=False,
    )

    assert len(reconciled) == 1
    assert reconciled[0].status == "published"
    assert reconciled[0].external_id == "t3_dragon_post"
    assert reconciled[0].public_url == "https://www.reddit.com/r/codexcryptica/comments/dragon_post/dragon_ecology_guide/"
    assert reconciled[0].metadata["status"] == "published"
    assert "reconciled_at" in reconciled[0].metadata

    # SQLite now has zero staged publications
    remaining_staged = repository.list_publications(
        workflow="release-comms", channel="reddit", status="staged"
    )
    assert len(remaining_staged) == 0

    # Event was recorded
    events = repository.list_run_events("release-comms", "run-500")
    reconcile_events = [e for e in events if e.event == "reddit_reconciled"]
    assert len(reconcile_events) == 1
    assert reconcile_events[0].metadata["reddit_id"] == "t3_dragon_post"


def test_sync_reddit_status_reconciles_a_link_post_by_its_submitted_url(tmp_path: Path) -> None:
    """A link post has no id tag in its body (the write-up is a comment), so it is matched by
    the page it links to."""
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, github="owner/demo")
    repository = StateRepository(tmp_path / "state.db", project_name="demo")
    repository.claim_run("release-comms", "run-600")
    repository.record_publication(
        workflow="release-comms",
        run_id="run-600",
        channel="reddit",
        destination="reddit",
        page_url="https://codexcryptica.com/answers/chases",
        public_url="dry-run://github/owner/demo/release-manifests/x.json",
        external_id="staged:pr-600",
        status="staged",
        metadata={"status": "staged_to_github", "source_id": "pr-600"},
    )
    unrelated = {
        "id": "t3_other",
        "title": "Something else",
        "selftext": "",
        "source_id": "",
        "permalink": "/r/codexcryptica/comments/other/x/",
        "url": "https://www.reddit.com/r/codexcryptica/comments/other/x/",
        "link_url": "https://codexcryptica.com/answers/other",
    }
    link_post = {
        "id": "t3_chase",
        "title": "How do you run a chase?",
        "selftext": "",
        "source_id": "",
        "permalink": "/r/codexcryptica/comments/chase/how_do_you_run_a_chase/",
        "url": "https://www.reddit.com/r/codexcryptica/comments/chase/how_do_you_run_a_chase/",
        # Reddit may add or drop a trailing slash.
        "link_url": "https://codexcryptica.com/answers/chases/",
    }

    reconciled = sync_reddit_status(
        project=project,
        project_name="demo",
        run_id="run-600",
        repository=repository,
        fetched_posts=[unrelated, link_post],
        notify_tracking_issue=False,
        prune_manifest=False,
    )

    assert [item.external_id for item in reconciled] == ["t3_chase"]
    assert reconciled[0].status == "published"


def test_cli_sync_reddit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dev_agents.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    state_db = tmp_path / "state.db"

    config_path = tmp_path / "projects.yaml"
    config_path.write_text(
        f"projects:\n"
        f"  demo:\n"
        f"    repo: {repo}\n"
        f"    github: owner/demo\n"
        f"    release_comms:\n"
        f"      state_path: {state_db}\n"
    )

    state_repo = StateRepository(state_db, project_name="demo")
    state_repo.claim_run("release-comms", "run-99")
    state_repo.record_publication(
        workflow="release-comms",
        run_id="run-99",
        channel="reddit",
        destination="reddit",
        page_url="https://codexcryptica.com/answers/sorcery",
        public_url="dry-run://r2/announcements/reddit-candidates.json",
        external_id="staged:pr-99",
        status="staged",
        metadata={"status": "staged_to_r2", "source_id": "pr-99"},
    )

    def fake_fetch(sub: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "sorcery_thread",
                "title": "Sorcery Rules Update",
                "selftext": "New sorcery systems <!-- id:pr-99 -->",
                "source_id": "pr-99",
                "permalink": "/r/codexcryptica/comments/sorcery_thread/",
                "url": "https://www.reddit.com/r/codexcryptica/comments/sorcery_thread/",
            }
        ]

    monkeypatch.setattr("dev_agents.workflows.reddit.fetch_subreddit_posts", fake_fetch)

    exit_code = main(["release-comms", "sync-reddit", "demo", "--config", str(config_path)])
    assert exit_code == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["reconciledCount"] == 1
    assert data["reconciled"][0]["runId"] == "run-99"
    assert data["reconciled"][0]["publicUrl"] == "https://www.reddit.com/r/codexcryptica/comments/sorcery_thread/"
    assert data["reconciled"][0]["status"] == "published"


def test_prune_published_reddit_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    initial_manifest = {
        "updated_at": 1000,
        "candidates": [
            {"id": "post-1", "source_id": "src-1", "title": "Post 1"},
            {"id": "post-2", "source_id": "src-2", "title": "Post 2"},
            {"id": "post-3", "source_id": "src-3", "title": "Post 3"},
        ],
    }

    class DummyResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(initial_manifest).encode("utf-8")

    uploaded_manifest: dict[str, Any] = {}

    def fake_upload(*, repo: Path, path: Path, key: str, **kwargs: Any) -> str:
        nonlocal uploaded_manifest
        uploaded_manifest = json.loads(path.read_text(encoding="utf-8"))
        return f"https://assets.codexcryptica.com/{key}"

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=15.0: DummyResponse())
    monkeypatch.setattr("dev_agents.workflows.reddit._upload_r2_file", fake_upload)

    pruned = prune_published_reddit_candidates(
        repo=repo,
        published_source_ids={"src-2", "post-999"},
    )

    assert pruned == 1
    assert len(uploaded_manifest["candidates"]) == 2
    candidate_ids = [c["id"] for c in uploaded_manifest["candidates"]]
    assert candidate_ids == ["post-1", "post-3"]


def test_sync_reddit_status_triggers_manifest_pruning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_db = tmp_path / "state.db"

    project = ProjectConfig(repo=repo, github="owner/demo")
    repository = StateRepository(state_db, project_name="demo")
    repository.claim_run("release-comms", "run-prune")

    repository.record_publication(
        workflow="release-comms",
        run_id="run-prune",
        channel="reddit",
        destination="reddit",
        page_url="https://codexcryptica.com/answers/prune",
        public_url="dry-run://r2/announcements/reddit-candidates.json",
        external_id="staged:src-prune",
        status="staged",
        metadata={"status": "staged_to_r2", "source_id": "src-prune"},
    )

    pruned_calls: list[set[str]] = []

    def fake_prune(*, repo: Path, published_source_ids: set[str], **kwargs: Any) -> int:
        pruned_calls.append(published_source_ids)
        return len(published_source_ids)

    monkeypatch.setattr("dev_agents.workflows.reddit.prune_published_reddit_candidates", fake_prune)

    fetched_posts = [
        {
            "id": "t3_pruned_post",
            "title": "Pruned Post Title",
            "selftext": "Details at https://codexcryptica.com/answers/prune <!-- id:src-prune -->",
            "source_id": "src-prune",
            "permalink": "/r/codexcryptica/comments/pruned_post/",
            "url": "https://www.reddit.com/r/codexcryptica/comments/pruned_post/",
        }
    ]

    reconciled = sync_reddit_status(
        project=project,
        project_name="demo",
        run_id="run-prune",
        repository=repository,
        fetched_posts=fetched_posts,
        notify_tracking_issue=False,
        prune_manifest=True,
    )

    assert len(reconciled) == 1
    assert len(pruned_calls) == 1
    assert "src-prune" in pruned_calls[0]


def test_stage_reddit_candidate_github_dry_run(tmp_path: Path) -> None:
    candidate = {
        "id": "reddit-pr-200-gh",
        "title": "GitHub Post",
        "body": "Body text",
        "url": "https://codexcryptica.com/answers/gh",
        "source_id": "pr-200",
    }
    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate=candidate,
        env={},
        dry_run=True,
        github="eserlan/Codex-Cryptica",
        branch="release-manifests",
    )
    assert receipt.channel == "reddit"
    assert receipt.destination == "reddit"
    assert receipt.page_url == "https://codexcryptica.com/answers/gh"
    assert receipt.public_url == "dry-run://github/eserlan/Codex-Cryptica/release-manifests/announcements/reddit-candidates.json"
    assert receipt.external_id == "staged:pr-200"
    assert receipt.metadata is not None
    assert receipt.metadata["status"] == "staged_to_github"
    assert receipt.metadata["github"] == "eserlan/Codex-Cryptica"
    assert receipt.metadata["branch"] == "release-manifests"


def test_stage_reddit_candidate_github_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    candidate = {
        "id": "reddit-pr-200-live",
        "title": "Live GH Post",
        "body": "Body text",
        "url": "https://codexcryptica.com/answers/live-gh",
        "source_id": "pr-200",
    }

    uploaded: list[dict[str, Any]] = []

    def fake_upload_github_manifest(*, repo: Path, github: str, branch: str, path: str, manifest: Any, timeout: float) -> str:
        uploaded.append({
            "github": github,
            "branch": branch,
            "path": path,
            "manifest": manifest,
        })
        return f"https://raw.githubusercontent.com/{github}/{branch}/{path}"

    monkeypatch.setattr("dev_agents.workflows.reddit._upload_github_manifest", fake_upload_github_manifest)
    monkeypatch.setattr("dev_agents.workflows.reddit._fetch_manifest_from_url", lambda url, timeout=15.0: [])

    receipt = stage_reddit_candidate(
        repo=tmp_path,
        candidate=candidate,
        env={},
        dry_run=False,
        github="eserlan/Codex-Cryptica",
        branch="release-manifests",
    )

    assert receipt.channel == "reddit"
    assert receipt.public_url == "https://raw.githubusercontent.com/eserlan/Codex-Cryptica/release-manifests/announcements/reddit-candidates.json"
    assert receipt.external_id == "staged:pr-200"
    assert receipt.metadata["status"] == "staged_to_github"
    assert len(uploaded) == 1
    assert uploaded[0]["github"] == "eserlan/Codex-Cryptica"
    assert len(uploaded[0]["manifest"]["candidates"]) == 1
    assert uploaded[0]["manifest"]["candidates"][0]["id"] == "reddit-pr-200-live"


def test_prune_published_reddit_candidates_github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    existing = [
        {"id": "c-1", "source_id": "s-1"},
        {"id": "c-2", "source_id": "s-2"},
    ]
    uploaded_manifests: list[dict[str, Any]] = []

    def fake_fetch(url: str, timeout: float = 15.0) -> list[dict[str, Any]]:
        return list(existing)

    def fake_upload(*, repo: Path, github: str, branch: str, path: str, manifest: Any, timeout: float) -> str:
        uploaded_manifests.append(dict(manifest))
        return f"https://raw.githubusercontent.com/{github}/{branch}/{path}"

    monkeypatch.setattr("dev_agents.workflows.reddit._fetch_manifest_from_url", fake_fetch)
    monkeypatch.setattr("dev_agents.workflows.reddit._upload_github_manifest", fake_upload)

    pruned = prune_published_reddit_candidates(
        repo=tmp_path,
        published_source_ids={"s-1"},
        github="eserlan/Codex-Cryptica",
        branch="release-manifests",
    )

    assert pruned == 1
    assert len(uploaded_manifests) == 1
    remaining = uploaded_manifests[0]["candidates"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == "c-2"


def test_upload_github_manifest_invokes_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dev_agents.workflows.reddit import _upload_github_manifest

    gh_calls: list[tuple[str, ...]] = []

    def fake_gh(repo: Path, *args: str, **kwargs: Any) -> str:
        gh_calls.append(args)
        return ""

    def fake_gh_json(repo: Path, *args: str, **kwargs: Any) -> Any:
        gh_calls.append(args)
        if "contents" in args[1]:
            return {"sha": "existing_blob_sha"}
        return {}

    monkeypatch.setattr("dev_agents.runtime.gh", fake_gh)
    monkeypatch.setattr("dev_agents.runtime.gh_json", fake_gh_json)

    url = _upload_github_manifest(
        repo=tmp_path,
        github="owner/repo",
        branch="release-manifests",
        path="announcements/reddit-candidates.json",
        manifest={"updated_at": 123, "candidates": []},
    )

    assert url == "https://raw.githubusercontent.com/owner/repo/release-manifests/announcements/reddit-candidates.json"
    put_calls = [c for c in gh_calls if "PUT" in c]
    assert len(put_calls) == 1
    assert "sha=existing_blob_sha" in put_calls[0]


