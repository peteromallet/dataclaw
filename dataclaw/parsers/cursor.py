import json
import platform
import sqlite3
from pathlib import Path
from typing import Any

from ..anonymizer import Anonymizer
from ..secrets import redact_text

CURSOR_SOURCE = "cursor"
_SYS = platform.system()
if _SYS == "Darwin":
    CURSOR_DB = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
elif _SYS == "Windows":
    CURSOR_DB = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
else:
    CURSOR_DB = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
UNKNOWN_CURSOR_CWD = "<unknown-cwd>"

_PROJECT_INDEX: dict[str, list[str]] = {}


def _try_parse_json(s: Any) -> Any:
    if not isinstance(s, str):
        return s
    try:
        return _try_parse_json(json.loads(s))
    except (json.JSONDecodeError, TypeError):
        return s


def _strip_mcp_prefix(name: str) -> str:
    if not name or not name.startswith("mcp"):
        return name
    if name.startswith("mcp_"):
        parts = name.split("_", 2)
        return parts[2] if len(parts) >= 3 else name
    if name.startswith("mcp-"):
        underscore_pos = name.find("_", 4)
        if underscore_pos > 0:
            dash_pos = name.rfind("-", 0, underscore_pos)
            if dash_pos > 3:
                return name[dash_pos + 1:]
        rest = name[4:]
        for length in range(1, len(rest) // 2 + 1):
            server = rest[:length]
            after = rest[length:]
            for sep in ("-", "-user-"):
                if after.startswith(sep + server + "-"):
                    return after[len(sep) + len(server) + 1:]
    return name


def get_project_index(refresh: bool = False) -> dict[str, list[str]]:
    global _PROJECT_INDEX
    if refresh or not _PROJECT_INDEX:
        _PROJECT_INDEX = _build_project_index()
    return _PROJECT_INDEX


def _build_project_index() -> dict[str, list[str]]:
    if not CURSOR_DB.exists():
        return {}
    index: dict[str, list[str]] = {}
    try:
        with sqlite3.connect(f"file:{CURSOR_DB}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()

            cid_to_first_bid: dict[str, str] = {}
            for key, value in rows:
                cid = key.replace("composerData:", "")
                try:
                    data = json.loads(value) if isinstance(value, (str, bytes)) else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                headers = data.get("fullConversationHeadersOnly") or data.get("conversation", [])
                if len(headers) < 2:
                    continue
                bid = headers[0].get("bubbleId", "")
                if bid:
                    cid_to_first_bid[cid] = bid

            if not cid_to_first_bid:
                return index

            bubble_keys = [f"bubbleId:{cid}:{bid}" for cid, bid in cid_to_first_bid.items()]
            conn.execute("CREATE TEMP TABLE _dc_keys(k TEXT)")
            conn.executemany("INSERT INTO _dc_keys VALUES(?)", [(k,) for k in bubble_keys])
            bubble_rows = conn.execute(
                "SELECT nk.k, kv.value FROM _dc_keys nk JOIN cursorDiskKV kv ON nk.k = kv.key"
            ).fetchall()
            conn.execute("DROP TABLE _dc_keys")

            found_cids: set[str] = set()
            for key, val in bubble_rows:
                parts = key.split(":")
                cid = parts[1] if len(parts) >= 3 else ""
                found_cids.add(cid)
                try:
                    bubble = json.loads(val) if isinstance(val, (str, bytes)) else {}
                except (json.JSONDecodeError, TypeError):
                    index.setdefault(UNKNOWN_CURSOR_CWD, []).append(cid)
                    continue
                wuris = bubble.get("workspaceUris", [])
                if wuris and isinstance(wuris, list) and wuris[0]:
                    uri = wuris[0]
                    if uri.startswith("file://"):
                        uri = uri[7:]
                    index.setdefault(uri, []).append(cid)
                else:
                    index.setdefault(UNKNOWN_CURSOR_CWD, []).append(cid)

            for cid in cid_to_first_bid:
                if cid not in found_cids:
                    index.setdefault(UNKNOWN_CURSOR_CWD, []).append(cid)

    except sqlite3.Error:
        return {}
    return index


def build_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_CURSOR_CWD:
        return "cursor:unknown"
    return f"cursor:{Path(cwd).name or cwd}"


def discover_projects() -> list[dict]:
    index = get_project_index(refresh=True)
    if not index:
        return []
    db_size = CURSOR_DB.stat().st_size if CURSOR_DB.exists() else 0
    total_sessions = sum(len(cids) for cids in index.values())
    projects = []
    for cwd, cids in sorted(index.items()):
        if not cids:
            continue
        estimated_size = int(db_size * (len(cids) / total_sessions)) if total_sessions else 0
        projects.append({
            "dir_name": cwd,
            "display_name": build_project_name(cwd),
            "session_count": len(cids),
            "total_size_bytes": estimated_size,
            "source": CURSOR_SOURCE,
        })
    return projects


def parse_session(
    composer_id: str,
    conn: sqlite3.Connection,
    anonymizer: Anonymizer,
    include_thinking: bool,
    _make_stats,
    _make_session_result,
    _parse_tool_input,
    _update_time_bounds,
    _normalize_timestamp,
    _safe_int,
) -> dict | None:
    row = conn.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{composer_id}",),
    ).fetchone()
    if not row:
        return None

    try:
        composer = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else {}
    except (json.JSONDecodeError, TypeError):
        return None

    headers = composer.get("fullConversationHeadersOnly") or []
    if not headers:
        conv = composer.get("conversation", [])
        headers = [{"bubbleId": b["bubbleId"], "type": b.get("type")} for b in conv if "bubbleId" in b]

    if not headers:
        return None

    bubble_map: dict[str, dict] = {}
    cursor = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
        (f"bubbleId:{composer_id}:%",),
    )
    for key, val in cursor:
        bid = key.split(":")[-1]
        try:
            bubble_map[bid] = json.loads(val) if isinstance(val, (str, bytes)) else {}
        except (json.JSONDecodeError, TypeError):
            pass

    metadata: dict[str, Any] = {
        "session_id": composer_id,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    messages: list[dict[str, Any]] = []
    stats = _make_stats()

    for h in headers:
        bubble = bubble_map.get(h.get("bubbleId", ""))
        if not bubble:
            continue

        timestamp = bubble.get("createdAt")
        if isinstance(timestamp, (int, float)):
            timestamp = _normalize_timestamp(timestamp)

        if metadata["cwd"] is None:
            wuris = bubble.get("workspaceUris", [])
            if wuris and isinstance(wuris, list) and wuris[0]:
                uri = wuris[0]
                if uri.startswith("file://"):
                    uri = uri[7:]
                metadata["cwd"] = anonymizer.path(uri)

        model_info = bubble.get("modelInfo")
        if isinstance(model_info, dict) and metadata["model"] is None:
            model_name = model_info.get("modelName")
            if isinstance(model_name, str) and model_name.strip():
                metadata["model"] = model_name

        bubble_type = bubble.get("type")

        if bubble_type == 1:
            text = (bubble.get("text") or "").strip()
            if not text:
                continue
            redacted, _ = redact_text(text)
            messages.append({
                "role": "user",
                "content": anonymizer.text(redacted),
                "timestamp": timestamp,
            })
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

        elif bubble_type == 2:
            tfd = bubble.get("toolFormerData")
            tool_name_raw = tfd.get("name", "") if isinstance(tfd, dict) else ""

            if tool_name_raw:
                tool_name = _strip_mcp_prefix(tool_name_raw)
                params_raw = _try_parse_json(tfd.get("params"))
                if isinstance(params_raw, dict) and "tools" in params_raw:
                    tools = params_raw["tools"]
                    if isinstance(tools, list) and len(tools) == 1:
                        inner = _try_parse_json(tools[0].get("parameters", "{}"))
                        if isinstance(inner, dict):
                            params_raw = inner

                tool_input = _parse_tool_input(tool_name, params_raw if isinstance(params_raw, dict) else {}, anonymizer)

                result_raw = _try_parse_json(tfd.get("result"))
                tool_output: dict[str, Any] = {}
                if isinstance(result_raw, str) and result_raw.strip():
                    redacted_out, _ = redact_text(result_raw)
                    tool_output = {"text": anonymizer.text(redacted_out)}
                elif isinstance(result_raw, dict):
                    tool_output = {k: anonymizer.text(str(v)) if isinstance(v, str) else v for k, v in result_raw.items()}
                elif result_raw is not None:
                    tool_output = {"text": anonymizer.text(str(result_raw))}

                status_val = tfd.get("status", "unknown")
                if isinstance(status_val, dict):
                    status_val = status_val.get("status", "unknown")

                tool_entry: dict[str, Any] = {
                    "tool": tool_name,
                    "input": tool_input,
                }
                if tool_output:
                    tool_entry["output"] = tool_output
                if isinstance(status_val, str):
                    tool_entry["status"] = status_val

                msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_uses": [tool_entry],
                    "timestamp": timestamp,
                }

                thinking = bubble.get("thinking")
                if include_thinking and isinstance(thinking, dict):
                    think_text = (thinking.get("text") or "").strip()
                    if think_text:
                        msg["thinking"] = anonymizer.text(think_text)

                text = (bubble.get("text") or "").strip()
                if text:
                    redacted, _ = redact_text(text)
                    msg["content"] = anonymizer.text(redacted)

                messages.append(msg)
                stats["assistant_messages"] += 1
                stats["tool_uses"] += 1
                _update_time_bounds(metadata, timestamp)
            else:
                text = (bubble.get("text") or "").strip()
                thinking = bubble.get("thinking")
                think_text = ""
                if include_thinking and isinstance(thinking, dict):
                    think_text = (thinking.get("text") or "").strip()

                if not text and not think_text:
                    continue

                msg = {"role": "assistant", "timestamp": timestamp}
                if text:
                    redacted, _ = redact_text(text)
                    msg["content"] = anonymizer.text(redacted)
                if think_text:
                    msg["thinking"] = anonymizer.text(think_text)

                messages.append(msg)
                stats["assistant_messages"] += 1
                _update_time_bounds(metadata, timestamp)

        tc = bubble.get("tokenCount")
        if isinstance(tc, dict):
            stats["input_tokens"] += _safe_int(tc.get("inputTokens"))
            stats["output_tokens"] += _safe_int(tc.get("outputTokens"))

    if metadata["model"] is None:
        metadata["model"] = "cursor-unknown"

    return _make_session_result(metadata, messages, stats)
