import { useEffect, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { open as openUrl } from "@tauri-apps/plugin-shell";
import UpdateChecker from "../components/UpdateChecker";
import { dataclawAutoNow, dataclawStatus, hfWhoami, logsTail } from "../lib/ipc";
import type { JsonObject } from "../lib/types";

type ProgressState = {
  runId: string;
  stageKey: string;
  stageLabel: string;
  stageIndex: number;
  totalStages: number;
  detail: string;
  percent: number | null;
  metrics: Array<{ label: string; value: string }>;
  nextStages: string[];
  status: "active" | "blocked" | "failed" | "complete";
  startedAtMs: number;
  updatedAtMs: number;
  eventCount: number;
};

const STAGES = [
  { key: "start", label: "Start" },
  { key: "gate", label: "Checks" },
  { key: "discover", label: "Projects" },
  { key: "export", label: "Export" },
  { key: "mechanical_pii", label: "PII scan" },
  { key: "model_privacy", label: "Model privacy" },
  { key: "confirm", label: "Confirm" },
  { key: "push", label: "Upload" },
  { key: "finish", label: "Finish" }
];

const STALL_AFTER_MS = 120_000;

function display(value: unknown, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function pick(status: JsonObject | null, keys: string[]) {
  if (!status) return undefined;
  for (const key of keys) {
    if (key in status) return status[key];
  }
  return undefined;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function sidecarPayload(message: string): JsonObject | null {
  const match = message.match(/:\s*(\{[\s\S]*\})\s*\nstderr:/);
  if (!match) return null;
  try {
    return JSON.parse(match[1]) as JsonObject;
  } catch {
    return null;
  }
}

function friendlyRunError(message: string) {
  const payload = sidecarPayload(message);
  const error = asString(payload?.error);
  if (!error) return message;
  const hint = asString(payload?.hint);
  return hint ? `${error} ${hint}` : error;
}

function progressFromRunError(message: string, startedAtMs: number): ProgressState {
  const payload = sidecarPayload(message);
  const now = Date.now();
  const blockedOn = asString(payload?.blocked_on_step);
  const nextCommand = asString(payload?.next_command);
  const isBlocked = Boolean(blockedOn) || message.includes("requires a freshly confirmed review") || message.includes("dataclaw confirm");
  const detail = friendlyRunError(message);
  const metrics = [
    metric(isBlocked ? "Blocked on" : "Error", blockedOn ?? "Run Now"),
    metric("Next command", nextCommand)
  ].filter((item): item is { label: string; value: string } => Boolean(item));

  return {
    runId: "",
    stageKey: isBlocked ? "review" : "finish",
    stageLabel: isBlocked ? "Review required" : "Failed",
    stageIndex: isBlocked ? 4 : STAGES.length - 1,
    totalStages: STAGES.length,
    detail,
    percent: null,
    metrics,
    nextStages: isBlocked ? ["Confirm", "Upload"] : [],
    status: isBlocked ? "blocked" : "failed",
    startedAtMs,
    updatedAtMs: now,
    eventCount: 1
  };
}

function asObject(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : null;
}

function asNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown) {
  return typeof value === "string" ? value : null;
}

function formatTime(ms: number) {
  return new Date(ms).toLocaleTimeString();
}

function formatDuration(ms: number) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}

function formatBytes(value: unknown) {
  const bytes = asNumber(value);
  if (bytes === null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatCount(value: unknown) {
  const count = asNumber(value);
  return count === null ? null : count.toLocaleString();
}

function compactPath(value: unknown) {
  const text = asString(value);
  if (!text) return null;
  const parts = text.split("/");
  return parts.slice(-2).join("/");
}

function ratio(index: unknown, total: unknown) {
  const current = asNumber(index);
  const max = asNumber(total);
  if (!current || !max || max <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((current / max) * 100)));
}

function metric(label: string, value: unknown, fallback = "") {
  const text = display(value, fallback);
  return text ? { label, value: text } : null;
}

function metricPair(label: string, current: unknown, total: unknown) {
  const currentText = display(current, "");
  const totalText = display(total, "");
  if (!currentText || !totalText) return null;
  return { label, value: `${currentText}/${totalText}` };
}

function formatLastRun(value: unknown) {
  const obj = asObject(value);
  if (!obj) return display(value);
  const result = asString(obj.result) ?? "unknown";
  const timestamp = asString(obj.timestamp);
  const bits = [result];
  const totalSessions = asNumber(obj.total_sessions_new);
  if (totalSessions !== null) bits.push(`${totalSessions} staged session${totalSessions === 1 ? "" : "s"}`);
  const sources = Array.isArray(obj.sources) ? obj.sources.filter((item) => typeof item === "string") : [];
  if (sources.length) bits.push(`sources: ${sources.join(", ")}`);
  const privacyFindings = asNumber(obj.privacy_findings);
  if (privacyFindings !== null) bits.push(`${privacyFindings} privacy finding${privacyFindings === 1 ? "" : "s"}`);
  const warnings = Array.isArray(obj.warnings) ? obj.warnings.length : null;
  if (warnings) bits.push(`${warnings} warning${warnings === 1 ? "" : "s"}`);
  if (timestamp) bits.push(new Date(timestamp).toLocaleString());
  return bits.join(" · ");
}

function datasetUrl(repo: unknown) {
  const text = asString(repo);
  return text ? `https://huggingface.co/datasets/${text}` : null;
}

function resultIcon(result: unknown) {
  if (result === "pushed" || result === "success" || result === "ready") return "✓";
  if (result === "error" || result === "blocked" || result === "failed") return "!";
  return "•";
}

function resultClass(result: unknown) {
  if (result === "pushed" || result === "success" || result === "ready") return "done";
  if (result === "error" || result === "blocked" || result === "failed") return "error";
  return "";
}

function stageIndexFor(key: string) {
  const index = STAGES.findIndex((stage) => stage.key === key);
  return index >= 0 ? index : 0;
}

function stageForMessage(msg: string, phase: string) {
  if (msg.startsWith("mechanical_pii_")) return "mechanical_pii";
  if (msg.startsWith("privacy_filter_")) return "model_privacy";
  if (msg.startsWith("token_count_")) return "export";
  if (msg.startsWith("export_")) return "export";
  if (msg.startsWith("push_")) return "push";
  if (msg.startsWith("auto_confirm_")) return "confirm";
  if (msg.startsWith("resolve_export_inputs_")) return "discover";
  if (msg === "auto_gate_checked" || phase === "gate") return "gate";
  if (msg === "auto_run_started" || phase === "start") return "start";
  if (msg.startsWith("auto_blocked_") || msg === "auto_noop" || msg === "auto_dry_run_finished" || phase === "finish") return "finish";
  return phase && STAGES.some((stage) => stage.key === phase) ? phase : "start";
}

function initialProgress(startedAtMs = Date.now()): ProgressState {
  return {
    runId: "",
    stageKey: "start",
    stageLabel: "Start",
    stageIndex: 0,
    totalStages: STAGES.length,
    detail: "Starting Run Now",
    percent: null,
    metrics: [],
    nextStages: STAGES.slice(1, 4).map((stage) => stage.label),
    status: "active",
    startedAtMs,
    updatedAtMs: startedAtMs,
    eventCount: 0
  };
}

function logTimestampMs(raw: string) {
  try {
    const row = JSON.parse(raw) as JsonObject;
    const ts = asString(row.ts);
    const parsed = Date.parse(ts ?? "");
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function isFreshLogLine(raw: string, sinceMs: number | null) {
  if (!sinceMs) return true;
  const ts = logTimestampMs(raw);
  return ts === null || ts >= sinceMs - 1_000;
}

function formatProgressLine(raw: string, previous: ProgressState | null): ProgressState | null {
  let row: JsonObject;
  try {
    row = JSON.parse(raw) as JsonObject;
  } catch {
    return null;
  }

  const msg = asString(row.msg ?? row.message);
  if (!msg) return null;
  const extra = asObject(row.extra) ?? {};
  const runId = asString(row.run_id) ?? asString(extra.run_id) ?? "";
  const phase = asString(row.phase) ?? asString(extra.phase) ?? "run";
  const stageKey = stageForMessage(msg, phase);
  const stageIndex = stageIndexFor(stageKey);
  const stageLabel = STAGES[stageIndex]?.label ?? stageKey.replace(/_/g, " ");
  const updatedAtMs = logTimestampMs(raw) ?? Date.now();
  const sameRun = !runId || !previous?.runId || previous.runId === runId;
  const startedAtMs = sameRun && previous ? previous.startedAtMs : updatedAtMs;
  const eventCount = sameRun && previous ? previous.eventCount + 1 : 1;
  let detail = msg.replace(/_/g, " ");
  let percent: number | null = null;
  let status: ProgressState["status"] = "active";
  const metrics: Array<{ label: string; value: string } | null> = [];

  switch (msg) {
    case "auto_run_started":
      detail = "Starting auto run";
      metrics.push(metric("Mode", extra.dry_run ? "dry run" : "publish"));
      metrics.push(metric("Source", extra.source));
      break;
    case "auto_gate_checked":
      detail = "Checked repo, token, source, and privacy policy";
      metrics.push(metric("Privacy", extra.privacy_filter_enabled ? "on" : "off"));
      metrics.push(metric("Policy", extra.policy));
      break;
    case "auto_confirm_started":
      detail = "Confirming automated review";
      metrics.push(metric("File", compactPath(extra.file) ?? extra.file));
      break;
    case "auto_confirm_finished":
      detail = "Automated review confirmed";
      percent = 100;
      metrics.push(metric("Sessions", extra.total_sessions, "0"));
      metrics.push(metric("File size", extra.file_size));
      break;
    case "resolve_export_inputs_started":
      detail = "Loading configured project selection";
      metrics.push(metric("Excluded", extra.excluded_projects, "0"));
      metrics.push(metric("Redactions", extra.redact_strings, "0"));
      break;
    case "resolve_export_inputs_finished":
      detail = "Project selection loaded";
      metrics.push(metric("Included", extra.included_projects, "0"));
      metrics.push(metric("Custom redactions", extra.custom_redactions, "0"));
      percent = 100;
      break;
    case "export_shards_started":
      detail = "Preparing export shards";
      metrics.push(metric("Projects", extra.included_projects, "0"));
      metrics.push(metric("Incremental", extra.incremental ? "yes" : "no"));
      break;
    case "export_project_started":
      detail = `Reading ${display(extra.project)}`;
      percent = ratio(extra.index, extra.total_projects);
      metrics.push(metricPair("Project", extra.index, extra.total_projects));
      metrics.push(metric("Source", extra.source));
      break;
    case "export_project_parsed":
      detail = `Parsed ${display(extra.project)}`;
      percent = ratio(extra.index, extra.total_projects);
      metrics.push(metric("Sessions parsed", extra.sessions_parsed, "0"));
      metrics.push(metricPair("Project", extra.index, extra.total_projects));
      break;
    case "export_fetch_existing_started":
      detail = `Fetching existing shard ${display(extra.path)}`;
      percent = ratio(extra.index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.index, extra.total_shards));
      metrics.push(metric("Fetch existing", extra.fetch_existing ? "yes" : "no"));
      break;
    case "export_fetch_existing_finished":
      detail = `Merged existing shard ${display(extra.path)}`;
      percent = ratio(extra.index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.index, extra.total_shards));
      metrics.push(metric("Merge", extra.merge_source));
      break;
    case "export_write_progress":
      detail = "Writing redacted shard rows";
      percent = ratio(extra.session_index, extra.total_sessions_parsed);
      metrics.push(metricPair("Sessions", extra.session_index, extra.total_sessions_parsed));
      metrics.push(metric("Shards seen", extra.shards_seen, "0"));
      break;
    case "export_shards_finished":
      detail = "Export staging complete";
      percent = 100;
      metrics.push(metric("New sessions", extra.total_sessions_new, "0"));
      metrics.push(metric("Shards", extra.shard_count, "0"));
      break;
    case "token_count_started":
      detail = "Counting tokens for dataset card";
      metrics.push(metric("Method", extra.method ?? extra.tokenizer ?? "byte estimate"));
      metrics.push(metric("Shards", extra.total_shards ?? extra.shards, "0"));
      break;
    case "token_count_progress":
      detail = `Counting tokens in ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.index, extra.total_shards));
      metrics.push(metric("JSONL tokens", formatCount(extra.jsonl_tokens ?? extra.jsonl_tokens_estimate)));
      metrics.push(metric("Content tokens", formatCount(extra.content_tokens ?? extra.content_tokens_estimate)));
      break;
    case "token_count_finished":
      detail = "Token count complete";
      percent = 100;
      metrics.push(metric("JSONL tokens", formatCount(extra.jsonl_tokens)));
      metrics.push(metric("Content tokens", formatCount(extra.content_tokens)));
      metrics.push(metric("Messages", formatCount(extra.messages)));
      break;
    case "mechanical_pii_scan_started":
      detail = "Mechanical PII scan started";
      metrics.push(metric("Policy", extra.policy));
      break;
    case "mechanical_pii_shard_started":
      detail = `Mechanical scan ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.index, extra.total_shards));
      metrics.push(metric("Size", formatBytes(extra.size_bytes)));
      break;
    case "mechanical_pii_shard_finished":
      detail = `Mechanical shard finished ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.index, extra.total_shards);
      metrics.push(metric("Findings", extra.total_findings ?? extra.finding_types, "0"));
      metrics.push(metricPair("Shard", extra.index, extra.total_shards));
      break;
    case "mechanical_pii_scan_finished":
      detail = "Mechanical PII scan finished";
      percent = 100;
      metrics.push(metric("Finding types", extra.finding_type_count, "0"));
      break;
    case "mechanical_pii_redaction_started":
      detail = "Redacting mechanical PII findings";
      metrics.push(metric("Findings", extra.total_findings, "0"));
      metrics.push(metric("Types", extra.finding_type_count, "0"));
      break;
    case "mechanical_pii_redaction_finished":
      detail = "Mechanical PII redaction complete";
      percent = 100;
      metrics.push(metric("Files changed", extra.files_changed, "0"));
      metrics.push(metric("Redactions", extra.redactions, "0"));
      break;
    case "mechanical_pii_rescan_started":
      detail = "Rescanning mechanically redacted output";
      metrics.push(metric("Policy", extra.policy));
      break;
    case "mechanical_pii_rescan_finished":
      detail = "Mechanical PII rescan finished";
      percent = 100;
      metrics.push(metric("Remaining findings", extra.total_findings, "0"));
      break;
    case "final_mechanical_pii_scan_started":
      detail = "Final mechanical PII scan started";
      metrics.push(metric("Policy", extra.policy));
      break;
    case "final_mechanical_pii_scan_finished":
      detail = "Final mechanical PII scan finished";
      percent = 100;
      metrics.push(metric("Remaining findings", extra.total_findings, "0"));
      break;
    case "privacy_filter_started":
      detail = "Model privacy filter gate started";
      metrics.push(metric("Policy", extra.policy));
      metrics.push(metric("Mode", extra.force ? "forced" : "strict"));
      break;
    case "privacy_filter_skipped":
      detail = "Model privacy filter skipped";
      metrics.push(metric("Reason", String(extra.reason || "disabled")));
      metrics.push(metric("Mechanical PII", String(extra.mechanical_pii_gate || "enforced")));
      break;
    case "privacy_filter_model_load_started":
      detail = "Loading privacy model";
      metrics.push(metric("Device", extra.device));
      metrics.push(metric("Dtype", extra.dtype));
      break;
    case "privacy_filter_model_load_finished":
      detail = "Privacy model loaded";
      metrics.push(metric("Device", extra.device));
      metrics.push(metric("Dtype", extra.dtype));
      break;
    case "privacy_filter_scan_started":
      detail = "Loading model privacy scanner";
      metrics.push(metric("Sessions", extra.total_sessions_new, "0"));
      metrics.push(metric("Shards", extra.shard_count, "0"));
      metrics.push(metric("Device", extra.device));
      break;
    case "privacy_filter_shard_started":
      detail = `Scanning ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      break;
    case "privacy_filter_shard_progress":
      detail = `Scanning ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      metrics.push(metric("Sessions scanned", extra.sessions_scanned, "0"));
      metrics.push(metric("Findings", extra.findings, "0"));
      break;
    case "privacy_filter_session_started":
      detail = `Scanning session ${display(extra.session_id)}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      metrics.push(metric("Sessions scanned", extra.sessions_scanned, "0"));
      metrics.push(metric("Messages", extra.message_count, "0"));
      metrics.push(metric("Chars", extra.char_count, "0"));
      break;
    case "privacy_filter_session_size_guard_redacted":
      detail = `Redacted oversized session ${display(extra.session_id)}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      metrics.push(metric("Chars", extra.char_count, "0"));
      metrics.push(metric("Strings", extra.string_count, "0"));
      break;
    case "privacy_filter_text_started":
      detail = `Scanning ${display(extra.field)} text`;
      metrics.push(metric("Session", extra.session_id));
      metrics.push(metric("Chunks", extra.chunk_count, "0"));
      metrics.push(metric("Chars", extra.char_count, "0"));
      break;
    case "privacy_filter_text_progress":
      detail = `Scanning ${display(extra.field)}`;
      percent = ratio(extra.chunk_index, extra.chunk_count);
      metrics.push(metricPair("Chunk", extra.chunk_index, extra.chunk_count));
      metrics.push(metric("Session", extra.session_id));
      metrics.push(metric("Findings", extra.findings, "0"));
      break;
    case "privacy_filter_text_finished":
      detail = `Finished ${display(extra.field)} text`;
      percent = 100;
      metrics.push(metric("Chunks", extra.chunk_count, "0"));
      metrics.push(metric("Findings", extra.findings, "0"));
      break;
    case "privacy_filter_session_finished":
      detail = `Session scan finished ${display(extra.session_id)}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      metrics.push(metric("Sessions scanned", extra.sessions_scanned, "0"));
      metrics.push(metric("Findings", extra.findings, "0"));
      break;
    case "privacy_filter_shard_finished":
      detail = `Shard scan finished ${compactPath(extra.path) ?? "shard"}`;
      percent = ratio(extra.shard_index, extra.total_shards);
      metrics.push(metricPair("Shard", extra.shard_index, extra.total_shards));
      metrics.push(metric("Sessions scanned", extra.sessions_scanned, "0"));
      metrics.push(metric("Findings", extra.findings, "0"));
      break;
    case "privacy_filter_scan_completed":
      detail = "Model privacy scan complete";
      percent = 100;
      metrics.push(metric("New findings", extra.new_findings, "0"));
      metrics.push(metric("Known", extra.known_findings, "0"));
      break;
    case "privacy_filter_finished":
      detail = "Privacy gates passed";
      percent = 100;
      metrics.push(metric("New findings", extra.new_findings, "0"));
      break;
    case "auto_blocked_mechanical_pii_findings":
      detail = "Blocked by mechanical PII findings";
      status = "blocked";
      metrics.push(metric("Finding types", extra.finding_types));
      break;
    case "auto_blocked_privacy_filter_failed":
      detail = "Blocked because privacy filter failed";
      status = "failed";
      metrics.push(metric("Error", extra.error));
      break;
    case "auto_blocked_privacy_findings":
      detail = "Blocked by model privacy findings";
      status = "blocked";
      metrics.push(metric("Findings", extra.privacy_findings, "0"));
      break;
    case "push_started":
      detail = "Uploading filtered shards";
      metrics.push(metric("Sessions", extra.total_sessions_new, "0"));
      metrics.push(metric("Shards", extra.shard_count, "0"));
      break;
    case "push_attempt_started":
      detail = `Upload attempt ${display(extra.attempt)} started`;
      metrics.push(metricPair("Attempt", extra.attempt, extra.max_attempts));
      metrics.push(metric("Shards", extra.shard_count, "0"));
      metrics.push(metric("Sessions", extra.total_sessions_new, "0"));
      break;
    case "push_retry_wait":
      detail = `Waiting ${display(extra.wait_seconds)}s before retry`;
      metrics.push(metric("Attempt", extra.attempt));
      metrics.push(metric("Reason", extra.reason));
      break;
    case "push_success":
      detail = "Upload succeeded";
      metrics.push(metric("Attempt", extra.attempt));
      metrics.push(metric("Backoff", `${display(extra.backoff_seconds_total, "0")}s`));
      break;
    case "push_failed":
      detail = "Upload failed";
      status = "failed";
      metrics.push(metric("Attempts", extra.attempts));
      metrics.push(metric("Error", extra.error));
      break;
    case "push_finished":
      detail = "Upload complete";
      metrics.push(metric("Repo", extra.repo_url));
      percent = 100;
      status = "complete";
      break;
    case "config_saved_after_push":
      detail = "Saved successful run state";
      percent = 100;
      status = "complete";
      metrics.push(metric("Repo", extra.repo_url));
      metrics.push(metric("Push attempts", extra.push_attempts));
      break;
    case "auto_noop":
      detail = "No new sessions to upload";
      percent = 100;
      status = "complete";
      break;
    case "auto_dry_run_finished":
      detail = "Dry run finished";
      percent = 100;
      status = "complete";
      break;
    case "run_summary_written":
      detail = "Run summary written";
      percent = 100;
      if (extra.result === "error" || extra.result === "failed") {
        status = "failed";
      } else if (extra.result === "blocked") {
        status = "blocked";
      } else if (extra.result) {
        status = "complete";
      }
      metrics.push(metric("Result", extra.result));
      metrics.push(metric("Repo", extra.repo_url ?? extra.repo));
      break;
    default:
      if (!phase || !["export", "privacy_filter", "push", "gate", "discover", "staging", "finish", "start"].includes(phase)) {
        return null;
      }
      metrics.push(metric("Event", msg));
  }

  const nextStages = STAGES.slice(stageIndex + 1, stageIndex + 4).map((stage) => stage.label);

  return {
    runId,
    stageKey,
    stageLabel,
    stageIndex,
    totalStages: STAGES.length,
    detail,
    percent,
    metrics: metrics.filter((item): item is { label: string; value: string } => Boolean(item)),
    nextStages,
    status,
    startedAtMs,
    updatedAtMs,
    eventCount
  };
}

export default function Dashboard() {
  const [status, setStatus] = useState<JsonObject | null>(null);
  const [hfUser, setHfUser] = useState<string | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [runMessage, setRunMessage] = useState("");
  const [progress, setProgress] = useState<ProgressState | null>(null);
  const [nowMs, setNowMs] = useState(Date.now());
  const seenLogLines = useRef<Set<string>>(new Set());
  const activeRunRef = useRef(false);
  const runInFlightRef = useRef(false);
  const runStartedAtRef = useRef<number | null>(null);

  async function ingestLogTail(lines = 400, sinceMs = runStartedAtRef.current) {
    try {
      const rows = await logsTail(lines);
      setProgress((current) => {
        let next = current;
        for (const row of rows) {
          if (!isFreshLogLine(row, sinceMs)) continue;
          if (seenLogLines.current.has(row)) continue;
          seenLogLines.current.add(row);
          next = formatProgressLine(row, next) ?? next;
        }
        return next;
      });
    } catch {
      // The live run itself is more important than surfacing log-tail errors here.
    }
  }

  async function refreshStatus(options: { clearIdleProgress?: boolean } = {}) {
    const clearIdleProgress = options.clearIdleProgress ?? true;
    setLoading(true);
    setError("");
    try {
      const [nextStatus, whoami] = await Promise.all([
        dataclawStatus(),
        hfWhoami().catch(() => null)
      ]);
      setStatus(nextStatus);
      setAuthChecked(true);
      setHfUser(whoami && typeof whoami.user === "string" ? whoami.user : null);
      activeRunRef.current = Boolean(nextStatus.active_auto_run) || runInFlightRef.current;
      if (!activeRunRef.current && clearIdleProgress) {
        setProgress(null);
        seenLogLines.current.clear();
      } else {
        void ingestLogTail(400, runStartedAtRef.current);
      }
    } catch (err) {
      setError(errorMessage(err));
      setProgress((current) => current ? { ...current, status: "failed", detail: "Run Now failed", updatedAtMs: Date.now() } : current);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshStatus();
    let unlistenTray: (() => void) | undefined;
    let unlistenLogs: (() => void) | undefined;
    listen("tray-run-now", () => {
      void runNow();
    }).then((fn) => {
      unlistenTray = fn;
    });
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    const logPoll = window.setInterval(() => {
      if (runInFlightRef.current || activeRunRef.current) void ingestLogTail(400, runStartedAtRef.current);
    }, 2000);
    listen<string>("logs-line", (event) => {
      if (!runInFlightRef.current && !activeRunRef.current) return;
      if (!isFreshLogLine(event.payload, runStartedAtRef.current)) return;
      seenLogLines.current.add(event.payload);
      setProgress((current) => formatProgressLine(event.payload, current) ?? current);
    }).then((fn) => {
      unlistenLogs = fn;
    });
    return () => {
      window.clearInterval(timer);
      window.clearInterval(logPoll);
      if (unlistenTray) unlistenTray();
      if (unlistenLogs) unlistenLogs();
    };
  }, []);

  async function runNow() {
    const startedAtMs = Date.now();
    setRunning(true);
    setError("");
    setRunMessage("");
    seenLogLines.current.clear();
    runStartedAtRef.current = startedAtMs;
    runInFlightRef.current = true;
    activeRunRef.current = true;
    setProgress(initialProgress(startedAtMs));
    try {
      const result = await dataclawAutoNow(false);
      setStatus(result);
      const summary = pick(result, ["result", "stage"]);
      setRunMessage(typeof summary === "string" ? `Result: ${summary}` : "Run complete.");
    } catch (err) {
      const rawMessage = errorMessage(err);
      const message = friendlyRunError(rawMessage);
      setError(message);
      setProgress((current) => current ? progressFromRunError(rawMessage, current.startedAtMs) : progressFromRunError(rawMessage, startedAtMs));
    } finally {
      runInFlightRef.current = false;
      activeRunRef.current = false;
      setRunning(false);
      await refreshStatus({ clearIdleProgress: false });
      runStartedAtRef.current = null;
    }
  }

  const setupStage = pick(status, ["stage"]);
  const lastDatasetUpdate = asObject(pick(status, ["last_dataset_update"]));
  const lastAutoRun = asObject(pick(status, ["last_auto_run"]));
  const uploadResult = asString(lastAutoRun?.result) ?? (lastDatasetUpdate ? "pushed" : "ready");
  const currentRepo = pick(status, ["repo", "repo_url", "dataset"]);
  const currentRepoText = asString(currentRepo);
  const uploadedRepo = asString(lastDatasetUpdate?.repo);
  const uploadedRepoMatchesCurrent = Boolean(currentRepoText && uploadedRepo && currentRepoText === uploadedRepo);
  const sessions =
    pick(status, ["sessions", "session_count", "count"]) ??
    (uploadedRepoMatchesCurrent ? lastDatasetUpdate?.total_sessions_in_shards ?? lastDatasetUpdate?.total_sessions_new : undefined);
  const lastAuto = pick(status, ["last_auto_run", "time", "last_export"]);
  const hint = pick(status, ["hint", "next_command", "next_steps"]);
  const repoUrl = datasetUrl(currentRepo);
  const uploadedRepoUrl = datasetUrl(uploadedRepo);
  const isError = uploadResult === "error" || uploadResult === "blocked" || uploadResult === "failed";
  const lastProgressAgeMs = progress ? nowMs - progress.updatedAtMs : 0;
  const runIsActive = running || activeRunRef.current;
  const isStalled = Boolean(runIsActive && progress && lastProgressAgeMs > STALL_AFTER_MS);
  const progressStatus = isStalled ? "stalled" : progress?.status ?? "active";
  const elapsedMs = progress ? nowMs - progress.startedAtMs : 0;
  const blockedRun = lastAutoRun?.result === "blocked";
  const lastUploadSessions = lastDatasetUpdate?.total_sessions_in_shards ?? lastDatasetUpdate?.total_sessions_new;

  return (
    <section className="page">
      <h1>Dashboard</h1>

      <div className="card">
        <h2>Quick status</h2>
        {loading ? (
          <p>
            <span className="spinner" /> Loading…
          </p>
        ) : (
          <dl className="status-grid">
            <dt>Result</dt>
            <dd style={isError ? { color: "var(--danger)" } : undefined}>
              <span className={`result-icon ${resultClass(uploadResult)}`}>
                {resultIcon(uploadResult)}
              </span>
              {display(uploadResult)}
            </dd>
            <dt>Setup</dt>
            <dd>{display(setupStage)}</dd>
            <dt>HF user</dt>
            <dd>{display(hfUser)}</dd>
            <dt>Repo</dt>
            <dd>
              {display(currentRepo)}
              {repoUrl ? (
                <button
                  type="button"
                  className="inline-link-icon"
                  onClick={() => void openUrl(repoUrl)}
                  aria-label="Open Hugging Face dataset"
                  title="Open Hugging Face dataset"
                >
                  ↗
                </button>
              ) : null}
            </dd>
            {uploadedRepo && !uploadedRepoMatchesCurrent ? (
              <>
                <dt>Last uploaded repo</dt>
                <dd>
                  {display(uploadedRepo)}
                  {uploadedRepoUrl ? (
                    <button
                      type="button"
                      className="inline-link-icon"
                      onClick={() => void openUrl(uploadedRepoUrl)}
                      aria-label="Open last uploaded Hugging Face dataset"
                      title="Open last uploaded Hugging Face dataset"
                    >
                      ↗
                    </button>
                  ) : null}
                </dd>
              </>
            ) : null}
            {sessions ? (
              <>
                <dt>Sessions</dt>
                <dd>{display(sessions)}</dd>
              </>
            ) : null}
            {lastUploadSessions && (!uploadedRepoMatchesCurrent || !sessions) ? (
              <>
                <dt>Last upload sessions</dt>
                <dd>{display(lastUploadSessions)}</dd>
              </>
            ) : null}
            <dt>Last run</dt>
            <dd>{formatLastRun(lastAuto)}</dd>
            {hint ? (
              <>
                <dt>Hint</dt>
                <dd>{display(hint)}</dd>
              </>
            ) : null}
          </dl>
        )}
        {error ? <div className="error">{error}</div> : null}
        {!loading && authChecked && !hfUser ? (
          <div className="warning">
            Hugging Face auth is required before DataClaw can upload. Open Auth and save a token.
          </div>
        ) : null}
        {!loading && blockedRun ? (
          <div className="warning">
            Last run was blocked before upload, so DataClaw did not create or update the configured Hugging Face repo.
          </div>
        ) : null}
        {runMessage ? <div className="notice">{runMessage}</div> : null}
        {progress ? (
          <div className={`progress-panel ${progressStatus}`} aria-live="polite">
            <div className="progress-heading">
              <span>Run progress</span>
              <span>{runIsActive ? "Active" : progress.status === "complete" ? "Complete" : "Latest"}</span>
            </div>
            <div className="progress-stage-row">
              <div>
                <div className="progress-stage">
                  Stage {progress.stageIndex + 1}/{progress.totalStages}: {progress.stageLabel}
                </div>
                <div className="progress-detail">{progress.detail}</div>
              </div>
              {progress.percent !== null ? <div className="progress-percent">{progress.percent}%</div> : null}
            </div>
            <div
              className={`progress-meter ${progress.percent === null ? "unknown" : ""}`}
              aria-label={progress.percent === null ? "Run stage progress unknown" : "Run stage progress"}
            >
              <span style={progress.percent === null ? undefined : { width: `${progress.percent}%` }} />
            </div>
            {progress.metrics.length ? (
              <dl className="progress-metrics">
                {progress.metrics.map((item) => (
                  <div key={`${item.label}:${item.value}`}>
                    <dt>{item.label}</dt>
                    <dd>{item.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            <div className="progress-meta">
              <span>Started {formatTime(progress.startedAtMs)}</span>
              <span>Last event {formatTime(progress.updatedAtMs)} ({formatDuration(lastProgressAgeMs)} ago)</span>
              <span>{progress.eventCount} events</span>
              <span>Elapsed {formatDuration(elapsedMs)}</span>
              {progress.runId ? <span>Run {progress.runId.slice(0, 12)}</span> : null}
            </div>
            {progress.nextStages.length ? <div className="progress-next">Next: {progress.nextStages.join(" → ")}</div> : null}
            {isStalled ? (
              <div className="progress-stalled">
                No log events for {formatDuration(lastProgressAgeMs)}. The sidecar may still be working on a long model-redaction step.
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="actions">
          <button type="button" className="primary" onClick={() => void runNow()} disabled={runIsActive}>
            {runIsActive ? (
              <>
                <span className="spinner" />
                Running…
              </>
            ) : (
              "Run now"
            )}
          </button>
          <button type="button" onClick={() => void refreshStatus()} disabled={loading || runIsActive}>
            Refresh
          </button>
        </div>
      </div>

      <UpdateChecker />
    </section>
  );
}
