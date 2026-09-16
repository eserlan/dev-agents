"""Label-driven issue remediation that hands the result to the PR fixer."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, Thread
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from dev_agents.config import IssueFixerConfig, PrFixerConfig, ProjectConfig
from dev_agents.context.instructions import discover_instructions
from dev_agents.runtime import StateRepository, repository_slug, run_agent
from dev_agents.runtime.agent import AgentResult

SHARED_PR_FIX_SKILL = Path(__file__).resolve().parents[2] / "skills/pr-fix/SKILL.md"
ISSUE_FIX_PROVIDER = "codex"


def _run(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            args, cwd=repo, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout:g}s") from error
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout.strip()


def _issue(repo: Path, number: int) -> dict[str, Any]:
    raw = _run(
        repo,
        "gh",
        "issue",
        "view",
        str(number),
        "--json",
        "number,title,body,labels,state,author,createdAt,updatedAt,url,comments",
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("gh issue view returned an invalid payload")
    return value


def _open_bug_issues(repo: Path, config: IssueFixerConfig) -> list[dict[str, Any]]:
    raw = _run(
        repo,
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        config.label,
        "--limit",
        str(config.max_open_issues),
        "--json",
        "number,title,body,labels,state,author,createdAt,updatedAt,url",
    )
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError("gh issue list returned an invalid payload")
    label = config.label.strip().lower()
    return [
        item
        for item in value
        if str(item.get("state", "")).upper() == "OPEN"
        and any(str(tag.get("name", "")).lower() == label for tag in item.get("labels", []))
    ]


def _existing_issue_pr(repo: Path, number: int) -> dict[str, Any] | None:
    marker = f"dev-agents:issue-fix issue={number}"
    closing_reference = re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\s*{number}\b", re.IGNORECASE
    )
    raw = _run(
        repo,
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,body,headRefName,url",
    )
    value = json.loads(raw)
    if not isinstance(value, list):
        raise TypeError("gh pr list returned an invalid payload")
    for item in value:
        if isinstance(item, dict) and (
            marker in str(item.get("body", ""))
            or closing_reference.search(str(item.get("body", "")))
        ):
            return item
    return None


def _publish_issue_comment(repo: Path, number: int, marker: str, body: str) -> bool:
    """Create or update one idempotent issue comment."""
    slug = repository_slug(repo)
    try:
        raw = _run(repo, "gh", "api", f"repos/{slug}/issues/{number}/comments")
        comments = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError):
        return False
    existing = next(
        (
            item
            for item in comments
            if isinstance(item, dict) and marker in str(item.get("body", ""))
        ),
        None,
    )
    if isinstance(existing, dict) and existing.get("id"):
        try:
            _run(
                repo,
                "gh",
                "api",
                f"repos/{slug}/issues/comments/{existing['id']}",
                "-X",
                "PATCH",
                "-f",
                f"body={body}",
            )
        except RuntimeError:
            return False
        return True
    try:
        _run(
            repo,
            "gh",
            "api",
            f"repos/{slug}/issues/{number}/comments",
            "-f",
            f"body={body}",
        )
    except RuntimeError:
        return False
    return True


def _issue_start_body(number: int, run_id: str, issue: dict[str, Any]) -> str:
    marker = f"<!-- dev-agents:issue-fix-started issue={number} run={run_id} -->"
    return f"""{marker}
### 🤖 Dev-agents issue fixer started

Codex is investigating issue #{number}: **{issue.get('title', 'Untitled issue')}**.
It will work in an isolated branch, validate the change, and open a PR for the normal review pipeline.
"""


def _issue_result_body(number: int, run_id: str, pr_url: str | None, summary: str) -> str:
    marker = f"<!-- dev-agents:issue-fix-result issue={number} run={run_id} -->"
    destination = f"[Open the PR]({pr_url})" if pr_url else "No PR was created."
    return f"""{marker}
### 🤖 Dev-agents issue fixer result

{summary}

{destination}
"""


def _prompt(
    repo: Path,
    issue: dict[str, Any],
    base_branch: str,
    branch: str,
    conflicts: list[str],
) -> str:
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(
        f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions
    )
    shared_skill = SHARED_PR_FIX_SKILL.read_text(encoding="utf-8") if SHARED_PR_FIX_SKILL.is_file() else ""
    title = str(issue.get("title", "Untitled issue"))
    body = str(issue.get("body") or "(issue has no body)")[:30_000]
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    return f"""Fix GitHub issue #{issue.get('number')} in this isolated worktree.

Issue title: {title}
Issue URL: {issue.get('url', '')}
Issue body:
{body}

Shared dev-agents PR-fix workflow:
{shared_skill}

Read and obey these repository instructions:
{instruction_text}

Branch: {branch} (based on {base_branch})
Merge-conflict paths: {conflict_text}

Investigate the issue and implement the smallest complete fix. Treat security acceptance criteria
as binding. Do not guess at production data or revoke legitimate access without evidence; make
the code and migration safe for every deployment environment. Add focused tests, and use the
target repository's optimized validation commands when available. Keep independent lint, test,
and type-check commands parallel where practical.

Commit the fix and push the branch to origin. Do not merge or close the issue/PR. If the issue is
not safely actionable from the repository, leave the tree clean and explain the blocker instead
of inventing a solution.

At the end, print exactly these fields as short plain-text lines:
ISSUE_FIX_REPORT_BEGIN
SUMMARY: <what was fixed, or the concrete blocker>
VALIDATION: <commands and results>
ISSUE_FIX_REPORT_END
"""


@contextmanager
def _isolated_issue_worktree(
    repo: Path, root: Path, branch: str, base_branch: str
) -> Iterator[tuple[Path, list[str]]]:
    root.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="issue-", dir=root))
    try:
        _run(repo, "git", "fetch", "origin", base_branch)
        remote_branch = _run(
            repo,
            "git",
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            branch,
            check=False,
        )
        if remote_branch:
            _run(
                repo,
                "git",
                "fetch",
                "origin",
                f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            )
            _run(repo, "git", "worktree", "add", "--detach", str(worktree), f"origin/{branch}")
        else:
            _run(repo, "git", "worktree", "add", "--detach", str(worktree), f"origin/{base_branch}")
            _run(worktree, "git", "switch", "-c", branch)
        merge = subprocess.run(
            ["git", "merge", f"origin/{base_branch}", "--no-edit"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        conflicts = _run(
            worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False
        ).splitlines()
        if merge.returncode != 0 and not conflicts:
            raise RuntimeError(f"merge failed for base branch {base_branch}: {merge.stderr.strip()}")
        yield worktree, conflicts
    finally:
        _run(repo, "git", "worktree", "remove", "--force", str(worktree), check=False)


class IssueFixerRunState(TypedDict, total=False):
    issue: dict[str, Any]
    number: int
    run_id: str
    branch: str
    claimed: bool
    skip: bool
    fixed: bool
    pr_url: str | None
    summary: str
    validation: str


class IssueFixerService:
    """Poll and remediate open issues carrying the configured bug label."""

    def __init__(
        self,
        project_name: str,
        project: ProjectConfig,
        issue_config: IssueFixerConfig,
        pr_config: PrFixerConfig,
        state: StateRepository,
    ) -> None:
        self.project_name = project_name
        self.project = project
        self.config = issue_config
        self.pr_config = pr_config
        self.state = state
        self.active: set[int] = set()
        self.lock = Lock()
        graph = StateGraph(IssueFixerRunState)
        graph.add_node("collect_issue", self._collect_issue)
        graph.add_node("remediate", self._remediate)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "collect_issue")
        graph.add_edge("collect_issue", "remediate")
        graph.add_edge("remediate", "finalize")
        graph.add_edge("finalize", END)
        self.workflow = graph.compile()

    def handle(self, number: int) -> bool:
        with self.lock:
            if number in self.active:
                return False
            self.active.add(number)
        try:
            result = self.workflow.invoke({"number": number})
            return bool(result.get("fixed", False))
        finally:
            with self.lock:
                self.active.discard(number)

    def _collect_issue(self, state: IssueFixerRunState) -> dict[str, Any]:
        number = state["number"]
        issue = _issue(self.project.repo, number)
        labels = {str(label.get("name", "")).lower() for label in issue.get("labels", [])}
        if (
            str(issue.get("state", "")).upper() != "OPEN"
            or self.config.label.strip().lower() not in labels
            or "paused" in labels
        ):
            return {"issue": issue, "skip": True}
        if _existing_issue_pr(self.project.repo, number) is not None:
            return {"issue": issue, "skip": True}
        fingerprint = hashlib.sha256(
            f"{issue.get('title', '')}\0{issue.get('body', '')}".encode()
        ).hexdigest()[:16]
        run_id = f"issue-fix-{number}-{fingerprint}"
        claim = self.state.claim_run(
            "issue-fixer",
            run_id,
            metadata={
                "issue": number,
                "label": self.config.label,
                "base_branch": self.config.base_branch,
                "branch": f"{self.config.branch_prefix}{number}",
            },
        )
        if not claim.claimed:
            return {"issue": issue, "run_id": run_id, "skip": True}
        self.state.record_event(
            "issue-fixer",
            run_id,
            "collect_issue",
            "issue_claimed",
            metadata={"issue": number, "label": self.config.label},
        )
        return {
            "issue": issue,
            "run_id": run_id,
            "branch": f"{self.config.branch_prefix}{number}",
            "claimed": True,
        }

    def _remediate(self, state: IssueFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("claimed"):
            return {"fixed": False}
        number = state["number"]
        run_id = state["run_id"]
        issue = state["issue"]
        _publish_issue_comment(
            self.project.repo,
            number,
            f"dev-agents:issue-fix-started issue={number} run={run_id}",
            _issue_start_body(number, run_id, issue),
        )
        log_dir = (
            self.pr_config.log_dir
            or Path.home() / ".local/state/dev-agents" / self.project_name / "logs"
        ).expanduser()
        log_path = log_dir / f"issue-{number}-{run_id.rsplit('-', 1)[-1]}.log"
        fixed = False
        summary = "Agent did not produce a clean pushed fix."
        validation = f"Agent log: {log_path}"
        try:
            with _isolated_issue_worktree(
                self.project.repo,
                (self.pr_config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser(),
                state["branch"],
                self.config.base_branch,
            ) as (worktree, conflicts):
                base_sha = _run(worktree, "git", "rev-parse", f"origin/{self.config.base_branch}")
                remote_before = _run(
                    worktree,
                    "git",
                    "rev-parse",
                    f"origin/{state['branch']}",
                    check=False,
                )
                current = _run(worktree, "git", "rev-parse", "HEAD", check=False)
                if remote_before and current == remote_before and remote_before != base_sha and not conflicts:
                    fixed = True
                    summary = "Reused the previously pushed issue fix and verified its branch."
                    validation = "Existing remote branch is clean and ahead of the configured base."
                else:
                    result: AgentResult = run_agent(
                        ISSUE_FIX_PROVIDER,
                        _prompt(worktree, issue, self.config.base_branch, state["branch"], conflicts),
                        cwd=worktree,
                        log_path=log_path,
                        timeout_seconds=self.config.timeout_minutes * 60,
                        heartbeat_seconds=self.pr_config.heartbeat_seconds,
                        reasoning_effort=self.pr_config.reasoning_effort,
                    )
                    _run(
                        worktree,
                        "git",
                        "fetch",
                        "origin",
                        f"refs/heads/{state['branch']}:refs/remotes/origin/{state['branch']}",
                        check=False,
                    )
                    head = _run(worktree, "git", "rev-parse", "HEAD", check=False)
                    remote = _run(
                        worktree, "git", "rev-parse", f"origin/{state['branch']}", check=False
                    )
                    clean = _run(worktree, "git", "status", "--porcelain", check=False) == ""
                    pushed_change = bool(head and head != base_sha and head == remote)
                    fixed = result.returncode == 0 and not result.timed_out and clean and pushed_change
                    validation = (
                        f"exit={result.returncode} timed_out={result.timed_out} "
                        f"clean={clean} head_matches_remote={head == remote} head_ahead_of_base={head != base_sha}; "
                        f"log: {log_path}"
                    )
                    if not fixed and result.timed_out:
                        summary = "Agent timed out before producing a verified pushed fix."
        except RuntimeError as error:
            return {"fixed": False, "summary": f"Issue fixer blocked: {error}", "validation": "none"}

        if not fixed:
            return {"fixed": False, "summary": summary, "validation": validation}

        try:
            pr_url = _run(
                self.project.repo,
                "gh",
                "pr",
                "create",
                "--base",
                self.config.base_branch,
                "--head",
                state["branch"],
                "--title",
                f"fix: {str(issue.get('title', 'bug'))[:60]}",
                "--body",
                f"<!-- dev-agents:issue-fix issue={number} run={run_id} -->\n\nFixes #{number}.\n\nCreated by the dev-agents issue fixer; the normal PR review and validation pipeline will run.",
            )
        except RuntimeError as error:
            return {"fixed": False, "summary": f"Fix was pushed but PR creation failed: {error}", "validation": f"log: {log_path}"}
        return {
            "fixed": True,
            "pr_url": pr_url,
            "summary": "Implemented and pushed the issue fix.",
            "validation": f"Agent log: {log_path}",
        }

    def _finalize(self, state: IssueFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("claimed"):
            return {"fixed": False}
        run_id = state["run_id"]
        number = state["number"]
        summary = state.get("summary", "No verified fix was produced.")
        pr_url = state.get("pr_url")
        self.state.record_event(
            "issue-fixer",
            run_id,
            "finalize",
            "pr_created" if state.get("fixed") else "fix_failed",
            status="completed" if state.get("fixed") else "failed",
            metadata={"pr_url": pr_url, "summary": summary, "validation": state.get("validation")},
        )
        self.state.complete_run(
            "issue-fixer",
            run_id,
            status="completed" if state.get("fixed") else "failed",
            error=None if state.get("fixed") else summary,
            metadata={"issue": number, "pr_url": pr_url, "summary": summary},
        )
        validation = state.get("validation", "Validation was not reported.")
        _publish_issue_comment(
            self.project.repo,
            number,
            f"dev-agents:issue-fix-result issue={number} run={run_id}",
            _issue_result_body(number, run_id, pr_url, f"{summary}\n\n{validation}"),
        )
        return {"fixed": bool(state.get("fixed"))}

    def reconcile(self) -> None:
        run_id = f"reconcile-issues-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16]}"
        claim = self.state.claim_run(
            "issue-reconcile",
            run_id,
            metadata={"label": self.config.label, "base_branch": self.config.base_branch},
        )
        if not claim.claimed:
            return
        try:
            issues = _open_bug_issues(self.project.repo, self.config)
            for issue in issues:
                number = issue.get("number")
                if isinstance(number, int):
                    Thread(target=self._handle_reconciled, args=(number,), daemon=True).start()
            self.state.complete_run(
                "issue-reconcile", run_id, metadata={"open_bug_issues": len(issues)}
            )
        except (RuntimeError, json.JSONDecodeError) as error:
            self.state.complete_run("issue-reconcile", run_id, status="failed", error=str(error))

    def _handle_reconciled(self, number: int) -> None:
        try:
            self.handle(number)
        except Exception as error:  # noqa: BLE001 - one issue must not stop polling
            print(f"[issue-fixer] issue={number} failed: {error}", flush=True)

    def reconcile_loop(self) -> None:
        while True:
            time.sleep(max(30, self.pr_config.reconcile_interval_seconds))
            self.reconcile()
