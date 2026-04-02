"""Tests for CLI facade commands and main dispatch."""

from pathlib import Path

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
        assert payload["blocked_on_step"] == "Step 4/6"
        assert len(payload["process_steps"]) == 9
        assert any("Step 4 - Export locally" in step for step in payload["process_steps"])

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
        assert payload["blocked_on_step"] == "Step 5/6"
        assert len(payload["process_steps"]) == 9

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
        assert payload["blocked_on_step"] == "Step 5/6"
        assert len(payload["process_steps"]) == 9
        assert any("Step 5 - Review and confirm" in step for step in payload["process_steps"])

    def test_push_reuses_confirmed_file(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "confirmed.jsonl"
        export_file.write_text('{"project":"p","model":"m","stats":{"input_tokens":1,"output_tokens":2}}\n')

        saved = {}
        pushed = {}

        monkeypatch.setattr(
            "dataclaw.cli.load_config",
            lambda: {
                "stage": "confirmed",
                "repo": "alice/repo",
                "last_confirm": {"file": str(export_file)},
                "review_attestations": {
                    "asked_full_name": "User declined to share full name; skipped exact-name scan.",
                    "asked_sensitive_entities": "I asked about company, client, internal names, and URLs; none required extra redaction.",
                    "manual_scan_done": "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
                },
                "review_verification": {
                    "full_name": None,
                    "full_name_scan_skipped": True,
                    "manual_scan_sessions": 20,
                },
            },
        )
        monkeypatch.setattr("dataclaw.cli.save_config", lambda cfg: saved.update(cfg))
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: (_ for _ in ()).throw(AssertionError("should not rediscover projects")),
        )
        monkeypatch.setattr(
            "dataclaw.cli._has_session_sources",
            lambda _src: (_ for _ in ()).throw(AssertionError("should not probe sources")),
        )
        monkeypatch.setattr(
            "dataclaw.cli.export_to_jsonl",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not regenerate jsonl")),
        )

        def fake_push(path, repo_id, meta):
            pushed["path"] = path
            pushed["repo_id"] = repo_id
            pushed["meta"] = meta

        monkeypatch.setattr("dataclaw.cli.push_to_huggingface", fake_push)
        monkeypatch.setattr(
            "sys.argv",
            [
                "dataclaw",
                "export",
                "--publish-attestation",
                "User explicitly approved publishing to Hugging Face.",
            ],
        )

        main()

        assert pushed["path"] == export_file
        assert pushed["repo_id"] == "alice/repo"
        assert pushed["meta"]["sessions"] == 1
        assert saved["stage"] == "done"
        output = capsys.readouterr().out
        assert "Reusing confirmed export file" in output

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
        assert payload["blocked_on_step"] == "Step 3B/6"
        assert len(payload["process_steps"]) == 9
        assert any("Step 3 - Prep" in step for step in payload["process_steps"])
        assert any("Step 3B - Choose project scope" in step for step in payload["process_steps"])
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
        assert payload["blocked_on_step"] == "Step 3A/6"
        assert len(payload["process_steps"]) == 9
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


class TestHelpAndCommandOrdering:
    def test_main_without_command_shows_help(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli._run_export", lambda _args: (_ for _ in ()).throw(AssertionError("should not export"))
        )
        monkeypatch.setattr("sys.argv", ["dataclaw"])

        main()

        output = capsys.readouterr().out
        assert "usage:" in output
        assert "{status,update-skill,prep,config,list,export,confirm,jsonl-to-yaml,diff-jsonl}" in output

    def test_help_lists_commands_in_workflow_order(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["dataclaw", "--help"])

        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        status_idx = output.index("status")
        update_skill_idx = output.index("update-skill")
        prep_idx = output.index("prep")
        config_idx = output.index("config")
        list_idx = output.index("list")
        export_idx = output.index("export")
        confirm_idx = output.index("confirm")
        jsonl_idx = output.index("jsonl-to-yaml")
        diff_idx = output.index("diff-jsonl")
        assert (
            status_idx
            < update_skill_idx
            < prep_idx
            < config_idx
            < list_idx
            < export_idx
            < confirm_idx
            < jsonl_idx
            < diff_idx
        )


class TestJsonlUtilityCommands:
    def test_main_jsonl_to_yaml_dispatches(self, monkeypatch, capsys):
        captured = {}

        def fake_jsonl_to_yaml(input_path, output_path):
            captured["input_path"] = input_path
            captured["output_path"] = output_path
            return Path("/tmp/rendered.yaml")

        monkeypatch.setattr("dataclaw.cli.jsonl_to_yaml", fake_jsonl_to_yaml)
        monkeypatch.setattr("sys.argv", ["dataclaw", "jsonl-to-yaml", "sample.jsonl", "-o", "sample.yaml"])

        main()

        assert captured == {
            "input_path": Path("sample.jsonl"),
            "output_path": Path("sample.yaml"),
        }
        assert f"Written to {Path('/tmp/rendered.yaml')}" in capsys.readouterr().out

    def test_main_diff_jsonl_dispatches(self, monkeypatch, capsys):
        captured = {}

        def fake_diff_jsonl(old_path, new_path, output_path, include_records_for_modified):
            captured["old_path"] = old_path
            captured["new_path"] = new_path
            captured["output_path"] = output_path
            captured["include_records_for_modified"] = include_records_for_modified
            return {"event_count": 2, "output_path": Path("/tmp/diff.yaml"), "summary": {}}

        monkeypatch.setattr("dataclaw.cli.diff_jsonl", fake_diff_jsonl)
        monkeypatch.setattr(
            "sys.argv",
            [
                "dataclaw",
                "diff-jsonl",
                "--old",
                "old.jsonl",
                "--new",
                "new.jsonl",
                "-o",
                "diff.yaml",
                "--include-records-for-modified",
            ],
        )

        main()

        assert captured == {
            "old_path": Path("old.jsonl"),
            "new_path": Path("new.jsonl"),
            "output_path": Path("diff.yaml"),
            "include_records_for_modified": True,
        }
        assert f"Wrote 2 change documents to {Path('/tmp/diff.yaml')}" in capsys.readouterr().out
