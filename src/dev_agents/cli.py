"""Command-line interface for dev-agents workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dev_agents.config import (
    ConfigError,
    ReleaseCommsConfig,
    load_projects_config,
    select_project,
)
from dev_agents.context.repository import RepositoryError
from dev_agents.pr_fixer import serve_project
from dev_agents.runtime import StateRepository, state_database_path
from dev_agents.visualize import (
    WORKFLOW_NAMES,
    deploy_report_to_vercel,
    render_report,
    write_report,
)
from dev_agents.workflows.degodify import run_degodify
from dev_agents.workflows.inspection import inspect_project
from dev_agents.workflows.release_comms import run_release_comms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-agents")
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser("inspect", help="Inspect a configured repository read-only")
    inspect.add_argument("project", help="Configured project name")
    inspect.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    range_group = inspect.add_mutually_exclusive_group()
    range_group.add_argument(
        "--range", dest="commit_range", help="Git revision range, e.g. main..HEAD"
    )
    inspect.add_argument("--base", help="Base revision; requires --head")
    inspect.add_argument("--head", help="Head revision; requires --base")
    fixer = subcommands.add_parser("pr-fixer", help="Run PR remediation workflows")
    fixer_subcommands = fixer.add_subparsers(dest="pr_fixer_command", required=True)
    serve = fixer_subcommands.add_parser(
        "serve", help="Run the authenticated GitHub webhook listener"
    )
    serve.add_argument("project", help="Configured project name")
    serve.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    degodify = subcommands.add_parser("degodify", help="Run bounded god-file decomposition")
    degodify_subcommands = degodify.add_subparsers(dest="degodify_command", required=True)
    run = degodify_subcommands.add_parser("run", help="Plan or execute one decomposition")
    run.add_argument("project", help="Configured project name")
    run.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    run.add_argument("--base", default="staging")
    run.add_argument("--provider", default="codex")
    run.add_argument("--execute", action="store_true", help="Create branch, push, and open a PR")
    comms = subcommands.add_parser("release-comms", help="Run release communications workflows")
    comms_subcommands = comms.add_subparsers(dest="release_comms_command", required=True)
    evaluate = comms_subcommands.add_parser(
        "evaluate", help="Evaluate release changes and generate drafts"
    )
    evaluate.add_argument("project", help="Configured project name")
    evaluate.add_argument("promote_run_id", help="GitHub workflow run ID of promote-to-prod")
    evaluate.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    evaluate.add_argument("--publish", action="store_true", help="Publish approved destinations")
    evaluate.add_argument(
        "--local-generation",
        dest="local_generation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Generate evaluator/writer content in-repo instead of the target script",
    )
    evaluate.add_argument(
        "--dry-run",
        dest="dry_run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run without publishing (default: True unless --publish is given)",
    )
    export_reddit = comms_subcommands.add_parser(
        "export-reddit", help="Export formatted Reddit markdown for a release run"
    )
    export_reddit.add_argument("project", help="Configured project name")
    export_reddit.add_argument("promote_run_id", help="GitHub workflow run ID of promote-to-prod")
    export_reddit.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    export_reddit.add_argument(
        "--output", type=Path, help="Optional file path to save exported markdown"
    )
    sync_reddit = comms_subcommands.add_parser(
        "sync-reddit", help="Reconcile staged Reddit candidates with live Reddit posts"
    )
    sync_reddit.add_argument("project", help="Configured project name")
    sync_reddit.add_argument(
        "--run-id", dest="run_id", help="Optional specific promote run ID to reconcile"
    )
    sync_reddit.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    sync_reddit.add_argument(
        "--subreddit", help="Optional subreddit override (e.g. codexcryptica)"
    )
    visualize = subcommands.add_parser(
        "visualize", help="Render LangGraph flows and persisted run timelines as HTML"
    )
    visualize.add_argument("project", help="Configured project name")
    visualize.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    visualize.add_argument("--output", type=Path, default=Path("dev-agents-flow.html"))
    visualize.add_argument(
        "--workflow", choices=WORKFLOW_NAMES, help="Show only one workflow"
    )
    visualize.add_argument("--limit", type=int, default=50, help="Recent runs per state database")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "inspect":
        if bool(arguments.base) != bool(arguments.head):
            parser.error("--base and --head must be supplied together")
        commit_range = arguments.commit_range or (
            f"{arguments.base}..{arguments.head}" if arguments.base else None
        )
        try:
            config = load_projects_config(arguments.config)
            project = select_project(config, arguments.project)
            summary = inspect_project(arguments.config, arguments.project, commit_range)
            from dev_agents.visualize import schedule_report_refresh

            schedule_report_refresh(arguments.project, project)
        except (ConfigError, RepositoryError) as error:
            parser.exit(2, f"error: {error}\n")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if arguments.command == "pr-fixer" and arguments.pr_fixer_command == "serve":
        try:
            serve_project(arguments.config, arguments.project)
        except (ConfigError, RepositoryError) as error:
            parser.exit(2, f"error: {error}\n")
        except KeyboardInterrupt:
            return 0
    if arguments.command == "degodify" and arguments.degodify_command == "run":
        try:
            project = select_project(load_projects_config(arguments.config), arguments.project)
            result = run_degodify(
                project.repo,
                base_branch=arguments.base,
                provider=arguments.provider,
                dry_run=not arguments.execute,
            )
            from dev_agents.visualize import schedule_report_refresh

            schedule_report_refresh(arguments.project, project)
        except (ConfigError, RepositoryError, RuntimeError) as error:
            parser.exit(2, f"error: {error}\n")
        print(
            json.dumps(
                {
                    "candidate": result.candidate.relative_path if result.candidate else None,
                    "branch": result.branch,
                    "pullRequestUrl": result.pull_request_url,
                    "dryRun": result.dry_run,
                    "succeeded": result.succeeded,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.succeeded else 1
    if arguments.command == "release-comms" and arguments.release_comms_command == "evaluate":
        try:
            config = load_projects_config(arguments.config)
            project = select_project(config, arguments.project)
            # An explicit --dry-run always wins over --publish (fail closed);
            # otherwise --publish implies a live run.
            dry_run = arguments.dry_run if arguments.dry_run is not None else not arguments.publish
            if arguments.local_generation is not None:
                comms_config = project.release_comms or ReleaseCommsConfig()
                project = project.model_copy(
                    update={
                        "release_comms": comms_config.model_copy(
                            update={"local_generation": arguments.local_generation}
                        )
                    }
                )
            comms_result = run_release_comms(
                project,
                arguments.project,
                arguments.promote_run_id,
                dry_run=dry_run,
                publish_approved=arguments.publish,
            )
        except (ConfigError, RepositoryError, RuntimeError) as error:
            parser.exit(2, f"error: {error}\n")
        print(
            json.dumps(
                {
                    "promoteRunId": comms_result.promote_run_id,
                    "newSha": comms_result.new_sha,
                    "previousSha": comms_result.previous_sha,
                    "postworthy": comms_result.postworthy,
                    "drafts": comms_result.drafts,
                    "published": comms_result.published,
                    "completed": comms_result.completed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if comms_result.completed else 1
    if arguments.command == "release-comms" and arguments.release_comms_command == "export-reddit":
        try:
            config = load_projects_config(arguments.config)
            project = select_project(config, arguments.project)
            comms_config = project.release_comms or ReleaseCommsConfig()
            database_path = state_database_path(
                comms_config.state_path, project.repo / ".dev-agents/release-comms-state.db"
            )
            state_repo = StateRepository(
                database_path,
                project_name=arguments.project,
            )
            run = state_repo.get_run("release-comms", str(arguments.promote_run_id))
            if run is None:
                parser.exit(2, f"error: no release-comms run found for {arguments.promote_run_id}\n")
            drafts = run.metadata.get("drafts") if isinstance(run.metadata, dict) else {}
            discussions = (drafts or {}).get("github_discussions") or []
            if not discussions:
                parser.exit(2, f"error: no discussion/reddit drafts found for run {arguments.promote_run_id}\n")

            from dev_agents.workflows.reddit import export_reddit_markdown

            output_content = export_reddit_markdown(discussions, str(arguments.promote_run_id))
            if arguments.output:
                arguments.output.parent.mkdir(parents=True, exist_ok=True)
                arguments.output.write_text(output_content, encoding="utf-8")
                print(f"Exported Reddit drafts to {arguments.output}")
            else:
                print(output_content)
            return 0
        except (ConfigError, RepositoryError, RuntimeError) as error:
            parser.exit(2, f"error: {error}\n")
    if arguments.command == "release-comms" and arguments.release_comms_command == "sync-reddit":
        try:
            config = load_projects_config(arguments.config)
            project = select_project(config, arguments.project)
            from dev_agents.workflows.reddit import sync_reddit_status

            reconciled = sync_reddit_status(
                project=project,
                project_name=arguments.project,
                run_id=arguments.run_id,
                subreddit=arguments.subreddit,
            )
            print(
                json.dumps(
                    {
                        "reconciledCount": len(reconciled),
                        "reconciled": [
                            {
                                "runId": r.run_id,
                                "destination": r.destination,
                                "pageUrl": r.page_url,
                                "publicUrl": r.public_url,
                                "externalId": r.external_id,
                                "status": r.status,
                            }
                            for r in reconciled
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        except (ConfigError, RepositoryError, RuntimeError) as error:
            parser.exit(2, f"error: {error}\n")
    if arguments.command == "visualize":
        try:
            config = load_projects_config(arguments.config)
            project = select_project(config, arguments.project)
            report = render_report(
                arguments.project,
                project,
                workflow=arguments.workflow,
                limit=arguments.limit,
            )
            output = write_report(arguments.output, report)
            deploy_report_to_vercel(project, output)
        except (ConfigError, OSError, ValueError) as error:
            parser.exit(2, f"error: {error}\n")
        print(output.resolve())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
