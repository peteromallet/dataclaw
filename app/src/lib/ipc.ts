import { invoke } from "@tauri-apps/api/core";
import type { DataClawConfig, HfToken, HfWhoami, JsonObject, Project } from "./types";

export function dataclawStatus(): Promise<JsonObject> {
  return invoke("dataclaw_status");
}

export function dataclawAutoNow(force = false): Promise<JsonObject> {
  return invoke("dataclaw_auto_now", { force });
}

export function dataclawConfigGet(options: { showSecrets?: boolean } = {}): Promise<DataClawConfig> {
  return invoke("dataclaw_config_get", { showSecrets: Boolean(options.showSecrets) });
}

export type ConfigSetArgs = {
  repo?: string;
  source?: string;
  set_redact?: string;
  set_redact_usernames?: string;
  set_excluded?: string;
  confirm_projects?: boolean;
  default_bucket?: string;
  privacy_filter?: boolean;
  privacy_filter_device?: string;
  launch_at_login?: boolean;
  sync_enabled?: boolean;
  sync_interval_hours?: number;
};

export function dataclawConfigSet(args: ConfigSetArgs): Promise<DataClawConfig> {
  return invoke("dataclaw_config_set", { args });
}

export function dataclawListProjects(source = "all"): Promise<Project[]> {
  return invoke("dataclaw_list_projects", { source });
}

export function hfSaveToken(token: string): Promise<void> {
  return invoke("hf_save_token", { token });
}

export function hfLoadToken(): Promise<HfToken> {
  return invoke("hf_load_token");
}

export function hfDeleteToken(): Promise<void> {
  return invoke("hf_delete_token");
}

export function hfWhoami(): Promise<HfWhoami> {
  return invoke("hf_whoami");
}

export function logsOpenInFinder(): Promise<void> {
  return invoke("logs_open_in_finder");
}

export function logsTail(lines = 200): Promise<string[]> {
  return invoke("logs_tail", { lines });
}
