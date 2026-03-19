export interface Session {
  session_id: string;
  project: string;
  source: string;
  model: string | null;
  start_time: string | null;
  end_time: string | null;
  duration_seconds: number | null;
  git_branch: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_uses: number;
  input_tokens: number;
  output_tokens: number;
  display_title: string;
  outcome_badge: string | null;
  value_badges: string[];
  risk_badges: string[];
  sensitivity_score: number;
  task_type: string | null;
  files_touched: string[];
  commands_run: string[];
  review_status: string;
  selection_reason: string | null;
  reviewer_notes: string | null;
  reviewed_at: string | null;
  ai_quality_score: number | null;
  ai_score_reason: string | null;
  blob_path: string | null;
  raw_source_path: string | null;
  indexed_at: string;
  updated_at: string | null;
  bundle_id: string | null;
}

export interface SessionDetail extends Session {
  messages: Message[];
}

export interface Message {
  role: 'user' | 'assistant';
  content: string;
  thinking?: string;
  tool_uses?: ToolUse[];
  timestamp?: string;
}

export interface ToolUse {
  tool: string;
  input: Record<string, unknown> | string;
  output: Record<string, unknown> | string;
  status: string;
}

export interface Bundle {
  bundle_id: string;
  created_at: string;
  session_count: number;
  status: string;
  attestation: string | null;
  submission_note: string | null;
  bundle_hash: string | null;
  manifest: Record<string, unknown> | null;
  sessions?: Session[];
}

export interface Policy {
  policy_id: string;
  policy_type: string;
  value: string;
  reason: string | null;
  created_at: string;
}

export interface Stats {
  total: number;
  by_status: Record<string, number>;
  by_source: Record<string, number>;
  by_project: Record<string, number>;
}

export interface ProjectSummary {
  project: string;
  source: string;
  session_count: number;
  total_tokens: number;
}

export type ReviewStatus = 'new' | 'shortlisted' | 'approved' | 'blocked';

export interface DashboardData {
  summary: {
    total_sessions: number;
    total_tokens: number;
    unique_projects: number;
    unique_sources: number;
  };
  activity: { day: string; count: number }[];
  by_outcome_badge: { outcome_badge: string; count: number }[];
  by_value_badge: { badge: string; count: number }[];
  by_risk_badge: { badge: string; count: number }[];
  by_task_type: { task_type: string; count: number }[];
  by_model: { model: string; count: number }[];
  tokens_by_source: { source: string; input_tokens: number; output_tokens: number }[];
}
