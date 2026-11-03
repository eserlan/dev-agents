# Persona sweeps (`sweep`)

Scheduled, single-purpose improvement PRs. This replaces the Jules scheduled tasks that ran the
Bolt (performance), Palette (accessibility) and Binder (dependency injection) personas on
Codex-Cryptica. Curator's job, cutting oversized files down, belongs to [degodify](degodify.md).

## What lives where

The **target repository** owns each persona, as `<personas_dir>/<name>.md` (default
`.agents/sweeps/`). The file is read from the freshly fetched base branch, so a persona change
takes effect once it is merged:

```markdown
---
title: "⚡ Bolt"                      # PR title prefix
journal: .agents/journal/bolt.md     # lessons file the agent reads and appends to
max_files: 6                         # changed files, journal excluded
max_changed_lines: 150               # added + deleted lines, journal excluded
timeout_minutes: 60                  # optional: per-provider timeout for this persona
failing_workflow: daily-e2e.yml      # optional: run only when this workflow's latest run failed
---
What this persona looks for, what counts as worth a PR, and what to avoid.
```

**dev-agents** owns how a sweep runs. It is configured in `config/projects.yaml` under
`sweep:` (see the example config): providers, model, effort, timeout and the Claude fallback.

## One run

1. Load the persona from `origin/<base>`.
2. If one of the persona's PRs (head branch `<name>/…`) is still open, record "waiting" and stop.
   Personas never stack PRs.
3. With `failing_workflow`, check that workflow's latest run on the base. If it passed, was
   cancelled or is still running, record "nothing to fix" without starting an agent. If it failed,
   add the run link and its failure summary (Playwright totals and failed test titles, or the log
   tail) to the prompt. Vigil uses this to repair the daily E2E run.
4. Collect the titles of the persona's recent PRs, including the earlier Jules ones (matched by
   title prefix), and put them in the prompt so it does not redo them.
5. Branch `<name>/sweep-<timestamp>` from `origin/<base>` in a throwaway worktree. The base is
   never merged back in, which avoids the drifting Jules branches.
6. The agent makes one focused change and a single commit. The commit subject is the PR title
   and the commit body is the PR description. If nothing is worth changing, it writes a reason to
   a marker file instead. That is a successful no-op and opens no PR.
7. The diff must stay within `max_files` and `max_changed_lines`, journal excluded, or nothing
   is pushed.
8. Push and open the PR against the base (always `staging` by default, never `main`). After that
   the normal review, fix and auto-merge chain takes over.

Provider fallback, continuations and per-provider deadlines are shared with degodify
(`workflows/committing_agent.py`).

## Running

```bash
# Dry run: loads and validates the persona, prints the branch it would use
uv run dev-agents sweep run codex-cryptica --persona bolt

# Real run: record it in the PR-fixer state database and open a PR when there is one
uv run dev-agents sweep run codex-cryptica --persona bolt --execute
```

Executed runs are recorded as workflow `sweep` in the PR-fixer state database, so they appear in
the flow report. Exit status 1 means the run produced no PR because of a failure, an exhausted
provider or the size gate. Waiting and no-op runs exit 0.

Schedule them with the templated systemd units in `ops/systemd/dev-agents-sweep@.{service,timer}.example`,
one timer per persona.
