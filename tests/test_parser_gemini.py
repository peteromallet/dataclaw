"""Tests for Gemini parser behavior."""

from dataclaw import _json as json
from dataclaw.parsers.gemini import (
    build_export_session_tasks,
    discover_projects,
    parse_project_sessions,
    parse_session_file,
)


class TestDiscoverGeminiProjects:
    def test_discovers_sessions_without_materializing_file_list(self, tmp_path, monkeypatch):
        gemini_dir = tmp_path / "tmp"
        chats_dir = gemini_dir / "project-hash" / "chats"
        chats_dir.mkdir(parents=True)
        (chats_dir / "session-1.json").write_text("{}", encoding="utf-8")
        (chats_dir / "session-2.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr("dataclaw.parsers.gemini.GEMINI_DIR", gemini_dir)

        projects = discover_projects(resolve_hash_fn=lambda _hash: "resolved-project")

        assert len(projects) == 1
        assert projects[0]["display_name"] == "gemini:resolved-project"
        assert projects[0]["session_count"] == 2

    def test_discovers_jsonl_sessions(self, tmp_path, monkeypatch):
        gemini_dir = tmp_path / "tmp"
        chats_dir = gemini_dir / "project-hash" / "chats"
        chats_dir.mkdir(parents=True)
        (chats_dir / "session-1.jsonl").write_text("{}\n", encoding="utf-8")

        monkeypatch.setattr("dataclaw.parsers.gemini.GEMINI_DIR", gemini_dir)

        projects = discover_projects(resolve_hash_fn=lambda _hash: "resolved-project")

        assert len(projects) == 1
        assert projects[0]["session_count"] == 1

    def test_parse_project_sessions_reads_jsonl_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        gemini_dir = tmp_path / "tmp"
        chats_dir = gemini_dir / "project-hash" / "chats"
        chats_dir.mkdir(parents=True)
        session_file = chats_dir / "session-2026-05-02T02-38-5c45ceef.jsonl"
        session_file.write_text(
            "\n".join(
                json.dumps(line)
                for line in [
                    {
                        "sessionId": "gemini-jsonl-session",
                        "startTime": "2026-05-02T02:38:45.721Z",
                        "lastUpdated": "2026-05-02T02:38:45.721Z",
                    },
                    {
                        "id": "message-1",
                        "timestamp": "2026-05-02T02:39:06.404Z",
                        "type": "user",
                        "content": [{"text": "hello"}],
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("dataclaw.parsers.gemini.GEMINI_DIR", gemini_dir)

        sessions = list(parse_project_sessions("project-hash", mock_anonymizer))
        tasks = build_export_session_tasks(0, {"dir_name": "project-hash", "display_name": "gemini:resolved-project"})

        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "gemini-jsonl-session"
        assert len(tasks) == 1
        assert tasks[0].file_path == str(session_file)


class TestParseGeminiUserContentParts:
    def test_user_text_parts_preserve_whitespace_and_empty_parts(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-0",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": [
                                {"text": "Alpha"},
                                {"text": ""},
                                {"text": "  "},
                                {"text": "Beta  "},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert message["content"] == "Alpha\n\n  \nBeta  "
        assert "content_parts" not in message

    def test_user_string_content_preserves_outer_whitespace(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-whitespace",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": "  padded request  ",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        assert result["messages"][0]["content"] == "  padded request  "

    def test_all_whitespace_user_text_parts_are_not_dropped(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-blank",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": [
                                {"text": "   "},
                                {"text": ""},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        assert result["messages"][0]["content"] == "   \n"
        assert result["stats"]["user_messages"] == 1

    def test_user_inline_data_preserved_without_duplicate_text(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-1",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": [
                                {"text": "Please inspect this screenshot."},
                                {"inlineData": {"mimeType": "image/png", "data": "QUJDRA=="}},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert message["content"] == "Please inspect this screenshot."
        assert message["content_parts"] == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "QUJDRA==",
                },
            }
        ]

    def test_user_function_parts_preserved_and_linked(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-2",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": [
                                {"text": "Use the read result below."},
                                {
                                    "functionCall": {
                                        "name": "read_file",
                                        "args": {"file_path": "/Users/testuser/Documents/myproject/src/app.py"},
                                    }
                                },
                                {
                                    "functionResponse": {
                                        "name": "read_file",
                                        "response": {"output": "print('hello')"},
                                    }
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert message["content"] == "Use the read result below."
        assert len(message["content_parts"]) == 2
        tool_use, tool_result = message["content_parts"]
        assert tool_use["type"] == "tool_use"
        assert tool_use["name"] == "read_file"
        assert tool_use["input"]["file_path"] == "/Users/testuser/Documents/myproject/src/app.py"
        assert tool_result == {
            "type": "tool_result",
            "tool_use_id": tool_use["id"],
            "content": "print('hello')",
        }

    def test_large_blob_string_content_preserved_in_content_parts(self, tmp_path, mock_anonymizer):
        blob = "data:image/png;base64," + ("A" * 5000)
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-3",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": blob,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert "content" not in message
        assert message["content_parts"] == [{"type": "text", "text": blob}]
        assert result["stats"]["user_messages"] == 1

    def test_multi_mb_inline_data_preserved_verbatim(self, tmp_path, mock_anonymizer):
        blob = "A" * (2 * 1024 * 1024)
        session_file = tmp_path / "session-gemini.json"
        session_file.write_text(
            json.dumps(
                {
                    "sessionId": "gemini-session-inline-large",
                    "startTime": "2026-03-24T12:00:00Z",
                    "lastUpdated": "2026-03-24T12:00:01Z",
                    "messages": [
                        {
                            "type": "user",
                            "timestamp": "2026-03-24T12:00:00Z",
                            "content": [
                                {"inlineData": {"mimeType": "image/png", "data": blob}},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert message["content_parts"][0]["source"]["data"] == blob

    def test_jsonl_user_inline_data_preserved(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.jsonl"
        session_file.write_text(
            "\n".join(
                json.dumps(line)
                for line in [
                    {
                        "sessionId": "gemini-jsonl-inline",
                        "startTime": "2026-05-02T02:38:45.721Z",
                        "lastUpdated": "2026-05-02T02:38:45.721Z",
                    },
                    {
                        "id": "message-1",
                        "timestamp": "2026-05-02T02:39:06.404Z",
                        "type": "user",
                        "content": [
                            {"text": "Let's test the image read tool again. Read @in.png and describe it."},
                            {"text": "\n--- Content from referenced files ---"},
                            {"inlineData": {"mimeType": "image/png", "data": "QUJDRA=="}},
                        ],
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        message = result["messages"][0]
        assert message["content"] == (
            "Let's test the image read tool again. Read @in.png and describe it.\n\n--- Content from referenced files ---"
        )
        assert message["content_parts"] == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "QUJDRA==",
                },
            }
        ]

    def test_jsonl_read_file_binary_tool_output_preserved(self, tmp_path, mock_anonymizer):
        session_file = tmp_path / "session-gemini.jsonl"
        lines = [
            {
                "sessionId": "gemini-jsonl-read-image",
                "startTime": "2026-05-02T02:37:49.732Z",
                "lastUpdated": "2026-05-02T02:37:49.732Z",
            },
            {
                "id": "message-1",
                "timestamp": "2026-05-02T02:37:52.741Z",
                "type": "user",
                "content": [{"text": "Let's test the image read tool. Read in.png in this folder and describe it."}],
            },
            {
                "id": "message-2",
                "timestamp": "2026-05-02T02:38:05.274Z",
                "type": "gemini",
                "content": "",
                "thoughts": [{"description": "Reading image contents"}],
                "tokens": {"input": 7, "cached": 2, "output": 3},
                "model": "gemini-3.1-pro-preview",
                "toolCalls": [
                    {
                        "id": "read_file_1",
                        "name": "read_file",
                        "args": {"file_path": "C:\\tmp\\test_codex\\in.png"},
                        "result": [
                            {
                                "functionResponse": {
                                    "id": "read_file_1",
                                    "name": "read_file",
                                    "response": {"output": "Binary content provided (1 item(s))."},
                                }
                            },
                            {"inlineData": {"mimeType": "image/png", "data": "QUJDRA=="}},
                        ],
                        "status": "success",
                    }
                ],
            },
            {"$set": {"lastUpdated": "2026-05-02T02:38:05.275Z"}},
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        result = parse_session_file(session_file, mock_anonymizer)

        assert result is not None
        assistant_message = result["messages"][1]
        tool_use = assistant_message["tool_uses"][0]
        assert tool_use["tool"] == "read_file"
        assert tool_use["input"] == {"file_path": "C:\\tmp\\test_codex\\in.png"}
        assert tool_use["output"] == {
            "text": "Binary content provided (1 item(s)).",
            "raw": {
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "QUJDRA==",
                        },
                    }
                ]
            },
        }
