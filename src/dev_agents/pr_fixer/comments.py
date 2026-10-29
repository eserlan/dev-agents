"""PR comment body rendering and the GitHub comment publish/update helpers.

See the module docstring in ``github_ops.py`` for why ``_run`` and ``repository_slug``
calls here go through the ``dev_agents.pr_fixer`` package object (``_pkg``) instead of a
plain local import.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import dev_agents.pr_fixer as _pkg
from dev_agents.runtime.agent import CODEX_MODEL

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


def _model_line(model: str, effort: str | None) -> str:
    """Name the model that did the work, so a comment never implies a different agent."""
    return f"- Model: `{model}`" + (f" ({effort} effort)" if effort else "") + "\n"


def _report_line(report_url: str | None) -> str:
    return f"- Report: [Open this run in dev-agents report]({report_url})\n" if report_url else ""


def _review_started_body(
    number: int,
    run_id: str,
    meta: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
) -> str:
    scope = (
        "the independent read-only audit (general defects and bundled repository-specific guidance)"
        if review_round == 0
        else "the one allowed targeted post-fix verification of the changed diff and prior findings"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 {model} review started

{model} has started {scope} for PR #{number}. A separate fixer runs only after evidence-backed findings.

- PR head: `{meta.get('headRefOid', 'unknown')}`
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
{_model_line(model, effort)}- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_progress_body(
    number: int,
    run_id: str,
    meta: dict[str, Any],
    report_url: str | None,
    review_round: int,
    elapsed_seconds: int,
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
) -> str:
    """A periodic progress note so a long review doesn't read as hung.

    Reuses the same `pr-review` marker as `_review_started_body`, so publishing updates the
    run's status comment instead of adding one.
    """
    minutes = max(1, elapsed_seconds // 60)
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 {model} review in progress

Still working on PR #{number} -- {minutes}m elapsed so far.

- PR head: `{meta.get('headRefOid', 'unknown')}`
- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
{_model_line(model, effort)}- Run: `{run_id}`
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
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
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
### 🤖 {model} review superseded

Someone else pushed `{superseded_by[:12]}` to this branch while the review was running, so its
results applied to an outdated head and were discarded. Nothing was pushed by this run. The new
head will receive a full review.

- PR: #{number}
{round_line}
{_model_line(model, effort)}- Run: `{run_id}`
{_report_line(report_url)}
"""
    handoff_agent = report.get("handoff_agent")
    if failure:
        heading = f"### 🤖 {model} review failed"
        outcome = failure
    elif handoff_agent and not pushed_fix:
        heading = f"### 🤖 {model} review completed — findings left for {handoff_agent}"
        outcome = (
            f"This PR is driven by {handoff_agent}, so {model} left the findings above for it to fix"
            " instead of pushing its own repairs. Its next push gets a fresh review; auto-merge"
            " waits for a clean one."
        )
    elif pushed_fix:
        heading = f"### 🤖 {model} fixes pushed"
        outcome = f"{model} fixed the review findings and pushed the result."
        if follow_up:
            outcome += (
                f" Because that commit changed the PR, a targeted verification review of it"
                f" (round {review_round + 2}/{MAX_INTERNAL_REVIEW_ROUNDS}) starts next."
                " That review is expected; it checks the fix, not a new problem."
            )
    elif verdict == "clean":
        heading = f"### 🤖 {model} review completed — clean"
        outcome = "No actionable defects were found."
    else:
        heading = f"### 🤖 {model} review completed"
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
    what_changed = _what_changed_section(review_report.get("pr_summary"), review_report.get("pr_files"))
    what_changed = f"\n{what_changed}" if what_changed else ""
    return f"""<!-- dev-agents:pr-review run={run_id} -->
{heading}

{outcome}
{fixes_section}{what_changed}
- PR: #{number}
{round_line}
{findings_line}- {commit_line}
- Changed files: {files_line}
- Validation: {validation_line}
{_model_line(model, effort)}- Run: `{run_id}`
{_report_line(report_url)}
"""


MAX_LISTED_FINDINGS = 10
MAX_LISTED_FILES = 30

# GitHub's Primer palette: danger-emphasis darker for critical, then danger, attention, accent.
SEVERITY_COLOURS = {"critical": "82071e", "high": "cf222e", "medium": "bf8700", "low": "0969da"}
SEVERITY_ORDER = ("critical", "high", "medium", "low")


def _severity_badge(severity: object) -> str:
    """A coloured severity label; unknown severities fall back to plain bold text."""
    name = str(severity or "unknown").lower()
    colour = SEVERITY_COLOURS.get(name)
    if not colour:
        return f"**{name.upper()}**"
    return f"![{name.upper()}](https://img.shields.io/badge/{name.upper()}-{colour}?style=flat-square)"


def _verdict_line(findings: list[dict[str, Any]]) -> str:
    severities = {str(item.get("severity") or "").lower() for item in findings}
    if severities & {"critical", "high"}:
        return "🔴 Changes required"
    if "medium" in severities:
        return "🟡 Changes recommended"
    return "🔵 Minor changes suggested"


def _severity_counts(findings: list[dict[str, Any]]) -> str:
    counts = [
        f"{sum(1 for item in findings if str(item.get('severity') or '').lower() == name)} {_severity_badge(name)}"
        for name in SEVERITY_ORDER
        if any(str(item.get("severity") or "").lower() == name for item in findings)
    ]
    return " · ".join(counts) or str(len(findings))


def _findings_markdown(report: dict[str, Any]) -> str:
    """Render each structured finding as its own reviewable list item, most severe first."""
    findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
    if not findings:
        return "No actionable defects found.\n"
    rank = {name: index for index, name in enumerate(SEVERITY_ORDER)}
    findings = sorted(findings, key=lambda item: rank.get(str(item.get("severity") or "").lower(), len(rank)))
    lines = []
    for index, item in enumerate(findings[:MAX_LISTED_FINDINGS], start=1):
        category = f" **{item['category']}**" if item.get("category") else ""
        lines.append(f"{index}. {_severity_badge(item.get('severity'))}{category} — `{item.get('location', '?')}`")
        lines.append(f"   - Impact: {item.get('impact', '')}")
        if item.get("evidence"):
            lines.append(f"   - Evidence: {item['evidence']}")
        if item.get("remediation"):
            lines.append(f"   - Suggested fix: {item['remediation']}")
    if len(findings) > MAX_LISTED_FINDINGS:
        lines.append(f"\n…and {len(findings) - MAX_LISTED_FINDINGS} more in the run report.")
    return "\n".join(lines) + "\n"


def _table_cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _what_changed_section(summary: object, files: object) -> str:
    """Collapsed description of the reviewed diff, or nothing when the audit gave none."""
    rows = [item for item in files or [] if isinstance(item, dict) and item.get("path")] if isinstance(files, list) else []
    if not (isinstance(summary, str) and summary.strip()) and not rows:
        return ""
    table = ""
    if rows:
        table = "\n| File | Description |\n| ---- | ----------- |\n" + "".join(
            f"| `{_table_cell(item['path'])}` | {_table_cell(item.get('description', ''))} |\n"
            for item in rows[:MAX_LISTED_FILES]
        )
        if len(rows) > MAX_LISTED_FILES:
            table += f"\n…and {len(rows) - MAX_LISTED_FILES} more files.\n"
    text = summary.strip() if isinstance(summary, str) else ""
    return f"""<details>
<summary><strong>What changed in this PR</strong></summary>

{text}
{table}</details>
"""


def _review_findings_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
    head_sha: str | None = None,
    claim_window_seconds: int = 60,
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
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
    findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
    return f"""<!-- dev-agents:pr-review-findings run={run_id} -->
### 🤖 {model} review findings

#### {_verdict_line(findings)}

{model}'s {scope} of PR #{number} found {count} issue{"" if count == 1 else "s"}.

**Findings:** {_severity_counts(findings)}

<details open>
<summary><strong>Open ({count})</strong></summary>

{_findings_markdown(report)}
</details>

{_what_changed_section(report.get("summary"), report.get("files"))}
{unverified_section}Dev-agents fixes these findings by default.

<details>
<summary>Claim this run for an external agent</summary>

An active external development process can claim this run before the fixer starts (it waits at
least {claim_window_seconds} seconds) by posting exactly:

`/dev-agents claim {run_id}`

Claims from repository owners, members or collaborators pause the fixer for up to 30 minutes.
A push starts a fresh review; without a push, dev-agents resumes fixing. Attribution alone
does not claim the work.
</details>

{head_line}- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
{_model_line(model, effort)}- Run: `{run_id}`
{_report_line(report_url)}
"""


def _review_fixes_started_body(
    number: int,
    run_id: str,
    report: dict[str, Any],
    report_url: str | None = None,
    review_round: int = 0,
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
) -> str:
    count = len([item for item in report.get("findings") or [] if isinstance(item, dict)])
    subject = (
        f"the {count} finding{'' if count == 1 else 's'} in the findings comment"
        if count
        else "the concrete findings identified during the review"
    )
    return f"""<!-- dev-agents:pr-review run={run_id} -->
### 🤖 {model} fixes started

{model} is applying the smallest correct fixes for {subject} on PR #{number}.

- Review round: `{review_round + 1}/{MAX_INTERNAL_REVIEW_ROUNDS}`
{_model_line(model, effort)}- Run: `{run_id}`
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
    *,
    model: str = CODEX_MODEL,
    effort: str | None = None,
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
{details_line}- Validation: the triggering feedback was re-checked successfully.
- Files in the resulting PR diff: {file_text}
{_model_line(model, effort)}- Run: `{run_id}`
{_report_line(report_url)}{report_line}
"""


STATUS_PHASES = frozenset({"review-started", "review-progress", "review-fixes-started", "review-final"})
HISTORY_MARKER = "<!-- dev-agents:history -->"


def _status_heading(body: str) -> str:
    heading = next((line for line in body.splitlines() if line.startswith("### ")), "### Update")
    return heading.removeprefix("### ").removeprefix("🤖 ").strip()


def _with_history(body: str, previous: str = "") -> str:
    """Append this phase to the status comment's run history, collapsing repeated progress notes."""
    entries = []
    if HISTORY_MARKER in previous:
        tail = previous.split(HISTORY_MARKER, 1)[1].split("</details>", 1)[0]
        entries = [line for line in tail.splitlines() if line.startswith("- ")]
    heading = _status_heading(body)
    entry = f"- {datetime.now().astimezone():%H:%M %Z} {heading}"
    if entries and entries[-1].split(" ", 3)[-1] == heading:
        entries[-1] = entry
    else:
        entries.append(entry)
    history = "\n".join(entries)
    return f"""{body.rstrip()}

<details><summary>Run history</summary>
{HISTORY_MARKER}

{history}
</details>
"""


def _run_status_comment(repo: Path, slug: str, number: int, run_id: str) -> dict[str, Any] | None:
    """The run's status comment: the earliest comment carrying both its marker and a history."""
    raw = _pkg._run(
        repo, "gh", "api", f"repos/{slug}/issues/{number}/comments", "--paginate", "--slurp"
    )
    pages = json.loads(raw) if raw else []
    marker = f"<!-- dev-agents:pr-review run={run_id} -->"
    for page in pages if isinstance(pages, list) else []:
        for comment in page if isinstance(page, list) else [page]:
            body = str(comment.get("body", "")) if isinstance(comment, dict) else ""
            if marker in body and HISTORY_MARKER in body and comment.get("id") is not None:
                return comment
    return None


def _publish_pr_run_comment(
    repo: Path, number: int, run_id: str, phase: str, body: str
) -> bool:
    """Publish a PR run's lifecycle update.

    Routine review phases (``STATUS_PHASES``) update one status comment per run, so a review
    no longer adds a comment for every step. Edits notify nobody, so findings, pauses, claims
    and failures that need a human still post new comments.
    """
    try:
        slug = _pkg.repository_slug(repo)
        existing = _run_status_comment(repo, slug, number, run_id) if phase in STATUS_PHASES else None
        if existing:
            _pkg._run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/comments/{existing['id']}",
                "--method",
                "PATCH",
                "-f",
                f"body={_with_history(body, str(existing.get('body', '')))}",
            )
        else:
            _pkg._run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/{number}/comments",
                "-f",
                f"body={_with_history(body) if phase in STATUS_PHASES else body}",
            )
    except (RuntimeError, json.JSONDecodeError) as error:
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
