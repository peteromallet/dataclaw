"""Tests for OpenCode parser behavior."""

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
