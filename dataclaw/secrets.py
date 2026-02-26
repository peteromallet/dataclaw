"""Detect and redact secrets in conversation data."""

import math
import re
from typing import Any

REDACTED = "[REDACTED]"

# Ordered from most specific to least specific
SECRET_PATTERNS = [
    # JWT tokens — full 3-segment form
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),

    # JWT tokens — partial (header only or header+partial payload, e.g. truncated)
    ("jwt_partial", re.compile(r"eyJ[A-Za-z0-9_-]{15,}")),

    # PostgreSQL/database connection strings with passwords
    ("db_url", re.compile(r"postgres(?:ql)?://[^:]+:[^@\s]+@[^\s\"'`]+")),

    # Anthropic API keys
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),

    # OpenAI API keys
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{40,}")),

    # Hugging Face tokens
    ("hf_token", re.compile(r"hf_[A-Za-z0-9]{20,}")),

    # GitHub tokens
    ("github_token", re.compile(r"(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}")),

    # PyPI tokens
    ("pypi_token", re.compile(r"pypi-[A-Za-z0-9_-]{50,}")),

    # NPM tokens
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{30,}")),

    # AWS access key IDs (but not in regex pattern context)
    ("aws_key", re.compile(r"(?<![A-Za-z0-9\[])AKIA[0-9A-Z]{16}(?![0-9A-Z\]{}])")),

    # AWS secret keys (40 chars, mixed case + special)
    ("aws_secret", re.compile(
        r"(?:aws_secret_access_key|secret_key)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?",
        re.IGNORECASE,
    )),

    # Slack tokens
    ("slack_token", re.compile(r"xox[bpsa]-[A-Za-z0-9-]{20,}")),

    # Discord webhook URLs (contain a secret token in the path)
    ("discord_webhook", re.compile(
        r"https?://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"
    )),

    # Private keys
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    )),

    # CLI flags that pass tokens/secrets: --token VALUE, --access-token VALUE, etc.
    ("cli_token_flag", re.compile(
        r"(?:--|-)(?:access[_-]?token|auth[_-]?token|api[_-]?key|secret|password|token)"
        r"[\s=]+([A-Za-z0-9_/+=.-]{8,})",
        re.IGNORECASE,
    )),

    # Environment variable assignments with secret-like names (with or without quotes)
    ("env_secret", re.compile(
        r"(?:SECRET|PASSWORD|TOKEN|API_KEY|AUTH_KEY|ACCESS_KEY|SERVICE_KEY|DB_PASSWORD"
        r"|SUPABASE_KEY|SUPABASE_SERVICE|ANON_KEY|SERVICE_ROLE)"
        r"\s*[=]\s*['\"]?([^\s'\"]{6,})['\"]?",
        re.IGNORECASE,
    )),

    # Generic secret assignments: SECRET_KEY = "value", api_key: "value", etc.
    ("generic_secret", re.compile(
        r"""(?:secret[_-]?key|api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token"""
        r"""|service[_-]?role[_-]?key|private[_-]?key)"""
        r"""\s*[=:]\s*['"]([A-Za-z0-9_/+=.-]{20,})['"]""",
        re.IGNORECASE,
    )),

    # Bearer tokens in headers
    ("bearer", re.compile(
        r"Bearer\s+(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
    )),

    # IP addresses (public, non-loopback, non-private-by-default)
    ("ip_address", re.compile(
        r"\b(?!127\.0\.0\.)(?!0\.0\.0\.0)(?!255\.255\.)"
        r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
    )),

    # URL query params with secrets: ?key=VALUE, &token=VALUE, etc.
    ("url_token", re.compile(
        r"[?&](?:key|token|secret|password|apikey|api_key|access_token|auth)"
        r"=([A-Za-z0-9_/+=.-]{8,})",
        re.IGNORECASE,
    )),

    # Email addresses (for PII removal) — require at least 2-char local part
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),

    # Long base64-like strings in quotes (checked for entropy — see scan_text)
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


def scan_text(text: str) -> list[dict]:
    if not text:
        return []

    findings = []
    for name, pattern in SECRET_PATTERNS:
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

            findings.append({
                "type": name,
                "start": match.start(),
                "end": match.end(),
                "match": matched_text,
            })

    return findings


def redact_text(text: str) -> tuple[str, int]:
    if not text:
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
        result = result[:f["start"]] + REDACTED + result[f["end"]:]

    return result, len(deduped)


def redact_custom_strings(text: str, strings: list[str]) -> tuple[str, int]:
    if not text or not strings:
        return text, 0

    count = 0
    for target in strings:
        if not target or len(target) < 3:
            continue
        escaped = re.escape(target)
        pattern = rf"\b{escaped}\b" if len(target) >= 4 else escaped
        text, replacements = re.subn(pattern, REDACTED, text)
        count += replacements

    return text, count


def _redact_value(value: Any, custom_strings: list[str] | None = None) -> tuple[Any, int]:
    """Recursively redact secrets from a string, list, or dict value."""
    if isinstance(value, str):
        result, count = redact_text(value)
        if custom_strings:
            result, n = redact_custom_strings(result, custom_strings)
            count += n
        return result, count
    if isinstance(value, dict):
        total = 0
        out = {}
        for k, v in value.items():
            out[k], n = _redact_value(v, custom_strings)
            total += n
        return out, total
    if isinstance(value, list):
        total = 0
        out_list = []
        for item in value:
            redacted, n = _redact_value(item, custom_strings)
            out_list.append(redacted)
            total += n
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
        for tool_use in msg.get("tool_uses", []):
            for field in ("input", "output"):
                if tool_use.get(field):
                    tool_use[field], count = _redact_value(tool_use[field], custom_strings)
                    total += count

    return session, total
