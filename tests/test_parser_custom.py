"""Tests for custom parser behavior."""

from dataclaw import _json as json
from dataclaw.parser import discover_projects, parse_project_sessions
from dataclaw.secrets import REDACTED
from tests.parser_helpers import disable_other_providers


class TestDiscoverCustomProjects:
    def _make_valid_session(self, session_id="s1", model="gpt-4", content="hello"):
        return json.dumps(
            {
                "session_id": session_id,
                "model": model,
                "messages": [
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": "hi there"},
                ],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 1,
                    "tool_uses": 0,
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            }
        )

    def test_discover_custom_projects(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"custom"})
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "my-project"
        project_dir.mkdir(parents=True)
        (project_dir / "sessions.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2") + "\n"
        )
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "custom:my-project"
        assert projects[0]["session_count"] == 2
        assert projects[0]["source"] == "custom"

    def test_discover_skips_empty_dir(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"custom"})
        custom_dir = tmp_path / "custom"
        (custom_dir / "empty-project").mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        assert discover_projects() == []

    def test_discover_missing_dir(self, tmp_path, monkeypatch):
        disable_other_providers(monkeypatch, tmp_path, keep={"custom"})
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", tmp_path / "nonexistent")
        assert discover_projects() == []

    def test_parse_valid_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "test-proj"
        project_dir.mkdir(parents=True)
        (project_dir / "data.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2", model="o1") + "\n"
        )
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        assert sessions[0]["session_id"] == "s1"
        assert sessions[1]["model"] == "o1"
        assert sessions[0]["project"] == "custom:test-proj"
        assert sessions[0]["source"] == "custom"

    def test_parse_skips_missing_fields(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "test-proj"
        project_dir.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        no_model = json.dumps({"session_id": "s2", "messages": []})
        no_messages = json.dumps({"session_id": "s3", "model": "m"})
        no_session_id = json.dumps({"model": "m", "messages": []})
        (project_dir / "data.jsonl").write_text("\n".join([valid, no_model, no_messages, no_session_id]) + "\n")
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    def test_parse_skips_invalid_json(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "test-proj"
        project_dir.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        (project_dir / "data.jsonl").write_text(valid + "\n" + "not-json\n")
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1

    def test_parse_multiple_files(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "test-proj"
        project_dir.mkdir(parents=True)
        (project_dir / "a.jsonl").write_text(self._make_valid_session("s1") + "\n")
        (project_dir / "b.jsonl").write_text(self._make_valid_session("s2") + "\n")
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        assert {session["session_id"] for session in sessions} == {"s1", "s2"}

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir(parents=True)
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        assert parse_project_sessions("nope", mock_anonymizer, source="custom") == []

    def test_parser_does_not_pre_redact_message_content(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        project_dir = custom_dir / "test-proj"
        project_dir.mkdir(parents=True)
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
        (project_dir / "data.jsonl").write_text(self._make_valid_session("s1", content=f"token={secret}") + "\n")
        monkeypatch.setattr("dataclaw.parsers.custom.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        content = sessions[0]["messages"][0]["content"]
        assert secret in content
        assert REDACTED not in content
