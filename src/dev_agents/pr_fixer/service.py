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
from threading import Event, Lock
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
from dev_agents.runtime.agent import CODEX_MODEL, AgentResult

from ._shared import MAX_INTERNAL_REVIEW_ROUNDS, PR_FIX_PROVIDER, _log
from .audit import (
    audit_json_schema,
    audit_telemetry,
    compare_reference_comments,
    parse_audit_report,
)
from .claims import repair_claim
from .comments import (
    _auto_pause_body,
    _external_agent_pause_body,
    _fix_summary_body,
    _publish_fix_summary,
    _review_completed_body,
    _review_findings_body,
    _review_fixes_started_body,
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
from .prompts import _prompt, _review_fix_prompt, _review_prompt, _review_skill


class PrFixerRunState(TypedDict, total=False):
    """State carried through one webhook/reconciliation PR-fix run."""

    number: int
    meta: dict[str, Any]
    keys: list[str]
    handled: list[str]
    unseen: list[str]
    state_path: Path
    run_id: str
    attempt: int
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


def _review_verdict(metadata: dict[str, Any]) -> str | None:
    """The verdict of a review run's structured report, or None if there is no valid report."""
    report = metadata.get("review_report")
    if metadata.get("review_report_valid") is True and isinstance(report, dict):
        verdict = report.get("verdict")
        return str(verdict) if verdict else None
    return None


def _review_report_has_fixes(report: dict[str, Any]) -> bool:
    """Whether the structured review report says the agent made concrete fixes."""
    review_report = report.get("review_report")
    fixes = review_report.get("fixes") if isinstance(review_report, dict) else None
    return report.get("report_valid") is True and isinstance(fixes, list) and bool(fixes)


def _round_ended_chain(metadata: dict[str, Any]) -> bool:
    """Whether the review round recorded in ``metadata`` leaves nothing further to verify.

    Round 0 (the full review) never ends the chain itself: a fix pushed there is verified by a
    targeted round. A targeted round ends the chain when it is the last allowed round, or when it
    pushed nothing or came back ``clean``. Otherwise it pushed a fix that no review has seen, so
    one more targeted round follows, bounded by MAX_INTERNAL_REVIEW_ROUNDS.
    """
    try:
        review_round = int(metadata.get("review_round", 0))
    except (TypeError, ValueError):
        review_round = 0
    if review_round <= 0:
        return False
    if review_round >= MAX_INTERNAL_REVIEW_ROUNDS - 1:
        return True
    return not (metadata.get("review_fix_pushed") and _review_verdict(metadata) != "clean")


class PrFixerService:
    def __init__(self, project_name: str, project: ProjectConfig, config: PrFixerConfig):
        self.project_name, self.project, self.config = project_name, project, config
        self.state = StateRepository(
            _state_path(config, project_name), project_name, project.repo
        )
        self.active: set[int] = set()
        self.rerun_requested: set[int] = set()
        self.cancel_events: dict[int, Event] = {}
        self.active_heads: dict[int, str] = {}
        self.active_worktrees: dict[int, Path] = {}
        self.lock = Lock()
        self.workflow = self._build_workflow()

    def _is_own_push(self, number: int, head_sha: str) -> bool:
        """Whether ``head_sha`` was committed in the active run's own worktree.

        The agent commits before it pushes, so its pushed head is HEAD or an ancestor of HEAD
        in that worktree. A head pushed by anyone else is absent there unless the agent
        fetched and merged it, in which case its result already includes that push.
        """
        worktree = self.active_worktrees.get(number)
        if worktree is None:
            return False
        try:
            _pkg._run(worktree, "git", "merge-base", "--is-ancestor", head_sha, "HEAD")
        except RuntimeError:
            return False
        return True

    def handle(self, number: int, *, new_head_sha: str | None = None) -> bool:
        with self.lock:
            if number in self.active:
                if new_head_sha and new_head_sha != self.active_heads.get(number):
                    self.rerun_requested.add(number)
                    if self._is_own_push(number, new_head_sha):
                        # The running fixer pushed this head itself. Cancelling it here would
                        # kill it before it reports its fixes, and the finalizer would then
                        # mistake its own commit for a concurrent push. Let it finish; the
                        # queued rerun starts the targeted post-fix verification.
                        self.active_heads[number] = new_head_sha
                        _log(
                            f"head-advanced pr={number} new_head={new_head_sha[:12]}; "
                            "pushed by the active fixer, verifying after it finishes"
                        )
                    else:
                        self.cancel_events[number].set()
                        _log(
                            f"head-advanced pr={number} new_head={new_head_sha[:12]}; "
                            "cancelling stale run and queuing latest head"
                        )
                return False
            self.active.add(number)
            self.cancel_events[number] = Event()
        try:
            started = False
            while True:
                result = self.workflow.invoke({"number": number})
                started = bool(result.get("started", False)) or started
                with self.lock:
                    if number in self.rerun_requested:
                        self.rerun_requested.discard(number)
                        self.cancel_events[number] = Event()
                        self.active_heads.pop(number, None)
                        _log(f"rerunning pr={number} against latest head")
                        continue
                # Merge readiness is deliberately independent from the feedback
                # run claim. A no-op run can be completed while checks are still
                # being created; a later check_run.completed event must still
                # be able to request auto-merge for the same PR head.
                self._ensure_auto_merge(number)
                with self.lock:
                    if number in self.rerun_requested:
                        self.rerun_requested.discard(number)
                        self.cancel_events[number] = Event()
                        self.active_heads.pop(number, None)
                        _log(f"rerunning pr={number} against latest head")
                        continue
                    self.active.discard(number)
                    self.cancel_events.pop(number, None)
                    self.active_heads.pop(number, None)
                    break
            return started
        finally:
            with self.lock:
                self.active.discard(number)
                self.rerun_requested.discard(number)
                self.cancel_events.pop(number, None)
                self.active_heads.pop(number, None)
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

    def _human_authored_head(self, meta: dict[str, Any]) -> bool:
        """Whether the PR head's latest commit was written only by people, not agents."""
        commits = meta.get("commits")
        latest = commits[-1] if isinstance(commits, list) and commits else None
        authors = latest.get("authors") if isinstance(latest, dict) else None
        if not isinstance(authors, list) or not authors:
            return False
        agents = {
            login.strip().lower()
            for login in (*self.config.external_agent_logins, *self.config.review_handoff_logins)
            if login.strip()
        }
        logins = [
            str(author.get("login") or "").strip().lower()
            for author in authors
            if isinstance(author, dict)
        ]
        return bool(logins) and all(
            login and login not in agents and not login.endswith("[bot]") for login in logins
        )

    def _agent_identity(self) -> dict[str, Any]:
        """The model and effort the review and fix agents run with, for naming them in comments."""
        return {"model": self.config.model or CODEX_MODEL, "effort": self.config.reasoning_effort}

    def _review_plan(
        self, number: int, head_sha: str, *, human_head: bool = False
    ) -> dict[str, Any] | None:
        """Select one initial review or the single follow-up allowed for a fixed head.

        A halt needs human adjudication. It holds for every head until a person pushes a
        new commit of their own; agent pushes, including this workflow's own repairs, never
        lift it.
        """
        runs = [
            run
            for run in self.state.list_runs("pr-review", limit=None, pull_request=number)
            if run.metadata.get("pull_request") == number
        ]
        fresh_plan = {
            "round": 0,
            "scope": "full",
            "chain_id": f"pr-review-{number}-{head_sha}",
            "parent_run_id": None,
            "parent_sha": None,
            "prior_report": None,
        }
        if not runs:
            return fresh_plan

        halted = [run for run in runs if run.metadata.get("review_halted")]
        if halted:
            own = {sha for run in halted for sha in run.metadata.get("own_commits") or []}
            halted_heads = {str(run.metadata.get("head_sha", "")) for run in halted}
            if head_sha in halted_heads or head_sha in own or not human_head:
                return None
            if str(runs[0].metadata.get("head_sha", "")) != head_sha:
                return fresh_plan
        latest = runs[0]
        metadata = latest.metadata
        try:
            review_round = int(metadata.get("review_round", 0))
        except (TypeError, ValueError):
            review_round = 0
        latest_head = str(metadata.get("head_sha", ""))
        chain_id = str(metadata.get("review_chain_id") or latest.run_id)

        # The recorded exhausted head only counts when that round really ended the chain: a
        # targeted round that pushed a non-clean fix leaves that head unverified (this is what
        # left PR #339 waiting forever for a review that could never come).
        if metadata.get("review_exhausted_head_sha") == head_sha and _round_ended_chain(metadata):
            return None

        # Older completed targeted reviews did not persist an exhausted head when
        # they found no new defect. Treat that completed round as exhausted too,
        # so reconciliation cannot restart the same review indefinitely.
        if (
            latest.status == "completed"
            and review_round >= 1
            and latest_head == head_sha
            and _round_ended_chain(metadata)
        ):
            return None

        if metadata.get("review_fix_pushed") and not _round_ended_chain(metadata):
            return {
                "round": review_round + 1,
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

    def _finish_superseded_review(
        self, state: PrFixerRunState, new_sha: str
    ) -> dict[str, bool]:
        """Close a review cleanly when an unreported external commit advanced the PR."""
        number = state["number"]
        workflow = state.get("workflow", "pr-fixer")
        old_sha = str(state.get("meta", {}).get("headRefOid", ""))
        metadata = {"reviewed_head_sha": old_sha, "superseded_by": new_sha}
        self.state.record_event(
            workflow,
            state["run_id"],
            "finalize",
            "review_superseded",
            metadata=metadata,
        )
        self.state.complete_run(
            workflow,
            state["run_id"],
            status="failed",
            error="superseded by a concurrent push to the same branch",
            metadata=metadata,
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
                old_sha,
                new_sha,
                fix_pushed=False,
                superseded_by=new_sha,
                **self._agent_identity(),
            ),
        )
        _log(f"review-superseded pr={number} original_head={old_sha} new_head={new_sha}")
        return {"started": False}

    def _track_audit_comparison(self, number: int, meta: dict[str, Any]) -> None:
        """Compare later Copilot feedback with persisted independent audits of the same SHA."""
        author = meta.get("author")
        author_login = author.get("login") if isinstance(author, dict) else None
        human_reviewers = {
            str(review.get("author", {}).get("login", "")) for review in meta.get("reviews", [])
            if isinstance(review, dict) and isinstance(review.get("author"), dict)
            and review["author"].get("login")
            and str(review.get("state", "")).upper() not in {"PENDING", "DISMISSED"}
            and "copilot" not in str(review["author"].get("login", "")).lower()
            and not str(review["author"].get("login", "")).lower().endswith("[bot]")
            and review["author"].get("login") != author_login
        }
        if (not _has_copilot_review(meta) and not human_reviewers) or not self.project.github:
            return
        runs = [run for run in self.state.list_runs("pr-review", limit=200)
                if run.metadata.get("pull_request") == number and run.metadata.get("audit_report")]
        if not runs:
            return
        try:
            pages = json.loads(_pkg._run(
                self.project.repo, "gh", "api",
                f"repos/{self.project.github}/pulls/{number}/comments", "--paginate", "--slurp",
            ))
            comments = [comment for page in pages for comment in page if isinstance(comment, dict)]
        except (RuntimeError, ValueError, TypeError) as error:
            _log(f"audit-comparison-deferred pr={number} error={error}")
            return
        for run in runs:
            comparison = compare_reference_comments(
                run.metadata["audit_report"], comments, str(run.metadata.get("head_sha", "")),
            )
            previous = run.metadata.get("audit_telemetry", {})
            human_comparison = compare_reference_comments(
                run.metadata["audit_report"], comments, str(run.metadata.get("head_sha", "")),
                human_reviewers=human_reviewers,
            )
            if previous.get("copilot_overlap") == comparison and previous.get("human_overlap") == human_comparison:
                continue
            telemetry = {**previous, "copilot_overlap": comparison, "human_overlap": human_comparison}
            self.state.update_run("pr-review", run.run_id, metadata={"audit_telemetry": telemetry})
            self.state.record_event("pr-review", run.run_id, "review_quality", "reference_compared", metadata={"copilot": comparison, "human": human_comparison})
            _log(f"audit-comparison pr={number} run_id={run.run_id} {json.dumps(comparison, sort_keys=True)}")

    def _collect_feedback(self, state: PrFixerRunState) -> dict[str, Any]:
        number = state["number"]
        meta, keys = _pkg._feedback(self.project.repo, number)
        self._track_audit_comparison(number, meta)
        with self.lock:
            self.active_heads[number] = str(meta.get("headRefOid", "unknown"))
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
        review_plan = self._review_plan(
            number, head_sha, human_head=self._human_authored_head(meta)
        )
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
        previous = self.state.get_run(workflow, run_id)
        pending = previous.metadata if previous and previous.metadata.get("repair_pending") else {}
        if pending:
            now = time.time()
            opened = float(pending["repair_opened_at"])
            claim_info = pending.get("repair_claim")
            if not claim_info:
                slug = _pkg.repository_slug(self.project.repo)
                try:
                    pages = json.loads(_pkg._run(self.project.repo, "gh", "api", "--paginate",
                                                "--slurp", f"repos/{slug}/issues/{number}/comments"))
                    claim_info = repair_claim([c for page in pages for c in page], run_id, opened)
                except (RuntimeError, ValueError, TypeError):
                    # Cannot establish ownership: retry on reconciliation instead of racing.
                    return {"meta": meta, "keys": keys, "skip": True}
                if claim_info:
                    self.state.update_run(workflow, run_id, metadata={"repair_claim": claim_info})
                    pending = {**pending, "repair_claim": claim_info}
                    self.state.record_event(workflow, run_id, "fixer", "repair_claimed", metadata=claim_info)
                    _pkg._publish_pr_run_comment(
                        self.project.repo, number, run_id, "repair-claimed",
                        f"### 🤖 Repair claimed by {claim_info['login']}\n\n"
                        "Dev-agents will wait for a push. If none arrives within 30 minutes of "
                        "the claim, its fixer will resume.\n",
                    )
            until = float(claim_info["expires_at"]) if claim_info else opened + self.config.review_claim_window_seconds
            if now < until:
                return {"meta": meta, "keys": keys, "skip": True}
        review_skill_path, _ = _review_skill(self.project.repo)
        claim = self.state.claim_run(
            workflow,
            run_id,
            metadata={
                **pending,
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
            "attempt": claim.record.attempt,
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
                    **self._agent_identity(),
                ),
            )
        else:
            report_url = None
            _pkg._publish_pr_run_comment(
                self.project.repo,
                number,
                state["run_id"],
                "fix-started",
                f"""<!-- dev-agents:pr-fix-started run={state['run_id']} -->
### 🤖 Dev-agents fix started

Dev-agents has picked up PR #{number} and is working on {len(unseen)} actionable feedback item(s),
including any failing checks. The agent will investigate, make fixes, and run validation.

- PR head: `{state['meta'].get('headRefOid', 'unknown')}`
- Attempt: {state.get('attempt', 1)}
- Model: `{self.config.model or CODEX_MODEL}` ({self.config.reasoning_effort} effort)
- Run: `{state['run_id']}`

A follow-up comment will report the outcome.
""",
            )
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

    def _auto_pause_if_exhausted(
        self, number: int, run_id: str, attempt: int, reason: str
    ) -> None:
        """Apply the `paused` label after too many consecutive failures on one run_id.

        A "failed" run stays reclaimable (unlike "completed"/"rejected"), so a
        persistent failure -- a provider quota outage, for example -- would
        otherwise retry and re-comment forever on reconcile's poll interval.
        """
        if attempt < self.config.max_consecutive_failures:
            return
        try:
            _pkg._run(self.project.repo, "gh", "pr", "edit", str(number), "--add-label", "paused")
        except RuntimeError as error:
            _log(f"auto-pause-label-failed pr={number} run_id={run_id} error={error}")
            return
        _pkg._publish_pr_run_comment(
            self.project.repo,
            number,
            run_id,
            "auto-paused",
            _auto_pause_body(number, run_id, attempt, reason),
        )
        _log(f"auto-paused pr={number} run_id={run_id} attempt={attempt} reason={reason}")

    def _finalize(self, state: PrFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("claimed"):
            return {"started": False}
        number = state["number"]
        workflow = state.get("workflow", "pr-fixer")
        if state.get("review_only") and state.get("report", {}).get("review_halted"):
            report = state["report"]
            self.state.update_run(workflow, state["run_id"], metadata={
                "review_halted": report["review_halted"],
                "review_report": report.get("review_report"), "review_report_valid": False,
                "repair_pending": False, "own_commits": report.get("own_commits") or [],
            })
            _pkg._publish_pr_run_comment(
                self.project.repo, number, state["run_id"], "review-halted",
                f"Automatic review repairs stopped: {report['review_halted']}. Human adjudication required.\n\n"
                + json.dumps(report.get("review_report", {}), ensure_ascii=False),
            )
            self.state.complete_run(workflow, state["run_id"])
            return {"started": False}
        if state.get("review_only") and state.get("report", {}).get("repair_pending"):
            self.state.update_run(workflow, state["run_id"], status="waiting",
                                    metadata={"repair_pending": True})
            return {"started": False}
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
                return self._finish_superseded_review(state, str(superseded_head))
            if not state.get("fixed"):
                try:
                    refreshed_meta, _ = _pkg._feedback(self.project.repo, number)
                except (RuntimeError, json.JSONDecodeError):
                    refreshed_meta = {}
                old_sha = str(state.get("meta", {}).get("headRefOid", ""))
                new_sha = str(refreshed_meta.get("headRefOid", ""))
                own_push = new_sha in (state.get("report", {}).get("own_commits") or [])
                if new_sha and new_sha != old_sha and not own_push and not _review_report_has_fixes(
                    state.get("report", {})
                ):
                    return self._finish_superseded_review(state, new_sha)
                recovery_metadata: dict[str, Any] = {}
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
                        failure=(
                            f"The fixer pushed `{new_sha[:12]}` but stopped before reporting what it"
                            " changed. A verification review will check the pushed commit."
                            if own_push and new_sha != old_sha
                            else "The review agent did not complete successfully; see the daemon log."
                        ),
                        **self._agent_identity(),
                    ),
                )
                self._auto_pause_if_exhausted(
                    number,
                    state["run_id"],
                    int(state.get("attempt", 1)),
                    "the review agent kept failing",
                )
                return {"started": False}
            refreshed_meta, _ = _pkg._feedback(self.project.repo, number)
            old_sha = str(state["meta"].get("headRefOid", ""))
            new_sha = str(refreshed_meta.get("headRefOid", ""))
            review_round = int(state.get("review_round", 0))
            head_advanced = bool(new_sha and new_sha != old_sha)
            report = state.get("report", {})
            agent_pushed_fix = head_advanced and _review_report_has_fixes(report)
            if head_advanced and not agent_pushed_fix:
                return self._finish_superseded_review(state, new_sha)
            fix_pushed = agent_pushed_fix
            review_passes = (
                ["general", "codex-review"]
                if review_round == 0
                else ["targeted-post-fix"]
            )
            round_outcome = {
                "review_round": review_round,
                "review_fix_pushed": fix_pushed,
                "review_report": state.get("report", {}).get("review_report"),
                "review_report_valid": state.get("report", {}).get("report_valid", False),
            }
            exhausted_head_sha = (new_sha or old_sha) if _round_ended_chain(round_outcome) else None
            follow_up_pending = fix_pushed and exhausted_head_sha is None
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
            review_report = report.get("review_report")
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
                    fix_pushed=fix_pushed,
                    follow_up=follow_up_pending,
                    **self._agent_identity(),
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
                    "review_follow_up_pending": follow_up_pending,
                    "review_follow_up_head_sha": new_sha if follow_up_pending else None,
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
                    "review_follow_up_pending": follow_up_pending,
                    "review_follow_up_head_sha": new_sha if follow_up_pending else None,
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
            _pkg._publish_pr_run_comment(
                self.project.repo,
                number,
                state["run_id"],
                "fix-failed",
                f"""<!-- dev-agents:pr-fix-failed run={state['run_id']} -->
### 🤖 Dev-agents fix incomplete

The agent did not complete the fix for PR #{number}. Feedback remains actionable for retry.

- Attempt: {state.get('attempt', 1)}
- Model: `{self.config.model or CODEX_MODEL}` ({self.config.reasoning_effort} effort)
- Run: `{state['run_id']}`
""",
            )
            self._auto_pause_if_exhausted(
                number,
                state["run_id"],
                int(state.get("attempt", 1)),
                "the fix agent kept failing",
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
                **self._agent_identity(),
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
                with self.lock:
                    self.active_worktrees[number] = worktree
                if conflicts:
                    _log(f"merge-conflict pr={number} paths={','.join(conflicts)}")
                log_dir = (
                    self.config.log_dir
                    or Path.home() / ".local/state/dev-agents" / self.project_name / "logs"
                ).expanduser()
                log_dir.mkdir(parents=True, exist_ok=True)
                log = log_dir / f"pr-{number}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.log"

                def run_provider(provider: str) -> Any:
                    deadline = time.monotonic() + self.config.timeout_minutes * 60
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
                        # Fix-mode has no status comment to update, so progress
                        # notes are review-only.
                        if not review_only or run_id is None:
                            return
                        _pkg._publish_pr_run_comment(
                            self.project.repo,
                            number,
                            run_id,
                            "review-progress",
                            _review_progress_body(
                                number, run_id, meta or {}, None, review_round, elapsed_seconds,
                                **self._agent_identity(),
                            ),
                        )
                        _log(f"review-progress pr={number} elapsed_minutes={elapsed_seconds // 60}")

                    if review_only:
                        before_head = _pkg._run(worktree, "git", "rev-parse", "HEAD")
                        before_status = _pkg._run(worktree, "git", "status", "--porcelain")
                        audit_path = log.with_suffix(".audit.json")
                        schema_path = log.with_suffix(".audit.schema.json")
                        schema_path.write_text(audit_json_schema(), encoding="utf-8")

                        def audit_failed(error: str, *, timed_out: bool = False) -> AgentResult:
                            failure: dict[str, Any] = {
                                "run_id": run_id, "pr_number": number, "stage": "auditor",
                                "reviewed_head_sha": str((meta or {}).get("headRefOid", before_head)),
                                "review_round": review_round, "verdict": "failed", "findings_count": None,
                                "categories_checked": [], "error": error, "timed_out": timed_out,
                            }
                            report.update(report_valid=False, report_error=error, audit_telemetry=failure)
                            _log(f"audit-result {json.dumps(failure, sort_keys=True)}")
                            if run_id and self.state.get_run("pr-review", run_id):
                                self.state.update_run("pr-review", run_id, metadata={"audit_telemetry": failure})
                                self.state.record_event("pr-review", run_id, "auditor", "audit_failed", status="failed", metadata=failure)
                            return AgentResult(1, timed_out)

                        saved = self.state.get_run("pr-review", run_id) if run_id else None
                        resuming = bool(saved and saved.metadata.get("repair_pending"))
                        if resuming and saved:
                            audit_path.write_text(json.dumps(saved.metadata["audit_report"]))
                            try:
                                parse_audit_report(audit_path, initial=review_round == 0)
                            except ValueError:
                                # Legacy pending reports lack contract evidence. Re-audit
                                # instead of retrying their invalid repair forever.
                                resuming = False
                                self.state.update_run("pr-review", saved.run_id, metadata={"repair_pending": False})
                        if resuming:
                            audit_result = AgentResult(0, False)
                        else:
                            audit_result = run_agent(
                                PR_FIX_PROVIDER, prompt, cwd=worktree,
                                log_path=log.with_suffix(".audit.log"),
                                timeout_seconds=self.config.timeout_minutes * 60,
                                heartbeat_seconds=self.config.heartbeat_seconds,
                                reasoning_effort=self.config.reasoning_effort, model=self.config.model,
                                on_progress=on_progress, cancel_event=self.cancel_events.get(number),
                                last_message_path=audit_path, read_only=True,
                                output_schema_path=schema_path,
                            )
                        if audit_result.returncode != 0 or audit_result.timed_out:
                            return audit_failed("independent auditor did not complete", timed_out=audit_result.timed_out)
                        if (
                            _pkg._run(worktree, "git", "rev-parse", "HEAD") != before_head
                            or _pkg._run(worktree, "git", "status", "--porcelain") != before_status
                        ):
                            return audit_failed("read-only auditor mutated the worktree")
                        try:
                            audit = parse_audit_report(audit_path, initial=review_round == 0)
                        except (ValueError, OSError) as error:
                            return audit_failed(f"invalid auditor report: {type(error).__name__}")
                        telemetry = audit_telemetry(
                            audit, run_id=run_id, number=number,
                            head_sha=str((meta or {}).get("headRefOid", before_head)),
                            review_round=review_round,
                        )
                        report.update(
                            audit_report=audit.model_dump(), audit_telemetry=telemetry,
                            review_report=audit.review_report(), report_valid=True,
                            findings="; ".join(f"{finding.location}: {finding.impact}" for finding in audit.findings) or "none",
                            fixes="none",
                        )
                        _log(f"audit-result {json.dumps(telemetry, sort_keys=True)}")
                        if run_id and self.state.get_run("pr-review", run_id):
                            self.state.update_run("pr-review", run_id, metadata={
                                "audit_report": audit.model_dump(), "audit_telemetry": telemetry,
                                "audit_report_path": str(audit_path),
                            })
                            self.state.record_event("pr-review", run_id, "auditor", "audit_completed", metadata=telemetry)
                        # Incomplete coverage with concrete findings still gets those findings
                        # fixed; the unverified checks are reported for a human, and the chain's
                        # clean-verdict requirement keeps auto-merge blocked until they're resolved.
                        if audit.verdict == "incomplete" and not audit.findings:
                            report.update(report_valid=False, report_error="auditor coverage is incomplete")
                            return AgentResult(1, False)
                        if audit.verdict == "clean":
                            return audit_result
                        # Publish what the auditor found before any repair starts, so the PR shows
                        # the findings themselves rather than only that fixing is under way.
                        opened_at = time.time()
                        if not resuming and run_id and self.state.get_run("pr-review", run_id):
                            _pkg._publish_pr_run_comment(
                                self.project.repo, number, run_id, "review-findings",
                                _review_findings_body(
                                    number, run_id, audit.model_dump(),
                                    review_round=review_round, head_sha=original_head or None,
                                    claim_window_seconds=self.config.review_claim_window_seconds,
                                    **self._agent_identity(),
                                ),
                            )
                        if (not resuming and run_id and self.config.review_claim_window_seconds
                                and self.state.get_run("pr-review", run_id)):
                            self.state.update_run("pr-review", run_id, metadata={
                                "repair_pending": True, "repair_opened_at": opened_at,
                            })
                            report["repair_pending"] = True
                            return audit_result
                        if resuming and run_id:
                            self.state.update_run("pr-review", run_id, metadata={"repair_pending": False})
                        # Do not hand findings to a writer after a concurrent push changed their
                        # source. Cancellation and normal final supersession handling still apply.
                        with repo_git_lock(self.project.repo):
                            _pkg._run(worktree, "git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
                        latest_head = _pkg._run(worktree, "git", "rev-parse", f"origin/{branch}", check=False)
                        cancellation = self.cancel_events.get(number)
                        if (original_head and latest_head != original_head) or (cancellation and cancellation.is_set()):
                            if latest_head and latest_head != original_head:
                                superseded_by.append(latest_head)
                            return AgentResult(1, False)
                        # Count writer starts across all heads/chains of this PR, including
                        # failed attempts. Bot pushes and retries cannot reset the budget.
                        repairs = sum(
                            int(run.metadata.get("review_repair_attempts", bool(run.metadata.get("review_fix_pushed"))))
                            for run in self.state.list_runs("pr-review", limit=None, pull_request=number)
                            if run.metadata.get("pull_request") == number
                        )
                        if repairs >= 2 or review_round >= MAX_INTERNAL_REVIEW_ROUNDS - 1:
                            report["review_halted"] = "two repair attempts exhausted; unresolved findings retained"
                            return AgentResult(1, False)
                        current = self.state.get_run("pr-review", run_id) if run_id else None
                        if current is not None and run_id is not None:
                            attempts = int(current.metadata.get("review_repair_attempts", 0))
                            self.state.update_run("pr-review", run_id, metadata={"review_repair_attempts": attempts + 1})
                        prompt = _review_fix_prompt(worktree, number, audit.model_dump(), base, conflicts)
                        _log(f"fixer-start pr={number} audited_findings={len(audit.findings)}")
                        fixer_started.append(True)
                        if run_id and self.state.get_run("pr-review", run_id):
                            self.state.record_event("pr-review", run_id, "fixer", "fixer_started", metadata={"findings_count": len(audit.findings)})
                            _pkg._publish_pr_run_comment(
                                self.project.repo, number, run_id, "review-fixes-started",
                                _review_fixes_started_body(
                                    number, run_id, audit.model_dump(), review_round=review_round,
                                    **self._agent_identity(),
                                ),
                            )

                    final_message = log.with_suffix(".fixer.txt")
                    if time.monotonic() >= deadline:
                        report.update(report_valid=False, report_error="run budget expired before repair")
                        return AgentResult(1, True)
                    result = run_agent(
                        PR_FIX_PROVIDER,
                        prompt,
                        cwd=worktree,
                        log_path=log,
                        timeout_seconds=max(1.0, deadline - time.monotonic()),
                        heartbeat_seconds=self.config.heartbeat_seconds,
                        reasoning_effort=self.config.reasoning_effort,
                        model=self.config.model,
                        on_progress=on_progress,
                        cancel_event=self.cancel_events.get(number),
                        last_message_path=final_message,
                    )
                    if review_only:
                        fixer_telemetry = {
                            "stage": "fixer", "run_id": run_id, "pr_number": number,
                            "reviewed_head_sha": str((meta or {}).get("headRefOid", before_head)),
                            "review_round": review_round, "returncode": result.returncode,
                            "timed_out": result.timed_out,
                        }
                        _log(f"fixer-result {json.dumps(fixer_telemetry, sort_keys=True)}")
                        if run_id and self.state.get_run("pr-review", run_id):
                            self.state.update_run("pr-review", run_id, metadata={"fixer_telemetry": fixer_telemetry})
                            self.state.record_event(
                                "pr-review", run_id, "fixer", "fixer_completed",
                                status="failed" if result.returncode or result.timed_out else "completed",
                                metadata=fixer_telemetry,
                            )
                    report.update(_agent_report(final_message if final_message.is_file() else log))
                    if review_only:
                        repair = report.get("review_report", {})
                        if repair.get("verdict") == "disputed":
                            repair["findings"] = audit.review_report()["findings"]
                            report["review_halted"] = "fixer disputed audit: " + "; ".join(repair["validation"])
                            return AgentResult(1, result.timed_out)
                        if not report.get("report_valid") or not repair.get("fixes"):
                            report.update(report_valid=False, report_error="fixer did not report repairs for audited findings")
                            return AgentResult(1, result.timed_out)
                        # Preserve the independent findings; fixing them is not an independent
                        # clean audit. The existing bounded post-fix round verifies the repairs.
                        repair["verdict"] = "findings"
                        repair["findings"] = audit.review_report()["findings"]
                        repair["categories_checked"] = audit.categories_checked
                    result_code = result.returncode
                    if result.timed_out:
                        _log(f"agent-timeout pr={number} provider={provider}")
                    _log(f"agent-finished pr={number} provider={provider} exit={result_code}")
                    return result

                original_head = str((meta or {}).get("headRefOid", ""))
                superseded_by: list[str] = []
                # Set once a review run hands its findings to the fixer. Until then the run is a
                # read-only audit, which must not push anything (see accept_provider).
                fixer_started: list[bool] = []

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
                    # An audit that changed nothing must not push the worktree's staging merge:
                    # that new head would invalidate the clean verdict it just earned and start
                    # another review (PR #4032), every time staging moves during a review.
                    audit_only = review_only and not fixer_started

                    if (
                        not audit_only
                        and local_head
                        and origin_head
                        and local_head != origin_head
                        and origin_head == original_head
                        and not conflict_markers
                        and worktree_clean
                        and result.returncode == 0
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
                    current = pushed or (audit_only and origin_head == original_head)
                    return bool(
                        result.returncode == 0 and current and not conflict_markers and worktree_clean
                    )

                # PR reviews and fixes are intentionally Codex-only. Keep the legacy providers
                # field for config compatibility, but never let another agent handle PR code.
                accepted = run_with_fallback((PR_FIX_PROVIDER,), run_provider, accept_provider)
                # Remember which commits this run made before the worktree is removed, so the
                # finalizer can still recognise its own push if the agent died before reporting.
                own_range = f"{original_head}..HEAD" if original_head else "HEAD^!"
                report["own_commits"] = _pkg._run(worktree, "git", "rev-list", own_range, check=False).splitlines()
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
        finally:
            with self.lock:
                self.active_worktrees.pop(number, None)
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
        halted = [
            run
            for run in self.state.list_runs("pr-review", limit=None, pull_request=number)
            if run.metadata.get("review_halted")
        ]
        if any(
            run.metadata.get("head_sha") == head_sha or head_sha in (run.metadata.get("own_commits") or [])
            for run in halted
        ):
            return False
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
