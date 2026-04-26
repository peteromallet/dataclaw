"""Tests for dataclaw.config — config persistence."""

import json
import stat

from dataclaw.config import DEFAULT_CONFIG, load_config, save_config


class TestLoadConfig:
    def test_no_file_returns_defaults(self, tmp_config):
        config = load_config()
        assert config["repo"] is None
        assert config["excluded_projects"] == []
        assert config["redact_strings"] == []
        assert config["privacy_filter"] == {"enabled": True, "device": "auto"}

    def test_valid_file_merged(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": "alice/data", "custom_key": "val"}))
        config = load_config()
        assert config["repo"] == "alice/data"
        assert config["custom_key"] == "val"
        # Defaults still present
        assert "excluded_projects" in config

    def test_corrupt_json_returns_defaults(self, tmp_config, capsys):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text("not valid json {{{")
        config = load_config()
        assert config["repo"] is None
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_extra_keys_preserved(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": None, "my_extra": [1, 2, 3]}))
        config = load_config()
        assert config["my_extra"] == [1, 2, 3]


class TestSaveConfig:
    def test_creates_dir_and_writes(self, tmp_config):
        save_config({"repo": "alice/data", "excluded_projects": []})
        assert tmp_config.exists()
        data = json.loads(tmp_config.read_text())
        assert data["repo"] == "alice/data"

    def test_config_roundtrip_with_new_fields(self, tmp_config):
        config = {
            **DEFAULT_CONFIG,
            "last_export_cutoff": {"claude": "2026-04-25T00:00:00Z"},
            "folder_rules": {
                "projects": {"repo-a": "work"},
                "default_bucket": "misc",
            },
            "project_tags": {"repo-a": ["client", "python"]},
            "last_auto_run": {"finished_at": "2026-04-25T01:00:00Z"},
        }
        save_config(config)

        assert load_config() == config

    def test_config_file_chmod_600(self, tmp_config):
        save_config({})

        assert stat.S_IMODE(tmp_config.stat().st_mode) == 0o600

    def test_overwrites_existing(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": "old"}))
        save_config({"repo": "new"})
        data = json.loads(tmp_config.read_text())
        assert data["repo"] == "new"

    def test_oserror_prints_warning(self, tmp_config, monkeypatch, capsys):
        # Make the directory unwritable
        monkeypatch.setattr(
            "dataclaw.config.CONFIG_DIR",
            tmp_config.parent / "nonexistent" / "deep" / "dir",
        )
        # Actually mock mkdir to raise
        import dataclaw.config as config_mod
        original_mkdir = type(tmp_config.parent).mkdir

        def failing_mkdir(self, *a, **kw):
            raise OSError("Permission denied")

        monkeypatch.setattr(type(tmp_config.parent), "mkdir", failing_mkdir)
        save_config({"repo": "test"})
        captured = capsys.readouterr()
        assert "Warning" in captured.err
