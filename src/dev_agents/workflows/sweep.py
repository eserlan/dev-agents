"""Scheduled persona sweeps: one small, focused improvement PR per run.

A persona (for example Bolt for performance or Palette for accessibility) is a Markdown file in
the target repository, `<personas_dir>/<name>.md`, read from the freshly fetched base branch:

    ---
    title: "⚡ Bolt"          # PR title prefix
    journal: .agents/journal/bolt.md
    max_files: 6             # changed files, not counting the journal
    max_changed_lines: 150   # added + deleted lines, not counting the journal
    ---
    <what the persona looks for and how it judges a change worth making>

Each run starts from a fresh branch off `origin/<base>`, never merges the base back in, and
opens its PR against the base. It does nothing while one of the persona's PRs is still open, and
"nothing worth changing" is a normal result that opens no PR.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml
from langgraph.graph import END, START, StateGraph

from dev_agents.runtime import GitHubError, gh_json
from dev_agents.workflows.committing_agent import (
    branch_worktree,
    committed_change,
    install_dependencies,
    open_pull_request_with_prefix,
    run_committing_agent,
)
from dev_agents.workflows.committing_agent import gh as _gh
from dev_agents.workflows.committing_agent import git as _git

NOOP_MARKER = "dev-agents-sweep-noop"


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


def build_sweep_prompt(persona: Persona, branch: str, base_branch: str, recent: list[str], noop_path: Path) -> str:
    history = "\n".join(f"- {title}" for title in recent) or "- (none yet)"
    return f"""You are {persona.title}, running a scheduled improvement sweep on this repository.

CURRENT BRANCH: {branch} (branched from {base_branch})

{persona.instructions}

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


def _run_sweep_impl(
    repo: Path,
    persona: Persona,
    *,
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
    """Run one persona sweep, with mutation disabled by default."""
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    branch = f"{persona.branch_prefix}sweep-{stamp}"
    if dry_run:
        return SweepRunResult(persona.name, branch, None, True, True)
    if open_pull := open_pull_request_with_prefix(repo, persona.branch_prefix):
        # Not a failure: the next change waits until the previous one has merged or closed.
        return SweepRunResult(persona.name, None, None, False, True, f"waiting for open {persona.name} PR {open_pull}")

    recent = recent_persona_pull_requests(repo, persona, recent_limit)
    with branch_worktree(repo, base_branch, branch, prefix=f"sweep-{persona.name}-") as worktree:
        noop_path = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-path", NOOP_MARKER))
        noop_path.unlink(missing_ok=True)
        # Validation (tests, type checks, generated tsconfigs) needs the locked dependencies.
        install_dependencies(worktree)

        def finished() -> bool:
            return committed_change(worktree, base_branch) or (
                noop_path.is_file()
                and _git(worktree, "rev-parse", "HEAD") == _git(worktree, "rev-parse", f"origin/{base_branch}")
            )

        accepted, attempts = run_committing_agent(
            worktree, base_branch, build_sweep_prompt(persona, branch, base_branch, recent, noop_path),
            providers=providers or ["codex"], task=f"{persona.name} improvement",
            log_path=log_path or repo / ".dev-agents" / "sweep.log",
            timeout_seconds=timeout_seconds, model=model, reasoning_effort=reasoning_effort,
            fallback_model=fallback_model, fallback_reasoning_effort=fallback_reasoning_effort,
            finished=finished, after_reset=lambda: install_dependencies(worktree), on_event=on_event,
        )
        if accepted is None:
            return SweepRunResult(persona.name, branch, None, False, False,
                                  f"no usable result after {attempts} agent attempts; see agent_attempt events and log")
        if not committed_change(worktree, base_branch):
            why = noop_path.read_text(encoding="utf-8", errors="replace").strip() or "no reason given"
            return SweepRunResult(persona.name, None, None, False, True, f"nothing worth changing: {why}", noop=True)
        reason = sweep_gate(_git(worktree, "diff", "--numstat", f"origin/{base_branch}...HEAD"), persona)
        if reason:
            return SweepRunResult(persona.name, branch, None, False, False, reason)
        first = _git(worktree, "rev-list", "--reverse", f"origin/{base_branch}..HEAD").splitlines()[0]
        title = _git(worktree, "log", "-1", "--format=%s", first)
        if not title.startswith(persona.title):
            title = f"{persona.title}: {title}"
        body = _git(worktree, "log", "-1", "--format=%b", first)
        body = unescape_newlines(body)
        body = f"{body}\n\n---\n*Scheduled `{persona.name}` sweep by dev-agents · agent `{accepted}`*".lstrip()
        _git(worktree, "push", "-u", "origin", branch)
        pr_url = _gh(repo, "pr", "create", "--base", base_branch, "--head", branch, "--title", title, "--body", body)
        return SweepRunResult(persona.name, branch, pr_url, False, True)


def build_sweep_workflow() -> Any:
    """Build the scheduled persona-sweep graph."""
    graph = StateGraph(SweepState)

    def emit(state: SweepState, event: str, payload: dict[str, Any]) -> None:
        if (callback := state.get("on_event")) is not None:
            callback(event, payload)

    def load(state: SweepState) -> dict[str, object]:
        persona = load_persona(
            state["repo"], state.get("base_branch", "staging"), state.get("personas_dir", ".agents/sweeps"),
            state["persona_name"],
        )
        emit(state, "load_persona", {"persona": persona.name, "title": persona.title})
        return {"persona": persona}

    def execute(state: SweepState) -> dict[str, object]:
        result = _run_sweep_impl(
            state["repo"], state["persona"],
            base_branch=state.get("base_branch", "staging"),
            providers=state.get("providers"),
            log_path=state.get("log_path"),
            timeout_seconds=state.get("timeout_seconds", 30 * 60),
            model=state.get("model"), reasoning_effort=state.get("reasoning_effort", "high"),
            fallback_model=state.get("fallback_model"),
            fallback_reasoning_effort=state.get("fallback_reasoning_effort"),
            recent_limit=state.get("recent_limit", 40),
            dry_run=state.get("dry_run", True),
            on_event=state.get("on_event"),
        )
        emit(state, "execute_sweep", {"succeeded": result.succeeded, "noop": result.noop})
        return {"result": result}

    def publish(state: SweepState) -> dict[str, object]:
        result = state["result"]
        emit(state, "publish_result", {"pull_request": result.pull_request_url, "reason": result.reason})
        return {"result": result}

    graph.add_node("load_persona", load)
    graph.add_node("execute_sweep", execute)
    graph.add_node("publish_result", publish)
    graph.add_edge(START, "load_persona")
    graph.add_edge("load_persona", "execute_sweep")
    graph.add_edge("execute_sweep", "publish_result")
    graph.add_edge("publish_result", END)
    return graph.compile()


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
    """Run one persona sweep through its LangGraph workflow."""
    result = build_sweep_workflow().invoke({
        "repo": repo, "persona_name": persona_name, "personas_dir": personas_dir,
        "base_branch": base_branch, "providers": providers, "log_path": log_path,
        "timeout_seconds": timeout_seconds, "model": model, "reasoning_effort": reasoning_effort,
        "fallback_model": fallback_model, "fallback_reasoning_effort": fallback_reasoning_effort,
        "recent_limit": recent_limit, "dry_run": dry_run, "on_event": on_event,
    })
    return cast(SweepRunResult, result["result"])
