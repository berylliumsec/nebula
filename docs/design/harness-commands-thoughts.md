# Harness commands and thinking

Entry: the chat composer with a Grok or Codex runtime selected. Commands must be
discoverable, execute against the selected harness session, and return a durable
visible result. Thinking supplied for display by either harness must have the same
discoverable, expandable presentation during streaming and after reload.

| Journey | Invariant | Authority | Planned evidence |
| --- | --- | --- | --- |
| Discover/select | Composer exposes supported command syntax for Grok/Codex | UI catalog, selected profile | component/browser |
| Create/use | Goal and usage commands reach native protocol operations without prompt wrappers obscuring command dispatch | Core turn and harness session | adapter + real Core |
| Stream/interrupt | Displayable thinking remains reachable; commands never become steering text | harness events, Core durable stream | component/browser + real Core |
| Refresh/reconnect/background | Saved command replies and thinking return with the selected conversation | Core transcript/activity, URL | real Core/browser |
| Failure/retry | Unsupported protocol operations fail visibly without silently invoking inference | adapter error, Core turn | adapter/browser |
| Delete/fork | Existing conversation deletion/fork behavior applies; no new state store | Core | existing lifecycle, no change |

Component state owns only disclosure and unsent composer input. No raw hidden
reasoning is requested or displayed. Test only changed adapter, command and
thinking journeys; use production desktop/mobile Chromium and mobile WebKit,
real Core and LAN evidence where available. Physical devices must be reported
separately. No full-suite run is authorized.

Underlying product rule: native capabilities need an end-to-end operator entry
point; event normalization must track the actual installed protocol, and retained
thinking must remain discoverable without opening multiple diagnostic panels.

The failure/retry/restart journey exposed catch-up notices consuming the remaining
chat height on compact screens. Their height is now bounded by the chat container
(`cqh`) so transcript controls remain reachable while notices can still scroll.

## Operator behavior and limits

Type `/` in the Grok/Codex composer to discover commands. Both integrations
include `/goal`, `/goals` (an alias), `/usage`, and `/help`; Grok also adds commands
advertised by the connected ACP session, including installed skills. Use `/help`
to connect a new or restarted session and read its current catalog. Goals support
an objective plus status, pause, resume and clear operations. Commands use the normal chat queue; to apply one
before an active turn finishes, use the existing Stop and send action. They are
not sent as live steering. Unknown or withdrawn commands fail visibly rather
than becoming model prompts. Codex terminal-only commands remain outside this
change because App Server has no generic slash-command dispatcher.

Grok usage describes the current harness process's session totals. Codex usage
is a native session estimate and explicitly reports unavailable estimates.
Reading cumulative usage does not add those totals to Nebula's turn billing.
Goal continuations retain the selected planning mode and Core context.

## Acceptance evidence — September 11, 2026

- Python: 27 passed. Exact selection: `tests/v3/test_harness_commands.py`, plus
  `test_grok_thinking_episodes_drain_completion_race`,
  `test_codex_reasoning_summary_uses_streams_and_authoritative_completion`,
  `test_codex_reasoning_summary_preserves_bounded_stream_without_snapshot`, and
  `test_codex_reasoning_summary_rejects_malformed_private_payloads` in
  `tests/v3/test_harness_adapters.py`. Command: `PYTHONPATH=src .venv/bin/python -m pytest -q <selected nodes>`.
- Frontend: 26 passed via `npm --prefix ui test -- src/components/HarnessThinking.test.tsx src/pages/harnessActivity.test.ts src/components/HarnessStatusRail.test.tsx`.
- Build: `npm --prefix ui run build` passed. The tested servers served this worktree's
  `ui/dist`; Vite reported its existing large-chunk advisory.
- Mocked production browsers: 16 passed via `npm --prefix ui run test:e2e -- tests/interface.spec.ts --grep 'stabilization harness commands and thinking'`
  with the eight `entry:harness-commands/*` projects recorded in `.github/test-selection.json`.
  Origin: `http://192.168.1.155:19465`, using Vite preview of the production bundle.
- Real Core: eight passed via `npm --prefix ui run test:e2e -- tests/real-core.spec.ts --grep 'assistant upgrade native commands retain thinking'`
  with the eight `entry:harness-commands-real/*` projects in that selection.
  Each case used a disposable Core on a non-loopback LAN origin, served the
  production bundle, and exercised both Grok and Codex through the real adapters
  with inert protocol peers: goal creation, thinking, invalid usage, correction
  and retry, usage, Core restart, transcript/activity recovery, and goal status.
- Desktop Chromium: 1440 and 1024 px. Emulated Android Chromium and iPhone WebKit:
  320, 390 and 430 px. Keyboard disclosure, focus, accessible names, long thinking,
  touch targets and no horizontal clipping were checked. Desktop and 320 px WebKit
  screenshots were visually inspected; catch-up and thinking remain reachable together.
- Read-only installed-peer checks: Codex CLI 0.153.2 and Grok 1.0.25 accepted their
  session-usage RPCs. The Codex goal-read probe used an ephemeral thread and was
  rejected because ephemeral threads do not support goals; it is not goal validation.
- Ruff checks and `git diff --check` passed.

Local logs: `/tmp/nebula-harness-build.log`,
`/tmp/nebula-harness-browser-mock-final.log`, and
`/tmp/nebula-harness-browser-real-final.log`. Screenshots are in
`/tmp/nebula-harness-browser-real-final/`. The locally reviewed selection receipt
is `/tmp/nebula-harness-selection-receipt.json`; no CI receipt has been uploaded.

Limitations: live vendor model-driven goal execution, physical phones/software
keyboards, and deployment to the installed application were not exercised.
Native protocol peers are explicitly fixtures in real-Core acceptance, not live
models. These limits prevent an unqualified production-completion claim.

## Dynamic command catalog contract — September 12, 2026

| Journey | Observable invariant | Authority | Test layers |
| --- | --- | --- | --- |
| Discover/select | Slash picker and help share names, descriptions and argument hints; vendor additions require no UI edit | ACP live session catalog, Core compatibility catalog | protocol, component, production browser |
| Create/use | Advertised commands reach session/prompt unchanged ahead of Core context; unknown commands fail without inference | current connection catalog | adapter, real Core |
| Idle/stream/update | Startup and idle advertisements survive turn replay draining; replacements remove old commands | protocol reader, scoped external session ID | protocol, real Core |
| Refresh/reconnect | Browser reload reads current catalog; disconnected/Core-restarted sessions expose only compatibility commands until reconnection advertises again | live connection, Core activity API | component, real Core |
| Failure/retry | Removed or unknown commands explain /help recovery; no silent model fallback | dispatch-time catalog | adapter, browser |
| Fork/delete | Catalog never transfers between external sessions; connection teardown discards ephemeral discovery | existing connection lifecycle | protocol isolation |

Catalogs are ephemeral capabilities, not durable transcript state. Replies retain
existing durable chat handling. New sessions can use /help to connect and discover.
Codex keeps explicit RPC bridges; its terminal command names are not advertised as
executable. Existing permissions remain authoritative. Physical phone and installed
application validation remain separate gates. Focused selection will extend the
existing command/thinking entries, not unrelated journeys or full suites.

## Dynamic discovery acceptance — September 12, 2026

- Journey: slash picker reads Core's session catalog, shows native argument hints,
  invokes an advertised command, removes a withdrawn command, rejects its retry,
  and retains the reply after reload. Existing goal/usage/thinking journeys and
  durable replay across Core restart remain covered for Grok and Codex.
- Python: 34 passed in 1.55 seconds. Same files/node filters as the selection above;
  the command file now covers catalog normalization, startup/idle capture, session
  isolation, replacement, generic invocation, help, unknown/withdrawn commands,
  no-text replies, and maximum-size catalogs.
- Frontend: 27 passed in 1.44 seconds across the three selected files. Includes
  live catalog equality, dynamic picker replacement, argument hints and selection.
- Production build passed. Index SHA256:
  `f08899b9619d689d0a94bf75ddab6de3e3ff2697ee425489cc009e39cf911cd1`.
- Mocked production browser journey: 16 passed in 1 minute at
  `http://192.168.1.155:19475`. Exact filter remains
  `stabilization harness commands and thinking` in `ui/tests/interface.spec.ts`.
  Projects: desktop, compact, mobile-chromium-small,
  mobile-chromium-ledger-390, mobile-chromium-wide, mobile-webkit-small,
  mobile-webkit, mobile-webkit-wide. Covered keyboard selection/focus, 44 px
  targets, accessibility scans, long hints, reload and horizontal bounds.
- Real-Core production journey: 8 passed in 2.3 minutes on disposable
  `http://192.168.1.155:<allocated-port>` origins. Exact filter remains
  `assistant upgrade native commands retain thinking` in `ui/tests/real-core.spec.ts`.
  Projects: assistant-real-desktop, assistant-real-compact, and
  assistant-real-{chromium,webkit}-{320,390,430}. These use inert native protocol
  peers with the real adapters, Core, durable store, API mapping and UI.
- Desktop Chromium 1440/1024 and emulated Android Chromium/iPhone WebKit
  320/390/430 passed. Desktop and 320 px WebKit screenshots visually reviewed.
- Installed Grok advertised 52 commands in an isolated temporary workspace.
  The real ACP reader captured the catalog at startup; `/session-info` then ran
  through generic dispatch and returned native session details with turn count 0.
  This verifies discovery and one read-only native command, not every advertised
  operation. No vendor model-driven goal was executed.
- The small-screen mock initially exposed long picker hints crowding transcript
  controls after draft restoration. The final bounded picker and one-line visible
  hints passed the same matrix. One initial real-Core case collided with a build;
  the complete final matrix ran after the build finished and passed.
- Ruff (Python 3.12 target) and diff whitespace checks passed. Reviewed local
  selection receipt: `/tmp/nebula-command-discovery-selection-receipt.json`.
  No CI receipt was uploaded; no push or installed-app deployment was performed.

Logs: `/tmp/nebula-command-discovery-{python,frontend,build,mock-final,real-final}.log`.
Screenshots: `/tmp/nebula-command-discovery-{mock-final,real-final}/`.
Live checks: `/tmp/nebula-command-discovery-live.log` and
`/tmp/nebula-command-discovery-invoke.log`.

Remaining limits: physical phones/software keyboards, installed-app deployment,
and live vendor goal execution are unverified. Codex retains explicit RPC bridges;
this change does not provide Codex terminal-command parity. Browser discovery is
live-session state: after Core restart only compatibility commands are shown until
reconnection supplies a fresh advertisement. Native catalogs are bounded to 256
entries and are never copied across sessions.
