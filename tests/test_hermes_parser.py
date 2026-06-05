"""Tests for Hermes parser behavior."""

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.parsers import hermes
from dataclaw.secrets import transform_session
from tests.parser_helpers import disable_other_providers, write_hermes_db


def insert_session(
    conn,
    session_id="h1",
    source="cli",
    model="deepseek-v4-pro",
    started_at=1_766_000_000.0,
    ended_at=1_766_000_010.0,
    message_count=2,
    tool_call_count=0,
    input_tokens=100,
    output_tokens=25,
    cache_read_tokens=0,
    cache_write_tokens=0,
):
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, model, started_at, ended_at, message_count, tool_call_count,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source,
            model,
            started_at,
            ended_at,
            message_count,
            tool_call_count,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
        ),
    )


def insert_message(
    conn,
    session_id,
    role,
    content,
    timestamp,
    *,
    tool_call_id=None,
    tool_calls=None,
    tool_name=None,
    finish_reason=None,
    reasoning=None,
    reasoning_content=None,
):
    conn.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason, reasoning, reasoning_content
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            role,
            content,
            tool_call_id,
            json.dumps(tool_calls) if tool_calls is not None else None,
            tool_name,
            timestamp,
            finish_reason,
            reasoning,
            reasoning_content,
        ),
    )


class TestHermesDiscoverProjects:
    def test_discover_groups_by_session_source(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        db_path = tmp_path / "state.db"
        conn = write_hermes_db(db_path)
        insert_session(conn, "cli-1", source="cli", message_count=2)
        insert_session(conn, "cli-2", source="cli", message_count=3)
        insert_session(conn, "telegram-1", source="telegram", message_count=1)
        insert_session(conn, "empty", source="cron", message_count=0)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parsers.hermes._SESSION_SIZE_MAP", {})

        projects = discover_projects()

        assert {project["display_name"] for project in projects} == {"hermes:cli", "hermes:telegram"}
        counts = {project["display_name"]: project["session_count"] for project in projects}
        assert counts["hermes:cli"] == 2
        assert counts["hermes:telegram"] == 1

    def test_missing_db_returns_no_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", tmp_path / "missing.db")
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})

        assert discover_projects() == []


class TestHermesParseSessions:
    def test_basic_session_and_tool_call_mapping(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        db_path = tmp_path / "state.db"
        conn = write_hermes_db(db_path)
        insert_session(
            conn,
            "h1",
            source="cli",
            model="deepseek-v4-pro",
            message_count=4,
            tool_call_count=1,
            input_tokens=100,
            output_tokens=25,
            cache_read_tokens=7,
            cache_write_tokens=3,
        )
        insert_message(conn, "h1", "user", "List files", 1_766_000_000.0)
        insert_message(
            conn,
            "h1",
            "assistant",
            "I will inspect the repo.",
            1_766_000_001.0,
            tool_calls=[
                {
                    "id": "call_1",
                    "call_id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": json.dumps({"command": "ls -la"})},
                }
            ],
            finish_reason="tool_calls",
        )
        insert_message(conn, "h1", "tool", "README.md\npyproject.toml", 1_766_000_002.0, tool_call_id="call_1")
        insert_message(conn, "h1", "assistant", "Done.", 1_766_000_003.0)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parsers.hermes._SESSION_SIZE_MAP", {})

        sessions = parse_project_sessions("cli", mock_anonymizer, source="hermes")

        assert len(sessions) == 1
        session = sessions[0]
        assert session["session_id"] == "h1"
        assert session["source"] == "hermes"
        assert session["project"] == "hermes:cli"
        assert session["model"] == "deepseek-v4-pro"
        assert session["start_time"] == "2025-12-17T19:33:20+00:00"
        assert session["stats"]["input_tokens"] == 110
        assert session["stats"]["output_tokens"] == 25
        assert session["stats"]["user_messages"] == 1
        assert session["stats"]["assistant_messages"] == 2
        assert session["stats"]["tool_uses"] == 1
        tool_use = session["messages"][1]["tool_uses"][0]
        assert tool_use["tool"] == "terminal"
        assert tool_use["id"] == "call_1"
        assert tool_use["input"] == {"command": "ls -la"}
        assert tool_use["output"] == {"text": "README.md\npyproject.toml"}
        assert tool_use["status"] == "success"

    def test_include_thinking_controls_reasoning_fields(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        db_path = tmp_path / "state.db"
        conn = write_hermes_db(db_path)
        insert_session(conn, "h2", message_count=2)
        insert_message(conn, "h2", "user", "Think", 1_766_000_000.0)
        insert_message(
            conn,
            "h2",
            "assistant",
            "Answer",
            1_766_000_001.0,
            reasoning="private scratch",
            reasoning_content="private scratch",
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})

        with_thinking = parse_project_sessions("cli", mock_anonymizer, include_thinking=True, source="hermes")
        without_thinking = parse_project_sessions("cli", mock_anonymizer, include_thinking=False, source="hermes")

        assert with_thinking[0]["messages"][1]["thinking"] == "private scratch"
        assert "thinking" not in without_thinking[0]["messages"][1]

    def test_empty_session_is_skipped(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        db_path = tmp_path / "state.db"
        conn = write_hermes_db(db_path)
        insert_session(conn, "empty", message_count=1)
        insert_message(conn, "empty", "assistant", "", 1_766_000_001.0)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})

        assert parse_project_sessions("cli", mock_anonymizer, source="hermes") == []

    def test_export_task_parse_missing_db_returns_none(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", tmp_path / "missing.db")
        task = hermes.ExportSessionTask(
            source="hermes",
            project_index=0,
            task_index=0,
            project_dir_name="cli",
            project_display_name="hermes:cli",
            estimated_bytes=0,
            kind="hermes",
            item_id="h1",
        )

        assert hermes.parse_export_session_task(task, mock_anonymizer, include_thinking=True) is None

    def test_anonymizer_is_applied_by_export_transform(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"hermes"})
        db_path = tmp_path / "state.db"
        conn = write_hermes_db(db_path)
        insert_session(conn, "h3", message_count=2)
        insert_message(conn, "h3", "user", "Open /Users/testuser/work/app.py", 1_766_000_000.0)
        insert_message(conn, "h3", "assistant", "Reading /Users/testuser/work/app.py", 1_766_000_001.0)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.hermes.HERMES_DB", db_path)
        monkeypatch.setattr("dataclaw.parsers.hermes._PROJECT_INDEX", {})

        session = parse_project_sessions("cli", mock_anonymizer, source="hermes")[0]
        transformed, _ = transform_session(session, mock_anonymizer)

        assert "/Users/testuser" not in transformed["messages"][0]["content"]
        assert "/Users/testuser" not in transformed["messages"][1]["content"]
