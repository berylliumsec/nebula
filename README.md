# Acknowledgement

First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.

# Nebula – Security Testing Workbench

Nebula is an operator first, AI powered penetration testing platform that seeks to simplify the process of network penetration testing by integrating AI Agents, Terminals, a code editor, a web browser, screenshots, note-taking, screenshots in one desktop application. 

At its core it is driven by the human operator and assisted by AI agents.

## Release status

Nebula 3 is in preview. A build is available only when a `nebula-v3.*` entry and
its native artifacts appear on the
[GitHub Releases page](https://github.com/BerylliumSec/nebula/releases). If no
Nebula 3 entry is listed, no Nebula 3 installer has been published yet; use a
source checkout instead. Do not use `pip install nebula-ai` to install Nebula 3.

Preview builds are intended for evaluation on authorized systems. Back up
engagement data, review the release notes and checksums, and do not treat an
alpha build as a stable production release. The first release line is
[Nebula 3.0.0-alpha.1](docs/releases/3.0.0-alpha.1.md).

## Install and launch

Published releases can contain:

- macOS 13 or newer DMGs for Apple silicon and Intel;
- a Linux x86_64 AppImage with direct updates; and
- a Linux x86_64 DEB for managed installations.

Windows installers are not part of the current release matrix. Docker or
Podman must be installed separately for terminal and automation features; a
model provider is optional.

Launch the native desktop from the operating-system application menu or run:

```console
nebula
```

Administration and diagnostics use the bundled Core command:

```console
nebula-core doctor --json
nebula-core migrate
```

## Migrate existing Nebula 2 data

The discontinued application is not required to migrate an existing engagement.
Quit any running Nebula 2 process, preserve a backup of the engagement directory,
and import it without modifying the source:

```console
nebula-core import-2x "/path/to/nebula-2-engagement"
```

Verify the imported Project and its evidence before deleting the original data.
See [Migrating from Nebula 2](docs/MIGRATING-2-TO-3.md) for the integrity,
external-knowledge, verification, and recovery procedures.

## Develop from source

From an existing checkout, synchronize dependencies and launch the native
desktop:

```console
git pull --ff-only
poetry install --with dev
npm --prefix ui ci
npm --prefix ui run tauri -- dev
```

For browser-only development, build the workspace and let Core choose an
available loopback port:

```console
npm --prefix ui run build
poetry run nebula-core ui
```

Run the principal pre-merge checks with:

```console
python scripts/nebula3_version.py check
poetry run pytest -q tests/v3
npm --prefix ui test
npm --prefix ui run build
```

The Poetry package is a build-time Core boundary and is not a replacement for
the native desktop installer. End users should use a published native package.

## Documentation

- [Nebula 3 guide](docs/NEBULA3.md)
- [Nebula 3.0.0-alpha.1 release notes](docs/releases/3.0.0-alpha.1.md)
- [Automation runtime](docs/AUTOMATION-RUNTIME.md)
- [Local diagnostics](docs/NEBULA3_DIAGNOSTICS.md)
- [Usage scenarios](docs/NEBULA3_USAGE_SCENARIOS.md)
- [Release process](packaging/RELEASING.md)

Use Nebula only on systems and networks you own or are explicitly authorized to
test.
