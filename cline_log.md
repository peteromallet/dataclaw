# Adding Cline as a DataClaw Source

## Cline Storage Format

Cline is a VS Code extension (formerly "Claude Dev") that stores conversation data locally.

### Storage Locations

1. **Primary (new, cross-platform):** `~/.cline/data/tasks/<task-id>/`
2. **Legacy (macOS VS Code):** `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/<task-id>/`

### Key Files Per Task

- `api_conversation_history.json` — Full LLM conversation (Anthropic MessageParam format)
- `ui_messages.json` — UI-level messages (less useful for training data)
- `task_metadata.json` — File context, model usage, environment info

### Task Index

- `~/.cline/data/state/taskHistory.json` — Array of `HistoryItem` objects
- Alternative: `~/.cline/data/globalState.json` key `taskHistory`

### Message Format (`api_conversation_history.json`)

Array of `ClineStorageMessage` objects:
```json
{
  "role": "user" | "assistant",
  "content": "string" | [content_blocks],
  "modelInfo": { "modelId": "...", "providerId": "..." },
  "metrics": { "tokens": { "prompt": N, "completion": N, "cached": N }, "cost": N },
  "ts": 1234567890
}
```

Content blocks follow Anthropic SDK format:
- `{ "type": "text", "text": "..." }`
- `{ "type": "tool_use", "id": "...", "name": "...", "input": {...} }`
- `{ "type": "tool_result", "tool_use_id": "...", "content": "..." }`
- `{ "type": "thinking", "thinking": "...", "signature": "..." }`
- `{ "type": "image", "source": {...} }`

### Task History Item (for discovery)

```json
{
  "id": "task-id",
  "ts": 1234567890,
  "task": "initial user prompt",
  "tokensIn": N,
  "tokensOut": N,
  "totalCost": N,
  "cwdOnTaskInitialization": "/path/to/project",
  "modelId": "claude-sonnet-4-20250514"
}
```

## Implementation Plan

### 1. `parser.py` — Constants (top of file, lines 17-54)

Add:
```python
CLINE_SOURCE = "cline"

CLINE_DIR = Path.home() / ".cline" / "data"
CLINE_TASKS_DIR = CLINE_DIR / "tasks"
CLINE_STATE_DIR = CLINE_DIR / "state"
UNKNOWN_CLINE_CWD = "<unknown-cwd>"

_CLINE_PROJECT_INDEX: dict[str, list[Path]] = {}
```

Also add legacy VS Code globalStorage paths (no `platform` import — just define and let `.exists()` handle it):
```python
# Legacy VS Code globalStorage paths (per-platform, checked at runtime)
_CLINE_LEGACY_DIRS = [
    Path.home() / "Library" / "Application Support" / "Code" / "User"
        / "globalStorage" / "saoudrizwan.claude-dev",  # macOS
    Path.home() / ".config" / "Code" / "User"
        / "globalStorage" / "saoudrizwan.claude-dev",  # Linux
    Path.home() / "AppData" / "Roaming" / "Code" / "User"
        / "globalStorage" / "saoudrizwan.claude-dev",  # Windows
]
```

### 2. `parser.py` — Discovery (`_discover_cline_projects`)

Strategy: Group tasks by `cwdOnTaskInitialization` from `taskHistory.json`.

```python
def _discover_cline_projects() -> list[dict]:
```

- Load task history via `_load_cline_task_history()`:
  1. Try `~/.cline/data/state/taskHistory.json` (primary)
  2. Fall back to `~/.cline/data/globalState.json` key `taskHistory`
  3. Fall back to legacy VS Code globalStorage `globalState.json` (if exists)
- Group tasks by `cwdOnTaskInitialization`
- For each group, count tasks that have `api_conversation_history.json` and sum file sizes
- Tasks without `cwdOnTaskInitialization` go under `UNKNOWN_CLINE_CWD`
- Return project dicts with all 5 required keys: `dir_name` (CWD path), `display_name` (`cline:<project-name>`), `session_count`, `total_size_bytes`, `source`
- Collect task dirs from both primary and legacy locations; deduplicate by task ID (prefer primary)

**Important**: If `taskHistory.json` is missing entirely, scan task dirs directly but group all under `UNKNOWN_CLINE_CWD` (no way to recover CWD without the history file — CWD is not stored in per-task files). This matches how OpenCode handles unknown CWDs.

### 3. `parser.py` — Project Index & Helpers

```python
def _load_cline_task_history() -> list[dict]:
def _get_cline_task_dirs() -> list[Path]:
def _get_cline_project_index(refresh=False) -> dict[str, list[Path]]:
def _build_cline_project_index() -> dict[str, list[Path]]:
def _build_cline_project_name(cwd: str) -> str:
```

- `_load_cline_task_history()`: Read taskHistory.json with fallback chain (primary → globalState → legacy)
- `_get_cline_task_dirs()`: Collect all task dirs from primary `CLINE_TASKS_DIR` + any existing legacy `_CLINE_LEGACY_DIRS`, deduplicate by task ID
- `_build_cline_project_index()`: Map `cwd -> [task_dir_paths]` using task history for CWD lookup; tasks with no CWD mapping go under `UNKNOWN_CLINE_CWD`
- `_build_cline_project_name(cwd)`: Returns `"cline:unknown"` for unknown CWD, otherwise `f"cline:{Path(cwd).name}"`

### 4. `parser.py` — Session Parser

```python
def _parse_cline_session(
    task_dir: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
```

- Read `api_conversation_history.json` from the task dir (JSON array, not JSONL)
- **Cannot reuse** `_build_tool_result_map` from Claude Code parser — Cline uses flat `{"role": ..., "content": [...]}` format, not Claude Code's `{"type": "user", "message": {"content": [...]}}` wrapper. Write a Cline-specific `_build_cline_tool_result_map()` with the same inner logic but adapted to the flat structure.
- Parse messages:
  - User messages: extract text content from string or content blocks; skip bare tool_result-only messages (fold into previous assistant's tool_uses)
  - Assistant messages: extract text blocks, thinking blocks (if `include_thinking`), tool_use blocks
- Extract model from `modelInfo.modelId` field on assistant messages (may be absent on user messages)
- Extract timestamps from `ts` fields (milliseconds since epoch → ISO format)
- Build token stats from `metrics.tokens` fields (prompt → input_tokens, completion → output_tokens)
- Return standardized session dict via `_make_session_result`

### 5. `parser.py` — Wire into dispatchers

- Add `_discover_cline_projects()` call to `discover_projects()` (line ~143)
- Add `CLINE_SOURCE` branch to `parse_project_sessions()` (line ~427):
  ```python
  if source == CLINE_SOURCE:
      index = _get_cline_project_index()
      task_dirs = index.get(project_dir_name, [])
      sessions = []
      for task_dir in task_dirs:
          parsed = _parse_cline_session(task_dir, anonymizer, include_thinking)
          if parsed and parsed["messages"]:
              parsed["project"] = _build_cline_project_name(project_dir_name)
              parsed["source"] = CLINE_SOURCE
              if not parsed.get("model"):
                  parsed["model"] = "unknown"
              sessions.append(parsed)
      return sessions
  ```

### 6. `cli.py` — Integration

Core source plumbing:
- **Line 15**: Add `CLINE_TASKS_DIR` to imports from parser (not `CLINE_DIR` — check tasks dir for existence)
- **Line 61**: Add `"cline"` to `EXPLICIT_SOURCE_CHOICES`
- **Line 62**: Add `"cline"` to `SOURCE_CHOICES`
- **Lines 80-96**: Add `"cline"` case to `_source_label()` → `"Cline"`
- **Lines 128-144**: Add `"cline"` case to `_has_session_sources()` checking `CLINE_TASKS_DIR.exists()`
- **Line 144**: Add `CLINE_TASKS_DIR.exists()` to the `auto` fallback `or` chain
- **Line 96**: Update the "all" label to include Cline

Additional stale locations to fix (pre-existing bugs for other sources too):
- **Line ~54** (`SETUP_TO_PUBLISH_STEPS`): Source hint `<claude|codex|gemini|all>` → add cline (and other missing sources)
- **Line ~117** (`_resolve_source_choice` docstring): Add cline, kimi, custom to listed choices
- **Lines ~230-231, ~1339-1345**: Source hint strings → add cline
- **Lines ~479-485** (HF README tags): Add `"cline"` tag (also add missing `"kimi-cli"`)
- **Lines ~1122-1130** (`prep()` error messages): Add cline to the source-specific error messages and fallback string
- **Lines ~1429-1438** (`_run_export()` error messages): Same — add cline to error strings
- **Line ~1426** (`_run_export()` banner): "DataClaw -- Claude/Codex Log Exporter" → update to mention more sources or make generic

### 7. `tests/test_parser.py` — Tests

Add test class `TestClineParser` with:
- `test_discover_cline_projects` — mock task history + task dirs
- `test_parse_cline_session_basic` — simple user/assistant conversation
- `test_parse_cline_session_with_tools` — tool_use + tool_result blocks
- `test_parse_cline_session_with_thinking` — thinking blocks
- `test_parse_cline_session_no_file` — missing file returns None
- `test_cline_project_index_grouping` — tasks grouped by cwd
- `test_cline_unknown_cwd_fallback` — tasks without taskHistory.json all go to unknown

**Test isolation**: Update ALL existing `_disable_codex` / `_disable_others` helpers to also disable Cline (monkeypatch `CLINE_TASKS_DIR` to nonexistent path and clear `_CLINE_PROJECT_INDEX`). There are at least 4 copies of this pattern:
- `TestDiscoverProjects._disable_codex` (~line 430)
- `TestDiscoverSubagentProjects._disable_codex` (~line 983)
- `TestDiscoverOpenclawProjects._disable_others` (~line 1583)
- `TestDiscoverCustomProjects._disable_others` (~line 1669)

### 8. Docstring / module-level comment

Update the module docstring in `parser.py` line 1.

## Key Design Decisions

1. **Anthropic format — similar but not identical**: Cline uses Anthropic MessageParam format natively, so content blocks (text, tool_use, tool_result, thinking) share the same schema. However, the outer wrapper differs: Claude Code uses `{"type": "user", "message": {"content": [...]}}` while Cline uses flat `{"role": "user", "content": [...]}`. This means `_build_tool_result_map` and `_process_entry` cannot be reused directly — need Cline-specific versions with shared inner logic.

2. **Legacy path support**: Check both `~/.cline/data/` and the VS Code globalStorage path. Prefer the new path; only fall back to legacy if primary doesn't exist. **Note**: `modelInfo`, `metrics`, and `ts` are Cline-specific extensions to the Anthropic format — not all messages will have them (especially user messages). The parser must handle their absence gracefully.

3. **No cwd hashing**: Unlike Kimi/Gemini, Cline doesn't hash working directories. Tasks are indexed by ID in `taskHistory.json` with `cwdOnTaskInitialization` as a plain path.

4. **Project grouping**: Group by `cwdOnTaskInitialization` field from task history. Tasks without a cwd get grouped under `<unknown-cwd>`.

5. **Default model**: When `modelInfo` is absent from messages, default to `"unknown"` (unlike Kimi which defaults to `"kimi-k2"`).

## Review Findings & Fixes Applied

### Round 1 — HIGH

- **Legacy path**: Don't use `platform` import. Define legacy paths unconditionally and let `.exists()` checks handle it. Define for macOS, Linux, and Windows. *(Fixed in plan section 1)*
- **Merge both dirs**: If both primary and legacy exist, merge tasks, deduplicate by task ID. *(Fixed in plan sections 2-3)*
- **Discovery return dict**: Must include all 5 keys: `dir_name`, `display_name`, `session_count`, `total_size_bytes`, `source`. *(Fixed in plan section 2)*
- **`parse_project_sessions` branch detail**: Full dispatcher code provided. *(Fixed in plan section 5)*

### Round 2 — HIGH

- **taskHistory.json is the only source of CWD mapping**: Per-task files (`api_conversation_history.json`, `task_metadata.json`) do NOT contain `cwdOnTaskInitialization`. Without `taskHistory.json`, all tasks fall under `UNKNOWN_CLINE_CWD`. This is acceptable (same as OpenCode's pattern) but the plan must not claim it "recovers project grouping" from filesystem scanning. *(Fixed in plan sections 2-3)*
- **Legacy dir may lack `taskHistory.json`**: The legacy VS Code globalStorage may only have `tasks/<id>/` dirs with no state file. Legacy tasks without a history file all go under `UNKNOWN_CLINE_CWD`. *(Fixed in plan section 2)*
- **CLI has ~7 additional stale locations** beyond the core source plumbing (error messages, banner text, HF tags, source hints). *(Fixed in plan section 6)*
- **`_build_tool_result_map` cannot be reused**: Different outer wrapper format. Need Cline-specific version. *(Fixed in plan section 4 and design decision 1)*
- **`_has_session_sources` should check `CLINE_TASKS_DIR`** not `CLINE_DIR` — import `CLINE_TASKS_DIR` in cli.py. *(Fixed in plan section 6)*
- **Test isolation**: Must update 4+ existing `_disable_*` helpers to also disable Cline. *(Fixed in plan section 7)*

### Round 2 — MEDIUM

- **VS Code forks**: Known limitation — Cursor, VSCodium, Windsurf not included in v1.
- **globalState.json fallback**: When `taskHistory.json` is missing, try `globalState.json` key `taskHistory` as alternative (same data, different container).

### Round 2 — LOW

- **Module docstring** (parser.py line 1): Missing "Kimi CLI" — fix alongside Cline.
- **HF README tags**: Missing `"kimi-cli"` (pre-existing bug) — fix alongside adding `"cline"`.

## Files to Modify

| File | Changes |
|------|---------|
| `dataclaw/parser.py` | Constants, discovery, index, parser, dispatcher wiring |
| `dataclaw/cli.py` | Imports, source choices, labels, existence checks, docstring fix |
| `tests/test_parser.py` | New test class for Cline |
