"""Tests for OpenCode parser behavior."""

import sqlite3
from pathlib import Path

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from tests.parser_helpers import disable_other_providers, write_opencode_db


class TestOpenCodeProjects:
    def test_discover_opencode_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_1", "/Users/testuser/work/repo", 1706000000000, 1706000002000),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "opencode"
        assert projects[0]["display_name"] == "opencode:repo"

    def test_parse_opencode_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_1"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_message_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        assistant_message_data = {
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
            ("msg_1", session_id, 1706000001000, json.dumps(user_message_data)),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_2", session_id, 1706000002000, json.dumps(assistant_message_data)),
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
            ("prt_4", "msg_2", 1706000002003, json.dumps({"type": "text", "text": "I checked the directory."})),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "opencode:repo"
        assert sessions[0]["model"] == "openai/gpt-5.3-codex"
        assert sessions[0]["stats"]["input_tokens"] == 150
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "bash"

    def test_parse_opencode_user_file_parts(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_files"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_message_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_file", session_id, 1706000001000, json.dumps(user_message_data)),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_text",
                "msg_file",
                1706000001001,
                json.dumps({"type": "text", "text": "Please inspect these files."}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_image",
                "msg_file",
                1706000001002,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "image/png",
                        "filename": "plot.png",
                        "url": "data:image/png;base64,QUJDRA==",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_doc",
                "msg_file",
                1706000001003,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "notes.txt",
                        "url": "file:///Users/testuser/work/repo/notes.txt",
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert message["content"] == "Please inspect these files."
        assert message["content_parts"][0] == {
            "type": "image",
            "filename": "plot.png",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "QUJDRA==",
            },
        }
        assert message["content_parts"][1]["type"] == "document"
        assert message["content_parts"][1]["source"]["type"] == "url"
        assert message["content_parts"][1]["source"]["media_type"] == "text/plain"
        assert message["content_parts"][1]["source"]["url"].startswith("file:///Users/testuser")
        assert message["content_parts"][1]["source"]["url"].endswith("/work/repo/notes.txt")

    def test_parse_opencode_user_file_only_message(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_file_only"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_file_only",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_file_only",
                "msg_file_only",
                1706000001001,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "image/png",
                        "filename": "plot.png",
                        "url": "data:image/png;base64," + ("A" * 5000),
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert "content" not in message
        assert message["content_parts"] == [
            {
                "type": "image",
                "filename": "plot.png",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 5000,
                },
            }
        ]

    def test_parse_opencode_user_synthetic_image_file(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_synthetic_file"
        cwd = "C:\\tmp\\test_codex"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.5"}}),
            ),
        )
        parts = [
            {"id": "prt_text", "data": {"type": "text", "text": "Let's test the image read tool again."}},
            {
                "id": "prt_synthetic_call",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_codex\\\\in.png"}',
                },
            },
            {
                "id": "prt_synthetic_output",
                "data": {"type": "text", "synthetic": True, "text": "Image read successfully"},
            },
            {
                "id": "prt_image",
                "data": {
                    "type": "file",
                    "mime": "image/png",
                    "url": "data:image/png;base64,QUJDRA==",
                    "synthetic": True,
                    "filename": "in.png",
                },
            },
        ]
        for index, part in enumerate(parts):
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part["id"], "msg_user", 1706000001001 + index, json.dumps(part["data"])),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert message["content"] == "Let's test the image read tool again."
        assert message["content_parts"] == [
            {
                "type": "image",
                "path": "C:\\tmp\\test_codex\\in.png",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "QUJDRA==",
                },
            }
        ]

    def test_parse_opencode_user_synthetic_text_file_content(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_synthetic_text_file"
        cwd = "C:\\dataclaw"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.5"}}),
            ),
        )
        parts = [
            {"id": "prt_text", "data": {"type": "text", "text": "Check @dataclaw\\cli.py"}},
            {
                "id": "prt_synthetic_call",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\dataclaw\\\\dataclaw\\\\cli.py"}',
                },
            },
            {
                "id": "prt_synthetic_content",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": '<path>C:\\dataclaw\\dataclaw\\cli.py</path>\n<type>file</type>\n<content>\n1: """CLI facade for DataClaw."""\n</content>',
                },
            },
            {
                "id": "prt_file",
                "data": {
                    "type": "file",
                    "mime": "text/plain",
                    "filename": "dataclaw\\cli.py",
                    "url": "file:///C:/dataclaw/dataclaw/cli.py",
                },
            },
        ]
        for index, part in enumerate(parts):
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part["id"], "msg_user", 1706000001001 + index, json.dumps(part["data"])),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert message["content"] == (
            'Check @dataclaw\\cli.py\n\n<path>C:\\dataclaw\\dataclaw\\cli.py</path>\n<type>file</type>\n<content>\n1: """CLI facade for DataClaw."""\n</content>'
        )
        assert message["content_parts"] == [
            {
                "type": "document",
                "source": {
                    "type": "url",
                    "url": "file:///C:/dataclaw/dataclaw/cli.py",
                    "media_type": "text/plain",
                },
            }
        ]

    def test_parse_opencode_user_synthetic_legacy_text_file_contents(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_legacy_text_files"
        cwd = "C:\\tmp\\test_html"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.5"}}),
            ),
        )
        parts = [
            {
                "id": "prt_text",
                "data": {"type": "text", "text": "Read @abs.html and @abs_expected.json"},
            },
            {
                "id": "prt_doc_html",
                "data": {
                    "type": "file",
                    "mime": "text/plain",
                    "filename": "abs.html",
                    "url": "file://C:\\tmp\\test_html/abs.html",
                },
            },
            {
                "id": "prt_doc_json",
                "data": {
                    "type": "file",
                    "mime": "text/plain",
                    "filename": "abs_expected.json",
                    "url": "file://C:\\tmp\\test_html/abs_expected.json",
                },
            },
            {
                "id": "prt_synthetic_call_json",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_html\\\\abs_expected.json"}',
                },
            },
            {
                "id": "prt_synthetic_call_html",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_html\\\\abs.html"}',
                },
            },
            {
                "id": "prt_synthetic_html",
                "data": {"type": "text", "synthetic": True, "text": "<file>\n00001| <html>\n</file>"},
            },
            {
                "id": "prt_synthetic_json",
                "data": {"type": "text", "synthetic": True, "text": '<file>\n00001| {"Input": {}}\n</file>'},
            },
        ]
        for index, part in enumerate(parts):
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part["id"], "msg_user", 1706000001001 + index, json.dumps(part["data"])),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert "Called the Read tool" not in message["content"]
        assert message["content"] == (
            'Read @abs.html and @abs_expected.json\n\n<file>\n00001| <html>\n</file>\n\n<file>\n00001| {"Input": {}}\n</file>'
        )
        assert message["content_parts"] == [
            {
                "type": "document",
                "source": {
                    "type": "url",
                    "url": "file://C:\\tmp\\test_html/abs.html",
                    "media_type": "text/plain",
                },
            },
            {
                "type": "document",
                "source": {
                    "type": "url",
                    "url": "file://C:\\tmp\\test_html/abs_expected.json",
                    "media_type": "text/plain",
                },
            },
        ]

    def test_parse_opencode_user_synthetic_multiple_image_and_text_files(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_multiple_files"
        cwd = "C:\\tmp\\test_codex"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.5"}}),
            ),
        )
        parts = [
            {"id": "prt_text", "data": {"type": "text", "text": "Read @in1.png, @in2.png, and @in3.txt"}},
            {
                "id": "prt_synthetic_call_image1",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_codex\\\\in1.png"}',
                },
            },
            {
                "id": "prt_image_success1",
                "data": {"type": "text", "synthetic": True, "text": "Image read successfully"},
            },
            {
                "id": "prt_image1",
                "data": {
                    "type": "file",
                    "mime": "image/png",
                    "filename": "in1.png",
                    "url": "data:image/png;base64,SU1HMQ==",
                    "synthetic": True,
                },
            },
            {
                "id": "prt_synthetic_call_image2",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_codex\\\\in2.png"}',
                },
            },
            {
                "id": "prt_image_success2",
                "data": {"type": "text", "synthetic": True, "text": "Image read successfully"},
            },
            {
                "id": "prt_image2",
                "data": {
                    "type": "file",
                    "mime": "image/png",
                    "filename": "in2.png",
                    "url": "data:image/png;base64,SU1HMg==",
                    "synthetic": True,
                },
            },
            {
                "id": "prt_synthetic_call_text",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": 'Called the Read tool with the following input: {"filePath":"C:\\\\tmp\\\\test_codex\\\\in3.txt"}',
                },
            },
            {
                "id": "prt_synthetic_text_content",
                "data": {
                    "type": "text",
                    "synthetic": True,
                    "text": "<path>C:\\tmp\\test_codex\\in3.txt</path>\n<type>file</type>\n<content>\n1: hello\n</content>",
                },
            },
            {
                "id": "prt_doc",
                "data": {
                    "type": "file",
                    "mime": "text/plain",
                    "filename": "in3.txt",
                    "url": "file://C:\\tmp\\test_codex/in3.txt",
                },
            },
        ]
        for index, part in enumerate(parts):
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part["id"], "msg_user", 1706000001001 + index, json.dumps(part["data"])),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        message = sessions[0]["messages"][0]
        assert "Called the Read tool" not in message["content"]
        assert "Image read successfully" not in message["content"]
        assert message["content"] == (
            "Read @in1.png, @in2.png, and @in3.txt\n\n"
            "<path>C:\\tmp\\test_codex\\in3.txt</path>\n<type>file</type>\n<content>\n1: hello\n</content>"
        )
        assert message["content_parts"] == [
            {
                "type": "image",
                "path": "C:\\tmp\\test_codex\\in1.png",
                "source": {"type": "base64", "media_type": "image/png", "data": "SU1HMQ=="},
            },
            {
                "type": "image",
                "path": "C:\\tmp\\test_codex\\in2.png",
                "source": {"type": "base64", "media_type": "image/png", "data": "SU1HMg=="},
            },
            {
                "type": "document",
                "source": {
                    "type": "url",
                    "url": "file://C:\\tmp\\test_codex/in3.txt",
                    "media_type": "text/plain",
                },
            },
        ]

    def test_parse_opencode_tool_image_attachments(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_tool_attachment"
        cwd = "C:\\tmp\\test_codex"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.5"}}),
            ),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_assistant",
                session_id,
                1706000002000,
                json.dumps({"role": "assistant", "providerID": "openai", "modelID": "gpt-5.5"}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_user", "msg_user", 1706000001001, json.dumps({"type": "text", "text": "Read in.png"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_read",
                "msg_assistant",
                1706000002001,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "C:\\tmp\\test_codex\\in.png", "limit": 2000, "offset": 1},
                            "output": "Image read successfully",
                            "attachments": [
                                {
                                    "type": "file",
                                    "mime": "image/png",
                                    "url": "data:image/png;base64,QUJDRA==",
                                    "id": "prt_attachment",
                                    "sessionID": session_id,
                                    "messageID": "msg_assistant",
                                }
                            ],
                        },
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        tool_use = sessions[0]["messages"][1]["tool_uses"][0]
        assert tool_use["status"] == "success"
        assert tool_use["output"] == {
            "text": "Image read successfully",
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

    def test_parse_opencode_tool_error_output(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_tool_error"
        cwd = "C:\\tmp\\test_codex"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_assistant",
                session_id,
                1706000002000,
                json.dumps({"role": "assistant", "providerID": "openai", "modelID": "gpt-5.5"}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_read_error",
                "msg_assistant",
                1706000002001,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "error",
                            "input": {"filePath": "C:\\tmp\\test_codex\\in.png", "limit": 2000, "offset": 0},
                            "error": "offset must be greater than or equal to 1",
                        },
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        tool_use = sessions[0]["messages"][0]["tool_uses"][0]
        assert tool_use["status"] == "error"
        assert tool_use["output"] == {"text": "offset must be greater than or equal to 1"}

    def test_parse_project_sessions_reuses_single_db_connection(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        cwd = "/Users/testuser/work/repo"
        for index in range(2):
            session_id = f"ses_{index}"
            message_id = f"msg_{index}"
            part_id = f"prt_{index}"
            conn.execute(
                "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
                (session_id, cwd, 1706000000000 + index, 1706000005000 + index),
            )
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    1706000001000 + index,
                    json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
                ),
            )
            conn.execute(
                "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
                (part_id, message_id, 1706000001001 + index, json.dumps({"type": "text", "text": f"Hello {index}"})),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        connect_calls = 0
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            nonlocal connect_calls
            if args and Path(args[0]) == db_path:
                connect_calls += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("dataclaw.parsers.opencode.sqlite3.connect", counting_connect)

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 2
        assert connect_calls == 2

    def test_parse_opencode_surrogate_escaped_tool_output(self, tmp_path, monkeypatch, mock_anonymizer):
        disable_other_providers(monkeypatch, tmp_path, keep={"opencode"})
        db_path = tmp_path / "opencode.db"
        conn = write_opencode_db(db_path)

        session_id = "ses_surrogate"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_user",
                session_id,
                1706000001000,
                json.dumps({"role": "user", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "msg_assistant",
                session_id,
                1706000002000,
                json.dumps({"role": "assistant", "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_user", "msg_user", 1706000001001, json.dumps({"type": "text", "text": "show output"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_tool",
                "msg_assistant",
                1706000002001,
                '{"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"python bad.py"},"output":"prefix '
                + "\\udcbf"
                + ' suffix"}}',
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("dataclaw.parsers.opencode.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("dataclaw.parsers.opencode._PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")

        assert len(sessions) == 1
        assert sessions[0]["messages"][1]["tool_uses"][0]["output"] == {"text": r"prefix \xbf suffix"}
        assert json.dumps(sessions[0])
