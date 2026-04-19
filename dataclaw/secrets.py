"""Detect and redact secrets in conversation data."""

import math
import re
from typing import Any

import ahocorasick

REDACTED = "[REDACTED]"

_GENERIC_SECRET_SUFFIXES = (
    "auth",
    "key",
    "secret",
    "token",
    "password",
)
_GENERIC_SECRET_SUFFIX_RE = "|".join(re.escape(name) for name in _GENERIC_SECRET_SUFFIXES)
_GENERIC_SECRET_NAME_PATTERN = rf"[A-Za-z0-9_-]*?(?:{_GENERIC_SECRET_SUFFIX_RE})"
_GENERIC_SECRET_MARKERS = tuple(
    f"{suffix}{delimiter}" for suffix in _GENERIC_SECRET_SUFFIXES for delimiter in ("=", ":", '"', "'", " ")
)
_FAST_PATH_CASE_MARKERS = (
    "eyJ",
    "sk-ant-",
    "sk-",
    "AIzaSy",
    "gsk_",
    "fm1_",
    "fm2_",
    "0x",
    "hf_",
    "ghp_",
    "gho_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "pypi-",
    "npm_",
    "AKIA",
    "xox",
    "discord",
    "PRIVATE KEY",
    "Bearer",
    "密码",
)
_FAST_PATH_LOWER_MARKERS = tuple(
    dict.fromkeys(("postgres", "secret_key", "aws_secret_access_key", "password", "passwd") + _GENERIC_SECRET_MARKERS)
)

# Ordered from most specific to least specific
SECRET_PATTERNS = [
    # JWT tokens - full 3-segment form
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),
    # JWT tokens - partial (header only or header+partial payload, e.g. truncated)
    ("jwt_partial", re.compile(r"eyJ[A-Za-z0-9_-]{15,}")),
    # PostgreSQL/database connection strings with passwords
    ("db_url", re.compile(r"postgres(?:ql)?://[^:]+:[^@\s]+@[^\s\"'`]+")),
    # Anthropic API keys
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI API keys
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{40,}")),
    # Google API keys (Gemini, Maps, etc.)
    ("google_api_key", re.compile(r"AIzaSy[A-Za-z0-9_-]{33}")),
    # Groq API keys
    ("groq_key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    # Telegram bot tokens
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # Fly.io machine/access tokens
    ("flyio_token", re.compile(r"fm[12]_[A-Za-z0-9/+=]{20,}")),
    # Ethereum / EVM private keys (0x + 64 hex chars)
    ("eth_private_key", re.compile(r"0x[0-9a-fA-F]{56,64}\b")),
    # Hugging Face tokens
    ("hf_token", re.compile(r"hf_[A-Za-z0-9]{20,}")),
    # GitHub tokens
    ("github_token", re.compile(r"(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("github_pat_token", re.compile(r"github_pat_[A-Za-z0-9]{22,}_[A-Za-z0-9]{59,}")),
    # PyPI tokens
    ("pypi_token", re.compile(r"pypi-[A-Za-z0-9_-]{50,}")),
    # NPM tokens
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{30,}")),
    # AWS access key IDs (but not in regex pattern context)
    ("aws_key", re.compile(r"(?<![A-Za-z0-9\[])AKIA[0-9A-Z]{16}(?![0-9A-Z\]{}])")),
    # AWS secret keys (40 chars, mixed case + special) - allow suffixed names like _GUTENBERG
    (
        "aws_secret",
        re.compile(
            r"(?:aws_secret_access_key\w*|secret_key)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # Slack tokens
    ("slack_token", re.compile(r"xox[bpsa]-[A-Za-z0-9-]{20,}")),
    # Discord webhook URLs (contain a secret token in the path)
    ("discord_webhook", re.compile(r"https?://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    # Private keys
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    # Generic secret references across CLI flags, URL params, JSON, and assignments
    (
        "generic_secret",
        re.compile(
            rf"""
            (?<![A-Za-z0-9])
            {_GENERIC_SECRET_NAME_PATTERN}
            ['"]?
            (?:\s*[=:]\s*|\s+)
            ['"]?
            [A-Za-z0-9_/+=.-]{{16,}}
            ['"]?
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    # Bearer tokens in headers (JWT and non-JWT)
    ("bearer", re.compile(r"Bearer\s+([A-Za-z0-9_/+=.-]{20,})")),
    # IP addresses (public, non-loopback, non-private-by-default)
    (
        "ip_address",
        re.compile(
            r"\b(?!127\.0\.0\.)(?!0\.0\.0\.0)(?!255\.255\.)"
            r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
    ),
    # Passwords pasted after a keyword (English/Chinese), on same or next line
    (
        "password_value",
        re.compile(
            r"(?:password|passwd|密码)\s*[=:]?\s*\n?\s*([A-Za-z0-9_/+=.-]{8,})\b",
            re.IGNORECASE,
        ),
    ),
    # Email addresses (for PII removal) - require at least 2-char local part
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Long base64-like strings in quotes (checked for entropy - see scan_text)
    ("high_entropy", re.compile(r"""['"][A-Za-z0-9_/+=.-]{40,}['"]""")),
]

ALLOWLIST = [
    re.compile(r"noreply@"),
    re.compile(r"@example\.com"),
    re.compile(r"@localhost"),
    re.compile(r"@anthropic\.com"),
    re.compile(r"@github\.com"),
    re.compile(r"@users\.noreply\.github\.com"),
    re.compile(r"AKIA\["),  # regex patterns about AWS keys
    re.compile(r"sk-ant-\.\*"),  # regex patterns about API keys
    re.compile(r"postgres://user:pass@"),  # example/documentation URLs
    re.compile(r"postgres://username:password@"),
    re.compile(r"@pytest"),  # Python decorator false positives
    re.compile(r"@tasks\."),
    re.compile(r"@mcp\."),
    re.compile(r"@server\."),
    re.compile(r"@app\."),
    re.compile(r"@router\."),
    re.compile(r"192\.168\."),  # private IPs (low risk)
    re.compile(r"10\.\d+\.\d+\.\d+"),
    re.compile(r"172\.(?:1[6-9]|2\d|3[01])\."),
    re.compile(r"8\.8\.8\.8"),  # Google DNS
    re.compile(r"8\.8\.4\.4"),
    re.compile(r"1\.1\.1\.1"),  # Cloudflare DNS
]

_BASE64_BLOB_RE = re.compile(r"(?:[A-Za-z0-9+/]{4}){1024,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BINARY_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0E-\x1F]")
_WHITESPACE_RE = re.compile(r"\s+")


def should_skip_large_binary_string(text: str) -> bool:
    """Return True for large base64/binary-like payloads we should not rewrite."""
    if not text or len(text) < 4096:
        return False

    sample = text[:8192]
    if sample.startswith("data:") and "base64," in sample[:128]:
        return True

    # ANSI color/control sequences are common in long terminal output and should
    # still be treated as text, not binary blobs.
    sample_without_ansi = sample if "\x1b" not in sample else _ANSI_ESCAPE_RE.sub("", sample)
    if _BINARY_CONTROL_CHAR_RE.search(sample_without_ansi):
        return True

    compact = (
        sample_without_ansi
        if _WHITESPACE_RE.search(sample_without_ansi) is None
        else _WHITESPACE_RE.sub("", sample_without_ansi)
    )
    if len(compact) < 4096:
        return False
    return _BASE64_BLOB_RE.fullmatch(compact) is not None


def should_skip_structured_string_transform(
    key: str | None,
    value: str,
    parent_dict: dict[str, Any] | None,
) -> bool:
    """Return True for schema-marked binary/document payload strings we should not rewrite."""
    if key == "data" and isinstance(parent_dict, dict) and parent_dict.get("type") == "base64":
        return True
    if key == "url" and value.startswith("data:"):
        return True
    return False


def contains_large_binary_value(value: Any) -> bool:
    if isinstance(value, str):
        return should_skip_large_binary_string(value)
    if isinstance(value, dict):
        return any(contains_large_binary_value(child_value) for child_value in value.values())
    if isinstance(value, list):
        return any(contains_large_binary_value(item) for item in value)
    return False


def summarize_large_binary_value(value: Any) -> Any:
    if isinstance(value, str) and should_skip_large_binary_string(value):
        return {"type": "large_blob", "length": len(value)}
    if isinstance(value, dict):
        return {key: summarize_large_binary_value(child_value) for key, child_value in value.items()}
    if isinstance(value, list):
        return [summarize_large_binary_value(item) for item in value]
    return value


def _shannon_entropy(s: str) -> float:
    """Higher values indicate more random-looking strings."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _has_mixed_char_types(s: str) -> bool:
    """Check if string has a mix of uppercase, lowercase, and digits."""
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_upper and has_lower and has_digit


def _build_marker_automaton(markers: tuple[str, ...]):
    automaton = ahocorasick.Automaton()
    for marker in markers:
        automaton.add_word(marker, marker)
    automaton.make_automaton()
    return automaton


_FAST_PATH_CASE_AUTOMATON = _build_marker_automaton(_FAST_PATH_CASE_MARKERS)
_FAST_PATH_LOWER_AUTOMATON = _build_marker_automaton(_FAST_PATH_LOWER_MARKERS)


class _FastPathState:
    __slots__ = ("text", "_case_markers", "_lower_markers")

    def __init__(self, text: str):
        self.text = text
        self._case_markers: set[str] | None = None
        self._lower_markers: set[str] | None = None

    def case_markers(self) -> set[str]:
        if self._case_markers is None:
            self._case_markers = {marker for _, marker in _FAST_PATH_CASE_AUTOMATON.iter(self.text)}
        return self._case_markers

    def lower_markers(self) -> set[str]:
        if self._lower_markers is None:
            self._lower_markers = {marker for _, marker in _FAST_PATH_LOWER_AUTOMATON.iter(self.text.lower())}
        return self._lower_markers


def _has_any_marker(found_markers: set[str], needles: tuple[str, ...]) -> bool:
    return any(needle in found_markers for needle in needles)


def _pattern_may_match(name: str, state: _FastPathState) -> bool:
    text = state.text
    if name in ("jwt", "jwt_partial"):
        return "eyJ" in state.case_markers()
    if name == "db_url":
        return "postgres" in state.lower_markers()
    if name == "anthropic_key":
        return "sk-ant-" in state.case_markers()
    if name == "openai_key":
        return "sk-" in state.case_markers()
    if name == "google_api_key":
        return "AIzaSy" in state.case_markers()
    if name == "groq_key":
        return "gsk_" in state.case_markers()
    if name == "telegram_token":
        return ":" in text
    if name == "flyio_token":
        case_markers = state.case_markers()
        return "fm1_" in case_markers or "fm2_" in case_markers
    if name == "eth_private_key":
        return "0x" in state.case_markers()
    if name == "hf_token":
        return "hf_" in state.case_markers()
    if name == "github_token":
        return _has_any_marker(state.case_markers(), ("ghp_", "gho_", "ghs_", "ghr_"))
    if name == "github_pat_token":
        return "github_pat_" in state.case_markers()
    if name == "pypi_token":
        return "pypi-" in state.case_markers()
    if name == "npm_token":
        return "npm_" in state.case_markers()
    if name == "aws_key":
        return "AKIA" in state.case_markers()
    if name == "aws_secret":
        if "=" not in text and ":" not in text:
            return False
        return _has_any_marker(state.lower_markers(), ("secret_key", "aws_secret_access_key"))
    if name == "slack_token":
        return "xox" in state.case_markers()
    if name == "discord_webhook":
        return "discord" in state.case_markers()
    if name == "private_key":
        return "PRIVATE KEY" in state.case_markers()
    if name == "generic_secret":
        if (
            "-" not in text
            and "=" not in text
            and ":" not in text
            and "?" not in text
            and "&" not in text
            and " " not in text
        ):
            return False
        return _has_any_marker(state.lower_markers(), _GENERIC_SECRET_MARKERS)
    if name == "bearer":
        return "Bearer" in state.case_markers()
    if name == "ip_address":
        return "." in text
    if name == "password_value":
        case_markers = state.case_markers()
        lower_markers = state.lower_markers()
        return "password" in lower_markers or "passwd" in lower_markers or "密码" in case_markers
    if name == "email":
        return "@" in text
    if name == "high_entropy":
        return '"' in text or "'" in text
    return True


def scan_text(text: str) -> list[dict]:
    if not text:
        return []

    findings = []
    fast_path_state = _FastPathState(text)
    for name, pattern in SECRET_PATTERNS:
        if not _pattern_may_match(name, fast_path_state):
            continue
        for match in pattern.finditer(text):
            matched_text = match.group(0)

            if any(allow_pat.search(matched_text) for allow_pat in ALLOWLIST):
                continue

            # For high_entropy, verify string actually looks like a secret
            if name == "high_entropy":
                inner = matched_text[1:-1]  # strip quotes
                if not _has_mixed_char_types(inner):
                    continue
                if _shannon_entropy(inner) < 3.5:
                    continue
                if inner.count(".") > 2:
                    continue

            findings.append(
                {
                    "type": name,
                    "start": match.start(),
                    "end": match.end(),
                    "match": matched_text,
                }
            )

    return findings


def redact_text(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    if should_skip_large_binary_string(text):
        return text, 0

    findings = scan_text(text)
    if not findings:
        return text, 0

    # Sort by position (descending start) to replace without shifting indices
    findings.sort(key=lambda f: f["start"], reverse=True)

    # Deduplicate overlapping findings (keep the later-starting match on overlap)
    deduped = []
    for f in findings:
        if not deduped or f["end"] <= deduped[-1]["start"]:
            deduped.append(f)

    # Replace from end-to-start (deduped is already in descending start order)
    result = text
    for f in deduped:
        result = result[: f["start"]] + REDACTED + result[f["end"] :]

    return result, len(deduped)


def redact_custom_strings(text: str, strings: list[str]) -> tuple[str, int]:
    if not text or not strings:
        return text, 0

    original_text = text
    count = 0
    for target in strings:
        if not target or len(target) < 3:
            continue
        escaped = re.escape(target)
        pattern = rf"\b{escaped}\b" if len(target) >= 4 else escaped
        text, replacements = re.subn(pattern, REDACTED, text)
        count += replacements

    if count == 0:
        return original_text, 0
    return text, count


def _redact_value(
    value: Any,
    custom_strings: list[str] | None = None,
    key: str | None = None,
    parent_dict: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    """Recursively redact secrets from a string, list, or dict value."""
    if isinstance(value, str):
        if should_skip_structured_string_transform(key, value, parent_dict):
            return value, 0
        if should_skip_large_binary_string(value):
            return value, 0
        result, count = redact_text(value)
        if custom_strings:
            result, n = redact_custom_strings(result, custom_strings)
            count += n
        return result, count
    if isinstance(value, dict):
        total = 0
        out: dict[Any, Any] | None = None
        for k, v in value.items():
            redacted, n = _redact_value(v, custom_strings, k, value)
            total += n
            if out is None:
                if n == 0 and redacted is v:
                    continue
                out = dict(value)
            out[k] = redacted
        if out is None:
            return value, 0
        return out, total
    if isinstance(value, list):
        total = 0
        out_list: list[Any] | None = None
        for idx, item in enumerate(value):
            redacted, n = _redact_value(item, custom_strings, key, parent_dict)
            total += n
            if out_list is None:
                if n == 0 and redacted is item:
                    continue
                out_list = list(value[:idx])
            out_list.append(redacted)
        if out_list is None:
            return value, 0
        return out_list, total
    return value, 0


def redact_session(session: dict, custom_strings: list[str] | None = None) -> tuple[dict, int]:
    """Redact all secrets in a session dict. Returns (redacted_session, total_redactions)."""
    total = 0

    for msg in session.get("messages", []):
        for field in ("content", "thinking"):
            if msg.get(field):
                msg[field], count = redact_text(msg[field])
                total += count
                if custom_strings:
                    msg[field], count = redact_custom_strings(msg[field], custom_strings)
                    total += count
        if msg.get("content_parts"):
            msg["content_parts"], count = _redact_value(msg["content_parts"], custom_strings)
            total += count
        for tool_use in msg.get("tool_uses", []):
            for field in ("input", "output"):
                if tool_use.get(field):
                    tool_use[field], count = _redact_value(tool_use[field], custom_strings)
                    total += count

    return session, total
