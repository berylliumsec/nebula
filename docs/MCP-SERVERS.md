# MCP server configuration files

Nebula can import MCP servers from the JSON files that Claude Desktop, Cursor,
and VS Code already use, and export its own servers back to that format. You
can usually import an existing file unchanged.

```bash
nebula-core mcp import ~/.cursor/mcp.json            # preview only
nebula-core mcp import ~/.cursor/mcp.json --apply    # save the servers
nebula-core mcp export mcp.json                      # write Nebula's servers
```

Imported servers always start **disabled** and local programs start
**untrusted**. After importing, open **Settings → Automation → MCP servers**,
review each server, tick **I trust this local program** for stdio servers you
trust (**Edit**), then **Probe** and **Enable** it.

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
always made in Nebula.

## Fields that are not imported

These are recognised and skipped with a warning in the preview:
`disabled` (servers always start disabled), `alwaysAllow` and `autoApprove`
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
preview lists, for each server, whether it will be created, replaced, skipped,
or is invalid and why, the resolved command, and where each secret will come
from. Secret values are never shown.

A server whose name already exists is skipped. Pass `--replace` (API:
`"on_conflict": "replace"`) to overwrite it; the replaced server is disabled
and untrusted again so it is reviewed before use.

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
  "on_conflict": "skip",          // or "replace"
  "literal_secrets": "vault",     // or "session", "reject"
  "source_name": "mcp.json" }

GET /api/v1/mcp-servers/export?profile_id=ID&profile_id=ID
```
