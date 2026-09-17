"""Interactive local HTML reports for LangGraph structure and persisted runs."""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from dev_agents.config import ProjectConfig
from dev_agents.runtime import state_database_path
from dev_agents.workflows.degodify import build_degodify_workflow
from dev_agents.workflows.inspection import build_inspection_workflow
from dev_agents.workflows.release_comms import build_release_comms_workflow

WORKFLOW_NAMES = (
    "inspection",
    "degodify",
    "issue-fixer",
    "pr-fixer",
    "pr-review",
    "release-comms",
)
PR_DATABASE_WORKFLOWS = (
    "pr-fixer",
    "pr-review",
    "issue-fixer",
    "issue-reconcile",
    "degodify",
    "github-webhook",
)
REPORT_HIDE_AFTER = timedelta(hours=1)
REPORT_DELETE_AFTER = timedelta(days=1)
REPORT_DEPLOY_BACKOFF = timedelta(hours=24)


def default_report_path(project_name: str) -> Path:
    """Return the stable automatic-report location for a project."""
    return Path.home() / ".local/state/dev-agents" / project_name / "dev-agents-flow.html"


def report_run_url(project: ProjectConfig, workflow: str, run_id: str) -> str | None:
    """Return a stable report URL focused on one persisted workflow run."""
    host = project.report_vercel_alias or project.report_vercel_project
    if not host:
        return None
    base = host.rstrip("/")
    if not base.startswith(("http://", "https://")):
        domain = base if "." in base else f"{base}.vercel.app"
        base = f"https://{domain}"
    return f"{base}?{urlencode({'workflow': workflow, 'run': run_id})}"


def refresh_report_run_url(
    project_name: str, project: ProjectConfig, workflow: str, run_id: str
) -> str | None:
    """Refresh the local report and return a stable run-focused URL.

    The local snapshot is refreshed immediately. Public deployments are deliberately throttled;
    when a deployment is deferred, callers still receive the stable configured dashboard URL.
    """
    output = getattr(project, "visualization_path", None) or default_report_path(project_name)
    try:
        report = refresh_project_report(project_name, project, output=output)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Report refresh before comment failed: {error}", file=sys.stderr)
        return None
    deploy_report_to_vercel(project, report)
    return report_run_url(project, workflow, run_id)


def write_report(output: Path, report: str) -> Path:
    """Write a report atomically so concurrent job refreshes cannot corrupt the HTML."""
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=f".{output.name}.", delete=False
    ) as file:
        file.write(report)
        temporary = Path(file.name)
    temporary.replace(output)
    return output


def refresh_project_report(
    project_name: str,
    project: ProjectConfig,
    *,
    output: Path | None = None,
    limit: int = 50,
) -> Path:
    """Generate the project report from its latest state and return its path."""
    destination = output or default_report_path(project_name)
    return write_report(
        destination,
        render_report(project_name, project, limit=limit),
    )


def _parse_report_deploy_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _report_deploy_state_path(report: Path) -> Path:
    return report.with_name(f".{report.name}.vercel-deploy.json")


def _write_report_deploy_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _report_deploy_lock(report: Path) -> Iterator[None]:
    lock_path = report.with_name(f".{report.name}.vercel-deploy.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def deploy_report_to_vercel(project: ProjectConfig, report: Path) -> str | None:
    """Deploy a report while serializing concurrent upload attempts for that report."""
    resolved_report = report.expanduser().resolve()
    if not resolved_report.is_file():
        return _deploy_report_to_vercel_locked(project, resolved_report)
    with _report_deploy_lock(resolved_report):
        return _deploy_report_to_vercel_locked(project, resolved_report)


def _deploy_report_to_vercel_locked(project: ProjectConfig, report: Path) -> str | None:
    """Best-effort deploy a generated report to an opted-in Vercel project.

    The report worker calls this after the local file has been written. Deployment is
    deliberately isolated from workflow success: missing configuration, CLI/auth failures,
    and alias failures are all reported to stderr and return ``None``.
    """
    project_name = project.report_vercel_project
    if not project_name:
        return None

    executable = shutil.which("vercel")
    if executable is None:
        bun_executable = Path.home() / ".cache/.bun/bin/vercel"
        executable = str(bun_executable) if bun_executable.is_file() else None
    if executable is None:
        print(
            "Vercel report deployment skipped: the 'vercel' CLI is not installed",
            file=sys.stderr,
        )
        return None

    if not report.is_file():
        print(f"Vercel report deployment skipped: report does not exist: {report}", file=sys.stderr)
        return None

    report_bytes = report.read_bytes()
    content_hash = hashlib.sha256(report_bytes).hexdigest()
    state_path = _report_deploy_state_path(report)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}

    cached_url = state.get("deployment_url")
    if state.get("content_hash") == content_hash and isinstance(cached_url, str) and cached_url:
        print(f"Vercel report deployment reused: {cached_url}", file=sys.stderr)
        return cached_url

    now = datetime.now(UTC)
    blocked_until = _parse_report_deploy_time(state.get("blocked_until"))
    if blocked_until and blocked_until > now:
        print(
            f"Vercel report deployment throttled until {blocked_until.isoformat()}: quota backoff",
            file=sys.stderr,
        )
        return None

    recent_deployments = [
        timestamp
        for value in state.get("deployments", [])
        if (timestamp := _parse_report_deploy_time(value)) is not None
        and now - timestamp < timedelta(hours=24)
    ]
    max_deployments = max(1, project.report_vercel_max_deployments_24h)
    if len(recent_deployments) >= max_deployments:
        print(
            "Vercel report deployment throttled: 24-hour upload limit reached",
            file=sys.stderr,
        )
        return None

    last_deployed_at = _parse_report_deploy_time(state.get("deployed_at"))
    if last_deployed_at and (
        now - last_deployed_at
    ).total_seconds() < project.report_vercel_min_interval_seconds:
        print(
            "Vercel report deployment throttled: minimum upload interval not reached",
            file=sys.stderr,
        )
        return None

    # Deploy a directory containing only index.html. In particular, do not deploy the report's
    # parent directory, which may contain unrelated state files or other reports.
    with tempfile.TemporaryDirectory(prefix="dev-agents-vercel-") as directory:
        deployment_root = Path(directory)
        (deployment_root / "index.html").write_bytes(report_bytes)
        command = [
            executable,
            "deploy",
            str(deployment_root),
            "--yes",
            "--name",
            project_name,
        ]
        # A token is recommended for unattended services, but the CLI's logged-in user
        # credentials are also sufficient for a local daemon running as that same user.
        token = os.environ.get(project.report_vercel_token_env)
        if token:
            command[4:4] = ["--token", token]
        if project.report_vercel_scope:
            command.extend(("--scope", project.report_vercel_scope))

        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=project.report_vercel_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"Vercel report deployment failed: {error}", file=sys.stderr)
            return None

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Vercel error"
            if "api-deployments-free-per-day" in detail:
                state["blocked_until"] = (now + REPORT_DEPLOY_BACKOFF).isoformat()
                _write_report_deploy_state(state_path, state)
            print(f"Vercel report deployment failed: {detail}", file=sys.stderr)
            return None

        deployment_output = result.stdout
        deployment_urls = re.findall(r'"url"\s*:\s*"(https?://[^"\\]+)"', deployment_output)
        if deployment_urls:
            deployment_url = str(deployment_urls[-1])
        else:
            deployment_url = next(
                (
                    line.strip()
                    for line in reversed(deployment_output.splitlines())
                    if line.strip().startswith(("https://", "http://"))
                ),
                "",
            )
        if not deployment_url:
            print("Vercel report deployment failed: CLI returned no deployment URL", file=sys.stderr)
            return None

        if project.report_vercel_alias:
            alias_command = [
                executable,
                "alias",
                "set",
                deployment_url,
                project.report_vercel_alias,
            ]
            if token:
                alias_command.extend(("--token", token))
            if project.report_vercel_scope:
                alias_command.extend(("--scope", project.report_vercel_scope))
            try:
                alias_result = subprocess.run(
                    alias_command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=project.report_vercel_timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Vercel report alias failed: {error}", file=sys.stderr)
            else:
                if alias_result.returncode != 0:
                    detail = (
                        alias_result.stderr.strip()
                        or alias_result.stdout.strip()
                        or "unknown Vercel error"
                    )
                    print(f"Vercel report alias failed: {detail}", file=sys.stderr)

        state["content_hash"] = content_hash
        state["deployment_url"] = deployment_url
        state["deployed_at"] = now.isoformat()
        state["blocked_until"] = None
        state["deployments"] = [
            timestamp.isoformat() for timestamp in recent_deployments[-127:]
        ] + [now.isoformat()]
        _write_report_deploy_state(state_path, state)
        print(f"Vercel report deployed: {deployment_url}", file=sys.stderr)
        return deployment_url


def schedule_report_refresh(project_name: str, project: ProjectConfig) -> None:
    """Schedule a best-effort report refresh without holding up a completed job.

    A detached interpreter is used instead of a daemon thread so short-lived CLI jobs do not
    exit before the refresh has written its file. Failures are intentionally isolated from the
    completed workflow; the next job can try again.
    """
    output = getattr(project, "visualization_path", None) or default_report_path(project_name)
    command = [
        sys.executable,
        "-m",
        "dev_agents.visualize_refresh",
        project_name,
        "--output",
        str(output.expanduser()),
        "--project-json",
        json.dumps(project.model_dump(mode="json"), separators=(",", ":")),
    ]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        # Report refresh is observability only and must never alter job success.
        return


def _pr_fixer_mermaid() -> str:
    return """graph TD
    __start__([START]) --> collect_feedback[collect_feedback]
    collect_feedback --> remediate[remediate]
    remediate --> finalize[finalize]
    finalize --> __end__([END])"""


def _pr_review_mermaid() -> str:
    return """graph TD
    __start__([START]) --> collect_feedback[collect_feedback]
    collect_feedback --> general_review[general_review]
    general_review --> codex_review[codex_review]
    codex_review --> remediate[remediate]
    remediate --> finalize[finalize]
    finalize --> __end__([END])"""


def _issue_fixer_mermaid() -> str:
    return """graph TD
    __start__([START]) --> collect_issue[collect_issue]
    collect_issue --> remediate[remediate]
    remediate --> finalize[finalize]
    finalize --> __end__([END])"""


def workflow_mermaid(workflow: str) -> str:
    """Return the current static graph definition for compatibility callers."""
    if workflow == "pr-fixer":
        return _pr_fixer_mermaid()
    if workflow == "pr-review":
        return _pr_review_mermaid()
    if workflow == "issue-fixer":
        return _issue_fixer_mermaid()
    builders = {
        "inspection": build_inspection_workflow,
        "degodify": build_degodify_workflow,
        "release-comms": build_release_comms_workflow,
    }
    try:
        return cast(str, builders[workflow]().get_graph().draw_mermaid())
    except KeyError as error:
        raise ValueError(f"unknown workflow: {workflow}") from error


def workflow_graph(workflow: str) -> dict[str, list[dict[str, Any]]]:
    """Return node and edge data suitable for an interactive graph renderer."""
    if workflow == "pr-fixer":
        return {
            "nodes": [
                {"id": "__start__", "label": "START", "kind": "start"},
                {"id": "collect_feedback", "label": "collect_feedback", "kind": "node"},
                {"id": "remediate", "label": "remediate", "kind": "node"},
                {"id": "finalize", "label": "finalize", "kind": "node"},
                {"id": "__end__", "label": "END", "kind": "end"},
            ],
            "edges": [
                {"source": "__start__", "target": "collect_feedback", "conditional": False},
                {"source": "collect_feedback", "target": "remediate", "conditional": False},
                {"source": "remediate", "target": "finalize", "conditional": False},
                {"source": "finalize", "target": "__end__", "conditional": False},
            ],
        }
    if workflow == "pr-review":
        nodes = [
            {"id": "__start__", "label": "START", "kind": "start"},
            {"id": "collect_feedback", "label": "collect_feedback", "kind": "node"},
            {"id": "general_review", "label": "general_review", "kind": "node"},
            {"id": "codex_review", "label": "codex_review", "kind": "node"},
            {"id": "remediate", "label": "remediate", "kind": "node"},
            {"id": "finalize", "label": "finalize", "kind": "node"},
            {"id": "__end__", "label": "END", "kind": "end"},
        ]
        node_ids = [node["id"] for node in nodes]
        return {
            "nodes": nodes,
            "edges": [
                {"source": source, "target": target, "conditional": False}
                for source, target in pairwise(node_ids)
            ],
        }
    if workflow == "issue-fixer":
        nodes = [
            {"id": "__start__", "label": "START", "kind": "start"},
            {"id": "collect_issue", "label": "collect_issue", "kind": "node"},
            {"id": "remediate", "label": "remediate", "kind": "node"},
            {"id": "finalize", "label": "finalize", "kind": "node"},
            {"id": "__end__", "label": "END", "kind": "end"},
        ]
        node_ids = [node["id"] for node in nodes]
        return {
            "nodes": nodes,
            "edges": [
                {"source": source, "target": target, "conditional": False}
                for source, target in pairwise(node_ids)
            ],
        }
    if workflow == "release-comms":
        # The platform calls happen inside the target publisher subprocess rather than as
        # separate Python LangGraph nodes. These channel nodes are an explicit observability
        # projection, populated with receipts from the publications table at render time.
        channel_nodes = [
            {"id": f"channel:{channel}", "label": label, "kind": "channel"}
            for channel, label in (
                ("bluesky", "Bluesky"),
                ("instagram", "Instagram"),
                ("discord", "Discord"),
                ("github_discussions", "GitHub Discussions"),
                ("x", "X"),
            )
        ]
        nodes = [
            {"id": "__start__", "label": "START", "kind": "start"},
            {"id": "resolve_context", "label": "resolve_context", "kind": "node"},
            {"id": "evaluate_changes", "label": "evaluate_changes", "kind": "node"},
            {"id": "route_forms", "label": "route_forms", "kind": "node"},
            {"id": "write_short", "label": "write_short", "kind": "node"},
            {"id": "write_long", "label": "write_long", "kind": "node"},
            {"id": "merge_drafts", "label": "merge_drafts", "kind": "node"},
            {"id": "generate_art", "label": "generate_art", "kind": "node"},
            {"id": "publish_destinations", "label": "publish_destinations", "kind": "node"},
            {"id": "schedule_publication", "label": "schedule_publication", "kind": "node"},
            *channel_nodes,
            {"id": "__end__", "label": "END", "kind": "end"},
        ]
        edge_specs: list[tuple[str, str, bool]] = [
            ("__start__", "resolve_context", False),
            ("resolve_context", "evaluate_changes", False),
            ("evaluate_changes", "route_forms", False),
            ("route_forms", "write_short", True),
            ("route_forms", "write_long", True),
            ("route_forms", "merge_drafts", True),
            ("write_short", "merge_drafts", False),
            ("write_long", "merge_drafts", False),
            ("merge_drafts", "generate_art", True),
            ("merge_drafts", "publish_destinations", True),
            ("generate_art", "publish_destinations", False),
            ("publish_destinations", "schedule_publication", True),
            ("schedule_publication", "__end__", False),
            *[("publish_destinations", node["id"], False) for node in channel_nodes],
            *[(node["id"], "__end__", False) for node in channel_nodes],
        ]
        edges = [
            {"source": source, "target": target, "conditional": conditional}
            for source, target, conditional in edge_specs
        ]
        return {
            "nodes": nodes,
            "edges": edges,
        }
    builders = {
        "inspection": build_inspection_workflow,
        "degodify": build_degodify_workflow,
    }
    try:
        graph = builders[workflow]().get_graph()
    except KeyError as error:
        raise ValueError(f"unknown workflow: {workflow}") from error
    nodes = []
    for node in graph.nodes.values():
        node_id = str(node.id)
        kind = "start" if node_id == "__start__" else "end" if node_id == "__end__" else "node"
        nodes.append({"id": node_id, "label": str(node.name or node.id), "kind": kind})
    edges = [
        {
            "source": str(edge.source),
            "target": str(edge.target),
            "conditional": bool(edge.conditional),
        }
        for edge in graph.edges
    ]
    return {"nodes": nodes, "edges": edges}


def _database_paths(project_name: str, project: ProjectConfig) -> list[tuple[Path, tuple[str, ...]]]:
    paths: list[tuple[Path, tuple[str, ...]]] = []
    if project.pr_fixer is not None:
        paths.append(
            (
                state_database_path(
                    project.pr_fixer.state_path,
                    Path.home() / ".local/state/dev-agents" / project_name / "pr-fixer-state.db",
                ),
                PR_DATABASE_WORKFLOWS,
            )
        )
    if project.release_comms is not None:
        paths.append(
            (
                state_database_path(
                    project.release_comms.state_path,
                    project.repo / ".dev-agents/release-comms-state.db",
                ),
                ("release-comms",),
            )
        )
    return paths


def _decode_metadata(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def _run_activity_at(run: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "completed_at", "started_at"):
        timestamp = _parse_timestamp(run.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _retain_report_runs(runs: list[dict[str, Any]], generated_at: datetime) -> list[dict[str, Any]]:
    """Keep report data within its one-day message retention window."""
    cutoff = generated_at - REPORT_DELETE_AFTER
    retained: list[dict[str, Any]] = []
    for run in runs:
        activity_at = _run_activity_at(run)
        if activity_at is None or activity_at < cutoff:
            continue
        retained.append(
            {
                **run,
                "events": [
                    event
                    for event in run["events"]
                    if (event_at := _parse_timestamp(event.get("completed_at") or event.get("started_at")))
                    is not None
                    and event_at >= cutoff
                ],
                "publications": [
                    publication
                    for publication in run["publications"]
                    if (publication_at := _parse_timestamp(publication.get("published_at")))
                    is not None
                    and publication_at >= cutoff
                ],
            }
        )
    return retained


def _read_runs(
    path: Path, project_name: str, workflows: tuple[str, ...] | None, limit: int
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        workflow_filter = ""
        parameters: list[Any] = [project_name]
        if workflows:
            placeholders = ",".join("?" for _ in workflows)
            workflow_filter = f" AND workflow IN ({placeholders})"
            parameters.extend(workflows)
        limit_clause = " LIMIT ?" if limit > 0 else ""
        if limit > 0:
            parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT * FROM runs
            WHERE project_name = ?{workflow_filter}
            ORDER BY started_at DESC{limit_clause}
            """,
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            metadata = _decode_metadata(row["metadata"])
            try:
                events = connection.execute(
                    """
                    SELECT * FROM run_events
                    WHERE project_name = ? AND workflow = ? AND run_id = ?
                    ORDER BY sequence
                    """,
                    (project_name, row["workflow"], row["run_id"]),
                ).fetchall()
            except sqlite3.OperationalError:
                events = []
            try:
                publications = connection.execute(
                    """
                    SELECT * FROM publications
                    WHERE project_name = ? AND workflow = ? AND run_id = ?
                    ORDER BY published_at, channel, destination, page_url
                    """,
                    (project_name, row["workflow"], row["run_id"]),
                ).fetchall()
            except sqlite3.OperationalError:
                publications = []
            result.append(
                {
                    "project_name": row["project_name"],
                    "workflow": row["workflow"],
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "delivery_id": row["delivery_id"],
                    "metadata": metadata,
                    "error": row["error"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "updated_at": row["updated_at"],
                    "database_path": str(path),
                    "events": [
                        {
                            "sequence": event["sequence"],
                            "node": event["node"],
                            "event": event["event"],
                            "status": event["status"],
                            "metadata": _decode_metadata(event["metadata"]),
                            "started_at": event["started_at"],
                            "completed_at": event["completed_at"],
                        }
                        for event in events
                    ],
                    "publications": [
                        {
                            "channel": publication["channel"],
                            "destination": publication["destination"],
                            "page_url": publication["page_url"],
                            "public_url": publication["public_url"],
                            "external_id": publication["external_id"],
                            "status": publication["status"],
                            "error": publication["error"],
                            "published_at": publication["published_at"],
                            "metadata": _decode_metadata(publication["metadata"]),
                        }
                        for publication in publications
                    ],
                }
            )
        return result
    except sqlite3.DatabaseError:
        return []
    finally:
        if connection is not None:
            connection.close()


def _text(value: Any) -> str:
    return html.escape(str(value))


def _json_for_script(value: Any) -> str:
    """Serialize data safely inside a script element."""
    return json.dumps(value, sort_keys=True, default=str).replace("<", "\\u003c")


INTERACTIVE_SCRIPT = r"""
const report = __REPORT_DATA__;
const canvas = document.querySelector('#graph');
const context = canvas.getContext('2d');
const workflowSelect = document.querySelector('#workflow-select');
const runSelect = document.querySelector('#run-select');
const runSummary = document.querySelector('#run-summary');
const nodeDetails = document.querySelector('#node-details');
const eventList = document.querySelector('#event-list');
const emptyState = document.querySelector('#empty-state');
const graphHost = document.querySelector('#graph-host');
const indexSearch = document.querySelector('#index-search');
const indexStatus = document.querySelector('#index-status');
const indexWorkflowOptions = document.querySelector('#index-workflow-options');
const indexWorkflowSummary = document.querySelector('#index-workflow-summary');
const indexSummary = document.querySelector('#index-summary');
const indexColumns = document.querySelector('#index-columns');
const indexRows = document.querySelector('#index-rows');
const indexDetails = document.querySelector('#index-details');
const pubSearch = document.querySelector('#pub-search');
const pubStatus = document.querySelector('#pub-status');
const pubChannelOptions = document.querySelector('#pub-channel-options');
const pubChannelSummary = document.querySelector('#pub-channel-summary');
const pubSummary = document.querySelector('#pub-summary');
const pubColumns = document.querySelector('#pub-columns');
const pubRows = document.querySelector('#pub-rows');
const reportParams = new URLSearchParams(window.location.search);
const requestedWorkflow = reportParams.get('workflow') || '';
const requestedRun = reportParams.get('run') || reportParams.get('run_id') || '';
let reportView = reportParams.get('view') === 'release-comms' || requestedWorkflow === 'release-comms' ? 'release-comms' : 'daemon';
let currentWorkflow = report.workflows[0] || {name: '', graph: {nodes: [], edges: []}, runs: []};
let currentRun = null;
let selectedNode = null;
let layout = new Map();
let zoom = 1;
let pan = {x: 18, y: 0};
let dragging = false;
let dragStart = null;
let indexSort = {key: 'started_at', direction: 'desc'};
let pubSort = {key: 'published_at', direction: 'desc'};
const pubColumnDefinitions = [
  {key: 'status', label: 'Status'},
  {key: 'channel', label: 'Channel'},
  {key: 'destination', label: 'Destination'},
  {key: 'content', label: 'Content'},
  {key: 'published_at', label: 'Sent'},
  {key: 'run_id', label: 'Run'},
];
const indexColumnDefinitions = [
  {key: 'status', label: 'Status'},
  {key: 'workflow', label: 'Workflow'},
  {key: 'run_id', label: 'Run'},
  {key: 'started_at', label: 'Started'},
  {key: 'duration', label: 'Duration'},
  {key: 'publications', label: 'Published'},
  {key: 'database', label: 'Database'},
];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function pretty(value) { return escapeHtml(JSON.stringify(value ?? {}, null, 2)); }
function findWorkflow(name) { return report.workflows.find(item => item.name === name) || null; }
function reportWorkflowNames() {
  const names = [...new Set([
    ...(report.workflows || []).map(workflow => workflow.name),
    ...(report.allRuns || []).map(run => run.workflow),
  ])].sort();
  return reportView === 'release-comms' ? names.filter(name => name === 'release-comms') : names.filter(name => name !== 'release-comms');
}
function workflowIsDefault(workflow) { return !/(webhook|reconcile)/i.test(workflow); }
function selectedIndexWorkflows() { return new Set([...indexWorkflowOptions.querySelectorAll('input:checked')].map(input => input.value)); }
function updateWorkflowFilterSummary() {
  const selected = selectedIndexWorkflows(), total = indexWorkflowOptions.querySelectorAll('input').length;
  indexWorkflowSummary.textContent = selected.size === total ? 'All workflows' : `${selected.size} selected`;
}
function renderWorkflowOptions() {
  const names = reportWorkflowNames();
  indexWorkflowOptions.innerHTML = names.map(workflow => `<label class="workflow-option"><input type="checkbox" value="${escapeHtml(workflow)}" ${workflowIsDefault(workflow) ? 'checked' : ''}> <span>${escapeHtml(workflow)}</span></label>`).join('');
  indexWorkflowOptions.querySelectorAll('input').forEach(input => input.addEventListener('change', () => { updateWorkflowFilterSummary(); renderIndex(); }));
  updateWorkflowFilterSummary();
}
function renderWorkflowSelect() {
  const names = reportWorkflowNames();
  workflowSelect.innerHTML = names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
  const selected = names.includes(currentWorkflow.name) ? currentWorkflow.name : names[0];
  if (selected) { workflowSelect.value = selected; updateWorkflow(); }
}
function setReportView(view) {
  reportView = view === 'release-comms' ? 'release-comms' : 'daemon';
  document.querySelector('#daemon-tab').setAttribute('aria-selected', reportView === 'daemon' ? 'true' : 'false');
  document.querySelector('#release-comms-tab').setAttribute('aria-selected', reportView === 'release-comms' ? 'true' : 'false');
  document.querySelector('#index-panel').hidden = reportView !== 'daemon';
  document.querySelector('#pub-panel').hidden = reportView !== 'release-comms';
  renderWorkflowOptions();
  renderWorkflowSelect();
  if (history.replaceState) {
    const params = new URLSearchParams(window.location.search);
    if (reportView === 'release-comms') params.set('view', 'release-comms'); else params.delete('view');
    if (reportView === 'daemon' && params.get('workflow') === 'release-comms') {
      params.delete('workflow'); params.delete('run'); params.delete('run_id');
    }
    history.replaceState(null, '', `${window.location.pathname}${params.toString() ? `?${params}` : ''}`);
  }
}
function runActivityTimestamp(run) { return Date.parse(run.updated_at || run.completed_at || run.started_at); }
function isRecentRun(run) {
  const timestamp = runActivityTimestamp(run);
  return Number.isFinite(timestamp) && report.generatedAt - timestamp <= report.retention.hideAfterSeconds * 1000;
}
function eventForNode(nodeId) { return (currentRun?.events || []).filter(event => event.node === nodeId); }
function publicationsForNode(nodeId) { const channel = nodeId?.startsWith('channel:') ? nodeId.slice(8) : null; return channel ? (currentRun?.publications || []).filter(publication => publication.channel === channel) : []; }
function nodeStatus(nodeId) {
  const events = eventForNode(nodeId);
  const publications = publicationsForNode(nodeId);
  if (publications.some(publication => ['failed', 'error'].includes(publication.status))) return 'failed';
  if (publications.length) return 'completed';
  if (events.some(event => ['failed', 'error'].includes(event.status))) return 'failed';
  if (events.length) return 'completed';
  return 'idle';
}
function visited(nodeId) { return eventForNode(nodeId).length > 0 || publicationsForNode(nodeId).length > 0; }
function edgeStatus(edge) { return visited(edge.source) && visited(edge.target) ? nodeStatus(edge.target) : 'idle'; }
function nodeLabel(node) { return node.kind === 'start' ? 'START' : node.kind === 'end' ? 'END' : node.label; }

function duration(run) {
  if (!run.completed_at) return 'in progress';
  const milliseconds = Date.parse(run.completed_at) - Date.parse(run.started_at);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '—';
  if (milliseconds < 1000) return `${milliseconds}ms`;
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(1)}s`;
  return `${Math.floor(milliseconds / 60000)}m ${Math.round((milliseconds % 60000) / 1000)}s`;
}
function publicationCount(run) { return (run.publications || []).filter(publication => publication.status === 'published').length; }
function indexSortValue(run, key) {
  if (key === 'duration') {
    if (!run.completed_at) return -1;
    const milliseconds = Date.parse(run.completed_at) - Date.parse(run.started_at);
    return Number.isFinite(milliseconds) && milliseconds >= 0 ? milliseconds : -1;
  }
  if (key === 'publications') return publicationCount(run);
  if (key === 'database') return run.database_path || '';
  if (key === 'started_at') {
    const timestamp = Date.parse(run.started_at);
    return Number.isFinite(timestamp) ? timestamp : 0;
  }
  return run[key] || '';
}
function sortIndexRuns(runs) {
  return runs.map((run, index) => ({run, index})).sort((left, right) => {
    const a = indexSortValue(left.run, indexSort.key), b = indexSortValue(right.run, indexSort.key);
    let comparison;
    if (typeof a === 'number' && typeof b === 'number') comparison = a - b;
    else comparison = String(a).localeCompare(String(b), undefined, {numeric: true, sensitivity: 'base'});
    return (indexSort.direction === 'asc' ? comparison : -comparison) || left.index - right.index;
  }).map(item => item.run);
}
function renderIndexColumns() {
  indexColumns.innerHTML = indexColumnDefinitions.map(column => {
    const active = indexSort.key === column.key, arrow = active ? (indexSort.direction === 'asc' ? ' ↑' : ' ↓') : '';
    return `<button class="index-column" type="button" data-sort-key="${column.key}" aria-label="Sort by ${column.label}" aria-sort="${active ? indexSort.direction : 'none'}">${column.label}${arrow}</button>`;
  }).join('');
  indexColumns.querySelectorAll('.index-column').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.sortKey;
    if (indexSort.key === key) indexSort.direction = indexSort.direction === 'asc' ? 'desc' : 'asc';
    else { indexSort.key = key; indexSort.direction = key === 'started_at' ? 'desc' : 'asc'; }
    renderIndex();
  }));
}
function formatRunDetails(run) {
  const publications = (run.publications || []).map(publication => `<div class="publication-detail"><span class="status ${escapeHtml(publication.status)}">${escapeHtml(publication.status)}</span><b>${escapeHtml(publication.channel)}</b>${publication.destination ? ` · ${escapeHtml(publication.destination)}` : ''}${publication.public_url ? ` · <a href="${escapeHtml(publication.public_url)}" target="_blank" rel="noreferrer">${escapeHtml(publication.public_url)}</a>` : ''}${publication.page_url ? `<small>${escapeHtml(publication.page_url)}</small>` : ''}${publication.error ? `<p class="error">${escapeHtml(publication.error)}</p>` : ''}</div>`).join('');
  return `<div class="run-title"><span class="status ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span><b>${escapeHtml(run.workflow)} / ${escapeHtml(run.run_id)}</b></div><p>Started ${escapeHtml(run.started_at)} · completed ${escapeHtml(run.completed_at || '—')} · duration ${escapeHtml(duration(run))}</p>${run.error ? `<p class="error">${escapeHtml(run.error)}</p>` : ''}<details><summary>Run metadata</summary><pre>${pretty(run.metadata)}</pre></details><h3>Publications (${publicationCount(run)})</h3>${publications || '<p>No publication receipts recorded.</p>'}<h3>Events (${run.events.length})</h3>${run.events.map(event => `<div class="event-detail"><div><b>${escapeHtml(event.node)}</b> · ${escapeHtml(event.event)} <span class="status ${escapeHtml(event.status)}">${escapeHtml(event.status)}</span></div><time>${escapeHtml(event.started_at)}</time><pre>${pretty(event.metadata)}</pre></div>`).join('') || '<p>No recorded events.</p>'}`;
}
function openIndexedRun(run) {
  const workflow = findWorkflow(run.workflow);
  if (workflow && isRecentRun(run)) {
    workflowSelect.value = workflow.name; updateWorkflow(); runSelect.value = run.run_id; updateRun(); document.querySelector('.explorer').scrollIntoView({behavior: 'smooth', block: 'start'}); return;
  }
  indexDetails.hidden = false; indexDetails.innerHTML = formatRunDetails(run); indexDetails.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function indexRowMarkup(run) {
  return `<button class="index-row" data-workflow="${escapeHtml(run.workflow)}" data-run="${escapeHtml(run.run_id)}"><span class="status ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span><b>${escapeHtml(run.workflow)}</b><code>${escapeHtml(run.run_id)}</code><time>${escapeHtml(run.started_at)}</time><span>${escapeHtml(duration(run))}</span><span>${escapeHtml(publicationCount(run))} published</span><small>${escapeHtml(run.database_path.split('/').pop())}</small></button>`;
}
function bindIndexRows() {
  indexRows.querySelectorAll('.index-row').forEach(row => row.addEventListener('click', () => openIndexedRun((report.allRuns || []).find(run => run.workflow === row.dataset.workflow && run.run_id === row.dataset.run) || null)));
}
function renderIndex() {
  const query = indexSearch.value.trim().toLowerCase(), status = indexStatus.value, workflows = selectedIndexWorkflows();
  const filtered = (report.allRuns || []).filter(run => {
    const searchable = `${run.workflow} ${run.run_id} ${run.delivery_id || ''} ${run.error || ''} ${JSON.stringify(run.metadata)} ${run.events.map(event => `${event.node} ${event.event}`).join(' ')}`.toLowerCase();
    return (!query || searchable.includes(query)) && (!status || run.status === status) && workflows.has(run.workflow);
  });
  const failed = filtered.filter(run => ['failed', 'error'].includes(run.status)).length;
  const active = filtered.filter(run => ['running', 'pending', 'scheduled'].includes(run.status)).length;
  const sorted = sortIndexRuns(filtered), recent = sorted.filter(isRecentRun), older = sorted.filter(run => !isRecentRun(run));
  indexSummary.textContent = `${filtered.length} runs · ${recent.length} visible · ${older.length} collapsed · ${failed} failed · ${active} active`;
  if (!filtered.length) {
    indexRows.innerHTML = '<p class="empty-index">No daemon runs match these filters.</p>';
    return;
  }
  indexRows.innerHTML = recent.map(indexRowMarkup).join('') + (older.length ? `<details class="older-runs"><summary>Older than 1 hour (${older.length})</summary><div class="older-index-rows">${older.map(indexRowMarkup).join('')}</div></details>` : '');
  bindIndexRows();
}

function selectedPubChannels() { return new Set([...pubChannelOptions.querySelectorAll('input:checked')].map(input => input.value)); }
function updatePubChannelSummary() {
  const selected = selectedPubChannels(), total = pubChannelOptions.querySelectorAll('input').length;
  pubChannelSummary.textContent = selected.size === total ? 'All channels' : `${selected.size} selected`;
}
function renderPubChannelOptions() {
  const channels = [...new Set((report.allPublications || []).map(publication => publication.channel))].sort();
  pubChannelOptions.innerHTML = channels.map(channel => `<label class="workflow-option"><input type="checkbox" value="${escapeHtml(channel)}" checked> <span>${escapeHtml(channel)}</span></label>`).join('');
  pubChannelOptions.querySelectorAll('input').forEach(input => input.addEventListener('change', () => { updatePubChannelSummary(); renderPublications(); }));
  updatePubChannelSummary();
}
function pubContent(publication) { return publication.public_url || publication.page_url || ''; }
function pubSortValue(publication, key) {
  if (key === 'content') return pubContent(publication);
  if (key === 'published_at') { const timestamp = Date.parse(publication.published_at); return Number.isFinite(timestamp) ? timestamp : 0; }
  return publication[key] || '';
}
function sortPubs(publications) {
  return publications.map((publication, index) => ({publication, index})).sort((left, right) => {
    const a = pubSortValue(left.publication, pubSort.key), b = pubSortValue(right.publication, pubSort.key);
    let comparison;
    if (typeof a === 'number' && typeof b === 'number') comparison = a - b;
    else comparison = String(a).localeCompare(String(b), undefined, {numeric: true, sensitivity: 'base'});
    return (pubSort.direction === 'asc' ? comparison : -comparison) || left.index - right.index;
  }).map(item => item.publication);
}
function renderPubColumns() {
  pubColumns.innerHTML = pubColumnDefinitions.map(column => {
    const active = pubSort.key === column.key, arrow = active ? (pubSort.direction === 'asc' ? ' ↑' : ' ↓') : '';
    return `<button class="index-column" type="button" data-sort-key="${column.key}" aria-label="Sort by ${column.label}" aria-sort="${active ? pubSort.direction : 'none'}">${column.label}${arrow}</button>`;
  }).join('');
  pubColumns.querySelectorAll('.index-column').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.sortKey;
    if (pubSort.key === key) pubSort.direction = pubSort.direction === 'asc' ? 'desc' : 'asc';
    else { pubSort.key = key; pubSort.direction = key === 'published_at' ? 'desc' : 'asc'; }
    renderPublications();
  }));
}
function pubRowMarkup(publication) {
  const content = pubContent(publication);
  return `<button class="index-row pub-row" data-workflow="${escapeHtml(publication.workflow)}" data-run="${escapeHtml(publication.run_id)}"><span class="status ${escapeHtml(publication.status)}">${escapeHtml(publication.status)}</span><b>${escapeHtml(publication.channel)}</b><span>${escapeHtml(publication.destination || '—')}</span>${content ? `<a href="${escapeHtml(content)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">${escapeHtml(content)}</a>` : '<span>—</span>'}<time>${escapeHtml(publication.published_at)}</time><code>${escapeHtml(publication.workflow)}/${escapeHtml(publication.run_id)}</code></button>`;
}
function bindPubRows() {
  pubRows.querySelectorAll('.pub-row').forEach(row => row.addEventListener('click', () => openIndexedRun((report.allRuns || []).find(run => run.workflow === row.dataset.workflow && run.run_id === row.dataset.run) || null)));
}
function renderPublications() {
  const query = pubSearch.value.trim().toLowerCase(), status = pubStatus.value, channels = selectedPubChannels();
  const filtered = (report.allPublications || []).filter(publication => {
    const searchable = `${publication.destination} ${publication.page_url} ${publication.public_url || ''} ${publication.run_id} ${publication.error || ''}`.toLowerCase();
    return (!query || searchable.includes(query)) && (!status || publication.status === status) && channels.has(publication.channel);
  });
  const failed = filtered.filter(publication => ['failed', 'error'].includes(publication.status)).length;
  pubSummary.textContent = `${filtered.length} deliveries · ${failed} failed`;
  if (!filtered.length) { pubRows.innerHTML = '<p class="empty-index">No publication receipts match these filters.</p>'; return; }
  pubRows.innerHTML = sortPubs(filtered).map(pubRowMarkup).join('');
  bindPubRows();
}

function rebuildLayout() {
  const nodes = currentWorkflow.graph.nodes, edges = currentWorkflow.graph.edges;
  const layers = new Map();
  const start = nodes.find(node => node.kind === 'start')?.id || nodes[0]?.id;
  if (start) layers.set(start, 0);
  for (let pass = 0; pass < nodes.length; pass++) edges.forEach(edge => {
    if (layers.has(edge.source)) layers.set(edge.target, Math.max(layers.get(edge.target) ?? 0, layers.get(edge.source) + 1));
  });
  nodes.forEach(node => { if (!layers.has(node.id)) layers.set(node.id, 0); });
  const byLayer = new Map();
  nodes.forEach(node => { const layer = layers.get(node.id); if (!byLayer.has(layer)) byLayer.set(layer, []); byLayer.get(layer).push(node); });
  layout = new Map();
  [...byLayer.keys()].sort((a, b) => a - b).forEach(layer => {
    const members = byLayer.get(layer);
    members.forEach((node, index) => layout.set(node.id, {x: 140 + index * 220, y: 85 + layer * 125, width: Math.max(150, Math.min(220, node.label.length * 9 + 42)), height: 54}));
  });
}
function resizeCanvas() {
  const bounds = graphHost.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, bounds.width * ratio); canvas.height = Math.max(360, Math.min(680, Math.max(360, bounds.height) * ratio));
  canvas.style.height = `${canvas.height / ratio}px`; context.setTransform(ratio, 0, 0, ratio, 0, 0); draw();
}
function worldPoint(x, y) { return {x: (x - pan.x) / zoom, y: (y - pan.y) / zoom}; }
function drawArrow(edge) {
  const source = layout.get(edge.source), target = layout.get(edge.target); if (!source || !target) return;
  const x1 = source.x, y1 = source.y + source.height / 2, x2 = target.x, y2 = target.y - target.height / 2;
  const status = edgeStatus(edge), color = status === 'failed' ? '#ff7280' : visited(edge.target) ? '#7dd3fc' : '#475569';
  context.strokeStyle = color; context.lineWidth = status === 'idle' ? 1.5 : 3; context.setLineDash(edge.conditional ? [6, 5] : []);
  context.beginPath(); context.moveTo(x1, y1); const bend = (y2 - y1) / 2; context.bezierCurveTo(x1, y1 + bend, x2, y2 - bend, x2, y2); context.stroke(); context.setLineDash([]);
  const angle = Math.atan2(y2 - y1, x2 - x1); context.fillStyle = color; context.beginPath(); context.moveTo(x2, y2); context.lineTo(x2 - 10 * Math.cos(angle - 0.45), y2 - 10 * Math.sin(angle - 0.45)); context.lineTo(x2 - 10 * Math.cos(angle + 0.45), y2 - 10 * Math.sin(angle + 0.45)); context.fill();
}
function draw() {
  const width = canvas.clientWidth, height = canvas.clientHeight; context.clearRect(0, 0, width, height); context.save(); context.translate(pan.x, pan.y); context.scale(zoom, zoom);
  currentWorkflow.graph.edges.forEach(drawArrow);
  currentWorkflow.graph.nodes.forEach(node => {
    const box = layout.get(node.id); if (!box) return; const status = nodeStatus(node.id), selected = selectedNode === node.id;
    const fill = node.kind === 'start' || node.kind === 'end' ? '#202b3b' : status === 'failed' ? '#542a34' : status === 'completed' ? '#123e3c' : '#1c2633';
    const stroke = selected ? '#f8d477' : status === 'failed' ? '#ff7280' : status === 'completed' ? '#46d7aa' : '#64748b';
    context.fillStyle = fill; context.strokeStyle = stroke; context.lineWidth = selected ? 4 : 2; context.beginPath(); context.roundRect(box.x - box.width / 2, box.y - box.height / 2, box.width, box.height, 12); context.fill(); context.stroke();
    context.fillStyle = '#edf2f7'; context.font = '600 13px system-ui, sans-serif'; context.textAlign = 'center'; context.textBaseline = 'middle'; context.fillText(nodeLabel(node), box.x, box.y - (status === 'idle' ? 0 : 8));
    if (status !== 'idle') { context.fillStyle = stroke; context.font = '10px system-ui, sans-serif'; context.fillText(status.toUpperCase(), box.x, box.y + 14); }
  }); context.restore();
}
function updateDetails() {
  const events = eventForNode(selectedNode), publications = publicationsForNode(selectedNode); if (!selectedNode) { nodeDetails.innerHTML = '<p>Select a node to inspect its events.</p>'; return; }
  const node = currentWorkflow.graph.nodes.find(item => item.id === selectedNode);
  const publicationMarkup = publications.map(publication => `<div class="publication-detail"><span class="status ${escapeHtml(publication.status)}">${escapeHtml(publication.status)}</span>${publication.destination ? ` <b>${escapeHtml(publication.destination)}</b>` : ''}${publication.public_url ? ` · <a href="${escapeHtml(publication.public_url)}" target="_blank" rel="noreferrer">${escapeHtml(publication.public_url)}</a>` : ''}${publication.page_url ? `<small>${escapeHtml(publication.page_url)}</small>` : ''}${publication.external_id ? `<small>ID: ${escapeHtml(publication.external_id)}</small>` : ''}</div>`).join('');
  nodeDetails.innerHTML = `<h3>${escapeHtml(nodeLabel(node || {label: selectedNode}))}</h3>` + (publicationMarkup ? `<h4>Publication receipts</h4>${publicationMarkup}` : '') + (events.length ? `<h4>Events</h4>${events.map(event => `<div class="event-detail"><div><b>${escapeHtml(event.event)}</b> <span class="status ${escapeHtml(event.status)}">${escapeHtml(event.status)}</span></div><time>${escapeHtml(event.started_at)}${event.completed_at && event.completed_at !== event.started_at ? ` → ${escapeHtml(event.completed_at)}` : ''}</time><pre>${pretty(event.metadata)}</pre></div>`).join('')}` : '<p>No recorded events for this node in the selected run.</p>');
}
function updateRun() {
  currentRun = currentWorkflow.runs.find(run => run.run_id === runSelect.value) || null; selectedNode = null; emptyState.hidden = Boolean(currentRun);
  if (!currentRun) { runSummary.innerHTML = '<p>No persisted runs for this workflow.</p>'; eventList.innerHTML = ''; updateDetails(); draw(); return; }
  runSummary.innerHTML = `<div class="run-title"><span class="status ${escapeHtml(currentRun.status)}">${escapeHtml(currentRun.status)}</span><b>${escapeHtml(currentRun.run_id)}</b></div><p>Started ${escapeHtml(currentRun.started_at)} · completed ${escapeHtml(currentRun.completed_at || '—')} · attempt ${escapeHtml(currentRun.attempt)}</p>${currentRun.error ? `<p class="error">${escapeHtml(currentRun.error)}</p>` : ''}<details><summary>Run metadata</summary><pre>${pretty(currentRun.metadata)}</pre></details>`;
  eventList.innerHTML = currentRun.events.length ? currentRun.events.map(event => `<button class="event-row" data-node="${escapeHtml(event.node)}"><span>${escapeHtml(event.sequence)}</span><b>${escapeHtml(event.node)}</b><span>${escapeHtml(event.event)}</span><i class="status ${escapeHtml(event.status)}">${escapeHtml(event.status)}</i></button>`).join('') : '<p>No node events recorded.</p>';
  eventList.querySelectorAll('.event-row').forEach(button => button.addEventListener('click', () => { selectedNode = button.dataset.node; updateDetails(); draw(); })); updateDetails(); draw();
}
function updateWorkflow() { currentWorkflow = findWorkflow(workflowSelect.value); runSelect.innerHTML = currentWorkflow.runs.map(run => `<option value="${escapeHtml(run.run_id)}">${escapeHtml(run.status)} · ${escapeHtml(run.run_id)}</option>`).join(''); rebuildLayout(); updateRun(); }
function resetView() { zoom = 1; pan = {x: 18, y: canvas.clientHeight / 2}; draw(); }
canvas.addEventListener('click', event => {
  if (dragStart && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > 4) return;
  const bounds = canvas.getBoundingClientRect(), point = worldPoint(event.clientX - bounds.left, event.clientY - bounds.top); selectedNode = null;
  for (const node of currentWorkflow.graph.nodes) { const box = layout.get(node.id); if (box && Math.abs(point.x - box.x) <= box.width / 2 && Math.abs(point.y - box.y) <= box.height / 2) { selectedNode = node.id; break; } }
  updateDetails(); draw();
});
canvas.addEventListener('pointerdown', event => { dragging = true; dragStart = {x: event.clientX, y: event.clientY}; canvas.setPointerCapture(event.pointerId); });
canvas.addEventListener('pointermove', event => { if (dragging) { pan.x += event.movementX; pan.y += event.movementY; draw(); } });
canvas.addEventListener('pointerup', event => { dragging = false; canvas.releasePointerCapture(event.pointerId); });
canvas.addEventListener('wheel', event => { event.preventDefault(); const factor = event.deltaY < 0 ? 1.1 : 0.9, bounds = canvas.getBoundingClientRect(), before = worldPoint(event.clientX - bounds.left, event.clientY - bounds.top); zoom = Math.max(.45, Math.min(2.5, zoom * factor)); pan.x = event.clientX - bounds.left - before.x * zoom; pan.y = event.clientY - bounds.top - before.y * zoom; draw(); }, {passive: false});
workflowSelect.addEventListener('change', updateWorkflow); runSelect.addEventListener('change', updateRun); document.querySelector('#reset-view').addEventListener('click', resetView); window.addEventListener('resize', resizeCanvas);
indexSearch.addEventListener('input', renderIndex); indexStatus.addEventListener('change', renderIndex);
pubSearch.addEventListener('input', renderPublications); pubStatus.addEventListener('change', renderPublications);
document.querySelector('#daemon-tab').addEventListener('click', () => setReportView('daemon'));
document.querySelector('#release-comms-tab').addEventListener('click', () => setReportView('release-comms'));
renderIndexColumns();
renderPubChannelOptions();
renderPubColumns();
renderPublications();
setReportView(reportView);
if (requestedWorkflow && findWorkflow(requestedWorkflow)) {
  indexWorkflowOptions.querySelectorAll('input').forEach(input => { input.checked = input.value === requestedWorkflow; });
  updateWorkflowFilterSummary();
  if (requestedRun) indexSearch.value = requestedRun;
}
renderIndex();
if (requestedWorkflow && findWorkflow(requestedWorkflow) && reportWorkflowNames().includes(requestedWorkflow)) {
  workflowSelect.value = requestedWorkflow;
  updateWorkflow();
  if (requestedRun && currentWorkflow.runs.some(run => run.run_id === requestedRun)) {
    runSelect.value = requestedRun;
    updateRun();
  }
}
resizeCanvas(); resetView();
"""


def _interactive_app(data: dict[str, Any]) -> str:
    script = INTERACTIVE_SCRIPT.replace("__REPORT_DATA__", _json_for_script(data))
    return f"""
    <nav class="report-tabs" role="tablist" aria-label="Report views">
      <button id="daemon-tab" type="button" role="tab" aria-selected="true" aria-controls="index-panel">Daemon runs</button>
      <button id="release-comms-tab" type="button" role="tab" aria-selected="false" aria-controls="pub-panel">Release comms</button>
    </nav>
    <section id="index-panel" class="run-index" role="tabpanel" aria-labelledby="daemon-tab">
      <div class="index-header"><div><h2>Daemon run index</h2><p class="subtitle">Recent jobs are visible; jobs older than 1 hour are collapsed. Report messages older than 1 day are omitted.</p></div><strong id="index-summary"></strong></div>
      <div class="index-filters"><input id="index-search" type="search" placeholder="Search run, event, PR, error…" aria-label="Search daemon runs"><details id="index-workflow-filter" class="workflow-filter"><summary>Workflows: <span id="index-workflow-summary">All workflows</span></summary><div id="index-workflow-options" class="workflow-options" role="group" aria-label="Filter by workflow"></div></details><select id="index-status" aria-label="Filter by status"><option value="">All statuses</option><option value="completed">completed</option><option value="failed">failed</option><option value="running">running</option><option value="pending">pending</option><option value="scheduled">scheduled</option></select></div>
      <div id="index-columns" class="index-columns" role="row" aria-label="Sort runs by column"></div>
      <div id="index-rows" class="index-rows"></div>
      <div id="index-details" class="index-details" hidden></div>
    </section>
    <section id="pub-panel" class="run-index pub-index" role="tabpanel" aria-labelledby="release-comms-tab" hidden>
      <div class="index-header"><div><h2>Release comms deliveries</h2><p class="subtitle">Every channel receipt written by the release-comms daemon: what was sent, to which channel, when, and with what result.</p></div><strong id="pub-summary"></strong></div>
      <div class="index-filters"><input id="pub-search" type="search" placeholder="Search destination, URL, run, error…" aria-label="Search publications"><details id="pub-channel-filter" class="workflow-filter"><summary>Channels: <span id="pub-channel-summary">All channels</span></summary><div id="pub-channel-options" class="workflow-options" role="group" aria-label="Filter by channel"></div></details><select id="pub-status" aria-label="Filter by status"><option value="">All statuses</option><option value="published">published</option><option value="failed">failed</option><option value="error">error</option><option value="pending">pending</option></select></div>
      <div id="pub-columns" class="pub-columns index-columns" role="row" aria-label="Sort publications by column"></div>
      <div id="pub-rows" class="pub-rows index-rows"></div>
    </section>
    <section class="explorer">
      <div class="toolbar">
        <label>Workflow <select id="workflow-select"></select></label>
        <label>Run <select id="run-select"></select></label>
        <button id="reset-view" type="button">Reset view</button>
        <span class="hint">Click nodes or events · drag to pan · scroll to zoom</span>
      </div>
      <div class="explorer-grid">
        <div id="graph-host"><canvas id="graph" aria-label="Interactive workflow graph"></canvas><div id="empty-state" class="empty-state" hidden>Select a run to see what happened.</div></div>
        <aside class="inspector"><div id="run-summary"></div><hr><div id="node-details"><p>Select a node to inspect its events.</p></div><hr><h3>Run timeline</h3><div id="event-list"></div></aside>
      </div>
      <p class="legend"><span><i class="dot idle"></i>not visited</span><span><i class="dot completed"></i>completed</span><span><i class="dot failed"></i>failed</span><span>Dashed edges are conditional paths.</span></p>
      <noscript><p class="error">This interactive report needs JavaScript enabled.</p></noscript>
    </section>
    <script>{script}</script>
    """


def render_report(
    project_name: str,
    project: ProjectConfig,
    *,
    workflow: str | None = None,
    limit: int = 50,
) -> str:
    """Render a self-contained interactive canvas report with bounded message history."""
    generated_at = datetime.now(UTC)
    selected = [workflow] if workflow else list(WORKFLOW_NAMES)
    unknown = set(selected) - set(WORKFLOW_NAMES)
    if unknown:
        raise ValueError(f"unknown workflow: {min(unknown)}")
    all_runs: list[dict[str, Any]] = []
    recent_runs: list[dict[str, Any]] = []
    for path, _database_workflows in _database_paths(project_name, project):
        runs = _retain_report_runs(
            _read_runs(path, project_name, None, 0),
            generated_at,
        )
        all_runs.extend(runs)
        recent_runs.extend(
            run
            for run in runs
            if (activity_at := _run_activity_at(run)) is not None
            and activity_at >= generated_at - REPORT_HIDE_AFTER
        )
    all_runs = [run for run in all_runs if not workflow or run["workflow"] == workflow]
    all_runs.sort(key=lambda run: run["started_at"], reverse=True)
    recent_runs = [run for run in recent_runs if run["workflow"] in selected]
    recent_runs.sort(key=lambda run: run["started_at"], reverse=True)
    recent_runs = recent_runs[:limit]
    all_publications = [
        {
            "workflow": run["workflow"],
            "run_id": run["run_id"],
            "run_started_at": run["started_at"],
            **publication,
        }
        for run in all_runs
        for publication in run["publications"]
    ]
    all_publications.sort(key=lambda publication: publication["published_at"], reverse=True)
    data = {
        "project": project_name,
        "repository": str(project.repo),
        "generatedAt": generated_at.timestamp() * 1000,
        "retention": {
            "hideAfterSeconds": int(REPORT_HIDE_AFTER.total_seconds()),
            "deleteAfterSeconds": int(REPORT_DELETE_AFTER.total_seconds()),
        },
        "allRuns": all_runs,
        "allPublications": all_publications,
        "workflows": [
            {"name": name, "graph": workflow_graph(name), "runs": [run for run in recent_runs if run["workflow"] == name]}
            for name in selected
        ],
    }
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>dev-agents flow — {_text(project_name)}</title>
<style>
:root{{color-scheme:dark;--bg:#0d1117;--panel:#151c26;--panel2:#1b2532;--border:#334155;--text:#e5edf5;--muted:#94a3b8;--gold:#f8d477;--cyan:#7dd3fc;--green:#46d7aa;--red:#ff7280}}
*{{box-sizing:border-box}}body{{font:14px system-ui,sans-serif;max-width:1500px;margin:0 auto;padding:24px;background:var(--bg);color:var(--text)}}h1,h2,h3{{color:var(--gold);margin-top:0}}h1{{margin-bottom:4px}}code,pre,select,button{{font-family:ui-monospace,SFMono-Regular,monospace}}code{{color:var(--cyan)}}
.report-tabs{{display:flex;gap:8px;margin-top:24px}}.report-tabs button{{border-radius:8px 8px 0 0;border-bottom:2px solid transparent;color:var(--muted)}}.report-tabs button[aria-selected="true"]{{background:var(--panel);border-color:var(--gold);color:var(--gold)}}
.subtitle,.hint,.legend{{color:var(--muted)}}.run-index,.explorer{{margin-top:24px;background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden}}.run-index{{padding:18px}}.index-header{{display:flex;justify-content:space-between;gap:16px;align-items:start}}.index-header h2{{margin-bottom:4px}}.index-header .subtitle{{margin:0}}.index-header strong{{color:var(--cyan);white-space:nowrap}}.index-filters{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 10px}}input,select,button{{border:1px solid var(--border);border-radius:7px;background:var(--panel2);color:var(--text);padding:8px 10px}}input{{min-width:260px;flex:1}}button{{cursor:pointer}}button:hover{{border-color:var(--gold)}}.index-columns,.index-row{{display:grid;grid-template-columns:86px 125px minmax(150px,1.4fr) minmax(165px,1fr) 70px 100px 145px;align-items:center;gap:10px;text-align:left;font-size:12px}}.index-columns{{margin-bottom:5px}}.index-column{{border:0;background:transparent;color:var(--muted);padding:6px 10px;font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;text-align:left;white-space:nowrap}}.index-column:hover,.index-column[aria-sort="asc"],.index-column[aria-sort="desc"]{{color:var(--gold)}}.index-rows{{display:grid;gap:5px}}.index-row{{font-family:system-ui}}.index-row code,.index-row time,.index-row small{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.index-row time,.index-row small{{color:var(--muted)}}.index-row b{{overflow:hidden;text-overflow:ellipsis}}.older-runs{{margin-top:12px;border-top:1px solid var(--border);padding-top:10px}}.older-runs summary{{padding:6px 10px}}.older-index-rows{{display:grid;gap:5px;margin-top:6px}}.index-details{{border-top:1px solid var(--border);margin-top:14px;padding-top:14px;max-width:900px}}.publication-detail{{border-left:2px solid var(--green);padding:7px 0 7px 10px;margin:7px 0}}.publication-detail small{{display:block;color:var(--muted);overflow-wrap:anywhere;margin-top:4px}}.publication-detail a{{color:var(--cyan);overflow-wrap:anywhere}}.empty-index{{color:var(--muted)}}.toolbar{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--border);background:#111923}}label{{display:flex;align-items:center;gap:8px;color:var(--muted)}}.hint{{margin-left:auto;font-size:12px}}
.explorer-grid{{display:grid;grid-template-columns:minmax(0,1fr) 350px;min-height:520px}}#graph-host{{position:relative;min-height:520px;background:radial-gradient(#263342 1px,transparent 1px);background-size:22px 22px;overflow:hidden}}canvas{{display:block;width:100%;height:520px;cursor:grab}}canvas:active{{cursor:grabbing}}.empty-state{{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);pointer-events:none}}.inspector{{padding:18px;overflow:auto;max-height:620px;background:#111923;border-left:1px solid var(--border)}}.inspector hr{{border:0;border-top:1px solid var(--border);margin:18px 0}}.inspector p{{color:var(--muted);line-height:1.45}}.run-title{{display:flex;gap:9px;align-items:center;flex-wrap:wrap}}.run-title b{{overflow-wrap:anywhere}}.status{{display:inline-block;border-radius:999px;padding:2px 7px;font-size:11px;background:#334155;color:var(--muted)}}.status.completed{{background:#123e3c;color:#8af0cf}}.status.failed,.status.error{{background:#542a34;color:#ffb6bd}}.status.running,.status.pending{{background:#4a3a1b;color:#ffe5a1}}.error{{color:#ffb6bd!important}}pre{{white-space:pre-wrap;overflow:auto;background:#0b1016;border-radius:7px;padding:9px;font-size:11px;color:#cbd5e1}}details summary{{cursor:pointer;color:var(--cyan)}}.event-detail{{border-left:2px solid var(--border);padding:6px 0 8px 10px;margin:9px 0}}.event-detail time{{display:block;color:var(--muted);font-size:11px;margin-top:4px}}.event-detail pre{{margin:7px 0 0}}.event-row{{width:100%;display:grid;grid-template-columns:24px 1fr auto auto;gap:7px;align-items:center;text-align:left;margin:5px 0;padding:7px;font-family:system-ui;font-size:12px}}.event-row span:first-child{{color:var(--muted)}}.event-row b{{overflow:hidden;text-overflow:ellipsis}}.event-row span:nth-child(3){{color:var(--muted);overflow:hidden;text-overflow:ellipsis}}.event-row .status{{font-size:10px}}.legend{{display:flex;gap:18px;flex-wrap:wrap;padding:10px 16px 14px;font-size:12px}}.legend span{{display:flex;gap:6px;align-items:center}}.dot{{width:9px;height:9px;border-radius:50%;display:inline-block;background:#64748b}}.dot.completed{{background:var(--green)}}.dot.failed{{background:var(--red)}}
.workflow-filter{{border:1px solid var(--border);border-radius:7px;background:var(--panel2);color:var(--text);padding:8px 10px;min-width:190px}}.workflow-filter summary{{cursor:pointer;color:var(--muted)}}.workflow-options{{display:grid;gap:6px;margin-top:8px;max-height:240px;overflow:auto}}.workflow-option{{display:flex;align-items:center;gap:7px;color:var(--text);font-size:12px}}
.pub-columns,.pub-row{{grid-template-columns:86px 110px minmax(130px,1fr) minmax(180px,1.6fr) 145px 145px}}.pub-row{{font-family:system-ui}}.pub-row code,.pub-row time,.pub-row small{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.pub-row time,.pub-row small{{color:var(--muted)}}.pub-row a{{color:var(--cyan);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
@media(max-width:900px){{.pub-columns,.pub-row{{grid-template-columns:80px 1fr auto}}.pub-columns .index-column:nth-child(4),.pub-columns .index-column:nth-child(5),.pub-columns .index-column:nth-child(6),.pub-row time,.pub-row small,.pub-row a{{display:none}}}}
@media(max-width:900px){{.explorer-grid{{grid-template-columns:1fr}}.inspector{{border-left:0;border-top:1px solid var(--border);max-height:none}}.hint{{width:100%;margin-left:0}}}}
@media(max-width:900px){{.index-columns,.index-row{{grid-template-columns:80px 1fr auto}}.index-columns .index-column:nth-child(4),.index-columns .index-column:nth-child(6),.index-columns .index-column:nth-child(7),.index-row time,.index-row span:last-of-type,.index-row small{{display:none}}.index-row code{{grid-column:2}}}}
</style></head><body><h1>Workflow run explorer</h1>
<p class="subtitle"><b>project</b> {_text(project_name)} · <b>repository</b> <code>{_text(project.repo)}</code></p>
<p class="subtitle">Interactive canvas built from persisted SQLite events. This replaces the static Mermaid-first view (the former static LangGraph flow); the graph structure remains available through the generated data.</p>
{_interactive_app(data)}
</body></html>"""
