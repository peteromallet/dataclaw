"""Shared CLI constants and helpers."""

import hashlib
import logging
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .. import _json as json
from ..config import DataClawConfig
from ..providers import PROVIDERS

logger = logging.getLogger(__name__)

_HASH_READ_CHUNK = 1024 * 1024  # 1 MiB


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 and return the hex digest.

    Used to fingerprint a confirmed export so we can detect modification
    between `confirm` and `publish`.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_strings(values: Iterable[str]) -> str:
    """Stable SHA-256 fingerprint of a string collection.

    Used to detect when a redaction list shrinks (loosens) between exports
    without storing the list contents (which could themselves be sensitive).
    """
    h = hashlib.sha256()
    for value in sorted(values):
        h.update(value.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class CLIBlockedError(Exception):
    """Raised when the CLI must stop because a process-gating precondition is not met.

    Caught at the CLI entry point, which prints the structured payload and exits with code 1.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", ""))
        self.payload = payload


def emit_blocked_error(
    error: str,
    *,
    hint: str | None = None,
    blocked_on_step: str | None = None,
    process_steps: Any = None,
    next_command: str | None = None,
    **extra: Any,
) -> NoReturn:
    """Raise a CLIBlockedError carrying the structured error payload."""
    payload: dict[str, Any] = {"error": error}
    if hint is not None:
        payload["hint"] = hint
    if blocked_on_step is not None:
        payload["blocked_on_step"] = blocked_on_step
    if process_steps is not None:
        payload["process_steps"] = process_steps
    if next_command is not None:
        payload["next_command"] = next_command
    payload.update(extra)
    raise CLIBlockedError(payload)


def format_elapsed_seconds(seconds: float) -> str:
    return f"{seconds:.2f}s"


def emit_progress_event(msg: str, phase: str, extra: Mapping[str, Any] | None = None) -> None:
    """Emit a structured progress event without touching stdout JSON contracts."""
    payload = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "msg": msg,
        "phase": phase,
        "extra": dict(extra or {}),
    }
    print(json.dumps(payload), file=sys.stderr, flush=True)


class ProgressReporter:
    """Throttle parent-process progress events for long loops."""

    def __init__(
        self,
        msg: str,
        phase: str,
        total: int | None,
        *,
        interval_seconds: float = 2.0,
        base_extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.msg = msg
        self.phase = phase
        self.total = total
        self.interval_seconds = interval_seconds
        self.base_extra = dict(base_extra or {})
        self._last_emit_at: float | None = None
        self._last_current: int | None = None

    def emit(self, current: int, *, force: bool = False, extra: Mapping[str, Any] | None = None) -> bool:
        now = time.monotonic()
        is_final = self.total is not None and self.total >= 0 and current >= self.total
        should_emit = (
            force
            or self._last_emit_at is None
            or is_final
            or now - self._last_emit_at >= self.interval_seconds
        )
        if not should_emit or (not force and self._last_current == current):
            return False

        payload = dict(self.base_extra)
        payload.update(extra or {})
        payload["current"] = current
        if self.total is not None:
            payload["total"] = self.total
        emit_progress_event(self.msg, self.phase, payload)
        self._last_emit_at = now
        self._last_current = current
        return True

HF_TAG = "dataclaw"
HF_DATASETS_URL = "https://huggingface.co/datasets"
HF_JOIN_URL = "https://huggingface.co/join"
HF_TOKEN_SETTINGS_URL = "https://huggingface.co/settings/tokens"
REPO_URL = "https://github.com/peteromallet/dataclaw"
SKILL_URL = "https://raw.githubusercontent.com/peteromallet/dataclaw/main/.claude/skills/dataclaw/SKILL.md"


def hf_dataset_url(repo_id: str) -> str:
    return f"{HF_DATASETS_URL}/{repo_id}"


def hf_browse_tagged_url(tag: str = HF_TAG) -> str:
    return f"{HF_DATASETS_URL}?other={tag}"

REQUIRED_REVIEW_ATTESTATIONS: dict[str, str] = {
    "asked_full_name": "I asked the user for their full name and scanned for it.",
    "asked_sensitive_entities": "I asked about company/client/internal names and private URLs.",
    "manual_scan_done": "I performed a manual sample scan of exported sessions.",
}
MIN_ATTESTATION_CHARS = 24
MIN_MANUAL_SCAN_SESSIONS = 20

CONFIRM_COMMAND_EXAMPLE = (
    "dataclaw confirm "
    '--full-name "THEIR FULL NAME" '
    '--attest-full-name "Asked for full name and scanned export for THEIR FULL NAME." '
    '--attest-sensitive "Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed." '
    '--attest-manual-scan "Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user."'
)

CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE = (
    "dataclaw confirm "
    "--skip-full-name-scan "
    '--attest-full-name "User declined to share full name; skipped exact-name scan." '
    '--attest-sensitive "Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed." '
    '--attest-manual-scan "Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user."'
)

EXPORT_REVIEW_PUBLISH_STEPS = [
    "Step 1 - Install: pip install -U dataclaw",
    "Step 2 - Install skill (Claude Code only): dataclaw update-skill claude",
    "Step 3 - Prep: dataclaw prep",
    "Step 3A - Choose source scope: dataclaw config --source <source|all>",
    'Step 3B - Choose project scope: dataclaw list --source all, then dataclaw config --exclude "p1,p2" or dataclaw config --confirm-projects',
    'Step 3C - Set redacted strings: dataclaw config --redact "string1,string2" and dataclaw config --redact-usernames "user1,user2"',
    "Step 4 - Export locally: dataclaw export --no-push --output dataclaw_export.jsonl",
    "Step 5 - Review and confirm: dataclaw confirm ...",
    'Step 6 - Publish after explicit approval: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
]

PROVIDER_SOURCES = tuple(PROVIDERS)
DEFAULT_SOURCE = PROVIDER_SOURCES[0]
EXPLICIT_SOURCE_CHOICES = set(PROVIDER_SOURCES) | {"all", "both"}
SOURCE_CHOICES = ["auto", *PROVIDER_SOURCES, "all"]


def _mask_secret(s: str) -> str:
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _mask_config_for_display(config: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(config)
    if out.get("redact_strings"):
        out["redact_strings"] = [_mask_secret(s) for s in out["redact_strings"]]
    return out


def _format_human_list(items: list[str], conjunction: str = "or") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return f"{', '.join(items[:-1])}, {conjunction} {items[-1]}"


def _all_provider_labels() -> str:
    return _format_human_list([provider.source for provider in PROVIDERS.values()])


def _source_scope_choices(include_aliases: bool = False) -> list[str]:
    choices = [*PROVIDER_SOURCES, "all"]
    if include_aliases:
        choices.append("both")
    return choices


def _source_scope_placeholder() -> str:
    return f"<{'|'.join(_source_scope_choices())}>"


def _source_scope_literals() -> str:
    return _format_human_list([f"`{choice}`" for choice in _source_scope_choices()])


def _setup_to_publish_steps() -> list[str]:
    return list(EXPORT_REVIEW_PUBLISH_STEPS)


def _provider_dataset_tags() -> str:
    return "\n".join(f"  - {provider.hf_metadata_tag}" for provider in PROVIDERS.values())


def _normalize_source_filter(source_filter: str) -> str:
    if source_filter in ("all", "both"):
        return "auto"
    return source_filter


def _source_label(source_filter: str) -> str:
    source_filter = _normalize_source_filter(source_filter)
    provider = PROVIDERS.get(source_filter)
    if provider:
        return provider.source
    return _all_provider_labels()


def _is_explicit_source_choice(source_filter: str | None) -> bool:
    return source_filter in EXPLICIT_SOURCE_CHOICES


def _resolve_source_choice(
    requested_source: str,
    config: DataClawConfig | None = None,
) -> tuple[str, bool]:
    if _is_explicit_source_choice(requested_source):
        return requested_source, True
    if config:
        configured_source = config.get("source")
        if _is_explicit_source_choice(configured_source):
            return str(configured_source), True
    return "auto", False


def _has_session_sources(source_filter: str = "auto") -> bool:
    source_filter = _normalize_source_filter(source_filter)
    provider = PROVIDERS.get(source_filter)
    if provider:
        return provider.has_session_source()
    return any(provider.has_session_source() for provider in PROVIDERS.values())


def _filter_projects_by_source(projects: list[dict], source_filter: str) -> list[dict]:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "auto":
        return projects
    return [project for project in projects if project.get("source", DEFAULT_SOURCE) == source_filter]


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _format_token_count(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def get_hf_username() -> str | None:
    from huggingface_hub import HfApi

    try:
        return HfApi().whoami()["name"]
    except Exception as e:  # noqa: BLE001 - auth/network probing must not break status/prep
        logger.warning("Could not fetch HuggingFace username: %s", e)
        return None


def default_repo_name(hf_username: str) -> str:
    return f"{hf_username}/my-personal-codex-data"


def _compute_stage(config: DataClawConfig) -> tuple[str, int, str | None]:
    hf_user = get_hf_username()
    if not hf_user:
        return ("auth", 1, None)
    saved = config.get("stage")
    last_export = config.get("last_export")
    if saved == "done" and last_export:
        return ("done", 4, hf_user)
    if saved == "confirmed" and last_export:
        return ("confirmed", 3, hf_user)
    if saved == "review" and last_export:
        return ("review", 3, hf_user)
    return ("configure", 2, hf_user)


def _build_status_next_steps(
    stage: str,
    config: DataClawConfig,
    hf_user: str | None,
    repo_id: str | None,
) -> tuple[list[str], str | None]:
    if stage == "auth":
        return (
            [
                f"Before Step 3 - Prep: ask the user for their Hugging Face token. Sign up: {HF_JOIN_URL} - Create WRITE token: {HF_TOKEN_SETTINGS_URL}",
                "Run: hf auth login --token <THEIR_TOKEN> (NEVER run bare hf auth login when automating this with an agent - it hangs)",
                'Run: dataclaw config --redact "<THEIR_TOKEN>" (so the token gets redacted from exports)',
                "Step 3 - Prep: run dataclaw prep (to confirm login and get next steps)",
            ],
            None,
        )

    if stage == "configure":
        projects_confirmed = config.get("projects_confirmed", False)
        configured_source = config.get("source")
        source_confirmed = _is_explicit_source_choice(configured_source)
        list_command = f"dataclaw list --source {configured_source}" if source_confirmed else "dataclaw list"
        steps = []
        if not source_confirmed:
            steps.append(
                f"Step 3A - Choose source scope: ask the user to explicitly choose {_all_provider_labels()} or all. "
                f"Then set it: dataclaw config --source {_source_scope_placeholder()}. "
                "Do not run export until source scope is explicitly confirmed."
            )
        else:
            steps.append(
                f"Step 3A - Choose source scope: source scope is currently set to '{configured_source}'. "
                f"If the user wants a different scope, run: dataclaw config --source {_source_scope_placeholder()}."
            )
        if not projects_confirmed:
            steps.append(
                f"Step 3B - Choose project scope: run {list_command}, then send the FULL project/folder list to the user in your next message "
                "(name, source, sessions, size, excluded), and ask which to EXCLUDE."
            )
            steps.append(
                'Step 3B - Choose project scope: run dataclaw config --exclude "project1,project2" '
                "or dataclaw config --confirm-projects (to include all listed projects). "
                "Do not run export until this folder review is confirmed."
            )
        steps.extend(
            [
                "Step 3C - Set redacted strings: ask about GitHub/Discord usernames to anonymize and sensitive strings to redact. "
                'Configure with dataclaw config --redact-usernames "handle1" and dataclaw config --redact "string1".',
                "Step 4 - Export locally: dataclaw export --no-push --output dataclaw_export.jsonl",
            ]
        )
        return (steps, None)

    if stage == "review":
        return (
            [
                "Step 5 - Review and confirm: ask the user for their full name to run an exact-name privacy check against the export. If they decline, you may skip this check with --skip-full-name-scan and include a clear attestation.",
                "Run PII scan commands and review results with the user.",
                "Ask the user whether there are any company names, internal project names, client names, private URLs, other people's names, custom domains, or internal tools that should be redacted. Add anything they mention with dataclaw config --redact.",
                "Do a deep manual scan of about 20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials, and anything else that looks sensitive. Report findings to the user.",
                "If PII is found in any of the above, update redactions (dataclaw config --redact) and repeat Step 4: dataclaw export --no-push",
                "Run: " + CONFIRM_COMMAND_EXAMPLE + " - scans for PII, shows project breakdown, and unlocks pushing.",
                'Do NOT push until the user explicitly confirms. Once confirmed, push: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
            ],
            "dataclaw confirm",
        )

    if stage == "confirmed":
        return (
            [
                "Step 6 - Publish: the user has reviewed the export. Ask 'Ready to publish to Hugging Face?' and push with dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\".",
            ],
            "dataclaw export",
        )

    dataset_url = hf_dataset_url(repo_id) if repo_id else None
    return (
        [
            f"Done! Dataset is live{f' at {dataset_url}' if dataset_url else ''}. To update later, repeat Steps 3 through 6: dataclaw prep, reconfigure as needed, export locally, confirm, then publish.",
        ],
        None,
    )


def _merge_config_list(config: DataClawConfig, key: str, new_values: list[str]) -> None:
    existing = set(config.get(key, []))
    existing.update(new_values)
    config[key] = sorted(existing)


def _parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]
