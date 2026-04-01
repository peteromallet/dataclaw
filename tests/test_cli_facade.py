"""Tests for CLI facade commands and main dispatch."""

import pytest

from dataclaw import _json as json
from dataclaw._cli.common import _source_label
from dataclaw.cli import configure, list_projects, main
from tests.cli_helpers import extract_json_payload


class TestConfigure:
    def test_sets_repo(self, tmp_config, monkeypatch):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr(
            "dataclaw.cli.load_config",
            lambda: {"repo": None, "excluded_projects": [], "redact_strings": []},
        )
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(repo="alice/my-repo")
        assert saved["repo"] == "alice/my-repo"

    def test_merges_exclude(self, tmp_config, monkeypatch):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"excluded_projects": ["a"], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(exclude=["b", "c"])
        assert sorted(saved["excluded_projects"]) == ["a", "b", "c"]

    def test_sets_source(self, tmp_config, monkeypatch):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": None, "source": None})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert saved["source"] == "codex"


class TestListProjects:
    def test_with_projects(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [{"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024}],
        )
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"excluded_projects": []})
        list_projects()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "proj1"

    def test_no_projects(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.discover_projects", lambda: [])
        list_projects()
        captured = capsys.readouterr()
        assert captured.out.strip() == f"No {_source_label('auto')} sessions found."

    def test_source_filter_codex(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [
                {"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"},
                {"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"},
            ],
        )
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"excluded_projects": []})
        list_projects(source_filter="codex")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "codex:proj2"
        assert data[0]["source"] == "codex"

    def test_no_projects_for_selected_source(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [{"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"}],
        )
        list_projects(source_filter="codex")
        captured = capsys.readouterr()
        assert "No codex sessions found." in captured.out

    def test_main_list_uses_configured_source_when_auto(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [
                {"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"},
                {"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"},
            ],
        )
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"source": "codex", "excluded_projects": []})
        monkeypatch.setattr("sys.argv", ["dataclaw", "list"])
        main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "codex:proj2"


class TestWorkflowGateMessages:
    def test_confirm_without_export_shows_step_process(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "missing.jsonl"
        monkeypatch.setattr("sys.argv", ["dataclaw", "confirm", "--file", str(missing)])
        with pytest.raises(SystemExit):
            main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["error"] == "No export file found."
        assert payload["blocked_on_step"] == "Step 1/3"
        assert len(payload["process_steps"]) == 3
        assert "export --no-push" in payload["process_steps"][0]

    def test_confirm_missing_full_name_explains_purpose_and_skip(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text('{"project":"p","model":"m","messages":[]}\n')
        monkeypatch.setattr(
            "sys.argv",
            [
                "dataclaw",
                "confirm",
                "--file",
                str(export_file),
                "--attest-full-name",
                "Asked for full name and scanned export.",
                "--attest-sensitive",
                "Asked about company/client/internal names and private URLs; none found.",
                "--attest-manual-scan",
                "Manually scanned 20 sessions across beginning/middle/end and reviewed findings.",
            ],
        )
        with pytest.raises(SystemExit):
            main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["error"] == "Missing required --full-name for verification scan."
        assert "--skip-full-name-scan" in payload["hint"]
        assert payload["blocked_on_step"] == "Step 2/3"
        assert len(payload["process_steps"]) == 3

    def test_confirm_skip_full_name_scan_succeeds(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text('{"project":"p","model":"m","messages":[]}\n')
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})
        monkeypatch.setattr("dataclaw.cli.save_config", lambda _c: None)
        monkeypatch.setattr(
            "sys.argv",
            [
                "dataclaw",
                "confirm",
                "--file",
                str(export_file),
                "--skip-full-name-scan",
                "--attest-full-name",
                "User declined to share full name; skipped exact-name scan.",
                "--attest-sensitive",
                "I asked about company/client/internal names and private URLs; none found.",
                "--attest-manual-scan",
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            ],
        )
        main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["stage"] == "confirmed"
        assert payload["full_name_scan"]["skipped"] is True

    def test_push_before_confirm_shows_step_process(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"stage": "review", "source": "all"})
        monkeypatch.setattr("sys.argv", ["dataclaw", "export"])
        with pytest.raises(SystemExit):
            main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["error"] == "You must run `dataclaw confirm` before pushing."
        assert payload["blocked_on_step"] == "Step 2/3"
        assert len(payload["process_steps"]) == 3
        assert "confirm" in payload["process_steps"][1]

    def test_export_requires_project_confirmation_with_full_flow(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli._has_session_sources", lambda _src: True)
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [{"display_name": "proj1", "session_count": 2, "total_size_bytes": 1024, "source": "claude"}],
        )
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"source": "all"})
        monkeypatch.setattr("sys.argv", ["dataclaw", "export", "--no-push"])
        with pytest.raises(SystemExit):
            main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["error"] == "Project selection is not confirmed yet."
        assert payload["blocked_on_step"] == "Step 3/6"
        assert len(payload["process_steps"]) == 6
        assert "prep && dataclaw list" in payload["process_steps"][0]
        assert payload["required_action"].startswith("Send the full project/folder list")
        assert "in a message" in payload["required_action"]
        assert isinstance(payload["projects"], list)
        assert payload["projects"][0]["name"] == "proj1"
        assert payload["projects"][0]["sessions"] == 2

    def test_export_requires_explicit_source_selection(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})
        monkeypatch.setattr("sys.argv", ["dataclaw", "export", "--no-push"])
        with pytest.raises(SystemExit):
            main()
        payload = extract_json_payload(capsys.readouterr().out)
        assert payload["error"] == "Source scope is not confirmed yet."
        assert payload["blocked_on_step"] == "Step 2/6"
        assert len(payload["process_steps"]) == 6
        assert payload["allowed_sources"] == [
            "all",
            "both",
            "claude",
            "codex",
            "cursor",
            "custom",
            "gemini",
            "kimi",
            "openclaw",
            "opencode",
        ]
        assert payload["next_command"] == "dataclaw config --source all"
