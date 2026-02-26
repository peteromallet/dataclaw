"""Tests for dataclaw.parser — JSONL parsing and project discovery."""

import json
import sqlite3

import pytest

from dataclaw.parser import (
    _build_project_name,
    _extract_assistant_content,
    _extract_user_content,
    _normalize_timestamp,
    _parse_session_file,
    _process_entry,
    _summarize_tool_input,
    discover_projects,
    parse_project_sessions,
)


# --- _build_project_name ---


class TestBuildProjectName:
    def test_documents_prefix(self):
        assert _build_project_name("-Users-alice-Documents-myproject") == "myproject"

    def test_home_prefix(self):
        assert _build_project_name("-home-bob-project") == "project"

    def test_standalone(self):
        assert _build_project_name("standalone") == "standalone"

    def test_deep_documents_path(self):
        result = _build_project_name("-Users-alice-Documents-work-repo")
        assert result == "work-repo"

    def test_downloads_prefix(self):
        assert _build_project_name("-Users-alice-Downloads-thing") == "thing"

    def test_desktop_prefix(self):
        assert _build_project_name("-Users-alice-Desktop-stuff") == "stuff"

    def test_bare_home(self):
        # /Users/alice -> just username, no project
        assert _build_project_name("-Users-alice") == "~home"

    def test_users_common_dir_only(self):
        # /Users/alice/Documents (no project after common dir)
        assert _build_project_name("-Users-alice-Documents") == "~Documents"

    def test_home_bare(self):
        assert _build_project_name("-home-bob") == "~home"

    def test_non_common_dir(self):
        # /Users/alice/code/myproject
        result = _build_project_name("-Users-alice-code-myproject")
        assert result == "code-myproject"

    def test_empty_string(self):
        # Empty string: path="" -> parts=[""] -> meaningful=[""] -> returns ""
        result = _build_project_name("")
        assert result == ""

    def test_linux_deep_path(self):
        assert _build_project_name("-home-bob-projects-app") == "projects-app"

    def test_hyphens_preserved_in_project_name(self):
        result = _build_project_name("-Users-alice-Documents-my-cool-project")
        assert result == "my-cool-project"


# --- _normalize_timestamp ---


class TestNormalizeTimestamp:
    def test_none(self):
        assert _normalize_timestamp(None) is None

    def test_string_passthrough(self):
        ts = "2025-01-15T10:00:00+00:00"
        assert _normalize_timestamp(ts) == ts

    def test_int_ms_to_iso(self):
        # 1706000000000 ms = 2024-01-23T09:33:20+00:00
        result = _normalize_timestamp(1706000000000)
        assert result is not None
        assert "2024" in result
        assert "T" in result

    def test_float_ms_to_iso(self):
        result = _normalize_timestamp(1706000000000.0)
        assert result is not None
        assert "T" in result

    def test_other_type_returns_none(self):
        assert _normalize_timestamp([1, 2, 3]) is None
        assert _normalize_timestamp({"ts": 123}) is None


# --- _summarize_tool_input ---


class TestSummarizeToolInput:
    def test_read_tool(self, mock_anonymizer):
        result = _summarize_tool_input("Read", {"file_path": "/tmp/test.py"}, mock_anonymizer)
        assert "test.py" in result

    def test_write_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "Write", {"file_path": "/tmp/test.py", "content": "abc"}, mock_anonymizer,
        )
        assert "test.py" in result
        assert "3 chars" in result

    def test_bash_tool(self, mock_anonymizer):
        result = _summarize_tool_input("Bash", {"command": "ls -la"}, mock_anonymizer)
        assert "ls -la" in result

    def test_grep_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "Grep", {"pattern": "TODO", "path": "/tmp"}, mock_anonymizer,
        )
        assert "pattern=" in result
        assert "path=" in result

    def test_glob_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "Glob", {"pattern": "*.py", "path": "/tmp"}, mock_anonymizer,
        )
        assert "pattern=" in result

    def test_task_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "Task", {"prompt": "Search for bugs"}, mock_anonymizer,
        )
        assert "Search for bugs" in result

    def test_websearch_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "WebSearch", {"query": "python async"}, mock_anonymizer,
        )
        assert "python async" in result

    def test_webfetch_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "WebFetch", {"url": "https://example.com"}, mock_anonymizer,
        )
        assert "https://example.com" in result

    def test_unknown_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "CustomTool", {"foo": "bar"}, mock_anonymizer,
        )
        assert "foo" in result or "bar" in result

    def test_edit_tool(self, mock_anonymizer):
        result = _summarize_tool_input(
            "Edit", {"file_path": "/tmp/test.py"}, mock_anonymizer,
        )
        assert "test.py" in result

    def test_none_tool_name(self, mock_anonymizer):
        result = _summarize_tool_input(None, {"data": "value"}, mock_anonymizer)
        assert isinstance(result, str)

    def test_non_dict_input(self, mock_anonymizer):
        result = _summarize_tool_input("Read", "just a string", mock_anonymizer)
        assert isinstance(result, str)


# --- _extract_user_content ---


class TestExtractUserContent:
    def test_string_content(self, mock_anonymizer):
        entry = {"message": {"content": "Fix the bug"}}
        result = _extract_user_content(entry, mock_anonymizer)
        assert result == "Fix the bug"

    def test_list_content(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ]
            }
        }
        result = _extract_user_content(entry, mock_anonymizer)
        assert "Hello" in result
        assert "World" in result

    def test_empty_content(self, mock_anonymizer):
        entry = {"message": {"content": ""}}
        assert _extract_user_content(entry, mock_anonymizer) is None

    def test_whitespace_content(self, mock_anonymizer):
        entry = {"message": {"content": "   \n  "}}
        assert _extract_user_content(entry, mock_anonymizer) is None

    def test_missing_message(self, mock_anonymizer):
        entry = {}
        assert _extract_user_content(entry, mock_anonymizer) is None


# --- _extract_assistant_content ---


class TestExtractAssistantContent:
    def test_text_blocks(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Here's the fix."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert result is not None
        assert result["content"] == "Here's the fix."

    def test_thinking_included(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "Done."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert "thinking" in result
        assert "Let me think..." in result["thinking"]

    def test_thinking_excluded(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "Done."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=False)
        assert "thinking" not in result

    def test_tool_uses(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert result is not None
        assert len(result["tool_uses"]) == 1
        assert result["tool_uses"][0]["tool"] == "Read"

    def test_empty_content(self, mock_anonymizer):
        entry = {"message": {"content": []}}
        assert _extract_assistant_content(entry, mock_anonymizer, True) is None

    def test_non_list_content(self, mock_anonymizer):
        entry = {"message": {"content": "just a string"}}
        assert _extract_assistant_content(entry, mock_anonymizer, True) is None

    def test_non_dict_block_skipped(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    "not a dict",
                    {"type": "text", "text": "Valid."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, True)
        assert result is not None
        assert result["content"] == "Valid."


# --- _process_entry ---


class TestProcessEntry:
    def _run(self, entry, anonymizer, include_thinking=True):
        messages = []
        metadata = {
            "session_id": "test", "cwd": None, "git_branch": None,
            "claude_version": None, "model": None,
            "start_time": None, "end_time": None,
        }
        stats = {
            "user_messages": 0, "assistant_messages": 0,
            "tool_uses": 0, "input_tokens": 0, "output_tokens": 0,
        }
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking)
        return messages, metadata, stats

    def test_user_entry(self, mock_anonymizer, sample_user_entry):
        msgs, meta, stats = self._run(sample_user_entry, mock_anonymizer)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert stats["user_messages"] == 1
        assert meta["git_branch"] == "main"

    def test_assistant_entry(self, mock_anonymizer, sample_assistant_entry):
        msgs, meta, stats = self._run(sample_assistant_entry, mock_anonymizer)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert stats["assistant_messages"] == 1
        assert stats["input_tokens"] > 0
        assert stats["output_tokens"] > 0

    def test_unknown_type(self, mock_anonymizer):
        entry = {"type": "system", "message": {}}
        msgs, _, _ = self._run(entry, mock_anonymizer)
        assert len(msgs) == 0

    def test_metadata_extraction(self, mock_anonymizer, sample_user_entry):
        _, meta, _ = self._run(sample_user_entry, mock_anonymizer)
        assert meta["cwd"] is not None
        assert meta["claude_version"] == "1.0.0"
        assert meta["start_time"] is not None


# --- _parse_session_file ---


class TestParseSessionFile:
    def test_valid_jsonl(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        entries = [
            {"type": "user", "timestamp": 1706000000000,
             "message": {"content": "Hello"}, "cwd": "/tmp/proj"},
            {"type": "assistant", "timestamp": 1706000001000,
             "message": {
                 "model": "claude-sonnet-4-20250514",
                 "content": [{"type": "text", "text": "Hi there!"}],
                 "usage": {"input_tokens": 10, "output_tokens": 5},
             }},
        ]
        f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_malformed_lines_skipped(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/tmp"}\n'
            "not valid json\n"
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2

    def test_empty_file(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text("")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is None

    def test_oserror_returns_none(self, tmp_path, mock_anonymizer):
        f = tmp_path / "nonexistent.jsonl"
        result = _parse_session_file(f, mock_anonymizer)
        assert result is None

    def test_blank_lines_skipped(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text(
            "\n\n"
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/tmp"}\n'
            "\n"
        )
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 1


# --- discover_projects + parse_project_sessions ---


class TestDiscoverProjects:
    def _disable_codex(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude-projects")
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})

    def _write_opencode_db(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE session ("
            "id TEXT PRIMARY KEY, "
            "directory TEXT, "
            "time_created INTEGER, "
            "time_updated INTEGER"
            ")"
        )
        conn.execute(
            "CREATE TABLE message ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT, "
            "time_created INTEGER, "
            "data TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE part ("
            "id TEXT PRIMARY KEY, "
            "message_id TEXT, "
            "time_created INTEGER, "
            "data TEXT"
            ")"
        )
        conn.commit()
        return conn

    def test_with_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-alice-Documents-myapp"
        proj.mkdir(parents=True)

        # Write a valid session file
        session = proj / "abc-123.jsonl"
        session.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/tmp"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hey"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "myapp"
        assert projects[0]["session_count"] == 1

    def test_no_projects_dir(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "nonexistent")
        assert discover_projects() == []

    def test_empty_project_dir(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "empty-project"
        proj.mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        assert discover_projects() == []

    def test_parse_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "test-project"
        proj.mkdir(parents=True)

        session = proj / "session1.jsonl"
        session.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/tmp"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("test-project", mock_anonymizer)
        assert len(sessions) == 1
        assert sessions[0]["project"] == "test-project"

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "projects")
        assert parse_project_sessions("nope", mock_anonymizer) == []

    def test_discover_codex_projects(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir / "nonexistent")

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "timestamp": "2026-02-24T16:09:59.567Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "session-1",
                        "cwd": "/Users/testuser/Documents/myrepo",
                        "model_provider": "openai",
                    },
                }
            ) + "\n"
        )

        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "codex"
        assert projects[0]["display_name"] == "codex:myrepo"

    def test_parse_codex_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "projects" / "nonexistent")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        lines = [
            {
                "timestamp": "2026-02-24T16:09:59.567Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-1",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                    "git": {"branch": "main"},
                },
            },
            {
                "timestamp": "2026-02-24T16:09:59.568Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.3-codex",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "please list files",
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "ls -la"}),
                },
            },
            {
                "timestamp": "2026-02-24T16:10:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "I checked the directory.",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 30,
                            "output_tokens": 40,
                        }
                    },
                    "rate_limits": {},
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        sessions = parse_project_sessions(
            "/Users/testuser/Documents/myrepo",
            mock_anonymizer,
            source="codex",
        )
        assert len(sessions) == 1
        assert sessions[0]["project"] == "codex:myrepo"
        assert sessions[0]["model"] == "gpt-5.3-codex"
        assert sessions[0]["stats"]["input_tokens"] == 150
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "exec_command"

    def test_discover_opencode_projects(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        db_path = tmp_path / "opencode.db"
        conn = self._write_opencode_db(db_path)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_1", "/Users/testuser/work/repo", 1706000000000, 1706000002000),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "opencode"
        assert projects[0]["display_name"] == "opencode:repo"

    def test_parse_opencode_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        db_path = tmp_path / "opencode.db"
        conn = self._write_opencode_db(db_path)

        session_id = "ses_1"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_msg_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        assistant_msg_data = {
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
            ("msg_1", session_id, 1706000001000, json.dumps(user_msg_data)),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_2", session_id, 1706000002000, json.dumps(assistant_msg_data)),
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
            (
                "prt_4",
                "msg_2",
                1706000002003,
                json.dumps({"type": "text", "text": "I checked the directory."}),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "opencode:repo"
        assert sessions[0]["model"] == "openai/gpt-5.3-codex"
        assert sessions[0]["stats"]["input_tokens"] == 150
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "bash"
