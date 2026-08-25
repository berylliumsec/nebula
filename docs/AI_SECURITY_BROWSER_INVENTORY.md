# Nebula AI Security Browser implementation contract

Status: active implementation contract for `codex/web-proxy-ai` in the
`nebula-web-proxy-ai` worktree. This contract is a required whole-product
upgrade, not a claim that every gate below has passed.

## Delivered and bounded in this branch

This branch delivers durable sessions/tabs/timelines, isolated named identities,
redacted HTTP/1.1 and HTTP/2 history, WebSocket frame metadata, two-exchange
authorization diffing, reviewed edit/replay through the active identity,
automatic Project CA generation with fingerprint/expiry/trust instructions, a
session-owned native proxy, declarative bounded request/response/WebSocket
rules, compiled Project scope, redacted bounded body artifacts, HTTP/HTTPS
CONNECT and SOCKS5 upstream chaining, immutable semantic page Evidence, durable
run-scoped AI leases/commands/rules, and a paired-desktop command worker.
Project scope also normalizes equivalent hostname and root-URL entries, exposes
an explicit warning-confirmed all-target mode, and accepts the current page from
the native browser's **Add to scope** context-menu action.

It does **not** claim pixel screenshots or browser-extension hosting. Body
artifacts are explicit, text-format bounded captures redacted by Core before
immutable storage; binary/compressed/oversized bodies are retained as metadata
only. The native connector supports authenticated HTTP/HTTPS CONNECT and
SOCKS5 chaining with strict TLS verification and no URL userinfo. The semantic
page Evidence capture is deliberately named as such and is not relabeled a
screenshot.

## Outcome

An operator enters **Workbench > Browser**, selects a durable research session
and named identity, opens an authorized target, observes redacted HTTP and
WebSocket traffic, and can replay or compare exchanges without exporting the
identity's cookies. Captures become hash-addressed Evidence. AI may inspect an
explicit bounded snapshot and, when the operator starts a lease, issue durable
native browser/proxy commands inside its frozen scope. The same durable session,
history, evidence, commands, and queued navigation handoffs remain visible after
refresh, restart, reconnect, and from a paired LAN/mobile client.

## State authorities

| State | Authority | Rule |
| --- | --- | --- |
| Engagement scope and active window | Core `ScopePolicy` | Core revalidates mutable operations; the UI badge is explanatory only |
| Session, tabs, navigation timeline, capture policy | Core browser entities | Native and web clients rehydrate from Core and update with optimistic revisions |
| Cookie/cache/local-storage partition | Native webview profile keyed by Project + browser identity | Secrets do not enter ordinary Core entity JSON |
| Traffic metadata, redacted headers, and body artifacts | Core traffic entities + immutable artifact store | Body artifacts are explicit, text-format bounded, redacted before storage, and referenced by ID only |
| Raw bodies, DOM, screenshots, HAR, receipts | Artifact store + immutable `Evidence` | Exact SHA-256 and provenance; raw sensitive reads require acknowledgement |
| Pending AI/browser actions | Core browser action entity | Proposal is inert; execution requires current scope plus explicit approval |
| Autonomous authority, commands, run-owned rules | Core lease/command/rule entities | Pinned scope revision, paired desktop claim, expiry, idempotency, and budgets |
| Project CA and proxy execution | Session-owned native runtime | CA private key stays native; proxy starts with no compiled scope and blocks until one is installed |
| Live webview and native command worker | Desktop application shell | The worker remains mounted across Browser-page navigation; mobile never claims native commands |
| Mobile/desktop handoff | Core expiring command | Paired clients may queue bounded navigation/focus; only desktop owns live DOM |

## Observable invariants and lifecycle matrix

| Journey step | Observable invariant | Test layer |
| --- | --- | --- |
| Discover | Browser opens on a session workbench that explains identity, capture mode, scope, traffic, and required recovery | component + desktop/mobile Playwright |
| Create | A first-use Project gets one default session and identity; named identities create isolated native storage partitions | domain + real Core + native |
| Select/use | URL, selected session, tab, and identity agree; unsupported native-only controls are explained on LAN/mobile | component + real Core |
| Navigate | Only credential-free HTTP(S) URLs open; Core scope is rechecked and final URL is recorded with its scope revision | Rust + Core + desktop dogfood |
| Capture | HTTP/1.1, HTTP/2, and WebSocket metadata is bounded; secrets are redacted by default; body capture is explicit and visibly sensitive | Rust + integration |
| Inspect | History filters, request/response detail, WebSocket frames, copy, replay, and side-by-side diff remain usable with long or binary content | component + Playwright |
| Replay | Replay is derived from a recorded exchange, visibly editable, scope checked, bound to one identity, and produces a new linked exchange | domain + Rust + real Core |
| Compare identities | The same request can be replayed in two named cookie jars and the status/header/body diff identifies authorization variance | domain + native + Playwright |
| Evidence | Screenshot, DOM, request/response, WebSocket transcript, or session bundle becomes an immutable Evidence record with exact digest and provenance | real Core + artifact integrity |
| AI inspect | An explicit, bounded, untrusted snapshot excludes values/cookies/storage; autonomous results are marked untrusted | Rust + component + real Core |
| AI act | Autonomous commands are lease-bound, target/scope checked, budgeted, idempotent, paired-device claimed, and revocable; supervised proposals remain inert until approval | domain + Rust + real Core |
| DevTools/proxy | Desktop exposes DevTools plus automatic local CA generation, explicit trust, scope compilation, native declarative rules, and protected upstream chaining | native + packaged dogfood |
| Refresh/reconnect | Sessions, tabs, timeline, traffic, evidence links, and pending proposals reappear without duplication; native tabs rehydrate explicitly | real Core + packaged desktop |
| Mobile/LAN | A paired device can inspect durable state and queue an expiring navigation/focus handoff, but cannot claim live-DOM ownership | real Core + production LAN |
| Failure/retry | Errors retain safe state, identify the failed layer, and make idempotent retry explicit | component + real Core |
| Close/revoke | Closing a session is durable; deleting an identity requires confirmation and clears only that partition; Project clear cannot affect another Project | native + real Core |

Lifecycle coverage is required for discover, create, select, navigate, capture,
inspect, replay, compare, evidence, propose, approve/reject, execute, interrupt,
background, reconnect, refresh, close, revoke, and clear. Multi-step autonomy is
permitted only as a sequence of separately approved deterministic actions unless
the operator has enabled an explicit bounded approval policy.

## Security and privacy contract

1. Navigation, replay, capture, and action execution fail closed when Project
   scope is missing, inactive, stale, or does not authorize the final target.
2. `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, CSRF-like
   headers, form values, and WebSocket credentials are never stored in normal
   entity JSON or included in AI context. Redacted records keep a stable SHA-256
   marker so equality can be compared without disclosure.
3. Default capture is metadata plus redacted headers. Body artifacts require
   explicit per-session body mode, are capped at 1 MiB, accept only supported
   text/JSON/form/XML media types, and are redacted by Core before storage.
   Binary, compressed, malformed, and oversized bodies remain metadata-only or
   fail closed; they are never silently stored as raw artifacts.
4. Replay uses the chosen native identity partition. A replay editor cannot add
   credential-bearing URLs, target a different Project, or bypass current scope.
5. The generated local interception CA is Project-local, never exported or
   installed globally without an explicit operating-system action, and can be
   rotated or revoked. Upstream-proxy credentials use protected credential
   storage rather than Core JSON.
6. AI observes only an explicit, bounded snapshot or durable redacted traffic
   summary. Autonomous browser/proxy commands require an expiring lease pinned to
   one Project scope revision, identity, target subset, risk set, and budgets.
   Native results and page/traffic content are marked untrusted. There is no
   arbitrary JavaScript, cookie/storage/password/token-read tool, or raw secret
   argument; credential use accepts only an authorized `credential_ref` and is
   resolved ephemerally for bounded fill actions.
7. The existing supervised proposal path remains inert. Execution re-resolves a
   semantic locator to exactly one element and records the scope revision, page
   URL, action digest, result, and resulting evidence.
8. Mobile continuity carries durable metadata and expiring commands, never live
   cookies, raw identity storage, or an implicit remote-control channel.
9. Interception rules are declarative, bounded, run-owned, expiring, and
   fail-closed on invalid or out-of-scope mutations. App restart releases them
   without forwarding modified data.

## Product composition

The benchmark is composite: request/response inspection, WebSocket and HTTP/2
capture, declarative interception, replay, diff, Project CA lifecycle, isolated
browser contexts and DevTools, and AI observe/propose/approve/act with durable
lease-bound receipts. Nebula's differentiator is that those primitives share
Project scope, Evidence, chat context integrity, durable research timelines,
provider policy, identities, and paired-device continuity instead of living in a
separate proxy application.

## Test layers and completion boundary

Required release evidence is: Python domain/API/storage tests; Rust capture,
redaction, partition, replay, and action tests; React component tests for every
state; real-Core persistence and reconnect; production bundle at 1440 and 1024
desktop widths; mobile Chromium and WebKit at 320, 390, and 430 widths; a
non-loopback paired LAN origin; packaged native DevTools/proxy/identity dogfood;
and physical-device mobile validation where available.

No partial slice, mocked route, build success, or desktop viewport upgrades this
contract to “complete.” The final handoff must name any missing native platform,
LAN, packaged, engine, or physical-device evidence and describe the resulting
limitation.
