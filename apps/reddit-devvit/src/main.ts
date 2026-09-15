import { Devvit } from '@devvit/public-api';
import {
  getQueueStatus,
  getNextApprovedCandidate,
  markPostPublished,
  syncCandidatesFromCdn,
  togglePause,
  MIN_SPACING_MS,
} from './queue.js';
import { publishCandidatePost } from './publisher.js';

const DEFAULT_MANIFEST_URL =
  'https://assets.codexcryptica.com/announcements/reddit-candidates.json';
const SCHEDULER_JOB_NAME = 'reddit_dispatcher';

Devvit.configure({
  redditAPI: true,
  redis: true,
  http: true,
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

export default Devvit;
