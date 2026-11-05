# Headless Core and paired terminal Assistant

Run Nebula Core on a server and use Assistant or Butler from a terminal. Neither path
opens a desktop or browser, and neither requires a frontend build. Core retains
projects, model configuration, Butler conversations and running work. The client
holds only a revocable paired-device credential.

## Lightweight client installation

The separate `nebula-client` package supports Python 3.11–3.13 on Linux and macOS.
Install the wheel from the matching Nebula release in its own environment:

```console
pipx install /path/to/nebula_client-VERSION-py3-none-any.whl
```

The client requires only HTTP and terminal packages. It does not install Core,
model libraries, browsers, databases, Node or Rust. Configure models and tools on
Core first. For a source build, run `python -m scripts.build_terminal_client`;
the independent client build produces a wheel, source distribution and checksums.

Pair as described below, then run `nebula-chat` or
`nebula-chat shell --project "My project" --chat "My conversation"`.
`nebula-core chat shell` exposes the same shell on a full installation.

## Interactive Assistant

The shell checks Core terminal protocol 1. On an interactive TTY it opens a
full-screen view with a project/chat rail, transcript, live activity, and
composer. F2 browses projects, F3 browses conversations, F4 opens a searchable
command menu, F5 shows detailed activity, and F8 opens pending approvals. Typing
`/` at the start of the composer opens a selectable command list for the current
conversation. Keep typing to filter it, use Up/Down and Enter to run a selection,
or Tab to insert it without running. When no command list is open, Tab focuses
the transcript for scrolling. Ctrl-C stops a supported selected turn.
The view updates as Core accepts, queues, and works on a turn, before the first
answer text arrives. Progress and requests for action stay visible during input.
The activity strip shows **LOADING** as soon as a command or menu selection starts
opening. It stays visible through Core reads, then clears when the next choice or
view is ready. This local navigation status is separate from a running Core turn.
The conversation rail labels each chat **RUN**, **WAIT**, or **IDLE** using Core's
turn state. **WAIT** means Core needs a decision or intervention; `/status` shows
the next action. A **?** means activity could not be read, so it must not be
mistaken for idle. The rail refreshes every five seconds, and F3 reads fresh
states when browsing conversations. At narrow terminal widths, use F3 to see
those labels in the conversation picker.

Searchable menus include **New conversation** and **Butler**. The client remembers
the last selected project/chat separately for each paired Core and validates
them when reconnecting. Names, models and skills come from Core catalogs.
Use arrow keys and search in the full-screen menus. With `NEBULA_CHAT_PLAIN=1`
or redirected input, use numbered menus, `/next` and `/back` to browse pages;
`more` loads another catalog page.

| Command | Workflow |
| --- | --- |
| `/core`, `/projects`, `/chats`, `/new`, `/butler` | Switch paired Cores, select/create a project, resume a main agent chat, or open Butler |
| `/chat` | Rename, pin, flag, archive, restore, branch, rewind/edit, retry in a branch, export visible text, or explicitly delete |
| `/history`, `/history more`, `/context`, `/activity`, `/results` | Read saved history, context usage, work details and retained results |
| `/model`, `/settings` | Select Core models and supported next-turn reasoning, tools, MCP, hooks, knowledge, environments and subagent controls |
| `/skills`, `$name` | Select a Core skill for one turn; `$name message` invokes it in that message |
| `/goal` | Create, inspect, edit, start, pause, resume, cancel or complete a Core goal, including budgets and retained skills |
| `/agents` | Open main conversations or inspect delegated work and its supported stop control |
| `/approvals`, `/input` | Review an exact action or answer a pending question; secret answers use hidden input |
| `/queue`, `/schedule` | Manage follow-ups and provider-chat schedules saved and executed by Core |
| `/attach`, `/attachments` | Upload a client document/image, browse Core files/knowledge, preview exact excerpts, or remove pending attachments |
| `/status`, `/follow`, `/detach`, `/steer`, `/stop`, `/quit` | Read state, attach/detach output, guide a supported active harness, stop a selected turn, or exit |

Enter sends; Escape followed by Enter inserts a newline. Pasted multiline text
requires a separate send. While a turn is active, ordinary text queues a durable
follow-up; `/steer` explicitly guides a supported running harness. Ctrl-C at the
main prompt stops a supported selected response. Ctrl-D or `/quit` detaches and
leaves Core work running. Switching conversations also detaches the viewer.

Normal skills apply once. Goal skills retain Core's immutable snapshots and are
changed through `/goal skills`. Assistant settings apply on the next turn;
changes from another device are reread before sending. A revision conflict asks
for fresh review instead of overwriting another device's choice.

Local attachment/export paths refer to the client. Workspace and skill paths
refer to Core. Documents are uploaded to the project's Core knowledge library;
preview the exact bounded excerpt before attaching it. Image bytes are validated
by Core and require an image-capable runtime. Pending attachments and unsent drafts
stay in client memory; uploaded sources remain on Core after removing a draft
attachment. No provider keys or transcript mirror are stored on the client.

Output follows Core sequence/epoch cursors. Network recovery attaches only to
known existing turns; an uncertain submission is never automatically resent.
Use `/status`, `/history` and `/follow` to recover, or `/chats` to locate a newly
saved conversation whose acceptance response was lost. Core retains its existing
permissions, scope and data-sharing consent. Browser-owned actions, Voice,
editors, notebooks and embedded terminals still use their existing interfaces.

`NO_COLOR`, terminal resizing and plain numbered menus are supported. Set
`NEBULA_CHAT_PLAIN=1` for a scrolling prompt. Ctrl-G or Ctrl-C closes a full-screen
menu; `/cancel` or Ctrl-C closes a plain menu. Noninteractive prompt execution is
deferred.

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

## Legacy Butler chat

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
