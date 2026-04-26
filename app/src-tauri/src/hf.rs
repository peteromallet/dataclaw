use std::{collections::HashMap, fs};

use serde_json::{json, Value};
use tauri::AppHandle;

const SERVICE: &str = "io.dataclaw.app";
const ACCOUNT: &str = "hf_token";
const WHOAMI_URL: &str = "https://huggingface.co/api/whoami-v2";

fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|e| e.to_string())
}

pub fn hf_token_path() -> std::path::PathBuf {
    dirs::home_dir()
        .expect("home directory is required")
        .join(".cache")
        .join("huggingface")
        .join("token")
}

fn write_mirror(token: &str) -> Result<(), String> {
    let path = hf_token_path();
    let parent = path
        .parent()
        .ok_or_else(|| "token path has no parent".to_string())?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    fs::write(&path, token).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn read_mirror() -> Option<String> {
    let token = fs::read_to_string(hf_token_path()).ok()?;
    let trimmed = token.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn delete_mirror() -> Result<(), String> {
    match fs::remove_file(hf_token_path()) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn try_save_keychain(token: &str) {
    if let Ok(entry) = entry() {
        let _ = entry.set_password(token);
    }
}

fn try_load_keychain() -> Option<String> {
    entry().ok()?.get_password().ok()
}

fn try_delete_keychain() {
    if let Ok(entry) = entry() {
        let _ = entry.delete_credential();
    }
}

#[tauri::command]
pub fn hf_save_token(token: String) -> Result<(), String> {
    let trimmed = token.trim();
    if trimmed.is_empty() {
        return Err("token is empty".into());
    }
    write_mirror(trimmed)?;
    try_save_keychain(trimmed);
    Ok(())
}

#[tauri::command]
pub fn hf_load_token() -> Result<Option<String>, String> {
    if let Some(token) = read_mirror() {
        return Ok(Some(token));
    }
    Ok(try_load_keychain())
}

#[tauri::command]
pub fn hf_delete_token() -> Result<(), String> {
    try_delete_keychain();
    let _ = delete_mirror();
    Ok(())
}

pub async fn run_with_token(app: &AppHandle, args: &[&str]) -> Result<Value, String> {
    let env = match hf_load_token()? {
        Some(token) if !token.is_empty() => {
            let mut env = HashMap::new();
            env.insert("HF_TOKEN".to_string(), token);
            Some(env)
        }
        _ => None,
    };
    crate::dataclaw::run_sidecar(app, args, env).await
}

#[tauri::command]
pub async fn hf_whoami(_app: AppHandle) -> Result<Value, String> {
    let token = match hf_load_token()? {
        Some(token) => token,
        None => return Ok(json!({ "ok": false, "user": null, "error": "no token" })),
    };
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(WHOAMI_URL)
        .header("Authorization", format!("Bearer {token}"))
        .header("User-Agent", "DataClaw-app")
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Ok(json!({
            "ok": false,
            "user": null,
            "status": status.as_u16(),
            "error": body
        }));
    }
    let body: Value = resp.json().await.map_err(|e| e.to_string())?;
    let user = body
        .get("name")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(json!({
        "ok": true,
        "user": user,
        "raw": body
    }))
}

#[cfg(test)]
mod tests {
    use super::{delete_mirror, hf_token_path, write_mirror};
    use std::{
        fs,
        sync::Mutex,
        time::{SystemTime, UNIX_EPOCH},
    };

    static HOME_LOCK: Mutex<()> = Mutex::new(());

    fn with_temp_home(test: impl FnOnce()) {
        let _guard = HOME_LOCK.lock().unwrap();
        let old_home = std::env::var_os("HOME");
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let home = std::env::temp_dir().join(format!("dataclaw-hf-test-{suffix}"));
        fs::create_dir_all(&home).unwrap();
        std::env::set_var("HOME", &home);
        test();
        if let Some(old_home) = old_home {
            std::env::set_var("HOME", old_home);
        } else {
            std::env::remove_var("HOME");
        }
        let _ = fs::remove_dir_all(home);
    }

    #[cfg(unix)]
    #[test]
    fn write_mirror_writes_token_with_chmod_600() {
        use std::os::unix::fs::PermissionsExt;

        with_temp_home(|| {
            write_mirror("hf_test").unwrap();
            let path = hf_token_path();
            assert_eq!(fs::read_to_string(&path).unwrap(), "hf_test");
            let mode = fs::metadata(path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        });
    }

    #[test]
    fn delete_mirror_removes_file() {
        with_temp_home(|| {
            write_mirror("hf_test").unwrap();
            let path = hf_token_path();
            assert!(path.exists());
            delete_mirror().unwrap();
            assert!(!path.exists());
        });
    }
}
