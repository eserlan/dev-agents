"""Read-only AI replay of the existing reviewer; never publishes issues or edits articles."""

from __future__ import annotations

import argparse
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dev_agents.config import load_projects_config, select_project
from dev_agents.copy_review import _parse_assessment, _prompt
from dev_agents.runtime import run_agent
from dev_agents.runtime.github import gh_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "answer-quality/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+", help="Run only the named manifest cases")
    args = parser.parse_args()
    project = select_project(load_projects_config(args.config), args.project)
    settings = project.copy_review
    if settings is None:
        parser.error("project must configure copy_review")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text())
    if args.only:
        cases = [case for case in cases if case["name"] in args.only]
        if len(cases) != len(set(args.only)):
            parser.error("--only contains a case absent from the manifest")

    def replay(case: dict[str, Any]) -> dict[str, Any]:
        name = case["name"]
        correction = None
        if "fixture" in case:
            source = (args.cases.parent / case["fixture"]).read_text()
            revision = f"fixture:{name}"
            issue = {"body": case["brief"]}
            number, url, body = 0, "", ""
        else:
            number = case["pr"]
            pr = gh_json(project.repo, "api", f"repos/{project.github}/pulls/{number}")
            if not pr.get("merged"):
                return {"name": name, "status": "skipped", "reason": "positive correction not merged yet"}
            revision, url, body = pr["merge_commit_sha"], pr["html_url"], pr["body"] or ""
            content = gh_json(project.repo, "api", f"repos/{project.github}/contents/{case['path']}?ref={revision}")
            source = base64.b64decode(content["content"]).decode()
            issue = gh_json(project.repo, "issue", "view", str(case["issue"]), "--json", "number,title,body,url")
            correction = gh_json(project.repo, "issue", "view", str(case["correction_issue"]), "--json", "number,title,body,url")
        inputs = {"revision": revision, "source": source, "issue": issue, "correction_issue": correction}
        (args.output / f"{name}.input.json").write_text(json.dumps(inputs, indent=2))
        output = args.output / f"{name}.assessment.json"
        prompt = _prompt(number, url, source, instructions="Use British English. Assess only material reader outcomes; this is a read-only replay.", merge_sha=revision, pr_body=body, original_issue=issue, existing_issues=[], correction_issue=correction, correction_verification=correction is not None)
        prompt += "\nReplay constraint: assess only the supplied snapshot and briefs. Do not inspect local repository files, GitHub issues or PRs, neighbours or live pages. State these unavailable checks as limitations. This prevents later critiques from contaminating the benchmark."
        result = run_agent(
            "codex", prompt,
            cwd=project.repo, log_path=args.output / f"{name}.log",
            timeout_seconds=settings.timeout_minutes * 60,
            model=settings.model, reasoning_effort=settings.reasoning_effort,
            last_message_path=output,
        )
        if result.returncode != 0 or result.timed_out:
            return {"name": name, "status": "failed", "reason": "agent execution failed"}
        report = _parse_assessment(output.read_text(), require_coherence=True, correction_verification=correction is not None)
        failed_scopes = {check.scope for check in report.coherence_checks if check.status == "blocking"}
        passed = set(case.get("required_blocking_scopes", [])).issubset(failed_scopes)
        for category, severities in case.get("required_findings", {}).items():
            passed = passed and any(
                category in finding.category.lower() and finding.severity in severities
                for finding in report.findings
            )
        passed = passed and ("expected_verdict" not in case or report.verdict == case["expected_verdict"])
        return {"name": name, "status": "passed" if passed else "failed", "verdict": report.verdict, "blocking_scopes": sorted(failed_scopes)}

    def safe_replay(case: dict[str, Any]) -> dict[str, Any]:
        try:
            return replay(case)
        except Exception as error:  # noqa: BLE001 - preserve a result for every independent case
            return {"name": case["name"], "status": "failed", "reason": str(error)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(safe_replay, cases))
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    if any(result["status"] == "failed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
