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

The **Publish Safe Foundation tool packs** workflow targets the protected
`tool-pack-release` environment. `validate-source` remains read-only. `publish`
requires an immutable `nebula-tools-v*` tag and required-reviewer approval. It
then builds and signs architecture-specific OCI images, generates SBOMs and
provenance, signs the catalog with the environment-held Ed25519 key, and
deploys the verified catalog under the existing Nebula Pages site.

For the first release of each GHCR component, an organization owner must make
the new package public in GitHub's Package settings. GitHub does not provide a
public API for this visibility transition. The anonymous-pull gate must pass
before catalog signing or Pages deployment; rerun failed jobs after completing
the one-time visibility change. The failed job summary provides the five
package-settings links and the exact `gh run rerun <run-id> --failed` command.

Never commit the private key, resolved digests, or generated release evidence
to the source branch. Back up the release key offline before approving the
first publication. Key rotation requires an overlapping Nebula release that
trusts both the retiring and replacement public keys.
