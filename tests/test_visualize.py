import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_agents.config import PrFixerConfig, ProjectConfig, ReleaseCommsConfig
from dev_agents.runtime import StateRepository
from dev_agents.visualize import (
    default_report_path,
    deploy_report_to_vercel,
    refresh_report_run_url,
    render_report,
    report_run_url,
    schedule_report_refresh,
    workflow_graph,
    workflow_mermaid,
)


def test_report_run_url_focuses_workflow_and_run() -> None:
    project = ProjectConfig(
        repo=Path("/tmp/repo"),
        report_vercel_project="flow-report",
        report_vercel_alias="dev-agents.example.com",
    )

    assert report_run_url(project, "pr-review", "pr-review-3056-head/abc") == (
        "https://dev-agents.example.com?workflow=pr-review&run=pr-review-3056-head%2Fabc"
    )


def test_report_run_url_uses_vercel_project_when_alias_is_missing() -> None:
    project = ProjectConfig(repo=Path("/tmp/repo"), report_vercel_project="flow-report")

    assert report_run_url(project, "pr-review", "run-1") == (
        "https://flow-report.vercel.app?workflow=pr-review&run=run-1"
    )


def test_report_can_deploy_to_vercel_and_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>flow</html>", encoding="utf-8")
    project = ProjectConfig(
        repo=tmp_path,
        report_vercel_project="flow-report",
        report_vercel_scope="team-slug",
        report_vercel_alias="flow.example.com",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Result:
        returncode = 0
        stdout = "https://flow-report.vercel.app\n"
        stderr = ""

    monkeypatch.setenv("VERCEL_TOKEN", "secret")
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (calls.append((tuple(command), kwargs)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) == "https://flow-report.vercel.app"

    assert len(calls) == 2
    deploy_command, deploy_kwargs = calls[0]
    assert deploy_command[:2] == ("/usr/bin/vercel", "deploy")
    assert "--name" in deploy_command
    assert "flow-report" in deploy_command
    assert deploy_kwargs["timeout"] == 120
    assert "index.html" not in deploy_command
    assert calls[1][0][:4] == (
        "/usr/bin/vercel",
        "alias",
        "set",
        "https://flow-report.vercel.app",
    )


def test_report_deploy_can_use_logged_in_cli_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>flow</html>", encoding="utf-8")
    project = ProjectConfig(repo=tmp_path, report_vercel_project="flow-report")

    class Result:
        returncode = 0
        stdout = "https://flow-report.vercel.app\n"
        stderr = ""

    commands: list[tuple[str, ...]] = []
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (commands.append(tuple(command)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) == "https://flow-report.vercel.app"
    assert "--token" not in commands[0]


def test_report_deploy_extracts_url_from_vercel_json_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>flow</html>", encoding="utf-8")
    project = ProjectConfig(repo=tmp_path, report_vercel_project="flow-report")

    class Result:
        returncode = 0
        stdout = '{"deployment": {"url": "https://flow-report-preview.vercel.app"}}\n'
        stderr = ""

    commands: list[tuple[str, ...]] = []
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (commands.append(tuple(command)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) == "https://flow-report-preview.vercel.app"


def test_report_deploy_reuses_unchanged_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>flow</html>", encoding="utf-8")
    project = ProjectConfig(repo=tmp_path, report_vercel_project="flow-report")

    class Result:
        returncode = 0
        stdout = "https://flow-report-preview.vercel.app\n"
        stderr = ""

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (calls.append(tuple(command)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) == "https://flow-report-preview.vercel.app"
    assert deploy_report_to_vercel(project, report) == "https://flow-report-preview.vercel.app"
    assert len(calls) == 1


def test_report_deploy_limits_changed_content_uploads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>first</html>", encoding="utf-8")
    project = ProjectConfig(
        repo=tmp_path,
        report_vercel_project="flow-report",
        report_vercel_min_interval_seconds=0,
        report_vercel_max_deployments_24h=1,
    )

    class Result:
        returncode = 0
        stdout = "https://flow-report-preview.vercel.app\n"
        stderr = ""

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (calls.append(tuple(command)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) == "https://flow-report-preview.vercel.app"
    report.write_text("<html>second</html>", encoding="utf-8")
    assert deploy_report_to_vercel(project, report) is None
    assert len(calls) == 1


def test_report_deploy_backs_off_after_vercel_daily_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "flow.html"
    report.write_text("<html>flow</html>", encoding="utf-8")
    project = ProjectConfig(
        repo=tmp_path,
        report_vercel_project="flow-report",
        report_vercel_min_interval_seconds=0,
    )

    class Result:
        returncode = 1
        stdout = ""
        stderr = "Resource is limited (api-deployments-free-per-day)"

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("dev_agents.visualize.shutil.which", lambda name: "/usr/bin/vercel")
    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.run",
        lambda command, **kwargs: (calls.append(tuple(command)) or Result()),
    )

    assert deploy_report_to_vercel(project, report) is None
    assert deploy_report_to_vercel(project, report) is None
    assert len(calls) == 1
    state = (tmp_path / ".flow.html.vercel-deploy.json").read_text(encoding="utf-8")
    assert "blocked_until" in state


def test_report_contains_graph_and_run_timeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    state = StateRepository(database, "demo", repo)
    state.claim_run("degodify", "run-1", metadata={"candidate": "src/large.ts"})
    state.record_event("degodify", "run-1", "prepare_candidate", "candidate_selected")
    state.record_publication(
        "degodify",
        "run-1",
        "discord",
        destination="main-community",
        page_url="https://example.com/a",
        public_url="https://discord.example/message/1",
    )
    state.complete_run("degodify", "run-1")

    project = ProjectConfig(
        repo=repo,
        pr_fixer=PrFixerConfig(state_path=database),
        release_comms=ReleaseCommsConfig(state_path=tmp_path / "release.db"),
    )
    report = render_report("demo", project)

    assert "static LangGraph flow" in report
    assert "run-1" in report
    assert "candidate_selected" in report
    assert "src/large.ts" in report
    assert "release-comms" in report
    assert "canvas" in report
    assert "Click nodes or events" in report
    assert "discord.example/message/1" in report
    assert "index-columns" in report
    assert 'aria-label="Sort by ${column.label}"' in report
    assert 'id="index-workflow-options"' in report
    assert 'type="checkbox"' in report
    assert "webhook|reconcile" in report


def test_workflow_graph_contains_clickable_topology() -> None:
    graph = workflow_graph("inspection")

    assert {node["id"] for node in graph["nodes"]} >= {
        "__start__",
        "load_configuration",
        "produce_summary",
        "__end__",
    }
    assert {edge["target"] for edge in graph["edges"]} >= {"load_configuration", "__end__"}


def test_release_comms_graph_branches_to_publication_channels() -> None:
    graph = workflow_graph("release-comms")

    channel_ids = {node["id"] for node in graph["nodes"] if node["kind"] == "channel"}
    assert channel_ids == {
        "channel:bluesky",
        "channel:instagram",
        "channel:discord",
        "channel:github_discussions",
        "channel:x",
    }
    assert all(
        {"source": "publish_destinations", "target": channel, "conditional": False}
        in graph["edges"]
        for channel in channel_ids
    )
    assert {"id": "schedule_publication", "label": "schedule_publication", "kind": "node"} in graph[
        "nodes"
    ]
    assert {
        "source": "publish_destinations",
        "target": "schedule_publication",
        "conditional": True,
    } in graph["edges"]


def test_report_refresh_is_detached_and_uses_project_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(repo=repo, visualization_path=tmp_path / "flow.html")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    monkeypatch.setattr(
        "dev_agents.visualize.subprocess.Popen",
        lambda command, **kwargs: calls.append((tuple(command), kwargs)),
    )

    schedule_report_refresh("demo", project)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:3] == ("-m", "dev_agents.visualize_refresh")
    assert str(tmp_path / "flow.html") in command
    assert kwargs["start_new_session"] is True
    assert default_report_path("demo").name == "dev-agents-flow.html"


def test_refresh_report_run_url_deploys_a_fresh_snapshot_before_linking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        repo=repo,
        visualization_path=tmp_path / "flow.html",
        report_vercel_project="dev-agents-reports",
    )
    report = tmp_path / "flow.html"

    monkeypatch.setattr(
        "dev_agents.visualize.refresh_project_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        "dev_agents.visualize.deploy_report_to_vercel",
        lambda _project, _report: "https://dev-agents-reports-new.vercel.app",
    )

    assert refresh_report_run_url("demo", project, "pr-review", "run/1") == (
        "https://dev-agents-reports-new.vercel.app?workflow=pr-review&run=run%2F1"
    )


def test_index_includes_daemon_only_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    state = StateRepository(database, "demo", repo)
    for workflow, run_id in (("github-webhook", "delivery:abc"), ("pr-auto-merge", "pr-42-head"), ("pr-reconcile", "reconcile-1")):
        state.claim_run(workflow, run_id, metadata={"source": "daemon"})
        state.record_event(workflow, run_id, "daemon", "started")
        state.complete_run(workflow, run_id)

    project = ProjectConfig(repo=repo, pr_fixer=PrFixerConfig(state_path=database))
    report = render_report("demo", project)

    assert "Daemon run index" in report
    assert "delivery:abc" in report
    assert "pr-42-head" in report
    assert "reconcile-1" in report
    assert "All workflows" in report


def test_report_collapses_hour_old_runs_and_omits_day_old_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "state.db"
    state = StateRepository(database, "demo", repo)
    for run_id in ("recent", "collapsed", "deleted"):
        state.claim_run("pr-reconcile", run_id, metadata={"source": "daemon"})
        state.record_event("pr-reconcile", run_id, "daemon", "started")
        state.complete_run("pr-reconcile", run_id)

    now = datetime.now(UTC)
    timestamps = {
        "recent": now - timedelta(minutes=10),
        "collapsed": now - timedelta(hours=2),
        "deleted": now - timedelta(days=2),
    }
    with sqlite3.connect(database) as connection:
        for run_id, timestamp in timestamps.items():
            value = timestamp.isoformat()
            connection.execute(
                "UPDATE runs SET created_at = ?, started_at = ?, completed_at = ?, updated_at = ? "
                "WHERE project_name = ? AND run_id = ?",
                (value, value, value, value, "demo", run_id),
            )

    project = ProjectConfig(repo=repo, pr_fixer=PrFixerConfig(state_path=database))
    report = render_report("demo", project)

    assert '"run_id": "recent"' in report
    assert '"run_id": "collapsed"' in report
    assert '"run_id": "deleted"' not in report
    assert "Older than 1 hour" in report
    assert '"hideAfterSeconds": 3600' in report
    assert '"deleteAfterSeconds": 86400' in report


def test_workflow_mermaid_rejects_unknown_workflow() -> None:
    assert "collect_feedback" in workflow_mermaid("pr-fixer")
    assert "collect_issue" in workflow_mermaid("issue-fixer")
    with pytest.raises(ValueError, match="unknown workflow"):
        workflow_mermaid("missing")
