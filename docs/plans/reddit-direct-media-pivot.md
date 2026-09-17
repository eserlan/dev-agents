# Plan: Reddit Publishing & Manifest Staging Pivot

## Context & Problem
1. The Devvit review team rejected domain permissions for `assets.codexcryptica.com` under `http.domains` in `apps/reddit-devvit/`.
2. Under Reddit's [HTTP Fetch Policy](https://developers.reddit.com/docs/capabilities/server/http-fetch-policy), personal and custom domains (such as `*.codexcryptica.com`) are generally **not approved**.
3. A proposal to pivot to a direct Reddit OAuth "script" app was evaluated, but Reddit's **Responsible Builder Policy** closed self-service app creation at `reddit.com/prefs/apps`. New Data API apps require manual approval with strict use-case review and no SLA.

## Architectural Decision
Pivot candidate manifest staging from the personal R2 CDN (`assets.codexcryptica.com`) to **GitHub Raw (`raw.githubusercontent.com`)**, keeping the first-party Devvit companion app architecture intact.

### Why This Works
1. **Approved Devvit Domain Category**: Devvit's HTTP Fetch Policy explicitly permits and routinely approves public APIs and developer platforms, specifically `raw.githubusercontent.com` and `api.github.com`.
2. **Zero New App Approval Gates**: Bypasses the Reddit Data API manual ticket bottleneck—moderator-installed Devvit apps remain the official, supported platform.
3. **Native GitHub Integration**: `dev-agents` already manages the target repository (`github: owner/repo`) via the `gh` CLI. Staging candidate JSON manifests to a dedicated branch (e.g. `release-manifests`) is fully automated without polluting the main branch or requiring separate credentials.
4. **Moderator Configurable**: Devvit app settings expose `manifestUrl` so moderators can customize the raw GitHub URL directly in the Reddit interface.

---

## Technical Details

### 1. Devvit Companion App (`apps/reddit-devvit`)
- `devvit.yaml` & `devvit.json`: Allow-lists `raw.githubusercontent.com`.
- `src/main.ts`:
  - Sets `DEFAULT_MANIFEST_URL` to `https://raw.githubusercontent.com/<owner>/<repo>/release-manifests/announcements/reddit-candidates.json`.
  - Configures `Devvit.addSettings()` allowing subreddit moderators to inspect or override `manifestUrl`.
  - Dispatches hourly sync and menu actions using the configured URL.
- `README.md`: Documents `raw.githubusercontent.com` under `## Fetch Domains` explaining its role in candidate manifest synchronization.

### 2. Candidate Staging in `dev-agents` (`src/dev_agents/workflows/reddit.py`)
- `stage_reddit_candidate()`:
  - When `github` is provided (e.g. `project.github`), fetches the current manifest from `https://raw.githubusercontent.com/{github}/{branch}/{key}`.
  - Merges new candidate posts with deduplication by `id` and `source_id`.
  - Commits the updated JSON to `{branch}` via `gh api` without altering local working trees.
  - Falls back gracefully to Cloudflare R2 when `github` is not specified.
- `prune_published_reddit_candidates()`:
  - Prunes published candidate entries from the GitHub manifest upon reconciliation.
- `sync_reddit_status()`:
  - Reconciles live Reddit posts matching `<!-- id:... -->` tags and triggers manifest pruning on GitHub.

### 3. Pipeline Wiring (`src/dev_agents/workflows/release_publish.py`)
- `publish_release_drafts()` passes `github=project.github` to `stage_reddit_candidate()`.
- Dry-run receipts indicate `dry-run://github/{github}/{branch}/{key}` with `status: "staged_to_github"`.
