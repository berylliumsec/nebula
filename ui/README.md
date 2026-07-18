# Nebula 3 interface

This directory is the independent React/TypeScript interface and Tauri 2 shell for Nebula 3. It can run in a browser against the versioned Core API or as a local desktop application.

## Development

```bash
npm install
npm run dev
npm test
npm run build
```

Set `VITE_NEBULA_API_URL` only when the browser API is not served from the same origin. Vite proxies `/api` to `NEBULA_DEV_BACKEND` (default `http://127.0.0.1:8765`) during development. Preview data is visibly labeled and disappears only after Core health, authentication, and initial resource loading succeed.

To run the desktop application, install the repository's Poetry development
dependencies and launch Tauri from the repository root:

```bash
npm --prefix ui run tauri -- dev
```

The Tauri development hook builds the browser workspace, freezes the current
Nebula Core into the target-triple sidecar path, generates its build metadata
and third-party notices, and then starts Vite. The first launch therefore takes
longer than browser-only development with `npm run dev`.

## Usage recordings

Record the realistic Nebula 3 workflows described in
[`docs/NEBULA3_USAGE_SCENARIOS.md`](../docs/NEBULA3_USAGE_SCENARIOS.md):

```bash
npm run record:usage
```

The named WebM files are saved under the gitignored `usage-videos/` directory.

`nebula3 ui` launches the browser with a one-time token in the URL fragment. The runtime consumes `#token=…` into memory and immediately removes it with `history.replaceState`; it never stores the token in local or session storage.

## API boundaries

- HTTP resources are accessed only through `src/api/client.ts` under `/api/v1`. The client maps Core's snake_case entity arrays to UI summaries at that boundary; components do not depend on persistence records.
- Run events use `src/api/events.ts`. A selected run is required; reconnects replay after its last accepted monotonic sequence. The one-time token is carried in a WebSocket subprotocol, never in the URL.
- Human PTY sessions use `src/api/terminal.ts`. If the runner is unavailable, the terminal is inert; it never falls back to a host shell.
- Agent tool execution is intentionally not implemented in this interface. It must pass through the Core policy broker and certified sandbox runner.

## Desktop sidecar contract

The shell launches only a canonicalized `nebula-core` sibling binary. It clears inherited environment variables, binds Core to loopback with port `0`, and sends a 256-bit one-time IPC token over stdin as one JSON line:

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
