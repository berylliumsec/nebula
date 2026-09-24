# Assistant provider protocols

This crate implements the first OpenAI-compatible adapter for the bounded,
plain-text Assistant execution milestone. It does not execute parsed tools,
start helpers, dispatch agents, mutate storage, or authorize provider endpoints.
The caller validates stored profile locality/capabilities, resolves credentials,
and owns durable acceptance, event commits, whole-turn deadlines and recovery.

`OpenAiProvider` shares a `ProviderPool`. `stream` reserves admission and request
bytes before building its payload, then returns a bounded `ProviderStream`.
Each `ReceivedEvent` owns its global byte credits until dropped; the runtime
must hold that event through its durable commit. Slow UI subscribers must not
own this provider receiver. Dropping a stream cancels its internal request, not
the caller's shared cancellation token. `complete` returns an ordinary response;
its caller must account for that response after the integration call returns.

Defaults: 128 active requests holding connection permits, 256 admitted requests,
4 MiB encoded requests, 256 MiB shared working credits, 64 MiB event credits,
32 events per receiver, 256 KiB frames, 2 MiB accumulated replies, and 128 inert
calls. Working credits conservatively account for JSON/container amplification;
these are admission limits, not measured RSS guarantees. HTTP decompression is
checked against decoded limits. Leading frames hand over after 32, including
empty/whitespace frames, or fail on their byte bound. This explicitly bounds a
previously unbounded source whitespace path.

Reqwest 0.13.4 has no implicit retries, redirects, cookies, or default credential
headers. HTTP/1.1 matches HTTPX's default. Environment proxies remain enabled by
default; isolated fixtures turn them off. Gzip and deflate are enabled. Clients
are cached by origin and connect timeout, capped at 32 with no eviction; another
origin/timeout gets an actionable capacity error. Each client retains at most
20 idle connections for five seconds (at most 640 idle plus the configured active
connection bound). Different profiles at the same origin/timeout reuse sockets
while each request supplies a separate sensitive header snapshot. Stream headers
and reads have the configured idle timeout; nonstreaming connect timeout is ten
seconds and its headers/reads use the completion timeout. Whole-turn deadlines
remain the runtime's responsibility.

Explicit retries cover classified transient overloads and transport failures
only before source handover. Once output, malformed choices, semantic errors,
terminal evidence, or the leading-frame bound transfers ownership, failures do
not replay the request. Generic errors have no invented public error kind.
Incomplete clean EOF is a failure; a finish reason or `[DONE]` is required.
`[DONE]` terminates before parsing unrelated trailing bytes in the same read.

Compatibility evidence comes from `python-provider-protocol.json`, captured
using the real Python adapter with an in-process HTTPX transport. No live provider
or credentials participate. Four protocol and three local TCP tests cover
normalized payloads, response fields, split-byte framing, reasoning, error
classification, actual keep-alive reuse, retry boundaries, cancellation, decoded
size limits, malformed compression and redacted diagnostics. Validation is not
claimed until the coordinating task executes its diff-bound selection.

Remaining parity work is explicit: signed/grouped tool-result replay and
JSON-object instruction mode fail as unsupported; tool wire-name collision
recovery, permissive argument repair, full control-markup recovery, streamed
reasoning-detail/signature replay, vendor diagnostic text, non-UTF-8 charsets,
and other provider adapters are not complete. Tools remain inert data. Advanced
native tool/JSON replay must not be enabled by the parent runtime using this
milestone. Serialized tool-control markup is quarantined at finalization; the
runtime must also withhold possible control-prefix deltas from plain-answer
viewers. Provider discovery/catalog calls are not implemented here. No load,
100-agent, production UI, mobile, LAN, or live-provider result is implied.
