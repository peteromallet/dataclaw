"""Parse Claude Code session JSONL files into structured conversations."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scout.tools import AnonymizerTool

from .secrets import redact_text

logger = logging.getLogger(__name__)


class AnonymizerWrapper:
    """Wrapper around Scout AnonymizerTool that provides the same interface as the old AnonymizerWrapper."""

    def __init__(self, extra_usernames: list[str] | None = None):
        self._tool = AnonymizerTool()
        # Always include the current system username
        self._current_username = os.path.basename(os.path.expanduser("~"))
        self._extra_usernames = extra_usernames or []

    def _get_all_usernames(self) -> list[str]:
        """Combine current username with extra usernames (deduplicated)."""
        usernames = set(self._extra_usernames)
        if self._current_username:
            usernames.add(self._current_username)
        return list(usernames)

    def text(self, content: str) -> str:
        if not content:
            return content
        usernames = self._get_all_usernames()
        try:
            result = self._tool.run({
                "mode": "text",
                "data": content,
                "extra_usernames": usernames,
            })
            return result.get("result") or content
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Anonymizer text() failed: {e}, returning original")
            return content

    def path(self, file_path: str) -> str:
        if not file_path:
            return file_path
        usernames = self._get_all_usernames()
        try:
            result = self._tool.run({
                "mode": "path",
                "data": file_path,
                "extra_usernames": usernames,
            })
            return result.get("result") or file_path
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Anonymizer path() failed: {e}, returning original")
            return file_path


class PassthroughAnonymizer:
    """A no-op anonymizer for when raw data is needed (e.g., for search indexing)."""

    def text(self, content: str) -> str:
        return content

    def path(self, file_path: str) -> str:
        return file_path

# Claude Code directory - can be overridden via CLAUDE_DIR environment variable
# or by passing a custom path to functions that need it
_DEFAULT_CLAUDE_DIR = Path.home() / ".claude"

def get_claude_dir() -> Path:
    """Get the Claude Code directory, respecting CLAUDE_DIR environment variable."""
    env_path = os.environ.get("CLAUDE_DIR")
    if env_path:
        return Path(env_path)
    return _DEFAULT_CLAUDE_DIR

# For backward compatibility - but code should use get_claude_dir() instead
CLAUDE_DIR = _DEFAULT_CLAUDE_DIR
PROJECTS_DIR = CLAUDE_DIR / "projects"


def discover_projects(claude_dir: Path | None = None) -> list[dict]:
    """Discover all Claude Code projects with session counts.
    
    Args:
        claude_dir: Optional path to Claude Code directory. 
                   Defaults to CLAUDE_DIR env var or ~/.claude
    """
    base_dir = claude_dir or get_claude_dir()
    projects_dir = base_dir / "projects"
    
    if not projects_dir.exists():
        return []

    projects = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        sessions = list(project_dir.glob("*.jsonl"))
        if not sessions:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": _build_project_name(project_dir.name),
                "session_count": len(sessions),
                "total_size_bytes": sum(f.stat().st_size for f in sessions),
            }
        )
    return projects


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: AnonymizerWrapper,
    include_thinking: bool = True,
    claude_dir: Path | None = None,
    anonymize: bool = True,
) -> list[dict]:
    """Parse all sessions for a project into structured dicts.
    
    Args:
        project_dir_name: Name of the project directory
        anonymizer: AnonymizerWrapper instance (used if anonymize=True)
        include_thinking: Whether to include thinking blocks
        claude_dir: Optional path to Claude Code directory
        anonymize: Whether to anonymize the session content (default: True).
                   Set to False when raw data is needed (e.g., for search indexing).
    """
    base_dir = claude_dir or get_claude_dir()
    project_path = base_dir / "projects" / project_dir_name
    if not project_path.exists():
        return []

    # Use passthrough anonymizer if anonymize=False
    effective_anonymizer = anonymizer if anonymize else PassthroughAnonymizer()

    sessions = []
    for session_file in sorted(project_path.glob("*.jsonl")):
        parsed = _parse_session_file(session_file, effective_anonymizer, include_thinking)
        if parsed and parsed["messages"]:
            parsed["project"] = _build_project_name(project_dir_name)
            sessions.append(parsed)
    return sessions


def _parse_session_file(
    filepath: Path, anonymizer: AnonymizerWrapper, include_thinking: bool = True
) -> dict | None:
    messages = []
    metadata = {
        "session_id": filepath.stem,
        "cwd": None,
        "git_branch": None,
        "claude_version": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    skipped_lines = 0
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    skipped_lines += 1
                    continue
                _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking)
    except OSError:
        return None

    if skipped_lines:
        logger.debug("Skipped %d malformed lines in %s", skipped_lines, filepath.name)
    stats["skipped_lines"] = skipped_lines

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


def _process_entry(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: AnonymizerWrapper,
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
            if metadata["start_time"] is None:
                metadata["start_time"] = timestamp
            metadata["end_time"] = timestamp

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
            metadata["end_time"] = timestamp


def _extract_user_content(entry: dict[str, Any], anonymizer: AnonymizerWrapper) -> str | None:
    msg_data = entry.get("message", {})
    content = msg_data.get("content", "")
    if isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        content = "\n".join(text_parts)
    if not content or not content.strip():
        return None
    return anonymizer.text(content)


def _extract_assistant_content(
    entry: dict[str, Any], anonymizer: AnonymizerWrapper, include_thinking: bool,
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


def _redact_and_truncate(text: str, anonymizer: AnonymizerWrapper) -> str:
    """Redact secrets BEFORE truncating to avoid partial secret leaks."""
    text, _ = redact_text(text)
    return anonymizer.text(text[:MAX_TOOL_INPUT_LENGTH])


def _summarize_tool_input(tool_name: str | None, input_data: Any, anonymizer: AnonymizerWrapper) -> str:
    """Summarize tool input for export."""
    if not isinstance(input_data, dict):
        return _redact_and_truncate(str(input_data), anonymizer)

    name = tool_name.lower() if tool_name else ""

    if name in ("read", "edit"):
        return anonymizer.path(input_data.get("file_path", ""))
    if name == "write":
        path = anonymizer.path(input_data.get("file_path", ""))
        return f"{path} ({len(input_data.get('content', ''))} chars)"
    if name == "bash":
        return _redact_and_truncate(input_data.get("command", ""), anonymizer)
    if name == "grep":
        pattern, _ = redact_text(input_data.get("pattern", ""))
        return f"pattern={anonymizer.text(pattern)} path={anonymizer.path(input_data.get('path', ''))}"
    if name == "glob":
        return f"pattern={anonymizer.text(input_data.get('pattern', ''))} path={anonymizer.path(input_data.get('path', ''))}"
    if name == "task":
        return _redact_and_truncate(input_data.get("prompt", ""), anonymizer)
    if name == "websearch":
        return input_data.get("query", "")
    if name == "webfetch":
        return input_data.get("url", "")
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
