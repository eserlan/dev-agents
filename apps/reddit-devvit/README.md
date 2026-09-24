# dev-agent-publisher

First-party Reddit companion app for the [`dev-agents`](https://github.com/eserlan/dev-agents) autonomous release pipeline.

## Overview

`dev-agent-publisher` automates community announcements and release discussions on moderator-controlled subreddits. It operates as a pull-based companion to the `dev-agents` release pipeline:

1. **Pull Ingestion**: Fetches approved post candidates from the release staging CDN (`announcements/reddit-candidates.json`).
2. **Spam & Flooding Protection**: Enforces a strict minimum 24-hour cadence spacing between consecutive submissions.
3. **Deduplication**: Tracks seen candidates by unique source ID (`seen:<source_id>`) in Redis to prevent duplicate submissions.
4. **Authentic Engagement**: Formats discussion posts with clear release details, authentic prompts, and standard automated disclosure.

## Moderator Controls

When installed on a subreddit, moderators have access to menu actions directly in the subreddit's moderation tools:

* **Publisher: Queue Status** — Displays pending, approved, and posted counts, time since last post, and pause status.
* **Publisher: Sync from CDN Now** — Immediately triggers an ingestion sync from the candidate manifest without waiting for the hourly cron.
* **Publisher: Publish Next Post Now** — Publishes the next approved candidate immediately (bypassing the 24-hour spacing guard).
* **Publisher: Toggle Pause** — Emergency pause switch that halts all automated publishing.

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

## Fetch Domains

* `raw.githubusercontent.com`: Used by the companion app to pull approved announcement candidate JSON manifests (`announcements/reddit-candidates.json`) staged by the release pipeline for automated community publication.

## Architecture

* Built using Reddit's official **Devvit** platform (`@devvit/public-api`).
* Target repositories supply only release copy; all orchestration and staging logic lives in `dev-agents`.
* Requires moderator permissions on target subreddits.
