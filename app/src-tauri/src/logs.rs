use std::{
    collections::HashMap,
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Seek, SeekFrom},
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
};

use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use tauri::{AppHandle, Emitter, Manager};

pub fn log_dir() -> PathBuf {
    dirs::home_dir().unwrap().join(".dataclaw/logs")
}

fn is_jsonl(path: &Path) -> bool {
    path.extension().and_then(|ext| ext.to_str()) == Some("jsonl")
}

fn handle_log_event(
    event: Event,
    app_handle: &AppHandle,
    positions: &Arc<Mutex<HashMap<PathBuf, u64>>>,
) {
    if !matches!(event.kind, EventKind::Modify(_) | EventKind::Create(_)) {
        return;
    }

    for path in event.paths.into_iter().filter(|path| is_jsonl(path)) {
        let Ok(mut file) = OpenOptions::new().read(true).open(&path) else {
            continue;
        };
        let mut position = {
            let guard = positions.lock().unwrap();
            *guard.get(&path).unwrap_or(&0)
        };
        if file.seek(SeekFrom::Start(position)).is_err() {
            continue;
        }

        let mut reader = BufReader::new(file);
        let mut line = String::new();
        loop {
            line.clear();
            let Ok(bytes_read) = reader.read_line(&mut line) else {
                break;
            };
            if bytes_read == 0 {
                break;
            }
            position += bytes_read as u64;
            let payload = line.trim_end_matches(&['\r', '\n'][..]).to_string();
            let _ = app_handle.emit("logs-line", payload);
        }

        let mut guard = positions.lock().unwrap();
        guard.insert(path, position);
    }
}

pub fn start_log_watcher(app: AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let dir = log_dir();
    fs::create_dir_all(&dir)?;
    let mut initial_positions = HashMap::<PathBuf, u64>::new();
    for entry in fs::read_dir(&dir)?.filter_map(Result::ok) {
        let path = entry.path();
        if is_jsonl(&path) {
            if let Ok(metadata) = fs::metadata(&path) {
                initial_positions.insert(path, metadata.len());
            }
        }
    }
    let positions = Arc::new(Mutex::new(initial_positions));
    let app_handle = app.clone();
    let positions_for_watcher = Arc::clone(&positions);
    let mut watcher = RecommendedWatcher::new(
        move |result: notify::Result<Event>| {
            if let Ok(event) = result {
                handle_log_event(event, &app_handle, &positions_for_watcher);
            }
        },
        Config::default(),
    )?;
    watcher.watch(&dir, RecursiveMode::NonRecursive)?;
    app.manage(Mutex::new(watcher));
    Ok(())
}

#[tauri::command]
pub fn logs_open_in_finder() -> Result<(), String> {
    Command::new("open")
        .arg(log_dir())
        .status()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn logs_tail(lines: Option<usize>) -> Result<Vec<String>, String> {
    let limit = lines.unwrap_or(200);
    let dir = log_dir();
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.to_string()),
    };
    let mut candidates = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| is_jsonl(path))
        .collect::<Vec<_>>();
    candidates.sort_by_key(|path| fs::metadata(path).and_then(|m| m.modified()).ok());
    let Some(path) = candidates.pop() else {
        return Ok(Vec::new());
    };
    let file = match OpenOptions::new().read(true).open(&path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.to_string()),
    };
    let reader = BufReader::new(file);
    let mut rows = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|e| e.to_string())?;
        if !line.trim().is_empty() {
            rows.push(line);
        }
    }
    if rows.len() > limit {
        Ok(rows.split_off(rows.len() - limit))
    } else {
        Ok(rows)
    }
}

#[cfg(test)]
mod tests {
    use super::log_dir;

    #[test]
    fn log_dir_points_to_dataclaw_logs() {
        assert!(log_dir().ends_with(".dataclaw/logs"));
    }
}
