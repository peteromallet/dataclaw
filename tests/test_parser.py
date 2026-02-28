"""Tests for dataclaw.parser — JSONL parsing and project discovery."""

import json
import sqlite3

import pytest

from dataclaw.parser import (
    _build_project_name,
    _build_tool_result_map,
    _build_codex_tool_result_map,
    _extract_assistant_content,
    _extract_user_content,
    _find_subagent_only_sessions,
    _normalize_timestamp,
    _parse_session_file,
    _parse_subagent_session,
    _parse_tool_input,
    _process_entry,
    discover_projects,
    parse_project_sessions,
    _parse_codex_session_file,
    _parse_openclaw_session_file,
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


# --- _parse_tool_input ---


class TestParseToolInput:
    def test_read_tool(self, mock_anonymizer):
        result = _parse_tool_input("Read", {"file_path": "/tmp/test.py"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "test.py" in result["file_path"]

    def test_write_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Write", {"file_path": "/tmp/test.py", "content": "abc"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "content" in result

    def test_bash_tool(self, mock_anonymizer):
        result = _parse_tool_input("Bash", {"command": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["command"] == "ls -la"

    def test_grep_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Grep", {"pattern": "TODO", "path": "/tmp"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "pattern" in result
        assert "path" in result

    def test_glob_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Glob", {"pattern": "*.py", "path": "/tmp"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["pattern"] == "*.py"

    def test_task_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Task", {"prompt": "Search for bugs"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "Search for bugs" in result["prompt"]

    def test_websearch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebSearch", {"query": "python async"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["query"] == "python async"

    def test_webfetch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebFetch", {"url": "https://example.com"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com"

    def test_edit_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Edit", {"file_path": "/tmp/test.py"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result

    def test_exec_command_tool(self, mock_anonymizer):
        result = _parse_tool_input("exec_command", {"cmd": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["cmd"] == "ls -la"

    def test_shell_command_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "shell_command", {"command": "ls", "workdir": "/tmp"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["command"] == "ls"
        assert "workdir" in result

    def test_update_plan_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "update_plan",
            {"explanation": "New plan", "plan": [{"step": "do it", "status": "pending"}]},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "explanation" in result
        assert "plan" in result

    def test_unknown_tool(self, mock_anonymizer):
        result = _parse_tool_input("CustomTool", {"foo": "bar"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_none_tool_name(self, mock_anonymizer):
        result = _parse_tool_input(None, {"data": "value"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_non_dict_input(self, mock_anonymizer):
        result = _parse_tool_input("Read", "just a string", mock_anonymizer)
        assert isinstance(result, dict)
        assert "raw" in result


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
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "no-cursor.vscdb")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

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

    def test_codex_thinking_not_duplicated(self, tmp_path, monkeypatch, mock_anonymizer):
        """Reasoning from response_item and agent_reasoning event_msg should not duplicate."""
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "projects" / "nonexistent")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "25"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-2.jsonl"
        lines = [
            {
                "timestamp": "2026-02-25T10:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-2",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-02-25T10:00:00.001Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.3-codex",
                },
            },
            {
                "timestamp": "2026-02-25T10:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "fix the bug"},
            },
            {
                "timestamp": "2026-02-25T10:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Planning fix"}, {"text": "Reading code"}],
                },
            },
            {
                "timestamp": "2026-02-25T10:00:02.001Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Planning fix"},
            },
            {
                "timestamp": "2026-02-25T10:00:02.002Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Reading code"},
            },
            {
                "timestamp": "2026-02-25T10:00:03.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "I found the issue."},
            },
        ]
        session_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        from dataclaw.anonymizer import Anonymizer
        anonymizer = Anonymizer()

        result = _parse_codex_session_file(
            session_file, anonymizer, include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )
        assert result is not None
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        thinking = assistant_msgs[0]["thinking"]
        paragraphs = [p.strip() for p in thinking.split("\n\n") if p.strip()]
        assert paragraphs == ["Planning fix", "Reading code"]

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


# --- Subagent-only session discovery and parsing ---


def _make_subagent_entry(role, content, timestamp, cwd=None, session_id=None):
    """Build a minimal JSONL entry matching the subagent file format."""
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


class TestFindSubagentOnlySessions:
    def test_finds_subagent_dirs_without_root_jsonl(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()

        # Session with root JSONL — should NOT be returned.
        (proj / "has-root.jsonl").write_text("{}\n")
        sa_dir = proj / "has-root" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a1.jsonl").write_text("{}\n")

        # Session with only subagent data — SHOULD be returned.
        sa_dir2 = proj / "subagent-only" / "subagents"
        sa_dir2.mkdir(parents=True)
        (sa_dir2 / "agent-b1.jsonl").write_text("{}\n")

        result = _find_subagent_only_sessions(proj)
        assert len(result) == 1
        assert result[0].name == "subagent-only"

    def test_ignores_dirs_without_subagents(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()

        # Directory with only tool-results, no subagents.
        (proj / "tool-only" / "tool-results").mkdir(parents=True)

        result = _find_subagent_only_sessions(proj)
        assert result == []

    def test_ignores_empty_subagent_dirs(self, tmp_path):
        proj = tmp_path / "project"
        (proj / "empty-sa" / "subagents").mkdir(parents=True)

        result = _find_subagent_only_sessions(proj)
        assert result == []

    def test_returns_empty_for_no_dirs(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}\n")

        result = _find_subagent_only_sessions(proj)
        assert result == []


class TestParseSubagentSession:
    def test_merges_multiple_files_sorted_by_timestamp(self, tmp_path, mock_anonymizer):
        session = tmp_path / "abc-123"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)

        # Write entries across two subagent files with interleaved timestamps.
        (sa_dir / "agent-a1.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "First message", "2026-01-10T08:00:00Z",
                cwd="/tmp/proj", session_id="abc-123",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Third reply", "2026-01-10T08:02:00Z",
            )) + "\n"
        )
        (sa_dir / "agent-b2.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "assistant", "Second reply", "2026-01-10T08:01:00Z",
            )) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "abc-123"
        assert len(result["messages"]) == 3
        # Verify sort order: user(08:00), assistant(08:01), assistant(08:02)
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "First message"
        assert result["messages"][1]["content"] == "Second reply"
        assert result["messages"][2]["content"] == "Third reply"
        assert result["model"] == "claude-opus-4-5-20251101"

    def test_returns_none_for_empty_subagents(self, tmp_path, mock_anonymizer):
        session = tmp_path / "empty"
        (session / "subagents").mkdir(parents=True)

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_returns_none_for_no_subagent_dir(self, tmp_path, mock_anonymizer):
        session = tmp_path / "no-sa"
        session.mkdir()

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_returns_none_when_no_messages(self, tmp_path, mock_anonymizer):
        session = tmp_path / "no-msgs"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)
        # Entry with unknown type — produces no messages.
        (sa_dir / "agent-x.jsonl").write_text(
            json.dumps({"type": "system", "timestamp": "2026-01-01T00:00:00Z"}) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_stats_aggregated(self, tmp_path, mock_anonymizer):
        session = tmp_path / "stats-test"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)

        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Hello", "2026-01-10T10:00:00Z", cwd="/tmp/p",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Hi", "2026-01-10T10:00:01Z",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Done", "2026-01-10T10:00:02Z",
            )) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is not None
        assert result["stats"]["user_messages"] == 1
        assert result["stats"]["assistant_messages"] == 2
        assert result["stats"]["input_tokens"] == 100  # 50 * 2
        assert result["stats"]["output_tokens"] == 40  # 20 * 2


class TestDiscoverSubagentProjects:
    """Verify discover_projects and parse_project_sessions include subagent-only sessions."""

    def _disable_codex(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "no-cursor.vscdb")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

    def test_discover_includes_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-alice-Documents-research"
        proj.mkdir(parents=True)

        # One root session.
        (proj / "root-session.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Hi", "2026-01-01T00:00:00Z", cwd="/tmp",
            )) + "\n"
        )

        # One subagent-only session.
        sa_dir = proj / "subagent-session" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Build it", "2026-01-02T00:00:00Z", cwd="/tmp",
            )) + "\n"
        )

        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "research"

    def test_discover_subagent_only_project(self, tmp_path, monkeypatch, mock_anonymizer):
        """A project with zero root .jsonl but subagent sessions should still appear."""
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "subagent-project"
        proj.mkdir(parents=True)

        sa_dir = proj / "session-uuid" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Do work", "2026-01-01T00:00:00Z", cwd="/tmp",
            )) + "\n"
        )

        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 1

    def test_parse_includes_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "mixed-project"
        proj.mkdir(parents=True)

        # Root session.
        (proj / "root.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Root msg", "2026-01-01T00:00:00Z", cwd="/tmp",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Root reply", "2026-01-01T00:00:01Z",
            )) + "\n"
        )

        # Subagent-only session.
        sa_dir = proj / "sa-session" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "SA msg", "2026-01-02T00:00:00Z", cwd="/tmp",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "SA reply", "2026-01-02T00:00:01Z",
            )) + "\n"
        )

        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("mixed-project", mock_anonymizer)
        assert len(sessions) == 2
        contents = {s["messages"][0]["content"] for s in sessions}
        assert "Root msg" in contents
        assert "SA msg" in contents


# --- _build_tool_result_map (Claude tool outputs) ---


class TestBuildToolResultMap:
    def test_basic_string_output(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-1",
                            "content": "file contents here",
                            "is_error": False,
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "tu-1" in result
        assert result["tu-1"]["status"] == "success"
        assert result["tu-1"]["output"]["text"] == "file contents here"

    def test_error_result(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-2",
                            "content": "Permission denied",
                            "is_error": True,
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert result["tu-2"]["status"] == "error"

    def test_list_content(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-3",
                            "content": [
                                {"type": "text", "text": "Part one"},
                                {"type": "text", "text": "Part two"},
                            ],
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "Part one" in result["tu-3"]["output"]["text"]
        assert "Part two" in result["tu-3"]["output"]["text"]

    def test_empty_content_gives_empty_output(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-4", "content": ""}
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert result["tu-4"]["output"] == {}

    def test_non_user_entries_ignored(self, mock_anonymizer):
        entries = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-5", "content": "ignored"}
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "tu-5" not in result

    def test_tool_output_attached_in_session(self, tmp_path, mock_anonymizer):
        """End-to-end: tool_use in assistant entry gets output from tool_result in user entry."""
        f = tmp_path / "session.jsonl"
        entries = [
            {
                "type": "assistant",
                "timestamp": 1706000001000,
                "message": {
                    "model": "claude-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-abc",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "user",
                "timestamp": 1706000002000,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-abc",
                            "content": "file1.py\nfile2.py",
                            "is_error": False,
                        }
                    ]
                },
            },
        ]
        f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        tu = result["messages"][0]["tool_uses"][0]
        assert tu["tool"] == "Bash"
        assert tu["status"] == "success"
        assert "file1.py" in tu["output"]["text"]


# --- _build_codex_tool_result_map ---


class TestBuildCodexToolResultMap:
    def test_function_call_output(self, mock_anonymizer):
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Exit code: 0\nWall time: 1 seconds\nOutput:\nhello world\n",
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-1" in result
        assert result["call-1"]["status"] == "success"
        assert result["call-1"]["output"]["exit_code"] == 0
        assert result["call-1"]["output"]["wall_time"] == "1 seconds"
        assert "hello world" in result["call-1"]["output"]["output"]

    def test_custom_tool_call_output(self, mock_anonymizer):
        import json as _json
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-2",
                    "output": _json.dumps({
                        "output": "Successfully applied patch",
                        "metadata": {"exit_code": 0, "duration_seconds": 0.5},
                    }),
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-2" in result
        assert result["call-2"]["output"]["exit_code"] == 0
        assert "Successfully applied patch" in result["call-2"]["output"]["output"]
        assert result["call-2"]["output"]["duration_seconds"] == 0.5

    def test_non_response_item_ignored(self, mock_anonymizer):
        entries = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-3",
                    "output": "ignored",
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-3" not in result

    def test_output_attached_end_to_end(self, tmp_path, monkeypatch, mock_anonymizer):
        """Codex tool output is attached to the tool_use in the parsed session."""
        import json as _json
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        lines = [
            {
                "timestamp": "2026-02-24T16:09:59.567Z",
                "type": "session_meta",
                "payload": {"id": "s1", "cwd": "/home/user/repo", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-02-24T16:10:00.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "run ls"},
            },
            {
                "timestamp": "2026-02-24T16:10:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-x",
                    "arguments": _json.dumps({"command": "ls", "workdir": "/home/user/repo"}),
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.200Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-x",
                    "output": "Exit code: 0\nWall time: 0 seconds\nOutput:\nfoo.py\nbar.py\n",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:01.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Done."},
            },
        ]
        session_file.write_text("\n".join(_json.dumps(l) for l in lines) + "\n")

        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = _parse_codex_session_file(
            session_file, mock_anonymizer, include_thinking=True,
            target_cwd="/home/user/repo",
        )
        assert result is not None
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        tu = assistant_msgs[0]["tool_uses"][0]
        assert tu["tool"] == "shell_command"
        assert tu["status"] == "success"
        assert tu["output"]["exit_code"] == 0
        assert "foo.py" in tu["output"]["output"]


# --- OpenClaw session parsing ---


def _make_openclaw_session_header(session_id="oc-sess-1", cwd="/Users/alice/projects/myapp"):
    return {
        "type": "session",
        "id": session_id,
        "cwd": cwd,
        "timestamp": "2026-02-20T10:00:00.000Z",
    }


def _make_openclaw_user_message(text, timestamp="2026-02-20T10:01:00.000Z"):
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_openclaw_assistant_message(
    text, timestamp="2026-02-20T10:02:00.000Z", model="claude-sonnet-4-20250514",
    thinking=None, tool_calls=None, usage=None,
):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for tc in (tool_calls or []):
        content.append(tc)
    msg = {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
        },
    }
    if usage:
        msg["message"]["usage"] = usage
    return msg


def _make_openclaw_tool_result(tool_call_id, output_text, is_error=False):
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


class TestParseOpenclawSessionFile:
    def test_basic_conversation(self, mock_anonymizer):
        """Parse a simple user/assistant conversation."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi there!", usage={"input": 50, "output": 20}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "oc-sess-1"
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == "Hi there!"
        assert result["stats"]["user_messages"] == 1
        assert result["stats"]["assistant_messages"] == 1
        assert result["stats"]["input_tokens"] == 50
        assert result["stats"]["output_tokens"] == 20
        fpath.unlink()

    def test_thinking_included(self, mock_anonymizer):
        """Thinking blocks should be included when include_thinking=True."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Explain X"),
            _make_openclaw_assistant_message("Here's the answer", thinking="Let me think about X..."),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer, include_thinking=True)
        assistant_msg = result["messages"][1]
        assert "thinking" in assistant_msg
        assert "Let me think about X" in assistant_msg["thinking"]

        result_no_think = _parse_openclaw_session_file(fpath, mock_anonymizer, include_thinking=False)
        assistant_msg_no_think = result_no_think["messages"][1]
        assert "thinking" not in assistant_msg_no_think
        fpath.unlink()

    def test_tool_calls_with_results(self, mock_anonymizer):
        """Tool calls should be paired with their results."""
        import tempfile
        from pathlib import Path

        tool_call = {
            "type": "toolCall",
            "id": "tc-1",
            "name": "read_file",
            "arguments": {"path": "/tmp/test.py"},
        }
        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Read the file"),
            _make_openclaw_assistant_message("Let me read that", tool_calls=[tool_call]),
            _make_openclaw_tool_result("tc-1", "print('hello')"),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assistant_msg = result["messages"][1]
        assert len(assistant_msg["tool_uses"]) == 1
        tu = assistant_msg["tool_uses"][0]
        assert tu["tool"] == "read_file"
        assert tu["status"] == "success"
        assert "hello" in tu["output"]["text"]
        assert result["stats"]["tool_uses"] == 1
        fpath.unlink()

    def test_error_tool_result(self, mock_anonymizer):
        """Tool results with isError=True should have status 'error'."""
        import tempfile
        from pathlib import Path

        tool_call = {
            "type": "toolCall",
            "id": "tc-err",
            "name": "bash",
            "arguments": {"command": "rm /nope"},
        }
        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Delete it"),
            _make_openclaw_assistant_message("Trying", tool_calls=[tool_call]),
            _make_openclaw_tool_result("tc-err", "Permission denied", is_error=True),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        tu = result["messages"][1]["tool_uses"][0]
        assert tu["status"] == "error"
        fpath.unlink()

    def test_empty_file_returns_none(self, mock_anonymizer):
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is None
        fpath.unlink()

    def test_no_session_header_returns_none(self, mock_anonymizer):
        import tempfile
        from pathlib import Path

        lines = [_make_openclaw_user_message("Hello")]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is None
        fpath.unlink()

    def test_model_change_entry(self, mock_anonymizer):
        """model_change entries should update the session model."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            {"type": "model_change", "timestamp": "2026-02-20T10:00:30.000Z",
             "provider": "anthropic", "modelId": "claude-opus-4-20250514"},
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi", model=None),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result["model"] == "anthropic/claude-opus-4-20250514"
        fpath.unlink()

    def test_cache_read_tokens(self, mock_anonymizer):
        """cacheRead should be added to input_tokens."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Do something"),
            _make_openclaw_assistant_message(
                "Done", usage={"input": 100, "output": 50, "cacheRead": 200}
            ),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result["stats"]["input_tokens"] == 300  # 100 + 200
        assert result["stats"]["output_tokens"] == 50
        fpath.unlink()


class TestDiscoverOpenclawProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "no-cursor.vscdb")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

    def test_discover_openclaw_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Write two sessions for the same cwd
        for i, sid in enumerate(["sess-1", "sess-2"]):
            lines = [
                _make_openclaw_session_header(session_id=sid, cwd="/Users/alice/projects/myapp"),
                _make_openclaw_user_message(f"Message {i}"),
                _make_openclaw_assistant_message(f"Reply {i}"),
            ]
            (sessions_dir / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(l) for l in lines) + "\n"
            )

        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "openclaw"
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "openclaw:myapp"

    def test_parse_openclaw_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)

        lines = [
            _make_openclaw_session_header(session_id="sess-1", cwd="/Users/alice/projects/myapp"),
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi!", usage={"input": 10, "output": 5}),
        ]
        (sessions_dir / "sess-1.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n"
        )

        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        sessions = parse_project_sessions(
            "/Users/alice/projects/myapp", mock_anonymizer, source="openclaw"
        )
        assert len(sessions) == 1
        assert sessions[0]["source"] == "openclaw"
        assert sessions[0]["project"] == "openclaw:myapp"
        assert sessions[0]["messages"][0]["content"] == "Hello"

    def test_multiple_agents_same_cwd(self, tmp_path, monkeypatch, mock_anonymizer):
        """Sessions from different agents but same cwd should be grouped."""
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        for agent_name, sid in [("agent-1", "s1"), ("agent-2", "s2")]:
            sessions_dir = agents_dir / agent_name / "sessions"
            sessions_dir.mkdir(parents=True)
            lines = [
                _make_openclaw_session_header(session_id=sid, cwd="/Users/alice/projects/myapp"),
                _make_openclaw_user_message(f"From {agent_name}"),
                _make_openclaw_assistant_message(f"Reply from {agent_name}"),
            ]
            (sessions_dir / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(l) for l in lines) + "\n"
            )

        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2


class TestDiscoverCustomProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "no-cursor.vscdb")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

    def _make_valid_session(self, session_id="s1", model="gpt-4", content="hello"):
        return json.dumps({
            "session_id": session_id,
            "model": model,
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "hi there"},
            ],
            "stats": {"user_messages": 1, "assistant_messages": 1, "tool_uses": 0,
                       "input_tokens": 10, "output_tokens": 5},
        })

    def test_discover_custom_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "my-project"
        proj.mkdir(parents=True)
        (proj / "sessions.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2") + "\n"
        )
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "custom:my-project"
        assert projects[0]["session_count"] == 2
        assert projects[0]["source"] == "custom"

    def test_discover_skips_empty_dir(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        custom_dir = tmp_path / "custom"
        (custom_dir / "empty-project").mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        projects = discover_projects()
        assert len(projects) == 0

    def test_discover_missing_dir(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "nonexistent")
        projects = discover_projects()
        assert len(projects) == 0

    def test_parse_valid_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        (proj / "data.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2", model="o1") + "\n"
        )
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        assert sessions[0]["session_id"] == "s1"
        assert sessions[1]["model"] == "o1"
        assert sessions[0]["project"] == "custom:test-proj"
        assert sessions[0]["source"] == "custom"

    def test_parse_skips_missing_fields(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        no_model = json.dumps({"session_id": "s2", "messages": []})
        no_messages = json.dumps({"session_id": "s3", "model": "m"})
        no_session_id = json.dumps({"model": "m", "messages": []})
        (proj / "data.jsonl").write_text(
            "\n".join([valid, no_model, no_messages, no_session_id]) + "\n"
        )
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    def test_parse_skips_invalid_json(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        (proj / "data.jsonl").write_text(valid + "\n" + "not-json\n")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1

    def test_parse_multiple_files(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_text(self._make_valid_session("s1") + "\n")
        (proj / "b.jsonl").write_text(self._make_valid_session("s2") + "\n")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"s1", "s2"}

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("nope", mock_anonymizer, source="custom")
        assert sessions == []


def _write_cursor_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def _insert_cursor_conversation(conn, composer_id, bubbles):
    headers = [{"bubbleId": b["id"], "type": b["type"]} for b in bubbles]
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES(?, ?)",
        (f"composerData:{composer_id}", json.dumps({"fullConversationHeadersOnly": headers})),
    )
    for b in bubbles:
        data = dict(b)
        data.pop("id")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES(?, ?)",
            (f"bubbleId:{composer_id}:{b['id']}", json.dumps(data)),
        )


class TestCursorDiscoverProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw")
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

    def test_discover_cursor_projects(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-1", [
            {"id": "b1", "type": 1, "text": "Hello", "createdAt": 1706000000000,
             "workspaceUris": ["file:///Users/testuser/work/repo"]},
            {"id": "b2", "type": 2, "text": "Hi there!", "createdAt": 1706000001000},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "cursor"
        assert projects[0]["display_name"] == "cursor:repo"
        assert projects[0]["session_count"] == 1

    def test_discover_groups_by_workspace(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        for cid, uri in [("c1", "file:///Users/alice/proj-a"), ("c2", "file:///Users/alice/proj-a"), ("c3", "file:///Users/alice/proj-b")]:
            _insert_cursor_conversation(conn, cid, [
                {"id": "b1", "type": 1, "text": "msg", "createdAt": 1706000000000, "workspaceUris": [uri]},
                {"id": "b2", "type": 2, "text": "reply", "createdAt": 1706000001000},
            ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        projects = discover_projects()
        assert len(projects) == 2
        names = {p["display_name"] for p in projects}
        assert names == {"cursor:proj-a", "cursor:proj-b"}
        counts = {p["display_name"]: p["session_count"] for p in projects}
        assert counts["cursor:proj-a"] == 2
        assert counts["cursor:proj-b"] == 1

    def test_discover_no_db(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", tmp_path / "nonexistent.vscdb")
        projects = discover_projects()
        assert projects == []

    def test_discover_skips_single_bubble_conversations(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES(?, ?)",
            ("composerData:lonely", json.dumps({"fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]})),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        projects = discover_projects()
        assert projects == []


class TestCursorParseSessions:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dataclaw.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("dataclaw.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("dataclaw.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw")
        monkeypatch.setattr("dataclaw.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("dataclaw.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi")
        monkeypatch.setattr("dataclaw.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("dataclaw.parsers.cursor._PROJECT_INDEX", {})

    def test_basic_conversation(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-1", [
            {"id": "b1", "type": 1, "text": "Fix the bug", "createdAt": 1706000000000,
             "workspaceUris": [f"file://{cwd}"]},
            {"id": "b2", "type": 2, "text": "I'll fix it now.", "createdAt": 1706000001000,
             "modelInfo": {"modelName": "claude-sonnet-4-20250514"},
             "tokenCount": {"inputTokens": 100, "outputTokens": 30}},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == "conv-1"
        assert s["source"] == "cursor"
        assert s["project"] == "cursor:myapp"
        assert s["model"] == "claude-sonnet-4-20250514"
        assert len(s["messages"]) == 2
        assert s["messages"][0]["role"] == "user"
        assert "Fix the bug" in s["messages"][0]["content"]
        assert s["messages"][1]["role"] == "assistant"
        assert "fix it" in s["messages"][1]["content"]
        assert s["stats"]["user_messages"] == 1
        assert s["stats"]["assistant_messages"] == 1
        assert s["stats"]["input_tokens"] == 100
        assert s["stats"]["output_tokens"] == 30

    def test_tool_call(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-2", [
            {"id": "b1", "type": 1, "text": "Read the file", "createdAt": 1706000000000,
             "workspaceUris": [f"file://{cwd}"]},
            {"id": "b2", "type": 2, "text": "", "createdAt": 1706000001000,
             "toolFormerData": {
                 "name": "Read",
                 "params": json.dumps({"file_path": "/tmp/test.py"}),
                 "result": json.dumps("print('hello')"),
                 "status": "completed",
             },
             "modelInfo": {"modelName": "claude-sonnet-4-20250514"},
             "tokenCount": {"inputTokens": 50, "outputTokens": 10}},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        s = sessions[0]
        assert len(s["messages"]) == 2
        tool_msg = s["messages"][1]
        assert tool_msg["role"] == "assistant"
        assert len(tool_msg["tool_uses"]) == 1
        tu = tool_msg["tool_uses"][0]
        assert tu["tool"] == "Read"
        assert tu["status"] == "completed"
        assert "hello" in tu["output"]["text"]
        assert s["stats"]["tool_uses"] == 1

    def test_mcp_tool_prefix_stripped(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-3", [
            {"id": "b1", "type": 1, "text": "search", "createdAt": 1706000000000,
             "workspaceUris": [f"file://{cwd}"]},
            {"id": "b2", "type": 2, "text": "", "createdAt": 1706000001000,
             "toolFormerData": {
                 "name": "mcp_server_toolname",
                 "params": "{}",
                 "result": json.dumps("ok"),
                 "status": "completed",
             }},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        tu = sessions[0]["messages"][1]["tool_uses"][0]
        assert tu["tool"] == "toolname"

    def test_thinking_included(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-4", [
            {"id": "b1", "type": 1, "text": "Explain X", "createdAt": 1706000000000,
             "workspaceUris": [f"file://{cwd}"]},
            {"id": "b2", "type": 2, "text": "Here's the answer.", "createdAt": 1706000001000,
             "thinking": {"text": "Let me reason about X..."},
             "modelInfo": {"modelName": "claude-sonnet-4-20250514"}},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor", include_thinking=True)
        msg = sessions[0]["messages"][1]
        assert "thinking" in msg
        assert "reason about X" in msg["thinking"]

        sessions_no = parse_project_sessions(cwd, mock_anonymizer, source="cursor", include_thinking=False)
        msg_no = sessions_no[0]["messages"][1]
        assert "thinking" not in msg_no

    def test_unknown_workspace_grouped(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        _insert_cursor_conversation(conn, "conv-5", [
            {"id": "b1", "type": 1, "text": "Hello", "createdAt": 1706000000000},
            {"id": "b2", "type": 2, "text": "Hi", "createdAt": 1706000001000},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions("<unknown-cwd>", mock_anonymizer, source="cursor")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "cursor:unknown"

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions("/no/such/path", mock_anonymizer, source="cursor")
        assert sessions == []

    def test_nested_json_params_unwrapped(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/testuser/work/myapp"
        db_path = tmp_path / "state.vscdb"
        conn = _write_cursor_db(db_path)
        inner_params = {"file_path": "/tmp/foo.py"}
        wrapped_params = {"tools": [{"parameters": json.dumps(inner_params)}]}
        _insert_cursor_conversation(conn, "conv-6", [
            {"id": "b1", "type": 1, "text": "read", "createdAt": 1706000000000,
             "workspaceUris": [f"file://{cwd}"]},
            {"id": "b2", "type": 2, "text": "", "createdAt": 1706000001000,
             "toolFormerData": {
                 "name": "Read",
                 "params": json.dumps(wrapped_params),
                 "result": json.dumps("contents"),
                 "status": "completed",
             }},
        ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.cursor.CURSOR_DB", db_path)
        monkeypatch.setattr("dataclaw.parser.CURSOR_DB", db_path)
        sessions = parse_project_sessions(cwd, mock_anonymizer, source="cursor")
        tu = sessions[0]["messages"][1]["tool_uses"][0]
        assert "file_path" in tu["input"]
