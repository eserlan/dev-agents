import { describe, expect, test } from 'bun:test';
import { cleanBody, publishCandidatePost } from '../src/publisher.js';
import {
  REDIS_POST_PREFIX,
  enqueueCandidate,
  getNextApprovedCandidate,
  markPostPublished,
  syncCandidatesFromBundle,
} from '../src/queue.js';
import type { CandidatePost } from '../src/types.js';
import { FakeReddit, FakeRedis } from './fakes.js';

const PAGE = 'https://codexcryptica.com/answers/how-do-you-run-a-chase-in-a-tabletop-rpg';
const IMAGE = 'https://assets.codexcryptica.com/og/how-do-you-run-a-chase-in-a-tabletop-rpg.jpg';

function candidate(overrides: Partial<CandidatePost> = {}): CandidatePost {
  return {
    id: 'reddit-pr-1-chase',
    title: 'How do you run a chase in a tabletop RPG?',
    body:
      `I wrote up a framework: ${PAGE}\n\n` +
      `[🖼️ View Chase Flowchart](${IMAGE})\n\n---\n*Posted automatically*\n<!-- id:pr-1 -->`,
    url: PAGE,
    image_url: IMAGE,
    source_id: 'pr-1',
    status: 'approved',
    created_at: 1000,
    ...overrides,
  };
}

// The Devvit client types are much larger than what the publisher uses.
const asReddit = (fake: FakeReddit) => fake as unknown as Parameters<typeof publishCandidatePost>[0];
const asRedis = (fake: FakeRedis) => fake as unknown as Parameters<typeof enqueueCandidate>[0];

describe('publishCandidatePost', () => {
  test('submits the page as a link post and the write-up as a comment without the image link', async () => {
    const reddit = new FakeReddit();
    const result = await publishCandidatePost(asReddit(reddit), 'test', candidate());

    expect(result.kind).toBe('link');
    expect(result.commentPosted).toBe(true);
    expect(reddit.posts).toEqual([{ subredditName: 'test', title: 'How do you run a chase in a tabletop RPG?', url: PAGE }]);
    expect(reddit.posts[0]).not.toHaveProperty('text');
    expect(reddit.comments).toHaveLength(1);
    expect(reddit.comments[0].id).toBe(result.redditPostId);
    expect(reddit.comments[0].text).toContain('I wrote up a framework');
    expect(reddit.comments[0].text).not.toContain('<!-- id:');
    expect(reddit.comments[0].text).not.toContain('Posted automatically');
    expect(reddit.comments[0].text).not.toContain(IMAGE);
    expect(reddit.comments[0].text).not.toContain('🖼️');
    expect(reddit.comments[0].text).not.toMatch(/\n{3,}/);
  });

  test('a failing comment does not throw, so the queue cannot re-submit the post', async () => {
    const reddit = new FakeReddit();
    reddit.failComments = true;

    const result = await publishCandidatePost(asReddit(reddit), 'test', candidate());

    expect(result.kind).toBe('link');
    expect(result.commentPosted).toBe(false);
    expect(reddit.posts).toHaveLength(1);
  });

  test('falls back to a text post with the image as a plain link when there is no page URL', async () => {
    const reddit = new FakeReddit();
    const result = await publishCandidatePost(
      asReddit(reddit),
      'test',
      candidate({ url: '', body: 'Just some text\n\n---\n*Posted automatically*' })
    );

    expect(result.kind).toBe('text');
    expect(reddit.comments).toHaveLength(0);
    expect(reddit.posts[0]).not.toHaveProperty('url');
    const text = String(reddit.posts[0].text);
    expect(text).toBe(`Just some text\n\n[🖼️ View Illustration / Reference Guide](${IMAGE})`);
  });

  test('post_type "text" posts the write-up as the body with the page and image as links, and no comment', async () => {
    const reddit = new FakeReddit();
    const body = `Browse them here: ${PAGE}\n\n---\n*Posted automatically*`;

    const result = await publishCandidatePost(
      asReddit(reddit),
      'test',
      candidate({ post_type: 'text', body })
    );

    expect(result.kind).toBe('text');
    expect(reddit.comments).toHaveLength(0);
    expect(reddit.posts[0]).not.toHaveProperty('url');
    const text = String(reddit.posts[0].text);
    // The page link was already in the body, so it is not repeated; the image link follows it.
    expect(text.split(PAGE).length - 1).toBe(1);
    expect(text).toBe(`Browse them here: ${PAGE}\n\n[🖼️ View Illustration / Reference Guide](${IMAGE})`);
  });

  test('post_type "text" adds the page link when the body does not contain it', async () => {
    const reddit = new FakeReddit();
    await publishCandidatePost(
      asReddit(reddit),
      'test',
      candidate({ post_type: 'text', image_url: undefined, body: 'No link here\n\n---\n*Posted automatically*' })
    );
    expect(String(reddit.posts[0].text)).toBe(`No link here\n\n${PAGE}`);
  });

  test('a non-http url is treated as no page', async () => {
    const reddit = new FakeReddit();
    const result = await publishCandidatePost(asReddit(reddit), 'test', candidate({ url: 'not-a-url' }));
    expect(result.kind).toBe('text');
  });

  test('image embeds in the body become clickable links in a text post', async () => {
    const reddit = new FakeReddit();
    await publishCandidatePost(
      asReddit(reddit),
      'test',
      candidate({ url: '', image_url: undefined, body: `Look: ![map](${IMAGE})` })
    );
    expect(String(reddit.posts[0].text)).toBe(`Look: [🖼️ map](${IMAGE})`);
  });
});

describe('cleanBody', () => {
  test('removes the automatic footer and the hidden id tag, however they were staged', () => {
    const staged = 'Hello there\n\nSecond paragraph\n\n---\n*Posted automatically via release pipeline. Feedback and discussion welcome!*\n<!-- id:discussion-3358 -->';
    expect(cleanBody(staged)).toBe('Hello there\n\nSecond paragraph');

    const older = 'Body\n\n---\n*Posted automatically via Codex Cryptica\'s release pipeline. Feedback welcome!*\n<!-- id:pr-3093 -->';
    expect(cleanBody(older)).toBe('Body');
  });

  test('leaves ordinary horizontal rules and text alone', () => {
    const body = 'Part one\n\n---\n\nPart two';
    expect(cleanBody(body)).toBe(body);
  });

  test('published posts never contain the footer or the id tag', async () => {
    const reddit = new FakeReddit();
    await publishCandidatePost(asReddit(reddit), 'test', candidate({ post_type: 'text' }));
    const text = String(reddit.posts[0].text);
    expect(text).not.toContain('Posted automatically');
    expect(text).not.toContain('<!-- id:');
  });
});

describe('bundled release queue', () => {
  test('sync enqueues only approved candidates and is idempotent', async () => {
    const redis = new FakeRedis();
    const bundle = [
      candidate(),
      candidate({ id: 'reddit-pr-2', source_id: 'pr-2', status: 'pending' }),
      candidate({ id: 'reddit-pr-3', source_id: 'pr-3', created_at: 2000 }),
    ];

    expect(await syncCandidatesFromBundle(asRedis(redis), bundle)).toEqual({ added: 2, skipped: 1, total: 3 });
    expect(await syncCandidatesFromBundle(asRedis(redis), bundle)).toEqual({ added: 0, skipped: 3, total: 3 });
  });

  test('publishing marks the candidate posted and it is never posted twice', async () => {
    const redis = new FakeRedis();
    const reddit = new FakeReddit();
    const bundle = [candidate()];

    await syncCandidatesFromBundle(asRedis(redis), bundle);
    const next = await getNextApprovedCandidate(asRedis(redis));
    expect(next?.id).toBe('reddit-pr-1-chase');

    const result = await publishCandidatePost(asReddit(reddit), 'test', next!);
    await markPostPublished(asRedis(redis), next!, result.redditPostId);

    const stored = JSON.parse((await redis.get(`${REDIS_POST_PREFIX}reddit-pr-1-chase`))!);
    expect(stored.status).toBe('posted');
    expect(stored.reddit_post_id).toBe(result.redditPostId);

    // A later menu press re-syncs the same bundle and must find nothing new to post.
    await syncCandidatesFromBundle(asRedis(redis), bundle);
    expect(await getNextApprovedCandidate(asRedis(redis))).toBeNull();
    expect(reddit.posts).toHaveLength(1);
  });
});
