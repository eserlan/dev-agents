"""Structured, read-only PR auditing independent from repair execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AUDIT_LENSES = {"premise-inversion", "tests", "resource-budget", "security-privacy", "lifecycle", "accessibility"}


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["critical", "high", "medium", "low"]
    confidence: Literal["high"]
    category: str = Field(min_length=1)
    location: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    introduced_by: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    remediation: str = Field(min_length=1)


class AuditCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    status: Literal["pass", "finding", "not_applicable", "unverified"]
    evidence: str = Field(min_length=1)


class AuditFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    description: str = Field(min_length=1, description="What this PR changes in the file, in one short phrase.")


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, description="One or two sentences on what the PR changes, for a reviewer.")
    files: list[AuditFile] = Field(description="Every file the reviewed diff changes.")
    verdict: Literal["clean", "findings", "incomplete"]
    findings: list[AuditFinding]
    categories_checked: list[str] = Field(min_length=1)
    checks: list[AuditCheck] = Field(min_length=1)
    limitations: list[str]

    def review_report(self) -> dict[str, Any]:
        """Preserve the existing review-chain report shape for comments and verification."""
        return {
            "verdict": self.verdict,
            "findings": [finding.model_dump() for finding in self.findings],
            "categories_checked": self.categories_checked,
            "validation": ["Independent read-only audit; no repair or test execution"],
            "fixes": [],
            "pr_summary": self.summary,
            "pr_files": [file.model_dump() for file in self.files],
        }


_FILLER_WORDS = {"lens", "and", "the"}


def canonical_lens(category: str) -> str | None:
    """Map a check category the auditor paraphrased to its lens identifier.

    The model often writes "Security and privacy lens" or "tests and assertion completeness"
    for `security-privacy` or `tests`. Only a category that is the lens identifier, or starts
    with it as whole words, counts; "architecture and privacy" does not cover security-privacy.
    """
    words = [word for word in re.split(r"[^a-z0-9]+", category.lower()) if word and word not in _FILLER_WORDS]
    normalised = "-".join(words)
    for lens in AUDIT_LENSES:
        if normalised == lens or normalised.startswith(f"{lens}-"):
            return lens
    return None


def parse_audit_report(path: Path, *, initial: bool) -> AuditReport:
    report = AuditReport.model_validate_json(path.read_text(encoding="utf-8"))
    for check in report.checks:
        check.category = canonical_lens(check.category) or check.category
    if initial and not AUDIT_LENSES.issubset({check.category for check in report.checks}):
        raise ValueError("initial audit must explicitly assess all adversarial lenses")
    if report.verdict == "clean" and (report.findings or any(check.status in {"finding", "unverified"} for check in report.checks)):
        raise ValueError("clean audit cannot contain findings or unverified coverage")
    if report.verdict == "findings" and not report.findings:
        raise ValueError("findings verdict requires evidence-backed findings")
    if any(check.status == "finding" for check in report.checks) and not report.findings:
        raise ValueError("a failed audit check requires an actionable finding")
    return report


def audit_telemetry(report: AuditReport, *, run_id: str | None, number: int, head_sha: str, review_round: int) -> dict[str, Any]:
    return {
        "run_id": run_id, "pr_number": number, "reviewed_head_sha": head_sha,
        "review_round": review_round, "stage": "auditor", "verdict": report.verdict,
        "findings_count": len(report.findings), "categories_checked": report.categories_checked,
        "checks": [check.model_dump() for check in report.checks],
        "copilot_overlap": {"status": "not_assessed"},
        "human_overlap": {"status": "not_assessed"},
    }


def audit_json_schema() -> str:
    return json.dumps(AuditReport.model_json_schema(), indent=2)


def compare_reference_comments(
    audit: dict[str, Any], comments: list[dict[str, Any]], head_sha: str,
    *, human_reviewers: set[str] | None = None,
) -> dict[str, Any]:
    """Track potential misses using same-revision locations, not asserted semantic equivalence."""
    reference = [
        comment for comment in comments
        if comment.get("original_commit_id", comment.get("commit_id")) == head_sha
        and (
            "copilot" in str(comment.get("user", {}).get("login", "")).lower()
            if human_reviewers is None else (
                str(comment.get("user", {}).get("login", "")) in human_reviewers
                and comment.get("user", {}).get("type") != "Bot"
            )
        )
    ]
    overlaps: list[int] = []
    potential_misses: list[dict[str, Any]] = []
    for comment in reference:
        path = str(comment.get("path", ""))
        line = comment.get("original_line") or comment.get("line")
        matched = False
        for finding in audit.get("findings", []):
            location = str(finding.get("location", ""))
            position = re.search(re.escape(path) + r":(\d+)", location) if path else None
            if position and isinstance(line, int) and abs(int(position.group(1)) - line) <= 5:
                matched = True
                break
        if matched:
            overlaps.append(comment["id"])
        else:
            potential_misses.append({"comment_id": comment.get("id"), "path": path, "line": line})
    return {
        "status": "location_comparison" if reference else "no_same_revision_reference",
        "reviewed_head_sha": head_sha, "reference_count": len(reference),
        "overlap_count": len(overlaps), "overlapping_comment_ids": overlaps,
        "potential_misses": potential_misses,
        "potential_false_negative_rate": len(potential_misses) / len(reference) if reference else None,
        "limitation": "Same-revision positional overlap is a diagnostic proxy, not semantic adjudication or a confirmed false-negative rate.",
    }
