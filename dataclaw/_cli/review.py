"""Review, PII scan, and confirm helpers for the DataClaw CLI."""

import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .. import _json as json
from .._workers import configured_workers
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

_PII_SCANS = {
    "emails": re.compile(r"[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}"),
    "jwt_tokens": re.compile(r"eyJ[A-Za-z0-9_-]{20,}"),
    "api_keys": re.compile(r"(ghp_|sk-|hf_)[A-Za-z0-9_-]{10,}"),
    "ip_addresses": re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"),
}
_PII_FALSE_POSITIVE_EMAIL_SUBSTRINGS = frozenset(
    {"noreply", "pytest.fixture", "mcp.tool", "mcp.resource", "server.tool", "tasks.loop", "github.com"}
)
_PII_FALSE_POSITIVE_API_KEYS = frozenset({"sk-notification"})
_REVIEW_MIN_PARALLEL_BYTES = 16 * 1024 * 1024
_REVIEW_MIN_CHUNK_BYTES = 8 * 1024 * 1024


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
                "hint": "Run Step 4 first to generate a local export file.",
                "blocked_on_step": "Step 4/6",
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
        if len(token) > 512:
            continue
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
    matches_by_scan: dict[str, set[str]] = {name: set() for name in _PII_SCANS}
    high_entropy_matches: dict[str, dict] = {}

    try:
        with open(file_path) as f:
            for line_no, line in enumerate(f, start=1):
                _update_pii_matches(line, matches_by_scan, high_entropy_matches, line_no=line_no)
    except OSError:
        return {}

    return _finalize_pii_results(matches_by_scan, high_entropy_matches)


def _update_pii_matches(
    line: str,
    matches_by_scan: dict[str, set[str]],
    high_entropy_matches: dict[str, dict],
    *,
    line_no: int | None = None,
) -> None:
    for name, pattern in _PII_SCANS.items():
        matches_by_scan[name].update(pattern.findall(line))

    for result in _scan_high_entropy_strings(line, max_results=50):
        if line_no is not None:
            result = {**result, "_line_no": line_no}
        existing = high_entropy_matches.get(result["match"])
        existing_line = existing.get("_line_no", sys.maxsize) if isinstance(existing, dict) else sys.maxsize
        result_line = result.get("_line_no", sys.maxsize)
        if (
            existing is None
            or result["entropy"] > existing["entropy"]
            or (result["entropy"] == existing["entropy"] and result_line < existing_line)
        ):
            high_entropy_matches[result["match"]] = result


def _finalize_pii_results(matches_by_scan: dict[str, set[str]], high_entropy_matches: dict[str, dict]) -> dict:
    results = {}
    for name, matches in matches_by_scan.items():
        if name == "emails":
            matches = {
                match for match in matches if not any(fp in match for fp in _PII_FALSE_POSITIVE_EMAIL_SUBSTRINGS)
            }
        if name == "api_keys":
            matches = {match for match in matches if match not in _PII_FALSE_POSITIVE_API_KEYS}
        if matches:
            results[name] = sorted(matches)[:20]

    high_entropy = sorted(
        high_entropy_matches.values(),
        key=lambda result: (-result["entropy"], result.get("_line_no", sys.maxsize)),
    )[:15]
    if high_entropy:
        results["high_entropy_strings"] = [
            {key: value for key, value in result.items() if key != "_line_no"} for result in high_entropy
        ]

    return results


def _format_occurrence_excerpt(line: str, max_len: int = 220) -> str:
    excerpt = line.strip()
    if len(excerpt) > max_len:
        return f"{excerpt[:max_len]}..."
    return excerpt


def _record_text_occurrence(
    line_no: int,
    line: str,
    pattern: re.Pattern[str],
    examples: list[dict[str, object]],
    *,
    max_examples: int,
) -> int:
    if not pattern.search(line):
        return 0

    if len(examples) < max_examples:
        examples.append({"line": line_no, "excerpt": _format_occurrence_excerpt(line)})
    return 1


def _resolve_review_workers(file_size: int, workers: int | None = None) -> int:
    if workers is not None:
        return max(1, workers)

    if file_size < _REVIEW_MIN_PARALLEL_BYTES:
        return 1

    workers = configured_workers()

    if workers is None:
        workers = os.cpu_count() or 1

    max_by_size = max(1, (file_size + _REVIEW_MIN_CHUNK_BYTES - 1) // _REVIEW_MIN_CHUNK_BYTES)
    return max(1, min(workers, max_by_size))


def _plan_review_chunks(file_path: Path, workers: int) -> list[tuple[int, int, int]]:
    file_size = file_path.stat().st_size
    if file_size <= 0 or workers <= 1:
        return [(0, file_size, 1)]

    target_bytes = max(file_size // workers, 1)
    chunks: list[tuple[int, int, int]] = []
    start_offset = 0
    start_line = 1
    offset = 0
    line_no = 1
    chunk_bytes = 0

    with file_path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            for byte in block:
                offset += 1
                chunk_bytes += 1
                if byte != 0x0A:
                    continue
                line_no += 1
                if chunk_bytes >= target_bytes and len(chunks) < workers - 1:
                    chunks.append((start_offset, offset, start_line))
                    start_offset = offset
                    start_line = line_no
                    chunk_bytes = 0

    if start_offset < offset or not chunks:
        chunks.append((start_offset, offset, start_line))
    return chunks


def _scan_review_chunk(payload: tuple[str, int, int, int, str | None, int]) -> dict:
    file_path_str, start_offset, end_offset, start_line, full_name_query, max_examples = payload
    full_name_pattern = re.compile(re.escape(full_name_query), re.IGNORECASE) if full_name_query else None
    matches_by_scan: dict[str, set[str]] = {name: set() for name in _PII_SCANS}
    high_entropy_matches: dict[str, dict] = {}
    full_name_matches = 0
    full_name_examples: list[dict[str, object]] = []
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0

    with open(file_path_str, "rb") as handle:
        handle.seek(start_offset)
        line_no = start_line
        while handle.tell() < end_offset:
            raw_line = handle.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

            if full_name_pattern is not None:
                full_name_matches += _record_text_occurrence(
                    line_no,
                    line,
                    full_name_pattern,
                    full_name_examples,
                    max_examples=max_examples,
                )

            _update_pii_matches(line, matches_by_scan, high_entropy_matches, line_no=line_no)

            stripped = line.strip()
            if stripped:
                row = json.loads(stripped)
                total += 1
                project = row.get("project", "<unknown>")
                projects[project] = projects.get(project, 0) + 1
                model = row.get("model", "<unknown>")
                models[model] = models.get(model, 0) + 1

            line_no += 1

    return {
        "total_sessions": total,
        "projects": projects,
        "models": models,
        "matches_by_scan": matches_by_scan,
        "high_entropy_matches": high_entropy_matches,
        "full_name_matches": full_name_matches,
        "full_name_examples": full_name_examples,
    }


def _merge_review_chunk_results(
    results: list[dict],
    full_name_query: str | None = None,
    max_examples: int = 5,
) -> dict:
    matches_by_scan: dict[str, set[str]] = {name: set() for name in _PII_SCANS}
    high_entropy_matches: dict[str, dict] = {}
    full_name_matches = 0
    full_name_examples: list[dict[str, object]] = []
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0

    for result in results:
        total += result["total_sessions"]
        full_name_matches += result["full_name_matches"]
        full_name_examples.extend(result["full_name_examples"])

        for name, matches in result["matches_by_scan"].items():
            matches_by_scan[name].update(matches)

        for token, candidate in result["high_entropy_matches"].items():
            existing = high_entropy_matches.get(token)
            existing_line = existing.get("_line_no", sys.maxsize) if isinstance(existing, dict) else sys.maxsize
            candidate_line = candidate.get("_line_no", sys.maxsize)
            if (
                existing is None
                or candidate["entropy"] > existing["entropy"]
                or (candidate["entropy"] == existing["entropy"] and candidate_line < existing_line)
            ):
                high_entropy_matches[token] = candidate

        for project, count in result["projects"].items():
            projects[project] = projects.get(project, 0) + count
        for model, count in result["models"].items():
            models[model] = models.get(model, 0) + count

    merged = {
        "total_sessions": total,
        "projects": projects,
        "models": models,
        "pii_scan": _finalize_pii_results(matches_by_scan, high_entropy_matches),
    }
    if full_name_query is not None:
        full_name_examples.sort(key=lambda example: example["line"])
        merged["full_name_scan"] = {
            "query": full_name_query,
            "match_count": full_name_matches,
            "examples": full_name_examples[:max_examples],
        }
    return merged


def _scan_export_review_serial(file_path: Path, full_name_query: str | None = None, max_examples: int = 5) -> dict:
    full_name_pattern = None
    if full_name_query:
        full_name_pattern = re.compile(re.escape(full_name_query), re.IGNORECASE)

    matches_by_scan: dict[str, set[str]] = {name: set() for name in _PII_SCANS}
    high_entropy_matches: dict[str, dict] = {}
    full_name_matches = 0
    full_name_examples: list[dict[str, object]] = []
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0

    with open(file_path) as f:
        for line_no, line in enumerate(f, start=1):
            if full_name_pattern is not None:
                full_name_matches += _record_text_occurrence(
                    line_no,
                    line,
                    full_name_pattern,
                    full_name_examples,
                    max_examples=max_examples,
                )

            _update_pii_matches(line, matches_by_scan, high_entropy_matches, line_no=line_no)

            line = line.strip()
            if not line:
                continue

            row = json.loads(line)
            total += 1
            project = row.get("project", "<unknown>")
            projects[project] = projects.get(project, 0) + 1
            model = row.get("model", "<unknown>")
            models[model] = models.get(model, 0) + 1

    result = {
        "total_sessions": total,
        "projects": projects,
        "models": models,
        "pii_scan": _finalize_pii_results(matches_by_scan, high_entropy_matches),
    }
    if full_name_pattern is not None:
        result["full_name_scan"] = {
            "query": full_name_query,
            "match_count": full_name_matches,
            "examples": full_name_examples,
        }
    return result


def _scan_export_review(
    file_path: Path, full_name_query: str | None = None, max_examples: int = 5, workers: int | None = None
) -> dict:
    resolved_workers = _resolve_review_workers(file_path.stat().st_size, workers)
    if resolved_workers <= 1:
        return _scan_export_review_serial(file_path, full_name_query, max_examples)

    chunks = _plan_review_chunks(file_path, resolved_workers)
    payloads = [
        (str(file_path), start_offset, end_offset, start_line, full_name_query, max_examples)
        for start_offset, end_offset, start_line in chunks
    ]

    with ProcessPoolExecutor(max_workers=resolved_workers) as executor:
        results = list(executor.map(_scan_review_chunk, payloads, chunksize=1))
    return _merge_review_chunk_results(results, full_name_query, max_examples)


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
        with open(file_path) as f:
            for line_no, line in enumerate(f, start=1):
                matches += _record_text_occurrence(
                    line_no,
                    line,
                    pattern,
                    examples,
                    max_examples=max_examples,
                )
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
    start_time = time.perf_counter()
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
                    "blocked_on_step": "Step 5/6",
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
                    "blocked_on_step": "Step 5/6",
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
                    "blocked_on_step": "Step 5/6",
                    "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                    "next_command": CONFIRM_COMMAND_EXAMPLE,
                },
                indent=2,
            )
        )
        sys.exit(1)

    try:
        review_scan = _scan_export_review(
            file_path,
            None if skip_full_name_scan else normalized_full_name,
        )
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Cannot read {file_path}: {e}"}))
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
        full_name_scan = review_scan["full_name_scan"]

    file_size = file_path.stat().st_size
    repo_id = config.get("repo")
    pii_findings = review_scan["pii_scan"]
    projects = review_scan["projects"]
    models = review_scan["models"]
    total = review_scan["total_sessions"]

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

    next_steps = [
        "Step 5 - Review and confirm: show the user the project breakdown, full-name scan, and PII scan results above."
    ]
    if full_name_scan.get("skipped"):
        next_steps.append(
            "Step 5 - Review and confirm: full-name scan was skipped at user request. Ensure this was explicitly reviewed with the user."
        )
    elif full_name_scan.get("match_count", 0):
        next_steps.append(
            "Step 5 - Review and confirm: full-name scan found matches. Review them with the user and redact if needed, then repeat Step 4 with --no-push."
        )
    if pii_findings:
        next_steps.append(
            "Step 5 - Review and confirm: PII findings detected - review each one with the user. "
            'If real: dataclaw config --redact "string" then repeat Step 4 with --no-push. '
            "False positives can be ignored."
        )
    if "high_entropy_strings" in pii_findings:
        next_steps.append(
            "Step 5 - Review and confirm: high-entropy strings detected - these may be leaked secrets (API keys, tokens, "
            "passwords) that escaped automatic redaction. Review each one using the provided "
            "context snippets. If any are real secrets, redact with: "
            'dataclaw config --redact "the_secret" then repeat Step 4 with --no-push.'
        )
    next_steps.extend(
        [
            'Step 5 - Review and confirm: if any project should be excluded, run dataclaw config --exclude "project_name" and repeat Step 4 with --no-push.',
            f"Step 6 - Publish: this will publish {total} sessions ({_format_size(file_size)}) publicly to Hugging Face"
            + (f" at {repo_id}" if repo_id else "")
            + ". Ask the user: 'Are you ready to proceed?'",
            'Once confirmed, push with dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
        ]
    )

    result = {
        "stage": "confirmed",
        "stage_number": 3,
        "total_stages": 4,
        "elapsed": f"{time.perf_counter() - start_time:.2f}s",
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

Step 5 next: ask for full name to run an exact-name privacy check, then scan for it:
  grep -i 'THEIR_NAME' {abs_output} | head -10
  If user declines sharing full name: use dataclaw confirm --skip-full-name-scan with a skip attestation.

If Step 5 finds anything sensitive, set redactions and repeat Step 4:
  dataclaw config --redact-usernames 'github_handle,discord_name'
  dataclaw config --redact 'secret-domain.com,my-api-key'
  dataclaw export --no-push -o {abs_output}

Found an issue? Help improve DataClaw: {repo_url}/issues
"""
    print(message.rstrip())
