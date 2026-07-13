# Nebula 3 desktop releases

Nebula 3 releases are tag-driven and never modify release source. Create a synchronized `nebula-v3.x.y` tag only after `python scripts/nebula3_version.py check --expected 3.x.y` passes.

Release builds install the locked Core boundary with `poetry install --without
legacy,legacy-dev --with dev`; neither the PyQt application nor its pytest
plugin is present in the freezer environment.

The protected `desktop-release` GitHub environment must define:

- `APPLE_CERTIFICATE`: base64 PKCS#12 Developer ID Application certificate.
- `APPLE_CERTIFICATE_PASSWORD` and `APPLE_KEYCHAIN_PASSWORD`.
- `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID` for signing and notarization.
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for updater artifacts.
- `NEBULA_UPDATER_PUBLIC_KEY`, embedded into direct builds and backed up with the private key offline.

Tag pushes build signed direct and managed installers on native macOS arm64, macOS x64, and Ubuntu 22.04 x64 runners. A workflow dispatch may validate an existing tag without publishing. Publication always creates a draft GitHub Release; a release manager publishes it only after notarization, installer inspection, SBOM, provenance, and upgrade evidence are reviewed. Publishing triggers channel-specific updater manifest generation.

## Tool-pack release inputs

Tool packs have a separate manual, protected release boundary. See the
[tool-pack operator and author guide](../docs/TOOL_PACKS.md) before preparing
one.

The **Tool-pack publication readiness** workflow targets the
`tool-pack-release` environment with read-only repository permissions.
Repository administrators must configure that environment with required
reviewers; the workflow file cannot create its protection rules. Source
validation reports unresolved placeholders without manufacturing values. Its
candidate mode requires an immutable `nebula-tools-v*` tag, exact image
digests, and the declared SBOM/provenance files, then emits unsigned archives
for offline review only.

That workflow does not push OCI images, sign manifests/catalogs, or publish a
catalog. Do not add placeholder keys or digests to make it pass. Enabling real
publication requires a separately reviewed offline Ed25519 signing ceremony,
key-distribution and rotation procedure, immutable hosting, provenance policy,
and clean-machine install verification.
