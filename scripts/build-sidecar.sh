#!/usr/bin/env bash
set -euo pipefail

ARCH="${1:-$(uname -m)}"
PYTHON_BIN="${PYTHON:-python}"

"${PYTHON_BIN}" -m PyInstaller pyinstaller.spec --clean --noconfirm

case "${ARCH}" in
  arm64|aarch64)
    TRIPLE="aarch64-apple-darwin"
    ;;
  x86_64)
    TRIPLE="x86_64-apple-darwin"
    ;;
  *)
    echo "Unsupported architecture: ${ARCH}" >&2
    exit 2
    ;;
esac

mkdir -p app/src-tauri/binaries
cp dist/dataclaw "app/src-tauri/binaries/dataclaw-${TRIPLE}"
codesign --force --sign - "app/src-tauri/binaries/dataclaw-${TRIPLE}"

SIDECAR_PATH="app/src-tauri/binaries/dataclaw-${TRIPLE}" "${PYTHON_BIN}" - <<'PY'
import os
import subprocess

subprocess.run(
    [os.environ["SIDECAR_PATH"], "status"],
    check=True,
    timeout=15,
)
PY
