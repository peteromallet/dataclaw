"""Shared helpers for parser test modules."""

import sqlite3

from dataclaw import _json as json


def disable_other_providers(monkeypatch, tmp_path, keep=()):
    keep = set(keep)

    if "claude" not in keep:
        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", tmp_path / "no-claude-projects")

    if "codex" not in keep:
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
    monkeypatch.setattr("dataclaw.parsers.codex._PROJECT_INDEX", {})

    if "gemini" not in keep:
        monkeypatch.setattr("dataclaw.parsers.gemini.GEMINI_DIR", tmp_path / "no-gemini")
    monkeypatch.setattr("dataclaw.parsers.gemini._HASH_MAP", {})

    if "opencode" not in keep:
        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
    monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})
    monkeypatch.setattr("dataclaw.parsers.opencode._SESSION_SIZE_MAP", {})

    if "openclaw" not in keep:
        monkeypatch.setattr("dataclaw.parsers.openclaw.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
    monkeypatch.setattr("dataclaw.parsers.openclaw._PROJECT_INDEX", {})

    if "kimi" not in keep:
        monkeypatch.setattr("dataclaw.parsers.kimi.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")

    if "custom" not in keep:
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", tmp_path / "no-custom")

    if "cursor" not in keep:
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "no-cursor.vscdb")
    monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})
    monkeypatch.setattr("dataclaw.parsers.cursor._SESSION_SIZE_MAP", {})


def write_opencode_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_created INTEGER, time_updated INTEGER)"
    )
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, time_created INTEGER, data TEXT)")
    conn.commit()
    return conn


def make_subagent_entry(role, content, timestamp, cwd=None, session_id=None):
    entry = {"timestamp": timestamp}
    if role == "user":
        entry["type"] = "user"
        entry["message"] = {"content": content}
        if cwd:
            entry["cwd"] = cwd
            entry["gitBranch"] = "main"
            entry["version"] = "2.1.2"
        if session_id:
            entry["sessionId"] = session_id
    elif role == "assistant":
        entry["type"] = "assistant"
        entry["message"] = {
            "model": "claude-opus-4-5-20251101",
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }
    return entry


def make_openclaw_session_header(session_id="oc-sess-1", cwd="/Users/alice/projects/myapp"):
    return {
        "type": "session",
        "id": session_id,
        "cwd": cwd,
        "timestamp": "2026-02-20T10:00:00.000Z",
    }


def make_openclaw_user_message(text, timestamp="2026-02-20T10:01:00.000Z"):
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def make_openclaw_assistant_message(
    text,
    timestamp="2026-02-20T10:02:00.000Z",
    model="claude-sonnet-4-20250514",
    thinking=None,
    tool_calls=None,
    usage=None,
):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for tool_call in tool_calls or []:
        content.append(tool_call)
    message = {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
        },
    }
    if usage:
        message["message"]["usage"] = usage
    return message


def make_openclaw_tool_result(tool_call_id, output_text, is_error=False):
    return {
        "type": "message",
        "timestamp": "2026-02-20T10:02:30.000Z",
        "message": {
            "role": "toolResult",
            "toolCallId": tool_call_id,
            "content": [{"type": "text", "text": output_text}],
            "isError": is_error,
        },
    }


def write_cursor_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def insert_cursor_conversation(conn, composer_id, bubbles):
    headers = [{"bubbleId": bubble["id"], "type": bubble["type"]} for bubble in bubbles]
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES(?, ?)",
        (f"composerData:{composer_id}", json.dumps({"fullConversationHeadersOnly": headers})),
    )
    for bubble in bubbles:
        data = dict(bubble)
        data.pop("id")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES(?, ?)",
            (f"bubbleId:{composer_id}:{bubble['id']}", json.dumps(data)),
        )
