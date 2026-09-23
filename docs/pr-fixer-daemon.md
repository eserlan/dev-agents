# PR fixer daemon

The daemon is designed to be a drop-in replacement for the existing `remotecc` PR-fixer
listener. It runs the Python listener under systemd and keeps Cloudflare Tunnel as a separate,
dependent service. Secrets and tunnel credentials stay outside this repository.

PR reviews, PR fixes, and issue-fixer queues (including `gui-fix`) are hard-pinned to Codex
using the `gpt-6-luna` model. The PR-fixer
`providers` setting is retained for configuration compatibility, but it cannot enable fallback
to another agent; `reasoning_effort` controls the Codex reasoning level and defaults to `high`.

```bash
mkdir -p ~/.config/dev-agents ~/.config/systemd/user
cp config/projects.example.yaml ~/.config/dev-agents/projects.yaml
cp ops/systemd/dev-agents-pr-fixer.service.example ~/.config/systemd/user/dev-agents.service
cp ops/systemd/dev-agents-pr-fixer-tunnel.service.example ~/.config/systemd/user/dev-agents-tunnel.service
# Reuse the existing remotecc secrets and tunnel config:
#   ~/.config/codex-pr-review/webhook.env
#   ~/.cloudflared/codex-pr-review.yml
systemctl --user daemon-reload
systemctl --user disable --now codex-pr-review-webhook.service codex-pr-review-tunnel.service || true
systemctl --user enable --now dev-agents.service dev-agents-tunnel.service
```

The GitHub webhook remains `https://pr-webhook.codexcryptica.com/github`; configure its secret to
match `GITHUB_WEBHOOK_SECRET`. Check both services with `systemctl --user status`, inspect logs with
`journalctl --user -u dev-agents.service -f`, and verify the local listener with
`curl http://127.0.0.1:8788/health`.

The new services reuse `~/.config/codex-pr-review/webhook.env` and
`~/.cloudflared/codex-pr-review.yml`; do not copy those files, their credential JSON, or tunnel
IDs into Git.

Completed run logs are retained for 30 days by default. Stale temporary worktree directories older
than two days are removed at daemon startup; active/recent worktrees are left alone. Adjust
`log_retention_days` and `worktree_retention_days` in the project configuration if needed.

When a target repository provides `scripts/affected-workspaces.mjs`, `scripts/lint-changed.mjs`,
and `scripts/test-changed.mjs`, remediation and review prompts require Luna to follow that
repository's CI scope selection. Ordinary PRs run the changed-file lint/test validators and the
independent type-check commands in parallel after dependencies are installed. Changes to shared
configuration, scripts, or dependency manifests follow the workflow's widened affected
workspace/full-validation path. Repositories without those helpers use focused changed-file
validation as directed by their own instructions.

## State database

Workflow state is stored in the configured `pr_fixer.state_path` SQLite database. If it is omitted,
the default is `~/.local/state/dev-agents/<project>/pr-fixer-state.db`. SQLite WAL mode and a
30-second busy timeout allow webhook and reconciliation workers to share the database safely.

The first PR-fixer run imports the previous `pr-fixer-state.json` file when it exists. The legacy file is
left in place as a rollback copy; new writes use SQLite. A configured path ending in `.json` is
accepted for backwards compatibility and is converted to the corresponding `.db` path.

If the database is missing, it is created with the current schema. If SQLite reports corruption,
the database and any WAL sidecars are renamed with a `.corrupt-<timestamp>` suffix and a fresh
database is created. The quarantined copy is retained for manual recovery. Agent output remains in
the configured log files, not in SQLite.

## Auto-merge

With `auto_merge: true`, each handled pull-request event and reconciliation pass independently
refreshes merge readiness after feedback processing. Auto-merge is deferred until GitHub reports at
least one check, no pending or failing checks, a clean mergeable PR, and no actionable feedback.

For a project that should auto-merge only daemon-created issue fixes, set both
`auto_merge: true` and `auto_merge_issue_fixes_only: true`. The daemon then requires the hidden
`dev-agents:issue-fix` PR marker in addition to every normal review, validation, mergeability, and
pause gate. Regular pull requests remain manual.

The auto-merge request has its own SQLite claim keyed by pull request and head SHA, separate from
the feedback-fix claim. This means a no-op run created while checks are pending cannot suppress a
later `check_run.completed` attempt. After `gh pr merge --auto --squash`, the daemon verifies that
GitHub set `autoMergeRequest` (or merged the PR) and records the result.

With `review_without_copilot: true` (the default), an open, non-draft PR targeting the configured
base branch is checked for a submitted Copilot review. If none exists, the daemon runs a two-pass
internal review—even when GitHub has not reported any checks yet: a general defect pass followed by
the target repository's canonical `.agent/skills/codex-review/SKILL.md` pass (with compatibility
fallbacks for older repositories). Concrete findings are fixed,
tested, committed, and pushed from an isolated worktree. Auto-merge remains stricter and requires
at least one completed successful check. A review chain allows one full initial review and, only
when that review pushes a fix, one targeted post-fix verification of the resulting diff and prior
findings. The chain is persisted per PR head SHA, capped after that verification, and a later
user push starts a new chain; duplicate webhook and reconciliation deliveries are deduplicated.

To avoid concurrent-agent rewrite loops, `pause_on_external_agent_commits` is enabled by default.
When the latest commit author matches `external_agent_logins` (Jules is included in the example
configuration), all review, fix, and auto-merge activity pauses for that PR. The daemon publishes
one idempotent pause comment per head. Apply the configured `external_agent_resume_label`
(`dev-agents-resume` by default) when the external agent is finished and automation should resume.

## Issue fixer

Projects may also opt in to the label-driven issue fixer:

```yaml
issue_fixer:
  label: bug
  base_branch: main
  branch_prefix: dev-agents/issue-
```

On startup and during reconciliation, open issues carrying the configured label are claimed in
SQLite. Luna works in an isolated branch, runs the target repository's validation, pushes the
branch, and opens a PR containing an issue marker. The normal PR review/fix workflow then owns
that PR; the issue fixer never merges or closes it. Existing marked PRs prevent duplicate issue
branches, and failed claims may be retried safely.

The PR workflow's `pause_on_external_agent_commits`, `external_agent_logins`, and
`external_agent_resume_label` settings also protect issue-fixer PRs. If an external agent such as
Jules pushes the latest commit, all automation pauses for that PR until the resume label is added.

After a successful remediation run, the daemon also creates or updates top-level PR comments for
that run. A review creates one lifecycle comment; Luna updates it with concise findings before
editing and a fixes-started status when concrete defects need remediation. Luna must also emit a
validated structured `REPORT_JSON` containing the verdict, findings with severity/category/location,
the categories checked, validation commands, and fixes. The normalized result is persisted in the
run metadata and event timeline; the final lifecycle update uses its human-readable summary to
explain what actually changed, alongside the resulting commit, PR files, and validation result.
Each review or issue run carries one hidden lifecycle marker so retries update the same comment
instead of adding phase-by-phase duplicates. Pause alerts and completed feedback-fix summaries stay
separate because they represent distinct actionable events. Comment delivery is best-effort and is
recorded in workflow event metadata.

Report deployments are also best-effort and rate-limited. Unchanged report content reuses its last
deployment; changed content is limited to one public upload per hour and 24 uploads per rolling
24 hours by default. Local report files still refresh immediately. A Vercel daily-quota response
pauses further attempts for 24 hours. Configure
the limits with `report_vercel_min_interval_seconds` and `report_vercel_max_deployments_24h`.
