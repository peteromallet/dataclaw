# Codex Gaps vs CCHV

## Scope

This note compares Codex handling in:

- DataClaw: `~/dataclaw`
- Claude Code History Viewer (CCHV): `~/claude-code-history-viewer`

The goal is to identify Codex data that CCHV captures more faithfully than DataClaw today.

## Summary

CCHV preserves more of the raw Codex rollout structure than DataClaw.

The biggest current DataClaw gaps are:

1. It ignores `response_item.type == "message"` entries, which means it drops Codex developer/context messages and some wrapper messages.
2. It drops Codex progress/system events such as `task_started`, `task_complete`, `context_compacted`, and `turn_aborted`.
3. It only keeps session-level token totals, while CCHV attaches token usage to individual assistant messages.
4. It exports a smaller session schema and omits some session-list metadata that CCHV computes.

There is no evidence on this machine that Codex is storing separate child-agent/subagent logs outside the rollout JSONL files that DataClaw reads.

## Detailed Findings

### 1. CCHV keeps `response_item.type == "message"`; DataClaw drops them

CCHV converts all Codex `response_item` messages, including `developer`, `user`, and `assistant` roles.

References:

- CCHV message loading loop: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:267-393`
- CCHV message conversion for `response_item.type == "message"`: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:603-620`

DataClaw's Codex parser only handles these `response_item` variants:

- `function_call`
- `custom_tool_call`
- `reasoning`

It does not ingest `response_item.type == "message"` at all.

References:

- DataClaw `handle_response_item(...)`: `~/dataclaw/dataclaw/parsers/codex.py:288-330`

Practical consequence:

- DataClaw drops Codex developer messages and wrapper/context messages that are stored as normal `response_item` messages.

On this machine, real Codex rollout files contain:

- `321` developer `response_item` messages
- `1194` user `response_item` messages
- `2554` assistant `response_item` messages

Example real file with dropped developer and wrapper user messages:

- `~/.codex/sessions/2026/03/28/rollout-2026-03-28T09-51-22-019d3223-8607-7ff3-bf8f-6f7f2ca14fe4.jsonl:3-6`

Important nuance:

- Much of the assistant/user conversation text is duplicated by `event_msg.agent_message` and `event_msg.user_message`, so the biggest practical loss is the extra wrapper/context material rather than ordinary user/assistant chat text.

### 2. CCHV keeps Codex progress/system events; DataClaw drops them

CCHV explicitly converts these Codex event types into messages:

- `task_started`
- `task_complete`
- `context_compacted`
- `turn_aborted`
- `agent_reasoning`
- `agent_message`
- `user_message`

References:

- CCHV Codex event conversion: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:838-1027`

DataClaw only keeps this smaller subset:

- `token_count`
- `agent_reasoning`
- `user_message`
- `agent_message`

References:

- DataClaw event handling: `~/dataclaw/dataclaw/parsers/codex.py:215-230`

Practical consequence:

- DataClaw drops Codex task/progress/system boundaries that CCHV exposes.

Observed in real Codex logs on this machine:

- `task_started`: `62`
- `task_complete`: `43`
- `context_compacted`: `78`
- `turn_aborted`: `99`

Examples:

- `task_started` and later `task_complete` in one real rollout:
  `~/.codex/sessions/2026/03/27/rollout-2026-03-27T14-05-48-019d2de6-1cd3-7cc1-89bc-fc30f7f0314f.jsonl:2,304`
- `turn_aborted` in a real rollout:
  `~/.codex/sessions/2026/03/28/rollout-2026-03-28T09-51-22-019d3223-8607-7ff3-bf8f-6f7f2ca14fe4.jsonl:101`

### 3. CCHV attaches per-message token usage; DataClaw only keeps session totals

CCHV reads Codex `token_count` events, computes deltas, and attaches usage to the last assistant message.

References:

- CCHV token handling in Codex loader: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:336-376`

DataClaw only updates session-level maxima/totals and stores them in `stats`.

References:

- DataClaw token aggregation: `~/dataclaw/dataclaw/parsers/codex.py:332-341`
- DataClaw session result shape: `~/dataclaw/dataclaw/parsers/common.py:56-71`

Practical consequence:

- DataClaw loses per-message token usage that CCHV preserves.

### 4. CCHV keeps more session-list metadata than DataClaw exports

CCHV computes extra Codex session metadata including:

- `file_path`
- `message_count`
- `has_tool_use`
- first-user-message `summary`

References:

- CCHV session info extraction: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:441-560`

DataClaw exports a smaller normalized session schema with only:

- `session_id`
- `model`
- `git_branch`
- `start_time`
- `end_time`
- `messages`
- `stats`

References:

- DataClaw normalized session shape: `~/dataclaw/dataclaw/parsers/common.py:56-71`

Practical consequence:

- DataClaw omits some Codex session-level metadata that CCHV exposes in its session browser/index.

### 5. There is no evidence here of extra Codex subagent logs that CCHV captures and DataClaw misses

DataClaw builds the Codex project index from every session JSONL file under:

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/*.jsonl`

References:

- DataClaw Codex session discovery: `~/dataclaw/dataclaw/parsers/codex.py:74-88`

On this machine:

- `~/.codex/sessions` contains `78` rollout JSONL files
- `~/.codex/state_5.sqlite` contains `78` rows in `threads`
- every `threads.rollout_path` matches one of those JSONL files
- `thread_spawn_edges` has `0` rows
- `agent_jobs` has `0` rows
- `agent_job_items` has `0` rows

Practical consequence:

- There is no extra Codex child-thread/subagent graph on this machine that CCHV captures and DataClaw misses.

This is different from the Claude case, where separate sidechain files existed on disk.

## Important Counterpoint: DataClaw keeps some Codex tool metadata that CCHV normalizes away

This is not a pure one-way comparison.

For Codex tool outputs, DataClaw preserves some parsed metadata such as:

- `exit_code`
- `wall_time`
- `duration_seconds`

References:

- DataClaw Codex tool-result parsing: `~/dataclaw/dataclaw/parsers/codex.py:122-172`

CCHV normalizes tool outputs more aggressively and mainly keeps the extracted payload text.

References:

- CCHV tool output normalization: `~/claude-code-history-viewer/src-tauri/src/providers/codex.rs:1142-1159`

So CCHV has higher Codex message/event fidelity overall, but DataClaw is not strictly worse on every Codex field.

## Bottom Line

Compared with CCHV, DataClaw currently loses more Codex rollout structure around:

- `response_item` messages, especially developer/context wrappers
- progress/system events (`task_started`, `task_complete`, `context_compacted`, `turn_aborted`)
- per-message token usage
- some session-list metadata

But for the real Codex data on this machine, there is no separate Codex subagent/session graph outside the rollout JSONL files, so DataClaw is not missing hidden child-session files in the way Claude previously was.
