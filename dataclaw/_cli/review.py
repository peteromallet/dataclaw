"""Review, PII scan, and confirm helpers for the DataClaw CLI."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import _json as json
from ..config import DataClawConfig
from ..secrets import _has_mixed_char_types, _shannon_entropy
from .common import (
    CONFIRM_COMMAND_EXAMPLE,
    CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
    EXPORT_REVIEW_PUBLISH_STEPS,
    MIN_ATTESTATION_CHARS,
    MIN_MANUAL_SCAN_SESSIONS,
    REQUIRED_REVIEW_ATTESTATIONS,
    _format_size,
)


def _find_export_file(file_path: Path | None) -> Path:
    if file_path and file_path.exists():
        return file_path
    if file_path is None:
        for candidate in [Path("dataclaw_export.jsonl"), Path("dataclaw_conversations.jsonl")]:
            if candidate.exists():
                return candidate
    print(
        json.dumps(
            {
                "error": "No export file found.",
                "hint": "Run step 1 first to generate a local export file.",
                "blocked_on_step": "Step 1/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": "dataclaw export --no-push --output dataclaw_export.jsonl",
            },
            indent=2,
        )
    )
    sys.exit(1)


def _scan_high_entropy_strings(content: str, max_results: int = 15) -> list[dict]:
    if not content:
        return []

    candidate_re = re.compile(r"[A-Za-z0-9_/+=.-]{20,}")
    known_prefixes = ("eyJ", "ghp_", "gho_", "ghs_", "ghr_", "sk-", "hf_", "AKIA", "pypi-", "npm_", "xox")
    benign_prefixes = ("https://", "http://", "sha256-", "sha384-", "sha512-", "sha1-", "data:", "file://", "mailto:")
    benign_substrings = (
        "node_modules",
        "[REDACTED]",
        "package-lock",
        "webpack",
        "babel",
        "eslint",
        ".chunk.",
        "vendor/",
        "dist/",
        "build/",
    )
    file_extensions = (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".rst",
        ".txt",
        ".sh",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".swift",
        ".kt",
        ".lock",
        ".cfg",
        ".ini",
        ".xml",
        ".svg",
        ".png",
        ".jpg",
        ".gif",
        ".woff",
        ".ttf",
        ".map",
        ".vue",
        ".scss",
        ".less",
        ".sql",
        ".env",
        ".log",
    )
    hex_re = re.compile(r"^[0-9a-fA-F]+$")
    uuid_re = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")

    unique_candidates: dict[str, list[int]] = {}
    for match in candidate_re.finditer(content):
        token = match.group(0)
        unique_candidates.setdefault(token, []).append(match.start())

    results = []
    for token, positions in unique_candidates.items():
        if any(token.startswith(prefix) for prefix in known_prefixes):
            continue
        if hex_re.match(token) or uuid_re.match(token):
            continue
        token_lower = token.lower()
        if any(ext in token_lower for ext in file_extensions):
            continue
        if token.count("/") >= 2 or token.count(".") >= 3:
            continue
        if any(token_lower.startswith(prefix) for prefix in benign_prefixes):
            continue
        if any(substring in token_lower for substring in benign_substrings):
            continue
        if not _has_mixed_char_types(token):
            continue

        entropy = _shannon_entropy(token)
        if entropy < 4.0:
            continue

        pos = positions[0]
        context = content[max(0, pos - 40) : min(len(content), pos + len(token) + 40)].replace("\n", " ")
        results.append({"match": token, "entropy": round(entropy, 2), "context": context})

    results.sort(key=lambda result: result["entropy"], reverse=True)
    return results[:max_results]


def _scan_pii(file_path: Path) -> dict:
    scans = {
        "emails": r"[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}",
        "jwt_tokens": r"eyJ[A-Za-z0-9_-]{20,}",
        "api_keys": r"(ghp_|sk-|hf_)[A-Za-z0-9_-]{10,}",
        "ip_addresses": r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}",
    }
    fp_emails = {"noreply", "pytest.fixture", "mcp.tool", "mcp.resource", "server.tool", "tasks.loop", "github.com"}
    fp_keys = {"sk-notification"}

    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return {}

    results = {}
    for name, pattern in scans.items():
        matches = set(re.findall(pattern, content))
        if name == "emails":
            matches = {match for match in matches if not any(fp in match for fp in fp_emails)}
        if name == "api_keys":
            matches = {match for match in matches if match not in fp_keys}
        if matches:
            results[name] = sorted(matches)[:20]

    high_entropy = _scan_high_entropy_strings(content)
    if high_entropy:
        results["high_entropy_strings"] = high_entropy

    return results


def _normalize_attestation_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return " ".join(str(value).split()).strip()


def _extract_manual_scan_sessions(attestation: str) -> int | None:
    numbers = [int(number) for number in re.findall(r"\b(\d+)\b", attestation)]
    return max(numbers) if numbers else None


def _scan_for_text_occurrences(file_path: Path, query: str, max_examples: int = 5) -> dict[str, object]:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = 0
    examples: list[dict[str, object]] = []
    try:
        with open(file_path, errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                if pattern.search(line):
                    matches += 1
                    if len(examples) < max_examples:
                        excerpt = line.strip()
                        if len(excerpt) > 220:
                            excerpt = f"{excerpt[:220]}..."
                        examples.append({"line": line_no, "excerpt": excerpt})
    except OSError as e:
        return {"query": query, "match_count": 0, "examples": [], "error": str(e)}
    return {"query": query, "match_count": matches, "examples": examples}


def _collect_review_attestations(
    attest_asked_full_name: object,
    attest_asked_sensitive: object,
    attest_manual_scan: object,
    full_name: str | None,
    skip_full_name_scan: bool = False,
) -> tuple[dict[str, str], dict[str, str], int | None]:
    provided = {
        "asked_full_name": _normalize_attestation_text(attest_asked_full_name),
        "asked_sensitive_entities": _normalize_attestation_text(attest_asked_sensitive),
        "manual_scan_done": _normalize_attestation_text(attest_manual_scan),
    }
    errors: dict[str, str] = {}

    full_name_attestation = provided["asked_full_name"]
    if len(full_name_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_full_name"] = "Provide a detailed text attestation for full-name review."
    else:
        lower = full_name_attestation.lower()
        if skip_full_name_scan:
            mentions_skip = any(token in lower for token in ("skip", "skipped", "declined", "opt out", "prefer not"))
            if "full name" not in lower or not mentions_skip:
                errors["asked_full_name"] = (
                    "When skipping full-name scan, attestation must say the user declined/skipped full name."
                )
        else:
            full_name_lower = (full_name or "").lower()
            full_name_tokens = [token for token in re.split(r"\s+", full_name_lower) if len(token) > 1]
            if "ask" not in lower or "scan" not in lower:
                errors["asked_full_name"] = (
                    "Full-name attestation must mention that you asked the user and scanned the export."
                )
            elif full_name_tokens and not all(token in lower for token in full_name_tokens):
                errors["asked_full_name"] = (
                    "Full-name attestation must reference the same full name passed in --full-name."
                )

    sensitive_attestation = provided["asked_sensitive_entities"]
    if len(sensitive_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_sensitive_entities"] = "Provide a detailed text attestation for sensitive-entity review."
    else:
        lower = sensitive_attestation.lower()
        asked = "ask" in lower
        topics = any(token in lower for token in ("company", "client", "internal", "url", "domain", "tool", "name"))
        outcome = any(token in lower for token in ("none", "no", "redact", "added", "updated", "configured"))
        if not asked or not topics or not outcome:
            errors["asked_sensitive_entities"] = (
                "Sensitive attestation must say what you asked and the outcome (none found or redactions updated)."
            )

    manual_attestation = provided["manual_scan_done"]
    manual_sessions = _extract_manual_scan_sessions(manual_attestation)
    if len(manual_attestation) < MIN_ATTESTATION_CHARS:
        errors["manual_scan_done"] = "Provide a detailed text attestation for the manual scan."
    else:
        lower = manual_attestation.lower()
        if "manual" not in lower or "scan" not in lower:
            errors["manual_scan_done"] = "Manual scan attestation must explicitly mention a manual scan."
        elif manual_sessions is None or manual_sessions < MIN_MANUAL_SCAN_SESSIONS:
            errors["manual_scan_done"] = (
                f"Manual scan attestation must include a reviewed-session count >= {MIN_MANUAL_SCAN_SESSIONS}."
            )

    return provided, errors, manual_sessions


def _validate_publish_attestation(attestation: object) -> tuple[str, str | None]:
    normalized = _normalize_attestation_text(attestation)
    if len(normalized) < MIN_ATTESTATION_CHARS:
        return normalized, "Provide a detailed text publish attestation."
    lower = normalized.lower()
    if "approv" not in lower or ("publish" not in lower and "push" not in lower):
        return normalized, "Publish attestation must state that the user explicitly approved publishing/pushing."
    return normalized, None


def confirm(
    file_path: Path | None = None,
    full_name: str | None = None,
    attest_asked_full_name: str | None = None,
    attest_asked_sensitive: str | None = None,
    attest_manual_scan: str | None = None,
    skip_full_name_scan: bool = False,
    *,
    load_config_fn,
    save_config_fn,
) -> None:
    config: DataClawConfig = load_config_fn()
    last_export = config.get("last_export", {})
    file_path = _find_export_file(file_path)

    normalized_full_name = _normalize_attestation_text(full_name)
    if skip_full_name_scan and normalized_full_name:
        print(
            json.dumps(
                {
                    "error": "Use either --full-name or --skip-full-name-scan, not both.",
                    "hint": (
                        "Provide --full-name for an exact-name scan, or use --skip-full-name-scan "
                        "if the user declines sharing their name."
                    ),
                    "blocked_on_step": "Step 2/3",
                    "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                    "next_command": CONFIRM_COMMAND_EXAMPLE,
                },
                indent=2,
            )
        )
        sys.exit(1)
    if not normalized_full_name and not skip_full_name_scan:
        print(
            json.dumps(
                {
                    "error": "Missing required --full-name for verification scan.",
                    "hint": (
                        "Ask the user for their full name and pass it via --full-name "
                        "to run an exact-name privacy check. If the user declines, rerun with "
                        "--skip-full-name-scan and a full-name attestation describing the skip."
                    ),
                    "blocked_on_step": "Step 2/3",
                    "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                    "next_command": CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
                },
                indent=2,
            )
        )
        sys.exit(1)

    attestations, attestation_errors, manual_scan_sessions = _collect_review_attestations(
        attest_asked_full_name=attest_asked_full_name,
        attest_asked_sensitive=attest_asked_sensitive,
        attest_manual_scan=attest_manual_scan,
        full_name=normalized_full_name if normalized_full_name else None,
        skip_full_name_scan=skip_full_name_scan,
    )
    if attestation_errors:
        print(
            json.dumps(
                {
                    "error": "Missing or invalid review attestations.",
                    "attestation_errors": attestation_errors,
                    "required_attestations": REQUIRED_REVIEW_ATTESTATIONS,
                    "blocked_on_step": "Step 2/3",
                    "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                    "next_command": CONFIRM_COMMAND_EXAMPLE,
                },
                indent=2,
            )
        )
        sys.exit(1)

    if skip_full_name_scan:
        full_name_scan = {
            "query": None,
            "match_count": 0,
            "examples": [],
            "skipped": True,
            "reason": "User declined sharing full name; exact-name scan skipped.",
        }
    else:
        full_name_scan = _scan_for_text_occurrences(file_path, normalized_full_name)

    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0
    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                total += 1
                project = row.get("project", "<unknown>")
                projects[project] = projects.get(project, 0) + 1
                model = row.get("model", "<unknown>")
                models[model] = models.get(model, 0) + 1
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Cannot read {file_path}: {e}"}))
        sys.exit(1)

    file_size = file_path.stat().st_size
    repo_id = config.get("repo")
    pii_findings = _scan_pii(file_path)

    config["stage"] = "confirmed"
    config["review_attestations"] = attestations
    config["review_verification"] = {
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    config["last_confirm"] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "file": str(file_path.resolve()),
        "pii_findings": bool(pii_findings),
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    save_config_fn(config)

    next_steps = ["Show the user the project breakdown, full-name scan, and PII scan results above."]
    if full_name_scan.get("skipped"):
        next_steps.append(
            "Full-name scan was skipped at user request. Ensure this was explicitly reviewed with the user."
        )
    elif full_name_scan.get("match_count", 0):
        next_steps.append(
            "Full-name scan found matches. Review them with the user and redact if needed, then re-export with --no-push."
        )
    if pii_findings:
        next_steps.append(
            "PII findings detected — review each one with the user. "
            'If real: dataclaw config --redact "string" then re-export with --no-push. '
            "False positives can be ignored."
        )
    if "high_entropy_strings" in pii_findings:
        next_steps.append(
            "High-entropy strings detected — these may be leaked secrets (API keys, tokens, "
            "passwords) that escaped automatic redaction. Review each one using the provided "
            "context snippets. If any are real secrets, redact with: "
            'dataclaw config --redact "the_secret" then re-export with --no-push.'
        )
    next_steps.extend(
        [
            'If any project should be excluded, run: dataclaw config --exclude "project_name" and re-export with --no-push.',
            f"This will publish {total} sessions ({_format_size(file_size)}) publicly to Hugging Face"
            + (f" at {repo_id}" if repo_id else "")
            + ". Ask the user: 'Are you ready to proceed?'",
            'Once confirmed, push: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
        ]
    )

    result = {
        "stage": "confirmed",
        "stage_number": 3,
        "total_stages": 4,
        "file": str(file_path.resolve()),
        "file_size": _format_size(file_size),
        "total_sessions": total,
        "projects": [
            {"name": name, "sessions": count} for name, count in sorted(projects.items(), key=lambda x: -x[1])
        ],
        "models": {model: count for model, count in sorted(models.items(), key=lambda x: -x[1])},
        "pii_scan": pii_findings if pii_findings else "clean",
        "full_name_scan": full_name_scan,
        "manual_scan_sessions": manual_scan_sessions,
        "repo": repo_id,
        "last_export_timestamp": last_export.get("timestamp"),
        "next_steps": next_steps,
        "next_command": 'dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
        "attestations": attestations,
    }
    print(json.dumps(result, indent=2))


def _build_pii_commands(output_path: Path) -> list[str]:
    p = str(output_path.resolve())
    return [
        f"grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {p} | grep -v noreply | head -20",
        f"grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {p} | head -5",
        f"grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {p} | head -5",
        f"grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {p} | sort -u",
    ]


def _print_pii_guidance(output_path: Path, repo_url: str) -> None:
    abs_output = output_path.resolve()
    message = f"""
{"=" * 50}
  IMPORTANT: Review your data before publishing!
{"=" * 50}
DataClaw's automatic redaction is NOT foolproof.
You should scan the exported data for remaining PII.

Quick checks (run these and review any matches):
  grep -i 'your_name' {abs_output}
  grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {abs_output} | grep -v noreply | head -20
  grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {abs_output} | head -5
  grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {abs_output} | head -5
  grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {abs_output} | sort -u

NEXT: Ask for full name to run an exact-name privacy check, then scan for it:
  grep -i 'THEIR_NAME' {abs_output} | head -10
  If user declines sharing full name: use dataclaw confirm --skip-full-name-scan with a skip attestation.

To add custom redactions, then re-export:
  dataclaw config --redact-usernames 'github_handle,discord_name'
  dataclaw config --redact 'secret-domain.com,my-api-key'
  dataclaw export --no-push -o {abs_output}

Found an issue? Help improve DataClaw: {repo_url}/issues
"""
    print(message.rstrip())
