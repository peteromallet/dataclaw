import atexit
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
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
    make_session_result,
    make_stats,
    parse_tool_input,
    safe_int,
    update_time_bounds,
)

logger = logging.getLogger(__name__)

SOURCE = "hermes"
HERMES_DIR = Path.home() / ".hermes"
HERMES_DB = HERMES_DIR / "state.db"
_DEFAULT_HERMES_DB = HERMES_DB
UNKNOWN_HERMES_SOURCE = "<unknown-source>"

_PROJECT_INDEX: dict[str, list[str]] = {}
_SESSION_SIZE_MAP: dict[str, int] = {}
_EXPORT_CONN: sqlite3.Connection | None = None
_EXPORT_CONN_KEY: tuple[str, int, int] | None = None


def get_project_index(refresh: bool = False) -> dict[str, list[str]]:
    global _PROJECT_INDEX
    _PROJECT_INDEX = get_cached_index(
        _PROJECT_INDEX,
        refresh,
        lambda: build_project_index(HERMES_DB),
    )
    return _PROJECT_INDEX


def discover_projects(
    index: dict[str, list[str]] | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    if index is None:
        index = get_project_index(refresh=True)
    if db_path is None:
        db_path = HERMES_DB
    size_map = build_session_size_map(db_path)
    return build_projects_from_index(
        index,
        SOURCE,
        build_project_name,
        lambda session_ids: sum(size_map.get(session_id, 0) for session_id in session_ids),
    )


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> Iterable[dict]:
    session_ids = get_project_index().get(project_dir_name, [])
    if not session_ids or not HERMES_DB.exists():
        return ()

    project_name = build_project_name(project_dir_name)

    def iter_sessions() -> Iterator[dict]:
        try:
            with connect_readonly(HERMES_DB) as conn:
                yield from collect_project_sessions(
                    session_ids,
                    lambda session_id: _parse_session_with_connection(
                        conn,
                        session_id=session_id,
                        anonymizer=anonymizer,
                        include_thinking=include_thinking,
                        target_session_source=project_dir_name,
                    ),
                    project_name,
                    SOURCE,
                )
        except (sqlite3.Error, OSError) as e:
            logger.warning("Failed to open Hermes database %s: %s", HERMES_DB, e)
            return

    return iter_sessions()


def build_export_session_tasks(project_index: int, project: dict) -> list[ExportSessionTask]:
    size_map = build_session_size_map()
    tasks: list[ExportSessionTask] = []
    for task_index, session_id in enumerate(get_project_index().get(project["dir_name"], [])):
        tasks.append(
            ExportSessionTask(
                source=SOURCE,
                project_index=project_index,
                task_index=task_index,
                project_dir_name=project["dir_name"],
                project_display_name=project["display_name"],
                estimated_bytes=size_map.get(session_id, 0),
                kind="hermes",
                item_id=session_id,
            )
        )
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    if not task.item_id:
        return None
    if not HERMES_DB.exists():
        return None
    if HERMES_DB != _DEFAULT_HERMES_DB:
        return parse_session(task.item_id, HERMES_DB, anonymizer, include_thinking, task.project_dir_name)

    try:
        conn = get_export_connection(HERMES_DB)
        return _parse_session_with_connection(
            conn,
            session_id=task.item_id,
            anonymizer=anonymizer,
            include_thinking=include_thinking,
            target_session_source=task.project_dir_name,
        )
    except (sqlite3.Error, OSError) as e:
        close_export_connection()
        logger.warning("Failed to parse Hermes session %s: %s", task.item_id, e)
        return None


def build_project_name(session_source: str) -> str:
    return build_prefixed_project_name(SOURCE, session_source, UNKNOWN_HERMES_SOURCE)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri_path = db_path.expanduser().resolve()
    try:
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{uri_path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    configure_readonly_connection(conn)
    return conn


def configure_readonly_connection(conn: sqlite3.Connection) -> None:
    for pragma in (
        "PRAGMA query_only = ON",
        "PRAGMA cache_size = -65536",
        "PRAGMA mmap_size = 268435456",
    ):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            continue


def get_export_connection(db_path: Path) -> sqlite3.Connection:
    if db_path != _DEFAULT_HERMES_DB:
        return connect_readonly(db_path)

    global _EXPORT_CONN, _EXPORT_CONN_KEY
    try:
        resolved = db_path.expanduser().resolve()
        stat = resolved.stat()
    except OSError:
        raise

    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    if _EXPORT_CONN is None or _EXPORT_CONN_KEY != key:
        close_export_connection()
        _EXPORT_CONN = connect_readonly(db_path)
        _EXPORT_CONN_KEY = key
    return _EXPORT_CONN


def close_export_connection() -> None:
    global _EXPORT_CONN, _EXPORT_CONN_KEY
    if _EXPORT_CONN is not None:
        try:
            _EXPORT_CONN.close()
        except sqlite3.Error:
            pass
    _EXPORT_CONN = None
    _EXPORT_CONN_KEY = None


atexit.register(close_export_connection)


def build_project_index(db_path: Path) -> dict[str, list[str]]:
    if not db_path.exists():
        return {}

    index: dict[str, list[str]] = {}
    query = """
        SELECT id, source
        FROM sessions
        WHERE COALESCE(message_count, 0) > 0
        ORDER BY started_at DESC, id DESC
    """
    try:
        with connect_readonly(db_path) as conn:
            rows = conn.execute(query)
            for session_id, session_source in rows:
                if not isinstance(session_id, str) or not session_id:
                    continue
                normalized_source = (
                    session_source if isinstance(session_source, str) and session_source.strip() else UNKNOWN_HERMES_SOURCE
                )
                index.setdefault(normalized_source, []).append(session_id)
    except (sqlite3.Error, OSError) as e:
        logger.warning("Failed to query Hermes database %s: %s", db_path, e)
        return {}
    return index


def build_session_size_map(db_path: Path | None = None) -> dict[str, int]:
    global _SESSION_SIZE_MAP
    if db_path is None:
        db_path = HERMES_DB
    if _SESSION_SIZE_MAP and db_path == HERMES_DB:
        return _SESSION_SIZE_MAP
    if not db_path.exists():
        return {}

    try:
        db_size = db_path.stat().st_size
    except OSError:
        db_size = 0

    query = """
        SELECT id, COALESCE(message_count, 0)
        FROM sessions
        WHERE COALESCE(message_count, 0) > 0
    """
    sizes: dict[str, int] = {}
    try:
        with connect_readonly(db_path) as conn:
            rows = list(conn.execute(query))
    except (sqlite3.Error, OSError):
        return {}

    total_messages = sum(safe_int(row[1]) for row in rows)
    if total_messages <= 0:
        return {}
    for session_id, message_count in rows:
        if isinstance(session_id, str) and session_id:
            sizes[session_id] = int(db_size * (safe_int(message_count) / total_messages))

    if db_path == HERMES_DB:
        _SESSION_SIZE_MAP = sizes
    return sizes


def parse_session(
    session_id: str,
    db_path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_session_source: str,
) -> dict | None:
    if not db_path.exists():
        return None

    try:
        with connect_readonly(db_path) as conn:
            return _parse_session_with_connection(
                conn,
                session_id=session_id,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_session_source=target_session_source,
            )
    except (sqlite3.Error, OSError) as e:
        logger.warning("Failed to parse Hermes session %s: %s", session_id, e)
        return None


def _parse_session_with_connection(
    conn: sqlite3.Connection,
    session_id: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_session_source: str,
) -> dict | None:
    del anonymizer
    session_row = conn.execute(
        """
        SELECT id, source, model, started_at, ended_at, message_count, tool_call_count,
               input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        FROM sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if session_row is None:
        return None

    session_source = session_row["source"]
    normalized_source = session_source if isinstance(session_source, str) and session_source.strip() else UNKNOWN_HERMES_SOURCE
    if normalized_source != target_session_source:
        return None

    metadata: dict[str, Any] = {
        "session_id": session_id,
        "git_branch": None,
        "model": session_row["model"] if isinstance(session_row["model"], str) else None,
        "start_time": normalize_hermes_timestamp(session_row["started_at"]),
        "end_time": normalize_hermes_timestamp(session_row["ended_at"]),
    }
    stats = make_stats()
    stats["input_tokens"] = (
        safe_int(session_row["input_tokens"])
        + safe_int(session_row["cache_read_tokens"])
        + safe_int(session_row["cache_write_tokens"])
    )
    stats["output_tokens"] = safe_int(session_row["output_tokens"])

    messages: list[dict[str, Any]] = []
    pending_tool_uses: dict[str, dict[str, Any]] = {}
    pending_tool_results: dict[str, dict[str, Any]] = {}

    rows = conn.execute(
        """
        SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp, token_count,
               finish_reason, reasoning, reasoning_content, reasoning_details,
               codex_reasoning_items, codex_message_items
        FROM messages
        WHERE session_id = ?
        ORDER BY timestamp ASC, id ASC
        """,
        (session_id,),
    )
    for row in rows:
        timestamp = normalize_hermes_timestamp(row["timestamp"])
        role = row["role"]
        if role == "user":
            msg = build_text_message("user", row["content"], timestamp)
            if msg is None:
                continue
            messages.append(msg)
            stats["user_messages"] += 1
            update_time_bounds(metadata, timestamp)
        elif role == "assistant":
            msg = build_assistant_message(row, timestamp, include_thinking, pending_tool_uses, pending_tool_results)
            if msg is None:
                continue
            messages.append(msg)
            stats["assistant_messages"] += 1
            stats["tool_uses"] += len(msg.get("tool_uses", []))
            update_time_bounds(metadata, timestamp)
        elif role == "tool":
            apply_tool_result_row(row, pending_tool_uses, pending_tool_results)

    if metadata["model"] is None:
        metadata["model"] = "hermes-unknown"

    return make_session_result(metadata, messages, stats)


def normalize_hermes_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    if abs(timestamp) > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def build_text_message(role: str, content: Any, timestamp: str | None) -> dict[str, Any] | None:
    if not isinstance(content, str) or not content.strip():
        return None
    return {
        "role": role,
        "content": content.strip(),
        "timestamp": timestamp,
    }


def build_assistant_message(
    row: sqlite3.Row,
    timestamp: str | None,
    include_thinking: bool,
    pending_tool_uses: dict[str, dict[str, Any]],
    pending_tool_results: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    msg: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
    content = row["content"]
    if isinstance(content, str) and content.strip():
        msg["content"] = content.strip()

    if include_thinking:
        thinking = extract_thinking(row)
        if thinking:
            msg["thinking"] = thinking

    tool_uses = parse_tool_calls(row["tool_calls"])
    if tool_uses:
        msg["tool_uses"] = tool_uses
        for tool_use in tool_uses:
            call_id = tool_use.get("id")
            if not isinstance(call_id, str) or not call_id:
                continue
            pending_tool_uses[call_id] = tool_use
            result = pending_tool_results.pop(call_id, None)
            if result is not None:
                tool_use.update(result)

    if len(msg) <= 2:
        return None
    return msg


def parse_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tool_calls, str) or not raw_tool_calls.strip():
        return []
    try:
        calls = json.loads(raw_tool_calls)
    except json.JSONDecodeError:
        return []
    if not isinstance(calls, list):
        return []

    tool_uses: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        tool_use: dict[str, Any] = {
            "tool": name,
            "input": parse_tool_input(parse_tool_arguments(function.get("arguments"))),
        }
        call_id = call.get("call_id") or call.get("id")
        if isinstance(call_id, str) and call_id:
            tool_use["id"] = call_id
        tool_uses.append(tool_use)
    return tool_uses


def parse_tool_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments if arguments is not None else {}
    if not arguments.strip():
        return {}
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {"raw": arguments}


def apply_tool_result_row(
    row: sqlite3.Row,
    pending_tool_uses: dict[str, dict[str, Any]],
    pending_tool_results: dict[str, dict[str, Any]],
) -> None:
    call_id = row["tool_call_id"]
    if not isinstance(call_id, str) or not call_id:
        return

    output: dict[str, Any] = {}
    content = row["content"]
    if isinstance(content, str) and content.strip():
        output["text"] = content

    result: dict[str, Any] = {"status": "success"}
    if output:
        result["output"] = output

    tool_use = pending_tool_uses.get(call_id)
    if tool_use is not None:
        tool_use.update(result)
    else:
        pending_tool_results[call_id] = result


def extract_thinking(row: sqlite3.Row) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    raw_seen: set[str] = set()
    for key in (
        "reasoning_content",
        "reasoning",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
    ):
        value = row[key]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped in raw_seen:
                continue
            raw_seen.add(stripped)
        text = stringify_thinking_value(value)
        if text and text not in seen:
            parts.append(text)
            seen.add(text)
    if not parts:
        return None
    return "\n\n".join(parts)


def stringify_thinking_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    if stripped[0] not in "[{\"-0123456789tfn":
        return stripped
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    extracted = extract_text_fragments(parsed)
    if extracted:
        return "\n\n".join(extracted)
    return json.dumps(parsed, sort_keys=True)


def extract_text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, str):
        if value.strip():
            fragments.append(value.strip())
    elif isinstance(value, dict):
        for key in ("text", "summary", "content", "reasoning", "thinking"):
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                fragments.append(child.strip())
        for child in value.values():
            if isinstance(child, (dict, list)):
                fragments.extend(extract_text_fragments(child))
    elif isinstance(value, list):
        for item in value:
            fragments.extend(extract_text_fragments(item))
    return list(dict.fromkeys(fragments))
