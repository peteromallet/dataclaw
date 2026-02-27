"""Anonymize PII in Claude Code log data."""

import hashlib
import os
import re


def _hash_username(username: str) -> str:
    return "user_" + hashlib.sha256(username.encode()).hexdigest()[:8]


def _detect_home_dir() -> tuple[str, str]:
    home = os.path.expanduser("~")
    username = os.path.basename(home)
    return home, username


def anonymize_text(text: str, username: str, username_hash: str, home: str | None = None) -> str:
    if not text or not username:
        return text

    escaped = re.escape(username)

    # Replace bare username in contexts (ls output, prose, etc.)
    # Only if username is >= 4 chars to avoid false positives
    # \b does not match word boundaries around underscore, but we need to match them
    if len(username) >= 4:
        return re.sub(rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])", username_hash, text, flags=re.IGNORECASE)

    # When username is < 4 chars, replace with more specific patterns

    # Match /Users/username , \Users\username , \\Users\\username , -Users-username (hyphen-encoded path like Claude Code and HuggingFace cache)
    # Match conventional indicators of home dir: 'Users' and 'home'
    # Ignore case for Windows-like pattern
    text = re.sub(rf"([/\\-]+(Users|home)[/\\-]+){escaped}(?=[^a-zA-Z0-9]|$)", rf"\g<1>{username_hash}", text, flags=re.IGNORECASE)

    # If home is not conventional, replace it with more specific pattern
    if home and not home.startswith(("/Users/", "/home/", "C:\\Users\\")):
        # Escape home and replace / or \ with `r"[/\\-]+"`
        home_escaped = home.replace("\\", "/")
        home_escaped = re.escape(home_escaped)
        home_escaped = home_escaped.replace("/", r"[/\\-]+")
        # In WSL and MSYS2, C:\ may be represented by /c/
        home_escaped = home_escaped.replace(":", ":?")

        def f(match):
            # match.group(0) is a non-escaped string
            return re.sub(rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])", username_hash, match.group(0), flags=re.IGNORECASE)

        text = re.sub(home_escaped, f, text, flags=re.IGNORECASE)

    return text


# Backward compatibility
anonymize_path = anonymize_text


class Anonymizer:
    """Stateful anonymizer that consistently hashes usernames."""

    def __init__(self, extra_usernames: list[str] | None = None):
        self.home, self.username = _detect_home_dir()
        self.username_hash = _hash_username(self.username)

        # Additional usernames to anonymize (GitHub handles, Discord names, etc.)
        self._extra: list[tuple[str, str]] = []
        for name in (extra_usernames or []):
            name = name.strip()
            if name and name != self.username:
                self._extra.append((name, _hash_username(name)))

    def path(self, file_path: str) -> str:
        return self.text(file_path)

    def text(self, content: str) -> str:
        result = anonymize_text(content, self.username, self.username_hash, self.home)
        for name, hashed in self._extra:
            result = _replace_username(result, name, hashed)
        return result


def _replace_username(text: str, username: str, username_hash: str) -> str:
    if not text or not username or len(username) < 4:
        return text
    escaped = re.escape(username)
    text = re.sub(rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])", username_hash, text, flags=re.IGNORECASE)
    return text
