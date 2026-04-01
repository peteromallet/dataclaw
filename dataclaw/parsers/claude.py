from pathlib import Path
from typing import Any

from ..anonymizer import Anonymizer
from .common import (
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
        subagent_sessions = find_subagent_only_sessions(project_dir)
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
            find_subagent_only_sessions(project_path),
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
        for block in entry.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            is_error = bool(block.get("is_error"))
            content = block.get("content", "")
            if isinstance(content, list):
                text = "\n\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
            else:
                text = str(content).strip() if content else ""
            result[tid] = {
                "output": {"text": anonymizer.text(text)} if text else {},
                "status": "error" if is_error else "success",
            }
    return result


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


def find_subagent_only_sessions(project_dir: Path) -> list[Path]:
    """Find session directories that have subagent data but no root-level JSONL."""
    root_stems = {f.stem for f in project_dir.glob("*.jsonl")}
    sessions = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir() or entry.name in root_stems:
            continue
        subagent_dir = entry / "subagents"
        if subagent_dir.is_dir() and any(subagent_dir.glob("agent-*.jsonl")):
            sessions.append(entry)
    return sessions


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

    return make_session_result(metadata, messages, stats)


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
            meaningful = parts[home_idx + 3:]
        elif len(parts) > home_idx + 2 and parts[home_idx + 2] not in common_dirs:
            meaningful = parts[home_idx + 2:]
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
