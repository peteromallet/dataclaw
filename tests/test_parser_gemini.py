"""Tests for Gemini parser behavior."""

from dataclaw import _json as json
from dataclaw.parsers.gemini import discover_projects, parse_session_file


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
