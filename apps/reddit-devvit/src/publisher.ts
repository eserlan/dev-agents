import type { RedditAPIClient } from '@devvit/public-api';
import type { CandidatePost } from './types.js';

export interface PublishResult {
  redditPostId: string;
  redditPostUrl?: string;
}

export async function publishCandidatePost(
  reddit: RedditAPIClient,
  subredditName: string,
  candidate: CandidatePost
): Promise<PublishResult> {
  const post = await reddit.submitPost({
    subredditName,
    title: candidate.title,
    text: candidate.body,
  });

  return {
    redditPostId: post.id,
    redditPostUrl: post.url,
  };
}
