---
name: release-nebula-apt
description: Validate, promote, build, sign, test, or publish Nebula Linux DEB releases through the public signed APT repository. Use for BerylliumSec/nebula-apt, stable or prerelease APT channels, archive signing keys, GitHub Pages deployment, package install or upgrade testing, and changes to packaging/repositories/nebula-apt.
---

# Release Nebula APT

Use `packaging/RELEASING.md` as the release contract and
`packaging/repositories/nebula-apt` as the external repository scaffold. Treat
release tags, published assets, attestations, and promoted channel entries as
immutable.

## Guardrails

- Accept only a published `nebula-v3.*` release and its
  `Nebula-VERSION-linux-x86_64.deb`.
- Map semantic versions with a prerelease suffix only to `prerelease`; map
  versions without one only to `stable`.
- Verify the release-provided SHA-256 and GitHub provenance attestation before
  changing `channels.json`. Match the checksum filename exactly; similarly
  prefixed SBOM filenames are separate assets.
- Validate `Package`, `Version`, and `Architecture` from DEB metadata. Do not
  pass `--arch` to `dpkg-scanpackages`: Nebula's managed asset ends in
  `linux-x86_64.deb`, while that option filters Debian-style filenames ending
  in `_amd64.deb`.
- Retain the current and previous package in each channel so CI tests a real
  upgrade.
- Require every promotion to advance semantic-version precedence. Reject
  downgrades, duplicate versions, and any change to an already-recorded tag.
- Keep the OpenPGP primary key and revocation certificate offline. Put only an
  exported dedicated signing subkey and its passphrase in the protected
  `apt-release` environment.
- Never commit or upload a private key, passphrase, token, `.env` file, decrypted
  credential, or signing home directory.
- Commit the public archive key only after its fingerprint is verified through
  an independent channel.
- Do not push a promotion branch, open or merge a pull request, publish Pages,
  replace an asset, or repoint a tag unless the user explicitly authorizes that
  external change.

## Change the repository source

When editing the scaffold, run:

```console
python -m unittest discover -s packaging/repositories/nebula-apt/tests -v
sh -n packaging/repositories/nebula-apt/scripts/build-repository.sh
sh -n packaging/repositories/nebula-apt/scripts/smoke-test.sh
```

Review every workflow for least-privilege permissions and pinned action commit
SHAs. Validate `channels.json` before any job can access `apt-release`. Confirm
that signing material is created under the runner temporary directory and that
the job never uploads it as an artifact.

## Promote a release

1. Confirm the source GitHub release is published, immutable, and has the
   expected stable or prerelease flag.
2. Confirm the managed DEB digest matches `SHA256SUMS-linux-x64.txt`.
3. Verify its GitHub attestation against `BerylliumSec/nebula`.
4. Dispatch `promote.yml` in `BerylliumSec/nebula-apt` with the exact tag and
   channel.
5. Review the generated `channels.json` pull request. It must contain only the
   new current package and, when present, the prior package for that channel.
6. Merge only after repository CI passes and explicit publication approval is
   granted.

## Verify publication

Require `publish.yml` to:

1. Re-download and verify every retained DEB.
2. Generate `Packages`, `Packages.gz`, `Release`, `Release.gpg`, and `InRelease`
   for both channels, and make the entire public tree world-readable.
3. Verify the metadata with the exported public archive key.
4. Run clean install, upgrade, `nebula --self-test`,
   `nebula-core doctor --json`, and uninstall checks on Ubuntu, Debian, and
   Kali.
5. Deploy the tested directory to GitHub Pages.

After deployment, fetch `InRelease` and `Packages.gz` from Pages, verify the
signature using a clean keyring, and confirm the promoted version is indexed.
