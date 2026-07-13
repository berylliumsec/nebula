# Nebula 3 tool packs

Nebula tool packs give supervised missions a closed set of typed capabilities.
They do not give a model a host shell, do not add commands to the human
terminal, and do not install executables into `/usr/local/bin` or the Nebula
application bundle.

## Current release status

The repository contains hardened Safe Foundation image sources and a protected
publisher. A `nebula-tools-v*` release builds each platform image, generates its
SBOM and GitHub provenance, signs the OCI digest through Sigstore, resolves and
signs every manifest, and publishes the signed catalog at
`https://berylliumsec.github.io/nebula/tool-packs/`.

Nebula embeds only the publisher-bound Beryllium Ed25519 public key. The private
key is never committed and is available only to the protected
`tool-pack-release` environment. Administrators may add other publisher keys
with `NEBULA_TOOL_PACK_PUBLIC_KEYS`, but cannot replace an embedded key ID.

Source manifests retain `{{sha256:...}}` placeholders. The protected publisher
resolves them from actual registry outputs without committing generated
digests, SBOMs, provenance, or signing material back to the source branch.

Current execution constraints:

- Network tools remain unavailable unless a runner also has a digest-pinned
  egress helper that passes runner verification. A configured custom seccomp
  profile must be an existing absolute local file.
- Public Core/API builds keep executable mission budgets at zero until the
  complete runner-isolation acceptance flow passes. Tool installation and
  verification can be exercised without opening this execution gate.
- Analysis-only chat and missions continue to work.

The Safe Foundation collection is:

| Source pack | Declared tools | Network | Current status |
| --- | --- | --- | --- |
| `safe-network` | `nmap.connect_scan`, `nmap.service_scan` | Scoped active scan | Isolated Nmap image |
| `safe-web` | `nuclei.scan`, `nikto.scan` | Scoped active scan | Separate Nuclei and Nikto images |
| `safe-intelligence` | `searchsploit.query` | None | Isolated SearchSploit image |
| `safe-code` | `semgrep.scan` | None | Isolated Semgrep image |

Masscan, raw SYN scanning, exploitation, credentials, persistence, and
destructive tools are not part of Safe Foundation.

## Where tools are installed

Tool executables live in an external OCI runtime's content store. Docker
Desktop and Podman Machine keep images inside their Linux VM; rootless Docker
or Podman on Linux keeps them in that user's runtime storage. Nebula does not
copy those binaries onto the host.

Nebula stores immutable manifests, signatures, and catalog cache in one exact
per-user tool-pack root:

- macOS: `~/Library/Application Support/io.nebula.security/tool-packs/`
- Linux: `$XDG_DATA_HOME/io.nebula.security/tool-packs/`, or by default
  `~/.local/share/io.nebula.security/tool-packs/`

Canonical manifests are below `manifests/sha256/` in that root. Temporary local
uploads use its `incoming/` directory and are deleted after validation.

The Core database, engagement workspaces, parser workspaces, and evidence stay
in the separate Core data directory. The desktop chooses its platform
application-data `core/` directory; headless use honors `NEBULA_V3_DATA_DIR`,
`--data-dir`, or defaults to `~/.local/share/nebula/v3/`. The database holds
installation state and exact manifest/image digest locks.

Removing a pack disables it but retains historical manifests referenced by run
records. OCI image garbage collection remains an explicit runtime-administrator
operation.

## Configure a runner

Install and configure Docker or Podman yourself first. Nebula never installs a
runtime, starts a privileged helper, selects an executable from `PATH`, or
accepts a remote TCP daemon.

Supported profiles are:

- macOS Podman Machine with `podman_machine` isolation.
- macOS Docker Desktop with `docker_desktop_vm` isolation and a local context.
- Linux rootless Podman or rootless Docker with `rootless` isolation.

In **Settings → Sandbox runners**, select the host arrangement, enter the
trusted absolute Docker/Podman executable, choose the image platform
(`linux/arm64` or `linux/amd64`), and optionally specify a local context or Unix
socket. Saving the profile asks Core to inspect it; a profile is not usable
until its `healthy` status is true.

The equivalent API request is:

```http
PUT /api/v1/runner-profiles/local-podman
Authorization: Bearer <local-core-token>
Content-Type: application/json

{
  "name": "Rootless Podman",
  "runtime": "podman",
  "executable": "/usr/bin/podman",
  "socket": "unix:///run/user/1000/podman/podman.sock",
  "platform": "linux/amd64",
  "isolation": "rootless",
  "enabled": true
}
```

Adjust paths and platform to the actual local installation. Core rejects a
relative executable, a binary whose name does not match `runtime`, remote
sockets, mutable egress-helper tags, and invalid runtime/isolation
combinations.

Use the CLI to recheck one saved profile and view tool availability:

```bash
nebula3 tools doctor --runner local-podman
```

Network tools additionally require `egress_helper_image` as an exact
`repository@sha256:<digest>`. An optional custom `seccomp_profile` must be an
absolute local path and is checked when the runner is saved. Do not substitute
a mutable tag or an unreviewed helper. Until an approved helper digest is
released, leave the helper unset and expect network tools to be reported
unavailable.

## Install and manage packs

Nebula embeds the Beryllium catalog's publisher-bound Ed25519 public key. An
administrator-owned key file is needed only for additional trusted publishers
or a self-hosted catalog. It contains a `keys` object mapping release key IDs to
their public-key encodings. Symbolic example only:

```json
{
  "keys": {
    "RELEASE_KEY_ID": {
      "public_key": "BASE64_ED25519_PUBLIC_KEY",
      "publishers": ["berylliumsec"]
    }
  }
}
```

Use publisher-bound entries for curated trust. When a key has a `publishers`
allowlist, a valid signature from that key is still rejected for any other
manifest publisher. Legacy raw public-key values remain compatibility-only.

Set its absolute path before Core starts:

```bash
export NEBULA_TOOL_PACK_PUBLIC_KEYS=/secure/config/nebula-tool-keys.json
```

The catalog and signature URLs default to the Beryllium locations declared by
Core and can be overridden with `NEBULA_TOOL_CATALOG_URL` and
`NEBULA_TOOL_CATALOG_SIGNATURE_URL`. Overrides do not bypass signature
verification.

After reviewing permissions:

```bash
nebula3 tools catalog
nebula3 tools install-collection safe-foundation \
  --runner local-podman --yes
nebula3 tools install berylliumsec/safe-network@0.1.0 \
  --runner local-podman --yes
nebula3 tools list
nebula3 tools verify INSTALLATION_ID
nebula3 tools update INSTALLATION_ID --yes
nebula3 tools remove INSTALLATION_ID --yes
```

Place `--json` immediately after `tools` for the stable machine-readable CLI
contract, for example `nebula3 tools --json list`.

The desktop presents the four independently versioned packs as one **Safe
Foundation** collection. One action installs its latest signed members; if a
new member fails, Core disables the members newly installed by that operation.
Installation is separate from execution: it
pulls exact digests, verifies the manifest and platform, runs bounded smoke
tests, and records the installation. Missions use `--pull=never` and cannot
download a missing or newer image.

The REST equivalents are authenticated:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/tool-catalog` | Read the verified curated catalog |
| `GET` | `/api/v1/tool-packs` | List installation records |
| `GET` | `/api/v1/tools` | List tools with availability reasons |
| `POST` | `/api/v1/tool-packs/install` | Install a catalog ID for a runner |
| `POST` | `/api/v1/tool-collections/install` | Install the latest members of a signed collection |
| `POST` | `/api/v1/tool-packs/install-local` | Upload an explicitly confirmed local bundle |
| `POST` | `/api/v1/tool-packs/{id}/verify` | Reverify images and smoke tests |
| `POST` | `/api/v1/tool-packs/{id}/update` | Install a newer signed version side by side |
| `DELETE` | `/api/v1/tool-packs/{id}` | Disable an installation |
| WebSocket | `/api/v1/tool-packs/events/ws` | Replay and follow sanitized installation progress |

Every endpoint requires the Core bearer token. A Core built without the tool
platform returns `501` for platform operations; listing installed packs and
tools remains safe. An unreachable catalog never causes an unsigned fallback.

### Installation progress stream

Connect to `/api/v1/tool-packs/events/ws?after_sequence=N` using the Core bearer
token. Browser clients can offer `nebula.tool-packs.v1` plus the same
URL-safe-base64 `nebula.auth.<token>` subprotocol used by other authenticated
Core WebSockets. Header authentication is also accepted for non-browser
clients.

The process-local journal emits monotonic `pending`, `pulling`, `verifying`,
`ready`, and `failed` phases. Events contain only an operation ID/type, phase,
timestamp, validated pack identity/digest, installation ID when known, and
result status. Bundles, runner identifiers, command output, exception text,
credentials, and signatures are never included.

Core retains the latest 256 events in memory by default. Initial replay ends
with `replay_complete`; `truncated: true` and `oldest_sequence` tell a reconnecting
client that its cursor predates retained history. Idle connections receive
heartbeats carrying the current cursor; a `replay_gap` reports the same condition
if a connected client falls behind retention. Sequences reset when Core
restarts, so the authoritative installation record remains
`/api/v1/tool-packs`. Missing authentication closes with `4401`; an
authenticated Core without a configured tool platform closes with `4501` (the
WebSocket equivalent of fail-closed `501`).

## Grant tools to a mission

Installation never grants agent access. The operator must complete all of the
following:

1. Create an engagement scope under **Settings → Rules of engagement**, with
   explicit CIDRs/domains/URLs, ports, time bounds, and prohibited actions.
2. Assign selected tool names from one exact ready manifest digest to that
   engagement.
3. Select an enabled provider/model whose verified profile declares reliable
   tool calling and strict structured output.
4. Start a mission with non-empty `tool_names`, a positive `max_tool_calls`, and
   concurrency no greater than two.
5. Resolve any approval requested for an active or higher-risk call.

The relevant API flow is:

```http
PUT /api/v1/engagements/ENGAGEMENT_ID/scope
PUT /api/v1/engagements/ENGAGEMENT_ID/tool-assignment
POST /api/v1/missions
POST /api/v1/approvals/APPROVAL_ID/decision
```

Example assignment body:

```json
{
  "manifest_digest": "<64-lowercase-hex-digest>",
  "tool_names": ["nmap.connect_scan"],
  "enabled": true
}
```

Example executable mission body:

```json
{
  "engagement_id": "ENGAGEMENT_ID",
  "objective": "Identify services on the authorized lab host",
  "provider_id": "PROVIDER_PROFILE_ID",
  "model": "MODEL_ID",
  "tool_names": ["nmap.connect_scan"],
  "max_tool_calls": 20,
  "max_concurrency": 1
}
```

The CLI uses the same durable mission service after scope, assignment, runner,
pack, and provider records exist:

```bash
nebula3 run ENGAGEMENT_ID "Identify authorized lab services" \
  --provider PROVIDER_PROFILE_ID --model MODEL_ID \
  --tool nmap.connect_scan --max-tool-calls 20 --max-concurrency 1
```

An active call pauses at `waiting_approval`. Use the desktop approval card or
the authenticated approval endpoint to approve, reject, edit, or stop it. The
exact request and decision are persisted before resumption. Scanner output is
stored as evidence and candidate observations; it is not automatically a
confirmed finding.

Omitting `tool_names` keeps the defaults `max_tool_calls: 0`, concurrency one,
and delegation depth zero. This analysis-only path does not require a runner.

During the pre-release acceptance suite only, an isolated QA build may set
`NEBULA_ENABLE_TOOL_EXECUTION_QA=1` before Core starts. This is not an end-user
compatibility option: public builds leave it unset, and promotion requires the
packet-capture, restart/resume, evidence, approval, and clean-machine gates to
pass together. Direct and managed distribution channels ignore the flag even
if it is present in the launching environment.

## Local unsigned developer packs

Unsigned packs are permitted only for local development. They require all
three controls:

1. Start Core with `NEBULA_TOOL_DEVELOPER_MODE=1`.
2. Supply a local `.nebula-toolpack` archive; remote unsigned URLs are never
   accepted.
3. Explicitly confirm the requested permissions in the UI or with `--yes`.

```bash
export NEBULA_TOOL_DEVELOPER_MODE=1
nebula3 tools install-local ./my-pack.nebula-toolpack \
  --runner local-podman --yes
```

The installation is permanently recorded as `local_unsigned`. Developer mode
does not relax digest pinning, schema validation, scope, runner isolation,
approvals, evidence capture, or mission budgets. Local packs cannot use the
signed automatic update path.

## Author a pack

Start from the conservative SDK template:

```bash
nebula3 tools init ./my-pack --name my-pack --publisher my-org
nebula3 tools validate ./my-pack
nebula3 tools test ./my-pack
```

Source validation permits explicit `{{sha256:...}}` placeholders so authors can
work before publishing images. It still enforces closed JSON schemas, absolute
non-shell executables, typed argument bindings, declared target/path mappings,
multi-platform image contracts, bounded smoke tests, and source-tree safety.

Builds are operator-selected and do not edit the manifest:

```bash
nebula3 tools build ./my-pack \
  --runtime /usr/bin/podman \
  --tag registry.example/my-org/my-pack:build-amd64 \
  --platform linux/amd64
```

Before packaging a local candidate:

1. Build and test both `linux/amd64` and `linux/arm64` images as non-root.
2. Push them through the publisher's reviewed registry process.
3. Replace every source and Containerfile digest placeholder with an immutable
   digest obtained from that registry.
4. Generate each declared SBOM and provenance file and place it at the exact
   manifest path.
5. Run validation with release placeholders forbidden.
6. Create the deterministic archive.

```bash
nebula3 tools validate ./my-pack --require-digests
nebula3 tools test ./my-pack
nebula3 tools pack ./my-pack ./my-pack.nebula-toolpack
```

Complex custom parsers must be declared as digest-pinned parser containers.
They run without network, as non-root, with read-only input and bounded output;
third-party parser code is never imported into Core.

## Safe Foundation release gate

The **Publish Safe Foundation tool packs** workflow has two modes:

- `validate-source` performs read-only source validation from any selected ref.
- `publish` requires an immutable `nebula-tools-v*` tag and approval through the
  protected `tool-pack-release` environment.

Publication builds `linux/amd64` and `linux/arm64` images independently,
records exact Kali package versions, generates per-platform CycloneDX SBOMs and
in-toto provenance, creates Sigstore signatures and GitHub attestations, then
assembles the Ed25519-signed catalog and deterministic `.nebula-toolpack`
bundles. Pages deployment occurs only after signature verification passes.

GitHub creates new GHCR packages as private and does not expose package
visibility changes through its public API. On the first publication of a new
component, an organization owner must open that component's **Package
settings**, choose **Change visibility → Public**, and confirm the package name.
The workflow deliberately fails its anonymous-pull gate until this is done.
Afterward, rerun only the failed jobs; successful image builds are retained.

Run the same gate locally:

```bash
poetry run python -m scripts.validate_tool_pack_release --json
poetry run python -m scripts.validate_tool_pack_release \
  --require-candidate-ready --json
```

Release owners must back up the Ed25519 private key outside GitHub and preserve
required-reviewer protection on `tool-pack-release`. Key rotation requires an
overlapping Nebula release containing both old and new public keys. Do not
promote a release if clean-machine installation, smoke tests, signatures,
catalog verification, or runner-isolation checks fail.

## Fail-closed guarantees

A tool is exposed to a mission only when the pack is ready, its manifest digest
is assigned to the engagement, the runner is enabled and healthy, the provider
supports strict tools, the target is in the current scope, budget remains, and
required approval exists. Network tools also require enforced egress.

Failure of any condition leaves the tool unavailable or the mission
analysis-only. Nebula does not fall back to a host command, generated shell
text, ambient `PATH`, mutable image tag, remote runtime socket, unverified
catalog, or automatic pull.
