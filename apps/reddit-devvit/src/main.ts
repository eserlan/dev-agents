import { Devvit } from '@devvit/public-api';
import bundledManifest from './candidates.json' with { type: 'json' };
import {
  getQueueStatus,
  markPostPublished,
  syncCandidatesFromBundle,
  REDIS_POST_PREFIX,
} from './queue.js';
import { buttonCandidates, buttonLabels, publishBundledCandidate } from './release.js';
import type { CandidatePost } from './types.js';

const BUNDLED_CANDIDATES = bundledManifest.candidates as CandidatePost[];
const SCHEDULER_JOB_NAME = 'reddit_dispatcher';

Devvit.configure({
  redditAPI: true,
  redis: true,
  media: true,
});

async function nextUnpostedCandidate(
  redis: Parameters<typeof getQueueStatus>[0]
): Promise<CandidatePost | null> {
  const candidates = [...BUNDLED_CANDIDATES]
    .filter((candidate) => candidate.status === 'approved')
    .sort((a, b) => b.created_at - a.created_at);

  for (const candidate of candidates) {
    const raw = await redis.get(`${REDIS_POST_PREFIX}${candidate.id}`);
    if (!raw) {
      return candidate;
    }
    try {
      if ((JSON.parse(raw) as CandidatePost).status !== 'posted') {
        return candidate;
      }
    } catch {
      return candidate;
    }
  }
  return null;
}

// Keep queue counts current, but only a moderator action can submit a post.
Devvit.addTrigger({
  event: 'AppInstall',
  onEvent: async (_, context) => {
    await context.scheduler.runJob({ cron: '0 * * * *', name: SCHEDULER_JOB_NAME });
  },
});

Devvit.addTrigger({
  event: 'AppUpgrade',
  onEvent: async (_, context) => {
    await context.scheduler.runJob({ cron: '0 * * * *', name: SCHEDULER_JOB_NAME });
  },
});

Devvit.addSchedulerJob({
  name: SCHEDULER_JOB_NAME,
  onRun: async (_, context) => {
    await syncCandidatesFromBundle(context.redis, BUNDLED_CANDIDATES);
  },
});

// Moderator Menu: Queue Status
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release Queue: Status',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    await syncCandidatesFromBundle(context.redis, BUNDLED_CANDIDATES);
    const status = await getQueueStatus(context.redis);
    const spacingInfo =
      status.hoursSinceLastPost !== null
        ? `${status.hoursSinceLastPost}h ago`
        : 'never';
    const msg = [
      `Queued: ${status.approvedCount}`,
      `Posted: ${status.postedCount}`,
      `Last post: ${spacingInfo}`,
      `Mode: moderator click`,
      `Ready now: ${status.approvedCount > 0 ? 'YES' : 'NO'}`,
    ].join(' | ');

    context.ui.showToast(msg);
  },
});

// Moderator Menu: Publish the next bundled release candidate
Devvit.addMenuItem({
  location: 'subreddit',
  label: 'Release: Post Next Approved Candidate',
  forUserType: 'moderator',
  onPress: async (_, context) => {
    await syncCandidatesFromBundle(context.redis, BUNDLED_CANDIDATES);
    const candidate = await nextUnpostedCandidate(context.redis);
    if (!candidate) {
      context.ui.showToast('No unposted approved release candidate is available.');
      return;
    }
    await postCandidate(context, candidate);
  },
});

async function postCandidate(
  context: Devvit.Context,
  candidate: CandidatePost
): Promise<void> {
  try {
    const subreddit = await context.reddit.getCurrentSubreddit();
    context.ui.showToast(`Publishing "${candidate.title}"...`);
    const outcome = await publishBundledCandidate(
      context.redis,
      context.reddit,
      subreddit.name,
      candidate
    );
    context.ui.showToast(
      outcome.kind === 'already-posted'
        ? `"${candidate.title}" was already posted.`
        : `Published successfully to r/${subreddit.name}!`
    );
  } catch (err) {
    context.ui.showToast(`Publishing failed: ${err}`);
  }
}

// One button per bundled release, so each can be posted on its own and in any order.
// The bundle is baked into the app build, so these are registered when the app is built.
const releaseButtons = buttonCandidates(BUNDLED_CANDIDATES);
const releaseLabels = buttonLabels(releaseButtons);
releaseButtons.forEach((candidate, index) => {
  Devvit.addMenuItem({
    location: 'subreddit',
    label: releaseLabels[index],
    forUserType: 'moderator',
    onPress: async (_, context) => {
      await postCandidate(context, candidate);
    },
  });
});

export default Devvit;
