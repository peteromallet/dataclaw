"""Tests for Codex parser behavior."""

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.parsers.codex import build_tool_result_map, parse_session_file
from tests.parser_helpers import disable_other_providers


class TestDiscoverCodexProjects:
    def test_discover_codex_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
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
            )
            + "\n"
        )

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")
        monkeypatch.setattr("dataclaw.parsers.codex._PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "codex"
        assert projects[0]["display_name"] == "codex:myrepo"

    def test_parse_codex_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
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

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        sessions = parse_project_sessions(
            "/Users/testuser/Documents/myrepo",
            mock_anonymizer,
            source="codex",
        )
        assert len(sessions) == 1
        assert sessions[0]["project"] == "codex:myrepo"
        assert sessions[0]["model"] == "gpt-5.3-codex"
        assert sessions[0]["stats"]["input_tokens"] == 120
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "exec_command"

    def test_codex_thinking_not_duplicated(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
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
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        from dataclaw.anonymizer import Anonymizer

        result = parse_session_file(
            session_file,
            Anonymizer(),
            include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )
        assert result is not None
        assistant_messages = [message for message in result["messages"] if message["role"] == "assistant"]
        assert len(assistant_messages) == 1
        paragraphs = [
            paragraph.strip() for paragraph in assistant_messages[0]["thinking"].split("\n\n") if paragraph.strip()
        ]
        assert paragraphs == ["Planning fix", "Reading code"]

    def test_codex_user_image_input_preserved_in_content_parts(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
        codex_sessions = tmp_path / "codex-sessions" / "2026" / "04" / "02"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-image.jsonl"
        lines = [
            {
                "timestamp": "2026-04-02T00:26:54.204Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-image",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.207Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.336Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<image name=[Image #1]>"},
                        {"type": "input_image", "image_url": "data:image/png;base64,QUJDRA=="},
                    ],
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.339Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Can you read the image [Image #1] ?",
                    "images": [],
                    "local_images": ["tmp/image.png"],
                    "text_elements": [{"placeholder": "[Image #1]"}],
                },
            },
            {
                "timestamp": "2026-04-02T00:27:08.731Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Yes."},
            },
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = parse_session_file(
            session_file,
            mock_anonymizer,
            include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )

        assert result is not None
        user_message = result["messages"][0]
        assert user_message["role"] == "user"
        assert user_message["content"] == "Can you read the image [Image #1] ?"
        assert user_message["content_parts"] == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "QUJDRA==",
                },
            }
        ]

    def test_codex_user_local_image_fallback(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
        codex_sessions = tmp_path / "codex-sessions" / "2026" / "04" / "02"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-local-image.jsonl"
        lines = [
            {
                "timestamp": "2026-04-02T00:26:54.204Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-local-image",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.207Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.339Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Please inspect this image.",
                    "images": [],
                    "local_images": ["tmp/image.png"],
                    "text_elements": [],
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = parse_session_file(
            session_file,
            mock_anonymizer,
            include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )

        assert result is not None
        user_message = result["messages"][0]
        assert user_message["content"] == "Please inspect this image."
        assert user_message["content_parts"][0]["type"] == "image"
        assert user_message["content_parts"][0]["source"]["type"] == "url"
        assert "testuser" not in user_message["content_parts"][0]["source"]["url"]
        assert (
            user_message["content_parts"][0]["source"]["url"]
            .replace("\\", "/")
            .endswith("/Documents/myrepo/tmp/image.png")
        )

    def test_codex_image_only_response_item_flushes_user_message(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
        codex_sessions = tmp_path / "codex-sessions" / "2026" / "04" / "02"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-image-only.jsonl"
        lines = [
            {
                "timestamp": "2026-04-02T00:26:54.204Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-image-only",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.207Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-04-02T00:26:54.336Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "data:image/png;base64," + ("A" * 5000)},
                    ],
                },
            },
            {
                "timestamp": "2026-04-02T00:27:08.731Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Yes."},
            },
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = parse_session_file(
            session_file,
            mock_anonymizer,
            include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )

        assert result is not None
        user_message = result["messages"][0]
        assert user_message["role"] == "user"
        assert "content" not in user_message
        assert user_message["content_parts"] == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 5000,
                },
            }
        ]


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
        result = build_tool_result_map(entries)
        assert "call-1" in result
        assert result["call-1"]["status"] == "success"
        assert result["call-1"]["output"]["exit_code"] == 0
        assert result["call-1"]["output"]["wall_time"] == "1 seconds"
        assert "hello world" in result["call-1"]["output"]["output"]

    def test_custom_tool_call_output(self, mock_anonymizer):
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-2",
                    "output": json.dumps(
                        {
                            "output": "Successfully applied patch",
                            "metadata": {"exit_code": 0, "duration_seconds": 0.5},
                        }
                    ),
                },
            }
        ]
        result = build_tool_result_map(entries)
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
        result = build_tool_result_map(entries)
        assert "call-3" not in result

    def test_output_attached_end_to_end(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"codex"})
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
                    "arguments": json.dumps({"command": "ls", "workdir": "/home/user/repo"}),
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
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("dataclaw.parsers.codex.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = parse_session_file(
            session_file,
            mock_anonymizer,
            include_thinking=True,
            target_cwd="/home/user/repo",
        )
        assert result is not None
        assistant_messages = [message for message in result["messages"] if message["role"] == "assistant"]
        assert len(assistant_messages) == 1
        tool_use = assistant_messages[0]["tool_uses"][0]
        assert tool_use["tool"] == "shell_command"
        assert tool_use["status"] == "success"
        assert tool_use["output"]["exit_code"] == 0
        assert "foo.py" in tool_use["output"]["output"]
