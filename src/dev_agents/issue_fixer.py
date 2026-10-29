"""Label-driven issue remediation that hands the result to the PR fixer."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, Thread
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from dev_agents.config import IssueFixerConfig, PrFixerConfig, ProjectConfig
from dev_agents.copy_review import review_answer_draft
from dev_agents.pr_fixer import _log
from dev_agents.runtime import (
    StateRepository,
    repo_git_lock,
    repo_worktree_semaphore,
    repository_slug,
    run_agent,
    run_with_fallback,
)
from dev_agents.runtime.agent import AgentResult

SHARED_PR_FIX_SKILL = Path(__file__).resolve().parents[2] / "skills/pr-fix/SKILL.md"


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
    # A reopen is a new remediation cycle even when the title/body is unchanged.
    # Read durable GitHub history so reconciliation also recovers missed webhooks.
    pages = json.loads(
        _run(
            repo,
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{number}/events",
            "--paginate",
            "--slurp",
        )
    )
    reopened = [event for page in pages for event in page if event.get("event") == "reopened"]
    if reopened:
        value["reopenEventId"] = str(max(reopened, key=lambda event: int(event["id"]))["id"])
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
        and not any(
            str(tag.get("name", "")).lower() == "paused"
            for tag in item.get("labels", [])
        )
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


def _publish_issue_comment(repo: Path, number: int, body: str) -> bool:
    """Post one new issue comment; never overwrites an existing one."""
    slug = repository_slug(repo)
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
    marker = f"<!-- dev-agents:issue-fix issue={number} run={run_id} -->"
    return f"""{marker}
### 🤖 Dev-agents issue fixer started

Codex is investigating issue #{number}: **{issue.get('title', 'Untitled issue')}**.
It will work in an isolated branch, validate the change, and open a PR for the normal review pipeline.
"""


def _issue_result_body(number: int, run_id: str, pr_url: str | None, summary: str) -> str:
    marker = f"<!-- dev-agents:issue-fix issue={number} run={run_id} -->"
    destination = f"[Open the PR]({pr_url})" if pr_url else "No PR was created."
    return f"""{marker}
### 🤖 Dev-agents issue fixer result

{summary}

{destination}
"""


def _issue_auto_pause_body(number: int, run_id: str, attempt: int, reason: str) -> str:
    return f"""<!-- dev-agents:issue-fix issue={number} run={run_id} -->
### 🤖 Dev-agents issue fixer auto-paused

Issue #{number} failed {attempt} consecutive automation attempts ({reason}), so dev-agents applied
the `paused` label to stop retrying and posting duplicate comments here.

Remove the `paused` label once the underlying problem is resolved to resume automation.
"""


def _pr_result_body(number: int, run_id: str, summary: str) -> str:
    marker = f"<!-- dev-agents:issue-fix issue={number} run={run_id} -->"
    return f"""{marker}
### 🤖 Dev-agents issue fixer result

Fixes #{number}.

{summary}
"""


def _pr_number_from_url(pr_url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", pr_url)
    return int(match.group(1)) if match else None


_KIND_FRAMING = {
    "fix": {
        "verb": "Fix",
        "goal": "implement the smallest complete fix",
        "artifact": "fix",
        "report_field": "what was fixed, or the concrete blocker",
        "title_prefix": "fix",
    },
    "enhancement": {
        "verb": "Implement the GUI enhancement described in",
        "goal": "implement the smallest complete version of the enhancement",
        "artifact": "enhancement",
        "report_field": "what was implemented, or the concrete blocker",
        "title_prefix": "feat",
    },
    "copy": {
        "verb": "Address the copy review in",
        "goal": "make the requested copy edits while preserving the intended meaning and voice",
        "artifact": "copy changes",
        "report_field": "what copy was changed, or the concrete blocker",
        "title_prefix": "docs",
        "guidance": """Treat this as a focused copyediting request. Locate the source passage in the
target repository and use surrounding content for context. Make the requested editorial changes
        while preserving factual claims, game rules, character voice, terminology, Markdown structure,
        links, and formatting. Limit edits to the requested copy; do not make unrelated code or content
        changes. If the requested correction is ambiguous or cannot be verified from the repository,
        leave the copy unchanged and explain what needs clarification.""",
    },
    "agent": {
        "verb": "Address",
        "goal": "implement the smallest complete solution requested by the issue",
        "artifact": "implementation",
        "report_field": "what was implemented, or the concrete blocker",
        "title_prefix": "agent",
    },
    "answer": {
        "verb": "Create the public Answer article requested in",
        "goal": "write and integrate a complete Answer article in the existing site content system",
        "artifact": "Answer article",
        "report_field": "which article was created and how it was validated",
        "title_prefix": "answer",
        "guidance": """Treat the issue as an article brief, not a request to answer in an issue comment.
Read and follow `.agents/skills/add-answer/SKILL.md` before editing; it governs the Answer schema,
discovery intent, required R2 illustration, and validation steps. Keep changes scoped to the article
and its required registry or index entries.

Apply this additional dev-agents editorial standard while drafting:
- Choose the teaching structure for the reader's task and level. A rules explainer should work
  through a realistic roll and result; GM advice should show a situation and its table consequence;
  comparisons should explain trade-offs; reference answers should define scope and boundary cases.
  Do not force a before/after story or checklist when it adds no teaching value. If the target
  repository's schema or skill requires a block, make that block useful for this question rather
  than silently omitting it or filling it with generic material.
- Make examples demonstrate the advice. For mechanics, include concrete inputs, modifiers, result,
  and what the reader does next; distinguish an illustrative sequence from a universal rule. For
  GM guidance, show what players can observe or choose and how the fiction responds. For a dice
  explainer, the corrected version in PR #3915 is a useful calibration: calculate a real attack and
  damage result instead of describing one from a distance, and do not imply every damage or healing
  amount requires a preceding d20 roll. Borrow the reader-outcome standard, not that article's format.
- Write for the issue's declared audience. Define terms and abbreviations before relying on them.
  Verify concrete rules against authoritative references, and clearly separate edition-specific
  guidance (including 2014 and 2024 D&D where relevant). Do not invent rules, requirements, or
  certainty.
- Edit the short answer, opening, headings, examples, checklist, and conclusion together. Remove
  repeated explanations, empty reassurance, misleading micro-copy, and examples that narrate rather
  than teach. Keep useful distinctions and practical detail; do not chase an arbitrary length.
- Select related Answers, tools, and product references by the reader's likely next task. Prefer a
  relevant, well-connected neighbour, and omit a tool or Codex Cryptica mention when it does not
  help answer this question. Keep product claims accurate, restrained, and optional.
- The add-answer skill's required example and checklist blocks are schema requirements, not
  optional extras: include them, shape them to this question, and do not add a second generic
  example or checklist alongside them.
- Treat the existing post-merge `copy-fix` review as a safety net, not a drafting stage or a score to
  game. Resolve material quality problems you find now, while remaining open to valid editorial
  corrections later; the goal is a useful Answer, not zero review findings.
- Before committing, do a critical read against the original issue and these points: name the
  reader's actual problem, check whether the example proves the advice and enables the reader to do
  the task, look for a counterexample that breaks an absolute claim, and verify that the ending
  follows from the article's own guidance. Judge the result by reader outcomes, not by whether each
  checklist item is present. Correct material problems rather than merely reporting them in the PR
  summary.""",
    },
    "help": {
        "verb": "Improve the help content requested in",
        "goal": "make the smallest complete improvement to the help content for the named feature",
        "artifact": "help content changes",
        "report_field": "which help content changed and how it was validated",
        "title_prefix": "docs",
        "guidance": """Treat this as a documentation task for the feature identified by the issue.
Inspect the target repository's instructions and existing help content system before editing. Find
where help for that feature belongs, follow its format and terminology, and verify product behavior
from the repository rather than inventing instructions. Improve the existing help entry when one
exists; add an entry only when the feature has no suitable help content. Keep changes focused on
help content and its required indexes or registries. If the feature or requested guidance is unclear,
leave content unchanged and explain what needs clarification.""",
    },
}


PLAN_REQUIRED_FIELD = "ISSUE_FIX_PLAN_REQUIRED"
MAX_POSTED_PLAN_CHARS = 50_000

_ASSESS_SCOPE = f"""Before editing anything, assess the issue's scope. If it is comprehensive, meaning it
needs coordinated changes across several layers (for example database migrations or functions,
server code and UI together) or a design decision spanning many files, do not start implementing.
Leave the tree clean, print exactly one line and stop:
{PLAN_REQUIRED_FIELD}: <why this issue needs a plan, in one line>
A planning pass at higher reasoning effort then writes the plan, and you implement it in a new run.
For a smaller issue, implement it directly and do not print that line."""


def _plan_section(plan: str) -> str:
    return f"""A planning pass assessed this issue as comprehensive and wrote the implementation plan
below. Implement the whole plan in this run, in order, and validate it as the plan says. Where the
code shows that a step is wrong, adapt it and say so in SUMMARY. A step being large or touching
schema, finance or other sensitive logic is not a blocker: the plan exists to handle it.

Implementation plan:
{plan}"""


def _planning_prompt(issue: dict[str, Any], base_branch: str) -> str:
    """A read-only pass that plans a comprehensive issue for a lower-effort implementation run."""
    title = str(issue.get("title", "Untitled issue"))
    body = str(issue.get("body") or "(issue has no body)")[:30_000]
    return f"""Plan the implementation of GitHub issue #{issue.get('number')} in this repository
(based on {base_branch}). This is a read-only planning pass: inspect the code, but do not edit
files, run migrations or commit. A separate implementation run at lower reasoning effort follows
your plan, so make it concrete and complete.

Issue title: {title}
Issue URL: {issue.get('url', '')}
Issue body:
{body}

Plan the whole issue, not a reduced version. Treat explicit acceptance criteria and repository
instructions (AGENTS.md and similar files) as binding. Cover:
1. Approach: the design in a few sentences and the existing code it builds on.
2. Steps, in order: for each, the files to create or change and what changes, including
   migrations, database functions, server code, UI and tests.
3. Tests: which test proves each acceptance criterion.
4. Validation: the commands to run, in order.
5. Risks: what could go wrong and how the steps guard against it.

Your final message is the plan in Markdown and nothing else.
"""


def _last_agent_output(log_path: Path) -> str:
    """The latest agent run's part of an issue log, which appends one run after another."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.rsplit("=== agent started", 1)[-1]


def _reported_line(output: str, field: str) -> str:
    """The agent's last value for a report field, ignoring the prompt's `<placeholder>` echo."""
    matches = re.findall(rf"^{field}: (?!<)(.+)$", output, flags=re.MULTILINE)
    return matches[-1].strip() if matches else ""


def _implement_with_planning(
    run: Callable[..., AgentResult],
    *,
    prompt_for: Callable[[str | None], str],
    planning_prompt: str,
    log_path: Path,
    plan_path: Path,
    effort: str | None,
    planning_effort: str,
    on_plan: Callable[[str, str], None],
) -> AgentResult:
    """Implement directly, or plan first at higher effort when the run reports a comprehensive issue.

    The implementation run decides; at most one planning pass happens per attempt.
    """
    result = run(prompt_for(None), effort=effort)
    reason = _reported_line(_last_agent_output(log_path), PLAN_REQUIRED_FIELD)
    if result.returncode != 0 or result.timed_out or not reason:
        return result
    plan_path.unlink(missing_ok=True)
    planned = run(planning_prompt, effort=planning_effort, read_only=True, last_message_path=plan_path)
    plan = plan_path.read_text(encoding="utf-8").strip() if plan_path.is_file() else ""
    if planned.returncode != 0 or planned.timed_out or not plan:
        return AgentResult(planned.returncode or 1, planned.timed_out)
    on_plan(plan, reason)
    return run(prompt_for(plan), effort=effort)


def _plan_comment_body(number: int, run_id: str, reason: str, plan: str, effort: str) -> str:
    shown = plan if len(plan) <= MAX_POSTED_PLAN_CHARS else plan[:MAX_POSTED_PLAN_CHARS] + "\n\n…(truncated)"
    return f"""<!-- dev-agents:issue-fix issue={number} run={run_id} phase=plan -->
### 🤖 Dev-agents planning pass

The implementation run assessed this issue as comprehensive: {reason}

A read-only planning pass at {effort} effort wrote the plan below; implementation now follows it.

<details>
<summary>Implementation plan</summary>

{shown}
</details>
"""


def _prompt(
    repo: Path,
    issue: dict[str, Any],
    base_branch: str,
    branch: str,
    conflicts: list[str],
    *,
    kind: str = "fix",
    assess_scope: bool = False,
    plan: str | None = None,
) -> str:
    framing = _KIND_FRAMING.get(kind, _KIND_FRAMING["fix"])
    if kind == "answer":
        # The pr-fix skill assumes an existing PR under review; an Answer run
        # starts from an issue, and the orchestrator owns the branch and PR.
        shared_skill = """Orchestrator ownership (takes precedence over any conflicting step in
repository skills, including the add-answer skill's branch/PR step):
- Work only on the branch named below, in this worktree. Do not create branches or pull
  requests, and do not merge anything; the orchestrator opens the PR after you push.
- Commit and push that branch when the article is complete. Do not bypass failing
  validation hooks to get a push through; fix the failure or report it as a blocker."""
    else:
        shared_skill = SHARED_PR_FIX_SKILL.read_text(encoding="utf-8") if SHARED_PR_FIX_SKILL.is_file() else ""
    title = str(issue.get("title", "Untitled issue"))
    body = str(issue.get("body") or "(issue has no body)")[:30_000]
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    discussion = ""
    if kind in {"answer", "help"} and isinstance(issue.get("comments"), list):
        notes = []
        for comment in issue["comments"][-6:]:
            if not isinstance(comment, dict):
                continue
            text = str(comment.get("body") or "").strip()
            if text:
                notes.append(text[:2_500])
        if notes:
            discussion = "\n\nIssue discussion:\n" + "\n\n---\n\n".join(notes)
    return f"""{framing['verb']} GitHub issue #{issue.get('number')} in this isolated worktree.

Issue title: {title}
Issue URL: {issue.get('url', '')}
Issue body:
{body}
{discussion}

{"Orchestrator workflow:" if kind == "answer" else "Shared dev-agents PR-fix workflow:"}
{shared_skill}

Branch: {branch} (based on {base_branch})
Merge-conflict paths: {conflict_text}

Investigate the issue and {framing['goal']}. Treat explicit acceptance criteria and target
repository instructions as binding. Make the smallest complete change and keep it safe for every
deployment environment. Add focused tests when behavior changes. Use the target repository's
optimized validation commands when available, and keep independent checks parallel where practical.

{framing.get('guidance', '')}
{_plan_section(plan) if plan else _ASSESS_SCOPE if assess_scope else ''}

Commit the {framing['artifact']} and push the branch to origin. Do not merge or close the issue/PR.
If the issue is not safely actionable from the repository, leave the tree clean and explain the
blocker instead of inventing a solution.

At the end, print exactly these fields as short plain-text lines:
ISSUE_FIX_REPORT_BEGIN
SUMMARY: <{framing['report_field']}>
VALIDATION: <commands and results>
ISSUE_FIX_REPORT_END
"""


def _reset_worktree_for_fallback(worktree: Path, base_branch: str, branch: str) -> None:
    """Discard a failed provider's partial edits before a fallback provider runs.

    Also deletes any remote branch the failed provider managed to push, so the
    fallback's own push is not rejected as a non-fast-forward.
    """
    _run(worktree, "git", "reset", "--hard", f"origin/{base_branch}")
    _run(worktree, "git", "clean", "-fd")
    _run(worktree, "git", "push", "origin", "--delete", branch, check=False)


def _drop_empty_local_branch(repo: Path, branch: str, base_branch: str) -> None:
    """Delete a local branch that has no commits of its own and was never pushed."""
    if not _run(
        repo, "git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    ):
        return
    if _run(repo, "git", "ls-remote", "--heads", "origin", branch, check=False):
        return
    ahead = _run(
        repo, "git", "rev-list", "--count", f"origin/{base_branch}..refs/heads/{branch}", check=False
    )
    if ahead == "0":
        _run(repo, "git", "branch", "-D", branch, check=False)


def _existing_issue_worktree(repo: Path, root: Path, branch: str) -> Path | None:
    """Find an interrupted issue attempt's registered worktree so edits can be reused."""
    listing = _run(repo, "git", "worktree", "list", "--porcelain", check=False)
    for block in listing.split("\n\n"):
        fields = dict(
            line.split(" ", 1) for line in block.splitlines() if " " in line
        )
        path_text = fields.get("worktree")
        if fields.get("branch") != f"refs/heads/{branch}" or not path_text:
            continue
        path = Path(path_text)
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if relative.parts and relative.parts[0].startswith("issue-") and path.is_dir():
            return path
    return None


@contextmanager
def _isolated_issue_worktree(
    repo: Path, root: Path, branch: str, base_branch: str, *, max_concurrent: int = 2
) -> Iterator[tuple[Path, list[str]]]:
    root.mkdir(parents=True, exist_ok=True)
    # Shared with pr_fixer's isolated_worktree via repo_git_lock/repo_worktree_semaphore
    # (both keyed by repo path): fetch/worktree-add mutate the same central checkout,
    # and PR review/fix + issue fix each run a full validation build, so both the git
    # ref race and the CPU oversubscription risk are shared, not per-workflow.
    semaphore = repo_worktree_semaphore(repo, max_concurrent)
    semaphore.acquire()
    try:
        worktree = _existing_issue_worktree(repo, root, branch)
        reused_worktree = worktree is not None
        if worktree is None:
            worktree = Path(tempfile.mkdtemp(prefix="issue-", dir=root))
        lock = repo_git_lock(repo)
        try:
            with lock:
                # See the matching note in runtime/worktree.py's isolated_worktree:
                # a bare `fetch origin <branch>` only populates FETCH_HEAD, not
                # refs/remotes/origin/<branch>, which the merge step below reads.
                _run(
                    repo,
                    "git",
                    "fetch",
                    "origin",
                    f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
                )
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
                if reused_worktree:
                    if remote_branch:
                        _run(
                            repo,
                            "git",
                            "fetch",
                            "origin",
                            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                        )
                elif remote_branch:
                    _run(
                        repo,
                        "git",
                        "fetch",
                        "origin",
                        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                    )
                    _run(
                        repo, "git", "worktree", "add", "--detach", str(worktree), f"origin/{branch}"
                    )
                else:
                    # A previous attempt that failed before pushing leaves its empty local
                    # branch behind (worktree removal keeps the ref), and `switch -c` would then
                    # fail with "already exists" on every retry. Drop it if it holds no work;
                    # if it does hold unpushed commits, continue from them instead.
                    _drop_empty_local_branch(repo, branch, base_branch)
                    has_local_branch = bool(
                        _run(
                            repo, "git", "rev-parse", "--verify", "--quiet",
                            f"refs/heads/{branch}", check=False,
                        )
                    )
                    _run(
                        repo,
                        "git",
                        "worktree",
                        "add",
                        "--detach",
                        str(worktree),
                        f"origin/{base_branch}",
                    )
                    if has_local_branch:
                        _run(worktree, "git", "switch", branch)
                    else:
                        _run(worktree, "git", "switch", "-c", branch)
            # rerere has no business in a one-shot automated worktree merge -- see
            # the matching note in runtime/worktree.py's isolated_worktree.
            merge_in_progress = bool(
                _run(worktree, "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False)
            )
            merge = (
                subprocess.CompletedProcess([], 1, "", "merge already in progress")
                if merge_in_progress
                else subprocess.run(
                    ["git", "-c", "rerere.enabled=false", "merge", f"origin/{base_branch}", "--no-edit"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            )
            conflicts = _run(
                worktree, "git", "diff", "--name-only", "--diff-filter=U", check=False
            ).splitlines()
            if merge.returncode != 0 and not conflicts:
                raise RuntimeError(f"merge failed for base branch {base_branch}: {merge.stderr.strip()}")
            yield worktree, conflicts
        finally:
            with lock:
                _run(repo, "git", "worktree", "remove", "--force", str(worktree), check=False)
                _drop_empty_local_branch(repo, branch, base_branch)
    finally:
        semaphore.release()


class IssueFixerRunState(TypedDict, total=False):
    issue: dict[str, Any]
    number: int
    run_id: str
    attempt: int
    branch: str
    claimed: bool
    skip: bool
    fixed: bool
    pr_url: str | None
    summary: str
    validation: str
    review_note: str


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
        identity = f"{issue.get('title', '')}\0{issue.get('body', '')}"
        reopen_event = issue.get("reopenEventId")
        branch = f"{self.config.branch_prefix}{number}"
        if reopen_event:
            identity += f"\0reopen={reopen_event}"
            # Never treat the old merged branch as an already-pushed new fix.
            branch += f"-reopen-{reopen_event}"
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:16]
        run_id = f"issue-fix-{number}-{fingerprint}"
        claim = self.state.claim_run(
            "issue-fixer",
            run_id,
            metadata={
                "issue": number,
                "label": self.config.label,
                "base_branch": self.config.base_branch,
                "branch": branch,
                "reopen_event_id": reopen_event,
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
        _log(f"issue-fixer issue={number} run_id={run_id} phase=claimed")
        return {
            "issue": issue,
            "run_id": run_id,
            "branch": branch,
            "claimed": True,
            "attempt": claim.record.attempt,
        }

    def _remediate(self, state: IssueFixerRunState) -> dict[str, Any]:
        if state.get("skip") or not state.get("claimed"):
            return {"fixed": False}
        number = state["number"]
        run_id = state["run_id"]
        issue = state["issue"]
        # Retries of a failed run must not re-announce themselves on every reconcile pass.
        if int(state.get("attempt", 1)) <= 1:
            _publish_issue_comment(
                self.project.repo,
                number,
                _issue_start_body(number, run_id, issue),
            )
        log_dir = (
            self.pr_config.log_dir
            or Path.home() / ".local/state/dev-agents" / self.project_name / "logs"
        ).expanduser()
        log_path = log_dir / f"issue-{number}-{run_id.rsplit('-', 1)[-1]}.log"
        _log(f"issue-fixer issue={number} run_id={run_id} phase=remediating log={log_path}")
        fixed = False
        summary = "Agent did not produce a clean pushed fix."
        validation = f"Agent log: {log_path}"
        review_note = ""
        try:
            with _isolated_issue_worktree(
                self.project.repo,
                (self.pr_config.worktree_dir or Path.home() / ".cache/dev-agents/pr-fixer").expanduser(),
                state["branch"],
                self.config.base_branch,
                max_concurrent=self.pr_config.max_concurrent_worktree_runs,
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
                    def prompt_for(plan: str | None = None, *, assess_scope: bool = False) -> str:
                        return _prompt(
                            worktree,
                            issue,
                            self.config.base_branch,
                            state["branch"],
                            conflicts,
                            kind=self.config.kind,
                            assess_scope=assess_scope,
                            plan=plan,
                        )

                    prompt = prompt_for()
                    providers = [self.config.provider]
                    if self.config.fallback_provider and self.config.fallback_provider not in providers:
                        providers.append(self.config.fallback_provider)
                    results: dict[str, AgentResult] = {}

                    def run_provider(provider: str) -> AgentResult:
                        if results:
                            # A fallback provider starts from the base branch, not
                            # from the failed provider's partial edits.
                            _reset_worktree_for_fallback(
                                worktree, self.config.base_branch, state["branch"]
                            )
                            _log(
                                f"issue-fixer issue={number} run_id={run_id} "
                                f"fallback-reset provider={provider}"
                            )
                        model = (
                            self.config.fallback_model
                            if provider == self.config.fallback_provider
                            else self.config.model or (
                                self.pr_config.model if provider == "codex" else None
                            )
                        )
                        effort = self.config.reasoning_effort or self.pr_config.reasoning_effort

                        def attempt(agent_prompt: str, **options: Any) -> AgentResult:
                            return run_agent(
                                provider,
                                agent_prompt,
                                cwd=worktree,
                                log_path=log_path,
                                timeout_seconds=self.config.timeout_minutes * 60,
                                heartbeat_seconds=self.pr_config.heartbeat_seconds,
                                reasoning_effort=options.pop("effort", effort),
                                model=model,
                                json_events=provider == "muse",
                                **options,
                            )

                        planning_effort = self.config.planning_effort
                        if planning_effort and provider == "codex":
                            def on_plan(plan: str, reason: str) -> None:
                                _log(f"issue-fixer issue={number} run_id={run_id} phase=planned")
                                _publish_issue_comment(
                                    self.project.repo,
                                    number,
                                    _plan_comment_body(number, run_id, reason, plan, planning_effort),
                                )

                            result = _implement_with_planning(
                                attempt,
                                prompt_for=lambda plan: prompt_for(plan, assess_scope=plan is None),
                                planning_prompt=_planning_prompt(issue, self.config.base_branch),
                                log_path=log_path,
                                plan_path=log_path.with_suffix(".plan.md"),
                                effort=effort,
                                planning_effort=planning_effort,
                                on_plan=on_plan,
                            )
                        else:
                            result = attempt(prompt)
                        results[provider] = result
                        return result

                    accepted = run_with_fallback(providers, run_provider)
                    used_provider, result = accepted or (providers[-1], results[providers[-1]])
                    if used_provider != self.config.provider:
                        _log(
                            f"issue-fixer issue={number} run_id={run_id} "
                            f"provider-fallback={self.config.provider}->{used_provider}"
                        )
                    def verify_pushed(agent_result: AgentResult) -> tuple[bool, str]:
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
                        pushed = bool(head and head != base_sha and head == remote)
                        ok = (
                            agent_result.returncode == 0
                            and not agent_result.timed_out
                            and clean
                            and pushed
                        )
                        return ok, (
                            f"exit={agent_result.returncode} timed_out={agent_result.timed_out} "
                            f"clean={clean} head_matches_remote={head == remote} "
                            f"head_ahead_of_base={head != base_sha}; log: {log_path}"
                        )

                    fixed, validation = verify_pushed(result)
                    if not fixed and result.timed_out:
                        summary = "Agent timed out before producing a verified pushed fix."
                    elif not fixed:
                        # Report the agent's own reason (a blocker, say) instead of the generic line.
                        summary = _reported_line(_last_agent_output(log_path), "SUMMARY") or summary
                    if used_provider != self.config.provider:
                        review_note = (
                            f"Written by fallback provider {used_provider} "
                            f"(primary {self.config.provider} did not produce an accepted run; "
                            "the worktree was reset to the base branch before the fallback started)."
                        )
                    if fixed and self.config.kind == "answer":
                        def report_round(
                            round_number: int,
                            score: float,
                            verdict: str,
                            blocking: int,
                            revising: bool,
                        ) -> None:
                            _publish_issue_comment(
                                self.project.repo,
                                number,
                                f"<!-- dev-agents:issue-fix issue={number} run={run_id} "
                                f"round={round_number} -->\n"
                                f"### 🤖 Pre-PR quality loop: round {round_number}\n\n"
                                f"Reviewer score {score}/10 "
                                f"(gate {self.config.pre_pr_score_threshold}/10), "
                                f"verdict `{verdict}`, {blocking} blocking finding(s). "
                                + (
                                    "Revising and re-reviewing."
                                    if revising
                                    else "Opening the PR."
                                ),
                            )

                        review_note, fixed = self._pre_pr_answer_review(
                            worktree=worktree,
                            issue=issue,
                            number=number,
                            run_id=run_id,
                            log_path=log_path,
                            base_sha=base_sha,
                            branch=state["branch"],
                            verify_pushed=verify_pushed,
                            initial_note=review_note,
                            provider=used_provider,
                            on_round=report_round,
                        )
                        if not fixed:
                            summary = "Pre-PR review revision did not produce a verified pushed fix."
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
                f"{_KIND_FRAMING.get(self.config.kind, _KIND_FRAMING['fix'])['title_prefix']}: "
                f"{str(issue.get('title', 'bug'))[:60]}",
                "--body",
                f"<!-- dev-agents:issue-fix issue={number} run={run_id} -->\n\nFixes #{number}.\n\nCreated by the dev-agents issue fixer; the normal PR review and validation pipeline will run."
                + (f"\n\n{review_note}" if review_note else ""),
            )
        except RuntimeError as error:
            return {"fixed": False, "summary": f"Fix was pushed but PR creation failed: {error}", "validation": f"log: {log_path}"}
        return {
            "fixed": True,
            "pr_url": pr_url,
            "summary": "Implemented and pushed the issue fix.",
            "validation": f"Agent log: {log_path}",
            "review_note": review_note,
        }

    def _pre_pr_answer_review(
        self,
        *,
        worktree: Path,
        issue: dict[str, Any],
        number: int,
        run_id: str,
        log_path: Path,
        base_sha: str,
        branch: str,
        verify_pushed: Callable[[AgentResult], tuple[bool, str]],
        initial_note: str,
        provider: str | None = None,
        on_round: Callable[[int, float, str, int, bool], None] | None = None,
    ) -> tuple[str, bool]:
        """Fresh-eyes review loop for an Answer draft before its PR exists.

        Returns ``(note_for_pr_body, still_fixed)``. The loop revises until
        the draft scores at least ``pre_pr_score_threshold`` with a clean
        verdict and no blocking findings, or ``pre_pr_max_revisions`` revisions have been spent; the
        cap keeps a nondeterministic reviewer from looping forever, and the
        outcome is always disclosed in the note. Each round is reported via
        ``on_round`` so the issue can show the loop is still working.
        Reviewer failure never blocks the article; the post-merge copy
        review remains the safety net.
        """
        notes = [initial_note] if initial_note else []
        settings = self.project.copy_review
        if not self.config.pre_pr_review or settings is None:
            return "\n\n".join(notes), True
        threshold = self.config.pre_pr_score_threshold
        max_revisions = self.config.pre_pr_max_revisions
        # Revisions go to the provider that wrote the draft, not necessarily the primary.
        provider = provider or self.config.provider
        review_log = log_path.with_name(f"{log_path.stem}-draft-review.log")
        try:
            changed = _run(
                worktree, "git", "diff", "--name-only", f"{base_sha}...HEAD", check=False
            ).splitlines()
            pages = [
                path
                for path in changed
                if any(fnmatch.fnmatchcase(path, glob) for glob in settings.page_globs)
            ]
        except Exception as error:  # noqa: BLE001 - disclosed, post-merge review remains
            _log(f"issue-fixer issue={number} run_id={run_id} pre-pr-review-failed error={error}")
            notes.append(
                f"Pre-PR editorial review unavailable ({error}); "
                "the post-merge copy review remains the safety net."
            )
            return "\n\n".join(notes), True
        if not pages:
            notes.append(
                "Pre-PR editorial review skipped: no changed Answer page matched "
                "the configured page globs."
            )
            return "\n\n".join(notes), True

        history: list[str] = []
        revisions = 0
        while True:
            round_number = revisions + 1
            try:
                assessment = review_answer_draft(
                    self.project,
                    settings,
                    worktree=worktree,
                    page_paths=pages,
                    issue=issue,
                    log_path=review_log,
                )
            except Exception as error:  # noqa: BLE001 - disclosed, post-merge review remains
                _log(
                    f"issue-fixer issue={number} run_id={run_id} "
                    f"pre-pr-review-failed round={round_number} error={error}"
                )
                notes.append(
                    f"Pre-PR editorial review unavailable in round {round_number} ({error}); "
                    "the post-merge copy review remains the safety net. "
                    f"Rounds so far: {'; '.join(history) or 'none'}."
                )
                return "\n\n".join(notes), True
            blockers = [
                finding for finding in assessment.findings if finding.severity == "blocking"
            ]
            history.append(
                f"round {round_number}: score {assessment.score}/10, "
                f"verdict {assessment.verdict}, {len(blockers)} blocking"
            )
            _log(
                f"issue-fixer issue={number} run_id={run_id} pre-pr-loop "
                f"round={round_number} score={assessment.score} verdict={assessment.verdict} "
                f"blocking={len(blockers)} threshold={threshold}"
            )
            passed = (
                assessment.score >= threshold
                and not blockers
                and assessment.verdict == "clean"
            )
            if on_round is not None:
                on_round(
                    round_number,
                    assessment.score,
                    assessment.verdict,
                    len(blockers),
                    not passed and revisions < max_revisions and bool(assessment.findings),
                )
            if passed:
                notes.append(
                    f"Pre-PR quality loop passed the score gate in round {round_number}: "
                    f"score {assessment.score}/10 (threshold {threshold}), clean verdict, "
                    "no blocking findings. "
                    f"Rounds: {'; '.join(history)}."
                )
                return "\n\n".join(notes), True
            if revisions >= max_revisions:
                remaining = "; ".join(
                    f"{finding.category} at {finding.location}" for finding in blockers
                )
                notes.append(
                    f"Pre-PR quality loop reached the revision cap ({max_revisions}) without "
                    f"clearing the score gate. Rounds: {'; '.join(history)}. "
                    + (
                        f"Remaining blocking findings for human review: {remaining}."
                        if blockers
                        else "No blocking findings remain; the score or verdict "
                        f"(`{assessment.verdict}`) is still short of the {threshold} gate, "
                        "with the reviewer's most important improvement: "
                        f"{assessment.most_important_improvement}."
                    )
                )
                return "\n\n".join(notes), True
            if not assessment.findings and not blockers:
                notes.append(
                    f"Pre-PR quality loop stopped in round {round_number}: score "
                    f"{assessment.score}/10 (verdict `{assessment.verdict}`) did not clear the "
                    f"{threshold} gate but the reviewer "
                    "gave no actionable findings to revise against. "
                    f"Rounds: {'; '.join(history)}."
                )
                return "\n\n".join(notes), True
            findings_text = "\n".join(
                f"- [{finding.severity}/{finding.category}] {finding.location}: "
                f"{finding.reader_impact} Proposed fix: {finding.proposed_fix} "
                f"Evidence: {finding.evidence}"
                for finding in assessment.findings
            )
            revision_prompt = (
                _prompt(
                    worktree, issue, self.config.base_branch, branch, [], kind=self.config.kind
                )
                + f"\n\nA fresh editorial review (round {round_number}) scored your pushed "
                f"draft {assessment.score}/10 (verdict {assessment.verdict}, "
                f"{len(blockers)} blocking finding(s)) against a gate of {threshold}/10 "
                "with a clean verdict. Most important improvement: "
                f"{assessment.most_important_improvement}\nRevise the article to resolve "
                "these findings, re-run the target repository's validation, then commit "
                "and push the same branch. State each corrected fact once; do not repeat "
                "a correction across sections, and preserve the article's strengths:\n"
                + findings_text
            )
            revision_model = (
                self.config.fallback_model
                if provider == self.config.fallback_provider
                and provider != self.config.provider
                else self.config.model
                or (self.pr_config.model if provider == "codex" else None)
            )
            result = run_agent(
                provider,
                revision_prompt,
                cwd=worktree,
                log_path=log_path,
                timeout_seconds=self.config.timeout_minutes * 60,
                heartbeat_seconds=self.pr_config.heartbeat_seconds,
                reasoning_effort=self.config.reasoning_effort or self.pr_config.reasoning_effort,
                model=revision_model,
                json_events=provider == "muse",
            )
            verified, _validation = verify_pushed(result)
            if not verified:
                notes.append(
                    f"Pre-PR quality loop: revision {revisions + 1} did not produce a "
                    f"verified pushed fix, so no PR was opened. Rounds: {'; '.join(history)}."
                )
                return "\n\n".join(notes), False
            revisions += 1

    def _auto_pause_if_exhausted(
        self, number: int, run_id: str, attempt: int, reason: str
    ) -> bool:
        """Apply the `paused` label after too many consecutive failures on one run_id.

        A failed run stays reclaimable, so a persistent failure would otherwise retry and
        re-comment on every reconcile pass forever (as the PR fixer's guard prevents).
        """
        if attempt < self.pr_config.max_consecutive_failures:
            return False
        repo = self.project.repo
        try:
            try:
                _run(repo, "gh", "issue", "edit", str(number), "--add-label", "paused")
            except RuntimeError:
                # The label may not exist in this repository yet.
                _run(
                    repo, "gh", "label", "create", "paused", "--color", "fbca04",
                    "--description", "Automation paused; remove to resume", check=False,
                )
                _run(repo, "gh", "issue", "edit", str(number), "--add-label", "paused")
        except RuntimeError as error:
            _log(f"issue-fixer auto-pause-label-failed issue={number} run_id={run_id} error={error}")
            return False
        _publish_issue_comment(
            repo, number, _issue_auto_pause_body(number, run_id, attempt, reason)
        )
        _log(f"issue-fixer auto-paused issue={number} run_id={run_id} attempt={attempt}")
        return True

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
        attempt = int(state.get("attempt", 1))
        if not state.get("fixed"):
            self._auto_pause_if_exhausted(number, run_id, attempt, summary)
        pr_number = _pr_number_from_url(pr_url) if pr_url else None
        if pr_number is not None:
            _publish_issue_comment(
                self.project.repo,
                pr_number,
                _pr_result_body(number, run_id, f"{summary}\n\n{validation}"),
            )
        elif state.get("fixed") or attempt <= 1:
            # A repeated failure is not re-posted: the first one explained it, and the
            # auto-pause comment covers the last.
            _publish_issue_comment(
                self.project.repo,
                number,
                _issue_result_body(number, run_id, pr_url, f"{summary}\n\n{validation}"),
            )
        _log(
            f"issue-fixer issue={number} run_id={run_id} phase=finalized "
            f"fixed={bool(state.get('fixed'))} pr_url={pr_url} summary={summary}"
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
            _log(f"issue-fixer reconcile label={self.config.label} open_issues={len(issues)}")
            for issue in issues:
                number = issue.get("number")
                if isinstance(number, int):
                    Thread(target=self._handle_reconciled, args=(number,), daemon=True).start()
            self.state.complete_run(
                "issue-reconcile",
                run_id,
                metadata={"label": self.config.label, "open_issues": len(issues)},
            )
        except Exception as error:  # noqa: BLE001 - a leaked "running" claim never
            # gets picked up again (nothing polls stale non-terminal reconcile rows),
            # and reconcile_loop below has no guard either, so anything narrower than
            # Exception here (e.g. a sqlite3.OperationalError under contention) used
            # to both strand this claim forever and kill the whole reconcile thread.
            _log(f"issue-fixer reconcile error={error}")
            self.state.complete_run("issue-reconcile", run_id, status="failed", error=str(error))

    def _handle_reconciled(self, number: int) -> None:
        try:
            self.handle(number)
        except Exception as error:  # noqa: BLE001 - one issue must not stop polling
            _log(f"issue-fixer issue={number} failed: {error}")

    def reconcile_loop(self) -> None:
        while True:
            time.sleep(max(30, self.pr_config.reconcile_interval_seconds))
            try:
                self.reconcile()
            except Exception as error:  # noqa: BLE001 - this loop must never die; a
                # silent thread death here means no PR/issue is ever reconciled again
                # for the rest of the process's life, with nothing visibly wrong.
                _log(f"issue-fixer reconcile_loop error={error}")
