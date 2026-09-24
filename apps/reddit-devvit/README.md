# dev-agent-publisher

First-party Reddit companion app for the [`dev-agents`](https://github.com/eserlan/dev-agents) autonomous release pipeline.

## Overview

`dev-agent-publisher` publishes approved release announcements to moderator-controlled subreddits. The `dev-agents` release-comms workflow packages approved Reddit candidates into each app build, so the running app makes no external HTTP requests or background posts:

1. **Release packaging**: After release-comms stages an approved candidate, it writes the candidate manifest into `src/candidates.json`, uploads the Devvit app, and installs the new version on the configured subreddit.
2. **One-click posting**: A moderator chooses **Release: Post Next Approved Candidate**. The app syncs bundled candidates into Redis and publishes the next unposted candidate; no separate enqueue action is needed.
3. **Moderator control**: Reddit submissions happen only after a moderator clicks the post action.
4. **Deduplication**: Tracks seen candidates by unique source ID (`seen:<source_id>`) in Redis to prevent duplicate submissions.
5. **Authentic Engagement**: Formats discussion posts with clear release details, authentic prompts, and standard automated disclosure.

## Moderator Controls

When installed on a subreddit, moderators have access to menu actions directly in the subreddit's moderation tools:

* **Release Queue: Status** — Displays pending, approved, and posted counts, time since last post, and pause status.
* **Release: Post Next Approved Candidate** — Publishes the next bundled candidate immediately (bypassing the 24-hour spacing guard).

## Automated Disclosure

Submissions made by `dev-agent-publisher` include the following standard footer for complete transparency:

```markdown
---
*Posted automatically via release pipeline. Feedback and discussion welcome!*
<!-- id:<source_id> -->
```

## How posts are submitted

When a candidate has a page `url`, the app submits that page as a **link post** and adds the
write-up as the first comment. Reddit shows the page's own preview image (its `og:image`), so no
image is uploaded or linked from the app, which avoids image-hosting restrictions. The
"View Illustration" link that the pipeline adds to the body is dropped from the comment for the
same reason. If the comment fails after the post is created, the failure is logged and the
candidate is still marked posted, so it is never submitted twice.

Candidates with no `url` fall back to a text post, with the image (if any) as a plain link.

Because the `<!-- id:... -->` tag now lives in the comment rather than the post, dev-agents
reconciles link posts by the URL they were submitted with.

## Release-comms integration

Configure the release-comms workflow with the local Devvit app checkout and subreddit:

```yaml
release_comms:
  devvit_app_dir: /path/to/dev-agents/apps/reddit-devvit
  devvit_subreddit: dev_agent_publish_dev
```

The daemon account must be logged in with the Devvit CLI and have moderator permission on the subreddit. Release-comms creates the candidate from its approved GitHub Discussion draft, stages the manifest for audit/history, bundles the candidates, uploads the app, and installs the latest version. The Devvit app itself needs no external-domain permission.

## Architecture

* Built using Reddit's official **Devvit** platform (`@devvit/public-api`).
* Target repositories supply only release copy; all orchestration and staging logic lives in `dev-agents`.
* Requires moderator permissions on target subreddits.
