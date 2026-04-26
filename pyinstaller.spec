# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


hiddenimports = (
    collect_submodules("keyring")
    + collect_submodules("huggingface_hub")
    + collect_submodules("dataclaw")
    + [
        "dataclaw",
        "dataclaw.parser",
        "dataclaw.secrets",
        "dataclaw.anonymizer",
        "dataclaw.privacy_filter",
        "dataclaw.logging",
        "dataclaw.auth",
        "dataclaw.scheduler",
        "dataclaw.cli",
        "dataclaw.config",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.fail",
        "secretstorage",
        "jeepney",
    ]
)
datas = collect_data_files("huggingface_hub")

a = Analysis(
    ["scripts/sidecar_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dataclaw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
