import dataclasses
import logging
from pathlib import Path
from typing import Any

from .. import _json as json
from ..anonymizer import Anonymizer
from .common import (
    build_prefixed_project_name,
    build_projects_from_index,
    collect_project_sessions,
    get_cached_index,
    iter_jsonl,
    make_session_result,
    make_stats,
    normalize_timestamp,
    parse_tool_input,
    safe_int,
    sum_existing_path_sizes,
    update_time_bounds,
)

logger = logging.getLogger(__name__)

SOURCE = "codex"
CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_ARCHIVED_DIR = CODEX_DIR / "archived_sessions"
UNKNOWN_CODEX_CWD = "<unknown-cwd>"

_PROJECT_INDEX: dict[str, list[Path]] = {}


def get_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _PROJECT_INDEX
    _PROJECT_INDEX = get_cached_index(
        _PROJECT_INDEX,
        refresh,
        lambda: build_project_index(CODEX_SESSIONS_DIR, CODEX_ARCHIVED_DIR),
    )
    return _PROJECT_INDEX


def discover_projects(index: dict[str, list[Path]] | None = None) -> list[dict]:
    if index is None:
        index = get_project_index(refresh=True)
    return build_projects_from_index(
        index,
        SOURCE,
        build_project_name,
        sum_existing_path_sizes,
    )


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> list[dict]:
    session_files = get_project_index().get(project_dir_name, [])
    return collect_project_sessions(
        session_files,
        lambda session_file: parse_session_file(
            session_file,
            anonymizer=anonymizer,
            include_thinking=include_thinking,
            target_cwd=project_dir_name,
        ),
        build_project_name(project_dir_name),
        SOURCE,
    )


def build_project_index(sessions_dir: Path, archived_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for session_file in iter_session_files(sessions_dir, archived_dir):
        cwd = extract_cwd(session_file) or UNKNOWN_CODEX_CWD
        index.setdefault(cwd, []).append(session_file)
    return index


def iter_session_files(sessions_dir: Path, archived_dir: Path) -> list[Path]:
    files: list[Path] = []
    if sessions_dir.exists():
        files.extend(sorted(sessions_dir.rglob("*.jsonl")))
    if archived_dir.exists():
        files.extend(sorted(archived_dir.glob("*.jsonl")))
    return files


def extract_cwd(session_file: Path) -> str | None:
    try:
        for entry in iter_jsonl(session_file):
            if entry.get("type") in ("session_meta", "turn_context"):
                cwd = entry.get("payload", {}).get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
    except OSError as e:
        logger.warning("Failed to read Codex session file %s: %s", session_file, e)
        return None
    return None


def build_project_name(cwd: str) -> str:
    return build_prefixed_project_name(SOURCE, cwd, UNKNOWN_CODEX_CWD)


@dataclasses.dataclass
class CodexParseState:
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    stats: dict[str, int] = dataclasses.field(default_factory=make_stats)
    pending_tool_uses: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    pending_thinking: list[str] = dataclasses.field(default_factory=list)
    _pending_thinking_seen: set[str] = dataclasses.field(default_factory=set)
    raw_cwd: str = UNKNOWN_CODEX_CWD
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    tool_result_map: dict[str, dict] = dataclasses.field(default_factory=dict)


def build_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build call_id -> {output, status} from tool outputs."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload", {})
        payload_type = payload.get("type")
        call_id = payload.get("call_id")
        if not call_id:
            continue

        if payload_type == "function_call_output":
            raw = payload.get("output", "")
            out: dict[str, Any] = {}
            lines = raw.splitlines()
            output_lines: list[str] = []
            in_output = False
            for line in lines:
                if line.startswith("Exit code: "):
                    try:
                        out["exit_code"] = int(line[len("Exit code: ") :].strip())
                    except ValueError:
                        out["exit_code"] = line[len("Exit code: ") :].strip()
                elif line.startswith("Wall time: "):
                    out["wall_time"] = line[len("Wall time: ") :].strip()
                elif line == "Output:":
                    in_output = True
                elif in_output:
                    output_lines.append(line)
            if output_lines:
                out["output"] = anonymizer.text("\n".join(output_lines).strip())
            result[call_id] = {"output": out, "status": "success"}

        elif payload_type == "custom_tool_call_output":
            raw = payload.get("output", "")
            out: dict[str, Any] = {}
            try:
                parsed = json.loads(raw)
                text = parsed.get("output", "")
                if text:
                    out["output"] = anonymizer.text(str(text))
                meta = parsed.get("metadata", {})
                if "exit_code" in meta:
                    out["exit_code"] = meta["exit_code"]
                if "duration_seconds" in meta:
                    out["duration_seconds"] = meta["duration_seconds"]
            except (json.JSONDecodeError, AttributeError):
                if raw:
                    out["output"] = anonymizer.text(raw)
            result[call_id] = {"output": out, "status": "success"}

    return result


def parse_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    state = CodexParseState(
        metadata={
            "session_id": filepath.stem,
            "cwd": None,
            "git_branch": None,
            "model": None,
            "start_time": None,
            "end_time": None,
            "model_provider": None,
        },
    )

    try:
        entries = list(iter_jsonl(filepath))
    except OSError as e:
        logger.warning("Failed to read Codex session file %s: %s", filepath, e)
        return None

    state.tool_result_map = build_tool_result_map(entries, anonymizer)

    last_timestamp: str | None = None
    for entry in entries:
        timestamp = normalize_timestamp(entry.get("timestamp"))
        last_timestamp = timestamp
        entry_type = entry.get("type")

        if entry_type == "session_meta":
            handle_session_meta(state, entry, filepath, anonymizer)
        elif entry_type == "turn_context":
            handle_turn_context(state, entry, anonymizer)
        elif entry_type == "response_item":
            handle_response_item(state, entry, anonymizer, include_thinking)
        elif entry_type == "event_msg":
            payload = entry.get("payload", {})
            event_type = payload.get("type")
            if event_type == "token_count":
                handle_token_count(state, payload)
            elif event_type == "agent_reasoning" and include_thinking:
                thinking = payload.get("text")
                if isinstance(thinking, str) and thinking.strip():
                    cleaned = anonymizer.text(thinking.strip())
                    if cleaned not in state._pending_thinking_seen:
                        state._pending_thinking_seen.add(cleaned)
                        state.pending_thinking.append(cleaned)
            elif event_type == "user_message":
                handle_user_message(state, payload, timestamp, anonymizer)
            elif event_type == "agent_message":
                handle_agent_message(state, payload, timestamp, anonymizer, include_thinking)

    state.stats["input_tokens"] = state.max_input_tokens
    state.stats["output_tokens"] = state.max_output_tokens

    if state.raw_cwd != target_cwd:
        return None

    flush_pending(state, timestamp=state.metadata["end_time"] or last_timestamp)

    if state.metadata["model"] is None:
        model_provider = state.metadata.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            state.metadata["model"] = f"{model_provider}-codex"
        else:
            state.metadata["model"] = "codex-unknown"

    return make_session_result(state.metadata, state.messages, state.stats)


def handle_session_meta(
    state: CodexParseState,
    entry: dict[str, Any],
    filepath: Path,
    anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        state.raw_cwd = session_cwd
        if state.metadata["cwd"] is None:
            state.metadata["cwd"] = anonymizer.path(session_cwd)
    if state.metadata["session_id"] == filepath.stem:
        state.metadata["session_id"] = payload.get("id", state.metadata["session_id"])
    if state.metadata["model_provider"] is None:
        state.metadata["model_provider"] = payload.get("model_provider")
    git_info = payload.get("git", {})
    if isinstance(git_info, dict) and state.metadata["git_branch"] is None:
        state.metadata["git_branch"] = git_info.get("branch")


def handle_turn_context(
    state: CodexParseState,
    entry: dict[str, Any],
    anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        state.raw_cwd = session_cwd
        if state.metadata["cwd"] is None:
            state.metadata["cwd"] = anonymizer.path(session_cwd)
    if state.metadata["model"] is None:
        model_name = payload.get("model")
        if isinstance(model_name, str) and model_name.strip():
            state.metadata["model"] = model_name


def handle_response_item(
    state: CodexParseState,
    entry: dict[str, Any],
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    payload = entry.get("payload", {})
    item_type = payload.get("type")
    if item_type == "function_call":
        tool_name = payload.get("name")
        args_data = parse_tool_arguments(payload.get("arguments"))
        state.pending_tool_uses.append(
            {
                "tool": tool_name,
                "input": parse_tool_input(tool_name, args_data, anonymizer),
                "_call_id": payload.get("call_id"),
            }
        )
    elif item_type == "custom_tool_call":
        tool_name = payload.get("name")
        raw_input = payload.get("input", "")
        if isinstance(raw_input, str):
            inp = {"patch": anonymizer.text(raw_input)}
        else:
            inp = parse_tool_input(tool_name, raw_input, anonymizer)
        state.pending_tool_uses.append(
            {
                "tool": tool_name,
                "input": inp,
                "_call_id": payload.get("call_id"),
            }
        )
    elif item_type == "reasoning" and include_thinking:
        for summary in payload.get("summary", []):
            if not isinstance(summary, dict):
                continue
            text = summary.get("text")
            if isinstance(text, str) and text.strip():
                cleaned = anonymizer.text(text.strip())
                if cleaned not in state._pending_thinking_seen:
                    state._pending_thinking_seen.add(cleaned)
                    state.pending_thinking.append(cleaned)


def handle_token_count(state: CodexParseState, payload: dict[str, Any]) -> None:
    info = payload.get("info", {})
    if isinstance(info, dict):
        total_usage = info.get("total_token_usage", {})
        if isinstance(total_usage, dict):
            input_tokens = safe_int(total_usage.get("input_tokens"))
            cached_tokens = safe_int(total_usage.get("cached_input_tokens"))
            output_tokens = safe_int(total_usage.get("output_tokens"))
            state.max_input_tokens = max(state.max_input_tokens, input_tokens + cached_tokens)
            state.max_output_tokens = max(state.max_output_tokens, output_tokens)


def handle_user_message(
    state: CodexParseState,
    payload: dict[str, Any],
    timestamp: str | None,
    anonymizer: Anonymizer,
) -> None:
    flush_pending(state, timestamp)
    content = payload.get("message")
    if isinstance(content, str) and content.strip():
        state.messages.append(
            {
                "role": "user",
                "content": anonymizer.text(content.strip()),
                "timestamp": timestamp,
            }
        )
        state.stats["user_messages"] += 1
        update_time_bounds(state.metadata, timestamp)


def resolve_tool_uses(state: CodexParseState) -> list[dict]:
    """Attach outputs from tool_result_map and strip internal _call_id field."""
    resolved = []
    for tool_use in state.pending_tool_uses:
        call_id = tool_use.pop("_call_id", None)
        if call_id and call_id in state.tool_result_map:
            result = state.tool_result_map[call_id]
            tool_use["output"] = result["output"]
            tool_use["status"] = result["status"]
        resolved.append(tool_use)
    return resolved


def handle_agent_message(
    state: CodexParseState,
    payload: dict[str, Any],
    timestamp: str | None,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    content = payload.get("message")
    msg: dict[str, Any] = {"role": "assistant"}
    if isinstance(content, str) and content.strip():
        msg["content"] = anonymizer.text(content.strip())
    if state.pending_thinking and include_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = resolve_tool_uses(state)

    if len(msg) > 1:
        msg["timestamp"] = timestamp
        state.messages.append(msg)
        state.stats["assistant_messages"] += 1
        state.stats["tool_uses"] += len(msg.get("tool_uses", []))
        update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def flush_pending(state: CodexParseState, timestamp: str | None) -> None:
    if not state.pending_tool_uses and not state.pending_thinking:
        return

    msg: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
    if state.pending_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = resolve_tool_uses(state)

    state.messages.append(msg)
    state.stats["assistant_messages"] += 1
    state.stats["tool_uses"] += len(msg.get("tool_uses", []))
    update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def parse_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse tool arguments as JSON: %s", e)
            return arguments
    return arguments
