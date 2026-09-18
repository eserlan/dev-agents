"""The per-PR LangGraph state machine: feedback collection, remediation, finalize.

Calls to ``_run``, ``_feedback``, ``_publish_pr_run_comment``, ``repository_slug``, and
``Thread`` route through the ``dev_agents.pr_fixer`` package object (``_pkg``) rather than
a plain local import -- see the module docstring in ``github_ops.py`` for why: tests
monkeypatch these by their dotted path on the package, which only affects lookups made
through the package object at call time.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import dev_agents.pr_fixer as _pkg
from dev_agents.config import PrFixerConfig, ProjectConfig
from dev_agents.runtime import (
    StateRepository,
    isolated_worktree,
    legacy_json_path,
    repo_git_lock,
    run_agent,
    run_with_fallback,
)

from ._shared import MAX_INTERNAL_REVIEW_ROUNDS, PR_FIX_PROVIDER, _log
from .comments import (
    _external_agent_pause_body,
    _fix_summary_body,
    _publish_fix_summary,
    _review_completed_body,
    _review_progress_body,
    _review_started_body,
)
from .github_ops import (
    _agent_report,
    _external_agent_commit,
    _has_copilot_review,
    _is_issue_fix_pr,
    _review_is_due,
)
from .local_state import _load_state, _state_path
from .prompts import _prompt, _review_prompt, _review_skill


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
        meta, keys = _pkg._feedback(self.project.repo, number)
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
            # This branch is re-entered by every webhook delivery for the PR (one
            # check_run.completed per CI job, plus periodic reconciliation) for as
            # long as the pause holds -- often many times for the same head SHA.
            # Only re-announce (log + the comment fetch/patch round-trip) the first
            # time we see this exact SHA paused; a new push still gets a fresh one.
            pause_key = f"external-agent-pause:{head_sha}"
            if self.state.unprocessed_feedback("pr-fixer", str(number), [pause_key]):
                _pkg._publish_pr_run_comment(
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
                self.state.mark_feedback_processed("pr-fixer", str(number), [pause_key])
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
            _pkg._publish_pr_run_comment(
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
            superseded_head = state.get("report", {}).get("superseded_by")
            if superseded_head:
                # Someone else pushed to this branch while the review was running.
                # Complete the claim (so it isn't left "running" forever) without
                # posting a misleading failure comment or setting any of the
                # chain/exhaustion metadata that _review_plan reads -- otherwise
                # this would either burn a review-chain round on nothing, or worse,
                # permanently block review of the very head that superseded us.
                # The webhook for that new push already queued its own run.
                self.state.record_event(
                    workflow,
                    state["run_id"],
                    "finalize",
                    "review_superseded",
                    metadata={
                        "reviewed_head_sha": state.get("meta", {}).get("headRefOid"),
                        "superseded_by": superseded_head,
                    },
                )
                self.state.complete_run(
                    workflow,
                    state["run_id"],
                    status="failed",
                    error="superseded by a concurrent push to the same branch",
                    metadata={
                        "reviewed_head_sha": state.get("meta", {}).get("headRefOid"),
                        "superseded_by": superseded_head,
                    },
                )
                _log(f"review-superseded pr={number} superseded_by={superseded_head}")
                return {"started": False}
            if not state.get("fixed"):
                recovery_metadata: dict[str, Any] = {}
                try:
                    refreshed_meta, _ = _pkg._feedback(self.project.repo, number)
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
                _pkg._publish_pr_run_comment(
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
            refreshed_meta, _ = _pkg._feedback(self.project.repo, number)
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
                for path in _pkg._run(
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
            comment_posted = _pkg._publish_pr_run_comment(
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
            superseded_head = state.get("report", {}).get("superseded_by")
            if superseded_head:
                # Same race as the review path: someone else pushed to this branch
                # while we were fixing it. The feedback keys we targeted stay
                # "unseen" (never marked processed), so the next reconcile/webhook
                # naturally retries against the new head -- nothing more to do here.
                self.state.record_event(
                    workflow,
                    state["run_id"],
                    "finalize",
                    "fix_superseded",
                    metadata={"superseded_by": superseded_head},
                )
                self.state.complete_run(
                    workflow,
                    state["run_id"],
                    status="failed",
                    error="superseded by a concurrent push to the same branch",
                    metadata={"superseded_by": superseded_head},
                )
                _log(f"fix-superseded pr={number} superseded_by={superseded_head}")
                return {"started": False}
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
        slug = _pkg.repository_slug(self.project.repo)
        for key in state["unseen"]:
            if key.startswith("comment:"):
                comment_id = key.split(":", 1)[1]
                _pkg._run(
                    self.project.repo,
                    "gh",
                    "api",
                    f"repos/{slug}/pulls/{number}/comments/{comment_id}/replies",
                    "-f",
                    "body=Processed by the PR fixer; verification completed.",
                    check=False,
                )
        refreshed_meta, refreshed = _pkg._feedback(self.project.repo, number)
        if all(key not in refreshed or key.startswith("comment:") for key in state["unseen"]):
            changed_files = [
                path
                for path in _pkg._run(
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
            with isolated_worktree(
                self.project.repo,
                root,
                branch,
                base,
                max_concurrent=self.config.max_concurrent_worktree_runs,
            ) as (worktree, conflicts):
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

                    def on_progress(elapsed_seconds: int) -> None:
                        # Fix-mode has no lifecycle "started" comment yet to patch
                        # in place, so this is review-only for now -- see
                        # _review_progress_body's docstring for why it reuses the
                        # same marker instead of posting a new comment each time.
                        if not review_only or run_id is None:
                            return
                        _pkg._publish_pr_run_comment(
                            self.project.repo,
                            number,
                            run_id,
                            "review-progress",
                            _review_progress_body(
                                number, run_id, meta or {}, None, review_round, elapsed_seconds
                            ),
                        )
                        _log(f"review-progress pr={number} elapsed_minutes={elapsed_seconds // 60}")

                    result = run_agent(
                        PR_FIX_PROVIDER,
                        prompt,
                        cwd=worktree,
                        log_path=log,
                        timeout_seconds=self.config.timeout_minutes * 60,
                        heartbeat_seconds=self.config.heartbeat_seconds,
                        reasoning_effort=self.config.reasoning_effort,
                        on_progress=on_progress,
                    )
                    report.update(_agent_report(log))
                    result_code = result.returncode
                    if result.timed_out:
                        _log(f"agent-timeout pr={number} provider={provider}")
                    _log(f"agent-finished pr={number} provider={provider} exit={result_code}")
                    return result

                original_head = str((meta or {}).get("headRefOid", ""))
                superseded_by: list[str] = []

                def accept_provider(_provider: str, result: Any) -> bool:
                    # A worktree shares refs/remotes/* with the common .git dir, so this
                    # fetch races the same way a fetch against the main checkout would --
                    # see repo_git_lock's docstring. The explicit refspec matters just
                    # as much here as in isolated_worktree's own fetch: a bare
                    # `fetch origin <branch>` only populates FETCH_HEAD, leaving the
                    # origin/{branch} ref this function compares against stale --
                    # confirmed live (PR #198 on LearBear): after 3 separate review
                    # attempts each freshly fetched, origin/{branch} still resolved to
                    # a commit two pushes behind the real remote tip, so every attempt
                    # falsely detected "supersession" against a push that never
                    # actually happened.
                    with repo_git_lock(self.project.repo):
                        _pkg._run(
                            worktree,
                            "git",
                            "fetch",
                            "origin",
                            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                            check=False,
                        )
                    origin_head = _pkg._run(worktree, "git", "rev-parse", f"origin/{branch}", check=False)
                    local_head = _pkg._run(worktree, "git", "rev-parse", "HEAD", check=False)
                    conflict_markers = _pkg._run(
                        worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False
                    )
                    worktree_clean = _pkg._run(worktree, "git", "status", "--porcelain") == ""

                    if (
                        local_head
                        and origin_head
                        and local_head != origin_head
                        and origin_head == original_head
                        and not conflict_markers
                        and worktree_clean
                    ):
                        # isolated_worktree merges origin/{base} into the branch during
                        # setup to surface conflicts before the agent starts. When that
                        # merge is clean but the agent finds nothing to fix, local HEAD
                        # ends up one commit ahead of origin/{branch} -- a housekeeping
                        # sync commit, not agent output -- and the agent has proven
                        # unreliable about remembering to push it itself (its own
                        # "clean" check is git status, which only catches uncommitted
                        # changes, not a committed-but-unpushed commit). Push it here
                        # mechanically, but only as a provable fast-forward (origin is
                        # an ancestor of local HEAD) so this can never discard or
                        # overwrite anything already on origin, and only when nothing
                        # else moved origin since we started (an actual divergence is
                        # handled by the supersession check below, not auto-pushed).
                        try:
                            _pkg._run(worktree, "git", "merge-base", "--is-ancestor", origin_head, "HEAD")
                        except RuntimeError:
                            pass
                        else:
                            with repo_git_lock(self.project.repo):
                                _pkg._run(worktree, "git", "push", "origin", f"HEAD:{branch}", check=False)
                            origin_head = _pkg._run(
                                worktree, "git", "rev-parse", f"origin/{branch}", check=False
                            )

                    pushed = local_head == origin_head
                    if (
                        not pushed
                        and origin_head
                        and original_head
                        and origin_head != original_head
                    ):
                        # A run can take 5-20+ minutes (an agent pass plus validation).
                        # If origin has moved to a head that is neither what we started
                        # from nor what we ourselves just pushed, someone else (an
                        # external agent like Jules, a human, or an overlapping run)
                        # pushed to this same branch while we were working. Our result
                        # was computed against an already-superseded head -- it is not
                        # evidence of a genuine review/fix failure, and must not consume
                        # a review-chain round or trip the exhaustion guard.
                        superseded_by.append(origin_head)
                    return bool(
                        result.returncode == 0 and pushed and not conflict_markers and worktree_clean
                    )

                # PR reviews and fixes are intentionally Codex-only. Keep the legacy providers
                # field for config compatibility, but never let another agent handle PR code.
                accepted = run_with_fallback((PR_FIX_PROVIDER,), run_provider, accept_provider)
                if superseded_by:
                    report["superseded_by"] = superseded_by[-1]
                    _log(
                        f"review-superseded pr={number} original_head={original_head} "
                        f"new_head={superseded_by[-1]}"
                    )
                elif accepted:
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
            meta, keys = _pkg._feedback(self.project.repo, number)
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
            _pkg._run(self.project.repo, "gh", "pr", "merge", str(number), "--auto", "--squash")
            verification = json.loads(
                _pkg._run(
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
            raw = _pkg._run(
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
            if not isinstance(prs, list):
                raise TypeError("GitHub returned a non-list PR response")
            eligible = 0
            for pr in prs:
                if any(label.get("name", "").lower() == "paused" for label in pr.get("labels", [])):
                    continue
                number = pr.get("number")
                if isinstance(number, int):
                    eligible += 1
                    _pkg.Thread(target=self._handle_reconciled, args=(number,), daemon=True).start()
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
        except Exception as error:  # noqa: BLE001 - a leaked "running" claim never
            # gets picked up again (nothing polls stale non-terminal reconcile rows),
            # and reconcile_loop below has no guard either, so anything narrower than
            # Exception here (e.g. a sqlite3.OperationalError under contention) used
            # to both strand this claim forever and kill the whole reconcile thread.
            self.state.record_event(
                "pr-reconcile", run_id, "reconcile", "failed", status="failed", metadata={"error": str(error)}
            )
            self.state.complete_run("pr-reconcile", run_id, status="failed", error=str(error))
            _log(f"reconcile-failed error={error}")

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
            try:
                self.reconcile()
            except Exception as error:  # noqa: BLE001 - this loop must never die; a
                # silent thread death here means no PR is ever reconciled again for
                # the rest of the process's life, with nothing visibly wrong.
                _log(f"reconcile_loop error={error}")
