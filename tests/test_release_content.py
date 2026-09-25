from __future__ import annotations

import subprocess
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import AgentResult
from dev_agents.workflows.release_comms import run_release_comms
from dev_agents.workflows.release_content import (
    PNG_MAGIC,
    EvaluatorResult,
    ImageStatus,
    ReleaseDelta,
    WriterResult,
    _sha_present,
    collect_release_delta,
    derive_discord_from_bluesky,
    ensure_shas_present,
    generate_announcement_image,
    is_png_image,
    load_asset_inventory,
    load_skill_prompt,
    parse_evaluator_result,
    parse_writer_result,
    prepare_bluesky_text,
    recommend_channels,
    render_template,
    run_evaluator_pass,
    run_longform_writer_pass,
    run_shortform_writer_pass,
    run_writer_pass,
    select_forms,
    sync_asset_db,
    verify_draft_images,
)


@pytest.mark.parametrize(
    ("channels", "features", "enabled", "expected"),
    [
        (["bluesky"], [], ("short", "long"), ["short"]),
        (["github_discussions"], [], ("short", "long"), ["long"]),
        (["bluesky", "github_discussions"], [], ("short", "long"), ["short", "long"]),
        ([], [], ("short", "long"), ["short", "long"]),
        (["instagram"], [], ("short", "long"), ["short", "long"]),
        ([], [{"name": "F", "bluesky_worthy": True}], ("short", "long"), ["short"]),
        (["bluesky"], [], ("long",), []),
        (["bluesky"], [], ("short",), ["short"]),
        (["github_discussions"], [], (), []),
    ],
)
def test_select_forms_routes_on_signal(
    channels: list[str],
    features: list[dict[str, object]],
    enabled: tuple[str, ...],
    expected: list[str],
) -> None:
    evaluator = EvaluatorResult(
        postworthy=True, reason="R", features=features, recommended_channels=channels
    )
    assert select_forms(evaluator, enabled=enabled) == expected


def test_select_forms_rejects_not_postworthy() -> None:
    evaluator = EvaluatorResult(postworthy=False, reason="R")
    assert select_forms(evaluator) == []


def test_skill_prompts_carry_json_contract() -> None:
    for skill_id in ("release-evaluate", "release-shortform", "release-longform"):
        body = load_skill_prompt(skill_id)
        assert "```json" in body
        assert "---" not in body.splitlines()[0]
    assert "{new_sha}" in load_skill_prompt("release-evaluate")
    assert "{reason}" in load_skill_prompt("release-shortform")
    assert "{reason}" in load_skill_prompt("release-longform")
    assert '"reddit"' not in load_skill_prompt("release-shortform")


def test_skill_frontmatter_stays_out_of_prompt() -> None:
    body = load_skill_prompt("release-shortform")
    assert body.startswith("# Shortform release writer")
    assert "name: release-shortform" not in body


def test_load_skill_prompt_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="invalid skill id"):
        load_skill_prompt("../escape")


def test_writer_prompts_require_pageurl_evidence() -> None:
    for skill_id in ("release-shortform", "release-longform"):
        body = load_skill_prompt(skill_id)
        assert "Never invent or guess" in body
        assert "leave `pageUrl` empty" in body


def test_evaluator_prompt_routes_answer_pages_to_longform() -> None:
    body = load_skill_prompt("release-evaluate")
    assert "one new page is enough" in body
    assert "`github_discussions`" in body
    assert "`reddit`" in body


def test_evaluator_prompt_keeps_technical_changes_out_of_public_announcements() -> None:
    """A Cloud Backup delta-sync/sharded-storage change was announced publicly because the
    prompt said sync work was ALWAYS postworthy."""
    body = load_skill_prompt("release-evaluate")

    assert "sync/import" not in body
    assert "Technical changes are NOT announcements" in body
    for technical in ("Delta sync", "sharding", "battery or CPU", "Cloud Backup"):
        assert technical in body
    assert "leave it out" in body.lower()


def test_render_template_leaves_unknown_braces() -> None:
    rendered = render_template("a {known} b {unknown} c", {"known": "X"})
    assert rendered == "a X b {unknown} c"


def test_parse_evaluator_result_validates_postworthy() -> None:
    full = parse_evaluator_result(
        {
            "postworthy": True,
            "reason": "Big launch",
            "importance": "high",
            "features": [{"name": "F", "why_users_care": "W"}],
            "recommended_channels": ["bluesky", 7],
        }
    )
    assert full is not None
    assert full.postworthy is True
    assert full.importance == "high"
    assert full.features == [{"name": "F", "why_users_care": "W"}]
    assert full.recommended_channels == ["bluesky"]

    assert parse_evaluator_result({"reason": "no flag"}) is None
    assert parse_evaluator_result({"postworthy": "yes"}) is None
    assert parse_evaluator_result(None) is None


def test_parse_writer_result_drops_empty_drafts() -> None:
    result = parse_writer_result(
        {
            "bluesky": [
                {"pageUrl": "https://example.com/a", "text": "Hello #launch"},
                {"pageUrl": "x", "text": "   "},
            ],
            "github_discussions": [{"title": "T", "body": "B"}],
        }
    )
    assert result is not None
    assert result.bluesky == [
        {"pageUrl": "https://example.com/a", "text": "Hello #launch", "image": ""}
    ]
    assert result.github_discussions == [{"title": "T", "body": "B"}]

    assert parse_writer_result(None) is None
    assert parse_writer_result([]) is None  # type: ignore[arg-type]


def test_prepare_bluesky_text_fits_limit() -> None:
    assert prepare_bluesky_text("Hello", "") == "Hello"
    assert prepare_bluesky_text("Hello", "https://example.com/a") == ("Hello https://example.com/a")
    assert prepare_bluesky_text("Hello https://example.com/a", "https://example.com/a") == (
        "Hello https://example.com/a"
    )

    long_text = "x" * 400
    fitted = prepare_bluesky_text(long_text, "https://example.com/a")
    assert len(fitted) <= 300
    assert fitted.endswith("… https://example.com/a")


def test_derive_discord_from_bluesky_strips_hashtags() -> None:
    assert derive_discord_from_bluesky(["Hello #launch world", ""]) == "Hello world"
    assert derive_discord_from_bluesky([]) == ""
    assert derive_discord_from_bluesky(["#only"]) == ""


def test_recommend_channels_needs_copy_and_opt_in() -> None:
    drafts = WriterResult(
        bluesky=[{"pageUrl": "", "text": "Hi"}],
        github_discussions=[{"title": "T", "body": "B"}],
        discord="Hi",
    )
    assert recommend_channels(drafts) == ["bluesky", "discord", "github_discussions"]
    assert recommend_channels(drafts, enabled={"bluesky"}) == ["bluesky"]
    assert recommend_channels(WriterResult()) == []


def test_recommend_channels_derives_discord_from_bluesky() -> None:
    assert recommend_channels(
        WriterResult(bluesky=[{"pageUrl": "https://example.com/a", "text": "Hi"}])
    ) == ["bluesky", "discord"]


def test_collect_release_delta_reads_git_history(git_repository: Path) -> None:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    delta = collect_release_delta(git_repository, sha, None)
    assert delta.new_sha == sha
    assert delta.previous_sha is None
    assert any("initial" in line for line in delta.commits)
    assert "README.md" in delta.files_changed

    empty = collect_release_delta(git_repository, sha, sha)
    assert empty.commits == []
    assert empty.files_changed == []


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{message}\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_collect_release_delta_fetches_sha_missing_from_clone(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=origin, check=True, capture_output=True)
    first_sha = _commit(origin, "one.txt", "first")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)], check=True, capture_output=True
    )

    second_sha = _commit(origin, "two.txt", "second")
    assert not _sha_present(clone, second_sha)

    ensure_shas_present(clone, second_sha, first_sha)
    assert _sha_present(clone, second_sha)

    delta = collect_release_delta(clone, second_sha, first_sha)
    assert any("second" in line for line in delta.commits)
    assert "two.txt" in delta.files_changed


def _agent_success(payload: str, seen: dict[str, str]):  # type: ignore[no-untyped-def]
    def fake(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        seen["prompt"] = prompt
        Path(str(kwargs["log_path"])).write_text(payload, encoding="utf-8")
        return AgentResult(returncode=0, timed_out=False)

    return fake


def test_run_evaluator_pass_parses_agent_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = 'notes\n```json\n{"postworthy": true, "reason": "R", "importance": "high"}\n```'
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "dev_agents.workflows.release_content.run_agent", _agent_success(payload, seen)
    )
    delta = ReleaseDelta("new", "prev", ["abc first"], ["a.py"], "1 file")
    result = run_evaluator_pass(
        repo=tmp_path,
        delta=delta,
        providers=["muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    )
    assert result.postworthy is True
    assert result.reason == "R"
    assert "abc first" in seen["prompt"]


def test_run_evaluator_pass_raises_when_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(returncode=1, timed_out=False)

    monkeypatch.setattr("dev_agents.workflows.release_content.run_agent", failing)
    delta = ReleaseDelta("new", "prev", [], [], "")
    with pytest.raises(RuntimeError, match="eval pass failed"):
        run_evaluator_pass(
            repo=tmp_path,
            delta=delta,
            providers=["muse"],
            log_dir=tmp_path,
            run_id="7",
            timeout_seconds=60,
        )


def test_run_writer_pass_does_not_request_a_discord_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = (
        '```json\n{"bluesky": [{"pageUrl": "", "text": "Ship it #launch"}], '
        '"github_discussions": []}\n```'
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_content.run_agent", _agent_success(payload, {})
    )
    evaluator = EvaluatorResult(postworthy=True, reason="R", features=[])
    delta = ReleaseDelta("new", "prev", [], [], "")
    result = run_writer_pass(
        repo=tmp_path,
        evaluator=evaluator,
        delta=delta,
        providers=["muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    )
    assert result.bluesky == [{"pageUrl": "", "text": "Ship it #launch", "image": ""}]
    assert result.discord is None


def test_short_and_long_passes_merge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        kind = str(kwargs["log_path"])
        if "writer-short" in kind:
            payload = '```json\n{"bluesky": [{"pageUrl": "", "text": "Short #x"}]}\n```'
        else:
            payload = '```json\n{"github_discussions": [{"title": "T", "body": "B"}]}\n```'
        Path(str(kwargs["log_path"])).write_text(payload, encoding="utf-8")
        return AgentResult(returncode=0, timed_out=False)

    monkeypatch.setattr("dev_agents.workflows.release_content.run_agent", fake)
    evaluator = EvaluatorResult(postworthy=True, reason="R", features=[])
    delta = ReleaseDelta("new", "prev", [], [], "")
    result = run_writer_pass(
        repo=tmp_path,
        evaluator=evaluator,
        delta=delta,
        providers=["muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    )
    assert result.bluesky == [{"pageUrl": "", "text": "Short #x", "image": ""}]
    assert result.github_discussions == [{"title": "T", "body": "B"}]
    assert result.discord is None
    assert run_shortform_writer_pass(
        repo=tmp_path,
        evaluator=evaluator,
        delta=delta,
        providers=["muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    ) == [{"pageUrl": "", "text": "Short #x", "image": ""}]


def test_longform_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        if "writer-long" in str(kwargs["log_path"]):
            return AgentResult(returncode=1, timed_out=False)
        Path(str(kwargs["log_path"])).write_text(
            '```json\n{"bluesky": [{"pageUrl": "", "text": "Hi"}]}\n```',
            encoding="utf-8",
        )
        return AgentResult(returncode=0, timed_out=False)

    monkeypatch.setattr("dev_agents.workflows.release_content.run_agent", fake)
    evaluator = EvaluatorResult(postworthy=True, reason="R", features=[])
    delta = ReleaseDelta("new", "prev", [], [], "")
    with pytest.raises(RuntimeError, match="writer-long pass failed"):
        run_longform_writer_pass(
            repo=tmp_path,
            evaluator=evaluator,
            delta=delta,
            providers=["muse"],
            log_dir=tmp_path,
            run_id="7",
            timeout_seconds=60,
        )


def test_branch_runs_only_signaled_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(local_generation=True),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.collect_release_delta",
        lambda r, new, prev: ReleaseDelta(new, prev, [], [], ""),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_evaluator_pass",
        lambda **kwargs: EvaluatorResult(
            postworthy=True,
            reason="Real",
            features=[],
            recommended_channels=["bluesky"],
        ),
    )
    long_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_shortform_writer_pass",
        lambda **kwargs: [
            {"pageUrl": "", "text": "Hi #launch", "image": "announcements/hi-v1.png"}
        ],
    )

    def fail_if_long(**kwargs: Any) -> Any:
        long_calls.append(kwargs)
        raise AssertionError("long form must not run for bluesky-only signal")

    monkeypatch.setattr("dev_agents.workflows.release_comms.run_longform_writer_pass", fail_if_long)
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.verify_draft_images",
        lambda drafts: [
            ImageStatus("page", "announcements/hi-v1.png", "ok", "10 KB", "image/png", "http 200")
        ],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.sync_asset_db",
        lambda repo, statuses, purpose: ["announcements/hi-v1.png"],
    )
    events: list[tuple[str, dict[str, Any]]] = []
    result = run_release_comms(
        project, "demo", "123", dry_run=True, on_event=lambda e, p: events.append((e, p))
    )

    assert result.completed is True
    assert result.postworthy is True
    assert long_calls == []
    assert result.drafts is not None
    assert result.drafts["bluesky"] == [
        {"pageUrl": "", "text": "Hi #launch", "image": "announcements/hi-v1.png"}
    ]
    assert result.drafts["github_discussions"] == []
    assert result.drafts["discord"] is None
    assert result.published["bluesky"] == []
    assert dict(events)["routed"] == {"forms": "short"}
    assert dict(events)["images"] == {
        "ok": 1,
        "missing": "",
        "capture_requested": "",
        "db_updated": "announcements/hi-v1.png",
    }
    assert "generated" not in dict(events)


def test_merge_generates_art_for_missing_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(local_generation=True, image_generation=True),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.collect_release_delta",
        lambda r, new, prev: ReleaseDelta(new, prev, [], [], ""),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_evaluator_pass",
        lambda **kwargs: EvaluatorResult(postworthy=True, reason="Real", features=[]),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_shortform_writer_pass",
        lambda **kwargs: [{"pageUrl": "p", "text": "Hi", "image": "capture:/generators"}],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_longform_writer_pass",
        lambda **kwargs: [{"pageUrl": "p", "title": "Long", "body": "Details"}],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.verify_draft_images",
        lambda drafts: [
            ImageStatus("p", "capture:/generators", "capture-requested"),
            ImageStatus("p", "capture:/generators", "capture-requested"),
        ],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.sync_asset_db",
        lambda repo, statuses, purpose: [],
    )
    made: list[str] = []
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.generate_announcement_image",
        lambda **kwargs: made.append(str(kwargs["dest"])) or kwargs["dest"],
    )
    events: list[tuple[str, dict[str, Any]]] = []
    result = run_release_comms(
        project, "demo", "123", dry_run=True, on_event=lambda e, p: events.append((e, p))
    )

    assert result.completed is True
    assert len(made) == 1
    assert made[0].endswith("gen-123-0.png")
    images = dict(events)["images"]
    assert images["capture_requested"] == "capture:/generators,capture:/generators"
    assert "generated" not in images
    assert dict(events)["generated"] == {"files": made[0], "uploaded": "", "errors": ""}
    assert [event for event, _ in events] == [
        "resolved",
        "evaluated",
        "routed",
        "drafted",
        "images",
        "generated",
        "published",
    ]


def test_generate_art_skipped_when_nothing_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        repo=repo,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(local_generation=True, image_generation=True),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.resolve_promote_shas",
        lambda r, run_id: ("newsha", "prevsha"),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.collect_release_delta",
        lambda r, new, prev: ReleaseDelta(new, prev, [], [], ""),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_evaluator_pass",
        lambda **kwargs: EvaluatorResult(postworthy=True, reason="Real", features=[]),
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_shortform_writer_pass",
        lambda **kwargs: [{"pageUrl": "p", "text": "Hi", "image": "a/b.png"}],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.run_longform_writer_pass",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.verify_draft_images",
        lambda drafts: [ImageStatus("p", "a/b.png", "ok")],
    )
    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.sync_asset_db",
        lambda repo, statuses, purpose: [],
    )

    def fail_if_generate(**kwargs: Any) -> Any:
        raise AssertionError("generate must not run when every asset resolves")

    monkeypatch.setattr(
        "dev_agents.workflows.release_comms.generate_announcement_image", fail_if_generate
    )
    events: list[tuple[str, dict[str, Any]]] = []
    result = run_release_comms(
        project, "demo", "123", dry_run=True, on_event=lambda e, p: events.append((e, p))
    )

    assert result.completed is True
    assert "generated" not in dict(events)
    assert dict(events)["images"]["ok"] == 1


def test_writer_result_has_no_reddit_contract() -> None:
    assert "reddit" not in WriterResult.__dataclass_fields__


def test_verify_draft_images_classifies_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers(dict):
        def get(self, key: str, default: str = "") -> str:
            return super().get(key, default)

    class Response:
        status = 200
        headers = Headers({"Content-Length": "2048", "Content-Type": "image/png"})

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float = 0) -> Response:
        url = str(getattr(request, "full_url", ""))
        if "good" in url:
            return Response()
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    statuses = verify_draft_images(
        [
            {"pageUrl": "p", "text": "t", "image": "announcements/good-v1.png"},
            {"pageUrl": "p", "text": "t", "image": "announcements/gone-v1.png"},
            {"pageUrl": "p", "text": "t", "image": "capture:/generators/x"},
            {"pageUrl": "p", "text": "t", "image": ""},
        ]
    )
    assert [(item.image, item.status) for item in statuses] == [
        ("announcements/good-v1.png", "ok"),
        ("announcements/gone-v1.png", "missing"),
        ("capture:/generators/x", "capture-requested"),
        ("", "unspecified"),
    ]
    assert statuses[0].size == "2 KB"
    assert statuses[0].mime == "image/png"


def test_sync_asset_db_appends_missing_rows(tmp_path: Path) -> None:
    db = tmp_path / "r2-asset-db.md"
    db.write_text(
        "# R2 Asset Database\n\n## `announcements/` — launch images\n\n"
        "| Key | Size | Type | Modified | Purpose |\n"
        "| --- | ---- | ---- | -------- | ------- |\n"
        "| [`announcements/old-v1.png`](https://cdn.example/announcements/old-v1.png) "
        "| 1 KB | image/png | 2026-09-01 | Old launch |\n"
        "\n## `blog/` — other\n",
        encoding="utf-8",
    )
    statuses = [
        ImageStatus("p", "announcements/old-v1.png", "ok", "1 KB", "image/png", "http 200"),
        ImageStatus("p", "announcements/new-v1.png", "ok", "2 KB", "image/png", "http 200"),
        ImageStatus("p", "announcements/gone-v1.png", "missing"),
    ]
    added = sync_asset_db(
        tmp_path,
        statuses,
        "release abc1234",
        db_relative_path=Path("r2-asset-db.md"),
        base_url="https://cdn.example",
        today="2026-09-12",
    )
    assert added == ["announcements/new-v1.png"]
    text = db.read_text(encoding="utf-8")
    assert (
        "| [`announcements/new-v1.png`](https://cdn.example/announcements/new-v1.png) "
        "| 2 KB | image/png | 2026-09-12 | release abc1234 |" in text
    )
    assert text.find("new-v1") < text.find("## `blog/`")


def test_sync_asset_db_missing_file_returns_empty(tmp_path: Path) -> None:
    statuses = [ImageStatus("p", "announcements/new-v1.png", "ok", "2 KB", "image/png")]
    assert sync_asset_db(tmp_path, statuses, "release x") == []


def test_load_asset_inventory_lists_db_keys(tmp_path: Path) -> None:
    db_dir = tmp_path / "docs" / "deployment"
    db_dir.mkdir(parents=True)
    (db_dir / "r2-asset-db.md").write_text(
        "## `announcements/`\n\n"
        "| [`announcements/b-v1.png`](https://cdn.example/announcements/b-v1.png) | 1 KB |\n"
        "| [`announcements/a-v1.png`](https://cdn.example/announcements/a-v1.png) | 1 KB |\n"
        "| [`published/x/y.png`](https://cdn.example/published/x/y.png) | 1 KB |\n",
        encoding="utf-8",
    )
    assert load_asset_inventory(tmp_path) == [
        "announcements/a-v1.png",
        "announcements/b-v1.png",
    ]
    assert load_asset_inventory(tmp_path / "missing") == []


def test_shortform_prompt_includes_known_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_dir = tmp_path / "docs" / "deployment"
    db_dir.mkdir(parents=True)
    (db_dir / "r2-asset-db.md").write_text("`announcements/hero-v1.png`", encoding="utf-8")
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "dev_agents.workflows.release_content.run_agent",
        _agent_success('```json\n{"bluesky": []}\n```', seen),
    )
    run_shortform_writer_pass(
        repo=tmp_path,
        evaluator=EvaluatorResult(postworthy=True, reason="R", features=[]),
        delta=ReleaseDelta("new", "prev", [], [], ""),
        providers=["muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    )
    assert "announcements/hero-v1.png" in seen["prompt"]


def test_is_png_image_checks_magic_bytes(tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    good.write_bytes(PNG_MAGIC + b"\x00" * 10)
    assert is_png_image(good) is True
    text = tmp_path / "note.txt"
    text.write_text("DONE", encoding="utf-8")
    assert is_png_image(text) is False
    assert is_png_image(tmp_path / "absent.png") is False


def test_generate_announcement_image_falls_back_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        calls.append(provider)
        assert str(kwargs["log_path"]).endswith(f"local-image-7-{len(calls) - 1}.log")
        assert "art.png" in prompt
        if provider == "codex":
            Path(str(dest)).write_bytes(PNG_MAGIC + b"\x00")
        return AgentResult(returncode=0, timed_out=False)

    dest = tmp_path / "art.png"
    monkeypatch.setattr("dev_agents.workflows.release_content.run_agent", fake)
    result = generate_announcement_image(
        repo=tmp_path,
        subject="dice",
        dest=dest,
        providers=["agy", "codex", "muse"],
        log_dir=tmp_path,
        run_id="7",
        timeout_seconds=60,
    )
    assert result == dest
    assert calls == ["agy", "codex"]


def test_generate_announcement_image_returns_none_when_all_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake(provider: str, prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(returncode=0, timed_out=False)

    monkeypatch.setattr("dev_agents.workflows.release_content.run_agent", fake)
    dest = tmp_path / "art.png"
    assert (
        generate_announcement_image(
            repo=tmp_path,
            subject="dice",
            dest=dest,
            providers=["agy"],
            log_dir=tmp_path,
            run_id="7",
            timeout_seconds=60,
        )
        is None
    )
    assert not dest.exists()
