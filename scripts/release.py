#!/usr/bin/env python3
"""Cut a DataClaw release from the default branch."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

import sync_version

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSIONED_FILES = (
    PYPROJECT,
    sync_version.PACKAGE_JSON,
    sync_version.CARGO_TOML,
    sync_version.CARGO_LOCK,
    sync_version.TAURI_CONF,
)


class ReleaseError(RuntimeError):
    pass


def run_git(*args: str, capture: bool = True) -> str:
    result = subprocess.run(  # nosec B603
        ["git", *args],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else ""
        raise ReleaseError(f"git {' '.join(args)} failed" + (f": {detail}" if detail else ""))
    return result.stdout.strip() if capture else ""


def parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ReleaseError(f"Invalid version {version!r}. Use stable semver like 0.5.0.")
    return tuple(int(part) for part in match.groups())


def default_branch() -> str:
    ref = run_git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if not ref.startswith("origin/"):
        raise ReleaseError(f"Could not determine default branch from origin/HEAD: {ref!r}")
    return ref.removeprefix("origin/")


def current_branch() -> str:
    return run_git("branch", "--show-current")


def ensure_clean_tree() -> None:
    status = run_git("status", "--porcelain")
    if status:
        raise ReleaseError("Working tree is dirty. Commit or stash changes before releasing.")


def ensure_default_branch() -> str:
    expected = default_branch()
    actual = current_branch()
    if actual != expected:
        raise ReleaseError(f"Release must run on default branch {expected!r}; current branch is {actual!r}.")
    return expected


def ensure_tag_available(tag: str) -> None:
    if run_git("tag", "--list", tag):
        raise ReleaseError(f"Tag {tag} already exists.")


def replace_pyproject_version(new_version: str) -> str:
    old_text = PYPROJECT.read_text()
    PYPROJECT.write_text(replace_project_version(old_text, new_version))
    return old_text


def replace_project_version(text: str, new_version: str) -> str:
    updated, count = re.subn(
        r'(?m)^version\s*=\s*"([^"]+)"',
        f'version = "{new_version}"',
        text,
        count=1,
    )
    if count != 1:
        raise ReleaseError("Could not update pyproject.toml [project] version.")
    return updated


def render_synced_file(path: Path, new_version: str) -> str:
    text = path.read_text()
    if path == PYPROJECT:
        return replace_project_version(text, new_version)
    if path == sync_version.PACKAGE_JSON:
        data = json.loads(text)
        data["version"] = new_version
        return json.dumps(data, indent=2) + "\n"
    if path == sync_version.CARGO_TOML:
        return re.sub(r'(?m)^version\s*=\s*"([^"]+)"', f'version = "{new_version}"', text, count=1)
    if path == sync_version.CARGO_LOCK:
        return re.sub(
            r'(\[\[package\]\]\nname = "dataclaw-app"\nversion = )"([^"]+)"',
            rf'\1"{new_version}"',
            text,
            count=1,
        )
    if path == sync_version.TAURI_CONF:
        data = json.loads(text)
        data.pop("version", None)
        return json.dumps(data, indent=2) + "\n"
    raise ReleaseError(f"Unsupported dry-run path: {path}")


def diff_for_dry_run(new_version: str) -> str:
    chunks: list[str] = []
    for path in VERSIONED_FILES:
        old_text = path.read_text()
        new_text = render_synced_file(path, new_version)
        if old_text == new_text:
            continue
        rel = path.relative_to(ROOT).as_posix()
        chunks.append(
            "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=rel,
                    tofile=rel,
                )
            )
        )
    return "".join(chunks)


def validate_new_version(new_version: str) -> str:
    current = sync_version.read_pyproject_version()
    if parse_semver(new_version) <= parse_semver(current):
        raise ReleaseError(f"New version {new_version} must be strictly greater than current version {current}.")
    return current


def print_next_steps(tag: str, *, dry_run: bool = False) -> None:
    print()
    if dry_run:
        print("After the real release push:")
    else:
        print("Release queued.")
    print("GitHub Actions will build the macOS app and publish release assets from the tag push.")
    print(f"Actions: https://github.com/peteromallet/dataclaw/actions/workflows/release.yml?query=branch%3A{tag}")
    print(f"Release: https://github.com/peteromallet/dataclaw/releases/tag/{tag}")


def dry_run(new_version: str) -> int:
    current = validate_new_version(new_version)
    tag = f"v{new_version}"
    branch = current_branch()
    expected = default_branch()
    dirty = bool(run_git("status", "--porcelain"))

    print(f"Dry run release {tag}")
    print(f"Current version: {current}")
    print(f"Would require clean working tree: {'FAILED now' if dirty else 'ok now'}")
    print(f"Would require default branch {expected!r}: {'ok now' if branch == expected else f'current branch is {branch!r}'}")
    print(f"Would verify tag is unused: {tag}")
    print("Would update pyproject.toml, then run: python scripts/sync_version.py")
    diff = diff_for_dry_run(new_version)
    if diff:
        print()
        print(diff, end="")
    print(f"Would commit: release: {tag}")
    print(f"Would create annotated tag: {tag}")
    print(f"Would push: git push origin {expected} --follow-tags")
    print_next_steps(tag, dry_run=True)
    return 0


def release(new_version: str) -> int:
    current = validate_new_version(new_version)
    tag = f"v{new_version}"
    branch = ensure_default_branch()
    ensure_clean_tree()
    ensure_tag_available(tag)

    print(f"Releasing {tag} from {branch} (current version {current})")
    replace_pyproject_version(new_version)
    sync_changes = sync_version.sync()
    for change in sync_changes:
        print(f"  - {change}")
    run_git("add", "pyproject.toml", "app/package.json", "app/src-tauri/Cargo.toml", "app/src-tauri/Cargo.lock", "app/src-tauri/tauri.conf.json")
    run_git("commit", "-m", f"release: {tag}", capture=False)
    run_git("tag", "-a", tag, "-m", f"release: {tag}", capture=False)
    run_git("push", "origin", branch, "--follow-tags", capture=False)
    print_next_steps(tag)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump, tag, and push a DataClaw release")
    parser.add_argument("version", help="New release version, for example 0.5.0")
    parser.add_argument("--dry-run", action="store_true", help="Print planned steps without changing files")
    args = parser.parse_args()

    try:
        if args.dry_run:
            return dry_run(args.version)
        return release(args.version)
    except ReleaseError as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
