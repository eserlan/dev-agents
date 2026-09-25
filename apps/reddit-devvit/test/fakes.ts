// Minimal in-memory stand-ins for the Devvit Redis and Reddit clients.
type Scored = { member: string; score: number };

export class FakeRedis {
  private kv = new Map<string, string>();
  private zsets = new Map<string, Scored[]>();

  async get(key: string) {
    return this.kv.get(key);
  }
  async set(key: string, value: string) {
    this.kv.set(key, value);
  }
  async zAdd(key: string, ...members: Scored[]) {
    const set = this.zsets.get(key) ?? [];
    for (const item of members) {
      const existing = set.find((entry) => entry.member === item.member);
      if (existing) existing.score = item.score;
      else set.push({ ...item });
    }
    this.zsets.set(key, set);
  }
  async zRem(key: string, members: string[]) {
    this.zsets.set(key, (this.zsets.get(key) ?? []).filter((e) => !members.includes(e.member)));
  }
  async zRange(key: string, start: number, stop: number) {
    const sorted = [...(this.zsets.get(key) ?? [])].sort((a, b) => a.score - b.score);
    return sorted.slice(start, stop === -1 ? undefined : stop + 1);
  }
  async zCard(key: string) {
    return (this.zsets.get(key) ?? []).length;
  }
}

export class FakeReddit {
  posts: Array<Record<string, unknown>> = [];
  comments: Array<{ id: string; text: string }> = [];
  failComments = false;

  async submitPost(options: Record<string, unknown>) {
    this.posts.push(options);
    const id = `t3_post${this.posts.length}`;
    return { id, url: `https://www.reddit.com/r/test/comments/${id}/` };
  }
  async submitComment(options: { id: string; text: string }) {
    if (this.failComments) throw new Error('comment rejected');
    this.comments.push(options);
    return { id: `t1_comment${this.comments.length}` };
  }
}
