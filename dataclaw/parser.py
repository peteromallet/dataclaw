"""Parse Claude Code, Codex, Gemini CLI, OpenCode, and OpenClaw session data into conversations."""

import dataclasses
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .anonymizer import Anonymizer
from .secrets import redact_text

logger = logging.getLogger(__name__)

CLAUDE_SOURCE = "claude"
CODEX_SOURCE = "codex"
GEMINI_SOURCE = "gemini"
OPENCODE_SOURCE = "opencode"
OPENCLAW_SOURCE = "openclaw"
KIMI_SOURCE = "kimi"
CUSTOM_SOURCE = "custom"

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_ARCHIVED_DIR = CODEX_DIR / "archived_sessions"
UNKNOWN_CODEX_CWD = "<unknown-cwd>"

GEMINI_DIR = Path.home() / ".gemini" / "tmp"

OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"
OPENCODE_DB_PATH = OPENCODE_DIR / "opencode.db"
UNKNOWN_OPENCODE_CWD = "<unknown-cwd>"

OPENCLAW_DIR = Path.home() / ".openclaw"
OPENCLAW_AGENTS_DIR = OPENCLAW_DIR / "agents"
UNKNOWN_OPENCLAW_CWD = "<unknown-cwd>"

KIMI_DIR = Path.home() / ".kimi"
KIMI_SESSIONS_DIR = KIMI_DIR / "sessions"
KIMI_CONFIG_PATH = KIMI_DIR / "kimi.json"
UNKNOWN_KIMI_CWD = "<unknown-cwd>"

CUSTOM_DIR = Path.home() / ".dataclaw" / "custom"

# Backup directory — session files are copied here by `dataclaw backup-sessions`
# to survive automatic cleanup by coding tools (Claude Code, Gemini CLI, etc.)
BACKUP_BASE_DIR = Path.home() / ".dataclaw" / "session_backup"
CLAUDE_BACKUP_PROJECTS_DIR = BACKUP_BASE_DIR / "claude" / "projects"
GEMINI_BACKUP_DIR = BACKUP_BASE_DIR / "gemini"

_CODEX_PROJECT_INDEX: dict[str, list[Path]] = {}
_GEMINI_HASH_MAP: dict[str, str] = {}
_OPENCODE_PROJECT_INDEX: dict[str, list[str]] = {}
_OPENCLAW_PROJECT_INDEX: dict[str, list[Path]] = {}
_KIMI_PROJECT_INDEX: dict[str, list[Path]] = {}


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
    """Discover all supported source projects with session counts."""
    projects = _discover_claude_projects()
    projects.extend(_discover_codex_projects())
    projects.extend(_discover_gemini_projects())
    projects.extend(_discover_opencode_projects())
    projects.extend(_discover_openclaw_projects())
    projects.extend(_discover_kimi_projects())
    projects.extend(_discover_custom_projects())
    return sorted(projects, key=lambda p: (p["display_name"], p["source"]))


def _discover_claude_projects() -> list[dict]:
    base_dirs = [d for d in [PROJECTS_DIR, CLAUDE_BACKUP_PROJECTS_DIR] if d.exists()]
    if not base_dirs:
        return []

    # Collect all project dirs from both original and backup, merging by dir name
    all_project_dirs: dict[str, list[Path]] = {}
    for base_dir in base_dirs:
        for project_dir in sorted(base_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            all_project_dirs.setdefault(project_dir.name, []).append(project_dir)

    projects = []
    for dir_name, dirs in sorted(all_project_dirs.items()):
        # Merge session files across dirs, deduplicating by filename
        seen_files: dict[str, Path] = {}
        for d in dirs:
            for f in d.glob("*.jsonl"):
                seen_files.setdefault(f.name, f)

        # Collect subagent-only session dirs from all locations
        subagent_sessions: list[Path] = []
        for d in dirs:
            subagent_sessions.extend(_find_subagent_only_sessions(d))

        total_count = len(seen_files) + len(subagent_sessions)
        if total_count == 0:
            continue
        total_size = sum(f.stat().st_size for f in seen_files.values())
        for session_dir in subagent_sessions:
            for sa_file in (session_dir / "subagents").glob("agent-*.jsonl"):
                total_size += sa_file.stat().st_size
        projects.append(
            {
                "dir_name": dir_name,
                "display_name": _build_project_name(dir_name),
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
    base_dirs = [d for d in [GEMINI_DIR, GEMINI_BACKUP_DIR] if d.exists()]
    if not base_dirs:
        return []

    # Collect all project dirs from both original and backup, merging by hash name
    all_project_dirs: dict[str, list[Path]] = {}
    for base_dir in base_dirs:
        for project_dir in sorted(base_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name == "bin":
                continue
            chats_dir = project_dir / "chats"
            if not chats_dir.exists():
                continue
            all_project_dirs.setdefault(project_dir.name, []).append(project_dir)

    projects = []
    for project_hash, dirs in all_project_dirs.items():
        # Merge session files across dirs, deduplicating by filename
        seen_files: dict[str, Path] = {}
        for d in dirs:
            for f in (d / "chats").glob("session-*.json"):
                seen_files.setdefault(f.name, f)
        if not seen_files:
            continue
        projects.append(
            {
                "dir_name": project_hash,
                "display_name": f"gemini:{_resolve_gemini_hash(project_hash)}",
                "session_count": len(seen_files),
                "total_size_bytes": sum(f.stat().st_size for f in seen_files.values()),
                "source": GEMINI_SOURCE,
            }
        )
    return projects


def _discover_opencode_projects() -> list[dict]:
    index = _get_opencode_project_index(refresh=True)
    total_sessions = sum(len(session_ids) for session_ids in index.values())
    db_size = OPENCODE_DB_PATH.stat().st_size if OPENCODE_DB_PATH.exists() else 0

    projects = []
    for cwd, session_ids in sorted(index.items()):
        if not session_ids:
            continue
        estimated_size = int(db_size * (len(session_ids) / total_sessions)) if total_sessions else 0
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_opencode_project_name(cwd),
                "session_count": len(session_ids),
                "total_size_bytes": estimated_size,
                "source": OPENCODE_SOURCE,
            }
        )
    return projects


def _discover_openclaw_projects() -> list[dict]:
    index = _get_openclaw_project_index(refresh=True)
    projects = []
    for cwd, session_files in sorted(index.items()):
        if not session_files:
            continue
        total_size = sum(f.stat().st_size for f in session_files if f.exists())
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_openclaw_project_name(cwd),
                "session_count": len(session_files),
                "total_size_bytes": total_size,
                "source": OPENCLAW_SOURCE,
            }
        )
    return projects


def _load_kimi_work_dirs() -> dict[str, str]:
    """Load Kimi work directory mapping from config file."""
    if not KIMI_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(KIMI_CONFIG_PATH.read_text())
        work_dirs = data.get("work_dirs", [])
        return {
            entry.get("path", ""): entry.get("path", "")
            for entry in work_dirs
            if entry.get("path")
        }
    except (json.JSONDecodeError, OSError):
        return {}


def _get_kimi_project_hash(cwd: str) -> str:
    """Generate Kimi project hash from working directory path (MD5)."""
    return hashlib.md5(cwd.encode()).hexdigest()


def _discover_kimi_projects() -> list[dict]:
    if not KIMI_SESSIONS_DIR.exists():
        return []

    work_dirs = _load_kimi_work_dirs()
    path_to_hash = {path: _get_kimi_project_hash(path) for path in work_dirs}
    hash_to_path = {h: p for p, h in path_to_hash.items()}

    projects = []
    for project_dir in sorted(KIMI_SESSIONS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        project_hash = project_dir.name
        session_dirs = [d for d in project_dir.iterdir() if d.is_dir()]
        if not session_dirs:
            continue

        total_sessions = 0
        total_size = 0
        for session_dir in session_dirs:
            context_file = session_dir / "context.jsonl"
            if context_file.exists():
                total_sessions += 1
                total_size += context_file.stat().st_size

        if total_sessions == 0:
            continue

        project_path = hash_to_path.get(project_hash)
        if project_path:
            display_name = f"kimi:{Path(project_path).name}"
            dir_name = project_path
        else:
            display_name = f"kimi:{project_hash[:8]}"
            dir_name = project_hash

        projects.append(
            {
                "dir_name": dir_name,
                "display_name": display_name,
                "session_count": total_sessions,
                "total_size_bytes": total_size,
                "source": KIMI_SOURCE,
            }
        )
    return projects


def _build_kimi_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_KIMI_CWD:
        return "kimi:unknown"
    return f"kimi:{Path(cwd).name or cwd}"


def _discover_custom_projects() -> list[dict]:
    if not CUSTOM_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(CUSTOM_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        jsonl_files = list(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue
        session_count = 0
        total_size = 0
        for f in jsonl_files:
            total_size += f.stat().st_size
            try:
                session_count += sum(1 for line in f.open() if line.strip())
            except OSError:
                pass
        if session_count == 0:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": f"custom:{project_dir.name}",
                "session_count": session_count,
                "total_size_bytes": total_size,
                "source": CUSTOM_SOURCE,
            }
        )
    return projects


def _parse_custom_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
) -> list[dict]:
    project_path = CUSTOM_DIR / project_dir_name
    if not project_path.exists():
        return []

    required_fields = {"session_id", "model", "messages"}
    sessions = []
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        try:
            for line_num, line in enumerate(jsonl_file.open(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    session = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "custom:%s: %s line %d: invalid JSON, skipping",
                        project_dir_name, jsonl_file.name, line_num,
                    )
                    continue
                if not isinstance(session, dict):
                    logger.warning(
                        "custom:%s: %s line %d: not a JSON object, skipping",
                        project_dir_name, jsonl_file.name, line_num,
                    )
                    continue
                missing = required_fields - session.keys()
                if missing:
                    logger.warning(
                        "custom:%s: %s line %d: missing required fields %s, skipping",
                        project_dir_name, jsonl_file.name, line_num, sorted(missing),
                    )
                    continue
                session["project"] = f"custom:{project_dir_name}"
                session["source"] = CUSTOM_SOURCE
                # Redact message content through the anonymizer
                for msg in session.get("messages", []):
                    if "content" in msg and isinstance(msg["content"], str):
                        redacted, _ = redact_text(msg["content"])
                        msg["content"] = anonymizer.text(redacted)
                sessions.append(session)
        except OSError:
            logger.warning("custom:%s: failed to read %s", project_dir_name, jsonl_file.name)
    return sessions


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    source: str = CLAUDE_SOURCE,
) -> list[dict]:
    """Parse all sessions for a project into structured dicts."""
    if source == CUSTOM_SOURCE:
        return _parse_custom_sessions(project_dir_name, anonymizer)

    if source == KIMI_SOURCE:
        project_hash = _get_kimi_project_hash(project_dir_name)
        project_path = KIMI_SESSIONS_DIR / project_hash
        if not project_path.exists():
            return []
        sessions = []
        for session_dir in sorted(project_path.iterdir()):
            if not session_dir.is_dir():
                continue
            context_file = session_dir / "context.jsonl"
            if not context_file.exists():
                continue
            parsed = _parse_kimi_session_file(
                context_file,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_kimi_project_name(project_dir_name)
                parsed["source"] = KIMI_SOURCE
                if not parsed.get("model"):
                    parsed["model"] = "kimi-k2"
                sessions.append(parsed)
        return sessions

    if source == OPENCLAW_SOURCE:
        index = _get_openclaw_project_index()
        session_files = index.get(project_dir_name, [])
        sessions = []
        for session_file in session_files:
            parsed = _parse_openclaw_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = _build_openclaw_project_name(project_dir_name)
                parsed["source"] = OPENCLAW_SOURCE
                sessions.append(parsed)
        return sessions

    if source == GEMINI_SOURCE:
        # Merge session files from original dir and backup, deduplicating by filename
        session_file_map: dict[str, Path] = {}
        for base_dir in [GEMINI_DIR, GEMINI_BACKUP_DIR]:
            chats_dir = base_dir / project_dir_name / "chats"
            if chats_dir.exists():
                for f in chats_dir.glob("session-*.json"):
                    session_file_map.setdefault(f.name, f)
        if not session_file_map:
            return []
        sessions = []
        for session_file in sorted(session_file_map.values()):
            parsed = _parse_gemini_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = f"gemini:{_resolve_gemini_hash(project_dir_name)}"
                parsed["source"] = GEMINI_SOURCE
                sessions.append(parsed)
        return sessions

    if source == OPENCODE_SOURCE:
        index = _get_opencode_project_index()
        session_ids = index.get(project_dir_name, [])
        sessions = []
        for session_id in session_ids:
            parsed = _parse_opencode_session(
                session_id,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=project_dir_name,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_opencode_project_name(project_dir_name)
                parsed["source"] = OPENCODE_SOURCE
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

    # Merge session files from original dir and backup, deduplicating by filename
    session_file_map: dict[str, Path] = {}
    project_dirs = []
    for base_dir in [PROJECTS_DIR, CLAUDE_BACKUP_PROJECTS_DIR]:
        project_path = base_dir / project_dir_name
        if project_path.exists():
            project_dirs.append(project_path)
            for f in project_path.glob("*.jsonl"):
                session_file_map.setdefault(f.name, f)

    if not project_dirs:
        return []

    sessions = []
    seen_session_ids: set[str] = set()
    for session_file in sorted(session_file_map.values()):
        parsed = _parse_claude_session_file(session_file, anonymizer, include_thinking)
        if parsed and parsed["messages"]:
            session_id = parsed.get("session_id", "")
            if session_id and session_id in seen_session_ids:
                continue
            if session_id:
                seen_session_ids.add(session_id)
            parsed["project"] = _build_project_name(project_dir_name)
            parsed["source"] = CLAUDE_SOURCE
            sessions.append(parsed)

    for project_path in project_dirs:
        for session_dir in _find_subagent_only_sessions(project_path):
            parsed = _parse_subagent_session(session_dir, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                session_id = parsed.get("session_id", "")
                if session_id and session_id in seen_session_ids:
                    continue
                if session_id:
                    seen_session_ids.add(session_id)
                parsed["project"] = _build_project_name(project_dir_name)
                parsed["source"] = CLAUDE_SOURCE
                sessions.append(parsed)

    return sessions


def _parse_opencode_session(
    session_id: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    if not OPENCODE_DB_PATH.exists():
        return None

    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        with sqlite3.connect(OPENCODE_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
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
                metadata["cwd"] = anonymizer.path(raw_cwd)
            elif target_cwd != UNKNOWN_OPENCODE_CWD:
                return None

            metadata["start_time"] = _normalize_timestamp(session_row["time_created"])
            metadata["end_time"] = _normalize_timestamp(session_row["time_updated"])

            message_rows = conn.execute(
                "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
                (session_id,),
            ).fetchall()

            for message_row in message_rows:
                message_data = _load_json_field(message_row["data"])
                role = message_data.get("role")
                timestamp = _normalize_timestamp(message_row["time_created"])

                model = _extract_opencode_model(message_data)
                if metadata["model"] is None and model:
                    metadata["model"] = model

                part_rows = conn.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC, id ASC",
                    (message_row["id"],),
                ).fetchall()
                parts = [_load_json_field(part_row["data"]) for part_row in part_rows]

                if role == "user":
                    content = _extract_opencode_user_content(parts, anonymizer)
                    if content is not None:
                        messages.append({"role": "user", "content": content, "timestamp": timestamp})
                        stats["user_messages"] += 1
                        _update_time_bounds(metadata, timestamp)
                elif role == "assistant":
                    msg = _extract_opencode_assistant_content(parts, anonymizer, include_thinking)
                    if msg:
                        msg["timestamp"] = timestamp
                        messages.append(msg)
                        stats["assistant_messages"] += 1
                        stats["tool_uses"] += len(msg.get("tool_uses", []))
                        _update_time_bounds(metadata, timestamp)

                    tokens = message_data.get("tokens", {})
                    if isinstance(tokens, dict):
                        cache = tokens.get("cache", {})
                        cache_read = _safe_int(cache.get("read")) if isinstance(cache, dict) else 0
                        cache_write = _safe_int(cache.get("write")) if isinstance(cache, dict) else 0
                        stats["input_tokens"] += _safe_int(tokens.get("input")) + cache_read + cache_write
                        stats["output_tokens"] += _safe_int(tokens.get("output"))
    except (sqlite3.Error, OSError):
        return None

    if metadata["model"] is None:
        metadata["model"] = "opencode-unknown"

    return _make_session_result(metadata, messages, stats)


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


def _build_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
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
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
            else:
                text = str(content).strip() if content else ""
            result[tid] = {
                "output": {"text": anonymizer.text(text)} if text else {},
                "status": "error" if is_error else "success",
            }
    return result


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
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    tool_result_map = _build_tool_result_map(entries, anonymizer)
    for entry in entries:
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking, tool_result_map)

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

    entries = [entry for _ts, entry in timed_entries]
    tool_result_map = _build_tool_result_map(entries, anonymizer)
    for entry in entries:
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking, tool_result_map)

    return _make_session_result(metadata, messages, stats)


def _parse_gemini_tool_call(tc: dict, anonymizer: Anonymizer) -> dict:
    """Parse a Gemini tool call into a structured dict with input/output/status."""
    name = tc.get("name")
    args = tc.get("args", {})
    status = tc.get("status", "unknown")
    result_list = tc.get("result") or []

    # --- Extract output text from functionResponse ---
    output_text: str | None = None
    extra_texts: list[str] = []
    for item in result_list:
        if not isinstance(item, dict):
            continue
        if "functionResponse" in item:
            resp = item["functionResponse"].get("response", {})
            output_text = resp.get("output")
        elif "text" in item:
            extra_texts.append(item["text"])

    # --- Build structured input ---
    if name == "read_file":
        inp = {"file_path": anonymizer.path(args.get("file_path", ""))}
    elif name == "write_file":
        inp = {
            "file_path": anonymizer.path(args.get("file_path", "")),
            "content": anonymizer.text(args.get("content", "")),
        }
    elif name == "replace":
        inp = {
            "file_path": anonymizer.path(args.get("file_path", "")),
            "old_string": anonymizer.text(args.get("old_string", "")),
            "new_string": anonymizer.text(args.get("new_string", "")),
            "expected_replacements": args.get("expected_replacements"),
            "instruction": anonymizer.text(args.get("instruction", "")) if args.get("instruction") else None,
        }
        inp = {k: v for k, v in inp.items() if v is not None}
    elif name == "run_shell_command":
        inp = {"command": anonymizer.text(args.get("command", ""))}
    elif name == "read_many_files":
        inp = {"paths": [anonymizer.path(p) for p in args.get("paths", [])]}
    elif name in ("search_file_content", "grep_search"):
        inp = {k: anonymizer.text(str(v)) for k, v in args.items()}
    elif name == "list_directory":
        inp = {"dir_path": anonymizer.path(args.get("dir_path", ""))}
        if args.get("ignore"):
            inp["ignore"] = [anonymizer.text(str(p)) for p in args["ignore"]] if isinstance(args["ignore"], list) else anonymizer.text(str(args["ignore"]))
    elif name == "glob":
        inp = {"pattern": args.get("pattern", "")}
    elif name in ("google_web_search", "web_fetch", "codebase_investigator"):
        inp = {k: anonymizer.text(str(v)) for k, v in args.items()}
    else:
        inp = {k: anonymizer.text(str(v)) if isinstance(v, str) else v for k, v in args.items()}

    # --- Build structured output ---
    if name == "read_many_files":
        # Parse "--- /path/to/file ---\n<content>" blocks from extra text parts
        files: list[dict] = []
        for raw in extra_texts:
            lines = raw.split("\n")
            current_path: str | None = None
            content_lines: list[str] = []
            for line in lines:
                if line.startswith("--- ") and line.endswith(" ---"):
                    if current_path is not None:
                        files.append({
                            "path": anonymizer.path(current_path),
                            "content": anonymizer.text("\n".join(content_lines).strip()),
                        })
                    current_path = line[4:-4].strip()
                    content_lines = []
                else:
                    content_lines.append(line)
            if current_path is not None:
                files.append({
                    "path": anonymizer.path(current_path),
                    "content": anonymizer.text("\n".join(content_lines).strip()),
                })
        out: dict = {"files": files}
    elif name == "run_shell_command" and output_text:
        # Parse "Command: ...\nDirectory: ...\nOutput: ...\nExit Code: ..." format
        parsed: dict = {}
        current_key: str | None = None
        current_val: list[str] = []
        for line in output_text.splitlines():
            for key, prefix in (("command", "Command: "), ("directory", "Directory: "),
                                 ("output", "Output: "), ("exit_code", "Exit Code: ")):
                if line.startswith(prefix):
                    if current_key:
                        parsed[current_key] = "\n".join(current_val).strip()
                    current_key = key
                    current_val = [line[len(prefix):]]
                    break
            else:
                if current_key:
                    current_val.append(line)
        if current_key:
            parsed[current_key] = "\n".join(current_val).strip()
        if "exit_code" in parsed:
            try:
                parsed["exit_code"] = int(parsed["exit_code"])
            except ValueError:
                pass
        if "command" in parsed:
            parsed["command"] = anonymizer.text(parsed["command"])
        if "directory" in parsed:
            parsed["directory"] = anonymizer.path(parsed["directory"])
        if "output" in parsed:
            parsed["output"] = anonymizer.text(parsed["output"])
        out = parsed
    elif output_text is not None:
        out = {"text": anonymizer.text(output_text)}
    else:
        out = {}

    result: dict = {"tool": name, "input": inp, "output": out, "status": status}
    return result


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
                tool_uses.append(_parse_gemini_tool_call(tc, anonymizer))

            if tool_uses:
                msg["tool_uses"] = tool_uses
                stats["tool_uses"] += len(tool_uses)

            if "content" in msg or "thinking" in msg or "tool_uses" in msg:
                messages.append(msg)
                stats["assistant_messages"] += 1
                _update_time_bounds(metadata, timestamp)

    return _make_session_result(metadata, messages, stats)


def _parse_openclaw_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    """Parse an OpenClaw session JSONL file into a structured conversation."""
    try:
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    if not entries:
        return None

    # First entry is the session header
    header = entries[0]
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
    stats = _make_stats()

    # Pre-pass: build tool result map from toolResult messages
    tool_result_map: dict[str, dict] = {}
    for entry in entries[1:]:
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
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            output_text = "\n".join(text_parts).strip()
        elif isinstance(content, str):
            output_text = content.strip()
        else:
            output_text = ""
        tool_result_map[tool_call_id] = {
            "output": {"text": anonymizer.text(output_text)} if output_text else {},
            "status": "error" if is_error else "success",
        }

    # Main pass: process message entries
    for entry in entries[1:]:
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
        # Message-level timestamp is epoch ms; entry-level is ISO string
        if isinstance(msg_ts, (int, float)):
            msg_ts = _normalize_timestamp(msg_ts)
        effective_ts = msg_ts or timestamp

        if role == "user":
            content = msg_data.get("content")
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
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
                "timestamp": effective_ts,
            })
            stats["user_messages"] += 1
            _update_time_bounds(metadata, effective_ts)

        elif role == "assistant":
            model = msg_data.get("model")
            if model and metadata["model"] is None:
                provider = msg_data.get("provider", "")
                metadata["model"] = f"{provider}/{model}" if provider else model

            usage = msg_data.get("usage", {})
            if isinstance(usage, dict):
                stats["input_tokens"] += _safe_int(usage.get("input")) + _safe_int(usage.get("cacheRead"))
                stats["output_tokens"] += _safe_int(usage.get("output"))

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
                        "input": _parse_tool_input(tool_name, args, anonymizer),
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
            _update_time_bounds(metadata, effective_ts)

        elif role == "bashExecution":
            # Standalone bash execution (interactive shell, not via toolCall/toolResult)
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
            messages.append({
                "role": "assistant",
                "tool_uses": [tool_entry],
                "timestamp": effective_ts,
            })
            stats["assistant_messages"] += 1
            stats["tool_uses"] += 1
            _update_time_bounds(metadata, effective_ts)

    if metadata["model"] is None:
        metadata["model"] = "openclaw-unknown"

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
    tool_result_map: dict[str, dict] = dataclasses.field(default_factory=dict)


def _build_codex_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build call_id -> {output, status} from function_call_output and custom_tool_call_output."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "response_item":
            continue
        p = entry.get("payload", {})
        pt = p.get("type")
        call_id = p.get("call_id")
        if not call_id:
            continue

        if pt == "function_call_output":
            raw = p.get("output", "")
            # Parse "Exit code: N\nWall time: ...\nOutput:\n..." format
            out: dict = {}
            lines = raw.splitlines()
            output_lines: list[str] = []
            in_output = False
            for line in lines:
                if line.startswith("Exit code: "):
                    try:
                        out["exit_code"] = int(line[len("Exit code: "):].strip())
                    except ValueError:
                        out["exit_code"] = line[len("Exit code: "):].strip()
                elif line.startswith("Wall time: "):
                    out["wall_time"] = line[len("Wall time: "):].strip()
                elif line == "Output:":
                    in_output = True
                elif in_output:
                    output_lines.append(line)
            if output_lines:
                out["output"] = anonymizer.text("\n".join(output_lines).strip())
            result[call_id] = {"output": out, "status": "success"}

        elif pt == "custom_tool_call_output":
            raw = p.get("output", "")
            out = {}
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
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    state.tool_result_map = _build_codex_tool_result_map(entries, anonymizer)

    for entry in entries:
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
                "input": _parse_tool_input(tool_name, args_data, anonymizer),
                "_call_id": payload.get("call_id"),
            }
        )
    elif item_type == "custom_tool_call":
        tool_name = payload.get("name")
        raw_input = payload.get("input", "")
        inp = {"patch": anonymizer.text(raw_input)} if isinstance(raw_input, str) else _parse_tool_input(tool_name, raw_input, anonymizer)
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


def _resolve_codex_tool_uses(state: _CodexParseState) -> list[dict]:
    """Attach outputs from tool_result_map and strip internal _call_id field."""
    resolved = []
    for tu in state.pending_tool_uses:
        call_id = tu.pop("_call_id", None)
        if call_id and call_id in state.tool_result_map:
            r = state.tool_result_map[call_id]
            tu["output"] = r["output"]
            tu["status"] = r["status"]
        resolved.append(tu)
    return resolved


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
        msg["tool_uses"] = _resolve_codex_tool_uses(state)

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
        msg["tool_uses"] = _resolve_codex_tool_uses(state)

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


def _load_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _extract_opencode_model(message_data: dict[str, Any]) -> str | None:
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


def _extract_opencode_user_content(parts: list[dict[str, Any]], anonymizer: Anonymizer) -> str | None:
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(anonymizer.text(text.strip()))

    if not text_parts:
        return None
    return "\n\n".join(text_parts)


def _extract_opencode_assistant_content(
    parts: list[dict[str, Any]], anonymizer: Anonymizer, include_thinking: bool,
) -> dict[str, Any] | None:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict[str, str | None]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")

        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(anonymizer.text(text.strip()))
        elif part_type == "reasoning" and include_thinking:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                thinking_parts.append(anonymizer.text(text.strip()))
        elif part_type == "tool":
            tool_name = part.get("tool")
            state = part.get("state", {})
            tool_input = state.get("input", {}) if isinstance(state, dict) else {}
            tu: dict[str, Any] = {
                "tool": tool_name,
                "input": _parse_tool_input(tool_name, tool_input, anonymizer),
            }
            if isinstance(state, dict):
                status = state.get("status")
                if isinstance(status, str):
                    tu["status"] = "success" if status == "completed" else status
                output = state.get("output")
                if isinstance(output, str) and output:
                    tu["output"] = {"text": anonymizer.text(output)}
                elif output is not None:
                    tu["output"] = {}
            tool_uses.append(tu)

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


def _build_opencode_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_OPENCODE_CWD:
        return "opencode:unknown"
    return f"opencode:{Path(cwd).name or cwd}"


def _build_openclaw_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_OPENCLAW_CWD:
        return "openclaw:unknown"
    return f"openclaw:{Path(cwd).name or cwd}"


def _get_opencode_project_index(refresh: bool = False) -> dict[str, list[str]]:
    global _OPENCODE_PROJECT_INDEX
    if refresh or not _OPENCODE_PROJECT_INDEX:
        _OPENCODE_PROJECT_INDEX = _build_opencode_project_index()
    return _OPENCODE_PROJECT_INDEX


def _parse_kimi_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Parse a Kimi CLI context.jsonl file into structured session data."""
    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": filepath.parent.name,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        for entry in _iter_jsonl(filepath):
            role = entry.get("role")

            if role == "user":
                content = entry.get("content")
                if isinstance(content, str) and content.strip():
                    messages.append({
                        "role": "user",
                        "content": anonymizer.text(content.strip()),
                        "timestamp": None,
                    })
                    stats["user_messages"] += 1

            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant"}

                content = entry.get("content")
                text_parts = []
                thinking_parts = []

                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text":
                            text = block.get("text", "").strip()
                            if text:
                                text_parts.append(anonymizer.text(text))
                        elif block_type == "think" and include_thinking:
                            think = block.get("think", "").strip()
                            if think:
                                thinking_parts.append(anonymizer.text(think))

                if text_parts:
                    msg["content"] = "\n\n".join(text_parts)
                if thinking_parts:
                    msg["thinking"] = "\n\n".join(thinking_parts)

                tool_calls = entry.get("tool_calls", [])
                tool_uses = []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        func = tc.get("function", {})
                        if isinstance(func, dict):
                            tool_name = func.get("name")
                            args_str = func.get("arguments", "")
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except json.JSONDecodeError:
                                args = args_str
                            tool_uses.append({
                                "tool": tool_name,
                                "input": _parse_tool_input(tool_name, args, anonymizer),
                            })

                if tool_uses:
                    msg["tool_uses"] = tool_uses
                    stats["tool_uses"] += len(tool_uses)

                if text_parts or thinking_parts or tool_uses:
                    messages.append(msg)
                    stats["assistant_messages"] += 1

            elif role == "_usage":
                token_count = entry.get("token_count")
                if isinstance(token_count, int):
                    stats["output_tokens"] = max(stats["output_tokens"], token_count)

    except OSError:
        return None

    return _make_session_result(metadata, messages, stats)


def _build_opencode_project_index() -> dict[str, list[str]]:
    if not OPENCODE_DB_PATH.exists():
        return {}

    index: dict[str, list[str]] = {}
    try:
        with sqlite3.connect(OPENCODE_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, directory FROM session ORDER BY time_updated DESC, id DESC"
            ).fetchall()
    except sqlite3.Error:
        return {}

    for session_id, cwd in rows:
        normalized_cwd = cwd if isinstance(cwd, str) and cwd.strip() else UNKNOWN_OPENCODE_CWD
        if not isinstance(session_id, str) or not session_id:
            continue
        index.setdefault(normalized_cwd, []).append(session_id)
    return index


def _get_openclaw_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _OPENCLAW_PROJECT_INDEX
    if refresh or not _OPENCLAW_PROJECT_INDEX:
        _OPENCLAW_PROJECT_INDEX = _build_openclaw_project_index()
    return _OPENCLAW_PROJECT_INDEX


def _build_openclaw_project_index() -> dict[str, list[Path]]:
    """Scan ~/.openclaw/agents/*/sessions/*.jsonl, read each session header to get cwd."""
    if not OPENCLAW_AGENTS_DIR.exists():
        return {}

    index: dict[str, list[Path]] = {}
    try:
        for agent_dir in sorted(OPENCLAW_AGENTS_DIR.iterdir()):
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for session_file in sorted(sessions_dir.glob("*.jsonl")):
                cwd = _extract_openclaw_cwd(session_file) or UNKNOWN_OPENCLAW_CWD
                index.setdefault(cwd, []).append(session_file)
    except OSError:
        pass
    return index


def _extract_openclaw_cwd(session_file: Path) -> str | None:
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
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _process_entry(
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

    timestamp = _normalize_timestamp(entry.get("timestamp"))

    if entry_type == "user":
        content = _extract_user_content(entry, anonymizer)
        if content is not None:
            messages.append({"role": "user", "content": content, "timestamp": timestamp})
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

    elif entry_type == "assistant":
        msg = _extract_assistant_content(entry, anonymizer, include_thinking, tool_result_map)
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
                "input": _parse_tool_input(block.get("name"), block.get("input", {}), anonymizer),
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


def _parse_tool_input(tool_name: str | None, input_data: Any, anonymizer: Anonymizer) -> dict:
    """Return a structured dict for a tool's input args, with paths/content anonymized."""
    if not isinstance(input_data, dict):
        return {"raw": anonymizer.text(str(input_data))}

    name = (tool_name or "").lower()

    # Claude Code tools
    if name in ("read", "edit"):
        return {"file_path": anonymizer.path(input_data.get("file_path", ""))}
    if name == "write":
        return {
            "file_path": anonymizer.path(input_data.get("file_path", "")),
            "content": anonymizer.text(input_data.get("content", "")),
        }
    if name == "bash":
        cmd, _ = redact_text(input_data.get("command", ""))
        return {"command": anonymizer.text(cmd)}
    if name == "grep":
        pattern, _ = redact_text(input_data.get("pattern", ""))
        return {"pattern": anonymizer.text(pattern), "path": anonymizer.path(input_data.get("path", ""))}
    if name == "glob":
        return {"pattern": input_data.get("pattern", ""), "path": anonymizer.path(input_data.get("path", ""))}
    if name == "task":
        return {"prompt": anonymizer.text(input_data.get("prompt", ""))}
    if name == "websearch":
        return {"query": anonymizer.text(input_data.get("query", ""))}
    if name == "webfetch":
        return {"url": anonymizer.text(input_data.get("url", ""))}
    if name == "apply_patch":
        return {"patch": anonymizer.text(input_data.get("patchText", ""))}
    if name == "codesearch":
        return {"query": anonymizer.text(input_data.get("query", ""))}

    # Codex tools
    if name == "exec_command":
        cmd, _ = redact_text(input_data.get("cmd", ""))
        return {"cmd": anonymizer.text(cmd)}
    if name == "shell_command":
        cmd, _ = redact_text(input_data.get("command", ""))
        return {
            "command": anonymizer.text(cmd),
            "workdir": anonymizer.path(input_data.get("workdir", "")),
        }
    if name == "write_stdin":
        return {
            "session_id": input_data.get("session_id"),
            "chars": anonymizer.text(input_data.get("chars", "")),
            "yield_time_ms": input_data.get("yield_time_ms"),
            "max_output_tokens": input_data.get("max_output_tokens"),
        }
    if name == "update_plan":
        plan = input_data.get("plan", [])
        return {
            "explanation": anonymizer.text(input_data.get("explanation", "")),
            "plan": [anonymizer.text(str(p)) if isinstance(p, str) else p for p in plan],
        }

    # Fallback: anonymize all string values
    return {k: anonymizer.text(str(v)) if isinstance(v, str) else v for k, v in input_data.items()}

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
