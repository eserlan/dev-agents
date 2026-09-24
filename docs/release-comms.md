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
Discussion bodies retain the canonical asset URL. Reddit is delivered via the
`dev-agent-publisher` Devvit companion app (`apps/reddit-devvit`): `dev-agents`
stages candidate JSON manifests to `raw.githubusercontent.com` on the target
repository's `release-manifests` branch, and the companion app pulls them to
manage cadence gates and native Reddit submission. Live status is reconciled
automatically or via `dev-agents release-comms sync-reddit <project>`. External
subreddits can be exported for manual posting with `dev-agents release-comms
export-reddit <project> <run-id>`.

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

## Avoiding repeat announcements

Each promote run used to know only its own publications, so consecutive releases touching the
same feature were announced again with different wording. Two layers now look back
`recent_posts_days` days (14 by default) across earlier runs:

1. **Prompt history.** The evaluator and both writers receive a "recently announced" block (page,
   channels, first line of what was said). The evaluator must treat a listed feature as not
   postworthy again, including follow-up fixes and URL moves, and returns `postworthy: false`
   when everything user-facing was already covered.
2. **Publish guard.** Before publishing, any draft whose page URL ends in the same path segment
   as an earlier announcement is skipped on the channels that carried it (so `/tools/x` and
   `/generators/x` count as one page). The skip is recorded as a `repeat_suppressed` event on
   the run, listing the channels and earlier run IDs. Because only the last path segment is
   compared, two different pages that share a slug (say `/blog/tips` and `/answers/tips`) are
   treated as one; that fails toward not posting.

## Pinterest setup

Pinterest is an opt-in destination (`publish_pinterest` in `release_publish.py`), wired the
same way as Instagram: it rides on the Bluesky draft's image + text, so it only fires for
messages that already resolved an image. Nothing below is required unless a project adds
`pinterest` to its `destinations` list.

1. **Create a Pinterest business account** for the target product (a personal account cannot
   create an app). Business accounts are free to convert at pinterest.com/business/create.
2. **Register an app** at [developers.pinterest.com/apps](https://developers.pinterest.com/apps).
   Note the app's client ID/secret — only needed once, to mint the access token below.
3. **Generate an access token** via Pinterest's OAuth flow, granting at minimum the
   `pins:write` and `boards:read` scopes. Pinterest access tokens expire (the standard OAuth
   token lifetime); the daemon does **not** refresh them, so plan to re-mint the token
   periodically (or run Pinterest's refresh-token flow externally and update the secret) —
   treat this the same as rotating any other credential, not a one-time setup step.
4. **Create (or pick) a board** to pin to, and get its `board_id`: call
   `GET https://api.pinterest.com/v5/boards` with the access token, or read the ID out of the
   board's Pinterest URL/settings.
5. **Set daemon environment variables** (same environment the systemd unit runs under, not the
   target repository's `.env`):
   - `PINTEREST_ACCESS_TOKEN` — the token from step 3.
   - `PINTEREST_BOARD_ID` — the board ID from step 4.
   - `PINTEREST_API_URL` — optional, only for pointing at a mock/staging API in tests; defaults
     to `https://api.pinterest.com/v5`.
6. **Enable the destination** in `config/projects.yaml` for the project:
   ```yaml
   release_comms:
     destinations:
       - bluesky
       - pinterest
   ```
7. **Dry-run first.** `dev-agents release-comms evaluate <project> <promote_run_id>` (without
   `--publish`) exercises the same code path and returns a `dry-run://pinterest/<page_url>`
   receipt without calling the Pinterest API — confirms the image resolves and the batch wiring
   picks up the channel before any credentials are needed.
8. **Verify a live pin once `--publish`/`auto_publish` is on**: check the run's publication
   receipt (`public_url` becomes `https://www.pinterest.com/pin/<id>/`) and confirm the pin
   appears on the configured board, since Pinterest's API does not surface most content-policy
   rejections as a distinct error — a silent-looking failure is usually a scope, expired-token,
   or board-permission problem rather than a code bug.

Pinterest's own image guidance favors a taller 2:3 crop; this pipeline currently delivers the
same 1080×1080 square R2 variant used for Instagram/Bluesky (via `social_delivery_image_url`).
That renders fine but isn't Pinterest-optimal — revisit if Pinterest engagement matters enough
to justify a second image variant.

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
      recent_posts_days: 14   # lookback for the repeat-announcement checks
```
