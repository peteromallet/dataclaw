"""Command orchestration for the DataClaw CLI."""

import argparse
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from .. import _json as json
from ..anonymizer import Anonymizer
from ..config import CONFIG_FILE, DataClawConfig
from ..providers import PROVIDERS
from .common import (
    CONFIRM_COMMAND_EXAMPLE,
    DEFAULT_SOURCE,
    EXPLICIT_SOURCE_CHOICES,
    EXPORT_REVIEW_PUBLISH_STEPS,
    MIN_MANUAL_SCAN_SESSIONS,
    REPO_URL,
    SOURCE_CHOICES,
    _all_provider_labels,
    _build_status_next_steps,
    _compute_stage,
    _filter_projects_by_source,
    _format_size,
    _mask_config_for_display,
    _mask_secret,
    _merge_config_list,
    _normalize_source_filter,
    _parse_csv_arg,
    _resolve_source_choice,
    _setup_to_publish_steps,
    _source_label,
    _source_scope_literals,
    _source_scope_placeholder,
    default_repo_name,
    emit_blocked_error,
    fingerprint_strings,
    format_elapsed_seconds,
    get_hf_username,
    hf_dataset_url,
    sha256_file,
)
from .review import (
    _build_pii_commands,
    _collect_review_attestations,
    _normalize_attestation_text,
    _print_pii_guidance,
    _validate_publish_attestation,
)


def _print_export_elapsed(start_time: float) -> None:
    elapsed = time.perf_counter() - start_time
    print(f"Total time: {format_elapsed_seconds(elapsed)}")


def list_projects(
    source_filter: str,
    *,
    discover_projects_fn: Callable[[], list[dict]],
    load_config_fn: Callable[[], DataClawConfig],
) -> None:
    projects = _filter_projects_by_source(discover_projects_fn(), source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.")
        return
    config = load_config_fn()
    excluded = set(config.get("excluded_projects", []))
    print(
        json.dumps(
            [
                {
                    "name": project["display_name"],
                    "sessions": project["session_count"],
                    "size": _format_size(project["total_size_bytes"]),
                    "excluded": project["display_name"] in excluded,
                    "source": project.get("source", DEFAULT_SOURCE),
                }
                for project in projects
            ],
            indent=2,
        )
    )


def configure(
    *,
    repo: str | None,
    source: str | None,
    exclude: list[str] | None,
    redact: list[str] | None,
    redact_usernames: list[str] | None,
    confirm_projects: bool,
    load_config_fn: Callable[[], DataClawConfig],
    save_config_fn: Callable[[DataClawConfig], None],
    config_file: Path = CONFIG_FILE,
) -> None:
    config = load_config_fn()
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
    if confirm_projects:
        config["projects_confirmed"] = True
    save_config_fn(config)
    print(f"Config saved to {config_file}")
    print(json.dumps(_mask_config_for_display(config), indent=2))


def status(*, load_config_fn: Callable[[], DataClawConfig]) -> None:
    config = load_config_fn()
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    next_steps, next_command = _build_status_next_steps(stage, config, hf_user, repo_id)
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
        "next_steps": next_steps,
        "next_command": next_command,
    }
    print(json.dumps(result, indent=2))


def prep(
    source_filter: str,
    *,
    load_config_fn: Callable[[], DataClawConfig],
    save_config_fn: Callable[[DataClawConfig], None],
    discover_projects_fn: Callable[[], list[dict]],
    has_session_sources_fn: Callable[[str], bool],
) -> None:
    config = load_config_fn()
    resolved_source_choice, source_explicit = _resolve_source_choice(source_filter, config)
    effective_source_filter = _normalize_source_filter(resolved_source_choice)

    if not has_session_sources_fn(effective_source_filter):
        provider = PROVIDERS.get(effective_source_filter)
        err = (
            provider.missing_source_message()
            if provider
            else "None of the supported provider session directories were found."
        )
        emit_blocked_error(err)

    projects = _filter_projects_by_source(discover_projects_fn(), effective_source_filter)
    if not projects:
        emit_blocked_error(f"No {_source_label(effective_source_filter)} sessions found.")

    excluded = set(config.get("excluded_projects", []))
    stage, stage_number, hf_user = _compute_stage(config)
    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    stage_config = cast(DataClawConfig, dict(config))
    if source_explicit:
        stage_config["source"] = resolved_source_choice
    next_steps, next_command = _build_status_next_steps(stage, stage_config, hf_user, repo_id)

    config["stage"] = stage
    save_config_fn(config)

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
                "name": project["display_name"],
                "sessions": project["session_count"],
                "size": _format_size(project["total_size_bytes"]),
                "excluded": project["display_name"] in excluded,
                "source": project.get("source", DEFAULT_SOURCE),
            }
            for project in projects
        ],
        "redact_strings": [_mask_secret(s) for s in config.get("redact_strings", [])],
        "redact_usernames": config.get("redact_usernames", []),
        "config_file": str(CONFIG_FILE),
        "next_steps": next_steps,
    }
    print(json.dumps(result, indent=2))


def handle_config(
    args: argparse.Namespace,
    *,
    load_config_fn: Callable[[], DataClawConfig],
    save_config_fn: Callable[[DataClawConfig], None],
    configure_fn: Callable[..., Any],
) -> None:
    has_changes = (
        args.repo or args.source or args.exclude or args.redact or args.redact_usernames or args.confirm_projects
    )
    if not has_changes:
        print(json.dumps(_mask_config_for_display(load_config_fn()), indent=2))
        return
    configure_fn(
        repo=args.repo,
        source=args.source,
        exclude=_parse_csv_arg(args.exclude),
        redact=_parse_csv_arg(args.redact),
        redact_usernames=_parse_csv_arg(args.redact_usernames),
        confirm_projects=args.confirm_projects or bool(args.exclude),
    )


def run_export(
    args: argparse.Namespace,
    *,
    load_config_fn: Callable[[], DataClawConfig],
    save_config_fn: Callable[[DataClawConfig], None],
    discover_projects_fn: Callable[[], list[dict]],
    has_session_sources_fn: Callable[[str], bool],
    export_to_jsonl_fn: Callable[..., dict],
    summarize_jsonl_fn: Callable[[Path], dict],
    push_to_huggingface_fn: Callable[..., None],
) -> None:
    config = load_config_fn()
    redaction = {
        "redact_strings": config.get("redact_strings", []) or [],
        "redact_usernames": config.get("redact_usernames", []) or [],
    }
    source_choice, source_explicit = _resolve_source_choice(args.source, config)
    source_filter = _normalize_source_filter(source_choice)

    confirmed_file: Path | None = None

    if not args.no_push:
        publish_attestation_next_command = (
            "dataclaw export --publish-attestation "
            '"User explicitly approved publishing to Hugging Face on YYYY-MM-DD."'
        )
        if args.attest_user_approved_publish and not args.publish_attestation:
            emit_blocked_error(
                "Deprecated publish attestation flag was provided.",
                hint="Use --publish-attestation with a detailed text statement.",
                blocked_on_step="Step 6/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command=publish_attestation_next_command,
            )
        if config.get("stage") != "confirmed":
            emit_blocked_error(
                "You must run `dataclaw confirm` before pushing.",
                hint="Export first with --no-push, review the data, then run `dataclaw confirm`.",
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw confirm",
            )

        publish_attestation, publish_error = _validate_publish_attestation(args.publish_attestation)
        if publish_error:
            emit_blocked_error(
                "Missing or invalid publish attestation.",
                hint="Ask the user to explicitly approve publishing, then pass a detailed text attestation.",
                blocked_on_step="Step 6/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command=publish_attestation_next_command,
                publish_attestation_error=publish_error,
            )

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
            emit_blocked_error(
                "Missing or invalid review attestations from confirm step.",
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command=CONFIRM_COMMAND_EXAMPLE,
                attestation_errors=review_errors,
            )

        config["publish_attestation"] = publish_attestation
        save_config_fn(config)

        last_confirm = config.get("last_confirm", {})
        confirmed_file_raw = last_confirm.get("file")
        if not isinstance(confirmed_file_raw, str) or not confirmed_file_raw:
            emit_blocked_error(
                "No confirmed export file is recorded.",
                hint="Run `dataclaw confirm --file path/to/export.jsonl` on the reviewed local export, then push again.",
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw confirm",
            )

        confirmed_file = Path(confirmed_file_raw)
        if not confirmed_file.exists():
            emit_blocked_error(
                f"Confirmed export file does not exist: {confirmed_file}",
                hint="Re-export locally with `dataclaw export --no-push`, review it, rerun `dataclaw confirm`, then push again.",
                blocked_on_step="Step 4/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw export --no-push --output dataclaw_export.jsonl",
            )

        recorded_sha256 = last_confirm.get("sha256")
        recorded_size = last_confirm.get("size_bytes")
        if not recorded_sha256 or not isinstance(recorded_size, int):
            emit_blocked_error(
                "Confirmed export file has no recorded fingerprint.",
                hint=(
                    "Your config predates the content-integrity check. Re-run "
                    "`dataclaw confirm --file <path> ...` to record a fingerprint, then push again."
                ),
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw confirm",
            )

        current_size = confirmed_file.stat().st_size
        if current_size != recorded_size:
            emit_blocked_error(
                "Confirmed export file size has changed since `dataclaw confirm`.",
                hint=(
                    "The file at the recorded path is not the file you confirmed. Re-run "
                    "`dataclaw confirm --file <path> ...` to re-review the current contents."
                ),
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw confirm",
                expected_size_bytes=recorded_size,
                actual_size_bytes=current_size,
            )

        current_sha256 = sha256_file(confirmed_file)
        if current_sha256 != recorded_sha256:
            emit_blocked_error(
                "Confirmed export file contents have changed since `dataclaw confirm`.",
                hint=(
                    "The bytes at the recorded path no longer match what was reviewed. Re-run "
                    "`dataclaw confirm --file <path> ...` to re-review the current contents before publishing."
                ),
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command="dataclaw confirm",
                expected_sha256=recorded_sha256,
                actual_sha256=current_sha256,
            )

    if confirmed_file is None and not source_explicit:
        emit_blocked_error(
            "Source scope is not confirmed yet.",
            hint=f"Explicitly choose one source scope before exporting: {_source_scope_literals()}.",
            blocked_on_step="Step 3A/6",
            process_steps=_setup_to_publish_steps(),
            next_command="dataclaw config --source all",
            required_action=(
                f"Ask the user whether to export {_all_provider_labels()} or all. "
                f"Then run `dataclaw config --source {_source_scope_placeholder()}` "
                f"or pass `--source {_source_scope_placeholder()}` on the export command."
            ),
            allowed_sources=sorted(EXPLICIT_SOURCE_CHOICES),
        )

    print("=" * 50)
    print("  DataClaw: Coding Agent Logs -> Hugging Face")
    print("=" * 50)
    export_start_time = time.perf_counter()

    repo_id = args.repo or config.get("repo")
    if not repo_id and not args.no_push:
        hf_user = get_hf_username()
        if hf_user:
            repo_id = default_repo_name(hf_user)
            print(f"\nAuto-detected HF repo: {repo_id}")
            config["repo"] = repo_id
            save_config_fn(config)

    if confirmed_file is not None:
        file_size = confirmed_file.stat().st_size
        print(f"\nReusing confirmed export file: {confirmed_file}")
        meta = summarize_jsonl_fn(confirmed_file)
        print(f"Publishing {meta['sessions']} confirmed sessions ({_format_size(file_size)})")

        if not repo_id:
            print("\nNo HF repo. Log in first: hf auth login --token YOUR_TOKEN")
            print("Then re-run dataclaw and it will auto-detect your username.")
            print(f"Or set manually: dataclaw config --repo {default_repo_name('username')}")
            print(f"\nLocal file: {confirmed_file}")
            _print_export_elapsed(export_start_time)
            return

        push_to_huggingface_fn(confirmed_file, repo_id, meta, redaction)

        # push_to_huggingface updates meta["sessions"] to the merged remote+local total;
        # keep last_export.sessions consistent with what was actually published (H5).
        last_export = config.get("last_export")
        if isinstance(last_export, dict):
            last_export["sessions"] = meta.get("sessions", last_export.get("sessions"))

        config["stage"] = "done"
        save_config_fn(config)

        _print_export_elapsed(export_start_time)
        print("\n---DATACLAW_JSON---")
        print(
            json.dumps(
                {
                    "stage": "done",
                    "stage_number": 4,
                    "total_stages": 4,
                    "dataset_url": hf_dataset_url(repo_id),
                    "next_steps": [
                        "Done! Dataset is live. To update later, repeat Steps 3 through 6: dataclaw prep, reconfigure as needed, export locally, confirm, then publish.",
                    ],
                    "next_command": None,
                },
                indent=2,
            )
        )
        return

    if not has_session_sources_fn(source_filter):
        provider = PROVIDERS.get(source_filter)
        msg = (
            provider.missing_source_message()
            if provider
            else "none of the supported provider session directories were found."
        )
        emit_blocked_error(msg)

    projects = _filter_projects_by_source(discover_projects_fn(), source_filter)
    if not projects:
        emit_blocked_error(f"No {_source_label(source_filter)} sessions found.")

    if not args.all_projects and not config.get("projects_confirmed", False):
        excluded = set(config.get("excluded_projects", []))
        list_command = f"dataclaw list --source {source_choice}"
        emit_blocked_error(
            "Project selection is not confirmed yet.",
            hint=(
                f"Run `{list_command}`, present the full project list to the user, discuss which projects to exclude, then run "
                '`dataclaw config --exclude "p1,p2"` or `dataclaw config --confirm-projects`.'
            ),
            blocked_on_step="Step 3B/6",
            process_steps=_setup_to_publish_steps(),
            next_command="dataclaw config --confirm-projects",
            required_action=(
                "Send the full project/folder list below to the user in a message and get explicit "
                "confirmation on exclusions before exporting."
            ),
            projects=[
                {
                    "name": project["display_name"],
                    "source": project.get("source", DEFAULT_SOURCE),
                    "sessions": project["session_count"],
                    "size": _format_size(project["total_size_bytes"]),
                    "excluded": project["display_name"] in excluded,
                }
                for project in projects
            ],
        )

    total_sessions = sum(project["session_count"] for project in projects)
    total_size = sum(project["total_size_bytes"] for project in projects)
    print(f"\nFound {total_sessions} sessions across {len(projects)} projects ({_format_size(total_size)} raw)")
    print(f"Source scope: {source_choice}")

    excluded = set(config.get("excluded_projects", []))
    if args.all_projects:
        excluded = set()

    included = [project for project in projects if project["display_name"] not in excluded]
    excluded_projects = [project for project in projects if project["display_name"] in excluded]

    if excluded_projects:
        print(f"\nIncluding {len(included)} projects (excluding {len(excluded_projects)}):")
    else:
        print(f"\nIncluding all {len(included)} projects:")
    for project in included:
        print(f"  + {project['display_name']} ({project['session_count']} sessions)")
    for project in excluded_projects:
        print(f"  - {project['display_name']} (excluded)")

    if not included:
        emit_blocked_error("No projects to export. Run: dataclaw config --exclude ''")

    extra_usernames = config.get("redact_usernames", [])
    anonymizer = Anonymizer(extra_usernames=extra_usernames)
    custom_strings = config.get("redact_strings", [])

    if extra_usernames:
        print(f"\nAnonymizing usernames: {', '.join(extra_usernames)}")
    if custom_strings:
        print(f"Redacting custom strings: {len(custom_strings)} configured")

    output_path = args.output or Path("dataclaw_conversations.jsonl")

    print(f"\nExporting to {output_path}...")
    meta = export_to_jsonl_fn(
        included,
        output_path,
        anonymizer,
        not args.no_thinking,
        custom_strings=custom_strings,
    )
    file_size = output_path.stat().st_size
    print(f"\nExported {meta['sessions']} sessions ({_format_size(file_size)})")
    if meta.get("skipped"):
        print(f"Skipped {meta['skipped']} abandoned/error sessions")
    if meta.get("redactions"):
        print(f"Redacted {meta['redactions']} secrets (API keys, tokens, emails, etc.)")
    model_breakdown = meta.get("model_breakdown", {})
    if model_breakdown:
        print(
            "Models: "
            + ", ".join(
                f"{model} ({stats['sessions']})"
                for model, stats in sorted(
                    model_breakdown.items(),
                    key=lambda item: (-item[1].get("sessions", 0), item[0]),
                )
            )
        )

    _print_pii_guidance(output_path, REPO_URL)

    redact_strings_list = config.get("redact_strings", []) or []
    redact_usernames_list = config.get("redact_usernames", []) or []
    config["last_export"] = {
        "timestamp": meta["exported_at"],
        "sessions": meta["sessions"],
        "source": source_choice,
        "redact_strings_fingerprint": fingerprint_strings(redact_strings_list),
        "redact_strings_count": len(redact_strings_list),
        "redact_usernames_fingerprint": fingerprint_strings(redact_usernames_list),
        "redact_usernames_count": len(redact_usernames_list),
    }
    if args.no_push:
        config["stage"] = "review"
    save_config_fn(config)

    if args.no_push:
        abs_path = str(output_path.resolve())
        next_steps, next_command = _build_status_next_steps("review", config, None, None)
        print(f"\nDone! JSONL file: {output_path}")
        _print_export_elapsed(export_start_time)
        print("\n---DATACLAW_JSON---")
        print(
            json.dumps(
                {
                    "stage": "review",
                    "stage_number": 3,
                    "total_stages": 4,
                    "sessions": meta["sessions"],
                    "source": source_choice,
                    "output_file": abs_path,
                    "pii_commands": _build_pii_commands(output_path),
                    "next_steps": next_steps,
                    "next_command": next_command,
                },
                indent=2,
            )
        )
        return

    if not repo_id:
        print("\nNo HF repo. Log in first: hf auth login --token YOUR_TOKEN")
        print("Then re-run dataclaw and it will auto-detect your username.")
        print(f"Or set manually: dataclaw config --repo {default_repo_name('username')}")
        print(f"\nLocal file: {output_path}")
        _print_export_elapsed(export_start_time)
        return

    push_to_huggingface_fn(output_path, repo_id, meta, redaction)

    last_export = config.get("last_export")
    if isinstance(last_export, dict):
        last_export["sessions"] = meta.get("sessions", last_export.get("sessions"))

    config["stage"] = "done"
    save_config_fn(config)

    _print_export_elapsed(export_start_time)
    print("\n---DATACLAW_JSON---")
    print(
        json.dumps(
            {
                "stage": "done",
                "stage_number": 4,
                "total_stages": 4,
                "dataset_url": hf_dataset_url(repo_id),
                "next_steps": [
                    "Done! Dataset is live. To update later, repeat Steps 3 through 6: dataclaw prep, reconfigure as needed, export locally, confirm, then publish.",
                ],
                "next_command": None,
            },
            indent=2,
        )
    )


def run_jsonl_to_yaml(
    args: argparse.Namespace,
    *,
    jsonl_to_yaml_fn: Callable[[Path, Path | None], Path],
) -> None:
    input_path = args.input or Path("dataclaw_conversations.jsonl")
    try:
        output_path = jsonl_to_yaml_fn(input_path, args.output)
    except FileNotFoundError as exc:
        emit_blocked_error(str(exc))
    print(f"Written to {output_path}")


def run_diff_jsonl(args: argparse.Namespace, *, diff_jsonl_fn: Callable[..., Any]) -> None:
    try:
        result = diff_jsonl_fn(
            args.old,
            args.new,
            args.output,
            include_records_for_modified=args.include_records_for_modified,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        emit_blocked_error(str(exc))
    print(f"Wrote {result.event_count} change documents to {result.output_path}")


def main_impl(
    *,
    prep_fn: Callable[..., Any],
    status_fn: Callable[[], None],
    confirm_fn: Callable[..., Any],
    update_skill_fn: Callable[[str], None],
    list_projects_fn: Callable[..., Any],
    load_config_fn: Callable[[], DataClawConfig],
    handle_config_fn: Callable[[argparse.Namespace], None],
    run_export_fn: Callable[[argparse.Namespace], None],
    run_jsonl_to_yaml_fn: Callable[[argparse.Namespace], None],
    run_diff_jsonl_fn: Callable[[argparse.Namespace], None],
) -> None:
    parser = argparse.ArgumentParser(description="DataClaw: Coding Agent Logs -> Hugging Face")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current stage and next steps")

    us = sub.add_parser("update-skill", help="Install/update the dataclaw skill for a coding agent")
    us.add_argument("target", choices=["claude"], help="Agent to install skill for")

    prep_parser = sub.add_parser("prep", help="Data prep - discover projects, detect HF, output JSON")
    prep_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")

    cfg = sub.add_parser("config", help="View or set config")
    cfg.add_argument("--repo", type=str, help="Set HF repo")
    cfg.add_argument(
        "--source",
        choices=sorted(EXPLICIT_SOURCE_CHOICES),
        help=f"Set export source scope explicitly: {_source_scope_literals()}",
    )
    cfg.add_argument("--exclude", type=str, help="Comma-separated projects to exclude")
    cfg.add_argument(
        "--redact", type=str, help="Comma-separated strings to always redact (API keys, usernames, domains)"
    )
    cfg.add_argument(
        "--redact-usernames", type=str, help="Comma-separated usernames to anonymize (GitHub handles, Discord names)"
    )
    cfg.add_argument(
        "--confirm-projects", action="store_true", help="Mark project selection as confirmed (include all)"
    )

    list_parser = sub.add_parser("list", help="List all projects")
    list_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")

    exp = sub.add_parser("export", help="Export locally or publish to Hugging Face")
    exp.add_argument("--output", "-o", type=Path, default=None)
    exp.add_argument("--repo", "-r", type=str, default=None)
    exp.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
    exp.add_argument("--all-projects", action="store_true")
    exp.add_argument("--no-thinking", action="store_true")
    exp.add_argument("--no-push", action="store_true")
    exp.add_argument(
        "--publish-attestation",
        type=str,
        default=None,
        help="Required for push: text attestation that user explicitly approved publishing.",
    )
    exp.add_argument("--attest-user-approved-publish", action="store_true", help=argparse.SUPPRESS)

    cf = sub.add_parser("confirm", help="Scan for PII, summarize export, and unlock pushing")
    cf.add_argument("--file", "-f", type=Path, default=None, help="Path to export JSONL file")
    cf.add_argument(
        "--full-name",
        type=str,
        default=None,
        help="User's full name to scan for in the export file (exact-name privacy check).",
    )
    cf.add_argument(
        "--skip-full-name-scan",
        action="store_true",
        help="Skip exact full-name scan when the user declines sharing their name.",
    )
    cf.add_argument(
        "--attest-full-name", type=str, default=None, help="Text attestation describing how full-name scan was done."
    )
    cf.add_argument(
        "--attest-sensitive",
        type=str,
        default=None,
        help="Text attestation describing sensitive-entity review and outcome.",
    )
    cf.add_argument(
        "--attest-manual-scan",
        type=str,
        nargs="?",
        const="__DEPRECATED_FLAG__",
        default=None,
        help=f"Text attestation describing manual scan ({MIN_MANUAL_SCAN_SESSIONS}+ sessions).",
    )
    cf.add_argument(
        "--accept-full-name-matches",
        type=str,
        default=None,
        help="Text attestation explicitly accepting that full-name scan matches will be published.",
    )
    cf.add_argument(
        "--accept-session-shrink",
        type=str,
        default=None,
        help="Text attestation explicitly accepting that this export has fewer sessions than the previous one.",
    )
    cf.add_argument(
        "--accept-redaction-drift",
        type=str,
        default=None,
        help="Text attestation explicitly accepting that the redaction list shrank since the previous export.",
    )
    cf.add_argument("--attest-asked-full-name", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-sensitive", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-manual-scan", action="store_true", help=argparse.SUPPRESS)

    jsonl_yaml = sub.add_parser("jsonl-to-yaml", help="Convert a JSONL export to human-readable YAML")
    jsonl_yaml.add_argument("input", nargs="?", type=Path, default=Path("dataclaw_conversations.jsonl"))
    jsonl_yaml.add_argument("--output", "-o", type=Path, default=None)

    diff_jsonl = sub.add_parser("diff-jsonl", help="Structurally diff two JSONL exports and render YAML")
    diff_jsonl.add_argument("--old", type=Path, default=Path("dataclaw_conversations_old.jsonl"))
    diff_jsonl.add_argument("--new", type=Path, default=Path("dataclaw_conversations.jsonl"))
    diff_jsonl.add_argument("--output", "-o", type=Path, default=None)
    diff_jsonl.add_argument("--include-records-for-modified", action="store_true")
    args = parser.parse_args()
    command = args.command

    if command is None:
        parser.print_help()
        return

    if command == "prep":
        prep_fn(source_filter=args.source)
        return
    if command == "status":
        status_fn()
        return
    if command == "confirm":
        if (
            args.attest_asked_full_name
            or args.attest_asked_sensitive
            or args.attest_asked_manual_scan
            or args.attest_manual_scan == "__DEPRECATED_FLAG__"
        ):
            emit_blocked_error(
                "Deprecated boolean attestation flags were provided.",
                hint="Use text attestations instead so the command can validate what was reviewed.",
                blocked_on_step="Step 5/6",
                process_steps=EXPORT_REVIEW_PUBLISH_STEPS,
                next_command=CONFIRM_COMMAND_EXAMPLE,
            )
        confirm_fn(
            file_path=args.file,
            full_name=args.full_name,
            attest_asked_full_name=args.attest_full_name,
            attest_asked_sensitive=args.attest_sensitive,
            attest_manual_scan=args.attest_manual_scan,
            skip_full_name_scan=args.skip_full_name_scan,
            accept_full_name_matches=args.accept_full_name_matches,
            accept_session_shrink=args.accept_session_shrink,
            accept_redaction_drift=args.accept_redaction_drift,
        )
        return
    if command == "update-skill":
        update_skill_fn(args.target)
        return
    if command == "list":
        config = load_config_fn()
        resolved_source_choice, _ = _resolve_source_choice(args.source, config)
        list_projects_fn(source_filter=resolved_source_choice)
        return
    if command == "config":
        handle_config_fn(args)
        return
    if command == "jsonl-to-yaml":
        run_jsonl_to_yaml_fn(args)
        return
    if command == "diff-jsonl":
        run_diff_jsonl_fn(args)
        return
    run_export_fn(args)
