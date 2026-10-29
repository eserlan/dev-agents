import json
from pathlib import Path

import pytest

from dev_agents.pr_fixer.audit import AUDIT_LENSES, compare_reference_comments, parse_audit_report

SUMMARY = {"summary": "Adds a feature.", "files": [{"path": "feature.txt", "description": "Adds the feature"}]}


def payload() -> dict:
    return {
        "summary": "Adds a feature.", "files": [{"path": "feature.txt", "description": "Adds the feature"}],
        "verdict": "clean", "findings": [], "categories_checked": sorted(AUDIT_LENSES),
        "checks": [{"category": category, "status": "pass", "evidence": "Inspected the affected execution path"}
                   for category in sorted(AUDIT_LENSES)], "limitations": [],
    }


def test_clean_requires_complete_noncontradictory_audit(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    value = payload()
    value["checks"][0]["status"] = "unverified"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="clean audit"):
        parse_audit_report(path, initial=True)
    value["verdict"] = "incomplete"
    path.write_text(json.dumps(value))
    assert parse_audit_report(path, initial=True).verdict == "incomplete"


def test_missing_lens_cannot_silently_pass_initial_audit(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    value = payload()
    value["checks"].pop()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="all adversarial lenses"):
        parse_audit_report(path, initial=True)
    assert parse_audit_report(path, initial=False).verdict == "clean"


def test_finding_requires_high_confidence_and_evidence(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    value = payload()
    value["verdict"] = "findings"
    value["findings"] = [{
        "severity": "high", "confidence": "low", "category": "privacy", "location": "view.ts:42",
        "evidence": "", "impact": "Remote request leaks user activity", "remediation": "Reject remote images",
    }]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        parse_audit_report(path, initial=True)


def test_reference_comparison_filters_revision_and_labels_proxy_limits() -> None:
    audit = {"findings": [{"location": "view.ts:42"}]}
    comments = [
        {"id": 1, "original_commit_id": "reviewed", "path": "view.ts", "original_line": 43, "user": {"login": "copilot[bot]"}},
        {"id": 2, "original_commit_id": "reviewed", "path": "other.ts", "original_line": 8, "user": {"login": "copilot[bot]"}},
        {"id": 3, "original_commit_id": "newer", "path": "other.ts", "original_line": 8, "user": {"login": "copilot[bot]"}},
    ]
    comparison = compare_reference_comments(audit, comments, "reviewed")
    assert comparison["reference_count"] == 2
    assert comparison["overlap_count"] == 1
    assert comparison["potential_false_negative_rate"] == 0.5
    assert comparison["potential_misses"] == [{"comment_id": 2, "path": "other.ts", "line": 8}]
    assert "not semantic" in comparison["limitation"]


def test_human_reference_comments_are_separate_from_bots() -> None:
    comments = [
        {"id": 1, "original_commit_id": "reviewed", "path": "view.ts", "line": 42, "user": {"login": "alice", "type": "User"}},
        {"id": 2, "original_commit_id": "reviewed", "path": "view.ts", "line": 42, "user": {"login": "automated", "type": "Bot"}},
    ]
    comparison = compare_reference_comments(
        {"findings": [{"location": "view.ts:42"}]}, comments, "reviewed", human_reviewers={"alice", "automated"},
    )
    assert comparison["reference_count"] == comparison["overlap_count"] == 1


def test_paraphrased_lens_categories_are_canonicalised(tmp_path: Path) -> None:
    paraphrased = ["Premise-inversion lens", "tests and assertion completeness", "Resource-budget lens",
                   "Security and privacy lens", "lifecycle", "Accessibility lens"]
    checks = [{"category": name, "status": "pass", "evidence": "Traced the changed path"} for name in paraphrased]
    checks.append({"category": "Codex-Cryptica architecture and privacy", "status": "pass", "evidence": "Checked"})
    path = tmp_path / "audit.json"
    path.write_text(json.dumps({**SUMMARY, "verdict": "clean", "findings": [], "categories_checked": ["general"],
                                "checks": checks, "limitations": []}))

    report = parse_audit_report(path, initial=True)

    assert [check.category for check in report.checks] == [
        "premise-inversion", "tests", "resource-budget", "security-privacy", "lifecycle", "accessibility",
        "Codex-Cryptica architecture and privacy",
    ]


def test_a_lens_merely_mentioned_inside_another_category_does_not_count(tmp_path: Path) -> None:
    checks = [{"category": lens, "status": "pass", "evidence": "Traced"} for lens in sorted(AUDIT_LENSES - {"security-privacy"})]
    checks.append({"category": "architecture and privacy", "status": "pass", "evidence": "Checked"})
    path = tmp_path / "audit.json"
    path.write_text(json.dumps({**SUMMARY, "verdict": "clean", "findings": [], "categories_checked": ["general"],
                                "checks": checks, "limitations": []}))

    with pytest.raises(ValueError, match="all adversarial lenses"):
        parse_audit_report(path, initial=True)


@pytest.mark.parametrize("missing", ["requirement", "trigger", "introduced_by"])
def test_blocking_findings_require_contract_and_regression_evidence(tmp_path: Path, missing: str) -> None:
    finding = {
        "severity": "medium", "confidence": "high", "category": "correctness",
        "location": "input.ts:4", "requirement": "spec.md:8 rejects invalid IDs",
        "trigger": "An unknown ID", "introduced_by": "New unchecked lookup at input.ts:4",
        "evidence": "Lookup returns undefined", "impact": "Request crashes",
        "remediation": "Check ID before lookup",
    }
    value = payload()
    value.update(verdict="findings", findings=[finding])
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(value))
    assert parse_audit_report(path, initial=True).findings[0].requirement
    del finding[missing]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        parse_audit_report(path, initial=True)
