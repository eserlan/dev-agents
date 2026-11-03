"""Scheduled persona sweeps: one small, focused improvement PR per run.

A persona (for example Bolt for performance or Palette for accessibility) is a Markdown file in
the target repository, `<personas_dir>/<name>.md`, read from the freshly fetched base branch:

    ---
    title: "⚡ Bolt"          # PR title prefix
    journal: .agents/journal/bolt.md
    max_files: 6             # changed files, not counting the journal
    max_changed_lines: 150   # added + deleted lines, not counting the journal
    timeout_minutes: 60      # optional: overrides the configured per-provider timeout
    failing_workflow: daily-e2e.yml  # optional: only run when this workflow's latest run on
                                     # the base failed, and hand its failures to the agent
    ---
    <what the persona looks for and how it judges a change worth making>

Each run starts from a fresh branch off `origin/<base>`, never merges the base back in, and
opens its PR against the base. It does nothing while one of the persona's PRs is still open, and
"nothing worth changing" is a normal result that opens no PR.

When a finished change cannot be published, because it is over the persona's limits or the
repository's pre-push hook rejects it, the graph hands the problem back to the same agent to
amend its commit, at most `MAX_REPAIRS` times, instead of discarding the work.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml
from langgraph.graph import END, START, StateGraph

from dev_agents.runtime import GitHubError, gh_json
from dev_agents.workflows.committing_agent import (
    committed_change,
    install_dependencies,
    open_pull_request_with_prefix,
    run_committing_agent,
)
from dev_agents.workflows.committing_agent import gh as _gh
from dev_agents.workflows.committing_agent import git as _git

NOOP_MARKER = "dev-agents-sweep-noop"
MAX_REPAIRS = 2

REPAIR_PROMPT = """

The workflow could not publish your commit:

{problem}

Fix this in the same worktree, then amend your commit (`git commit --amend -F <file>`) so the
branch still has exactly one commit and its message still describes the change accurately.
Do not bypass git hooks. If it cannot be fixed within this run's rules, leave the commit
unchanged and explain why.
"""


class PersonaError(ValueError):
    """A persona file is missing or malformed."""


@dataclass(frozen=True)
class Persona:
    name: str
    title: str
    journal: str
    max_files: int
    max_changed_lines: int
    instructions: str
    timeout_minutes: int | None = None
    failing_workflow: str | None = None

    @property
    def branch_prefix(self) -> str:
        return f"{self.name}/"


@dataclass(frozen=True)
class SweepRunResult:
    persona: str
    branch: str | None
    pull_request_url: str | None
    dry_run: bool
    succeeded: bool
    reason: str | None = None
    noop: bool = False


class SweepState(TypedDict, total=False):
    repo: Path
    persona_name: str
    personas_dir: str
    base_branch: str
    providers: list[str]
    model: str | None
    reasoning_effort: str
    fallback_model: str | None
    fallback_reasoning_effort: str | None
    log_path: Path | None
    timeout_seconds: float
    recent_limit: int
    dry_run: bool
    on_event: Callable[[str, dict[str, Any]], None] | None
    persona: Persona
    # Set by `prepare` once mutation starts.
    branch: str
    worktree: Path
    noop_path: Path
    prompt: str
    # The provider whose change is being published, and how many agent turns it took.
    accepted: str
    attempts: int
    # A problem the agent is asked to repair (oversized change, rejected push), and the
    # result reason to report if the repair budget runs out.
    problem: str | None
    problem_reason: str | None
    repairs: int
    result: SweepRunResult


def parse_persona(name: str, text: str) -> Persona:
    """Parse a persona file: YAML front matter followed by the persona's instructions."""
    if not text.startswith("---"):
        raise PersonaError(f"persona {name!r} has no front matter")
    _, front, instructions = text.split("---", 2)
    try:
        meta = yaml.safe_load(front) or {}
        return Persona(
            name=name,
            title=str(meta["title"]).strip(),
            journal=str(meta.get("journal") or f".agents/journal/{name}.md"),
            max_files=int(meta.get("max_files", 6)),
            max_changed_lines=int(meta.get("max_changed_lines", 150)),
            instructions=instructions.strip(),
            timeout_minutes=int(meta["timeout_minutes"]) if meta.get("timeout_minutes") else None,
            failing_workflow=str(meta["failing_workflow"]) if meta.get("failing_workflow") else None,
        )
    except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise PersonaError(f"persona {name!r} has invalid front matter: {error}") from error


def load_persona(repo: Path, base_branch: str, personas_dir: str, name: str) -> Persona:
    """Read the persona as it is on the freshly fetched base, not the local checkout."""
    if not name.replace("-", "").isalnum():
        raise PersonaError(f"invalid persona name {name!r}")
    _git(repo, "fetch", "origin", base_branch)
    path = f"{personas_dir.rstrip('/')}/{name}.md"
    try:
        text = _git(repo, "show", f"origin/{base_branch}:{path}")
    except RuntimeError as error:
        raise PersonaError(f"persona file {path} not found on origin/{base_branch}") from error
    return parse_persona(name, text)


def recent_persona_pull_requests(repo: Path, persona: Persona, limit: int) -> list[str]:
    """Titles of the persona's recent PRs (open, merged or closed), newest first."""
    if limit <= 0:
        return []
    try:
        pulls = gh_json(
            repo, "pr", "list", "--state", "all", "--limit", "300",
            "--json", "title,headRefName,state",
        )
    except (GitHubError, TypeError):
        return []
    titles = [
        f"[{str(pull.get('state', '')).lower()}] {pull.get('title', '')}"
        for pull in (pulls if isinstance(pulls, list) else [])
        if str(pull.get("headRefName", "")).startswith(persona.branch_prefix)
        or str(pull.get("title", "")).startswith(persona.title)
    ]
    return titles[:limit]


_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t(?:\d{4}-\d\d-\d\dT\S+ )?")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TOTAL = re.compile(r"^ {2}(\d+) (failed|flaky|skipped|passed|did not run|interrupted)\b")
MAX_FAILURE_LINES = 60


def summarize_failed_log(log: str) -> str:
    """Pull the Playwright-style totals and failed test titles out of `gh run view --log-failed`.

    Falls back to the log's last lines when there is no recognisable summary.
    """
    lines = [_ANSI.sub("", _LOG_PREFIX.sub("", line)).rstrip() for line in log.splitlines()]
    summary: list[str] = []
    section: str | None = None
    for line in lines:
        if total := _TOTAL.match(line):
            section = total.group(2)
            summary.append(line.strip())
        elif section is None:
            continue
        elif line.startswith("    ") and line.strip():
            entry = f"  - {line.strip()}"
            if section == "failed" and entry not in summary and len(summary) < MAX_FAILURE_LINES:
                summary.append(entry)
        else:
            break  # the first summary block has ended
    if summary:
        return "\n".join(summary)
    return "\n".join(line for line in lines[-80:] if line.strip())


def failing_workflow_evidence(repo: Path, workflow: str, base_branch: str) -> tuple[str | None, str]:
    """Return (why to skip, evidence). Evidence describes the latest failed run on the base."""
    try:
        runs = gh_json(
            repo, "run", "list", "--workflow", workflow, "--branch", base_branch, "--limit", "1",
            "--json", "databaseId,status,conclusion,url,createdAt",
        )
    except (GitHubError, TypeError) as error:
        return f"could not read {workflow} runs: {error}", ""
    if not isinstance(runs, list) or not runs:
        return f"no {workflow} runs on {base_branch}", ""
    run = runs[0]
    if run.get("status") != "completed":
        return f"latest {workflow} run on {base_branch} has not finished", ""
    if run.get("conclusion") != "failure":
        return f"latest {workflow} run on {base_branch} concluded {run.get('conclusion')}", ""
    run_id = str(run.get("databaseId"))
    try:
        log = _gh(repo, "run", "view", run_id, "--log-failed", timeout=300)
    except RuntimeError as error:
        log = f"(could not download the failed log: {error})"
    return None, (
        f"The latest `{workflow}` run on `{base_branch}` failed: {run.get('url')} "
        f"(started {run.get('createdAt')}).\n"
        f"Full failure output: `gh run view {run_id} --log-failed`.\n\n"
        f"Summary of that run:\n{summarize_failed_log(log)}"
    )


def build_sweep_prompt(
    persona: Persona, branch: str, base_branch: str, recent: list[str], noop_path: Path, evidence: str = ""
) -> str:
    history = "\n".join(f"- {title}" for title in recent) or "- (none yet)"
    evidence_section = f"\n## Failing workflow\n\n{evidence}\n" if evidence else ""
    return f"""You are {persona.title}, running a scheduled improvement sweep on this repository.

CURRENT BRANCH: {branch} (branched from {base_branch})

{persona.instructions}
{evidence_section}
Before choosing a change:
- Read the repository's agent instructions (AGENTS.md / CLAUDE.md) and follow them.
- Read your journal at `{persona.journal}` (it may not exist yet) for earlier lessons.
- Do not repeat or overlap with these recent PRs of yours:
{history}

Rules for this run:
- Make exactly ONE focused improvement. At most {persona.max_files} changed files and
  {persona.max_changed_lines} changed lines, not counting the journal. Do not change behaviour
  beyond what the improvement requires.
- Run focused validation for what you changed (tests for the touched code; lint and format only
  the changed files). Run the type check when the repository instructions require it.
- If you learned something worth remembering, append a short dated entry to `{persona.journal}`
  (## YYYY-MM-DD - <title>, then **Learning:** and **Action:** lines).
- Make exactly one commit. Its subject is the PR title and must start with "{persona.title}: ".
  Its body is the PR description: what changed, why it matters, and how it was verified.
  Write the message to a file and commit with `git commit -F <file>` so the body keeps its
  line breaks; do not put `\\n` escapes in a `-m` argument.
- If you find nothing clearly worth changing, do not invent work: leave the tree unchanged, make
  no commit, and write a one-line reason to `{noop_path}`.
- Never bypass git hooks (`--no-verify`, `HUSKY=0`, or similar). The workflow pushes through the
  repository's pre-push hook, which lints every file you changed; if it fails, the run is
  discarded. Before committing, run the repository's changed-file lint and make it pass fully,
  including problems that already existed in the files you touched. If the commit-message hook
  rejects your message, fix the message rather than skipping the hook.
- Do not push, open, or merge a pull request; the workflow does that."""


def unescape_newlines(text: str) -> str:
    """Turn literal `\\n` escapes into line breaks, leaving `code spans` untouched.

    Agents sometimes pass a multi-line message to `git commit -m` with escaped newlines.
    """
    parts = text.split("`")
    return "`".join(part if index % 2 else part.replace("\\n", "\n") for index, part in enumerate(parts))


def sweep_gate(numstat: str, persona: Persona) -> str | None:
    """Return why a finished change should not become a PR, or None when it qualifies."""
    files = 0
    lines = 0
    for row in numstat.splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or parts[2] == persona.journal:
            continue
        files += 1
        # Binary files report "-"; count them as a file but not as lines.
        lines += sum(int(value) for value in parts[:2] if value.isdigit())
    if files == 0:
        return "the commit only touched the journal"
    if files > persona.max_files:
        return f"changed {files} files (limit {persona.max_files})"
    if lines > persona.max_changed_lines:
        return f"changed {lines} lines (limit {persona.max_changed_lines})"
    return None


def _push_rejection(error: RuntimeError) -> tuple[str, str]:
    """The agent-facing problem and the result reason for a rejected push."""
    # Usually the pre-push hook rejected the change; its last lines say why.
    output = "\n".join(str(error).splitlines()[-15:])
    return f"`git push` ran the repository's pre-push hook, which rejected it:\n\n{output}", f"push rejected: {output}"


def build_sweep_workflow() -> Any:
    """Build the scheduled persona-sweep graph.

    load_persona -> prepare -> run_agent -> check_change -> push_branch -> open_pull_request,
    with check_change and push_branch looping back to run_agent to repair a problem, and every
    finished outcome ending in publish_result. The worktree is removed by `run_sweep`.
    """
    graph = StateGraph(SweepState)

    def emit(state: SweepState, event: str, payload: dict[str, Any]) -> None:
        if (callback := state.get("on_event")) is not None:
            callback(event, payload)

    def finish(result: SweepRunResult) -> dict[str, object]:
        return {"result": result}

    def load(state: SweepState) -> dict[str, object]:
        persona = state.get("persona") or load_persona(
            state["repo"], state.get("base_branch", "staging"), state.get("personas_dir", ".agents/sweeps"),
            state["persona_name"],
        )
        emit(state, "load_persona", {"persona": persona.name, "title": persona.title})
        return {"persona": persona}

    def prepare(state: SweepState) -> dict[str, object]:
        repo, persona, branch = state["repo"], state["persona"], state["branch"]
        base = state.get("base_branch", "staging")
        if state.get("dry_run", True):
            return finish(SweepRunResult(persona.name, branch, None, True, True))
        if open_pull := open_pull_request_with_prefix(repo, persona.branch_prefix):
            # Not a failure: the next change waits until the previous one has merged or closed.
            return finish(SweepRunResult(
                persona.name, None, None, False, True, f"waiting for open {persona.name} PR {open_pull}"))
        evidence = ""
        if persona.failing_workflow:
            skip, evidence = failing_workflow_evidence(repo, persona.failing_workflow, base)
            if skip:
                # Nothing to repair: do not start an agent.
                return finish(SweepRunResult(
                    persona.name, None, None, False, True, f"nothing to fix: {skip}", noop=True))
        recent = recent_persona_pull_requests(repo, persona, state.get("recent_limit", 40))
        worktree = state["worktree"]
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "fetch", "origin", base)
        _git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
        noop_path = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-path", NOOP_MARKER))
        noop_path.unlink(missing_ok=True)
        # Validation (tests, type checks, generated tsconfigs) needs the locked dependencies.
        install_dependencies(worktree)
        emit(state, "prepare", {"branch": branch, "evidence": bool(evidence)})
        return {
            "noop_path": noop_path, "repairs": 0, "problem": None, "attempts": 0,
            "prompt": build_sweep_prompt(persona, branch, base, recent, noop_path, evidence),
        }

    def agent(state: SweepState) -> dict[str, object]:
        persona, worktree, noop_path = state["persona"], state["worktree"], state["noop_path"]
        base = state.get("base_branch", "staging")
        problem = state.get("problem")
        options: dict[str, Any] = {
            "task": f"{persona.name} improvement",
            "log_path": state.get("log_path") or state["repo"] / ".dev-agents" / "sweep.log",
            "timeout_seconds": (
                persona.timeout_minutes * 60 if persona.timeout_minutes else state.get("timeout_seconds", 30 * 60)
            ),
            "model": state.get("model"), "reasoning_effort": state.get("reasoning_effort", "high"),
            "fallback_model": state.get("fallback_model"),
            "fallback_reasoning_effort": state.get("fallback_reasoning_effort"),
            "after_reset": lambda: install_dependencies(worktree), "on_event": state.get("on_event"),
        }
        if problem is None:
            def finished() -> bool:
                return committed_change(worktree, base) or (
                    noop_path.is_file()
                    and _git(worktree, "rev-parse", "HEAD") == _git(worktree, "rev-parse", f"origin/{base}")
                )

            accepted, attempts = run_committing_agent(
                worktree, base, state["prompt"], providers=state.get("providers") or ["codex"],
                finished=finished, **options,
            )
            if accepted is None:
                return finish(SweepRunResult(
                    persona.name, state["branch"], None, False, False,
                    f"no usable result after {attempts} agent attempts; see agent_attempt events and log"))
            return {"accepted": accepted, "attempts": attempts}

        # A repair stays with the provider that made the change: a fallback would reset the
        # worktree to the base and discard it.
        before = _git(worktree, "rev-parse", "HEAD")

        def repaired() -> bool:
            return committed_change(worktree, base) and _git(worktree, "rev-parse", "HEAD") != before

        accepted, attempts = run_committing_agent(
            worktree, base, state["prompt"] + REPAIR_PROMPT.format(problem=problem),
            providers=[state["accepted"]], finished=repaired, **options,
        )
        total = state.get("attempts", 0) + attempts
        emit(state, "repair", {"repair": state.get("repairs", 0), "amended": accepted is not None})
        if accepted is None:
            return finish(SweepRunResult(
                persona.name, state["branch"], None, False, False,
                f"{state.get('problem_reason')} (not repaired after {state.get('repairs', 0)} attempt(s))"))
        return {"attempts": total, "problem": None}

    def needs_repair(state: SweepState, problem: str, reason: str) -> dict[str, object]:
        repairs = state.get("repairs", 0)
        if repairs >= MAX_REPAIRS:
            suffix = f" (still failing after {repairs} repair(s))" if repairs else ""
            return finish(SweepRunResult(state["persona"].name, state["branch"], None, False, False, reason + suffix))
        return {"problem": problem, "problem_reason": reason, "repairs": repairs + 1}

    def check(state: SweepState) -> dict[str, object]:
        persona, worktree = state["persona"], state["worktree"]
        base = state.get("base_branch", "staging")
        if not committed_change(worktree, base):
            why = state["noop_path"].read_text(encoding="utf-8", errors="replace").strip() or "no reason given"
            return finish(SweepRunResult(persona.name, None, None, False, True, f"nothing worth changing: {why}", noop=True))
        reason = sweep_gate(_git(worktree, "diff", "--numstat", f"origin/{base}...HEAD"), persona)
        emit(state, "check_change", {"gate": reason})
        if reason:
            return needs_repair(state, (
                f"The change is over this persona's limits: {reason}. Keep only its single most valuable "
                "part and drop the rest."), reason)
        return {"problem": None}

    def push(state: SweepState) -> dict[str, object]:
        try:
            _git(state["worktree"], "push", "-u", "origin", state["branch"], timeout=900)
        except RuntimeError as error:
            problem, reason = _push_rejection(error)
            emit(state, "push_branch", {"rejected": True})
            return needs_repair(state, problem, reason)
        emit(state, "push_branch", {"rejected": False})
        return {"problem": None}

    def open_pr(state: SweepState) -> dict[str, object]:
        persona, worktree = state["persona"], state["worktree"]
        base = state.get("base_branch", "staging")
        first = _git(worktree, "rev-list", "--reverse", f"origin/{base}..HEAD").splitlines()[0]
        title = _git(worktree, "log", "-1", "--format=%s", first)
        if not title.startswith(persona.title):
            title = f"{persona.title}: {title}"
        body = unescape_newlines(_git(worktree, "log", "-1", "--format=%b", first))
        body = f"{body}\n\n---\n*Scheduled `{persona.name}` sweep by dev-agents · agent `{state['accepted']}`*".lstrip()
        pr_url = _gh(state["repo"], "pr", "create", "--base", base, "--head", state["branch"],
                     "--title", title, "--body", body)
        return finish(SweepRunResult(persona.name, state["branch"], pr_url, False, True))

    def publish(state: SweepState) -> dict[str, object]:
        result = state["result"]
        emit(state, "publish_result", {
            "succeeded": result.succeeded, "noop": result.noop,
            "pull_request": result.pull_request_url, "reason": result.reason, "repairs": state.get("repairs", 0),
        })
        return {"result": result}

    def route(next_node: str, *, repair: bool = False) -> Callable[[SweepState], str]:
        def choose(state: SweepState) -> str:
            if "result" in state:
                return "publish_result"
            if repair and state.get("problem"):
                return "run_agent"
            return next_node
        return choose

    graph.add_node("load_persona", load)
    graph.add_node("prepare", prepare)
    graph.add_node("run_agent", agent)
    graph.add_node("check_change", check)
    graph.add_node("push_branch", push)
    graph.add_node("open_pull_request", open_pr)
    graph.add_node("publish_result", publish)
    graph.add_edge(START, "load_persona")
    graph.add_edge("load_persona", "prepare")
    graph.add_conditional_edges("prepare", route("run_agent"), ["run_agent", "publish_result"])
    graph.add_conditional_edges("run_agent", route("check_change"), ["check_change", "publish_result"])
    graph.add_conditional_edges(
        "check_change", route("push_branch", repair=True), ["push_branch", "run_agent", "publish_result"])
    graph.add_conditional_edges(
        "push_branch", route("open_pull_request", repair=True), ["open_pull_request", "run_agent", "publish_result"])
    graph.add_edge("open_pull_request", "publish_result")
    graph.add_edge("publish_result", END)
    return graph.compile()


def _run_sweep_impl(
    repo: Path,
    persona: Persona | None = None,
    *,
    persona_name: str | None = None,
    personas_dir: str = ".agents/sweeps",
    base_branch: str = "staging",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 30 * 60,
    model: str | None = None,
    reasoning_effort: str = "high",
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
    recent_limit: int = 40,
    dry_run: bool = True,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> SweepRunResult:
    """Invoke the sweep graph, then remove its worktree and unpushed branch whatever happened."""
    name = persona.name if persona else persona_name
    if not name:
        raise PersonaError("a persona or persona name is required")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    branch = f"{name}/sweep-{stamp}"
    worktree = repo / ".dev-agents-worktrees" / f"sweep-{name}-{stamp}"
    state: dict[str, Any] = {
        "repo": repo, "persona_name": name, "personas_dir": personas_dir, "base_branch": base_branch,
        "providers": providers, "log_path": log_path, "timeout_seconds": timeout_seconds, "model": model,
        "reasoning_effort": reasoning_effort, "fallback_model": fallback_model,
        "fallback_reasoning_effort": fallback_reasoning_effort, "recent_limit": recent_limit,
        "dry_run": dry_run, "on_event": on_event, "branch": branch, "worktree": worktree,
    }
    if persona is not None:
        state["persona"] = persona
    try:
        # Each repair is three more steps (run_agent, check_change, push_branch).
        final = build_sweep_workflow().invoke(state, {"recursion_limit": 25 + 3 * MAX_REPAIRS})
    finally:
        if worktree.exists():
            _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            # Pushed work lives on the remote; a run that pushed nothing leaves nothing behind.
            _git(repo, "branch", "-D", branch, check=False)
    return cast(SweepRunResult, final["result"])


def run_sweep(
    repo: Path,
    persona_name: str,
    *,
    personas_dir: str = ".agents/sweeps",
    base_branch: str = "staging",
    providers: list[str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float = 30 * 60,
    model: str | None = None,
    reasoning_effort: str = "high",
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
    recent_limit: int = 40,
    dry_run: bool = True,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> SweepRunResult:
    """Run one persona sweep through its LangGraph workflow, with mutation disabled by default."""
    return _run_sweep_impl(
        repo, persona_name=persona_name, personas_dir=personas_dir, base_branch=base_branch,
        providers=providers, log_path=log_path, timeout_seconds=timeout_seconds, model=model,
        reasoning_effort=reasoning_effort, fallback_model=fallback_model,
        fallback_reasoning_effort=fallback_reasoning_effort, recent_limit=recent_limit,
        dry_run=dry_run, on_event=on_event,
    )
