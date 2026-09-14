# Release Communications Workflow (`release-comms`)

The `release-comms` workflow evaluates deployed release changes, drafts announcement copy, validates it, and optionally publishes it to approved destinations.

## Architecture

1. **Context Resolution**: Resolves release changes for a given deployment / promote run ID (identifying commit SHAs, git log, diffs, and promotion metadata).
2. **Evaluation**: Assesses whether changes are user-facing and postworthy, ranking importance and identifying featured improvements.
3. **Draft Generation**: If postworthy, generates Bluesky and long-form drafts. Discord is derived mechanically from each Bluesky message with hashtags removed; it does not require a separate draft.
4. **Publishing**: When explicitly approved (or configured with `auto_publish`), publishes directly to target destinations from the daemon. Every successful channel write is checkpointed in SQLite. Publishing is dry-run by default.

## CLI Usage

Evaluate release changes and generate drafts on demand:

```bash
# Evaluate in dry-run mode (outputs evaluation and generated drafts without publishing)
uv run dev-agents release-comms evaluate codex-cryptica <promote_run_id> --config config/projects.yaml

# Evaluate and publish approved drafts
uv run dev-agents release-comms evaluate codex-cryptica <promote_run_id> --config config/projects.yaml --publish
```

## Local content generation (opt-in)

Evaluation and drafting run in the daemon. Product-specific voice still comes
from the target repository's release skills in `skills/` when present, with
the built-in skills as a fallback:

- The evaluator and writers run as workflow nodes through the configured
  providers, with prompts rendered from the `release-evaluate`,
  `release-shortform`, and `release-longform` skills in `skills/` — the same
  files a human or agent loads for manual drafting, so pipeline and hand-made
  copy share one voice.
- Discord copy is derived from the Bluesky drafts (hashtags stripped) and the
  recommended channels reflect what was actually generated.
- `evaluate` dry-run output then contains real drafts instead of empty lists.
- Each Bluesky draft names its art (`image` key or `capture:<page>` request),
  preferring exact keys from the asset DB inventory fed into the shortform
  prompt — the DB is always consulted before anything is created.
- Merge verifies every URL against the asset CDN, appends resolving-but-undocumented
  assets to `docs/deployment/r2-asset-db.md` in the target checkout, and journals
  an `images` phase (`ok`, `missing`, `capture_requested`, `db_updated`).
- With `image_generation: true`, a conditional `generate_art` node runs after
  merge (skipped entirely when every asset resolves or the flag is off) and
  generates art in-provider order (`image_providers`, default
  `agy → codex → muse`; verified live: agy draws natively, codex draws at high
  token cost, muse falls back to programmatic drawing). Output is magic-byte
  verified and staged under `<log_dir>/images/`. During an approved live run,
  generated PNGs are uploaded to R2 under `announcements/` and the public URL
  is carried into both Bluesky and GitHub Discussion delivery. Dry runs only
  stage the files. A `generated` journal phase records local files, uploaded
  URLs, and upload errors.
- The graph branches on the evaluator signal: a `route_forms` node runs the
  shortform writer, the longform writer, or both in parallel (first real
  conditional edge in the repo). Empty or unrecognized channel signals fail
  open to every enabled form; `forms: [short]` (or `[long]`) in config forces
  one side. The journal `routed` phase shows which branch ran.

Live publishing is owned by `dev-agents`. Bluesky, Instagram, X, Discord, and
GitHub Discussions are delivered by the daemon's direct adapters. Feed-oriented
channels use the square, compressed Cloudflare R2 image variant; long-form
Discussion bodies retain the canonical asset URL. Reddit is out of the
contract: reuse the GitHub Discussion body for a manual Reddit post.

The target repository only supplies repository-specific inputs: the GitHub
slug, `.social/discord-destinations.yaml`, and release copy/image metadata.
It no longer needs a listener, publisher script, or daemon systemd unit.

The daemon environment should include `CLOUDFLARE_API_TOKEN` with permission to
write the `codex-cryptica-statics` R2 bucket. As a migration fallback, the
publisher reads only this variable from the target repository's `.env` when the
service environment does not provide it; it never imports the rest of that
file.

Live publications use a random delay between
`publication_delay_min_seconds` and `publication_delay_max_seconds` (15–30
minutes by default). All channel variants of one message are sent in the same
batch without an artificial delay; the delay is applied only between distinct
messages. The delay is skipped for dry runs, and retries do not add a delay for
channels already checkpointed.

The daemon resumes scheduled runs every `scheduler_poll_seconds` (30 seconds by
default). The scheduled run stores its drafts, image URLs, and next wake time in
SQLite, so a daemon restart resumes the queue without regenerating content.

## Webhook Integration

The workflow integrates with deployment pipelines (such as GitHub Actions deployment / promotion workflows):
1. Deployment workflow posts a payload containing `{"promoteRunId": "<run_id>"}` to `/release-comms`.
2. The `dev-agents` daemon validates the webhook secret via header `X-Release-Comms-Secret` or `X-Hub-Signature-256`.
3. Runs the `release-comms` LangGraph workflow asynchronously in the background.

Each promote run is claimed in the configured SQLite state database before the workflow starts.
Set `release_comms.state_path` to choose its location; when omitted, the default is
`<target-repository>/.dev-agents/release-comms-state.db`. A completed or currently running promote
run is rejected idempotently, while failed runs may be retried and retain their attempt count.

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
