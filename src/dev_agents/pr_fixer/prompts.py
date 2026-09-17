"""Agent prompt construction for the PR-fix and internal review passes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import dev_agents.pr_fixer as _pkg
from dev_agents.context.instructions import discover_instructions

from ._shared import (
    REVIEW_REPORT_BEGIN,
    REVIEW_REPORT_END,
    REVIEW_SKILL_CANDIDATES,
    SHARED_PR_FIX_SKILL,
)


def _review_skill(repo: Path) -> tuple[str, str]:
    """Load the canonical review skill, with compatibility fallbacks for older repos."""
    for relative_path in REVIEW_SKILL_CANDIDATES:
        path = repo / relative_path
        if path.is_file():
            return relative_path, path.read_text(encoding="utf-8")
    return REVIEW_SKILL_CANDIDATES[0], ""


def _target_validation_instructions(repo: Path, base_branch: str) -> str:
    """Describe the target repository's optimized validation path when it provides one."""
    scripts = {
        name: repo / "scripts" / name
        for name in ("affected-workspaces.mjs", "lint-changed.mjs", "test-changed.mjs")
    }
    if all(path.is_file() for path in scripts.values()):
        return f"""This repository provides dependency-aware affected validation. Follow the same
decision path as `.github/workflows/deploy.yml`; do not substitute repository-wide checks for it.
First resolve the merge base and inspect the selected scope:

BASE_SHA=$(git merge-base HEAD origin/{base_branch} 2>/dev/null || git merge-base HEAD {base_branch})
bun scripts/affected-workspaces.mjs --base \"$BASE_SHA\" --head HEAD

For an ordinary PR scope, run the exact changed-file validators in parallel after dependencies are
installed:
- `bun scripts/lint-changed.mjs --base \"$BASE_SHA\" --head HEAD`
- `bun scripts/test-changed.mjs --base \"$BASE_SHA\" --head HEAD`
- the selected workspace `lint:types` commands from the workflow, when applicable

If the selector reports full validation (for example because package.json, bun.lock, scripts/,
`.github/`, or shared configuration changed), follow the workflow's widened affected-workspace
commands instead. Do not run `bun run lint` or a repository-wide test command for an ordinary
affected-only PR."""
    return """Run focused tests first. For linting, run ESLint/format checks only against changed files;
do not run repository-wide lint or test commands unless the target repository's instructions
require them or no targeted command is available. Independent lint, test, and type-check commands
may run in parallel after dependencies are installed."""


def _prompt(
    repo: Path, number: int, keys: list[str], base_branch: str, conflicts: list[str]
) -> str:
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(
        f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions
    )
    shared_skill = (
        SHARED_PR_FIX_SKILL.read_text(encoding="utf-8") if SHARED_PR_FIX_SKILL.is_file() else ""
    )
    target_skill = repo / ".codex/skills/pr-fix/SKILL.md"
    target_skill_text = target_skill.read_text(encoding="utf-8") if target_skill.is_file() else ""
    changed_files = _pkg._run(
        repo, "git", "diff", "--name-only", f"origin/{base_branch}...HEAD", check=False
    )
    changed = [path for path in changed_files.splitlines() if path and (repo / path).exists()]
    changed_text = " ".join(changed) if changed else "<no changed files detected>"
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    validation_instructions = _target_validation_instructions(repo, base_branch)
    return f"""Fix actionable GitHub feedback for PR #{number} in this isolated worktree.
Shared dev-agents PR-fix workflow:
{shared_skill}

Target-repository PR-fix workflow (if present):
{target_skill_text}

Read and obey these repository instructions:
{instruction_text}

Actionable feedback identities:
{chr(10).join("- " + key for key in keys)}

Changed files (use these paths for targeted linting):
{changed_text}

Merge-conflict paths (resolve, stage, commit, and push them before testing):
{conflict_text}

Validation protocol:
{validation_instructions}

Commit and push HEAD to the PR branch.
Do not close or merge the PR.

At the end, print this exact plain-text block to stdout (no markdown fence), with one concise line
for each field. REPORT_JSON must be valid compact JSON matching the supplied skill's schema:
{REVIEW_REPORT_BEGIN}
FINDINGS: <what the supplied feedback identified>
FIXES: <what changed, or none>
REPORT_JSON: {{"verdict":"clean|findings","findings":[],"categories_checked":[],"validation":[],"fixes":[]}}
{REVIEW_REPORT_END}"""


def _review_prompt(
    repo: Path,
    number: int,
    meta: dict[str, Any],
    base_branch: str,
    conflicts: list[str],
    run_id: str | None = None,
    review_round: int = 0,
    parent_sha: str | None = None,
    prior_report: dict[str, Any] | None = None,
) -> str:
    """Build either the initial or bounded post-fix review prompt."""
    instructions = discover_instructions(repo).documents
    instruction_text = "\n\n".join(
        f"## {item.path.relative_to(repo)}\n{item.content}" for item in instructions
    )
    skill_relative_path, skill_text = _review_skill(repo)
    conflict_text = "\n".join(f"- {path}" for path in conflicts) or "None"
    validation_instructions = _target_validation_instructions(repo, base_branch)
    head_sha = meta.get("headRefOid", "unknown")
    report_id = run_id or "the run ID supplied by the daemon"
    if review_round == 0:
        review_instructions = f"""The PR is green and GitHub has no submitted Copilot review. Run both independent passes:

1. GENERAL DEFECT REVIEW: inspect the actual diff against origin/{base_branch}, surrounding call
sites, security/privacy boundaries, async behavior, tests, and likely regressions. Report only
concrete defects introduced by this PR; do not invent style nits or speculative concerns.

2. CODEX-CRYPTICA REVIEW: read and apply the repository's `{skill_relative_path}`
below, including its linked guidance. Check Svelte 5/TypeScript behavior, worker safety, AI
parsing, privacy, accessibility, performance, tests, and bounded responsibility."""
        pass_description = "the two-pass review"
        pass_names = "[\"general\", \"codex-review\"]"
        fix_condition = "If either pass finds a concrete defect"
        no_fix_condition = "If both passes find no actionable defect"
    else:
        previous_report = json.dumps(prior_report or {}, sort_keys=True)
        review_instructions = f"""This is the one allowed targeted post-fix verification after an earlier Luna review.
The prior review head was `{parent_sha or 'unknown'}`. Inspect the diff from that head to the
current head, the files changed by the fix, and every finding in the prior structured report:
{previous_report}

Verify that the original findings are actually resolved and that the fix did not introduce a
new concrete defect. Do not repeat the full general and repository-specific review checklists;
keep this pass limited to the post-fix diff and the prior findings."""
        pass_description = "the targeted post-fix verification"
        pass_names = "[\"targeted-post-fix\"]"
        fix_condition = "If the targeted verification finds a concrete unresolved defect"
        no_fix_condition = "If the targeted verification finds no actionable defect"
    return f"""Perform {pass_description} and, if needed, fix Pull Request #{number}.

PR head: {head_sha}
PR branch: {meta.get("headRefName", "unknown")} (based on {base_branch})
PR URL: {meta.get("url", "")}

{review_instructions}

Review pass identifiers to use in the run report: {pass_names}

Repository instructions:
{instruction_text}

Codex review skill:
{skill_text}

Merge-conflict paths:
{conflict_text}

Validation protocol:
{validation_instructions}

Review communication protocol:
- The daemon owns one evolving lifecycle comment for this run, marked
  `<!-- dev-agents:pr-review run={report_id} -->`.
- Do not create additional PR comments for findings or fixes. Before editing, use `gh api` to PATCH
  the existing comment containing that marker with a concise findings update, then PATCH it again
  immediately before edits begin when concrete fixes are needed. Never use `gh pr comment` for
  review progress. The daemon will make the final update with the structured findings, what changed,
  commit, files, and validation. Keep the full detail in the required report block and persisted
  run timeline.
- At the end, print this exact plain-text block to stdout (no markdown fence), with one concise line
  for each field. Use `none` when appropriate:
  {REVIEW_REPORT_BEGIN}
  FINDINGS: <what {pass_description} found>
  FIXES: <what changed, or none>
  REPORT_JSON: {{"verdict":"clean|findings","findings":[],"categories_checked":[],"validation":[],"fixes":[]}}
  {REVIEW_REPORT_END}
- Do not include credentials, tokens, or private user data in comments or the report.

{no_fix_condition}, make no code changes. If the worktree contains a
pre-merge base commit, push that commit; otherwise leave the original head unchanged. In either
case finish successfully only when `git status --porcelain` is clean and the remote branch is at
the reviewed head.

{fix_condition}, make the smallest correct fix, add focused tests, run the
targeted validation required by the repository instructions, commit, and push HEAD to the PR
branch. Never close or merge the PR. Do not use a failing test bypass or hide unresolved merge
conflicts."""
