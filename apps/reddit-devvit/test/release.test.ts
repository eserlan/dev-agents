import { describe, expect, test } from 'bun:test';
import {
  MAX_CANDIDATE_BUTTONS,
  buttonCandidates,
  buttonLabels,
  publishBundledCandidate,
} from '../src/release.js';
import type { CandidatePost } from '../src/types.js';
import { FakeReddit, FakeRedis } from './fakes.js';

function candidate(n: number, overrides: Partial<CandidatePost> = {}): CandidatePost {
  return {
    id: `reddit-pr-${n}`,
    title: `Release ${n}`,
    body: `Body ${n}\n\n---\n*Posted automatically*`,
    url: `https://codexcryptica.com/page-${n}`,
    source_id: `pr-${n}`,
    status: 'approved',
    created_at: 1000 + n,
    ...overrides,
  };
}

const asReddit = (fake: FakeReddit) => fake as unknown as Parameters<typeof publishBundledCandidate>[1];
const asRedis = (fake: FakeRedis) => fake as unknown as Parameters<typeof publishBundledCandidate>[0];

describe('release buttons', () => {
  test('are newest first, approved only, and capped', () => {
    const many = Array.from({ length: MAX_CANDIDATE_BUTTONS + 5 }, (_, i) => candidate(i));
    many.push(candidate(99, { status: 'pending', created_at: 99999 }));

    const shown = buttonCandidates(many);

    expect(shown).toHaveLength(MAX_CANDIDATE_BUTTONS);
    expect(shown[0].id).toBe(`reddit-pr-${MAX_CANDIDATE_BUTTONS + 4}`);
    expect(shown.some((c) => c.status !== 'approved')).toBe(false);
  });

  test('labels are short and unique', () => {
    const long = candidate(1, { title: 'A very long release title that keeps going well past the label limit' });
    const dupA = candidate(2, { title: 'Same title' });
    const dupB = candidate(3, { title: 'Same title' });

    const labels = buttonLabels([long, dupA, dupB]);

    expect(labels[0].length).toBeLessThanOrEqual('Post: '.length + 45);
    expect(labels[0].endsWith('…')).toBe(true);
    expect(labels[1]).toBe('Post: Same title (pr-2)');
    expect(labels[2]).toBe('Post: Same title (pr-3)');
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe('publishBundledCandidate', () => {
  test('posts each candidate on its own, in any order, once', async () => {
    const redis = new FakeRedis();
    const reddit = new FakeReddit();
    const [a, b, c] = [candidate(1), candidate(2), candidate(3)];

    // Click the middle one first, then the oldest.
    expect((await publishBundledCandidate(asRedis(redis), asReddit(reddit), 'test', b)).kind).toBe('published');
    expect((await publishBundledCandidate(asRedis(redis), asReddit(reddit), 'test', a)).kind).toBe('published');

    expect(reddit.posts.map((post) => post.url)).toEqual([b.url, a.url]);

    // Clicking a posted one again does nothing; the untouched one is still available.
    expect((await publishBundledCandidate(asRedis(redis), asReddit(reddit), 'test', b)).kind).toBe('already-posted');
    expect(reddit.posts).toHaveLength(2);
    expect((await publishBundledCandidate(asRedis(redis), asReddit(reddit), 'test', c)).kind).toBe('published');
    expect(reddit.posts).toHaveLength(3);
  });

  test('a failed submission is not marked posted, so it can be retried', async () => {
    const redis = new FakeRedis();
    const failing = { submitPost: async () => { throw new Error('rate limited'); } } as unknown as FakeReddit;
    const target = candidate(1);

    await expect(publishBundledCandidate(asRedis(redis), asReddit(failing), 'test', target)).rejects.toThrow('rate limited');

    const reddit = new FakeReddit();
    expect((await publishBundledCandidate(asRedis(redis), asReddit(reddit), 'test', target)).kind).toBe('published');
  });
});
