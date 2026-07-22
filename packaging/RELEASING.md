# Nebula 3 desktop releases

Nebula 3 releases are tag-driven and never modify release source. The installed
command contract is release-blocking: `nebula` launches the native desktop,
while `nebula-core` provides administration and diagnostics. Promotion smoke
tests exercise both installed commands.

## Supported release matrix

The protected workflow currently produces Linux x86_64 packages only:

| Platform | Architecture | Direct distribution | Managed distribution |
| --- | --- | --- | --- |
| Linux | x86_64 | AppImage with signed updater metadata | DEB without the direct updater |

macOS, Windows, Linux arm64, RPM, Flatpak, Snap, and Homebrew artifacts are not
built by this workflow and must not be promised in release copy.

## One-time repository setup

Create a protected GitHub environment named `desktop-release`. Require release
manager approval and restrict deployment branches and tags according to the
repository policy. Define these environment secrets:

- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for the
  AppImage updater signature.
- `NEBULA_UPDATER_PUBLIC_KEY`, embedded into direct builds and backed up with
  the private key offline.

Never use placeholder credentials for a release candidate. Preparation and
manual draft creation must complete before a GitHub Release exists.

## Candidate checklist

1. Merge the release changes to `main` and wait for every required CI job to
   pass on the merge commit.
2. Pull `main` with a clean working tree and verify the candidate version:

   ```console
   git pull --ff-only origin main
   python scripts/nebula3_version.py check --expected 3.0.0-alpha.5
   test -s docs/releases/3.0.0-alpha.5.md
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
   git tag -a nebula-v3.0.0-alpha.5 -m "Nebula 3.0.0-alpha.5"
   git push origin nebula-v3.0.0-alpha.5
   ```

The tag push starts the preparation workflow on Ubuntu 22.04 x86_64. It builds
the direct AppImage and managed DEB, audits and self-tests both packages,
verifies the updater signature against a tampered copy, generates SBOMs and
checksums, creates GitHub provenance attestations, and runs clean DEB install
tests on Ubuntu 24.04, Debian 12, and Kali rolling containers.

Record the successful preparation workflow run ID. Then manually run **Create
Nebula 3 Linux release draft** with the immutable tag and preparation run ID:

```console
gh workflow run nebula3-release-finalize.yml \
  -f release_tag=nebula-v3.0.0-alpha.5 \
  -f preparation_run_id=123456789 \
  -f create_draft=true
```

The draft workflow verifies that the preparation run succeeded, that its
artifacts contain the exact tagged commit and version, and that the complete
Linux artifact, signature, SBOM, and checksum set still exists.

## Draft review and publication

Before publishing the draft, a release manager must verify:

- the Linux build and clean-install matrix passed;
- the artifact names and counts match the workflow's immutable manifest;
- updater signatures accept the original AppImage and reject the tampered test;
- the AppImage and DEB layouts pass `nebula --self-test` and
  `nebula-core doctor --json`;
- CycloneDX and SPDX SBOMs, SHA-256 sums, and GitHub provenance attestations are
  present for every required deliverable;
- the version is marked as a prerelease when it contains a semantic-version
  prerelease suffix; and
- the checked-in release notes remain accurate after inspecting the artifacts.

Publishing the draft triggers channel-specific Linux updater manifest
generation on GitHub Pages. Confirm that workflow succeeds and preserves the
existing website before announcing the release.

If the updater-manifest workflow itself needs a post-publication repair, merge
the workflow fix to `main` and recover the same immutable published release with:

```bash
gh workflow run publish-updater-manifest.yml \
  --ref main \
  -f release_tag=nebula-v3.0.0-alpha.5
```

The recovery path rejects drafts, reloads the published timestamp and channel
from GitHub, and revalidates checksums, attestations, and the updater signature.

If a content or release gate fails, leave the draft unpublished, fix the problem
on a new commit, advance the version, and create a new tag. Do not replace
artifacts on an existing published release or repoint its tag.
