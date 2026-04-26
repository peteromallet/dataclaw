import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_release_yml_uses_apple_signing_identity_var():
    text = read(".github/workflows/release.yml")
    assert "APPLE_SIGNING_IDENTITY" in text
    assert "DATACLAW_SIGNING_IDENTITY" not in text


def test_release_yml_maps_api_key_id_to_apple_api_key():
    text = read(".github/workflows/release.yml")
    assert re.search(
        r"APPLE_API_KEY:\s*\$\{\{\s*secrets\.APPLE_API_KEY_ID\s*\}\}",
        text,
        re.MULTILINE,
    )


def test_release_yml_has_required_secrets():
    text = read(".github/workflows/release.yml")
    required = {
        "APPLE_DEVELOPER_ID_CERT_P12_BASE64",
        "APPLE_DEVELOPER_ID_CERT_PASSWORD",
        "APPLE_API_KEY_P8_BASE64",
        "APPLE_API_KEY_ID",
        "APPLE_API_ISSUER",
        "APPLE_TEAM_ID",
        "APPLE_SIGNING_IDENTITY",
        "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
    }
    missing = sorted(secret for secret in required if secret not in text)
    assert missing == []


def test_release_yml_matrix_has_explicit_targets():
    text = read(".github/workflows/release.yml")
    assert re.search(
        r"os:\s*macos-14\s+target:\s*aarch64-apple-darwin\s+arch:\s*arm64",
        text,
        re.MULTILINE,
    )
    assert re.search(
        r"os:\s*macos-13\s+target:\s*x86_64-apple-darwin\s+arch:\s*x86_64",
        text,
        re.MULTILINE,
    )
    assert "pnpm -C app tauri build --target ${{ matrix.target }}" in text


def test_test_yml_unsigned_uses_empty_apple_signing_identity():
    text = read(".github/workflows/test.yml")
    assert "os: [macos-14, macos-13]" in text
    assert "pnpm -C app tauri build" in text
    assert 'APPLE_SIGNING_IDENTITY: ""' in text


def test_tauri_conf_has_updater_plugin():
    conf = json.loads(read("app/src-tauri/tauri.conf.json"))
    updater = conf["plugins"]["updater"]
    assert updater["active"] is True
    assert "https://github.com/banodoco/dataclaw/releases/latest/download/latest.json" in updater[
        "endpoints"
    ]
    assert updater["pubkey"]
    assert updater["pubkey"] != ""
    assert "${" not in updater["pubkey"]


def test_app_versions_match_python_package():
    pyproject = read("pyproject.toml")
    package_json = json.loads(read("app/package.json"))
    tauri_conf = json.loads(read("app/src-tauri/tauri.conf.json"))
    cargo_toml = read("app/src-tauri/Cargo.toml")

    py_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)
    cargo_version = re.search(r'^version\s*=\s*"([^"]+)"', cargo_toml, re.MULTILINE).group(1)

    assert package_json["version"] == py_version
    assert tauri_conf["version"] == py_version
    assert cargo_version == py_version


def test_tauri_conf_creates_updater_artifacts():
    conf = json.loads(read("app/src-tauri/tauri.conf.json"))
    assert conf["bundle"]["createUpdaterArtifacts"] is True


def test_tauri_conf_macos_signing_block_present():
    conf = json.loads(read("app/src-tauri/tauri.conf.json"))
    macos = conf["bundle"]["macOS"]
    assert macos["signingIdentity"] == "${APPLE_SIGNING_IDENTITY-}"
    assert macos["entitlements"] == "entitlements.plist"
    assert macos["hardenedRuntime"] is True
    assert macos["providerShortName"] == "${APPLE_TEAM_ID-}"


def test_capabilities_grants_updater_default():
    capabilities = json.loads(read("app/src-tauri/capabilities/default.json"))
    assert "updater:default" in capabilities["permissions"]


def test_entitlements_disables_library_validation_for_pyinstaller_sidecar():
    root = ET.fromstring(read("app/src-tauri/entitlements.plist"))
    dict_node = root.find("dict")
    assert dict_node is not None
    children = list(dict_node)
    for index, child in enumerate(children[:-1]):
        if child.tag == "key" and child.text == "com.apple.security.cs.disable-library-validation":
            assert children[index + 1].tag == "true"
            return
    raise AssertionError("disable-library-validation entitlement not found")
