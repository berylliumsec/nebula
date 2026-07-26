# Nebula APT repository

This directory is the complete, secret-free source scaffold for the public
`BerylliumSec/nebula-apt` repository. Copy its contents to that repository; do
not publish it as a subdirectory of the Nebula website.

The repository has two Debian distributions:

- `stable` accepts semantic versions without a prerelease suffix.
- `prerelease` accepts semantic versions with a prerelease suffix.

`channels.json` retains the current and previous release in each distribution
so CI can prove a real package upgrade. Promotions must move a channel forward;
downgrades and changes to an already-recorded tag are rejected. `promote.yml`
validates a published, immutable GitHub release and opens a reviewable
channel-change pull request. Merging that pull request makes `publish.yml`
validate all repository-controlled input before granting signing access,
download the DEBs, verify their release checksums and GitHub attestations,
create signed APT metadata, test fresh install/upgrade/doctor/uninstall on
Ubuntu, Debian, and Kali, and deploy Pages.

## One-time setup

1. Create the public repository with an initial `main` branch, then protect it.
2. Copy this scaffold onto a setup branch; do not push directly to `main`.
3. Generate the archive primary key offline and a dedicated signing subkey.
   Export the public archive key as `nebula-archive-keyring.asc`, add it to the
   setup branch, and verify its fingerprint through an independently controlled
   channel.
4. Create an `apt-release` environment with required reviewer approval and
   tag/branch protection. Add:
   - `APT_SIGNING_SUBKEY`: an ASCII-armored export of a dedicated signing
     subkey, never the offline primary key.
   - `APT_SIGNING_PASSPHRASE`: the signing subkey passphrase.
5. Configure GitHub Pages to use GitHub Actions.
6. Permit Actions to create pull requests, or have a release manager push the
   generated promotion branch and open the pull request manually.
7. Open and review the setup pull request. After merging, approve the initial
   `apt-release` deployment only after confirming the workflow is the reviewed
   version and the public-key fingerprint is correct.

Never put a private key, passphrase, token, `.env` file, or decrypted credential
in this repository or an artifact.

## Consumer setup

After the archive key fingerprint has been independently verified:

```console
curl -fsSL https://berylliumsec.github.io/nebula-apt/nebula-archive-keyring.asc |
  sudo gpg --dearmor -o /usr/share/keyrings/nebula-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/nebula-archive-keyring.gpg] https://berylliumsec.github.io/nebula-apt stable main" |
  sudo tee /etc/apt/sources.list.d/nebula.list
sudo apt update
sudo apt install nebula
```

Use `prerelease` in place of `stable` only when prerelease upgrades are wanted.
