import re
from pathlib import Path
from typing import Any

from ..anonymizer import Anonymizer
from ..secrets import should_skip_large_binary_string
from .common import (
    anonymize_value,
    collect_project_sessions,
    iter_jsonl,
    make_session_result,
    make_stats,
    normalize_timestamp,
    parse_tool_input,
    sum_existing_path_sizes,
    update_time_bounds,
)

SOURCE = "claude"
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def discover_projects(projects_dir: Path | None = None) -> list[dict]:
    if projects_dir is None:
        projects_dir = PROJECTS_DIR
    if not projects_dir.exists():
        return []

    projects = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        root_sessions = list(project_dir.glob("*.jsonl"))
        subagent_sessions = find_subagent_sessions(project_dir)
        total_count = len(root_sessions) + len(subagent_sessions)
        if total_count == 0:
            continue
        total_size = sum_existing_path_sizes(root_sessions)
        for session_dir in subagent_sessions:
            for sa_file in (session_dir / "subagents").glob("agent-*.jsonl"):
                total_size += sa_file.stat().st_size
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": build_project_name(project_dir.name),
                "session_count": total_count,
                "total_size_bytes": total_size,
                "source": "claude",
            }
        )
    return projects


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    projects_dir: Path | None = None,
) -> list[dict]:
    if projects_dir is None:
        projects_dir = PROJECTS_DIR

    project_path = projects_dir / project_dir_name
    if not project_path.exists():
        return []

    project_name = build_project_name(project_dir_name)
    sessions = collect_project_sessions(
        sorted(project_path.glob("*.jsonl")),
        lambda session_file: parse_session_file(session_file, anonymizer, include_thinking),
        project_name,
        SOURCE,
    )
    sessions.extend(
        collect_project_sessions(
            find_subagent_sessions(project_path),
            lambda session_dir: parse_subagent_session(session_dir, anonymizer, include_thinking),
            project_name,
            SOURCE,
        )
    )
    return sessions


def build_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build a map of tool_use_id -> {output, status} from tool_result blocks."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "user":
            continue
        content_blocks = entry.get("message", {}).get("content", [])
        if not isinstance(content_blocks, list):
            continue
        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            output = build_tool_result_output(block, entry, anonymizer)
            result[tid] = {
                "output": output,
                "status": "error" if block.get("is_error") else "success",
            }
    return result


def build_tool_result_output(
    block: dict[str, Any],
    entry: dict[str, Any],
    anonymizer: Anonymizer,
) -> dict[str, Any]:
    text, raw_content = parse_tool_result_content(block.get("content"), anonymizer)
    if text is None:
        text = extract_tool_result_text(entry.get("toolUseResult"), anonymizer)

    raw_result = sanitize_tool_use_result(entry.get("toolUseResult"), text, anonymizer)
    source_tool_uuid = entry.get("sourceToolAssistantUUID")
    if isinstance(source_tool_uuid, str) and source_tool_uuid:
        if raw_result is None:
            raw_result = {"sourceToolAssistantUUID": source_tool_uuid}
        else:
            raw_result = {**raw_result, "sourceToolAssistantUUID": source_tool_uuid}

    output: dict[str, Any] = {}
    if text:
        output["text"] = text

    raw = merge_tool_result_raw(raw_content, raw_result)
    if raw is not None:
        output["raw"] = raw
    return output


def parse_tool_result_content(content: Any, anonymizer: Anonymizer) -> tuple[str | None, Any]:
    if isinstance(content, str):
        text = normalize_tool_result_text(content, anonymizer)
        if text is not None:
            return text, None
        if should_skip_large_binary_string(content):
            return None, content
        return None, None

    if isinstance(content, list):
        text_parts: list[str] = []
        raw_parts: list[Any] = []
        for part in content:
            if isinstance(part, dict):
                anonymized_part = anonymize_value("content", part, anonymizer)
                if part.get("type") == "text":
                    part_text = extract_tool_result_text(anonymized_part, anonymizer=None)
                    if part_text:
                        text_parts.append(part_text)
                    raw_part = prune_empty_values(drop_duplicate_text_fields(anonymized_part, part_text))
                    if raw_part is not None and raw_part != {"type": "text"}:
                        raw_parts.append(raw_part)
                    continue
                raw_parts.append(anonymized_part)
                continue
            raw_parts.append(anonymize_value("content", part, anonymizer))

        text = "\n\n".join(text_parts).strip() if text_parts else None
        return text or None, prune_empty_values(raw_parts)

    if isinstance(content, dict):
        anonymized_content = anonymize_value("content", content, anonymizer)
        text = extract_tool_result_text(anonymized_content, anonymizer=None)
        raw = prune_empty_values(drop_duplicate_text_fields(anonymized_content, text))
        if raw == {"type": "text"}:
            raw = None
        return text, raw

    return None, prune_empty_values(anonymize_value("content", content, anonymizer))


def extract_tool_result_text(value: Any, anonymizer: Anonymizer | None) -> str | None:
    if isinstance(value, str):
        return normalize_tool_result_text(value, anonymizer)

    if isinstance(value, list):
        text_parts = []
        for part in value:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = normalize_tool_result_text(part.get("text"), anonymizer)
            if text:
                text_parts.append(text)
        if text_parts:
            return "\n\n".join(text_parts)
        return None

    if not isinstance(value, dict):
        return None

    for candidate in (value.get("stdout"), value.get("content"), value.get("text")):
        text = normalize_tool_result_text(candidate, anonymizer)
        if text:
            return text

    file_info = value.get("file")
    if isinstance(file_info, dict):
        return normalize_tool_result_text(file_info.get("content"), anonymizer)

    return None


def normalize_tool_result_text(value: Any, anonymizer: Anonymizer | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or should_skip_large_binary_string(text):
        return None
    if anonymizer is None:
        return text
    return anonymizer.text(text)


def sanitize_tool_use_result(
    tool_use_result: Any,
    text: str | None,
    anonymizer: Anonymizer,
) -> dict[str, Any] | None:
    if tool_use_result is None:
        return None

    if isinstance(tool_use_result, str):
        if should_skip_large_binary_string(tool_use_result):
            return {"text": tool_use_result}
        sanitized_text = normalize_tool_result_text(tool_use_result, anonymizer)
        if not sanitized_text or text_matches_tool_result(sanitized_text, text):
            return None
        return {"text": sanitized_text}

    sanitized = anonymize_value("toolUseResult", tool_use_result, anonymizer)
    sanitized = drop_redundant_result_fields(sanitized)
    sanitized = drop_duplicate_text_fields(sanitized, text)
    pruned = prune_empty_values(sanitized)
    if pruned is None:
        return None
    if isinstance(pruned, dict):
        return pruned
    return {"value": pruned}


def drop_redundant_result_fields(value: Any) -> Any:
    if isinstance(value, dict):
        redundant_keys = set()
        if value.get("type") == "create":
            # Claude create results repeat the full created file contents that are
            # already present in the assistant Write tool input, so keeping them
            # again in `output.raw` mostly doubles the largest payload in export.
            redundant_keys.add("content")
        # Claude edit results repeat the same edit delta that is already present
        # in the assistant tool input (`old_string` / `new_string`), so keeping
        # these fields again in `output.raw` mostly adds export size, not signal.
        redundant_keys.update({"oldString", "newString", "structuredPatch"})
        return {key: drop_redundant_result_fields(item) for key, item in value.items() if key not in redundant_keys}
    if isinstance(value, list):
        return [drop_redundant_result_fields(item) for item in value]
    return value


def drop_duplicate_text_fields(value: Any, text: str | None, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: drop_duplicate_text_fields(v, text, k) for k, v in value.items()}
    if isinstance(value, list):
        return [drop_duplicate_text_fields(item, text) for item in value]
    if isinstance(value, str) and (key is None or key in {"stdout", "content", "text"}):
        if text_matches_tool_result(value, text):
            return None
    return value


def text_matches_tool_result(value: str, text: str | None) -> bool:
    if text is None:
        return False
    normalized = value.strip()
    comparable_value = normalize_comparable_tool_text(normalized)
    comparable_text = normalize_comparable_tool_text(text)
    if comparable_value == comparable_text:
        return True
    if comparable_value and comparable_text:
        if comparable_value in comparable_text or comparable_text in comparable_value:
            return True
    if normalized == text:
        return True
    return normalized.startswith("Error: ") and normalized[7:].strip() == text


def normalize_comparable_tool_text(text: str) -> str:
    lines = []
    for line in text.strip().splitlines():
        lines.append(re.sub(r"^\s*\d+→", "", line))
    return "\n".join(lines).strip()


def prune_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {}
        for key, item in value.items():
            cleaned = prune_empty_values(item)
            if cleaned in (None, "", [], {}):
                continue
            pruned[key] = cleaned
        return pruned or None

    if isinstance(value, list):
        pruned = [cleaned for item in value if (cleaned := prune_empty_values(item)) not in (None, "", [], {})]
        return pruned or None

    if value == "":
        return None
    return value


def merge_tool_result_raw(raw_content: Any, raw_result: dict[str, Any] | None) -> Any:
    if raw_content is None:
        return raw_result
    if raw_result is None:
        return {"content": raw_content}
    if isinstance(raw_content, str) and raw_result.get("text") == raw_content:
        merged = {key: value for key, value in raw_result.items() if key != "text"}
        if not merged:
            return {"content": raw_content}
        return {"content": raw_content, **merged}
    return {"content": raw_content, "toolUseResult": raw_result}


def parse_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    messages: list[dict[str, Any]] = []
    metadata = {
        "session_id": filepath.stem,
        "cwd": None,
        "git_branch": None,
        "claude_version": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = make_stats()

    try:
        entries = list(iter_jsonl(filepath))
    except OSError:
        return None

    tool_result_map = build_tool_result_map(entries, anonymizer)
    for entry in entries:
        process_entry(
            entry,
            messages,
            metadata,
            stats,
            anonymizer,
            include_thinking,
            tool_result_map,
        )

    return make_session_result(metadata, messages, stats)


def find_subagent_sessions(project_dir: Path) -> list[Path]:
    """Find session directories that contain Claude subagent logs."""
    sessions = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir():
            continue
        subagent_dir = entry / "subagents"
        if subagent_dir.is_dir() and any(subagent_dir.glob("agent-*.jsonl")):
            sessions.append(entry)
    return sessions


def find_subagent_only_sessions(project_dir: Path) -> list[Path]:
    """Find session directories that have subagent data but no root-level JSONL."""
    root_stems = {f.stem for f in project_dir.glob("*.jsonl")}
    return [entry for entry in find_subagent_sessions(project_dir) if entry.name not in root_stems]


def parse_subagent_session(
    session_dir: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Merge subagent JSONL files into a single session and parse it."""
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.is_dir():
        return None

    timed_entries: list[tuple[str, dict[str, Any]]] = []
    for sa_file in sorted(subagent_dir.glob("agent-*.jsonl")):
        for entry in iter_jsonl(sa_file):
            ts = entry.get("timestamp", "")
            timed_entries.append((ts if isinstance(ts, str) else "", entry))

    if not timed_entries:
        return None

    timed_entries.sort(key=lambda pair: pair[0])

    messages: list[dict[str, Any]] = []
    metadata = {
        "session_id": session_dir.name,
        "cwd": None,
        "git_branch": None,
        "claude_version": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = make_stats()

    entries = [entry for _ts, entry in timed_entries]
    tool_result_map = build_tool_result_map(entries, anonymizer)
    for entry in entries:
        process_entry(
            entry,
            messages,
            metadata,
            stats,
            anonymizer,
            include_thinking,
            tool_result_map,
        )

    metadata["session_id"] = resolve_subagent_session_id(session_dir, metadata["session_id"])
    return make_session_result(metadata, messages, stats)


def resolve_subagent_session_id(session_dir: Path, session_id: str) -> str:
    root_session_file = session_dir.parent / f"{session_dir.name}.jsonl"
    if root_session_file.exists():
        return f"{session_id}:subagents"
    return session_id


def process_entry(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: Anonymizer,
    include_thinking: bool,
    tool_result_map: dict[str, dict] | None = None,
) -> None:
    entry_type = entry.get("type")

    if metadata["cwd"] is None and entry.get("cwd"):
        metadata["cwd"] = anonymizer.path(entry["cwd"])
        metadata["git_branch"] = entry.get("gitBranch")
        metadata["claude_version"] = entry.get("version")
        metadata["session_id"] = entry.get("sessionId", metadata["session_id"])

    timestamp = normalize_timestamp(entry.get("timestamp"))

    if entry_type == "user":
        content = extract_user_content(entry, anonymizer)
        if content is not None:
            messages.append({"role": "user", "content": content, "timestamp": timestamp})
            stats["user_messages"] += 1
            update_time_bounds(metadata, timestamp)

    elif entry_type == "assistant":
        msg = extract_assistant_content(entry, anonymizer, include_thinking, tool_result_map)
        if msg:
            if metadata["model"] is None:
                metadata["model"] = entry.get("message", {}).get("model")
            usage = entry.get("message", {}).get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            stats["input_tokens"] += usage.get("input_tokens", 0) + usage.get(
                "cache_read_input_tokens",
                0,
            )
            stats["output_tokens"] += usage.get("output_tokens", 0)
            stats["tool_uses"] += len(msg.get("tool_uses", []))
            msg["timestamp"] = timestamp
            messages.append(msg)
            stats["assistant_messages"] += 1
            update_time_bounds(metadata, timestamp)


def extract_user_content(entry: dict[str, Any], anonymizer: Anonymizer) -> str | None:
    msg_data = entry.get("message", {})
    content = msg_data.get("content", "")
    if isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        content = "\n".join(text_parts)
    if not content or not content.strip():
        return None
    return anonymizer.text(content)


def extract_assistant_content(
    entry: dict[str, Any],
    anonymizer: Anonymizer,
    include_thinking: bool,
    tool_result_map: dict[str, dict] | None = None,
) -> dict[str, Any] | None:
    msg_data = entry.get("message", {})
    content_blocks = msg_data.get("content", [])
    if not isinstance(content_blocks, list):
        return None

    text_parts = []
    thinking_parts = []
    tool_uses = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "").strip()
            if text:
                text_parts.append(anonymizer.text(text))
        elif block_type == "thinking" and include_thinking:
            thinking = block.get("thinking", "").strip()
            if thinking:
                thinking_parts.append(anonymizer.text(thinking))
        elif block_type == "tool_use":
            tu: dict[str, Any] = {
                "tool": block.get("name"),
                "input": parse_tool_input(block.get("name"), block.get("input", {}), anonymizer),
            }
            if tool_result_map is not None:
                result = tool_result_map.get(block.get("id", ""))
                if result:
                    tu["output"] = result["output"]
                    tu["status"] = result["status"]
            tool_uses.append(tu)

    if not text_parts and not tool_uses and not thinking_parts:
        return None

    msg: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    if thinking_parts:
        msg["thinking"] = "\n\n".join(thinking_parts)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg


def build_project_name(dir_name: str) -> str:
    """Convert a hyphen-encoded project dir name to a human-readable name."""
    parts = dir_name.lstrip("-").split("-")
    common_dirs = {"Documents", "Downloads", "Desktop"}

    home_idx = -1
    for i, part in enumerate(parts):
        if part in {"Users", "home"}:
            home_idx = i
            break

    if home_idx >= 0:
        if len(parts) > home_idx + 3 and parts[home_idx + 2] in common_dirs:
            meaningful = parts[home_idx + 3 :]
        elif len(parts) > home_idx + 2 and parts[home_idx + 2] not in common_dirs:
            meaningful = parts[home_idx + 2 :]
        else:
            meaningful = []
    else:
        meaningful = parts

    if meaningful:
        return "-".join(meaningful)

    if home_idx >= 0:
        if len(parts) == home_idx + 3 and parts[home_idx + 2] in common_dirs:
            return f"~{parts[home_idx + 2]}"
        if len(parts) == home_idx + 2:
            return "~home"

    return dir_name.strip("-") or "unknown"
