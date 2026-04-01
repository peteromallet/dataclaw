"""Tests for shared parser helpers."""

from dataclaw.parsers.common import (
    normalize_timestamp as _normalize_timestamp,
    parse_tool_input as _parse_tool_input,
)


class TestNormalizeTimestamp:
    def test_none(self):
        assert _normalize_timestamp(None) is None

    def test_string_passthrough(self):
        ts = "2025-01-15T10:00:00+00:00"
        assert _normalize_timestamp(ts) == ts

    def test_int_ms_to_iso(self):
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


class TestParseToolInput:
    def test_read_tool(self, mock_anonymizer):
        result = _parse_tool_input("Read", {"file_path": "/tmp/test.py"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "test.py" in result["file_path"]

    def test_write_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Write",
            {"file_path": "/tmp/test.py", "content": "abc"},
            mock_anonymizer,
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
            "Grep",
            {"pattern": "TODO", "path": "/tmp"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "pattern" in result
        assert "path" in result

    def test_glob_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Glob",
            {"pattern": "*.py", "path": "/tmp"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["pattern"] == "*.py"

    def test_task_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Task",
            {"prompt": "Search for bugs"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "Search for bugs" in result["prompt"]

    def test_websearch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebSearch",
            {"query": "python async"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["query"] == "python async"

    def test_webfetch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebFetch",
            {"url": "https://example.com"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com"

    def test_edit_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Edit",
            {"file_path": "/tmp/test.py"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result

    def test_exec_command_tool(self, mock_anonymizer):
        result = _parse_tool_input("exec_command", {"cmd": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["cmd"] == "ls -la"

    def test_shell_command_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "shell_command",
            {"command": "ls", "workdir": "/tmp"},
            mock_anonymizer,
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
