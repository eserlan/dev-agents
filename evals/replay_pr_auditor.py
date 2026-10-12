"""Audit original historical PR snapshots without fixing them or publishing comments."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dev_agents.config import load_projects_config, select_project
from dev_agents.pr_fixer.audit import (
    audit_json_schema,
    compare_reference_comments,
    parse_audit_report,
)
from dev_agents.pr_fixer.prompts import _review_prompt
from dev_agents.runtime import run_agent
from dev_agents.runtime.github import gh_json


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "pr-audit/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = select_project(load_projects_config(args.config), args.project)
    settings = project.pr_fixer
    if settings is None:
        parser.error("project must configure pr_fixer")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text())

    def replay(case: dict[str, Any]) -> dict[str, Any]:
        number = case["pr"]
        pr = gh_json(project.repo, "api", f"repos/{project.github}/pulls/{number}")
        reviews = gh_json(project.repo, "api", f"repos/{project.github}/pulls/{number}/reviews", "--paginate", "--slurp")
        reference = next(review for page in reviews for review in page if "copilot" in review["user"]["login"].lower())
        head = reference["commit_id"]
        merge = gh_json(project.repo, "api", f"repos/{project.github}/commits/{pr['merge_commit_sha']}")
        before_merge = merge["parents"][0]["sha"]
        with tempfile.TemporaryDirectory(prefix=f"dev-agents-audit-{number}-") as temporary:
            checkout = Path(temporary) / "repo"
            subprocess.run(["git", "clone", "--shared", "--no-checkout", str(project.repo), str(checkout)], check=True, capture_output=True)
            git(checkout, "remote", "set-url", "origin", git(project.repo, "remote", "get-url", "origin"))
            git(checkout, "fetch", "origin", head, before_merge, "--no-tags")
            base = git(checkout, "merge-base", head, before_merge)
            git(checkout, "update-ref", f"refs/remotes/origin/{settings.base_branch}", base)
            git(checkout, "checkout", "--detach", head)
            diff = git(checkout, "diff", f"{base}...HEAD")
            (args.output / f"pr-{number}.diff").write_text(diff)
            prompt = _review_prompt(checkout, number, {"headRefOid": head, "headRefName": "historical-snapshot", "url": pr["html_url"], "body": pr.get("body", "")}, settings.base_branch, [], run_id=f"replay-pr-{number}-{head}")
            prompt += "\nReplay isolation: use only this checked-out snapshot and bundled guidance. Do not retrieve GitHub review comments, later issues, newer source or PR discussion. The original reviewer feedback is withheld for evaluation."
            (args.output / f"pr-{number}.prompt.txt").write_text(prompt)
            schema = args.output / f"pr-{number}.schema.json"
            schema.write_text(audit_json_schema())
            output = args.output / f"pr-{number}.audit.json"
            result = run_agent("codex", prompt, cwd=checkout, log_path=args.output / f"pr-{number}.log", timeout_seconds=settings.timeout_minutes * 60, model=settings.model, reasoning_effort=settings.reasoning_effort, read_only=True, last_message_path=output, output_schema_path=schema)
            if result.returncode != 0 or result.timed_out:
                raise RuntimeError("read-only auditor execution failed")
            if git(checkout, "status", "--porcelain") or git(checkout, "rev-parse", "HEAD") != head:
                raise RuntimeError("auditor mutated the replay snapshot")
            audit = parse_audit_report(output, initial=True)
        # Reference review data is compared only after the independent auditor has finished.
        pages = gh_json(project.repo, "api", f"repos/{project.github}/pulls/{number}/comments", "--paginate", "--slurp")
        comparison = compare_reference_comments(audit.model_dump(), [c for page in pages for c in page], head)
        (args.output / f"pr-{number}.comparison.json").write_text(json.dumps(comparison, indent=2))
        matched = any(
            re.search(case["location"], finding.location, re.IGNORECASE)
            and re.search(case["impact"], f"{finding.evidence} {finding.impact} {finding.remediation}", re.IGNORECASE)
            for finding in audit.findings
        )
        return {"pr": number, "head_sha": head, "base_sha": base, "verdict": audit.verdict, "findings_count": len(audit.findings), "expected_defect_detected": matched, "status": "passed" if matched else "failed", "copilot_overlap": comparison}

    def safe_replay(case: dict[str, Any]) -> dict[str, Any]:
        try:
            return replay(case)
        except Exception as error:  # noqa: BLE001 - retain independent results for every benchmark
            return {"pr": case["pr"], "status": "failed", "reason": str(error)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(safe_replay, cases))
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    if any(result["status"] == "failed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
