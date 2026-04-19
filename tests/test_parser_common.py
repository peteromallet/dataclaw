"""Tests for shared parser helpers."""

from dataclaw import _json as json
from dataclaw.parsers.common import load_json_field, make_session_result, normalize_timestamp, parse_tool_input
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
        result = parse_tool_input({"file_path": "/tmp/test.py"})
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "test.py" in result["file_path"]

    def test_write_tool(self, mock_anonymizer):
        result = parse_tool_input({"file_path": "/tmp/test.py", "content": "abc"})
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "content" in result

    def test_bash_tool(self, mock_anonymizer):
        result = parse_tool_input({"command": "ls -la"})
        assert isinstance(result, dict)
        assert result["command"] == "ls -la"

    def test_grep_tool(self, mock_anonymizer):
        result = parse_tool_input({"pattern": "TODO", "path": "/tmp"})
        assert isinstance(result, dict)
        assert "pattern" in result
        assert "path" in result

    def test_glob_tool(self, mock_anonymizer):
        result = parse_tool_input({"pattern": "*.py", "path": "/tmp"})
        assert isinstance(result, dict)
        assert result["pattern"] == "*.py"

    def test_task_tool(self, mock_anonymizer):
        result = parse_tool_input({"prompt": "Search for bugs"})
        assert isinstance(result, dict)
        assert "Search for bugs" in result["prompt"]

    def test_websearch_tool(self, mock_anonymizer):
        result = parse_tool_input({"query": "python async"})
        assert isinstance(result, dict)
        assert result["query"] == "python async"

    def test_webfetch_tool(self, mock_anonymizer):
        result = parse_tool_input({"url": "https://example.com"})
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com"

    def test_edit_tool(self, mock_anonymizer):
        result = parse_tool_input({"file_path": "/tmp/test.py"})
        assert isinstance(result, dict)
        assert "file_path" in result

    def test_exec_command_tool(self, mock_anonymizer):
        result = parse_tool_input({"cmd": "ls -la"})
        assert isinstance(result, dict)
        assert result["cmd"] == "ls -la"

    def test_shell_command_tool(self, mock_anonymizer):
        result = parse_tool_input({"command": "ls", "workdir": "/tmp"})
        assert isinstance(result, dict)
        assert result["command"] == "ls"
        assert "workdir" in result

    def test_command_field_is_not_pre_redacted(self, mock_anonymizer):
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
        result = parse_tool_input({"command": f"export ANTHROPIC_API_KEY={secret}"})
        assert secret in result["command"]
        assert REDACTED not in result["command"]

    def test_update_plan_tool(self, mock_anonymizer):
        result = parse_tool_input({"explanation": "New plan", "plan": [{"step": "do it", "status": "pending"}]})
        assert isinstance(result, dict)
        assert "explanation" in result
        assert "plan" in result

    def test_unknown_tool(self, mock_anonymizer):
        result = parse_tool_input({"foo": "bar"})
        assert isinstance(result, dict)

    def test_none_tool_name(self, mock_anonymizer):
        result = parse_tool_input({"data": "value"})
        assert isinstance(result, dict)

    def test_non_dict_input(self, mock_anonymizer):
        result = parse_tool_input("just a string")
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


class TestMakeSessionResult:
    def test_centralized_anonymization_skips_base64_data(self, mock_anonymizer):
        session = make_session_result(
            {
                "session_id": "s1",
                "model": "m",
                "git_branch": None,
                "start_time": None,
                "end_time": None,
            },
            [
                {
                    "role": "user",
                    "content": "hello testuser at /Users/testuser/project",
                    "content_parts": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "text/plain",
                                "data": "testuserbase64payload",
                            },
                        }
                    ],
                }
            ],
            {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0, "input_tokens": 0, "output_tokens": 0},
            anonymizer=mock_anonymizer,
        )

        assert session is not None
        assert "testuser" not in session["messages"][0]["content"]
        assert session["messages"][0]["content_parts"][0]["source"]["data"] == "testuserbase64payload"
