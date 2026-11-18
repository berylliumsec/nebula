# Nebula 3 interface

This directory is the React/TypeScript interface and Tauri 2 shell for Nebula 3. It can run in a browser against the versioned Core API or as a desktop application connected to a local or configured remote Core.

## Development

```bash
npm ci
npm run dev
npm run build
```

Choose the test file for the changed behavior and follow the
[focused test policy](../docs/TEST_SELECTION.md). For example, a change to
runtime defaults can use `npm test -- src/api/runtimeDefaults.test.ts`.
Unscoped `npm test` is rejected.

## End-to-end test structure

Real-Core journeys are being separated from `tests/real-core.spec.ts` into focused
specs: `authentication-real-core.spec.ts`, `first-user-real-core.spec.ts`, and
`runtime-settings-real-core.spec.ts`. The original test titles and CI area filters
are preserved. Other journeys remain in the original file for incremental migration.

These specs use `tests/fixtures/journey-test.ts`. Register each Core, model stub,
or request context with `resources.own()` immediately after acquiring it, before
starting the next resource. Teardown runs in reverse order and attempts every
disposer even when setup, assertions, or another disposer fail. Keep each test's
database, ports, browser context, and synthetic credentials independent.
Shared Core startup, model stubs, and visible navigation live in the adjacent
`real-core.ts`, `model-stub.ts`, and `operator-ui.ts` helpers. Wait for observable
UI or API state before reading it; avoid fixed sleeps.

Build the production UI before running these self-hosted journeys. They serve it
from disposable LAN Core instances and do not start a redundant Vite server.
For example, after recording the focused selection:

```bash
npm run test:e2e -- tests/authentication-real-core.spec.ts --project=assistant-real-desktop
```

CI collection and execution share `scripts/playwright_entry_args.mjs`. Only the
three isolated specs above use two workers; legacy specs keep one worker and their
existing shard behavior. Add a spec to that allowlist only after verifying resource
isolation. CI retains failure traces, screenshots, bounded fixture logs, and
`test-results/results.json` with per-test timings and retry outcomes. Use those
results to identify slow or retrying cases before changing timeouts or sharding.
Artifacts contain synthetic fixture data and should not use operator credentials.

Set `VITE_NEBULA_API_URL` only when the browser API is not served from the same origin. Vite proxies `/api` to `NEBULA_DEV_BACKEND` (default `http://127.0.0.1:8765`) during development. Preview data is visibly labeled and disappears only after Core health, authentication, and initial resource loading succeed.

To run the desktop application, install the repository's Poetry development
dependencies and launch Tauri from the repository root:

```bash
npm --prefix ui run dev:desktop
```

The desktop launcher first builds the browser workspace, freezes the current
Nebula Core into the target-triple sidecar path, and generates its build
metadata and third-party notices. It starts Tauri only after those steps finish;
Tauri's development hook can then start Vite immediately. The first launch
therefore takes longer than browser-only development with `npm run dev`.

## Usage recordings

Record the realistic Nebula 3 workflows described in
[`docs/NEBULA3_USAGE_SCENARIOS.md`](../docs/NEBULA3_USAGE_SCENARIOS.md):

```bash
npm run record:usage
```

The named H.264 MP4 files are saved under the gitignored `usage-videos/`
directory. Recordings use Nebula's Zero Layer theme; Playwright's intermediate
WebM files are removed after successful conversion.

`nebula-core ui` launches the browser with a one-time token in the URL fragment. The runtime consumes `#token=…` into memory and immediately removes it with `history.replaceState`; it never stores the token in local or session storage.

## API boundaries

- HTTP resources are accessed only through `src/api/client.ts` under `/api/v1`. The client maps Core's snake_case entity arrays to UI summaries at that boundary; components do not depend on persistence records.
- Run events use `src/api/events.ts`. A selected run is required; reconnects replay after its last accepted monotonic sequence. The one-time token is carried in a WebSocket subprotocol, never in the URL.
- Human PTY sessions use `src/api/containerTerminal.ts`. If the runner is unavailable, the terminal is inert; it never falls back to a host shell.
- Chat and mission tool requests flow through `src/api/client.ts`; Core owns policy checks, approvals, and execution.

## Desktop sidecar contract

In Local Core mode, the shell launches only a canonicalized `nebula-core` sibling binary. It clears inherited environment variables, binds Core to loopback with port `0`, and sends a 256-bit one-time IPC token over stdin as one JSON line:

```json
{"protocol":"nebula-sidecar-v1","ipc_token":"…"}
```

Core must reply on stdout within 60 seconds with exactly one bounded JSON line:

```json
{"protocol":"nebula-sidecar-v1","host":"127.0.0.1","port":49152}
```

No shell capability is granted to the webview. The trusted Tauri development
hook builds only the checked-out Core entry point; distributable Core and
installer packaging remain controlled by the audited release pipeline.
