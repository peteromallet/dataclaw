import hashlib
import logging
import os
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from .. import _json as json
from ..anonymizer import Anonymizer
from ..export_tasks import ExportSessionTask
from ..secrets import should_skip_large_binary_string
from .common import (
    anonymize_value,
    collect_project_sessions,
    count_existing_paths_and_sizes,
    make_session_result,
    make_stats,
    parse_tool_input,
    update_time_bounds,
)

logger = logging.getLogger(__name__)

SOURCE = "gemini"
GEMINI_DIR = Path.home() / ".gemini" / "tmp"

_HASH_MAP: dict[str, str] = {}


def build_hash_map() -> dict[str, str]:
    """Build a mapping from SHA-256 hash prefix to directory path."""
    result: dict[str, str] = {}

    root_dirs = [Path.home()]
    if hasattr(os, "listdrives"):
        for drive in os.listdrives():
            root_dirs.append(Path(drive))

    for root in root_dirs:
        try:
            for entry in root.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    digest = hashlib.sha256(str(entry).encode()).hexdigest()
                    result[digest] = str(entry)
        except OSError as e:
            logger.warning("Failed to scan directory %s: %s", root, e)

    return result


def extract_project_path_from_sessions(project_hash: str, gemini_dir: Path) -> str | None:
    """Try to extract the project working directory from session tool call file paths."""
    chats_dir = gemini_dir / project_hash / "chats"
    if not chats_dir.exists():
        return None

    for session_file in sorted(chats_dir.glob("session-*.json"), reverse=True):
        try:
            with open(session_file, "rb") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse JSON in %s: %s", session_file, e)
            continue
        except OSError as e:
            logger.warning("Failed to read %s: %s", session_file, e)
            continue

        has_tool_calls = False
        for msg in data.get("messages", []):
            tool_calls = msg.get("toolCalls", [])
            if tool_calls:
                has_tool_calls = True
            for tool_call in tool_calls:
                fp = tool_call.get("args", {}).get("file_path") or tool_call.get("args", {}).get(
                    "path",
                    "",
                )
                fp = Path(fp)
                if fp.is_absolute():
                    parts = fp.parts
                    for depth in range(1, len(parts)):
                        candidate = str(Path(*parts[: depth + 1]))
                        if hashlib.sha256(candidate.encode()).hexdigest() == project_hash:
                            return candidate
        if has_tool_calls:
            break

    return None


def resolve_hash(project_hash: str, gemini_dir: Path, hash_map: dict[str, str]) -> str:
    """Resolve a Gemini project hash to a readable directory name."""
    if len(project_hash) != 64:
        return project_hash

    if not hash_map:
        hash_map.update(build_hash_map())

    full_path = hash_map.get(project_hash)
    if full_path:
        return Path(full_path).name

    extracted = extract_project_path_from_sessions(project_hash, gemini_dir)
    if extracted:
        hash_map[project_hash] = extracted
        return Path(extracted).name

    return project_hash[:8]


def resolve_project_hash(project_hash: str) -> str:
    return resolve_hash(project_hash, GEMINI_DIR, _HASH_MAP)


def build_project_name(
    project_hash: str,
    resolve_hash_fn: Callable[[str], str] | None = None,
) -> str:
    if resolve_hash_fn is None:
        resolve_hash_fn = resolve_project_hash
    return f"{SOURCE}:{resolve_hash_fn(project_hash)}"


def discover_projects(
    gemini_dir: Path | None = None,
    resolve_hash_fn: Callable[[str], str] | None = None,
) -> list[dict]:
    if gemini_dir is None:
        gemini_dir = GEMINI_DIR
    if resolve_hash_fn is None:
        resolve_hash_fn = resolve_project_hash
    if not gemini_dir.exists():
        return []

    projects = []
    for project_dir in sorted(gemini_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "bin":
            continue
        chats_dir = project_dir / "chats"
        if not chats_dir.exists():
            continue
        session_count, total_size = count_existing_paths_and_sizes(chats_dir.glob("session-*.json"))
        if session_count == 0:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": build_project_name(project_dir.name, resolve_hash_fn),
                "session_count": session_count,
                "total_size_bytes": total_size,
                "source": SOURCE,
            }
        )
    return projects


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> Iterable[dict]:
    project_path = GEMINI_DIR / project_dir_name / "chats"
    if not project_path.exists():
        return ()

    return collect_project_sessions(
        sorted(project_path.glob("session-*.json")),
        lambda session_file: parse_session_file(session_file, anonymizer, include_thinking),
        build_project_name(project_dir_name),
        SOURCE,
    )


def build_export_session_tasks(project_index: int, project: dict) -> list[ExportSessionTask]:
    project_path = GEMINI_DIR / project["dir_name"] / "chats"
    if not project_path.exists():
        return []

    tasks: list[ExportSessionTask] = []
    for task_index, session_file in enumerate(sorted(project_path.glob("session-*.json"))):
        tasks.append(
            ExportSessionTask(
                source=SOURCE,
                project_index=project_index,
                task_index=task_index,
                project_dir_name=project["dir_name"],
                project_display_name=project["display_name"],
                estimated_bytes=session_file.stat().st_size if session_file.exists() else 0,
                kind="gemini",
                file_path=str(session_file),
            )
        )
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    if not task.file_path:
        return None
    return parse_session_file(Path(task.file_path), anonymizer, include_thinking)


def parse_tool_call(tool_call: dict, anonymizer: Anonymizer) -> dict:
    """Parse a Gemini tool call into a structured dict with input/output/status."""
    name = tool_call.get("name")
    args = tool_call.get("args", {})
    status = tool_call.get("status", "unknown")
    result_list = tool_call.get("result") or []

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
            "instruction": (anonymizer.text(args.get("instruction", "")) if args.get("instruction") else None),
        }
        inp = {k: v for k, v in inp.items() if v is not None}
    elif name == "run_shell_command":
        inp = {"command": anonymizer.text(args.get("command", ""))}
    elif name == "read_many_files":
        inp = {"paths": [anonymizer.path(path) for path in args.get("paths", [])]}
    elif name in ("search_file_content", "grep_search"):
        inp = {k: anonymizer.text(str(v)) for k, v in args.items()}
    elif name == "list_directory":
        inp = {"dir_path": anonymizer.path(args.get("dir_path", ""))}
        if args.get("ignore"):
            if isinstance(args["ignore"], list):
                inp["ignore"] = [anonymizer.text(str(path)) for path in args["ignore"]]
            else:
                inp["ignore"] = anonymizer.text(str(args["ignore"]))
    elif name == "glob":
        inp = {"pattern": args.get("pattern", "")}
    elif name in ("google_web_search", "web_fetch", "codebase_investigator"):
        inp = {k: anonymizer.text(str(v)) for k, v in args.items()}
    else:
        inp = {k: anonymizer.text(str(v)) if isinstance(v, str) else v for k, v in args.items()}

    if name == "read_many_files":
        files: list[dict] = []
        for raw in extra_texts:
            lines = raw.split("\n")
            current_path: str | None = None
            content_lines: list[str] = []
            for line in lines:
                if line.startswith("--- ") and line.endswith(" ---"):
                    if current_path is not None:
                        files.append(
                            {
                                "path": anonymizer.path(current_path),
                                "content": anonymizer.text("\n".join(content_lines).strip()),
                            }
                        )
                    current_path = line[4:-4].strip()
                    content_lines = []
                else:
                    content_lines.append(line)
            if current_path is not None:
                files.append(
                    {
                        "path": anonymizer.path(current_path),
                        "content": anonymizer.text("\n".join(content_lines).strip()),
                    }
                )
        out: dict[str, Any] = {"files": files}
    elif name == "run_shell_command" and output_text:
        parsed: dict[str, Any] = {}
        current_key: str | None = None
        current_val: list[str] = []
        for line in output_text.splitlines():
            for key, prefix in (
                ("command", "Command: "),
                ("directory", "Directory: "),
                ("output", "Output: "),
                ("exit_code", "Exit Code: "),
            ):
                if line.startswith(prefix):
                    if current_key:
                        parsed[current_key] = "\n".join(current_val).strip()
                    current_key = key
                    current_val = [line[len(prefix) :]]
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
                parsed["exit_code"] = anonymizer.text(parsed["exit_code"])
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

    return {"tool": name, "input": inp, "output": out, "status": status}


def anonymize_text_preserving_blobs(
    text: Any,
    anonymizer: Anonymizer,
    *,
    strip: bool = False,
    drop_empty: bool = True,
) -> str | None:
    if not isinstance(text, str):
        return None
    if should_skip_large_binary_string(text):
        return text
    normalized = text.strip() if strip else text
    if drop_empty and not normalized.strip():
        return None
    return anonymizer.text(normalized)


def build_gemini_call_id(name: str, args: Any, counters: dict[str, int]) -> str:
    counters[name] += 1
    return f"fc_{name}_{counters[name]}"


def anonymize_file_uri(file_uri: Any, anonymizer: Anonymizer) -> str | None:
    if not isinstance(file_uri, str):
        return None
    if file_uri.startswith("file://"):
        return f"file://{anonymizer.path(file_uri[7:])}"
    return anonymizer.text(file_uri)


def parse_gemini_user_part(
    part: Any,
    anonymizer: Anonymizer,
    pending_call_ids: dict[str, deque[str]],
    call_counters: dict[str, int],
) -> tuple[str | None, dict[str, Any] | None]:
    if isinstance(part, str):
        text = anonymize_text_preserving_blobs(part, anonymizer, drop_empty=False)
        if text is None:
            return None, None
        if should_skip_large_binary_string(part):
            return None, {"type": "text", "text": text}
        return text, None

    if not isinstance(part, dict):
        return None, None

    if "text" in part:
        text = anonymize_text_preserving_blobs(part.get("text"), anonymizer, drop_empty=False)
        if text is None:
            return None, None
        if should_skip_large_binary_string(part.get("text", "")):
            return None, {"type": "text", "text": text}
        return text, None

    inline = part.get("inlineData")
    if isinstance(inline, dict):
        mime_type = inline.get("mimeType", "")
        return None, {
            "type": "image" if isinstance(mime_type, str) and mime_type.startswith("image/") else "document",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": inline.get("data", ""),
            },
        }

    file_data = part.get("fileData")
    if isinstance(file_data, dict):
        source: dict[str, Any] = {"type": "url"}
        url = anonymize_file_uri(file_data.get("fileUri"), anonymizer)
        if url:
            source["url"] = url
        mime_type = file_data.get("mimeType")
        if mime_type:
            source["media_type"] = mime_type
        return None, {"type": "document", "source": source}

    function_call = part.get("functionCall")
    if isinstance(function_call, dict):
        name = function_call.get("name", "unknown")
        args = function_call.get("args", {})
        call_id = function_call.get("id") or build_gemini_call_id(name, args, call_counters)
        pending_call_ids[name].append(call_id)
        return None, {
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": parse_tool_input(name, args, anonymizer),
        }

    function_response = part.get("functionResponse")
    if isinstance(function_response, dict):
        name = function_response.get("name", "unknown")
        tool_use_id = function_response.get("id") or (
            pending_call_ids[name].popleft() if pending_call_ids.get(name) else f"fc_{name}"
        )
        response = function_response.get("response")
        content: Any = None
        if isinstance(response, dict) and "output" in response:
            content = anonymize_text_preserving_blobs(response.get("output"), anonymizer)
        elif response is not None:
            content = anonymize_value("response", response, anonymizer)
        part_result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id}
        if content not in (None, "", [], {}):
            part_result["content"] = content
        return None, part_result

    return None, None


def parse_gemini_user_content(content: Any, anonymizer: Anonymizer) -> tuple[str | None, list[dict[str, Any]]]:
    if isinstance(content, str):
        text = anonymize_text_preserving_blobs(content, anonymizer, drop_empty=False)
        if text is None:
            return None, []
        if should_skip_large_binary_string(content):
            return None, [{"type": "text", "text": text}]
        return text, []

    if not isinstance(content, list):
        return None, []

    text_parts: list[str] = []
    content_parts: list[dict[str, Any]] = []
    pending_call_ids: dict[str, deque[str]] = defaultdict(deque)
    call_counters: dict[str, int] = defaultdict(int)

    for part in content:
        text, content_part = parse_gemini_user_part(part, anonymizer, pending_call_ids, call_counters)
        if text is not None:
            text_parts.append(text)
        if content_part:
            content_parts.append(content_part)

    text_content = "\n".join(text_parts) if text_parts else None
    return text_content, content_parts


def parse_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    try:
        with open(filepath, "rb") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse JSON in %s: %s", filepath, e)
        return None
    except OSError as e:
        logger.warning("Failed to read %s: %s", filepath, e)
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
    stats = make_stats()

    for msg_data in data.get("messages", []):
        msg_type = msg_data.get("type")
        timestamp = msg_data.get("timestamp")

        if msg_type == "user":
            text, content_parts = parse_gemini_user_content(msg_data.get("content"), anonymizer)
            if text is None and not content_parts:
                continue
            message: dict[str, Any] = {"role": "user", "timestamp": timestamp}
            if text is not None:
                message["content"] = text
            if content_parts:
                message["content_parts"] = content_parts
            messages.append(message)
            stats["user_messages"] += 1
            update_time_bounds(metadata, timestamp)

        elif msg_type == "gemini":
            if metadata["model"] is None:
                metadata["model"] = msg_data.get("model")

            tokens = msg_data.get("tokens", {})
            if tokens:
                stats["input_tokens"] += tokens.get("input", 0) + tokens.get("cached", 0)
                stats["output_tokens"] += tokens.get("output", 0)

            msg: dict[str, Any] = {"role": "assistant"}
            if timestamp:
                msg["timestamp"] = timestamp

            content = msg_data.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = anonymizer.text(content.strip())

            if include_thinking:
                thoughts = msg_data.get("thoughts", [])
                if thoughts:
                    thought_texts = []
                    for thought in thoughts:
                        if "description" in thought and isinstance(thought["description"], str):
                            thought_texts.append(thought["description"].strip())
                    if thought_texts:
                        msg["thinking"] = anonymizer.text("\n\n".join(thought_texts))

            tool_uses = []
            for tool_call in msg_data.get("toolCalls", []):
                tool_uses.append(parse_tool_call(tool_call, anonymizer))

            if tool_uses:
                msg["tool_uses"] = tool_uses
                stats["tool_uses"] += len(tool_uses)

            if "content" in msg or "thinking" in msg or "tool_uses" in msg:
                messages.append(msg)
                stats["assistant_messages"] += 1
                update_time_bounds(metadata, timestamp)

    return make_session_result(metadata, messages, stats)
