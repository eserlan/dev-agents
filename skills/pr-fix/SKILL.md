---
name: pr-fix
description: Shared workflow for safely resolving pull-request feedback.
---

# Pull-request fixer workflow

Use this workflow for review comments, requested changes, failed checks, and merge conflicts.

1. Inspect the PR metadata, base/head branches, review threads, reviews, and complete check state.
2. Only process configured base branches. Never close or merge a PR as part of the agent fix pass.
3. Work in the isolated worktree supplied by the orchestrator. Do not modify the source checkout.
4. Fetch and merge the configured base branch before editing. Resolve all unmerged paths and commit
   the merge before validation.
5. Read the target repository's `AGENTS.md` and any repository-local PR-fix skill. Those rules are
   authoritative for architecture, commands, terminology, and product behavior.
6. Address every actionable comment, failed check, and conflict. Inspect bounded failed-check logs
   rather than guessing from a check name.
7. Run focused tests for affected files and targeted lint/format checks. Run the repository type
   check when its instructions require it; never claim success for a check that did not complete.
8. Commit only the intended changes with the repository's required commit convention and push the
   PR branch. Verify the remote head changed or the feedback is observably resolved.
9. Report the PR number, addressed feedback, validation results, push status, and any unresolved
   blockers. Auto-merge is a separate guarded orchestrator action.

Safety requirements:

- Never use `--no-verify` to hide a failing quality gate unless the repository explicitly requires it.
- Never mark feedback handled after an unsuccessful, timed-out, unpushed, or conflicted agent run.
- Preserve unrelated work and never reset or delete files outside the supplied worktree.
