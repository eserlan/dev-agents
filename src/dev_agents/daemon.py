"""The daemon entrypoint: webhook HTTP server, routing, and schedulers.

Wires up three independent services behind one HTTP listener -- the PR fixer
(``dev_agents.pr_fixer.PrFixerService``), the label-driven issue fixer
(``dev_agents.issue_fixer.IssueFixerService``), and the release-comms
scheduler (``dev_agents.workflows.release_comms``) -- plus periodic
reconciliation and artifact cleanup. None of that is PR-fixing itself, so it
lives here rather than under the ``pr_fixer`` package.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from dev_agents.config import (
    ConfigError,
    PrFixerConfig,
    ProjectConfig,
    load_projects_config,
    select_project,
)
from dev_agents.issue_fixer import IssueFixerService
from dev_agents.pr_fixer import (
    _ACTIONS,
    MAX_BODY_BYTES,
    PrFixerService,
    _cleanup_artifacts,
    _log,
    _state_path,
)
from dev_agents.runtime import StateRepository, state_database_path
from dev_agents.workflows.degodify import DegodifyFile, run_degodify
from dev_agents.workflows.release_comms import run_release_comms


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

    _cleanup_artifacts(project_name, project, config)
    service = PrFixerService(project_name, project, config)
    # Every configured label queue (e.g. a "bug" fix queue and a separate
    # "gui-fix" enhancement queue) gets its own IssueFixerService, sharing the
    # PR fixer's state repository. Each service filters by its own label, so
    # dispatching an "issues" webhook or reconcile tick to all of them is safe
    # -- only the one whose label matches actually acts.
    issue_fixer_configs = list(project.issue_fixers)
    if project.issue_fixer is not None:
        issue_fixer_configs = [project.issue_fixer, *issue_fixer_configs]
    issue_services = [
        IssueFixerService(project_name, project, issue_fixer_config, config, service.state)
        for issue_fixer_config in issue_fixer_configs
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "ok": self.path == "/health",
                        "active_jobs": len(service.active)
                        + sum(len(issue_service.active) for issue_service in issue_services),
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
                            log_path=(
                                config.log_dir / "degodify.log"
                                if config.log_dir is not None
                                else None
                            ),
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
                    not issue_services
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
                        # Each service filters by its own label internally, so
                        # dispatching to all of them is safe -- only the queue
                        # whose label the issue actually carries will act.
                        started = any(
                            issue_service.handle(issue_number)
                            for issue_service in issue_services
                        )
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
        MAX_FAILED_RETRY_ATTEMPTS = 5
        MAX_SCHEDULED_RESUME_ATTEMPTS = 15
        # Gate how often a *new* run may start (independent of any one run's own
        # internal message-to-message pacing) so a backlog of several distinct
        # releases drains at the same human cadence as a single release's batches,
        # instead of firing every due/retryable run in one poll tick. In-memory
        # only: a daemon restart resets it, same as the rest of this loop's state.
        next_start_slot = datetime.now(UTC)
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
                attempt_cap: int | None = None
                if run.status == "scheduled":
                    try:
                        next_at = datetime.fromisoformat(
                            str(run.metadata.get("next_publication_at", ""))
                        )
                    except ValueError:
                        next_at = now
                    if next_at > now:
                        continue
                    # "scheduled" is the normal healthy multi-batch pacing flow (one
                    # resume per feature/page), not a failure, so it gets a much more
                    # generous cap than a genuine failure -- but it still needs one.
                    # Confirmed live: a resume/redraft mismatch left one run's
                    # pending-publication count permanently non-zero with nothing
                    # left it could actually publish, so it resumed (and re-ran a
                    # real evaluator/writer LLM pass) every cycle forever with zero
                    # progress until this cap existed.
                    attempt_cap = MAX_SCHEDULED_RESUME_ATTEMPTS
                elif run.status == "failed":
                    attempt_cap = MAX_FAILED_RETRY_ATTEMPTS
                elif run.status == "running":
                    # A "running" run that has sat past the staleness window was
                    # interrupted (e.g. the daemon restarted mid-run) and orphaned --
                    # claim_run() is the real safety gate against reclaiming a run
                    # that is genuinely still in flight.
                    try:
                        started = datetime.fromisoformat(run.started_at)
                    except ValueError:
                        continue
                    if (now - started).total_seconds() < StateRepository.STALE_RUN_SECONDS:
                        continue
                    attempt_cap = MAX_FAILED_RETRY_ATTEMPTS
                else:
                    # Only scheduled (delay-timer), failed, and stale-running
                    # (retry-until-posted) runs are resumed here; completed runs
                    # and actively-running runs are left alone.
                    continue
                if attempt_cap is not None and run.attempt >= attempt_cap:
                    if not run.metadata.get("_release_comms_retry_exhausted"):
                        _log(
                            f"release-comms run_id={run.run_id} giving up after "
                            f"{run.attempt} attempts (cap={attempt_cap})"
                        )
                        claims.update_run(
                            "release-comms",
                            run.run_id,
                            metadata={"_release_comms_retry_exhausted": True},
                        )
                    continue
                if now < next_start_slot:
                    # Another run already claimed this poll's start slot; leave this
                    # one for a later tick instead of starting it alongside others.
                    continue
                with active_lock:
                    if run.run_id in active:
                        continue
                    active.add(run.run_id)
                delay_min = max(0.0, comms_config.publication_delay_min_seconds)
                delay_max = max(delay_min, comms_config.publication_delay_max_seconds)
                next_start_slot = now + timedelta(seconds=random.uniform(delay_min, delay_max))

                def resume(run_id: str = run.run_id, attempt: int = run.attempt) -> None:
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
                            f"resumed release-comms run_id={run_id} attempt={attempt} completed={result.completed} scheduled={result.scheduled}"
                        )
                    except Exception as error:  # noqa: BLE001 - scheduler keeps serving
                        _log(
                            f"release-comms resume failed run_id={run_id} attempt={attempt} error={error} (will retry next wake)"
                        )
                    finally:
                        with active_lock:
                            active.discard(run_id)

                Thread(target=resume, daemon=True).start()

    _log(f"listening on 127.0.0.1:{config.port}{config.webhook_path} for {project.github}")
    Thread(target=service.reconcile_loop, daemon=True).start()
    for issue_service in issue_services:
        Thread(target=issue_service.reconcile, daemon=True).start()
        Thread(target=issue_service.reconcile_loop, daemon=True).start()
    Thread(target=release_comms_scheduler, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", config.port), Handler).serve_forever()


def serve_project(config_path: Path, project_name: str) -> None:
    project = select_project(load_projects_config(config_path), project_name)
    if project.pr_fixer is None or project.github is None:
        raise ConfigError(f"Project {project_name!r} requires github and pr_fixer configuration")
    serve(project_name, project, project.pr_fixer)
