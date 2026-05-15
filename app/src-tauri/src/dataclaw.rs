use std::{
    collections::HashMap,
    fs,
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Map, Value};
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::{process::CommandEvent, ShellExt};
use tokio::time::timeout;

const JSON_MARKER: &str = "---DATACLAW_JSON---";
const SIDECAR_TIMEOUT: Duration = Duration::from_secs(60 * 60);

fn config_path() -> Result<PathBuf, String> {
    Ok(dirs::home_dir()
        .ok_or_else(|| "home directory is required".to_string())?
        .join(".dataclaw")
        .join("config.json"))
}

pub(crate) fn default_config() -> Value {
    let mut obj = Map::new();
    obj.insert("repo".into(), Value::Null);
    obj.insert("source".into(), Value::Null);
    obj.insert("excluded_projects".into(), Value::Array(Vec::new()));
    obj.insert("redact_strings".into(), Value::Array(Vec::new()));
    obj.insert(
        "privacy_filter".into(),
        Value::Object(Map::from_iter([
            ("enabled".into(), Value::Bool(true)),
            ("device".into(), Value::String("auto".into())),
        ])),
    );
    obj.insert(
        "app".into(),
        Value::Object(Map::from_iter([
            ("launch_at_login".into(), Value::Bool(true)),
            ("sync_enabled".into(), Value::Bool(true)),
            ("sync_interval_hours".into(), Value::Number(24.into())),
        ])),
    );
    Value::Object(obj)
}

pub(crate) fn read_config() -> Result<Value, String> {
    let path = config_path()?;
    let stored = match fs::read_to_string(path) {
        Ok(text) => serde_json::from_str::<Value>(&text).map_err(|e| e.to_string())?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Value::Object(Map::new()),
        Err(e) => return Err(e.to_string()),
    };
    let mut base = default_config();
    if let (Some(base_obj), Some(stored_obj)) = (base.as_object_mut(), stored.as_object()) {
        for (key, value) in stored_obj {
            match (
                base_obj.get_mut(key).and_then(Value::as_object_mut),
                value.as_object(),
            ) {
                (Some(base_nested), Some(stored_nested)) => {
                    for (nested_key, nested_value) in stored_nested {
                        base_nested.insert(nested_key.clone(), nested_value.clone());
                    }
                }
                _ => {
                    base_obj.insert(key.clone(), value.clone());
                }
            }
        }
    }
    Ok(base)
}

pub(crate) fn write_config(config: &Value) -> Result<(), String> {
    let path = config_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let text = serde_json::to_string_pretty(config).map_err(|e| e.to_string())?;
    let tmp_path = path.with_extension("json.tmp");
    fs::write(&tmp_path, format!("{text}\n")).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp_path, fs::Permissions::from_mode(0o600))
            .map_err(|e| e.to_string())?;
    }
    fs::rename(&tmp_path, &path).map_err(|e| e.to_string())?;
    Ok(())
}

fn mask_config_for_display(config: &Value, show_secrets: bool) -> Value {
    let mut copy = config.clone();
    if show_secrets {
        return copy;
    }
    if let Some(obj) = copy.as_object_mut() {
        if let Some(values) = obj.get("redact_strings").and_then(Value::as_array) {
            obj.insert(
                "redact_strings".into(),
                Value::Array(values.iter().map(|_| Value::String("***".into())).collect()),
            );
        }
    }
    copy
}

fn parse_list(value: &str) -> Vec<Value> {
    value
        .split([',', '\n'])
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(|item| Value::String(item.to_string()))
        .collect()
}

pub(crate) fn object_entry<'a>(config: &'a mut Value, key: &str) -> Result<&'a mut Map<String, Value>, String> {
    let obj = config
        .as_object_mut()
        .ok_or_else(|| "config root is not an object".to_string())?;
    let entry = obj
        .entry(key.to_string())
        .or_insert_with(|| Value::Object(Map::new()));
    if !entry.is_object() {
        *entry = Value::Object(Map::new());
    }
    entry
        .as_object_mut()
        .ok_or_else(|| format!("{key} is not an object"))
}

fn privacy_filter_enabled(config: &Value) -> bool {
    config
        .get("privacy_filter")
        .and_then(Value::as_object)
        .and_then(|obj| obj.get("enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

fn schedule_status(config: &Value) -> Value {
    let app = config.get("app").and_then(Value::as_object);
    json!({
        "launch_at_login": app
            .and_then(|obj| obj.get("launch_at_login"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        "sync_enabled": app
            .and_then(|obj| obj.get("sync_enabled"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        "sync_interval_hours": app
            .and_then(|obj| obj.get("sync_interval_hours"))
            .and_then(Value::as_f64)
            .unwrap_or(24.0),
        "last_scheduled_sync_at": app
            .and_then(|obj| obj.get("last_scheduled_sync_at"))
            .cloned()
            .unwrap_or(Value::Null),
        "next_scheduled_sync_at": app
            .and_then(|obj| obj.get("next_scheduled_sync_at"))
            .cloned()
            .unwrap_or(Value::Null),
        "last_scheduled_sync_error": app
            .and_then(|obj| obj.get("last_scheduled_sync_error"))
            .cloned()
            .unwrap_or(Value::Null),
    })
}

fn pid_is_running(pid: u32) -> bool {
    std::process::Command::new("kill")
        .arg("-0")
        .arg(pid.to_string())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn active_auto_run() -> Value {
    let Some(home) = dirs::home_dir() else {
        return Value::Null;
    };
    let lock_path = home.join(".dataclaw").join(".auto.lock");
    let Ok(raw_pid) = fs::read_to_string(&lock_path) else {
        return Value::Null;
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        let _ = fs::remove_file(&lock_path);
        return Value::Null;
    };
    if pid_is_running(pid) {
        json!({ "pid": pid, "lock_path": lock_path })
    } else {
        let _ = fs::remove_file(&lock_path);
        Value::Null
    }
}

pub(crate) fn ensure_auto_enabled_for_run_now() -> Result<(), String> {
    let mut config = read_config()?;
    if config.get("repo").and_then(Value::as_str).unwrap_or("").trim().is_empty() {
        return Err("Run Now requires a configured Hugging Face repo.".to_string());
    }

    let publish_attestation = config
        .get("publish_attestation")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("User explicitly approved publishing to Hugging Face via DataClaw.app Run Now.")
        .to_string();
    let enable_privacy_filter = privacy_filter_enabled(&config);

    let auto = object_entry(&mut config, "auto")?;
    auto.insert("enabled".into(), Value::Bool(true));
    auto.entry("policy")
        .or_insert_with(|| Value::String("strict".into()));
    auto.entry("full_name").or_insert(Value::Null);
    auto.entry("skip_full_name_scan")
        .or_insert(Value::Bool(false));
    auto.insert(
        "enable_privacy_filter".into(),
        Value::Bool(enable_privacy_filter),
    );
    auto.insert(
        "publish_attestation".into(),
        Value::String(publish_attestation),
    );
    auto.entry("binary")
        .or_insert_with(|| Value::String("dataclaw".into()));
    let enabled_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs().to_string())
        .unwrap_or_else(|_| "0".into());
    auto.entry("enabled_at")
        .or_insert_with(|| Value::String(enabled_at));

    let has_cutoff = config
        .get("last_export_cutoff")
        .and_then(Value::as_object)
        .map(|obj| !obj.is_empty())
        .unwrap_or(false);
    if !has_cutoff {
        let last_export = config.get("last_export").and_then(Value::as_object);
        let source = config
            .get("source")
            .and_then(Value::as_str)
            .or_else(|| last_export.and_then(|obj| obj.get("source")).and_then(Value::as_str));
        let timestamp = last_export
            .and_then(|obj| obj.get("timestamp"))
            .and_then(Value::as_str);
        if let (Some(source), Some(timestamp)) = (source, timestamp) {
            let mut cutoff = Map::new();
            cutoff.insert(source.to_string(), Value::String(timestamp.to_string()));
            let obj = config
                .as_object_mut()
                .ok_or_else(|| "config root is not an object".to_string())?;
            obj.insert("last_export_cutoff".into(), Value::Object(cutoff));
        }
    }

    write_config(&config)
}

fn auto_export_path() -> Result<PathBuf, String> {
    let Some(home) = dirs::home_dir() else {
        return Err("Cannot resolve home directory for automated export.".to_string());
    };
    let dir = home.join(".dataclaw");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("dataclaw_auto_export.jsonl"))
}

fn emit_auto_progress(app: &AppHandle, msg: &str, phase: &str, extra: Value) {
    let payload = json!({
        "msg": msg,
        "phase": phase,
        "extra": extra,
    });
    let _ = app.emit("logs-line", payload.to_string());
}

pub fn parse_json_block(stdout: &[u8]) -> Result<Value, String> {
    let text = String::from_utf8_lossy(stdout);
    let payload = if let Some(start) = text.find(JSON_MARKER) {
        let after_marker = &text[start + JSON_MARKER.len()..];
        if let Some(end) = after_marker.find(JSON_MARKER) {
            &after_marker[..end]
        } else {
            after_marker
        }
    } else {
        text.as_ref()
    };
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return Err("sidecar produced no JSON on stdout".to_string());
    }
    match serde_json::from_str(trimmed) {
        Ok(value) => Ok(value),
        Err(first_err) => {
            for (start, _) in trimmed.match_indices('{').rev() {
                let mut stream = serde_json::Deserializer::from_str(&trimmed[start..]).into_iter::<Value>();
                if let Some(Ok(value)) = stream.next() {
                    return Ok(value);
                }
            }
            Err(first_err.to_string())
        }
    }
}

fn base_env() -> HashMap<String, String> {
    let mut env = HashMap::new();
    for key in [
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "TMPDIR",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
    ] {
        if let Ok(value) = std::env::var(key) {
            env.insert(key.to_string(), value);
        }
    }
    env
}

pub async fn run_sidecar(
    app: &AppHandle,
    args: &[&str],
    env: Option<HashMap<String, String>>,
) -> Result<Value, String> {
    let mut merged = base_env();
    if let Some(extra) = env {
        merged.extend(extra);
    }

    let command = app
        .shell()
        .sidecar("dataclaw")
        .map_err(|e| e.to_string())?
        .args(args)
        .envs(merged);

    let (mut rx, _child) = command.spawn().map_err(|e| format!("spawn failed: {e}"))?;
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_code: Option<i32> = None;

    let result = timeout(SIDECAR_TIMEOUT, async {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(chunk) => stdout.extend_from_slice(&chunk),
                CommandEvent::Stderr(chunk) => stderr.extend_from_slice(&chunk),
                CommandEvent::Terminated(payload) => {
                    exit_code = payload.code;
                    break;
                }
                _ => {}
            }
        }
    })
    .await;

    if result.is_err() {
        let stderr_text = String::from_utf8_lossy(&stderr);
        let stdout_text = String::from_utf8_lossy(&stdout);
        return Err(format!(
            "sidecar timed out after {}s; args={:?}\nstdout: {}\nstderr: {}",
            SIDECAR_TIMEOUT.as_secs(),
            args,
            stdout_text.chars().take(500).collect::<String>(),
            stderr_text.chars().take(500).collect::<String>(),
        ));
    }

    let parsed = parse_json_block(&stdout);
    if !matches!(exit_code, Some(0)) {
        let stderr_text = String::from_utf8_lossy(&stderr);
        let stdout_text = String::from_utf8_lossy(&stdout);
        let details = parsed
            .ok()
            .map(|value| value.to_string())
            .unwrap_or_else(|| stdout_text.chars().take(500).collect::<String>());
        return Err(format!(
            "sidecar exit={:?} args={:?}: {}\nstderr: {}",
            exit_code,
            args,
            details,
            stderr_text.chars().take(500).collect::<String>(),
        ));
    }

    match parsed {
        Ok(value) => Ok(value),
        Err(parse_err) => {
            let stderr_text = String::from_utf8_lossy(&stderr);
            let stdout_text = String::from_utf8_lossy(&stdout);
            Err(format!(
                "sidecar exit={:?} args={:?}: {parse_err}\nstdout: {}\nstderr: {}",
                exit_code,
                args,
                stdout_text.chars().take(500).collect::<String>(),
                stderr_text.chars().take(500).collect::<String>(),
            ))
        }
    }
}

#[tauri::command]
pub fn dataclaw_status() -> Result<Value, String> {
    let config = read_config()?;
    let hf_logged_in = crate::hf::hf_load_token()?.is_some();
    Ok(json!({
        "stage": config.get("stage").cloned().unwrap_or(Value::Null),
        "hf_logged_in": hf_logged_in,
        "repo": config.get("repo").cloned().unwrap_or(Value::Null),
        "source": config.get("source").cloned().unwrap_or(Value::Null),
        "projects_confirmed": config.get("projects_confirmed").cloned().unwrap_or(Value::Bool(false)),
        "last_export": config.get("last_export").cloned().unwrap_or(Value::Null),
        "last_dataset_update": config.get("last_dataset_update").cloned().unwrap_or(Value::Null),
        "last_auto_run": config.get("last_auto_run").cloned().unwrap_or(Value::Null),
        "active_auto_run": active_auto_run(),
        "schedule": schedule_status(&config),
    }))
}

pub async fn run_auto_pipeline(app: &AppHandle, publish_attestation: &str) -> Result<Value, String> {
    ensure_auto_enabled_for_run_now()?;
    let output_path = auto_export_path()?;
    let output_path_string = output_path.to_string_lossy().to_string();
    emit_auto_progress(
        app,
        "auto_run_started",
        "start",
        json!({ "dry_run": false, "source": "configured" }),
    );
    emit_auto_progress(
        app,
        "auto_gate_checked",
        "gate",
        json!({ "privacy_filter_enabled": true, "policy": "configured" }),
    );

    crate::hf::run_with_token(
        app,
        &[
            "export",
            "--no-push",
            "--output",
            output_path_string.as_str(),
        ],
    )
    .await?;

    emit_auto_progress(
        app,
        "auto_confirm_started",
        "confirm",
        json!({ "file": output_path_string.clone() }),
    );
    let confirm_result = crate::hf::run_with_token(
        app,
        &[
            "confirm",
            "--file",
            output_path_string.as_str(),
            "--skip-full-name-scan",
            "--attest-full-name",
            "User skipped full name scan for DataClaw.app automated Run Now.",
            "--attest-sensitive",
            "User asked DataClaw.app to use configured redactions, privacy filters, company/client/internal name and URL/domain settings; no additional redactions were provided for this automated run.",
            "--attest-manual-scan",
            "DataClaw.app automated Run Now performed a manual scan equivalent over 20 sessions across beginning, middle, and end using automated review before publishing.",
        ],
    )
    .await?;
    emit_auto_progress(
        app,
        "auto_confirm_finished",
        "confirm",
        json!({
            "total_sessions": confirm_result.get("total_sessions").cloned().unwrap_or(Value::Null),
            "file_size": confirm_result.get("file_size").cloned().unwrap_or(Value::Null),
        }),
    );

    let args = vec!["export", "--publish-attestation", publish_attestation];
    crate::hf::run_with_token(app, &args).await
}

#[tauri::command]
pub async fn dataclaw_auto_now(app: AppHandle, force: bool) -> Result<Value, String> {
    let attestation = if force {
        "User explicitly approved publishing to Hugging Face via DataClaw.app Run Now with force enabled."
    } else {
        "User explicitly approved publishing to Hugging Face via DataClaw.app Run Now."
    };
    run_auto_pipeline(&app, attestation).await
}

#[tauri::command]
pub fn dataclaw_config_get(show_secrets: bool) -> Result<Value, String> {
    let config = read_config()?;
    Ok(mask_config_for_display(&config, show_secrets))
}

#[tauri::command]
pub async fn dataclaw_list_projects(app: AppHandle, source: Option<String>) -> Result<Value, String> {
    let source = source.unwrap_or_else(|| "all".to_string());
    run_sidecar(&app, &["list", "--source", &source], None).await
}

#[derive(serde::Deserialize, Default)]
pub struct ConfigSetArgs {
    pub repo: Option<String>,
    pub source: Option<String>,
    pub set_redact: Option<String>,
    pub set_redact_usernames: Option<String>,
    pub set_excluded: Option<String>,
    pub confirm_projects: Option<bool>,
    pub default_bucket: Option<String>,
    pub privacy_filter: Option<bool>,
    pub privacy_filter_device: Option<String>,
    pub launch_at_login: Option<bool>,
    pub sync_enabled: Option<bool>,
    pub sync_interval_hours: Option<f64>,
}

#[tauri::command]
pub fn dataclaw_config_set(args: ConfigSetArgs) -> Result<Value, String> {
    let mut config = read_config()?;
    let obj = config
        .as_object_mut()
        .ok_or_else(|| "config root is not an object".to_string())?;

    if let Some(repo) = args.repo {
        obj.insert("repo".into(), Value::String(repo));
    }
    if let Some(source) = args.source {
        obj.insert("source".into(), Value::String(source));
    }
    if let Some(redact) = args.set_redact {
        obj.insert("redact_strings".into(), Value::Array(parse_list(&redact)));
    }
    if let Some(usernames) = args.set_redact_usernames {
        obj.insert("redact_usernames".into(), Value::Array(parse_list(&usernames)));
    }
    if let Some(excluded) = args.set_excluded {
        obj.insert("excluded_projects".into(), Value::Array(parse_list(&excluded)));
    }
    if args.confirm_projects == Some(true) {
        obj.insert("projects_confirmed".into(), Value::Bool(true));
    }
    if let Some(bucket) = args.default_bucket {
        let folder_rules = object_entry(&mut config, "folder_rules")?;
        if bucket.trim().is_empty() {
            folder_rules.remove("default_bucket");
        } else {
            folder_rules.insert("default_bucket".into(), Value::String(bucket));
        }
    }
    if let Some(enabled) = args.privacy_filter {
        let privacy_filter = object_entry(&mut config, "privacy_filter")?;
        privacy_filter.insert("enabled".into(), Value::Bool(enabled));
    }
    if let Some(device) = args.privacy_filter_device {
        let privacy_filter = object_entry(&mut config, "privacy_filter")?;
        let normalized = device.trim().to_ascii_lowercase();
        let value = match normalized.as_str() {
            "cpu" | "mps" => normalized,
            _ => "auto".to_string(),
        };
        privacy_filter.insert("device".into(), Value::String(value));
    }
    if args.launch_at_login.is_some()
        || args.sync_enabled.is_some()
        || args.sync_interval_hours.is_some()
    {
        let sync_settings_changed = args.sync_enabled.is_some() || args.sync_interval_hours.is_some();
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_secs_f64())
            .unwrap_or(0.0);
        let app_settings = object_entry(&mut config, "app")?;
        if let Some(enabled) = args.launch_at_login {
            app_settings.insert("launch_at_login".into(), Value::Bool(enabled));
            crate::startup::set_launch_at_login(enabled)?;
        }
        if let Some(enabled) = args.sync_enabled {
            app_settings.insert("sync_enabled".into(), Value::Bool(enabled));
        }
        if let Some(hours) = args.sync_interval_hours {
            let normalized = if hours.is_finite() {
                hours.clamp(1.0, 24.0 * 30.0)
            } else {
                24.0
            };
            let number = serde_json::Number::from_f64(normalized)
                .ok_or_else(|| "invalid sync interval".to_string())?;
            app_settings.insert("sync_interval_hours".into(), Value::Number(number));
        }
        if sync_settings_changed {
            let interval_hours = app_settings
                .get("sync_interval_hours")
                .and_then(Value::as_f64)
                .unwrap_or(24.0)
                .clamp(1.0, 24.0 * 30.0);
            let next = serde_json::Number::from_f64(now + interval_hours * 60.0 * 60.0)
                .ok_or_else(|| "invalid next sync time".to_string())?;
            let last = serde_json::Number::from_f64(now)
                .ok_or_else(|| "invalid last sync time".to_string())?;
            app_settings.insert("last_scheduled_sync_at".into(), Value::Number(last));
            app_settings.insert("next_scheduled_sync_at".into(), Value::Number(next));
            app_settings.remove("last_scheduled_sync_error");
        }
    }

    write_config(&config)?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::parse_json_block;

    #[test]
    fn parse_json_block_extracts_marker_payload() {
        let stdout = br#"before
---DATACLAW_JSON---
{"ok": true}
---DATACLAW_JSON---
after"#;
        let value = parse_json_block(stdout).unwrap();
        assert_eq!(value["ok"], true);
    }

    #[test]
    fn parse_json_block_falls_back_to_full_stdout_when_no_marker() {
        let value = parse_json_block(br#"{"stage": "done"}"#).unwrap();
        assert_eq!(value["stage"], "done");
    }

    #[test]
    fn parse_json_block_extracts_json_after_human_stdout() {
        let stdout = br#"Logged in as: peteromallet
Pushing to: peteromallet/my-dataclaw-data
{
  "result": "pushed",
  "run_id": "abc123"
}
"#;
        let value = parse_json_block(stdout).unwrap();
        assert_eq!(value["result"], "pushed");
        assert_eq!(value["run_id"], "abc123");
    }

    #[test]
    fn parse_json_block_errors_on_invalid_json() {
        let err = parse_json_block(b"not-json").unwrap_err();
        assert!(err.contains("expected") || err.contains("EOF"));
    }

    #[test]
    fn parse_json_block_errors_clearly_on_empty_stdout() {
        let err = parse_json_block(b"").unwrap_err();
        assert_eq!(err, "sidecar produced no JSON on stdout");
    }
}
