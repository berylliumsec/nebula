# Headless Core and paired Butler CLI

Run Nebula Core on a server and chat with Butler from a terminal. Neither path
opens a desktop or browser, and neither requires a frontend build. Core retains
projects, model configuration, Butler conversations and running work. The client
holds only a revocable paired-device credential.

## Start headless Core

From a source checkout, install Python dependencies with `poetry install --only
main`. Node, Rust, a display server and downloaded browser binaries are not needed
for terminal text chat. Browser automation and container tools have their own
runtime requirements when used.

```console
poetry run nebula-core headless --host 0.0.0.0 --port 8765 --allow-remote \
  --tls-cert /path/to/core-cert.pem --tls-key /path/to/core-key.pem \
  --data-dir /path/to/nebula-data
```

Use a certificate valid for the Core hostname and trusted by the client. A
private CA works with the client's `pair --ca-cert /path/to/ca.pem` option; the
client remembers this path and continues to verify TLS. Keep the private key on
the Core host. The listener must also accept loopback connections so the host can
issue pairing invitations. `0.0.0.0` provides this for IPv4. Restrict access to
your intended network.

Core prints its host administration token once at startup. Keep it on the host;
remote chat uses device pairing. `NEBULA_V3_API_TOKEN` can supply the host token
for both startup and invitations. Headless mode keeps authentication enabled and
requires TLS plus `--allow-remote` for a remote binding. A loopback-only API can
also run as `nebula-core headless`; paired chat still needs an HTTPS listener.
The existing `serve` administration command remains available.

Use a dedicated data directory for a new server. To use existing saved projects
and providers, shut down their previous Core before opening that same directory.
Only one Core process may own a SQLite data directory.

## Pair entirely from the terminal

On the Core host, use its loopback origin and enter the host token at the hidden
prompt. Its TLS certificate must cover the loopback hostname or address too.

```console
poetry run nebula-core chat --core https://127.0.0.1:8765 invite \
  --ca-cert /path/to/ca.pem
```

The command displays a single-use pairing secret and six-digit confirmation
code. They expire after five minutes. On the client:

```console
poetry run nebula-chat --core https://nebula.example:8765 pair \
  --ca-cert /path/to/ca.pem
poetry run nebula-chat projects
poetry run nebula-chat chat --project "My project"
```

Omit `--ca-cert` when the system already trusts Core's certificate. Enter the
secret and code at the hidden prompts; they are not command arguments or saved
in shell history. A mistyped code can be retried with the same invitation within
Core's attempt and expiry limits. The CLI confirms the saved device with Core
before reporting pairing success.

`nebula-core chat` exposes the same subcommands as `nebula-chat`, including on an
installation that provides the Core executable. With a source checkout, the
client can also run as `python -m nebula.chat_cli`.

The client remembers the last paired Core. `--core` or `NEBULA_CORE_URL` selects a
different origin, which needs its own pairing. Saved cookies live in
`$XDG_CONFIG_HOME/nebula/chat.json`, or `~/.config/nebula/chat.json`, with file mode
0600. `--config` or `NEBULA_CHAT_CONFIG` chooses another file. No provider keys or
transcripts are saved there. Redirects never forward credentials to another
origin. If a certificate changes, update the trusted CA at its saved path before
reconnecting.

## Projects, models and conversations

`projects` lists names and IDs. `projects --create "My project"` creates a new
project and verifies Core's saved record. `chat` offers a numbered project menu;
`chat --project "My project"` selects by name. Duplicate names require a choice.

The CLI resumes the selected project's current Butler conversation, including
one started in the desktop. It reads that conversation's saved model and
assistant settings before each new message. For a project's first Butler chat,
it offers configured provider and harness models and honors the project's saved
runtime default. Configure a provider or harness on Core first; local provider
endpoints and `env:NAME` credential references refer to the Core host. Existing
provider and project REST APIs remain available for headless administration.

Messages stream in the terminal and remain in Core's transcript. Recent history
appears when reconnecting. Use these commands inside chat:

| Command | Effect |
| --- | --- |
| `/history` | Read the latest saved messages from Core |
| `/status` | Read the current turn's status |
| `/follow` | Attach to existing output without starting another turn |
| `/stop` | Cancel the current turn through Core |
| `/quit` or Ctrl-D | Exit the client; Core work continues |

Ctrl-C during a response requests cancellation of that exact turn if Core has
confirmed its identity. If the connection ends before confirmation, read
`/status` before stopping or sending again. The client never automatically
resends an uncertain message. A pending turn blocks another submission until it
finishes or is stopped. Approval and input pauses remain visible; browser-owned
actions such as file pickers, downloads and Voice still require their existing
device interface and cannot be completed in this text client.

Run `nebula-chat unpair` to revoke this terminal and remove its local credentials.
Devices can also be revoked through Core's existing paired-device controls. If
Core already rejected an expired or revoked pairing, `unpair` removes the stale
local entry; then issue a fresh invitation and pair again. A network failure
leaves the credentials available so revocation can be retried.

Unencrypted test networks require both Core's existing explicit insecure-pairing
option and `pair --allow-http`. The new headless command requires HTTPS for
remote clients and does not enable insecure pairing.
