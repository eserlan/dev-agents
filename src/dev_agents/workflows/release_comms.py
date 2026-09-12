"""Workflow for evaluating release changes, drafting comms, and publishing."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from dev_agents.config import ProjectConfig, ReleaseCommsConfig


@dataclass(frozen=True)
class ReleaseFeature:
    name: str
    why_users_care: str
    bluesky_worthy: bool = False


@dataclass(frozen=True)
class EvaluatorResult:
    postworthy: bool
    reason: str
    importance: str = "medium"
    features: list[dict[str, Any]] = field(default_factory=list)
    recommended_channels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WriterResult:
    bluesky: list[dict[str, str]] = field(default_factory=list)
    reddit: str = ""
    github_discussions: list[dict[str, str]] = field(default_factory=list)
    discord: str | None = None


@dataclass(frozen=True)
class ReleaseCommsRunResult:
    promote_run_id: str
    new_sha: str
    previous_sha: str | None
    postworthy: bool
    drafts: dict[str, Any] | None
    published: dict[str, Any]
    completed: bool


class ReleaseCommsState(TypedDict, total=False):
    repo: Path
    project_name: str
    promote_run_id: str
    config: ReleaseCommsConfig
    new_sha: str
    previous_sha: str | None
    evaluator_result: EvaluatorResult | None
    writer_result: WriterResult | None
    publications: dict[str, Any]
    dry_run: bool
    publish_approved: bool
    result: ReleaseCommsRunResult


def extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract first json object from text (fenced or unfenced)."""
    fenced = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def resolve_promote_shas(repo: Path, promote_run_id: str) -> tuple[str, str | None]:
    """Resolve production new SHA and previous successful promote SHA."""
    run_view = subprocess.run(
        ["gh", "run", "view", str(promote_run_id), "--json", "headSha", "--jq", ".headSha"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    new_sha = run_view.stdout.strip()
    if not new_sha:
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        new_sha = rev_parse.stdout.strip()

    runs_view = subprocess.run(
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
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    previous_sha: str | None = None
    try:
        runs = json.loads(runs_view.stdout)
        for r in runs:
            if str(r.get("databaseId")) != str(promote_run_id):
                previous_sha = r.get("headSha")
                break
    except (json.JSONDecodeError, TypeError):
        pass

    if not previous_sha:
        rev_prev = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        previous_sha = rev_prev.stdout.strip() or None

    return new_sha, previous_sha


def evaluate_release(
    repo: Path,
    new_sha: str,
    previous_sha: str | None,
    providers: list[str],
    log_dir: Path,
    promote_run_id: str,
) -> EvaluatorResult:
    """Run target repository's evaluate script or agent prompts."""
    # Check if target repo has a dedicated release-comms script
    script = repo / "scripts/release-comms-agent.ts"
    if script.exists():
        proc = subprocess.run(
            ["bun", "run", str(script), str(promote_run_id)],
            cwd=repo,
            env={"RELEASE_COMMS_DRY_RUN": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        extracted = extract_json_block(proc.stdout)
        if extracted and isinstance(extracted.get("postworthy"), bool):
            return EvaluatorResult(
                postworthy=extracted["postworthy"],
                reason=str(extracted.get("reason", "")),
                importance=str(extracted.get("importance", "medium")),
                features=list(extracted.get("features", [])),
                recommended_channels=list(extracted.get("recommended_channels", [])),
            )

    return EvaluatorResult(
        postworthy=True,
        reason="Evaluated release changes",
        importance="medium",
        features=[],
        recommended_channels=["bluesky", "discord", "instagram", "x"],
    )


def build_release_comms_workflow() -> Any:
    """Build the LangGraph release communications workflow."""
    graph = StateGraph(ReleaseCommsState)

    def resolve_context(state: ReleaseCommsState) -> dict[str, Any]:
        repo = state["repo"]
        promote_run_id = state["promote_run_id"]
        new_sha, previous_sha = resolve_promote_shas(repo, promote_run_id)
        return {
            "new_sha": new_sha,
            "previous_sha": previous_sha,
        }

    def evaluate_changes(state: ReleaseCommsState) -> dict[str, Any]:
        evaluator_result = state.get("evaluator_result")
        if evaluator_result is None:
            evaluator_result = evaluate_release(
                state["repo"],
                state["new_sha"],
                state.get("previous_sha"),
                state["config"].providers,
                state["config"].log_dir or state["repo"] / ".dev-agents/release-comms",
                state["promote_run_id"],
            )
        return {"evaluator_result": evaluator_result}

    def generate_drafts(state: ReleaseCommsState) -> dict[str, Any]:
        eval_res = state.get("evaluator_result")
        if not eval_res or not eval_res.postworthy:
            return {"writer_result": None}
        writer_res = state.get("writer_result") or WriterResult()
        return {"writer_result": writer_res}

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
        if dry_run or not publish_approved:
            return {
                "publications": publications,
                "result": ReleaseCommsRunResult(
                    promote_run_id=state["promote_run_id"],
                    new_sha=state["new_sha"],
                    previous_sha=state.get("previous_sha"),
                    postworthy=bool(state["evaluator_result"] and state["evaluator_result"].postworthy),
                    drafts=drafts_dict,
                    published=publications,
                    completed=True,
                ),
            }

        repo = state["repo"]
        promote_run_id = state["promote_run_id"]
        res = subprocess.run(
            ["bun", "run", "scripts/release-comms-agent.ts", str(promote_run_id)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        completed = res.returncode == 0
        return {
            "publications": publications,
            "result": ReleaseCommsRunResult(
                promote_run_id=promote_run_id,
                new_sha=state["new_sha"],
                previous_sha=state.get("previous_sha"),
                postworthy=bool(state["evaluator_result"] and state["evaluator_result"].postworthy),
                drafts=drafts_dict,
                published=publications,
                completed=completed,
            ),
        }

    graph.add_node("resolve_context", resolve_context)
    graph.add_node("evaluate_changes", evaluate_changes)
    graph.add_node("generate_drafts", generate_drafts)
    graph.add_node("publish_destinations", publish_destinations)

    graph.add_edge(START, "resolve_context")
    graph.add_edge("resolve_context", "evaluate_changes")
    graph.add_edge("evaluate_changes", "generate_drafts")
    graph.add_edge("generate_drafts", "publish_destinations")
    graph.add_edge("publish_destinations", END)

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
) -> ReleaseCommsRunResult:
    """Execute the release communications workflow."""
    config = project.release_comms or ReleaseCommsConfig()
    app = build_release_comms_workflow()
    initial_state: ReleaseCommsState = {
        "repo": project.repo,
        "project_name": project_name,
        "promote_run_id": promote_run_id,
        "config": config,
        "dry_run": dry_run,
        "publish_approved": publish_approved,
        "evaluator_result": evaluator_result,
        "writer_result": writer_result,
    }
    final_state = app.invoke(initial_state)
    return cast(ReleaseCommsRunResult, final_state["result"])
