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

Live publishing is owned by `dev-agents`. Bluesky, Threads, Instagram, X,
Discord, and GitHub Discussions are delivered by the daemon's provider adapters. Feed-oriented
channels use the square, compressed Cloudflare R2 image variant; long-form
Discussion bodies retain the canonical asset URL. Reddit is delivered via the
`dev-agent-publisher` Devvit companion app (`apps/reddit-devvit`): release-comms
stages the approved candidate manifest, bundles it into the app, uploads the
app, and installs the latest version on the configured subreddit. The Reddit
app makes no external HTTP requests. A moderator can publish the next candidate
with one menu action; the app does not publish in the background. Live status is reconciled automatically or via
`dev-agents release-comms sync-reddit <project>`. External
subreddits can be exported for manual posting with `dev-agents release-comms
export-reddit <project> <run-id>`.

To enable this deploy, configure `release_comms.devvit_app_dir` in the target
project's config. The install target defaults to `release_comms.subreddit`;
`release_comms.devvit_subreddit` can override it. The daemon
account must have an authenticated Devvit CLI session and moderator access to
that subreddit. `auto_publish: false` requires an explicit release-comms publish
approval; that approval packages and installs the candidate but does not itself
click the Reddit post action. See the [Reddit Publisher Operations runbook](reddit-publisher-operations.md)
for setup, deploy, and troubleshooting steps.

The target repository only supplies repository-specific inputs: the GitHub
slug, `.social/discord-destinations.yaml`, and release copy/image metadata.
It no longer needs a listener, publisher script, or daemon systemd unit.

## Threads publishing

Threads is an opt-in release-comms destination. The workflow adapts the short
release copy into a separate Threads draft, then sends a provider-neutral post
record to the Threads adapter. That adapter owns Meta authentication, text/link
formatting, optional image upload by public URL, topic tags, UTM attribution,
and the Threads API calls. Other destinations keep the shared release batching,
deduplication, persistence, retry, and reporting behavior.

1. Create a Meta app with the Threads use case and authorize the publishing
   account with `threads_basic` and `threads_content_publish`.
2. Supply a Threads User Access Token to the daemon as `THREADS_ACCESS_TOKEN`.
   Use a long-lived token and rotate it before expiry through Meta's OAuth token
   refresh flow; token contents stay outside the repository and are never logged.
3. Add `threads` to the project's `release_comms.destinations` list. Optional
   provider settings live under `release_comms.destination_options.threads`:

   ```yaml
   release_comms:
     destinations: [bluesky, threads, discord]
     destination_options:
       threads:
         topic_tag: TTRPG
         utm_source: threads
         utm_medium: social
         utm_campaign: codex-cryptica-release
   ```

   Attribution defaults to `utm_source=threads`, `utm_medium=social`, and
   `utm_campaign=release`. Existing UTM values in the canonical URL are kept.
   Text-only posts attach the tracked URL as a Threads link preview; image posts
   include that same tracked URL in the post text.
4. Dry-run with
   `dev-agents release-comms evaluate <project> <promote_run_id>` before enabling
   `auto_publish`. Dry runs generate adapted copy and a Threads receipt without
   making an API call. A live approved run supports text/link posts and
   text/image posts, and stores the returned Threads post ID, container ID,
   tracked URL, and permalink when Meta returns one.

The adapter checkpoints Meta's media-container ID before attempting publish.
Retries reuse that same container rather than creating a second one, so a
network timeout after Meta accepted the publish cannot cause release-comms to
silently create a duplicate. If Meta rejects a reused/expired container, the
run remains failed for operator review instead of making a fresh post. The
standard Threads API is used at `https://graph.threads.net/v1.0`; set
`THREADS_GRAPH_API_URL` only for a compatible test endpoint. See Meta's
[Threads publishing guide](https://developers.facebook.com/docs/threads/) and
[Meta's API request collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api)
for current OAuth, permission, container, and publish requirements.

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
same feature were announced again with different wording. The root cause is overlapping diffs:
`previous_sha` comes from the last *successful* promote, so two releases in flight together both
start from the same commit and the second re-evaluates everything in the first. A new run
therefore starts its diff after the newest commit an earlier release-comms run already evaluated
(when that commit is an ancestor of this release and newer than the promote-derived start).
Failed runs are ignored, since they may never announce their range. The event log records the
original value as `promote_previous_sha` whenever the start moves.

Two more layers back that up, looking back `recent_posts_days` days (14 by default) across
earlier runs:

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

## Internal technical notes

Technical changes (performance, storage format, sync or backup internals, notable reliability or
security work) are not public announcements. The evaluator keeps them out of `features` and
instead returns an optional `internal_note`: one to three plain sentences in a dev-log voice. That
note is posted **only to the Discord destinations** (currently `main-community`, the project's own
community server) as `**Dev note:** ...`, never to Bluesky, Instagram, GitHub Discussions or
Reddit. It is recorded as a Discord publication with the synthetic page URL `internal-note:<run>`,
so it appears in the run report and is never sent twice for a run.

`postworthy` still refers to public announcements only, so a release with only technical changes is
recorded as "rejected" even though its note was posted. A failing Discord webhook is logged as an
`internal_note_failed` event and does not block the public posts. Routine chores (dependency
bumps, CI tweaks, formatting) get no note. A dry run reports the note in an `internal_note` event
without sending it.

## Pinterest setup

Pinterest is an opt-in destination (`publish_pinterest` in `release_publish.py`), wired the
same way as Instagram: it rides on the Bluesky draft's image + text, so it only fires for
messages that already resolved an image. Nothing below is required unless a project adds
`pinterest` to its `destinations` list.

> **Blocked on Pinterest Trial access.** Apps on Trial access cannot create pins in production
> (`403`, code 29). Until the app is upgraded to Standard, do not add `pinterest` to `destinations`.
> See [pinterest-standard-access.md](pinterest-standard-access.md) for the status, the upgrade
> requirements and the demo video plan.

1. **Create a Pinterest business account** for the target product (a personal account cannot
   create an app). Business accounts are free to convert at pinterest.com/business/create.
2. **Register an app** at [developers.pinterest.com/apps](https://developers.pinterest.com/apps).
   Note the app's client ID/secret — only needed once, to mint the access token below.
3. **Generate an access token** via Pinterest's OAuth flow, granting at minimum the
   `boards:read`, `boards:write`, `pins:read`, `pins:write` and `user_accounts:read` scopes
   (Pinterest names the missing scopes in a 401 if `pins:write`/`boards:write` are absent). Pinterest access tokens expire (the standard OAuth
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
