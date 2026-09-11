"""Webhook-driven, project-configured pull-request remediation service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
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
from dev_agents.runtime import isolated_worktree, run_agent

SHARED_PR_FIX_SKILL = Path(__file__).resolve().parents[2] / "skills/pr-fix/SKILL.md"

_ACTIONS = {
    "pull_request": {"opened", "reopened", "synchronize", "ready_for_review"},
    "pull_request_review": {"submitted", "edited"},
    "pull_request_review_comment": {"created", "edited"},
    "check_run": {"completed"},
}
MAX_BODY_BYTES = 1_000_000


def _log(message: str) -> None:
    print(f"[pr-fixer] {message}", flush=True)


def _run(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(args, cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _state_path(config: PrFixerConfig, project: str) -> Path:
    return (config.state_path or Path.home() / ".local/state/dev-agents" / project / "pr-fixer-state.json").expanduser()


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get("pullRequests"), dict):
            return value
        # Migrate the original {"<pr>": ["comment:..."]} shape.
        if isinstance(value, dict):
            return {"version": 1, "pullRequests": value}
        return {"version": 1, "pullRequests": {}}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "pullRequests": {}}


def _save_state(path: Path, state: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write("\n")
        temporary = Path(file.name)
    temporary.replace(path)


def _cleanup_artifacts(project_name: str, config: PrFixerConfig) -> None:
    """Bound completed logs and stale temporary worktree directories."""
    now = datetime.now(UTC).timestamp()
    log_dir = (config.log_dir or Path.home() / ".local/state/dev-agents" / project_name / "logs").expanduser()
    log_cutoff = now - config.log_retention_days * 86400
    if log_dir.is_dir():
        for path in log_dir.glob("pr-*.log"):
            try:
                if path.stat().st_mtime < log_cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue

    worktree_root = (config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser()
    worktree_cutoff = now - config.worktree_retention_days * 86400
    if worktree_root.is_dir():
        for path in worktree_root.glob("pr-*"):
            try:
                if path.is_dir() and path.stat().st_mtime < worktree_cutoff:
                    shutil.rmtree(path)
            except FileNotFoundError:
                continue


def _feedback(repo: Path, number: int) -> tuple[dict[str, Any], list[str]]:
    meta = json.loads(_run(repo, "gh", "pr", "view", str(number), "--json", "number,headRefName,headRefOid,baseRefName,state,isDraft,labels,mergeable,mergeStateStatus,reviewDecision,reviews"))
    slug = _run(repo, "gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    owner, name = slug.split("/", 1)
    threads_query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}"
    threads = json.loads(_run(repo, "gh", "api", "graphql", "-f", f"query={threads_query}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"))
    checks = json.loads(_run(repo, "gh", "pr", "checks", str(number), "--json", "name,state,bucket,workflow,link"))
    for check in checks:
        if check.get("bucket") == "fail" or check.get("state") == "FAILURE":
            run_id = re.search(r"/runs/(\d+)", check.get("link", ""))
            if run_id:
                check["failureDetails"] = _run(repo, "gh", "run", "view", run_id.group(1), "--log-failed", check=False)[-12000:]
    meta["checks"] = checks
    nodes = threads.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    keys = [f"comment:{item['comments']['nodes'][0]['databaseId']}" for item in nodes if not item.get("isResolved") and item.get("comments", {}).get("nodes")]
    keys += [f"review:{review['id']}" for review in meta.get("reviews", []) if review.get("state") == "CHANGES_REQUESTED"]
    keys += [f"check:{meta['headRefOid']}:{item.get('workflow','')}:{item['name']}:{item['state']}" for item in checks if item.get("bucket") == "fail" or item.get("state") == "FAILURE"]
    if meta.get("mergeable") == "CONFLICTING" or meta.get("mergeStateStatus") == "DIRTY":
        keys.append(f"conflict:{meta['headRefOid']}")
    return meta, keys


def _prompt(repo: Path, number: int, keys: list[str], base_branch: str, conflicts: list[str]) -> str:
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions)
    shared_skill = SHARED_PR_FIX_SKILL.read_text(encoding="utf-8") if SHARED_PR_FIX_SKILL.is_file() else ""
    target_skill = repo / ".codex/skills/pr-fix/SKILL.md"
    target_skill_text = target_skill.read_text(encoding="utf-8") if target_skill.is_file() else ""
    changed_files = _run(repo, "git", "diff", "--name-only", f"origin/{base_branch}...HEAD", check=False)
    changed = [path for path in changed_files.splitlines() if path and (repo / path).exists()]
    changed_text = " ".join(changed) if changed else "<no changed files detected>"
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    return f"""Fix actionable GitHub feedback for PR #{number} in this isolated worktree.
Shared dev-agents PR-fix workflow:
{shared_skill}

Target-repository PR-fix workflow (if present):
{target_skill_text}

Read and obey these repository instructions:
{instruction_text}

Actionable feedback identities:
{chr(10).join('- ' + key for key in keys)}

Changed files (use these paths for targeted linting):
{changed_text}

Merge-conflict paths (resolve, stage, commit, and push them before testing):
{conflict_text}

Run focused tests first. For linting, run ESLint/format checks only against the changed files above
(for example, `bunx eslint <changed-files>` and `bunx prettier --check <changed-files>`); do not run
the repository-wide `bun run lint` unless a targeted command is unavailable. Run the repository's
type check only when required by its local instructions. Commit and push HEAD to the PR branch.
Do not close or merge the PR."""


class PrFixerRunState(TypedDict, total=False):
    """State carried through one webhook/reconciliation PR-fix run."""

    number: int
    meta: dict[str, Any]
    keys: list[str]
    handled: list[str]
    unseen: list[str]
    state_path: Path
    skip: bool
    fixed: bool
    started: bool


class PrFixerService:
    def __init__(self, project_name: str, project: ProjectConfig, config: PrFixerConfig):
        self.project_name, self.project, self.config = project_name, project, config
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
            return bool(result.get("started", False))
        finally:
            with self.lock:
                self.active.discard(number)

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

    def _collect_feedback(self, state: PrFixerRunState) -> dict[str, Any]:
        number = state["number"]
        meta, keys = _feedback(self.project.repo, number)
        labels = {label["name"].lower() for label in meta.get("labels", [])}
        if meta.get("baseRefName") != self.config.base_branch or meta.get("isDraft") or "paused" in labels:
            return {"meta": meta, "keys": keys, "skip": True}
        state_path = _state_path(self.config, self.project_name)
        records = _load_state(state_path).setdefault("pullRequests", {})
        handled = records.get(str(number), [])
        unseen = [key for key in keys if key not in handled]
        return {
            "meta": meta,
            "keys": keys,
            "handled": handled,
            "unseen": unseen,
            "state_path": state_path,
            "skip": False,
        }

    def _remediate(self, state: PrFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("unseen"):
            return {"started": False, "fixed": False}
        number = state["number"]
        unseen = state["unseen"]
        _log(f"starting pr={number} actionable_items={len(unseen)}")
        fixed = self._fix(number, state["meta"]["headRefName"], unseen)
        if not fixed:
            _log(f"fix-incomplete pr={number}; feedback remains actionable")
        return {"started": fixed, "fixed": fixed}

    def _finalize(self, state: PrFixerRunState) -> dict[str, Any]:
        if state.get("skip"):
            return {"started": False}
        number = state["number"]
        meta, keys = state["meta"], state["keys"]
        if not state.get("unseen"):
            if self.config.auto_merge and self._auto_merge_eligible(meta, keys):
                self._request_auto_merge(number)
            return {"started": False}
        if not state.get("fixed"):
            return {"started": False}
        slug = _run(self.project.repo, "gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        for key in state["unseen"]:
            if key.startswith("comment:"):
                comment_id = key.split(":", 1)[1]
                _run(self.project.repo, "gh", "api", f"repos/{slug}/pulls/{number}/comments/{comment_id}/replies", "-f", "body=Processed by the PR fixer; verification completed.", check=False)
        refreshed_meta, refreshed = _feedback(self.project.repo, number)
        if all(key not in refreshed or key.startswith("comment:") for key in state["unseen"]):
            saved = _load_state(state["state_path"])
            records = saved.setdefault("pullRequests", {})
            records[str(number)] = sorted(set(state.get("handled", []) + state["unseen"]))
            _save_state(state["state_path"], saved)
            if self.config.auto_merge and self._auto_merge_eligible(refreshed_meta, refreshed):
                self._request_auto_merge(number)
        return {"started": True}

    def _fix(self, number: int, branch: str, keys: list[str]) -> bool:
        base = self.config.base_branch
        root = (self.config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser()
        succeeded = False
        try:
            with isolated_worktree(self.project.repo, root, branch, base) as (worktree, conflicts):
                _log(f"worktree pr={number} path={worktree}")
                if conflicts:
                    _log(f"merge-conflict pr={number} paths={','.join(conflicts)}")
                log_dir = (self.config.log_dir or Path.home() / ".local/state/dev-agents" / self.project_name / "logs").expanduser()
                log_dir.mkdir(parents=True, exist_ok=True)
                log = log_dir / f"pr-{number}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.log"
                prompt = _prompt(worktree, number, keys, base, conflicts)
                for provider in self.config.providers:
                    _log(f"agent-start pr={number} provider={provider} log={log}")
                    result = run_agent(
                        provider,
                        prompt,
                        cwd=worktree,
                        log_path=log,
                        timeout_seconds=self.config.timeout_minutes * 60,
                        heartbeat_seconds=self.config.heartbeat_seconds,
                    )
                    result_code = result.returncode
                    if result.timed_out:
                        _log(f"agent-timeout pr={number} provider={provider}")
                    _log(f"agent-finished pr={number} provider={provider} exit={result_code}")
                    _run(worktree, "git", "fetch", "origin", branch, check=False)
                    pushed = _run(worktree, "git", "rev-parse", "HEAD", check=False) == _run(worktree, "git", "rev-parse", f"origin/{branch}", check=False)
                    if result_code == 0 and pushed and not _run(worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False) and _run(worktree, "git", "status", "--porcelain") == "":
                        _log(f"agent-accepted pr={number} provider={provider}")
                        succeeded = True
                        break
        except RuntimeError as error:
            _log(f"worktree-failed pr={number} error={error}")
        return succeeded

    def _auto_merge_eligible(self, meta: dict[str, Any], keys: list[str]) -> bool:
        labels = {label["name"].lower() for label in meta.get("labels", [])}
        return (
            meta.get("state") == "OPEN"
            and meta.get("baseRefName") == self.config.base_branch
            and not meta.get("isDraft")
            and "paused" not in labels
            and meta.get("mergeable") == "MERGEABLE"
            and meta.get("reviewDecision") != "CHANGES_REQUESTED"
            and not any(item.get("bucket") == "pending" or item.get("state") in {"PENDING", "IN_PROGRESS", "QUEUED"} for item in meta.get("checks", []))
            and not any(not key.startswith("comment:") for key in keys)
        )

    def _request_auto_merge(self, number: int) -> None:
        _log(f"auto-merge-wait pr={number} seconds={self.config.auto_merge_quiet_seconds}")
        time.sleep(self.config.auto_merge_quiet_seconds)
        meta, keys = _feedback(self.project.repo, number)
        if not self._auto_merge_eligible(meta, keys):
            _log(f"auto-merge-cancelled pr={number} reason=state-changed")
            return
        _run(self.project.repo, "gh", "pr", "merge", str(number), "--auto", "--squash")
        _log(f"auto-merge-requested pr={number} mode=squash")

    def reconcile(self) -> None:
        try:
            raw = _run(self.project.repo, "gh", "pr", "list", "--base", self.config.base_branch, "--state", "open", "--json", "number,labels")
            prs = json.loads(raw)
        except (RuntimeError, json.JSONDecodeError) as error:
            _log(f"reconcile-failed error={error}")
            return
        for pr in prs:
            if any(label.get("name", "").lower() == "paused" for label in pr.get("labels", [])):
                continue
            number = pr.get("number")
            if isinstance(number, int):
                Thread(target=self.handle, args=(number,), daemon=True).start()

    def reconcile_loop(self) -> None:
        while True:
            time.sleep(self.config.reconcile_interval_seconds)
            _log("reconcile-start")
            self.reconcile()


def _signature_valid(body: bytes, received: str | None, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return received is not None and hmac.compare_digest(received, expected)


def serve(project_name: str, project: ProjectConfig, config: PrFixerConfig) -> None:
    secret = os.environ.get(config.webhook_secret_env)
    if not secret:
        raise ConfigError(f"Set {config.webhook_secret_env} before starting the PR fixer")
    webhook_secret = secret

    _cleanup_artifacts(project_name, config)
    service = PrFixerService(project_name, project, config)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404); self.end_headers()
            self.wfile.write(json.dumps({"ok": self.path == "/health", "active_jobs": len(service.active)}).encode())
        def do_POST(self) -> None:
            event = self.headers.get("X-GitHub-Event", "")
            delivery = self.headers.get("X-GitHub-Delivery", "unknown")
            content_length = int(self.headers.get("content-length", "0"))
            if content_length > MAX_BODY_BYTES:
                self.send_response(413); self.end_headers(); return
            body = self.rfile.read(content_length)
            if self.path != config.webhook_path or not _signature_valid(body, self.headers.get("X-Hub-Signature-256"), webhook_secret):
                _log(f"rejected delivery={delivery} event={event or 'unknown'} reason=invalid-path-or-signature")
                self.send_response(401); self.end_headers(); return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                _log(f"rejected delivery={delivery} event={event or 'unknown'} reason=invalid-json")
                self.send_response(400); self.end_headers(); return
            action = payload.get("action")
            if event == "push" and payload.get("ref") == f"refs/heads/{config.base_branch}":
                _log(f"accepted delivery={delivery} event=push branch={config.base_branch}; reconciling")
                Thread(target=service.reconcile, daemon=True).start()
                self.send_response(202); self.end_headers(); return
            check_pull_requests = payload.get("check_run", {}).get("pull_requests") or []
            check_pr_number = check_pull_requests[0].get("number") if check_pull_requests else None
            number = payload.get("number") or payload.get("pull_request", {}).get("number") or check_pr_number
            if payload.get("repository", {}).get("full_name") != project.github or action not in _ACTIONS.get(event, set()) or not isinstance(number, int):
                _log(f"ignored delivery={delivery} event={event or 'unknown'} action={action or 'unknown'} pr={number or 'unknown'}")
                self.send_response(200); self.end_headers(); return
            def run() -> None:
                try:
                    started = service.handle(number)
                    _log(f"handled delivery={delivery} event={event} action={action} pr={number} started={started}")
                except Exception as error:  # noqa: BLE001 - daemon must log worker failures
                    _log(f"failed delivery={delivery} event={event} action={action} pr={number} error={error}")
            Thread(target=run, daemon=True).start()
            _log(f"accepted delivery={delivery} event={event} action={action} pr={number}")
            self.send_response(202); self.end_headers()
        def log_message(self, *_: object) -> None: pass
    _log(f"listening on 127.0.0.1:{config.port}{config.webhook_path} for {project.github}")
    Thread(target=service.reconcile_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", config.port), Handler).serve_forever()


def serve_project(config_path: Path, project_name: str) -> None:
    project = select_project(load_projects_config(config_path), project_name)
    if project.pr_fixer is None or project.github is None:
        raise ConfigError(f"Project {project_name!r} requires github and pr_fixer configuration")
    serve(project_name, project, project.pr_fixer)
