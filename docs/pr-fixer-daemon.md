# PR fixer daemon

The daemon is designed to be a drop-in replacement for the existing `remotecc` PR-fixer
listener. It runs the Python listener under systemd and keeps Cloudflare Tunnel as a separate,
dependent service. Secrets and tunnel credentials stay outside this repository.

PR reviews, PR fixes, and issue-fixer queues (including `gui-fix`) use Codex. The runtime's
default model is `gpt-6-luna`; set `pr_fixer.model` to override it for a project.
The PR-fixer `providers` setting is retained for configuration compatibility, but it cannot enable
fallback to another agent. `reasoning_effort` controls the Codex reasoning level and defaults to
`high`.

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

Agent processes run with `TMPDIR=~/.cache/dev-agents/tmp` instead of `/tmp`. Validation tooling the
agents invoke (node, bunx, `fallow audit`, ...) caches under `$TMPDIR` and never cleans up after
itself; on a typical desktop `/tmp` is RAM-backed and often quota-limited, and these caches filled
it until unrelated tools (including the shell) failed with "Disk quota exceeded". After each agent
run, entries in that directory untouched for 24 hours are removed. Only agent subprocesses are
redirected; the daemon's own temporary files are unchanged.

When a target repository provides `scripts/affected-workspaces.mjs`, `scripts/lint-changed.mjs`,
and `scripts/test-changed.mjs`, remediation and review prompts require Luna to follow that
repository's CI scope selection. Ordinary PRs run the changed-file lint/test validators and the
independent type-check commands in parallel after dependencies are installed. Changes to shared
configuration, scripts, or dependency manifests follow the workflow's widened affected
workspace/full-validation path. Repositories without those helpers use focused changed-file
validation as directed by their own instructions.

If a `pull_request.synchronize` event advances a PR while its fixer or review is running, the
daemon terminates that run's agent process, closes the old run as superseded, and immediately
restarts feedback collection against the latest head. Other webhook events do not cancel active
work; reconciliation still avoids starting duplicate work for a PR already being handled.

## Review stopping rules

Blocking audit findings must cite an accepted requirement, a reachable failure trigger, and the
changed code that introduced the failure. Hardening, missing tests alone, broader coverage and
design suggestions are nonblocking limitations and do not trigger automatic repairs. Generated
prose is checked for structural facts; semantic guarantees require an explicit product contract
rather than increasingly elaborate word-matching heuristics.

Post-fix reviews preserve the original acceptance criteria and inspect only unresolved findings
and regressions caused by repairs. The final targeted review verifies only; it cannot start a
third repair. Writer starts (including failed attempts) count toward a persistent two-attempt
budget per PR across all heads and review chains. Historical pushed repairs count as attempts.

A fixer may return `disputed` with source-backed reasons instead of editing. Disputes and exhausted
budgets persist `review_halted` and the unresolved report in SQLite, post a review-halted comment,
and stop automatic reviews and internal-review auto-merge eligibility for that PR, including after
new pushes or daemon restarts. Human adjudication is required. There is no automatic reset; an
operator must deliberately clear the halted metadata and repair-attempt history to authorize a
fresh budget. Previously saved audits without the new required evidence fields must be re-audited.

## State database

Workflow state is stored in the configured `pr_fixer.state_path` SQLite database. If it is omitted,
the default is `~/.local/state/dev-agents/<project>/pr-fixer-state.db`. SQLite WAL mode and a
30-second busy timeout allow webhook and reconciliation workers to share the database safely.

Run claims record the owning process identity. On daemon startup, claims whose owner exited are
marked failed and become retryable; periodic issue and PR reconciliation then resumes the work.
Issue retries reuse an interrupted attempt's registered worktree when it is still available, so
uncommitted edits survive a service restart. Older claims without process identity retain the
one-hour stale-run fallback.

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

After posting findings, dev-agents waits at least `review_claim_window_seconds` (default 60)
before repairing them. An active external process can post `/dev-agents claim <run-id>` as a
standalone PR comment any time after the findings, until the repair actually starts; reconciliation
may start it well after the window closes, and a claim posted before then still counts. Only owners, members and collaborators can claim work. The first
valid claim pauses repairs until 30 minutes after the claim. Repeated comments do not renew it.
If the head advances, the new revision gets a fresh review; otherwise reconciliation resumes
the saved audit's repairs when the claim expires. Waiting survives restarts and releases the
worktree. Set the window to zero
for immediate repairs. The old attribution-based handoff settings no longer control repairs.

With `review_without_copilot: true` (the default), an open, non-draft PR targeting the configured
base branch is checked for a submitted Copilot review. If none exists, the daemon runs an independent
read-only auditor, even when GitHub has not reported any checks yet. The auditor inspects general
defects and the target repository's canonical `.agent/skills/codex-review/SKILL.md` guidance (with
compatibility fallbacks for older repositories). Only valid, evidence-backed findings are handed to
a separate fixer for repairs, tests, commits and pushes in the isolated worktree. A clean audit runs
no fixer; malformed or incomplete audits cannot qualify as clean. Auto-merge requires
at least one completed successful check. A review chain allows one full initial review and, only
when that review pushes a fix, a targeted post-fix verification of the resulting diff and prior
findings. If that verification itself pushes a fix without a `clean` verdict, the new head has
still not been reviewed, so one more targeted round runs (`MAX_INTERNAL_REVIEW_ROUNDS = 3` reviews
in total: one full and up to two targeted). Auto-merge only accepts a `clean` verdict, so without
that extra round such a PR would wait forever for a review that could never come. A round that pushes nothing or comes back
`clean` ends the chain early. The chain is persisted per PR head SHA, capped after the final
round, and a later user push starts a new chain; duplicate webhook and reconciliation deliveries
are deduplicated. If the last round still is not clean, auto-merge stays deferred and a human
should review and merge.

The auditor falsifies premises and examines test completeness, frame/resource budgets,
security/privacy boundaries, lifecycle ownership and accessibility. Passing tests do not substitute
for tracing those paths. Relative Markdown links, reference definitions and backtick document paths
are recursively bundled, including the general methodology, patterns catalogue and constitution.
Cycles are deduplicated, paths escaping the target repository are rejected, and missing/over-budget
guidance is explicitly listed. The auditor uses Codex `--sandbox read-only`, never approvals, and
`--ignore-user-config` to exclude user-configured MCP servers and hooks. Its final response is a
schema-constrained JSON file; the daemon checks that source HEAD and worktree status are unchanged.
Auditing and fixing share the configured run time budget. A concurrent push stops a stale auditor's
findings from reaching the fixer.

Independent audit evidence is persisted before repair. Each run records stage, source SHA, verdict,
finding count, categorized checks, limitations and audit artifact path. Later Copilot and independent
human inline reviews are compared by original source revision and location, with snapshots stored
in the event history. `potential_false_negative_rate` is a positional diagnostic proxy; unmatched
comments are candidates to investigate, not automatically confirmed misses. Reference feedback is
never fed into the independent audit. The existing reviewer decision still controls merge readiness.
Fixer execution has separate start/completion events and records its exit code and timeout status.
Audit logs, JSON/schema files and final fixer reports follow the PR log retention period.

[dev-agents #22](https://github.com/eserlan/dev-agents/issues/22) owns this separation and reviewer
quality tracking. Read-only historical replays are documented in
[the PR auditor replay notes](../evals/pr-audit/README.md).

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

Additional queues can use their own label and work framing. For Codex Cryptica, the copy review
queue is configured alongside the existing bug queue:

```yaml
issue_fixers:
  - label: copy-fix
    kind: copy
    base_branch: staging
    branch_prefix: dev-agents/copy-fix-
```

When an open issue in that repository receives the `copy-fix` label, Luna uses the issue and
surrounding repository content to make the requested copy edits, preserves meaning and formatting,
then pushes a branch and opens a PR against `staging`. Ambiguous editorial requests are reported
without guessing. The normal PR review workflow handles the resulting PR.

The `cif` queue handles focused improvements to feature help content:

```yaml
issue_fixers:
  - label: cif
    kind: help
    base_branch: staging
    branch_prefix: dev-agents/cif-
```

Codex inspects the target repository's documentation instructions and existing help system, then
updates the help for the feature named in the issue. It opens a PR for the usual review pipeline;
unclear feature requests are reported without guessing.

An optional reactive copy review runs after an Answer PR is merged to the configured branch:

```yaml
copy_review:
  base_branch: staging
  page_globs: ["apps/web/src/lib/content/answers/pages/*.ts"]
  instructions_path: docs/answer-quality-review.md
  model: gpt-6.1-sol
  reasoning_effort: low
  timeout_minutes: 10
```

The daemon recognizes Answer PRs by their `answer:` title or `dev-agents/answer-` branch prefix,
identifies changed Answer pages using `page_globs`, and reviews the complete source and metadata at
the merge commit. It checks the original issue and neighboring content, then records a structured
`clean`, `changes_required`, or `needs_human_review` verdict with evidence, findings, limitations,
Answer paths, PR number, and exact source revision in SQLite and the assessment log. Infrastructure
or parse failures are recorded as failed runs. Blocking findings produce one focused issue labelled
`copy-fix`; suggestions alone do not block. Newly added pages take priority over related pages that
only received link updates. Starts, skips, failures and outcomes are logged to the daemon journal.

The current trigger covers merged PRs targeting the configured branch whose title starts `answer:`
or whose branch starts `dev-agents/answer-`. It does not automatically review an unrelated PR just
because it edits an existing Answer page. Webhook delivery and run claims are deduplicated by
delivery ID and merge SHA. Copy-fix PRs from `dev-agents/copy-fix-` receive a **single targeted
verification** against their linked issue after merging. A clean result is commented on that PR and
recorded against the correction merge commit. An unresolved or unverifiable correction is marked
`needs_human_review` and commented on; the daemon does not open another automated correction issue,
so this path cannot loop indefinitely. A correction that changes no configured Answer source is
also escalated for human review. Remove `copy_review` to disable both review triggers.

Whole-article review is part of both the initial review and correction verification. The reviewer
maps repeated substantive guidance across sections, tests whether worked examples use credible
beginner choices and demonstrate real consequences, and checks headings and overall reader flow.
It groups overlapping edits and proposes a specific consolidation while preserving factual fixes.
Brief recaps, optional wording preferences and concise reference Answers without worked examples
can pass. These are semantic assessments, not punctuation bans or word-count targets.

Every new assessment must report evidence and status for repetition, examples and flow. A missing
check cannot silently qualify as clean. Material coherence defects after correction go to
`needs_human_review` with actionable findings on the correction PR; they do not create another
automated issue. Original briefs are recovered from the prior review or explicit issue references
when available. Editorial clean is separate from technical lint compliance and presentation checks;
inaccessible rendering is reported as a limitation.

This implements the editorial-cohesion slice of [dev-agents #21](https://github.com/eserlan/dev-agents/issues/21)
within [#19](https://github.com/eserlan/dev-agents/issues/19). Writer instruction ownership stays with
[CC #3918](https://github.com/eserlan/Codex-Cryptica/issues/3918). It does not implement #19's production gate.
Read-only AI replays are available through `evals/replay_answer_quality.py`; see
[the replay notes](../evals/answer-quality/README.md).

`instructions_path` loads the project's editorial policy from its checkout (an absolute path is
also accepted). The Answer quality reviewer prioritises correctness, conceptual reasoning,
practical use, player agency, consistency, content overlap, metadata and product accuracy over
minor wording. It can inspect surrounding repository content and GitHub issues using read-only
commands. Only material improvements warrant a consolidated issue with strengths to preserve,
specific problems, proposed improvements and acceptance criteria. A matching open issue is reused
and receives `copy-fix` if needed.

Each completed review records a score out of ten, verdict, strengths, evidence-backed findings
(severity, category, location, reader impact and proposed correction), checks and limitations. The
score, verdict and summary are logged to the journal; the full assessment is stored beside the agent
log as `.assessment.json` and in SQLite run metadata. The final assessment is read from Codex's
final-message output file, so echoed prompts and intermediate commentary cannot be mistaken for the
result. `clean` means no unresolved blocking editorial defect was identified at the reviewed merge
commit; it does not assert that every subjective improvement has been made.

This workflow ends at corrected staging content. Codex Cryptica promotes an explicitly selected
staging deployment artifact through a separate workflow in the target repository. This daemon does
not determine which artifact is promoted or enforce editorial readiness for production; a clean
review or correction verification must not be presented as proof that production contains that
content. The promotion workflow needs a separate artifact-specific readiness integration.

Provider fallback is available per issue queue. The Answer queue can keep Muse as its primary
writer and use Codex with `gpt-6-luna` when Muse exits unsuccessfully or times out:

```yaml
- label: answer
  kind: answer
  provider: muse
  model: muse-wonder-1.0
  fallback_provider: codex
  fallback_model: gpt-6-luna
```

On startup and during reconciliation, open issues carrying the configured label are claimed in
SQLite. Luna works in an isolated branch, runs the target repository's validation, pushes the
branch, and opens a PR containing an issue marker. The normal PR review/fix workflow then owns
that PR; the issue fixer never merges or closes it. Existing marked PRs prevent duplicate issue
branches, and failed claims may be retried safely.

Reopening an issue starts a new remediation cycle even when its title and body are unchanged.
The latest GitHub reopen event identifies the cycle, so repeated webhook deliveries and
reconciliation passes are deduplicated, while reconciliation can recover a missed webhook.
Each reopened cycle uses a fresh branch; an existing open fix PR still prevents another run.

The PR workflow's `pause_on_external_agent_commits`, `external_agent_logins`, and
`external_agent_resume_label` settings also protect issue-fixer PRs. If an external agent such as
Jules pushes the latest commit, all automation pauses for that PR until the resume label is added.

After a successful remediation run, the daemon also creates or updates top-level PR comments for
that run. Each review run has one status comment (marker `<!-- dev-agents:pr-review run=… -->`)
that is edited in place as the run moves through started, in progress, fixes started and its final
outcome, with a collapsed run history of those steps; retries of the same run update it too. A
fix push starts a new run and so a new status comment. Luna must also emit a validated structured
`REPORT_JSON` containing the verdict, findings with severity/category/location, the categories
checked, validation commands, and fixes. The normalized result is persisted in the run metadata and
event timeline; the final status update uses its human-readable summary to explain what actually
changed, alongside the resulting commit, PR files, and validation result.

Edits notify nobody, so events that may need a human post their own comment: the findings (with
the claim instructions, marker `pr-review-findings`), pause alerts, claims, halts, fix failures and
completed feedback-fix summaries. Comment delivery is best-effort and is recorded in workflow event
metadata.

Report deployments are also best-effort and rate-limited. Unchanged report content reuses its last
deployment; changed content is limited to one public upload per hour and 24 uploads per rolling
24 hours by default. Local report files still refresh immediately. A Vercel daily-quota response
pauses further attempts for 24 hours. Configure
the limits with `report_vercel_min_interval_seconds` and `report_vercel_max_deployments_24h`.


Agent subprocesses run in individual transient user systemd scopes when the user
manager is available. Cancellation, timeout, and normal completion stop all
processes in that scope before releasing the worktree, including children that
start separate sessions. systemd allows five seconds for termination before
killing survivors. Outside systemd, cleanup waits for and escalates the original
process group; children that detach into separate sessions are not covered by
that fallback. If shutdown cannot be confirmed, the PR worktree is retained for
inspection rather than removed. No service restart is performed by this change.
