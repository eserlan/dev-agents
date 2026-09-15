export interface CandidatePost {
  id: string;
  title: string;
  body: string;
  url: string;
  image_url?: string;
  source_id: string;
  status: 'pending' | 'approved' | 'posted' | 'rejected';
  created_at: number;
  reddit_post_id?: string;
  posted_at?: number;
}

export interface CandidateManifest {
  updated_at: number;
  candidates: CandidatePost[];
}

export interface QueueStatus {
  pendingCount: number;
  approvedCount: number;
  postedCount: number;
  isPaused: boolean;
  lastPublishedAt: number | null;
  hoursSinceLastPost: number | null;
  canPublishNow: boolean;
}
