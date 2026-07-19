# Nebula – Security Testing Workbench

> [!IMPORTANT]
> **Nebula 2 has been discontinued and removed.** It is no longer maintained,
> tested, packaged, distributed, or supported. Nebula 3 is the only supported
> Nebula application. Historical `nebula-ai` releases remain on PyPI only for
> reproducible exact-version installs and receive no fixes or security updates.

Nebula 3 is a native, local-first security testing workbench. It combines an
isolated human terminal, optional AI assistance, immutable evidence, reviewed
execution, and reporting. Terminal use does not require a model provider, and
human terminal work never falls back to a host shell.

## Install and launch

Install a signed macOS DMG/Homebrew cask or Linux DEB/AppImage from the
[Nebula releases](https://github.com/BerylliumSec/nebula/releases). Docker or
Podman must be installed separately for terminal and automation features.

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

```console
poetry install --with dev
poetry run nebula-core doctor
poetry run pytest -q tests/v3
npm --prefix ui ci
npm --prefix ui run build
poetry run nebula-core ui
```

The Poetry package is a build-time Core boundary and is not a replacement for
the native desktop installer. End users should use the signed application.

## Documentation

- [Nebula 3 guide](docs/NEBULA3.md)
- [Automation runtime](docs/AUTOMATION-RUNTIME.md)
- [Local diagnostics](docs/NEBULA3_DIAGNOSTICS.md)
- [Usage scenarios](docs/NEBULA3_USAGE_SCENARIOS.md)
- [Release process](packaging/RELEASING.md)

Use Nebula only on systems and networks you own or are explicitly authorized to
test.
