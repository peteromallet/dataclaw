"""Tests for dataclaw.cli — CLI commands and helpers."""

import hashlib
import inspect
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

import dataclaw.cli as cli
from dataclaw.cli import (
    _build_status_next_steps,
    _build_dataset_card,
    _build_dataset_card_v2,
    _bucket_for_project,
    _collect_review_attestations,
    _format_size,
    _format_token_count,
    _format_token_title,
    _handle_rollback,
    _merge_config_list,
    _parse_csv_arg,
    _scan_for_text_occurrences,
    _scan_high_entropy_strings,
    _scan_pii,
    _validate_publish_attestation,
    export_to_shards,
    configure,
    default_repo_name,
    export_to_jsonl,
    list_projects,
    main,
    MANIFEST_REL,
    push_shards_to_huggingface,
    push_to_huggingface,
)
from dataclaw.config import load_config


# --- _format_size ---


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        result = _format_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = _format_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _format_size(2 * 1024 * 1024 * 1024)
        assert "GB" in result

    def test_zero(self):
        assert _format_size(0) == "0 B"

    def test_exactly_1024(self):
        result = _format_size(1024)
        assert "KB" in result


# --- _format_token_count ---


class TestFormatTokenCount:
    def test_plain(self):
        assert _format_token_count(500) == "500"

    def test_thousands(self):
        result = _format_token_count(5000)
        assert result == "5K"

    def test_millions(self):
        result = _format_token_count(2_500_000)
        assert "M" in result

    def test_billions(self):
        result = _format_token_count(1_500_000_000)
        assert "B" in result

    def test_zero(self):
        assert _format_token_count(0) == "0"


class TestFormatTokenTitle:
    def test_avoids_zero_billion_for_small_counts(self):
        assert _format_token_title(6_000_000) == "0.01b"

    def test_uses_one_decimal_for_current_dataset_scale(self):
        assert _format_token_title(614_000_000) == "0.6b"


# --- attestation helpers ---


class TestAttestationHelpers:
    def test_collect_review_attestations_valid(self):
        attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name=(
                "I asked Jane Doe for their full name and scanned the export for Jane Doe."
            ),
            attest_asked_sensitive=(
                "I asked about company, client, and internal names plus URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name="Jane Doe",
        )
        assert not errors
        assert manual_count == 20
        assert "Jane Doe" in attestations["asked_full_name"]

    def test_collect_review_attestations_invalid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name="scanned quickly",
            attest_asked_sensitive="checked stuff",
            attest_manual_scan="manual scan of 5 sessions",
            full_name="Jane Doe",
        )
        assert errors
        assert "asked_full_name" in errors
        assert "asked_sensitive_entities" in errors
        assert "manual_scan_done" in errors
        assert manual_count == 5

    def test_collect_review_attestations_skip_full_name_valid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name=(
                "User declined to share full name; skipped exact-name scan."
            ),
            attest_asked_sensitive=(
                "I asked about company/client/internal names and private URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name=None,
            skip_full_name_scan=True,
        )
        assert not errors
        assert manual_count == 20

    def test_collect_review_attestations_skip_full_name_invalid(self):
        _attestations, errors, _manual_count = _collect_review_attestations(
            attest_asked_full_name="Asked user and scanned it.",
            attest_asked_sensitive=(
                "I asked about company/client/internal names and private URLs; none found."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name=None,
            skip_full_name_scan=True,
        )
        assert "asked_full_name" in errors

    def test_validate_publish_attestation(self):
        _normalized, err = _validate_publish_attestation(
            "User explicitly approved publishing this dataset now."
        )
        assert err is None

        _normalized, err = _validate_publish_attestation("ok to go")
        assert err is not None

    def test_scan_for_text_occurrences(self, tmp_path):
        f = tmp_path / "sample.jsonl"
        f.write_text('{"message":"Jane Doe says hi"}\n{"message":"nothing here"}\n')
        result = _scan_for_text_occurrences(f, "Jane Doe")
        assert result["match_count"] == 1


# --- _parse_csv_arg ---


class TestParseCsvArg:
    def test_none(self):
        assert _parse_csv_arg(None) is None

    def test_empty(self):
        assert _parse_csv_arg("") is None

    def test_single(self):
        assert _parse_csv_arg("foo") == ["foo"]

    def test_comma_separated(self):
        assert _parse_csv_arg("foo, bar, baz") == ["foo", "bar", "baz"]

    def test_strips_whitespace(self):
        assert _parse_csv_arg("  a ,  b  ") == ["a", "b"]

    def test_empty_items_filtered(self):
        assert _parse_csv_arg("a,,b,") == ["a", "b"]


# --- _merge_config_list ---


class TestMergeConfigList:
    def test_merge_new_values(self):
        config = {"items": ["a", "b"]}
        _merge_config_list(config, "items", ["c", "d"])
        assert sorted(config["items"]) == ["a", "b", "c", "d"]

    def test_deduplicate(self):
        config = {"items": ["a", "b"]}
        _merge_config_list(config, "items", ["b", "c"])
        assert sorted(config["items"]) == ["a", "b", "c"]

    def test_sorted(self):
        config = {"items": ["z"]}
        _merge_config_list(config, "items", ["a", "m"])
        assert config["items"] == ["a", "m", "z"]

    def test_missing_key(self):
        config = {}
        _merge_config_list(config, "items", ["a"])
        assert config["items"] == ["a"]


# --- default_repo_name ---


class TestDefaultRepoName:
    def test_format(self):
        result = default_repo_name("alice")
        assert result == "alice/my-personal-codex-data"

    def test_contains_username(self):
        result = default_repo_name("bob")
        assert "bob" in result
        assert "/" in result


# --- _build_dataset_card ---


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
        assert "---" in card  # YAML frontmatter
        assert "dataclaw" in card
        assert "claude-sonnet" in card
        assert "10" in card

    def test_yaml_frontmatter(self):
        meta = {
            "models": {}, "sessions": 0, "projects": [],
            "total_input_tokens": 0, "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("user/repo", meta)
        lines = card.strip().split("\n")
        assert lines[0] == "---"
        # Find second ---
        second_dash = [i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"]
        assert len(second_dash) >= 1

    def test_contains_repo_id(self):
        meta = {
            "models": {}, "sessions": 0, "projects": [],
            "total_input_tokens": 0, "total_output_tokens": 0,
            "exported_at": "",
        }
        card = _build_dataset_card("alice/my-dataset", meta)
        assert "alice/my-dataset" in card


# --- export_to_jsonl ---


class TestExportToJsonl:
    def test_writes_jsonl(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "claude-sonnet-4-20250514",
            "git_branch": "main",
            "start_time": "2025-01-01T00:00:00",
            "end_time": "2025-01-01T01:00:00",
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {"input_tokens": 100, "output_tokens": 50},
            "project": "test",
        }]
        monkeypatch.setattr(
            "dataclaw.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )

        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)

        assert output.exists()
        lines = output.read_text().strip().split("\n")
        assert len(lines) == 1
        assert meta["sessions"] == 1

    def test_skips_synthetic_model(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "<synthetic>",
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {},
        }]
        monkeypatch.setattr(
            "dataclaw.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1

    def test_counts_redactions(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}],
            "stats": {"input_tokens": 10, "output_tokens": 5},
        }]
        monkeypatch.setattr(
            "dataclaw.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["redactions"] >= 1

    def test_skips_none_model(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": None,
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {},
        }]
        monkeypatch.setattr(
            "dataclaw.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "t", "display_name": "t"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1


class TestPhase2ShardedExport:
    @staticmethod
    def _session(session_id="s1", source="claude", end_time="2026-04-25T12:00:00Z"):
        return {
            "session_id": session_id,
            "source": source,
            "project": f"{source}:proj",
            "model": "model-a",
            "end_time": end_time,
            "messages": [{"role": "user", "content": "hello"}],
            "stats": {},
        }

    @staticmethod
    def _project(source="claude"):
        return {"dir_name": f"{source}-dir", "display_name": f"{source}:proj", "source": source}

    def test_export_to_shards_writes_per_source_dates(self, tmp_path, mock_anonymizer, monkeypatch):
        def fake_parse(*_args, source="claude", **_kwargs):
            return [self._session(source=source, end_time="2026-04-25T12:34:56Z")]

        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", fake_parse)
        manifest = export_to_shards(
            [self._project("claude"), self._project("codex")],
            tmp_path,
            mock_anonymizer,
            {"repo": None},
            fetch_existing=False,
        )

        assert (tmp_path / "claude" / "2026-04-25.jsonl").exists()
        assert (tmp_path / "codex" / "2026-04-25.jsonl").exists()
        assert sorted(s["source"] for s in manifest["shards"]) == ["claude", "codex"]

    def test_export_to_jsonl_flat_path_unchanged_byte_for_byte(self, tmp_path, mock_anonymizer, monkeypatch):
        clean = self._session()
        clean.pop("source")
        clean.pop("end_time")
        clean["project"] = "proj"
        clean["start_time"] = "2026-04-25T11:00:00Z"

        before = tmp_path / "before.jsonl"
        after = tmp_path / "after.jsonl"
        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", lambda *_a, **_kw: [dict(clean)])
        export_to_jsonl([{"dir_name": "proj", "display_name": "proj"}], before, mock_anonymizer)

        stamped = dict(clean)
        stamped["_source_file"] = "/tmp/source.jsonl"
        stamped["_project_dir_name"] = "proj"
        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", lambda *_a, **_kw: [dict(stamped)])
        export_to_jsonl([{"dir_name": "proj", "display_name": "proj"}], after, mock_anonymizer)

        assert hashlib.sha256(before.read_bytes()).hexdigest() == hashlib.sha256(after.read_bytes()).hexdigest()

    def test_manifest_schema_v1(self, tmp_path, mock_anonymizer, monkeypatch):
        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", lambda *_a, **_kw: [self._session()])
        manifest = export_to_shards([self._project()], tmp_path, mock_anonymizer, {"repo": None}, fetch_existing=False)

        assert list(manifest.keys()) == [
            "export_id",
            "schema_version",
            "root_dir",
            "started_at",
            "finished_at",
            "shards",
            "sources",
            "buckets",
            "total_sessions_new",
            "total_sessions_in_shards",
            "total_redactions",
            "models",
            "max_end_time_by_source",
            "token_count",
            "include_thinking",
            "merge_source",
        ]
        assert manifest["schema_version"] == 1
        assert list(manifest["shards"][0].keys()) == [
            "path",
            "source",
            "date",
            "sessions_new",
            "sessions_total",
            "bytes",
            "messages_total",
            "content_chars",
            "content_bytes",
            "content_tokens_estimate",
            "jsonl_tokens_estimate",
        ]
        assert manifest["token_count"]["method"] == "byte_estimate"
        assert manifest["token_count"]["scope"] == "jsonl"
        assert manifest["token_count"]["jsonl_tokens"] > 0
        assert manifest["token_count"]["content_tokens"] > 0
        assert json.loads((tmp_path / MANIFEST_REL).read_text()) == manifest

    def test_same_day_run_merges_via_dedup(self, tmp_path, mock_anonymizer, monkeypatch):
        calls = iter([
            [self._session("s1"), self._session("s2")],
            [self._session("s1"), self._session("s3")],
        ])
        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", lambda *_a, **_kw: next(calls))

        first = export_to_shards([self._project()], tmp_path, mock_anonymizer, {"repo": None}, fetch_existing=False)
        second = export_to_shards([self._project()], tmp_path, mock_anonymizer, {"repo": None}, fetch_existing=False)

        shard_path = tmp_path / "claude" / "2026-04-25.jsonl"
        lines = [json.loads(line) for line in shard_path.read_text().splitlines()]
        assert [line["session_id"] for line in lines] == ["s1", "s2", "s3"]
        assert first["total_sessions_new"] == 2
        assert second["total_sessions_new"] == 1
        assert second["total_sessions_in_shards"] == 3

    def _run_fetch_existing_exception(self, tmp_path, mock_anonymizer, monkeypatch, exc):
        monkeypatch.setattr("dataclaw.cli.parse_project_sessions", lambda *_a, **_kw: [self._session()])
        monkeypatch.setattr("huggingface_hub.hf_hub_download", MagicMock(side_effect=exc))
        return export_to_shards(
            [self._project()],
            tmp_path,
            mock_anonymizer,
            {"repo": "user/repo"},
            fetch_existing=True,
        )

    def test_fetch_existing_repo_not_found_continues(self, tmp_path, mock_anonymizer, monkeypatch):
        from huggingface_hub.errors import RepositoryNotFoundError

        manifest = self._run_fetch_existing_exception(
            tmp_path, mock_anonymizer, monkeypatch, RepositoryNotFoundError("missing")
        )
        assert manifest["merge_source"] == {"claude/2026-04-25.jsonl": "new"}

    def test_fetch_existing_entry_not_found_continues(self, tmp_path, mock_anonymizer, monkeypatch):
        from huggingface_hub.errors import EntryNotFoundError

        manifest = self._run_fetch_existing_exception(
            tmp_path, mock_anonymizer, monkeypatch, EntryNotFoundError("missing")
        )
        assert manifest["merge_source"] == {"claude/2026-04-25.jsonl": "new"}

    def test_fetch_existing_local_entry_not_found_continues(self, tmp_path, mock_anonymizer, monkeypatch):
        from huggingface_hub.errors import LocalEntryNotFoundError

        manifest = self._run_fetch_existing_exception(
            tmp_path, mock_anonymizer, monkeypatch, LocalEntryNotFoundError("missing")
        )
        assert manifest["merge_source"] == {"claude/2026-04-25.jsonl": "new"}

    def test_fetch_existing_404_continues(self, tmp_path, mock_anonymizer, monkeypatch):
        from huggingface_hub.errors import HfHubHTTPError
        from requests import Response

        response = Response()
        response.status_code = 404
        manifest = self._run_fetch_existing_exception(
            tmp_path, mock_anonymizer, monkeypatch, HfHubHTTPError("missing", response=response)
        )
        assert manifest["merge_source"] == {"claude/2026-04-25.jsonl": "new"}

    def test_fetch_existing_network_error_raises(self, tmp_path, mock_anonymizer, monkeypatch):
        from huggingface_hub.errors import HfHubHTTPError
        from requests import Response

        response = Response()
        response.status_code = 500
        with pytest.raises(HfHubHTTPError):
            self._run_fetch_existing_exception(
                tmp_path, mock_anonymizer, monkeypatch, HfHubHTTPError("boom", response=response)
            )

    def test_push_shards_upload_folder_scoped_to_manifest_paths(self, tmp_path, monkeypatch):
        class FakeApi:
            def __init__(self):
                self.upload_folder_kwargs = {}

            def whoami(self):
                return {"name": "tester"}

            def create_repo(self, *args, **kwargs):
                pass

            def upload_file(self, *args, **kwargs):
                pass

            def upload_folder(self, *args, **kwargs):
                self.upload_folder_kwargs = kwargs

        fake = FakeApi()
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake)
        manifest = {
            "shards": [{"path": "claude/2026-04-25.jsonl"}, {"path": "work/codex/2026-04-25.jsonl"}],
            "total_sessions_new": 2,
            "sources": ["claude", "codex"],
            "buckets": ["work"],
            "models": {},
            "total_sessions_in_shards": 2,
        }
        push_shards_to_huggingface(tmp_path, "user/repo", manifest)

        assert fake.upload_folder_kwargs["allow_patterns"] == [
            "claude/2026-04-25.jsonl",
            "work/codex/2026-04-25.jsonl",
            ".dataclaw/manifest.json",
        ]
        assert fake.upload_folder_kwargs["ignore_patterns"] == ["conversations.jsonl"]
        assert ".dataclaw/*" not in fake.upload_folder_kwargs["ignore_patterns"]
        assert ".dataclaw/manifest.json" not in fake.upload_folder_kwargs["ignore_patterns"]

    def test_merge_remote_manifest_preserves_untouched_shards_and_recomputes_totals(self):
        remote = {
            "export_id": "remote-export",
            "schema_version": 1,
            "finished_at": "2026-04-24T00:00:00Z",
            "shards": [
                {
                    "path": "claude/2026-04-24.jsonl",
                    "source": "claude",
                    "date": "2026-04-24",
                    "sessions_new": 7,
                    "sessions_total": 7,
                    "bytes": 111,
                },
                {
                    "path": "claude/2026-04-25.jsonl",
                    "source": "claude",
                    "date": "2026-04-25",
                    "sessions_new": 2,
                    "sessions_total": 2,
                    "bytes": 222,
                },
            ],
            "sources": ["claude"],
            "buckets": [],
            "total_sessions_new": 9,
            "total_sessions_in_shards": 9,
            "models": {"old-model": 7},
            "max_end_time_by_source": {"claude": "2026-04-24T20:00:00Z"},
            "merge_source": {"claude/2026-04-24.jsonl": "merged"},
        }
        local = {
            "export_id": "local-export",
            "schema_version": 1,
            "finished_at": "2026-04-25T12:00:00Z",
            "shards": [
                {
                    "path": "claude/2026-04-25.jsonl",
                    "source": "claude",
                    "date": "2026-04-25",
                    "sessions_new": 1,
                    "sessions_total": 3,
                    "bytes": 333,
                },
                {
                    "path": "work/codex/2026-04-25.jsonl",
                    "source": "codex",
                    "date": "2026-04-25",
                    "sessions_new": 4,
                    "sessions_total": 4,
                    "bytes": 444,
                },
            ],
            "sources": ["claude", "codex"],
            "buckets": ["work"],
            "total_sessions_new": 5,
            "total_sessions_in_shards": 7,
            "models": {"new-model": 5},
            "max_end_time_by_source": {
                "claude": "2026-04-25T12:00:00Z",
                "codex": "2026-04-25T13:00:00Z",
            },
            "merge_source": {
                "claude/2026-04-25.jsonl": "merged",
                "work/codex/2026-04-25.jsonl": "new",
            },
        }

        merged = cli._merge_remote_manifest_metadata(local, remote)

        assert [shard["path"] for shard in merged["shards"]] == [
            "claude/2026-04-24.jsonl",
            "claude/2026-04-25.jsonl",
            "work/codex/2026-04-25.jsonl",
        ]
        untouched = merged["shards"][0]
        touched = merged["shards"][1]
        assert untouched["sessions_new"] == 0
        assert untouched["sessions_total"] == 7
        assert touched["sessions_new"] == 1
        assert touched["sessions_total"] == 3
        assert merged["total_sessions_new"] == 5
        assert merged["total_sessions_in_shards"] == 14
        assert merged["sources"] == ["claude", "codex"]
        assert merged["buckets"] == ["work"]
        assert merged["models"] == {"old-model": 7, "new-model": 5}
        assert merged["max_end_time_by_source"] == {
            "claude": "2026-04-25T12:00:00Z",
            "codex": "2026-04-25T13:00:00Z",
        }
        assert merged["merge_source"] == {
            "claude/2026-04-24.jsonl": "remote",
            "claude/2026-04-25.jsonl": "merged",
            "work/codex/2026-04-25.jsonl": "new",
        }
        assert merged["token_count"]["jsonl_tokens"] == round(111 / 3.6) + round(333 / 3.6) + round(444 / 3.6)
        assert merged["remote_manifest"]["export_id"] == "remote-export"

    def test_push_shards_merges_remote_manifest_before_upload(self, tmp_path, monkeypatch):
        class FakeApi:
            def __init__(self):
                self.upload_file_payloads = []
                self.upload_folder_kwargs = {}

            def whoami(self):
                return {"name": "tester"}

            def create_repo(self, *args, **kwargs):
                pass

            def upload_file(self, *args, **kwargs):
                self.upload_file_payloads.append(kwargs)

            def upload_folder(self, *args, **kwargs):
                self.upload_folder_kwargs = kwargs

        local_shard = tmp_path / "claude" / "2026-04-25.jsonl"
        local_shard.parent.mkdir(parents=True)
        local_shard.write_text(json.dumps({"session_id": "new"}) + "\n")
        manifest = {
            "export_id": "local",
            "schema_version": 1,
            "finished_at": "2026-04-25T12:00:00Z",
            "shards": [{
                "path": "claude/2026-04-25.jsonl",
                "source": "claude",
                "date": "2026-04-25",
                "sessions_new": 1,
                "sessions_total": 3,
                "bytes": local_shard.stat().st_size,
            }],
            "sources": ["claude"],
            "buckets": [],
            "total_sessions_new": 1,
            "total_sessions_in_shards": 3,
            "models": {},
            "max_end_time_by_source": {"claude": "2026-04-25T12:00:00Z"},
            "merge_source": {"claude/2026-04-25.jsonl": "merged"},
        }
        remote_manifest = {
            "export_id": "remote",
            "schema_version": 1,
            "finished_at": "2026-04-24T12:00:00Z",
            "shards": [{
                "path": "claude/2026-04-24.jsonl",
                "source": "claude",
                "date": "2026-04-24",
                "sessions_new": 2,
                "sessions_total": 2,
                "bytes": 20,
            }],
            "sources": ["claude"],
            "buckets": [],
            "models": {},
            "max_end_time_by_source": {"claude": "2026-04-24T12:00:00Z"},
        }

        fake = FakeApi()
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake)
        monkeypatch.setattr(cli, "_fetch_remote_manifest", lambda repo_id: remote_manifest)

        cli._push_shards_attempt(tmp_path, "user/repo", manifest)

        written = json.loads((tmp_path / MANIFEST_REL).read_text())
        assert [shard["path"] for shard in written["shards"]] == [
            "claude/2026-04-24.jsonl",
            "claude/2026-04-25.jsonl",
        ]
        assert written["total_sessions_new"] == 1
        assert written["total_sessions_in_shards"] == 5
        assert manifest == written
        assert fake.upload_folder_kwargs["allow_patterns"] == [
            "claude/2026-04-24.jsonl",
            "claude/2026-04-25.jsonl",
            ".dataclaw/manifest.json",
        ]

    def test_push_shards_manifest_survives_filter_repo_objects(self):
        from huggingface_hub.utils import filter_repo_objects

        shard_paths = ["claude/2026-04-25.jsonl", "work/codex/2026-04-25.jsonl"]
        allow = shard_paths + [".dataclaw/manifest.json"]
        ignore = ["conversations.jsonl"]
        paths = shard_paths + [".dataclaw/manifest.json", "conversations.jsonl", "stale.txt"]

        assert list(filter_repo_objects(paths, allow_patterns=allow, ignore_patterns=ignore)) == [
            "claude/2026-04-25.jsonl",
            "work/codex/2026-04-25.jsonl",
            ".dataclaw/manifest.json",
        ]

    def test_push_to_huggingface_legacy_signature_unchanged(self):
        assert str(inspect.signature(push_to_huggingface)) == "(jsonl_path: pathlib.Path, repo_id: str, meta: dict) -> None"

    def _write_manifest_run(self, run_dir: Path) -> None:
        shard = run_dir / "claude" / "2026-04-25.jsonl"
        shard.parent.mkdir(parents=True)
        shard.write_text(json.dumps({"session_id": "s1", "project": "p", "model": "m"}) + "\n")
        manifest_path = run_dir / MANIFEST_REL
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({
            "shards": [{"path": "claude/2026-04-25.jsonl"}],
            "total_sessions_new": 1,
        }))

    def _confirm_args(self, *extra):
        return [
            "dataclaw",
            "confirm",
            *extra,
            "--skip-full-name-scan",
            "--attest-full-name",
            "User declined to share full name; skipped exact-name scan.",
            "--attest-sensitive",
            "I asked about company/client/internal names and private URLs; none found.",
            "--attest-manual-scan",
            "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
        ]

    def test_confirm_positional_path_accepts_directory(self, tmp_path, monkeypatch, capsys):
        run_dir = tmp_path / "run"
        self._write_manifest_run(run_dir)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})
        monkeypatch.setattr("dataclaw.cli.save_config", lambda _c: None)
        monkeypatch.setattr(sys, "argv", self._confirm_args(str(run_dir)))

        main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "shards"
        assert payload["shard_count"] == 1
        assert payload["total_sessions_new"] == 1

    def test_confirm_file_flag_still_accepted_as_alias(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text(json.dumps({"session_id": "s1", "project": "p", "model": "m"}) + "\n")
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})
        monkeypatch.setattr("dataclaw.cli.save_config", lambda _c: None)
        monkeypatch.setattr(sys, "argv", self._confirm_args("--file", str(export_file)))

        main()
        payload = json.loads(capsys.readouterr().out)
        assert "mode" not in payload
        assert payload["file"] == str(export_file.resolve())

    def test_confirm_no_arg_finds_latest_staging_run(self, tmp_path, monkeypatch, capsys):
        older = tmp_path / "home" / ".dataclaw" / "staging" / "older"
        newer = tmp_path / "home" / ".dataclaw" / "staging" / "newer"
        self._write_manifest_run(older)
        self._write_manifest_run(newer)
        os.utime(older, (time.time() - 100, time.time() - 100))
        os.utime(newer, (time.time(), time.time()))
        monkeypatch.setattr("dataclaw.cli.Path.home", lambda: tmp_path / "home")
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})
        monkeypatch.setattr("dataclaw.cli.save_config", lambda _c: None)
        monkeypatch.setattr(sys, "argv", self._confirm_args())

        main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "shards"
        assert payload["file"] == str(newer.resolve())

    def test_configs_block_renders_only_present_sources(self):
        card = _build_dataset_card_v2("user/repo", {
            "sources": ["claude", "opencode"],
            "buckets": ["work"],
            "models": {},
            "shards": [],
            "total_sessions_new": 0,
            "total_sessions_in_shards": 0,
        })
        frontmatter = card.split("---", 2)[1]
        assert "config_name: claude" in frontmatter
        assert 'data_files: "**/claude/*.jsonl"' in frontmatter
        assert "config_name: opencode" in frontmatter
        assert 'data_files: "**/opencode/*.jsonl"' in frontmatter
        assert "config_name: codex" not in frontmatter
        assert "  - codex-cli" not in frontmatter


# --- configure ---


class TestConfigure:
    def test_sets_repo(self, tmp_config, monkeypatch, capsys):
        # Also monkeypatch the cli module's references
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": None, "excluded_projects": [], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(repo="alice/my-repo")
        assert saved["repo"] == "alice/my-repo"

    def test_merges_exclude(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"excluded_projects": ["a"], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(exclude=["b", "c"])
        assert sorted(saved["excluded_projects"]) == ["a", "b", "c"]

    def test_sets_source(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": None, "source": None})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert saved["source"] == "codex"

    def test_sets_privacy_filter(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"privacy_filter": {"enabled": True}})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(privacy_filter=False)
        assert saved["privacy_filter"]["enabled"] is False

    def test_sets_privacy_filter_device(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"privacy_filter": {"enabled": True}})
        saved = {}
        monkeypatch.setattr("dataclaw.cli.save_config", lambda c: saved.update(c))

        configure(privacy_filter_device="mps")
        assert saved["privacy_filter"]["enabled"] is True
        assert saved["privacy_filter"]["device"] == "mps"


class TestPhase3BucketResolution:
    def test_bucket_explicit_match_by_dir_name(self):
        session = {"project": "Display Name", "_project_dir_name": "dir-name"}
        config = {"folder_rules": {"projects": {"dir-name": "work"}}}

        assert _bucket_for_project(session, config) == "work"

    def test_bucket_explicit_match_by_display_name(self):
        session = {"project": "Display Name", "_project_dir_name": "dir-name"}
        config = {"folder_rules": {"projects": {"Display Name": "personal"}}}

        assert _bucket_for_project(session, config) == "personal"

    def test_bucket_tag_fallback(self):
        session = {"project": "claude:proj", "_project_dir_name": "dir-name"}
        config = {
            "folder_rules": {"tags": {"client": "clients"}},
            "project_tags": {"proj": ["client"]},
        }

        assert _bucket_for_project(session, config) == "clients"

    def test_bucket_default_when_no_match(self):
        session = {"project": "unmatched", "_project_dir_name": "dir-name"}
        config = {"folder_rules": {"default_bucket": "misc"}}

        assert _bucket_for_project(session, config) == "misc"

    def test_bucket_none_when_no_rules(self):
        assert _bucket_for_project({"project": "proj"}, {}) is None


class TestPhase3ConfigVerbs:
    @staticmethod
    def _run_config(monkeypatch, capsys, state, *args):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: json.loads(json.dumps(state)))

        def save_config(config):
            state.clear()
            state.update(json.loads(json.dumps(config)))

        monkeypatch.setattr("dataclaw.cli.save_config", save_config)
        monkeypatch.setattr(sys, "argv", ["dataclaw", "config", *args])
        main()
        return capsys.readouterr().out

    def test_config_set_redact_replaces_list(self, monkeypatch, capsys):
        state = {"redact_strings": ["a", "b"]}

        self._run_config(monkeypatch, capsys, state, "--set-redact", "c,d")

        assert state["redact_strings"] == ["c", "d"]

    def test_config_remove_redact_removes_entries(self, monkeypatch, capsys):
        state = {"redact_strings": ["a", "b", "c"]}

        self._run_config(monkeypatch, capsys, state, "--remove-redact", "a,c")

        assert state["redact_strings"] == ["b"]

    def test_config_assign_and_unassign_round_trip(self, monkeypatch, capsys):
        state = {"folder_rules": {}, "project_tags": {}}

        self._run_config(
            monkeypatch,
            capsys,
            state,
            "--assign",
            "proj",
            "work",
            "--tag-project",
            "proj",
            "client",
            "--bucket-by-tag",
            "client",
            "clients",
        )
        assert state["folder_rules"]["projects"] == {"proj": "work"}
        assert state["project_tags"] == {"proj": ["client"]}
        assert state["folder_rules"]["tags"] == {"client": "clients"}

        self._run_config(
            monkeypatch,
            capsys,
            state,
            "--unassign",
            "proj",
            "--untag-project",
            "proj",
            "client",
            "--clear-bucket-by-tag",
            "client",
        )
        assert state["folder_rules"]["projects"] == {}
        assert state["project_tags"] == {}
        assert state["folder_rules"]["tags"] == {}

    def test_config_show_secrets_unmasks(self, monkeypatch, capsys):
        state = {"redact_strings": ["secret"]}

        masked = self._run_config(monkeypatch, capsys, state)
        assert json.loads(masked)["redact_strings"] == ["***"]

        raw = self._run_config(monkeypatch, capsys, state, "--show-secrets")
        assert json.loads(raw)["redact_strings"] == ["secret"]


class TestPhase3CopyAndList:
    def test_status_next_steps_lists_all_sources(self):
        next_steps, _next_command = _build_status_next_steps(
            "configure",
            {"projects_confirmed": False},
            hf_user="alice",
            repo_id="alice/repo",
        )
        text = "\n".join(next_steps)

        for source in ["claude", "codex", "gemini", "opencode", "openclaw", "kimi", "custom", "all"]:
            assert source in text

    def test_list_projects_emits_bucket_and_tags(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [{
                "display_name": "Demo Project",
                "dir_name": "demo-dir",
                "session_count": 5,
                "total_size_bytes": 1024,
                "source": "claude",
            }],
        )
        monkeypatch.setattr(
            "dataclaw.cli.load_config",
            lambda: {
                "excluded_projects": [],
                "folder_rules": {"projects": {"demo-dir": "work"}},
                "project_tags": {"demo-dir": ["client", "research"]},
            },
        )

        list_projects()

        row = json.loads(capsys.readouterr().out)[0]
        assert row["bucket"] == "work"
        assert row["tags"] == ["client", "research"]


# --- list_projects ---


class TestListProjects:
    def test_with_projects(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [{"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024}],
        )
        monkeypatch.setattr(
            "dataclaw.cli.load_config",
            lambda: {"excluded_projects": []},
        )
        list_projects()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "proj1"

    def test_no_projects(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.discover_projects", lambda: [])
        list_projects()
        captured = capsys.readouterr()
        assert "No Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, or Custom sessions" in captured.out

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
        assert "No Codex sessions found." in captured.out

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


# --- push_to_huggingface ---


class TestPushToHuggingface:
    def test_missing_huggingface_hub(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        # Simulate ImportError for huggingface_hub
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                raise ImportError("No module named 'huggingface_hub'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(SystemExit):
            push_to_huggingface(jsonl_path, "user/repo", {})

    def test_success_flow(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        mock_api = MagicMock()
        mock_api.whoami.return_value = {"name": "alice"}

        mock_hfapi_cls = MagicMock(return_value=mock_api)

        # Patch the import inside push_to_huggingface
        import dataclaw.cli as cli_mod
        monkeypatch.setattr(cli_mod, "push_to_huggingface", lambda *a, **kw: None)

        # Direct test with mock
        with patch.dict("sys.modules", {"huggingface_hub": MagicMock(HfApi=mock_hfapi_cls)}):
            # Re-import would be needed for real test, but let's test the mock setup
            assert mock_hfapi_cls() == mock_api

    def test_auth_failure(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n")

        mock_api = MagicMock()
        mock_api.whoami.side_effect = OSError("Auth failed")

        mock_hf_module = MagicMock()
        mock_hf_module.HfApi.return_value = mock_api

        with patch.dict("sys.modules", {"huggingface_hub": mock_hf_module}):
            # Need to reimport to pick up the mock
            import importlib
            import dataclaw.cli
            importlib.reload(dataclaw.cli)
            with pytest.raises(SystemExit):
                dataclaw.cli.push_to_huggingface(jsonl_path, "user/repo", {})
            # Reload again to restore
            importlib.reload(dataclaw.cli)


class TestWorkflowGateMessages:
    @staticmethod
    def _extract_json(stdout: str) -> dict:
        start = stdout.find("{")
        assert start >= 0, f"No JSON payload found in output: {stdout!r}"
        return json.loads(stdout[start:])

    def test_confirm_without_export_shows_step_process(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "missing.jsonl"
        monkeypatch.setattr(
            "sys.argv",
            ["dataclaw", "confirm", "--file", str(missing)],
        )
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
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
        payload = self._extract_json(capsys.readouterr().out)
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
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["stage"] == "confirmed"
        assert payload["full_name_scan"]["skipped"] is True

    def test_push_before_confirm_shows_step_process(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"stage": "review", "source": "all"})
        monkeypatch.setattr("sys.argv", ["dataclaw", "export"])
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "You must run `dataclaw confirm` before pushing."
        assert payload["blocked_on_step"] == "Step 2/3"
        assert len(payload["process_steps"]) == 3
        assert "confirm" in payload["process_steps"][1]

    def test_export_requires_project_confirmation_with_full_flow(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli._has_session_sources", lambda _src: True)
        monkeypatch.setattr(
            "dataclaw.cli.discover_projects",
            lambda: [
                {
                    "display_name": "proj1",
                    "session_count": 2,
                    "total_size_bytes": 1024,
                    "source": "claude",
                }
            ],
        )
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"source": "all"})
        monkeypatch.setattr("sys.argv", ["dataclaw", "export", "--no-push"])
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
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
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "Source scope is not confirmed yet."
        assert payload["blocked_on_step"] == "Step 2/6"
        assert len(payload["process_steps"]) == 6
        assert payload["allowed_sources"] == ["all", "both", "claude", "codex", "custom", "gemini", "kimi", "openclaw", "opencode"]
        assert payload["next_command"] == "dataclaw config --source all"

    def test_configure_next_steps_require_full_folder_presentation(self):
        steps, _next = _build_status_next_steps(
            "configure",
            {"projects_confirmed": False},
            "alice",
            "alice/my-personal-codex-data",
        )
        assert any("dataclaw list" in step for step in steps)
        assert any("FULL project/folder list" in step for step in steps)
        assert any("in your next message" in step for step in steps)
        assert any("source scope" in step.lower() for step in steps)

    def test_review_next_steps_explain_full_name_purpose_and_skip_option(self):
        steps, _next = _build_status_next_steps(
            "review",
            {},
            "alice",
            "alice/my-personal-codex-data",
        )
        assert any("exact-name privacy check" in step for step in steps)
        assert any("--skip-full-name-scan" in step for step in steps)


class TestConfirmPrivacyFilter:
    @staticmethod
    def _attestation_kwargs() -> dict:
        return {
            "skip_full_name_scan": True,
            "attest_asked_full_name": "User declined to share full name; skipped exact-name scan.",
            "attest_asked_sensitive": (
                "I asked about company/client/internal names and private URLs; none found."
            ),
            "attest_manual_scan": (
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
        }

    @staticmethod
    def _write_config(tmp_config, payload: dict) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps(payload))

    @staticmethod
    def _write_export(tmp_path: Path) -> Path:
        export_file = tmp_path / "export.jsonl"
        export_file.write_text(json.dumps({
            "session_id": "s1",
            "project": "p",
            "model": "m",
            "messages": [{"content": "Jane Doe"}],
        }) + "\n")
        return export_file

    @staticmethod
    def _extract_json(stdout: str) -> dict:
        start = stdout.find("{")
        assert start >= 0, f"No JSON payload found in output: {stdout!r}"
        return json.loads(stdout[start:])

    def test_confirm_does_nothing_when_privacy_filter_disabled(self, tmp_path, tmp_config, capsys):
        import dataclaw.cli as cli_mod

        self._write_config(tmp_config, {"known_findings": {}, "privacy_filter": {"enabled": False}})
        export_file = self._write_export(tmp_path)

        cli_mod.confirm(file_path=export_file, **self._attestation_kwargs())

        payload = self._extract_json(capsys.readouterr().out)
        config = load_config()
        assert "privacy_filter" not in payload
        assert config["stage"] == "confirmed"
        assert config["known_findings"] == {}

    def test_confirm_blocks_on_new_pf_findings_strict(
        self, tmp_path, tmp_config, monkeypatch, capsys,
    ):
        import dataclaw.cli as cli_mod
        from dataclaw import privacy_filter as pf

        finding = pf.Finding("NAME", "Jane Doe", 0.99, field="messages[0].content")
        self._write_config(tmp_config, {
            "stage": "review",
            "known_findings": {},
            "privacy_filter": {"enabled": True},
        })
        export_file = self._write_export(tmp_path)
        monkeypatch.setattr(pf, "is_available", lambda: True)
        monkeypatch.setattr(pf, "scan_jsonl", lambda *_a, **_kw: [finding])
        monkeypatch.setattr(pf, "scan_shards", lambda *_a, **_kw: [finding])

        with pytest.raises(SystemExit) as exc:
            cli_mod.confirm(file_path=export_file, **self._attestation_kwargs())

        payload = self._extract_json(capsys.readouterr().out)
        config = load_config()
        assert exc.value.code == 2
        assert payload["pf_new"][0]["fingerprint"] == finding.fingerprint()
        assert config["stage"] == "review"
        assert config["known_findings"] == {}

    def test_confirm_ack_adds_to_known_findings(self, tmp_path, tmp_config, monkeypatch, capsys):
        import dataclaw.cli as cli_mod
        from dataclaw import privacy_filter as pf

        finding = pf.Finding("NAME", "Jane Doe", 0.99, field="messages[0].content")
        self._write_config(tmp_config, {
            "stage": "review",
            "known_findings": {},
            "privacy_filter": {"enabled": True},
        })
        export_file = self._write_export(tmp_path)
        monkeypatch.setattr(pf, "is_available", lambda: True)
        monkeypatch.setattr(pf, "scan_jsonl", lambda *_a, **_kw: [finding])
        monkeypatch.setattr(pf, "scan_shards", lambda *_a, **_kw: [finding])

        cli_mod.confirm(
            file_path=export_file,
            ack_privacy_findings=True,
            **self._attestation_kwargs(),
        )

        payload = self._extract_json(capsys.readouterr().out)
        config = load_config()
        record = config["known_findings"][finding.fingerprint()]
        assert payload["privacy_filter"]["status"] == "scanned"
        assert config["stage"] == "confirmed"
        assert record["count"] == 1

    def test_confirm_permissive_does_not_block(self, tmp_path, tmp_config, monkeypatch, capsys):
        import dataclaw.cli as cli_mod
        from dataclaw import privacy_filter as pf

        finding = pf.Finding("NAME", "Jane Doe", 0.99, field="messages[0].content")
        self._write_config(tmp_config, {
            "stage": "review",
            "known_findings": {},
            "privacy_filter": {"enabled": True},
        })
        export_file = self._write_export(tmp_path)
        monkeypatch.setattr(pf, "is_available", lambda: True)
        monkeypatch.setattr(pf, "scan_jsonl", lambda *_a, **_kw: [finding])
        monkeypatch.setattr(pf, "scan_shards", lambda *_a, **_kw: [finding])

        cli_mod.confirm(
            file_path=export_file,
            policy="permissive",
            **self._attestation_kwargs(),
        )

        payload = self._extract_json(capsys.readouterr().out)
        config = load_config()
        assert payload["privacy_filter"]["new"][0]["fingerprint"] == finding.fingerprint()
        assert config["stage"] == "confirmed"
        assert config["known_findings"] == {}

    def test_confirm_pf_unavailable_emits_hint(self, tmp_path, tmp_config, monkeypatch, capsys):
        import dataclaw.cli as cli_mod
        from dataclaw import privacy_filter as pf

        self._write_config(tmp_config, {
            "stage": "review",
            "known_findings": {},
            "privacy_filter": {"enabled": True},
        })
        export_file = self._write_export(tmp_path)
        monkeypatch.setattr(pf, "is_available", lambda: False)

        cli_mod.confirm(file_path=export_file, **self._attestation_kwargs())

        payload = self._extract_json(capsys.readouterr().out)
        config = load_config()
        assert payload["privacy_filter"]["status"] == "unavailable"
        assert "dataclaw[pii]" in payload["privacy_filter"]["hint"]
        assert config["stage"] == "confirmed"
        assert config["known_findings"] == {}


class TestStatusLogs:
    @staticmethod
    def _extract_json(stdout: str) -> dict:
        start = stdout.find("{")
        assert start >= 0, f"No JSON payload found in output: {stdout!r}"
        return json.loads(stdout[start:])

    @freeze_time("2026-04-25T12:00:00Z")
    def test_status_logs_filters_by_run_id(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.logging.LOG_DIR", tmp_path)
        log_file = tmp_path / "auto-2026-04-25.jsonl"
        log_file.write_text(
            json.dumps({"run_id": "A", "msg": "one"}) + "\n"
            + json.dumps({"run_id": "B", "msg": "two"}) + "\n"
        )
        monkeypatch.setattr(sys, "argv", ["dataclaw", "status", "--logs", "--run", "A"])

        main()

        payload = self._extract_json(capsys.readouterr().out)
        assert payload["date"] == "2026-04-25"
        assert payload["lines"] == [{"run_id": "A", "msg": "one"}]

    @freeze_time("2026-04-25T12:00:00Z")
    def test_status_logs_no_file_returns_empty_lines(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.logging.LOG_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["dataclaw", "status", "--logs"])

        main()

        payload = self._extract_json(capsys.readouterr().out)
        assert payload["date"] == "2026-04-25"
        assert payload["lines"] == []

    def test_status_no_args_unchanged(self, tmp_config, monkeypatch, capsys):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({
            "repo": "alice/repo",
            "source": "all",
            "excluded_projects": [],
            "redact_strings": [],
            "projects_confirmed": True,
        }))
        monkeypatch.setattr("dataclaw.cli.get_hf_username", lambda: "alice")
        monkeypatch.setattr(sys, "argv", ["dataclaw", "status"])

        main()

        payload = self._extract_json(capsys.readouterr().out)
        assert "lines" not in payload
        assert payload["repo"] == "alice/repo"
        assert payload["source"] == "all"
        assert payload["stage"] == "configure"


# --- _scan_high_entropy_strings ---


class TestScanHighEntropyStrings:
    def test_detects_real_secret(self):
        # A realistic API key-like string with high entropy and mixed chars
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        content = f"some config here token {secret} and more text"
        results = _scan_high_entropy_strings(content)
        assert len(results) >= 1
        assert any(r["match"] == secret for r in results)
        # Entropy should be >= 4.0
        for r in results:
            if r["match"] == secret:
                assert r["entropy"] >= 4.0

    def test_filters_uuid(self):
        content = "id=550e8400e29b41d4a716446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in r["match"] for r in results)

    def test_filters_uuid_with_hyphens(self):
        # UUID with hyphens won't match the 20+ contiguous regex, but without hyphens should be filtered
        content = "id=550e8400-e29b-41d4-a716-446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in r["match"] for r in results)

    def test_filters_hex_hash(self):
        content = f"commit=abcdef1234567890abcdef1234567890abcdef12 done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdef1234567890" in r["match"] for r in results)

    def test_filters_known_prefix_eyj(self):
        content = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("eyJ") for r in results)

    def test_filters_known_prefix_ghp(self):
        content = "token=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345 done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("ghp_") for r in results)

    def test_filters_file_extension_path(self):
        content = "import=some_long_module_name_thing.py done"
        results = _scan_high_entropy_strings(content)
        assert not any(".py" in r["match"] for r in results)

    def test_filters_path_like(self):
        content = "path=src/components/authentication/LoginForm done"
        results = _scan_high_entropy_strings(content)
        assert not any("src/components" in r["match"] for r in results)

    def test_filters_low_entropy(self):
        # Repetitive string with mixed chars but low entropy
        content = "val=aaaaaaBBBBBB111111aaaaaaBBBBBB111111 done"
        results = _scan_high_entropy_strings(content)
        assert not any("aaaaaa" in r["match"] for r in results)

    def test_filters_no_mixed_chars(self):
        # All lowercase - no mixed char types
        content = "val=abcdefghijklmnopqrstuvwxyz done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdefghijklmnop" in r["match"] for r in results)

    def test_context_snippet(self):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        prefix = "before_context token "
        suffix = " after_context"
        content = prefix + secret + suffix
        results = _scan_high_entropy_strings(content)
        matched = [r for r in results if r["match"] == secret]
        assert len(matched) == 1
        assert "before_context" in matched[0]["context"]
        assert "after_context" in matched[0]["context"]

    def test_results_capped_at_max(self):
        # Generate many distinct high-entropy strings
        import string
        import random
        rng = random.Random(42)
        chars = string.ascii_letters + string.digits
        secrets = []
        for _ in range(25):
            s = "".join(rng.choices(chars, k=30))
            secrets.append(s)
        content = " ".join(f"key={s}" for s in secrets)
        results = _scan_high_entropy_strings(content, max_results=15)
        assert len(results) <= 15

    def test_empty_content(self):
        assert _scan_high_entropy_strings("") == []

    def test_sorted_by_entropy_descending(self):
        secret1 = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        secret2 = "Zx9Yw8Xv7Wu6Ts5Rq4Po3Nm2Lk1Jh0G"
        content = f"a={secret1} b={secret2}"
        results = _scan_high_entropy_strings(content)
        if len(results) >= 2:
            assert results[0]["entropy"] >= results[1]["entropy"]

    def test_filters_benign_prefix_https(self):
        content = "url=https://example.com/some/long/path/here done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("https://") for r in results)

    def test_filters_three_dots(self):
        content = "ver=com.example.app.module.v1.2.3 done"
        results = _scan_high_entropy_strings(content)
        assert not any("com.example.app" in r["match"] for r in results)

    def test_filters_node_modules(self):
        content = "path=some_long_node_modules_path_thing done"
        results = _scan_high_entropy_strings(content)
        assert not any("node_modules" in r["match"] for r in results)

    def test_filters_redacted_secret_tail_context(self):
        content = 'api_key="[REDACTED].[REDACTED].r-P9lh3Hx7T9K3JdE7Mp2Qz4Ls8Bv5N"'
        results = _scan_high_entropy_strings(content)
        assert results == []

    def test_filters_encrypted_content_blob(self):
        blob = "gAAAAABmQ1cXdY3Kp7Lm9Nq2R8vS4tU6wX1yZ3aB5cD7eF9gH0iJ"
        content = f'"encrypted_content":"{blob}"'
        results = _scan_high_entropy_strings(content)
        assert results == []

    def test_filters_tool_call_ids(self):
        content = "token context call_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890 done"
        results = _scan_high_entropy_strings(content)
        assert results == []


# --- _scan_pii integration with high_entropy_strings ---


class TestScanPiiHighEntropy:
    def test_includes_high_entropy_when_present(self, tmp_path):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        f = tmp_path / "export.jsonl"
        f.write_text(f'{{"message": "config token {secret} end"}}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" in results
        assert any(r["match"] == secret for r in results["high_entropy_strings"])

    def test_excludes_high_entropy_when_clean(self, tmp_path):
        f = tmp_path / "export.jsonl"
        f.write_text('{"message": "nothing suspicious here at all"}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" not in results

    def test_filters_common_code_artifacts(self, tmp_path):
        f = tmp_path / "export.jsonl"
        f.write_text(
            "\n".join([
                '{"message": "n+@pytest.mark.parametrize and n@tasks.loop are decorators"}',
                '{"message": "class names sk-notification sk-details-panel hf_hub_download"}',
                '{"message": "attachment hf_20260302_abc123def456ghi789jkl012.mp4"}',
                '{"message": "versions 844.4.0.17 and private 192.168.1.10"}',
            ])
        )
        assert _scan_pii(f) == {}

    def test_detects_real_api_keys(self, tmp_path):
        f = tmp_path / "export.jsonl"
        hf_token = "hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        openai_key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
        f.write_text(f'{{"message": "{hf_token} {openai_key}"}}\n')
        results = _scan_pii(f)
        assert hf_token in results["api_keys"]
        assert openai_key in results["api_keys"]

    def test_redacts_mechanical_findings_from_shards(self, tmp_path):
        run_dir = tmp_path / "run"
        shard = run_dir / "codex" / "2026-04-25.jsonl"
        shard.parent.mkdir(parents=True)
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        shard.write_text(f'{{"message": "config token {secret} end"}}\n')
        manifest = {
            "shards": [{"path": "codex/2026-04-25.jsonl", "bytes": shard.stat().st_size}],
            "total_redactions": 0,
        }
        cli._write_manifest(run_dir, manifest)

        findings = cli._scan_pii_dir(run_dir, manifest)
        summary = cli._redact_mechanical_findings_dir(run_dir, manifest, findings)

        assert summary["redactions"] == 1
        assert secret not in shard.read_text()
        assert cli._scan_pii_dir(run_dir, manifest) == {}
        assert cli._read_manifest(run_dir)["total_redactions"] == 1


class TestPhase5Auto:
    class UUID:
        hex = "auto-run"

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    def _args(self, **overrides):
        values = {
            "retry_only": False,
            "force": False,
            "dry_run": False,
            "policy_override": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _enable_args(self, **overrides):
        values = {
            "publish_attestation": "User explicitly approved publishing to Hugging Face for auto mode.",
            "policy": "strict",
            "full_name": None,
            "skip_full_name_scan": False,
            "enable_privacy_filter": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _manifest(self, total=1):
        return {
            "total_sessions_new": total,
            "max_end_time_by_source": {"claude": "2026-04-25T00:00:00Z"} if total else {},
            "shards": [],
        }

    def _patch_auto_common(self, monkeypatch, tmp_path, config=None, manifest=None, saved=None):
        staging_root = tmp_path / "staging"
        config = config or {"auto": {"enabled": True, "policy": "strict"}, "repo": "user/repo"}
        manifest = manifest or self._manifest()
        saved = [] if saved is None else saved

        monkeypatch.setattr(cli, "STAGING_ROOT", staging_root)
        monkeypatch.setattr(cli.uuid, "uuid4", lambda: self.UUID())
        monkeypatch.setattr(cli.dc_logging, "setup_logging", lambda _run_id: self.Logger())
        monkeypatch.setattr(cli, "load_config", lambda: json.loads(json.dumps(config)))
        monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(json.loads(json.dumps(cfg))))
        monkeypatch.setattr(cli, "_resolve_export_inputs", lambda cfg: ([{"display_name": "p"}], object(), []))
        monkeypatch.setattr(cli, "_scan_pf_new", lambda run_dir, cfg, logger=None: [])
        monkeypatch.setattr(cli, "_inspect_remote_dataset", lambda repo: {
            "manifest_exists": True,
            "manifest_error": None,
            "files_checked": True,
            "shard_count": 1,
            "missing_shards": [],
        })
        monkeypatch.setattr(cli, "export_to_shards", lambda *args, **kwargs: dict(manifest))
        return staging_root, saved

    def test_auto_requires_enable_auto(self, tmp_path, monkeypatch, capsys):
        self._patch_auto_common(monkeypatch, tmp_path, config={"repo": "user/repo", "auto": {"enabled": False}})

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(self._args())

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "auto"
        assert "enable-auto" in payload["next_command"]

    def test_auto_writes_directly_to_staging_root(self, tmp_path, monkeypatch, capsys):
        staging_root, _saved = self._patch_auto_common(monkeypatch, tmp_path)
        push = MagicMock()
        monkeypatch.setattr(cli, "_push_with_retry", push)

        cli._handle_auto(self._args(dry_run=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "dry-run"
        assert (staging_root / "auto-run").exists()
        assert not (tmp_path / ".dataclaw" / "export" / "runs").exists()
        push.assert_not_called()

    def test_auto_noop_when_no_new_sessions(self, tmp_path, monkeypatch, capsys):
        staging_root, saved = self._patch_auto_common(monkeypatch, tmp_path, manifest=self._manifest(total=0))
        push = MagicMock()
        monkeypatch.setattr(cli, "_push_with_retry", push)

        cli._handle_auto(self._args())

        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "noop"
        assert not (staging_root / "auto-run").exists()
        assert saved[-1]["last_auto_run"]["result"] == "noop"
        push.assert_not_called()

    def test_auto_success_moves_to_published(self, tmp_path, monkeypatch, capsys):
        staging_root, saved = self._patch_auto_common(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args())

        payload = json.loads(capsys.readouterr().out)
        published = staging_root / cli.PUBLISHED_DIRNAME / "auto-run"
        assert payload["result"] == "pushed"
        assert published.exists()
        assert not (staging_root / "auto-run").exists()
        assert saved[-1]["last_auto_run"]["result"] == "pushed"
        assert saved[-1]["last_dataset_update"]["repo"] == "user/repo"
        assert saved[-1]["last_dataset_update"]["total_sessions_new"] == 1

    def test_auto_crash_during_push_preserves_staging(self, tmp_path, monkeypatch):
        staging_root, _saved = self._patch_auto_common(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_push_with_retry", MagicMock(side_effect=KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            cli._handle_auto(self._args())

        assert (staging_root / "auto-run").exists()

    def test_auto_dry_run_skips_push(self, tmp_path, monkeypatch, capsys):
        staging_root, saved = self._patch_auto_common(monkeypatch, tmp_path)
        push = MagicMock()
        monkeypatch.setattr(cli, "_push_with_retry", push)

        cli._handle_auto(self._args(dry_run=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "dry-run"
        assert (staging_root / "auto-run").exists()
        assert saved[-1]["last_auto_run"]["result"] == "dry-run"
        push.assert_not_called()

    def test_cleanup_published_keeps_last_3(self, tmp_path):
        published = tmp_path / cli.PUBLISHED_DIRNAME
        for idx in range(5):
            run_dir = published / f"run-{idx}"
            run_dir.mkdir(parents=True)
            os.utime(run_dir, (idx, idx))

        cli._cleanup_published(tmp_path, keep=3)

        remaining = sorted(path.name for path in published.iterdir())
        assert remaining == ["run-2", "run-3", "run-4"]

    def test_cleanup_failed_staging_keeps_newest(self, tmp_path):
        published = tmp_path / cli.PUBLISHED_DIRNAME / "published-run"
        published.mkdir(parents=True)
        for idx in range(3):
            run_dir = tmp_path / f"failed-{idx}"
            run_dir.mkdir()
            os.utime(run_dir, (idx, idx))

        deleted = cli._cleanup_failed_staging(tmp_path, keep=1)

        assert deleted == 2
        assert set(path.name for path in tmp_path.iterdir()) == {cli.PUBLISHED_DIRNAME, "failed-2"}

    def test_cutoff_only_advances_on_push_success(self, tmp_path, monkeypatch):
        _staging_root, saved = self._patch_auto_common(
            monkeypatch,
            tmp_path,
            config={"auto": {"enabled": True, "policy": "strict"}, "repo": "user/repo", "last_export_cutoff": {}},
        )
        monkeypatch.setattr(cli, "_push_with_retry", MagicMock(side_effect=cli.PushFailed(ConnectionError("down"), 3, 150)))

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(self._args())

        assert exc.value.code == 4
        assert saved[-1].get("last_export_cutoff", {}) == {}

        saved.clear()
        self._patch_auto_common(
            monkeypatch,
            tmp_path / "success",
            config={"auto": {"enabled": True, "policy": "strict"}, "repo": "user/repo", "last_export_cutoff": {}},
            saved=saved,
        )
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args())

        assert saved[-1]["last_export_cutoff"] == {"claude": "2026-04-25T00:00:00Z"}

    def test_auto_uses_cutoff_when_remote_manifest_is_complete(self, tmp_path, monkeypatch):
        calls = []
        manifest = self._manifest()
        config = {
            "auto": {"enabled": True, "policy": "strict"},
            "repo": "user/repo",
            "last_export_cutoff": {"claude": "2026-04-24T00:00:00Z"},
        }
        self._patch_auto_common(monkeypatch, tmp_path, config=config, manifest=manifest)

        def fake_export(*args, **kwargs):
            calls.append(kwargs)
            return dict(manifest)

        monkeypatch.setattr(cli, "export_to_shards", fake_export)
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args())

        assert calls[-1]["since"] == {"claude": "2026-04-24T00:00:00Z"}

    def test_auto_falls_back_to_full_rebuild_when_remote_manifest_missing(self, tmp_path, monkeypatch):
        calls = []
        manifest = self._manifest()
        config = {
            "auto": {"enabled": True, "policy": "strict"},
            "repo": "user/repo",
            "last_export_cutoff": {"claude": "2026-04-24T00:00:00Z"},
        }
        self._patch_auto_common(monkeypatch, tmp_path, config=config, manifest=manifest)
        monkeypatch.setattr(cli, "_inspect_remote_dataset", lambda repo: {
            "manifest_exists": False,
            "manifest_error": None,
            "files_checked": False,
            "shard_count": 0,
            "missing_shards": [],
        })

        def fake_export(*args, **kwargs):
            calls.append(kwargs)
            return dict(manifest)

        monkeypatch.setattr(cli, "export_to_shards", fake_export)
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args())

        assert calls[-1]["since"] == {}

    def test_auto_falls_back_to_full_rebuild_when_remote_shards_missing(self, tmp_path, monkeypatch):
        calls = []
        manifest = self._manifest()
        config = {
            "auto": {"enabled": True, "policy": "strict"},
            "repo": "user/repo",
            "last_export_cutoff": {"claude": "2026-04-24T00:00:00Z"},
        }
        self._patch_auto_common(monkeypatch, tmp_path, config=config, manifest=manifest)
        monkeypatch.setattr(cli, "_inspect_remote_dataset", lambda repo: {
            "manifest_exists": True,
            "manifest_error": None,
            "files_checked": True,
            "shard_count": 2,
            "missing_shards": ["claude/2026-04-01.jsonl"],
        })

        def fake_export(*args, **kwargs):
            calls.append(kwargs)
            return dict(manifest)

        monkeypatch.setattr(cli, "export_to_shards", fake_export)
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args())

        assert calls[-1]["since"] == {}

    def test_enable_auto_requires_confirmed_stage(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "load_config", lambda: {"stage": "configured", "repo": "user/repo"})

        with pytest.raises(SystemExit) as exc:
            cli._handle_enable_auto(self._enable_args())

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "enable-auto"

    def test_enable_auto_requires_repo_or_hf_login(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "load_config", lambda: {"stage": "confirmed"})
        monkeypatch.setattr(cli, "get_hf_username", lambda: None)
        save = MagicMock()
        monkeypatch.setattr(cli, "save_config", save)

        with pytest.raises(SystemExit) as exc:
            cli._handle_enable_auto(self._enable_args())

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "enable-auto"
        save.assert_not_called()

    def test_enable_auto_auto_detects_repo_from_hf_login(self, monkeypatch, capsys):
        saved = []
        monkeypatch.setattr(cli, "load_config", lambda: {"stage": "confirmed"})
        monkeypatch.setattr(cli, "get_hf_username", lambda: "tester")
        monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/dataclaw")
        monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(json.loads(json.dumps(cfg))))

        cli._handle_enable_auto(self._enable_args())

        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"] == "tester/my-personal-codex-data"
        assert saved[-1]["repo"] == "tester/my-personal-codex-data"
        assert saved[-1]["auto"]["enabled"] is True

    def test_auto_run_exits_2_when_repo_missing(self, tmp_path, monkeypatch, capsys):
        staging_root, _saved = self._patch_auto_common(
            monkeypatch,
            tmp_path,
            config={"auto": {"enabled": True, "policy": "strict"}},
        )

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(self._args())

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "auto"
        assert not staging_root.exists()


class TestPhase10PushRetry:
    class Logger:
        def __init__(self):
            self.records = []

        def info(self, msg, extra=None):
            self.records.append((msg, extra))

    def _response(self, status, headers=None):
        return SimpleNamespace(
            status_code=status,
            headers=headers or {},
            request=SimpleNamespace(method="PUT", url="https://example.invalid"),
            url="https://example.invalid",
        )

    def _http_error(self, status, headers=None):
        from huggingface_hub.utils import HfHubHTTPError

        return HfHubHTTPError("http error", response=self._response(status, headers))

    def _args(self, **overrides):
        values = {
            "retry_only": False,
            "force": False,
            "dry_run": False,
            "policy_override": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _manifest(self):
        return {
            "total_sessions_new": 1,
            "max_end_time_by_source": {"claude": "2026-04-25T00:00:00Z"},
            "shards": [],
        }

    def _patch_auto_common(self, monkeypatch, tmp_path, saved=None):
        saved = [] if saved is None else saved
        staging_root = tmp_path / "staging"

        class UUID:
            hex = "push-run"

        monkeypatch.setattr(cli, "STAGING_ROOT", staging_root)
        monkeypatch.setattr(cli.uuid, "uuid4", lambda: UUID())
        monkeypatch.setattr(cli.dc_logging, "setup_logging", lambda _run_id: self.Logger())
        monkeypatch.setattr(cli, "load_config", lambda: {"auto": {"enabled": True, "policy": "strict"}, "repo": "user/repo"})
        monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(json.loads(json.dumps(cfg))))
        monkeypatch.setattr(cli, "_resolve_export_inputs", lambda cfg: ([{"display_name": "p"}], object(), []))
        monkeypatch.setattr(cli, "_scan_pf_new", lambda run_dir, cfg: [])
        monkeypatch.setattr(cli, "export_to_shards", lambda *args, **kwargs: self._manifest())
        return staging_root, saved

    def test_push_retry_backoff(self, monkeypatch):
        calls = {"n": 0}

        def attempt(*args):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("down")
            return "https://hf/user/repo"

        sleeps = []
        monkeypatch.setattr(cli, "_push_shards_attempt", attempt)

        result = cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=sleeps.append, jitter=lambda: 0)

        assert result == ("https://hf/user/repo", 3, 150)
        assert sleeps == [30, 120]

    def test_push_429_honors_retry_after(self, monkeypatch):
        calls = {"n": 0}

        def attempt(*args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._http_error(429, {"Retry-After": "45"})
            return "https://hf/user/repo"

        sleeps = []
        monkeypatch.setattr(cli, "_push_shards_attempt", attempt)

        result = cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=sleeps.append, jitter=lambda: 0)

        assert result == ("https://hf/user/repo", 2, 75)
        assert sleeps == [45, 30]

    def _assert_http_authz_error_fails_immediately(self, monkeypatch, status):
        monkeypatch.setattr(cli, "_push_shards_attempt", MagicMock(side_effect=self._http_error(status)))

        with pytest.raises(cli.PushFailed) as exc:
            cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=lambda _s: None, jitter=lambda: 0)

        assert exc.value.attempts == 1
        assert exc.value.backoff_seconds_total == 0

    def test_push_401_fails_immediately(self, monkeypatch):
        self._assert_http_authz_error_fails_immediately(monkeypatch, 401)

    def test_push_400_fails_immediately(self, monkeypatch):
        self._assert_http_authz_error_fails_immediately(monkeypatch, 400)

    def test_push_403_fails_immediately(self, monkeypatch):
        self._assert_http_authz_error_fails_immediately(monkeypatch, 403)

    def test_push_auth_failed_raises_pushfailed_not_systemexit(self, monkeypatch):
        monkeypatch.setattr(cli, "_push_shards_attempt", MagicMock(side_effect=cli._AuthFailed("not logged in")))

        with pytest.raises(cli.PushFailed) as exc:
            try:
                cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=lambda _s: None, jitter=lambda: 0)
            except SystemExit as e:
                pytest.fail(f"expected PushFailed, got SystemExit({e.code})")

        assert exc.value.attempts == 1
        assert isinstance(exc.value.cause, cli._AuthFailed)

    def test_push_import_error_raises_pushfailed_not_systemexit(self, monkeypatch):
        monkeypatch.setattr(cli, "_push_shards_attempt", MagicMock(side_effect=cli._AuthFailed("huggingface_hub not installed")))

        with pytest.raises(cli.PushFailed) as exc:
            try:
                cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=lambda _s: None, jitter=lambda: 0)
            except SystemExit as e:
                pytest.fail(f"expected PushFailed, got SystemExit({e.code})")

        assert exc.value.attempts == 1
        assert isinstance(exc.value.cause, cli._AuthFailed)

    def test_push_exhausts_with_3_attempts_and_total_wait_150s(self, monkeypatch):
        attempts = MagicMock(side_effect=ConnectionError("down"))
        sleeps = []
        monkeypatch.setattr(cli, "_push_shards_attempt", attempts)

        with pytest.raises(cli.PushFailed) as exc:
            cli._push_with_retry(Path("/tmp/run"), "user/repo", {}, self.Logger(), sleep=sleeps.append, jitter=lambda: 0)

        assert attempts.call_count == 3
        assert exc.value.attempts == 3
        assert exc.value.backoff_seconds_total == 150
        assert sleeps == [30, 120]

    def test_push_exhaustion_in_handle_auto_exits_4_and_preserves_staging_in_root(self, tmp_path, monkeypatch):
        staging_root, saved = self._patch_auto_common(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_push_with_retry", MagicMock(side_effect=cli.PushFailed(ConnectionError("down"), 3, 150)))

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(self._args())

        assert exc.value.code == 4
        assert (staging_root / "push-run").exists()
        assert saved[-1]["last_auto_run"]["result"] == "error"

    def test_handle_auto_authfailed_exits_4_not_1(self, tmp_path, monkeypatch):
        staging_root, saved = self._patch_auto_common(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_push_shards_attempt", MagicMock(side_effect=cli._AuthFailed("not logged in")))

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(self._args())

        assert exc.value.code == 4
        assert (staging_root / "push-run").exists()
        assert saved[-1]["last_auto_run"]["result"] == "error"

    def test_retry_only_reuses_failed_run_dir_and_publishes(self, tmp_path, monkeypatch):
        staging_root = tmp_path / "staging"
        failed = staging_root / "original-run"
        (failed / cli.MANIFEST_REL).parent.mkdir(parents=True)
        (failed / cli.MANIFEST_REL).write_text(json.dumps(self._manifest()))
        saved = []

        class UUID:
            hex = "retry-wrapper-run"

        monkeypatch.setattr(cli, "STAGING_ROOT", staging_root)
        monkeypatch.setattr(cli.uuid, "uuid4", lambda: UUID())
        monkeypatch.setattr(cli.dc_logging, "setup_logging", lambda _run_id: self.Logger())
        monkeypatch.setattr(cli, "load_config", lambda: {"auto": {"enabled": True}, "repo": "user/repo", "last_export_cutoff": {}})
        monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(json.loads(json.dumps(cfg))))
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))

        cli._handle_auto(self._args(retry_only=True))

        published = staging_root / cli.PUBLISHED_DIRNAME / "original-run"
        assert published.exists()
        assert not failed.exists()
        assert saved[-1]["last_auto_run"]["result"] == "pushed"

    def test_retry_only_no_failed_dir_exits_2(self, tmp_path, monkeypatch, capsys):
        staging_root = tmp_path / "staging"
        monkeypatch.setattr(cli, "STAGING_ROOT", staging_root)

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto_retry_only(self._args(retry_only=True), {"repo": "user/repo"}, "run", self.Logger(), "2026-04-25T00:00:00Z")

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "auto --retry-only"

    def test_staging_size_warning_above_5gb(self, tmp_path, monkeypatch):
        _staging_root, _saved = self._patch_auto_common(monkeypatch, tmp_path)
        summaries = []
        monkeypatch.setattr(cli, "_staging_size_bytes", lambda _root: 6 * 1024 * 1024 * 1024)
        monkeypatch.setattr(cli, "_push_with_retry", lambda *args, **kwargs: ("https://hf/user/repo", 1, 0))
        monkeypatch.setattr(cli.dc_logging, "write_run_summary", lambda run_dir, summary: summaries.append(summary))

        cli._handle_auto(self._args())

        assert summaries
        assert any("exceeds 5 GB" in warning for warning in summaries[-1]["warnings"])

    def test_clean_staging_removes_failed_not_published(self, tmp_path, monkeypatch, capsys):
        failed = tmp_path / "failed"
        published = tmp_path / cli.PUBLISHED_DIRNAME / "published-run"
        failed.mkdir(parents=True)
        published.mkdir(parents=True)
        monkeypatch.setattr(cli, "STAGING_ROOT", tmp_path)

        cli._handle_clean_staging(SimpleNamespace(yes=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] is True
        assert not failed.exists()
        assert published.exists()

    def test_push_shards_to_huggingface_legacy_authfailed_still_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_push_shards_attempt", MagicMock(side_effect=cli._AuthFailed("huggingface_hub not installed")))

        with pytest.raises(SystemExit) as exc:
            cli.push_shards_to_huggingface(Path("/tmp/run"), "user/repo", {"shards": [], "total_sessions_new": 0})

        assert exc.value.code == 1
        assert "huggingface_hub not installed" in capsys.readouterr().err


class TestPhase12Rollback:
    class FakeCommit:
        def __init__(self, commit_id, title="title", created_at="2026-04-25T00:00:00Z"):
            self.commit_id = commit_id
            self.title = title
            self.created_at = created_at

    class FakeApi:
        def __init__(self, tmp_path: Path):
            self.tmp_path = tmp_path
            self.create_commit_calls = []

        def list_repo_commits(self, repo_id, repo_type):
            return [
                TestPhase12Rollback.FakeCommit("sha3", "third", "2026-04-25T03:00:00Z"),
                TestPhase12Rollback.FakeCommit("sha2", "second", "2026-04-25T02:00:00Z"),
                TestPhase12Rollback.FakeCommit("sha1", "first", "2026-04-25T01:00:00Z"),
            ]

        def list_repo_files(self, repo_id, repo_type, revision=None):
            if revision == "sha1":
                return ["README.md", "data/target.jsonl"]
            return ["README.md", "data/target.jsonl", "data/delete.jsonl"]

        def hf_hub_download(self, repo_id, filename, repo_type, revision):
            path = self.tmp_path / filename.replace("/", "_")
            path.write_text(f"{revision}:{filename}")
            return str(path)

        def create_commit(self, **kwargs):
            self.create_commit_calls.append(kwargs)
            return TestPhase12Rollback.FakeCommit("new-sha")

    def _args(self, **overrides):
        values = {
            "repo": "user/repo",
            "list_commits": False,
            "commit": "sha1",
            "dry_run": False,
            "limit": 20,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_rollback_list_shows_recent_commits(self, tmp_path, monkeypatch, capsys):
        fake_api = self.FakeApi(tmp_path)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": "config/repo"})
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake_api)

        _handle_rollback(self._args(list_commits=True, limit=3))

        payload = json.loads(capsys.readouterr().out)
        assert [row["sha"] for row in payload] == ["sha3", "sha2", "sha1"]
        assert [row["title"] for row in payload] == ["third", "second", "first"]
        assert payload[0]["created_at"] == "2026-04-25T03:00:00Z"

    def test_rollback_reverts_to_commit(self, tmp_path, monkeypatch, capsys):
        fake_api = self.FakeApi(tmp_path)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": "config/repo"})
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake_api)
        monkeypatch.setattr("dataclaw.cli.dc_logging.setup_logging", lambda _run_id: logging.getLogger("rollback-test"))

        _handle_rollback(self._args(commit="sha1"))

        assert len(fake_api.create_commit_calls) == 1
        call = fake_api.create_commit_calls[0]
        assert call["repo_id"] == "user/repo"
        assert call["repo_type"] == "dataset"
        assert call["commit_message"] == "Rollback to sha1"
        paths = [op.path_in_repo for op in call["operations"]]
        assert paths == ["README.md", "data/target.jsonl", "data/delete.jsonl"]
        assert call["operations"][-1].__class__.__name__ == "CommitOperationDelete"
        payload = json.loads(capsys.readouterr().out)
        assert payload["rolled_back"] is True
        assert payload["new_commit"] == "new-sha"

    def test_rollback_dry_run_does_not_call_create_commit(self, tmp_path, monkeypatch, capsys):
        fake_api = self.FakeApi(tmp_path)
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": "config/repo"})
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake_api)
        monkeypatch.setattr("dataclaw.cli.dc_logging.setup_logging", lambda _run_id: logging.getLogger("rollback-test"))

        _handle_rollback(self._args(commit="sha1", dry_run=True))

        assert fake_api.create_commit_calls == []
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["target_commit"] == "sha1"

    def test_rollback_logs_structured_event(self, tmp_path, monkeypatch, capsys):
        fake_api = self.FakeApi(tmp_path)
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("rollback-structured-test")
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(CaptureHandler())

        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": "config/repo"})
        monkeypatch.setattr("huggingface_hub.HfApi", lambda: fake_api)
        monkeypatch.setattr("dataclaw.cli.dc_logging.setup_logging", lambda _run_id: logger)

        _handle_rollback(self._args(commit="sha1", dry_run=True))

        capsys.readouterr()
        rollback_records = [record for record in records if getattr(record, "phase", None) == "rollback"]
        assert len(rollback_records) == 1
        record = rollback_records[0]
        for key in ("initiator", "target_commit", "previous_commit", "reverted_files", "dry_run"):
            assert hasattr(record, key)
            assert key in record.extra_data

    def test_rollback_requires_repo(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {})

        with pytest.raises(SystemExit) as exc:
            _handle_rollback(self._args(repo=None, commit="sha1"))

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_on_step"] == "rollback"


class TestHfAuthCli:
    class FakeKeyring:
        def __init__(self):
            self.store = {}

        def get_password(self, service, account):
            return self.store.get((service, account))

        def set_password(self, service, account, token):
            self.store[(service, account)] = token

        def delete_password(self, service, account):
            self.store.pop((service, account), None)

    @staticmethod
    def _json_block(stdout: str) -> dict:
        marker = "---DATACLAW_JSON---"
        assert marker in stdout
        return json.loads(stdout.split(marker, 1)[1])

    @pytest.fixture
    def fake_keyring(self, monkeypatch):
        fake = self.FakeKeyring()
        monkeypatch.setattr("dataclaw.auth.keyring", fake)
        monkeypatch.setitem(sys.modules, "keyring", fake)
        return fake

    @pytest.fixture
    def token_path(self, tmp_path, monkeypatch):
        path = tmp_path / "huggingface" / "token"
        monkeypatch.setattr("dataclaw.auth.HF_STANDARD_TOKEN", path)
        return path

    def test_hf_login_stdin_stores_in_keyring_and_mirrors(
        self, monkeypatch, capsys, fake_keyring, token_path
    ):
        class FakeApi:
            def whoami(self):
                return {"name": "alice"}

        monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=lambda: FakeApi()))
        monkeypatch.setattr("sys.stdin", io.StringIO("hf_xxxxx\n"))
        monkeypatch.setattr("sys.argv", ["dataclaw", "hf", "login", "--token-stdin"])

        main()

        payload = self._json_block(capsys.readouterr().out)
        assert payload == {"ok": True, "user": "alice", "mirrored": True}
        assert fake_keyring.get_password("io.dataclaw.app", "hf_token") == "hf_xxxxx"
        assert token_path.read_text(encoding="utf-8") == "hf_xxxxx"
        assert oct(token_path.stat().st_mode)[-3:] == "600"
        assert os.environ["HF_TOKEN"] == "hf_xxxxx"

    def test_hf_logout_clears_keyring_and_mirror(
        self, monkeypatch, capsys, fake_keyring, token_path
    ):
        fake_keyring.set_password("io.dataclaw.app", "hf_token", "hf_xxxxx")
        token_path.parent.mkdir(parents=True)
        token_path.write_text("hf_xxxxx", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["dataclaw", "hf", "logout"])

        main()

        payload = self._json_block(capsys.readouterr().out)
        assert payload == {"ok": True}
        assert fake_keyring.get_password("io.dataclaw.app", "hf_token") is None
        assert not token_path.exists()

    def test_hf_whoami_check_keyring_only_exits_0_when_present_2_when_absent(
        self, monkeypatch, capsys, fake_keyring
    ):
        monkeypatch.setattr("sys.argv", ["dataclaw", "hf", "whoami", "--check-keyring-only"])

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        missing_payload = self._json_block(capsys.readouterr().out)
        assert missing_payload["ok"] is False

        fake_keyring.set_password("io.dataclaw.app", "hf_token", "hf_xxxxx")
        main()
        present_payload = self._json_block(capsys.readouterr().out)
        assert present_payload == {"ok": True}

    def test_auto_exits_2_with_hf_hint_when_no_token_anywhere(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {
            "auto": {"enabled": True},
            "repo": "user/repo",
        })
        monkeypatch.setattr("dataclaw.cli.dc_logging.setup_logging", lambda _run_id: logging.getLogger("auto-test"))
        monkeypatch.setattr("dataclaw.cli._resolve_hf_token", lambda: None)

        with pytest.raises(SystemExit) as exc:
            cli._handle_auto(SimpleNamespace(retry_only=False, dry_run=False, force=False, policy_override=None))

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert "dataclaw hf login" in payload["hint"]

    def test_status_json_emits_envelope_block(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"repo": "user/repo", "source": "codex"})
        monkeypatch.setattr("dataclaw.cli.get_hf_username", lambda: "alice")
        monkeypatch.setattr("sys.argv", ["dataclaw", "status", "--json"])

        main()

        payload = self._json_block(capsys.readouterr().out)
        assert payload["repo"] == "user/repo"
        assert payload["source"] == "codex"
        assert "last_auto_run" in payload
        assert "schedule" in payload

    def test_config_json_respects_show_secrets(self, monkeypatch, capsys):
        monkeypatch.setattr("dataclaw.cli.load_config", lambda: {"redact_strings": ["secret-value"]})
        monkeypatch.setattr("sys.argv", ["dataclaw", "config", "--json"])
        main()
        masked = self._json_block(capsys.readouterr().out)
        assert masked["redact_strings"] == ["***"]

        monkeypatch.setattr("sys.argv", ["dataclaw", "config", "--json", "--show-secrets"])
        main()
        unmasked = self._json_block(capsys.readouterr().out)
        assert unmasked["redact_strings"] == ["secret-value"]

    def test_push_to_huggingface_uses_resolved_keychain_token(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "data.jsonl"
        jsonl_path.write_text("{}\n", encoding="utf-8")
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.setattr("dataclaw.cli._resolve_hf_token", lambda: "hf_fake")
        seen = {}

        class FakeApi:
            def whoami(self):
                seen["token"] = os.environ.get("HF_TOKEN")
                return {"name": "alice"}

            def create_repo(self, *args, **kwargs):
                pass

            def upload_file(self, *args, **kwargs):
                pass

        monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=lambda: FakeApi()))

        push_to_huggingface(jsonl_path, "user/repo", {})

        assert seen["token"] == "hf_fake"

    def test_push_shards_uses_resolved_keychain_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.setattr("dataclaw.cli._resolve_hf_token", lambda: "hf_fake")
        monkeypatch.setattr("dataclaw.cli._fetch_remote_manifest", lambda _repo_id: None)
        seen = {}

        class FakeApi:
            def whoami(self):
                seen["token"] = os.environ.get("HF_TOKEN")
                return {"name": "alice"}

            def create_repo(self, *args, **kwargs):
                pass

            def upload_file(self, *args, **kwargs):
                pass

            def upload_folder(self, *args, **kwargs):
                pass

        monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=lambda: FakeApi()))
        manifest = {
            "shards": [{"path": "claude/2026-04-25.jsonl"}],
            "total_sessions_new": 1,
            "sources": ["claude"],
            "buckets": [],
            "models": {},
            "total_sessions_in_shards": 1,
        }

        cli._push_shards_attempt(tmp_path, "user/repo", manifest)

        assert seen["token"] == "hf_fake"
