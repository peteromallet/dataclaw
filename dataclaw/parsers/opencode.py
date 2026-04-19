import logging
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ..anonymizer import Anonymizer
from ..export_tasks import ExportSessionTask
from .common import (
    build_prefixed_project_name,
    build_projects_from_index,
    collect_project_sessions,
    get_cached_index,
    load_json_field,
    make_session_result,
    make_stats,
    normalize_timestamp,
    parse_tool_input,
    safe_int,
    update_time_bounds,
)

logger = logging.getLogger(__name__)

SOURCE = "opencode"
OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"
OPENCODE_DB_PATH = OPENCODE_DIR / "opencode.db"
UNKNOWN_OPENCODE_CWD = "<unknown-cwd>"

_PROJECT_INDEX: dict[str, list[str]] = {}
_SESSION_SIZE_MAP: dict[str, int] = {}


def get_project_index(refresh: bool = False) -> dict[str, list[str]]:
    global _PROJECT_INDEX
    _PROJECT_INDEX = get_cached_index(
        _PROJECT_INDEX,
        refresh,
        lambda: build_project_index(OPENCODE_DB_PATH),
    )
    return _PROJECT_INDEX


def discover_projects(
    index: dict[str, list[str]] | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    if index is None:
        index = get_project_index(refresh=True)
    if db_path is None:
        db_path = OPENCODE_DB_PATH
    total_sessions = sum(len(session_ids) for session_ids in index.values())
    db_size = db_path.stat().st_size if db_path.exists() else 0
    return build_projects_from_index(
        index,
        SOURCE,
        build_project_name,
        lambda session_ids: int(db_size * (len(session_ids) / total_sessions)) if total_sessions else 0,
    )


def build_project_name(cwd: str) -> str:
    return build_prefixed_project_name(SOURCE, cwd, UNKNOWN_OPENCODE_CWD)


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> Iterable[dict]:
    session_ids = get_project_index().get(project_dir_name, [])
    if not session_ids or not OPENCODE_DB_PATH.exists():
        return ()

    project_name = build_project_name(project_dir_name)

    def iter_sessions() -> Iterator[dict]:
        try:
            with sqlite3.connect(OPENCODE_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                yield from collect_project_sessions(
                    session_ids,
                    lambda session_id: _parse_session_with_connection(
                        conn,
                        session_id=session_id,
                        anonymizer=anonymizer,
                        include_thinking=include_thinking,
                        target_cwd=project_dir_name,
                    ),
                    project_name,
                    SOURCE,
                )
        except (sqlite3.Error, OSError) as e:
            logger.warning("Failed to open OpenCode database %s: %s", OPENCODE_DB_PATH, e)
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
                kind="opencode",
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
    return parse_session(task.item_id, OPENCODE_DB_PATH, anonymizer, include_thinking, task.project_dir_name)


def build_session_size_map() -> dict[str, int]:
    global _SESSION_SIZE_MAP
    if _SESSION_SIZE_MAP:
        return _SESSION_SIZE_MAP
    if not OPENCODE_DB_PATH.exists():
        return {}

    query = """
        WITH message_sizes AS (
          SELECT session_id, SUM(LENGTH(data)) AS total FROM message GROUP BY session_id
        ),
        part_sizes AS (
          SELECT session_id, SUM(LENGTH(data)) AS total FROM part GROUP BY session_id
        )
        SELECT s.id, COALESCE(ms.total, 0) + COALESCE(ps.total, 0)
        FROM session s
        LEFT JOIN message_sizes ms ON ms.session_id = s.id
        LEFT JOIN part_sizes ps ON ps.session_id = s.id
    """

    try:
        with sqlite3.connect(OPENCODE_DB_PATH) as conn:
            _SESSION_SIZE_MAP = {session_id: int(total or 0) for session_id, total in conn.execute(query)}
            return _SESSION_SIZE_MAP
    except sqlite3.Error:
        return {}


def build_project_index(db_path: Path) -> dict[str, list[str]]:
    if not db_path.exists():
        return {}

    index: dict[str, list[str]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT id, directory FROM session ORDER BY time_updated DESC, id DESC")
    except sqlite3.Error as e:
        logger.warning("Failed to query OpenCode database %s: %s", db_path, e)
        return {}

    for session_id, cwd in rows:
        normalized_cwd = cwd if isinstance(cwd, str) and cwd.strip() else UNKNOWN_OPENCODE_CWD
        if not isinstance(session_id, str) or not session_id:
            continue
        index.setdefault(normalized_cwd, []).append(session_id)
    return index


def parse_session(
    session_id: str,
    db_path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    if not db_path.exists():
        return None

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return _parse_session_with_connection(
                conn,
                session_id=session_id,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=target_cwd,
            )
    except (sqlite3.Error, OSError) as e:
        logger.warning("Failed to parse OpenCode session %s: %s", session_id, e)
        return None


def _parse_session_with_connection(
    conn: sqlite3.Connection,
    session_id: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = make_stats()

    try:
        session_row = conn.execute(
            "SELECT id, directory, time_created, time_updated FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            return None

        raw_cwd = session_row["directory"]
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            if raw_cwd != target_cwd:
                return None
        elif target_cwd != UNKNOWN_OPENCODE_CWD:
            return None

        metadata["start_time"] = normalize_timestamp(session_row["time_created"])
        metadata["end_time"] = normalize_timestamp(session_row["time_updated"])

        message_rows = conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
            (session_id,),
        )

        for message_row in message_rows:
            message_data = load_json_field(message_row["data"])
            role = message_data.get("role")
            timestamp = normalize_timestamp(message_row["time_created"])

            model = extract_model(message_data)
            if metadata["model"] is None and model:
                metadata["model"] = model

            parts = iter_message_parts(conn, message_row["id"])

            if role == "user":
                msg = extract_user_message(parts)
                if msg is not None:
                    msg["timestamp"] = timestamp
                    messages.append(msg)
                    stats["user_messages"] += 1
                    update_time_bounds(metadata, timestamp)
            elif role == "assistant":
                msg = extract_assistant_content(parts, include_thinking)
                if msg:
                    msg["timestamp"] = timestamp
                    messages.append(msg)
                    stats["assistant_messages"] += 1
                    stats["tool_uses"] += len(msg.get("tool_uses", []))
                    update_time_bounds(metadata, timestamp)

                tokens = message_data.get("tokens", {})
                if isinstance(tokens, dict):
                    cache = tokens.get("cache", {})
                    cache_read = safe_int(cache.get("read")) if isinstance(cache, dict) else 0
                    cache_write = safe_int(cache.get("write")) if isinstance(cache, dict) else 0
                    stats["input_tokens"] += safe_int(tokens.get("input")) + cache_read + cache_write
                    stats["output_tokens"] += safe_int(tokens.get("output"))
    except (sqlite3.Error, OSError) as e:
        logger.warning("Failed to parse OpenCode session %s: %s", session_id, e)
        return None

    if metadata["model"] is None:
        metadata["model"] = "opencode-unknown"

    return make_session_result(metadata, messages, stats, anonymizer=anonymizer)


def extract_model(message_data: dict[str, Any]) -> str | None:
    model = message_data.get("model")
    if not isinstance(model, dict):
        return None
    provider_id = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider_id, str) and provider_id.strip() and isinstance(model_id, str) and model_id.strip():
        return f"{provider_id}/{model_id}"
    if isinstance(model_id, str) and model_id.strip():
        return model_id
    return None


def iter_message_parts(conn: sqlite3.Connection, message_id: str) -> Iterator[dict[str, Any]]:
    rows = conn.execute(
        "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC, id ASC",
        (message_id,),
    )
    for part_row in rows:
        yield load_json_field(part_row["data"])


def build_opencode_file_source(url: Any, mime: Any) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url:
        return None

    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(",", 1)
        media_type = mime if isinstance(mime, str) and mime else header[5:].split(";", 1)[0]
        return {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        }

    if url.startswith("file://"):
        source: dict[str, Any] = {
            "type": "url",
            "url": url,
        }
    else:
        source = {"type": "url", "url": url}

    if isinstance(mime, str) and mime:
        source["media_type"] = mime
    return source


def extract_opencode_file_part(part: dict[str, Any]) -> dict[str, Any] | None:
    source = build_opencode_file_source(part.get("url"), part.get("mime"))
    if source is None:
        return None

    mime = part.get("mime")
    if isinstance(mime, str) and mime.startswith("image/"):
        return {"type": "image", "source": source}
    return {"type": "document", "source": source}


def extract_user_message(parts: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    text_parts: list[str] = []
    content_parts: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        elif part_type == "file":
            content_part = extract_opencode_file_part(part)
            if content_part is not None:
                content_parts.append(content_part)

    if not text_parts and not content_parts:
        return None

    message: dict[str, Any] = {"role": "user"}
    if text_parts:
        message["content"] = "\n\n".join(text_parts)
    if content_parts:
        message["content_parts"] = content_parts
    return message


def extract_assistant_content(
    parts: Iterable[dict[str, Any]],
    include_thinking: bool,
) -> dict[str, Any] | None:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")

        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        elif part_type == "reasoning" and include_thinking:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                thinking_parts.append(text.strip())
        elif part_type == "tool":
            tool_name = part.get("tool")
            state = part.get("state", {})
            tool_input = state.get("input", {}) if isinstance(state, dict) else {}
            tool_use: dict[str, Any] = {
                "tool": tool_name,
                "input": parse_tool_input(tool_input),
            }
            if isinstance(state, dict):
                status = state.get("status")
                if isinstance(status, str):
                    tool_use["status"] = "success" if status == "completed" else status
                output = state.get("output")
                if isinstance(output, str) and output:
                    tool_use["output"] = {"text": output}
                elif output is not None:
                    tool_use["output"] = {}
            tool_uses.append(tool_use)

    if not text_parts and not thinking_parts and not tool_uses:
        return None

    msg: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    if thinking_parts:
        msg["thinking"] = "\n\n".join(thinking_parts)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg
