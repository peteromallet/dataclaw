"""Tests for shared CLI helpers."""

from dataclaw import _json as json
from dataclaw._cli import common as common_mod
from dataclaw._cli.common import (
    ProgressReporter,
    _build_status_next_steps,
    _format_size,
    _format_token_count,
    _merge_config_list,
    _parse_csv_arg,
    default_repo_name,
)


class TestProgressReporter:
    def test_emits_well_formed_throttled_progress_and_forced_final(self, monkeypatch, capsys):
        times = iter([0.0, 1.0, 1.5, 2.1, 2.2])
        monkeypatch.setattr(common_mod.time, "monotonic", lambda: next(times))

        reporter = ProgressReporter("export_session_progress", "export", 4)

        assert reporter.emit(0, force=True, extra={"sessions_exported": 0})
        assert not reporter.emit(1, extra={"sessions_exported": 1})
        assert not reporter.emit(2, extra={"sessions_exported": 2})
        assert reporter.emit(3, extra={"sessions_exported": 3})
        assert reporter.emit(4, force=True, extra={"sessions_exported": 4})

        events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert [event["msg"] for event in events] == ["export_session_progress"] * 3
        assert [event["extra"]["current"] for event in events] == [0, 3, 4]
        assert events[-1]["phase"] == "export"
        assert events[-1]["extra"]["total"] == 4
        assert events[-1]["extra"]["sessions_exported"] == 4
        assert "ts" in events[-1]


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


class TestDefaultRepoName:
    def test_format(self):
        result = default_repo_name("alice")
        assert result == "alice/my-personal-codex-data"

    def test_contains_username(self):
        result = default_repo_name("bob")
        assert "bob" in result
        assert "/" in result


class TestStatusNextSteps:
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
