import logging
from collections.abc import Iterable
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

SOURCE = "openclaw"
OPENCLAW_DIR = Path.home() / ".openclaw"
OPENCLAW_AGENTS_DIR = OPENCLAW_DIR / "agents"
UNKNOWN_OPENCLAW_CWD = "<unknown-cwd>"

_PROJECT_INDEX: dict[str, list[Path]] = {}


def get_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _PROJECT_INDEX
    _PROJECT_INDEX = get_cached_index(
        _PROJECT_INDEX,
        refresh,
        lambda: build_project_index(OPENCLAW_AGENTS_DIR),
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


def build_project_name(cwd: str) -> str:
    return build_prefixed_project_name(SOURCE, cwd, UNKNOWN_OPENCLAW_CWD)


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> Iterable[dict]:
    session_files = get_project_index().get(project_dir_name, [])
    return collect_project_sessions(
        session_files,
        lambda session_file: parse_session_file(session_file, anonymizer, include_thinking),
        build_project_name(project_dir_name),
        SOURCE,
    )


def build_project_index(agents_dir: Path) -> dict[str, list[Path]]:
    """Scan ~/.openclaw/agents/*/sessions/*.jsonl and index by cwd."""
    if not agents_dir.exists():
        return {}

    index: dict[str, list[Path]] = {}
    try:
        for agent_dir in sorted(agents_dir.iterdir()):
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for session_file in sorted(sessions_dir.glob("*.jsonl")):
                cwd = extract_cwd(session_file) or UNKNOWN_OPENCLAW_CWD
                index.setdefault(cwd, []).append(session_file)
    except OSError as e:
        logger.warning("Failed to scan OpenClaw agents directory %s: %s", agents_dir, e)
    return index


def extract_cwd(session_file: Path) -> str | None:
    """Read the first line (session header) of an OpenClaw JSONL file to extract cwd."""
    try:
        with open(session_file) as f:
            first_line = f.readline().strip()
            if not first_line:
                return None
            header = json.loads(first_line)
            if header.get("type") != "session":
                return None
            cwd = header.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                return cwd
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse JSON in %s: %s", session_file, e)
    except OSError as e:
        logger.warning("Failed to read %s: %s", session_file, e)
    return None


def parse_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Parse an OpenClaw session JSONL file into a structured conversation."""
    try:
        header_entries = iter_jsonl(filepath)
        header = next(header_entries, None)
    except OSError as e:
        logger.warning("Failed to read OpenClaw session file %s: %s", filepath, e)
        return None

    if header is None:
        return None

    if header.get("type") != "session":
        return None

    metadata: dict[str, Any] = {
        "session_id": header.get("id", filepath.stem),
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": header.get("timestamp"),
        "end_time": None,
    }
    cwd = header.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        metadata["cwd"] = anonymizer.path(cwd)

    messages: list[dict[str, Any]] = []
    stats = make_stats()

    try:
        tool_result_map = build_tool_result_map(filepath, anonymizer)

        for entry in iter_entries_after_header(filepath):
            entry_type = entry.get("type")
            timestamp = entry.get("timestamp")

            if entry_type == "model_change":
                provider = entry.get("provider", "")
                model_id = entry.get("modelId", "")
                if model_id:
                    metadata["model"] = f"{provider}/{model_id}" if provider else model_id

            if entry_type != "message":
                continue

            msg_data = entry.get("message", {})
            role = msg_data.get("role")
            msg_ts = msg_data.get("timestamp")
            if isinstance(msg_ts, (int, float)):
                msg_ts = normalize_timestamp(msg_ts)
            effective_ts = msg_ts or timestamp

            if role == "user":
                content = msg_data.get("content")
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    text = "\n".join(text_parts)
                elif isinstance(content, str):
                    text = content
                else:
                    continue
                if not text.strip():
                    continue
                messages.append(
                    {
                        "role": "user",
                        "content": anonymizer.text(text.strip()),
                        "timestamp": effective_ts,
                    }
                )
                stats["user_messages"] += 1
                update_time_bounds(metadata, effective_ts)

            elif role == "assistant":
                model = msg_data.get("model")
                if model and metadata["model"] is None:
                    provider = msg_data.get("provider", "")
                    metadata["model"] = f"{provider}/{model}" if provider else model

                usage = msg_data.get("usage", {})
                if isinstance(usage, dict):
                    stats["input_tokens"] += safe_int(usage.get("input")) + safe_int(usage.get("cacheRead"))
                    stats["output_tokens"] += safe_int(usage.get("output"))

                content = msg_data.get("content", [])
                if not isinstance(content, list):
                    continue

                text_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_uses: list[dict[str, Any]] = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")

                    if block_type == "text":
                        text = block.get("text", "")
                        if isinstance(text, str) and text.strip():
                            text_parts.append(anonymizer.text(text.strip()))

                    elif block_type == "thinking" and include_thinking:
                        thinking = block.get("thinking", "")
                        if isinstance(thinking, str) and thinking.strip():
                            thinking_parts.append(anonymizer.text(thinking.strip()))

                    elif block_type == "toolCall":
                        tool_name = block.get("name")
                        args = block.get("arguments", {})
                        tool_entry: dict[str, Any] = {
                            "tool": tool_name,
                            "input": parse_tool_input(tool_name, args, anonymizer),
                        }
                        tool_call_id = block.get("id")
                        if tool_call_id and tool_call_id in tool_result_map:
                            result = tool_result_map[tool_call_id]
                            if result.get("output"):
                                tool_entry["output"] = result["output"]
                            if result.get("status"):
                                tool_entry["status"] = result["status"]
                        tool_uses.append(tool_entry)

                if not text_parts and not thinking_parts and not tool_uses:
                    continue

                msg: dict[str, Any] = {"role": "assistant"}
                if effective_ts:
                    msg["timestamp"] = effective_ts
                if text_parts:
                    msg["content"] = "\n\n".join(text_parts)
                if thinking_parts:
                    msg["thinking"] = "\n\n".join(thinking_parts)
                if tool_uses:
                    msg["tool_uses"] = tool_uses
                    stats["tool_uses"] += len(tool_uses)

                messages.append(msg)
                stats["assistant_messages"] += 1
                update_time_bounds(metadata, effective_ts)

            elif role == "bashExecution":
                command = msg_data.get("command", "")
                output = msg_data.get("output", "")
                exit_code = msg_data.get("exitCode")
                is_error = exit_code is not None and exit_code != 0
                tool_entry: dict[str, Any] = {
                    "tool": "bash",
                    "input": {"command": anonymizer.text(command)} if command else {},
                }
                out_dict: dict[str, Any] = {}
                if output:
                    out_dict["text"] = anonymizer.text(output.strip())
                if exit_code is not None:
                    out_dict["exit_code"] = exit_code
                if out_dict:
                    tool_entry["output"] = out_dict
                tool_entry["status"] = "error" if is_error else "success"
                messages.append(
                    {
                        "role": "assistant",
                        "tool_uses": [tool_entry],
                        "timestamp": effective_ts,
                    }
                )
                stats["assistant_messages"] += 1
                stats["tool_uses"] += 1
                update_time_bounds(metadata, effective_ts)
    except OSError as e:
        logger.warning("Failed to read OpenClaw session file %s: %s", filepath, e)
        return None

    if metadata["model"] is None:
        metadata["model"] = "openclaw-unknown"

    return make_session_result(metadata, messages, stats)


def iter_entries_after_header(filepath: Path):
    entries = iter_jsonl(filepath)
    next(entries, None)
    yield from entries


def build_tool_result_map(filepath: Path, anonymizer: Anonymizer) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for entry in iter_entries_after_header(filepath):
        if entry.get("type") != "message":
            continue
        msg_data = entry.get("message", {})
        if msg_data.get("role") != "toolResult":
            continue
        tool_call_id = msg_data.get("toolCallId")
        if not tool_call_id:
            continue
        is_error = bool(msg_data.get("isError"))
        content = msg_data.get("content", [])
        if isinstance(content, list):
            text_parts = [
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            ]
            output_text = "\n".join(text_parts).strip()
        elif isinstance(content, str):
            output_text = content.strip()
        else:
            output_text = ""
        result[tool_call_id] = {
            "output": {"text": anonymizer.text(output_text)} if output_text else {},
            "status": "error" if is_error else "success",
        }
    return result
