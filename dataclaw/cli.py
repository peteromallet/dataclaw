"""CLI for DataClaw — export coding agent conversations to Hugging Face."""

import argparse
import getpass
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from . import logging as dc_logging
from .anonymizer import Anonymizer
from .auth import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    _delete_hf_token,
    _resolve_hf_token,
    _store_hf_token,
)
from .config import CONFIG_FILE, DataClawConfig, load_config, save_config
from .parser import (
    CLAUDE_DIR,
    CODEX_DIR,
    CUSTOM_DIR,
    GEMINI_DIR,
    HERMES_DIR,
    KIMI_DIR,
    OPENCODE_DIR,
    OPENCLAW_DIR,
    _parse_iso,
    discover_projects,
    parse_project_sessions,
)
from .secrets import _has_mixed_char_types, _shannon_entropy, redact_session

HF_TAG = "dataclaw"
STAGING_ROOT = Path.home() / ".dataclaw" / "staging"
PUBLISHED_DIRNAME = "published"
STAGING_SIZE_WARN_BYTES = 5 * 1024 * 1024 * 1024
PUSH_MAX_ATTEMPTS = 3
PUSH_BACKOFFS = (30, 120)
REPO_URL = "https://github.com/banodoco/dataclaw"
SKILL_URL = "https://raw.githubusercontent.com/banodoco/dataclaw/main/docs/SKILL.md"
TOKEN_ESTIMATE_BYTES_PER_TOKEN = 3.6


class PushFailed(Exception):
    def __init__(self, cause: Exception, attempts: int, backoff_seconds_total: float):
        super().__init__(str(cause))
        self.cause = cause
        self.attempts = attempts
        self.backoff_seconds_total = backoff_seconds_total


class _AuthFailed(Exception):
    pass


class PrivacyFilterFailed(Exception):
    pass


class AutoRunAlreadyActive(Exception):
    pass


class _AutoRunLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AutoRunAlreadyActive("another DataClaw auto run is already active") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def __exit__(self, exc_type, exc, tb):
        import fcntl

        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


REQUIRED_REVIEW_ATTESTATIONS: dict[str, str] = {
    "asked_full_name": "I asked the user for their full name and scanned for it.",
    "asked_sensitive_entities": "I asked about company/client/internal names and private URLs.",
    "manual_scan_done": "I performed a manual sample scan of exported sessions.",
}
MIN_ATTESTATION_CHARS = 24
MIN_MANUAL_SCAN_SESSIONS = 20

CONFIRM_COMMAND_EXAMPLE = (
    "dataclaw confirm "
    "--full-name \"THEIR FULL NAME\" "
    "--attest-full-name \"Asked for full name and scanned export for THEIR FULL NAME.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE = (
    "dataclaw confirm "
    "--skip-full-name-scan "
    "--attest-full-name \"User declined to share full name; skipped exact-name scan.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

EXPORT_REVIEW_PUBLISH_STEPS = [
    "Step 1/3: Export locally only: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    "Step 2/3: Review/redact, then run confirm: dataclaw confirm ...",
    "Step 3/3: After explicit user approval, publish: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
]

SETUP_TO_PUBLISH_STEPS = [
    "Step 1/6: Run prep/list to review project scope: dataclaw prep && dataclaw list",
    "Step 2/6: Explicitly choose source scope: dataclaw config --source <claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all>",
    "Step 3/6: Configure exclusions/redactions and confirm projects: dataclaw config ...",
    "Step 4/6: Export locally only: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    "Step 5/6: Review and confirm: dataclaw confirm ...",
    "Step 6/6: After explicit user approval, publish: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
]

EXPLICIT_SOURCE_CHOICES = {"claude", "codex", "custom", "gemini", "kimi", "hermes", "opencode", "openclaw", "all", "both"}
SOURCE_CHOICES = ["auto", "claude", "codex", "custom", "gemini", "kimi", "hermes", "opencode", "openclaw", "all"]
AUTO_MODE_STEPS: list[str] = []
HF_LOGIN_HINT = "Launch DataClaw.app and sign in, OR run `dataclaw hf login --token-stdin`"


def emit_json(payload: Mapping[str, Any]) -> None:
    print("---DATACLAW_JSON---")
    print(json.dumps(payload, indent=2))


def _mask_secret(s: str) -> str:
    """Mask a secret string for display, e.g. 'hf_OOgd...oEVH'."""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _mask_config_for_display(config: Mapping[str, Any], unmask: bool = False) -> dict[str, Any]:
    """Return a copy of config with redact_strings values masked."""
    out = dict(config)
    if not unmask and out.get("redact_strings"):
        out["redact_strings"] = ["***" for _ in out["redact_strings"]]
    return out


def _source_label(source_filter: str) -> str:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "claude":
        return "Claude Code"
    if source_filter == "codex":
        return "Codex"
    if source_filter == "gemini":
        return "Gemini CLI"
    if source_filter == "opencode":
        return "OpenCode"
    if source_filter == "openclaw":
        return "OpenClaw"
    if source_filter == "kimi":
        return "Kimi CLI"
    if source_filter == "hermes":
        return "Hermes Agent"
    if source_filter == "custom":
        return "Custom"
    return "Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, Hermes Agent, or Custom"


def _normalize_source_filter(source_filter: str) -> str:
    if source_filter in ("all", "both"):
        return "auto"
    return source_filter


def _is_explicit_source_choice(source_filter: str | None) -> bool:
    return source_filter in EXPLICIT_SOURCE_CHOICES


def _resolve_source_choice(
    requested_source: str,
    config: DataClawConfig | None = None,
) -> tuple[str, bool]:
    """Resolve source choice from CLI + config.

    Returns:
      (source_choice, explicit) where source_choice is one of
      "claude" | "codex" | "gemini" | "opencode" | "openclaw" | "all" | "auto".
    """
    if _is_explicit_source_choice(requested_source):
        return requested_source, True
    if config:
        configured_source = config.get("source")
        if _is_explicit_source_choice(configured_source):
            return str(configured_source), True
    return "auto", False


def _has_session_sources(source_filter: str = "auto") -> bool:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "claude":
        return CLAUDE_DIR.exists()
    if source_filter == "codex":
        return CODEX_DIR.exists()
    if source_filter == "gemini":
        return GEMINI_DIR.exists()
    if source_filter == "opencode":
        return OPENCODE_DIR.exists()
    if source_filter == "openclaw":
        return OPENCLAW_DIR.exists()
    if source_filter == "kimi":
        return KIMI_DIR.exists()
    if source_filter == "hermes":
        return HERMES_DIR.exists()
    if source_filter == "custom":
        return CUSTOM_DIR.exists()
    return CLAUDE_DIR.exists() or CODEX_DIR.exists() or CUSTOM_DIR.exists() or GEMINI_DIR.exists() or KIMI_DIR.exists() or HERMES_DIR.exists() or OPENCODE_DIR.exists() or OPENCLAW_DIR.exists()


def _filter_projects_by_source(projects: list[dict], source_filter: str) -> list[dict]:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "auto":
        return projects
    return [p for p in projects if p.get("source", "claude") == source_filter]


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


def _format_token_title(count: int) -> str:
    if count <= 0:
        return "0b"
    if count < 100_000_000:
        return f"{count / 1_000_000_000:.2f}b"
    return f"{count / 1_000_000_000:.1f}b"


def get_hf_username() -> str | None:
    """Get the currently logged-in HF username, or None."""
    try:
        from huggingface_hub import HfApi
        return HfApi().whoami()["name"]
    except ImportError:
        return None
    except (OSError, KeyError, ValueError):
        return None


def _set_hf_env_from_keyring() -> None:
    token = _resolve_hf_token()
    if token:
        os.environ["HF_TOKEN"] = token


def _handle_hf(args) -> None:
    if args.hf_command == "login":
        token = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("HF token: ").strip()
        if not token:
            emit_json({"error": "empty token"})
            sys.exit(2)
        try:
            _store_hf_token(token, mirror_to_hf_path=not args.no_mirror)
            os.environ["HF_TOKEN"] = token
            from huggingface_hub import HfApi

            user = HfApi().whoami()
        except Exception as e:
            emit_json({"error": str(e)})
            sys.exit(1)
        emit_json({"ok": True, "user": user.get("name"), "mirrored": not args.no_mirror})
        return

    if args.hf_command == "logout":
        _delete_hf_token(also_remove_hf_path=not args.no_mirror)
        emit_json({"ok": True})
        return

    if args.hf_command == "whoami":
        if args.check_keyring_only:
            try:
                import keyring

                token = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            except Exception as e:
                emit_json({"ok": False, "error": str(e)})
                sys.exit(2)
            if token:
                emit_json({"ok": True})
                return
            emit_json({"ok": False, "hint": HF_LOGIN_HINT})
            sys.exit(2)

        if _resolve_hf_token() is None:
            emit_json({"error": "No Hugging Face token configured.", "hint": HF_LOGIN_HINT})
            sys.exit(2)
        _set_hf_env_from_keyring()
        try:
            from huggingface_hub import HfApi

            user = HfApi().whoami()
        except Exception as e:
            emit_json({"error": str(e)})
            sys.exit(1)
        emit_json({"ok": True, "user": user.get("name")})
        return

    emit_json({"error": "Unknown hf command."})
    sys.exit(2)


def default_repo_name(hf_username: str) -> str:
    """Standard repo name: {username}/my-personal-codex-data"""
    return f"{hf_username}/my-personal-codex-data"


def _compute_stage(config: DataClawConfig) -> tuple[str, int, str | None]:
    """Return (stage_name, stage_number, hf_username)."""
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
    stage: str, config: DataClawConfig, hf_user: str | None, repo_id: str | None,
) -> tuple[list[str], str | None]:
    """Return (next_steps, next_command) for the given stage."""
    if stage == "auth":
        return (
            [
                "Ask the user for their Hugging Face token. Sign up: https://huggingface.co/join — Create WRITE token: https://huggingface.co/settings/tokens",
                "Run: huggingface-cli login --token <THEIR_TOKEN> (NEVER run bare huggingface-cli login — it hangs)",
                "Run: dataclaw config --redact \"<THEIR_TOKEN>\" (so the token gets redacted from exports)",
                "Run: dataclaw prep (to confirm login and get next steps)",
            ],
            None,
        )

    if stage == "configure":
        projects_confirmed = config.get("projects_confirmed", False)
        configured_source = config.get("source")
        source_confirmed = _is_explicit_source_choice(configured_source)
        list_command = (
            f"dataclaw list --source {configured_source}" if source_confirmed else "dataclaw list"
        )
        steps = []
        if not source_confirmed:
            steps.append(
                "Ask the user to explicitly choose export source scope: claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all. "
                "Then set it: dataclaw config --source <claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all>. "
                "Do not run export until source scope is explicitly confirmed."
            )
        else:
            steps.append(
                f"Source scope is currently set to '{configured_source}'. "
                "If the user wants a different scope, run: dataclaw config --source <claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all>."
            )
        if not projects_confirmed:
            steps.append(
                f"Run: {list_command} — then send the FULL project/folder list to the user in your next message "
                "(name, source, sessions, size, excluded), and ask which to EXCLUDE."
            )
            steps.append(
                "Configure project scope: dataclaw config --exclude \"project1,project2\" "
                "or dataclaw config --confirm-projects (to include all listed projects). "
                "Do not run export until this folder review is confirmed."
            )
        steps.extend([
            "Ask about GitHub/Discord usernames to anonymize and sensitive strings to redact. "
            "Configure: dataclaw config --redact-usernames \"handle1\" and dataclaw config --redact \"string1\"",
            "When done configuring, export locally: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
        ])
        # next_command is null because user input is needed before exporting
        return (steps, None)

    if stage == "review":
        return (
            [
                "Ask the user for their full name to run an exact-name privacy check against the export. If they decline, you may skip this check with --skip-full-name-scan and include a clear attestation.",
                "Run PII scan commands and review results with the user.",
                "Ask the user: 'Are there any company names, internal project names, client names, private URLs, or other people's names in your conversations that you'd want redacted? Any custom domains or internal tools?' Add anything they mention with dataclaw config --redact.",
                "Do a deep manual scan: sample ~20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials in conversation text, and anything else that looks sensitive. Report findings to the user.",
                "If PII found in any of the above, add redactions (dataclaw config --redact) and re-export: dataclaw export --no-push",
                (
                    "Run: "
                    + CONFIRM_COMMAND_EXAMPLE
                    + " — scans for PII, shows project breakdown, and unlocks pushing."
                ),
                "Do NOT push until the user explicitly confirms. Once confirmed, push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
            ],
            "dataclaw confirm",
        )

    if stage == "confirmed":
        return (
            [
                "User has reviewed the export. Ask: 'Ready to publish to Hugging Face?' and push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
            ],
            "dataclaw export",
        )

    # done
    dataset_url = f"https://huggingface.co/datasets/{repo_id}" if repo_id else None
    return (
        [
            f"Done! Dataset is live{f' at {dataset_url}' if dataset_url else ''}. To update later: dataclaw export",
            "To reconfigure: dataclaw prep then dataclaw config",
        ],
        None,
    )


def list_projects(source_filter: str = "auto") -> None:
    """Print all projects as JSON (for agents to parse)."""
    projects = _filter_projects_by_source(discover_projects(), source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.")
        return
    config = load_config()
    excluded = set(config.get("excluded_projects", []))
    tags_by_project = config.get("project_tags") if isinstance(config.get("project_tags"), dict) else {}
    rows = []
    for p in projects:
        session_like = {
            "project": p["display_name"],
            "_project_dir_name": p.get("dir_name"),
        }
        tags: list[str] = []
        if isinstance(tags_by_project, dict):
            for candidate in _project_candidates(session_like):
                candidate_tags = tags_by_project.get(candidate)
                if isinstance(candidate_tags, list):
                    tags = candidate_tags
                    break
        rows.append({
            "name": p["display_name"],
            "sessions": p["session_count"],
            "size": _format_size(p["total_size_bytes"]),
            "excluded": p["display_name"] in excluded,
            "source": p.get("source", "claude"),
            "bucket": _bucket_for_project(session_like, config),
            "tags": tags,
        })
    print(json.dumps(rows, indent=2))


def _merge_config_list(config: DataClawConfig, key: str, new_values: list[str]) -> None:
    """Append new_values to a config list (deduplicated, sorted)."""
    existing = set(config.get(key, []))
    existing.update(new_values)
    config[key] = sorted(existing)


def _replace_config_list(config: DataClawConfig, key: str, values: list[str]) -> None:
    config[key] = sorted(set(values))


def _remove_config_list(config: DataClawConfig, key: str, values: list[str]) -> None:
    remove = set(values)
    existing = config.get(key, [])
    config[key] = [value for value in existing if value not in remove]


def _folder_rules(config: DataClawConfig) -> dict:
    rules = config.get("folder_rules")
    if not isinstance(rules, dict):
        rules = {}
        config["folder_rules"] = rules
    return rules


def _project_tags(config: DataClawConfig) -> dict[str, list[str]]:
    tags = config.get("project_tags")
    if not isinstance(tags, dict):
        tags = {}
        config["project_tags"] = tags
    return tags


def _assign_bucket(config: DataClawConfig, project: str, bucket: str) -> None:
    rules = _folder_rules(config)
    projects = rules.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        rules["projects"] = projects
    projects[project] = bucket


def _unassign_bucket(config: DataClawConfig, project: str) -> None:
    rules = _folder_rules(config)
    for key in ("projects", "assignments", "project_buckets"):
        projects = rules.get(key)
        if isinstance(projects, dict):
            projects.pop(project, None)


def _tag_project(config: DataClawConfig, project: str, tag: str) -> None:
    tags_by_project = _project_tags(config)
    tags = tags_by_project.get(project)
    if not isinstance(tags, list):
        tags = []
    if tag not in tags:
        tags.append(tag)
    tags_by_project[project] = sorted(tags)


def _untag_project(config: DataClawConfig, project: str, tag: str) -> None:
    tags_by_project = _project_tags(config)
    tags = tags_by_project.get(project)
    if not isinstance(tags, list):
        return
    remaining = [existing for existing in tags if existing != tag]
    if remaining:
        tags_by_project[project] = remaining
    else:
        tags_by_project.pop(project, None)


def _set_bucket_by_tag(config: DataClawConfig, tag: str, bucket: str) -> None:
    rules = _folder_rules(config)
    tags = rules.get("tags")
    if not isinstance(tags, dict):
        tags = {}
        rules["tags"] = tags
    tags[tag] = bucket


def _clear_bucket_by_tag(config: DataClawConfig, tag: str) -> None:
    rules = _folder_rules(config)
    for key in ("tags", "bucket_by_tag"):
        tags = rules.get(key)
        if isinstance(tags, dict):
            tags.pop(tag, None)


def _privacy_filter_enabled(config: Mapping[str, Any]) -> bool:
    privacy_config = config.get("privacy_filter")
    if isinstance(privacy_config, dict) and "enabled" in privacy_config:
        return privacy_config.get("enabled") is not False
    return True


def _set_privacy_filter_enabled(config: DataClawConfig, enabled: bool) -> None:
    privacy_config = config.get("privacy_filter")
    if not isinstance(privacy_config, dict):
        privacy_config = {}
    privacy_config["enabled"] = enabled
    config["privacy_filter"] = privacy_config


def _set_privacy_filter_device(config: DataClawConfig, device: str | None) -> None:
    if device is None:
        return
    privacy_config = config.get("privacy_filter")
    if not isinstance(privacy_config, dict):
        privacy_config = {}
    normalized = device.strip().lower()
    privacy_config["device"] = normalized if normalized else "auto"
    config["privacy_filter"] = privacy_config


def _parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    raise argparse.ArgumentTypeError("expected one of: on, off, true, false, enabled, disabled")


def configure(
    repo: str | None = None,
    source: str | None = None,
    exclude: list[str] | None = None,
    redact: list[str] | None = None,
    redact_usernames: list[str] | None = None,
    set_excluded: list[str] | None = None,
    remove_excluded: list[str] | None = None,
    set_redact: list[str] | None = None,
    remove_redact: list[str] | None = None,
    set_redact_usernames: list[str] | None = None,
    remove_redact_usernames: list[str] | None = None,
    confirm_projects: bool = False,
    assign: list[tuple[str, str]] | None = None,
    unassign: list[str] | None = None,
    default_bucket: str | None = None,
    clear_default_bucket: bool = False,
    tag_project: list[tuple[str, str]] | None = None,
    untag_project: list[tuple[str, str]] | None = None,
    bucket_by_tag: list[tuple[str, str]] | None = None,
    clear_bucket_by_tag: list[str] | None = None,
    privacy_filter: bool | None = None,
    privacy_filter_device: str | None = None,
    show_secrets: bool = False,
    json_output: bool = False,
):
    """Set config values non-interactively. Lists are MERGED (append), not replaced."""
    config = load_config()
    if repo is not None:
        config["repo"] = repo
    if source is not None:
        config["source"] = source
    if exclude is not None:
        _merge_config_list(config, "excluded_projects", exclude)
    if redact is not None:
        _merge_config_list(config, "redact_strings", redact)
    if redact_usernames is not None:
        _merge_config_list(config, "redact_usernames", redact_usernames)
    if set_excluded is not None:
        _replace_config_list(config, "excluded_projects", set_excluded)
    if remove_excluded is not None:
        _remove_config_list(config, "excluded_projects", remove_excluded)
    if set_redact is not None:
        _replace_config_list(config, "redact_strings", set_redact)
    if remove_redact is not None:
        _remove_config_list(config, "redact_strings", remove_redact)
    if set_redact_usernames is not None:
        _replace_config_list(config, "redact_usernames", set_redact_usernames)
    if remove_redact_usernames is not None:
        _remove_config_list(config, "redact_usernames", remove_redact_usernames)
    if confirm_projects:
        config["projects_confirmed"] = True
    for project, bucket in assign or []:
        _assign_bucket(config, project, bucket)
    for project in unassign or []:
        _unassign_bucket(config, project)
    if default_bucket is not None:
        _folder_rules(config)["default_bucket"] = default_bucket
    if clear_default_bucket:
        _folder_rules(config).pop("default_bucket", None)
    if privacy_filter is not None:
        _set_privacy_filter_enabled(config, privacy_filter)
    _set_privacy_filter_device(config, privacy_filter_device)
    for project, tag in tag_project or []:
        _tag_project(config, project, tag)
    for project, tag in untag_project or []:
        _untag_project(config, project, tag)
    for tag, bucket in bucket_by_tag or []:
        _set_bucket_by_tag(config, tag, bucket)
    for tag in clear_bucket_by_tag or []:
        _clear_bucket_by_tag(config, tag)
    save_config(config)
    display_config = _mask_config_for_display(config, unmask=show_secrets)
    if json_output:
        emit_json(display_config)
    else:
        print(f"Config saved to {CONFIG_FILE}")
        print(json.dumps(display_config, indent=2))


def export_to_jsonl(
    selected_projects: list[dict],
    output_path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    custom_strings: list[str] | None = None,
) -> dict:
    """Export selected projects to JSONL. Returns metadata."""
    total = 0
    skipped = 0
    total_redactions = 0
    models: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    project_names = []

    try:
        fh = open(output_path, "w")
    except OSError as e:
        print(f"Error: cannot write to {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    with fh as f:
        for project in selected_projects:
            print(f"  Parsing {project['display_name']}...", end="", flush=True)
            sessions = parse_project_sessions(
                project["dir_name"], anonymizer=anonymizer,
                include_thinking=include_thinking,
                source=project.get("source", "claude"),
            )
            proj_count = 0
            for session in sessions:
                model = session.get("model")
                if not model or model == "<synthetic>":
                    skipped += 1
                    continue

                session, n_redacted = redact_session(session, custom_strings=custom_strings)
                total_redactions += n_redacted

                clean = {k: v for k, v in session.items() if not k.startswith("_")}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")
                total += 1
                proj_count += 1
                models[model] = models.get(model, 0) + 1
                stats = session.get("stats", {})
                total_input_tokens += stats.get("input_tokens", 0)
                total_output_tokens += stats.get("output_tokens", 0)
            if proj_count:
                project_names.append(project["display_name"])
            print(f" {proj_count} sessions")

    return {
        "sessions": total,
        "skipped": skipped,
        "redactions": total_redactions,
        "models": models,
        "projects": project_names,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


MANIFEST_REL = Path(".dataclaw/manifest.json")


def _project_candidates(session: Mapping[str, Any]) -> list[str]:
    candidates = []
    for value in (session.get("_project_dir_name"), session.get("project")):
        if isinstance(value, str) and value and value not in candidates:
            candidates.append(value)
    project = session.get("project")
    if isinstance(project, str) and ":" in project:
        stripped = project.split(":", 1)[1]
        if stripped and stripped not in candidates:
            candidates.append(stripped)
    return candidates


def _bucket_for_project(session: Mapping[str, Any], config: Mapping[str, Any]) -> str | None:
    rules = config.get("folder_rules") if isinstance(config.get("folder_rules"), dict) else {}
    tags_by_project = config.get("project_tags") if isinstance(config.get("project_tags"), dict) else {}
    explicit = (
        rules.get("projects")
        or rules.get("assignments")
        or rules.get("project_buckets")
        or {}
    )
    by_tag = rules.get("tags") or rules.get("bucket_by_tag") or {}
    candidates = _project_candidates(session)

    if isinstance(explicit, dict):
        for candidate in candidates:
            bucket = explicit.get(candidate)
            if isinstance(bucket, str) and bucket:
                return bucket

    if isinstance(tags_by_project, dict) and isinstance(by_tag, dict):
        for candidate in candidates:
            tags = tags_by_project.get(candidate, [])
            if not isinstance(tags, list):
                continue
            for tag in tags:
                bucket = by_tag.get(tag)
                if isinstance(bucket, str) and bucket:
                    return bucket

    default_bucket = rules.get("default_bucket")
    return default_bucket if isinstance(default_bucket, str) and default_bucket else None


def _resolve_shard_path(root: Path, session: Mapping[str, Any], config: Mapping[str, Any]) -> Path:
    source = str(session.get("source") or "unknown")
    parsed_end = _parse_iso(session.get("end_time"))
    date = parsed_end.date().isoformat() if parsed_end else "unknown"
    bucket = _bucket_for_project(session, config)
    parts = [bucket, source, f"{date}.jsonl"] if bucket else [source, f"{date}.jsonl"]
    return root.joinpath(*parts)


def _stamp_project_dir_name(sessions: list[dict], project: Mapping[str, Any]) -> None:
    project_dir_name = project.get("dir_name")
    for session in sessions:
        session["_project_dir_name"] = project_dir_name


def _is_missing_hf_target(exc: Exception) -> bool:
    try:
        from huggingface_hub.errors import (
            EntryNotFoundError,
            HfHubHTTPError,
            LocalEntryNotFoundError,
            RepositoryNotFoundError,
        )
    except ImportError:
        return False

    if isinstance(exc, (RepositoryNotFoundError, EntryNotFoundError, LocalEntryNotFoundError)):
        return True
    if isinstance(exc, HfHubHTTPError):
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) == 404
    return False


def _fetch_existing_shard(run_dir: Path, repo_id: str, rel_path: str) -> str:
    from huggingface_hub import hf_hub_download

    target = run_dir / rel_path
    if target.exists():
        return "merged"
    try:
        downloaded = Path(hf_hub_download(repo_id=repo_id, filename=rel_path, repo_type="dataset"))
    except Exception as exc:
        if _is_missing_hf_target(exc):
            return "new"
        raise
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(downloaded.read_bytes())
    return "merged"


def _manifest_shard_paths(manifest: Mapping[str, Any]) -> list[str]:
    paths = []
    for shard in manifest.get("shards", []):
        if isinstance(shard, Mapping) and isinstance(shard.get("path"), str):
            paths.append(str(shard["path"]))
    return sorted(set(paths))


def _fetch_remote_manifest(repo_id: str) -> dict[str, Any] | None:
    from huggingface_hub import hf_hub_download

    try:
        downloaded = Path(hf_hub_download(
            repo_id=repo_id,
            filename=MANIFEST_REL.as_posix(),
            repo_type="dataset",
        ))
    except Exception as exc:
        if _is_missing_hf_target(exc):
            return None
        raise
    return json.loads(downloaded.read_text())


def _inspect_remote_dataset(repo_id: str) -> dict[str, Any]:
    """Return enough remote state to decide whether incremental append is safe."""
    _set_hf_env_from_keyring()
    from huggingface_hub import HfApi

    status: dict[str, Any] = {
        "manifest_exists": False,
        "manifest_error": None,
        "files_checked": False,
        "shard_count": 0,
        "missing_shards": [],
    }
    try:
        manifest = _fetch_remote_manifest(repo_id)
    except (json.JSONDecodeError, OSError) as exc:
        status["manifest_error"] = f"{type(exc).__name__}: {exc}"
        return status
    if manifest is None:
        return status

    shard_paths = _manifest_shard_paths(manifest)
    status["manifest_exists"] = True
    status["shard_count"] = len(shard_paths)
    status["max_end_time_by_source"] = manifest.get("max_end_time_by_source", {})
    status["finished_at"] = manifest.get("finished_at")

    try:
        remote_files = set(HfApi().list_repo_files(repo_id=repo_id, repo_type="dataset"))
    except Exception as exc:
        if _is_missing_hf_target(exc):
            status["manifest_exists"] = False
            status["missing_shards"] = shard_paths
            status["files_checked"] = True
            return status
        raise
    status["files_checked"] = True
    status["missing_shards"] = [path for path in shard_paths if path not in remote_files]
    return status


def _load_existing_session_ids(path: Path) -> tuple[set[str], int]:
    session_ids = set()
    total = 0
    if not path.exists():
        return session_ids, total
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            try:
                session_id = json.loads(line).get("session_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
    return session_ids, total


def _rel_shard_path(run_dir: Path, path: Path) -> str:
    return path.relative_to(run_dir).as_posix()


def _write_manifest(run_dir: Path, manifest: dict) -> Path:
    path = run_dir / MANIFEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)
    os.chmod(path, 0o600)
    return path


def _metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "export_id": manifest.get("export_id"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "sources": manifest.get("sources", []),
        "buckets": manifest.get("buckets", []),
        "shards": len(manifest.get("shards", [])),
        "total_sessions_new": manifest.get("total_sessions_new", 0),
        "total_sessions_in_shards": manifest.get("total_sessions_in_shards", 0),
        "total_redactions": manifest.get("total_redactions", 0),
        "models": manifest.get("models", {}),
        "max_end_time_by_source": manifest.get("max_end_time_by_source", {}),
        "include_thinking": manifest.get("include_thinking"),
        "token_count": manifest.get("token_count"),
    }


def _estimate_tokens_from_bytes(byte_count: int) -> int:
    return round(byte_count / TOKEN_ESTIMATE_BYTES_PER_TOKEN)


def _session_message_content(session: Mapping[str, Any]) -> list[str]:
    contents: list[str] = []
    messages = session.get("messages")
    if not isinstance(messages, list):
        return contents
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if content is None:
            continue
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        contents.append(content)
    return contents


def _count_shard_tokens(path: Path) -> dict[str, int]:
    sessions = 0
    messages = 0
    content_chars = 0
    content_bytes = 0
    jsonl_bytes = 0
    if not path.exists():
        return {
            "sessions": 0,
            "messages": 0,
            "content_chars": 0,
            "content_bytes": 0,
            "content_tokens_estimate": 0,
            "jsonl_bytes": 0,
            "jsonl_tokens_estimate": 0,
        }
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            jsonl_bytes += len(line.encode("utf-8"))
            session = json.loads(line)
            sessions += 1
            for content in _session_message_content(session):
                messages += 1
                content_chars += len(content)
                content_bytes += len(content.encode("utf-8"))
    return {
        "sessions": sessions,
        "messages": messages,
        "content_chars": content_chars,
        "content_bytes": content_bytes,
        "content_tokens_estimate": _estimate_tokens_from_bytes(content_bytes),
        "jsonl_bytes": jsonl_bytes,
        "jsonl_tokens_estimate": _estimate_tokens_from_bytes(jsonl_bytes),
    }


def _token_count_from_shards(shards: list[Mapping[str, Any]]) -> dict[str, Any]:
    jsonl_bytes = sum(_numeric(shard.get("bytes")) for shard in shards)
    content_bytes = sum(_numeric(shard.get("content_bytes")) for shard in shards)
    return {
        "method": "byte_estimate",
        "scope": "jsonl",
        "bytes_per_token": TOKEN_ESTIMATE_BYTES_PER_TOKEN,
        "jsonl_tokens": sum(_shard_jsonl_token_count(shard) for shard in shards),
        "jsonl_bytes": jsonl_bytes,
        "content_tokens": sum(_shard_content_token_count(shard) for shard in shards),
        "content_bytes": content_bytes,
        "content_chars": sum(_numeric(shard.get("content_chars")) for shard in shards),
        "messages": sum(_numeric(shard.get("messages_total")) for shard in shards),
    }


def _shard_jsonl_token_count(shard: Mapping[str, Any]) -> int:
    estimate = _numeric(shard.get("jsonl_tokens_estimate"))
    if estimate:
        return estimate
    legacy_exact = _numeric(shard.get("jsonl_tokens_o200k"))
    if legacy_exact:
        return legacy_exact
    return _estimate_tokens_from_bytes(_numeric(shard.get("bytes")))


def _shard_content_token_count(shard: Mapping[str, Any]) -> int:
    estimate = _numeric(shard.get("content_tokens_estimate"))
    if estimate:
        return estimate
    legacy_exact = _numeric(shard.get("content_tokens_o200k"))
    if legacy_exact:
        return legacy_exact
    content_bytes = _numeric(shard.get("content_bytes"))
    if content_bytes:
        return _estimate_tokens_from_bytes(content_bytes)
    return _estimate_tokens_from_bytes(_numeric(shard.get("content_chars")))


def _numeric(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _merge_count_maps(remote: Any, local: Any) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in (remote, local):
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            if isinstance(key, str) and isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    return merged


def _merge_max_end_times(remote: Any, local: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in (remote, local):
        if not isinstance(source, Mapping):
            continue
        for name, end_time in source.items():
            if not isinstance(name, str) or not isinstance(end_time, str):
                continue
            current = merged.get(name)
            parsed = _parse_iso(end_time)
            current_parsed = _parse_iso(current) if isinstance(current, str) else None
            if current is None or (parsed and (current_parsed is None or parsed > current_parsed)):
                merged[name] = end_time
    return merged


def _bucket_from_shard_path(path: str) -> str | None:
    parts = path.split("/")
    return parts[0] if len(parts) == 3 else None


def _normalize_remote_shard_for_merge(shard: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(shard)
    normalized["sessions_new"] = 0
    return normalized


def _merge_remote_manifest_metadata(local_manifest: dict, remote_manifest: Mapping[str, Any] | None) -> dict:
    """Return a push manifest that preserves remote shard metadata not touched by this run."""
    if not remote_manifest:
        return local_manifest

    local_shards = [
        dict(shard)
        for shard in local_manifest.get("shards", [])
        if isinstance(shard, Mapping) and isinstance(shard.get("path"), str)
    ]
    remote_shards = [
        _normalize_remote_shard_for_merge(shard)
        for shard in remote_manifest.get("shards", [])
        if isinstance(shard, Mapping) and isinstance(shard.get("path"), str)
    ]

    shards_by_path = {str(shard["path"]): shard for shard in remote_shards}
    touched_paths = {str(shard["path"]) for shard in local_shards}
    for shard in local_shards:
        shards_by_path[str(shard["path"])] = shard

    merged = dict(local_manifest)
    merged_shards = [shards_by_path[path] for path in sorted(shards_by_path)]
    merged["shards"] = merged_shards
    merged["sources"] = sorted({
        str(shard["source"])
        for shard in merged_shards
        if isinstance(shard.get("source"), str) and shard.get("source")
    })
    bucket_values = {
        str(bucket)
        for bucket in remote_manifest.get("buckets", [])
        if isinstance(bucket, str) and bucket
    }
    bucket_values.update(
        str(bucket)
        for bucket in local_manifest.get("buckets", [])
        if isinstance(bucket, str) and bucket
    )
    bucket_values.update(
        bucket
        for shard in merged_shards
        if isinstance(shard.get("path"), str)
        for bucket in [_bucket_from_shard_path(str(shard["path"]))]
        if bucket
    )
    merged["buckets"] = sorted(bucket_values)
    merged["total_sessions_new"] = _numeric(local_manifest.get("total_sessions_new"))
    merged["total_sessions_in_shards"] = sum(_numeric(shard.get("sessions_total")) for shard in merged_shards)
    merged["models"] = _merge_count_maps(remote_manifest.get("models"), local_manifest.get("models"))
    merged["max_end_time_by_source"] = _merge_max_end_times(
        remote_manifest.get("max_end_time_by_source"),
        local_manifest.get("max_end_time_by_source"),
    )
    merged["token_count"] = _token_count_from_shards(merged_shards)

    merge_source = {
        str(path): str(value)
        for path, value in (remote_manifest.get("merge_source") or {}).items()
        if isinstance(path, str)
    } if isinstance(remote_manifest.get("merge_source"), Mapping) else {}
    merge_source.update({path: "remote" for path in shards_by_path if path not in touched_paths})
    if isinstance(local_manifest.get("merge_source"), Mapping):
        merge_source.update({
            str(path): str(value)
            for path, value in local_manifest["merge_source"].items()
            if isinstance(path, str)
        })
    merged["merge_source"] = {path: merge_source[path] for path in sorted(merge_source)}
    merged["remote_manifest"] = {
        "export_id": remote_manifest.get("export_id"),
        "finished_at": remote_manifest.get("finished_at"),
        "schema_version": remote_manifest.get("schema_version"),
        "shard_count": len(remote_shards),
    }
    return merged


def export_to_shards(
    selected_projects: list[dict],
    run_dir: Path,
    anonymizer: Anonymizer,
    config: Mapping[str, Any],
    *,
    include_thinking: bool = True,
    custom_strings: list[str] | None = None,
    cooled_only: bool = False,
    since: dict[str, str] | None = None,
    fetch_existing: bool = True,
    logger=None,
) -> dict:
    started_at = datetime.now(tz=timezone.utc).isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    sessions: list[dict] = []
    target_paths: set[str] = set()
    for index, project in enumerate(selected_projects, start=1):
        if logger is not None:
            logger.info(
                "export_project_started",
                extra={
                    "phase": "export",
                    "extra": {
                        "index": index,
                        "total_projects": len(selected_projects),
                        "project": project.get("display_name"),
                        "dir_name": project.get("dir_name"),
                        "source": project.get("source", "claude"),
                    },
                },
            )
        parsed = parse_project_sessions(
            project["dir_name"], anonymizer=anonymizer,
            include_thinking=include_thinking,
            source=project.get("source", "claude"),
            cooled_only=cooled_only,
            since=since,
        )
        if logger is not None:
            logger.info(
                "export_project_parsed",
                extra={
                    "phase": "export",
                    "extra": {
                        "index": index,
                        "total_projects": len(selected_projects),
                        "project": project.get("display_name"),
                        "source": project.get("source", "claude"),
                        "sessions_parsed": len(parsed),
                    },
                },
            )
        _stamp_project_dir_name(parsed, project)
        for session in parsed:
            target = _resolve_shard_path(run_dir, session, config)
            session["_target_path"] = target
            target_paths.add(_rel_shard_path(run_dir, target))
        sessions.extend(parsed)

    repo_id = config.get("repo") if isinstance(config.get("repo"), str) else None
    merge_source = {}
    for index, rel_path in enumerate(sorted(target_paths), start=1):
        if logger is not None:
            logger.info(
                "export_fetch_existing_started",
                extra={
                    "phase": "export",
                    "extra": {
                        "index": index,
                        "total_shards": len(target_paths),
                        "path": rel_path,
                        "fetch_existing": bool(fetch_existing and repo_id),
                    },
                },
            )
        if fetch_existing and repo_id:
            merge_source[rel_path] = _fetch_existing_shard(run_dir, repo_id, rel_path)
        else:
            merge_source[rel_path] = "merged" if (run_dir / rel_path).exists() else "new"
        if logger is not None:
            logger.info(
                "export_fetch_existing_finished",
                extra={
                    "phase": "export",
                    "extra": {
                        "index": index,
                        "total_shards": len(target_paths),
                        "path": rel_path,
                        "merge_source": merge_source[rel_path],
                    },
                },
            )

    existing_ids: dict[str, set[str]] = {}
    shard_totals: dict[str, int] = {}
    for rel_path in sorted(target_paths):
        ids, total = _load_existing_session_ids(run_dir / rel_path)
        existing_ids[rel_path] = ids
        shard_totals[rel_path] = total

    shard_meta: dict[str, dict[str, Any]] = {}
    total_redactions = 0
    models: dict[str, int] = {}
    max_end_time_by_source: dict[str, str] = {}
    for index, session in enumerate(sessions, start=1):
        if logger is not None and (index == 1 or index % 100 == 0 or index == len(sessions)):
            logger.info(
                "export_write_progress",
                extra={
                    "phase": "export",
                    "extra": {
                        "session_index": index,
                        "total_sessions_parsed": len(sessions),
                        "shards_seen": len(target_paths),
                    },
                },
            )
        target = session["_target_path"]
        rel_path = _rel_shard_path(run_dir, target)
        session_id = session.get("session_id")
        if isinstance(session_id, str) and session_id in existing_ids.get(rel_path, set()):
            continue

        session, n_redacted = redact_session(session, custom_strings=custom_strings)
        total_redactions += n_redacted
        clean = {k: v for k, v in session.items() if not k.startswith("_")}
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")
        if isinstance(session_id, str) and session_id:
            existing_ids.setdefault(rel_path, set()).add(session_id)
        shard_totals[rel_path] = shard_totals.get(rel_path, 0) + 1

        source = str(clean.get("source") or "unknown")
        date = target.stem
        meta = shard_meta.setdefault(
            rel_path,
            {"path": rel_path, "source": source, "date": date, "sessions_new": 0},
        )
        meta["sessions_new"] += 1
        model = clean.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            models[model] = models.get(model, 0) + 1
        end_time = clean.get("end_time")
        if isinstance(end_time, str):
            current = max_end_time_by_source.get(source)
            if current is None or (_parse_iso(end_time) and _parse_iso(current) and _parse_iso(end_time) > _parse_iso(current)):
                max_end_time_by_source[source] = end_time

    shards = []
    sorted_target_paths = sorted(target_paths)
    if logger is not None and sorted_target_paths:
        logger.info(
            "token_count_started",
            extra={
                "phase": "export",
                "extra": {
                    "method": "byte_estimate",
                    "bytes_per_token": TOKEN_ESTIMATE_BYTES_PER_TOKEN,
                    "scope": "jsonl",
                    "shards": len(sorted_target_paths),
                },
            },
        )
    for index, rel_path in enumerate(sorted_target_paths, start=1):
        source = rel_path.split("/")[-2] if "/" in rel_path else "unknown"
        date = Path(rel_path).stem
        meta = shard_meta.get(rel_path, {"path": rel_path, "source": source, "date": date, "sessions_new": 0})
        meta["sessions_total"] = shard_totals.get(rel_path, 0)
        meta["bytes"] = (run_dir / rel_path).stat().st_size if (run_dir / rel_path).exists() else 0
        token_summary = _count_shard_tokens(run_dir / rel_path)
        meta["messages_total"] = token_summary["messages"]
        meta["content_chars"] = token_summary["content_chars"]
        meta["content_bytes"] = token_summary["content_bytes"]
        meta["content_tokens_estimate"] = token_summary["content_tokens_estimate"]
        meta["jsonl_tokens_estimate"] = token_summary["jsonl_tokens_estimate"]
        if logger is not None and (index == 1 or index % 10 == 0 or index == len(sorted_target_paths)):
            logger.info(
                "token_count_progress",
                extra={
                    "phase": "export",
                    "extra": {
                        "index": index,
                        "total_shards": len(sorted_target_paths),
                        "path": rel_path,
                        "content_tokens_estimate": meta["content_tokens_estimate"],
                        "jsonl_tokens_estimate": meta["jsonl_tokens_estimate"],
                    },
                },
            )
        shards.append(meta)
    token_count = _token_count_from_shards(shards)
    if logger is not None and sorted_target_paths:
        logger.info(
            "token_count_finished",
            extra={
                "phase": "export",
                "extra": token_count,
            },
        )

    buckets = sorted({p.split("/", 1)[0] for p in target_paths if len(p.split("/")) == 3})
    manifest = {
        "export_id": uuid.uuid4().hex,
        "schema_version": 1,
        "root_dir": str(run_dir),
        "started_at": started_at,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "shards": shards,
        "sources": sorted({s["source"] for s in shards}),
        "buckets": buckets,
        "total_sessions_new": sum(s["sessions_new"] for s in shards),
        "total_sessions_in_shards": sum(s["sessions_total"] for s in shards),
        "total_redactions": total_redactions,
        "models": models,
        "max_end_time_by_source": max_end_time_by_source,
        "token_count": token_count,
        "include_thinking": include_thinking,
        "merge_source": merge_source,
    }
    _write_manifest(run_dir, manifest)
    return manifest


def push_to_huggingface(jsonl_path: Path, repo_id: str, meta: dict) -> None:
    """Push JSONL + metadata to HF dataset repo."""
    _set_hf_env_from_keyring()
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Error: huggingface_hub not installed. Run: pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)

    api = HfApi()

    try:
        user_info = api.whoami()
        print(f"Logged in as: {user_info['name']}")
    except (OSError, KeyError, ValueError) as e:
        print(f"Error: Not logged in to Hugging Face ({e}).", file=sys.stderr)
        print("Run: huggingface-cli login", file=sys.stderr)
        sys.exit(1)

    print(f"Pushing to: {repo_id}")
    try:
        api.create_repo(repo_id, repo_type="dataset", exist_ok=True)

        api.upload_file(
            path_or_fileobj=str(jsonl_path),
            path_in_repo="conversations.jsonl",
            repo_id=repo_id, repo_type="dataset",
            commit_message="Update conversation data",
        )

        api.upload_file(
            path_or_fileobj=json.dumps(meta, indent=2).encode(),
            path_in_repo="metadata.json",
            repo_id=repo_id, repo_type="dataset",
            commit_message="Update metadata",
        )

        api.upload_file(
            path_or_fileobj=_build_dataset_card(repo_id, meta).encode(),
            path_in_repo="README.md",
            repo_id=repo_id, repo_type="dataset",
            commit_message="Update dataset card",
        )
    except (OSError, ValueError) as e:
        print(f"Error uploading to Hugging Face: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nDataset: https://huggingface.co/datasets/{repo_id}")
    print(f"Browse all: https://huggingface.co/datasets?other={HF_TAG}")


def _push_shards_attempt(run_dir: Path, repo_id: str, manifest: dict) -> str:
    """Push manifest-scoped shards to HF dataset repo without exiting."""
    _set_hf_env_from_keyring()
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise _AuthFailed("huggingface_hub not installed. Run: pip install huggingface_hub")

    api = HfApi()

    try:
        user_info = api.whoami()
        print(f"Logged in as: {user_info['name']}")
    except (OSError, KeyError, ValueError) as e:
        raise _AuthFailed(f"Not logged in to Hugging Face ({e}).")

    print(f"Pushing to: {repo_id}")
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    remote_manifest = _fetch_remote_manifest(repo_id)
    local_manifest = json.loads(json.dumps(manifest))
    manifest.clear()
    manifest.update(_merge_remote_manifest_metadata(local_manifest, remote_manifest))
    _write_manifest(run_dir, manifest)
    api.upload_file(
        path_or_fileobj=_build_dataset_card_v2(repo_id, manifest).encode(),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Update dataset card",
    )
    api.upload_file(
        path_or_fileobj=json.dumps(_metadata_from_manifest(manifest), indent=2).encode(),
        path_in_repo="metadata.json",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Update metadata",
    )
    for legacy_path in ("conversations.jsonl",):
        try:
            api.delete_file(
                path_in_repo=legacy_path,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=f"Remove legacy {legacy_path}",
            )
        except Exception:
            pass
    allow_patterns = [s["path"] for s in manifest["shards"]] + [".dataclaw/manifest.json"]
    ignore_patterns = ["conversations.jsonl"]
    api.upload_folder(
        folder_path=str(run_dir),
        repo_id=repo_id,
        repo_type="dataset",
        allow_patterns=allow_patterns,
        ignore_patterns=ignore_patterns,
        commit_message=f"Update {len(manifest['shards'])} shard(s) ({manifest['total_sessions_new']} new)",
    )
    return f"https://huggingface.co/datasets/{repo_id}"


def _push_with_retry(
    run_dir: Path,
    repo_id: str,
    manifest: dict,
    logger,
    *,
    backoffs=PUSH_BACKOFFS,
    max_attempts=PUSH_MAX_ATTEMPTS,
    sleep=time.sleep,
    jitter=lambda: random.uniform(0, 5),
) -> tuple[str, int, float]:
    """Push shards with retry semantics for unattended auto mode."""
    from huggingface_hub.utils import HfHubHTTPError

    total_wait = 0.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            wait = backoffs[attempt - 2] + jitter()
            logger.info(
                "push_retry_wait",
                extra={
                    "phase": "push",
                    "extra": {"attempt": attempt, "wait_seconds": wait},
                    "extra_data": {"attempt": attempt, "wait_seconds": wait},
                },
            )
            sleep(wait)
            total_wait += wait

        try:
            logger.info(
                "push_attempt_started",
                extra={
                    "phase": "push",
                    "extra": {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "repo": repo_id,
                        "staging_dir": str(run_dir),
                        "shard_count": len(manifest.get("shards", [])),
                        "total_sessions_new": manifest.get("total_sessions_new", 0),
                    },
                },
            )
            url = _push_shards_attempt(run_dir, repo_id, manifest)
            logger.info(
                "push_success",
                extra={
                    "phase": "push",
                    "extra": {
                        "attempt": attempt,
                        "backoff_seconds_total": total_wait,
                        "repo_url": url,
                    },
                    "extra_data": {
                        "attempt": attempt,
                        "backoff_seconds_total": total_wait,
                        "repo_url": url,
                    },
                },
            )
            return url, attempt, total_wait
        except _AuthFailed as e:
            logger.warning(
                "push_auth_failed",
                extra={"phase": "push", "extra": {"attempt": attempt, "error": str(e)}},
            )
            raise PushFailed(e, attempt, total_wait)
        except HfHubHTTPError as e:
            response = getattr(e, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {400, 401, 403}:
                logger.warning(
                    "push_non_retryable_http_error",
                    extra={
                        "phase": "push",
                        "extra": {"attempt": attempt, "status_code": status_code, "error": str(e)},
                    },
                )
                raise PushFailed(e, attempt, total_wait)
            if status_code == 429:
                last_exc = e
                retry_after = 0
                headers = getattr(response, "headers", {}) or {}
                try:
                    retry_after = int(headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    retry_after = 0
                if retry_after:
                    logger.info(
                        "push_retry_wait",
                        extra={
                            "phase": "push",
                            "extra": {
                                "attempt": attempt,
                                "wait_seconds": retry_after,
                                "reason": "retry-after",
                            },
                            "extra_data": {
                                "attempt": attempt,
                                "wait_seconds": retry_after,
                                "reason": "retry-after",
                            },
                        },
                    )
                    sleep(retry_after)
                    total_wait += retry_after
                continue
            if status_code is not None and status_code >= 500:
                logger.warning(
                    "push_retryable_http_error",
                    extra={
                        "phase": "push",
                        "extra": {"attempt": attempt, "status_code": status_code, "error": str(e)},
                    },
                )
                last_exc = e
                continue
            logger.warning(
                "push_http_error",
                extra={
                    "phase": "push",
                    "extra": {"attempt": attempt, "status_code": status_code, "error": str(e)},
                },
            )
            last_exc = e
            continue
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(
                "push_transport_error",
                extra={
                    "phase": "push",
                    "extra": {"attempt": attempt, "error_type": type(e).__name__, "error": str(e)},
                },
            )
            last_exc = e
            continue

    if last_exc is None:
        last_exc = RuntimeError("push failed")
    raise PushFailed(last_exc, max_attempts, total_wait)


def push_shards_to_huggingface(run_dir: Path, repo_id: str, manifest: dict) -> str:
    """Push manifest-scoped shards to HF dataset repo."""
    try:
        return _push_shards_attempt(run_dir, repo_id, manifest)
    except _AuthFailed as e:
        print(f"Error: {e}", file=sys.stderr)
        if str(e).startswith("Not logged in to Hugging Face"):
            print("Run: huggingface-cli login", file=sys.stderr)
        sys.exit(1)


def _build_dataset_card(repo_id: str, meta: dict) -> str:
    models = meta.get("models", {})
    sessions = meta.get("sessions", 0)
    projects = meta.get("projects", [])
    total_input = meta.get("total_input_tokens", 0)
    total_output = meta.get("total_output_tokens", 0)
    timestamp = meta.get("exported_at", "")[:10]

    model_tags = "\n".join(f"  - {m}" for m in sorted(models.keys()) if m != "unknown")
    model_lines = "\n".join(
        f"| {m} | {c} |" for m, c in sorted(models.items(), key=lambda x: -x[1])
    )

    return f"""---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - dataclaw
  - claude-code
  - codex-cli
  - gemini-cli
  - opencode
  - openclaw
  - conversations
  - coding-assistant
  - tool-use
  - agentic-coding
{model_tags}
pretty_name: Coding Agent Conversations
configs:
  - config_name: default
    data_files: conversations.jsonl
---

# Coding Agent Conversation Logs

> **This is a performance art project.** Anthropic built their models on the world's freely shared information, then introduced increasingly [dystopian data policies](https://www.anthropic.com/news/detecting-and-preventing-distillation-attacks) to stop anyone else from doing the same with their data — pulling up the ladder behind them. DataClaw lets you throw the ladder back down. The dataset it produces is yours to share.

Exported with [DataClaw]({REPO_URL}).

**Tag: `dataclaw`** — [Browse all DataClaw datasets](https://huggingface.co/datasets?other=dataclaw)

## Stats

| Metric | Value |
|--------|-------|
| Sessions | {sessions} |
| Projects | {len(projects)} |
| Input tokens | {_format_token_count(total_input)} |
| Output tokens | {_format_token_count(total_output)} |
| Last updated | {timestamp} |

### Models

| Model | Sessions |
|-------|----------|
{model_lines}

## Schema

Each line in `conversations.jsonl` is one conversation session:

```json
{{
  "session_id": "uuid",
  "project": "my-project",
  "model": "gpt-5.3-codex",
  "git_branch": "main",
  "start_time": "2025-01-15T10:00:00+00:00",
  "end_time": "2025-01-15T10:30:00+00:00",
  "messages": [
    {{"role": "user", "content": "Fix the login bug", "timestamp": "..."}},
    {{
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to...",
      "tool_uses": [
          {{
            "tool": "bash",
            "input": {{"command": "grep -r 'login' src/"}},
            "output": {{"text": "src/auth.py:42: def login(user, password):"}},
            "status": "success"
          }}
        ],
      "timestamp": "..."
    }}
  ],
  "stats": {{
    "user_messages": 5,
    "assistant_messages": 8,
    "tool_uses": 20,
    "input_tokens": 50000,
    "output_tokens": 3000
  }}
}}
```

### Privacy

- Paths anonymized to project-relative; usernames hashed

## Load

```python
from datasets import load_dataset
ds = load_dataset("{repo_id}", split="train")
```

## Export your own

```bash
pip install dataclaw
dataclaw
```
"""


def _build_dataset_card_v2(repo_id: str, manifest: dict) -> str:
    """Build a dataset card for manifest-scoped sharded exports."""
    username = repo_id.split("/", 1)[0] if "/" in repo_id else repo_id
    sources = []
    for source in manifest.get("sources", []):
        if isinstance(source, str) and source and source not in sources:
            sources.append(source)
    has_buckets = bool(manifest.get("buckets"))
    data_glob = lambda source: f"**/{source}/*.jsonl" if has_buckets else f"{source}/*.jsonl"
    configs_block = "\n".join(
        f"  - config_name: {source}\n    data_files: \"{data_glob(source)}\""
        for source in sources
    )
    source_tag_map = {
        "claude": "claude-code",
        "codex": "codex-cli",
        "gemini": "gemini-cli",
        "opencode": "opencode",
        "openclaw": "openclaw",
        "kimi": "kimi-cli",
        "hermes": "hermes-agent",
        "custom": "custom",
    }
    source_tag_lines = "\n".join(
        f"  - {source_tag_map.get(source, source)}"
        for source in sources
    )
    models = manifest.get("models", {})
    sessions_new = manifest.get("total_sessions_new", 0)
    sessions_total = manifest.get("total_sessions_in_shards", 0)
    timestamp = manifest.get("finished_at", "")[:10]
    token_count = manifest.get("token_count") if isinstance(manifest.get("token_count"), Mapping) else {}
    jsonl_tokens = _numeric(token_count.get("jsonl_tokens")) if isinstance(token_count, Mapping) else 0
    token_title = _format_token_title(jsonl_tokens)
    model_tags = "\n".join(f"  - {m}" for m in sorted(models.keys()) if m != "unknown")
    model_lines = "\n".join(
        f"| {m} | {c} |" for m, c in sorted(models.items(), key=lambda x: -x[1])
    )
    example_config = sources[0] if sources else "claude"

    return f"""---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - dataclaw
{source_tag_lines}
  - conversations
  - coding-assistant
  - tool-use
  - agentic-coding
{model_tags}
pretty_name: {token_title} Tokens of {username}'s Agent Logs
configs:
{configs_block}
---

# {token_title} Tokens of {username}'s Agent Logs

> **This is a performance art project.** Anthropic built their models on the world's freely shared information, then introduced increasingly [dystopian data policies](https://www.anthropic.com/news/detecting-and-preventing-distillation-attacks) to stop anyone else from doing the same with their data — pulling up the ladder behind them. DataClaw lets you throw the ladder back down. The dataset it produces is yours to share.

Exported with [DataClaw]({REPO_URL}).

**Tag: `dataclaw`** — [Browse all DataClaw datasets](https://huggingface.co/datasets?other=dataclaw)

## Stats

| Metric | Value |
|--------|-------|
| New sessions | {sessions_new} |
| Total sessions in shards | {sessions_total} |
| Shards | {len(manifest.get("shards", []))} |
| Sources | {len(sources)} |
| Dataset JSONL tokens | {jsonl_tokens:,} |
| Token estimate | UTF-8 bytes / {TOKEN_ESTIMATE_BYTES_PER_TOKEN} |
| Last updated | {timestamp} |

### Models

| Model | Sessions |
|-------|----------|
{model_lines}

## Schema

Each line in the sharded JSONL files is one conversation session:

```json
{{
  "session_id": "uuid",
  "project": "my-project",
  "model": "gpt-5.3-codex",
  "git_branch": "main",
  "start_time": "2025-01-15T10:00:00+00:00",
  "end_time": "2025-01-15T10:30:00+00:00",
  "messages": [
    {{"role": "user", "content": "Fix the login bug", "timestamp": "..."}},
    {{
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to...",
      "tool_uses": [
          {{
            "tool": "bash",
            "input": {{"command": "grep -r 'login' src/"}},
            "output": {{"text": "src/auth.py:42: def login(user, password):"}},
            "status": "success"
          }}
        ],
      "timestamp": "..."
    }}
  ],
  "stats": {{
    "user_messages": 5,
    "assistant_messages": 8,
    "tool_uses": 20,
    "input_tokens": 50000,
    "output_tokens": 3000
  }}
}}
```

### Privacy

- Paths anonymized to project-relative; usernames hashed

## Load

```python
from datasets import load_dataset
ds = load_dataset("{repo_id}", "{example_config}", split="train")
```

## Export your own

```bash
pip install dataclaw
dataclaw
```
"""


def update_skill(target: str) -> None:
    """Download and install the dataclaw skill for a coding agent."""
    if target != "claude":
        print(f"Error: unknown target '{target}'. Supported: claude", file=sys.stderr)
        sys.exit(1)

    dest = Path.cwd() / ".claude" / "skills" / "dataclaw" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading skill from {SKILL_URL}...")
    try:
        with urllib.request.urlopen(SKILL_URL, timeout=15) as resp:
            content = resp.read().decode()
    except (OSError, urllib.error.URLError) as e:
        print(f"Error downloading skill: {e}", file=sys.stderr)
        # Fall back to bundled copy
        bundled = Path(__file__).resolve().parent.parent / "docs" / "SKILL.md"
        if bundled.exists():
            print(f"Using bundled copy from {bundled}")
            content = bundled.read_text()
        else:
            print("No bundled copy available either.", file=sys.stderr)
            sys.exit(1)

    dest.write_text(content)
    print(f"Skill installed to {dest}")
    print(json.dumps({
        "installed": str(dest),
        "next_steps": ["Run: dataclaw prep"],
        "next_command": "dataclaw prep",
    }, indent=2))


def status(*, json_output: bool = False) -> None:
    """Show current stage and next steps (JSON). Read-only — does not modify config."""
    config = load_config()
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    next_steps, next_command = _build_status_next_steps(stage, config, hf_user, repo_id)

    app_settings = config.get("app") if isinstance(config.get("app"), dict) else {}
    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "hf_logged_in": hf_user is not None,
        "hf_username": hf_user,
        "repo": repo_id,
        "source": config.get("source"),
        "projects_confirmed": config.get("projects_confirmed", False),
        "last_export": config.get("last_export"),
        "last_auto_run": config.get("last_auto_run"),
        "schedule": {
            "launch_at_login": app_settings.get("launch_at_login", True),
            "sync_enabled": app_settings.get("sync_enabled", True),
            "sync_interval_hours": app_settings.get("sync_interval_hours", 24),
            "last_scheduled_sync_at": app_settings.get("last_scheduled_sync_at"),
            "next_scheduled_sync_at": app_settings.get("next_scheduled_sync_at"),
            "last_scheduled_sync_error": app_settings.get("last_scheduled_sync_error"),
        },
        "next_steps": next_steps,
        "next_command": next_command,
    }
    if json_output:
        emit_json(result)
    else:
        print(json.dumps(result, indent=2))


def _status_logs(run_id: str | None, lines: int) -> None:
    """Show recent structured logs from today's UTC log file."""
    from . import logging as dc_logging

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    path = dc_logging.LOG_DIR / f"auto-{today}.jsonl"
    payload: dict[str, object] = {"date": today, "log_file": str(path), "lines": []}
    if not path.exists():
        print(json.dumps(payload, indent=2))
        return

    wanted = max(0, lines)
    try:
        raw_lines = path.read_text(errors="replace").splitlines()
    except OSError:
        print(json.dumps(payload, indent=2))
        return

    parsed: list[dict[str, object]] = []
    for line in raw_lines[-wanted:] if wanted else []:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and (run_id is None or row.get("run_id") == run_id):
            parsed.append(row)
    payload["lines"] = parsed
    print(json.dumps(payload, indent=2))


def _find_export_file(file_path: Path | None) -> Path:
    """Resolve the export file path, or exit with an error."""
    if file_path and file_path.exists():
        return file_path
    if file_path is None:
        for c in [Path("/tmp/dataclaw_export.jsonl"), Path("dataclaw_conversations.jsonl")]:
            if c.exists():
                return c
    print(json.dumps({
        "error": "No export file found.",
        "hint": "Run step 1 first to generate a local export file.",
        "blocked_on_step": "Step 1/3",
        "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
        "next_command": "dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    }, indent=2))
    sys.exit(1)


def _read_manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / MANIFEST_REL).read_text())


def _find_export_target(hint: Path | None) -> tuple[Literal["file", "shards"], Path, dict | None]:
    if hint is not None:
        if hint.suffix == ".jsonl":
            return "file", _find_export_file(hint), None
        if hint.is_dir() and (hint / MANIFEST_REL).exists():
            return "shards", hint, _read_manifest(hint)
        print(json.dumps({
            "error": "No export target found.",
            "hint": "Pass a JSONL export file or a sharded staging directory with .dataclaw/manifest.json.",
            "target": str(hint),
            "next_command": "dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
        }, indent=2))
        sys.exit(1)

    staging = Path.home() / ".dataclaw" / "staging"
    if staging.exists():
        candidates = [
            path for path in staging.iterdir()
            if path.is_dir() and (path / MANIFEST_REL).exists()
        ]
        if candidates:
            run_dir = max(candidates, key=lambda path: path.stat().st_mtime)
            return "shards", run_dir, _read_manifest(run_dir)

    export_file = _find_export_file(None)
    return "file", export_file, None


def _iter_manifest_shard_paths(run_dir: Path, manifest: dict) -> list[Path]:
    return [
        run_dir / shard["path"]
        for shard in manifest.get("shards", [])
        if isinstance(shard, dict) and isinstance(shard.get("path"), str)
    ]


def _summarize_shards(run_dir: Path, manifest: dict) -> dict[str, object]:
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0
    file_size = 0
    try:
        for shard_path in _iter_manifest_shard_paths(run_dir, manifest):
            if shard_path.exists():
                file_size += shard_path.stat().st_size
            with shard_path.open() as f:
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
        print(json.dumps({"error": f"Cannot read sharded export in {run_dir}: {e}"}))
        sys.exit(1)
    return {"projects": projects, "models": models, "total": total, "file_size": file_size}


def _merge_pii_findings(target: dict, findings: dict) -> None:
    for key, value in findings.items():
        if key == "high_entropy_strings":
            existing = {item.get("match"): item for item in target.get(key, []) if isinstance(item, dict)}
            for item in value:
                if isinstance(item, dict) and item.get("match") not in existing:
                    existing[item.get("match")] = item
            target[key] = sorted(existing.values(), key=lambda item: item.get("entropy", 0), reverse=True)[:15]
            continue
        seen = set(target.get(key, []))
        for item in value:
            seen.add(item)
        target[key] = sorted(seen)[:20]


def _scan_pii_dir(run_dir: Path, manifest: dict, logger=None) -> dict:
    results: dict = {}
    if logger is None:
        logger = dc_logging.logging.getLogger("dataclaw")
    shard_paths = _iter_manifest_shard_paths(run_dir, manifest)
    total_shards = len(shard_paths)
    for index, shard_path in enumerate(shard_paths, start=1):
        rel_path = shard_path.relative_to(run_dir).as_posix()
        logger.info(
            "mechanical_pii_shard_started",
            extra={
                "phase": "privacy_filter",
                "extra": {
                    "index": index,
                    "total_shards": total_shards,
                    "path": rel_path,
                    "size_bytes": shard_path.stat().st_size if shard_path.exists() else None,
                },
            },
        )
        shard_findings = _scan_pii(shard_path)
        for items in shard_findings.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("file", rel_path)
        _merge_pii_findings(results, shard_findings)
        logger.info(
            "mechanical_pii_shard_finished",
            extra={
                "phase": "privacy_filter",
                "extra": {
                    "index": index,
                    "total_shards": total_shards,
                    "path": rel_path,
                    "finding_types": sorted(shard_findings.keys()),
                    "total_findings": sum(
                        len(value) for value in shard_findings.values() if isinstance(value, list)
                    ),
                },
            },
        )
    return results


def _finding_values(findings: dict) -> set[str]:
    values: set[str] = set()
    for key, items in findings.items():
        if not isinstance(items, list):
            continue
        if key == "high_entropy_strings":
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("match"), str):
                    values.add(item["match"])
            continue
        for item in items:
            if isinstance(item, str):
                values.add(item)
            elif isinstance(item, dict) and isinstance(item.get("match"), str):
                values.add(item["match"])
    return {value for value in values if value and value != "[REDACTED]"}


def _refresh_manifest_file_sizes(run_dir: Path, manifest: dict) -> None:
    for shard in manifest.get("shards", []):
        if not isinstance(shard, dict) or not isinstance(shard.get("path"), str):
            continue
        shard_path = run_dir / shard["path"]
        if shard_path.exists():
            shard["bytes"] = shard_path.stat().st_size
    _write_manifest(run_dir, manifest)


def _redact_mechanical_findings_dir(run_dir: Path, manifest: dict, findings: dict) -> dict[str, int]:
    """Best-effort exact redaction for mechanical scan matches before model redaction."""
    matches = sorted(_finding_values(findings), key=len, reverse=True)
    if not matches:
        return {"files_changed": 0, "redactions": 0}

    files_changed = 0
    redactions = 0
    for shard_path in _iter_manifest_shard_paths(run_dir, manifest):
        try:
            text = shard_path.read_text(errors="replace")
        except OSError:
            continue
        original = text
        for match in matches:
            count = text.count(match)
            if count:
                text = text.replace(match, "[REDACTED]")
                redactions += count
        if text != original:
            tmp_path = shard_path.with_suffix(shard_path.suffix + ".mechanical-redact.tmp")
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(shard_path)
            files_changed += 1

    if files_changed:
        manifest["total_redactions"] = _numeric(manifest.get("total_redactions")) + redactions
        _refresh_manifest_file_sizes(run_dir, manifest)

    return {"files_changed": files_changed, "redactions": redactions}


def _safe_finding_id(value: object) -> str:
    text = str(value)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _summarize_pii_findings(findings: dict, *, max_examples: int = 5) -> dict[str, object]:
    summary: dict[str, object] = {
        "finding_types": sorted(findings.keys()),
        "finding_type_count": len(findings),
        "total_findings": 0,
        "examples": {},
    }
    examples: dict[str, list[dict[str, object]]] = {}
    total = 0
    for key, value in findings.items():
        rows = value if isinstance(value, list) else []
        total += len(rows)
        key_examples: list[dict[str, object]] = []
        for item in rows[:max_examples]:
            if isinstance(item, dict):
                match = item.get("match", "")
                example = {
                    "id": _safe_finding_id(match),
                    "length": len(str(match)),
                }
                if "entropy" in item:
                    example["entropy"] = item["entropy"]
                if item.get("file"):
                    example["file"] = item["file"]
                key_examples.append(example)
            else:
                key_examples.append({
                    "id": _safe_finding_id(item),
                    "length": len(str(item)),
                })
        examples[key] = key_examples
    summary["total_findings"] = total
    summary["examples"] = examples
    return summary


def _scan_for_text_in_dir(
    run_dir: Path, manifest: dict, full_name: str, max_examples: int = 5,
) -> dict[str, object]:
    matches = 0
    examples: list[dict[str, object]] = []
    for shard_path in _iter_manifest_shard_paths(run_dir, manifest):
        scan = _scan_for_text_occurrences(shard_path, full_name, max_examples=max_examples)
        matches += int(scan.get("match_count", 0))
        for example in scan.get("examples", []):
            if len(examples) >= max_examples:
                break
            examples.append({
                "file": shard_path.relative_to(run_dir).as_posix(),
                **cast(dict[str, object], example),
            })
    return {"query": full_name, "match_count": matches, "examples": examples}


def _scan_high_entropy_strings(content: str, max_results: int = 15) -> list[dict]:
    """Scan for high-entropy random strings that might be leaked secrets.

    Complements the regex-based _scan_pii by catching unquoted tokens
    that slipped through Layer 1 (secrets.py) redaction.
    """
    if not content:
        return []

    _CANDIDATE_RE = re.compile(r'[A-Za-z0-9_/+=.-]{20,512}')
    _SECRET_CONTEXT_RE = re.compile(
        r"(token|secret|api[_-]?key|password|credential|authorization|bearer)",
        re.IGNORECASE,
    )

    # Prefixes already caught by other scans
    _KNOWN_PREFIXES = ("eyJ", "ghp_", "gho_", "ghs_", "ghr_", "sk-", "hf_",
                       "AKIA", "pypi-", "npm_", "xox")

    # Benign prefixes that look random but aren't secrets
    _BENIGN_PREFIXES = ("https://", "http://", "sha256-", "sha384-", "sha512-",
                        "sha1-", "data:", "file://", "mailto:")

    # Substrings that indicate non-secret content
    _BENIGN_SUBSTRINGS = ("node_modules", "[redacted]", "package-lock",
                          "webpack", "babel", "eslint", ".chunk.",
                          "vendor/", "dist/", "build/", "encrypted_content",
                          "max_completion_tokens", "background-password-check",
                          "passwordmanager", "serviceworker", "s3tokenizer",
                          "qwen", "oauth", "device_code", "user_code")

    _BENIGN_CONTEXT_SUBSTRINGS = (
        "[redacted]",
        "encrypted_content",
        "secret1",
        "secret2",
        "background-password-check",
        "passwordmanager",
        "serviceworker",
        "max_completion_tokens",
    )

    # File extensions that indicate path-like strings
    _FILE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html",
                        ".json", ".yaml", ".yml", ".toml", ".md", ".rst",
                        ".txt", ".sh", ".go", ".rs", ".java", ".rb", ".php",
                        ".c", ".h", ".cpp", ".hpp", ".swift", ".kt",
                        ".lock", ".cfg", ".ini", ".xml", ".svg", ".png",
                        ".jpg", ".gif", ".woff", ".ttf", ".map", ".vue",
                        ".scss", ".less", ".sql", ".env", ".log")

    _HEX_RE = re.compile(r'^[0-9a-fA-F]+$')
    _UUID_RE = re.compile(
        r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$'
    )

    # Collect unique candidates only near secret-like context. Large exports can
    # contain megabytes of opaque blobs; scanning the whole shard for entropy is
    # too slow and creates noise.
    unique_candidates: dict[str, list[int]] = {}
    context_matches = list(_SECRET_CONTEXT_RE.finditer(content))
    if not context_matches:
        return []
    for ctx_match in context_matches:
        window_start = max(0, ctx_match.start() - 500)
        window_end = min(len(content), ctx_match.end() + 500)
        window = content[window_start:window_end]
        for m in _CANDIDATE_RE.finditer(window):
            token = m.group(0)
            if token not in unique_candidates:
                unique_candidates[token] = []
            unique_candidates[token].append(window_start + m.start())

    results = []
    for token, positions in unique_candidates.items():
        if len(token) > 512:
            continue
        # --- cheap filters first ---

        # Skip known prefixes (already caught by other scans)
        if any(token.startswith(p) for p in _KNOWN_PREFIXES):
            continue

        # Skip hex-only strings (git hashes etc.)
        if _HEX_RE.match(token):
            continue

        # Skip UUIDs (with or without hyphens)
        if _UUID_RE.match(token):
            continue

        # Skip strings containing file extensions
        token_lower = token.lower()
        if any(ext in token_lower for ext in _FILE_EXTENSIONS):
            continue

        # Skip path-like strings (2+ slashes)
        if token.count("/") >= 2:
            continue

        # Skip 3+ dots (domain names, version strings)
        if token.count(".") >= 3:
            continue

        # Skip benign prefixes
        if any(token_lower.startswith(p) for p in _BENIGN_PREFIXES):
            continue

        # Skip benign substrings
        if any(sub in token_lower for sub in _BENIGN_SUBSTRINGS):
            continue

        # Skip generated/tool-call identifiers and obvious code-ish names.
        if token.startswith(("call_", "toolu_", "msg_", "FLAG-")):
            continue
        if any(content[max(0, pos - len(prefix)):pos] == prefix for pos in positions for prefix in ("call_", "toolu_", "msg_")):
            continue
        if "_" in token and token.count("_") >= 2:
            continue
        if "_" in token and "-" in token:
            continue

        # Require mixed char types (upper + lower + digit)
        if not _has_mixed_char_types(token):
            continue

        # --- entropy check (most expensive, done last) ---
        entropy = _shannon_entropy(token)
        if entropy < 4.0:
            continue

        # Build context from first occurrence
        pos = positions[0]
        before_token = content[max(0, pos - 80):pos]
        if re.search(r'(?:["\\]path["\\]|["\\]file_path["\\]|["\\]filename["\\])\s*:\s*(?:["\\])?\s*$', before_token, re.IGNORECASE):
            continue
        ctx_start = max(0, pos - 40)
        ctx_end = min(len(content), pos + len(token) + 40)
        context = content[ctx_start:ctx_end].replace("\n", " ")
        context_lower = context.lower()
        if any(sub in context_lower for sub in _BENIGN_CONTEXT_SUBSTRINGS):
            continue
        if not _SECRET_CONTEXT_RE.search(context):
            continue

        results.append({
            "match": token,
            "entropy": round(entropy, 2),
            "context": context,
        })

    # Sort by entropy descending, cap at max_results
    results.sort(key=lambda r: r["entropy"], reverse=True)
    return results[:max_results]


def _scan_pii(file_path: Path) -> dict:
    """Run PII regex scans on the export file. Returns dict of findings."""
    import re

    p = str(file_path.resolve())
    email_re = re.compile(r'[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
    api_key_res = [
        re.compile(r"gh[opsr]_[A-Za-z0-9]{30,}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
        re.compile(r"sk-(?:proj-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{40,})"),
        re.compile(r"hf_(?!hub_)[A-Za-z0-9_-]{24,}"),
    ]
    ip_re = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
    )
    scans = {
        "emails": email_re,
        "jwt_tokens": r'eyJ[A-Za-z0-9_-]{20,}',
        "api_keys": api_key_res,
        "ip_addresses": ip_re,
    }
    # Known false positives
    fp_emails = {
        "noreply",
        "pytest.",
        "mcp.tool",
        "mcp.resource",
        "server.tool",
        "tasks.loop",
        "github.com",
        "users.noreply.github.com",
        "example.com",
        "localhost",
        "anthropic.com",
        "app.",
        "router.",
    }
    fp_keys = {
        "hf_hub_",
        "sk-notification",
        "sk-status",
        "sk-details",
        "sk-type",
        "sk-generation",
        "sk-update",
    }

    results = {}
    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return {}
    content = re.sub(r'"encrypted_content"\s*:\s*"[^"]{40,}"', '"encrypted_content":"[REDACTED]"', content)
    content = re.sub(r'\\"encrypted_content\\"\s*:\s*\\"[^"\\]{40,}\\"', r'\"encrypted_content\":\"[REDACTED]\"', content)

    for name, pattern in scans.items():
        if name == "api_keys":
            matches = set()
            for api_pattern in cast(list[re.Pattern[str]], pattern):
                matches.update(m.group(0) for m in api_pattern.finditer(content))
        elif hasattr(pattern, "finditer"):
            matches = {m.group(0) for m in cast(re.Pattern[str], pattern).finditer(content)}
        else:
            matches = set(re.findall(cast(str, pattern), content))
        # Filter false positives
        if name == "emails":
            matches = {
                m for m in matches
                if not any(fp in m.lower() for fp in fp_emails)
                and not m.startswith(("n@", "nn@", "n+@", "n-@", "on@", "non@", "function@"))
            }
        if name == "api_keys":
            filtered_keys = set()
            for m in matches:
                if any(m.startswith(fp) for fp in fp_keys):
                    continue
                if m.startswith("hf_"):
                    suffix = m[3:]
                    # Hugging Face access tokens are mixed random-looking
                    # strings; export attachment names like hf_20260302_...
                    # should not block publishing.
                    if not _has_mixed_char_types(suffix):
                        continue
                    if _shannon_entropy(suffix) < 3.5:
                        continue
                filtered_keys.add(m)
            matches = filtered_keys
        if name == "ip_addresses":
            matches = {
                m for m in matches
                if not (
                    m.startswith("0.0.0.")
                    or m.startswith("127.0.0.")
                    or m.startswith("192.168.")
                    or m.startswith("10.")
                    or re.match(r"172\.(?:1[6-9]|2\d|3[01])\.", m)
                    or m in {"8.8.8.8", "8.8.4.4", "1.1.1.1"}
                )
            }
        if matches:
            results[name] = sorted(matches)[:20]  # cap at 20

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
    numbers = [int(n) for n in re.findall(r"\b(\d+)\b", attestation)]
    return max(numbers) if numbers else None


def _scan_for_text_occurrences(
    file_path: Path, query: str, max_examples: int = 5,
) -> dict[str, object]:
    """Scan file for case-insensitive occurrences of query and return a compact summary."""
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
        return {
            "query": query,
            "match_count": 0,
            "examples": [],
            "error": str(e),
        }
    return {
        "query": query,
        "match_count": matches,
        "examples": examples,
    }


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
            mentions_skip = any(
                token in lower
                for token in ("skip", "skipped", "declined", "opt out", "prefer not")
            )
            if "full name" not in lower or not mentions_skip:
                errors["asked_full_name"] = (
                    "When skipping full-name scan, attestation must say the user declined/skipped full name."
                )
        else:
            full_name_lower = (full_name or "").lower()
            full_name_tokens = [t for t in re.split(r"\s+", full_name_lower) if len(t) > 1]
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
        errors["asked_sensitive_entities"] = (
            "Provide a detailed text attestation for sensitive-entity review."
        )
    else:
        lower = sensitive_attestation.lower()
        asked = "ask" in lower
        topics = any(
            token in lower
            for token in ("company", "client", "internal", "url", "domain", "tool", "name")
        )
        outcome = any(
            token in lower
            for token in ("none", "no", "redact", "added", "updated", "configured")
        )
        if not asked or not topics or not outcome:
            errors["asked_sensitive_entities"] = (
                "Sensitive attestation must say what you asked and the outcome "
                "(none found or redactions updated)."
            )

    manual_attestation = provided["manual_scan_done"]
    manual_sessions = _extract_manual_scan_sessions(manual_attestation)
    if len(manual_attestation) < MIN_ATTESTATION_CHARS:
        errors["manual_scan_done"] = "Provide a detailed text attestation for the manual scan."
    else:
        lower = manual_attestation.lower()
        if "manual" not in lower or "scan" not in lower:
            errors["manual_scan_done"] = (
                "Manual scan attestation must explicitly mention a manual scan."
            )
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
        return normalized, (
            "Publish attestation must state that the user explicitly approved publishing/pushing."
        )
    return normalized, None


def _finding_dict(finding: object) -> dict[str, object]:
    return {
        "entity": getattr(finding, "entity", None),
        "text": getattr(finding, "text", None),
        "score": getattr(finding, "score", None),
        "start": getattr(finding, "start", None),
        "end": getattr(finding, "end", None),
        "field": getattr(finding, "field", None),
        "session_id": getattr(finding, "session_id", None),
        "source": getattr(finding, "source", None),
        "fingerprint": finding.fingerprint(),
    }


def confirm(
    file_path: Path | None = None,
    full_name: str | None = None,
    attest_asked_full_name: str | None = None,
    attest_asked_sensitive: str | None = None,
    attest_manual_scan: str | None = None,
    skip_full_name_scan: bool = False,
    *,
    policy: str = "strict",
    ack_privacy_findings: bool = False,
) -> None:
    """Scan export for PII, summarize projects, and unlock pushing. JSON output."""
    config = load_config()
    last_export = config.get("last_export", {})
    mode, path, manifest = _find_export_target(file_path)

    normalized_full_name = _normalize_attestation_text(full_name)
    if skip_full_name_scan and normalized_full_name:
        print(json.dumps({
            "error": "Use either --full-name or --skip-full-name-scan, not both.",
            "hint": (
                "Provide --full-name for an exact-name scan, or use --skip-full-name-scan "
                "if the user declines sharing their name."
            ),
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
        sys.exit(1)
    if not normalized_full_name and not skip_full_name_scan:
        print(json.dumps({
            "error": "Missing required --full-name for verification scan.",
            "hint": (
                "Ask the user for their full name and pass it via --full-name "
                "to run an exact-name privacy check. If the user declines, rerun with "
                "--skip-full-name-scan and a full-name attestation describing the skip."
            ),
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
        }, indent=2))
        sys.exit(1)

    attestations, attestation_errors, manual_scan_sessions = _collect_review_attestations(
        attest_asked_full_name=attest_asked_full_name,
        attest_asked_sensitive=attest_asked_sensitive,
        attest_manual_scan=attest_manual_scan,
        full_name=normalized_full_name if normalized_full_name else None,
        skip_full_name_scan=skip_full_name_scan,
    )
    if attestation_errors:
        print(json.dumps({
            "error": "Missing or invalid review attestations.",
            "attestation_errors": attestation_errors,
            "required_attestations": REQUIRED_REVIEW_ATTESTATIONS,
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
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
        if mode == "shards" and manifest is not None:
            full_name_scan = _scan_for_text_in_dir(path, manifest, normalized_full_name)
        else:
            full_name_scan = _scan_for_text_occurrences(path, normalized_full_name)

    # Read and summarize
    if mode == "shards" and manifest is not None:
        summary = _summarize_shards(path, manifest)
        projects = cast(dict[str, int], summary["projects"])
        models = cast(dict[str, int], summary["models"])
        total = cast(int, summary["total"])
        file_size = cast(int, summary["file_size"])
    else:
        projects: dict[str, int] = {}
        models: dict[str, int] = {}
        total = 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    total += 1
                    proj = row.get("project", "<unknown>")
                    projects[proj] = projects.get(proj, 0) + 1
                    model = row.get("model", "<unknown>")
                    models[model] = models.get(model, 0) + 1
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"Cannot read {path}: {e}"}))
            sys.exit(1)
        file_size = path.stat().st_size
    repo_id = config.get("repo")

    # Run PII scans
    pii_findings = _scan_pii_dir(path, manifest) if mode == "shards" and manifest is not None else _scan_pii(path)
    pf_payload: dict[str, object] | None = None
    pf_new: list[object] = []
    privacy_config = config.get("privacy_filter") if isinstance(config.get("privacy_filter"), dict) else {}
    if _privacy_filter_enabled(config):
        from . import privacy_filter as pf

        device = privacy_config.get("device") if isinstance(privacy_config, dict) else None
        min_score = float(
            privacy_config.get("min_score", getattr(pf, "_DEFAULT_MIN_SCORE", 0.85))
            if isinstance(privacy_config, dict)
            else getattr(pf, "_DEFAULT_MIN_SCORE", 0.85)
        )
        if not pf.is_available():
            pf_payload = {
                "status": "unavailable",
                "hint": "Install with: pip install dataclaw[pii]",
            }
        else:
            findings = (
                pf.scan_shards(path, manifest, device=device, min_score=min_score)
                if mode == "shards" and manifest is not None
                else pf.scan_jsonl(path, device=device, min_score=min_score)
            )
            pf_new, pf_known = pf.diff_findings(findings, config.get("known_findings") or {})
            pf_payload = {
                "status": "scanned",
                "new": [_finding_dict(f) for f in pf_new],
                "known": [_finding_dict(f) for f in pf_known],
                "device": device,
                "min_score": min_score,
            }
            if pf_new and policy == "strict" and not ack_privacy_findings:
                print(json.dumps({
                    "error": "New privacy-filter findings require acknowledgement.",
                    "pf_new": [_finding_dict(f) for f in pf_new],
                    "privacy_filter": pf_payload,
                    "blocked_on_step": "Step 2/3",
                    "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                    "next_command": "dataclaw confirm --ack-privacy-findings ...",
                }, indent=2))
                sys.exit(2)
            if pf_new and ack_privacy_findings:
                config["known_findings"] = pf.record_findings(
                    pf_new, config.get("known_findings") or {},
                )
            elif pf_new and policy == "permissive":
                try:
                    from . import logging as dc_logging

                    dc_logging.logging.getLogger("dataclaw").warning(
                        "privacy_filter_new_findings",
                        extra={"extra": {"count": len(pf_new)}},
                    )
                except Exception:
                    pass

    # Advance stage from review -> confirmed
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
        "file": str(path.resolve()),
        "mode": mode,
        "pii_findings": bool(pii_findings),
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    if mode == "shards":
        config["last_confirm"]["manifest"] = str((path / MANIFEST_REL).resolve())
    save_config(config)

    next_steps = [
        "Show the user the project breakdown, full-name scan, and PII scan results above.",
    ]
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
            "If real: dataclaw config --redact \"string\" then re-export with --no-push. "
            "False positives can be ignored."
        )
    if "high_entropy_strings" in pii_findings:
        next_steps.append(
            "High-entropy strings detected — these may be leaked secrets (API keys, tokens, "
            "passwords) that escaped automatic redaction. Review each one using the provided "
            "context snippets. If any are real secrets, redact with: "
            "dataclaw config --redact \"the_secret\" then re-export with --no-push."
        )
    next_steps.extend([
        "If any project should be excluded, run: dataclaw config --exclude \"project_name\" and re-export with --no-push.",
        f"This will publish {total} sessions ({_format_size(file_size)}) publicly to Hugging Face"
        + (f" at {repo_id}" if repo_id else "") + ". Ask the user: 'Are you ready to proceed?'",
        "Once confirmed, push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
    ])

    result = {
        "stage": "confirmed",
        "stage_number": 3,
        "total_stages": 4,
        "file": str(path.resolve()),
        "file_size": _format_size(file_size),
        "total_sessions": total,
        "projects": [
            {"name": name, "sessions": count}
            for name, count in sorted(projects.items(), key=lambda x: -x[1])
        ],
        "models": {m: c for m, c in sorted(models.items(), key=lambda x: -x[1])},
        "pii_scan": pii_findings if pii_findings else "clean",
        "full_name_scan": full_name_scan,
        "manual_scan_sessions": manual_scan_sessions,
        "repo": repo_id,
        "last_export_timestamp": last_export.get("timestamp"),
        "next_steps": next_steps,
        "next_command": "dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
        "attestations": attestations,
    }
    if pf_payload is not None:
        result["privacy_filter"] = pf_payload
    if mode == "shards" and manifest is not None:
        result.update({
            "mode": "shards",
            "manifest": str((path / MANIFEST_REL).resolve()),
            "shard_count": len(manifest.get("shards", [])),
            "total_sessions_new": manifest.get("total_sessions_new", total),
        })
    print(json.dumps(result, indent=2))


def prep(source_filter: str = "auto") -> None:
    """Data prep — discover projects, detect HF auth, output JSON.

    Designed to be called by an agent which handles the interactive parts.
    Outputs pure JSON to stdout so agents can parse it directly.
    """
    config = load_config()
    resolved_source_choice, source_explicit = _resolve_source_choice(source_filter, config)
    effective_source_filter = _normalize_source_filter(resolved_source_choice)

    if not _has_session_sources(effective_source_filter):
        if effective_source_filter == "claude":
            err = "~/.claude was not found."
        elif effective_source_filter == "codex":
            err = "~/.codex was not found."
        elif effective_source_filter == "gemini":
            from .parser import GEMINI_DIR
            err = f"{GEMINI_DIR} was not found."
        else:
            err = "None of the supported agent session directories were found."
        print(json.dumps({"error": err}))
        sys.exit(1)

    projects = _filter_projects_by_source(discover_projects(), effective_source_filter)
    if not projects:
        print(json.dumps({"error": f"No {_source_label(effective_source_filter)} sessions found."}))
        sys.exit(1)

    excluded = set(config.get("excluded_projects", []))

    # Use _compute_stage to determine where we are
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    # Build contextual next_steps
    stage_config = cast(DataClawConfig, dict(config))
    if source_explicit:
        stage_config["source"] = resolved_source_choice
    next_steps, next_command = _build_status_next_steps(stage, stage_config, hf_user, repo_id)

    # Persist stage
    config["stage"] = stage
    save_config(config)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "next_command": next_command,
        "requested_source_filter": source_filter,
        "source_filter": resolved_source_choice,
        "source_selection_confirmed": source_explicit,
        "hf_logged_in": hf_user is not None,
        "hf_username": hf_user,
        "repo": repo_id,
        "projects": [
            {
                "name": p["display_name"],
                "sessions": p["session_count"],
                "size": _format_size(p["total_size_bytes"]),
                "excluded": p["display_name"] in excluded,
                "source": p.get("source", "claude"),
            }
            for p in projects
        ],
        "redact_strings": [_mask_secret(s) for s in config.get("redact_strings", [])],
        "redact_usernames": config.get("redact_usernames", []),
        "config_file": str(CONFIG_FILE),
        "next_steps": next_steps,
    }
    print(json.dumps(result, indent=2))


def main() -> None:
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="DataClaw — Claude/Codex -> Hugging Face")
    sub = parser.add_subparsers(dest="command")

    prep_parser = sub.add_parser("prep", help="Data prep — discover projects, detect HF, output JSON")
    prep_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
    status_parser = sub.add_parser("status", help="Show current stage and next steps (JSON)")
    status_parser.add_argument("--json", action="store_true", help="Emit JSON envelope for app integration")
    status_parser.add_argument("--logs", action="store_true")
    status_parser.add_argument("--run", type=str, default=None)
    status_parser.add_argument("--lines", type=int, default=200)
    cf = sub.add_parser("confirm", help="Scan for PII, summarize export, and unlock pushing (JSON)")
    cf.add_argument("path", nargs="?", default=None, type=Path,
                    help="Sharded staging dir or JSONL file (default: auto-detect latest).")
    cf.add_argument("--file", "-f", dest="legacy_file", type=Path, default=None,
                    help="Deprecated alias for the positional path argument.")
    cf.add_argument("--full-name", type=str, default=None,
                    help="User's full name to scan for in the export file (exact-name privacy check).")
    cf.add_argument("--skip-full-name-scan", action="store_true",
                    help="Skip exact full-name scan when the user declines sharing their name.")
    cf.add_argument("--attest-full-name", type=str, default=None,
                    help="Text attestation describing how full-name scan was done.")
    cf.add_argument("--attest-sensitive", type=str, default=None,
                    help="Text attestation describing sensitive-entity review and outcome.")
    cf.add_argument("--attest-manual-scan", type=str, nargs="?", const="__DEPRECATED_FLAG__", default=None,
                    help=f"Text attestation describing manual scan ({MIN_MANUAL_SCAN_SESSIONS}+ sessions).")
    cf.add_argument("--policy", choices=["strict", "permissive"], default="strict")
    cf.add_argument("--ack-privacy-findings", action="store_true")
    # Deprecated boolean attestations retained only for a guided migration error.
    cf.add_argument("--attest-asked-full-name", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-sensitive", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-manual-scan", action="store_true", help=argparse.SUPPRESS)
    list_parser = sub.add_parser("list", help="List all projects")
    list_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")

    us = sub.add_parser("update-skill", help="Install/update the dataclaw skill for a coding agent")
    us.add_argument("target", choices=["claude"], help="Agent to install skill for")

    cfg = sub.add_parser("config", help="View or set config")
    cfg.add_argument("--repo", type=str, help="Set HF repo")
    cfg.add_argument("--source", choices=sorted(EXPLICIT_SOURCE_CHOICES),
                     help="Set export source scope explicitly: claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all")
    cfg.add_argument("--exclude", type=str, help="Comma-separated projects to exclude")
    cfg.add_argument("--redact", type=str,
                     help="Comma-separated strings to always redact (API keys, usernames, domains)")
    cfg.add_argument("--redact-usernames", type=str,
                     help="Comma-separated usernames to anonymize (GitHub handles, Discord names)")
    cfg.add_argument("--set-redact", type=str,
                     help="Comma-separated strings to set as the full redact list")
    cfg.add_argument("--remove-redact", type=str,
                     help="Comma-separated strings to remove from the redact list")
    cfg.add_argument("--set-redact-usernames", type=str,
                     help="Comma-separated usernames to set as the full username redact list")
    cfg.add_argument("--remove-redact-usernames", type=str,
                     help="Comma-separated usernames to remove from the username redact list")
    cfg.add_argument("--set-excluded", type=str,
                     help="Comma-separated projects to set as the full exclusion list")
    cfg.add_argument("--remove-excluded", type=str,
                     help="Comma-separated projects to remove from the exclusion list")
    cfg.add_argument("--show-secrets", action="store_true",
                     help="Show raw redact_strings instead of masking them")
    cfg.add_argument("--json", action="store_true", help="Emit JSON envelope for app integration")
    cfg.add_argument("--confirm-projects", action="store_true",
                     help="Mark project selection as confirmed (include all)")
    cfg.add_argument("--assign", nargs=2, metavar=("PROJECT", "BUCKET"), action="append",
                     help="Assign a project to a bucket")
    cfg.add_argument("--unassign", metavar="PROJECT", action="append",
                     help="Remove a project bucket assignment")
    cfg.add_argument("--default-bucket", type=str, help="Set the fallback bucket")
    cfg.add_argument("--clear-default-bucket", action="store_true",
                     help="Clear the fallback bucket")
    cfg.add_argument("--tag-project", nargs=2, metavar=("PROJECT", "TAG"), action="append",
                     help="Attach a tag to a project")
    cfg.add_argument("--untag-project", nargs=2, metavar=("PROJECT", "TAG"), action="append",
                     help="Remove a tag from a project")
    cfg.add_argument("--bucket-by-tag", nargs=2, metavar=("TAG", "BUCKET"), action="append",
                     help="Route projects with a tag to a bucket")
    cfg.add_argument("--clear-bucket-by-tag", metavar="TAG", action="append",
                     help="Clear a tag-to-bucket rule")
    cfg.add_argument("--privacy-filter", type=_parse_bool_flag, metavar="on|off",
                     help="Enable or disable privacy-filter scanning")
    cfg.add_argument("--privacy-filter-device", choices=["auto", "cpu", "mps"],
                     help="Set privacy-filter device. auto uses Apple GPU/MPS when available, otherwise CPU")

    enable_auto = sub.add_parser("enable-auto", help="Enable unattended automatic exports")
    enable_auto.add_argument("--publish-attestation", required=True, type=str)
    enable_auto.add_argument("--policy", choices=["strict", "permissive"], default="strict")
    enable_auto.add_argument("--full-name", type=str, default=None)
    enable_auto.add_argument("--skip-full-name-scan", action="store_true")
    enable_auto.add_argument("--enable-privacy-filter", dest="enable_privacy_filter", action="store_true", default=True)
    enable_auto.add_argument("--disable-privacy-filter", dest="enable_privacy_filter", action="store_false")

    hf = sub.add_parser("hf", help="Manage Hugging Face auth for DataClaw")
    hf_sub = hf.add_subparsers(dest="hf_command", required=True)
    hf_login = hf_sub.add_parser("login", help="Store and verify a Hugging Face token")
    hf_login.add_argument("--token-stdin", action="store_true")
    hf_login.add_argument("--no-mirror", action="store_true")
    hf_logout = hf_sub.add_parser("logout", help="Delete the stored Hugging Face token")
    hf_logout.add_argument("--no-mirror", action="store_true")
    hf_whoami = hf_sub.add_parser("whoami", help="Show the current Hugging Face user")
    hf_whoami.add_argument("--check-keyring-only", action="store_true")

    auto = sub.add_parser("auto", help="Run an unattended automatic export")
    auto.add_argument("--force", action="store_true")
    auto.add_argument("--dry-run", action="store_true")
    auto.add_argument("--policy-override", choices=["strict", "permissive"], default=None)
    auto.add_argument("--retry-only", action="store_true")

    rollback = sub.add_parser("rollback", help="List or restore Hugging Face dataset commits")
    rollback_action = rollback.add_mutually_exclusive_group(required=True)
    rollback_action.add_argument("--commit", dest="commit", type=str)
    rollback_action.add_argument("--list", dest="list_commits", action="store_true")
    rollback.add_argument("--repo", type=str, default=None)
    rollback.add_argument("--dry-run", action="store_true")
    rollback.add_argument("--limit", type=int, default=20)

    clean_staging = sub.add_parser("clean-staging", help="Preview or remove failed auto staging runs")
    clean_staging.add_argument("--yes", action="store_true")

    install_schedule = sub.add_parser("install-schedule", help="Install OS scheduler for auto mode")
    install_schedule.add_argument("--time", default="03:00")
    sub.add_parser("uninstall-schedule", help="Remove OS scheduler for auto mode")
    sub.add_parser("schedule-status", help="Show OS scheduler status")

    exp = sub.add_parser("export", help="Export and push (default)")
    # Export flags on both the subcommand and root parser so `dataclaw --no-push` works
    for target in (exp, parser):
        target.add_argument("--output", "-o", type=Path, default=None)
        target.add_argument("--repo", "-r", type=str, default=None)
        target.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
        target.add_argument("--all-projects", action="store_true")
        target.add_argument("--no-thinking", action="store_true")
        target.add_argument("--no-push", action="store_true")
        target.add_argument(
            "--publish-attestation",
            type=str,
            default=None,
            help="Required for push: text attestation that user explicitly approved publishing.",
        )
        target.add_argument("--attest-user-approved-publish", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()
    command = args.command or "export"

    if command == "prep":
        prep(source_filter=args.source)
        return

    if command == "status":
        if args.logs:
            _status_logs(args.run, args.lines)
            return
        status(json_output=args.json)
        return

    if command == "confirm":
        if (
            args.attest_asked_full_name
            or args.attest_asked_sensitive
            or args.attest_asked_manual_scan
            or args.attest_manual_scan == "__DEPRECATED_FLAG__"
        ):
            print(json.dumps({
                "error": "Deprecated boolean attestation flags were provided.",
                "hint": (
                    "Use text attestations instead so the command can validate what was reviewed."
                ),
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": CONFIRM_COMMAND_EXAMPLE,
            }, indent=2))
            sys.exit(1)
        confirm(
            file_path=args.path or args.legacy_file,
            full_name=args.full_name,
            attest_asked_full_name=args.attest_full_name,
            attest_asked_sensitive=args.attest_sensitive,
            attest_manual_scan=args.attest_manual_scan,
            skip_full_name_scan=args.skip_full_name_scan,
            policy=args.policy,
            ack_privacy_findings=args.ack_privacy_findings,
        )
        return

    if command == "update-skill":
        update_skill(args.target)
        return

    if command == "list":
        config = load_config()
        resolved_source_choice, _ = _resolve_source_choice(args.source, config)
        list_projects(source_filter=resolved_source_choice)
        return

    if command == "config":
        _handle_config(args)
        return

    if command == "enable-auto":
        _handle_enable_auto(args)
        return

    if command == "hf":
        _handle_hf(args)
        return

    if command == "auto":
        _handle_auto(args)
        return

    if command == "rollback":
        _handle_rollback(args)
        return

    if command == "clean-staging":
        _handle_clean_staging(args)
        return

    if command == "install-schedule":
        _handle_install_schedule(args)
        return

    if command == "uninstall-schedule":
        _handle_uninstall_schedule(args)
        return

    if command == "schedule-status":
        _handle_schedule_status(args)
        return

    _run_export(args)


def _parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _handle_config(args) -> None:
    """Handle the config subcommand."""
    has_changes = (
        args.repo
        or args.source
        or args.exclude
        or args.redact
        or args.redact_usernames
        or args.set_redact
        or args.remove_redact
        or args.set_redact_usernames
        or args.remove_redact_usernames
        or args.set_excluded
        or args.remove_excluded
        or args.confirm_projects
        or args.assign
        or args.unassign
        or args.default_bucket
        or args.clear_default_bucket
        or args.tag_project
        or args.untag_project
        or args.bucket_by_tag
        or args.clear_bucket_by_tag
        or args.privacy_filter is not None
        or args.privacy_filter_device
    )
    if not has_changes:
        display_config = _mask_config_for_display(load_config(), unmask=args.show_secrets)
        if args.json:
            emit_json(display_config)
        else:
            print(json.dumps(display_config, indent=2))
        return
    configure(
        repo=args.repo,
        source=args.source,
        exclude=_parse_csv_arg(args.exclude),
        redact=_parse_csv_arg(args.redact),
        redact_usernames=_parse_csv_arg(args.redact_usernames),
        set_excluded=_parse_csv_arg(args.set_excluded),
        remove_excluded=_parse_csv_arg(args.remove_excluded),
        set_redact=_parse_csv_arg(args.set_redact),
        remove_redact=_parse_csv_arg(args.remove_redact),
        set_redact_usernames=_parse_csv_arg(args.set_redact_usernames),
        remove_redact_usernames=_parse_csv_arg(args.remove_redact_usernames),
        confirm_projects=args.confirm_projects or bool(args.exclude),
        assign=args.assign,
        unassign=args.unassign,
        default_bucket=args.default_bucket,
        clear_default_bucket=args.clear_default_bucket,
        tag_project=args.tag_project,
        untag_project=args.untag_project,
        bucket_by_tag=args.bucket_by_tag,
        clear_bucket_by_tag=args.clear_bucket_by_tag,
        privacy_filter=args.privacy_filter,
        privacy_filter_device=args.privacy_filter_device,
        show_secrets=args.show_secrets,
        json_output=args.json,
    )


def record_last_auto_run(result: str, config: DataClawConfig, **fields) -> None:
    config["last_auto_run"] = {
        "result": result,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        **fields,
    }


def _auto_run_manifest_fields(manifest: dict) -> dict[str, Any]:
    return {
        "sources": manifest.get("sources", []),
        "shard_count": len(manifest.get("shards", [])),
        "total_sessions_new": manifest.get("total_sessions_new", 0),
        "total_sessions_in_shards": manifest.get("total_sessions_in_shards", 0),
        "max_end_time_by_source": manifest.get("max_end_time_by_source", {}),
        "manifest_export_id": manifest.get("export_id"),
        "manifest_finished_at": manifest.get("finished_at"),
    }


def _cleanup_published(staging_root: Path, *, keep: int = 3) -> None:
    published_root = staging_root / PUBLISHED_DIRNAME
    if not published_root.exists():
        return
    published_dirs = [path for path in published_root.iterdir() if path.is_dir()]
    published_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for old_dir in published_dirs[max(0, keep):]:
        shutil.rmtree(old_dir)


def _cleanup_failed_staging(staging_root: Path, *, keep: int = 1) -> int:
    failed_dirs = _failed_staging_dirs(staging_root)
    deleted = 0
    for old_dir in failed_dirs[max(0, keep):]:
        shutil.rmtree(old_dir)
        deleted += 1
    return deleted


def _staging_size_bytes(staging_root: Path) -> int:
    if not staging_root.exists():
        return 0
    total = 0
    for run_dir in staging_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == PUBLISHED_DIRNAME:
            continue
        for path in run_dir.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def _find_failed_run_dir(staging_root: Path) -> Path | None:
    if not staging_root.exists():
        return None
    candidates = [
        path for path in staging_root.iterdir()
        if path.is_dir()
        and path.name != PUBLISHED_DIRNAME
        and (path / MANIFEST_REL).exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _failed_staging_dirs(staging_root: Path) -> list[Path]:
    if not staging_root.exists():
        return []
    return sorted(
        [
            path for path in staging_root.iterdir()
            if path.is_dir() and path.name != PUBLISHED_DIRNAME
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _handle_clean_staging(args) -> None:
    failed_dirs = _failed_staging_dirs(STAGING_ROOT)
    if args.yes:
        for path in failed_dirs:
            shutil.rmtree(path)
    print(json.dumps({
        "deleted": bool(args.yes),
        "staging_root": str(STAGING_ROOT),
        "failed_dirs": [str(path) for path in failed_dirs],
    }, indent=2))


def _scheduler_module():
    from . import scheduler
    return scheduler


def _handle_install_schedule(args) -> None:
    config = load_config()
    auto = config.get("auto")
    if not isinstance(auto, dict) or not auto.get("enabled"):
        print(json.dumps({
            "error": "Auto mode is not enabled.",
            "hint": "Run dataclaw enable-auto first.",
            "blocked_on_step": "install-schedule",
            "next_command": "dataclaw enable-auto --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
        }, indent=2))
        sys.exit(2)
    scheduler = _scheduler_module()
    if sys.platform == "darwin":
        path = scheduler.install_macos(config, args.time)
    else:
        path = scheduler.install_linux(config, args.time)
    print(json.dumps({
        "installed": True,
        "path": str(path),
        "time": args.time,
    }, indent=2))


def _handle_uninstall_schedule(args) -> None:
    scheduler = _scheduler_module()
    if sys.platform == "darwin":
        scheduler.uninstall_macos()
    else:
        scheduler.uninstall_linux()
    print(json.dumps({"uninstalled": True}, indent=2))


def _handle_schedule_status(args) -> None:
    scheduler = _scheduler_module()
    print(json.dumps(scheduler.status(), indent=2))


def _handle_enable_auto(args) -> None:
    publish_attestation, publish_error = _validate_publish_attestation(args.publish_attestation)
    if publish_error:
        print(json.dumps({
            "error": "Missing or invalid publish attestation.",
            "publish_attestation_error": publish_error,
            "hint": "Ask the user to explicitly approve publishing, then pass a detailed text attestation.",
            "blocked_on_step": "enable-auto",
            "next_command": (
                "dataclaw enable-auto --publish-attestation "
                "\"User explicitly approved publishing to Hugging Face on YYYY-MM-DD.\""
            ),
        }, indent=2))
        sys.exit(1)

    config = load_config()
    if config.get("stage") not in {"confirmed", "done"}:
        print(json.dumps({
            "error": "Auto mode requires a confirmed export review.",
            "hint": "Run dataclaw confirm first",
            "blocked_on_step": "enable-auto",
            "next_command": "dataclaw confirm",
        }, indent=2))
        sys.exit(2)

    repo_id = config.get("repo")
    if not repo_id:
        hf_user = get_hf_username()
        if hf_user:
            repo_id = default_repo_name(hf_user)
            config["repo"] = repo_id
        else:
            print(json.dumps({
                "error": "No HF repo configured and HF login not detected",
                "fix": "Run `dataclaw config --repo <user>/<repo>` or `huggingface-cli login` first.",
                "blocked_on_step": "enable-auto",
            }, indent=2))
            sys.exit(2)

    binary = shutil.which("dataclaw") or sys.argv[0]
    config["auto"] = {
        "enabled": True,
        "policy": args.policy,
        "full_name": args.full_name,
        "skip_full_name_scan": args.skip_full_name_scan,
        "enable_privacy_filter": args.enable_privacy_filter,
        "publish_attestation": publish_attestation,
        "binary": binary,
        "enabled_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _set_privacy_filter_enabled(config, args.enable_privacy_filter)
    save_config(config)
    print(json.dumps({
        "enabled": True,
        "repo": repo_id,
        "policy": args.policy,
        "next_command": "dataclaw install-schedule",
    }, indent=2))


def _commit_attr(commit: object, *names: str) -> object:
    for name in names:
        if isinstance(commit, dict) and name in commit:
            return commit[name]
        if hasattr(commit, name):
            return getattr(commit, name)
    return None


def _handle_rollback(args) -> None:
    config = load_config()
    repo_id = args.repo or config.get("repo")
    if not repo_id:
        print(json.dumps({
            "error": "No HF repo configured.",
            "hint": "Pass --repo <user>/<repo> or run dataclaw config --repo <user>/<repo>.",
            "blocked_on_step": "rollback",
        }, indent=2))
        sys.exit(2)

    try:
        from huggingface_hub import HfApi, CommitOperationAdd, CommitOperationDelete
    except ImportError:
        print(json.dumps({
            "error": "huggingface_hub not installed.",
            "hint": "Run: pip install huggingface_hub",
            "blocked_on_step": "rollback",
        }, indent=2))
        sys.exit(2)

    api = HfApi()
    if args.list_commits:
        commits = api.list_repo_commits(repo_id, repo_type="dataset")[:args.limit]
        rows = []
        for commit in commits:
            rows.append({
                "sha": _commit_attr(commit, "commit_id", "sha"),
                "title": _commit_attr(commit, "title", "message"),
                "created_at": str(_commit_attr(commit, "created_at", "date")),
            })
        print(json.dumps(rows, indent=2))
        return

    target = args.commit
    current_commits = api.list_repo_commits(repo_id, repo_type="dataset")
    previous_commit = _commit_attr(current_commits[0], "commit_id", "sha") if current_commits else None

    target_files = set(api.list_repo_files(repo_id, repo_type="dataset", revision=target))
    current_files = set(api.list_repo_files(repo_id, repo_type="dataset"))
    operations = []
    for path in sorted(target_files):
        local = api.hf_hub_download(
            repo_id=repo_id,
            filename=path,
            repo_type="dataset",
            revision=target,
        )
        operations.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=local))
    for path in sorted(current_files - target_files):
        operations.append(CommitOperationDelete(path_in_repo=path))

    run_id = uuid.uuid4().hex
    logger = dc_logging.setup_logging(run_id)
    rollback_event = {
        "initiator": os.environ.get("USER", "unknown"),
        "target_commit": target,
        "previous_commit": previous_commit,
        "reverted_files": len(operations),
        "dry_run": args.dry_run,
    }
    logger.info(
        "rollback",
        extra={
            "phase": "rollback",
            "extra": rollback_event,
            "extra_data": rollback_event,
            **rollback_event,
        },
    )

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "repo": repo_id,
            "target_commit": target,
            "previous_commit": previous_commit,
            "reverted_files": len(operations),
            "operations": [
                getattr(op, "path_in_repo", None)
                for op in operations
            ],
        }, indent=2))
        return

    new_commit = api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=f"Rollback to {target[:8]}",
    )
    print(json.dumps({
        "rolled_back": True,
        "target_commit": target,
        "previous_commit": previous_commit,
        "new_commit": _commit_attr(new_commit, "commit_id", "oid", "sha") or str(new_commit),
    }, indent=2))


def _resolve_export_inputs(
    config: DataClawConfig,
    *,
    all_projects: bool = False,
) -> tuple[list[dict], Anonymizer, list[str]]:
    source_choice, _ = _resolve_source_choice(str(config.get("source") or "auto"), config)
    source_filter = _normalize_source_filter(source_choice)
    projects = _filter_projects_by_source(discover_projects(), source_filter)
    excluded = set(config.get("excluded_projects", []))
    if all_projects:
        excluded = set()
    included = [p for p in projects if p["display_name"] not in excluded]
    extra_usernames = config.get("redact_usernames", [])
    anonymizer = Anonymizer(extra_usernames=extra_usernames)
    custom_strings = config.get("redact_strings", [])
    return included, anonymizer, custom_strings


def _build_run_summary(
    run_id: str,
    result: str,
    manifest: dict,
    run_dir: Path,
    started_at: str,
    **extra,
) -> dict[str, Any]:
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "result": result,
        "total_sessions_new": manifest.get("total_sessions_new", 0),
        "staging_dir": str(run_dir),
    }
    summary.update(extra)
    return summary


def _scan_pf_new(run_dir: Path, config: DataClawConfig, logger=None) -> list:
    if logger is None:
        logger = dc_logging.logging.getLogger("dataclaw")
    if not _privacy_filter_enabled(config):
        logger.info(
            "privacy_filter_skipped",
            extra={
                "phase": "privacy_filter",
                "extra": {
                    "run_dir": str(run_dir),
                    "reason": "model_privacy_filter_disabled",
                    "mechanical_pii_gate": "already_enforced",
                },
            },
        )
        return []
    try:
        from . import privacy_filter as pf
    except Exception as e:
        logger.warning(
            "privacy_filter_import_failed",
            extra={"phase": "privacy_filter", "extra": {"error_type": type(e).__name__, "error": str(e)}},
        )
        raise PrivacyFilterFailed(f"privacy filter import failed: {e}") from e
    try:
        manifest = _read_manifest(run_dir)
        privacy_config = config.get("privacy_filter") if isinstance(config.get("privacy_filter"), dict) else {}
        device = privacy_config.get("device") if isinstance(privacy_config, dict) else None
        effective_device = pf.resolve_device(device)
        min_score = float(
            privacy_config.get("min_score", getattr(pf, "_DEFAULT_MIN_SCORE", 0.85))
            if isinstance(privacy_config, dict)
            else getattr(pf, "_DEFAULT_MIN_SCORE", 0.85)
        )
        include_tool_io = bool(privacy_config.get("include_tool_io", False)) if isinstance(privacy_config, dict) else False
        model_roles = set(privacy_config.get("roles", ["user"])) if isinstance(privacy_config, dict) else {"user"}
        model_roles = {role for role in model_roles if isinstance(role, str)}
        if not model_roles:
            model_roles = {"user"}
        def log_progress(event: str, payload: dict[str, Any]) -> None:
            logger.info(event, extra={"phase": "privacy_filter", "extra": payload})

        logger.info(
            "privacy_filter_scan_started",
            extra={
                "phase": "privacy_filter",
                "extra": {
                    "run_dir": str(run_dir),
                    "shard_count": len(manifest.get("shards", [])),
                "total_sessions_new": manifest.get("total_sessions_new", 0),
                "device": effective_device,
                "configured_device": device or "auto",
                "min_score": min_score,
                "mode": "redact",
                "include_tool_io": include_tool_io,
                "roles": sorted(model_roles),
            },
            },
        )
        findings = pf.redact_shards(
            run_dir,
            manifest,
            device=device,
            min_score=min_score,
            progress_callback=log_progress,
            include_tool_io=include_tool_io,
            roles=model_roles,
        )
        logger.info(
            "privacy_filter_scan_completed",
            extra={
                "phase": "privacy_filter",
                "extra": {
                    "run_dir": str(run_dir),
                    "total_findings": len(findings),
                    "redactions": len(findings),
                    "new_findings": 0,
                    "known_findings": 0,
                },
            },
        )
        return []
    except Exception as e:
        logger.warning(
            "privacy_filter_scan_failed",
            extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "error_type": type(e).__name__, "error": str(e)}},
        )
        raise PrivacyFilterFailed(f"privacy filter scan failed: {e}") from e


def _advance_last_export_cutoff(config: DataClawConfig, manifest: dict) -> None:
    cutoff = dict(config.get("last_export_cutoff") or {})
    for source, end_time in (manifest.get("max_end_time_by_source") or {}).items():
        if isinstance(source, str) and isinstance(end_time, str):
            cutoff[source] = end_time
    config["last_export_cutoff"] = cutoff


def _record_last_dataset_update(
    config: DataClawConfig,
    manifest: dict,
    repo_id: str,
    *,
    repo_url: str | None = None,
) -> None:
    config["last_dataset_update"] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "repo": repo_id,
        "repo_url": repo_url,
        "sources": manifest.get("sources", []),
        "shard_count": len(manifest.get("shards", [])),
        "total_sessions_new": manifest.get("total_sessions_new", 0),
        "total_sessions_in_shards": manifest.get("total_sessions_in_shards", 0),
        "max_end_time_by_source": manifest.get("max_end_time_by_source", {}),
        "manifest_export_id": manifest.get("export_id"),
        "manifest_finished_at": manifest.get("finished_at"),
    }


def _handle_auto(args) -> None:
    config = load_config()
    run_id = uuid.uuid4().hex
    started_at = datetime.now(tz=timezone.utc).isoformat()
    logger = dc_logging.setup_logging(run_id)
    logger.info("auto_run_started", extra={"phase": "start", "extra": {"force": bool(getattr(args, "force", False)), "dry_run": bool(getattr(args, "dry_run", False)), "retry_only": bool(getattr(args, "retry_only", False)), "repo": config.get("repo"), "source": config.get("source"), "stage": config.get("stage"), "privacy_filter_enabled": _privacy_filter_enabled(config)}})
    lock_path = STAGING_ROOT.parent / ".auto.lock"
    try:
        lock = _AutoRunLock(lock_path)
        lock.__enter__()
    except AutoRunAlreadyActive:
        logger.warning("auto_already_running", extra={"phase": "gate", "extra": {"lock_path": str(lock_path)}})
        print(json.dumps({
            "result": "busy",
            "run_id": run_id,
            "error": "Another DataClaw run is already active.",
            "hint": "Wait for the current Run Now job to finish before starting another one.",
        }, indent=2))
        sys.exit(2)
    try:
        _handle_auto_locked(args, config, run_id, logger, started_at)
    finally:
        lock.__exit__(None, None, None)


def _handle_auto_locked(args, config: DataClawConfig, run_id: str, logger, started_at: str) -> None:

    if args.retry_only:
        logger.info("auto_retry_only_start", extra={"phase": "retry", "extra": {"staging_root": str(STAGING_ROOT)}})
        _handle_auto_retry_only(args, config, run_id, logger, started_at)
        return

    auto = config.get("auto")
    if not isinstance(auto, dict) or not auto.get("enabled"):
        logger.warning("auto_disabled", extra={"phase": "gate", "extra": {"has_auto_config": isinstance(auto, dict)}})
        print(json.dumps({
            "error": "Auto mode is not enabled.",
            "hint": "Run dataclaw enable-auto first.",
            "blocked_on_step": "auto",
            "next_command": "dataclaw enable-auto --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
        }, indent=2))
        sys.exit(2)

    repo_id = config.get("repo")
    if not repo_id:
        logger.warning("no_repo_configured", extra={"phase": "gate", "extra": {"source": config.get("source")}})
        print(json.dumps({
            "error": "No HF repo configured.",
            "hint": "Run dataclaw config --repo <user>/<repo> first.",
            "blocked_on_step": "auto",
        }, indent=2))
        sys.exit(2)
    token_present = _resolve_hf_token() is not None
    logger.info("auto_gate_checked", extra={"phase": "gate", "extra": {"repo": repo_id, "policy": auto.get("policy"), "token_present": token_present, "privacy_filter_enabled": _privacy_filter_enabled(config), "auto_privacy_filter_enabled": auto.get("enable_privacy_filter")}})
    if not token_present:
        logger.warning("no_hf_token", extra={"phase": "gate", "extra": {"repo": repo_id}})
        print(json.dumps({
            "error": "No Hugging Face token configured.",
            "hint": HF_LOGIN_HINT,
            "blocked_on_step": "auto",
        }, indent=2))
        sys.exit(2)

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info("staging_cleanup_started", extra={"phase": "staging", "extra": {"staging_root": str(STAGING_ROOT), "keep_published": 3, "keep_failed": 1}})
    _cleanup_published(STAGING_ROOT, keep=3)
    failed_deleted = _cleanup_failed_staging(STAGING_ROOT, keep=1)
    logger.info("staging_cleanup_finished", extra={"phase": "staging", "extra": {"staging_root": str(STAGING_ROOT), "failed_deleted": failed_deleted}})
    warnings: list[str] = []
    staging_size = _staging_size_bytes(STAGING_ROOT)
    logger.info("staging_size_checked", extra={"phase": "staging", "extra": {"staging_root": str(STAGING_ROOT), "size_bytes": staging_size, "warning_threshold_bytes": STAGING_SIZE_WARN_BYTES}})
    if staging_size > STAGING_SIZE_WARN_BYTES:
        warnings.append("Staging directory exceeds 5 GB; run dataclaw clean-staging after reviewing failed runs.")

    run_dir = STAGING_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    logger.info("run_staging_created", extra={"phase": "staging", "extra": {"run_id": run_id, "run_dir": str(run_dir)}})
    logger.info("resolve_export_inputs_started", extra={"phase": "discover", "extra": {"source": config.get("source"), "excluded_projects": len(config.get("excluded_projects", [])), "redact_strings": len(config.get("redact_strings", [])), "redact_usernames": len(config.get("redact_usernames", []))}})
    projects, anonymizer, custom_strings = _resolve_export_inputs(config)
    logger.info("resolve_export_inputs_finished", extra={"phase": "discover", "extra": {"included_projects": len(projects), "custom_redactions": len(custom_strings)}})
    configured_since = dict(config.get("last_export_cutoff") or {})
    since_for_export = configured_since
    if configured_since:
        logger.info("remote_dataset_check_started", extra={"phase": "export", "extra": {"repo": repo_id, "cutoff_sources": sorted(configured_since.keys())}})
        try:
            remote_status = _inspect_remote_dataset(str(repo_id))
        except Exception as e:
            record_last_auto_run("error", config, staging_dir=str(run_dir), warnings=warnings, error=f"remote dataset check failed: {e}")
            save_config(config)
            logger.warning(
                "remote_dataset_check_failed",
                extra={"phase": "export", "extra": {"repo": repo_id, "error_type": type(e).__name__, "error": str(e), "run_id": run_id}},
            )
            print(json.dumps({
                "result": "error",
                "run_id": run_id,
                "staging_dir": str(run_dir),
                "error": f"remote dataset check failed: {e}",
            }, indent=2))
            sys.exit(4)
        missing_shards = remote_status.get("missing_shards") or []
        logger.info(
            "remote_dataset_check_finished",
            extra={
                "phase": "export",
                "extra": {
                    "repo": repo_id,
                    "manifest_exists": remote_status.get("manifest_exists"),
                    "manifest_error": remote_status.get("manifest_error"),
                    "files_checked": remote_status.get("files_checked"),
                    "remote_shard_count": remote_status.get("shard_count", 0),
                    "missing_shard_count": len(missing_shards),
                    "remote_max_end_time_by_source": remote_status.get("max_end_time_by_source", {}),
                    "remote_finished_at": remote_status.get("finished_at"),
                },
            },
        )
        if (not remote_status.get("manifest_exists")) or missing_shards:
            since_for_export = {}
            reason = "missing_manifest" if not remote_status.get("manifest_exists") else "missing_shards"
            warnings.append(f"Remote dataset {reason}; rebuilt from full local history instead of using last_export_cutoff.")
            logger.warning(
                "incremental_fallback_full_rebuild",
                extra={
                    "phase": "export",
                    "extra": {
                        "repo": repo_id,
                        "reason": reason,
                        "missing_shard_count": len(missing_shards),
                        "cutoff_sources_ignored": sorted(configured_since.keys()),
                    },
                },
            )
    logger.info("export_shards_started", extra={"phase": "export", "extra": {"run_dir": str(run_dir), "included_projects": len(projects), "cooled_only": True, "since_sources": sorted(since_for_export.keys()), "incremental": bool(since_for_export)}})
    manifest = export_to_shards(
        projects,
        run_dir,
        anonymizer,
        config,
        custom_strings=custom_strings,
        cooled_only=True,
        since=since_for_export,
        fetch_existing=True,
        logger=logger,
    )
    logger.info("export_shards_finished", extra={"phase": "export", "extra": {"run_dir": str(run_dir), "total_sessions_new": manifest.get("total_sessions_new", 0), "shard_count": len(manifest.get("shards", [])), "sources": manifest.get("sources", []), "max_end_time_by_source": manifest.get("max_end_time_by_source", {})}})

    if manifest.get("total_sessions_new", 0) == 0:
        record_last_auto_run(
            "noop",
            config,
            staging_dir=str(run_dir),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        shutil.rmtree(run_dir)
        logger.info("auto_noop", extra={"phase": "finish", "extra": {"run_id": run_id, "reason": "no_new_sessions", "run_dir_removed": str(run_dir), "warnings": warnings}})
        print(json.dumps({"result": "noop", "run_id": run_id, "total_sessions_new": 0}, indent=2))
        return

    effective_policy = args.policy_override or auto.get("policy") or "strict"
    logger.info("mechanical_pii_scan_started", extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "policy": effective_policy}})
    pii_findings = _scan_pii_dir(run_dir, manifest, logger=logger)
    pii_summary = _summarize_pii_findings(pii_findings)
    logger.info(
        "mechanical_pii_scan_finished",
        extra={
            "phase": "privacy_filter",
            "extra": {
                "run_dir": str(run_dir),
                **pii_summary,
                "policy": effective_policy,
            },
        },
    )
    redaction_pass = 0
    while pii_findings and redaction_pass < 5:
        redaction_pass += 1
        logger.info(
            "mechanical_pii_redaction_started",
            extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "pass": redaction_pass, **pii_summary}},
        )
        redaction_summary = _redact_mechanical_findings_dir(run_dir, manifest, pii_findings)
        logger.info(
            "mechanical_pii_redaction_finished",
            extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "pass": redaction_pass, **redaction_summary}},
        )
        if redaction_summary.get("redactions", 0) <= 0:
            logger.warning(
                "mechanical_pii_redaction_stalled",
                extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "pass": redaction_pass, **pii_summary}},
            )
            break
        logger.info("mechanical_pii_rescan_started", extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "policy": effective_policy}})
        pii_findings = _scan_pii_dir(run_dir, manifest, logger=logger)
        pii_summary = _summarize_pii_findings(pii_findings)
        logger.info(
            "mechanical_pii_rescan_finished",
            extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "pass": redaction_pass, **pii_summary, "policy": effective_policy}},
        )
    if pii_findings and redaction_pass >= 5:
        logger.warning(
            "mechanical_pii_redaction_pass_limit_reached",
            extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "passes": redaction_pass, **pii_summary}},
        )
    if effective_policy == "strict" and pii_findings and not args.force:
        record_last_auto_run(
            "blocked",
            config,
            staging_dir=str(run_dir),
            privacy_findings=sum(len(value) for value in pii_findings.values() if isinstance(value, list)),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        logger.warning("auto_blocked_mechanical_pii_findings", extra={"phase": "privacy_filter", "extra": {"run_id": run_id, "staging_dir": str(run_dir), **pii_summary, "warnings": warnings}})
        print(json.dumps({
            "result": "blocked",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "mechanical_pii_findings": pii_summary,
        }, indent=2))
        sys.exit(3)

    logger.info("privacy_filter_started", extra={"phase": "privacy_filter", "extra": {"enabled": _privacy_filter_enabled(config), "policy": effective_policy, "force": bool(args.force), "run_dir": str(run_dir)}})
    try:
        new_findings = _scan_pf_new(run_dir, config, logger=logger)
    except PrivacyFilterFailed as e:
        record_last_auto_run(
            "error",
            config,
            staging_dir=str(run_dir),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        logger.warning("auto_blocked_privacy_filter_failed", extra={"phase": "privacy_filter", "extra": {"run_id": run_id, "staging_dir": str(run_dir), "error": str(e), "warnings": warnings}})
        print(json.dumps({
            "result": "error",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "error": str(e),
        }, indent=2))
        sys.exit(4)
    logger.info("privacy_filter_finished", extra={"phase": "privacy_filter", "extra": {"new_findings": len(new_findings), "policy": effective_policy, "force": bool(args.force)}})
    if effective_policy == "strict" and new_findings and not args.force:
        record_last_auto_run(
            "blocked",
            config,
            staging_dir=str(run_dir),
            privacy_findings=len(new_findings),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        logger.warning("auto_blocked_privacy_findings", extra={"phase": "privacy_filter", "extra": {"run_id": run_id, "staging_dir": str(run_dir), "privacy_findings": len(new_findings), "warnings": warnings}})
        print(json.dumps({
            "result": "blocked",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "privacy_findings": len(new_findings),
        }, indent=2))
        sys.exit(3)

    logger.info("final_mechanical_pii_scan_started", extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), "policy": effective_policy}})
    final_pii_findings = _scan_pii_dir(run_dir, manifest, logger=logger)
    final_pii_summary = _summarize_pii_findings(final_pii_findings)
    logger.info(
        "final_mechanical_pii_scan_finished",
        extra={"phase": "privacy_filter", "extra": {"run_dir": str(run_dir), **final_pii_summary, "policy": effective_policy}},
    )
    if effective_policy == "strict" and final_pii_findings and not args.force:
        record_last_auto_run(
            "blocked",
            config,
            staging_dir=str(run_dir),
            privacy_findings=sum(len(value) for value in final_pii_findings.values() if isinstance(value, list)),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        logger.warning("auto_blocked_final_mechanical_pii_findings", extra={"phase": "privacy_filter", "extra": {"run_id": run_id, "staging_dir": str(run_dir), **final_pii_summary, "warnings": warnings}})
        print(json.dumps({
            "result": "blocked",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "mechanical_pii_findings": final_pii_summary,
        }, indent=2))
        sys.exit(3)

    if args.dry_run:
        record_last_auto_run(
            "dry-run",
            config,
            staging_dir=str(run_dir),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        logger.info("auto_dry_run_finished", extra={"phase": "finish", "extra": {"run_id": run_id, "staging_dir": str(run_dir), "total_sessions_new": manifest.get("total_sessions_new", 0), "warnings": warnings}})
        print(json.dumps({
            "result": "dry-run",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "total_sessions_new": manifest.get("total_sessions_new", 0),
        }, indent=2))
        return

    try:
        logger.info("push_started", extra={"phase": "push", "extra": {"run_id": run_id, "repo": repo_id, "run_dir": str(run_dir), "total_sessions_new": manifest.get("total_sessions_new", 0), "shard_count": len(manifest.get("shards", []))}})
        repo_url, push_attempts, backoff_seconds_total = _push_with_retry(
            run_dir,
            str(repo_id),
            manifest,
            logger,
        )
    except PushFailed as e:
        logger.warning("push_failed", extra={"phase": "push", "extra": {"run_id": run_id, "attempts": e.attempts, "backoff_seconds_total": e.backoff_seconds_total, "error_type": type(e.cause).__name__, "error": str(e.cause), "staging_dir": str(run_dir)}})
        record_last_auto_run(
            "error",
            config,
            push_attempts=e.attempts,
            backoff_seconds_total=e.backoff_seconds_total,
            error=str(e.cause),
            staging_dir=str(run_dir),
            warnings=warnings,
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        dc_logging.write_run_summary(
            run_dir,
            _build_run_summary(
                run_id,
                "error",
                manifest,
                run_dir,
                started_at,
                push_attempts=e.attempts,
                backoff_seconds_total=e.backoff_seconds_total,
                error=str(e.cause),
                warnings=warnings,
            ),
        )
        logger.info("run_summary_written", extra={"phase": "finish", "extra": {"run_id": run_id, "result": "error", "staging_dir": str(run_dir)}})
        print(json.dumps({
            "result": "error",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "push_attempts": e.attempts,
            "backoff_seconds_total": e.backoff_seconds_total,
            "error": str(e.cause),
        }, indent=2))
        sys.exit(4)

    logger.info("cutoff_update_started", extra={"phase": "finish", "extra": {"max_end_time_by_source": manifest.get("max_end_time_by_source", {})}})
    _advance_last_export_cutoff(config, manifest)
    _record_last_dataset_update(config, manifest, str(repo_id), repo_url=repo_url)
    published_root = STAGING_ROOT / PUBLISHED_DIRNAME
    published_root.mkdir(parents=True, exist_ok=True)
    published_dir = published_root / run_id
    logger.info("staging_publish_move_started", extra={"phase": "finish", "extra": {"from": str(run_dir), "to": str(published_dir)}})
    shutil.move(str(run_dir), str(published_dir))
    record_last_auto_run(
        "pushed",
        config,
        repo_url=repo_url,
        staging_dir=str(published_dir),
        push_attempts=push_attempts,
        backoff_seconds_total=backoff_seconds_total,
        warnings=warnings,
        repo=str(repo_id),
        **_auto_run_manifest_fields(manifest),
    )
    save_config(config)
    logger.info("config_saved_after_push", extra={"phase": "finish", "extra": {"run_id": run_id, "result": "pushed", "published_dir": str(published_dir), "repo_url": repo_url, "push_attempts": push_attempts, "backoff_seconds_total": backoff_seconds_total}})
    dc_logging.write_run_summary(
        published_dir,
        _build_run_summary(
            run_id,
            "pushed",
            manifest,
            published_dir,
            started_at,
            repo_url=repo_url,
            push_attempts=push_attempts,
            backoff_seconds_total=backoff_seconds_total,
            warnings=warnings,
        ),
    )
    logger.info("run_summary_written", extra={"phase": "finish", "extra": {"run_id": run_id, "result": "pushed", "published_dir": str(published_dir)}})
    print(json.dumps({
        "result": "pushed",
        "run_id": run_id,
        "repo_url": repo_url,
        "staging_dir": str(published_dir),
        "push_attempts": push_attempts,
        "backoff_seconds_total": backoff_seconds_total,
    }, indent=2))


def _handle_auto_retry_only(args, config: DataClawConfig, run_id: str, logger, started_at: str) -> None:
    run_dir = _find_failed_run_dir(STAGING_ROOT)
    if run_dir is None:
        print(json.dumps({
            "error": "No failed auto staging run found.",
            "hint": "Run dataclaw auto first, or check dataclaw status --logs.",
            "blocked_on_step": "auto --retry-only",
        }, indent=2))
        sys.exit(2)

    repo_id = config.get("repo")
    if not repo_id:
        print(json.dumps({
            "error": "No HF repo configured.",
            "hint": "Run dataclaw config --repo <user>/<repo> first.",
            "blocked_on_step": "auto --retry-only",
            "staging_dir": str(run_dir),
        }, indent=2))
        sys.exit(2)

    manifest = _read_manifest(run_dir)
    try:
        repo_url, push_attempts, backoff_seconds_total = _push_with_retry(
            run_dir,
            str(repo_id),
            manifest,
            logger,
        )
    except PushFailed as e:
        record_last_auto_run(
            "error",
            config,
            push_attempts=e.attempts,
            backoff_seconds_total=e.backoff_seconds_total,
            error=str(e.cause),
            staging_dir=str(run_dir),
            repo=str(repo_id),
            **_auto_run_manifest_fields(manifest),
        )
        save_config(config)
        print(json.dumps({
            "result": "error",
            "run_id": run_id,
            "staging_dir": str(run_dir),
            "push_attempts": e.attempts,
            "backoff_seconds_total": e.backoff_seconds_total,
            "error": str(e.cause),
        }, indent=2))
        sys.exit(4)

    _advance_last_export_cutoff(config, manifest)
    _record_last_dataset_update(config, manifest, str(repo_id), repo_url=repo_url)
    published_root = STAGING_ROOT / PUBLISHED_DIRNAME
    published_root.mkdir(parents=True, exist_ok=True)
    published_dir = published_root / run_dir.name
    shutil.move(str(run_dir), str(published_dir))
    record_last_auto_run(
        "pushed",
        config,
        repo_url=repo_url,
        staging_dir=str(published_dir),
        push_attempts=push_attempts,
        backoff_seconds_total=backoff_seconds_total,
        repo=str(repo_id),
        **_auto_run_manifest_fields(manifest),
    )
    save_config(config)
    dc_logging.write_run_summary(
        published_dir,
        _build_run_summary(
            run_id,
            "pushed",
            manifest,
            published_dir,
            started_at,
            repo_url=repo_url,
            push_attempts=push_attempts,
            backoff_seconds_total=backoff_seconds_total,
        ),
    )
    print(json.dumps({
        "result": "pushed",
        "run_id": run_id,
        "repo_url": repo_url,
        "staging_dir": str(published_dir),
        "push_attempts": push_attempts,
        "backoff_seconds_total": backoff_seconds_total,
    }, indent=2))


def _run_export(args) -> None:
    """Run the export flow — discover, anonymize, export, optionally push."""
    config = load_config()
    source_choice, source_explicit = _resolve_source_choice(args.source, config)
    source_filter = _normalize_source_filter(source_choice)

    if not source_explicit:
        print(json.dumps({
            "error": "Source scope is not confirmed yet.",
            "hint": (
                "Explicitly choose one source scope before exporting: "
                "`claude`, `codex`, `gemini`, `opencode`, `openclaw`, `kimi`, `hermes`, `custom`, or `all`."
            ),
            "required_action": (
                "Ask the user whether to export claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all. "
                "Then run `dataclaw config --source <claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all>` "
                "or pass `--source <claude|codex|gemini|opencode|openclaw|kimi|hermes|custom|all>` on the export command."
            ),
            "allowed_sources": sorted(EXPLICIT_SOURCE_CHOICES),
            "blocked_on_step": "Step 2/6",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "dataclaw config --source all",
        }, indent=2))
        sys.exit(1)

    # Gate: require `dataclaw confirm` before pushing
    if not args.no_push:
        if args.attest_user_approved_publish and not args.publish_attestation:
            print(json.dumps({
                "error": "Deprecated publish attestation flag was provided.",
                "hint": "Use --publish-attestation with a detailed text statement.",
                "blocked_on_step": "Step 3/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": (
                    "dataclaw export --publish-attestation "
                    "\"User explicitly approved publishing to Hugging Face on YYYY-MM-DD.\""
                ),
            }, indent=2))
            sys.exit(1)
        if config.get("stage") != "confirmed":
            print(json.dumps({
                "error": "You must run `dataclaw confirm` before pushing.",
                "hint": "Export first with --no-push, review the data, then run `dataclaw confirm`.",
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": "dataclaw confirm",
            }, indent=2))
            sys.exit(1)
        publish_attestation, publish_error = _validate_publish_attestation(args.publish_attestation)
        if publish_error:
            print(json.dumps({
                "error": "Missing or invalid publish attestation.",
                "publish_attestation_error": publish_error,
                "hint": "Ask the user to explicitly approve publishing, then pass a detailed text attestation.",
                "blocked_on_step": "Step 3/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": (
                    "dataclaw export --publish-attestation "
                    "\"User explicitly approved publishing to Hugging Face on YYYY-MM-DD.\""
                ),
            }, indent=2))
            sys.exit(1)

        review_attestations = config.get("review_attestations", {})
        review_verification = config.get("review_verification", {})
        verified_full_name = _normalize_attestation_text(review_verification.get("full_name"))
        _, review_errors, _ = _collect_review_attestations(
            attest_asked_full_name=review_attestations.get("asked_full_name"),
            attest_asked_sensitive=review_attestations.get("asked_sensitive_entities"),
            attest_manual_scan=review_attestations.get("manual_scan_done"),
            full_name=verified_full_name if verified_full_name else None,
            skip_full_name_scan=bool(review_verification.get("full_name_scan_skipped", False)),
        )
        if not verified_full_name and not review_verification.get("full_name_scan_skipped", False):
            review_errors["asked_full_name"] = (
                "Missing verified full-name scan from confirm step; rerun confirm (or use --skip-full-name-scan if the user declined)."
            )
        verified_manual_count = review_verification.get("manual_scan_sessions")
        if not isinstance(verified_manual_count, int) or verified_manual_count < MIN_MANUAL_SCAN_SESSIONS:
            review_errors["manual_scan_done"] = (
                "Missing verified manual scan evidence from confirm step; rerun confirm."
            )

        if review_errors:
            print(json.dumps({
                "error": "Missing or invalid review attestations from confirm step.",
                "attestation_errors": review_errors,
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": CONFIRM_COMMAND_EXAMPLE,
            }, indent=2))
            sys.exit(1)

        config["publish_attestation"] = publish_attestation
        save_config(config)

    print("=" * 50)
    print("  DataClaw — Claude/Codex Log Exporter")
    print("=" * 50)

    if not _has_session_sources(source_filter):
        if source_filter == "claude":
            print(f"Error: {CLAUDE_DIR} not found.", file=sys.stderr)
        elif source_filter == "codex":
            print(f"Error: {CODEX_DIR} not found.", file=sys.stderr)
        elif source_filter == "gemini":
            from .parser import GEMINI_DIR
            print(f"Error: {GEMINI_DIR} not found.", file=sys.stderr)
        elif source_filter == "hermes":
            print(f"Error: {HERMES_DIR} not found.", file=sys.stderr)
        else:
            print("Error: none of the supported agent session directories were found.", file=sys.stderr)
        sys.exit(1)

    projects = _filter_projects_by_source(discover_projects(), source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.", file=sys.stderr)
        sys.exit(1)

    if not args.all_projects and not config.get("projects_confirmed", False):
        excluded = set(config.get("excluded_projects", []))
        list_command = f"dataclaw list --source {source_choice}"
        print(json.dumps({
            "error": "Project selection is not confirmed yet.",
            "hint": (
                f"Run `{list_command}`, present the full project list to the user, discuss which projects to exclude, then run "
                "`dataclaw config --exclude \"p1,p2\"` or `dataclaw config --confirm-projects`."
            ),
            "required_action": (
                "Send the full project/folder list below to the user in a message and get explicit "
                "confirmation on exclusions before exporting."
            ),
            "projects": [
                {
                    "name": p["display_name"],
                    "source": p.get("source", "claude"),
                    "sessions": p["session_count"],
                    "size": _format_size(p["total_size_bytes"]),
                    "excluded": p["display_name"] in excluded,
                }
                for p in projects
            ],
            "blocked_on_step": "Step 3/6",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "dataclaw config --confirm-projects",
        }, indent=2))
        sys.exit(1)

    total_sessions = sum(p["session_count"] for p in projects)
    total_size = sum(p["total_size_bytes"] for p in projects)
    print(f"\nFound {total_sessions} sessions across {len(projects)} projects "
          f"({_format_size(total_size)} raw)")
    print(f"Source scope: {source_choice}")

    # Resolve repo — CLI flag > config > auto-detect from HF username
    repo_id = args.repo or config.get("repo")
    if not repo_id and not args.no_push:
        hf_user = get_hf_username()
        if hf_user:
            repo_id = default_repo_name(hf_user)
            print(f"\nAuto-detected HF repo: {repo_id}")
            config["repo"] = repo_id
            save_config(config)

    # Apply exclusions
    excluded = set(config.get("excluded_projects", []))
    if args.all_projects:
        excluded = set()

    resolve_config = cast(DataClawConfig, dict(config))
    resolve_config["source"] = source_filter
    included, anonymizer, custom_strings = _resolve_export_inputs(
        resolve_config,
        all_projects=args.all_projects,
    )
    excluded_projects = [p for p in projects if p["display_name"] in excluded]

    if excluded_projects:
        print(f"\nIncluding {len(included)} projects (excluding {len(excluded_projects)}):")
    else:
        print(f"\nIncluding all {len(included)} projects:")
    for p in included:
        print(f"  + {p['display_name']} ({p['session_count']} sessions)")
    for p in excluded_projects:
        print(f"  - {p['display_name']} (excluded)")

    if not included:
        print("\nNo projects to export. Run: dataclaw config --exclude ''")
        sys.exit(1)

    extra_usernames = config.get("redact_usernames", [])
    if extra_usernames:
        print(f"\nAnonymizing usernames: {', '.join(extra_usernames)}")
    if custom_strings:
        print(f"Redacting custom strings: {len(custom_strings)} configured")

    # Export
    output_path = args.output or Path("dataclaw_conversations.jsonl")

    print(f"\nExporting to {output_path}...")
    meta = export_to_jsonl(
        included, output_path, anonymizer, not args.no_thinking,
        custom_strings=custom_strings,
    )
    file_size = output_path.stat().st_size
    print(f"\nExported {meta['sessions']} sessions ({_format_size(file_size)})")
    if meta.get("skipped"):
        print(f"Skipped {meta['skipped']} abandoned/error sessions")
    if meta.get("redactions"):
        print(f"Redacted {meta['redactions']} secrets (API keys, tokens, emails, etc.)")
    print(f"Models: {', '.join(f'{m} ({c})' for m, c in sorted(meta['models'].items(), key=lambda x: -x[1]))}")

    _print_pii_guidance(output_path)

    config["last_export"] = {
        "timestamp": meta["exported_at"],
        "sessions": meta["sessions"],
        "models": meta["models"],
        "source": source_choice,
    }
    if args.no_push:
        config["stage"] = "review"
    save_config(config)

    if args.no_push:
        print(f"\nDone! JSONL file: {output_path}")
        abs_path = str(output_path.resolve())
        next_steps, next_command = _build_status_next_steps("review", config, None, None)
        json_block = {
            "stage": "review",
            "stage_number": 3,
            "total_stages": 4,
            "sessions": meta["sessions"],
            "source": source_choice,
            "output_file": abs_path,
            "pii_commands": _build_pii_commands(output_path),
            "next_steps": next_steps,
            "next_command": next_command,
        }
        print("\n---DATACLAW_JSON---")
        print(json.dumps(json_block, indent=2))
        return

    if not repo_id:
        print(f"\nNo HF repo. Log in first: huggingface-cli login")
        print(f"Then re-run dataclaw and it will auto-detect your username.")
        print(f"Or set manually: dataclaw config --repo username/my-personal-codex-data")
        print(f"\nLocal file: {output_path}")
        return

    push_to_huggingface(output_path, repo_id, meta)

    config["stage"] = "done"
    save_config(config)

    json_block = {
        "stage": "done",
        "stage_number": 4,
        "total_stages": 4,
        "dataset_url": f"https://huggingface.co/datasets/{repo_id}",
        "next_steps": [
            "Done! Dataset is live. To update later: dataclaw export",
            "To reconfigure: dataclaw prep then dataclaw config",
        ],
        "next_command": None,
    }
    print("\n---DATACLAW_JSON---")
    print(json.dumps(json_block, indent=2))


def _build_pii_commands(output_path: Path) -> list[str]:
    """Return grep commands for PII scanning."""
    if output_path.is_dir():
        p = str(output_path.resolve())
        grep_prefix = f"find {p} -name '*.jsonl' -type f -print0 | xargs -0"
        return [
            f"{grep_prefix} grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' | grep -v noreply | head -20",
            f"{grep_prefix} grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' | head -5",
            f"{grep_prefix} grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' | head -5",
            f"{grep_prefix} grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' | sort -u",
        ]
    p = str(output_path.resolve())
    return [
        f"grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {p} | grep -v noreply | head -20",
        f"grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {p} | head -5",
        f"grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {p} | head -5",
        f"grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {p} | sort -u",
    ]


def _print_pii_guidance(output_path: Path) -> None:
    """Print PII review guidance with concrete grep commands."""
    abs_output = output_path.resolve()
    if output_path.is_dir():
        scan_target = f"{abs_output}/**/*.jsonl"
        manifest_path = abs_output / MANIFEST_REL
    else:
        scan_target = str(abs_output)
        manifest_path = None
    print(f"\n{'=' * 50}")
    print("  IMPORTANT: Review your data before publishing!")
    print(f"{'=' * 50}")
    print("DataClaw's automatic redaction is NOT foolproof.")
    print("You should scan the exported data for remaining PII.")
    if manifest_path:
        print(f"Manifest: {manifest_path}")
    print()
    print("Quick checks (run these and review any matches):")
    print(f"  grep -i 'your_name' {scan_target}")
    print(f"  grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {scan_target} | grep -v noreply | head -20")
    print(f"  grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {scan_target} | head -5")
    print(f"  grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {scan_target} | head -5")
    print(f"  grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {scan_target} | sort -u")
    print()
    print("NEXT: Ask for full name to run an exact-name privacy check, then scan for it:")
    print(f"  grep -i 'THEIR_NAME' {scan_target} | head -10")
    print("  If user declines sharing full name: use dataclaw confirm --skip-full-name-scan with a skip attestation.")
    print()
    print("To add custom redactions, then re-export:")
    print("  dataclaw config --redact-usernames 'github_handle,discord_name'")
    print("  dataclaw config --redact 'secret-domain.com,my-api-key'")
    print(f"  dataclaw export --no-push -o {abs_output}")
    print()
    print(f"Found an issue? Help improve DataClaw: {REPO_URL}/issues")


if __name__ == "__main__":
    main()
