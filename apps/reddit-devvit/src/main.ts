import { Devvit } from '@devvit/public-api';
import {
  getQueueStatus,
  getNextApprovedCandidate,
  markPostPublished,
  syncCandidatesFromCdn,
  togglePause,
  MIN_SPACING_MS,
  enqueueCandidate,
} from './queue.js';
import { publishCandidatePost } from './publisher.js';

const DEFAULT_MANIFEST_URL =
  'https://assets.codexcryptica.com/announcements/reddit-candidates.json';
const SCHEDULER_JOB_NAME = 'reddit_dispatcher';

Devvit.configure({
  redditAPI: true,
  redis: true,
  http: {
    domains: ['assets.codexcryptica.com'],
  },
  media: true,
});

// Self-healing scheduler registration on install and upgrade
Devvit.addTrigger({
  event: 'AppInstall',
  onEvent: async (_, context) => {
    try {
      await context.scheduler.runJob({
        cron: '0 * * * *',
        name: SCHEDULER_JOB_NAME,
      });
      console.log('Registered hourly scheduler on AppInstall');
    } catch (err) {
      console.error('Failed to register scheduler on AppInstall:', err);
    }
  },
});

Devvit.addTrigger({
  event: 'AppUpgrade',
  onEvent: async (_, context) => {
    try {
      await context.scheduler.runJob({
        cron: '0 * * * *',
        name: SCHEDULER_JOB_NAME,
      });
      console.log('Registered hourly scheduler on AppUpgrade');
    } catch (err) {
      console.error('Failed to register scheduler on AppUpgrade:', err);
    }
  },
});

// Background dispatcher running once per hour
Devvit.addSchedulerJob({
  name: SCHEDULER_JOB_NAME,
  onRun: async (_, context) => {
    const { redis, reddit } = context;

    const subreddit = await reddit.getCurrentSubreddit();
    const subredditName = subreddit.name;

    // 1. Sync candidates from CDN
    try {
      const syncResult = await syncCandidatesFromCdn(redis, DEFAULT_MANIFEST_URL);
      if (syncResult.added > 0) {
        console.log(`Synced ${syncResult.added} new candidates from CDN`);
      }
    } catch (err) {
      console.error('Error syncing candidates from CDN:', err);
    }

    // 2. Check queue status and cadence gates
    const status = await getQueueStatus(redis);
    if (status.isPaused) {
      console.log('Dispatcher skipped: auto-publishing is paused');
      return;
    }

    if (status.approvedCount === 0) {
      console.log('Dispatcher: queue is empty');
      return;
    }

    if (!status.canPublishNow) {
      console.log(
        `Dispatcher skipped: cadence spacing active (${status.hoursSinceLastPost ?? 0}h since last post, minimum 24h)`
      );
      return;
    }

    // 3. Pop and publish the next approved post
    const candidate = await getNextApprovedCandidate(redis);
    if (!candidate) {
      return;
    }

    try {
      console.log(`Publishing post "${candidate.title}" to r/${subredditName}...`);
      const result = await publishCandidatePost(reddit, subredditName, candidate);
      await markPostPublished(redis, candidate, result.redditPostId);
      console.log(`Successfully published post ${result.redditPostId}`);
    } catch (err) {
      console.error(`Failed to publish post ${candidate.id}:`, err);
    }
  },
});

// Moderator Menu: Queue Status
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Status',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    const status = await getQueueStatus(context.redis);
    const spacingInfo =
      status.hoursSinceLastPost !== null
        ? `${status.hoursSinceLastPost}h ago`
        : 'never';
    const msg = [
      `Queued: ${status.approvedCount}`,
      `Posted: ${status.postedCount}`,
      `Last post: ${spacingInfo}`,
      `Auto-publish: ${status.isPaused ? 'PAUSED' : 'ACTIVE'}`,
      `Ready now: ${status.canPublishNow ? 'YES' : 'NO (cadence lock)'}`,
    ].join(' | ');

    context.ui.showToast(msg);
  },
});

// Moderator Menu: Sync Queue Now
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Sync from CDN Now',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    try {
      const syncResult = await syncCandidatesFromCdn(
        context.redis,
        DEFAULT_MANIFEST_URL
      );
      context.ui.showToast(
        `Sync complete: ${syncResult.added} added, ${syncResult.skipped} already in queue`
      );
    } catch (err) {
      context.ui.showToast(`Sync failed: ${err}`);
    }
  },
});

// Moderator Menu: Publish Next Post Now (Manual Override)
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Publish Next Post Now',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    const candidate = await getNextApprovedCandidate(context.redis);
    if (!candidate) {
      context.ui.showToast('Release queue is empty. Nothing to publish.');
      return;
    }

    try {
      const subreddit = await context.reddit.getCurrentSubreddit();
      context.ui.showToast(`Publishing "${candidate.title}"...`);

      const result = await publishCandidatePost(
        context.reddit,
        subreddit.name,
        candidate
      );
      await markPostPublished(context.redis, candidate, result.redditPostId);

      context.ui.showToast(`Published successfully to r/${subreddit.name}!`);
    } catch (err) {
      context.ui.showToast(`Publishing failed: ${err}`);
    }
  },
});

// Moderator Menu: Toggle Auto-Publish Pause
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Toggle Pause',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    const isPaused = await togglePause(context.redis);
    context.ui.showToast(
      `Auto-publishing is now ${isPaused ? 'PAUSED' : 'ACTIVE'}`
    );
  },
});

// Moderator Menu: Enqueue Discussion #3066 Candidate
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Enqueue Discussion #3066',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    const candidate = {
      id: 'reddit-34789183506-share-any-generator-result-as-a-public-l',
      title: 'Share any generator result as a public link with one-click Remix',
      body: 'You roll an NPC or a location you actually want to use, then you copy the text into Discord and the formatting falls apart. If someone wants to tweak one detail they have to paste it back into the generator and guess your inputs.\n\nI built shareable generator results: any output can become a public link with a human-readable slug that anyone can view and Remix straight back into the generator to make it their own.\n\nTry it from any generator: https://codexcryptica.com/generators\n\n- Works for NPCs, locations, encounters, factions, items, quests and the rest, not just one generator\n- Viewer gets a clean rendered page, no account needed to open it\n- Remix drops the content back into the generator with the prompt and options prefilled so you can reroll a variant\n- Link uses a readable slug like `/share/whispering-cold-tavern-...` instead of a random ID wall\n\nTradeoff I am still sitting with: once you share, the link is public to anyone with the URL. You can revoke it from My Stuff, but there is no private share or password yet. I kept it simple to ship, and I am not sure if private links are worth the extra friction.\n\nWhat is the last generator result you copy-pasted somewhere and wished you could just link instead?\n\n![A tabletop roleplaying illustration for https://codexcryptica.com/generators](https://assets.codexcryptica.com/announcements/release-34789183506-3.png)\n\n---\n*Posted automatically via release pipeline. Feedback and discussion welcome!*\n<!-- id:34789183506 -->',
      url: 'https://codexcryptica.com/generators',
      image_url: 'https://assets.codexcryptica.com/announcements/release-34789183506-3.png',
      source_id: '34789183506',
      status: 'approved' as const,
      created_at: Math.floor(Date.now() / 1000),
    };

    const added = await enqueueCandidate(context.redis, candidate, true);
    if (added) {
      context.ui.showToast('Discussion #3066 enqueued to release queue!');
    } else {
      context.ui.showToast('Discussion #3066 was already enqueued or seen.');
    }
  },
});

export default Devvit;
