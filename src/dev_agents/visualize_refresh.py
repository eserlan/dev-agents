"""Detached worker used to refresh a project's local workflow report.

Every finished job schedules one of these workers. Refreshes for the same report are coalesced
so they cannot pile up on a busy machine: a worker first records a pending request, then only
the worker holding the report's refresh lock does the work, repeating while new requests
arrive. A worker that finds the lock taken exits immediately; the holder picks up its request.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from collections.abc import Callable
from pathlib import Path

from dev_agents.config import ProjectConfig
from dev_agents.visualize import deploy_report_to_vercel, refresh_project_report


def _coalesced(output: Path, refresh: Callable[[], None]) -> None:
    """Run ``refresh`` until no request is pending, unless another worker already owns it."""
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(f".{output.name}.refresh-pending")
    lock_path = output.with_name(f".{output.name}.refresh.lock")
    pending.touch()
    # Re-check after releasing the lock: a request recorded just before release would
    # otherwise be lost, because its worker saw the lock held and exited.
    while pending.exists():
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                while pending.exists():
                    pending.unlink(missing_ok=True)
                    refresh()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-agents-report-refresh")
    parser.add_argument("project", help="Configured project name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-json", required=True)
    parser.add_argument("--limit", type=int, default=50)
    arguments = parser.parse_args(argv)
    try:
        project = ProjectConfig.model_validate(json.loads(arguments.project_json))

        def refresh() -> None:
            report = refresh_project_report(
                arguments.project,
                project,
                output=arguments.output,
                limit=arguments.limit,
            )
            deploy_report_to_vercel(project, report)

        _coalesced(arguments.output.expanduser(), refresh)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"report refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
