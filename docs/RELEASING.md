# Releasing DataClaw

This runbook covers the manual Apple setup, GitHub Secrets, updater signing keys, tagged release flow, and post-release checks for signed and notarized macOS releases.

## 1. Enroll in Apple Developer Program

1. Join the Apple Developer Program for the account or organization that owns the release.
2. In Apple Developer, create a `Developer ID Application` certificate.
3. Install the certificate in Keychain Access on the Mac that will export it.
4. Export the certificate and private key as a password-protected `.p12` file.

```sh
# Keychain Access UI:
# My Certificates -> Developer ID Application: <Team Name> -> Export...
# Save as cert.p12 and set a strong export password.
```

5. Record the exact signing identity string shown by `security find-identity`.

```sh
security find-identity -v -p codesigning
```

Use the full `Developer ID Application: ... (TEAMID)` value as the GitHub Secret `APPLE_SIGNING_IDENTITY`.

## 2. Create App Store Connect API key

1. Open App Store Connect.
2. Go to `Users and Access` -> `Integrations` -> `App Store Connect API`.
3. Create an API key with access that can notarize Developer ID applications.
4. Download the `.p8` key file once. Apple will not let you download it again.
5. Record:
   - Key ID -> GitHub Secret `APPLE_API_KEY_ID`
   - Issuer ID -> GitHub Secret `APPLE_API_ISSUER`
   - Team ID -> GitHub Secret `APPLE_TEAM_ID`

## 3. Generate the updater ed25519 keypair

Generate the Tauri updater signing key outside the repository.

```sh
mkdir -p ~/.tauri
pnpm -C app tauri signer generate -w ~/.tauri/dataclaw.key
```

The command prints a public key and writes the private key file to `~/.tauri/dataclaw.key`.

1. Copy the printed `Public key:` value into `app/src-tauri/tauri.conf.json` at `plugins.updater.pubkey`.
2. Commit the public key in `tauri.conf.json`.
3. Never commit `~/.tauri/dataclaw.key`.
4. Store the private key file contents as GitHub Secret `TAURI_SIGNING_PRIVATE_KEY`.
5. Store the private key password printed by the command as GitHub Secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.

```sh
pbcopy < ~/.tauri/dataclaw.key
# Paste into GitHub Secret TAURI_SIGNING_PRIVATE_KEY.

# Copy the password printed by `tauri signer generate`.
# Paste it into GitHub Secret TAURI_SIGNING_PRIVATE_KEY_PASSWORD.
```

## 4. Base64 encode Apple secrets

GitHub Actions receives the certificate and API key as base64-encoded strings.

```sh
base64 -i cert.p12 -o cert.p12.b64
base64 -i AuthKey_ABC123DEFG.p8 -o AuthKey_ABC123DEFG.p8.b64

pbcopy < cert.p12.b64
# Paste into GitHub Secret APPLE_DEVELOPER_ID_CERT_P12_BASE64.

pbcopy < AuthKey_ABC123DEFG.p8.b64
# Paste into GitHub Secret APPLE_API_KEY_P8_BASE64.
```

## 5. Configure GitHub Secrets

Create these repository secrets before pushing a release tag.

| GitHub Secret | Used as | Description |
| --- | --- | --- |
| `APPLE_DEVELOPER_ID_CERT_P12_BASE64` | Certificate import input | Base64 contents of the exported Developer ID Application `.p12`. |
| `APPLE_DEVELOPER_ID_CERT_PASSWORD` | Certificate import input | Password used when exporting the `.p12`. |
| `APPLE_API_KEY_P8_BASE64` | Notarization API key file input | Base64 contents of the App Store Connect `.p8` key. |
| `APPLE_API_KEY_ID` | Tauri env var `APPLE_API_KEY` | Apple Key ID. The secret keeps Apple's name, but `release.yml` must rename it to env var `APPLE_API_KEY` because Tauri reads `APPLE_API_KEY`. |
| `APPLE_API_ISSUER` | Tauri env var `APPLE_API_ISSUER` | App Store Connect Issuer ID. |
| `APPLE_TEAM_ID` | Tauri env var `APPLE_TEAM_ID` | Apple Developer Team ID. |
| `APPLE_SIGNING_IDENTITY` | Tauri env var `APPLE_SIGNING_IDENTITY` | Full `Developer ID Application: ... (TEAMID)` signing identity. |
| `TAURI_SIGNING_PRIVATE_KEY` | Tauri updater signing key | Contents of `~/.tauri/dataclaw.key`. Never commit this. |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Tauri updater signing password | Password printed by `pnpm -C app tauri signer generate`. Never commit this. |

## 6. Cut a signed release

1. Confirm local unsigned builds still work before tagging.

```sh
APPLE_SIGNING_IDENTITY="" pnpm -C app tauri build
```

2. Create and push a release tag.

```sh
git tag v0.4.0
git push --tags
```

3. GitHub Actions runs `.github/workflows/release.yml`.
4. The release workflow builds on `macos-14` for `aarch64-apple-darwin` and `macos-13` for `x86_64-apple-darwin`.
5. Signing and notarization happen only for tagged releases. Pull requests run unsigned builds.
6. The workflow uploads the `.dmg`, `.app.tar.gz`, `.app.tar.gz.sig`, and signed `latest.json` release assets.

## 7. Verify the release manually

1. Download the released `.dmg` from GitHub Releases.
2. Install `DataClaw.app` into `/Applications`.
3. Verify Gatekeeper accepts the app as notarized.

```sh
spctl --assess --type execute /Applications/DataClaw.app
```

Expected output:

```text
/Applications/DataClaw.app: accepted
source=Notarized Developer ID
```

4. Launch the app and check for an update from a previously installed signed version before announcing the release.

## 8. Rotate the updater signing key

Use this when the updater private key or password may have been exposed, or when rotating credentials on a fixed schedule.

1. Preserve the old updater private key and password until the cutover release has reached users. Existing installed apps can only verify updates signed by the old public key.
2. Generate a new keypair outside the repository.

```sh
pnpm -C app tauri signer generate -w ~/.tauri/dataclaw-rotated.key
```

3. Replace `plugins.updater.pubkey` in `app/src-tauri/tauri.conf.json` with the new printed public key and commit it.
4. Update GitHub Secrets for future releases:

```sh
pbcopy < ~/.tauri/dataclaw-rotated.key
# Paste into GitHub Secret TAURI_SIGNING_PRIVATE_KEY.

# Paste the new printed password into GitHub Secret TAURI_SIGNING_PRIVATE_KEY_PASSWORD.
```

5. Sign the cutover release with both old and new updater keys:
   - Publish the normal `latest.json` signed with the old key so currently installed apps can install the cutover build.
   - Also produce and retain a new-key-signed updater manifest for verification against the committed new public key.
6. After the cutover build is installed, those apps contain the new public key and can verify future releases signed only by the new key.
7. Remove the old key from operational use only after the cutover window is complete.

