# Nebula Toolbox

Nebula Toolbox is the default executable environment for Nebula 3 missions. It
is one non-root, multi-architecture OCI image rather than a collection of
scanner-specific images. Agents discover commands from a generated catalog inside the
image and invoke them through the broker; Core never exposes the host shell or
container-runtime socket.

The initial image contains a practical set of network, web, discovery, code,
crypto, and utility commands:

- Nmap 7.99, Ncat, socat, DNS utilities, WHOIS, curl, OpenSSL, jq
- Nuclei with pinned templates, httpx, dnsx, subfinder, and Katana
- ffuf, Gobuster, Nikto, WhatWeb, testssl.sh, and sqlmap
- Semgrep and Impacket
- Bash inside the disposable container

It intentionally omits GUI applications, wireless/GPU stacks, Metasploit, word
lists, and broad Kali metapackages. Add those to a compatible custom image when
an engagement needs them.

## Installation and use

1. Install Docker Desktop or Podman Machine on macOS, or rootless
   Docker/Podman on Linux. Nebula detects supported fixed-path runtimes; use
   **Settings → Advanced** only to resolve multiple candidates or diagnose an
   unavailable runtime.
2. Choose **Automate task** in Workbench. On first use Nebula prepares the
   official signed Toolbox in the background, verifies the exact platform
   digest and non-root manifest, and runs its smoke tests.
3. Define authorized CIDRs/domains/URLs and ports under **Project → Assets**.
   Advanced policy and digest-locked environment assignment remain available
   in **Settings → Advanced**.
4. Enter the automation objective. Model, capabilities, budgets, retries, and
   concurrency use safe defaults and remain available under **Advanced**.

Ordinary local commands and in-scope active scans run without a per-command
approval. Core still enforces scope, ports, time windows, prohibited actions,
container isolation, concurrency and call budgets, evidence recording, and
mission cancellation. Credential use, exploitation, persistence, destructive
actions, scope changes, and external filesystem writes remain approval-gated.

Network execution uses the environment's embedded digest-pinned egress helper;
a runner profile may explicitly override it with another compatible pinned
helper. If the runner or egress boundary is unavailable, local/offline analysis
continues and network capabilities report why they are unavailable. Nebula
never falls back to host execution.

The same operations are available from the bundled CLI:

```bash
nebula-core tools doctor
nebula-core tools catalog
nebula-core tools install berylliumsec/nebula-toolbox --runner local --yes
nebula-core tools list
```

The installer stores immutable manifests and verification state per user:

- macOS: `~/Library/Application Support/io.nebula.security/tool-packs/`
- Linux: `$XDG_DATA_HOME/io.nebula.security/tool-packs/`, or
  `~/.local/share/io.nebula.security/tool-packs/`

OCI layers remain in Docker or Podman's content store.

## Compatibility contract

A compatible image must:

- support `linux/amd64` and `linux/arm64`;
- declare a non-root `USER` and contain no mutable image references in its
  manifest;
- contain `/opt/nebula/tool-catalog.json` using protocol
  `nebula.toolbox.catalog/v2` and its JSON Schema at
  `/opt/nebula/tool-catalog.schema.json`;
- provide `/opt/nebula/bin/nebula-toolbox` with the `search`, `help`, `exec`,
  `shell`, and bounded `code` subcommands and `/usr/local/bin/nebula-egress`
  with the signed helper contract;
- emit exactly one JSON object for catalogued Toolbox envelope operations; the
  operator-reviewed `code` adapter instead streams the declared interpreter's
  raw stdout/stderr and never gives it interactive stdin;
- use `/workspace` for engagement files and run without an OCI `ENTRYPOINT`;
- pass the offline search, help, local-exec, and loopback network smoke tests.

The model catalog contains one exact-version interface per first-class
executable. Each command and subcommand declares named positionals, every
documented short and long option, value types, repetition, dependencies,
conflicts, exact examples, and captured help evidence. Structured requests use
command paths plus named option and positional IDs; the wrapper compiles and
validates argv before execution.

The reviewed sources are the YAML files under `interfaces/`. During every image
build, `build-tool-catalog.py` executes every version probe and help command,
discovers command trees such as Git and OpenSSL, and requires complete option
coverage with no unmapped switches.
The build fails if an executable is missing, reports a version different from
`tool-versions.env`, returns unusable help, or produces an invalid descriptor.
This makes the JSON an interface contract for the image that was actually
built rather than documentation that can silently drift.

Core validates the signed JSON again when installing it. Mutable version
labels, duplicate aliases or flags, ambiguous positionals, unknown value
types, dangling option relationships, malformed help hashes, and inconsistent
coverage counts all fail closed. The same validation applies to unsigned local
catalogs in developer mode.

Agents receive seven stable broker capabilities rather than hundreds of
hard-coded schemas:

| Capability | Purpose | Network |
|---|---|---|
| `environment.search` | Search the image's tool index | None |
| `environment.help` | Read indexed command help | None |
| `environment.run_local` | Run an indexed command against `/workspace` | None |
| `environment.run_network` | Run an indexed command against one broker-pinned target and port set | Scoped |
| `environment.run_invasive` | Run an indexed invasive command after durable operator approval | Scoped |
| `environment.shell_local` | Run full Bash for uncatalogued tools and pipelines | None |
| `environment.shell_network` | Run full Bash through broker-pinned egress | Scoped |

Core automatically injects the selected command's detailed interface into the
execution model turn. Structured network positionals may use `{target}`; shell
scripts use `NEBULA_TARGET` and `NEBULA_PORTS`. Shell guidance is intentionally
not a syntax gate and may invoke catalogued or uncatalogued commands, but it
still runs only in the disposable container.

## Building a custom environment

The simplest extension is to derive from an immutable official Toolbox digest,
install additional binaries, and replace the tool index:

```Dockerfile
FROM ghcr.io/berylliumsec/nebula-toolbox@sha256:<published-digest>

USER 0:0
COPY your-tool-versions.env /opt/nebula/tool-versions.env
COPY interfaces/ /opt/nebula/interfaces/
RUN <install-your-exact-digest-or-version-pinned-tools> \
    && /opt/nebula/bin/build-tool-catalog \
       --interfaces /opt/nebula/interfaces \
       --versions /opt/nebula/tool-versions.env \
       --schema /opt/nebula/tool-catalog.schema.json \
       --output /opt/nebula/tool-catalog.json
USER 10001:10001
```

Build each platform image, push it to a registry, and record the immutable
digests. Create a local environment bundle using the existing author SDK:

```bash
nebula-core tools init my-toolbox --publisher your-company
# Replace the generated image/tool definitions with the Toolbox compatibility
# manifest and its exact platform digests.
nebula-core tools validate my-toolbox
nebula-core tools test my-toolbox --runner local
nebula-core tools pack my-toolbox --output my-toolbox.nebula-toolpack
```

Enable device developer mode, then upload the bundle under **Settings →
Advanced → Toolbox → Use a custom compatible environment**. Unsigned local environments
retain a permanent warning. Nebula does not fetch unsigned manifests from a
URL, import third-party code into Core, or grant a newly installed environment
to existing engagements.

An organization may instead create a completely independent image as long as
it implements the same paths, wrapper protocol, catalog, non-root user, and
generic capability manifest. Historical missions remain locked to the original
manifest and image digest when a replacement is installed.

### Testing the official Toolbox before publication

Pushes to the `nebula-3-modernization` branch run the non-publishing
**Build Nebula Toolbox Staging Bundle** workflow. It uses a separate staging
GHCR package and uploads a digest-resolved unsigned bundle for end-to-end Mac
installation tests without creating a release tag, using the release signing
key, or deploying the official catalog. Follow the
[staging installation guide](TOOLBOX-STAGING.md) to download and install the
artifact.

## Reproducibility and version updates

`tool-versions.env` is the single reviewed version ledger. The current image
uses digest-pinned Python and Go bases, a timestamped Debian snapshot with exact
top-level package revisions, exact Go module and Python distribution versions,
and SHA-256-verified source archives. Nmap and Ncat are built from the verified
Nmap 7.99 upstream source instead of the older Debian package. The complete
resolved Python environment is retained in the image as
`/opt/nebula/python-environment.lock`.

Updating a tool is an explicit source change: update its version and content
hash where applicable, update version-specific syntax/examples if the interface
changed, then build both architectures. The version probes, captured help,
image smoke tests, SBOM, provenance, and signed digest are all regenerated; an
active or historical mission never moves to the new image implicitly.

## Publication

The **Publish Nebula Toolbox** workflow supports read-only `validate-source`
and protected `publish` modes. Publication requires an immutable
`nebula-toolbox-v<version>` tag and approval through the `tool-pack-release`
environment. It builds native Linux amd64/arm64 images, signs both digests with
GitHub OIDC, generates SBOM and provenance evidence, creates a multi-arch tag,
signs the manifest/catalog with the offline Ed25519 release key, verifies
anonymous pulls, and deploys the catalog at:

`https://berylliumsec.github.io/nebula/toolbox/`

The workflow requires the amd64 and arm64 catalogs to be byte-identical. The
resolved catalog is published by SHA-256, referenced by the signed catalog
entry, downloaded and verified during installation, and pinned into each
mission alongside the image and pack manifest digests. A matching copy remains
inside the image for wrapper-side validation.

The source manifest contains digest placeholders and intentionally fails the
candidate-ready gate until the workflow builds the actual images. Never commit
resolved digests, generated release evidence, or the private signing key.
