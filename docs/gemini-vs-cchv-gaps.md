# Gemini CLI Gaps vs CCHV

## Scope

This note compares Gemini CLI handling in:

- DataClaw: `~/dataclaw`
- Claude Code History Viewer (CCHV): `~/claude-code-history-viewer`

The goal is to identify Gemini CLI data that CCHV captures more faithfully than DataClaw today, while also noting important cases where DataClaw retains data that CCHV skips.

## Summary

CCHV preserves more Gemini message/event structure than DataClaw in several places.

The biggest current DataClaw gaps are:

1. It drops Gemini `info` / `warning` / `error` messages.
2. It drops `resultDisplay` tool UI output, including file-diff previews and tool-status strings.
3. It drops non-text user content parts such as `inlineData` images/documents.
4. It drops part-level `functionResponse` blocks embedded in message content.
5. It only keeps session-level token totals, while CCHV keeps per-message Gemini token usage.
6. It does not preserve top-level Gemini session metadata such as `summary` and `kind`.

Important counterpoint:

- CCHV explicitly skips Gemini sessions whose top-level `kind` is `subagent`, while DataClaw currently exports them as normal sessions.

## Detailed Findings

### 1. CCHV keeps `info` / `warning` / `error` messages; DataClaw drops them

CCHV converts Gemini message records with types:

- `user`
- `gemini`
- `info`
- `warning`
- `error`

References:

- CCHV Gemini message dispatch: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:464-485`
- CCHV system-message conversion: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:630-653`

DataClaw only exports:

- `user`
- `gemini`

References:

- DataClaw Gemini parser message handling: `~/dataclaw/dataclaw/parsers/gemini.py:311-375`

Practical consequence:

- DataClaw drops Gemini informational and error messages that CCHV exposes as system messages.

Observed in real Gemini data on this machine:

- `835` `info` messages
- `29` `error` messages

Example real file:

- `~/.gemini/tmp/comfyui-featherops/chats/session-2026-03-24T08-56-51cb7147.json:10,16,1111`

### 2. CCHV keeps `resultDisplay`; DataClaw ignores it

CCHV converts Gemini `toolCalls[].resultDisplay` into extra content blocks.

References:

- CCHV includes `resultDisplay` during Gemini tool-call conversion: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:592-597`
- CCHV `extract_result_display(...)`: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:893-928`

DataClaw's Gemini parser never reads `resultDisplay`.

References:

- DataClaw Gemini tool-call parsing: `~/dataclaw/dataclaw/parsers/gemini.py:162-282`

Practical consequence:

- DataClaw drops user-visible tool UI output, including:
  - short status strings such as `Found 4 matching file(s)`
  - read-file previews such as `Read lines 50-150 ...`
  - file-diff previews stored in `resultDisplay.fileDiff`
  - potential subagent-progress markers (`isSubagentProgress`) if they appear

Observed in real Gemini data on this machine:

- `4386` string `resultDisplay` values
- `854` object `resultDisplay` values containing:
  - `fileDiff`
  - `fileName`
  - `filePath`
  - `originalContent`
  - `newContent`
  - `diffStat`
  - `isNewFile`

Example real file with file-diff previews:

- `~/.gemini/tmp/comfyui-featherops/chats/session-2026-03-28T01-43-f9f3aa2a.json:305-306,350-351,395-396,440-441`

### 3. CCHV keeps non-text Gemini content parts; DataClaw drops them in user messages

CCHV converts Gemini content parts such as:

- `inlineData` image/document blocks
- `fileData` URL-backed document blocks
- plain text parts
- `functionCall`
- `functionResponse`
- `executableCode`
- `codeExecutionResult`

References:

- CCHV content conversion helpers: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:660-867`

DataClaw's Gemini parser, for `user` messages, only extracts parts containing `text` and drops the rest.

References:

- DataClaw user-message extraction: `~/dataclaw/dataclaw/parsers/gemini.py:315-323`

Practical consequence:

- DataClaw drops user attachments and other structured Gemini content parts that CCHV preserves.

Observed in real Gemini data on this machine:

- real `inlineData` image attachments exist in user messages, for example:
  `~/.gemini/tmp/rocm-systems/chats/session-2026-03-06T03-33-68bc726c.json:4764,4794,5006,5036`

These include large base64 image payloads that CCHV maps to image/document blocks.

### 4. CCHV keeps part-level `functionResponse` blocks; DataClaw drops them when embedded in content

CCHV converts `functionResponse` parts in Gemini content into `tool_result` blocks.

References:

- CCHV `functionResponse` conversion in `convert_gemini_content_to_claude(...)`: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:691-709`
- CCHV direct `functionResponse` part conversion: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:813-830`

DataClaw does not preserve these when they appear inside a message `content` array, because its user-message parser keeps only text parts.

References:

- DataClaw user-message extraction: `~/dataclaw/dataclaw/parsers/gemini.py:315-323`

Practical consequence:

- DataClaw loses some Gemini tool-result structure that is encoded directly in content parts rather than only in `toolCalls[].result`.

Observed in real Gemini data on this machine:

- real `functionResponse` content parts exist, for example in:
  `~/.gemini/tmp/rocm-systems/chats/session-2026-03-06T03-33-68bc726c.json:122,147,172,231`

### 5. CCHV keeps per-message token usage; DataClaw only keeps session totals

CCHV stores Gemini per-message token usage derived directly from each Gemini response record's `tokens` field.

References:

- CCHV Gemini usage extraction: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:601-608,616-627`

DataClaw only aggregates token counts into session-level `stats`.

References:

- DataClaw Gemini token aggregation: `~/dataclaw/dataclaw/parsers/gemini.py:340-343`
- DataClaw normalized session shape: `~/dataclaw/dataclaw/parsers/common.py:56-71`

Practical consequence:

- DataClaw loses per-message Gemini usage, including cached-input attribution on individual assistant responses.

### 6. CCHV keeps more Gemini session metadata than DataClaw exports

CCHV extracts Gemini session metadata including:

- `session_id`
- `kind`
- `start_time`
- `last_updated`
- `message_count`
- `has_tool_use`
- `summary`

References:

- CCHV Gemini lightweight metadata extraction: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:388-457`

DataClaw exports a smaller normalized session shape and does not preserve top-level Gemini `summary` or `kind`.

References:

- DataClaw Gemini metadata initialization: `~/dataclaw/dataclaw/parsers/gemini.py:300-308`
- DataClaw normalized session shape: `~/dataclaw/dataclaw/parsers/common.py:56-71`

Practical consequence:

- DataClaw loses Gemini session metadata that CCHV surfaces in its session index/browser.

Observed in real Gemini data on this machine:

- `122` Gemini session files have top-level `summary`
- top-level `kind` values present include:
  - `main`
  - `subagent`

## Important Counterpoint: CCHV skips Gemini `kind == "subagent"` sessions, but DataClaw exports them

This is an important Gemini difference in the opposite direction.

CCHV explicitly skips Gemini sessions whose top-level `kind` is `subagent` in both:

- project/session listing
- search

References:

- CCHV session listing skip: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:167-169`
- CCHV project scan skip: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:85-88`
- CCHV search skip: `~/claude-code-history-viewer/src-tauri/src/providers/gemini.rs:283-286`

DataClaw does not filter by `kind`, so it includes Gemini subagent sessions if they exist as chat files.

References:

- DataClaw Gemini discovery and parse paths read all `session-*.json` files: `~/dataclaw/dataclaw/parsers/gemini.py:123-159`

Observed in real Gemini data on this machine:

- there is exactly one real Gemini chat file with `kind: "subagent"`:
  `~/.gemini/tmp/tmp/chats/session-2026-03-05T03-59-51c63ffc.json:82`
- DataClaw successfully parses and exports that session.

So, unlike the Claude comparison, Gemini is not a simple one-way story where CCHV always preserves more.

## Observed In Real Gemini Data On This Machine

The following real Gemini structures exist on this machine and matter for the comparison:

- `info` / `error` message types:
  `~/.gemini/tmp/comfyui-featherops/chats/session-2026-03-24T08-56-51cb7147.json:10,16,1111`

- `resultDisplay.fileDiff` edit previews:
  `~/.gemini/tmp/comfyui-featherops/chats/session-2026-03-28T01-43-f9f3aa2a.json:305-306,350-351,395-396,440-441`

- user `inlineData` image attachments:
  `~/.gemini/tmp/rocm-systems/chats/session-2026-03-06T03-33-68bc726c.json:4764,4794,5006,5036`

- content-part `functionResponse` blocks:
  `~/.gemini/tmp/rocm-systems/chats/session-2026-03-06T03-33-68bc726c.json:122,147,172,231`

- a real Gemini subagent session:
  `~/.gemini/tmp/tmp/chats/session-2026-03-05T03-59-51c63ffc.json:82`

These are all real, present data shapes, not just theoretical parser code paths.

## Bottom Line

Compared with CCHV, DataClaw currently loses more Gemini fidelity around:

- `info` / `warning` / `error` messages
- `resultDisplay` tool UI output
- non-text content parts such as `inlineData`
- part-level `functionResponse` blocks
- per-message token usage
- top-level session metadata like `summary` and `kind`

But CCHV also has one notable Gemini omission that DataClaw does not:

- CCHV skips Gemini `kind == "subagent"` sessions from its normal session views, while DataClaw exports them.
