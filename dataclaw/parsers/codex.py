import dataclasses
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import _json as json
from ..anonymizer import Anonymizer
from ..export_tasks import ExportSessionTask
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
) -> Iterable[dict]:
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


def build_export_session_tasks(project_index: int, project: dict) -> list[ExportSessionTask]:
    tasks: list[ExportSessionTask] = []
    for task_index, session_file in enumerate(get_project_index().get(project["dir_name"], [])):
        tasks.append(
            ExportSessionTask(
                source=SOURCE,
                project_index=project_index,
                task_index=task_index,
                project_dir_name=project["dir_name"],
                project_display_name=project["display_name"],
                estimated_bytes=session_file.stat().st_size if session_file.exists() else 0,
                kind="codex",
                file_path=str(session_file),
            )
        )
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    if not task.file_path:
        return None
    return parse_session_file(Path(task.file_path), anonymizer, include_thinking, task.project_dir_name)


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
    pending_tool_uses: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    pending_tool_uses_by_call_id: dict[str, list[dict[str, Any]]] = dataclasses.field(default_factory=dict)
    pending_tool_results: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    pending_thinking: list[str] = dataclasses.field(default_factory=list)
    _pending_thinking_seen: set[str] = dataclasses.field(default_factory=set)
    raw_cwd: str = UNKNOWN_CODEX_CWD
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    pending_user_content_parts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    pending_user_timestamp: str | None = None


def build_tool_result_map(entries: Iterable[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build call_id -> {output, status} from tool outputs."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload", {})
        call_id = payload.get("call_id")
        if not call_id:
            continue
        built = _build_codex_tool_result(payload, anonymizer)
        if built is not None:
            result[call_id] = built

    return result


def _build_codex_tool_result(payload: dict[str, Any], anonymizer: Anonymizer) -> dict[str, Any] | None:
    payload_type = payload.get("type")

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
        return {"output": out, "status": "success"}

    if payload_type == "custom_tool_call_output":
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
        return {"output": out, "status": "success"}

    return None


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

    last_timestamp: str | None = None
    try:
        for entry in iter_jsonl(filepath):
            timestamp = normalize_timestamp(entry.get("timestamp"))
            if state.pending_user_content_parts and not _is_user_content_entry(entry):
                _flush_pending_user_message(state, state.pending_user_timestamp or last_timestamp or timestamp)
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
    except OSError as e:
        logger.warning("Failed to read Codex session file %s: %s", filepath, e)
        return None

    state.stats["input_tokens"] = state.max_input_tokens
    state.stats["output_tokens"] = state.max_output_tokens

    if state.raw_cwd != target_cwd:
        return None

    _flush_pending_user_message(state, state.pending_user_timestamp or state.metadata["end_time"] or last_timestamp)
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


def _build_codex_image_part(image_url: str, anonymizer: Anonymizer) -> dict[str, Any] | None:
    if not image_url:
        return None

    if image_url.startswith("data:") and ";base64," in image_url:
        header, data = image_url.split(",", 1)
        media_type = header[5:].split(";", 1)[0]
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    if image_url.startswith("file://"):
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": f"file://{anonymizer.path(image_url[7:])}",
            },
        }

    return {
        "type": "image",
        "source": {
            "type": "url",
            "url": anonymizer.text(image_url),
        },
    }


def _build_codex_local_image_part(image_path: str, state: CodexParseState, anonymizer: Anonymizer) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_absolute() and state.raw_cwd != UNKNOWN_CODEX_CWD:
        path = Path(state.raw_cwd) / path
    return {
        "type": "image",
        "source": {
            "type": "url",
            "url": f"file://{anonymizer.path(str(path))}",
        },
    }


def _extract_response_user_content_parts(payload: dict[str, Any], anonymizer: Anonymizer) -> list[dict[str, Any]]:
    content_parts: list[dict[str, Any]] = []
    for part in payload.get("content", []):
        if not isinstance(part, dict):
            continue
        if part.get("type") != "input_image":
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, str) and image_url:
            image_part = _build_codex_image_part(image_url, anonymizer)
            if image_part is not None:
                content_parts.append(image_part)
    return content_parts


def _extract_event_user_content_parts(
    payload: dict[str, Any],
    state: CodexParseState,
    anonymizer: Anonymizer,
) -> list[dict[str, Any]]:
    content_parts: list[dict[str, Any]] = []
    for image_url in payload.get("images", []):
        if isinstance(image_url, str) and image_url:
            image_part = _build_codex_image_part(image_url, anonymizer)
            if image_part is not None:
                content_parts.append(image_part)
    for image_path in payload.get("local_images", []):
        if isinstance(image_path, str) and image_path:
            content_parts.append(_build_codex_local_image_part(image_path, state, anonymizer))
    return content_parts


def _clear_pending_user_content(state: CodexParseState) -> None:
    state.pending_user_content_parts.clear()
    state.pending_user_timestamp = None


def _apply_codex_tool_result(tool_use: dict[str, Any], result: dict[str, Any]) -> None:
    if result.get("output"):
        tool_use["output"] = result["output"]
    if result.get("status"):
        tool_use["status"] = result["status"]


def _attach_codex_tool_result(state: CodexParseState, call_id: str, result: dict[str, Any]) -> None:
    matched_tool_uses = state.pending_tool_uses_by_call_id.pop(call_id, [])
    if matched_tool_uses:
        for tool_use in matched_tool_uses:
            _apply_codex_tool_result(tool_use, result)
        return
    state.pending_tool_results[call_id] = result


def _register_codex_tool_use(state: CodexParseState, tool_use: dict[str, Any], call_id: str | None) -> None:
    if not isinstance(call_id, str) or not call_id:
        return
    pending_result = state.pending_tool_results.pop(call_id, None)
    if pending_result is not None:
        _apply_codex_tool_result(tool_use, pending_result)
        return
    state.pending_tool_uses_by_call_id.setdefault(call_id, []).append(tool_use)


def _flush_pending_user_message(state: CodexParseState, timestamp: str | None) -> None:
    if not state.pending_user_content_parts:
        return

    effective_timestamp = state.pending_user_timestamp or timestamp
    state.messages.append(
        {
            "role": "user",
            "content_parts": list(state.pending_user_content_parts),
            "timestamp": effective_timestamp,
        }
    )
    state.stats["user_messages"] += 1
    update_time_bounds(state.metadata, effective_timestamp)
    _clear_pending_user_content(state)


def _is_user_content_entry(entry: dict[str, Any]) -> bool:
    if entry.get("type") == "event_msg":
        return entry.get("payload", {}).get("type") == "user_message"
    if entry.get("type") == "response_item":
        payload = entry.get("payload", {})
        return payload.get("type") == "message" and payload.get("role") == "user"
    return False


def handle_response_item(
    state: CodexParseState,
    entry: dict[str, Any],
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    payload = entry.get("payload", {})
    item_type = payload.get("type")
    if item_type == "message" and payload.get("role") == "user":
        content_parts = _extract_response_user_content_parts(payload, anonymizer)
        if content_parts:
            state.pending_user_content_parts.extend(content_parts)
            if state.pending_user_timestamp is None:
                state.pending_user_timestamp = normalize_timestamp(entry.get("timestamp"))
        return
    if item_type == "function_call":
        tool_name = payload.get("name")
        args_data = parse_tool_arguments(payload.get("arguments"))
        tool_use = {
            "tool": tool_name,
            "input": parse_tool_input(tool_name, args_data, anonymizer),
            "_call_id": payload.get("call_id"),
        }
        _register_codex_tool_use(state, tool_use, payload.get("call_id"))
        state.pending_tool_uses.append(tool_use)
    elif item_type == "custom_tool_call":
        tool_name = payload.get("name")
        raw_input = payload.get("input", "")
        if isinstance(raw_input, str):
            inp = {"patch": anonymizer.text(raw_input)}
        else:
            inp = parse_tool_input(tool_name, raw_input, anonymizer)
        tool_use = {
            "tool": tool_name,
            "input": inp,
            "_call_id": payload.get("call_id"),
        }
        _register_codex_tool_use(state, tool_use, payload.get("call_id"))
        state.pending_tool_uses.append(tool_use)
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            result = _build_codex_tool_result(payload, anonymizer)
            if result is not None:
                _attach_codex_tool_result(state, call_id, result)
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
            output_tokens = safe_int(total_usage.get("output_tokens"))
            state.max_input_tokens = max(state.max_input_tokens, input_tokens)
            state.max_output_tokens = max(state.max_output_tokens, output_tokens)


def handle_user_message(
    state: CodexParseState,
    payload: dict[str, Any],
    timestamp: str | None,
    anonymizer: Anonymizer,
) -> None:
    flush_pending(state, timestamp)
    pending_parts = list(state.pending_user_content_parts)
    content = payload.get("message")
    if not pending_parts:
        pending_parts.extend(_extract_event_user_content_parts(payload, state, anonymizer))

    msg: dict[str, Any] = {"role": "user", "timestamp": timestamp}
    if isinstance(content, str) and content.strip():
        msg["content"] = anonymizer.text(content.strip())
    if pending_parts:
        msg["content_parts"] = pending_parts

    if len(msg) > 2 or (len(msg) == 2 and "content" in msg):
        state.messages.append(msg)
        state.stats["user_messages"] += 1
        update_time_bounds(state.metadata, timestamp)

    _clear_pending_user_content(state)


def resolve_tool_uses(state: CodexParseState) -> list[dict]:
    """Strip internal `_call_id` field from pending tool uses."""
    resolved = []
    for tool_use in state.pending_tool_uses:
        tool_use.pop("_call_id", None)
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
