use std::{env, fs, path::PathBuf};

use serde_json::Value;

const LAUNCH_AGENT_LABEL: &str = "io.dataclaw.desktop";

fn launch_agent_path() -> Result<PathBuf, String> {
    Ok(dirs::home_dir()
        .ok_or_else(|| "home directory is required".to_string())?
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{LAUNCH_AGENT_LABEL}.plist")))
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn launch_agent_plist(exe: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
"#,
        label = LAUNCH_AGENT_LABEL,
        exe = xml_escape(exe),
    )
}

pub fn set_launch_at_login(enabled: bool) -> Result<(), String> {
    let path = launch_agent_path()?;
    if enabled {
        let exe = env::current_exe().map_err(|e| e.to_string())?;
        let exe = exe
            .to_str()
            .ok_or_else(|| "current executable path is not valid UTF-8".to_string())?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        fs::write(&path, launch_agent_plist(exe)).map_err(|e| e.to_string())?;
    } else if path.exists() {
        fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

pub fn reconcile_launch_at_login_default() -> Result<(), String> {
    let mut config = crate::dataclaw::read_config()?;
    let enabled = config
        .get("app")
        .and_then(Value::as_object)
        .and_then(|obj| obj.get("launch_at_login"))
        .and_then(Value::as_bool)
        .unwrap_or(true);

    let app_settings = crate::dataclaw::object_entry(&mut config, "app")?;
    app_settings.insert("launch_at_login".into(), Value::Bool(enabled));
    set_launch_at_login(enabled)?;
    crate::dataclaw::write_config(&config)
}

#[tauri::command]
pub fn launch_at_login_is_installed() -> Result<bool, String> {
    Ok(launch_agent_path()?.exists())
}
