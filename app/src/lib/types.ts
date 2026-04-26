export type JsonObject = Record<string, unknown>;

export type DataClawConfig = JsonObject & {
  repo?: string | null;
  source?: string | null;
  redact_strings?: string[];
  redact_usernames?: string[];
  excluded_projects?: string[];
  exclude?: string[];
  redact?: string[];
  default_bucket?: string | null;
  folder_rules?: {
    default_bucket?: string | null;
  };
  privacy_filter?: {
    enabled?: boolean;
    device?: "auto" | "cpu" | "mps" | string;
  };
  app?: {
    launch_at_login?: boolean;
    sync_enabled?: boolean;
    sync_interval_hours?: number;
    last_scheduled_sync_at?: number;
    next_scheduled_sync_at?: number;
    last_scheduled_sync_error?: string;
  };
};

export type Project = {
  name: string;
  sessions: number;
  size: string;
  excluded: boolean;
  source: string;
  bucket: string | null;
  tags: string[];
};

export type HfToken = string | null;

export type HfWhoami = JsonObject & {
  ok?: boolean;
  user?: string | null;
};

export const SOURCE_CHOICES = [
  "all",
  "claude",
  "codex",
  "gemini",
  "opencode",
  "openclaw",
  "kimi",
  "hermes",
  "custom"
] as const;
