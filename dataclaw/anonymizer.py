"""Anonymize PII in Claude Code log data."""

import functools
import hashlib
import os
import re


def _hash_username(username: str) -> str:
    return "user_" + hashlib.sha256(username.encode()).hexdigest()[:8]


def _detect_home_dir() -> tuple[str, str]:
    home = os.path.expanduser("~")
    username = os.path.basename(home)
    return home, username


@functools.lru_cache(maxsize=32)
def _get_username_pattern(username: str) -> re.Pattern:
    escaped = re.escape(username)
    # \b does not match word boundaries around underscore, but we need to match them
    return re.compile(rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])", flags=re.IGNORECASE)


@functools.lru_cache(maxsize=32)
def _get_home_pattern(username: str) -> re.Pattern:
    escaped = re.escape(username)
    # Match /Users/username , \Users\username , \\Users\\username , -Users-username (hyphen-encoded path like Claude Code and HuggingFace cache)
    # Match conventional indicators of home dir: 'Users' and 'home'
    # Ignore case for Windows-like path
    return re.compile(rf"([/\\-]+(Users|home)[/\\-]+){escaped}(?=[^a-zA-Z0-9]|$)", flags=re.IGNORECASE)


@functools.lru_cache(maxsize=32)
def _get_custom_home_pattern(home: str) -> re.Pattern | None:
    if home.startswith(("/Users/", "/home/", "C:\\Users\\")):
        return None

    # If home is not conventional, replace with more specific pattern

    # Escape home and replace / or \ with `r"[/\\-]+"`
    home_escaped = home.replace("\\", "/")
    home_escaped = re.escape(home_escaped)
    home_escaped = home_escaped.replace("/", r"[/\\-]+")
    # In WSL and MSYS2, C:\ may be represented by /c/
    home_escaped = home_escaped.replace(":", ":?")
    return re.compile(home_escaped, flags=re.IGNORECASE)


def anonymize_text(text: str, username: str, username_hash: str, home: str | None = None) -> str:
    if not text or not username:
        return text

    if username.lower() not in text.lower():
        return text

    # Replace bare username in contexts (ls output, prose, etc.)
    # Only if username is >= 4 chars to avoid false positives
    if len(username) >= 4:
        return _get_username_pattern(username).sub(username_hash, text)

    # When username is < 4 chars, replace with more specific patterns

    text = _get_home_pattern(username).sub(rf"\g<1>{username_hash}", text)

    if home:
        pat_home = _get_custom_home_pattern(home)
        if pat_home:
            pat_user = _get_username_pattern(username)
            def f(match):
                # match.group(0) is a non-escaped string
                return pat_user.sub(username_hash, match.group(0))
            text = pat_home.sub(f, text)

    return text


# Backward compatibility
anonymize_path = anonymize_text


class Anonymizer:
    """Stateful anonymizer that consistently hashes usernames."""

    def __init__(self, extra_usernames: list[str] | None = None):
        self.home, self.username = _detect_home_dir()
        self.username_hash = _hash_username(self.username)

        # Additional usernames to anonymize (GitHub handles, Discord names, etc.)
        self._extra_dict = {}
        for name in (extra_usernames or []):
            name = name.strip()
            if name and name != self.username and len(name) >= 4:
                self._extra_dict[name.lower()] = _hash_username(name)

        self._extra = list(self._extra_dict.keys())

        if self._extra_dict:
            escaped_names = [re.escape(k) for k in sorted(self._extra_dict.keys(), key=len, reverse=True)]
            self._extra_pattern = re.compile(rf"(?<![a-zA-Z0-9])({'|'.join(escaped_names)})(?![a-zA-Z0-9])", flags=re.IGNORECASE)
        else:
            self._extra_pattern = None

    def path(self, file_path: str) -> str:
        return self.text(file_path)

    def text(self, content: str) -> str:
        result = anonymize_text(content, self.username, self.username_hash, self.home)
        if self._extra_pattern:
            def f(match):
                return self._extra_dict[match.group(1).lower()]
            result = self._extra_pattern.sub(f, result)
        return result


def _replace_username(text: str, username: str, username_hash: str) -> str:
    if not text or not username or len(username) < 4:
        return text

    if username.lower() not in text.lower():
        return text

    pat = _get_username_pattern(username)
    return pat.sub(username_hash, text)
