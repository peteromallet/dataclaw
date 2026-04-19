"""Tests for OpenCode parser behavior."""

import sqlite3
from pathlib import Path

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from tests.parser_helpers import disable_other_providers, write_opencode_db


class TestOpenCodeProjects:
    def test_discover_opencode_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_1", "/Users/testuser/work/repo", 1706000000000, 1706000002000),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "opencode"
        assert projects[0]["display_name"] == "opencode:repo"

    def test_parse_opencode_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_1"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_message_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        assistant_message_data = {
            "role": "assistant",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
            "tokens": {
                "input": 120,
                "output": 40,
                "reasoning": 10,
                "cache": {"read": 30, "write": 0},
            },
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_1", session_id, 1706000001000, json.dumps(user_message_data)),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_2", session_id, 1706000002000, json.dumps(assistant_message_data)),
        )

        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_1", "msg_1", 1706000001001, json.dumps({"type": "text", "text": "please list files"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_2", "msg_2", 1706000002001, json.dumps({"type": "reasoning", "text": "Thinking..."})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_3",
                "msg_2",
                1706000002002,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "ls -la"}},
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_4", "msg_2", 1706000002003, json.dumps({"type": "text", "text": "I checked the directory."})),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "opencode:repo"
        assert sessions[0]["model"] == "openai/gpt-5.3-codex"
        assert sessions[0]["stats"]["input_tokens"] == 150
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "bash"

    def test_parse_opencode_user_file_parts(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_files"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_message_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_file", session_id, 1706000001000, json.dumps(user_message_data)),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_text",
                "msg_file",
                1706000001001,
                json.dumps({"type": "text", "text": "Please inspect these files."}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_image",
                "msg_file",
                1706000001002,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "image/png",
                        "filename": "plot.png",
                        "url": "data:image/png;base64,QUJDRA==",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_doc",
                "msg_file",
                1706000001003,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "notes.txt",
                        "url": "file:///Users/testuser/work/repo/notes.txt",
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert message["content"] == "Please inspect these files."
        assert message["content_parts"][0] == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "QUJDRA==",
            },
        }
        assert message["content_parts"][1]["type"] == "document"
        assert message["content_parts"][1]["source"]["type"] == "url"
        assert message["content_parts"][1]["source"]["media_type"] == "text/plain"
        assert message["content_parts"][1]["source"]["url"].startswith("file:///Users/testuser")
        assert message["content_parts"][1]["source"]["url"].endswith("/work/repo/notes.txt")

    def test_parse_opencode_user_file_only_message(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_file_only"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_file_only",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_file_only",
                "msg_file_only",
                1706000001001,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "image/png",
                        "filename": "plot.png",
                        "url": "data:image/png;base64," + ("A" * 5000),
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert "content" not in message
        assert message["content_parts"] == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 5000,
                },
            }
        ]

    def test_parse_project_sessions_reuses_single_db_connection(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        cwd = "/Users/testuser/work/repo"
        for index in range(2):
            session_id = f"ses_{index}"
            message_id = f"msg_{index}"
            part_id = f"prt_{index}"
            conn.execute(
                "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
                (session_id, cwd, 1706000000000 + index, 1706000005000 + index),
            )
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    1706000001000 + index,
                    json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
                ),
            )
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part_id, message_id, 1706000001001 + index, json.dumps({"type": "text", "text": f"Hello {index}"})),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        connect_calls = 0
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            nonlocal connect_calls
            if args and Path(args[0]) == db_path:
                connect_calls += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("dataclaw.parsers.opencode.sqlite3.connect", counting_connect)

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 2
        assert connect_calls == 2

    def test_parse_opencode_surrogate_escaped_tool_output(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_surrogate"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_assistant",
                session_id,
                1706000002000,
                json.dumps({"role": "assistant", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_user", "msg_user", 1706000001001, json.dumps({"type": "text", "text": "show output"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_tool",
                "msg_assistant",
                1706000002001,
                '{"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"python bad.py"},"output":"prefix '
                + "\\udcbf"
                + ' suffix"}}',
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        assert sessions[0]["messages"][1]["tool_uses"][0]["output"] == {"text": r"prefix \xbf suffix"}
        assert json.dumps(sessions[0])
