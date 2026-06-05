# Hermes Provider Notes

## Provider contract

- Providers are registered in `dataclaw/providers.py` through `ModuleProvider.from_module`.
- A parser module must expose `SOURCE`, a module-level source path selected by `source_path_attr`, and these functions: `discover_projects`, `parse_project_sessions`, `build_export_session_tasks`, and `parse_export_session_task`.
- Parsed sessions use the shared `make_session_result` shape from `dataclaw/parsers/common.py`: `session_id`, `model`, `git_branch`, `start_time`, `end_time`, `messages`, and `stats`. `collect_project_sessions` adds `project` and `source`.
- Built-in parsers accept an `Anonymizer`, but they do not rewrite strings during parsing. Export applies `secrets.transform_session` recursively with the provider's `NON_ANON_STRING_KEYS`. Hermes follows that same flow.
- `include_thinking` controls whether parser-specific reasoning/thinking fields are emitted as message `thinking`.
- Export parallelism is provider-driven: `build_export_session_tasks` creates lightweight `ExportSessionTask` records and `parse_export_session_task` reparses one session in a worker process.

## Hermes state.db

Path inspected read-only: `~/.hermes/state.db`.

Tables of interest:

- `sessions(id, source, user_id, model, model_config, system_prompt, parent_session_id, started_at REAL, ended_at, end_reason, message_count, tool_call_count, input_tokens, output_tokens, title, cache_read_tokens, cache_write_tokens, reasoning_tokens, billing_*, estimated_cost_usd, actual_cost_usd, ..., api_call_count)`
- `messages(id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp REAL, token_count, finish_reason, reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items)`
- FTS5 tables exist for search and are ignored.

Real counts on 2026-06-05:

- `sessions`: 10,033 total.
- By `sessions.source`: `cli` 10,026 sessions, `telegram` 6 sessions, `cron` 1 session.
- `message_count = 0`: 1,261 sessions. `message_count = 1`: 33 sessions.
- `parent_session_id` is populated on 917 sessions. The common export schema has no parent-chain field, so the provider exports each session independently.
- `messages.role`: `tool` 175,231 rows, `assistant` 127,614 rows, `user` 9,721 rows.
- Non-empty assistant `tool_calls`: 118,534 rows.
- Non-empty reasoning fields: `reasoning` 2 rows and `reasoning_content` 2 rows. `reasoning_details`, `codex_reasoning_items`, and `codex_message_items` were empty in the inspected DB.

Important timestamp detail: Hermes `started_at`, `ended_at`, and message `timestamp` values are Unix seconds, not milliseconds.

Observed message structure:

- `user` rows carry user-visible text in `content`.
- `assistant` rows carry assistant text in `content` and OpenAI-style function call lists in `tool_calls`.
- Each `tool_calls` item is a dict with keys like `id`, `call_id`, `type`, `response_item_id`, and `function`; `function` has `name` and JSON-string `arguments`.
- `tool` rows carry tool output in `content`, keyed by `tool_call_id`. Tool rows generally do not repeat the tool name, so the parser attaches them to the matching assistant tool call.

## Hermes session JSON files

Path inspected: `~/.hermes/sessions/`.

Two file families exist:

- `session_<id>.json` with keys like `session_id`, `model`, `base_url`, `platform`, `session_start`, `last_updated`, `system_prompt`, `tools`, `message_count`, and `messages`.
- `request_dump_<id>_<timestamp>.json`, which are raw/debug API request dumps.

Coverage comparison:

- `session_*.json`: 12,490 files.
- DB sessions: 10,033 rows.
- Overlap by session id: 3,825.
- DB-only sessions: 6,208.
- JSON-only sessions: 8,665.

Source-of-truth decision: use `state.db`. It is canonical for current Hermes history, has normalized tool-result rows, includes richer reasoning columns, and covers thousands of sessions absent from the JSON files. The JSON files are useful for historical/debug inspection but are not complete enough to drive export.

## Project grouping

Hermes has no per-project directory. The parser groups by `sessions.source`, yielding display names like `hermes:cli`. This mirrors the Cursor-style single-store approach: build a project index from stable metadata in the store, then parse sessions by id.

Discovery ignores sessions with `message_count <= 0`; parsing still skips any session that produces no normalized messages.
