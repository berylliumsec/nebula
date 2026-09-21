# MCP server configuration files

Nebula can import MCP servers from the JSON files that Claude Desktop, Cursor,
and VS Code already use, and export its own servers back to that format. You
can usually import an existing file unchanged.

```bash
nebula-core mcp import ~/.cursor/mcp.json            # preview only
nebula-core mcp import ~/.cursor/mcp.json --apply    # save the servers
nebula-core mcp export mcp.json                      # write Nebula's servers
```

New servers start **disabled** and local programs start **untrusted**, unless
you choose otherwise for that import: tick **Enable after import** (CLI:
`--enable`) and pick a **Tool approval** (CLI: `--approval ask`). Enabling a
local (stdio) program also needs **Trust** (CLI: `--trust-local-programs`),
because it runs on the Nebula host outside the automation container; without
it the program is saved disabled. Nebula probes every server it enables so its
tools are ready. You can also enable servers later: open **Settings →
Automation → MCP servers**, tick **I trust this local program** for a stdio
server (**Edit**), then **Probe** and **Enable** it.

Importing a file you imported before updates only what changed; see
[Preview, conflicts, and re-importing](#preview-conflicts-and-re-importing).

## Where to find an existing file

| Client | File |
| --- | --- |
| Claude Desktop | `claude_desktop_config.json` (Settings → Developer → Edit Config) |
| Claude Code | `.mcp.json` in a project |
| Cursor | `~/.cursor/mcp.json` or `.cursor/mcp.json` in a project |
| VS Code | `.vscode/mcp.json`, or the `"mcp"` key in `settings.json` |

Nebula never reads these locations by itself; you choose the file to import.

## File structure

The file is a JSON object with the servers under one of these keys, each
server keyed by its name:

```jsonc
{ "mcpServers": { "NAME": { ... } } }       // Claude, Cursor
{ "servers":    { "NAME": { ... } } }       // VS Code .vscode/mcp.json
{ "mcp": { "servers": { "NAME": { ... } } } } // VS Code settings.json
```

A single file may contain up to 200 servers. Names may use letters, digits,
`.`, `_`, and `-`; other characters are replaced with `-` and the preview says
so.

### Local (stdio) server

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
        "LOG_LEVEL": "info"
      }
    }
  }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `command` | yes | Program to run on the Nebula host. A bare name such as `npx` or `uvx` is resolved on the host `PATH` when you import; an absolute path is used as given. Relative paths are rejected. |
| `args` | no | List of arguments, passed literally. They cannot contain `${...}`; put values in `env` instead. |
| `env` | no | Environment variables for the program. See [Values and secrets](#values-and-secrets). |
| `cwd` | no | Absolute working directory on the host. Omit it to run in the Project workspace. |
| `type` | no | `"stdio"`. Inferred from `command` when omitted. |

### Remote (HTTP) server

```json
{
  "mcpServers": {
    "intel": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${INTEL_TOKEN}" }
    }
  }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `url` | yes | Streamable HTTP endpoint. Must be `https://` unless it is on the host itself (`localhost`, `127.0.0.1`, `::1`), and cannot contain credentials, a query string, or a fragment. |
| `headers` | no | Request headers. `Authorization: Bearer ...` becomes bearer authentication; every other header is treated as a secret. |
| `type` | no | `"http"` (also accepted: `"streamable-http"`, `"streamableHttp"`). Inferred from `url` when omitted. |

The legacy `"sse"` transport is not supported; use the server's streamable
HTTP endpoint.

## Values and secrets

Nebula never saves a secret value in a server profile.

| You write | Nebula stores |
| --- | --- |
| `"${NAME}"` or `"${env:NAME}"` | A reference to the environment variable `NAME` **of the Nebula Core process**, read each time the server starts. |
| A literal value in a header, or in an `env` entry whose name looks like a credential (contains `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `AUTH`, `COOKIE`, `SESSION`, `CREDENTIAL`, or ends in `_KEY`) | The value is moved into the operating-system credential vault and the profile keeps only the vault reference. |
| Any other literal `env` value | Stored as written. |

Only whole-value references work: `"Bearer ${TOKEN}"` in `Authorization` is
supported, but `"${HOME}/data"` is not. VS Code `${input:...}` prompts are not
supported; replace them with `${env:NAME}`.

If the host credential vault is unavailable, entries with literal secrets are
reported as invalid. Use `${NAME}` references instead, or pass
`--reject-literal-secrets` to refuse literal secrets outright.

## Nebula settings

An optional `nebula` object on a server sets options other clients don't have:

```jsonc
"burp": {
  "command": "npx",
  "args": ["-y", "burp-mcp"],
  "nebula": {
    "default_approval": "ask",
    "tool_overrides": { "scan_passive": "allow" },
    "disabled_tools": ["delete_project"],
    "tool_timeout_seconds": 300
  }
}
```

| Key | Values |
| --- | --- |
| `default_approval` | `risk_based` (default), `ask`, `allow`, or `deny` |
| `tool_overrides` | Object mapping a tool name to one of the approval values |
| `enabled_tools` | Only these tools are offered (default: all) |
| `disabled_tools` | These tools are never offered |
| `required` | `true` fails new sessions when the server cannot start |
| `startup_timeout_seconds` | Up to 120 (default 10) |
| `tool_timeout_seconds` | Up to 900 (default 60) |

`enabled` and `trusted_stdio` cannot be set from a file; that decision is
always made in Nebula, when importing or afterwards. A `default_approval` in
the file wins over the Tool approval chosen for the import.

## How the assistant uses MCP tools

In a provider chat, the servers ticked under MCP servers in Assistant settings
are always sent: every usable tool they offer is in the function list of every
request. Every other enabled, probed server with at least one usable tool is
available on demand. Its tools are not sent with the request. A server that is
disabled, has not been probed, or has no usable tool left is not offered and
does not fail the turn. The on-demand servers join only a turn that already
uses tools, such as a command-runtime chat or one with a ticked server, so a
plain chat stays one. A harness chat hands only its ticked servers to the
harness.

Instead of on-demand tools, the assistant sees three fixed tools:

- `tool_catalog.search` finds MCP tools by what they do.
- `tool_catalog.load` returns the full description and input schema for up to
  five tools.
- `tool_catalog.call` runs a tool by name with arguments that match its schema.

Core runs the real tool behind `tool_catalog.call`, so its arguments are
validated against the tool's own schema and its approval, risk, and scope rules
apply unchanged. Transcripts, approvals, and tool cards show the real tool name.

Because the list of tools sent to the model never changes during a
conversation, the provider's prompt cache stays valid across steps and turns,
with any provider.

Search runs on the Nebula host with the same local embedding model used for
project documents (all-MiniLM-L6-v2 in the local Chroma index), combined with
keyword matching. Nothing about the tools or the request leaves the machine for
search. Until the model has been downloaded and loaded, search uses keyword
matching only. Before each turn, the same search ranks the tools against the
operator's latest message: a strong match has its schema included for that
turn, and weaker matches are named as hints.

Loading on demand is on for every project. Set `on_demand_tools` to `false` on
a project's scope (`PUT /api/v1/engagements/{id}/scope`) to turn it off: only
the ticked servers are then offered, and all their tools go out with every
request. A tool listed in the scope's `always_loaded_tools` is sent with every
request whenever its server is offered, ticked or on demand. A project that opts into Jev suggestions from
TypeSafe uses Jev's picks instead of the local ranking, and falls back to the
local ranking when Jev is unavailable. Jev is sent the redacted operator
messages, the expanded instructions of the skills selected for the turn,
server names and descriptions, and tool names and descriptions — never tool
output.

Jev ranks two things in one call: the on-demand servers and their individual
tools. Ticked servers are already loaded, so Jev is not asked about them. It is never asked whether the turn needs a tool at all; every question
carries a "none of these" option instead. A server's rank weights the tools
that come from it — the top-ranked server's tools keep their full score and
the bottom-ranked server's tools keep half — and at most three tools survive:
up to two have their schema preloaded, and the rest are named as hints
alongside the servers most likely to hold what the request needs. A tool whose
question rated "none of these" higher is never preloaded, only hinted.

Jev is asked once per turn, and a ranking is reused when the request that
produced it recurs: the cache key covers the operator's message, the selected
skills, the tool list and the source descriptions, so a retried turn or a
repeated question against an unchanged catalog costs nothing, while the next
message, a new MCP server or a changed server description asks again. The cache
holds the newest 64 rankings in the Core process, records nothing on disk, and
never stores a failed call; a turn answered from it is marked `cached` in its
snapshot and reports no input tokens.

### Server descriptions

A server can carry a description of what it is for, up to 500 characters. Set
it in the server's Edit dialog, with a `description` field in an imported file,
or through the API:

```jsonc
"linear": {
  "url": "https://mcp.linear.app/mcp",
  "description": "Issue tracker for the web app: search, file, and close bugs."
}
```

Jev ranks a server by its description. A server without one is described by the
`instructions` string it returned at the MCP handshake, stored in its capability
snapshot, and a server with neither by its tool names. Handshake instructions are
written by the server, so like tool descriptions they steer a ranking and
nothing else.

Once tools are picked for a turn, by Jev or by the local ranking, the model is
told which server each pick comes from, and it gets the description of every
server behind a pick or ranked by Jev, as JSON data. Handshake instructions are
never sent to the model.

## Fields that are not imported

These are recognised and skipped with a warning in the preview:
`disabled` (enable servers in Nebula when importing), `alwaysAllow` and `autoApprove`
(set approvals in Nebula instead), `timeout` (use
`nebula.tool_timeout_seconds`), and `envFile` (reference variables with
`${NAME}`). Any other unknown field is skipped with a warning.

## Editor validation

Nebula publishes a JSON Schema for these files. An editor that uses it
autocompletes fields, shows what each one means, and flags mistakes such as a
relative `command`, the `sse` transport, or an unknown `nebula` setting before
you import. Save it from an installed Nebula:

```bash
nebula-core mcp schema > ~/.config/nebula/mcp-servers.schema.json
```

It is also in this repository as
[`mcp-servers.schema.json`](mcp-servers.schema.json). In VS Code, map it to your
MCP files in `settings.json`:

```jsonc
"json.schemas": [
  { "fileMatch": ["**/.cursor/mcp.json", "**/.mcp.json", "**/nebula-mcp.json"],
    "url": "file:///home/YOU/.config/nebula/mcp-servers.schema.json" }
]
```

or add a `"$schema"` key pointing at the file to a config you keep only for
Nebula; import ignores that key. The schema cannot see the Nebula host, so a
missing program or an unset `${NAME}` variable only shows up in the import
preview or when probing.

## Preview, conflicts, and re-importing

An import is a preview unless you pass `--apply` (API: `"dry_run": false`). The
preview lists, for each server, whether it will be created, updated, left
unchanged, replaced, skipped, or is invalid and why, the resolved command,
where each secret will come from, and whether it will be enabled. Secret values
are never shown.

Servers are matched by name. When a server already exists, Nebula compares it
with the file and saves only what differs; the preview lists each changed
setting with its old and new value. Importing the same file twice changes
nothing.

- The file owns the connection: `command`, `args`, `env`, `cwd`, `url`, and
  `headers`. A variable or header removed from the file is removed from the
  server.
- Settings made in Nebula (enabled state, approvals, tool overrides, timeouts)
  are kept unless the server's `nebula` block sets them.
- A `description` in the file replaces the saved one; a file without one keeps
  the description written in Nebula.
- A literal secret that matches the stored one keeps the stored credential; a
  different value is stored and the old one is deleted.
- If the `command`, `args`, `env`, or `cwd` of a trusted local program changes,
  it becomes untrusted and disabled again, unless you tick **Trust** (CLI:
  `--trust-local-programs`) for that import.
- Any connection change clears the server's probed tools. Nebula probes it
  again straight away if it stays enabled.
- Enable and Tool approval apply only to new servers.
- Servers that are not in the file are never deleted.

Pass `--skip-existing` (API: `"on_conflict": "skip"`) to leave existing servers
untouched, or `--replace` (API: `"on_conflict": "replace"`) to overwrite them
from the file and discard settings made in Nebula; a replaced server is treated
like a new one.

## Exporting

`nebula-core mcp export [FILE]` (API: `GET /api/v1/mcp-servers/export`) writes
all servers in the `mcpServers` format with a `nebula` block for any
non-default settings. Environment references are written as `${NAME}`. Secrets
stored in the credential vault are written as `${NAME}` placeholders and listed
as warnings: set those environment variables on the host where the file is
imported.

## API

```jsonc
POST /api/v1/mcp-servers/import
{ "config": { "mcpServers": { ... } },
  "dry_run": true,
  "on_conflict": "update",        // or "skip", "replace"
  "literal_secrets": "vault",     // or "session", "reject"
  "source_name": "mcp.json",
  "defaults": { "enabled": false, "default_approval": "risk_based" },
  "trust_local_programs": false }

GET /api/v1/mcp-servers/export?profile_id=ID&profile_id=ID
```
