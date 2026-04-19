"""Tests for Claude parser behavior."""

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.parsers.claude import (
    build_project_name,
    build_tool_result_map,
    extract_assistant_content,
    extract_user_content,
    find_subagent_only_sessions,
    find_subagent_sessions,
    parse_session_file,
    parse_subagent_session,
    process_entry,
)
from tests.parser_helpers import disable_other_providers, make_subagent_entry


class TestBuildProjectName:
    def test_documents_prefix(self):
        assert build_project_name("-Users-alice-Documents-myproject") == "myproject"

    def test_home_prefix(self):
        assert build_project_name("-home-bob-project") == "project"

    def test_standalone(self):
        assert build_project_name("standalone") == "standalone"

    def test_deep_documents_path(self):
        result = build_project_name("-Users-alice-Documents-work-repo")
        assert result == "work-repo"

    def test_downloads_prefix(self):
        assert build_project_name("-Users-alice-Downloads-thing") == "thing"

    def test_desktop_prefix(self):
        assert build_project_name("-Users-alice-Desktop-stuff") == "stuff"

    def test_bare_home(self):
        assert build_project_name("-Users-alice") == "~home"

    def test_users_common_dir_only(self):
        assert build_project_name("-Users-alice-Documents") == "~Documents"

    def test_home_bare(self):
        assert build_project_name("-home-bob") == "~home"

    def test_windows_paths(self):
        assert build_project_name("C-Users-bob-Documents-proj") == "proj"
        assert build_project_name("D-Users-alice-code-myapp") == "code-myapp"
        assert build_project_name("E-Users-charlie") == "~home"

    def test_non_common_dir(self):
        result = build_project_name("-Users-alice-code-myproject")
        assert result == "code-myproject"

    def test_empty_string(self):
        result = build_project_name("")
        assert result == ""

    def test_linux_deep_path(self):
        assert build_project_name("-home-bob-projects-app") == "projects-app"

    def test_hyphens_preserved_in_project_name(self):
        result = build_project_name("-Users-alice-Documents-my-cool-project")
        assert result == "my-cool-project"


class TestExtractUserContent:
    def test_string_content(self, mock_anonymizer):
        entry = {"message": {"content": "Fix the bug"}}
        result = extract_user_content(entry)
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
        result = extract_user_content(entry)
        assert "Hello" in result
        assert "World" in result

    def test_empty_content(self, mock_anonymizer):
        entry = {"message": {"content": ""}}
        assert extract_user_content(entry) is None

    def test_whitespace_content(self, mock_anonymizer):
        entry = {"message": {"content": "   \n  "}}
        assert extract_user_content(entry) is None

    def test_missing_message(self, mock_anonymizer):
        entry = {}
        assert extract_user_content(entry) is None


class TestExtractAssistantContent:
    def test_text_blocks(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Part 1"},
                    {"type": "text", "text": "Part 2"},
                ]
            }
        }
        result = extract_assistant_content(entry, True)
        assert result is not None
        assert result["role"] == "assistant"
        assert "Part 1" in result["content"]
        assert "Part 2" in result["content"]

    def test_thinking_included(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Need to inspect files."},
                    {"type": "text", "text": "I found it."},
                ]
            }
        }
        result = extract_assistant_content(entry, True)
        assert result is not None
        assert "thinking" in result
        assert "Need to inspect files." in result["thinking"]

    def test_thinking_excluded(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Internal."},
                    {"type": "text", "text": "Visible."},
                ]
            }
        }
        result = extract_assistant_content(entry, False)
        assert result is not None
        assert "thinking" not in result
        assert result["content"] == "Visible."

    def test_tool_use_parsed(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    }
                ]
            }
        }
        result = extract_assistant_content(entry, True)
        assert result is not None
        assert len(result["tool_uses"]) == 1
        assert result["tool_uses"][0]["tool"] == "Read"

    def test_tool_use_with_result_map(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ]
            }
        }
        result = extract_assistant_content(
            entry, True, {"tool-1": {"output": {"text": "file.txt"}, "status": "success"}}
        )
        assert result is not None
        tool_use = result["tool_uses"][0]
        assert tool_use["status"] == "success"
        assert tool_use["output"]["text"] == "file.txt"

    def test_empty_blocks_returns_none(self, mock_anonymizer):
        entry = {"message": {"content": []}}
        assert extract_assistant_content(entry, True) is None

    def test_non_list_content_returns_none(self, mock_anonymizer):
        entry = {"message": {"content": "not-a-list"}}
        assert extract_assistant_content(entry, True) is None

    def test_ignores_non_dict_blocks(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    "not a dict",
                    {"type": "text", "text": "Valid."},
                ]
            }
        }
        result = extract_assistant_content(entry, True)
        assert result is not None
        assert result["content"] == "Valid."


class TestProcessEntry:
    def _run(self, entry, anonymizer, include_thinking=True):
        messages = []
        metadata = {
            "session_id": "test",
            "git_branch": None,
            "claude_version": None,
            "model": None,
            "start_time": None,
            "end_time": None,
        }
        stats = {
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_uses": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        process_entry(entry, messages, metadata, stats, include_thinking)
        return messages, metadata, stats

    def test_user_entry(self, mock_anonymizer, sample_user_entry):
        messages, metadata, stats = self._run(sample_user_entry, mock_anonymizer)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert stats["user_messages"] == 1
        assert metadata["git_branch"] == "main"

    def test_assistant_entry(self, mock_anonymizer, sample_assistant_entry):
        messages, metadata, stats = self._run(sample_assistant_entry, mock_anonymizer)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert stats["assistant_messages"] == 1
        assert stats["input_tokens"] == 750
        assert stats["output_tokens"] > 0

    def test_unknown_type(self, mock_anonymizer):
        entry = {"type": "system", "message": {}}
        messages, _, _ = self._run(entry, mock_anonymizer)
        assert len(messages) == 0

    def test_metadata_extraction(self, mock_anonymizer, sample_user_entry):
        _, metadata, _ = self._run(sample_user_entry, mock_anonymizer)
        assert metadata["claude_version"] == "1.0.0"
        assert metadata["start_time"] is not None


class TestParseSessionFile:
    def test_valid_jsonl(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session.jsonl"
        entries = [
            {
                "type": "user",
                "timestamp": 1706000000000,
                "message": {"content": "Hello"},
                "cwd": "/tmp/proj",
            },
            {
                "type": "assistant",
                "timestamp": 1706000001000,
                "message": {
                    "model": "claude-sonnet-4-20250514",
                    "content": [{"type": "text", "text": "Hi there!"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        result = parse_session_file(session_file, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_malformed_lines_skipped(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/tmp"}\n'
            "not valid json\n"
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )
        result = parse_session_file(session_file, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2

    def test_empty_file(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text("")
        assert parse_session_file(session_file, mock_anonymizer) is None

    def test_oserror_returns_none(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "nonexistent.jsonl"
        assert parse_session_file(session_file, mock_anonymizer) is None

    def test_blank_lines_skipped(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            '\n\n{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/tmp"}\n\n'
        )
        result = parse_session_file(session_file, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 1


class TestDiscoverProjects:
    def test_with_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "-Users-alice-Documents-myapp"
        project_dir.mkdir(parents=True)
        session_file = project_dir / "abc-123.jsonl"
        session_file.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/tmp"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hey"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "myapp"
        assert projects[0]["session_count"] == 1

    def test_no_projects_dir(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", tmp_path / "nonexistent")
        assert discover_projects() == []

    def test_empty_project_dir(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        (projects_dir / "empty-project").mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        assert discover_projects() == []

    def test_parse_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "test-project"
        project_dir.mkdir(parents=True)
        session_file = project_dir / "session1.jsonl"
        session_file.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/tmp"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("test-project", mock_anonymizer)
        assert len(sessions) == 1
        assert sessions[0]["project"] == "test-project"

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", tmp_path / "projects")
        assert parse_project_sessions("nope", mock_anonymizer) == []


class TestFindSubagentOnlySessions:
    def test_finds_all_subagent_dirs(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "has-root.jsonl").write_text("{}\n")

        attached_subagent_dir = project_dir / "has-root" / "subagents"
        attached_subagent_dir.mkdir(parents=True)
        (attached_subagent_dir / "agent-a1.jsonl").write_text("{}\n")

        subagent_only_dir = project_dir / "subagent-only" / "subagents"
        subagent_only_dir.mkdir(parents=True)
        (subagent_only_dir / "agent-b1.jsonl").write_text("{}\n")

        result = find_subagent_sessions(project_dir)
        assert [entry.name for entry in result] == ["has-root", "subagent-only"]

    def test_finds_subagent_dirs_without_root_jsonl(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "has-root.jsonl").write_text("{}\n")
        subagent_dir = project_dir / "has-root" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a1.jsonl").write_text("{}\n")

        subagent_only_dir = project_dir / "subagent-only" / "subagents"
        subagent_only_dir.mkdir(parents=True)
        (subagent_only_dir / "agent-b1.jsonl").write_text("{}\n")

        result = find_subagent_only_sessions(project_dir)
        assert len(result) == 1
        assert result[0].name == "subagent-only"

    def test_ignores_dirs_without_subagents(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "tool-only" / "tool-results").mkdir(parents=True)
        assert find_subagent_only_sessions(project_dir) == []

    def test_ignores_empty_subagent_dirs(self, tmp_path):
        project_dir = tmp_path / "project"
        (project_dir / "empty-sa" / "subagents").mkdir(parents=True)
        assert find_subagent_only_sessions(project_dir) == []

    def test_returns_empty_for_no_dirs(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "session.jsonl").write_text("{}\n")
        assert find_subagent_only_sessions(project_dir) == []


class TestParseSubagentSession:
    def test_merges_multiple_files_sorted_by_timestamp(self, tmp_path, mock_anonymizer):
        session_dir = tmp_path / "abc-123"
        subagent_dir = session_dir / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a1.jsonl").write_text(
            json.dumps(
                make_subagent_entry(
                    "user",
                    "First message",
                    "2026-01-10T08:00:00Z",
                    cwd="/tmp/proj",
                    session_id="abc-123",
                )
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Third reply", "2026-01-10T08:02:00Z"))
            + "\n"
        )
        (subagent_dir / "agent-b2.jsonl").write_text(
            json.dumps(make_subagent_entry("assistant", "Second reply", "2026-01-10T08:01:00Z")) + "\n"
        )

        result = parse_subagent_session(session_dir, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "abc-123"
        assert len(result["messages"]) == 3
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "First message"
        assert result["messages"][1]["content"] == "Second reply"
        assert result["messages"][2]["content"] == "Third reply"
        assert result["model"] == "claude-opus-4-5-20251101"

    def test_returns_none_for_empty_subagents(self, tmp_path, mock_anonymizer):
        session_dir = tmp_path / "empty"
        (session_dir / "subagents").mkdir(parents=True)
        assert parse_subagent_session(session_dir, mock_anonymizer) is None

    def test_returns_none_for_no_subagent_dir(self, tmp_path, mock_anonymizer):
        session_dir = tmp_path / "no-sa"
        session_dir.mkdir()
        assert parse_subagent_session(session_dir, mock_anonymizer) is None

    def test_returns_none_when_no_messages(self, tmp_path, mock_anonymizer):
        session_dir = tmp_path / "no-msgs"
        subagent_dir = session_dir / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-x.jsonl").write_text(
            json.dumps({"type": "system", "timestamp": "2026-01-01T00:00:00Z"}) + "\n"
        )
        assert parse_subagent_session(session_dir, mock_anonymizer) is None

    def test_stats_aggregated(self, tmp_path, mock_anonymizer):
        session_dir = tmp_path / "stats-test"
        subagent_dir = session_dir / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "Hello", "2026-01-10T10:00:00Z", cwd="/tmp/p"))
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Hi", "2026-01-10T10:00:01Z"))
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Done", "2026-01-10T10:00:02Z"))
            + "\n"
        )

        result = parse_subagent_session(session_dir, mock_anonymizer)
        assert result is not None
        assert result["stats"]["user_messages"] == 1
        assert result["stats"]["assistant_messages"] == 2
        assert result["stats"]["input_tokens"] == 100
        assert result["stats"]["output_tokens"] == 40

    def test_suffixes_session_id_when_root_session_exists(self, tmp_path, mock_anonymizer):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "same-session.jsonl").write_text("{}\n")

        session_dir = project_dir / "same-session"
        subagent_dir = session_dir / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(
                make_subagent_entry("user", "Hello", "2026-01-10T10:00:00Z", cwd="/tmp/p", session_id="same-session")
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Hi", "2026-01-10T10:00:01Z"))
            + "\n"
        )

        result = parse_subagent_session(session_dir, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "same-session:subagents"


class TestDiscoverSubagentProjects:
    def test_discover_counts_attached_subagent_sessions(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "-Users-alice-Documents-research"
        project_dir.mkdir(parents=True)
        (project_dir / "same-session.jsonl").write_text(
            json.dumps(
                make_subagent_entry("user", "Root msg", "2026-01-01T00:00:00Z", cwd="/tmp", session_id="same-session")
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Root reply", "2026-01-01T00:00:01Z"))
            + "\n"
        )

        subagent_dir = project_dir / "same-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(
                make_subagent_entry("user", "SA msg", "2026-01-02T00:00:00Z", cwd="/tmp", session_id="same-session")
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "SA reply", "2026-01-02T00:00:01Z"))
            + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2

    def test_discover_includes_subagent_sessions(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "-Users-alice-Documents-research"
        project_dir.mkdir(parents=True)
        (project_dir / "root-session.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "Hi", "2026-01-01T00:00:00Z", cwd="/tmp")) + "\n"
        )

        subagent_dir = project_dir / "subagent-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "Build it", "2026-01-02T00:00:00Z", cwd="/tmp")) + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "research"

    def test_discover_subagent_only_project(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "subagent-project"
        project_dir.mkdir(parents=True)
        subagent_dir = project_dir / "session-uuid" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "Do work", "2026-01-01T00:00:00Z", cwd="/tmp")) + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 1

    def test_parse_includes_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "mixed-project"
        project_dir.mkdir(parents=True)
        (project_dir / "root.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "Root msg", "2026-01-01T00:00:00Z", cwd="/tmp"))
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Root reply", "2026-01-01T00:00:01Z"))
            + "\n"
        )

        subagent_dir = project_dir / "sa-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(make_subagent_entry("user", "SA msg", "2026-01-02T00:00:00Z", cwd="/tmp"))
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "SA reply", "2026-01-02T00:00:01Z"))
            + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("mixed-project", mock_anonymizer)
        assert len(sessions) == 2
        contents = {session["messages"][0]["content"] for session in sessions}
        assert "Root msg" in contents
        assert "SA msg" in contents

    def test_parse_includes_attached_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"claude"})
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "attached-project"
        project_dir.mkdir(parents=True)
        (project_dir / "same-session.jsonl").write_text(
            json.dumps(
                make_subagent_entry("user", "Root msg", "2026-01-01T00:00:00Z", cwd="/tmp", session_id="same-session")
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "Root reply", "2026-01-01T00:00:01Z"))
            + "\n"
        )

        subagent_dir = project_dir / "same-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-a.jsonl").write_text(
            json.dumps(
                make_subagent_entry("user", "SA msg", "2026-01-02T00:00:00Z", cwd="/tmp", session_id="same-session")
            )
            + "\n"
            + json.dumps(make_subagent_entry("assistant", "SA reply", "2026-01-02T00:00:01Z"))
            + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.claude.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("attached-project", mock_anonymizer)
        assert len(sessions) == 2
        assert {session["session_id"] for session in sessions} == {"same-session", "same-session:subagents"}
        contents = {session["messages"][0]["content"] for session in sessions}
        assert "Root msg" in contents
        assert "SA msg" in contents


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
        result = build_tool_result_map(entries)
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
        result = build_tool_result_map(entries)
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
        result = build_tool_result_map(entries)
        assert "Part one" in result["tu-3"]["output"]["text"]
        assert "Part two" in result["tu-3"]["output"]["text"]

    def test_empty_content_gives_empty_output(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-4", "content": ""}]},
            }
        ]
        result = build_tool_result_map(entries)
        assert result["tu-4"]["output"] == {}

    def test_structured_tool_result_keeps_extra_fields_without_dup_text(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-structured",
                            "content": "command output",
                            "is_error": False,
                        }
                    ]
                },
                "toolUseResult": {
                    "stdout": "command output",
                    "stderr": "warning text",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                },
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-structured"]["output"]
        assert output["text"] == "command output"
        assert "stdout" not in output["raw"]
        assert output["raw"]["stderr"] == "warning text"
        assert output["raw"]["interrupted"] is False
        assert output["raw"]["isImage"] is False

    def test_file_tool_result_omits_duplicate_file_content(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-file",
                            "content": "line one\nline two",
                        }
                    ]
                },
                "toolUseResult": {
                    "type": "text",
                    "file": {
                        "filePath": "/Users/testuser/Documents/myproject/out.txt",
                        "content": "line one\nline two",
                        "numLines": 2,
                        "startLine": 1,
                        "totalLines": 2,
                    },
                },
            }
        ]
        result = build_tool_result_map(entries)
        raw = result["tu-file"]["output"]["raw"]
        assert raw["type"] == "text"
        assert raw["file"]["numLines"] == 2
        assert "content" not in raw["file"]
        assert raw["file"]["filePath"] == "/Users/testuser/Documents/myproject/out.txt"

    def test_file_tool_result_omits_duplicate_content_with_line_prefixes(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-file-numbered",
                            "content": "     1→line one\n     2→line two",
                        }
                    ]
                },
                "toolUseResult": {
                    "type": "text",
                    "file": {
                        "filePath": "/Users/testuser/Documents/myproject/out.txt",
                        "content": "line one\nline two",
                        "numLines": 2,
                    },
                },
            }
        ]
        result = build_tool_result_map(entries)
        raw = result["tu-file-numbered"]["output"]["raw"]
        assert raw["type"] == "text"
        assert "content" not in raw["file"]

    def test_file_tool_result_omits_duplicate_content_when_output_wraps_it(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-file-wrapped",
                            "content": (
                                "     1→line one\n     2→line two\n\n<system-reminder>extra wrapper</system-reminder>"
                            ),
                        }
                    ]
                },
                "toolUseResult": {
                    "type": "text",
                    "file": {
                        "filePath": "/Users/testuser/Documents/myproject/out.txt",
                        "content": "line one\nline two",
                        "numLines": 2,
                    },
                },
            }
        ]
        result = build_tool_result_map(entries)
        raw = result["tu-file-wrapped"]["output"]["raw"]
        assert raw["type"] == "text"
        assert "content" not in raw["file"]

    def test_non_text_tool_result_blocks_preserved(self, mock_anonymizer):
        image_data = "A" * 5000
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-image",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "data": image_data},
                                }
                            ],
                        }
                    ]
                },
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-image"]["output"]
        assert "text" not in output
        assert output["raw"]["content"][0]["type"] == "image"
        assert output["raw"]["content"][0]["source"]["data"] == image_data

    def test_large_string_blob_content_preserved_verbatim_in_raw(self, mock_anonymizer):
        blob = "A" * 5000
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-blob",
                            "content": blob,
                        }
                    ]
                },
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-blob"]["output"]
        assert "text" not in output
        assert output["raw"]["content"] == blob

    def test_large_string_tool_use_result_preserved_verbatim_in_raw(self, mock_anonymizer):
        blob = "A" * 5000
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-blob-result",
                            "content": blob,
                        }
                    ]
                },
                "toolUseResult": blob,
                "sourceToolAssistantUUID": "assistant-blob",
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-blob-result"]["output"]
        assert "text" not in output
        assert output["raw"]["content"] == blob
        assert output["raw"]["sourceToolAssistantUUID"] == "assistant-blob"

    def test_long_ansi_terminal_output_is_preserved_as_text(self, mock_anonymizer):
        terminal_output = (
            "Exit code 1\n"
            + "\x1b[92mSuccessfully preprocessed all matching files.\x1b[0m\n"
            + ("Traceback line with context\n" * 250)
        )
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-ansi",
                            "content": terminal_output,
                            "is_error": True,
                        }
                    ]
                },
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-ansi"]["output"]
        assert output["text"].startswith("Exit code 1")
        assert "Successfully preprocessed" in output["text"]

    def test_edit_tool_result_preserves_raw_payload(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-edit",
                            "content": "The file was updated successfully.",
                        }
                    ]
                },
                "toolUseResult": {
                    "filePath": "/Users/testuser/Documents/myproject/app.py",
                    "oldString": "secret = 'abc'",
                    "newString": "secret = 'xyz'",
                    "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1}],
                },
                "sourceToolAssistantUUID": "assistant-123",
            }
        ]
        result = build_tool_result_map(entries)
        raw = result["tu-edit"]["output"]["raw"]
        assert raw["filePath"] == "/Users/testuser/Documents/myproject/app.py"
        assert "oldString" not in raw
        assert "newString" not in raw
        assert "structuredPatch" not in raw
        assert raw["sourceToolAssistantUUID"] == "assistant-123"

    def test_create_tool_result_drops_duplicate_created_file_content(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-create",
                            "content": "File created successfully at: /Users/testuser/Documents/myproject/out.txt",
                        }
                    ]
                },
                "toolUseResult": {
                    "type": "create",
                    "filePath": "/Users/testuser/Documents/myproject/out.txt",
                    "content": "full file contents",
                },
                "sourceToolAssistantUUID": "assistant-create",
            }
        ]
        result = build_tool_result_map(entries)
        output = result["tu-create"]["output"]
        assert output["text"].startswith("File created successfully at:")
        assert output["raw"]["type"] == "create"
        assert output["raw"]["filePath"] == "/Users/testuser/Documents/myproject/out.txt"
        assert "content" not in output["raw"]
        assert output["raw"]["sourceToolAssistantUUID"] == "assistant-create"

    def test_non_user_entries_ignored(self, mock_anonymizer):
        entries = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-5", "content": "ignored"}]},
            }
        ]
        result = build_tool_result_map(entries)
        assert "tu-5" not in result

    def test_tool_output_attached_in_session(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session.jsonl"
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
        session_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        result = parse_session_file(session_file, mock_anonymizer)
        assert result is not None
        tool_use = result["messages"][0]["tool_uses"][0]
        assert tool_use["tool"] == "Bash"
        assert tool_use["status"] == "success"
        assert "file1.py" in tool_use["output"]["text"]
