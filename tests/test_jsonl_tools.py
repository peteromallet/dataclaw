"""Tests for JSONL formatting and diff helpers."""

from concurrent.futures import Future
from pathlib import Path

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

    def test_writes_lf_line_endings(self, tmp_path):
        input_path = tmp_path / "conversations.jsonl"
        input_path.write_text('{"text":"line 1\\nline 2"}\n', encoding="utf-8")

        output_path = jsonl_tools.jsonl_to_yaml_file(input_path)

        raw = output_path.read_bytes()
        assert b"\r\n" not in raw
        assert b"\n" in raw

    def test_resolve_diff_workers_uses_shared_env(self, monkeypatch):
        monkeypatch.setenv("DATACLAW_WORKERS", "3")

        assert jsonl_tools._resolve_diff_workers(10) == 3


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

    def test_matches_remove_add_messages_with_same_timestamp_and_diffs_content(self, monkeypatch):
        old_message = {
            "role": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "content": "old request",
        }
        new_message = {
            "role": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "content": "new request",
        }

        def fake_run_jd_patch(old_obj, new_obj):
            if old_obj == old_message and new_obj == new_message:
                return [{"op": "replace", "path": "/content", "old": "old request", "new": "new request"}]
            raise AssertionError("Unexpected jd patch request")

        monkeypatch.setattr("dataclaw.jsonl_tools.run_jd_patch", fake_run_jd_patch)

        result = jsonl_tools.simplify_patch_ops(
            [
                {"op": "remove", "path": "/messages/19", "value": old_message},
                {"op": "add", "path": "/messages/19", "value": new_message},
            ]
        )

        assert result == [{"op": "replace", "path": "/messages/19/content", "old": "old request", "new": "new request"}]

    def test_matches_remove_add_tool_uses_by_tool_and_structure(self, monkeypatch):
        old_tool_use = {
            "tool": "shell_command",
            "input": {
                "command": "sed -n '1,5p' file.py",
                "workdir": "/tmp/project",
            },
            "output": {
                "output": "old output",
                "exit_code": 0,
            },
            "status": "success",
        }
        new_tool_use = {
            "tool": "shell_command",
            "input": {
                "command": "sed -n '6,10p' file.py",
                "workdir": "/tmp/project",
            },
            "output": {
                "output": "new output",
                "exit_code": 0,
            },
            "status": "success",
        }

        def fake_run_jd_patch(old_obj, new_obj):
            if old_obj == old_tool_use and new_obj == new_tool_use:
                return [
                    {
                        "op": "replace",
                        "path": "/input/command",
                        "old": "sed -n '1,5p' file.py",
                        "new": "sed -n '6,10p' file.py",
                    },
                    {
                        "op": "replace",
                        "path": "/output/output",
                        "old": "old output",
                        "new": "new output",
                    },
                ]
            raise AssertionError("Unexpected jd patch request")

        monkeypatch.setattr("dataclaw.jsonl_tools.run_jd_patch", fake_run_jd_patch)

        result = jsonl_tools.simplify_patch_ops(
            [
                {"op": "remove", "path": "/messages/1/tool_uses/20", "value": old_tool_use},
                {"op": "add", "path": "/messages/1/tool_uses/20", "value": new_tool_use},
            ]
        )

        assert result == [
            {
                "op": "replace",
                "path": "/messages/1/tool_uses/20/input/command",
                "old": "sed -n '1,5p' file.py",
                "new": "sed -n '6,10p' file.py",
            },
            {
                "op": "replace",
                "path": "/messages/1/tool_uses/20/output/output",
                "old": "old output",
                "new": "new output",
            },
        ]


class TestBuildTextReplaceDiff:
    def test_limits_context_to_three_surrounding_lines(self):
        old = "\n".join(f"line {idx}" for idx in range(1, 11))
        new = old.replace("line 6", "updated line 6")

        diff = jsonl_tools.build_text_replace_diff(old, new)

        assert diff is not None
        assert "line 3" in diff
        assert "line 9" in diff
        assert "line 2" not in diff
        assert "line 10" not in diff
        assert "-line 6" in diff
        assert "+updated line 6" in diff


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

    def test_diff_output_uses_lf_line_endings(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"

        old_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"old"}]}\n',
            encoding="utf-8",
        )
        new_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"new"}]}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "dataclaw.jsonl_tools.run_jd_patch",
            lambda _old, _new: [{"op": "replace", "path": "/messages/0/content", "old": "old", "new": "new"}],
        )

        jsonl_tools.diff_jsonl_files(old_path, new_path, output_path)

        raw = output_path.read_bytes()
        assert b"\r\n" not in raw
        assert b"\n" in raw

    def test_localizes_large_blob_replacements(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"

        old_blob = "AbC123+/" * (2 * 1024 * 1024 // 8)
        new_blob = "XyZ987+/" * (2 * 1024 * 1024 // 8)
        old_path.write_text(
            '{"source":"gemini","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content_parts":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"'
            + old_blob
            + '"}}]}]}'
            + "\n",
            encoding="utf-8",
        )
        new_path.write_text(
            '{"source":"gemini","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content_parts":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"'
            + new_blob
            + '"}}]}]}'
            + "\n",
            encoding="utf-8",
        )

        captured = {}

        def fake_run_jd_patch(old_obj, new_obj):
            captured["old_obj"] = old_obj
            captured["new_obj"] = new_obj
            return [
                {
                    "op": "replace",
                    "path": "/messages/0/content_parts/0/source/data",
                    "old": old_obj["messages"][0]["content_parts"][0]["source"]["data"],
                    "new": new_obj["messages"][0]["content_parts"][0]["source"]["data"],
                }
            ]

        monkeypatch.setattr("dataclaw.jsonl_tools.run_jd_patch", fake_run_jd_patch)

        result = jsonl_tools.diff_jsonl_files(old_path, new_path, output_path)

        assert result.event_count == 1
        docs = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
        patch = docs[1]["patch"]
        assert patch[0]["op"] == "replace_large_blob"
        assert patch[0]["path"] == "/messages/0/content_parts/0/source/data"
        assert patch[0]["old"] == {"type": "large_blob", "length": len(old_blob)}
        assert patch[0]["new"] == {"type": "large_blob", "length": len(new_blob)}
        assert captured["old_obj"]["messages"][0]["content_parts"][0]["source"]["data"].startswith(
            "__DATACLAW_LARGE_BLOB__:"
        )
        assert captured["new_obj"]["messages"][0]["content_parts"][0]["source"]["data"].startswith(
            "__DATACLAW_LARGE_BLOB__:"
        )

    def test_parallel_patch_generation_preserves_event_order(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"

        old_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"old-a"}]}\n'
            '{"source":"claude","project":"proj","session_id":"s2","start_time":"2026-01-01T00:00:01Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:01Z","content":"old-b"}]}\n',
            encoding="utf-8",
        )
        new_path.write_text(
            '{"source":"claude","project":"proj","session_id":"s1","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"new-a"}]}\n'
            '{"source":"claude","project":"proj","session_id":"s2","start_time":"2026-01-01T00:00:01Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:01Z","content":"new-b"}]}\n',
            encoding="utf-8",
        )

        captured = {}

        class FakeExecutor:
            def __init__(self, max_workers):
                captured["max_workers"] = max_workers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def map(self, fn, payloads):
                payload_list = list(payloads)
                captured["payloads"] = payload_list
                futures = []
                for payload in payload_list:
                    future = Future()
                    future.set_result(fn(payload))
                    futures.append(future)
                return [future.result() for future in reversed(futures)][::-1]

        def fake_run_jd_patch(old_obj, new_obj):
            return [
                {
                    "op": "replace",
                    "path": "/messages/0/content",
                    "old": old_obj["messages"][0]["content"],
                    "new": new_obj["messages"][0]["content"],
                }
            ]

        monkeypatch.setattr("dataclaw.jsonl_tools.ThreadPoolExecutor", FakeExecutor)
        monkeypatch.setattr("dataclaw.jsonl_tools.run_jd_patch", fake_run_jd_patch)

        result = jsonl_tools.diff_jsonl_files(old_path, new_path, output_path, workers=2)

        assert result.event_count == 2
        assert captured["max_workers"] == 2
        assert len(captured["payloads"]) == 2
        docs = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
        assert docs[1]["identity"]["session_id"] == "s1"
        assert docs[1]["patch"][0]["old"] == "old-a"
        assert docs[1]["patch"][0]["new"] == "new-a"
        assert docs[2]["identity"]["session_id"] == "s2"
        assert docs[2]["patch"][0]["old"] == "old-b"
        assert docs[2]["patch"][0]["new"] == "new-b"

    def test_handles_duplicate_identity_records_with_multiple_modifications(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"

        old_path.write_text(
            '{"source":"claude","project":"proj","session_id":"dup","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"old-a"}]}\n'
            '{"source":"claude","project":"proj","session_id":"dup","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"old-b"}]}\n',
            encoding="utf-8",
        )
        new_path.write_text(
            '{"source":"claude","project":"proj","session_id":"dup","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"new-a"}]}\n'
            '{"source":"claude","project":"proj","session_id":"dup","start_time":"2026-01-01T00:00:00Z","messages":[{"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":"new-b"}]}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "dataclaw.jsonl_tools.run_jd_patch",
            lambda _old, _new: [{"op": "replace", "path": "/messages/0/content", "old": "x", "new": "y"}],
        )

        result = jsonl_tools.diff_jsonl_files(old_path, new_path, output_path)

        assert result.event_count == 2
        docs = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
        assert docs[0]["summary"]["modified_records"] == 2
        assert docs[1]["old_line"] == 1
        assert docs[2]["old_line"] == 2

    def test_identical_files_do_not_reload_records(self, tmp_path, monkeypatch):
        old_path = tmp_path / "old.jsonl"
        new_path = tmp_path / "new.jsonl"
        output_path = tmp_path / "diff.yaml"
        payload = (
            '{"source":"claude","project":"proj","session_id":"same","start_time":"2026-01-01T00:00:00Z","messages":[]}'
            + "\n"
        )
        old_path.write_text(payload, encoding="utf-8")
        new_path.write_text(payload, encoding="utf-8")

        open_counts = {str(old_path): 0, str(new_path): 0}
        real_path_open = Path.open

        def counting_open(self, *args, **kwargs):
            key = str(self)
            if key in open_counts:
                open_counts[key] += 1
            return real_path_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)

        result = jsonl_tools.diff_jsonl_files(old_path, new_path, output_path)

        assert result.event_count == 0
        assert open_counts[str(old_path)] == 1
        assert open_counts[str(new_path)] == 1
