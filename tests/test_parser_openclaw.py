"""Tests for OpenClaw parser behavior."""

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.parsers.openclaw import parse_session_file
from tests.parser_helpers import (
    disable_other_providers,
    make_openclaw_assistant_message,
    make_openclaw_session_header,
    make_openclaw_tool_result,
    make_openclaw_user_message,
)


def _write_openclaw_session(tmp_path, name, lines):
    session_file = tmp_path / name
    session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return session_file


class TestParseOpenclawSessionFile:
    def test_basic_conversation(self, tmp_path, mock_anonymizer):
        session_file = _write_openclaw_session(
            tmp_path,
            "basic.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Hello"),
                make_openclaw_assistant_message("Hi there!", usage={"input": 50, "output": 20}),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer)
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

    def test_thinking_included(self, tmp_path, mock_anonymizer):
        session_file = _write_openclaw_session(
            tmp_path,
            "thinking.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Explain X"),
                make_openclaw_assistant_message("Here's the answer", thinking="Let me think about X..."),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer, include_thinking=True)
        assert "thinking" in result["messages"][1]
        assert "Let me think about X" in result["messages"][1]["thinking"]

        result_no_think = parse_session_file(session_file, mock_anonymizer, include_thinking=False)
        assert "thinking" not in result_no_think["messages"][1]

    def test_tool_calls_with_results(self, tmp_path, mock_anonymizer):
        tool_call = {
            "type": "toolCall",
            "id": "tc-1",
            "name": "read_file",
            "arguments": {"path": "/tmp/test.py"},
        }
        session_file = _write_openclaw_session(
            tmp_path,
            "tool.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Read the file"),
                make_openclaw_assistant_message("Let me read that", tool_calls=[tool_call]),
                make_openclaw_tool_result("tc-1", "print('hello')"),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer)
        assistant_message = result["messages"][1]
        assert len(assistant_message["tool_uses"]) == 1
        tool_use = assistant_message["tool_uses"][0]
        assert tool_use["tool"] == "read_file"
        assert tool_use["status"] == "success"
        assert "hello" in tool_use["output"]["text"]
        assert result["stats"]["tool_uses"] == 1

    def test_error_tool_result(self, tmp_path, mock_anonymizer):
        tool_call = {
            "type": "toolCall",
            "id": "tc-err",
            "name": "bash",
            "arguments": {"command": "rm /nope"},
        }
        session_file = _write_openclaw_session(
            tmp_path,
            "tool-error.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Delete it"),
                make_openclaw_assistant_message("Trying", tool_calls=[tool_call]),
                make_openclaw_tool_result("tc-err", "Permission denied", is_error=True),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer)
        assert result["messages"][1]["tool_uses"][0]["status"] == "error"

    def test_empty_file_returns_none(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "empty.jsonl"
        session_file.write_text("")
        assert parse_session_file(session_file, mock_anonymizer) is None

    def test_no_session_header_returns_none(self, tmp_path, mock_anonymizer):
        session_file = _write_openclaw_session(tmp_path, "no-header.jsonl", [make_openclaw_user_message("Hello")])
        assert parse_session_file(session_file, mock_anonymizer) is None

    def test_model_change_entry(self, tmp_path, mock_anonymizer):
        session_file = _write_openclaw_session(
            tmp_path,
            "model-change.jsonl",
            [
                make_openclaw_session_header(),
                {
                    "type": "model_change",
                    "timestamp": "2026-02-20T10:00:30.000Z",
                    "provider": "anthropic",
                    "modelId": "claude-opus-4-20250514",
                },
                make_openclaw_user_message("Hello"),
                make_openclaw_assistant_message("Hi", model=None),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer)
        assert result["model"] == "anthropic/claude-opus-4-20250514"

    def test_cache_read_tokens(self, tmp_path, mock_anonymizer):
        session_file = _write_openclaw_session(
            tmp_path,
            "cache.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Do something"),
                make_openclaw_assistant_message(
                    "Done",
                    usage={"input": 100, "output": 50, "cacheRead": 200},
                ),
            ],
        )

        result = parse_session_file(session_file, mock_anonymizer)
        assert result["stats"]["input_tokens"] == 300
        assert result["stats"]["output_tokens"] == 50

    def test_reads_session_file_once(self, tmp_path, monkeypatch, mock_anonymizer):
        session_file = _write_openclaw_session(
            tmp_path,
            "single-pass.jsonl",
            [
                make_openclaw_session_header(),
                make_openclaw_user_message("Hello"),
                make_openclaw_assistant_message("Hi there!"),
            ],
        )

        open_calls = 0
        real_open = open

        def counting_open(file, *args, **kwargs):
            nonlocal open_calls
            if str(file) == str(session_file):
                open_calls += 1
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", counting_open)

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        assert open_calls == 1


class TestDiscoverOpenclawProjects:
    def test_discover_openclaw_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"openclaw"})
        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)

        for index, session_id in enumerate(["sess-1", "sess-2"]):
            session_file = sessions_dir / f"{session_id}.jsonl"
            session_file.write_text(
                "\n".join(
                    json.dumps(line)
                    for line in [
                        make_openclaw_session_header(session_id=session_id, cwd="/Users/alice/projects/myapp"),
                        make_openclaw_user_message(f"Message {index}"),
                        make_openclaw_assistant_message(f"Reply {index}"),
                    ]
                )
                + "\n"
            )

        monkeypatch.setattr("dataclaw.parsers.openclaw.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "openclaw"
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "openclaw:myapp"

    def test_parse_openclaw_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"openclaw"})
        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)
        session_file = sessions_dir / "sess-1.jsonl"
        session_file.write_text(
            "\n".join(
                json.dumps(line)
                for line in [
                    make_openclaw_session_header(session_id="sess-1", cwd="/Users/alice/projects/myapp"),
                    make_openclaw_user_message("Hello"),
                    make_openclaw_assistant_message("Hi!", usage={"input": 10, "output": 5}),
                ]
            )
            + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.openclaw.OPENCLAW_AGENTS_DIR", agents_dir)
        sessions = parse_project_sessions("/Users/alice/projects/myapp", mock_anonymizer, source="openclaw")
        assert len(sessions) == 1
        assert sessions[0]["source"] == "openclaw"
        assert sessions[0]["project"] == "openclaw:myapp"
        assert sessions[0]["messages"][0]["content"] == "Hello"

    def test_multiple_agents_same_cwd(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"openclaw"})
        agents_dir = tmp_path / "openclaw-agents"
        for agent_name, session_id in [("agent-1", "s1"), ("agent-2", "s2")]:
            sessions_dir = agents_dir / agent_name / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / f"{session_id}.jsonl"
            session_file.write_text(
                "\n".join(
                    json.dumps(line)
                    for line in [
                        make_openclaw_session_header(session_id=session_id, cwd="/Users/alice/projects/myapp"),
                        make_openclaw_user_message(f"From {agent_name}"),
                        make_openclaw_assistant_message(f"Reply from {agent_name}"),
                    ]
                )
                + "\n"
            )

        monkeypatch.setattr("dataclaw.parsers.openclaw.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2
