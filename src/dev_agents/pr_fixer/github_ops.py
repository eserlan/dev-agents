"""GitHub CLI calls and PR feedback/report parsing.

Call sites for ``_run`` and ``repository_slug`` route through the ``dev_agents.pr_fixer``
package object (``_pkg``) rather than a plain local name. Tests monkeypatch these by their
dotted path on that package (e.g. ``monkeypatch.setattr("dev_agents.pr_fixer._run", ...)``),
which only affects attribute lookups made *through* the package object at call time --
not a name a submodule imported by value. Routing through ``_pkg`` keeps that patching
working the same way it did when everything lived in one module.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import dev_agents.pr_fixer as _pkg
from dev_agents.config import PrFixerConfig

from ._shared import REVIEW_REPORT_BEGIN, REVIEW_REPORT_END


def _run(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            args, cwd=repo, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout:g}s") from error
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _feedback(repo: Path, number: int) -> tuple[dict[str, Any], list[str]]:
    meta = json.loads(
        _pkg._run(
            repo,
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "number,headRefName,headRefOid,baseRefName,state,isDraft,labels,mergeable,mergeStateStatus,reviewDecision,reviews,commits,autoMergeRequest,url,body",
        )
    )
    slug = _pkg.repository_slug(repo)
    owner, name = slug.split("/", 1)
    threads_query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}"
    threads = json.loads(
        _pkg._run(
            repo,
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={threads_query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        )
    )
    checks = _pull_request_checks(repo, number)
    for check in checks:
        if check.get("bucket") == "fail" or check.get("state") == "FAILURE":
            run_id = re.search(r"/runs/(\d+)", check.get("link", ""))
            if run_id:
                check["failureDetails"] = _pkg._run(
                    repo, "gh", "run", "view", run_id.group(1), "--log-failed", check=False
                )[-12000:]
    meta["checks"] = checks
    nodes = (
        threads.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    keys = [
        f"comment:{item['comments']['nodes'][0]['databaseId']}"
        for item in nodes
        if not item.get("isResolved") and item.get("comments", {}).get("nodes")
    ]
    keys += [
        f"review:{review['id']}"
        for review in meta.get("reviews", [])
        if review.get("state") == "CHANGES_REQUESTED"
    ]
    keys += [
        f"check:{meta['headRefOid']}:{item.get('workflow', '')}:{item['name']}:{item['state']}"
        for item in checks
        if item.get("bucket") == "fail" or item.get("state") == "FAILURE"
    ]
    if meta.get("mergeable") == "CONFLICTING" or meta.get("mergeStateStatus") == "DIRTY":
        keys.append(f"conflict:{meta['headRefOid']}")
    return meta, keys


def _is_issue_fix_pr(meta: dict[str, Any]) -> bool:
    """Identify PRs created by the label-driven issue fixer."""
    return bool(re.search(r"<!--\s*dev-agents:issue-fix\s+issue=\d+\b", str(meta.get("body", ""))))


def _pull_request_checks(repo: Path, number: int) -> list[dict[str, Any]]:
    """Read PR checks, allowing GitHub's normal pre-checks empty state."""
    try:
        raw = _pkg._run(
            repo, "gh", "pr", "checks", str(number), "--json", "name,state,bucket,workflow,link"
        )
    except RuntimeError as error:
        # gh exits non-zero when a PR has not received its first check yet. This
        # is expected during webhook/reconciliation races, not a daemon error.
        if str(error).startswith("no checks reported on the "):
            return []
        raise
    checks = json.loads(raw)
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
        raise RuntimeError("gh pr checks returned an invalid JSON payload")
    return checks


def _normalise_review_report(value: Any) -> dict[str, Any] | None:
    """Validate the review JSON without retaining unbounded agent output."""
    if not isinstance(value, dict) or value.get("verdict") not in {"clean", "findings"}:
        return None
    findings = value.get("findings")
    categories = value.get("categories_checked")
    validation = value.get("validation")
    fixes = value.get("fixes")
    required_finding_fields = {"severity", "category", "location", "impact", "remediation"}
    if not isinstance(findings, list) or not all(
        isinstance(item, dict)
        and required_finding_fields <= set(item)
        and all(isinstance(item[field], str) for field in required_finding_fields)
        for item in findings
    ):
        return None
    if not isinstance(categories, list) or not all(isinstance(item, str) for item in categories):
        return None
    if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
        return None
    if not isinstance(fixes, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("location"), str)
        and isinstance(item.get("summary"), str)
        for item in fixes
    ):
        return None
    return {
        "verdict": value["verdict"],
        "findings": [
            {field: item[field] for field in required_finding_fields}
            for item in findings
        ],
        "categories_checked": categories,
        "validation": validation,
        "fixes": [
            {"location": item["location"], "summary": item["summary"]}
            for item in fixes
        ],
    }


def _agent_report(log_path: Path) -> dict[str, Any]:
    """Extract the bounded human and structured review result from an agent log."""
    try:
        output = log_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if REVIEW_REPORT_BEGIN not in output or REVIEW_REPORT_END not in output:
        return {}
    report = output.rsplit(REVIEW_REPORT_BEGIN, 1)[1].split(REVIEW_REPORT_END, 1)[0]
    values: dict[str, Any] = {}
    structured_json: str | None = None
    for line in report.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized_key = key.strip().upper()
        if normalized_key in {"FINDINGS", "FIXES"}:
            values[normalized_key.lower()] = value.strip()
        elif normalized_key == "REPORT_JSON":
            structured_json = value.strip()
    if structured_json is None:
        values["report_valid"] = False
        values["report_error"] = "REPORT_JSON was not emitted"
        return values
    try:
        parsed = json.loads(structured_json)
    except json.JSONDecodeError:
        values["report_valid"] = False
        values["report_error"] = "REPORT_JSON was not valid JSON"
        return values
    normalized = _normalise_review_report(parsed)
    if normalized is None:
        values["report_valid"] = False
        values["report_error"] = "REPORT_JSON did not match the review schema"
        return values
    values["review_report"] = normalized
    values["report_valid"] = True
    return values


def _findings_summary(report: dict[str, Any], default: str) -> str:
    """Render structured findings compactly for a PR comment."""
    summary = report.get("findings")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    if not isinstance(summary, list) or not summary:
        return default
    items = [
        f"{item['severity']} `{item['location']}`: {item['impact']}"
        for item in summary[:5]
        if isinstance(item, dict)
        and all(isinstance(item.get(field), str) for field in ("severity", "location", "impact"))
    ]
    return "; ".join(items) if items else default


def _external_agent_commit(meta: dict[str, Any], config: PrFixerConfig) -> str | None:
    """Return the latest commit author when an external agent owns the PR head."""
    if not config.pause_on_external_agent_commits:
        return None
    configured = {
        login.strip().lower() for login in config.external_agent_logins if login.strip()
    }
    commits = meta.get("commits")
    if not configured or not isinstance(commits, list) or not commits:
        return None
    latest = commits[-1]
    if not isinstance(latest, dict):
        return None
    authors = latest.get("authors")
    if not isinstance(authors, list):
        return None
    for author in authors:
        if not isinstance(author, dict):
            continue
        login = str(author.get("login") or "").strip()
        if login.lower() in configured:
            return login
    return None


def _has_copilot_review(meta: dict[str, Any]) -> bool:
    """Return whether GitHub has a submitted review from a Copilot account."""
    for review in meta.get("reviews", []):
        if not isinstance(review, dict):
            continue
        author = review.get("author")
        login = author.get("login", "") if isinstance(author, dict) else str(author or "")
        state = str(review.get("state", "")).upper()
        if "copilot" in login.lower() and state not in {"PENDING", "DISMISSED"}:
            return True
    return False


def _review_is_due(meta: dict[str, Any], config: PrFixerConfig) -> bool:
    """Check the settled-PR conditions for an internal review pass."""
    if (
        meta.get("state") != "OPEN"
        or meta.get("baseRefName") != config.base_branch
        or meta.get("isDraft")
        or meta.get("mergeable") != "MERGEABLE"
        or meta.get("reviewDecision") == "CHANGES_REQUESTED"
    ):
        return False
    checks = meta.get("checks", [])
    if not isinstance(checks, list):
        return False
    return not any(
        item.get("bucket") in {"fail", "pending"}
        or item.get("state")
        in {"FAILURE", "ERROR", "CANCELLED", "PENDING", "IN_PROGRESS", "QUEUED"}
        for item in checks
        if isinstance(item, dict)
    )
