"""Tests for JSONL formatting and diff helpers."""

import yaml

from dataclaw import jsonl_tools


class TestJsonlToYamlFile:
    def test_renders_multiline_strings_with_block_style(self, tmp_path):
        input_path = tmp_path / "conversations.jsonl"
        input_path.write_text('{"text":"line 1\\nline 2"}\n', encoding="utf-8")

        output_path = jsonl_tools.jsonl_to_yaml_file(input_path)

        assert output_path == tmp_path / "conversations_formatted.yaml"
        content = output_path.read_text(encoding="utf-8")
        assert "text: |-" in content or "text: |" in content
        assert "line 1" in content
        assert "line 2" in content


class TestSimplifyPatchOps:
    def test_matches_remove_add_message_runs_and_diffs_inside(self, monkeypatch):
        old_message = {
            "role": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "tool_uses": [{"tool": "Read", "input": {"file_path": "/tmp/file.py"}, "output": {"text": "hi"}}],
        }
        new_message = {
            "role": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "tool_uses": [
                {
                    "tool": "Read",
                    "input": {"file_path": "/tmp/file.py"},
                    "output": {"text": "hi", "raw": {"type": "text"}},
                }
            ],
        }

        def fake_run_jd_patch(old_obj, new_obj):
            if old_obj == old_message and new_obj == new_message:
                return [{"op": "add", "path": "/tool_uses/0/output/raw", "value": {"type": "text"}}]
            raise AssertionError("Unexpected jd patch request")

        monkeypatch.setattr("dataclaw.jsonl_tools.run_jd_patch", fake_run_jd_patch)

        result = jsonl_tools.simplify_patch_ops(
            [
                {"op": "remove", "path": "/messages/0", "value": old_message},
                {"op": "add", "path": "/messages/0", "value": new_message},
            ]
        )

        assert result == [{"op": "add", "path": "/messages/0/tool_uses/0/output/raw", "value": {"type": "text"}}]


class TestDiffJsonlFiles:
    def test_writes_yaml_summary_and_patch(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"

        old_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","tool_uses":[{"tool":"Read","input":{"file_path":"/tmp/file.py"},"output":{"text":"hi"}}]}]}\n',
            encoding="utf-8",
        )
        new_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","tool_uses":[{"tool":"Read","input":{"file_path":"/tmp/file.py"},"output":{"text":"hi","raw":{"type":"text"}}}]}]}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "dataclaw.jsonl_tools.run_jd_patch",
            lambda _old, _new: [{"op": "add", "path": "/messages/0/tool_uses/0/output/raw", "value": {"type": "text"}}],
        )

        result = jsonl_tools.diff_jsonl_files(old_path, new_path, output_path)

        assert result.output_path == output_path
        assert result.event_count == 1
        docs = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
        assert docs[0]["summary"]["modified_records"] == 1
        assert docs[1]["patch"][0]["path"] == "/messages/0/tool_uses/0/output/raw"
