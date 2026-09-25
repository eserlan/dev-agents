import type { RedditAPIClient, RedisClient } from '@devvit/public-api';
import { publishCandidatePost, type PublishResult } from './publisher.js';
import { REDIS_POST_PREFIX, enqueueCandidate, markPostPublished } from './queue.js';
import type { CandidatePost } from './types.js';

/** At most this many per-candidate buttons are registered, newest first. */
export const MAX_CANDIDATE_BUTTONS = 10;
const LABEL_TITLE_CHARS = 45;

export type ReleaseOutcome =
  | { kind: 'published'; result: PublishResult }
  | { kind: 'already-posted' };

/** Approved candidates, newest first: the order buttons are shown in. */
export function buttonCandidates(candidates: CandidatePost[]): CandidatePost[] {
  return candidates
    .filter((candidate) => candidate.status === 'approved')
    .sort((a, b) => b.created_at - a.created_at)
    .slice(0, MAX_CANDIDATE_BUTTONS);
}

/** Menu labels for the given candidates; identical titles get their id appended so labels stay unique. */
export function buttonLabels(candidates: CandidatePost[]): string[] {
  const short = (title: string) =>
    title.length > LABEL_TITLE_CHARS ? `${title.slice(0, LABEL_TITLE_CHARS - 1).trimEnd()}…` : title;
  const counts = new Map<string, number>();
  for (const candidate of candidates) {
    counts.set(short(candidate.title), (counts.get(short(candidate.title)) ?? 0) + 1);
  }
  return candidates.map((candidate) => {
    const title = short(candidate.title);
    return (counts.get(title) ?? 0) > 1 ? `Post: ${title} (${candidate.source_id})` : `Post: ${title}`;
  });
}

async function alreadyPosted(redis: RedisClient, candidate: CandidatePost): Promise<boolean> {
  const raw = await redis.get(`${REDIS_POST_PREFIX}${candidate.id}`);
  if (!raw) return false;
  try {
    return (JSON.parse(raw) as CandidatePost).status === 'posted';
  } catch {
    return false;
  }
}

/** Post one specific bundled candidate, unless it has already been posted. */
export async function publishBundledCandidate(
  redis: RedisClient,
  reddit: RedditAPIClient,
  subredditName: string,
  candidate: CandidatePost
): Promise<ReleaseOutcome> {
  if (await alreadyPosted(redis, candidate)) {
    return { kind: 'already-posted' };
  }
  await enqueueCandidate(redis, candidate);
  const result = await publishCandidatePost(reddit, subredditName, candidate);
  await markPostPublished(redis, candidate, result.redditPostId);
  return { kind: 'published', result };
}
