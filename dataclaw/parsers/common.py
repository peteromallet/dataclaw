import json as stdlib_json
import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _json as json
from ..anonymizer import Anonymizer
from ..secrets import should_skip_large_binary_string, should_skip_structured_string_transform

logger = logging.getLogger(__name__)

_NON_ANON_STRING_KEYS = frozenset(
    {
        "session_id",
        "model",
        "git_branch",
        "start_time",
        "end_time",
        "role",
        "timestamp",
        "tool",
        "status",
        "type",
        "media_type",
        "mime_type",
        "id",
        "tool_use_id",
        "sourceToolAssistantUUID",
        "source",
        "project",
        "wall_time",
    }
)


def iter_jsonl(filepath: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/malformed lines."""
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed JSON line in %s: %s", filepath, e)
                continue


def make_stats() -> dict[str, int]:
    return {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def make_session_result(
    metadata: dict[str, Any],
    messages: list[dict[str, Any]],
    stats: dict[str, int],
    anonymizer: Anonymizer | None = None,
) -> dict[str, Any] | None:
    if not messages:
        return None
    session = {
        "session_id": metadata["session_id"],
        "model": metadata["model"],
        "git_branch": metadata["git_branch"],
        "start_time": metadata["start_time"],
        "end_time": metadata["end_time"],
        "messages": messages,
        "stats": stats,
    }
    if anonymizer is None:
        return session
    return anonymize_session(session, anonymizer)


def update_time_bounds(metadata: dict[str, Any], timestamp: str | None) -> None:
    if timestamp is None:
        return
    if metadata["start_time"] is None:
        metadata["start_time"] = timestamp
    metadata["end_time"] = timestamp


def safe_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _escape_invalid_unicode_text(text: str) -> str:
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text

    parts: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:
            parts.append(f"\\x{code - 0xDC00:02x}")
        elif 0xD800 <= code <= 0xDFFF:
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(ch)
    return "".join(parts)


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _escape_invalid_unicode_text(value)
    if isinstance(value, dict):
        return {_escape_invalid_unicode_text(str(k)): _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    return value


def load_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            try:
                parsed = _sanitize_json_value(stdlib_json.loads(value))
            except stdlib_json.JSONDecodeError:
                logger.warning("Failed to parse JSON field: %s", e)
                return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return None


def _should_skip_anonymizing_string(key: str | None, value: str, parent_dict: dict[str, Any] | None) -> bool:
    if key in _NON_ANON_STRING_KEYS:
        return True
    return should_skip_structured_string_transform(key, value, parent_dict)


def _anonymize_session_value(
    key: str | None,
    value: Any,
    anonymizer: Anonymizer,
    parent_dict: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    if isinstance(value, str):
        if _should_skip_anonymizing_string(key, value, parent_dict):
            return value, False
        if should_skip_large_binary_string(value):
            return value, False
        anonymized = anonymizer.text(value)
        return anonymized, anonymized != value

    if isinstance(value, dict):
        out: dict[str, Any] | None = None
        for child_key, child_value in value.items():
            anonymized_child, changed = _anonymize_session_value(child_key, child_value, anonymizer, value)
            if not changed:
                continue
            if out is None:
                out = dict(value)
            out[child_key] = anonymized_child
        if out is None:
            return value, False
        return out, True

    if isinstance(value, list):
        out_list: list[Any] | None = None
        for idx, item in enumerate(value):
            anonymized_item, changed = _anonymize_session_value(key, item, anonymizer, parent_dict)
            if not changed:
                continue
            if out_list is None:
                out_list = list(value)
            out_list[idx] = anonymized_item
        if out_list is None:
            return value, False
        return out_list, True

    return value, False


def anonymize_session(session: dict[str, Any], anonymizer: Anonymizer) -> dict[str, Any]:
    anonymized, _changed = _anonymize_session_value(None, session, anonymizer)
    if isinstance(anonymized, dict):
        return anonymized
    return session


def parse_tool_input(input_data: Any) -> dict:
    """Return a structured dict for a tool's input args without anonymizing it yet."""
    if not isinstance(input_data, dict):
        return {"raw": str(input_data)}

    return input_data


def get_cached_index(
    current_index: dict[str, list[Any]],
    refresh: bool,
    load_index: Callable[[], dict[str, list[Any]]],
) -> dict[str, list[Any]]:
    if refresh or not current_index:
        return load_index()
    return current_index


def build_prefixed_project_name(source: str, cwd: str, unknown_cwd: str) -> str:
    if cwd == unknown_cwd:
        return f"{source}:unknown"
    return f"{source}:{Path(cwd).name or cwd}"


def build_projects_from_index(
    index: dict[str, list[Any]],
    source: str,
    build_project_name: Callable[[str], str],
    get_total_size_bytes: Callable[[list[Any]], int],
) -> list[dict]:
    projects = []
    for cwd, items in sorted(index.items()):
        if not items:
            continue
        projects.append(
            {
                "dir_name": cwd,
                "display_name": build_project_name(cwd),
                "session_count": len(items),
                "total_size_bytes": get_total_size_bytes(items),
                "source": source,
            }
        )
    return projects


def sum_existing_path_sizes(paths: Iterable[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def count_existing_paths_and_sizes(paths: Iterable[Path]) -> tuple[int, int]:
    count = 0
    total_size = 0
    for path in paths:
        if not path.exists():
            continue
        count += 1
        total_size += path.stat().st_size
    return count, total_size


def collect_project_sessions(
    items: Iterable[Any],
    parse_item: Callable[[Any], dict | None],
    project_name: str,
    source: str,
    default_model: str | None = None,
) -> Iterator[dict]:
    for item in items:
        parsed = parse_item(item)
        if not parsed or not parsed.get("messages"):
            continue
        parsed["project"] = project_name
        parsed["source"] = source
        if default_model and not parsed.get("model"):
            parsed["model"] = default_model
        yield parsed
