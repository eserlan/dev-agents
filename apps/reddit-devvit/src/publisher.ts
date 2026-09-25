import type { RedditAPIClient } from '@devvit/public-api';
import type { CandidatePost } from './types.js';

export interface PublishResult {
  redditPostId: string;
  redditPostUrl?: string;
  /** "link" when the candidate's page was submitted as a link post, else a text post. */
  kind: 'link' | 'text';
  /** For link posts: whether the write-up was added as the first comment. */
  commentPosted: boolean;
}

/**
 * Strip the "posted automatically" footer and the hidden id tag that earlier pipeline versions
 * added to candidate bodies. Reddit escapes raw HTML, so the tag would appear as text.
 */
export function cleanBody(body: string): string {
  return body
    .replace(/<!--\s*id:[A-Za-z0-9_-]+\s*-->/g, '')
    .replace(/\n*-{3,}[ \t]*\n\s*\*Posted automatically[^\n]*\*[ \t]*/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function isHttpUrl(value: string | undefined): value is string {
  return Boolean(value && /^https?:\/\//i.test(value.trim()));
}

/** Remove the "[🖼️ ...](image_url)" line the pipeline appends; a link post shows the page's own preview. */
function withoutImageLink(text: string, imageUrl: string | undefined): string {
  if (!imageUrl) {
    return text;
  }
  const escaped = imageUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return text
    .replace(new RegExp(`\\n*\\[🖼️[^\\]]*\\]\\(${escaped}\\)`, 'g'), '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export async function publishCandidatePost(
  reddit: RedditAPIClient,
  subredditName: string,
  candidate: CandidatePost
): Promise<PublishResult> {
  // Convert any markdown image embeds ![alt](url) to clean clickable links
  // because Reddit selftext does not render external image embeds inline.
  let text = cleanBody(candidate.body).replace(
    /!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/g,
    (_, alt, url) => `[🖼️ ${alt.trim() || 'View Illustration'}](${url})`
  );

  // Preferred: submit the candidate's page as a link post. Reddit renders the page's own
  // preview image (its og:image), so no image has to be uploaded or linked, and the
  // write-up goes in as the first comment. This avoids image hosting restrictions.
  if (candidate.post_type !== 'text' && isHttpUrl(candidate.url)) {
    const post = await reddit.submitPost({
      subredditName,
      title: candidate.title,
      url: candidate.url.trim(),
    });

    // The post now exists, so a failure here must not make the caller retry the candidate
    // (that would submit a duplicate); report it and carry on.
    let commentPosted = false;
    try {
      await reddit.submitComment({ id: post.id, text: withoutImageLink(text, candidate.image_url) });
      commentPosted = true;
    } catch (error) {
      console.error(`Link post ${post.id} created but its comment failed:`, error);
    }

    return {
      redditPostId: post.id,
      redditPostUrl: post.url,
      kind: 'link',
      commentPosted,
    };
  }

  // Text post (requested per candidate, or there is no page to link to). Make sure the page and
  // the image (if any) are reachable as plain links in the body.
  if (isHttpUrl(candidate.url) && !text.includes(candidate.url.trim())) {
    text = `${text}\n\n${candidate.url.trim()}`;
  }
  if (candidate.image_url && !text.includes(candidate.image_url)) {
    text = `${text}\n\n[🖼️ View Illustration / Reference Guide](${candidate.image_url})`;
  }

  const post = await reddit.submitPost({
    subredditName,
    title: candidate.title,
    text,
  });

  return {
    redditPostId: post.id,
    redditPostUrl: post.url,
    kind: 'text',
    commentPosted: false,
  };
}
