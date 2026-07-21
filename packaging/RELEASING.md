# Nebula 3 desktop releases

Nebula 3 releases are tag-driven and never modify release source. The installed
command contract is release-blocking: `nebula` launches the native desktop,
while `nebula-core` provides administration and diagnostics. Promotion smoke
tests exercise both installed commands.

## Supported release matrix

The protected workflow produces these native packages:

| Platform | Architecture | Direct distribution | Managed distribution |
| --- | --- | --- | --- |
| macOS 13+ | Apple silicon | signed and notarized DMG with updater | signed and notarized DMG without updater |
| macOS 13+ | Intel | signed and notarized DMG with updater | signed and notarized DMG without updater |
| Linux | x86_64 | AppImage with signed updater metadata | DEB without the direct updater |

Windows, Linux arm64, RPM, Flatpak, Snap, and Homebrew artifacts are not built
by this workflow and must not be promised in release copy.

## One-time repository setup

Create a protected GitHub environment named `desktop-release`. Require release
manager approval and restrict deployment branches/tags according to the
repository policy. Define these environment secrets:

- `APPLE_CERTIFICATE`: base64 PKCS#12 Developer ID Application certificate.
- `APPLE_CERTIFICATE_PASSWORD` and `APPLE_KEYCHAIN_PASSWORD`.
- `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID`
  for signing and notarization.
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for
  updater artifacts.
- `NEBULA_UPDATER_PUBLIC_KEY`, embedded into direct builds and backed up with
  the private key offline.

The release cannot be prepared without this environment. Confirm the
`macos-15-intel` runner label is available to the repository before tagging.
Never use placeholder credentials for a release candidate. The preparation run
and manual finalization must complete before a draft release exists.

## Candidate checklist

1. Merge the release changes to `main` and wait for every required CI job to
   pass on the merge commit.
2. Pull `main` with a clean working tree and verify the candidate version:

   ```console
   git pull --ff-only origin main
   python scripts/nebula3_version.py check --expected 3.0.0-alpha.1
   test -s docs/releases/3.0.0-alpha.1.md
   ```

3. Review the checked-in notes. They are the exact GitHub Release body; do not
   describe an artifact, platform, migration, or security property that the
   workflow does not verify.
4. Run the portable release gates:

   ```console
   poetry install --with dev --no-interaction
   poetry run pytest -q tests/v3
   poetry run pytest -q packaging/updater/test_generate_manifest.py
   npm --prefix ui ci
   npm --prefix ui run audit:diagnostics
   npm --prefix ui test
   npm --prefix ui run build
   cargo test --locked --manifest-path ui/src-tauri/Cargo.toml
   ```

5. Create the immutable tag at the reviewed `main` commit and push it:

   ```console
   git tag -a nebula-v3.0.0-alpha.1 -m "Nebula 3.0.0-alpha.1"
   git push origin nebula-v3.0.0-alpha.1
   ```

The tag push starts the preparation workflow. It builds direct and managed
installers on native macOS arm64, macOS x64, and Ubuntu 22.04 x64 runners.
macOS builds are signed and submitted to Apple with Tauri's `--skip-stapling`
mode, so the runners do not wait for Apple to finish. Linux packages complete
their final audits, SBOMs, checksums, and attestations during preparation.

Record the successful preparation workflow run ID. After Apple has processed
the submissions, manually run **Finalize Nebula 3 desktop release** with the
immutable tag and preparation run ID:

```console
gh workflow run nebula3-release-finalize.yml \
  -f release_tag=nebula-v3.0.0-alpha.1 \
  -f preparation_run_id=123456789 \
  -f create_draft=true
```

The finalizer verifies that the preparation run succeeded at the exact tagged
commit and that all three private artifact sets still exist. If Apple is still
processing either macOS architecture, stapling fails safely and no draft is
created; rerun the finalizer later with the same inputs. Never resubmit or move
the tag merely because Apple is still processing it.

## Draft review and publication

Only a successful finalization creates a draft GitHub Release. Finalization
staples the submitted apps inside writable copies of the prepared DMGs, rebuilds
and signs the final DMGs, creates and signs the updater archives from the stapled
direct apps, repeats Gatekeeper and installed-layout checks, generates final
macOS SBOMs and checksums, attests the final bytes, and combines them with the
prepared Linux outputs. Before publishing the draft, a
release manager must verify:

- all three platform jobs and the clean Linux install matrix passed;
- the artifact names and counts match the workflow's immutable manifest;
- macOS signing, Gatekeeper assessment, notarization, and stapling passed;
- updater signatures accept the original artifact and reject the tampered test;
- both direct and managed installed layouts pass `nebula --self-test` and
  `nebula-core doctor --json`;
- CycloneDX and SPDX SBOMs, SHA-256 sums, and GitHub provenance attestations are
  present for every required deliverable;
- the version is marked as a prerelease when it contains a semantic-version
  prerelease suffix; and
- the checked-in release notes remain accurate after inspecting the artifacts.

Publishing the draft triggers channel-specific updater manifest generation on
GitHub Pages. Confirm that workflow succeeds and that the new manifest preserves
the existing website before announcing the release.

If a content or release gate fails, leave the draft unpublished, fix the problem
on a new commit, advance the version, and create a new tag. An Apple
`In Progress` result is not a content failure: wait and rerun finalization against
the same preparation run. Do not replace artifacts on an existing published
release or repoint its tag.
