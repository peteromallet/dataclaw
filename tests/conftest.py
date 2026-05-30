"""Shared fixtures for dataclaw tests."""

import pytest

from dataclaw.anonymizer import Anonymizer


@pytest.fixture
def sample_user_entry():
    """Realistic JSONL user entry dict."""
    return {
        "type": "user",
        "timestamp": 1706000000000,
        "cwd": "/Users/testuser/Documents/myproject",
        "gitBranch": "main",
        "version": "1.0.0",
        "sessionId": "abc-123",
        "message": {
            "content": "Fix the login bug in src/auth.py",
        },
    }


@pytest.fixture
def sample_assistant_entry():
    """Realistic JSONL assistant entry dict."""
    return {
        "type": "assistant",
        "timestamp": 1706000001000,
        "message": {
            "model": "claude-sonnet-4-20250514",
            "content": [
                {"type": "thinking", "thinking": "Let me look at the auth file."},
                {"type": "text", "text": "I'll fix the login bug."},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/Users/testuser/Documents/myproject/src/auth.py"},
                },
            ],
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 50,
            },
        },
    }


@pytest.fixture
def mock_anonymizer(monkeypatch):
    """Anonymizer with patched _detect_home_dir returning deterministic values."""
    monkeypatch.setattr(
        "dataclaw.anonymizer._detect_home_dir",
        lambda: ("/Users/testuser", "testuser"),
    )
    return Anonymizer()


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Monkeypatch CONFIG_FILE and CONFIG_DIR to tmp_path."""
    config_dir = tmp_path / ".dataclaw"
    config_file = config_dir / "config.json"
    monkeypatch.setattr("dataclaw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("dataclaw.config.CONFIG_FILE", config_file)
    return config_file


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Never read the developer's real ~/.dataclaw/config.json during tests.

    Without this, settings like ``privacy_filter.enabled`` from a real local
    config would leak into export tests (slow, non-deterministic). Tests that
    need specific config still monkeypatch ``load_config`` or use ``tmp_config``.
    """
    isolated_dir = tmp_path_factory.mktemp("dataclaw-config")
    monkeypatch.setattr("dataclaw.config.CONFIG_DIR", isolated_dir)
    monkeypatch.setattr("dataclaw.config.CONFIG_FILE", isolated_dir / "config.json")
