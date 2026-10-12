"""PR comment body rendering and the GitHub comment publish/update helpers.

See the module docstring in ``github_ops.py`` for why ``_run`` and ``repository_slug``
calls here go through the ``dev_agents.pr_fixer`` package object (``_pkg``) instead of a
plain local import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dev_agents.pr_fixer as _pkg

from ._shared import MAX_INTERNAL_REVIEW_ROUNDS, _log
from .github_ops import _findings_summary


def _external_agent_pause_body(
    number: int, head_sha: str, author: str, resume_label: str
) -> str:
    return f"""<!-- dev-agents:pr-external-agent-paused run=pr-review-pause-{number}-{head_sha} -->
### 🤖 Dev-agents paused

Automation is paused for PR #{number} because its latest commit was pushed by `{author}`.

Apply the `{resume_label}` label when the external agent is finished and you want dev-agents to resume review/fix automation.
"""


def _auto_pause_body(number: int, run_id: str, attempt: int, reason: str) -> str:
    return f"""<!-- dev-agents:pr-auto-paused run={run_id} -->
### 🤖 Dev-agents auto-paused

PR #{number} failed {attempt} consecutive automation attempts ({reason}), so dev-agents applied
the `paused` label to stop retrying and posting duplicate comments here.

Remove the `paused` label once the underlying issue is resolved to resume automation.
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
        "the independent read-only audit (general defects and bundled repository-specific guidance)"
        if review_round == 0
        else "the one allowed targeted post-fix verification of the changed diff and prior findings"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review started

Luna has started {scope} for PR #{number}. A separate fixer runs only after evidence-backed findings.

- PR head: `{meta.get('headRefOid', 'unknown')}`
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_progress_body(
    number: int,
    run_id: str,
    meta: dict[str, Any],
    report_url: str | None,
    review_round: int,
    elapsed_seconds: int,
) -> str:
    """A periodic progress note so a long review doesn't read as hung.

    Reuses the same `pr-review` marker as `_review_started_body` for
    correlation; publishing posts it as a new comment.
    """
    minutes = max(1, elapsed_seconds // 60)
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review in progress

Still working on PR #{number} -- {minutes}m elapsed so far.

- PR head: `{meta.get('headRefOid', 'unknown')}`
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
- Run: `{run_id}`
{_report_line(report_url)}
"""


def _fixes_markdown(review_report: dict[str, Any]) -> str:
    """List each concrete repair the fixer reported, one per line."""
    fixes = [item for item in review_report.get("fixes") or [] if isinstance(item, dict)]
    lines = [
        f"- `{item['location']}`: {item.get('summary', '')}" if item.get("location") else f"- {item.get('summary', '')}"
        for item in fixes[:MAX_LISTED_FINDINGS]
    ]
    if len(fixes) > MAX_LISTED_FINDINGS:
        lines.append(f"- …and {len(fixes) - MAX_LISTED_FINDINGS} more in the run report.")
    return "\n".join(lines) + "\n" if lines else ""


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
    fix_pushed: bool | None = None,
    superseded_by: str | None = None,
    follow_up: bool | None = None,
) -> str:
    """Render the final state into the same comment used for review progress."""
    review_report = report.get("review_report")
    review_report = review_report if isinstance(review_report, dict) else {}
    verdict = review_report.get("verdict")
    pushed_fix = (
        fix_pushed
        if fix_pushed is not None
        else bool(new_sha and new_sha != old_sha)
    )
    round_line = f"- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`"
    if superseded_by:
        # Only pushes this run did not make reach here: a run's own fix push is finalized as
        # "fixes pushed". Say so plainly, and leave out the discarded results.
        return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review superseded

Someone else pushed `{superseded_by[:12]}` to this branch while the review was running, so its
results applied to an outdated head and were discarded. Nothing was pushed by this run. The new
head will receive a full review.

- PR: #{number}
{round_line}
- Run: `{run_id}`
{_report_line(report_url)}
"""
    if failure:
        heading = "### 🤖 Luna review failed"
        outcome = failure
    elif pushed_fix:
        heading = "### 🤖 Luna fixes pushed"
        outcome = "Luna fixed the review findings and pushed the result."
        if follow_up:
            outcome += (
                f" Because that commit changed the PR, a targeted verification review of it"
                f" (round {review_round + 2}/{MAX_INTERNAL_REVIEW_ROUNDS}) starts next."
                " That review is expected; it checks the fix, not a new problem."
            )
    elif verdict == "clean":
        heading = "### 🤖 Luna review completed — clean"
        outcome = "No actionable defects were found."
    else:
        heading = "### 🤖 Luna review completed"
        outcome = "The review completed without pushing a code change."
    shown_files = changed_files or []
    files_line = ", ".join(f"`{path}`" for path in shown_files[:12]) or "None"
    if len(shown_files) > 12:
        files_line += f" (+{len(shown_files) - 12} more)"
    commit_line = "The PR head was unchanged."
    if pushed_fix:
        commit_line = f"Pushed fix commit `{new_sha[:12]}`."
    validation_line = validation or "See the run report for validation details."
    fixes = _fixes_markdown(review_report)
    fixes_section = f"\n**What was fixed**\n\n{fixes}" if fixes else ""
    findings_line = (
        ""
        if pushed_fix and fixes
        else f"- Findings: {_findings_summary(review_report, 'No actionable defects found.')}\n"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
{heading}

{outcome}
{fixes_section}
- PR: #{number}
{round_line}
{findings_line}- {commit_line}
- Changed files: {files_line}
- Validation: {validation_line}
- Run: `{run_id}`
{_report_line(report_url)}
"""


MAX_LISTED_FINDINGS = 10


def _findings_markdown(report: dict[str, Any]) -> str:
    """Render each structured finding as its own reviewable list item."""
    findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
    if not findings:
        return "No actionable defects found.\n"
    lines = []
    for index, item in enumerate(findings[:MAX_LISTED_FINDINGS], start=1):
        severity = str(item.get("severity") or "unknown").upper()
        category = f" · {item['category']}" if item.get("category") else ""
        lines.append(f"{index}. **{severity}**{category} — `{item.get('location', '?')}`")
        lines.append(f"   - Impact: {item.get('impact', '')}")
        if item.get("evidence"):
            lines.append(f"   - Evidence: {item['evidence']}")
        if item.get("remediation"):
            lines.append(f"   - Suggested fix: {item['remediation']}")
    if len(findings) > MAX_LISTED_FINDINGS:
        lines.append(f"\n…and {len(findings) - MAX_LISTED_FINDINGS} more in the run report.")
    return "\n".join(lines) + "\n"


def _review_findings_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
    head_sha: str | None = None,
) -> str:
    scope = "independent audit" if review_round == 0 else "targeted post-fix verification"
    count = len([item for item in report.get("findings") or [] if isinstance(item, dict)])
    head_line = f"- PR head: `{head_sha}`\n" if head_sha else ""
    unverified = [
        check for check in report.get("checks") or []
        if isinstance(check, dict) and check.get("status") == "unverified"
    ]
    unverified_section = (
        "**Not verified, needs a human check** (auto-merge stays blocked until resolved):\n\n"
        + "\n".join(f"- {check.get('category', '?')}: {check.get('evidence', '')}" for check in unverified)
        + "\n\n"
        if unverified
        else ""
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna review findings

Luna's {scope} of PR #{number} found {count} issue{"" if count == 1 else "s"}:

{_findings_markdown(report)}
{unverified_section}{head_line}- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
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
    count = len([item for item in report.get("findings") or [] if isinstance(item, dict)])
    subject = (
        f"the {count} finding{'' if count == 1 else 's'} reported above"
        if count
        else "the concrete findings identified during the review"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 Luna fixes started

Luna is applying the smallest correct fixes for {subject} on PR #{number}.

- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
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


def _publish_pr_run_comment(
    repo: Path, number: int, run_id: str, phase: str, body: str
) -> bool:
    """Post a new lifecycle comment for a PR run; never overwrites an existing one."""
    try:
        slug = _pkg.repository_slug(repo)
        _pkg._run(
            repo,
            "gh",
            "api",
            f"repos/{slug}/issues/{number}/comments",
            "-f",
            f"body={body}",
        )
    except RuntimeError as error:
        _log(f"run-comment-failed pr={number} phase={phase} error={error}")
        return False
    return True


def _publish_fix_summary(repo: Path, number: int, run_id: str, body: str) -> bool:
    """Post the bot's summary as a new comment for one fixer run."""
    try:
        slug = _pkg.repository_slug(repo)
        _pkg._run(
            repo,
            "gh",
            "api",
            f"repos/{slug}/issues/{number}/comments",
            "-f",
            f"body={body}",
        )
    except RuntimeError as error:
        _log(f"summary-comment-failed pr={number} error={error}")
        return False
    return True
