# Plan: Podcast episodes for answers (Patreon RSS sync)

Origin: [Codex-Cryptica#4438](https://github.com/eserlan/Codex-Cryptica/issues/4438). The POC
(branch `4438-podcast-sync-poc`) recommended **go**: a static RSS feed on R2 synced into Patreon,
including a public answer-based episode with Gemini narration and rich `content:encoded`, and
an in-place edit under the same GUID.

## Goal

Publish each newly deployed answer page as a narrated episode in a public podcast feed, with
no manual posting. Patreon's podcast sync, set to publish automatically, turns episodes into posts.
GitHub and Codex Cryptica remain the source of truth; Patreon is a downstream channel.

## Decisions

- **Answers only.** Release announcements and blog posts are out of scope for v1.
- **No unreviewed copy.** Narration is `"{question} {shortAnswer} Read the full answer at Codex
  Cryptica."` and the show notes contain the og image, `shortAnswer`, the section headings and a
  link. All of it is answer text that passed PR review, so the merge is the approval and
  `auto_publish` is acceptable.
- **A static feed on R2**, the same setup the POC proved; no Pages Function and no change to the
  production site. Concurrent release runs are serialized by a dev-agents file lock around the
  target command.
- **Receipt semantics:** a receipt means the episode is in the feed. Patreon publication is never
  confirmed (`metadata.patreon = "unconfirmed"`).

## Responsibility split

| dev-agents | Codex Cryptica |
|---|---|
| Detect new answers (existing content-post discovery, no backfill) | `bun run podcast:publish <slug>` |
| Recommend `podcast` when enabled and an answer draft exists | Narration + show notes from the answer config |
| Lock, run the target command, validate its JSON result | TTS (`GeminiTTSService`), ffmpeg, R2 upload |
| Checkpoint the receipt; isolate failures from other channels | Episode index, feed regeneration, HEAD/Range check |

## Target command contract

`bun run podcast:publish <slug>`, run in a detached worktree of the released commit (after
`bun install --frozen-lockfile`) with the daemon's environment (plus `CLOUDFLARE_API_TOKEN` from
the shared checkout's `.env` when missing).

- Final stdout line: `{"guid": str, "audioUrl": "https://assets.codexcryptica.com/...", "status": "created" | "unchanged"}`.
- Exit non-zero on any failure, including a TTS failure. dev-agents then records no receipt.
- **Idempotent across runs:** an already-published slug returns `unchanged`. dev-agents only
  deduplicates within a run.
- GUID: `codex-cryptica:answer:<slug>`, never changed.
- Audio key: `podcast/answers/audio/<slug>-<sha256(narration+voice+model+encoder)[:12]>.mp3`,
  HEAD-checked before calling TTS so retries cost nothing.
- R2 layout: `podcast/answers/feed.xml` (newest 50), `podcast/answers/episodes.json` (index,
  rebuildable from the audio objects and the answer registry), `podcast/answers/audio/`.
- The command resolves `<slug>` from its own checkout. The daemon never updates the shared
  checkout's working tree, which can trail production by hundreds of commits, so dev-agents checks
  out the release commit instead. A missing or unfetchable release commit fails the episode.

## Tasks

### dev-agents (this branch)
- [x] DA-1 `publish_podcast()` in `release_publish.py`: answer-URL guard, file lock, subprocess,
  JSON result validation, dry-run receipt.
- [x] DA-2 Wire into `publish_release_drafts` for answer content posts; `pending_publication_count`;
  channel recommendation in `release_comms.py` only when `podcast` is in `destinations`.
- [x] DA-3 Config: `podcast` off by default; example in `projects.example.yaml`.
- [x] DA-4 Tests: answer-only routing, enabled/disabled, a retry within the run is a no-op, failure
  leaves no receipt, malformed or off-host results rejected, dry run runs nothing.
- [x] DA-5 `docs/release-comms.md` section: contract, receipt semantics, kill switch, manual
  Patreon cleanup.

### Codex Cryptica (separate issues)
- [ ] CC-1 `scripts/podcast/publish.ts <slug>` and a `podcast:publish` package script, promoted from
  the POC's `audio.ts`, `feed.ts`, `upload.ts` and `verify.ts`: input-hash audio keys, HEAD before
  TTS, episode index and feed regeneration, HEAD/Range verification, JSON result line.
- [ ] CC-2 Tests: created, unchanged on rerun, TTS failure exits non-zero, non-public or invalid
  links rejected, a CDATA `]]>` split.

### Ops
- [ ] OPS-1 Connect a **new** Patreon podcast to `podcast/answers/feed.xml` with auto-publish.
  Delete the POC posts and the POC connection, because the POC used different GUIDs
  (`codex-cryptica-release-poc:*`) and two answers would otherwise duplicate.
- [ ] OPS-2 Add `podcast` to `destinations` and watch the first live episode: mobile rendering,
  player, artwork, and whether patrons are notified.

## Kill switch

Remove `podcast` from `destinations` to stop new episodes, or pause sync in Patreon. Already-synced
Patreon posts must be deleted manually.

## Not in v1

Release announcements, draft-mode workflow, confirming Patreon publication, per-tier access,
correcting already-published episodes (a later `--correct` flag on the target command).
