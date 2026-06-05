#!/usr/bin/env python3
"""Sync derived package versions from pyproject.toml."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "app/package.json"
CARGO_TOML = ROOT / "app/src-tauri/Cargo.toml"
CARGO_LOCK = ROOT / "app/src-tauri/Cargo.lock"
TAURI_CONF = ROOT / "app/src-tauri/tauri.conf.json"


class VersionError(RuntimeError):
    pass


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r'(?m)^\[project\][\s\S]*?^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise VersionError(f"Could not find [project] version in {relative(PYPROJECT)}")
    return match.group(1)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def update_package_json(version: str) -> tuple[bool, str | None]:
    data = json.loads(PACKAGE_JSON.read_text())
    current = data.get("version")
    if current == version:
        return False, None
    data["version"] = version
    PACKAGE_JSON.write_text(json.dumps(data, indent=2) + "\n")
    return True, f"{relative(PACKAGE_JSON)}: {current!r} -> {version!r}"


def update_cargo_toml(version: str) -> tuple[bool, str | None]:
    text = CARGO_TOML.read_text()
    updated, count = re.subn(
        r'(?m)^version\s*=\s*"([^"]+)"',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise VersionError(f"Could not find package version in {relative(CARGO_TOML)}")
    if updated == text:
        return False, None
    old = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1)
    CARGO_TOML.write_text(updated)
    return True, f"{relative(CARGO_TOML)}: {old!r} -> {version!r}"


def update_cargo_lock(version: str) -> tuple[bool, str | None]:
    text = CARGO_LOCK.read_text()
    pattern = re.compile(r'(\[\[package\]\]\nname = "dataclaw-app"\nversion = )"([^"]+)"')
    match = pattern.search(text)
    if not match:
        raise VersionError(f"Could not find dataclaw-app package in {relative(CARGO_LOCK)}")
    current = match.group(2)
    if current == version:
        return False, None
    updated = pattern.sub(rf'\1"{version}"', text, count=1)
    CARGO_LOCK.write_text(updated)
    return True, f"{relative(CARGO_LOCK)}: {current!r} -> {version!r}"


def update_tauri_conf() -> tuple[bool, str | None]:
    data = json.loads(TAURI_CONF.read_text())
    if "version" not in data:
        return False, None
    current = data.pop("version")
    TAURI_CONF.write_text(json.dumps(data, indent=2) + "\n")
    return True, f"{relative(TAURI_CONF)}: removed version {current!r}; Tauri derives it from Cargo.toml"


def collect_mismatches(version: str) -> list[str]:
    mismatches: list[str] = []

    package_version = json.loads(PACKAGE_JSON.read_text()).get("version")
    if package_version != version:
        mismatches.append(f"{relative(PACKAGE_JSON)} has {package_version!r}, expected {version!r}")

    cargo_text = CARGO_TOML.read_text()
    cargo_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo_text)
    cargo_version = cargo_match.group(1) if cargo_match else None
    if cargo_version != version:
        mismatches.append(f"{relative(CARGO_TOML)} has {cargo_version!r}, expected {version!r}")

    lock_text = CARGO_LOCK.read_text()
    lock_match = re.search(r'\[\[package\]\]\nname = "dataclaw-app"\nversion = "([^"]+)"', lock_text)
    lock_version = lock_match.group(1) if lock_match else None
    if lock_version != version:
        mismatches.append(f"{relative(CARGO_LOCK)} has dataclaw-app {lock_version!r}, expected {version!r}")

    tauri_conf = json.loads(TAURI_CONF.read_text())
    if "version" in tauri_conf:
        mismatches.append(
            f"{relative(TAURI_CONF)} still has a version field; remove it so Tauri derives from Cargo.toml"
        )

    return mismatches


def sync() -> list[str]:
    version = read_pyproject_version()
    changes = []
    for changed, message in (
        update_package_json(version),
        update_cargo_toml(version),
        update_cargo_lock(version),
        update_tauri_conf(),
    ):
        if changed and message:
            changes.append(message)
    return changes


def check() -> int:
    version = read_pyproject_version()
    mismatches = collect_mismatches(version)
    if not mismatches:
        print(f"Version check passed: {version}")
        return 0

    print(f"Version check failed: pyproject.toml is {version!r}, but derived files drifted.", file=sys.stderr)
    for mismatch in mismatches:
        print(f"  - {mismatch}", file=sys.stderr)
    print("Run `python scripts/sync_version.py` to update derived version files.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync derived DataClaw version files from pyproject.toml")
    parser.add_argument("--check", action="store_true", help="Fail if derived files do not match pyproject.toml")
    args = parser.parse_args()

    try:
        if args.check:
            return check()
        changes = sync()
    except (OSError, VersionError, json.JSONDecodeError) as exc:
        print(f"version sync failed: {exc}", file=sys.stderr)
        return 1

    if changes:
        print("Synced version files:")
        for change in changes:
            print(f"  - {change}")
    else:
        print(f"Version files already synced: {read_pyproject_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
