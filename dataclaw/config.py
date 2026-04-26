"""Persistent config for DataClaw — stored at ~/.dataclaw/config.json"""

import json
import os
import sys
from pathlib import Path
from typing import TypedDict, cast

CONFIG_DIR = Path.home() / ".dataclaw"
CONFIG_FILE = CONFIG_DIR / "config.json"
MIGRATION_VERSION = 1


class DataClawConfig(TypedDict, total=False):
    """Expected shape of the config dict."""

    repo: str | None
    source: str | None  # "claude" | "codex" | "gemini" | "opencode" | "openclaw" | "kimi" | "hermes" | "custom" | "all"
    excluded_projects: list[str]
    redact_strings: list[str]
    redact_usernames: list[str]
    last_export: dict
    stage: str | None  # "auth" | "configure" | "review" | "confirmed" | "done"
    projects_confirmed: bool  # True once user has addressed folder exclusions
    review_attestations: dict
    review_verification: dict
    last_confirm: dict
    publish_attestation: str
    last_export_cutoff: dict[str, str]
    last_dataset_update: dict
    folder_rules: dict
    project_tags: dict[str, list[str]]
    auto: dict
    known_findings: dict
    last_auto_run: dict
    privacy_filter: dict
    updater: dict


DEFAULT_CONFIG: DataClawConfig = {
    "repo": None,
    "source": "all",
    "excluded_projects": [],
    "redact_strings": [],
    "privacy_filter": {"enabled": True, "device": "auto"},
    "app": {
        "launch_at_login": True,
        "sync_enabled": True,
        "sync_interval_hours": 24,
    },
}


def load_config() -> DataClawConfig:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            return cast(DataClawConfig, {**DEFAULT_CONFIG, **stored})
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {CONFIG_FILE}: {e}", file=sys.stderr)
    return cast(DataClawConfig, dict(DEFAULT_CONFIG))


def save_config(config: DataClawConfig) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(CONFIG_FILE, 0o600)
    except OSError as e:
        print(f"Warning: could not save {CONFIG_FILE}: {e}", file=sys.stderr)
