"""Tests for AnonymizerWrapper and PassthroughAnonymizer."""

import pytest
from unittest.mock import patch, MagicMock

from dataclaw.parser import AnonymizerWrapper, PassthroughAnonymizer


class TestPassthroughAnonymizer:
    """Tests for the no-op PassthroughAnonymizer."""

    def test_text_passthrough(self):
        """Text should be returned unchanged."""
        anon = PassthroughAnonymizer()
        assert anon.text("Hello world") == "Hello world"
        assert anon.text("/Users/alice/project/file.py") == "/Users/alice/project/file.py"
        assert anon.text("") == ""

    def test_path_passthrough(self):
        """Paths should be returned unchanged."""
        anon = PassthroughAnonymizer()
        assert anon.path("/Users/bob/code/main.py") == "/Users/bob/code/main.py"
        assert anon.path("") == ""

    def test_none_handling(self):
        """None inputs should be handled gracefully."""
        anon = PassthroughAnonymizer()
        assert anon.text("") == ""
        assert anon.path("") == ""


class TestAnonymizerWrapper:
    """Tests for AnonymizerWrapper that wraps scout.tools.AnonymizerTool."""

    @patch("dataclaw.parser.AnonymizerTool")
    def test_text_calls_tool(self, mock_tool_class):
        """Text should call the underlying tool."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "anonymized_text"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()
        result = wrapper.text("original text")

        mock_tool.run.assert_called_once()
        call_args = mock_tool.run.call_args[0][0]
        assert call_args["mode"] == "text"
        assert call_args["data"] == "original text"
        assert result == "anonymized_text"

    @patch("dataclaw.parser.AnonymizerTool")
    def test_path_calls_tool(self, mock_tool_class):
        """Path should call the underlying tool."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "anonymized_path"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()
        result = wrapper.path("/Users/alice/project/file.py")

        mock_tool.run.assert_called_once()
        call_args = mock_tool.run.call_args[0][0]
        assert call_args["mode"] == "path"
        assert call_args["data"] == "/Users/alice/project/file.py"
        assert result == "anonymized_path"

    @patch("dataclaw.parser.AnonymizerTool")
    def test_extra_usernames_passed(self, mock_tool_class):
        """Extra usernames should be passed to the tool."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "test"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper(extra_usernames=["github_user", "discord_user"])
        wrapper.text("test content")

        call_args = mock_tool.run.call_args[0][0]
        # Should include both extra usernames
        assert "github_user" in call_args["extra_usernames"]
        assert "discord_user" in call_args["extra_usernames"]

    @patch("dataclaw.parser.AnonymizerTool")
    def test_current_username_included(self, mock_tool_class):
        """Current system username should be included."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "test"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper(extra_usernames=["extra_user"])
        wrapper.text("test")

        call_args = mock_tool.run.call_args[0][0]
        # Current username should be in the list
        import os
        current_user = os.path.basename(os.path.expanduser("~"))
        assert current_user in call_args["extra_usernames"]

    @patch("dataclaw.parser.AnonymizerTool")
    def test_usernames_deduplicated(self, mock_tool_class):
        """Usernames should be deduplicated."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "test"}
        mock_tool_class.return_value = mock_tool

        # Pass current username as extra - should not cause duplicates
        import os
        current_user = os.path.basename(os.path.expanduser("~"))
        wrapper = AnonymizerWrapper(extra_usernames=[current_user])
        wrapper.text("test")

        call_args = mock_tool.run.call_args[0][0]
        # Should have only one occurrence
        assert len(call_args["extra_usernames"]) == len(set(call_args["extra_usernames"]))

    @patch("dataclaw.parser.AnonymizerTool")
    def test_empty_string_handling(self, mock_tool_class):
        """Empty strings should be handled gracefully."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "test"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()

        # Empty string should return empty
        result = wrapper.text("")
        assert result == ""

    @patch("dataclaw.parser.AnonymizerTool")
    def test_none_handling(self, mock_tool_class):
        """None inputs should return the original."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "test"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()

        # None should return None
        result = wrapper.text(None)
        assert result is None

    @patch("dataclaw.parser.AnonymizerTool")
    def test_tool_error_fallback(self, mock_tool_class):
        """If tool fails, return original text."""
        mock_tool = MagicMock()
        mock_tool.run.side_effect = Exception("Tool failed")
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()
        result = wrapper.text("sensitive data")

        # Should return original on error
        assert result == "sensitive data"

    @patch("dataclaw.parser.AnonymizerTool")
    def test_path_error_fallback(self, mock_tool_class):
        """If tool fails on path, return original path."""
        mock_tool = MagicMock()
        mock_tool.run.side_effect = Exception("Tool failed")
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()
        result = wrapper.path("/Users/alice/secret.txt")

        # Should return original on error
        assert result == "/Users/alice/secret.txt"

    @patch("dataclaw.parser.AnonymizerTool")
    def test_tool_returns_none_result(self, mock_tool_class):
        """If tool returns None result, return original."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": None}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()
        result = wrapper.text("test")

        # Should return original if result is None
        assert result == "test"


class TestAnonymizerWrapperIntegration:
    """Integration-style tests that verify the wrapper works with real scenarios."""

    @patch("dataclaw.parser.AnonymizerTool")
    def test_path_patterns(self, mock_tool_class):
        """Test various path patterns are passed correctly."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "anonymized"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()

        test_paths = [
            "/Users/username/Documents/project/file.py",
            "/home/username/project/src/main.rs",
            "/private/tmp/claude-123/-Users-username-/file.txt",
        ]

        for path in test_paths:
            wrapper.path(path)
            call_args = mock_tool.run.call_args[0][0]
            assert call_args["data"] == path

    @patch("dataclaw.parser.AnonymizerTool")
    def test_text_with_special_chars(self, mock_tool_class):
        """Test text with special characters is handled."""
        mock_tool = MagicMock()
        mock_tool.run.return_value = {"result": "anonymized"}
        mock_tool_class.return_value = mock_tool

        wrapper = AnonymizerWrapper()

        test_texts = [
            "Working in /Users/john/project",
            "Ran command: ls -la /home/bob/files",
            "Email: user@example.com",
        ]

        for text in test_texts:
            wrapper.text(text)
            call_args = mock_tool.run.call_args[0][0]
            assert call_args["data"] == text
