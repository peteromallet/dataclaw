"""Tests for dataclaw.anonymizer — PII anonymization."""

import pytest

from dataclaw.anonymizer import (
    Anonymizer,
    _hash_username,
    _replace_username,
    anonymize_path,
    anonymize_text,
)


# --- _hash_username ---


class TestHashUsername:
    def test_deterministic(self):
        assert _hash_username("alice") == _hash_username("alice")

    def test_different_inputs_differ(self):
        assert _hash_username("alice") != _hash_username("bob")

    def test_prefix_format(self):
        result = _hash_username("alice")
        assert result.startswith("user_")
        assert len(result) == 13  # "user_" + 8 hex chars

    def test_hex_chars(self):
        result = _hash_username("testuser")
        suffix = result[5:]
        assert all(c in "0123456789abcdef" for c in suffix)


# --- anonymize_path ---


class TestAnonymizePath:
    def test_empty_path(self):
        assert anonymize_path("", "alice", "user_abc12345") == ""

    def test_global_replace(self):
        # if username is >= 4 chars, username is hashed using global replace
        result = anonymize_path(
            "/Users/alice/something",
            "alice", "user_abc12345"
        )
        assert result == "/Users/user_abc12345/something"

    def test_bare_home_hashed(self):
        result = anonymize_path(
            "/Users/s/somedir/file.py",
            "s", "user_abc12345", home="/Users/s",
        )
        assert result == "/Users/user_abc12345/somedir/file.py"

    def test_linux_home_path(self):
        result = anonymize_path(
            "/home/s/Documents/project/file.py",
            "s", "user_abc12345", home="/home/s",
        )
        assert result == "/home/user_abc12345/Documents/project/file.py"

    def test_path_not_under_home(self):
        result = anonymize_path(
            "/var/log/syslog",
            "s", "user_abc12345", home="/Users/s",
        )
        assert result == "/var/log/syslog"


# --- anonymize_text ---


class TestAnonymizeText:
    def test_empty_text(self):
        assert anonymize_text("", "alice", "user_abc12345") == ""

    def test_empty_username(self):
        assert anonymize_text("hello alice", "", "user_abc12345") == "hello alice"

    def test_none_text(self):
        assert anonymize_text(None, "alice", "user_abc12345") is None

    def test_users_path_replaced(self):
        result = anonymize_text(
            "File at /Users/alice/project/main.py",
            "alice", "user_abc12345",
        )
        assert result == "File at /Users/user_abc12345/project/main.py"

    def test_home_path_replaced(self):
        result = anonymize_text(
            "File at /home/alice/project/main.py",
            "alice", "user_abc12345",
        )
        assert result == "File at /home/user_abc12345/project/main.py"

    def test_hyphen_encoded_path(self):
        result = anonymize_text(
            "-Users-alice-Documents-myproject",
            "alice", "user_abc12345",
        )
        assert result == "-Users-user_abc12345-Documents-myproject"

    def test_temp_path(self):
        result = anonymize_text(
            "/private/tmp/claude-501/-Users-alice-Documents-proj/foo",
            "alice", "user_abc12345",
        )
        assert result == "/private/tmp/claude-501/-Users-user_abc12345-Documents-proj/foo"

    def test_bare_username_replaced(self):
        result = anonymize_text(
            "Hello alice, welcome back",
            "alice", "user_abc12345",
        )
        assert result == "Hello user_abc12345, welcome back"

    def test_short_username_not_replaced_bare(self):
        # Usernames < 4 chars should NOT be replaced as bare words
        result = anonymize_text(
            "Hello bob, welcome back",
            "bob", "user_abc12345",
        )
        assert result == "Hello bob, welcome back"

    def test_short_username_path_still_replaced(self):
        # Even short usernames should be replaced in path contexts
        result = anonymize_text(
            "File at /Users/bob/project",
            "bob", "user_abc12345",
        )
        assert result == "File at /Users/user_abc12345/project"


# --- Anonymizer class ---


class TestAnonymizer:
    def test_path_method(self, mock_anonymizer):
        result = mock_anonymizer.path("/Users/testuser/Documents/myproject/main.py")
        assert "testuser" not in result
        assert "myproject/main.py" in result

    def test_text_method(self, mock_anonymizer):
        result = mock_anonymizer.text("Hello testuser, your home is /Users/testuser")
        assert "testuser" not in result

    def test_deterministic_hash(self, mock_anonymizer):
        r1 = mock_anonymizer.path("/Users/testuser/Documents/proj/a.py")
        r2 = mock_anonymizer.path("/Users/testuser/Documents/proj/a.py")
        assert r1 == r2

    def test_extra_usernames(self, monkeypatch):
        monkeypatch.setattr(
            "dataclaw.anonymizer._detect_home_dir",
            lambda: ("/Users/testuser", "testuser"),
        )
        anon = Anonymizer(extra_usernames=["github_handle"])
        result = anon.text("by github_handle on GitHub")
        assert "github_handle" not in result

    def test_extra_usernames_dedup(self, monkeypatch):
        monkeypatch.setattr(
            "dataclaw.anonymizer._detect_home_dir",
            lambda: ("/Users/testuser", "testuser"),
        )
        # Primary username in extra list should be skipped (not duplicated)
        anon = Anonymizer(extra_usernames=["testuser", "other"])
        assert len(anon._extra) == 1  # only "other"


# --- _replace_username ---


class TestReplaceUsername:
    def test_case_insensitive(self):
        result = _replace_username("Hello ALICE and Alice", "alice", "user_abc")
        assert "ALICE" not in result
        assert "Alice" not in result
        assert "user_abc" in result

    def test_short_username_skipped(self):
        # < 3 chars should be skipped
        result = _replace_username("Hello ab and AB", "ab", "user_abc")
        assert result == "Hello ab and AB"

    def test_empty_text(self):
        assert _replace_username("", "alice", "user_abc") == ""

    def test_empty_username(self):
        assert _replace_username("hello", "", "user_abc") == "hello"

    def test_none_text(self):
        assert _replace_username(None, "alice", "user_abc") is None
