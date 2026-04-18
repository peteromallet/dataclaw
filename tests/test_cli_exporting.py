"""Tests for CLI export and publish helpers."""

from unittest.mock import MagicMock, patch

import pytest

from dataclaw import _json as json
from dataclaw._cli.exporting import _build_dataset_card, export_to_jsonl, push_to_huggingface, summarize_export_jsonl


class TestBuildDatasetCard:
    def test_returns_valid_markdown(self):
        meta = {
            "model_breakdown": {
                "claude-sonnet-4-20250514": {"sessions": 10, "input_tokens": 50000, "output_tokens": 3000}
            },
            "sessions": 10,
            "project_breakdown": {"proj1": {"sessions": 10, "input_tokens": 50000, "output_tokens": 3000}},
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
            "model_breakdown": {},
            "sessions": 0,
            "project_breakdown": {},
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
            "model_breakdown": {},
            "sessions": 0,
            "project_breakdown": {},
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
            "model_breakdown": {},
            "sessions": 0,
            "project_breakdown": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("alice/my-dataset", meta)
        assert "alice/my-dataset" in card

    def test_includes_model_and_project_tables_sorted_by_output_tokens(self):
        meta = {
            "model_breakdown": {
                "m1": {"sessions": 1, "input_tokens": 10, "output_tokens": 3},
                "m2": {"sessions": 2, "input_tokens": 20, "output_tokens": 7},
            },
            "sessions": 3,
            "project_breakdown": {
                "p1": {"sessions": 2, "input_tokens": 15, "output_tokens": 9},
                "p2": {"sessions": 1, "input_tokens": 15, "output_tokens": 2},
            },
            "total_input_tokens": 30,
            "total_output_tokens": 10,
            "exported_at": "2025-01-15T10:00:00+00:00",
        }

        card = _build_dataset_card("user/repo", meta)

        assert "### Models" in card
        assert "| Model | Sessions | Input tokens | Output tokens |" in card
        assert card.index("| m2 | 2 | 20 | 7 |") < card.index("| m1 | 1 | 10 | 3 |")
        assert "### Projects" in card
        assert "| Project | Sessions | Input tokens | Output tokens |" in card
        assert card.index("| p1 | 2 | 15 | 9 |") < card.index("| p2 | 1 | 15 | 2 |")

    def test_normalizes_model_and_project_keys_in_card(self):
        meta = {
            "sessions": 3,
            "model_breakdown": {
                "openai/gpt-5.4": {"sessions": 1, "input_tokens": 10, "output_tokens": 3},
                "gpt-5.4": {"sessions": 2, "input_tokens": 20, "output_tokens": 7},
            },
            "project_breakdown": {
                "codex:ComfyUI": {"sessions": 1, "input_tokens": 15, "output_tokens": 9},
                "comfyui": {"sessions": 2, "input_tokens": 15, "output_tokens": 2},
            },
            "total_input_tokens": 30,
            "total_output_tokens": 10,
            "exported_at": "2025-01-15T10:00:00+00:00",
        }

        card = _build_dataset_card("user/repo", meta)

        assert "| gpt-5-4 | 3 | 30 | 10 |" in card
        assert "openai/gpt-5.4" not in card
        assert "| comfyui | 3 | 30 | 11 |" in card
        assert "codex:ComfyUI" not in card


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
        assert "models" not in meta
        assert "projects" not in meta
        assert meta["model_breakdown"] == {
            "claude-sonnet-4-20250514": {"sessions": 1, "input_tokens": 100, "output_tokens": 50}
        }
        assert meta["project_breakdown"] == {"test": {"sessions": 1, "input_tokens": 100, "output_tokens": 50}}

    def test_prints_per_project_elapsed_and_tokens_when_available(self, tmp_path, mock_anonymizer, monkeypatch, capsys):
        output = tmp_path / "out.jsonl"
        perf_counter_values = iter([10.0, 11.25])
        session_data = [
            {
                "session_id": "s1",
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "hi"}],
                "stats": {"input_tokens": 12, "output_tokens": 34},
                "project": "test",
            }
        ]

        monkeypatch.setattr("dataclaw._cli.exporting.time.perf_counter", lambda: next(perf_counter_values))

        export_to_jsonl(
            [{"dir_name": "test", "display_name": "test"}],
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )

        assert "Parsing test... 1 sessions in 1.25s (12 input / 34 output tokens)" in capsys.readouterr().out

    def test_normalizes_stats_without_changing_dataset_rows(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_data = [
            {
                "session_id": "s1",
                "model": "openai/gpt-5.4",
                "messages": [{"role": "user", "content": "hi"}],
                "stats": {"input_tokens": 10, "output_tokens": 3},
                "project": "codex:ComfyUI",
            },
            {
                "session_id": "s2",
                "model": "gpt-5.4",
                "messages": [{"role": "user", "content": "hi"}],
                "stats": {"input_tokens": 20, "output_tokens": 7},
                "project": "comfyui",
            },
        ]
        projects = [{"dir_name": "test", "display_name": "test"}]

        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="claude",
        )

        lines = output.read_text().strip().split("\n")
        assert '"model":"openai/gpt-5.4"' in lines[0]
        assert '"project":"codex:ComfyUI"' in lines[0]
        assert meta["model_breakdown"] == {"gpt-5-4": {"sessions": 2, "input_tokens": 30, "output_tokens": 10}}
        assert meta["project_breakdown"] == {"comfyui": {"sessions": 2, "input_tokens": 30, "output_tokens": 10}}

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

    def test_accepts_session_iterators(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        projects = [{"dir_name": "test", "display_name": "test"}]

        def iter_sessions(*args, **kwargs):
            del args, kwargs
            yield {
                "session_id": "s1",
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "hi"}],
                "stats": {"input_tokens": 10, "output_tokens": 5},
                "project": "test",
            }

        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=iter_sessions,
            default_source="claude",
        )

        assert output.exists()
        assert output.read_text().count("\n") == 1
        assert meta["sessions"] == 1

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

    def test_dedupes_gemini_sessions_with_different_dict_insertion_order(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        session_a = {
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
        session_b = {
            "source": "gemini",
            "project": "gemini:comfyui",
            "stats": {"output_tokens": 2, "input_tokens": 1},
            "messages": [{"content": "hi", "role": "user"}],
            "end_time": "2026-01-01T00:01:00Z",
            "start_time": "2026-01-01T00:00:00Z",
            "git_branch": None,
            "model": "gemini-2.5-pro",
            "session_id": "g1",
        }
        projects = [{"dir_name": "proj", "display_name": "gemini:comfyui", "source": "gemini"}]

        meta = export_to_jsonl(
            projects,
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: [session_a, session_b],
            default_source="gemini",
        )

        lines = output.read_text().strip().split("\n")
        assert len(lines) == 1
        assert meta["sessions"] == 1

    def test_writes_multi_mb_blob_verbatim(self, tmp_path, mock_anonymizer):
        output = tmp_path / "out.jsonl"
        blob = "A" * (2 * 1024 * 1024)
        session_data = [
            {
                "session_id": "g-large",
                "model": "gemini-2.5-pro",
                "git_branch": None,
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T00:01:00Z",
                "messages": [
                    {
                        "role": "user",
                        "content_parts": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": blob},
                            }
                        ],
                    }
                ],
                "stats": {"input_tokens": 1, "output_tokens": 2},
                "project": "gemini:proj",
                "source": "gemini",
            }
        ]

        meta = export_to_jsonl(
            [{"dir_name": "proj", "display_name": "gemini:proj", "source": "gemini"}],
            output,
            mock_anonymizer,
            parse_project_sessions_fn=lambda *args, **kwargs: session_data,
            default_source="gemini",
        )

        rows = [json.loads(line) for line in output.read_bytes().splitlines() if line.strip()]
        assert meta["sessions"] == 1
        assert rows[0]["messages"][0]["content_parts"][0]["source"]["data"] == blob


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


class TestSummarizeExportJsonl:
    def test_summarizes_existing_export_file(self, tmp_path):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text(
            "\n".join(
                [
                    '{"project":"p1","model":"m1","stats":{"input_tokens":10,"output_tokens":3}}',
                    '{"project":"p2","model":"m1","stats":{"input_tokens":7,"output_tokens":1}}',
                    '{"project":"p1","model":"m2","stats":{"input_tokens":5,"output_tokens":2}}',
                ]
            )
            + "\n"
        )

        meta = summarize_export_jsonl(jsonl_path)

        assert meta["sessions"] == 3
        assert "models" not in meta
        assert meta["model_breakdown"] == {
            "m1": {"sessions": 2, "input_tokens": 17, "output_tokens": 4},
            "m2": {"sessions": 1, "input_tokens": 5, "output_tokens": 2},
        }
        assert "projects" not in meta
        assert meta["project_breakdown"] == {
            "p1": {"sessions": 2, "input_tokens": 15, "output_tokens": 5},
            "p2": {"sessions": 1, "input_tokens": 7, "output_tokens": 1},
        }
        assert meta["total_input_tokens"] == 22
        assert meta["total_output_tokens"] == 6

    def test_summarize_normalizes_model_and_project_keys(self, tmp_path):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text(
            "\n".join(
                [
                    '{"project":"codex:ComfyUI","model":"openai/gpt-5.4","stats":{"input_tokens":10,"output_tokens":3}}',
                    '{"project":"comfyui","model":"gpt-5.4","stats":{"input_tokens":20,"output_tokens":7}}',
                ]
            )
            + "\n"
        )

        meta = summarize_export_jsonl(jsonl_path)

        assert meta["model_breakdown"] == {"gpt-5-4": {"sessions": 2, "input_tokens": 30, "output_tokens": 10}}
        assert meta["project_breakdown"] == {"comfyui": {"sessions": 2, "input_tokens": 30, "output_tokens": 10}}
