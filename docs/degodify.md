# God-file decomposition workflow (`degodify`)

The `degodify` workflow identifies oversized files in target repositories and performs bounded, autonomous decompositions using configurable AI agents.

## Architecture

1. **Analysis & Candidate Selection**: When the target repository has `scripts/god-file-analysis.ts`,
   its own report ranks the files, so the repository decides which files may stay large (for
   Codex-Cryptica, the data catalogues exempted by Constitution Principle XIV). Otherwise a built-in
   scan ranks files exceeding thresholds (`CRITICAL` >= 1000 lines, `WATCH` >= 500 lines) and skips
   files that look like data catalogues. Files targeted by open PRs or active branches are skipped.
2. **Fallow evidence**: In repositories that use fallow, dependencies are installed in the isolated
   worktree and `fallow inspect --file <target>` supplies the file's exports and their importers,
   complex functions, duplicated blocks and dead code.
3. **Decomposition**: The agent splits the file into cohesive sibling modules until it is at most
   500 lines (or half its size, whichever is smaller), keeping the original path as a thin module so
   imports and behaviour are unchanged. It deletes the dead code, breaks up the complex functions,
   merges duplicates and adds focused tests.
4. **Gate**: A PR opens only when the target shrank by at least 25% and `fallow audit --base
   origin/<base>` does not fail. Otherwise the run fails with the reason and nothing is pushed.
5. **Publication**: Pushes `curator/degod-<slug>-<timestamp>` and opens a PR against `staging`
   with before/after line counts and the new modules.

Only one degodify PR is open at a time: while a `curator/degod-*` PR is open, a new run records
"waiting for open degodify PR" and does nothing, so nightly runs never stack refactors. Each run
gets `pr_fixer.degodify_timeout_minutes` (default 60) shared across provider attempts. It uses
`pr_fixer.degodify_model` (default `gpt-6.1-sol`) and the project's reasoning effort, including
when invoked from the CLI. An agent's zero exit code is accepted only when it leaves a clean
committed change. Each provider gets one
continuation in the same worktree to finish inspection-only or partial work, then the next
configured provider may try. Each attempt records its exit status and committed-progress result
as an `agent_attempt` event. Exhaustion records an explicit failure and opens no PR.
Degodify PRs go through the normal PR review
chain and auto-merge like any other PR when the review comes back clean.

`fallow fix` is not run: it only removes unused code repository-wide, which would widen the PR
beyond the target file. The target's own dead code comes from `fallow inspect` instead.

## CLI Usage

Inspect and run degodify on demand:

```bash
# Dry run (plan only)
uv run dev-agents degodify run codex-cryptica --config config/projects.yaml

# Execute decomposition and open PR
uv run dev-agents degodify run codex-cryptica --config config/projects.yaml --execute --provider muse
```

## GitHub Actions & Webhook Integration

The workflow integrates with scheduled GitHub Actions (e.g. `Nightly God File Analysis`):
1. GitHub Action runs `bun scripts/god-file-analysis.ts` to identify candidates.
2. Posts signed payload to `/degodify` on the `dev-agents` daemon.
3. The daemon validates HMAC (`DEGODIFY_WEBHOOK_SECRET`), claims the event once, and executes the decomposition with fallback across configured providers.

## Configuration

In `config/projects.yaml`:

```yaml
projects:
  codex-cryptica:
    repo: /path/to/repo
    github: owner/repo
    pr_fixer:
      base_branch: staging
      providers: [muse, claude, agy, codex]
      degodify_webhook_path: /degodify
      degodify_webhook_secret_env: DEGODIFY_WEBHOOK_SECRET
```

Degodify delivery claims share the PR-fixer SQLite database. Set `pr_fixer.state_path` to choose
the location; the default is `~/.local/state/dev-agents/<project>/pr-fixer-state.db`. Claims are
durable across daemon processes, so a repeated delivery or analysis run is rejected transactionally.
