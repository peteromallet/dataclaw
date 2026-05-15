use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::Mutex;

const CHECK_EVERY: Duration = Duration::from_secs(60);
const DEFAULT_SYNC_INTERVAL_HOURS: f64 = 24.0;
const MIN_SYNC_INTERVAL_HOURS: f64 = 1.0;
const MAX_SYNC_INTERVAL_HOURS: f64 = 24.0 * 30.0;

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn app_settings(config: &Value) -> Option<&Map<String, Value>> {
    config.get("app").and_then(Value::as_object)
}

fn sync_enabled(config: &Value) -> bool {
    app_settings(config)
        .and_then(|obj| obj.get("sync_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

fn interval_hours(config: &Value) -> f64 {
    app_settings(config)
        .and_then(|obj| obj.get("sync_interval_hours"))
        .and_then(Value::as_f64)
        .unwrap_or(DEFAULT_SYNC_INTERVAL_HOURS)
        .clamp(MIN_SYNC_INTERVAL_HOURS, MAX_SYNC_INTERVAL_HOURS)
}

fn last_scheduled_sync_at(config: &Value) -> Option<f64> {
    app_settings(config)
        .and_then(|obj| obj.get("last_scheduled_sync_at"))
        .and_then(Value::as_f64)
}

fn set_app_number(config: &mut Value, key: &str, value: f64) -> Result<(), String> {
    let app = crate::dataclaw::object_entry(config, "app")?;
    let number = serde_json::Number::from_f64(value)
        .ok_or_else(|| format!("invalid app setting number for {key}"))?;
    app.insert(key.to_string(), Value::Number(number));
    Ok(())
}

fn set_next_sync(config: &mut Value, from_seconds: f64, interval_seconds: f64) -> Result<(), String> {
    set_app_number(config, "next_scheduled_sync_at", from_seconds + interval_seconds)
}

fn initialize_schedule_if_needed() -> Result<(), String> {
    let mut config = crate::dataclaw::read_config()?;
    let enabled = sync_enabled(&config);
    let interval_seconds = interval_hours(&config) * 60.0 * 60.0;
    let now = now_seconds();
    let needs_initial_timestamp = enabled && last_scheduled_sync_at(&config).is_none();
    let app = crate::dataclaw::object_entry(&mut config, "app")?;
    app.entry("sync_enabled").or_insert(Value::Bool(true));
    app.entry("sync_interval_hours")
        .or_insert_with(|| Value::Number(24.into()));

    if needs_initial_timestamp {
        set_app_number(&mut config, "last_scheduled_sync_at", now)?;
        set_next_sync(&mut config, now, interval_seconds)?;
        crate::dataclaw::write_config(&config)?;
    }
    Ok(())
}

fn due(config: &Value, now: f64) -> bool {
    if !sync_enabled(config) {
        return false;
    }
    let interval_seconds = interval_hours(config) * 60.0 * 60.0;
    let last = last_scheduled_sync_at(config).unwrap_or(now);
    now - last >= interval_seconds
}

async fn run_due_sync(app: AppHandle) -> Result<(), String> {
    let mut config = crate::dataclaw::read_config()?;
    let now = now_seconds();
    if !due(&config, now) {
        return Ok(());
    }

    let interval_seconds = interval_hours(&config) * 60.0 * 60.0;
    set_app_number(&mut config, "last_scheduled_sync_at", now)?;
    set_app_number(&mut config, "last_scheduled_sync_started_at", now)?;
    set_next_sync(&mut config, now, interval_seconds)?;
    crate::dataclaw::write_config(&config)?;

    let _ = app.emit("dataclaw-scheduled-sync-started", ());
    match crate::dataclaw::run_auto_pipeline(
        &app,
        "User explicitly approved publishing to Hugging Face via DataClaw.app scheduled sync.",
    )
    .await
    {
        Ok(result) => {
            let mut config = crate::dataclaw::read_config()?;
            set_app_number(&mut config, "last_scheduled_sync_finished_at", now_seconds())?;
            crate::dataclaw::write_config(&config)?;
            let _ = app.emit("dataclaw-scheduled-sync-finished", result);
            Ok(())
        }
        Err(error) => {
            let mut config = crate::dataclaw::read_config()?;
            let app_settings = crate::dataclaw::object_entry(&mut config, "app")?;
            app_settings.insert("last_scheduled_sync_error".into(), Value::String(error.clone()));
            set_app_number(&mut config, "last_scheduled_sync_finished_at", now_seconds())?;
            crate::dataclaw::write_config(&config)?;
            let _ = app.emit("dataclaw-scheduled-sync-failed", error.clone());
            Err(error)
        }
    }
}

pub fn start(app: AppHandle) -> Result<(), String> {
    initialize_schedule_if_needed()?;
    let running = std::sync::Arc::new(Mutex::new(()));
    tauri::async_runtime::spawn({
        let running = running.clone();
        async move {
            let mut interval = tokio::time::interval(CHECK_EVERY);
            loop {
                interval.tick().await;
                let Ok(_guard) = running.try_lock() else {
                    continue;
                };
                let _ = run_due_sync(app.clone()).await;
            }
        }
    });
    Ok(())
}
