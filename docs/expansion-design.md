# Implementation Plan: DataClaw — Auto runs, privacy-filter PII, sharded HF uploads, folder rules, Tauri menubar app (v5)

## Overview

DataClaw today is a Python CLI that discovers coding-agent sessions (`claude`/`codex`/`gemini`/`opencode`/`openclaw`/`kimi`/`custom`), anonymizes paths & usernames (`dataclaw/anonymizer.py`), runs regex + entropy redaction (`dataclaw/secrets.py`), writes a single flat `dataclaw_conversations.jsonl`, and pushes to one HF dataset repo (`dataclaw/cli.py:409–456`).

This plan delivers: (1) `dataclaw auto` replacing the human confirm gate; (2) `openai/privacy-filter` PII augmentation; (3) sharded HF uploads with per-session-id dedup + HF-hub merge; (4) folder rules / buckets; (5) a **fully self-contained Tauri 2 menubar app** signed + notarized + auto-updating, with macOS Keychain HF auth, structured logging, push retry + persistent failed-run staging, and one-click rollback.

## User decisions (open — defaults chosen so work proceeds; both overridable without architectural rework)

**1. Auto-mode default policy** — implementation defaults to `strict`; user flips to `permissive` via `dataclaw enable-auto --policy permissive`. Recommendation: **strict** (publishing is irreversible).

**2. Folder-rules specification style** — defaults to explicit `folder_rules.buckets` with optional tag fallback. User flips to tag-only by leaving `buckets` empty and populating `buckets_by_tag`. Recommendation: **explicit-first**.

## Hard constraints (back-compat + correctness invariants)

- **Legacy function signatures preserved exactly** (`export_to_jsonl`, `push_to_huggingface`).
- **Sharded exports never overwrite prior sessions**: per-session-id dedup + HF-hub merge handling `RepositoryNotFoundError`, `EntryNotFoundError`, `LocalEntryNotFoundError`, and 404-status `HfHubHTTPError` as "first-time push, continue"; other network errors abort.
- **Uploads are manifest-scoped** via exact `allow_patterns`.
- **Staging writes go to `~/.dataclaw/staging/{run_id}/` from the start** (no `export/runs/` intermediate). On push success → move to `~/.dataclaw/staging/published/{run_id}/`. On failure (caught OR crash) → left in place; `dataclaw auto --retry-only` resumes the most recent failed dir.
- **`last_export_cutoff` advances only on push success.**
- **Privacy-filter is opt-in**; base install never imports `torch`/`transformers`.
- **`confirm` accepts a positional `path` argument** (dir or file); `--file/-f` preserved as alias.
- **Cooled-session rule = source-file mtime ≥ 24h** for ALL sources (file-based AND opencode SQLite — uses the DB file mtime, NOT session `end_time`).
- **.dmg fully self-contained**: PyInstaller `universal2` sidecar bundled, no `pip install` required. PyInstaller spec includes `keyring` + platform backends as hidden-imports.
- **HF token in macOS Keychain** (Tauri Rust `keyring` crate + Python `keyring`). `dataclaw hf login` writes to BOTH Keychain AND mirrors to `~/.cache/huggingface/token` (huggingface_hub standard path).
- **Structured JSON logs** at `~/.dataclaw/logs/auto-YYYY-MM-DD.jsonl` (today's file uses today's date in the filename, NOT a date suffix on yesterday's rotated file).
- **Tauri v2 sidecar API**: invoke via `tauri_plugin_shell::ShellExt`; capabilities defined in `app/src-tauri/capabilities/default.json` (NOT v1 `allowlist`).
- **Signing env var**: `APPLE_SIGNING_IDENTITY` (Tauri v2 documented), NOT custom `DATACLAW_SIGNING_IDENTITY`.
- **`chmod 0600`** on config.json, plist, systemd units, manifests.
- **Signed + notarized + stapled .dmg** + `tauri-plugin-updater` with signed `latest.json`.
- **Rollback**: `dataclaw rollback --commit <sha>` + Tauri revert button.

## Settled decisions (locked)

SD-001..SD-024 from gate v4. Auto = strict default; explicit buckets primary. Per-run staging at `~/.dataclaw/staging/{run_id}/` direct (SD-004 revised). HF-hub merge with full exception taxonomy (SD-005). Manifest-scoped upload (SD-006). Directory-aware confirm with positional path (SD-007). Pipeline-simple decoder + contract test (SD-008). `device`/`min_score` keyword-only threading (SD-009). Scheduler env allowlist excludes HF_TOKEN; `chmod 0600` (SD-010). Exit codes 0/2/3/4 (SD-011). Tauri thin GUI (SD-012). Cooled = file mtime universally (SD-013 revised — covers opencode SQLite DB file). Base install no torch/transformers (SD-014). PyInstaller universal2 + Tauri v2 externalBin + sidecar-first; PyInstaller spec includes keyring backends (SD-016 revised). HF token in Keychain; `hf login` mirrors to standard path (SD-017 revised). Daily-rotated logs `auto-YYYY-MM-DD.jsonl` via custom `namer` (SD-018 revised). Direct-to-staging writes (SD-019 revised). `APPLE_SIGNING_IDENTITY` env var (SD-020 revised). `tauri-plugin-updater` (SD-021). Rollback CLI + UI (SD-022). Tauri v2 `tauri-plugin-shell` sidecar API (SD-023). `app/src-tauri/capabilities/default.json` (SD-024).

---

## Phase 1: Foundation — schema, optional deps, cooled filter, cutoff semantics

### Step 1: Extend `DataClawConfig` schema (`dataclaw/config.py`)
**Scope:** Small
1. **Add** to TypedDict at `dataclaw/config.py:12`: `last_export_cutoff`, `folder_rules`, `project_tags`, `auto`, `known_findings`, `last_auto_run` (with `push_attempts`, `backoff_seconds_total`, `staging_dir`), `privacy_filter`, `updater`. All optional.
2. **Keep** `load_config` merge behavior. `save_config` runs `os.chmod(CONFIG_FILE, 0o600)` after every write.
3. **Add** `MIGRATION_VERSION = 1`.

### Step 2: Optional deps + base-install import isolation (`pyproject.toml`, `tests/test_import_isolation.py`)
**Scope:** Small
1. **Extend** `[project.optional-dependencies]`:
   ```toml
   dev      = ["pytest", "pytest-mock", "freezegun"]
   pii      = ["transformers>=4.57", "torch>=2.3", "accelerate>=0.30", "tokenizers>=0.20"]
   keyring  = ["keyring>=25", "secretstorage>=3.3; sys_platform == 'linux'"]
   build    = ["pyinstaller>=6.10", "keyring>=25", "secretstorage>=3.3; sys_platform == 'linux'"]
   ```
2. **`tests/test_import_isolation.py`** asserts `import dataclaw.cli` does not pull `torch`/`transformers` via subprocess.
3. **README** install section.

### Step 3: ISO-timestamp helper + cooled filter — file-mtime universally (`dataclaw/parser.py`)
**Scope:** Medium
1. **Add** at `parser.py:23`: `_parse_iso(ts)`, `_is_cooled_path(p)`, `COOL_DOWN_SECONDS=86400`. Drop `_is_cooled_ts` from the public path — see (3).
2. **Thread** `cooled_only`, `since` keyword-only through `parse_project_sessions`.
3. **`_apply_session_filters`** runs post-parse and uses `_is_cooled_path(session["_source_file"])` for **every** source. Stamping rules:
   - **Claude / Codex / Gemini / OpenClaw / Kimi / Custom** (file-based): `_source_file` = the JSONL/JSON file backing this session.
   - **Claude subagent merges**: `_source_file` = the newest contributing JSONL (`max(mtimes)`).
   - **OpenCode (SQLite)**: `_source_file` = the SQLite DB file path (`~/.local/share/opencode/storage.db` or wherever the DB lives). The DB file mtime advances on any session activity, so this is conservatively-correct: it over-cools (delays export) when a different session in the same DB is active; never under-cools. **This matches the user spec exactly: "source file mtime > 24h old".**
   - For all sources, drop sessions where `_parse_iso(end_time)` is None (debug line per project).
4. **Cutoff** uses strict-`>` aware-datetime compare on `end_time`.

### Step 4: Cutoff-aware export paths (`dataclaw/cli.py:346–406`)
**Scope:** Small
1. **Do NOT change `export_to_jsonl` signature.** New behavior in `export_to_shards` (Step 6).

### Step 5: Tests — foundation (`tests/test_config.py`, `tests/test_parser.py`)
**Scope:** Small
1. `test_config_roundtrip_with_new_fields`, **`test_config_file_chmod_600`**.
2. `test_parse_iso_handles_z_suffix_offset_and_bad`.
3. `test_cooled_only_drops_fresh_claude_session`, `test_cooled_only_keeps_25h_claude_session`, `test_cooled_only_claude_subagent_merged`.
4. **`test_cooled_only_opencode_uses_db_file_mtime`** — touch DB file 23h ago → session dropped; touch DB file 25h ago → session included. Asserts the cooled gate uses the SQLite file's mtime, not the session's `end_time`.
5. `test_since_cutoff_strictly_greater`, `test_session_with_none_end_time_is_skipped`.

---

## Phase 2: Sharded export — direct-to-staging + dedup + manifest-scoped upload

### Step 6: Path resolver + `export_to_shards` writes directly to staging (`dataclaw/cli.py`)
**Scope:** Medium
1. **Add** `_resolve_shard_path(root, session, config)` and `_bucket_for_project(session, config)` (Step 8).
2. **Add** `export_to_shards(selected_projects, run_dir, anonymizer, config, *, include_thinking=True, custom_strings=None, cooled_only=False, since=None, fetch_existing=True) -> dict`:
   - **`run_dir` is the staging dir directly** — caller passes `~/.dataclaw/staging/{run_id}/` (Step 22). No `export/runs/` intermediate.
   - Pre-flight: compute target shard paths.
   - If `fetch_existing and config.get("repo")`: for each target path, `hf_hub_download(repo_id, path_in_repo, local_dir=run_dir, local_dir_use_symlinks=False)` with full exception taxonomy:
     ```python
     from huggingface_hub.utils import (RepositoryNotFoundError, EntryNotFoundError,
                                         LocalEntryNotFoundError, HfHubHTTPError)
     try:
         hf_hub_download(...)
     except (RepositoryNotFoundError, EntryNotFoundError, LocalEntryNotFoundError):
         merge_source[path] = "new"
     except HfHubHTTPError as e:
         if e.response is not None and e.response.status_code == 404:
             merge_source[path] = "new"
         else:
             raise  # abort run on genuine network error
     ```
   - Load existing `session_id`s into `seen_ids`; open shard in append mode.
   - Second pass: skip duplicates; write new sessions; strip `_source_file`/`_project_dir_name`.
   - Write manifest atomically (Step 7).
3. **Do not touch** `export_to_jsonl`.

### Step 7: Manifest file (`dataclaw/cli.py`)
**Scope:** Small
1. `MANIFEST_REL = Path(".dataclaw/manifest.json")`. Schema: `export_id`, `schema_version=1`, `root_dir`, `started_at`, `finished_at`, `shards[]` (`path`, `source`, `date`, `sessions_new`, `sessions_total`, `bytes`), `sources`, `buckets`, `total_sessions_new`, `total_sessions_in_shards`, `total_redactions`, `models`, `max_end_time_by_source`, `include_thinking`, `merge_source`.
2. Atomic write via `tmp.write_text → rename`. `os.chmod(manifest, 0o600)`.

### Step 8: Bucket resolver (`dataclaw/cli.py`)
**Scope:** Medium
1. `_project_candidates(session) -> [_project_dir_name, project, stripped_prefix]`.
2. `_bucket_for_project(session, config)` → explicit `buckets` → tag fallback → `default_bucket`.

### Step 9: Manifest-scoped HF push (`dataclaw/cli.py:409–456`)
**Scope:** Medium
1. **Leave** `push_to_huggingface(jsonl_path, repo_id, meta)` unchanged.
2. **Add** `push_shards_to_huggingface(run_dir, repo_id, manifest) -> str` with `allow_patterns = [s["path"] for s in manifest["shards"]]`, `ignore_patterns=[".dataclaw/*", "conversations.jsonl"]`. Retry wrapper in Phase 10.

### Step 10: README `configs:` block (`dataclaw/cli.py:459–576`)
**Scope:** Small
1. New `_build_dataset_card_v2(repo_id, manifest)`; original untouched. Per-source configs only when present.

### Step 11: Directory-aware `confirm` with positional path (`dataclaw/cli.py:1194`, `941–1108`)
**Scope:** Medium
1. Argparse: `cf.add_argument("path", nargs="?", default=None, type=Path, help="Sharded dir or JSONL file (default: auto-detect latest staging)")` + `cf.add_argument("--file", "-f", dest="legacy_file", type=Path, default=None, help="Deprecated alias for positional path")`.
2. Handler resolves `args.path or args.legacy_file`. Auto-detect = scan `~/.dataclaw/staging/{run_id}/.dataclaw/manifest.json` (newest mtime) when no hint.
3. `_find_export_target(hint) -> ("file"|"shards", path, manifest|None)`.
4. Shards mode: `_summarize_shards`, `_scan_pii_dir`, `_scan_for_text_in_dir`; output JSON gains `"mode":"shards"`, `"shard_count"`, `"total_sessions_new"`.
5. Update `_build_pii_commands`/`_print_pii_guidance` to accept dir or file.

### Step 12: Tests — sharded export + review (`tests/test_cli.py`)
**Scope:** Medium
1. `test_export_to_shards_writes_per_source_dates`, `test_export_to_jsonl_flat_path_unchanged_byte_for_byte`, `test_manifest_schema_v1`.
2. `test_same_day_run_merges_via_dedup`, `test_fetch_existing_404_continues`, `test_fetch_existing_entry_not_found_continues`, `test_fetch_existing_local_entry_not_found_continues`, `test_fetch_existing_network_error_raises`.
3. `test_push_shards_upload_folder_scoped_to_manifest_paths`, `test_push_to_huggingface_legacy_signature_unchanged`.
4. `test_confirm_positional_path_accepts_directory`, `test_confirm_file_flag_still_accepted_as_alias`, **`test_confirm_no_arg_finds_latest_staging_run`**.
5. `test_configs_block_renders_only_present_sources`.

---

## Phase 3: Folder rules + config CLI surface extensions

### Step 13: Bucket config verbs (`dataclaw/cli.py:1216–1226`)
**Scope:** Medium
1. `--assign/--unassign`, `--default-bucket/--clear-default-bucket`, `--tag-project/--untag-project`, `--bucket-by-tag/--clear-bucket-by-tag`.

### Step 14: List replace/remove + `--show-secrets` (`dataclaw/cli.py:319–343`, `1216`, `1306`)
**Scope:** Medium
1. `--set-redact`/`--remove-redact` (and pairs for usernames + excluded). `--show-secrets`. `_mask_config_for_display(config, unmask=False)`.

### Step 15: User-facing copy sync (`dataclaw/cli.py`)
**Scope:** Small
1. Audit every source enumeration → full `claude|codex|gemini|opencode|openclaw|kimi|custom|all` set.
2. `AUTO_MODE_STEPS` constant.
3. `list_projects` rows gain `bucket`/`tags`.

### Step 16: Tests
**Scope:** Small
1. Bucket resolution (explicit, display-name, by-tag, default, none).
2. `test_config_set_redact_replaces_list`, `test_config_remove_redact_removes_entries`, `test_config_show_secrets_unmasks`.
3. `test_status_next_steps_lists_all_sources`.

---

## Phase 4: Privacy-filter PII detection

### Step 17: `dataclaw/privacy_filter.py` — pipeline decoder (new module)
**Scope:** Medium
1. Lazy imports; `MODEL_ID="openai/privacy-filter"`; `_PIPE_CACHE`; `Finding` dataclass with `.fingerprint()`; `is_available()`; `_load(device)` using `transformers.pipeline("token-classification", aggregation_strategy="simple")`.
2. `scan_text`/`scan_session`/`scan_shards`/`scan_jsonl` — `device` and `min_score` keyword-only.
3. `_chunk_by_tokens` at 480.
4. Never mutates sessions.

### Step 18: Known-findings registry
**Scope:** Small
1. `diff_findings`, `record_findings`. O(1) fingerprint lookup.

### Step 19: Wire into `confirm` (`dataclaw/cli.py:1033`)
**Scope:** Medium
1. Thread `device`/`min_score` from `config["privacy_filter"]`.
2. `confirm --policy strict/permissive` (default strict); `--ack-privacy-findings` to proceed with non-empty `pf_new`.

### Step 20: Tests + contract test
**Scope:** Small
1. `test_fingerprint_deterministic`, `test_diff_findings_splits_correctly`, `test_record_findings_increments`.
2. `test_is_available_false_without_deps`, `test_scan_text_passes_device_and_min_score_through`.
3. `test_confirm_blocks_on_new_pf_findings_strict`, `test_confirm_ack_adds_to_known_findings`.
4. `test_pipeline_decoder_matches_model_card_examples` marked `@pytest.mark.pii`.

### Step 21: Phase-4 preflight
**Scope:** Small
1. Verify `transformers>=4.57` loads `OpenaiPrivacyFilterForTokenClassification` before merging Phase 4 PR.

---

## Phase 5: Auto mode + OS scheduling

### Step 22: `dataclaw enable-auto` + `auto` + direct staging (`dataclaw/cli.py`)
**Scope:** Medium

**`enable-auto`** (subparser + handler):
1. `--publish-attestation` required, `--policy {strict,permissive}` default strict, `--full-name`, `--skip-full-name-scan`, `--enable-privacy-filter`.
2. Handler: requires `stage in {"confirmed","done"}`; resolves binary via `shutil.which`; saves `config["auto"]`; emits `next_command`.

**`auto`** (subparser + handler — direct staging):
1. `auto --force --dry-run --policy-override {strict,permissive} --retry-only` (retry-only handled in Step 47 helper).
2. Handler:
   ```python
   config = load_config()
   if not (config.get("auto") or {}).get("enabled"):
       fail_json({"error":"Auto mode not enabled", "fix":"dataclaw enable-auto …"}, exit_code=2)

   staging_root = Path.home() / ".dataclaw" / "staging"
   staging_root.mkdir(parents=True, exist_ok=True)
   _cleanup_published(staging_root, keep=3)  # only published/ subdir is GC'd; failed dirs stay
   run_id = uuid.uuid4().hex
   run_dir = staging_root / run_id
   run_dir.mkdir()

   manifest = export_to_shards(projects, run_dir, anonymizer, config,
                               cooled_only=True, since=config.get("last_export_cutoff", {}),
                               fetch_existing=True)

   if manifest["total_sessions_new"] == 0:
       record_last_auto_run("noop", config, staging_dir=str(run_dir))
       save_config(config)
       # Optionally rmtree run_dir on noop, since nothing to retry
       shutil.rmtree(run_dir, ignore_errors=True)
       return

   # privacy-filter policy branch (unchanged from v4)
   ...

   if args.dry_run:
       record_last_auto_run("dry-run", config, sessions=manifest["total_sessions_new"],
                            staging_dir=str(run_dir)); save_config(config); return

   try:
       url, attempts, total_wait = _push_with_retry(run_dir, config["repo"], manifest, logger)
   except PushFailed as e:
       record_last_auto_run("error", config, sessions=manifest["total_sessions_new"],
                            staging_dir=str(run_dir),  # LEFT IN PLACE for --retry-only
                            push_attempts=e.attempts, backoff_seconds_total=e.backoff_seconds_total,
                            error=str(e.cause))
       save_config(config); sys.exit(4)

   # Success: advance cutoff, move staging dir to published/
   config["last_export_cutoff"] = {**config.get("last_export_cutoff", {}),
                                   **manifest["max_end_time_by_source"]}
   published = staging_root / "published" / run_id
   published.parent.mkdir(parents=True, exist_ok=True)
   shutil.move(str(run_dir), str(published))
   record_last_auto_run("pushed", config, sessions=manifest["total_sessions_new"],
                        repo_url=url, staging_dir=str(published),
                        push_attempts=attempts, backoff_seconds_total=total_wait)
   save_config(config)
   ```
3. **`_cleanup_published(root, keep=3)`** keeps the 3 most recent dirs under `root/published/`; failed top-level dirs are NEVER auto-cleaned (manual `dataclaw clean-staging`).
4. **Crash safety**: if the process dies between `export_to_shards` and `_push_with_retry`, the staging dir at `~/.dataclaw/staging/{run_id}/` is preserved with its manifest. Next `dataclaw auto --retry-only` finds it (most-recent staging dir without a `published/` move) and pushes it.

### Step 23: `dataclaw install-schedule` + env preservation + `chmod 0600` (`dataclaw/scheduler.py`)
**Scope:** Medium
1. **`_ENV_ALLOWLIST`** (no `HF_TOKEN`): `("PATH","HOME","HF_HOME","HUGGINGFACE_HUB_CACHE","LANG","LC_ALL","TMPDIR")`. CLI resolves token from Keychain (Phase 9).
2. **macOS plist**: `WorkingDirectory=$HOME`, `EnvironmentVariables=_capture_env()`, log paths, `Label=io.dataclaw.auto`. **`os.chmod(plist_path, 0o600)`**. `launchctl bootout` (ignore failures) + `launchctl bootstrap gui/$(id -u) plist`.
3. **Linux**: `.service` + `.timer` with `chmod 0600`; `systemctl --user daemon-reload && enable --now`.
4. `uninstall`, `schedule-status`, `install-schedule --time HH:MM` subparsers.

### Step 24: Notifications (`dataclaw/scheduler.py`)
**Scope:** Small
1. `notify(title, body)` via `osascript`/`notify-send`; swallow failures.

### Step 25: Tests — auto + scheduler
**Scope:** Medium
1. `test_auto_requires_enable_auto`, `test_auto_noop_when_no_new_sessions`.
2. **`test_auto_writes_directly_to_staging_root`** — assert no `~/.dataclaw/export/runs/` dir is created; the run dir is `~/.dataclaw/staging/{run_id}/`.
3. **`test_auto_crash_during_push_preserves_staging`** — monkey-patch `_push_with_retry` to raise SIGKILL-equivalent (`KeyboardInterrupt` via mid-call); assert run dir still exists at staging root after.
4. **`test_auto_success_moves_to_published`** — push succeeds; assert run dir is gone from staging root and now under `staging/published/{run_id}/`.
5. `test_auto_blocks_in_strict_mode_preserves_staging`, `test_auto_permissive_appends_redact_reredacts_and_pushes`, `test_auto_dry_run_skips_push`, `test_auto_force_pushes_despite_findings`.
6. `test_cleanup_published_keeps_last_3` (only `published/` subdir GC'd; top-level failed dirs preserved).
7. `test_blocked_run_then_next_run_uses_new_staging_dir`, `test_auto_threads_device_and_min_score_from_config`.
8. `test_install_schedule_macos_plist_contents_and_chmod_600` (HF_TOKEN NOT in `EnvironmentVariables`).
9. `test_install_schedule_linux_systemd_unit_contents_and_chmod_600`.
10. `test_capture_env_filters_to_allowlist_excludes_hf_token`.
11. `test_cutoff_only_advances_on_push_success`.

---

## Phase 6: Tauri 2 menubar app — base + capabilities

### Step 26: Scaffold Tauri 2 (`app/`)
**Scope:** Medium
1. `app/src-tauri/{tauri.conf.json, Cargo.toml, src/{main.rs, dataclaw.rs}, capabilities/default.json}`. React + TypeScript + Vite. Routes: `Dashboard`, `Config`, `Findings`, `Logs`, `Auth`, `Releases`.
2. **`Cargo.toml`** declares Tauri 2 plugins:
   ```toml
   [dependencies]
   tauri = { version = "2", features = ["macos-private-api"] }
   tauri-plugin-shell = "2"
   tauri-plugin-keyring = "2"   # OR: keyring = "3" + manual integration; verify at impl
   tauri-plugin-updater = "2"
   tauri-plugin-fs = "2"
   tauri-plugin-notification = "2"
   notify = "6"
   serde = { version = "1", features = ["derive"] }
   serde_json = "1"
   tokio = { version = "1", features = ["full"] }
   ```
   Implementation note: Tauri 2 doesn't ship a first-party keyring plugin yet — use the `keyring = "3"` Rust crate directly inside our own commands. Falls back to `Cargo.toml` `keyring = "3"` if `tauri-plugin-keyring` isn't available at impl time.

### Step 27: Tauri 2 sidecar invocation + capability file (`app/src-tauri/src/dataclaw.rs`, `capabilities/default.json`)
**Scope:** Medium
1. **Tauri 2 sidecar API** (NOT v1's `tauri::api::process::Command::new_sidecar`):
   ```rust
   use tauri_plugin_shell::ShellExt;

   #[tauri::command]
   async fn dataclaw_status(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
       let (_rx, child) = app.shell()
           .sidecar("dataclaw").map_err(|e| e.to_string())?
           .args(["status"])
           .spawn().map_err(|e| e.to_string())?;
       let output = child.wait_with_output().await.map_err(|e| e.to_string())?;
       parse_json_block(&output.stdout)
   }
   ```
2. **Resolution order in dev** (when sidecar isn't bundled): the `tauri_plugin_shell` plugin handles sidecar lookup automatically at the bundle path. For `pnpm tauri dev`, the `binaries/` dir must contain the matching architecture stub (Phase 7 produces it). Fallback for `cargo run` without a built sidecar: `$DATACLAW_BIN` → `which dataclaw` → error.
3. **`app/src-tauri/capabilities/default.json`** (NEW FILE — replaces v1 `tauri.conf.json` `allowlist`):
   ```json
   {
     "$schema": "https://schema.tauri.app/config/2",
     "identifier": "default",
     "description": "Default capabilities for DataClaw window",
     "windows": ["main"],
     "permissions": [
       "core:default",
       "shell:allow-execute",
       { "identifier": "shell:allow-spawn", "allow": [{ "name": "dataclaw", "sidecar": true }] },
       "fs:allow-read-text-file",
       "fs:allow-write-text-file",
       "notification:default",
       "updater:default",
       "core:event:allow-listen",
       "core:event:allow-emit"
     ]
   }
   ```
4. Commands: `dataclaw_status`, `dataclaw_config_get(show_secrets)`, `dataclaw_config_set_redact`/`remove_redact` + ditto for usernames/excluded, `dataclaw_config_assign/unassign`, `dataclaw_auto_now(force)`, `dataclaw_auto_retry_only`, `dataclaw_install_schedule/uninstall_schedule/schedule_status`, `hf_save_token/load_token/delete_token/whoami` (Phase 9), `dataclaw_rollback_list/rollback_commit` (Phase 12).
5. `parse_json_block(bytes)` extracts `---DATACLAW_JSON---`; falls back to full stdout.

### Step 28: Frontend base (`app/src/routes/*.tsx`)
**Scope:** Medium
1. Dashboard: last-run card from `RUN_SUMMARY.json`, Run Now, Open Findings, retry button (Phase 10) when `result=error`.
2. Config: form for repo, source, folder-rules buckets, redact lists (add+remove), redact-usernames, excluded projects, auto policy, schedule time, HF Account section (Phase 9).
3. Findings: `pf_new` rows with "Add to redact list".
4. Tray: `TrayIconBuilder` with `[Open, Run now, Check for updates, View last run, -, Quit]`.

### Step 29: Tests — Tauri base
**Scope:** Small
1. Rust unit tests for `parse_json_block`.
2. **`test_capabilities_file_grants_shell_sidecar_for_dataclaw`** — parse `capabilities/default.json`; assert `shell:allow-spawn` includes `name: "dataclaw", sidecar: true`.

---

## Phase 7: Self-contained .app — PyInstaller sidecar with keyring backends

### Step 30: PyInstaller spec + build script — keyring backends bundled (`pyinstaller.spec`, `Makefile`, `scripts/build-sidecar.sh`)
**Scope:** Medium
1. **`pyinstaller.spec`** with full hidden-imports for keyring:
   ```python
   # pyinstaller.spec
   from PyInstaller.utils.hooks import collect_submodules

   hiddenimports = (
       collect_submodules("keyring") +    # bundles keyring + all backends
       collect_submodules("huggingface_hub") + [
           "dataclaw.parser", "dataclaw.secrets", "dataclaw.anonymizer",
           "dataclaw.privacy_filter", "dataclaw.logging", "dataclaw.auth",
           "dataclaw.scheduler",
           "keyring.backends.macOS",          # macOS Keychain backend
           "keyring.backends.SecretService",  # Linux Secret Service
           "keyring.backends.fail",           # last-resort fallback
           "secretstorage", "jeepney",        # SecretService transitive deps
       ]
   )
   datas = collect_data_files("huggingface_hub")
   a = Analysis(["dataclaw/cli.py"], hiddenimports=hiddenimports, datas=datas, ...)
   pyz = PYZ(a.pure)
   exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="dataclaw",
             target_arch="universal2", onefile=True, console=True)
   ```
2. **`scripts/build-sidecar.sh`**:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   ARCH="${1:-$(uname -m)}"
   uv run pyinstaller pyinstaller.spec --clean --noconfirm
   case "$ARCH" in
     arm64|aarch64) TRIPLE="aarch64-apple-darwin" ;;
     x86_64)        TRIPLE="x86_64-apple-darwin" ;;
   esac
   mkdir -p app/src-tauri/binaries
   cp dist/dataclaw "app/src-tauri/binaries/dataclaw-${TRIPLE}"
   codesign --remove-signature "app/src-tauri/binaries/dataclaw-${TRIPLE}" || true
   # Smoke test: bundled binary can read from Keychain
   "app/src-tauri/binaries/dataclaw-${TRIPLE}" hf whoami --check-keyring-only \
     || echo "(expected: no token configured yet — keyring backend itself loaded OK)"
   ```
3. **`Makefile`** targets `build-sidecar`, `build-sidecar-arm64`, `build-sidecar-x86_64`. CI uses the per-arch targets per matrix host.
4. Add `build` extra (Step 2) to dev sync so `pyinstaller`+`keyring` are present at build time.

### Step 31: Tauri externalBin declaration (`app/src-tauri/tauri.conf.json`)
**Scope:** Small
1. ```json
   { "bundle": { "externalBin": ["binaries/dataclaw"] } }
   ```
   Tauri 2 resolves `binaries/dataclaw-<target-triple>` at bundle time and copies into `.app/Contents/MacOS/`. Pairs with the capability grant from Step 27.

### Step 32: CI matrix (`.github/workflows/ci.yml`)
**Scope:** Small
1. Matrix `[macos-14, macos-13]`. Steps: checkout → Python 3.12 → uv sync `dev,pii,build` → `make build-sidecar` → pnpm install → `pnpm tauri build` (signing identity unset for unsigned PR builds) → assert `.app` exists → assert sidecar binary exists at `.app/Contents/MacOS/dataclaw-<triple>`.

### Step 33: Privacy-filter download banner + README copy
**Scope:** Small
1. Dashboard banner shown when `config.privacy_filter.enabled && model_cached_at is None`.
2. README: "Download DataClaw.dmg → open → that's it. No pip install."

### Step 34: Tests — sidecar
**Scope:** Small
1. CI bash: `test -x app/src-tauri/target/release/bundle/macos/DataClaw.app/Contents/MacOS/dataclaw-aarch64-apple-darwin`.
2. **`test_sidecar_keyring_backend_loads`** — invoke the bundled binary with `dataclaw hf whoami` (no token configured); verify exit is non-zero with the install-hint error message (proves the binary loaded `keyring` at all; `ImportError` would surface differently).
3. Rust `test_resolution_prefers_sidecar`.

---

## Phase 8: Structured logging + RUN_SUMMARY + Logs UI — daily-named files

### Step 35: `dataclaw/logging.py` — daily-named JSON-lines logger (new module)
**Scope:** Medium
1. **Custom `namer` so today's file uses today's date in filename**:
   ```python
   import logging, json, sys, re
   from datetime import datetime, timezone
   from pathlib import Path
   from logging.handlers import TimedRotatingFileHandler

   LOG_DIR = Path.home() / ".dataclaw" / "logs"
   _HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9_-]{20,}")

   def _scrub_secrets(text: str) -> str:
       return _HF_TOKEN_RE.sub("[REDACTED]", text)

   class JsonFormatter(logging.Formatter):
       def format(self, record):
           msg = _scrub_secrets(record.getMessage())
           payload = {"ts": datetime.now(tz=timezone.utc).isoformat(),
                      "level": record.levelname, "logger": record.name, "msg": msg,
                      "run_id": getattr(record, "run_id", None),
                      "phase": getattr(record, "phase", None),
                      "session_id": getattr(record, "session_id", None),
                      "project": getattr(record, "project", None),
                      "source": getattr(record, "source", None),
                      "extra": getattr(record, "extra_data", None)}
           if record.exc_info: payload["exc"] = _scrub_secrets(self.formatException(record.exc_info))
           return json.dumps({k: v for k, v in payload.items() if v is not None})

   class DailyNamedHandler(TimedRotatingFileHandler):
       """Override naming so the *active* file always uses today's date.
       Default TimedRotatingFileHandler writes to baseFilename then renames it
       on rotation; that produces `auto.jsonl` (today) + `auto.jsonl.YYYY-MM-DD`
       (yesterday). User spec wants `auto-YYYY-MM-DD.jsonl` for today."""
       def __init__(self, log_dir: Path, **kw):
           today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
           self._log_dir = log_dir
           super().__init__(filename=str(log_dir / f"auto-{today}.jsonl"),
                            when="midnight", backupCount=30, utc=True, **kw)
       def doRollover(self):
           # Close old, open new file with tomorrow's date
           if self.stream:
               self.stream.close(); self.stream = None
           tomorrow = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
           self.baseFilename = str(self._log_dir / f"auto-{tomorrow}.jsonl")
           # Prune old files
           for f in sorted(self._log_dir.glob("auto-*.jsonl"))[:-30]:
               try: f.unlink()
               except OSError: pass
           if not self.delay:
               self.stream = self._open()

   def setup_logging(run_id: str, level: str = "INFO") -> logging.LoggerAdapter:
       LOG_DIR.mkdir(parents=True, exist_ok=True)
       handler = DailyNamedHandler(LOG_DIR)
       handler.setFormatter(JsonFormatter())
       stderr_handler = logging.StreamHandler(sys.stderr)
       stderr_handler.setLevel("WARNING"); stderr_handler.setFormatter(JsonFormatter())
       logger = logging.getLogger("dataclaw")
       logger.setLevel(level); logger.handlers = [handler, stderr_handler]
       return logging.LoggerAdapter(logger, {"run_id": run_id})
   ```
2. **Test that today's file is `auto-YYYY-MM-DD.jsonl`**: `freezegun` to set today; assert `(LOG_DIR / f"auto-{today_str}.jsonl").exists()`.

### Step 36: Wire logging into every phase
**Scope:** Medium
1. Emit `phase_start`/`phase_end` at boundaries: discover, parse, redact, privacy_filter, push, confirm, schedule.
2. Per-session/per-finding/per-upload events as in v4.

### Step 37: `RUN_SUMMARY.json` per auto run
**Scope:** Small
1. Written at end of `auto` handler under `{run_dir}/RUN_SUMMARY.json` with required keys + `chmod 0600`.

### Step 38: `dataclaw status --logs` verb
**Scope:** Small
1. `status --logs [--run <id>] [--lines N=200]` reads today's `auto-YYYY-MM-DD.jsonl`.

### Step 39: Tauri Logs tab (`app/src-tauri/src/logs.rs`, `app/src/routes/Logs.tsx`)
**Scope:** Medium
1. Rust: `notify` watcher on `~/.dataclaw/logs/`; emits `logs-line` events.
2. Frontend: virtualized list, color-coded, copy-to-clipboard, "Show in Finder".

### Step 40: Tests — logging
**Scope:** Small
1. **`test_logging_writes_to_dated_filename_today`** — assert `(LOG_DIR / f"auto-{today_iso}.jsonl").exists()` after a write.
2. **`test_logging_rotates_to_new_dated_filename_at_midnight`** — `freezegun` advance past midnight; force `doRollover`; assert tomorrow's file exists and old retained until backupCount.
3. `test_run_summary_written`, `test_token_scrubbed_from_logs` (covers `hf_…` pattern AND raw `HF_TOKEN=` env-var dumps), `test_status_logs_filters_by_run_id`.

---

## Phase 9: macOS Keychain HF token — `hf login` mirrors to standard path

### Step 41: `_resolve_hf_token()` helper (`dataclaw/auth.py` — new)
**Scope:** Medium
1. Module:
   ```python
   import os
   from pathlib import Path
   KEYRING_SERVICE = "io.dataclaw.app"
   KEYRING_ACCOUNT = "hf_token"
   HF_STANDARD_TOKEN = Path.home() / ".cache" / "huggingface" / "token"

   def _resolve_hf_token() -> str | None:
       """Order: env > Keychain > huggingface_hub default token file."""
       tok = os.environ.get("HF_TOKEN")
       if tok: return tok
       try:
           import keyring
           tok = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
           if tok: return tok
       except (ImportError, Exception):
           pass
       try:
           if HF_STANDARD_TOKEN.exists():
               return HF_STANDARD_TOKEN.read_text().strip() or None
       except OSError:
           return None
       return None

   def _store_hf_token(tok: str, *, mirror_to_hf_path: bool = True) -> None:
       """Stores in Keychain AND mirrors to ~/.cache/huggingface/token by default
       so non-DataClaw tools (huggingface-cli, raw huggingface_hub usage) keep working."""
       import keyring
       keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, tok)
       if mirror_to_hf_path:
           HF_STANDARD_TOKEN.parent.mkdir(parents=True, exist_ok=True)
           HF_STANDARD_TOKEN.write_text(tok)
           os.chmod(HF_STANDARD_TOKEN, 0o600)

   def _delete_hf_token(*, also_remove_hf_path: bool = True) -> None:
       try:
           import keyring; keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
       except Exception: pass
       if also_remove_hf_path:
           try: HF_STANDARD_TOKEN.unlink()
           except (OSError, FileNotFoundError): pass
   ```
2. **`push_*` functions** call `_resolve_hf_token()` and set `os.environ["HF_TOKEN"]` before `HfApi()`.
3. **Missing-token fail**: `auto` exits 2 with hint "Launch DataClaw.app and sign in, OR run `dataclaw hf login --token-stdin`".

### Step 42: `dataclaw hf {login,logout,whoami}` — mirrors to HF standard path (`dataclaw/cli.py`)
**Scope:** Small
1. Subparser `hf` with `login [--token-stdin] [--no-mirror]`, `logout [--no-mirror]`, `whoami [--check-keyring-only]`.
2. **`login`** reads token from stdin (or interactive `getpass`); calls `_store_hf_token(tok, mirror_to_hf_path=not args.no_mirror)` — by default mirrors to `~/.cache/huggingface/token`. Then `HfApi().whoami()` to verify.
3. **`logout`** calls `_delete_hf_token(also_remove_hf_path=not args.no_mirror)`.
4. **`whoami`** prints user info from `HfApi(token=_resolve_hf_token()).whoami()`. `--check-keyring-only` exits 0 if Keychain has a token (used as a smoke test for the bundled sidecar).

### Step 43: Tauri first-run modal + Keychain writes (`app/src-tauri/src/hf.rs`, `app/src/routes/Auth.tsx`)
**Scope:** Medium
1. **Rust** `hf.rs` uses the `keyring = "3"` Rust crate:
   ```rust
   use keyring::Entry;
   const SERVICE: &str = "io.dataclaw.app";
   const ACCOUNT: &str = "hf_token";

   #[tauri::command]
   async fn hf_save_token(token: String) -> Result<(), String> {
       Entry::new(SERVICE, ACCOUNT).map_err(|e| e.to_string())?
           .set_password(&token).map_err(|e| e.to_string())
       // Note: mirroring to ~/.cache/huggingface/token from Tauri is intentionally
       // skipped — the sidecar's `dataclaw hf login` does that. Tauri's Keychain
       // write + sidecar env var injection is sufficient for app-internal use.
   }
   #[tauri::command]
   async fn hf_load_token() -> Result<Option<String>, String> { ... }
   #[tauri::command]
   async fn hf_delete_token() -> Result<(), String> { ... }
   #[tauri::command]
   async fn hf_whoami(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
       run_cli_with_token(&app, &["hf", "whoami"]).await
   }
   ```
2. **Sidecar token delivery**:
   ```rust
   async fn run_cli_with_token(app: &tauri::AppHandle, args: &[&str])
       -> Result<serde_json::Value, String> {
       let token = hf_load_token().await?.unwrap_or_default();
       let mut cmd = app.shell().sidecar("dataclaw").map_err(|e| e.to_string())?;
       if !token.is_empty() { cmd = cmd.env("HF_TOKEN", &token); }
       let (_rx, child) = cmd.args(args).spawn().map_err(|e| e.to_string())?;
       let output = child.wait_with_output().await.map_err(|e| e.to_string())?;
       parse_json_block(&output.stdout)
   }
   ```
3. **First-run modal** in `Auth.tsx` blocks app when `hf_load_token()` is None and `config.repo` is set.
4. Config tab "HF Account" section.
5. **`capabilities/default.json`** declares `hf_save_token`/`hf_load_token`/`hf_delete_token`/`hf_whoami` as allowed commands (cross-ref Step 27).

### Step 44: Scheduler/CLI token resolution
**Scope:** Small
1. `_ENV_ALLOWLIST` excludes HF_TOKEN. Scheduled `dataclaw auto` reads from Keychain via `_resolve_hf_token()`. Falls back to `~/.cache/huggingface/token` (the mirror written by `hf login` per Step 42), then fails exit-2.
2. `docs/AUTO_MODE.md`: scheduled runs require user login session for Keychain access; if logged out, the CLI falls back to the mirrored token file.

### Step 45: Tests — auth + token scrubbing
**Scope:** Small
1. `test_resolve_hf_token_prefers_env_then_keyring_then_file`.
2. **`test_hf_login_stores_in_keyring_AND_mirrors_to_hf_path`** — assert both `keyring.get_password` and `HF_STANDARD_TOKEN.read_text()` return the token after login. Verify `HF_STANDARD_TOKEN` mode is `0o600`.
3. **`test_hf_login_no_mirror_skips_file_write`** — `--no-mirror` flag does not touch `~/.cache/huggingface/token`.
4. `test_hf_logout_deletes_from_keyring_AND_removes_mirror`.
5. `test_auto_exits_2_when_no_token_anywhere`.
6. **`test_auto_falls_back_to_hf_path_when_keychain_locked`** — monkey-patch `keyring.get_password` to raise; HF token file present; auto succeeds.
7. `test_token_never_written_to_config_json`.
8. Rust unit tests for `hf_save_token`/`hf_load_token`/`hf_delete_token` round-trip.

---

## Phase 10: Push retry + persistent staging

### Step 46: Retry wrapper (`dataclaw/cli.py`)
**Scope:** Medium
1. **`_push_with_retry(run_dir, repo_id, manifest, logger)`** — same as v4: 3 attempts at 30s/2m/10m + jitter; retriable = `ConnectionError`/`TimeoutError`/urllib3-transport/`HfHubHTTPError` 429-with-Retry-After or 5xx; non-retriable = 401/403/400. On exhaustion: raise `PushFailed(cause, attempts, backoff_seconds_total)`.
2. **`auto` handler** (Step 22): on `PushFailed`, leave run dir at `~/.dataclaw/staging/{run_id}/` (no move); record `last_auto_run.result="error"` + `staging_dir`/`push_attempts`/`backoff_seconds_total`/`error`; exit 4.

### Step 47: `dataclaw auto --retry-only` (`dataclaw/cli.py`)
**Scope:** Small
1. Locates the most recent staging dir at `~/.dataclaw/staging/{run_id}/` that is NOT under `published/` and has a manifest. Reads manifest. Invokes `_push_with_retry` directly — no parsing.
2. On success: move dir to `~/.dataclaw/staging/published/{run_id}/`; advance cutoff from `manifest["max_end_time_by_source"]`; record `pushed`.
3. On failure: leave in place; exit 4.

### Step 48: Staging-size warning + `dataclaw clean-staging`
**Scope:** Small
1. On every `auto`, `du` over `~/.dataclaw/staging/` (excluding `published/`); if > 5 GB, log warning + surface in `RUN_SUMMARY.warnings`.
2. `dataclaw clean-staging [--yes]` — `shutil.rmtree` failed staging dirs (NOT `published/`).

### Step 49: Tauri retry + clear buttons
**Scope:** Small
1. Dashboard: "Last push failed — Retry" button when `result=error` → invokes `dataclaw_auto_retry_only`.
2. Config: "Clear staging" button → invokes `dataclaw_clean_staging({yes: true})`.

### Step 50: Tests
**Scope:** Medium
1. **`test_push_retry_backoff`** with `freezegun` — `ConnectionError` on attempts 1–2, success on 3; assert 3 attempts, total wait ≈ 150s.
2. `test_push_429_honors_retry_after`, `test_push_401_fails_immediately`.
3. **`test_push_exhaustion_exits_4_and_preserves_staging_in_root`** — all 3 attempts fail; assert run dir is at `~/.dataclaw/staging/{run_id}/` (NOT `published/`); `last_auto_run.result="error"`, `push_attempts==3`.
4. **`test_retry_only_reuses_failed_run_dir_and_publishes`** — seed failed staging dir; `--retry-only` succeeds; dir is now under `published/`.
5. `test_staging_size_warning_above_5gb`.

---

## Phase 11: Code signing + notarization + updater + release infrastructure

### Step 51: `docs/RELEASING.md` (new)
**Scope:** Small
1. Document one-time manual steps (Apple enrollment, cert, `.p8` API key, Team/Issuer/Key IDs, Tauri updater key generation via `pnpm tauri signer generate`).
2. Required GitHub repo secrets: `APPLE_DEVELOPER_ID_CERT_P12_BASE64`, `APPLE_DEVELOPER_ID_CERT_PASSWORD`, `APPLE_API_KEY_P8_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`, `APPLE_TEAM_ID`, **`APPLE_SIGNING_IDENTITY`** (the documented Tauri 2 var), `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.

### Step 52: Entitlements (`app/src-tauri/entitlements.plist`)
**Scope:** Small
1. Minimal hardened-runtime entitlements; `disable-library-validation=false` by default. Bump only if PyInstaller runtime fails to load (document the why).

### Step 53: `tauri.conf.json` — signing + updater (`app/src-tauri/tauri.conf.json`)
**Scope:** Small
1. ```json
   {
     "bundle": {
       "externalBin": ["binaries/dataclaw"],
       "macOS": {
         "signingIdentity": "${APPLE_SIGNING_IDENTITY}",
         "entitlements": "entitlements.plist",
         "hardenedRuntime": true,
         "providerShortName": "${APPLE_TEAM_ID}"
       }
     },
     "plugins": {
       "updater": {
         "active": true,
         "endpoints": ["https://github.com/banodoco/dataclaw/releases/latest/download/latest.json"],
         "dialog": false,
         "pubkey": "${TAURI_UPDATER_PUBKEY}"
       }
     }
   }
   ```
   `${APPLE_SIGNING_IDENTITY}` is the **documented Tauri 2 env var** (replaces v4's custom `DATACLAW_SIGNING_IDENTITY`).

### Step 54: `tauri-plugin-updater` wiring
**Scope:** Medium
1. Plugin in `Cargo.toml` (Step 26 already has it) + `package.json`: `@tauri-apps/plugin-updater: ^2`.
2. `main.rs`: register plugin; expose `check_for_updates`/`install_update` commands; "Check for updates" tray menu item.
3. Frontend: launch + weekly check via `config.updater.last_check`; non-blocking toast; user click → `install_update` (downloads, verifies ed25519 signature, relaunches).

### Step 55: `.github/workflows/release.yml` — uses `APPLE_SIGNING_IDENTITY`
**Scope:** Medium
1. Triggers on `v*` tag, matrix `[macos-14, macos-13]`. Imports cert + writes API key. **Sets `APPLE_SIGNING_IDENTITY` (NOT `DATACLAW_SIGNING_IDENTITY`)**:
   ```yaml
   - name: Build signed + notarized app
     env:
       APPLE_SIGNING_IDENTITY:   ${{ secrets.APPLE_SIGNING_IDENTITY }}
       APPLE_API_KEY_ID:         ${{ secrets.APPLE_API_KEY_ID }}
       APPLE_API_ISSUER:         ${{ secrets.APPLE_API_ISSUER }}
       APPLE_TEAM_ID:            ${{ secrets.APPLE_TEAM_ID }}
       APPLE_API_KEY_PATH:       ~/.appstoreconnect/private_keys/AuthKey_${{ secrets.APPLE_API_KEY_ID }}.p8
       TAURI_SIGNING_PRIVATE_KEY:          ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
       TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}
     run: pnpm -C app tauri build
   ```
2. `gh release upload` of `.dmg` + `.app.tar.gz` + `.sig`. Separate `publish-latest` job assembles `latest.json`.

### Step 56: `.github/workflows/ci.yml` — unsigned PR builds
**Scope:** Small
1. Matrix `[macos-14, macos-13]`; `APPLE_SIGNING_IDENTITY=""` (empty) to force unsigned; `pnpm tauri build`; assert `.app` + `.dmg` exist.

### Step 57: `Makefile` release targets
**Scope:** Small
1. ```makefile
   release-local:  ; APPLE_SIGNING_IDENTITY="" pnpm -C app tauri build
   release-signed: ; @test -n "$${APPLE_SIGNING_IDENTITY}" || (echo "set APPLE_SIGNING_IDENTITY" && exit 1); pnpm -C app tauri build
   ```

### Step 58: `README.md` — app installation
**Scope:** Small
1. "Pre-built .dmg files from GitHub Releases are signed and notarized — double-click to install. Building from source requires your own Apple Developer Certificate; see `docs/RELEASING.md`."

### Step 59: Tests — signing + updater
**Scope:** Small
1. **`test_release_yml_uses_apple_signing_identity_var`** — parse `release.yml`; assert `APPLE_SIGNING_IDENTITY` (NOT `DATACLAW_SIGNING_IDENTITY`) is in the env block.
2. `test_release_yml_has_required_secrets` — all 9 secrets referenced.
3. **`test_ci_yml_unsigned_uses_empty_apple_signing_identity`** — parse `ci.yml`; assert `APPLE_SIGNING_IDENTITY: ""` in PR env.
4. Rust `test_updater_signature_verification_rejects_tampered_json`.
5. Manual `info`: `spctl --assess --type execute DataClaw.app` reports "accepted; source=Notarized Developer ID".

---

## Phase 12: Emergency rollback

### Step 60: `dataclaw rollback` CLI verb (`dataclaw/cli.py`)
**Scope:** Medium
1. Subparser:
   ```python
   rb = sub.add_parser("rollback", help="Revert HF dataset repo to previous commit.")
   group = rb.add_mutually_exclusive_group(required=True)
   group.add_argument("--commit", help="Target commit SHA")
   group.add_argument("--list", action="store_true")
   rb.add_argument("--repo", help="Override configured repo")
   rb.add_argument("--dry-run", action="store_true")
   rb.add_argument("--limit", type=int, default=20)
   ```
2. `--list`: `HfApi.list_repo_commits` → JSON array.
3. `--commit SHA`: build `CommitOperation` list (Add per file at target revision, Delete for files present-now-absent-then), then `HfApi.create_commit(... operations=...)` with message `Rollback to {SHA[:8]}: {original_message}`.
4. `--dry-run`: print operations without `create_commit`.
5. Structured log event: `phase=rollback`, `initiator`, `target_commit`, `previous_commit`, `reverted_files`, `dry_run`.

### Step 61: Tauri "Releases" tab (`app/src/routes/Releases.tsx`)
**Scope:** Medium
1. List recent commits via `dataclaw_rollback_list`. Red Revert button per row → confirm modal with details → `dataclaw_rollback_commit(sha)`. Success banner.
2. Rust `dataclaw_rollback_list`/`dataclaw_rollback_commit` commands wired through Step 27 sidecar API.

### Step 62: README Privacy section
**Scope:** Small
1. "DataClaw has a one-click rollback. You can revert your HF dataset to any previous commit from the app or via `dataclaw rollback --commit <sha>`."

### Step 63: Tests
**Scope:** Small
1. `test_rollback_list_shows_recent_commits`.
2. `test_rollback_reverts_to_commit`.
3. `test_rollback_dry_run_does_not_call_create_commit`.
4. `test_rollback_logs_structured_event`.

---

## Execution Order

1. **Phase 1** — schema + cooled filter (file-mtime universally) + chmod 0600 + ISO parsing.
2. **Phase 2** — sharded export (direct-to-staging) with full HF exception taxonomy + positional `confirm`.
3. **Phase 3** — buckets + config CLI verbs + copy sync.
4. **Phase 4** — privacy-filter behind `pii` extra; floor preflight before merge.
5. **Phase 5** — auto + scheduler (HF_TOKEN excluded from allowlist).
6. **Phase 7** — PyInstaller sidecar (with keyring backends bundled) before Phase 6 frontend.
7. **Phase 6** — Tauri 2 base + capabilities file.
8. **Phase 8** — structured logging (daily-named files).
9. **Phase 9** — HF Keychain auth + `hf` verbs (mirroring to standard path).
10. **Phase 10** — push retry + persistent staging + `--retry-only`.
11. **Phase 11** — signing (`APPLE_SIGNING_IDENTITY`) + notarization + updater.
12. **Phase 12** — rollback (CLI + Tauri).

## Validation Order

1. Unit tests per phase.
2. Legacy parity: `test_export_to_jsonl_flat_path_unchanged_byte_for_byte`, `test_push_to_huggingface_legacy_signature_unchanged`, `test_confirm_file_flag_still_accepted_as_alias`.
3. **Direct-to-staging smoke**: kill process between export and push; verify staging dir at `~/.dataclaw/staging/{run_id}/` survives; `dataclaw auto --retry-only` resumes.
4. Same-day merge smoke: two sharded runs 5 min apart; shard contains all sessions.
5. **Cooled-rule smoke**: touch `~/.local/share/opencode/storage.db` to 23h ago → opencode session NOT exported; touch to 25h ago → exported. Same for claude jsonl files.
6. Token tests: `test_token_never_written_to_config_json`, `test_token_scrubbed_from_logs`, `test_hf_login_stores_in_keyring_AND_mirrors_to_hf_path`.
7. Contract test: `pytest -m pii`.
8. CI matrix: `ci.yml` builds unsigned `.app`+`.dmg` on macos-14 + macos-13 with `APPLE_SIGNING_IDENTITY=""`.
9. Capabilities file: `test_capabilities_file_grants_shell_sidecar_for_dataclaw`.
10. Bundled sidecar smoke: invoke `.app/Contents/MacOS/dataclaw-<triple> hf whoami --check-keyring-only` — exits non-zero with install hint (proves keyring backend loaded).
11. Logging filename: `test_logging_writes_to_dated_filename_today` asserts `auto-YYYY-MM-DD.jsonl`.
12. Release dry-run (manual `info`): tag → `release.yml` → notarized .dmg → `spctl --assess` "Notarized Developer ID".
13. Rollback smoke (manual `info`): two commits → revert → tree matches first commit.
14. Tauri E2E (manual `info`): fresh Mac → open .dmg → first-run token modal → Run Now → Logs tab streams → blocked → Findings → Retry → Releases tab → Revert.

## Risk & tradeoff notes

- **Tauri v2 sidecar API**: `tauri-plugin-shell` is the v2-canonical path. v1's `tauri::api::process::Command::new_sidecar` no longer exists in v2. Capabilities file is mandatory for IPC to work.
- **`APPLE_SIGNING_IDENTITY`**: Tauri 2 reads this name natively from the env. No custom indirection needed.
- **OpenCode SQLite mtime**: file-mtime semantic is conservative — any session activity in the same DB advances the mtime, so cooled-filter waits for 24h of total opencode quiet. Trade-off: a heavily-active opencode user may delay export of an old session indefinitely. Acceptable given user spec wording. If users complain, future work could split SQLite per session OR check the per-session row's `updated_at`.
- **Direct-to-staging crash safety**: writing directly to `~/.dataclaw/staging/{run_id}/` from the start eliminates the v4 crash window between `export_to_shards` and `_push_with_retry`. On any crash, the run dir is preserved with manifest; `--retry-only` resumes.
- **PyInstaller bundling keyring backends**: `collect_submodules("keyring")` plus explicit hidden-imports for `keyring.backends.macOS`, `keyring.backends.SecretService`, `secretstorage`, `jeepney` ensures the bundled binary can read Keychain on macOS and Secret Service on Linux. Without these, the sidecar would fail at first Keychain access in scheduled (no-Tauri) auto runs.
- **`hf login` mirrors to `~/.cache/huggingface/token`**: matches huggingface_hub convention; lets `huggingface-cli` and other tools see the token; provides a fallback path for scheduled runs where Keychain access is denied.
- **Logging filename custom namer**: subclassing `TimedRotatingFileHandler` to override `__init__` filename + `doRollover` is the cleanest path to "today's file is `auto-YYYY-MM-DD.jsonl`". Slightly more code than default, but matches user spec exactly and tests are easy to write.
- **Notarization + ed25519 updater key**: as v4. Document key rotation in `docs/RELEASING.md`.
- **Entitlements**: `disable-library-validation=false` by default; bump only if PyInstaller fails to launch.

## File-change index

| File | Phases | Nature |
|------|--------|--------|
| `dataclaw/config.py` | 1 | schema + chmod 0600 |
| `dataclaw/parser.py` | 1 | ISO/cooled (file-mtime universally; opencode = DB file) |
| `dataclaw/cli.py` | 1–5, 10, 12 | shards, confirm positional, auto direct-staging, retry, rollback |
| `dataclaw/privacy_filter.py` | 4 | new |
| `dataclaw/scheduler.py` | 5 | new (no HF_TOKEN, chmod 0600) |
| `dataclaw/logging.py` | 8 | new (DailyNamedHandler) |
| `dataclaw/auth.py` | 9 | new (`_resolve_hf_token`, `_store_hf_token` mirrors to HF path) |
| `pyproject.toml` | 1, 2, 4, 7 | extras + keyring + build extra |
| `pyinstaller.spec` | 7 | hidden-imports incl. `keyring.backends.*` + `secretstorage` |
| `scripts/build-sidecar.sh`, `scripts/build-latest-json.sh` | 7, 11 | new |
| `Makefile` | 7, 11 | build-sidecar, release-local/signed |
| `README.md`, `docs/RELEASING.md`, `docs/AUTO_MODE.md` | various | new sections / new files |
| `tests/test_*` | various | new + extended |
| `app/src-tauri/tauri.conf.json` | 6, 7, 11 | externalBin + APPLE_SIGNING_IDENTITY + updater |
| `app/src-tauri/capabilities/default.json` | 6 | NEW (Tauri v2 capabilities) |
| `app/src-tauri/Cargo.toml` | 6, 8, 9, 11 | tauri-plugin-shell/updater + keyring + notify |
| `app/src-tauri/entitlements.plist` | 11 | new |
| `app/src-tauri/src/{main.rs, dataclaw.rs, logs.rs, hf.rs}` | 6, 8, 9 | v2 ShellExt sidecar API |
| `app/src/routes/{Dashboard,Config,Findings,Logs,Auth,Releases}.tsx` | 6, 8–10, 12 | new/extended |
| `.github/workflows/{ci.yml, release.yml}` | 7, 11 | matrix + APPLE_SIGNING_IDENTITY |
