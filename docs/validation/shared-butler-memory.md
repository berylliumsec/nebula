# Shared Butler memory boundary

The host-memory backend now keeps authoritative memories and pivotal-event
cursors in Core SQL on both SQLite and PostgreSQL. Every operation uses the exact
producer's project lease and commit fence. A write that crosses lease expiry
rolls back before indexing. Shared global content deduplicates under a common
writer lock, edits and deletes check revisions, and project-local rows stay
private. Derived Chroma hits are restricted to current authoritative rows;
foreign global recollections omit source references.

The explicit authenticated `host.memory/v1` protocol binds each peer to its
runtime incarnation. It carries bounded typed memory operations and an exact
project fence, never arbitrary SQL or method names. The worker adapter preserves
capture/recollection semantics without constructing a local SQLite connection
pool, index, or index paths. Novelty comparisons and monotonic replay cursors also
use host persistence. Unsafe-candidate and argument errors do not echo candidate
contents. The compatibility SQLite backend remains available to legacy launchers.

All 26 collected cases passed in 13.33 seconds: 22 new host/protocol cases across
isolated SQLite/PostgreSQL and four exact affected compatibility/capture cases.
They cover concurrent edits, global deduplication, host replacement, stale-worker
rejection, project/peer identity, redaction, cursors, novelty, malformed operations,
SQL rollback at lease expiry, and actual host-only Chroma with deterministic local
vectors. Workers opened no local memory or Chroma store. No embedding download,
provider network, operational installation, compute job or deployment was used.

Targeted mypy initially found a class-scope `list` annotation resolving to the
adapter's `list` method. Fresh independent source validation confirmed this static
name collision and ruled out a runtime response problem. Qualifying the builtin
preserves runtime behavior; no behavior repair or additional test run was needed.
Raw collection, receipt and outputs are `/tmp/nebula-shared-memory-*`.

Supervisor socket/lifetime wiring is now covered by the
[host-services evidence](runtime-host-services.md). A verified import of
compatibility memories is still required before enabling the full architecture. Shared knowledge
index operations, production execution construction, production LAN/reconnect,
mobile WebKit and packaged-desktop continuity evidence also remain pending.
