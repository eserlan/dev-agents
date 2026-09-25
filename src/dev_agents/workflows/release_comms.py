"""Workflow for evaluating release changes, drafting comms, and publishing."""

from __future__ import annotations

import json
import os
import random
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from dev_agents.config import ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import DuplicateRunError, StateRepository, state_database_path
from dev_agents.workflows.release_content import (
    EvaluatorResult,
    ImageStatus,
    ReleaseDelta,
    WriterResult,
    collect_release_delta,
    extract_json_block,
    generate_announcement_image,
    parse_evaluator_result,
    parse_writer_result,
    recommend_channels,
    run_evaluator_pass,
    run_longform_writer_pass,
    run_shortform_writer_pass,
    select_forms,
    sync_asset_db,
    verify_draft_images,
)
from dev_agents.workflows.release_history import (
    Announcement,
    earlier_release_shas,
    format_recent_posts,
    narrow_previous_sha,
    recent_announcements,
    repeated_publication_keys,
)
from dev_agents.workflows.release_publish import (
    PublicationError,
    PublicationReceipt,
    comment_tracking_issue,
    pending_publication_count,
    publication_key,
    publish_internal_note,
    publish_release_drafts,
    upload_release_image,
)

__all__ = [
    "EvaluatorResult",
    "ReleaseCommsRunResult",
    "WriterResult",
    "extract_json_block",
]


@dataclass(frozen=True)
class ReleaseFeature:
    name: str
    why_users_care: str
    bluesky_worthy: bool = False


@dataclass(frozen=True)
class ReleaseCommsRunResult:
    promote_run_id: str
    new_sha: str
    previous_sha: str | None
    postworthy: bool
    drafts: dict[str, Any] | None
    published: dict[str, Any]
    completed: bool
    scheduled: bool = False


ProgressEvent = Callable[[str, dict[str, Any]], None]


class ReleaseCommsState(TypedDict, total=False):
    repo: Path
    project: ProjectConfig
    project_name: str
    promote_run_id: str
    config: ReleaseCommsConfig
    new_sha: str
    previous_sha: str | None
    evaluator_result: EvaluatorResult | None
    writer_result: WriterResult | None
    forms: list[str]
    short_drafts: list[dict[str, str]]
    long_drafts: list[dict[str, str]]
    image_statuses: list[ImageStatus]
    image_overrides: dict[str, str]
    publications: dict[str, Any]
    existing_publications: list[Any]
    recent_announcements: list[Announcement]
    handled_shas: list[str]
    publication_sink: Callable[[PublicationReceipt], None]
    dry_run: bool
    publish_approved: bool
    on_event: ProgressEvent | None
    result: ReleaseCommsRunResult
    scheduled: bool
    next_publication_at: str | None
    remaining_publications: int


def _emit(state: ReleaseCommsState, event: str, payload: dict[str, Any]) -> None:
    """Report a workflow phase; observability only, never affects the result."""
    callback = state.get("on_event")
    if callback is not None:
        callback(event, payload)


READONLY_TIMEOUT_SECONDS = 60.0


def _capture(repo: Path, args: list[str], timeout: float) -> str:
    """Run a read-only command, returning stdout or "" on failure/timeout."""
    try:
        proc = subprocess.run(
            args, cwd=repo, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def resolve_promote_shas(
    repo: Path, promote_run_id: str, timeout_seconds: float = READONLY_TIMEOUT_SECONDS
) -> tuple[str, str | None]:
    """Resolve production new SHA and previous successful promote SHA."""
    new_sha = _capture(
        repo,
        ["gh", "run", "view", str(promote_run_id), "--json", "headSha", "--jq", ".headSha"],
        timeout_seconds,
    )
    if not new_sha:
        new_sha = _capture(repo, ["git", "rev-parse", "HEAD"], timeout_seconds)
    if not new_sha:
        raise RuntimeError(f"could not resolve promote SHA for run {promote_run_id}")

    runs_raw = _capture(
        repo,
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "Promote Staging to Production",
            "--status",
            "success",
            "--limit",
            "5",
            "--json",
            "databaseId,headSha",
        ],
        timeout_seconds,
    )
    previous_sha: str | None = None
    try:
        runs = json.loads(runs_raw)
        for r in runs:
            if str(r.get("databaseId")) != str(promote_run_id):
                previous_sha = r.get("headSha")
                break
    except (json.JSONDecodeError, TypeError):
        pass

    if not previous_sha:
        previous_sha = _capture(repo, ["git", "rev-parse", "HEAD~1"], timeout_seconds) or None

    return new_sha, previous_sha


def evaluate_release(
    repo: Path,
    new_sha: str,
    previous_sha: str | None,
    providers: list[str],
    log_dir: Path,
    promote_run_id: str,
    timeout_seconds: float = 600,
    recent_posts: str = "(none)",
) -> EvaluatorResult:
    """Run the daemon-owned evaluator, including when local generation is enabled."""
    delta = collect_release_delta(repo, new_sha, previous_sha, timeout_seconds)
    return run_evaluator_pass(
        repo=repo,
        delta=delta,
        providers=providers,
        log_dir=log_dir,
        run_id=promote_run_id,
        timeout_seconds=timeout_seconds,
        recent_posts=recent_posts,
    )

def _draft_counts(drafts: WriterResult) -> dict[str, Any]:
    """Summarize generated copy without dumping full text into logs."""
    return {
        "bluesky": len(drafts.bluesky),
        "github_discussions": len(drafts.github_discussions),
        "discord": bool(drafts.bluesky) or bool(drafts.discord and drafts.discord.strip()),
    }


def build_release_comms_workflow() -> Any:
    """Build the LangGraph release communications workflow."""
    graph = StateGraph(ReleaseCommsState)

    def resolve_context(state: ReleaseCommsState) -> dict[str, Any]:
        # Pinned by a prior attempt (retry/resume) takes priority: re-deriving the
        # "previous successful promote" from a live `gh run list` on every retry
        # means later promotions/reverts on main shift what "previous" means, so a
        # release can flip from postworthy to a false "net revert" the longer a
        # retry is delayed -- pin it once, on first resolution, instead.
        if state.get("new_sha"):
            _emit(
                state,
                "resolved",
                {"new_sha": state["new_sha"], "previous_sha": state.get("previous_sha"), "pinned": True},
            )
            return {}
        repo = state["repo"]
        promote_run_id = state["promote_run_id"]
        new_sha, previous_sha = resolve_promote_shas(repo, promote_run_id)
        promote_previous = previous_sha
        # Do not re-evaluate commits an earlier release already announced (see
        # narrow_previous_sha); pinned below like any other first resolution.
        previous_sha = narrow_previous_sha(
            repo, previous_sha, new_sha, state.get("handled_shas", [])
        )
        payload: dict[str, Any] = {"new_sha": new_sha, "previous_sha": previous_sha}
        if previous_sha != promote_previous:
            payload["promote_previous_sha"] = promote_previous
        _emit(state, "resolved", payload)
        return {
            "new_sha": new_sha,
            "previous_sha": previous_sha,
        }

    def local_log_dir(state: ReleaseCommsState) -> Path:
        return state["config"].log_dir or state["repo"] / ".dev-agents/release-comms"

    def local_timeout(state: ReleaseCommsState) -> float:
        return state["config"].timeout_minutes * 60

    def local_delta(state: ReleaseCommsState) -> ReleaseDelta:
        return collect_release_delta(state["repo"], state["new_sha"], state.get("previous_sha"))

    def recent_posts(state: ReleaseCommsState) -> str:
        return format_recent_posts(state.get("recent_announcements", []))

    def evaluate_changes(state: ReleaseCommsState) -> dict[str, Any]:
        evaluator_result = state.get("evaluator_result")
        if evaluator_result is None:
            if state["config"].local_generation:
                evaluator_result = run_evaluator_pass(
                    repo=state["repo"],
                    delta=local_delta(state),
                    providers=state["config"].providers,
                    log_dir=local_log_dir(state),
                    run_id=state["promote_run_id"],
                    timeout_seconds=local_timeout(state),
                    recent_posts=recent_posts(state),
                )
            else:
                evaluator_result = evaluate_release(
                    state["repo"],
                    state["new_sha"],
                    state.get("previous_sha"),
                    state["config"].providers,
                    local_log_dir(state),
                    state["promote_run_id"],
                    timeout_seconds=local_timeout(state),
                    recent_posts=recent_posts(state),
                )
        _emit(
            state,
            "evaluated",
            {
                "postworthy": evaluator_result.postworthy,
                "importance": evaluator_result.importance,
                "reason": evaluator_result.reason,
                "features": len(evaluator_result.features),
                "channels": ",".join(evaluator_result.recommended_channels),
            },
        )
        return {"evaluator_result": evaluator_result}

    def route_forms(state: ReleaseCommsState) -> dict[str, Any]:
        eval_res = state.get("evaluator_result")
        if (
            not eval_res
            or not eval_res.postworthy
            or state.get("writer_result") is not None
        ):
            forms: list[str] = []
        else:
            forms = select_forms(eval_res, enabled=state["config"].forms)
        _emit(state, "routed", {"forms": ",".join(forms)})
        return {"forms": forms}

    def pick_forms(state: ReleaseCommsState) -> str | list[str]:
        forms = state.get("forms") or []
        if not forms:
            return "merge_drafts"
        return [f"write_{form}" for form in forms]

    def write_short(state: ReleaseCommsState) -> dict[str, Any]:
        eval_res = state.get("evaluator_result")
        assert eval_res is not None
        drafts = run_shortform_writer_pass(
            repo=state["repo"],
            evaluator=eval_res,
            delta=local_delta(state),
            providers=state["config"].providers,
            log_dir=local_log_dir(state),
            run_id=state["promote_run_id"],
            timeout_seconds=local_timeout(state),
            recent_posts=recent_posts(state),
        )
        return {"short_drafts": drafts}

    def write_long(state: ReleaseCommsState) -> dict[str, Any]:
        eval_res = state.get("evaluator_result")
        assert eval_res is not None
        drafts = run_longform_writer_pass(
            repo=state["repo"],
            evaluator=eval_res,
            delta=local_delta(state),
            providers=state["config"].providers,
            log_dir=local_log_dir(state),
            run_id=state["promote_run_id"],
            timeout_seconds=local_timeout(state),
            recent_posts=recent_posts(state),
        )
        return {"long_drafts": drafts}

    def generate_missing_art(
        state: ReleaseCommsState,
        drafts: list[dict[str, str]],
        statuses: list[ImageStatus],
    ) -> list[tuple[str, str, Path]]:
        """Generate one image per page for drafts with no resolving asset."""
        art_dir = local_log_dir(state) / "images"
        art_dir.mkdir(parents=True, exist_ok=True)
        stem = "".join(
            char if char.isalnum() or char in "-_" else "-" for char in state["promote_run_id"]
        )
        generated: list[tuple[str, str, Path]] = []
        seen_pages: set[str] = set()
        for index, (draft, status) in enumerate(zip(drafts, statuses)):
            if status.status not in ("missing", "capture-requested", "unspecified"):
                continue
            page_url = draft.get("pageUrl", "").strip()
            dedup_key = page_url or f"draft:{index}"
            if dedup_key in seen_pages:
                continue
            seen_pages.add(dedup_key)
            made = generate_announcement_image(
                repo=state["repo"],
                subject=f"{draft.get('pageUrl', '')}\n{draft.get('text') or draft.get('body', '')}",
                dest=art_dir / f"gen-{stem}-{index}.png",
                providers=state["config"].image_providers,
                log_dir=local_log_dir(state),
                run_id=state["promote_run_id"],
                timeout_seconds=local_timeout(state),
            )
            if made is not None:
                generated.append((dedup_key, page_url, made))
        return generated

    def align_discussion_images(writer_res: WriterResult) -> WriterResult:
        """Give Discussions the same page/image source as their short drafts."""
        bluesky = [dict(item) for item in writer_res.bluesky]
        discussions = [dict(item) for item in writer_res.github_discussions]
        for index, discussion in enumerate(discussions):
            source = bluesky[index] if index < len(bluesky) else {}
            if not discussion.get("pageUrl") and source.get("pageUrl"):
                discussion["pageUrl"] = source["pageUrl"]
            if not discussion.get("image") and source.get("image"):
                discussion["image"] = source["image"]
        return WriterResult(
            bluesky=bluesky,
            github_discussions=discussions,
        )

    def with_derived_channels(
        evaluation: EvaluatorResult, writer_res: WriterResult
    ) -> EvaluatorResult:
        channels = list(evaluation.recommended_channels)
        if writer_res.bluesky and "discord" not in channels:
            channels.append("discord")
        return EvaluatorResult(
            postworthy=evaluation.postworthy,
            reason=evaluation.reason,
            importance=evaluation.importance,
            features=evaluation.features,
            recommended_channels=channels,
            internal_note=evaluation.internal_note,
        )

    def merge_drafts(state: ReleaseCommsState) -> dict[str, Any]:
        eval_res = state.get("evaluator_result")
        if not eval_res or not eval_res.postworthy:
            _emit(state, "drafts_skipped", {"reason": "not-postworthy"})
            return {"writer_result": None}
        injected = state.get("writer_result")
        if injected is not None:
            injected = align_discussion_images(injected)
            eval_res = with_derived_channels(eval_res, injected)
            _emit(state, "drafted", _draft_counts(injected))
            return {"writer_result": injected, "evaluator_result": eval_res}
        bluesky = state.get("short_drafts") or []
        discussions = state.get("long_drafts") or []
        writer_res = WriterResult(
            bluesky=bluesky,
            github_discussions=discussions,
        )
        writer_res = align_discussion_images(writer_res)
        enabled_destinations = state["config"].destinations
        recommended_channels = recommend_channels(writer_res, enabled=enabled_destinations)
        if writer_res.bluesky:
            for extra in ("instagram", "x", "pinterest"):
                if extra in enabled_destinations and extra not in recommended_channels:
                    recommended_channels.append(extra)
        updated = EvaluatorResult(
            postworthy=eval_res.postworthy,
            reason=eval_res.reason,
            importance=eval_res.importance,
            features=eval_res.features,
            recommended_channels=recommended_channels,
            internal_note=eval_res.internal_note,
        )
        _emit(state, "drafted", _draft_counts(writer_res))
        image_drafts = [*writer_res.bluesky, *writer_res.github_discussions]
        statuses = verify_draft_images(image_drafts)
        added = sync_asset_db(state["repo"], statuses, f"release {state['new_sha'][:7]}")
        _emit(
            state,
            "images",
            {
                "ok": sum(1 for item in statuses if item.status == "ok"),
                "missing": ",".join(item.image for item in statuses if item.status == "missing"),
                "capture_requested": ",".join(
                    item.image for item in statuses if item.status == "capture-requested"
                ),
                "db_updated": ",".join(added),
            },
        )
        return {
            "writer_result": writer_res,
            "evaluator_result": updated,
            "image_statuses": statuses,
        }

    def pick_art(state: ReleaseCommsState) -> str:
        if not state["config"].image_generation:
            return "publish_destinations"
        if any(
            item.status in ("missing", "capture-requested", "unspecified")
            for item in state.get("image_statuses") or []
        ):
            return "generate_art"
        return "publish_destinations"

    def generate_art(state: ReleaseCommsState) -> dict[str, Any]:
        writer_res = state.get("writer_result")
        assert writer_res is not None
        image_drafts = [*writer_res.bluesky, *writer_res.github_discussions]
        generated = generate_missing_art(
            state, image_drafts, state.get("image_statuses") or []
        )
        overrides: dict[str, str] = {}
        failures: list[str] = []
        stem = "".join(
            char if char.isalnum() or char in "-_" else "-" for char in state["promote_run_id"]
        )
        for index, (_, page_url, path) in enumerate(generated):
            if state.get("dry_run", True):
                continue
            if not page_url:
                failures.append(f"{path}: generated image has no page URL")
                continue
            try:
                overrides[page_url] = upload_release_image(
                    repo=state["repo"],
                    path=path,
                    key=f"announcements/release-{stem}-{index}.png",
                    env=os.environ,
                    timeout=local_timeout(state),
                )
            except Exception as error:  # noqa: BLE001 - report all generated-art failures
                failures.append(f"{path}: {error}")
        _emit(
            state,
            "generated",
            {
                "files": ",".join(str(path) for _, _, path in generated),
                "uploaded": ",".join(overrides.values()),
                "errors": ",".join(failures),
            },
        )
        if failures:
            raise RuntimeError("generated art could not be published: " + "; ".join(failures))
        updated_bluesky = [dict(item) for item in writer_res.bluesky]
        updated_discussions = [dict(item) for item in writer_res.github_discussions]
        for draft in updated_bluesky:
            image_url = overrides.get(draft.get("pageUrl", ""))
            if image_url:
                draft["image"] = image_url
        for index, discussion in enumerate(updated_discussions):
            page_url = discussion.get("pageUrl", "")
            if not page_url and index < len(updated_bluesky):
                page_url = updated_bluesky[index].get("pageUrl", "")
                if page_url:
                    discussion["pageUrl"] = page_url
            image_url = overrides.get(page_url, "")
            if image_url:
                discussion["image"] = image_url
        return {
            "writer_result": WriterResult(
                bluesky=updated_bluesky,
                github_discussions=updated_discussions,
            ),
            "image_overrides": overrides,
        }

    def publish_destinations(state: ReleaseCommsState) -> dict[str, Any]:
        dry_run = state.get("dry_run", True)
        publish_approved = state.get("publish_approved", False)
        publications: dict[str, Any] = state.get("publications") or {
            "bluesky": [],
            "discord": [],
            "instagram": [],
            "x": [],
            "githubDiscussions": [],
        }

        # Publishing must be explicitly approved and not dry-run
        writer_res = state.get("writer_result")
        drafts_dict = asdict(writer_res) if writer_res is not None else None
        postworthy = bool(state["evaluator_result"] and state["evaluator_result"].postworthy)
        # Backstop for the evaluator's repeat rule: refuse channels that already carried a
        # page with the same slug in an earlier run, even if the URL prefix has since changed.
        draft_urls = [
            str(draft.get("pageUrl", ""))
            for field in ("bluesky", "github_discussions")
            for draft in (drafts_dict or {}).get(field) or []
        ]
        repeated_keys, repeated_runs = repeated_publication_keys(
            state.get("recent_announcements", []), draft_urls
        )
        if repeated_keys:
            _emit(
                state,
                "repeat_suppressed",
                {
                    "channels": ",".join(sorted({channel for channel, _ in repeated_keys})),
                    "earlier_runs": ",".join(sorted(repeated_runs)),
                },
            )
        evaluated = state.get("evaluator_result")
        internal_note = evaluated.internal_note if evaluated is not None else ""
        if internal_note:
            _emit(
                state,
                "internal_note",
                {"text": internal_note, "sent": not (dry_run or not publish_approved)},
            )
        if dry_run or not publish_approved:
            _emit(state, "published", {"completed": True, "postworthy": postworthy})
            return {
                "publications": publications,
                "result": ReleaseCommsRunResult(
                    promote_run_id=state["promote_run_id"],
                    new_sha=state["new_sha"],
                    previous_sha=state.get("previous_sha"),
                    postworthy=postworthy,
                    drafts=drafts_dict,
                    published=publications,
                    completed=True,
                ),
            }

        evaluation = state.get("evaluator_result")
        recommended_channels = evaluation.recommended_channels if evaluation is not None else []
        already_published = {
            publication_key(record.channel, record.destination, record.page_url)
            for record in state.get("existing_publications", [])
            if record.status in ("published", "staged")
        } | repeated_keys
        if internal_note:
            # Technical notes go to the project's own Discord only. A Discord failure must not
            # block the public announcements below, so it is recorded rather than raised.
            try:
                for receipt in publish_internal_note(
                    repo=state["project"].repo,
                    note=internal_note,
                    source_id=str(state["promote_run_id"]),
                    env=os.environ,
                    dry_run=False,
                    already_published=already_published,
                ):
                    state["publication_sink"](receipt)
                    already_published.add(
                        publication_key(receipt.channel, receipt.destination, receipt.page_url)
                    )
            except PublicationError as error:
                _emit(state, "internal_note_failed", {"error": str(error)[:200]})
        published, errors = publish_release_drafts(
            project=state["project"],
            drafts=drafts_dict or {},
            recommended_channels=recommended_channels,
            env=os.environ,
            dry_run=False,
            already_published=already_published,
            image_overrides=state.get("image_overrides"),
            publication_delay_min_seconds=0,
            publication_delay_max_seconds=0,
            max_publications=1,
            on_receipt=state["publication_sink"],
            source_id=str(state["promote_run_id"]),
            devvit_app_dir=(state["config"].devvit_app_dir),
            devvit_subreddit=(state["config"].devvit_subreddit or state["config"].subreddit),
        )
        publications.update(published)
        completed = not errors
        postworthy = bool(state["evaluator_result"] and state["evaluator_result"].postworthy)
        remaining = pending_publication_count(
            repo=state["project"].repo,
            drafts=drafts_dict or {},
            recommended_channels=recommended_channels,
            already_published=already_published,
        )
        if not errors and published and remaining:
            delay_min = max(0.0, state["config"].publication_delay_min_seconds)
            delay_max = max(delay_min, state["config"].publication_delay_max_seconds)
            delay = random.uniform(delay_min, delay_max)
            next_publication_at = (
                datetime.now(UTC) + timedelta(seconds=delay)
            ).isoformat()
            return {
                "publications": publications,
                "scheduled": True,
                "next_publication_at": next_publication_at,
                "remaining_publications": remaining,
                "result": ReleaseCommsRunResult(
                    promote_run_id=state["promote_run_id"],
                    new_sha=state["new_sha"],
                    previous_sha=state.get("previous_sha"),
                    postworthy=postworthy,
                    drafts=drafts_dict,
                    published=publications,
                    completed=False,
                    scheduled=True,
                ),
            }
        _emit(
            state,
            "published",
            {"completed": completed, "postworthy": postworthy, "errors": errors},
        )
        comment_tracking_issue(
            project=state["project"],
            issue_number=state["config"].tracking_issue,
            promote_run_id=state["promote_run_id"],
            evaluator=asdict(evaluation) if evaluation is not None else {},
            drafts=drafts_dict,
            publications=publications,
            errors=errors,
        )
        return {
            "publications": publications,
            "result": ReleaseCommsRunResult(
                promote_run_id=state["promote_run_id"],
                new_sha=state["new_sha"],
                previous_sha=state.get("previous_sha"),
                postworthy=postworthy,
                drafts=drafts_dict,
                published=publications,
                completed=completed,
            ),
        }

    def schedule_publication(state: ReleaseCommsState) -> dict[str, Any]:
        """Record the durable wake-up point after one publication succeeds."""
        _emit(
            state,
            "scheduled",
            {
                "next_publication_at": state.get("next_publication_at"),
                "remaining": state.get("remaining_publications", 0),
            },
        )
        return {}

    def route_after_publish(state: ReleaseCommsState) -> str:
        return "schedule_publication" if state.get("scheduled") else END

    graph.add_node("resolve_context", resolve_context)
    graph.add_node("evaluate_changes", evaluate_changes)
    graph.add_node("route_forms", route_forms)
    graph.add_node("write_short", write_short)
    graph.add_node("write_long", write_long)
    graph.add_node("merge_drafts", merge_drafts)
    graph.add_node("generate_art", generate_art)
    graph.add_node("publish_destinations", publish_destinations)
    graph.add_node("schedule_publication", schedule_publication)

    graph.add_edge(START, "resolve_context")
    graph.add_edge("resolve_context", "evaluate_changes")
    graph.add_edge("evaluate_changes", "route_forms")
    graph.add_conditional_edges("route_forms", pick_forms)
    graph.add_edge("write_short", "merge_drafts")
    graph.add_edge("write_long", "merge_drafts")
    graph.add_conditional_edges("merge_drafts", pick_art)
    graph.add_edge("generate_art", "publish_destinations")
    graph.add_conditional_edges("publish_destinations", route_after_publish)
    graph.add_edge("schedule_publication", END)

    return graph.compile()


def run_release_comms(
    project: ProjectConfig,
    project_name: str,
    promote_run_id: str,
    *,
    dry_run: bool = True,
    publish_approved: bool = False,
    evaluator_result: EvaluatorResult | None = None,
    writer_result: WriterResult | None = None,
    on_event: ProgressEvent | None = None,
    delivery_id: str | None = None,
) -> ReleaseCommsRunResult:
    """Execute the release communications workflow."""
    config = project.release_comms or ReleaseCommsConfig()
    database_path = state_database_path(
        config.state_path, project.repo / ".dev-agents/release-comms-state.db"
    )
    repository = StateRepository(database_path, project_name, project.repo)
    existing = repository.get_run("release-comms", str(promote_run_id))
    resume_metadata: dict[str, Any] = {}
    existing_metadata: dict[str, Any] = {}
    pinned_new_sha: str | None = None
    pinned_previous_sha: str | None = None
    # "running" is included so a stale/interrupted claim (e.g. the daemon restarted
    # mid-run) can reuse its carried-over drafts on retry; claim_run() below is the
    # actual safety gate (it refuses to reclaim a run that is genuinely still active).
    # "rejected" is a permanent verdict (claim_run refuses to reclaim it, same as
    # "completed") so it is deliberately excluded here: nothing to resume.
    if existing is not None and existing.status in ("scheduled", "failed", "running"):
        existing_metadata = existing.metadata
        raw_resume_metadata = existing_metadata.get("_release_comms_resume", {})
        resume_metadata = raw_resume_metadata if isinstance(raw_resume_metadata, dict) else {}
        if existing.status == "scheduled":
            next_at = str(existing_metadata.get("next_publication_at", ""))
            try:
                due = datetime.fromisoformat(next_at)
            except ValueError:
                due = datetime.now(UTC)
            if due > datetime.now(UTC):
                raise DuplicateRunError(
                    f"release-comms run {promote_run_id} is scheduled for {next_at}"
                )
        if evaluator_result is None:
            evaluator_result = parse_evaluator_result(resume_metadata.get("evaluator_result"))
        if writer_result is None:
            writer_result = parse_writer_result(resume_metadata.get("writer_result"))
        pinned_new_sha = resume_metadata.get("new_sha") or None
        pinned_previous_sha = resume_metadata.get("previous_sha") or None
    claim = repository.claim_run(
        "release-comms",
        str(promote_run_id),
        delivery_id=delivery_id,
        metadata={
            **existing_metadata,
            "promote_run_id": str(promote_run_id),
            "dry_run": dry_run,
        },
    )
    if not claim.claimed:
        raise DuplicateRunError(f"release-comms run {promote_run_id} was already claimed")

    def observe(event: str, payload: dict[str, Any]) -> None:
        repository.record_event(
            "release-comms", str(promote_run_id), event, event, metadata=payload
        )
        if on_event is not None:
            on_event(event, payload)

    def persist_publication(receipt: PublicationReceipt) -> None:
        status = (
            "staged"
            if receipt.metadata and receipt.metadata.get("status") == "staged_to_r2"
            else "published"
        )
        repository.record_publication(
            "release-comms",
            str(promote_run_id),
            receipt.channel,
            destination=receipt.destination,
            page_url=receipt.page_url,
            public_url=receipt.public_url,
            external_id=receipt.external_id,
            status=status,
            metadata=receipt.metadata or {"publisher": "dev-agents"},
        )

    try:
        from dev_agents.workflows.reddit import sync_reddit_status

        sync_reddit_status(
            project=project,
            project_name=project_name,
            repository=repository,
            notify_tracking_issue=False,
        )
    except Exception:  # noqa: BLE001, S110 - opportunistic sync should not block run
        pass

    try:
        app = build_release_comms_workflow()
        initial_state: ReleaseCommsState = {
            "repo": project.repo,
            "project": project,
            "project_name": project_name,
            "promote_run_id": promote_run_id,
            "config": config,
            "dry_run": dry_run,
            "publish_approved": publish_approved,
            "evaluator_result": evaluator_result,
            "writer_result": writer_result,
            "image_overrides": (
                resume_metadata.get("image_overrides", {})
                if isinstance(resume_metadata.get("image_overrides", {}), dict)
                else {}
            ),
            "on_event": observe,
            "existing_publications": repository.list_run_publications(
                "release-comms", str(promote_run_id)
            ),
            "handled_shas": earlier_release_shas(repository, exclude_run_id=str(promote_run_id)),
            "recent_announcements": recent_announcements(
                repository, exclude_run_id=str(promote_run_id), days=config.recent_posts_days
            ),
            "publication_sink": persist_publication,
            **({"new_sha": pinned_new_sha, "previous_sha": pinned_previous_sha} if pinned_new_sha else {}),
        }
        final_state = app.invoke(initial_state)
        result = cast(ReleaseCommsRunResult, final_state["result"])
    except Exception as error:
        repository.complete_run("release-comms", str(promote_run_id), status="failed", error=str(error))
        from dev_agents.visualize import schedule_report_refresh

        schedule_report_refresh(project_name, project)
        raise
    if final_state.get("scheduled"):
        repository.update_run(
            "release-comms",
            str(promote_run_id),
            status="scheduled",
            metadata={
                "next_publication_at": final_state.get("next_publication_at"),
                "_release_comms_resume": {
                    "evaluator_result": asdict(final_state.get("evaluator_result")),
                    "writer_result": asdict(final_state.get("writer_result")),
                    "image_overrides": final_state.get("image_overrides", {}),
                    "new_sha": final_state.get("new_sha"),
                    "previous_sha": final_state.get("previous_sha"),
                },
            },
        )
        repository.record_event(
            "release-comms",
            str(promote_run_id),
            "__run__",
            "run_scheduled",
            status="scheduled",
            metadata={"next_publication_at": final_state.get("next_publication_at")},
        )
        from dev_agents.visualize import schedule_report_refresh

        schedule_report_refresh(project_name, project)
        return result
    final_evaluator_result = final_state.get("evaluator_result")
    final_writer_result = final_state.get("writer_result")
    if not result.completed:
        final_status = "failed"
    elif not result.postworthy:
        final_status = "rejected"
    else:
        final_status = "completed"
    # Pin the resolved SHAs (and any drafted content) for "failed" (re-triggerable,
    # must resume against the same diff) and "rejected" (not re-triggerable --
    # claim_run() refuses to reclaim it -- but recorded for audit: a human reviewing
    # why a release was rejected can see exactly what diff was evaluated).
    resume_for_retry = (
        {
            "_release_comms_resume": {
                "evaluator_result": (
                    asdict(final_evaluator_result) if final_evaluator_result is not None else None
                ),
                "writer_result": (
                    asdict(final_writer_result) if final_writer_result is not None else None
                ),
                "image_overrides": final_state.get("image_overrides", {}),
                "new_sha": final_state.get("new_sha"),
                "previous_sha": final_state.get("previous_sha"),
            }
        }
        if final_status in ("failed", "rejected")
        else {}
    )
    repository.complete_run(
        "release-comms",
        str(promote_run_id),
        status=final_status,
        error=None if result.completed else "publishing failed",
        metadata={
            "postworthy": result.postworthy,
            "drafts": result.drafts,
            **resume_for_retry,
        },
    )
    from dev_agents.visualize import schedule_report_refresh

    schedule_report_refresh(project_name, project)
    return result
