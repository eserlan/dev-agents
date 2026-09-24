import type { RedisClient } from '@devvit/public-api';
import type { CandidatePost, QueueStatus } from './types.js';

export const REDIS_POST_PREFIX = 'reddit:post:';
export const REDIS_QUEUE_APPROVED = 'reddit:queue:approved';
export const REDIS_QUEUE_POSTED = 'reddit:queue:posted';
export const REDIS_SEEN_PREFIX = 'reddit:seen:';
export const REDIS_CONFIG_PAUSED = 'reddit:config:paused';
export const REDIS_LAST_PUBLISHED = 'reddit:last_published_at';

export const MIN_SPACING_MS = 24 * 60 * 60 * 1000; // 24 hours

export async function enqueueCandidate(
  redis: RedisClient,
  post: CandidatePost,
  force: boolean = false
): Promise<boolean> {
  const seenKey = `${REDIS_SEEN_PREFIX}${post.source_id}`;
  if (!force) {
    const alreadySeen = await redis.get(seenKey);
    if (alreadySeen) {
      return false;
    }
  }

  const postKey = `${REDIS_POST_PREFIX}${post.id}`;
  await redis.set(postKey, JSON.stringify(post));
  await redis.zAdd(REDIS_QUEUE_APPROVED, {
    member: post.id,
    score: post.created_at,
  });
  await redis.set(seenKey, '1');
  return true;
}

export async function getNextApprovedCandidate(
  redis: RedisClient
): Promise<CandidatePost | null> {
  const items = await redis.zRange(REDIS_QUEUE_APPROVED, 0, 0);
  if (!items || items.length === 0) {
    return null;
  }

  const postId = items[0].member;
  const raw = await redis.get(`${REDIS_POST_PREFIX}${postId}`);
  if (!raw) {
    // If the post data is missing, clean up the queue entry
    await redis.zRem(REDIS_QUEUE_APPROVED, [postId]);
    return null;
  }

  try {
    return JSON.parse(raw) as CandidatePost;
  } catch {
    return null;
  }
}

export async function markPostPublished(
  redis: RedisClient,
  post: CandidatePost,
  redditPostId: string
): Promise<void> {
  const now = Date.now();
  await redis.zRem(REDIS_QUEUE_APPROVED, [post.id]);

  const updated: CandidatePost = {
    ...post,
    status: 'posted',
    reddit_post_id: redditPostId,
    posted_at: now,
  };

  await redis.set(`${REDIS_POST_PREFIX}${post.id}`, JSON.stringify(updated));
  await redis.zAdd(REDIS_QUEUE_POSTED, {
    member: post.id,
    score: now,
  });
  await redis.set(REDIS_LAST_PUBLISHED, now.toString());
}

export async function getQueueStatus(redis: RedisClient): Promise<QueueStatus> {
  const approvedCount = await redis.zCard(REDIS_QUEUE_APPROVED);
  const postedCount = await redis.zCard(REDIS_QUEUE_POSTED);
  const isPaused = (await redis.get(REDIS_CONFIG_PAUSED)) === 'true';

  const rawLastPublished = await redis.get(REDIS_LAST_PUBLISHED);
  const lastPublishedAt = rawLastPublished ? parseInt(rawLastPublished, 10) : null;

  const now = Date.now();
  const hoursSinceLastPost = lastPublishedAt
    ? Math.round((now - lastPublishedAt) / (1000 * 60 * 60) * 10) / 10
    : null;

  const spacingSatisfied =
    lastPublishedAt === null || now - lastPublishedAt >= MIN_SPACING_MS;
  const canPublishNow = !isPaused && approvedCount > 0 && spacingSatisfied;

  return {
    pendingCount: 0,
    approvedCount,
    postedCount,
    isPaused,
    lastPublishedAt,
    hoursSinceLastPost,
    canPublishNow,
  };
}

export async function togglePause(redis: RedisClient): Promise<boolean> {
  const current = (await redis.get(REDIS_CONFIG_PAUSED)) === 'true';
  const next = !current;
  await redis.set(REDIS_CONFIG_PAUSED, next ? 'true' : 'false');
  return next;
}

export async function syncCandidatesFromBundle(
  redis: RedisClient,
  candidates: CandidatePost[]
): Promise<{ added: number; skipped: number; total: number }> {
  let added = 0;
  let skipped = 0;

  for (const candidate of candidates) {
    if (candidate.status !== 'approved') {
      skipped++;
      continue;
    }
    const wasAdded = await enqueueCandidate(redis, candidate);
    if (wasAdded) {
      added++;
    } else {
      skipped++;
    }
  }

  return { added, skipped, total: candidates.length };
}
