# Independent desktop runtime

The desktop is a viewer of the independently owned local Core. It uses a short
`desktop-connect` process to obtain a private, durable connection; closing its
bootstrap pipe does not own Core's lifetime. Linux is the currently supported
local process boundary. A remote Core remains available on other platforms.

| Journey | Observable invariant | Authority | Required evidence |
| --- | --- | --- | --- |
| First local connection | One supervisor owns a stable listener and private credential | Private runtime directory and host lock | Source and installed process acceptance |
| Reopen, close or desktop update | Same Core identity and admitted work survive | Supervisor, SQL receipts and retained bundles | Installed desktop close/update/reopen |
| Supervisor recovery | Reattach on the recorded port using verified incarnation and credential | Private discovery plus authenticated health | Process recovery acceptance |
| Shutdown | Explicit confirmation, durable successful receipt and observed service exit | Host shutdown operation | Component plus real host acceptance |
| Blocked shutdown | Explain the blocker; never claim completed shutdown | Host receipt | Component plus effectful-worker acceptance |
| Legacy root | One explicitly coordinated offline adoption preserves existing data | Existing data root and operator acknowledgement | Adoption unit plus migration acceptance |

`runtime-adopt --confirm-core-stopped --data-dir ...` is required for an existing
SQLite or explicitly configured external database with no durable runtime
credential. Coordinate the old Core's shutdown first; absence of the new lock
does not prove a legacy Core has stopped. Data roots are never merged. An
existing `NEBULA_V3_API_TOKEN` can be retained during adoption without printing it.

Frozen packages contain `nebula-runtime.json`, an executable, UI and runtime
resources. Installation verifies all hashes before copying into the private
retained store. Compatibility ranges come from `runtime-compatibility.json`;
changes to the migration tree fail packaging until that review record is updated.
The source-development equivalent retains Nebula code but still requires the
selected Python environment. That does not certify retained frozen dependencies.

The launcher accepts a recorded private supervisor only after authenticated
health. A live but unreachable supervisor is not replaced. A cold-start timeout
does not kill a service that may have admitted work. PyInstaller bootstrap PID
differences are handled by a per-start nonce and actual kernel incarnation.

Native close/restart only detaches. The explicit shutdown control waits for
`runtime-stop`, which requires the durable operation to succeed and the exact
service incarnation to exit. The current Tauri window blocks automatic restart
after an explicit shutdown. A missing supervisor is not proof its retained
workers stopped and is reported as requiring reconnection.

SIGTERM to `serve` also only detaches. The stable listener gives open viewer
streams (such as `/api/v1/work/events`) 5 s and then closes them; clients
reconnect with their cursors. Project workers deliberately keep running with
their grants when their supervisor exits or dies: the next `serve` rediscovers
and adopts them, and only `runtime-stop` (or that next supervisor's
reconciliation) retires them. Workers never stop on supervisor loss alone.

Desktop updates alone do not promote a different API. API promotion and rollback
are explicit host deployment operations; they must be exercised independently of
viewer reopen. The legacy `--handshake-stdout` protocol and unauthenticated remote
mode are refused explicitly. Headless source use needs no frontend build; it has
a minimal retained status page when the production UI is absent.
Explicit remote browser diagnostic ingress is currently unavailable rather than
silently ignoring its compatibility flag.

Local evidence covers selected launcher, bundle, CLI and packaging tests, the
explicit shutdown component journey, product catalog discovery, and a production
UI build. Native Rust/Cargo is unavailable in this environment. Installed desktop,
actual frozen-dependency continuity, real-Core LAN, mobile browsers and physical
devices remain unverified here; this implementation is not a completed packaged
continuity acceptance receipt.
