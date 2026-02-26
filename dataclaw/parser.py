"""Parse Claude Code and Codex session JSONL files into structured conversations."""

import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .anonymizer import Anonymizer
from .secrets import redact_text

logger = logging.getLogger(__name__)

CLAUDE_SOURCE = "claude"
CODEX_SOURCE = "codex"
GEMINI_SOURCE = "gemini"

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_ARCHIVED_DIR = CODEX_DIR / "archived_sessions"
UNKNOWN_CODEX_CWD = "<unknown-cwd>"

GEMINI_DIR = Path.home() / ".gemini" / "tmp"

_CODEX_PROJECT_INDEX: dict[str, list[Path]] = {}
_GEMINI_HASH_MAP: dict[str, str] = {}


def _build_gemini_hash_map() -> dict[str, str]:
    """Build a mapping from SHA-256 hash prefix to directory path.

    Gemini CLI names project dirs by hashing the absolute working directory path.
    We scan first-level dirs under $HOME to reverse this mapping.
    """
    result: dict[str, str] = {}
    home = Path.home()
    try:
        for entry in home.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                h = hashlib.sha256(str(entry).encode()).hexdigest()
                result[h] = str(entry)
    except OSError:
        pass
    return result


def _extract_project_path_from_sessions(project_hash: str) -> str | None:
    """Try to extract the project working directory from session tool call file paths."""
    chats_dir = GEMINI_DIR / project_hash / "chats"
    if not chats_dir.exists():
        return None
    for session_file in sorted(chats_dir.glob("session-*.json"), reverse=True):
        try:
            data = json.loads(session_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for msg in data.get("messages", []):
            for tc in msg.get("toolCalls", []):
                fp = tc.get("args", {}).get("file_path") or tc.get("args", {}).get("path", "")
                if fp.startswith("/"):
                    # Extract the shallowest directory and verify its hash matches
                    parts = Path(fp).parts  # e.g. ('/', 'home', 'wd', 'project', ...)
                    for depth in range(3, len(parts)):
                        candidate = str(Path(*parts[:depth + 1]))
                        if hashlib.sha256(candidate.encode()).hexdigest() == project_hash:
                            return candidate
        # Only check the most recent session file with tool calls
        break
    return None


def _resolve_gemini_hash(project_hash: str) -> str:
    """Resolve a Gemini project hash to a readable directory name.

    Strategy:
    1. Check hash map built from first-level dirs under $HOME.
    2. Fallback: extract path from session file tool call args.
    3. Last resort: return first 8 chars of the hash.
    """
    global _GEMINI_HASH_MAP
    if not _GEMINI_HASH_MAP:
        _GEMINI_HASH_MAP = _build_gemini_hash_map()
    full_path = _GEMINI_HASH_MAP.get(project_hash)
    if full_path:
        return Path(full_path).name
    # Fallback: try extracting from session files
    extracted = _extract_project_path_from_sessions(project_hash)
    if extracted:
        _GEMINI_HASH_MAP[project_hash] = extracted  # cache it
        return Path(extracted).name
    return project_hash[:8]


def _iter_jsonl(filepath: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/malformed lines."""
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def discover_projects() -> list[dict]:
    """Discover Claude Code, Codex, and Gemini CLI projects with session counts."""
    projects = _discover_claude_projects()
    projects.extend(_discover_codex_projects())
    projects.extend(_discover_gemini_projects())
    return sorted(projects, key=lambda p: (p["display_name"], p["source"]))


def _discover_claude_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        root_sessions = list(project_dir.glob("*.jsonl"))
        subagent_sessions = _find_subagent_only_sessions(project_dir)
        total_count = len(root_sessions) + len(subagent_sessions)
        if total_count == 0:
            continue
        total_size = sum(f.stat().st_size for f in root_sessions)
        for session_dir in subagent_sessions:
            for sa_file in (session_dir / "subagents").glob("agent-*.jsonl"):
                total_size += sa_file.stat().st_size
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": _build_project_name(project_dir.name),
                "session_count": total_count,
                "total_size_bytes": total_size,
                "source": CLAUDE_SOURCE,
            }
        )
    return projects


def _discover_codex_projects() -> list[dict]:
    index = _get_codex_project_index(refresh=True)
    projects = []
    for cwd, session_files in sorted(index.items()):
        if not session_files:
            continue
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_codex_project_name(cwd),
                "session_count": len(session_files),
                "total_size_bytes": sum(f.stat().st_size for f in session_files),
                "source": CODEX_SOURCE,
            }
        )
    return projects


def _discover_gemini_projects() -> list[dict]:
    if not GEMINI_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(GEMINI_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "bin":
            continue
        chats_dir = project_dir / "chats"
        if not chats_dir.exists():
            continue
        sessions = list(chats_dir.glob("session-*.json"))
        if not sessions:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": f"gemini:{_resolve_gemini_hash(project_dir.name)}",
                "session_count": len(sessions),
                "total_size_bytes": sum(f.stat().st_size for f in sessions),
                "source": GEMINI_SOURCE,
            }
        )
    return projects


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    source: str = CLAUDE_SOURCE,
) -> list[dict]:
    """Parse all sessions for a project into structured dicts."""
    if source == GEMINI_SOURCE:
        project_path = GEMINI_DIR / project_dir_name / "chats"
        if not project_path.exists():
            return []
        sessions = []
        for session_file in sorted(project_path.glob("session-*.json")):
            parsed = _parse_gemini_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = f"gemini:{_resolve_gemini_hash(project_dir_name)}"
                parsed["source"] = GEMINI_SOURCE
                sessions.append(parsed)
        return sessions

    if source == CODEX_SOURCE:
        index = _get_codex_project_index()
        session_files = index.get(project_dir_name, [])
        sessions = []
        for session_file in session_files:
            parsed = _parse_codex_session_file(
                session_file,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=project_dir_name,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_codex_project_name(project_dir_name)
                parsed["source"] = CODEX_SOURCE
                sessions.append(parsed)
        return sessions

    project_path = PROJECTS_DIR / project_dir_name
    if not project_path.exists():
        return []

    sessions = []
    for session_file in sorted(project_path.glob("*.jsonl")):
        parsed = _parse_claude_session_file(session_file, anonymizer, include_thinking)
        if parsed and parsed["messages"]:
            parsed["project"] = _build_project_name(project_dir_name)
            parsed["source"] = CLAUDE_SOURCE
            sessions.append(parsed)

    for session_dir in _find_subagent_only_sessions(project_path):
        parsed = _parse_subagent_session(session_dir, anonymizer, include_thinking)
        if parsed and parsed["messages"]:
            parsed["project"] = _build_project_name(project_dir_name)
            parsed["source"] = CLAUDE_SOURCE
            sessions.append(parsed)

    return sessions


def _make_stats() -> dict[str, int]:
    return {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _make_session_result(
    metadata: dict[str, Any], messages: list[dict[str, Any]], stats: dict[str, int],
) -> dict[str, Any] | None:
    if not messages:
        return None
    return {
        "session_id": metadata["session_id"],
        "model": metadata["model"],
        "git_branch": metadata["git_branch"],
        "start_time": metadata["start_time"],
        "end_time": metadata["end_time"],
        "messages": messages,
        "stats": stats,
    }


def _parse_claude_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
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
    stats = _make_stats()

    try:
        for entry in _iter_jsonl(filepath):
            _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking)
    except OSError:
        return None

    return _make_session_result(metadata, messages, stats)


def _parse_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    """Backward-compatible alias for the Claude parser used by tests."""
    return _parse_claude_session_file(filepath, anonymizer, include_thinking)


def _find_subagent_only_sessions(project_dir: Path) -> list[Path]:
    """Find session directories that have subagent data but no root-level JSONL.

    Some Claude Code sessions (especially those run entirely via the Task tool)
    store conversation data only in ``<uuid>/subagents/agent-*.jsonl`` without
    writing a root-level ``<uuid>.jsonl`` file.  This function identifies those
    directories so they can be parsed separately.
    """
    root_stems = {f.stem for f in project_dir.glob("*.jsonl")}
    sessions = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir() or entry.name in root_stems:
            continue
        subagent_dir = entry / "subagents"
        if subagent_dir.is_dir() and any(subagent_dir.glob("agent-*.jsonl")):
            sessions.append(entry)
    return sessions


def _parse_subagent_session(
    session_dir: Path, anonymizer: Anonymizer, include_thinking: bool = True,
) -> dict | None:
    """Merge subagent JSONL files into a single session and parse it.

    Reads all ``agent-*.jsonl`` files from the session's ``subagents/``
    directory, sorts entries by timestamp, and feeds them through the
    standard Claude entry processor.
    """
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.is_dir():
        return None

    # Collect all entries with their timestamps for sorting.
    timed_entries: list[tuple[str, dict[str, Any]]] = []
    for sa_file in sorted(subagent_dir.glob("agent-*.jsonl")):
        for entry in _iter_jsonl(sa_file):
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
    stats = _make_stats()

    for _ts, entry in timed_entries:
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking)

    return _make_session_result(metadata, messages, stats)


def _parse_gemini_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    messages = []
    metadata = {
        "session_id": data.get("sessionId", filepath.stem),
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": data.get("startTime"),
        "end_time": data.get("lastUpdated"),
    }
    stats = _make_stats()

    for msg_data in data.get("messages", []):
        msg_type = msg_data.get("type")
        timestamp = msg_data.get("timestamp")

        if msg_type == "user":
            content = msg_data.get("content")
            if isinstance(content, list):
                text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and "text" in part]
                text = "\n".join(text_parts)
            elif isinstance(content, str):
                text = content
            else:
                continue
            if not text.strip():
                continue
            messages.append({
                "role": "user",
                "content": anonymizer.text(text.strip()),
                "timestamp": timestamp,
            })
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

        elif msg_type == "gemini":
            if metadata["model"] is None:
                metadata["model"] = msg_data.get("model")

            tokens = msg_data.get("tokens", {})
            if tokens:
                stats["input_tokens"] += tokens.get("input", 0) + tokens.get("cached", 0)
                stats["output_tokens"] += tokens.get("output", 0)

            msg = {"role": "assistant"}
            if timestamp:
                msg["timestamp"] = timestamp

            content = msg_data.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = anonymizer.text(content.strip())

            if include_thinking:
                thoughts = msg_data.get("thoughts", [])
                if thoughts:
                    thought_texts = []
                    for t in thoughts:
                        if "description" in t and isinstance(t["description"], str):
                            thought_texts.append(t["description"].strip())
                    if thought_texts:
                        msg["thinking"] = anonymizer.text("\n\n".join(thought_texts))

            tool_uses = []
            for tc in msg_data.get("toolCalls", []):
                tool_name = tc.get("name")
                args_data = tc.get("args", {})
                tool_uses.append({
                    "tool": tool_name,
                    "input": _summarize_tool_input(tool_name, args_data, anonymizer)
                })

            if tool_uses:
                msg["tool_uses"] = tool_uses
                stats["tool_uses"] += len(tool_uses)

            if "content" in msg or "thinking" in msg or "tool_uses" in msg:
                messages.append(msg)
                stats["assistant_messages"] += 1
                _update_time_bounds(metadata, timestamp)

    return _make_session_result(metadata, messages, stats)


@dataclasses.dataclass
class _CodexParseState:
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    stats: dict[str, int] = dataclasses.field(default_factory=_make_stats)
    pending_tool_uses: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    pending_thinking: list[str] = dataclasses.field(default_factory=list)
    _pending_thinking_seen: set[str] = dataclasses.field(default_factory=set)
    raw_cwd: str = UNKNOWN_CODEX_CWD
    max_input_tokens: int = 0
    max_output_tokens: int = 0


def _parse_codex_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    state = _CodexParseState(
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
        for entry in _iter_jsonl(filepath):
            timestamp = _normalize_timestamp(entry.get("timestamp"))
            entry_type = entry.get("type")

            if entry_type == "session_meta":
                _handle_codex_session_meta(state, entry, filepath, anonymizer)
            elif entry_type == "turn_context":
                _handle_codex_turn_context(state, entry, anonymizer)
            elif entry_type == "response_item":
                _handle_codex_response_item(state, entry, anonymizer, include_thinking)
            elif entry_type == "event_msg":
                payload = entry.get("payload", {})
                event_type = payload.get("type")
                if event_type == "token_count":
                    _handle_codex_token_count(state, payload)
                elif event_type == "agent_reasoning" and include_thinking:
                    thinking = payload.get("text")
                    if isinstance(thinking, str) and thinking.strip():
                        cleaned = anonymizer.text(thinking.strip())
                        if cleaned not in state._pending_thinking_seen:
                            state._pending_thinking_seen.add(cleaned)
                            state.pending_thinking.append(cleaned)
                elif event_type == "user_message":
                    _handle_codex_user_message(state, payload, timestamp, anonymizer)
                elif event_type == "agent_message":
                    _handle_codex_agent_message(state, payload, timestamp, anonymizer, include_thinking)
    except OSError:
        return None

    state.stats["input_tokens"] = state.max_input_tokens
    state.stats["output_tokens"] = state.max_output_tokens

    if state.raw_cwd != target_cwd:
        return None

    _flush_codex_pending(state, timestamp=state.metadata["end_time"])

    if state.metadata["model"] is None:
        model_provider = state.metadata.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            state.metadata["model"] = f"{model_provider}-codex"
        else:
            state.metadata["model"] = "codex-unknown"

    return _make_session_result(state.metadata, state.messages, state.stats)


def _handle_codex_session_meta(
    state: _CodexParseState, entry: dict[str, Any], filepath: Path,
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


def _handle_codex_turn_context(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
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


def _handle_codex_response_item(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    payload = entry.get("payload", {})
    item_type = payload.get("type")
    if item_type == "function_call":
        tool_name = payload.get("name")
        args_data = _parse_codex_tool_arguments(payload.get("arguments"))
        state.pending_tool_uses.append(
            {
                "tool": tool_name,
                "input": _summarize_tool_input(tool_name, args_data, anonymizer),
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


def _handle_codex_token_count(state: _CodexParseState, payload: dict[str, Any]) -> None:
    info = payload.get("info", {})
    if isinstance(info, dict):
        total_usage = info.get("total_token_usage", {})
        if isinstance(total_usage, dict):
            input_tokens = _safe_int(total_usage.get("input_tokens"))
            cached_tokens = _safe_int(total_usage.get("cached_input_tokens"))
            output_tokens = _safe_int(total_usage.get("output_tokens"))
            state.max_input_tokens = max(state.max_input_tokens, input_tokens + cached_tokens)
            state.max_output_tokens = max(state.max_output_tokens, output_tokens)


def _handle_codex_user_message(
    state: _CodexParseState, payload: dict[str, Any],
    timestamp: str | None, anonymizer: Anonymizer,
) -> None:
    _flush_codex_pending(state, timestamp)
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
        _update_time_bounds(state.metadata, timestamp)


def _handle_codex_agent_message(
    state: _CodexParseState, payload: dict[str, Any],
    timestamp: str | None, anonymizer: Anonymizer, include_thinking: bool,
) -> None:
    content = payload.get("message")
    msg: dict[str, Any] = {"role": "assistant"}
    if isinstance(content, str) and content.strip():
        msg["content"] = anonymizer.text(content.strip())
    if state.pending_thinking and include_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = list(state.pending_tool_uses)

    if len(msg) > 1:
        msg["timestamp"] = timestamp
        state.messages.append(msg)
        state.stats["assistant_messages"] += 1
        state.stats["tool_uses"] += len(msg.get("tool_uses", []))
        _update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def _flush_codex_pending(state: _CodexParseState, timestamp: str | None) -> None:
    if not state.pending_tool_uses and not state.pending_thinking:
        return

    msg: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
    if state.pending_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = list(state.pending_tool_uses)

    state.messages.append(msg)
    state.stats["assistant_messages"] += 1
    state.stats["tool_uses"] += len(msg.get("tool_uses", []))
    _update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def _parse_codex_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        return parsed
    return arguments


def _update_time_bounds(metadata: dict[str, Any], timestamp: str | None) -> None:
    if timestamp is None:
        return
    if metadata["start_time"] is None:
        metadata["start_time"] = timestamp
    metadata["end_time"] = timestamp


def _safe_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _get_codex_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _CODEX_PROJECT_INDEX
    if refresh or not _CODEX_PROJECT_INDEX:
        _CODEX_PROJECT_INDEX = _build_codex_project_index()
    return _CODEX_PROJECT_INDEX


def _build_codex_project_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for session_file in _iter_codex_session_files():
        cwd = _extract_codex_cwd(session_file) or UNKNOWN_CODEX_CWD
        index.setdefault(cwd, []).append(session_file)
    return index


def _iter_codex_session_files() -> list[Path]:
    files: list[Path] = []
    if CODEX_SESSIONS_DIR.exists():
        files.extend(sorted(CODEX_SESSIONS_DIR.rglob("*.jsonl")))
    if CODEX_ARCHIVED_DIR.exists():
        files.extend(sorted(CODEX_ARCHIVED_DIR.glob("*.jsonl")))
    return files


def _extract_codex_cwd(session_file: Path) -> str | None:
    try:
        for entry in _iter_jsonl(session_file):
            if entry.get("type") in ("session_meta", "turn_context"):
                cwd = entry.get("payload", {}).get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
    except OSError:
        return None
    return None


def _build_codex_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_CODEX_CWD:
        return "codex:unknown"
    return f"codex:{Path(cwd).name or cwd}"


def _process_entry(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    entry_type = entry.get("type")

    if metadata["cwd"] is None and entry.get("cwd"):
        metadata["cwd"] = anonymizer.path(entry["cwd"])
        metadata["git_branch"] = entry.get("gitBranch")
        metadata["claude_version"] = entry.get("version")
        metadata["session_id"] = entry.get("sessionId", metadata["session_id"])

    timestamp = _normalize_timestamp(entry.get("timestamp"))

    if entry_type == "user":
        content = _extract_user_content(entry, anonymizer)
        if content is not None:
            messages.append({"role": "user", "content": content, "timestamp": timestamp})
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

    elif entry_type == "assistant":
        msg = _extract_assistant_content(entry, anonymizer, include_thinking)
        if msg:
            if metadata["model"] is None:
                metadata["model"] = entry.get("message", {}).get("model")
            usage = entry.get("message", {}).get("usage", {})
            stats["input_tokens"] += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            stats["output_tokens"] += usage.get("output_tokens", 0)
            stats["tool_uses"] += len(msg.get("tool_uses", []))
            msg["timestamp"] = timestamp
            messages.append(msg)
            stats["assistant_messages"] += 1
            _update_time_bounds(metadata, timestamp)


def _extract_user_content(entry: dict[str, Any], anonymizer: Anonymizer) -> str | None:
    msg_data = entry.get("message", {})
    content = msg_data.get("content", "")
    if isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        content = "\n".join(text_parts)
    if not content or not content.strip():
        return None
    return anonymizer.text(content)


def _extract_assistant_content(
    entry: dict[str, Any], anonymizer: Anonymizer, include_thinking: bool,
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
            tool_uses.append({
                "tool": block.get("name"),
                "input": _summarize_tool_input(block.get("name"), block.get("input", {}), anonymizer),
            })

    if not text_parts and not tool_uses and not thinking_parts:
        return None

    msg = {"role": "assistant"}
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    if thinking_parts:
        msg["thinking"] = "\n\n".join(thinking_parts)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg


MAX_TOOL_INPUT_LENGTH = 300


def _redact_and_truncate(text: str, anonymizer: Anonymizer) -> str:
    """Redact secrets BEFORE truncating to avoid partial secret leaks."""
    text, _ = redact_text(text)
    return anonymizer.text(text[:MAX_TOOL_INPUT_LENGTH])


def _summarize_file_path(d: dict, a: Anonymizer) -> str:
    return a.path(d.get("file_path", ""))


def _summarize_write(d: dict, a: Anonymizer) -> str:
    return f"{a.path(d.get('file_path', ''))} ({len(d.get('content', ''))} chars)"


def _summarize_bash(d: dict, a: Anonymizer) -> str:
    return _redact_and_truncate(d.get("command", ""), a)


def _summarize_grep(d: dict, a: Anonymizer) -> str:
    pattern, _ = redact_text(d.get("pattern", ""))
    return f"pattern={a.text(pattern)} path={a.path(d.get('path', ''))}"


def _summarize_glob(d: dict, a: Anonymizer) -> str:
    return f"pattern={a.text(d.get('pattern', ''))} path={a.path(d.get('path', ''))}"


_TOOL_SUMMARIZERS: dict[str, Any] = {
    "read": _summarize_file_path,
    "edit": _summarize_file_path,
    "write": _summarize_write,
    "bash": _summarize_bash,
    "grep": _summarize_grep,
    "glob": _summarize_glob,
    "task": lambda d, a: _redact_and_truncate(d.get("prompt", ""), a),
    "websearch": lambda d, _: d.get("query", ""),
    "webfetch": lambda d, _: d.get("url", ""),
}


def _summarize_tool_input(tool_name: str | None, input_data: Any, anonymizer: Anonymizer) -> str:
    """Summarize tool input for export."""
    if not isinstance(input_data, dict):
        return _redact_and_truncate(str(input_data), anonymizer)

    name = tool_name.lower() if tool_name else ""
    summarizer = _TOOL_SUMMARIZERS.get(name)
    if summarizer is not None:
        return summarizer(input_data, anonymizer)
    return _redact_and_truncate(str(input_data), anonymizer)


def _normalize_timestamp(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return None


def _build_project_name(dir_name: str) -> str:
    """Convert a hyphen-encoded project dir name to a human-readable name.

    Examples: '-Users-alice-Documents-myapp' -> 'myapp'
              '-home-bob-project' -> 'project'
              'standalone' -> 'standalone'
    """
    path = dir_name.replace("-", "/")
    path = path.lstrip("/")
    parts = path.split("/")
    common_dirs = {"Documents", "Downloads", "Desktop"}

    if len(parts) >= 2 and parts[0] == "Users":
        if len(parts) >= 4 and parts[2] in common_dirs:
            meaningful = parts[3:]
        elif len(parts) >= 3 and parts[2] not in common_dirs:
            meaningful = parts[2:]
        else:
            meaningful = []
    elif len(parts) >= 2 and parts[0] == "home":
        meaningful = parts[2:] if len(parts) > 2 else []
    else:
        meaningful = parts

    if meaningful:
        segments = dir_name.lstrip("-").split("-")
        prefix_parts = len(parts) - len(meaningful)
        return "-".join(segments[prefix_parts:]) or dir_name
    else:
        if len(parts) >= 2 and parts[0] in ("Users", "home"):
            if len(parts) == 2:
                return "~home"
            if len(parts) == 3 and parts[2] in common_dirs:
                return f"~{parts[2]}"
        return dir_name.strip("-") or "unknown"
