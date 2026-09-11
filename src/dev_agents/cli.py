"""Command-line interface for dev-agents workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dev_agents.config import ConfigError
from dev_agents.context.repository import RepositoryError
from dev_agents.pr_fixer import serve_project
from dev_agents.workflows.inspection import inspect_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-agents")
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser("inspect", help="Inspect a configured repository read-only")
    inspect.add_argument("project", help="Configured project name")
    inspect.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    range_group = inspect.add_mutually_exclusive_group()
    range_group.add_argument("--range", dest="commit_range", help="Git revision range, e.g. main..HEAD")
    inspect.add_argument("--base", help="Base revision; requires --head")
    inspect.add_argument("--head", help="Head revision; requires --base")
    fixer = subcommands.add_parser("pr-fixer", help="Run PR remediation workflows")
    fixer_subcommands = fixer.add_subparsers(dest="pr_fixer_command", required=True)
    serve = fixer_subcommands.add_parser("serve", help="Run the authenticated GitHub webhook listener")
    serve.add_argument("project", help="Configured project name")
    serve.add_argument("--config", type=Path, default=Path("config/projects.yaml"))

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
            summary = inspect_project(arguments.config, arguments.project, commit_range)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
