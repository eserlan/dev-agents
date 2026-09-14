"""Detached worker used to refresh a project's local workflow report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dev_agents.config import ProjectConfig
from dev_agents.visualize import deploy_report_to_vercel, refresh_project_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-agents-report-refresh")
    parser.add_argument("project", help="Configured project name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-json", required=True)
    parser.add_argument("--limit", type=int, default=50)
    arguments = parser.parse_args(argv)
    try:
        project = ProjectConfig.model_validate(json.loads(arguments.project_json))
        report = refresh_project_report(
            arguments.project,
            project,
            output=arguments.output,
            limit=arguments.limit,
        )
        deploy_report_to_vercel(project, report)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"report refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
