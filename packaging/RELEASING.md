# Nebula 3 desktop releases

Nebula 3 releases are tag-driven and never modify release source. The installed
command contract is release-blocking: `nebula` launches the native desktop,
while `nebula-core` provides administration and diagnostics. Promotion smoke
tests exercise both installed commands.

## Daily stable publication

`daily-stable-release.yml` is currently disabled pending a separately authorized
release candidate and schedule resumption. When enabled, it checks `main` every
day at 9:00 AM `America/New_York`. It skips when no commits have landed since the source commit
of the latest published Nebula 3 release. A manual dispatch runs the same gate.
It requires the exact main commit's CI run to have passed before it acts.

For a changed, green main, the job makes a release-only commit containing the
next stable patch version and a changelog of commit subjects. The first stable
release after a prerelease keeps its base version, for example
`3.0.0-beta.3` becomes `3.0.0`; later stable releases advance the patch.
The release-only commit records both main and the prior release in its ancestry,
so release ancestry stays intact without changing main.
It pushes only an immutable tag, then dispatches the existing preparation and
draft workflows. The release driver checks the one installed-Core smoke receipt,
checksums, and provenance attestations before publishing the stable GitHub
Release. It explicitly dispatches updater metadata publication because releases
made by `GITHUB_TOKEN` do not recursively trigger another workflow.

A dedicated, organization-owned GitHub App authenticates only identity
preflight, the tag push and the driver's three workflow dispatches. Its
installation is restricted to `berylliumsec/nebula`, with Contents write, Actions
write and implicit Metadata read. The driver verifies the App ID, slug, owner,
installation, bot ID, permissions and each issued token's single-repository scope
before use. It mints a fresh token immediately before each operation, narrowed
to either Contents write or Actions write, and revokes it afterward, including
on failure. Installation tokens expire after one hour; no token survives across
the driver's multi-hour workflow waits.

An unprivileged job still verifies the human initiating actor's repository admin
permission before the protected job can access the App key. The daily job accepts
only `main`. Downstream gates retain their human-admin path and additionally accept
only `workflow_dispatch` from the configured App's exact bot ID, sender ID, Bot
type and slug. A different rerun initiator must be an admin. An unset identity,
unrelated bot, tag push by the App or authorization denial fails closed.

Checkout uses its ordinary job token with credential persistence disabled. The
App token is injected into Git's temporary credential helper only for the push.
Reads, version tools, artifact downloads, checksum/provenance
verification and draft publication use the job token. Ordinary child processes
never inherit the App key or an App token; the signing subprocess has only a
private temporary key file, removed before any release operation. No PAT fallback
is supported. Revocation failure stops the driver for review.

The generated release-only commit includes `[skip ci]` to avoid a second tag-push
preparation beside the explicit dispatch. This commit never changes main.
Explicit `workflow_dispatch` still runs preparation, signing, package
smoke checks, and draft validation, and the driver must verify their successful
runs and receipts. Ordinary release tag pushes retain their existing trigger.
The final draft publication uses `GITHUB_TOKEN` to suppress a duplicate
release-event updater run. The
updater dispatch uses a fresh Actions-only App token and its exact-identity gate.

Preparation has exactly three jobs: validate the immutable green-main candidate,
build the managed DEB and signed direct AppImage, and install the DEB in one clean
Ubuntu 24.04 container. The Ubuntu job launches the installed Core and bundled
Chromium, then runs one desktop Chromium case against the Core's embedded production
UI on a disposable LAN origin. It pairs, creates a project, sends one request to an
inert local model, and checks that the project, session, and answer survive reload.
Core and UI must identify the tagged commit. A receipt binds that passing case to
the tag, commit, and installed DEB SHA-256. Draft creation and daily publication
both reject a missing, failed, or mismatched receipt.

Packaging unit tests, redundant compile checks, sandbox integration, crash cleanup,
negative signature checks, and Debian/Kali installs are available in the optional
`nebula3-release-diagnostics.yml` manual workflow. The existing manually selected
Playwright workflow retains its explicit full-suite approval guard. Focused PR
selection remains separate; the release gate claims only this install and core
journey. Current main branch protection does not require the PR Playwright check,
so the exact release smoke is the independent browser check before publication.

Any failed check before publication stops publication. An existing release tag or draft is a
recovery case for a release manager; the daily job never moves a tag or replaces
an asset. GitHub may delay or drop scheduled runs under load, so the manual
dispatch remains available. APT promotion is a separate protected workflow in
`BerylliumSec/nebula-apt` and is not performed by this schedule.

The driver deliberately does not retry mutations or resume partially completed
releases automatically. A tag push or dispatch can succeed remotely even when
its response is lost. After tag creation, a failure or cancellation leaves that
immutable tag (and possibly a draft) reserved for release-manager recovery;
subsequent daily runs stop rather than retagging or replacing assets. Since the
release commit has `[skip ci]`, a failed preparation dispatch requires an approved
manual preparation on the same tag on that immutable tag.
Recover finalization using its verified successful preparation run. A publication
response failure requires reading actual release state before any next action.
If updater dispatch or execution fails after publication, the release stays
published: authorize `publish-updater-manifest.yml` on `main` with that same
`release_tag`. A no-new-commits daily run skips; it does not repair updater state.

## Supported release matrix

The protected workflow currently produces Linux x86_64 packages only:

| Platform | Architecture | Direct distribution | Managed distribution |
| --- | --- | --- | --- |
| Linux | x86_64 | AppImage with signed updater metadata | DEB without the direct updater |

macOS, Windows, Linux arm64, RPM, Flatpak, Snap, and Homebrew artifacts are not
built by this workflow and must not be promised in release copy.

Use `.agents/skills/release-nebula-apt/SKILL.md` when promoting a published DEB
into the signed APT repository.

## One-time repository setup

Create a GitHub environment named `desktop-release` and restrict deployment
branches and tags according to the repository policy. Release tags are
restricted to repository admins or the configured release App, and both
preparation and draft workflows verify that identity before reaching this
environment. The daily initiating actor must still be a repository admin.
Define these values only after explicit setup approval:

- Register a private App owned by `berylliumsec`, proposed name
  `berylliumsec-nebula-release` (availability must be verified in the form), home
  page `https://github.com/berylliumsec/nebula`. Disable webhooks; configure no
  callbacks, user authorization, events or organization/user permissions.
  Request repository Contents write and Actions write only, with Metadata read
  implicit. Install it with Selected repositories: `nebula` only.
- Repository variables `NEBULA_RELEASE_APP_ID`,
  `NEBULA_RELEASE_APP_CLIENT_ID`, `NEBULA_RELEASE_APP_INSTALLATION_ID`,
  `NEBULA_RELEASE_APP_SLUG` and `NEBULA_RELEASE_APP_BOT_ID` must contain the
  verified registration/installation identities. Repository variables are needed
  by the unprivileged downstream actor checks; they are not credentials.
- The user must generate a private key and enter/save it directly as the
  `desktop-release` environment secret `NEBULA_RELEASE_APP_PRIVATE_KEY`.
  Never place the key in chat, Git, artifacts or repository-wide secrets.
- A separately approved change adds exactly this App's Integration/App ID to
  creation ruleset `24663353` (Admin-only Nebula 3 release tags), in Always bypass
  mode. Its only rule must remain Creation, scoped to `refs/tags/nebula-v3.*`.
  Keep the existing admin-role bypass. Ruleset `24663368` (Immutable Nebula 3
  release tags) must retain Update and Deletion restrictions with no bypasses.
  Do not add the App to any broader ruleset or grant it Administration permission.

Workflows write is unnecessary: the generated release commit retains main's
already-present workflow files, and the driver rejects workflow changes before
creating a tag. Attestations read remains on `GITHUB_TOKEN`; no App attestation
permission is needed. No Secrets, Variables, Administration, organization or user
permission is needed by the App. Stop and review any denial instead of adding
permissions. Registration, installation, creation-rule changes, key generation
and secret entry require their separately approved setup actions; deployment or a
publishing workflow dispatch still needs explicit approval after code/setup review.
The App's Contents/Actions grants are repository-wide capabilities; the token
scope and existing branch/ruleset/workflow gates restrict the intended path.
Protect and rotate the private key; revoke old keys after validation.

Read-only inspection on 2026-10-09 confirmed `desktop-release` exists with allowed
branch `main` and allowed tags `nebula-v3.*`. Its returned protection rules
contain only the branch policy: no required reviewers or wait timer conflict
with the schedule. No environment policy changes are required by this fix.

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
   version="$(cat NEBULA3_VERSION)"
   release_tag="nebula-v${version}"
   python scripts/nebula3_version.py check --expected "$version"
   test -s "docs/releases/${version}.md"
   ```

3. Review the checked-in notes. They are the exact GitHub Release body; do not
   describe an artifact, platform, migration, or security property that the
   workflow does not verify.
4. Review the diff-bound PR CI test selection and its receipt. Invoke optional
   release diagnostics only for a specific unresolved risk; they are not a
   preparation prerequisite.

5. Create the immutable tag at the reviewed `main` commit and push it:

   ```console
   version="$(cat NEBULA3_VERSION)"
   release_tag="nebula-v${version}"
   git tag -a "$release_tag" -m "Nebula $version"
   git push origin "$release_tag"
   ```

The tag push starts the three-job preparation workflow. It builds the direct
AppImage and managed DEB, checks versions, required contents, the positive AppImage
signature, checksums, SBOMs, and provenance, then installs the DEB in Ubuntu 24.04
and runs exactly one packaged-Core Chromium journey. Its
`release-core-smoke-<tag>` artifact contains `release-core-smoke.json` with the
case name, tag, tagged commit, installed DEB SHA-256, selected count, retries, and
result. Review this receipt and the native artifacts before finalization. A failed
or missing smoke blocks draft creation and publication.

The optional **Optional Nebula 3 release diagnostics** workflow is manual only.
Select `source`, `sandbox`, `installers`, `artifact`, or `all`; installer and
artifact diagnostics require the successful preparation run ID. The separate
**Change-aware Playwright matrix** manual selector keeps its explicit full-suite
approval guard. These diagnostics do not gate release preparation. The daily
schedule remains disabled until a later authorized candidate passes and schedule
resumption is separately approved.

Record the successful preparation workflow run ID. Then manually run **Create
Nebula 3 Linux release draft** with the immutable tag and preparation run ID:

```console
release_tag="nebula-v$(cat NEBULA3_VERSION)"
gh workflow run nebula3-release-finalize.yml \
  -f release_tag="$release_tag" \
  -f preparation_run_id=123456789 \
  -f create_draft=true
```

The draft workflow verifies that the preparation run succeeded, that its
artifacts contain the exact tagged commit and version, and that the complete
Linux artifact, signature, SBOM, and checksum set still exists.

## Draft review and publication

Before publishing the draft, a release manager must verify:

- the exact installed-DEB core-smoke receipt passed and matches the tag, commit, and DEB hash;
- the three preparation jobs passed, including one Ubuntu 24.04 clean install;
- the artifact names and counts match the workflow's immutable manifest;
- the original AppImage updater signature verifies; the negative tamper test is optional manual diagnostics;
- the AppImage and DEB layouts pass `nebula --self-test` and
  `nebula-core doctor --json`;
- the bundled Playwright Chromium headless shell launches in the Ubuntu install;
- CycloneDX and SPDX SBOMs, SHA-256 sums, and GitHub provenance attestations are
  present for every required deliverable;
- the version is marked as a prerelease when it contains a semantic-version
  prerelease suffix; and
- the checked-in release notes remain accurate after inspecting the artifacts.

Publishing the draft triggers channel-specific Linux updater manifest
generation on GitHub Pages. Confirm that workflow succeeds and preserves the
existing website before announcing the release.
The updater publisher also checks that the initiating actor is a repository
admin before its protected job runs.

If the updater-manifest workflow itself needs a post-publication repair, merge
the workflow fix to `main`, set `release_tag` to that immutable published tag,
and recover it with:

```bash
release_tag="nebula-v<version>"
gh workflow run publish-updater-manifest.yml \
  --ref main \
  -f release_tag="$release_tag"
```

The recovery path rejects drafts, reloads the published timestamp and channel
from GitHub, and revalidates checksums, attestations, and the updater signature.

If a content or release gate fails, leave the draft unpublished, fix the problem
on a new commit, advance the version, and create a new tag. Do not replace
artifacts on an existing published release or repoint its tag.

## APT promotion

The public `BerylliumSec/nebula-apt` repository is created from the secret-free
scaffold in `packaging/repositories/nebula-apt`. Its protected `apt-release`
environment holds only a dedicated signing subkey and passphrase; keep the
primary key and revocation certificate offline. Commit only the exported public
archive key and verify its fingerprint through an independent channel.

After publishing an immutable Nebula release, dispatch `promote.yml` in the APT
repository with the tag and matching `stable` or `prerelease` channel. The
workflow verifies the release checksum and GitHub attestation and opens a
reviewable pull request. Promotions must advance the channel and cannot change
an already-recorded tag. Merging that pull request validates repository input
before signing access, builds and signs both APT distributions, retains the
current and previous package for upgrade testing, runs clean install, upgrade,
self-test, doctor, and uninstall checks on Ubuntu, Debian, and Kali, and deploys
the repository through GitHub Pages.

Never upload a private key, passphrase, token, `.env` file, or decrypted
credential to either public repository or a workflow artifact.
