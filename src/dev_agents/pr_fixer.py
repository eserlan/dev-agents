"""Webhook-driven, project-configured pull-request remediation service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from dev_agents.config import (
    ConfigError,
    PrFixerConfig,
    ProjectConfig,
    load_projects_config,
    select_project,
)
from dev_agents.context.instructions import discover_instructions
from dev_agents.issue_fixer import IssueFixerService
from dev_agents.runtime import (
    SqliteStateStore,
    StateRepository,
    isolated_worktree,
    legacy_json_path,
    remove_older_than,
    repository_slug,
    run_agent,
    run_with_fallback,
    state_database_path,
)
from dev_agents.workflows.degodify import DegodifyFile, run_degodify
from dev_agents.workflows.release_comms import run_release_comms

SHARED_PR_FIX_SKILL = Path(__file__).resolve().parents[2] / "skills/pr-fix/SKILL.md"
PR_FIX_PROVIDER = "codex"

_ACTIONS = {
    "pull_request": {"opened", "reopened", "synchronize", "ready_for_review"},
    "pull_request_review": {"submitted", "edited"},
    "pull_request_review_comment": {"created", "edited"},
    "check_run": {"completed"},
    "issues": {"opened", "reopened", "labeled", "unlabeled", "edited"},
}
MAX_BODY_BYTES = 1_000_000
REVIEW_REPORT_BEGIN = "DEV_AGENTS_REVIEW_REPORT_BEGIN"
REVIEW_REPORT_END = "DEV_AGENTS_REVIEW_REPORT_END"
REVIEW_SKILL_CANDIDATES = (
    ".agent/skills/codex-review/SKILL.md",
    ".codex/skills/codex-review/SKILL.md",
    ".claude/skills/codex-review/SKILL.md",
    ".agents/skills/codex-review/SKILL.md",
)
MAX_INTERNAL_REVIEW_ROUNDS = 2


def _log(message: str) -> None:
    print(f"[pr-fixer] {message}", flush=True)


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


def _state_path(config: PrFixerConfig, project: str) -> Path:
    return state_database_path(
        config.state_path,
        Path.home() / ".local/state/dev-agents" / project / "pr-fixer-state.db",
    )


def _load_state(path: Path) -> dict[str, Any]:
    database_path = state_database_path(path, path)
    value = SqliteStateStore(
        database_path,
        "pr-fixer",
        {"version": 1, "pullRequests": {}},
        legacy_json_path(path, database_path),
    ).load()
    if value.get("version") == 1 and isinstance(value.get("pullRequests"), dict):
        return value
    # Migrate the original {"<pr>": ["comment:..."]} shape.
    migrated = {"version": 1, "pullRequests": value}
    SqliteStateStore(database_path, "pr-fixer", {}).save(migrated)
    return migrated


def _save_state(path: Path, state: dict[str, list[str]]) -> None:
    SqliteStateStore(state_database_path(path, path), "pr-fixer", {}).save(state)


def _cleanup_artifacts(project_name: str, config: PrFixerConfig) -> None:
    """Bound completed logs and stale temporary worktree directories."""
    log_dir = (
        config.log_dir or Path.home() / ".local/state/dev-agents" / project_name / "logs"
    ).expanduser()
    worktree_root = (config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser()
    removed_logs = remove_older_than(log_dir, "pr-*.log", config.log_retention_days * 86400)
    removed_worktrees = remove_older_than(
        worktree_root, "pr-*", config.worktree_retention_days * 86400, directories=True
    )
    if removed_logs or removed_worktrees:
        _log(f"cleanup logs={removed_logs} worktrees={removed_worktrees}")


def _feedback(repo: Path, number: int) -> tuple[dict[str, Any], list[str]]:
    meta = json.loads(
        _run(
            repo,
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "number,headRefName,headRefOid,baseRefName,state,isDraft,labels,mergeable,mergeStateStatus,reviewDecision,reviews,commits,autoMergeRequest,url,body",
        )
    )
    slug = repository_slug(repo)
    owner, name = slug.split("/", 1)
    threads_query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}"
    threads = json.loads(
        _run(
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
                check["failureDetails"] = _run(
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
        raw = _run(
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


def _review_skill(repo: Path) -> tuple[str, str]:
    """Load the canonical review skill, with compatibility fallbacks for older repos."""
    for relative_path in REVIEW_SKILL_CANDIDATES:
        path = repo / relative_path
        if path.is_file():
            return relative_path, path.read_text(encoding="utf-8")
    return REVIEW_SKILL_CANDIDATES[0], ""


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


def _publish_pr_run_comment(
    repo: Path, number: int, run_id: str, phase: str, body: str
) -> bool:
    """Create or update the single lifecycle comment for a PR run."""
    try:
        slug = repository_slug(repo)
        raw = _run(
            repo,
            "gh",
            "api",
            f"repos/{slug}/issues/{number}/comments",
            "--paginate",
            "--slurp",
        )
        pages = json.loads(raw) if raw else []
        comments: list[dict[str, Any]] = []
        for page in pages if isinstance(pages, list) else []:
            if isinstance(page, list):
                comments.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict):
                comments.append(page)
        if phase.startswith("review"):
            markers = [
                f"<!-- dev-agents:pr-review run={run_id} -->",
                # Migrate a lifecycle comment created by an older daemon version.
                *(
                    f"<!-- dev-agents:pr-{legacy_phase} run={run_id} -->"
                    for legacy_phase in (
                        "review-started",
                        "review-findings",
                        "review-fixes-started",
                    )
                ),
            ]
        else:
            markers = [f"<!-- dev-agents:pr-{phase} run={run_id} -->"]
        existing = next(
            (
                comment
                for marker in markers
                for comment in comments
                if marker in str(comment.get("body", ""))
            ),
            None,
        )
        if existing and existing.get("id") is not None:
            _run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/comments/{existing['id']}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            )
        else:
            _run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/{number}/comments",
                "-f",
                f"body={body}",
            )
    except (RuntimeError, json.JSONDecodeError) as error:
        _log(f"run-comment-failed pr={number} phase={phase} error={error}")
        return False
    return True


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


def _external_agent_pause_body(
    number: int, head_sha: str, author: str, resume_label: str
) -> str:
    return f"""<!-- dev-agents:pr-external-agent-paused run=pr-review-pause-{number}-{head_sha} -->
### 🤖 Dev-agents paused

Automation is paused for PR #{number} because its latest commit was pushed by `{author}`.

Apply the `{resume_label}` label when the external agent is finished and you want dev-agents to resume review/fix automation.
"""


def _report_line(report_url: str | None) -> str:
    return f"- Report: [Open this run in dev-agents report]({report_url})\n" if report_url else ""


def _review_started_body(
    number: int,
    run_id: str,
    meta: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
) -> str:
    scope = (
        "the full two-pass review (general defects followed by the repository-specific Codex review)"
        if review_round == 0
        else "the one allowed targeted post-fix verification of the changed diff and prior findings"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review started

Luna has started {scope} for PR #{number}.

- PR head: `{meta.get('headRefOid', 'unknown')}`
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_completed_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
    old_sha: str = "",
    new_sha: str = "",
    changed_files: list[str] | None = None,
    validation: str | None = None,
    failure: str | None = None,
) -> str:
    """Render the final state into the same comment used for review progress."""
    review_report = report.get("review_report")
    verdict = review_report.get("verdict") if isinstance(review_report, dict) else None
    if failure:
        heading = "### 🤖 Luna review failed"
        outcome = failure
    elif new_sha and new_sha != old_sha:
        heading = "### 🤖 Luna review completed — fixes pushed"
        outcome = "Concrete findings were identified and fixed."
    elif verdict == "clean":
        heading = "### 🤖 Luna review completed — clean"
        outcome = "No actionable defects were found."
    else:
        heading = "### 🤖 Luna review completed"
        outcome = "The review completed without pushing a code change."
    findings = _findings_summary(report, "No actionable defects found.")
    fixes = str(report.get("fixes") or "None reported.").strip()
    shown_files = changed_files or []
    files_line = ", ".join(f"`{path}`" for path in shown_files[:12]) or "None"
    if len(shown_files) > 12:
        files_line += f" (+{len(shown_files) - 12} more)"
    commit_line = "The PR head was unchanged."
    if new_sha and new_sha != old_sha:
        commit_line = f"Pushed fix commit `{new_sha[:12]}`."
    validation_line = validation or "See the run report for validation details."
    return f"""<!-- dev-agents:pr-review run={run_id} -->
{heading}

{outcome}

- PR: #{number}
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Findings: {findings}
- What changed: {fixes}
- {commit_line}
- Changed files: {files_line}
- Validation: {validation_line}
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_findings_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
) -> str:
    findings = _findings_summary(report, "No actionable defects found.")
    scope = "review passes" if review_round == 0 else "targeted post-fix verification"
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review findings

Luna completed the {scope} for PR #{number}.

- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Findings: {findings}
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_fixes_started_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
) -> str:
    findings = _findings_summary(
        report, "Concrete findings were identified during the review."
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna fixes started

Luna is applying the smallest correct fixes for PR #{number}.

- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Findings being addressed: {findings}
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _fix_summary_body(
    number: int,
    run_id: str,
    meta: dict[str, Any],
    refreshed_meta: dict[str, Any],
    keys: list[str],
    changed_files: list[str],
    reason: str | None = None,
    details: str | None = None,
    report_url: str | None = None,
) -> str:
    """Build the human-readable PR comment for a completed remediation run."""
    counts: dict[str, int] = {}
    for key in keys:
        kind = key.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    labels = {
        "comment": "review thread",
        "review": "change-request review",
        "check": "failing check",
        "conflict": "merge conflict",
    }
    reasons = []
    for kind in ("comment", "review", "check", "conflict"):
        if counts.get(kind):
            count = counts[kind]
            label = labels[kind] if count == 1 else f"{labels[kind]}s"
            reasons.append(f"{count} {label}")
    reason_text = reason or ", ".join(reasons) or "actionable PR feedback"
    old_sha = str(meta.get("headRefOid", ""))
    new_sha = str(refreshed_meta.get("headRefOid", ""))
    pr_url = str(refreshed_meta.get("url") or meta.get("url") or "")
    commit_line = "The PR head was unchanged."
    if new_sha and new_sha != old_sha:
        commit = new_sha[:12]
        commit_url = f"{pr_url}/commits/{new_sha}" if pr_url else ""
        commit_text = f"[`{commit}`]({commit_url})" if commit_url else f"`{commit}`"
        commit_line = f"Pushed fix commit {commit_text}."
    if changed_files:
        shown_files = changed_files[:12]
        file_text = ", ".join(f"`{path}`" for path in shown_files)
        if len(changed_files) > len(shown_files):
            file_text += f" (+{len(changed_files) - len(shown_files)} more)"
    else:
        file_text = "(not available)"
    report_line = f"\n[View the PR]({pr_url})" if pr_url else ""
    details_line = f"- What changed: {details}\n" if details else ""
    marker = f"<!-- dev-agents:pr-fixer-summary run={run_id} -->"
    return f"""{marker}
### 🤖 PR fixer completed

The automated fixer addressed {reason_text} on PR #{number}.

- {commit_line}
- Validation: the triggering feedback was re-checked successfully.
{details_line}- Validation: the triggering feedback was re-checked successfully.
- Files in the resulting PR diff: {file_text}
- Run: `{run_id}`
{_report_line(report_url)}{report_line}
"""


def _publish_fix_summary(repo: Path, number: int, run_id: str, body: str) -> bool:
    """Create or update the bot's single summary comment for one fixer run."""
    try:
        slug = repository_slug(repo)
        raw = _run(
            repo,
            "gh",
            "api",
            f"repos/{slug}/issues/{number}/comments",
            "--paginate",
            "--slurp",
        )
        pages = json.loads(raw) if raw else []
        comments: list[dict[str, Any]] = []
        for page in pages if isinstance(pages, list) else []:
            if isinstance(page, list):
                comments.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict):
                comments.append(page)
        marker = f"<!-- dev-agents:pr-fixer-summary run={run_id} -->"
        existing = next(
            (comment for comment in comments if marker in str(comment.get("body", ""))),
            None,
        )
        if existing and existing.get("id") is not None:
            _run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/comments/{existing['id']}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            )
        else:
            _run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/{number}/comments",
                "-f",
                f"body={body}",
            )
    except (RuntimeError, json.JSONDecodeError) as error:
        _log(f"summary-comment-failed pr={number} error={error}")
        return False
    return True


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


def _target_validation_instructions(repo: Path, base_branch: str) -> str:
    """Describe the target repository's optimized validation path when it provides one."""
    scripts = {
        name: repo / "scripts" / name
        for name in ("affected-workspaces.mjs", "lint-changed.mjs", "test-changed.mjs")
    }
    if all(path.is_file() for path in scripts.values()):
        return f"""This repository provides dependency-aware affected validation. Follow the same
decision path as `.github/workflows/deploy.yml`; do not substitute repository-wide checks for it.
First resolve the merge base and inspect the selected scope:

BASE_SHA=$(git merge-base HEAD origin/{base_branch} 2>/dev/null || git merge-base HEAD {base_branch})
bun scripts/affected-workspaces.mjs --base \"$BASE_SHA\" --head HEAD

For an ordinary PR scope, run the exact changed-file validators in parallel after dependencies are
installed:
- `bun scripts/lint-changed.mjs --base \"$BASE_SHA\" --head HEAD`
- `bun scripts/test-changed.mjs --base \"$BASE_SHA\" --head HEAD`
- the selected workspace `lint:types` commands from the workflow, when applicable

If the selector reports full validation (for example because package.json, bun.lock, scripts/,
`.github/`, or shared configuration changed), follow the workflow's widened affected-workspace
commands instead. Do not run `bun run lint` or a repository-wide test command for an ordinary
affected-only PR."""
    return """Run focused tests first. For linting, run ESLint/format checks only against changed files;
do not run repository-wide lint or test commands unless the target repository's instructions
require them or no targeted command is available. Independent lint, test, and type-check commands
may run in parallel after dependencies are installed."""


def _prompt(
    repo: Path, number: int, keys: list[str], base_branch: str, conflicts: list[str]
) -> str:
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(
        f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions
    )
    shared_skill = (
        SHARED_PR_FIX_SKILL.read_text(encoding="utf-8") if SHARED_PR_FIX_SKILL.is_file() else ""
    )
    target_skill = repo / ".codex/skills/pr-fix/SKILL.md"
    target_skill_text = target_skill.read_text(encoding="utf-8") if target_skill.is_file() else ""
    changed_files = _run(
        repo, "git", "diff", "--name-only", f"origin/{base_branch}...HEAD", check=False
    )
    changed = [path for path in changed_files.splitlines() if path and (repo / path).exists()]
    changed_text = " ".join(changed) if changed else "<no changed files detected>"
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    validation_instructions = _target_validation_instructions(repo, base_branch)
    return f"""Fix actionable GitHub feedback for PR #{number} in this isolated worktree.
Shared dev-agents PR-fix workflow:
{shared_skill}

Target-repository PR-fix workflow (if present):
{target_skill_text}

Read and obey these repository instructions:
{instruction_text}

Actionable feedback identities:
{chr(10).join("- " + key for key in keys)}

Changed files (use these paths for targeted linting):
{changed_text}

Merge-conflict paths (resolve, stage, commit, and push them before testing):
{conflict_text}

Validation protocol:
{validation_instructions}

Commit and push HEAD to the PR branch.
Do not close or merge the PR.

At the end, print this exact plain-text block to stdout (no markdown fence), with one concise line
for each field. REPORT_JSON must be valid compact JSON matching the supplied skill's schema:
{REVIEW_REPORT_BEGIN}
FINDINGS: <what the supplied feedback identified>
FIXES: <what changed, or none>
REPORT_JSON: {{"verdict":"clean|findings","findings":[],"categories_checked":[],"validation":[],"fixes":[]}}
{REVIEW_REPORT_END}"""


def _review_prompt(
    repo: Path,
    number: int,
    meta: dict[str, Any],
    base_branch: str,
    conflicts: list[str],
    run_id: str | None = None,
    review_round: int = 0,
    parent_sha: str | None = None,
    prior_report: dict[str, Any] | None = None,
) -> str:
    """Build either the initial or bounded post-fix review prompt."""
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(
        f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions
    )
    skill_relative_path, skill_text = _review_skill(repo)
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    validation_instructions = _target_validation_instructions(repo, base_branch)
    head_sha = meta.get("headRefOid", "unknown")
    report_id = run_id or "the run ID supplied by the daemon"
    if review_round == 0:
        review_instructions = f"""The PR is green and GitHub has no submitted Copilot review. Run both independent passes:

1. GENERAL DEFECT REVIEW: inspect the actual diff against origin/{base_branch}, surrounding call
sites, security/privacy boundaries, async behavior, tests, and likely regressions. Report only
concrete defects introduced by this PR; do not invent style nits or speculative concerns.

2. CODEX-CRYPTICA REVIEW: read and apply the repository's `{skill_relative_path}`
below, including its linked guidance. Check Svelte 5/TypeScript behavior, worker safety, AI
parsing, privacy, accessibility, performance, tests, and bounded responsibility."""
        pass_description = "the two-pass review"
        pass_names = "[\"general\", \"codex-review\"]"
        fix_condition = "If either pass finds a concrete defect"
        no_fix_condition = "If both passes find no actionable defect"
    else:
        previous_report = json.dumps(prior_report or {}, sort_keys=True)
        review_instructions = f"""This is the one allowed targeted post-fix verification after an earlier Luna review.
The prior review head was `{parent_sha or 'unknown'}`. Inspect the diff from that head to the
current head, the files changed by the fix, and every finding in the prior structured report:
{previous_report}

Verify that the original findings are actually resolved and that the fix did not introduce a
new concrete defect. Do not repeat the full general and repository-specific review checklists;
keep this pass limited to the post-fix diff and the prior findings."""
        pass_description = "the targeted post-fix verification"
        pass_names = "[\"targeted-post-fix\"]"
        fix_condition = "If the targeted verification finds a concrete unresolved defect"
        no_fix_condition = "If the targeted verification finds no actionable defect"
    return f"""Perform {pass_description} and, if needed, fix Pull Request #{number}.

PR head: {head_sha}
PR branch: {meta.get("headRefName", "unknown")} (based on {base_branch})
PR URL: {meta.get("url", "")}

{review_instructions}

Review pass identifiers to use in the run report: {pass_names}

Repository instructions:
{instruction_text}

Codex review skill:
{skill_text}

Merge-conflict paths:
{conflict_text}

Validation protocol:
{validation_instructions}

Review communication protocol:
- The daemon owns one evolving lifecycle comment for this run, marked
  `<!-- dev-agents:pr-review run={report_id} -->`.
- Do not create additional PR comments for findings or fixes. Before editing, use `gh api` to PATCH
  the existing comment containing that marker with a concise findings update, then PATCH it again
  immediately before edits begin when concrete fixes are needed. Never use `gh pr comment` for
  review progress. The daemon will make the final update with the structured findings, what changed,
  commit, files, and validation. Keep the full detail in the required report block and persisted
  run timeline.
- At the end, print this exact plain-text block to stdout (no markdown fence), with one concise line
  for each field. Use `none` when appropriate:
  {REVIEW_REPORT_BEGIN}
  FINDINGS: <what {pass_description} found>
  FIXES: <what changed, or none>
  REPORT_JSON: {{"verdict":"clean|findings","findings":[],"categories_checked":[],"validation":[],"fixes":[]}}
  {REVIEW_REPORT_END}
- Do not include credentials, tokens, or private user data in comments or the report.

{no_fix_condition}, make no code changes. If the worktree contains a
pre-merge base commit, push that commit; otherwise leave the original head unchanged. In either
case finish successfully only when `git status --porcelain` is clean and the remote branch is at
the reviewed head.

{fix_condition}, make the smallest correct fix, add focused tests, run the
targeted validation required by the repository instructions, commit, and push HEAD to the PR
branch. Never close or merge the PR. Do not use a failing test bypass or hide unresolved merge
conflicts."""


class PrFixerRunState(TypedDict, total=False):
    """State carried through one webhook/reconciliation PR-fix run."""

    number: int
    meta: dict[str, Any]
    keys: list[str]
    handled: list[str]
    unseen: list[str]
    state_path: Path
    run_id: str
    claimed: bool
    skip: bool
    fixed: bool
    started: bool
    review_only: bool
    workflow: str
    report: dict[str, Any]
    report_url: str | None
    review_round: int
    review_scope: str
    review_chain_id: str
    review_parent_run_id: str | None
    review_parent_sha: str | None
    review_prior_report: dict[str, Any] | None


class PrFixerService:
    def __init__(self, project_name: str, project: ProjectConfig, config: PrFixerConfig):
        self.project_name, self.project, self.config = project_name, project, config
        self.state = StateRepository(
            _state_path(config, project_name), project_name, project.repo
        )
        self.active: set[int] = set()
        self.lock = Lock()
        self.workflow = self._build_workflow()

    def handle(self, number: int) -> bool:
        with self.lock:
            if number in self.active:
                return False
            self.active.add(number)
        try:
            result = self.workflow.invoke({"number": number})
            # Merge readiness is deliberately independent from the feedback
            # run claim. A no-op run can be completed while checks are still
            # being created; a later check_run.completed event must still be
            # able to request auto-merge for the same PR head.
            self._ensure_auto_merge(number)
            return bool(result.get("started", False))
        finally:
            with self.lock:
                self.active.discard(number)
            from dev_agents.visualize import schedule_report_refresh

            schedule_report_refresh(self.project_name, self.project)

    def _build_workflow(self) -> Any:
        graph = StateGraph(PrFixerRunState)
        graph.add_node("collect_feedback", self._collect_feedback)
        graph.add_node("remediate", self._remediate)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "collect_feedback")
        graph.add_edge("collect_feedback", "remediate")
        graph.add_edge("remediate", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    def _review_plan(self, number: int, head_sha: str) -> dict[str, Any] | None:
        """Select one initial review or the single follow-up allowed for a fixed head."""
        runs = [
            run
            for run in self.state.list_runs("pr-review", limit=200)
            if run.metadata.get("pull_request") == number
        ]
        if not runs:
            return {
                "round": 0,
                "scope": "full",
                "chain_id": f"pr-review-{number}-{head_sha}",
                "parent_run_id": None,
                "parent_sha": None,
                "prior_report": None,
            }

        latest = runs[0]
        metadata = latest.metadata
        try:
            review_round = int(metadata.get("review_round", 0))
        except (TypeError, ValueError):
            review_round = 0
        latest_head = str(metadata.get("head_sha", ""))
        chain_id = str(metadata.get("review_chain_id") or latest.run_id)

        if metadata.get("review_exhausted_head_sha") == head_sha:
            return None

        # Older completed targeted reviews did not persist an exhausted head when
        # they found no new defect. Treat that completed round as exhausted too,
        # so reconciliation cannot restart the same review indefinitely.
        if (
            latest.status == "completed"
            and review_round >= MAX_INTERNAL_REVIEW_ROUNDS - 1
            and latest_head == head_sha
        ):
            return None

        if review_round == 0 and metadata.get("review_fix_pushed"):
            return {
                "round": 1,
                "scope": "targeted-post-fix",
                "chain_id": chain_id,
                "parent_run_id": latest.run_id,
                "parent_sha": str(metadata.get("reviewed_head_sha") or latest_head),
                "prior_report": metadata.get("review_report"),
            }

        if review_round >= MAX_INTERNAL_REVIEW_ROUNDS - 1:
            if latest_head == head_sha:
                return {
                    "round": review_round,
                    "scope": "targeted-post-fix",
                    "chain_id": chain_id,
                    "parent_run_id": metadata.get("review_parent_run_id"),
                    "parent_sha": metadata.get("review_parent_sha"),
                    "prior_report": metadata.get("review_prior_report"),
                }
            return {
                "round": 0,
                "scope": "full",
                "chain_id": f"pr-review-{number}-{head_sha}",
                "parent_run_id": None,
                "parent_sha": None,
                "prior_report": None,
            }

        return {
            "round": 0,
            "scope": "full",
            "chain_id": f"pr-review-{number}-{head_sha}",
            "parent_run_id": None,
            "parent_sha": None,
            "prior_report": None,
        }

    def _collect_feedback(self, state: PrFixerRunState) -> dict[str, Any]:
        number = state["number"]
        meta, keys = _feedback(self.project.repo, number)
        labels = {label["name"].lower() for label in meta.get("labels", [])}
        if (
            meta.get("baseRefName") != self.config.base_branch
            or meta.get("isDraft")
            or "paused" in labels
        ):
            return {"meta": meta, "keys": keys, "skip": True}
        head_sha = str(meta.get("headRefOid", "unknown"))
        external_author = _external_agent_commit(meta, self.config)
        resume_label = self.config.external_agent_resume_label.strip().lower()
        if (
            meta.get("state") == "OPEN"
            and external_author is not None
            and resume_label not in labels
        ):
            _publish_pr_run_comment(
                self.project.repo,
                number,
                f"pr-review-pause-{number}-{head_sha}",
                "external-agent-paused",
                _external_agent_pause_body(
                    number, head_sha, external_author, self.config.external_agent_resume_label
                ),
            )
            _log(
                f"paused pr={number} reason=external-agent-commit author={external_author}"
            )
            return {
                "meta": meta,
                "keys": keys,
                "skip": True,
                "skip_reason": "external-agent-commit",
                "external_agent": external_author,
            }
        state_path = _state_path(self.config, self.project_name)
        # Import old JSON feedback identities into relational rows once. The legacy document
        # remains readable for rollback, while all new transitions use SQLite rows.
        legacy_handled: list[str] = []
        legacy_path = legacy_json_path(None, state_path)
        if legacy_path.is_file():
            legacy_records = _load_state(state_path).setdefault("pullRequests", {})
            candidate_handled = legacy_records.get(str(number), [])
            if isinstance(candidate_handled, list):
                legacy_handled = candidate_handled
                self.state.mark_feedback_processed("pr-fixer", str(number), legacy_handled)
        unseen = self.state.unprocessed_feedback("pr-fixer", str(number), keys)
        review_plan = self._review_plan(number, head_sha)
        review_only = (
            self.config.review_without_copilot
            and not keys
            and _review_is_due(meta, self.config)
            and not _has_copilot_review(meta)
            and review_plan is not None
        )
        fingerprint = hashlib.sha256("\0".join(sorted(keys)).encode()).hexdigest()[:16]
        workflow = "pr-review" if review_only else "pr-fixer"
        run_id = (
            f"pr-review-{number}-{head_sha}"
            if review_only
            else f"pr-{number}-{head_sha}-{fingerprint}"
        )
        review_skill_path, _ = _review_skill(self.project.repo)
        claim = self.state.claim_run(
            workflow,
            run_id,
            metadata={
                "pull_request": number,
                "head_sha": head_sha,
                "feedback": keys,
                "review_only": review_only,
                "copilot_reviewed": _has_copilot_review(meta),
                "review_skill": review_skill_path if review_only else None,
                "review_report_schema": 1 if review_only else None,
                "review_round": review_plan["round"] if review_only and review_plan else None,
                "review_scope": review_plan["scope"] if review_only and review_plan else None,
                "review_chain_id": review_plan["chain_id"] if review_only and review_plan else None,
                "review_parent_run_id": (
                    review_plan["parent_run_id"] if review_only and review_plan else None
                ),
                "review_parent_sha": (
                    review_plan["parent_sha"] if review_only and review_plan else None
                ),
                "review_prior_report": (
                    review_plan["prior_report"] if review_only and review_plan else None
                ),
            },
        )
        if claim.claimed:
            self.state.record_event(
                workflow,
                run_id,
                "collect_feedback",
                "review_started" if review_only else "feedback_collected",
                metadata=(
                    {
                        "passes": ["general", "codex-review"]
                        if review_plan and review_plan["round"] == 0
                        else ["targeted-post-fix"],
                        "copilot_reviewed": False,
                        "review_round": review_plan["round"] if review_plan else 0,
                    }
                    if review_only
                    else {"keys": len(keys), "unseen": len(unseen)}
                ),
            )
            if review_only:
                if review_plan and review_plan["round"] == 0:
                    self.state.record_event(
                        workflow,
                        run_id,
                        "general_review",
                        "pass_scheduled",
                        metadata={"kind": "general-defect-review", "review_round": 0},
                    )
                    self.state.record_event(
                        workflow,
                        run_id,
                        "codex_review",
                        "pass_scheduled",
                        metadata={"skill": review_skill_path, "report_schema": 1},
                    )
                else:
                    self.state.record_event(
                        workflow,
                        run_id,
                        "targeted_post_fix_review",
                        "pass_scheduled",
                        metadata={
                            "parent_run_id": review_plan["parent_run_id"] if review_plan else None,
                            "parent_sha": review_plan["parent_sha"] if review_plan else None,
                            "report_schema": 1,
                        },
                    )
        return {
            "meta": meta,
            "keys": keys,
            "handled": legacy_handled if isinstance(legacy_handled, list) else [],
            "unseen": unseen,
            "state_path": state_path,
            "run_id": run_id,
            "workflow": workflow,
            "review_only": review_only,
            "review_round": review_plan["round"] if review_only and review_plan else 0,
            "review_scope": review_plan["scope"] if review_only and review_plan else "full",
            "review_chain_id": (
                review_plan["chain_id"] if review_only and review_plan else run_id
            ),
            "review_parent_run_id": (
                review_plan["parent_run_id"] if review_only and review_plan else None
            ),
            "review_parent_sha": (
                review_plan["parent_sha"] if review_only and review_plan else None
            ),
            "review_prior_report": (
                review_plan["prior_report"] if review_only and review_plan else None
            ),
            "claimed": claim.claimed,
            "skip": not claim.claimed,
        }

    def _remediate(self, state: PrFixerRunState) -> dict[str, Any]:
        if state.get("skip") or (not state.get("review_only") and not state.get("unseen")):
            return {"started": False, "fixed": False}
        number = state["number"]
        unseen = state["unseen"]
        review_only = bool(state.get("review_only"))
        workflow = state.get("workflow", "pr-fixer")
        _log(
            f"starting pr={number} mode={'review' if review_only else 'fix'} "
            f"actionable_items={len(unseen)}"
        )
        if review_only:
            from dev_agents.visualize import refresh_report_run_url

            report_url = refresh_report_run_url(
                self.project_name, self.project, workflow, state["run_id"]
            )
            _publish_pr_run_comment(
                self.project.repo,
                number,
                state["run_id"],
                "review-started",
                _review_started_body(
                    number,
                    state["run_id"],
                    state["meta"],
                    report_url,
                    int(state.get("review_round", 0)),
                ),
            )
        else:
            report_url = None
        fixed, report = self._fix(
            number,
            state["meta"]["headRefName"],
            unseen,
            review_only=review_only,
            meta=state["meta"],
            run_id=state["run_id"],
            review_round=int(state.get("review_round", 0)),
            parent_sha=state.get("review_parent_sha"),
            prior_report=state.get("review_prior_report"),
        )
        if review_only:
            self.state.record_event(
                workflow,
                state["run_id"],
                "review_result",
                "result_recorded",
                status="completed" if fixed else "failed",
                metadata={
                    "review_round": int(state.get("review_round", 0)),
                    "review_scope": state.get("review_scope", "full"),
                    "review_report": report.get("review_report"),
                    "report_valid": report.get("report_valid", False),
                    "report_error": report.get("report_error"),
                },
            )
        self.state.record_event(
            workflow,
            state["run_id"],
            "remediate",
            "review_completed" if review_only else "fix_completed",
            status="completed" if fixed else "failed",
            metadata={
                "fixed": fixed,
                "unseen": len(unseen),
                "review_only": review_only,
                "review_report": report.get("review_report") if review_only else None,
                "review_report_valid": report.get("report_valid") if review_only else None,
                "review_report_error": report.get("report_error") if review_only else None,
            },
        )
        if not fixed:
            _log(
                f"{'review' if review_only else 'fix'}-incomplete pr={number}; "
                "work remains actionable"
            )
        return {"started": fixed, "fixed": fixed, "report": report, "report_url": report_url}

    def _finalize(self, state: PrFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("claimed"):
            return {"started": False}
        number = state["number"]
        workflow = state.get("workflow", "pr-fixer")
        if state.get("review_only"):
            if not state.get("fixed"):
                recovery_metadata: dict[str, Any] = {}
                try:
                    refreshed_meta, _ = _feedback(self.project.repo, number)
                except (RuntimeError, json.JSONDecodeError):
                    refreshed_meta = {}
                old_sha = str(state.get("meta", {}).get("headRefOid", ""))
                new_sha = str(refreshed_meta.get("headRefOid", ""))
                if new_sha and new_sha != old_sha:
                    review_round = int(state.get("review_round", 0))
                    recovery_metadata = {
                        "reviewed_head_sha": old_sha,
                        "review_fix_pushed": True,
                        "review_follow_up_pending": review_round == 0,
                        "review_follow_up_head_sha": new_sha if review_round == 0 else None,
                        "review_exhausted_head_sha": new_sha if review_round >= 1 else None,
                        "review_failure_recovered": True,
                        "review_report": state.get("report", {}).get("review_report"),
                        "review_report_valid": state.get("report", {}).get(
                            "report_valid", False
                        ),
                    }
                self.state.record_event(
                    workflow,
                    state["run_id"],
                    "finalize",
                    "review_failed",
                    status="failed",
                    metadata=recovery_metadata,
                )
                self.state.complete_run(
                    workflow,
                    state["run_id"],
                    status="failed",
                    error="review agent did not complete successfully",
                    metadata=recovery_metadata,
                )
                _publish_pr_run_comment(
                    self.project.repo,
                    number,
                    state["run_id"],
                    "review-final",
                    _review_completed_body(
                        number,
                        state["run_id"],
                        state.get("report", {}),
                        state.get("report_url"),
                        int(state.get("review_round", 0)),
                        failure="The review agent did not complete successfully; see the daemon log.",
                    ),
                )
                return {"started": False}
            refreshed_meta, _ = _feedback(self.project.repo, number)
            old_sha = str(state["meta"].get("headRefOid", ""))
            new_sha = str(refreshed_meta.get("headRefOid", ""))
            review_round = int(state.get("review_round", 0))
            fix_pushed = bool(new_sha and new_sha != old_sha)
            review_passes = (
                ["general", "codex-review"]
                if review_round == 0
                else ["targeted-post-fix"]
            )
            exhausted_head_sha = (new_sha or old_sha) if review_round >= 1 else None
            changed_files = [
                path
                for path in _run(
                    self.project.repo,
                    "gh",
                    "pr",
                    "diff",
                    str(number),
                    "--name-only",
                    check=False,
                ).splitlines()
                if path
            ]
            review_report = state.get("report", {}).get("review_report")
            validation = None
            if isinstance(review_report, dict) and isinstance(review_report.get("validation"), list):
                validation = "; ".join(str(item) for item in review_report["validation"])
            comment_posted = _publish_pr_run_comment(
                self.project.repo,
                number,
                state["run_id"],
                "review-final",
                _review_completed_body(
                    number,
                    state["run_id"],
                    state.get("report", {}),
                    state.get("report_url"),
                    review_round,
                    old_sha,
                    new_sha,
                    changed_files,
                    validation,
                ),
            )
            self.state.record_event(
                workflow,
                state["run_id"],
                "finalize",
                "review_finalized",
                metadata={
                    "passes": review_passes,
                    "review_round": review_round,
                    "review_fix_pushed": fix_pushed,
                    "review_follow_up_pending": review_round == 0 and fix_pushed,
                    "review_follow_up_head_sha": new_sha if review_round == 0 and fix_pushed else None,
                    "review_exhausted_head_sha": exhausted_head_sha,
                    "summary_comment_posted": comment_posted,
                    "review_report": state.get("report", {}).get("review_report"),
                    "review_report_valid": state.get("report", {}).get("report_valid", False),
                },
            )
            self.state.complete_run(
                workflow,
                state["run_id"],
                metadata={
                    "reviewed_head_sha": state["meta"].get("headRefOid"),
                    "review_round": review_round,
                    "review_scope": state.get("review_scope", "full"),
                    "review_chain_id": state.get("review_chain_id", state["run_id"]),
                    "review_parent_run_id": state.get("review_parent_run_id"),
                    "review_parent_sha": state.get("review_parent_sha"),
                    "review_prior_report": state.get("review_prior_report"),
                    "review_fix_pushed": fix_pushed,
                    "review_follow_up_pending": review_round == 0 and fix_pushed,
                    "review_follow_up_head_sha": new_sha if review_round == 0 and fix_pushed else None,
                    "review_exhausted_head_sha": exhausted_head_sha,
                    "review_report": state.get("report", {}).get("review_report"),
                    "review_report_valid": state.get("report", {}).get("report_valid", False),
                },
            )
            return {"started": True}
        if not state.get("unseen"):
            self.state.record_event(workflow, state["run_id"], "finalize", "no_action")
            self.state.complete_run(workflow, state["run_id"])
            return {"started": False}
        if not state.get("fixed"):
            self.state.record_event(
                workflow, state["run_id"], "finalize", "fix_failed", status="failed"
            )
            self.state.complete_run(
                workflow,
                state["run_id"],
                status="failed",
                error="agent did not complete the fix",
            )
            return {"started": False}
        slug = repository_slug(self.project.repo)
        for key in state["unseen"]:
            if key.startswith("comment:"):
                comment_id = key.split(":", 1)[1]
                _run(
                    self.project.repo,
                    "gh",
                    "api",
                    f"repos/{slug}/pulls/{number}/comments/{comment_id}/replies",
                    "-f",
                    "body=Processed by the PR fixer; verification completed.",
                    check=False,
                )
        refreshed_meta, refreshed = _feedback(self.project.repo, number)
        if all(key not in refreshed or key.startswith("comment:") for key in state["unseen"]):
            changed_files = [
                path
                for path in _run(
                    self.project.repo,
                    "gh",
                    "pr",
                    "diff",
                    str(number),
                    "--name-only",
                    check=False,
                ).splitlines()
                if path
            ]
            summary = _fix_summary_body(
                number,
                state["run_id"],
                state["meta"],
                refreshed_meta,
                state["unseen"],
                changed_files,
                details=state.get("report", {}).get("fixes"),
                report_url=state.get("report_url"),
            )
            comment_posted = _publish_fix_summary(
                self.project.repo, number, state["run_id"], summary
            )
            self.state.record_event(
                workflow,
                state["run_id"],
                "finalize",
                "feedback_processed",
                metadata={
                    "processed": len(state["unseen"]),
                    "summary_comment_posted": comment_posted,
                },
            )
            self.state.mark_feedback_processed("pr-fixer", str(number), state["unseen"])
            self.state.complete_run(workflow, state["run_id"])
        return {"started": True}

    def _fix(
        self,
        number: int,
        branch: str,
        keys: list[str],
        *,
        review_only: bool = False,
        meta: dict[str, Any] | None = None,
        run_id: str | None = None,
        review_round: int = 0,
        parent_sha: str | None = None,
        prior_report: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        base = self.config.base_branch
        root = (self.config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser()
        succeeded = False
        report: dict[str, Any] = {}
        try:
            with isolated_worktree(self.project.repo, root, branch, base) as (worktree, conflicts):
                _log(f"worktree pr={number} path={worktree}")
                if conflicts:
                    _log(f"merge-conflict pr={number} paths={','.join(conflicts)}")
                log_dir = (
                    self.config.log_dir
                    or Path.home() / ".local/state/dev-agents" / self.project_name / "logs"
                ).expanduser()
                log_dir.mkdir(parents=True, exist_ok=True)
                log = log_dir / f"pr-{number}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.log"

                def run_provider(provider: str) -> Any:
                    prompt = (
                        _review_prompt(
                            worktree,
                            number,
                            meta or {},
                            base,
                            conflicts,
                            run_id=run_id,
                            review_round=review_round,
                            parent_sha=parent_sha,
                            prior_report=prior_report,
                        )
                        if review_only
                        else _prompt(worktree, number, keys, base, conflicts)
                    )
                    _log(f"agent-start pr={number} provider={provider} log={log}")
                    result = run_agent(
                        PR_FIX_PROVIDER,
                        prompt,
                        cwd=worktree,
                        log_path=log,
                        timeout_seconds=self.config.timeout_minutes * 60,
                        heartbeat_seconds=self.config.heartbeat_seconds,
                        reasoning_effort=self.config.reasoning_effort,
                    )
                    report.update(_agent_report(log))
                    result_code = result.returncode
                    if result.timed_out:
                        _log(f"agent-timeout pr={number} provider={provider}")
                    _log(f"agent-finished pr={number} provider={provider} exit={result_code}")
                    return result

                def accept_provider(_provider: str, result: Any) -> bool:
                    _run(worktree, "git", "fetch", "origin", branch, check=False)
                    pushed = _run(worktree, "git", "rev-parse", "HEAD", check=False) == _run(
                        worktree, "git", "rev-parse", f"origin/{branch}", check=False
                    )
                    return bool(
                        result.returncode == 0
                        and pushed
                        and not _run(
                            worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False
                        )
                        and _run(worktree, "git", "status", "--porcelain") == ""
                    )

                # PR reviews and fixes are intentionally Codex-only. Keep the legacy providers
                # field for config compatibility, but never let another agent handle PR code.
                accepted = run_with_fallback((PR_FIX_PROVIDER,), run_provider, accept_provider)
                if accepted:
                    if review_only and not report.get("report_valid", False):
                        _log(
                            f"review-report-invalid pr={number} "
                            f"reason={report.get('report_error', 'missing report')}"
                        )
                    else:
                        _log(f"agent-accepted pr={number} provider={accepted[0]}")
                        succeeded = True
        except RuntimeError as error:
            _log(f"worktree-failed pr={number} error={error}")
        return succeeded, report

    def _auto_merge_eligible(self, meta: dict[str, Any], keys: list[str]) -> bool:
        labels = {label["name"].lower() for label in meta.get("labels", [])}
        checks = meta.get("checks", [])
        resume_label = self.config.external_agent_resume_label.strip().lower()
        if _external_agent_commit(meta, self.config) and resume_label not in labels:
            return False
        if self.config.auto_merge_issue_fixes_only and not _is_issue_fix_pr(meta):
            return False
        if not checks:
            return False
        return (
            meta.get("state") == "OPEN"
            and meta.get("baseRefName") == self.config.base_branch
            and not meta.get("isDraft")
            and "paused" not in labels
            and meta.get("mergeable") == "MERGEABLE"
            and meta.get("reviewDecision") != "CHANGES_REQUESTED"
            and not any(
                item.get("bucket") == "pending"
                or item.get("state") in {"PENDING", "IN_PROGRESS", "QUEUED"}
                for item in meta.get("checks", [])
            )
            and not any(
                item.get("bucket") == "fail"
                or item.get("state") in {"FAILURE", "ERROR", "CANCELLED"}
                for item in checks
            )
            and not any(not key.startswith("comment:") for key in keys)
        )

    def _ensure_auto_merge(self, number: int) -> None:
        """Request and verify GitHub auto-merge for an eligible PR head.

        This has its own persisted claim instead of sharing the feedback run
        identity. That lets a later check completion retry after an earlier
        no-op/remediation run has already been completed.
        """
        if not self.config.auto_merge:
            return
        try:
            meta, keys = _feedback(self.project.repo, number)
        except RuntimeError as error:
            _log(f"auto-merge-deferred pr={number} reason=refresh-failed error={error}")
            return
        if meta.get("autoMergeRequest"):
            _log(f"auto-merge-already-requested pr={number}")
            return
        if not self._auto_merge_eligible(meta, keys):
            return
        if self.config.review_without_copilot and not _has_copilot_review(meta):
            head_sha = str(meta.get("headRefOid", "unknown"))
            if not self._has_completed_internal_review(number, head_sha):
                _log(f"auto-merge-deferred pr={number} reason=internal-review-required")
                return

        head_sha = str(meta.get("headRefOid", "unknown"))
        run_id = f"pr-{number}-{head_sha}"
        claim = self.state.claim_run(
            "pr-auto-merge",
            run_id,
            metadata={"pull_request": number, "head_sha": head_sha},
        )
        if not claim.claimed:
            return

        try:
            _run(self.project.repo, "gh", "pr", "merge", str(number), "--auto", "--squash")
            verification = json.loads(
                _run(
                    self.project.repo,
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--json",
                    "state,autoMergeRequest",
                )
            )
            if verification.get("state") == "OPEN" and not verification.get("autoMergeRequest"):
                raise RuntimeError("GitHub did not set autoMergeRequest")
        except (RuntimeError, json.JSONDecodeError) as error:
            self.state.complete_run(
                "pr-auto-merge",
                run_id,
                status="failed",
                error=str(error),
            )
            _log(f"auto-merge-failed pr={number} error={error}")
            return

        self.state.complete_run(
            "pr-auto-merge",
            run_id,
            metadata={"requested": True},
        )
        _log(f"auto-merge-requested pr={number} mode=squash head={head_sha[:12]}")

    def _has_completed_internal_review(self, number: int, head_sha: str) -> bool:
        """Return whether the current head is covered by a completed review chain.

        A targeted post-fix review can finish by pushing the final fix commit.
        In that case the review run is keyed to the pre-fix head, while its
        ``review_exhausted_head_sha`` records the new head that was actually
        validated. Treat that explicit clean chain result as covering the
        current head too.
        """
        review_run = self.state.load_run("pr-review", f"pr-review-{number}-{head_sha}")
        if review_run is not None and review_run.status == "completed":
            return True

        for candidate in self.state.list_runs("pr-review", limit=100):
            metadata = candidate.metadata
            if (
                candidate.status == "completed"
                and metadata.get("pull_request") == number
                and metadata.get("review_exhausted_head_sha") == head_sha
                and metadata.get("review_report_valid") is True
                and isinstance(metadata.get("review_report"), dict)
                and metadata["review_report"].get("verdict") == "clean"
            ):
                return True
        return False

    def reconcile(self) -> None:
        run_id = f"reconcile-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{time.time_ns()}"
        claim = self.state.claim_run(
            "pr-reconcile",
            run_id,
            metadata={"base_branch": self.config.base_branch, "trigger": "daemon"},
        )
        if not claim.claimed:
            return
        self.state.record_event("pr-reconcile", run_id, "reconcile", "started")
        try:
            raw = _run(
                self.project.repo,
                "gh",
                "pr",
                "list",
                "--base",
                self.config.base_branch,
                "--state",
                "open",
                "--json",
                "number,labels",
            )
            prs = json.loads(raw)
        except (RuntimeError, json.JSONDecodeError) as error:
            self.state.record_event(
                "pr-reconcile", run_id, "reconcile", "failed", status="failed", metadata={"error": str(error)}
            )
            self.state.complete_run("pr-reconcile", run_id, status="failed", error=str(error))
            _log(f"reconcile-failed error={error}")
            return
        if not isinstance(prs, list):
            reconcile_error = "GitHub returned a non-list PR response"
            self.state.record_event(
                "pr-reconcile",
                run_id,
                "reconcile",
                "failed",
                status="failed",
                metadata={"error": reconcile_error},
            )
            self.state.complete_run("pr-reconcile", run_id, status="failed", error=reconcile_error)
            _log(f"reconcile-failed error={reconcile_error}")
            return
        eligible = 0
        for pr in prs:
            if any(label.get("name", "").lower() == "paused" for label in pr.get("labels", [])):
                continue
            number = pr.get("number")
            if isinstance(number, int):
                eligible += 1
                Thread(target=self._handle_reconciled, args=(number,), daemon=True).start()
        self.state.record_event(
            "pr-reconcile",
            run_id,
            "reconcile",
            "jobs_dispatched",
            metadata={"open_prs": len(prs), "eligible_prs": eligible},
        )
        self.state.complete_run(
            "pr-reconcile",
            run_id,
            metadata={"open_prs": len(prs), "eligible_prs": eligible},
        )

    def _handle_reconciled(self, number: int) -> None:
        """Run a reconciled PR without allowing worker errors to escape the thread."""
        try:
            self.handle(number)
        except Exception as error:  # noqa: BLE001 - daemon must keep reconciling other PRs
            _log(f"reconcile-pr-failed pr={number} error={error}")

    def reconcile_loop(self) -> None:
        while True:
            time.sleep(self.config.reconcile_interval_seconds)
            _log("reconcile-start")
            self.reconcile()


def _signature_valid(body: bytes, received: str | None, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return received is not None and hmac.compare_digest(received, expected)


def _degodify_candidate(payload: dict[str, Any]) -> DegodifyFile | None:
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("path"), str):
        return None
    try:
        return DegodifyFile(
            relative_path=candidate["path"],
            total_lines=int(candidate["totalLines"]),
            code_lines=int(candidate["codeLines"]),
            file_type=str(candidate.get("type", "Utility / Module")),
            status=str(candidate.get("status", "WATCH")),
            is_data_catalog=bool(candidate.get("isDataCatalog", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def serve(project_name: str, project: ProjectConfig, config: PrFixerConfig) -> None:
    secret = os.environ.get(config.webhook_secret_env)
    if not secret:
        raise ConfigError(f"Set {config.webhook_secret_env} before starting the PR fixer")
    webhook_secret = secret
    degodify_secret = os.environ.get(config.degodify_webhook_secret_env)
    release_comms_secret = (
        os.environ.get(project.release_comms.webhook_secret_env) if project.release_comms else None
    )

    _cleanup_artifacts(project_name, config)
    service = PrFixerService(project_name, project, config)
    issue_service = (
        IssueFixerService(
            project_name,
            project,
            project.issue_fixer,
            config,
            service.state,
        )
        if project.issue_fixer is not None
        else None
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "ok": self.path == "/health",
                        "active_jobs": len(service.active)
                        + (len(issue_service.active) if issue_service is not None else 0),
                    }
                ).encode()
            )

        def do_POST(self) -> None:
            event = self.headers.get("X-GitHub-Event", "")
            delivery_header = self.headers.get("X-GitHub-Delivery")
            content_length = int(self.headers.get("content-length", "0"))
            if content_length > MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(content_length)
            delivery = delivery_header or hashlib.sha256(body).hexdigest()
            if self.path == config.degodify_webhook_path:
                if not degodify_secret or not _signature_valid(
                    body, self.headers.get("X-Hub-Signature-256"), degodify_secret
                ):
                    _log(f"rejected delivery={delivery} event=degodify reason=invalid-signature")
                    self.send_response(401)
                    self.end_headers()
                    return
                degodify_payload: Any = None
                try:
                    degodify_payload = json.loads(body)
                    candidate = _degodify_candidate(degodify_payload)
                except json.JSONDecodeError:
                    candidate = None
                if (
                    candidate is None
                    or not isinstance(degodify_payload, dict)
                    or degodify_payload.get("repository") != project.github
                ):
                    _log(f"ignored delivery={delivery} event=degodify reason=invalid-payload")
                    self.send_response(400)
                    self.end_headers()
                    return
                event_id = str(degodify_payload.get("analysisRunId") or delivery)
                state_file = _state_path(config, project_name)
                claims = StateRepository(state_file, project_name, project.repo)
                claim = claims.claim_run(
                    "degodify",
                    event_id,
                    delivery_id=delivery,
                    metadata={"repository": project.github, "candidate": candidate.relative_path},
                )
                if not claim.claimed:
                    _log(
                        f"ignored delivery={delivery} event=degodify reason=duplicate event_id={event_id}"
                    )
                    self.send_response(200)
                    self.end_headers()
                    return

                def degodify_phase(event: str, payload: dict[str, Any]) -> None:
                    claims.record_event("degodify", event_id, event, event, metadata=payload)

                def run_deg() -> None:
                    try:
                        result = run_degodify(
                            project.repo,
                            base_branch=config.base_branch,
                            providers=config.providers,
                            dry_run=False,
                            supplied_candidate=candidate,
                            on_event=degodify_phase,
                        )
                        _log(
                            f"handled delivery={delivery} event=degodify path={candidate.relative_path} succeeded={result.succeeded}"
                        )
                        claims.complete_run(
                            "degodify",
                            event_id,
                            status="completed" if result.succeeded else "failed",
                            error=None if result.succeeded else "decomposition failed",
                        )
                        from dev_agents.visualize import schedule_report_refresh

                        schedule_report_refresh(project_name, project)
                    except Exception as error:  # noqa: BLE001 - daemon must log worker failures
                        claims.complete_run("degodify", event_id, status="failed", error=str(error))
                        from dev_agents.visualize import schedule_report_refresh

                        schedule_report_refresh(project_name, project)
                        _log(f"failed delivery={delivery} event=degodify error={error}")

                Thread(target=run_deg, daemon=True).start()
                _log(f"accepted delivery={delivery} event=degodify path={candidate.relative_path}")
                self.send_response(202)
                self.end_headers()
                return
            if project.release_comms and self.path == project.release_comms.webhook_path:
                secret_hdr = self.headers.get("X-Release-Comms-Secret")
                if (
                    not release_comms_secret
                    or not secret_hdr
                    or not hmac.compare_digest(secret_hdr, release_comms_secret)
                ):
                    _log(f"rejected delivery={delivery} event=release-comms reason=invalid-secret")
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    comms_payload = json.loads(body)
                except json.JSONDecodeError:
                    _log(f"rejected delivery={delivery} event=release-comms reason=invalid-json")
                    self.send_response(400)
                    self.end_headers()
                    return
                promote_run_id = str(comms_payload.get("promoteRunId", ""))
                if not promote_run_id:
                    _log(
                        f"ignored delivery={delivery} event=release-comms reason=missing-promote-run-id"
                    )
                    self.send_response(400)
                    self.end_headers()
                    return
                comms_cfg = project.release_comms
                auto_publish = bool(comms_cfg and comms_cfg.auto_publish)

                def comms_phase(event: str, payload: dict[str, Any]) -> None:
                    detail = " ".join(f"{key}={value}" for key, value in payload.items())
                    _log(f"release-comms run_id={promote_run_id} phase={event} {detail}".rstrip())

                def run_comms() -> None:
                    try:
                        comms_phase("started", {"dry_run": not auto_publish})
                        res = run_release_comms(
                            project,
                            project_name,
                            promote_run_id,
                            dry_run=not auto_publish,
                            publish_approved=auto_publish,
                            on_event=comms_phase,
                            delivery_id=delivery,
                        )
                        _log(
                            f"handled delivery={delivery} event=release-comms run_id={promote_run_id} completed={res.completed} scheduled={res.scheduled}"
                        )
                    except Exception as error:  # noqa: BLE001 - daemon must log worker failures
                        _log(f"failed delivery={delivery} event=release-comms error={error}")

                Thread(target=run_comms, daemon=True).start()
                _log(f"accepted delivery={delivery} event=release-comms run_id={promote_run_id}")
                self.send_response(202)
                self.end_headers()
                return
            if self.path != config.webhook_path or not _signature_valid(
                body, self.headers.get("X-Hub-Signature-256"), webhook_secret
            ):
                _log(
                    f"rejected delivery={delivery} event={event or 'unknown'} reason=invalid-path-or-signature"
                )
                self.send_response(401)
                self.end_headers()
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                _log(f"rejected delivery={delivery} event={event or 'unknown'} reason=invalid-json")
                self.send_response(400)
                self.end_headers()
                return
            action = payload.get("action")
            if event == "push" and payload.get("ref") == f"refs/heads/{config.base_branch}":
                claim = service.state.claim_run(
                    "github-webhook",
                    f"delivery:{delivery}",
                    delivery_id=delivery,
                    metadata={"event": event, "action": action, "branch": config.base_branch},
                )
                if not claim.claimed:
                    _log(f"ignored delivery={delivery} event=push reason=duplicate")
                    self.send_response(200)
                    self.end_headers()
                    return

                def reconcile() -> None:
                    try:
                        service.reconcile()
                    except Exception as error:  # noqa: BLE001 - daemon must log worker failures
                        service.state.complete_run(
                            "github-webhook", f"delivery:{delivery}", status="failed", error=str(error)
                        )
                        _log(f"failed delivery={delivery} event=push error={error}")
                    else:
                        service.state.complete_run("github-webhook", f"delivery:{delivery}")

                _log(
                    f"accepted delivery={delivery} event=push branch={config.base_branch}; reconciling"
                )
                Thread(target=reconcile, daemon=True).start()
                self.send_response(202)
                self.end_headers()
                return
            if event == "issues":
                issue_number = payload.get("issue", {}).get("number")
                if (
                    issue_service is None
                    or payload.get("repository", {}).get("full_name") != project.github
                    or action not in _ACTIONS["issues"]
                    or not isinstance(issue_number, int)
                ):
                    _log(
                        f"ignored delivery={delivery} event=issues action={action or 'unknown'} "
                        f"issue={issue_number or 'unknown'}"
                    )
                    self.send_response(200)
                    self.end_headers()
                    return
                assert issue_service is not None
                claim = service.state.claim_run(
                    "github-webhook",
                    f"delivery:{delivery}",
                    delivery_id=delivery,
                    metadata={"event": event, "action": action, "issue": issue_number},
                )
                if not claim.claimed:
                    _log(f"ignored delivery={delivery} event=issues reason=duplicate")
                    self.send_response(200)
                    self.end_headers()
                    return

                def run_issue() -> None:
                    try:
                        started = issue_service.handle(issue_number)
                        service.state.complete_run(
                            "github-webhook",
                            f"delivery:{delivery}",
                            metadata={"started": started, "issue": issue_number},
                        )
                        _log(
                            f"handled delivery={delivery} event=issues action={action} "
                            f"issue={issue_number} started={started}"
                        )
                    except Exception as error:  # noqa: BLE001 - daemon must keep serving
                        service.state.complete_run(
                            "github-webhook",
                            f"delivery:{delivery}",
                            status="failed",
                            error=str(error),
                        )
                        _log(
                            f"failed delivery={delivery} event=issues action={action} "
                            f"issue={issue_number} error={error}"
                        )

                Thread(target=run_issue, daemon=True).start()
                _log(
                    f"accepted delivery={delivery} event=issues action={action} issue={issue_number}"
                )
                self.send_response(202)
                self.end_headers()
                return
            check_pull_requests = payload.get("check_run", {}).get("pull_requests") or []
            check_pr_number = check_pull_requests[0].get("number") if check_pull_requests else None
            number = (
                payload.get("number")
                or payload.get("pull_request", {}).get("number")
                or check_pr_number
            )
            if (
                payload.get("repository", {}).get("full_name") != project.github
                or action not in _ACTIONS.get(event, set())
                or not isinstance(number, int)
            ):
                _log(
                    f"ignored delivery={delivery} event={event or 'unknown'} action={action or 'unknown'} pr={number or 'unknown'}"
                )
                self.send_response(200)
                self.end_headers()
                return

            claim = service.state.claim_run(
                "github-webhook",
                f"delivery:{delivery}",
                delivery_id=delivery,
                metadata={"event": event, "action": action, "pull_request": number},
            )
            if not claim.claimed:
                _log(f"ignored delivery={delivery} event={event} reason=duplicate")
                self.send_response(200)
                self.end_headers()
                return

            def run() -> None:
                try:
                    started = service.handle(number)
                    service.state.complete_run(
                        "github-webhook",
                        f"delivery:{delivery}",
                        metadata={"started": started},
                    )
                    from dev_agents.visualize import schedule_report_refresh

                    schedule_report_refresh(project_name, project)
                    _log(
                        f"handled delivery={delivery} event={event} action={action} pr={number} started={started}"
                    )
                except Exception as error:  # noqa: BLE001 - daemon must log worker failures
                    service.state.complete_run(
                        "github-webhook", f"delivery:{delivery}", status="failed", error=str(error)
                    )
                    from dev_agents.visualize import schedule_report_refresh

                    schedule_report_refresh(project_name, project)
                    _log(
                        f"failed delivery={delivery} event={event} action={action} pr={number} error={error}"
                    )

            Thread(target=run, daemon=True).start()
            _log(f"accepted delivery={delivery} event={event} action={action} pr={number}")
            self.send_response(202)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    def release_comms_scheduler() -> None:
        """Resume due publication batches without holding a worker thread asleep."""
        comms_config = project.release_comms
        if comms_config is None or not comms_config.auto_publish:
            return
        state_path = state_database_path(
            comms_config.state_path, project.repo / ".dev-agents/release-comms-state.db"
        )
        claims = StateRepository(state_path, project_name, project.repo)
        active: set[str] = set()
        active_lock = Lock()
        interval = max(5, comms_config.scheduler_poll_seconds)
        last_reddit_sync = 0.0
        while True:
            time.sleep(interval)
            now = datetime.now(UTC)
            if time.time() - last_reddit_sync >= 300:
                last_reddit_sync = time.time()
                try:
                    from dev_agents.workflows.reddit import sync_reddit_status

                    reconciled = sync_reddit_status(
                        project=project,
                        project_name=project_name,
                        repository=claims,
                    )
                    if reconciled:
                        _log(f"reconciled {len(reconciled)} reddit posts")
                except Exception as error:  # noqa: BLE001 - scheduler keeps serving
                    _log(f"periodic reddit sync error: {error}")

            for run in claims.list_runs("release-comms", limit=100):
                if run.status != "scheduled":
                    continue
                try:
                    next_at = datetime.fromisoformat(
                        str(run.metadata.get("next_publication_at", ""))
                    )
                except ValueError:
                    next_at = now
                if next_at > now:
                    continue
                with active_lock:
                    if run.run_id in active:
                        continue
                    active.add(run.run_id)

                def resume(run_id: str = run.run_id) -> None:
                    try:
                        def comms_phase(event: str, payload: dict[str, Any]) -> None:
                            detail = " ".join(
                                f"{key}={value}" for key, value in payload.items()
                            )
                            _log(
                                f"release-comms run_id={run_id} phase={event} {detail}".rstrip()
                            )

                        result = run_release_comms(
                            project,
                            project_name,
                            run_id,
                            dry_run=False,
                            publish_approved=True,
                            on_event=comms_phase,
                        )
                        _log(
                            f"resumed release-comms run_id={run_id} completed={result.completed} scheduled={result.scheduled}"
                        )
                    except Exception as error:  # noqa: BLE001 - scheduler keeps serving
                        _log(f"release-comms resume failed run_id={run_id} error={error}")
                    finally:
                        with active_lock:
                            active.discard(run_id)

                Thread(target=resume, daemon=True).start()

    _log(f"listening on 127.0.0.1:{config.port}{config.webhook_path} for {project.github}")
    Thread(target=service.reconcile_loop, daemon=True).start()
    if issue_service is not None:
        Thread(target=issue_service.reconcile, daemon=True).start()
        Thread(target=issue_service.reconcile_loop, daemon=True).start()
    Thread(target=release_comms_scheduler, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", config.port), Handler).serve_forever()


def serve_project(config_path: Path, project_name: str) -> None:
    project = select_project(load_projects_config(config_path), project_name)
    if project.pr_fixer is None or project.github is None:
        raise ConfigError(f"Project {project_name!r} requires github and pr_fixer configuration")
    serve(project_name, project, project.pr_fixer)
