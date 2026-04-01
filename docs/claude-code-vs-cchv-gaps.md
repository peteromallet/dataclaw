# Claude Code Gaps vs CCHV

## Scope

This note compares Claude Code handling in:

- DataClaw: `~/dataclaw`
- Claude Code History Viewer (CCHV): `~/claude-code-history-viewer`

The goal is to identify Claude Code data that CCHV captures more faithfully than DataClaw today.

## Summary

CCHV retains substantially more Claude-log structure than DataClaw.

The biggest current DataClaw gaps are:

1. It flattens the raw message graph into a simplified conversation schema.
2. It reduces tool results to plain text plus a success/error flag.
3. It drops Claude `summary` entries and rename-derived session metadata.
4. It drops per-message metadata such as `uuid`, `parentUuid`, `stop_reason`, and raw `usage`.
5. It merges all Claude subagent files in one session directory into a single synthetic `:subagents` export entry, while CCHV keeps per-file session granularity.

## Detailed Findings

### 1. CCHV preserves raw Claude message identity and threading; DataClaw does not

CCHV's Claude message model keeps raw message identity and graph fields such as:

- `uuid`
- `parentUuid`
- raw `sessionId`
- `isSidechain`

References:

- CCHV raw/model fields: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:24-47`
- CCHV exported message fields: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:95-143`

By contrast, DataClaw exports a simplified session object with only:

- `session_id`
- `model`
- `git_branch`
- `start_time`
- `end_time`
- `messages`
- `stats`

References:

- DataClaw session result shape: `~/dataclaw/dataclaw/parsers/common.py:54-69`
- DataClaw Claude parser only converts `user` and `assistant` entries into simplified messages: `~/dataclaw/dataclaw/parsers/claude.py:222-265`

Practical consequence:

- DataClaw loses the original message DAG / parent-child chain and raw entry identity.

### 2. CCHV keeps raw `toolUse` / `toolUseResult`; DataClaw flattens them heavily

CCHV stores raw Claude tool structures directly:

- `toolUse`
- `toolUseResult`

References:

- CCHV raw tool fields: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:40-67`
- CCHV exported tool fields: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:108-143`

DataClaw instead:

- extracts assistant `tool_use` blocks
- parses tool input into anonymized structured fields
- converts tool results into only `output.text` and `status`

References:

- DataClaw tool-result map flattening: `~/dataclaw/dataclaw/parsers/claude.py:83-107`
- DataClaw assistant tool export shape: `~/dataclaw/dataclaw/parsers/claude.py:279-328`

Practical consequence:

- DataClaw drops structured tool-result details such as separate `stdout`/`stderr`, edit payloads, file objects, image results, and other nested result metadata.

### 3. CCHV keeps per-message metadata; DataClaw mostly aggregates or drops it

CCHV keeps per-message metadata including:

- `usage`
- `stop_reason`
- `costUSD`
- `durationMs`

References:

- CCHV message content metadata: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:13-21`
- CCHV message-level exported metadata: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:117-127`

DataClaw only aggregates selected token counts into session stats and does not export message-level `usage` or `stop_reason`.

References:

- DataClaw token aggregation: `~/dataclaw/dataclaw/parsers/claude.py:251-260`
- DataClaw exported session shape has only session-level `stats`: `~/dataclaw/dataclaw/parsers/common.py:54-69`

Practical consequence:

- DataClaw loses per-message token usage and stop metadata that CCHV preserves.

### 4. CCHV captures Claude `summary` entries and rename-derived session names; DataClaw drops them

CCHV explicitly reads Claude `summary` entries into session metadata and also extracts rename information from `system/local_command` messages.

References:

- Summary ingestion during metadata extraction: `~/claude-code-history-viewer/src-tauri/src/commands/session/load.rs:318-323`
- Summary fallback in phase-2 scanning: `~/claude-code-history-viewer/src-tauri/src/commands/session/load.rs:445-455`
- Rename extraction from system/local command content: `~/claude-code-history-viewer/src-tauri/src/commands/session/load.rs:593-619`
- Session metadata fields that expose this: `~/claude-code-history-viewer/src-tauri/src/models/session.rs:51-72`

DataClaw's Claude parser only processes `user` and `assistant` entries, so Claude `summary` entries are currently ignored.

References:

- DataClaw only handles `user` / `assistant`: `~/dataclaw/dataclaw/parsers/claude.py:241-265`

Practical consequence:

- DataClaw loses Claude-generated session summaries and rename-derived display names that CCHV keeps.

### 5. CCHV keeps subagent files as distinct sessions; DataClaw merges them per session directory

CCHV recursively scans all JSONL files under the project path and builds session metadata per file path.

References:

- Recursive JSONL discovery: `~/claude-code-history-viewer/src-tauri/src/commands/session/load.rs:806-811`
- CCHV session metadata distinguishes file-path identity from raw session ID via `session_id` vs `actual_session_id`: `~/claude-code-history-viewer/src-tauri/src/models/session.rs:51-72`

DataClaw now exports Claude subagents, but it merges all `subagents/agent-*.jsonl` files in a session directory into one synthetic export session and suffixes the session ID with `:subagents` when a root session also exists.

References:

- DataClaw subagent discovery: `~/dataclaw/dataclaw/parsers/claude.py:147-162`
- DataClaw subagent merge behavior: `~/dataclaw/dataclaw/parsers/claude.py:165-218`

Practical consequence:

- DataClaw preserves more than before, but still loses per-subagent-file granularity that CCHV retains.

### 6. CCHV preserves raw `message.content` JSON blocks; DataClaw converts them into a narrow schema

CCHV preserves raw `message.content` JSON for loaded messages.

References:

- CCHV content model: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:13-21`
- CCHV message content field: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:104-105`

DataClaw instead extracts only:

- user text
- assistant text
- assistant thinking text
- assistant tool uses

References:

- DataClaw user-content extraction: `~/dataclaw/dataclaw/parsers/claude.py:268-276`
- DataClaw assistant-content extraction: `~/dataclaw/dataclaw/parsers/claude.py:279-328`

Practical consequence:

- DataClaw drops raw Claude content-block fidelity, including structured user `tool_result` content and other non-text block details.

### 7. CCHV has partial support for more Claude event types than DataClaw, but its viewer still filters some of them

CCHV's raw model knows about additional Claude fields and event payloads for:

- `summary`
- `system`
- `progress`
- `file-history-snapshot`
- `queue-operation`

References:

- CCHV raw event fields: `~/claude-code-history-viewer/src-tauri/src/models/message.rs:23-92`

However, CCHV's session message viewer excludes some event types and hides some system subtypes:

- excluded from the viewer: `progress`, `queue-operation`, `file-history-snapshot`, `last-prompt`, `pr-link`
- hidden system subtypes: `stop_hook_summary`, `turn_duration`

References:

- Excluded message types: `~/claude-code-history-viewer/src-tauri/src/commands/session/load.rs:563-590`

DataClaw is stricter still: its Claude export path ignores all non-`user` / non-`assistant` entries entirely.

References:

- DataClaw entry handling: `~/dataclaw/dataclaw/parsers/claude.py:241-265`

Practical consequence:

- CCHV is not fully lossless, but it still has broader Claude-log awareness than DataClaw.

## Observed In Real Claude Logs On This Machine

The following real Claude files on this machine contain data shapes that matter for the comparison:

- Root session example with `file-history-snapshot`, `isMeta`, `permissionMode`, raw `tool_result`, `progress`, and `thinking` blocks:
  `~/.claude/projects/-home-wd-transformers-qwen3-moe-fused/5801999e-5607-4d50-9b68-8bf91a1b9252.jsonl:1-51`

- Subagent example with `isSidechain: true` and `agentId`:
  `~/.claude/projects/-home-wd-transformers-qwen3-moe-fused/5801999e-5607-4d50-9b68-8bf91a1b9252/subagents/agent-ac4d48d.jsonl:1-48`

- Real Claude `summary` entries exist in session logs:
  `~/.claude/projects/-home-wd-rocm-systems/050b046a-5e97-40f3-88c6-4611e385f672.jsonl:393-1441`

These machine logs confirm that the gaps above are not theoretical; the omitted or flattened fields are present in actual Claude Code data.

## Bottom Line

If the goal is a training/export dataset with simple, normalized conversation rows, DataClaw's current Claude export shape is reasonable.

If the goal is Claude-log fidelity, CCHV currently retains more of the original Claude structure than DataClaw, especially around:

- raw message identity / threading
- raw tool payloads and results
- per-message metadata
- summaries / rename metadata
- per-file subagent granularity
