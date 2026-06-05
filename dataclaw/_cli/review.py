"""Review, PII scan, and confirm helpers for the DataClaw CLI."""

import logging
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .. import _json as json
from .._workers import configured_workers
from ..config import DataClawConfig
from ..secrets import has_mixed_char_types, shannon_entropy

logger = logging.getLogger(__name__)
from .common import (
    CONFIRM_COMMAND_EXAMPLE,
    CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
    EXPORT_REVIEW_PUBLISH_STEPS,
    MIN_ATTESTATION_CHARS,
    MIN_MANUAL_SCAN_SESSIONS,
    REQUIRED_REVIEW_ATTESTATIONS,
    _format_size,
    emit_blocked_error,
    emit_progress_event,
    fingerprint_strings,
    ProgressReporter,
    sha256_file,
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
    emit_blocked_error(
        "No export file found.",
        hint="Run Step 4 first to generate a local export file.",
        blocked_on_step="Step 4/6",
        process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
        next_command="dataclaw export --no-push --output dataclaw_export.jsonl",
    )


_ENTROPY_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_/+=.-]{20,}")
_ENTROPY_KNOWN_PREFIXES = ("eyJ", "ghp_", "gho_", "ghs_", "ghr_", "sk-", "hf_", "AKIA", "pypi-", "npm_", "xox")
_ENTROPY_BENIGN_PREFIXES = ("https://", "http://", "sha256-", "sha384-", "sha512-", "sha1-", "data:", "file://", "mailto:")
_ENTROPY_BENIGN_SUBSTRINGS = (
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
_ENTROPY_FILE_EXTENSIONS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".json", ".yaml", ".yml",
    ".toml", ".md", ".rst", ".txt", ".sh", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".swift", ".kt", ".lock", ".cfg", ".ini", ".xml",
    ".svg", ".png", ".jpg", ".gif", ".woff", ".ttf", ".map", ".vue", ".scss",
    ".less", ".sql", ".env", ".log",
)
_ENTROPY_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_ENTROPY_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def _scan_high_entropy_strings(content: str, max_results: int = 15) -> list[dict]:
    if not content:
        return []

    unique_candidates: dict[str, list[int]] = {}
    for match in _ENTROPY_CANDIDATE_RE.finditer(content):
        token = match.group(0)
        unique_candidates.setdefault(token, []).append(match.start())

    results = []
    for token, positions in unique_candidates.items():
        if len(token) > 512:
            continue
        if any(token.startswith(prefix) for prefix in _ENTROPY_KNOWN_PREFIXES):
            continue
        if _ENTROPY_HEX_RE.match(token) or _ENTROPY_UUID_RE.match(token):
            continue
        token_lower = token.lower()
        if any(ext in token_lower for ext in _ENTROPY_FILE_EXTENSIONS):
            continue
        if token.count("/") >= 2 or token.count(".") >= 3:
            continue
        if any(token_lower.startswith(prefix) for prefix in _ENTROPY_BENIGN_PREFIXES):
            continue
        if any(substring in token_lower for substring in _ENTROPY_BENIGN_SUBSTRINGS):
            continue
        if not has_mixed_char_types(token):
            continue

        entropy = shannon_entropy(token)
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
    except OSError as e:
        logger.warning("PII scan failed to read %s: %s", file_path, e)
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


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _strip_diacritics(text: str) -> str:
    """NFD-decompose, drop combining marks, collapse to NFC. Lossy by design."""
    nfd = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", "".join(c for c in nfd if unicodedata.category(c) != "Mn"))


def _build_full_name_patterns(query: str | None) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """Return (nfc_pattern, stripped_pattern). Empty / None query → (None, None)."""
    if not query:
        return None, None
    nfc_query = _nfc(query)
    stripped_query = _strip_diacritics(nfc_query)
    nfc_pat = re.compile(re.escape(nfc_query), re.IGNORECASE)
    # Only build the stripped pattern when it actually differs — avoids double-counting matches
    # for queries that contain no diacritics.
    stripped_pat: re.Pattern[str] | None = None
    if stripped_query and stripped_query != nfc_query:
        stripped_pat = re.compile(re.escape(stripped_query), re.IGNORECASE)
    return nfc_pat, stripped_pat


def _record_full_name_occurrence(
    line_no: int,
    line: str,
    nfc_pattern: re.Pattern[str] | None,
    stripped_pattern: re.Pattern[str] | None,
    examples: list[dict[str, object]],
    *,
    max_examples: int,
) -> int:
    """Return 1 if the line matches the name in either NFC or diacritics-stripped form, else 0."""
    if nfc_pattern is None and stripped_pattern is None:
        return 0
    nfc_line = _nfc(line)
    matched = False
    if nfc_pattern is not None and nfc_pattern.search(nfc_line):
        matched = True
    elif stripped_pattern is not None and stripped_pattern.search(_strip_diacritics(nfc_line)):
        matched = True
    if not matched:
        return 0
    if len(examples) < max_examples:
        examples.append({"line": line_no, "excerpt": _format_occurrence_excerpt(line)})
    return 1


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
    nfc_pattern, stripped_pattern = _build_full_name_patterns(full_name_query)
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

            if nfc_pattern is not None or stripped_pattern is not None:
                full_name_matches += _record_full_name_occurrence(
                    line_no,
                    line,
                    nfc_pattern,
                    stripped_pattern,
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
    nfc_pattern, stripped_pattern = _build_full_name_patterns(full_name_query)

    matches_by_scan: dict[str, set[str]] = {name: set() for name in _PII_SCANS}
    high_entropy_matches: dict[str, dict] = {}
    full_name_matches = 0
    full_name_examples: list[dict[str, object]] = []
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0

    with open(file_path) as f:
        for line_no, line in enumerate(f, start=1):
            if nfc_pattern is not None or stripped_pattern is not None:
                full_name_matches += _record_full_name_occurrence(
                    line_no,
                    line,
                    nfc_pattern,
                    stripped_pattern,
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
    if nfc_pattern is not None or stripped_pattern is not None:
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
        emit_progress_event(
            "pii_scan_started",
            "mechanical_pii",
            {"current": 0, "total": 1, "chunks": 1, "mode": "serial"},
        )
        result = _scan_export_review_serial(file_path, full_name_query, max_examples)
        emit_progress_event(
            "pii_scan_progress",
            "mechanical_pii",
            {"current": 1, "total": 1, "chunks": 1, "mode": "serial", "sessions": result["total_sessions"]},
        )
        emit_progress_event(
            "pii_scan_finished",
            "mechanical_pii",
            {"current": 1, "total": 1, "chunks": 1, "mode": "serial", "sessions": result["total_sessions"]},
        )
        return result

    chunks = _plan_review_chunks(file_path, resolved_workers)
    payloads = [
        (str(file_path), start_offset, end_offset, start_line, full_name_query, max_examples)
        for start_offset, end_offset, start_line in chunks
    ]
    emit_progress_event(
        "pii_scan_started",
        "mechanical_pii",
        {"current": 0, "total": len(payloads), "chunks": len(payloads), "mode": "parallel", "workers": resolved_workers},
    )
    progress = ProgressReporter(
        "pii_scan_progress",
        "mechanical_pii",
        len(payloads),
        base_extra={"chunks": len(payloads), "mode": "parallel", "workers": resolved_workers},
    )

    with ProcessPoolExecutor(max_workers=resolved_workers) as executor:
        futures = [executor.submit(_scan_review_chunk, payload) for payload in payloads]
        results = []
        for current, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            progress.emit(current)
    merged = _merge_review_chunk_results(results, full_name_query, max_examples)
    progress.emit(len(payloads), force=True, extra={"sessions": merged["total_sessions"]})
    emit_progress_event(
        "pii_scan_finished",
        "mechanical_pii",
        {"current": len(payloads), "total": len(payloads), "chunks": len(payloads), "sessions": merged["total_sessions"]},
    )
    return merged


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


_SESSION_SHRINK_RELATIVE_THRESHOLD = 0.05  # 5%
_SESSION_SHRINK_SMALL_PRIOR_THRESHOLD = 20  # below this, any decrease blocks


def _validate_acceptance_attestation(text: str | None, flag_name: str) -> tuple[str | None, str | None]:
    """Normalize an --accept-* attestation; return (normalized, error_or_None)."""
    if text is None:
        return None, None
    normalized = _normalize_attestation_text(text)
    if not normalized:
        return None, f"{flag_name} attestation cannot be empty."
    if len(normalized) < MIN_ATTESTATION_CHARS:
        return (
            None,
            f"{flag_name} attestation must be at least {MIN_ATTESTATION_CHARS} characters (got {len(normalized)}).",
        )
    return normalized, None


def _session_shrink_blocks(previous: int, current: int) -> bool:
    """Decide whether a drop in session count is large enough to block confirm."""
    if previous <= 0 or current >= previous:
        return False
    if previous <= _SESSION_SHRINK_SMALL_PRIOR_THRESHOLD:
        return True
    return (previous - current) / previous >= _SESSION_SHRINK_RELATIVE_THRESHOLD


def confirm(
    file_path: Path | None = None,
    full_name: str | None = None,
    attest_asked_full_name: str | None = None,
    attest_asked_sensitive: str | None = None,
    attest_manual_scan: str | None = None,
    skip_full_name_scan: bool = False,
    accept_full_name_matches: str | None = None,
    accept_session_shrink: str | None = None,
    accept_redaction_drift: str | None = None,
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
        emit_blocked_error(
            "Use either --full-name or --skip-full-name-scan, not both.",
            hint=(
                "Provide --full-name for an exact-name scan, or use --skip-full-name-scan "
                "if the user declines sharing their name."
            ),
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
        )
    if not normalized_full_name and not skip_full_name_scan:
        emit_blocked_error(
            "Missing required --full-name for verification scan.",
            hint=(
                "Ask the user for their full name and pass it via --full-name "
                "to run an exact-name privacy check. If the user declines, rerun with "
                "--skip-full-name-scan and a full-name attestation describing the skip."
            ),
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
        )

    # Validate accept-* attestations early so we can short-circuit with clear errors.
    accept_full_name_matches_norm, fn_err = _validate_acceptance_attestation(
        accept_full_name_matches, "--accept-full-name-matches"
    )
    accept_session_shrink_norm, ss_err = _validate_acceptance_attestation(
        accept_session_shrink, "--accept-session-shrink"
    )
    accept_redaction_drift_norm, rd_err = _validate_acceptance_attestation(
        accept_redaction_drift, "--accept-redaction-drift"
    )
    acceptance_errors = {
        key: msg
        for key, msg in (
            ("accept_full_name_matches", fn_err),
            ("accept_session_shrink", ss_err),
            ("accept_redaction_drift", rd_err),
        )
        if msg
    }
    if acceptance_errors:
        emit_blocked_error(
            "Invalid acceptance attestation.",
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
            acceptance_errors=acceptance_errors,
        )

    attestations, attestation_errors, manual_scan_sessions = _collect_review_attestations(
        attest_asked_full_name=attest_asked_full_name,
        attest_asked_sensitive=attest_asked_sensitive,
        attest_manual_scan=attest_manual_scan,
        full_name=normalized_full_name if normalized_full_name else None,
        skip_full_name_scan=skip_full_name_scan,
    )
    if attestation_errors:
        emit_blocked_error(
            "Missing or invalid review attestations.",
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
            attestation_errors=attestation_errors,
            required_attestations=REQUIRED_REVIEW_ATTESTATIONS,
        )

    try:
        review_scan = _scan_export_review(
            file_path,
            None if skip_full_name_scan else normalized_full_name,
        )
    except (OSError, json.JSONDecodeError) as e:
        emit_blocked_error(f"Cannot read {file_path}: {e}")

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

    # Gate: full-name scan matches must be explicitly accepted.
    full_name_match_count = int(full_name_scan.get("match_count") or 0)
    if full_name_match_count > 0 and not accept_full_name_matches_norm:
        emit_blocked_error(
            f"Full-name scan found {full_name_match_count} match(es). Confirm is blocked.",
            hint=(
                "Two ways forward: (a) redact the matches with `dataclaw config --redact \"...\"` "
                "then re-export with `dataclaw export --no-push` and re-run confirm; OR "
                "(b) re-run confirm with `--accept-full-name-matches \"<reason this is acceptable to publish>\"`."
            ),
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
            full_name_scan=full_name_scan,
        )

    # Gate: session-count shrink vs previous export must be explicitly accepted.
    previous_sessions = int(last_export.get("sessions") or 0)
    shrink_blocks = _session_shrink_blocks(previous_sessions, total)
    if shrink_blocks and not accept_session_shrink_norm:
        delta = previous_sessions - total
        delta_pct = (delta / previous_sessions) if previous_sessions else 0.0
        emit_blocked_error(
            f"This export has {total} sessions; the previous export had {previous_sessions} ({delta} fewer).",
            hint=(
                "If source session directories were intentionally cleaned, re-run with "
                "`--accept-session-shrink \"<reason for the drop>\"`. Otherwise investigate why sessions "
                "are missing before publishing."
            ),
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
            shrink_warning={
                "previous_sessions": previous_sessions,
                "current_sessions": total,
                "delta": delta,
                "delta_pct": round(delta_pct, 4),
            },
        )

    # Gate: redaction-policy loosening (entries removed) must be explicitly accepted.
    redact_strings = config.get("redact_strings", []) or []
    redact_usernames = config.get("redact_usernames", []) or []
    current_strings_fp = fingerprint_strings(redact_strings)
    current_usernames_fp = fingerprint_strings(redact_usernames)
    current_strings_count = len(redact_strings)
    current_usernames_count = len(redact_usernames)

    prev_strings_fp = last_export.get("redact_strings_fingerprint")
    prev_usernames_fp = last_export.get("redact_usernames_fingerprint")
    prev_strings_count = int(last_export.get("redact_strings_count") or 0)
    prev_usernames_count = int(last_export.get("redact_usernames_count") or 0)

    strings_drifted = (
        prev_strings_fp is not None
        and prev_strings_fp != current_strings_fp
        and current_strings_count < prev_strings_count
    )
    usernames_drifted = (
        prev_usernames_fp is not None
        and prev_usernames_fp != current_usernames_fp
        and current_usernames_count < prev_usernames_count
    )
    if (strings_drifted or usernames_drifted) and not accept_redaction_drift_norm:
        emit_blocked_error(
            "Redaction list shrank since the previous export — confirm is blocked.",
            hint=(
                "Adding redactions tightens privacy and is safe. Removing them loosens it. "
                "If the removal is intentional, re-run with "
                "`--accept-redaction-drift \"<reason for the removal>\"`. Otherwise restore the entry "
                "via `dataclaw config --redact \"...\"` or `--redact-usernames \"...\"` before continuing."
            ),
            blocked_on_step="Step 5/6",
            process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
            next_command=CONFIRM_COMMAND_EXAMPLE,
            redaction_drift_warning={
                "redact_strings": {
                    "previous_count": prev_strings_count,
                    "current_count": current_strings_count,
                    "shrunk": strings_drifted,
                },
                "redact_usernames": {
                    "previous_count": prev_usernames_count,
                    "current_count": current_usernames_count,
                    "shrunk": usernames_drifted,
                },
            },
        )

    # All gates cleared. Hash the file so we can detect modification before publish.
    confirmed_sha256 = sha256_file(file_path)

    config["stage"] = "confirmed"
    if accept_full_name_matches_norm:
        attestations["accepted_full_name_matches"] = accept_full_name_matches_norm
    if accept_session_shrink_norm:
        attestations["accepted_session_shrink"] = accept_session_shrink_norm
    if accept_redaction_drift_norm:
        attestations["accepted_redaction_drift"] = accept_redaction_drift_norm
    config["review_attestations"] = attestations
    review_verification = {
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_match_count,
        "manual_scan_sessions": manual_scan_sessions,
    }
    if accept_full_name_matches_norm:
        review_verification["full_name_matches_accepted"] = accept_full_name_matches_norm
    if accept_session_shrink_norm:
        review_verification["session_shrink_accepted"] = accept_session_shrink_norm
    if accept_redaction_drift_norm:
        review_verification["redaction_drift_accepted"] = accept_redaction_drift_norm
    config["review_verification"] = review_verification
    config["last_confirm"] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "file": str(file_path.resolve()),
        "sha256": confirmed_sha256,
        "size_bytes": file_size,
        "pii_findings": bool(pii_findings),
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_match_count,
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
