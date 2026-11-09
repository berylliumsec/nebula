# Shared knowledge index boundary

The host can now serve knowledge, Library retrieval, dense embeddings, tool
ranking and relevance scoring through authenticated `host.knowledge/v1` operations.
Retained workers use a synchronous adapter whose blocking operations run through
the index thread/context seam; they open no Chroma collection or model session.
Model warmup carries the original exact project context. A configured host index
is also the embedding source for its shared Butler-memory service.

SQL records the source attempt, content digest and derived publication state.
Admission checks the exact source revision and project fence before indexing;
positive publication checks the fence and attempt again. Work that loses its
lease closes only its own failed derived publication. Retrieval filters Chroma
results against current source visibility, ready state, artifact, attempt and
digest. This rejects an old query's chunks even when a new publication completed
while that query was running. Global Library results stay globally visible;
project sources remain scoped.

Per-attempt identities also protect ingestion results. Independent source
diagnosis confirmed that each old failure handler reloaded the newest revision
and could therefore mark a newer indexing/ready attempt failed. The revision CAS
only protected writes after that reload. Success and failure now check their
original attempt identity before updating status. The selected concurrency cases
cover both result types, both source kinds, ingest/reindex, and a newer attempt
that is either indexing or ready, on SQLite and PostgreSQL.

Document and tool bodies use purpose-bound encrypted references with a separate
explicit 32 MiB bound; the RPC envelope remains bounded at 2 MiB. Ordinary command
vaults retain their existing 16 MiB default. Known completed inputs are removed
only after purpose/integrity verification. Index work is joined through
cancellation so a disappearing request cannot release host ownership while its
thread still runs.

The initial 71 collected cases passed in 42.41 seconds. Two additional optional
relevance-transport cases passed in 3.51 seconds, for 73 distinct cases: 20 shared-index cases, 34 attempt
identity/context cases, two actual Unix-service index journeys, nine vault cases,
six exact knowledge ingestion/migration/reindex/failure cases and two exact
conversation-search cases. The relevance fixture first failed because it omitted
the existing typed reranker status property; fresh independent source diagnosis
confirmed the incomplete fixture, which was repaired without a product change.
Chroma used deterministic local four-dimensional
vectors; one synthetic text input was explicitly bounded between 2 and 4 MiB to
verify bulk transfer. No embedding/reranker download, operational provider,
existing installation, SSH, deployment or compute allocation was used. Ruff and
targeted mypy passed. Two existing WebSocket deprecation warnings remain.
Collection, receipts and results are `/tmp/nebula-shared-knowledge-*` and
`/tmp/nebula-shared-relevance-*`. Relevance transport used a deterministic host-only
scorer; it does not validate neural model accuracy.

Remaining integration is explicit. Host product-guidance synchronization is now
covered by the [host ownership evidence](runtime-host-services.md).
Production execution construction still needs wiring. Crash-orphan bulk payload
retention needs lifecycle cleanup. Deleting a legacy derived source without a
publication receipt defers physical cleanup; SQL-deleted sources remain excluded
from retrieval and the adapter reports that cleanup limitation. Full client,
desktop and deployer work, production LAN/reconnect, mobile WebKit and packaged
desktop gates remain pending. This boundary is not an end-to-end deployment
continuity claim.
