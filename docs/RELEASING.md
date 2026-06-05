# Releasing DataClaw

1. Edit no version files by hand.
2. From an up-to-date `main`, run `make release VERSION=x.y.z`.
3. `scripts/release.py` verifies a clean tree on the default branch, checks that `x.y.z` is stable semver and greater than the current version, bumps `pyproject.toml`, syncs derived files, commits `release: vx.y.z`, creates annotated tag `vx.y.z`, and pushes `main` plus tags.
4. The tag push runs `.github/workflows/release.yml`; CI builds the macOS app, uploads assets, and creates the GitHub release with generated notes.
5. Watch https://github.com/peteromallet/dataclaw/actions/workflows/release.yml and verify the new assets at https://github.com/peteromallet/dataclaw/releases/latest.
6. In `release.yml`, checkout reads the tagged tree, the tag guard compares `github.ref_name` to `pyproject.toml`, then both signed and unsigned paths run `pnpm -C app tauri build`.
7. At that build, Tauri reads the app version from `app/src-tauri/Cargo.toml` because `tauri.conf.json` has no `version`; CI keeps Cargo synced to `pyproject.toml`, so the app bundle cannot drift from the tag.

Version derivation:

- `pyproject.toml` `[project].version` is the only hand-edited source of truth.
- `app/package.json` is rewritten by `python scripts/sync_version.py`.
- `app/src-tauri/Cargo.toml` is rewritten by `python scripts/sync_version.py`.
- `app/src-tauri/Cargo.lock` `dataclaw-app` entry is rewritten by `python scripts/sync_version.py`.
- `app/src-tauri/tauri.conf.json` intentionally has no `version`; Tauri v2 uses `Cargo.toml` when the config version is omitted.

Guards:

- PR CI runs `python scripts/sync_version.py --check` and fails if any derived file drifts or if `tauri.conf.json` grows a version field.
- Release CI fails before building if pushed tag `vx.y.z` does not match `pyproject.toml`.
- `README.md` download links point at `releases/latest`; `CHANGELOG.md` keeps historical release headings only.
- There is no `dataclaw.__version__`, CLI `--version`, or `pyinstaller.spec` version copy.
