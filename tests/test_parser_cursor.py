"""Tests for Cursor parser behavior."""

import sqlite3

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.secrets import REDACTED
from tests.parser_helpers import disable_other_providers, insert_cursor_conversation, write_cursor_db


class TestCursorDiscoverProjects:
    def test_discover_cursor_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-1",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "Hello",
                    "createdAt": 1706000000000,
                    "workspaceUris": ["file:///Users/testuser/work/repo"],
                },
                {"id": "b2", "type": 2, "text": "Hi there!", "createdAt": 1706000001000},
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "cursor"
        assert projects[0]["display_name"] == "cursor:repo"
        assert projects[0]["session_count"] == 1

    def test_discover_groups_by_workspace(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        for composer_id, uri in [
            ("c1", "file:///Users/alice/proj-a"),
            ("c2", "file:///Users/alice/proj-a"),
            ("c3", "file:///Users/alice/proj-b"),
        ]:
            insert_cursor_conversation(
                conn,
                composer_id,
                [
                    {"id": "b1", "type": 1, "text": "msg", "createdAt": 1706000000000, "workspaceUris": [uri]},
                    {"id": "b2", "type": 2, "text": "reply", "createdAt": 1706000001000},
                ],
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        projects = discover_projects()
        assert len(projects) == 2
        names = {project["display_name"] for project in projects}
        assert names == {"cursor:proj-a", "cursor:proj-b"}
        counts = {project["display_name"]: project["session_count"] for project in projects}
        assert counts["cursor:proj-a"] == 2
        assert counts["cursor:proj-b"] == 1

    def test_discover_no_db(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "nonexistent.vscdb")
        assert discover_projects() == []

    def test_discover_skips_single_bubble_conversations(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES(?, ?)",
            ("composerData:lonely", json.dumps({"fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]})),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        assert discover_projects() == []

    def test_discover_projects_does_not_use_fetchall(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-1",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "Hello",
                    "createdAt": 1706000000000,
                    "workspaceUris": ["file:///Users/testuser/work/repo"],
                },
                {"id": "b2", "type": 2, "text": "Hi there!", "createdAt": 1706000001000},
            ],
        )
        conn.commit()
        conn.close()

        class CursorWrapper:
            def __init__(self, cursor):
                self._cursor = cursor

            def __iter__(self):
                return iter(self._cursor)

            def fetchall(self):
                raise AssertionError("fetchall should not be called during Cursor discovery")

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class ConnectionWrapper:
            def __init__(self, conn):
                self._conn = conn

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._conn.__exit__(exc_type, exc, tb)

            def execute(self, *args, **kwargs):
                return CursorWrapper(self._conn.execute(*args, **kwargs))

            def executemany(self, *args, **kwargs):
                return CursorWrapper(self._conn.executemany(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self._conn, name)

        real_connect = sqlite3.connect

        def wrapped_connect(*args, **kwargs):
            return ConnectionWrapper(real_connect(*args, **kwargs))

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.cursor.sqlite3.connect", wrapped_connect)

        projects = discover_projects()

        assert len(projects) == 1
        assert projects[0]["display_name"] == "cursor:repo"


class TestCursorParseSessions:
    def test_basic_conversation(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-1",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "Fix the bug",
                    "createdAt": 1706000000000,
                    "workspaceUris": [f"file://{cwd}"],
                },
                {
                    "id": "b2",
                    "type": 2,
                    "text": "I'll fix it now.",
                    "createdAt": 1706000001000,
                    "modelInfo": {"modelName": "claude-sonnet-4-20250514"},
                    "tokenCount": {"inputTokens": 100, "outputTokens": 30},
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        session = sessions[0]
        assert session["session_id"] == "conv-1"
        assert session["source"] == "cursor"
        assert session["project"] == "cursor:myapp"
        assert session["model"] == "claude-sonnet-4-20250514"
        assert len(session["messages"]) == 2
        assert session["messages"][0]["role"] == "user"
        assert "Fix the bug" in session["messages"][0]["content"]
        assert session["messages"][1]["role"] == "assistant"
        assert "fix it" in session["messages"][1]["content"]
        assert session["stats"]["user_messages"] == 1
        assert session["stats"]["assistant_messages"] == 1
        assert session["stats"]["input_tokens"] == 100
        assert session["stats"]["output_tokens"] == 30

    def test_tool_call(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-2",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "Read the file",
                    "createdAt": 1706000000000,
                    "workspaceUris": [f"file://{cwd}"],
                },
                {
                    "id": "b2",
                    "type": 2,
                    "text": "",
                    "createdAt": 1706000001000,
                    "toolFormerData": {
                        "name": "Read",
                        "params": json.dumps({"file_path": "/tmp/test.py"}),
                        "result": json.dumps("print('hello')"),
                        "status": "completed",
                    },
                    "modelInfo": {"modelName": "claude-sonnet-4-20250514"},
                    "tokenCount": {"inputTokens": 50, "outputTokens": 10},
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        session = sessions[0]
        assert len(session["messages"]) == 2
        tool_message = session["messages"][1]
        assert tool_message["role"] == "assistant"
        assert len(tool_message["tool_uses"]) == 1
        tool_use = tool_message["tool_uses"][0]
        assert tool_use["tool"] == "Read"
        assert tool_use["status"] == "completed"
        assert "hello" in tool_use["output"]["text"]
        assert session["stats"]["tool_uses"] == 1

    def test_mcp_tool_prefix_stripped(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-3",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "search",
                    "createdAt": 1706000000000,
                    "workspaceUris": [f"file://{cwd}"],
                },
                {
                    "id": "b2",
                    "type": 2,
                    "text": "",
                    "createdAt": 1706000001000,
                    "toolFormerData": {
                        "name": "mcp_server_toolname",
                        "params": "{}",
                        "result": json.dumps("ok"),
                        "status": "completed",
                    },
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "toolname"

    def test_thinking_included(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-4",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": "Explain X",
                    "createdAt": 1706000000000,
                    "workspaceUris": [f"file://{cwd}"],
                },
                {
                    "id": "b2",
                    "type": 2,
                    "text": "Here's the answer.",
                    "createdAt": 1706000001000,
                    "thinking": {"text": "Let me reason about X..."},
                    "modelInfo": {"modelName": "claude-sonnet-4-20250514"},
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor", include_thinking=True)
        assert "thinking" in sessions[0]["messages"][1]
        assert "reason about X" in sessions[0]["messages"][1]["thinking"]

        sessions_no_thinking = parse_project_sessions(cwd, mock_anonymizer, source="cursor", include_thinking=False)
        assert "thinking" not in sessions_no_thinking[0]["messages"][1]

    def test_unknown_workspace_grouped(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        insert_cursor_conversation(
            conn,
            "conv-5",
            [
                {"id": "b1", "type": 1, "text": "Hello", "createdAt": 1706000000000},
                {"id": "b2", "type": 2, "text": "Hi", "createdAt": 1706000001000},
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions("<unknown-cwd>", mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "cursor:unknown"

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        assert parse_project_sessions("/no/such/path", mock_anonymizer, source="cursor") == []

    def test_nested_json_params_unwrapped(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        wrapped_params = {"tools": [{"parameters": json.dumps({"file_path": "/tmp/foo.py"})}]}
        insert_cursor_conversation(
            conn,
            "conv-6",
            [
                {"id": "b1", "type": 1, "text": "read", "createdAt": 1706000000000, "workspaceUris": [f"file://{cwd}"]},
                {
                    "id": "b2",
                    "type": 2,
                    "text": "",
                    "createdAt": 1706000001000,
                    "toolFormerData": {
                        "name": "Read",
                        "params": json.dumps(wrapped_params),
                        "result": json.dumps("contents"),
                        "status": "completed",
                    },
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert "file_path" in sessions[0]["messages"][1]["tool_uses"][0]["input"]

    def test_parser_does_not_pre_redact_message_content(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"cursor"})
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = write_cursor_db(db_path)
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
        insert_cursor_conversation(
            conn,
            "conv-secret",
            [
                {
                    "id": "b1",
                    "type": 1,
                    "text": f"Use this key: {secret}",
                    "createdAt": 1706000000000,
                    "workspaceUris": [f"file://{cwd}"],
                },
                {
                    "id": "b2",
                    "type": 2,
                    "text": "Noted.",
                    "createdAt": 1706000001000,
                },
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        content = sessions[0]["messages"][0]["content"]
        assert secret in content
        assert REDACTED not in content
