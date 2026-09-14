# God-file decomposition workflow (`degodify`)

The `degodify` workflow identifies oversized files in target repositories and performs bounded, autonomous decompositions using configurable AI agents.

## Architecture

1. **Analysis & Candidate Selection**: Scans source files, ranks files exceeding thresholds (`CRITICAL` >= 1000 lines, `WATCH` >= 500 lines), filters out data catalogs, and excludes candidates targeting active branches or open PRs.
2. **Decomposition Execution**: Creates an isolated git worktree on the target base branch (`staging`), runs the configured agent provider (`muse`, `claude`, `agy`, or `codex`), and verifies that changes are clean and committed.
3. **Publication**: Pushes `curator/degod-<slug>-<timestamp>` and creates a Pull Request targeting `staging`.

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
