<div align="center">
  <br />
  <h1>Nebula</h1>
  <p>AI Powered Pentesting</p>
  <br />
</div>

<p align="center">
  <img src="docs/images/nebula-3-workbench.png" alt="Nebula 3 Zero Layer workbench with a live contained terminal and operator safety controls" width="100%" />
</p>

<p align="center"><sub>The Zero Layer workbench</sub></p>

<br />

## Acknowledgement

First, I would like to thank Almighty God, who is the source of all knowledge. Without Him, this would not be possible.


Nebula brings the working parts of a security engagement into one desktop surface: terminal, code, browser, assistant, files, notes, missions, findings, and reports.

AI can investigate, organize, and write. The operator defines the scope, grants authority, and decides what runs.

## Designed around the operator

**Work in one place.** Move from research to a contained terminal, preserve useful output as evidence, and carry it into findings and reports without losing context.

**Keep authority explicit.** Scope enforcement, approval pauses, hard budgets, and isolated OCI execution sit between AI assistance and the systems under test.

**Leave a durable trail.** Content-addressed artifacts, append-only events, execution provenance, and integrity-manifested exports preserve how a conclusion was reached.

```text
intent  →  assistance  →  approval  →  execution  →  evidence
```

Nebula supports hosted, local, and OpenAI-compatible model runtimes. A model provider is optional; the human terminal, evidence workflow, notes, findings, and reports remain available without one.

> Use Nebula only on systems and networks you own or are explicitly authorized to test.

## Install the preview

The current release candidate is **[Nebula 3.0.0-alpha.5](docs/releases/3.0.0-alpha.5.md)** for Linux x86_64. Docker or Podman is required for terminal and automation features.

Download the DEB and `SHA256SUMS-linux-x64.txt` from [GitHub Releases](https://github.com/BerylliumSec/nebula/releases), then verify and install:

```console
sha256sum --check --ignore-missing SHA256SUMS-linux-x64.txt
sudo apt install ./Nebula-3.0.0-alpha.5-linux-x86_64.deb
nebula
```

The DEB supports Debian, Ubuntu, Kali, and compatible systems. Updates remain under administrator control.

<details>
<summary>Use the portable AppImage instead</summary>

Download the AppImage and checksum file from the same release, then run:

```console
sha256sum --check --ignore-missing SHA256SUMS-linux-x64.txt
chmod +x Nebula-3.0.0-alpha.5-linux-x86_64.AppImage
./Nebula-3.0.0-alpha.5-linux-x86_64.AppImage
```

The AppImage requires no system-wide installation and uses Nebula's signed direct-update channel.

</details>

After launch, inspect the bundled Core and local runtime boundary:

```console
nebula-core doctor --json
```

> **About preview builds**
>
> A build exists only when a `nebula-v3.*` entry and its native artifacts appear on [GitHub Releases](https://github.com/BerylliumSec/nebula/releases). Back up engagement data and review the release notes and checksums before use. Do not use `pip install nebula-ai` to install Nebula 3.

macOS, Windows, and Linux arm64 installers are not part of the current release matrix.

## Run from source

Install Python 3.11–3.13, Poetry 2.1.3, Node.js 20 with npm, the stable Rust toolchain, and the [Tauri prerequisites for your operating system](https://v2.tauri.app/start/prerequisites/).

```console
git clone https://github.com/BerylliumSec/nebula.git
cd nebula
poetry install --with dev
npm --prefix ui ci
npm --prefix ui run tauri -- dev
```

The final command builds the local Nebula Core sidecar, starts the UI development server, and opens the native desktop directly from the checkout.

<details>
<summary>Browser-only development and pre-merge checks</summary>

Build the workspace and let Core choose an available loopback port:

```console
npm --prefix ui run build
poetry run nebula-core ui
```

Run the principal checks:

```console
python scripts/nebula3_version.py check
poetry run pytest -q tests/v3
npm --prefix ui test
npm --prefix ui run build
```

</details>

## Migrate from Nebula 2

Quit Nebula 2, preserve a backup of the engagement directory, and import it without modifying the source:

```console
nebula-core import-2x "/path/to/nebula-2-engagement"
```

Verify the imported project and its evidence before deleting the original data. See [Migrating from Nebula 2](docs/MIGRATING-2-TO-3.md) for the complete integrity and recovery procedure.

## Read more

- [Nebula 3 guide](docs/NEBULA3.md)
- [Usage scenarios](docs/NEBULA3_USAGE_SCENARIOS.md)
- [Automation runtime](docs/AUTOMATION-RUNTIME.md)
- [Local diagnostics](docs/NEBULA3_DIAGNOSTICS.md)
- [Release notes](docs/releases/3.0.0-alpha.5.md)
- [Release process](packaging/RELEASING.md)

<br />

---

<p align="center"><sub>Built for deliberate security work.</sub></p>
