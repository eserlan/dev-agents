# Implementation Plan: Reddit Publishing via Devvit in `dev-agents`

## 1. Goal & Philosophy

Build a safe, high-signal, automated Reddit publishing pipeline for `dev-agents`, leveraging Reddit's official first-party **Devvit** developer platform.

### Core Principles
- **Discussion over Marketing**: Posts lead with genuine utility, worldbuilding/RPG problems, and design questions—never release-note spam or marketing copy.
- **Solo Developer Voice with Honest Disclosure**: Authentic builder framing grounded in real repository changes. Posts automated via the Devvit app openly disclose their automated dispatch from the project's builder bot.
- **First-Party Reddit Platform**: Use Devvit (`@devvit/public-api`) rather than unofficial bot accounts, scraping, or browser automation (avoiding rate-limit bans and credential exposure).
- **Clear Automation Boundary**: 
  - Subreddits we control (e.g. `r/codexcryptica`): automated scheduling permitted with human-overridable queue and minimum 24h cadence gates.
  - External subreddits (e.g. `r/rpg`, `r/worldbuilding`): manual review, CLI export, and human-only posting. Devvit apps cannot and must not attempt to post across uninstalled subreddits.
- **Separation of Concerns**:
  - `dev-agents` (Python): owns content inspection, evaluation, unified long-form draft generation, R2 candidate staging, and receipt reconciliation. Target repositories remain 100% free of publisher code.
  - Devvit App (TypeScript, in `apps/reddit-devvit/`): generic companion app owned by `dev-agents`. Manages in-Reddit Redis queue, cadence gates, moderator menu actions, and native Reddit submission (`submitPost`).

---

## 2. Architecture Overview

```text
               Release Deployment Webhook (promoteRunId)
                                  │
                                  ▼
                        [ dev-agents Daemon ]
                     (src/dev_agents/workflows/)
                                  │
     ┌────────────────────────────┼────────────────────────────┐
     ▼                            ▼                            ▼
release-evaluate          release-shortform            release-longform
(Postworthy check)       (Bluesky / Discord)       (GitHub Discussions & Reddit)
                                  │                            │
                                  │                   (Same copy, channel-
                                  │                    specific art attachment)
                                  ▼                            │
                         release_publish.py ◄──────────────────┘
                                  │
         ┌────────────────────────┴────────────────────────┐
         ▼ (Live API calls)                                ▼ (Stage candidate)
  Bluesky / Discord / X / Discussions              Cloudflare R2 Bucket
         │                                      (announcements/reddit-queue.json)
         ▼                                                 │
  Stage 1: Record immediate                        Stage 1: Record
  live receipts in SQLite                       "staged" receipt in SQLite
                                                           │
                                                           ▼ (Hourly fetch / Mod sync)
                                                 [ Devvit App on Reddit ]
                                                   (apps/reddit-devvit/)
                                                           │
                                                   Devvit Redis Queue
                                                 (pending / approved)
                                                           │
                                           ┌───────────────┴───────────────┐
                                           ▼                               ▼
                                   Devvit Scheduler                Moderator Menu Action
                                (Auto-cadence: >= 24h)           ("Publish Next Post Now")
                                           │                               │
                                           └───────────────┬───────────────┘
                                                           ▼
                                                context.reddit.submitPost()
                                                Published to r/codexcryptica
                                                           │
                                                           ▼
                                                Stage 2: Reconciliation
                                            (dev-agents reads public new.json
                                             and updates SQLite receipt to live URL)
```

---

## 3. Directory Layout in `dev-agents`

```text
dev-agents/
├── apps/
│   └── reddit-devvit/                 # Generic first-party Devvit companion app
│       ├── devvit.yaml                # Permissions (redditAPI, redis, scheduler, http)
│       ├── package.json               # @devvit/public-api, typescript, bun/node config
│       ├── tsconfig.json
│       └── src/
│           ├── main.ts                # App entrypoint, AppInstall/AppUpgrade triggers, mod menu
│           ├── queue.ts               # Redis queue operations (enqueue, dequeue, get status)
│           ├── publisher.ts           # Reddit submitPost logic & link/image formatting
│           └── types.ts               # Post payload, queue state, and metadata types
├── skills/
│   └── release-longform/
│       └── SKILL.md                   # Unified long-form writer (GitHub Discussions & Reddit)
├── src/dev_agents/
│   ├── cli.py                         # Adds sync-reddit and export-reddit commands
│   └── workflows/
│       ├── release_comms.py           # Orchestrates unified drafting and stages Reddit payloads
│       ├── release_content.py         # Formats long-form copy for both Discussions and Reddit
│       └── release_publish.py         # R2 staging adapter and Stage 2 public reconciliation
├── tests/
│   ├── test_release_longform_draft.py # Unit tests for long-form draft formatting
│   └── test_reddit_publish_adapter.py # Unit tests for R2 staging and receipt reconciliation
└── docs/
    ├── plans/
    │   └── reddit-devvit-publishing.md# This implementation plan
    └── reddit-publishing.md           # Setup, deployment, and moderation runbook
```

---

## 4. Component Details & Decisions

### 4.1. Devvit Companion Application (`apps/reddit-devvit`)

Runs inside Reddit's sandboxed server environment, installed onto target subreddits (e.g. `r/codexcryptica`).

#### Generic Configuration (`devvit.yaml`)
```yaml
name: dev-agent-publisher
version: 0.1.0
capabilities:
  redditAPI:
    - linksAndComments
  redis: true
  scheduler: true
  http:
    enable: true
    domains:
      - "assets.codexcryptica.com"  # Configured R2 CDN domain
```

#### Self-Healing Scheduler Registration
In Devvit, declaring a scheduler job does not start it. The app registers the recurring cron job automatically on install/upgrade:
```typescript
Devvit.addTrigger({
  event: 'AppInstall',
  onEvent: async (_, context) => {
    await context.scheduler.runJob({ cron: '0 * * * *', name: 'reddit_dispatcher' });
  },
});

Devvit.addTrigger({
  event: 'AppUpgrade',
  onEvent: async (_, context) => {
    await context.scheduler.runJob({ cron: '0 * * * *', name: 'reddit_dispatcher' });
  },
});
```

#### Redis Data Schema
- `reddit:post:<id>`: Hash containing:
  - `id`: Unique draft identifier (e.g. `reddit-3093-superhero-theme`)
  - `title`: Post title
  - `body`: Post markdown body (with trailing `<!-- id:<id> -->` tag for reconciliation)
  - `url`: Outbound link to the feature/page
  - `image_url`: Canonical CDN image attachment URL
  - `source_id`: Origin identifier (`pr-3093`, `run-xyz`)
  - `status`: `pending` | `approved` | `posted` | `rejected`
  - `created_at`: Epoch timestamp
  - `reddit_post_id`: Reddit submission ID (populated upon posting)
- `reddit:queue:approved`: Sorted set (score = `created_at`, value = `id`)
- `reddit:seen:<source_id>`: String key preventing duplicate ingestion from R2
- `reddit:config:paused`: Flag (`true` / `false`) serving as emergency kill-switch
- `reddit:last_published_at`: Epoch timestamp enforcing minimum 24-hour spacing

#### Moderator Actions (`Devvit.addMenuItem`)
1. **"Queue Status"**: Toast/modal displaying counts of pending/approved posts, last publish time, and paused status.
2. **"Sync Queue Now"**: Immediately calls `fetch()` on the R2 manifest to ingest new candidates without waiting for the next hourly tick.
3. **"Publish Next Post Now"**: Immediately pops and publishes the next queued item, bypassing the 24h cadence spacing lock.
4. **"Toggle Auto-Publish Pause"**: Flips `reddit:config:paused` to stop/resume the background scheduler.

---

### 4.2. Content Generation (Unified `release-longform`)

- **Single Writer Pass**: GitHub Discussions and Reddit share the exact same long-form draft generated by `skills/release-longform/SKILL.md`.
- **Identical Copy**: Both channels receive the same problem-first opening, distinct idea, bulleted specifics, and authentic table-discussion questions.
- **Channel-Specific Image Handling**:
  - GitHub Discussions embeds markdown image tags: `![alt](url)`.
  - Reddit submissions attach the image URL as a preview/link or markdown embed based on the subreddit's configured post type.
- **Author Attribution & Voice**:
  - Posts automated by Devvit publish under the app account (`u/dev-agent-publisher-devvit`).
  - The post footer includes standard authentic disclosure:
    ```markdown
    ---
    *Posted automatically via Codex Cryptica's release pipeline. Feedback and discussion welcome!*
    <!-- id:pr-3093 -->
    ```

---

### 4.3. Ingestion Bridge (Cloudflare R2 Push / Pull)

1. When `release-comms` runs and Reddit is in `recommended_channels`:
   - `release_publish.py` generates the candidate JSON payload.
   - Pushes to Cloudflare R2 at `announcements/reddit-candidates.json` using `wrangler r2 object put` (already authenticated in daemon environment).
2. The Devvit app's hourly scheduler (or mod clicking "Sync Queue Now") calls `fetch("https://assets.codexcryptica.com/announcements/reddit-candidates.json")`.
3. Devvit checks `seen:<source_id>`. If unseen, adds the post to Redis `reddit:queue:approved` and marks `seen:<source_id> = true`.

---

### 4.4. Two-Stage Receipt & Reconciliation Model

- **Stage 1 (At Workflow Run Time):**
  - When the candidate manifest is uploaded to R2, `release_publish.py` records:
    ```python
    PublicationReceipt(
        channel="reddit",
        destination="r/codexcryptica",
        page_url=candidate["page_url"],
        external_id=f"staged:{source_id}",
        metadata={"status": "staged_to_r2", "staged_at": timestamp},
    )
    ```
  - GitHub tracking issue reports: `- **reddit:** staged in queue (r/codexcryptica)`.
- **Stage 2 (Reconciliation to Live URL):**
  - Devvit posts include `<!-- id:<source_id> -->` in the body.
  - A reconciliation function reads the public JSON feed `https://www.reddit.com/r/codexcryptica/new.json` (zero auth required, 60 req/min limit).
  - Executed opportunistically:
    - On the next release run.
    - During periodic daemon scheduler ticks (1/hour).
    - Via manual CLI: `uv run dev-agents release-comms sync-reddit <project>`.
  - Once found: updates SQLite with the live `https://reddit.com/r/codexcryptica/comments/...` URL and appends the live link to the GitHub tracking issue.

---

### 4.5. Manual Export for External Subreddits

External subreddits (`r/rpg`, `r/worldbuilding`) cannot have Devvit apps installed.
- CLI command: `uv run dev-agents release-comms export-reddit <project> <promote_run_id>`.
- Outputs clean, human-reviewed Markdown ready for manual copy/paste from the developer's personal Reddit account.

---

## 5. Phased Implementation Roadmap

### Phase 1: R2 Staging & Unified Drafting in `dev-agents`
- [x] Update `skills/release-longform/SKILL.md` to ensure drafts are optimized for both Discussions and Reddit.
- [x] Add `stage_reddit_candidate()` in `src/dev_agents/workflows/reddit.py` uploading candidate JSON to R2.
- [x] Record Stage 1 `staged` receipt in SQLite.
- [x] Add `export-reddit` CLI command in `src/dev_agents/cli.py`.
- [x] Add unit tests for R2 staging payload structure.

### Phase 2: Devvit Companion App (`apps/reddit-devvit`)
- [x] Scaffold `apps/reddit-devvit` with `@devvit/public-api` and TypeScript configuration.
- [x] Configure `devvit.yaml` with `redditAPI`, `redis`, `scheduler`, and `http` capabilities.
- [x] Implement `src/queue.ts` with Redis operations (enqueue, dequeue, get status, deduplicate).
- [x] Implement `src/publisher.ts` wrapping `context.reddit.submitPost`.
- [x] Implement `AppInstall` / `AppUpgrade` scheduler triggers.
- [x] Add moderator menu items (`Queue Status`, `Sync Queue Now`, `Publish Next Post Now`, `Toggle Auto-Publish Pause`).
- [ ] Test in a private test subreddit using `devvit playtest`.

### Phase 3: Stage 2 Reconciliation & End-to-End Testing
- [x] Implement `sync_reddit_status()` in `src/dev_agents/workflows/reddit.py` querying public `r/<subshell>/new.json`.
- [x] Add `sync-reddit` CLI command in `src/dev_agents/cli.py`.
- [x] Wire reconciliation into daemon scheduler tick and tracking issue updater.
- [x] Run comprehensive automated unit and CLI tests across the entire pipeline.

---

## 6. Success Metrics & Verification Criteria

- **Zero Bot Spam**: Minimum 24h cadence spacing enforced in Devvit Redis.
- **Authentic Discussion**: Posts share the high-utility, problem-first voice of `release-longform`.
- **Zero Target Repo Code**: `dev-agents` owns all publishing code; target checkout remains 100% free of publisher infrastructure.
- **Fail-Safe Operation**: Global pause toggle in Reddit moderator menu halts posting immediately.
- **Traceability**: All posts begin as `staged` receipts and reconcile to live Reddit thread URLs in SQLite and GitHub issues.
