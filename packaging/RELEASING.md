# Nebula 3 desktop releases

Nebula 3 releases are tag-driven and never modify release source. Create a synchronized `nebula-v3.x.y` tag only after `python scripts/nebula3_version.py check --expected 3.x.y` passes.

The installed command contract is release-blocking: `nebula` launches the
native desktop, while `nebula-core` provides administration and diagnostics.
Promotion smoke tests must exercise both installed commands.

Release builds install the locked Core boundary with `poetry install --with
dev`; package and installer audits reject GUI bindings and heavy in-process
model stacks from the freezer environment.

The protected `desktop-release` GitHub environment must define:

- `APPLE_CERTIFICATE`: base64 PKCS#12 Developer ID Application certificate.
- `APPLE_CERTIFICATE_PASSWORD` and `APPLE_KEYCHAIN_PASSWORD`.
- `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID` for signing and notarization.
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for updater artifacts.
- `NEBULA_UPDATER_PUBLIC_KEY`, embedded into direct builds and backed up with the private key offline.

Tag pushes build signed direct and managed installers on native macOS arm64, macOS x64, and Ubuntu 22.04 x64 runners. A workflow dispatch may validate an existing tag without publishing. Publication always creates a draft GitHub Release; a release manager publishes it only after notarization, installer inspection, SBOM, provenance, and upgrade evidence are reviewed. Publishing triggers channel-specific updater manifest generation.
