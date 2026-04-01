"""Tests for CLI export and publish helpers."""

from unittest.mock import MagicMock, patch

import pytest

from dataclaw._cli.exporting import _build_dataset_card, export_to_jsonl, push_to_huggingface


class TestBuildDatasetCard:
    def test_returns_valid_markdown(self):
        meta = {
            "models": {"claude-sonnet-4-20250514": 10},
            "sessions": 10,
            "projects": ["proj1"],
            "total_input_tokens": 50000,
            "total_output_tokens": 3000,
            "exported_at": "2025-01-15T10:00:00+00:00",
        }
        card = _build_dataset_card("user/repo", meta)
        assert "---" in card
        assert "dataclaw" in card
        assert "claude-sonnet" in card
        assert "10" in card

    def test_includes_stable_provider_tags(self):
        meta = {
            "models": {},
            "sessions": 0,
            "projects": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("user/repo", meta)
        assert "  - claude-code" in card
        assert "  - codex-cli" in card
        assert "  - gemini-cli" in card
        assert "  - opencode" in card
        assert "  - openclaw" in card

    def test_yaml_frontmatter(self):
        meta = {
            "models": {},
            "sessions": 0,
            "projects": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("user/repo", meta)
        lines = card.strip().split("\n")
        assert lines[0] == "---"
        second_dash = [i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"]
        assert len(second_dash) >= 1

    def test_contains_repo_id(self):
        meta = {
            "models": {},
            "sessions": 0,
            "projects": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("alice/my-dataset", meta)
        assert "alice/my-dataset" in card


class TestExportToJsonl:
    def test_writes_jsonl(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_data = [
            {
                "session_id": "s1",
                "model": "claude-sonnet-4-20250514",
                "git_branch": "main",
                "start_time": "2025-01-01T00:00:00",
                "end_time": "2025-01-01T01:00:00",
                "messages": [{"role": "user", "content": "hi"}],
                "stats": {"input_tokens": 100, "output_tokens": 50},
                "project": "test",
            }
        ]
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )

        assert output.exists()
        lines = output.read_text().strip().split("\n")
        assert len(lines) == 1
        assert meta["sessions"] == 1

    def test_skips_synthetic_model(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_data = [
            {"session_id": "s1", "model": "<synthetic>", "messages": [{"role": "user", "content": "hi"}], "stats": {}}
        ]
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1

    def test_counts_redactions(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_data = [
            {
                "session_id": "s1",
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}],
                "stats": {"input_tokens": 10, "output_tokens": 5},
            }
        ]
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )
        assert meta["redactions"] >= 1

    def test_skips_none_model(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_data = [
            {"session_id": "s1", "model": None, "messages": [{"role": "user", "content": "hi"}], "stats": {}}
        ]
        projects = [{"dir_name": "t", "display_name": "t"}]
        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1

    def test_dedupes_identical_gemini_sessions_ignoring_project_label(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_upper = {
            "session_id": "g1",
            "model": "gemini-2.5-pro",
            "git_branch": None,
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {"input_tokens": 1, "output_tokens": 2},
            "project": "gemini:ComfyUI",
            "source": "gemini",
        }
        session_lower = {**session_upper, "project": "gemini:comfyui"}
        projects = [
            {"dir_name": "upper", "display_name": "gemini:ComfyUI", "source": "gemini"},
            {"dir_name": "lower", "display_name": "gemini:comfyui", "source": "gemini"},
        ]

        def parse_project_sessions(*args, **kwargs):
            return [session_upper] if args[0] == "upper" else [session_lower]

        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=parse_project_sessions,
            default_source="gemini",
        )

        lines = output.read_text().strip().split("\n")
        assert len(lines) == 1
        assert meta["sessions"] == 1

    def test_keeps_distinct_gemini_snapshots(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_old = {
            "session_id": "g1",
            "model": "gemini-2.5-pro",
            "git_branch": None,
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "messages": [{"role": "user", "content": "short"}],
            "stats": {"input_tokens": 1, "output_tokens": 2},
            "project": "gemini:comfyui",
            "source": "gemini",
        }
        session_new = {
            **session_old,
            "end_time": "2026-01-01T00:02:00Z",
            "messages": [{"role": "user", "content": "longer"}],
            "stats": {"input_tokens": 3, "output_tokens": 4},
        }
        projects = [{"dir_name": "proj", "display_name": "gemini:comfyui", "source": "gemini"}]

        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: [session_old, session_new],
            default_source="gemini",
        )

        lines = output.read_text().strip().split("\n")
        assert len(lines) == 2
        assert meta["sessions"] == 2


class TestPushToHuggingface:
    def test_missing_huggingface_hub(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                raise ImportError("No module named 'huggingface_hub'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(SystemExit):
            push_to_huggingface(jsonl_path, "user/repo", {})

    def test_success_flow(self, tmp_path):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        mock_api = MagicMock()
        mock_api.whoami.return_value = {"name": "alice"}
        mock_hfapi_cls = MagicMock(return_value=mock_api)

        with patch.dict("sys.modules", {"huggingface_hub": MagicMock(HfApi=mock_hfapi_cls)}):
            push_to_huggingface(jsonl_path, "user/repo", {})

        mock_api.create_repo.assert_called_once_with("user/repo", repo_type="dataset", exist_ok=True)
        assert mock_api.upload_file.call_count == 3

    def test_auth_failure(self, tmp_path):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        mock_api = MagicMock()
        mock_api.whoami.side_effect = OSError("Auth failed")
        mock_hf_module = MagicMock(HfApi=MagicMock(return_value=mock_api))

        with patch.dict("sys.modules", {"huggingface_hub": mock_hf_module}):
            with pytest.raises(SystemExit):
                push_to_huggingface(jsonl_path, "user/repo", {})
