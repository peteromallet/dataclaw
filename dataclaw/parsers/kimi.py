import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import _json as json
from ..anonymizer import Anonymizer
from ..export_tasks import ExportSessionTask
from .common import (
    build_prefixed_project_name,
    collect_project_sessions,
    iter_jsonl,
    make_session_result,
    make_stats,
    parse_tool_input,
)

logger = logging.getLogger(__name__)

SOURCE = "kimi"
KIMI_DIR = Path.home() / ".kimi"
KIMI_SESSIONS_DIR = KIMI_DIR / "sessions"
KIMI_CONFIG_PATH = KIMI_DIR / "kimi.json"
UNKNOWN_KIMI_CWD = "<unknown-cwd>"


def load_work_dirs(config_path: Path) -> dict[str, str]:
    """Load Kimi work directory mapping from config file."""
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            data = json.load(f)
        work_dirs = data.get("work_dirs", [])
        return {entry.get("path", ""): entry.get("path", "") for entry in work_dirs if entry.get("path")}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load Kimi config %s: %s", config_path, e)
        return {}


def get_project_hash(cwd: str) -> str:
    """Generate Kimi project hash from working directory path (MD5)."""
    return hashlib.md5(cwd.encode()).hexdigest()


def discover_projects(
    sessions_dir: Path | None = None,
    config_path: Path | None = None,
) -> list[dict]:
    if sessions_dir is None:
        sessions_dir = KIMI_SESSIONS_DIR
    if config_path is None:
        config_path = KIMI_CONFIG_PATH
    if not sessions_dir.exists():
        return []

    work_dirs = load_work_dirs(config_path)
    path_to_hash = {path: get_project_hash(path) for path in work_dirs}
    hash_to_path = {digest: path for path, digest in path_to_hash.items()}

    projects = []
    for project_dir in sorted(sessions_dir.iterdir()):
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
                "source": SOURCE,
            }
        )
    return projects


def build_project_name(cwd: str) -> str:
    return build_prefixed_project_name(SOURCE, cwd, UNKNOWN_KIMI_CWD)


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> Iterable[dict]:
    project_hash = get_project_hash(project_dir_name)
    project_path = KIMI_SESSIONS_DIR / project_hash
    if not project_path.exists():
        return ()

    context_files = []
    for session_dir in sorted(project_path.iterdir()):
        if not session_dir.is_dir():
            continue
        context_file = session_dir / "context.jsonl"
        if context_file.exists():
            context_files.append(context_file)

    return collect_project_sessions(
        context_files,
        lambda context_file: parse_session_file(
            context_file,
            anonymizer=anonymizer,
            include_thinking=include_thinking,
        ),
        build_project_name(project_dir_name),
        SOURCE,
        default_model="kimi-k2",
    )


def build_export_session_tasks(project_index: int, project: dict) -> list[ExportSessionTask]:
    project_hash = get_project_hash(project["dir_name"])
    project_path = KIMI_SESSIONS_DIR / project_hash
    if not project_path.exists():
        return []

    tasks: list[ExportSessionTask] = []
    task_index = 0
    for session_dir in sorted(project_path.iterdir()):
        if not session_dir.is_dir():
            continue
        context_file = session_dir / "context.jsonl"
        if not context_file.exists():
            continue
        tasks.append(
            ExportSessionTask(
                source=SOURCE,
                project_index=project_index,
                task_index=task_index,
                project_dir_name=project["dir_name"],
                project_display_name=project["display_name"],
                estimated_bytes=context_file.stat().st_size,
                kind="kimi",
                file_path=str(context_file),
                default_model="kimi-k2",
            )
        )
        task_index += 1
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    if not task.file_path:
        return None
    return parse_session_file(Path(task.file_path), anonymizer, include_thinking)


def parse_session_file(
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
    stats = make_stats()

    try:
        for entry in iter_jsonl(filepath):
            role = entry.get("role")

            if role == "user":
                content = entry.get("content")
                if isinstance(content, str) and content.strip():
                    messages.append(
                        {
                            "role": "user",
                            "content": anonymizer.text(content.strip()),
                            "timestamp": None,
                        }
                    )
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
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue
                        func = tool_call.get("function", {})
                        if isinstance(func, dict):
                            tool_name = func.get("name")
                            args_str = func.get("arguments", "")
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except json.JSONDecodeError as e:
                                logger.warning(
                                    "Failed to parse Kimi tool arguments for %s: %s",
                                    tool_name,
                                    e,
                                )
                                args = args_str
                            tool_uses.append(
                                {
                                    "tool": tool_name,
                                    "input": parse_tool_input(tool_name, args, anonymizer),
                                }
                            )

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

    except OSError as e:
        logger.warning("Failed to read Kimi session file %s: %s", filepath, e)
        return None

    return make_session_result(metadata, messages, stats)
