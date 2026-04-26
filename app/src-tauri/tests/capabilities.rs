use std::fs;

use serde_json::Value;

#[test]
fn capabilities_file_grants_shell_sidecar_for_dataclaw() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("capabilities/default.json");
    let text = fs::read_to_string(path).unwrap();
    let value: Value = serde_json::from_str(&text).unwrap();
    let permissions = value["permissions"].as_array().unwrap();
    let spawn = permissions
        .iter()
        .find(|permission| {
            permission.get("identifier").and_then(Value::as_str) == Some("shell:allow-spawn")
        })
        .unwrap();
    let allow = spawn["allow"].as_array().unwrap();
    assert!(allow.iter().any(|entry| {
        entry.get("name").and_then(Value::as_str) == Some("dataclaw")
            && entry.get("sidecar").and_then(Value::as_bool) == Some(true)
    }));
}
