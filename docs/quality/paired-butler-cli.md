# Paired Butler terminal chat

Entry: `nebula-core headless` to run Core without a GUI or frontend build;
`nebula-chat invite` on the Core host, then `nebula-chat pair` and
`nebula-chat chat` on the operator's terminal. This is a terminal-only client of
an existing Core; it does not start Core or open a browser.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover | Help explains host invitation, pairing, project selection and chat | CLI help and product help | CLI and product-help tests |
| Pair | Only a host-issued, single-use secret plus code creates a device session | Existing Core pairing API | CLI and isolated TLS Core |
| Select | Project names are discoverable; ambiguous names require a selection | Core project list | CLI tests |
| Use/stream | Text reaches the selected project's ordinary Butler conversation | Core transcript and saved assistant configuration | CLI and isolated TLS Core |
| Interrupt | Ctrl-C stops the exact submitted turn when its identity is known | Core turn cancellation | Stream regression and isolated Core |
| Reconnect | Reopening reads Core history and attaches to existing work without resubmitting | Core transcript and pending turn | CLI and isolated TLS Core |
| Failure/retry | Auth, TLS and network errors give a recovery action; uncertain sends are never replayed automatically | Core response and local connection | CLI tests |
| Revoke | Unpair revokes the current device before removing local credentials | Core device list | CLI and isolated TLS Core |
| Protect credentials | Saved device cookies are private and scoped to one exact Core origin | Local config file | CLI tests |

Browser layouts, touch, WebRTC, browser navigation, file handoffs and production UI
bundles are not applicable to this terminal-only entry point. Existing browser
destinations remain the owners of browser-only actions. No real provider, harness,
operator project, existing Core process or deployment is used for validation.

## Acceptance evidence

- Client and headless command checks: 38 passed with
  `.venv/bin/python -m pytest -q tests/v3/test_chat_cli.py`.
- Actual Core journey: one passed in 71.83 seconds with
  `.venv/bin/python -m pytest -q -s tests/v3/test_chat_cli_core.py --tb=short`.
  Origin: `https://172.26.91.244:53731`; candidate Python source, TLS verified
  against the fixture CA, normal authentication and pairing enabled, no UI build.
- The committed journey launches `nebula-core headless` as a subprocess, issues
  an invitation from loopback, pairs over the LAN address, rejects invitation
  reuse, creates and reads back a project, streams an inert local provider reply
  into the project's ordinary Butler transcript, restarts Core and reconnects
  without duplicating history, sends SIGINT and reads back that exact turn as
  cancelled, then revokes the terminal and verifies the device list is empty.
- Temporary Core processes stop, and the successful journey removes its Core
  database, model/index data, paired credentials, certificate and key. Its JSON
  receipt is retained separately from this disposable data.
- Ruff lint/format and focused mypy checks pass for the changed Python modules.
  The repository diagnostic blind-spot audit reports zero unclassified handlers
  or ignored results. The installed Core command exposes the same client help.
- Independent read-only validation confirmed the menu's unavailable Typer type,
  harness pause frames, model discovery, saved runtime controls and fixture
  contract issues before repair. Tests cover those client paths. Core's Scratch
  project is distinct from the named test project; cancellation is verified via
  the session state projection rather than an unregistered generic turn route.

Selection: 42 collected Python tests: 38 client checks, this one live journey,
and three exact product-help checks. No unrelated suites. Browser/device layout,
physical devices, production UI bundles and native packaging are not exercised;
they do not implement this terminal-only surface. A fresh package build and the
three product-help checks remain unavailable locally: rebuilding the bundled
index needs Chroma's MiniLM model, whose download is rejected by the environment
HTTP proxy with 403. This is partial verification until the selected CI or an
environment with that model completes package generation, current-source digest,
CLI guidance retrieval and Chroma synchronization checks. Do not merge without
that evidence.
