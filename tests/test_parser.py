"""Tests for dataclaw.parser — JSONL parsing and project discovery."""

import json

import pytest

from dataclaw.parser import (
    _build_project_name,
    _extract_assistant_content,
    _extract_user_content,
    _find_subagent_only_sessions,
    _normalize_timestamp,
    _parse_session_file,
    _parse_subagent_session,
    _process_entry,
    _summarize_tool_input,
    discover_projects,
    parse_project_sessions,
    _parse_codex_session_file,
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
        monkeypatch.setattr("dataclaw.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("dataclaw.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("dataclaw.parser._CODEX_PROJECT_INDEX", {})

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
