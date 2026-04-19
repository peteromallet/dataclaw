"""Tests for shared parser helpers."""

from dataclaw import _json as json
from dataclaw.parsers.common import load_json_field, normalize_timestamp, parse_tool_input
from dataclaw.secrets import REDACTED


class TestNormalizeTimestamp:
    def test_none(self):
        assert normalize_timestamp(None) is None

    def test_string_passthrough(self):
        ts = "2025-01-15T10:00:00+00:00"
        assert normalize_timestamp(ts) == ts

    def test_int_ms_to_iso(self):
        result = normalize_timestamp(1706000000000)
        assert result is not None
        assert "2024" in result
        assert "T" in result

    def test_float_ms_to_iso(self):
        result = normalize_timestamp(1706000000000.0)
        assert result is not None
        assert "T" in result

    def test_other_type_returns_none(self):
        assert normalize_timestamp([1, 2, 3]) is None
        assert normalize_timestamp({"ts": 123}) is None


class TestParseToolInput:
    def test_read_tool(self, mock_anonymizer):
        result = parse_tool_input("Read", {"file_path": "/tmp/test.py"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "test.py" in result["file_path"]

    def test_write_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "Write",
            {"file_path": "/tmp/test.py", "content": "abc"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "content" in result

    def test_bash_tool(self, mock_anonymizer):
        result = parse_tool_input("Bash", {"command": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["command"] == "ls -la"

    def test_grep_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "Grep",
            {"pattern": "TODO", "path": "/tmp"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "pattern" in result
        assert "path" in result

    def test_glob_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "Glob",
            {"pattern": "*.py", "path": "/tmp"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["pattern"] == "*.py"

    def test_task_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "Task",
            {"prompt": "Search for bugs"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "Search for bugs" in result["prompt"]

    def test_websearch_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "WebSearch",
            {"query": "python async"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["query"] == "python async"

    def test_webfetch_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "WebFetch",
            {"url": "https://example.com"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com"

    def test_edit_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "Edit",
            {"file_path": "/tmp/test.py"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result

    def test_exec_command_tool(self, mock_anonymizer):
        result = parse_tool_input("exec_command", {"cmd": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["cmd"] == "ls -la"

    def test_shell_command_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "shell_command",
            {"command": "ls", "workdir": "/tmp"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["command"] == "ls"
        assert "workdir" in result

    def test_command_field_is_not_pre_redacted(self, mock_anonymizer):
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
        result = parse_tool_input("Bash", {"command": f"export ANTHROPIC_API_KEY={secret}"}, mock_anonymizer)
        assert secret in result["command"]
        assert REDACTED not in result["command"]

    def test_update_plan_tool(self, mock_anonymizer):
        result = parse_tool_input(
            "update_plan",
            {"explanation": "New plan", "plan": [{"step": "do it", "status": "pending"}]},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "explanation" in result
        assert "plan" in result

    def test_unknown_tool(self, mock_anonymizer):
        result = parse_tool_input("CustomTool", {"foo": "bar"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_none_tool_name(self, mock_anonymizer):
        result = parse_tool_input(None, {"data": "value"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_non_dict_input(self, mock_anonymizer):
        result = parse_tool_input("Read", "just a string", mock_anonymizer)
        assert isinstance(result, dict)
        assert "raw" in result


class TestLoadJsonField:
    def test_surrogate_escapes_are_sanitized_for_export(self):
        value = '{"output":"prefix ' + "\\udcbf" + " middle " + "\\ud83d" + '","nested":["' + "\\udce1" + '"]}'

        result = load_json_field(value)

        assert result == {
            "output": r"prefix \xbf middle \ud83d",
            "nested": [r"\xe1"],
        }
        assert json.dumps(result)
