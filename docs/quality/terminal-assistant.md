# Paired terminal Assistant

Entry: `nebula-chat` or `nebula-chat shell`; the installed Core also exposes
`nebula-core chat shell`. Legacy `chat` still opens Butler.

Core owns projects, transcripts, runtime choices, approvals, goal snapshots,
queues and execution. The terminal owns only its revocable device credential,
last selected identifiers, presentation, and in-memory unsent drafts. Workspace
paths and skill paths always refer to Core; explicitly selected upload and
export paths refer to the client.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover/pair | A minimal client installs independently and uses normal TLS device pairing | Wheel metadata, Core pairing | Package and CLI tests, TLS LAN |
| Select/create | Searchable project and chat menus include New conversation and Butler; first send saves a main conversation | Core | CLI and real Core |
| Use | Models, skills, settings, attachments and goals are discoverable by name; unsupported controls explain why | Core catalogs and capabilities | Workflow contracts and real Core |
| Stream/interrupt | Input remains available, event identities stay separate, and cancellation targets the selected turn | Core turn and stream | Terminal process and LAN tests |
| Background/switch | Switching or exiting detaches the viewer; late output cannot enter another conversation | Core and terminal view identity | Async and process tests |
| Reconnect | Saved history and settings reload; following never resends uncertain work | Core, stream cursor | Disconnect and real Core tests |
| Mutate/retry | Revision conflicts require fresh review; success is read back; lost responses do not replay writes | Core | Workflow and LAN tests |
| Delete/revoke | Explicit deletion and revocation clear stale selection and credentials | Core | CLI and real Core |

Browser layouts, touch, Voice, local execution, editor, notebook and embedded
browser controls are inapplicable. Acceptance uses the actual terminal entry
point over authenticated, non-loopback TLS with synthetic data and inert
provider/harness fixtures. The package must exclude Core, database, model,
browser and GUI dependencies. Linux and macOS package/terminal evidence is
reported separately; unavailable platform evidence remains an explicit gate.

## Implementation receipt

The reviewed selection in `.github/test-selection.json` collects 85 tests.
On Linux/Python 3.12, the following bounded run passed **82 tests** in 201.69s:

```console
.venv/bin/pytest -q -s --tb=short tests/test_terminal_shell.py tests/test_terminal_package.py tests/test_terminal_pty.py tests/v3/test_chat_cli.py tests/v3/test_chat_cli_core.py tests/v3/test_terminal_core.py
```

The subprocess journeys used normal device pairing and private-CA verification,
candidate Python source, isolated temporary data, synthetic files and credentials,
and inert provider/harness fixtures. No frontend was served.

| TLS LAN origin | Observed journey |
| --- | --- |
| `https://172.26.91.245:55529` | Actual headless entry point, invitation, pairing, project creation, saved Butler transcript, Core restart/reconnect, SIGINT and revocation |
| `https://172.26.91.245:44965` | Main agent chat, one-turn skill, Core file excerpt, local document/image ingestion, settings readback/concurrent changes, retained goal skill snapshot, reconnect without replay, harness question and follow |
| `https://172.26.91.245:56475` | Durable follow-up, detach/quit preserving work, reconnect, exact-turn stop, exact approval rejection, actual PTY Ctrl-C |

The fixtures stop their Core and client processes on teardown. Request/stream
contracts separately prove no POST replay after lost acceptance or failed
reconciliation, distinct turn identities, late-event isolation, revision conflict
review, hidden sensitive answers, and origin isolation. POSIX terminal tests cover
bracketed paste, multiline Unicode input, resizing, and menu interruption.

The independent builder produced `nebula_client-3.0.0b3-py3-none-any.whl`
(about 39 KiB), a self-contained source distribution, and
`nebula-client-SHA256SUMS`. A clean virtual environment installs only HTTP and
terminal dependencies; imports and help load no Core, database, browser or GUI
modules. Focused Ruff, client mypy (10 files), Core mypy, and diagnostic audits
passed. The audit reports zero unclassified handlers or ignored results.

CI gates the 41 standalone client tests on Linux and macOS with Python 3.11,
3.12 and 3.13, and attaches wheel/source/checksums. Its Core job runs the full
reviewed 85-test selection, including three exact product-help checks for the
current source digest, terminal discovery, and Chroma synchronization. Local
product-help regeneration could not download the embedding model because the
managed environment's enforced proxy rejects its S3 URL; CI must supply that
evidence before merge. Release preparation stages the client distributions and
finalization verifies their checksums. No release or deployment was triggered
for this implementation.
