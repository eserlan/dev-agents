# Release Communications Workflow (`release-comms`)

The `release-comms` workflow evaluates deployed release changes, drafts channel-specific announcements (e.g., Bluesky and Discord), validates drafts, and optionally publishes them to approved destinations.

## Architecture

1. **Context Resolution**: Resolves release changes for a given deployment / promote run ID (identifying commit SHAs, git log, diffs, and promotion metadata).
2. **Evaluation**: Assesses whether changes are user-facing and postworthy, ranking importance and identifying featured improvements.
3. **Draft Generation**: If postworthy, generates draft copy tailored to configured destinations (Bluesky character limits, link attachment, Discord formatting).
4. **Publishing**: When explicitly approved (or configured with `auto_publish`), publishes directly to target destinations via the target repository's comms runner. Publishing is dry-run by default.

## CLI Usage

Evaluate release changes and generate drafts on demand:

```bash
# Evaluate in dry-run mode (outputs evaluation and generated drafts without publishing)
uv run dev-agents release-comms evaluate codex-cryptica <promote_run_id> --config config/projects.yaml

# Evaluate and publish approved drafts
uv run dev-agents release-comms evaluate codex-cryptica <promote_run_id> --config config/projects.yaml --publish
```

## Webhook Integration

The workflow integrates with deployment pipelines (such as GitHub Actions deployment / promotion workflows):
1. Deployment workflow posts a payload containing `{"promoteRunId": "<run_id>"}` to `/release-comms`.
2. The `dev-agents` daemon validates the webhook secret via header `X-Release-Comms-Secret` or `X-Hub-Signature-256`.
3. Runs the `release-comms` LangGraph workflow asynchronously in the background.

## Configuration

In `config/projects.yaml`:

```yaml
projects:
  codex-cryptica:
    repo: /path/to/repo
    github: owner/repo
    release_comms:
      tracking_issue: 100
      destinations:
        - bluesky
        - discord
      auto_publish: false
      webhook_path: /release-comms
      webhook_secret_env: RELEASE_COMMS_WEBHOOK_SECRET
```
