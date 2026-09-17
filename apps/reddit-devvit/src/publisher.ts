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
  // Convert any markdown image embeds ![alt](url) to clean clickable links
  // because Reddit selftext does not render external image embeds inline.
  let text = candidate.body.replace(
    /!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/g,
    (_, alt, url) => `[🖼️ ${alt.trim() || 'View Illustration'}](${url})`
  );

  // If candidate has an image_url that isn't referenced anywhere in the body, include it as a clean link
  if (candidate.image_url && !text.includes(candidate.image_url)) {
    const link = `\n\n[🖼️ View Illustration / Reference Guide](${candidate.image_url})`;
    if (text.includes('\n\n---')) {
      text = text.replace('\n\n---', `${link}\n\n---`);
    } else {
      text = `${text}${link}`;
    }
  }

  const post = await reddit.submitPost({
    subredditName,
    title: candidate.title,
    text,
  });

  return {
    redditPostId: post.id,
    redditPostUrl: post.url,
  };
}
