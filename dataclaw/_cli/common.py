"""Shared CLI constants and helpers."""

from typing import Any, Mapping

from ..config import DataClawConfig
from ..providers import PROVIDERS

HF_TAG = "dataclaw"
REPO_URL = "https://github.com/banodoco/dataclaw"
SKILL_URL = "https://raw.githubusercontent.com/banodoco/dataclaw/main/docs/SKILL.md"

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
    "Step 1/3: Export locally only: dataclaw export --no-push --output dataclaw_export.jsonl",
    "Step 2/3: Review/redact, then run confirm: dataclaw confirm ...",
    'Step 3/3: After explicit user approval, publish: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
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
    return [
        "Step 1/6: Run prep/list to review project scope: dataclaw prep && dataclaw list",
        f"Step 2/6: Explicitly choose source scope: dataclaw config --source {_source_scope_placeholder()}",
        "Step 3/6: Configure exclusions/redactions and confirm projects: dataclaw config ...",
        "Step 4/6: Export locally only: dataclaw export --no-push --output dataclaw_export.jsonl",
        "Step 5/6: Review and confirm: dataclaw confirm ...",
        'Step 6/6: After explicit user approval, publish: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
    ]


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
    try:
        from huggingface_hub import HfApi

        return HfApi().whoami()["name"]
    except ImportError:
        return None
    except (OSError, KeyError, ValueError):
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
                "Ask the user for their Hugging Face token. Sign up: https://huggingface.co/join — Create WRITE token: https://huggingface.co/settings/tokens",
                "Run: huggingface-cli login --token <THEIR_TOKEN> (NEVER run bare huggingface-cli login — it hangs)",
                'Run: dataclaw config --redact "<THEIR_TOKEN>" (so the token gets redacted from exports)',
                "Run: dataclaw prep (to confirm login and get next steps)",
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
                f"Ask the user to explicitly choose export source scope: {_all_provider_labels()} or all. "
                f"Then set it: dataclaw config --source {_source_scope_placeholder()}. "
                "Do not run export until source scope is explicitly confirmed."
            )
        else:
            steps.append(
                f"Source scope is currently set to '{configured_source}'. "
                f"If the user wants a different scope, run: dataclaw config --source {_source_scope_placeholder()}."
            )
        if not projects_confirmed:
            steps.append(
                f"Run: {list_command} — then send the FULL project/folder list to the user in your next message "
                "(name, source, sessions, size, excluded), and ask which to EXCLUDE."
            )
            steps.append(
                'Configure project scope: dataclaw config --exclude "project1,project2" '
                "or dataclaw config --confirm-projects (to include all listed projects). "
                "Do not run export until this folder review is confirmed."
            )
        steps.extend(
            [
                "Ask about GitHub/Discord usernames to anonymize and sensitive strings to redact. "
                'Configure: dataclaw config --redact-usernames "handle1" and dataclaw config --redact "string1"',
                "When done configuring, export locally: dataclaw export --no-push --output dataclaw_export.jsonl",
            ]
        )
        return (steps, None)

    if stage == "review":
        return (
            [
                "Ask the user for their full name to run an exact-name privacy check against the export. If they decline, you may skip this check with --skip-full-name-scan and include a clear attestation.",
                "Run PII scan commands and review results with the user.",
                "Ask the user: 'Are there any company names, internal project names, client names, private URLs, or other people's names in your conversations that you'd want redacted? Any custom domains or internal tools?' Add anything they mention with dataclaw config --redact.",
                "Do a deep manual scan: sample ~20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials in conversation text, and anything else that looks sensitive. Report findings to the user.",
                "If PII found in any of the above, add redactions (dataclaw config --redact) and re-export: dataclaw export --no-push",
                "Run: " + CONFIRM_COMMAND_EXAMPLE + " — scans for PII, shows project breakdown, and unlocks pushing.",
                'Do NOT push until the user explicitly confirms. Once confirmed, push: dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."',
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

    dataset_url = f"https://huggingface.co/datasets/{repo_id}" if repo_id else None
    return (
        [
            f"Done! Dataset is live{f' at {dataset_url}' if dataset_url else ''}. To update later: dataclaw export",
            "To reconfigure: dataclaw prep then dataclaw config",
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
