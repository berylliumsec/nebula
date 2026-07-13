# Nebula Toolbox staging installation

The branch-only **Build Nebula Toolbox Staging Bundle** workflow builds and
smoke-tests native `linux/amd64` and `linux/arm64` images, pushes them to the
separate `ghcr.io/berylliumsec/nebula-toolbox-staging` package, and uploads one
unsigned, digest-resolved `.nebula-toolpack` artifact. Before upload, the
workflow also installs the assembled bundle through Nebula Core's production
pull, digest-inspection, hardened smoke-test, parsing, and schema-validation
path. It never signs or deploys the official Toolbox catalog.

## Download the bundle on macOS

Download the artifact named `nebula-toolbox-staging-<run-id>` from the workflow
run. With GitHub CLI, find the run and download it with:

```bash
gh run list \
  --workflow toolbox-staging.yml \
  --branch nebula-3-modernization

gh run download <run-id> \
  --name nebula-toolbox-staging-<run-id> \
  --dir "$HOME/Downloads/nebula-toolbox-staging"

cd "$HOME/Downloads/nebula-toolbox-staging"
shasum -a 256 -c SHA256SUMS
```

The workflow artifact expires after 14 days. Its manifest remains locked to the
immutable image digests from that workflow run.

## Authenticate the container runtime

Skip this step if the staging GHCR package has been made public. Otherwise,
create a GitHub token with `read:packages`, then authenticate the same Docker or
Podman runtime configured in Nebula:

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io \
  --username <github-username> \
  --password-stdin
```

For Podman, replace `docker` with `podman`. Nebula's installer uses the runtime's
normal credential store and pulls the exact platform digest from the bundle.

## Install through the Nebula desktop app

1. Quit Nebula.
2. Start the app from Terminal with local unsigned tool packs enabled:

   ```bash
   NEBULA_TOOL_DEVELOPER_MODE=1 \
     /Applications/Nebula.app/Contents/MacOS/nebula-ui
   ```

   From a source checkout, use:

   ```bash
   NEBULA_TOOL_DEVELOPER_MODE=1 npm --prefix ui run tauri -- dev
   ```

3. In **Settings → Runners**, configure and verify Docker Desktop or Podman
   Machine.
4. In **Settings → Toolbox**, choose **Use a custom compatible environment**,
   select `nebula-toolbox-staging.nebula-toolpack`, review the permissions, and
   confirm the permanent unsigned-development warning.

Installation pulls only the image matching the Mac runner (`linux/arm64` on
Apple silicon or `linux/amd64` on Intel), verifies its digest and non-root
contract, and runs the pack smoke tests. Mission execution subsequently uses
`--pull=never`.

## CLI alternative

If the packaged `nebula` command and a ready runner profile use the same Nebula
data directory as the app, install with:

```bash
NEBULA_TOOL_DEVELOPER_MODE=1 nebula tools doctor

NEBULA_TOOL_DEVELOPER_MODE=1 nebula tools install-local \
  "$HOME/Downloads/nebula-toolbox-staging/nebula-toolbox-staging.nebula-toolpack" \
  --runner <runner-id> \
  --yes
```

For a source checkout, replace `nebula` with `poetry run nebula3`.

## Remove or replace the staging pack

Staging packs never update through the official catalog. Remove the installed
environment in **Settings → Toolbox**, then upload the bundle from a newer
workflow run. Historical missions retain their original digest locks.
