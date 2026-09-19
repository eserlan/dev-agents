"""Webhook-driven, project-configured pull-request remediation service.

This package was split out of a single 2200+ line ``pr_fixer.py`` module by
responsibility (github_ops, comments, prompts, local_state, service). The
daemon-level wiring (HTTP server, webhook routing, the release-comms and
issue-fixer scheduling) lives in ``dev_agents.daemon`` instead, since it
orchestrates more than just PR fixing.

This file re-exports the ``pr_fixer``-specific namespace so every existing
``from dev_agents.pr_fixer import X`` keeps working unchanged, including the
handful of names tests monkeypatch by their dotted path on this package
(``_run``, ``_feedback``, ``_publish_pr_run_comment``, ``repository_slug``,
``Thread``) -- see the docstring in ``github_ops.py`` for why those specific
calls route back through this package object instead of a plain import.
"""

from __future__ import annotations

from threading import Thread

from dev_agents.runtime import repository_slug

from ._shared import (
    _ACTIONS,
    MAX_BODY_BYTES,
    MAX_INTERNAL_REVIEW_ROUNDS,
    PR_FIX_PROVIDER,
    REVIEW_REPORT_BEGIN,
    REVIEW_REPORT_END,
    REVIEW_SKILL_CANDIDATES,
    SHARED_PR_FIX_SKILL,
    _log,
)
from .comments import (
    _auto_pause_body,
    _external_agent_pause_body,
    _fix_summary_body,
    _publish_fix_summary,
    _publish_pr_run_comment,
    _report_line,
    _review_completed_body,
    _review_findings_body,
    _review_fixes_started_body,
    _review_progress_body,
    _review_started_body,
)
from .github_ops import (
    _agent_report,
    _external_agent_commit,
    _feedback,
    _findings_summary,
    _has_copilot_review,
    _is_issue_fix_pr,
    _normalise_review_report,
    _pull_request_checks,
    _review_is_due,
    _run,
)
from .local_state import _cleanup_artifacts, _load_state, _save_state, _state_path
from .prompts import _prompt, _review_prompt, _review_skill, _target_validation_instructions
from .service import PrFixerRunState, PrFixerService

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_INTERNAL_REVIEW_ROUNDS",
    "PR_FIX_PROVIDER",
    "REVIEW_REPORT_BEGIN",
    "REVIEW_REPORT_END",
    "REVIEW_SKILL_CANDIDATES",
    "SHARED_PR_FIX_SKILL",
    "_ACTIONS",
    "PrFixerRunState",
    "PrFixerService",
    "Thread",
    "_agent_report",
    "_auto_pause_body",
    "_cleanup_artifacts",
    "_external_agent_commit",
    "_external_agent_pause_body",
    "_feedback",
    "_findings_summary",
    "_fix_summary_body",
    "_has_copilot_review",
    "_is_issue_fix_pr",
    "_load_state",
    "_log",
    "_normalise_review_report",
    "_prompt",
    "_publish_fix_summary",
    "_publish_pr_run_comment",
    "_pull_request_checks",
    "_report_line",
    "_review_completed_body",
    "_review_findings_body",
    "_review_fixes_started_body",
    "_review_is_due",
    "_review_progress_body",
    "_review_prompt",
    "_review_skill",
    "_review_started_body",
    "_run",
    "_save_state",
    "_state_path",
    "_target_validation_instructions",
    "repository_slug",
]
